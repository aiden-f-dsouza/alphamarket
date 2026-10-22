# GMX v2 (Arbitrum)

Status: **G0 design note, 2026-10-22.** No connector exists yet. This note
is the spec for G1 (`docs/onchain_perps_plan.md` §3, W-G). Founder decisions
4 (account model) and 5 (numeric policy) are made from it. G1 adds the
remaining venue-doc sections (consent text, ownership proof, rate limits,
mapping summary, watchlist, leaderboard, known gaps) in the shape of
`docs/venues/hyperliquid.md`.

Inputs: `docs/research/2026-10-22-gmx.md`, the raw responses in the research
fixtures dir, and the anonymised rows in `tests/fixtures/gmx/`
(`g0_examples_2026-10-22.json` for the worked examples,
`g0_research_rows_2026-10-22.json` for the figures quoted from wider research
rows). Every number in the worked examples below is checked against the real
valuation engine by `tests/test_gmx_g0_examples.py`, as are the collateral
chain (2.1) and the quantity drift (4.2). Other research figures (leaderboard
medians, market and page counts, fee-impact statistics) are read from the
research fixtures dir, outside the repo, and are not pinned by a test.
Amounts are USDC, shown at 6 dp; the ledger stores them as integer micros
(1.000000 = 1,000,000).

## Terms-of-service position

Position: UNREAD

G1 writes this section from a primary reading of the GMX.io Terms and
Conditions (research summary: `docs/research/2026-10-22-gmx.md` §8; the
terms restrict US persons' use of the Interface and have no clause on API
use or scraping). Until then, preflight (S9) keeps `gmx_arbitrum` out of prod
`WATCH_VENUES`.

## Decisions requested (summary)

**Decided 2026-10-22 by the founder: decision 4 = B (capital floor),
decision 5 = accepted as recommended below** (quantities truncated on the
running position, tolerance 0). G1 builds to these.

**Decision 4, account model. Recommendation: B, payout-as-withdrawal with a
capital floor, on balance**, over the plan's A (retained payout). The margin
is narrow: A is the better pick if records on every venue must be measured
on one basis. Both are specified and worked through below on real fixture
trades, with C, Koine's existing convention for a venue without a balance
(Polymarket's return on peak invested capital), as a third option.

- A and B agree until the trader's profit **since the watch** passes the most
  collateral posted at once since the watch. After that, A measures returns
  against every dollar won since the watch, and B against the capital the
  trader deploys. The dilution builds with time watched. At the median pace
  of GMX's top-100 month board on 2026-10-22 (PnL 0.395x max capital in a
  month), A's daily returns and drawdowns are about 0.72x B's after one month
  of watching; at the week board's median (0.118x), about 0.89x after a
  week (section 2.8).
- Paper copying sizes a follower by creator equity (`paper.size_asset_order`).
  Under A that equity grows with banked profit, so the follower's copied
  leverage shrinks the more successful the creator is. That is also what
  happens on every other venue when a trader leaves profit in the account.
  Under B it stays on the scale the creator actually trades.
- B is close to GMX's own leaderboard ROI (PnL over max capital), which
  `/watch/discover` shows next to it.
- Costs of B:
  - **Withdrawals the trader never made.** B books a WITHDRAWAL after every
    profitable close above the floor. A win followed by an equal loss then
    shows a negative TWR at zero dollar P&L: floor 100, +50 (withdrawn),
    then -50 gives **-25.00 %** under B and 0.00 % under A, though the
    trader withdrew nothing (2.8, pinned by a test).
  - **A different basis from every other venue.** A is how Koine measures a
    Hyperliquid (or any) trader who leaves profit in the account. B is how it
    would measure one who withdraws every profit above a floor. Under B, a
    GMX record's TWR, drawdown, Sharpe, Sortino and risk score sit on a
    different basis from the other records beside it on `/watch` (and in the
    marketplace table if a GMX record is ever public). The methodology must
    say so (section 10).
  - More ledger rows (a WITHDRAWAL per profitable close or claim above the
    floor), and the `daily_equity` extras sit at the floor while the TWR
    index rises (the charts already plot the TWR growth curve).
- Under either model the base only rises: one large posting dilutes every
  later return, and that is gameable (2.9, limit 7). And under either model
  the TWR and the dollar P&L can differ in sign (example 3: -60.12 % on
  +20,219.50).

Two rules apply under either model, and both are part of the recommendation:

- **The synthetic account starts at the watch anchor**, not at the address's
  first trade (section 2.3). This needs one small shared change: the
  connector must know `anchor_at` (section 9, S-G1).
- **Synthetic deposits are dated at the start of the UTC day they are
  needed** (section 2.4). Without this, a loss funded by a same-day deposit
  disappears from the TWR. In worked example 3 the record of a trader who
  was liquidated shows +4,590.76 % with no drawdown, instead of -60.12 %
  with a 96.24 % drawdown. T1 recovers the first funded day only when it
  falls after the anchor day, which is the base of every record (2.4).

**Decision 5, numeric policy. Recommendation: accept**, in this form:

- USD legs: half-even at 6 dp, per leg. The error is at most 0.5 micro per
  rounded leg, and an ETH round trip has 2 rounded legs.
- Quantities: truncated at 12 dp, applied to the **running position**, not
  to each fill. The ledger then always equals the venue size truncated, so it
  never drifts and needs no separate full-close correction (the full close
  is the special case where the running size is zero), and the nightly
  position tolerance is **0**.
- The literal per-fill rule with a full-close correction also works. It
  drifts by up to 1 atom per fill while a position is open: 52 atoms (52e-12
  ETH) over 109 real fills in eight hours of one day (pinned on the
  anonymised rows). It then needs a tolerance, proposed at 1,000,000 atoms
  (section 4.2).
- The `positions_verified` tier's public wording is in section 8.

What GMX coverage costs, honestly:

- Returns are on posted collateral, so they are leveraged and volatile (+36 %
  on a 1.6 % ETH move in example 1).
- The cash is reconstructed and never checked against the venue.
- Received funding counts only once claimed.
- v1 skips positions with non-stable collateral. The fixtures' month #1
  address trades only with WETH collateral and would be flagged throughout.
  How common that is is open (section 12).

## 1. What GMX gives and what it does not

- **No account balance in wallet mode.** Each position (market, collateral
  token, side) holds its own collateral. An increase pulls collateral from
  the wallet, and a decrease sends collateral plus or minus PnL, minus fees,
  back to the wallet. The amount actually sent to the wallet is not public.
- **Collateral after every change is public.** GraphQL `positionChanges`
  carries `collateralAmount` (after) and `sizeInTokens` (after) for every
  change of every position, joined to its trade action by `orderKey`. On
  fixture address B's 20 position changes (WETH collateral, two positions)
  the chain is exact to the wei: each change's collateral after equals the
  previous one's plus or minus its `collateralDeltaAmount` (18 links; test
  `test_research_position_change_chain_is_exact`).
- **Every cash component of a trade is public:**
  - USD figures: `basePnlUsd`, `totalImpactUsd` / `priceImpactUsd`,
    `swapImpactUsd`, `swapFeeUsd` (30-dp USD).
  - Token figures: `positionFeeAmount`, `traderDiscountAmount`,
    `borrowingFeeAmount`, `fundingFeeAmount`, `liquidationFeeAmount` (in
    the collateral token).
  - GMX's own net figure, `pnlUsd`, is exactly the sum of these. On every
    fixture decrease, including two 2025 rows with a PnL swap, the identity
    holds to the last of the 30 decimals (test
    `test_realized_pnl_definition_reproduces_gmx_net_pnl`):

    ```
    pnlUsd = basePnlUsd + impact + swapImpactUsd - swapFeeUsd
             - (token fees x collateralTokenPriceMin)
    ```

    `impact` is `totalImpactUsd` when the row has it (GMX v2.2, which defers
    an increase's impact to its close) and `priceImpactUsd` before that.
- **Received funding** becomes a claimable balance per (market, token),
  often in the long token (WETH, BTC, ...). It reaches the wallet only
  through a `ClaimFunding` claim action, whose entries carry the token
  amount and the oracle price at the claim.

## Account model

### 2.1 The synthetic account

Koine keeps a synthetic USDC account per watched address. Its state:

| Symbol | Meaning |
|---|---|
| `C` | synthetic cash: the anchor's `USDC` row plus every USDC leg after it (collateral included) |
| `K_p` | collateral of in-scope position `p` (USDC atoms, from `positionChanges.collateralAmount`) |
| `L = sum K_p` | collateral currently posted |
| `F = C - L` | free cash: in the account but not posted |
| `M` | floor (model B only): the largest `L` since the anchor |

For every trade action, with `dK = K_after - K_before` and `dc` the sum of its
cash legs (section 3.1), the wallet's net flow into the position is

```
w = dK - dc        w > 0: the wallet posted w        w < 0: the wallet was paid -w
```

This identity needs no wallet data. Fees and losses leave the position
(`dK`) and the cash (`dc`) together, and only what crosses the position's
boundary remains. Valuation is unchanged: equity = `C` + the unrealized PnL
of open positions (`valuation.daily_equity_series`), because collateral is
inside `C`, exactly as margin is inside a Hyperliquid account's USDC.

### 2.2 The two candidate models

Both models book the same cash legs. They differ only in the synthetic flows
(EXTERNAL_FLOW_KINDS legs):

| Event | A: retained payout | B: floor |
|---|---|---|
| increase, wallet posts `w > 0` | `DEPOSIT max(0, w - F)` (draw from free cash first) | same |
| decrease, wallet paid `-w > 0` | nothing: the payout stays as free cash | the invariant below |
| funding claim `x` | FUNDING `+x`, stays | FUNDING `+x`, then the invariant below |
| floor | none | `M = max(M, L)` after every action |

**B's invariant:** after every action (its legs, any deposit, then the floor
update), if `C > M`, book `WITHDRAWAL C - M`. So `C ≤ M` holds after every
action. Only a payout can break it in practice: an increase that needs a
deposit leaves `F = 0`, so `C = L ≤ M`, and one that draws on free cash
lowers `C` by its fees. The rule is stated on `C` and `M`, not on the sign
of `w`, so it covers trades and claims alike.

