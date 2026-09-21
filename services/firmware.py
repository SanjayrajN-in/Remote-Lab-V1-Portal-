"""Firmware upload safety checks.

Students upload compiled firmware to the portal, not to the Pi. Every file is
checked here before it is stored or forwarded. The Pi's /flash endpoint has no
validation of its own beyond secure_filename, so this is the only gate.

The checks, cheapest first so an obvious reject costs nothing:

  1. Extension - only .bin .hex .out .elf .uf2
  2. Size - a real microcontroller image is small; anything large is wrong
  3. Content sniff - reject anything that is actually a script, ELF-for-PC,
     archive, or Windows executable wearing a firmware extension
  4. Format sanity - .hex must be valid Intel HEX; .uf2 must have the UF2
     magic. .bin is raw so it cannot be structurally validated, only sized.

"Malicious" for a firmware upload does not mean the same thing as for a
document. A compiled image is opaque bytes; the risk is not a virus signature
but someone smuggling a shell script, a host executable, or a decompression
bomb through a channel that then runs avrdude on it. So the scan is about
*what kind of file this really is*, not virus hashes - though if ClamAV is
installed it is used as a second layer.
"""
import re
import shutil
import subprocess

# Board -> the extensions that make sense for it. A .bin for an Arduino, or a
# .hex for an ESP32, is a sign of a confused or probing upload.
ALLOWED_EXTENSIONS = {".bin", ".hex", ".out", ".elf", ".uf2"}
BOARD_EXTENSIONS = {
    "arduino": {".hex", ".elf"},
    "attiny": {".hex", ".elf"},
    "esp32": {".bin"},
    "esp8266": {".bin"},
    "stm32": {".bin", ".hex", ".elf"},
    # STM32F4-family boards - same toolchain/output shape as "stm32" above.
    "black_pill": {".bin", ".hex", ".elf"},
    "nucleo_f446re": {".bin", ".hex", ".elf"},
    # TI toolchains (Code Composer Studio) commonly produce .out for these,
    # same as tms320f28377s below - confirmed directly in this deployment
    # (sw1.out flashed successfully to a Tiva bench).
    "tiva": {".out", ".bin", ".elf"},
    "msp430": {".hex", ".elf", ".out"},
    "tms320f28377s": {".out", ".hex"},
    "generic": ALLOWED_EXTENSIONS,
}
# services/nodes.py's BOARD_TYPE_CHOICES is the full list of boards this
# deployment supports (shown in admin's Board dropdown) - every key there
# should have an entry above, or board/extension checking is silently
# disabled for it (falls back to ALLOWED_EXTENSIONS, i.e. unrestricted).
# This isn't imported directly to avoid a services/ <-> services/ import
# cycle; keep the two lists in sync by hand.

MAX_BYTES = 8 * 1024 * 1024      # 8 MB - far above any real MCU image
MIN_BYTES = 8                    # empty / near-empty is not firmware

# Byte signatures that must never appear at the start of an upload, whatever
# the extension claims. These are the file types an attacker would smuggle.
_MAGIC_REJECT = {
    b"MZ": "a Windows executable",
    b"PK\x03\x04": "a ZIP archive",
    b"\x1f\x8b": "a gzip archive",
    b"BZh": "a bzip2 archive",
    b"\xfd7zXZ": "an xz archive",
    b"Rar!": "a RAR archive",
    b"\x89PNG": "a PNG image",
    b"\xff\xd8\xff": "a JPEG image",
    b"%PDF": "a PDF document",
    b"\xca\xfe\xba\xbe": "a Java class file",
}

# A script betrays itself in its first line.
_SCRIPT_SIGNS = (b"#!/", b"<?php", b"<script", b"import ", b"#include",
                 b"function ", b"eval(", b"os.system", b"subprocess")

_HEX_LINE = re.compile(rb"^:[0-9A-Fa-f]{10,}$")


class Rejected(Exception):
    """Raised with a message a student can act on.

    `suspicious=True` marks a reason that means the upload wasn't any kind of
    firmware at all - a disguised executable/script/archive, or a real
    malware-scanner hit - as opposed to an honest mistake (wrong extension,
    wrong board, a corrupt/truncated file). blueprints/firmware.py uses this
    to log louder and show the student a firmer policy notice, not just the
    normal "here's what to fix" message."""
    def __init__(self, message, suspicious=False):
        super().__init__(message)
        self.suspicious = suspicious


def _ext(filename):
    filename = (filename or "").lower()
    return filename[filename.rfind("."):] if "." in filename else ""


def _looks_like_elf(head):
    return head[:4] == b"\x7fELF"


