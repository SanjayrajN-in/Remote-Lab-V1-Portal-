"""The Lab Pi side of the wire.

remote_lab_pi already runs `lab_pi_session_poller.py`, which expects a master
that can answer "what sessions do I have?". These endpoints are that master.

  POST /api/node/register            node announces itself and its IP
  POST /api/node/heartbeat           telemetry, every 30s
  GET  /api/node/<node_id>/sessions  poller: sessions this node should honour
  POST /api/node/session/validate    node checks a key a browser presented
  POST /api/node/session/end         node reports a session finished

Every request carries X-Node-Secret. Register uses only the shared secret;
after that a node also presents its own X-Node-Token, so one leaked node
cannot impersonate another.
"""
import hmac
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from models import (Booking, Experiment, LabPi, LabPiHeartbeat, Session,
                    SystemLog, db, utcnow)

bp = Blueprint("node_api", __name__, url_prefix="/api/node")


def _secret_ok():
    given = request.headers.get("X-Node-Secret", "")
    return hmac.compare_digest(given, current_app.config["NODE_SHARED_SECRET"])


def require_secret(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _secret_ok():
            return jsonify({"error": "bad or missing node secret"}), 401
        return f(*args, **kwargs)
    return wrapper


def _authenticated_node(node_id):
    """Resolve a node and check its own token."""
    node = LabPi.query.filter_by(node_id=node_id).first()
    if not node:
        return None, (jsonify({"error": f"unknown node {node_id}"}), 404)
    token = request.headers.get("X-Node-Token", "")
    if not hmac.compare_digest(token, node.api_token or ""):
        return None, (jsonify({"error": "bad node token"}), 401)
    return node, None


# --------------------------------------------------------------------------

@bp.post("/register")
@require_secret
def register():
    """A node announcing itself, typically at boot.

    Re-registration is normal and idempotent: DHCP hands out a new address
    and the node tells us, so the admin never has to chase IPs by hand.
    """
    data = request.get_json(silent=True) or {}
    node_id = (data.get("node_id") or "").strip()
    if not node_id:
        return jsonify({"error": "node_id is required"}), 400

    ip = data.get("ip") or request.remote_addr
    node = LabPi.query.filter_by(node_id=node_id).first()
    created = node is None
    if created:
        node = LabPi(node_id=node_id, name=data.get("name") or node_id, ip_address=ip)
        db.session.add(node)

    node.ip_address = ip
    node.port = int(data.get("port") or node.port or 5000)
    node.name = data.get("name") or node.name
    node.board = data.get("board") or node.board
    node.location = data.get("location") or node.location
    node.last_seen = utcnow()

    slug = data.get("experiment_slug")
    if slug:
        exp = Experiment.query.filter_by(slug=slug).first()
        if exp:
            node.experiment_id = exp.id

    SystemLog.write(f"Node {node_id} {'registered' if created else 'checked in'} from {ip}",
                    category="device")
    db.session.commit()

    body = {
        "ok": True,
        "created": created,
        "node_id": node.node_id,
        "experiment": node.experiment.slug if node.experiment else None,
        "heartbeat_seconds": 30,
    }
    if created:
        # Only on genuine first registration. Returning it on every re-register
        # meant one shared secret could read every node's own token, defeating
        # the point of having a per-node credential at all. An existing node
        # that has lost its token needs one reissued from the admin console.
        body["api_token"] = node.api_token
    return jsonify(body)


@bp.post("/heartbeat")
@require_secret
def heartbeat():
    data = request.get_json(silent=True) or {}
    node, err = _authenticated_node((data.get("node_id") or "").strip())
    if err:
        return err

    node.last_seen = utcnow()
    if data.get("ip") and data["ip"] != node.ip_address:
        node.ip_address = data["ip"]

    db.session.add(LabPiHeartbeat(
        lab_pi_id=node.id,
        cpu_percent=data.get("cpu"), ram_percent=data.get("ram"),
        temperature_c=data.get("temp"),
        battery_percent=data.get("battery_percent"),
        battery_status=data.get("battery_status"),
        uptime_seconds=data.get("uptime"),
        serial_connected=bool(data.get("serial_connected")),
    ))

    # Keep the table bounded: 200 samples per node is roughly 100 minutes.
    old = (LabPiHeartbeat.query.filter_by(lab_pi_id=node.id)
           .order_by(LabPiHeartbeat.recorded_at.desc()).offset(200).all())
    for row in old:
        db.session.delete(row)

    db.session.commit()
    return jsonify({"ok": True, "server_time": utcnow().isoformat()})


@bp.get("/<node_id>/sessions")
@require_secret
def node_sessions(node_id):
    """What lab_pi_session_poller.py polls for.

    Returned keys are the only ones the node should admit. Anything the node
    holds that is absent here has been cancelled or has expired.
    """
    node, err = _authenticated_node(node_id)
    if err:
        return err

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
@require_secret
def validate_session():
    """A browser arrived at the node with ?key=... - is it good?"""
    data = request.get_json(silent=True) or {}
    key = (data.get("session_key") or "").strip()
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

    node_id = (data.get("node_id") or "").strip()
    if node_id and s.node and s.node.node_id != node_id:
        return jsonify({"valid": False,
                        "reason": "this key belongs to a different node"}), 403

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
@require_secret
def end_session():
    """The node reporting that a session finished on its side."""
    data = request.get_json(silent=True) or {}
    s = Session.query.filter_by(session_key=(data.get("session_key") or "").strip()).first()
    if not s:
        return jsonify({"error": "unknown session key"}), 404

    s.status = "completed"
    s.ended_at = utcnow()
    if s.booking:
        s.booking.status = "completed"
        s.booking.completed_at = utcnow()
    SystemLog.write(f"Node reported session {s.session_key} finished", category="session")
    db.session.commit()
    return jsonify({"ok": True})
