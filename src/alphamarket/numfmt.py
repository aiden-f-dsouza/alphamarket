"""Display formatting for quantities and prices (w3-tail-ui). Pure.

Holdings, trade sizes and prices reach the templates as the venue's decimal
strings (`money.format_atoms`, the price column) or as display floats. These
helpers only change how they read: thousands separators, a typographic
minus, and a precision that keeps what matters. A whole number of shares or
dollars stays whole; a coin quantity keeps its significant decimals instead
of being rounded to zero. Stored values are never touched (invariant 1).
"""

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

MINUS = "−"
MAX_PLACES_LARGE = 4  # |x| >= 1: up to 4 decimals, trailing zeros dropped
SIG_DIGITS_SMALL = 6  # |x| < 1: six significant digits
MAX_PLACES = 12  # asset decimals never exceed 12 (invariant 1)


def _parse(value) -> tuple[Decimal | None, bool]:
    """(the number, whether the input carried an explicit "+")."""
    if value is None or isinstance(value, bool):
        return None, False
    if isinstance(value, Decimal):
        return value, False
    if isinstance(value, int | float):
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value), False
    text = str(value).strip().replace(",", "").replace(MINUS, "-")
    if not text:
        return None, False
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None, False
    return (number if number.is_finite() else None), text.startswith("+")


def _places(magnitude: Decimal) -> int:
    if magnitude == 0 or magnitude >= 1:
        return MAX_PLACES_LARGE
    # 0.000123456789 -> adjusted() is -4 -> 4 + 6 - 1 = 9 places -> 0.000123457
    return min(MAX_PLACES, -magnitude.adjusted() + SIG_DIGITS_SMALL - 1)


def fmt_num(value, places: int | None = None) -> str:
    """A quantity or price for display. `places` fixes the decimals (cash: 2);
    without it, |x| >= 1 keeps up to 4 decimals and anything smaller keeps 6
    significant digits, trailing zeros dropped either way. Text that is not
    a number comes back unchanged; None reads as an em dash."""
    number, plus = _parse(value)
    if number is None:
        return "—" if value is None or str(value).strip() == "" else str(value)
    magnitude = abs(number)
    fixed = places is not None
    digits = places if fixed else _places(magnitude)
    rounded = magnitude.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN)
    text = f"{rounded:,.{digits}f}"
    if not fixed and "." in text:
        text = text.rstrip("0").rstrip(".")
    if rounded == 0:
        return text
    if number < 0:
        return MINUS + text
    return ("+" if plus else "") + text
