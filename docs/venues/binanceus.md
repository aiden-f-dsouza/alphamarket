# Binance.US

**Status: `ok`** (with an "at your own risk" consent, matching
`docs/platform_plan.md` section 3.3's summary of the 2026-09-16 research).
Binance.US's Terms of Use neither authorizes nor forbids read-only
third-party API access by name; it permits granting API-key access to a
third party generally, disclaims liability for what that third party does,
and puts the security burden on the account holder. Nothing in the Terms
prohibits Koine from reading fills/balances/history with a read-only key and
publishing a computed track record from them, but nothing explicitly blesses
it either -- hence "at your own risk" rather than a stronger `ok`.

## Terms of service position

- **Checked**: 2026-09-26, at <https://www.binance.us/terms-of-use>.
- **Relevant clause** (Section on API/Automated Trading access, paraphrased
  from the live page, quoted where exact): granting a third-party
  application access is expressly contemplated -- *"you are granting
  permission to a Third-Party Application to access or connect to your
  Account, and you expressly authorize BAM to provide information to, or
  accept information, including Instructions, from, such Third-Party
  Application"* -- but every risk of doing so sits with the account holder:
  *"Under no circumstances shall any of the Indemnified Persons be
  responsible or liable for any direct or indirect losses ... arising out of
  or related to any act or omission of any Third-Party Application."* The
  Terms do not separately address data aggregation, redistribution, or
  public display of account data (no clause found either permitting or
  forbidding it); Koine's own public-page invariant (platform CLAUDE.md #5:
  only platform-computed numbers, never raw statements) is the relevant
  guardrail on Koine's side regardless.
- Binance.US's own help center explicitly recommends exactly the shape this
  connector uses: *"only share your API keys with trusted third-party
  platforms and provide the minimum level of access required ... for tax
  reporting platforms, only grant Read access instead of Spot Trading or
  Withdrawal access."* (support.binance.us, "API keys: Best practices &
  safety tips", checked 2026-09-26.)
- No commercial-use disclosure requirement was found (unlike Alpaca); no
  written approval process exists to request (unlike Coinbase CDP).

## Consent text shown on the connect screen

> "You are connecting a Binance.US API key in READ-ONLY mode. Koine will use
> it only to read your trade history, balances, deposits and withdrawals --
> never to place an order, convert, or withdraw. Binance.US's own Terms of
> Use hold you responsible for any third-party application you grant key
> access to; only continue with a key that has Enable Reading checked and
> every other permission (Spot & Margin Trading, Withdrawals, Margin,
> Futures, Internal Transfer) OFF. Connect at your own risk."

## Read-only proof method

`ReadOnlyProof.SCOPE_ENDPOINT`, via `GET /sapi/v1/account/apiRestrictions`
(the exact path on `api.binance.us` is **[VERIFY]**: confirmed for
`api.binance.com`, and the 2026-09-16 crypto-venues research states
Binance.US serves "the same shapes", but no live Binance.US account has
called it yet -- `scripts/binanceus_smoke.py` is the check to run first).
The response's `enableSpotAndMarginTrading`, `enableWithdrawals`,
`enableMargin`, `enableFutures` and `enableInternalTransfer` flags must all
be false for the connector to report `read_only=True`; `enableReading` and
`ipRestrict` are recorded in `ReadOnlyEvidence.details` for the methodology
page but are not, by themselves, sufficient (a key can have reading enabled
*and* trading enabled at once).

## What a read-only credential exposes

An `Enable Reading`-only Binance.US API key (secret key, HMAC-SHA256
signing) lets Koine read, and nothing more:

- Spot balances (`GET /api/v3/account`)
- Trade history per symbol (`GET /api/v3/myTrades`)
- Deposit and withdrawal history (`GET /sapi/v1/capital/deposit/hisrec`,
  `GET /sapi/v1/capital/withdraw/history`)
- Convert and dust-conversion history (`GET /sapi/v1/convert/tradeFlow`,
  `GET /sapi/v1/asset/dust-log` -- both **[VERIFY]** exact paths on
  `api.binance.us`)
- The key's own permission flags (`apiRestrictions`, above)

It cannot place, modify or cancel an order, initiate a withdrawal, transfer
between accounts, or change account settings -- Binance.US enforces this
key-side (an unauthorized action returns an API error), and
`prove_read_only()` re-checks the flags nightly so a key later upgraded to
trading is caught and the account suspended (platform_plan D4/M9.8).

## Rate limits

- **6,000 request-weight per minute, per IP** (`REQUEST_WEIGHT` bucket;
  docs.binance.us rest-api.md, checked 2026-09-26). Koine's `RateBudget` for
  `binanceus` is `RateScope.IP` with `capacity=6000, refill_per_minute=6000`.
- Per-endpoint weights used here: `account` 10, `myTrades` 10 (with
  `symbol`), `exchangeInfo` 20, `klines`/`bookTicker` 1. `apiRestrictions`,
  `depositHistory`, `withdrawHistory`, `convertHistory` and `dustLog` are
  marked `[VERIFY]` at weight 1 in `registry.py` -- not in the fetched docs
  excerpt; confirm from the `X-MBX-USED-WEIGHT-1M` response header on a live
  smoke run (`throttle_after` already syncs to it).
- 429 (rate limited) or 418 (IP auto-banned for repeat 429s) both raise
  `RateLimited`, parsed from the `Retry-After` header when present.

## History depth

- `myTrades` itself has no documented depth limit: it pages by trade id
  (`fromId`), so a full history is reachable, just slowly (per-symbol, and
  bounded to 24h windows until a symbol's first trade id is known).
- Deposit, withdrawal, convert and dust histories are windowed to **under
  90 days per call** (`history_depth="90d"` in the profile). A community
  Binance.US API issue (binance-us/binance-official-api-docs#74, cited in
  the 2026-09-16 research) reports the deposit/withdraw endpoints have at
  times ignored `startTime`/`endTime` bounds and returned only a recent
  slice regardless -- treat wider historical transfer backfills as
  best-effort until confirmed against a live account.

## Known gaps

- **Ed25519 key signing** [VERIFY]: Binance's newer API-key type
  (Ed25519, recommended over HMAC on Binance.com) is not confirmed as
  available on Binance.US. This connector only implements HMAC-SHA256,
  which every source checked (docs.binance.us, the 2026-09-16 research)
  confirms is supported.
- **`apiRestrictions`, `convert/tradeFlow`, `asset/dust-log` exact paths**
  [VERIFY] on `api.binance.us` specifically (see above); carried over from
  Binance's global API docs on the stated assumption ("same shapes") that
  Binance.US mirrors them.
- **No fiat deposit/withdrawal endpoint** is documented for Binance.US in
  the sources checked; only crypto deposit/withdrawal histories are wired
  up. A fiat-funded account's cash movements would be invisible to
  `fetch_events_page` until this is revisited.
- **Symbol discovery is balance/deposit-based**, so a symbol the account
  fully exited (sold to zero) with no matching deposit on file (e.g. it
  arrived via a convert/dust conversion rather than a deposit) may be
  missed on the very first backfill. A future pass could also cross convert
  history into the discovery candidate set.
- No sandbox/testnet exists for Binance.US in the sources checked, so this
  connector has only `envs=("prod",)`; every mapping here is verified
  against official docs' example payloads, not a live account (see
  `tests/fixtures/binanceus/README.md`), pending `scripts/binanceus_smoke.py`.