Under B, profit first refills the account up to the floor, and only the part
above it leaves. A loss below the floor stays in the record until profit
or a new deposit restores it.

### 2.3 Where the account starts: the watch anchor

Koine's verified record starts at the anchor on every venue: legs at or
before `anchor_at` are display only (`valuation_db.account_inputs`). The
synthetic account follows that rule:

- **At the anchor**, `fetch_balances()` returns:
  - each in-scope open position as a `<...>-PERP` entry: size truncated to
    12 dp, `entry_price` = `entryPrice` exactly
    (`fixed_to_decimal_str(entryPrice, 30)`);
  - a `USDC` row equal to `L_anchor`, the collateral of those positions
    (exact USDC atoms from `/v1/positions` `collateralAmount`).

  So `C = L_anchor`, `F = 0`, and under B `M = L_anchor`.
- **Before the anchor**, the connector emits every leg that does not depend
  on `C` or `M` (FILL_BASE, REALIZED_PNL, FEE, INTEREST, FUNDING,
  LIQUIDATION, claims) for the history display, but no DEPOSIT or
  WITHDRAWAL. It still replays `K_p`, the exact sizes `S_p` and the set of
  in-scope markets from genesis, so its state is complete when it crosses
  the anchor.
  - The cash legs depend only on their own row. FILL_BASE does not: under
    the running-position rule (4.2) its quantity is `trunc12(S_after) -
    trunc12(S_before)`, so it depends on `S_before`, which is deterministic
    from genesis but path-dependent. The pre-anchor replay must carry `S_p`.
- **At the first action after the anchor**, the connector sets `C := L`
  (and `M := L`) from its replayed `K_p`. From there the rules of 2.2 apply.
  The replayed `L` equals the anchor's `USDC` row, because both are the
  on-chain collateral of the same positions at the same instant, provided
  both indexers have caught up to that instant (section 8, "Anchor timing").
  The G1 replay test pins this.

Why not start the synthetic cash at genesis, as the plan first read? The
anchor vector is written synchronously when the address is watched
(`watchlist._track` → `take_anchor` → `fetch_balances`). A genesis `C` would
need the address's whole history replayed inside that web request: 57
GraphQL pages for the top all-time address (56,819 trade actions). A
different `C` in the anchor and in the connector would make every later
deposit wrong. Starting at the anchor needs one shared change: the connector
must know `anchor_at` (S-G1).

### 2.4 Deposit dating (T1)

`valuation.daily_returns` puts flows at the end of the day:
`r_d = (E_d - F_d) / E_(d-1) - 1`. A synthetic deposit is made at the
moment an increase needs it, so it always comes before the PnL it funds.
With end-of-day attribution:

- On the first funded day `E_(d-1) = 0`, so `r_d` is undefined and that
  day's PnL never reaches the TWR (example 1: the TWR is -18.61 % instead of
  +10.68 %).
- A same-day loss on a freshly funded position either vanishes (`E_(d-1)` =
  0, example 3) or produces `r_d < -100 %` when `E_(d-1)` is small: with
  `E_(d-1)` = 1,000, a 26,524 deposit and a 25,527 loss,
  (1,997 - 26,524) / 1,000 - 1 = -2,553 %.

**Rule:** a synthetic DEPOSIT is dated
`max(start of its UTC day - 1 microsecond, anchor_at + 1 microsecond)`.
Valuation then counts it as capital at the start of the day it was needed,
`r_d = E_d / (E_(d-1) + D_d) - 1`, with no change to shared code.
Withdrawals keep their real time: they follow the PnL they pay out, so
end-of-day is already correct for them.

Consequences, stated in methodology:

- The equity point of the day before a funded day includes that deposit.
  It is revised when the deposit is ingested.
- **The stored DEPOSIT leg is dated up to 24 hours before the real
  transaction.** The ledger and the history display show that date, so the
  description of the leg carries the real time ("needed at 16:40:10 UTC,
  counted from the start of the day").
- **Gains made earlier on the same day before a posting are understated.**
  A day with gain `G` before a posting `D` (made later that day) returns
  `G / (E_(d-1) + D)` instead of `G / E_(d-1)`. This is conservative, but it
  is a real distortion for a trader who adds collateral intraday after a win.
- **The anchor day is the base, as on every venue.** `compute_asset_stats`
  starts the series at the anchor day, and the first point's return is never
  counted. So when the first posting falls on the anchor day itself, its
  deposit (dated `anchor_at + 1 µs`) and that day's PnL are both in the base
  `E_0`, and the day's PnL never reaches the TWR. T1 recovers the first
  funded day only when the anchor precedes it: example 1's +10.68 % (rather
  than -18.61 %) holds because the anchor is 07-06 and the first posting
  07-07.
- `equity_before` a fill (signal sizing) includes deposits needed later the
  same day. For the opening fill of a flat account this is the only
  non-zero base paper sizing can have, provided the deposit is already
  ingested when signals run (section 11, item 8: stream order).

A shared alternative, a per-profile flow timing in `valuation.daily_returns`
(deposits at the start, withdrawals at the end), gives the same numbers
with true timestamps. It touches every venue's code path, so it is not
proposed for v1.

### 2.5 Worked example 1: one ETH short, real rows (A = B)

The account is fixture address A, the top all-time PnL address. Its
ETH/USD [ETH-USDC] short of 2026-07-07 to 2026-07-10 is in the fixtures
from open to full close: 500.039352125288774211 ETH out, and
166.554837072900248044 + 333.484515052388526167 back. It is taken as the
account's only activity, watched flat at 2026-07-06 12:00 UTC. Daily closes
are GMX's ETH `ohlcv` closes.

Actions (legs per section 3.1). The cash legs come from the fixture rows.
The `dK` column does not: the rows are REST `/v1/trades` rows, which carry no
collateral after the change, so `dK` is each row's
`initialCollateralDeltaAmount` (added on increases, removed on decreases),
typed into the test by hand. That this is the change of
`positionChanges.collateralAmount` is an **inference**, supported by two
checks:

- on address B's 20 position changes joined to its trade actions by
  `orderKey`, every `collateralDeltaAmount` equals the trade's
  `initialCollateralDeltaAmount` (6 increases, 14 decreases), and the
  increases report their fees beside it (test
  `test_research_collateral_delta_equals_initial_collateral_delta`);
- here, t5 (the full close) releases 25,883.581774 = 42,010.954459 -
  16,127.372685 to the atom, which is what the inference predicts for the
  collateral left after t4.

G1 pins it on recorded `positionChanges` rows of a USDC-collateral address
(section 11, item 11).

| # | Time (UTC) | Action | Cash legs | `dc` | `dK` | `w = dK - dc` |
|---|---|---|---|---|---|---|
| t1 | 07-07 16:40:10 | open short 900,675.166654 USD = 500.039352125288 ETH @ 1,801.208570536390 | FEE -324.307197 (360.341330 - 36.034133 discount) | -324.307197 | +22,196.954461 | +22,521.261658 |
| t2 | 07-07 16:40:17 | collateral top-up | none | 0 | +9,906.999999 | +9,906.999999 |
| t3 | 07-07 16:40:39 | collateral top-up | none | 0 | +9,906.999999 | +9,906.999999 |
| t4 | 07-10 15:38:33 | close 166.554837072900 ETH @ 1,788.542493710192 | REALIZED_PNL +2,328.645728, FEE -108.031115, INTEREST -345.523541, FUNDING -694.422380 | +1,180.668692 | -16,127.372685 | -17,308.041377 |
| t5 | 07-10 21:28:54 | close 333.484515052388 ETH @ 1,790.585233861742 | REALIZED_PNL +3,981.749038, FEE -216.307953, INTEREST -50.217964, FUNDING -51.365482 | +3,663.857639 | -25,883.581774 | -29,547.439413 |

The rounded legs:

- t4 REALIZED_PNL: 1,937.068096358089... (`basePnlUsd`) + 391.577631489888...
  (`totalImpactUsd`) = 2,328.645727847977..., half-even to 2,328.645728.
- t5 REALIZED_PNL: 3,281.082473144732... + 700.666565017784... =
  3,981.749038162516..., half-even to 3,981.749038.

Synthetic flows and state after each action:

| # | A: flow | A: `C` / `L` / `F` | B: flow | B: `C` / `L` / `F` / `M` |
|---|---|---|---|---|
| t1 | DEPOSIT 22,521.261658 | 22,196.954461 / 22,196.954461 / 0 | same | same, `M` 22,196.954461 |
| t2 | DEPOSIT 9,906.999999 | 32,103.954460 / 32,103.954460 / 0 | same | same, `M` 32,103.954460 |
| t3 | DEPOSIT 9,906.999999 | 42,010.954459 / 42,010.954459 / 0 | same | same, `M` 42,010.954459 |
| t4 | none | 43,191.623151 / 25,883.581774 / 17,308.041377 | WITHDRAWAL 1,180.668692 | 42,010.954459 / 25,883.581774 / 16,127.372685 |
| t5 | none | 46,855.480790 / 0 / 46,855.480790 | WITHDRAWAL 3,663.857639 | 42,010.954459 / 0 / 42,010.954459 |

