# Koine-run strategies and private watchlists: implementation plan (2026-10-07)

Implements `docs/copy_strategies_plan.md` (the what and why). This file is the
how: file-level changes, the order to build them in, the frontend work, and the
open decisions. Status lines get ticked as steps land.

## 0. What the code survey changed in the plan

Findings from reading the code that the feature plan did not account for:

1. **`/u/{handle}` is demo-only** (`pages.creator_profile` calls `_demo_or_404`;
   a seeded creator redirects to `/s/{slug}`). There is no real user page to
   put a "Run by Koine" badge on. Plan: drop `/u/{handle}` from the house scope;
   the badge goes on every strategy surface instead.
2. **Transparency counts every quarantined/delisted strategy**
   (`pages._transparency`, `n_removed`), with no visibility filter. A watched
   strategy that gets quarantined would inflate "ever listed / removed".
   Must exclude `kind = "watch"`.
3. **Portfolio links and handles come from the strategy slug and owner**
   (`pages.portfolio`, `portfolio_view`). A tail of a watched strategy would
   show the system user's display name and link to a `/s/{slug}` that 404s.
   Needs a watch-aware row.
4. **No uniqueness on `venue_accounts (venue, key_id)`.** A creator can already
   connect a Hyperliquid/Polymarket address unclaimed. The watch account is a
   separate row owned by the system user; the partial unique index applies to
   system-owned rows only.
5. **The sync tick has one global interval** (`jobs.enqueue_due_syncs` →
   `next_sync_slot(account, now, interval)`). Slower cadence for untailed
   watches means a per-account interval there, not a new status. "Dormant"
   (no watcher, no tail) is derived from data and skipped by the tick; no
   status column changes, nothing to undo when someone watches again.
6. **Polymarket is a binary venue** (inline `sync_account` on connect);
   Hyperliquid is an asset venue (queued backfill). The watch add flow queues
   a job for both so the web request stays a single cheap venue call.
7. **Stream candidates sort by allocations, then follows, then id**
   (`stream.streamable_accounts`). Add "is watch" as the first sort key.
8. **Every public query already funnels through `public.publicly_visible`**
   (marketplace, strategy page + JSON, signals JSON, feed, ticker, landing
   featured, seeded creator redirect, tail and follow). Adding the watch check
   inside `is_visible` covers all of them; the visibility test enumerates them.

## 1. Data model (migration 0017)

`alembic/versions/0017_house_and_watchlist.py`, hand-written like 0015:

| Change | Detail |
|---|---|
| `users.is_house` | `BOOLEAN NOT NULL DEFAULT false`. A normal user otherwise. |
| `users.is_system` | `BOOLEAN NOT NULL DEFAULT false`. Owns shared watch accounts. Password hash is unusable (`"!"`), so login always fails; `auth` refuses it explicitly too. |
| `strategies.kind` | `VARCHAR(10) NOT NULL DEFAULT 'creator'`; `'watch'` for watched addresses. |
| `watches` | `id, user_id FK, strategy_id FK, label VARCHAR(60), created_at`; unique `(user_id, strategy_id)`; index on `strategy_id`. Mutable (delete on remove): not append-only. |
| `watch_optouts` | `id, venue, address (lowercased), proved_by_user_id FK, created_at`; unique `(venue, address)`. Append-only (an opt-out is a record we must keep); ORM guard + `watch_optouts_append_only` trigger + preflight set. |
| Shared-account index | Partial unique index on `venue_accounts (venue, lower(key_id))` `WHERE user_id = <system user>` is not expressible without a constant id, so: add `venue_accounts.is_watch BOOLEAN NOT NULL DEFAULT false` and a partial unique index `WHERE is_watch` (Postgres) / plain check in code for SQLite tests. |

Models: `User.is_house`, `User.is_system`, `Strategy.kind` (`StrategyKind`
StrEnum), `VenueAccount.is_watch`, `Watch`, `WatchOptout`.

## 2. Workstream A: "Run by Koine" (house strategies)

