"""Ingestion: pull new fills/settlements for a connected venue account and
append them to the ledger.

Idempotent by construction: rows are keyed on the unique venue ids, already-
seen rows are skipped, and nothing here ever UPDATEs or DELETEs a ledger row
(invariant #3). Re-polling with overlap is therefore safe and deliberate.

Ledger v2: every fill, settlement and event is also written as its legs
(ledger_entries), in the same transaction. Idempotency there is at EVENT
granularity: if any leg of an event is already stored, all of them must
match, and a partial or different duplicate quarantines the account.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from sqlalchemy import and_, func, literal, or_, select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import (
    InstrumentKind,
    InstrumentType,
    NormalizedAssetFill,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    VenueConnector,
)
from alphamarket.connectors.registry import build_connector, get_profile
from alphamarket.ledger import (
    CASH_ASSET,
    MICRO_DECIMALS,
    LegGroup,
    aware,
    group_kind,
    legs_for_fill,
    legs_for_settlement,
    nonzero_legs,
    rescale,
)
from alphamarket.models import (
    AssetFill,
    Fill,
    Instrument,
    LedgerEntry,
    LedgerQuarantine,
    LedgerSyncState,
    Settlement,
    Strategy,
    StrategyStatus,
    SymbolHistory,
    VenueAccount,
    VenueAccountStatus,
    utcnow,
)
from alphamarket.vault import decrypt_secret

logger = logging.getLogger(__name__)

# Re-fetch window behind the newest stored fill, so a fill that landed in the
# same second as the previous poll's cutoff is never missed. Duplicates are
# discarded by venue-id dedup, so overlap costs nothing but bandwidth.
OVERLAP = timedelta(minutes=5)


@dataclass
class SyncResult:
    new_fills: int
    new_settlements: int
    new_events: int = 0  # ledger v2 non-fill events (asset venues)


def sync_account(db: Session, account: VenueAccount) -> SyncResult:
    """Fetch and append new ledger rows for one account. Raises on failure
    after recording it on the account row; the worker retries next tick."""
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    if strategy is None:
        raise ValueError(f"venue account {account.id} has no strategy")
    connector = connector_for(account)

    try:
        if is_asset_venue(account.venue):
            result = _sync_assets(db, connector, account)
        else:
            result = _sync(db, connector, strategy)
    except Exception as exc:
        account.status = VenueAccountStatus.ERROR
        # Exception text only; never credentials (invariant #4).
        account.last_sync_error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise

    account.status = VenueAccountStatus.ACTIVE
    account.last_sync_error = None
    account.last_synced_at = utcnow()
    db.commit()
    return result


def connector_for(account: VenueAccount) -> VenueConnector:
    """Rebuild the venue client from the stored (encrypted) credential. A
    venue that takes no secret (Polymarket) is built from the public id."""
    if get_profile(account.venue).needs_secret:
        if account.encrypted_private_key is None:
            raise ValueError(f"venue account {account.id} has no stored credential")
        secret = decrypt_secret(account.encrypted_private_key)
    else:
        secret = None
    return build_connector(account.venue, account.venue_env, account.key_id, secret)


def _sync(db: Session, connector, strategy: Strategy) -> SyncResult:
    since = _cursor(db, Fill.filled_at, strategy.id)
    fills = connector.fetch_fills(since=since)
    new_fills = 0
    if fills:
        existing = set(
            db.scalars(
                select(Fill.venue_fill_id).where(
                    Fill.venue_fill_id.in_([f.venue_fill_id for f in fills])
                )
            )
        )
        for f in fills:
            if f.venue_fill_id in existing:
                continue
            db.add(
                Fill(
                    strategy_id=strategy.id,
                    venue_fill_id=f.venue_fill_id,
                    ticker=f.ticker,
                    side=f.side,
                    action=f.action,
                    count_micros=f.count_micros,
                    price_micros=f.price_micros,
                    fee_micros=f.fee_micros,
                    notional_micros=f.notional_micros,
                    filled_at=f.filled_at,
                )
            )
            new_fills += 1

    since = _cursor(db, Settlement.settled_at, strategy.id)
    settlements = connector.fetch_settlements(since=since)
    new_settlements = 0
    if settlements:
        existing = set(
            db.scalars(
                select(Settlement.venue_settlement_id).where(
                    Settlement.venue_settlement_id.in_([s.venue_settlement_id for s in settlements])
                )
            )
        )
        for s in settlements:
            if s.venue_settlement_id in existing:
                continue
            db.add(
                Settlement(
                    strategy_id=strategy.id,
                    venue_settlement_id=s.venue_settlement_id,
                    ticker=s.ticker,
                    market_result=s.market_result,
                    yes_count_micros=s.yes_count_micros,
                    no_count_micros=s.no_count_micros,
                    revenue_micros=s.revenue_micros,
                    fee_micros=s.fee_micros,
                    settled_at=s.settled_at,
                )
            )
            new_settlements += 1

    # Ledger v2: the legs of every fill/settlement land in the same
    # transaction as the rows themselves (and older rows get theirs here too).
    account = db.get(VenueAccount, strategy.venue_account_id)
    if account is not None:
        db.flush()
        write_binary_legs(db, account, strategy)
    db.commit()
    logger.info(
        "synced strategy %s: +%d fills, +%d settlements", strategy.id, new_fills, new_settlements
    )
    return SyncResult(new_fills=new_fills, new_settlements=new_settlements)


def _cursor(db: Session, ts_column, strategy_id: int) -> datetime | None:
    """Newest stored timestamp minus overlap; None means full history."""
    table = ts_column.class_
    newest = db.scalar(select(func.max(ts_column)).where(table.strategy_id == strategy_id))
    if newest is None:
        return None
    if newest.tzinfo is None:  # SQLite loses tzinfo; stored values are UTC (invariant #2)
        newest = newest.replace(tzinfo=UTC)
    return newest - OVERLAP


# ---- ledger v2: legs, instruments, asset fills, events (P1) ----------------------

STREAM_ASSET_FILLS = "asset_fills"
STREAM_EVENTS = "events"


def is_asset_venue(venue: str) -> bool:
    """Venues whose profile says ASSET use the v2 fetch/ingest path."""
    try:
        return get_profile(venue).instrument is InstrumentKind.ASSET
    except ValueError:
        return False


def cash_asset_for(venue: str) -> str:
    """The account's cash asset (VenueProfile.quote_asset), "USD" if unknown."""
    try:
        return get_profile(venue).quote_asset
    except ValueError:
        return CASH_ASSET


