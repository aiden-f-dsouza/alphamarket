# Copy tickets (2026-10-22)

Real-money copying without Koine ever placing an order. A paper tail can turn
on copy tickets: each trade the paper tail copies also becomes a ticket, the
same trade written as an order the user can place themselves on the venue.
When the user has connected their own account on that venue (read-only, as
today), Koine checks whether they placed it and shows their real result next
to the paper one.

Status (2026-10-22): v1 built on `feat/copy-tickets`, off by default
(`COPY_TICKETS_ENABLED`). Done: migration 0019, `copy_tickets.py` (issue,
expire, match, stats, settings, views, email), the worker hooks, `/copy`,
the settings form route and the JSON routes, methodology "Copy tickets",
tests (tests/test_copy_tickets.py). Not done yet: the portfolio settings
block, the tail form checkbox and a /portfolio notice for `copy_saved`
(those templates were being changed elsewhere; `allocation_copy_context`
holds what they need). See "Implementation notes (v1)" at the end for where
the build refines this plan.

## Why this shape

- Invariants 4 and 8 stay as they are: no write-scoped key, no order placed
  anywhere, no automation. The user places every order by hand on the venue.
- regulatory_reference.md (section 3, row 2) rates "signal + flat sub, manual
  replicate" as no registration if impersonal and CTA if tailored or
  automated; SignalPush is the cautionary case for automation. Tickets are
  manual. Per-user sizing is the tailored part, so it sits behind its own
  switch (`COPY_TICKETS_SIZED`). With it off, a ticket shows only the
  impersonal trade (instrument, side, price, share of the creator's equity),
  the same for every follower.
- Rolling back is a config change (`COPY_TICKETS_ENABLED=false` hides every
  surface and stops issuing). The tables stay.
- Never add: one-click order placement, pre-filled order submission through
  a venue API, a write key, or anything that places the order for the user.

## The model

A ticket is derived from a paper fill, not from the raw signal, so it inherits
the paper engine's sizing, step and minimum rules, and the paper tail is the
reference the real copies are measured against.

- Binary allocations: one ticket per new `mirror_fills` row.
- Asset allocations: one ticket per new `mirror_asset_fills` row with
  `reason = "signal"` (paper liquidations, funding and corporate actions do
  not issue tickets).
- Only fills created after the user turned tickets on (`copy_enabled_at`).
  Nothing earlier is replayed.
- Each ticket expires `VenueProfile.copy_ticket_ttl_seconds` after the
  creator's fill (default 1 hour; a stale price is not a trade the user could
  still make at that level). A ticket already past expiry when issued is
  stored as `expired`, so the history shows the trade they could not catch.
- A close (sell to reduce or exit) is still a ticket; the page says to place
  it only if they copied the opening trade.

### Tables (migration 0019)

`allocations` gains:
- `copy_tickets` bool, default false
- `copy_enabled_at` timestamptz, null
- `copy_email` bool, default false: email when new tickets are issued
- `copy_account_id` FK venue_accounts, null: the user's own account on the
  same venue to check copies against (must be theirs, same venue, not a
  watch account, not suspended)

`copy_tickets` (status is mutable; not an append-only table, like
`mirror_skips`):
- `id`, `allocation_id` (index), `user_id` (index), `signal_id`
- `mirror_fill_id` unique null, `mirror_asset_fill_id` unique null; exactly
  one set (check constraint)
- `venue`, `instrument_id` null, `ticker` (venue symbol), `label` (display)
- `side` (yes/no or long/short), `action` (buy/sell)
- `qty_atoms`, `qty_decimals` (binary: count_micros at 6)
- `paper_price` (string, the price the paper fill used), `creator_price`
  (string)
- `stake_bps`: the creator's trade as a share of their equity before it (the
  impersonal size)
- `created_at`, `expires_at`
- `status`: `open` | `done` (user says they placed it) | `skipped` |
  `expired` | `matched` (seen on their connected account); `status_at`
- `matched_qty_atoms`, `matched_price` (VWAP string), `matched_at`,
  `matched_refs` (JSON list of `fill:<id>` / `asset_fill:<id>`)

## Matching

Runs at the end of the `signals` job for an account (every sync of every
account already queues one), for tickets of allocations whose
`copy_account_id` is that account:
- Candidate fills: same venue instrument (binary: ticker and side via the
  account's strategy's `fills`; asset: `asset_fills` on that account and
  `instrument_id`), same action, `filled_at` from the creator's fill time to
  24 hours after the ticket was issued.
- A fill matches at most one ticket. Fills are taken oldest first and summed
  until the ticket's quantity is reached; `matched_price` is their VWAP.
  Partial copies stay matched with the smaller quantity.
- `open`, `done` and `expired` tickets can be matched (expiry only stops the
  page from suggesting the trade; a late copy still counts).

Per ticket, slippage against the paper fill, in bps, signed so positive is
worse for the user: buy `(matched - paper) / paper`, sell
`(paper - matched) / paper`. Per allocation: tickets issued, matched,
median slippage. Formula and unit tests go in docs/methodology.md
("Copy tickets").

## Surfaces

