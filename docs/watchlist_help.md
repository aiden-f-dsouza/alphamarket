# How these are computed

Help for the "How this trader trades" section of a watched address
(`/watch/{id}`). It is private help for people using their own watchlist, not
part of the public methodology: nothing here is a verified track record, and
none of it appears on a public strategy page. The numbers describe habits
read from the address's public trades. They say nothing about who trades it
or whether the habits will continue.

The section needs at least 10 fills. Below that it shows "not enough history
yet", because every figure would rest on a handful of trades. A figure that
cannot be computed from the data (no funding payments, no prediction-market
trades, no equity history yet) is left out rather than guessed.

All times are UTC. Money is exact (whole micro-dollars); quantities are exact
decimals. Only the final ratios and percentages are rounded for display.

## Holding periods

Trades are matched first-in-first-out per instrument (for prediction markets,
per market and side). Every lot, or part of a lot, that a later trade closes
is one *round trip piece*, lasting from the time the lot was opened to the
time it was closed. A partial close leaves the rest of the lot open for a
later piece. A perp trade that crosses zero closes everything open and opens a
new position the other way with what is left (a flip). A prediction-market lot
still open when its market settles closes at the settlement time.

The histogram counts pieces by duration, lower bound included:
under 1 hour, 1 to 24 hours, 1 to 7 days, 7 to 30 days, 30 days or more. The
median is the middle duration of all pieces (the mean of the two middle ones
for an even count). Lots still open are not counted. A spot or prediction
sell with no inventory in the history (it came from before our data starts) is
ignored, not turned into a short.

## Trade frequency

- Per active day: fills / the number of UTC days with at least one fill.
- Per week: fills / (span in days / 7), where the span counts calendar days
  from the first fill to the last, both included.

A fill is one execution; one order can produce several.

## When they trade

A grid of fill counts: weekday (Monday to Sunday) by hour of day (0 to 23),
all UTC. Darker cells have more fills; the shade is relative to the busiest
cell.

## What they trade

Each instrument's share is its notional (price times quantity, summed over
all its fills) divided by the notional of all fills. The list shows the five
largest. The concentration figure is the Herfindahl-Hirschman index: the sum
of the squared shares, from 1/n (spread evenly over n instruments) up to 1
(everything in one instrument). For example, shares of 60%, 30% and 10% give
0.36 + 0.09 + 0.01 = 0.46.

## Position sizing

An *entry* is a fill that opens a position from flat; a flip's leftover
quantity counts as one. Its size is the notional it opened, divided by the
address's equity at the close of the last complete UTC day before the fill
(the daily equity series Koine already computes). Entries before that series
starts, or on a day with equity of zero or less, are left out. The median is
the middle size; the 90th percentile is the nearest-rank value,
`sorted[ceil(0.9 x n) - 1]`. For example, entries of 20% and 13% of equity
have a median of 16.5% and a p90 of 20%.

A *scale-in* is a fill that adds to an open position. A *scale-out* is a fill
that reduces a position without closing it. The fill that closes a position,
and the one that flips it, are neither.

## Perpetual futures

- **Leverage.** After each perp fill: the sum over perp positions of
  |position size| x last fill price, divided by equity (same equity rule as
  sizing). The average and the maximum are over perp fills. It uses the last
  traded price, not a mark, and ignores spot holdings, so it is an indication
  of how hard the address leans on its account, not the venue's own figure.
- **Long/short skew.** Only notional that opens or adds to a position counts:
  buys are long, sells are short. Long share = long / (long + short).
- **Funding paid.** Minus the sum of the funding payments the venue reported.
  Positive means the address paid funding over the period; negative, that it
  received more than it paid. Left out when no funding payments are recorded.

## Prediction markets

Every buy is an entry, put in a bucket by its price in cents: 0 to 20, 20 to
40, 40 to 60, 60 to 80, 80 to 100 (lower bound included, the last bucket
includes 100).

Sells are matched first-in-first-out against earlier buys of the same market
and side. By quantity:

- **Held to settlement** = quantity still held when the market settled /
  (that + quantity sold before settlement). Quantity in markets that have not
  settled yet is not counted.
- **Win rate** = quantity on the side that matched the settled result /
  quantity held through a settlement with a yes or no result. Entries sold
  early are not counted: whether the sale was a good one depends on the sale
  price, not the result.

For example, 10 contracts bought at 15 cents, 5 sold early and 5 held to a
yes result: the 0 to 20 bucket shows 50% held to settlement and a 100% win
rate.

## Copy feasibility

A follower of a watched address polls it about every five minutes (the
tail's sync interval, `ingest_interval_seconds`), so a paper copy is always
that far behind. A trade opened and closed faster than that cannot be copied.

- **Round trips shorter than the copy delay.** Over the closed round trip
  pieces of the holding-period figure (same first-in-first-out matching), the
  share whose holding period is strictly shorter than the delay. The headline
  is by closed notional: each piece counts at its entry price (its quantity
  times the venue's notional per unit for the fill that opened it), so a tiny
  scalp weighs little and a large position weighs a lot. The count (n of N)
  is shown beside it. Left out when nothing has closed yet.
  For example, a 1 unit trade at 10 closed after 60 seconds and a 10 unit
  trade at 100 closed after an hour, with a 300 second delay: 1 of 2 pieces is
  too fast, but only 10 / 1010 = 1.0% of the closed notional.
- **Median gap between trades.** Fills that share an order id are one *trade*,
  timed at the first of them; without an order id every fill is its own
  trade. The gap is the number of seconds between a trade and the one before;
  the figure is the median of the gaps (the mean of the two middle ones for an
  even count). It needs at least two trades.

Neither figure measures Koine's own observation delay beyond the sync
interval, or whether the venue would have filled a copy at the same price.
