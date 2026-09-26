"""One-off recorder for Coinbase's PUBLIC (unauthenticated) market-data
fixtures. Invariant #6 forbids live calls from tests, but permits a real call
from a script like this one to seed the fixtures tests then run against.

Run:  uv run python scripts/record_coinbase_market_fixtures.py

Writes into tests/fixtures/coinbase/. No credentials involved: every request
here is the same public feed `CoinbaseMarketData` uses.
"""

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "https://api.coinbase.com"
OUT = Path(__file__).parent.parent / "tests" / "fixtures" / "coinbase"


def _save(name: str, resp: httpx.Response) -> None:
    (OUT / name).write_text(json.dumps(resp.json(), indent=2) + "\n")
    print(f"wrote {name} (HTTP {resp.status_code})")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=BASE, timeout=15.0) as client:
        _save(
            "coinbase_products.json",
            client.get("/api/v3/brokerage/market/products", params={"limit": 3}),
        )
        time.sleep(0.2)
        _save(
            "coinbase_product_btc_usd.json", client.get("/api/v3/brokerage/market/products/BTC-USD")
        )
        time.sleep(0.2)
        end = int(time.time())
        start = end - 5 * 86400
        _save(
            "coinbase_candles_btc_usd.json",
            client.get(
                "/api/v3/brokerage/market/products/BTC-USD/candles",
                params={"start": start, "end": end, "granularity": "ONE_DAY"},
            ),
        )
        time.sleep(0.2)
        resp = client.get("/api/v3/brokerage/market/products/DOESNOTEXIST-USD")
        (OUT / "coinbase_product_404.json").write_text(json.dumps(resp.json(), indent=2) + "\n")
        print(f"wrote coinbase_product_404.json (HTTP {resp.status_code})")
    print("\nDone. Update tests/fixtures/coinbase/README.md if any shape changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
