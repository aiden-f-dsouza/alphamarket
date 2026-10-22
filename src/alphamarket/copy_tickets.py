"""Copy tickets (docs/copy_tickets_plan.md; methodology "Copy tickets").

A paper tail (Allocation) can turn copy tickets on. From then on every new
paper fill it makes also becomes a ticket: the same trade written as an
order the user may place THEMSELVES on the venue. Koine never places it,
never pre-fills or submits an order anywhere, and never asks for a write key
(invariants 4 and 8). The user places every order by hand.

When the user picks their own connected read-only account on that venue
(`allocations.copy_account_id`), the worker matches their real fills to the
tickets after each sync and reports the slippage against the paper fill.

Regulatory levers (copy_tickets_plan "Why this shape"): tickets are manual
only; per-user sizing sits behind COPY_TICKETS_SIZED (off: only the
impersonal trade and the share of the creator's equity); COPY_TICKETS_ENABLED
off hides every surface and stops issuing. Never add one-click placement.

Units: quantities are integer atoms at `qty_decimals` (binary tickets carry
count_micros at 6), prices are decimal strings, and every computation on
them is exact (Fraction); only display values become floats.

Public functions:
- issue_tickets(db, allocation, now) -> new tickets (idempotent)
- notify_new_tickets(db, allocation, tickets) -> sent? (one email per pass)
- expire_tickets(db, now) -> rows expired
- match_tickets(db, account, now) -> tickets matched or extended
- slippage_bps(action, paper_price, matched_price) -> Fraction | None
- copy_stats(db, allocation) / summarize(tickets) -> counts + median slippage
- set_copy_settings(db, user, allocation, enabled, email, account_id)
- allocation_copy_context(db, user, allocation) -> the portfolio form's data
- ticket_view(ticket, sized, ...) -> one ticket as a page/JSON dict
"""

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from urllib.parse import quote

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamarket import audit
from alphamarket.config import get_settings
from alphamarket.connectors.base import Action, VenueProfile
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    AssetFill,
    CopyTicket,
    CopyTicketStatus,
    Fill,
    Instrument,
    MirrorAssetFill,
    MirrorFill,
    Signal,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
    utcnow,
)
from alphamarket.money import MICROS_PER_UNIT

logger = logging.getLogger(__name__)

BINARY_QTY_DECIMALS = 6  # count_micros
# A real fill counts for a ticket from the creator's fill time until this long
# after the ticket was issued.
MATCH_WINDOW = timedelta(hours=24)
# Tickets older than this are not matched any more: a daily venue (IBKR,
# SnapTrade) can ingest a fill a day or two late, and the window above is on
# fill time, not ingestion time.
MATCH_LOOKBACK = timedelta(days=7)
MATCHABLE = (
    CopyTicketStatus.OPEN,
    CopyTicketStatus.DONE,
    CopyTicketStatus.EXPIRED,
    CopyTicketStatus.MATCHED,  # only while partial, see match_tickets
)
# A connected account a user may check copies against.
UNUSABLE_ACCOUNT_STATUSES = (
    VenueAccountStatus.SUSPENDED,
    VenueAccountStatus.DISCONNECTED,
    VenueAccountStatus.PENDING,
)

DISCLAIMER = (
    "Koine does not place orders and cannot: it only reads your accounts. A copy ticket is "
    "the creator's trade sized to your paper allocation, written so you can place it yourself "
    "if you choose. It is not a recommendation or advice, prices move, and the paper result "
    "is not what a real order would get."
)
DISCLAIMER_UNSIZED = (
    "Koine does not place orders and cannot: it only reads your accounts. A copy ticket is "
    "the creator's trade, shown as a share of the creator's equity, the same for everyone who "
    "follows them. It is not a recommendation or advice, prices move, and the paper result "
    "is not what a real order would get."
)


def enabled() -> bool:
    return get_settings().copy_tickets_enabled


def sized() -> bool:
    return get_settings().copy_tickets_sized


