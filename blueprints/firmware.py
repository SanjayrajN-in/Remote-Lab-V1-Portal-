"""Firmware upload gateway.

The student uploads to the portal, never to the Pi directly. The flow:

    student -> POST /session/<key>/firmware   (this blueprint)
            -> validate + malware sniff       (services/firmware.py)
            -> store a copy on the portal      (audit trail)
            -> forward to the node's /flash    (services/nodes.flash)

A key is required, and it must be a live session belonging to the person
uploading - so a leaked upload URL is useless once the slot ends, and one
student cannot flash another's bench.
"""
import hashlib
import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from models import FirmwareUpload, Session, SystemLog, db, utcnow
from services import firmware, nodes

log = logging.getLogger(__name__)
bp = Blueprint("firmware", __name__)


def _firmware_dir():
    d = Path(current_app.config["UPLOAD_FOLDER"]) / "firmware"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _live_session(key):
    s = Session.query.filter_by(session_key=key).first()
    if not s or not s.is_live:
        return None
    return s


@bp.post("/session/<key>/firmware")
def upload(key):
    """Accept a firmware file for a live session and forward it to the bench."""
    session = _live_session(key)
    if not session:
        return jsonify({"status": "error",
                        "message": "This session has ended or the link is invalid."}), 403

    fw = request.files.get("firmware") or request.files.get("file")
    if not fw or not fw.filename:
        return jsonify({"status": "error", "message": "No file was chosen."}), 400

    board = (request.form.get("board")
             or (session.node.board if session.node else None)
             or "generic")

    data = fw.read()
    record = FirmwareUpload(
        session_id=session.id, user_id=session.user_id,
        lab_pi_id=session.lab_pi_id, original_name=fw.filename,
        stored_name="", board=board, size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )

    # 1. Validate. A rejection is recorded, so repeated bad uploads are visible.
    try:
        firmware.validate(fw.filename, data, board)
    except firmware.Rejected as e:
        record.status = "rejected"
        record.reason = str(e)
        db.session.add(record)
        # A suspicious rejection (disguised executable/script/archive, or a
        # real malware-scanner hit) is not the same as an honest mistake
        # (wrong extension, wrong board, a truncated file) - log it louder
        # so it doesn't blend into routine warnings on the activity log.
        SystemLog.write(
            (f"Firmware from {session.user.email} rejected as SUSPICIOUS "
             f"(not a firmware file): {e}") if e.suspicious else
            f"Firmware from {session.user.email} rejected: {e}",
            level="error" if e.suspicious else "warning",
            category="firmware", user_id=session.user_id)
        db.session.commit()
        return jsonify({"status": "rejected", "message": str(e),
                        "suspicious": e.suspicious}), 422

    # 2. Store a copy on the portal - the audit trail, and the source of truth
    #    if the forward has to be retried.
    safe = secure_filename(fw.filename) or "firmware.bin"
    stored = f"{session.session_key}_{int(utcnow().timestamp())}_{safe}"
    (_firmware_dir() / stored).write_bytes(data)
    record.stored_name = stored
    db.session.add(record)
    db.session.commit()

    # 3. Forward to the node's /flash.
    ok, message = nodes.flash(session, data, safe, board)
    record.status = "forwarded" if ok else "failed"
    record.reason = None if ok else message
    record.forwarded_at = utcnow() if ok else None
    SystemLog.write(
        f"Firmware {safe} from {session.user.email} "
        + ("forwarded to " + (session.node.node_id if session.node else "?")
           if ok else f"failed to forward: {message}"),
        level="info" if ok else "error",
        category="firmware", user_id=session.user_id)
    db.session.commit()

    if not ok:
        return jsonify({"status": "error",
                        "message": f"The file passed checks but the bench didn't "
                                   f"accept it: {message}"}), 502

    # 4. .out/.elf files carry debug symbols alongside the flashable image -
    # feed the same upload to the debugger's symbol slot too, so a student
    # debugging this board doesn't have to pick the identical file a second
    # time in the Debug panel (see templates/portal/lab.html's Symbols
    # section). Best-effort and silent: most flashes aren't debug sessions,
    # and a board with no debug profile has nowhere to send this anyway.
    debug_capable = safe.lower().endswith((".out", ".elf"))
    if debug_capable:
        nodes.upload_debug_elf(session, data, safe)

    return jsonify({"status": "ok",
                    "message": f"{safe} accepted and sent to the bench for flashing.",
                    "upload_id": record.id,
                    "debug_symbols": debug_capable})
