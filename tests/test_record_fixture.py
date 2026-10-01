"""scripts/record_fixture.py: records one public request, refuses anything
that looks like it would carry a credential. Uses respx (invariant #6: no
live calls even for a script that tests exercise)."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

# scripts/ is not an installed package (only src/alphamarket is), so load the
# module directly from its file path rather than adding scripts/ to the
# import path via conftest.py or pyproject.toml (both off-limits here).
_SPEC = importlib.util.spec_from_file_location(
    "record_fixture", Path(__file__).resolve().parent.parent / "scripts" / "record_fixture.py"
)
record_fixture = importlib.util.module_from_spec(_SPEC)
sys.modules["record_fixture"] = record_fixture
_SPEC.loader.exec_module(record_fixture)

CredentialLikeInput = record_fixture.CredentialLikeInput
main = record_fixture.main
record = record_fixture.record


@pytest.fixture()
def frozen_now() -> datetime:
    return datetime(2026, 9, 26, tzinfo=UTC)


@respx.mock
def test_records_get_response_and_provenance(tmp_path: Path, frozen_now: datetime):
    route = respx.get("https://example.com/markets/BTC").mock(
        return_value=httpx.Response(200, json={"ticker": "BTC", "yes_bid": 58})
    )

    path = record(
        "examplevenue",
        "market_btc",
        "https://example.com/markets/BTC",
        fixtures_root=tmp_path,
        now=frozen_now,
    )

    assert route.called
    assert path == tmp_path / "examplevenue" / "market_btc_20260926.json"
    written = json.loads(path.read_text())
    assert written == {"ticker": "BTC", "yes_bid": 58}
    # pretty-printed (not a single line)
    assert path.read_text().count("\n") > 1

    readme = (tmp_path / "examplevenue" / "README.md").read_text()
    assert "market_btc_20260926.json" in readme
    assert "https://example.com/markets/BTC" in readme
    assert "2026-09-26" in readme


@respx.mock
def test_appends_to_existing_readme(tmp_path: Path, frozen_now: datetime):
    respx.get("https://example.com/a").mock(return_value=httpx.Response(200, json={"a": 1}))
    respx.get("https://example.com/b").mock(return_value=httpx.Response(200, json={"b": 2}))

    record("v", "a", "https://example.com/a", fixtures_root=tmp_path, now=frozen_now)
    record("v", "b", "https://example.com/b", fixtures_root=tmp_path, now=frozen_now)

    readme = (tmp_path / "v" / "README.md").read_text()
    assert readme.count("# v fixtures") == 1
    assert "a_20260926.json" in readme
    assert "b_20260926.json" in readme


@respx.mock
def test_records_post_json_with_params(tmp_path: Path, frozen_now: datetime):
    route = respx.post("https://example.com/quote", params={"user": "0xabc"}).mock(
        return_value=httpx.Response(200, json={"price": "1.23"})
    )

    record(
        "v",
        "quote",
        "https://example.com/quote",
        method="POST",
        params={"user": "0xabc"},
        json_body={"symbol": "BTC-USD"},
        fixtures_root=tmp_path,
        now=frozen_now,
    )

    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"symbol": "BTC-USD"}


@pytest.mark.parametrize(
    "url,params,headers",
    [
        ("https://example.com/data?api_key=abc123", {}, {}),
        ("https://example.com/data", {"secret": "shh"}, {}),
        ("https://example.com/data", {"user": "has-a-token-in-value"}, {}),
        ("https://example.com/data", {}, {"Authorization": "Bearer x"}),
        ("https://example.com/data", {}, {"X-Signature": "abc"}),
    ],
)
def test_refuses_credential_like_input(tmp_path, frozen_now, url, params, headers):
    with pytest.raises(CredentialLikeInput):
        record(
            "v",
            "name",
            url,
            params=params,
            headers=headers,
            fixtures_root=tmp_path,
            now=frozen_now,
        )
    # nothing written, and no request attempted (respx not even mocked here:
    # a real request would fail the test suite under invariant #6)
    assert not (tmp_path / "v").exists()


@pytest.mark.parametrize(
    "json_body",
    [
        {"api_key": "abc123"},
        {"symbol": "BTC-USD", "auth": {"token": "shh"}},
        {"filters": [{"secret": "nope"}]},
        {"note": "contains a token embedded in this value"},
    ],
)
def test_refuses_credential_like_json_body(tmp_path, frozen_now, json_body):
    with pytest.raises(CredentialLikeInput):
        record(
            "v",
            "name",
            "https://example.com/quote",
            method="POST",
            json_body=json_body,
            fixtures_root=tmp_path,
            now=frozen_now,
        )
    # nothing written, and no request attempted (respx not even mocked here:
    # a real request would fail the test suite under invariant #6)
    assert not (tmp_path / "v").exists()


@respx.mock
def test_cli_writes_fixture(tmp_path, frozen_now, monkeypatch):
    respx.get("https://example.com/markets/BTC").mock(
        return_value=httpx.Response(200, json={"ticker": "BTC"})
    )
    monkeypatch.setattr("record_fixture.FIXTURES_ROOT", tmp_path)

    rc = main(["examplevenue", "market_btc", "https://example.com/markets/BTC", "--get"])

    assert rc == 0
    files = list((tmp_path / "examplevenue").glob("market_btc_*.json"))
    assert len(files) == 1


def test_cli_refuses_credential_like_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("record_fixture.FIXTURES_ROOT", tmp_path)

    rc = main(["v", "n", "https://example.com/data?token=abc"])

    assert rc == 1
    assert "refused" in capsys.readouterr().err
    assert not (tmp_path / "v").exists()
