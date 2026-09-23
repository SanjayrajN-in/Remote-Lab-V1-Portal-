# Remote Lab portal

Master server for a remote embedded-systems laboratory. Students book an hour
on a real hardware bench; the portal decides who gets which rig and when, then
hands that student's browser a time-limited key to the Raspberry Pi wired to
the experiment.

This is the `remote_lab_admin` role, rebuilt with a new interface and a
course-enrolment model. It pairs with `remote_lab_pi` unchanged on the node
side, save for three small endpoints listed under *Node protocol* below.

## Architecture

```
                    ┌──────────────────────────┐
   students ───────▶│   Remote Lab portal      │
   admins   ───────▶│   (this app, "master")   │
                    │                          │
                    │  users · courses         │
                    │  experiments · bookings  │
                    │  sessions · telemetry    │
                    └───────────┬──────────────┘
                                │  register / heartbeat / poll sessions
                ┌───────────────┼───────────────┐
                ▼               ▼               ▼
          ┌──────────┐    ┌──────────┐    ┌──────────┐
          │ Lab Pi 1 │    │ Lab Pi 2 │    │ Lab Pi 3 │   ← remote_lab_pi
          │ DC motor │    │ DHT22    │    │ stepper  │
          └────┬─────┘    └────┬─────┘    └────┬─────┘
               │               │               │
            hardware        hardware        hardware
```

The master never touches a serial port, relay or camera. Each experiment is a
separate node with its own ID, and the node owns its hardware. The only thing
that crosses the boundary is a session key.

## What it does

**For students**

- Sign in with the account an administrator created; choose a real password on
  first use.
- See experiments from enrolled courses only — nothing else is listed, and the
  URLs 404 rather than merely hiding the cards.
- Open an experiment for its full description, objectives, apparatus and a
  downloadable lab manual (PDF).
- Book an hourly slot from a 24-slot day grid. Taken, past and already-yours
  slots are visibly distinct and not selectable.
- Get a confirmation email, then start the session from **My bookings** —
  *Start* before the slot is live, *Go to lab* once it is.

**Signing in**

There are two doors, and they look nothing alike so nobody has to guess which
side they are on:

| URL | Who | Looks like |
|---|---|---|
| `/login` | students (and admins, who are then routed onward) | light page, indigo accent |
| `/admin/login` | administrators only | dark page, amber accent, "Staff access" |

`/admin/login` refuses a student account outright and says where to go instead,
rather than signing them in and dropping them somewhere with nothing on it.

Inside the app the same split holds: the student portal has a white top bar,
the admin panel a dark sidebar with an amber rule and an "Admin mode" flag.

**For administrators**

- Add users one at a time or by uploading a CSV/Excel roster; everyone receives
  an invitation email with a temporary password.
- Define courses, attach experiments to them, and enrol students. Enrolment is
  the whole access rule.
- Add a lab node by IP address — the portal asks the node who it is rather than
  making you retype it.
- Watch node telemetry: CPU, RAM, temperature, battery state and uptime, with
  nodes marked offline after 90 seconds of silence.
- Review every booking with filters, open any booking to see the student behind
  it, and end a running session immediately if a bench needs freeing.

## Requirements

- Python 3.9 or newer
- SQLite (default) or PostgreSQL
- An SMTP account for invitation and confirmation emails — optional; without
  one, messages are written to the log and the portal still works end to end.

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # edit SECRET_KEY, NODE_SHARED_SECRET, PORTAL_BASE_URL
python seed.py            # creates the admin account and prints its password

python app.py             # students: http://localhost:5000/login
                          # admins:   http://localhost:5000/admin/login
