"""Exact fixed-point arithmetic for money and quantities.

Project invariant #1: monetary values are stored as INTEGER MICRO-DOLLARS
(millionths of one USD). Binary floats never touch a stored value.

Why micro-dollars and not cents: Kalshi reports decimal strings with sub-cent
precision -- `fee_cost: "0.139800"` is 13.98 cents, not 14. Rounding each fill
to whole cents injects error into the equity curve, which is precisely what M2
reconciliation exists to detect. A scale of 1e6 represents every field the
venue returns (4dp prices, 4dp balances, 6dp fees) exactly, with room to spare.

Contract quantities use the SAME 1e6 scale, because Kalshi quantities are now
fractional too (`count_fp: "101.89"`). That shared scale is what makes
`notional_micros` a clean divide.
"""

from decimal import Decimal

MICROS_PER_UNIT = 1_000_000
"""Micro-dollars per dollar (and micro-contracts per contract)."""


def to_micros(value: str | Decimal) -> int:
    """Parse a venue decimal string into exact integer micros.

    Accepts only `str` and `Decimal`. Floats are rejected because they cannot
    represent decimal fractions exactly, and bare ints are rejected because
    "5" is ambiguous between 5 dollars and 5 micros -- ambiguity is how
    scaling bugs get in.

    Raises ValueError if the value carries precision finer than 1e-6, rather
    than silently rounding it away. If that ever fires in production, the
    venue changed its precision and the scale needs revisiting -- which is
    information we want loudly, not quietly.
    """
    if isinstance(value, float | int):  # bool is an int subclass, so covered
        raise TypeError(
            f"to_micros() takes str or Decimal, got {type(value).__name__}. "
            "Floats cannot represent decimals exactly; ints are ambiguous about scale."
        )
    scaled = Decimal(value) * MICROS_PER_UNIT
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{value!r} has precision finer than 1e-6 and cannot be stored exactly "
            "in micros; the fixed-point scale needs revisiting."
        )
    return int(scaled)


def from_micros(micros: int) -> Decimal:
    """Exact Decimal in whole units. For display and assertions, not storage."""
    return Decimal(micros) / MICROS_PER_UNIT


def format_micros(micros: int, places: int = 4) -> str:
    """Human-readable unit string, e.g. 1_101_898_000 -> '1101.8980'."""
    return f"{from_micros(micros):.{places}f}"


def notional_micros(price_micros: int, count_micros: int) -> int:
    """Value of `count` contracts at `price`, in micros.

    Both inputs carry the 1e6 scale, so their product carries 1e12 and one
    divide brings it back. Raises if that divide is not exact.
    """
    product = price_micros * count_micros
    value, remainder = divmod(product, MICROS_PER_UNIT)
    if remainder:
        raise ValueError(
            f"notional of price={price_micros} x count={count_micros} is not "
            "representable in micros without loss"
        )
    return value
