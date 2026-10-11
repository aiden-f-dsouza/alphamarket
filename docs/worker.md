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
| `housekeeping` | global | Prunes rate-limit counters, dead sessions and tokens, old asset quotes, finished jobs; abandons stale pending connects; verifies a random sample of 50 paper book checkpoints against a full replay and drops any that disagree (the next `mirror` pass rebuilds them) | worker.py (wave 2) |
| `signals` | account or global | Creator fills to signals; then queues `mirror` for that strategy | worker.py |
| `mirror` | strategy or global | Paper copying: signals and settlements to allocations | worker.py |
| `oauth_refresh` | account | Refresh an OAuth access token (`oauth_jobs.run`) | worker.py (wave 2) |
| `key_rotate` | global | Re-encrypt credentials under a new data key | P3 |
| `scope_recheck` | account | Re-prove a credential is read-only | P3 |
| `aggregator_delete_user` | account (released) | Retry a failed SnapTrade `deleteUser` for an account that is already disconnected or suspended (below) | worker.py (wave 2) |
| `lease` | none (key names the holding) | Not work: a coordination lease of the stream process ("Streaming", below). No handler, so no worker claims it; `venue_account_id` stays NULL so it never blocks the account's jobs | stream.py (P7) |

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
| 5 | `oauth_refresh` (an expired token would fail every sync of that account); `aggregator_delete_user` (one request, and until it lands a brokerage link, a trade-capable one after a scope suspension, stays live at SnapTrade) |
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

`jobs.enqueue_fresh` (P7) is for callers that just committed new input the
job must see. Plain `enqueue` also coalesces into a RUNNING job, which may
have read its input before the caller's commit; `enqueue_fresh` coalesces
only into a PENDING one, and queues a follow-up under `<key>:next` when the
default key is running (one running job per account means the follow-up
starts after it). The stream process and `sync`'s own `signals` hand-off use
it.

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
- A job for a disconnected, suspended or pending account is completed without
  running, except the kinds in `jobs.RUNS_ON_INACTIVE_ACCOUNT`
  (`aggregator_delete_user`: the account is inactive because the work exists).

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

**Web requests spend the same buckets, but never wait for them.** The SnapTrade
connect pages (account picker, portal link, `registerUser` on start) and the
`deleteUser` of a disconnect call `venue_limits.call_venue`: the same buckets
(`EGRESS_IP`, shared with the workers through Postgres), the same 429
penalty and the same circuit breaker. A bucket that needs at most 2 s to
refill (`WEB_MAX_WAIT_SECONDS`) is slept on; a longer wait, an open circuit or
a 429 raises `VenueBusy` and nothing is sent. Pages answer 503 with
`Retry-After` and "SnapTrade is busy, try again in a minute"; a disconnect
does not fail but hands its `deleteUser` to the job below.

So do the other request-path venue calls: the connect check, read-only proof
and anchor of `venues.connect_account` (every venue; the SnapTrade finish POST,
the JSON and form connects, the OAuth callback), `rotate.rotate_credential`
and the ownership claim (`venues.claim_account`). They run as one unit inside
`web_venue_call`, which turns `VenueBusy` into a 503 with `Retry-After`
before anything is written, so a PENDING account stays PENDING and the same
form can be submitted again. A 429 from the venue during the check or the
proof is passed up as busy (never as a failed login or an "undetermined"
proof). Not yet routed: the dashboard's inline sync, reconcile and claim
buttons (`pages.account_action_form`) and the manual `POST /venues/{id}/sync`
and `/reconcile`, which call the venue with no limiter.

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
holds (net non-zero in its ledger), or a book stopped within
`prices.STOPPED_QUOTE_WINDOW` (30 days) that is not flat yet (its stop close
waits for a fresh quote), it stores the venue's top of book in
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

