# Deploying to <portal-host>

I could not reach your server from where this was built, so these are the
steps to run yourself. They take about five minutes.

## Before you start

**Change that password.** The initial password was shared out-of-band and must be
considered compromised. On the server:

```bash
passwd                          # pick something long
```

Better still, switch to key-based login and turn password authentication off:

```bash
ssh-copy-id user@<portal-host>
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

## 1. Copy the code across

From the machine where you downloaded `remote_lab_portal.tar.gz`:

```bash
scp remote_lab_portal.tar.gz user@<portal-host>:~
ssh user@<portal-host>
tar xzf remote_lab_portal.tar.gz
cd remote_lab_portal
```

## 2. Install

```bash
sudo ./install/install.sh
```

The script installs Python and a virtualenv, generates a `.env` with a random
`SECRET_KEY` and `NODE_SHARED_SECRET`, creates the database and the first admin
account, then installs and starts a systemd service on port 5000.

Two things it prints are worth keeping:

- **The admin password.** Shown once. You are asked to change it at first
  sign-in anyway.
- **`NODE_SHARED_SECRET`.** Every Lab Pi needs this exact value.

Re-running the script later picks up code changes and never overwrites an
existing `.env`, so it cannot sever a working configuration.

## 3. Open the firewall

```bash
sudo ufw allow 5000/tcp          # if ufw is running
```

Then visit `http://<portal-host>:5000` and sign in as `admin@vlab.edu`.

## 4. Configure email

Invitations and booking confirmations need SMTP. Until it is set, messages are
written to the journal instead — the portal works, but nobody receives their
temporary password, so you would have to read it out of the log.

```bash
sudo nano /path/to/remote_lab_portal/.env
```

```ini
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=your-account@gmail.com
MAIL_PASSWORD=your-app-password
MAIL_DEFAULT_SENDER=Remote Lab <no-reply@vlab.edu>
```

Gmail needs an **app password**, not your account password, with 2FA enabled.
Then `sudo systemctl restart remote-lab-portal`.

To confirm it works, add one user to yourself in the admin panel and check the
message arrives.

## 5. Set the portal up

In this order, because each step depends on the one before:

1. **Courses** — add `E3-241` and any others.
2. **Experiments** — the seed creates three; attach each to a course and upload
   its lab manual PDF.
3. **Users** — upload your roster. `install/sample_users.csv` shows the format.
4. **Lab nodes** — see below.

Nothing appears on a student's dashboard until they are enrolled on a course
that has experiments, so step 1 really does come first.

## 6. Connect the Lab Pis

On each Pi running `remote_lab_pi`:

```bash
cd ~/lab-pi
cp /path/to/node_integration.py .
nano .env
```

```ini
MASTER_URL=http://<portal-host>:5000
NODE_SHARED_SECRET=<the value install.sh printed>
LAB_PI_ID=lab-bench-1
LAB_PI_NAME=Lab Bench 1
EXPERIMENT_SLUG=dc-motor-speed-control
BOARD=arduino
```

`EXPERIMENT_SLUG` must match the slug shown under the experiment's name in the
admin panel — that is what pairs a node to a rig.

Then register the blueprint in the Pi's `app.py`:

```python
from node_integration import bp as master_bp, start_background_tasks
app.register_blueprint(master_bp)
start_background_tasks(app)
```

Restart the node service. Within thirty seconds it appears under **Lab nodes**
as online, with its telemetry.

If a node cannot reach the master, add it from the other direction instead:
**Lab nodes → Add node by IP**. The portal queries the node and fills in the
rest.

The node's `/experiment` page needs one change — validate the incoming key:

```python
from node_integration import validate_key

@app.route("/experiment")
def experiment():
    ok, info = validate_key(request.args.get("key"))
    if not ok:
        return render_template("expired_session.html", reason=info), 403
    return render_template("experiment.html", session=info)
```

Without this the bench would accept anyone who knows the URL.

## 7. Check it end to end

Book a slot as a test student and start it. You should land on the node's
experiment page with the timer running. If you do not:

```bash
journalctl -u remote-lab-portal -f     # on the master
journalctl -u vlab-lab-pi -f           # on the node
```

Then check the **Activity log** in the admin panel, which records every session
issued and every node registration.

## Locked out of the admin account

Re-running `install.sh` does **not** reset an existing admin's password — it
prints one only the first time, when the account is created. If that password
was lost, recover from the command line rather than deleting the database:

```bash
cd ~/remote_lab_portal
source venv/bin/activate

python manage.py list-admins                        # which accounts exist
python manage.py reset-password admin@vlab.edu      # prints a new password
```

To choose the password yourself instead of having one generated:

```bash
python manage.py reset-password admin@vlab.edu --prompt
```

Other recovery commands:

```bash
python manage.py make-admin someone@iisc.ac.in   # promote an existing account
python manage.py unlock admin@vlab.edu           # re-enable a disabled account
```

If `list-admins` reports no administrators at all, create one:

```bash
python seed.py --admin-email you@iisc.ac.in
```

## Existing Lab Pis on the older API