All three deposits (42,335.261656) are dated 2026-07-06 23:59:59.999999 (T1).

Daily series. 07-06 is the anchor day, which is where the real pipeline
(`analytics_v2.compute_asset_stats`) starts the series: its point is the
base of the TWR and its own flow, F(07-06), is not netted again when P&L is
taken (`net_flows` sums the points after the first). The unrealized PnL is 500.039352125288 x (1,801.208570536390 -
close), truncated toward zero to the micro:

| Day | ETH close | Unrealized | E (A) | F (A) | E (B) | F (B) | `r_d` (A and B) | TWR index |
|---|---|---|---|---|---|---|---|---|
| 07-06 | 1,798.24 | 0 | 42,335.261656 | +42,335.261656 (start capital) | 42,335.261656 | +42,335.261656 | none (first point) | 1.000000 |
| 07-07 | 1,770.09 | +15,560.509850 | 57,571.464309 | 0 | 57,571.464309 | 0 | +35.99 % | 1.359894 |
| 07-08 | 1,742.01 | +29,601.614857 | 71,612.569316 | 0 | 71,612.569316 | 0 | +24.39 % | 1.691558 |
| 07-09 | 1,743.80 | +28,706.544417 | 70,717.498876 | 0 | 70,717.498876 | 0 | -1.25 % | 1.670416 |
| 07-10 | 1,795.81 | 0 (flat) | 46,855.480790 | 0 | 42,010.954459 | -4,844.526331 | -33.74 % | 1.106772 |
| 07-11 | 1,786.75 | 0 | 46,855.480790 | 0 | 42,010.954459 | 0 | 0 | 1.106772 |

How the 07-07 and 07-10 values are reached:

- 07-07 unrealized: 500.039352125288 x 31.118570536390 = 15,560.509850081...
  E = 42,010.954459 + 15,560.509850 = 57,571.464309, and
  r = 57,571.464309 / 42,335.261656 - 1 = +35.99 %.
- 07-10, B: (42,010.954459 + 4,844.526331) / 70,717.498876 - 1 = 46,855.480790
  / 70,717.498876 - 1 = -33.74 %, which is A's number. B's withdrawals are
  end-of-day flows.

Results:

- **TWR +10.68 %** under both, = 46,855.480790 / 42,335.261656 - 1 (the
  chain telescopes).
- **Max drawdown 34.57 %** under both, = 1 - 46,855.480790 / 71,612.569316
  (from the 07-08 peak).
- **P&L net of deposits +4,520.219134** under both: A, 46,855.480790 -
  42,335.261656; B, 42,010.954459 - 42,335.261656 + 4,844.526331 (its
  withdrawals are flows after the base). It is the trade's realized net
  (1,180.668692 + 3,663.857639 - 324.307197) on 42,335.261656 of synthetic
  deposits. It had been +29.6k unrealized on 07-08.
- Without T1 the deposits land on 07-07, `E(07-06)` = 0, and the first day
  is undefined. The TWR becomes 46,855.480790 / 57,571.464309 - 1 =
  **-18.61 %**, for a trade that made money.

### 2.6 Worked example 2: a liquidation and a big win (A and B diverge)

The account continues after example 1 with two later closing rows of the
same address:

- the 2026-09-02 ARB/USD [ARB-USDC] short **liquidation**;
- the 2026-10-13 XPL/USD [WBTC.b-USDC] short **full close**, whose
  `basePnlUsd` is the plan's 51,033.09 rounding example.

Their opens are not in the fixtures: the real positions were opened before
the fixture window. Each open is **constructed** on the morning of its close
with that close row's size, entry (`sizeDeltaUsd / sizeDeltaInTokens`) and
collateral (`initialCollateralDeltaAmount`), plus a 6 bps position fee. The
account is then flat at every midnight, and no ARB or XPL closes are needed.
Every closing number is verbatim.

| # | Time (UTC) | Action | Cash legs (`dc`) | `dK` | `w` |
|---|---|---|---|---|---|
| a1 | 09-02 00:00:01 | open short 71,422.708123 USD = 803,603.139946326251 ARB @ 0.088878084931 (constructed) | FEE -42.853625 | +26,480.734267 | +26,523.587892 |
| a2 | 09-02 16:31:54 | liquidation @ 0.120726122401 | REALIZED_PNL -25,484.240845 | -26,480.734267 | -996.493422 |
| x1 | 10-13 00:00:30 | open short 106,125.229835 USD = 690,631.526897078958 XPL @ 0.153664038929 (constructed) | FEE -63.675138 | +4,693.985368 | +4,757.660506 |
| x2 | 10-13 00:06:52 | close @ 0.079827656320 | REALIZED_PNL +51,115.416099, FEE -63.684669, INTEREST -3,646.511290, FUNDING -1,594.952122 (`dc` +45,810.268018) | -4,693.985368 | -50,504.253386 |

Notes on these legs:

- a2 REALIZED_PNL: -25,338.356027019290... (`basePnlUsd`) - 145.884817537476...
  (`totalImpactUsd` = -254.826885 immediate + 108.942068 deferred) =
  -25,484.240844556766..., half-even to -25,484.240845. GMX reports zero
  fees on this liquidation row. The 996.493422 of collateral left over went
  back to the wallet.
- x2 REALIZED_PNL: 51,033.091798973216... + 82.324299717257... =
  51,115.416098690473..., half-even to 51,115.416099.

Neither model needs a deposit here: free cash covers both opens.

| | A: retained | B: floor (`M` = 42,010.954459) |
|---|---|---|
| before a1 | `C` 46,855.480790 | `C` 42,010.954459 |
| after a2 | 46,855.480790 - 42.853625 - 25,484.240845 = **21,328.386320** | 42,010.954459 - 42.853625 - 25,484.240845 = **16,483.859989** (below the floor: no withdrawal) |
| after x2 | 21,328.386320 - 63.675138 + 45,810.268018 = **67,074.979200** | 16,483.859989 - 63.675138 + 45,810.268018 = 62,230.452869, then WITHDRAWAL 20,219.498410, leaving **42,010.954459** |

Daily series. Days 07-12 to 08-31 and 09-03 to 10-11 are flat, with r = 0;
they change neither the TWR nor the drawdown and are omitted.

| Day | E (A) | `r_d` (A) | index (A) | E (B) | F (B) | `r_d` (B) | index (B) |
|---|---|---|---|---|---|---|---|
| 07-10 | 46,855.480790 | | 1.106772 | 42,010.954459 | | | 1.106772 |
| 09-01 | 46,855.480790 | 0 | 1.106772 | 42,010.954459 | 0 | 0 | 1.106772 |
| 09-02 | 21,328.386320 | -54.48 % | 0.503797 | 16,483.859989 | 0 | -60.76 % | 0.434265 |
| 10-12 | 21,328.386320 | 0 | 0.503797 | 16,483.859989 | 0 | 0 | 0.434265 |
| 10-13 | 67,074.979200 | +214.49 % | 1.584376 | 42,010.954459 | -20,219.498410 | +277.52 % | 1.639451 |

The hand formulas:

- A on 09-02: 21,328.386320 / 46,855.480790 - 1. B on 09-02: 16,483.859989 /
  42,010.954459 - 1.
- B on 10-13: (42,010.954459 + 20,219.498410) / 16,483.859989 - 1 =
  62,230.452869 / 16,483.859989 - 1.
- **TWR over both examples:**
  - A: 67,074.979200 / 42,335.261656 - 1 = **+58.44 %**.
  - B: (46,855.480790 / 42,335.261656) x (62,230.452869 / 42,010.954459) - 1 =
    **+63.95 %**.
- **Max drawdown** (07-08 peak to 09-02 trough):
  - A: 1 - 21,328.386320 / 71,612.569316 = **70.22 %**.
  - B: 1 - (46,855.480790 x 16,483.859989) / (42,010.954459 x 71,612.569316) =
    **74.33 %**.
- **P&L net of deposits +24,739.717544 under both** (A: 67,074.979200 -
  42,335.261656; B: 42,010.954459 - 42,335.261656 + 4,844.526331 +
  20,219.498410). The models change the returns, never the dollar P&L.
- End equity: A 67,074.979200, B 42,010.954459. A's next trade is measured
  against 1.60x B's base.

For contrast, plain payout-as-withdrawal with no floor gives a TWR of
-55.86 % and a drawdown of 97.54 % over the same trades. It withdraws every
payout and deposits every posting, both T1-dated, and judges each trade
against its own collateral only.

### 2.7 Worked example 3: why deposits are dated at the start of the day

Take example 2's trades alone, watched flat at 2026-09-01 12:00. The ARB
open now needs a deposit of 26,523.587892. Later the XPL open needs
4,757.660506 - 996.493422 = 3,761.167084.

| | With T1 (recommended) | Without T1 (deposit at its real time) |
|---|---|---|
| E 09-01 | 26,523.587892 (deposit counted at the start of 09-02) | 0 |
| `r` 09-02 (liquidation) | 996.493422 / 26,523.587892 - 1 = **-96.24 %** | undefined (`E_(d-1)` = 0): the loss never enters the TWR |
| `r` 10-13 | 50,504.253386 / 4,757.660506 - 1 = +961.54 % | (50,504.253386 - 3,761.167084) / 996.493422 - 1 = +4,590.76 % |
| TWR | **-60.12 %** | **+4,590.76 %** |
| Max drawdown | **96.24 %** | **0** |