**`aggregator_delete_user`** (wave 2, D20). Every SnapTrade connection is its
own SnapTrade user; disconnecting (user, `scopecheck.suspend` for a link that
turned writable, `pending.abandon_stale`) deletes it, or Koine keeps paying
for it and SnapTrade keeps the brokerage link. `credentials.delete_aggregator_user`
makes one attempt through the outbound limiter; any failure, including "busy",
queues this job in the same transaction as the disconnect, so the retry cannot
be lost.

- **Dedupe key** `aggregator_delete_user:<account id>`: one active job per
  account however many paths ask. **Payload** `{"cause": ...}` only: the
  handler needs just the SnapTrade user id, which is `venue_accounts.key_id`
  (an identifier Koine chose, not a secret). The user secret is never in a
  payload, an error or an audit row.
- The job keeps the account's id, so the runner still applies SnapTrade's
  circuit breaker and 429 backoff and serialises it per account;
  `RUNS_ON_INACTIVE_ACCOUNT` stops the runner completing it unrun for the
  inactive account. The handler refuses to act on an account that is not
  `disconnected` or `suspended`.
- **Done** when SnapTrade accepts the delete, or answers 404/410 for the user
  (already deleted: idempotent; `[VERIFY]` the exact status against a live
  account). **Transient** (429, 5xx, timeouts): backs off, an outage opens the
  circuit and defers without spending attempts. **Permanent** (other 4xx, e.g.
  a wrong consumer key): retries and dead-letters with the rest.
- `max_attempts` is 12 (`aggregator_jobs.DELETE_MAX_ATTEMPTS`), about five hours
  of backoff, because the failure costs money every month. On the dead letter
  `aggregator_jobs` logs at error and audits `aggregator.user_delete` with
  outcome `gave_up`; delete the user by hand in SnapTrade's dashboard.
- Audit: `aggregator.user_delete` on the account, outcome `deleted`,
  `already_gone`, `retry_queued` or `gave_up` (shown as "Brokerage link
  removed" on the user's security page).

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

## Streaming (P7, optional third process)

```
uv run python -m alphamarket.stream
```

Polling every `INGEST_INTERVAL_SECONDS` means a paper copy lags its creator
by minutes. The stream process (`src/alphamarket/stream.py`) holds a private
websocket per creator **with at least one active paper allocation** on a
venue that can stream, so the fill is imported within seconds and the copy
lag (`signals.observed_at - filled_at`, where `observed_at` is the import
time) drops with it. The poller stays the source of truth: a stream only
shortens lag, and a dropped or late stream changes nothing else. Not running
the process at all is a supported configuration.

| Venue | Channel | Mode | Status |
|---|---|---|---|
| Hyperliquid | public WS `userFills` (address only, no credential) | `fills`: the frames are the REST fill objects, ingested directly | on |
| Kraken | WS v2 private `executions` (token from `GetWebSocketsToken`) | `nudge`: a trade queues the account's `sync` | only for keys that also carry the optional "WebSocket interface" permission; others are polled (docs/venues/kraken.md) |
| Coinbase | Advanced Trade `user` (+ `heartbeats`) | `nudge`: an order whose filled quantity rose queues a `sync` | built; [VERIFY] on a real View key |
| Alpaca | `trade_updates` | — | stub: [VERIFY] whether it works on a no-trading-scope OAuth grant (`AlpacaConnector.stream_source`) |

**Modes.** `VenueProfile.stream_mode` says how a venue's frames reach the
ledger. `fills`: the frame carries the same payload the poller normalizes
(same venue ids, same legs), so it goes through `ingest.ingest_asset_fills`
and a fill seen by both is stored once. A fill already stored is skipped
without the exact-duplicate comparison, so only the poller can ever
quarantine an account over a differing copy. `nudge`: the frame only says
"this account traded" (Kraken's legs need its Ledgers rows, Coinbase reports
orders, not fills), so the account's `sync` is queued at once at copy-lag
priority and REST ingests the fill. After every reconnect a `nudge` venue
queues one catch-up sync for the gap; Hyperliquid's subscription snapshot
covers its own gap.

**What happens after a fill.** New streamed fills are committed, then the
account's `signals` job is *enqueued* (`jobs.enqueue_fresh`, never run in
the stream process): a burst coalesces into one pending job, and a fill that
lands while a `signals` job is running gets a follow-up. The worker's claim
loop picks it up within `WORKER_POLL_SECONDS` at priority 10, and `signals`
queues `mirror` as usual. A streamed fill that arrives while the poller is
syncing the same account becomes a follow-up `sync` instead of racing the
poller's insert.

