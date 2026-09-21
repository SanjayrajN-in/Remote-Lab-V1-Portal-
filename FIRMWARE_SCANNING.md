# Firmware upload scanning — how it's implemented and how it works

Reference doc for the firmware-scanning subsystem covered in [HANDOFF_v6.md](HANDOFF_v6.md)'s
"firmware upload gateway" item. This is the detailed version: every layer, every file, how to
verify it's actually working, and what it doesn't cover.

## Why this exists

Before v6, a student's browser could reach a Lab Pi's own `/flash` endpoint more or less
directly. The Pi's own `/flash` does **no validation beyond `secure_filename` on the filename**
— it will happily run its flash tool (`avrdude`/`esptool`/`lm4flash`/...) on whatever bytes it's
given. So the portal is the only place anything gets checked, and design follows from that: the
browser can never reach a Lab Pi directly for anything (serial, camera, firmware, all of it) —
see [HANDOFF_v7.md](HANDOFF_v7.md) for the rest of that boundary. Firmware is the one case where
that boundary is also a security control, not just an architectural one: a lab network reachable
from student laptops, feeding an upload straight into a shell command on embedded Linux, is a
real path to code execution on the Lab Pi itself if nothing stands in the way.

## The data flow

```
Student's browser
      │  POST /session/<key>/firmware   (multipart: firmware file, board)
      ▼
blueprints/firmware.py  upload()
      │
      │  1. session must be live and belong to nobody else       (403 if not)
      │  2. services/firmware.py validate(filename, data, board) (422 if rejected)
      │  3. store a copy: uploads/firmware/<session_key>_<ts>_<filename>
      │  4. record a FirmwareUpload row (status=received)
      │  5. services/nodes.flash()  →  POST <node>/flash          (502 if the bench refuses it)
      │  6. if .out/.elf: services/nodes.upload_debug_elf() → POST <node>/debug/upload-elf
      │  7. update the FirmwareUpload row (status=forwarded|failed)
      ▼
Lab Pi's own /flash endpoint — runs the actual flash tool on real hardware
```

Nothing the browser sends reaches the Lab Pi directly. Every hop after the initial POST is a
request the **portal** makes, server-side, using its own shared secret — see `_headers()` in
`services/nodes.py`.

## Layer 1: `services/firmware.py` — content validation

The primary gate. Pure function, `validate(filename, data, board="generic") -> ext`, raises
`firmware.Rejected(reason)` on any failure. Writes nothing, has no side effects — safe to call
speculatively. Checks run cheapest-first so an obvious reject costs nothing:

| # | Check | Rejects |
|---|---|---|
| 1 | **Extension** | Anything outside `.bin .hex .out .elf .uf2` |
| 2 | **Board/extension match** | A `.bin` for an Arduino (wants `.hex`/`.elf`), a `.hex` for an ESP32 (wants `.bin`), etc. — see `BOARD_EXTENSIONS` |
| 3 | **Size** | Under 8 bytes (empty/near-empty) or over 8 MB (real MCU images are far smaller — catches "uploaded the wrong file entirely," e.g. a project archive) |
| 4 | **Magic-byte sniff** | The file's real type, whatever its extension claims — `MZ` (Windows EXE), `PK\x03\x04` (ZIP), gzip/bzip2/xz/RAR archives, PNG/JPEG, PDF, Java class files — see `_MAGIC_REJECT` |
| 5 | **ELF-extension mismatch** | A real ELF binary (`\x7fELF` header) claiming to be `.bin`/`.hex`/`.uf2` instead of `.elf`/`.out` |
| 6 | **Script signature** | Content starting `#!/`, `<?php`, `<script`, `import `, `#include`, `function `, `eval(`, `os.system`, `subprocess` — a shell/PHP/Python/JS file wearing a firmware extension |
| 7 | **Text-content check (`.bin`/`.out` only)** | Overwhelmingly printable ASCII with no NUL bytes in the first 4 KB — a CSV, README, log, or source file that clears every check above unscathed because it doesn't match any *known* disguise signature. See below — this one shipped with a real gap. |
| 8 | **Structural: `.hex`** | Must decode as ASCII, every line (first 200 checked) must match Intel HEX record syntax (`:` + ≥10 hex digits), must end with an EOF record (`:00000001FF`) — catches a raw `.bin` renamed to `.hex`, or a truncated/corrupt file |
| 9 | **Structural: `.uf2`** | Must have the UF2 magic (`UF2\n` or its byte form) in the first 4 bytes |
| 10 | **ClamAV** (optional) | Second layer, only runs if `clamscan` is on `PATH` — see below |

`.bin` and `.out` are raw/opaque formats — check 7 above is the ceiling for what's structurally
checkable on those without actually executing or disassembling the image.

### The gap check 7 closed

Reported 2026-09-01: a CSV (`user_registration_template.out` — one of this project's own
`install/sample_users.csv`-style files, just renamed) was uploaded as `.out` firmware for a Tiva
board and **flashed successfully** — every check up to that point passed it clean:

