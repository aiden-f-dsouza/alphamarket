"""How a paper tail is priced, in words (w3-tail-ui).

One derivation of the fill-price and trading-cost text per venue, from the
venue profile (fee model, fee rate) and the methodology ("Paper tailing",
"Paper copying v2"), shared by the sample pages (`demo.py`) and the real
strategy and tail pages (`pages.py`). Display text only: the engine that
actually prices paper fills lives in paper.py / mirror.py.

Tailing is paper only (invariant 8) and Koine's pricing is flat: nothing here
describes a live account, a performance fee or a high-water mark.
"""

from alphamarket.connectors.base import PaperFeeModel
from alphamarket.connectors.registry import PROFILES

# Pricing decision: flat subscriptions for followers, flat royalties for
# creators. Never a share of returns (no performance fee, no high-water mark).
PRICING_TEXT = "Flat subscription, never a share of returns"

PRICING_ROWS = [
    ("Followers", "A flat monthly subscription. Never a share of returns."),
    ("Creators", "A flat royalty from Koine, the same whether your paper tail is up or down."),
    ("Performance fees", "None. Nobody is paid more because a strategy took more risk."),
    ("Today", "Billing has not launched. Nothing is charged."),
]

# The drawdown limits offered on the tail form, as basis points of the
# allocation's high (tails.AllocationIn accepts 100..9000).
DRAWDOWN_CHOICES_BPS = (500, 1000, 1500, 2000, 2500, 3000, 4000, 5000)
DEFAULT_DRAWDOWN_BPS = 1500


def default_drawdown_bps(max_drawdown: float | None) -> int:
    """The drawdown limit a tail form preselects for a strategy: the smallest
    choice strictly looser than the strategy's own worst drawdown (a fraction,
    0.183 -> 2000), so its record would not already have tripped the stop. No
    stats -> DEFAULT_DRAWDOWN_BPS; worse than every choice -> the loosest."""
    if max_drawdown is None:
        return DEFAULT_DRAWDOWN_BPS
    for bps in DRAWDOWN_CHOICES_BPS:
        if bps / 10_000 > max_drawdown:
            return bps
    return DRAWDOWN_CHOICES_BPS[-1]


def drawdown_note(max_drawdown: float | None, bps: int) -> str | None:
    """A warning for a limit at or inside the strategy's worst drawdown."""
    if max_drawdown is None or bps / 10_000 > max_drawdown:
        return None
    return (
        f"This strategy has already fallen {max_drawdown * 100:.1f}% from a high. "
        "A tighter limit is likely to stop the tail."
    )


def venue_label(venue: str) -> str:
    """Short venue name for page chrome ("Interactive Brokers", without the
    profile's "(Flex)" report-type suffix)."""
    return PROFILES[venue].display_name.replace(" (Flex)", "")


def paper_terms(venue: str) -> dict:
    """`fill` and `costs` (one line each, for the strategy page's tail card)
    and `rows` (the tail page's "How each paper fill is priced" table)."""
    profile = PROFILES[venue]
    name = venue_label(venue)
    rate = f"{profile.fee_rate_bps / 100:.2f}%"
    if profile.paper_fee_model is PaperFeeModel.BINARY_PXQ:
        return {
            "fill": "Creator's fill price, at the tick",
            "costs": f"{name} fee formula",
            "rows": [
                (
                    "Fill price",
                    f"The creator's own fill price, snapped to {name}'s tick. This assumes the "
                    "price was still there for you: the model's main optimism, stated on every "
                    "paper figure.",
                ),
                (
                    "Venue fee",
                    f"{name}'s published formula, {rate} × contracts × price × (1 − price), "
                    "charged on every paper fill.",
                ),
                (
                    "Settlement",
                    "Same result and time as the creator's position: $1 per winning contract.",
                ),
                ("Starts", "With the creator's next trade. Nothing earlier is replayed."),
            ],
        }
    if profile.paper_fee_model is PaperFeeModel.US_EQUITY_REGULATORY:
        costs = "No commission; FINRA activity fee on sales"
        fee_row = (
            "Fees",
            f"{name} charges no commission; every paper sale pays the FINRA trading "
            "activity fee. Dividends and splits are applied on their dates.",
        )
        stale = "3 days (end-of-day reports)" if venue in ("ibkr", "snaptrade") else "6 hours"
    else:
        costs = f"{name} taker fee, {rate}" + (", plus funding" if venue == "hyperliquid" else "")
        fee_row = (
            "Fees",
            f"{name}'s taker fee, {rate} of notional, on every paper fill"
            + (
                ". Perp funding is paid or received every hour, and a position past its "
                "maintenance margin is liquidated, as at the venue."
                if venue == "hyperliquid"
                else "."
            ),
        )
        stale = "6 hours"
    return {
        "fill": "Venue quote when Koine saw the trade",
        "costs": costs,
        "rows": [
            (
                "Fill price",
                f"{name}'s public quote at the moment Koine saw the creator's trade: the ask to "
                "buy, the bid to sell. Never the creator's price, never hindsight.",
            ),
            ("No quote?", "The creator's price, moved 0.5% against you."),
            fee_row,
            (
                "Late signals",
                f"A trade Koine saw more than {stale} after it happened is skipped, and the skip "
                "is counted on your portfolio.",
            ),
            ("Starts", "With the creator's next trade. Nothing earlier is replayed."),
        ],
    }


def tailing_card(venue: str, paper_tails: int) -> dict:
    """The strategy page's "Paper-tail this strategy" card, minus the
    viewer-specific state (pages.py adds that)."""
    terms = paper_terms(venue)
    return {
        "paper_tails": paper_tails,
        "min_usd": 100,
        "fill": terms["fill"],
        "costs": terms["costs"],
        "pricing": PRICING_TEXT,
    }
