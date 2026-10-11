"""The static demo shows copy-strategies round 2 (invariant 7): the sample
portfolio carries a crypto tail stopped by its drawdown limit and closed and a
stock tail stopped by the user and still "Closing"; /watch/optout walks
through its three stages on invented data, writing nothing and calling no
venue. With DEMO_MODE off nothing here is reachable and the real opt-out is
unchanged."""

import re
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select

from alphamarket import demo
from alphamarket.config import get_settings
from alphamarket.connectors.hyperliquid import INFO_URL
from alphamarket.models import Allocation, AuditLog, User, Watch, WatchOptout
from alphamarket.pages import portfolio_view
from alphamarket.watchlist import OPTOUT_DEMO
from authutil import verify_email

BANNER = "Sample data"
SAMPLE = demo.OPTOUT_SAMPLE
REAL_ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"
PASSWORD = "hunter2hunter2"
CLOSED_NAME = "Large-cap alt rotation"
CLOSING_NAME = "Quality dividend compounders"


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def signup(client, email: str) -> None:
    resp = client.post(
        "/signup-form",
        data={"email": email, "password": PASSWORD, "display_name": "User " + email.split("@")[0]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verify_email(client, email)


def rows_of(html: str) -> dict[str, str]:
    """The portfolio's allocation rows, by strategy name."""
    out = {}
    for chunk in html.split('<div class="arow">')[1:]:
        out[re.search(r">([^<]+)</a>", chunk).group(1)] = chunk
    return out


def counts(session_factory) -> dict[str, int]:
    with session_factory() as db:
        return {
            m.__name__: db.scalar(select(func.count()).select_from(m))
            for m in (WatchOptout, AuditLog, Watch, Allocation, User)
        }


# ---- stopped tails in the sample portfolio --------------------------------------


def test_closed_and_closing_tails_render_with_the_live_wording(client, demo_on):
    html = client.get("/portfolio").text
    assert BANNER in html
    rows = rows_of(html)
    assert len(rows) == 6
    closed, closing = rows[CLOSED_NAME], rows[CLOSING_NAME]
    # The closed tail: stopped by its drawdown limit, flat, nothing left to sell.
    assert "stopped (drawdown limit)" in closed
    assert "closing" not in closed.lower()
    assert "Closing:" not in closed and "· closing" not in closed
    assert "−11.8%" in closed and "−10%" in closed  # drawdown past the limit
    # The closing tail: the live status text and warn note.
    assert "stopped (user) · closing" in closing
    assert "Closing: this stopped tail sells its remaining positions" in closing
    assert "nothing is closed at a stale price" in closing
    assert html.count("Closing: this stopped tail") == 1 and html.count("· closing") == 1
    # The four active tails keep their status and have no note.
    for name in ("Fed path, both directions", "Weather &amp; CPI mean reversion"):
        assert "active" in rows[name] and "closing" not in rows[name].lower()
    # No stop button on samples; header counts include the stopped tails.
    assert "/stop" not in html and "6 paper tails" in html
    assert 'href="#"' not in html


def test_the_closing_tails_numbers_add_up():
    book = demo.closing_stock_book()
    # Equity is cash plus positions: 1,096.40 + 845.40 + 627.00.
    assert [r["value"] for r in book["positions"]] == [845.40, 627.00]
    assert book["equity"] == pytest.approx(1096.40 + 845.40 + 627.00) == pytest.approx(2568.80)
    assert all(r["value"] == pytest.approx(float(r["qty"]) * r["mark"]) for r in book["positions"])
    row = next(a for a in demo.portfolio()["allocations"] if a["name"] == CLOSING_NAME)
    assert row["value"] == book["equity"] and row["closing"] is True
    assert row["status"] == "stopped" and row["stop_reason"] == "user"
    assert row["ret"] == pytest.approx(2568.80 / 2500 - 1)  # +2.75%
    assert row["dd"] == pytest.approx(1 - 2568.80 / 2590.00)  # 0.82%, under its 15% limit
    assert row["dd"] < row["limit"]


def test_the_closed_tails_equity_is_just_its_cash():
    book = demo.closed_crypto_book()
    # Before the close: cash 120.00 + 0.20 x 3,310.00 + 4 x 167.50 = 1,452.00,
    # past the 10% limit from the 1,620.00 high (10.37%).
    assert book["pre_close"] == 1452.00
    assert 1 - book["pre_close"] / 1620.00 > 0.10
    # The close at the bid: 0.20 x 3,290.00 + 4 x 165.50 = 1,320.00, fee 0.90% = 11.88.
    assert book["proceeds"] == 1320.00 and book["fee"] == 11.88
    assert book["cash"] == pytest.approx(120.00 + 1320.00 - 11.88) == pytest.approx(1428.12)
    row = next(a for a in demo.portfolio()["allocations"] if a["name"] == CLOSED_NAME)
    assert row["value"] == book["cash"]  # flat: no position value to add
    assert row["positions"] == [] and row["closing"] is False
    assert row["status"] == "stopped" and row["stop_reason"] == "drawdown_limit"
    assert row["ret"] == pytest.approx(1428.12 / 1500 - 1)  # -4.79%
    assert row["dd"] == pytest.approx(1 - 1428.12 / 1620.00)  # 11.84%
    assert row["dd"] > row["limit"] == 0.10


def test_portfolio_totals_add_up():
    p = demo.portfolio()
    assert p["accounts"] == len(p["allocations"]) == 6
    assert p["value"] == pytest.approx(sum(a["value"] for a in p["allocations"]))
    assert p["value"] == pytest.approx(10423.90 + 1428.12 + 2568.80) == pytest.approx(14420.82)
    allocated = sum(a["allocated"] for a in p["allocations"])
    assert allocated == 13500
    assert p["pnl"] == pytest.approx(p["value"] - allocated) == pytest.approx(920.82)
    assert p["pnl_pct"] == pytest.approx(920.82 / 13500)
    assert p["trading_costs"] == pytest.approx(61.84 + 11.88)
    assert ("Largest allocation", "22%") in p["risk"]  # 3,000 / 13,500
    assert dict(p["risk"])["Simulated venue fees"] == "$73.72"


def test_sample_allocations_only_use_the_real_view_keys():
    """The demo rows go through portfolio.html's real branches; every key they
    carry must be one pages.portfolio_view produces, so a rename cannot leave
    the demo behind."""
    view = {
        "id": 1,
        "strategy_slug": "s",
        "strategy_name": "S",
        "started_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "allocation": 1000.0,
        "equity": 1000.0,
        "return": 0.0,
        "drawdown": 0.0,
        "drawdown_limit": 0.15,
        "status": "stopped",
        "stop_reason": "user",
        "closing": True,
        "curve": [["2026-08-01", 1000.0], ["2026-08-02", 1000.0]],
        "positions": [],
        "venue": "Alpaca",
    }
    summary = {
        "allocations": [view],
        "allocated": 1000.0,
        "value": 1000.0,
        "pnl": 0.0,
        "n_active": 0,
    }
    real_keys = set(portfolio_view(summary, {"s": "h"})["allocations"][0])
    for a in demo.portfolio()["allocations"]:
        assert set(a) <= real_keys, set(a) - real_keys
        assert {"status", "stop_reason", "closing"} <= set(a)


def test_closing_tails_strategy_page_carries_the_closing_notice(client, demo_on):
    html = client.get(f"/s/{demo.CLOSING_TAIL_SLUG}/tail").text
    assert BANNER in html
    assert (
        "Closing: your stopped paper tail of this strategy is still selling its positions" in html
    )
    other = client.get("/s/weather-cpi-mean-reversion/tail").text
    assert "Closing: your stopped paper tail" not in other


def test_portfolio_and_tail_page_are_unreachable_without_demo_mode(client):
    assert client.get("/portfolio").status_code == 404
    assert client.get(f"/s/{demo.CLOSING_TAIL_SLUG}/tail").status_code == 404


# ---- methodology ----------------------------------------------------------------


def test_methodology_is_the_real_doc_in_demo_mode(client, demo_on):
    for path in ("/verification", "/methodology"):
        html = client.get(path).text
        assert "When a tail stops" in html, path
        assert "Worked example (Kraken, crypto)" in html and "Closing" in html


# ---- opt-out walkthrough --------------------------------------------------------


def test_each_optout_stage_renders_in_demo_mode_and_writes_nothing(
    client, session_factory, demo_on
):
    before = counts(session_factory)
    with respx.mock(assert_all_called=False) as router:  # any venue call would raise
        route = router.post(INFO_URL).mock(return_value=httpx.Response(200, json=[]))
        start = client.get("/watch/optout")
        proof = client.post("/watch/optout", data={"venue": "hyperliquid", "address": "junk"})
        done = client.post(
            "/watch/optout/verify",
            data={"venue": "hyperliquid", "address": "junk", "signature": "0x" + "ab" * 65},
        )
        assert route.call_count == 0
        assert len(router.calls) == 0
    assert counts(session_factory) == before

    for resp in (start, proof, done):
        assert resp.status_code == 200
        assert BANNER in resp.text
        assert "Demo walkthrough" in resp.text
        assert resp.headers["cache-control"] == "private, no-store"
        assert 'name="robots" content="noindex' in resp.text
        assert 'href="#"' not in resp.text

    # 1: the form, prefilled with the sample Hyperliquid address.
    assert f'value="{SAMPLE["address"]}"' in start.text
    assert 'action="/watch/optout"' in start.text and "Ask not to be watched." in start.text
    assert "Verify your email address first" not in start.text
    # 2: the proof step: the sample code and the real claim text for the sample address.
    assert "Prove this address is yours." in proof.text
    assert SAMPLE["code"] in proof.text
    assert "Koine" in proof.text and SAMPLE["address"] in proof.text
    assert 'action="/watch/optout/verify"' in proof.text
    assert "data-claim-form" not in proof.text and "Sign with my wallet" not in proof.text
    assert "junk" not in proof.text and "junk" not in done.text  # nothing posted is echoed
    # 3: done, with the sample counts and the not-recorded notice.
    assert "Opted out." in done.text
    assert "removed it from 3 watchlists and stopped 1 paper tail of it" in done.text
    assert "nothing was removed, stopped or recorded" in done.text
    assert SAMPLE["address"] in done.text


def test_optout_walkthrough_for_a_logged_in_user_also_writes_nothing(
    client, session_factory, demo_on
):
    signup(client, "a@example.com")
    before = counts(session_factory)
    assert client.get("/watch/optout").status_code == 200
    proof = client.post("/watch/optout", data={"venue": "hyperliquid", "address": REAL_ADDRESS})
    assert SAMPLE["code"] in proof.text and REAL_ADDRESS not in proof.text
    done = client.post(
        "/watch/optout/verify", data={"venue": "hyperliquid", "address": REAL_ADDRESS}
    )
    assert "Opted out." in done.text and BANNER in done.text
    assert counts(session_factory) == before


def test_optout_is_the_login_redirect_without_demo_mode(client):
    resp = client.get("/watch/optout", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    for path in ("/watch/optout", "/watch/optout/verify"):
        post = client.post(
            path,
            data={"venue": "hyperliquid", "address": REAL_ADDRESS},
            follow_redirects=False,
        )
        assert post.status_code == 303 and post.headers["location"].startswith("/login"), path


def test_real_optout_is_unchanged_without_demo_mode(client, session_factory):
    signup(client, "a@example.com")
    page = client.get("/watch/optout")
    assert page.status_code == 200 and BANNER not in page.text
    assert "Demo walkthrough" not in page.text and 'value=""' in page.text
    proof = client.post("/watch/optout", data={"venue": "hyperliquid", "address": REAL_ADDRESS})
    assert proof.status_code == 200
    assert BANNER not in proof.text and "Demo walkthrough" not in proof.text
    codes = set(re.findall(r"koine-optout-[0-9a-f]{12}", proof.text))
    assert len(codes) == 1 and SAMPLE["code"] not in proof.text  # a real, derived code
    assert REAL_ADDRESS in proof.text
    assert "data-claim-form" in proof.text
    # A bad signature is refused by the real proof check, not waved through.
    with respx.mock(assert_all_called=False) as router:
        router.post(INFO_URL).mock(return_value=httpx.Response(200, json=[]))
        bad = client.post(
            "/watch/optout/verify",
            data={"venue": "hyperliquid", "address": REAL_ADDRESS, "signature": "0x" + "ab" * 65},
        )
    assert bad.status_code != 200 and "Opted out." not in bad.text
    assert counts(session_factory)["WatchOptout"] == 0


def test_seeded_demo_user_keeps_the_refusal_without_demo_mode(client, session_factory):
    signup(client, "a@example.com")
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        user.is_demo = True
        db.commit()
    before = counts(session_factory)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(INFO_URL).mock(return_value=httpx.Response(200, json=[]))
        first = client.post("/watch/optout", data={"venue": "hyperliquid", "address": REAL_ADDRESS})
        second = client.post(
            "/watch/optout/verify", data={"venue": "hyperliquid", "address": REAL_ADDRESS}
        )
        assert route.call_count == 0
    for resp in (first, second):
        assert resp.status_code == 403
        assert OPTOUT_DEMO in resp.text
        assert SAMPLE["code"] not in resp.text and "Opted out." not in resp.text
    assert counts(session_factory) == before
