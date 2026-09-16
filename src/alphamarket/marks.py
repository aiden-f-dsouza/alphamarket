"""Identity marks for strategies and tags: a deterministic sigil per slug and a
category colour per tag. Pure functions of the string, so the same strategy
shows the same mark on every page with nothing uploaded, stored or moderated.
Display only."""

import html

from markupsafe import Markup

SIGIL_COLORS = ["#2f7fb0", "#8a4fb5", "#1b7a47", "#c2611f", "#b58a1b", "#b6367a", "#3b7fb8"]
TAG_COLORS = {
    "weather": "#2f7fb0",
    "climate": "#2f7fb0",
    "politics": "#8a4fb5",
    "elections": "#8a4fb5",
    "macro": "#1b7a47",
    "econ": "#1b7a47",
    "economics": "#1b7a47",
    "rates": "#1b7a47",
    "finance": "#1f6f8f",
    "sports": "#c2611f",
    "crypto": "#b58a1b",
    "btc": "#b58a1b",
    "culture": "#b6367a",
    "entertainment": "#b6367a",
}


def fnv1a(s: str) -> int:
    h = 0x811C9DC5
    for ch in s.encode():
        h = ((h ^ ch) * 0x01000193) & 0xFFFFFFFF
    return h


def tag_color(tag: str) -> str:
    """Known market families get a fixed colour; anything else a stable one."""
    key = tag.strip().lower()
    if key in TAG_COLORS:
        return TAG_COLORS[key]
    return SIGIL_COLORS[fnv1a(key) % len(SIGIL_COLORS)]


def sigil(seed: str, size: int = 40) -> Markup:
    """A 5x5 vertically symmetric mark on a tinted square, as inline SVG. The
    15 free cells come from the hash bits, so ~32k distinct marks."""
    h = fnv1a(seed)
    color = SIGIL_COLORS[h % len(SIGIL_COLORS)]
    bits = h >> 3
    cells = []
    for y in range(5):
        for x in range(3):
            if (bits >> (y * 3 + x)) & 1:
                cells.append((x, y))
                if x != 2:
                    cells.append((4 - x, y))
    if not cells:  # never render a blank square
        cells = [(2, 2)]
    rects = "".join(
        f'<rect x="{10 + x * 12}" y="{10 + y * 12}" width="12" height="12"/>' for x, y in cells
    )
    return Markup(
        f'<svg class="sigil" width="{size}" height="{size}" viewBox="0 0 80 80" role="img" '
        f'aria-label="Mark for {html.escape(seed)}"><rect width="80" height="80" rx="4" '
        f'fill="{color}" fill-opacity="0.14"/><g fill="{color}">{rects}</g></svg>'
    )
