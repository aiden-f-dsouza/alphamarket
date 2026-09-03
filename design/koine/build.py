"""Koine design pack: generates .dc.html artboards for the design canvas.

Run from this directory: python3 build.py
Every name, handle, market and number is SAMPLE data for the mockups.
"""

import random

# ---------------------------------------------------------------- tokens (Direction A: Prospectus)
BG = "#f6f4ef"
SURF = "#ffffff"
LINE = "#e4e0d8"
LINE2 = "#cfc9be"
INK = "#141518"
INK2 = "#4a4e56"
MUTE = "#7b8089"
GOOD = "#1b7a47"
BAD = "#b6363f"
WARN = "#9a6a10"
GOODBG = "#e8f2ec"
BADBG = "#f7e8e9"
WARNBG = "#f6efdf"
GRAY = "#b3b7be"  # de-emphasis series (benchmarks)

SANS = 'font-family: &quot;IBM Plex Sans&quot;, &quot;Helvetica Neue&quot;, Helvetica, Arial, sans-serif;'
MONO = 'font-family: &quot;IBM Plex Mono&quot;, Menlo, Consolas, monospace; font-variant-numeric: tabular-nums;'
SERIF = 'font-family: Newsreader, Georgia, &quot;Times New Roman&quot;, serif;'
LABEL = MONO + f" font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: {MUTE};"
R = "border-radius: 4px;"

FONTS = ('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500'
         '&amp;family=IBM+Plex+Sans:wght@400;500;600&amp;family=IBM+Plex+Mono:wght@400;500&amp;display=swap">')

HEAD = f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  {FONTS}
  <style>
    body {{ margin: 0; background: {BG}; color: {INK}; font-family: "IBM Plex Sans", "Helvetica Neue", Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; }}
    a {{ color: {INK}; text-decoration: none; }} a:hover {{ color: {GOOD}; }}
    * {{ box-sizing: border-box; }}
    input, textarea {{ font-family: inherit; }}
    input::placeholder, textarea::placeholder {{ color: {MUTE}; }}
  </style>
