"""HTML surface: landing (investor and creator sides), marketplace, public
strategy page, creator dashboard, connect page, trust pages (verification,
transparency, research, security), invite. Jinja templates, plain forms,
charts as inline SVG built server-side (`charts.py`); no client-side scripts.

Everything numeric shown to real users comes from strategy_stats /
equity_snapshots (platform-computed, invariant #5). Paper tailing, following
and /portfolio work for real users from plain forms (w3-tail-ui). With
DEMO_MODE on, pages the MVP cannot compute yet (feed, creator profiles) and
the marketplace are padded from `demo.py`, always under a "Sample data"
banner; those routes 404 when demo mode is off, and preflight refuses to
launch with it on.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from statistics import median
from urllib.parse import quote

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import auth, credentials, demo, mfa, ratelimit
from alphamarket.account_status import account_status
from alphamarket.analytics import latest_stats
from alphamarket.charts import drawdown_window, polyline, ticks
from alphamarket.config import get_settings
from alphamarket.connectors.registry import PROFILES, get_profile
from alphamarket.consent import STALE_DETAIL as STALE_CONSENT
from alphamarket.consent import consent_text
from alphamarket.db import get_db
from alphamarket.follows import (
    feed_items,
    follow_strategy,
    follower_count,
    is_following,
    unfollow_strategy,
)
from alphamarket.marks import sigil, tag_color
from alphamarket.models import (
    Allocation,
    EquitySnapshot,
    Follow,
    Instrument,
    MirrorAssetFill,
    MirrorFill,
    MirrorSettlement,
    Strategy,
    StrategyKind,
    StrategyStatus,
    User,
    VenueAccount,
)
from alphamarket.money import format_atoms
from alphamarket.numfmt import fmt_num
from alphamarket.portfolio import portfolio_summary
from alphamarket.public import (
    ASSET_CLASS_LABELS,
    ASSET_CLASS_ORDER,
    INCUBATION_DAYS,
    all_tags,
    instrument_label,
    is_visible,
    list_visible,
    page_data,
    publicly_visible,
    venues_in,
    verification_tier,
    visible_strategy_or_404,
)
from alphamarket.reconcile import reconcilable, reconcile_account
from alphamarket.stats import drawdown_series, worst_periods
from alphamarket.strategies import ListingIn, update_listing
from alphamarket.tail_terms import (
    DEFAULT_DRAWDOWN_BPS,
    DRAWDOWN_CHOICES_BPS,
    PRICING_ROWS,
    paper_terms,
    tailing_card,
)
from alphamarket.tails import (
    MAX_ALLOCATION_MICROS,
    MIN_ALLOCATION_MICROS,
    AllocationIn,
    active_allocation,
    active_tail_count,
    create_allocation,
    owned_allocation,
    stop_by_user,
)
from alphamarket.venue_limits import get_web_limiter
from alphamarket.venues import (
    ConnectIn,
    _owned_account,
    claim_account,
    connect_account,
    import_now,
    venue_closed_reason,
)
from alphamarket.watchlist import watch_labels

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
DOCS = Path(__file__).resolve().parents[2] / "docs"
templates.env.globals.update(sigil=sigil, tag_color=tag_color, fmt_num=fmt_num)


def _user(request: Request, db: Session) -> User | None:
    return auth.optional_user(request, db)  # DB session cookie (sessions.py)


def safe_next(next_url: str | None, default: str = "/dashboard") -> str:
    """Only same-site paths: never an open redirect. Browsers read a
    backslash as a slash (`/\\evil.com` goes to evil.com) and drop tabs and
    newlines, so those are refused too."""
    if (
        next_url
        and next_url.startswith("/")
        and not next_url.startswith("//")
        and "\\" not in next_url
        and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in next_url)
    ):
        return next_url
    return default


def login_url(next_path: str) -> str:
    """/login that comes back to `next_path` afterwards."""
    return "/login?next=" + quote(next_path, safe="/")


def _demo() -> bool:
    return get_settings().demo_mode


def _demo_or_404() -> None:
    if not _demo():
        raise HTTPException(status_code=404)


# ---- w3-seed: seeded demo database vs static sample rows ---------------------------
# scripts/seed_demo.py runs synthetic venue data through the real pipeline and
# marks its users is_demo (invariant 7). When such rows exist (and DEMO_MODE is
# on), they replace the static `demo.py` padding on the marketplace, landing and
# transparency pages, so nothing is listed twice. An empty database in
# DEMO_MODE (the no-DB Vercel demo) keeps the static padding exactly as before.


def _seeded(db: Session) -> bool:
    """DEMO_MODE is on and the database holds seeded demo strategies."""
    if not _demo():
        return False
    found = db.scalar(
        select(Strategy.id).join(User, User.id == Strategy.user_id).where(User.is_demo).limit(1)
    )
    return found is not None


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


def _marketplace_rows(
    db: Session,
    include_incubating: bool,
    tag: str | None,
    venue: str | None = None,
    asset: str | None = None,
    operator: str | None = None,
) -> list[dict]:
    """Real rows first (sorted by return, as `list_visible` does), then sample
    rows in demo mode. A row's `demo` key drives the sample banner. Sample
    rows are Kalshi-style prediction markets, so a venue or asset filter for
    anything else leaves them out."""
    rows = list_visible(
        db,
        include_incubating=include_incubating,
        tag=tag,
        venue=venue,
        asset=asset,
        operator=operator,
    )
    if _demo() and not _seeded(db):  # w3-seed: seeded rows replace the static padding
        for r in demo.marketplace_rows():
            if venue and venue != r["venue"]:
                continue
            if asset and asset not in r["asset_classes"]:
                continue
            if tag and tag not in r["tags"]:
                continue
            if operator and operator != r.get("operator", "creator"):
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


def _auth(request: Request, db: Session, mode: str, error: str | None = None, next_url: str = ""):
    """Login and sign-up get their own pages so an error is shown next to the
    form that produced it, not at the bottom of the landing page. A visitor who
    is already logged in is sent on (to `next` when it is a same-site path,
    else the dashboard) instead of an empty form; a failed submission always
    shows its error. `next` rides along as a hidden field."""
    next_url = safe_next(next_url, "")
    if error is None and _user(request, db) is not None:
        return RedirectResponse(next_url or "/dashboard", status_code=303)
    return _render(request, "auth.html", mode=mode, error=error, user=None, next=next_url)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", db: Session = Depends(get_db)):
    return _auth(request, db, "login", next_url=next)


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, next: str = "", db: Session = Depends(get_db)):
    return _auth(request, db, "signup", next_url=next)


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
    venue: str | None = None,
    asset: str | None = None,
    operator: str | None = None,
    db: Session = Depends(get_db),
):
    """`view=cards` (default) is for browsing; `view=table` is the comparison
    table with every column. Same rows, same order, either way. `venue=` keeps
    one venue's strategies and `asset=` (prediction | equities | crypto) one
    asset class; the tabs set `asset`, the venue chips set `venue`. `operator=`
    koine | creator keeps only Koine-run strategies or drops them (no sort
    effect); anything else shows all."""
    venue, asset = (venue or "").strip(), (asset or "").strip()
    operator = operator if operator in ("koine", "creator") else ""
    rows = _marketplace_rows(
        db,
        include_incubating=incubating == "1",
        tag=tag or None,
        venue=venue or None,
        asset=asset or None,
        operator=operator or None,
    )
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
        tags=all_tags(
            _marketplace_rows(
                db,
                include_incubating=True,
                tag=None,
                venue=venue or None,
                asset=asset or None,
                operator=operator or None,
            )
        ),
        venues=venues_in(
            _marketplace_rows(
                db,
                include_incubating=True,
                tag=None,
                asset=asset or None,
                operator=operator or None,
            )
        ),
        tag=tag or "",
        operator=operator,
        venue=venue,
        asset=asset,
        asset_tabs=[(key, ASSET_CLASS_LABELS[key]) for key in ASSET_CLASS_ORDER],
        asset_label=ASSET_CLASS_LABELS.get(asset, "").lower(),
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
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        auth.signup(
            auth.SignupIn(email=email, password=password, display_name=display_name), request, db
        )
    except HTTPException as exc:
        return _auth(request, db, "signup", exc.detail, next_url=next)
    except ValueError as exc:  # pydantic validation
        return _auth(request, db, "signup", str(exc), next_url=next)
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/login-form")
def login_form(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        result = auth.login(auth.LoginIn(email=email, password=password), request, db)
    except HTTPException as exc:
        if exc.status_code == 429:
            resp = _auth(
                request,
                db,
                "login",
                "Too many attempts. Wait a minute and try again.",
                next_url=next,
            )
            resp.status_code = 429
            return resp
        return _auth(request, db, "login", "Invalid credentials", next_url=next)
    except ValueError:
        return _auth(request, db, "login", "Invalid credentials", next_url=next)
    next = safe_next(next, "")
    if result.get("mfa_required"):
        # The second step carries `next` on to its own redirect.
        target = "/login/2fa" + (f"?next={quote(next, safe='/')}" if next else "")
        return RedirectResponse(target, status_code=303)
    return RedirectResponse(next or "/dashboard", status_code=303)


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
                "status": account_status(db, account),
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
    return _render(
        request,
        "dashboard.html",
        error=None,
        demo=user.is_demo,  # w3-seed: a seeded user's accounts are sample data
        **_dashboard_ctx(db, user),
    )


def _connect_ctx(venue: str) -> dict:
    if venue not in PROFILES:
        raise HTTPException(status_code=404, detail="Unknown venue")
    profile = get_profile(venue)
    return {
        "profile": profile,
        "profiles": list(PROFILES.values()),
        "venue_env": get_settings().venue_env(venue),
        "closed_reason": venue_closed_reason(venue),
        # wave2 (D18): the exact text, and the hash the form posts back.
        "consent": consent_text(venue),
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
    key_id: str = Form(""),
    secret: str | None = Form(None),
    passphrase: str | None = Form(None),
    account_ref: str | None = Form(None),
    strategy_name: str = Form(""),
    consent_sha256: str | None = Form(None),
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
        payload = ConnectIn(
            venue=venue,
            key_id=key_id.strip(),
            secret=secret or None,
            passphrase=passphrase or None,
            account_ref=(account_ref or "").strip() or None,
            strategy_name=strategy_name.strip(),
            consent_sha256=consent_sha256,
        )
        result = connect_account(db, user, payload, request=request)
    except ValidationError as exc:
        # Field names only: a pydantic error must never echo a pasted secret.
        fields = sorted({str(e["loc"][-1]) for e in exc.errors() if e.get("loc")})
        error = "Check these fields: " + ", ".join(fields).replace("_", " ") + "."
        resp = _render(request, "connect.html", user=user, error=error, gate=None, **ctx)
        resp.status_code = 422
        return resp
    except HTTPException as exc:
        resp = _render(request, "connect.html", user=user, error=exc.detail, gate=None, **ctx)
        if exc.status_code == 422 and exc.detail == STALE_CONSENT:
            resp.status_code = 422  # the re-rendered page carries the current text
        return resp
    if not result.get("claimed", True):
        # Address-keyed venues: straight on to proving the address is theirs.
        return RedirectResponse(f"/dashboard/connect/claim/{result['id']}", status_code=303)
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
            import_now(db, account)
        elif action == "reconcile":
            reconcile_account(db, account)
        elif action == "claim" and get_profile(account.venue).ownership == "signed_message":
            # wave2 (D21): a signed claim needs the message page, not a bare POST.
            return RedirectResponse(f"/dashboard/connect/claim/{account.id}", status_code=303)
        elif action == "claim":
            ratelimit.check(request, "venues_user", user.id)
            claim_account(db, user, account.id, limiter=get_web_limiter(request.app))
        elif action == "disconnect":
            mfa.ensure_step_up(request, db, user)
            credentials.disconnect(
                db, account, actor_user_id=user.id, limiter=get_web_limiter(request.app)
            )
        else:
            raise HTTPException(status_code=404)
    except HTTPException as exc:
        if action not in ("claim", "disconnect"):
            raise
        error = str(exc.detail)
    except Exception as exc:  # recorded on the account row by sync/reconcile
        error = f"{action} failed: {type(exc).__name__}"
    if error:
        return _render(
            request, "dashboard.html", error=error, demo=user.is_demo, **_dashboard_ctx(db, user)
        )
    return RedirectResponse("/dashboard", status_code=303)


# ---- public strategy page ------------------------------------------------------


CHART_W, EQUITY_H, DRAWDOWN_H = 900, 260, 110


def _charts(stats: dict | None, benchmarks: list | None = None) -> dict | None:
    """SVG geometry for the equity and drawdown curves. Benchmarks (sample
    pages only) share the equity axis so the lines are comparable."""
    if not stats:
        return None
    # v2 (asset) pages plot the flow-adjusted growth curve only: falling back
    # to raw equity would chart a deposit as a gain, and its drawdown curve
    # (from the index) would not line up with it.
    curve = stats.get("chart_curve") if stats.get("valuation") == "v2" else stats["equity_curve"]
    if not curve or len(stats["drawdown_curve"]) != len(curve):
        return None
    equity = [e for _, e in curve]
    dates = [d for d, _ in curve]
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


def _worst_periods(st: dict) -> list[tuple[str, float, str, str]]:
    """The page's dated worst periods (stats.worst_periods over the drawdown
    curve the chart draws), once the record is past incubation. None before
    the first nightly stats run."""
    if not st or (st.get("track_days") or 0) < INCUBATION_DAYS:
        return []
    curve = [(date.fromisoformat(d), dd) for d, dd in st.get("drawdown_curve") or []]
    rows = []
    for peak, trough, depth, back in worst_periods(curve):
        span = f"{peak:%b} {peak.day} to {trough:%b} {trough.day}"
        if back is None:
            rec = "not yet recovered"
        else:
            days = (back - peak).days
            rec = f"recovered in {days} day{'s' if days != 1 else ''}"
        rows.append((span, -depth, rec, ""))
    return rows


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
    user = _user(request, db)
    page = page_data(db, strategy)
    page["worst_periods"] = _worst_periods(page["stats"])
    page["tailing"] = _tailing(db, strategy, page, user)
    page["follow"] = {
        "count": follower_count(db, strategy.id),
        "following": user is not None and is_following(db, user.id, strategy.id),
        "own": user is not None and strategy.user_id == user.id,
        "login": login_url(f"/s/{strategy.slug}"),
    }
    return _render(
        request,
        "strategy.html",
        user=user,
        page=page,
        charts=_charts(page["stats"]),
        demo=page["sample_data"],  # w3-seed: a seeded record carries the sample banner
    )


# ---- w3-tail-ui: paper tailing and following from the HTML pages ---------------------
# Every action reuses the JSON-route logic (tails.create_allocation, stop_by_user,
# follows.follow_strategy), with the same rate limit, so both surfaces enforce
# the same rules. Paper only (invariant 8): no form here can ask for live mode.


def _tailing(db: Session, strategy: Strategy, page: dict, user: User | None) -> dict | None:
    """The strategy page's tail card for a real (or seeded) strategy, with the
    viewer's state: `anon` (log in first), `own` (a creator cannot tail
    themselves), `active` (already tailing: see /portfolio) or `ready`."""
    venue = page.get("venue")
    if venue not in PROFILES:
        return None
    card = tailing_card(venue, active_tail_count(db, strategy.id))
    tail_path = f"/s/{strategy.slug}/tail"
    if user is None:
        card.update(state="anon", href=login_url(tail_path))
    elif strategy.user_id == user.id:
        card.update(state="own", href=None)
    elif active_allocation(db, user.id, strategy.id) is not None:
        card.update(state="active", href="/portfolio")
    else:
        card.update(state="ready", href=tail_path)
    return card


DRAWDOWN_LABELS = [(bps, f"−{bps // 100}%") for bps in DRAWDOWN_CHOICES_BPS]


def _tail_ctx(
    db: Session,
    strategy: Strategy,
    user: User,
    allocation: str = "2000",
    drawdown_bps: int = DEFAULT_DRAWDOWN_BPS,
    confirmed: bool = False,
    error: str | None = None,
) -> dict:
    page = page_data(db, strategy)
    venue = page["venue"]
    return {
        "real": True,
        "strategy": page,
        "terms": paper_terms(venue)["rows"] if venue in PROFILES else [],
        "pricing_rows": PRICING_ROWS,
        "min_usd": MIN_ALLOCATION_MICROS // 1_000_000,
        "max_usd": MAX_ALLOCATION_MICROS // 1_000_000,
        "allocation_input": allocation,
        "dd_choices": DRAWDOWN_LABELS,
        "dd_bps": drawdown_bps,
        "confirmed": confirmed,
        "error": error,
        "own": strategy.user_id == user.id,
        "active": active_allocation(db, user.id, strategy.id) is not None,
    }


def _render_tail(request: Request, t: dict, user: User, status_code: int = 200) -> HTMLResponse:
    watch = bool(t.get("watch"))  # a private watch (watch_pages): its banner, no indexing
    resp = _render(
        request,
        "tail.html",
        user=user,
        t=t,
        demo=t["strategy"]["sample_data"],
        private_watch=watch,
        noindex=watch,
    )
    resp.status_code = status_code
    return resp


@router.get("/s/{slug}/tail", response_class=HTMLResponse)
def tail_page(request: Request, slug: str, db: Session = Depends(get_db)):
    """The paper-tail form for a publicly visible strategy (logged-in users;
    a visitor is sent to log in and comes back here). With DEMO_MODE on, the
    static sample strategies keep their sample flow."""
    try:
        strategy = visible_strategy_or_404(db, slug)
    except HTTPException:
        if not (_demo() and demo.has_strategy(slug)):
            raise
        t = demo.tail_page(slug)
        return _render(request, "tail.html", user=_user(request, db), t=t, demo=True)
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url(f"/s/{strategy.slug}/tail"), status_code=303)
    return _render_tail(request, _tail_ctx(db, strategy, user), user)


@router.post("/s/{slug}/tail")
def tail_form(
    request: Request,
    slug: str,
    allocation: str = Form(""),
    drawdown_limit_bps: str = Form(str(DEFAULT_DRAWDOWN_BPS)),
    confirm: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Start a paper tail: tails.create_allocation does every check (paper
    only, not your own strategy, $100 to $1,000,000, one active tail per
    strategy); this adds the paper-only confirmation box. Errors re-render
    the form with what was entered."""
    strategy = visible_strategy_or_404(db, slug)
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url(f"/s/{strategy.slug}/tail"), status_code=303)
    return start_tail_form(
        request, db, user, strategy, allocation, drawdown_limit_bps, confirm, _tail_ctx
    )


