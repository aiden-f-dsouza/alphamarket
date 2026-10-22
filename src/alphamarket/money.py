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

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

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


MAX_DECIMALS = 12
"""Finest quantity precision the ledger stores (a CHECK constraint enforces it).
Every exchange in scope reports at most 10 dp; BigInteger at 12 dp still
holds 9.2 million whole units, and nothing in scope needs 18-dp wei."""


def to_atoms(value: str | Decimal, decimals: int) -> int:
    """Parse a venue decimal string into an exact integer count of atoms,
    where one whole unit is 10**decimals atoms (BTC at 8 dp: "0.5" -> 50_000_000).

    Asset quantities use this instead of micros (invariant 1, amended by
    platform_plan D3): rounding 1e-7 BTC away would manufacture exactly the
    divergence reconciliation exists to detect. Same rules as to_micros:
    str/Decimal only, and precision finer than `decimals` raises instead of
    rounding.
    """
    if isinstance(value, float | int):
        raise TypeError(
            f"to_atoms() takes str or Decimal, got {type(value).__name__}. "
            "Floats cannot represent decimals exactly; ints are ambiguous about scale."
        )
    if not 0 <= decimals <= MAX_DECIMALS:
        raise ValueError(f"decimals must be within 0..{MAX_DECIMALS}, got {decimals}")
    scaled = Decimal(value).scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{value!r} has precision finer than {decimals} decimals and cannot be "
            "stored exactly; use the venue's own precision for this asset."
        )
    return int(scaled)


FIXED_ROUNDING_MODES = frozenset({ROUND_HALF_EVEN, ROUND_DOWN})
"""The only rounding modes fixed_to_atoms accepts (onchain_perps_plan S7)."""


def _fixed_int(raw: str, scale: int) -> int:
    if not isinstance(raw, str):
        raise TypeError(
            f"fixed-point values are exact integer strings, got {type(raw).__name__}; "
            "an int or float may already have lost digits."
        )
    text = raw.strip()
    digits = text[1:] if text[:1] in "+-" else text
    if not digits or not digits.isdigit() or not digits.isascii():
        raise ValueError(f"{raw!r} is not an integer string")
    if scale < 0:
        raise ValueError(f"scale must be >= 0, got {scale}")
    return int(text)


def fixed_to_atoms(raw: str, scale: int, decimals: int, *, rounding: str | None = None) -> int:
    """An exact integer string at 10**scale (GMX 30-decimal USD, Nado x18) as
    atoms at `decimals` (<= MAX_DECIMALS): value = int(raw) / 10**scale.

    Default (rounding=None): raises ValueError when the value is not exactly
    representable at `decimals`, like to_atoms. rounding=ROUND_HALF_EVEN or
    ROUND_DOWN (toward zero) quantizes instead; only the GMX connector passes
    it, at the sites its venue doc lists (a test pins that no other module
    does). Integer arithmetic throughout, so no Decimal context precision can
    ever round a 30-digit value silently.

    Examples: ("51033091798973216164946606156791229", 30, 6, HALF_EVEN)
    = 51,033.091798973216... -> 51_033_091_799; ("1500000000000000000", 18, 6)
    = 1.5 -> 1_500_000."""
    if not 0 <= decimals <= MAX_DECIMALS:
        raise ValueError(f"decimals must be within 0..{MAX_DECIMALS}, got {decimals}")
    if rounding is not None and rounding not in FIXED_ROUNDING_MODES:
        raise ValueError(f"rounding must be ROUND_HALF_EVEN or ROUND_DOWN, got {rounding!r}")
    n = _fixed_int(raw, scale)
    if decimals >= scale:
        return n * 10 ** (decimals - scale)
    divisor = 10 ** (scale - decimals)
    q, r = divmod(abs(n), divisor)
    if r:
        if rounding is None:
            raise ValueError(
                f"{raw!r} at scale {scale} has precision finer than {decimals} decimals "
                "and cannot be stored exactly"
            )
        if rounding == ROUND_HALF_EVEN and (2 * r > divisor or (2 * r == divisor and q % 2)):
            q += 1
    return -q if n < 0 else q


def fixed_to_decimal_str(raw: str, scale: int) -> str:
    """The exact decimal string of an integer string at 10**scale, for DISPLAY
    figures (leaderboard pnl), which then go through
    connectors.base.leaderboard_figure and nothing else. Trailing zeros are
    dropped; ("1500000000000000000", 18) -> "1.5", ("-5", 2) -> "-0.05"."""
    n = _fixed_int(raw, scale)
    if scale == 0:
        return str(n)
    sign = "-" if n < 0 else ""
    whole, frac = divmod(abs(n), 10**scale)
    frac_text = f"{frac:0{scale}d}".rstrip("0")
    return f"{sign}{whole}.{frac_text}" if frac_text else f"{sign}{whole}"


def from_atoms(atoms: int, decimals: int) -> Decimal:
    """Exact Decimal in whole units. For display and assertions, not storage."""
    return Decimal(atoms).scaleb(-decimals)


def format_atoms(atoms: int, decimals: int) -> str:
    """Fixed-point string with exactly `decimals` places, e.g. (50_000_000, 8) -> '0.50000000'."""
    return f"{from_atoms(atoms, decimals):.{decimals}f}"


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
