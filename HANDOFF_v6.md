# IKEN Remote Lab — v6 changes (portal-hosted lab page + firmware gateway)

## What's new since v5

### 1. Firmware upload gateway (student uploads to portal, not the Pi)
- `services/firmware.py` — validates every upload: extension whitelist
  (.bin .hex .out .elf .uf2), board/extension match, size 8B–8MB, magic-byte
  rejection (MZ/ZIP/gzip/ELF-mismatch/scripts), Intel HEX structural check,
  UF2 magic, optional ClamAV.
- `blueprints/firmware.py` — POST /session/<key>/firmware. Requires a LIVE
  session. Flow: validate → store copy under UPLOAD_FOLDER/firmware → forward
  to the Pi's /flash via nodes.flash(). Every upload recorded in
  FirmwareUpload (received/forwarded/rejected/failed) for audit.
- `services/nodes.flash()` — multipart POST to the Pi's /flash (firmware +
  board + session_key), matching the Pi's contract.
- `FirmwareUpload` model added to models.py.

### 2. Portal-hosted lab page (/lab/<code>)
- Student reaches the rig through the portal, not a raw Pi link.
- Gates: owner + live session only.
- Embeds the Pi's /experiment?key=...&end_time=... in an iframe (serial,
  camera, oscilloscope, audio all stay on the Pi — a 2175-line Socket.IO app,
  not worth proxying).
- Hosts the scanned firmware upload above the frame (JS POSTs to
  /session/<key>/firmware).
- Server-driven countdown from Session.seconds_left.
- start_session now redirects here instead of the raw Pi URL, and probes the
  node live (socket connect) instead of trusting stale heartbeat.

### 3. UI fixes
- base.html: all flash messages auto-dismiss (errors 15s, others 5s).
  Previously error flashes never cleared.
- dashboard: bench status probed live per experiment, so a reachable Pi shows
  "bench ready" not "benches offline".

## Tests: 133 passing (was 113 in v5)

## NOT YET DONE
- Deploy to server (<portal-host>). New table firmware_uploads needs
  db.create_all() or a migration.
- Rotate NODE_SHARED_SECRET / Pi MASTER_API_KEY (c563... was leaked in chat).
- Clean duplicate experiments (dc-motor-speed-control vs -062939) and nodes
  (lab1 typed vs pi2 auto-registered = same bench).
