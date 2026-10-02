# Worker: jobs, budgets, backfills

How the background worker schedules and runs work (platform_plan 2.5,
implementation_plan P2). Code: `src/alphamarket/jobs.py` (queue),
`src/alphamarket/venue_limits.py` (outbound limits), `src/alphamarket/worker.py`
(handlers and ticks). Migration `0010_jobs_rate_limits`.

```
uv run python -m alphamarket.worker
```

## Shape

Each worker process runs two things:

1. **A ticker** (APScheduler). It only *enqueues* jobs; it never calls a venue.
2. **A claim loop** (`jobs.JobRunner`). It claims due jobs from the `jobs`
   table with `SELECT ... FOR UPDATE SKIP LOCKED`, runs each through the
   handler registered for its kind, and records the outcome.

Postgres is the only queue (decision E3: no Redis). Enqueueing is idempotent
(dedupe keys, below), so every process can run the ticker.

| Tick | Cadence | Enqueues |
|---|---|---|
| `tick_syncs` | `WORKER_SYNC_TICK_SECONDS` (30 s) | `sync` for each account whose `next_sync_at` has come; one global `mirror` sweep; a `backfill` for paged venues not yet backfilled |
| `tick_prices` | `PRICE_REFRESH_INTERVAL_SECONDS` (300 s) | one global `prices` |
| `tick_nightly` | 04:15 UTC | `scope_recheck`, `reconcile` per reconcilable account, `prices`, one `prices_daily` per pricing venue in use, one global `stats`, one `housekeeping` (which prunes finished jobs: done > 7 days, dead > 30 days) |
| `tick_oauth` | 15 min | `oauth_refresh` for OAuth accounts whose token expires within twice the refresh margin |

## Job kinds

| Kind | Scope | Handler does | Registered by |
|---|---|---|---|
| `sync` | account | Binary venues: `ingest.sync_account`. Paged venues: every page stream from its resume cursor. Then queues `signals` | worker.py |
| `backfill` | account | Every page stream from the start of history; resumable (below). Then queues `signals` (post-anchor fills it imported become signals; stale ones are not copied) | worker.py |
| `reconcile` | account | `reconcile.reconcile_account` (skipped for fills-only venues); asset venues also run the broker equity check with fresh quotes | worker.py |
| `prices` | global | Binary: public quotes for every possibly-open ticker, per venue. Asset: `prices.refresh_asset_marks` (quotes and funding rates for instruments in open paper books) | worker.py |
| `prices_daily` | venue | `prices.run_daily`: missing daily closes, perp margin tiers, corporate actions for held equities; CoinGecko fallback | worker.py (wave 2) |
| `stats` | global | Strategy stats (asset venues: `analytics_v2`), incubation promotion, allocation valuations. Waits (up to 1 h) while any `reconcile` or `prices_daily` is still active | worker.py |
| `housekeeping` | global | Prunes rate-limit counters, dead sessions and tokens, old asset quotes, finished jobs; abandons stale pending connects | worker.py (wave 2) |
| `signals` | account or global | Creator fills to signals; then queues `mirror` for that strategy | worker.py |
| `mirror` | strategy or global | Paper copying: signals and settlements to allocations | worker.py |
| `oauth_refresh` | account | Refresh an OAuth access token (`oauth_jobs.run`) | worker.py (wave 2) |
| `key_rotate` | global | Re-encrypt credentials under a new data key | P3 |
| `scope_recheck` | account | Re-prove a credential is read-only | P3 |

Another module adds a handler with
`jobs.register_handler(JobKind.X, fn)` (or as a decorator). `fn(ctx)` gets a
`JobContext`: its own session `ctx.db`, the job, the account, the throttled
connector `ctx.connector()`, and `ctx.heartbeat()`. Returning normally
completes the job. A worker only claims the kinds it has handlers for, so a
kind whose module is not deployed yet simply waits.

## Priorities

Lower runs first; within a priority, the older `run_after` first.

| Priority | Jobs |
|---|---|
| 5 | `oauth_refresh` (an expired token would fail every sync of that account) |
| 10 | `sync` for creators with followers or active paper allocations; `signals`, `mirror` (copy lag) |
| 20 | every other `sync` |
| 30 | `reconcile`, `stats`, `prices`, `prices_daily`, `housekeeping`, `scope_recheck`, `key_rotate` |
| 40 | `backfill` |

Re-enqueueing an already-pending job with a more urgent priority raises its
priority; it never pulls a deferred `run_after` forward.

## Dedupe keys

A job's `dedupe_key` defaults to `<kind>:<account id>` (or `<kind>` for
global jobs). A partial unique index allows one *pending or running* job per
key, and enqueue is `INSERT ... ON CONFLICT DO NOTHING`, so two ticking
processes cannot queue the same account's sync twice. Nightly jobs use dated
keys (`reconcile:<id>:<date>`, `stats:<date>`) with `once=True`, so they run
once per night however many processes tick.

