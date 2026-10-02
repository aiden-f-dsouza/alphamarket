"""Stablecoins valued at par, whichever venue holds them (registry.CASH_AT_PAR).

Kraken keeps held and earning stablecoin balances under their own codes
(USDC.M, USD.HOLD, ...) and Hyperliquid's HIP-3 dexes margin in USDT0 or
USDH. Before the shared set, only USD/USDC/USDT were at par in valuation, so
those balances fell out of creator equity as "unpriced"."""

from datetime import date

from alphamarket import paper_signals, prices, valuation
from alphamarket.connectors import registry
from alphamarket.connectors.base import MarketDataSource
from alphamarket.connectors.hyperliquid import HyperliquidMarketData
from alphamarket.connectors.kraken import KrakenMarketData
from alphamarket.valuation import AnchorIn, FlagReason, PositionKind

M = 1_000_000
DAY = date(2026, 9, 10)


def test_the_shared_set_is_every_venue_code_for_a_dollar_stablecoin():
    assert registry.CASH_AT_PAR == (
        MarketDataSource.STABLECOINS_AT_PAR
        | KrakenMarketData.STABLECOINS_AT_PAR
        | HyperliquidMarketData.STABLECOINS_AT_PAR
    )
    assert {"USD", "USDC", "USDT", "USDC.M", "USD.HOLD", "USDT0", "USDH"} <= registry.CASH_AT_PAR
    assert valuation.CASH_AT_PAR is registry.CASH_AT_PAR
    assert "BTC" not in registry.CASH_AT_PAR


def test_hyperliquid_collateral_tokens_need_no_close():
    source = HyperliquidMarketData.__new__(HyperliquidMarketData)  # no network: never listed
    assert source.pricing_symbol("USDT0", instruments=[]) is None
    assert source.pricing_symbol("USDH", instruments=[]) is None


def test_creator_equity_counts_venue_stablecoin_codes_at_par():
    # 1,000 USDT0 + 250.5 USDH (Hyperliquid, 6 and 8 dp) + 400 USDC.M (Kraken
    # earn, 8 dp) and no prices at all: 1,000 + 250.5 + 400 = 1,650.50.
    anchor = [
        AnchorIn("USDT0", 1_000 * M, 6),
        AnchorIn("USDH", 25_050_000_000, 8),
        AnchorIn("USDC.M", 40_000_000_000, 8),
    ]
    day = valuation.valuation_on(anchor, [], [], [], DAY)
    assert day.equity_micros == 1_650_500_000
    assert {p.asset: p.kind for p in day.positions} == {
        "USDT0": PositionKind.CASH,
        "USDH": PositionKind.CASH,
        "USDC.M": PositionKind.CASH,
    }
    assert not [f for f in day.flags if f.reason is FlagReason.UNPRICED]


def test_a_close_or_signal_quoted_in_usdt0_is_a_usd_value():
    # 2.5 x 1,000,000 micros: a HIP-3 contract quoted in USDT0.
    assert prices.usd_micros_per_unit("2.5", "USDT0") == 2_500_000
    # 0.1 x 60,000 = 6,000 USD of notional on the signal.
    assert paper_signals.notional_usd_micros(10_000_000, 8, "60000", "USDH") == 6_000 * M
    assert prices.usd_micros_per_unit("2.5", "BTC") is None
