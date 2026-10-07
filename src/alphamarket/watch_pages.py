"""HTML pages of the private watchlist (watchlist.py holds the rules).

`/watch` lists the user's watched addresses and adds new ones; `/watch/{id}`
is the strategy page of one of them, shown only to a user with a Watch row
(404 for anyone else, logged in or not); `/watch/{id}/tail` starts a paper
tail through the same handler as a public strategy's tail form. Every
response is `Cache-Control: private, no-store` and carries a noindex meta.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from alphamarket import ratelimit
from alphamarket.analytics import latest_stats
from alphamarket.charts import polyline, window_x
from alphamarket.config import get_settings
from alphamarket.connectors.registry import get_profile
from alphamarket.db import get_db
from alphamarket.models import Strategy, User
from alphamarket.pages import (
    DEFAULT_DRAWDOWN_BPS,
    _charts,
    _render,
    _tail_ctx,
    _user,
    _worst_periods,
    login_url,
    start_tail_form,
)
from alphamarket.public import INCUBATION_DAYS, SPARKLINE_DAYS, is_v2, page_data
from alphamarket.tail_terms import tailing_card
from alphamarket.tails import active_allocation
from alphamarket.watchlist import (
    AlreadyListed,
    Watched,
    WatchIn,
    add_watch,
    open_venues,
    remove_watch,
    rename_watch,
    short_address,
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
        rows=rows,
        venues=venues,
        limit=get_settings().watch_max_per_user,
        verified=user.email_verified_at is not None,
        error=error,
        listed_slug=listed_slug,
        form=form or {"venue": venues[0][0] if venues else "", "address": "", "label": ""},
        notice=notice,
    )


@router.get("/watch", response_class=HTMLResponse)
def watch_list(request: Request, notice: str = "", db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/watch"), status_code=303)
    return _list(request, db, user, notice=NOTICES.get(notice))  # fixed texts only


@router.post("/watch")
def watch_add_form(
    request: Request,
    venue: str = Form(""),
    address: str = Form(""),
    label: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/watch"), status_code=303)
    form = {"venue": venue, "address": address.strip(), "label": label}
    try:
        ratelimit.check(request, "watch_user", user.id)  # the JSON route's limit
        watched = add_watch(
            db, user, WatchIn(venue=venue, address=address, label=label[:60]), request
        )
    except AlreadyListed as exc:
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
        return _list(
            request, db, user, error=str(exc.detail), form=form, status_code=exc.status_code
        )
    return RedirectResponse(f"/watch/{watched.strategy.id}?notice=added", status_code=303)


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
    card.update(
        state="active" if active else "ready",
        href="/portfolio" if active else f"/watch/{w.strategy.id}/tail",
    )
    page["tailing"] = card
    return page


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
        page=page,
        charts=_charts(page["stats"]),
        watch=True,
        private_watch=True,
        notice=NOTICES.get(notice),
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
    return _render_private(request, "tail.html", user=user, t=t, private_watch=True)


@router.post("/watch/{strategy_id}/tail")
def watch_tail_form(
    request: Request,
    strategy_id: int,
    allocation: str = Form(""),
    drawdown_limit_bps: str = Form(str(DEFAULT_DRAWDOWN_BPS)),
    confirm: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        raise HTTPException(status_code=404)
    w = watch_strategy_or_404(db, user, strategy_id)
    resp = start_tail_form(
        request, db, user, w.strategy, allocation, drawdown_limit_bps, confirm, _watch_tail_ctx
    )
    if resp.status_code != 303:  # the re-rendered form: private like the rest
        resp.headers["Cache-Control"] = "private, no-store"
    return resp
