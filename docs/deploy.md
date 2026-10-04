# Deploy (M4.4; target chosen in docs/implementation_plan.md section 2)

Target: **Fly.io + Fly Managed Postgres**, `web` and `worker` (plus the optional `stream`) process
GROUPS from the one Dockerfile image (`fly.toml`; the image runs as a
non-root user), **Cloudflare** in front for TLS/WAF/rate rules/CDN, a
**static egress IP** so users can IP-restrict venue keys, and (a later
phase, once vault v2 lands) **AWS KMS** wrapping the vault's data keys.
Anything else with a Dockerfile and a Postgres works the same way; see
implementation_plan.md 2.1-2.2 for the GCP/AWS comparison and when to
switch.

## Before the first deploy

1. **Cloudflare**: move the domain's DNS there (free plan to start).
   Proxy (orange-cloud) the app's hostname so Cloudflare's edge WAF, bot
   protection and rate rules sit in front of every request before it
   reaches Fly. This is `implementation_plan.md` decision E4 and is
   required for every non-demo deployment, not optional hardening.
2. **Static egress IP** (needed once any venue that lets users IP-restrict
   a key -- Kraken, Coinbase, Binance.US -- is connectable in prod;
   implementation_plan.md P4/2.3):
   ```
   fly ips allocate-egress -r iad
   ```
   One per region, shared by every machine in that region (up to 64
   machines each). Publish the resulting address(es) on `/security` before
   telling users they can restrict a key to it -- an unpublished IP is not
   a feature anyone can use. Allocate this before the first venue key is
   connected in prod, not after.
3. **AWS KMS** (a later phase, with vault v2 / P3 -- not needed for the
   current Fernet-only vault): one KMS key in `us-east-1`, IAM credentials
   scoped to `Encrypt`/`Decrypt`/`GenerateDataKey` on that key only. The
   vault wraps its Fernet data key with it (`KeyWrapper` protocol,
   implementation_plan.md P3 item 7); until then `VAULT_KEY` alone is the
   master key, as today.

## First deploy

```
fly launch --no-deploy              # uses fly.toml; both process groups, no Postgres yet

# Fly Managed Postgres (FMP) is the current managed offering; check
# `fly mpg --help` for the exact subcommands on your flyctl version --
# this replaced the older `fly postgres create` Nomad-based product.
fly mpg create --name koine-db --region iad
fly mpg attach koine-db --app alphamarket   # sets DATABASE_URL (postgres://...)

fly secrets set \
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  VAULT_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  SECURE_COOKIES=true KALSHI_ENV=demo PUBLIC_BASE_URL=https://<your-domain>

fly deploy
```

`DATABASE_URL` from Fly is `postgres://`; the app rewrites it to
`postgresql+psycopg://` at startup (config.py).

## The optional `stream` process (P7)

`fly.toml` declares a third process group, `stream`
(`python -m alphamarket.stream`): websockets for creators with active paper
allocations, so paper copies lag by seconds instead of a polling interval
(docs/worker.md "Streaming"). It is optional: the poller is the source of
truth, and `fly scale count stream=0` turns streaming off with no other
effect. Turn it on once creators have paper followers:

```
fly scale count stream=1
```

- Outbound only (no `http_service`), same secrets as `worker` (it decrypts
  keys through the vault, audited as purpose `stream`).
- More than one machine is safe: each streamed account is a Postgres lease,
  so no account is subscribed twice; a stopped machine's accounts move to
  another within `STREAM_LEASE_SECONDS` (120 s).
