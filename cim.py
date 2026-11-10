#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# cim.py - CoGS Image Manager, text edition.
#
# Share a folder of Apple II disk images (and music and pictures) to every
# CoGS card on your LAN from any machine with a Python interpreter: a
# PowerPC Mac on Tiger or Leopard, an Intel Mac on whatever it was left
# on, a Raspberry Pi, a NAS, a Windows box with Python installed. Same
# wire contract as the Mac and Windows Image Manager apps and a2cogs.com,
# so the card cannot tell them apart.
#
#     python cim.py ~/Apple2Disks
#
# One file, standard library only. Written to the Python 2.5 subset so the
# same file runs unchanged on 2.5, 2.6, 2.7 and 3.x. Do not add: b''
# literals, "except X as e", str.format, set literals, dict
# comprehensions, argparse, json, collections.namedtuple, os.path.relpath,
# bytearray, print() calls. See README.md.
#
# Screen: a curses text UI (ASCII box drawing, green on black) when the
# terminal has one; a line-mode prompt otherwise (--plain), or nothing at
# all (--headless) for launchd, cron, or a NAS.
#

import os
import re
import sys
import time
import socket
import signal
import hashlib
import threading
import subprocess
import optparse

try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn
except ImportError:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

try:
    from urllib import unquote as _unquote_bytes
except ImportError:
    _unquote_bytes = None
    from urllib.parse import unquote as _unquote_text

try:
    from email.utils import formatdate
except ImportError:
    from email.Utils import formatdate

try:
    import curses
except ImportError:
    curses = None

VERSION = "0.3"
DEFAULT_PORT = 8271
BLOCK = 512
PER_PAGE = 16            # disks: what Image Manager serves; the card holds 18
AUDIO_PER_PAGE = 17      # audio and pictures
HUB_MAX = 12             # one hub screen on the card
HUB_LABEL_MAX = 30       # 40 columns minus badges and count
FILES_SUB = u"files"
FILES_MAX_NAME = 64
FILES_MAX_BYTES = 4 * 1024 * 1024
AUDIO_RATE = 21973       # Tool225 / DOC true rate
SHR_BYTES = 32712
CFG_NAME = u".cim-text.cfg"

DISK_EXT = (u"po", u"hdv", u"2mg")
REJECTED_EXT = (u"dsk", u"do", u"woz", u"nib")
AUDIO_SRC_EXT = (u"mp3", u"m4a", u"wav", u"aif", u"aiff", u"flac", u"ogg")
PICTURE_SRC_EXT = (u"jpg", u"jpeg", u"png", u"gif", u"webp", u"bmp")
PACKAGE_DIR_EXT = (u".app", u".bundle", u".pkg", u".framework", u".photoslibrary")
SIG_EXT = DISK_EXT + REJECTED_EXT + AUDIO_SRC_EXT + PICTURE_SRC_EXT + (u"pcm", u"shr")

# ---------------------------------------------------------------- compat

PY2 = sys.version_info[0] == 2
if PY2:
    text_type = unicode          # noqa (only evaluated on 2.x)
    bytes_type = str
else:
    text_type = str
    bytes_type = bytes


def tob(s):
    """text -> UTF-8 bytes (str on 2.x, bytes on 3.x); bytes pass through."""
    if isinstance(s, text_type):
        return s.encode("utf-8")
    return s


def tos(b, errors="replace"):
    """bytes -> text. Text passes through."""
    if isinstance(b, text_type):
        return b
    return b.decode("utf-8", errors)


def byte_at(data, i):
    x = data[i]
    if isinstance(x, int):
        return x
    return ord(x)


def fs_text(s):
    """A path from sys.argv or os.environ as text."""
    if isinstance(s, text_type):
        return s
    return s.decode(sys.getfilesystemencoding() or "utf-8", "replace")


def unquote(s):
    """Percent-decode a URL path or query value to text (UTF-8)."""
    if _unquote_bytes is not None:
        return tos(_unquote_bytes(tob(s)))
    return _unquote_text(s)


_SAFE = set(u"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_~")


def pct(s):
    """Percent-encode one path segment (space -> %20, '/' encoded too)."""
    out = []
    for ch in s:
        if ch in _SAFE:
            out.append(ch)
        else:
            for b in bytearray_of(ch.encode("utf-8")):
                out.append(u"%%%02X" % b)
    return u"".join(out)


def bytearray_of(b):
    """Iterate a bytes object as ints on both Pythons (no bytearray on 2.5)."""
    return [byte_at(b, i) for i in range(len(b))]


def encode_path(rel):
    """Percent-encode a relative path segment by segment, keeping slashes."""
    return u"/".join([pct(seg) for seg in rel.split(u"/") if seg])


def json_str(s):
    out = [u'"']
    for ch in s:
        if ch == u'"':
            out.append(u'\\"')
        elif ch == u"\\":
            out.append(u"\\\\")
        elif ord(ch) < 0x20:
            out.append(u"\\u%04X" % ord(ch))
        else:
            out.append(ch)
    out.append(u'"')
    return u"".join(out)


def lower_key(s):
    return s.lower()


def nfc(s):
    """File names compare composed: HFS+ hands them back decomposed."""
    try:
        import unicodedata
        return unicodedata.normalize("NFC", s)
    except Exception:
        return s


def json_dump_small(v):
    """Enough of a JSON writer for a catalog entry (2.5 has no json)."""
    if v is None:
        return u"null"
    if v is True:
        return u"true"
    if v is False:
        return u"false"
    if isinstance(v, dict):
        return u"{" + u", ".join([json_str(tos(k)) + u": " + json_dump_small(v[k])
                                  for k in sorted(v.keys())]) + u"}"
    if isinstance(v, (list, tuple)):
        return u"[" + u", ".join([json_dump_small(x) for x in v]) + u"]"
    if isinstance(v, float):
        return tos(repr(v))
    if isinstance(v, (str, type(u""))):
        return json_str(tos(v))
    return tos(str(v))


def last_component(path):
    return path.split(u"/")[-1] if path else u""


def now_hms():
    return time.strftime("%H:%M:%S")


def makedirs(p):
    if not os.path.isdir(p):
        try:
            os.makedirs(p)
        except OSError:
            pass


def file_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return None


def rename_over(src, dst):
    """os.rename that replaces on Windows too."""
    if os.path.exists(dst):
        try:
            os.remove(dst)
        except OSError:
            pass
    os.rename(src, dst)


def run_quiet(args):
    """Run a tool with no stdin and no output. Returns the exit code or -1."""
    devnull = open(os.devnull, "wb")
    try:
        try:
            return subprocess.call(args, stdin=devnull, stdout=devnull, stderr=devnull)
        except (OSError, ValueError):
            return -1
    finally:
        devnull.close()


def which(name, extra=()):
    paths = list(extra) + os.environ.get("PATH", "").split(os.pathsep) + [
        "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin", "/sw/bin", "/usr/bin"]
    for d in paths:
        if not d:
            continue
        for cand in (name, name + ".exe"):
            p = os.path.join(d, cand)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


# ------------------------------------------------------------- disk image

def clean_name(file_name):
    """'Total.Replay.v6.0.1.hdv' -> 'Total Replay v6.0.1' (Image Manager's rule)."""
    base = os.path.splitext(file_name)[0].replace(u"_", u" ")
    out = []
    n = len(base)
    for i in range(n):
        c = base[i]
        if c == u".":
            prev_digit = i > 0 and base[i - 1].isdigit()
            next_digit = i + 1 < n and base[i + 1].isdigit()
            out.append(u"." if (prev_digit and next_digit) else u" ")
        else:
            out.append(c)
    collapsed = u" ".join(u"".join(out).split())
    return collapsed or file_name


def read_le32(d, off):
    if len(d) < off + 4:
        return 0
    return (byte_at(d, off) | (byte_at(d, off + 1) << 8) |
            (byte_at(d, off + 2) << 16) | (byte_at(d, off + 3) << 24))


def boot_probe(head, data_offset):
    """(bootable, volume_name). Block 0 must carry the ProDOS $01 marker and
    the volume directory a PRODOS kernel or a *.SYSTEM loader. Same rule as
    the a2cogs catalog and the Mac app."""
    if len(head) < data_offset + BLOCK:
        return False, None
    img = head[data_offset:]
    if len(img) < BLOCK or byte_at(img, 0) != 0x01:
        return False, None
    vol_name = None
    has_prodos = False
    has_system = False
    blk = 2
    seen = set()
    while blk != 0 and blk not in seen and (blk + 1) * BLOCK <= len(img):
        seen.add(blk)
        b = img[blk * BLOCK:(blk + 1) * BLOCK]
        nxt = byte_at(b, 2) | (byte_at(b, 3) << 8)
        for e in range(13):
            off = 4 + e * 0x27
            if off + 0x27 > len(b):
                break
            storage = byte_at(b, off) >> 4
            name_len = byte_at(b, off) & 0x0F
            if name_len == 0 or name_len > 15:
                continue
            name = tos(b[off + 1:off + 1 + name_len], "ignore").upper()
            if storage == 0xF:
                vol_name = name
                continue
            if storage == 0:
                continue
            ftype = byte_at(b, off + 0x10)
            if name == u"PRODOS":
                has_prodos = True
            if ftype == 0xFF and name.endswith(u".SYSTEM"):
                has_system = True
        blk = nxt
    return (has_prodos or has_system), vol_name


class DiskImage(object):
    def __init__(self, path, folder, file_name, size, data_offset, bootable, vol_name):
        self.path = path
        self.folder = folder
        self.file_name = file_name
        self.display = clean_name(file_name)
        self.bytes = size
        self.blocks = (size - data_offset) // BLOCK
        self.bootable = bootable
        self.volume = vol_name

    def rel_path(self):
        return self.folder + u"/" + self.file_name if self.folder else self.file_name


def probe_disk(path, folder, file_name):
    ext = os.path.splitext(file_name)[1][1:].lower()
    if ext not in DISK_EXT:
        return None
    size = file_size(path)
    if size is None or size < BLOCK:
        return None
    try:
        f = open(path, "rb")
        try:
            head = f.read(32 * BLOCK)
        finally:
            f.close()
    except IOError:
        return None
    data_offset = 0
    if len(head) >= 64 and head[:4] == tob(u"2IMG"):
        off = read_le32(head, 0x18)
        data_offset = off if (off >= 64 and off < size) else 64
    bootable, vol = boot_probe(head, data_offset)
    return DiskImage(path, folder, file_name, size, data_offset, bootable, vol)


# ------------------------------------------------------------- folder walk

class WalkedFile(object):
    def __init__(self, path, folder, name):
        self.path = path
        self.folder = folder
        self.name = name

    def rel_path(self):
        return self.folder + u"/" + self.name if self.folder else self.name

    def ext(self):
        return os.path.splitext(self.name)[1][1:].lower()


def walk_library(root):
    """Regular files under root, sorted by relative path. Hidden names,
    package folders and the files/ folder are skipped; symlinks that
    resolve outside the root are not part of the library."""
    out = []
    root_real = os.path.realpath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = dirpath[len(root):].replace(os.sep, u"/").strip(u"/")
        keep = []
        for d in dirnames:
            if d.startswith(u"."):
                continue
            if d.lower().endswith(PACKAGE_DIR_EXT):
                continue
            if not rel and d == FILES_SUB:
                continue
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                r = os.path.realpath(full)
                if not (r == root_real or r.startswith(root_real + os.sep)):
                    continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if fn.startswith(u"."):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                r = os.path.realpath(full)
                if not r.startswith(root_real + os.sep):
                    continue
            if not os.path.isfile(full):
                continue
            out.append(WalkedFile(full, rel, fn))
    out.sort(key=lambda f: f.rel_path().lower())
    return out


def folders_of(files):
    """Every folder with a file in or below it, plus ancestors. Sorted."""
    s = set()
    for f in files:
        parts = [p for p in f.folder.split(u"/") if p]
        while parts:
            s.add(u"/".join(parts))
            parts.pop()
    return sorted(s, key=lower_key)


def resolve_request_path(root, req_path):
    """'/Games/Arkanoid.2mg' (decoded) -> absolute file inside root, or None
    on traversal, dotfiles, directories, or anything not there."""
    segs = [s for s in req_path.split(u"/") if s]
    if not segs:
        return None
    for s in segs:
        if s == u"." or s == u".." or s.startswith(u".") or u"\\" in s:
            return None
    p = root
    for s in segs:
        p = os.path.join(p, s)
    if not os.path.isfile(p):
        return None
    root_real = os.path.realpath(root)
    if not os.path.realpath(p).startswith(root_real + os.sep):
        return None
    return p


# ---------------------------------------------------------------- hub

def hub_under(folder, cat):
    return folder == cat or folder.startswith(cat + u"/")


def hub_prefixed(name, folder, cat):
    if folder == cat or not folder.startswith(cat + u"/"):
        return name
    return last_component(folder) + u": " + name


def hub_clip(s):
    return s if len(s) <= HUB_LABEL_MAX else s[:HUB_LABEL_MAX]


class HubConfig(object):
    def __init__(self):
        self.cats = []            # list of [path, label-or-None]
        self.prefix_subfolder = False
        self.explicit = False     # user set; otherwise defaults from top folders

    def defaults_from(self, folders):
        top = sorted([f for f in folders if u"/" not in f], key=lower_key)
        self.cats = [[t, None] for t in top]

    def label(self, path):
        for p, l in self.cats:
            if p == path:
                return hub_clip((l or u"").strip() or last_component(p))
        return None

    def contains(self, path):
        for p, l in self.cats:
            if p == path:
                return True
        return False

    def toggle(self, path):
        if self.contains(path):
            self.cats = [c for c in self.cats if c[0] != path]
        else:
            self.cats.append([path, None])
        self.explicit = True

    def json(self, kind, total, count_fn):
        rows = []
        for p, l in self.cats:
            n = count_fn(p)
            if n > 0:
                rows.append((p, self.label(p), n))
            if len(rows) >= HUB_MAX:
                break
        s = [u'{ "hub": "your", "kind": "%s", "total": %d, "cats": [\n' % (kind, total)]
        for i in range(len(rows)):
            p, lab, n = rows[i]
            s.append(u'  { "name": %s, "cat": %s, "n": %d }' % (json_str(lab), json_str(p), n))
            s.append(u",\n" if i < len(rows) - 1 else u"\n")
        s.append(u"] }\n")
        return u"".join(s)


# ------------------------------------------------------------- catalogs

