# Methodology: what a verified track record proves, and what it does not

This page is the public contract behind every number Koine displays.
Every formula here is pinned by a unit test with hand-computed expected values
(`tests/test_ledger.py`, `tests/test_reconcile.py`).

## Where the numbers come from

- Every number on a strategy page is computed by Koine from data pulled
  directly from the venue using a **read-only** connection the trader made:
  a read-only API key (Kalshi, Kraken, Coinbase, Binance.US), a read-only
  OAuth grant (Alpaca), a reporting token (Interactive Brokers Flex), an
  aggregator's read-only connection (SnapTrade) or a public wallet address
  (Polymarket, Hyperliquid). Traders cannot enter, edit, or upload
  performance figures.
- A key's scope is checked at connect time (against the venue's own key
  registry, or by a probe a read-only key must fail; see each venue's section
  below); a key with trading permission is rejected. A wallet address
  involves no key at all: the history is public on-chain data.
- Fills and settlements are stored **append-only**: rows are never updated or
  deleted, at the application layer and (on Postgres) by database triggers.
  Corrections, if a venue ever issues them, arrive as new rows.

## Units

All money is held as integer micro-dollars (1e-6 USD). Kalshi reports
sub-cent fees (e.g. `0.139800`) and fractional contract counts (e.g. `101.89`);
whole cents cannot represent a fill exactly, and rounding would create the very
divergence reconciliation exists to detect.

Polymarket reports a fill's average price to ten decimals (it is the volume-
weighted price across the maker fills of one order) but the cash that moved
to six. Koine stores the price rounded to the micro, the venue's gross value
(`size × price`, rounded to the micro) as the fill's notional, and the fee as
`|gross − cash moved|`, so the ledger's cash flow reproduces the venue's
`usdc_size` exactly. The rounding lands on the split between notional and fee,
never on cash. Checked against a captured trade: 21.294467 tokens at
0.4696055083, $10.265189 out, fee $0.265190, which is Polymarket's 5% ×
size × p × (1 − p) to within the price rounding.

## The cash ledger

Let `anchor` be the venue cash balance observed at the moment the key was
connected, at time `anchor_at`. For any later time `t`:

```
cash(t) = anchor
        + Σ fill_cash_flow(f)      for fills f      with anchor_at < filled_at  <= t
        + Σ revenue(s)             for settlements s with anchor_at < settled_at <= t

fill_cash_flow(buy)  = -(count × price + fee)
fill_cash_flow(sell) = +(count × price - fee)
```

Fees are charged by the venue at fill time only. The settlement record's fee
field is a summary of fees already paid and is **not** deducted again
(verified against a live account to the micro-dollar).

Events at or before `anchor_at` are excluded because the anchor balance already
contains them. This is also why limited venue history cannot break the
guarantee: a position opened before connection and settled afterwards adds its
settlement revenue as real cash, exactly as the venue does.

## Reconciliation

Nightly, and on demand, Koine re-reads the venue balance and compares it
with `cash(now)` computed purely from stored fills and settlements:

```
divergence = venue_balance - cash(now)
tolerance  = max(0.1% × venue_balance, $0.01)
```

If `|divergence| > tolerance`, the strategy is **quarantined** and its public
page is hidden until a human reviews it. Nothing is un-quarantined
automatically. Every check is stored as an `equity_snapshot` so the full
history of "did the ledger explain the balance?" is itself auditable.

## Verification tiers

Not every venue exposes the same evidence, and the page says which tier a
strategy is in:

- **Reconciled** (Kalshi): fills and settlements are pulled with a read-only
  key, and the cash balance is re-read nightly and checked against the
  ledger as above. This is the strongest claim Koine makes.
- **Fills verified, balance not reconciled** (Polymarket): fills, positions
  and redemptions are pulled from public, per-wallet endpoints, but the
  venue exposes no cash balance to check the ledger against (the proxy
  wallets of active traders show no USDC on-chain; the collateral sits
  where a public read cannot see it, as of September 2026). The ledger is
  anchored at zero cash, so the raw curve is P&L since connection. To give
  return-based statistics a meaning, the curve is shifted by the **peak
  capital invested**, the deepest the trader's cash went below the anchor:

  ```
  peak_invested          = max over t of (−cash(t)), floored at 0
  curve(t)               = equity(t) + peak_invested
  return_on_peak_invested = (equity(now) − equity(anchor)) / peak_invested
  ```

  Total return on such a page **is** the return on peak invested capital,
  and the label says so. Because the denominator is the peak over the whole
  window, early returns are understated rather than overstated. A trader who
  never redeems a losing position keeps it open at cost on this curve; the
  mark-to-market figures show the truth.
- **Positions verified nightly, cash reconstructed** (on-chain perp venues
  whose positions are funded straight from the wallet, with no account
  balance at the venue): fills are pulled from the venue's public data, and
  every night each open perp position (its signed size) is checked against
  the venue's own position, within the venue's documented quantity rounding.
  The cash is **not** checked: the venue keeps none for the address, so
  Koine reconstructs it from the trades, and the venue's page for the
  account says how. A position outside its tolerance quarantines the
  strategy, as under "Reconciliation v2"; the cash never can.

  ```
  for each perp position in either vector (a missing side counts as 0):
      divergence = venue size - ledger size        (atoms, signed)
      ok         = |divergence| <= the venue's position tolerance (atoms)
  ```

  Example: with a tolerance of 2 atoms, a ledger long of 1.500000000000 ETH
  against a venue report of 1.500000000001 ETH (1 atom at 12 dp) passes;
  1.500000000003 (3 atoms) quarantines.
- **Unclaimed**: a wallet address anyone could have pasted. The trader
  proves ownership by putting a Koine code in their venue profile bio; until
  Koine sees it there, nothing about the account is shown publicly.

## Honest limits

- Verification starts at connection. Anything before `anchor_at` is shown with
  its provenance labeled but is not part of the verified claim.
- We verify that **this venue account** did these trades. We cannot detect
  account sharing, or a trader holding the opposite position elsewhere.
- Deposits and withdrawals move the venue balance without a fill or a
  settlement. They will show up as a divergence and trigger review; support
  for classifying them is planned before public launch.
- Credentials are encrypted at rest with a server-side key. This is not a
  hardware security module. Disconnecting erases the stored key immediately.
  Full detail: [security and privacy](/security).
- Qualitative strategy descriptions are written by the trader and are labeled
  as unverified wherever they appear.

<a id="run-by-koine"></a>

## Run by Koine

Some strategies on the marketplace carry a **Run by Koine** badge. Koine runs
them itself, in trading accounts Koine owns. What that changes, and what it
does not:

- **Verified the same way.** A Koine-run account connects to Koine with a
  read-only key through the same connect flow as any creator: verified email,
  TOTP and a fresh step-up first. Its fills, balances, track record and
  statistics are computed from venue data by the same code, and reconciled
  nightly, exactly as for a creator. There is no special path for Koine's own
  accounts.
- **Koine never holds the trading key.** The agent that places the orders is
  a separate service with its own infrastructure and secrets; it holds the
  venue's trading key. Koine's platform only ever sees the read-only key for
  the same account. The nightly read-only proof applies to these accounts too:
  a key that has become writable is erased and the account suspended.
- **The description is Koine's own words.** The text that explains what a
  Koine-run strategy does is written by Koine and is **unverified**, like any
  creator's description, and is labeled as such on the page.
- **No ranking advantage.** The marketplace sorts a Koine-run strategy by the
  same measure as every other strategy and does not boost it. It goes through
  the same 90-day incubation and is hidden by default until then. You can
  show only Koine-run strategies, or hide them, with the Operator filter on the
  marketplace.
- **Featured slots.** If a featured slot is ever given to a Koine-run
  strategy, it carries the badge and this page says how featured strategies
  are chosen. Today the landing page shows the top row of the marketplace by
  the same sort as everyone else.

## Equity curve and statistics

`equity(t) = cash(t) + open_cost(t)`, where `open_cost` is the cost basis of
contracts not yet settled. **Open positions are carried at cost, not marked to
market.** P&L is recognized when a position is sold (at average cost) or when
the market settles. Positions already open at connect time are seeded from
the venue's own positions report, captured together with the anchor, so they
carry their real cost basis. Fills before the anchor are not replayed: the
anchor balance already contains their cash, and a position whose settlement
fell outside the venue's returned history must never sit on the books
inflating equity.

