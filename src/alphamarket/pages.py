"""HTML surface (M4): landing/login, creator dashboard, public strategy page,
methodology. Jinja templates, plain forms, Chart.js on the public page.

Everything numeric shown here comes from strategy_stats / equity_snapshots
(platform-computed, invariant #5). Forms delegate to the same functions the
JSON API uses, so there is one code path per action.
"""

from pathlib import Path

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import auth
from alphamarket.analytics import latest_stats
from alphamarket.config import get_settings
from alphamarket.db import get_db
from alphamarket.ingest import sync_account
from alphamarket.models import EquitySnapshot, Strategy, User, VenueAccount, VenueAccountStatus
from alphamarket.public import is_visible, page_data, visible_strategy_or_404
from alphamarket.reconcile import reconcile_account
from alphamarket.strategies import ListingIn, update_listing
from alphamarket.venues import ConnectIn, _owned_account, connect_account

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
METHODOLOGY_MD = Path(__file__).resolve().parents[2] / "docs" / "methodology.md"


def _user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    return db.get(User, user_id) if user_id else None


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("settings", get_settings())
    return templates.TemplateResponse(request, name, ctx)


# ---- landing / auth ----------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    if _user(request, db):
        return RedirectResponse("/dashboard", status_code=303)
    return _render(request, "index.html", error=None)


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
        return _render(request, "index.html", error=exc.detail)
    except ValueError as exc:  # pydantic validation
        return _render(request, "index.html", error=str(exc))
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/login-form")
def login_form(
    request: Request,
    email: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        auth.login(auth.LoginIn(email=email, password=password), request, db)
    except (HTTPException, ValueError):
        return _render(request, "index.html", error="Invalid credentials")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout-form")
def logout_form(request: Request):
    request.session.clear()
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
                "visible": is_visible(strategy) if strategy else False,
            }
        )
    return {"user": user, "rows": rows}


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    return _render(request, "dashboard.html", error=None, **_dashboard_ctx(db, user))


@router.post("/dashboard/connect")
def connect_form(
    request: Request,
    key_id: str = Form(),
    private_key_pem: str = Form(),
    strategy_name: str = Form(),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    try:
        connect_account(
            db,
            user,
            ConnectIn(key_id=key_id, private_key_pem=private_key_pem, strategy_name=strategy_name),
        )
    except HTTPException as exc:
        return _render(request, "dashboard.html", error=exc.detail, **_dashboard_ctx(db, user))
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
        return RedirectResponse("/", status_code=303)
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
        return RedirectResponse("/", status_code=303)
    account = _owned_account(db, account_id, user)
    error = None
    try:
        if action == "sync":
            sync_account(db, account)
        elif action == "reconcile":
            reconcile_account(db, account)
        elif action == "disconnect":
            account.encrypted_private_key = None
            account.status = VenueAccountStatus.DISCONNECTED
            db.commit()
        else:
            raise HTTPException(status_code=404)
    except HTTPException:
        raise
    except Exception as exc:  # recorded on the account row by sync/reconcile
        error = f"{action} failed: {type(exc).__name__}"
    if error:
        return _render(request, "dashboard.html", error=error, **_dashboard_ctx(db, user))
    return RedirectResponse("/dashboard", status_code=303)


# ---- public ---------------------------------------------------------------------


@router.get("/s/{slug}", response_class=HTMLResponse)
def strategy_page(request: Request, slug: str, db: Session = Depends(get_db)):
    strategy = visible_strategy_or_404(db, slug)
    return _render(request, "strategy.html", page=page_data(db, strategy))


@router.get("/methodology", response_class=HTMLResponse)
def methodology(request: Request):
    body = markdown.markdown(METHODOLOGY_MD.read_text(), extensions=["tables", "fenced_code"])
    return _render(request, "methodology.html", body=body)
