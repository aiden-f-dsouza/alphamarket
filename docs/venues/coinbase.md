# Coinbase Advanced Trade

**Status: `needs_approval`.** The connector is built and tested behind
`VenueProfile.requires_partner_approval=True`; the connect form refuses new
Coinbase accounts in production until that flips to `False` (BUILD_PLAN /
`docs/implementation_plan.md` P4.3). No Koine account or connection has ever
touched a real Coinbase key.

## Terms of service position

**Direct re-check attempted 2026-09-26**: fetching
`https://www.coinbase.com/legal/developer-platform/terms-of-service` returned
HTTP 403 both directly and via a search-engine cache; the page could not be
re-read today. The clause below is quoted from
`docs/research/2026-09-16-crypto-venues.md`, which did load the page ten days
earlier and cites it as:

> CDP ToS: may not "collect, cache, aggregate, or store" data other than for
> permitted purposes nor share with third parties without written
> authorization; a public track-record page is exactly "sharing". OAuth
> client creation "limited to approved partners".

That is the clause this connector is gated on. A public strategy page that
shows a Coinbase-derived track record to visitors who are not the account
owner is squarely "sharing" data obtained via the API with a third party
(every other visitor), and Koine's own read (cache-then-serve) is exactly the
"collect, cache, aggregate, store" the clause names. Until CDP partnerships
replies in writing that consented, read-only, public display is permitted (or
grants an explicit exception), Coinbase stays `needs_approval`:
`requires_partner_approval=True` on the profile, and `docs/implementation_plan.md`
section 5 lists "email CDP partnerships for written approval now" as a
founder action, independent of when this code ships.

**Re-verify before flipping the flag**: re-read the ToS in full (not just the
one clause above) once CDP partnerships responds, and record the reply
(date + who signed it) here.

## Consent text for the connect screen

> Koine will read your Coinbase Advanced Trade account with a
> **View-only** API key: your fills, balances, and account activity
> (deposits, withdrawals, staking, conversions). It cannot place orders,
> cancel orders, or move funds — Koine checks this every time it connects
> and every night afterward, and will not display data for a key with any
> trade or transfer permission. Coinbase requires written approval before
> Koine can show this data on a public page; until that approval is granted,
> your Coinbase account stays private to you even if connected.

## Read-only proof method

`ReadOnlyProof.SCOPE_ENDPOINT` via `GET /api/v3/brokerage/key_permissions`
(same proof class as Kalshi's `GET /api_keys`). The response's `can_view`
must be `true` and both `can_trade` and `can_transfer` must be `false`;
`portfolio_uuid` is recorded as `account_ref` (a Coinbase key is scoped to
exactly one portfolio, so this never needs the multi-account disambiguation
Coinbase's Advanced Trade / IBKR / SnapTrade share). Checked at connect time
and re-checked nightly (P3 item 8); a key that becomes writable is suspended.

## What a read-only credential exposes

- Every fill (`GET /orders/historical/fills`): product, side, price, size,
  commission, maker/taker, timestamp. `entry_id` is the stable id —
  **`trade_id` is explicitly documented as NOT unique** for adjusted fills,
  so it is never used for idempotency.
- Per-asset balances (`GET /accounts`): available + on-hold, one row per
  asset the portfolio has ever held.
- Deposits, withdrawals, sends, receives, staking transfers and other
  non-trade activity (`GET /v2/accounts/:id/transactions`, the older
  "coinbase.com" API, accepted with the same CDP key per the docs).
- No withdrawal or trading capability whatsoever, by construction of a
  View-only key; there is no separate "read withdrawal addresses" scope to
  worry about (unlike Kraken's granular permission list).

## Rate limits

**[VERIFY]**: Coinbase's rate-limit documentation page has moved and the
official current numbers were not re-confirmed while writing this connector
(`docs/research/2026-09-16-crypto-venues.md` section 6). The widely-cited
figure is **~30 requests/second per credential** for private (authenticated)
endpoints; this connector's `VenueProfile.rate_budgets` encodes that as a
token bucket (capacity 30, refill 1800/min) plus a conservative, unverified
10 req/s guess for the public market-data mirror. Re-verify against the
current docs (or empirically, once a key exists) before relying on these
numbers under load — see `docs/implementation_plan.md` P2 (outbound limiter).

## History depth

**[VERIFY]**: `/orders/historical/fills` carries no documented retention cap
(unlike Binance's 24h `myTrades` window or Hyperliquid's 10,000-fill limit),
so `VenueProfile.history_depth = "all"`. This has not been confirmed against
a real, long-lived account.

## Streaming (P7)

`CoinbaseUserStream`: Advanced Trade websocket
`wss://advanced-trade-ws-user.coinbase.com`, channels `user` and
`heartbeats` (the latter keeps the subscription open), each subscribe frame
signed with a fresh CDP JWT without a `uri` claim (`build_ws_jwt`, as the
SDK's `build_ws_jwt`). Mode NUDGE: the `user` channel reports orders
(`cumulative_quantity`, `avg_price`, `status`), not fills keyed by
`trade_id`, so a rise in an order's `cumulative_quantity` queues the
account's sync instead of writing anything.

[VERIFY] with a real View-only key: that it may subscribe to `user` at all
(an auth error frame makes the account poll-only for the cool-off); which
portfolio's orders a portfolio-scoped key sees (extra orders only cost an
extra sync); the frame shapes in `tests/fixtures/streams/coinbase_ws_user.json`
(docs examples).

## Known gaps

- **v2 transactions auth is unconfirmed.** The task and the research doc
  both flag `[VERIFY]` on whether a CDP (Advanced Trade) API key actually
  authenticates against `api.coinbase.com/v2/...` (the older "Coinbase App"
  API) at all, versus needing a separate OAuth2 token. If it does not, every
  external-flow event (deposits, withdrawals, staking) is unreachable with a
  read-only CDP key and `transfers_observable` should flip to `False`.
- **Per-asset precision is assumed, not looked up.** Fills and balances use a
  fixed 8 decimals for crypto assets and 6 for USD/USDC rather than each
  product's actual `base_increment`/`quote_increment`. `fetch_instruments()`
  does derive real per-product decimals from the public product list, but
  the fill/balance/event mapping does not yet cross-reference it. Wrong for
  any asset the venue reports at finer than 8dp (none observed as of
  2026-09-26, but not exhaustively checked).
- **`size_in_quote=True` fills use an estimated base quantity**, rounded down
  to an assumed 1e-8 lot size, rather than the exact figure the task says
  lives in the matching `advanced_trade_fill` v2 transaction. Cross-checking
  those two rows is left for when a real account can confirm the join key.
- **Fee currency is assumed to be the quote asset.** The docs' fills example
  does not state what currency `commission` is denominated in; every
  Advanced Trade fee schedule seen is quoted against the quote asset, so
  that is the assumption, unconfirmed against a real non-USD-quoted pair.
- **No portfolio-level equity figure.** `/accounts` has no analogue of
  Alpaca's `account.equity`; `NormalizedBalances.equity_usd_micros` stays
  `None`, and M7's `fetch_balance()` falls back to summing the USD/USDC cash
  row (correct, just conservative — it ignores unrealized crypto P&L, which
  is what the ledger's own valuation is for).
- **Public best bid/ask is empty.** The unauthenticated product feed's
  `best_bid_price` / `best_ask_price` fields are blank strings (confirmed
  live 2026-09-26); `fetch_quote` returns only `last` (the `price` field), no
  spread, until an authenticated call is wired in behind a real key.