The numbers are the same under A and B: B's 24,023.519119 withdrawal on
10-13 is an end-of-day flow.

Note the sign: with T1 the TWR is -60.12 % while the P&L net of deposits is
**+20,219.498410** (50,504.253386 - 26,523.587892 - 3,761.167084), under A
and B alike. A TWR chains daily returns and ignores how much capital each
day had: a 96 % loss on 26.5k followed by a 962 % gain on 4.8k compounds to
a loss. That is the TWR's definition on every venue, not an artefact of
either model, but a reader of a GMX record will meet it often, because
collateral swings with every posting. Option C (2.8) gives +66.77 % here.

### 2.8 Comparison and recommendation

| | A: retained payout | B: floor | C: peak invested (Polymarket's convention) |
|---|---|---|---|
| Example 1 (one trade from a flat start) | TWR +10.68 %, drawdown 34.57 % | identical | identical |
| Example 2 (liquidation, then a win) | TWR +58.44 %, drawdown 70.22 % | TWR +63.95 %, drawdown 74.33 % | as A |
| Example 3 (funded liquidation, then a win; P&L +20,219.50) | TWR -60.12 %, drawdown 96.24 % | identical | TWR +66.77 %, drawdown 84.29 % |
| Win 50, then lose 50 on a floor of 100 (P&L 0) | TWR 0.00 % | **TWR -25.00 %** | TWR 0.00 % |
| Liquidation day (example 2) | -54.48 % (diluted by the 4,844.53 banked in July) | -60.76 % | as A |
| Base for the next trade | 67,074.98 and growing with every win | 42,010.95 (the most posted at once since the watch) | as A, plus capital first needed later |
| Dilution over time | grows with profit since the watch | none from profit | as A, and early days diluted by capital needed only later |
| Basis against other venues | the same: how Koine measures any trader who leaves profit in the account | different: as if every profit above the floor were withdrawn; the methodology must say so | Polymarket's fills-only basis (a P&L ratio, not a TWR over flows) |
| Paper copy sizing (`F / C` in `paper.size_asset_order`) | the follower's mirrored leverage falls as the creator wins, as on other venues | stable on the creator's deployed scale | as A |
| Synthetic rows | DEPOSIT when a posting exceeds free cash | DEPOSIT, plus one WITHDRAWAL per profitable close or claim above the floor | none (a computed shift) |
| Past figures revised | the day before a funded day (T1) | the same | every past return and drawdown, whenever the peak rises |
| `daily_equity` extras | rise with retained profit | sit at the floor after wins (charts plot the TWR growth curve on every v2 page, so nothing visible changes) | shifted by the peak |
| Cursor state | `C`, `K_p` | `C`, `K_p`, `M` | `C`, `K_p` |
| Shared code | S-G1 | S-G1 | S-G1, plus a peak-invested path in `analytics_v2` |
| Relation to GMX's own ROI (`pnlBps` = PnL / maxCapital) | none | same idea (max capital as the base); not the same number (TWR vs PnL ratio) | close (P&L over peak capital) |

All figures in this table are pinned by `tests/test_gmx_g0_examples.py`.

**B's withdrawals and a zero-P&L record.** The trader posts 100, closes for
+50, posts the same 100 again and closes for -50. Dollar P&L is 0, and the
trader withdrew nothing: the 50 won first is still in the wallet. Under
B the 50 above the floor is booked as a WITHDRAWAL, so the second trade
loses 50 of 100: +50 % then -50 %, TWR **-25.00 %**. Under A it loses 50 of
150: +50 % then -33.33 %, TWR 0.00 %. B's TWR is the compounded return on
the capital deployed per trade, which is a defensible reading, but it
attaches to withdrawals the trader never made, and a reader comparing it with
the record's P&L will find it hard to follow. (TWR and P&L can also differ
in sign under A, as example 3 shows; B adds cases A does not have.)

**How large A's dilution gets.** Under A, a trader's base after a while is
roughly the floor plus everything won **since the watch** (the account
starts at the anchor, 2.3), so A's daily returns are roughly B's divided by
(1 + PnL since the watch / max capital since the watch). GMX publishes PnL
and max capital per address and period (`accountPnlRankMetrics`, each
period's own figures; the month and all-time `maxCapital` of one address
differ). On 2026-10-22, top 100 by PnL, if a watched address keeps its
board's median pace:

| Watched for | Median PnL / max capital (board) | A's returns vs B's |
|---|---|---|
| one week | 0.118 (week; 6 of 100 above 1.0) | about 0.89x |
| one month | 0.395 (month; 19 of 100 above 1.0) | about 0.72x |
| three months, if the month pace held | 3 x 0.395 | about 0.46x |

The all-time board's median, 1.055 (52 of 100 above 1.0), would give about
0.49x, but only for an address watched since its first trade, which never
happens. The #1 all-time address (fixture address A) has made 8,278,357 USD
on max capital 7,432,165 (`pnlBps` 11,138), so its past is no guide to the
first months of a watch. The dilution builds fastest on the addresses at the
top of the boards, which are the ones people will watch.

