"""Compatibility layer for Lab Pis that already speak the older API.

Nodes running the existing `remote_lab_pi` post to `/api/lab-pi/...` rather
than `/api/node/...`, and they do not send the shared secret. Making every Pi
in the lab change at once is not realistic, so this blueprint accepts them as
they are and writes into the same tables.

Two things this has to be tolerant about, because the payloads come from
firmware that is already deployed and cannot be assumed:

  * **Field names.** `cpu`, `cpu_percent` and `cpu_usage` all mean the same
    thing. `_pick()` accepts any of them.
  * **Identity.** A node may call itself `node_id`, `lab_pi_id`, `id` or just
    `name`. Whatever it sends becomes its ID, and an unknown node is
    registered on first contact rather than rejected - otherwise a working Pi
    would sit there 404-ing until someone noticed.

The first payload from each node is logged in full at INFO, so you can see
exactly what your hardware sends and tighten this up later.

Security note: every endpoint here requires the shared secret, presented as
either `X-Master-Api-Key` or `X-Node-Secret`. `LEGACY_NODE_COMPAT` controls
only whether these older *paths* are served at all; it has never been able to
waive authentication since the F-01 fix, and it now defaults to off.
"""
import hmac
import logging

from flask import Blueprint, current_app, jsonify, request

from models import (Experiment, LabPi, LabPiHeartbeat, Session, SystemLog, db,
                    utcnow)

log = logging.getLogger(__name__)
bp = Blueprint("legacy_api", __name__, url_prefix="/api/lab-pi")

# Node IDs whose payload has already been logged this process.
_seen = set()


def _pick(data, *names, default=None):
    """First present, non-empty value among several possible field names."""
    for n in names:
        if n in data and data[n] not in (None, ""):
            return data[n]
    return default


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _allowed():
    """The deployed Pi sends X-Master-Api-Key; newer nodes send X-Node-Secret.

    Either is accepted against the same configured secret. A header that is
    present but wrong is always refused; only a *missing* one depends on
    LEGACY_NODE_COMPAT.
    """
    secret = current_app.config["NODE_SHARED_SECRET"]
    for header in ("X-Master-Api-Key", "X-Node-Secret"):
        given = request.headers.get(header)
        if given:
            return hmac.compare_digest(given, secret)
    # No anonymous fallback, ever. This used to return LEGACY_NODE_COMPAT,
    # which defaulted to True - so a missing header authenticated the caller
    # and every endpoint below was open to anyone who could reach the port.
    return False


def _identify(data):
    """Find or create the node this request came from."""
    node_id = _pick(data, "lab_pi_id", "node_id", "labPiId", "id") \
        or request.headers.get("X-Lab-Pi-Id") or _pick(data, "name")
    if not node_id:
        return None, (jsonify({"error": "no node id in payload"}), 400)
    node_id = str(node_id).strip()

    node = LabPi.query.filter_by(node_id=node_id).first()
    if node is None:
        # A live Pi that is not in the database yet gets registered rather
        # than refused, so hardware is never silently invisible.
        node = LabPi(
            node_id=node_id,
            name=str(_pick(data, "name", "lab_pi_name", default=node_id)),
            ip_address=str(_pick(data, "ip", "ip_address", default=request.remote_addr)),
            port=int(_number(_pick(data, "port", default=5000)) or 5000),
            board=str(_pick(data, "board", "board_type", default="arduino")),
        )
        db.session.add(node)
        # Flush so the row gets its primary key: a heartbeat written in the
        # same request needs node.id, which is NULL until this happens.
        db.session.flush()
        SystemLog.write(f"Legacy node {node_id} auto-registered from "
                        f"{request.remote_addr}", level="warning", category="device")
        log.info("Auto-registered legacy node %s from %s", node_id, request.remote_addr)
    return node, None


def _log_first_payload(node_id, data):
    if node_id not in _seen:
        _seen.add(node_id)
        log.info("First legacy payload from %s: %s", node_id, data)