## Sync scheduling and jitter

Each account syncs every `INGEST_INTERVAL_SECONDS` at a fixed phase inside
the interval: `offset = sha256("koine-sync:<id>") mod interval`, and
`next_sync_at` is the next instant `k x interval + offset` after now. The
phase is deterministic (the same on every process and restart) and uniform
across accounts, so a 5-minute tick never sends every account to a venue in
the same second. `next_sync_at` lives in `account_schedules`.

## Failures, retries, dead letters

- A failed run counts one attempt and retries after
  `WORKER_BACKOFF_BASE_SECONDS x 2^(attempts-1)`, capped at
  `WORKER_BACKOFF_CAP_SECONDS` (30 s, 60 s, 120 s, 240 s, ... 1 h).
- After `WORKER_MAX_ATTEMPTS` (5) the job is **dead**: the account goes to
  `error` with a user-facing reason ("Syncing your venue history failed 5 times
  in a row, so Koine paused it. Last error: ... reconnect it if ..."), and its
  next sync is pushed out by `WORKER_DEAD_LETTER_COOLOFF_SECONDS` (1 h).
  The next scheduled sync starts a fresh job.
- A claim holds a lease of `WORKER_LEASE_SECONDS` (300 s). The lease is
  renewed when each job of a claimed batch starts, and page streams renew it
  with every page's commit (the page is only committed if the lease is still
  held). A lease that expires (the worker died) is
  reaped by the next claim round as one failed attempt. Every state change is
  conditional on the worker still holding the lease, so a slow worker that
  lost its job cannot overwrite the new owner's progress.
- A job for a disconnected account is completed without running.

**Deferrals are not failures.** `BudgetExhausted`, `RateLimited` (and an
httpx 429/418 from connectors written before contract v2), an open circuit,
and a handler's own `jobs.Defer` put the job back to pending at the
retry-after time *without* counting an attempt. A budget can delay a job; it
can never dead-letter it. The global `prices` job is the exception: it has no
cursor, so a venue that runs out of budget or answers 429 keeps the quotes
already fetched and waits for the next `tick_prices` rather than re-running
early and re-spending the shared IP bucket on the same tickers.

**Mirroring is serialised per allocation.** The global `mirror` sweep and a
strategy's `mirror` job can run at once on two workers; each pass locks the
allocation row (`FOR UPDATE SKIP LOCKED`) so a signal is never mirrored twice
into the append-only `mirror_fills`.

## Outbound rate limits

Each venue profile lists `rate_budgets` (`connectors.base.RateBudget`). The
limiter keeps one token bucket per budget:

- `RateScope.CREDENTIAL`: per (venue, credential). The credential id is a
  hash of the key id; the raw id is never stored in `rate_buckets`.
- `RateScope.IP`: per (venue, `EGRESS_IP`). Every account polled from the
  same public IP shares it, and so does public market data.

Connectors call `throttle(endpoint)` before every request; the limiter takes
the endpoint's cost from every bucket or raises `BudgetExhausted` with the
exact wait (and refunds any bucket it already charged). `throttle_after()`
charges response-dependent cost (Hyperliquid: +1 per 20 items) and syncs down
to venue-reported usage (Binance `X-MBX-USED-WEIGHT-1M`).

Bucket arithmetic is integer: milli-tokens and epoch milliseconds.

```
gain_ms  = elapsed_ms x refill_per_minute // 60        (// 120 while penalised)
level    = min(capacity, level + gain)
wait_ms  = ceil((cost - level) x 60 / refill_per_minute)   (x 120 while penalised)
```

Worked example, Kraken Starter (capacity 15, 20/min, `TradesHistory` costs 2):
seven calls spend 14 of 15 tokens; the eighth needs 2, has 1, and waits
`1000 x 60 / 20 = 3000 ms`. Hyperliquid (IP, 1200/min): a `userFills` call
costs 20, a 2000-fill response adds `2000 / 20 = 100`, and the bucket refills
20 tokens per second.

**Adaptive.** On a 429/418 the limiter halves that caller's buckets' refill
for 10 minutes and blocks them until the venue's `Retry-After`, then the job
defers to the same time.

**Circuit breaker.** 5 consecutive 5xx/timeouts/connection errors from a
venue open its circuit for 2 minutes. While open, that venue's jobs are
deferred to the reopen time (spread by a few seconds per job) instead of
running or failing, so a venue outage never dead-letters every account. After
2 minutes the next job tries (half-open): one more failure reopens it, one
success closes it and resets the count.

