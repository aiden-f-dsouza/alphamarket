"""Private watchlist (docs/copy_strategies_plan.md section 2).

A logged-in user adds a public Hyperliquid or Polymarket address; Koine
ingests its public fills through the normal pipeline and the user can see its
stats and paper-tail it. Nothing about the address reaches a public surface
(invariant 4): its strategy has kind "watch", which `public.is_visible`
refuses, and every private route goes through `watch_strategy_or_404`.

One shared venue account per (venue, address), owned by the system user,
never claimed, no credential: two watchers share one ingestion. A `Watch`
row links each user to it with their own private label.

Adding a watch is not a venue connect: there is no credential and no venue
consent, so the gate is a verified email, a per-user cap, a global cap on
tracked addresses and a rate limit (decision 3 in
docs/copy_strategies_implementation.md). An address whose owner opted out
(`watch_optouts`) is refused. Never link an address to an identity: no ENS,
no social handles, no cross-venue matching.
"""

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamarket import audit
from alphamarket.auth import current_user
from alphamarket.config import get_settings
from alphamarket.connectors.base import OwnershipProof
from alphamarket.connectors.registry import build_connector, get_profile
from alphamarket.db import get_db
from alphamarket.ingest import is_asset_venue
from alphamarket.jobs import JobKind, enqueue
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    Strategy,
    StrategyKind,
    User,
    VenueAccount,
    VenueAccountStatus,
    Watch,
    WatchOptout,
)
from alphamarket.public import publicly_visible, visible_strategy_or_404
from alphamarket.ratelimit import rate_limit
from alphamarket.reconcile import store_anchor_balances, take_anchor
from alphamarket.scopecheck import apply_evidence
from alphamarket.venue_limits import VenueLimiter, get_web_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watch", tags=["watchlist"])

# Venues with public per-address data. Which of them are open is a setting
# (`watch_venues`): see open_venues.
WATCHABLE_VENUES = ("hyperliquid", "polymarket")
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
LABEL_MAX = 60
# The system user's address fails email validation (a reserved TLD), so
# nobody can sign up or log in as it; auth refuses `is_system` users anyway.
SYSTEM_EMAIL = "system@koine.invalid"
UNUSABLE_PASSWORD_HASH = "!"  # no bcrypt hash starts with "!": nothing verifies

NOT_FOUND = "Not on your watchlist"
OPTED_OUT = "The owner of this address has asked not to be tracked."
VERIFY_FIRST = "Verify your email address before adding to your watchlist."
FULL = "The watchlist is not taking new addresses right now. Try again later."


