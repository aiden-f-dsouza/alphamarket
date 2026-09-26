"""Invariant 4: fixture secrets pushed through every logger and error path
never appear in captured output, stored error strings or HTTP responses."""

import base64
import hashlib
import io
import logging

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import select

from alphamarket import ingest, redaction
from alphamarket.main import create_app
from alphamarket.models import Strategy, User, VenueAccount
from alphamarket.redaction import redact

KALSHI_PEM = (
    rsa.generate_private_key(public_exponent=65537, key_size=2048)
    .private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    .decode()
)
# Kraken API secrets are base64 of 64 bytes (88 chars).
KRAKEN_SECRET = base64.b64encode(hashlib.sha512(b"fake kraken secret").digest()).decode()
# An OAuth access token (Google/Alpaca-style opaque bearer) and a JWT.
OAUTH_TOKEN = "ya29.a0AfB_byC3xQ9Lm2N7pR4sT6vW8yZ1aB3cD5eF7gH9iJ0kL2mN4oP6qR8sT0uV2wX4yZ"
JWT = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwic2NvcGUiOiJyZWFkIn0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
HEX_SECRET = hashlib.sha256(b"hex api secret").hexdigest()  # 64 hex digits
UUID_TOKEN = "79500537-5796-4230-9661-7f7108877c60"  # opaque token: only its field name tells

SECRETS = [KALSHI_PEM, KRAKEN_SECRET, OAUTH_TOKEN, JWT, HEX_SECRET]


def _fragments() -> list[str]:
    """Every recognisable piece: whole secrets plus each PEM body line."""
    body = [ln for ln in KALSHI_PEM.splitlines() if ln and not ln.startswith("-----")]
    return [*SECRETS, *body, KRAKEN_SECRET[:40], OAUTH_TOKEN[5:45], JWT.split(".")[1]]


def _assert_clean(text: str) -> None:
    for fragment in _fragments():
        assert fragment not in text, f"secret fragment leaked: {fragment[:12]}..."


@pytest.fixture()
def captured():
    """A root handler (the worker/app formatter) plus a stderr-style stream."""
    redaction.install()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield stream
    root.removeHandler(handler)
    root.setLevel(old_level)


# ---- the scrubber itself ------------------------------------------------------------


