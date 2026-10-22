# Pacifica

Status: **watch only, not cleared for prod.** The terms were found and read
on 2026-10-22 and do **not** ban API use, automated access or third-party or
commercial use of public account data, so the connector is built. They do name
the API, exclude US persons **and US-incorporated entities**, and list
"unauthorized access to the API" as a suspension ground, so the position stays
`PENDING COUNSEL` (onchain_perps_plan.md counsel Q2) and preflight keeps
Pacifica out of prod `WATCH_VENUES` until that clears (S9).

Checked: 2026-10-22. Connector: `src/alphamarket/connectors/pacifica.py`.
Profile: `registry.PROFILES["pacifica"]` (`requires_partner_approval=True`: no
wallet connect, the watch path only).

## Terms-of-service position

Position: PENDING COUNSEL

Sources (all read in full on 2026-10-22):

- **Pacifica Terms of Use**, "Last updated on June 11, 2026", made by
  **SkyLake Global Corp** (Panama), https://app.pacifica.fi/term (the "Terms"
  link in the app footer; the marketing site pacifica.fi links to the app).
  Read from the page's rendered text (25.7 KB). The Privacy Policy is at
  https://app.pacifica.fi/policy (not needed for the position).
- API documentation, https://docs.pacifica.fi/api-documentation/api (REST
  endpoints, rate limits, websocket), index at
  https://docs.pacifica.fi/llms.txt. The docs carry no separate terms.

What the terms cover (verbatim): "These Terms of Use ('Terms') govern your
access to and use of: (a) the website-hosted user interface available at
app.pacifica.fi and related subdomains (the 'Website Interface'); (b) the
Pacifica mobile application(s) (the 'Mobile App'); and (c) any related
application programming interfaces ('API') (collectively, the 'Interface')."
Unlike Hyperliquid's terms, which cover the app only, **the API is in scope**.
The protocol itself is described as "a decentralized, permissionless set of
smart contracts" and "The Interface is not the exclusive means of accessing the
Protocol".

Clauses that touch Koine's use (a public, unauthenticated read of an address's
history through the documented REST API):

- **No clause bans reading public account data through the API, automated or
  scripted access, or third-party, commercial or data-aggregation use.** The
  words "scrape", "crawl", "bot", "commercial", "redistribute" and "data
  license" do not appear. The only automation-adjacent text is §3.
- **§3 API Usage** (the whole section): "3.1 Availability. You acknowledge and
  accept that the API may not be available at all times. ... We reserve the
  right to, without prior notice, terminate the support of the API and to limit
  or suspend the functionality thereof. 3.2 Representations and Warranties.
  When using the API, you represent and warrant that: You will not use the API
  in any way that causes, or may cause, damage to the Interface or impairment
  of the availability or accessibility thereof; You will not use the API in any
  way which is unlawful, illegal, fraudulent, or harmful, or in connection with
  any unlawful, illegal, fraudulent, or harmful purpose or activity; You will
  not use the API to copy, store, host, transmit, send, use, publish, or
  distribute any material which consists of, or is connected to, any spyware,
  computer virus, Trojan horse, worm, keystroke logger, rootkit, or other
  malicious computer software." None of that describes reading public data
  inside the published limits (see "Rate limits").
- **Suspension ground** (Access and Restrictions): "The Company may suspend or
  terminate your access, without prior notice, if it detects or reasonably
  suspects: Impersonation of the Interface or its components; Unauthorized
  access to the API; Price manipulation or market interference; Any other
  activity deemed suspicious or potentially harmful". "Unauthorized access" is
  undefined. The public endpoints Koine reads need no key or signature, and the
  docs offer an optional "API Config Key" only to raise the rate limit, so we
  read the endpoints as authorized for anyone, but that is our reading.
- **§2.7** forbids reverse engineering "the Mobile App"; the leaderboard
  endpoint below is not in the docs but is the document the app's own page
  loads, and Koine did not reverse engineer the Mobile App to find it.