**Recommendation: B, on balance.** The Koine numbers that matter for a
watched GMX address are its recent returns, its drawdowns, and a paper-copy
size that mirrors how it trades. Under A all three drift toward zero in
proportion to how well the trader has done since the watch (about 0.72x
after a month at the month board's median pace, and falling). B keeps them
on the trader's deployed capital. Its price is withdrawals the trader never
made, with the zero-P&L case above, and a basis that differs from every
other venue, which the methodology states (section 10).

**When to pick A instead.** If records across venues must mean the same
thing number for number (a GMX address and a Hyperliquid address side by
side on `/watch`), A is the right choice. It is exactly how Koine measures
any trader who leaves profit in the account, it books no withdrawal nobody
made, and its dilution is the one every other venue already has. If the
founder picks A, everything else in this note stands unchanged: drop `M` and
the invariant of 2.2.

**Option C, return on peak invested capital.** The methodology's
"Verification tiers" gives Polymarket, which has no observable balance
either, a return on peak invested capital: the ledger is anchored at zero
cash and the curve is shifted by the deepest the cash went below the anchor.
The GMX analogue is the peak of the cumulative net posting `sum of w` since
the anchor. That peak is exactly A's total deposits, because A deposits only
when it is exceeded, so C is A with every deposit moved to the anchor (test
`test_option_c_peak_invested_capital`). It needs no synthetic DEPOSIT or
WITHDRAWAL rows and no T1 backdating, and its total return is P&L over peak
capital, so it never shows a loss for a profitable record (example 3:
+66.77 % against -60.12 %). It is not proposed:

- The shift is recomputed whenever the peak rises, so every past daily
  return and drawdown of the record changes after the fact. A and B revise
  at most the point of the day before a funded day.
- It has A's dilution by banked profit, and adds one: early returns are
  measured against capital the trader first posted later (the methodology
  already says so for Polymarket).
- Asset venues have no peak-invested path. `stats.peak_invested` serves the
  binary replay, and `analytics_v2` computes a TWR over ledger flows, so C
  is a shared change in `analytics_v2` for POSITIONS-mode venues, where A
  and B need S-G1 alone.

Rejected, for the record. Plain payout-as-withdrawal (no floor), which the
plan dismissed because flat midnights leave no defined return, is computable
once deposits follow T1. It is still rejected:

- Equity is zero whenever the trader is flat, so paper sizing has no base
  between trades.
- Every flat day drops out of the return series, so Sharpe and Sortino are
  computed only over days with risk on.
- Each trade is judged against its own collateral alone. Example 2's TWR
  becomes -55.86 % with a 97.54 % drawdown, for an account that ended up
  over 20,000 USD ahead.

### 2.9 Limits the methodology must state

1. **Returns are on collateral, not on the trader's wallet.** GMX positions
   commonly run 10-50x on their collateral, so daily returns are leveraged.
   In example 1, ETH fell 1.6 % and the day's return was +35.99 %. These
   numbers are not comparable with a venue whose account holds idle margin.
2. **A synthetic deposit is Koine's inference.** The trader's wallet may have
   held the money all along. Koine counts capital only from the moment a
   position needs it (T1: from the start of that UTC day).
3. **Accruing costs and pending impact are booked when GMX settles them**,
   at the next change of the position. Until then equity is overstated by
   the pending borrowing and funding fees. Example 1 booked 1,141.529367 of
   them on 07-10, accrued over three days.
4. **Received funding counts only when claimed** (section 5).
5. **USDC is valued at 1.00.** GMX's oracle priced USDC at 0.99970-0.99986
   in these rows. Fees are booked in USDC tokens and PnL in USD, so the
   depeg is not counted: 0.33 on t4's 1,147.98 of fees.
6. **v1 covers USDC and USDC.e collateral only** (section 3.3).
7. **The base only rises, and that is gameable.** Under B a one-off large
   posting raises the floor for good. Under A (and C) a posting closed flat
   stays in the account as retained free cash, or in the peak. Either way it
   dilutes every later return, every later drawdown and the risk score. It
   is cheap to do: a collateral top-up of an open position, withdrawn a
   minute later, costs only execution fees and lowers every later drawdown
   in proportion. Koine does not correct for it in v1; the methodology says
   so.
8. **TWR and dollar P&L can differ in sign** (example 3: -60.12 % on
   +20,219.50). Collateral swings with every posting, so this happens more
   often than on a venue with a standing balance. The page shows both.
9. **Under B only: a different basis from other venues** (2.8), and
   withdrawals the trader never made.

## 3. Leg mapping

### 3.1 Cash legs per trade action (USDC collateral)

Only `eventName = "OrderExecuted"` rows with `orderType` 2-8 are used.
`OrderCreated`, `OrderCancelled`, `OrderFrozen` and swaps (orderType 0/1,
wallet activity) produce nothing.

| Leg | Value | Rounding |
|---|---|---|
| REALIZED_PNL (decreases and liquidations, orderType 4-7, only) | `basePnlUsd + impact + swapImpactUsd`, `impact` = `totalImpactUsd` if present, else `priceImpactUsd` | one half-even rounding of the 30-dp sum |
| FEE | `-(positionFeeAmount - traderDiscountAmount + uiFeeAmount)` `- swapFeeUsd` | token part exact; `swapFeeUsd` rounded (only when non-zero) |
| INTEREST | `-borrowingFeeAmount` | exact (USDC atoms) |
| FUNDING | `-fundingFeeAmount` (funding paid) | exact |
| LIQUIDATION | `-liquidationFeeAmount` (orderType 7) | exact |
| DEPOSIT / WITHDRAWAL | section 2.2, from `w` | exact (USDC atoms) |

The legs sum to GMX's `pnlUsd` plus `token fees x (collateral price - 1)`
(the depeg of limit 5), up to the two roundings. Size-0 actions are handled
by the same rules: collateral top-ups and withdrawals, keeper funding
settlements (`isFundingFeeSettle`) and fee-only decreases. Their `w` comes
out right: a fee-settling decrease has `dK = dc = -fees`, so `w = 0`.

**Loss beyond collateral.** If a decrease or liquidation gives `w > 0`
(Koine's legs take more than the collateral released, which on-chain only
happens when the pool absorbs bad debt or by a rounding micro), the
REALIZED_PNL leg is raised by `w`. The loss is then capped at the
collateral, `w = 0`, and the description says "loss beyond collateral
absorbed by the pool: X". A decrease never creates a DEPOSIT.

### 3.2 Position legs (FILL_BASE) and fill prices

- Quantity: section 4.2.
- **Increase price** = `sizeDeltaUsd / sizeDeltaInTokens`, half-even at the
  venue's price scale (30 - token decimals dp; 12 dp for an 18-dp token).
  This is GMX's own entry: `sizeInUsd / sizeInTokens`. It makes the
  average-cost entry replayed by `valuation` match GMX's `entryPrice`.
  `executionPrice` is **not** used for increases: GMX v2.2 defers the
  increase impact to the close. On t1 the oracle price is 1,801.208570536390
  (what the position is carried at) and the `executionPrice` is
  1,802.533269877269; valuing 500 ETH at the wrong one would misstate
  equity by about 662 USD for as long as the position is open.
- **Decrease price** = `executionPrice` x 10^-(30 - token decimals), exact.
  It never changes the replayed entry (a reduction keeps the entry, and GMX
  positions cannot flip), so it only serves signals and display.
- `liquidity = "taker"` for every fill (GMX executes against the pool).

### 3.3 Scope (v1)

- **In scope:** positions whose collateral token is USDC
  (`0xaf88d065e77c8cc2239327c5edb3a432268e5831`) or USDC.e. The collateral
  token is read from `positionChanges.collateralToken`, not from
  `initialCollateralTokenAddress`, because an increase with a `swapPath`
  posts a different token from the one sent.
- **Flagged:** every action on a position with any other collateral (WETH,
  WBTC, USDG, USDe, ...) becomes `UnsupportedActivity` (reason
  `unsupported_type`, activity type `non_stable_collateral`). It books no
  legs and its fills are not emitted.
  - USDG ([USDG-USDG] markets) and USDe are stablecoins but not in v1: one
    constant in the connector adds them later, and they would be valued at
    par like USDC.
  - Non-stable collateral is a v2 extension: the synthetic account would
    hold the collateral token as a priced asset.
- **Not flagged, not booked: GMX Account (multichain) flows**
  (`multichainSamechainFundingEvents`, `multichainFunding*`). They move money
  between the wallet and the GMX Account. Both sit outside the synthetic
  account's boundary, the same as ERC-20 transfers, and positions funded
  from a GMX Account produce the same trade actions. The plan said to flag
  them if not booked. Flagging would mark every GMX Account user's record
  "numbers may not reconcile" when nothing is wrong with them (deviation,
  listed in section 9).
- **Ignored:** swaps, LP (GM/GLV) activity, the keeper execution fee in ETH
  (gas, not in USD PnL), `SettleFundingFeeCreated/Executed` claim actions
  (section 5).

## 4. Numeric policy (decision 5)

### 4.1 USD → USDC legs: half-even at 6 dp, per leg

Every 30-dp USD figure becomes USDC atoms through
`money.fixed_to_atoms(raw, 30, 6, rounding=ROUND_HALF_EVEN)`. Only
`connectors/gmx.py` may pass `rounding=`; `tests/test_fixed_point.py`
enforces that.

- `basePnlUsd` 51033091798973216164946606156791229 = 51,033.091798973216...
  → **51,033.091799** (51,033,091,799 micros).
- The same row's REALIZED_PNL leg, 51,115.416098690473..., becomes
  51,115.416099, an error of +0.309527 micro.
- A 30-dp value of exactly 0.0000025 becomes 0.000002, and 0.0000035 becomes
  0.000004 (ties go to the even micro).

Rounded values per action, and so the error bound:

- 1 for a decrease (REALIZED_PNL), plus 1 when `swapFeeUsd` is non-zero;
- 0 for an increase (all its legs are exact USDC atoms);
- 1 per non-USDC entry of a funding claim.

So an account's reconstructed cash differs from exact arithmetic by **at
most 0.5 micro x (decreases + swap fees + non-USDC claim entries)**.
Example 1 has 2 rounded legs, so at most 1 micro. Example 2's claim (section
5) has 8. The synthetic cash is not reconciled with anything (section 8), so
this bound is the published tolerance.

Display figures (leaderboard pnl, volume) use `fixed_to_decimal_str` and
`leaderboard_figure`, never the ledger.

### 4.2 Quantities: truncation at 12 dp

`sizeDeltaInTokens` is exact at the index token's decimals (18 for ETH,
ARB, XPL and most synthetics; 24 for some; 5-12 for others). The ledger
keeps at most 12 dp:

- `qty_decimals = min(token decimals, 12)`;
- truncation toward zero with `fixed_to_atoms(raw, dec, 12, rounding=ROUND_DOWN)`;
- tokens with 12 dp or fewer are exact.

**The drift, on real fills.** The first four of address A's ETH short
increases on 2026-07-02 (18 dp):

| Fill | `sizeDeltaInTokens` | Truncated (12 dp atoms) | Dropped |
|---|---|---|---|
| 1 | 30.401281604480431397 | 30,401,281,604,480 | 0.431397e-12 |
| 2 | 30.427654947719200293 | 30,427,654,947,719 | 0.200293e-12 |
| 3 | 30.458743914442201290 | 30,458,743,914,442 | 0.201290e-12 |
| 4 | 30.410271210872919522 | 30,410,271,210,872 | 0.919522e-12 |

The dropped parts add up to 1.752502e-12. Then:

- The sum of the truncated fills, 121,697,951,677,513 atoms, is **1 atom
  short** of the truncated position, 121,697,951,677,514.
- Over all 109 non-zero ETH short increases in the same 300-row page
  (2026-07-02 09:39 to 17:43 UTC; 118 rows, 9 of them collateral-only) the
  shortfall is 52 atoms (52e-12 ETH). Test
  `test_per_fill_truncation_drift_over_the_research_page`; the rows are in
  `g0_research_rows_2026-10-22.json`.
- If the short is closed by one decrease of its exact size, per-fill
  truncation books 121,697,951,677,514 back. The ledger is left at **+1
  atom**: a phantom long on an `-S` instrument, whose size must stay ≤ 0.