</helmet>
'''
TAIL = '''</x-dc>
</body>
</html>
'''


# ---------------------------------------------------------------- sample curves
def walk(seed, n, drift, vol, start=100.0):
    r = random.Random(seed)
    v = start
    out = []
    for _ in range(n):
        v *= 1 + r.gauss(drift, vol)
        out.append(v)
    return out


def drawdown(vals):
    peak = vals[0]
    out = []
    for v in vals:
        peak = max(peak, v)
        out.append((v - peak) / peak)
    return out


def scale(vals, w, h, lo=None, hi=None, pad=0):
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    rng = hi - lo or 1
    n = len(vals)
    return [(pad + i * (w - 2 * pad) / (n - 1), pad + (h - 2 * pad) * (1 - (v - lo) / rng)) for i, v in enumerate(vals)]


def pts_str(pts):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


STRAT = walk(7, 120, 0.0032, 0.020)          # featured strategy, +32%
SPX = walk(4, 120, 0.00055, 0.009)           # sample S&P
CASH = [100 * (1 + 0.000115) ** i for i in range(120)]  # ~1.4% over window
PORT = walk(19, 90, 0.0011, 0.007)           # investor portfolio
DD = drawdown(STRAT)
SPARKS = {
    "s1": walk(21, 60, 0.0028, 0.022), "s2": walk(13, 60, 0.0022, 0.015), "s3": walk(11, 60, 0.003, 0.018),
    "s4": walk(5, 60, 0.004, 0.035), "s5": walk(9, 60, -0.0005, 0.02), "s6": walk(3, 60, 0.0015, 0.012),
}


# ---------------------------------------------------------------- primitives
def icon(name, size=16, color="currentColor", sw=1.5):
    p = {
        "check": '<polyline points="3.5,8.5 6.5,11.5 12.5,4.5"></polyline>',
        "x": '<line x1="4" y1="4" x2="12" y2="12"></line><line x1="12" y1="4" x2="4" y2="12"></line>',
        "arrow": '<line x1="3" y1="8" x2="13" y2="8"></line><polyline points="9,4 13,8 9,12"></polyline>',
        "arrow-ne": '<line x1="4" y1="12" x2="12" y2="4"></line><polyline points="6,4 12,4 12,10"></polyline>',
        "lock": '<rect x="3" y="7" width="10" height="7" rx="1"></rect><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"></path>',
        "shield": '<path d="M8 1.5l5.5 2v4c0 3.2-2.3 5.6-5.5 7-3.2-1.4-5.5-3.8-5.5-7v-4z"></path><polyline points="5.5,8 7.2,9.7 10.5,6.5"></polyline>',
        "eye": '<path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8s-2.5 4.5-6.5 4.5S1.5 8 1.5 8z"></path><circle cx="8" cy="8" r="2"></circle>',
        "eye-off": '<path d="M2 2l12 12"></path><path d="M6.4 6.5A2 2 0 0 0 9.5 9.6"></path><path d="M3.6 5.3C2.4 6.4 1.5 8 1.5 8s2.5 4.5 6.5 4.5c1.1 0 2.1-.3 3-.8"></path><path d="M6.8 3.7C7.2 3.6 7.6 3.5 8 3.5c4 0 6.5 4.5 6.5 4.5s-.6 1.1-1.7 2.2"></path>',
        "sync": '<path d="M13 8a5 5 0 0 1-8.5 3.6"></path><path d="M3 8a5 5 0 0 1 8.5-3.6"></path><polyline points="11,1.5 11.5,4.5 8.5,5"></polyline><polyline points="5,14.5 4.5,11.5 7.5,11"></polyline>',
        "plus": '<line x1="8" y1="3" x2="8" y2="13"></line><line x1="3" y1="8" x2="13" y2="8"></line>',
        "user": '<circle cx="8" cy="5.5" r="3"></circle><path d="M2.5 14c.6-3 2.8-4.5 5.5-4.5s4.9 1.5 5.5 4.5"></path>',
        "users": '<circle cx="6" cy="5.5" r="2.5"></circle><path d="M1.5 13.5c.5-2.6 2.2-4 4.5-4s4 1.4 4.5 4"></path><path d="M10.5 3.2a2.5 2.5 0 0 1 0 4.6"></path><path d="M11.5 9.6c1.7.4 2.7 1.7 3 3.9"></path>',
        "chat": '<path d="M2 3.5h12v7H7l-3.5 3v-3H2z"></path>',
        "search": '<circle cx="7" cy="7" r="4.5"></circle><line x1="10.5" y1="10.5" x2="14" y2="14"></line>',
        "filter": '<path d="M2 3h12l-4.5 5.5V13l-3-1.5V8.5z"></path>',
        "link": '<path d="M6.5 9.5l3-3"></path><path d="M7.5 4.5l1-1a2.5 2.5 0 0 1 3.5 3.5l-1 1"></path><path d="M8.5 11.5l-1 1A2.5 2.5 0 0 1 4 9l1-1"></path>',
        "mail": '<rect x="2" y="3.5" width="12" height="9" rx="1"></rect><polyline points="2,4.5 8,9 14,4.5"></polyline>',
        "chev": '<polyline points="6,4 10,8 6,12"></polyline>',
        "chevd": '<polyline points="4,6 8,10 12,6"></polyline>',
        "doc": '<path d="M4 1.5h5.5L13 5v9.5H4z"></path><polyline points="9.5,1.5 9.5,5 13,5"></polyline><line x1="6" y1="8.5" x2="11" y2="8.5"></line><line x1="6" y1="11" x2="11" y2="11"></line>',
        "scale": '<line x1="8" y1="2" x2="8" y2="14"></line><path d="M3 5l-2 5a2.2 2.2 0 0 0 4 0z"></path><path d="M13 5l-2 5a2.2 2.2 0 0 0 4 0z"></path><line x1="3" y1="5" x2="13" y2="5"></line>',
        "minus": '<line x1="3" y1="8" x2="13" y2="8"></line>',
        "bell": '<path d="M4 11V7a4 4 0 0 1 8 0v4l1.5 1.5h-11z"></path><path d="M6.5 14a1.5 1.5 0 0 0 3 0"></path>',
    }[name]
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 16 16" fill="none" stroke="{color}" stroke-width="{sw}" '
            f'stroke-linecap="round" stroke-linejoin="round" style="flex-shrink: 0;">{p}</svg>')


def mark(size=20, color=INK):
    # Koine mark: a square seal with a check, the "verified stamp"
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 20 20" fill="none" style="flex-shrink: 0;">'
            f'<rect x="1" y="1" width="18" height="18" rx="4" stroke="{color}" stroke-width="1.5"></rect>'
            f'<polyline points="5.5,10.5 8.5,13.5 14.5,6.5" stroke="{color}" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"></polyline></svg>')


def brand(color=INK):
    return (f'<a href="#" style="display: flex; align-items: center; gap: 10px; color: {color};">{mark(20, color)}'
            f'<span style="{SERIF} font-size: 22px; font-weight: 500; letter-spacing: -0.01em; line-height: 1;">Koine</span></a>')


def audience_toggle(mode="investor"):
    def seg(t, on):
        return (f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.04em; padding: 7px 14px; {R} '
                f'color: {INK if on else MUTE}; background: {SURF if on else "transparent"}; '
                f'box-shadow: {"0 1px 2px rgba(20,21,24,0.08)" if on else "none"};">{t}</a>')
    return (f'<div style="display: flex; align-items: center; gap: 2px; padding: 3px; background: #ecE9e2; {R}">'
            f'{seg("For investors", mode == "investor")}{seg("For creators", mode == "creator")}</div>')


def nav(active="", logged_in=False, mode="investor"):
    links = [("Marketplace", "market"), ("Feed", "feed"), ("Verification", "verify"), ("Transparency", "transparency"), ("Research", "research")]
    items = "".join(
        f'<a href="#" style="font-size: 14px; font-weight: 500; color: {INK if k == active else INK2}; padding: 6px 0; '
        f'border-bottom: 1.5px solid {INK if k == active else "transparent"};">{t}</a>' for t, k in links)
    if logged_in:
        right = (f'<a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 14px; color: {INK2};">{icon("bell", 16, INK2)}</a>'
                 f'<a href="#" style="font-size: 14px; font-weight: 500; color: {INK2};">Portfolio</a>'
                 f'<a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 500; color: {INK};">'
                 f'<span style="width: 28px; height: 28px; border-radius: 50%; background: #d9d4c9; display: inline-flex; align-items: center; justify-content: center; {MONO} font-size: 11px;">AD</span></a>')
    else:
        right = (f'<a href="#" style="font-size: 14px; font-weight: 500; color: {INK2}; padding: 9px 4px;">Log in</a>'
                 f'{btn("Get started", size=36)}')
    return (f'<div style="display: flex; align-items: center; justify-content: space-between; height: 64px; padding: 0 48px; '
            f'border-bottom: 1px solid {LINE}; background: {BG};">'
            f'<div style="display: flex; align-items: center; gap: 28px;">{brand()}{audience_toggle(mode)}</div>'
            f'<div style="display: flex; align-items: center; gap: 28px;">{items}</div>'
            f'<div style="display: flex; align-items: center; gap: 18px;">{right}</div></div>')


def footer():
    col = lambda t, ls: (f'<div style="display: flex; flex-direction: column; gap: 10px;"><span style="{LABEL}">{t}</span>'
                         + "".join(f'<a href="#" style="font-size: 14px; color: {INK2};">{x}</a>' for x in ls) + "</div>")
    return (f'<div style="display: grid; grid-template-columns: 1.4fr repeat(4, minmax(0, 1fr)); gap: 48px; padding: 48px 120px 40px; border-top: 1px solid {LINE};">'
            f'<div style="display: flex; flex-direction: column; gap: 14px;">{brand()}'
            f'<p style="margin: 0; font-size: 14px; line-height: 1.5; color: {INK2}; max-width: 300px; text-wrap: pretty;">A verified marketplace for trading strategies. Records are computed from venue data, never typed in.</p>'
            f'<span style="{MONO} font-size: 12px; color: {MUTE};">Koine never holds investor funds.</span></div>'
            f'{col("Product", ["Marketplace", "Feed", "Portfolio", "For creators"])}'
            f'{col("Trust", ["Verification", "Transparency", "Security &amp; privacy", "Fees"])}'
            f'{col("Learn", ["Research", "How to read a track record", "Risk glossary"])}'
            f'{col("Company", ["About", "Invite", "Terms", "Contact"])}</div>')


def btn(text, kind="primary", size=44, icon_html=""):
    if kind == "primary":
        s = f"color: {SURF}; background: {INK}; border: 1px solid {INK};"
    elif kind == "good":
        s = f"color: {SURF}; background: {GOOD}; border: 1px solid {GOOD};"
    elif kind == "danger":
        s = f"color: {BAD}; background: transparent; border: 1px solid {BAD};"
    elif kind == "ghost":
        s = f"color: {INK}; background: transparent; border: 1px solid transparent;"
    else:
        s = f"color: {INK}; background: {SURF}; border: 1px solid {LINE2};"
    pad = 20 if size >= 44 else 14
    fs = 14 if size >= 44 else 13
    return (f'<a href="#" style="display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: {size}px; padding: 0 {pad}px; '
            f'font-size: {fs}px; font-weight: 500; white-space: nowrap; {R} {s}">{icon_html}{text}</a>')


def badge(text, tone="good", ic=None):
    col = {"good": GOOD, "warn": WARN, "bad": BAD, "neutral": INK2}[tone]
    bg = {"good": GOODBG, "warn": WARNBG, "bad": BADBG, "neutral": "#ecE9e2"}[tone]
    i = icon(ic, 12, col, 2) if ic else ""
    return (f'<span style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; '
            f'color: {col}; background: {bg}; padding: 5px 9px; {R}">{i}{text}</span>')


def verified(text="Verified", days=None):
    t = text + (f" · {days} days" if days else "")
    return badge(t, "good", "shield")


def tag(text):
    return f'<span style="{MONO} font-size: 11px; letter-spacing: 0.04em; color: {INK2}; border: 1px solid {LINE2}; padding: 4px 8px; {R}">{text}</span>'


def num(v, unit="%", plus=True, dec=1):
    s = f"{abs(v):.{dec}f}{unit}"
    if v < 0:
        return "−" + s
    return ("+" if plus else "") + s


def sharpe(v):
    return num(v, '', plus=False, dec=2)


def numcol(v):
    return GOOD if v > 0 else (BAD if v < 0 else INK)


def stat(label, value, tone="", sub="", size=24, border=True):
    col = {"good": GOOD, "bad": BAD, "": INK}[tone]
    b = f"border-right: 1px solid {LINE};" if border else ""
    return (f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 18px 20px; {b}">'
            f'<span style="{LABEL}">{label}</span>'
            f'<span style="{MONO} font-size: {size}px; font-weight: 500; color: {col}; line-height: 1;">{value}'
            f'<span style="font-size: 13px; color: {MUTE}; margin-left: 5px; font-weight: 400;">{sub}</span></span></div>')


def h1(text, size=56):
    return f'<h1 style="margin: 0; {SERIF} font-size: {size}px; font-weight: 500; letter-spacing: -0.015em; line-height: 1.05; text-wrap: balance;">{text}</h1>'


def h2(text, size=36):
    return f'<h2 style="margin: 0; {SERIF} font-size: {size}px; font-weight: 500; letter-spacing: -0.01em; line-height: 1.1; text-wrap: balance;">{text}</h2>'


def h3(text, size=18):
    return f'<h3 style="margin: 0; font-size: {size}px; font-weight: 600; letter-spacing: -0.01em; line-height: 1.25;">{text}</h3>'


def p(text, size=16, color=INK2, mw=None):
    m = f"max-width: {mw}px;" if mw else ""
    return f'<p style="margin: 0; font-size: {size}px; line-height: 1.55; color: {color}; text-wrap: pretty; {m}">{text}</p>'


def card(inner, pad="24px 28px", bg=SURF, extra=""):
    return f'<div style="display: flex; flex-direction: column; background: {bg}; border: 1px solid {LINE2}; {R} padding: {pad}; {extra}">{inner}</div>'


def input_(label, placeholder="", value="", mono=False, textarea=False, note="", prefix=""):
    f = MONO + " font-size: 13px;" if mono else "font-size: 15px;"
    base = f'width: 100%; background: {SURF}; color: {INK}; border: 1px solid {LINE2}; {R} padding: 12px 14px; {f}'
    if textarea:
        ctl = f'<textarea style="{base} min-height: 120px; resize: vertical;" placeholder="{placeholder}">{value}</textarea>'
    else:
        ctl = f'<input type="text" style="{base} height: 44px;" placeholder="{placeholder}" value="{value}">'
    n = f'<span style="font-size: 13px; color: {MUTE};">{note}</span>' if note else ""
    return f'<label style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">{label}</span>{ctl}{n}</label>'


def toggle(on=True):
    return (f'<span style="display: inline-flex; align-items: center; width: 36px; height: 20px; padding: 2px; background: {GOOD if on else LINE2}; border-radius: 10px;">'
            f'<span style="width: 16px; height: 16px; background: {SURF}; border-radius: 50%; margin-left: {"auto" if on else "0"};"></span></span>')


# ---------------------------------------------------------------- charts (static mockups: inline SVG)
def grid_lines(w, h, n=4):
    return "".join(f'<line x1="0" y1="{h * i / n:.1f}" x2="{w}" y2="{h * i / n:.1f}" stroke="{LINE}" stroke-width="1"></line>' for i in range(n + 1))


def line_chart(series, w=720, h=240, gridy=4, end_dot=True):
    """series: list of (values, color, width, dash). All share one y-scale (indexed to 100)."""
    lo = min(min(v) for v, *_ in series)
    hi = max(max(v) for v, *_ in series)
    out = [f'<svg width="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display: block; overflow: visible;">{grid_lines(w, h, gridy)}']
    for vals, color, sw, dash in series:
        pts = scale(vals, w, h, lo, hi)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(f'<polyline points="{pts_str(pts)}" fill="none" stroke="{color}" stroke-width="{sw}" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"{d}></polyline>')
    if end_dot:
        vals, color, *_ = series[0]
        x, y = scale(vals, w, h, lo, hi)[-1]
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}" stroke="{SURF}" stroke-width="2"></circle>')
    out.append("</svg>")
    return "".join(out), lo, hi


def dd_chart(dd, w=720, h=110):
    pts = scale(dd, w, h, min(dd), 0)
    s = pts_str(pts)
    return (f'<svg width="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display: block;">'
            f'<line x1="0" y1="0.5" x2="{w}" y2="0.5" stroke="{LINE2}" stroke-width="1"></line>'
            f'<line x1="0" y1="{h - 0.5}" x2="{w}" y2="{h - 0.5}" stroke="{LINE}" stroke-width="1"></line>'
            f'<polygon points="0,0 {s} {w},0" fill="{BAD}" fill-opacity="0.10"></polygon>'
            f'<polyline points="{s}" fill="none" stroke="{BAD}" stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"></polyline></svg>')


def spark(vals, w=112, h=30):
    good = vals[-1] >= vals[0]
    pts = scale(vals, w, h, pad=2)
    col = GOOD if good else BAD
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="display: block;">'
            f'<polyline points="{pts_str(pts)}" fill="none" stroke="{col}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"></polyline></svg>')


def y_axis(lo, hi, n=4, fmt=lambda v: f"{v:.0f}"):
    vals = [hi - (hi - lo) * i / n for i in range(n + 1)]
    return (f'<div style="display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end;">'
            + "".join(f'<span style="{MONO} font-size: 11px; color: {MUTE}; line-height: 1;">{fmt(v)}</span>' for v in vals) + "</div>")


def x_axis(labels):
    return ('<div style="display: flex; justify-content: space-between;">'
            + "".join(f'<span style="{MONO} font-size: 11px; color: {MUTE};">{v}</span>' for v in labels) + "</div>")


def legend(items):
    return ('<div style="display: flex; align-items: center; gap: 18px;">'
            + "".join(f'<span style="display: inline-flex; align-items: center; gap: 7px; font-size: 13px; color: {INK2};">'
                      f'<span style="width: 14px; height: 2px; background: {c}; {"border-top: 2px dashed " + c + "; background: transparent; height: 0;" if d else ""}"></span>{t}</span>' for t, c, d in items)
            + "</div>")


def bar(pct, color=INK, h=8, track=LINE):
    return (f'<span style="display: block; width: 100%; height: {h}px; background: {track}; border-radius: {h // 2}px; overflow: hidden;">'
            f'<span style="display: block; width: {pct}%; height: 100%; background: {color}; border-radius: {h // 2}px;"></span></span>')


# ---------------------------------------------------------------- shared blocks
def strategy_card(w=540):
    chart, lo, hi = line_chart([(STRAT, INK, 2, None), (SPX, GRAY, 1.5, None)], 640, 200)
    return (f'<div style="display: flex; flex-direction: column; background: {SURF}; border: 1px solid {LINE2}; {R} width: {w}px; box-shadow: 0 24px 60px -30px rgba(20,21,24,0.25);">'
            f'<div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 20px 24px 16px; border-bottom: 1px solid {LINE};">'
            f'<div style="display: flex; flex-direction: column; gap: 6px;">{verified("Verified from venue data", 118)}'
            f'<span style="font-size: 18px; font-weight: 600; letter-spacing: -0.01em; margin-top: 4px;">Weather &amp; CPI mean reversion</span>'
            f'<span style="display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: {INK2};">'
            f'<span style="width: 20px; height: 20px; border-radius: 50%; background: #d9d4c9;"></span>quietedge · Kalshi · prediction markets</span></div>'
            f'<span style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 11px; color: {MUTE}; white-space: nowrap;">{icon("check", 12, GOOD, 2)}reconciled 02:14 UTC</span></div>'
            f'<div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-bottom: 1px solid {LINE};">'
            f'{stat("Return, net of fees", "+32.5%", "good", "118d")}{stat("Worst drawdown", "−14.3%", "bad", "31d to recover")}{stat("Sharpe", "1.84", "", "", border=False)}</div>'
            f'<div style="display: flex; flex-direction: column; gap: 10px; padding: 18px 24px 16px;">'
            f'<div style="display: flex; align-items: center; justify-content: space-between;"><span style="{LABEL}">Growth of $100</span>{legend([("Strategy", INK, False), ("S&amp;P 500", GRAY, False)])}</div>{chart}</div>'
            f'<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); border-top: 1px solid {LINE};">'
            f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 14px 24px; border-right: 1px solid {LINE};"><span style="{LABEL}">Capacity</span>'
            f'<span style="{MONO} font-size: 13px; color: {INK};">$41.2k tailing <span style="color: {MUTE};">of ~$180k</span></span>{bar(23, INK, 4)}</div>'
            f'<div style="display: flex; align-items: center; justify-content: space-between; padding: 14px 24px;"><span style="{LABEL}">Risk score</span>'
            f'<span style="{MONO} font-size: 16px;">6<span style="color: {MUTE};">/10</span></span></div></div></div>')


def page(inner, h, active="", logged_in=False, mode="investor"):
    return HEAD + (f'<div style="width: 1440px; min-height: {h}px; background: {BG}; display: flex; flex-direction: column;">'
                   f'{nav(active, logged_in, mode)}{inner}<div style="flex-grow: 1;"></div>{footer()}</div>') + TAIL


# ================================================================= MAIN: landing (investor mode)
def landing():
    points = [
        ("shield", "Track records are verified", "Every number is computed by Koine from the venue's own fill and settlement data, pulled with a read-only key. Nothing on a strategy page was typed by its creator."),
        ("lock", "Your money stays in your account", "Tailing mirrors a strategy into your own brokerage account. Koine never holds, moves or can withdraw a dollar of it."),
        ("eye-off", "Strategies stay private", "Creators are verified on outcomes. They never reveal their model, code or thesis to Koine or to you."),
        ("scale", "Creators earn only when you profit", "A performance fee on gains above your high-water mark. No gains, no fee. No subscriptions, no signal groups."),
    ]
    pts = "".join(
        f'<div style="display: flex; flex-direction: column; gap: 14px; padding: 28px 28px 32px; border-right: 1px solid {LINE};">'
        f'<span style="display: inline-flex; width: 36px; height: 36px; align-items: center; justify-content: center; background: {BG}; {R}">{icon(i, 18, INK)}</span>'
        f'{h3(t)}{p(d, 14)}</div>' for i, t, d in points)
    steps = [
        ("01", "A creator connects a read-only key", "Koine checks the key's scope before storing it. A key that can place orders is refused. Fills and settlements stream into an append-only ledger."),
        ("02", "The ledger is reconciled every night", "We replay every trade and compare the computed balance against the venue's. If they disagree, the page is quarantined until they match."),
        ("03", "You tail with limits you set", "Pick an allocation and a maximum drawdown. Mirrored trades land in your own account. Breach the limit and tailing stops."),
    ]
    st = "".join(
        f'<div style="display: grid; grid-template-columns: 56px 1fr; gap: 20px; padding: 28px 0; border-top: 1px solid {LINE};">'
        f'<span style="{MONO} font-size: 13px; color: {MUTE}; padding-top: 4px;">{n}</span>'
        f'<div style="display: flex; flex-direction: column; gap: 8px;">{h3(t, 20)}{p(d, 15)}</div></div>' for n, t, d in steps)
    proves = ["Every trade happened at the venue, in that account.", "Returns are net of venue fees, to the sub-cent.", "The equity curve matched the venue balance last night.", "Drawdowns and losing months are shown at the same size as gains."]
    doesnt = ["That the creator will keep winning.", "Anything the creator wrote about the strategy.", "Activity older than the venue's history allows.", "Positions held on venues that are not connected."]
    li = lambda items, ok: "".join(
        f'<div style="display: flex; align-items: flex-start; gap: 12px; padding: 14px 0; border-bottom: 1px solid {LINE}; font-size: 15px; line-height: 1.45;">'
        f'{icon("check" if ok else "minus", 18, GOOD if ok else MUTE, 2)}<span style="color: {INK if ok else INK2};">{t}</span></div>' for t in items)
    rows = [("Fed path, both directions", "rateswatch", 40.7, -7.8, 2.1, 134), ("Election polling arbitrage", "median_voter", 29.8, -4.3, 2.62, 201), ("Weather &amp; CPI mean reversion", "quietedge", 32.5, -14.3, 1.84, 118)]
    rr = "".join(
        f'<div style="display: grid; grid-template-columns: 1fr 100px 100px 80px 90px; align-items: center; gap: 16px; padding: 14px 0; border-top: 1px solid {LINE};">'
        f'<div style="display: flex; flex-direction: column; gap: 3px;"><span style="font-size: 15px; font-weight: 500;">{n}</span><span style="{MONO} font-size: 12px; color: {MUTE};">{h}</span></div>'
        f'<span style="{MONO} font-size: 15px; text-align: right; color: {GOOD};">{num(r)}</span>'
        f'<span style="{MONO} font-size: 15px; text-align: right; color: {BAD};">{num(d)}</span>'
        f'<span style="{MONO} font-size: 15px; text-align: right;">{s:.2f}</span>'
        f'<span style="display: inline-flex; justify-content: flex-end;">{verified(str(days) + "d")}</span></div>' for n, h, r, d, s, days in rows)
    hdr = lambda t, right=True: f'<span style="{LABEL} text-align: {"right" if right else "left"};">{t}</span>'
    inner = f'''
