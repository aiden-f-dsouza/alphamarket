"""Venue consent screens (wave2, workstream w2-p8-connect; invariant 10).

Every connect, whatever the path (pasted key, JSON POST /venues, OAuth,
SnapTrade portal, public address), shows one exact consent text first and
records which text the user accepted:

consent_text(venue) -> ConsentScreen(venue, version, body, sha256)
    The exact text shown before a connect: what is read, that access is
    read-only, what becomes public, how to revoke. Built from the venue
    profile and the "Consent text" section of docs/venues/<venue>.md (when
    the venue has one); `version` names the template and the body hash;
    `sha256` is the hex SHA-256 of the UTF-8 body.

require_consent(db, user, venue, text_sha256, request) -> ConsentScreen
    Recompute the current text; a missing or different hash raises
    HTTPException(422) (the page re-renders the screen). Inserts the
    consent_texts row if absent in its own short transaction before the
    connect transaction, tolerating a concurrent first insert (append-only
    PK: INSERT ... ON CONFLICT DO NOTHING on Postgres, savepoint-and-ignore
    IntegrityError elsewhere).

record_consent(db, user, screen, account, request) -> VenueConsent
    Append the venue_consents row (ip, user agent) in the same transaction
    that creates the account, and audit VENUE_CONSENT.

Routes: every new connect route lives in connect_pages.py. pages.py's
connect functions (_connect_ctx, connect_page, connect_form) may be edited
in place, but no new routes go in pages.py (w2-p8-pages owns the rest of it).

The form posts the hash as a hidden field (`consent_sha256`), the JSON API
takes it in the body, OAuth carries it in the signed state cookie, and a
SnapTrade connect records it when the PENDING account is created (before
the user leaves for the portal). The server never trusts the hash as a
statement of WHAT was shown: it recomputes the text and only accepts a hash
that matches it, so a stale screen (the text changed after the page loaded)
is refused and re-rendered.
"""

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamarket import audit
from alphamarket.audit import AuditAction
from alphamarket.connectors.base import CredentialKind, OwnershipProof, VenueProfile
from alphamarket.connectors.registry import get_profile
from alphamarket.models import ConsentText, User, VenueAccount, VenueConsent

# Bump when the shared paragraphs below change; the body hash changes anyway,
# so this is a human-readable label, not the identity (the hash is).
TEMPLATE_VERSION = "2026-10-01"

REPO_ROOT = Path(__file__).resolve().parents[2]

STALE_DETAIL = (
    "The consent text for this venue changed or was not accepted. Read the "
    "current text and accept it to continue."
)


@dataclass(frozen=True)
class ConsentScreen:
    venue: str
    version: str
    body: str
    sha256: str

    @property
    def paragraphs(self) -> list[str]:
        """The body split for display (paragraphs are separated by a blank line)."""
        return [p for p in self.body.split("\n\n") if p.strip()]


# ---- text ------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BUTTON_RE = re.compile(r"^\[\s*[^\]]*\]$")  # "[ Continue to Alpaca ]" in a mock-up


def _plain(markdown_text: str) -> str:
    """Markdown emphasis and links to plain text: the consent body is shown
    escaped, so it carries no markup, and its hash is over what is read."""
    text = _LINK_RE.sub(r"\1 (\2)", markdown_text)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return text


@lru_cache(maxsize=32)
def _doc_paragraphs(path: str) -> tuple[str, ...]:
    """Blockquoted paragraphs of the "## Consent text..." section of a venue
    terms doc (docs/venues/<venue>.md), as plain text. Empty when the doc or
    the section is missing."""
    file = REPO_ROOT / path
    try:
        lines = file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    paragraphs: list[str] = []
    current: list[str] = []
    in_section = False
    for line in lines:
        if line.startswith("## "):
            if in_section:
                break
            in_section = line[3:].strip().lower().startswith("consent text")
            continue
        if not in_section or not line.startswith(">"):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        content = line[1:].strip()
        if not content:  # ">" alone separates quoted paragraphs
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(content)
    if current:
        paragraphs.append(" ".join(current))
    out = []
    for p in paragraphs:
        raw = p.strip()
        text = _plain(p)
        if not text or _BUTTON_RE.match(raw):
            continue
        if raw.startswith("**") and raw.endswith("**") and raw.count("**") == 2:
            continue  # a bold title line; the screen has its own heading
        out.append(text)
    return tuple(out)


def _reads(profile: VenueProfile) -> str:
    name = profile.display_name
    kind = profile.credential
    if kind is CredentialKind.ADDRESS:
        return (
            f"What Koine reads: the public {name} history of the address you enter "
            "(trades, positions, transfers and balances). Koine holds no key of any kind "
            "for this venue and cannot trade or move funds."
        )
    if kind is CredentialKind.OAUTH2:
        what = f"the read-only access you approve on {name}'s own page"
    elif kind is CredentialKind.AGGREGATOR:
        what = f"a read-only connection you make on {name}'s own page"
    elif kind is CredentialKind.REPORT_TOKEN:
        what = f"the {profile.key_label} and {profile.secret_label} you enter"
    else:
        what = f"the read-only {name} API key you enter"
    return (
        f"What Koine reads: through {what}, your fills, balances, positions and account "
        "activity (deposits, withdrawals, fees, dividends). Koine never asks for your "
        f"{name} password and never places, changes or cancels an order or moves money."
    )


