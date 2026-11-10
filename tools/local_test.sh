#!/bin/bash
# CoGS Catalog Local R/W test for cim.py. No network: a stand-in for
# a2cogs.com runs on 127.0.0.1 with a small catalog-all.json and images.
#
# Checked: download, the record (images.tsv), the hub / pages / search the
# card asks for (same JSON as a2cogs.com/disks/), URL rewriting, parts,
# range GET, PUT marks an image yours, sync (new title fetched, untouched
# image replaced, yours image kept and flagged newer), Reset, the
# "cogslocal" announce on the library's own catalog page, and the small
# JSON reader used on Python 2.5 against the real catalog.
#
# Usage: tools/local_test.sh            (prints PASS/FAIL lines)
#        PY=/path/to/python tools/local_test.sh

set -u
cd "$(dirname "$0")/.."

PORT=8296
UP=8295
PY="${PY:-python3}"
DIR=$(mktemp -d /tmp/cogs-local-test.XXXX)
LIBD="$DIR/library"; LOC="$DIR/local"; SITE="$DIR/site"
mkdir -p "$LIBD" "$SITE/disks"
PASS=0; FAIL=0
SRV=""; UPS=""

say()  { printf '%s\n' "$*"; }
ok()   { PASS=$((PASS+1)); say "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); say "FAIL  $1"; }
cleanup() { [ -n "$SRV" ] && kill $SRV 2>/dev/null; [ -n "$UPS" ] && kill $UPS 2>/dev/null; rm -rf "$DIR"; }
trap cleanup EXIT

# --- the stand-in site: catalog + images, ETag = md5 of the bytes ---
cat > "$DIR/upstream.py" <<'EOF'
import sys, os, hashlib
try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
root = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        from urllib.parse import unquote
        p = os.path.join(root, unquote(self.path.split("?")[0]).lstrip("/"))
        if not os.path.isfile(p):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers(); return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("ETag", '"%s"' % hashlib.md5(data).hexdigest())
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
HTTPServer(("127.0.0.1", int(sys.argv[2])), H).serve_forever()
EOF

make_site() {  # make_site <stage>: 1 = first catalog, 2 = the catalog after an update
python3 - "$SITE" "$UP" "$1" <<'EOF'
import sys, os, json
site, port, stage = sys.argv[1], sys.argv[2], int(sys.argv[3])
U = "http://127.0.0.1:%s/disks/" % port
def img(name, blocks, seed):
    b = bytearray(512 * blocks)
    for i in range(0, len(b), 7):
        b[i] = (i * seed + seed) & 0xFF
    open(os.path.join(site, "disks", name), "wb").write(bytes(b))
    return len(b)
vols = []
def add(name, file, blocks, seed, **kw):
    n = img(file, blocks, seed) if not kw.pop("_noimg", False) else 512 * blocks
    e = {"name": name, "bytes": n, "blocks": blocks, "readonly": True, "boot": kw.pop("boot", True),
         "source": "test", "verified": False}
    e.update(kw)
    e["url"] = U + file.replace(" ", "%20")
    vols.append(e)
add("Alpha Game", "Alpha Game.2mg", 280, 3, cat="G", host="gs", featured=2,
    cover="https://a2cogs.com/pictures/Alpha.shr")
if stage != 3:
    add("Beta Eduware", "Beta.po", 280, 5 if stage == 1 else 55, cat="E", host="any")
add("Collection", "Big Collection.hdv", 1600, 7, cat="G", host="any", gsalso=True, featured=1)
add("Game One", "Big Collection.hdv", 1600, 7, cat="G", host="any", _noimg=True, **{"in": "Collection"})
add("Game Two", "Big Collection.hdv", 1600, 7, cat="G", host="any", _noimg=True, **{"in": "Collection"})
if stage != 3:
    add("System Disk", "System.2mg", 1600, 9 if stage == 1 else 99, cat="S", host="gs", boot=True)
img("Two D2.po", 280, 11)
if stage != 3:
    add("Two Disker (2 disks)", "Two D1.po", 280, 13, cat="D", host="gs", parts=[U + "Two%20D2.po"])
if stage == 4:
    add("Clash Title", "Clash.po", 280, 19, cat="G", host="gs")
if stage >= 2:
    add("New Arrival", "New Arrival.po", 280, 17, cat="T", host="gs", boot=False)
vols.sort(key=lambda e: e["name"].lower())
json.dump({"catalog": "test", "note": "x", "volumes": vols}, open(os.path.join(site, "catalog-all.json"), "w"), indent=1)
EOF
}

start_cim() {  # start_cim [extra args...]
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --headless --local-dir "$LOC" \
        --local-url "http://127.0.0.1:$UP/catalog-all.json" "$@" >"$DIR/cim.log" 2>&1 &
    SRV=$!
    for i in $(seq 1 40); do
        curl -s -o /dev/null "http://127.0.0.1:$PORT/cogs-volumes.json" && break
        sleep 0.25
    done
}
stop_cim() { kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; SRV=""; }
wait_log() {  # wait_log <text>: until the log shows it (the job finished)
    for i in $(seq 1 80); do
        grep -q "$1" "$DIR/cim.log" && return 0
        sleep 0.25
    done
    return 1
}
jq_py() {  # jq_py <python expr over j> reads JSON on stdin, exits 0 when the assert holds
    python3 -c 'import json,sys
j = json.load(sys.stdin)
'"$1"
}

B="http://127.0.0.1:$PORT"
L="$B/cogs-local"

say "interpreter: $($PY --version 2>&1)"
make_site 1
python3 "$DIR/upstream.py" "$SITE" $UP &
UPS=$!
sleep 0.5
python3 - "$LIBD" <<'EOF'
import sys, os
open(os.path.join(sys.argv[1], "Mine.po"), "wb").write(bytes(bytearray(512 * 280)))
EOF

# --- off: nothing announced, nothing served ---
start_cim
curl -s "$B/cogs-volumes.json" | grep -q cogslocal && bad "no announce while off" || ok "no announce while off"
curl -s "$L/cogs-hub.json" | jq_py 'assert j["total"] == 0 and j["cats"] == []' \
    && ok "hub is empty while off" || bad "hub while off"
stop_cim

# --- download ---
start_cim --local-download
wait_log "titles, 6 images" && ok "download finished (6 images)" || bad "download: $(cat "$DIR/cim.log")"
[ -f "$LOC/catalog.json" ] && [ -f "$LOC/images.tsv" ] && ok "folder: catalog.json and images.tsv" || bad "folder files"
cmp -s "$SITE/disks/Big Collection.hdv" "$LOC/disks/Big Collection.hdv" && ok "image bytes match" || bad "image bytes"
[ "$(grep -vc '^#' "$LOC/images.tsv")" = 6 ] && ok "record has one line per image" || bad "record lines: $(cat "$LOC/images.tsv")"
grep -q "	ok	" "$LOC/images.tsv" && ! grep -q "	yours	" "$LOC/images.tsv" && ok "all images start as ok" || bad "initial state"

curl -s "$B/cogs-volumes.json" | grep -q '"total": 1, "cogslocal": 7' && ok "library page announces cogslocal 7" \
    || bad "announce: $(curl -s "$B/cogs-volumes.json" | head -c 200)"
curl -s "$B/cogs-volumes.json" | jq_py 'assert len(j["volumes"]) == 1 and j["volumes"][0]["name"] == "Mine"' \
    && ok "library list holds only the library (local stays apart)" || bad "library list"

H=$(curl -s "$L/cogs-hub.json")
echo "$H" | jq_py '
assert (j["total"], j["total8"], j["totalgs"]) == (7, 4, 4), (j["total"], j["total8"], j["totalgs"])
c = dict((x["k"], (x["n"], x["n8"])) for x in j["cats"])
assert c == {"F": (2, 1), "G": (4, 3), "E": (1, 1), "S": (1, 0), "D": (1, 0)}, c
assert [x["k"] for x in j["cats"]] == ["F", "G", "E", "S", "D"]
' && ok "hub totals and category counts (a2cogs rules)" || bad "hub: $H"
echo "$H" | tr -d '\n' | grep -q '"k".*"name".*"n".*"n8"' && ok "hub field order k, name, n, n8" || bad "hub order"

P=$(curl -s "$L/cogs-volumes.json")
echo "$P" | jq_py '
assert j["pages"] == 1 and j["total"] == 7
v = dict((e["name"], e) for e in j["volumes"])
assert [e["name"] for e in j["volumes"]] == sorted(v, key=str.lower)
a = v["Alpha Game"]
assert a["url"] == "http://127.0.0.1:'$PORT'/cogs-local/disks/Alpha%20Game.2mg", a["url"]
assert a["readonly"] is False and a["boot"] is True and a["cat"] == "G" and a["host"] == "gs"
assert a["cover"] == "https://a2cogs.com/pictures/Alpha.shr"
assert a["bytes"] == 143360 and a["blocks"] == 280
g = v["Game One"]
assert g["in"] == "Collection" and g["url"].endswith("/cogs-local/disks/Big%20Collection.hdv")
t = v["Two Disker (2 disks)"]
assert t["parts"] == ["http://127.0.0.1:'$PORT'/cogs-local/disks/Two%20D2.po"], t
' && ok "A-Z page: rows, rewritten URLs, in, parts, cover" || bad "page: $P"
echo "$P" | grep -q '"name".*"url"' && ok "row field order name before url" || bad "row order"

S=$(curl -s "$L/search?q=&p=1&cat=F")
echo "$S" | jq_py 'assert j["count"] == 2 and [e["name"] for e in j["volumes"]] == ["Collection", "Alpha Game"]' \
    && ok "cat=F is the featured rank order" || bad "featured: $S"
curl -s "$L/search?q=&p=1&cat=G" | jq_py 'assert j["count"] == 4' && ok "cat=G" || bad "cat=G"
curl -s "$L/search?q=&p=1&host=8" | jq_py 'assert j["count"] == 4 and all(e["host"] == "any" for e in j["volumes"])' \
    && ok "host=8 drops the GS-only titles" || bad "host=8"
curl -s "$L/search?q=&p=1&host=gs" | jq_py 'assert sorted(e["name"] for e in j["volumes"]) == ["Alpha Game", "Collection", "System Disk", "Two Disker (2 disks)"]' \
    && ok "host=gs is GS titles plus gsalso" || bad "host=gs"
curl -s "$L/search?q=game&p=1" | jq_py 'assert j["count"] == 3 and "bytes" not in j["volumes"][0]' \
    && ok "q= matches names (search rows carry blocks, no bytes)" || bad "q="
curl -s "$L/search?q=game&p=1&cat=G&host=8" | jq_py 'assert j["count"] == 2' && ok "q, cat and host combine" || bad "combine"
curl -s "$L/search?q=zzz&p=1" | grep -q '"count": 0' && ok "no-hit search is an empty list" || bad "no-hit"

# --- image GET ---
curl -s -o "$DIR/r.bin" -H "Range: bytes=1024-1535" "$L/disks/Alpha%20Game.2mg"
cmp -s <(dd if="$SITE/disks/Alpha Game.2mg" bs=1 skip=1024 count=512 2>/dev/null) "$DIR/r.bin" \
    && ok "range GET of a local image" || bad "range GET"
curl -s -D- -o /dev/null -H "Range: bytes=0-511" "$L/disks/System.2mg" | tr -d '\r' | grep -q "X-CoGS-Writable: 1" \
    && ok "image replies carry X-CoGS-Writable: 1" || bad "writable header"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$L/disks/Nope.po")" = 404 ] && ok "unknown image 404" || bad "unknown image"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$L/disks/..%2Fimages.tsv")" = 404 ] && ok "traversal refused" || bad "traversal"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$B/cogs-local/images.tsv")" = 404 ] && ok "the record is not served" || bad "record served"