<div style="display: grid; grid-template-columns: 600px 540px; justify-content: space-between; align-items: center; padding: 88px 120px 80px;">
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <span style="{LABEL} color: {GOOD};">Verified strategy marketplace · prediction markets first</span>
    {h1("Invest in traders who can prove it.", 64)}
    {p("Koine verifies trading track records straight from the venue, then lets you tail the ones you trust from your own account, with a drawdown limit you set. No screenshots, no signal groups, no custody.", 18, INK2, 520)}
    <div style="display: flex; align-items: center; gap: 20px; margin-top: 4px;">
      {btn("Browse verified strategies", size=48)}
      <a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 500; color: {INK2};">How verification works {icon("arrow", 14)}</a>
    </div>
    <div style="display: flex; align-items: center; gap: 22px; margin-top: 8px; {MONO} font-size: 12px; color: {MUTE};">
      <span style="display: inline-flex; align-items: center; gap: 6px;">{icon("lock", 13, MUTE)}Read-only verification keys</span>
      <span style="display: inline-flex; align-items: center; gap: 6px;">{icon("check", 13, MUTE, 2)}Reconciled nightly</span>
      <span style="display: inline-flex; align-items: center; gap: 6px;">{icon("x", 13, MUTE, 2)}Never holds funds</span>
    </div>
  </div>
  {strategy_card()}
</div>
<div style="display: flex; flex-direction: column; gap: 32px; padding: 0 120px;">
  <div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid {LINE2}; {R} background: {SURF}; overflow: hidden;">{pts}</div>
</div>
<div style="display: grid; grid-template-columns: 420px 1fr; gap: 96px; padding: 112px 120px 0;">
  <div style="display: flex; flex-direction: column; gap: 20px;">
    <span style="{LABEL}">How it works</span>
    {h2("Proof first. Then, and only then, capital.")}
    {p("Every strategy on Koine earns its place with a record we computed ourselves. Returns are never shown without the drawdown it took to get them.")}
    <div>{btn("Read the methodology", "secondary")}</div>
  </div>
  <div style="display: flex; flex-direction: column; border-bottom: 1px solid {LINE};">{st}</div>
</div>
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 64px; padding: 112px 120px 0; align-items: start;">
  <div style="display: flex; flex-direction: column; gap: 20px;">
    <span style="{LABEL}">Ranked by risk-adjusted return</span>
    {h2("What is verified today.", 32)}
    <div style="display: flex; flex-direction: column;">
      <div style="display: grid; grid-template-columns: 1fr 100px 100px 80px 90px; gap: 16px; padding: 0 0 10px;">{hdr("Strategy", False)}{hdr("Return")}{hdr("Worst DD")}{hdr("Sharpe")}{hdr("Record")}</div>{rr}
    </div>
    <a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 500;">Open the marketplace {icon("arrow", 14)}</a>
  </div>
  <div style="display: flex; flex-direction: column; gap: 20px;">
    <span style="{LABEL}">What a page proves</span>
    {h2("A narrow claim, made precisely.", 32)}
    <div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 32px;">
      <div style="display: flex; flex-direction: column;"><span style="{LABEL} padding-bottom: 8px; border-bottom: 1px solid {LINE2};">Verified</span>{li(proves, True)}</div>
      <div style="display: flex; flex-direction: column;"><span style="{LABEL} padding-bottom: 8px; border-bottom: 1px solid {LINE2};">Not verified</span>{li(doesnt, False)}</div>
    </div>
  </div>
</div>
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin: 112px 120px 96px;">
  <div style="display: flex; flex-direction: column; gap: 16px; padding: 40px; background: {INK}; color: {SURF}; {R}">
    <span style="{LABEL} color: #b9bcc3;">For investors</span>
    <span style="{SERIF} font-size: 30px; font-weight: 500; line-height: 1.15; letter-spacing: -0.01em;">Tail a verified strategy from your own account.</span>
    <span style="font-size: 15px; color: #b9bcc3; line-height: 1.5;">Set an allocation and a drawdown limit. Pay a fee only on profit above your high-water mark.</span>
    <div style="margin-top: 8px;"><a href="#" style="display: inline-flex; align-items: center; height: 44px; padding: 0 20px; font-size: 14px; font-weight: 500; color: {INK}; background: {SURF}; {R}">Browse strategies</a></div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 16px; padding: 40px; background: {SURF}; border: 1px solid {LINE2}; {R}">
    <span style="{LABEL}">For creators</span>
    <span style="{SERIF} font-size: 30px; font-weight: 500; line-height: 1.15; letter-spacing: -0.01em;">Get paid for outcomes. Keep your edge to yourself.</span>
    <span style="font-size: 15px; color: {INK2}; line-height: 1.5;">Connect a read-only key. We publish the record; you never reveal the strategy.</span>
    <div style="margin-top: 8px;">{btn("Verify a track record", "secondary")}</div>
  </div>
</div>
'''
    return page(inner, 2760)


# ================================================================= MARKETPLACE
def marketplace():
    rows = [("s1", "Fed path, both directions", "rateswatch", ["macro", "rates"], 40.7, -7.8, 2.10, 4, 134, 62, 240, 8.7),
            ("s2", "Election polling arbitrage", "median_voter", ["politics"], 29.8, -4.3, 2.62, 3, 201, 118, 320, 8.4),
            ("s3", "Weather &amp; CPI mean reversion", "quietedge", ["macro", "weather"], 32.5, -14.3, 1.84, 6, 118, 41, 180, 7.1),
            ("s4", "Long-tail sports underdogs", "tailrisk", ["sports"], 36.5, -15.5, 1.21, 8, 92, 12, 90, 5.9),
            ("s6", "Earnings-day binaries", "printday", ["finance"], 11.6, -19.0, 0.44, 9, 96, 4, 60, 3.2),
            ("s5", "Daily temperature spreads", "quietedge", ["weather"], -8.1, -17.0, -0.31, 7, 101, 0, 45, 1.8)]
    rr = ""
    for i, (k, name, h, tags, ret, dd, sh, risk, days, tailing, cap, score) in enumerate(rows):
        tg = "".join(tag(t) for t in tags)
        rr += (f'<div style="display: grid; grid-template-columns: 36px 1fr 112px 96px 96px 72px 64px 150px 88px; align-items: center; gap: 18px; padding: 16px 24px; border-bottom: 1px solid {LINE};">'
               f'<span style="{MONO} font-size: 13px; color: {MUTE};">{i + 1:02d}</span>'
               f'<div style="display: flex; flex-direction: column; gap: 6px;"><a href="#" style="font-size: 16px; font-weight: 500;">{name}</a>'
               f'<div style="display: flex; align-items: center; gap: 8px;"><span style="{MONO} font-size: 12px; color: {MUTE};">{h}</span>{tg}{verified(str(days) + "d")}</div></div>'
               f'{spark(SPARKS[k])}'
               f'<span style="{MONO} font-size: 16px; text-align: right; color: {numcol(ret)};">{num(ret)}</span>'
               f'<span style="{MONO} font-size: 14px; text-align: right; color: {BAD};">{num(dd)}</span>'
               f'<span style="{MONO} font-size: 14px; text-align: right;">{sharpe(sh)}</span>'
               f'<span style="{MONO} font-size: 14px; text-align: right;">{risk}<span style="color: {MUTE};">/10</span></span>'
               f'<div style="display: flex; flex-direction: column; gap: 5px;"><span style="{MONO} font-size: 12px; color: {INK2};">${tailing}k <span style="color: {MUTE};">of ~${cap}k</span></span>{bar(int(tailing / cap * 100), INK, 4)}</div>'
               f'<span style="{MONO} font-size: 16px; font-weight: 500; text-align: right;">{score:.1f}</span></div>')
    hdr = lambda t, r=False: f'<span style="{LABEL} text-align: {"right" if r else "left"};">{t}</span>'
    asset_tabs = "".join(
        f'<a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 500; padding: 10px 0; color: {INK if on else MUTE}; border-bottom: 1.5px solid {INK if on else "transparent"};">{t}'
        + (f'<span style="{MONO} font-size: 10px; letter-spacing: 0.06em; text-transform: uppercase; color: {MUTE}; border: 1px solid {LINE2}; padding: 2px 6px; {R}">{s}</span>' if s else "") + "</a>"
        for t, on, s in [("Prediction markets", True, ""), ("Stocks", False, "soon"), ("Crypto", False, "soon"), ("All", False, "")])
    def filt(label, val):
        return (f'<div style="display: flex; flex-direction: column; gap: 6px;"><span style="{LABEL}">{label}</span>'
                f'<span style="display: inline-flex; align-items: center; justify-content: space-between; gap: 10px; height: 40px; padding: 0 12px; background: {SURF}; border: 1px solid {LINE2}; {R} font-size: 14px;">{val}{icon("chevd", 14, MUTE)}</span></div>')
    filters = (filt("Track record", "90+ days") + filt("Return", "Any") + filt("Worst drawdown", "Under 20%") + filt("Risk score", "Any")
               + filt("Sharpe", "Any") + filt("Capacity left", "Any"))
    inner = f'''
<div style="display: flex; flex-direction: column; gap: 24px; padding: 48px 120px 64px;">
  <div style="display: flex; align-items: flex-end; justify-content: space-between;">
    <div style="display: flex; flex-direction: column; gap: 12px;">
      {h1("Marketplace", 44)}
      {p("Every row is computed from venue data and reconciled nightly. Ranked by the Koine score: risk-adjusted return, consistency, drawdown and length of verified record. Not by raw return.", 15, INK2, 720)}
    </div>
    <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 4px;"><span style="{MONO} font-size: 32px; font-weight: 500; line-height: 1;">6</span><span style="{LABEL}">verified strategies</span></div>
  </div>
  <div style="display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid {LINE};">
    <div style="display: flex; gap: 28px;">{asset_tabs}</div>
    <span style="display: inline-flex; align-items: center; gap: 8px; {MONO} font-size: 12px; color: {MUTE};">{icon("search", 14, MUTE)}Search strategies or creators</span>
  </div>
  <div style="display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)) auto; gap: 16px; align-items: end;">
    {filters}
    <div style="display: flex; flex-direction: column; gap: 6px;"><span style="{LABEL}">Sort</span>
      <span style="display: inline-flex; align-items: center; gap: 10px; height: 40px; padding: 0 12px; background: {INK}; color: {SURF}; {R} font-size: 14px;">Koine score{icon("chevd", 14, SURF)}</span></div>
  </div>
  <div style="display: flex; flex-direction: column; background: {SURF}; border: 1px solid {LINE2}; {R} overflow: hidden;">
    <div style="display: grid; grid-template-columns: 36px 1fr 112px 96px 96px 72px 64px 150px 88px; gap: 18px; padding: 12px 24px; border-bottom: 1px solid {LINE2}; background: {BG};">
      {hdr("#")}{hdr("Strategy")}{hdr("60 days")}{hdr("Return", True)}{hdr("Worst DD", True)}{hdr("Sharpe", True)}{hdr("Risk", True)}{hdr("Capacity")}{hdr("Score", True)}
    </div>
    {rr}
  </div>
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
    {card(f'<div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">How the score works</span>{p("Sharpe and Sortino carry most of the weight. Consistency (share of profitable months), worst drawdown and verified days adjust it. A strategy under 90 verified days is incubating and cannot rank above 5.0 regardless of return. Full formula on the Verification page.", 14)}</div>', "20px 24px", BG)}
    {card(f'<div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">Why losers are listed</span>{p("Strategies that are losing money stay on the marketplace with their record intact. Hiding them would make the winners look like the whole population. See platform-wide outcomes on the Transparency page.", 14)}</div>', "20px 24px", BG)}
  </div>
</div>
'''
    return page(inner, 1500, "market")


# ================================================================= STRATEGY (diligence page)
def strategy():
    chart, lo, hi = line_chart([(STRAT, INK, 2, None), (SPX, GRAY, 1.5, None), (CASH, GRAY, 1.5, "4 4")], 900, 260)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    cells = ["·", "·", "·", "·", "+5.2%", "+9.8%", "−3.1%", "+18.4%", "+0.6%", "·", "·", "·"]
    mh = "".join(f'<th style="text-align: right; padding: 8px 6px; {LABEL} font-weight: 500; border-bottom: 1px solid {LINE2};">{m}</th>' for m in months)
    tds = "".join(f'<td style="text-align: right; padding: 12px 6px; {MONO} font-size: 13px; color: {MUTE if c == "·" else (BAD if c.startswith("−") else GOOD)}; border-bottom: 1px solid {LINE};">{c}</td>' for c in cells)
    worst = [("Jul 3 to Jul 29", "−14.3%", "31 days", "Heat-wave contracts settled against the model four days running."),
             ("Jun 11 to Jun 16", "−6.1%", "9 days", ""), ("Aug 20 to Aug 23", "−4.7%", "6 days", "")]
    wr = "".join(
        f'<div style="display: grid; grid-template-columns: 150px 80px 90px 1fr; gap: 16px; align-items: baseline; padding: 12px 0; border-bottom: 1px solid {LINE};">'
        f'<span style="{MONO} font-size: 13px; color: {INK2};">{a}</span><span style="{MONO} font-size: 15px; color: {BAD};">{b}</span>'
        f'<span style="{MONO} font-size: 13px; color: {INK2};">{c}</span><span style="font-size: 13px; color: {MUTE};">{d}</span></div>' for a, b, c, d in worst)
    risk = [("Sharpe (annualised)", "1.84"), ("Sortino", "2.31"), ("Volatility, 30d", "11.2%"), ("Worst month", "−3.1%"), ("Profitable months", "4 of 5"),
            ("Win rate", "58%"), ("Avg win / avg loss", "1.6×"), ("Largest single loss", "−$212.40"), ("Series concentration", "22% in one series"), ("Settled trades", "214")]
    rk = "".join(f'<div style="display: flex; justify-content: space-between; align-items: baseline; padding: 10px 0; border-bottom: 1px solid {LINE};"><span style="font-size: 14px; color: {INK2};">{a}</span><span style="{MONO} font-size: 14px;">{b}</span></div>' for a, b in risk)
    ver = [("Read-only key, scope re-checked every sync", True), ("1,204 fills and 214 settlements from Kalshi", True), ("Balance reconciled last night: $8,746.80 = $8,746.80", True),
           ("No behavioural flags (martingale, stake spikes)", True), ("Creator's description below is not verified", False)]
    vr = "".join(f'<div style="display: flex; align-items: flex-start; gap: 10px; padding: 9px 0; font-size: 14px; line-height: 1.4; color: {INK2};">{icon("check" if ok else "minus", 16, GOOD if ok else WARN, 2)}<span>{t}</span></div>' for t, ok in ver)
    inner = f'''
