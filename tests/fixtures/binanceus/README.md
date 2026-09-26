Binance.US fixtures. No account exists yet (invariant #6: no live API calls in
tests), so private-endpoint fixtures are built from official docs' example
responses rather than captured from a real account. Re-record every one of
them from a real read-only key once the founder has a Binance.US account
(`scripts/binanceus_smoke.py` prints exactly the shapes to diff against).

## Public (captured live, no account/key involved)

Binance.US's public market-data endpoints are reachable from this
environment, so these were captured verbatim on 2026-09-26 by a one-off
`curl`, never by a test:

- `exchangeinfo.json` -- `GET https://api.binance.us/api/v3/exchangeInfo?symbols=["BTCUSD","ETHUSD"]`,
  trimmed to two symbols for a small fixture. `LOT_SIZE`/`MIN_NOTIONAL`
  filters and `baseAssetPrecision`/`quoteAssetPrecision` are real.
- `klines_btcusd_1d.json` -- `GET .../api/v3/klines?symbol=BTCUSD&interval=1d&limit=5`,
  trimmed to the first 2 of 5 bars returned.
- `bookticker_btcusd.json` -- `GET .../api/v3/ticker/bookTicker?symbol=BTCUSD`.

## Private (docs examples; NOT captured from a real account)

- `account.json` -- shape of `GET /api/v3/account`, docs example:
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints
  (checked 2026-09-26; Binance.US serves "the same shapes" per
  docs/research/2026-09-16-crypto-venues.md). Balances edited to give a
  small, hand-computable fixture (BTC free 0.5, USD free 1234.56 + locked
  10.00, ETH zero so it is excluded from discovery).
- `mytrades_btcusd.json` -- shape of `GET /api/v3/myTrades`, docs example:
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints
  (checked 2026-09-26). Two rows: a buy with the fee in the base asset
  (BTC) and a sell with the fee in the quote asset (USD), so the connector
  test covers both fee-asset branches.
- `apirestrictions_readonly.json` / `apirestrictions_writable.json` -- shape
  of `GET /sapi/v1/account/apiRestrictions`, docs example:
  https://developers.binance.com/docs/wallet/account/api-key-permission
  (checked 2026-09-26; exact path on api.binance.us is [VERIFY], see
  connectors/binanceus.py). Two variants hand-built for the two
  `prove_read_only` outcomes the test suite exercises.
- `deposit_history.json` -- shape of `GET /sapi/v1/capital/deposit/hisrec`,
  docs example: https://developers.binance.com/docs/wallet/capital/deposite-history
  (checked 2026-09-26).
- `withdraw_history.json` -- shape of `GET /sapi/v1/capital/withdraw/history`,
  docs example: https://developers.binance.com/docs/wallet/capital/withdraw-history
  (checked 2026-09-26). `applyTime` is deliberately the documented naive
  "YYYY-MM-DD HH:MM:SS" string (not epoch millis) to exercise that branch.
- `convert_tradeflow.json` -- shape of `GET /sapi/v1/convert/tradeFlow`,
  carried over from Binance global docs cited in
  docs/research/2026-09-16-crypto-venues.md (path on binance.us is
  [VERIFY]).
- `dustlog.json` -- shape of `GET /sapi/v1/asset/dust-log` (the batch /
  per-asset-detail nesting is the real, well-documented Binance dust-log
  shape); path on binance.us is [VERIFY] (`/sapi/v1/asset/dribblet` per the
  2026-09-16 research doc citing Binance global). Two detail rows share
  `fromAsset: "TRX"` on purpose, to test that `_dust_event` merges same-asset
  legs within one batch instead of producing a duplicate-leg error.