- `/copy`: open tickets first (instrument, action, quantity, paper price,
  creator price, expires in, a "Place on <venue>" link to the venue's trade
  page, Done / Skip buttons), then history with matched results. Disclaimer
  on the page and in every email: Koine does not place orders; this is the
  creator's trade sized to your paper allocation; not a recommendation.
- Portfolio: per allocation, the copy tickets toggle, email toggle, account
  picker, and the copy stats.
- Tail form (strategy card, tail page): an optional "Also give me copy
  tickets" checkbox.
- Email: one message per allocation per mirror pass listing the new open
  tickets, only when `copy_email` is on.
- JSON: `GET /api/copy-tickets`, `POST /allocations/{id}/copy` (settings).

`VenueProfile.trade_url` (template with `{symbol}`, or None to fall back to
`trade_home_url`) builds the venue link. A venue constant, so it lives in the
profile.

## Settings

- `COPY_TICKETS_ENABLED` (default false): every surface and the issuing step.
- `COPY_TICKETS_SIZED` (default true): false shows only the impersonal fields
  (no quantity for the user's amount; stake as a share of equity).

## Implementation notes (v1)

Where the build settles something the plan left open, or differs from it:

- Issuing runs in `worker.handle_mirror` right after `process_allocation`
  commits, so outside the allocation's row lock. Two passes racing on the
  same paper fill are kept apart by the unique `mirror_fill_id` /
  `mirror_asset_fill_id`: the loser's insert is rolled back alone (a
  savepoint) and it issues and emails nothing (tested on SQLite and
  Postgres). A ticket failure is logged and never fails mirroring. A paper
  fill made by `POST /allocations/{id}/run` gets its ticket on the next
  worker pass (issuing is a catch-up over every ticketless fill since
  `copy_enabled_at`).
- "Created after the user turned tickets on" uses the paper fill's
  `created_at` (when Koine wrote it), compared with `copy_enabled_at`.
- Expiry: the global mirror sweep (every sync tick) runs one UPDATE; pages
  also show an open ticket past `expires_at` as expired before that runs.
- Matching: tickets oldest first, real fills oldest first. The fill that
  crosses a ticket's quantity counts only for the part needed but is used
  up (it matches at most one ticket). A partial match keeps taking later
  fills in its window until it is whole; `matched_price` is the VWAP of
  every counted quantity and `matched_at` the time of the last real fill
  used. Skipped tickets never match. Tickets older than 7 days are not
  matched any more (daily venues ingest late, so the 24-hour window is on
  fill time). `matched_qty_atoms` is at the ticket's `qty_decimals`
  (floored if a real fill reports finer).
- Accounts the user may pick: their own, same venue, not a watch account,
  status not suspended, disconnected or pending. The venue env is not
  compared.
- User actions: Done from open, or from expired within 24 hours of issue (the match
  window; an older ticket stops asking), Skip from open; anything else
  (a double click, a ticket matched meanwhile) leaves the ticket as it is.
  Settings changes and Done/Skip are audited (`copy.settings`,
  `copy.ticket_done`, `copy.ticket_skip`). The settings routes share the
  tail routes' `follow_user` rate limit.
- `COPY_TICKETS_SIZED=false` drops the matched quantity too, not only the
  ticket's, and the email and page use a disclaimer that says the trade is
  the same for everyone.
- `trade_url` is a `str.format` template with `{symbol}` (URL-quoted),
  `{base}`, `{quote}`, `{base_lower}` and `{quote_lower}` (canonical codes
  from the instrument; a perp's `-PERP` suffix dropped). Venue links, checked
  2026-10-22:
  - Kraken: `https://pro.kraken.com/app/trade/{base_lower}-{quote_lower}`,
    confirmed (the BTC/USD page's canonical link is `/app/trade/btc-usd`).
  - Binance.US: `https://www.binance.us/spot-trade/{base_lower}_{quote_lower}`,
    confirmed (the BTC page links `/spot-trade/btc_usdt`).
  - Hyperliquid: `https://app.hyperliquid.xyz/trade/{symbol}`, the widely
    used perp form; the app loads any path, so not confirmed for spot pair
    names [VERIFY].
  - Kalshi (`https://kalshi.com/markets`), Polymarket
    (`https://polymarket.com`), Coinbase
    (`https://www.coinbase.com/advanced-trade`), Alpaca
    (`https://app.alpaca.markets`), IBKR
    (`https://www.interactivebrokers.com/portal/`): home link only. Kalshi
    market pages need a slug the ticker does not carry, Polymarket's need
    the event slug, Coinbase refuses automated checks, Alpaca and IBKR have
    no public per-symbol page.
  - SnapTrade: no link (orders go through the user's own broker).
- Integration hooks for the portfolio and tail pages:
  `copy_tickets.allocation_copy_context(db, user, allocation)` returns
  `available`, `sized`, `allocation_id`, `enabled`, `email`, `account_id`,
  `enabled_at`, `can_enable`, `venue`, `venue_label`, `accounts`
  (`[{id, label}]`, never a key id), `action` (the form URL) and `stats`.
  The form posts `enabled` and `email` checkboxes and `account_id` ("" for
  none) to `/portfolio/allocations/{id}/copy`, which redirects to
  `/portfolio?notice=copy_saved`. A tail form checkbox can call
  `set_copy_settings(db, user, allocation, True, False, None)` after the
  allocation is created.
