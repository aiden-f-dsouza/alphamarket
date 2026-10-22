# Perp DEX scout: watchable venues beyond Hyperliquid (2026-10-22)

Quick scan, not a deep dive. Scope: on-chain or self-custodial perp venues whose
**public, unauthenticated** data would let Koine watch a third-party address the
way `watchlist.py` watches a Hyperliquid address. dYdX v4, GMX, Drift and Lighter
are covered by a separate pass and are not repeated here.

All requests were made live on 2026-10-22 from this machine (US residential IP).
Anything not confirmed by a real response or a fetched document is marked
**[VERIFY]**. Raw responses are saved under the session scratchpad
`fixtures/<venue>/` (pacifica, nado, gtrade, jupiter_perps, plus
`_market_only/` for venues that only expose market data).

## What the pipeline needs (the test each venue was put through)

From `connectors/hyperliquid.py`, `connectors/base.py` (contract v2) and
`docs/venues/hyperliquid.md`:

1. A **public leaderboard of addresses** (for `/watch/discover`; optional, a venue
   can be watch-only without one).
2. **Per-address public history**: fills with a unique id, instrument, side,
   price, size, fee, time; funding payments; deposits/withdrawals/transfers
   (flow-adjusted TWR needs every external flow); liquidations.
3. **Current state** for reconcile: positions and account value.
4. **Instrument metadata** (tick, step, decimals) and **public quotes**
   (bid/ask at detection time for paper fills, funding rates, marks).
5. US egress (Fly `iad`) must reach the API.

## Summary

| Venue | Leaderboard (addresses) | Per-address fills/funding/flows | US API reach | Verdict |
|---|---|---|---|---|
| **Pacifica** (Solana) | Yes, `GET /api/v1/leaderboard`, ~7k rows, with `privacy_mode` flag | Yes: trades, funding, balance history (deposit/withdraw), positions, account | 200 | **strong alternate** |
| **Nado** (Ink; Vertex team) | None found [VERIFY] | Yes: archive indexer `matches`, `events`, `interest_and_funding`, gateway `subaccount_info` | 200; docs: US is "view-only" | **strong alternate** (watch-only, no discover) |
| **gTrade / Gains** (Arbitrum, Base, ...) | Yes, `backend-global.gains.trade/api/leaderboard/all` (top 25 per 1/7/30/90d) | Trade history per address (open/close/TP/SL/liq events); no account-level deposits (wallet-funded per trade) | 200 | **maybe** |
| **Jupiter Perps** (Solana) | Weekly top traders per market (`/v1/top-traders`) | Trades per wallet incl. fees, liquidations; per-position, wallet-funded | 200 | **maybe** |
| **Avantis** (Base) | Not found [VERIFY] | On-chain events only (no REST found) | 200 (market data) | maybe (needs chain indexer) |
| **Ostium** (Arbitrum) | Not found [VERIFY] | On-chain events; guessed subgraph 404 | 200 (prices) | maybe (needs chain indexer) |
| Aster | Not found (guessed bapi path 404) | No: Binance-style signed API keys | 200 | no |
| Paradex | `/v1/leaderboard` needs JWT (401) | No: private by design (account JWT) | 200 | no |
| edgeX | No | No (StarkEx L2 keys) | wrong host returned HTML [VERIFY] | no |
| Extended (ex-X10) | `/api/v1/leaderboard` 404 | No (API-key auth) | 200 | no |
| ApeX Omni | No | No (API-key auth) | 200 | no |
| Orderly | Broker campaign boards only [VERIFY] | No (Orderly key auth) | 200 | no |
| Bluefin (Sui) | No | Public trades carry no account [VERIFY account endpoints] | 200 | no |
| GRVT | No | No (API-key auth) | 200 | no |
| Aevo | Yes but **usernames only, no addresses** | No | 200 | no |
| Vertex | n/a | n/a | DNS failure: sunset (team moved to Nado) [VERIFY date] | no |

