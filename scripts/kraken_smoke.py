"""Kraken smoke test: signed read-only requests against a real account.

Prereqs:
  1. In Kraken: Settings -> Connections & API -> Create API key with ONLY
     Query Funds, Query Closed Orders & Trades and Query Ledger Entries.
  2. export KRAKEN_API_KEY=...  KRAKEN_API_SECRET=...   (never commit them)

Run:  uv run python scripts/kraken_smoke.py [--record tests/fixtures/kraken/real]

Prints the read-only proof (what each probe returned), the balance vector and
the first page of fills and events. It never prints the key or the secret.
AddOrder is only ever sent with validate=true (Kraken checks it and never
sends it to the matching engine). With --record, the raw JSON bodies of the
private responses are saved to that directory so the docs-example fixtures
can be replaced by real ones (see tests/fixtures/kraken/README.md); request
headers, which carry the key and signature, are never saved.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

from alphamarket.connectors.kraken import KrakenConnector
from alphamarket.money import format_atoms

MAX_REQUESTS_PER_STREAM = 6


def _recorder(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}

    def hook(response: httpx.Response) -> None:
        response.read()
        method = response.request.url.path.rsplit("/", 1)[-1]
        counter["n"] += 1
        path = directory / f"{counter['n']:02d}_{method}.json"
        path.write_text(response.text)  # verbatim: numbers keep their printed digits

    return hook


def _first_items(fetch_page):
    cursor, pages = None, 0
    while pages < MAX_REQUESTS_PER_STREAM:
        page = fetch_page(cursor)
        pages += 1
        if page.items or page.next_cursor is None:
            return page.items, pages
        cursor = page.next_cursor
    return [], pages


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, help="save raw private responses here")
    args = parser.parse_args()
    key, secret = os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET")
    if not key or not secret:
        print("Set KRAKEN_API_KEY and KRAKEN_API_SECRET first (see the docstring).")
        return 1
    hooks = {"response": [_recorder(args.record)]} if args.record else {}
    client = httpx.Client(timeout=20.0, event_hooks=hooks)
    conn = KrakenConnector(api_key=key, api_secret=secret, client=client)

    evidence = conn.prove_read_only()
    verdict = {True: "READ-ONLY (accepted)", False: "CAN TRADE OR WITHDRAW (rejected)"}
    print(f"Read-only proof ({evidence.method}): {verdict.get(evidence.read_only, 'undetermined')}")
    for name, value in evidence.details.items():
        print(f"  {name}: {value}")

    balances = conn.fetch_balances()
    print(f"\nBalances as of {balances.as_of}:")
    for amount in balances.assets:
        print(f"  {amount.asset:>10}  {format_atoms(amount.atoms, amount.decimals)}")

    fills, requests = _first_items(conn.fetch_asset_fills_page)
    print(f"\nFirst page of fills ({requests} requests): {len(fills)}")
    for fill in fills[:10]:
        legs = ", ".join(
            f"{leg.kind}:{leg.asset} {format_atoms(leg.delta_atoms, leg.decimals)}"
            for leg in fill.legs
        )
        symbol = fill.instrument.symbol
        print(f"  {fill.filled_at}  {symbol} {fill.action} @ {fill.price}  [{legs}]")

    events, requests = _first_items(conn.fetch_events_page)
    print(f"\nFirst page of events ({requests} requests): {len(events)}")
    for event in events[:10]:
        legs = ", ".join(
            f"{leg.kind}:{leg.asset} {format_atoms(leg.delta_atoms, leg.decimals)}"
            for leg in event.legs
        )
        print(f"  {event.occurred_at}  {event.description}  [{legs}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