<div style="display: flex; flex-direction: column; gap: 32px; padding: 40px 120px 64px;">
  <div style="display: flex; align-items: center; gap: 8px; {MONO} font-size: 12px; color: {MUTE};"><a href="#" style="color: {MUTE};">Marketplace</a>{icon("chev", 12, MUTE)}<span>Prediction markets</span>{icon("chev", 12, MUTE)}<span style="color: {INK2};">Weather &amp; CPI mean reversion</span></div>
  <div style="display: grid; grid-template-columns: 1fr 360px; gap: 48px; align-items: start;">
    <div style="display: flex; flex-direction: column; gap: 14px;">
      <div style="display: flex; align-items: center; gap: 10px;">{verified("Verified from venue data", 118)}{badge("Reconciled last night", "good", "check")}{badge("Incubation complete", "neutral")}</div>
      {h1("Weather &amp; CPI mean reversion", 48)}
      <div style="display: flex; align-items: center; gap: 12px;">
        <span style="width: 28px; height: 28px; border-radius: 50%; background: #d9d4c9;"></span>
        <a href="#" style="font-size: 15px; font-weight: 500;">quietedge</a><span style="font-size: 14px; color: {MUTE};">· Kalshi · 2 verified strategies · 340 followers</span>
        <span style="display: flex; gap: 6px; margin-left: 6px;">{tag("macro")}{tag("weather")}</span>
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 12px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: flex; justify-content: space-between; align-items: baseline;"><span style="{LABEL}">Tail this strategy</span><span style="{MONO} font-size: 12px; color: {MUTE};">min $500</span></div>
      <div style="display: flex; justify-content: space-between; font-size: 14px; color: {INK2};"><span>Performance fee</span><span style="{MONO} color: {INK};">20% of profit above HWM</span></div>
      <div style="display: flex; justify-content: space-between; font-size: 14px; color: {INK2};"><span>Capacity left</span><span style="{MONO} color: {INK};">~$139k</span></div>
      <div style="display: flex; justify-content: space-between; font-size: 14px; color: {INK2};"><span>Currently tailing</span><span style="{MONO} color: {INK};">$41.2k · 23 investors</span></div>
      {btn("Tail from my account", size=48)}
      <span style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: {MUTE};">{icon("lock", 12, MUTE)}Koine cannot withdraw from your account.</span>
    </div>
  </div>
  <div style="display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); background: {SURF}; border: 1px solid {LINE2}; {R} overflow: hidden;">
    {stat("Return, net of fees", "+32.5%", "good", "since May 6")}{stat("Worst drawdown", "−14.3%", "bad", "31d recovery")}{stat("Sharpe", "1.84")}{stat("Risk score", "6", "", "/10")}{stat("vs. S&amp;P 500", "+25.7 pts", "good", "same window")}{stat("Capacity", "$180k", "", "est.", border=False)}
  </div>
  <div style="display: grid; grid-template-columns: 1fr 340px; gap: 32px; align-items: start;">
    <div style="display: flex; flex-direction: column; gap: 24px;">
      <div style="display: flex; flex-direction: column; gap: 14px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <div style="display: flex; align-items: center; justify-content: space-between;">
          <div style="display: flex; align-items: center; gap: 16px;"><span style="{LABEL}">Growth of $100 · net of venue fees · open positions at cost</span></div>
          <div style="display: flex; gap: 2px;">{"".join(f'<span style="{MONO} font-size: 11px; letter-spacing: 0.06em; padding: 4px 8px; {R} color: {INK if on else MUTE}; background: {BG if on else "transparent"};">{t}</span>' for t, on in [("30D", False), ("90D", False), ("ALL", True)])}</div>
        </div>
        {legend([("This strategy", INK, False), ("S&amp;P 500 (sample benchmark)", GRAY, False), ("Cash, T-bill", GRAY, True)])}
        <div style="display: grid; grid-template-columns: 40px 1fr; gap: 12px;">{y_axis(lo, hi, 4, lambda v: f"${v:.0f}")}<div style="display: flex; flex-direction: column; gap: 8px;">{chart}{x_axis(["May 6", "Jun", "Jul", "Aug", "Sep 1"])}</div></div>
        <div style="display: grid; grid-template-columns: 40px 1fr; gap: 12px; margin-top: 8px; padding-top: 16px; border-top: 1px solid {LINE};">
          <div style="display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end;"><span style="{MONO} font-size: 11px; color: {MUTE};">0%</span><span style="{MONO} font-size: 11px; color: {MUTE};">−15%</span></div>
          <div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">Drawdown from peak</span>{dd_chart(DD, 900, 110)}</div>
        </div>
      </div>
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
        <div style="display: flex; flex-direction: column; gap: 12px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
          <span style="{LABEL}">Monthly returns · 2026</span>
          <div style="overflow-x: auto;"><table style="width: 100%; border-collapse: collapse;"><tr>{mh}</tr><tr>{tds}</tr></table></div>
        </div>
        <div style="display: flex; flex-direction: column; gap: 4px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
          <span style="{LABEL} padding-bottom: 6px;">Worst periods</span>{wr}
        </div>
      </div>
      <div style="display: flex; flex-direction: column; gap: 12px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <div style="display: flex; align-items: center; gap: 10px;"><span style="{LABEL}">Creator's description</span>{badge("Not verified", "warn")}</div>
        {p("Fade extreme prints on weather and CPI contracts in the last 48 hours before settlement, when retail flow pushes YES past what the underlying forecast supports. Flat sizing, never adding to losers. Capacity is limited by liquidity in the daily temperature series.", 15, INK)}
        <span style="{MONO} font-size: 11px; color: {MUTE};">Written by quietedge. Koine does not verify anything in this box.</span>
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 24px;">
      <div style="display: flex; flex-direction: column; gap: 4px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <span style="{LABEL} padding-bottom: 6px;">Risk</span>{rk}
      </div>
      <div style="display: flex; flex-direction: column; gap: 6px; padding: 20px 24px; background: {GOODBG}; border: 1px solid #cfe3d6; {R}">
        <div style="display: flex; align-items: center; gap: 8px;">{icon("shield", 16, GOOD)}<span style="font-size: 14px; font-weight: 600;">How this record is verified</span></div>{vr}
        <a href="#" style="display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 500; margin-top: 4px;">Full methodology {icon("arrow", 12)}</a>
      </div>
      <div style="display: flex; flex-direction: column; gap: 10px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <div style="display: flex; align-items: center; justify-content: space-between;"><span style="{LABEL}">Discussion</span><span style="{MONO} font-size: 12px; color: {MUTE};">14 posts</span></div>
        <div style="display: flex; flex-direction: column; gap: 4px; padding: 8px 0; border-bottom: 1px solid {LINE};"><span style="font-size: 13px; color: {INK2};"><span style="color: {INK}; font-weight: 500;">isobar</span> · 2h</span><span style="font-size: 14px;">July drawdown was the Phoenix heat series. Has sizing changed since?</span></div>
        <div style="display: flex; flex-direction: column; gap: 4px; padding: 8px 0;"><span style="font-size: 13px; color: {INK2};"><span style="color: {INK}; font-weight: 500;">quietedge</span> {badge("creator", "neutral")} · 1h</span><span style="font-size: 14px;">Cap per series is now 15% of book. Visible in the concentration number.</span></div>
        <a href="#" style="font-size: 13px; font-weight: 500;">Open the thread</a>
      </div>
    </div>
  </div>
  <p style="margin: 0; {MONO} font-size: 12px; color: {MUTE}; line-height: 1.6;">Every number on this page is computed by Koine from venue data pulled with a read-only key. Past performance is not a guarantee of future results; a 118-day record is short. <a href="#" style="color: {INK2}; text-decoration: underline;">What this page proves and what it does not.</a></p>
</div>
'''
    return page(inner, 2200, "market")


# ================================================================= TAIL flow
def tail():
    steps = [("1", "Account", True), ("2", "Allocation", True), ("3", "Drawdown limit", True), ("4", "Fees", True), ("5", "Confirm", False)]
    st = "".join(f'<div style="display: flex; align-items: center; gap: 10px;"><span style="display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px; border-radius: 50%; {MONO} font-size: 12px; color: {SURF if on else MUTE}; background: {INK if on else "transparent"}; border: 1px solid {INK if on else LINE2};">{n}</span><span style="font-size: 14px; color: {INK if on else MUTE};">{t}</span></div>'
                 + (f'<span style="flex-grow: 1; height: 1px; background: {LINE2};"></span>' if n != "5" else "") for n, t, on in steps)
    acct = (f'<div style="display: flex; align-items: center; justify-content: space-between; padding: 16px 18px; border: 1px solid {INK}; {R} background: {SURF};">'
            f'<div style="display: flex; align-items: center; gap: 12px;"><span style="display: inline-flex; width: 18px; height: 18px; border-radius: 50%; border: 5px solid {INK};"></span>'
            f'<div style="display: flex; flex-direction: column; gap: 2px;"><span style="font-size: 15px; font-weight: 500;">Kalshi · ····4821</span><span style="{MONO} font-size: 12px; color: {MUTE};">trade-scoped key · connected Aug 14 · no withdrawal permission</span></div></div>'
            f'<span style="{MONO} font-size: 15px;">$12,400.00</span></div>'
            f'<a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 500; color: {INK2};">{icon("plus", 14, INK2)}Connect another account</a>')
    fee_rows = [("Performance fee", "20% of profit above high-water mark"), ("High-water mark", "$2,000.00, your starting allocation"), ("Charged", "Monthly, only on new highs"),
                ("Example", "Allocation grows to $2,300: fee is $60. Falls back to $2,100 next month: no fee until it passes $2,300 again."), ("Subscriptions or other fees", "None")]
    fr = "".join(f'<div style="display: grid; grid-template-columns: 200px 1fr; gap: 16px; padding: 10px 0; border-bottom: 1px solid {LINE}; font-size: 14px;"><span style="color: {INK2};">{a}</span><span>{b}</span></div>' for a, b in fee_rows)
    inner = f'''
<div style="display: grid; grid-template-columns: 720px 1fr; gap: 64px; padding: 48px 120px 64px; align-items: start;">
  <div style="display: flex; flex-direction: column; gap: 32px;">
    <div style="display: flex; flex-direction: column; gap: 10px;">
      <span style="{LABEL}">Tail · Weather &amp; CPI mean reversion · by quietedge</span>
      {h1("Tail from your own account.", 40)}
      {p("Set the amount and the limits. Trades are mirrored into the account you choose, in proportion to your allocation. Stop any time.", 15)}
    </div>
    <div style="display: flex; align-items: center; gap: 12px;">{st}</div>
    <div style="display: flex; flex-direction: column; gap: 12px;"><span style="{LABEL}">1 · Connected account</span>{acct}</div>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
      <div style="display: flex; flex-direction: column; gap: 12px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <span style="{LABEL}">2 · Allocation</span>
        <span style="display: inline-flex; align-items: baseline; gap: 6px; {MONO}"><span style="font-size: 36px; font-weight: 500;">$2,000</span><span style="font-size: 13px; color: {MUTE};">16% of account</span></span>
        {bar(16, INK, 6)}
        <div style="display: flex; justify-content: space-between; {MONO} font-size: 11px; color: {MUTE};"><span>$500 min</span><span>$12,400</span></div>
        <span style="font-size: 13px; color: {MUTE};">Koine mirrors each trade at 2,000 / 8,746 of the creator's size.</span>
      </div>
      <div style="display: flex; flex-direction: column; gap: 12px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <span style="{LABEL}">3 · Maximum drawdown</span>
        <span style="display: inline-flex; align-items: baseline; gap: 6px; {MONO}"><span style="font-size: 36px; font-weight: 500; color: {BAD};">−15%</span><span style="font-size: 13px; color: {MUTE};">stop at $1,700</span></span>
        {bar(30, BAD, 6)}
        <div style="display: flex; justify-content: space-between; {MONO} font-size: 11px; color: {MUTE};"><span>−5%</span><span>−50%</span></div>
        <span style="font-size: 13px; color: {MUTE};">If your allocation falls 15% from its high, mirroring stops and you are notified. The strategy's worst drawdown so far is 14.3%.</span>
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 4px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL} padding-bottom: 6px;">4 · Fees</span>{fr}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px; padding: 24px; background: {INK}; color: {SURF}; {R}">
      <span style="{LABEL} color: #b9bcc3;">5 · Confirm</span>
      <span style="font-size: 16px; line-height: 1.5;">Koine will place mirrored trades in Kalshi ····4821 using a trade-scoped key. <span style="color: #b9bcc3;">That key cannot withdraw, transfer or deposit funds. Koine never holds your money.</span></span>
      <label style="display: flex; align-items: flex-start; gap: 12px; font-size: 14px; color: #b9bcc3; line-height: 1.5;"><span style="display: inline-flex; width: 18px; height: 18px; border: 1px solid #6d717a; {R} flex-shrink: 0; margin-top: 2px;"></span>I understand this is a live trading strategy with a 118-day record. I can lose money, including more than the drawdown limit in a fast market.</label>
      <div style="display: flex; align-items: center; gap: 16px;"><a href="#" style="display: inline-flex; align-items: center; height: 48px; padding: 0 22px; font-size: 14px; font-weight: 500; color: {INK}; background: {SURF}; {R}">Start tailing</a><span style="font-size: 13px; color: #b9bcc3;">Stops immediately when you say so.</span></div>
    </div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 20px; position: sticky; top: 24px;">
    <div style="display: flex; flex-direction: column; gap: 14px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL}">You are tailing</span>
      <div style="display: flex; flex-direction: column; gap: 4px;"><span style="font-size: 16px; font-weight: 600;">Weather &amp; CPI mean reversion</span><span style="font-size: 13px; color: {MUTE};">quietedge · Kalshi</span></div>
      {verified("Verified", 118)}
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding-top: 8px; border-top: 1px solid {LINE};">
        <div style="display: flex; flex-direction: column; gap: 4px;"><span style="{LABEL}">Return</span><span style="{MONO} font-size: 18px; color: {GOOD};">+32.5%</span></div>
        <div style="display: flex; flex-direction: column; gap: 4px;"><span style="{LABEL}">Worst DD</span><span style="{MONO} font-size: 18px; color: {BAD};">−14.3%</span></div>
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 10px; padding: 20px 24px; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL}">What Koine can and cannot do</span>
      <div style="display: flex; align-items: flex-start; gap: 10px; font-size: 14px; color: {INK2}; line-height: 1.4;">{icon("check", 16, GOOD, 2)}Place and close mirrored positions, sized to your allocation</div>
      <div style="display: flex; align-items: flex-start; gap: 10px; font-size: 14px; color: {INK2}; line-height: 1.4;">{icon("check", 16, GOOD, 2)}Stop when your drawdown limit is hit</div>
      <div style="display: flex; align-items: flex-start; gap: 10px; font-size: 14px; color: {INK2}; line-height: 1.4;">{icon("x", 16, BAD, 2)}Withdraw, transfer or deposit funds</div>
      <div style="display: flex; align-items: flex-start; gap: 10px; font-size: 14px; color: {INK2}; line-height: 1.4;">{icon("x", 16, BAD, 2)}Trade outside the tailed strategy</div>
      <div style="display: flex; align-items: flex-start; gap: 10px; font-size: 14px; color: {INK2}; line-height: 1.4;">{icon("x", 16, BAD, 2)}See or share the creator's strategy logic</div>
    </div>
  </div>
