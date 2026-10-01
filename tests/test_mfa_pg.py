"""TOTP replay guard against real Postgres: a request whose session already
holds a stale copy of the user's TOTP row (as `totp_enabled` leaves it) must
still see a code another request just spent."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pyotp
import pytest

from alphamarket import mfa
from alphamarket.models import User, UserTotp
from alphamarket.ratelimit import MemoryLimiter
from alphamarket.vault import encrypt_secret

pytestmark = pytest.mark.pg

SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 seed, base32


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rate_limiter=MemoryLimiter())))


def test_second_request_with_stale_row_cannot_replay_a_code(pg_session_factory):
    now = datetime.fromtimestamp(2_000_000_010, UTC)  # step floor(2_000_000_010 / 30) = 66_666_667
    code = pyotp.TOTP(SECRET).at(now)
    with pg_session_factory() as setup:
        user = User(email="replay-pg@example.com", password_hash="x", display_name="Pg")
        setup.add(user)
        setup.flush()
        setup.add(
            UserTotp(user_id=user.id, secret_encrypted=encrypt_secret(SECRET), enabled_at=now)
        )
        setup.commit()
        uid = user.id

    with pg_session_factory() as a, pg_session_factory() as b:
        user_a = a.get(User, uid)
        # A now holds a copy (kept referenced: the identity map is weak).
        stale = a.get(UserTotp, uid)
        assert stale.last_used_step is None
        assert mfa.check_code(_request(), b, b.get(User, uid), code, now=now) is True
        # B committed last_used_step = 66_666_667; A must not accept the same code.
        assert mfa.check_code(_request(), a, user_a, code, now=now) is False
        a.rollback()

    with pg_session_factory() as check:
        assert check.get(UserTotp, uid).last_used_step == 66_666_667
