# SnapTrade

Aggregator for the long-tail equities brokers Koine cannot reach directly
(Robinhood, Schwab, Fidelity, Webull, Public, E\*TRADE, Vanguard and 30+
others), via one API and one consent flow.

## Status: `needs_approval`

Not `blocked`, but not a plain `ok` either -- see "Terms of service position"
below. **Do not enable public strategy pages built from SnapTrade data until
this is resolved with SnapTrade in writing.** Connecting an account and
computing a private track record is fine today; publishing it to other users
is the part the terms put in question.

## Terms of service position (invariant 10)

Checked 2026-09-26:

- **Developer Terms of Use**: https://snaptrade.com/developer-terms-of-use
- **End User Terms and Conditions**: https://snaptrade.com/terms-and-conditions

The Pay-as-you-Go and free-tier plans are explicitly licensed for
"testing, personal and commercial use" (Developer Terms, Limitations on
Use), which covers Koine operating as a commercial partner at all. The
clause that matters for Koine's *product*, not just its existence as a
customer, is in the same section:

> "Not to mine, re-sell, or re-package any End User data obtained via the
> SnapTrade API to third parties without the express written consent of
> both the End User and SnapTrade."

Koine's core feature is publishing a trader's verified, SnapTrade-derived
track record to other people (a public strategy page, a marketplace listing,
a follower's feed) and letting followers paper-copy signals derived from it.
That is a repackaging of End User data shown to third parties. The trader's
own consent is straightforward (the connect flow, and the disclosure below);
**SnapTrade's express written consent is not** -- nothing in the public
Developer Terms grants it generally, and the clause requires it from *both*
parties. Read narrowly, PUBLIC display of a SnapTrade-connected track record
needs a written sign-off from SnapTrade before it ships, the same shape as
`requires_partner_approval` on Coinbase (docs/platform_plan.md 3.1).

This reads more cautiously than the 2026-09-16 research
(`docs/research/2026-09-16-equities-brokers.md`) and platform_plan.md 3.3,
which called SnapTrade `ok` "under their agreement" without weighing this
clause specifically. `requires_partner_approval=True` is set in
`registry.PROFILES["snaptrade"]` pending that email; **flagged as a
contract_request / integration_note for whoever merges this branch: get a
written answer from SnapTrade (partnerships contact via the dashboard) on
whether a public, opt-in strategy page counts as "re-package to third
parties," and flip the flag once it does.** Private track-record computation
(the trader's own dashboard) is unaffected either way -- it is not shown to
a third party.

Read-only connections themselves are unambiguously fine: `connectionType:
"read"` is a first-class, self-serve option in SnapTrade's own Connection
Portal API, not something Koine is working around.

## Consent text (connect screen)

Shown before a trader is redirected to SnapTrade's Connection Portal, per
the Developer Terms' own requirement to "inform End Users that they are
using a third party service to connect their accounts" and SnapTrade's End
User Terms (which require the end user to "expressly authorize SnapTrade and
[the] Partner ... to access, process, store, use, and disclose your account
data"):

> You're about to connect a brokerage account through **SnapTrade**, a
> third-party account-data service Koine uses for brokers we can't reach
> directly (Robinhood, Schwab, Fidelity, Webull and others). Koine will
> request **read-only** access: the connection cannot place trades, move
> money, or change anything in your account. You'll pick your broker and log
> in on SnapTrade's own secure page -- your brokerage password is never seen
> by Koine. You can disconnect at any time from your Koine dashboard, which
> also deletes your SnapTrade registration. By continuing, you agree to
> [SnapTrade's End User Terms and Conditions](https://snaptrade.com/terms-and-conditions).

The hash of this exact text is stored with the consent record (P3 item 12,
`docs/implementation_plan.md`).

## Read-only proof

`ReadOnlyProof.AGGREGATOR_FLAG`. Koine requests `connectionType: "read"` on
every Connection Portal link it generates
(`connectors.snaptrade.generate_connection_portal_url`); there is no
separate scope-probe endpoint. `prove_read_only()` re-checks this nightly by
reading the resulting brokerage authorization's own `type` field via
`GET /authorizations/{id}` (`"read"` or `"trade"`) -- this is SnapTrade's own
record of what was granted, not an independent probe, and the methodology
page must say so plainly: **SnapTrade is the only tier where "verified
read-only" rests on the aggregator's word, not on a request Koine can make
that only a read-only credential would answer.** If a connection somehow
comes back `"trade"` (the trader answered a broker's own prompt differently,
or reconnected with a different type), it is suspended the same as any other
venue (P3 item 8).

## What a read-only credential exposes

The stored credential (`CredentialKind.AGGREGATOR`: `key_id` = SnapTrade
`userId`, `secret` = SnapTrade `userSecret`) grants read access to every
brokerage account under that SnapTrade user, for as long as the connection
stays active: account list, holdings/positions, balances, and the full
activity history (trades, dividends, transfers, splits...). It cannot place,
modify or cancel an order, move money, or change account settings --
enforced by SnapTrade at the `connectionType: "read"` level, not by Koine.
Deleting the SnapTrade user (`connectors.snaptrade.delete_user`, called on
disconnect) revokes it entirely.

## Rate limits

250 requests/minute at the commercial-partner ("customer") level, shared
across every trader Koine has connected through this one `clientId`
(https://docs.snaptrade.com/docs/ratelimiting , checked 2026-09-26). The
10-requests/minute **per-account** limit documented on the same page is
marked personal-tier only; commercial partners are not shown a separate
per-account cap there. **[VERIFY]** against the live commercial dashboard
once an account exists -- the outbound limiter (P2) should treat the
per-account number as a soft budget anyway, since polling every connected
account on a 5-minute tick will otherwise burn the 250/min pool fast once
Koine has more than a few dozen SnapTrade users.

Activities are also throttled at the data-freshness level, separate from
the request-rate limit: "cached and refreshed once a day, no intraday
transactions" (same source). Polling more often than daily wastes budget
without getting fresher data.

## History depth

`history_depth="all"`: SnapTrade backfills full available broker history on
first connect (subject to what the underlying broker itself retains and
returns), not a rolling window. `daily_resolution=True`: everything --
activities, holdings, balances -- refreshes at most once a day, so this
venue sits in the `daily_verified` tier (alongside IBKR Flex) rather than
the `reconciled` tier Kalshi/Alpaca get.

## Known gaps

- **No independent read-only proof.** See "Read-only proof" above -- stated
  on the methodology page, not just here.
- **Activity ids are not stable.** SnapTrade's own docs warn an activity
  `id` "can change if the broker reprocesses" it. Every fill and event here
  uses a SHA-256 content hash of the row's stable fields as `venue_fill_id`
  / `venue_ref` (`ref_is_content_hash=True`) instead of SnapTrade's `id`.
  Consequence: two genuinely identical same-day activities on one account
  (same type, date, symbol, units, price, amount) collapse into one row.
  Acceptable on the `daily_verified` tier; would not be acceptable on a
  `reconciled` one.
- **No cross-checked cash-flow sign convention.** The activities endpoint's
  `amount` field's sign (income positive / outflow negative) is read
  directly for events (dividends, interest, fees, contributions,
  withdrawals) but has not been confirmed against a real, signed payload
  (the reference docs describe the field without a full worked example).
  **[VERIFY]** on the first real account and cross-check against the
  broker's own statement before trusting this on a `reconciled`-tier venue;
  fine for `daily_verified` in the meantime. Asset-fill cash legs are
  computed independently from `price x units - fee` instead of trusting
  `amount`'s sign, which sidesteps this for BUY/SELL specifically.
- **Options and short positions are not modelled.** `OPTIONEXPIRATION`,
  `OPTIONASSIGNMENT`, `OPTIONEXERCISE` and `SUBSTITUTE_DIVIDEND` (stock-loan
  payments on a short position) activities are dropped by the connector, not
  fabricated into a wrong leg. Matches the equities-wide phase-2 deferral in
  `docs/implementation_plan.md` P5.
- **Position `symbol` nesting is [VERIFY].** The connector's `_symbol_of`
  handles the nested shape SnapTrade's own docs describe (a position-level
  wrapper around a universal `symbol` object) and a flat string as a
  fallback, but neither was confirmed against a captured real payload --
  see tests/fixtures/snaptrade/README.md.
- **Each registered user costs money.** SnapTrade bills per connected user
  ($1-2/user/month; free tier: 5 connections), independent of whether Koine
  ever polls that user again. `connectors.snaptrade.delete_user` exists
  specifically so disconnecting a Koine account also deletes the SnapTrade
  registration -- call it on every disconnect, not just a cleanup job, or
  Koine pays for abandoned connections indefinitely. Track this in an
  `integration_costs` row per connected user (`docs/platform_plan.md` M11
  item 5) so the running bill is visible on the admin dashboard.
- **Public strategy pages need the written approval above.** Repeated from
  "Status" for visibility: do not flip this venue's public-page gate on
  until that email exists.
