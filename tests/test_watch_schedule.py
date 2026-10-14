"""Private-watchlist scheduling (copy_strategies B4): how often each kind of
shared `is_watch` account is polled, that a watch sync is claimed after every
creator sync, and that an idle watch that gets tailed does not wait out its
idle slot. SQLite; the hand-computed slots come from jobs.next_sync_slot."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import jobs, worker
from alphamarket.jobs import (
    PRIORITY_MAINTENANCE,
    PRIORITY_SYNC,
    PRIORITY_SYNC_FOLLOWED,
    PRIORITY_SYNC_WATCH,
    JobKind,
    WatchCadence,
    claim,
    enqueue,
    enqueue_due_syncs,
    next_sync_slot,
    watch_cadences,
)
from alphamarket.models import (
    AccountSchedule,
    Allocation,
    AllocationStatus,
    Follow,
    Job,
    Strategy,
    StrategyKind,
    User,
    VenueAccount,
    Watch,
    WatchOptout,
)

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
NORMAL = 300
IDLE = 3600


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def make_creator(db, key_id="creator", followed=False) -> VenueAccount:
    user = User(email=f"{key_id}@example.com", password_hash="x", display_name=key_id)
    db.add(user)
    db.flush()
    account = VenueAccount(user_id=user.id, venue="hyperliquid", venue_env="prod", key_id=key_id)
    db.add(account)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=account.id, name="c", slug=f"s-{key_id}")
    db.add(strategy)
    db.flush()
    if followed:
        db.add(Follow(user_id=user.id, strategy_id=strategy.id))
    db.commit()
    return account


def make_watch(
    db, n: int, *, watchers=0, active=0, stopped=0, optout=False, system: User | None = None
) -> VenueAccount:
    """A shared watch account for address n with `watchers` Watch rows,
    `active` ACTIVE and `stopped` STOPPED allocations."""
    system = system or db.scalar(select(User).where(User.is_system.is_(True)))
    if system is None:
        system = User(
            email="system@koine.invalid", password_hash="!", display_name="s", is_system=True
        )
        db.add(system)
        db.flush()
    account = VenueAccount(
        user_id=system.id, venue="hyperliquid", venue_env="prod", key_id=addr(n), is_watch=True
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=system.id,
        venue_account_id=account.id,
        name="w",
        slug=f"watch-{n}",
        kind=StrategyKind.WATCH,
        public=False,
    )
    db.add(strategy)
    db.flush()
    for i in range(watchers + active + stopped):
        user = User(email=f"u{n}-{i}@example.com", password_hash="x", display_name="u")
        db.add(user)
        db.flush()
        if i < watchers:
            db.add(Watch(user_id=user.id, strategy_id=strategy.id))
        if watchers <= i < watchers + active + stopped:
            status = AllocationStatus.ACTIVE if i < watchers + active else AllocationStatus.STOPPED
            db.add(
                Allocation(
                    user_id=user.id,
                    strategy_id=strategy.id,
                    allocation_micros=1_000_000_000,
                    drawdown_limit_bps=1500,
                    high_water_micros=1_000_000_000,
                    status=status,
                )
            )
    if optout:
        db.add(WatchOptout(venue="hyperliquid", address=addr(n), proved_by_user_id=system.id))
    db.commit()
    return account


def tail(db, account: VenueAccount) -> None:
    """Start an ACTIVE paper tail on the watch (a user who is not a watcher)."""
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    user = User(email=f"t{account.id}@example.com", password_hash="x", display_name="t")
    db.add(user)
    db.flush()
    db.add(
        Allocation(
            user_id=user.id,
            strategy_id=strategy.id,
            allocation_micros=1_000_000_000,
            drawdown_limit_bps=1500,
            high_water_micros=1_000_000_000,
        )
    )
    db.commit()


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


def tick(db, now, **kw):
    return enqueue_due_syncs(
        db, interval_seconds=NORMAL, watch_idle_interval_seconds=IDLE, now=now, **kw
    )


def queued(db) -> dict[int, int]:
    """account id -> priority of its PENDING sync."""
    return {
        j.venue_account_id: j.priority
        for j in db.scalars(select(Job).where(Job.kind == JobKind.SYNC, Job.status == "pending"))
    }


def next_at(db, account) -> datetime | None:
    sched = db.get(AccountSchedule, account.id)
    return jobs.aware(sched.next_sync_at) if sched else None


# ---- cadence per state -----------------------------------------------------------


def test_cadence_classification(db):
    tailed = make_watch(db, 1, active=1)
    idle = make_watch(db, 2, watchers=1)
    dormant = make_watch(db, 3)
    stopped = make_watch(db, 4, stopped=2)
    stopped_but_watched = make_watch(db, 5, watchers=1, stopped=1)
    optout = make_watch(db, 6, watchers=2, active=1, optout=True)
    make_creator(db)
    assert watch_cadences(db) == {
        tailed.id: WatchCadence.TAILED,
        idle.id: WatchCadence.IDLE,
        dormant.id: WatchCadence.DORMANT,
        stopped.id: WatchCadence.DORMANT,  # a closing tail needs quotes, not syncs
        stopped_but_watched.id: WatchCadence.IDLE,
        optout.id: WatchCadence.OPTED_OUT,  # beats the Watch rows and the ACTIVE tail
    }


def test_each_state_is_polled_at_its_own_cadence(db):
    creator = make_creator(db, "creator", followed=True)
    plain = make_creator(db, "plain")
    tailed = make_watch(db, 1, active=1)
    idle = make_watch(db, 2, watchers=1)
    dormant = make_watch(db, 3)
    closing = make_watch(db, 4, stopped=1)  # STOPPED tail still closing its book
    optout = make_watch(db, 5, watchers=1, active=1, optout=True)

    assert tick(db, T0) == 4
    assert queued(db) == {
        creator.id: PRIORITY_SYNC_FOLLOWED,
        plain.id: PRIORITY_SYNC,
        tailed.id: PRIORITY_SYNC_WATCH,
        idle.id: PRIORITY_SYNC_WATCH,
    }
    # next_sync_at: creators and tails at the normal interval, idle at the idle one,
    # the rest untouched (no schedule row is even created).
    assert next_at(db, creator) == next_sync_slot(creator.id, T0, NORMAL)
    assert next_at(db, plain) == next_sync_slot(plain.id, T0, NORMAL)
    assert next_at(db, tailed) == next_sync_slot(tailed.id, T0, NORMAL)
    assert next_at(db, idle) == next_sync_slot(idle.id, T0, IDLE)
    for skipped in (dormant, closing, optout):
        assert db.get(AccountSchedule, skipped.id) is None

    # Finish the first round of jobs; one normal interval later the creators and
    # the tail are due again but the idle watch (and the skipped ones) are not.
    for job in db.scalars(select(Job)).all():
        job.status = "done"
    db.commit()
    later = T0 + timedelta(seconds=NORMAL + 1)
    assert tick(db, later) == 3
    assert set(queued(db)) == {creator.id, plain.id, tailed.id}

    # After the idle interval the idle watch is due too; the skipped ones never are.
    for job in db.scalars(select(Job)).all():
        job.status = "done"
    db.commit()
    assert tick(db, T0 + timedelta(seconds=IDLE + 1)) == 4
    assert set(queued(db)) == {creator.id, plain.id, tailed.id, idle.id}


def test_non_watch_creators_are_unchanged(db):
    """The same account list, scheduled with the old arguments, gives the old
    jobs: priority by followers, normal interval, disconnected skipped."""
    creator = make_creator(db, "creator", followed=True)
    plain = make_creator(db, "plain")
    gone = make_creator(db, "gone")
    gone.status = "disconnected"
    db.commit()
    assert enqueue_due_syncs(db, interval_seconds=NORMAL, now=T0) == 2  # default idle setting
    assert queued(db) == {creator.id: PRIORITY_SYNC_FOLLOWED, plain.id: PRIORITY_SYNC}
    assert next_at(db, plain) == next_sync_slot(plain.id, T0, NORMAL)
    assert enqueue_due_syncs(db, interval_seconds=NORMAL, now=T0 + timedelta(seconds=1)) == 0


def test_a_watch_account_that_is_not_active_is_not_scheduled(db):
    idle = make_watch(db, 1, watchers=1)
    idle.status = "suspended"
    db.commit()
    assert tick(db, T0) == 0


def test_tick_syncs_passes_the_idle_setting(session_factory, db, monkeypatch):
    idle = make_watch(db, 1, watchers=1)
    settings = jobs.get_settings().model_copy(update={"watch_idle_interval_seconds": 7200})
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "utcnow", lambda: T0)
    worker.tick_syncs(session_factory)
    db.expire_all()
    assert next_at(db, idle) == next_sync_slot(idle.id, T0, 7200)


# ---- priorities ------------------------------------------------------------------


def test_priority_values_put_watches_after_every_creator_sync():
    assert PRIORITY_SYNC_FOLLOWED < PRIORITY_SYNC < PRIORITY_SYNC_WATCH < PRIORITY_MAINTENANCE


def test_a_creator_sync_is_always_claimed_before_a_watch_sync(db):
    watch = make_watch(db, 1, active=1)  # a tailed watch: would be "followed" for a creator
    watch_idle = make_watch(db, 2, watchers=1)
    followed = make_creator(db, "followed", followed=True)
    plain = make_creator(db, "plain")
    maint = make_creator(db, "maint")
    # Enqueue the watches first and with the earliest run_after, so only the
    # priority can put the creators ahead.
    assert jobs.sync_priority(db, watch.id) == PRIORITY_SYNC_WATCH
    assert jobs.sync_priority(db, watch_idle.id) == PRIORITY_SYNC_WATCH
    for account in (watch, watch_idle, plain, followed):
        enqueue(
            db,
            JobKind.SYNC,
            account_id=account.id,
            priority=jobs.sync_priority(db, account.id),
            run_after=T0 - timedelta(minutes=10 if account in (watch, watch_idle) else 1),
            now=T0,
        )
    enqueue(db, JobKind.RECONCILE, account_id=maint.id, now=T0)
    db.commit()
    got = claim(db, "w1", limit=10, lease_seconds=60, now=T0 + timedelta(seconds=1))
    assert [(j.venue_account_id, j.priority) for j in got] == [
        (followed.id, PRIORITY_SYNC_FOLLOWED),
        (plain.id, PRIORITY_SYNC),
        (watch.id, PRIORITY_SYNC_WATCH),
        (watch_idle.id, PRIORITY_SYNC_WATCH),
        (maint.id, PRIORITY_MAINTENANCE),
    ]


# ---- idle -> tailed ------------------------------------------------------------------


def test_an_idle_watch_that_gets_tailed_syncs_on_the_next_tick(db):
    account = make_watch(db, 1, watchers=1)
    assert tick(db, T0) == 1
    idle_slot = next_at(db, account)
    assert idle_slot == next_sync_slot(account.id, T0, IDLE)
    assert idle_slot - T0 > timedelta(seconds=NORMAL)  # this slot is beyond a normal one
    [job] = db.scalars(select(Job)).all()
    job.status = "done"
    db.commit()

    # Still idle a minute later: not due, however it is polled.
    t1 = T0 + timedelta(seconds=60)
    assert tick(db, t1) == 0

    # Someone starts a tail. The very next tick syncs it (not at idle_slot)...
    tail(db, account)
    t2 = T0 + timedelta(seconds=90)
    assert t2 < idle_slot
    assert tick(db, t2) == 1
    assert queued(db) == {account.id: PRIORITY_SYNC_WATCH}
    # ...and moves it onto the normal cadence.
    assert next_at(db, account) == next_sync_slot(account.id, t2, NORMAL)
    assert next_at(db, account) - t2 <= timedelta(seconds=NORMAL)


def test_a_tailed_watch_with_a_normal_slot_is_not_resynced_early(db):
    account = make_watch(db, 1, active=1)
    assert tick(db, T0) == 1
    [job] = db.scalars(select(Job)).all()
    job.status = "done"
    db.commit()
    slot = next_at(db, account)
    # A normal slot is within one interval, so it is never mistaken for an idle one.
    assert slot - T0 <= timedelta(seconds=NORMAL)
    assert tick(db, slot - timedelta(seconds=1)) == 0
    assert tick(db, slot) == 1


def test_a_tail_that_ends_drops_the_watch_back_to_idle(db):
    account = make_watch(db, 1, watchers=1, active=1)
    assert tick(db, T0) == 1
    [job] = db.scalars(select(Job)).all()
    job.status = "done"
    alloc = db.scalar(select(Allocation))
    alloc.status = AllocationStatus.STOPPED
    db.commit()
    # The pending normal slot still fires once (at most one normal interval away)...
    t1 = T0 + timedelta(seconds=NORMAL + 1)
    assert tick(db, t1) == 1
    # ...and the sync after it is an idle interval out.
    assert next_at(db, account) == next_sync_slot(account.id, t1, IDLE)


def test_a_dormant_watch_that_is_watched_again_is_due_at_once(db):
    account = make_watch(db, 1, watchers=1)
    assert tick(db, T0) == 1
    [job] = db.scalars(select(Job)).all()
    job.status = "done"
    # The only watcher leaves: dormant, nothing is scheduled for a long time.
    db.delete(db.scalar(select(Watch)))
    db.commit()
    assert tick(db, T0 + timedelta(seconds=IDLE * 3)) == 0
    # Someone watches it again after the old idle slot has passed: due now.
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    user = User(email="again@example.com", password_hash="x", display_name="a")
    db.add(user)
    db.flush()
    db.add(Watch(user_id=user.id, strategy_id=strategy.id))
    db.commit()
    assert tick(db, T0 + timedelta(seconds=IDLE * 3 + 30)) == 1


def test_opted_out_is_never_scheduled_even_when_tailed(db):
    account = make_watch(db, 1, watchers=1, active=2, optout=True)
    for i in range(3):
        assert tick(db, T0 + timedelta(seconds=IDLE * i)) == 0
    assert queued(db) == {}
    assert db.get(AccountSchedule, account.id) is None


def test_optout_address_match_is_case_insensitive(db):
    account = make_watch(db, 1, watchers=1)
    account.key_id = account.key_id.upper().replace("0X", "0x")
    db.add(WatchOptout(venue="hyperliquid", address=addr(1), proved_by_user_id=account.user_id))
    db.commit()
    assert watch_cadences(db) == {account.id: WatchCadence.OPTED_OUT}


# ---- backfills and streamed nudges -------------------------------------------------


def test_backfills_skip_dormant_and_opted_out_watches(session_factory, db, monkeypatch):
    monkeypatch.setattr(worker, "_is_paged", lambda account: True)
    idle = make_watch(db, 1, watchers=1)
    tailed = make_watch(db, 2, active=1)
    dormant = make_watch(db, 3)
    optout = make_watch(db, 4, watchers=1, optout=True)
    creator = make_creator(db)
    worker._enqueue_backfills(db, T0)
    db.commit()
    got = {j.venue_account_id for j in db.scalars(select(Job).where(Job.kind == JobKind.BACKFILL))}
    assert got == {idle.id, tailed.id, creator.id}
    assert dormant.id not in got and optout.id not in got


def test_streamed_syncs_and_signals_of_a_watch_stay_below_creators(db):
    from alphamarket import stream

    watch = make_watch(db, 1, active=1)
    creator = make_creator(db, "creator", followed=True)
    stream.nudge_sync(db, watch.id, now=T0)
    stream.nudge_sync(db, creator.id, now=T0)
    prio = {j.venue_account_id: j.priority for j in db.scalars(select(Job))}
    assert prio == {watch.id: PRIORITY_SYNC_WATCH, creator.id: PRIORITY_SYNC_FOLLOWED}
