# Deploy (M4.4)

Target: Fly.io, one `web` and one `worker` process from the same image, Fly
managed Postgres. Anything with a Dockerfile and a Postgres works the same way.

## First deploy

```
fly launch --no-deploy            # uses fly.toml; say no to creating a new Postgres if you have one
fly postgres create --name alphamarket-db
fly postgres attach alphamarket-db   # sets DATABASE_URL (postgres://...)
fly secrets set \
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')" \
  VAULT_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  SECURE_COOKIES=true KALSHI_ENV=demo
fly deploy
```

`DATABASE_URL` from Fly is `postgres://`; the app rewrites it to
`postgresql+psycopg://` at startup (config.py).

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
users connect a key to.

## Backups

Fly Postgres takes daily snapshots; also run `fly postgres backup` before every
migration. The ledger is append-only, so a restore never loses corrections,
only recent rows, which the worker re-ingests on its next tick.

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