Backends: `PostgresBuckets` (default; `rate_buckets` and `venue_circuits`
tables, refill+take in one `UPDATE ... RETURNING`, shared by all processes)
and `MemoryBuckets` (`RATE_LIMITER_BACKEND=memory`: one process only, since
two processes would each spend the venue's whole budget).

## Resumable backfills (page streams)

Non-binary venues ingest through **page runners**:
`jobs.register_page_runner(stream, fn)`, where
`fn(db, account, connector, cursor) -> (next_cursor, resume_cursor)` fetches
and ingests one connector page (one venue request; see `connectors.base.Page`)
without committing. Streams run in registration order (e.g. `asset_fills`,
then `events`).

A `backfill` or paged `sync` job keeps `{"streams": [remaining], "cursor": ...}`
in its payload. After each page the runner commits **the page's ledger rows and
the advanced cursor in one transaction**, so any interruption (budget, 429,
crash, deploy) loses at most the page in flight, and the next run resumes at
the committed cursor. After `WORKER_BACKFILL_PAGES_PER_RUN` (50) pages a job
yields its turn and re-queues itself. When a stream finishes, its
`resume_cursor` is saved in `account_schedules.resume_cursors[stream]` and the
next incremental `sync` starts there (Kraken: last seen refid; Binance: symbol
set + per-symbol `fromId`). A backfill starts every stream from `None` (full
history) and sets `account_schedules.backfill_done_at` when it finishes.

## Running more worker processes

- Start more processes with the same command. SKIP LOCKED claims, lease-guarded
  updates and dedupe keys make any number safe; the Postgres exit test runs
  three concurrent claim loops over one table and checks that each job ran
  once.
- Every process behind the **same public IP** must use the same `EGRESS_IP`
  (default `"default"`), so they share the venue's IP bucket. A process with a
  different egress IP (another region, a dedicated NAT) sets its real IP.
- Keep `RATE_LIMITER_BACKEND=postgres` whenever more than one process runs.
- Worker hosts need NTP: bucket refill uses each host's clock (skew is capped:
  a clock behind the stored time refills nothing).
- `WORKER_CLAIM_SIZE` (5) jobs are claimed per round trip and run one after
  another; each job's lease is renewed as it starts, so keep the slowest
  single non-paged job (stats, a global mirror) under `WORKER_LEASE_SECONDS`.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `EGRESS_IP` | `default` | IP-bucket identity of this process |
| `WORKER_CLAIM_SIZE` | 5 | jobs per claim |
| `WORKER_LEASE_SECONDS` | 300 | lease before a job is reclaimable |
| `WORKER_MAX_ATTEMPTS` | 5 | failures before dead-lettering |
| `WORKER_POLL_SECONDS` | 2.0 | idle sleep when nothing is due |
| `WORKER_BACKOFF_BASE_SECONDS` / `_CAP_SECONDS` | 30 / 3600 | retry backoff |
| `WORKER_DEAD_LETTER_COOLOFF_SECONDS` | 3600 | next sync after a dead letter |
| `WORKER_BACKFILL_PAGES_PER_RUN` | 50 | pages before a backfill yields |
| `WORKER_SYNC_TICK_SECONDS` | 30 | how often due syncs are enqueued |
| `RATE_LIMITER_BACKEND` | `postgres` | `postgres` or `memory` |
| `INGEST_INTERVAL_SECONDS` | 300 | sync interval per account |

<!-- wave2:market-data -->
## Market data jobs (wave 2)

| Kind | Scope | Handler does | Code |
|---|---|---|---|
| `prices_daily` | global, payload `{"venue": <pricing venue>}` | Daily closes for every asset held by an account priced by that venue (anchors, creator legs, follower paper legs), perpetual margin tiers, corporate actions for stocks held by followers | `prices.run_daily` |

**Nightly tick.** `tick_nightly` enqueues one `prices_daily` per pricing venue
in use (`dedupe_key = prices_daily:<venue>:<day>`, once per day). The `stats`
barrier waits (up to its 1 h cap) while any `prices_daily` job is active, so
tonight's closes are in before tonight's valuations.

**Cursor.** The missing days are the cursor. A budget stop (Koine's own
outbound bucket spent, the circuit open, or a 429 from the source) commits
what was fetched and raises `Defer(retry_after)`: the job is re-queued without
counting an attempt and the rerun asks only for what is still missing. Rerun
cost is zero when nothing is missing: what has been asked of a source is kept
in `market_data_coverage` (closes by instrument, corporate actions by symbol
and day, assets no source prices), so a second run the same day makes no HTTP
request. The last `LATE_CLOSE_GRACE_DAYS` (4) days without a close are re-asked
by the next night's tail request (the same single request, so a late bar is not
lost); older gaps such as weekends are not asked for again. An asset no source
prices is re-checked once per UTC day.

