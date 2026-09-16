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


def drawdown_window(values: list[float]) -> tuple[int, int]:
    """Indices (peak, trough) of the worst peak-to-trough drawdown in `values`,
    so a page can shade the period on the equity curve. Ties keep the earliest
    window; a series that never falls returns (0, 0)."""
    if not values:
        return (0, 0)
    peak_i, worst, win = 0, 0.0, (0, 0)
    for i, v in enumerate(values):
        if v > values[peak_i]:
            peak_i = i
        peak = values[peak_i]
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > worst:
            worst, win = dd, (peak_i, i)
    return win


def window_x(values: list[float], width: float) -> tuple[float, float]:
    """`drawdown_window` mapped onto the x axis of a `polyline` of the same
    series and width, for an SVG shading rect."""
    n = len(values)
    if n < 2:
        return (0.0, 0.0)
    a, b = drawdown_window(values)
    return (round(width * a / (n - 1), 1), round(width * b / (n - 1), 1))
