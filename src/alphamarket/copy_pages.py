"""Copy ticket pages and JSON routes (copy_tickets.py holds the rules).

`/copy` lists the user's open tickets (soonest expiry first) and the history
with matched results; Done / Skip are forms. The portfolio form and the JSON
route change one allocation's copy settings. Every page response is
`Cache-Control: private, no-store` with a noindex meta. With
COPY_TICKETS_ENABLED off every route here is a 404.

Nothing here places an order: the "Place on <venue>" link only opens the
venue's own page (rel="noopener", new tab) for the user to act on, or not.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from alphamarket import copy_tickets, ratelimit
from alphamarket.auth import current_user
from alphamarket.db import get_db
from alphamarket.models import CopyTicketStatus, User
from alphamarket.pages import _user, login_url
from alphamarket.tails import owned_allocation
from alphamarket.watch_pages import _render_private

router = APIRouter(include_in_schema=False)
api_router = APIRouter(tags=["copy tickets"])

NOTICES = {
    "done": "Marked as placed. If you connected your account on this venue, Koine will look "
    "for the fill on its next sync.",
    "skipped": "Ticket skipped.",
    "saved": "Copy ticket settings saved.",
}


def _require_enabled() -> None:
    if not copy_tickets.enabled():
        raise HTTPException(status_code=404)


def _page(
    request: Request,
    db: Session,
    user: User,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    data = copy_tickets.user_tickets(db, user)
    return _render_private(
        request,
        "copy.html",
        status_code=status_code,
        user=user,
        notice=notice,
        error=error,
        **data,
    )


@router.get("/copy", response_class=HTMLResponse)
def copy_page(request: Request, notice: str = "", db: Session = Depends(get_db)):
    _require_enabled()
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/copy"), status_code=303)
    return _page(request, db, user, notice=NOTICES.get(notice))  # fixed texts only


def _mark(request: Request, ticket_id: int, status: str, db: Session) -> Response:
    _require_enabled()
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/copy"), status_code=303)
    ticket = copy_tickets.owned_ticket(db, user, ticket_id)
    changed = copy_tickets.mark_ticket(db, user, ticket, status)
    return RedirectResponse(f"/copy?notice={status}" if changed else "/copy", status_code=303)


@router.post("/copy/{ticket_id}/done")
def copy_done(request: Request, ticket_id: int, db: Session = Depends(get_db)):
    return _mark(request, ticket_id, CopyTicketStatus.DONE, db)


@router.post("/copy/{ticket_id}/skip")
def copy_skip(request: Request, ticket_id: int, db: Session = Depends(get_db)):
    return _mark(request, ticket_id, CopyTicketStatus.SKIPPED, db)


def _account_id(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown account") from None


@router.post("/portfolio/allocations/{allocation_id}/copy")
def copy_settings_form(
    request: Request,
    allocation_id: int,
    enabled: str = Form(""),
    email: str = Form(""),
    account_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """The portfolio's per-tail form: checkboxes `enabled` and `email`, a
    select `account_id` ("" = none). Redirects to /portfolio?notice=copy_saved;
    a refusal re-renders /copy with the message."""
    _require_enabled()
    user = _user(request, db)
    if user is None:
        return RedirectResponse(login_url("/portfolio"), status_code=303)
    allocation = owned_allocation(db, allocation_id, user)
    try:
        ratelimit.check(request, "follow_user", user.id)  # the tail routes' limit
        copy_tickets.set_copy_settings(
            db, user, allocation, bool(enabled), bool(email), _account_id(account_id)
        )
    except HTTPException as exc:
        if exc.status_code in (404, 429):
            raise
        db.rollback()
        return _page(request, db, user, error=str(exc.detail), status_code=exc.status_code)
    return RedirectResponse("/portfolio?notice=copy_saved", status_code=303)


# ---- JSON --------------------------------------------------------------------------------


class CopySettingsIn(BaseModel):
    enabled: bool
    email: bool = False
    account_id: int | None = None


@api_router.post(
    "/allocations/{allocation_id}/copy",
    dependencies=[Depends(ratelimit.rate_limit("follow_user", by="user"))],
)
def copy_settings_api(
    allocation_id: int,
    payload: CopySettingsIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    _require_enabled()
    allocation = owned_allocation(db, allocation_id, user)
    copy_tickets.set_copy_settings(
        db, user, allocation, payload.enabled, payload.email, payload.account_id
    )
    return copy_tickets.allocation_copy_context(db, user, allocation)


@api_router.get("/api/copy-tickets")
def copy_tickets_api(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    _require_enabled()
    return copy_tickets.user_tickets(db, user)
