Real payloads captured from a Kalshi **demo** account on 2026-08-27 by
`scripts/kalshi_smoke.py`. Invariant #6 says no live API calls in tests, so
these are the ground truth the connector is tested against.

`kalshi_fills.json` / `kalshi_balance.json` -- captured verbatim.

`kalshi_api_keys.json` -- shape captured live from `GET /api_keys` on
2026-08-31 (key id and name anonymized). The real demo key reported
`"scopes": ["read", "write"]`; the fixture's read-only variant is synthetic
but uses the same shape.

`kalshi_positions.json` -- captured verbatim on 2026-09-01 after a demo trade
(0.84 NO contracts on an ETH 15-minute market). Note `position_fp` is signed
and fractional, and rows carry `exchange_index`.

`kalshi_settlements.json` -- captured verbatim on 2026-09-01 when that ETH
trade settled (market_result yes, so the NO position lost). Notable: the fee
field is `fee_cost` like fills, `revenue` is an INTEGER in CENTS (no
`revenue_dollars` exists), and there is no venue settlement id.

`kalshi_market.json` / `kalshi_market_settled.json` -- captured verbatim from
the **production** public endpoint `GET /markets/{ticker}` on 2026-09-06 (no
auth needed, so no account is involved). The first is an open BTC market with
a live 0.58/0.59 quote; the second is a finalized one (`status: finalized`,
`result: no`). `/markets/{unknown}` returns 404 `{"error": {"code":
"not_found"}}`.

`polymarket_*.json` -- captured verbatim on 2026-09-06 from Polymarket's
**public** endpoints (no account or key exists for this venue):
`polymarket_activity_trades.json` and `polymarket_activity_redeems.json`
from `data-api.polymarket.com/activity?user=…&type=TRADE|REDEEM`
(oldest-first, `start` filtered) for a public wallet picked from the global
trade feed; `polymarket_positions.json` from `/positions?user=…` (first two
rows); `polymarket_profile.json` from `gamma-api.polymarket.com/public-profile
?address=…` for a different wallet that has a bio; `polymarket_market.json`
(active) and `polymarket_market_closed.json` (resolved, `winner` flags) from
`clob.polymarket.com/markets/{conditionId}`, descriptions truncated. Notable:
`price` has up to 10 dp, `usdcSize` is net cash including the fee, there is
no per-fill id (several fills share a `transactionHash`), unknown markets and
profiles return 404, and the unfiltered feed reports `outcomeIndex: 999`.

All connector mappings are verified against real payloads.

`streams/` -- websocket frames for the P7 stream process (Hyperliquid,
Kraken, Coinbase); provenance per file in `streams/README.md`.
