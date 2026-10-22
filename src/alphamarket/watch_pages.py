"""HTML pages of the private watchlist (watchlist.py holds the rules).

`/watch` lists the user's watched addresses and adds new ones; `/watch/{id}`
is the strategy page of one of them, shown only to a user with a Watch row
(404 for anyone else, logged in or not); `/watch/{id}/tail` starts a paper
tail through the same handler as a public strategy's tail form. Every
response is `Cache-Control: private, no-store` and carries a noindex meta.
"""

from datetime import UTC, datetime

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from alphamarket import demo, discover, ratelimit
from alphamarket.analytics import latest_stats
from alphamarket.charts import polyline, window_x
from alphamarket.config import get_settings
from alphamarket.connectors.registry import build_connector, get_profile
from alphamarket.db import get_db
from alphamarket.models import Strategy, User
from alphamarket.pages import (
    DEFAULT_DRAWDOWN_BPS,
    DOCS,
    _charts,
    _render,
    _tail_ctx,
    _user,
    _worst_periods,
    login_url,
    start_tail_form,
    tail_form_fields,
)
from alphamarket.public import INCUBATION_DAYS, SPARKLINE_DAYS, is_v2, page_data
from alphamarket.tail_terms import tailing_card
from alphamarket.tails import active_allocation
from alphamarket.trader_profile_db import trader_profile
from alphamarket.venue_limits import get_web_limiter
from alphamarket.watchlist import (
    WATCHABLE_VENUES,
    AlreadyListed,
    OptoutClaim,
    OptoutResult,
    Watched,
    WatchIn,
    add_watch,
    open_venues,
    perform_optout,
    remove_watch,
    rename_watch,
    short_address,
    start_optout,
    watch_strategy_or_404,
    watched_for,
)

router = APIRouter(include_in_schema=False)

NOTICES = {
    "added": "Added to your watchlist. Its history is being imported; stats follow the "
    "nightly run.",
    "removed": "Removed from your watchlist. Any paper tail of it was stopped.",
    "renamed": "Label saved.",
}
DISCOVER_NOTICES = {"added": "Added to your watchlist. Its history is being imported."}


