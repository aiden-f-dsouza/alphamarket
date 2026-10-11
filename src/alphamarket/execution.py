"""Execution layer (M6): how a mirrored fill comes into existence.

Only PAPER execution exists. A live executor would place real orders in a
follower's venue account, which needs a licensed party in the loop (CTA
registration or a regulated partner; see regulatory_reference.md sections
2b and 5, and BUILD_PLAN "Hard non-goals"). It is refused structurally here,
not behind a flag, so no configuration mistake can turn it on. Invariant #4
(read-only venue credentials only) stays intact: nothing in this module ever
touches a credential.

Paper fills are modelled at the creator's own fill price, snapped to the
venue's price tick, plus the venue's fee formula at the venue's rate (see
mirror.paper_fee_micros and registry.PROFILES).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from alphamarket.connectors.base import VenueProfile
from alphamarket.models import Allocation, AllocationMode, Signal


class LiveMirroringUnavailable(RuntimeError):
    """Raised for any attempt to execute in live mode."""

    def __init__(self) -> None:
        super().__init__(
            "Live mirroring is not available: placing orders in a follower's account "
            "requires a licensed party in the loop. Paper mode only."
        )


@dataclass(frozen=True)
class ExecutedFill:
    count_micros: int
    price_micros: int
    fee_micros: int
    filled_at: datetime
    venue_order_id: str | None = None


class Executor(Protocol):
    mode: str

    def execute(
        self, allocation: Allocation, signal: Signal, count_micros: int, profile: VenueProfile
    ) -> ExecutedFill: ...


class PaperExecutor:
    """Fills instantly at the signal's price on the venue's tick grid; fee per
    the venue's formula and rate."""

    mode = AllocationMode.PAPER

    def execute(
        self, allocation: Allocation, signal: Signal, count_micros: int, profile: VenueProfile
    ) -> ExecutedFill:
        # Local import: mirror imports this module.
        from alphamarket.mirror import paper_fee_micros, round_to_tick

        price = round_to_tick(signal.price_micros, profile.tick_micros)
        return ExecutedFill(
            count_micros=count_micros,
            price_micros=price,
            fee_micros=paper_fee_micros(
                count_micros, price, profile.fee_rate_bps, profile.fee_rounding_micros
            ),
            filled_at=signal.filled_at,
        )


def get_executor(mode: str) -> Executor:
    if AllocationMode(mode) is AllocationMode.PAPER:
        return PaperExecutor()
    raise LiveMirroringUnavailable()


# ---- wave2:p6-engine ----
# Paper copy v2 for ASSET strategies (paper.py). Same structural refusal as
# get_executor: there is no live asset executor, and asking for one raises.
# The paper executor fills at the price paper.paper_price chose (the quote
# at observed_at, else the creator's price with the stated penalty), at the
# instant Koine observed the signal, with the fee of the venue's paper fee
# model (methodology "Paper copying v2: sizing, fill price, fees").


@dataclass(frozen=True)
class ExecutedAssetFill:
    qty_atoms: int  # unsigned, at qty_decimals
    qty_decimals: int
    price: str  # decimal string, quote asset per unit
    price_basis: str  # "ask" | "bid" | "creator_price_penalty"
    notional_micros: int  # qty x price: rounded UP for buys, DOWN for sells
    fee_micros: int
    filled_at: datetime
    penalty_bps: int | None = None
    quote_at: datetime | None = None
    venue_order_id: str | None = None


class AssetPaperExecutor:
    """Fills instantly at the chosen paper price when Koine observed the
    signal (decision E5: a follower cannot trade before Koine saw the fill)."""

    mode = AllocationMode.PAPER

    def quote(self, signal: Signal, action, profile: VenueProfile):
        """The price this executor will fill `action` at (paper.PaperPrice)."""
        from alphamarket.paper import paper_price

        return paper_price(signal, action, profile)

    def execute(
        self,
        allocation: Allocation,
        signal: Signal,
        instrument,
        action,
        qty_atoms: int,
        price,
        profile: VenueProfile,
    ) -> ExecutedAssetFill:
        from alphamarket.jobs import aware

        return self.execute_at(
            instrument,
            action,
            qty_atoms,
            instrument.qty_decimals,
            price,
            profile,
            aware(signal.observed_at) or aware(signal.filled_at),
        )

    def execute_at(
        self,
        instrument,
        action,
        qty_atoms: int,
        decimals: int,
        price,
        profile: VenueProfile,
        filled_at: datetime,
    ) -> ExecutedAssetFill:
        """Fill `qty_atoms` (at `decimals`) at `price` (paper.PaperPrice) at
        `filled_at`: a copied signal (execute) or a stop close
        (paper_lifecycle.close_stopped). Notional and fee: paper.fill_amounts."""
        from alphamarket.connectors.base import Action
        from alphamarket.paper import fill_amounts

        notional, fee = fill_amounts(profile, Action(action), qty_atoms, decimals, price.price)
        return ExecutedAssetFill(
            qty_atoms=qty_atoms,
            qty_decimals=decimals,
            price=price.price,
            price_basis=price.basis,
            notional_micros=notional,
            fee_micros=fee,
            filled_at=filled_at,
            penalty_bps=price.penalty_bps,
            quote_at=price.quote_at,
        )


def get_asset_executor(mode: str) -> AssetPaperExecutor:
    if AllocationMode(mode) is AllocationMode.PAPER:
        return AssetPaperExecutor()
    raise LiveMirroringUnavailable()
