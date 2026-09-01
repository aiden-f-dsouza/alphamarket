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

All connector mappings are now verified against real payloads.
