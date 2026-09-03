"""Server-rendered chart geometry for the HTML pages.

Pure functions: a list of values in, SVG `points` strings out. Rendering the
equity and drawdown curves as inline SVG keeps the public page free of
third-party scripts and makes the drawn curve a deterministic function of the
platform-computed series (invariant #5 extends to the picture, not just the
numbers). Display only: nothing here feeds back into storage.
"""


def polyline(
    values: list[float],
    width: float,
    height: float,
    lo: float | None = None,
    hi: float | None = None,
) -> str:
    """Map `values` onto a width×height box as SVG polyline points.

    x is spread evenly across the width; y maps [lo, hi] onto [height, 0] (SVG
    y grows downward). `lo`/`hi` default to the series' own extremes so the
    curve fills the box; pass them to pin a scale (e.g. hi=0 for drawdown). A
    flat series draws a horizontal line at mid-height. Coordinates are rounded
    to 0.1px so the markup stays small and stable across runs.
    """
    if not values:
        return ""
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = hi - lo
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = width * i / (n - 1) if n > 1 else width / 2
        y = height / 2 if span == 0 else height * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    """`n` evenly spaced tick values from `hi` down to `lo` (top-to-bottom, the
    order axis labels are laid out in)."""
    if n < 2:
        return [hi]
    step = (hi - lo) / (n - 1)
    return [hi - i * step for i in range(n)]