</div>
'''
    return page(inner, 1700, "market", True)


# ================================================================= PORTFOLIO
def portfolio():
    chart, lo, hi = line_chart([(PORT, INK, 2, None)], 900, 200)
    allocs = [("Weather &amp; CPI mean reversion", "quietedge", 2000, 2318.40, 15.9, -2.1, 34), ("Election polling arbitrage", "median_voter", 3000, 3187.20, 6.2, -0.8, 41), ("Fed path, both directions", "rateswatch", 2500, 2756.50, 10.3, -3.4, 46)]
    ar = "".join(
        f'<div style="display: grid; grid-template-columns: 1fr 110px 110px 90px 90px 70px; align-items: center; gap: 16px; padding: 14px 0; border-bottom: 1px solid {LINE};">'
        f'<div style="display: flex; flex-direction: column; gap: 3px;"><a href="#" style="font-size: 15px; font-weight: 500;">{n}</a><span style="{MONO} font-size: 12px; color: {MUTE};">{h} · {d}d tailing</span></div>'
        f'<span style="{MONO} font-size: 14px; text-align: right; color: {INK2};">${a:,.0f}</span><span style="{MONO} font-size: 15px; text-align: right;">${v:,.2f}</span>'
        f'<span style="{MONO} font-size: 14px; text-align: right; color: {GOOD};">{num(r)}</span><span style="{MONO} font-size: 14px; text-align: right; color: {BAD};">{num(dd)}</span>'
        f'<span style="{MONO} font-size: 12px; text-align: right; color: {MUTE};">−15%</span></div>' for n, h, a, v, r, dd, d in allocs)
    hdr = lambda t, r=True: f'<span style="{LABEL} text-align: {"right" if r else "left"};">{t}</span>'
    acts = [("Today 02:14", "Reconciled", "3 accounts matched venue balances", ""), ("Yesterday", "Mirrored", "Fed path: NO on Sept cut ×120 @ 0.31", "−$37.20"), ("Aug 31", "Fee", "Election polling arbitrage, new high-water mark $3,187.20", "−$18.40"),
            ("Aug 29", "Mirrored", "Weather &amp; CPI: settled NYC high above 91°F, NO", "+$41.20"), ("Aug 28", "Allocation", "Added $500 to Fed path, both directions", "")]
    acr = "".join(f'<div style="display: grid; grid-template-columns: 100px 90px 1fr 80px; gap: 14px; align-items: baseline; padding: 11px 0; border-bottom: 1px solid {LINE};"><span style="{MONO} font-size: 12px; color: {MUTE};">{a}</span><span style="{MONO} font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; color: {INK2};">{b}</span><span style="font-size: 14px;">{c}</span><span style="{MONO} font-size: 13px; text-align: right; color: {numcol(1 if d.startswith("+") else (-1 if d else 0))};">{d}</span></div>' for a, b, c, d in acts)
    risk = [("Portfolio Sharpe, 90d", "1.92"), ("Current drawdown", "−2.1%"), ("Worst drawdown", "−6.8%"), ("Strategy correlation, avg", "0.18"), ("Largest allocation", "38%"), ("Fees paid to date", "$118.40")]
    rk = "".join(f'<div style="display: flex; justify-content: space-between; align-items: baseline; padding: 10px 0; border-bottom: 1px solid {LINE};"><span style="font-size: 14px; color: {INK2};">{a}</span><span style="{MONO} font-size: 14px; color: {BAD if b.startswith("−") else INK};">{b}</span></div>' for a, b in risk)
    inner = f'''
<div style="display: flex; flex-direction: column; gap: 28px; padding: 48px 120px 64px;">
  <div style="display: flex; align-items: flex-end; justify-content: space-between;">
    <div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">Portfolio</span>{h1("Your tailed strategies", 40)}</div>
    <div style="display: flex; gap: 12px;">{btn("Connect an account", "secondary")}{btn("Browse strategies")}</div>
  </div>
  <div style="display: grid; grid-template-columns: 1fr 340px; gap: 24px; align-items: start;">
    <div style="display: flex; flex-direction: column; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-bottom: 1px solid {LINE};">
        {stat("Portfolio value", "$8,262.10", "", "3 accounts", 28)}{stat("P&amp;L, all time", "+$762.10", "good", "+10.2%", 28)}{stat("Current drawdown", "−2.1%", "bad", "worst −6.8%", 28)}{stat("Fees paid", "$118.40", "", "20% above HWM", 28, False)}
      </div>
      <div style="display: flex; flex-direction: column; gap: 10px; padding: 20px 28px;">
        <div style="display: flex; justify-content: space-between;"><span style="{LABEL}">Portfolio value · 90 days</span><span style="{MONO} font-size: 12px; color: {MUTE};">net of fees</span></div>
        <div style="display: grid; grid-template-columns: 48px 1fr; gap: 12px;">{y_axis(lo * 75, hi * 75, 4, lambda v: f"${v / 1000:.1f}k")}<div style="display: flex; flex-direction: column; gap: 8px;">{chart}{x_axis(["Jun 4", "Jul", "Aug", "Sep 1"])}</div></div>
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 4px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}"><span style="{LABEL} padding-bottom: 6px;">Portfolio risk</span>{rk}</div>
  </div>
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start;">
    <div style="display: flex; flex-direction: column; gap: 4px; padding: 20px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: grid; grid-template-columns: 1fr 110px 110px 90px 90px 70px; gap: 16px; padding-bottom: 8px; border-bottom: 1px solid {LINE2};">{hdr("Strategy", False)}{hdr("Allocated")}{hdr("Value")}{hdr("Return")}{hdr("Drawdown")}{hdr("Limit")}</div>{ar}
    </div>
    <div style="display: flex; flex-direction: column; gap: 4px; padding: 20px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: flex; justify-content: space-between; padding-bottom: 8px; border-bottom: 1px solid {LINE2};"><span style="{LABEL}">Activity</span><span style="{MONO} font-size: 12px; color: {MUTE};">all accounts</span></div>{acr}
    </div>
  </div>
</div>
'''
    return page(inner, 1360, "", True)


# ================================================================= CREATOR profile
def creator():
    strategies = [("Weather &amp; CPI mean reversion", 32.5, -14.3, 1.84, 118, 41.2, 23), ("Daily temperature spreads", -8.1, -17.0, -0.31, 101, 0, 0)]
    sr = "".join(
        f'<div style="display: grid; grid-template-columns: 1fr 112px 96px 96px 72px 150px; align-items: center; gap: 18px; padding: 16px 0; border-bottom: 1px solid {LINE};">'
        f'<div style="display: flex; flex-direction: column; gap: 6px;"><a href="#" style="font-size: 16px; font-weight: 500;">{n}</a>{verified("Verified", d)}</div>'
        f'{spark(SPARKS["s3" if r > 0 else "s5"])}'
        f'<span style="{MONO} font-size: 16px; text-align: right; color: {numcol(r)};">{num(r)}</span><span style="{MONO} font-size: 14px; text-align: right; color: {BAD};">{num(dd)}</span>'
        f'<span style="{MONO} font-size: 14px; text-align: right;">{sharpe(s)}</span>'
        f'<span style="{MONO} font-size: 13px; text-align: right; color: {INK2};">{"$" + str(t) + "k · " + str(i) + " investors" if t else "not open to tailing"}</span></div>' for n, r, dd, s, d, t, i in strategies)
    posts = [("2h", "Cap per series is now 15% of book after the July heat-wave run. You can see it in the concentration figure on the page. No other changes.", 12, 3),
             ("Aug 29", "August was the best month on record (+18.4%) and I want to be clear it was mostly two CPI contracts. Do not extrapolate that.", 41, 9),
             ("Aug 12", "Drawdown ran to 14.3% in July. Nothing about the model broke; the heat series settled against the forecast four days straight. Kept sizing flat.", 27, 6)]
    pr = "".join(
        f'<div style="display: flex; flex-direction: column; gap: 10px; padding: 18px 0; border-bottom: 1px solid {LINE};">'
        f'<div style="display: flex; align-items: center; gap: 10px;"><span style="width: 24px; height: 24px; border-radius: 50%; background: #d9d4c9;"></span><span style="font-size: 14px; font-weight: 500;">quietedge</span><span style="{MONO} font-size: 12px; color: {MUTE};">{t}</span>'
        f'<span style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 11px; color: {GOOD}; margin-left: auto;">{icon("shield", 12, GOOD, 2)}+32.5% · −14.3% DD verified</span></div>'
        f'{p(b, 15, INK)}<div style="display: flex; gap: 18px; {MONO} font-size: 12px; color: {MUTE};"><span>{l} likes</span><span>{c} replies</span></div></div>' for t, b, l, c in posts)
    hdr = lambda t, r=True: f'<span style="{LABEL} text-align: {"right" if r else "left"};">{t}</span>'
    inner = f'''
<div style="display: flex; flex-direction: column; gap: 32px; padding: 48px 120px 64px;">
  <div style="display: grid; grid-template-columns: 1fr auto; gap: 48px; align-items: start;">
    <div style="display: flex; align-items: flex-start; gap: 24px;">
      <span style="width: 88px; height: 88px; border-radius: 50%; background: #d9d4c9; flex-shrink: 0;"></span>
      <div style="display: flex; flex-direction: column; gap: 10px;">
        <div style="display: flex; align-items: center; gap: 12px;">{h1("quietedge", 40)}{verified("Verified creator")}</div>
        {p("Macro and weather contracts on Kalshi since 2024. I do not post trades; the page is the proof.", 15, INK2, 560)}
        <div style="display: flex; align-items: center; gap: 20px; {MONO} font-size: 13px; color: {INK2};"><span><span style="color: {INK}; font-weight: 500;">340</span> followers</span><span><span style="color: {INK}; font-weight: 500;">2</span> verified strategies</span><span><span style="color: {INK}; font-weight: 500;">$41.2k</span> tailing</span><span>verified since <span style="color: {INK};">May 2026</span></span></div>
      </div>
    </div>
    <div style="display: flex; gap: 12px;">{btn("Follow", "secondary")}{btn("Tail a strategy")}</div>
  </div>
  <div style="display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); background: {SURF}; border: 1px solid {LINE2}; {R} overflow: hidden;">
    {stat("Best strategy, net", "+32.5%", "good", "118d")}{stat("Worst strategy, net", "−8.1%", "bad", "101d")}{stat("Worst drawdown", "−17.0%", "bad")}{stat("Reconciliation", "121 of 121", "", "nights matched")}{stat("Capital tailing", "$41.2k", "", "23 investors", border=False)}
  </div>
  <div style="display: grid; grid-template-columns: 1fr 420px; gap: 32px; align-items: start;">
    <div style="display: flex; flex-direction: column; gap: 24px;">
      <div style="display: flex; flex-direction: column; padding: 20px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <div style="display: grid; grid-template-columns: 1fr 112px 96px 96px 72px 150px; gap: 18px; padding-bottom: 10px; border-bottom: 1px solid {LINE2};">{hdr("Strategies", False)}{hdr("60 days", False)}{hdr("Return")}{hdr("Worst DD")}{hdr("Sharpe")}{hdr("Tailing")}</div>{sr}
        <span style="{MONO} font-size: 12px; color: {MUTE}; padding-top: 12px;">Both strategies are shown, including the one that is losing. Creators cannot hide a verified record.</span>
      </div>
      <div style="display: flex; flex-direction: column; padding: 8px 28px 20px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <div style="display: flex; align-items: center; gap: 24px; border-bottom: 1px solid {LINE2};">{"".join(f'<span style="font-size: 14px; font-weight: 500; padding: 12px 0; color: {INK if on else MUTE}; border-bottom: 1.5px solid {INK if on else "transparent"};">{t}</span>' for t, on in [("Posts", True), ("Replies", False), ("Investors", False)])}</div>{pr}
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 24px;">
      <div style="display: flex; flex-direction: column; gap: 10px; padding: 20px 24px; background: {GOODBG}; border: 1px solid #cfe3d6; {R}">
        <div style="display: flex; align-items: center; gap: 8px;">{icon("shield", 16, GOOD)}<span style="font-size: 14px; font-weight: 600;">What verified creator means</span></div>
        {p("Every strategy on this profile is computed from a read-only venue key and reconciled nightly. quietedge cannot edit, delete or hide any of it. Followers, posts and likes are not verification.", 14)}
      </div>
      <div style="display: flex; flex-direction: column; gap: 10px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <span style="{LABEL}">Investors tailing quietedge</span>
        <div style="display: flex; align-items: center; gap: 12px;"><span style="{MONO} font-size: 28px; font-weight: 500; line-height: 1;">23</span><span style="font-size: 13px; color: {MUTE};">across 1 strategy · median allocation $1,500</span></div>
        <span style="font-size: 13px; color: {MUTE};">Individual investors are never shown.</span>
      </div>
    </div>
  </div>