class AlreadyListed(Exception):
    """The address is a claimed, public Koine strategy: the user should follow
    or tail it there, not watch it privately."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


class WatchIn(BaseModel):
    venue: str
    address: str
    label: str = Field(default="", max_length=LABEL_MAX)


class LabelIn(BaseModel):
    label: str = Field(default="", max_length=LABEL_MAX)


@dataclass(frozen=True)
class Watched:
    strategy: Strategy
    account: VenueAccount
    watch: Watch


# ---- helpers -----------------------------------------------------------------


def system_user(db: Session) -> User:
    """The one user that owns shared watch accounts (get or create; flushed,
    not committed)."""
    user = db.scalar(select(User).where(User.is_system.is_(True)).order_by(User.id).limit(1))
    if user is None:
        user = User(
            email=SYSTEM_EMAIL,
            password_hash=UNUSABLE_PASSWORD_HASH,
            display_name="Koine watchlist",
            is_system=True,
        )
        db.add(user)
        db.flush()
    return user


def open_venues() -> tuple[str, ...]:
    """The watchable venues the settings open, in WATCHABLE_VENUES order."""
    wanted = {v.strip() for v in get_settings().watch_venues.split(",") if v.strip()}
    return tuple(v for v in WATCHABLE_VENUES if v in wanted)


def normalize_address(venue: str, raw: str) -> str:
    """Lowercased `0x` + 40 hex for an open watchable venue; 422 otherwise."""
    venues = open_venues()
    if venue not in venues:
        names = " and ".join(get_profile(v).display_name for v in venues) or "no"
        raise HTTPException(status_code=422, detail=f"Only {names} addresses can be watched")
    address = (raw or "").strip().lower()
    if not ADDRESS_RE.match(address):
        raise HTTPException(status_code=422, detail="Enter a 0x address (42 characters)")
    return address


def short_address(address: str) -> str:
    """0x12ab…89cd: enough to tell addresses apart on a page."""
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def clean_label(label: str | None) -> str:
    return " ".join((label or "").split())[:LABEL_MAX]


def opted_out(db: Session, venue: str, address: str) -> bool:
    return (
        db.scalar(
            select(WatchOptout.id).where(WatchOptout.venue == venue, WatchOptout.address == address)
        )
        is not None
    )


def listed_strategy(db: Session, venue: str, address: str) -> Strategy | None:
    """A claimed creator account for this address whose strategy is public."""
    rows = db.execute(
        select(Strategy, VenueAccount)
        .join(VenueAccount, VenueAccount.id == Strategy.venue_account_id)
        .where(
            VenueAccount.venue == venue,
            func.lower(VenueAccount.key_id) == address,
            VenueAccount.is_watch.is_(False),
            VenueAccount.claimed_at.is_not(None),
        )
    ).all()
    for strategy, account in rows:
        if publicly_visible(db, strategy, account):
            return strategy
    return None


def watch_account(db: Session, venue: str, address: str) -> VenueAccount | None:
    return db.scalar(
        select(VenueAccount).where(
            VenueAccount.is_watch.is_(True),
            VenueAccount.venue == venue,
            VenueAccount.key_id == address,
        )
    )


def strategy_of(db: Session, account: VenueAccount) -> Strategy | None:
    return db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))


def watch_count(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count(Watch.id)).where(Watch.user_id == user_id)) or 0


def tracked_count(db: Session) -> int:
    """Watch accounts someone still watches: what the global cap counts."""
    return (
        db.scalar(
            select(func.count(func.distinct(Strategy.venue_account_id))).join(
                Watch, Watch.strategy_id == Strategy.id
            )
        )
        or 0
    )


def user_watch(db: Session, user_id: int, strategy_id: int) -> Watch | None:
    return db.scalar(
        select(Watch).where(Watch.user_id == user_id, Watch.strategy_id == strategy_id)
    )


# ---- private lookups ------------------------------------------------------------


def watch_strategy_or_404(db: Session, user: User, strategy_id: int) -> Watched:
    """The user's own watch of this strategy, else 404 (the same 404 for "no
    such strategy" and "not yours": ids are not confirmed to strangers)."""
    watch = user_watch(db, user.id, strategy_id)
    strategy = db.get(Strategy, strategy_id) if watch is not None else None
    if strategy is None or strategy.kind != StrategyKind.WATCH:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return Watched(strategy, db.get(VenueAccount, strategy.venue_account_id), watch)


def tailable_strategy_or_404(db: Session, user: User, slug: str) -> Strategy:
    """What a user may paper-tail: a publicly visible strategy, or a watched
    one on their own watchlist. Everything after the allocation is created
    is the same engine."""
    strategy = db.scalar(select(Strategy).where(Strategy.slug == slug))
    if strategy is not None and strategy.kind == StrategyKind.WATCH:
        if user_watch(db, user.id, strategy.id) is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
        return strategy
    return visible_strategy_or_404(db, slug)


# ---- add / remove ---------------------------------------------------------------


