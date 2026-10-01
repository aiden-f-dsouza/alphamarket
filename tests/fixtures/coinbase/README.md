# Coinbase fixture provenance

No Coinbase account exists yet (`requires_partner_approval`, see
`docs/venues/coinbase.md`), so unlike the Kalshi/Polymarket fixtures these are
**not** all captured from a live account. Invariant #6 (no live API calls in
tests) still holds: every one of these is a file on disk that respx serves.

## Private-endpoint fixtures (docs examples, not a real account)

Built by hand from the official CDP API reference's own example payloads.
Field names, shapes and the two gotchas the task called out (`entry_id` vs
`trade_id`, `size_in_quote`) are copied from the docs; the numeric values are
invented so the connector's arithmetic can be hand-checked in the tests.
**Must be re-recorded from a real account** once CDP approval and a funded
account exist (`scripts/coinbase_smoke.py` at that point, mirroring how
`kalshi_fills.json` was captured).

- `coinbase_key_permissions_readonly.json` / `_writable.json` -- shape per
  `docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/data-api/get-api-key-permissions`
  (fetched 2026-09-26). The writable variant flips `can_trade` to exercise the
  "reject at connect" path.
- `coinbase_fills_page1.json` / `_page2.json` -- shape per
  `docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/orders/list-fills`
  (fetched 2026-09-26). Page 1 has a BTC-USD buy and an ETH-USD sell (both
  `size_in_quote: false`); page 2 has a BTC-USD buy with `size_in_quote: true`
  to exercise the base-quantity estimate. `entry_id` values differ from
  `trade_id` on purpose, matching the docs' warning that `trade_id` is not a
  safe idempotency key.
- `coinbase_accounts.json` -- shape per
  `docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/accounts/list-accounts`
  (fetched 2026-09-26). One crypto account with a zero hold, one fiat account
  with a non-zero hold, so `fetch_balances` is checked summing `available + hold`.
- `coinbase_transactions_btc.json` / `_usd.json` -- shape per
  `docs.cdp.coinbase.com/coinbase-app/track-apis/transactions` (fetched
  2026-09-26). The BTC account's second row is `advanced_trade_fill`, which
  the connector must skip (it duplicates a row `fetch_asset_fills_page`
  already returns); the USD account covers `fiat_deposit` and
  `staking_transfer`. **[VERIFY]** whether a CDP (Advanced Trade) key actually
  authenticates against the v2 API at all -- the task flagged this as
  unconfirmed, and it could not be checked without a real key.

## Public market-data fixtures (real, unauthenticated calls)

Captured verbatim on 2026-09-26 by a one-off shell command (invariant #6
permits calling public endpoints once to seed fixtures; never done by a test):

```
curl -sS "https://api.coinbase.com/api/v3/brokerage/market/products?limit=3"
curl -sS "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD"
curl -sS "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles?start=...&end=...&granularity=ONE_DAY"
curl -sS "https://api.coinbase.com/api/v3/brokerage/market/products/DOESNOTEXIST-USD"   # -> HTTP 404
```

- `coinbase_products.json` -- confirms the public product list needs no
  auth, and that `best_bid_price` / `best_ask_price` are empty strings on
  this feed (`fetch_quote` uses `price` only, see the connector's docstring).
- `coinbase_product_btc_usd.json` -- single-product shape, same fields.
- `coinbase_candles_btc_usd.json` -- confirms candles come back **newest
  first** (`start` descending); the connector sorts them back to oldest-first.
- `coinbase_product_404.json` -- an unknown product id, real HTTP 404 with an
  `{"error": "NOT_FOUND", ...}` body.

Rerun `scripts/record_coinbase_market_fixtures.py` to refresh these against
whatever BTC-USD is trading at when this venue is picked back up; the tests
only assert on shapes and arithmetic, not on today's price.
