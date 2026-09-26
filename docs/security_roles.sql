-- Postgres roles for Koine (platform_plan 2.7 item 7; P3 item 11).
--
-- koine_web     the web app. Reads and writes the app tables, but CANNOT read
--               credential ciphertext: a SQL injection or a leaked web DB
--               password yields no encrypted key material at all. It can
--               still insert a credential (connect) and erase one (revoke).
-- koine_worker  the ingestion worker. The only role that can SELECT
--               ciphertext, because it is the only process that decrypts.
-- koine_migrator (the owner) runs alembic; the app never connects as it.
--
-- Apply once per database as the owner, after `alembic upgrade head`, and
-- again after every migration (DEFAULT PRIVILEGES cover new tables; the
-- column rules below are recomputed from the catalog on each run).
--
--   psql "$OWNER_DATABASE_URL" -v web_pw="'...'" -v worker_pw="'...'" \
--        -f docs/security_roles.sql
--
-- Wiring: DATABASE_URL for the web process uses koine_web, the worker's uses
-- koine_worker. A web request that needs a decrypt (manual sync, reconcile,
-- connect-time first import) must be moved to a worker job before the web
-- process is switched to koine_web; until then run both as koine_worker.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'koine_web') THEN
        CREATE ROLE koine_web LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'koine_worker') THEN
        CREATE ROLE koine_worker LOGIN;
    END IF;
END
$$;

ALTER ROLE koine_web PASSWORD :web_pw;
ALTER ROLE koine_worker PASSWORD :worker_pw;

GRANT USAGE ON SCHEMA public TO koine_web, koine_worker;

-- Everything except venue_credentials: ordinary read/write. Append-only
-- tables are protected by triggers, not by grants, so they hold for every
-- role including the owner.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
    TO koine_web, koine_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO koine_web, koine_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO koine_web, koine_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO koine_web, koine_worker;

-- audit_log: insert and read only (the trigger also forbids UPDATE/DELETE).
REVOKE UPDATE, DELETE ON audit_log FROM koine_web, koine_worker;

-- Web role: column-level SELECT on the two tables that hold key material,
-- computed from the catalog so columns added by later migrations are
-- included automatically (re-run this file after each migration).
--   venue_credentials: no ciphertext, refresh_ciphertext, wrapped_dek.
--   venue_accounts:    no encrypted_private_key (legacy v1 column, same
--                      secret class, until a later migration drops it).
-- INSERT (connect) and UPDATE (revoke writes NULL) stay allowed: writing a
-- value never reveals one. The ORM defers these columns, so ordinary
-- queries never select them.
DO $$
DECLARE
    spec record;
    cols text;
BEGIN
    FOR spec IN
        SELECT * FROM (VALUES
            ('venue_credentials', ARRAY['ciphertext', 'refresh_ciphertext', 'wrapped_dek']),
            ('venue_accounts', ARRAY['encrypted_private_key'])
        ) AS t(tbl, hidden)
    LOOP
        SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position)
          INTO cols
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = spec.tbl
           AND column_name <> ALL (spec.hidden);
        EXECUTE format('REVOKE SELECT ON %I FROM koine_web', spec.tbl);
        EXECUTE format('GRANT SELECT (%s) ON %I TO koine_web', cols, spec.tbl);
    END LOOP;
END
$$;
REVOKE DELETE ON venue_credentials FROM koine_web;

-- venue_credentials, worker role: full access (it decrypts and rotates).
GRANT SELECT, INSERT, UPDATE ON venue_credentials TO koine_worker;
REVOKE DELETE ON venue_credentials FROM koine_worker;
