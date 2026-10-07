# Koine-run strategies and private watchlists (plan, 2026-10-07)

Two features, planned for a fresh session to build:

1. **Koine-run strategies**: agent-run portfolios that trade real Koine-owned
   accounts and are listed like any creator, labelled "Run by Koine".
2. **Private watchlist**: a user paper-tails a public address (Hyperliquid,
   Polymarket) that is not a Koine user, privately, with analytics on how that
   trader trades.

Section 5 records the other ideas from the same conversation (disclosure
portfolios, live high-speed copying, reverse engineering) so they can be
picked up as offshoots later.

Read CLAUDE.md invariants before starting. The ones this plan leans on: 4
(unclaimed accounts never appear publicly), 5 (public numbers are
platform-computed from venue data), 8 (paper only, no write keys), 10 (terms
position per venue), 11 (connect needs verified email + TOTP + step-up).

---

## 1. Koine-run strategies

### What it is

Koine runs strategies (agent-driven or rule-based) in **real accounts Koine
owns**. Each account connects to Koine through the normal connect flow with a
**read-only** key, exactly like a creator's. Its track record, stats, signals
and paper tails then come from the existing pipeline with no special path, so
the "verified" claim holds unchanged.

### The separation rule (the one hard design constraint)

The agent that places orders **does not live in this repo or on Koine's
infrastructure**. It runs as a separate service (its own repo, e.g.
`koine-agents`, its own host and secrets) holding the venue's trading key.
Koine only ever sees the read-only key for the same account.

Why: invariant 8 says no write-scoped key is ever accepted and no order is
placed from Koine. `scopecheck.py` already erases a key that turns writable
and suspends the account, so a house account connected with a trading key
would be suspended overnight, and that is the right behaviour. Do not add an
exemption.

### Venue choice for the first house accounts

