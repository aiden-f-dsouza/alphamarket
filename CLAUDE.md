# AlphaMarket

Verification-layer MVP: traders connect a READ-ONLY Kalshi API key, we ingest
fills/settlements, compute verified track records, and publish public strategy
pages. No money movement of any kind. Full context: BUILD_PLAN.md (architecture,
milestones M0-M5) and AlphaMarket_PRD_v0_2.pdf (long-term vision; most of it is
deliberately out of scope).

## Commands

- Install: `uv sync --all-groups`
- Run dev server: `uv run uvicorn alphamarket.main:app --reload`
- Postgres: `docker compose up -d db` (test DB: `db_test` on :5433)
- Migrations: `uv run alembic upgrade head` / `uv run alembic revision -m "msg"`
- Tests: `uv run pytest`
- Lint/format: `uv run ruff check --fix . && uv run ruff format .`
- Kalshi smoke test: `uv run python scripts/kalshi_smoke.py` (needs .env + demo key)
- Ingestion worker: `uv run python -m alphamarket.worker` (ticks enqueue jobs,
  a claim loop runs them; run several processes safely, see docs/worker.md)
- Stream process (optional): `uv run python -m alphamarket.stream`
- Prod preflight: `uv run python scripts/preflight.py` (must pass before real users)
- Seeded demo: `scripts/seed_demo.py` into a dedicated `*_demo` database (docs/demo.md)
- Paper engine bench: `uv run python scripts/bench_mirror.py [-n N -m MONTHS -k SIGNALS]`
- Code graph: `graphify update .` (see "Code graph" below)

## Layout