def _track(db: Session, venue: str, address: str, actor: User, request: Request | None):
    """Create the shared watch account and its strategy: one venue call unit
    (the address exists, the anchor), exactly the anchoring a connect does,
    with no credential, no consent row and no claim code. Then queue the
    first import (asset venues: a full backfill)."""
    from alphamarket.venues import _verify_new_credential, web_venue_call  # venues imports us

    venue_env = get_settings().venue_env(venue)

    def verify_and_anchor(throttle):
        connector, evidence = _verify_new_credential(
            db,
            actor,
            venue,
            venue_env,
            address,
            None,
            None,
            reject_target=("watch_address", venue),
            throttle=throttle,
        )
        return evidence, take_anchor(connector)

    limiter = get_web_limiter(request.app) if request is not None else None
    evidence, anchor = web_venue_call(limiter, venue, address, verify_and_anchor)

    owner = system_user(db)
    account = VenueAccount(
        user_id=owner.id,
        venue=venue,
        venue_env=venue_env,
        key_id=address,
        encrypted_private_key=None,
        status=VenueAccountStatus.ACTIVE,
        anchor_balance_micros=anchor.balance_micros,
        anchor_at=anchor.at,
        anchor_positions=anchor.positions,
        claim_code=None,
        claimed_at=None,
        is_watch=True,
    )
    apply_evidence(account, evidence)
    db.add(account)
    db.flush()
    store_anchor_balances(db, account, anchor)
    strategy = Strategy(
        user_id=owner.id,
        venue_account_id=account.id,
        name=f"{get_profile(venue).display_name} {short_address(address)}",
        slug="pending",
        kind=StrategyKind.WATCH,
        public=False,
    )
    db.add(strategy)
    db.flush()
    # The slug never carries the address.
    strategy.slug = f"watch-{strategy.id}"
    enqueue(db, JobKind.BACKFILL if is_asset_venue(venue) else JobKind.SYNC, account_id=account.id)
    return account, strategy


def add_watch(db: Session, user: User, payload: WatchIn, request: Request | None = None) -> Watched:
    """Add an address to the user's watchlist. Raises HTTPException with a
    user-safe detail, or AlreadyListed when it is a public Koine strategy."""
    settings = get_settings()
    if settings.demo_mode:  # before anything touches a venue or stores an unflagged address
        raise HTTPException(status_code=403, detail="Adding addresses is off in the demo.")
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail=VERIFY_FIRST)
    venue = payload.venue
    address = normalize_address(venue, payload.address)
    if opted_out(db, venue, address):
        raise HTTPException(status_code=422, detail=OPTED_OUT)
    listed = listed_strategy(db, venue, address)
    if listed is not None:
        raise AlreadyListed(listed.slug)

    account = watch_account(db, venue, address)
    strategy = strategy_of(db, account) if account is not None else None
    if strategy is not None and user_watch(db, user.id, strategy.id) is not None:
        raise HTTPException(status_code=409, detail="Already on your watchlist")
    if watch_count(db, user.id) >= settings.watch_max_per_user:
        raise HTTPException(
            status_code=422,
            detail=f"You can watch up to {settings.watch_max_per_user} addresses. "
            "Remove one to add another.",
        )
    if strategy is None:
        if tracked_count(db) >= settings.watch_max_tracked:
            raise HTTPException(status_code=503, detail=FULL)
        try:
            account, strategy = _track(db, venue, address, user, request)
            db.commit()
        except IntegrityError:
            # Another watcher created the shared account between our read and
            # our insert: use theirs.
            db.rollback()
            account = watch_account(db, venue, address)
            strategy = strategy_of(db, account) if account is not None else None
            if strategy is None:
                raise
    watch = Watch(user_id=user.id, strategy_id=strategy.id, label=clean_label(payload.label))
    db.add(watch)
    audit.record(
        db,
        audit.AuditAction.WATCH_ADD,
        actor_user_id=user.id,
        target_type="strategy",
        target_id=strategy.id,
        details={"venue": venue},
    )
    try:
        db.commit()
    except IntegrityError:  # a double submit: the first one stands
        db.rollback()
        raise HTTPException(status_code=409, detail="Already on your watchlist") from None
    return Watched(strategy, account, watch)


