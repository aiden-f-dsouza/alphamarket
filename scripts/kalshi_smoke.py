"""M0 smoke test: one signed request against the Kalshi DEMO API.

Prereqs:
  1. Create a demo account at https://demo.kalshi.co and generate an API key
     (choose READ-ONLY scope). Download the private key PEM into keys/.
  2. Copy .env.example to .env, set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH.

Run:  uv run python scripts/kalshi_smoke.py

If this prints a balance and a fills count, signing works and M1 can start.
Getting RSA-PSS right is the fiddly part of the whole connector; that is why
this script exists before any real code depends on it.
"""

import sys

from alphamarket.config import get_settings
from alphamarket.connectors.kalshi import KalshiConnector, load_private_key
from alphamarket.money import format_micros


def main() -> int:
    settings = get_settings()
    if not settings.kalshi_key_id:
        print("KALSHI_KEY_ID is empty. Fill in .env first (see .env.example).")
        return 1

    print(f"Environment: {settings.kalshi_env} ({settings.kalshi_base_url})")
    connector = KalshiConnector(
        base_url=settings.kalshi_base_url,
        key_id=settings.kalshi_key_id,
        private_key=load_private_key(settings.kalshi_private_key_path),
    )

    balance = connector.fetch_balance()
    print(f"Balance: ${format_micros(balance.balance_micros)} (as of {balance.as_of})")

    writable = connector.check_write_scope()
    scope = {
        False: "READ-ONLY (connect flow will accept it)",
        True: "READ+WRITE (connect flow will REJECT it; create a read-only key)",
        None: "undetermined",
    }[writable]
    print(f"Key scope: {scope}")

    fills = connector.fetch_fills()
    print(f"Fills fetched: {len(fills)}")
    for fill in fills[-5:]:
        print(
            f"  {fill.filled_at}  {fill.ticker}  {fill.action} "
            f"{format_micros(fill.count_micros, 2)} {fill.side} "
            f"@ ${format_micros(fill.price_micros)} (fee ${format_micros(fill.fee_micros, 6)})"
        )

    settlements = connector.fetch_settlements()
    print(f"Settlements fetched: {len(settlements)}")
    print("\nSigning works. All payload mappings are verified against real")
    print("demo captures; see tests/fixtures/README.md for provenance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