| # | Change | Files |
|---|---|---|
| A1 | `operator_of(owner) -> "koine" \| "creator"`; `operator` key in `page_data`, `list_visible` rows, JSON API, `feed_items` | `public.py`, `follows.py` |
| A2 | Marketplace filter `operator=koine\|creator` (absent = all). Sort untouched | `public.list_visible`, `pages.marketplace`, `pages._marketplace_rows` |
| A3 | Badge on every strategy surface (see section 5) | templates |
| A4 | Methodology section "Run by Koine"; security note that Koine's own trading keys never touch the platform | `docs/methodology.md`, `docs/security.md` |
| A5 | `scripts/create_house_user.py`: creates the user with `is_house`, prints the TOTP + connect steps. No shortcut around invariant 11 | `scripts/` |
| A6 | Tests: badge on every surface (cards, page, table, JSON, feed, tail page, portfolio row); marketplace order identical with `is_house` on and off; a house account with a writable key is suspended by `scopecheck` | `tests/test_house.py` |

## 3. Workstream B: private watchlist

### B1. Visibility (lands with the migration, before any route)

- `public.is_visible`: `strategy.kind == "watch"` returns False first.
- `pages._transparency`: `n_removed` filters `Strategy.kind == "creator"`.
- `tests/test_watch_visibility.py`: one parametrised test over every public
  query (marketplace cards + table, `/api/strategies/{slug}`, `/signals`,
  `/feed` + `/api/feed`, landing featured, ticker, `/transparency` counts,
  seeded creator redirect, `/s/{slug}`, `/s/{slug}/tail`, follow). Mirrors
  `test_demo_visibility.py`.

### B2. Core module `watchlist.py`

- `system_user(db)`: get-or-create the single `is_system` user.
- `normalize_address(venue, raw)`: venue in `WATCHABLE_VENUES =
  {"hyperliquid", "polymarket"}`, `0x` + 40 hex, lowercased. 422 otherwise.
- `add_watch(db, user, venue, address, label, request)`:
  1. refuse opted-out addresses (`watch_optouts`), per-user cap
     (`settings.watch_max_per_user`, default 10), global cap
     (`settings.watch_max_tracked`), inbound rate limit (`ratelimit.py`);
  2. if a **claimed, publicly visible** creator strategy exists for the
     address, return it instead (the UI sends the user to `/s/{slug}`);
  3. find the shared `is_watch` account; if absent, one fail-fast venue call
     through `web_venue_call` (`check_connection` + `take_anchor`, exactly the
     anchoring `connect_account` does, no credential, no consent row, no
     claim code), create account + strategy (`kind="watch"`, `public=False`,
     name `"Hyperliquid 0x12ab…89cd"`, slug `watch-{id}` so the slug never
     carries the address), enqueue `BACKFILL` (asset) or `SYNC` (binary);
  4. insert the `Watch` row; audit `WATCH_ADDED` (no address in the user's
     security email; the address is not secret but there is no reason to
     send it around).
- `remove_watch(db, user, strategy_id)`: delete the row, stop the user's active
  allocation on it (`tails.stop_by_user`), audit.
- `watch_strategy_or_404(db, user, strategy_id)`: the user has a `Watch` row,
  else 404 (same 404 for "does not exist" and "not yours").
- `tailable_strategy_or_404(db, user, slug)`: publicly visible, or a watch
  strategy the user watches. `tails.create_allocation` switches to it.

### B3. Routes

| Route | What |
|---|---|
| `GET /watch` | list + add form (login required, else redirect to login) |
| `POST /watch` | add (form; CSRF via existing Origin check) |
| `GET /watch/{strategy_id}` | private strategy page |
| `POST /watch/{strategy_id}/label` | rename (private label) |
| `POST /watch/{strategy_id}/remove` | remove (+ stops the tail) |
| `GET/POST /watch/{strategy_id}/tail` | tail form, then the normal allocation path |
| `GET /api/watch`, `POST /api/watch`, `DELETE /api/watch/{id}` | JSON equivalents, owner-only |
| `GET/POST /watch/optout` | step B6 |

All `/watch*` responses: `Cache-Control: private, no-store` and
`<meta name="robots" content="noindex">`.

### B4. Scheduling