# --- a write makes it yours ---
python3 -c 'import sys; sys.stdout.buffer.write(bytes([0xA5]) * 512)' > "$DIR/blk.bin"
C=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Content-Range: bytes 1024-1535/*" --data-binary @"$DIR/blk.bin" "$L/disks/System.2mg")
[ "$C" = 200 ] && ok "PUT a block into a local image (200)" || bad "PUT: $C"
cmp -s <(dd if="$LOC/disks/System.2mg" bs=512 skip=2 count=1 2>/dev/null) "$DIR/blk.bin" && ok "the block is in the file" || bad "block bytes"
grep -q "^System.2mg	.*	yours	" "$LOC/images.tsv" && ok "first write marks the image yours in the record" || bad "yours: $(grep System "$LOC/images.tsv")"
[ "$(grep -c '	yours	' "$LOC/images.tsv")" = 1 ] && ok "only that image is yours" || bad "yours count"
C=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Content-Range: bytes 819200-819711/*" --data-binary @"$DIR/blk.bin" "$L/disks/System.2mg")
[ "$C" = 416 ] && ok "a write past the end is refused (416)" || bad "grow: $C"
stop_cim

# --- written to behind our back (Finder): mtime moved, so it is yours ---
python3 - "$LOC/disks/Two D1.po" <<'EOF'
import os, sys, time
p = sys.argv[1]
f = open(p, "r+b"); f.seek(4096); f.write(b"finder"); f.close()
os.utime(p, (time.time() + 5, time.time() + 5))
EOF

# --- sync against an updated catalog ---
make_site 2      # Beta.po and System.2mg changed upstream, New Arrival added
start_cim --local-download
wait_log "titles, 7 images" && ok "sync finished (7 images)" || bad "sync: $(cat "$DIR/cim.log")"
grep -q "^Two D1.po	.*	yours	" "$LOC/images.tsv" && ok "an image changed on disk between runs is yours" || bad "mtime yours"
cmp -s "$SITE/disks/New Arrival.po" "$LOC/disks/New Arrival.po" && ok "new title fetched" || bad "new title"
cmp -s "$SITE/disks/Beta.po" "$LOC/disks/Beta.po" && ok "untouched image replaced by the newer copy" || bad "replace untouched"
cmp -s "$SITE/disks/System.2mg" "$LOC/disks/System.2mg" && bad "yours image was replaced" || ok "yours image left alone"
grep -q "^System.2mg	.*	yours	.*	1$" "$LOC/images.tsv" && ok "yours image flagged: newer copy on a2cogs" || bad "newer flag: $(grep System "$LOC/images.tsv")"
grep -q "^Beta.po	.*	ok	.*	0$" "$LOC/images.tsv" && ok "replaced image is ok, not flagged" || bad "replaced state"
grep -q "1 updated, 1 newer copy of images marked yours" "$DIR/cim.log" && ok "sync log line counts updates and newer copies" || bad "log: $(grep -i local "$DIR/cim.log")"
curl -s "$L/cogs-hub.json" | jq_py 'assert j["total"] == 8' && ok "hub follows the new catalog (8 titles)" || bad "hub after sync"
stop_cim

# --- reset (line mode): yours images take the catalog copy again ---
(sleep 1; echo "m list newer"; echo "m reset yours"; sleep 4; echo "m"; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --local-dir "$LOC" \
    --local-url "http://127.0.0.1:$UP/catalog-all.json" >"$DIR/plain.log" 2>&1
grep -q "! System.2mg .*NEWER" "$DIR/plain.log" && ok "m list newer shows the flagged image" || bad "list newer: $(cat "$DIR/plain.log")"
cmp -s "$SITE/disks/System.2mg" "$LOC/disks/System.2mg" && ok "reset took the catalog copy" || bad "reset bytes"
cmp -s "$SITE/disks/Two D1.po" "$LOC/disks/Two D1.po" && ok "reset yours covers every yours image" || bad "reset all"
grep -q "	yours	" "$LOC/images.tsv" && bad "still yours after reset" || ok "nothing is yours after reset"
grep -q "0 yours (local)  0 newer" "$DIR/plain.log" && ok "status line after reset" || bad "status: $(tail -3 "$DIR/plain.log")"

# --- the small JSON reader (Python 2.5 has no json) against the real catalog ---
REAL=../../site/disks/catalog-all.json
if [ -f "$REAL" ]; then
    $PY - "$REAL" <<'EOF' && ok "small JSON reader matches json on catalog-all.json" || bad "small JSON reader"
import sys, json, io
try:
    import importlib.util as u
    spec = u.spec_from_file_location("cim", "cim.py")
    cim = u.module_from_spec(spec)
    spec.loader.exec_module(cim)
except ImportError:
    import imp
    cim = imp.load_source("cim", "cim.py")
raw = io.open(sys.argv[1], encoding="utf-8").read()
want = json.loads(raw)
got = cim.json_loads(raw, force_small=True)
assert got == want, "small reader differs"
assert len([cim.LocalTitle(v) for v in got[u"volumes"]]) == len(want["volumes"])
assert cim.json_loads(u'{"a": [1, -2.5e1, true, false, null, "x\\"y\\u0041\\n"], "b": {}}', True) == \
    {u"a": [1, -25.0, True, False, None, u'x"yA\n'], u"b": {}}
EOF
fi


# --- titles a2cogs dropped: untouched images go at the next sync, yours stay ---
hub_total() { curl -s "$L/cogs-hub.json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'; }
python3 - "$LOC/disks/System.2mg" <<'EOF'
import os, sys, time
p = sys.argv[1]
f = open(p, "r+b"); f.seek(4096); f.write(b"mine"); f.close()
os.utime(p, (time.time() + 9, time.time() + 9))
EOF
make_site 3      # Beta Eduware, System Disk and Two Disker are gone from the catalog
start_cim --local-download
wait_log "3 removed (no longer in the catalog)" && ok "sync removes the images of dropped titles (3)" || bad "prune: $(cat "$DIR/cim.log")"
[ ! -f "$LOC/disks/Beta.po" ] && [ ! -f "$LOC/disks/Two D1.po" ] && [ ! -f "$LOC/disks/Two D2.po" ] && ok "untouched images of dropped titles are deleted, parts too" || bad "dropped images still on disk"
grep -q "^Beta.po	\|^Two D" "$LOC/images.tsv" && bad "dropped rows still in the record" || ok "their rows leave the record"
[ -f "$LOC/disks/System.2mg" ] && grep -q "^System.2mg	.*	yours	" "$LOC/images.tsv" && ok "a yours image of a dropped title stays" || bad "yours image of a dropped title: $(grep System "$LOC/images.tsv")"
grep -q "no longer in the CoGS catalog: System Disk (yours kept" "$DIR/cim.log" && ok "the log says the title was dropped and the yours copy kept" || bad "kept log: $(cat "$DIR/cim.log")"
[ "$(hub_total)" = 6 ] && ok "its title stays listed (5 in the catalog, 1 kept)" || bad "hub with a kept title: $(hub_total)"
grep -q '"System Disk"' "$LOC/kept.json" && ok "kept.json holds the dropped title's entry" || bad "kept.json"
stop_cim
start_cim
[ "$(hub_total)" = 6 ] && ok "the kept title is still listed after a restart" || bad "kept after restart: $(hub_total)"
# an image deleted from the folder by hand leaves the list, and the next sync pulls it down
rm "$LOC/disks/Alpha Game.2mg"
T=""; for i in $(seq 1 48); do T=$(hub_total); [ "$T" = 5 ] && break; sleep 0.25; done
[ "$T" = 5 ] && ok "an image deleted from the folder leaves the list" || bad "deleted by hand still listed: $T"
stop_cim
start_cim --local-download
wait_log "sync: 1 image(s) fetched" && cmp -s "$SITE/disks/Alpha Game.2mg" "$LOC/disks/Alpha Game.2mg" && ok "the next sync pulls a hand-deleted image down again" || bad "refetch: $(cat "$DIR/cim.log")"
stop_cim
# Reset on a kept title lets it go
(sleep 1; echo "m reset yours"; sleep 4; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --local-dir "$LOC" \
    --local-url "http://127.0.0.1:$UP/catalog-all.json" >"$DIR/plain2.log" 2>&1
[ ! -f "$LOC/disks/System.2mg" ] && [ ! -f "$LOC/kept.json" ] && ok "Reset on a dropped title removes the yours copy and its listing" || bad "reset of a kept title: $(cat "$DIR/plain2.log")"
grep -q "^System.2mg	" "$LOC/images.tsv" && bad "kept row left after reset" || ok "its row leaves the record"
# a catalog that would take over a quarter of the copy takes nothing
python3 - "$LOC" <<'EOF'
import os, sys, io
loc = sys.argv[1]
rows = []
for i in range(10):
    n = "Junk%02d.po" % i
    p = os.path.join(loc, "disks", n)
    open(p, "wb").write(b"x" * 512)
    rows.append(u"%s\thttp://127.0.0.1:1/disks/%s\t512\t\tok\t%d\t0\n" % (n, n, int(os.stat(p).st_mtime)))
io.open(os.path.join(loc, "images.tsv"), "a", encoding="utf-8").write(u"".join(rows))
EOF
start_cim --local-download
wait_log "none were removed" && [ -f "$LOC/disks/Junk00.po" ] && [ -f "$LOC/disks/Junk09.po" ] && ok "a sync that would remove over a quarter of the copy removes nothing" || bad "guard: $(cat "$DIR/cim.log")"
stop_cim
rm -f "$LOC"/disks/Junk*.po
make_site 2
start_cim --local-download
wait_log "titles, 7 images" && ok "back on the full catalog (7 images)" || bad "back to stage 2: $(cat "$DIR/cim.log")"
grep -q "^Junk" "$LOC/images.tsv" && bad "rows of hand-deleted images left" || ok "rows of images deleted by hand leave the record"
stop_cim


# --- images the user puts in the folder themselves: listed, marked added, left alone ---
start_cim
python3 - "$LOC/disks" "$DIR" <<'EOF'
import os, sys
d, work = sys.argv[1], sys.argv[2]
b = bytearray(512 * 280)
for i in range(0, len(b), 5):
    b[i] = (i * 3 + 1) & 0xFF
for p in (os.path.join(d, "My.Own_Game.po"), os.path.join(d, "Clash.po"), os.path.join(work, "mine0.bin")):
    open(p, "wb").write(bytes(b))
open(os.path.join(d, "notes.txt"), "wb").write(b"not an image")
open(os.path.join(d, "Old Floppy.dsk"), "wb").write(bytes(bytearray(143360)))
open(os.path.join(work, "blk2.bin"), "wb").write(bytes(bytearray([0x5A]) * 512))
EOF
T=""; for i in $(seq 1 48); do T=$(hub_total); [ "$T" = 10 ] && break; sleep 0.25; done
[ "$T" = 10 ] && ok "disk images put in the folder are listed (8 + 2)" || bad "added not listed: $T"
curl -s "$L/search?q=own&p=1" | jq_py 'e = j["volumes"]; assert j["count"] == 1 and e[0]["name"] == "My Own Game" and e[0]["blocks"] == 280 and e[0]["host"] == "any" and e[0]["url"].endswith("/cogs-local/disks/My.Own_Game.po")' \
    && ok "an added image is named from its file and found by search" || bad "added search: $(curl -s "$L/search?q=own&p=1")"
curl -s "$L/search?q=own&p=1&host=gs" | jq_py 'assert j["count"] == 1' && curl -s "$L/search?q=own&p=1&host=8" | jq_py 'assert j["count"] == 1' \
    && ok "an added image lists for both the GS and 8-bit machines" || bad "added host filter"
curl -s "$L/search?q=floppy&p=1" | jq_py 'assert j["count"] == 0' && curl -s "$L/search?q=notes&p=1" | jq_py 'assert j["count"] == 0' \
    && ok "a .dsk and a text file in the folder are not listed" || bad "non-images listed"
curl -s -o "$DIR/got.bin" "$L/disks/My.Own_Game.po" && cmp -s "$DIR/got.bin" "$DIR/mine0.bin" && ok "an added image is served" || bad "added GET"
C=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Content-Range: bytes 1024-1535/*" --data-binary @"$DIR/blk2.bin" "$L/disks/My.Own_Game.po")
[ "$C" = 200 ] && ok "an added image takes writes (200)" || bad "added PUT: $C"
grep -q "^My.Own_Game.po	" "$LOC/images.tsv" && bad "an added image got a row in the record" || ok "an added image is not in the record"
curl -s "$B/" | grep -q "2 added" && ok "index page counts 2 added" || bad "index added: $(curl -s "$B/" | grep -o 'newer[^<]*')"
cp "$LOC/disks/My.Own_Game.po" "$DIR/mine1.bin"
stop_cim
make_site 4      # the catalog gains a title whose file is named like one already in the folder
start_cim --local-download
wait_log "10 titles, 8 images" && ok "sync finished with the added image listed" || bad "sync with added: $(cat "$DIR/cim.log")"
cmp -s "$LOC/disks/My.Own_Game.po" "$DIR/mine1.bin" && ok "sync leaves an added image alone" || bad "added image changed by sync"
cmp -s "$LOC/disks/Clash.po" "$DIR/mine0.bin" && grep -q "Clash.po was already in the folder" "$DIR/cim.log" && ok "a file already in the folder under a catalog name is not overwritten" || bad "clash: $(cat "$DIR/cim.log")"
grep -q "^Clash.po	.*	yours	" "$LOC/images.tsv" && ok "that file is Yours (Local) from then on" || bad "clash row: $(grep Clash "$LOC/images.tsv")"
stop_cim
rm "$LOC/disks/Clash.po" "$LOC/disks/notes.txt" "$LOC/disks/Old Floppy.dsk" "$LOC/disks/My.Own_Game.po"
make_site 2
start_cim --local-download
wait_log "8 titles, 7 images" && [ ! -f "$LOC/kept.json" ] && ok "back on the full catalog, nothing added" || bad "after added: $(cat "$DIR/cim.log")"
stop_cim


# --- sync by itself: at start when the last sync is over an hour old ---
old_stamp() { python3 - "$LOC/images.tsv" <<'PYEOF'
import sys, time, io
p = sys.argv[1]
L = io.open(p, encoding="utf-8").read().split("\n")
L = [l.startswith("#synced ") and "#synced %d" % (time.time() - 7200) or l for l in L]
io.open(p, "w", encoding="utf-8").write("\n".join(L))
PYEOF
}
start_cim
sleep 1.5
grep -q "checking a2cogs" "$DIR/cim.log" && bad "synced at start with a fresh last sync" || ok "no sync at start when the last one is under an hour old"
stop_cim
old_stamp
start_cim
wait_log "sync: 0 image(s) fetched" && grep -q "checking a2cogs for new titles" "$DIR/cim.log" && ok "a start over an hour after the last sync syncs by itself" || bad "sync at start: $(cat "$DIR/cim.log")"
[ ! -f "$LOC/.working" ] && ok "the folder lock is gone when the job ends" || bad "lock left behind"
stop_cim
old_stamp
start_cim --no-auto-sync
sleep 1.5
grep -q "checking a2cogs" "$DIR/cim.log" && bad "--no-auto-sync still synced" || ok "--no-auto-sync leaves it to the S key"
stop_cim

# --- once a day while running: the stamp turns a day old under a running manager ---
stamp_age() { python3 - "$LOC/images.tsv" "$1" <<'PYEOF'
import sys, time, io
p = sys.argv[1]
L = io.open(p, encoding="utf-8").read().split("\n")
L = [l.startswith("#synced ") and "#synced %d" % (time.time() - int(sys.argv[2])) or l for l in L]
io.open(p, "w", encoding="utf-8").write("\n".join(L))
PYEOF
}
stamp_age 60
start_cim
sleep 1.5
stamp_age 90000
wait_log "daily check for new titles" && wait_log "sync: 0 image(s) fetched" && ok "a running manager syncs once the last sync is a day old" || bad "daily sync: $(cat "$DIR/cim.log")"
stop_cim

# --- one Image Manager at a time works on the folder ---
HOSTN=$(python3 -c 'import socket; print(socket.gethostname())')
printf '%s\n%s\n' $$ "$HOSTN" > "$LOC/.working"
start_cim --local-download
wait_log "another Image Manager is working" && ok "a sync is refused while another manager holds the folder" || bad "lock held: $(cat "$DIR/cim.log")"
stop_cim
[ -f "$LOC/.working" ] && ok "a live manager's lock is left alone" || bad "live lock removed"
(exit 0) & DEAD=$!; wait $DEAD
printf '%s\n%s\n' $DEAD "$HOSTN" > "$LOC/.working"
start_cim --local-download
wait_log "sync: 0 image(s) fetched" && [ ! -f "$LOC/.working" ] && ok "a lock left by a manager that is gone is taken over" || bad "stale lock: $(cat "$DIR/cim.log")"
stop_cim

# --- the record written by another manager is picked up while running ---
mark_other() { python3 - "$LOC/images.tsv" <<'PYEOF'
import sys, io
p = sys.argv[1]
L = io.open(p, encoding="utf-8").read().split("\n")
for i, l in enumerate(L):
    c = l.split("\t")
    if c[0] == "Two D2.po":
        c[4] = "yours"
        L[i] = "\t".join(c)
io.open(p, "w", encoding="utf-8").write("\n".join(L))
PYEOF
}
(sleep 1; mark_other; echo "m list yours"; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --local-dir "$LOC" >"$DIR/other.log" 2>&1
grep -q "\* Two D2.po .*yours" "$DIR/other.log" && ok "yours marked by another manager shows here" || bad "other manager's record: $(cat "$DIR/other.log")"

# --- written to while this program runs, by something else (another Image
# Manager on the same folder): the next look at the copy marks it yours ---
(sleep 1; touch -t 203001010000 "$LOC/disks/Two D1.po"; echo "m list yours"; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --local-dir "$LOC" >"$DIR/behind.log" 2>&1
grep -q "\* Two D1.po .*yours" "$DIR/behind.log" && ok "an image changed while running is marked yours" || bad "changed while running: $(cat "$DIR/behind.log")"

# --- remove (line mode): asks twice, then the copy is gone ---
echo keep > "$LOC/disks/not ours.txt"
NB=$(ls "$LOC/disks" | wc -l | tr -d ' ')
(sleep 1; echo "m remove"; echo "m remove sure"; echo "m remove yes"; echo "m remove"; echo "m"; echo "m remove yes"; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --local-dir "$LOC" >"$DIR/remove1.log" 2>&1
[ -f "$LOC/catalog.json" ] && [ "$(ls "$LOC/disks" | wc -l | tr -d ' ')" = "$NB" ] && ok "m remove out of order deletes nothing" || bad "remove out of order: $(cat "$DIR/remove1.log")"
grep -q "download all of it again" "$DIR/remove1.log" && grep -q "Type  m remove yes" "$DIR/remove1.log" && ok "m remove prints the warning first" || bad "remove warning: $(cat "$DIR/remove1.log")"
(sleep 1; echo "m remove"; echo "m remove yes"; echo "m remove sure"; echo "m"; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --local-dir "$LOC" >"$DIR/remove2.log" 2>&1
grep -q "Are you sure" "$DIR/remove2.log" && ok "m remove yes asks again" || bad "second question: $(cat "$DIR/remove2.log")"
[ ! -f "$LOC/catalog.json" ] && [ ! -f "$LOC/images.tsv" ] && ok "remove deletes the catalog and the record" || bad "remove left: $(ls "$LOC")"
[ "$(ls "$LOC/disks")" = "not ours.txt" ] && ok "remove deletes every image and leaves a file that is not ours" || bad "disks after remove: $(ls "$LOC/disks")"
grep -q "off (M downloads" "$DIR/remove2.log" && ok "status after remove: off" || bad "status after remove: $(tail -3 "$DIR/remove2.log")"

# --- Remove leaves the images the user added ---
mkdir -p "$LOC/disks"
rm -f "$LOC/disks/not ours.txt"
cp "$DIR/mine0.bin" "$LOC/disks/Keep Me.po"
start_cim --local-download
wait_log "9 titles, 7 images" && ok "a first download lists an image that was already in the folder" || bad "download over added: $(cat "$DIR/cim.log")"
stop_cim
(sleep 1; echo "m remove"; echo "m remove yes"; echo "m remove sure"; echo q) | \
    $PY cim.py "$LIBD" --port $PORT --no-bonjour --plain --no-auto-sync --local-dir "$LOC" >"$DIR/remove3.log" 2>&1
tr '\n' ' ' < "$DIR/remove3.log" | tr -s ' ' | grep -q "The 1 image you added to the folder stays where it is" && ok "the remove warning says the added image stays" || bad "remove warning with added: $(cat "$DIR/remove3.log")"
[ "$(ls "$LOC/disks")" = "Keep Me.po" ] && [ ! -f "$LOC/catalog.json" ] && cmp -s "$LOC/disks/Keep Me.po" "$DIR/mine0.bin" && ok "remove leaves the added image in the folder" || bad "after remove with added: $(ls "$LOC" "$LOC/disks")"

say ""
say "$PASS passed, $FAIL failed"
[ $FAIL = 0 ]