def remove_watch(db: Session, user: User, strategy_id: int) -> None:
    """Delete the user's watch and stop their paper tail of it. The shared
    account and its ledger stay (append-only); with no watcher and no tail
    left, the scheduler stops polling it."""
    from alphamarket.tails import stop_by_user  # tails imports us

    watched = watch_strategy_or_404(db, user, strategy_id)
    allocation = db.scalar(
        select(Allocation).where(
            Allocation.user_id == user.id,
            Allocation.strategy_id == strategy_id,
            Allocation.status == AllocationStatus.ACTIVE,
        )
    )
    if allocation is not None:
        stop_by_user(db, allocation)
    db.delete(watched.watch)
    audit.record(
        db,
        audit.AuditAction.WATCH_REMOVE,
        actor_user_id=user.id,
        target_type="strategy",
        target_id=strategy_id,
    )
    db.commit()


def rename_watch(db: Session, user: User, strategy_id: int, label: str) -> Watch:
    watched = watch_strategy_or_404(db, user, strategy_id)
    watched.watch.label = clean_label(label)
    db.commit()
    return watched.watch


def watched_for(db: Session, user: User) -> list[Watched]:
    """The user's watches, newest first."""
    rows = db.execute(
        select(Watch, Strategy, VenueAccount)
        .join(Strategy, Strategy.id == Watch.strategy_id)
        .join(VenueAccount, VenueAccount.id == Strategy.venue_account_id)
        .where(Watch.user_id == user.id)
        .order_by(Watch.created_at.desc(), Watch.id.desc())
    ).all()
    return [Watched(strategy, account, watch) for watch, strategy, account in rows]


def watch_labels(db: Session, user_id: int) -> dict[int, str]:
    """{strategy_id: label or short address} for the user's watches: how the
    portfolio and feed name a watched strategy to its watcher."""
    rows = db.execute(
        select(Watch.strategy_id, Watch.label, VenueAccount.key_id)
        .join(Strategy, Strategy.id == Watch.strategy_id)
        .join(VenueAccount, VenueAccount.id == Strategy.venue_account_id)
        .where(Watch.user_id == user_id)
    ).all()
    return {sid: label or short_address(addr) for sid, label, addr in rows}


# ---- JSON API -----------------------------------------------------------------


def watch_out(w: Watched) -> dict:
    return {
        "strategy_id": w.strategy.id,
        "slug": w.strategy.slug,
        "venue": w.account.venue,
        "address": w.account.key_id,
        "label": w.watch.label,
        "status": w.strategy.status,
        "created_at": w.watch.created_at.isoformat(),
    }


@router.get("")
def list_watches(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return {
        "watches": [watch_out(w) for w in watched_for(db, user)],
        "limit": get_settings().watch_max_per_user,
    }


@router.get("/discover")
def discover_watches(
    venue: str = "",
    period: str = "",
    sort: str = "pnl",
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """The /watch/discover view as JSON: the venue's own leaderboard figures for
    the period (not Koine-verified), with the user's state per address."""
    from alphamarket import discover  # discover imports this module

    if not discover.enabled():
        raise HTTPException(status_code=404)
    view = discover.discover_view(db, user, venue, period, sort)
    rows = []
    for r in view.rows:
        row = {
            "address": r.address,
            "rank": r.rank,
            "pnl_micros": r.pnl_micros,
            "roi": r.roi,
            "volume_micros": r.volume_micros,
            "account_value_micros": r.account_value_micros,
            "state": r.state,
        }
        if r.watch_strategy_id is not None:
            row["watch_strategy_id"] = r.watch_strategy_id
        if r.listed_slug is not None:
            row["listed_slug"] = r.listed_slug
        rows.append(row)
    return {
        "venue": view.venue,
        "period": view.period,
        "sort": view.sort,
        "venues": list(view.venues),
        "periods": list(view.periods),
        "fetched_at": view.fetched_at.isoformat() if view.fetched_at else None,
        "watch_count": view.watch_count,
        "watch_max": view.watch_max,
        "rows": rows,
    }


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("watch_user", by="user"))],
)
def create_watch(
    payload: WatchIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    try:
        return watch_out(add_watch(db, user, payload, request))
    except AlreadyListed as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "This address is a verified Koine strategy", "slug": exc.slug},
        ) from None