Nodes running the current `remote_lab_pi` post to `/api/lab-pi/...` and send no
shared secret. The portal accepts them as they are — no change needed on the
Pi. Unknown nodes are registered on first heartbeat and appear under **Lab
nodes** within thirty seconds.

The compatibility layer is tolerant about field names, because deployed
firmware cannot be assumed: `cpu` / `cpu_percent` / `cpu_usage` all work, as do
`temp` / `temperature` / `cpu_temp`, and a node may identify itself with
`node_id`, `lab_pi_id`, `id` or `name`.

To see exactly what your hardware sends, the first payload from each node is
logged in full:

```bash
sudo journalctl -u remote-lab-portal | grep "First legacy payload"
```

**This accepts unauthenticated heartbeats** from anything that can reach the
port. On a closed lab network that is a fair trade for working hardware; on a
routable one it is not. Once every Pi runs `install/node_integration.py` and
presents `X-Node-Secret`, close it:

```ini
LEGACY_NODE_COMPAT=false
```

A node that sends a *wrong* secret is refused either way.

## TemplateNotFound, or the pages render with no styling

The service is running from a directory that has no `templates/` or `static/`.
Almost always this is a nested extraction — running `tar xzf` while already
inside `~/remote_lab_portal` produces
`~/remote_lab_portal/remote_lab_portal/`, and systemd keeps pointing at the
outer one.

```bash
ls ~/remote_lab_portal          # a remote_lab_portal folder in here is the fault
```

Fix by flattening it:

```bash
cd ~/remote_lab_portal
sudo systemctl stop remote-lab-portal
cp -r remote_lab_portal/* .     # lift the inner copy up one level
rm -rf remote_lab_portal
sudo ./install/install.sh
```

Always extract from your **home** directory, not from inside the project:

```bash
cd ~ && tar xzf remote_lab_portal.tar.gz
```

## Common problems

**Node shows offline.** It has not sent a heartbeat in 90 seconds. Check
`MASTER_URL` is reachable from the Pi (`curl http://<portal-host>:5000/login`)
and that `NODE_SHARED_SECRET` matches exactly — a trailing newline in `.env`
will break it.

**"Fetch details and add" fails.** The master cannot reach the node on that IP
and port. Confirm the lab-pi service is listening and that nothing between them
is filtering port 5000.

**"That email and password don't match an account."** Either the password is
wrong or the address does not exist. `python manage.py list-admins` shows every
administrator account, and `reset-password` above fixes the first case.

**Invitations never arrive.** Check spam first, then
`journalctl -u remote-lab-portal | grep mail`. A line tagged
`[mail:not-configured]` means `MAIL_SERVER` is unset and the message was only
logged.

**Student sees no experiments.** They are not enrolled on a course, or that
course has no active experiments. Open their profile under **Users** and check
the enrolment boxes.

**Session key rejected at the node.** Usually clock drift. Install NTP on both
ends: `sudo timedatectl set-ntp true`.

## Running it behind TLS

Session keys travel in URLs and invitation emails carry temporary passwords, so
if the portal is reachable beyond your own network, put nginx in front of it:

```nginx
server {
    listen 443 ssl;
    server_name vlab.example.edu;

    ssl_certificate     /etc/letsencrypt/live/vlab.example.edu/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/vlab.example.edu/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Then set `PORTAL_BASE_URL=https://vlab.example.edu` in `.env` so invitation
links point at the right place.

## Backups

Everything that matters lives in three places:

```bash
tar czf backup-$(date +%F).tar.gz data/ static/sop/ .env
```

`data/portal.db` is the database, `static/sop/` holds the lab manuals, and
`.env` holds the secrets. Losing `.env` signs everyone out and breaks the node
link, so keep a copy somewhere safe.


## Removing the portal

Do not simply delete the folder — the systemd service will keep trying to
restart from a path that no longer exists, and you will lose the database and
`.env` with no copy.

**Keep the data, remove the service** (what you want before a clean reinstall):

```bash
cd ~/remote_lab_portal
sudo ./install/uninstall.sh
```

This stops and unregisters the service and leaves `data/`, `static/sop/` and
`.env` untouched, so `sudo ./install/install.sh` afterwards picks up exactly
where you were.

**Remove everything**, including accounts, bookings and secrets:

```bash
sudo ./install/uninstall.sh --purge
```

It asks you to type `DELETE`, then writes a timestamped backup to `/root/`
before removing anything. Once that has run, the source tree can go too:

```bash
cd ~
sudo rm -rf ~/remote_lab_portal
rm -f ~/remote_lab_portal.tar.gz
```

**Back up first if there is any doubt.** One command captures everything that
matters:

```bash
cd ~/remote_lab_portal
tar czf ~/remote-lab-backup-$(date +%F).tar.gz data/ static/sop/ .env
```

### Replacing an existing install with a newer build

```bash
cd ~/remote_lab_portal
tar czf ~/remote-lab-backup-$(date +%F).tar.gz data/ static/sop/ .env   # safety copy
cd ~
tar xzf remote_lab_portal.tar.gz          # extracts over the top
cd remote_lab_portal
sudo ./install/install.sh                 # keeps .env and the database
```

Extracting over the top replaces code but leaves `data/` and `.env` in place,
because neither is in the archive. That is the intended upgrade path.
