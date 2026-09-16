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
