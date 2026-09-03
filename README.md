# Koine (working title; package still `alphamarket`)

Verified track records for prediction-market traders. Connect a read-only
Kalshi API key, get a platform-computed, provably real performance page.
No screenshots, no self-reported numbers, no custody of anything.

Status: M0-M4 built, M5 (real users) in progress. See `BUILD_PLAN.md` for
architecture, milestone status, and open items; `docs/methodology.md` for
every formula; `docs/security.md` for the credential story; `docs/deploy.md`
to ship it.

What works end to end today (verified against a live Kalshi demo account):
connect a read-only key (write-scoped keys rejected via Kalshi's key
registry), ingest fills/settlements into an append-only ledger (Postgres
triggers + ORM guard), nightly reconciliation of ledger cash vs. venue
balance (0.1% tolerance, divergence quarantines the page), stats and risk
score, creator dashboard, and a public strategy page at `/s/{slug}`.

## Quickstart (WSL2 recommended on Windows)

```bash
# 1. Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-groups

# 2. Postgres
docker compose up -d db
cp .env.example .env          # then edit values
uv run alembic upgrade head

# 3. Run (web + background worker)
uv run uvicorn alphamarket.main:app --reload     # -> http://127.0.0.1:8000
uv run python -m alphamarket.worker              # polls every 5 min, nightly reconcile + stats

# 4. Checks
uv run pytest
uv run ruff check .
uv run pre-commit install     # once, to enable git hooks

# 5. Kalshi demo smoke test (after creating a demo API key, see script header)
uv run python scripts/kalshi_smoke.py
```