Conclusion: the terms do **not** ban API use or commercial/third-party use, so
the build proceeds. Because they name the API and bar US entities, the same
shape as Lighter's terms, `requires counsel` before any prod use. A written
answer from Pacifica (for example the optional API Config Key for a data
use) also clears it. [VERIFY with counsel: whether read-only use of public
endpoints by a US company is "using the Interface" under the Restricted
Territories clause; counsel Q2 and Q4.]

## The US-person restriction and what it means for Koine

"Restricted Jurisdictions": "Our services were not developed for, and are not
available to, persons or entities who reside in, are located in, are
incorporated in, or have a registered office or principal place of business in
the United States of America, Canada, the United Kingdom, China, Russia,
Ukraine, Cuba, Iran, Venezuela, Syria, North Korea, the Philippines, or any
other jurisdiction subject to comprehensive economic sanctions ... (collectively,
'Restricted Territories')." Access and Restrictions then says the
acknowledgements apply "By accessing or using the Interface, including by
connecting a third-party non-custodial digital wallet", and "Geolocation
Enforcement. The Interface employs IP-based geolocation blocking to restrict
access from Restricted Territories. You agree not to circumvent this
restriction using VPNs, proxies, or any other technology." §2.5 repeats it as
a prohibited activity.

What that means here, the same as Hyperliquid's section in
docs/venues/hyperliquid.md with one difference (the API is named):

1. **No circumvention, ever.** Koine reads from its own Fly `iad` egress. The
   public API answered US IPs with 200 on 2026-10-22 (smoke script,
   `scripts/pacifica_smoke.py`), and the docs list a `403 Forbidden:
   restricted region` code. If that code ever appears, the connector fails
   closed (HTTP 403 raises) and nobody works around it: no proxy, no non-US
   region (§2.5).
2. **Koine does not decide who may trade on Pacifica** and does not check
   creators' residency. A watched page is evidence of what an address did, not
   a statement that its owner was allowed to do it.
3. **Followers are never pointed at Pacifica.** Paper only (invariant 8), no
   referral link, no "trade this on Pacifica" button. The US notice used for
   Hyperliquid appears on watched pages too: "Pacifica's app is not available
   to US persons; Koine shows this record for information and paper copying
   only." [integration item for the templates, not this branch]
4. **Our own test accounts.** Nobody at Koine should fund a Pacifica wallet to
   generate fills if they are a Restricted Person. Every fixture here came
   from public third-party addresses and carries synthetic addresses.
5. **Counsel Q2** (onchain_perps_plan §5): paper-copying and profiling
   third-party addresses on a venue whose terms exclude US persons, for US
   users. Because the API is named, ask whether read-only use of the public
   endpoints by a US company falls under the clause.

## Consent text (connect screen; kept for a future connect, invariant 10)

> Koine will read your Pacifica account's public history (trades, funding,
> deposits, withdrawals and balances) from Pacifica's public API and publish
> performance statistics computed from it. We never ask for a private key,
> seed phrase or API key, and we cannot trade or move funds. To prove the
> address is yours, you sign a Koine message in your Solana wallet; signing is
> not a transaction and costs nothing. Your page stays hidden until the
> signature checks out. You confirm that your use of Pacifica complies with
> its terms and the laws that apply to you. Pacifica pays small interest on
> idle balances that its balance history does not list, so a cash figure Koine
> reconstructs can sit a few cents below Pacifica's own.

Watch only: no connect screen exists (`requires_partner_approval=True`).

## Read-only proof

`ReadOnlyProof.NONE`: no credential is held, only the public Solana address.
`check_write_scope()` returns False; `prove_read_only()` returns
`read_only=True` with the details `credential: none (public address)`,
`can_trade: False`, `can_withdraw: False`. `check_connection()` reads
`GET /account` and refuses an address Pacifica has never seen (`404 Account not
found`, recorded as a fixture).

