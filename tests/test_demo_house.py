"""The static demo's sample "Run by Koine" strategy (DEMO_MODE, empty DB)."""

import pytest

from alphamarket.config import get_settings

SLUG = "koine-lab-funding-carry"
NAME = "Koine Lab"
BADGE = "Run by Koine"


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def test_house_sample_on_marketplace(client, demo_on):
    cards = client.get("/marketplace").text
    assert NAME in cards and BADGE in cards and 'class="card scard"' in cards
    table = client.get("/marketplace?view=table").text
    assert NAME in table and BADGE in table
    # creators' samples carry no badge: only the house row has one per view
    assert cards.count('class="operator-badge"') == 1


def test_operator_filter_on_samples(client, demo_on):
    koine = client.get("/marketplace?operator=koine").text
    assert NAME in koine and "Fed path, both directions" not in koine
    assert koine.count('class="operator-badge"') == 1
    creator = client.get("/marketplace?operator=creator").text
    assert NAME not in creator and "Fed path, both directions" in creator


def test_house_strategy_and_tail_pages(client, demo_on):
    html = client.get(f"/s/{SLUG}").text
    assert "Sample data" in html and BADGE in html
    assert 'href="/verification#run-by-koine"' in html
    assert "/u/Koine" not in html and "followers" not in html.split("</h1>")[1].split("</div>")[0]
    assert "not verified" in html.lower()
    tail = client.get(f"/s/{SLUG}/tail").text
    assert "Sample data" in tail and BADGE in tail
    assert client.get("/u/Koine Lab").status_code == 404
    assert client.get("/u/koine").status_code == 404


def test_house_sample_absent_without_demo_mode(client):
    assert client.get(f"/s/{SLUG}").status_code == 404
    assert client.get(f"/s/{SLUG}/tail").status_code == 404
    for path in ("/marketplace", "/marketplace?operator=koine", "/marketplace?view=table"):
        html = client.get(path).text
        assert NAME not in html and 'class="operator-badge"' not in html
