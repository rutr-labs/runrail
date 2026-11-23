"""Refuse requests that a page on another website is making.

RunRail has no accounts, by design, so the browser is the perimeter. Two
checks hold it, on every HTTP request and every WebSocket handshake:

The Host header must be a name RunRail answers to. DNS rebinding points a
domain the attacker controls at 127.0.0.1: their page then talks to this
RunRail under their own name, Origin and Host agree, and the same-origin
check below has nothing to object to. Loopback names and IP addresses pass
(rebinding needs a domain name, and a page served from an IP address is a
page served by whatever answers there); any other name must be the host of
RUNRAIL_PUBLIC_URL, RUNRAIL_HOST, or listed in RUNRAIL_ALLOWED_HOSTS.

A GET from another site is refused too, when the browser says that is what
it is (Sec-Fetch-Site: cross-site, sent by every current browser). The
response cannot be read across sites, but the request still runs: a page
could make RunRail do work for it (a log search with a pathological pattern
holds the process) or drop a backup with the secrets key into Downloads. The
exception is a report page opened as a link from elsewhere, which is what the
pinnable report URL is for.

The Origin must not name another site. Browsers put the page's Origin on
every cross-site POST, PUT, PATCH and DELETE, and on every WebSocket
handshake, which the same-origin policy does not otherwise restrict: a
"simple" cross-site POST needs no preflight, and a WebSocket from any tab
could read live logs and run events. No Origin (curl, the CLI) passes. A
loopback origin is allowed to speak to a loopback host, which is what the
Vite dev proxy looks like from here.
"""

import ipaddress
import re
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from runrail.config import Settings, get_settings

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
#: Addresses a server listens on, not names a browser asks for.
_WILDCARDS = {"0.0.0.0", "::", "[::]", ""}
_SAFE = {"GET", "HEAD"}
#: API pages meant to be linked from a wiki or a chat: a rendered report,
#: which carries its own sandboxing headers (see reports.REPORT_CSP).
_LINKABLE = re.compile(r"/api/(runs/\d+/report|workflows/[^/]+/latest-report/html)")

#: On every response that does not set its own. RunRail's pages must not be
#: framed by another site, where a click on Approve or Run now could be
#: steered (an approval gate is the one human check in a run); report pages
#: keep their own, stricter policy.
SECURITY_HEADERS = {
    b"x-frame-options": b"SAMEORIGIN",
    b"content-security-policy": b"frame-ancestors 'self'",
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"same-origin",
}


def _host_of(netloc: str) -> str:
    netloc = netloc.lower()
    if netloc.startswith("["):  # [::1]:8080
        return netloc.split("]")[0] + "]"
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


def cross_site(origin: str | None, host: str | None) -> bool:
    if not origin or origin == "null" or not host:
        return bool(origin == "null")
    origin_netloc = urlsplit(origin).netloc.lower()
    host = host.lower()
    if origin_netloc == host:
        return False
    return not (_host_of(origin_netloc) in _LOOPBACK and _host_of(host) in _LOOPBACK)


def cross_site_read(method: str, path: str, headers: Headers) -> bool:
    """A GET or HEAD to the API that the browser says another site made, other
    than following a link to a report page."""
    if method not in _SAFE or not path.startswith("/api/"):
        return False
    if headers.get("sec-fetch-site") != "cross-site":
        return False
    return not (headers.get("sec-fetch-mode") == "navigate" and _LINKABLE.fullmatch(path))


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return False
    return True


def known_hosts(settings: Settings) -> set[str]:
    """The names, besides loopback and IP addresses, this RunRail answers to."""
    names = {settings.host.lower(), *settings.allowed_host_names}
    if settings.public_url:
        names.add((urlsplit(settings.public_url).hostname or "").lower())
    return names - _WILDCARDS


def allowed_host(host: str | None, settings: Settings) -> bool:
    if not host:  # HTTP/1.0 and raw clients send none; a browser always does
        return True
    name = _host_of(host).rstrip(".")
    if name == "localhost" or name.endswith(".localhost") or _is_ip(name):
        return True
    names = known_hosts(settings)
    return "*" in names or name in names


class SameOriginMiddleware:
    """Pure ASGI rather than BaseHTTPMiddleware, which never sees a WebSocket
    handshake — the reason the WebSocket routes once accepted any site."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host, origin = headers.get("host"), headers.get("origin")
        if not allowed_host(host, get_settings()):
            detail = (f"RunRail does not answer to the name {_host_of(host or '')!r}. If that is "
                      "how you reach it, set RUNRAIL_PUBLIC_URL or add it to RUNRAIL_ALLOWED_HOSTS")
        elif (scope["type"] == "websocket" or scope["method"] in _UNSAFE) and cross_site(origin, host):
            detail = "Cross-site requests are not accepted"
        elif scope["type"] == "http" and cross_site_read(scope["method"], scope["path"], headers):
            detail = "Cross-site requests are not accepted"
        else:
            await self.app(scope, receive, _with_security_headers(send) if scope["type"] == "http" else send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008, reason=detail[:120])(scope, receive, send)
        else:
            await JSONResponse({"detail": detail}, status_code=403)(scope, receive, _with_security_headers(send))


def _with_security_headers(send: Send) -> Send:
    """Add SECURITY_HEADERS to a response, leaving any it already sets."""
    async def wrapped(message):
        if message["type"] == "http.response.start":
            present = {name.lower() for name, _ in message.get("headers", [])}
            extra = [(name, value) for name, value in SECURITY_HEADERS.items() if name not in present]
            if extra:
                message = {**message, "headers": [*message.get("headers", []), *extra]}
        await send(message)
    return wrapped
