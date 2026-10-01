"""Contract v2 defaults (connectors/base.py): pagination, rate budgets, and the
M7-compatible balance an asset venue reports. Hand-computed values."""

from datetime import UTC, datetime

import pytest

from alphamarket.connectors.base import (
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    BalanceUnavailable,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    Page,
    RateBudget,
    RateScope,
    ReadOnlyEvidence,
    ReadOnlyProof,
)

NOW = datetime(2026, 9, 26, tzinfo=UTC)


class _Fake(AssetVenueConnector):
    venue = "fake"

    def __init__(self, assets, pages=None):
        self.assets = assets
        self.pages = pages or {}
        self.calls: list[str] = []

    def fetch_balances(self):
        self.throttle("Balance")
        return NormalizedBalances(as_of=NOW, assets=self.assets)

    def check_write_scope(self):
        return False

    def fetch_events_page(self, cursor=None, since=None):
        return self.pages[cursor]

    def prove_read_only(self):
        return ReadOnlyEvidence(method=ReadOnlyProof.ORDER_PROBE, read_only=True, checked_at=NOW)


def _event(ref):
    return NormalizedEvent(venue_ref=ref, kind="deposit", legs=[], occurred_at=NOW)


def test_fetch_events_follows_cursors_to_the_end():
    conn = _Fake([], pages={None: Page([_event("a")], "c1"), "c1": Page([_event("b")], None)})
    assert [e.venue_ref for e in conn.fetch_events()] == ["a", "b"]


def test_asset_venue_m7_balance_is_cash_only_rescaled_to_micros():
    # 12.34 USDC at 6 dp = 12_340_000 micros; BTC is ignored (not cash).
    conn = _Fake(
        [
            AssetAmount(asset="BTC", atoms=50_000_000, decimals=8),
            AssetAmount(asset="USDC", atoms=12_340_000, decimals=6),
        ]
    )
    assert conn.fetch_balance().balance_micros == 12_340_000
    # USD at 4 dp: 1.2345 USD = 12_345 atoms -> 1_234_500 micros.
    conn = _Fake([AssetAmount(asset="USD", atoms=12_345, decimals=4)])
    assert conn.check_connection().balance_micros == 1_234_500


def test_asset_venue_without_cash_raises_balance_unavailable():
    with pytest.raises(BalanceUnavailable):
        _Fake([AssetAmount(asset="BTC", atoms=1, decimals=8)]).fetch_balance()


class _Recorder:
    def __init__(self):
        self.seen: list[str] = []

    def before(self, endpoint):
        self.seen.append(endpoint)

    def after(self, endpoint, extra_cost=0, used=None):
        self.seen.append(f"after:{endpoint}:{extra_cost}")


def test_throttle_is_called_before_requests_and_can_be_unset():
    rec = _Recorder()
    conn = _Fake([AssetAmount(asset="USD", atoms=0, decimals=2)])
    conn.set_throttle(rec)
    conn.fetch_balances()
    conn.throttle_after("Balance", extra_cost=3)
    conn.set_throttle(None)
    conn.fetch_balances()
    assert rec.seen == ["Balance", "after:Balance:3"]


def test_legs_with_same_kind_and_asset_are_rejected():
    leg = {"asset": "USDC", "delta_atoms": -1, "decimals": 6, "kind": "funding"}
    with pytest.raises(ValueError, match="duplicate leg"):
        NormalizedEvent(venue_ref="t", kind="funding", legs=[leg, leg], occurred_at=NOW)
    # Same asset, different kinds (Kraken quote + fee in USD) is fine.
    NormalizedEvent(
        venue_ref="t", kind="funding", legs=[leg, {**leg, "kind": "fee"}], occurred_at=NOW
    )


def test_pricing_symbol_prefers_usd_then_usdc_and_skips_stablecoins():
    class _Md(AssetMarketData):
        venue = "fake"

    def inst(symbol, base, quote):
        return NormalizedInstrument(
            symbol=symbol, type="spot", base_asset=base, quote_asset=quote, qty_decimals=8
        )

    listed = [
        inst("ETHBTC", "ETH", "BTC"),
        inst("ETHUSDC", "ETH", "USDC"),
        inst("ETHUSD", "ETH", "USD"),
    ]
    md = _Md()
    assert md.pricing_symbol("ETH", listed) == "ETHUSD"
    assert md.pricing_symbol("ETH", listed[:2]) == "ETHUSDC"
    assert md.pricing_symbol("ETH", listed[:1]) is None
    assert md.pricing_symbol("USDC", listed) is None


def test_build_connector_sets_account_ref():
    from alphamarket.connectors.registry import build_connector

    conn = build_connector("polymarket", "prod", "0x" + "ab" * 20, account_ref="main")
    assert conn.account_ref == "main"


def test_rate_budget_cost_lookup():
    kraken = RateBudget(
        scope=RateScope.CREDENTIAL,
        capacity=15,
        refill_per_minute=20,
        costs=(("TradesHistory", 2), ("Ledgers", 2)),
    )
    assert kraken.cost("Ledgers") == 2
    assert kraken.cost("Balance") == 1
