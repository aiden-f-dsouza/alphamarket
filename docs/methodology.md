# Methodology: what a verified track record proves, and what it does not

This page is the public contract behind every number AlphaMarket displays.
Every formula here is pinned by a unit test with hand-computed expected values
(`tests/test_ledger.py`, `tests/test_reconcile.py`).

## Where the numbers come from

- Every number on a strategy page is computed by AlphaMarket from data pulled
  directly from the venue (Kalshi) using a **read-only** API key the trader
  connected. Traders cannot enter, edit, or upload performance figures.
- The key's scope is checked at connect time against the venue's own key
  registry; a key with trading permission is rejected.
- Fills and settlements are stored **append-only**: rows are never updated or
  deleted, at the application layer and (on Postgres) by database triggers.
  Corrections, if a venue ever issues them, arrive as new rows.

## Units

All money is held as integer micro-dollars (1e-6 USD). Kalshi reports
sub-cent fees (e.g. `0.139800`) and fractional contract counts (e.g. `101.89`);
whole cents cannot represent a fill exactly, and rounding would create the very
divergence reconciliation exists to detect.

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

Nightly, and on demand, AlphaMarket re-reads the venue balance and compares it
with `cash(now)` computed purely from stored fills and settlements:

```
divergence = venue_balance - cash(now)
tolerance  = max(0.1% × venue_balance, $0.01)
```

If `|divergence| > tolerance`, the strategy is **quarantined** and its public
page is hidden until a human reviews it. Nothing is un-quarantined
automatically. Every check is stored as an `equity_snapshot` so the full
history of "did the ledger explain the balance?" is itself auditable.

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