Daily series: end-of-day (UTC) equity for every calendar day from the anchor
day. Daily return `r_d = E_d / E_{d-1} - 1`.

| Statistic | Formula |
|---|---|
| Total return | `E_last / E_first - 1` |
| Max drawdown | `max over t of (peak_t - E_t) / peak_t` on the daily series |
| Sharpe | `mean(r) / stdev(r) × √365`, sample stdev; undefined below 2 days or zero variance |
| Sortino | `mean(r) / downside(r) × √365`, `downside = √(mean(min(r,0)²))` |
| Win rate | share of realized trades (settlements and sells after the anchor) with P&L > 0 |
| Concentration | top ticker share and top *series* share of total buy notional (series = ticker prefix, e.g. `KXBTCD`) |
| Max stake | largest single buy as a fraction of equity just before it |

Annualization uses 365 because prediction markets settle every day. No
risk-free rate is subtracted.

### Mark-to-market of the open book

The daily curve above carries open positions at cost so that it depends only
on the trader's own venue data. Beside it, Koine shows what the open book is
worth **right now** at public market quotes, refreshed every few minutes from
the venue's unauthenticated market endpoint:

```
mark(YES)         = settlement value once the market has resolved ($1 or $0),
                    else the bid/ask midpoint while both sides are quoted,
                    else the last trade price
value(position)   = count × mark(YES)            for a YES position
                  = count × (1 - mark(YES))      for a NO position
open_value        = Σ value(position)     (positions with no usable quote are
                                           carried at cost and their cost is
                                           reported as "unmarked")
unrealized P&L    = open_value - open_cost
marked equity     = cash + open_value
```

Marks are the venue's public quotes, never anything a trader enters.

## Risk score (1 calm to 10 wild)

Computed from realized behaviour only, never from the trader's description:

```
score = 1 + 3 × min(max_drawdown / 0.30, 1)
          + 3 × min(annualized downside deviation / 0.50, 1)
          + 1.5 × top_series_share
          + 1.5 × min(max_stake_fraction / 0.25, 1)
```
rounded to the nearest integer and clamped to [1, 10].

## Behavioural flags

Flags are displayed, never used to delist automatically:

- **martingale_sizing**: three consecutive losing trades each followed by a
  next buy of at least 1.8× the lost stake.
- **stake_spike**: with at least 10 buys, the largest buy exceeds 5× the
  median buy.
- **single_series_concentration**: with at least 5 buys, more than 50% of buy
  notional is in one market series.

## Paper tailing

A paper allocation is virtual capital that follows a strategy's verified
fills. Nothing is placed anywhere and no venue credential is involved. Every
paper figure is labeled as modelled. What the model does:

**Signals.** Each of the creator's post-anchor fills, as ingested from the
venue, becomes one signal carrying the creator's equity just before the fill
(`cash + open_cost`) and the creator's position in that market just after it.
Signals are append-only facts about fills that already happened; they are
never recommendations. A follower who joins sees only signals from that moment
on; nothing earlier is replayed.

**Sizing.** Counts are floored to 0.01 contract, the venue's own granularity.

```
buy:   follower_count = signal_count × follower_equity / creator_equity_before
       (capped at what the follower's cash can pay for, fee included)
sell:  follower_count = follower_position × signal_count
                        / (signal_count + creator_position_after)
       (the follower sells the same fraction of its position that the creator
        sold; a creator who sells out sells the follower out)
```

**Price and fee.** A paper fill takes the creator's own fill price, snapped
to the venue's price tick ($0.01 on Kalshi, $0.001 on Polymarket) so that
price × count is exact; this assumes that price was available to the
follower, which is the model's main optimism and is stated wherever a paper
figure appears. The fee is the venue's published formula,
`rate × count × price × (1 - price)`, at the venue's rate and precision:
Kalshi 7% rounded to $0.0001 (reproduces the one recorded live fill to the
micro-dollar), Polymarket 5% to the micro (reproduces the captured activity
rows).

**Settlement.** A follower position settles when the creator's settlement for
that market is ingested (same result, same time), paying $1 per contract on the
winning side. If the creator had already sold out and never receives a
settlement row, the public market's finalized result settles the follower.

**Valuation and the drawdown limit.**

```
cash(t)     = allocation + Σ mirror fill flows + Σ mirror settlement revenue
equity(t)   = cash(t) + open positions at the latest public mark
drawdown    = 1 - equity / high-water mark
stop        : drawdown > the follower's limit  ->  no further mirrors;
              open positions run to settlement and the curve keeps updating
```

(Crypto and stock tails close their positions instead: "When a tail stops".)

Each valuation is stored, and the series of valuations is the follower's
curve. Live mirroring into a real account is not offered: it requires a
licensed party in the loop, and the platform refuses it structurally rather
than behind a setting.

## Ledger legs

Every fill, settlement and account event is stored as a group of **legs**.
A leg is the signed change of one asset in one venue account: a contract, a
coin, a share, or cash. An account's holding of any asset is the sum of its
legs, and the cash ledger above is the special case "sum of the USD legs
after the anchor". Legs are append-only like fills.

A binary fill of `count` contracts at `price` with `fee` becomes three legs,
all in micro-units:

```
fill_base   contract   +count or -count (see below)
fill_quote  USD        -count × price (buy)   or  +count × price (sell)
fee         USD        -fee
```

`fill_quote + fee` is exactly `fill_cash_flow` above. How contracts are
counted depends on the venue:

- **Netting venues (Kalshi)** hold one signed position per market, as the
  venue reports it: `+` is YES, `-` is NO. Buying YES or selling NO adds
  `count`; buying NO or selling YES subtracts it. Settlement removes the
  position: `-(yes_count - no_count)`.
- **Tokenised venues (Polymarket)** can hold both outcome tokens at once, so
  each is its own asset (`<market>:yes`, `<market>:no`). Buying adds, selling
  subtracts, and a redemption burns the tokens redeemed.

A settlement adds one more leg, `+revenue` in USD. Its fee field is not a
leg, for the reason given under "The cash ledger".

Worked example (the live demo account): cash 1101.8980. Buy 0.84 NO at 0.57
with fee 0.0145: legs `-0.84` contract, `-0.4788` USD, `-0.0145` USD. The
market resolves YES: legs `+0.84` contract, `+0` USD. Result: contract
position `-0.84 + 0.84 = 0`, cash `1101.8980 - 0.4788 - 0.0145 = 1101.4047`,
the venue's balance to the micro-dollar.

