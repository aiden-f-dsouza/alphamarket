# Alpaca fixtures

No Alpaca account or OAuth app exists yet, so every file here is built from
the official docs' example responses (fetched 2026-09-26 from the `.md`
versions of the pages below). **Re-record all of them from a real paper and
live account** with `scripts/alpaca_smoke.py` once the OAuth app is
registered, and note the date here.

| File | Source | Changes |
|---|---|---|
| `account.json` | docs example, <https://docs.alpaca.markets/us/reference/getaccount-1>, 2026-09-26 | verbatim |
| `positions.json` | docs example, <https://docs.alpaca.markets/us/reference/getallopenpositions>, 2026-09-26 | the docs list AAPL twice; the second row's symbol is changed to SPY and its qty to a fractional `2.123456789` (synthetic) |
| `activities_fills.json` | docs examples, <https://docs.alpaca.markets/us/docs/account-activities> and <https://docs.alpaca.markets/us/reference/getaccountactivities-2>, 2026-09-26 | rows 1-2 verbatim; rows 3-5 synthetic in the same shape: a fractional sell with a nanosecond timestamp, a `sell_short`, an OCC option fill |
| `activities_non_trade.json` | docs examples, same pages plus <https://docs.alpaca.markets/us/docs/non-trade-activities-for-option-events>, 2026-09-26 | the SDIV `DIV` row and the `OPEXP` row are verbatim (OPEXP id changed so ids stay unique); every other row is synthetic in the documented NTA shape (fields: id, activity_type, activity_sub_type, date, net_amount, symbol, qty, status, description) |
| `oauth_token.json` | docs example, <https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api>, 2026-09-26 | verbatim (it is the docs' own sample token, not a credential; note it carries write scopes) |
| `bars_daily.json`, `bars_daily_page2.json` | shape from <https://docs.alpaca.markets/us/reference/stockbars>, 2026-09-26 | synthetic `1Day` bars (the docs example is intraday); daily bars are stamped 00:00 New York time |
| `latest_quotes.json` | docs example, <https://docs.alpaca.markets/us/reference/stocklatestquotes-1>, 2026-09-26 | verbatim |
| `corporate_actions.json` | docs examples, <https://docs.alpaca.markets/us/reference/corporateactions-1>, 2026-09-26 | the per-type examples combined into one response |
| `assets.json` | shape from <https://docs.alpaca.markets/us/reference/get-v2-assets-1>, 2026-09-26 | synthetic rows |

Error bodies used inline in the tests (`{"code": ..., "message": ...}`) follow
Alpaca's documented error shape; the exact message for a missing OAuth scope
is **[VERIFY]**: the read-only probe does not depend on its wording, only on
the status code (and on "buying power"/"insufficient" for business refusals).

Unverified semantics the fixtures encode (check against real rows first):

- `SPLIT`/`SSP` `qty` is read as the signed change in shares.
- `JNLS`/`ACATS` `qty` is signed (+ in, - out).
- NTA `date` is date-only; fills use `transaction_time`.
- `after` filters FILL rows by transaction time.
