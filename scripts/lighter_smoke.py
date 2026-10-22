"""Lighter smoke test: every read the connector makes, for one address.

No key exists for Lighter: every read is public (explorer logs and the
documented public API), so this works against any address. It prints counts,
the replayed vector against the venue's, and the reconcile verdict; never
another person's address or account index, and nothing secret (there is
nothing secret).

It is also the egress check from a Fly machine (onchain_perps_plan W-L exit):
  fly ssh console -C "python scripts/lighter_smoke.py 0x..."

Run locally:
  uv run python scripts/lighter_smoke.py 0x... [--max-pages 60]

A full replay of a busy account is long (about 90 explorer rows per page,
45 pages a minute): past --max-pages per stream the script stops, says the
replay is incomplete and skips the reconcile.

Optional ownership check: LIGHTER_CLAIM_SIGNATURE=0x... (a personal_sign of
the printed message by the address).

Exit status: 0 when every read worked (and, for a complete replay, the
vector reconciles), 1 otherwise.
"""

import argparse
import os
import sys
import time
from decimal import Decimal

from alphamarket.connectors.base import BudgetExhausted, LegKind, RateLimited
from alphamarket.connectors.lighter import (
    LighterConnector,
    LighterMarketData,
    claim_message,
    encode_claim_proof,
)
from alphamarket.money import format_micros, from_atoms

CLAIM_CODE = os.environ.get("LIGHTER_CLAIM_CODE", "koine-smoketest")
USDC_TOLERANCE = Decimal("0.001")  # reconcile's 0.1% of the venue amount


def run_pass(fetch, max_pages: int):
    """All pages of one pass (items, complete?), pausing on rate limits."""
    items, cursor, pages = [], None, 0
    while pages < max_pages:
        try:
            page = fetch(cursor)
        except (RateLimited, BudgetExhausted) as exc:
            print(f"  rate limited; waiting {exc.retry_after_seconds:.0f} s")
            time.sleep(exc.retry_after_seconds)
            continue
        pages += 1
        items.extend(page.items)
        if page.next_cursor is None:
            return items, True, pages
        cursor = page.next_cursor
    return items, False, pages


def vector_of(fills, events) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for leg in [leg for f in fills for leg in f.legs] + [leg for e in events for leg in e.legs]:
        out[leg.asset] = out.get(leg.asset, Decimal(0)) + from_atoms(leg.delta_atoms, leg.decimals)
    return {k: v for k, v in out.items() if v}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("address", help="the 0x L1 address")
    parser.add_argument("--max-pages", type=int, default=60, help="per stream (default 60)")
    args = parser.parse_args()
    try:
        conn = LighterConnector(args.address)
    except ValueError as exc:
        print(f"Not a usable address: {exc}")
        return 1

    indexes = conn.account_indexes()
    print(f"Account indexes: {len(indexes)} (master and sub-accounts)")
    if not indexes:
        print("Lighter has no account at this address.")
        return 1
    evidence = conn.prove_read_only()
    print(f"Read-only proof: {evidence.method} read_only={evidence.read_only}")

    balances = conn.fetch_balances()
    print(f"Venue vector as of {balances.as_of}: {len(balances.assets)} assets")
    for amount in balances.assets:
        entry = f" entry {amount.entry_price}" if amount.entry_price else ""
        print(f"  {amount.asset:>16} {from_atoms(amount.atoms, amount.decimals)}{entry}")
    if balances.equity_usd_micros is not None:
        print(
            "  equity (Lighter's total_asset_value): "
            f"${format_micros(balances.equity_usd_micros, 6)}"
        )

    fills, fills_done, fill_pages = run_pass(conn.fetch_asset_fills_page, args.max_pages)
    print(
        f"Fills: {len(fills)} in {fill_pages} pages ({'complete' if fills_done else 'incomplete'})"
    )
    events, events_done, event_pages = run_pass(conn.fetch_events_page, args.max_pages)
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e.kind.value] = kinds.get(e.kind.value, 0) + 1
    print(f"Events: {kinds} in {event_pages} pages ({'complete' if events_done else 'incomplete'})")
    flags = conn.drain_unsupported()
    if flags:
        reasons: dict[str, int] = {}
        for f in flags:
            reasons[f.reason] = reasons.get(f.reason, 0) + 1
        print(f"Unsupported activity flagged: {reasons}")
    pnl = sum(
        (
            from_atoms(leg.delta_atoms, leg.decimals)
            for f in fills
            for leg in f.legs
            if leg.kind is LegKind.REALIZED_PNL
        ),
        Decimal(0),
    )
    print(f"Derived realized PnL: {pnl} USDC")

    data = LighterMarketData()
    instruments = data.fetch_instruments()
    print(
        f"Instruments: {len(instruments)} ({sum(not i.copyable for i in instruments)} not copyable)"
    )
    quote = data.fetch_quote("BTC")
    print(f"BTC quote: bid {quote.bid if quote else None} ask {quote.ask if quote else None}")

    ok = True
    if fills_done and events_done:
        replayed = vector_of(fills, events)
        venue = {a.asset: from_atoms(a.atoms, a.decimals) for a in balances.assets}
        print("Reconcile (replayed vs venue):")
        for asset in sorted(set(replayed) | set(venue)):
            mine, theirs = replayed.get(asset, Decimal(0)), venue.get(asset, Decimal(0))
            if asset.endswith("-PERP"):
                good = mine == theirs
            else:
                good = abs(mine - theirs) <= abs(theirs) * USDC_TOLERANCE + Decimal("0.000001")
            ok = ok and good
            mark = "ok" if good else "MISMATCH"
            print(f"  {asset:>16} replayed {mine} venue {theirs} diff {mine - theirs} {mark}")
        if flags:
            print("  (flagged pool/staking moves are outside the vector by design)")
    else:
        print("Replay incomplete: raise --max-pages to reconcile.")

    print("\nClaim message to sign (personal_sign):\n")
    print(claim_message(conn.address, CLAIM_CODE))
    signature = os.environ.get("LIGHTER_CLAIM_SIGNATURE", "").strip()
    if signature:
        good = conn.verify_claim(encode_claim_proof(CLAIM_CODE, signature))
        print(f"\nSignature check: {good}")
        ok = ok and bool(good)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