```

`python seed.py --demo` additionally creates three sample students, a node and
a couple of bookings, which is useful for looking around before any real
hardware is attached. Each student gets its own random password, printed once
when the command runs - write them down, they are not recoverable.

Don't run `--demo` against a database that is already in real use: it adds
accounts that no one is expecting to be there.

For a server install with systemd and gunicorn, see `DEPLOY.md`.

## Recovering a lost admin password

`install.sh` prints the admin password only when it first creates the account.
Re-running it leaves an existing password alone. If it is lost:

```bash
source venv/bin/activate
python manage.py list-admins
python manage.py reset-password admin@vlab.edu
```

Use `--prompt` to type the new password instead of having one generated.
`make-admin` promotes an existing account; `unlock` re-enables a disabled one.

## Configuration

Everything is read from the environment; `.env.example` documents each value.
The ones that matter on day one:

| Variable | Why it matters |
|---|---|
| `SECRET_KEY` | Signs session cookies. Random, secret, and stable — changing it signs everyone out. |
| `NODE_SHARED_SECRET` | Every Lab Pi presents this. Must match the value in each node's `.env`. |
| `PORTAL_BASE_URL` | The address that goes into invitation emails. |
| `MAIL_SERVER` etc. | Leave unset to log messages instead of sending them. |
| `SLOT_MINUTES` | Slot length. 60 by default, giving 24 slots a day. |
| `MAX_ADVANCE_DAYS` | How far ahead students may book. |
| `MAX_OPEN_BOOKINGS_PER_USER` | Stops one student holding every bench. |

## Bulk user upload

CSV, TSV or `.xlsx`. Headers are case-insensitive and order does not matter.

| Column | Required | Notes |
|---|---|---|
| `full_name` | yes | |
| `email` | yes | Must be unique; the invitation goes here. |
| `roll_number` | no | |
| `department` | no | Created if it does not exist. |
| `courses` | no | Course codes separated by `;` or `,` |
| `role` | no | `user` (default) or `admin` |

```csv
full_name,email,roll_number,courses
Asha Rao,asha@iisc.ac.in,EE21B001,E3-241
Vikram Nair,vikram@iisc.ac.in,EE21B002,E3-241;E9-201
```

Every row is validated before anything is written, and the import runs in one
transaction. A row with a malformed email, a duplicate address or an unknown
course code is reported back to you individually; the remaining rows still
import. A bad file never leaves half-created users behind.

## Node protocol

Nodes authenticate with `X-Node-Secret` on every call. After registering, a
node also sends its own `X-Node-Token`, so one compromised node cannot
impersonate another.

**Master endpoints — implemented here**

| Endpoint | Called by | Purpose |
|---|---|---|
| `POST /api/node/register` | node at boot | Announce ID, IP, board, experiment. Returns the node's token. |
| `POST /api/node/heartbeat` | node, every 30 s | CPU, RAM, temperature, battery, uptime. |
| `GET /api/node/<id>/sessions` | node poller | The only session keys this node should admit. |
| `POST /api/node/session/validate` | node | A browser arrived with `?key=` — is it good? |
| `POST /api/node/session/end` | node | Session finished on the node's side. |

**Node endpoints — implement these on the Pi**

| Endpoint | Called by | Purpose |
|---|---|---|
| `GET /api/info` | master | Identify yourself when an admin adds you by IP. |
| `POST /api/session/start` | master | Accept a session key immediately. |
| `POST /api/session/end` | master | Revoke a key early. |

`install/node_integration.py` is a working reference implementation of all
six, ready to drop into `remote_lab_pi`.

**Existing nodes need no changes.** Pis that still post to the older
`/api/lab-pi/...` paths without a shared secret are accepted by
`blueprints/legacy_api.py`, which writes into the same tables and registers an
unknown node on first heartbeat. Set `LEGACY_NODE_COMPAT=false` once every node
has been migrated.

Because the node also polls, direct pushes from the master are an optimisation
rather than a requirement — a node behind NAT, or one that was briefly
unreachable, still picks up its sessions within one poll interval.

## Session lifecycle

1. Student clicks **Start**, five minutes before the slot at the earliest.
2. The master mints a `Session` with a random key expiring when the slot does.
3. The key is pushed to the node, and appears in that node's poll response.
4. The browser is redirected to `http://<node-ip>:5000/experiment?key=…`.
5. The node validates the key against the master and grants bench access.
6. At expiry the key stops validating and drops out of the poll list, so the
   node revokes access without needing to be told.

Booking status is derived from the clock rather than stored, so *upcoming*
becomes *active* becomes *completed* on its own. No cron job is required.

## Testing

```bash
python test_flows.py
```

91 end-to-end checks over the real routes: authentication, course-scoped
visibility, PDF download, slot booking and double-booking, session issue and
expiry, the full node API including token rejection, bulk upload with
deliberately malformed rows, the staff sign-in refusing a student account, and
every page rendering. No mocking of the app
itself — only outbound HTTP and SMTP are stubbed.

## Layout

```
app.py                  application factory
config.py               configuration from the environment
models.py               schema
seed.py                 first admin, courses, experiments
manage.py               password resets and account recovery
test_flows.py           end-to-end checks
blueprints/
  auth.py               sign-in, password change and reset
  portal.py             student dashboard, booking, session launch
  admin.py              admin panel
  node_api.py           the Lab Pi side of the wire
services/
  mailer.py             email, with a log fallback
  importer.py           CSV/Excel roster import
  nodes.py              outbound calls to nodes
install/
  install.sh            server install
  remote-lab-portal.service
  node_integration.py   reference implementation for remote_lab_pi
```

## Security notes

- `.env`, `data/`, `uploads/`, `*.pem` and `*.db` are excluded by `.gitignore`
  from the start. Do not commit secrets, TLS keys or the database.
- Set a real `SECRET_KEY` and `NODE_SHARED_SECRET` before going live; the
  defaults in `config.py` are placeholders and are meant to be replaced.
- Passwords are hashed with Werkzeug's PBKDF2. Reset tokens are single-use and
  expire in 24 hours.
- Put the portal behind TLS if it is reachable from outside your network —
  invitation emails carry temporary passwords, and session keys travel in URLs.
# remote_lab_portal