@router.patch("/{strategy_id}")
def relabel_watch(
    strategy_id: int,
    payload: LabelIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    rename_watch(db, user, strategy_id, payload.label)
    return watch_out(watch_strategy_or_404(db, user, strategy_id))


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watch(
    strategy_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    remove_watch(db, user, strategy_id)


# ---- opt-out (B6) ---------------------------------------------------------------
#
# The owner of an address can ask not to be tracked. They prove the address the
# way a creator claims it (venues.claim_account): a wallet signature over
# connector.claim_text(code) for Hyperliquid, a code in the profile bio for
# Polymarket. On proof, in ONE transaction: a `watch_optouts` row (append-only,
# the record we must keep), every Watch on the address deleted for all users,
# every ACTIVE tail of it stopped, and an audit row. With no watch and no tail
# the account is dormant (jobs.py reads that from the data; nothing to flag).
# Polymarket opt-out works even while Polymarket watches are closed
# (`watch_venues`): the record must exist before the venue opens.

OPTOUT_VERIFY_FIRST = "Verify your email address before opting an address out."
OPTOUT_DEMO = "Opting out is off in the demo."


@dataclass(frozen=True)
class OptoutClaim:
    """What the proof step shows: the code issued for this user + address, and
    for a signed-message venue the exact text to sign."""

    venue: str
    address: str
    display_name: str
    code: str
    message: str | None  # None: profile-code venue (the bio holds the code)
    already: bool  # the address is already opted out


@dataclass(frozen=True)
class OptoutResult:
    venue: str
    address: str
    already: bool
    watches_removed: int = 0
    tails_stopped: int = 0


def optout_code(user_id: int, venue: str, address: str) -> str:
    """The claim code for one (user, venue, address): HMAC-SHA256 under the
    server's secret key, so it is issued and held by the server only. Nothing
    the client posts is ever read back as a code (the venue claim's replay
    rule): every request re-derives it, and a signature or bio code made for
    another user or another address does not match. No table: the opt-out
    holds no state until the proof succeeds."""
    msg = f"watch-optout|{user_id}|{venue}|{address}".encode()
    digest = hmac.new(get_settings().secret_key.encode(), msg, hashlib.sha256).hexdigest()
    return "koine-optout-" + digest[:12]


def normalize_optout_address(venue: str, raw: str) -> str:
    """Like normalize_address, but for every watchable venue whether or not
    watches are open for it (an owner may opt out ahead of the launch)."""
    if venue not in WATCHABLE_VENUES:
        names = " and ".join(get_profile(v).display_name for v in WATCHABLE_VENUES)
        raise HTTPException(status_code=422, detail=f"Only {names} addresses can opt out")
    address = (raw or "").strip().lower()
    if not ADDRESS_RE.match(address):
        raise HTTPException(status_code=422, detail="Enter a 0x address (42 characters)")
    return address


def optout_gate(user: User) -> None:
    """403 unless the user may opt an address out (not in the demo; verified email)."""
    if get_settings().demo_mode or user.is_demo:
        raise HTTPException(status_code=403, detail=OPTOUT_DEMO)
    if user.email_verified_at is None:
        raise HTTPException(status_code=403, detail=OPTOUT_VERIFY_FIRST)


def start_optout(db: Session, user: User, venue: str, raw_address: str) -> OptoutClaim:
    """Step one: validate and issue the proof step. No venue call."""
    optout_gate(user)
    address = normalize_optout_address(venue, raw_address)
    profile = get_profile(venue)
    code = optout_code(user.id, venue, address)
    message = None
    if profile.ownership is OwnershipProof.SIGNED_MESSAGE:
        connector = build_connector(venue, get_settings().venue_env(venue), address, None)
        message = connector.claim_text(code)
    return OptoutClaim(
        venue=venue,
        address=address,
        display_name=profile.display_name,
        code=code,
        message=message,
        already=opted_out(db, venue, address),
    )


def _prove_address(limiter: VenueLimiter | None, claim: OptoutClaim, signature: str | None) -> None:
    """Raise unless the proof holds. The venue call goes through the outbound
    limiter (venues.web_venue_call): a busy venue is a 503 saying so, never a
    "failed" proof."""
    from alphamarket.venues import web_venue_call  # venues imports us

    profile = get_profile(claim.venue)
    connector = build_connector(claim.venue, get_settings().venue_env(claim.venue), claim.address)
    if profile.ownership is OwnershipProof.SIGNED_MESSAGE:
        sig = (signature or "").strip()
        if not sig:
            raise HTTPException(
                status_code=422,
                detail="Sign the Koine message with your wallet, then submit the signature.",
            )
        payload = connector.claim_payload(claim.code, sig)  # the SERVER's code
    else:
        payload = claim.code

    def call(throttle):
        if throttle is not None:
            connector.set_throttle(throttle)
        return connector.verify_claim(payload)

    found = web_venue_call(limiter, claim.venue, claim.address, call)
    if found is None:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach {claim.display_name} to check the proof; try again.",
        )
    if not found:
        if claim.message is not None:
            detail = (
                "That signature does not match the Koine message for this address. "
                "Sign the exact message shown, with the wallet that controls the address."
            )
        else:
            detail = (
                f"Code {claim.code} was not found in the profile bio. "
                "Save the bio on the venue first, then try again."
            )
        raise HTTPException(status_code=422, detail=detail)


def perform_optout(
    db: Session,
    user: User,
    venue: str,
    raw_address: str,
    *,
    signature: str | None = None,
    limiter: VenueLimiter | None = None,
) -> OptoutResult:
    """Prove the address, then opt it out (idempotent: an address already opted
    out answers `already` without a venue call). One transaction for the
    record, the watches, the tails and the audit row. Then each stopped
    crypto/stock tail closes inline, as a manual Stop does
    (paper_lifecycle.close_now); a close that fails is retried by the next
    mirror pass."""
    from alphamarket.paper_lifecycle import close_now
    from alphamarket.portfolio import stop_allocation  # portfolio imports us

    claim = start_optout(db, user, venue, raw_address)
    if claim.already:
        return OptoutResult(venue, claim.address, already=True)
    _prove_address(limiter, claim, signature)

    optout = WatchOptout(venue=venue, address=claim.address, proved_by_user_id=user.id)
    db.add(optout)
    watches_removed = 0
    stopped: list[Allocation] = []
    account = watch_account(db, venue, claim.address)
    strategy = strategy_of(db, account) if account is not None else None
    if strategy is not None:
        watches_removed = db.execute(delete(Watch).where(Watch.strategy_id == strategy.id)).rowcount
        for allocation in db.scalars(
            select(Allocation).where(
                Allocation.strategy_id == strategy.id,
                Allocation.status == AllocationStatus.ACTIVE,
            )
        ).all():
            stop_allocation(db, allocation, "optout", commit=False)
            stopped.append(allocation)
    try:
        db.flush()  # the unique (venue, address): a concurrent opt-out wins
    except IntegrityError:
        db.rollback()
        return OptoutResult(venue, claim.address, already=True)
    audit.record(
        db,
        audit.AuditAction.WATCH_OPTOUT,
        actor_user_id=user.id,
        target_type="watch_optout",
        target_id=optout.id,
        details={
            "venue": venue,
            "watches_removed": watches_removed,
            "tails_stopped": len(stopped),
        },
    )
    db.commit()
    for allocation in stopped:
        try:
            close_now(db, allocation)
        except Exception:
            logger.exception("opt-out: closing allocation %s deferred", allocation.id)
    return OptoutResult(venue, claim.address, False, watches_removed, len(stopped))
