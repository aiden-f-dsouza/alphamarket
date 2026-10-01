Provenance: no SnapTrade account exists yet (invariant 6 also forbids live
calls in tests), so every fixture here is built from **SnapTrade's own
published API reference examples**, checked 2026-09-26. Field names and
example values are taken verbatim from the reference pages below; the
specific numbers (prices, quantities, dates, ids) are invented for test
coverage where the docs only sketched a shape. **Re-record every one of
these from a real connected account** before this tier leaves the demo
build, the same way `scripts/kalshi_smoke.py` did for Kalshi
(tests/fixtures/README.md).

- `snaptrade_accounts.json` -- `GET /accounts` response shape and the
  `Robinhood Individual` example row, verbatim from
  https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_listUserAccounts
- `snaptrade_balances.json` -- `GET /accounts/{id}/balances`, verbatim
  example (`cash: 300.71`, `buying_power: 410.71`) from
  https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getUserAccountBalance
- `snaptrade_holdings.json` -- `GET /accounts/{id}/holdings` (deprecated in
  favour of the split balances/positions/orders endpoints, but still the
  best-documented single-call shape; used by `fetch_positions_raw` and
  `scripts/snaptrade_smoke.py`). Positions schema (`units`, `price`,
  `average_purchase_price`, `open_pnl`, nested `symbol.symbol.symbol`) and
  the `cash`/`buying_power` example from
  https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getUserHoldings .
  **[VERIFY]**: the exact nesting of the `symbol` object on a live payload;
  `_symbol_of` in connectors/snaptrade.py is defensive about a flatter shape
  too, but this is the one gap here that could not be confirmed against a
  captured response (the docs page states positions "rely on the `symbol`
  child property" without printing the full nested JSON).
- `snaptrade_activities_fills.json` / `snaptrade_activities_events.json` --
  `GET /accounts/{id}/activities` response envelope (`data` +
  `pagination.{offset,limit,total}`) and the activity object's field names
  (`price`, `units`, `amount`, `fee`, `currency`, `trade_date`,
  `settlement_date`, `institution`, `external_reference_id`) from
  https://docs.snaptrade.com/reference/Account%20Information/AccountInformation_getAccountActivities
  , which also lists every `type` enum value used across the two files
  (BUY, SELL, DIVIDEND, CONTRIBUTION, WITHDRAWAL, REI, STOCK_DIVIDEND,
  INTEREST, FEE, TAX, SPLIT, EXTERNAL_ASSET_TRANSFER_IN/OUT,
  OPTIONEXPIRATION, ADJUSTMENT...). The individual amounts, dates and ids
  are invented for the hand-computed unit tests (tests/test_snaptrade_connector.py);
  `OPTIONEXPIRATION` is included specifically to assert the connector drops
  it (options are out of scope this pass -- see docs/venues/snaptrade.md).
- `snaptrade_authorization_read.json` / `snaptrade_authorization_trade.json`
  -- `GET /authorizations/{id}`, `type: "read"|"trade"` and `disabled` per
  https://docs.snaptrade.com/reference/Connections/Connections_detailBrokerageAuthorization
  ; this is the read-only proof (`ReadOnlyProof.AGGREGATOR_FLAG`).

Request signing (`sign()` in connectors/snaptrade.py) has no published
numeric test vector: SnapTrade's docs show the algorithm
(https://docs.snaptrade.com/docs/request-signatures , checked 2026-09-26)
and a *mockSignature* request shape (`content={"userId": "api@passiv.com",
"userSecret": "CHRIS.P.BACON"}`, `path="/api/v1/snapTrade/mockSignature"`,
`query="clientId=PASSIVTEST&timestamp=1635790389"`) with placeholder keys
("YOUR_CONSUMER_KEY") and no printed output. `test_sign_matches_documented_
algorithm` in tests/test_snaptrade_connector.py pins the output of this
module's own implementation against that documented input shape (with a
concrete test consumer key substituted for the placeholder) so a future
change to the canonical-JSON construction is caught, and reproduces the
byte-for-byte construction described in the docs and confirmed against the
OpenAPI spec at
https://github.com/passiv/snaptrade-api-docs/blob/main/docs/api.yaml
(`Signature` header; `clientId`/`timestamp`/`userId`/`userSecret` query
params).

Rate limits (`registry.PROFILES["snaptrade"].rate_budgets`): 250
requests/minute at the commercial-partner ("customer") level, from
https://docs.snaptrade.com/docs/ratelimiting , checked 2026-09-26. That page
also documents the 429 response body used by `_retry_after()`
(`{"detail": "Request was throttled. Expected available in N seconds.",
"status_code": 429, "code": "0000"}`) and the `X-RateLimit-*` headers; no
`Retry-After` header is documented, which is why `_retry_after()` falls back
through the reset headers and then a regex on `detail`.
