# Kraken (spot)

Status: **needs_approval**. The connector is built and tested; the profile
ships with `requires_partner_approval=True`, so production refuses Kraken
connects until Kraken's written consent is on file (or a legal read concludes
the support-article endorsement below is enough). Flip the flag in
`connectors/registry.py` and change this status in the same commit.

Terms checked: 2026-09-26, by the venue-kraken workstream (not a lawyer).

## Terms of service position

Sources, all read on 2026-09-26:

- Kraken.com Terms of Service, "Last Updated: 14 September, 2026":
  https://www.kraken.com/legal/global-terms (www.kraken.com/legal/us-terms
  served no separate text on 2026-09-26; re-check it for US users [VERIFY])
- Kraken API documentation, "Terms of use" section of the API introduction:
  https://docs-legacy.kraken.com/api/docs/guides/global-intro/
- Kraken support, "Using the Kraken API with a third party service":
  https://support.kraken.com/articles/360041395271-using-the-kraken-api-with-a-third-party-service
- Kraken support, "How to use our API for taxes":
  https://support.kraken.com/articles/how-to-use-our-api-for-taxes

What the Terms say (section "Restrictions", "You will not: ..."):

> use (except as expressly permitted in these Terms), license, sublicense,
> sell, resell, transfer, assign, distribute or otherwise commercially exploit
> or make available to any third party Our Content in any way, [...]
> develop any third-party applications that interact with Our Content without
> our prior written consent, [...] use or attempt to use Our Content for any
> person other than your self, [...] or encourage, permit, or enable any other
> person or entity to do any of the foregoing.

"Our Content" is defined (section 8) as Kraken's "services and Platforms, all
content, materials, software, and trademarks found on them, the selection and
arrangement of them, and all intellectual property rights in them". The
account-security clause adds: "not share your account or password with anyone
else".

What the API documentation says:

> Use of the Kraken APIs is subject to the Kraken Terms & Conditions and
> Privacy Notice, as well as all other applicable terms and disclosures made
> available on www.kraken.com.
>
> You must seek our prior permission for certain uses of the Kraken API's.
> This includes, but is not limited to, any non-personal commercial use of
> data from publicly accessible endpoints, such as market data, exchange
> status, and any other data. (contact: marketdata@kraken.com)

What Kraken support says (in favour): an account owner "simply needs to create
an API key with the appropriate configuration, and provide this key to the
third party service", and the tax article recommends read-only keys (Query
Funds, Query Closed Orders & Trades, Query Ledger Entries, Export Data) for
third-party tax software. A dedicated, permission-limited API key is not the
account password, so the password clause does not bite.

Reading: handing a read-only key to a third-party service is explicitly
endorsed by Kraken's own support pages, but Koine is exactly "a third-party
application that interacts with Our Content", it publishes the resulting
numbers (a public track record, not a private tax report), and it would use
public OHLC data commercially for valuation. Each of those needs Kraken's
prior written consent under the current wording. Hence `needs_approval`
rather than the `ok` the 2026-09-16 research assumed (that research marked the
terms [VERIFY]; the Terms were updated on 2026-09-14).

To clear it: (1) write to Kraken partnerships describing the read-only,
user-consented, public-track-record use and ask for written consent;
(2) write to marketdata@kraken.com for OHLC/Ticker commercial use, or value
positions from CoinGecko instead (the `pricing_symbol` fallback already
returns None for anything Kraken does not list).

## Consent text for the connect screen

> **Connect Kraken (read-only)**
>
> You are giving Koine a Kraken API key that you created. Koine will use it to
> read your balances, your trade history and your ledger (deposits,
> withdrawals, staking and transfers), and will compute and publish a track
> record from them on your public strategy page.
>
> Create the key with ONLY these permissions: Query Funds, Query Closed Orders
> & Trades, Query Ledger Entries. Koine tests every key when you connect it and
> again every night: it asks Kraken to validate (not place) an order and to list
> your withdrawal methods, and it refuses the key unless Kraken answers
> "Permission denied" to both. Koine never places orders, moves funds or stakes
> on your behalf, and your key is stored encrypted and never shown again.
>
> Kraken's terms restrict third-party applications and the sharing of Kraken
> data; by connecting you confirm the account is yours and that you want this
> data published. You can disconnect at any time and should then delete the
> key in Kraken.

## Read-only proof (ReadOnlyProof.ORDER_PROBE)

Kraken has no endpoint that describes a key's permissions, so the connector
probes. Exactly what is sent, in this order, each a signed POST:

