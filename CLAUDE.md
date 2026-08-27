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

## Layout

- `src/alphamarket/` — app package. `main.py` (app factory), `auth.py`,
  `config.py` (pydantic-settings), `db.py`, `models.py`
- `src/alphamarket/connectors/` — `base.py` defines the venue-agnostic contract
  (VenueConnector + Normalized* models); one module per venue (`kalshi.py`)
- `alembic/` — migrations, hand-written for ledger tables
- `tests/` — pytest; unit tests use in-memory SQLite via the `client` fixture

## Invariants (do not violate, do not "improve away")

1. All money is INTEGER CENTS. Floats never touch stored monetary values.
2. All timestamps are timezone-aware UTC.
3. `fills` and `settlements` are APPEND-ONLY. No UPDATE, no DELETE, ever.
   Corrections are new rows. Idempotency comes from unique venue ids.
4. Venue credentials must be read-only scoped and are never logged, never
   committed, never returned by any API. PEMs live in `keys/` (gitignored).
5. Public pages show ONLY platform-computed numbers from venue data.
   Self-reported performance is never displayed (PRD TR-1).
6. No live API calls in tests. Use respx mocks or recorded fixtures.

## Conventions

- SQLAlchemy 2.0 style (Mapped/mapped_column), Pydantic v2.
- New venue = new module implementing VenueConnector; core code never imports
  a venue module directly except through a registry/factory.
- Every stats/ledger formula gets a unit test with hand-computed expected
  values, and gets documented for the public methodology page.
- Line length 100, ruff is the only formatter.
- Plans and milestone tracking live in BUILD_PLAN.md; keep it updated when
  scope changes.
