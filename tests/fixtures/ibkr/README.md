# IBKR Flex fixture provenance

No IBKR account exists yet (invariant #6: no live API calls in tests), so
these are **reconstructed sample statements**, not captures from a real
account. Every tag and attribute name is taken verbatim from IBKR's own Flex
schema as documented across:

- IBKR Flex Web Service introduction / retrieve-the-report pages:
  https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/ ,
  https://www.interactivebrokers.com/docs/web-api/flex-web-service/introduction
  (checked 2026-09-26; the exact request/response bodies are behind a
  login-walled doc viewer, so the request/response shape below is
  cross-checked against `ibflex` below rather than pasted from IBKR's page).
- `ibflex` (github.com/csingley/ibflex), a widely used open-source Python
  parser/client for this exact API: `ibflex/client.py` for the
  SendRequest/GetStatement URLs, query params (`t`, `q`, `v`, `p`, `fd`,
  `td`) and the documented error-code table (1003-1021, with 1009/1018/1019
  retryable); `ibflex/Types.py` and `ibflex/enums.py` for the field names on
  `Trade`, `CashTransaction`, `CorporateAction`, `OpenPosition` and
  `EquitySummaryByReportDateInBase`, and the `CashAction`/`Reorg` (corporate
  action) enum values ("FS" forward split, "RS" reverse split, "Deposits &
  Withdrawals" / "Dividends" / "Withholding Tax" / "Broker Interest
  Received", etc). Retrieved 2026-09-26.

**Re-record from a real account** once a founder IBKR account and Flex Query
exist (implementation_plan.md section 5, "IBKR: Account + a Flex Query
(Activity) + Flex Web Service token"): swap these files for
`scripts/record_fixture.py` captures the same way `tests/fixtures/kalshi_*`
and `tests/fixtures/polymarket_*` were, and update this file with real dates
and a note on what changed. In particular, VERIFY on a real statement:

- Whether `dateTime` on Trades/CashTransactions carries a UTC offset (this
  connector currently assumes the venue's Date/Time format is UTC; see
  `connectors/ibkr.py` module docstring).
- The exact spelling IBKR uses for the deposit/withdrawal `type` string
  today (`ibflex` issue #125 notes IBKR's real statements have been seen to
  say "Deposits/Withdrawals" where the library's own enum says "Deposits &
  Withdrawals"; both are matched).
- Whether `ibExecID` is present at all report configurations, or only when
  the query's "Level of Detail" is set to "Execution" (`levelOfDetail` is
  included on every synthetic Trade row here to make that requirement
  explicit).

## Files

- `send_request_success.xml` / `send_request_fail_bad_token.xml` --
  `SendRequest` responses: a numeric `ReferenceCode` plus the `GetStatement`
  URL on success; `Status=Fail` with an `ErrorCode`/`ErrorMessage` pair
  otherwise (1003 = the generic "invalid request" code, seen for a bad
  token or query id).
- `get_statement_in_progress.xml` -- a `GetStatement` poll while IBKR is
  still generating the statement (code 1019); the connector sleeps
  `POLL_INTERVAL_SECONDS` and retries.
- `get_statement_too_many_requests.xml` -- IBKR's rate-limit signal (code
  1018), mapped to `base.RateLimited` exactly like an HTTP 429 would be.
- `get_statement_success.xml` -- a `<FlexStatements count="2">` response
  covering two IBKR accounts under one ("master") Flex Query, each its own
  SIBLING `<FlexStatement accountId="...">` element (verified against
  `ibflex`'s `FlexQueryResponse.FlexStatements: tuple[FlexStatement, ...]`,
  each with its own required `accountId` -- NOT one statement with mixed
  per-row accountIds, which an earlier version of this fixture wrongly
  modeled and which let `connectors/ibkr.py` get away with reading only the
  first `<FlexStatement>` in the response). `U1234567` (the account this
  connector's tests mostly read via `account_ref`) has two `Trade` rows (a
  buy then a partial sell of AAPL, both at Execution detail with
  `ibExecID`), a deposit/dividend/withholding-tax/broker-interest
  `CashTransaction` set, a 4-for-1 forward split `CorporateAction`, one
  `OpenPosition`, and two `EquitySummaryByReportDateInBase` rows (to prove
  the connector picks the latest `reportDate`, not the first one in
  document order). `U9999999` has its own statement with one MSFT `Trade`,
  one deposit, one `OpenPosition` and one equity summary row, to prove
  `account_ref` selects the matching SIBLING statement rather than merging
  or reading the wrong one. Every dollar figure in the tests is
  hand-computed from these exact attribute values -- see
  `tests/test_ibkr_connector.py`.