- **Hyperliquid caps websocket user subscriptions at 10 unique users per
  IP.** Every machine behind one static egress IP shares those 10 slots
  (keyed by `EGRESS_IP`, which must therefore name the real shared IP on
  every machine, as for the worker's rate buckets); the 10 most-followed
  Hyperliquid creators stream, the rest are polled. More needs a second
  egress IP (another region) with its own `EGRESS_IP`.
- Kraken streams only for keys that also carry the optional "WebSocket
  interface" permission (docs/venues/kraken.md); Coinbase and Kraken stay
  behind their partner approvals like every other path.

## Secrets (full list)

Set with `fly secrets set <NAME>=<value> ...`; never in the image, never in
`fly.toml`, never committed (`keys/` and `.env*` are gitignored and in
`.dockerignore`).

| Secret | Required | What it's for |
|---|---|---|
| `DATABASE_URL` | yes | set automatically by `fly mpg attach` |
| `SECRET_KEY` | yes | session/cookie signing (`>=32` random bytes; preflight enforces length) |
| `VAULT_KEY` | yes | Fernet master key encrypting stored venue credentials (vault.py). Losing it means every user reconnects -- keep a copy in the password manager too, per implementation_plan.md section 5 |
| `SECURE_COOKIES` | yes, `true` in prod | preflight refuses `prod` without it |
| `PUBLIC_BASE_URL` | yes | must be `https://` in prod; preflight enforces it |
| `KALSHI_ENV` | yes | `demo` until M5 sign-off, then `prod` |
| `DEMO_MODE` | no (default `false`) | must stay unset/`false` here; preflight refuses to start with it on |
| `POLYMARKET_ENV` | no (default `prod`) | Polymarket has no demo environment |
| `BACKUP_AGE_RECIPIENT` | needed for `scripts/backup_db.sh` | the `age` **public** key nightly backups are encrypted to; safe to set as a plain secret (it's a public key). The matching identity (private key) is never on this host -- it lives in the password manager and is used only by `scripts/restore_drill.sh`, run from a laptop or a separate, narrowly-scoped CI job |
| `INGEST_INTERVAL_SECONDS`, `PRICE_REFRESH_INTERVAL_SECONDS` | no | worker cadence; defaults are fine to start |

Coming with P3 (not required yet, listed here so this table stays the one
place to check before a real deploy): transactional email provider key
(Postmark/Resend/SES), `AWS_KMS_KEY_ID` + IAM credentials once vault v2
ships, Sentry DSN.

## Backups

`scripts/backup_db.sh` runs `pg_dump --format=custom` piped straight through
`age -r "$BACKUP_AGE_RECIPIENT"` -- plaintext never touches disk -- and fails
closed (refuses to run) if `BACKUP_AGE_RECIPIENT` is unset. Schedule it
nightly (Fly Machines can run a scheduled one-off, or run it from the
`worker` machine via APScheduler once that's wired up) writing to an
off-host bucket (S3 or Cloudflare R2; implementation_plan.md section 5), not
onto the Fly volume.

`scripts/restore_drill.sh <backup-file>` decrypts a backup with an `age`
identity file, `pg_restore`s it into a throwaway scratch database, sanity
checks that `fills`/`settlements`/`signals` came back with rows, then drops
the scratch database. Run this on a schedule too -- the only backup you can
trust is one you've restored. Fly Managed Postgres also takes its own
snapshots; the `age`-encrypted, off-host copy is the independent copy that
survives losing the Fly account entirely.

The ledger is append-only, so a restore never loses corrections, only very
recent rows, which the worker re-ingests on its next tick.

## Vercel (demo build only)

The click-through demo for people who just want to explore the site. Vercel
runs the same FastAPI app as one serverless function (`api/index.py`, with
`vercel.json` rewriting every path to it) plus a hosted Postgres. It is **not**
the production target: serverless has no long-running process, so the worker
never runs — no polling, no reconciliation, no nightly stats. Real traders go
on Fly.

1. Create a Postgres (Vercel Storage -> Neon) in the project; it sets
   `DATABASE_URL` automatically. Use its *pooled* connection string.
2. Run the migrations from a laptop, once:
   `DATABASE_URL=<pooled url> uv run alembic upgrade head`
3. Project settings -> Environment Variables:
   `DEMO_MODE=true`, `SECURE_COOKIES=true`, `SECRET_KEY=<random>`,
   `VAULT_KEY=<fresh Fernet key>`, `PUBLIC_BASE_URL=https://<app>.vercel.app`.
   Framework preset: Other. No build command.
4. Deploy. Every page is sample data behind the "Sample data" banner
   (invariant 7); nothing here touches a real venue.

`scripts/preflight.py` fails against this deployment on purpose: `DEMO_MODE`
is on. That is the point — it is a demo, and it must never become the URL real
users connect a key to. The demo's Neon database is not covered by
`scripts/backup_db.sh` / `restore_drill.sh` above -- it holds only sample
data, never a real venue key.

## Never

- Never set `KALSHI_ENV=prod` until M5 sign-off.
- Never put keys in the image: `keys/` and `.env` are in .dockerignore. Users'
  keys live only in the database, Fernet-encrypted with VAULT_KEY. Losing
  VAULT_KEY means every user reconnects; store it in a password manager too.

## Account security settings (P3)

Preflight refuses to pass until these are set for the production app:

```
fly secrets set \
  RATE_LIMITER=postgres TRUST_PROXY_HEADERS=true HIBP_ENABLED=true \
  EMAIL_BACKEND=smtp SMTP_HOST=smtp.postmarkapp.com SMTP_PORT=587 \
  SMTP_USER=<server token> SMTP_PASSWORD=<server token> \
  EMAIL_FROM="Koine <no-reply@your-domain>"
```

`TRUST_PROXY_HEADERS=true` only once Cloudflare (or the Fly edge) is the sole
way in; otherwise a client could forge `CF-Connecting-IP` and dodge the per-IP
limits. The worker should prune `rate_limit_counters` rows past `expires_at`
(`ratelimit.prune_counters`) periodically.
