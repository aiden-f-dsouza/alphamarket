# Alpaca

**Status: `needs_approval`** (profile `requires_partner_approval=True`).
Built and tested against Alpaca's documented example payloads; connectable in
production only once Alpaca has approved Koine's OAuth app in writing.

- Venue: Alpaca Securities LLC, US stocks and ETFs. Koine supports **long
  equity positions only** (see "Known gaps").
- Connector: `src/alphamarket/connectors/alpaca.py`; OAuth client:
  `src/alphamarket/oauth.py`; smoke script: `scripts/alpaca_smoke.py`.
- Environments: `paper` (`paper-api.alpaca.markets`) and `live`
  (`api.alpaca.markets`), chosen by the `ALPACA_ENV` setting.

## Terms-of-service position (checked 2026-09-26)

Alpaca offers a sanctioned third-party path: OAuth apps registered under
"Alpaca Connect". The relevant terms:

- **About Connect API, "Terms of Access and Use"**
  (<https://docs.alpaca.markets/us/docs/about-connect-api>, page updated
  2026-04-24): "You must read the terms and register in order to connect
  and use Alpaca's APIs"; "All API clients must authenticate with OAuth
  2.0"; "You may not imply that the app was developed by Alpaca"; **"If you
  are building a commercial application that makes money (including ads,
  in-app purchases, etc), you must disclose it in the registration form and
  receive written approval."** Live trading for other users needs a separate
  approval, which Koine never requests.
- **Using OAuth2 and Trading API**
  (<https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api>, updated
  2026-03-02): scopes are `account:write`, `trading` and `data`; **"Read-only
  endpoint access is assumed by default."**
