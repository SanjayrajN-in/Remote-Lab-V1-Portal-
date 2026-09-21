# Project state — pick up from here

Last updated: 13 August 2026

A snapshot of what exists, what changed most recently, and what is still open.
Read this first when returning to the project after a gap, or when handing it
to someone else.

---

## Where things stand

The master server is **complete and tested**. 78 end-to-end checks pass from a
clean extract. Nothing is stubbed or half-finished on this side.

What has **not** happened yet: the Lab Pis have not been connected. That is the
next real milestone and it needs physical hardware, so it could not be done
here. Everything the nodes need is written and documented — see *Next steps*.

| Area | State |
|---|---|
| Authentication, roles, password reset | done |
| Courses, enrolment, course-scoped visibility | done |
| Experiments, detail popup, PDF manuals | done |
| Slot booking, cancellation, conflict handling | done |
| Sessions: issue, expiry, revoke | done |
| Admin panel — all tabs | done |
| Bulk user import (CSV + Excel) with invitations | done |
| Node API — new `/api/node/...` paths | done |
| Node API — legacy `/api/lab-pi/...` compatibility | done; a real node is heartbeating against it |
| Email | works; needs real SMTP credentials in `.env` |
| Lab Pi integration | reference code written, not yet deployed to a Pi |
| TLS / reverse proxy | not set up; see `DEPLOY.md` |

---

## Changelog

### 13 August 2026 (latest) — matched to the real remote_lab_pi API

Read from the actual repo (`LAB_PI_API_CONTRACT.md` and `app.py`) rather than
guessed. The Pi's real contract differs from what was built against:

| What | Was assumed | Actually |
|---|---|---|
| Identify a node | `GET /api/info` | **does not exist** — the Pi has `/api/ui-config`, `/ports`, `/experiment` |
| Start a session | `POST /api/session/start` | `POST /api/lab-pi/session-start` |
| End a session | `POST /api/session/end` | `POST /api/lab-pi/session-end` |
| Auth header | `X-Node-Secret` | `X-Master-Api-Key` (+ `X-Lab-Pi-Id`), per `_verify_master_request()` |
| Session expiry | ISO string | `session_end_time` in **JavaScript milliseconds** |
| Poller endpoint | `/api/lab-pi/<id>/sessions` | `/api/lab-pi/<id>/active-session`, answering `{"status": "running"\|"stopped"}` |
| Heartbeat fields | `cpu`, `ram`, `temp`, `battery_percent` | `cpu_usage`, `ram_usage`, `temperature`, `battery_soc`, `battery_ac_status: AC_CONNECTED\|ON_BATTERY` |
| Node identity | `node_id` | `lab_pi_id`, with `ip_address` not `ip` |
| Registration | `experiment_slug` | `experiment_id` (int), plus `mac_address`, `location` |
| Student link back | `/my-bookings` | the Pi links to `/my_bookings` |

**"Could not connect to <bench-1>:5000" on Add node** was this: `probe()`
asked for `/api/info`, which no real Pi serves. It now tries several endpoints
in turn and treats *any* answer as proof a Lab Pi is there, taking whatever
fields come back. If the node is unreachable entirely, an admin can supply the
`LAB_PI_ID` and add it anyway — it comes online by itself on its first
heartbeat. Refusing to record a node because one JSON endpoint is missing was
the wrong trade.

The heartbeat reply now carries any pending session as
`{new_session: true, session: {...}}`, which is what `app.py` reads straight
out of the response — so a node gets its session even if the poller is not
running.

Verified end to end: the Pi's own `session_end_time` arithmetic, replayed
against a generated payload, computes 60.0 minutes for a one-hour booking.
Tests 100–113 cover the real payloads.

### 13 August 2026 — IST throughout

Everything a person reads or types is now Indian Standard Time. Storage stays
naive UTC, which is what keeps comparisons unambiguous.

This fixed a real bug, not just labelling. The slot grid was built from the
*server's* local date but compared against `utcnow()`, so with the server in
IST every slot appeared 5.5 hours in the past — at 12:47 IST the grid greyed
out everything before 07:00.

- `services/timeutil.py` is the only place the conversion happens:
  `from_local()` on the way in, `to_local()` / `fmt()` on the way out.
- The `dt` template filter now renders stored UTC in IST, so every existing
  `| dt` call was corrected without touching the templates. `dtz` adds the
  zone name where it could be ambiguous.
- Slot generation builds a wall clock in IST and converts each boundary to UTC
  for querying and comparison. The posted value is IST and is converted on
  arrival.
- The admin bookings date filter interprets the picked date as an IST day.
- Confirmation emails print the zone: `2026-08-14 23:00 IST`.
- Booking, My bookings and admin Bookings state "All times IST".
- `TIMEZONE` / `TIMEZONE_LABEL` are configurable; defaults are
  `Asia/Kolkata` / `IST`.

Tests 92–99 cover it, including that 23:00 IST is stored as 17:30 UTC and
renders back as 23:00 — a slot whose UTC day differs from its IST day, which
is exactly what the old code got wrong.

### 13 August 2026 — landing page with inline sign-in

