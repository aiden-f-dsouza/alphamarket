# Methodology: what a verified track record proves, and what it does not

This page is the public contract behind every number Koine displays.
Every formula here is pinned by a unit test with hand-computed expected values
(`tests/test_ledger.py`, `tests/test_reconcile.py`).

## Where the numbers come from

- Every number on a strategy page is computed by Koine from data pulled
  directly from the venue using a **read-only** connection the trader made:
  a read-only API key (Kalshi) or a public wallet address (Polymarket).
  Traders cannot enter, edit, or upload performance figures.
- A key's scope is checked at connect time against the venue's own key
  registry; a key with trading permission is rejected. A wallet address
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
`usdcSize` exactly. The rounding lands on the split between notional and fee,
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

Each valuation is stored, and the series of valuations is the follower's
curve. Live mirroring into a real account is not offered: it requires a
licensed party in the loop, and the platform refuses it structurally rather
than behind a setting.

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