def start_tail_form(
    request: Request,
    db: Session,
    user: User,
    strategy: Strategy,
    allocation: str,
    drawdown_limit_bps: str,
    confirm: str | None,
    tail_ctx: Callable[..., dict],
) -> Response:
    """The tail form's POST for a strategy the caller has already resolved
    (public here, a private watch in watch_pages): the paper-only box, then
    tails.create_allocation. `tail_ctx` re-renders the form on an error."""
    amount = allocation.strip().replace(",", "").removeprefix("$").strip()
    try:
        bps = int(drawdown_limit_bps)
    except ValueError:
        bps = -1  # refused by AllocationIn below
    error, code = None, 422
    if confirm != "on":
        error = "Tick the box to confirm you understand this is a paper simulation."
    else:
        try:
            ratelimit.check(request, "follow_user", user.id)  # the JSON route's limit
            payload = AllocationIn(
                strategy_slug=strategy.slug, allocation=amount, drawdown_limit_bps=bps
            )
            create_allocation(db, user, payload)
        except ValidationError:
            error = "Choose a drawdown limit from the list."
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            error, code = str(exc.detail), exc.status_code
    if error is not None:
        if bps not in DRAWDOWN_CHOICES_BPS:
            bps = DEFAULT_DRAWDOWN_BPS
        t = tail_ctx(db, strategy, user, amount, bps, confirm == "on", error)
        return _render_tail(request, t, user, code)
    return RedirectResponse("/portfolio?notice=started", status_code=303)