`/` no longer sends visitors straight to a bare login form. It now introduces
IKEN and carries the sign-in form on the same page:

- Two columns — the pitch and the three-point explanation on the left, the
  sign-in card on the right — collapsing to one column on mobile with the
  **form first**, since people on a phone are usually returning users.
- A failed sign-in from this form re-renders the landing page rather than
  bouncing to `/login`, and keeps the email that was typed. `/login` still
  exists and works on its own for anyone linking directly to it.
- Signed-in visitors see "My experiments" / "My bookings" instead of the form.
- Footer names the department and support contacts.

Also fixed: the `LEGACY_NODE_COMPAT` warning was logging eight times on start
(once per worker per import). Now once per process.

### 13 August 2026 — legacy node compatibility

Found from a live deployment log: a Pi at <bench-1> was heartbeating every
two seconds and getting 404 on every one. The deployed `remote_lab_pi` posts to
**`/api/lab-pi/...`**, not the `/api/node/...` paths this portal was built
with, and it sends no shared secret. The API path was guessed wrong.

Rather than requiring every Pi in the lab to be edited before anything works,
`blueprints/legacy_api.py` accepts the old shape:

- `/api/lab-pi/register`, `/heartbeat`, `/<id>/sessions`, `/session/validate`,
  `/session/end` — same tables, same semantics as `/api/node/...`.
- Tolerant field mapping, because deployed firmware cannot be assumed:
  `cpu` / `cpu_percent` / `cpu_usage`, `temp` / `temperature` / `cpu_temp`,
  `node_id` / `lab_pi_id` / `id` / `name`, and battery state normalised from
  several spellings.
- An unknown node is **auto-registered on first contact** rather than refused,
  so working hardware is never silently invisible.
- The first payload from each node is logged in full at INFO, so the real
  schema can be read off a running system instead of guessed at again.
- `LEGACY_NODE_COMPAT` (default on) gates it. A wrong secret is refused
  either way; the flag only controls whether a *missing* one is tolerated.

Turn the flag off once every node runs `node_integration.py`. Tests 79–91
cover the legacy paths, including the switch-off behaviour.

One bug the tests caught while writing this: an auto-registered node had no
primary key yet when its first heartbeat row was written, so the insert failed
on a NOT NULL constraint. Fixed with a `flush()` before the heartbeat is added.

### 13 August 2026 (later) — IKEN branding and account recovery

**Branding.** The portal now carries the IKEN identity throughout.

- `static/img/iken-logo.png` (full lockup with tagline) and
  `iken-mark.png` (cap + wordmark, for small spaces) — both trimmed to the
  artwork with transparent backgrounds, so they sit on any surface.
- Palette realigned to the mark: navy `#011a38` for structure and primary
  actions, orange `#f48221` as the accent. Teal and coral are kept strictly
  for status — online and destructive — where the hue has to carry meaning
  regardless of branding. Old `--indigo`/`--amber` variables are aliased to
  the brand colours, so existing rules keep working.
- On the navy sidebar the logo sits on a white chip (`.logo-chip`) rather than
  being knocked out to white, so the brand keeps its own colours.
- Product name is now "IKEN Remote Lab" in page titles, both sign-in pages,
  both shells, the hero and the email header.

**Account recovery — `manage.py`.** This closes a real hole. Re-running
`install.sh` runs `seed.py`, which leaves an existing admin's password
untouched and prints nothing; if the original was lost there was no way back
in short of deleting the database. Now:

```
python manage.py list-admins
python manage.py reset-password admin@vlab.edu [--prompt | --password X]
python manage.py make-admin someone@iisc.ac.in
python manage.py unlock someone@iisc.ac.in
```

A generated password sets `must_change_password`; one you type yourself is
taken as final. Mistyping an address lists the accounts that do exist rather
than just failing.

### 13 August 2026 — admin/student separation

**The problem.** Signing in as `admin@vlab.edu` appeared to land on the student
portal. The routing was correct; the chrome was not. The seeded admin has
`must_change_password = True`, so login sent them to the password-change page
first — and that template extended the *student* shell, so it rendered with the
white top bar and "My bookings" nav. Changing the password would have reached
the admin panel, but the first screen looked wrong.

**What changed.**

- `templates/change_password.html` now selects its shell by role, so an admin
  never sees student navigation.
- New route `auth.admin_login` at `/admin/login` — a separate staff sign-in.
  It refuses non-admin accounts with a message pointing at the student page,
  instead of signing them in and dropping them somewhere empty.
- New template `templates/admin_login.html`: dark field, amber accent,
  "Staff access" tag. Deliberately unlike the student page.
- Admin panel gained a persistent identity: amber rule along the top of every
  page (`.main-admin`) and an "Admin mode" flag in the sidebar.
- `/login` now links across to the staff door.
- Tests 72–78 added, covering the role-refusal and the chrome fix.

### 12 August 2026 — initial build

Full application: models, four blueprints, three services, 25 templates, the
new visual language, installer, systemd unit, node reference implementation,
and 71 end-to-end tests.

---

## Design decisions worth remembering

Written down because they look like oversights if you do not know the reasoning.