- `jobs.enqueue_due_syncs`: for `is_watch` accounts, interval =
  normal if any active allocation, else `settings.watch_idle_interval_seconds`
  (default 3600); skipped entirely when no `Watch` row and no active
  allocation (dormant). Priority below creators (`PRIORITY_SYNC_WATCH`).
- `stream.streamable_accounts`: order by `is_watch` first (creators always
  outrank), then allocations, follows, id. The slot-eviction logic in
  `StreamSupervisor.plan` already releases lower-ranked accounts.
- Tests: cadence per state; a claimed creator takes a slot from a watch.

### B5. `trader_profile.py` (pure) + page section

Metrics from the plan (holding periods, frequency, time-of-day/weekday,
concentration, sizing, perps leverage/skew/funding, prediction-market price
buckets, copy feasibility vs `max_copy_lag_seconds`). Each gets hand-computed
unit tests and a section in a new `docs/watchlist_help.md` (private help, not
methodology). Computed on demand for the page; cache in `strategy_stats.extras`
only if it proves slow.

### B6. Opt-out

`/watch/optout` (logged in, verified email): venue + address, then the existing
proof (`connector.claim_payload` + EIP-191 signature for Hyperliquid, bio code
for Polymarket). On proof: insert `watch_optouts`, delete every `Watch` on the
address, stop allocations on it, mark the shared account dormant. Linked from
`/security` and the footer.

### B7. Later: `/watch/discover` (leaderboards). Not started until B3 ships and
the venue terms in step 0 are settled.

## 4. Build order

| Step | Work | Blocks |
|---|---|---|
| 0 | `docs/venues/polymarket.md` (missing today, invariant 10); hyperliquid.md watchlist section. Drafted here with `[VERIFY]` marks; terms position is Aiden's/counsel's | B3 going live, B7 |
| 1 | Migration 0017 + models + B1 visibility + tests | everything |
| 2 | Workstream A (A1-A6) | - |
| 3 | B2 + B3 + frontend for B3 | 1 |
| 4 | B4 scheduling | 3 |
| 5 | B6 opt-out | 3 |
| 6 | B5 trader profile | 3 |
| 7 | B7 discover | 3, step 0 |

Steps 2 and 3 can run as parallel agents once step 1 is merged (they touch
different templates except `_macros.html`, where A only adds a macro).

## 5. Frontend changes

### House ("Run by Koine")

- `_macros.html`: new `operator_badge(op)` macro (navy outline chip, Koine logo
  mark, "Run by Koine"). Green stays reserved for "verified".
- Placed in: `strategy_card` (marketplace cards, landing featured card),
  marketplace table row (`marketplace.html`, next to the name), `strategy.html`
  header (beside the verification tier), `tail.html` header, `feed.html` post
  header, `portfolio.html` allocation rows.
- Marketplace: a "Run by Koine" filter chip group (All / Only Koine / Exclude
  Koine) in the existing filter bar, as `?operator=`.
- Strategy page: the description block keeps its "unverified" label; for house
  strategies the byline reads "Run by Koine" with a link to the methodology
  section instead of a creator name.
- `/verification` (methodology): new "Run by Koine" section.

### Watchlist

- **Nav** (`base.html`): "Watchlist" link for logged-in users, desktop links
  and the mobile `<details>` menu; active state via `nav_active`.
- **`/watch`** (new `watch.html`): header with "N of 10 addresses"; add form
  (venue select limited to Hyperliquid/Polymarket, address input with
  `pattern`, optional private label); rows with label, venue, shortened
  address, track days, total return, max drawdown, sparkline, tail status
  ("Tailing $X" / "Not tailed"), Remove button. Empty state explaining what
  can be watched and why it stays private. Error states: bad address,
  cap reached, address opted out, address unknown to the venue, venue busy,
  "already a verified Koine strategy" with a link to `/s/{slug}`.
- **`/watch/{id}`**: `strategy.html` rendered with a `watch` context:
  - private banner (distinct style from "Sample data"): "Public address, not
    a Koine member. We do not know who this is. Only you can see this page.";
  - byline shows the private label + shortened address (copy button reuses the
    invite-link copy JS) instead of a creator name; no description block;
  - verification block reworded: venue data, address not claimed;
  - Follow button hidden (watching replaces following); Tail button posts to
    `/watch/{id}/tail`; "Rename" and "Remove from watchlist" controls;
  - from B5: a "How this trader trades" section (holding-period histogram,
    weekday x hour heatmap, top instruments bar list, sizing and leverage
    stats, prediction-market price buckets, copy-feasibility stat), inline
    SVG like the existing charts.
