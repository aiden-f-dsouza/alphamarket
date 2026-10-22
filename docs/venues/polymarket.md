# Polymarket (international, polymarket.com)

Status: **DRAFT, `needs_counsel`**. This is the first terms document for
Polymarket (invariant 10 wants one for every connectable venue; the connector
has been connectable without it). The connector reads only unauthenticated
public endpoints, and for a trader's own account that read has been the basis
of the connect flow so far. Reading the current Terms of Use turned up one
clause (the "Data" clause, section 4.2) that may reach Koine as a company
whatever the use case, and a scraping clause whose reach over the public API
is unclear. Both are listed under "Open questions for counsel". Until counsel
has answered, treat the Terms position below as **unconfirmed**. The private
watchlist and the leaderboard discover page shipped for Polymarket ahead of
counsel on 2026-10-22 (founder decision, as for Hyperliquid): fly.toml `[env]`
sets `WATCH_VENUES = "hyperliquid,polymarket"`; drop `polymarket` there and
redeploy to vault both.

Checked: 2026-10-07 (the date every quote below was read). Written by an AI
assistant for Aiden and counsel; not legal advice. Anything not confirmed is
marked `[VERIFY]`. Connector: `src/alphamarket/connectors/polymarket.py`.
Profile: `registry.PROFILES["polymarket"]` (no `tos_doc` is set on the profile
yet, so no consent paragraph from this file is shown on the connect screen;
see "Consent text" below).

Scope. This file is about **international Polymarket** (polymarket.com, the
Polygon protocol, operated by Adventure One QSS, Inc.). **Polymarket US**
(polymarket.us, a CFTC-regulated exchange reached through brokers) is a
separate venue with separate terms. Koine does not support it, and its terms
were **not read** for this draft [VERIFY if it is ever added].

## What Koine reads, and from where

No credential exists. The "key id" is the account's proxy-wallet address
(`0x` plus 40 hex). Every call is an unauthenticated `GET`; no auth header is
ever sent (`_PublicHttp` in the connector). Base URLs are in
`config.POLYMARKET_BASE_URLS`.

| Purpose | Endpoint | Host |
|---|---|---|
| Fills (type TRADE) and redemptions (type REDEEM), 1000 per page, cursor paging | `GET /v2/activity?user=<addr>&type=...&sort_by=TIMESTAMP&sort_direction=ASC&start=...&cursor=...` | `data-api.polymarket.com` |
| Open positions (cost basis, size) | `GET /v2/positions?user=<addr>&status=OPEN&cursor=...` | `data-api.polymarket.com` |
| Does this address have a profile; read the bio for the claim code | `GET /public-profile?address=<addr>` | `gamma-api.polymarket.com` |
| Resolve a token to an outcome side (only when a row has no usable `outcome_index`) | `GET /markets/<condition_id>` | `clob.polymarket.com` |
| Market quotes for paper fills and marks | `GET /markets/<conditionId>`, `/price?token_id=&side=`, `/midpoint?token_id=` | `clob.polymarket.com` |

Notes.

- Everything above returns data that is public by design: Polymarket's own
  documentation says wallet positions and activity are readable by anyone
  without authentication ("Wallet Activity",
  https://docs.polymarket.com/trading/wallet-activity.md, read 2026-10-07,
  which shows `PublicClient` calls such as `listPositions()` and
  `listActivity()` taking a wallet address).
