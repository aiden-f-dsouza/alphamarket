"""The security page's activity list (wave2 w2-account-security): a user sees
their own audit rows (as actor, and every row about their venue accounts,
including the worker's decrypts) and nobody else's, with only the action,
purpose, venue, time and IP."""

import pytest
from sqlalchemy import select

from alphamarket import audit, credentials
from alphamarket.email import ConsoleEmailSender, set_sender
from alphamarket.models import User, VenueAccount, VenueAccountStatus
from alphamarket.security_pages import audit_trail
from authutil import secure_account

EMAIL = "trail@example.com"


@pytest.fixture(autouse=True)
def outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


@pytest.fixture()
def member(client):
    resp = client.post(
        "/auth/signup",
        json={"email": EMAIL, "password": "hunter2hunter2", "display_name": "Trail"},
    )
    secure_account(client, EMAIL)
    return resp.json()["id"]


def _account(db, user_id: int, venue: str, key_version_marker: str) -> VenueAccount:
    account = VenueAccount(
        user_id=user_id,
        venue=venue,
        venue_env="prod",
        key_id=f"{venue}-key",
        status=VenueAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    credentials.store(db, account, f"secret-for-{key_version_marker}", kind="key_secret")
    db.commit()
    return account


def test_page_lists_a_job_driven_decrypt_of_mine_and_nothing_of_anothers(
    client, member, session_factory
):
    with session_factory() as db:
        mine = _account(db, member, "kraken", "mine")
        stranger = User(email="stranger@example.com", password_hash="x", display_name="S")
        db.add(stranger)
        db.flush()
        theirs = _account(db, stranger.id, "binanceus", "theirs")
        with audit.job_context("sync"):  # what JobRunner wraps every handler in
            credentials.open_credential(db, mine, purpose="sync")
            credentials.open_credential(db, theirs, purpose="their_private_purpose")
        audit.record(
            db,
            "security.login",
            actor_user_id=stranger.id,
            target_type="user",
            target_id=stranger.id,
            ip="203.0.113.99",
        )
        # A row about the stranger's account whose target id merely looks like
        # one of mine as an integer is still not mine (target_type differs).
        audit.record(db, "admin.action", target_type="user", target_id=mine.id, details={})
        db.commit()
        mine_id = mine.id

    page = client.get("/dashboard/security").text
    assert "Activity" in page
    assert '<tr data-action="credential.decrypt">' in page
    assert "Key used" in page and "<td>sync</td>" in page and "<td>Kraken</td>" in page
    assert "Koine worker" in page  # no IP: the worker did it
    assert "their private purpose" not in page and "<td>Binance.US</td>" not in page
    assert "203.0.113.99" not in page
    assert "secret-for" not in page and "credential_id" not in page

    with session_factory() as db:
        user = db.get(User, member)
        rows = audit_trail(db, user)
        decrypts = [r for r in rows if r["action"] == "credential.decrypt"]
        assert len(decrypts) == 1
        assert decrypts[0]["purpose"] == "sync" and decrypts[0]["venue"] == "Kraken"
        assert set(decrypts[0]) == {"action", "label", "purpose", "venue", "at", "ip"}
        actions = {r["action"] for r in rows}
        # Mine as actor (sign-up flow, TOTP) and about my account (key stored).
        assert {"credential.connect", "security.totp_enabled"} <= actions
        assert "admin.action" not in actions
        assert all(r["at"].tzinfo is not None for r in rows)
        assert rows == sorted(rows, key=lambda r: r["at"], reverse=True)
        # Another user's view never shows my account's rows.
        stranger = db.scalars(select(User).where(User.email == "stranger@example.com")).one()
        theirs = audit_trail(db, stranger)
        assert all(r["venue"] != "Kraken" for r in theirs)
        assert {r["purpose"] for r in theirs if r["action"] == "credential.decrypt"} == {
            "their private purpose"
        }
        assert mine_id


def test_a_user_with_no_accounts_sees_only_their_own_actions(client, member, session_factory):
    with session_factory() as db:
        other = User(email="x@example.com", password_hash="x", display_name="X")
        db.add(other)
        db.flush()
        _account(db, other.id, "kraken", "x")
        rows = audit_trail(db, db.get(User, member))
        assert rows and all(r["venue"] == "" for r in rows)


def test_routine_decrypts_do_not_push_security_events_off_the_list(client, member, session_factory):
    """A sync decrypts every few minutes. 60 of them, all newer than the
    sign-up's events, still leave room for those events: decrypts are capped
    at the newest AUDIT_TRAIL_DECRYPTS, merged in time order."""
    from alphamarket.security_pages import AUDIT_TRAIL_DECRYPTS

    with session_factory() as db:
        mine = _account(db, member, "kraken", "mine")
        with audit.job_context("sync"):
            for _ in range(60):
                credentials.open_credential(db, mine, purpose="sync")
        rows = audit_trail(db, db.get(User, member))
    actions = [r["action"] for r in rows]
    assert actions.count("credential.decrypt") == AUDIT_TRAIL_DECRYPTS == 10
    assert "security.totp_enabled" in actions and "credential.connect" in actions
    assert actions[:10] == ["credential.decrypt"] * 10  # still newest first
