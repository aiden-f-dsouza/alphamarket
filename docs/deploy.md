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

## Backups

Fly Postgres takes daily snapshots; also run `fly postgres backup` before every
migration. The ledger is append-only, so a restore never loses corrections,
only recent rows, which the worker re-ingests on its next tick.

## Never

- Never set `KALSHI_ENV=prod` until M5 sign-off.
- Never put keys in the image: `keys/` and `.env` are in .dockerignore. Users'
  keys live only in the database, Fernet-encrypted with VAULT_KEY. Losing
  VAULT_KEY means every user reconnects; store it in a password manager too.
