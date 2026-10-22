"""Copy tickets on the investor pages: the tail form's opt-in, the portfolio's
per-tail settings and the nav link. Every surface is hidden while
COPY_TICKETS_ENABLED is off (copy_tickets_plan "Settings")."""

from alphamarket.models import User
from test_copy_tickets import copy_on  # noqa: F401  (fixture)
from test_tail_ui import _allocations, _start, eth, viewer  # noqa: F401  (fixtures)


def test_feature_off_hides_every_copy_surface(client, eth, viewer):  # noqa: F811
    assert 'name="copy_on"' not in client.get(f"/s/{eth}").text
    assert 'name="copy_on"' not in client.get(f"/s/{eth}/tail").text
    assert _start(client).status_code == 303
    html = client.get("/portfolio").text
    assert "/copy" not in html and "Copy tickets" not in html


def test_tail_forms_offer_the_opt_in(client, eth, viewer, copy_on):  # noqa: F811
    for path in (f"/s/{eth}", f"/s/{eth}/tail"):
        html = client.get(path).text
        assert 'name="copy_on"' in html, path
        assert "Koine never places it" in html, path
    assert 'href="/copy"' in client.get("/portfolio").text  # the nav link


def test_starting_a_tail_with_the_opt_in_turns_tickets_on(
    client,
    session_factory,
    eth,  # noqa: F811
    viewer,  # noqa: F811
    copy_on,  # noqa: F811
):
    resp = client.post(
        f"/s/{eth}/tail",
        data={"allocation": "2500", "drawdown_limit_bps": "2000", "confirm": "on", "copy_on": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    (row,) = _allocations(session_factory, viewer)
    assert row.copy_tickets is True and row.copy_enabled_at is not None
    assert row.copy_email is False and row.copy_account_id is None


def test_starting_a_tail_without_the_opt_in_leaves_tickets_off(
    client,
    session_factory,
    eth,  # noqa: F811
    viewer,  # noqa: F811
    copy_on,  # noqa: F811
):
    assert _start(client).status_code == 303
    (row,) = _allocations(session_factory, viewer)
    assert row.copy_tickets is False and row.copy_enabled_at is None


def test_portfolio_copy_settings_round_trip(
    client,
    session_factory,
    eth,  # noqa: F811
    viewer,  # noqa: F811
    copy_on,  # noqa: F811
):
    assert _start(client).status_code == 303
    (row,) = _allocations(session_factory, viewer)
    html = client.get("/portfolio").text
    assert f'action="/portfolio/allocations/{row.id}/copy"' in html
    # no own Kraken account yet: the panel points at connecting one
    assert "connect your own Kraken account" in html

    resp = client.post(
        f"/portfolio/allocations/{row.id}/copy",
        data={"enabled": "on", "email": "on", "account_id": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/portfolio?notice=copy_saved"
    (row,) = _allocations(session_factory, viewer)
    assert row.copy_tickets is True and row.copy_email is True

    html = client.get("/portfolio?notice=copy_saved").text
    assert "Copy ticket settings saved" in html
    assert "Copy tickets · on" in html


def test_a_demo_investors_copy_page_carries_the_sample_banner(
    client,
    session_factory,
    viewer,  # noqa: F811
    copy_on,  # noqa: F811
):
    assert "Sample data" not in client.get("/copy").text
    with session_factory() as db:
        db.get(User, viewer).is_demo = True  # a seeded demo investor (invariant 7)
        db.commit()
    assert "Sample data" in client.get("/copy").text
