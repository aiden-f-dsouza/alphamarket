# CoinGecko (fallback daily prices)

Status: **usable for daily closes, with two [VERIFY] items to settle before
real users** (commercial-use plan and visible attribution). CoinGecko is not a
venue: nothing is connected, no user credential is involved, and it never has a
`VenueProfile` (it lives only in `registry._MARKET_DATA_BUILDERS`, so
`build_connector("coingecko", ...)` raises). It prices crypto assets that no
connected venue lists, so a holding always has a USD value.

Checked: 2026-10-01 by the w2-market-data workstream **from memory of the public
documentation; nothing here was read against the live site or an account.**
Every item marked [VERIFY] must be checked first. Connector:
`src/alphamarket/connectors/coingecko.py`. Curated id map:
`registry.COINGECKO_COIN_IDS`.

## What Koine uses

- `GET {base}/coins/{id}/market_chart/range?vs_currency=usd&from=&to=` with the
  Koine demo key in the `x-cg-demo-api-key` header, base
  `https://api.coingecko.com/api/v3` (`COINGECKO_BASE_URL`). [VERIFY the path,
  the header name, and that demo keys use the `api.coingecko.com` host and not
  the `pro-api` one.]
- Nothing else: no search, no coin list at runtime (ids are curated by hand), no
  quotes, no websocket. One request per asset per missing date range.
- The close of day `d` is the price at 00:00 UTC of `d + 1`. CoinGecko's automatic
  granularity is hourly for 2 to 90 day ranges and daily (00:00 UTC) for longer
  ones [VERIFY]; Koine reads the sample nearest to each midnight within 30
  minutes and records no close when there is none that near.

## Terms position [VERIFY all of it]

- **Plan and commercial use.** The demo plan is documented as free, rate limited
  (about 30 calls a minute and a monthly call cap) and aimed at individuals and
  evaluation; commercial products are expected to use a paid plan. Koine shows
  values derived from CoinGecko data on public pages (fallback closes valuing
  creators' holdings), which is commercial use. **Decision needed before real
  users:** move to a paid plan, or get written confirmation that the demo plan
  covers this use. The key and base URL are settings; a paid plan uses another
  host and header, which needs a small connector change.
- **Attribution.** The free plans require attribution to CoinGecko ("Powered by
  CoinGecko API") with a link wherever their data is shown. Until a footer or the
  methodology page carries a visible link, a page that shows a fallback-priced
  number is out of compliance. The methodology page already names CoinGecko as
  the source of every fallback close; the visible attribution link is an open
  item for the pages workstream or integration.
- **Redistribution.** Koine shows derived USD values, not raw price series, and
  never offers CoinGecko's data as a download or an API. The public JSON API
  must keep it that way [VERIFY against the redistribution clause].
- **Rate limits.** `registry.DATA_SOURCE_BUDGETS["coingecko"]` is 30 calls a
  minute per egress IP [VERIFY against the account's actual plan]. A 429 raises
  `RateLimited` and the job defers.

## Data limits Koine accepts

- Prices are CoinGecko's aggregated market prices, not any one exchange's, so a
  fallback close can differ from the venue the trader used. Every such row is
  stored with `source = coingecko`, and `valuation_db.price_inputs` always
  prefers the pricing venue's own close when both exist.
- The demo plan's history is limited (about one year) [VERIFY]; an account
  anchored longer ago has no fallback closes before that.
- Unknown assets are never guessed: an asset not in the curated map stays
  unpriced and is displayed without a value.
- Coin ids can change when a coin migrates. Re-check the map against `/coins/list`
  before launch and when a coin stops returning data (the connector maps a 404 to
  "no closes", so a retired id fails quietly: watch for assets that stay
  unpriced).

## Fixtures

`tests/fixtures/coingecko/` is hand-built from the documented response shape, not
recorded (no key exists yet). Replace with recordings when the demo key exists.