No venue tested geoblocks US IPs at the API layer. Pacifica documents a
`403 Forbidden: restricted region` error code, but US requests returned 200.

## Strong alternates

### Pacifica (Solana perp orderbook)

Best fit found: it is the closest thing to Hyperliquid's `info` endpoint.

- Leaderboard: `GET https://api.pacifica.fi/api/v1/leaderboard` returns about
  6,958 rows: `address`, `username`, `pnl_1d/7d/30d/all_time`,
  `equity_current`, `oi_current`, `volume_*`, `privacy_mode`. Maps directly to
  `LeaderboardEntry` (DAY/WEEK/MONTH/ALL, pnl, volume, account_value). Drop
  `username`. **Drop every row with `privacy_mode: true`** and refuse to watch
  such an address: it is the owner's own opt-out signal. Note that the trades API
  still served history for a `privacy_mode` address, so the API does not
  enforce it and we have to.
- Per address (`?account=<base58>`), all 200 with no auth:
  `/api/v1/account` (equity, balance, margin), `/api/v1/positions` (entry,
  funding, isolated flag), `/api/v1/trades/history` (`history_id`, `order_id`,
  symbol, amount, price, fee, pnl, `event_type` taker/maker, `side`
  open/close long/short, `cause` normal/liquidation [VERIFY values]),
  `/api/v1/funding/history` (`history_id`, payout, rate), and
  `/api/v1/account/balance/history` (deposit, withdraw, deposit_release,
  with running `balance`). Cursor pagination (`next_cursor`, `has_more`).
  This covers every flow TWR needs.
- Market data: `/api/v1/info` (tick_size, lot_size per symbol),
  `/api/v1/info/prices` (mark, mid, oracle, funding), `/api/v1/book?symbol=`.
- Rate limits: credit-based, 60s rolling window. Credit cost for an
  unidentified IP not captured [VERIFY]. Websocket exists [VERIFY user-trades channel].
- Pipeline impact: **addresses are base58 and case-sensitive.** The
  0x-lowercase assumption in `watchlist.normalize_address` and
  `LeaderboardEntry.address` must become per-venue, and Pacifica addresses must
  never be lowercased. All amounts are decimal strings (fits `to_micros` /
  `to_atoms`).
- Terms: ToS page did not render to text; whether US persons are excluded is
  [VERIFY]. Treat like Hyperliquid until read.

### Nado (Ink L2, built by the former Vertex team)

- No leaderboard query exists in the archive indexer (the 422 error lists every
  query variant; none is a leaderboard). A UI-only board may exist [VERIFY]. So
  this is watch-by-paste only; nothing for `/watch/discover`.
- Per subaccount (bytes32 = 20-byte address + 12-byte name, e.g. `default`),
  no auth: `POST https://archive.prod.nado.xyz/v1` with `matches` (order
  digest, base/quote filled, fee, pre/post balance), `events` (filter
  `deposit_collateral`, `withdraw_collateral`, `transfer_quote`, liquidations),
  `interest_and_funding` (funding payments with idx, rate, oracle price);
  `GET gateway.prod.nado.xyz/v1/query?type=subaccount_info` for positions and
  health; `subaccounts` query to enumerate a wallet's subaccounts.
- Market data: `gateway.prod.nado.xyz/v1/symbols` (price/size increments,
  fees, all x18), `query?type=market_price` (bid/ask). Gateway requires an
  `Accept-Encoding: gzip|br|deflate` header (403 without it).
- Numbers are 1e18 fixed-point integers: convert with integer math only, never
  floats (invariant 1).
- Terms: `docs.nado.xyz/legal/legal/restricted-territories` lists the **US and
  Canada as "view-only"**: viewing allowed, trading disabled. That is a cleaner
  position than Hyperliquid's for read-only watching. Full Terms of Use on
  scraping/automated access [VERIFY].

## Maybe

### gTrade (Gains Network)

