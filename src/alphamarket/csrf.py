"""CSRF protection: Origin/Referer check on cookie-authenticated unsafe
methods (platform_plan 2.7 item 5, implementation_plan P3 item 5).

SameSite=lax alone is not sufficient defense (some top-level cross-site
navigations still attach the cookie, and not every client honours
SameSite), so this is the second layer: any POST/PUT/PATCH/DELETE that
carries a session cookie must also carry an Origin header (or, when the
browser omitted Origin, a Referer) whose origin matches
`settings.public_base_url` or one of `settings.csrf_allowed_origin_list`.
Anything else is rejected with 403.

A request with NO session cookie passes through unchecked: it is not
cookie-authenticated, so there is no session to forge into (a bare JSON API
call, a future webhook). This also means the very first login/signup POST
(before any cookie has been issued) is never blocked.

The auth agent is renaming the session cookie away from starlette
SessionMiddleware's default ("session"); this checks both that default and
`settings.session_cookie_name`, so the check keeps working either way and
never needs to know which one is actually live.
"""

from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from alphamarket.config import get_settings
from alphamarket.sessions import COOKIE_NAME as AUTH_SESSION_COOKIE

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_SESSION_COOKIE = "session"  # starlette.middleware.sessions.SessionMiddleware default


def _origin_of(url: str) -> str | None:
    """scheme://host[:port] of `url`, or None if it does not parse as one."""
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _has_session_cookie(request: Request, cookie_names: frozenset[str]) -> bool:
    return any(name in request.cookies for name in cookie_names)


class CSRFMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware overhead needed): runs
    before SessionMiddleware parses anything, since it only ever inspects the
    raw Cookie/Origin/Referer headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        if request.method in UNSAFE_METHODS:
            settings = get_settings()
            cookie_names = frozenset(
                {DEFAULT_SESSION_COOKIE, AUTH_SESSION_COOKIE, settings.session_cookie_name}
            )
            if _has_session_cookie(request, cookie_names):
                allowed = {_origin_of(settings.public_base_url)}
                allowed.update(_origin_of(o) for o in settings.csrf_allowed_origin_list)
                allowed.discard(None)

                origin = request.headers.get("origin") or _origin_of(
                    request.headers.get("referer", "")
                )
                if origin is None or origin not in allowed:
                    response = PlainTextResponse(
                        "CSRF check failed: missing or mismatched Origin/Referer",
                        status_code=403,
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