def group_ref_for(source: str, venue_ref: str, kind: str | None = None) -> str:
    """Identity of one fill/settlement/event's legs within an account:
    "fill:<id>", "settlement:<id>" or "event:<kind>:<venue_ref>" (an event's
    venue_ref is unique per account AND kind, base.NormalizedEvent)."""
    if source == "event":
        return f"event:{kind}:{venue_ref}"
    return f"{source}:{venue_ref}"


@dataclass
class LedgerWriteResult:
    new: int = 0
    duplicates: int = 0
    rejected: int = 0  # out-of-scope instruments (options)
    quarantined: list[str] = field(default_factory=list)

    def merge(self, other: "LedgerWriteResult") -> None:
        self.new += other.new
        self.duplicates += other.duplicates
        self.rejected += other.rejected
        self.quarantined.extend(other.quarantined)


class PageCursors(NamedTuple):
    next_cursor: str | None  # None: this pass is done
    resume_cursor: str | None  # set only on the final page; stored per account


def quarantine_account(
    db: Session, account: VenueAccount, reason: str, ref: str | None = None
) -> None:
    """Record why, and quarantine the account's strategy (as reconciliation
    does). Nothing is auto-unquarantined; a human reads the reason first.
    A problem that persists across runs is recorded once, but still
    re-quarantines a strategy someone released without fixing it."""
    ref = ref[:300] if ref else None
    seen = db.scalar(
        select(LedgerQuarantine.id).where(
            LedgerQuarantine.venue_account_id == account.id,
            LedgerQuarantine.ref == ref if ref is not None else LedgerQuarantine.ref.is_(None),
            LedgerQuarantine.reason == reason,
        )
    )
    if seen is None:
        db.add(LedgerQuarantine(venue_account_id=account.id, reason=reason, ref=ref))
        db.flush()
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    if strategy is not None and strategy.status != StrategyStatus.DELISTED:
        strategy.status = StrategyStatus.QUARANTINED
    logger.warning("venue account %s quarantined by the ledger: %s", account.id, reason)