def test_redacts_each_secret_shape():
    for secret in SECRETS:
        assert secret not in redact(f"value: {secret} end")
    assert "[REDACTED PEM]" in redact(KALSHI_PEM)
    # A PEM cut off before its END line (truncated log) is still scrubbed.
    truncated = KALSHI_PEM[: len(KALSHI_PEM) // 2]
    assert truncated.splitlines()[3] not in redact(truncated)
    # A PEM inside JSON/repr, newlines escaped.
    assert KALSHI_PEM.splitlines()[2] not in redact(repr({"pem": KALSHI_PEM}))


def test_redacts_by_field_name_even_for_low_entropy_values():
    assert UUID_TOKEN not in redact(f"access_token={UUID_TOKEN}&x=1")
    assert redact('{"password": "hunter2", "key_id": "abc"}') == (
        '{"password": "[REDACTED]", "key_id": "abc"}'
    )
    assert redact("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"
    assert redact("secret='s3', passphrase=pp") == "secret='[REDACTED]', passphrase=[REDACTED]"
    assert redact("API-Sign: xyz") == "API-Sign: [REDACTED]"


def test_leaves_public_identifiers_alone():
    for public in (
        "0x" + "ab12" * 10,  # EVM wallet address (public)
        "e9efc650-5012-75a0-6383-21440fdfc3aa",  # Kalshi fill / key id (uuid)
        "KXBTCD-25SEP0617-T112499.99",  # Kalshi ticker
        "/trade-api/v2/portfolio/settlements?limit=200",
        "/home/Afd22/koine/src/alphamarket/Ingest2Module/connectors/registry.py",
        "password_hash column",
        "key_id=demo-key-id",
    ):
        assert redact(public) == public, public


def test_extra_patterns_can_be_registered():
    import re

    redaction.add_pattern(re.compile(r"\bTOTPSEED[A-Z2-7]{8}\b"))
    try:
        assert redact("seed TOTPSEEDABCDEFGH") == "seed [REDACTED]"
    finally:
        redaction._extra.pop()


# ---- logger calls -----------------------------------------------------------------


def test_logger_calls_never_emit_secrets(captured, caplog):
    log = logging.getLogger("alphamarket.test_redaction")
    caplog.set_level(logging.DEBUG)
    for secret in SECRETS:
        log.info("connecting with %s", secret)
        log.warning(f"f-string {secret}")
        log.debug("dict arg %s", {"secret": secret, "key_id": "demo"})
        log.error("mapping %(value)s", {"value": secret})
    log.info("headers %r", {"Authorization": f"Bearer {OAUTH_TOKEN}"})
    log.info("token=%s", UUID_TOKEN)  # secret straddles the format string and the arg
    log.info(KRAKEN_SECRET)  # the secret *is* the message
    log.info(ValueError(f"exception object as msg {JWT}"))
    out = captured.getvalue()
    _assert_clean(out)
    assert UUID_TOKEN not in out
    _assert_clean(caplog.text)
    assert "[REDACTED" in out


def test_exception_messages_and_tracebacks_are_scrubbed(captured, capsys):
    log = logging.getLogger("alphamarket.test_redaction")
    for secret in SECRETS:
        try:
            try:
                raise ValueError(f"venue rejected key {secret}")
            except ValueError as inner:
                raise RuntimeError(f"sync failed: {secret}") from inner
        except RuntimeError:
            log.exception("sync failed")
    try:
        raise KeyError(KALSHI_PEM)
    except KeyError:
        log.error("lookup failed", exc_info=True, stack_info=True)
    out = captured.getvalue()
    _assert_clean(out)
    assert "Traceback" in out and "RuntimeError" in out  # still useful
    captured_std = capsys.readouterr()
    _assert_clean(captured_std.out + captured_std.err)


def test_uvicorn_access_log_args_keep_their_shape(captured):
    """uvicorn's AccessFormatter unpacks record.args positionally."""
    from uvicorn.logging import AccessFormatter

    handler = logging.StreamHandler(io.StringIO())
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
    access = logging.getLogger("uvicorn.access.test")
    access.addHandler(handler)
    access.propagate = False
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5",
            "GET",
            f"/cb?code=x&access_token={OAUTH_TOKEN}",
            "1.1",
            200,
        )
    finally:
        access.removeHandler(handler)
    line = handler.stream.getvalue()
    assert "GET /cb?code=x&access_token=[REDACTED]" in line and "200" in line
    _assert_clean(line)


# ---- stored error strings -----------------------------------------------------------


def test_sync_error_stored_on_account_is_scrubbed(session_factory, monkeypatch, captured):
    class Leaky:
        def fetch_fills(self, since=None):
            raise RuntimeError(f"401 for key {KRAKEN_SECRET} / {KALSHI_PEM}")

    monkeypatch.setattr(ingest, "connector_for", lambda account, *a, **k: Leaky())
    with session_factory() as db:
        user = User(email="r@x.com", password_hash="x", display_name="R")
        db.add(user)
        db.flush()
        account = VenueAccount(user_id=user.id, venue="kalshi", venue_env="demo", key_id="k")
        db.add(account)
        db.flush()
        db.add(Strategy(user_id=user.id, venue_account_id=account.id, name="s", slug="s"))
        db.commit()
        with pytest.raises(RuntimeError):
            ingest.sync_account(db, account)
        stored = db.scalars(select(VenueAccount.last_sync_error)).one()
    assert stored.startswith("RuntimeError: 401 for key")
    _assert_clean(stored)


# ---- HTTP error handlers ------------------------------------------------------------


def test_validation_errors_never_echo_the_body(client):
    client.post(
        "/auth/signup",
        json={"email": "v@example.com", "password": "hunter2hunter2", "display_name": "Vee"},
    )
    # Missing strategy_name: FastAPI's default 422 would echo the whole body
    # (PEM included) as `input`.
    resp = client.post(
        "/venues", json={"venue": "kalshi", "key_id": "k", "private_key_pem": KALSHI_PEM}
    )
    assert resp.status_code == 422
    _assert_clean(resp.text)
    detail = resp.json()["detail"]
    assert detail[0]["loc"] == ["body", "strategy_name"]
    assert "input" not in detail[0]
    # Field-level error on the secret itself.
    resp = client.post(
        "/venues",
        json={"venue": "kalshi", "key_id": "", "secret": KRAKEN_SECRET, "strategy_name": "x"},
    )
    assert resp.status_code == 422
    _assert_clean(resp.text)


def test_unhandled_errors_are_logged_scrubbed_and_answered_generically(captured):
    app = create_app()

    @app.get("/boom")
    def boom():
        raise RuntimeError(f"venue said: bad secret {KRAKEN_SECRET} for {KALSHI_PEM}")

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/boom", headers={"Authorization": f"Bearer {OAUTH_TOKEN}"})
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal Server Error"}
    _assert_clean(resp.text)
    out = captured.getvalue()
    assert "unhandled error on GET /boom" in out and "RuntimeError" in out
    _assert_clean(out)
