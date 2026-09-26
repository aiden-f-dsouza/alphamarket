"""HTML for account security (P3): the login second step, /dashboard/security
(email, two-factor, recovery codes, sessions, password), forgot/reset
password and the email-verification landing page.

Plain forms, no client script (the CSP work keeps templates script-free).
Every action reuses the JSON-route logic in auth.py, mfa.py and account.py,
so both surfaces enforce the same limits.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from alphamarket import account, mfa, ratelimit, sessions
from alphamarket.auth import optional_user, totp_enabled
from alphamarket.db import get_db
from alphamarket.models import User
from alphamarket.pages import _render

router = APIRouter(include_in_schema=False)


def _safe_next(next_url: str | None, default: str = "/dashboard/security") -> str:
    """Only same-site paths: never an open redirect."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default


def _status(exc: HTTPException) -> int:
    return exc.status_code if exc.status_code in (401, 403, 409, 422, 429) else 400


# ---- login, second step -------------------------------------------------------


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, db: Session = Depends(get_db)):
    if sessions.pending(db, request) is None:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "login_2fa.html", user=None, error=None)


@router.post("/login/2fa")
def login_2fa(request: Request, code: str = Form(), db: Session = Depends(get_db)):
    try:
        mfa.complete_login(request, db, code)
    except HTTPException as exc:
        if sessions.pending(db, request) is None:
            return RedirectResponse("/login", status_code=303)
        resp = _render(request, "login_2fa.html", user=None, error=str(exc.detail))
        resp.status_code = _status(exc)
        return resp
    return RedirectResponse("/dashboard", status_code=303)


# ---- /dashboard/security --------------------------------------------------------


def _security_ctx(request: Request, db: Session, user: User, **extra) -> dict:
    current = sessions.load(db, request)
    rows = sessions.active_sessions(db, user.id)
    return {
        "user": user,
        "email_verified": user.email_verified_at is not None,
        "totp_on": totp_enabled(db, user),
        "recovery_left": mfa.remaining_recovery_codes(db, user),
        "stepped_up": sessions.has_recent_step_up(current),
        "sessions": [
            {
                "id": s.id,
                "ip": s.ip or "unknown",
                "agent": s.user_agent or "unknown device",
                "created": sessions.aware(s.created_at),
                "seen": sessions.aware(s.last_seen_at),
                "current": current is not None and s.id == current.id,
            }
            for s in rows
        ],
        "next": _safe_next(request.query_params.get("next"), ""),
        "notice": None,
        "error": None,
        **extra,
    }


def _page(request: Request, db: Session, user: User, status_code: int = 200, **extra):
    resp = _render(request, "account_security.html", **_security_ctx(request, db, user, **extra))
    resp.status_code = status_code
    return resp


def _need_user(request: Request, db: Session) -> User | None:
    return optional_user(request, db)


