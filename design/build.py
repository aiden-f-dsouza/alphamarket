import json

C = json.load(open("curves.json"))

HEAD = '''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&amp;family=IBM+Plex+Sans:wght@400;500;600;700&amp;display=swap">
  <style>
    body { margin: 0; background: #0b0d10; color: #e9ecf0; font-family: "IBM Plex Sans", "Helvetica Neue", Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; }
    a { color: #e9ecf0; text-decoration: none; } a:hover { color: #1fc46a; }
    * { box-sizing: border-box; }
    .mono { font-family: "IBM Plex Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
    input, textarea { font-family: inherit; }
  </style>
</helmet>
'''
TAIL = '''</x-dc>
</body>
</html>
'''
MONO = 'font-family: &quot;IBM Plex Mono&quot;, Menlo, Consolas, monospace; font-variant-numeric: tabular-nums;'
LABEL = MONO + ' font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: #7d8694;'

def icon(name, size=16, color="currentColor"):
    p = {
     "check": '<polyline points="3.5,8.5 6.5,11.5 12.5,4.5"></polyline>',
     "x": '<line x1="4" y1="4" x2="12" y2="12"></line><line x1="12" y1="4" x2="4" y2="12"></line>',
     "arrow": '<line x1="4" y1="12" x2="12" y2="4"></line><polyline points="6,4 12,4 12,10"></polyline>',
     "lock": '<rect x="3" y="7" width="10" height="7" rx="1"></rect><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"></path>',
     "eye": '<path d="M1.5 8s2.5-4.5 6.5-4.5S14.5 8 14.5 8s-2.5 4.5-6.5 4.5S1.5 8 1.5 8z"></path><circle cx="8" cy="8" r="2"></circle>',
     "sync": '<path d="M13 8a5 5 0 0 1-8.5 3.6"></path><path d="M3 8a5 5 0 0 1 8.5-3.6"></path><polyline points="11,1.5 11.5,4.5 8.5,5"></polyline><polyline points="5,14.5 4.5,11.5 7.5,11"></polyline>',
     "plus": '<line x1="8" y1="3" x2="8" y2="13"></line><line x1="3" y1="8" x2="13" y2="8"></line>',
    }[name]
    return f'<svg width="{size}" height="{size}" viewBox="0 0 16 16" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink: 0;">{p}</svg>'

def brand():
    return ('<a href="#" style="display: flex; align-items: center; gap: 10px; color: #e9ecf0;">'
            '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" style="flex-shrink: 0;"><rect x="1" y="1" width="16" height="16" stroke="#e9ecf0" stroke-width="1.5"></rect><polyline points="4,11 7.5,7.5 10,10 14,5" stroke="#1fc46a" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></polyline></svg>'
            '<span style="font-size: 15px; font-weight: 600; letter-spacing: -0.01em;">AlphaMarket</span></a>')