**Other failures.** An asset whose source call fails (not a budget stop) is
logged and skipped so the others still get their prices; after everything else
ran, the job raises `PricesDailyError` and the normal retry/backoff applies. The
retry is cheap for the same reason as above.

**Sources and budgets.** Closes come from the pricing venue's public market data
(Kraken OHLC, Hyperliquid candles, Alpaca bars) through the venue's own bucket
(`limiter.throttle(venue, None)`). CoinGecko is the fallback, throttled by
`registry.DATA_SOURCE_BUDGETS["coingecko"]` (30 calls a minute per egress IP,
shared by all workers behind it) with `COINGECKO_API_KEY` as the demo key. It
has no venue profile and cannot be connected.

**Asset marks inside `prices`.** After the binary quotes, every `prices` job calls
`prices.refresh_asset_marks`: for each instrument an active follower paper book
holds (net non-zero in its ledger) it stores the venue's top of book in
`asset_quotes` and, for perpetuals, the funding rates since the newest stored one
(the first fetch starts at the earliest open fill). A source that is out of
budget, rate-limited or circuit-open is skipped for the rest of that pass; the
next tick retries. `asset_quotes` rows older than `QUOTE_RETENTION_DAYS` are
pruned by housekeeping.

| Env var | Default | Meaning |
|---|---|---|
| `COINGECKO_API_KEY` | empty | demo key, sent as `x-cg-demo-api-key` |
| `COINGECKO_BASE_URL` | `https://api.coingecko.com/api/v3` | API base |
| `QUOTE_MAX_AGE_SECONDS` | 900 | a quote older than this is not a mark |

<!-- wave2:account-security -->
## OAuth refresh and nightly housekeeping (wave2)

**`oauth_refresh` is live** (`oauth_jobs.py`).

- `tick_oauth` runs every 15 minutes. It queues one `oauth_refresh` job
  (priority 5, dedupe key `oauth_refresh:<account>`) for each **active**
  OAuth account whose live credential has `venue_credentials.expires_at <=
  now + 2 x REFRESH_MARGIN` (10 minutes). The expiry column is written at
  connect and at every refresh, so the tick decrypts nothing. Tokens with no
  expiry (Alpaca) are never queued.
- The job re-reads the credential first. If a sync already refreshed it
  (the expiry is past the horizon again), the job stops without a call, so
  each due token is refreshed once. Otherwise it decrypts the credential
  (audited), calls the token endpoint, stores the new record with its
  `expires_at` (the old credential row is erased), and audits
  `credential.refresh` with outcome `refreshed`.
- **The venue refuses** (`invalid_grant`, `invalid_client`, or a refresh
  that grants a write scope): the account goes to `error` with
  "Reconnect <venue>: ...", the refusal is audited (outcome `refused`, the
  error code only), and the owner gets one `oauth_reconnect` alert. The
  alert is sent only on that transition. The job completes; the tick skips
  `error` accounts.
- **A 5xx, a 429, a network error or a "try later" code**
  (`temporarily_unavailable`, `server_error`, `slow_down`) is re-raised, so
  the job backs off and retries like any other failure, and the account is
  unchanged.
- **No refresh token in the record.** Nothing can refresh it, so once the
  token has expired the account is marked for reconnect as above (which
  also stops the tick queueing it again).
- **Lazy path.** `ingest.connector_for` calls `oauth_jobs.refresh_if_due`
  before it builds an OAuth connector: the 5-minute margin plus jitter
  (`oauth.ensure_fresh`). On a refresh it stores and commits the new record.
  A refusal marks the account as above and raises `ReconnectRequired`, so the
  sync fails with the reconnect message instead of a 401. A transient error
  while the current token has not yet expired is logged and the sync goes
  ahead with that token.

**`housekeeping`** is queued nightly by `tick_nightly`, with dedupe key
`housekeeping:<day>` and `once=True`. `housekeeping.run` deletes, committing
each category on its own:

| Category | Deleted when | Setting (default) |
|---|---|---|
| `rate_limit_counters` | the window has ended | none |
| `sessions` | revoked, or expired (idle 24 h after last seen, 30 d after creation, 10 min for a password-only session), more than N days ago; each user's newest full session is always kept, so the new-sign-in alert still knows the user | `SESSION_RETENTION_DAYS` (30) |
| `auth_tokens` | used, or expired, more than N days ago | `TOKEN_RETENTION_DAYS` (7) |
| `asset_quotes` | `as_of` older than N days | `QUOTE_RETENTION_DAYS` (30) |
| pending accounts | `pending.abandon_stale` (unfinished SnapTrade connects) | `PENDING_CONNECT_TTL_HOURS` (24) |
| `jobs` | done more than 7 d ago, dead more than 30 d ago (`jobs.prune_finished`, which moved here from `tick_nightly`) | none |

It returns the count per category, which is logged. It never touches an
append-only table.
