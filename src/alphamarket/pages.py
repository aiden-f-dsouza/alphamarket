"""HTML surface: landing (investor and creator sides), marketplace, public
strategy page, creator dashboard, connect page, trust pages (verification,
transparency, research, security), invite. Jinja templates, plain forms,
charts as inline SVG built server-side (`charts.py`); no client-side scripts.

Everything numeric shown to real users comes from strategy_stats /
equity_snapshots (platform-computed, invariant #5). With DEMO_MODE on, pages
the MVP cannot compute yet (tailing, portfolio, feed, creator profiles) and
the marketplace are padded from `demo.py`, always under a "Sample data"
banner; those routes 404 when demo mode is off, and preflight refuses to
launch with it on.
"""

from pathlib import Path
from statistics import median

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import auth, demo, mfa, ratelimit
from alphamarket.analytics import latest_stats
from alphamarket.charts import drawdown_window, polyline, ticks
from alphamarket.config import get_settings
from alphamarket.connectors.registry import PROFILES, get_profile
from alphamarket.db import get_db
from alphamarket.ingest import sync_account
from alphamarket.marks import sigil, tag_color
from alphamarket.models import (
    EquitySnapshot,
    Strategy,
    StrategyStatus,
    User,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.public import (
    all_tags,
    is_visible,
    list_visible,
    page_data,
    visible_strategy_or_404,
)
from alphamarket.reconcile import reconcilable, reconcile_account
from alphamarket.strategies import ListingIn, update_listing
from alphamarket.venues import (
    ConnectIn,
    _owned_account,
    claim_account,
    connect_account,
    verification_tier,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
DOCS = Path(__file__).resolve().parents[2] / "docs"
templates.env.globals.update(sigil=sigil, tag_color=tag_color)


def _user(request: Request, db: Session) -> User | None:
    return auth.optional_user(request, db)  # DB session cookie (sessions.py)


def _demo() -> bool:
    return get_settings().demo_mode


def _demo_or_404() -> None:
    if not _demo():
        raise HTTPException(status_code=404)


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("settings", get_settings())
    ctx.setdefault("demo", False)
    return templates.TemplateResponse(request, name, ctx)


# ---- landing / auth ----------------------------------------------------------


def _featured(db: Session) -> dict | None:
    """The landing card is a real public page (the top row of the marketplace,
    incubating included) whenever one exists; sample numbers only in demo
    mode, and then under the sample banner (invariant #5)."""
    rows = list_visible(db, include_incubating=True)
    if rows:
        return rows[0]
    return demo.featured() if _demo() else None


def _marketplace_rows(db: Session, include_incubating: bool, tag: str | None) -> list[dict]:
    """Real rows first (sorted by return, as `list_visible` does), then sample
    rows in demo mode. A row's `demo` key drives the sample banner."""
    rows = list_visible(db, include_incubating=include_incubating, tag=tag)
    if _demo():
        for r in demo.marketplace_rows():
            if tag and tag not in r["tags"]:
                continue
            if r["incubating"] and not include_incubating:
                continue
            rows.append(r)
    return rows


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    """The landing page is public for everyone; a logged-in visitor sees a
    dashboard link where the sign-up call to action would be."""
    featured = _featured(db)
    top = _marketplace_rows(db, include_incubating=True, tag=None)[:3]
    sample = bool(featured and featured.get("demo")) or any(r.get("demo") for r in top)
    return _render(
        request, "index.html", featured=featured, top=top, user=_user(request, db), demo=sample
    )


def _auth(request: Request, db: Session, mode: str, error: str | None = None):
    """Login and sign-up get their own pages so an error is shown next to the
    form that produced it, not at the bottom of the landing page. A visitor who
    is already logged in is sent to the dashboard instead of an empty form; a
    failed submission always shows its error."""
    if error is None and _user(request, db) is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return _render(request, "auth.html", mode=mode, error=error, user=None)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    return _auth(request, db, "login")


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, db: Session = Depends(get_db)):
    return _auth(request, db, "signup")


@router.get("/creators", response_class=HTMLResponse)
def creators(request: Request, db: Session = Depends(get_db)):
    return _render(request, "creators.html", user=_user(request, db))


@router.get("/marketplace", response_class=HTMLResponse)
def marketplace(
    request: Request,
    incubating: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    view: str = "cards",
    db: Session = Depends(get_db),
):
    """`view=cards` (default) is for browsing; `view=table` is the comparison
    table with every column. Same rows, same order, either way."""
    rows = _marketplace_rows(db, include_incubating=incubating == "1", tag=tag or None)
    q = (q or "").strip()
    if q:
        needle = q.lower()
        rows = [
            r for r in rows if needle in r["name"].lower() or needle in r["display_name"].lower()
        ]
    return _render(
        request,
        "marketplace.html",
        user=_user(request, db),
        rows=rows,
        tags=all_tags(_marketplace_rows(db, include_incubating=True, tag=None)),
        tag=tag or "",
        q=q,
        view="table" if view == "table" else "cards",
        incubating=incubating == "1",
        demo=any(r.get("demo") for r in rows),
    )


@router.get("/explore")
def explore(request: Request):
    """Old name for the marketplace; links in the wild keep working."""
    qs = request.url.query
    return RedirectResponse("/marketplace" + (f"?{qs}" if qs else ""), status_code=307)


@router.post("/signup-form")
def signup_form(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    display_name: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        auth.signup(
            auth.SignupIn(email=email, password=password, display_name=display_name), request, db
        )
    except HTTPException as exc:
        return _auth(request, db, "signup", exc.detail)
    except ValueError as exc:  # pydantic validation
        return _auth(request, db, "signup", str(exc))
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/login-form")
def login_form(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        result = auth.login(auth.LoginIn(email=email, password=password), request, db)
    except HTTPException as exc:
        if exc.status_code == 429:
            resp = _auth(request, db, "login", "Too many attempts. Wait a minute and try again.")
            resp.status_code = 429
            return resp
        return _auth(request, db, "login", "Invalid credentials")
    except ValueError:
        return _auth(request, db, "login", "Invalid credentials")
    if result.get("mfa_required"):
        return RedirectResponse("/login/2fa", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout-form")
def logout_form(request: Request, db: Session = Depends(get_db)):
    auth.logout_current(request, db)
    return RedirectResponse("/", status_code=303)


# ---- dashboard ------------------------------------------------------------------


def _dashboard_ctx(db: Session, user: User) -> dict:
    accounts = db.scalars(select(VenueAccount).where(VenueAccount.user_id == user.id)).all()
    rows = []
    for account in accounts:
        strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
        snapshots = (
            db.scalars(
                select(EquitySnapshot)
                .where(EquitySnapshot.strategy_id == strategy.id)
                .order_by(EquitySnapshot.as_of.desc())
                .limit(5)
            ).all()
            if strategy
            else []
        )
        rows.append(
            {
                "account": account,
                "strategy": strategy,
                "stats": latest_stats(db, strategy.id) if strategy else None,
                "snapshots": snapshots,
                "visible": is_visible(strategy, account) if strategy else False,
                "profile": get_profile(account.venue),
                "tier": verification_tier(account),
                "reconcilable": reconcilable(account),
            }
        )
    return {
        "user": user,
        "rows": rows,
        "profiles": list(PROFILES.values()),
        "totp_on": auth.totp_enabled(db, user),
        "email_verified": user.email_verified_at is not None,
    }


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "dashboard.html", error=None, **_dashboard_ctx(db, user))


def _connect_ctx(venue: str) -> dict:
    if venue not in PROFILES:
        raise HTTPException(status_code=404, detail="Unknown venue")
    profile = get_profile(venue)
    return {
        "profile": profile,
        "profiles": list(PROFILES.values()),
        "venue_env": get_settings().venue_env(venue),
    }


@router.get("/dashboard/connect", response_class=HTMLResponse)
def connect_page(request: Request, venue: str = "kalshi", db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _render(
        request,
        "connect.html",
        user=user,
        error=None,
        gate=mfa.connect_block(request, db, user),
        **_connect_ctx(venue),
    )


@router.post("/dashboard/connect")
def connect_form(
    request: Request,
    venue: str = Form("kalshi"),
    key_id: str = Form(),
    secret: str | None = Form(None),
    strategy_name: str = Form(),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    ctx = _connect_ctx(venue)
    gate = mfa.connect_block(request, db, user)
    if gate is not None:
        resp = _render(request, "connect.html", user=user, error=gate[1], gate=gate, **ctx)
        resp.status_code = 403
        return resp
    try:
        ratelimit.check(request, "venues_user", user.id)
        connect_account(
            db,
            user,
            ConnectIn(venue=venue, key_id=key_id, secret=secret, strategy_name=strategy_name),
        )
    except HTTPException as exc:
        return _render(request, "connect.html", user=user, error=exc.detail, gate=None, **ctx)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/strategy/{strategy_id}")
def edit_listing_form(
    request: Request,
    strategy_id: int,
    name: str = Form(),
    description: str = Form(""),
    style_tags: str = Form(""),
    public: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    update_listing(
        db,
        user,
        strategy_id,
        ListingIn(name=name, description=description, style_tags=style_tags, public=public == "on"),
    )
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/dashboard/accounts/{account_id}/{action}")
def account_action_form(
    request: Request, account_id: int, action: str, db: Session = Depends(get_db)
):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    account = _owned_account(db, account_id, user)
    error = None
    try:
        if action == "sync":
            sync_account(db, account)
        elif action == "reconcile":
            reconcile_account(db, account)
        elif action == "claim":
            ratelimit.check(request, "venues_user", user.id)
            claim_account(db, user, account.id)
        elif action == "disconnect":
            mfa.ensure_step_up(request, db, user)
            account.encrypted_private_key = None
            account.status = VenueAccountStatus.DISCONNECTED
            db.commit()
        else:
            raise HTTPException(status_code=404)
    except HTTPException as exc:
        if action not in ("claim", "disconnect"):
            raise
        error = str(exc.detail)
    except Exception as exc:  # recorded on the account row by sync/reconcile
        error = f"{action} failed: {type(exc).__name__}"
    if error:
        return _render(request, "dashboard.html", error=error, **_dashboard_ctx(db, user))
    return RedirectResponse("/dashboard", status_code=303)


# ---- public strategy page ------------------------------------------------------


CHART_W, EQUITY_H, DRAWDOWN_H = 900, 260, 110


def _charts(stats: dict | None, benchmarks: list | None = None) -> dict | None:
    """SVG geometry for the equity and drawdown curves. Benchmarks (sample
    pages only) share the equity axis so the lines are comparable."""
    if not stats or not stats["equity_curve"]:
        return None
    equity = [e for _, e in stats["equity_curve"]]
    dates = [d for d, _ in stats["equity_curve"]]
    dd = [-x for _, x in stats["drawdown_curve"]]  # drawdown_series is positive; plot below 0
    dd_floor = min(min(dd), -0.01)
    lo, hi = min(equity), max(equity)
    for _, values, _ in benchmarks or []:
        lo, hi = min(lo, min(values)), max(hi, max(values))
    n = len(dates)
    x_idx = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1})
    a, b = drawdown_window(equity)
    return {
        "w": CHART_W,
        "equity_h": EQUITY_H,
        "drawdown_h": DRAWDOWN_H,
        "equity": polyline(equity, CHART_W, EQUITY_H, lo=lo, hi=hi),
        "equity_ticks": ticks(lo, hi, 5),
        "benchmarks": [
            (label, polyline(values, CHART_W, EQUITY_H, lo=lo, hi=hi), dashed)
            for label, values, dashed in benchmarks or []
        ],
        "drawdown": polyline(dd, CHART_W, DRAWDOWN_H, lo=dd_floor, hi=0.0),
        "drawdown_floor": dd_floor,
        "x_labels": [dates[i] for i in x_idx],
        "up": equity[-1] >= equity[0],
        "dd_x": (CHART_W * a / max(n - 1, 1), CHART_W * b / max(n - 1, 1)),
        "series": [[d, round(e, 2)] for d, e in zip(dates, equity, strict=True)],
    }


@router.get("/s/{slug}", response_class=HTMLResponse)
def strategy_page(request: Request, slug: str, db: Session = Depends(get_db)):
    try:
        strategy = visible_strategy_or_404(db, slug)
    except HTTPException:
        if not (_demo() and demo.has_strategy(slug)):
            raise
        page = demo.strategy_page(slug)
        return _render(
            request,
            "strategy.html",
            user=_user(request, db),
            page=page,
            charts=_charts(page["stats"], page["benchmarks"]),
            demo=True,
        )
    page = page_data(db, strategy)
    return _render(
        request, "strategy.html", user=_user(request, db), page=page, charts=_charts(page["stats"])
    )


# ---- demo-only investor surface (tailing, portfolio, feed, creator profiles) ----


@router.get("/s/{slug}/tail", response_class=HTMLResponse)
def tail_page(request: Request, slug: str, db: Session = Depends(get_db)):
    _demo_or_404()
    t = demo.tail_page(slug)
    if t is None:
        raise HTTPException(status_code=404)
    return _render(request, "tail.html", user=_user(request, db), t=t, demo=True)


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio(request: Request, db: Session = Depends(get_db)):
    _demo_or_404()
    return _render(
        request, "portfolio.html", user=_user(request, db), p=demo.portfolio(), demo=True
    )


@router.get("/u/{handle}", response_class=HTMLResponse)
def creator_profile(request: Request, handle: str, db: Session = Depends(get_db)):
    _demo_or_404()
    c = demo.creator(handle)
    if c is None:
        raise HTTPException(status_code=404)
    return _render(request, "creator.html", user=_user(request, db), c=c, demo=True)


@router.get("/feed", response_class=HTMLResponse)
def feed(request: Request, db: Session = Depends(get_db)):
    _demo_or_404()
    return _render(request, "feed.html", user=_user(request, db), f=demo.feed(), demo=True)


# ---- invite ---------------------------------------------------------------------


@router.get("/invite", response_class=HTMLResponse)
def invite(request: Request, kind: str = "creator", db: Session = Depends(get_db)):
    return _render(request, "invite.html", user=_user(request, db), notice=None, kind=kind)


@router.post("/invite", response_class=HTMLResponse)
def invite_form(request: Request, kind: str = Form("creator"), db: Session = Depends(get_db)):
    """Stub: nothing is sent yet. The form re-renders with an honest notice."""
    return _render(
        request,
        "invite.html",
        user=_user(request, db),
        kind=kind,
        notice=(
            "Invitations open with the first cohort. "
            "Nothing was sent; share the link above in the meantime."
        ),
    )


# ---- trust pages ----------------------------------------------------------------


def _doc(request: Request, db: Session, name: str, title: str, nav_key: str) -> HTMLResponse:
    md = (DOCS / f"{name}.md").read_text()
    body = markdown.markdown(md, extensions=["tables", "fenced_code"])
    return _render(
        request,
        "methodology.html",
        user=_user(request, db),
        body=body,
        title=title,
        nav_key=nav_key,
    )


@router.get("/verification", response_class=HTMLResponse)
def verification(request: Request, db: Session = Depends(get_db)):
    return _doc(request, db, "methodology", "Verification", "verification")


@router.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request, db: Session = Depends(get_db)):
    return _doc(request, db, "methodology", "Verification", "verification")


@router.get("/security", response_class=HTMLResponse)
def security(request: Request, db: Session = Depends(get_db)):
    return _doc(request, db, "security", "Security & privacy", "security")


def _transparency(db: Session) -> dict:
    """Platform-wide figures from every visible strategy with stats, plus the
    count of quarantined/delisted records. Sample figures only in demo mode."""
    if _demo():
        return demo.transparency()
    rows = list_visible(db, include_incubating=True)
    removed = db.scalar(
        select(Strategy.id)
        .where(Strategy.status.in_([StrategyStatus.QUARANTINED, StrategyStatus.DELISTED]))
        .limit(1)
    )
    n_removed = (
        len(
            db.scalars(
                select(Strategy).where(
                    Strategy.status.in_([StrategyStatus.QUARANTINED, StrategyStatus.DELISTED])
                )
            ).all()
        )
        if removed
        else 0
    )
    return {
        "demo": False,
        "ever": len(rows) + n_removed,
        "losing": sum(1 for r in rows if r["total_return"] < 0),
        "median_return": median(r["total_return"] for r in rows) if rows else None,
        "median_dd": median(r["max_drawdown"] for r in rows) if rows else None,
        "removed": n_removed,
        "histogram": None,
    }


@router.get("/transparency", response_class=HTMLResponse)
def transparency(request: Request, db: Session = Depends(get_db)):
    t = _transparency(db)
    return _render(request, "transparency.html", user=_user(request, db), t=t, demo=t["demo"])


@router.get("/research", response_class=HTMLResponse)
def research(request: Request, db: Session = Depends(get_db)):
    return _render(request, "research.html", user=_user(request, db))