# ---- instruments ---------------------------------------------------------------


def upsert_instrument(
    db: Session, venue: str, inst: NormalizedInstrument, seen_at: datetime
) -> Instrument:
    """The instrument row for (venue, symbol), created on first sight with a
    symbol_history row. Lot sizes and precision follow the venue's latest
    description; type and assets are identity and never change here."""
    query = select(Instrument).where(Instrument.venue == venue, Instrument.symbol == inst.symbol)
    row = db.scalar(query)
    if row is None:
        values = {
            "venue": venue,
            "symbol": inst.symbol,
            "type": inst.type.value,
            "base_asset": inst.base_asset,
            "quote_asset": inst.quote_asset,
            "qty_decimals": inst.qty_decimals,
            "qty_step_atoms": inst.qty_step_atoms,
            "min_qty_atoms": inst.min_qty_atoms,
            "min_notional": inst.min_notional,
            "multiplier": inst.multiplier,
            "expiry": inst.expiry,
            "first_seen_at": seen_at,
        }
        if db.get_bind().dialect.name == "postgresql":
            # Two accounts on one venue can meet the same symbol concurrently.
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = (
                pg_insert(Instrument)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["venue", "symbol"])
                .returning(Instrument.id)
            )
            new_id = db.scalar(stmt)
            row = db.scalar(query)
        else:
            row = Instrument(**values)
            db.add(row)
            db.flush()
            new_id = row.id
        if new_id is not None:
            db.add(SymbolHistory(instrument_id=row.id, symbol=inst.symbol, valid_from=seen_at))
        return row

    if (row.type, row.base_asset, row.quote_asset) != (
        inst.type.value,
        inst.base_asset,
        inst.quote_asset,
    ):
        logger.warning(
            "instrument %s %s described differently by the venue; keeping the stored identity",
            venue,
            inst.symbol,
        )
    for attr in ("qty_decimals", "qty_step_atoms", "min_qty_atoms", "min_notional", "multiplier"):
        if getattr(row, attr) != getattr(inst, attr):
            setattr(row, attr, getattr(inst, attr))
    stored_expiry = aware(row.expiry) if row.expiry is not None else None
    if stored_expiry != inst.expiry:
        row.expiry = inst.expiry
    return row


def _binary_instrument(venue: str, ticker: str) -> NormalizedInstrument:
    """One instrument per binary market ticker; quantities are micro-contracts."""
    try:
        profile = get_profile(venue)
        step, quote = profile.contract_step_micros, profile.quote_asset
    except ValueError:
        step, quote = 1, CASH_ASSET
    return NormalizedInstrument(
        symbol=ticker,
        type=InstrumentType.BINARY,
        base_asset=ticker,
        quote_asset=quote,
        qty_decimals=MICRO_DECIMALS,
        qty_step_atoms=step,
    )


def _entries(
    account_id: int,
    legs: list[NormalizedLeg],
    venue_ref: str,
    group_ref: str,
    occurred_at: datetime,
    instrument_id_of,
) -> list[LedgerEntry]:
    return [
        LedgerEntry(
            venue_account_id=account_id,
            instrument_id=instrument_id_of(leg) if callable(instrument_id_of) else instrument_id_of,
            asset=leg.asset,
            delta_atoms=leg.delta_atoms,
            decimals=leg.decimals,
            kind=str(leg.kind),
            venue_ref=venue_ref,
            group_ref=group_ref,
            balance_after_atoms=leg.balance_after_atoms,
            occurred_at=occurred_at,
        )
        for leg in legs
    ]


