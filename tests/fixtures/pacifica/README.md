# Pacifica fixtures

Every file here was recorded on **2026-10-22** from Pacifica's **public** REST
API (`https://api.pacifica.fi/api/v1`, no account or key exists or is needed)
and public websocket (`wss://ws.pacifica.fi/ws`) by one-off scripts, never by
tests (invariant 6). The accounts are public trading accounts, not ours. Every
address in a file is a synthetic one (`De3o...`, from
`alphamarket.demo.synthetic_solana_address`); usernames and every other
identity field are removed. Files are named `<endpoint>_<YYYY-MM-DD>.json`.

## From the research pass (verbatim, no address inside)

`/home/afd22/koine-research-fixtures/pacifica/`, an account at fee level 3
(8 open positions, a 13-month history with large deposits):

| File | Request |
|---|---|
| `account_*.json` | `GET /account` |
| `positions_*.json` | `GET /positions` |
| `trades_history_*.json` | `GET /trades/history`, newest 3 rows (`close_short`, two `open_short`), `has_more` true |
| `funding_history_*.json` | `GET /funding/history`, newest 3 rows |
| `balance_history_*.json` | `GET /account/balance/history`, 5 rows (a split deposit with its `deposit_release`, a withdrawal) |
| `book_BTC_*.json` | `GET /book?symbol=BTC` |
| `trades_history_privacy_mode_*.json` | `GET /trades/history` of an address whose leaderboard row has `privacy_mode: true` (the API still serves it); only `client_order_id` replaced |

## Recorded for this build

- `account_small_*.json`, `positions_small_*.json`,
  `trades_history_small_*.json`, `funding_history_small_*.json`,
  `balance_history_small_*.json`: one **complete** account, picked from the
  leaderboard for being tiny and flat (a 5,000 deposit, three trades on SOL,
  one funding row, no open position, 13 months old, so `has_more` is false on
  every stream). All pages in full, verbatim. The test replays it to the
  venue's own `balance`.
  The 0.037046 USDC that replay is below the venue's `balance` is the account's
  4,588 hourly `loan_interest_payout` rows (0.037029 in total; recorded with
  `include_trades=true`, then discarded because the default balance history
  does not return them).
- `trades_history_liquidation_*.json`: six rows verbatim from two accounts that
  were liquidated: four `market_liquidation` fills of one order (a long BTC
  position closed in four parts), one `backstop_liquidation` fill (ETH, fee 0)
  and one `settlement` (ADL) fill (BTC, fee 0). `client_order_id` is null on all
  of them. `balance_history_liquidation_*.json`: the matching
  `GET /account/balance/history?include_trades=true` rows: the four `trade` rows
  whose amounts equal the market-liquidation fills' `pnl`, and the two
  `backstop_liquidation` rows (-125.72372, the fill's pnl, then -146.762287,
  which empties the balance and is in no fill).
- `account_isolated_*.json`, `positions_isolated_*.json`: an account with two
  isolated positions (margin 810.171523 and 1,242.873061) and one cross, for
  "cash = balance + isolated margin" (`account_equity` 3,002.639487,
  `balance` 951.518416).
- `account_not_found_*.json`: `GET /account` for a valid but unused address
  (HTTP 404, `"error": "Account not found"`).
- `info_*.json`: `GET /info`, 78 markets cut to 14: BTC, SOL, ETH, XRP, ZEC,
  BNB, HYPE, PAXG, XMR (crypto), GOOGL, SNDK, EURUSD, XAU (real-world assets,
  all with the `rfq` execution mode) and the spot market `SOL-USDC`. Rows
  verbatim.
- `info_prices_*.json`: `GET /info/prices` for the same symbols. Rows verbatim.
- `info_fees_*.json`: `GET /info/fees` (level 0 taker 0.0004 = 4 bps).
- `funding_rate_history_BTC_*.json`: `GET /funding_rate/history?symbol=BTC&limit=4000`
  cut to the newest 5 rows (hourly); `next_cursor` as served.
- `kline_BTC_1d_*.json`: `GET /kline?symbol=BTC&interval=1d&start_time=1790000000000`
  cut to the first three candles and the still-open candle of 2026-10-22.
- `leaderboard_*.json`: one `GET /leaderboard` (2.2 MB, 6,952 rows, 94 with
  `privacy_mode: true`) cut to 15 rows chosen to exercise the ranking: the
  top public rows by all-time pnl and by 1-day pnl, one row with no volume,
  one tiny account, one with a negative volume figure and three
  `privacy_mode: true` rows. Rows verbatim except `address` (synthetic) and
  `username` (removed). Document order kept.
- `ws_account_trades_*.json`: the websocket. `ack`, `pong` and `error` are the
  real replies to a subscribe, a ping and a malformed subscribe. `frames` are
  four real `account_trades` frames from the most active public accounts: a
  maker close with a fee rebate on a real-world-asset market, a two-row maker
  close, a two-row maker open, a two-row taker close. `u` (the account) and `I`
  (a client order id) are replaced; everything else is as sent.

## Findings worth knowing

- Pages are cursor-paged **newest first** (`next_cursor`, `has_more`), 4,000
  rows at most per page, and a page costs the same 9 credits whatever its
  size. The `/leaderboard` is one 2.2 MB response, 9 credits.
- Same-millisecond rows exist (two fills at `1757417983986`): `history_id` is
  the tie-break.
- `/account/balance/history` has no id field. `balance` is the cash right after
  the row including trading since the previous one, and `amount` and `balance`
  are rounded independently (1e-6 breaks on a merged replay).
- Default `/account/balance/history` omits `trade`, `funding` and
  `loan_interest_payout` rows (returned with `include_trades=true`, in which
  case a 13-month account has 4,588 hourly interest rows).
- `fee` can be negative (a maker rebate); `pnl` is net of the fee in every
  case (`pnl = gross - fee`).
- `GET /account` for an unused valid address: 404. `GET /positions` and
  `GET /trades/history` for it: 200 with an empty list.
- A GET with a body or `Content-Length` gets a 403 from CloudFront.

## Not recorded (from the docs, tests use inline values)

`insolvency_liquidation` fills (a `cause` value in the docs, not seen), spot
balances and fills, `subaccount_transfer` and `payout` balance events, and
`loan_interest_payout` rows as the connector parses them. **Re-record them**
from a real account when one is available.
