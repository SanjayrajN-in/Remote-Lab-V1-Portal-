"""Where the portal is allowed to send outbound requests.

LabPi.ip_address and LabPi.port are written from request input and then used
to build every master-to-node URL (models.LabPi.base_url). Whoever can write
them chooses where this server connects - so they are validated here, at the
point of write, rather than trusted because of who supplied them.

Rejected: anything that is not an IP address, loopback, link-local (which
covers cloud instance metadata at 169.254.169.254), multicast, reserved and
unspecified addresses, and anything outside the configured laboratory
ranges. Ports are limited to the services a bench actually runs.
"""
import ipaddress
import logging

from flask import current_app

log = logging.getLogger(__name__)


class UnsafeNodeAddress(ValueError):
    """The supplied address must not be used for outbound requests."""


def _allowed_networks():
    return [ipaddress.ip_network(c) for c in current_app.config["LAB_NODE_CIDRS"]]


def validate_address(ip_text):
    """Return the address as a string, or raise UnsafeNodeAddress."""
    try:
        ip = ipaddress.ip_address(str(ip_text).strip())
    except (ValueError, TypeError):
        raise UnsafeNodeAddress(f"{ip_text!r} is not an IP address")

    if ip.is_loopback or ip.is_link_local or ip.is_multicast \
            or ip.is_unspecified or ip.is_reserved:
        raise UnsafeNodeAddress(f"{ip} is in a forbidden range")

    if not any(ip in net for net in _allowed_networks()):
        raise UnsafeNodeAddress(f"{ip} is outside the laboratory network")

    return str(ip)


def validate_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise UnsafeNodeAddress(f"{port!r} is not a port number")
    if port not in current_app.config["NODE_ALLOWED_PORTS"]:
        raise UnsafeNodeAddress(f"port {port} is not permitted for a node")
    return port


def safe_address(candidate, fallback=None, context=""):
    """Validated address, or `fallback` - never raises.

    For the machine-to-machine paths, where refusing the whole request would
    take a bench offline over a field the node should not have been setting
    in the first place. The rejection is logged so it is visible.

    Callers pass the *body-supplied* address here and use request.remote_addr
    as the fallback. That asymmetry is deliberate: the body field is
    attacker-chosen and is what made the hijack of F-10 possible, whereas
    remote_addr is the peer the packet actually came from and cannot be set
    by the sender.
    """
    if candidate in (None, ""):
        return fallback
    try:
        return validate_address(candidate)
    except UnsafeNodeAddress as e:
        log.warning("Refused node address%s: %s", f" for {context}" if context else "", e)
        return fallback


def preferred_node_address(reported, peer, existing=None, context=""):
    """The address the portal should dial for this node.

    The peer address wins. It is where the node's packets actually arrive
    from, so by construction the portal can reach it back; a self-reported
    value is a claim about an interface the node happens to prefer, which
    may be on a different network entirely.

    This was originally the other way round - validated self-report first,
    peer address only as a fallback - and it bit immediately: after a node's
    MASTER_URL was repointed at the public hostname, the node recomputed its
    own address from the route to the new master and reported a 192.168.x
    interface. That passed validation, overwrote a working entry, and left
    the portal dialling an address it could not reach.

    A self-reported value is consulted only when the peer address is not
    usable - behind a proxy that strips forwarding headers, for instance.
    """
    return (safe_address(peer, context=context)
            or safe_address(reported, context=context)
            or existing
            or peer)