# ---- binary venues: legs derived from stored fills/settlements ------------------


def write_binary_legs(db: Session, account: VenueAccount, strategy: Strategy) -> int:
    """Write the legs of every fill and settlement of `strategy` that has none
    yet (new rows of this sync, and rows ingested before ledger v2). Derived
    from the STORED rows, so a re-run writes nothing. Returns groups written."""
    venue, cash = account.venue, cash_asset_for(account.venue)

    def has_legs(prefix: str, venue_id_column):
        return (
            select(LedgerEntry.id)
            .where(
                LedgerEntry.venue_account_id == account.id,
                LedgerEntry.group_ref == literal(prefix).concat(venue_id_column),
            )
            .exists()
        )

    fills = db.scalars(
        select(Fill)
        .where(Fill.strategy_id == strategy.id, ~has_legs("fill:", Fill.venue_fill_id))
        .order_by(Fill.id)
    ).all()
    settlements = db.scalars(
        select(Settlement)
        .where(
            Settlement.strategy_id == strategy.id,
            ~has_legs("settlement:", Settlement.venue_settlement_id),
        )
        .order_by(Settlement.id)
    ).all()

    instrument_ids: dict[str, int] = {}

    def instrument_id(ticker: str, seen_at: datetime) -> int:
        if ticker not in instrument_ids:
            inst = upsert_instrument(db, venue, _binary_instrument(venue, ticker), seen_at)
            instrument_ids[ticker] = inst.id
        return instrument_ids[ticker]

    written = 0
    for f in fills:
        legs = nonzero_legs(legs_for_fill(f, venue, cash))
        inst_id = instrument_id(f.ticker, f.filled_at)
        ref = f.venue_fill_id
        rows = _entries(account.id, legs, ref, group_ref_for("fill", ref), f.filled_at, inst_id)
        db.add_all(rows)
        written += bool(rows)
    for s in settlements:
        legs = nonzero_legs(legs_for_settlement(s, venue, cash))
        if not legs:
            continue
        inst_id = instrument_id(s.ticker, s.settled_at)
        ref = s.venue_settlement_id
        rows = _entries(
            account.id, legs, ref, group_ref_for("settlement", ref), s.settled_at, inst_id
        )
        db.add_all(rows)
        written += 1
    db.flush()
    return written


# ---- event-granularity idempotency ---------------------------------------------


def _legs_mismatch(stored: list[LedgerEntry], legs: list[NormalizedLeg]) -> str | None:
    """None when the stored legs of an event are exactly the venue's legs
    (values compared at the finer of the two scales), else the reason."""
    old = {(e.kind, e.asset): e for e in stored}
    new = {(str(leg.kind), leg.asset): leg for leg in legs}
    if old.keys() != new.keys():
        return f"legs differ: stored {sorted(old)} vs venue {sorted(new)}"
    for key, e in old.items():
        n = new[key]
        d = max(e.decimals, n.decimals)
        if rescale(e.delta_atoms, e.decimals, d) != rescale(n.delta_atoms, n.decimals, d):
            return (
                f"{key[0]} {key[1]}: stored {e.delta_atoms} at {e.decimals} dp, "
                f"venue {n.delta_atoms} at {n.decimals} dp"
            )
        if (
            e.balance_after_atoms is not None
            and n.balance_after_atoms is not None
            and rescale(e.balance_after_atoms, e.decimals, d)
            != rescale(n.balance_after_atoms, n.decimals, d)
        ):
            return f"{key[0]} {key[1]}: running balance changed"
    return None


