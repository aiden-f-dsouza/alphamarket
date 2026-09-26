"""Security response headers (platform_plan 2.7 item 6, implementation_plan
P3 item 6): CSP, HSTS, frame/sniff/referrer/permissions policy, on every
response this app sends, including error responses (the middleware wraps the
whole ASGI app, outside CSRFMiddleware and SessionMiddleware).

CSP notes:
- `script-src 'self'`: every inline `<script>` was moved to
  `static/app.js`, served via StaticFiles and referenced with a `<script
  src>` tag, and the two inline `on*=` handlers (dashboard disconnect
  confirm, marketplace incubating-toggle auto-submit) became
  `data-confirm` / `data-autosubmit` attributes app.js reads instead.
- `style-src ... 'unsafe-inline'`: templates lean on `style="..."`
  attributes throughout (layout one-offs) and base.html's <head> has one
  inline `<style>` block for the design tokens; neither is worth a nonce
  plumbed through every render for this MVP, so styles keep 'unsafe-inline'.
  Scripts do not: that is the actual XSS-relevant half of CSP.
- `font-src`/`style-src` include Google Fonts (Newsreader, IBM Plex), the
  only third-party origin any page loads from.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from alphamarket.config import get_settings

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'"
)

STRICT_TRANSPORT_SECURITY = "max-age=63072000; includeSubDomains"
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: adds headers to every `http.response.start`
    message. Added last in main.py so it is the outermost user middleware
    (see the ordering comment there) and its headers are never overwritten
    by an inner middleware or the app."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("content-security-policy", CONTENT_SECURITY_POLICY)
                headers.append("x-content-type-options", "nosniff")
                headers.append("referrer-policy", "strict-origin-when-cross-origin")
                headers.append("permissions-policy", PERMISSIONS_POLICY)
                headers.append("x-frame-options", "DENY")
                if get_settings().secure_cookies:
                    headers.append("strict-transport-security", STRICT_TRANSPORT_SECURITY)
            await send(message)

        await self.app(scope, receive, send_wrapper)
