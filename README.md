# AlphaMarket (working title)

Verified track records for prediction-market traders. Connect a read-only
Kalshi API key, get a platform-computed, provably real performance page.
No screenshots, no self-reported numbers, no custody of anything.

Status: M0 skeleton. See `BUILD_PLAN.md` for architecture and milestones.

## Quickstart (WSL2 recommended on Windows)

```bash
# 1. Install uv:  curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --all-groups

# 2. Postgres
docker compose up -d db
cp .env.example .env          # then edit values
uv run alembic upgrade head

# 3. Run
uv run uvicorn alphamarket.main:app --reload
# -> http://127.0.0.1:8000/docs

# 4. Checks
uv run pytest
uv run ruff check .
uv run pre-commit install     # once, to enable git hooks

# 5. Kalshi demo smoke test (after creating a demo API key, see script header)
uv run python scripts/kalshi_smoke.py
```