## Ownership proof

`OwnershipProof.SIGNED_MESSAGE`. The trader signs this exact message with the
wallet that holds the address (Phantom or Solflare "Sign Message" signs the raw
UTF-8 bytes) and pastes the signature (base58, base64 or 0x hex of 64 bytes):

```
Koine ownership claim
Venue: Pacifica
Address: <the base58 address, exactly as stored>
Code: <the account's Koine claim code>
Signing this message proves you control this address. It is not a transaction: it cannot trade, move funds or approve anything.
```

`verify_claim` checks `addresses.verify_ed25519(address, message, signature)`.
There is no sub-account or vault indirection: a Pacifica sub-account is its own
address with its own key (docs, "Create Subaccount": "Both the main account
and subaccount must sign to establish the relationship"), so each address signs
for itself [VERIFY with a real sub-account owner]. The claim code is the nonce
and is never read from the user (replay protection, as for Hyperliquid).

## What the "credential" exposes

Nothing beyond what is already public. The address alone lets anyone read the
account's trades, funding, balance events and positions.

## Rate limits

Docs, https://docs.pacifica.fi/api-documentation/api/rate-limits (read
2026-10-22): "Pacifica uses a credit-based rate limiting system with a
60-second rolling window." Unidentified IP: **100 credits per 60 s**
(an API Config Key gives 300; Koine has none). Standard request 1 credit;
"Heavy GET requests: 3-12" for an unidentified IP. HTTP 429 when exhausted.
Websocket: max 100 concurrent connections per IP, 20 subscriptions per channel
per connection; a connection is closed after 60 s of silence or 24 h.

Measured costs from the `ratelimit` response header on 2026-10-22 (the header
is in tenths of a credit, `r=910` of `q=1000` after one call means 9 credits):

| Endpoint | Cost | `RateBudget` name |
|---|---|---|
| `/leaderboard` (2.2 MB, 6,952 rows) | 9 (charged 10) | `leaderboard` |
| `/trades/history`, `/funding/history`, `/account/balance/history` page | 9, whatever the `limit` (100, 500, 4000 all 9) (charged 10) | `trades_history`, `funding_history`, `balance_history` |
| `/funding_rate/history` page | 9 (charged 10) | `funding_rate_history` |
| `/kline` | 2 (charged 3) | `kline` |
| `/account`, `/positions`, `/info`, `/info/prices`, `/book`, `/info/fees` | 1 | default |

Because a history page costs 9 whatever its size, the connector asks for
`limit=1000` per page (the server caps a page at 4,000 rows), so a long
backfill spends credits per thousand rows. One sync of a quiet account is three
heavy pages (about 30 credits) plus `/info` once per process. The budget is
IP-scoped and shared by every Pacifica account Koine polls, so the watch
cadence must stay slow (about 3 accounts per minute in steady state; the
discover refresh needs 10 credits per hour).

The docs' "403 CloudFront" FAQ: a GET with an empty body is rejected with 403.
The connector sends no body and no `Content-Length` on any GET (a test pins it).

## History depth

`history_depth = "all"` [VERIFIED on a 13-month-old account: its balance
history starts with its first deposit, and trades and funding start with its
first trade]. Cursor-paged, newest first (`next_cursor`, `has_more`), no
retention limit documented. `/funding_rate/history` pages 4,000 rows of hourly
rates (about 5.5 months) per request.

## Mapping (connector summary)

Perp fill (`GET /trades/history?account=`, `venue_fill_id = history_id`):

- `side`: `open_long` and `close_short` are buys, `open_short` and
  `close_long` are sells. `FILL_BASE ±amount` of `"<SYM>-PERP"` at the symbol's
  lot-size decimals (`/info`), price = `price` (the fill price; `entry_price`
  is the position's average entry after the fill, which the docs label the
  other way round).
