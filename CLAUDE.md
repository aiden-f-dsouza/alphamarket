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
- Ingestion worker: `uv run python -m alphamarket.worker` (polls connected accounts)
- Prod preflight: `uv run python scripts/preflight.py` (must pass before real users)

## Layout

- `src/alphamarket/` — app package. `main.py` (app factory), `auth.py`,
  `config.py` (pydantic-settings), `db.py`, `models.py`, `money.py` (fixed-point),
  `vault.py` (Fernet credential encryption; needs VAULT_KEY), `venues.py`
  (key connect/disconnect/sync/reconcile API), `ingest.py` (idempotent ledger
  ingestion), `ledger.py` (cash-curve math), `reconcile.py` (venue balance vs
  ledger; quarantine), `stats.py` (returns/drawdown/Sharpe/risk score/flags,
  pure functions), `analytics.py` (persists strategy_stats), `worker.py`
  (APScheduler: polling + nightly reconcile + stats + incubation promotion),
  `strategies.py` (listing editor), `public.py` (public page data + JSON API
  + marketplace listing), `charts.py` (SVG polyline geometry, pure),
  `demo.py` (SAMPLE data for DEMO_MODE; invariant 7), `pages.py`
  + `templates/` (Jinja HTML: `/` investor landing, `/creators`,
  `/marketplace` (`/explore` redirects), `/s/{slug}`, `/s/{slug}/tail`,
  `/portfolio`, `/u/{handle}`, `/feed`, `/invite`, `/verification`
  (= `/methodology`), `/transparency`, `/research`, `/security` (these four
  share the Learn sub-nav), `/login`, `/signup`, dashboard,
  `/dashboard/connect`; `_macros.html` holds the logo, icons, number
  formatting, the Learn tabs and the landing card; charts are inline SVG.
  Nav menus are `<details>` elements; the only client JS closes them and
  copies the invite link. Stubbed controls are disabled buttons, never
  `href="#"` (pinned by a test).
  Brand is **Koine** (package name unchanged); design tokens live in
  `base.html` (warm off-white, Newsreader headlines, IBM Plex body/mono,
  navy from the logo for CTAs, green reserved for "verified").
- `design/koine/` — the Koine design pack (`build.py` generates the .dc.html
  artboards; canvas linked from BUILD_PLAN). `design/` root holds the older
  AlphaMarket dark mockups. Both excluded from ruff
- `src/alphamarket/connectors/` — `base.py` defines the venue-agnostic contract
  (VenueConnector + Normalized* models); one module per venue (`kalshi.py`);
  `registry.py` is the only import path core code may use
- `alembic/` — migrations, hand-written for ledger tables
- `docs/methodology.md` — public methodology page source; every ledger/stats
  formula lands here alongside its unit test. `docs/security.md` (served at
  /security), `docs/launch.md` (M5 outreach), `docs/deploy.md` + `Dockerfile`
  + `fly.toml` — deployment (web + worker processes)
- `tests/` — pytest; unit tests use in-memory SQLite via the `client` fixture

## Invariants (do not violate, do not "improve away")

1. All money is INTEGER MICRO-DOLLARS (1e-6 USD), via `money.to_micros`.
   Floats never touch stored monetary values. Cents are NOT enough: Kalshi
   reports sub-cent decimals (`fee_cost: "0.139800"` = 13.98c), and rounding
   them corrupts the equity curve that M2 reconciliation must verify.
   Contract quantities share the 1e6 scale (`count_fp` is fractional).
2. All timestamps are timezone-aware UTC.
3. `fills` and `settlements` are APPEND-ONLY. No UPDATE, no DELETE, ever.
   Corrections are new rows. Idempotency comes from unique venue ids.
4. Venue credentials must be read-only scoped and are never logged, never
   committed, never returned by any API. PEMs live in `keys/` (gitignored).
5. Public pages show ONLY platform-computed numbers from venue data.
   Self-reported performance is never displayed (PRD TR-1).
6. No live API calls in tests. Use respx mocks or recorded fixtures.
7. `DEMO_MODE` (default false) is the only way sample numbers reach a page.
   Sample pages carry the "Sample data" banner, demo-only routes 404 when it
   is off, and `scripts/preflight.py` refuses to launch with it on. Never
   merge sample and real rows without the `demo` flag on the sample row.

## Conventions

- SQLAlchemy 2.0 style (Mapped/mapped_column), Pydantic v2.
- New venue = new module implementing VenueConnector; core code never imports
  a venue module directly except through a registry/factory.
- Every stats/ledger formula gets a unit test with hand-computed expected
  values, and gets documented for the public methodology page.
- Line length 100, ruff is the only formatter.
- Plans and milestone tracking live in BUILD_PLAN.md; keep it updated when
  scope changes.
