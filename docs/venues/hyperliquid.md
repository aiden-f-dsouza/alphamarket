# Hyperliquid

Status: **ok** (reading public account data needs no permission from anyone;
see the US-person section before any US-facing marketing of Hyperliquid
creators).

Checked: 2026-09-26. Connector: `src/alphamarket/connectors/hyperliquid.py`.
Profile: `registry.PROFILES["hyperliquid"]`.

## Terms-of-service position

Sources:

- Hyperliquid Terms of Use, "Last updated on June 15, 2026", made by
  **Hyperliquid Corp.**, https://app.hyperliquid.xyz/terms (read from the
  app's terms bundle on 2026-09-26).
- API docs, https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
  (info endpoint, rate limits), read 2026-09-26.

What the terms cover. They govern "this website-hosted user interface
('Interface'), available at app.hyperliquid.xyz". Hyperliquid itself is
described as "a decentralized, permissionless, and community-driven
blockchain" that the Company "does not own, control, or operate", and
§1.1 says "The Interface is not the exclusive means of accessing
Hyperliquid."

Third-party read access. No clause addresses it, permits it or forbids it.
Every account's fills, funding, transfers and balances are public chain
state, served without authentication by the public info endpoint
(`POST https://api.hyperliquid.xyz/info`), which Hyperliquid documents for
exactly this use ("fetch information about the exchange and specific
users"). Koine never uses the Interface, never holds a key, and never asks
the trader for one. The only clause that touches automated reads is §3.1.8
("bots, scripts, or other automated methods to interact with the Interface
in ways that exceed reasonable usage, bypass rate limits ..."); it applies
to the Interface, and Koine in any case stays inside the published API
limits through the outbound limiter (`RateBudget` in the profile).

Conclusion: **ok**. Nothing to apply for; `requires_partner_approval=False`.

## The US-person restriction and what it means for Koine

§1.6 of the Terms: "The Interface is not available to 'Restricted Persons.'
For the purposes of these Terms, Restricted Persons include: (a) persons or
entities who reside in, are located in, are incorporated in, or have a
registered office in the United States of America or Ontario, Canada; (b)
... jurisdictions subject to applicable economic and trade sanctions ...;
and (c) citizens of Restricted Territories, regardless of their location.
Restricted Persons are strictly prohibited from accessing or using the
Interface described herein." §3.1.5 also prohibits circumvention (VPNs,
false residency statements).

What that means here:

1. **Reading and displaying is not "using the Interface".** Koine reads
   public chain data through the documented API, the same data any block
   explorer shows. The restriction binds people who use the app; it does
   not make an address's history private.
2. **Koine does not decide who may trade on Hyperliquid and does not check
   creators' residency.** The consent text (below) has the creator confirm
   their own use of Hyperliquid complies with its terms and their local law.
   A creator's page is evidence of what an address did, not a statement
   that the creator was allowed to do it.
3. **Followers are never pointed at Hyperliquid.** Copying is paper only
   (invariant 8), there is no "trade this on Hyperliquid" button or referral
   link, and a Hyperliquid strategy page must carry (integration item, not
   yet in the templates) the line: "Hyperliquid's app
   is not available to US persons; Koine shows this record for information
   and paper copying only." Koine never suggests a VPN or any other way
   around §1.6/§3.1.5.
4. **Our own test account.** docs/implementation_plan.md suggests funding a
   wallet to generate our own fills. If the person doing that is a
   Restricted Person, don't: the connector and the smoke script work
   against any public address, and every fixture here came from third-party
   public addresses.
5. **Before US-facing marketing of Hyperliquid creators**, get counsel to
   confirm point 1 and the notice in point 3 [VERIFY with counsel; not yet
   listed in regulatory_reference.md].

## Consent text (connect screen)

> Koine will read your Hyperliquid account's public history (trades,
> funding, transfers and balances) from Hyperliquid's public API and publish
> performance statistics computed from it. We never ask for a private key,
> seed phrase or API wallet, and we cannot trade or move funds. To prove the
> address is yours, you sign a Koine message with your wallet; signing is
> not a transaction and costs nothing. Your page stays hidden until the
> signature checks out. You confirm that your use of Hyperliquid complies
> with its terms and the laws that apply to you. Hyperliquid only serves
> your most recent 10,000 fills, so older trades cannot be verified
> individually; they are covered by the starting balance we record today.