def nav(active="", logged_in=False):
    links = [("Explore", "explore"), ("Methodology", "methodology"), ("Security", "security")]
    if logged_in: links.append(("Dashboard", "dashboard"))
    items = "".join(f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: {"#e9ecf0" if key == active else "#a3abb8"}; padding: 6px 0; border-bottom: 1px solid {"#1fc46a" if key == active else "transparent"};">{t}</a>' for t, key in links)
    if logged_in:
        right = (f'<span style="{MONO} font-size: 12px; color: #a3abb8;">quietedge</span>'
                 f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #a3abb8; padding: 9px 14px; border: 1px solid #2a303a; border-radius: 2px;">Log out</a>')
    else:
        right = (f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #a3abb8; padding: 9px 4px;">Log in</a>'
                 f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #0b0d10; background: #f2f4f6; padding: 10px 16px; border-radius: 2px; font-weight: 500;">Connect a key</a>')
    return (f'<div style="display: flex; align-items: center; justify-content: space-between; height: 60px; padding: 0 48px; border-bottom: 1px solid #1e232a;">'
            f'{brand()}<div style="display: flex; align-items: center; gap: 32px;">{items}</div>'
            f'<div style="display: flex; align-items: center; gap: 20px;">{right}</div></div>')

def badge(text, tone="good"):
    col = {"good": "#1fc46a", "warn": "#e0a63a", "bad": "#f0626b", "neutral": "#7d8694"}[tone]
    dot = f'<span style="width: 6px; height: 6px; background: {col}; border-radius: 50%; flex-shrink: 0;"></span>' if tone != "neutral" else ""
    return (f'<span style="display: inline-flex; align-items: center; gap: 8px; {MONO} font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: {"#a3abb8" if tone=="neutral" else "#e9ecf0"}; border: 1px solid {"#2a303a" if tone=="neutral" else col}; padding: 5px 10px; border-radius: 2px;">{dot}{text}</span>')

def equity_svg(w, h, pts_key, gridy=4, labels=None, stroke="#1fc46a"):
    grid = "".join(f'<line x1="0" y1="{h*i/gridy:.1f}" x2="{w}" y2="{h*i/gridy:.1f}" stroke="#1e232a" stroke-width="1"></line>' for i in range(gridy+1))
    pts = C[pts_key]
    last = pts.split()[-1]
    lx, ly = last.split(",")
    return (f'<svg width="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display: block; overflow: visible;">{grid}'
            f'<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"></polyline>'
            f'<circle cx="{lx}" cy="{ly}" r="5" fill="{stroke}" stroke="#0b0d10" stroke-width="2"></circle></svg>')

def dd_svg(w=640, h=120):
    pts = C["main_dd"]
    area = f'0,0 {pts} 640,0'
    return (f'<svg width="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="display: block;">'
            f'<line x1="0" y1="0" x2="{w}" y2="0" stroke="#2a303a" stroke-width="1"></line>'
            f'<line x1="0" y1="{h}" x2="{w}" y2="{h}" stroke="#1e232a" stroke-width="1"></line>'
            f'<polygon points="{area}" fill="#f0626b" fill-opacity="0.12"></polygon>'
            f'<polyline points="{pts}" fill="none" stroke="#f0626b" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"></polyline></svg>')

def spark(key, good=True):
    col = "#1fc46a" if good else "#f0626b"
    return (f'<svg width="120" height="32" viewBox="0 0 120 32" style="display: block;">'
            f'<polyline points="{C[key]}" fill="none" stroke="{col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline></svg>')

def stat(label, value, tone="", small=""):
    col = {"good": "#1fc46a", "bad": "#f0626b", "": "#e9ecf0"}[tone]
    return (f'<div style="display: flex; flex-direction: column; gap: 6px; padding: 18px 20px; border-right: 1px solid #1e232a;">'
            f'<span style="{LABEL}">{label}</span>'
            f'<span style="{MONO} font-size: 24px; font-weight: 500; color: {col}; line-height: 1;">{value}<span style="font-size: 13px; color: #7d8694; margin-left: 4px;">{small}</span></span></div>')

def btn(text, kind="primary"):
    if kind == "primary":
        s = "color: #0b0d10; background: #f2f4f6; border: 1px solid #f2f4f6;"
    elif kind == "danger":
        s = "color: #f0626b; background: transparent; border: 1px solid #f0626b;"
    elif kind == "good":
        s = "color: #0b0d10; background: #1fc46a; border: 1px solid #1fc46a;"
    else:
        s = "color: #e9ecf0; background: transparent; border: 1px solid #2a303a;"
    return f'<a href="#" style="display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: 44px; padding: 0 20px; {MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; font-weight: 500; border-radius: 2px; {s}">{text}</a>'

def input_(label, placeholder="", value="", mono=False, textarea=False, note=""):
    f = MONO + " font-size: 13px;" if mono else "font-size: 15px;"
    base = f'width: 100%; background: #0b0d10; color: #e9ecf0; border: 1px solid #2a303a; border-radius: 2px; padding: 12px 14px; {f}'
    if textarea:
        ctl = f'<textarea style="{base} min-height: 132px; resize: vertical;" placeholder="{placeholder}">{value}</textarea>'
    else:
        ctl = f'<input type="text" style="{base} height: 44px;" placeholder="{placeholder}" value="{value}">'
    n = f'<span style="font-size: 13px; color: #7d8694;">{note}</span>' if note else ""
    return f'<label style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">{label}</span>{ctl}{n}</label>'

def footer():
    return (f'<div style="display: flex; align-items: center; justify-content: space-between; padding: 32px 48px; border-top: 1px solid #1e232a;">'
            f'{brand()}<div style="display: flex; gap: 28px;">'
            f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #a3abb8;">Explore</a>'
            f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #a3abb8;">Methodology</a>'
            f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #a3abb8;">Security &amp; privacy</a></div>'
            f'<span style="{MONO} font-size: 12px; color: #7d8694;">No money movement. Read-only keys only.</span></div>')

# ---------------- strategy card (used on landing + explore hero) ----------------
def strategy_card():
    return (f'<div style="display: flex; flex-direction: column; background: #101317; border: 1px solid #2a303a; border-radius: 2px; width: 520px;">'
            f'<div style="display: flex; align-items: flex-start; justify-content: space-between; padding: 20px 24px; border-bottom: 1px solid #1e232a;">'
            f'<div style="display: flex; flex-direction: column; gap: 4px;"><span style="font-size: 17px; font-weight: 600;">Weather &amp; CPI mean reversion</span><span style="{MONO} font-size: 12px; color: #7d8694;">quietedge · Kalshi</span></div>'
            f'{badge("Platform-verified · 118 days")}</div>'
            f'<div style="display: flex; flex-direction: column; gap: 4px; padding: 24px 24px 8px;"><span style="{LABEL}">Total return, net of fees</span>'
            f'<span style="{MONO} font-size: 48px; font-weight: 500; color: #1fc46a; line-height: 1; letter-spacing: -0.02em;">+32.5%</span>'
            f'<span style="{MONO} font-size: 12px; color: #7d8694; margin-top: 6px;">since verified 2026-05-06</span></div>'
            f'<div style="padding: 12px 24px 20px;">{equity_svg(640, 220, "main_equity")}</div>'
            f'<div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border-top: 1px solid #1e232a;">'
            f'<div style="display: flex; flex-direction: column; gap: 4px; padding: 14px 24px; border-right: 1px solid #1e232a;"><span style="{LABEL}">Max DD</span><span style="{MONO} font-size: 16px; color: #f0626b;">−14.3%</span></div>'
            f'<div style="display: flex; flex-direction: column; gap: 4px; padding: 14px 20px; border-right: 1px solid #1e232a;"><span style="{LABEL}">Risk</span><span style="{MONO} font-size: 16px;">6<span style="color: #7d8694;">/10</span></span></div>'
            f'<div style="display: flex; flex-direction: column; gap: 4px; padding: 14px 20px; border-right: 1px solid #1e232a;"><span style="{LABEL}">Sharpe</span><span style="{MONO} font-size: 16px;">1.84</span></div>'
            f'<div style="display: flex; flex-direction: column; gap: 4px; padding: 14px 20px;"><span style="{LABEL}">Reconciled</span><span style="display: flex; align-items: center; gap: 6px; {MONO} font-size: 13px; color: #e9ecf0;">{icon("check", 14, "#1fc46a")}02:14 UTC</span></div>'
            f'</div></div>')

# ---------------- MAIN: landing ----------------
def landing():
    strip_items = ["Read-only keys only", "Write scope rejected at connect", "Nightly balance reconciliation", "Append-only ledger", "No self-reported numbers", "No money movement"]
    strip = "".join(f'<span style="{MONO} font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: #a3abb8; white-space: nowrap;">{s}</span>' + ('<span style="width: 4px; height: 4px; background: #2a303a; flex-shrink: 0;"></span>' if i < len(strip_items)-1 else "") for i, s in enumerate(strip_items))
    steps = [
        ("01", "Connect", "Paste a read-only Kalshi API key. We check its scope before we store it: a key that can place orders is rejected on the spot. The private key is encrypted at rest and erased the moment you disconnect."),
        ("02", "Reconcile", "Every fill and settlement is pulled straight from the venue into an append-only ledger. Each night we replay it and compare the computed cash against your actual Kalshi balance. If they diverge, the page says so."),
        ("03", "Publish", "Your public page shows only what we computed: equity curve, drawdown, monthly returns, risk score, behavioural flags. The one thing you write, your thesis, sits in a box labelled unverified."),
    ]
    steps_html = "".join(
        f'<div style="display: flex; flex-direction: column; gap: 16px; padding: 32px 32px 40px; border-right: 1px solid #1e232a;">'
        f'<span style="{MONO} font-size: 13px; color: #1fc46a;">{n}</span>'
        f'<span style="font-size: 24px; font-weight: 600; letter-spacing: -0.01em;">{t}</span>'
        f'<p style="margin: 0; font-size: 15px; line-height: 1.55; color: #a3abb8; text-wrap: pretty;">{d}</p></div>' for n, t, d in steps)
    proves = ["Every trade on the page happened on Kalshi, in this account.", "Returns are net of venue fees, to the sub-cent.", "The equity curve matched the venue balance last night.", "The connected key cannot place, cancel or fund anything."]
    doesnt = ["That the trader will keep winning.", "Anything the trader typed, including the thesis.", "Activity older than the venue's fill history allows.", "Positions held on any other venue."]
    li = lambda items, ok: "".join(f'<div style="display: flex; align-items: flex-start; gap: 14px; padding: 16px 0; border-bottom: 1px solid #1e232a; font-size: 16px; line-height: 1.4;">{icon("check" if ok else "x", 18, "#1fc46a" if ok else "#7d8694")}<span style="color: {"#e9ecf0" if ok else "#a3abb8"};">{t}</span></div>' for t in items)
    return HEAD + f'''<div style="width: 1440px; min-height: 2140px; background: #0b0d10; display: flex; flex-direction: column;">
{nav()}
<div style="display: grid; grid-template-columns: 620px 520px; justify-content: space-between; align-items: center; padding: 96px 120px 88px;">
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <span style="{LABEL} color: #1fc46a;">Verified track records for Kalshi traders</span>
    <h1 style="margin: 0; font-size: 68px; line-height: 1.0; font-weight: 600; letter-spacing: -0.03em; text-wrap: balance;">Stop posting screenshots. Post a link that proves it.</h1>
    <p style="margin: 0; font-size: 19px; line-height: 1.5; color: #a3abb8; max-width: 540px; text-wrap: pretty;">Connect a read-only Kalshi API key. AlphaMarket pulls your fills and settlements from the venue, reconciles them against your balance every night, and publishes a track record you didn't type.</p>
    <div style="display: flex; align-items: center; gap: 24px; margin-top: 8px;">
      {btn("Connect a read-only key")}
      <a href="#" style="display: inline-flex; align-items: center; gap: 8px; {MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; color: #a3abb8;">See a verified page {icon("arrow", 14)}</a>
    </div>
  </div>
  {strategy_card()}
</div>
<div style="display: flex; align-items: center; justify-content: space-between; gap: 24px; padding: 16px 48px; border-top: 1px solid #1e232a; border-bottom: 1px solid #1e232a; overflow: hidden;">{strip}</div>
<div style="display: flex; flex-direction: column; gap: 40px; padding: 96px 120px 0;">
  <div style="display: flex; align-items: flex-end; justify-content: space-between;">
    <h2 style="margin: 0; font-size: 40px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1;">Three steps. Nothing to trust.</h2>
    <span style="{MONO} font-size: 13px; color: #7d8694;">Full method on the methodology page</span>
  </div>
  <div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border: 1px solid #1e232a; border-radius: 2px; background: #101317;">{steps_html}</div>
</div>
<div style="display: grid; grid-template-columns: 400px 1fr; gap: 96px; padding: 112px 120px 0;">
  <div style="display: flex; flex-direction: column; gap: 20px;">
    <h2 style="margin: 0; font-size: 40px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1;">What a page proves, and what it doesn't.</h2>
    <p style="margin: 0; font-size: 16px; line-height: 1.55; color: #a3abb8; text-wrap: pretty;">A verified page is a narrow claim, made precisely. We'd rather you know the edges than believe the whole thing.</p>
  </div>
  <div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 48px;">
    <div style="display: flex; flex-direction: column;"><span style="{LABEL} padding-bottom: 8px; border-bottom: 1px solid #2a303a;">Verified</span>{li(proves, True)}</div>
    <div style="display: flex; flex-direction: column;"><span style="{LABEL} padding-bottom: 8px; border-bottom: 1px solid #2a303a;">Not verified</span>{li(doesnt, False)}</div>
  </div>
</div>
<div style="display: flex; align-items: center; justify-content: space-between; margin: 128px 120px 96px; padding: 48px; border: 1px solid #2a303a; border-radius: 2px; background: #101317;">
  <div style="display: flex; flex-direction: column; gap: 10px;">
    <span style="font-size: 32px; font-weight: 600; letter-spacing: -0.02em;">Your next P&amp;L post could be a link.</span>
    <span style="font-size: 15px; color: #a3abb8;">Free while we're in the first cohort. Disconnect any time; the key is erased.</span>
  </div>
  {btn("Connect a read-only key")}
</div>
{footer()}
</div>
''' + TAIL

# ---------------- Strategy page ----------------
def strategy():
    months = [("2026", ["·","·","·","·","+5.2%","+9.8%","−3.1%","+18.4%","·","·","·","·"])]
    mh = "".join(f'<th style="text-align: right; padding: 10px 8px; {LABEL} font-weight: 500; border-bottom: 1px solid #2a303a;">{m}</th>' for m in ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])
    rows = ""
    for y, cells in months:
        tds = "".join(f'<td style="text-align: right; padding: 12px 8px; {MONO} font-size: 13px; color: {"#7d8694" if c=="·" else ("#f0626b" if c.startswith("−") else "#1fc46a")}; border-bottom: 1px solid #1e232a;">{c}</td>' for c in cells)
        rows += f'<tr><td style="padding: 12px 8px; {MONO} font-size: 13px; color: #a3abb8; border-bottom: 1px solid #1e232a;">{y}</td>{tds}</tr>'
    settle = [("Denver high above 95°F on Aug 30?", "NO", "60", "−$9.40", False, "Aug 31"),
              ("Will the Aug jobs report exceed 150k?", "NO", "150", "+$27.75", True, "Aug 29"),
              ("Fed cuts by 25bp at the September meeting?", "YES", "200", "+$63.50", True, "Aug 28"),
              ("High temperature in NYC on Aug 22 above 91°F?", "YES", "80", "−$18.00", False, "Aug 23"),
              ("Will the July CPI print be above 0.3% m/m?", "NO", "120", "+$41.20", True, "Aug 12")]
    srows = "".join(
        f'<div style="display: grid; grid-template-columns: 1fr 64px 64px 96px; align-items: center; gap: 16px; padding: 14px 0; border-bottom: 1px solid #1e232a;">'
        f'<div style="display: flex; flex-direction: column; gap: 3px;"><span style="font-size: 14px; line-height: 1.3;">{q}</span><span style="{MONO} font-size: 11px; color: #7d8694;">settled {d}</span></div>'
        f'<span style="{MONO} font-size: 11px; letter-spacing: 0.08em; text-align: center; padding: 4px 0; border-radius: 2px; color: {"#1fc46a" if s=="YES" else "#f0626b"}; border: 1px solid {"#1fc46a" if s=="YES" else "#f0626b"};">{s}</span>'
        f'<span style="{MONO} font-size: 13px; color: #a3abb8; text-align: right;">×{n}</span>'
        f'<span style="{MONO} font-size: 14px; text-align: right; color: {"#1fc46a" if ok else "#f0626b"};">{p}</span></div>' for q, s, n, p, ok, d in settle)
    ylabels = "".join(f'<span style="{MONO} font-size: 11px; color: #7d8694;">{v}</span>' for v in ["$8.5k","$8.0k","$7.5k","$7.0k","$6.5k"])
    xlabels = "".join(f'<span style="{MONO} font-size: 11px; color: #7d8694;">{v}</span>' for v in ["May 6","Jun","Jul","Aug","Sep 1"])
    return HEAD + f'''<div style="width: 1440px; min-height: 1950px; background: #0b0d10; display: flex; flex-direction: column;">
{nav()}
<div style="display: flex; flex-direction: column; gap: 40px; padding: 48px 120px 0;">
  <div style="display: flex; align-items: flex-start; justify-content: space-between;">
    <div style="display: flex; flex-direction: column; gap: 14px;">
      <span style="{LABEL}">Strategy · Kalshi · production</span>
      <h1 style="margin: 0; font-size: 44px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.05;">Weather &amp; CPI mean reversion</h1>
      <span style="{MONO} font-size: 13px; color: #a3abb8;">by quietedge</span>
      <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 6px;">
        {badge("Platform-verified since 2026-05-06")}{badge("118 days verified")}{badge("Reconciled · matched venue balance")}{badge("macro", "neutral")}{badge("weather", "neutral")}
      </div>
    </div>
    <div style="display: flex; gap: 12px;">{btn("Copy link", "secondary")}</div>
  </div>
  <div style="display: grid; grid-template-columns: 360px 1fr; border: 1px solid #2a303a; border-radius: 2px; background: #101317;">
    <div style="display: flex; flex-direction: column; gap: 10px; padding: 28px 28px; border-right: 1px solid #1e232a;">
      <div style="display: flex; align-items: center; justify-content: space-between;"><span style="{LABEL}">Total return</span>
        <div style="display: flex; gap: 2px;">
          <span style="{MONO} font-size: 11px; letter-spacing: 0.06em; color: #7d8694; padding: 4px 8px;">30D</span>
          <span style="{MONO} font-size: 11px; letter-spacing: 0.06em; color: #7d8694; padding: 4px 8px;">90D</span>
          <span style="{MONO} font-size: 11px; letter-spacing: 0.06em; color: #e9ecf0; padding: 4px 8px; border: 1px solid #2a303a; border-radius: 2px;">ALL</span>
        </div></div>
      <span style="{MONO} font-size: 64px; font-weight: 500; color: #1fc46a; line-height: 1; letter-spacing: -0.03em;">+32.5%</span>
      <span style="{MONO} font-size: 13px; color: #a3abb8;">$6,600.00 → $8,746.80 · net of $312.44 fees</span>
    </div>
    <div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));">
      {stat("Max drawdown", "−14.3%", "bad")}{stat("Risk score", "6", "", "/10")}{stat("Sharpe", "1.84")}{stat("Sortino", "2.31")}
      {stat("Win rate", "58%")}{stat("Realized P&amp;L", "+$1,928.40", "good")}{stat("Settled trades", "214")}{stat("Open at cost", "$1,180.00")}
    </div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 12px; padding: 24px 28px 20px; border: 1px solid #1e232a; border-radius: 2px;">
    <div style="display: flex; align-items: baseline; justify-content: space-between;"><span style="{LABEL}">Equity · net of venue fees, open positions at cost</span><span style="{MONO} font-size: 12px; color: #7d8694;">daily, UTC close</span></div>
    <div style="display: grid; grid-template-columns: 44px 1fr; gap: 12px;">
      <div style="display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end; padding-bottom: 6px;">{ylabels}</div>
      <div style="display: flex; flex-direction: column; gap: 10px;">{equity_svg(640, 220, "main_equity")}<div style="display: flex; justify-content: space-between;">{xlabels}</div></div>
    </div>
    <div style="display: grid; grid-template-columns: 44px 1fr; gap: 12px; margin-top: 16px; padding-top: 16px; border-top: 1px solid #1e232a;">
      <div style="display: flex; flex-direction: column; justify-content: space-between; align-items: flex-end;"><span style="{MONO} font-size: 11px; color: #7d8694;">0%</span><span style="{MONO} font-size: 11px; color: #7d8694;">−15%</span></div>
      <div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">Drawdown from peak</span>{dd_svg(640, 120)}</div>
    </div>
  </div>
  <div style="display: grid; grid-template-columns: 1fr 480px; gap: 48px;">
    <div style="display: flex; flex-direction: column; gap: 16px;">
      <span style="{LABEL}">Monthly returns</span>
      <table style="width: 100%; border-collapse: collapse;"><tr><th style="text-align: left; padding: 10px 8px; {LABEL} font-weight: 500; border-bottom: 1px solid #2a303a;">Year</th>{mh}</tr>{rows}</table>
      <div style="display: flex; align-items: center; gap: 12px; margin-top: 24px; padding: 16px 20px; border: 1px solid #1e232a; border-radius: 2px;">
        {icon("check", 16, "#1fc46a")}<span style="font-size: 14px; color: #a3abb8;"><span style="color: #e9ecf0;">No behavioural flags.</span> Martingale sizing, stake spikes and single-series concentration are checked nightly.</span>
      </div>
      <div style="display: flex; flex-direction: column; gap: 12px; margin-top: 24px;">
        <div style="display: flex; align-items: center; gap: 10px;"><span style="{LABEL}">Trader's thesis</span>{badge("Unverified", "warn")}</div>
        <div style="padding: 20px 22px; border: 1px solid #e0a63a; border-radius: 2px; font-size: 15px; line-height: 1.55; color: #e9ecf0;">Fade extreme prints on weather and CPI contracts in the last 48 hours before settlement, when retail flow pushes YES past what the underlying forecast supports. Size flat, never add to losers.
          <div style="margin-top: 12px; {MONO} font-size: 11px; color: #7d8694;">Written by the trader. AlphaMarket does not verify anything in this box.</div></div>
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 4px;">
      <div style="display: flex; align-items: baseline; justify-content: space-between; padding-bottom: 8px; border-bottom: 1px solid #2a303a;"><span style="{LABEL}">Recent settlements</span><span style="{MONO} font-size: 12px; color: #7d8694;">5 of 214</span></div>
      {srows}
    </div>
  </div>
  <p style="margin: 8px 0 56px; {MONO} font-size: 12px; color: #7d8694; line-height: 1.6;">Every number on this page is computed by AlphaMarket from venue data pulled with a read-only key. Nothing here was entered by the trader. <a href="#" style="color: #a3abb8; text-decoration: underline;">What this proves and what it doesn't.</a></p>
</div>
{footer()}
</div>
''' + TAIL

# ---------------- Dashboard ----------------
def dashboard():
    recon = [("2026-09-01 02:14", "$8,746.8000", "$8,746.8000", "$0.0000", True), ("2026-08-31 02:11", "$8,702.3500", "$8,702.3500", "$0.0000", True),
             ("2026-08-30 02:13", "$8,690.1200", "$8,690.1200", "$0.0000", True), ("2026-08-29 02:10", "$8,655.9000", "$8,655.9000", "$0.0000", True),
             ("2026-08-28 02:12", "$8,611.4400", "$8,611.4400", "$0.0000", True)]
    rr = "".join(f'<tr><td style="padding: 11px 8px; {MONO} font-size: 13px; color: #a3abb8; border-bottom: 1px solid #1e232a;">{a}</td>'
                 f'<td style="padding: 11px 8px; {MONO} font-size: 13px; text-align: right; border-bottom: 1px solid #1e232a;">{b}</td>'
                 f'<td style="padding: 11px 8px; {MONO} font-size: 13px; text-align: right; border-bottom: 1px solid #1e232a;">{c}</td>'
                 f'<td style="padding: 11px 8px; {MONO} font-size: 13px; text-align: right; color: #7d8694; border-bottom: 1px solid #1e232a;">{d}</td>'
                 f'<td style="padding: 11px 8px; border-bottom: 1px solid #1e232a;"><span style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 12px; color: #1fc46a;">{icon("check", 14, "#1fc46a")}match</span></td></tr>' for a, b, c, d, ok in recon)
    th = lambda t, r=False: f'<th style="text-align: {"right" if r else "left"}; padding: 8px; {LABEL} font-weight: 500; border-bottom: 1px solid #2a303a;">{t}</th>'
    return HEAD + f'''<div style="width: 1440px; min-height: 1560px; background: #0b0d10; display: flex; flex-direction: column;">
{nav("dashboard", True)}
<div style="display: flex; flex-direction: column; gap: 32px; padding: 48px 120px 64px;">
  <div style="display: flex; align-items: flex-end; justify-content: space-between;">
    <div style="display: flex; flex-direction: column; gap: 8px;"><span style="{LABEL}">Dashboard</span><h1 style="margin: 0; font-size: 40px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.05;">quietedge</h1></div>
    {btn(icon("plus", 14, "#0b0d10") + "Connect another account")}
  </div>
  <div style="display: flex; flex-direction: column; border: 1px solid #2a303a; border-radius: 2px; background: #101317;">
    <div style="display: flex; align-items: center; justify-content: space-between; padding: 22px 28px; border-bottom: 1px solid #1e232a;">
      <div style="display: flex; flex-direction: column; gap: 6px;">
        <div style="display: flex; align-items: center; gap: 14px;"><span style="font-size: 22px; font-weight: 600;">Weather &amp; CPI mean reversion</span><span style="{MONO} font-size: 12px; color: #7d8694;">Kalshi · prod</span></div>
        <div style="display: flex; align-items: center; gap: 8px;">{badge("Connected")}{badge("Read-only verified")}{badge("Active")}<a href="#" style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 12px; color: #a3abb8; margin-left: 8px;">Public page {icon("arrow", 12)}</a></div>
      </div>
      <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 4px; {MONO} font-size: 12px; color: #7d8694;"><span>Last sync <span style="color: #a3abb8;">today 02:14 UTC</span></span><span>Verified since <span style="color: #a3abb8;">2026-05-06</span></span></div>
    </div>
    <div style="display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); border-bottom: 1px solid #1e232a;">
      {stat("Equity", "$8,746.80")}{stat("Total return", "+32.5%", "good")}{stat("Max drawdown", "−14.3%", "bad")}{stat("Risk score", "6", "", "/10")}{stat("Win rate", "58%")}{stat("Track", "118", "", "days")}
    </div>
    <div style="display: flex; flex-direction: column; gap: 12px; padding: 24px 28px;">
      <div style="display: flex; align-items: baseline; justify-content: space-between;"><span style="{LABEL}">Nightly reconciliation</span><span style="{MONO} font-size: 12px; color: #7d8694;">1,204 fills · 214 settlements · stats as of 2026-09-01</span></div>
      <table style="width: 100%; border-collapse: collapse;"><tr>{th("Run (UTC)")}{th("Venue balance", True)}{th("Computed cash", True)}{th("Divergence", True)}{th("Result")}</tr>{rr}</table>
    </div>
    <div style="display: flex; align-items: center; gap: 12px; padding: 20px 28px; border-top: 1px solid #1e232a;">
      {btn(icon("sync", 14) + "Sync now", "secondary")}{btn("Reconcile now", "secondary")}<span style="flex-grow: 1;"></span>{btn("Disconnect and erase key", "danger")}
    </div>
  </div>
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 32px;">
    <div style="display: flex; flex-direction: column; gap: 20px; padding: 28px; border: 1px solid #1e232a; border-radius: 2px;">
      <div style="display: flex; flex-direction: column; gap: 6px;"><span style="font-size: 18px; font-weight: 600;">Public listing</span><span style="font-size: 14px; color: #7d8694;">Name and tags are shown as-is. The thesis is shown boxed and labelled unverified.</span></div>
      {input_("Name", value="Weather &amp; CPI mean reversion")}
      {input_("Thesis", value="Fade extreme prints on weather and CPI contracts in the last 48 hours before settlement, when retail flow pushes YES past what the underlying forecast supports. Size flat, never add to losers.", textarea=True, note="Shown publicly, labelled unverified.")}
      {input_("Style tags", value="macro, weather", note="Comma-separated.")}
      <div style="display: flex; align-items: center; justify-content: space-between; padding-top: 8px;">
        <label style="display: flex; align-items: center; gap: 12px; font-size: 14px;"><span style="display: inline-flex; align-items: center; width: 36px; height: 20px; padding: 2px; background: #1fc46a; border-radius: 2px;"><span style="width: 16px; height: 16px; background: #0b0d10; margin-left: auto;"></span></span>Public page enabled</label>
        {btn("Save listing")}
      </div>
    </div>
    <div style="display: flex; flex-direction: column; gap: 16px; padding: 28px; border: 1px solid #1e232a; border-radius: 2px;">
      <span style="font-size: 18px; font-weight: 600;">What we hold for this account</span>
      <div style="display: flex; flex-direction: column;">
        <div style="display: flex; align-items: flex-start; gap: 14px; padding: 14px 0; border-bottom: 1px solid #1e232a;">{icon("lock", 18, "#a3abb8")}<div style="display: flex; flex-direction: column; gap: 3px;"><span style="font-size: 15px;">Kalshi API key, read-only scope</span><span style="{MONO} font-size: 12px; color: #7d8694;">encrypted at rest · scope re-checked every sync · erased on disconnect</span></div></div>
        <div style="display: flex; align-items: flex-start; gap: 14px; padding: 14px 0; border-bottom: 1px solid #1e232a;">{icon("eye", 18, "#a3abb8")}<div style="display: flex; flex-direction: column; gap: 3px;"><span style="font-size: 15px;">Fills, settlements, balance snapshots</span><span style="{MONO} font-size: 12px; color: #7d8694;">append-only · never edited · corrections are new rows</span></div></div>
        <div style="display: flex; align-items: flex-start; gap: 14px; padding: 14px 0;">{icon("x", 18, "#a3abb8")}<div style="display: flex; flex-direction: column; gap: 3px;"><span style="font-size: 15px;">Nothing that can move money</span><span style="{MONO} font-size: 12px; color: #7d8694;">no orders, no cancels, no deposits, no withdrawals</span></div></div>
      </div>
    </div>
  </div>
</div>
{footer()}
</div>
''' + TAIL

# ---------------- Connect flow ----------------
def connect():
    steps = [("1", "Create a read-only key at Kalshi", True), ("2", "Paste it here", True), ("3", "Scope check", False), ("4", "Import and verify", False)]
    st = "".join(f'<div style="display: flex; align-items: center; gap: 12px; padding: 12px 0; border-bottom: 1px solid #1e232a;">'
                 f'<span style="display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; {MONO} font-size: 12px; border-radius: 2px; color: {"#0b0d10" if on else "#7d8694"}; background: {"#1fc46a" if on else "transparent"}; border: 1px solid {"#1fc46a" if on else "#2a303a"};">{n}</span>'
                 f'<span style="font-size: 14px; color: {"#e9ecf0" if on else "#7d8694"};">{t}</span></div>' for n, t, on in steps)
    checks = [("Key scope is read-only", "A key with trading permission is rejected before anything is stored."),
              ("Key is encrypted with a server-side vault key", "Stored ciphertext only. Never logged, never returned by any page or API."),
              ("First import from Kalshi", "Fills and settlements are pulled as far back as the venue returns them. Your verified window starts today, not earlier."),
              ("First balance reconciliation tonight", "The page goes public only once the ledger matches your venue balance.")]
    ch = "".join(f'<div style="display: flex; align-items: flex-start; gap: 14px; padding: 16px 0; border-bottom: 1px solid #1e232a;">{icon("check", 18, "#1fc46a")}<div style="display: flex; flex-direction: column; gap: 4px;"><span style="font-size: 15px;">{t}</span><span style="font-size: 13px; line-height: 1.5; color: #7d8694;">{d}</span></div></div>' for t, d in checks)
    return HEAD + f'''<div style="width: 1440px; min-height: 1100px; background: #0b0d10; display: flex; flex-direction: column;">
{nav("dashboard", True)}
<div style="display: grid; grid-template-columns: 220px 560px 1fr; gap: 48px; padding: 56px 120px 72px;">
  <div style="display: flex; flex-direction: column; gap: 20px;">
    <span style="{LABEL}">Connect Kalshi</span>
    <div style="display: flex; flex-direction: column;">{st}</div>
    <p style="margin: 8px 0 0; font-size: 13px; line-height: 1.55; color: #7d8694;">In Kalshi: Account → API keys → New key. Choose <span style="color: #a3abb8;">read-only</span>. Download the PEM file once; Kalshi won't show it again.</p>
  </div>
  <div style="display: flex; flex-direction: column; gap: 28px;">
    <div style="display: flex; flex-direction: column; gap: 10px;">
      <h1 style="margin: 0; font-size: 40px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.05;">Paste a read-only key.</h1>
      <p style="margin: 0; font-size: 16px; line-height: 1.5; color: #a3abb8; text-wrap: pretty;">We check the scope before we store it. A key that can place orders is refused, and you'll be asked to make a read-only one instead.</p>
    </div>
    <div style="display: flex; flex-direction: column; gap: 22px; padding: 28px; border: 1px solid #2a303a; border-radius: 2px; background: #101317;">
      {input_("Strategy name", placeholder="Shown on your public page", value="Weather &amp; CPI mean reversion")}
      {input_("API key id", placeholder="From Kalshi → API keys", value="7c1e0d4a-3b9f-4d2e-a1c5-9e8f0b2d6a17", mono=True)}
      {input_("Private key (PEM)", placeholder="Paste the contents of the .pem file", value="-----BEGIN RSA PRIVATE KEY-----&#10;MIIEowIBAAKCAQEAv3…&#10;-----END RSA PRIVATE KEY-----", mono=True, textarea=True, note="Encrypted before it touches the database. Erased when you disconnect.")}
      <div style="display: flex; align-items: center; justify-content: space-between; padding-top: 4px;">
        <span style="display: inline-flex; align-items: center; gap: 8px; {MONO} font-size: 12px; color: #7d8694;">{icon("lock", 14, "#7d8694")}Environment: production</span>
        {btn("Check scope and import")}
      </div>
    </div>
  </div>
  <div style="display: flex; flex-direction: column; gap: 12px; padding-top: 4px;">
    <span style="{LABEL} padding-bottom: 8px; border-bottom: 1px solid #2a303a;">What happens next</span>
    {ch}
  </div>
</div>
<div style="flex-grow: 1;"></div>
{footer()}
</div>
''' + TAIL

# ---------------- Explore ----------------
def explore():
    rows = [("s6", "Fed path, both directions", "rateswatch", ["macro"], 40.7, -7.8, 5, 134, True),
            ("s3", "Long-tail sports underdogs", "tailrisk", ["sports"], 36.5, -15.5, 8, 92, True),
            ("s1", "Weather &amp; CPI mean reversion", "quietedge", ["macro","weather"], 32.5, -14.3, 6, 118, True),
            ("s5", "Election polling arbitrage", "median_voter", ["politics"], 29.8, -4.3, 3, 201, True),
            ("s4", "Daily temperature spreads", "isobar", ["weather"], -8.1, -17.0, 7, 61, False),
            ("s2", "Earnings-day binaries", "printday", ["finance"], -11.6, -19.0, 9, 45, True)]
    rr = ""
    for i, (k, name, h, tags, ret, dd, risk, days, rec) in enumerate(rows):
        tg = "".join(badge(t, "neutral") for t in tags)
        good = ret >= 0
        rr += (f'<div style="display: grid; grid-template-columns: 40px 1fr 120px 100px 90px 70px 90px 180px; align-items: center; gap: 20px; padding: 18px 24px; border-bottom: 1px solid #1e232a;">'
               f'<span style="{MONO} font-size: 13px; color: #7d8694;">{i+1:02d}</span>'
               f'<div style="display: flex; flex-direction: column; gap: 6px;"><a href="#" style="font-size: 16px; font-weight: 500;">{name}</a><div style="display: flex; align-items: center; gap: 8px;"><span style="{MONO} font-size: 12px; color: #7d8694;">{h}</span>{tg}</div></div>'
               f'{spark(k+"_spark", good)}'
               f'<span style="{MONO} font-size: 18px; text-align: right; color: {"#1fc46a" if good else "#f0626b"};">{"+" if good else "−"}{abs(ret):.1f}%</span>'
               f'<span style="{MONO} font-size: 14px; text-align: right; color: #a3abb8;">−{abs(dd):.1f}%</span>'
               f'<span style="{MONO} font-size: 14px; text-align: right;">{risk}<span style="color: #7d8694;">/10</span></span>'
               f'<span style="{MONO} font-size: 14px; text-align: right;">{days}<span style="color: #7d8694;"> d</span></span>'
               f'<span style="display: inline-flex; align-items: center; gap: 6px; {MONO} font-size: 12px; color: {"#a3abb8" if rec else "#e0a63a"}; justify-content: flex-end;">{icon("check", 14, "#1fc46a") if rec else icon("x", 14, "#e0a63a")}{"matched last night" if rec else "diverged · quarantined"}</span></div>')
    hdr = lambda t, r=False: f'<span style="{LABEL} text-align: {"right" if r else "left"};">{t}</span>'
    chips = "".join(f'<a href="#" style="{MONO} font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; padding: 9px 14px; border-radius: 2px; color: {"#0b0d10" if i==0 else "#a3abb8"}; background: {"#f2f4f6" if i==0 else "transparent"}; border: 1px solid {"#f2f4f6" if i==0 else "#2a303a"};">{c}</a>' for i, c in enumerate(["All", "Macro", "Weather", "Politics", "Sports", "Finance"]))
    return HEAD + f'''<div style="width: 1440px; min-height: 1200px; background: #0b0d10; display: flex; flex-direction: column;">
{nav("explore")}
<div style="display: flex; flex-direction: column; gap: 28px; padding: 48px 120px 64px;">
  <div style="display: flex; align-items: flex-end; justify-content: space-between;">
    <div style="display: flex; flex-direction: column; gap: 10px;">
      <span style="{LABEL}">Explore</span>
      <h1 style="margin: 0; font-size: 44px; font-weight: 600; letter-spacing: -0.02em; line-height: 1.05;">Verified strategies</h1>
      <span style="font-size: 15px; color: #a3abb8;">Every row is computed from venue data and reconciled nightly. Sorted by total return since verification.</span>
    </div>
    <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 4px;"><span style="{MONO} font-size: 32px; font-weight: 500; line-height: 1;">6</span><span style="{LABEL}">public pages · Kalshi</span></div>
  </div>
  <div style="display: flex; align-items: center; justify-content: space-between; padding: 16px 0; border-top: 1px solid #1e232a; border-bottom: 1px solid #1e232a;">
    <div style="display: flex; gap: 8px;">{chips}</div>
    <div style="display: flex; align-items: center; gap: 20px;">
      <span style="{MONO} font-size: 12px; color: #7d8694;">Sort <span style="color: #e9ecf0;">Return</span> · Drawdown · Days verified</span>
      <label style="display: flex; align-items: center; gap: 10px; {MONO} font-size: 12px; color: #a3abb8;"><span style="display: inline-flex; align-items: center; width: 32px; height: 18px; padding: 2px; background: #1fc46a; border: 1px solid #1fc46a; border-radius: 2px;"><span style="width: 12px; height: 12px; background: #0b0d10; margin-left: auto;"></span></span>Show incubating</label>
    </div>
  </div>
  <div style="display: flex; flex-direction: column; border: 1px solid #2a303a; border-radius: 2px; background: #101317;">
    <div style="display: grid; grid-template-columns: 40px 1fr 120px 100px 90px 70px 90px 180px; gap: 20px; padding: 12px 24px; border-bottom: 1px solid #2a303a;">
      {hdr("#")}{hdr("Strategy")}{hdr("60 days")}{hdr("Return", True)}{hdr("Max DD", True)}{hdr("Risk", True)}{hdr("Verified", True)}{hdr("Reconciliation", True)}
    </div>
    {rr}
  </div>
  <p style="margin: 0; {MONO} font-size: 12px; color: #7d8694; line-height: 1.6;">Strategies with fewer than 90 verified days are incubating and hidden unless you show them. A diverged reconciliation quarantines the page until it matches again. <a href="#" style="color: #a3abb8; text-decoration: underline;">How ranking works.</a></p>
</div>
<div style="flex-grow: 1;"></div>
{footer()}
</div>
''' + TAIL

for name, fn in [("Main", landing), ("Strategy", strategy), ("Dashboard", dashboard), ("Connect", connect), ("Explore", explore)]:
    open(f"{name}.dc.html", "w").write(fn())
    print(name, "ok")