- **`/watch/{id}/tail`**: `tail.html` with the watch context (back link and
  header point at `/watch/{id}`, same private banner).
- **Portfolio** (`portfolio.html`, `pages.portfolio_view`): rows for watch
  tails show the private label and a "Private watch" tag instead of the
  creator handle and link to `/watch/{id}`.
- **Opt-out** (`watch_optout.html`): address form, then the sign/bio-code step
  reusing `claim.html` pieces. Footer + `/security` link.
- Later: `/watch/discover` table with "Add to my watchlist" buttons.

## 6. Decisions (answered 2026-10-07)

1. House accounts: real money only (plan default; not re-asked).
2. Watchlist: free for every logged-in user, cap 10 (`watch_max_per_user`).
3. Watch add gate: verified email + inbound rate limit + cap. Not the connect
   gate: no credential and no venue consent are involved.
4. Watched strategies' signals in the watcher's own `/feed`: yes, private,
   labelled (default, not re-asked).
5. `/u/{handle}` badge: dropped; every strategy surface carries it instead.
6. Address already connected by a Koine creator but unclaimed: separate shared
   watch account (default, not re-asked).
7. This session builds steps 0-3; steps 4-7 follow.

## 7. Status (2026-10-07, branch `copy-strategies`)

Done: steps 0-3.

- Step 0: `docs/venues/polymarket.md` (draft, needs counsel) and the
  Hyperliquid watchlist section. The Polymarket terms' data clause (section 4.2,
  "Capital Market Clients" such as fintechs) may cover Koine, so Polymarket
  watches are off by default: `watch_venues = "hyperliquid"`.
- Step 1: migration 0017, models, `is_visible` refuses `kind = "watch"`,
  transparency counts only creator strategies, the system user cannot log in
  or reset a password (`tests/test_watch_visibility.py`).
- Step 2: "Run by Koine" badge on cards, table, landing featured card,
  strategy page byline, tail page, portfolio row and feed posts; `operator`
  in the JSON API, feed and portfolio JSON; `?operator=` marketplace filter;
  methodology and security sections; `scripts/create_house_user.py`
  (`tests/test_house.py`).
- Step 3: `watchlist.py` (rules + `/api/watch`), `watch_pages.py`, templates
  `watch.html`, `watch_strategy.html`, the shared `_strategy_body.html`; tails
  accept the user's own watches; portfolio rows name a watch by its label
  (`tests/test_watchlist.py`).
- Seeded demo (`scripts/seed_demo.py`, `docs/demo.md`): a Koine-run creator
  (`koinelab`, `is_house`), a demo system user owning one synthetic watched
  Hyperliquid address, and the investor's and analyst's watches plus the
  investor's paper tail of it (`tests/test_seed_demo_watch.py`, pg twin in
  `tests/test_seed_demo_pg.py`).

Changed from the plan:

- Decision 4 (watched signals in the watcher's feed) is deferred: `/feed` is
  still demo-only, so there is no real feed to put them in.
- No `/watch/{id}` "how this trader trades" section yet (step 6).

Found along the way:

- Polymarket retires Data API v1 on 2026-10-24; `connectors/polymarket.py`
  calls `/activity` and `/positions` (v1). Needs a port to `/v2/...` and
  re-recorded fixtures before then, independent of this feature. Done
  2026-10-08 on branch `polymarket-data-v2`: `/v2/activity` and `/v2/positions`
  with cursor paging, fixtures re-recorded live, dedupe ids unchanged (checked
  against v1 on a live wallet).
- `/s/{slug}` returned a 500 for a public strategy with no stats yet
  (`_worst_periods(None)`); fixed and pinned in `tests/test_worst_periods.py`.

Next: step 4 (slower cadence for untailed watches, dormant accounts skipped,
creators outrank watches for stream slots), step 5 (opt-out), step 6
(`trader_profile.py`).