**Booking status is derived, not stored.** `Booking.live_status` computes
*upcoming / active / completed* from the clock each time it is read. There is
no scheduled job flipping rows, and there does not need to be. The stored
`status` column only records the two states a clock cannot infer: `cancelled`
and an explicitly ended `completed`.

**Enrolment is the access rule, enforced at the route.** `_visible_or_404()` in
`blueprints/portal.py` guards every experiment route. Hiding cards in the
template would not be enough — a student guessing an ID gets a 404, not a
booking form.

**Nodes poll as well as being pushed to.** The master pushes a session key
directly when a student clicks Start, but `GET /api/node/<id>/sessions` is the
authoritative list. A node behind NAT, or one that was briefly unreachable,
still picks up its sessions within one poll cycle. A failed push is logged as a
warning, not an error, and the student is told it may take a few seconds.

**The master never touches hardware.** No serial, no relays, no camera. It
decides who may use which rig and when, and hands over a key. All hardware
logic stays in `remote_lab_pi`. Keep it that way — it is what lets a node be
rebooted or swapped without the portal caring.

**Bulk import validates everything before writing anything.** One transaction.
A file with three bad rows imports the good ones and reports each problem
individually. It never leaves half-created users behind.

**Deleting an experiment that has bookings deactivates it instead.** Deleting
would orphan booking history. The admin is told this happened rather than left
to wonder why the row is still there.

---

## Next steps, in order

**1. Connect the first Lab Pi.** The largest remaining piece.

- Copy `install/node_integration.py` into the Pi's `remote_lab_pi` directory.
- Set `MASTER_URL`, `NODE_SHARED_SECRET`, `LAB_PI_ID`, `EXPERIMENT_SLUG` in the
  node's `.env`. The shared secret must match the master's exactly — a trailing
  newline will break it.
- Register the blueprint and start the background tasks in the Pi's `app.py`.
- Add the `validate_key()` check to the node's `/experiment` route. Without it
  the bench accepts anyone who knows the URL.
- Full instructions in `DEPLOY.md` §6.

**2. Configure SMTP.** Until `MAIL_SERVER` is set, invitations are only written
to the journal, so students never receive their temporary password. Test by
adding one user to yourself.

**3. Load real data.** Courses first, then experiments with their manuals, then
the student roster. Nothing appears on a student dashboard until they are
enrolled on a course that has experiments.

**4. Run one real session end to end.** Book a slot as a test student, start
it, confirm the browser lands on the node with the timer running.

**5. TLS.** Session keys travel in URLs and invitation emails carry temporary
passwords. If the portal is reachable beyond the lab network, put nginx in
front of it — `DEPLOY.md` has the config.

---

## Known gaps

Not bugs; things deliberately left out of scope.

- **No recurring or bulk bookings.** One slot at a time.
- **No waitlist.** A taken slot is simply unavailable.
- **One display timezone.** Storage is UTC and display is IST for everyone.
  A student abroad sees IST, not their own zone. Per-user zones would mean a
  preference on `User` and threading it through `timeutil`.
- **Heartbeat history is capped at 200 rows per node** — roughly 100 minutes.
  Enough for a live view, not for long-term trend analysis.
- **No CSRF tokens.** Flask-WTF was left out to keep the dependency list short.
  Worth adding if the portal ever faces the open internet.
- **Node telemetry is trusted as reported.** A compromised node could lie about
  its own health. It cannot forge sessions, which is the part that matters.

---

## Things that will bite you

- **Clock drift** between master and node causes valid keys to be rejected.
  `sudo timedatectl set-ntp true` on both.
- **Losing `.env`** signs everyone out (`SECRET_KEY`) and breaks every node
  link (`NODE_SHARED_SECRET`). Back it up.
- **Re-running `install.sh`** is safe and picks up code changes. It will not
  overwrite an existing `.env`.
- **The old repos still have secrets in git history** — `key.pem`,
  `cookies.txt` and `vlab.db` in `remote_lab_with_DB` and both `virtual_lab`
  repos. Deleting them in a new commit does not remove them; that needs
  `git filter-repo` or a fresh repository.

---

## Orientation

Start with `blueprints/portal.py` — the docstring at the top traces the whole
student journey, and the rest of the system exists to serve it. Then
`blueprints/node_api.py` for the hardware side. `models.py` explains the schema
choices in comments where they are not obvious.

```
app.py                  application factory
config.py               configuration from the environment
models.py               schema
seed.py                 first admin, courses, experiments
test_flows.py           78 end-to-end checks — run these after any change
blueprints/
  auth.py               both sign-in doors, password change and reset
  portal.py             student dashboard, booking, session launch
  admin.py              admin panel
  node_api.py           the Lab Pi side of the wire
services/
  mailer.py             email, with a log fallback
  importer.py           CSV/Excel roster import
  nodes.py              outbound calls to nodes
install/
  install.sh            server install (idempotent)
  uninstall.sh          remove the service and optionally the data
  node_integration.py   reference implementation for remote_lab_pi
  sample_users.csv      bulk-upload format
```

Run `python test_flows.py` before and after any change. It is fast and it
covers the paths that are easy to break without noticing.