A leg that moves nothing (a zero fee, a losing side's $0 payout) is not
stored. A settlement with no contracts and no payout therefore leaves no
trace, and the cash curve at every moment is unchanged.

**Re-reads are checked, not trusted.** Koine re-reads recent venue history
on every sync, so it sees events it has already stored. An event is either
stored whole or not at all. If any leg of an event is already stored, every
leg the venue now reports must match it exactly (compared at the finer of
the two precisions). A partial or different duplicate is not written. It
quarantines the strategy, with the reason recorded for review.

Before the legs path replaced the old formula, every recorded Kalshi and
Polymarket history, plus a generated 200-event history, was replayed both
ways and gave identical cash curves to the micro-dollar.

## Precision by asset

Coins and shares are not counted in micro-units. Each quantity is an integer
number of **atoms** at a stated number of decimal places, at most 12: 0.5
BTC at 8 decimals is `50000000` atoms. Quantities are parsed only from the
venue's decimal text, never from a floating-point number, and a value finer
than its stated precision is refused rather than rounded.

Venues change precision, and one venue can report the same asset at
different precisions (for example 8 decimals on fills and 10 on its ledger).
So before any sum, every value of an asset is rescaled to the finest
precision seen for that asset. Rescaling to more decimals is exact:

```
BTC:  +0.5 (8 dp)  and  -0.1234567891 (10 dp)
      -> 5000000000 - 1234567891 = 3765432109 atoms at 10 dp  (0.3765432109 BTC)
USD:  +100.25 (2 dp)  and  -0.0001 (4 dp)
      -> 1002500 - 1 = 1002499 atoms at 4 dp  (100.2499 USD)
```

Nothing is ever rescaled to fewer decimals unless the value is exact at the
coarser precision.

## Reconciliation v2 (coins and shares)

Binary venues are reconciled exactly as described under "Reconciliation".
Venues that hold coins or shares are reconciled asset by asset:

```
anchor vector   = every asset balance the venue reported at connection
ledger vector   = anchor vector + every leg after the anchor, per asset
for each asset in either vector (a missing side counts as 0),
at the finer of the two precisions:
    divergence  = venue amount - ledger amount
    tolerance   = max(0.1% × venue amount, 1 atom)
                  (USD keeps the one-cent floor: max(0.1% × venue, $0.01))
```

Examples: USD 1000.0000 has a tolerance of $1.00. USD 5.00 gets one cent,
because 0.1% is only half a cent. 0.5 BTC gets 0.0005 BTC (50000 atoms at 8
dp). 0.000001 BTC gets one atom (0.1% rounds down to zero). A ledger
holding 0.3765432109 BTC against a venue report of 0.37654321 BTC differs by
9 atoms at 10 dp, well inside its tolerance of 3765432 atoms.

**Running balances.** Some venues report the balance after each ledger row.
Koine checks these exactly, with no tolerance, in the order it stored them.
For each asset, the first reported balance starts the chain. Every later one
must equal the previous reported balance plus every leg of that asset in
between:

```
+100.0000  balance 100.0000   (chain starts)
 -30.0000  (no balance)
 -20.0000  balance  50.0000   100 - 30 - 20 = 50: ok
```

Had the venue reported 49.0000 on the last row, the chain would break
(expected 50.0000).

Any asset outside its tolerance, or any running balance that does not
follow, quarantines the strategy, as today. Every asset of every check is
stored, so the history of "did the ledger explain each balance?" is
auditable per asset.

## Asset valuation

Crypto and equity accounts hold more than one asset, so their equity is a sum
of positions, each valued in US dollars at a daily close. Pinned by
`tests/test_valuation.py`; the code is `alphamarket/valuation.py`.

**Holdings.** An account's holding of an asset at time `t` is the venue's own
balance at connect time (the anchor) plus every ledger leg in that asset up
to and including `t`. Quantities are exact integers at the precision the
venue reported. When a venue reports one asset at different precisions (Kraken
reports USD at 4 decimals in one place and 6 in another), every amount is
first rescaled to the finest precision seen before anything is added. Example:
12.3456 USD at 4 decimals plus 0.000001 USD at 6 decimals is 12.345601 USD.

**Equity at the end of each UTC day `d`:**

```
E_d = Σ cash assets (USD, stablecoins)  qty × 1.00
    + Σ coins and shares                qty × close_d
    + Σ open perpetual positions        qty × (close_d − entry)
```

- Stablecoins are valued at exactly 1.00. Every other asset uses its UTC daily
  close in US dollars. A perp uses its own contract's close when Koine has
  one, and otherwise the underlying coin's close.
- Closes are as-traded prices, not split-adjusted ones. A 2-for-1 split adds
  shares on the split day, the day the close halves, so equity does not move:
  10 shares × $200 = $2,000 before the split, 20 × $100 = $2,000 after it.
- Fees, funding, dividends, interest and tax withholding are ledger legs in
  whatever asset the venue booked them in, so they are already in the
  holdings. A fee paid in BNB is valued at BNB's close: an account holding
  1,000 USDT and 1 BNB (close $600) buys 0.01 BTC for 600 USDT with a fee of
  0.0075 BNB, and at closes BTC $60,000 and BNB $600 its equity is
  400 + 0.01 × 60,000 + 0.9925 × 600 = **$1,595.50** (it was $1,600; the fee
  cost 0.0075 × 600 = $4.50).
- A cross pair is valued through both closes: buying 10 ETH for 0.5 BTC
  leaves 0.5 BTC and 10 ETH, which is 0.5 × 60,000 + 10 × 3,100 = $61,000
  and, the next day, 0.5 × 62,000 + 10 × 3,050 = $61,500.
- A dividend is a return, not a deposit: 20 shares paying $0.25, with $0.75
  withheld, add $4.25 to equity ($2,000 → $2,004.25, a 0.2125% day).

**Missing prices.** If an asset has no close for day `d`, the last known close
before `d` is used and the day is flagged "carried forward". An asset with no
close on or before `d` is left out of that day's equity and flagged
"unpriced". Nothing is ever valued at a price Koine does not have. Example: 10
SOL and $50 cash with SOL closes of $150 on day 1 and $160 on day 3 give
$1,550, $1,550 (day 1's close carried forward, flagged) and $1,650.

**Rounding.** Every figure is computed exactly. Rounding happens once per
position per day: its dollar value is truncated toward zero to whole
micro-dollars (1 satoshi at $50,000.123456 is 500.00123456 micro-dollars,
counted as 500; a short of 1 satoshi counts −500, not −501). Equity is the
exact sum of those positions, so a day's equity is off by less than one
micro-dollar per position, and no position is ever shown larger than it is.

**Perpetual positions: average entry.** A perp position is valued as
`qty × (close − entry)`, where `entry` is the average-cost entry price:

```
opening from flat           entry = fill price
adding in the same direction entry = (|q| × entry + |fill| × price) / (|q| + |fill|)
reducing                    entry unchanged
closing exactly             no position
flipping through zero       entry = the flip fill's price
```

A position already open at connect time starts at the entry price the venue
reports. If the venue reports none, the position is left out of equity and
flagged until it closes or flips. Realized P&L is not recomputed. The
venue's own realized P&L, funding and fees are cash legs, and the formula
above values only what is still open. Worked example (BTC perp, $10,000 USDC
margin):

| Day | Fill | Position, entry | Cash legs | Close | Unrealized | Equity |
|---|---|---|---|---|---|---|
| 1 | buy 0.1 @ 60,000 | +0.1 @ 60,000 | fee −2.40 | 61,000 | 0.1 × 1,000 = 100 | 10,097.60 |
| 2 | buy 0.1 @ 62,000 | +0.2 @ 61,000 | funding −1.50 | 61,500 | 0.2 × 500 = 100 | 10,096.10 |
| 3 | sell 0.15 @ 63,000 | +0.05 @ 61,000 | realized +300 | 62,000 | 0.05 × 1,000 = 50 | 10,346.10 |
| 4 | sell 0.15 @ 60,000 | −0.1 @ 60,000 | realized −50, funding +0.80 | 59,000 | −0.1 × −1,000 = 100 | 10,346.90 |

Rounding applies the same way: after buying 1 @ $10 and 2 @ $11 and selling 1
@ $12, the entry is 32/3. At $10.50 two units are worth 2 × (10.5 − 32/3) =
−$1/3, counted as −333,333 micro-dollars.

**Exposure.** For each day: gross exposure is the sum of `|qty × close|` over
coins, shares and perps (cash is not exposure). Each underlying's share is
its part of gross exposure, with spot BTC and the BTC perp counted together.
Concentration is the largest share. Perp leverage is the sum of
`|perp qty × close|` divided by equity. Example: $10,000 USDC, 0.1 BTC and
1 ETH at $50,000 / $3,000, short 0.2 BTC perp entered at $51,000. Equity is
10,000 + 5,000 + 3,000 + 200 = $18,200. Gross exposure is 5,000 + 3,000 +
10,000 = $18,000. BTC is 5/6 of it and ETH 1/6. Perp leverage is
10,000 / 18,200 = 0.55×.

## External flows (time-weighted return)

Deposits, withdrawals and transfers in or out of the account move capital.
They do not earn anything, so they must not show up as gains or losses.
Koine uses a time-weighted return that takes them out.

Each day's external flow `F_d` is the dollar value of that day's deposit,
withdrawal and transfer legs at that day's closes, with the same pricing,
missing-price and rounding rules as equity (in `+`, out `−`). Flows are
assumed to arrive at the **end** of the day, so they never count as that
day's performance:

```
r_d = (E_d − F_d) / E_(d−1) − 1
TWR = Π (1 + r_d) − 1        over the days of the window after its first
```

A day whose previous equity is zero or negative has no return: the first
funded day starts the series, and an account that is emptied and refunded
resumes chaining on the next day. When the series skips calendar days, a
flow on a skipped day counts toward the next listed day.

Worked example: an account holds 1 BTC, BTC closes at $50,000, $55,000 and
$49,500, and the trader deposits 0.5 BTC on day 2.

| Day | Equity | Flow | Daily return |
|---|---|---|---|
| 1 | 50,000 | | |
| 2 | 1.5 × 55,000 = 82,500 | 0.5 × 55,000 = 27,500 | (82,500 − 27,500) / 50,000 − 1 = +10% |
| 3 | 1.5 × 49,500 = 74,250 | | 74,250 / 82,500 − 1 = −10% |

TWR = 1.10 × 0.90 − 1 = **−1%**, exactly the return of simply holding 1 BTC
(49,500 / 50,000 − 1). A cash deposit works the same way: $1,000 plus a $500
deposit and later a $200 withdrawal, with no trading, is a 0% return every
day.

For comparison Koine can also compute the **Modified Dietz** money-weighted
return. It answers a different question ("how did the dollars actually
invested do?") and is shown only beside the TWR, never in its place:

```
MD = (E_end − E_start − Σ F_d) / (E_start + Σ w_d × F_d),   w_d = (end − d) / (end − start)
```

In the example: (74,250 − 50,000 − 27,500) / (50,000 + ½ × 27,500) =
−3,250 / 63,750 = **−5.1%**. Adding capital just before a loss makes the
money-weighted figure worse, while the time-weighted figure, which measures
the trader's decisions, stays at −1%.

Returns are computed exactly (as fractions) and are only rounded for display.

## Kraken (spot)

**Where each number comes from.** Kraken's `Ledgers` endpoint lists every
change to every asset balance, with the balance after it. Kraken defines

```
balance = previous balance + amount - fee          (per asset, per ledger row)
```

so each row becomes a leg of `amount` plus a fee leg of `-fee`, and the
reported `balance` is checked exactly against our replay after that row.

**Trades.** A trade's legs are its ledger rows (the rows whose `refid` is the
trade id), not the trade's own `cost` and `fee` fields: Kraken prints trade
cost to the pair's cost precision (5 decimals for USD pairs) but moves the
USD balance at 4 decimals, so only the ledger rows reproduce the balance. The
asset that moves with the trade direction (up on a buy, down on a sell) is the
base, the other is the quote, and every row's fee is a fee leg in that row's
asset (Kraken fee credits, KFEE, included). The trade's own price, volume and
side describe the fill; they are cross-checked against the ledger rows and a
disagreement beyond one unit of the last decimal is logged.

Worked example (tested): buy 0.02 BTC at 30010.0, ledger rows BTC +0.0200000000
and USD -600.2000 with fee 1.5605: legs BTC +0.02, USD -600.2000, fee USD
-1.5605, and 2000.0000 - 600.2000 - 1.5605 = 1398.2395 is the USD balance
Kraken reports after it.

**Everything else** is one event per ledger row: deposits and withdrawals are
external flows; staking rewards are income; moving coins into or out of a
staked or earning balance (ETH to ETH2.S, Earn allocations) is a conversion
inside the account, not a flow; transfers to Kraken Futures and unlabelled
transfers (airdrops) are external flows, so they never count as a return;
margin rollover is interest; anything unrecognised is an audited adjustment.

**Assets.** Kraken's codes are renamed to the common ones (XXBT is BTC, ZUSD is
USD). Staked and earning balances (ETH2.S, DOT28.S, BTC.M) are kept as their
own assets because Kraken keeps a separate running balance for each, and are
valued at the price of the underlying coin. USD, USDC and USDT (and their held
or earning variants) are valued at 1.00.

**Read-only check.** Kraken cannot describe a key's permissions, so we ask it
to validate an order that is never placed (`validate=true`) and to list
withdrawal methods; both must be refused with "Permission denied", while
balance, trade and ledger reads succeed. The check reruns nightly.

## Hyperliquid accounts

Hyperliquid is an on-chain exchange: every account is a public address, and
we read it through Hyperliquid's public API without any key. Terms, consent
and limits: `docs/venues/hyperliquid.md`.

**Ownership.** Anyone can paste an address, so the page stays hidden until
the trader signs a Koine message with the wallet (EIP-191 `personal_sign`)
and the signature recovers that address, the master of a sub-account, or
the leader of a vault. Signing is not a transaction.

**What becomes a ledger row.** Quantities are stored exactly, in the
venue's own decimals (perp sizes at the coin's `szDecimals`, spot tokens at
their `weiDecimals`, USDC at 8 places).