@router.post("/s/{slug}/follow")
def follow_form(request: Request, slug: str, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        visible_strategy_or_404(db, slug)
        return RedirectResponse(login_url(f"/s/{slug}"), status_code=303)
    ratelimit.check(request, "follow_user", user.id)  # the JSON route's limit
    strategy, _ = follow_strategy(db, user, slug)
    return RedirectResponse(f"/s/{strategy.slug}", status_code=303)


@router.post("/s/{slug}/unfollow")
def unfollow_form(request: Request, slug: str, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url(f"/s/{slug}"), status_code=303)
    unfollow_strategy(db, user, slug)
    return RedirectResponse(f"/s/{slug}", status_code=303)


@router.post("/portfolio/allocations/{allocation_id}/stop")
def stop_tail_form(request: Request, allocation_id: int, db: Session = Depends(get_db)):
    """Stop one of your paper tails (tails.stop_by_user); someone else's is a 404."""
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/portfolio"), status_code=303)
    stop_by_user(db, owned_allocation(db, allocation_id, user))
    return RedirectResponse("/portfolio?notice=stopped", status_code=303)


PORTFOLIO_NOTICES = {
    "started": "Paper tail started. It copies the creator's trades from their next one; "
    "nothing earlier is replayed.",
    "stopped": "Paper tail stopped. No new trades are copied; its history stays here.",
}


# ---- demo-only investor surface (portfolio, feed, creator profiles) ----------------


def _age(seconds: int | None) -> str:
    """Copy lag as people say it: 42 s, 7 min, 3.5 h, 2.0 d."""
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def _utc(iso: str) -> datetime:
    """An ISO timestamp as aware UTC (SQLite hands back naive ones; invariant 2)."""
    parsed = datetime.fromisoformat(iso)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _combined_curve(views: list[dict]) -> list[tuple[str, float]]:
    """Sum of the allocations' equity per UTC day: each allocation counts its
    latest value at or before the day, and from the day its first snapshot
    exists (an allocation that has not started contributes nothing)."""
    per_alloc = []
    for v in views:
        by_day: dict[str, float] = {}
        for when, equity in sorted((_utc(at), e) for at, e in v["curve"]):
            by_day[when.date().isoformat()] = equity  # last snapshot of the UTC day wins
        per_alloc.append(by_day)
    days = sorted({d for by_day in per_alloc for d in by_day})
    last: list[float | None] = [None] * len(per_alloc)
    out = []
    for day in days:
        for i, by_day in enumerate(per_alloc):
            if day in by_day:
                last[i] = by_day[day]
        out.append((day, sum(x for x in last if x is not None)))
    return out


def _allocated_by_day(views: list[dict], days: list[str]) -> list[float]:
    """Paper money put in by each day: an allocation counts from the UTC day
    of its first snapshot, the day it starts contributing to _combined_curve."""
    starts = []
    for v in views:
        if v["curve"]:
            first = min(_utc(at) for at, _ in v["curve"]).date().isoformat()
            starts.append((first, v["allocation"]))
    return [sum(a for first, a in starts if first <= day) for day in days]


def _flow_adjusted_index(values: list[float], allocated: list[float]) -> list[float]:
    """Growth of 1 across the combined curve with new allocations removed, as
    strategy pages do for deposits (methodology, E14): a day's return is
    (V_d - new paper money_d) / V_(d-1) - 1, so starting a tail is not a gain
    and does not raise the peak drawdown is measured from."""
    index, out = 1.0, []
    for i, value in enumerate(values):
        if i and values[i - 1] > 0:
            index *= (value - (allocated[i] - allocated[i - 1])) / values[i - 1]
        out.append(index)
    return out


def _position_rows(view: dict) -> list[dict]:
    """One allocation's positions in a single shape. Asset rows (paper.py
    asset_view_extras) carry kind/symbol/mark/value/unrealized; binary rows
    (portfolio.allocation_view) are carried at cost with no mark."""
    rows = []
    for p in view.get("positions", []):
        if "symbol" in p:
            rows.append(
                {
                    "symbol": p.get("label") or p["symbol"],
                    "kind": p.get("kind"),
                    "side": p.get("side"),
                    "qty": p.get("qty"),
                    "mark": p.get("mark"),
                    "mark_source": p.get("mark_source"),
                    "value": p.get("value"),
                    "unrealized": p.get("unrealized"),
                    "entry": p.get("entry"),
                }
            )
        else:
            rows.append(
                {
                    "symbol": p["ticker"],
                    "kind": "binary",
                    "side": p["side"],
                    "qty": f"{p['count']:g}",
                    "mark": None,
                    "mark_source": "cost",
                    "value": p["cost"],
                    "unrealized": None,
                    "entry": None,
                }
            )
    return rows


def portfolio_view(
    summary: dict,
    handles: dict[str, str],
    now: datetime | None = None,
    watched: dict[str, tuple[str, str | None]] | None = None,
) -> dict:
    """The /portfolio template's `p` for a real user, from portfolio_summary
    (paper.asset_view_extras documents the v2 position keys). Pure: handles
    maps strategy slug -> creator display name; watched maps the slug of a
    private watch -> (the user's label, its /watch page or None if removed)."""
    watched = watched or {}
    now = now or datetime.now(UTC)
    views = summary["allocations"]
    allocated = summary["allocated"]
    curve = _combined_curve(views)
    days = [d for d, _ in curve]
    put_in = _allocated_by_day(views, days)
    # The chart is paper P&L in dollars and drawdown is read off the
    # flow-adjusted index: summed value would show every new tail as a jump.
    equity = [e - a for (_, e), a in zip(curve, put_in, strict=True)]
    index = _flow_adjusted_index([e for _, e in curve], put_in)
    dd = drawdown_series(
        [
            (datetime.fromisoformat(d).date(), round(x * 1e9))
            for d, x in zip(days, index, strict=True)
        ]
    )
    positions, allocations = [], []
    lag_all, skipped_all = [], 0
    for v in views:
        rows = _position_rows(v)
        skipped = v.get("skipped") or {}
        skipped_all += sum(skipped.values())
        if v.get("copy_lag_median_seconds") is not None:
            lag_all.append(v["copy_lag_median_seconds"])
        for r in rows:
            positions.append({**r, "allocation": v["strategy_name"], "slug": v["strategy_slug"]})
        name, href = watched.get(
            v["strategy_slug"], (v["strategy_name"], f"/s/{v['strategy_slug']}")
        )
        allocations.append(
            {
                "id": v.get("id"),
                "slug": v["strategy_slug"],
                "name": name,
                "href": href,
                "watch": v["strategy_slug"] in watched,
                "handle": handles.get(v["strategy_slug"], ""),
                "operator": v.get("operator", "creator"),
                "days": max(0, (now - _utc(v["started_at"])).days),
                "allocated": v["allocation"],
                "value": v["equity"],
                "ret": v["return"],
                "dd": v["drawdown"],
                "limit": v["drawdown_limit"],
                "status": v["status"],
                "stop_reason": v["stop_reason"],
                "positions": rows,
                "copy_lag": _age(v.get("copy_lag_median_seconds")),
                "skipped": sorted(skipped.items()),
                "symbol_changes": v.get("symbol_changes") or [],
                "venue": v.get("venue"),
            }
        )
    lo, hi = (min(equity), max(equity)) if equity else (0.0, 0.0)
    n = len(curve)
    x_idx = sorted({0, n // 4, n // 2, 3 * n // 4, n - 1}) if n else []
    unrealized = sum(r["unrealized"] or 0.0 for r in positions)
    return {
        "real": True,
        "value": summary["value"],
        "accounts": len(views),
        "pnl": summary["pnl"],
        "pnl_pct": summary["pnl"] / allocated if allocated else 0.0,
        "current_dd": dd[-1][1] if dd else 0.0,
        "worst_dd": max((x for _, x in dd), default=0.0),
        "n_positions": len(positions),
        "unrealized": unrealized,
        "curve": polyline(equity, 900, 200, lo=lo, hi=hi),
        "curve_lo": lo,
        "curve_hi": hi,
        "x_labels": [curve[i][0] for i in x_idx],
        "risk": [
            ("Active tails", str(summary["n_active"])),
            ("Paper allocated", f"${allocated:,.2f}"),
            ("Open positions", str(len(positions))),
            ("Median copy lag", _age(int(median(lag_all))) if lag_all else "—"),
            ("Signals skipped", str(skipped_all)),
        ],
        "allocations": allocations,
        "positions": positions,
        "skipped_total": skipped_all,
        "activity": [],
    }


def _portfolio_activity(db: Session, allocation_ids: list[int], limit: int = 12) -> list[tuple]:
    """Recent paper events across the user's allocations, newest first, as
    (when, kind, what, amount | None) rows for the Activity panel."""
    if not allocation_ids:
        return []
    events: list[tuple[datetime, tuple]] = []
    for f in db.scalars(
        select(MirrorFill)
        .where(MirrorFill.allocation_id.in_(allocation_ids))
        .order_by(MirrorFill.filled_at.desc())
        .limit(limit)
    ):
        count, price = f.count_micros / 1e6, f.price_micros / 1e6  # display only
        what = f"{f.action} {f.side} ×{fmt_num(count)} {f.ticker} @ ${price:.2f}"
        events.append((f.filled_at, ("Paper fill", what, None)))
    for f in db.scalars(
        select(MirrorAssetFill)
        .where(MirrorAssetFill.allocation_id.in_(allocation_ids))
        .order_by(MirrorAssetFill.filled_at.desc())
        .limit(limit)
    ):
        inst = db.get(Instrument, f.instrument_id)
        qty = fmt_num(format_atoms(f.qty_atoms, f.qty_decimals))
        what = f"{f.action} {qty} {instrument_label(inst)} @ {fmt_num(f.price)}"
        events.append(
            (
                f.filled_at,
                ("Liquidation" if f.reason == "liquidation" else "Paper fill", what, None),
            )
        )
    for st in db.scalars(
        select(MirrorSettlement)
        .where(MirrorSettlement.allocation_id.in_(allocation_ids))
        .order_by(MirrorSettlement.settled_at.desc())
        .limit(limit)
    ):
        events.append(
            (
                st.settled_at,
                ("Settled", f"{st.ticker} → {st.market_result}", st.revenue_micros / 1e6),
            )
        )
    events = [(_utc(at.isoformat()), row) for at, row in events]  # one clock: aware UTC
    events.sort(key=lambda e: e[0], reverse=True)
    return [
        (at.strftime("%m-%d %H:%M"), kind, what, amt) for at, (kind, what, amt) in events[:limit]
    ]


@router.get("/portfolio", response_class=HTMLResponse)
def portfolio(request: Request, notice: str = "", db: Session = Depends(get_db)):
    """A logged-in user sees their own paper allocations; with DEMO_MODE on a
    visitor without an account sees the sample portfolio; otherwise 404."""
    user = _user(request, db)
    if user is not None:
        summary = portfolio_summary(db, user)
        ids = [
            a.id for a in db.scalars(select(Allocation).where(Allocation.user_id == user.id)).all()
        ]
        handles, watched = {}, {}
        labels = watch_labels(db, user.id)
        for v in summary["allocations"]:
            strategy = db.scalar(select(Strategy).where(Strategy.slug == v["strategy_slug"]))
            if strategy is not None and strategy.kind == StrategyKind.WATCH:
                # A private watch: the user's own label, its private page (none
                # once the watch is removed), never the system owner's name.
                handles[v["strategy_slug"]] = "Private watch"
                watched[v["strategy_slug"]] = (
                    labels.get(strategy.id, strategy.name),
                    f"/watch/{strategy.id}" if strategy.id in labels else None,
                )
                continue
            owner = db.get(User, strategy.user_id) if strategy else None
            handles[v["strategy_slug"]] = owner.display_name if owner else ""
        p = portfolio_view(summary, handles, watched=watched)
        p["activity"] = _portfolio_activity(db, ids)
        # w3-seed: a seeded demo investor's paper book is sample data (invariant 7).
        return _render(
            request,
            "portfolio.html",
            user=user,
            p=p,
            demo=user.is_demo,
            notice=PORTFOLIO_NOTICES.get(notice),  # fixed texts: nothing is reflected
        )
    _demo_or_404()
    return _render(request, "portfolio.html", user=None, p=demo.portfolio(), demo=True)


@router.get("/u/{handle}", response_class=HTMLResponse)
def creator_profile(request: Request, handle: str, db: Session = Depends(get_db)):
    _demo_or_404()
    c = demo.creator(handle)
    if c is None:
        # w3-seed: a seeded creator has no sample profile; send the visitor to
        # their public strategy page instead of a 404.
        slug = _seeded_creator_slug(db, handle)
        if slug is None:
            raise HTTPException(status_code=404)
        return RedirectResponse(f"/s/{slug}", status_code=303)
    return _render(request, "creator.html", user=_user(request, db), c=c, demo=True)


@router.get("/feed", response_class=HTMLResponse)
def feed(request: Request, db: Session = Depends(get_db)):
    _demo_or_404()
    f = demo.feed()
    user = _user(request, db)
    if user is not None and _seeded(db):
        # w3-seed: a logged-in visitor of a seeded demo sees the real signals
        # of the strategies they follow or tail (follows.feed_items) as posts;
        # rooms and chat stay the static community mock-up.
        f = {**f, "posts": _feed_posts(db, user)}
    return _render(request, "feed.html", user=user, f=f, demo=True)


# ---- w3-seed: the seeded feed --------------------------------------------------------


def _seeded_creator_slug(db: Session, handle: str) -> str | None:
    """The first publicly visible strategy of the demo user named `handle`."""
    for strategy in db.scalars(
        select(Strategy)
        .join(User, User.id == Strategy.user_id)
        .where(User.is_demo, User.display_name == handle)
        .order_by(Strategy.id)
    ).all():
        if publicly_visible(db, strategy, db.get(VenueAccount, strategy.venue_account_id)):
            return strategy.slug
    return None


def _ago(at: str, now: datetime) -> str:
    seconds = max(0, int((now - _utc(at)).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return "yesterday" if seconds < 2 * 86_400 else f"{seconds // 86_400}d"


def _post_body(item: dict, strategy_name: str) -> str:
    """One feed item in words, from the platform's own numbers only."""
    if item["kind"] == "settlement":
        paid = f"paid ${item['payout']:,.2f}" if item["payout"] else "settled at 0"
        return (
            f"{strategy_name}: {item['ticker']} resolved {item['result'].upper()}; "
            f"held {item['count']:g} {item['side'].upper()}, {paid}."
        )
    share = item.get("stake_fraction")
    size = f" ({share:.1%} of equity)" if share is not None else ""
    if item.get("asset"):
        price = fmt_num(item.get("price_str"))
        lag = item.get("copy_lag_seconds")
        seen = f" Seen {_age(lag)} after the fill." if lag is not None else ""
        label = item.get("label") or item["ticker"]
        return (
            f"{strategy_name}: {item['action']} {fmt_num(item['qty'])} {label} at {price}"
            f"{size}, {item['side']} position.{seen}"
        )
    cents = round(item["price"] * 100)
    return (
        f"{strategy_name}: {item['action']} {item['count']:g} {item['side'].upper()} "
        f"on {item['ticker']} at {cents}¢{size}."
    )


def _feed_posts(db: Session, user: User, limit: int = 12) -> list[dict]:
    ids = set(db.scalars(select(Follow.strategy_id).where(Follow.user_id == user.id)).all())
    ids |= set(
        db.scalars(select(Allocation.strategy_id).where(Allocation.user_id == user.id)).all()
    )
    strategies = db.scalars(select(Strategy).where(Strategy.id.in_(ids))).all() if ids else []
    rows = {r["slug"]: r for r in list_visible(db, include_incubating=True)}
    now = datetime.now(UTC)
    posts = []
    for item in feed_items(db, list(strategies), limit=limit):
        row = rows.get(item["strategy_slug"])
        context = (
            f"{row['total_return'] * 100:+.1f}% · −{row['max_drawdown'] * 100:.1f}% DD verified"
            if row is not None
            else None
        )
        posts.append(
            {
                "handle": row["display_name"] if row else item["strategy_name"],
                "operator": item.get("operator", "creator"),
                "when": _ago(item["at"], now),
                "likes": 0,
                "replies": 0,
                "strategy": None,
                "context": context,
                "body": _post_body(item, item["strategy_name"]),
            }
        )
    return posts


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
    count of quarantined/delisted records. Sample figures only in demo mode:
    static ones on an empty database, else the seeded records' (w3-seed)."""
    if _demo() and not _seeded(db):
        return demo.transparency()
    rows = list_visible(db, include_incubating=True)
    # Watched addresses were never listed, so they never count as removed.
    n_removed = len(
        db.scalars(
            select(Strategy.id).where(
                Strategy.status.in_([StrategyStatus.QUARANTINED, StrategyStatus.DELISTED]),
                Strategy.kind == StrategyKind.CREATOR,
            )
        ).all()
    )
    return {
        "demo": any(r.get("demo") for r in rows),
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
