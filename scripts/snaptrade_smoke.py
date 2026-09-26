"""SnapTrade smoke test: run once a connected brokerage account exists.

Prereqs:
  1. A SnapTrade developer account (free tier: https://snaptrade.com), with
     SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY set in .env.
  2. A registered SnapTrade user with at least one connected brokerage
     account (via generate_connection_portal_url()'s redirect, connectionType
     "read"). Set SNAPTRADE_USER_ID / SNAPTRADE_USER_SECRET in .env (or pass
     them as argv[1] / argv[2]) -- these are NOT the same thing as this
     script's own client_id/consumer_key.

Run:  uv run python scripts/snaptrade_smoke.py [user_id] [user_secret]

Prints prove_read_only() evidence, balances, and the first page of fills and
events -- never the userSecret or consumer key (invariant 4)."""

import sys

from alphamarket.config import get_settings
from alphamarket.connectors.snaptrade import BASE_URL, SnapTradeConnector
from alphamarket.money import format_atoms


def main() -> int:
    settings = get_settings()
    if not settings.snaptrade_client_id or not settings.snaptrade_consumer_key:
        print("SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY are empty. Fill in .env first.")
        return 1

    user_id = sys.argv[1] if len(sys.argv) > 1 else getattr(settings, "snaptrade_user_id", "")
    user_secret = (
        sys.argv[2] if len(sys.argv) > 2 else getattr(settings, "snaptrade_user_secret", "")
    )
    if not user_id or not user_secret:
        print("Need a registered SnapTrade user_id and user_secret (see module docstring).")
        return 1

    connector = SnapTradeConnector(
        client_id=settings.snaptrade_client_id,
        consumer_key=settings.snaptrade_consumer_key,
        user_id=user_id,
        user_secret=user_secret,
        base_url=BASE_URL,
    )

    accounts = connector.list_accounts()
    print(f"Connected brokerage accounts: {len(accounts)}")
    for a in accounts:
        print(f"  {a['account_ref']}  {a['institution_name']}  {a['name']}  paper={a['is_paper']}")
    if not accounts:
        print("No accounts connected yet; open the Connection Portal link and connect one.")
        return 1

    connector.account_ref = accounts[0]["account_ref"]
    print(f"\nUsing account: {connector.account_ref} ({accounts[0]['institution_name']})")

    evidence = connector.prove_read_only()
    print(f"Read-only proof: method={evidence.method.value} read_only={evidence.read_only}")
    print(f"  details: {evidence.details}")

    balances = connector.fetch_balances()
    print(f"\nBalances (as of {balances.as_of}):")
    for a in balances.assets:
        print(f"  {format_atoms(a.atoms, a.decimals)} {a.asset}")

    fills_page = connector.fetch_asset_fills_page()
    print(f"\nFirst page of asset fills: {len(fills_page.items)}")
    for f in fills_page.items[:5]:
        print(f"  {f.filled_at}  {f.action}  {f.instrument.symbol}  qty={f.qty_atoms} @ {f.price}")

    events_page = connector.fetch_events_page()
    print(f"\nFirst page of events: {len(events_page.items)}")
    for e in events_page.items[:5]:
        print(f"  {e.occurred_at}  {e.kind}  {e.description}")

    print(
        "\nDone. All payload mappings are documented-example verified only; "
        "see tests/fixtures/snaptrade/README.md for provenance and re-record "
        "these fixtures from this real account before launch."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
