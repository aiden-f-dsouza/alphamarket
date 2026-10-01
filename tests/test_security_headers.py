"""security_headers.py (implementation_plan P3 item 6): every response from
every page route carries the security headers, and the HTML itself has no
inline <script> or on*= handler left for CSP script-src 'self' to break."""

import re

import pytest

from alphamarket.config import get_settings

# Same route lists as test_demo_pages.py, so this exercises the same surface
# with DEMO_MODE on and off.
DEMO_ONLY = [
    "/s/weather-cpi-mean-reversion",
    "/s/weather-cpi-mean-reversion/tail",
    "/portfolio",
    "/u/quietedge",
    "/feed",
]
STATIC = [
    "/creators",
    "/invite",
    "/invite?kind=investor",
    "/verification",
    "/methodology",
    "/security",
    "/research",
    "/login",
    "/signup",
]
LEARN = ["/verification", "/transparency", "/research", "/security"]
ALWAYS_ON = [*STATIC, *LEARN, "/", "/marketplace", "/marketplace?view=table", "/transparency"]

REQUIRED_HEADERS = {
    "content-security-policy": [
        "default-src 'self'",
        "script-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'none'",
    ],
    "x-content-type-options": ["nosniff"],
    "referrer-policy": ["strict-origin-when-cross-origin"],
    "permissions-policy": ["camera=()", "microphone=()", "geolocation=()"],
    "x-frame-options": ["DENY"],
}

# A real event-handler attribute: whitespace, "on", letters, optional
# whitespace, "=". Doesn't false-positive on words/attrs merely containing
# "on" (e.g. data-icon=, aria-labelledby=) because those have no preceding
# word boundary directly before "on".
_ON_HANDLER_RE = re.compile(r'\son[a-zA-Z]+\s*=\s*"', re.IGNORECASE)
# An inline <script> block: a <script ...> tag with no src= attribute.
_INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", re.IGNORECASE)


def _assert_headers(resp):
    for header, must_contain in REQUIRED_HEADERS.items():
        value = resp.headers.get(header)
        assert value, (header, resp.request.url)
        for fragment in must_contain:
            assert fragment in value, (header, fragment, value)


def _assert_no_inline_script_or_handlers(html: str, path: str):
    assert not _ON_HANDLER_RE.search(html), (path, _ON_HANDLER_RE.search(html))
    assert not _INLINE_SCRIPT_RE.search(html), path


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def test_headers_present_demo_off(client):
    assert get_settings().demo_mode is False
    for path in ALWAYS_ON:
        resp = client.get(path)
        assert resp.status_code == 200, path
        _assert_headers(resp)
        _assert_no_inline_script_or_handlers(resp.text, path)
    # DEMO_ONLY routes 404 with demo off, but still get the headers.
    for path in DEMO_ONLY:
        resp = client.get(path)
        assert resp.status_code == 404, path
        _assert_headers(resp)


def test_headers_present_demo_on(client, demo_on):
    for path in [*ALWAYS_ON, *DEMO_ONLY]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        _assert_headers(resp)
        _assert_no_inline_script_or_handlers(resp.text, path)


def test_headers_present_on_dashboard_and_connect(client):
    resp = client.post(
        "/signup-form",
        data={"email": "sh@example.com", "password": "hunter2hunter2", "display_name": "SH"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    for path in ["/dashboard", "/dashboard/connect"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        _assert_headers(resp)
        _assert_no_inline_script_or_handlers(resp.text, path)


def test_headers_present_on_404_and_auth_error(client):
    """Headers are added by the outermost middleware, so they show up on
    every response this app sends, not just the happy path."""
    resp = client.get("/no-such-page")
    assert resp.status_code == 404
    _assert_headers(resp)

    resp = client.get("/auth/me")  # 401, not logged in
    assert resp.status_code == 401
    _assert_headers(resp)


def test_hsts_only_when_secure_cookies(client):
    settings = get_settings()
    assert settings.secure_cookies is False
    resp = client.get("/")
    assert "strict-transport-security" not in resp.headers

    settings.secure_cookies = True
    try:
        resp = client.get("/")
        assert "strict-transport-security" in resp.headers
        assert "max-age=" in resp.headers["strict-transport-security"]
    finally:
        settings.secure_cookies = False


def test_static_app_js_is_served_and_referenced(client):
    html = client.get("/").text
    assert '<script src="/static/app.js"></script>' in html
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    content_type = resp.headers["content-type"]
    assert "javascript" in content_type
    # The chart crosshair still reads the server-rendered data-series attribute.
    assert "data-series" in resp.text


def test_invite_copy_button_guarded_on_other_pages(client):
    """app.js's getElementById("copy-link") lookup is guarded, so pages
    without the invite markup don't error (nothing to assert on the response
    itself here beyond it rendering at all -- this pins the guard exists)."""
    js = client.get("/static/app.js").text
    assert 'getElementById("copy-link")' in js
    assert "if (copyBtn)" in js