</div>
'''
    return page(inner, 1640, "")


# ================================================================= FEED (social)
def feed():
    def post(handle, when, body, ctx, likes, replies, strat=None):
        chip = f'<span style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 11px; color: {GOOD}; background: {GOODBG}; padding: 4px 8px; {R} margin-left: auto;">{icon("shield", 12, GOOD, 2)}{ctx}</span>' if ctx else f'<span style="{MONO} font-size: 11px; color: {MUTE}; margin-left: auto;">no verified record</span>'
        s = (f'<a href="#" style="display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 1px solid {LINE2}; {R} background: {BG};">'
             f'<div style="display: flex; flex-direction: column; gap: 2px;"><span style="font-size: 14px; font-weight: 500;">{strat[0]}</span><span style="{MONO} font-size: 12px; color: {MUTE};">{strat[1]}</span></div>'
             f'<div style="display: flex; align-items: center; gap: 14px; {MONO} font-size: 13px;"><span style="color: {GOOD};">{strat[2]}</span><span style="color: {BAD};">{strat[3]}</span>{icon("arrow", 14, MUTE)}</div></a>') if strat else ""
        return (f'<div style="display: flex; flex-direction: column; gap: 12px; padding: 20px 24px; background: {SURF}; border: 1px solid {LINE2}; {R}">'
                f'<div style="display: flex; align-items: center; gap: 10px;"><span style="width: 28px; height: 28px; border-radius: 50%; background: #d9d4c9;"></span><a href="#" style="font-size: 14px; font-weight: 500;">{handle}</a><span style="{MONO} font-size: 12px; color: {MUTE};">{when}</span>{chip}</div>'
                f'{p(body, 15, INK)}{s}'
                f'<div style="display: flex; gap: 18px; {MONO} font-size: 12px; color: {MUTE};"><span>{likes} likes</span><span>{replies} {"reply" if replies == 1 else "replies"}</span><span>Share</span></div></div>')
    posts = (post("rateswatch", "35m", "September meeting is priced at 86% for a cut. I am on the NO side small, and the page will show it either way.", "+40.7% · −7.8% DD", 18, 4, ("Fed path, both directions", "rateswatch · Kalshi", "+40.7%", "−7.8% DD"))
             + post("quietedge", "2h", "Cap per series is now 15% of book after the July heat-wave run. You can see it in the concentration figure on the page. No other changes.", "+32.5% · −14.3% DD", 12, 3)
             + post("median_voter", "5h", "Reminder for anyone tailing: this strategy goes quiet between election cycles. Expect flat months, not zeros because something broke.", "+29.8% · −4.3% DD", 33, 7)
             + post("newtrader_88", "yesterday", "Anyone else looking at the Q4 GDP contracts? Feels mispriced.", None, 2, 1))
    thread = [("isobar", "10:14", "Is the reconciliation panel showing computed vs. venue balance in the creator's account, or ours?"),
              ("koine", "10:16", "The creator's. Your own account's reconciliation is on your Portfolio page."),
              ("tailrisk", "10:20", "Would love a filter for strategies with no correlation to my current three."),
              ("median_voter", "10:31", "Correlation column would help creators too. Investors keep asking me.")]
    tr = "".join(f'<div style="display: flex; flex-direction: column; gap: 3px; padding: 8px 0;"><span style="{MONO} font-size: 12px; color: {MUTE};"><span style="color: {INK}; font-weight: 500; {SANS}">{h}</span> · {t}</span><span style="font-size: 14px; line-height: 1.45;">{b}</span></div>' for h, t, b in thread)
    inner = f'''
<div style="display: grid; grid-template-columns: 240px 1fr 360px; gap: 32px; padding: 40px 120px 64px; align-items: start;">
  <div style="display: flex; flex-direction: column; gap: 24px;">
    <div style="display: flex; flex-direction: column; gap: 4px;">
      {"".join(f'<a href="#" style="display: flex; align-items: center; gap: 10px; padding: 9px 12px; {R} font-size: 14px; font-weight: 500; color: {INK if on else INK2}; background: {SURF if on else "transparent"}; border: 1px solid {LINE2 if on else "transparent"};">{icon(i, 16, INK if on else MUTE)}{t}</a>' for t, i, on in [("Following", "users", True), ("Verified only", "shield", False), ("Prediction markets", "filter", False), ("Rooms", "chat", False)])}
    </div>
    <div style="display: flex; flex-direction: column; gap: 10px;"><span style="{LABEL}">Rooms</span>
      {"".join(f'<a href="#" style="display: flex; align-items: center; justify-content: space-between; font-size: 14px; color: {INK2}; padding: 4px 0;">#{t}<span style="{MONO} font-size: 11px; color: {MUTE};">{n}</span></a>' for t, n in [("macro", "412"), ("weather", "128"), ("politics", "390"), ("tailing-questions", "96")])}
    </div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 16px;">
    <div style="display: flex; align-items: center; gap: 12px; padding: 14px 18px; background: {SURF}; border: 1px solid {LINE2}; {R}"><span style="width: 28px; height: 28px; border-radius: 50%; background: #d9d4c9;"></span><span style="font-size: 15px; color: {MUTE}; flex-grow: 1;">Post an update. Your verified numbers travel with it.</span>{btn("Post", size=36)}</div>
    {posts}
  </div>
  <div style="display: flex; flex-direction: column; gap: 20px;">
    <div style="display: flex; flex-direction: column; gap: 4px; padding: 16px 20px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: flex; align-items: center; justify-content: space-between; padding-bottom: 8px; border-bottom: 1px solid {LINE};"><span style="display: inline-flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600;">{icon("chat", 16, INK)}#tailing-questions</span><span style="{MONO} font-size: 11px; color: {MUTE};">96 online</span></div>
      {tr}
      <div style="display: flex; align-items: center; gap: 8px; margin-top: 8px; padding: 10px 12px; border: 1px solid {LINE2}; {R}"><span style="font-size: 14px; color: {MUTE}; flex-grow: 1;">Message #tailing-questions</span>{icon("arrow", 16, MUTE)}</div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 8px; padding: 16px 20px; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL}">Feed rules</span>
      {p("Performance claims in posts are ignored. The only numbers next to a name are the ones Koine computed. Accounts without a verified record are labelled as such.", 13)}
    </div>
  </div>
</div>
'''
    return page(inner, 1500, "feed", True)


# ================================================================= CREATORS landing (creator mode)
def creators():
    steps = [("01", "Connect a read-only key", "From your venue account. We check its scope on the spot: a key that can trade is refused. It is encrypted at rest and erased when you disconnect."),
             ("02", "We compute and publish the record", "Fills, settlements and fees flow into an append-only ledger, reconciled against your balance every night. After 90 days of incubation the page goes live on the marketplace."),
             ("03", "Investors tail you. You get paid on their profit.", "Set your capacity. Investors mirror from their own accounts. You earn a performance fee on the gains above each investor's high-water mark.")]
    st = "".join(f'<div style="display: flex; flex-direction: column; gap: 12px; padding: 28px 28px 32px; border-right: 1px solid {LINE};"><span style="{MONO} font-size: 13px; color: {MUTE};">{n}</span>{h3(t, 20)}{p(d, 14)}</div>' for n, t, d in steps)
    never = [("Your model, code or signals", "eye-off"), ("Your open orders in real time", "eye-off"), ("Any ability to move your money", "lock")]
    always = [("Your fills, settlements and fees", "eye"), ("Your balance, for nightly reconciliation", "eye"), ("Losing months, at the same size as winning ones", "eye")]
    li = lambda items, col: "".join(f'<div style="display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid {LINE}; font-size: 15px;">{icon(i, 18, col)}<span>{t}</span></div>' for t, i in items)
    inner = f'''
<div style="display: grid; grid-template-columns: 620px 1fr; gap: 80px; padding: 88px 120px 80px; align-items: center;">
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <span style="{LABEL} color: {GOOD};">For creators</span>
    {h1("Get paid for outcomes. Keep your edge to yourself.", 60)}
    {p("Koine verifies your track record straight from the venue and publishes a page you cannot fake and do not have to explain. Investors tail it from their own accounts. You earn only when they profit.", 18, INK2, 540)}
    <div style="display: flex; align-items: center; gap: 20px; margin-top: 4px;">{btn("Verify a track record", size=48)}<a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 500; color: {INK2};">See an example page {icon("arrow", 14)}</a></div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 0; background: {SURF}; border: 1px solid {LINE2}; {R} padding: 8px 28px 16px;">
    <div style="display: flex; flex-direction: column; padding-top: 12px;"><span style="{LABEL} padding-bottom: 6px;">Koine never sees</span>{li(never, INK)}</div>
    <div style="display: flex; flex-direction: column; padding-top: 20px;"><span style="{LABEL} padding-bottom: 6px;">Koine always shows</span>{li(always, MUTE)}</div>
  </div>
</div>
<div style="display: flex; flex-direction: column; gap: 32px; padding: 0 120px;">
  {h2("Three steps. Nothing to explain.", 32)}
  <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); background: {SURF}; border: 1px solid {LINE2}; {R} overflow: hidden;">{st}</div>
</div>
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; padding: 96px 120px 96px;">
  <div style="display: flex; flex-direction: column; gap: 14px; padding: 32px; background: {SURF}; border: 1px solid {LINE2}; {R}">
    <span style="{LABEL}">Fees, in one line</span>
    <span style="{SERIF} font-size: 28px; font-weight: 500; line-height: 1.2;">You earn 20% of an investor's profit above their high-water mark. Koine keeps a quarter of that.</span>
    {p("No listing fee, no subscription. An investor who is under water pays nothing, so neither of us is paid until they recover.", 14)}
  </div>
  <div style="display: flex; flex-direction: column; gap: 14px; padding: 32px; background: {INK}; color: {SURF}; {R}">
    <span style="{LABEL} color: #b9bcc3;">Already have a record elsewhere?</span>
    <span style="{SERIF} font-size: 28px; font-weight: 500; line-height: 1.2;">We can only verify what the venue can show us.</span>
    <span style="font-size: 14px; color: #b9bcc3; line-height: 1.5;">Your verified window starts the day you connect. Older activity is imported where the venue allows and is labelled by how far back the data reaches.</span>
    <div style="margin-top: 4px;"><a href="#" style="display: inline-flex; align-items: center; height: 44px; padding: 0 20px; font-size: 14px; font-weight: 500; color: {INK}; background: {SURF}; {R}">Connect Kalshi</a></div>
  </div>
</div>
'''
    return page(inner, 1600, "", False, "creator")


# ================================================================= INVITE
def invite():
    whom = [("Traders with a real record", "Anyone posting P&amp;L screenshots on X, Reddit or Discord who could replace them with a link."),
            ("People who invest in traders", "Friends who already follow someone's calls. Koine gives them the record and the drawdown before the money."),
            ("Known names in prediction markets", "Writers, forecasters and public traders. A verified page is a stronger claim than a follower count.")]
    wr = "".join(f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 18px 0; border-bottom: 1px solid {LINE};">{h3(t, 16)}{p(d, 14)}</div>' for t, d in whom)
    sent = [("@rates_desk", "X", "Accepted · verified page live", "good"), ("m.okafor", "email", "Invited Aug 28", "neutral"), ("@forecast_nerd", "X", "Invited Aug 30", "neutral")]
    sr = "".join(f'<div style="display: grid; grid-template-columns: 1fr 60px 200px; gap: 12px; align-items: center; padding: 10px 0; border-bottom: 1px solid {LINE};"><span style="{MONO} font-size: 13px;">{a}</span><span style="{MONO} font-size: 12px; color: {MUTE};">{b}</span>{badge(c, d)}</div>' for a, b, c, d in sent)
    inner = f'''
<div style="display: grid; grid-template-columns: 560px 1fr; gap: 80px; padding: 64px 120px 72px; align-items: start;">
  <div style="display: flex; flex-direction: column; gap: 24px;">
    <span style="{LABEL}">Invite</span>
    {h1("Know a trader who can prove it?", 44)}
    {p("Koine is invitation-first while the marketplace is small. Invite creators you would tail and investors you would trust with a link, not a screenshot.", 16)}
    <div style="display: flex; flex-direction: column;">{wr}</div>
    <div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">Your invite link</span>
      <div style="display: flex; align-items: center; gap: 8px;"><span style="flex-grow: 1; display: inline-flex; align-items: center; height: 44px; padding: 0 14px; background: {SURF}; border: 1px solid {LINE2}; {R} {MONO} font-size: 13px; color: {INK2};">koine.[DOMAIN]/i/quietedge</span>{btn("Copy", "secondary", icon_html=icon("link", 14))}</div>
      <span style="font-size: 13px; color: {MUTE};">Invitations do not affect ranking. A creator you invite still starts in incubation.</span></div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 24px;">
    <div style="display: flex; flex-direction: column; gap: 20px; padding: 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: flex; align-items: center; gap: 4px; padding: 3px; background: {BG}; {R} align-self: flex-start;">{"".join(f'<span style="{MONO} font-size: 12px; padding: 7px 14px; {R} color: {INK if on else MUTE}; background: {SURF if on else "transparent"};">{t}</span>' for t, on in [("Invite a creator", True), ("Invite an investor", False)])}</div>
      {input_("Handle or email", placeholder="@handle or name@example.com", mono=True)}
      {input_("Venue they trade on", value="Kalshi", note="Prediction markets today. Stocks and crypto venues are on the roadmap.")}
      {input_("A note from you", placeholder="Optional. Shown in the invitation.", textarea=True)}
      <div style="display: flex; align-items: center; justify-content: space-between;"><span style="display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: {MUTE};">{icon("mail", 14, MUTE)}We send one message and never follow up.</span>{btn("Send invitation")}</div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 4px; padding: 20px 28px; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL} padding-bottom: 6px;">Sent by you</span>{sr}
    </div>
  </div>