```
perp fill      position  "<COIN>-PERP"  +size (buy) / -size (sell)
               cash      +closedPnl                 (realized P&L, USDC)
               fee       -fee in feeToken           (includes any builder fee;
                                                     a maker rebate is positive)
spot fill      base      +size / -size
               quote     -size x price / +size x price   (exact product)
               fee       -fee in feeToken
funding        cash      +usdc (negative when paid), one row per hour and coin
transfers      deposits, withdrawals (+ the $1 fee), sends to or from other
               addresses, vault and staking moves: external flows, so the
               time-weighted return is chained across them, never a gain
own moves      spot <-> perp between your own balances: no change at all
liquidation    recorded as an event; the cash moves in the liquidation fills
```

Worked example (recorded fill, 2026-09-25): closing 73,874 XPL long at
0.11013 with `closedPnl` -286.852742 and fee 1.220361 books the position
-73,874 XPL-PERP and cash -286.852742 - 1.220361 = -288.073103 USDC.

**Balance check.** The venue's own view is rebuilt as a vector: spot token
totals, plus (for accounts in standard mode) each perp venue's account value
minus the unrealized P&L of its open positions, plus the open positions
themselves with their entry prices. Accounts in Hyperliquid's unified or
portfolio-margin mode keep all cash in the spot balances, so the perp account
value is not used for them. The USDC account value we compare equity with is
USDC cash plus the unrealized P&L of USDC-margined positions.

**Limits.** Hyperliquid serves only the 10,000 most recent fills of an
address. Older history is represented by the balance we record when the
account connects (the anchor), and the page says from which date fills are
individually verified. Staking rewards and borrow/lend interest are treated
as flows, not returns (a small understatement), until they are verified
against a real account.

## Alpaca accounts (US stocks and ETFs)

**Source.** Alpaca's account activities (every fill and every non-trade cash
or share movement, each with a stable id), the account's cash and equity,
and its open positions, read with a read-only OAuth grant.

**Units.** Shares are counted to 9 decimal places (Alpaca sells fractional
shares); dollars to 6 (micro-dollars). A fill moves two amounts:

```
buy  q shares at price p :  shares +q ,  cash -(q x p)
sell q shares at price p :  shares -q ,  cash +(q x p)
q x p is rounded half-even to $0.000001; it is exact for every whole-share
fill at a cent price. Example: 0.123456789 x 174.78 = 21.57777758142
-> $21.577778.
```

Regulatory fees (SEC fee, FINRA trading activity fee) are not part of the
fill: Alpaca books them as separate fee entries, usually the next day, and
they are counted as fees on that day. Dividend withholding (non-resident or
backup withholding, foreign tax) is recorded as tax, never as a trading
cost. Deposits, withdrawals and journals are external flows: they change the
capital, not the return. A stock split changes the share count with no cash;
cash paid in lieu of a fractional share is recorded with it. Non-trade
entries that Alpaca dates without a time are placed at 23:59:59 UTC of that
date.

**What is not supported yet.** Short sales, options, crypto, stock
dividends, mergers, spin-offs and symbol changes on the account are not put
into the ledger. When one appears the account carries a visible
"unsupported activity" flag, because numbers after it may not reconcile;
nothing is estimated in its place.

**Reconciliation.** Nightly, the ledger's cash and share counts are compared
with Alpaca's reported cash and positions, and the valuation with Alpaca's
reported account equity.

**Prices.** Daily closes are Alpaca's unadjusted daily bars (share counts
are adjusted by the split entries themselves, so prices must not be
adjusted twice). A bar belongs to its New York trading date.

<!-- wave2:market-data -->
## Daily prices and fallback pricing

