"""Coinbase Advanced Trade smoke test: prove a CDP key signs correctly and is
read-only, then print balances and the first page of fills and events.

Prereqs:
  1. Create a CDP API key scoped to View only (Coinbase Developer Platform ->
     API keys -> new key). Note the key name
     ("organizations/{org}/apiKeys/{id}") and the private key it shows once.
  2. Set env vars (no .env.example entry yet -- this venue needs written CDP
     approval before it goes in the normal connect flow, see
     docs/venues/coinbase.md):
       COINBASE_KEY_NAME       the full "organizations/.../apiKeys/..." string
       COINBASE_PRIVATE_KEY    the key material, base64 (Ed25519) or PEM (ES256)
     or COINBASE_PRIVATE_KEY_PATH pointing at a file holding the key material.

Run:  uv run python scripts/coinbase_smoke.py

Never prints the key material itself, only what the API says about it.
"""

import os
import sys
from pathlib import Path

from alphamarket.connectors.coinbase import CoinbaseConnector, CoinbaseMarketData, load_private_key
from alphamarket.money import format_atoms


def _read_secret() -> str | None:
    if secret := os.environ.get("COINBASE_PRIVATE_KEY"):
        return secret
    if path := os.environ.get("COINBASE_PRIVATE_KEY_PATH"):
        return Path(path).read_text()
    return None


def main() -> int:
    key_name = os.environ.get("COINBASE_KEY_NAME", "")
    secret = _read_secret()
    if not key_name or not secret:
        print(
            "Set COINBASE_KEY_NAME and COINBASE_PRIVATE_KEY(_PATH) first (see this file's header)."
        )
        return 1

    alg, private_key = load_private_key(secret)
    print(f"Key: {key_name} (alg {alg})")
    connector = CoinbaseConnector(key_name=key_name, private_key=private_key, alg=alg)

    evidence = connector.prove_read_only()
    print(f"Read-only proof: {evidence.method.value} -> read_only={evidence.read_only}")
    print(f"  details: {evidence.details}")
    if evidence.read_only is False:
        print("WRITABLE KEY -- the connect flow would REJECT this. Create a View-only key.")

    balances = connector.fetch_balances()
    print(f"\nBalances (as of {balances.as_of}):")
    for asset in balances.assets:
        print(f"  {asset.asset}: {format_atoms(asset.atoms, asset.decimals)}")

    fills = connector.fetch_asset_fills()
    print(f"\nFills fetched: {len(fills)}")
    for fill in fills[-5:]:
        print(
            f"  {fill.filled_at}  {fill.instrument.symbol}  {fill.action} "
            f"{fill.qty_atoms} atoms @ {fill.price}"
        )

    events = connector.fetch_events()
    print(f"\nEvents fetched: {len(events)}")
    for event in events[-5:]:
        print(f"  {event.occurred_at}  {event.kind}  {event.venue_ref}")

    md = CoinbaseMarketData()
    quote = md.fetch_quote("BTC-USD")
    if quote:
        print(f"\nBTC-USD last: {quote.last} (as of {quote.as_of})")

    print("\nAll mappings are as documented in connectors/coinbase.py; anything")
    print("marked [VERIFY] there should be double-checked against this account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
