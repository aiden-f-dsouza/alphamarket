Kraken fixtures. Invariant #6: tests never call Kraken; these are the ground
truth the connector is tested against. No Kraken account exists yet, so the
private-endpoint files are NOT real account data.

Real public payloads, recorded 2026-09-26 by one-off unauthenticated calls to
`https://api.kraken.com/0/public/...` (no account involved):

- `asset_pairs.json`: `AssetPairs?pair=XXBTZUSD,XETHZUSD,XETHXXBT,USDCUSD,SOLUSD`,
  verbatim.
- `assets.json`: `Assets` (all 849), filtered to 18 codes covering the legacy
  X/Z codes, KFEE and the staked/held variants (ETH2.S, DOT28.S, XBT.M,
  USD.HOLD, ...).
- `ohlc_xxbtzusd_1440.json`: `OHLC?pair=XXBTZUSD&interval=1440&since=1757894400`,
  trimmed to the last 5 bars (2026-09-22..26) plus `last` (1790294400). The
  final bar (2026-09-26) is the running, uncommitted day.
- `ticker_xxbtzusd.json`: `Ticker?pair=XXBTZUSD`, verbatim.

Docs examples, copied verbatim on 2026-09-26; re-record from a real account:

- `docs_trades_history.json`: https://docs.kraken.com/api/docs/rest-api/get-trade-history
- `docs_ledgers.json`: https://docs.kraken.com/api/docs/rest-api/get-ledgers-info
- `docs_balance.json`: https://docs.kraken.com/api/docs/rest-api/get-account-balance
- `docs_withdraw_methods.json`: https://docs.kraken.com/api/docs/rest-api/get-withdrawal-methods

Synthetic account, docs-shaped (same fields and number formats as the docs
examples), hand-built so trades, ledger rows and balances agree exactly
(`balance = old_balance + amount - fee`, support.kraken.com "How to interpret
Ledger history fields"); re-record from a real account:

- `account_ledgers.json`: 11 rows, newest first: a 2000 USD deposit, three
  trades (two rows each), an ETH -> ETH2.S staking move (two transfer rows),
  a staking reward and a 100 USD withdrawal with a 0.35 fee.
- `account_trades.json`: the three trades with `ledgers` ids (as returned with
  `ledgers=true`). T1's `fee` has 5 dp (1.56052) while its ZUSD ledger row has
  4 (1.5605), the precision gap that makes fills use ledger amounts.
- `account_balance.json`: the resulting `Balance`.

To replace the synthetic files once the founder's key exists:
`uv run python scripts/kraken_smoke.py --record /tmp/kraken-real`, anonymise
ids and amounts if wanted, keep the numbers' printed digits, and update this
file. The Terms/permission status is in `docs/venues/kraken.md`.