def _group_state(
    db: Session, account: VenueAccount, group_ref: str, venue_ref: str, legs: list[NormalizedLeg]
) -> tuple[str, str | None]:
    """("new", None) | ("same", None) | ("mismatch", reason)."""
    stored = db.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.venue_account_id == account.id, LedgerEntry.group_ref == group_ref)
        .order_by(LedgerEntry.id)
    ).all()
    if stored:
        reason = _legs_mismatch(list(stored), legs)
        return ("mismatch", f"{group_ref}: {reason}") if reason else ("same", None)
    if legs:
        # The table identity is (account, venue_ref, kind, asset): another
        # event that reused this venue_ref would collide on insert.
        clash = db.scalar(
            select(LedgerEntry.group_ref)
            .where(
                LedgerEntry.venue_account_id == account.id,
                LedgerEntry.venue_ref == venue_ref,
                or_(
                    *(
                        and_(LedgerEntry.kind == str(leg.kind), LedgerEntry.asset == leg.asset)
                        for leg in legs
                    )
                ),
            )
            .limit(1)
        )
        if clash is not None:
            return "mismatch", f"{group_ref}: venue_ref already used by {clash}"
    return "new", None


def _asset_fill_mismatch(stored: AssetFill, fill: NormalizedAssetFill, inst_id: int) -> str | None:
    d = max(stored.qty_decimals, fill.instrument.qty_decimals)
    try:
        same_price = Decimal(stored.price) == Decimal(fill.price)
    except InvalidOperation:
        same_price = stored.price == fill.price
    if (
        stored.instrument_id != inst_id
        or stored.action != fill.action.value
        or rescale(stored.qty_atoms, stored.qty_decimals, d)
        != rescale(fill.qty_atoms, fill.instrument.qty_decimals, d)
        or not same_price
    ):
        return f"fill:{fill.venue_fill_id}: venue changed the fill under the same id"
    return None


# ---- asset fills and events ------------------------------------------------------


def ingest_asset_fills(
    db: Session, account: VenueAccount, fills: list[NormalizedAssetFill]
) -> LedgerWriteResult:
    """Append asset fills and their legs. A fill seen before is skipped when
    it matches exactly; a partial or different duplicate quarantines the
    account and is not written. Options are out of scope and rejected.
    Flushes, does not commit."""
    result = LedgerWriteResult()
    known: dict[str, int] = {}  # symbol -> instrument id
    upserted: dict[str, Instrument] = {}  # one upsert per instrument description

    for fill in fills:
        if fill.instrument.type is InstrumentType.OPTION:
            logger.warning("account %s: option fill %s rejected", account.id, fill.venue_fill_id)
            result.rejected += 1
            continue
        described = fill.instrument.model_dump_json()
        inst = upserted.get(described)
        if inst is None:
            inst = upsert_instrument(db, account.venue, fill.instrument, fill.filled_at)
            upserted[described] = inst
        known[inst.symbol] = inst.id
        ref = fill.venue_fill_id
        group_ref = group_ref_for("fill", ref)
        legs = nonzero_legs(fill.legs)
        state, reason = _group_state(db, account, group_ref, ref, legs)
        stored = db.scalar(
            select(AssetFill).where(
                AssetFill.venue_account_id == account.id, AssetFill.venue_fill_id == ref
            )
        )
        if stored is not None:
            reason = reason or _asset_fill_mismatch(stored, fill, inst.id)
            if state == "new" and legs:
                reason = reason or f"{group_ref}: partial duplicate (fill stored without its legs)"
        elif state == "same":
            reason = f"{group_ref}: partial duplicate (legs stored without the fill row)"
        if reason:
            quarantine_account(db, account, reason, ref=group_ref)
            result.quarantined.append(reason)
            continue
        if stored is not None:
            result.duplicates += 1
            continue

        db.add(
            AssetFill(
                venue_account_id=account.id,
                instrument_id=inst.id,
                venue_fill_id=ref,
                order_id=fill.order_id,
                action=fill.action.value,
                qty_atoms=fill.qty_atoms,
                qty_decimals=fill.instrument.qty_decimals,
                price=fill.price,
                liquidity=fill.liquidity,
                filled_at=fill.filled_at,
            )
        )
        db.add_all(
            _entries(
                account.id,
                legs,
                ref,
                group_ref,
                fill.filled_at,
                lambda leg, default=inst.id: _instrument_id_of(
                    db, account.venue, leg, default, known
                ),
            )
        )
        db.flush()
        result.new += 1
    return result


