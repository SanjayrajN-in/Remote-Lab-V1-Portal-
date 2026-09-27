#!/usr/bin/env bash
# Remove the Remote Lab portal from this server.
#
#   sudo ./install/uninstall.sh            stop and remove the service, keep data
#   sudo ./install/uninstall.sh --purge    also delete the database, manuals and .env
#
# The default is deliberately conservative: it stops the service and removes
# the systemd unit but leaves your data alone, so you can reinstall over the
# top without losing anything. --purge is the destructive one, and it takes a
# backup first regardless.

set -euo pipefail

APP_NAME="remote-lab-portal"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
PURGE=false

[ "${1:-}" = "--purge" ] && PURGE=true

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m x\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."

say "Stopping the service"
if systemctl list-unit-files | grep -q "^${APP_NAME}.service"; then
  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl disable --quiet "$APP_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload
  systemctl reset-failed 2>/dev/null || true
  echo "  Service stopped and removed."
else
  warn "No systemd service found — nothing to stop."
fi

if [ "$PURGE" = false ]; then
  say "Done"
  echo "  The service is gone. Your data is still here:"
  echo "    ${APP_DIR}/data/       database and lab manuals (data/sop/)"
  echo "    ${APP_DIR}/.env        secrets"
  echo
  echo "  Reinstall over the top:  sudo ./install/install.sh"
  echo "  Delete everything:       sudo ./install/uninstall.sh --purge"
  exit 0
fi

# --- purge ---------------------------------------------------------------

echo
warn "This will delete the database, the lab manuals and .env."
warn "Every user account, booking and session record goes with it."
read -r -p "Type the word DELETE to confirm: " reply
[ "$reply" = "DELETE" ] || die "Not confirmed — nothing was deleted."

BACKUP="/root/${APP_NAME}-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
say "Backing up first to ${BACKUP}"
tar czf "$BACKUP" -C "$APP_DIR" \
  $( [ -d "$APP_DIR/data" ] && echo data ) \
  $( [ -d "$APP_DIR/static/sop" ] && echo static/sop ) \
  $( [ -f "$APP_DIR/.env" ] && echo .env ) 2>/dev/null || warn "Backup was incomplete."
[ -f "$BACKUP" ] && echo "  Saved. Keep this if there is any doubt."

say "Deleting application data"
rm -rf "$APP_DIR/data" "$APP_DIR/venv" "$APP_DIR/uploads" "$APP_DIR/static/sop"
rm -f  "$APP_DIR/.env"

say "Done"
echo "  Data removed. The source tree is still at ${APP_DIR}."
echo "  To remove that too:  sudo rm -rf ${APP_DIR}"
echo "  Backup kept at:      ${BACKUP}"