- **`pnl` is net of the fee.** Verified on two accounts. Fixture close_short of
  200 SOL at 103.66 against entry 107.490396: gross (107.490396 - 103.66) x 200
  = 766.0792, fee 7.04888, gross - fee = 759.03032, reported `pnl` 759.03031;
  an open fill reports `pnl = -fee`. So the `REALIZED_PNL` leg is `pnl + fee`
  (766.07919) and the `FEE` leg is `-fee`; together they sum to the venue's
  `pnl` exactly. The 1e-5 residual is the venue rounding `entry_price` to 6 dp
  (200 x 5e-7 = 1e-4 at most), not a missing term: on the second account
  (5.52 SOL, entry 217.307935, price 217.31) gross 0.0113988 against
  `pnl + fee` 0.0114.
- `event_type` `fulfill_taker` / `fulfill_maker` become liquidity
  `taker` / `maker`; any other value raises `ValueError` (never defaults).
- `cause` (docs, Get Trade History and the `account_trades` stream): `normal`,
  `market_liquidation`, `backstop_liquidation`, `insolvency_liquidation` and
  `settlement` (ADL or settlement) are fills. Only `normal` appears in the
  fixtures. For the others the docs state `pnl` as "PnL generated by the trade
  event" and Koine books it the same way [VERIFY on a recorded liquidation: if
  Pacifica charges a liquidation fee outside `fee`, reconcile will quarantine
  the account, visibly]. Any other cause (`game_settlement`, `fulfill_rfq`, an
  unknown) becomes an `UnsupportedActivity` flag and the row is skipped, so an
  unknown value never silently changes the ledger. A spot fill (a `spot_fee`,
  or a `spot` instrument such as `SOL-USDC`) is flagged the same way.
- Funding (`GET /funding/history`): one `FUNDING` event per row,
  `venue_ref = history_id`, delta = `payout` (signed cash effect: negative when
  paid. Verified: a long on a positive rate pays, and the balance history shows
  the same signed amount).
- Balance events (`GET /account/balance/history`, default, no
  `include_trades`): `deposit` and `deposit_release` are `DEPOSIT`, `withdraw`
  is `WITHDRAWAL`, `subaccount_transfer` is `TRANSFER_IN` / `TRANSFER_OUT` by
  sign, `payout` (affiliate and rewards) is `TRANSFER_IN`. No row carries an id
  (docs and fixtures: `amount`, `balance`, `pending_balance`, `event_type`,
  `created_at`), so `venue_ref =
  balance:{created_at_ms}:{event_type}:{amount}:{balance}`, which are all
  venue values, so it is deterministic; it is not a content hash of unstable
  data and `ref_is_content_hash` stays False [flagged for the main session:
  that flag is reserved for the daily tier].
- **Deposits with a pending part.** A large deposit is split: the credited part
  is a `deposit` row at once and the rest sits in `pending_balance` until a
  `deposit_release` row. Fixture: a 300,000 deposit shows as `deposit`
  19,833.236692 (pending 280,166.763308) and later `deposit_release`
  280,166.763308 (pending 0). `amount` is the change to `balance`, so each row
  is booked once, when it reaches `balance`; the pending part is not cash until
  its release.
- `trade`, `funding` and the liquidation event types appear in the balance
  history only with `include_trades=true`, which Koine never requests; if one
  appears anyway it is skipped (the other streams carry it).
  `loan_interest_payout` becomes an `INTEREST` leg if it appears.
- Balances (`GET /account` + `GET /positions`): cash `USDC` =
  `balance + sum(margin of isolated positions)`; positions `"<SYM>-PERP"`
  signed by side (`bid` +, `ask` -) with `entry_price`;
  `equity_usd_micros = account_equity`. Verified on four live accounts on
  2026-10-22: cross-margin `account_equity - balance` equals the sum of
  `side x amount x (mark - entry)` over the positions to the cent, and with
  isolated positions `account_equity = balance + uPnL + isolated margin`
  (951.571824 + 2,052.792522 - 9.879224 = 2,994.485122 against 2,994.485167),
  so `balance` excludes isolated margin, which is still the trader's cash.
  Non-empty `spot_balances` are an `UnsupportedActivity` flag and are left out
  of the vector.
