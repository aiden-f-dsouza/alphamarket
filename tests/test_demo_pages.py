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
    assert "Start a paper tail" in html and "Paper only" in html
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
    assert 'class="card scard"' in html and "<polyline" in html  # browsing cards by default
    table = client.get("/marketplace?view=table").text
    assert 'class="card scard"' not in table and "Worst DD" in table  # comparison table
    assert 'href="/marketplace?tag=weather&amp;view=table"' in table  # chips keep the view


ASSET_SAMPLES = {
    # slug: (venue label, asset tab, a holding that must be on the page)
    "btc-eth-weekly-trend": ("Kraken", "crypto", "BTC"),
    "large-cap-alt-rotation": ("Coinbase", "crypto", "SOL"),
    "funding-aware-btc-perps": ("Hyperliquid", "crypto", "BTC-PERP"),
    "quality-dividend-compounders": ("Alpaca", "equities", "MSFT"),
    "semis-swing-long-only": ("Interactive Brokers", "equities", "NVDA"),
}


def test_asset_samples_render_as_asset_pages(client, demo_on):
    """Crypto and equity samples use the v2 (asset) page: holdings and
    exposure, recent trades instead of settlements, under the sample banner."""
    labels = {"crypto": "Crypto", "equities": "Stocks"}
    for slug, (venue, asset, holding) in ASSET_SAMPLES.items():
        html = client.get(f"/s/{slug}").text
        assert "Sample data" in html and f"<span>{labels[asset]}</span>" in html, slug
        assert 'id="holdings"' in html and f'<td class="mono">{holding}</td>' in html, slug
        assert "Recent trades" in html and "Recent settlements" not in html, slug
        assert venue in html and "data-series=" in html, slug
        assert "Start a paper tail" in html, slug
        assert client.get(f"/s/{slug}/tail").status_code == 200, slug
    perps = client.get("/s/funding-aware-btc-perps").text
    assert "Perp leverage (gross)" in perps and "unrealized" in perps
    assert "daily verified" in client.get("/s/semis-swing-long-only").text


def test_marketplace_tabs_show_sample_rows_by_asset_class(client, demo_on):
    crypto = client.get("/marketplace?asset=crypto&incubating=1").text
    assert "BTC/ETH trend" in crypto and "Funding-aware BTC perps" in crypto
    assert "Fed path" not in crypto and "Quality dividend" not in crypto
    stocks = client.get("/marketplace?asset=equities&incubating=1").text
    assert "Quality dividend compounders" in stocks and "Semis swing" in stocks
    assert "BTC/ETH trend" not in stocks
    prediction = client.get("/marketplace?asset=prediction").text
    assert "Fed path" in prediction and "BTC/ETH trend" not in prediction
    kraken = client.get("/marketplace?venue=kraken").text
    assert "BTC/ETH trend" in kraken and "Large-cap alt rotation" not in kraken
    for html in (crypto, stocks, prediction):
        assert "Sample data" in html


STALE_COPY = [
    "high-water",
    "profit above",
    "earn only when",
    "fee on gains",
    "20% of",
    "from my account",
    "from your own account",
    "own brokerage",
    "Connect an account",
    "Capacity",
    "investors",
    "prediction markets first",
]


def test_copy_is_paper_only_with_flat_pricing(client, demo_on):
    """Tailing is paper only (invariant 8) and pricing is flat: no page claims
    live execution, a performance fee, a high-water mark or tailing capital."""
    pages = [
        "/",
        "/creators",
        "/marketplace",
        "/marketplace?view=table",
        "/portfolio",
        "/feed",
        "/transparency",
        "/research",
        "/invite",
        "/invite?kind=investor",
        "/u/quietedge",
        "/u/basisbook",
        "/s/weather-cpi-mean-reversion",
        "/s/weather-cpi-mean-reversion/tail",
        "/s/btc-eth-weekly-trend/tail",
    ]
    for path in pages:
        html = client.get(path).text
        for phrase in STALE_COPY:
            assert phrase.lower() not in html.lower(), (path, phrase)
    tail = client.get("/s/btc-eth-weekly-trend/tail").text
    assert "No order is placed" in tail and "never a share of returns" in tail.lower()
    assert "trade-scoped" not in tail and "Connected account" not in tail


def test_landing_marks_and_chart(client, demo_on):
    """Identity marks and the drawn curve come from the same rows the marketplace
    shows: sample ones under the banner, nothing else. The draw-in is a clip on
    a group, never pathLength dashes: those are measured in viewBox units but
    drawn in screen pixels under non-scaling-stroke, so a stretched chart only
    showed part of its line."""
    html = client.get("/").text
    assert html.count('<svg class="sigil"') >= 4  # featured card + top three
    assert '<g class="draw">' in html and "shaded: worst drawdown" in html
    assert "pathLength=" not in html
    html = client.get("/s/weather-cpi-mean-reversion").text
    assert "data-series=" in html and '<g class="draw">' in html  # crosshair series + draw-in
    assert "pathLength=" not in html


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
    for path in [p for p in DEMO_ONLY if p != "/feed"]:  # /feed is real now: login redirect
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
