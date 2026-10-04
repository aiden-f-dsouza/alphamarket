# Seeded demo

`scripts/seed_demo.py` fills a dedicated database with a demo that the real
engine computed: synthetic venue data goes through the production pipeline,
so every number on the demo pages (returns, drawdowns, Sharpe, holdings,
exposure, reconciliation, signals, paper copies, the portfolio) comes from
the same code that will serve real users. It is for showing Koine to people
(the accelerator, investors), locally or on a staging app. It is never for a
database real users touch.

## What is synthetic, what is real

Synthetic: the venues. A seeded random number generator draws price paths
(BTC, ETH, SOL, US equities and ETFs, Kalshi markets) and each creator's
trading on them, exactly as the venue would report it: fills and events with
their ledger legs, balance vectors, quotes, daily closes, hourly funding
rates, margin tiers, corporate actions. Fake connectors and market-data
sources serve that data from behind `registry.build_connector` /
`registry.build_market_data`, inside the seed process only. Outbound HTTP is
refused while seeding (invariant 6), so no venue is ever called.

Real: everything after the connector. Each creator account gets a
vault-sealed (dummy) credential and is read through `ingest.connector_for`;
the real `JobRunner` runs the real handlers (backfill and sync page streams,
signals v2 with quote capture, the paper engine with funding, splits,
dividends and drawdown stops, `prices_daily`, reconcile, stats v1 and v2,
scope re-checks, housekeeping), and investors tail strategies through
`tails.create_allocation`. The clock is moved (time-machine) from each
creator's connect date to today, so syncs, copy lag, nightly jobs and
portfolio snapshots happen at the times they would have in production.

Same seed + same day = same data (`--seed N` draws another world; the
default was picked from a sweep for a realistic spread: most records up,
one deep loser, one flat). The end is the current hour, so a fresh seed
always ends "now". With the default seed on 2026-10-04 the marketplace read:
Hyperliquid perps +88% (max drawdown 15%), Kalshi weather +52% (19%), Kraken
+24% (10%), Alpaca +16% (4%), Binance.US +3%, Kalshi macro +3%, IBKR -0.6%,
Coinbase alt rotation -52%; every record reconciled, and the investor's
paper portfolio +8.6% with the alt-rotation tail stopped by its 10% limit.
Equity trades fall on weekdays, so other days give slightly different
numbers.

## What the seed creates

All users are `is_demo = true` (invariant 7, see below).

| Creator | Venue | Strategy | History | Notes |
| --- | --- | --- | --- | --- |
| northwind | Kraken (spot) | BTC/ETH swing | 9 months | a mid-history deposit (flow-adjusted TWR ignores it) |
| tidewater | Hyperliquid (perps) | Perp momentum: BTC, ETH, SOL | 7 months | longs and shorts, hourly funding, margin tiers |
| lantern | Coinbase (spot) | High-beta alt rotation | 5 months | the losing record: losers stay listed |
| quarry | Binance.US (spot) | BTC accumulation bands | 45 days | incubating |
| meridian | Alpaca (equities) | US mega-cap momentum | 8 months | an AVGO 4-for-1 split and MSFT/AVGO dividends |
| harbor | IBKR Flex (equities) | Dividend growth core | 10 months | daily-resolution venue ("daily verified") |
| isobar | Kalshi | NYC & Chicago daily highs | 8 months | prediction markets, settled daily |
| fedwatch | Kalshi | CPI & Fed path | 75 days | incubating |

Investors (password-only logins, no TOTP):

- `investor@demo.koine`: paper tails of northwind, meridian, isobar,
  tidewater and lantern (with a tight 10% drawdown stop), started 50-60
  days ago; follows six strategies.
- `analyst@demo.koine`: tails of harbor and northwind; follows four.

Every creator can log in too (`<handle>@demo.koine`, e.g.
`northwind@demo.koine`) to show the creator dashboard. The password for every
login is printed at the end of the run (default `koine-demo-2026`, change it
with `--password`). Do not press "Sync now" or "Reconcile" on a creator
dashboard: the stored keys are dummies and the live connector would fail.