- `balance_after_atoms` is **never set** (see Methodology notes).

## Streaming

`wss://ws.pacifica.fi/ws` (docs "Websocket"; checked live 2026-10-22). Mode
FILLS, `stream_users_per_ip = 90` (100 connections per IP minus headroom).
Subscribe `{"method":"subscribe","params":{"source":"account_trades","account":
"<address>"}}`; the venue acks `{"channel":"subscribe","data":{"source":
"account_trades","account":"..."}}` and sends **no snapshot**, so a reconnect
gap is closed by the poller (history ids dedupe). Frames
`{"channel":"account_trades","data":[{"h","i","I","u","s","p","o","a","te",
"ts","tc","f","n","t","li","it"}]}` carry the same fields as the REST row under
short names (`h` history_id, `p` price, `o` entry price, `a` amount, `te`
event type, `ts` side, `tc` cause, `f` fee, `n` pnl, `t` ms); the stream parser
renames them to the REST shape and calls the same `to_fill`, so ids and legs
are identical. An error is `{"code":400,"err":"Invalid subscription
parameters.","t":...}`. Heartbeat: send `{"method":"ping"}` every 30 s, answered
`{"channel":"pong"}`; a connection is dropped after 60 s of silence.

## Watchlist (third-party addresses)

The same private-only position as docs/venues/hyperliquid.md "Watchlist": an
address and its public history, shown only to the Koine user who added it, no
identity linking, paper only, owner opt-out by signature.

- **Opt-out by signature**: the ed25519 claim above, through
  `watch_optouts`.
- **Pacifica's own privacy flag.** The leaderboard carries `privacy_mode` per
  address (94 of 6,952 rows true on 2026-10-22). The history API **still serves
  an address with the flag on** (verified on the fixture), so Koine enforces it
  itself: such rows are dropped from every snapshot and their normalized
  addresses go to `PacificaMarketData.private_addresses()` for the once-per-
  refresh sweep (S5), and `owner_private(address)` answers one address at watch
  time (S3). While the flag is on the address is treated as opted out; because
  the flag can be turned off, the watch is removed without writing
  `watch_optouts`.
- **Limit.** `GET /account` carries no privacy flag (fixture), so an address
  absent from the leaderboard cannot be checked: its flag is invisible to Koine
  and the signed opt-out is its only route.
- **Sub-accounts** are separate addresses and watch independently; their
  transfers to and from the main account appear as `subaccount_transfer`
  external flows on each side. `POST /account/subaccount/list` needs the main
  account's signature, so sub-accounts are never enumerated [VERIFY a real
  sub-account].

## Leaderboard (watch discover)

`GET https://api.pacifica.fi/api/v1/leaderboard`, no authentication, **not in
the API docs** (found by the research pass; the app's own leaderboard page
loads it). Undocumented and unversioned: it can change or vanish without notice
[VERIFY: ask Pacifica whether it is meant for third parties]. One response of
2.2 MB holds all 6,952 rows (`has_more: false`), `cache-control: public,
max-age=300` behind CloudFront, 9 credits.