</div>
'''
    return page(inner, 1120, "", True)


# ================================================================= VERIFICATION
def verification():
    toc = ["What a read-only key can do", "The append-only ledger", "Nightly reconciliation", "Returns and the equity curve", "Drawdown", "Sharpe, Sortino and the risk score", "Behavioural flags", "Incubation and the Koine score", "What we cannot verify"]
    tc = "".join(f'<a href="#" style="font-size: 14px; color: {INK if i == 2 else INK2}; padding: 6px 0; border-left: 2px solid {INK if i == 2 else "transparent"}; padding-left: 14px; margin-left: -15px;">{t}</a>' for i, t in enumerate(toc))
    formula = lambda s: f'<div style="{MONO} font-size: 13px; line-height: 1.7; padding: 16px 20px; background: {SURF}; border: 1px solid {LINE2}; {R} white-space: pre-line;">{s}</div>'
    recon = [("2026-09-01 02:14", "$8,746.8000", "$8,746.8000", "match"), ("2026-08-31 02:11", "$8,702.3500", "$8,702.3500", "match"), ("2026-08-30 02:13", "$8,690.1200", "$8,690.1200", "match")]
    rr = "".join(f'<div style="display: grid; grid-template-columns: 1fr 1fr 1fr 80px; gap: 12px; padding: 9px 0; border-bottom: 1px solid {LINE}; {MONO} font-size: 13px;"><span style="color: {MUTE};">{a}</span><span style="text-align: right;">{b}</span><span style="text-align: right;">{c}</span><span style="display: inline-flex; align-items: center; justify-content: flex-end; gap: 6px; color: {GOOD};">{icon("check", 12, GOOD, 2)}{d}</span></div>' for a, b, c, d in recon)
    inner = f'''
