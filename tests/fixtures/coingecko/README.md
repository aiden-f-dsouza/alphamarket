# CoinGecko fixtures

HAND-BUILT from CoinGecko's documented response shapes (no account or demo
key exists yet, and invariant 6 forbids live calls in tests), NOT recorded:

- `market_chart_range_*.json`: `GET /coins/{id}/market_chart/range` returns
  `{"prices": [[unix_ms, price], ...], "market_caps": [...], "total_volumes":
  [...]}`. Hourly granularity (2-90 day ranges) is modelled with samples a few
  seconds to two minutes off the hour, as CoinGecko stamps them; the daily
  granularity (ranges over 90 days) with samples exactly at 00:00 UTC.
  Prices are written as plain decimals with more digits than a float keeps
  (`61234.56789123`, `0.000012345678`) to prove no precision is lost.
- `error_coin_not_found.json`: the 404 body of an unknown coin id.
- `error_invalid_key.json`: the shape of the 401/429 error body.

[VERIFY] every shape and the 00:00 UTC / granularity behaviour against a real
call with the demo key (docs/venues/coingecko.md) and replace these with
recordings when the key exists.