Per row: `address`, `username`, `pnl_1d`, `pnl_7d`, `pnl_30d`, `pnl_all_time`,
`volume_1d/7d/30d/all_time`, `equity_current`, `oi_current`, `privacy_mode`.
**Dropped inside the connector:** `username` (a user-chosen name; no field in
`LeaderboardEntry`, nothing identity-like is stored, no lookup from an address
to a person) and every row with `privacy_mode: true` (see Watchlist). Mapped:
`pnl_1d/7d/30d/all_time` to `day/week/month/all` pnl, `volume_*` to volume,
`equity_current` to account value, `roi = None` (the board has none). The board
has no rank: rank = position after sorting a period's public rows by pnl
descending (ties keep document order). Rows below the discover account-value
floor, or with no volume in the period, are ranked but not offered, as for
Hyperliquid. Figures are Pacifica's own, rounded to 6 dp (`leaderboard_figure`)
and never ledger money.

Cadence and cost: one GET per discover refresh (10 credits charged, hourly), so
well inside the IP budget. Terms (for counsel): the board is something the app
itself publishes to every visitor, but Koine reads the file directly and
offers its addresses as watch candidates. See counsel Q2 and Q4.

## Known gaps

- **Loan interest is not ingested.** Pacifica lends idle balances and credits
  hourly `loan_interest_payout` rows, which the default balance history does
  not return; they appear only with `include_trades=true`, which interleaves
  every trade and funding row (a 13-month-old account had 4,588 interest rows
  against 5 others) at 9 credits per page. Measured on that account: interest
  totalled 0.037029 USDC on a 4,999 balance (7e-6 of it), so the reconstructed
  cash sits that much under the venue's `balance`, well inside the 0.1%
  reconcile tolerance. A very large lending balance could exceed it; if
  reconcile ever flags that, ingest the interest stream behind a flag.
- Trades that share one millisecond across a page boundary are ingested
  newest-page-first, so their ledger insertion order can be reversed relative
  to the venue's; only the order of two same-millisecond fills on one symbol is
  affected (valuation orders by timestamp).
- Spot balances and spot fills are flagged, not ledgered (unified margin spot
  collateral is out of scope).
- Liquidation and settlement cause semantics are taken from the docs, not
  recorded fixtures.
- `/info` has no category field: instruments with the `rfq` execution mode are
  the real-world-asset markets on 2026-10-22 (stocks, FX, metals, energy) and
  are marked `copyable=False` until counsel Q3 answers. An unknown symbol is
  also not copyable.

## Methodology notes (for docs/methodology.md)

**Pacifica realized PnL and fees.** Pacifica reports one `pnl` per fill that is
already net of that fill's fee. Koine books the fee as a `FEE` leg (negative)
and the realized profit as `pnl + fee` (a `REALIZED_PNL` leg), so the two legs
sum to Pacifica's `pnl` exactly. Example: closing 200 SOL short from an average
entry of 107.490396 at 103.66 reports `pnl` 759.03031 and `fee` 7.04888, so
Koine books REALIZED_PNL +766.07919 and FEE -7.04888 (sum +759.03031).

**Pacifica cash.** The reconciled cash is Pacifica's `balance` (before
settlement of unrealized profit and loss) plus the margin held in isolated
positions, because the venue's own equity formula is `balance + unrealized PnL
+ isolated margin`. The ledger replays deposits, withdrawals, sub-account
transfers, realized PnL net of fees, and funding.

**Pacifica running balance is not chained.** `balance` on a balance-history row
is the account's cash right after that row **including** trading, funding and
interest booked since the previous row (fixture: after a 175,000 deposit the
balance was 450,539.272678, after the next 19,833.236692 deposit 469,665.487632,
a difference that trading explains). It is a post-event figure but its legs
arrive on separate page streams, newest page first, and the ledger's
`balance_chain_breaks` checks in ingestion order, which cannot be time order
here; the venue also rounds `amount` and `balance` independently, so a merged
time-ordered replay breaks by 1e-6 on rows (checked on 4,593 merged rows of one
account). So Koine never sets `balance_after_atoms` for Pacifica and relies on
the whole-vector reconcile against `GET /account`.

**Pacifica deposits.** Each `deposit` or `deposit_release` is booked when it
reaches `balance`; the pending part of a split deposit is not cash until its
release.