def _looks_like_text(data):
    """True if `data` is overwhelmingly printable ASCII with no NUL bytes -
    the profile of a text file (CSV/log/README/source), not a compiled
    binary image, however small. Sampling the first 4 KB is enough: a real
    firmware image has NUL bytes or non-printable content well within that
    window; a text file doesn't suddenly stop being text after 4 KB."""
    sample = data[:4096]
    if b"\x00" in sample:
        return False
    printable = sum(1 for b in sample if 32 <= b <= 126 or b in (9, 10, 13))
    return (printable / len(sample)) > 0.98


def validate(filename, data, board="generic"):
    """Check a firmware upload. Returns the normalised extension, or raises
    Rejected with a reason. Never writes anything."""
    ext = _ext(filename)

    if ext not in ALLOWED_EXTENSIONS:
        raise Rejected(
            f"'{ext or 'no extension'}' isn't an accepted firmware type. "
            f"Upload a compiled image: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")

    allowed_for_board = BOARD_EXTENSIONS.get(board, ALLOWED_EXTENSIONS)
    if ext not in allowed_for_board:
        raise Rejected(
            f"A {ext} file doesn't match a {board} board. "
            f"Expected {', '.join(sorted(allowed_for_board))}.")

    if len(data) < MIN_BYTES:
        raise Rejected("That file is empty or too small to be firmware.")
    if len(data) > MAX_BYTES:
        raise Rejected(f"That file is {len(data) // 1024} KB. Firmware images are "
                       f"far smaller than {MAX_BYTES // (1024 * 1024)} MB - check "
                       f"you uploaded the compiled image, not a project archive.")

    head = data[:64]

    for magic, what in _MAGIC_REJECT.items():
        if data.startswith(magic):
            raise Rejected(f"That file is {what}, not firmware.", suspicious=True)

    # ELF is legitimate for .elf/.out but not for a claimed .bin/.hex/.uf2.
    if _looks_like_elf(head) and ext not in (".elf", ".out"):
        raise Rejected(f"That file is an ELF binary but is named {ext}. "
                       f"If it's really firmware, give it a .elf extension.")

    lowered = head.lstrip()[:32].lower()
    for sign in _SCRIPT_SIGNS:
        if lowered.startswith(sign):
            raise Rejected("That looks like source code or a script, not "
                           "compiled firmware. Upload the built image.", suspicious=True)

    # .bin/.out are opaque - nothing below this validates their structure,
    # so this is the last chance to catch "not actually a binary at all".
    # A CSV, a README, a log file, anything text-shaped clears every check
    # above (none of the _MAGIC_REJECT signatures or _SCRIPT_SIGNS match a
    # plain data/text file) and would otherwise sail straight through to a
    # flash tool. Real compiled machine code is essentially never this
    # uniform - it has NUL bytes (padding, uninitialised sections) and a
    # wide spread of non-printable byte values throughout.
    if ext in (".bin", ".out") and _looks_like_text(data):
        raise Rejected(
            f"That {ext} file looks like plain text (a CSV, a log, a README, "
            f"source code, ...), not compiled firmware. Upload the actual "
            f"compiled image.", suspicious=True)

    # Structural checks for the formats that have structure.
    if ext == ".hex":
        _validate_intel_hex(data)
    elif ext == ".uf2":
        if data[:4] != b"UF2\n" and data[:4] != b"\x55\x46\x32\x0a":
            raise Rejected("That .uf2 file is missing its UF2 signature.")

    _clamav_scan(data)          # second layer, only if ClamAV is present
    return ext


def _validate_intel_hex(data):
    """A .hex must be printable ASCII Intel HEX, and end with an EOF record."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise Rejected("That .hex file isn't valid Intel HEX (it contains "
                       "non-text bytes). It may be a raw .bin renamed to .hex.")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise Rejected("That .hex file is empty.")
    for i, ln in enumerate(lines[:200], 1):     # first 200 lines is plenty
        if not _HEX_LINE.match(ln.encode()):
            raise Rejected(f"Line {i} of that .hex file isn't a valid Intel HEX "
                           f"record. The file may be corrupt.")
    if not lines[-1].upper().startswith(":00000001"):
        raise Rejected("That .hex file has no end-of-file record - it looks "
                       "truncated.")


def _clamav_scan(data):
    """If clamscan is installed, run it. Absent ClamAV is not an error - the
    structural checks above are the primary defence; this is defence in depth."""
    clamscan = shutil.which("clamscan")
    if not clamscan:
        return
    try:
        proc = subprocess.run(
            [clamscan, "--no-summary", "--infected", "-"],
            input=data, capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return          # a scanner that will not run must not block a lab session
    if proc.returncode == 1:            # 1 = infected, 0 = clean, 2 = error
        raise Rejected("That file was flagged by the malware scanner and "
                       "cannot be uploaded.", suspicious=True)