**The full-close correction (the plan's rule).** When the venue size after a
decrease is exactly zero, the FILL_BASE quantity is the negation of the
running ledger size, not the truncated delta: -(-121,697,951,677,513) =
+121,697,951,677,513, and the ledger ends at 0.

**Recommended form: truncate the running position, not the fill.** The
connector keeps the exact venue size `S_p` per position in its cursor (it
must already, to detect a full close) and books

```
qty_i = trunc12(S_after) - trunc12(S_before)
```

- Fill 4 then books 30,410,271,210,**873**, one atom more than its own
  truncation.
- The ledger equals `trunc12(S)` after every fill, so the full close is the
  case `S_after = 0` and needs no special rule.
- The anchor's position rows (`trunc12(sizeInTokens)`) continue the same
  sum, so the nightly reconcile compares two identical truncations and
  needs **tolerance 0**.
- A fill never moves the ledger by more than one atom away from its own
  truncated delta.
- In example 1 both forms give the same quantities (-500,039,352,125,288,
  +166,554,837,072,900, +333,484,515,052,388): the dropped parts 248044 +
  526167 = 774211 (e-18) never carry.

Profile value: `position_tolerance_atoms = 0` with the recommended form.
With the literal per-fill rule, use `1_000_000` atoms (1e-6 tokens). It
covers up to a million partial fills between flat points and still catches
any missed fill: GMX's minimum position (about 1-2 USD of collateral) is
far above 1e-6 of any token.

The same rounding edge exists under both forms: a fill below 1e-12 tokens
gives `qty = 0`. NormalizedAssetFill needs `qty_atoms > 0`, so the
connector emits no fill for it, books its cash legs as usual, and raises in
a test-covered branch if `S` crosses zero inside such a fill.

### 4.3 Prices and closes

- Fill prices: section 3.2.
- Daily closes: `/v1/prices/ohlcv?timeframe=1d` of the **index token**,
  decimal strings, used for both `-L` and `-S` instruments of every market
  on that index (section 6).
  - Koine's day is the UTC day, and the worked examples assume each `close`
    is the last price before 00:00 UTC of the next day. The recorded candles'
    `timestamp`s are UTC midnights (for example 1792627200000 ms), which
    fits, but whether the close is taken at the next midnight and not at
    some other cut is [VERIFY] (section 12).
- The unrealized PnL uses `valuation`'s average-cost entry, which equals
  GMX's `sizeInUsd / sizeInTokens` to within the 12-dp quantity truncation.

## 5. Funding claims in non-stable tokens

Received funding reaches the wallet only through `claimActions` with
`eventName = "ClaimFunding"`. Each claim has parallel arrays
`marketAddresses`, `tokenAddresses`, `amounts` and `tokenPrices`, and the id
`txHash:account:ClaimFunding`.

Rule:

- One NormalizedEvent per claim: kind FUNDING, `venue_ref` = the claim id,
  one leg FUNDING `USDC`.
- Its value is the sum over entries:
  - a USDC or USDC.e entry: its token amount at **par** (exact; the claim's
    own USDC oracle price, 0.99977 in the fixture, is ignored, consistent
    with limit 5);
  - any other token: `amount x tokenPrice` is an exact 30-dp USD integer
    (amount at the token's decimals x price at 30 - decimals), converted
    with `fixed_to_atoms(..., 30, 6, ROUND_HALF_EVEN)`. That is one rounding
    per entry.
- The description lists every entry (token, amount, price).
- The claimed token counts as converted to USDC at that price. Koine does
  not hold it afterwards: what the wallet does with it is wallet activity.

**Worked example (WETH).** Address A's first claim in the fixtures
(2026-07-04) has 17 entries. Its WETH entry on ETH/USD [ETH-USDC]:

```
5.728900499289178355 WETH x 1,754.326858477229 USD
  = 5728900499289178355 x 1754326858477229 / 10^30
  = 10,050.364015446612953304... → 10,050.364015  (10,050,364,015 micros)
```

The whole claim books FUNDING **+46,144.662412**: 9 USDC entries at par and
8 rounded entries (3 BTC, 3 WETH, 1 SOL, 1 ARB), so at most 4 micros off
exact.

**Scope.** An entry for a market where the cursor has never seen an
in-scope (USDC-collateral) position becomes `UnsupportedActivity` instead
(activity type `funding_claim_out_of_scope`). An account that trades only
WETH collateral thus gets no claim income on zero capital. An entry for a
market that had both an in-scope and a flagged position cannot be split and
is booked whole; the account already carries the flag.

**Booking point.** The claim is the first per-event record with an amount
and a price:

- `SettleFundingFeeExecuted` claim actions move accrued funding into the
  claimable balance, but not all of it: claimables also accrue at every
  position change, with no event in the fixtures.
- `AccountStat.positiveFundingFeesUsd` is a daily USD aggregate without
  token amounts.

So v1 books at `ClaimFunding` only and ignores `SettleFundingFee*`, whose
paid side is already in the trade action's `fundingFeeAmount`. Unclaimed
funding is not in equity until claimed. A trader who never claims has their
received funding left out (limit 4).

Under B, a claim is a payout: it first refills cash up to the floor, and
anything above is withdrawn at the claim time.

## 6. Instrument naming

| Field | Value | Example |
|---|---|---|
| `symbol` | `<market address, lowercase>:<L or S>` | `0x70d95587d40a2caf56bd97485ab3eec10bee6336:S` |
| `base_asset` | `<INDEX>.<first 8 hex of the market>-<L or S>-PERP` | `ETH.70d95587-S-PERP` |
| `quote_asset` | `USD` | |
| `type` | `PERP` | |
| `qty_decimals` | `min(index token decimals, 12)` | 12 |
| `qty_step_atoms`, `min_qty_atoms` | 1, 0 | |
| `copyable` | False for TradFi markets (SPY, QQQ, SPCX, gold, oil, FX), True otherwise | |

**Why the market tag (a deviation from the plan's `ETH-L-PERP`).** Holdings
and the perp book are keyed by asset, so `ETH-S-PERP` would net every ETH
short across GMX's four ETH/USD markets ([ETH-USDC], [ETH-ETH],
[wstETH-USDe], [USDG-USDG]) into one average entry. A partial close on one
market would then keep the merged entry and misvalue what is left on the
other. Example: short 1 at 3,000 on one market and 1 at 2,000 on another,
merged entry 2,500. Closing the first at 2,500 books +500 realized, and the
remaining short is valued at 0 unrealized instead of -500, so equity is
overstated by 500 for as long as it stays open.

This happens in the fixtures: address B held ETH shorts on [ETH-USDC]
(entry 2,834.07) and [ETH-ETH] (entry 1,577.78) at the same time from
2025-04-16 to 2025-08-06.

With one asset per (market, side):

- a long and a short never net, so a hedged long and short on one market
  are two instruments;
- `-L` sizes are ≥ 0 and `-S` sizes ≤ 0.

Rules:

- `INDEX` is the index token's `/tokens` symbol, uppercased. If it is not
  `^[A-Z0-9]+$`, or is missing, it is replaced by the market name's text
  before the `/`, reduced to `[A-Z0-9]`. In the fixtures that applies to two
  deprecated markets (XAUT, APE).
- `-PERP` stays the last suffix, so every `PERP_SUFFIX` site (`valuation`,
  `paper`, `paper_signals`, `prices`, `analytics_v2`, `paper_lifecycle`)
  treats it as a perp.
- The tag sits between the index and the side marker, so `-L`/`-S` cannot
  collide with a real index symbol.
- The connector asserts that no two listed markets share an 8-hex tag. None
  do among the 126 perp markets of 2026-10-22.
- **Prices:**
  - `prices._resolve` prices a perp only by a venue PERP instrument whose
    `base_asset` equals the asset. `GmxMarketData.fetch_instruments` lists
    both sides of every market, and `fetch_daily_closes(symbol)` and
    `fetch_quote(symbol)` map `<market>:<L|S>` to the market's index token.
  - `pricing_symbol` returns None for these assets (perps never fall back to
    spot), so the stripped `ETH.70d95587-S` is never priced on its own.
  - `analytics_v2._price_assets` will ask for its closes and find none,
    which is harmless. G1 pins that no `unpriced` flag results.
- **Cosmetic, shared (S-G3):** `valuation.exposure` groups by
  `asset.removesuffix("-PERP")`. A GMX ETH long, an ETH short and a second
  ETH market therefore count as three "underlyings", and the concentration
  flag under-reads. Watch strategies are never public, so this can wait
  until a GMX record is.

## 7. The stateful cursor and the restart-from-genesis rule

Two page streams, each with its own opaque cursor (JSON with `"v": 1`):

**Position stream** (`fetch_asset_fills_page`):

- Emits one NormalizedAssetFill per in-scope OrderExecuted trade action with
  a non-zero quantity. Its only leg is FILL_BASE.
- `venue_fill_id = txHash:logIndex`, `order_id = orderKey`.
- Cursor: the keyset position, plus `S_p` (the exact venue size as a decimal
  integer string) and the scope of every position key seen.

**Cash stream** (`fetch_events_page`):

- Emits one event per trade action with any cash leg, `venue_ref =
  txHash:logIndex`, kind REALIZED_PNL for decreases and FEE otherwise.
- Emits one event per synthetic flow, `venue_ref = deposit:<txHash:logIndex>`
  or `withdrawal:<txHash:logIndex>` (or `withdrawal:<claim id>`), with the
  dating of 2.4.
- Emits one event per funding claim.
- Cursor: the keyset position of trade actions and claim actions, plus
  `anchored`, `C`, `M`, `K_p` per position key, and the set of markets with
  an in-scope position.

The position key is `<market>:<collateral token>:<L|S>`, lowercase: GMX's own
key without the account.

Putting all cash in one stream keeps every path-dependent number (`C`, `M`
and the flows) in a single cursor. Splitting the cash legs between fills and
events would let the two cursors disagree.

Both streams read the same GraphQL request per page, one POST with aliased
queries (one venue request per page, per the `Page` contract):

- `tradeActions(where: {account_eq: <EIP-55>, eventName_eq: "OrderExecuted",
  timestamp_gte: T}, orderBy: [timestamp_ASC, id_ASC], limit: 500)`;
- `positionChanges` over the same window (`collateralAmount`,
  `collateralToken`, `sizeInTokens`, `orderKey`);
- for the cash stream, `claimActions(where: {account_eq, eventName_eq:
  "ClaimFunding", timestamp_gte: T})`.

The account filter is the **checksummed** address: a lowercase one returns
an empty history.

Rules:

1. **Genesis.** A `None` cursor starts at `timestamp_gte: 0` with empty
   state. Both streams always replay from the address's first action.
2. **Total order.** Items are processed by (timestamp, trade action before
   claim, id). A page processes only items with timestamp < `T_cut`.
   - `T_cut` is the smallest last timestamp among the lists that came back
     full; it is +∞ when none did.
   - The cursor stores the ids already processed at its last timestamp.
   - A single timestamp with more rows than the page size raises (a test
     covers it).
3. **Join.** Each trade action is joined to its position change by
   `orderKey`. A trade action without one in the window raises: the job
   retries and the connector never guesses. `PositionChange.id` (a bare tx
   hash, which collides) is never used.
4. **Collateral.** `K_before` comes from the cursor and `K_after` from
   `collateralAmount`. The unsigned `collateralDeltaAmount` is never used:
   an increase can lower collateral when its fees exceed the collateral it
   adds.
5. **Finality margin.** Only items older than `now - 300 s` are processed
   (squid lag about 1 s; Arbitrum reorgs are rare but would otherwise
   corrupt `C` for good) [VERIFY the margin].
6. **The anchor** (2.3) comes from `connector.anchor_at` (S-G1). Items at or
   before it update `K_p`, `S_p` and the market set only. At the first item
   after it, `C := L` and `M := L`. With `anchor_at` None (the smoke
   script), the account counts as anchored at genesis with `C = 0`.
7. **Determinism.** The legs depend only on the rows and `anchor_at`: no
   clock, no prices beyond those on the rows, no REST data.
   - **Restart test:** run the full sync, then from `None` again, and from
     `None` again with the run cut at every page boundary. Every run gives
     identical events and fills.
   - `ingest` quarantines any group re-ingested with different legs, so a
     divergence cannot pass silently.
8. **Versioning.** A cursor with an unknown `v` raises. Changing a rule after
   accounts exist means watching those addresses afresh: the ledger is
   append-only, and changed legs would quarantine.
9. **No REST fallback for the streams** (a deviation from the plan, which
   kept REST `/v1/trades` as a fallback). REST rows have no collateral
   after, so they cannot give `w`. If the squid is down, the sync fails and
   retries. REST `/v1/trades` (paged by `until`, never by cursor past
   10,000) and `/v1/positions` are used only by the smoke script and the
   reconcile.

## 8. Reconcile and the `positions_verified` tier

The profile has `balance_observable=False`, `reconcile_mode=POSITIONS`,
`position_tolerance_atoms=0` (section 4.2). Nightly:

- **Compared:** for every `-L`/`-S` instrument, the ledger's signed size
  against the venue's. The venue size comes from `fetch_balances()`
  (`/v1/positions`, in-scope collateral only), as `trunc12(sizeInTokens)`,
  negated for shorts. `reconcile.reconcile_positions` already does the rest
  (re-sync once, quarantine on a mismatch).
- **Not compared:** cash, and anything derived from it. GMX publishes no
  balance for a wallet-mode address.
- **Checked elsewhere:**
  - The connector's per-position collateral `K_p` against `/v1/positions`
    `collateralAmount`, exactly, in the G1 replay test and the smoke script.
  - Claimable funding (`claimableLongTokenAmount`,
    `claimableShortTokenAmount`) is not reconciled in v1.

**Anchor timing, a risk to tolerance 0 and to `C`.** The anchor's position
rows and its `USDC` row come from REST `/v1/positions` (the GMX API's
indexer), read at `anchor_at`, which is Koine's clock. The fills and `K_p`
come from the squid, a different indexer, and are compared with
`anchor_at` by block time. The two indexers lag the chain independently:

- A fill executed in the REST indexer's lag window just before `anchor_at`
  is missing from the snapshot. Being before the anchor, its legs are
  display only, so the ledger never counts it.
- A fill the snapshot already includes but whose block time is after
  `anchor_at` (clock skew) is counted twice: in the anchor vector and again
  as a post-anchor fill.

Either way the difference is a whole fill, so the first nightly reconcile
quarantines the account (under any tolerance, not only 0), and the replayed
`L` no longer equals the anchor's `USDC` row, so `C` and `M` start wrong.
`reconcile.take_asset_anchor` already waits out fills within 5 s of
`anchor_at`, using one `fetch_asset_fills_page(None, t0 - 5 s)`, but the
connector's 300 s finality margin (rule 7.5) would hide exactly those fills
from that check. G1 must (section 11, items 10 and 11):

- let that check see fills inside the finality margin (a raw query for the
  window, no legs) [VERIFY how the connector tells the call from a sync
  start], and make `fetch_balances` compare the REST snapshot's indexed
  block or time, if the API exposes one, with the squid's, retrying while
  either is behind `t0` [VERIFY];
- include in the replay test a fill seconds before the anchor and one
  seconds after it.

Proposed wording for the methodology page (the dashboard label "positions
verified nightly · cash reconstructed" and the note in
`public.verification_view` already exist):

> **Positions verified (GMX).** GMX keeps no account balance for a wallet:
> each position holds its own collateral, and money moves between the wallet
> and the positions with every trade. Koine therefore keeps a synthetic
> account for the address, described under "Synthetic margin accounts".
> Every night Koine checks the size of each open position against GMX's own
> record, to the 12th decimal of the token, and freezes the record on any
> difference. The account's cash (the collateral posted, the payouts, and
> the deposits and withdrawals Koine infers from them) is reconstructed from
> the address's public trade history. It is not checked against GMX, because
> GMX publishes no balance to check it against. USD amounts are rounded to
> the micro-dollar, half to even, once per amount, so the reconstructed cash
> can differ from exact arithmetic by at most half a micro-dollar per
> rounded amount.

## 9. Shared changes (main session, never the venue agent)

Already landed with the scaffold: `money.fixed_to_atoms(..., rounding=)`,
`ReconcileMode.POSITIONS`, `position_tolerance_atoms`, the
`positions_verified` tier and its label, and `FundingRate.short_rate`.

New, found by G0:

- **S-G1 (required before G1 merges): the connector learns the account's
  anchor.**
  - Add `anchor_at: datetime | None = None` to `VenueConnector` (base.py,
    documented as "the account's anchor; only synthetic-cash venues read
    it").
  - The place in `ingest.py` that builds an account's connector (around line
    130) sets `connector.anchor_at = account.anchor_at`.
  - Every other connector ignores it. Without it the synthetic account
    cannot start at the anchor (2.3).
- **S-G2 (docs only):** the `NormalizedBalances` docstring gains "for a
  POSITIONS-mode venue the quote-asset row is the synthetic cash at the
  anchor, i.e. the collateral of its open positions".
- **S-G3 (cosmetic, before any public GMX record):** an `underlying` rule in
  `valuation.exposure` and the concentration flag that maps
  `<INDEX>.<tag>-<L|S>-PERP` to `<INDEX>` (section 6).
- **S-G4 (methodology merge):** section 10.
- **S-G5 (proposed, before G1 merges): no signal before its funding is
  ingested.** `ingest._sync_assets` runs the asset-fills stream before the
  events stream, and the SIGNALS job runs after the sync. If a sync stops
  between the two streams (`BudgetExhausted`, `RateLimited`), a GMX fill is
  ingested while the DEPOSIT that funds it is not, and its signal gets
  `creator_equity_unknown` or an equity without the deposit (2.4). Either
  run the events stream first for synthetic-cash venues (a profile flag
  read by `_sync_assets`), or have the signals pass skip fills newer than
  the account's events cursor until it catches up. The main session picks
  one; G1 tests the chosen order.

Deviations from the plan's W-G text, for the main session to accept:

- model B recommended instead of A;
- the account starts at the anchor, not genesis (S-G1);
- T1 deposit dating;
- the market tag in `base_asset`;
- running-total truncation and tolerance 0;
- cash legs in the event stream and FILL_BASE alone on fills;
- no REST fallback for sync;
- GMX Account flows not flagged;
- REALIZED_PNL includes `totalImpactUsd` and `swapImpactUsd`, and the swap
  fee goes to FEE.

## 10. Methodology notes (for docs/methodology.md)

**Synthetic margin accounts (GMX).** GMX holds no balance for a wallet, so
Koine builds one. It starts when the address is watched, holding exactly the
collateral of the positions open at that moment. Each trade's fees, funding,
borrowing and realized PnL are booked as on any perp venue. What GMX moved
between the wallet and a position is worked out from the position's
collateral before and after the trade (`w = collateral change - cash legs`).
When a position needs more than the account's free cash, Koine books the
shortfall as a deposit, dated at the start of that UTC day so that it counts
as capital for the whole day it was at risk. The ledger therefore shows the
deposit up to 24 hours before the trade that needed it; its description
gives the real time. A gain made earlier the same day, before the posting,
is measured against the capital including that posting, so it is
understated. The day the address is watched is the base of the record, as on
every venue: a position opened and closed on that day does not count in the
return.

*[Model B]* Payouts first refill the account up to the most collateral it
has ever had posted at once since it was watched. Anything above that is
treated as withdrawn, so returns are measured against the capital the
trader deploys and not against everything they have won. **These returns
are on a different basis from Koine's other venues.** Elsewhere, profit the
trader leaves in the account stays in the base; here it is treated as if
the trader withdrew it, although they withdrew nothing. Total return,
drawdown, Sharpe, Sortino and the risk score of a GMX record are therefore
not directly comparable with those of a record on another venue. One
consequence: a winning trade followed by an equal losing trade on the same
collateral shows a negative return (+50 % then -50 %, -25 % in all) while
the dollar P&L is zero.

*[Model A instead]* Payouts stay in the account. Returns are measured
against everything the trader has posted and won since the account was
watched, as on any venue where the trader leaves profit in the account, so
recent returns of a very profitable trader are diluted.

Under either model, capital that was posted once stays in the base: a large
posting, even one withdrawn a minute later, lowers every later return and
drawdown. Koine does not correct for this. A return over time (TWR) and the
dollar P&L can also differ in sign, because collateral grows and shrinks
with every posting.

Limits (2.9) follow, in plain words.

**GMX rounding.** GMX reports USD at 30 decimals. Koine rounds each amount
once to the micro-dollar, half to even. Token quantities beyond 12 decimals
are cut off (toward zero) on the running position, so the recorded position
always equals GMX's position cut to 12 decimals.

**Funding received on GMX** is counted when the trader claims it, at the
token prices recorded on the claim. Stablecoins count at 1.00.

**Price impact on GMX** is part of realized PnL when the position is
reduced, including the part GMX deferred from the open.

## 11. G1 build checklist (Opus)

Files (plan §3 common rules):

- `src/alphamarket/connectors/gmx.py`
- the `# ---- venue-gmx_arbitrum ----` blocks in `connectors/registry.py`,
  `demo.py` and `scripts/seed_demo.py`
- `tests/test_gmx_*.py` and `tests/fixtures/gmx/`
- this doc
- `scripts/gmx_arbitrum_smoke.py`

Do not touch shared files. S-G1 must be merged by the main session first,
or G1 stops and reports.

1. **Founder decisions in hand.** Decision 4 (A or B) and decision 5. Under
   A, drop `M` and the WITHDRAWAL rule. Under the literal per-fill
   quantity rule, set the tolerance to 1,000,000 and add the full-close
   branch.
2. **Profile** (registry block). Copy Hyperliquid's comments.
   - Identity and credential: `display_name="GMX (Arbitrum)"`,
     `credential=ADDRESS`, `address_kind=EVM`, `secret_label=None`,
     `readonly_proof=NONE`, `key_label="Wallet address"`, `key_help` in
     Hyperliquid's shape.
   - Accounts and reconcile: `quote_asset="USDC"`,
     `balance_observable=False`, `reconcile_mode=POSITIONS`,
     `position_tolerance_atoms=0`, `transfers_observable=False`,
     `history_depth="all"`.
   - Binary-era fields: `tick_micros=1`, `contract_step_micros=1`,
     `fee_rounding_micros=1`.
   - Ownership: `ownership=SIGNED_MESSAGE`, `instrument=ASSET`.
   - Paper: `paper_fee_model=BPS_OF_NOTIONAL`, `fee_rate_bps=6`,
     `paper_fallback_penalty_bps=50`. The worst price impact across the 187
     sized fixture fills is -20.4 bps and the median is 0, so the default
     50 covers it with margin.
   - Watch and discover: `asset_classes=("crypto",)`,
     `leaderboard_periods=("day","week","month","all")`,
     `stream_mode=None`.
   - Gating: `requires_partner_approval=True`,
     `tos_doc="docs/venues/gmx_arbitrum.md"`, `envs=("prod",)`.
   - `rate_budgets`: REST 120/min and squid 120/min, IP-scoped, each with
     cost 0 for the other host.
3. **Address handling.** Store lowercase. Query the squid with
   `addresses.evm_checksum(address)`. Test: the request body carries the
   checksum, and a lowercase query (respx) would return `[]`.
4. **Numeric helpers in `gmx.py`** (the only module allowed `rounding=`):
   - `usd30(raw)`: half-even to 6 dp;
   - `qty12(raw, dec)`: `ROUND_DOWN`, `min(dec, 12)`;
   - `increase_price(size_usd, size_tokens, dec)`: half-even at 30 - dec dp;
   - `decrease_price(raw, dec)`: exact.

   Port the expected values of `tests/test_gmx_g0_examples.py` unchanged.
5. **Cash legs** (3.1), including the bad-debt cap and size-0 actions. Test
   the pnlUsd identity on every fixture decrease row (already pinned in the
   G0 test; keep it).
6. **Synthetic account** (2.1-2.4) as a pure class over (rows, `anchor_at`):
   `w`, the deposit with T1 dating, the B withdrawal and the floor. Replace
   the G0 test's reference with it: examples 1-3 must produce the same
   integers, TWRs and drawdowns.
7. **Position stream** (7). FILL_BASE by the running-total rule. In-scope
   check from `positionChanges.collateralToken`. Non-stable collateral goes
   to `UnsupportedActivity` through `drain_unsupported`.
8. **Cash stream** (7): trade-action cash events, synthetic flow events,
   claim events (5) with the market-scope rule.
   - Merge order, `T_cut`, `orderKey` join, finality margin, anchor
     crossing, cursor versioning.
   - Tests:
     - a page boundary inside one timestamp;
     - a missing join (raises);
     - a claim tie with a trade action in the same second;
     - the anchor in the middle of a page;
     - stream order (S-G5): a sync stopped after the fills stream and
       before the events stream must not yield a signal sized without the
       deposit that funds its fill. Stop and report if S-G5 has not landed.
9. **Restart test** (7.7) over a recorded multi-page history of one
   in-scope address: identical legs from `None` and from every page
   boundary. Also test through `ingest`: re-ingesting gives duplicates
   only, never a quarantine.
10. **`fetch_balances()`** from `/v1/positions` (REST, lowercase address
    accepted):
    - in-scope positions only, as `AssetAmount(base_asset,
      ±trunc12(sizeInTokens), 12, entry_price=fixed_to_decimal_str(entryPrice,
      30), mark_price=...)`;
    - `USDC` = the sum of their `collateralAmount`;
    - `equity_usd_micros=None`;
    - the anchor-timing guards of section 8 (fills inside the finality
      margin visible to `take_asset_anchor`'s settle check; the REST
      snapshot not behind `t0`).
11. **Replay test** (plan exit). Replaying the recorded history of one
    in-scope address up to the `/v1/positions` snapshot time must give:
    - every size equal to the snapshot's `trunc12(sizeInTokens)` (tolerance 0);
    - every `K_p` equal to `collateralAmount`, exactly;
    - `L` equal to the anchor's `USDC` row;
    - on every recorded change, `collateralAmount` after minus before equal
      to the joined trade action's `initialCollateralDeltaAmount` (the
      inference behind the worked examples' `dK`, 2.5), or the examples'
      `dK` are corrected from the recorded rows;
    - the same results with a fill a few seconds before the anchor and one
      a few seconds after it (section 8, "Anchor timing").

    Record a fresh in-scope address for this (address B in the G0 fixtures
    is WETH collateral). Anonymise as `tests/fixtures/gmx/README.md`
    describes.
12. **Market data** (`GmxMarketData`):
    - instruments from `/markets` + `/tokens`, both sides per perp market,
      with naming and asserts per section 6 and `copyable=False` for TradFi;
    - quotes from Oracle `/prices/tickers` (bid = minPrice, ask = maxPrice);
    - daily closes from `/v1/prices/ohlcv`;
    - funding from hourly `fundingRateSnapshots` + `borrowingRateSnapshots`,
      folded into `FundingRate(rate, short_rate)`;
    - `pricing_symbol` returns None for GMX perps.
    - Tests: two instruments sharing one index price, and the perp book plus
      paper sizing on `ETH.<tag>-S-PERP`. Pin that `analytics_v2` raises no
      `unpriced` flag for the stripped underlying.
13. **Leaderboard** (plan W-G):
    - `accountPnlRankMetrics` by `pnlUsd_DESC` and `pnlBps_DESC`, periods
      today/week/month/all;
    - `pnl` through `fixed_to_decimal_str` and `leaderboard_figure`;
    - `roi = pnlBps/10000`, labelled "PnL / max capital (GMX)";
    - `account_value=None`; addresses lowercased.
14. **Ownership proof:** EIP-191, the signer equal to the address. EIP-1271
    is a known gap.
15. **Smoke script** `scripts/gmx_arbitrum_smoke.py <addr>`:
    - schema introspection asserting every pinned field;
    - counts of fills, events and flags;
    - the replay-vs-`/v1/positions` check (sizes and collateral);
    - the share of the address's actions with non-stable collateral;
    - no other address ever printed.

    Run it from Fly `iad`.
16. **Venue doc:** fill the remaining sections in Hyperliquid's shape, and
    write the Terms-of-service position from a primary reading
    (`Position: UNREAD` until the founder accepts it).
17. **Demo blocks:** one synthetic leaderboard per period, and one watched
    synthetic address whose seed goes through the real money path (the
    connector's legs, not hand-made rows).
18. **Before handing back:** `uv run pytest` and ruff are green, and
    `graphify affected "PERP_SUFFIX"` lists no new caller.

## 12. Open [VERIFY] items carried to G1

- How common non-stable collateral is among leaderboard addresses: sample
  50 from the month and all-time boards in the smoke script. If most use
  WETH or BTC collateral, v1 covers few of the addresses people will
  watch, and non-stable collateral (3.3) moves up.
- Whether every OrderExecuted trade action has exactly one `positionChange`
  with its `orderKey`, including liquidations, ADL and keeper funding
  settlements.
- Liquidation rows with all fee fields at zero (the ARB row) next to rows
  carrying `liquidationFeeAmount` (the squid liquidation fixture): whether
  REST and GraphQL report liquidation fees differently.
- Subsquid `OR` in `where` (for the keyset), and the page size the squid
  accepts for the three-query request (`limit: 1000` worked for one query).
- The finality margin (300 s proposed).
- `uiFeeAmount` naming on executed rows (`uiFeeReceiver` / `uiFeeFactor` in
  the research; absent from the fixture rows).
- Whether `claimableFundingFeeInfos` gives a per-event claimable record with
  token amounts. If it does, booking at accrual becomes possible (5).
- Squid and API rate limits, and SQD Cloud terms (plan).
- That `/v1/prices/ohlcv?timeframe=1d` candles are UTC days, each `close`
  the last price before 00:00 UTC of the next day (the recorded candle
  timestamps are UTC midnights; the methodology's day boundary and the
  worked examples assume this).
- That `initialCollateralDeltaAmount` equals the change of
  `collateralAmount` on USDC-collateral positions too, including profitable
  decreases whose fees are paid from PnL (2.5; shown so far on address B's
  WETH rows and by example 1's t5).
- Whether the GMX API reports the block or time its `/v1/positions` snapshot
  is indexed to, for the anchor-timing guard (section 8).
