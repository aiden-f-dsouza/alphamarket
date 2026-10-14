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

The price paths are meant to look plausible to someone who knows the
markets, not to replay history: each name starts near where it traded in
late 2025 (BTC ~94.5k, ETH ~3.3k, SOL ~178, NVDA ~182, MSFT ~485, ...) with a
typical daily volatility (BTC 2.7%, ETH 3.7%, SOL 4.7%, large-cap equities
1.4-2.2%, staples ~1%). Daily shocks share a common factor per market, so
BTC, ETH and SOL move together (and so do the equities), and each log price
is pulled back toward a modest trend line, with a much stronger pull outside
~0.58x-1.6x of its start, so nine months never ends at an absurd level. The
AVGO 4-for-1 split is fictional; it is placed so the post-split price is
where AVGO actually trades (its dividend is split-adjusted too). Order sizes
are what a person would type (coins to two significant figures, shares in
round lots, Kalshi contracts in tens).

Kalshi markets get the same care: each event (a city's high on a day, a CPI
print, payrolls, a Fed decision) has one outcome drawn up front, so a strike
ladder always settles coherently, and the market prices every strike off one
forecast, so prices fall as strikes rise. Weather strikes follow the
seasonal normal high; CPI, payrolls and Fed markets settle on their release
days (second Wednesday, first Friday, the FOMC calendar). The creator has a
noisy private signal of the market's error and trades the side its own
(calibrated) probability favours, which is where its edge comes from.
Seeded strategies get clean slugs (`/s/perp-momentum-btc-eth-sol`); a real
connect appends the strategy id.

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
one deep loser, one or two roughly flat, nothing above about +60%, and only
the tight-limit tail stopped). The end is the current hour, so a fresh seed
always ends "now". With the default seed (127) on 2026-10-04 the marketplace
read: Hyperliquid perps +41.7% (max drawdown 25.0%), Kalshi weather +41.5%
(11.7%), Kraken +14.8% (12.1%), Alpaca +10.8% (7.7%), Binance.US +7.5%
(incubating), IBKR +1.4% (9.9%), Kalshi macro -2.5% (incubating), Coinbase
alt rotation -42.4% (50.3%); every record reconciled, and the investor's
paper portfolio stood at $11,412 on $10,000 with the alt-rotation tail
stopped by its 10% drawdown limit. Equity trades fall on weekdays, so other
days give slightly different numbers.

## What the seed creates

All users are `is_demo = true` (invariant 7, see below).

| Creator | Venue | Strategy | History | Notes |
| --- | --- | --- | --- | --- |
| northwind | Kraken (spot) | BTC/ETH swing | 9 months | a mid-history deposit (flow-adjusted TWR ignores it) |
| tidewater | Hyperliquid (perps) | Perp momentum: BTC, ETH, SOL | 7 months | longs and shorts, hourly funding, margin tiers |
| lantern | Coinbase (spot) | High-beta alt rotation | 5 months | the losing record: losers stay listed |
| quarry | Binance.US (spot) | BTC accumulation bands | 45 days | incubating |
| meridian | Alpaca (equities) | US mega-cap momentum | 8 months | an AVGO 4-for-1 split; MSFT, AVGO, AAPL and META dividends |
| harbor | IBKR Flex (equities) | Dividend growth core | 10 months | daily-resolution venue ("daily verified") |
| isobar | Kalshi | NYC & Chicago daily highs | 8 months | prediction markets, settled daily |
| fedwatch | Kalshi | CPI & Fed path | 75 days | incubating |
| koinelab | Kraken (spot) | Systematic BTC/ETH trend filter | 6 months | Koine-run (`is_house`): carries the "Run by Koine" badge, ranked like everyone else |

Investors (password-only logins, no TOTP):

- `investor@demo.koine`: paper tails of northwind, meridian, isobar,
  tidewater and lantern (with a tight 10% drawdown stop), started 50-60
  days ago; follows six strategies.
- `analyst@demo.koine`: tails of harbor and northwind; follows four.

Private watchlist (docs/copy_strategies_plan.md section 2): one synthetic
Hyperliquid address, `0xde30de30...de30` (`de30` repeated, never a real
person's), watched through the same shared-account path as a real watch. Its
venue account belongs to `system@demo.koine`, a seeded system user
(`is_system` and `is_demo`, unusable password hash `!`, no login); the account
is never claimed and its strategy has kind `watch`, is not public and has slug
`watch-{id}`. It is fed through the same pipeline as the Hyperliquid creator
(fills, hourly funding, stats, signals, paper engine). It appears on no
public surface and nobody follows it.

- `investor@demo.koine` watches it as "Funding-aware perp whale" and also has
  an active paper tail of it; open `/watch` to see it.
- `analyst@demo.koine` watches it as "BTC momentum book to study" (no tail).

Discover (`/watch/discover`): the seed stores synthetic Hyperliquid leaderboard
snapshots for every period (invented `c0de...` addresses, one of them the
watched address, so the investor sees it as "On your watchlist"). The page is
off unless `WATCH_DISCOVER_ENABLED=true` is set on the web process (step 3
below). Do not run the worker against the demo database: its refresh would
replace the invented rows with the real leaderboard.

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
DEMO_MODE=true WATCH_DISCOVER_ENABLED=true \
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