<div style="display: grid; grid-template-columns: 260px 760px; gap: 96px; padding: 64px 120px 80px; justify-content: start;">
  <div style="display: flex; flex-direction: column; gap: 24px; position: sticky; top: 24px;">
    <div style="display: flex; flex-direction: column; gap: 6px;">
      {"".join(f'<a href="#" style="font-size: 14px; font-weight: 500; padding: 6px 0; color: {INK if on else MUTE};">{t}</a>' for t, on in [("Verification", True), ("Transparency", False), ("Research", False), ("Security &amp; privacy", False)])}
    </div>
    <div style="display: flex; flex-direction: column; gap: 2px; border-left: 1px solid {LINE2}; padding-left: 14px;">{tc}</div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 32px;">
    <div style="display: flex; flex-direction: column; gap: 16px;">
      <span style="{LABEL}">Verification</span>
      {h1("How a Koine record is computed.", 44)}
      {p("This page is the contract behind the word verified. Every formula here has a unit test with hand-computed expected values, and the same code produces every number on every strategy page.", 17)}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("What a read-only key can do", 26)}
      {p("A creator connects a venue API key scoped to read. Before Koine stores it, the key is tested against the venue: if it can place, cancel or fund anything, it is refused. The key is encrypted with a server-side vault key, never logged, never returned by any page or API, and erased the moment the creator disconnects.")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("The append-only ledger", 26)}
      {p("Fills and settlements are pulled from the venue into a ledger that only accepts new rows. Nothing is ever updated or deleted; a correction is a new row. Every monetary value is stored as integer micro-dollars, because venues report sub-cent fees and rounding them would corrupt the curve that reconciliation has to match.")}
      {formula("fill:        venue_fill_id, market, side, count, price, fee, filled_at\nsettlement:  venue_settlement_id, market, revenue, fee, settled_at\nidempotency: unique venue ids. Re-importing is a no-op.")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("Nightly reconciliation", 26)}
      {p("Each night Koine replays the ledger from the balance recorded at connect time and compares the result with the balance the venue reports. A match is recorded on the page. A divergence quarantines the page until it is explained and matches again; it is never quietly absorbed.")}
      <div style="display: flex; flex-direction: column; gap: 4px; padding: 16px 20px; background: {SURF}; border: 1px solid {LINE2}; {R}">
        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr 80px; gap: 12px; padding-bottom: 8px; border-bottom: 1px solid {LINE2};"><span style="{LABEL}">Run (UTC)</span><span style="{LABEL} text-align: right;">Venue balance</span><span style="{LABEL} text-align: right;">Computed</span><span style="{LABEL} text-align: right;">Result</span></div>{rr}
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("Returns and the equity curve", 26)}
      {p("Equity on a day is cash plus open positions at cost, net of every venue fee. Returns are time-weighted, so a creator's own deposits and withdrawals do not show up as performance.")}
      {formula("equity_t   = cash_t + Σ open_positions_at_cost_t\nr_t        = equity_t / (equity_(t-1) + net_flow_t) − 1\nTWR        = Π (1 + r_t) − 1")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("Drawdown", 26)}
      {p("Drawdown is measured from the running peak of the equity curve at daily UTC close. The worst drawdown, its dates and the days to recover are shown on every page at the same visual weight as the return.")}
      {formula("dd_t       = equity_t / max(equity_0..t) − 1\nworst      = min(dd_t)        recovery = days from trough to new peak")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("Sharpe, Sortino and the risk score", 26)}
      {p("Sharpe and Sortino are computed on daily returns and annualised. Both are shown with the length of the record, because a ratio built on a few weeks of data says more about luck than skill. The risk score is a 1 to 10 rank of realised behaviour only: volatility, worst drawdown, position concentration and the size of the largest single loss. It never uses anything the creator wrote.")}
      {formula("sharpe   = mean(r_t) / stdev(r_t) × √365\nsortino  = mean(r_t) / stdev(r_t where r_t < 0) × √365\nrisk     = rank over volatility, worst dd, concentration, largest loss → 1..10")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("Behavioural flags", 26)}
      {p("Three patterns are checked nightly and shown on the page when found: martingale sizing (stakes that grow after losses), sudden stake spikes relative to the trailing average, and concentration of the book in a single market series. A flag does not remove a strategy; it is printed beside the return so a reader sees it before the number.")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("Incubation and the Koine score", 26)}
      {p("A new strategy is listed as incubating for its first 90 verified days and cannot score above 5.0, however good the return. The Koine score that ranks the marketplace weights Sharpe and Sortino most, then the share of profitable months, then worst drawdown and length of record. It is designed so that a longer, steadier record outranks a shorter, hotter one.")}
      {formula("score = 10 × w_1·f(sharpe, sortino) + w_2·profitable_months + w_3·g(worst_dd) + w_4·h(days)\nincubating (days < 90): score = min(score, 5.0)")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px;">
      {h2("What we cannot verify", 26)}
      {p("Anything the creator wrote. Positions on venues that are not connected. Activity older than the venue's history returns. Whether the record will continue. A verified page is a narrow claim about what happened in one account; treat it that way.")}
    </div>
  </div>
</div>
'''
    return page(inner, 2450, "verify")


# ================================================================= TRANSPARENCY
def transparency():
    bins = [("&lt; −20%", 1), ("−20 to −10", 2), ("−10 to 0", 4), ("0 to 10", 6), ("10 to 20", 5), ("20 to 30", 3), ("30 to 40", 3), ("> 40%", 1)]
    mx = max(n for _, n in bins)
    hist = "".join(f'<div style="display: flex; flex-direction: column; align-items: center; gap: 8px; flex-grow: 1;"><span style="{MONO} font-size: 12px; color: {INK2};">{n}</span><span style="width: 100%; height: {int(140 * n / mx)}px; background: {BAD if l.startswith("<") or l.startswith("−") else INK}; border-radius: 4px 4px 0 0;"></span><span style="{MONO} font-size: 11px; color: {MUTE}; white-space: nowrap;">{l}</span></div>' for l, n in bins)
    failed = [("Momentum on NFL spreads", "Delisted · Jul 2026", "−34.2% · worst drawdown −41%", "Creator disconnected after 62 days. Record frozen and kept."),
              ("Overnight CPI straddle", "Quarantined · Aug 2026", "diverged $212.40 on Aug 19", "Venue balance and ledger disagreed. Under review; page hidden."),
              ("Daily temperature spreads", "Live, losing", "−8.1% · worst drawdown −17.0%", "Still listed. Losing strategies are not removed.")]
    fr = "".join(f'<div style="display: grid; grid-template-columns: 1fr 160px 220px 1fr; gap: 16px; align-items: baseline; padding: 14px 0; border-bottom: 1px solid {LINE};"><span style="font-size: 15px; font-weight: 500;">{a}</span><span style="{MONO} font-size: 12px; color: {INK2};">{b}</span><span style="{MONO} font-size: 13px; color: {BAD};">{c}</span><span style="font-size: 13px; color: {MUTE};">{d}</span></div>' for a, b, c, d in failed)
    inner = f'''
<div style="display: flex; flex-direction: column; gap: 32px; padding: 64px 120px 72px;">
  <div style="display: grid; grid-template-columns: 620px 1fr; gap: 64px; align-items: end;">
    <div style="display: flex; flex-direction: column; gap: 16px;">
      <span style="{LABEL}">Transparency</span>
      {h1("The whole population, not the winners.", 44)}
      {p("Everything on this page is computed from every strategy that has ever connected, including the ones that lost, disconnected or were quarantined. Updated nightly.", 16)}
    </div>
    <span style="{MONO} font-size: 12px; color: {MUTE}; text-align: right;">Figures shown are sample data. The live page draws from the platform ledger.</span>
  </div>
  <div style="display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); background: {SURF}; border: 1px solid {LINE2}; {R} overflow: hidden;">
    {stat("Strategies ever verified", "25")}{stat("Losing money today", "7", "bad", "28%")}{stat("Median return, net", "+6.4%", "good", "all records")}{stat("Median worst drawdown", "−12.8%", "bad")}{stat("Quarantined or delisted", "3", "", "records kept", border=False)}
  </div>
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start;">
    <div style="display: flex; flex-direction: column; gap: 16px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <div style="display: flex; justify-content: space-between;"><span style="{LABEL}">Net return since verification, all 25 strategies</span></div>
      <div style="display: flex; align-items: flex-end; gap: 8px; height: 190px; padding-top: 10px; border-bottom: 1px solid {LINE2};">{hist}</div>
      {p("Seven of twenty-five are under water. Investors tailing the median strategy would have made 6.4% net of fees, with a 12.8% drawdown along the way.", 13)}
    </div>
    <div style="display: flex; flex-direction: column; gap: 16px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL}">Investor outcomes, all tailing positions</span>
      <div style="display: flex; flex-direction: column; gap: 14px;">
        {"".join(f'<div style="display: flex; flex-direction: column; gap: 6px;"><div style="display: flex; justify-content: space-between; font-size: 14px;"><span style="color: {INK2};">{a}</span><span style="{MONO}">{b}</span></div>{bar(c, INK, 6)}</div>' for a, b, c in [("Positions in profit", "61%", 61), ("Positions stopped by a drawdown limit", "9%", 9), ("Positions closed by the investor", "22%", 22), ("Median fee paid, as share of profit", "20%", 20)])}
      </div>
      {p("Fees are only ever a share of profit above a high-water mark, so an investor under water has paid nothing.", 13)}
    </div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 4px; padding: 24px 28px; background: {SURF}; border: 1px solid {LINE2}; {R}">
    <div style="display: flex; justify-content: space-between; padding-bottom: 8px; border-bottom: 1px solid {LINE2};"><span style="{LABEL}">Failed, quarantined and losing strategies</span><span style="{MONO} font-size: 12px; color: {MUTE};">records are never deleted</span></div>{fr}
  </div>
</div>
'''
    return page(inner, 1420, "transparency")


# ================================================================= RESEARCH
def research():
    guides = [("Start here", "How to read a track record", "Return is the last number to look at. Read the verified window, the worst drawdown and how it was recovered first.", "8 min"),
              ("Risk", "Why a 90-day Sharpe lies", "Short records flatter volatile strategies. What a Sharpe ratio needs before it means anything, and why Koine caps incubating scores.", "6 min"),
              ("Risk", "Drawdown is the price of the return", "Two strategies with the same return and different drawdowns are not the same bet. How to set a limit you will actually hold.", "7 min"),
              ("Capacity", "Returns shrink when money follows", "Prediction markets are thin. What capacity means on a Koine page and why it is an estimate.", "5 min"),
              ("Bias", "Survivorship and the screenshot", "Why every P&amp;L you see on social media is a selected sample, and what the Transparency page does about it.", "6 min"),
              ("Fees", "High-water marks, worked through", "Three months of numbers showing exactly when a performance fee is charged and when it is not.", "4 min")]
    gr = "".join(f'<a href="#" style="display: flex; flex-direction: column; gap: 10px; padding: 24px; background: {SURF}; border: 1px solid {LINE2}; {R} color: {INK};"><span style="{LABEL}">{c}</span><span style="{SERIF} font-size: 24px; font-weight: 500; line-height: 1.2;">{t}</span>{p(d, 14)}<span style="{MONO} font-size: 12px; color: {MUTE}; margin-top: auto; padding-top: 6px;">{m} read</span></a>' for c, t, d, m in guides)
    check = ["Is the verified window longer than the strategy's worst drawdown took to recover?", "Would you hold through the worst drawdown shown, at your allocation?", "Is the return concentrated in one month or one market series?", "How much of the estimated capacity is already taken?", "Does the creator's description promise anything the numbers do not show?"]
    cr = "".join(f'<div style="display: flex; align-items: flex-start; gap: 12px; padding: 12px 0; border-bottom: 1px solid {LINE}; font-size: 15px; line-height: 1.45;"><span style="display: inline-flex; width: 18px; height: 18px; border: 1px solid {LINE2}; {R} flex-shrink: 0; margin-top: 2px; background: {SURF};"></span><span>{t}</span></div>' for t in check)
    inner = f'''
<div style="display: flex; flex-direction: column; gap: 40px; padding: 64px 120px 72px;">
  <div style="display: flex; flex-direction: column; gap: 16px; max-width: 700px;">
    <span style="{LABEL}">Research</span>
    {h1("Learn to read a record before you trust one.", 44)}
    {p("Short, specific guides on evaluating track records and risk. Written for investors who are about to tail someone, and for creators who want to be judged on the right numbers.", 16)}
  </div>
  <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px;">{gr}</div>
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px; align-items: start;">
    <div style="display: flex; flex-direction: column; gap: 8px; padding: 24px 28px; border: 1px solid {LINE2}; {R}">
      <span style="{LABEL}">Before you tail: five questions</span>{cr}
      <span style="font-size: 13px; color: {MUTE}; padding-top: 8px;">Every answer is on the strategy page. If one is not, that is the answer.</span>
    </div>
    <div style="display: flex; flex-direction: column; gap: 12px; padding: 24px 28px; background: {INK}; color: {SURF}; {R}">
      <span style="{LABEL} color: #b9bcc3;">Glossary</span>
      {"".join(f'<div style="display: grid; grid-template-columns: 150px 1fr; gap: 16px; padding: 10px 0; border-bottom: 1px solid #2b2e35; font-size: 14px;"><span style="{MONO} font-size: 13px;">{a}</span><span style="color: #b9bcc3; line-height: 1.45;">{b}</span></div>' for a, b in [("Worst drawdown", "Largest fall from a peak in the equity curve, at daily close."), ("Sharpe", "Return per unit of volatility, annualised. Meaningless under ~90 days."), ("High-water mark", "The highest value your allocation has reached. Fees apply only above it."), ("Capacity", "Estimated capital a strategy can absorb before its own trades move the price."), ("Incubation", "The first 90 verified days. Listed, but cannot rank above 5.0.")])}
    </div>
  </div>
</div>
'''
    return page(inner, 1520, "research")


# ================================================================= DIRECTION sketches (hero only)
def direction_b():
    # Graphite: the current dark tape, lightened. Plex only, green accent.
    bg, surf, line, ink, ink2, mute, good, bad = "#1a1d22", "#22262c", "#30353d", "#eceef1", "#a7adb8", "#7b8290", "#2fbf71", "#f0626b"
    label = MONO + f" font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: {mute};"
    pts = pts_str(scale(STRAT, 560, 180))
    return HEAD.replace(BG, bg).replace(INK, ink) + f'''<div style="width: 1440px; min-height: 900px; background: {bg}; color: {ink}; display: flex; flex-direction: column;">
<div style="display: flex; align-items: center; justify-content: space-between; height: 64px; padding: 0 48px; border-bottom: 1px solid {line};">
  <div style="display: flex; align-items: center; gap: 28px;">{brand(ink)}<div style="display: flex; gap: 2px; padding: 3px; background: {surf}; border-radius: 2px;"><span style="{MONO} font-size: 12px; padding: 6px 12px; color: {ink}; background: {bg}; border-radius: 2px;">For investors</span><span style="{MONO} font-size: 12px; padding: 6px 12px; color: {mute};">For creators</span></div></div>
  <div style="display: flex; gap: 28px;">{"".join(f'<span style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: {ink2};">{t}</span>' for t in ["Marketplace", "Feed", "Verification", "Transparency", "Research"])}</div>
  <span style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: {bg}; background: {ink}; padding: 10px 16px; border-radius: 2px;">Get started</span>
</div>
<div style="display: grid; grid-template-columns: 600px 540px; justify-content: space-between; align-items: center; padding: 96px 120px;">
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <span style="{label} color: {good};">Verified strategy marketplace</span>
    <h1 style="margin: 0; font-size: 64px; font-weight: 600; line-height: 1.0; letter-spacing: -0.03em;">Invest in traders who can prove it.</h1>
    <p style="margin: 0; font-size: 18px; line-height: 1.5; color: {ink2}; max-width: 520px;">Track records verified from venue data. Tail from your own account with a drawdown limit you set. Creators are paid only when you profit.</p>
    <div style="display: flex; gap: 20px; align-items: center;"><span style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: {bg}; background: {ink}; padding: 15px 20px; border-radius: 2px;">Browse verified strategies</span><span style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: {ink2};">How verification works →</span></div>
  </div>
  <div style="display: flex; flex-direction: column; background: {surf}; border: 1px solid {line}; border-radius: 2px;">
    <div style="display: flex; justify-content: space-between; padding: 20px 24px; border-bottom: 1px solid {line};"><div style="display: flex; flex-direction: column; gap: 4px;"><span style="font-size: 17px; font-weight: 600;">Weather &amp; CPI mean reversion</span><span style="{MONO} font-size: 12px; color: {mute};">quietedge · Kalshi</span></div><span style="{MONO} font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: {ink}; border: 1px solid {good}; padding: 5px 10px; border-radius: 2px; height: fit-content;">Verified · 118 days</span></div>
    <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-bottom: 1px solid {line};">
      {"".join(f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 16px 24px; border-right: 1px solid {line};"><span style="{label}">{a}</span><span style="{MONO} font-size: 24px; color: {c};">{b}</span></div>' for a, b, c in [("Return", "+32.5%", good), ("Worst DD", "−14.3%", bad), ("Sharpe", "1.84", ink)])}
    </div>
    <div style="padding: 20px 24px;"><svg width="100%" viewBox="0 0 560 180" preserveAspectRatio="none" style="display: block;">{"".join(f'<line x1="0" y1="{180 * i / 4}" x2="560" y2="{180 * i / 4}" stroke="{line}"></line>' for i in range(5))}<polyline points="{pts}" fill="none" stroke="{good}" stroke-width="2" vector-effect="non-scaling-stroke"></polyline></svg></div>
  </div>
</div>
<div style="display: flex; justify-content: space-between; padding: 16px 48px; border-top: 1px solid {line};">{"".join(f'<span style="{label}">{t}</span>' for t in ["Read-only keys", "Nightly reconciliation", "Your money stays in your account", "Strategies stay private", "Paid only on profit"])}</div>
</div>
''' + TAIL


def direction_c():
    # Ledger: cool white product-app look, blue action colour, all-sans (Geist).
    bg, surf, line, line2, ink, ink2, mute, blue, good, bad = "#fbfbfd", "#ffffff", "#e8eaf0", "#d5d9e2", "#0f1320", "#4b5263", "#7c8494", "#2456e0", "#158a4f", "#c8323d"
    sans = 'font-family: Geist, &quot;Helvetica Neue&quot;, Helvetica, Arial, sans-serif;'
    head = HEAD.replace(BG, bg).replace(INK, ink).replace(FONTS, '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&amp;family=IBM+Plex+Mono:wght@400;500&amp;display=swap">').replace('font-family: "IBM Plex Sans", "Helvetica Neue", Helvetica, Arial, sans-serif;', 'font-family: Geist, "Helvetica Neue", Helvetica, Arial, sans-serif;')
    pts = pts_str(scale(STRAT, 560, 180))
    pill = lambda t, on: f'<span style="font-size: 13px; font-weight: 500; padding: 7px 14px; border-radius: 999px; color: {ink if on else mute}; background: {surf if on else "transparent"}; box-shadow: {"0 1px 2px rgba(15,19,32,0.08)" if on else "none"};">{t}</span>'
    return head + f'''<div style="width: 1440px; min-height: 900px; background: {bg}; color: {ink}; {sans} display: flex; flex-direction: column;">
<div style="display: flex; align-items: center; justify-content: space-between; height: 64px; padding: 0 48px; border-bottom: 1px solid {line};">
  <div style="display: flex; align-items: center; gap: 28px;"><span style="display: inline-flex; align-items: center; gap: 10px;">{mark(22, blue)}<span style="font-size: 20px; font-weight: 700; letter-spacing: -0.02em;">Koine</span></span><div style="display: flex; gap: 2px; padding: 3px; background: #eef0f5; border-radius: 999px;">{pill("For investors", True)}{pill("For creators", False)}</div></div>
  <div style="display: flex; gap: 28px;">{"".join(f'<span style="font-size: 14px; font-weight: 500; color: {ink2};">{t}</span>' for t in ["Marketplace", "Feed", "Verification", "Transparency", "Research"])}</div>
  <span style="font-size: 14px; font-weight: 600; color: {surf}; background: {blue}; padding: 9px 16px; border-radius: 8px;">Get started</span>
</div>
<div style="display: grid; grid-template-columns: 600px 540px; justify-content: space-between; align-items: center; padding: 96px 120px;">
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <span style="display: inline-flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; color: {blue}; background: #e9eefc; padding: 6px 12px; border-radius: 999px; align-self: flex-start;">{icon("shield", 14, blue)}Verified from venue data</span>
    <h1 style="margin: 0; font-size: 64px; font-weight: 700; line-height: 1.02; letter-spacing: -0.035em;">Invest in traders who can prove it.</h1>
    <p style="margin: 0; font-size: 18px; line-height: 1.5; color: {ink2}; max-width: 520px;">Track records verified from venue data. Tail from your own account with a drawdown limit you set. Creators are paid only when you profit.</p>
    <div style="display: flex; gap: 16px; align-items: center;"><span style="font-size: 15px; font-weight: 600; color: {surf}; background: {blue}; padding: 14px 22px; border-radius: 8px;">Browse verified strategies</span><span style="font-size: 15px; font-weight: 600; color: {ink}; background: {surf}; border: 1px solid {line2}; padding: 13px 22px; border-radius: 8px;">How verification works</span></div>
  </div>
  <div style="display: flex; flex-direction: column; background: {surf}; border: 1px solid {line2}; border-radius: 14px; box-shadow: 0 20px 50px -24px rgba(15,19,32,0.25);">
    <div style="display: flex; justify-content: space-between; padding: 20px 24px; border-bottom: 1px solid {line};"><div style="display: flex; flex-direction: column; gap: 4px;"><span style="font-size: 17px; font-weight: 600;">Weather &amp; CPI mean reversion</span><span style="font-size: 13px; color: {mute};">quietedge · Kalshi</span></div><span style="display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: {good}; background: #e6f4ec; padding: 6px 10px; border-radius: 999px; height: fit-content;">{icon("shield", 12, good, 2)}Verified · 118 days</span></div>
    <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-bottom: 1px solid {line};">
      {"".join(f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 16px 24px; border-right: 1px solid {line};"><span style="font-size: 12px; font-weight: 500; color: {mute};">{a}</span><span style="font-size: 24px; font-weight: 600; letter-spacing: -0.02em; color: {c};">{b}</span></div>' for a, b, c in [("Return", "+32.5%", good), ("Worst drawdown", "−14.3%", bad), ("Sharpe", "1.84", ink)])}
    </div>
    <div style="padding: 20px 24px;"><svg width="100%" viewBox="0 0 560 180" preserveAspectRatio="none" style="display: block;">{"".join(f'<line x1="0" y1="{180 * i / 4}" x2="560" y2="{180 * i / 4}" stroke="{line}"></line>' for i in range(5))}<polyline points="{pts}" fill="none" stroke="{blue}" stroke-width="2" vector-effect="non-scaling-stroke"></polyline></svg></div>
  </div>
</div>
<div style="display: flex; justify-content: space-between; padding: 16px 48px; border-top: 1px solid {line};">{"".join(f'<span style="display: inline-flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; color: {ink2};">{icon("check", 14, good, 2)}{t}</span>' for t in ["Read-only keys", "Nightly reconciliation", "Your money stays in your account", "Strategies stay private", "Paid only on profit"])}</div>
</div>
''' + TAIL


def direction_a():
    # Prospectus: the lead direction's hero, cropped to 900 for side-by-side comparison
    inner = f'''
<div style="display: grid; grid-template-columns: 600px 540px; justify-content: space-between; align-items: center; padding: 96px 120px;">
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <span style="{LABEL} color: {GOOD};">Verified strategy marketplace</span>
    {h1("Invest in traders who can prove it.", 64)}
    {p("Track records verified from venue data. Tail from your own account with a drawdown limit you set. Creators are paid only when you profit.", 18, INK2, 520)}
    <div style="display: flex; align-items: center; gap: 20px;">{btn("Browse verified strategies", size=48)}<a href="#" style="display: inline-flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 500; color: {INK2};">How verification works {icon("arrow", 14)}</a></div>
  </div>
  {strategy_card()}
</div>
<div style="display: flex; justify-content: space-between; padding: 16px 48px; border-top: 1px solid {LINE};">{"".join(f'<span style="{LABEL}">{t}</span>' for t in ["Read-only keys", "Nightly reconciliation", "Your money stays in your account", "Strategies stay private", "Paid only on profit"])}</div>
'''
    return HEAD + f'<div style="width: 1440px; min-height: 900px; background: {BG}; display: flex; flex-direction: column;">{nav()}{inner}</div>' + TAIL


if __name__ == "__main__":
    out = [("Main", landing), ("Marketplace", marketplace), ("Strategy", strategy), ("Tail", tail), ("Portfolio", portfolio),
           ("Creator", creator), ("Feed", feed), ("Creators", creators), ("Invite", invite), ("Verification", verification),
           ("Transparency", transparency), ("Research", research)]
    for name, fn in out:
        open(f"{name}.dc.html", "w").write(fn())
        print(name, "ok")