- Leaderboard: `GET https://backend-global.gains.trade/api/leaderboard/all?chainId=42161`
  gives top 25 by PnL for 1/7/30/90 days, with addresses (checksummed, so
  lowercase them), win/loss counts and pnl. Thin (25 rows), and PnL only.
- History: `GET https://backend-global.gains.trade/api/personal-trading-history/<addr>?chainId=42161`
  returns trade events (`TradeOpened`, `TradeClosedTP`, liquidations
  [VERIFY action names]) with price, size, leverage, pnl, tx hash, and fee
  breakdown in `meta`. Paginated.
- Problems: values are **JSON floats**, not strings, so we would need to read
  the on-chain events or accept float parsing at ingest, which conflicts with
  invariant 1. It is a synthetic, oracle-priced venue with no orderbook: there is
  no account balance, collateral is pulled from the wallet per trade, so each
  trade is an external flow. Our TWR model would need a "wallet-funded
  per-position" mode. Multi-chain (Arbitrum, Base, ApeChain, ...), so each chain
  needs its own query. Paper fill quotes would come from the oracle/pair spread
  (`trading-variables`, 3 MB), not a book.
- Terms: Gains docs ToS did not render [VERIFY]; historically it excludes US
  persons [VERIFY].

### Jupiter Perps (Solana)

- Board: `GET https://perps-api.jup.ag/v1/top-traders?marketMint=..&year=&week=`
  is a weekly top list per market (SOL, ETH, BTC), owner address plus pnl and
  volume (integer 1e6 USD). Usable as WEEK only.
- History: `/v1/trades?walletAddress=` gives increase/decrease/liquidation events with
  price, size, fee breakdown (base, borrow, liquidation, price impact), pnl,
  tx hash, position pubkey. `/v1/positions?walletAddress=` returns open positions.
  Fields are decimal strings.
- Problems: same per-position, wallet-funded model as gTrade (no account
  equity, no deposits). It charges borrow fees, not funding. Only 3 or so
  markets. Base58 addresses, as with Pacifica. The perps API is not formally
  documented as public [VERIFY].
- Terms: Jupiter ToS fetched (23 KB) but did not mention the United States in
  text; restricted jurisdictions [VERIFY].

### Avantis (Base) and Ostium (Arbitrum)

Both are fully on-chain, oracle-priced venues (crypto plus FX, commodities and indices).
History is public on-chain by construction, but no public per-address REST or
leaderboard endpoint was found (guessed Avantis `/v1/leaderboard` 404, the
portfolio endpoint errored, the guessed Ostium Goldsky subgraph 404). To build
either we would need our own log indexer or an RPC-based connector. They also share
the wallet-funded per-position model. Market data endpoints (Avantis socket-api
data, Ostium `metadata-backend` latest prices with bid/ask) answered 200.
Terms [VERIFY]; both historically exclude US persons [VERIFY].

## No

Aster, Paradex, edgeX, Extended, ApeX, Orderly, GRVT, Bluefin: account
history is behind per-account API keys or JWTs (Paradex is private by design).
None publishes an address leaderboard we could reach. They are watchable only if
the owner connects a key, which is out of scope (wallet connects are excluded).
Aevo's leaderboard has generated usernames, no addresses. Vertex is gone.

## Cross-cutting notes for the build phase

1. **Address type per venue.** Solana (Pacifica, Jupiter) is base58 and
   case-sensitive; Nado is a bytes32 subaccount derived from a 0x address. Make
   `normalize_address` venue-aware, keep 0x lowercasing for EVM only.
2. **Owner opt-out signals.** Pacifica's `privacy_mode` should be honoured as
   an automatic opt-out, in addition to `watch_optouts`.
3. **Per-position venues** (gTrade, Jupiter, Avantis, Ostium) do not fit the
   account-equity TWR. They need a methodology decision before any connector work.
4. Suggested order: Pacifica (watch and discover), then Nado (watch only). Defer
   the per-position venues.