- **The connector reads Data API v2 (`/v2/activity`, `/v2/positions`).
  Polymarket's migration guide states "Data API v1 is retired on October 24,
  2026"** (https://docs.polymarket.com/migrate/data-api-v1-to-v2.md, read
  2026-10-08; the OpenAPI document is https://data-api.polymarket.com/v2/openapi.json).
  Ported 2026-10-08. What changed, and how Koine handles it:
  - Envelope: `{"data": [...], "pagination": {limit, offset, has_more,
    next_cursor}}` instead of a bare array. Paging follows `next_cursor`
    until it is null (v1: `limit`/`offset`, offset capped). Page size up to
    1000 (v1: 500). A cursor binds the sort direction; `/v2/activity` only
    sorts by `TIMESTAMP`, ordered `(block_timestamp, sequence_id)`.
  - Field names are snake_case: `conditionId` -> `condition_id`,
    `outcomeIndex` -> `outcome_index`, `usdcSize` -> `usdc_size`,
    `transactionHash` -> `transaction_hash`, `asset` -> `token_id`. Values
    are unchanged: a live comparison of v1 and v2 for a wallet with 113
    trades and 73 redeems (2026-10-08) gave identical fill ids, settlement
    ids, cash, timestamps and outcome indexes. **Dedupe ids are unchanged**
    (`transaction_hash:token_id:side:price:size#n` carries the same text the
    v1 `transactionHash:asset:...` did), so history already ingested is not
    re-ingested. `tests/test_polymarket_connector.py` pins a hand-spelled id.
  - Request params are snake_case (`sort_by`, `sort_direction`); `since`
    still maps to the inclusive `start`.
  - An omitted `start` now floors to three years back; a full sync sends
    `start=1`. [VERIFY that `start=1` returns rows older than three years;
    the only wallet checked has history from 2026.]
  - Positions: `size` -> `current_size`, `initialValue` -> `entry_cost_usdc`
    (the fee-exclusive basis, same numbers: 0 mismatches across 46 shared
    positions of two wallets), new `status` filter (default `OPEN`, a
    superset that includes settled-but-unredeemed winners). v1 hid positions
    under 1 share (its default size threshold); v2's dust floor is 0.1
    share, so up to 20 extra tiny positions appeared for a wallet that had
    146 on v1. Positions under 0.1 share stay hidden. [VERIFY that those
    never matter for reconciliation: the venue has no observable balance, so
    positions only feed the anchor at cost basis.] One position out of 146
    differed in size between two sequential calls (135.6 vs 165.6 shares),
    most likely a live trade between the calls. [VERIFY]
  - Fees: `usdc_size` is still the net cash with the fee included (checked
    on recent October trades as well as the September ones); there is no
    separate fee field on activity rows. Position rows add
    `entry_fees_usdc` (disclosure only).
  - Protocol V2 (pUSD collateral) is a separate migration track
    (https://docs.polymarket.com/migrate/polymarket-v2/overview.md); Koine
    reads only the Data API and did not need a change for it. [VERIFY that
    pUSD-era rows keep the same `usdc_size` semantics for fees on other
    wallets.]
- `/public-profile` returns a profile object. Koine reads only the `bio`
  (for the claim code) and otherwise only that the profile exists (404 means
  not a Polymarket account). Any name, pseudonym, image or linked social
  handle in that response is discarded: it is not stored, shown or used.
  [VERIFY the response field list; the Gamma API reference was not read.]
- No cash balance is observable for a wallet (`BalanceUnavailable`), so these
  accounts sit in the fills-only verification tier.

## Authentication, and what Koine holds

None. `ReadOnlyProof` is not applicable, there is nothing to leak, and
`check_write_scope()` returns `False` because no credential exists to write
with. Koine never places an order on Polymarket for anyone: tailing is
paper only (invariant 8), there is no order endpoint in the connector, and
no signing key of any kind is ever requested (invariant 9). Followers are
never pointed at Polymarket: no referral link, no "trade this" button.

## Ownership proof (profile bio claim code)

`OwnershipProof.PROFILE_CODE`. When an address is connected, Koine issues a
claim code (`venues.new_claim_code()`). The owner pastes it into their
Polymarket profile bio and calls `POST /venues/{id}/claim`;
`verify_claim(code)` fetches `/public-profile` and accepts when the code is a
substring of `bio`. Only a bio the owner can edit can contain the code, so
this proves control of the profile without any key or signature. Until the
claim succeeds the account is `claimed = false`: **an unclaimed account never
appears on any public page** (invariant 4; `public.publicly_visible`).

Known gap: a Polymarket profile is attached to a proxy wallet. If someone
other than the address owner can edit that profile's bio, they could claim
it [VERIFY how profiles are bound to wallets; assume wallet-signed].

## Terms-of-service position

Sources, all read 2026-10-07:

- Polymarket Terms of Use, "Effective as of August 11, 2026", by **Adventure
  One QSS, Inc.** ("A1", the Company), https://polymarket.com/tos . The page
  is a Google Doc iframe
  (https://docs.google.com/document/d/1N4aYlRcqaWCeKMID7vUSWJ1UPMvw_v6SP4cOFN8ZxR4,
  read as plain-text export). The section numbers below are the document's
  top-level numbers; sub-numbers (4.1, 4.2, 3.3) were reconstructed from the
  outline because the text export drops list numbering [VERIFY against the
  live page].
- API rate limits: https://docs.polymarket.com/quickstart/introduction/rate-limits
- Geographic restrictions: https://docs.polymarket.com/api-reference/geoblock
- No separate API terms, data licence or developer agreement was found for the
  public Gamma, Data or CLOB read endpoints [VERIFY with Polymarket; the
  docs site was searched via its `llms.txt` index, not exhaustively].

### 1. What the Terms cover

Section 1 says the Terms apply to the "Content Features" (news and
information on polymarket.com) and the "Technology Features" (the protocol and
the Platform), together the "Features", and that "BY ACCESSING, INTERACTING
WITH OR USING THE SITE, ANY FEATURE OR ANY OTHER INTERFACE (INCLUDING BY
CONNECTING VIA AN API, LINKING YOUR WALLET, ...)" you agree to be bound. So
**API access is expressly an access mode the Terms address**; the Terms are
not limited to the website.

### US persons and restricted jurisdictions

Section 1 (capitals in the original): "USE OF THE SITE, PLATFORM OR
TECHNOLOGY FEATURES **FOR TRADING** IS NOT PERMITTED BY PERSONS OR ENTITIES
WHO RESIDE IN, ARE LOCATED IN, ARE INCORPORATED IN, HAVE A REGISTERED OFFICE
IN, OR HAVE THEIR PRINCIPAL PLACE OF BUSINESS IN THE UNITED STATES OF
AMERICA, ALBERTA, AUSTRALIA, BELGIUM, BRITISH COLUMBIA, FRANCE, GERMANY,
HUNGARY, IRELAND, ITALY, ONTARIO, POLAND, QUEBEC, RUSSIA, SINGAPORE,
SLOVAKIA, SLOVENIA, TAIWAN, THAILAND, THE NETHERLANDS, NEW ZEALAND, THE
UNITED KINGDOM ... (ANY SUCH PERSON OR ENTITY ... A 'RESTRICTED PERSON')."
It extends to persons acting "ON BEHALF OF" or "DIRECTED, COORDINATED OR
CONTROLLED BY" a Restricted Person, and prohibits VPN circumvention.

Section 1 also says: "If you are in a Restricted Jurisdiction (as defined
below), you are only permitted to use the Content Features on the Site or any
other Interface and may not use the Site or any other Interface for any other
purpose and you may not access the Technology Features, including and in
particular the Platform."