def _read_only(profile: VenueProfile) -> str:
    if profile.credential is CredentialKind.ADDRESS:
        if profile.ownership is OwnershipProof.SIGNED_MESSAGE:
            proof = (
                "you sign a Koine message with your wallet (signing is not a transaction "
                "and costs nothing)"
            )
        else:
            proof = f"you put a Koine code in your {profile.display_name} profile bio"
        return (
            "Ownership: anyone can enter a public address, so your page stays hidden until "
            f"{proof}."
        )
    return (
        "Read-only: Koine checks that the access is read-only before storing anything and "
        "again every night. Access that can trade or withdraw is refused, and if it ever "
        "becomes writable it is erased and the account suspended. The credential is "
        "encrypted at rest, never shown again and never logged."
    )


def _public(profile: VenueProfile) -> str:
    return (
        "What becomes public: only when you make your strategy page public, the "
        "performance Koine computes from this data (returns, drawdown, risk measures, "
        "holdings and trade history). Numbers you type yourself are never shown as "
        "performance."
    )


def _revoke(profile: VenueProfile) -> str:
    if profile.credential is CredentialKind.ADDRESS:
        after = ""
    elif profile.credential is CredentialKind.OAUTH2:
        after = f" You can also revoke Koine's access on {profile.display_name} at any time."
    elif profile.credential is CredentialKind.AGGREGATOR:
        after = f" Disconnecting also deletes your {profile.display_name} registration."
    else:
        after = f" Delete the key on {profile.display_name} as well once you disconnect."
    return (
        "How to revoke: disconnect at any time from your Koine dashboard. Koine erases "
        "the stored credential immediately; the history already imported stays, because "
        f"it is the record your track record was built on.{after}"
    )


def _build(profile: VenueProfile) -> str:
    head = f"Connect {profile.display_name} to Koine"
    venue_part = list(_doc_paragraphs(profile.tos_doc)) if profile.tos_doc else []
    parts = [
        head,
        *venue_part,
        _reads(profile),
        _read_only(profile),
        _public(profile),
        _revoke(profile),
    ]
    return "\n\n".join(parts)


def consent_text(venue: str) -> ConsentScreen:
    """The current consent screen for `venue` (ValueError for an unknown venue)."""
    profile = get_profile(venue)
    body = _build(profile)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return ConsentScreen(
        venue=venue, version=f"{TEMPLATE_VERSION}.{digest[:8]}", body=body, sha256=digest
    )


# ---- acceptance --------------------------------------------------------------------


def _store_text(db: Session, screen: ConsentScreen) -> None:
    """Insert the consent_texts row unless it exists, in its own committed
    transaction; a concurrent first insert of the same body is not an error."""
    if db.get(ConsentText, screen.sha256) is not None:
        return
    values = {
        "sha256": screen.sha256,
        "venue": screen.venue,
        "version": screen.version,
        "body": screen.body,
    }
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        # Both speak INSERT ... ON CONFLICT DO NOTHING (pysqlite savepoints are
        # unreliable, so SQLite uses the same statement as Postgres).
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        db.execute(
            insert(ConsentText).values(**values).on_conflict_do_nothing(index_elements=["sha256"])
        )
    else:
        try:
            with db.begin_nested():
                db.add(ConsentText(**values))
        except IntegrityError:
            pass  # another request stored the same text first
    db.commit()


def require_consent(
    db: Session,
    user: User,
    venue: str,
    text_sha256: str | None,
    request: Request | None = None,
) -> ConsentScreen:
    """The current screen, if `text_sha256` is its hash; else HTTPException(422)."""
    screen = consent_text(venue)
    given = (text_sha256 or "").strip().lower()
    if not given or given != screen.sha256:
        raise HTTPException(status_code=422, detail=STALE_DETAIL)
    _store_text(db, screen)
    return screen


def _client(request: Request | None) -> tuple[str | None, str | None]:
    ctx = audit.current_context()
    ip = ctx.ip
    ua = ctx.user_agent
    if request is not None:
        if ip is None and request.client is not None:
            ip = request.client.host
        if ua is None:
            ua = request.headers.get("user-agent")
    return (ip[:64] if ip else None), (ua[:400] if ua else None)


def record_consent(
    db: Session,
    user: User,
    screen: ConsentScreen,
    account: VenueAccount,
    request: Request | None = None,
) -> VenueConsent:
    """Append the acceptance row and its audit row. Not committed: the caller
    commits it together with the account it belongs to."""
    ip, ua = _client(request)
    row = VenueConsent(
        user_id=user.id,
        venue=screen.venue,
        venue_account_id=account.id,
        text_sha256=screen.sha256,
        text_version=screen.version,
        ip=ip,
        user_agent=ua,
    )
    db.add(row)
    db.flush()
    audit.record(
        db,
        AuditAction.VENUE_CONSENT,
        actor_user_id=user.id,
        target_type="venue_account",
        target_id=account.id,
        details={
            "venue": screen.venue,
            # The full hash is on the venue_consents row (consent_id); a 64-hex
            # value would be scrubbed from audit details as secret-shaped.
            "text_version": screen.version,
            "consent_id": row.id,
        },
        ip=ip,
        user_agent=ua,
    )
    return row


def consent_for(db: Session, account: VenueAccount) -> VenueConsent | None:
    """The consent row recorded for `account` (a PENDING SnapTrade connect
    records it before the portal round trip)."""
    return db.scalar(
        select(VenueConsent)
        .where(VenueConsent.venue_account_id == account.id)
        .order_by(VenueConsent.id.desc())
        .limit(1)
    )