def _instrument_id_of(
    db: Session, venue: str, leg: NormalizedLeg, default: int | None, known: dict[str, int]
) -> int | None:
    """The instrument a leg names (instrument_symbol), else `default`."""
    symbol = leg.instrument_symbol
    if symbol is None:
        return default
    if symbol not in known:
        found = db.scalar(
            select(Instrument.id).where(Instrument.venue == venue, Instrument.symbol == symbol)
        )
        if found is None:
            return default
        known[symbol] = found
    return known[symbol]


def ingest_events(
    db: Session, account: VenueAccount, events: list[NormalizedEvent]
) -> LedgerWriteResult:
    """Append non-fill events (transfers, dividends, funding, ...) as legs,
    with the same event-granularity idempotency as fills. Flushes only."""
    result = LedgerWriteResult()
    known: dict[str, int] = {}
    for event in events:
        group_ref = group_ref_for("event", event.venue_ref, str(event.kind))
        legs = nonzero_legs(event.legs)
        state, reason = _group_state(db, account, group_ref, event.venue_ref, legs)
        if state == "same":
            result.duplicates += 1
            continue
        if reason:
            quarantine_account(db, account, reason, ref=group_ref)
            result.quarantined.append(reason)
            continue
        if not legs:
            continue
        db.add_all(
            _entries(
                account.id,
                legs,
                event.venue_ref,
                group_ref,
                event.occurred_at,
                lambda leg: _instrument_id_of(db, account.venue, leg, None, known),
            )
        )
        db.flush()
        result.new += 1
    return result


# ---- resumable, page-level ingestion (a job runner calls these) ----------------


def stored_resume_cursor(db: Session, account: VenueAccount, stream: str) -> str | None:
    """What the last completed pass handed back for `stream` (None: use since)."""
    return db.scalar(
        select(LedgerSyncState.resume_cursor).where(
            LedgerSyncState.venue_account_id == account.id, LedgerSyncState.stream == stream
        )
    )


def _store_resume_cursor(
    db: Session, account: VenueAccount, stream: str, cursor: str | None
) -> None:
    row = db.scalar(
        select(LedgerSyncState).where(
            LedgerSyncState.venue_account_id == account.id, LedgerSyncState.stream == stream
        )
    )
    if row is None:
        db.add(LedgerSyncState(venue_account_id=account.id, stream=stream, resume_cursor=cursor))
    else:
        row.resume_cursor = cursor
        row.updated_at = utcnow()


def sync_since(db: Session, account: VenueAccount, stream: str) -> datetime | None:
    """Newest stored timestamp of the stream minus OVERLAP; None = full history."""
    if stream == STREAM_ASSET_FILLS:
        newest = db.scalar(
            select(func.max(AssetFill.filled_at)).where(AssetFill.venue_account_id == account.id)
        )
    else:
        newest = db.scalar(
            select(func.max(LedgerEntry.occurred_at)).where(
                LedgerEntry.venue_account_id == account.id,
                LedgerEntry.group_ref.startswith("event:"),
            )
        )
    return None if newest is None else aware(newest) - OVERLAP


