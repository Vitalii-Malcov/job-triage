"""Stage 13A: the client identity that rate limiting keys on.

**Trust model (explicit, one hop, fail closed).** The container runs Uvicorn
with `--no-proxy-headers`, so `request.client.host` is always the real TCP
peer -- Uvicorn never rewrites it from forwarding headers or from an ambient
`FORWARDED_ALLOW_IPS`. This module alone decides whether a forwarded client
address is believed:

1. The immediate peer is NOT in `Settings.trusted_proxy_ips` (the default:
   empty) -> the identity is the peer. Every forwarding header
   (`X-Real-IP`, `X-Forwarded-For`, `Forwarded`, ...) is ignored completely.
2. The immediate peer IS a listed proxy -> exactly ONE header is consulted:
   `X-Real-IP`, which the proxy must OVERWRITE with the real remote address
   (nginx: `proxy_set_header X-Real-IP $remote_addr;`). The value must be a
   single bare IP literal: exactly one header line, no list/comma, no
   whitespace, no port, no IPv6 zone. Anything else -- missing, malformed,
   repeated, a chain -- falls back to the peer (the proxy itself, i.e. one
   shared bucket: degraded, never spoofable).

A bad forwarding header never rejects the request; it simply cannot
influence the identity. `X-Forwarded-For` is deliberately NOT interpreted:
one overwritten single-value header is the whole contract for the initial
host-nginx deployment (docs/DEPLOYMENT.md "Reverse proxy"), and two
competing formats would be two places to get trust wrong.
"""

import ipaddress

from fastapi import Request

FORWARDED_CLIENT_HEADER = "x-real-ip"
UNKNOWN_CLIENT = "unknown"


def _normalize_ip(value: str) -> str | None:
    """Canonical text of one bare IP literal, or None. IPv4-mapped IPv6
    (`::ffff:a.b.c.d`) is folded to its IPv4 form so a dual-stack listener
    and a configured IPv4 proxy address compare equal."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:
            return None
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
    return str(address)


def _forwarded_client(request: Request) -> str | None:
    values = request.headers.getlist(FORWARDED_CLIENT_HEADER)
    if len(values) != 1:
        return None
    value = values[0]
    if not value or value != value.strip() or "," in value or " " in value:
        return None
    return _normalize_ip(value)


def resolve_client_identity(request: Request, trusted_proxy_ips: frozenset[str]) -> str:
    peer = request.client.host if request.client else None
    if not peer:
        return UNKNOWN_CLIENT
    normalized_peer = _normalize_ip(peer)
    if normalized_peer is None or normalized_peer not in trusted_proxy_ips:
        return normalized_peer or peer
    return _forwarded_client(request) or normalized_peer