def _private(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _render_private(request: Request, name: str, status_code: int = 200, **ctx) -> Response:
    resp = _render(request, name, noindex=True, **ctx)
    resp.status_code = status_code
    return _private(resp)


def _watch_name(w: Watched) -> str:
    return w.watch.label or short_address(w.account.key_id)


def watch_row(db: Session, user: User, w: Watched) -> dict:
    """One /watch list row: the label, the address, and the record so far."""
    stats = latest_stats(db, w.strategy.id)
    row = {
        "id": w.strategy.id,
        "name": _watch_name(w),
        "label": w.watch.label,
        "address": w.account.key_id,
        "short_address": short_address(w.account.key_id),
        "venue_label": get_profile(w.account.venue).display_name,
        "tailing": active_allocation(db, user.id, w.strategy.id) is not None,
        "stats": None,
    }
    if stats is None:
        return row
    if is_v2(stats) and stats.extras.get("twr_index"):
        daily = [float(v) for _, v in stats.extras["twr_index"][-SPARKLINE_DAYS:]]
    else:
        daily = [float(e) for _, e in stats.extras.get("daily_equity", [])[-SPARKLINE_DAYS:]]
    row["stats"] = {
        "track_days": stats.track_days,
        "incubating": stats.track_days < INCUBATION_DAYS,
        "total_return": stats.total_return
        if stats.total_return is not None
        else (stats.extras.get("return_on_peak_invested") or 0.0),
        "max_drawdown": stats.max_drawdown,
        "spark": polyline(daily, 120, 32),
        "spark_dd": window_x(daily, 120),
    }
    return row


def _list(
    request: Request,
    db: Session,
    user: User,
    *,
    error: str | None = None,
    listed_slug: str | None = None,
    form: dict | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    rows = [watch_row(db, user, w) for w in watched_for(db, user)]
    venues = [(v, get_profile(v).display_name) for v in open_venues()]
    return _render_private(
        request,
        "watch.html",
        status_code=status_code,
        user=user,
        demo=user.is_demo,  # a seeded demo investor's watchlist is sample data
        rows=rows,
        venues=venues,
        limit=get_settings().watch_max_per_user,
        verified=user.email_verified_at is not None,
        error=error,
        listed_slug=listed_slug,
        form=form or {"venue": venues[0][0] if venues else "", "address": "", "label": ""},
        notice=notice,
        discover_on=_discover_on(),
    )


def _discover_on() -> bool:
    return discover.enabled() or get_settings().demo_mode


@router.get("/watch", response_class=HTMLResponse)
def watch_list(request: Request, notice: str = "", db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        if get_settings().demo_mode:  # the static demo: a visitor sees the sample list
            return _render_private(
                request,
                "watch.html",
                user=None,
                demo=True,
                private_watch=True,
                sample=True,
                rows=demo.watch_list(),
                venues=[],
                limit=get_settings().watch_max_per_user,
                verified=False,
                error=None,
                listed_slug=None,
                form={"venue": "", "address": "", "label": ""},
                notice=None,
                discover_on=True,
            )
        return RedirectResponse(login_url("/watch"), status_code=303)
    return _list(request, db, user, notice=NOTICES.get(notice))  # fixed texts only


@router.post("/watch")
def watch_add_form(
    request: Request,
    venue: str = Form(""),
    address: str = Form(""),
    label: str = Form(""),
    return_to: str = Form(""),
    d_venue: str = Form(""),
    d_period: str = Form(""),
    d_sort: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/watch"), status_code=303)
    form = {"venue": venue, "address": address.strip(), "label": label}
    # From /watch/discover (exactly "discover", nothing else): the outcome goes
    # back there with the view's own params; the target is built here, never
    # taken from the form, so it cannot be an open redirect.
    back = return_to == "discover" and _discover_on()
    try:
        ratelimit.check(request, "watch_user", user.id)  # the JSON route's limit
        watched = add_watch(
            db, user, WatchIn(venue=venue, address=address, label=label[:60]), request
        )
    except AlreadyListed as exc:
        if back:
            return _discover_page(
                request,
                db,
                user,
                d_venue,
                d_period,
                d_sort,
                error="This address is a verified Koine strategy. Follow or tail it on its "
                "public page.",
                status_code=409,
            )
        return _list(
            request,
            db,
            user,
            error="This address is a verified Koine strategy. Follow or tail it on its "
            "public page.",
            listed_slug=exc.slug,
            form=form,
            status_code=409,
        )
    except HTTPException as exc:
        if back:
            return _discover_page(
                request,
                db,
                user,
                d_venue,
                d_period,
                d_sort,
                error=str(exc.detail),
                status_code=exc.status_code,
            )
        return _list(
            request, db, user, error=str(exc.detail), form=form, status_code=exc.status_code
        )
    if back:
        view = discover.discover_view(db, user, d_venue, d_period, d_sort)
        return RedirectResponse(_discover_url(view, notice="added"), status_code=303)
    return RedirectResponse(f"/watch/{watched.strategy.id}?notice=added", status_code=303)


# ---- discover ---------------------------------------------------------------------
# Declared before /watch/{strategy_id}. Reads snapshots only (discover.py).


def _discover_url(view: discover.DiscoverView, notice: str | None = None) -> str:
    """/watch/discover with the view's validated params (never a raw form value)."""
    q = f"venue={view.venue}&period={view.period}&sort={view.sort}" if view.venue else ""
    if notice:
        q += ("&" if q else "") + f"notice={notice}"
    return "/watch/discover" + (f"?{q}" if q else "")


def _ago(at: datetime | None) -> str | None:
    if at is None:
        return None
    secs = int((datetime.now(UTC) - at).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{round(secs / 60)} minutes ago"
    if secs < 86400 * 2:
        return f"{round(secs / 3600)} hours ago"
    return f"{secs // 86400} days ago"


def _discover_page(
    request: Request,
    db: Session,
    user: User,
    venue: str | None,
    period: str | None,
    sort: str,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    view = discover.discover_view(db, user, venue, period, sort)
    return _render_private(
        request,
        "watch_discover.html",
        status_code=status_code,
        user=user,
        demo=user.is_demo,
        view=view,
        updated=_ago(view.fetched_at),
        venue_labels={v: get_profile(v).display_name for v in view.venues},
        sample=False,
        notice=notice,
        error=error,
    )


@router.get("/watch/discover", response_class=HTMLResponse)
def watch_discover(
    request: Request,
    venue: str = "",
    period: str = "",
    sort: str = "pnl",
    notice: str = "",
    db: Session = Depends(get_db),
):
    if not _discover_on():
        raise HTTPException(status_code=404)
    user = _user(request, db)
    if user is None:
        if get_settings().demo_mode:  # the static demo: a visitor sees invented rows
            view = demo.discover_sample(venue, period, sort)
            return _render_private(
                request,
                "watch_discover.html",
                user=None,
                demo=True,
                view=view,
                updated=_ago(view.fetched_at),
                venue_labels={v: get_profile(v).display_name for v in view.venues},
                sample=True,
                notice=None,
                error=None,
            )
        return RedirectResponse(login_url("/watch/discover"), status_code=303)
    return _discover_page(
        request, db, user, venue, period, sort, notice=DISCOVER_NOTICES.get(notice)
    )


def _watch_page(db: Session, user: User, w: Watched) -> dict:
    """page_data with the creator's identity replaced by the user's own
    label and the address; the tail card without the count of other users'
    tails (another watcher's activity is not this user's business)."""
    page = page_data(db, w.strategy)
    page.update(
        display_name=_watch_name(w),
        label=w.watch.label,
        address=w.account.key_id,
        short_address=short_address(w.account.key_id),
        watch_id=w.strategy.id,
        description_unverified="",
    )
    # The venue profile's proof line speaks of a claimed owner; nobody claimed this.
    page["verification"] = {
        **(page["verification"] or {}),
        "proof": "Public address, no key held; trades read from the venue's public history",
    }
    page["worst_periods"] = _worst_periods(page["stats"])
    card = tailing_card(w.account.venue, 0)
    card.pop("paper_tails")
    active = active_allocation(db, user.id, w.strategy.id) is not None
    tail_path = f"/watch/{w.strategy.id}/tail"
    card.update(
        state="active" if active else "ready",
        href="/portfolio" if active else tail_path,
    )
    if not active:
        card.update(tail_form_fields(page, w.account.venue, tail_path))
    page["tailing"] = card
    return page


# ---- opt-out (B6) ---------------------------------------------------------------
# Declared before /watch/{strategy_id} so "optout" is never read as an id. The
# address owner needs no watchlist and no strategy page: just a logged-in user
# with a verified email, the venue and the address. CSRF: the app-wide Origin
# check covers these POSTs like every other form.


def _optout_page(
    request: Request,
    user: User | None,
    *,
    stage: str = "start",
    error: str | None = None,
    form: dict | None = None,
    claim: OptoutClaim | None = None,
    result: OptoutResult | None = None,
    status_code: int = 200,
    sample: bool = False,
) -> Response:
    venues = [(v, get_profile(v).display_name) for v in WATCHABLE_VENUES]
    return _render_private(
        request,
        "watch_optout.html",
        status_code=status_code,
        user=user,
        demo=sample or bool(user and user.is_demo),
        sample=sample,
        stage=stage,
        error=error,
        form=form or {"venue": venues[0][0], "address": ""},
        venues=venues,
        claim=claim,
        result=result,
        verified=sample or (user is not None and user.email_verified_at is not None),
    )


def _sample_optout(request: Request, db: Session, stage: str) -> Response:
    """The static demo's opt-out walkthrough (DEMO_MODE on): the three stages
    with an invented address, code and counts, for anyone, logged in or not.
    Nothing a visitor posts is read, nothing is written (no WatchOptout, Watch,
    Allocation or audit row, no rate-limit row) and no venue is called: the
    claim text is built by the connector from the sample code, offline."""
    s = demo.OPTOUT_SAMPLE
    user = _user(request, db)
    form = {"venue": s["venue"], "address": s["address"]}
    if stage == "start":
        return _optout_page(request, user, form=form, sample=True)
    if stage == "proof":
        profile = get_profile(s["venue"])
        connector = build_connector(
            s["venue"], get_settings().venue_env(s["venue"]), s["address"], None
        )
        claim = OptoutClaim(
            venue=s["venue"],
            address=s["address"],
            display_name=profile.display_name,
            code=s["code"],
            message=connector.claim_text(s["code"]),
            already=False,
        )
        return _optout_page(request, user, stage="proof", claim=claim, sample=True)
    result = OptoutResult(
        s["venue"],
        s["address"],
        already=False,
        watches_removed=s["watches_removed"],
        tails_stopped=s["tails_stopped"],
    )
    return _optout_page(request, user, stage="done", result=result, sample=True)


@router.get("/watch/optout", response_class=HTMLResponse)
def watch_optout_page(request: Request, db: Session = Depends(get_db)):
    if get_settings().demo_mode:
        return _sample_optout(request, db, "start")
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/watch/optout"), status_code=303)
    return _optout_page(request, user)


@router.post("/watch/optout")
def watch_optout_start(
    request: Request,
    venue: str = Form(""),
    address: str = Form(""),
    db: Session = Depends(get_db),
):
    """Step one: the venue and address, then the proof step (no venue call)."""
    if get_settings().demo_mode:
        return _sample_optout(request, db, "proof")
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/watch/optout"), status_code=303)
    form = {"venue": venue, "address": address.strip()}
    ratelimit.check(request, "watch_optout_user", user.id)
    try:
        claim = start_optout(db, user, venue, address)
    except HTTPException as exc:
        return _optout_page(
            request, user, error=str(exc.detail), form=form, status_code=exc.status_code
        )
    if claim.already:
        return _optout_page(
            request, user, stage="done", result=OptoutResult(venue, claim.address, already=True)
        )
    return _optout_page(request, user, stage="proof", claim=claim)


@router.post("/watch/optout/verify")
def watch_optout_verify(
    request: Request,
    venue: str = Form(""),
    address: str = Form(""),
    signature: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Step two: the proof. The code is re-derived server-side; only the
    venue, the address and the signature are read from the form."""
    if get_settings().demo_mode:
        return _sample_optout(request, db, "done")
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/watch/optout"), status_code=303)
    ratelimit.check(request, "watch_optout_user", user.id)
    try:
        result = perform_optout(
            db,
            user,
            venue,
            address,
            signature=signature,
            limiter=get_web_limiter(request.app),
        )
    except HTTPException as exc:
        try:  # back to the proof step with the reason (a busy venue says busy)
            claim = start_optout(db, user, venue, address)
        except HTTPException:
            return _optout_page(
                request,
                user,
                error=str(exc.detail),
                form={"venue": venue, "address": address.strip()},
                status_code=exc.status_code,
            )
        resp = _optout_page(
            request,
            user,
            stage="proof",
            claim=claim,
            error=str(exc.detail),
            status_code=exc.status_code,
        )
        if exc.status_code == 503 and exc.headers:
            resp.headers["Retry-After"] = exc.headers.get("Retry-After", "60")
        return resp
    return _optout_page(request, user, stage="done", result=result)


@router.get("/watch/help", response_class=HTMLResponse)
def watch_help(request: Request, db: Session = Depends(get_db)):
    """docs/watchlist_help.md: how the "How this trader trades" figures are
    computed. Private help for watchers (logged in; the static demo shows it
    to a visitor), noindex like the rest of /watch. Declared before
    /watch/{strategy_id}."""
    user = _user(request, db)
    if user is None and not get_settings().demo_mode:
        return RedirectResponse(login_url("/watch/help"), status_code=303)
    body = markdown.markdown((DOCS / "watchlist_help.md").read_text(), extensions=["tables"])
    return _render_private(
        request,
        "methodology.html",
        user=user,
        body=body,
        title="How these are computed",
        nav_key="watch",
        tabs=False,
    )


@router.get("/watch/sample-{n}", response_class=HTMLResponse)
def watch_sample(request: Request, n: int, db: Session = Depends(get_db)):
    """The static demo's sample watch page (DEMO_MODE on, nobody logged in);
    404 otherwise. Declared before the int-id route so "sample-1" never
    reaches it."""
    page = demo.watch_sample_page(f"sample-{n}") if get_settings().demo_mode else None
    if page is None or _user(request, db) is not None:
        raise HTTPException(status_code=404)
    return _render_private(
        request,
        "watch_strategy.html",
        user=None,
        page=page,
        charts=_charts(page["stats"], page["benchmarks"]),
        watch=True,
        private_watch=True,
        demo=True,
        notice=None,
        trader=demo.watch_sample_profile(),
    )


@router.get("/watch/{strategy_id}", response_class=HTMLResponse)
def watch_page(request: Request, strategy_id: int, notice: str = "", db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        raise HTTPException(status_code=404)  # never confirm a watch id to a visitor
    w = watch_strategy_or_404(db, user, strategy_id)
    page = _watch_page(db, user, w)
    return _render_private(
        request,
        "watch_strategy.html",
        user=user,
        demo=user.is_demo,
        page=page,
        charts=_charts(page["stats"]),
        watch=True,
        private_watch=True,
        notice=NOTICES.get(notice),
        trader=trader_profile(db, w.strategy),  # on demand; never on a public page
    )


@router.post("/watch/{strategy_id}/label")
def watch_label_form(
    request: Request, strategy_id: int, label: str = Form(""), db: Session = Depends(get_db)
):
    user = _user(request, db)
    if user is None:
        raise HTTPException(status_code=404)
    rename_watch(db, user, strategy_id, label)
    return RedirectResponse(f"/watch/{strategy_id}?notice=renamed", status_code=303)


@router.post("/watch/{strategy_id}/remove")
def watch_remove_form(request: Request, strategy_id: int, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        raise HTTPException(status_code=404)
    remove_watch(db, user, strategy_id)
    return RedirectResponse("/watch?notice=removed", status_code=303)


def _watch_tail_ctx(db: Session, strategy: Strategy, user: User, *args) -> dict:
    """pages._tail_ctx for a watched strategy: the user's label in place of a
    creator, and the form posting back to /watch/{id}/tail."""
    w = watch_strategy_or_404(db, user, strategy.id)
    t = _tail_ctx(db, strategy, user, *args)
    t["strategy"].update(display_name=_watch_name(w), description_unverified="")
    t["base"] = f"/watch/{strategy.id}"
    t["watch"] = True
    return t


@router.get("/watch/{strategy_id}/tail", response_class=HTMLResponse)
def watch_tail_page(request: Request, strategy_id: int, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        raise HTTPException(status_code=404)
    w = watch_strategy_or_404(db, user, strategy_id)
    t = _watch_tail_ctx(db, w.strategy, user)
    return _render_private(
        request, "tail.html", user=user, t=t, private_watch=True, demo=user.is_demo
    )


@router.post("/watch/{strategy_id}/tail")
def watch_tail_form(
    request: Request,
    strategy_id: int,
    allocation: str = Form(""),
    drawdown_limit_bps: str = Form(str(DEFAULT_DRAWDOWN_BPS)),
    confirm: str | None = Form(None),
    copy_on: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        raise HTTPException(status_code=404)
    w = watch_strategy_or_404(db, user, strategy_id)
    resp = start_tail_form(
        request,
        db,
        user,
        w.strategy,
        allocation,
        drawdown_limit_bps,
        confirm,
        _watch_tail_ctx,
        copy_on,
    )
    if resp.status_code != 303:  # the re-rendered form: private like the rest
        resp.headers["Cache-Control"] = "private, no-store"
    return resp
