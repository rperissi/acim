CoGS IMAGE MANAGER, TEXT EDITION  (version @@VER@@)
=================================================

Shares a folder of Apple II disk images, music and pictures to every CoGS
card on your network, from any machine with Python. Same wire protocol as
the Mac and Windows Image Manager apps; the card cannot tell them apart.
Built for the old Mac in the closet: a G4 or G5 on Tiger or Leopard, an
Intel Mac on whatever it was left on, a Raspberry Pi, a NAS, a Windows box.

WHAT IS IN THE ZIP

  CIM Text.app      Mac, 10.13 High Sierra and later: double-click
                    (signed and notarized; Intel and Apple Silicon)
  CIM Text.command  Mac, Tiger 10.4 through 10.12: double-click
  cim.py            the program, one file, Python 2.5 through 3.x
  img2shr.py        picture converter (optional; needs Python 3 + Pillow + numpy)
  cim-text.sh       Linux, BSD, NAS: ./cim-text.sh
  cim-text.bat      Windows: double-click
  README.txt        this file

QUICK START

  Mac:      unzip, double-click "CIM Text" (the app; on Tiger through
            Sierra use "CIM Text.command" instead). A Terminal window
            opens. The first time it asks for your disk image folder:
            drag the folder from the Finder into the window and press
            Return. Or put a folder named "Disk Images" next to the app
            and it uses that.
  Linux:    ./cim-text.sh /path/to/disks
  Windows:  double-click cim-text.bat, type the folder path when asked.
  Anything: python cim.py /path/to/disks

The screen shows the Image Source URL (http://your-ip:8271/). On the
card, Image Source menu, scan: the share appears by your machine's name
(Bonjour on Mac and Linux with avahi). No Bonjour: type the URL in.

PYTHON ON YOUR MACHINE

  Leopard 10.5 through macOS 12.2     already there (2.5 to 2.7)
  Tiger 10.4                           ships 2.3, too old: install Python
                                       2.7.18 from python.org (the
                                       "i386/PPC" installer runs on 10.3+)
  macOS 12.3 and later                 type python3 in Terminal once and
                                       accept the install, or python.org
  Raspberry Pi, Linux, BSD, NAS        python3 from the package manager
  Windows 10, 11                       python.org or the Microsoft Store;
                                       runs in plain command mode (no
                                       curses). pip install windows-curses
                                       for the full screen.

KEYS

  Up/Down, j/k       move          Tab      tree <-> item list
  Right/Enter        open folder   Left     close folder
  C   show/hide this folder as an Apple II category on the card
  P   prefix subfolder names inside categories
  W   writable / read-only (the card's lock icon)
  B   start / stop sharing
  R   rescan now        L  full-screen log       ?  help       Q  quit

COGS CATALOG LOCAL R/W  (needs card firmware 1.42)

  Keeps a copy of the whole a2cogs.com disk catalog on this computer,
  about 2 GB, and shares it writable. The card shows it as its own row
  on Browse Disks, so high scores and saved games stay.

  M   download it (asks first); once it is here, jump to its section
  S   sync now: new and changed titles. Also at start and once a day.
  X   reset: a fresh copy of the selected disk image, or of all yours
  O   sort by name or by status
  D   delete the copy from this computer (asks twice)

  *  Yours (Local): you wrote to it; a sync leaves it alone
  !  a newer copy is on a2cogs (X takes it)
  +  Added (Local): a disk image you put in the folder yourself

  On the card: Image/Audio Source, S to scan, pick this computer, then
  C for CoGS Catalog Local R/W or T for all four.

WHAT IT SERVES

  Disk images   .2mg .po .hdv  (.dsk .do .woz .nib are skipped; the card
                mounts ProDOS-order images only)
  Music         .mp3 .m4a .wav .aif .aiff .flac .ogg, converted to the
                card's 8-bit PCM by ffmpeg if installed, else afconvert
                (every Mac since 10.4). Ready-made .pcm files share as is.
  Pictures      .jpg .png .gif .webp .bmp, converted to SHR by img2shr.py
                (Python 3 with Pillow and numpy; without them only
                ready-made .shr files share)
  Files         a "files" folder for the card's file transfer (both ways)

OPTIONS

  --port N              listen on another port (default 8271)
  --read-only           refuse the card's block writes
  --name NAME           Bonjour share name (default: this machine's name)
  --no-bonjour          do not announce; type the URL on the card
  --category FOLDER[=LABEL]  show FOLDER as a category (repeatable)
  --prefix-subfolder    prefix subfolder names inside categories
  --plain               line-mode UI instead of the full screen
  --headless            no UI; log to stdout (NAS, systemd, screen)
  --local-dir FOLDER    where CoGS Catalog Local R/W is kept (default:
                        "CoGS Catalog Local" beside the library)
  --local-download      start the catalog download at launch
  --no-auto-sync        do not sync the local catalog at start or daily

GETTING THE ZIP ONTO AN OLD MAC

  Tiger and Leopard browsers cannot open a2cogs.com (it needs TLS 1.2).
  Download on a current machine and move the zip over by USB stick,
  file sharing, or a shared folder. Archive Utility on 10.4+ unzips it.

Notes and source: https://a2cogs.com/