def paged(items, page, per):
    pages = max(1, (len(items) + per - 1) // per)
    lo = (page - 1) * per
    hi = min(lo + per, len(items))
    return pages, (items[lo:hi] if lo < hi else [])


class DiskCatalog(object):
    def __init__(self, images, writable):
        counts = {}
        for i in images:
            k = i.display.lower()
            counts[k] = counts.get(k, 0) + 1
        for i in images:
            if counts[i.display.lower()] > 1 and i.folder:
                i.display = i.display + u" (" + last_component(i.folder) + u")"
        images.sort(key=lambda i: i.display.lower())
        self.images = images
        self.writable = writable

    def pages(self):
        return max(1, (len(self.images) + PER_PAGE - 1) // PER_PAGE)

    def in_category(self, cat, prefix_sub):
        hits = [i for i in self.images if hub_under(i.folder, cat)]
        if not prefix_sub:
            return [(i, i.display) for i in hits]
        rows = [(i, hub_prefixed(i.display, i.folder, cat)) for i in hits]
        rows.sort(key=lambda r: r[1].lower())
        return rows

    def page_json(self, page, base, covers, local=0):
        pages, sl = paged(self.images, page, PER_PAGE)
        head = u'"pages": %d, "total": %d' % (self.pages(), len(self.images))
        if local:
            # the share also carries CoGS Catalog Local R/W: the card reads
            # this off the page it already fetches for the Your source
            # count and adds its own row for it (fw 1.42)
            head += u', "cogslocal": %d' % local
        return self.body([(i, i.display) for i in sl], base, head, covers)

    def search_json(self, q, page, base, covers, cat=None, prefix_sub=False):
        q = q.lower()
        pool = self.in_category(cat, prefix_sub) if cat else [(i, i.display) for i in self.images]
        if q:
            pool = [r for r in pool if q in r[1].lower() or q in r[0].file_name.lower()
                    or q in r[0].folder.lower()]
        pages, sl = paged(pool, page, PER_PAGE)
        return self.body(sl, base, u'"pages": %d, "count": %d' % (pages, len(pool)), covers)

    def body(self, rows, base, head, covers):
        s = [u"{ %s, \"volumes\": [\n" % head]
        for i in range(len(rows)):
            img, name = rows[i]
            url = base + encode_path(img.rel_path())
            e = [u'  { "name": %s,' % json_str(name),
                 u' "bytes": %d,' % img.bytes,
                 u' "blocks": %d,' % img.blocks,
                 u' "boot": %s,' % (img.bootable and u"true" or u"false"),
                 u' "readonly": %s,' % (self.writable and u"false" or u"true"),
                 u' "source": "lan",']
            cover = covers.get(pic_stem(name))
            if cover:
                e.append(u' "cover": %s,' % json_str(cover))
            e.append(u' "url": %s }' % json_str(url))
            s.append(u"".join(e))
            s.append(u",\n" if i < len(rows) - 1 else u"\n")
        s.append(u"] }\n")
        return u"".join(s)


def pic_stem(s):
    t = s.lower()
    i = t.find(u" dk")
    if i >= 0:
        t = t[:i]
    i = t.find(u" (")
    if i >= 0:
        t = t[:i]
    return t.strip()


class Track(object):
    """One audio track (served under /audio/) or one picture (/pictures/)."""
    def __init__(self, served, folder, source, blob, nbytes):
        self.served = served          # relpath with .pcm / .shr
        self.folder = folder
        self.display = clean_name(os.path.basename(source))
        self.source = source
        self.blob = blob              # the file actually served
        self.bytes = nbytes

    def secs(self):
        return (self.bytes + AUDIO_RATE // 2) // AUDIO_RATE


class MediaCatalog(object):
    """Audio and pictures share one shape: the card's parser walks both."""
    def __init__(self, kind, tracks, pending, skipped):
        self.kind = kind              # "audio" or "pictures"
        tracks.sort(key=lambda t: t.display.lower())
        self.tracks = tracks
        self.pending = pending        # display names still converting
        self.skipped = skipped        # sources with no converter available
        self.prefix = u"audio/" if kind == u"audio" else u"pictures/"
        self.file = u"cogs-audio" if kind == u"audio" else u"cogs-pictures"

    def pages(self):
        return max(1, (len(self.tracks) + AUDIO_PER_PAGE - 1) // AUDIO_PER_PAGE)

    def in_category(self, cat, prefix_sub):
        hits = [t for t in self.tracks if hub_under(t.folder, cat)]
        if not prefix_sub:
            return [(t, t.display) for t in hits]
        rows = [(t, hub_prefixed(t.display, t.folder, cat)) for t in hits]
        rows.sort(key=lambda r: r[1].lower())
        return rows

    def page_json(self, page, base):
        pages, sl = paged(self.tracks, page, AUDIO_PER_PAGE)
        nxt = (page < self.pages()) and (u"%s-%d.json" % (self.file, page + 1)) or None
        return self.body([(t, t.display) for t in sl], self.pages(), len(self.tracks), nxt, base)

    def search_json(self, q, page, base, cat=None, prefix_sub=False):
        q = q.lower()
        pool = self.in_category(cat, prefix_sub) if cat else [(t, t.display) for t in self.tracks]
        if q:
            pool = [r for r in pool if q in r[1].lower()]
        pages = max(1, (len(pool) + AUDIO_PER_PAGE - 1) // AUDIO_PER_PAGE)
        p = min(max(1, page), pages)
        _, sl = paged(pool, p, AUDIO_PER_PAGE)
        return self.body(sl, pages, len(pool), None, base)

    def body(self, rows, pages, total, nxt, base):
        s = [u'{ "pages": %d, "total": %d,' % (pages, total)]
        if nxt:
            s.append(u' "next": %s,' % json_str(nxt))
        s.append(u' "catalog": "%s", "tracks": [\n' % self.kind)
        for i in range(len(rows)):
            t, name = rows[i]
            url = base + self.prefix + encode_path(t.served)
            e = [u'  { "name": %s,' % json_str(name), u' "bytes": %d,' % t.bytes]
            if self.kind == u"audio":
                e.append(u' "secs": %d, "rate": %d, "ch": 1,' % (t.secs(), AUDIO_RATE))
            e.append(u' "source": "lan", "url": %s }' % json_str(url))
            s.append(u"".join(e))
            s.append(u",\n" if i < len(rows) - 1 else u"\n")
        s.append(u"] }\n")
        return u"".join(s)

    def find(self, served):
        for t in self.tracks:
            if t.served == served:
                return t
        return None


STATIONS_EMPTY = u'{ "pages": 1, "total": 0, "catalog": "stations", "tracks": [] }\n'


# ------------------------------------------------------------ converters

def cache_root():
    home = fs_text(os.path.expanduser("~"))
    if sys.platform == "darwin":
        return os.path.join(home, u"Library", u"Caches", u"CoGSImageManager")
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return os.path.join(fs_text(xdg), u"cogs-image-manager")
    if sys.platform.startswith("win"):
        la = os.environ.get("LOCALAPPDATA")
        if la:
            return os.path.join(fs_text(la), u"CoGSImageManager", u"cache")
    return os.path.join(home, u".cache", u"cogs-image-manager")


def cache_key(path):
    """sha256('realpath|mtime')[:32], the Mac app's scheme, so a PCM the app
    already made is reused here and the other way round."""
    real = os.path.realpath(path)
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        mtime = 0
    key = u"%s|%d" % (real, mtime)
    return hashlib.sha256(tob(key)).hexdigest()[:32]


class Converters(object):
    """Finds what this machine can convert with. ffmpeg first, afconvert
    (every OS X since 10.4) for audio when ffmpeg is missing; img2shr.py
    with python3 + numpy + Pillow for pictures."""
    def __init__(self, script_dir, audio_tool=u"auto"):
        self.ffmpeg = which("ffmpeg")
        self.afconvert = None
        if sys.platform == "darwin" and os.path.isfile("/usr/bin/afconvert"):
            self.afconvert = "/usr/bin/afconvert"
        if audio_tool == u"afconvert":
            self.ffmpeg = None
        elif audio_tool == u"ffmpeg":
            self.afconvert = None
        elif audio_tool == u"none":
            self.ffmpeg = self.afconvert = None
        self.python3 = None
        self.img2shr = None
        for cand in (os.path.join(script_dir, u"img2shr.py"),
                     os.path.join(script_dir, u"..", u"imagemanager-mac", u"tools", u"img2shr.py")):
            if os.path.isfile(cand):
                self.img2shr = os.path.realpath(cand)
                break
        if self.img2shr:
            p3 = which("python3")
            if p3 and run_quiet([p3, "-c", "import numpy, PIL"]) == 0:
                self.python3 = p3
        self.failed = set()

    def audio_ok(self):
        return bool(self.ffmpeg or self.afconvert)

    def picture_ok(self):
        return bool(self.python3 and self.img2shr)

    def audio_how(self):
        if self.ffmpeg:
            return u"ffmpeg"
        if self.afconvert:
            return u"afconvert"
        return u"none (install ffmpeg; .pcm files still share)"

    def picture_how(self):
        if self.picture_ok():
            return u"img2shr.py"
        return u"none (.shr files still share)"

    def pcm_path(self, src):
        return os.path.join(cache_root(), u"pcm", cache_key(src) + u".pcm")

    def shr_path(self, src):
        return os.path.join(cache_root(), u"shr-text", cache_key(src) + u".shr")

    def transcode_audio(self, src):
        out = self.pcm_path(src)
        if os.path.exists(out):
            return True
        makedirs(os.path.dirname(out))
        tmp = out + u".part"
        ok = False
        if self.ffmpeg:
            rc = run_quiet([self.ffmpeg, "-nostdin", "-v", "error", "-y", "-i", src,
                            "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "u8", tmp])
            ok = (rc == 0 and (file_size(tmp) or 0) > 0)
        elif self.afconvert:
            wav = out + u".wav"
            rc = run_quiet([self.afconvert, "-f", "WAVE", "-d", "UI8@%d" % AUDIO_RATE,
                            "-c", "1", src, wav])
            if rc == 0 and os.path.exists(wav):
                ok = wav_data_to_file(wav, tmp)
            try:
                os.remove(wav)
            except OSError:
                pass
        if ok:
            # The Ensoniq DOC treats a $00 sample as an oscillator halt, so
            # $00 is forbidden on the wire: floor them to $01.
            sanitize_zero_bytes(tmp)
            try:
                rename_over(tmp, out)
                return True
            except OSError:
                pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    def convert_picture(self, src):
        out = self.shr_path(src)
        if os.path.exists(out):
            return True
        makedirs(os.path.dirname(out))
        tmp = out + u".part"
        rc = run_quiet([self.python3, self.img2shr, src, tmp])
        if rc == 0 and file_size(tmp) == SHR_BYTES:
            try:
                rename_over(tmp, out)
                return True
            except OSError:
                pass
        for junk in (tmp, tmp + u"_preview.png"):
            try:
                os.remove(junk)
            except OSError:
                pass
        return False


def wav_data_to_file(wav, out):
    """Copy the 'data' chunk of a RIFF WAVE file to out. afconvert writes a
    4 KB header, so walk the chunks instead of assuming 44 bytes."""
    try:
        f = open(wav, "rb")
    except IOError:
        return False
    try:
        head = f.read(12)
        if len(head) < 12 or head[:4] != tob(u"RIFF") or head[8:12] != tob(u"WAVE"):
            return False
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                return False
            cid = ch[:4]
            clen = read_le32(ch, 4)
            if cid == tob(u"data"):
                o = open(out, "wb")
                try:
                    left = clen
                    while left > 0:
                        chunk = f.read(min(left, 1 << 20))
                        if not chunk:
                            break
                        o.write(chunk)
                        left -= len(chunk)
                finally:
                    o.close()
                return (file_size(out) or 0) > 0
            f.seek(clen + (clen & 1), 1)
    finally:
        f.close()


def sanitize_zero_bytes(path):
    try:
        f = open(path, "rb")
        try:
            data = f.read()
        finally:
            f.close()
        if tob(u"\x00") in data:
            data = data.replace(tob(u"\x00"), tob(u"\x01"))
            f = open(path, "wb")
            try:
                f.write(data)
            finally:
                f.close()
    except IOError:
        pass


# ------------------------------------------------------------ files folder

def prodos_alias(name, taken):
    """'Letter to Mom.odt' -> LETTERTOMOM.ODT (15 chars, 3-char extension)."""
    stem, ext = os.path.splitext(name)
    ext = u"".join([c for c in ext[1:].upper() if c.isalnum() and ord(c) < 128])[:3]
    stem = u"".join([c for c in stem.upper() if (c.isalnum() or c == u".") and ord(c) < 128])
    stem = stem.strip(u".")
    if not stem or not stem[0].isalpha():
        stem = u"F" + stem
    room = 15 - (ext and (len(ext) + 1) or 0)
    stem = stem[:room]
    suffix = ext and (u"." + ext) or u""
    alias = stem + suffix
    n = 1
    while alias in taken:
        tail = text_type(n)
        alias = stem[:room - len(tail)] + tail + suffix
        n += 1
    return alias


class SharedFile(object):
    def __init__(self, alias, name, path, nbytes, mtime):
        self.alias = alias
        self.name = name
        self.path = path
        self.bytes = nbytes
        self.mtime = mtime


def files_dir(root):
    return os.path.join(root, FILES_SUB)


def files_list(root):
    d = files_dir(root)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    taken = set()
    for fn in sorted(names, key=lower_key):
        if fn.startswith(u".") or fn.endswith(u".part"):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        alias = prodos_alias(fn, taken)
        taken.add(alias)
        out.append(SharedFile(alias, fn, p, st.st_size, int(st.st_mtime)))
    return out


def files_find(root, name):
    allf = files_list(root)
    for f in allf:
        if f.name == name:
            return f
    up = name.upper()
    for f in allf:
        if f.alias == up or f.name.upper() == up:
            return f
    return None


def files_safe_name(raw):
    last = raw.replace(u"\\", u"/").split(u"/")
    last = [s for s in last if s]
    fn = (last and last[-1] or u"").strip()
    if not fn or fn.startswith(u".") or len(fn) > FILES_MAX_NAME:
        return None
    for ch in fn:
        if ord(ch) < 0x20 or ch == u":":
            return None
    return fn


def files_listing_txt(root):
    return u"".join([u"%s\t%s\t%d\n" % (f.alias, f.name, f.bytes) for f in files_list(root)])


def files_listing_json(root, base, writable):
    fl = files_list(root)
    s = [u'{\n  "writable": %s,\n  "files": [\n' % (writable and u"true" or u"false")]
    for i in range(len(fl)):
        f = fl[i]
        s.append(u'    {"name": %s, "a2": %s, "bytes": %d, "mtime": %d, "url": %s}' % (
            json_str(f.name), json_str(f.alias), f.bytes, f.mtime,
            json_str(base + u"files/" + pct(f.name))))
        s.append(u",\n" if i + 1 < len(fl) else u"\n")
    s.append(u"  ]\n}\n")
    return u"".join(s)


def files_put(root, raw_name, body, content_range):
    """Returns (code, name). 201 created, 200 replaced, 200/201 piece, or an
    error code. Pieces with Content-Range arrive in order into a .part that
    is renamed into place when complete."""
    safe = files_safe_name(raw_name)
    if safe is None:
        return 400, raw_name
    existing = files_find(root, safe)
    fn = existing and existing.name or safe
    d = files_dir(root)
    makedirs(d)
    final = os.path.join(d, fn)
    part = final + u".part"
    start = 0
    total = len(body)
    if content_range and content_range.startswith(u"bytes "):
        spec = content_range[6:].split(u"/", 1)
        a = spec[0].split(u"-", 1)[0]
        if not a.isdigit():
            return 400, fn
        start = int(a)
        if len(spec) == 2 and spec[1] != u"*":
            if not spec[1].strip().isdigit():
                return 400, fn
            total = int(spec[1])
        else:
            total = -1
    if total >= 0 and start + len(body) > total:
        return 416, fn
    if total > FILES_MAX_BYTES or len(body) > FILES_MAX_BYTES:
        return 413, fn
    have_part = os.path.exists(part)
    if start > 0 and not have_part:
        return 409, fn
    try:
        if start == 0:
            f = open(part, "wb")
            try:
                f.write(body)
            finally:
                f.close()
        else:
            f = open(part, "r+b")
            try:
                f.seek(start)
                f.write(body)
            finally:
                f.close()
    except IOError:
        return 500, fn
    done = total < 0 or start + len(body) >= total
    if done:
        try:
            rename_over(part, final)
        except OSError:
            return 500, fn
        return (existing and 200 or 201), fn
    return (existing and 200 or 201), fn


# ------------------------------------------------- CoGS Catalog Local R/W
#
# A full copy of the a2cogs.com disk catalog kept on this machine and
# served to the card on its own path (/cogs-local/), apart from the
# library. The card shows it as its own row on Browse Disks, with the
# same hub, categories and Find the a2cogs rows have.
#
# The folder (shared with the Mac app, same files):
#   catalog.json   catalog-all.json as a2cogs served it at the last sync
#   images.tsv     one line per image: file, catalog URL, bytes, revision
#                  tag, state (ok / yours), mtime at fetch, newer flag
#   disks/         the images, named by the last segment of their URL
#
# Every image is writable. The first write marks it "yours" and sync
# leaves it alone from then on; when the catalog has a newer copy of a
# yours image it is flagged, and Reset fetches it.

LOCAL_NAME = u"CoGS Catalog Local R/W"
LOCAL_DIR_NAME = u"CoGS Catalog Local"
LOCAL_PREFIX = u"/cogs-local"
LOCAL_DISKS = u"cogs-local/disks/"
LOCAL_CATALOG_URL = u"https://a2cogs.com/disks/catalog-all.json"
LOCAL_PER_PAGE = 18          # what a2cogs serves
LOCAL_WORKERS = 4
LOCAL_SYNC_EVERY = 24 * 3600   # while running: a sync once a day
LOCAL_SYNC_AT_OPEN = 3600      # at start: a sync when the last one is over an hour old
LOCAL_LOCK_STALE = 15 * 60     # a .working nobody touched for this long is dead
LOCAL_PRUNE_MIN = 8            # a sync removes this many dropped images without a second look
LOCAL_HEAD_HOSTS = (u"disks.a2cogs.com", u"a2cogs.com")   # hosts asked for a revision tag
LOCAL_CATS = ((u"F", u"Featured"), (u"G", u"Play a game"), (u"C", u"Music & art"),
              (u"E", u"Educate & learn"), (u"T", u"Go online"),
              (u"S", u"Utilities & work"), (u"D", u"Demos & odds"))

try:
    import json as _json
except ImportError:
    _json = None

try:
    import urllib2 as _urlreq
except ImportError:
    try:
        import urllib.request as _urlreq
    except ImportError:
        _urlreq = None

if PY2:
    _uchr = unichr               # noqa (only evaluated on 2.x)
else:
    _uchr = chr

_JS_WS = re.compile(u"[ \t\r\n]*")
_JS_NUM = re.compile(u"-?[0-9]+(\\.[0-9]+)?([eE][-+]?[0-9]+)?")
_JS_STR = re.compile(u'"((?:[^"\\\\]|\\\\.)*)"')
_JS_ESC = re.compile(u"\\\\(u[0-9a-fA-F]{4}|.)")
_JS_ESC_MAP = {u"n": u"\n", u"t": u"\t", u"r": u"\r", u"b": u"\b", u"f": u"\f"}


def _js_unesc(m):
    c = m.group(1)
    if c[0] == u"u" and len(c) == 5:
        return _uchr(int(c[1:], 16))
    return _JS_ESC_MAP.get(c, c)


def _js_value(s, i):
    i = _JS_WS.match(s, i).end()
    c = s[i]
    if c == u"{":
        d = {}
        i = _JS_WS.match(s, i + 1).end()
        if s[i] == u"}":
            return d, i + 1
        while True:
            k, i = _js_value(s, i)
            i = _JS_WS.match(s, i).end()
            if s[i] != u":":
                raise ValueError("json: expected ':'")
            v, i = _js_value(s, i + 1)
            d[k] = v
            i = _JS_WS.match(s, i).end()
            if s[i] == u",":
                i += 1
                continue
            if s[i] != u"}":
                raise ValueError("json: expected '}'")
            return d, i + 1
    if c == u"[":
        a = []
        i = _JS_WS.match(s, i + 1).end()
        if s[i] == u"]":
            return a, i + 1
        while True:
            v, i = _js_value(s, i)
            a.append(v)
            i = _JS_WS.match(s, i).end()
            if s[i] == u",":
                i += 1
                continue
            if s[i] != u"]":
                raise ValueError("json: expected ']'")
            return a, i + 1
    if c == u'"':
        m = _JS_STR.match(s, i)
        if not m:
            raise ValueError("json: bad string")
        raw = m.group(1)
        if u"\\" in raw:
            raw = _JS_ESC.sub(_js_unesc, raw)
        return raw, m.end()
    if s.startswith(u"true", i):
        return True, i + 4
    if s.startswith(u"false", i):
        return False, i + 5
    if s.startswith(u"null", i):
        return None, i + 4
    m = _JS_NUM.match(s, i)
    if not m:
        raise ValueError("json: bad value")
    t = m.group(0)
    if u"." in t or u"e" in t or u"E" in t:
        return float(t), m.end()
    return int(t), m.end()


def json_loads(text, force_small=False):
    """json.loads where there is a json module (2.6+), the small reader
    above on 2.5. Both return text strings."""
    text = tos(text)
    if _json is not None and not force_small:
        return _json.loads(text)
    try:
        v, i = _js_value(text, 0)
    except IndexError:
        raise ValueError("json: truncated")
    return v


def http_open(url, head=False, timeout=60):
    """urlopen on either Python. Raises IOError-family on failure."""
    if _urlreq is None:
        raise IOError("this Python has no urllib")
    req = _urlreq.Request(str(url), headers={"User-Agent": "CoGS-IM-text/" + VERSION})
    if head:
        req.get_method = lambda: "HEAD"
    try:
        return _urlreq.urlopen(req, timeout=timeout)
    except TypeError:
        return _urlreq.urlopen(req)          # 2.5: no timeout argument


def url_host_of(url):
    rest = url.split(u"://", 1)[-1]
    return rest.split(u"/", 1)[0].lower()


def url_file_of(url):
    """The local file name for a catalog URL: its last path segment, decoded."""
    tail = url.split(u"?", 1)[0].split(u"#", 1)[0].rstrip(u"/").split(u"/")[-1]
    name = unquote(tail)
    name = name.replace(u"/", u"_").replace(u"\\", u"_").replace(u":", u"_")
    if not name or name.startswith(u"."):
        name = u"_" + name
    return name


class LocalImage(object):
    def __init__(self, fname, url):
        self.file = fname
        self.url = url
        self.bytes = 0
        self.tag = u""
        self.state = u"ok"        # ok: as fetched; yours: written to since
        self.mtime = 0            # the file's mtime when the fetch finished
        self.newer = False        # yours, and the catalog has a newer copy


class LocalTitle(object):
    def __init__(self, d):
        self.name = d.get(u"name") or u""
        self.lname = self.name.lower()
        self.bytes = int(d.get(u"bytes") or 0)
        self.blocks = int(d.get(u"blocks") or 0)
        self.boot = d.get(u"boot") is True
        self.cat = d.get(u"cat") or u""
        self.host = d.get(u"host") or u""
        self.gsalso = d.get(u"gsalso") is True
        f = d.get(u"featured")
        if f is True:
            self.featured = 0          # rank: hand-picked order, lowest first
        elif f:
            self.featured = int(f)
        else:
            self.featured = None
        self.parent = d.get(u"in") or u""
        self.cover = d.get(u"cover") or u""
        self.url = d.get(u"url") or u""
        self.file = url_file_of(self.url)
        self.parts = [(p, url_file_of(p)) for p in (d.get(u"parts") or [])]
        self.raw = d              # the catalog entry as it came (kept.json)
        self.retired = False      # a2cogs dropped it; listed because its image is yours
        self.added = False        # not from a2cogs: an image the user put in the folder

    def gs_only(self):
        return (self.host or u"gs") == u"gs"


class LocalCatalog(object):
    """The folder, the record, the lists the card asks for, and the
    download / sync / reset job. One lock of its own."""

    def __init__(self, folder, log, catalog_url=None):
        self.dir = folder
        self.disks_dir = os.path.join(folder, u"disks")
        self.log = log
        self.catalog_url = catalog_url or LOCAL_CATALOG_URL
        self.lock = threading.RLock()
        self.titles = []
        self.images = {}          # file -> LocalImage
        self.have = set()         # files present on disk
        self.added = {}           # file -> LocalTitle: images the user put in disks/
        self._added_seen = {}     # file -> ((mtime, size), LocalTitle), so a scan reads each once
        self.enabled = False      # a catalog.json is in the folder
        self.synced = 0           # unix time of the last finished sync
        self.job = None           # dict while a download / sync / reset runs
        self.cancel = False
        self._listed = None
        self.stamp = None         # the record and the catalog as we last read or wrote them
        self.auto_tried = 0       # unix time of the last automatic sync
        self.load()

    # -- paths
    def catalog_path(self):
        return os.path.join(self.dir, u"catalog.json")

    def record_path(self):
        return os.path.join(self.dir, u"images.tsv")

    def image_path(self, fname):
        return os.path.join(self.disks_dir, fname)

    def lock_path(self):
        return os.path.join(self.dir, u".working")

    def kept_path(self):
        return os.path.join(self.dir, u"kept.json")

    # -- titles a2cogs dropped. An untouched image goes with the title at the
    # next sync. A yours image stays, and its title stays listed from
    # kept.json (same form as catalog.json) until Reset or a delete by hand.
    def merge_kept(self, titles, kept):
        out = list(titles)
        for k in kept:
            i = 0
            while i < len(out) and out[i].lname <= k.lname:
                i += 1
            out.insert(i, k)
        return out

    # -- images the user put in disks/ themselves. Anything with a disk
    # image extension that the catalog does not name and the record never
    # had is listed with the rest, marked "added". Sync, Reset and Remove
    # never touch one.
    def scan_added(self):
        if not self.enabled:
            return {}
        try:
            names = os.listdir(self.disks_dir)
        except OSError:
            return {}
        known = set([nfc(f) for f in self.images.keys()])
        for f in self.wanted(self.titles).keys():
            known.add(nfc(f))
        out, seen = {}, {}
        for fn in names:
            fn = tos(fn)
            if fn.startswith(u".") or nfc(fn) in known:
                continue
            if os.path.splitext(fn)[1][1:].lower() not in DISK_EXT:
                continue
            p = self.image_path(fn)
            if not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            key = (int(st.st_mtime), st.st_size)
            old = self._added_seen.get(fn)
            if old is not None and old[0] == key:
                t = old[1]
            else:
                d = probe_disk(p, u"", fn)
                if d is None:
                    continue
                t = LocalTitle({u"name": d.display, u"bytes": d.bytes, u"blocks": d.blocks,
                                u"boot": d.bootable is True, u"host": u"any", u"gsalso": True,
                                u"url": u"added"})
                t.file = fn
                t.added = True
            seen[fn] = (key, t)
            out[fn] = t
        self._added_seen = seen
        return out

    def added_titles(self):
        """The added images as titles, A-Z."""
        return sorted(self.added.values(), key=lambda t: (t.lname, t.file))

    def save_kept(self, kept):
        p = self.kept_path()
        if not kept:
            try:
                os.remove(p)
            except OSError:
                pass
            return
        try:
            f = open(p + u".tmp", "wb")
            try:
                f.write(tob(json_dump_small({u"volumes": [t.raw for t in kept]}) + u"\n"))
            finally:
                f.close()
            rename_over(p + u".tmp", p)
        except (IOError, OSError):
            pass

    # -- two Image Managers on one folder (the Mac app and this one, or two
    # of these): the record on disk is the shared truth. Each re-reads it
    # when the other one wrote it, and only one works on the folder at a time.
    def disk_stamp(self):
        out = []
        for p in (self.record_path(), self.catalog_path()):
            try:
                st = os.stat(p)
                out.append((int(st.st_mtime * 1000), st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def reload_if_changed(self):
        """Another Image Manager synced, reset, removed or marked an image
        yours: take its record. Not while a job of ours runs."""
        if self.job is not None or self.disk_stamp() == self.stamp:
            return False
        self.load()
        return True

    def take_folder(self):
        """Create .working (pid, host). False when a live manager holds it."""
        makedirs(self.dir)
        me = u"%d\n%s\n" % (os.getpid(), tos(socket.gethostname()))
        for attempt in (1, 2):
            try:
                fd = os.open(self.lock_path(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 420)       # 0644
            except OSError:
                if not self.folder_lock_stale():
                    return False
                try:
                    os.remove(self.lock_path())
                except OSError:
                    pass
                continue
            try:
                os.write(fd, tob(me))
            finally:
                os.close(fd)
            return True
        return False

    def folder_lock_stale(self):
        p = self.lock_path()
        try:
            st = os.stat(p)
            f = open(p, "rb")
            try:
                lines = tos(f.read()).split(u"\n")
            finally:
                f.close()
        except (IOError, OSError):
            return True
        if time.time() - st.st_mtime > LOCAL_LOCK_STALE:
            return True
        try:
            pid = int(lines[0])
        except (ValueError, IndexError):
            return False
        if len(lines) < 2 or lines[1] != tos(socket.gethostname()):
            return False                  # another machine on a shared folder: age decides
        if pid == os.getpid():
            return True
        if os.name == "nt":
            return False                  # os.kill(pid, 0) ends the process there
        try:
            os.kill(pid, 0)
        except OSError:
            e = sys.exc_info()[1]
            return getattr(e, "errno", 0) == 3      # ESRCH: that process is gone
        return False

    def touch_folder(self):
        try:
            os.utime(self.lock_path(), None)
        except OSError:
            pass

    def drop_folder(self):
        try:
            os.remove(self.lock_path())
        except OSError:
            pass

    # -- the record
    def load(self):
        titles = []
        try:
            f = open(self.catalog_path(), "rb")
            try:
                raw = f.read()
            finally:
                f.close()
            titles = self.parse_catalog(raw)
        except (IOError, OSError, ValueError, KeyError, TypeError):
            titles = []
        if titles:
            try:
                f = open(self.kept_path(), "rb")
                try:
                    kept = self.parse_catalog(f.read())
                finally:
                    f.close()
            except (IOError, OSError, ValueError, KeyError, TypeError):
                kept = []
            named = set(self.wanted(titles).keys())
            kept = [t for t in kept if t.file not in named]
            for t in kept:
                t.retired = True
            titles = self.merge_kept(titles, kept)
        images = {}
        synced = 0
        try:
            f = open(self.record_path(), "rb")
            try:
                lines = f.read().splitlines()
            finally:
                f.close()
        except (IOError, OSError):
            lines = []
        for raw in lines:
            line = tos(raw)
            if line.startswith(u"#synced "):
                try:
                    synced = int(line[8:].strip())
                except ValueError:
                    pass
                continue
            if not line or line.startswith(u"#"):
                continue
            c = line.split(u"\t")
            if len(c) < 7:
                continue
            im = LocalImage(c[0], c[1])
            try:
                im.bytes = int(c[2])
                im.mtime = int(c[5])
            except ValueError:
                pass
            im.tag = c[3]
            im.state = c[4] == u"yours" and u"yours" or u"ok"
            im.newer = c[6] == u"1"
            images[im.file] = im
        self.lock.acquire()
        try:
            self.titles = titles
            self.images = images
            self.synced = synced
            self.enabled = bool(titles)
            self._listed = None
            self.stamp = self.disk_stamp()
        finally:
            self.lock.release()
        self.mark_written()

    def mark_written(self):
        """Refresh which images are on disk, and mark the ones written to
        behind our back (Finder, an emulator, a copy dropped over one):
        the mtime moved since the fetch, so the image is yours."""
        have = set()
        changed = False
        for fname, im in list(self.images.items()):
            try:
                st = os.stat(self.image_path(fname))
            except OSError:
                continue
            have.add(fname)
            if im.state == u"ok" and im.mtime and int(st.st_mtime) != im.mtime:
                im.state = u"yours"
                changed = True
        added = self.scan_added()
        self.lock.acquire()
        try:
            self.have = have
            self.added = added
            self._listed = None
        finally:
            self.lock.release()
        if changed:
            self.save_record()

    def parse_catalog(self, raw):
        doc = json_loads(raw)
        return [LocalTitle(v) for v in doc[u"volumes"] if v.get(u"url") and v.get(u"name")]

    def save_record(self):
        self.lock.acquire()
        try:
            lines = [u"#cogs-local-images 1\n", u"#synced %d\n" % self.synced,
                     u"#file\turl\tbytes\ttag\tstate\tmtime\tnewer\n"]
            for fname in sorted(self.images.keys(), key=lower_key):
                im = self.images[fname]
                lines.append(u"%s\t%s\t%d\t%s\t%s\t%d\t%d\n" % (
                    im.file, im.url, im.bytes, im.tag.replace(u"\t", u" "), im.state,
                    im.mtime, im.newer and 1 or 0))
            makedirs(self.dir)
            tmp = self.record_path() + u".tmp"
            try:
                f = open(tmp, "wb")
                try:
                    f.write(tob(u"".join(lines)))
                finally:
                    f.close()
                rename_over(tmp, self.record_path())
            except (IOError, OSError):
                pass
            self.stamp = self.disk_stamp()
        finally:
            self.lock.release()

    # -- what the card and the screen list
    def listed(self):
        """Titles whose image is on disk, in catalog order (A-Z)."""
        self.lock.acquire()
        try:
            if self._listed is None:
                self._listed = self.merge_kept([t for t in self.titles if t.file in self.have],
                                               self.added_titles())
            return self._listed
        finally:
            self.lock.release()

    def count(self):
        return len(self.listed())

    def wanted(self, titles=None):
        """file -> catalog URL for every image the catalog names (parts too)."""
        w = {}
        if titles is None:
            titles = [t for t in self.titles if not t.retired]
        for t in titles:
            w[t.file] = t.url
            for url, fname in t.parts:
                w[fname] = url
        return w

    def counts(self):
        """(titles listed, images on disk, bytes on disk, yours, newer)."""
        self.lock.acquire()
        try:
            ims = [self.images[f] for f in self.have if f in self.images]
            return (len(self.listed()), len(ims), sum([im.bytes for im in ims]),
                    len([im for im in ims if im.state == u"yours"]),
                    len([im for im in ims if im.newer]))
        finally:
            self.lock.release()

    def status_of(self, fname):
        if fname in self.added:
            return u"added"
        im = self.images.get(fname)
        if im is None or fname not in self.have:
            return u""
        if im.newer:
            return u"newer"
        return im.state == u"yours" and u"yours" or u""

    def filter(self, q=u"", cat=u"", host=u""):
        q = q.strip().lower()
        cat = cat.strip().upper()[:1]
        out = []
        for t in self.listed():
            if q and q not in t.lname:
                continue
            if cat == u"F":
                if t.featured is None:
                    continue
            elif cat and t.cat != cat:
                continue
            if host == u"8" and t.gs_only():
                continue
            if host == u"gs" and not t.gs_only() and not t.gsalso:
                continue
            out.append(t)
        if cat == u"F":
            out.sort(key=lambda t: t.featured)
        return out

    def hub_json(self):
        rows = self.listed()
        total = len(rows)
        total8 = len([t for t in rows if not t.gs_only()])
        totalgs = len([t for t in rows if t.gs_only() or t.gsalso])
        s = [u'{\n  "hub": "CoGS Catalog Local R/W",\n  "total": %d,\n  "total8": %d,\n'
             u'  "totalgs": %d,\n  "cats": [\n' % (total, total8, totalgs)]
        cats = []
        for k, name in LOCAL_CATS:
            if k == u"F":
                hit = [t for t in rows if t.featured is not None]
            else:
                hit = [t for t in rows if t.cat == k]
            if not hit:
                continue
            cats.append(u'    { "k": "%s", "name": %s, "n": %d, "n8": %d }' % (
                k, json_str(name), len(hit), len([t for t in hit if not t.gs_only()])))
        s.append(u",\n".join(cats))
        s.append(u"\n  ]\n}\n")
        return u"".join(s)

    def row_json(self, t, base, writable, with_bytes):
        e = [u'  { "name": %s,' % json_str(t.name)]
        if with_bytes:
            e.append(u' "bytes": %d,' % t.bytes)
        e.append(u' "blocks": %d,' % t.blocks)
        e.append(u' "readonly": %s,' % (writable and u"false" or u"true"))
        e.append(u' "boot": %s,' % (t.boot and u"true" or u"false"))
        e.append(u' "source": "local",')
        if t.cat:
            e.append(u' "cat": %s,' % json_str(t.cat))
        if t.host:
            e.append(u' "host": %s,' % json_str(t.host))
        if t.parent:
            e.append(u' "in": %s,' % json_str(t.parent))
        if t.cover:
            e.append(u' "cover": %s,' % json_str(t.cover))
        e.append(u' "url": %s' % json_str(base + LOCAL_DISKS + pct(t.file)))
        parts = [json_str(base + LOCAL_DISKS + pct(fname)) for url, fname in t.parts
                 if fname in self.have]
        if parts:
            e.append(u', "parts": [%s]' % u", ".join(parts))
        e.append(u" }")
        return u"".join(e)

    def body(self, rows, head, base, writable, with_bytes):
        s = [u"{ %s, \"volumes\": [\n" % head]
        s.append(u",\n".join([self.row_json(t, base, writable, with_bytes) for t in rows]))
        s.append(u"\n] }\n")
        return u"".join(s)

    def page_json(self, page, base, writable):
        rows = self.listed()
        pages, sl = paged(rows, page, LOCAL_PER_PAGE)
        head = u'"pages": %d, "total": %d' % (pages, len(rows))
        if page < pages:
            head += u', "next": "cogs-volumes-%d.json"' % (page + 1)
        return self.body(sl, head, base, writable, True)

    def search_json(self, q, page, base, writable, cat=u"", host=u""):
        rows = self.filter(q, cat, host)
        pages, sl = paged(rows, min(page, max(1, (len(rows) + LOCAL_PER_PAGE - 1) // LOCAL_PER_PAGE)),
                          LOCAL_PER_PAGE)
        return self.body(sl, u'"pages": %d, "count": %d' % (pages, len(rows)), base, writable, False)

    def find_file(self, fname):
        """An image by the name in its URL, or None."""
        if not fname or u"/" in fname or u"\\" in fname or fname.startswith(u"."):
            return None
        if fname not in self.have and fname not in self.added:
            return None
        p = self.image_path(fname)
        return os.path.isfile(p) and p or None

    def note_write(self, fname):
        """The card (or anything) wrote to this image: it is yours now."""
        self.lock.acquire()
        try:
            im = self.images.get(fname)
            if im is None or im.state == u"yours":
                return False
            im.state = u"yours"
        finally:
            self.lock.release()
        self.save_record()
        self.log(u"%s is yours now (written to); sync will leave it alone" % fname)
        return True

    # -- remove
    def remove(self):
        """Delete the copy from this computer: every image the record or
        the catalog names (the yours ones too), leftover .part files, the
        record and the catalog. Anything else in the folder is left where
        it is, and the two folders go only when they end up empty. Returns
        how many image files were deleted, or None while a job runs."""
        self.lock.acquire()
        try:
            if self.job is not None:
                return None
            if not self.take_folder():
                self.log(u"%s: another Image Manager is working in this folder" % LOCAL_NAME)
                return None
            # held as a job so a download cannot start under the delete
            self.job = {u"what": u"remove", u"total": 0, u"done": 0, u"failed": 0,
                        u"bytes": 0, u"bytes_total": 0}
            names = set(self.images.keys())
            for t in self.titles:
                names.add(t.file)
                for url, fname in t.parts:
                    names.add(fname)
            stay = len(self.added)
            self.titles = []
            self.images = {}
            self.have = set()
            self.added = {}
            self.synced = 0
            self.enabled = False
            self._listed = None
        finally:
            self.lock.release()
        n = 0
        try:
            for fname in names:
                if not fname or fname in (u".", u"..") or u"/" in fname or u"\\" in fname:
                    continue
                p = self.image_path(fname)
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                        n += 1
                    except OSError:
                        pass
                try:
                    os.remove(p + u".part")
                except OSError:
                    pass
            try:
                left = os.listdir(self.disks_dir)
            except OSError:
                left = []
            for fn in left:
                if fn.endswith(u".part") or fn == u".DS_Store":
                    try:
                        os.remove(os.path.join(self.disks_dir, fn))
                    except OSError:
                        pass
            for p in (self.record_path(), self.record_path() + u".tmp",
                      self.catalog_path(), self.catalog_path() + u".tmp",
                      self.kept_path(), self.kept_path() + u".tmp"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            self.drop_folder()
            for d in (self.disks_dir, self.dir):
                try:
                    if os.listdir(d) == [u".DS_Store"]:
                        os.remove(os.path.join(d, u".DS_Store"))
                    os.rmdir(d)               # only when empty
                except OSError:
                    pass
        finally:
            self.drop_folder()
            self.stamp = self.disk_stamp()
            self.job = None
        self.log(u"%s removed: %d image file(s) deleted from %s" % (LOCAL_NAME, n, self.dir))
        if stay:
            self.log(u"%d image%s you added %s still in %s" % (
                stay, stay != 1 and u"s" or u"", stay != 1 and u"are" or u"is", self.disks_dir))
        return n

    def remove_warning(self, here=u"this computer"):
        """The text both confirms show."""
        t, i, b, y, n = self.counts()
        mine = y and (u"your %d Yours (Local) cop%s" % (y, y == 1 and u"y" or u"ies")) \
            or u"any Yours (Local) copies"
        return (u"This deletes the entire local catalog from %s, all %d image files "
                u"(%d titles, %s). That includes %s, and changes made to them are lost. "
                u"To get the catalog back you have to download all of it again.%s" % (
                    here, i, t - len(self.added), fmt_mb(b), mine, self.added_note()))

    def added_note(self):
        a = len(self.added)
        if not a:
            return u""
        return u" The %d image%s you added to the folder stay%s where %s." % (
            a, a != 1 and u"s" or u"", a == 1 and u"s" or u"", a == 1 and u"it is" or u"they are")

    # -- download / sync / reset
    def busy(self):
        return self.job is not None

    def job_line(self):
        j = self.job
        if not j:
            return u""
        if j[u"total"] == 0:
            return u"%s: reading the catalog..." % j[u"what"]
        s = u"%s: %d of %d images" % (j[u"what"], j[u"done"], j[u"total"])
        if j[u"bytes_total"]:
            s += u", %s of %s" % (fmt_mb(j[u"bytes"]), fmt_mb(j[u"bytes_total"]))
        elif j[u"bytes"]:
            s += u", %s" % fmt_mb(j[u"bytes"])
        if j[u"failed"]:
            s += u", %d failed" % j[u"failed"]
        return s

    def start(self, what, reset=None):
        """what: download / sync / reset. reset: file names to fetch again
        whatever their state, or 'yours' for every yours image. Returns
        False when a job is already running."""
        self.lock.acquire()
        try:
            if self.job is not None:
                return False
            if not self.take_folder():
                self.log(u"%s: another Image Manager is working in this folder, %s not started" % (
                    LOCAL_NAME, what))
                return False
            self.cancel = False
            self.job = {u"what": what, u"total": 0, u"done": 0, u"failed": 0,
                        u"bytes": 0, u"bytes_total": 0}
        finally:
            self.lock.release()
        daemon_thread(self.run_job, (what, reset)).start()
        return True

    def auto_sync(self, at_open=False, now=None):
        """Sync without being asked, once a copy is here: at start when the
        last sync is over an hour old, and once a day while running. A
        failed try (offline) waits an hour. Returns True when one started."""
        self.reload_if_changed()
        if not self.enabled or self.job is not None or not self.synced:
            return False
        now = now or int(time.time())
        age = now - self.synced
        if at_open:
            if age < LOCAL_SYNC_AT_OPEN:
                return False
        elif age < LOCAL_SYNC_EVERY or now - self.auto_tried < 3600:
            return False
        self.auto_tried = now
        self.log(u"%s: %s" % (LOCAL_NAME, at_open and u"checking a2cogs for new titles"
                              or u"daily check for new titles"))
        return self.start(u"sync")

    def stop(self):
        self.cancel = True

    def run_job(self, what, reset):
        try:
            try:
                self.sync(what, reset)
            except Exception:
                e = sys.exc_info()[1]
                self.log(u"%s: %s stopped: %s" % (LOCAL_NAME, what, tos(str(e))))
        finally:
            self.drop_folder()
            self.job = None

    def fetch_catalog(self):
        r = http_open(self.catalog_url)
        try:
            raw = r.read()
        finally:
            r.close()
        titles = self.parse_catalog(raw)
        if not titles:
            raise IOError("the catalog came back empty")
        makedirs(self.dir)
        makedirs(self.disks_dir)
        tmp = self.catalog_path() + u".tmp"
        f = open(tmp, "wb")
        try:
            f.write(raw)
        finally:
            f.close()
        rename_over(tmp, self.catalog_path())
        return titles

    def remote_tag(self, url):
        """The host's revision tag for url (ETag, else Last-Modified), or
        u'' when it cannot be had. Only our own hosts are asked."""
        if url_host_of(url) not in LOCAL_HEAD_HOSTS and not url.startswith(u"http://127.0.0.1"):
            return u""
        try:
            r = http_open(url, head=True, timeout=30)
            try:
                info = r.info()
                return tos(info.get("ETag") or info.get("Last-Modified") or u"").strip()
            finally:
                r.close()
        except Exception:
            return u""

    def fetch_image(self, url, fname):
        """Download url into disks/fname. Returns (tag, bytes)."""
        dest = self.image_path(fname)
        part = dest + u".part"
        last = None
        for attempt in range(3):
            if self.cancel:
                raise IOError("stopped")
            try:
                r = http_open(url, timeout=120)
                try:
                    info = r.info()
                    tag = tos(info.get("ETag") or info.get("Last-Modified") or u"").strip()
                    want = info.get("Content-Length")
                    n = 0
                    f = open(part, "wb")
                    try:
                        while True:
                            chunk = r.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            n += len(chunk)
                            j = self.job
                            if j:
                                j[u"bytes"] += len(chunk)
                            if self.cancel:
                                raise IOError("stopped")
                    finally:
                        f.close()
                finally:
                    r.close()
                if n == 0 or (want and str(want).strip().isdigit() and int(want) != n):
                    raise IOError("short download (%d bytes)" % n)
                rename_over(part, dest)
                return tag, n
            except Exception:
                last = sys.exc_info()[1]
                try:
                    os.remove(part)
                except OSError:
                    pass
                if self.cancel:
                    break
                time.sleep(1 + 2 * attempt)
        raise IOError(tos(str(last)))

    def free_bytes(self):
        if not hasattr(os, "statvfs"):
            return None
        try:
            st = os.statvfs(self.dir)
            return st.f_bavail * st.f_frsize
        except (OSError, AttributeError):
            return None

    def sync(self, what, reset):
        if self.disk_stamp() != self.stamp:
            self.load()                   # another manager wrote the record since we read it
        old = self.titles
        titles = self.fetch_catalog()
        self.mark_written()
        self.lock.acquire()
        try:
            wanted = self.wanted(titles)
            if reset == u"yours":
                reset = set([f for f, im in self.images.items() if im.state == u"yours"])
            reset = set(reset or [])
            for f in reset:               # Reset on a title a2cogs dropped lets it go
                if f not in wanted and f in self.images:
                    self.images[f].state = u"ok"
            # titles the catalog dropped: one whose image is yours stays listed
            kept, keep_files, newly = [], set(), []
            for t in old:
                if t.file in wanted:
                    continue
                files = [t.file] + [f for u, f in t.parts]
                if not [f for f in files if f not in wanted and f in self.have
                        and f in self.images and self.images[f].state == u"yours"]:
                    continue
                if not t.retired:
                    newly.append(t.name)
                t.retired = True
                kept.append(t)
                keep_files.update(files)
            # images nothing names any more, and nobody wrote to: they go
            drop = sorted([f for f, im in self.images.items()
                           if f not in wanted and f not in keep_files
                           and not (im.state == u"yours" and f in self.have)], key=lower_key)
            drop_files = [f for f in drop if f in self.have]
            held = len(drop_files) > max(LOCAL_PRUNE_MIN, len(self.images) // 4)
            if not held:
                for f in drop:
                    del self.images[f]
                    self.have.discard(f)
            self.titles = self.merge_kept(titles, kept)
            self.enabled = True
            self._listed = None
            size_of = {}
            for t in titles:
                size_of[t.file] = t.bytes
        finally:
            self.lock.release()
        self.save_kept(kept)
        for name in newly:
            self.log(u"no longer in the CoGS catalog: %s (yours kept; Reset removes it)" % name)
        removed = 0
        if held:
            self.log(u"%s: %d images are no longer in the catalog. That is over a quarter "
                     u"of this copy, so none were removed" % (LOCAL_NAME, len(drop_files)))
        else:
            for f in drop_files:
                if not f or f in (u".", u"..") or u"/" in f or u"\\" in f:
                    continue
                try:
                    os.remove(self.image_path(f))
                    removed += 1
                    if removed <= 8:
                        self.log(u"no longer in the CoGS catalog, removed: %s" % f)
                except OSError:
                    pass
            if drop:
                self.save_record()
        # what each image needs: nothing, a check of its tag, or a fetch
        todo = []
        need = 0
        for fname in sorted(wanted.keys(), key=lower_key):
            url = wanted[fname]
            im = self.images.get(fname)
            on_disk = os.path.isfile(self.image_path(fname))
            if im is None and on_disk and fname not in reset:
                # a file by this name was in the folder before the catalog's
                # copy ever came down: it is the user's, so it is kept
                im = LocalImage(fname, url)
                im.state = u"yours"
                try:
                    st = os.stat(self.image_path(fname))
                    im.bytes = st.st_size
                    im.mtime = int(st.st_mtime)
                except OSError:
                    pass
                self.lock.acquire()
                try:
                    self.images[fname] = im
                    self.have.add(fname)
                    self.added.pop(fname, None)
                    self._listed = None
                finally:
                    self.lock.release()
                self.log(u"%s was already in the folder: kept as Yours (Local), "
                         u"Reset takes the a2cogs copy" % fname)
            if im is None or not on_disk or fname in reset:
                todo.append((fname, url, u"fetch"))
                need += size_of.get(fname, 0)
            else:
                todo.append((fname, url, u"check"))
        free = self.free_bytes()
        if free is not None and need and free < need + 64 * 1048576:
            raise IOError("not enough room: %s needed, %s free on %s" % (
                fmt_mb(need), fmt_mb(free), self.dir))
        j = self.job
        j[u"total"] = len([t for t in todo if t[2] == u"fetch"])
        j[u"bytes_total"] = need
        stats = {u"fetched": 0, u"bytes": 0, u"newer": 0, u"replaced": 0, u"failed": 0}
        queue = list(todo)
        qlock = threading.Lock()

        def worker():
            while not self.cancel:
                qlock.acquire()
                try:
                    if not queue:
                        return
                    fname, url, act = queue.pop(0)
                finally:
                    qlock.release()
                self.touch_folder()
                im = self.images.get(fname)
                new_tag = None
                if act == u"check":
                    changed = (im.url != url)
                    if not changed:
                        new_tag = self.remote_tag(url)
                        changed = bool(new_tag and im.tag and new_tag != im.tag)
                    if not changed:
                        if im.newer and im.state != u"yours":
                            im.newer = False
                        continue
                    if im.state == u"yours":
                        if not im.newer:
                            im.newer = True
                            stats[u"newer"] += 1
                            self.log(u"newer copy on a2cogs: %s (yours kept; Reset takes it)" % fname)
                        continue
                    stats[u"replaced"] += 1
                    j[u"total"] += 1
                try:
                    tag, n = self.fetch_image(url, fname)
                except Exception:
                    e = sys.exc_info()[1]
                    stats[u"failed"] += 1
                    j[u"failed"] += 1
                    self.log(u"could not fetch %s: %s" % (fname, tos(str(e))))
                    continue
                self.lock.acquire()
                try:
                    im = LocalImage(fname, url)
                    im.bytes = n
                    im.tag = tag
                    try:
                        im.mtime = int(os.stat(self.image_path(fname)).st_mtime)
                    except OSError:
                        im.mtime = 0
                    self.images[fname] = im
                    self.have.add(fname)
                    self._listed = None
                    stats[u"fetched"] += 1
                    stats[u"bytes"] += n
                    j[u"done"] += 1
                    save_now = True       # a file with no row would pass for the user's own
                finally:
                    self.lock.release()
                if save_now:
                    self.save_record()

        threads = [daemon_thread(worker) for _ in range(LOCAL_WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            while thread_alive(t):
                t.join(0.5)
        if not self.cancel and not stats[u"failed"]:
            self.synced = int(time.time())
        self.save_record()
        line = u"%s %s: %d image(s) fetched (%s)" % (
            LOCAL_NAME, self.cancel and (what + u" stopped") or what, stats[u"fetched"],
            fmt_mb(stats[u"bytes"]))
        if stats[u"replaced"]:
            line += u", %d updated" % stats[u"replaced"]
        if removed:
            line += u", %d removed (no longer in the catalog)" % removed
        if stats[u"newer"]:
            line += u", %d newer cop%s of images marked yours (kept)" % (
                stats[u"newer"], stats[u"newer"] == 1 and u"y" or u"ies")
        if stats[u"failed"]:
            line += u", %d failed (Sync tries them again)" % stats[u"failed"]
        self.mark_written()               # images the user put in the folder meanwhile
        t, i, b, y, n = self.counts()
        self.log(line)
        self.log(u"%s: %d titles, %d images, %s on disk%s" % (
            LOCAL_NAME, t, i, fmt_mb(b), self.added and (u", %d added" % len(self.added)) or u""))


# ---------------------------------------------------------------- library

class Library(object):
    """The shared folder, its three catalogs, the hub, and the status the
    screen shows. One lock; the HTTP threads read under it, rescans swap
    whole catalogs under it."""

    def __init__(self, root, writable, port, share_name, advertise, converters,
                 local_dir=None, local_url=None):
        self.root = root
        self.writable = writable
        self.port = port
        self.share_name = share_name
        self.advertise = advertise
        self.conv = converters
        self.lock = threading.RLock()
        self.disks = DiskCatalog([], writable)
        self.audio = MediaCatalog(u"audio", [], [], 0)
        self.pictures = MediaCatalog(u"pictures", [], [], 0)
        self.hub = HubConfig()
        self.folders = []
        self.files = []
        self.rejected = 0
        self.requests = 0
        self.bytes_served = 0
        self.log_lines = []
        self.last_read = {}
        self.last_write = {}
        self.pending_audio = []
        self.pending_pictures = []
        self.converting = None
        self.signature = None
        self.bonjour = None
        self.bonjour_state = u"off"
        self.sharing = True       # B key: serve + announce (off = 503 to the card, no Bonjour)
        self.auto_sync_on = True  # --no-auto-sync
        self.stop_flag = False
        self.load_cfg()
        self.local = LocalCatalog(
            local_dir or os.path.join(os.path.dirname(root.rstrip(u"/\\")) or root, LOCAL_DIR_NAME),
            self.log, local_url)

    # -- config (per library, a dotfile the walker skips)
    def cfg_path(self):
        return os.path.join(self.root, CFG_NAME)

    def load_cfg(self):
        try:
            f = open(self.cfg_path(), "rb")
        except IOError:
            return
        try:
            cats = []
            for raw in f.read().splitlines():
                line = tos(raw).strip()
                if line.startswith(u"category="):
                    pl = line[9:].split(u"|", 1)
                    cats.append([pl[0], len(pl) == 2 and pl[1] or None])
                elif line == u"prefix_subfolder=1":
                    self.hub.prefix_subfolder = True
                elif line == u"writable=0":
                    self.writable = False
                elif line == u"writable=1":
                    self.writable = True
            if cats:
                self.hub.cats = cats
                self.hub.explicit = True
        finally:
            f.close()

    def save_cfg(self):
        lines = [u"# CoGS Image Manager (text) settings for this library\n"]
        if self.hub.explicit:
            for p, l in self.hub.cats:
                lines.append(u"category=%s%s\n" % (p, l and (u"|" + l) or u""))
        if self.hub.prefix_subfolder:
            lines.append(u"prefix_subfolder=1\n")
        lines.append(u"writable=%d\n" % (self.writable and 1 or 0))
        try:
            f = open(self.cfg_path(), "wb")
            try:
                f.write(tob(u"".join(lines)))
            finally:
                f.close()
        except IOError:
            pass

    # -- logging / status
    def log(self, line):
        self.lock.acquire()
        try:
            self.log_lines.append(u"%s  %s" % (now_hms(), line))
            if len(self.log_lines) > 300:
                del self.log_lines[:len(self.log_lines) - 300]
        finally:
            self.lock.release()
        if HEADLESS:
            write(self.log_lines[-1])

    def base_url_guess(self):
        return u"http://%s:%d/" % (lan_ip(), self.port)

    # -- scanning
    def file_signature(self):
        """Names, sizes and mtimes of everything the share could serve. Other
        files (a log written into the library, a stray .txt) are left out so
        they cannot trigger rescans."""
        h = hashlib.sha1()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(u".")]
            in_files = (dirpath == os.path.join(self.root, FILES_SUB))
            for fn in filenames:
                if fn.startswith(u"."):
                    continue
                ext = os.path.splitext(fn)[1][1:].lower()
                if not in_files and ext not in SIG_EXT:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                h.update(tob(u"%s|%d|%d\n" % (p, st.st_size, int(st.st_mtime))))
        return h.hexdigest()

    def rescan(self, reason=u"rescan"):
        files = walk_library(self.root)
        images = []
        rejected = 0
        audio_ready = []
        audio_pending = []
        audio_pending_src = []
        audio_skipped = 0
        pic_ready = []
        pic_pending = []
        pic_pending_src = []
        pic_skipped = 0
        seen_a = set()
        seen_p = set()
        for f in files:
            ext = f.ext()
            if ext in DISK_EXT:
                img = probe_disk(f.path, f.folder, f.name)
                if img:
                    images.append(img)
            elif ext in REJECTED_EXT:
                rejected += 1
            elif ext == u"pcm":
                sz = file_size(f.path)
                if sz and f.rel_path() not in seen_a:
                    seen_a.add(f.rel_path())
                    audio_ready.append(Track(f.rel_path(), f.folder, f.path, f.path, sz))
            elif ext == u"shr":
                sz = file_size(f.path)
                if sz and f.rel_path() not in seen_p:
                    seen_p.add(f.rel_path())
                    pic_ready.append(Track(f.rel_path(), f.folder, f.path, f.path, sz))
        for f in files:
            ext = f.ext()
            if ext in AUDIO_SRC_EXT:
                served = os.path.splitext(f.rel_path())[0] + u".pcm"
                if served in seen_a:
                    continue
                seen_a.add(served)
                cached = self.conv.pcm_path(f.path)
                sz = file_size(cached)
                if sz:
                    audio_ready.append(Track(served, f.folder, f.path, cached, sz))
                elif self.conv.audio_ok() and f.path not in self.conv.failed:
                    audio_pending.append(clean_name(f.name))
                    audio_pending_src.append(f.path)
                else:
                    audio_skipped += 1
            elif ext in PICTURE_SRC_EXT:
                served = os.path.splitext(f.rel_path())[0] + u".shr"
                if served in seen_p:
                    continue
                seen_p.add(served)
                cached = self.conv.shr_path(f.path)
                sz = file_size(cached)
                if sz == SHR_BYTES:
                    pic_ready.append(Track(served, f.folder, f.path, cached, sz))
                elif self.conv.picture_ok() and f.path not in self.conv.failed:
                    pic_pending.append(clean_name(f.name))
                    pic_pending_src.append(f.path)
                else:
                    pic_skipped += 1
        folders = folders_of(files)
        self.lock.acquire()
        try:
            self.disks = DiskCatalog(images, self.writable)
            self.audio = MediaCatalog(u"audio", audio_ready, sorted(audio_pending), audio_skipped)
            self.pictures = MediaCatalog(u"pictures", pic_ready, sorted(pic_pending), pic_skipped)
            self.folders = folders
            self.files = files_list(self.root)
            self.rejected = rejected
            self.all_files = files
            if not self.hub.explicit:
                self.hub.defaults_from(folders)
            self.pending_audio = audio_pending_src
            self.pending_pictures = pic_pending_src
            self.signature = self.file_signature()
        finally:
            self.lock.release()
        self.log(u"%s: %d disk image(s), %d track(s), %d picture(s), %d file(s)" % (
            reason, len(images), len(audio_ready), len(pic_ready), len(self.files)))

    def set_writable(self, w):
        self.lock.acquire()
        try:
            self.writable = w
            self.disks.writable = w
        finally:
            self.lock.release()
        self.save_cfg()
        self.log(u"share is now %s" % (w and u"writable" or u"read-only"))

    def counts_under(self, folder):
        """(disks, tracks, pictures) in folder and below."""
        d = len([i for i in self.disks.images if hub_under(i.folder, folder)])
        a = len([t for t in self.audio.tracks if hub_under(t.folder, folder)])
        p = len([t for t in self.pictures.tracks if hub_under(t.folder, folder)])
        return d, a, p

    def hub_json(self, kind):
        self.lock.acquire()
        try:
            if kind == u"audio":
                return self.hub.json(kind, len(self.audio.tracks),
                                     lambda c: len([t for t in self.audio.tracks if hub_under(t.folder, c)]))
            if kind == u"pictures":
                return self.hub.json(kind, len(self.pictures.tracks),
                                     lambda c: len([t for t in self.pictures.tracks if hub_under(t.folder, c)]))
            return self.hub.json(u"disks", len(self.disks.images),
                                 lambda c: len([i for i in self.disks.images if hub_under(i.folder, c)]))
        finally:
            self.lock.release()

    def cover_map(self, base):
        m = {}
        for p in self.pictures.tracks:
            # pct(), not encode_path(): the Mac app encodes the slash too
            # (%2F) and the two servers should emit identical bytes.
            m[pic_stem(p.display)] = base + u"pictures/" + pct(p.served)
        return m

    # -- background work
    def watcher(self):
        """Rescan when the folder changes (poll every 3 s; no FSEvents on a
        G4), and run conversions one at a time."""
        while not self.stop_flag:
            try:
                sig = self.file_signature()
                if sig != self.signature:
                    self.rescan(u"folder changed")
            except Exception:
                pass
            loc = self.local
            try:
                if not loc.busy():
                    # another manager's record, images written to or deleted
                    # outside this program, and the daily sync
                    if not loc.reload_if_changed() and loc.enabled:
                        loc.mark_written()
                    if self.auto_sync_on:
                        loc.auto_sync()
            except Exception:
                pass
            did = False
            src = None
            self.lock.acquire()
            try:
                if self.pending_audio:
                    src = self.pending_audio.pop(0)
                    kind = u"audio"
                elif self.pending_pictures:
                    src = self.pending_pictures.pop(0)
                    kind = u"picture"
            finally:
                self.lock.release()
            if src:
                self.converting = clean_name(os.path.basename(src))
                if kind == u"audio":
                    ok = self.conv.transcode_audio(src)
                else:
                    ok = self.conv.convert_picture(src)
                self.converting = None
                if ok:
                    self.log(u"%s ready: %s" % (kind, clean_name(os.path.basename(src))))
                else:
                    self.conv.failed.add(src)
                    self.log(u"%s convert failed: %s" % (kind, os.path.basename(src)))
                self.rescan(u"converted")
                did = True
            if not did:
                for _ in range(30):
                    if self.stop_flag:
                        break
                    time.sleep(0.1)

    # -- bonjour
    def start_bonjour(self):
        if self.bonjour or not self.advertise:
            return
        cmd = None
        name = self.share_name
        if which("dns-sd"):
            cmd = [which("dns-sd"), "-R", name, "_cogs-share._tcp", "local", str(self.port), "path=/"]
        elif which("avahi-publish"):
            cmd = [which("avahi-publish"), "-s", name, "_cogs-share._tcp", str(self.port), "path=/"]
        elif which("avahi-publish-service"):
            cmd = [which("avahi-publish-service"), name, "_cogs-share._tcp", str(self.port), "path=/"]
        if cmd is None:
            self.bonjour_state = u"no dns-sd/avahi (type the URL on the card)"
            return
        devnull = open(os.devnull, "wb")
        try:
            try:
                self.bonjour = subprocess.Popen(cmd, stdin=devnull, stdout=devnull, stderr=devnull)
                self.bonjour_state = u"on"
                self.log(u"Bonjour: announcing \"%s\" as _cogs-share._tcp" % name)
            except OSError:
                self.bonjour_state = u"failed to start"
        finally:
            devnull.close()

    def stop_bonjour(self):
        if self.bonjour:
            try:
                self.bonjour.terminate()
            except (OSError, AttributeError):
                try:
                    os.kill(self.bonjour.pid, 15)
                except OSError:
                    pass
            self.bonjour = None
            self.bonjour_state = u"off"
            self.log(u"Bonjour: off")

    def reap_stale_bonjour(self):
        """A dns-sd/avahi child outlives a cim.py that was killed (not quit),
        and keeps announcing a share nothing listens on: the card finds it
        and the fetch fails. Kill any leftover announcer of our service type
        on our port before starting ours. ps(1) instead of pgrep: Tiger and
        Leopard have no pgrep."""
        try:
            out = subprocess.Popen(["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE).communicate()[0]
        except OSError:
            return
        me = os.getpid()
        for line in tos(out).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid, cmd = parts
            if u"_cogs-share._tcp" not in cmd or (u"dns-sd" not in cmd and u"avahi-publish" not in cmd):
                continue
            if (u" %d " % self.port) not in cmd + u" ":
                continue
            try:
                if int(pid) == me:
                    continue
                os.kill(int(pid), 15)
                self.log(u"stopped a leftover announcer (pid %s)" % pid)
            except (OSError, ValueError):
                pass

    # -- sharing (B key): the card sees the share only while this is on
    def sharing_state(self):
        if not self.sharing:
            return u"off"
        if self.bonjour:
            return u"on (Bonjour \"%s\")" % self.share_name
        if not self.advertise:
            return u"on (not announced)"
        return u"on (%s)" % self.bonjour_state

    def start_sharing(self):
        self.sharing = True
        self.log(u"sharing on")
        self.start_bonjour()

    def stop_sharing(self):
        self.sharing = False
        self.stop_bonjour()
        self.log(u"sharing off (card requests get 503 until B again)")

    def toggle_sharing(self):
        if self.sharing:
            self.stop_sharing()
        else:
            self.start_sharing()


HEADLESS = False
LIB = None


def lan_ip():
    """The address the card will reach us at. A UDP connect picks the
    outbound interface without sending anything."""
    s = None
    try:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            return tos(s.getsockname()[0])
        except socket.error:
            pass
    finally:
        if s:
            s.close()
    try:
        return tos(socket.gethostbyname(socket.gethostname()))
    except socket.error:
        return u"127.0.0.1"


def is_private_ipv4(ip):
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        p = [int(x) for x in parts]
    except ValueError:
        return False
    if p[0] == 10 or p[0] == 127:
        return True
    if p[0] == 172 and 16 <= p[1] <= 31:
        return True
    if p[0] == 192 and p[1] == 168:
        return True
    if p[0] == 169 and p[1] == 254:
        return True
    return False


# ------------------------------------------------------------------- HTTP

STATUS_TEXT = {200: "OK", 201: "Created", 206: "Partial Content", 400: "Bad Request",
               403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
               409: "Conflict", 413: "Payload Too Large", 416: "Range Not Satisfiable",
               500: "Internal Server Error"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CoGS-IM-text/" + VERSION
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def __getattr__(self, name):
        # Any method we do not implement answers 405, not 501.
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self):
        self.reply(405, "text/plain", u"no\n")

    # -- helpers
    def hdr(self, name):
        v = self.headers.get(name)
        return v is not None and tos(v).strip() or None

    def wants_close(self):
        return (self.hdr("Connection") or u"").lower() == u"close"

    def split_path(self):
        raw = tos(self.path)
        if u"?" in raw:
            p, q = raw.split(u"?", 1)
        else:
            p, q = raw, u""
        query = {}
        for pair in q.split(u"&"):
            if not pair:
                continue
            if u"=" in pair:
                k, v = pair.split(u"=", 1)
                query[k] = unquote(v)
            else:
                query[pair] = u""
        return unquote(p), query

    def base_url(self):
        host = self.hdr("Host") or (u"%s:%d" % (lan_ip(), LIB.port))
        return u"http://%s/" % host

    def reply(self, code, mime, body, extra=None):
        data = tob(body)
        self.send_response(code, STATUS_TEXT.get(code, "?"))
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if code == 200 and mime == "application/json":
            self.send_header("Accept-Ranges", "bytes")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.send_header("Connection", self.wants_close() and "close" or "keep-alive")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def lan_guard(self):
        ip = self.client_address[0]
        ip4 = ip.replace("::ffff:", "")
        if is_private_ipv4(ip4) or ip.startswith("fe80") or ip == "::1":
            return True
        LIB.log(u"rejected non-LAN peer %s" % tos(ip))
        self.close_connection = 1
        return False

    # -- methods
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self.lan_guard():
            return
        LIB.requests += 1
        path, q = self.split_path()
        base = self.base_url()
        lib = LIB
        if path in (u"/", u"/index.html"):
            return self.reply(200, "text/html; charset=utf-8", index_html(lib, base))
        if not lib.sharing:
            return self.reply(503, "text/plain", u"sharing is off", [("Retry-After", "5")])
        if path == u"/cogs-volumes.json" or (path.startswith(u"/cogs-volumes-") and path.endswith(u".json")):
            n = 1
            if path != u"/cogs-volumes.json":
                try:
                    n = max(1, int(path[len(u"/cogs-volumes-"):-5]))
                except ValueError:
                    n = 1
            lib.log(u"catalog page %d" % n)
            lib.lock.acquire()
            try:
                body = lib.disks.page_json(n, base, lib.cover_map(base), lib.local.count())
            finally:
                lib.lock.release()
            return self.reply(200, "application/json", body)
        if path == LOCAL_PREFIX or path.startswith(LOCAL_PREFIX + u"/"):
            return self.serve_local(path[len(LOCAL_PREFIX):], q, base)
        if path == u"/cogs-your-hub.json":
            kind = {u"a": u"audio", u"p": u"pictures"}.get(q.get(u"i"), u"disks")
            lib.log(u"your hub (%s)" % kind)
            return self.reply(200, "application/json", lib.hub_json(kind))
        if path == u"/search":
            qs = q.get(u"q", u"")
            try:
                p = max(1, int(q.get(u"p", u"1") or u"1"))
            except ValueError:
                p = 1
            cat = q.get(u"cat") or None
            what = cat and (u' cat "%s"' % cat) or u""
            i = q.get(u"i")
            lib.lock.acquire()
            try:
                pre = lib.hub.prefix_subfolder
                if i == u"a":
                    lib.log(u'audio search "%s"%s p%d' % (qs, what, p))
                    body = lib.audio.search_json(qs, p, base, cat, pre)
                elif i == u"s":
                    lib.log(u'stations search "%s" (none on a LAN share)' % qs)
                    body = STATIONS_EMPTY
                elif i == u"p":
                    lib.log(u'pictures search "%s"%s p%d' % (qs, what, p))
                    body = lib.pictures.search_json(qs, p, base, cat, pre)
                else:
                    lib.log(u'search "%s"%s p%d' % (qs, what, p))
                    body = lib.disks.search_json(qs, p, base, lib.cover_map(base), cat, pre)
            finally:
                lib.lock.release()
            return self.reply(200, "application/json", body)
        for cat_obj, stem in ((lib.audio, u"/cogs-audio"), (lib.pictures, u"/cogs-pictures")):
            if path == stem + u".json" or (path.startswith(stem + u"-") and path.endswith(u".json")):
                n = 1
                if path != stem + u".json":
                    try:
                        n = max(1, int(path[len(stem) + 1:-5]))
                    except ValueError:
                        n = 1
                lib.log(u"%s page %d" % (cat_obj.kind, n))
                lib.lock.acquire()
                try:
                    body = cat_obj.page_json(n, base)
                finally:
                    lib.lock.release()
                return self.reply(200, "application/json", body)
        if path.startswith(u"/audio/"):
            t = lib.audio.find(served_path(path, u"/audio/"))
            return self.serve_file(t and t.blob, t and (u"audio/" + t.served))
        if path.startswith(u"/pictures/"):
            t = lib.pictures.find(served_path(path, u"/pictures/"))
            return self.serve_file(t and t.blob, t and (u"pictures/" + t.served))
        if path == u"/files" or path.startswith(u"/files/"):
            return self.serve_files(path, q, base)
        p = resolve_request_path(lib.root, path)
        rel = p and path.strip(u"/") or None
        return self.serve_file(p, rel)

    def serve_local(self, rest, q, base):
        """CoGS Catalog Local R/W: the a2cogs disk index, from this machine.
        Same files and JSON as a2cogs.com/disks/, image URLs pointing here."""
        lib = LIB
        loc = lib.local
        if rest == u"/cogs-hub.json":
            lib.log(u"hub (%s)" % LOCAL_NAME)
            return self.reply(200, "application/json", loc.hub_json())
        if rest == u"/cogs-volumes.json" or (rest.startswith(u"/cogs-volumes-") and rest.endswith(u".json")):
            n = 1
            if rest != u"/cogs-volumes.json":
                try:
                    n = max(1, int(rest[len(u"/cogs-volumes-"):-5]))
                except ValueError:
                    n = 1
            lib.log(u"local catalog page %d" % n)
            return self.reply(200, "application/json", loc.page_json(n, base, lib.writable))
        if rest == u"/search":
            try:
                p = max(1, int(q.get(u"p", u"1") or u"1"))
            except ValueError:
                p = 1
            qs = q.get(u"q", u"")
            cat = q.get(u"cat") or u""
            host = q.get(u"host") or u""
            lib.log(u'local search "%s"%s%s p%d' % (
                qs, cat and (u" cat %s" % cat) or u"", host and (u" host %s" % host) or u"", p))
            return self.reply(200, "application/json",
                              loc.search_json(qs, p, base, lib.writable, cat, host))
        if rest.startswith(u"/disks/"):
            fname = rest[len(u"/disks/"):]
            p = loc.find_file(fname)
            return self.serve_file(p, p and (LOCAL_DISKS + fname))
        return self.reply(404, "text/plain", u"no such file\n")

    def serve_files(self, path, q, base):
        lib = LIB
        rest = u"/".join([s for s in path[len(u"/files"):].split(u"/") if s])
        if not rest:
            if q.get(u"fmt") == u"txt":
                lib.log(u"files listing (txt)")
                return self.reply(200, "text/plain; charset=utf-8", files_listing_txt(lib.root))
            lib.log(u"files listing")
            return self.reply(200, "application/json", files_listing_json(lib.root, base, lib.writable))
        name = files_safe_name(rest)
        f = name and files_find(lib.root, name) or None
        if not f:
            return self.reply(404, "text/plain", u"no such file\n")
        lib.log(u"GET files/%s" % f.name)
        return self.serve_file(f.path, u"files/" + f.name)

    def serve_file(self, path, key):
        lib = LIB
        if not path:
            return self.reply(404, "text/plain", u"no such file\n")
        try:
            st = os.stat(path)
        except OSError:
            return self.reply(404, "text/plain", u"no such file\n")
        size = st.st_size
        start = 0
        end = size > 0 and size - 1 or 0
        partial = False
        rng = self.hdr("Range")
        if rng and rng.startswith(u"bytes="):
            partial = True
            spec = rng[6:].split(u",")[0].strip()
            ab = spec.split(u"-", 1)
            try:
                if ab[0] == u"":
                    n = int(ab[1] or u"0")
                    start = size > n and size - n or 0
                else:
                    start = int(ab[0])
                    if len(ab) > 1 and ab[1] != u"":
                        end = int(ab[1])
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size > 0 and size - 1 or 0)
            if start > end or start >= size:
                self.send_response(416, STATUS_TEXT[416])
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", self.wants_close() and "close" or "keep-alive")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(partial and 206 or 200, STATUS_TEXT[partial and 206 or 200])
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        # fw 1.26 reads these on every reply: the write-protect flag and the
        # idle re-check interval.
        self.send_header("X-CoGS-Writable", lib.writable and "1" or "0")
        self.send_header("X-CoGS-Poll", "5")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Last-Modified", formatdate(st.st_mtime, usegmt=True))
        self.send_header("Content-Length", str(length))
        self.send_header("Connection", self.wants_close() and "close" or "keep-alive")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            f = open(path, "rb")
        except IOError:
            self.close_connection = 1
            return
        try:
            f.seek(start)
            lib.bytes_served += length
            if key:
                lib.last_read[key] = time.time()
            if length > 4096:
                lib.log(u"GET %s [%d-%d]" % (os.path.basename(path), start, end))
            left = length
            while left > 0:
                chunk = f.read(min(left, 256 * 1024))
                if not chunk:
                    self.close_connection = 1
                    break
                try:
                    self.wfile.write(chunk)
                except socket.error:
                    self.close_connection = 1
                    break
                left -= len(chunk)
        finally:
            f.close()

    def read_body(self):
        try:
            n = int(self.hdr("Content-Length") or u"0")
        except ValueError:
            n = 0
        if n <= 0:
            return tob(u"")
        return self.rfile.read(n)

    def do_PUT(self):
        if not self.lan_guard():
            return
        LIB.requests += 1
        lib = LIB
        path, q = self.split_path()
        body = self.read_body()
        if not lib.sharing:
            return self.reply(503, "text/plain", u"sharing is off", [("Retry-After", "5")])
        if not lib.writable:
            return self.reply(403, "text/plain", u"")
        if path.startswith(u"/files/"):
            code, name = files_put(lib.root, path[len(u"/files/"):], body, self.hdr("Content-Range"))
            if code in (200, 201):
                lib.last_write[u"files/" + name] = time.time()
                cr = self.hdr("Content-Range") or u""
                if cr.startswith(u"bytes 0-"):
                    lib.log(u"files: receiving %s (%s bytes)" % (name, cr.split(u"/")[-1]))
                elif not cr:
                    lib.log(u"files: %s saved" % name)
                lib.lock.acquire()
                try:
                    lib.files = files_list(lib.root)
                finally:
                    lib.lock.release()
            else:
                lib.log(u"files: PUT %s -> %d" % (name, code))
            return self.reply(code, "text/plain", u"")
        local_file = None
        if path.startswith(LOCAL_PREFIX + u"/disks/"):
            local_file = path[len(LOCAL_PREFIX + u"/disks/"):]
            p = lib.local.find_file(local_file)
        else:
            p = resolve_request_path(lib.root, path)
        if not p:
            return self.reply(404, "text/plain", u"")
        size = file_size(p)
        start = 0
        cr = self.hdr("Content-Range")
        if cr and cr.startswith(u"bytes "):
            a = cr[6:].split(u"/")[0].split(u"-")[0]
            if not a.isdigit():
                return self.reply(400, "text/plain", u"")
            start = int(a)
        # Never grow an image: writes land inside the existing file only.
        if start + len(body) > size:
            return self.reply(416, "text/plain", u"")
        try:
            f = open(p, "r+b")
            try:
                f.seek(start)
                f.write(body)
            finally:
                f.close()
        except IOError:
            return self.reply(500, "text/plain", u"")
        lib.last_write[path.strip(u"/")] = time.time()
        lib.log(u"PUT %s @%d +%d" % (os.path.basename(p), start, len(body)))
        if local_file:
            lib.local.note_write(local_file)
        return self.reply(200, "text/plain", u"")


def served_path(path, prefix):
    rel = path[len(prefix):]
    segs = [s for s in rel.split(u"/") if s]
    for s in segs:
        if s == u".." or s.startswith(u"."):
            return None
    return u"/".join(segs) or None


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def handle_error(self, request, client_address):
        # the stock handler prints a traceback to stderr, which lands on
        # top of the TUI (a card that resets mid-request did, 2026-11-10)
        e = sys.exc_info()[1]
        try:
            LIB.log(u"connection from %s dropped: %s" % (client_address[0], tos(str(e)) or e.__class__.__name__))
        except Exception:
            pass


def html_esc(s):
    return s.replace(u"&", u"&amp;").replace(u"<", u"&lt;").replace(u">", u"&gt;")


def index_html(lib, base):
    lib.lock.acquire()
    try:
        rows = []
        for img in lib.disks.images:
            rows.append(u'<li><a href="/%s">%s</a> (%.1f MB, %d blocks%s)</li>' % (
                encode_path(img.rel_path()), html_esc(img.display), img.bytes / 1048576.0,
                img.blocks, img.bootable and u", bootable" or u""))
        if not rows:
            rows.append(u"<li><i>no disk images found</i></li>")
        parts = [u"<!doctype html><meta charset=utf-8><title>CoGS Image Manager (text)</title>",
                 u'<body style="font-family: ui-monospace, monospace; background:#111; color:#3fda66; padding:2em">',
                 u"<h1>] CoGS IMAGE MANAGER (text edition)</h1>",
                 u'<p>Image Source URL for the card: <code style="color:#fff">%s</code></p>' % html_esc(base)]
        if lib.hub.cats:
            cats = []
            for p, l in lib.hub.cats[:HUB_MAX]:
                d, a, pc = lib.counts_under(p)
                cats.append(u'<li><a href="/search?cat=%s">%s</a> (%d)</li>' % (
                    encode_path(p), html_esc(lib.hub.label(p)), d + a + pc))
            parts.append(u'<p>Apple II categories: <a style="color:#3fda66" href="/cogs-your-hub.json">'
                         u'/cogs-your-hub.json</a> (%d)</p><ul>%s</ul>' % (len(lib.hub.cats), u"".join(cats)))
        parts.append(u'<p>Catalog: <a style="color:#3fda66" href="/cogs-volumes.json">/cogs-volumes.json</a> '
                     u'(%d disk images, %d page%s, %s)</p><ul>%s</ul>' % (
                         len(lib.disks.images), lib.disks.pages(), lib.disks.pages() == 1 and u"" or u"s",
                         lib.writable and u"writable" or u"read-only", u"".join(rows)))
        for cat in (lib.audio, lib.pictures):
            if cat.tracks or cat.pending:
                li = [u'<li><a href="/%s%s">%s</a></li>' % (cat.prefix, encode_path(t.served), html_esc(t.display))
                      for t in cat.tracks]
                li += [u"<li><i>%s (converting...)</i></li>" % html_esc(n) for n in cat.pending]
                parts.append(u'<p>%s: <a style="color:#3fda66" href="/%s.json">/%s.json</a> (%d)</p><ul>%s</ul>' % (
                    cat.kind.capitalize(), cat.file, cat.file, len(cat.tracks), u"".join(li)))
        if lib.files or lib.writable:
            li = [u'<li><a href="/files/%s">%s</a> (%s, %d bytes)</li>' % (
                pct(f.name), html_esc(f.name), f.alias, f.bytes) for f in lib.files]
            if not li:
                li = [u"<li><i>empty; an Apple II program can PUT documents here</i></li>"]
            parts.append(u'<p>Files: <a style="color:#3fda66" href="/files/">/files/</a> (%d, %s)</p><ul>%s</ul>' % (
                len(lib.files), lib.writable and u"writable" or u"read-only", u"".join(li)))
        if lib.local.enabled:
            t, i, b, y, n = lib.local.counts()
            parts.append(u'<p>%s: <a style="color:#3fda66" href="%s/cogs-hub.json">%s/cogs-hub.json</a> '
                         u'(%d titles, %d images, %s, %d yours, %d with a newer copy on a2cogs%s)</p>' % (
                             LOCAL_NAME, LOCAL_PREFIX, LOCAL_PREFIX, t, i, fmt_mb(b), y, n,
                             lib.local.added and (u", %d added" % len(lib.local.added)) or u""))
        parts.append(u"</body>")
        return u"\n".join(parts)
    finally:
        lib.lock.release()


# ------------------------------------------------------------------ TUI

TYPES_WIDE = u"DISKS .po .hdv .2mg   AUDIO .mp3 .m4a .wav .aif .flac .ogg .pcm   PICTURES .jpg .png .gif .shr"
TYPES_NARROW = u"DISKS .po .hdv .2mg   AUDIO .mp3 .wav ...   PICTURES .jpg .png .shr ..."

HELP_LINES = [
    u"KEYS",
    u"",
    u"  Up/Down, j/k   move in the folder tree",
    u"  Right/Enter    open a folder (show its subfolders)",
    u"  Left           close a folder",
    u"  Tab            switch between the tree and the item list",
    u"  C              show/hide this folder as an Apple II category",
    u"  P              prefix subfolder names inside categories",
    u"  W              writable / read-only (the card's lock icon)",
    u"  B              start / stop sharing (Bonjour announce + answering the card)",
    u"  R              rescan the library now",
    u"  M              CoGS Catalog Local R/W: download the a2cogs disk catalog",
    u"                 (about 2 GB); once it is here, jump to its section",
    u"  S              sync it: new and changed titles. Yours (Local) are left alone.",
    u"                 It also syncs by itself at start and once a day",
    u"  X              reset: a fresh copy of the selected image, or of all Yours (Local)",
    u"  O              sort its lists by name or by status (newer, Yours (Local))",
    u"  D              delete the local catalog from this computer (it asks twice)",
    u"  L              full-screen request log (L again to return)",
    u"  ?              this help",
    u"  Q              quit",
    u"",
    u"FILE TYPES",
    u"",
    u"  Disk images    .po .hdv .2mg (ProDOS order; .dsk .do .woz .nib are skipped)",
    u"  Audio          .mp3 .m4a .wav .aif .flac .ogg .pcm",
    u"  Pictures       .jpg .png .gif .webp .bmp .shr",
    u"",
    u"ON THE APPLE II",
    u"",
    u"  CoGS config (C at boot, or the CDA) > Image/Audio Source,",
    u"  type the Image Source URL shown at the top of this screen,",
    u"  then Browse Disks. Top-level folders appear as categories.",
    u"  With the CoGS Catalog downloaded here (M), pick this",
    u"  computer for it on the card's Image/Audio Source page",
    u"  (scan, then C or T). Browse Disks lists it as its own row.",
]


def fmt_mb(n):
    if n >= 1048576 * 10:
        return u"%d MB" % (n // 1048576)
    if n >= 1048576:
        return u"%.1f MB" % (n / 1048576.0)
    return u"%d KB" % (n // 1024)


class Tui(object):
    def __init__(self, scr, lib):
        self.scr = scr
        self.lib = lib
        self.expanded = set([u""])
        self.sel = 0
        self.focus = u"tree"
        self.item_sel = 0
        self.mode = u"main"
        self.color = False
        self.local_sort = u"name"     # O: name / status (newer, yours, the rest)
        self.confirm = None           # (question, fn, detail) while a Y/N is up
        self.n_items = 0
        self._lrows = None
        self._lents = None
        try:
            curses.curs_set(0)
        except curses.error:
            pass                       # vt100 and friends have no cursor control
        if curses.has_colors():
            curses.start_color()
            # Only the eight basic colors, five pairs and A_BOLD: what any
            # color terminal has. A pair that will not set stays the
            # terminal's own colors, and a terminal with no color gets bold.
            try:
                curses.use_default_colors()
                bg = -1
                curses.init_pair(1, curses.COLOR_GREEN, bg)
            except curses.error:
                bg = curses.COLOR_BLACK
            for n, fg, back in ((1, curses.COLOR_GREEN, bg), (2, curses.COLOR_BLACK, curses.COLOR_GREEN),
                                (3, curses.COLOR_YELLOW, bg), (4, curses.COLOR_WHITE, bg),
                                (5, curses.COLOR_CYAN, bg)):
                try:
                    curses.init_pair(n, fg, back)
                except curses.error:
                    pass
            self.color = True
        scr.timeout(500)
        scr.keypad(1)

    def attr(self, n, bold=False):
        a = self.color and curses.color_pair(n) or 0
        if bold:
            a |= curses.A_BOLD
        return a

    def put(self, y, x, s, attr=0, width=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        if width is None:
            width = w - x
        s = s[:max(0, min(width, w - x - (1 if y == h - 1 else 0)))]
        if not s:
            return
        try:
            self.scr.addstr(y, x, PY2 and s.encode("utf-8") or s, attr)
        except curses.error:
            pass

    def put_parts(self, y, x, parts, width):
        """A line made of (text, kind) pieces. kind: None a label (green),
        "v" a value (white), "w" one that wants attention (yellow), "a"
        added (cyan). Values are bold, so they stand out without color too."""
        kinds = {u"v": self.attr(4, True), u"w": self.attr(3, True), u"a": self.attr(5, True)}
        for text, kind in parts:
            if width <= 0:
                break
            self.put(y, x, text, kinds.get(kind, self.attr(1)), width)
            x += len(text)
            width -= len(text)

    def hline(self, y, title=None):
        h, w = self.scr.getmaxyx()
        line = u"+" + u"-" * (w - 2) + u"+"
        if title:
            t = u" %s " % title
            pos = max(2, (w - len(t)) // 2)
            line = line[:pos] + t + line[pos + len(t):]
        self.put(y, 0, line, self.attr(1))

    def box_sides(self, y0, y1):
        h, w = self.scr.getmaxyx()
        for y in range(y0, y1):
            self.put(y, 0, u"|", self.attr(1))
            self.put(y, w - 1, u"|", self.attr(1))

    # -- tree rows: (path, depth, has_children)
    def tree_rows(self):
        lib = self.lib
        folders = lib.folders
        children = {}
        for f in folders:
            parent = u"/".join(f.split(u"/")[:-1])
            children.setdefault(parent, []).append(f)
        rows = []

        def add(path, depth):
            kids = children.get(path, [])
            rows.append((path, depth, bool(kids)))
            if path in self.expanded:
                for k in kids:
                    add(k, depth + 1)
        add(u"", 0)
        return rows

    # -- the left pane, top to bottom: the library tree, a rule, then the
    # CoGS Catalog Local R/W section. ("tree", path, depth, kids),
    # ("rule",), ("lhead",), ("local", key, label, count, mark). Only tree
    # and local rows take the cursor.
    def left_rows(self):
        rows = [(u"tree", p, d, k) for (p, d, k) in self.tree_rows()]
        rows.append((u"rule",))
        rows.append((u"lhead",))
        rows.extend(self.local_rows())
        return rows

    def local_rows(self):
        loc = self.lib.local
        if not loc.enabled:
            return [(u"local", u"off", u"M  download the catalog (2 GB)", None, u" ")]
        t, i, b, y, n = loc.counts()
        listed = loc.listed()
        a = len(loc.added)
        stamp = (id(listed), len(listed), i, y, n, a)
        if self._lrows and self._lrows[0] == stamp:
            return self._lrows[1]
        rows = [(u"local", u"all", u"All titles", t, u" "),
                (u"local", u"images", u"Disk images", i, u" "),
                (u"local", u"yours", u"Yours (Local)", y, u"*"),
                (u"local", u"newer", u"Newer copy on a2cogs", n, u"!")]
        if a:
            rows.insert(3, (u"local", u"added", u"Added (Local)", a, u"+"))
        for k, name in LOCAL_CATS:
            if k == u"F":
                hit = len([x for x in listed if x.featured is not None])
            else:
                hit = len([x for x in listed if x.cat == k])
            if hit:
                rows.append((u"local", u"cat:" + k, name, hit, u" "))
        rows.append((u"local", u"host:gs", u"GS titles",
                     len([x for x in listed if x.gs_only() or x.gsalso]), u" "))
        rows.append((u"local", u"host:8", u"8-bit titles",
                     len([x for x in listed if not x.gs_only()]), u" "))
        self._lrows = (stamp, rows)
        return rows

    def cur_row(self, rows=None):
        rows = rows or self.left_rows()
        if self.sel >= len(rows):
            self.sel = len(rows) - 1
        while self.sel > 0 and rows[self.sel][0] not in (u"tree", u"local"):
            self.sel -= 1
        return rows[self.sel]

    def move(self, delta):
        rows = self.left_rows()
        i = self.sel + delta
        while 0 <= i < len(rows) and rows[i][0] not in (u"tree", u"local"):
            i += delta
        if 0 <= i < len(rows):
            self.sel = i
            self.item_sel = 0

    def local_entries(self, key):
        """What the right pane lists for a local row: (file, title or None)."""
        loc = self.lib.local
        if key == u"off":
            return []
        listed = loc.listed()
        t, i, b, y, n = loc.counts()
        stamp = (key, self.local_sort, id(listed), len(listed), i, y, n)
        if self._lents and self._lents[0] == stamp:
            return self._lents[1]
        if key in (u"images", u"yours", u"newer"):
            ents = [(f, None) for f, im in local_image_rows(self.lib, key)]
        elif key == u"added":
            ents = [(x.file, x) for x in loc.added_titles()]
        elif key == u"all":
            ents = [(x.file, x) for x in listed]
        elif key.startswith(u"cat:"):
            ents = [(x.file, x) for x in loc.filter(cat=key[4:])]
        else:
            ents = [(x.file, x) for x in loc.filter(host=key[5:])]
        if self.local_sort == u"status":
            rank = {u"newer": 0, u"yours": 1, u"added": 2, u"": 3}
            ents = sorted(ents, key=lambda e: rank[loc.status_of(e[0])])   # stable: A-Z inside
        self._lents = (stamp, ents)
        return ents

    def local_entry_text(self, ent, nw, wide=True):
        fname, t = ent
        loc = self.lib.local
        st = loc.status_of(fname)
        flag = st == u"newer" and u"!" or (st == u"yours" and u"*" or (st == u"added" and u"+" or u" "))
        word = st == u"newer" and u"NEWER" or st
        tail = wide and (u"  " + word) or u""     # narrow panes keep the * / ! flag only
        if t is not None:
            return flag, u"%s %-*s %5d blk%s" % (t.boot and u"B" or u" ", nw, t.name[:nw], t.blocks, tail), st
        im = loc.images.get(fname)
        return flag, u"  %-*s %9s%s" % (nw, fname[:nw], fmt_mb(im and im.bytes or 0), tail), st

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        lib = self.lib
        loc = lib.local
        if self.mode == u"help":
            self.draw_help()
            return
        if self.mode == u"log":
            self.hline(0, u"REQUEST LOG")
            self.box_sides(1, h - 2)
            lines = lib.log_lines[-(h - 3):]
            for i in range(len(lines)):
                self.put(1 + i, 2, lines[i], self.attr(1), w - 4)
            self.hline(h - 2)
            self.put(h - 1, 1, u" L back   Q quit ", self.attr(2))
            return
        # header
        self.hline(0)
        title = u" ] CoGS IMAGE MANAGER  text edition v%s" % VERSION
        self.put(1, 0, u"|", self.attr(1))
        self.put(1, 1, title, self.attr(1, True))
        clock = time.strftime("%a %b %d  %H:%M:%S ")
        self.put(1, w - 1 - len(clock), clock, self.attr(1))
        self.put(1, w - 1, u"|", self.attr(1))
        types = TYPES_WIDE
        if len(types) + 6 > w:
            types = TYPES_NARROW
        self.hline(2, len(types) + 6 <= w and types or None)
        url = lib.base_url_guess()
        self.put(3, 0, u"|", self.attr(1))
        self.put(3, 2, u"Image Source URL: ", self.attr(1))
        self.put(3, 20, url, self.attr(4, True))
        self.put(3, 22 + len(url), u"<- use for image source", self.attr(1))
        self.put(3, w - 1, u"|", self.attr(1))
        self.put(4, 0, u"|", self.attr(1))
        self.put(4, 2, u"Library: %s" % lib.root, self.attr(1), w - 4)
        self.put(4, w - 1, u"|", self.attr(1))
        self.put(5, 0, u"|", self.attr(1))
        nb = len([i for i in lib.disks.images if i.bootable])
        self.put_parts(5, 2, [
            (u"%d" % len(lib.disks.images), u"v"), (u" disk images (", None), (u"%d" % nb, u"v"),
            (u" bootable)   ", None), (u"%d" % len(lib.audio.tracks), u"v"), (u" tracks   ", None),
            (u"%d" % len(lib.pictures.tracks), u"v"), (u" pictures   ", None),
            (u"%d" % len(lib.files), u"v"), (u" files", None)], w - 4)
        self.put(5, w - 1, u"|", self.attr(1))
        self.put(6, 0, u"|", self.attr(1))
        self.put_parts(6, 2, local_status_parts(lib), w - 4)
        self.put(6, w - 1, u"|", self.attr(1))
        self.put(7, 0, u"|", self.attr(1))
        self.put_parts(7, 2, [
            (u"Sharing: ", None), (lib.sharing_state(), lib.sharing and u"v" or u"w"),
            (u"   Access: ", None), (lib.writable and u"writable" or u"read-only", lib.writable and u"v" or u"w"),
            (u"   Requests: ", None), (u"%d" % lib.requests, u"v"),
            (u"   Served: ", None), (fmt_mb(lib.bytes_served), u"v")], w - 4)
        self.put(7, w - 1, u"|", self.attr(1))
        extra = []
        if lib.converting:
            extra.append(u"converting: %s" % lib.converting)
        pend = len(lib.pending_audio) + len(lib.pending_pictures)
        if pend:
            extra.append(u"%d waiting" % pend)
        if lib.audio.skipped:
            extra.append(u"%d audio need a converter (%s)" % (lib.audio.skipped, lib.conv.audio_how()))
        if lib.pictures.skipped:
            extra.append(u"%d pictures need a converter" % lib.pictures.skipped)
        if lib.rejected:
            extra.append(u"%d .dsk/.do/.woz/.nib skipped (ProDOS order only)" % lib.rejected)
        row = 8
        if extra:
            self.put(row, 0, u"|", self.attr(1))
            self.put(row, 2, u"; ".join(extra), self.attr(3), w - 4)
            self.put(row, w - 1, u"|", self.attr(1))
            row += 1
        # panes
        log_h = max(4, min(8, (h - row - 6) // 3))
        detail = []
        if self.confirm and self.confirm[2]:
            import textwrap
            detail = textwrap.wrap(self.confirm[2], max(20, w - 4))
            log_h = min(max(log_h, len(detail)), max(4, h - row - 8))
        body_top = row + 1
        body_bot = h - log_h - 3
        self.hline(row, u"FOLDERS" + (u"  (][ = Apple II category)"))
        self.box_sides(body_top, body_bot)
        tree_w = min(44, max(24, w // 2 - 2))
        for y in range(body_top, body_bot):
            self.put(y, tree_w + 1, u"|", self.attr(1))
        rows = self.left_rows()
        cur = self.cur_row(rows)
        avail = body_bot - body_top
        first = max(0, min(self.sel - avail // 2, len(rows) - avail))
        for i in range(avail):
            idx = first + i
            if idx >= len(rows):
                break
            r = rows[idx]
            sel_attr = (idx == self.sel and self.focus == u"tree") and self.attr(2) or self.attr(1)
            if idx == self.sel and self.focus != u"tree":
                sel_attr = self.attr(1, True)
            if r[0] == u"rule":
                self.put(body_top + i, 2, u"-" * (tree_w - 2), self.attr(1), tree_w - 1)
                continue
            if r[0] == u"lhead":
                if loc.busy():
                    tail = u"working"
                elif not loc.enabled:
                    tail = u"off"
                elif loc.synced:
                    tail = u"synced " + time.strftime(tree_w >= 44 and "%b %d %H:%M" or "%b %d",
                                                      time.localtime(loc.synced))
                else:
                    tail = u"not synced"
                self.put(body_top + i, 2, (u"%s  %s" % (LOCAL_NAME.upper(), tail)),
                         self.attr(1, True), tree_w - 1)
                continue
            if r[0] == u"local":
                key, label, count, mark = r[1], r[2], r[3], r[4]
                text = u"  %s %s" % (mark, label)
                cnt = count is not None and (u"%5d" % count) or u""
                text = text[:tree_w - 8].ljust(tree_w - 8) + cnt
                a = sel_attr
                if key == u"newer" and count and idx != self.sel:
                    a = self.attr(3, True)
                self.put(body_top + i, 2, text, a, tree_w - 1)
                continue
            path, depth, kids = r[1], r[2], r[3]
            d, a, p = lib.counts_under(path)
            name = path and last_component(path) or u"/"
            mark = kids and (path in self.expanded and u"[-]" or u"[+]") or u"   "
            cat = (path and lib.hub.contains(path)) and u"][" or u"  "
            counts = u"I%-4d" % d
            if a:
                counts += u" A%-3d" % a
            if p:
                counts += u" P%-3d" % p
            text = u"%s%s %s" % (u"  " * depth, mark, name)
            text = text[:tree_w - 14].ljust(tree_w - 14) + u" " + cat + u" " + counts
            self.put(body_top + i, 2, text, sel_attr, tree_w - 1)
        # right pane: what the selected row holds
        x0 = tree_w + 3
        iw = w - x0 - 1
        t = time.time()
        if cur[0] == u"local":
            key = cur[1]
            if key == u"off":
                lines = [u"%s keeps the whole a2cogs.com disk" % LOCAL_NAME,
                         u"catalog on this computer. The card lists it",
                         u"as its own row on Browse Disks, with the same",
                         u"categories and Find, and every image is writable.",
                         u"",
                         u"On the card, open Image/Audio Source, scan, pick",
                         u"this computer, then C (or T for all four). Browse",
                         u"Disks shows the row once images are downloaded.",
                         u"",
                         u"About 2 GB. It goes in:",
                         u"  %s" % loc.dir,
                         u"",
                         u"M starts the download."]
                if _json is None:
                    lines.append(u"")
                    lines.append(u"This Python is too old to reach a2cogs.com")
                    lines.append(u"(no TLS 1.2). Copy the folder from a newer")
                    lines.append(u"machine and it is served from here.")
                for i in range(min(avail, len(lines))):
                    self.put(body_top + i, x0, lines[i], self.attr(1), iw)
                self.n_items = 0
            else:
                ents = self.local_entries(key)
                self.n_items = len(ents)
                if self.item_sel >= len(ents):
                    self.item_sel = max(0, len(ents) - 1)
                first_i = max(0, min(self.item_sel - avail // 2, len(ents) - avail))
                # "f a B name blocks blk  status": 16 columns around the
                # name, 7 more for the status word when the pane has room
                wide = iw >= 50
                nw = max(12, iw - (wide and 23 or 16))
                if not ents:
                    self.put(body_top, x0, loc.busy() and u"(downloading...)" or u"(none)", self.attr(1), iw)
                shown = 0
                for i in range(avail):
                    idx = first_i + i
                    if idx >= len(ents):
                        break
                    flag, text, st = self.local_entry_text(ents[idx], nw, wide)
                    k = LOCAL_DISKS + ents[idx][0]
                    act = u"  "
                    if lib.last_write.get(k, 0) > t - 2:
                        act = u"W "
                    elif lib.last_read.get(k, 0) > t - 2:
                        act = u"R "
                    a = self.attr(1)
                    if idx == self.item_sel and self.focus == u"items":
                        a = self.attr(2)
                    elif st == u"newer" or act != u"  ":
                        a = self.attr(3, True)
                    elif st == u"added":
                        a = self.attr(5, True)
                    self.put(body_top + i, x0, u"%s %s%s" % (flag, act, text), a, iw)
                    shown += 1
                if key == u"added" and shown + 3 <= avail:
                    self.put(body_top + shown + 1, x0, u"+ = Added (Local): you put it here.",
                             self.attr(5, True), iw)
                    self.put(body_top + shown + 2, x0, u"    Sync and Delete leave it alone.",
                             self.attr(1), iw)
                if key in (u"yours", u"newer") and shown + 3 <= avail:
                    self.put(body_top + shown + 1, x0, u"! = a2cogs has a newer copy. X resets it.",
                             self.attr(3, True), iw)
                    self.put(body_top + shown + 2, x0, u"* = Yours (Local): written to. Sync leaves it alone.",
                             self.attr(1), iw)
        else:
            sel_path = cur[1]
            items = self.items_in(sel_path, max(12, iw - 24))
            self.n_items = len(items)
            if self.item_sel >= len(items):
                self.item_sel = max(0, len(items) - 1)
            first_i = max(0, min(self.item_sel - avail // 2, len(items) - avail))
            if not items:
                self.put(body_top, x0, u"(no items directly in this folder)", self.attr(1), iw)
            for i in range(avail):
                idx = first_i + i
                if idx >= len(items):
                    break
                kind, text, key = items[idx]
                act = u"  "
                if lib.last_write.get(key, 0) > t - 2:
                    act = u"W "
                elif lib.last_read.get(key, 0) > t - 2:
                    act = u"R "
                a = self.attr(1)
                if idx == self.item_sel and self.focus == u"items":
                    a = self.attr(2)
                elif act != u"  ":
                    a = self.attr(3, True)
                self.put(body_top + i, x0, u"%s %s%s" % (kind, act, text), a, iw)
        # log
        self.hline(body_bot, detail and u"PLEASE READ" or u"LOG")
        lines = detail and detail[:log_h] or lib.log_lines[-(log_h):]
        for i in range(len(lines)):
            self.put(body_bot + 1 + i, 0, u"|", self.attr(1))
            self.put(body_bot + 1 + i, 2, lines[i], detail and self.attr(3, True) or self.attr(1), w - 4)
            self.put(body_bot + 1 + i, w - 1, u"|", self.attr(1))
        self.box_sides(body_bot + 1 + len(lines), h - 2)
        self.hline(h - 2)
        if self.confirm:
            self.put(h - 1, 1, u" %s  Y / N " % self.confirm[0], self.attr(3, True), w - 2)
            return
        # the bar must always end with Quit: pick the longest wording that
        # fits (the full one is 105 columns, an 80-column tty gets the last)
        share = lib.sharing and u"Stop" or u"Start"
        for keys in (
                u" R Rescan  W Writable  B %s sharing  C Category  M Mirror  S Sync  X Reset  D Delete  O Sort  L Log  ? Help  Q Quit " % share,
                u" R Rescan  W Write  B %s  C Categ  M Mirror  S Sync  X Reset  D Delete  O Sort  L Log  ? Help  Q Quit " % share,
                u" R Scan  W Write  B %s  C Cat  M Mirror  S Sync  X Reset  D Delete  O Sort  L Log  ? Q Quit " % share,
                u" R W B C  M Mirror  S Sync  X Reset  D Delete  O Sort  L Log  ? Help  Q Quit ",
                u" R W B C  M Mirror  S Sync  X Reset  D Del  O Sort  L Log  ? Q Quit "):
            if len(keys) <= w - 2:
                break
        self.put(h - 1, 1, keys, self.attr(2), w - 2)

    def items_in(self, folder, nw=30):
        lib = self.lib
        out = []
        for i in lib.disks.images:
            if i.folder == folder:
                tag = i.bootable and u"B" or u" "
                vol = i.volume and (u"/" + i.volume) or u""
                out.append((u"I", u"%s %-*s %5d blk %s" % (tag, nw, i.display[:nw], i.blocks, vol), i.rel_path()))
        for t in lib.audio.tracks:
            if t.folder == folder:
                s = t.secs()
                out.append((u"A", u"  %-*s %3d:%02d" % (nw, t.display[:nw], s // 60, s % 60), u"audio/" + t.served))
        for t in lib.pictures.tracks:
            if t.folder == folder:
                out.append((u"P", u"  %-*s SHR" % (nw, t.display[:nw]), u"pictures/" + t.served))
        if folder == u"":
            for f in lib.files:
                out.append((u"F", u"  %-*s %s" % (nw, f.name[:nw], f.alias), u"files/" + f.name))
        return out

    def draw_help(self):
        h, w = self.scr.getmaxyx()
        self.hline(0, u"HELP")
        self.box_sides(1, h - 2)
        for i in range(len(HELP_LINES)):
            if 1 + i >= h - 2:
                break
            self.put(1 + i, 3, HELP_LINES[i], self.attr(1, HELP_LINES[i].isupper()), w - 5)
        self.hline(h - 2)
        self.put(h - 1, 1, u" any key to return ", self.attr(2))

    # -- CoGS Catalog Local R/W keys
    def ask(self, text, fn, detail=None):
        self.confirm = (text, fn, detail)

    def key_mirror(self):
        loc = self.lib.local
        if loc.busy():
            self.lib.log(loc.job_line())
        elif not loc.enabled:
            self.ask(u"Download the a2cogs disk catalog (about 2 GB) to %s ?" % loc.dir,
                     lambda: loc.start(u"download"))
        else:
            rows = self.left_rows()
            for i in range(len(rows)):
                if rows[i][0] == u"local":
                    self.sel = i
                    self.item_sel = 0
                    self.focus = u"tree"
                    break

    def key_sync(self):
        loc = self.lib.local
        if loc.busy():
            self.ask(u"%s. Stop it?" % loc.job_line(), loc.stop)
        elif not loc.enabled:
            self.lib.log(u"%s is off. M downloads it." % LOCAL_NAME)
        else:
            loc.start(u"sync")
            self.lib.log(u"%s: sync started" % LOCAL_NAME)

    def key_reset(self):
        loc = self.lib.local
        cur = self.cur_row()
        if cur[0] != u"local" or not loc.enabled:
            self.lib.log(u"X resets an image in %s (select it there first)" % LOCAL_NAME)
            return
        if loc.busy():
            self.lib.log(u"busy: %s" % loc.job_line())
            return
        if self.focus == u"items":
            ents = self.local_entries(cur[1])
            if self.item_sel < len(ents):
                fname = ents[self.item_sel][0]
                if loc.status_of(fname) == u"added":
                    self.lib.log(u"%s is one you added: there is no a2cogs copy to reset it to" % fname)
                    return
                self.ask(u"Fetch a fresh copy of %s? Changes made to it are lost." % fname,
                         lambda: loc.start(u"reset", [fname]))
            return
        t, i, b, y, n = loc.counts()
        if not y:
            self.lib.log(u"nothing to reset: no image is marked Yours (Local)")
            return
        self.ask(u"Reset all %d images marked Yours (Local)? Changes made to them are lost." % y,
                 lambda: loc.start(u"reset", u"yours"))

    def key_remove(self):
        """D: delete the whole copy. Two questions, the warning on screen
        for both."""
        loc = self.lib.local
        if loc.busy():
            self.lib.log(u"busy: %s (S stops it)" % loc.job_line())
            return
        if not loc.enabled and not loc.images:
            self.lib.log(u"%s is not on this computer, nothing to delete" % LOCAL_NAME)
            return
        warn = loc.remove_warning()
        i = loc.counts()[1]

        def gone():
            if loc.remove() is None:
                self.lib.log(u"busy: %s" % loc.job_line())
            self.sel = 0
            self.item_sel = 0
            self.focus = u"tree"
            self._lrows = None
            self._lents = None

        def sure():
            self.ask(u"Are you sure? All %d image files will be deleted." % i, gone, warn)
        self.ask(u"Delete %s from this computer?" % LOCAL_NAME, sure, warn)

    def run(self):
        lib = self.lib
        while True:
            self.draw()
            self.scr.refresh()
            try:
                ch = self.scr.getch()
            except KeyboardInterrupt:
                return
            if ch == -1:
                continue
            if self.mode == u"help":
                self.mode = u"main"
                continue
            if self.confirm:
                text, fn = self.confirm[0], self.confirm[1]
                self.confirm = None
                if ch in (ord("y"), ord("Y")):
                    fn()
                continue
            if ch in (ord("q"), ord("Q")):
                return
            if ch in (ord("?"), ord("h"), ord("H")):
                self.mode = u"help"
                continue
            if ch in (ord("l"), ord("L")):
                self.mode = (self.mode == u"log") and u"main" or u"log"
                continue
            if self.mode == u"log":
                continue
            cur = self.cur_row()
            if ch in (ord("r"), ord("R")):
                daemon_thread(lib.rescan, (u"rescan (R)",)).start()
            elif ch in (ord("w"), ord("W")):
                lib.set_writable(not lib.writable)
            elif ch in (ord("b"), ord("B")):
                lib.toggle_sharing()
            elif ch in (ord("p"), ord("P")):
                lib.hub.prefix_subfolder = not lib.hub.prefix_subfolder
                lib.save_cfg()
                lib.log(u"category names %s the subfolder" % (
                    lib.hub.prefix_subfolder and u"carry" or u"do not carry"))
            elif ch in (ord("c"), ord("C")):
                if cur[0] == u"tree" and cur[1]:
                    if not lib.hub.explicit:
                        lib.hub.explicit = True
                    lib.hub.toggle(cur[1])
                    lib.save_cfg()
                    lib.log(u"category %s: %s" % (
                        lib.hub.contains(cur[1]) and u"on" or u"off", cur[1]))
            elif ch in (ord("m"), ord("M")):
                self.key_mirror()
            elif ch in (ord("s"), ord("S")):
                self.key_sync()
            elif ch in (ord("x"), ord("X")):
                self.key_reset()
            elif ch in (ord("d"), ord("D")):
                self.key_remove()
            elif ch in (ord("o"), ord("O")):
                self.local_sort = (self.local_sort == u"name") and u"status" or u"name"
                self.item_sel = 0
                lib.log(u"%s lists sort by %s" % (LOCAL_NAME, self.local_sort))
            elif ch == 9:
                self.focus = (self.focus == u"tree") and u"items" or u"tree"
            elif ch in (curses.KEY_UP, ord("k")):
                if self.focus == u"tree":
                    self.move(-1)
                else:
                    self.item_sel = max(0, self.item_sel - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                if self.focus == u"tree":
                    self.move(1)
                else:
                    self.item_sel = min(self.item_sel + 1, max(0, self.n_items - 1))
            elif ch == curses.KEY_NPAGE and self.focus == u"items":
                self.item_sel = min(self.item_sel + 10, max(0, self.n_items - 1))
            elif ch == curses.KEY_PPAGE and self.focus == u"items":
                self.item_sel = max(0, self.item_sel - 10)
            elif ch in (curses.KEY_RIGHT, 10, 13, curses.KEY_ENTER):
                if cur[0] == u"tree":
                    self.expanded.add(cur[1])
                elif cur[1] == u"off":
                    self.key_mirror()
                else:
                    self.focus = u"items"
            elif ch == curses.KEY_LEFT:
                if self.focus == u"items":
                    self.focus = u"tree"
                elif cur[0] == u"tree":
                    rows = self.left_rows()
                    path = cur[1]
                    if path in self.expanded and path:
                        self.expanded.discard(path)
                    else:
                        parent = u"/".join(path.split(u"/")[:-1])
                        for i in range(len(rows)):
                            if rows[i][0] == u"tree" and rows[i][1] == parent:
                                self.sel = i
                                break
            elif ch == 12 or ch == curses.KEY_RESIZE:
                self.scr.clearok(1)       # Ctrl-L or a resize: repaint everything


def run_tui(lib):
    try:
        import locale
        locale.setlocale(locale.LC_ALL, "")
    except Exception:
        pass

    def body(scr):
        Tui(scr, lib).run()
    curses.wrapper(body)


# ---------------------------------------------------------------- plain

def plain_status(lib):
    out = []
    out.append(u"] CoGS IMAGE MANAGER  text edition v%s" % VERSION)
    out.append(u"")
    out.append(u"  Image Source URL:  %s   <- use for image source" % lib.base_url_guess())
    out.append(u"  Library:           %s" % lib.root)
    nb = len([i for i in lib.disks.images if i.bootable])
    out.append(u"  %d disk images (%d bootable), %d tracks, %d pictures, %d files" % (
        len(lib.disks.images), nb, len(lib.audio.tracks), len(lib.pictures.tracks), len(lib.files)))
    out.append(u"  Sharing: %s   Access: %s   Requests: %d   Served: %s" % (
        lib.sharing_state(), lib.writable and u"writable" or u"read-only", lib.requests, fmt_mb(lib.bytes_served)))
    out.append(u"  Converters: audio %s, pictures %s" % (lib.conv.audio_how(), lib.conv.picture_how()))
    if lib.rejected:
        out.append(u"  %d .dsk/.do/.woz/.nib skipped (the card mounts ProDOS-order images only)" % lib.rejected)
    out.append(u"  " + local_status_line(lib))
    return u"\n".join(out)


def local_status_parts(lib):
    """The CoGS Catalog Local R/W status line as (text, kind) pieces for the
    screen (Tui.put_parts). The line mode prints them joined, no color."""
    loc = lib.local
    if loc.busy():
        return [(u"%s  %s" % (LOCAL_NAME, loc.job_line()), u"w")]
    if not loc.enabled:
        return [(u"%s: " % LOCAL_NAME, None), (u"off", u"w"),
                (u" (M downloads the a2cogs disk catalog, about 2 GB)", None)]
    t, i, b, y, n = loc.counts()
    a = len(loc.added)
    parts = [(u"%s: " % LOCAL_NAME, None), (u"%d" % i, u"v"), (u" images  ", None),
             (u"%d" % t, u"v"), (u" titles  ", None), (fmt_mb(b), u"v"), (u"   ", None),
             (u"%d" % y, u"v"), (u" yours (local)  ", None),
             (u"%d" % n, n and u"w" or u"v"), (u" newer", None)]
    if a:
        parts += [(u"  ", None), (u"%d" % a, u"a"), (u" added", None)]
    parts.append((u"   %s" % loc.dir, None))
    return parts


def local_status_line(lib):
    return u"".join([text for text, kind in local_status_parts(lib)])


def local_image_rows(lib, which):
    """(file, image) rows for the image views: images / yours / newer."""
    loc = lib.local
    out = []
    for fname in sorted(loc.have, key=lower_key):
        im = loc.images.get(fname)
        if im is None:
            continue
        if which == u"yours" and im.state != u"yours":
            continue
        if which == u"newer" and not im.newer:
            continue
        out.append((fname, im))
    return out


def plain_local(lib, arg):
    loc = lib.local
    words = arg.split(None, 1)
    sub = words and words[0].lower() or u""
    rest = len(words) > 1 and words[1].strip() or u""
    if not loc.busy() and not loc.reload_if_changed() and loc.enabled:
        loc.mark_written()                    # written to outside this program
    step = getattr(lib, "remove_step", 0)     # m remove asks twice
    lib.remove_step = 0
    if sub in (u"", u"status"):
        return u"  " + local_status_line(lib)
    if sub in (u"download", u"sync"):
        if not loc.start(loc.enabled and u"sync" or u"download"):
            return u"  already running: %s" % loc.job_line()
        return u"  started; m shows progress, g the log"
    if sub == u"stop":
        loc.stop()
        return u"  stopping after the image in flight"
    if sub == u"reset":
        if not rest:
            return u"  m reset <file>   or   m reset yours"
        target = rest == u"yours" and u"yours" or [rest]
        if target != u"yours" and rest in loc.added:
            return u"  %s is one you added: there is no a2cogs copy to reset it to" % rest
        if target != u"yours" and rest not in loc.images:
            return u"  no such image: %s (m list shows the file names)" % rest
        if not loc.start(u"reset", target):
            return u"  already running: %s" % loc.job_line()
        return u"  fetching a fresh copy from the catalog"
    if sub == u"remove":
        if loc.busy():
            return u"  busy: %s (m stop first)" % loc.job_line()
        if not loc.enabled and not loc.images:
            return u"  %s is not on this computer, nothing to remove" % LOCAL_NAME
        if rest == u"yes" and step == 1:
            lib.remove_step = 2
            return (u"  Are you sure? All %d image files will be deleted.\n"
                    u"  Type  m remove sure  to delete them." % loc.counts()[1])
        if rest == u"sure" and step == 2:
            n = loc.remove()
            if n is None:
                return u"  busy: %s" % loc.job_line()
            return u"  removed: %d image file(s) deleted. m download fetches the catalog again." % n
        lib.remove_step = 1
        import textwrap
        return u"\n".join([u"  " + x for x in textwrap.wrap(loc.remove_warning(), 72)] +
                          [u"  Type  m remove yes  to go on."])
    if sub == u"list" and rest == u"added":
        return u"\n".join([u"  + %-44s %9s  added" % (t.file[:44], fmt_mb(t.bytes))
                           for t in loc.added_titles()]) or u"  (none)"
    if sub == u"list":
        which = rest in (u"yours", u"newer") and rest or u"images"
        rows = local_image_rows(lib, which)
        out = []
        for fname, im in rows:
            flag = im.newer and u"!" or (im.state == u"yours" and u"*" or u" ")
            out.append(u"  %s %-44s %9s  %s" % (flag, fname[:44], fmt_mb(im.bytes),
                                               im.newer and u"NEWER" or (im.state == u"yours" and u"yours" or u"")))
        return u"\n".join(out) or u"  (none)"
    return u"  m [status | download | sync | stop | list [yours|newer|added] | reset <file>|yours | remove]"


def plain_tree(lib):
    out = [u"  /  (I %d)" % lib.counts_under(u"")[0]]
    for f in lib.folders:
        depth = f.count(u"/")
        d, a, p = lib.counts_under(f)
        cat = lib.hub.contains(f) and u" ][" or u""
        extra = (a and (u" A %d" % a) or u"") + (p and (u" P %d" % p) or u"")
        out.append(u"  %s%s  (I %d%s)%s" % (u"  " * (depth + 1), last_component(f), d, extra, cat))
    return u"\n".join(out)


def plain_list(lib, folder):
    out = []
    for i in lib.disks.images:
        if i.folder == folder:
            out.append(u"  I %s %-32s %6d blk  %s" % (i.bootable and u"B" or u" ", i.display[:32], i.blocks,
                                                       i.volume and (u"/" + i.volume) or u""))
    for t in lib.audio.tracks:
        if t.folder == folder:
            out.append(u"  A   %-32s %3d:%02d" % (t.display[:32], t.secs() // 60, t.secs() % 60))
    for t in lib.pictures.tracks:
        if t.folder == folder:
            out.append(u"  P   %-32s SHR" % t.display[:32])
    if folder == u"":
        for f in lib.files:
            out.append(u"  F   %-32s %s" % (f.name[:32], f.alias))
    return u"\n".join(out) or u"  (nothing directly in this folder)"


PLAIN_HELP = u"""  s            status (URL, counts)
  t            folder tree
  l [folder]   list what is in a folder (l Games/Arcade)
  c <folder>   show/hide a folder as an Apple II category
  p            toggle subfolder prefix inside categories
  w            toggle writable / read-only
  b            start / stop sharing
  r            rescan now
  m            CoGS Catalog Local R/W status
  m download   keep the a2cogs disk catalog on this computer (about 2 GB)
  m sync       fetch new and changed titles (yours are left alone)
  m list       its images (m list yours, m list newer)
  m reset F    fetch a fresh copy of image F (m reset yours: all of yours)
  m stop       stop a download
  m remove     delete the local catalog from this computer (it asks twice)
  g            last 20 log lines
  q            quit"""


def write(s):
    """Print one text line; UTF-8 bytes on 2.x so a pipe or an ASCII
    terminal never raises."""
    if PY2:
        sys.stdout.write(tob(s))
    else:
        sys.stdout.write(s)
    sys.stdout.write("\n")
    sys.stdout.flush()


def run_plain(lib):
    write(plain_status(lib))
    write(u"")
    write(u"Type ? for commands.")
    while True:
        try:
            sys.stdout.write("cim> ")
            sys.stdout.flush()
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            return
        if not line:
            return
        line = fs_text(line).strip()
        if not line:
            continue
        cmd, _, arg = line.partition(u" ")
        cmd = cmd.lower()
        raw_arg = arg.strip()
        arg = arg.strip().strip(u"/")
        if cmd in (u"q", u"quit", u"exit"):
            return
        elif cmd in (u"?", u"help", u"h"):
            write(PLAIN_HELP)
        elif cmd == u"s":
            write(plain_status(lib))
        elif cmd == u"t":
            write(plain_tree(lib))
        elif cmd == u"l":
            write(plain_list(lib, arg))
        elif cmd == u"c" and arg:
            lib.hub.toggle(arg)
            lib.save_cfg()
            write(u"  category %s: %s" % (lib.hub.contains(arg) and u"on" or u"off", arg))
        elif cmd == u"p":
            lib.hub.prefix_subfolder = not lib.hub.prefix_subfolder
            lib.save_cfg()
            write(u"  prefix subfolder: %s" % (lib.hub.prefix_subfolder and u"on" or u"off"))
        elif cmd == u"w":
            lib.set_writable(not lib.writable)
            write(u"  share: %s" % (lib.writable and u"writable" or u"read-only"))
        elif cmd == u"b":
            lib.toggle_sharing()
            write(u"  sharing: %s" % lib.sharing_state())
        elif cmd == u"r":
            lib.rescan(u"rescan")
            write(plain_status(lib))
        elif cmd == u"g":
            write(u"\n".join(lib.log_lines[-20:]) or u"  (nothing yet)")
        elif cmd == u"m":
            write(plain_local(lib, raw_arg))
        else:
            write(u"  ? for commands")


# ---------------------------------------------------------------- main

def daemon_thread(target, args=()):
    t = threading.Thread(target=target, args=args)
    if hasattr(t, "daemon"):
        t.daemon = True            # 2.6+ / 3.x
    else:
        t.setDaemon(True)          # 2.5
    return t


def thread_alive(t):
    if hasattr(t, "is_alive"):
        return t.is_alive()
    return t.isAlive()             # 2.5


def main(argv):
    global LIB, HEADLESS
    op = optparse.OptionParser(
        usage="%prog [options] FOLDER",
        version="CoGS Image Manager text edition " + VERSION,
        description="Share a folder of Apple II disk images (plus music and pictures) "
                    "to CoGS cards on the LAN. Same wire contract as the Mac and Windows apps.")
    op.add_option("--port", type="int", default=DEFAULT_PORT, help="TCP port (default %d)" % DEFAULT_PORT)
    op.add_option("--read-only", action="store_true", default=False,
                  help="refuse the card's block writes (default: writable)")
    op.add_option("--no-bonjour", action="store_true", default=False, help="do not announce over Bonjour")
    op.add_option("--name", default=None, help="Bonjour share name (default: this machine's name)")
    op.add_option("--category", action="append", default=[], metavar="FOLDER[=LABEL]",
                  help="show FOLDER as an Apple II category (repeatable; default: every top-level folder)")
    op.add_option("--prefix-subfolder", action="store_true", default=False,
                  help="inside a category, names one level down carry the subfolder")
    op.add_option("--audio-tool", default="auto", metavar="auto|ffmpeg|afconvert|none",
                  help="what turns MP3/M4A/WAV/AIFF into the card's PCM (default: ffmpeg if installed, "
                       "else afconvert on a Mac)")
    op.add_option("--local-dir", default=None, metavar="FOLDER",
                  help="where CoGS Catalog Local R/W is kept (default: \"%s\" beside the library)" % LOCAL_DIR_NAME)
    op.add_option("--local-download", action="store_true", default=False,
                  help="download (or sync) CoGS Catalog Local R/W at start: the whole a2cogs disk "
                       "catalog, about 2 GB, served to the card as its own row")
    op.add_option("--no-auto-sync", action="store_true", default=False,
                  help="do not sync CoGS Catalog Local R/W by itself (at start when the last sync is "
                       "over an hour old, and once a day)")
    op.add_option("--local-url", default=None, help=optparse.SUPPRESS_HELP)
    op.add_option("--plain", action="store_true", default=False, help="line-mode prompt instead of the full-screen UI")
    op.add_option("--headless", action="store_true", default=False, help="no UI; log to stdout (launchd, cron, NAS)")
    opts, args = op.parse_args(argv[1:])
    if len(args) != 1:
        op.print_help()
        return 2
    root = os.path.abspath(fs_text(args[0]))
    if not os.path.isdir(root):
        sys.stderr.write("not a folder: %s\n" % tos(tob(root)))
        return 2
    HEADLESS = bool(opts.headless)
    script_dir = os.path.dirname(os.path.abspath(fs_text(__file__)))
    conv = Converters(script_dir, fs_text(opts.audio_tool))
    name = opts.name and fs_text(opts.name) or (tos(socket.gethostname()).split(u".")[0] or u"CoGS Share")
    lib = Library(root, not opts.read_only, opts.port, name, not opts.no_bonjour, conv,
                  opts.local_dir and os.path.abspath(fs_text(opts.local_dir)) or None,
                  opts.local_url and fs_text(opts.local_url) or None)
    if opts.read_only:
        lib.writable = False          # the flag beats a saved writable=1
    if opts.category:
        lib.hub.cats = []
        for c in opts.category:
            c = fs_text(c)
            if u"=" in c:
                p, l = c.split(u"=", 1)
                lib.hub.cats.append([p.strip(u"/"), l])
            else:
                lib.hub.cats.append([c.strip(u"/"), None])
        lib.hub.explicit = True
    if opts.prefix_subfolder:
        lib.hub.prefix_subfolder = True
    LIB = lib
    lib.rescan(u"scan")
    try:
        srv = Server(("", opts.port), Handler)
    except socket.error:
        e = sys.exc_info()[1]
        sys.stderr.write("cannot listen on port %d: %s\n" % (opts.port, e))
        return 1
    t = daemon_thread(srv.serve_forever)
    t.start()
    lib.log(u"share up on port %d" % opts.port)
    lib.reap_stale_bonjour()
    lib.start_bonjour()
    wt = daemon_thread(lib.watcher)
    wt.start()
    if lib.local.enabled:
        t, i, b, y, n = lib.local.counts()
        lib.log(u"%s: %d titles, %d images, %s in %s" % (LOCAL_NAME, t, i, fmt_mb(b), lib.local.dir))
    lib.auto_sync_on = not opts.no_auto_sync
    if opts.local_download:
        lib.local.start(lib.local.enabled and u"sync" or u"download")
    elif lib.auto_sync_on:
        lib.local.auto_sync(at_open=True)

    # A closed Terminal window (SIGHUP) or a kill (SIGTERM) must take the
    # dns-sd child down with us, or the card keeps finding a dead share.
    # Windows has no SIGHUP (0.2: the tuple below used to name it directly
    # and died before the try; Taciano, Windows 11 / Python 3.14, 11-09).
    def on_signal(signum, frame):
        raise KeyboardInterrupt()
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError, AttributeError):
            pass
    try:
        if HEADLESS:
            write(plain_status(lib))
            while True:
                time.sleep(3600)
        elif opts.plain or curses is None or not sys.stdout.isatty():
            run_plain(lib)
        else:
            run_tui(lib)
    except KeyboardInterrupt:
        pass
    lib.stop_flag = True
    lib.stop_bonjour()
    try:
        srv.shutdown()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