def disclaimer(is_sized: bool | None = None) -> str:
    return DISCLAIMER if (sized() if is_sized is None else is_sized) else DISCLAIMER_UNSIZED


# ---- exact helpers ---------------------------------------------------------------------


def decimal_str(value: Fraction, max_places: int = 12) -> str:
    """An exact decimal string for a terminating fraction; a non-terminating
    one (a VWAP) is truncated toward zero at `max_places`."""
    for places in range(max_places + 1):
        scaled = value * 10**places
        if scaled.denominator == 1:
            break
    return format(Decimal(int(scaled)).scaleb(-places), "f")


def _units(atoms: int, decimals: int) -> Fraction:
    return Fraction(atoms, 10**decimals)


def _price(value: str) -> Fraction:
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"price {value!r} is not a finite decimal")
    return Fraction(parsed)


def _qty_str(atoms: int, decimals: int) -> str:
    return decimal_str(_units(atoms, decimals))


def round_half_up(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


def stake_bps(signal: Signal) -> int | None:
    """The creator's trade as a share of their equity just before it, in bps
    (half-up): binary count x price / equity, asset notional / equity. None
    when either is unknown or the equity is not positive."""
    equity = signal.creator_equity_micros
    if equity is None or equity <= 0:
        return None
    if signal.fill_id is not None:
        if signal.count_micros is None or signal.price_micros is None:
            return None
        notional = Fraction(signal.count_micros * signal.price_micros, MICROS_PER_UNIT)
    else:
        if signal.notional_micros is None:
            return None
        notional = Fraction(signal.notional_micros)
    return round_half_up(notional * 10_000 / equity)


def slippage_bps(action: str, paper_price: str, matched_price: str) -> Fraction | None:
    """The user's real price against the paper fill, in bps, signed so that
    positive is worse for the user: buy (matched - paper) / paper, sell
    (paper - matched) / paper. None when the paper price is not positive."""
    paper = _price(paper_price)
    if paper <= 0:
        return None
    matched = _price(matched_price)
    diff = matched - paper if Action(action) is Action.BUY else paper - matched
    return diff * 10_000 / paper


def median(values: Sequence[Fraction]) -> Fraction | None:
    """The middle value; the mean of the two middle values for an even count."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


# ---- matching (pure) -------------------------------------------------------------------


@dataclass(frozen=True)
class Lot:
    """One real fill of the user's: its ref, quantity in units, price."""

    ref: str
    qty: Fraction
    price: Fraction
    filled_at: datetime


@dataclass(frozen=True)
class Taken:
    refs: tuple[str, ...]
    qty: Fraction  # never above the target
    vwap: Fraction | None
    last_filled_at: datetime | None


def take_lots(target: Fraction, lots: Iterable[Lot]) -> Taken:
    """Lots oldest first, summed until `target` is reached. The lot that
    crosses the target counts only for the part needed (it is still used up:
    a fill matches at most one ticket). VWAP = sum(q_i x p_i) / sum(q_i)
    over the quantities counted."""
    refs: list[str] = []
    qty = Fraction(0)
    value = Fraction(0)
    last = None
    for lot in lots:
        if qty >= target:
            break
        used = min(lot.qty, target - qty)
        if used <= 0:
            continue
        refs.append(lot.ref)
        qty += used
        value += used * lot.price
        last = lot.filled_at
    return Taken(tuple(refs), qty, value / qty if qty else None, last)


# ---- issuing ---------------------------------------------------------------------------


def _venue_account(db: Session, allocation: Allocation) -> VenueAccount | None:
    strategy = db.get(Strategy, allocation.strategy_id)
    return db.get(VenueAccount, strategy.venue_account_id) if strategy else None


def _expiry(signal: Signal, profile: VenueProfile) -> datetime:
    return aware(signal.filled_at) + timedelta(seconds=profile.copy_ticket_ttl_seconds)


def _new_binary(
    allocation: Allocation, venue: str, row: MirrorFill, signal: Signal, profile
) -> CopyTicket:
    return CopyTicket(
        allocation_id=allocation.id,
        user_id=allocation.user_id,
        signal_id=signal.id,
        mirror_fill_id=row.id,
        venue=venue,
        ticker=row.ticker,
        label=row.ticker,
        side=row.side,
        action=row.action,
        qty_atoms=row.count_micros,
        qty_decimals=BINARY_QTY_DECIMALS,
        paper_price=decimal_str(Fraction(row.price_micros, MICROS_PER_UNIT)),
        creator_price=decimal_str(Fraction(signal.price_micros or 0, MICROS_PER_UNIT)),
        stake_bps=stake_bps(signal),
        expires_at=_expiry(signal, profile),
    )


def _new_asset(
    db: Session, allocation: Allocation, venue: str, row: MirrorAssetFill, signal: Signal, profile
) -> CopyTicket:
    instrument = db.get(Instrument, row.instrument_id)
    return CopyTicket(
        allocation_id=allocation.id,
        user_id=allocation.user_id,
        signal_id=signal.id,
        mirror_asset_fill_id=row.id,
        venue=venue,
        instrument_id=row.instrument_id,
        ticker=instrument.symbol if instrument else signal.ticker,
        label=instrument.label if instrument else signal.ticker,
        side=signal.side,
        action=row.action,
        qty_atoms=row.qty_atoms,
        qty_decimals=row.qty_decimals,
        paper_price=row.price,
        creator_price=signal.price or row.price,
        stake_bps=stake_bps(signal),
        expires_at=_expiry(signal, profile),
    )


def _unticketed_sources(
    db: Session, allocation: Allocation, since: datetime
) -> list[MirrorFill | MirrorAssetFill]:
    """The allocation's paper fills created at or after `since` that have no
    ticket yet: binary mirror_fills, and asset mirror_asset_fills copied from
    a signal (reason "signal"), oldest first."""
    binary = db.scalars(
        select(MirrorFill)
        .outerjoin(CopyTicket, CopyTicket.mirror_fill_id == MirrorFill.id)
        .where(
            MirrorFill.allocation_id == allocation.id,
            MirrorFill.created_at >= since,
            CopyTicket.id.is_(None),
        )
        .order_by(MirrorFill.id)
    ).all()
    asset = db.scalars(
        select(MirrorAssetFill)
        .outerjoin(CopyTicket, CopyTicket.mirror_asset_fill_id == MirrorAssetFill.id)
        .where(
            MirrorAssetFill.allocation_id == allocation.id,
            MirrorAssetFill.reason == "signal",
            MirrorAssetFill.signal_id.is_not(None),
            MirrorAssetFill.created_at >= since,
            CopyTicket.id.is_(None),
        )
        .order_by(MirrorAssetFill.id)
    ).all()
    return [*binary, *asset]


def issue_tickets(
    db: Session, allocation: Allocation, now: datetime | None = None
) -> list[CopyTicket]:
    """One ticket per paper fill of `allocation` created at or after
    copy_enabled_at that has none yet: binary mirror_fills, asset
    mirror_asset_fills with reason "signal" (liquidations, stop closes,
    funding and corporate actions issue nothing). Only while the feature is
    on and the allocation is active with tickets on. A ticket already past
    its expiry is stored as expired. Idempotent: the source ids are unique,
    and a concurrent pass that inserted the same ticket first wins (this
    one's insert is rolled back alone). Commits; returns the new rows."""
    if not (enabled() and allocation.copy_tickets and allocation.copy_enabled_at is not None):
        return []
    if allocation.status != AllocationStatus.ACTIVE:
        return []
    now = now or utcnow()
    account = _venue_account(db, allocation)
    if account is None:
        return []
    profile = get_profile(account.venue)
    issued: list[CopyTicket] = []
    for row in _unticketed_sources(db, allocation, aware(allocation.copy_enabled_at)):
        signal = db.get(Signal, row.signal_id)
        if signal is None:
            continue
        if isinstance(row, MirrorFill):
            ticket = _new_binary(allocation, account.venue, row, signal, profile)
        else:
            ticket = _new_asset(db, allocation, account.venue, row, signal, profile)
        ticket.created_at = now
        if aware(ticket.expires_at) <= now:
            ticket.status, ticket.status_at = CopyTicketStatus.EXPIRED, now
        try:
            with db.begin_nested():
                db.add(ticket)
                db.flush()
        except IntegrityError:
            logger.info("allocation %s: ticket for %r issued by another pass", allocation.id, row)
            continue
        issued.append(ticket)
    db.commit()
    return issued


def expire_tickets(db: Session, now: datetime | None = None) -> int:
    """Open tickets past expires_at become expired (one UPDATE). Commits."""
    now = now or utcnow()
    result = db.execute(
        update(CopyTicket)
        .where(CopyTicket.status == CopyTicketStatus.OPEN, CopyTicket.expires_at <= now)
        .values(status=CopyTicketStatus.EXPIRED, status_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount or 0


# ---- email ----------------------------------------------------------------------------


def _ticket_line(t: CopyTicket, is_sized: bool) -> str:
    words = [t.action.upper(), t.side.upper()]
    if is_sized:
        words.append(_qty_str(t.qty_atoms, t.qty_decimals))
    words.append(t.label)
    line = f"- {' '.join(words)} at {t.paper_price} (creator {t.creator_price})"
    if t.stake_bps is not None:
        line += f", {t.stake_bps / 100:.2f}% of the creator's equity"
    return line + f", until {aware(t.expires_at):%Y-%m-%d %H:%M} UTC"


def notify_new_tickets(db: Session, allocation: Allocation, tickets: Sequence[CopyTicket]) -> bool:
    """One plain-text email for the OPEN tickets of one pass, when the user
    turned copy emails on. Instrument, side, price, size (sized mode only),
    expiry, a link to /copy and the disclaimer: never anything secret."""
    from alphamarket.email import send_email

    open_tickets = [t for t in tickets if t.status == CopyTicketStatus.OPEN]
    if not (enabled() and allocation.copy_email and open_tickets):
        return False
    user = db.get(User, allocation.user_id)
    strategy = db.get(Strategy, allocation.strategy_id)
    if user is None or strategy is None:
        return False
    is_sized = sized()
    venue = get_profile(open_tickets[0].venue).display_name
    n = len(open_tickets)
    base = get_settings().public_base_url.rstrip("/")
    lines = [
        f"Hi {user.display_name},",
        "",
        f"Your paper tail of {strategy.name} copied {n} new trade{'s' if n != 1 else ''}. "
        f"{'Each is' if n != 1 else 'It is'} written below as an order you can place yourself "
        f"on {venue}, if you choose to:",
        "",
        *(_ticket_line(t, is_sized) for t in open_tickets),
        "",
        f"Open your tickets, mark them done or skip them: {base}/copy",
        "",
        disclaimer(is_sized),
        "",
        f"Turn these emails off on your portfolio: {base}/portfolio",
        "",
        "Koine",
    ]
    subject = f"{n} new copy ticket{'s' if n != 1 else ''}: {strategy.name}"
    return send_email(user.email, subject, "\n".join(lines))


# ---- matching --------------------------------------------------------------------------


def _used_refs(db: Session, user_id: int) -> set[str]:
    used: set[str] = set()
    for refs in db.scalars(
        select(CopyTicket.matched_refs).where(
            CopyTicket.user_id == user_id, CopyTicket.matched_refs.is_not(None)
        )
    ):
        used.update(refs or [])
    return used


def _binary_lots(db: Session, strategy_id: int, t: CopyTicket, start, end) -> list[Lot]:
    rows = db.scalars(
        select(Fill)
        .where(
            Fill.strategy_id == strategy_id,
            Fill.ticker == t.ticker,
            Fill.side == t.side,
            Fill.action == t.action,
            Fill.filled_at >= start,
            Fill.filled_at <= end,
        )
        .order_by(Fill.filled_at, Fill.id)
    ).all()
    return [
        Lot(
            f"fill:{f.id}",
            _units(f.count_micros, BINARY_QTY_DECIMALS),
            Fraction(f.price_micros, MICROS_PER_UNIT),
            aware(f.filled_at),
        )
        for f in rows
    ]


def _asset_lots(db: Session, account_id: int, t: CopyTicket, start, end) -> list[Lot]:
    rows = db.scalars(
        select(AssetFill)
        .where(
            AssetFill.venue_account_id == account_id,
            AssetFill.instrument_id == t.instrument_id,
            AssetFill.action == t.action,
            AssetFill.filled_at >= start,
            AssetFill.filled_at <= end,
        )
        .order_by(AssetFill.filled_at, AssetFill.id)
    ).all()
    return [
        Lot(
            f"asset_fill:{f.id}",
            _units(f.qty_atoms, f.qty_decimals),
            _price(f.price),
            aware(f.filled_at),
        )
        for f in rows
    ]


def match_tickets(db: Session, account: VenueAccount, now: datetime | None = None) -> int:
    """Match the user's real fills on `account` to the tickets of their
    allocations that check copies against it (copy_account_id). Per ticket,
    oldest first: candidate fills are the same instrument (binary: ticker
    and side, through the account's strategy's fills; asset: asset_fills of
    the account and instrument), the same action, filled from the creator's
    fill time to MATCH_WINDOW after the ticket was issued, and not used by
    any other ticket of the user. They are summed oldest first up to the
    ticket's quantity (take_lots). Open, done and expired tickets can match,
    and a partial match keeps taking later fills until it is whole. Commits;
    returns how many tickets changed."""
    if not enabled():
        return 0
    now = now or utcnow()
    tickets = db.scalars(
        select(CopyTicket)
        .join(Allocation, Allocation.id == CopyTicket.allocation_id)
        .where(
            Allocation.copy_account_id == account.id,
            Allocation.user_id == account.user_id,
            CopyTicket.user_id == account.user_id,
            CopyTicket.venue == account.venue,
            CopyTicket.status.in_(MATCHABLE),
            CopyTicket.created_at >= now - MATCH_LOOKBACK,
        )
        .order_by(CopyTicket.created_at, CopyTicket.id)
    ).all()
    tickets = [
        t
        for t in tickets
        if t.status != CopyTicketStatus.MATCHED or (t.matched_qty_atoms or 0) < t.qty_atoms
    ]
    if not tickets:
        return 0
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    used = _used_refs(db, account.user_id)
    changed = 0
    for t in tickets:
        signal = db.get(Signal, t.signal_id)
        start = aware(signal.filled_at) if signal is not None else aware(t.created_at)
        end = aware(t.created_at) + MATCH_WINDOW
        if t.mirror_fill_id is not None:
            if strategy is None:
                continue
            lots = _binary_lots(db, strategy.id, t, start, end)
        else:
            lots = _asset_lots(db, account.id, t, start, end)
        mine = set(t.matched_refs or [])
        fresh = [lot for lot in lots if lot.ref not in used]
        if not fresh:
            continue
        prior = [lot for lot in lots if lot.ref in mine]
        taken = take_lots(_units(t.qty_atoms, t.qty_decimals), [*prior, *fresh])
        if not set(taken.refs) - mine or taken.vwap is None:
            continue
        scale = 10**t.qty_decimals
        t.matched_qty_atoms = math.floor(taken.qty * scale)
        t.matched_price = decimal_str(taken.vwap)
        t.matched_refs = list(taken.refs)
        t.matched_at = taken.last_filled_at
        t.status, t.status_at = CopyTicketStatus.MATCHED, now
        used.update(taken.refs)
        changed += 1
    db.commit()
    return changed


# ---- stats -----------------------------------------------------------------------------


def summarize(tickets: Iterable[CopyTicket]) -> dict:
    """Counts by status and the median slippage (bps, Fraction) of the
    matched tickets. Pure: no database."""
    counts = {s.value: 0 for s in CopyTicketStatus}
    slips: list[Fraction] = []
    issued = 0
    for t in tickets:
        issued += 1
        counts[t.status] = counts.get(t.status, 0) + 1
        if t.status == CopyTicketStatus.MATCHED and t.matched_price is not None:
            s = slippage_bps(t.action, t.paper_price, t.matched_price)
            if s is not None:
                slips.append(s)
    return {"issued": issued, **counts, "median_slippage_bps": median(slips)}


def _bps_display(value: Fraction | None) -> float | None:
    return None if value is None else round(float(value), 1)


def copy_stats(db: Session, allocation: Allocation) -> dict:
    """summarize() over the allocation's tickets, the median as a display
    float (0.1 bps) so it serializes."""
    rows = db.scalars(select(CopyTicket).where(CopyTicket.allocation_id == allocation.id)).all()
    out = summarize(rows)
    out["median_slippage_bps"] = _bps_display(out["median_slippage_bps"])
    return out


# ---- settings --------------------------------------------------------------------------


def eligible_accounts(db: Session, user: User, venue: str) -> list[VenueAccount]:
    """The user's own accounts on `venue` that copies can be checked against."""
    return list(
        db.scalars(
            select(VenueAccount)
            .where(
                VenueAccount.user_id == user.id,
                VenueAccount.venue == venue,
                VenueAccount.is_watch.is_(False),
                VenueAccount.status.not_in(UNUSABLE_ACCOUNT_STATUSES),
            )
            .order_by(VenueAccount.id)
        ).all()
    )


def _account_label(db: Session, account: VenueAccount) -> str:
    """Never the key id: the venue and the user's own strategy name."""
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    venue = get_profile(account.venue).display_name
    return f"{venue} · {strategy.name}" if strategy else f"{venue} account {account.id}"


def set_copy_settings(
    db: Session,
    user: User,
    allocation: Allocation,
    enabled_: bool,
    email: bool,
    account_id: int | None,
    now: datetime | None = None,
) -> Allocation:
    """Turn tickets on/off, the email on/off, and pick the account to check
    copies against. The account must be the user's own, on the strategy's
    venue, not a watch account and not suspended/disconnected/pending (422
    otherwise). Turning tickets on needs an active tail and stamps
    copy_enabled_at fresh each time it goes off -> on, so nothing from
    before is replayed. Audited; commits."""
    if allocation.user_id != user.id:
        raise HTTPException(status_code=404, detail="Allocation not found")
    now = now or utcnow()
    venue_account = _venue_account(db, allocation)
    if venue_account is None:
        raise HTTPException(status_code=404, detail="Allocation not found")
    venue = venue_account.venue
    if account_id is not None:
        account = db.get(VenueAccount, account_id)
        ok = (
            account is not None
            and account.user_id == user.id
            and account.venue == venue
            and not account.is_watch
            and account.status not in UNUSABLE_ACCOUNT_STATUSES
        )
        if not ok:
            raise HTTPException(
                status_code=422,
                detail=f"Pick one of your own connected {get_profile(venue).display_name} "
                "accounts.",
            )
    if enabled_ and not allocation.copy_tickets:
        if allocation.status != AllocationStatus.ACTIVE:
            raise HTTPException(status_code=422, detail="This paper tail has stopped.")
        allocation.copy_enabled_at = now
    allocation.copy_tickets = bool(enabled_)
    allocation.copy_email = bool(email)
    allocation.copy_account_id = account_id
    audit.record(
        db,
        audit.AuditAction.COPY_SETTINGS,
        actor_user_id=user.id,
        target_type="allocation",
        target_id=allocation.id,
        details={"enabled": bool(enabled_), "email": bool(email), "account_id": account_id},
    )
    db.commit()
    return allocation


def allocation_copy_context(db: Session, user: User, allocation: Allocation) -> dict:
    """What the portfolio page needs to show one allocation's copy settings:
    `available` (feature on), the current settings, the accounts the user may
    pick (id + label, never a key id), the form's action, and copy_stats."""
    venue_account = _venue_account(db, allocation)
    venue = venue_account.venue if venue_account else ""
    accounts = eligible_accounts(db, user, venue) if venue else []
    return {
        "available": enabled(),
        "sized": sized(),
        "allocation_id": allocation.id,
        "enabled": allocation.copy_tickets,
        "email": allocation.copy_email,
        "account_id": allocation.copy_account_id,
        "enabled_at": aware(allocation.copy_enabled_at).isoformat()
        if allocation.copy_enabled_at
        else None,
        "can_enable": allocation.status == AllocationStatus.ACTIVE,
        "venue": venue,
        "venue_label": get_profile(venue).display_name if venue else "",
        "accounts": [{"id": a.id, "label": _account_label(db, a)} for a in accounts],
        "action": f"/portfolio/allocations/{allocation.id}/copy",
        "stats": copy_stats(db, allocation),
    }


# ---- user actions ---------------------------------------------------------------------


def owned_ticket(db: Session, user: User, ticket_id: int) -> CopyTicket:
    ticket = db.get(CopyTicket, ticket_id)
    if ticket is None or ticket.user_id != user.id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


def can_mark_done(ticket: CopyTicket, status: str, now: datetime) -> bool:
    """Done from open, or from expired within MATCH_WINDOW of issue: the same
    window a real fill must fall in to match, so a stale ticket stops asking."""
    if status == CopyTicketStatus.OPEN:
        return True
    return status == CopyTicketStatus.EXPIRED and now <= aware(ticket.created_at) + MATCH_WINDOW


def mark_ticket(
    db: Session, user: User, ticket: CopyTicket, status: str, now: datetime | None = None
) -> bool:
    """The user says they placed it (done: from open, or expired within
    MATCH_WINDOW of issue, can_mark_done) or will not (skipped: from open).
    Anything else is left as it is (a double click, a ticket matched
    meanwhile); returns whether it changed. Audited; commits."""
    now = now or utcnow()
    current = display_status(ticket, now)
    if CopyTicketStatus(status) is CopyTicketStatus.DONE:
        if not can_mark_done(ticket, current, now):
            return False
    elif current != CopyTicketStatus.OPEN:
        return False
    ticket.status, ticket.status_at = status, now
    action = (
        audit.AuditAction.COPY_TICKET_DONE
        if status == CopyTicketStatus.DONE
        else audit.AuditAction.COPY_TICKET_SKIP
    )
    audit.record(db, action, actor_user_id=user.id, target_type="copy_ticket", target_id=ticket.id)
    db.commit()
    return True


# ---- display ---------------------------------------------------------------------------


def display_status(ticket: CopyTicket, now: datetime | None = None) -> str:
    """The stored status, except an open ticket past its expiry reads as
    expired before the worker's sweep catches up."""
    now = now or utcnow()
    if ticket.status == CopyTicketStatus.OPEN and aware(ticket.expires_at) <= now:
        return CopyTicketStatus.EXPIRED
    return ticket.status


def _expires_in(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    return f"{seconds / 3600:.1f} h"


def place_url(profile: VenueProfile, ticker: str, instrument: Instrument | None) -> str | None:
    """The venue page to place a ticket on: profile.trade_url filled in, or
    trade_home_url when there is no template or it needs fields we lack."""
    if profile.trade_url:
        fields = {"symbol": quote(ticker, safe="")}
        if instrument is not None:
            base = instrument.base_asset.removesuffix("-PERP")
            fields |= {
                "base": base,
                "quote": instrument.quote_asset,
                "base_lower": base.lower(),
                "quote_lower": instrument.quote_asset.lower(),
            }
        try:
            return profile.trade_url.format(**fields)
        except (KeyError, IndexError):
            pass
    return profile.trade_home_url


def ticket_view(
    ticket: CopyTicket,
    is_sized: bool,
    *,
    instrument: Instrument | None = None,
    now: datetime | None = None,
) -> dict:
    """One ticket for the page and the JSON API. With sizing off (`is_sized`
    False) there is no quantity at all (neither the ticket's nor the matched
    one): only the trade and the share of the creator's equity."""
    now = now or utcnow()
    profile = get_profile(ticket.venue)
    status = display_status(ticket, now)
    slip = (
        slippage_bps(ticket.action, ticket.paper_price, ticket.matched_price)
        if ticket.matched_price is not None
        else None
    )
    view = {
        "id": ticket.id,
        "allocation_id": ticket.allocation_id,
        "venue": ticket.venue,
        "venue_label": profile.display_name,
        "label": ticket.label,
        "ticker": ticket.ticker,
        "side": ticket.side,
        "action": ticket.action,
        "paper_price": ticket.paper_price,
        "creator_price": ticket.creator_price,
        "stake_pct": None if ticket.stake_bps is None else ticket.stake_bps / 10_000,
        "created_at": aware(ticket.created_at).isoformat(),
        "expires_at": aware(ticket.expires_at).isoformat(),
        "expires_in": _expires_in((aware(ticket.expires_at) - now).total_seconds()),
        "status": str(status),
        "status_at": aware(ticket.status_at).isoformat() if ticket.status_at else None,
        "place_url": place_url(profile, ticket.ticker, instrument),
        "matched_price": ticket.matched_price,
        "matched_at": aware(ticket.matched_at).isoformat() if ticket.matched_at else None,
        "slippage_bps": _bps_display(slip),
        "can_done": can_mark_done(ticket, status, now),
        "can_skip": status == CopyTicketStatus.OPEN,
    }
    if is_sized:
        view["qty"] = _qty_str(ticket.qty_atoms, ticket.qty_decimals)
        view["matched_qty"] = (
            _qty_str(ticket.matched_qty_atoms, ticket.qty_decimals)
            if ticket.matched_qty_atoms is not None
            else None
        )
        view["partial"] = (
            ticket.matched_qty_atoms is not None and ticket.matched_qty_atoms < ticket.qty_atoms
        )
    return view


def user_tickets(db: Session, user: User, now: datetime | None = None) -> dict:
    """Everything /copy and GET /api/copy-tickets show: open tickets (soonest
    expiry first), the rest newest first (at most 200), and per-allocation
    stats for every tail with tickets on or with any ticket."""
    now = now or utcnow()
    is_sized = sized()
    rows = db.scalars(
        select(CopyTicket)
        .where(CopyTicket.user_id == user.id)
        .order_by(CopyTicket.created_at.desc(), CopyTicket.id.desc())
        .limit(500)  # open tickets are at most hours old: always inside this
    ).all()
    inst_ids = {t.instrument_id for t in rows if t.instrument_id is not None}
    instruments = (
        {i.id: i for i in db.scalars(select(Instrument).where(Instrument.id.in_(inst_ids)))}
        if inst_ids
        else {}
    )
    views = [
        ticket_view(t, is_sized, instrument=instruments.get(t.instrument_id), now=now) for t in rows
    ]
    open_ = sorted((v for v in views if v["status"] == "open"), key=lambda v: v["expires_at"])
    history = [v for v in views if v["status"] != "open"][:200]
    alloc_ids = {t.allocation_id for t in rows}
    allocations = db.scalars(
        select(Allocation)
        .where(Allocation.user_id == user.id)
        .where(Allocation.copy_tickets.is_(True) | Allocation.id.in_(sorted(alloc_ids) or [-1]))
        .order_by(Allocation.id)
    ).all()
    summaries = []
    for a in allocations:
        strategy = db.get(Strategy, a.strategy_id)
        summaries.append(
            {
                "allocation_id": a.id,
                "name": strategy.name if strategy else f"Allocation {a.id}",
                "enabled": a.copy_tickets,
                "email": a.copy_email,
                "checking": a.copy_account_id is not None,
                "status": a.status,
                "stats": copy_stats(db, a),
            }
        )
    return {
        "sized": is_sized,
        "disclaimer": disclaimer(is_sized),
        "open": open_,
        "history": history,
        "allocations": summaries,
    }
