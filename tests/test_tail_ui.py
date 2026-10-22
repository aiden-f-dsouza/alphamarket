"""w3-tail-ui: paper tailing, following and stopping from the HTML pages.

The JSON routes (tails.py, follows.py) already had this logic; these tests
pin the forms that reuse it: the strategy page's tail card and follow
button, /s/{slug}/tail (GET form, POST start), the /portfolio stop button,
login `next` (and that it is never an open redirect), the demo-owned
visibility rule, the static demo flow, and quantity formatting."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from alphamarket import ratelimit
from alphamarket.config import get_settings
from alphamarket.models import Allocation, AllocationStatus, Follow, User
from alphamarket.numfmt import fmt_num
from alphamarket.pages import safe_next
from authutil import code, enrol_totp
from test_pages_v2 import M, _asset_signal, _stats, _strategy, v2_extras

PASSWORD = "hunter2hunter2"


@pytest.fixture()
def demo_mode():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def _signup(client, email="inv@example.com", name="Investor", **extra):
    resp = client.post(
        "/signup-form",
        data={"email": email, "password": PASSWORD, "display_name": name, **extra},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return resp


@pytest.fixture()
def viewer(client, session_factory) -> int:
    _signup(client)
    with session_factory() as db:
        return db.scalar(select(User).where(User.email == "inv@example.com")).id


@pytest.fixture()
def eth(session_factory) -> str:
    """A public Kraken strategy owned by someone else, with v2 stats."""
    with session_factory() as db:
        s = _strategy(db, "kraken", "eth-swing", "ETH Swing")
        _stats(db, s, v2_extras())
        db.commit()
        return s.slug


def _allocations(session_factory, user_id=None) -> list[Allocation]:
    with session_factory() as db:
        q = select(Allocation)
        if user_id is not None:
            q = q.where(Allocation.user_id == user_id)
        return list(db.scalars(q.order_by(Allocation.id)).all())


def _start(client, slug="eth-swing", allocation="2500", bps="2000", confirm=True):
    data = {"allocation": allocation, "drawdown_limit_bps": bps}
    if confirm:
        data["confirm"] = "on"
    return client.post(f"/s/{slug}/tail", data=data, follow_redirects=False)


# ---- strategy page card ---------------------------------------------------------------


def test_logged_out_card_sends_the_visitor_to_login_and_back(client, eth):
    html = client.get(f"/s/{eth}").text
    assert "Paper-tail this strategy" in html
    assert 'href="/signup?next=/s/eth-swing"' in html  # the tail button: most visitors are new
    assert 'href="/login?next=/s/eth-swing"' in html
    assert 'href="/login?next=/s/eth-swing"' in html  # the follow button
    assert "0 following" in html and "0 followers" in html
    # fill and cost text from the venue profile, the same words as the sample pages
    assert "Venue quote when Koine saw the trade" in html and "Kraken taker fee" in html
    assert "Flat subscription, never a share of returns" in html
    assert 'href="#"' not in html


def test_logged_out_tail_page_redirects_to_login_with_next(client, eth):
    resp = client.get(f"/s/{eth}/tail", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/s/eth-swing/tail"
    page = client.get("/login?next=/s/eth-swing/tail").text
    assert 'name="next" value="/s/eth-swing/tail"' in page
    assert 'href="/signup?next=/s/eth-swing/tail"' in page  # new visitors keep it too


def test_login_form_returns_to_next(client, eth, viewer):
    client.post("/logout-form")
    resp = client.post(
        "/login-form",
        data={"email": "inv@example.com", "password": PASSWORD, "next": "/s/eth-swing/tail"},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/s/eth-swing/tail"
    # a logged-in visit to /login?next= goes straight on
    resp = client.get("/login?next=/s/eth-swing/tail", follow_redirects=False)
    assert resp.headers["location"] == "/s/eth-swing/tail"


def test_signup_form_returns_to_next(client, eth):
    resp = _signup(client, "new@example.com", "Newcomer", next="/s/eth-swing/tail")
    assert resp.headers["location"] == "/s/eth-swing/tail"


def test_two_factor_login_carries_next_through_the_second_step(client, eth, viewer):
    secret, _ = enrol_totp(client)
    client.post("/logout-form")
    resp = client.post(
        "/login-form",
        data={"email": "inv@example.com", "password": PASSWORD, "next": "/s/eth-swing/tail"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/login/2fa?next=/s/eth-swing/tail"
    page = client.get("/login/2fa?next=/s/eth-swing/tail").text
    assert 'name="next" value="/s/eth-swing/tail"' in page
    resp = client.post(
        "/login/2fa",
        data={"code": code(secret, 1), "next": "/s/eth-swing/tail"},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/s/eth-swing/tail"


OFFSITE = [
    "//evil.example",
    "/\\evil.example",
    "/\t/evil.example",
    "https://evil.example",
    "evil.example",
    "javascript:alert(1)",
]


@pytest.mark.parametrize("target", OFFSITE)
def test_login_next_is_never_an_open_redirect(client, viewer, target):
    assert safe_next(target) == "/dashboard"
    client.post("/logout-form")
    resp = client.post(
        "/login-form",
        data={"email": "inv@example.com", "password": PASSWORD, "next": target},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
    assert "evil" not in client.get("/login", params={"next": target}).text


# ---- starting a tail --------------------------------------------------------------------


def test_tail_form_renders_for_a_logged_in_viewer(client, eth, viewer):
    resp = client.get(f"/s/{eth}/tail")
    assert resp.status_code == 200
    html = resp.text
    assert 'action="/s/eth-swing/tail"' in html and 'name="allocation"' in html
    assert 'min="100"' in html and 'max="1000000"' in html
    assert '<option value="1500" selected>' in html  # default drawdown limit −15%
    assert 'name="confirm"' in html and "paper simulation" in html
    assert "public quote at the moment Koine saw the creator" in html
    assert "Sample flow" not in html and "Sample data" not in html
    assert 'href="#"' not in html


def test_start_a_tail_via_the_form(client, session_factory, eth, viewer):
    resp = _start(client, allocation="2,500.00", bps="2000")
    assert resp.status_code == 303 and resp.headers["location"] == "/portfolio?notice=started"
    (row,) = _allocations(session_factory, viewer)
    assert row.allocation_micros == 2_500 * M and row.drawdown_limit_bps == 2000
    assert row.status == AllocationStatus.ACTIVE and row.mode == "paper"

    html = client.get("/portfolio?notice=started").text
    assert "Paper tail started" in html and "ETH Swing" in html
    assert f'action="/portfolio/allocations/{row.id}/stop"' in html
    assert "data-confirm=" in html and "Stop paper tail" in html
    # the strategy page now points at the portfolio instead of a second tail
    page = client.get(f"/s/{eth}").text
    assert "You are paper-tailing this strategy" in page and 'href="/portfolio"' in page
    assert "1 following" in page


def test_a_second_active_tail_is_refused_with_a_message(client, session_factory, eth, viewer):
    assert _start(client).status_code == 303
    resp = _start(client, allocation="500")
    assert resp.status_code == 409
    assert "You already have an active tail here" in resp.text
    assert len(_allocations(session_factory, viewer)) == 1
    html = client.get(f"/s/{eth}/tail").text
    assert "already have an active paper tail" in html and "disabled" in html


@pytest.mark.parametrize(
    ("allocation", "bps", "confirm", "message"),
    [
        ("99.99", "1500", True, "between $100 and $1,000,000"),
        ("1000000.01", "1500", True, "between $100 and $1,000,000"),
        ("lots", "1500", True, "Allocation must be a decimal amount"),
        ("2000", "50", True, "Choose a drawdown limit"),  # AllocationIn: 100..9000 bps
        ("2000", "x", True, "Choose a drawdown limit"),
        ("2000", "1500", False, "Tick the box to confirm"),
    ],
)
def test_tail_form_validation_rerenders_with_the_message(
    client, session_factory, eth, viewer, allocation, bps, confirm, message
):
    resp = _start(client, allocation=allocation, bps=bps, confirm=confirm)
    assert resp.status_code == 422
    assert message in resp.text and 'role="alert"' in resp.text
    assert f'value="{allocation.replace(",", "")}"' in resp.text  # what was typed stays
    assert _allocations(session_factory) == []


def test_bounds_are_inclusive(client, session_factory, eth, viewer):
    assert _start(client, allocation="100").status_code == 303
    assert _allocations(session_factory, viewer)[0].allocation_micros == 100 * M


def test_tail_form_uses_the_json_routes_rate_limit(
    client, session_factory, eth, viewer, monkeypatch
):
    monkeypatch.setitem(ratelimit.RULES, "follow_user", ratelimit.Rule("follow_user", 1, 3600))
    assert _start(client, allocation="lots").status_code == 422  # counted, then refused
    resp = _start(client)
    assert resp.status_code == 429 and "Too many requests" in resp.text
    assert _allocations(session_factory) == []


def test_a_creator_cannot_tail_their_own_strategy(client, session_factory, viewer):
    with session_factory() as db:
        s = _strategy(db, "kraken", "mine", "Mine", user=db.get(User, viewer))
        _stats(db, s, v2_extras())
        db.commit()
    page = client.get("/s/mine").text
    assert "Creators cannot paper-tail their own record" in page
    assert '<button class="btn lg" type="button" disabled>' in page
    assert "/s/mine/follow" not in page  # nor follow themselves
    resp = _start(client, slug="mine")
    assert resp.status_code == 422 and "You cannot tail your own strategy" in resp.text


def test_tail_post_without_origin_is_refused_by_csrf(client, eth, viewer):
    resp = client.post(
        f"/s/{eth}/tail",
        data={"allocation": "2000", "drawdown_limit_bps": "1500", "confirm": "on"},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---- visibility ----------------------------------------------------------------------------


def test_demo_owned_strategy_is_not_tailable_without_demo_mode(client, session_factory, viewer):
    with session_factory() as db:
        owner = User(email="seed@demo.koine", password_hash="x", display_name="seed", is_demo=True)
        db.add(owner)
        db.flush()
        s = _strategy(db, "kraken", "seeded", "Seeded", user=owner)
        _stats(db, s, v2_extras())
        db.commit()
    assert client.get("/s/seeded/tail").status_code == 404
    assert _start(client, slug="seeded").status_code == 404
    assert client.post("/s/seeded/follow", follow_redirects=False).status_code == 404
    assert _allocations(session_factory) == []


def test_unknown_strategy_tail_page_is_404(client, viewer):
    assert client.get("/s/nope/tail").status_code == 404
    assert _start(client, slug="nope").status_code == 404


def test_static_demo_tail_flow_still_renders(client, demo_mode):
    resp = client.get("/s/weather-cpi-mean-reversion/tail")
    assert resp.status_code == 200
    html = resp.text
    assert "Sample data" in html and "Sample flow" in html
    assert "published formula" in html and "A flat monthly subscription" in html
    assert 'href="#"' not in html


def test_static_demo_strategy_card_keeps_its_sample_wording(client, demo_mode):
    html = client.get("/s/weather-cpi-mean-reversion").text
    assert "fill price, at the tick" in html and "Kalshi fee formula" in html
    assert 'href="/s/weather-cpi-mean-reversion/tail"' in html


# ---- inline form on the strategy page -------------------------------------------------------


@pytest.fixture()
def deep(session_factory) -> str:
    """A strategy whose worst drawdown is 18.3%."""
    with session_factory() as db:
        s = _strategy(db, "kraken", "deep-dd", "Deep DD")
        _stats(db, s, v2_extras(), max_drawdown=0.183)
        db.commit()
        return s.slug


def test_strategy_page_has_the_inline_tail_form(client, eth, viewer):
    html = client.get(f"/s/{eth}").text
    assert '<form method="post" action="/s/eth-swing/tail"' in html
    assert 'name="allocation"' in html and 'value="2000"' in html
    assert 'min="100"' in html and 'max="1000000"' in html
    assert '<option value="1500" selected>' in html  # worst drawdown 10% -> -15%
    assert 'name="confirm"' in html and "paper simulation" in html
    assert "How paper fills are priced" in html and "Full details" in html
    assert 'href="#"' not in html


def test_inline_form_default_follows_the_strategys_drawdown(client, deep, viewer):
    html = client.get(f"/s/{deep}").text
    assert '<option value="2000" selected>' in html
    assert "already fallen" not in html


def test_tighter_limit_note_renders_on_a_rerender(client, deep, viewer):
    resp = _start(client, slug=deep, bps="1500", confirm=False)  # error path keeps the choice
    assert resp.status_code == 422
    assert '<option value="1500" selected>' in resp.text
    assert "already fallen 18.3% from a high" in resp.text
    assert "A tighter limit is likely to stop the tail" in resp.text


def test_posting_from_the_strategy_page_form_starts_the_tail(client, session_factory, deep, viewer):
    html = client.get(f"/s/{deep}").text
    assert 'action="/s/deep-dd/tail"' in html
    resp = _start(client, slug=deep, allocation="2000", bps="2000")
    assert resp.status_code == 303 and resp.headers["location"] == "/portfolio?notice=started"
    (row,) = _allocations(session_factory, viewer)
    assert row.drawdown_limit_bps == 2000


def test_anon_card_links_to_signup_and_login(client, eth):
    html = client.get(f"/s/{eth}").text
    assert 'href="/signup?next=/s/eth-swing"' in html
    assert '<form method="post" action="/s/eth-swing/tail"' not in html
    assert 'name="next" value="/s/eth-swing"' in client.get("/signup?next=/s/eth-swing").text


def test_own_strategy_has_no_inline_form(client, session_factory, viewer):
    with session_factory() as db:
        s = _strategy(db, "kraken", "mine", "Mine", user=db.get(User, viewer))
        _stats(db, s, v2_extras())
        db.commit()
    html = client.get("/s/mine").text
    assert 'action="/s/mine/tail"' not in html and "Creators cannot paper-tail" in html


def test_sample_strategy_page_has_no_submitting_tail_form(client, demo_mode):
    html = client.get("/s/weather-cpi-mean-reversion").text
    assert 'action="/s/weather-cpi-mean-reversion/tail"' not in html
    assert 'name="confirm"' not in html


# ---- stopping a tail -------------------------------------------------------------------------


def test_stop_a_tail_from_the_portfolio(client, session_factory, eth, viewer):
    _start(client)
    (row,) = _allocations(session_factory, viewer)
    resp = client.post(f"/portfolio/allocations/{row.id}/stop", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/portfolio?notice=stopped"
    (row,) = _allocations(session_factory, viewer)
    assert row.status == AllocationStatus.STOPPED and row.stop_reason == "user"
    html = client.get("/portfolio?notice=stopped").text
    assert "Paper tail stopped" in html and "Stop paper tail" not in html
    # a stopped tail frees the strategy for a new one
    assert "Start paper tail" in client.get(f"/s/{eth}").text
    assert _start(client).status_code == 303


def test_stop_is_only_for_your_own_tail(client, session_factory, eth, viewer):
    _start(client)
    (row,) = _allocations(session_factory, viewer)
    client.post("/logout-form")
    resp = client.post(f"/portfolio/allocations/{row.id}/stop", follow_redirects=False)
    assert resp.headers["location"] == "/login?next=/portfolio"
    _signup(client, "other@example.com", "Other")
    assert client.post(f"/portfolio/allocations/{row.id}/stop").status_code == 404
    assert _allocations(session_factory, viewer)[0].status == AllocationStatus.ACTIVE


def test_portfolio_notice_reflects_nothing(client, viewer):
    html = client.get("/portfolio?notice=<script>alert(1)</script>").text
    assert "alert(1)" not in html


# ---- following -----------------------------------------------------------------------------


def test_follow_and_unfollow_via_the_form(client, session_factory, eth, viewer):
    html = client.get(f"/s/{eth}").text
    assert 'action="/s/eth-swing/follow"' in html and "0 followers" in html
    resp = client.post(f"/s/{eth}/follow", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/s/eth-swing"
    client.post(f"/s/{eth}/follow")  # idempotent
    with session_factory() as db:
        assert len(db.scalars(select(Follow)).all()) == 1
    html = client.get(f"/s/{eth}").text
    assert "1 follower<" in html and 'action="/s/eth-swing/unfollow"' in html
    assert client.post(f"/s/{eth}/unfollow", follow_redirects=False).status_code == 303
    with session_factory() as db:
        assert db.scalars(select(Follow)).all() == []
    assert "0 followers" in client.get(f"/s/{eth}").text


def test_follow_logged_out_goes_to_login(client, eth):
    resp = client.post(f"/s/{eth}/follow", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=/s/eth-swing"


def test_feed_shows_a_followed_seeded_strategys_trades(client, session_factory, demo_mode):
    with session_factory() as db:
        owner = User(email="seed@demo.koine", password_hash="x", display_name="seed", is_demo=True)
        db.add(owner)
        db.flush()
        s = _strategy(db, "kraken", "seeded", "Seeded ETH", user=owner)
        _stats(db, s, v2_extras())
        _asset_signal(db, s)
        db.commit()
    _signup(client)
    assert "Seeded ETH: buy" not in client.get("/feed").text
    assert client.post("/s/seeded/follow", follow_redirects=False).status_code == 303
    html = client.get("/feed").text
    assert "Seeded ETH: buy 0.5 ETH/USD at 3,100.5" in html  # formatted, not atoms


# ---- quantity formatting -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "places", "expected"),
    [
        ("69390.277744", None, "69,390.2777"),  # 4 decimals from 1 up
        ("-1175.58", None, "−1,175.58"),  # typographic minus
        ("173630", None, "173,630"),
        (173630.0, None, "173,630"),  # display float from the signal price
        ("0.50000000", None, "0.5"),  # trailing zeros of the atom string dropped
        ("0.000123456789", None, "0.000123457"),  # six significant digits below 1
        ("5000.000000", 2, "5,000.00"),  # cash: to the cent
        ("+0.1234", None, "+0.1234"),  # an explicit sign (sample perp sizes) is kept
        ("1,234", None, "1,234"),  # already grouped
        (Decimal("2.5"), None, "2.5"),
        ("0", None, "0"),
        ("-0.00000000001", None, "−0.00000000001"),
        (None, None, "—"),
        ("n/a", None, "n/a"),  # not a number: unchanged
    ],
)
def test_fmt_num(value, places, expected):
    assert fmt_num(value, places) == expected


def test_strategy_page_formats_holdings_quantities_and_prices(client, session_factory):
    extras = v2_extras()
    extras["holdings"][0]["qty"] = "69390.277744"  # USD cash
    extras["holdings"][2].update(qty="-1175.58", entry="173630")  # a short perp
    with session_factory() as db:
        s = _strategy(db, "kraken", "fmt", "Fmt")
        _stats(db, s, extras)
        db.commit()
    html = client.get("/s/fmt").text
    assert ">69,390.28<" in html and "69390.277744" not in html
    assert ">−1,175.58<" in html and "-1175.58" not in html
    assert "entry 173,630" in html and "173630" not in html
