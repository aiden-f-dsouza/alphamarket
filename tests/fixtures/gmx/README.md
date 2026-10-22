# GMX v2 (Arbitrum) fixtures

`g0_examples_2026-10-22.json` holds the rows behind the worked examples in
`docs/venues/gmx_arbitrum.md` (the G0 design note) and
`tests/test_gmx_g0_examples.py`. They were recorded on **2026-10-22** from
GMX's public, keyless APIs during research (`docs/research/2026-10-22-gmx.md`
section 11), never by tests (invariant 6):

| Key | Source request |
|---|---|
| `eth_short_lifecycle` | GMX API `GET https://arbitrum.gmxapi.io/v1/trades?address=<A>&limit=50`: the five `OrderExecuted` rows of one ETH/USD [ETH-USDC] short, 2026-07-07 to 2026-07-10 (open, two collateral top-ups, partial close, full close) |
| `arb_short_liquidation` | same request: the ARB/USD [ARB-USDC] short liquidation (`orderType` 7), 2026-09-02 |
| `xpl_short_close` | same request: the XPL/USD [WBTC.b-USDC] short full close, 2026-10-13 |
| `eth_short_increases_first4` | `GET /v1/trades?address=<A>&limit=300&cursor=300`: the first four ETH/USD [ETH-USDC] short increases of 2026-07-02 (the per-fill truncation drift example) |
| `pre_v22_decreases_with_swap` | `GET /v1/trades?address=<B>&limit=100`: two 2025 decreases with `decreasePositionSwapType` 1 (WETH collateral; used only for the realized-PnL identity) |
| `claim_funding` | Subsquid GraphQL `claimActions` for address A: its first `ClaimFunding` row (17 market/token entries) |
| `eth_daily_closes` | GMX API `GET /v1/prices/ohlcv?symbol=ETH&timeframe=1d&limit=1000`, the `close` of 2026-07-06 to 2026-07-11 (UTC days) |

`g0_research_rows_2026-10-22.json` holds the wider research rows behind two
figures the note quotes (the collateral chain and the per-fill truncation
drift), from the same recording session:

| Key | Source request |
|---|---|
| `eth_short_increases_2026_07_02` | `GET /v1/trades?address=<A>&limit=300&cursor=300`: every `OrderExecuted` ETH/USD [ETH-USDC] short increase (`orderType` 2) of 2026-07-02 in that page, 118 rows (09:39 to 17:43 UTC), sorted by time and log index |
| `address_b_position_changes` | Subsquid GraphQL `positionChanges` for address B: 20 rows on two WETH-collateral positions, sorted by time |
| `address_b_trades_by_order_key` | `GET /v1/trades?address=<B>&limit=100`: the 20 `OrderExecuted` rows whose `orderKey` matches a position change above, in the same order |

Address A was the top all-time PnL address of `accountPnlRankMetrics` on
2026-10-22; address B was the top address of the `month` period.

Anonymisation: every row is verbatim except identity. `account` is replaced by
`0x00…01` (A) or `0x00…02` (B), and every transaction hash and order key by a
synthetic 32-byte value (`0x00…01`, `0x00…02`, …), because an on-chain hash
leads straight back to the trader. The second file continues the numbering
(from `0x00…1c`), reuses the first file's values for the rows both hold, and
replaces each `positionKey` (a hash of the account) the same way; a
`positionChange` `id`, which is a bare transaction hash, gets its
transaction's synthetic value. The `id` fields keep their `:logIndex` /
`:eventName` suffix on the synthetic hash. Market and token addresses are
protocol contracts, not people, and are kept.
