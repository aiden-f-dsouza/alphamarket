# Copy tickets (2026-10-22)

Real-money copying without Koine ever placing an order. A paper tail can turn
on copy tickets: each trade the paper tail copies also becomes a ticket, the
same trade written as an order the user can place themselves on the venue.
When the user has connected their own account on that venue (read-only, as
today), Koine checks whether they placed it and shows their real result next
to the paper one.

Status: v1 being built on `feat/copy-tickets`. Off by default
(`COPY_TICKETS_ENABLED`).

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