## Read-only proof

`ReadOnlyProof.NONE`: nothing to prove, because Koine holds no credential,
only the public 0x address. `prove_read_only()` always returns
`read_only=True` and records `userRole` (user, vault, subAccount, agent,
missing) in its details so the nightly check notices if the address changes
kind. `check_connection()` refuses "missing" (no Hyperliquid account) and
"agent" (an API wallet, whose own state is always empty; the error names
the master address to connect instead).

## Ownership proof

Hyperliquid has no profile bio, so the Polymarket claim-code flow does not
fit. Instead the trader signs this exact message with the wallet using
EIP-191 `personal_sign` (`claim_message(address, code)`):

```
Koine ownership claim
Venue: Hyperliquid
Address: <lower-case 0x address>
Code: <the account's Koine claim code>
Signing this message proves you control this address. It is not a transaction: it cannot trade, move funds or approve anything.
```

The claim code is the nonce (one per account, issued by `venues.py`).
`verify_claim(encode_claim_proof(code, signature))` recovers the signer and
accepts it when it is the address itself, the `master` of a sub-account
(`userRole`), or the `leader` of a vault (`vaultDetails`); neither a
sub-account nor a vault has a key of its own. Gaps: smart-contract wallets
(EIP-1271) cannot sign this way; the contract has no `OwnershipProof` value
for a signature yet, so the profile uses `PROFILE_CODE` to route through the
claim flow (contract request).

## What the "credential" exposes

Nothing beyond what is already public. The address alone lets anyone read
the account's entire fill, funding and transfer history and its open
positions. Publishing a Koine page makes that history easier to find and
ties it to the creator's chosen display name; the consent text says so.

## Rate limits