- Extension (`.out`) — allowed.
- Board match — allowed (in fact `"tiva"` isn't even a key in `BOARD_EXTENSIONS`, so it fell
  back to the unrestricted default — a second, smaller gap worth knowing about).
- Size — a small CSV is well within 8B–8MB.
- Magic-byte sniff — a CSV's first bytes (`full_name,email,...`) don't match any *known* bad
  signature; that list is specifically EXE/ZIP/archive/image formats, not "plain text."
  Script-signature check similarly only matches specific shebangs/tags, not a CSV header row.
- Structural checks only ever existed for `.hex`/`.uf2` — `.bin`/`.out` had none at all.

So an opaque-format file that isn't a *recognized* disguise, but also isn't remotely firmware,
sailed straight through. Fixed with `_looks_like_text()`: real compiled machine code is
essentially never uniform printable ASCII with zero NUL bytes — padding and uninitialized data
sections alone make that reliable. Verified against the actual reported file, the real
`install/sample_users.csv` renamed to `.out`, and a plain-text README — all now correctly
rejected (`suspicious=True`) — while confirming realistic binary-shaped content (NUL bytes,
non-printable byte spread) still passes untouched, no false positives.

The philosophy, from the module's own docstring: **"malicious" for a firmware upload doesn't
mean the same thing as for a document.** A compiled image is opaque bytes with no meaningful
"signature" of its own; the actual risk is someone smuggling a script or a host executable
through a channel that then runs a flash tool on it, or a decompression-bomb-style file that
isn't firmware at all. So the checks are about **what kind of file this really is**, not about
matching known-malware hashes — that part is ClamAV's job, layered on top, not the primary
defense.

## Layer 2: ClamAV — signature-based scanning (optional, defense in depth)

`_clamav_scan(data)` runs last, after everything above has already passed:

```python
def _clamav_scan(data):
    clamscan = shutil.which("clamscan")
    if not clamscan:
        return                              # absent ClamAV is not an error
    proc = subprocess.run([clamscan, "--no-summary", "--infected", "-"],
                          input=data, capture_output=True, timeout=30)
    if proc.returncode == 1:                # 1 = infected, 0 = clean, 2 = error
        raise Rejected("...")
```

**Fails open by design**: if `clamscan` isn't installed, or it times out, or errors, the upload
is *not* blocked — the structural checks above are the primary defense and this is explicitly
"second layer," not load-bearing. A scanner that can't run must not stall or break a lab session.

