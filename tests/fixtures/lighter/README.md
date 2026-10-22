# Lighter fixtures

Every file here was captured on **2026-10-22** from Lighter's **public**,
unauthenticated endpoints, from a US residential IP, by one-off recording
(the research session's scripts and, for the files marked "W-L", a handful of
requests made while building the connector), never by tests (invariant 6):

- API `https://mainnet.zklighter.elliot.ai/api/v1/...`
- explorer `https://explorer.elliot.ai/api/accounts/{index}/logs`
- read-only websocket `wss://mainnet.zklighter.elliot.ai/stream?readonly=true`

Raw captures (not anonymised, never committed) are in
`/home/afd22/koine-research-fixtures/lighter/` and the W-L session's
scratchpad. Rows below are verbatim except where "anonymised" says what
changed.

## Anonymisation

- Every L1 address is replaced by a synthetic one of the same kind:
  `0x00…00A1` (owner of index 714710), `0x00…00B2` (owner of index 760300),
  `0x00…00C3` (any other), and `0x00…1001` onward (upper-case hex, as
  EIP-55 sends mixed case) for leaderboard rows.
- `name`, `description`, `metadata` and `approved_integrators` are dropped
  from account objects (all empty or Lighter's own in the captures).
- Account indexes are kept: they are what the plan and the tests refer to
  (714710, 760300). Order-book `owner_account_index` fields are dropped.

## Accounts

- **714710** (`0x…A1`): rank 1 of the 30-day leaderboard, a large market
  maker; the research account. One master index, no sub-accounts.
- **760300** (`0x…B2`, W-L): picked from the middle of the all-time
  leaderboard (sorted by volume, offset 60,000) for a short, complete
  history: 145 explorer rows in two pages, from its first deposit
  (2026-10-12) to the capture, plus its account state read minutes later
  (transaction_time 1792694009, 18:33:29 UTC). Fees are 0 (Standard
  account). It holds an isolated-margin BTC long, so the cash check covers
  `allocated_margin`.

## Files

| File | Request | Notes |
|---|---|---|
| `explorer_logs_714710_*` | `accounts/714710/logs?limit=100&offset=0` | verbatim (100 Trade rows, 16:05-16:45 UTC); research `explorer_logs_all_page0` |
| `explorer_logs_<Type>_*` | `accounts/714710/logs?pub_data_type=<Type>&limit=5` | `L1DepositV2` (l1_address anonymised), `WithdrawV2`, `L2TransferV2`, `LiquidationTrade`, `LiquidationTradeWithFunding`, `TradeWithFunding` |
| `explorer_logs_pools_*` | `accounts/762954/logs?limit=100&offset=0` (W-L) | the first `L2MintShares` and `L2StakeAssets` rows only |
| `explorer_logs_760300_offset0_*`, `..._offset100_*` | `accounts/760300/logs?limit=100&offset=0` / `=100` (W-L) | the whole history (100 + 45 rows); deposit l1_address anonymised |
| `account_714710_*`, `account_760300_*` | `account?by=index&value=<index>` | anonymised as above |
| `accountsByL1Address_714710_*`, `..._760300_*` | `accountsByL1Address?l1_address=<owner>` | anonymised |
| `trades_714710_perps_*` | REST `trades?account_index=714710&sort_by=timestamp&limit=100` (research) | the 100 unique LIT-perp rows of the two research captures, ascending by trade id. **A cross-check reference only**: the connector never calls REST `trades` (auth-documented; founder decision 2026-10-22) |
| `orderBookDetails_*` | `orderBookDetails` | 16 perps and 3 spot markets kept (the ones the tests and accounts touch, plus AAPL, XAU, EURUSD, INTC, DELL, ANTHROPIC, BIRB for copyable/inactive cases); `daily_chart` dropped |
| `orderBookOrders_btc_*` | `orderBookOrders?market_id=1&limit=5` | two levels per side kept |
| `fundings_1_*`, `fundings_7_*`, `fundings_186_*` | `fundings?market_id=<m>&resolution=1h&...&count_back=300` (W-L) | BTC, XRP, RKLB hourly values from 2026-10-12 00:00 to 18:00 on 10-22 (rows before 10-12 dropped) |
| `fundings_120_*` | `fundings?market_id=120&...&count_back=60` (W-L) | LIT, for the prefix-sum cross-check |
| `fundings_1_sep_*` | `fundings?market_id=1&...&end_timestamp=1790845200&count_back=100` (W-L) | BTC 2026-09-27..10-01, for the BTC prefix-sum check |
| `candles_btc_1d_*` | `candles?market_id=1&resolution=1d&...&count_back=5` (W-L) | 2026-10-17..22; the last candle was still open |
| `pnlLeaderboard_<window>_<sort>_*` | `pnlLeaderboard?time_window=<w>&sort_by=<s>&sort_dir=desc&limit=<n>&offset=0` | first 12 rows each; addresses anonymised; `30d_pnl` and `all_pnl` from research, `24h_pnl` and `7d_roi` W-L |
| `ws_frames_*` | read-only websocket, `account_all_trades/714710` (research) | `connected`, the subscription snapshot cut to 2 trades, and a `pong` (seen in the W-L probe of `{"type":"ping"}`). The `update/...` frame in the tests is the snapshot with its type changed: no update was captured |

## What the captures pinned (tests cite these)

- Explorer `status` carries no meaning for the ledger: all 100 rows of
  `explorer_logs_714710` (43 `nothing_to_execute`) matched a REST trade
  field for field (price, size, side, both indexes, market, time to 1 ms;
  REST shows only the queried account's own fee). 760300's only deposit is
  `nothing_to_execute` and is in its cash. Other statuses seen: `null`,
  `committed`.
- One trade per tx hash in every capture (including a taker sweep across
  four makers: four hashes).
- Entry quote and realized PnL rule: `trades_714710_perps`, 100 of 100
  rows (test_lighter_replay.py).
- Prefix sums: USD per base atom x 1e12 (`fundings_1_sep`, BTC: exact).
- Full replay of 760300 reconciles with `account_760300` (positions exact,
  USDC within 0.0000025, the funding values' 8-dp rounding).

Not captured, so built from the docs or synthesised in the tests (re-record
from a real account when one exists): a spot buy with a fee (fee asset),
a liquidated account's own rows (only the maker side of liquidations is
here), legacy `L1Deposit` / `Withdraw` / `L2Transfer` shapes, `BurnedShares`
/ unstake rows, a websocket `update/account_all_trades` frame, any explorer
error body.
