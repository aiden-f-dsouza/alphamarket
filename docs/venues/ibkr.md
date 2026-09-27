# Interactive Brokers (Flex Web Service)

**Status: `needs_approval`** (`VenueProfile.requires_partner_approval=True`;
the connect form refuses this venue in prod until this is flipped to `ok`
by a human, per invariant 10 / platform_plan D10).

## Terms-of-service position

Checked 2026-09-26. IBKR's own developer pages
(https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/,
https://www.interactivebrokers.com/docs/web-api/flex-web-service/introduction,
and the Activity Flex Query reference at
https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm)
describe how to configure a Flex Query and retrieve its XML/CSV statement
via the Flex Web Service, but **contain no clause that either permits or
forbids a consented third party (like Koine) fetching and displaying a
user's own Flex statement on their behalf**. This is different from the TWS
API, whose third-party terms explicitly prohibit "disseminating ... market
data or any other licensed or copyrighted information ... to third parties
or non-registered IB customers" without written approval
(https://interactivebrokers.github.io/tws-api/third_party.html) --
Flex activity data (a user's own trades and cash movements) is not
IBKR-licensed market data, but no page found says so, and none of IBKR's
public developer pages mention "Flex" and "third party" in the same
sentence at all.

This is a materially different situation from Kraken or Alpaca (which say
"yes, a consented third party may read this") and from Coinbase's CDP terms
(which say "no, not without approval," so `requires_partner_approval` has a
concrete approval channel to pursue). Here the silence itself is the
`[VERIFY]`: **do not treat an unaddressed terms page as permission.**
`requires_partner_approval=True` is the conservative default until one of:

1. IBKR's Client Services or `webapionboarding@interactivebrokers.com`
   (the same address the OAuth 1.0a third-party program uses,
   docs/research/2026-09-16-equities-brokers.md) confirms in writing that a
   user may point a consented third-party reporting tool at their own Flex
   token; or
2. Counsel reviews the IBKR Customer Agreement and the Flex Web Service
   configuration screen's own clickthrough text (both gated behind a live
   account login, so not fetchable here) and finds an applicable clause.

Practically, a Flex token is themselves "personal" reporting credentials in
IBKR's own framing (research doc: "Flex is 'personal' reporting; used by
trade journals in practice but not an explicitly licensed third-party
channel"), which is some evidence in favor -- third-party trade-journal
tools built on Flex are common and IBKR has not, as far as this research
found, taken action against them -- but that is practice, not a documented
policy, so it does not move the status to `ok`.

## Consent text (connect screen)

Shown once `requires_partner_approval` flips to `False`:

> Interactive Brokers connects through a **Flex Web Service report token**,
> not an API key: it can only ever retrieve the one Activity Flex Query you
> configure for it (trades, cash transactions, corporate actions, positions
> and NAV), end-of-day. It cannot place an order, move money, or read
> anything outside that one statement. Koine reads this statement on the
> schedule you choose and stores only what your public strategy page and
> your own dashboard need. You can revoke the token in IBKR's Client Portal
> at any time, which disconnects Koine immediately.

## Read-only proof method

`ReadOnlyProof.TOKEN_TYPE`: a Flex token is reporting-only **by
construction** -- IBKR's Flex Web Service exposes no endpoint that accepts
one to place an order, submit an instruction, or move funds, so there is
nothing to probe (unlike Kraken's `order_probe`, which validates a refusal).
`prove_read_only()` still performs one cheap statement fetch (`p=1`, the
smallest lookback window) to confirm the token and query id are both still
valid and readable, and records the `accountId` and `whenGenerated` it got
back as evidence (never secrets).

**Token expiry is not observable via the API.** IBKR lets the user set an
expiration date on a Flex token when they generate it (Client Portal only),
but no Flex Web Service response exposes that date back to the caller, so
`ReadOnlyEvidence.details["token_expiry_observable"]` is always `False`.
Aiden/the user must track and rotate the token by hand before it lapses;
this is called out on the connect screen copy above and should be repeated
in the dashboard's credential-rotation reminder once P3 (M9) ships.

## What a read-only credential exposes

The Flex Query id and token together can retrieve exactly the statement
sections that specific query was configured with -- for Koine's use, that
is: Trades (execution-level detail, so `ibExecID` is present), Cash
Transactions, Corporate Actions, Open Positions and NAV
(`EquitySummaryInBase`). A query configured with fewer sections exposes
less; it can never expose more than what was checked when the query was
created, and it cannot read any *other* query or account the credential
holder did not explicitly add to the same query.

## Rate limits

No formal requests-per-minute number is published. The only documented
back-pressure signal is Flex Web Service error code **1018** ("too many
requests have been made from this token, please try again later"), plus
1009 ("server under heavy load") and 1019 ("statement generation in
progress") while a statement is still rendering -- all three are polled
with a fixed delay by `connectors/ibkr.py`, and 1018 is raised to the rest
of the codebase as `base.RateLimited` exactly like an HTTP 429 would be.
`VenueProfile.rate_budgets` carries a conservative placeholder (one request
roughly every 6 seconds) marked `[VERIFY]` in `registry.py` until a real
account's usage confirms a looser number.

## History depth

`fd`/`td` (the explicit date-range parameters) are capped at **365 days per
request** (`history_depth="365d"`). A Flex Query's own "Date Period"
setting (configured in Client Portal, not via this API) can independently
narrow what a given query is willing to return regardless of `fd`/`td`.

## Known gaps

- **Statement date/time format `[VERIFY]`**: this connector reads
  `dateTime="yyyyMMdd;HHmmss"` as UTC. That is only exact if the Flex
  Query's Date/Time Format is configured to report in UTC; IBKR's default
  is the account's local timezone. Must be re-checked against a real
  statement and either confirmed or corrected once a founder account
  exists (`tests/fixtures/ibkr/README.md`).
- **Non-USD currencies are not converted.** A trade or cash transaction in
  a currency other than the query's base currency is recorded as a leg in
  that native currency's asset code (e.g. `"EUR"`), unconverted. FX
  valuation of non-base-currency legs is a followup (platform_plan.md
  2.2's per-asset `prices_daily` design does not yet cover FX pairs).
- **Scope is equities/ETFs only** (`assetCategory == "STK"`); options,
  bonds, forex conversions and other Flex asset categories are parsed out
  of the statement and silently skipped (not misrepresented as equities).
  IBKR options support is explicitly out of scope for this pass
  (platform_plan 3.2, "Options and shorts are phase 2").
- **Only splits are read from `CorporateActions`** (`type` in `{"FS",
  "RS"}`); IBKR's Reorg code list has roughly 35 other types (mergers,
  spinoffs, rights issues, tender offers, ...). Anything else in that
  section is not turned into a ledger event at all this pass.
- **Unmapped `CashTransaction` types become `ADJUSTMENT` legs**, not
  dropped, with the venue's raw `type` string kept in the event
  description for an admin to reclassify later (`LegKind.ADJUSTMENT`:
  "venue correction or admin classification (audited)").
- **A statement can span several IBKR accounts under one query.** This
  connector filters every section to `account_ref` (the IBKR `accountId`);
  `account_ref=None` assumes the query covers exactly one account and reads
  whichever `accountId` appears first.
- **Market data is a stub.** IBKR has no public (unauthenticated)
  market-data API comparable to Kalshi's `/markets` or a crypto venue's
  public candles -- Flex is a reporting service for an existing account,
  not a quote feed. `IbkrMarketData` therefore implements nothing beyond
  the `MarketDataSource` defaults (empty lists / `None`); daily closes for
  marking IBKR equity positions are expected to come from Alpaca's market
  data connector once it exists (platform_plan M11, implementation_plan
  P5), keyed by the same equity symbol.

## References

- https://www.interactivebrokers.com/campus/ibkr-api-page/flex-web-service/
- https://www.interactivebrokers.com/docs/web-api/flex-web-service/introduction
- https://www.interactivebrokers.com/docs/web-api/flex-web-service/using-flex-web-service/retrieve-the-report
- https://www.ibkrguides.com/reportingreference/reportguide/activity%20flex%20query%20reference.htm
- https://interactivebrokers.github.io/tws-api/third_party.html (TWS API third-party terms, for contrast, not directly applicable to Flex)
- https://github.com/csingley/ibflex (open-source Flex client/parser used to cross-check field names, error codes and enum values; see tests/fixtures/ibkr/README.md)
- docs/research/2026-09-16-equities-brokers.md (row 2, IBKR)
