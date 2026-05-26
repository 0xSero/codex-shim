"""Host-header allowlist for the loopback shim server.

Binding to 127.0.0.1 keeps the shim off the network, but it does not stop a
web page the user is already viewing from reaching it. A browser resolves an
attacker-controlled domain to 127.0.0.1 (DNS rebinding) and then issues
same-origin requests to the shim; the loopback socket happily accepts them.
Because the shim forwards each request upstream with the user's BYOK API keys
or ChatGPT access token, an unguarded shim lets any visited page spend the
user's model credits and read the responses.

The browser still sends the attacker's domain in the ``Host`` header, so a
Host allowlist is the standard defense: accept loopback names plus whatever
bind host the operator configured, reject everything else.
"""

from __future__ import annotations

import os

from aiohttp import web

DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ALLOWED_HOSTS_ENV = "CODEX_SHIM_ALLOWED_HOSTS"
_WILDCARD_BINDS = frozenset({"", "0.0.0.0", "::"})


def host_only(host_header: str) -> str:
    """Return the hostname from a ``Host`` header value, port stripped."""
    value = (host_header or "").strip()
    if not value:
        return ""
    if value.startswith("["):  # bracketed IPv6 literal, e.g. [::1]:8765
        end = value.find("]")
        if end != -1:
            return value[1:end]
        return value
    if value.count(":") == 1:  # host:port (bare IPv6 has 2+ colons)
        return value.rsplit(":", 1)[0]
    return value


def build_allowed_hosts(bind_host: str) -> set[str]:
    """Loopback names + the configured bind host + the env allowlist."""
    allowed = {host.lower() for host in DEFAULT_ALLOWED_HOSTS}
    bind = (bind_host or "").strip().lower()
    if bind and bind not in _WILDCARD_BINDS:
        allowed.add(bind)
    for part in os.environ.get(ALLOWED_HOSTS_ENV, "").split(","):
        part = part.strip().lower()
        if part:
            allowed.add(part)
    return allowed


def host_guard_middleware(allowed_hosts: set[str]):
    """aiohttp middleware that rejects requests with a non-allowlisted Host."""
    allowed = {host.lower() for host in allowed_hosts}

    @web.middleware
    async def _guard(request: web.Request, handler):
        hostname = host_only(request.headers.get("Host", "")).lower()
        if hostname not in allowed:
            raise web.HTTPForbidden(text="Forbidden: Host header not allowed")
        return await handler(request)

    return _guard
