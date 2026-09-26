"""One-time smoke test for a real Binance.US account, once one exists.

Nothing in this repo has a Binance.US account yet (docs/venues/binanceus.md),
so every mapping in connectors/binanceus.py is verified against official
docs' example payloads (tests/fixtures/binanceus/README.md), not a live
account. Run this once a read-only key is created to confirm the paths
marked [VERIFY] in binanceus.py / registry.py / docs/venues/binanceus.md
actually match, then update those comments and re-record the fixtures.

Prereqs:
  1. In Binance.US: Account -> API Management -> Create API.
  2. Enable Reading ONLY. Leave Enable Spot & Margin Trading, Enable
     Withdrawals, Enable Margin, Enable Futures and Enable Internal Transfer
     all OFF. Restrict to Koine's IP once one is published (docs/security.md).
  3. Set BINANCEUS_API_KEY and BINANCEUS_API_SECRET in the environment (not
     settings.py / .env: this connector takes its base URL and credentials
     directly, see connectors/binanceus.py "module constants").

Run:  BINANCEUS_API_KEY=... BINANCEUS_API_SECRET=... uv run python scripts/binanceus_smoke.py

Never prints the secret key. If a key turns out to be writable, the script
says so loudly and the connect flow (once wired) must refuse it (invariant 4).
"""

import os
import sys

from alphamarket.connectors.binanceus import BASE_URL, BinanceUSConnector
from alphamarket.money import format_atoms


def main() -> int:
    api_key = os.environ.get("BINANCEUS_API_KEY", "")
    api_secret = os.environ.get("BINANCEUS_API_SECRET", "")
    if not api_key or not api_secret:
        print("Set BINANCEUS_API_KEY and BINANCEUS_API_SECRET in the environment first.")
        return 1

    print(f"Base URL: {BASE_URL}")
    connector = BinanceUSConnector(base_url=BASE_URL, api_key=api_key, api_secret=api_secret)

    evidence = connector.prove_read_only()
    print(f"Read-only proof method: {evidence.method}")
    print(f"Read-only: {evidence.read_only}")
    print(f"Flags (never secrets): {evidence.details}")
    if evidence.read_only is False:
        print(
            "\n*** THIS KEY CAN WRITE. Do not connect it. Create a new key with "
            "only Enable Reading checked. ***\n"
        )

    balances = connector.fetch_balances()
    print(f"\nBalances as of {balances.as_of}:")
    for amount in balances.assets:
        print(f"  {amount.asset}: {format_atoms(amount.atoms, amount.decimals)}")

    print("\nDiscovering traded symbols and fetching the first page of fills...")
    page = connector.fetch_asset_fills_page()
    print(f"Fills fetched (first page): {len(page.items)}")
    for fill in page.items[:5]:
        print(
            f"  {fill.filled_at}  {fill.instrument.symbol}  {fill.action} "
            f"{format_atoms(fill.qty_atoms, fill.instrument.qty_decimals)} @ {fill.price}"
        )
    print(f"next_cursor set: {page.next_cursor is not None}")
    print(f"resume_cursor set: {page.resume_cursor is not None}")

    print("\nFetching the first page of events (deposits/withdrawals/convert/dust)...")
    events_page = connector.fetch_events_page()
    print(f"Events fetched (first page): {len(events_page.items)}")
    for event in events_page.items[:5]:
        print(f"  {event.occurred_at}  {event.kind}  {event.venue_ref}")

    print("\nSigning works if no exception was raised above. See docs/venues/binanceus.md")
    print("for which paths above are still [VERIFY] -- update it once this ran clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