**Current status on this server (checked 2026-09-01):**
- `clamav-freshclam` runs as a background systemd service (`clamav-freshclam.service`), keeping
  virus definitions current automatically — no manual `freshclam` needed (running it by hand
  while the service owns the log file is what produces the `Failed to lock the log file` error;
  that error means the daemon is already doing its job, not that something's broken).
- `clamscan` is on `PATH`, database files present in `/var/lib/clamav/` (`main.cvd`, `daily.cvd`,
  `bytecode.cvd`), all current as of the last `freshclam` run.
- Verified end-to-end with the standard EICAR test string through the real `validate()` entry
  point: correctly rejected with "flagged by the malware scanner."

## Storage and the audit trail

Every upload attempt — accepted, rejected, or accepted-but-failed-to-forward — gets a row in
`firmware_uploads` (`models.py`, `FirmwareUpload`):

| Field | What |
|---|---|
| `session_id`, `user_id`, `lab_pi_id` | Who, for which session, on which bench |
| `original_name`, `stored_name` | The name as uploaded, and the name it's actually stored under |
| `board`, `size_bytes`, `sha256` | Recorded regardless of outcome |
| `status` | `received` → `forwarded` (bench accepted it) / `rejected` (failed validation) / `failed` (passed validation, bench refused it) |
| `reason` | The rejection/failure message, when applicable |
| `created_at`, `forwarded_at` | Timestamps |

Accepted files are also copied to disk: `uploads/firmware/<session_key>_<unix_ts>_<filename>` —
`secure_filename()`'d, so this is the literal audit trail if a "what was actually uploaded" file
is ever needed later, independent of the database row.

**Gap worth knowing about:** there's currently no admin page that lists `FirmwareUpload` rows —
the data is fully recorded (confirmed via the test suite's "every upload is recorded for audit"
check) but not yet surfaced anywhere in `/admin`. Querying the table directly is the only way to
review it today.

## Configuration

| Setting | Where | Value |
|---|---|---|
| `UPLOAD_FOLDER` | `config.py` | `<app root>/uploads` (firmware lands in `uploads/firmware/`) |
| `MAX_CONTENT_LENGTH` | `config.py` | 32 MB — Flask's own hard cap on any request body, ahead of `validate()`'s own 8 MB firmware-specific limit |
| `MAX_BYTES` / `MIN_BYTES` | `services/firmware.py` | 8 MB / 8 bytes |
| `ALLOWED_EXTENSIONS` / `BOARD_EXTENSIONS` | `services/firmware.py` | Hardcoded — edit here to support a new board or extension |

No environment variable toggles ClamAV on/off — it's presence-detected (`shutil.which`) every
call, so installing/removing it takes effect immediately with no restart or config change.

## Honest mistake vs. suspicious upload

Not every rejection means the same thing. Uploading a `.bin` for a board that wants `.hex` is
almost certainly just a mix-up; uploading a Windows executable, or a shell script, disguised
with a firmware extension is not something that happens by accident. `firmware.Rejected` carries
a `suspicious` flag (`services/firmware.py`) distinguishing the two:

| Suspicious (`True`) | Not suspicious (`False`) |
|---|---|
| Magic-byte disguise (EXE/ZIP/gzip/etc claiming to be firmware) | Wrong/missing extension |
| Script signature (`#!/`, `<?php`, ...) | Board/extension mismatch |
| ClamAV signature hit | Empty or oversize file |
| | Corrupt/truncated `.hex` or `.uf2` |

What each level actually does, end to end:

- **Student-facing**: a suspicious rejection pops a blocking `alert()` on top of the normal
  status message (`templates/portal/lab.html`) — a firm, explicit notice: *this system logs
  every upload attempt against your account, and uploading anything other than your own
  compiled firmware is a policy violation subject to review.* An honest mistake just shows the
  normal inline "here's what to fix" text, no dialog.
- **Audit trail**: `blueprints/firmware.py` logs a suspicious rejection at `SystemLog`
  `level="error"` with the message prefixed `rejected as SUSPICIOUS (not a firmware file)`,
  instead of the routine `level="warning"` an honest mistake gets — so it's visually and
  filterably distinct on `/admin/logs` (filter to **Error**) rather than blending into routine
  "wrong board selected" noise.
- **API response**: `{"status": "rejected", "message": "...", "suspicious": true|false}` — the
  flag is on the wire, not just inferred from message text, if anything downstream ever needs it
  (the planned dedicated firmware-uploads admin page, for one).

## How it's tested

**Automated** (`test_flows.py`, "Firmware upload gateway" section, 26 checks): valid `.hex`
passes; `.exe`, shell script, ZIP, wrong extension, empty file, raw-`.bin`-as-`.hex`,
board/extension mismatch, oversize file, a CSV/README disguised as `.out`/`.bin`, and a `.uf2`
for a board that has no business accepting one are all rejected; the suspicious/honest-mistake
split is correct for each of those; a real binary-shaped `.out` payload is confirmed to *pass*
(no false positive from the text-content check) and the actual `tiva`+`.out` workflow this
deployment uses is confirmed still working; upload without a live session is refused; a crafted
malicious upload is rejected with `422`, flagged `suspicious: true` in the response, logged at
`error` level, and never forwarded; a valid upload is accepted, forwarded to the bench's
`/flash`, and recorded. Full suite is 155/155 passing as of this doc.

**Manual, live, through the real code path** (not mocks) — run 2026-09-01, all as expected:

```python
from services import firmware
firmware.validate("blink.hex", good_intel_hex, "arduino")          # passes
firmware.validate("evil.bin", b"#!/bin/bash\nrm -rf /\n...", ...)  # rejected: looks like a script
firmware.validate("virus.hex", b"MZ" + ..., "arduino")             # rejected: Windows executable
firmware.validate("shell.elf", b"<?php system(...); ?>...", ...)   # rejected: looks like a script
firmware.validate("archive.uf2", b"PK\x03\x04" + ..., ...)         # rejected: ZIP archive
firmware.validate("fw.bin", ..., "arduino")                        # rejected: board/ext mismatch
firmware.validate("bad.hex", truncated_hex, "arduino")              # rejected: missing EOF record
firmware.validate("eicar.bin", EICAR_TEST_STRING, "generic")        # rejected: malware scanner
```

## What this does *not* protect against

Worth being honest about the ceiling here:

- **A well-formed malicious binary.** These checks establish *this is plausibly a compiled
  firmware image of the claimed type*, not that its logic is safe. A `.bin` that's genuinely a
  valid binary but does something harmful once running on the MCU (bricks it, drives a GPIO pin
  in a damaging way, etc.) passes every check here — that's a hardware-behavior problem, not a
  file-format one, and out of scope for a content scanner.
- **ClamAV's own coverage.** It's signature-based; a novel payload with no matching signature in
  the database won't be caught by layer 2 (structural checks in layer 1 remain the primary
  defense specifically because of this).
- **Anything past `/flash`.** Once the bench accepts the file, what its flash tool does with it,
  and whether the bench itself has any validation, is the Lab Pi's own concern — the portal's
  docstring says this explicitly: *"The Pi's /flash endpoint has no validation of its own beyond
  secure_filename, so this is the only gate."* If that ever changes on the node side, it doesn't
  reduce anything here, but it's worth knowing the portal isn't relying on a second check
  downstream.