# --------------------------------------------------------------------------

@bp.post("/register")
def register():
    if not _allowed():
        return jsonify({"error": "bad node secret"}), 401
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    node, err = _identify(data)
    if err:
        return err

    _log_first_payload(node.node_id, data)

    node.ip_address = str(_pick(data, "ip", "ip_address", default=request.remote_addr))
    node.name = str(_pick(data, "name", default=node.name))
    node.board = str(_pick(data, "board", "board_type", default=node.board))
    node.last_seen = utcnow()

    node.location = str(_pick(data, "location", default=node.location or "")) or None

    # The deployed Pi sends a numeric experiment_id; newer ones send a slug.
    exp_id = _pick(data, "experiment_id")
    slug = _pick(data, "experiment_slug", "experiment", "experiment_name")
    if exp_id is not None:
        exp = Experiment.query.get(int(_number(exp_id) or 0))
        if exp:
            node.experiment_id = exp.id
    elif slug:
        exp = (Experiment.query.filter_by(slug=str(slug)).first()
               or Experiment.query.filter_by(name=str(slug)).first())
        if exp:
            node.experiment_id = exp.id

    db.session.commit()
    # api_token is deliberately not returned. Echoing it here let anyone who
    # could reach this endpoint read any node's per-node credential just by
    # naming it. Legacy nodes authenticate with the shared secret and never
    # used this value; new nodes get theirs from /api/node/register.
    return jsonify({"ok": True, "status": "registered", "node_id": node.node_id,
                    "heartbeat_seconds": 30})


@bp.post("/heartbeat")
def heartbeat():
    if not _allowed():
        return jsonify({"error": "bad node secret"}), 401
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    node, err = _identify(data)
    if err:
        return err

    _log_first_payload(node.node_id, data)

    reported_ip = _pick(data, "ip", "ip_address")
    node.ip_address = str(reported_ip) if reported_ip else (node.ip_address or request.remote_addr)
    node.last_seen = utcnow()

    battery = _number(_pick(data, "battery_soc", "battery_percent", "battery",
                            "batteryPercent", "soc"))
    status = _pick(data, "battery_ac_status", "battery_status", "power_source",
                   "power", "batteryStatus")
    if status is not None:
        status = "AC" if str(status).strip().lower() in (
            "ac", "ac_connected", "mains", "plugged", "true", "1") else "Battery"

    db.session.add(LabPiHeartbeat(
        lab_pi_id=node.id,
        cpu_percent=_number(_pick(data, "cpu_usage", "cpu", "cpu_percent")),
        ram_percent=_number(_pick(data, "ram_usage", "ram", "ram_percent",
                                  "memory", "memory_percent")),
        temperature_c=_number(_pick(data, "temperature", "temp",
                                    "temperature_c", "cpu_temp")),
        battery_percent=battery,
        battery_status=status,
        uptime_seconds=int(_number(_pick(data, "uptime", "uptime_seconds")) or 0) or None,
        serial_connected=bool(_pick(data, "serial_connected", "serial", default=False)),
    ))

    stale = (LabPiHeartbeat.query.filter_by(lab_pi_id=node.id)
             .order_by(LabPiHeartbeat.recorded_at.desc()).offset(200).all())
    for row in stale:
        db.session.delete(row)

    db.session.commit()

    # The Pi reads any pending session straight out of the heartbeat reply,
    # so a node that never polls still gets its session.
    reply = {"ok": True, "status": "ok", "server_time": utcnow().isoformat()}
    live = (Session.query
            .filter(Session.lab_pi_id == node.id, Session.status == "active",
                    Session.expires_at > utcnow())
            .order_by(Session.created_at.desc()).first())
    if live:
        reply["new_session"] = True
        reply["session"] = {
            "session_key": live.session_key,
            "start_time": live.starts_at.isoformat(),
            "end_time": live.expires_at.isoformat(),
            "user_email": live.user.email,
            "booking_id": live.booking.code if live.booking else None,
            "board_type": (live.node.board if live.node else "arduino"),
        }
    return jsonify(reply)


