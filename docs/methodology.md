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