## Running it locally

```bash
# 1. Postgres (the dev server) and a dedicated database
docker compose up -d db
docker compose exec db psql -U alphamarket -c 'CREATE DATABASE alphamarket_demo'

# 2. Seed it (migrates to head first; --reset drops and recreates the schema)
DATABASE_URL=postgresql+psycopg://alphamarket:alphamarket@localhost:5432/alphamarket_demo \
DEMO_MODE=true \
uv run python scripts/seed_demo.py --reset

# 3. Serve it
DATABASE_URL=postgresql+psycopg://alphamarket:alphamarket@localhost:5432/alphamarket_demo \
DEMO_MODE=true \
uv run uvicorn alphamarket.main:app --reload
```

`VAULT_KEY` must be set (it is in `.env`): the dummy credentials are sealed
like real ones. `uv sync --all-groups` installs `time-machine`, which the
seed needs (dev group). The full seed takes a few minutes; `--scale small`
(about two weeks of history) takes under a minute and is what the test uses.

The demo is a snapshot ending at the hour it was seeded. Re-run with
`--reset` shortly before a demo so "last reconciled" and the latest signals
are fresh. Do NOT run the worker (`python -m alphamarket.worker`) against the
demo database: it would call the real venues with the dummy keys.

## On a staging app (Fly)

Give the staging app its own Postgres database whose name contains `demo`,
set `DEMO_MODE=true` on it, and run the seed from your machine through a
proxy (the production image has no dev dependencies):

```bash
fly proxy 15432:5432 -a <staging-db-app>
DATABASE_URL=postgresql+psycopg://<user>:<password>@localhost:15432/koine_demo \
DEMO_MODE=true VAULT_KEY=<the staging app's key> \
uv run python scripts/seed_demo.py --reset
```

Scale the staging app's `worker` process to zero.

## Safety (invariant 7)

- The script refuses unless `DEMO_MODE` is on, the database name contains
  `demo`, and the database holds no non-demo user. `--reset` drops the whole
  `public` schema (append-only triggers make row deletes impossible, by
  design), which is why the database must be dedicated.
- Rows owned by a demo user are public only with `DEMO_MODE` on
  (`public.publicly_visible`: marketplace, strategy pages and JSON, feed,
  landing), and every page showing them carries the "Sample data" banner.
- With seeded rows present, the static `demo.py` padding (marketplace,
  landing, transparency) steps aside so nothing is listed twice; an empty
  database in `DEMO_MODE` still shows the static sample pages.
- `scripts/preflight.py` fails while any demo user exists in the database.

## A five-minute click-through

1. **Landing** (`/`): the featured card and top three are seeded records,
   under the "Sample data" banner.
2. **Marketplace** (`/marketplace?incubating=1`): switch the tabs
   (Prediction markets / Stocks / Crypto) and the venue chips. Point out the
   losing alt-rotation record staying listed, and the incubating badges.
3. **A crypto perp strategy** (`/marketplace?asset=crypto`, then "Perp
   momentum"): flow-adjusted growth chart and drawdown, the holdings and
   exposure panel with perp entry prices, "reconciled last night", recent
   signals with copy lag.
4. **An equity strategy** ("US mega-cap momentum"): holdings in shares after
   the AVGO split, dividends in the return; then "Dividend growth core" for
   the daily-verified IBKR tier.
5. **Portfolio**: log in as `investor@demo.koine`, open `/portfolio`: five
   paper tails across crypto, perps, equities and Kalshi, the combined curve,
   open positions marked to market, copy lag, and the tail stopped by its
   drawdown limit (if the losing record hit it in this seed).
6. **Feed** (`/feed`, still logged in): the newest fills and settlements of
   every strategy the investor follows or tails, with each creator's verified
   return and drawdown beside the name and the copy lag of crypto/equity
   fills (also as JSON at `/api/feed`). Rooms and chat on that page remain the
   static community mock-up; a creator's handle links to their strategy.
7. **Verification** (`/verification`): how each number above is computed.