**Watermarks.** The stream never writes `account_schedules.resume_cursors`.
`fills` accounts are streamed only once the poller has stored an asset-fills
resume cursor and the backfill is done, because without a cursor the poller
would resume from the newest stored fill, which a streamed fill could move
past fills the poller has not fetched yet.

**Leases.** Each streamed account is a lease row in `jobs` (kind `lease`,
key `stream:account:<id>`), taken with `INSERT ... ON CONFLICT DO NOTHING`
on the active-dedupe index and renewed every `STREAM_REFRESH_SECONDS`. Any
number of stream processes can run: an account is subscribed by at most one,
and a dead process's accounts free up after `STREAM_LEASE_SECONDS` (the
worker's reaper may turn the expired row back to `pending`; any stream
process takes it over). A clean shutdown releases its leases at once. No
migration: the lease reuses the job table's index and lease columns.

**Hyperliquid's 10-user cap.** Hyperliquid allows at most **10 unique users
per IP** across websocket user subscriptions (docs "Rate limits and user
limits"). `VenueProfile.stream_users_per_ip = 10`, enforced with one slot
lease per IP (`stream:slot:hyperliquid:<EGRESS_IP>:<0..9>`) on top of the
account lease, so every stream process behind one `EGRESS_IP` shares the 10.
Creators are taken most-followed first (active allocations, then follows);
when a better-ranked creator is left without a slot, the process holding the
worst-ranked one gives its slot up at the next refresh and the better one
takes it. Every other Hyperliquid creator is polled as before. More than 10
needs another egress IP (a second region with its own `EGRESS_IP`) or a data
provider; see implementation_plan.md 2.3.

**Reconnects and limits.** A dropped connection reconnects with full-jitter
exponential backoff (`STREAM_BACKOFF_BASE_SECONDS` x 2^n, at most
`STREAM_BACKOFF_CAP_SECONDS`), reset after a session that received frames.
While a venue's circuit is open (venue_limits) the stream waits it out. REST
calls a stream makes (a Kraken websocket token, Hyperliquid metadata) spend
the same outbound buckets as the worker; a frame that cannot be normalized
right now (out of budget, an unknown coin) becomes a nudge, never a dropped
fill. A connection with no frame for the source's idle timeout is closed and
reopened; application pings go out every 30 s (Hyperliquid drops a silent
client after 60 s). A venue that refuses the account (Kraken key without the
WebSocket permission, an auth error) is left to the poller for
`STREAM_UNAVAILABLE_COOLOFF_SECONDS`.

**Secrets.** The credential is decrypted through `credentials.py` on every
(re)connect (audited, purpose `stream`), never stored by the stream. Frames
sent or received are never logged (a subscribe frame may hold a token), the
`websockets` logger is held at INFO, and every error string goes through
`redaction.redact`.

| Env var | Default | Meaning |
|---|---|---|
| `STREAM_REFRESH_SECONDS` | 30 | re-read who to stream, renew leases |
| `STREAM_LEASE_SECONDS` | 120 | a dead stream process's accounts free up after this |
| `STREAM_MAX_ACCOUNTS` | 200 | websockets one stream process holds at most |
| `STREAM_BACKOFF_BASE_SECONDS` / `_CAP_SECONDS` | 1 / 120 | reconnect backoff (full jitter) |
| `STREAM_UNAVAILABLE_COOLOFF_SECONDS` | 21600 | retry delay for an account the venue will not stream |
| `EGRESS_IP` | `default` | also names the Hyperliquid slot pool: set it per public IP |