Section 4.1 (Restricted Jurisdictions representation): no access, use or
trading "with the Contracts on the Platform" from a jurisdiction on the list
at https://docs.polymarket.com/api-reference/geoblock . That page (read
2026-10-07) lists as blocked: Iran, Syria, Cuba, North Korea, Crimea, Donetsk
and Luhansk, and puts the United States in "Regulatory-Restricted
Jurisdictions (Close-Only)": "Users can close existing positions but cannot
open new ones, on both the frontend and the API." Polymarket's signup copy
adds that traders "attest you are not a U.S. person, are not located in the
U.S." (site bundle, 2026-10-07).

What that means here, same shape as docs/venues/hyperliquid.md:

1. **The restriction is on trading, and on use of the Technology Features.**
   Koine does not trade, does not connect a wallet, and only reads public
   data. Whether reading the public APIs is "Content Features" use (allowed
   even to Restricted Persons) or "Technology Features" access is **not
   answered by the text** [VERIFY with counsel; see open question 2].
2. **Koine does not decide who may trade on Polymarket** and does not check
   creators' residency. A US-based creator with a Polymarket history would
   have broken Polymarket's terms, not Koine's; Koine should carry the line
   "Polymarket's international site is not available to US persons; Koine
   shows this record for information and paper copying only" on any
   Polymarket strategy page (integration item, not yet in the templates).
