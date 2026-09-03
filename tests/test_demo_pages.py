"""Demo build (DEMO_MODE): sample pages render under a banner, and vanish when
demo mode is off so no sample number can ever pass for a real one."""

import pytest

from alphamarket.config import get_settings

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


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def test_demo_pages_render_with_sample_banner(client, demo_on):
    for path in [*DEMO_ONLY, "/", "/marketplace", "/transparency"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "Sample data" in resp.text, path

    html = client.get("/s/weather-cpi-mean-reversion").text
    assert "Weather &amp; CPI mean reversion" in html
    assert "Worst drawdown" in html and "<polyline" in html
    assert "S&amp;P 500" in html  # benchmark lines share the equity axis
    assert "Tail from my account" in html
    assert "not verified" in html.lower()  # creator's description is labelled
    assert client.get("/s/no-such-strategy").status_code == 404
    assert client.get("/u/nobody").status_code == 404

    html = client.get("/marketplace").text
    assert "Daily temperature spreads" in html  # losing strategies stay listed
    assert "quietedge" not in client.get("/marketplace?tag=politics").text
    assert "Election polling arbitrage" in client.get("/marketplace?tag=politics").text
    html = client.get("/marketplace?q=weather").text  # search matches name or creator
    assert "Weather &amp; CPI" in html and "Election polling" not in html
    assert "Nothing matches" in client.get("/marketplace?q=zzz").text


def test_navigation_is_consistent(client, demo_on):
    """No dead links, one sub-nav on every Learn page, and the nav menus render."""
    for path in [*DEMO_ONLY, *STATIC, *LEARN, "/", "/marketplace"]:
        html = client.get(path).text
        assert 'href="#"' not in html, path
        assert 'aria-label="Primary"' in html and "Learn" in html, path
    for path in LEARN:
        html = client.get(path).text
        assert 'aria-label="Learn"' in html and 'aria-current="page"' in html, path
        for other in LEARN:
            assert f'href="{other}"' in html, (path, other)


def test_demo_routes_vanish_when_demo_is_off(client):
    assert get_settings().demo_mode is False
    for path in DEMO_ONLY:
        assert client.get(path).status_code == 404, path
    for path in ["/", "/marketplace", "/transparency"]:
        html = client.get(path).text
        assert "Sample data" not in html and "quietedge" not in html, path
    assert "Feed" not in client.get("/").text.split("<footer")[0].split("</nav>")[0]


def test_static_pages_and_explore_redirect(client):
    for path in STATIC:
        assert client.get(path).status_code == 200, path
    assert "Nothing was sent" in client.post("/invite", data={"handle": "@x"}).text
    resp = client.get("/explore?incubating=1", follow_redirects=False)
    assert resp.status_code == 307 and resp.headers["location"] == "/marketplace?incubating=1"
    html = client.get("/").text
    assert "Koine" in html and "AlphaMarket" not in html