- **Registering Your App** (<https://docs.alpaca.markets/us/docs/registering-your-app>):
  the app is submitted from the dashboard's Alpaca Connect page and Alpaca's
  team reviews it.
- **Alpaca Customer Agreement**
  (<https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf>):
  the customer's "grant of access to My Account to any third party to access
  information or place transactions in My Account is solely at My risk".
  Consented third-party read access is therefore contemplated by the
  customer agreement; the risk statement belongs in the consent text.

Position: **permitted with approval.** Consented, read-only third-party
access through a registered OAuth app is exactly what Alpaca Connect is for.
Koine is commercial, so it must disclose that on registration and hold
Alpaca's written approval before production users connect. Until then the
profile keeps `requires_partner_approval=True` and paper accounts are the only
test surface. Must not: use Alpaca's name or logo so as to imply Alpaca built
or endorses Koine.

## Consent text (connect screen)

> **Connect your Alpaca account to Koine (read-only)**
>
> You will be sent to Alpaca to sign in and approve access. Koine asks for
> **no permissions** beyond Alpaca's read-only default: not `trading`, not
> `account:write`, not `data`. With this grant Koine can read your account
> balance, positions and account activity (fills, deposits, withdrawals,
> dividends, fees, interest, journals, splits) to build your verified track
> record. It cannot place, change or cancel orders, change account settings,
> or move money.
>
> Koine never sees your Alpaca password. The access token is encrypted at
> rest and is erased when you disconnect; you can also revoke Koine at any
> time in the Alpaca dashboard (Alpaca Connect, authorized apps). On a paper
> account Koine checks the grant by sending one deliberately invalid order
> (zero shares of a symbol that does not exist) that Alpaca must refuse; on a
> live account it never sends anything but reads.
>
> Alpaca's customer agreement says that granting a third party access to
> your account information is at your own risk. Koine is not affiliated with
> or endorsed by Alpaca.
>
> [ Continue to Alpaca ]

## Read-only proof (`ReadOnlyProof.ORDER_PROBE` plus the granted scope list)

1. **Scope list.** The token response carries `scope`; Koine stores it with
   the token. Any of `trading` or `account:write` = writable: the grant is
   refused at callback (and on every re-check), with no further request.
2. **Order probe, paper only.** `GET /v2/account` first (so a later 401 cannot
   be a dead token), then `POST /v2/orders` with:

   ```json
   {"symbol": "KOINEPROBE0", "qty": "0", "side": "buy", "type": "limit",
    "limit_price": "0.01", "time_in_force": "day",
    "client_order_id": "koine-readonly-probe-<random>"}
   ```

   It cannot execute for three independent reasons: the quantity is zero
   (Alpaca requires qty > 0), the symbol is not a listed asset, and paper
   accounts hold no real money. Reading the answer:
   - 401/403 (except a "buying power / insufficient" refusal) = refused before
     validation, i.e. for lack of the `trading` scope: **read-only**. If the
     account itself is `trading_blocked`/`account_blocked`, the refusal
     proves nothing: **undetermined**.
   - 400/404/422, or a buying-power 403 = the request got past
     authorization into validation: **writable**, rejected.
   - 2xx (impossible for this body) = writable, logged as an error.
   - 5xx = undetermined, re-checked nightly.
3. **Live accounts are never probed.** The scope list alone decides: no write
   scope = read-only; unknown scope = undetermined.

Evidence details stored: env, granted scopes, write scopes granted, account
status, trading_blocked, probe description, probe status and Alpaca's error
message. Never the token.

[VERIFY] with a registered app: that a zero-scope token gets 401/403 (not
422) on `POST /v2/orders`, and the exact message.

## What a read-only grant exposes

Trading API v2 read endpoints for the one authorized account: `/v2/account`
(cash, equity, buying power, status flags, account number), `/v2/positions`,
`/v2/orders` (history), `/v2/account/activities`, portfolio history,
watchlists and account configuration (read), the trade-updates stream.
Koine reads only account, positions and activities. Market data is **not**
read with the user's token (that needs the `data` scope); Koine uses its own
Alpaca API key for bars, quotes, corporate actions and the asset list.

## Rate limits

- Trading API: **200 requests per minute per account**; 429 when exceeded
  (Alpaca support "usage limit", checked 2026-09-26). Headers
  `X-RateLimit-Limit/Remaining/Reset`. Profile budget: capacity 200, refill
  200/min, per credential.
- Market data (Koine's own key): 200/min on the free Basic plan, 10,000/min
  on Algo Trader Plus [VERIFY plan at signup]. Profile budget: one shared
  bucket (modelled as `RateScope.IP`).
- On 429 the connector raises `RateLimited` with `Retry-After`, else
  `X-RateLimit-Reset - now`, else 60 s.

## History depth

Activities go back to account opening, paged 100 at a time (`page_token` =
last activity id, `direction=asc`). No documented cutoff: `history_depth="all"`
[VERIFY on a multi-year live account].

## Activity mapping

| Alpaca `activity_type` | Koine |
|---|---|
| `FILL` side buy/sell, equity symbol | asset fill: FILL_BASE +/- qty (9 dp), FILL_QUOTE -/+ qty x price (USD 6 dp, half-even) |
| `FILL` side `sell_short` | unsupported (`short_sale`) |
| `FILL` on an OCC option or crypto symbol | unsupported (`option` / `crypto`) |
| `CSD`, `CSW`, `TRANS` | DEPOSIT / WITHDRAWAL by sign of `net_amount` |
| `JNL`, `JNLC`, `ACATC` | TRANSFER_IN / TRANSFER_OUT (USD) by sign |
| `JNLS`, `ACATS` (or `JNL` with symbol + qty and no cash) | TRANSFER_IN / TRANSFER_OUT of shares by sign of `qty` |
| `DIV` (not `SDIV`), `DIVCGL`, `DIVCGS`, `DIVROC`, `DIVTXEX`, `CGD` | DIVIDEND (USD) |
| `DIV` sub-type `SDIV` (stock dividend) | unsupported (`stock_dividend`) |
| `DIVNRA`, `DIVTW`, `DIVFT`, `INTNRA`, `INTTW` | TAX (USD) |
| `INT` | INTEREST (USD, negative for margin interest) |
| `FEE`, `DIVFEE`, `PTC` | FEE (USD; a positive amount is a refund -> ADJUSTMENT) |
| `PTR` | ADJUSTMENT (rebate) |
| `SSP`, `SPLIT` | SPLIT: signed `qty` in shares [VERIFY], plus a USD SPLIT leg for cash in lieu |
| `OPASN`, `OPEXP`, `OPXRC`, `OPEXC`, `OPTRD`, `OPCA`, `OPCSH` | unsupported (`option`) |
| `CFEE`, `OCT` | unsupported (`crypto`) |
| `MA`, `NC`, `SC`, `REORG`, `REO`, `SSO`, `SPIN`, `FOPT`, `MISC`, new types | unsupported (`unsupported_type`) |
| any row with `status: canceled` | unsupported (`canceled`) |

Non-trade rows are date-only (`date`), so they land at 23:59:59 UTC of that
date. Fills use `transaction_time` (nanoseconds truncated to microseconds).

## Streaming (P7): not enabled

`trade_updates` on `wss://{paper-,}api.alpaca.markets/stream` would be a
NUDGE source (its `fill`/`partial_fill` events carry an `execution_id`; the
poller's FILL activities carry their own ids). It is a documented stub
(`AlpacaConnector.stream_source` returns None) because of one open question:

[VERIFY] with a registered OAuth app: does `trade_updates` accept a token
granted with NO `trading` scope (Koine's read-only grant: authenticate with
`{"action":"auth"}`/OAuth token, then `listen` to `trade_updates`), and does
it then deliver the account's fills? "What a read-only grant exposes" above
lists the stream, but that has not been confirmed on a real app. Until it
is, Koine opens no trading-stream connection with a user's grant; Alpaca
accounts are polled.

## Known gaps

- **Unsupported activity** (above) is skipped, never guessed, and returned by
  `AlpacaConnector.unsupported_activities()` so the account shows an
  "unsupported activity" flag. Options and shorts need the option/short leg
  model (platform_plan phase 2).
- Symbol changes (`NC`/`SC`) and mergers are flagged on the account side;
  `AlpacaMarketData.fetch_corporate_actions` does return name changes for
  `symbol_history`, but the ledger wiring is the integrator's.
- Alpaca's cash rounding of fractional fills is [VERIFY]: Koine rounds qty x
  price half-even to 1e-6 USD; reconciliation tolerance absorbs sub-cent
  residue.
- Tokens: Alpaca documents no expiry, no refresh token and **no revoke
  endpoint**. Disconnect erases the token and tells the user to revoke Koine
  in the Alpaca dashboard.
- PKCE is sent (S256) although Alpaca's docs do not mention it; RFC 7636
  servers ignore unknown parameters [VERIFY at first real connect].
- The paper environment cannot simulate dividends or splits: those mappings
  are fixture-driven until a live account sees one.
- Crypto on Alpaca (fills, `CFEE`, `OCT`) is out of scope for this connector.