Every number that values a coin, a share or a perpetual contract in US
dollars comes from a daily close stored in `prices_daily`. The code is
`alphamarket/prices.py`; `tests/test_prices.py` and `tests/test_coingecko.py`
pin the rules below with hand-computed values.

**Which instrument prices an asset.** The venue whose public market data
prices the account (the venue itself, except IBKR and SnapTrade accounts,
which are priced from Alpaca's market data) picks one instrument per asset
with one rule: the first spot or equity instrument whose base is the asset and
whose quote is USD, then USDC, then USDT. A staked or bonded Kraken variant
(`ETH2.S`) is priced as its underlying coin. A perpetual contract (`BTC-PERP`)
is priced only by that venue's own perpetual contract, never by the spot coin.
Cash and stablecoins need no price: they are worth 1.00. That is USD, USDC
and USDT, plus each venue's own codes for them: Kraken's held and earning
balances (USD.HOLD, USD.M, USDC.M, USDC.F, USDT.M, USDT.F) and the tokens
Hyperliquid's HIP-3 markets settle in (USDT0, USDH). One list is used for the
creator's equity, the statistics, signals and paper books alike.

**Fallback pricing.** An asset the venue does not list is priced from
CoinGecko, which is a source of daily prices only (no quotes, no fills, never
a venue). Koine keeps a curated list of asset codes and CoinGecko coin ids and
never searches by ticker, because tickers collide: an asset that is not on the
list stays unpriced, and its holding is shown without a value instead of being
matched to whatever coin shares its ticker. Every fallback close is labelled
`source = coingecko`, and the pricing venue's own close always wins when both
exist.

**Which day a close belongs to.** The close of UTC day `d` is the last price of
that day. Exchange candles are read as the venue publishes them (Alpaca stamps
a bar with its New York trading date). CoinGecko publishes a price at an
instant, so the close of day `d` is its price at 00:00 UTC of `d + 1`: the
sample nearest to that midnight within 30 minutes, the earlier one on a tie,
and no close at all if there is none that near. The day still in progress has
no close.

**From close to dollars.** A close is stored exactly as the source sent it. Its
USD value per unit is that decimal times 1,000,000 micro-dollars, truncated
toward zero, and only when the quote asset is USD or a stablecoin held at par
(the list above):

```
61234.5 USD          -> 61,234.5 x 1,000,000       = 61,234,500,000 micros
100.5 USDT           -> 100.5 x 1,000,000          = 100,500,000 micros (par)
61234.56789123 USD   -> 61,234,567,891.23          -> 61,234,567,891 micros
0.0000004 USD        -> 0.4                        -> 0 micros
0.052 BTC            -> no value (quoted in BTC; it would need a second price)
```

The decimal is never passed through a floating-point number, so no digit of a
sub-cent price is lost before the truncation. The stored close keeps every
digit, which is what lets very small prices be valued exactly elsewhere.

**Which days are fetched.** For each priced instrument, only the days that are
missing: from the day before the earliest account anchor or paper allocation
start that holds the asset (the creator's equity at a fill uses the previous
complete close, so a fill on the anchor day needs it) up to yesterday UTC. A
day of the last four that the source has not published yet is asked for again
by the next night's request, so a close published late is still stored; an
older day without a close (weekends and holidays for stocks) is taken as one
the source never publishes and is not asked for again. A close that is already
stored is never rewritten.

**Perpetual contract margin tiers.** For Hyperliquid perpetuals Koine stores
the contract's maintenance-margin tiers, refreshed once per UTC day: a list of
`(notional lower bound in USD, maximum leverage)` ascending by lower bound, read
from the venue's published margin tables. A contract whose table is not
published gets one tier from its own maximum leverage. [VERIFY] that the
maintenance rate 1 / (2 x maximum leverage) of a tier is the venue's definition
for every table.

**Quotes, funding and corporate actions.** The latest quote of an instrument held
in an open paper book is stored with its time and source. It is a mark only while
younger than `QUOTE_MAX_AGE_SECONDS` (15 minutes): the mid of the bid and ask when
both are quoted, otherwise the last trade; an older quote is not a mark. Funding rates of held
perpetuals are stored as the venue publishes them, one row per interval. Splits,
cash dividends and symbol changes are stored for US stocks held by paper books,
looked ahead 30 days so an announced action is known before its ex-date, and
re-read for the last 14 days each night.

<!-- wave2:stats-v2 -->

## Asset strategy statistics

Crypto and stock strategies are measured on the daily equity of the
section "Asset valuation", with deposits and withdrawals taken out as in
"External flows (time-weighted return)". Pinned by
`tests/test_analytics_v2.py`; the code is `alphamarket/analytics_v2.py`.

**Which days.** One point per UTC day, from the day the account was
connected (the anchor day) to the last *complete* UTC day. Today is never in
the series, because today has no close yet, and no figure on the page uses
a close before its day is complete.

**Return index.** A growth-of-$1 index built from the flow-adjusted daily
returns:

```
I_first = 1
I_d     = I_(d−1) × (1 + r_d),    r_d = (E_d − F_d) / E_(d−1) − 1
```

A day with no capital at risk the day before has no return, and the index
stays flat across it.

| Statistic | Formula |
|---|---|
| Total return | `I_last − 1`, the time-weighted return |
| Max drawdown | `max over d of (peak_d − I_d) / peak_d` on the index, not on raw equity |
| Sharpe | `mean(r) / stdev(r) × √365` over the days with a return; sample stdev |
| Sortino | `mean(r) / √(mean(min(r,0)²)) × √365` |
| Win rate | not shown: a coin or a share has no single "trade" that wins or loses |
| P&L, net of deposits | last day's equity - first day's equity - net deposits and withdrawals; the headline dollar figure on an asset strategy's page (spot exchanges and brokers report no realized-P&L entries) |
| Realized P&L | the venue's own realized-P&L and liquidation entries, in dollars (a stablecoin at 1.00, anything else at its close that day, or the last close before it) |
| Money-weighted return | Modified Dietz, shown only beside the TWR |
| Worst periods | the two deepest runs of days below the running peak of the drawdown series: from the last day at the peak to the deepest day, with the days until the first day back at a peak ("not yet recovered" while below); listed after 90 verified days, for every strategy |

Drawdown and the ratios read the index, so a deposit can never look like a
gain and a withdrawal can never look like a loss. Worked example: equity
$10,000, $10,500, then $13,605 on a day the trader deposited $3,000, then
$12,244.50.

| Day | Equity | Flow | r_d | Index |
|---|---|---|---|---|
| 1 | 10,000 | | | 1 |
| 2 | 10,500 | | 10,500 / 10,000 − 1 = +5% | 1.05 |
| 3 | 13,605 | 3,000 | (13,605 − 3,000) / 10,500 − 1 = +1% | 1.0605 |
| 4 | 12,244.50 | | 12,244.50 / 13,605 − 1 = −10% | 0.95445 |

Total return is 0.95445 − 1 = **−4.555%**. Reading the raw equity instead
(12,244.50 / 10,000 − 1) would claim **+22.4%**: that is the deposit, not
skill. Max drawdown is (1.0605 − 0.95445) / 1.0605 = **10%**. The
money-weighted figure for the same days is
(12,244.50 − 10,000 − 3,000) / (10,000 + ⅓ × 3,000) = −6.87%.

**Risk score.** The same 1-to-10 formula as "Risk score", with the TWR
drawdown, the downside deviation of the daily returns above, and, as the
concentration term, the largest share of gross exposure on the last day
(spot and perp of one coin count together). The stake term is 0: a coin
account has no single "stake". Leverage is shown as a flag instead.

**Holdings and exposure** are those of the last day in the series, at that
day's closes. Next to them, the "marked equity" values the holdings right
now at the freshest public quote, or at the last complete day's close when
no quote is fresh. On the day an account connects there is no complete day
yet: holdings are shown at the previous day's closes and no return is shown.

**Flags** (displayed, never used to delist):

- **unpriced_assets**: on some day an asset held had no close at all, so it
  was left out of that day's equity.
- **carried_forward_prices**: a close older than the day it valued was used.
  Stock venues are allowed 4 calendar days (weekends and market holidays
  have no close); crypto, which trades every day, is allowed none.
- **single_asset_concentration**: one underlying is more than 50% of gross
  exposure on the last day.
- **high_leverage**: on some day the perp notional, `Σ |perp qty × close|`,
  was more than 5× equity.
- **broker_equity_divergence**: the newest broker equity check (below)
  failed.

**Exact closes.** A close is kept as the source's decimal string whenever
Koine has it, so a coin worth a fraction of a cent is not rounded before it
is multiplied: 1,000,000,000 tokens at $0.00001234 are worth exactly
$12,340, where a close rounded to whole micro-dollars ($0.000012) would
say $12,000. The rounding rule of "Asset valuation" (truncate once, per
position) is unchanged.

## Broker equity check

Some venues report their own total for the account: Alpaca's account
equity, IBKR's net asset value, Hyperliquid's account value. At every
reconciliation Koine compares that figure with its own equity for the same
moment: the holdings from the ledger, valued at the freshest public quote
(or the last daily close), with perp positions at `qty × (price − entry)`.

```
divergence = venue equity − Koine equity
tolerance  = max($1.00, 0.5% × venue equity)
ok         = |divergence| ≤ tolerance
```

Example: Koine computes $10,000. A venue figure of $10,040 is within
tolerance (40 ≤ 50.20); $10,100 is not (100 > 50.50). For a $150 account
the $1 floor applies (0.5% would be 75 cents).

The tolerance is wider than the per-asset check in "Reconciliation v2"
because the two sides use different prices taken at slightly different
moments. For the same reason **a failed check never quarantines a
strategy**: the per-asset balance check is what proves the ledger. A
failed equity check shows the broker_equity_divergence flag on the
strategy until a later check passes. Venues that report no total skip
the check.

<!-- wave2:p6-signals -->
## Signals v2 and copy lag

**What a signal is.** For stocks, coins and perpetual futures, every fill the
creator made after connecting (strictly after the anchor) becomes one
signal, in fill order. A fill at or before the anchor never does, and
deposits, withdrawals, transfers, staking, dividends and funding are never
signals. A signal is a fact about a trade that already happened, never a
recommendation, and it is never edited afterwards.

**What it records.**

```
qty, price          exactly as the venue reported the fill
notional            qty x price in USD, rounded down to $0.000001;
                    only when the pair is quoted in USD or a stablecoin at par,
                    otherwise left empty (a BTC-quoted pair has no USD notional)
position before     the creator's signed holding of the asset just before the fill
position after      before + qty for a buy, before - qty for a sell
side                long or short by the sign of the position after the fill
                    (before it, when the fill closed the position)
creator equity      cash + holdings x last complete daily close + perp
                    unrealized P&L, just before the fill (below)
```

Spot coins and shares take the position from the ledger (every entry before
the fill); perps take it from the perp position built from the creator's
perp fills. When one order fills several times at the same instant, the
fills chain: the second fill's "before" is the first fill's "after", and all
of them share one equity figure (none of them is "before" the others in
time). A holding recorded more precisely than the venue reports fill sizes
is cut to the fill's precision (dust below it is dropped).

**Creator equity at the fill.** The denominator followers are sized
against:

```
E(t-) = cash + sum(qty x close of day D-1) + sum(perp qty x (close of D-1 - entry))
```

where D is the fill's UTC day, holdings include everything strictly before
the fill, and the closes are those of the last complete UTC day (carried
back further if a day is missing), so no intraday price is assumed. The day
whose closes were used is stored with the signal. Assets with no close at
all are left out, exactly as in the daily equity series. When the result is
not positive the equity is stored as unknown and followers do not copy the
signal.

Worked example: anchored with $5,000 and 1 ETH, ETH closed at 3,000 on D-1,
and the creator buys 0.5 ETH at 3,100 on D. Equity before the fill is
5,000 + 1 x 3,000 = $8,000 (D's own close is not used); the position goes
1 -> 1.5 ETH; the notional is 0.5 x 3,100 = $1,550, which is 19.375% of
equity. Two sells of 0.2 and 0.3 ETH at the same later instant both use
equity 3,450 + 1.5 x 3,000 = $7,950, and the position goes 1.5 -> 1.3 -> 1.0.

**Copy lag.** Each signal stores when the fill happened and when Koine
first saw it (`observed_at`, the moment the fill was imported). The copy
lag is the difference. At that moment Koine takes one public quote per
instrument (bid, ask, last and its time and source) and stores it on the
signal: paper followers buy at that ask and sell at that bid, which is the
cost of copying late. A signal whose lag already exceeds the venue's limit
(6 hours; 3 days for venues that report once a day) gets no quote and is
not copied (it is still shown). If no quote can be had (the data budget is
spent or the source is down), the signal is stored without one and paper
followers use the creator's price with a stated penalty instead.

**Streaming shortens the lag, nothing else.** For creators whose strategy
has active paper copies, Koine may hold a live connection to the venue
(Hyperliquid's fill feed for the most-followed creators, and on Kraken or
Coinbase a notice that triggers an immediate import). A fill imported that
way is the same fill, with the same id and the same ledger entries, as the
one the regular import fetches minutes later; it is stored once, whichever
arrives first, and `observed_at` is simply earlier. The regular import stays
the record of truth: when a live connection drops, the next regular import
picks up anything it missed, and the only effect is a longer copy lag on
those signals.

<!-- wave2:p6-engine -->
## Paper copying v2: sizing, fill price, fees

Paper allocations of crypto and equity strategies (Kraken, Hyperliquid,
Coinbase, Binance.US, Alpaca, IBKR, SnapTrade) follow the creator's verified
fills with virtual capital. Nothing is placed anywhere, and a live mode does
not exist: asking for one is refused in code. Prediction markets keep the
rules in "Paper tailing" above. Every number below is reproduced by a
hand-computed test (`tests/test_paper_engine.py`).

**Which signals are copied.** A follower copies only signals filled after the
allocation started. A signal Koine observed more than 6 hours after the fill
(3 days on venues that only publish daily reports: IBKR, SnapTrade) is
skipped as stale, because a follower could not have traded near that price.
Instruments not quoted in USD or a stablecoin held at par (see "Daily prices
and fallback pricing") are skipped. So are instruments the venue lists that
Koine marks **not copyable** ("not copyable": synthetic US stocks and ETFs,
pre-IPO names, currencies and commodities on crypto perp venues, until
counsel confirms they may be paper-copied for US users); the creator's
signal is still recorded and shown, only the paper copy is skipped. Every skip is recorded with its reason and
counted on the follower's portfolio. A signal that makes the paper engine
fail is skipped on its own ("engine error") and logged for us to fix, so it
never holds back the follower's later signals, funding or liquidation check.

**Fill price.** A paper fill happens when Koine observed the creator's fill,
at the venue's public quote captured at that moment: buys at the ask, sells
at the bid. When no quote was available, the creator's own price is moved
against the follower by 0.5% (50 basis points):

```
buy:   price = creator_price x 1.005
sell:  price = creator_price x 0.995
```

The fill records which rule priced it ("ask", "bid" or
"creator_price_penalty") and the quote's timestamp.

**Late signals.** A paper fill is never dated before an event already in the
follower's book (funding, a dividend, an earlier paper fill). If Koine's own
processing fell behind and a signal arrives after such a later event was
booked, the fill keeps its price but takes that event's time, so no booked
payment is ever recomputed against a position it did not see. For a stock,
if that later time would cross the ex-date of a split, the signal is skipped
("late signal") instead: its size is in pre-split shares.

**Sizing.** F is the follower's equity (cash plus holdings at the freshest
mark, below) and C the creator's equity just before the fill.

```
spot / equity buy:   q = creator_qty x F / C
perp open or add:    q = creator_qty x creator_price x F / C / fill_price
                     (the notional scales, so the follower's leverage
                      equals the creator's)
reduce (any kind):   q = follower_position x closed_qty / creator_position_before
                     (the same fraction; all of it when the creator went
                      flat or flipped)
perp flip:           close the follower's whole position, then open the
                     remainder sized as an open; one fill of the sum
```

Quantities are floored to the instrument's step. An opening order below the
venue's minimum quantity or minimum order value is skipped, and so is a
partial reduction below them; a full exit never is. A spot or equity buy is
capped at the largest stepped quantity whose cost plus fee fits in the
follower's cash. Spot and shares are never sold short on paper. If the
follower's position has drifted from the creator's (a closing fill was
skipped as stale, say), any part of an order that runs against the
follower's own perp position closes it and realizes its profit or loss.

**Rounding.** Always against the follower: a buy's cost rounds up to the
micro-dollar, a sale's proceeds round down, realized perp profit rounds down
and a realized perp loss rounds up.

**Fees.** By the venue's fee model:

```
crypto (bps of notional):  fee = qty x price x taker_rate, rounded half up
                           to the venue's fee precision
                           (Kraken 0.80% to $0.0001; Hyperliquid 0.05% to $0.000001)
US equities:               buys: $0 (commission-free)
                           sells: SEC fee  = notional x SEC rate per $1M, up to the cent
                                + FINRA TAF = min(shares x TAF rate, TAF cap), up to the cent
```

The SEC and FINRA rates are listed with the date they were checked; they
change by regulatory order.

**Worked example (Kraken, 0.80% taker).** A $10,000 creator buys 0.1 BTC; the
quote has an ask of 60,030. A $1,000 follower buys 0.1 x 1,000 / 10,000 = 0.01
BTC: cost 600.30, fee 4.8024, cash left 394.8976. The creator then sells 0.04
of its 0.1 (40%) with the bid at 60,990.5: the follower sells 40% of 0.01 =
0.004 BTC for 243.962, fee 1.9517, cash 636.9079, 0.006 BTC left. A $100
follower of a $1,000,000 creator buying 0.1 BTC would buy 0.00001 BTC, under
Kraken's 0.0001 BTC minimum, so that signal is skipped.

**Worked example (Hyperliquid perp, 0.05%).** A $10,000 creator buys 1 BTC
(60,000 notional, 6x). A $1,000 follower mirrors 6,000 of notional at the ask
60,010: 0.09998 BTC (5 decimals), fee 2.9999, cash 997.0001 (perp margin stays
in cash). The creator then sells 3 at 61,000 (long 1 to short 2) with the bid
at 60,990: the follower closes its 0.09998 (realized 0.09998 x 980 = 97.9804)
and opens 2 x 61,000 x 997.0001 / 10,000 / 60,990 = 0.19943 short, one sale of
0.29941 (notional 18,261.0159, fee 9.130508), cash 1,085.849992, short 0.19943
at an entry of 60,990.

**Worked example (US equity sell).** Selling 20 shares at $80 (notional
$1,600) with the SEC rate at $0.00 per million and FINRA TAF at $0.000166 per
share: SEC fee $0.00, TAF 20 x 0.000166 = $0.00332, rounded up to $0.01.

**Valuing the follower.** Each holding is valued at the freshest mark: the
mid (or, without both sides, the last trade) of a public quote no
older than 15 minutes, else the last daily close,
else (never priced yet) its cost, so an unpriced holding never shows as a
loss. A perp position is worth quantity x (mark - entry), with the entry
averaged over the follower's own fills; while unpriced it adds nothing. The
drawdown stop reads this equity exactly as for prediction markets. The
portfolio shows the median copy lag (observed time minus fill time) of the
signals the follower copied, and how many were skipped and why.

<!-- wave2:p6-lifecycle -->
## Paper copying v2: funding, liquidation, corporate actions

A paper allocation that copies a perp or stock strategy keeps living between
signals: perps pay or receive funding and can be liquidated, and shares are
split and pay dividends. Each event is applied once (it is keyed by its
source row), on every mirror pass, including for an allocation that has
stopped copying, because what it still holds keeps accruing. Every amount is
computed exactly, then rounded to the micro-dollar against the follower:
credits round down and debits round up.

**Funding.** For each funding rate the venue publishes for a perp the
follower holds (not before the follower first traded it):

```
payment = -q x mark x rate        (q signed: + long, - short)
mark    = the rate's own mark price, else a quote within 15 minutes of it,
          else the last complete daily close before that day
```

A positive rate means longs pay and shorts receive it. Some venues charge the
two sides differently (GMX: adaptive funding plus borrowing per side) and
publish what shorts pay as its own rate (positive means shorts pay). Then a
short pays its own side's rate instead:

```
long  (q > 0):                     payment = -q x mark x rate
short (q < 0), short rate given:   payment =  q x mark x short_rate
short (q < 0), no short rate:      payment = -q x mark x rate   (as above)
```

Example: short 2 ETH at mark 2,500 with rate 0.0001 and short rate 0.00003
pays 2 x 2,500 x 0.00003 = 0.15 (-150,000 micro-dollars); on a venue without a
short rate the same short would receive 2 x 2,500 x 0.0001 = 0.50.

A rate with no position at that moment pays nothing. A rate with no mark yet
is never estimated: it is retried on later passes (the day's close or a
quote may still arrive) for 7 days, then given up. Example: 0.09998 BTC long, rate
+0.0000125 at mark 60,000 pays 0.074985; rate -0.00001 at mark 59,000
receives 0.0589882, credited as 0.058988. Cash 997.0001 becomes 996.925115,
then 996.984103.

**Liquidation (Hyperliquid, cross margin).** Checked whenever every open perp
has a mark no older than 15 minutes; with any mark stale, there is no check.

```
notional = |q| x mark
mmr      = 1 / (2 x max leverage of the tier with the largest lower bound <= notional)
MM       = sum of notional x mmr
AV       = cash + sum of q x (mark - entry)
AV < MM  ->  every perp is closed at the mark and the allocation stops ("liquidated")
```

The P&L of each closed position is realized at the mark (a liquidation entry,
no extra fee). A perp whose venue publishes no margin tiers is never given a
guessed leverage: it adds nothing to MM, and with no tiered position there is
no check. Example (BTC, max leverage 40, mmr 1.25%; 0.09998 long at 60,010,
cash 996.984103): at 51,000, AV = 996.984103 - 900.8198 = 96.164303 and MM =
5,098.98 x 1.25% = 63.73725, so nothing happens; at 50,500, AV =
996.984103 - 950.8098 = 46.174303 < MM = 63.112375, so the position is
closed, realizing -950.8098 and leaving cash 46.174303.

**Splits.** Applied at 00:00 UTC on the ex-date to the shares held just
before it: new shares = floor(held x new / old), at the venue's share
precision. A fraction the split would create is dropped, with no cash in
lieu. 10 shares in a 2-for-1 split become 20; the cash spent on them is
unchanged. 7 whole shares in a 1-for-3 reverse split become 2.

**Cash dividends.** Shares held at the end of the day before the ex-date x
cash per share, credited at 00:00 UTC on the pay date and not before (a
dividend whose pay date is not yet known waits). Shares bought on the
ex-date do not count. 20 shares x $0.25 = $5.00.

**Not modelled yet.** Symbol changes are not applied to paper books. A
follower who still holds a stock whose symbol changed sees it on the
portfolio: the position stays under the old symbol at its last price, and
the creator's trades in the new symbol cannot reduce it. When two data
sources report the same action (same symbol, kind and ex-date) it is applied
once.

## When a tail stops

A paper tail stops when its drawdown passes the follower's limit, when the
follower presses Stop, when the follower removes a watched trader they were
tailing, or when the trader opts out of being watched. From then on no new
trade is copied, and every crypto and stock position is closed: the follower
sees what the tail actually ended with, not a paper position left open
forever. Prediction-market positions are not sold: they run to settlement,
as described under "Paper tailing".

**Price.** Each position closes at the venue's public quote, on the side a
real order would take:

```
spot or shares (sell)   price = bid
long perp (sell)        price = bid
short perp (buy back)   price = ask
```

The quote is the freshest one no older than 15 minutes that has that side.
A last trade or a mid is never used for a close, and neither is an older
quote. If there is none (a stock after the market closed, or market data
down), the tail is already stopped and shows "Closing": that position closes
at the first fresh quote, on a later pass, while positions that do have a
quote close at once. Nothing is ever closed at a stale price. A closing
tail keeps asking for quotes for as long as it holds something, however
long ago it stopped, and its tail page names each position still waiting
and when it was last quoted. One still waiting a week after the stop (an
instrument the venue stopped quoting, say) is flagged to Koine to look at;
it is still never closed at an old price.

**Fees and P&L.** Exactly as for a copied sale, with the same formulas
("Paper copying v2: sizing, fill price, fees"); the close is never skipped
for a venue minimum or for cash:

```
spot / shares:  cash += qty x bid (rounded down) - fee
                fee   = qty x bid x taker rate, half up to the venue's precision (crypto)
                      = SEC fee + FINRA TAF, each up to the cent (US shares)
perp:           realized = qty x (price - entry) x side   (side +1 long, -1 short;
                           a gain rounds down, a loss up)
                cash += realized - fee,  fee = qty x price x taker rate
```

The close is dated when it happens. Each instrument closes once per tail:
if a liquidation already closed the perps, the stop has nothing left to
close, and a stop that has run never runs again. The drawdown stop records
the equity that crossed the limit (before the close) as that day's value;
the next value shows the close. Once flat, a stopped tail's value stops
moving, apart from funding or a dividend already owed for the time it held
the position.

**Worked example (Kraken, crypto).** 0.01 BTC bought at 60,030, cash
394.8976. Stopped with the bid at 60,990.5: proceeds 0.01 x 60,990.5 =
609.905, fee 609.905 x 0.80% = 4.87924, rounded to 4.8792. Cash 394.8976 +
609.905 - 4.8792 = 999.9234.

**Worked example (Hyperliquid, short perp).** 0.19943 BTC short at an entry
of 60,990, cash 993.918382. Stopped with the ask at 60,500.5: realized
0.19943 x (60,500.5 - 60,990) x (-1) = 97.620985; fee 0.19943 x 60,500.5 =
12,065.614715 x 0.05% = 6.0328073575, rounded to 6.032807. Cash 993.918382
+ 97.620985 - 6.032807 = 1,085.50656.

**Worked example (US shares, after hours).** 20 shares, cash 8,400, stopped
at 20:30 UTC after the close: the last quote is 40 minutes old, so the tail
shows "Closing" overnight. The next morning's first quote has the bid 81.25:
proceeds 1,625.00; at an SEC rate of $27.80 per million the SEC fee is
1,625 x 27.80 / 1,000,000 = 0.045175, up to 0.05, and the FINRA TAF 20 x
0.000166 = 0.00332, up to 0.01. Cash 8,400 + 1,625 - 0.06 = 10,024.94.

**A split Koine hears of after the close.** A stock split takes effect at
the open of its ex-date, so a close after that date takes a post-split
quote. If the split's data reaches Koine only after the close, the close
sold the pre-split number of shares at that post-split price, and the
split, booked on the shares held before its ex-date, puts the difference
back in the book. Those shares are closed at the stop close's own price
and time (the quote that was fresh then, never a later or an older one),
and the cash and fee are corrected so that the whole stop close equals
one sale of the right number of shares:

```
correction cash = proceeds(shares after split) - proceeds already booked
correction fee  = fee(shares after split) - fees already booked
```

A reverse split works the other way: the extra shares the close sold are
bought back at the same price, and the fee they paid is refunded. Fees do
not simply add up share by share (each fee is rounded up to the cent per
sale, and FINRA's TAF is capped), which is why the correction is the
difference, not a second sale.

**Worked example (late split).** 20 shares bought at 80, cash 8,400. A 2:1
split goes ex on Friday; the tail stops Monday with the bid at 40.60, and
the split's data arrives Tuesday. Monday's close sold 20 at 40.60: proceeds
812.00, SEC fee 812 x 27.80 / 1,000,000 = 0.0225736, up to 0.03, TAF 20 x
0.000166 = 0.00332, up to 0.01; cash 8,400 + 812 - 0.04 = 9,211.96. Split,
then closed, the tail would have sold 40 at 40.60: proceeds 1,624.00, SEC
0.0451472 up to 0.05, TAF 0.00664 up to 0.01, cash 8,400 + 1,624 - 0.06 =
10,023.94. The correction sells the 20 split shares at 40.60, dated Monday:
cash 1,624.00 - 812.00 = 812.00, fee 0.06 - 0.04 = 0.02 (a separate sale
of 20 would have paid 0.04). Cash 9,211.96 + 812 - 0.02 = 10,023.94.

<!-- wave2:p8-pages -->

## Reading an asset strategy's page

A strategy on a crypto or stock venue is valued by the asset statistics (the
time-weighted return, see "External flows"), and its page is drawn from that
series, never from the raw equity curve. A deposit or a withdrawal changes
the money in the account, not the return, so it must not bend the picture.

```
index(d)     = growth of 1.0 with external flows removed (time-weighted)
growth(d)    = equity(f) / index(f) x index(d)       the chart, in dollars; f is the
               first day with positive equity (index(f) = 1.0 for an
               account that opened funded or was funded on day f)
drawdown(d)  = (max over s <= d of index(s) - index(d)) / max over s <= d of index(s)
month return = index(month end) / index(prior month end) - 1
               (the first month is measured from the first day's index)
```

Worked example: equity 10,000, 10,500, 13,605 (a 3,000 deposit on day 3,
counted at the end of the day as above), 12,244.5. Day returns are
10,500 / 10,000 - 1 = +5%, (13,605 - 3,000) / 10,500 - 1 = +1% and
12,244.5 / 13,605 - 1 = -10%. The index is 1.0, 1.05, 1.0605, 0.95445, so the return
is -4.555% and the worst drawdown 10% (peak 1.0605 on day 3). The raw
equity would say +22.4%, which is the deposit counted as profit. The chart
plots 10,000, 10,500, 10,605, 9,544.5. The page still lists the actual equity
in its JSON, because that is also true.

**Holdings and exposure.** The holdings panel lists the positions at the last
complete UTC close, largest first, each at the day's close and valued as
described under "Asset valuation". Cash is not exposure.

```
share of a holding = value / sum of the cash, spot and equity values (perps excluded)
gross exposure     = sum over spot, equity and perp of |notional|
largest exposure   = the underlying with the largest share of gross (BTC spot and
                     BTC-PERP count together)
perp leverage      = sum over perps of |quantity x close| / equity
```

A perpetual's value is its unrealized profit or loss, quantity x (close -
entry), so the panel shows its notional separately and does not give it a
share. An asset with no price, or a perp with no known entry, is listed under
the table as not valued and carries no value; days valued at the last
available close are counted. Nothing is estimated.

**Verification tiers for asset venues.** *Reconciled*: an intraday venue whose
cash and holdings are re-read and checked every night. *Daily verified*:
a venue that reports end of day only (Interactive Brokers Flex statements,
SnapTrade). The record is checked against the latest daily statement, so it
is verified at day resolution and intraday activity between statements is
not independently checked; the page says so. Where a broker reports its own
account equity, the page shows it next to the equity Koine computes; a
difference beyond the tolerance flags the statistics and never hides them.

**What proves the credential cannot trade.** Each page states the method for
its venue: a read-only API key whose scope is read back from the venue; a key
proven read-only because a validate-only order or withdrawal is refused; a
read-only OAuth grant approved inside the account; a reporting-only token (the
token type cannot place orders); an aggregator's read-only connection (the
aggregator offers no independent proof, and the page says that rests on its
connection type); or a public wallet address with no key, whose ownership is
proven by a message signed with the wallet or a code in the venue profile.

**Unsupported activity.** Activity Koine skipped rather than guess at (a short
sale, an option, a stock dividend) is counted on the page by reason. Only the
reason and the count are public, not the symbol or the size.

**Paper portfolios.** A follower's page lists each position at the freshest
public quote, else the last daily close, else at cost, with the unrealized
profit or loss, the median delay between the creator's fill and Koine seeing
it (copy lag) and the signals that were skipped, by reason. The portfolio value
is the sum of its allocations' equity. Starting a new tail adds paper money,
not profit, so the portfolio chart shows paper profit or loss in dollars (value
minus the paper money put in so far), and its drawdown is measured on a
flow-adjusted index, the same rule as deposits on a strategy page: a day's
return is (V_d - new paper money_d) / V_(d-1) - 1. Example: a $1,000 tail falls
to $900 on the day a $500 tail starts. The summed value rises from $1,000 to
$1,400, but the index goes 1 to (1,400 - 500) / 1,000 = 0.9: a 10% drawdown,
and the chart reads -$100.
