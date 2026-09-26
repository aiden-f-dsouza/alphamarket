"""Hyperliquid smoke test: one pass of every read the connector makes.

No key exists for Hyperliquid: every read is public, so this works against
ANY address (yours once it exists, or a public leaderboard account). It
never prints anything secret because nothing secret is involved; a claim
signature is not a secret either, but it is only checked, never echoed.

Run:
  HYPERLIQUID_ADDRESS=0x... uv run python scripts/hyperliquid_smoke.py

Optional, to test the ownership proof end to end:
  1. Run once; copy the printed claim message.
  2. Sign it in your wallet with personal_sign (e.g. MetaMask "Sign message",
     or `cast wallet sign "<message>"`).
  3. Re-run with HYPERLIQUID_CLAIM_SIGNATURE=0x... set.

Budget: about 250 weight of the 1,200/min per-IP limit.
"""

import json
import os
import sys

from alphamarket.connectors.hyperliquid import (
    HyperliquidConnector,
    claim_message,
    encode_claim_proof,
    is_address,
)
from alphamarket.money import format_atoms, format_micros

CLAIM_CODE = os.environ.get("HYPERLIQUID_CLAIM_CODE", "koine-smoketest")


def main() -> int:
    address = os.environ.get("HYPERLIQUID_ADDRESS", "").strip()
    if not is_address(address):
        print("Set HYPERLIQUID_ADDRESS to a 0x address (40 hex characters).")
        return 1
    conn = HyperliquidConnector(address)

    evidence = conn.prove_read_only()
    print(f"Read-only proof: {evidence.method} read_only={evidence.read_only}")
    for key, value in evidence.details.items():
        print(f"  {key}: {value}")

    print(f"Abstraction mode: {conn.abstraction_mode()}")
    balances = conn.fetch_balances()
    print(f"Balances as of {balances.as_of}:")
    for amount in balances.assets:
        entry = f" entry {amount.entry_price}" if amount.entry_price else ""
        print(f"  {amount.asset:>16} {format_atoms(amount.atoms, amount.decimals)}{entry}")
    if balances.equity_usd_micros is not None:
        print(f"  USDC account value: ${format_micros(balances.equity_usd_micros, 6)}")

    fills = conn.fetch_asset_fills_page()
    print(f"First fills page: {len(fills.items)} fills, more={fills.next_cursor is not None}")
    for fill in fills.items[-5:]:
        legs = ", ".join(
            f"{leg.kind.value} {format_atoms(leg.delta_atoms, leg.decimals)} {leg.asset}"
            for leg in fill.legs
        )
        print(f"  {fill.filled_at} {fill.instrument.symbol} {fill.action} @ {fill.price}: {legs}")

    events = conn.fetch_events_page()  # funding stream, first page
    print(f"First funding page: {len(events.items)} events")
    cursor = events.next_cursor
    for _ in range(5):  # skip the rest of the funding stream, politely
        if not cursor or json.loads(cursor).get("p") != "funding":
            break
        cursor = conn.fetch_events_page(cursor).next_cursor
    if cursor and json.loads(cursor).get("p") == "ledger":
        ledger = conn.fetch_events_page(cursor)
        print(f"First ledger page: {len(ledger.items)} events")
        for event in ledger.items[-5:]:
            print(f"  {event.occurred_at} {event.kind}: {event.description}")

    print("\nClaim message to sign (personal_sign):\n")
    print(claim_message(address, CLAIM_CODE))
    signature = os.environ.get("HYPERLIQUID_CLAIM_SIGNATURE", "").strip()
    if signature:
        ok = conn.verify_claim(encode_claim_proof(CLAIM_CODE, signature))
        print(f"\nSignature check: {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
