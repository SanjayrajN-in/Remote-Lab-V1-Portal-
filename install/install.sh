#!/usr/bin/env bash
# Install the Remote Lab portal on a Debian-based server.
#
#   sudo ./install/install.sh
#
# Idempotent: safe to re-run to pick up code changes. It never overwrites an
# existing .env, so re-running cannot sever a working configuration.

set -euo pipefail

APP_NAME="remote-lab-portal"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
# A dedicated, unprivileged system account - not the operator's own login.
# This used to be ${SUDO_USER}, which meant a compromise of the web process
# inherited that person's shell, SSH keys and sudo rights, since by
# construction they had just run this script with sudo.
RUN_USER="${RUN_USER:-remotelab}"
PORT="${PORT:-5000}"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m ✗\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."

if ! command -v apt-get >/dev/null 2>&1; then
  die "This installer expects apt. On other distributions, follow the manual steps in DEPLOY.md."
fi

say "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

say "Creating the virtual environment"
cd "$APP_DIR"
if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

say "Creating the service account"
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
  say "Created system account ${RUN_USER}"
else
  say "Using existing account ${RUN_USER}"
fi

say "Preparing directories"
mkdir -p data/sop uploads
# The source tree and the virtualenv stay root-owned and read-only to the
# service, so a file-write primitive in the web tier cannot become code
# execution on the next restart. Only the two data directories are writable.
# Lab manuals live in data/sop/, not static/, so they are never public.
chown -R root:root "$APP_DIR"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR/data" "$APP_DIR/uploads"

if [ ! -f .env ]; then
  say "Generating .env"
  cp .env.example .env
  SECRET=$(./venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')
  NODE_SECRET=$(./venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')
  IP=$(hostname -I | awk '{print $1}')
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env
  sed -i "s|^NODE_SHARED_SECRET=.*|NODE_SHARED_SECRET=${NODE_SECRET}|" .env
  sed -i "s|^PORTAL_BASE_URL=.*|PORTAL_BASE_URL=http://${IP}:${PORT}|" .env
  # root-owned: systemd reads EnvironmentFile as root before dropping
  # privileges, so the service account never needs to read the secrets.
  chown root:root .env
  chmod 600 .env
  # Written to a file rather than echoed. Printing it put the fleet-wide node
  # secret into terminal scrollback, script(1)/CI transcripts and shell history.
  umask 077
  printf 'NODE_SHARED_SECRET=%s\n' "${NODE_SECRET}" > "${APP_DIR}/.node-secret"
  chown root:root "${APP_DIR}/.node-secret"
  chmod 600 "${APP_DIR}/.node-secret"
  echo
  echo "  NODE_SHARED_SECRET written to ${APP_DIR}/.node-secret (mode 0600)."
  echo "  Put that same value in every Lab Pi's .env, then delete the file."
else
  warn ".env already exists — left untouched."
fi

say "Initialising the database"
sudo -u "$RUN_USER" ./venv/bin/python seed.py || warn "Seed reported a problem; check the output above."

say "Installing the systemd service"
sed -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__PORT__|${PORT}|g" \
    install/remote-lab-portal.service > "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable --quiet "$APP_NAME"
systemctl restart "$APP_NAME"
sleep 2

if systemctl is-active --quiet "$APP_NAME"; then
  IP=$(hostname -I | awk '{print $1}')
  say "Done"
  echo "  Portal:  http://${IP}:${PORT}"
  echo "  Logs:    journalctl -u ${APP_NAME} -f"
  echo "  Restart: systemctl restart ${APP_NAME}"
else
  echo
  die "The service did not start. Run: journalctl -u ${APP_NAME} -n 50 --no-pager"
fi
