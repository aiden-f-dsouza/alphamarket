"""One-time smoke test for the IBKR Flex connector, once a founder account and
Flex Query exist (implementation_plan.md section 5).

Prereqs:
  1. IBKR Client Portal -> Reports -> Flex Queries -> New Activity Flex Query.
     Include: Trades (Level of Detail = Execution), Cash Transactions,
     Corporate Actions, Open Positions, NAV (EquitySummaryInBase).
  2. Reports -> Flex Web Service -> generate a token.
  3. Copy .env.example to .env, set IBKR_FLEX_QUERY_ID and IBKR_FLEX_TOKEN.

Run: uv run python scripts/ibkr_smoke.py

Prints prove_read_only evidence, balances/positions and the first page of
fills and events. Never prints the token (invariant #4).
"""

import sys

from alphamarket.config import get_settings
from alphamarket.connectors.ibkr import IbkrConnector
from alphamarket.money import format_atoms, format_micros


def main() -> int:
    settings = get_settings()
    if not settings.ibkr_flex_query_id or not settings.ibkr_flex_token:
        print("IBKR_FLEX_QUERY_ID / IBKR_FLEX_TOKEN are empty. Fill in .env first.")
        return 1

    connector = IbkrConnector(query_id=settings.ibkr_flex_query_id, token=settings.ibkr_flex_token)

    print("Fetching a statement (this can take a few seconds while IBKR renders it)...")

    evidence = connector.prove_read_only()
    print(f"Read-only proof: method={evidence.method} read_only={evidence.read_only}")
    print(f"  details: {evidence.details}")  # never secrets (invariant #4)

    balances = connector.fetch_balances()
    print(f"\nBalances as of {balances.as_of}:")
    for amount in balances.assets:
        line = f"  {amount.asset}: {format_atoms(amount.atoms, amount.decimals)}"
        if amount.entry_price:
            line += f" (entry {amount.entry_price}, mark {amount.mark_price})"
        print(line)
    if balances.equity_usd_micros is not None:
        print(f"  Total equity: ${format_micros(balances.equity_usd_micros)}")

    fills_page = connector.fetch_asset_fills_page()
    print(f"\nFills in this page: {len(fills_page.items)}")
    for fill in fills_page.items[-5:]:
        print(
            f"  {fill.filled_at}  {fill.instrument.symbol}  {fill.action} "
            f"{format_atoms(fill.qty_atoms, fill.instrument.qty_decimals)} @ {fill.price}"
        )
    print(f"  resume_cursor for next sync: {fills_page.resume_cursor}")

    events_page = connector.fetch_events_page()
    print(f"\nEvents in this page: {len(events_page.items)}")
    for event in events_page.items[-5:]:
        print(f"  {event.occurred_at}  {event.kind}  {event.description or ''}")

    print(
        "\nFlex Web Service works. See docs/venues/ibkr.md for known gaps"
        " (timezone assumption, non-USD legs, unmapped cash transaction types)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