def ingest_asset_fills_page(
    db: Session,
    account: VenueAccount,
    connector: VenueConnector,
    cursor: str | None = None,
    since: datetime | None = None,
    into: LedgerWriteResult | None = None,
) -> PageCursors:
    """Fetch ONE page (one venue request), append it, store the resume cursor
    if it is the final page, and commit. BudgetExhausted/RateLimited raised by
    the fetch leave nothing half-written; the caller retries with `cursor`."""
    page = connector.fetch_asset_fills_page(cursor, since)
    result = ingest_asset_fills(db, account, page.items)
    if page.next_cursor is None:
        _store_resume_cursor(db, account, STREAM_ASSET_FILLS, page.resume_cursor)
    db.commit()
    if into is not None:
        into.merge(result)
    return PageCursors(page.next_cursor, page.resume_cursor)


def ingest_events_page(
    db: Session,
    account: VenueAccount,
    connector: VenueConnector,
    cursor: str | None = None,
    since: datetime | None = None,
    into: LedgerWriteResult | None = None,
) -> PageCursors:
    """Same as ingest_asset_fills_page for non-fill events."""
    page = connector.fetch_events_page(cursor, since)
    result = ingest_events(db, account, page.items)
    if page.next_cursor is None:
        _store_resume_cursor(db, account, STREAM_EVENTS, page.resume_cursor)
    db.commit()
    if into is not None:
        into.merge(result)
    return PageCursors(page.next_cursor, page.resume_cursor)


def _run_stream(db: Session, account: VenueAccount, connector, stream: str, page_fn):
    result = LedgerWriteResult()
    cursor = stored_resume_cursor(db, account, stream)
    since = sync_since(db, account, stream)
    followed: set[str] = set()
    while True:
        next_cursor, _ = page_fn(db, account, connector, cursor, since, into=result)
        if next_cursor is None:
            return result
        if next_cursor in followed:
            raise RuntimeError(f"{account.venue} {stream}: connector repeated cursor")
        followed.add(next_cursor)
        cursor = next_cursor


def _sync_assets(db: Session, connector: VenueConnector, account: VenueAccount) -> SyncResult:
    """One full incremental pass of both v2 streams (asset venues)."""
    fills = _run_stream(db, account, connector, STREAM_ASSET_FILLS, ingest_asset_fills_page)
    events = _run_stream(db, account, connector, STREAM_EVENTS, ingest_events_page)
    logger.info(
        "synced venue account %s: +%d asset fills, +%d events (%d quarantined)",
        account.id,
        fills.new,
        events.new,
        len(fills.quarantined) + len(events.quarantined),
    )
    return SyncResult(new_fills=fills.new, new_settlements=0, new_events=events.new)


# ---- reading legs back -------------------------------------------------------------


def load_leg_groups(db: Session, account_id: int) -> list[LegGroup]:
    """Every stored event of the account as a LegGroup, in ingestion order
    (first leg id); ledger.cash_curve_from_groups sorts by time from there."""
    rows = db.execute(
        select(LedgerEntry, Instrument.symbol)
        .outerjoin(Instrument, LedgerEntry.instrument_id == Instrument.id)
        .where(LedgerEntry.venue_account_id == account_id)
        .order_by(LedgerEntry.id)
    ).all()
    grouped: dict[str, tuple[LedgerEntry, str | None, list[LedgerEntry]]] = {}
    for entry, symbol in rows:
        if entry.group_ref not in grouped:
            grouped[entry.group_ref] = (entry, symbol, [])
        grouped[entry.group_ref][2].append(entry)
    return [
        LegGroup(aware(first.occurred_at), group_kind(ref), symbol or "", tuple(legs))
        for ref, (first, symbol, legs) in grouped.items()
    ]


def account_entries(
    db: Session, account_id: int, after: datetime | None = None
) -> list[LedgerEntry]:
    """The account's legs in ingestion order, optionally only those after
    `after` (strictly: an anchor already contains what happened at it)."""
    query = select(LedgerEntry).where(LedgerEntry.venue_account_id == account_id)
    rows = db.scalars(query.order_by(LedgerEntry.id)).all()
    if after is None:
        return list(rows)
    after = aware(after)
    return [r for r in rows if aware(r.occurred_at) > after]