| Venue | Status for a house account | Notes |
|---|---|---|
| Kalshi | Usable | US company, US-friendly; `KALSHI_ENV=prod` waits for M5 sign-off |
| Binance.US | Usable | No partner approval needed; KYC by state [VERIFY Koine's state] |
| Alpaca | After written approval | Equities; the approval gate applies to house accounts too |
| Hyperliquid | **Probably not** | Terms §1.6 exclude US persons from the Interface; a US-incorporated Koine trading there is a terms and counsel question, not a code one |
| Polymarket | [VERIFY] | International Polymarket excludes US persons; Polymarket US is a separate venue Koine does not support yet |

Start with Kalshi and Binance.US. Real money, small size: a paper-trading
account (Alpaca paper) is venue data but not real money, and would need its
own label. Default: do not list paper-account house strategies.

### Product and labelling

- A Koine-run strategy shows a **"Run by Koine"** badge everywhere a strategy
  appears: card, strategy page, marketplace table row, JSON API
  (`"operator": "koine"`), feed items, `/u/{handle}` (the house handle).
- The methodology page gets a section: what "Run by Koine" means, that the
  record is verified the same way, and that the description of the agent is
  Koine's own (unverified text, like any creator description).
- **No ranking advantage.** Marketplace sort and filters treat house
  strategies like any other. No "featured" slot unless it carries the badge
  and the methodology page says how featured slots are chosen.
- Incubation applies (90 days, `INCUBATION_DAYS`); a house strategy is not
  promoted early.
- Marketplace filter chip: "Run by Koine" (include / only / exclude).

### Code changes in this repo (small)

1. Migration 0017: `users.is_house BOOLEAN NOT NULL DEFAULT false` (mirrors
   `users.is_demo`). The house user is a normal user: verified email, TOTP,
   step-up to connect (invariant 11 has no exemption).
2. `public.py`: `operator` field on every strategy payload ("creator" |
   "koine") derived from the owner; marketplace filter; no change to
   `is_visible`.
3. Templates: badge in `_macros.html` (`strategy_card`, page header, table
   row). Pin with a test that every surface rendering a house strategy shows
   the badge (like `test_demo_pages.py` pins sample banners).
4. `scripts/preflight.py`: nothing new; house users are real users. Do not
   reuse the `is_demo` path.
5. `docs/methodology.md` section; `docs/security.md` note that Koine's own
   trading keys never touch the platform.
6. Optional: `scripts/create_house_user.py` that creates the user and prints
   the steps to finish TOTP and connect.

### Outside this repo

- The agent runner: separate repo, separate secrets. Out of scope for this
  plan beyond the separation rule. Log every decision it makes; publishing a
  plain-English description of each house strategy is a product decision.
- Counsel: when Koine itself runs a strategy people can tail (even on paper,
  even under flat subscriptions), does Koine move from publisher toward
  adviser? Ask before listing the first house strategy publicly.

### Tests

- House badge on every surface (cards, page, table, JSON, feed, user page).
- Marketplace sort is identical with and without `is_house` (no boost).
- A house account connected with a writable key is suspended by the nightly
  scope check (existing behaviour, pinned for this case).

---

## 2. Private watchlist (public leaderboard traders)

### What it is

A logged-in user pastes a public **Hyperliquid** or **Polymarket** address.
Koine ingests that address's public fills, computes the same stats, and lets
**that user** follow and paper-tail it. Nothing about the address appears on
any public page, marketplace, feed, API or user profile.

Kalshi, Kraken, Coinbase, Binance.US and the brokers have no public per-account
data, so they are out.

### Why private

Invariant 4: an unclaimed account's strategy is never shown publicly; an
address could be anyone's, and a public page would profile a person who never
signed up. Private tracking of public chain data, shown only to the person who
asked for it, keeps that invariant. If the address's owner later claims it on
Koine, it can be listed through the normal path.

### Data model (reuse the pipeline, add one table)

Reuse `VenueAccount` + `Strategy` so ingestion, signals, valuation, stats,
paper engine and P7 streaming all work without forks.

- **One shared tracked account per (venue, address).** Owned by a system user
  (`users.is_system`, migration 0017 with `is_house`), `claimed_at` NULL,
  `strategies.public = false`.
- `strategies.kind` VARCHAR: `"creator"` (default) | `"watch"`. `is_visible`
  returns False for `kind = "watch"` explicitly (belt and braces on top of the
  unclaimed check).
- Partial unique index on `venue_accounts (venue, key_id)` where the owner is
  the system user, so two watchers share one ingestion.
- New table `watches (id, user_id, strategy_id, label, created_at)`, unique
  (user_id, strategy_id). `label` is the user's private nickname; never shown
  to anyone else.
- Allocations stay as they are; a tail of a watched strategy is a normal paper
  `Allocation`.

### Flows

1. `/watch` (logged in): list of the user's watched addresses with stats;
   "Add an address" form (venue + address). Validate the address format per
   venue (`0x` + 40 hex). Per-user cap (start at 10) and an inbound rate limit
   (`ratelimit.py`).
2. Adding an address that is already tracked links the existing strategy;
   otherwise create the account + strategy and queue a backfill job (asset
   venues already import via queued jobs).
3. `/watch/{strategy_id}`: the strategy page template rendered privately, with
   a banner: "Public address, not a Koine member. We do not know who this is.
   Only you can see this page." 404 for anyone without a `watches` row.
4. Tail from that page: a `watch_strategy_or_404(db, user, id)` used by the
   tail form and `create_allocation`, instead of `visible_strategy_or_404`.
   Everything after allocation creation is unchanged.
5. Remove from watchlist: deletes the `watches` row and stops the user's
   allocation on it. When no watcher and no allocation remains, housekeeping
   stops polling the account (status stays, ledger rows stay: append-only).

### Scheduling and limits

- Watched accounts compete with real creators for the Hyperliquid rate budget
  and the **10 websocket users per egress IP** (`stream.py` orders by
  allocations, then follows). Change the order so claimed creator accounts
  always outrank watch accounts; watch accounts stream only with spare slots.
- Poll watch accounts without a tail on a slower cadence (e.g. hourly) than
  ones with an active allocation (normal interval).
- Global cap on tracked addresses (setting), so a burst of signups cannot
  exhaust the outbound budget.

### "How this trader trades" (the reverse-engineering part, private)

New pure module `trader_profile.py`, every metric unit-tested with
hand-computed values (convention), documented in a private-help section, not
the public methodology page:

- Holding period distribution, trade frequency, time-of-day/day-of-week.
- Instruments and categories traded (concentration, top markets).
- Sizing: position size as share of equity, scaling in/out.
- Perps: leverage used, long/short skew, funding paid.
- Prediction markets: entry price buckets, held-to-settlement share, win rate
  by price bucket.
- Copy feasibility: median time between fills, fraction of fills a follower
  could not copy within `max_copy_lag_seconds`.

### Later: leaderboard browsing

Hyperliquid and Polymarket publish leaderboards [VERIFY endpoints, terms and
rate limits]. A `/watch/discover` page could list top addresses fetched live
(not stored as Koine pages, not indexed) with an "Add to my watchlist" button.
Keep it private and logged-in only. Build after the paste-an-address flow.

### Prerequisites

- **`docs/venues/polymarket.md` does not exist** although Polymarket is
  connectable; invariant 10 wants it. Write it first, covering reading
  third-party addresses.
- Extend `docs/venues/hyperliquid.md` with the watchlist use (reading
  addresses that are not Koine users).
- **Opt-out for address owners:** an owner who proves the address (existing
  signed-message / profile-code claim) can block tracking. `watch_optouts
  (venue, address, created_at)`; adding an opted-out address is refused and
  existing watches stop. Cheap, and the right answer to "you are tracking me".
- Never link an address to an identity: no ENS lookups, no social handles, no
  cross-venue linking.

### Tests

- A watched strategy never appears in: marketplace (both views), `/api/strategies`,
  feed, landing, `/u/{handle}`, the shelved ticker, any sitemap. One
  parametrised test over every public query.
- `/watch/{id}` is 404 for other users and anonymous visitors.
- Two users watching one address share one venue account and one ingestion.
- Tail of a watched strategy runs through the paper engine end to end
  (respx-mocked Hyperliquid, recorded public fixtures exist).
- Opt-out refuses new watches and stops existing ones.
- Stream slot ordering: a claimed creator outranks a watch account.

---

## 3. Build order

| # | Work | Size | Depends on |
|---|---|---|---|
| 0 | `docs/venues/polymarket.md`; hyperliquid.md watchlist section; counsel questions sent | S | - |
| 1 | Migration 0017 (`is_house`, `is_system`, `strategies.kind`, `watches`, `watch_optouts`, partial unique index) | S | - |
| 2 | House badge, operator field, marketplace filter, methodology section, tests | S | 1 |
| 3 | Watchlist add/remove, private page, private tail path, visibility tests | M | 1 |
| 4 | Scheduling: slower cadence for untailed watches, stream priority, global cap | S | 3 |
| 5 | `trader_profile.py` + private page section | M | 3 |
| 6 | Opt-out flow | S | 3 |
| 7 | Leaderboard discover page | M | 3, 0 |
| - | Agent runner (separate repo), first house account on Kalshi/Binance.US | outside | counsel |

Items 2 and 3-6 can run as parallel workstreams after 1 lands.

## 4. Decisions for Aiden

1. House accounts: real money only, or also list paper-account strategies
   with a label? (Plan default: real money only.)
2. Which venue gets the first house account (plan: Kalshi or Binance.US)?
3. Watchlist cap per user (plan: 10) and whether it is a paid-tier perk later.
4. Is the watchlist logged-in-only free for everyone, or creator/follower tier?
5. Counsel: Koine as operator of tailed strategies; Hyperliquid and Polymarket
   for a US company's own trading.

## 5. Parked ideas (noted for offshoots)

**Disclosure-based "known strategies" (Autopilot style).** Copy the disclosed
portfolios of public figures: congressional trades (STOCK Act periodic
transaction reports), hedge funds (SEC 13F), insiders (Form 4).
- Not venue data and 30-45 days late, so it cannot sit next to verified
  records without breaking invariant 5 and the brand. It would need its own
  clearly labelled section ("Disclosed, not verified").
- 13F and Form 4 are SEC public data, free to use.
- Congressional disclosures: the Ethics in Government Act restricts using the
  reports for commercial purposes [VERIFY with counsel; existing products
  rely on readings of the news-media exception].
- **Offshoot:** a Koine-run strategy (section 1) whose agent trades a real
  account *based on* 13F/Form 4 signals. The record is then verified, and
  the disclosure data is only an input. Fits every invariant.

**Live, high-speed copying into users' accounts.** Ruled out by invariant 8:
Autopilot does it as an SEC-registered adviser with broker partners. Koine
already copies at speed on paper (P7 streaming, seconds of lag on
Hyperliquid). **Offshoots:** publish copy lag per strategy as a stat ("could a
follower have kept up?"); a "copyability" score from `trader_profile.py`.

**Reverse engineering leaderboard traders.** Covered privately by section 2's
`trader_profile.py`. **Offshoot:** house strategies whose agents are designed
from patterns observed across many public traders (e.g. "funding-aware
momentum like top Hyperliquid traders"), traded in a real Koine account so
the result is verified rather than inferred.

**Many agent portfolios.** A "strategy lab": several small house accounts
incubating at once, only listed after 90 days and only if they clear a bar
decided in advance (published, so it is not cherry-picking). Survivorship
disclosure: list the ones that were retired too.