@router.get("/dashboard/security", response_class=HTMLResponse)
def security_page(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _page(request, db, user)


@router.post("/dashboard/security/totp/enroll", response_class=HTMLResponse)
def totp_enroll(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        secret = mfa.start_enrolment(db, user)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _enroll_page(request, user, secret)


def _enroll_page(request: Request, user: User, secret: str, error: str | None = None, code=200):
    uri = mfa.provisioning_uri(secret, user.email)
    resp = _render(
        request,
        "totp_enroll.html",
        user=user,
        qr=mfa.qr_svg(uri),
        secret=" ".join(secret[i : i + 4] for i in range(0, len(secret), 4)),
        error=error,
    )
    resp.status_code = code
    return resp


@router.post("/dashboard/security/totp/confirm", response_class=HTMLResponse)
def totp_confirm(request: Request, code: str = Form(), db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        codes = mfa.confirm_enrolment(request, db, user, code)
    except HTTPException as exc:
        secret = mfa.pending_secret(db, user)
        if secret is None:
            return _page(request, db, user, _status(exc), error=exc.detail)
        return _enroll_page(request, user, secret, str(exc.detail), _status(exc))
    return _render(request, "recovery_codes.html", user=user, codes=codes, fresh=True)


@router.post("/dashboard/security/step-up")
def step_up_form(
    request: Request,
    code: str = Form(),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        mfa.step_up(request, db, user, code)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/dashboard/security/totp/disable")
def totp_disable_form(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if code:
            mfa.step_up(request, db, user, code)
        mfa.ensure_step_up(request, db, user)
        mfa.disable(request, db, user)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _page(request, db, user, notice="Two-factor authentication is off.")


@router.post("/dashboard/security/recovery-codes", response_class=HTMLResponse)
def recovery_codes_form(request: Request, code: str = Form(""), db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if code:
            mfa.step_up(request, db, user, code)
        mfa.ensure_step_up(request, db, user)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    codes = mfa.replace_recovery_codes(db, user)
    return _render(request, "recovery_codes.html", user=user, codes=codes, fresh=False)


@router.post("/dashboard/security/sessions/revoke-all")
def revoke_all_form(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is not None:
        sessions.revoke_all(db, user.id)
    sessions.revoke_current(db, request)
    return RedirectResponse("/login", status_code=303)


@router.post("/dashboard/security/sessions/{session_id}/revoke")
def revoke_one_form(request: Request, session_id: int, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    sessions.revoke_one(db, user.id, session_id)
    return RedirectResponse("/dashboard/security", status_code=303)


@router.post("/dashboard/security/verify-email/resend")
def resend_form(request: Request, db: Session = Depends(get_db)):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.email_verified_at is None:
        try:
            ratelimit.check(request, "verify_resend_user", user.id)
        except HTTPException as exc:
            return _page(request, db, user, _status(exc), error=exc.detail)
        account.send_verification_email(db, user)
    return _page(request, db, user, notice=f"We sent a new link to {user.email}.")


@router.post("/dashboard/security/password")
def change_password_form(
    request: Request,
    current_password: str = Form(),
    new_password: str = Form(),
    db: Session = Depends(get_db),
):
    user = _need_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    try:
        if not 10 <= len(new_password) <= 128:
            raise HTTPException(status_code=422, detail="Use 10 to 128 characters.")
        account.change_password(request, db, user, current_password, new_password)
    except HTTPException as exc:
        return _page(request, db, user, _status(exc), error=exc.detail)
    return _page(request, db, user, notice="Password changed. Other sessions were signed out.")


# ---- email verification, forgot / reset password ----------------------------------


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = account.verify_email(db, token) if token else None
    return _render(
        request,
        "account_notice.html",
        user=optional_user(request, db),
        title="Email confirmed" if user else "Link expired",
        heading="Email confirmed." if user else "This link has expired.",
        body=(
            "Thanks. You can now connect a venue once two-factor authentication is on."
            if user
            else "Verification links work once and last 48 hours. "
            "Send a new one from your security settings."
        ),
        cta=("/dashboard/security", "Security settings"),
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_page(request: Request):
    return _render(request, "password_reset.html", user=None, mode="request", error=None)


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_form(request: Request, email: str = Form(), db: Session = Depends(get_db)):
    try:
        account.request_password_reset(request, db, email.strip())
    except HTTPException as exc:
        resp = _render(request, "password_reset.html", user=None, mode="request", error=exc.detail)
        resp.status_code = _status(exc)
        return resp
    return _render(
        request,
        "account_notice.html",
        user=None,
        title="Check your email",
        heading="Check your email.",
        body=account.RESET_REQUESTED + " The link lasts 1 hour.",
        cta=("/login", "Back to log in"),
    )


@router.get("/reset-password", response_class=HTMLResponse)
def reset_page(request: Request, token: str = ""):
    return _render(request, "password_reset.html", user=None, mode="reset", token=token, error=None)


@router.post("/reset-password", response_class=HTMLResponse)
def reset_form(
    request: Request,
    token: str = Form(),
    password: str = Form(),
    db: Session = Depends(get_db),
):
    try:
        if not 10 <= len(password) <= 128:
            raise HTTPException(status_code=422, detail="Use 10 to 128 characters.")
        account.reset_password(db, token, password)
    except HTTPException as exc:
        resp = _render(
            request, "password_reset.html", user=None, mode="reset", token=token, error=exc.detail
        )
        resp.status_code = _status(exc)
        return resp
    sessions.revoke_current(db, request)
    return _render(
        request,
        "account_notice.html",
        user=None,
        title="Password changed",
        heading="Password changed.",
        body="Every session was signed out. Log in with your new password.",
        cta=("/login", "Log in"),
    )