| Request | Parameters | Must return |
|---|---|---|
| `/0/private/Balance` | `nonce` | success |
| `/0/private/TradesHistory` | `nonce, without_count=true` | success |
| `/0/private/Ledgers` | `nonce, without_count=true` | success |
| `/0/private/AddOrder` | `nonce, pair=XBTUSD, type=buy, ordertype=limit, price=1.0, volume=0.0001, validate=true` | `EGeneral:Permission denied` |
| `/0/private/WithdrawMethods` | `nonce` | `EGeneral:Permission denied` |

`validate=true`: "the order will be validated only, it will not trade in the
matching engine" (AddOrder docs). The $1 limit price is a second guard.
WithdrawMethods needs "Funds permissions - Query" AND "Funds permissions -
Withdraw", so a query-only key is refused.

Outcome: `read_only=True` when both write probes are refused and all reads
succeed; `False` (reject at connect, suspend later) when either write probe
is answered with anything other than Permission denied (a validated order,
"Insufficient funds", a withdrawal-method list...); `None` when the key is
safe but lacks a read permission. `details` records each probe's first error
string (never the key or signature).

Not probed (known gaps): Cancel/Close Orders (a cancel probe risks matching a
real order by userref), Deposit Funds (address generation), Export Data, Earn
allocate/deallocate, Access WebSockets. The connect screen asks for the three
query permissions only; a key with only Cancel or Earn permissions would pass
the probes.

## What a read-only key exposes

Query Funds: every asset balance (Balance/BalanceEx), including staked and
held variants. Query Closed Orders & Trades: all-time trades and closed
orders (pair, side, price, volume, cost, fee, order ids). Query Ledger
Entries: every balance change with a running balance (deposits, withdrawals,
staking rewards, transfers, fees). It also reveals withdrawal/deposit
references in ledger refids, but not addresses or bank details.

## Rate limits

- Private, per API key: counter max 15 (Starter), decays 0.33/s (20/min);
  Intermediate/Pro 20 at 0.5/s or 1/s. History calls (Ledgers, TradesHistory)
  cost +2 per the docs page and +4 per the 2026-08-10 support article; the
  profile charges 4 until verified [VERIFY]. Errors: `EAPI:Rate limit
  exceeded` (mapped to RateLimited, retry 12 s) and `EService:Throttled:
  <unix ts>` (RateLimited until that time). HTTP 429/418 honour Retry-After.
- Public, per IP: about 1 request per second [VERIFY].
- A full read-only proof costs about 11 counter points (1 + 4 + 4 + 1, AddOrder
  uses the separate trading limiter).
- Nonces: microseconds from one process-wide counter. Two processes using the
  same key at the same instant can still collide (`EAPI:Invalid nonce`);
  the job queue should run one job per account at a time.

## History depth

All-time: TradesHistory and Ledgers page back to account opening
(`history_depth="all"`). Public OHLC serves the last 720 daily bars only;
older closes need Kraken's downloadable OHLCVT archive or CoinGecko.

## Asset codes

Legacy X/Z codes map through their altname (XXBT -> BTC, XETH -> ETH,
ZUSD -> USD, XXDG -> DOGE, ZEUR -> EUR; KFEE fee credits stay KFEE). Suffixed
balances are separate assets with their own running ledger balance, so they
keep their suffix: ETH2.S (staked), ETH2 (legacy unstaked ETH2), BTC.M (opt-in
rewards), DOT28.S (28-day bonded), USD.HOLD, *.F (auto-earn), *.B
(yield-bearing), *.P (parachain). Moving value between a base asset and a
variant is a CONVERT; valuation prices a variant as its underlying (ETH2.S
and ETH2 by ETH/USD, DOT28.S by DOT/USD), and USD/USDC/USDT variants at par.

## Known gaps

- Terms: needs written consent (above).
- Private payloads are from the docs' examples plus a docs-shaped synthetic
  account until the founder's account exists; re-record with
  `scripts/kraken_smoke.py --record`.
- Margin trading is out of scope: `margin`, `rollover` and `settled` ledger
  rows are kept (REALIZED_PNL, INTEREST) so balances replay exactly, but open
  margin positions are not valued.
- Kraken Futures is a separate account: `spottofutures`/`spotfromfutures`
  transfers are TRANSFER_OUT/IN.
- Buy/Sell/Convert (`spend`/`receive`) and staking wrappers are one CONVERT
  event per ledger row, not one event per conversion.
- Trade ledger rows with no TradesHistory entry stop the fills backfill with
  an error instead of being dropped.
