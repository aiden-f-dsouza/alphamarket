"""Alpaca smoke test: read-only proof, balances, first page of fills and events.

Prereqs (once Koine's Alpaca OAuth app exists):
  1. Connect the founder account through the app (GET /oauth/alpaca/start on a
     dev server), or obtain a zero-scope token by hand with Alpaca's OAuth flow.
  2. Put it in the environment, never in a file that is committed:
       export ALPACA_ENV=paper                # or live
       export ALPACA_ACCESS_TOKEN=...         # the OAuth access token
       export ALPACA_GRANTED_SCOPE=""         # the `scope` string from the token response
     Optional, for market data: ALPACA_DATA_KEY_ID / ALPACA_DATA_SECRET_KEY
     (Koine's own Alpaca API key pair).

Run:  uv run python scripts/alpaca_smoke.py [--record DIR]

On PAPER it sends the read-only probe order (zero shares of KOINEPROBE0: it
cannot execute); on LIVE it never sends anything but GETs. Prints no token.
With --record DIR it also writes the raw first pages as JSON into DIR (for
tests/fixtures/alpaca; review and redact before committing).
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from alphamarket.config import get_settings
from alphamarket.connectors.registry import build_connector, build_market_data
from alphamarket.money import format_atoms, format_micros


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", type=Path, default=None)
    args = parser.parse_args()

    settings = get_settings()
    env = os.environ.get("ALPACA_ENV", settings.alpaca_env)
    token = os.environ.get("ALPACA_ACCESS_TOKEN", "")
    if not token:
        print("ALPACA_ACCESS_TOKEN is empty; see the docstring.")
        return 1
    scope = os.environ.get("ALPACA_GRANTED_SCOPE")
    secret = json.dumps({"access_token": token, "scope": scope}) if scope is not None else token

    connector = build_connector("alpaca", env, "", secret)
    print(f"Environment: {env}")
    print(f"Account id: {connector.fetch_account_id()}")

    evidence = connector.prove_read_only()
    verdict = {True: "READ-ONLY", False: "WRITABLE (would be rejected)", None: "undetermined"}
    print(f"Read-only proof ({evidence.method}): {verdict[evidence.read_only]}")
    for key, value in evidence.details.items():
        print(f"  {key}: {value}")

    balances = connector.fetch_balances()
    print(f"Balances as of {balances.as_of}:")
    for amount in balances.assets:
        extra = ""
        if amount.entry_price:
            extra = f" entry {amount.entry_price} mark {amount.mark_price}"
        print(f"  {amount.asset}: {format_atoms(amount.atoms, amount.decimals)}{extra}")
    if balances.equity_usd_micros is not None:
        print(f"  equity: ${format_micros(balances.equity_usd_micros, 2)}")

    fills = connector.fetch_asset_fills_page()
    print(f"First fills page: {len(fills.items)} fills, next_cursor={fills.next_cursor}")
    for fill in fills.items[:10]:
        print(
            f"  {fill.filled_at}  {fill.action} {format_atoms(fill.qty_atoms, 9)} "
            f"{fill.instrument.symbol} @ {fill.price}  [{fill.venue_fill_id}]"
        )
    events = connector.fetch_events_page()
    print(f"First events page: {len(events.items)} events, next_cursor={events.next_cursor}")
    for event in events.items[:10]:
        legs = ", ".join(
            f"{leg.kind} {format_atoms(leg.delta_atoms, leg.decimals)} {leg.asset}"
            for leg in event.legs
        )
        print(f"  {event.occurred_at}  {event.description}: {legs}")
    unsupported = connector.unsupported_activities()
    print(f"Unsupported activities skipped: {len(unsupported)}")
    for item in unsupported[:10]:
        print(f"  {item.occurred_at}  {item.activity_type} {item.symbol or ''} ({item.reason})")

    if settings.alpaca_data_key_id:
        md = build_market_data("alpaca", env)
        end = date.today()
        closes = md.fetch_daily_closes("SPY", end - timedelta(days=7), end)
        print(f"SPY closes (7d): {[(c.day.isoformat(), c.close) for c in closes]}")
        print(f"SPY quote: {md.fetch_quote('SPY')}")

    if args.record:
        args.record.mkdir(parents=True, exist_ok=True)
        client = connector.client
        headers = {"Authorization": f"Bearer {token}"}
        for name, path, params in (
            ("account", "/v2/account", None),
            ("positions", "/v2/positions", None),
            ("activities_fills", "/v2/account/activities", {"activity_types": "FILL"}),
            ("activities_non_trade", "/v2/account/activities", {"category": "non_trade_activity"}),
        ):
            resp = client.get(f"{connector.base_url}{path}", params=params, headers=headers)
            out = args.record / f"{name}_{date.today().isoformat()}.json"
            out.write_text(json.dumps(resp.json(), indent=2) + "\n")
            print(f"recorded {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
