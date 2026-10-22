# Lighter

Status: **watch only, not cleared for prod.** The connector merges and runs
on the demo; Lighter stays out of prod `WATCH_VENUES` until counsel question
Q1 (docs/onchain_perps_plan.md section 3 "Counsel questions") or a written
answer from Lighter is on file. Preflight enforces this through the marker
in the next section. `requires_partner_approval=True`: no connect path.

Checked: 2026-10-22. Connector: `src/alphamarket/connectors/lighter.py`.
Profile: `registry.PROFILES["lighter"]`. Research notes:
`docs/research/2026-10-22-lighter.md`. Plan: `docs/onchain_perps_plan.md`
"W-L".

## Terms-of-service position

Position: PENDING COUNSEL

Sources:

- Lighter Terms of Service, "last updated" 2025-12-29, made by Elliot
  Technologies, Inc., https://lighter.xyz/terms (read 2026-10-22 by the
  research session; quotes below are from that reading).
- API docs, https://apidocs.lighter.xyz (environments, rate limits, account
  types, API keys), read 2026-10-22.

What the terms cover. Unlike Hyperliquid's (Interface only), Lighter's terms
name the API: "These Terms of Service cover our website, Interface,
Protocol, API, the LIT Token ... (collectively, the 'Services')."

Third-party read access. §5 (API usage) forbids use "that causes, or may
cause, damage to the Services or impairment of the availability", unlawful
use and malware. There is **no scraping, caching, redistribution or
commercial-use clause**, and no data licence either way. Suspension grounds
include "Impersonation of the frontend interface" and "Unauthorized access
to the API". §7: interaction with the Protocol is "solely ... through your
public digital wallet address"; "Any personal or other data that you may
make available in connection with the Interface or Protocol may not be
private or secure."