Per IP, shared by every account Koine polls (docs "Rate limits and user
limits", 2026-09-26): 1,200 weight per minute. `l2Book`, `allMids`,
`clearinghouseState`, `orderStatus`, `spotClearinghouseState` and
`exchangeStatus` weigh 2; `userRole` 60; every other info request 20. Extra
weight +1 per 20 items returned by `userFills`, `userFillsByTime`,
`userFunding`, `fundingHistory` (and other listed history types), and +1
per 60 items for `candleSnapshot`; the connector charges whole blocks
(`floor(items / 20)`) through `throttle_after` [VERIFY rounding]. WebSocket
user subscriptions are limited to 10 unique users per IP (relevant to P7).
Budget per sync of one account: fills 20 + up to 100, funding 20 + up to
25, ledger 20, metadata 20 per perp dex traded + 20 for spot, balances about
20 + 2 + 20 + 2 per perp dex (11 dexes on 2026-09-26) + metadata.

## History depth

- Fills: only the **10,000 most recent** per address (`history_depth =
  "10k_fills"`); `userFillsByTime` returns at most 2,000 per response. The
  anchor balance taken at connect covers everything older.
- Funding and non-funding ledger updates: time-paginated, 500 rows per
  response; no retention limit is documented [VERIFY on a long-lived
  account].
- Candles: the most recent 5,000 per interval (13+ years of daily closes).

## Mapping (connector summary)

- Perp fill: `FILL_BASE ±sz` of `"<COIN>-PERP"` (HIP-3: `"dex:COIN-PERP"`)
  at the coin's szDecimals; `REALIZED_PNL closedPnl` in the dex collateral
  (USDC on the first dex); `FEE -fee` in `feeToken` (fee already includes
  `builderFee`; a negative fee is a maker rebate and shows as a positive FEE
  leg).
- Spot fill (`"@107"`, `"PURR/USDC"`, resolved through `spotMeta`):
  `FILL_BASE ±sz` of the base token at its weiDecimals, `FILL_QUOTE ∓sz×px`
  exactly, `FEE` in `feeToken` (the base token on buys).
- Funding: one `FUNDING` event per `(time, coin)`, id `f"{time}:{coin}"`.
- Ledger updates: deposit/withdraw (bridge, with the $1 withdrawal fee as
  FEE), sends and transfers to/from other addresses (TRANSFER_IN/OUT plus
  fee and HYPE gas fee legs), transfers between your own spot and perp
  balances (CONVERT, no legs), vault deposit/withdraw/distribution/
  commission, staking moves and borrow/lend supply/withdraw/borrow/repay
  (TRANSFER_IN/OUT), liquidations (LIQUIDATION with no legs: the liquidation
  fills carry the cash, including a backstop's forfeited margin), unknown
  types (ADJUSTMENT with no legs, surfaced by reconciliation).
- Balances: spot totals plus, in standard mode, each perp dex's
  `accountValue - Σ unrealizedPnl`; positions as `"<COIN>-PERP"` with
  `entry_price`; unified-account and portfolio-margin users use spot totals
  only (the docs say perp dex states are not meaningful for them).

## Streaming (P7)

`HyperliquidFillStream`: public websocket `wss://api.hyperliquid.xyz/ws`,
subscription `{"type":"userFills","user":<address>}`, no credential. Mode
FILLS: each `data.fills` element is the same object REST `userFillsByTime`
returns (docs `WsFill`), normalized by the same `to_fill` and stored once
whichever side sees it first (`tid` is the id). The first frame is a
snapshot of recent fills (`isSnapshot: true`), which closes any gap a
reconnect left; duplicates are skipped. The core pings every 30 s (the
server drops a client silent for 60 s). The envelope and the fill keys were
checked with a one-off public probe on 2026-10-04 (snapshot of 30 fills,
keys identical to the REST rows).

**10 unique users per IP** across websocket user subscriptions (docs "Rate
limits and user limits"): `VenueProfile.stream_users_per_ip = 10`, enforced
by slot leases per `EGRESS_IP`, most-followed creators (active paper
allocations) first; everyone else is polled (docs/worker.md "Streaming").
[VERIFY] the exact error text when the cap is hit (the parser treats any
error frame mentioning "users" as the cap and backs off for the cool-off).

## Watchlist (third-party addresses)

New use, proposed in docs/copy_strategies_plan.md section 2 (not live). A
logged-in Koine user pastes a public Hyperliquid address that is **not a Koine
user**; Koine reads that address's public history through the same connector
and shows the pasting user the usual statistics, and lets them paper-tail it.
Read 2026-10-07 for this section; the rest of the doc keeps its 2026-09-26
check.

What is read. Exactly what the creator flow reads, nothing more: the public
`info` endpoint (`POST https://api.hyperliquid.xyz/info`: `userFillsByTime`,
`userFunding`, `userNonFundingLedgerUpdates`, `clearinghouseState`,
`spotClearinghouseState`, `userRole`, metadata) with an address and no
credential. Unlike the creator flow, no ownership proof is requested and no
signature exists, so `claimed = false` for these accounts; they are owned by a
system user, never listed, and covered by the same visibility guard as any
unclaimed account (invariant 4).

Position:

- **Private only.** The account's page is `/watch/{id}`, visible to the user
  who added the address and nobody else (404 otherwise). It is absent from the
  marketplace, feed, landing page, JSON API, sitemaps, `/u/{handle}` and the
  shelved ticker. If the address's owner later connects and claims it through
  the normal flow, it can be listed through the normal path, with their
  consent text.
- **No identity linking.** No ENS or other name lookups, no social handles, no
  matching of an address to a Polymarket address or to any venue account or
  person. The page says "Public address, not a Koine member. We do not know
  who this is." Koine stores the address and the venue data only.
- **Opt-out.** The owner can stop Koine tracking an address by proving it with
  the existing EIP-191 claim (`claim_message(address, code)` above, where the
  signer is the address itself, the `master` of a sub-account or the `leader`
  of a vault). Koine records the address in `watch_optouts`, refuses new
  watches of it, ends existing watches and allocations on it, and stops
  polling. Rows already imported stay (append-only, invariant 3).
- **Paper only.** No order, no key, no builder-code or referral link, and the
  watching user is never sent to Hyperliquid (invariants 8 and 9). The US
  notice from point 3 above appears on watched pages too.
- Smart-contract wallets cannot sign the opt-out message (EIP-1271 gap noted
  under "Ownership proof"), so their owners need a manual route [open
  question 2].

Terms re-check (2026-10-07). The Terms of Use bundle served at
https://app.hyperliquid.xyz/terms still reads "Last updated on June 15,
2026". Searching it for API, scraping, automated, data and commercial
language finds only: §3.1.8 (bots or scripts "to interact with the Interface
in ways that exceed reasonable usage, bypass rate limits, cause
denial-of-service conditions"), §4.2 ("Any content, information, or data made
available through the Interface may be incomplete, outdated, or subject to
other inaccuracies"), and §12.5 (notices). There is **no clause on API use,
third-party reading of account data, data licensing, redistribution or
commercial use of public chain data**, and none that mentions the info
endpoint. Conclusion unchanged: the Terms govern the Interface; reading
public chain state through the documented API is not addressed, so the
position is **ok but unconfirmed for the new use**. [VERIFY with counsel: that
silence is enough for collecting third-party addresses at scale, and whether a
US company doing so is "using" Hyperliquid in the sense of §1.6.] The API
documentation (https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api)
carries no separate terms either [VERIFY; page read for limits only].

What is different from the creator flow, and why it matters:

1. **The owner has not agreed to anything.** A creator consents to
   publication (the consent text); a watched address owner did not. The
   mitigations are privacy by default (nothing public), no identity linking,
   the opt-out, and the cap on watched addresses per user and globally.
2. **Privacy law.** An address plus a full trading history may be personal
   data; Koine stores and scores it for third parties. [VERIFY with counsel;
   open question 3.]
3. **Volume.** The creator flow polls a handful of consenting accounts; the
   watchlist can add many. Every account polled draws from the same per-IP
   budgets below.

Rate limits and streaming (docs "Rate limits and user limits", read
2026-10-07): REST shares **1,200 weight per minute per IP** with every other
Koine account (weights unchanged from "Rate limits" above), so watch accounts
are polled on a slower cadence than tailed ones and in total are capped
(settings; plan section 2 "Scheduling and limits"). Websocket user
subscriptions are limited to **10 unique users per IP** (the page also lists
10 connections, 30 new connections per minute, 1,000 subscriptions and 2,000
messages per minute). Koine's slot leases (`stream.py`, per `EGRESS_IP`) are
ordered so that **claimed creator accounts always outrank watch accounts; a
watch account streams only when a slot is spare** (planned change to
`streamable_accounts`: "is watch" becomes the first sort key). Everything
not streamed is polled. If slots are short, watch accounts lose first. The page also states address-based limits
("1 request per 1 USDC traded cumulatively since address inception"), but
those govern the exchange (order) endpoint and an address Koine only reads is
unaffected [VERIFY].

Open questions for counsel (watchlist):

1. Is collecting, storing and scoring a non-user's public Hyperliquid history
   for private display to Koine users acceptable on the Terms' silence, for a
   US company (§1.6 excludes US persons from the Interface; §3.1.5 forbids
   circumvention)?
2. Is the signed-message opt-out an adequate answer to "you are tracking me",
   including for smart-contract wallets that cannot sign it?
3. Does a wallet address with its trading history count as personal data under
   GDPR/UK GDPR/CCPA for this use, and what notice or deletion path (beyond
   the opt-out and no-identity-linking rule) is required?
4. Does anything change for the leaderboard discover page? (Endpoint, rate
   cost and what is read: "Leaderboard (watch discover)" below; the terms
   question is restated there.)

## Leaderboard (watch discover)

Copy strategies step 7 (`discover.py`, `/watch/discover`). Shipped behind
`WATCH_DISCOVER_ENABLED` (fly.toml `[env]`); turn that off to vault it.
Checked 2026-10-14.

Endpoint. `GET https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`
(`/Testnet/leaderboard` for testnet), no authentication. This is the document
the app's own leaderboard page (app.hyperliquid.xyz/leaderboard) loads: its
JavaScript bundle fetches `https://stats-data.hyperliquid.xyz/${chain}/leaderboard`.
It is **not** in the API documentation, which names the stats-data host only
for builder-fill files (`/Mainnet/builder_fills/...`), so it is an
undocumented, unversioned file and can change shape or disappear without
notice [VERIFY: ask Hyperliquid whether it is meant for third parties]. It is
a static JSON file on S3 behind CloudFront (`Server: AmazonS3`, `X-Cache: Hit
from cloudfront`), regenerated every few minutes. Size on 2026-10-14:
**38.7 MB** uncompressed (gzip is not offered), **47,097 rows**.

What is read. Per row: `ethAddress`, `accountValue` (USD, now) and
`windowPerformances` for `day`, `week`, `month` and `allTime` (each `pnl`,
`roi`, `vlm`; decimal strings). `allTime` is stored as the period `all`. The
document has no rank and no order; Koine ranks each period by pnl descending
over every row (ties keep document order), as the app does. The app also
hides vault and treasury addresses from its table; Koine does not, so its
ranks can sit a few places below the app's, and protocol accounts (large
holders with zero volume) can appear near the top.

What is dropped. `displayName` (a user-chosen name, set on about 3% of rows)
and `prize` are discarded inside the connector; `LeaderboardEntry` has no
field for them, nothing identity-like is stored, and no lookup is ever made
from an address to a person (plan: never link an address to an identity).

What is stored. One snapshot per period (`leaderboard_snapshots`, a cache
replaced on each refresh): rows with an account value of at least
`WATCH_DISCOVER_MIN_ACCOUNT_VALUE` (default $10,000), minus opted-out
addresses (`watch_optouts`), keeping the top `WATCH_DISCOVER_ROWS` (default
200) by pnl plus the top 200 by roi. Figures are rounded to 6 dp and stored
as integer micros; they are Hyperliquid's own numbers, labelled as such, not
Koine-computed ones. The connector streams the document and keeps only the
candidate rows (twice the snapshot size per period), so a refresh holds about
30 MB, not the ~300 MB a whole-document parse takes.

Cadence and rate cost. One GET per refresh, every
`WATCH_DISCOVER_REFRESH_MINUTES` (default 60), plus one at worker start when
the snapshots are older than that: about 0.9 GB of transfer a day from
CloudFront. The file is not served by the info API, so the documented
1,200-weight-per-minute IP budget does not obviously apply; Koine charges it
anyway as one ordinary info request (weight 20, no per-item weight) through
the same outbound limiter, circuit breaker and 429 path as every other
Hyperliquid call. A busy or failing fetch waits for the next tick.

Terms (for counsel). The Terms of Use govern the Interface and say nothing
about the API or this file (see "Terms re-check" above). The leaderboard is
something the Interface itself publishes to every visitor, but Koine reads
the file directly, ranks tens of thousands of third-party addresses and
offers them to its users as watch candidates. [VERIFY with counsel: does
reading the Interface's own data file count as using the Interface (§3.1.8
"automated methods ... in ways that exceed reasonable usage"), and does
browsing a leaderboard of non-users change the watchlist answers above
(questions 1 and 3)? Hourly, one request, is our position on "reasonable".]

## Known gaps

- Only the latest 10,000 fills are verifiable individually.
- Portfolio-margin accounts: borrowing across HYPE/BTC/USDC/USDT is not
  modelled beyond the borrowLend flows; reconciliation will flag them.
- Borrow/lend interest is folded into the flow amounts (treated as a flow,
  not a return) [VERIFY `interestAmount` on a repaid loan].
- Vault accounts: the outflow of a depositor withdrawal is taken as
  `netWithdrawnUsd + commission` [VERIFY with a vault account].
- Withdrawal fee: `usdc` is taken as the bridged amount with the $1 fee on
  top [VERIFY on our own account].
- HYPE staking rewards (`delegatorRewards`) are not ingested; staked HYPE is
  outside the balance vector.
- Market data lists the first perp dex and spot; HIP-3 dexes are priced by
  their own coin names but not listed by `fetch_instruments`.
- Spot tokens keep Hyperliquid's names (`UBTC`, `UETH`, `UXPL`): Unit-bridged
  tokens are not merged with `BTC`/`ETH`.
