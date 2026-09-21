# IKEN Remote Lab — v7 changes (native lab UI, no more iframe-to-the-node)

## What's new since v6

### 1. The lab page no longer embeds the node's own UI in an iframe
v6 shipped `/lab/<code>` as a portal-owned page with the node's `/experiment`
page inside an `<iframe>` — serial, camera, oscilloscope and audio all
actually ran on the Lab Pi, with the browser's `<iframe>` pointed straight at
it. That was a deliberate call at the time ("a 2175-line Socket.IO app, not
worth proxying" — see the old note in `blueprints/portal.py`).

v7 proxies all of it through the portal instead, the same way
`remote_lab_admin` already does in production (confirmed against the real
fleet — `<bench-1>` / `.112` — before building this: SocketIO on
`:10000`, ustreamer MJPEG on `:8080`, an aiortc audio `/offer` endpoint on
`:9000`). The browser's address bar now never leaves the portal's own host.

- `services/pi_relay.py` — one SocketIO **client** connection per active
  session out to the node, re-emitted into a SocketIO **server** room named
  after the session key. Every browser tab for that session joins the same
  room.
- `services/audio_relay.py` — a two-hop WebRTC relay (portal↔node,
  portal↔browser) via `aiortc`, so the browser never opens a peer connection
  straight to a node.
- `sockets.py` — the `connect`/`disconnect` handlers and the browser→node
  command forwarding loop, looked up against the real `Session`/`LabPi`
  tables (no in-memory session cache needed — the portal already has this as
  durable state).
- `blueprints/portal.py` — `/lab/<code>` renders the native control panel;
  `/chart`, `/oscilloscope`, `/camera` (flat routes, `?key=<session_key>`,
  matching remote_lab_admin's own shape) pop out as their own tabs;
  `/lab/relay` and `/lab/factory-reset` (POST) forward the physical
  power/relay toggle and default-firmware reset; `/lab/<key>/camera-stream`
  chunk-proxies the MJPEG feed; `/lab/<key>/audio-offer` negotiates the
  WebRTC leg.
- `services/nodes.py` gained `ui_config()`, `camera_url()`,
  `audio_offer_url()`, `open_camera_stream()`, `toggle_relay()`,
  `factory_reset()`, and `DEBUGGABLE_BOARD_TYPES` (GDB/OpenOCD sidebar entry
  only shows for `stm32`/`nucleo_f446re`/`black_pill`/`tiva` boards — every
  bench on the fleet right now is `arduino`, so it won't appear until a
  debuggable board is registered).
- `templates/portal/lab.html` is a full, adapted port of
  `remote_lab_admin/templates/index.html` (the IKEN-branded redesign —
  status pills, collapsible icon-rail sidebar, file-chip firmware picker,
  multi-slot serial monitor, dynamic controls, the works) — every
  admin-specific endpoint repointed at the portal's own routes, the CSRF
  shim stripped (the portal has none), and `sessionKey` read from Jinja
  instead of a `?key=` URL param the portal's URLs don't carry.
  `templates/portal/chart.html` / `oscilloscope.html` / `camera.html` got
  the same treatment. The old iframe page is kept as
  `templates/portal/lab_iframe.html` — an instant rollback via the new
  `NATIVE_LAB_UI` config flag (`.env`), no code changes needed.
- New deps: `Flask-SocketIO`, `python-socketio[client]`, `aiortc`, `av`
  (installed cleanly from prebuilt wheels — no system FFmpeg dev packages
  needed despite the older TESTING.md note in remote_lab_admin).
- **Deploy change:** gunicorn moved from 4 sync workers to
  `--worker-class gthread --workers 1 --threads 12` — relay state (which
  node a session's socket is talking to) lives in process memory, so a
  second worker process wouldn't see it. Matches remote_lab_admin's own
  deployed config. `install/remote-lab-portal.service` updated to match.

### 2. Admin console: per-node UI settings (`/admin/lab-pi/<id>/ui-settings`)
Full port of `remote_lab_admin`'s `admin_lab_pi_ui_settings` and its half
dozen sibling routes — which student controls are enabled, serial port
profiles (add/edit/delete), required dynamic controls (add/edit/delete),
and copying settings from one node to another serving the same experiment.
Styled to match the portal's own existing admin console (`_admin_shell.html`,
`.card`/`.field`/`.btn`) rather than importing admin's separate `theme.css` —
this is one more page among many in the portal's admin section, not a
standalone branded page like the lab UI.

Like admin's version, the portal stores none of this itself: every read and
write round-trips live to the node's own `/api/admin/ui-config` /
`/api/admin/controls` / `/api/admin/ports`. **Checked both live benches
before building this — neither answers that API yet** (`404` on
`<bench-1>` and `<bench-2>`), so right now every save on a real
bench fails with a clear "could not reach" message rather than a 500. It's
ready for the moment the node firmware adds that API.

- `services/nodes.py` — `admin_api()` (the proxy call), `DEBUG_BOARDS`,
  `BOARD_TYPE_CHOICES`.
- `blueprints/admin.py` — `lab_pi_ui_settings`, `lab_pi_ui_control_add/edit/delete`,
  `lab_pi_ui_port_add/edit/delete`, `lab_pi_ui_copy_to`, plus the form-shaping
  helpers (`_ui_config_body_from_form`, `_control_to_rc_form`,
  `_remap_port_id_by_label` — port ids are per-node, so a copy re-matches by
  port *label* on the target rather than carrying the id across).
- `templates/admin/lab_pi_ui_settings.html` (new), linked from a "UI
  settings" button on each row of `admin/devices.html`.
- `static/css/app.css` — added `.flash-warning` (Copy-to's port-remap
  warnings use it; every other flash category already existed).

### 3. Admin password recovery
Reset via the already-documented path (`manage.py reset-password`) — no
code changes, just a generated one-time password for `admin@vlab.edu`.

## Two live incidents this session, both fixed

1. **Systemd unit file corruption.** Someone editing
   `/etc/systemd/system/remote-lab-portal.service` by hand dropped the
   `[Unit]` header and merged `Restart=always` into the `ExecStart=` value
   via a stray line continuation, so gunicorn failed at every startup
   (`unrecognized arguments: Restart=always`) and the site was fully down.
   Rewrote and verified the unit with `systemd-analyze verify` before
   handing it back over.
2. **`/lab/<code>` briefly 500'd, then showed "Invalid Session."** Both were
   caused by a second, independent editor working the same files at the
   same time as this session (confirmed, not a caching bug — see the
   transcript around 12:30–12:56 IST if you want the full trail). Landed
   mid-collision:
   - `services/nodes.py` lost `DEBUGGABLE_BOARD_TYPES` in a rewrite → 500 on
     every `/lab/<code>` load. Restored it.
   - `blueprints/portal.py`'s `lab()` got repointed at a new,
     **still-unfinished** template, `templates/portal/experiment_ui.html` —
     a raw, unadapted copy of `remote_lab_admin/templates/index.html` (its
     `sessionKey` reads from a `?key=` URL param the portal's URLs don't
     have, and its buttons still POST to `/toggle_relay`, `/flash`,
     `/factory_reset`, `/sop/exp.pdf` — none of which exist here). Pointed
     `lab()` back at the fully-adapted `portal/lab.html` from item 1 above.
     `experiment_ui.html`, `services/realtime.py`, `services/lab_pi_relay.py`
     and `wsgi.py` were all left in place, untouched — someone else's
     in-progress work, not mine to delete. `wsgi.py` is genuinely useful
     (a real `application` entrypoint) and is now what the systemd unit
     runs (`wsgi:application` instead of `"app:create_app()"`).

## Tests: 143 passing (was 133 in v6)

## NOT YET DONE
- **Reconcile with the other in-progress edit.** `templates/portal/experiment_ui.html`,
  `services/realtime.py`, and `services/lab_pi_relay.py` are dead code right
  now (not referenced by any route) but still on disk. Whoever was building
  those should either finish wiring them up properly or they should be
  deleted once confirmed unused — don't delete blind.
- **The node firmware still needs its `/api/admin/*` API.** The portal side
  (item 2 above) is built and tested; nothing will actually save until at
  least one bench's `remote_lab_pi` exposes `/api/admin/ui-config` /
  `/api/admin/controls` / `/api/admin/ports`.
- **`/api/ui-config` 401s on `pi2`/`lab1` (<bench-1>).** The portal's
  `NODE_SHARED_SECRET` doesn't match whatever that specific bench has
  configured for its admin-auth header, so the lab page's `ui_config` falls
  back to the safe-default (`FALLBACK_UI_CONFIG` — mostly hides/disables
  controls) instead of that bench's real settings. `pi3` (`<bench-2>`)
  answers `404` on the same call (older firmware without the route at all,
  handled the same safe way). Neither breaks the page, both mean the real
  per-bench control config isn't actually reaching students yet — worth
  fixing the secret mismatch when you're back.
- Everything already open from v6's own list (duplicate `lab1`/`pi2` nodes,
  duplicate experiments, rotating `NODE_SHARED_SECRET`) — untouched this
  round.
