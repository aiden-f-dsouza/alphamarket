"""Pacifica smoke test: one pass of every read the connector makes, for one
Solana address given on the command line (or PACIFICA_ADDRESS).

No key exists for Pacifica: every read is public, so this works against ANY
address (a public leaderboard account, or yours once it exists). It prints
counts and the reconcile result, never an address (the one you pass is shown
shortened) and never the address of anyone on the leaderboard.

It is also the Fly `iad` egress check (docs/venues/pacifica.md: the terms bar
US persons and entities and name the API; the API answered US IPs with 200 on
2026-10-22, and the docs list a 403 "restricted region" code):

  fly ssh console -C "python scripts/pacifica_smoke.py <address>"

A 403 means Pacifica blocks that egress. Stop there: the terms forbid
circumventing it, so nobody works around it with a proxy or another region.

Run locally:
  uv run python scripts/pacifica_smoke.py <address>

Budget: the IP bucket is 100 credits per 60 s. A history page costs 9, so the
script walks at most MAX_PAGES pages per stream (default 6, one page is up to
1,000 rows) and sleeps when the venue answers 429. Pass --max-pages N to walk
more; the reconcile is only exact when every stream was walked to the end.

Optional, to test the ownership proof end to end:
  1. Run once; copy the printed claim message.
  2. Sign it in Phantom or Solflare ("Sign Message") with the wallet that
     holds the address.
  3. Re-run with PACIFICA_CLAIM_SIGNATURE=<base58, base64 or 0x signature> set.
"""

import argparse
import os
import sys
import time

import httpx

from alphamarket import reconcile
from alphamarket.connectors.addresses import AddressKind, InvalidAddress, normalize, short
from alphamarket.connectors.base import RateLimited
from alphamarket.connectors.pacifica import (
    PacificaConnector,
    PacificaMarketData,
    claim_message,
    encode_claim_proof,
)
from alphamarket.ledger import balance_vector
from alphamarket.money import format_atoms, format_micros

CLAIM_CODE = os.environ.get("PACIFICA_CLAIM_CODE", "koine-smoketest")
MAX_PAGES = 6


def patient(call, *args):
    """Run a connector call, sleeping out a 429 (the script has no limiter)."""
    while True:
        try:
            return call(*args)
        except RateLimited as exc:
            wait = max(1.0, exc.retry_after_seconds) + 1
            print(f"  ... rate limited on {exc.endpoint}, waiting {wait:.0f}s")
            time.sleep(wait)


def walk(page_fn, max_pages: int):
    """Pages of one stream until its pass ends or the cap. (items, complete)."""
    items, cursor = [], None
    for _ in range(max_pages):
        page = patient(page_fn, cursor)
        items.extend(page.items)
        if page.next_cursor is None:
            return items, True
        cursor = page.next_cursor
    return items, False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("address", nargs="?", default=os.environ.get("PACIFICA_ADDRESS", ""))
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    args = parser.parse_args()
    try:
        address = normalize(AddressKind.SOLANA, args.address)
    except InvalidAddress as exc:
        print(f"Pass a Solana address as the argument or PACIFICA_ADDRESS. {exc}")
        return 1
    print(f"Address {short(address)}")
    conn = PacificaConnector(address)

    try:
        markets = patient(conn.markets)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            print("403 from Pacifica: this egress is blocked. Do not work around it.")
        else:
            print(f"HTTP {exc.response.status_code} from /info")
        return 1
    perps = [m for m in markets.values() if m.kind == "perpetual"]
    rwa = [m for m in perps if not m.copyable]
    print(
        f"Egress ok. /info: {len(perps)} perp markets ({len(rwa)} real-world-asset, not copyable)"
    )

    evidence = conn.prove_read_only()
    print(f"Read-only proof: {evidence.method} read_only={evidence.read_only}")

    balance = patient(conn.check_connection)
    print(
        f"Account found. Equity ${format_micros(balance.balance_micros, 6)} as of {balance.as_of}"
    )
    balances = patient(conn.fetch_balances)
    for amount in balances.assets:
        entry = f" entry {amount.entry_price}" if amount.entry_price else ""
        print(f"  {amount.asset:>12} {format_atoms(amount.atoms, amount.decimals)}{entry}")

    fills, fills_done = walk(lambda cursor: conn.fetch_asset_fills_page(cursor), args.max_pages)
    print(f"Fills: {len(fills)} ({'complete' if fills_done else 'cap reached, partial'})")
    for fill in fills[-3:]:
        legs = ", ".join(
            f"{leg.kind.value} {format_atoms(leg.delta_atoms, leg.decimals)} {leg.asset}"
            for leg in fill.legs
        )
        print(f"  {fill.filled_at} {fill.instrument.symbol} {fill.action} @ {fill.price}: {legs}")
    events, events_done = walk(lambda cursor: conn.fetch_events_page(cursor), args.max_pages * 2)
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event.kind.value] = kinds.get(event.kind.value, 0) + 1
    print(f"Events: {len(events)} ({'complete' if events_done else 'partial'}) {kinds}")
    flags = conn.drain_unsupported()
    print(f"Unsupported activity flagged: {len(flags)}")
    for flag in flags[:5]:
        print(f"  {flag.occurred_at} {flag.activity_type} ({flag.reason})")

    if fills_done and events_done:
        legs = [leg for f in fills for leg in f.legs] + [leg for e in events for leg in e.legs]
        checks = reconcile.compare_vectors(balance_vector(legs), balances.assets)
        for c in checks:
            print(
                f"  reconcile {c.asset:>12}: venue {format_atoms(c.venue_atoms, c.decimals)} "
                f"ledger {format_atoms(c.computed_atoms, c.decimals)} "
                f"diff {format_atoms(c.divergence_atoms, c.decimals)} "
                f"tolerance {format_atoms(c.tolerance_atoms, c.decimals)} "
                f"{'ok' if c.ok else 'MISMATCH'}"
            )
        print(
            "Reconcile: "
            + ("ok" if all(c.ok for c in checks) else "MISMATCH")
            + " (the venue's loan interest is not in the replay; see docs/venues/pacifica.md)"
        )
        reconciled = all(c.ok for c in checks)
    else:
        print("Reconcile skipped: history not walked to the end (--max-pages).")
        reconciled = True

    md = PacificaMarketData()
    boards = patient(md.fetch_leaderboards, ("day", "week", "month", "all"))
    private = md.private_addresses() or frozenset()
    print(
        "Leaderboard: "
        + ", ".join(f"{p} {len(rows)} rows" for p, rows in boards.items())
        + f"; {len(private)} privacy-flagged rows dropped"
    )
    flag = patient(md.owner_private, address)
    print(
        "This address's privacy flag: "
        + {
            True: "ON (Koine would refuse to watch it)",
            False: "off",
            None: "unknown (not on board)",
        }[flag]
    )

    print("\nClaim message to sign (ed25519, raw UTF-8 bytes):\n")
    print(claim_message(address, CLAIM_CODE))
    signature = os.environ.get("PACIFICA_CLAIM_SIGNATURE", "").strip()
    if signature:
        ok = conn.verify_claim(encode_claim_proof(CLAIM_CODE, signature))
        print(f"\nSignature check: {ok}")
    return 0 if reconciled else 2


if __name__ == "__main__":
    sys.exit(main())