The API docs mark REST `trades` and `pnl` as needing auth ("`auth` is
required for master accounts and sub accounts"), though the servers did not
enforce it on 2026-10-22. Reading them without auth could be argued to be
"unauthorized access to the API", so Koine does **not** call them: fills
come from the public block explorer only (founder decision 2026-10-22: the
REST accelerator stays off until Q1 clears; see "Mapping"). Every endpoint
Koine calls is one the docs list without an auth requirement, or the
explorer, whose purpose is to show this data.

Mitigating facts for counsel: the data is the public state of an L2 whose
explorer exists to show it; Lighter's own docs give restricted regions a
read-only socket ("Clients in restricted regions can still read market
data"); Lighter lists third-party data resellers (0xArchive, Tardis.dev) and
a public explorer (Litscan) in its docs; nothing restricts reuse of data.

Conclusion: **PENDING COUNSEL** (counsel Q1, verbatim in the plan): does
read-only public-data use (explorer logs and the documented public REST
endpoints) by a US company fall under the US exclusion below and the
"unauthorized access to the API" ground? Should Koine ask Lighter for a
Builder or data arrangement (the founder's to send)? Either answer in
writing clears the marker above; replace it with the conclusion and the
date.

## The US-person restriction and what it means for Koine

At the top of the terms, in capitals: "OUR SERVICES WERE NOT DEVELOPED FOR,
AND ARE NOT AVAILABLE TO PERSONS OR ENTITIES WHO RESIDE IN, ARE LOCATED IN,
ARE INCORPORATED IN, OR HAVE A REGISTERED OFFICE OR PRINCIPAL PLACE OF
BUSINESS IN THE UNITED STATES OF AMERICA, CANADA, THE UNITED KINGDOM, ...".
A user represents they are not "located in, a resident of, or organized in
the United States" (a "Prohibited Person"). The trading websocket refuses US
IPs (HTTP 400, code 20558, "You are accessing Lighter from a restricted
jurisdiction").

What that means here:

1. **The exclusion names the Services, and the Services include the API.**
   Koine is a US company reading the API. That is one notch broader than
   Hyperliquid's interface-only exclusion, hence counsel Q1. Until it is
   answered, Lighter is not in prod `WATCH_VENUES`.
2. **Koine never trades, never holds a key, never asks for one.** It reads
   public data through the read-only paths Lighter documents for restricted
   regions (the `?readonly=true` socket) and its public explorer.
3. **Followers are never pointed at Lighter.** Copying is paper only
   (invariant 8): no "trade this on Lighter" button, referral, builder or
   integrator link. Watched pages and any future creator page carry the same
   US notice as Hyperliquid's: "Lighter's services are not available to US
   persons; Koine shows this record for information and paper copying
   only." Koine never suggests a VPN or any way around the restriction.
4. **No own test account.** Every fixture comes from third-party public
   accounts (tests/fixtures/lighter/README.md).
5. **Fly `iad` is US egress.** The smoke script is the egress check; a
   datacenter IP may be treated differently from the residential IP the
   research used [VERIFY from Fly before any prod entry].

## Consent text (connect screen)

Kept for a future creator connect (invariant 10); no connect path exists
today (`requires_partner_approval=True`).

> Koine will read your Lighter account's public history (trades, transfers,
> deposits, withdrawals and balances, across your sub-accounts) from
> Lighter's public block explorer and API, and publish performance
> statistics computed from it. We never ask for a private key, seed phrase
> or API key, and we cannot trade or move funds. To prove the address is
> yours, you sign a Koine message with your wallet; signing is not a
> transaction and costs nothing. Your page stays hidden until the signature
> checks out. Lighter does not publish your realized PnL or funding per
> trade, so Koine reconstructs both from your public trades and the
> market's published hourly funding, and says so on your page. You confirm
> that your use of Lighter complies with its terms and the laws that apply
> to you.

## Read-only proof

`ReadOnlyProof.NONE`: nothing to prove, because Koine holds no credential,
only the public 0x address. `prove_read_only()` always returns
`read_only=True`. `check_connection()` refuses an address Lighter has no
account for (`accountsByL1Address` answers HTTP 400, code 21100).

## Ownership proof

The owner signs this exact message with EIP-191 `personal_sign`
(`claim_message(address, code)`), the shared Koine claim format:

```
Koine ownership claim
Venue: Lighter
Address: <lower-case 0x address>
Code: <the Koine claim or opt-out code>
Signing this message proves you control this address. It is not a transaction: it cannot trade, move funds or approve anything.
```

`verify_claim(encode_claim_proof(code, signature))` accepts it only when
the recovered signer is the address itself. A Lighter sub-account is an
index of the same L1 address, so there is no master or vault indirection
(unlike Hyperliquid). Gap: smart-contract wallets (EIP-1271) cannot sign
this way, as on Hyperliquid. The opt-out (`/watch/opt-out`) uses the same
signature.

## What the "credential" exposes

Nothing beyond what is already public: the address alone lets anyone list
its account indexes and read every trade, deposit, withdrawal and transfer
on the explorer.

## Rate limits

https://apidocs.lighter.xyz/docs/rate-limits (2026-10-22). Two hosts, two
IP-scoped buckets (`lighter.RATE_BUDGETS`), each giving the other host's
endpoints cost 0:

- API (`mainnet.zklighter.elliot.ai/api/v1`): Standard (unauthenticated)
  accounts get **60 requests per rolling minute** per IP, unweighted
  (weights only apply on Premium/Builder budgets). Every API request costs 1.
- Explorer (`explorer.elliot.ai/api`): **90 weight per minute**; `accounts/*`
  weigh 2, so 45 log pages a minute.
- Over the limit: HTTP 429 or 405 and a static 60 s firewall cooldown;
  both become `RateLimited` (Retry-After, else 60 s).
- Websocket (read-only): 255 connections, 500 subscriptions and 500 unique
  accounts per connection, 5,000 unique accounts per IP, 200 client
  messages per minute. `stream_users_per_ip = 200` (headroom under 255).

Cost of one sync of one address with `n` indexes: fills `1 + n` requests
when caught up (the index list, one explorer page per index; more pages
when more than 90 rows arrived), the same again for events (plus one
`accountsByL1Address` when a transfer names an index not on the list), plus one
`fundings` request per perp market with an open position (or with trades
since the last sync). Balances (nightly reconcile): `1 + n`. A backfill of a
very active account is about two explorer pages per 90 rows per stream: a
200,000-row market maker takes about 2 x 2,200 pages, i.e. about 100
minutes of the explorer bucket, plus `fundings` at one request per market
per 30 days of history. The leaderboard refresh: at most 4 windows x
(3 pnl + 2 roi pages) = 20 API requests per hour.

## History depth

`history_depth = "all"`. The explorer's per-index log reaches back to the
account's first action (offset paging; 760300's full 145 rows, and offset
200,000 on 714710 reached 2026-09-01 in the research). Mainnet genesis was
2025-01-17. REST `trades` would serve only the newest 3,000 per index and is
not used. Hourly funding (`fundings`): up to 750 rows per request; no
retention limit was seen [VERIFY on a 2025 account].

## Mapping (connector summary)

Composite account. The watched key is the lower-cased L1 address. Every
pass starts with `accountsByL1Address` (master `account_type 0` and
sub-accounts `1`; public pools are not the address's money and are left
out) and walks each index's explorer log. The cursor is one opaque JSON
object with, per index, the walk position and the replay state; new indexes
are picked up on the next pass.

Explorer walk (`_Walk`). The log is newest first with offset paging and
new rows shift every offset, while both replays need oldest first.

- Locate. A first pass finds the oldest page by galloping (offsets 100,
  200, 400, ...) and then bisecting, on whether a page is full; a short
  non-empty page holds the genesis row. Rows arriving mid-search can fill
  the "empty" upper bound; once it is less than a page above the full lower
  bound it is known stale, dropped, and the search gallops on from the
  lower bound.
- Forward. The walk then moves toward offset 0, finding its anchor (the
  newest row applied) in each page and taking only the rows above it. It
  remembers where it last saw the anchor and how far the anchor moved
  between its last two requests (the drift: rows that arrived meanwhile),
  and asks for the page that should put the anchor near its bottom, so it
  takes about 90 rows a request however busy the account is. A page without
  the anchor is newer than it (the request is never below where the anchor
  was last seen), so the walk asks for the next page down. It keeps up while
  fewer than 90 rows arrive between two requests of one index; no walk of
  100-row pages can do better. Tests drive it with 10 to 60 rows arriving
  before every request (`tests/test_lighter_walk.py`).
- Never a gap, never a duplicate. A walk that loses its anchor (an explorer
  history that changed) raises rather than guesses, as does a page where a
  tx hash repeats (below).

`since` is ignored: a `None` cursor always replays from genesis, so a
restarted backfill reproduces identical legs (tested). One consequence:
`reconcile.take_asset_anchor`'s "no fill near the anchor" check does not
apply to Lighter. Its probe (`fetch_asset_fills_page(None, since)`) is the
index list, which holds no fills, so the anchor is always taken at once.
That is harmless here: the ledger replays from genesis, not from the
anchor's balance, and the nightly reconcile compares the replayed vector
with the venue's.

Status. `executed`, `nothing_to_execute`, `committed`, `verified` and null
all carry real activity (README): rows with those are booked whatever the
status. Any other status (a future `failed` or `rejected`) is flagged as
`UnsupportedActivity` and not booked.

Fills (`fetch_asset_fills_page`), from explorer rows with `trade_pubdata` or
`trade_pubdata_with_funding` (Trade, TradeWithFunding, LiquidationTrade,
LiquidationTradeWithFunding, and any deleverage or settlement type of the
same shape):

- **Deleverage and market settlement are not captured** [VERIFY]. A row
  whose type names a trade, liquidation, deleverage or settlement but
  carries no trade pubdata is flagged by both streams (it would move the
  position, so the replay of that market is off until it is mapped;
  reconcile shows the gap).
- id `f"{index}:{tx_hash}"`: one trade per hash in every capture; the index
  prefix keeps both sides of a trade between two of the address's own
  indexes. A page where a hash repeats stops the sync with an error rather
  than numbering the rows by position, which would depend on page
  boundaries [VERIFY multi-row transactions]. Rows of one transaction are
  adjacent, and every pair of adjacent rows shares a page of the walk, so
  the per-page check sees them.
- side: the index is the taker or the maker; `is_taker_ask` gives the
  taker's side. Liquidity is the role. Liquidations and deleverages are
  ordinary fills (the liquidated taker pays `taker_fee` 10000 = 1%).
- perp: `FILL_BASE ±size` of `"<SYMBOL>-PERP"` at the market's size
  decimals, `REALIZED_PNL` (derived, below) in USDC at 6 dp, `FEE`.
- spot (`"LIT/USDC"`): `FILL_BASE ±size` of the base, `FILL_QUOTE ∓price x
  size` USDC (exact at 6 dp), `FEE` in what the trade delivers: the base on
  a buy, USDC on a sell [VERIFY: no captured spot fee showed the asset].
- fee: `notional x rate`, the rate an integer in millionths (maker 28,
  taker 196 on Premium; 0/0 on Standard). Exact at 12 dp, never rounded:
  notional is exact at 6 dp because size_decimals + price_decimals = 6 on
  every market, and Lighter itself keeps balances at 12 dp. A market listed
  with a larger sum (notional finer than a micro) has every trade flagged,
  never rounded, and the rest of the account syncs on.
- instruments: `orderBookDetails` (one request, cached per instance): tick
  10^-price_decimals, step 1 atom at size_decimals, `min_base_amount`,
  `min_quote_amount`. Markets of any other type (binary up/down) and unknown
  markets are flagged (`UnsupportedActivity`), never booked.
- `copyable=False` for synthetic equities and ETFs, indices, pre-IPO names
  (ANTHROPIC, OPENAI, SPACEX ...), FX, rates, commodities and the spot
  tokens of the same kind (`NON_CRYPTO_SYMBOLS`; Lighter publishes no
  category field) [VERIFY each new listing; unknown symbols default to
  copyable].

Events (`fetch_events_page`), from the same explorer walk:

- `L1DepositV2` (legacy `L1Deposit`): DEPOSIT of `accepted_amount`.
- `WithdrawV2` (legacy `Withdraw`): WITHDRAWAL of `amount` [VERIFY how fast
  (Arbitrum) withdrawals log, and any withdrawal fee].
- `L2TransferV2` (legacy `L2Transfer`): to or from another index,
  TRANSFER_OUT/IN (plus `usdc_fee` as FEE on the sender); between the
  address's own indexes, or between one index's spot and perps routes,
  nothing (the fee, if any, once, as CONVERT). The first transfer of a pass
  that names an index not on the pass's list re-reads `accountsByL1Address`
  (once per pass): a sub-account created since the pass started is then
  internal, and it is walked in the same pass.
- `L2MintShares` / `L2StakeAssets` (captured): TRANSFER_OUT plus a
  `pool_share` flag. The way back (`BurnedShares`, `L2BurnShares`,
  `UnstakeAssets`, `L2UnstakeAssets`, `ExecutedPendingUnlock`) is not
  captured, and one unstake or burn may log both a request row and an
  executed-unlock row: booking both would count the money twice. So those
  are flagged (`pool_share`) with no legs [VERIFY with a capture]; reconcile
  shows the cash that came back.
- no-op types (leverage, margin, account config, sub-account creation, key
  changes, order management): nothing. Any other type is flagged.
- derived funding (below): one FUNDING event per (index, perp market, hour)
  with `venue_ref = funding:{index}:{market_id}:{hour}`. Hours are booked
  up to floor_hour(now - 5 min). An hour after the newest value in the
  `fundings` answer is not out yet: it waits in the cursor and is tried
  again at the end of the next pass, so the ledger does not depend on when
  the sync ran (tested). An hour missing between published values is
  booked as no funding and logged (a decision: Lighter's public record
  shows none; reconcile catches any charge it made).

Balances (`fetch_balances`), summed over the indexes from
`account?by=index`: each asset's `balance` (spot) + `margin_balance` (cross
collateral, 12 dp), plus every position's `allocated_margin` (isolated) for
USDC, i.e. cash excluding unrealized PnL; perps as `"<SYMBOL>-PERP"`
(`sign` x `position`) with `avg_entry_price` when one index holds the
market; `equity_usd_micros` = the sum of `total_asset_value` (collateral +
isolated margin + unrealized PnL; perps only) plus spot USDC, and None when
the address holds any other asset (Lighter values none). `locked_balance` is
taken as part of `balance` [VERIFY]. `/api/v1/pnl` (auth-documented) is not
read.

Decision (plan L1): public-pool and staking holdings are **not refused at
watch time**; they are outside the reconciled vector (`shares[]` and
`pending_unlocks` are ignored), the captured moves out are external flows,
the uncaptured moves back are flagged only (see Events), and the account
carries the `pool_share` flag so its numbers say what they cover.

## Streaming

`LighterTradeStream`, mode NUDGE: the read-only socket
`wss://mainnet.zklighter.elliot.ai/stream?readonly=true` (the only URL the
module can build; the plain one refuses US IPs), one
`{"type":"subscribe","channel":"account_all_trades/<index>"}` per index
(`prepare()` lists them), `{"type":"ping"}` every 30 s (answered
`{"type":"pong"}`, checked live 2026-10-22). The subscription snapshot
nudges nothing; an `update/account_all_trades` frame with any trade queues
the account's sync, and the explorer path books the legs. Not FILLS: the
frames are REST-shaped trades, but the legs depend on replay state (derived
PnL and funding, the composite-account transfer rule) the stream process
does not hold. A frame with code 20558 (restricted jurisdiction) is
`StreamUnavailable`.

## Watchlist (third-party addresses)

What is read for a watched address: exactly the list above, with no
credential: `accountsByL1Address`, `account?by=index`, the explorer log of
each index, `orderBookDetails`, `fundings`. No ownership proof, `claimed =
false`, owned by the system user, never listed (as on Hyperliquid).

Position:

- **Private only**: `/watch/{id}` for the user who added the address, absent
  from every public surface.
- **No identity linking**: no ENS, no name lookups. Lighter accounts carry
  `name` and `description` fields (empty in every capture); the connector
  never reads them. Koine stores the address and the venue data only.
- **Opt-out**: the owner proves the address with the EIP-191 message above
  (signer = the address); Koine records it in `watch_optouts`, refuses new
  watches, ends existing ones and their paper tails. Imported rows stay
  (append-only). Lighter has no venue-side privacy flag
  (`private_addresses()` / `owner_private()` stay None): the signed opt-out
  is the only route.
- **Paper only**, and the US notice above on every watched page.
- **Volume**: every watched address draws from the same two IP buckets
  (60 API requests and 90 explorer weight per minute). See "Rate limits";
  integration item 5 of the plan checks the headroom at `watch_max_tracked`.

Terms: the watchlist is the use counsel Q1 is about; Lighter is not in prod
`WATCH_VENUES` until it is answered. [VERIFY with counsel, as for
Hyperliquid: privacy law for scoring third-party addresses (plan Q4).]

## Leaderboard (watch discover)

Endpoint: `GET /api/v1/pnlLeaderboard?time_window=&sort_by=&sort_dir=desc&limit=100&offset=`
(documented, https://apidocs.lighter.xyz/reference/pnlleaderboard), no
authentication. Windows `24h`, `7d`, `30d`, `all` map 1:1 to
day/week/month/all. About 99,500 addresses; refreshed hourly on the hour.

What is read per row: `l1_address` (EIP-55 mixed case; lowercased),
`account_value`, `pnl`, `roi`, `volume`, `rank`, as JSON **floats**, parsed
straight to Decimal (never a binary float), then `leaderboard_figure`.
Dropped: `streak`, `longest_streak`, `net_position`,
`biggest_position_value`, `biggest_position_asset`, `long_percentage`,
`short_percentage`. There are no names, avatars or handles on the board.

Per refresh and window: up to 3 pages by pnl (stopping early once twice the
snapshot's rows clear the account-value floor), then up to 2 pages by roi
(small accounts top that sort; the floor drops them). A row keeps its pnl
rank; a row seen only in the roi pages is ranked after every fetched pnl row
(its pnl rank is unknown; Koine shows no ranks). At most 20 requests an
hour.

`roi` is a **percent** (rank 1 of 30 days: 14.93), divided by 100. Its
denominator is Lighter's own: not `pnl / account_value` (4,827,967 /
24,165,041 = 19.98%), apparently cumulative net inflow with a $100 floor
(several small accounts show roi = pnl / 100 exactly) [VERIFY]. Koine
labels it as Lighter's figure and does not reproduce it. Whether the board
aggregates sub-accounts and pool shares into the owner's row is [VERIFY]
(rank 1's account value includes its spot balances).

Terms: the board is documented API, not the interface; counsel Q1 covers it
with the rest.

## Known gaps

- **Realized PnL and funding are reconstructed by Koine** (methodology notes
  below). Realized PnL matched Lighter's own figure on every REST row
  captured (100 of 100, exact); funding matched the account's cash to
  0.0000025 USDC over a full history.
- Funding values are published to 8 dp, so each derived hour can differ from
  Lighter's exact charge by up to 0.5e-8 x |position| (the reconcile
  tolerance covers it). Lighter's per-position `total_funding_paid_out`
  differed from the derived sum by 0.0000956 on one position, a little more
  than our rounding bound (0.0000747) [VERIFY Lighter's own rounding].
- Spot fee asset (received asset assumed) [VERIFY].
- Pool shares (LLP) and LIT staking: outside the vector; flows out booked,
  flows back flagged only (uncaptured shapes), so reconcile shows them. The
  return on them is not Koine's to measure.
- A sub-account created during a sync: the first unknown index of a pass
  re-lists the address, so a transfer to it is internal. A second new
  sub-account created later in the same pass (after that one re-list)
  would still be booked as TRANSFER_OUT and not undone [rare].
- Deleverage and market-settlement rows: not captured, flagged [VERIFY].
- A delisted market that stops publishing funding while a position stays
  open: its hours wait in the cursor and cost one `fundings` request per
  pass [VERIFY what Lighter does with open positions at delisting].
- REST `trades` (rich rows, trade ids) is not used until counsel Q1 clears;
  the explorer gives the same fills, so turning it on later would need the
  same ids (`index:tx_hash`) and identical legs.
- Lighter on Robinhood Chain (`api.rh.lighter.xyz`, USDG collateral) is a
  separate exchange: a second venue (`lighter_rh`) if ever, not a
  sub-account.
- Margin tiers: one tier per market from `maintenance_margin_fraction`
  (Koine's paper model uses 1 / (2 x max leverage); max leverage =
  floor(5000 / fraction) keeps Koine's maintenance at or above Lighter's).
- Paper fee: 3 bps of notional (Premium taker 2.8 bps at 0 LIT staked,
  rounded up; Standard accounts pay 0), so paper copies are never flattered.

## Methodology notes

Merged into docs/methodology.md (served at /methodology) on 2026-10-22; edit them there.