@bp.get("/<node_id>/active-session")
def active_session(node_id):
    """What lab_pi_session_poller.py polls.

    It expects {"status": "running"|"stopped"} and turns the relay on or off
    accordingly, so "stopped" is a normal answer, not an error.
    """
    if not _allowed():
        return jsonify({"error": "bad node secret"}), 401
    node = LabPi.query.filter_by(node_id=node_id).first()
    if not node:
        # The poller prints a helpful "not registered" message on a 404.
        return jsonify({"status": "stopped",
                        "error": f"lab pi {node_id} is not registered"}), 404

    node.last_seen = utcnow()
    db.session.commit()

    live = (Session.query
            .filter(Session.lab_pi_id == node.id, Session.status == "active",
                    Session.expires_at > utcnow())
            .order_by(Session.created_at.desc()).first())
    if not live:
        return jsonify({"status": "stopped"})

    return jsonify({
        "status": "running",
        "session_key": live.session_key,
        "start_time": live.starts_at.isoformat(),
        "end_time": live.expires_at.isoformat(),
        "user_email": live.user.email,
        "booking_id": live.booking.code if live.booking else None,
        "board_type": node.board or "arduino",
    })


@bp.get("/<node_id>/sessions")
def sessions(node_id):
    if not _allowed():
        return jsonify({"error": "bad node secret"}), 401
    node = LabPi.query.filter_by(node_id=node_id).first()
    if not node:
        return jsonify({"node_id": node_id, "sessions": []})

    node.last_seen = utcnow()
    db.session.commit()

    now = utcnow()
    live = (Session.query
            .filter(Session.lab_pi_id == node.id, Session.status == "active",
                    Session.expires_at > now)
            .all())
    return jsonify({
        "node_id": node.node_id,
        "server_time": now.isoformat(),
        "sessions": [{
            "session_key": s.session_key,
            "user": s.user.full_name,
            "user_email": s.user.email,
            "experiment": s.experiment.name,
            "starts_at": s.starts_at.isoformat(),
            "expires_at": s.expires_at.isoformat(),
            "seconds_left": s.seconds_left,
        } for s in live],
    })


@bp.post("/session/validate")
def validate():
    if not _allowed():
        return jsonify({"error": "bad node secret"}), 401
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    key = str(_pick(data, "session_key", "key", "session", default="")).strip()
    s = Session.query.filter_by(session_key=key).first()

    if not s:
        return jsonify({"valid": False, "reason": "unknown session key"}), 404
    if s.status != "active":
        return jsonify({"valid": False, "reason": f"session is {s.status}"}), 403
    if s.seconds_left <= 0:
        s.status = "expired"
        if s.booking:
            s.booking.status = "completed"
            s.booking.completed_at = utcnow()
        db.session.commit()
        return jsonify({"valid": False, "reason": "session has expired"}), 403

    return jsonify({
        "valid": True,
        "user": s.user.full_name,
        "user_email": s.user.email,
        "experiment": s.experiment.name,
        "experiment_slug": s.experiment.slug,
        "seconds_left": s.seconds_left,
        "expires_at": s.expires_at.isoformat(),
    })


@bp.post("/session/end")
def end():
    if not _allowed():
        return jsonify({"error": "bad node secret"}), 401
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    key = str(_pick(data, "session_key", "key", default="")).strip()
    s = Session.query.filter_by(session_key=key).first()
    if not s:
        return jsonify({"error": "unknown session key"}), 404

    s.status = "completed"
    s.ended_at = utcnow()
    if s.booking:
        s.booking.status = "completed"
        s.booking.completed_at = utcnow()
    db.session.commit()
    return jsonify({"ok": True})