- `src/alphamarket/` — app package. `main.py` (app factory), `auth.py`,
  `config.py` (pydantic-settings), `db.py`, `models.py`, `money.py` (fixed-point),
  `vault.py` (envelope encryption: Fernet records, data keys wrapped by a
  `KeyWrapper`, local or AWS KMS), `credentials.py` (the only read/write path
  for `venue_credentials`; every decrypt is audited), `venues.py` (connect/
  disconnect/sync/reconcile API; asset venues import via queued jobs; the
  partner-approval gate), `oauth.py` (auth code + PKCE; Alpaca), `ingest.py`
  (idempotent ingestion: binary fills plus ledger v2 legs, per-event dedupe,
  page runners), `ledger.py` (legs, balance vectors, cash curve over USD
  legs), `valuation.py` (pure: holdings, perp entry, daily equity,
  flow-adjusted TWR, exposure, `equity_before` a fill, `LedgerSweep` for
  many fills in one pass), `valuation_db.py` (the only bridge from ledger rows to valuation inputs), `reconcile.py`
  (per-asset vectors + running balances vs the venue; quarantine; broker
  equity check, flag only), `stats.py` (returns/drawdown/Sharpe/risk
  score/flags, pure functions), `analytics.py` (persists strategy_stats;
  binary venues, unchanged) + `analytics_v2.py` (asset venues: TWR-based
  stats from valuation, extras marked `"valuation": "v2"`), `jobs.py` (Postgres job queue: SKIP LOCKED
  claims, one running job per account, leases, backoff, dead letter, handler
  and page-runner registries), `venue_limits.py` (outbound token buckets per
  credential and per egress IP, adaptive 429 backoff, circuit breaker;
  `call_venue` makes web-request calls fail fast as "busy"),
  `worker.py` (APScheduler ticks enqueue; JobRunner runs sync, backfill,
  reconcile, stats, prices, signals, mirror, scope_recheck, key_rotate,
  aggregator_delete_user: `aggregator_jobs.py` retries a failed SnapTrade
  deleteUser), `stream.py` (P7, optional `python -m alphamarket.stream`:
  websockets for creators with active paper allocations; `StreamMode` fills
  (Hyperliquid userFills via `ingest_asset_fills`) or nudge (Kraken
  executions, Coinbase user: queue a sync); per-account leases are `jobs`
  rows of kind `lease`, Hyperliquid 10-users/IP slots per `EGRESS_IP`;
  signals queued via `jobs.enqueue_fresh`; the poller stays the truth),
  `auth.py` + `sessions.py` + `mfa.py` + `account.py` (DB sessions, TOTP +
  step-up, email verify/reset, sessions page), `ratelimit.py` (inbound limits,
  memory or Postgres), `email.py` (console or SMTP; `security_event` audits
  and notifies), `csrf.py` + `security_headers.py` (Origin check, CSP),
  `audit.py` (append-only `audit_log`, request/job context), `alerts.py`,
  `redaction.py` (log/secret scrubbing), `scopecheck.py` (nightly read-only
  re-proof; writable keys are erased and the account suspended),
  `strategies.py` (listing editor), `public.py` (public page data + JSON API
  + marketplace listing; venue/asset filters), `charts.py` (SVG polyline
  geometry, pure),
  `demo.py` (SAMPLE data for DEMO_MODE; invariant 7), `marks.py`
  (deterministic strategy sigils + tag colours, pure), `marketdata.py`
  (binary public quotes -> `market_prices`, marks), `prices.py` (asset
  market data: `prices_daily`, `asset_quotes`, funding rates, corporate
  actions, margin tiers, CoinGecko fallback via `connectors/coingecko.py`),
  `signals.py` (creator fills -> append-only `signals`; asset venues go to
  `paper_signals.py`, signals v2 with the quote captured at observed_at),
  `follows.py` (follow/unfollow + `/api/feed`; the one signal renderer),
  `mirror.py` (binary paper mirroring: sizing, fee model, settlement; asset
  allocations dispatch to `paper.py`: sizing, fill price, fees over the
  `paper_book.py` follower ledger (`book_state` = `load_book` from a
  `paper_book_checkpoints` cache plus newer rows via `valuation.LedgerFold`;
  written only at the end of `process_asset_allocation`, count-verified on
  use, sampled nightly by housekeeping; the cache is never the truth), and `paper_lifecycle.py`: funding,
  liquidation, splits, dividends), `execution.py` (Executor; paper only,
  live raises), `portfolio.py` (allocation valuation, drawdown stop,
  snapshots), `tails.py` (allocations API + `/api/portfolio`),
  `watchlist.py` + `watch_pages.py` (private watchlist: public Hyperliquid
  addresses, one shared system-owned `is_watch` account per address, its
  strategy of kind "watch" never public; `/watch`, `/api/watch`; plan in
  docs/copy_strategies_*.md), `discover.py` (`/watch/discover`: venues'
  public leaderboards cached in `leaderboard_snapshots` by the
  `watch_discover` job, addresses only, never names; behind
  `WATCH_DISCOVER_ENABLED`, on in prod via fly.toml `[env]`),
  `consent.py` + `connect_pages.py` + `pending.py` (consent screens,
  Hyperliquid signed claim, SnapTrade portal and PENDING accounts),
  `rotate.py` (replace a key on an existing account), `oauth_jobs.py`
  (OAuth refresh tick/job), `housekeeping.py` (nightly pruning),
  `activity_flags.py` + `account_status.py` (unsupported activity,
  dashboard state),
  `pages.py`
  + `templates/` (Jinja HTML: `/` investor landing, `/creators`,
  `/marketplace` (`/explore` redirects), `/s/{slug}`, `/s/{slug}/tail`,
  `/portfolio`, `/u/{handle}`, `/feed`, `/invite`, `/verification`
  (= `/methodology`), `/transparency`, `/research`, `/security` (these four
  share the Learn sub-nav), `/login`, `/signup`, dashboard,
  `/dashboard/connect`; `_macros.html` holds the logo, icons, number
  formatting, the Learn tabs and the landing card; charts are inline SVG.
  Nav menus are `<details>` elements; the only client JS closes them,
  copies the invite link, and adds the equity-chart draw-in and crosshair
  (reads the `data-series` the server rendered; never fetches). Stubbed
  controls are disabled buttons, never `href="#"` (pinned by a test).
  Marketplace defaults to browsing cards; `?view=table` is the comparison
  table. The settled-trades ticker (`public.recent_public_settlements` +
  the `ticker` macro) is shelved: built and tested, rendered nowhere.
  Brand is **Koine** (package name unchanged); design tokens live in
  `base.html` (warm off-white, Newsreader headlines, IBM Plex body/mono,
  navy from the logo for CTAs, green reserved for "verified").
- `design/koine/` — the Koine design pack (`build.py` generates the .dc.html
  artboards; canvas linked from BUILD_PLAN). `design/` root holds the older
  AlphaMarket dark mockups. Both excluded from ruff
- `src/alphamarket/connectors/` — `base.py` defines the venue-agnostic contract
  (VenueConnector + Normalized* models, MarketDataSource for public quotes,
  and `VenueProfile`: credential kind, balance observability, tick, step,
  paper fee rate/precision, ownership proof). Contract v2 (read its comments
  before touching a connector): instruments, legs and events with asset
  codes, per-asset decimals, per-account ids, `Page` cursors +
  `resume_cursor`, the `Throttle` hook, `prove_read_only`, `account_ref`,
  `AssetVenueConnector`/`AssetMarketData` bases. One module per venue:
  `kalshi.py`, `polymarket.py` (binary), `kraken.py`, `hyperliquid.py`
  (address + EIP-191 signed claim), `coinbase.py`, `binanceus.py`,
  `alpaca.py` (OAuth), `ibkr.py` (Flex XML, daily), `snaptrade.py`
  (aggregator, daily). `registry.py` (`PROFILES`, `get_profile`,
  `build_connector(venue, env, key_id, secret, account_ref)`,
  `build_market_data`) is the only import path core code may use
- `alembic/` — migrations, hand-written for ledger tables (0009 ledger v2,
  0010 jobs, 0011 auth, 0012 vault/audit, 0013 wave 2: signals v2, paper
  asset book, market data, consent, activity flags; 0014 market data
  coverage, 0015 `users.is_demo`, 0016 paper book checkpoints (a cache,
  not append-only) and mirror ledger indexes; 0017 `users.is_house` /
  `is_system`, `strategies.kind`, `watches`, `watch_optouts`; 0018
  `leaderboard_snapshots`, a mutable cache)
- `docs/methodology.md` — public methodology page source; every ledger/stats
  formula lands here alongside its unit test. `docs/security.md` (served at
  /security), `docs/launch.md` (M5 outreach), `docs/deploy.md` + `Dockerfile`
  + `fly.toml` — deployment (Fly: web + worker processes),
  `docs/worker.md`, `docs/venues/<venue>.md` (terms position per venue,
  invariant 10), `docs/security_roles.sql`, `docs/platform_plan.md` +
  `docs/implementation_plan.md` (the current plan and its status section)
- `scripts/` — `preflight.py`, `<venue>_smoke.py` (read-only checks against a
  real account), `record_fixture.py`, `backup_db.sh`, `restore_drill.sh`,
  `rotate_credentials.py`
- `tests/` — pytest; unit tests use in-memory SQLite via the `client` fixture;
  `@pytest.mark.pg` tests take `pg_session_factory` (a fresh migrated
  database per session on db_test, skipped when it is down); `authutil.py`
  (`secure_account`: verified email + TOTP + step-up, needed before connect)

## Invariants (do not violate, do not "improve away")

1. All money is INTEGER MICRO-DOLLARS (1e-6 USD), via `money.to_micros`.
   Floats never touch stored monetary values. Cents are NOT enough: Kalshi
   reports sub-cent decimals (`fee_cost: "0.139800"` = 13.98c), and rounding
   them corrupts the equity curve that M2 reconciliation must verify.
   Contract quantities share the 1e6 scale (`count_fp` is fractional).
   Asset quantities (coins, shares) are integer ATOMS with a per-asset
   `decimals` (<= 12), parsed only from the venue's decimal string via
   `money.to_atoms`; never sum atoms across scales without rescaling.
2. All timestamps are timezone-aware UTC.
3. `fills` and `settlements` are APPEND-ONLY. No UPDATE, no DELETE, ever.
   Corrections are new rows. Idempotency comes from unique venue ids.
   `signals`, `mirror_fills`, `mirror_settlements`, `ledger_entries`,
   `asset_fills`, `ledger_quarantines`, `audit_log`, `mirror_asset_fills`,
   `mirror_ledger_entries`, `venue_activity_flags`, `consent_texts`,
   `venue_consents` and `watch_optouts` share the rule (ORM guard + Postgres
   `<table>_append_only` triggers; preflight checks the set).
4. Venue credentials (keys, OAuth grants, report tokens) must be read-only
   and are never logged, never committed, never returned by any API. They
   live only in `venue_credentials`, envelope-encrypted with a KMS-wrapped
   data key (the local wrapper is refused in prod); every decrypt is audited;
   `prove_read_only` re-runs nightly and a key that became writable is erased
   and its account suspended. PEMs for scripts live in `keys/` (gitignored).
   A venue with no credential (Polymarket) proves ownership with a claim
   code in the venue profile bio; unclaimed accounts never appear publicly.
5. Public pages show ONLY platform-computed numbers from venue data.
   Self-reported performance is never displayed (PRD TR-1).
6. No live API calls in tests. Use respx mocks or recorded fixtures.
7. `DEMO_MODE` (default false) is the only way sample numbers reach a page.
   Sample pages carry the "Sample data" banner, demo-only routes 404 when it
   is off, and `scripts/preflight.py` refuses to launch with it on. Never
   merge sample and real rows without the `demo` flag on the sample row.
8. Tailing is PAPER ONLY. No order is ever placed in anyone's account and no
   write-scoped key is ever accepted: `execution.get_executor("live")`
   raises, the API returns 403 for live mode. Real mirroring needs a licensed
   party (regulatory_reference.md); do not add a flag that turns it on.
9. Koine never accepts a venue username or password: only API keys, OAuth
   grants, report tokens, aggregator connections or public addresses.
10. A venue is connectable only with a `docs/venues/<venue>.md` terms
    position and a consent screen. Every connect path (form, JSON
    `POST /venues`, OAuth state, SnapTrade start) carries `consent_sha256`
    of the exact text shown, stored in `consent_texts`/`venue_consents`;
    JSON `POST /venues` refuses OAuth and aggregator venues. `requires_partner_approval=True` refuses
    connects until the written approval is on file (the flag flips);
    `ENABLE_UNAPPROVED_VENUES` opens them for dev only and preflight refuses it.
11. Connecting a venue needs a verified email, TOTP and a fresh step-up
    (`mfa.require_connect_ready`); do not add a path around it.

## Conventions

- SQLAlchemy 2.0 style (Mapped/mapped_column), Pydantic v2.
- New venue = new module implementing VenueConnector plus a VenueProfile in
  `registry.PROFILES`; core code never imports a venue module directly
  except through the registry. Venue constants (fees, ticks) live in the
  profile, never in core modules.
- Every stats/ledger formula gets a unit test with hand-computed expected
  values, and gets documented for the public methodology page.
- Line length 100, ruff is the only formatter.
- Plans and milestone tracking live in BUILD_PLAN.md; keep it updated when
  scope changes.

## Code graph (graphify)

`graphify-out/graph.json` is a tree-sitter call/import graph of the repo
(local, no LLM; gitignored). Git post-commit/post-checkout hooks rebuild it;
run `graphify update .` after `git pull` or before querying uncommitted work.
`.graphifyignore` excludes `design/` and PDFs. Use it for navigation and
blast radius, then read the files it points to:

- `graphify affected "<symbol>"`: every caller/importer with file:line. Run
  before changing a shared function, model or connector method.
- `graphify explain "<symbol>"`: a node's in/out edges (calls, imports, tests).
- `graphify path "<A>" "<B>"`: how two symbols connect (`--undirected` if none).
- `graphify god-nodes`: the hubs (Strategy, to_micros, VenueAccount, ...).

Free-text `graphify query "<question>"` is keyword-matched and noisy here;
prefer the exact-symbol commands above, or grep. Decisions and rationale live
in BUILD_PLAN.md and this file, not the graph.
