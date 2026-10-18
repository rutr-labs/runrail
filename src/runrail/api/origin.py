"""Refuse state-changing requests that come from another website.

RunRail has no accounts, by design, and a browser sends a POST to
127.0.0.1:8080 just as willingly from a page on evil.example as from the UI.
A "simple" cross-site request (no custom headers, a plain body) needs no
preflight, so the API's JSON routes — and the restore route, which applies
workflows whose tasks are shell commands — were reachable from any tab.

Browsers put the page's Origin on every cross-site POST, PUT, PATCH and
DELETE. If it names a different site than the one being asked, the request is
refused. No Origin (curl, the CLI, a same-origin GET) passes. A loopback
origin is allowed to speak to a loopback host, which is what the Vite dev
proxy looks like from here.
"""

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


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


class SameOriginMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in _UNSAFE and cross_site(request.headers.get("origin"),
                                                     request.headers.get("host")):
            return JSONResponse({"detail": "Cross-site requests are not accepted"}, status_code=403)
        return await call_next(request)