3. **Followers are never pointed at Polymarket** (invariant 8) and Koine never
   suggests a VPN. Koine's own corporate trading on Polymarket: do not.
4. **Koine is a US company with (expected) US users.** Paper-tailing a
   Polymarket address from the US is not a trade, but counsel should confirm
   it is not "use" of the Technology Features [open question 2].

### Data, scraping and commercial use (the risk area)

These bullets are in section 4.2 ("Your Responsibilities & Prohibited
Conduct", "You agree that you will not:"). Quoted briefly:

- "Use any data mining tools, robots, crawlers, or similar data gathering and
  extraction tools to scrape or otherwise remove data from the Site, any
  other Interface, or Features;"
- "Use any manual process to monitor or copy any of the material on the Site,
  any other Interface, or Features or for any other unauthorized purpose
  without our prior written consent;"
- "Harvest or otherwise collect information from the Site, any other
  Interface, or the Features for any unauthorized purpose;"
- "Use the Site, any other Interface, or Features in any manner that could
  disable, overburden, damage, or impair the Site ..."
- **The Data clause**: "Access or use any data, content, or information
  contained on our Site, any Interface or Features directly or through an API,
  or any other means, **including on-chain**, whether in raw, derived,
  aggregated, or anonymized form (the 'Data') if you are (i) a non-retail,
  professional entity that engages in capital markets activities (e.g.,
  brokerage, market making, proprietary trading, index calculation, or ETF
  issuance), including but not limited to broker-dealers, futures commission
  merchants, registered investment advisors, hedge funds, investment
  managers, proprietary trading firms, commodity pool operators, **financial
  technology companies**, banks, clearing houses, and exchanges or an
  affiliate of any such entity (each, a 'Capital Market Client'), or (ii) a
  market data distributor, in each case unless otherwise agreed to in writing
  by us;"
- "Sell, resell, sublicense, redistribute, or otherwise commercially exploit
  the Data to any Capital Market Client or market data distributor, unless
  otherwise agreed to in writing by us;"
- Closing sentence of that list: "the Company reserves the right to grant
  access to public on-chain infrastructure and the Company's builder
  program."

Reading, without drawing a conclusion:

- Section 3.3 reserves the right to set and change rate "Limits"; Koine stays
  inside the published limits (below) through its own floor and the outbound
  limiter.
- Whether the public Gamma/Data/CLOB APIs are the "Site, Interface or
  Features" for the scraping bullets is unclear. They are documented and
  offered for integration (docs.polymarket.com is titled "Build on the
  world's largest prediction market ... access real-time market data"), and
  section 1 contemplates "connecting via an API", which points toward
  "permitted use", but nothing says so for third-party consumers. [VERIFY]
- **The Data clause is the real issue.** It is not limited to scraping, it
  reaches data "through an API" and "on-chain", "derived" and "aggregated"
  forms, and its list of prohibited users includes "financial technology
  companies" and "registered investment advisors". Koine is a financial
  technology company that derives and publishes statistics from this data;
  that is what a track-record platform is. Whether Koine is a "non-retail,
  professional entity that engages in capital markets activities" depends on
  how the qualifier "engages in capital markets activities (e.g., brokerage,
  market making, proprietary trading, index calculation ...)" is read:
  Koine computes performance indices of individual traders (arguably "index
  calculation") but does not trade or broker. **Do not assume it is outside
  the clause.** Counsel decision needed, and a request to Polymarket for
  written agreement is the clean fix [open questions 1 and 3].
- The prohibition also names redistribution to Capital Market Clients and
  market data distributors. Public Koine pages are readable by anyone,
  including such entities. [open question 3]

### Conclusion

**Unresolved.** Koine's read is of public data via documented public APIs, and
Polymarket does not appear to require a licence for individual reads, but the
Terms as written (Data clause, scraping bullets, API access listed as an
access mode) leave real doubt for a commercial fintech that republishes
derived numbers. Recommended handling until counsel answers:

1. Do not add any new Polymarket-based feature (the private watchlist, the
   leaderboard discover page) to production.
2. Existing connect flow (a trader proving their own address): legal read
   needed on whether it can stay on; the strongest argument is that the
   owner consents and the data is the owner's own activity.
3. Write to Polymarket (builder/partnerships) describing the use and ask for
   written permission, and for the intended rate limits. Hold the flag
   `requires_partner_approval` as a possible gate (`registry.PROFILES`) if
   Polymarket asks for approval; it is `False` today.

## Consent text (connect screen)

Draft for the connect screen. Not shown today because the profile has no
`tos_doc`; setting `tos_doc="docs/venues/polymarket.md"` is the code change
that would make `consent.py` pick this section up (and the exact text becomes
hashable proof under invariant 10). Do not set it until counsel clears the
terms position above.

> Koine will read the public activity of your Polymarket wallet (trades,
> redemptions and open positions) from Polymarket's public data API and
> publish performance statistics computed from it. There is nothing secret
> to give us: Koine never asks for a private key, seed phrase or signature,
> and it cannot trade or move funds. To prove the wallet's Polymarket
> profile is yours, you put a short Koine code in your profile bio; your page
> stays hidden until the code is found. Polymarket does not make your cash
> balance visible, so your record is shown as "fills only" and cannot be
> reconciled against a balance. You confirm that your use of Polymarket
> complies with its terms and the laws that apply to you.

## Watchlist (third-party addresses)

New use, proposed in docs/copy_strategies_plan.md section 2. **Live in
production since 2026-10-22, ahead of counsel** (founder decision; vault by
removing `polymarket` from `WATCH_VENUES` in fly.toml). A logged-in Koine user pastes a public Polymarket
address that is not a Koine member; Koine ingests the address's public fills
through the same connector and shows that user the usual statistics, and lets
them paper-tail it.

Position, from Koine's side:

- **Private only.** The strategy page is a private page for the user who
  added the address (`/watch/{id}`, 404 for everyone else). It never appears
  in the marketplace, feed, landing page, JSON API, sitemap, the ticker or
  `/u/{handle}`; the visibility guard covers every public query.
- **No identity linking, at all.** No ENS lookup, no social handles, no
  Polymarket profile name, pseudonym or picture, no linking an address to a
  Hyperliquid address or to anything else. The page says "Public address,
  not a Koine member. We do not know who this is." The watch flow reads the
  profile only to check it exists; it never stores the name or image. A user
  who already knows who the address belongs to is outside Koine's data.
- **Opt-out for the address owner.** Someone who can prove the address (the
  same bio-code claim above) can ask Koine to stop: Koine records the
  address in `watch_optouts`, refuses new watches of it and stops existing
  ones (plan section 2, "Prerequisites"). Linked from `/security` and the
  footer. The address's already-ingested rows stay (append-only); the
  address is dormant and unpolled.
- **Paper only.** No order is placed anywhere (invariant 8); the watching
  user is never sent to Polymarket.
- **One shared ingestion** per address, rate-limited as for any account, with
  a per-user cap on watched addresses and a global cap on tracked addresses.
- Data is the same public data any Polymarket page or block explorer shows;
  but Koine would **collect and store it, derive scores from it and show it
  to a user who has no relationship with the address**. Privacy law may treat
  a wallet address (and the history attached to it) as personal data
  [VERIFY, GDPR/CCPA/state laws; open question 5].

Terms questions specific to the watchlist: the Data clause (`4.2`) applies in
the same way as above, and the scraping bullets apply with more force
(collecting data on accounts whose owners have not engaged with Koine at all).
The original recommendation was to wait for counsel and, ideally, Polymarket;
the founder chose to ship ahead of both (2026-10-22). The questions stay open. The Hyperliquid side has a
cleaner terms position (docs/venues/hyperliquid.md).

## Leaderboard (watch discover)

Copy strategies step 7 (`discover.py`, `/watch/discover`). **Live in
production since 2026-10-22, ahead of counsel**: fly.toml `[env]` sets
`WATCH_VENUES = "hyperliquid,polymarket"` and `WATCH_DISCOVER_ENABLED`. Remove
`polymarket` from `WATCH_VENUES` to vault watches and discover together.
Endpoint checked 2026-10-14.

Endpoint. `GET https://data-api.polymarket.com/v2/leaderboard` (Data API v2;
the v1 `/v1/leaderboard` retires with the rest of v1 on 2026-10-24), no
authentication. Documented in https://data-api.polymarket.com/v2/openapi.json
(`get_leaderboard`, tag "boards") and verified live. Parameters used:
`time_period` (`day` | `week` | `month` | `all`, which map one to one onto the
discover periods), `limit` (at most 1000), and `cursor` for deeper pages (a
cursor pins board, window and category, so it is sent alone). Defaults kept:
`category=overall`, `sort_by=PNL`.

What is read. `user_id` (the wallet, lowercase), `rank` (Polymarket's
competition rank: ties share a rank and the next skips) and `pnl` (USDC, a
JSON float, rounded to 6 dp and stored as integer micros). Per the OpenAPI,
`day`/`week`/`month` pnl is the marked equity change net of flows (it moves
with unrealised marks) while `all` is realised-only lifetime pnl; the two are
the venue's own figures and are labelled as such. The board has no roi and no
account value, so the account-value floor does not apply, the roi sort is not
offered for Polymarket, and its table has no ROI or Account value column.

What is dropped. `user_name`, `profile_image`, `x_username` (a linked X
handle) and `verified` are discarded inside the connector and never stored
(`LeaderboardEntry` has no field for them); the board's `volume` is dropped
too, because it is in shares, not dollars.

Wallet stats (enrichment). For the rows that are KEPT in a snapshot (after
opt-outs and the top `WATCH_DISCOVER_ROWS` cut, not the 2x over-fetch) the
connector (`PolymarketMarketData.leaderboard_details`, called from
`discover.refresh`) reads three more public Data API v2 endpoints, per
wallet, no authentication (OpenAPI read 2026-10-22, fixtures
`tests/fixtures/polymarket/{user_volume,value,user_stats}_20261022.json`):

| Endpoint | Read | Shown as |
|---|---|---|
| `GET /v2/user-volume?user=&start=&end=` | `data.volume_usdc` (USD; `volume` is shares and is ignored) | "Volume (UTC days)" |
| `GET /v2/value?user=` | `data.value`: open positions marked to market, USDC | "Positions value" |
| `GET /v2/user-stats?user=` | `data.trades` (distinct markets traded, lifetime), `data.biggest_win` (USDC, best single resolved win, lifetime), `data.join_date` (epoch s or null) | "Markets", "Biggest win", "Since" (month and year, UTC) |

Window. Volume is tracked in whole UTC days, so day, week and month are the
last 1, 7 and 30 UTC days ending today (today included): `start` is 00:00 UTC
of day today-N+1 and `end` is the refresh time (the venue floors both to
their UTC day); `all` sends no bounds. This is why the column header says "UTC
days": it is not a rolling 24 h or 7 d window and need not match the board's
own window. Positions value is NOT an account value: Polymarket cash is not
observable (see the connector docstring), so the column says positions only.
`value` and `user-stats` do not depend on the period, so each address is
fetched once per refresh and reused across the four periods (a per-refresh
cache; a failed fetch is not retried within the refresh).

Dropped from these responses: `views`, `all_time_pnl` and every other field,
`proxy_wallet`, and `data: null` (unknown user) just leaves the fields empty.
No name, username, image, X handle or `verified` flag is read or stored.

What is stored. Per period, the top `WATCH_DISCOVER_ROWS` (default 200) by
pnl after removing opted-out addresses (`watch_optouts`), in
`leaderboard_snapshots` (a cache replaced on each refresh), with the wallet
stats merged into each row as integer micros (`volume_micros`,
`positions_value_micros`, `biggest_win_micros`), `markets_traded` and
`active_since` (epoch seconds, UTC). Rows stored before enrichment existed
lack the new keys and still render. The connector asks the board for twice
`WATCH_DISCOVER_ROWS` (one request with `limit=400`), so opt-outs cannot leave
a snapshot short.

Cadence and rate cost. Every `WATCH_DISCOVER_REFRESH_MINUTES` (default 60):
one board request per period, four in all, about 220 KB each at the full
`limit=1000` (about 90 KB at 400). Enrichment, with N = 200 kept rows and U
unique addresses across the four periods (U <= 4 x 200 = 800): 4 x 200 = 800
`user-volume` calls plus 2 x U `value` and `user-stats` calls, so at most
800 + 1,600 = 2,400 requests an hour on top of the 4 board requests (2,404
in all). With overlapping boards U is usually well under 800, so about 1,000
requests is typical. The v2 general
limit is 800 requests per 10 s (rate-limits page, read 2026-10-14); the
connector spaces calls 0.11 s apart (`_PublicHttp`, about 9 a second, 90 per
10 s), so the worst case takes about 4.5 minutes of a job and stays far
under it. The refresh heartbeats the job's lease (300 s) before each period. Calls go through `_PublicHttp` (0.11 s floor, bounded `Retry-After`
retries) and the outbound limiter hook. Failure policy: a failed enrichment
call leaves its fields empty and is logged; a 429 that survives the retries,
or an exhausted outbound budget, stops enrichment for the rest of the
refresh (the limiter is told) and the rows done so far stand. The snapshot is
always stored: enrichment never loses a board. (A 429 on the board request
itself still leaves the venue for the next tick.)

Terms (for counsel). Everything in "Data, scraping and commercial use" above
applies, with more force: the board lists thousands of wallets whose owners
have no relationship with Koine, and Koine stores a ranked copy of it. The
leaderboard is published by Polymarket for everyone, and Koine keeps only
addresses and Polymarket's own pnl, never the names, handles or pictures that
come with it. [VERIFY with counsel; shipped ahead of the answer on 2026-10-22: open questions 1, 3 and 5 cover it, plus
whether storing a ranked copy of the board is "collecting" or "copying the
material" under section 4.2.]

## Rate limits

From https://docs.polymarket.com/quickstart/introduction/rate-limits (read
2026-10-07). Requests are "throttled (delayed/queued) rather than immediately
rejected", per the page.

| API | Limit | Koine uses |
|---|---|---|
| Gamma | general 4,000 / 10 s; `/markets` 300 / 10 s; `/events` 500 / 10 s | `/public-profile` (not itemised on the page [VERIFY]; falls under general presumably) |
| Data API v1 | general 1,000 / 10 s; `/trades` 200; `/positions` 150 / 10 s | retired 2026-10-24; no longer called |
| Data API v2 | general 800 / 10 s; `/v2/trades` 300; `/v2/positions` 200 / 10 s | `/v2/activity` (general), `/v2/positions` (200 / 10 s) |
| CLOB | general 9,000 / 10 s; `/price` 1,500; `/book` 1,500 / 10 s | `/markets/<id>` (general), `/price`, `/midpoint` (`/midpoint` is not itemised [VERIFY]) |

The limits are per IP [VERIFY: the page does not say per what in the part
read]. Koine's connector keeps at least 0.11 s between calls per HTTP client
instance (`_PublicHttp.MIN_INTERVAL`, about 9 per second per instance), and
retries 429 and 5xx with `Retry-After` or exponential backoff (4 attempts).
The venue profile declares no outbound `RateBudget` for Polymarket today
(compare Hyperliquid), so several workers at once are limited only by that
per-instance floor. [VERIFY and add an IP-scope budget in the profile before
the watchlist or any leaderboard fetching adds volume.] A full account
history sync costs roughly one `/v2/activity` page of 1000 per 1000 fills
for each of TRADE and REDEEM, plus `/v2/positions`. The v2 OpenAPI says
429 and 503 carry `Retry-After`, which `_PublicHttp` honours.

## History depth and mapping

Summarised from the connector docstring (VERIFIED on public payloads
captured 2026-09-06 on v1 and re-recorded on v2 2026-10-09):
`/v2/activity?type=TRADE` gives size, price and `usdc_size` (net cash, fee
included, so the fee is the difference); `type=REDEEM` is the settlement
event (payout $1 per winning token, 0 for the losing side); `/v2/positions`
gives `entry_cost_usdc` (cost basis) and `outcome_index` (0 = first outcome
= "yes"). Ticker is the `condition_id`. Fill identity is
`transaction_hash:token_id:side:price:size` plus an ordinal for identical
rows in one transaction (the same text as the v1 id). History depth is whatever the API pages
back to; no retention limit is documented [VERIFY on a long-lived account].

## Known gaps

- Terms position unresolved (above).
- Data API v1 retirement on 2026-10-24: done, the connector reads v2 (see above).
- No cash balance (fills-only tier).
- Polymarket's perps product and Polymarket US are not covered. The Perps
  section of the docs (docs.polymarket.com/perps) exists; Koine does not read
  it.
- Fee formula assumes the 5% taker rate seen in 2026-09-06 payloads
  [VERIFY against the current fee schedule; docs/methodology.md].

## Open questions for counsel

1. **The Data clause (section 4.2).** Is Koine, a US financial-technology
   company that derives and publishes track-record statistics from Polymarket
   activity data, a "Capital Market Client" (the list names "financial
   technology companies" and "registered investment advisors") or a "market
   data distributor"? If so, reading even public, on-chain data through the
   API needs Polymarket's written agreement. Is a request to Polymarket the
   right route, and who should send it?
2. **Do reads of the public APIs count as "use of the Technology
   Features" or of the "Content Features"?** Section 1 says a person in a
   Restricted Jurisdiction (the US is one) may use only the Content Features,
   and may not "access" the Technology Features "including and in
   particular the Platform". Koine, a US entity with US users, only reads and
   does paper-copying; is that outside the restriction? Same question for
   Koine's users who watch or tail a Polymarket address from the US.
3. **Scraping and republishing.** Do the "data mining tools, robots, crawlers
   ... scrape" and "harvest or otherwise collect information" bullets apply to
   documented public API endpoints? Does publishing derived numbers on a
   public Koine strategy page (readable by Capital Market Clients) fall under
   "commercially exploit the Data to any Capital Market Client or market data
   distributor"?
4. **Polymarket's right to change terms and limits** (sections 3.1 and 3.3):
   is a contractual dependence on public endpoints we cannot control
   acceptable, and should the product degrade gracefully (it does:
   fills-only, jobs back off)?
5. **Privacy and third-party watching.** Does ingesting, storing and scoring
   an identifiable-in-principle wallet's history for a user (watchlist) raise
   GDPR/UK GDPR or CCPA obligations (notice, access, deletion, legitimate
   interest), given the owner never dealt with Koine? Is the proof-based
   opt-out (`watch_optouts`) sufficient, or is a notice/deletion path needed
   beyond it? Is "no identity linking" enough, given that address plus
   behaviour can be identifying?
6. **Polymarket's own use for a US company.** Independent of the data
   question: Koine must not trade on Polymarket from the US (Restricted
   Person). Confirm no feature (house accounts, partnership with a creator)
   is allowed to create that exposure. Polymarket US terms must be read and
   assessed separately before it is added as a venue.
7. **The consent text.** Is the draft above sufficient for the owner-connect
   flow, and does the attestation "your use of Polymarket complies with its
   terms and the laws that apply to you" put the right burden on the
   creator, given the US-person exclusion?
