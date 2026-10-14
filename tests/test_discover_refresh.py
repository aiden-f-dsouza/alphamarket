"""Watch discover data (copy strategies step 7): the venues' public
leaderboards (Hyperliquid stats-data document, Polymarket /v2/leaderboard),
discover.refresh and its worker wiring. Venue traffic is respx-mocked from
anonymised recorded fixtures (invariant 6); expected values are worked out
by hand from the fixture rows in the comments."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import discover, worker
from alphamarket.config import get_settings
from alphamarket.connectors.base import (
    LeaderboardEntry,
    LeaderboardPeriod,
    RateLimited,
    leaderboard_figure,
)
from alphamarket.connectors.hyperliquid import (
    LEADERBOARD_URLS,
    HyperliquidMarketData,
    iter_leaderboard_rows,
)
from alphamarket.connectors.polymarket import PolymarketMarketData
from alphamarket.jobs import HANDLERS, JobKind
from alphamarket.models import Job, LeaderboardSnapshot, User, WatchOptout
from alphamarket.money import to_micros
from alphamarket.venue_limits import MemoryBuckets, VenueLimiter

FIXTURES = Path(__file__).parent / "fixtures"
HL_DOC = (FIXTURES / "hyperliquid" / "leaderboard_2026-10-14.json").read_text()
PM_PAGE = json.loads((FIXTURES / "polymarket" / "leaderboard_week_20261014.json").read_text())
HL_URL = LEADERBOARD_URLS["prod"]
PM_URL = "https://data-api.polymarket.com/v2/leaderboard"
NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
WEEK = LeaderboardPeriod.WEEK
ENTRY_FIELDS = {"address", "rank", "pnl", "roi", "volume", "account_value"}


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def _n(address: str) -> int:
    return int(address, 16)


@pytest.fixture()
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "watch_discover_enabled", True)
    monkeypatch.setattr(s, "watch_venues", "hyperliquid,polymarket")
    monkeypatch.setattr(s, "watch_discover_rows", 2)
    monkeypatch.setattr(s, "watch_discover_min_account_value", 10_000)
    return s


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Polymarket's _PublicHttp spaces requests 0.11 s apart; not in tests.
    monkeypatch.setattr("alphamarket.connectors.polymarket.time.sleep", lambda _s: None)


def _ctx(db, limiter=None):
    return SimpleNamespace(db=db, limiter=limiter, clock=lambda: NOW, settings=get_settings())


def _pm_page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=PM_PAGE)


# ---- money figures ------------------------------------------------------------------


def test_leaderboard_figure_rounds_half_even_to_micros():
    # to_micros refuses anything finer than 1e-6 (invariant 1), and the venues
    # print up to 10 dp: the display figures are rounded once, explicitly.
    with pytest.raises(ValueError):
        to_micros("51126546.9711679965")
    assert leaderboard_figure("51126546.9711679965") == "51126546.971168"
    assert leaderboard_figure("0.0000005") == "0.000000"  # half-even: to the even 0
    assert leaderboard_figure("0.0000015") == "0.000002"  # half-even: to the even 2
    assert leaderboard_figure("-5796558.3265969995") == "-5796558.326597"
    assert leaderboard_figure("698.1") == "698.100000"
    assert leaderboard_figure(Decimal("1E+3")) == "1000.000000"  # never exponent notation


# ---- Hyperliquid: streamed document -----------------------------------------------------


def test_iter_rows_survives_any_chunking_and_odd_shapes():
    doc = '{"leaderboardRows": [{"a": "x,]"}, {"a": [1, {"b": "}"}]}\n,{"a": null}]}'
    expected = json.loads(doc)["leaderboardRows"]
    for size in (1, 2, 3, 7, 1000):
        chunks = [doc[i : i + size] for i in range(0, len(doc), size)]
        assert list(iter_leaderboard_rows(chunks)) == expected
    # Another key first: falls back to a full parse instead of failing.
    other = '{"generatedAt": 1, "leaderboardRows": [{"a": 1}]}'
    assert list(iter_leaderboard_rows([other[:5], other[5:]])) == [{"a": 1}]
    assert list(iter_leaderboard_rows(['{"leaderboardRows": []}'])) == []
    with pytest.raises(ValueError):  # body cut off mid-row
        list(iter_leaderboard_rows(['{"leaderboardRows": [{"a": 1}, {"a"']))


@respx.mock
def test_hyperliquid_leaderboard_parses_ranks_and_drops_identity():
    route = respx.get(HL_URL).mock(return_value=httpx.Response(200, text=HL_DOC))
    md = HyperliquidMarketData()  # no bound, no floor: every fixture row
    boards = md.fetch_leaderboards(tuple(LeaderboardPeriod))
    assert route.call_count == 1  # one document serves every period
    assert set(boards) == set(LeaderboardPeriod)

    week = boards[WEEK]
    assert [e.rank for e in week] == list(range(1, 28))  # 27 rows, ranked by week pnl
    # Week pnl, highest first: row 1 51,126,546.97; row 2 47,327,853.36;
    # row 3 12,338,539.80; ... row 10 8,448,157.47 (9th); row 25 6,930,754.19
    # (10th); row 9 2,682,416.35 (11th); ... the lowest is row 20 at -6,686,824.41.
    assert [_n(e.address) for e in week[:3]] == [1, 2, 3]
    assert [_n(e.address) for e in week[8:11]] == [10, 25, 9]
    assert _n(week[-1].address) == 20 and week[-1].pnl == "-6686824.406910"
    top = week[0]
    assert top.model_dump() == {
        "address": addr(1),
        "rank": 1,
        "pnl": "51126546.971168",  # 51126546.9711679965 rounded half-even
        "roi": "0.2915420951",  # verbatim: a display fraction, not money
        "volume": "41802185.900000",  # 41802185.8999999985
        "account_value": "278354896.324668",  # 278354896.3246679902
    }
    # allTime maps to "all": row 1 168.8M, row 10 141.1M, row 2 131.8M.
    assert [_n(e.address) for e in boards[LeaderboardPeriod.ALL][:3]] == [1, 10, 2]

    # displayName and prize never leave the connector.
    dumped = json.dumps([e.model_dump() for b in boards.values() for e in b])
    assert "placeholder-name" not in dumped and "prize" not in dumped
    assert all(set(e.model_dump()) == ENTRY_FIELDS for b in boards.values() for e in b)
    # every figure fits integer micros
    assert all(discover.snapshot_row(e) for b in boards.values() for e in b)


@respx.mock
def test_hyperliquid_leaderboard_bounds_output_but_ranks_over_every_row():
    respx.get(HL_URL).mock(return_value=httpx.Response(200, text=HL_DOC))
    md = HyperliquidMarketData(leaderboard_keep=2, leaderboard_min_account_value="10000")
    week = md.fetch_leaderboards((WEEK,))
    assert list(week) == [WEEK]  # only what was asked
    # Accounts >= $10k: best week pnl rows 1, 2; best week roi row 5 (0.4138)
    # and row 4 (0.3431). Ranks stay the full-board ranks.
    assert [(_n(e.address), e.rank) for e in week[WEEK]] == [(1, 1), (2, 2), (4, 4), (5, 5)]


@respx.mock
def test_hyperliquid_leaderboard_skips_rows_with_no_volume_in_the_period():
    respx.get(HL_URL).mock(return_value=httpx.Response(200, text=HL_DOC))
    md = HyperliquidMarketData(leaderboard_keep=100, leaderboard_min_account_value="0")
    boards = md.fetch_leaderboards((LeaderboardPeriod.DAY, WEEK))
    # Rows 4, 20, 21, 22 and 23 have day vlm "0.0" (protocol-style accounts
    # that top the board on mark-to-market alone): gone from day, still in week.
    day = {_n(e.address) for e in boards[LeaderboardPeriod.DAY]}
    assert day == set(range(1, 28)) - {4, 20, 21, 22, 23}
    assert {_n(e.address) for e in boards[WEEK]} == set(range(1, 28))


@respx.mock
def test_hyperliquid_leaderboard_429_is_rate_limited_and_throttled():
    calls = []

    class Recorder:
        def before(self, endpoint):
            calls.append(endpoint)

        def after(self, endpoint, extra_cost=0, used=None):
            calls.append(("after", endpoint))

    respx.get(HL_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    md = HyperliquidMarketData()
    md.set_throttle(Recorder())
    with pytest.raises(RateLimited) as exc:
        md.fetch_leaderboards((WEEK,))
    assert exc.value.retry_after_seconds == 30
    assert calls == ["leaderboard"]  # charged once, no per-item weight


def test_hyperliquid_testnet_document_follows_the_info_url():
    md = HyperliquidMarketData(info_url="https://api.hyperliquid-testnet.xyz/info")
    assert md.leaderboard_url == "https://stats-data.hyperliquid.xyz/Testnet/leaderboard"


# ---- Polymarket: /v2/leaderboard ------------------------------------------------------


@respx.mock
def test_polymarket_leaderboard_parses_and_drops_identity():
    route = respx.get(PM_URL).mock(side_effect=_pm_page)
    md = PolymarketMarketData(clob_url="https://clob.invalid", leaderboard_keep=30)
    boards = md.fetch_leaderboards((WEEK,))
    assert dict(route.calls.last.request.url.params) == {"time_period": "week", "limit": "30"}
    week = boards[WEEK]
    assert len(week) == 30 and [e.rank for e in week] == list(range(1, 31))
    assert week[0].model_dump() == {
        "address": addr(1),
        "rank": 1,
        "pnl": "3054314.534295",  # float 3054314.534295246
        "roi": None,  # the board has no roi
        "volume": None,  # volume is in SHARES, not dollars: dropped
        "account_value": None,  # no account value either
    }
    assert week[8].pnl == "479287.405400"  # float 479287.40539999993
    dumped = json.dumps([e.model_dump() for e in week])
    for marker in ("placeholder-name", "placeholder-handle", "example.invalid", "verified"):
        assert marker not in dumped


@respx.mock
def test_polymarket_leaderboard_follows_the_cursor_for_deeper_boards():
    extra = {
        "data": [
            {
                "rank": 30 + i,
                "user_id": addr(100 + i),
                "pnl": 1000.5 - i,
                "volume": 1.0,
                "user_name": "placeholder",
                "profile_image": "",
                "x_username": "",
                "verified": False,
            }
            for i in range(1, 11)
        ],
        "pagination": {"limit": 30, "offset": 30, "has_more": False, "next_cursor": None},
    }
    pages = iter([PM_PAGE, extra])
    route = respx.get(PM_URL).mock(side_effect=lambda req: httpx.Response(200, json=next(pages)))
    md = PolymarketMarketData(clob_url="https://clob.invalid", leaderboard_keep=35)
    week = md.fetch_leaderboards((WEEK,))[WEEK]
    # 30 rows from page one, then 5 of page two: the 5th is row 105, 1000.5 - 5.
    assert len(week) == 35 and _n(week[-1].address) == 105 and week[-1].pnl == "995.500000"
    # The cursor pins board and window: the second request sends nothing else.
    second = dict(route.calls[1].request.url.params)
    assert second == {"cursor": PM_PAGE["pagination"]["next_cursor"]}


# ---- discover.select_rows ------------------------------------------------------------------


def test_select_rows_filters_and_unions_pnl_and_roi(settings):
    def e(n, rank, pnl, roi=None, value=None):
        return LeaderboardEntry(address=addr(n), rank=rank, pnl=pnl, roi=roi, account_value=value)

    entries = [
        e(1, 1, "900", "0.10", "50000"),
        e(2, 2, "800", "0.05", "9999.999999"),  # under the $10k floor: dropped
        e(3, 3, "700", None, None),  # no account value reported: kept
        e(4, 4, "600", "0.90", "20000"),
        e(5, 5, "500", "0.70", "10000"),  # exactly the floor: kept
        e(6, 6, "400", "0.95", "15000"),  # opted out
        e(7, 7, "300", "0.01", "80000"),
    ]
    rows = discover.select_rows(entries, blocked={addr(6)})
    # Survivors 1, 3, 4, 5, 7. Top 2 by pnl: 1 (900), 3 (700). Top 2 by roi:
    # 4 (0.90), 5 (0.70). Union in pnl order: 1, 3, 4, 5.
    assert [_n(r["address"]) for r in rows] == [1, 3, 4, 5]
    assert rows[0] == {
        "address": addr(1),
        "rank": 1,
        "pnl_micros": 900_000_000,
        "roi": "0.10",
        "volume_micros": None,
        "account_value_micros": 50_000_000_000,
    }


def test_select_rows_drops_a_reported_zero_volume(settings):
    entries = [
        LeaderboardEntry(address=addr(1), rank=1, pnl="900", volume="0"),  # dropped
        LeaderboardEntry(address=addr(2), rank=2, pnl="800", volume="0.000001"),
        LeaderboardEntry(address=addr(3), rank=3, pnl="700"),  # volume not reported: kept
    ]
    rows = discover.select_rows(entries, blocked=set())
    assert [_n(r["address"]) for r in rows] == [2, 3]


# ---- discover.refresh ------------------------------------------------------------------


def _system_user(db) -> User:
    u = User(email="owner@example.com", password_hash="!", display_name="owner")
    db.add(u)
    db.flush()
    return u


def _snapshots(db) -> dict[tuple[str, str], list[int]]:
    return {
        (s.venue, s.period): [_n(r["address"]) for r in s.rows]
        for s in db.scalars(select(LeaderboardSnapshot))
    }


@respx.mock
def test_refresh_end_to_end(session_factory, settings):
    hl = respx.get(HL_URL).mock(return_value=httpx.Response(200, text=HL_DOC))
    pm = respx.get(PM_URL).mock(side_effect=_pm_page)
    with session_factory() as db:
        owner = _system_user(db)
        db.add(WatchOptout(venue="hyperliquid", address=addr(1), proved_by_user_id=owner.id))
        db.commit()
        counts = discover.refresh(_ctx(db))

        assert hl.call_count == 1 and pm.call_count == 4  # one document; one page per period
        venues = discover.discover_venues()
        assert venues == ("hyperliquid", "polymarket")
        assert set(counts) == {(v, p.value) for v in venues for p in LeaderboardPeriod}
        # Hyperliquid week, rows=2, floor $10k, row 1 opted out. The connector
        # (keep 4) returns the best 4 by pnl {1,2,3,4} and by roi {5,4,1,2};
        # dropping row 1 leaves top pnl 2, 3 and top roi 5 (0.4138), 4 (0.3431).
        assert counts[("hyperliquid", "week")] == 4
        snaps = _snapshots(db)
        assert snaps[("hyperliquid", "week")] == [2, 3, 4, 5]
        assert all(1 not in rows for (v, _), rows in snaps.items() if v == "hyperliquid")
        week = db.scalar(
            select(LeaderboardSnapshot).where(
                LeaderboardSnapshot.venue == "hyperliquid", LeaderboardSnapshot.period == "week"
            )
        )
        assert week.fetched_at.replace(tzinfo=UTC) == NOW
        assert week.rows[0] == {
            "address": addr(2),
            "rank": 2,
            "pnl_micros": 47_327_853_355_939,  # 47327853.3559390008
            "roi": "0.2690481056",
            "volume_micros": 99_757_955_860_000,  # 99757955.8599999994
            "account_value_micros": 288_989_957_192_498,  # 288989957.1924980283
        }
        # Polymarket: no roi, no account value, so the top 2 by pnl per period.
        for period in ("day", "week", "month", "all"):
            assert counts[("polymarket", period)] == 2
            assert snaps[("polymarket", period)] == [1, 2]


@respx.mock
def test_refresh_one_venue_failing_leaves_the_other(session_factory, settings):
    respx.get(HL_URL).mock(return_value=httpx.Response(500))
    respx.get(PM_URL).mock(side_effect=_pm_page)
    limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda v: ())
    with session_factory() as db:
        discover.store_snapshot(db, "hyperliquid", "week", [], NOW - timedelta(hours=3))
        db.commit()
        counts = discover.refresh(_ctx(db, limiter))
        assert set(counts) == {("polymarket", p) for p in ("day", "week", "month", "all")}
        snaps = _snapshots(db)
        assert snaps[("hyperliquid", "week")] == []  # the old snapshot is kept
        assert ("hyperliquid", "day") not in snaps
    assert limiter.backend._circuits["hyperliquid"][0] == 1  # fed the breaker


@respx.mock
def test_refresh_skips_open_breaker_and_leaves_429_for_next_tick(session_factory, settings):
    hl = respx.get(HL_URL).mock(return_value=httpx.Response(200, text=HL_DOC))
    pm = respx.get(PM_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "5"}, json={})
    )
    limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda v: (), breaker_threshold=1)
    limiter.record_failure("hyperliquid")  # opens its circuit
    with session_factory() as db:
        assert discover.refresh(_ctx(db, limiter)) == {}
        assert _snapshots(db) == {}
    assert not hl.called  # breaker open: not even asked
    # 429: _PublicHttp's own bounded retries (MAX_ATTEMPTS), then left alone;
    # the job does not loop on it.
    assert pm.call_count == 4


def test_refresh_does_nothing_when_disabled(session_factory, settings, monkeypatch):
    monkeypatch.setattr(settings, "watch_discover_enabled", False)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(HL_URL)
        with session_factory() as db:
            assert discover.refresh(_ctx(db)) == {}
        assert not route.called


# ---- worker wiring ---------------------------------------------------------------------


def _discover_jobs(db) -> list[Job]:
    return list(db.scalars(select(Job).where(Job.kind == JobKind.WATCH_DISCOVER)))


def test_handler_is_registered():
    assert HANDLERS[JobKind.WATCH_DISCOVER] is worker.handle_watch_discover


def test_tick_enqueues_only_when_enabled(session_factory, settings, monkeypatch):
    monkeypatch.setattr(settings, "watch_discover_enabled", False)
    worker.tick_watch_discover(session_factory)
    with session_factory() as db:
        assert _discover_jobs(db) == []

    monkeypatch.setattr(settings, "watch_discover_enabled", True)
    worker.tick_watch_discover(session_factory)
    worker.tick_watch_discover(session_factory)  # deduped while the first is pending
    with session_factory() as db:
        jobs = _discover_jobs(db)
        assert len(jobs) == 1 and jobs[0].venue_account_id is None


def test_startup_tick_skips_fresh_snapshots(session_factory, settings, monkeypatch):
    monkeypatch.setattr(worker, "utcnow", lambda: NOW)
    with session_factory() as db:
        discover.store_snapshot(db, "hyperliquid", "week", [], NOW - timedelta(minutes=10))
        db.commit()
    worker.tick_watch_discover(session_factory, only_if_stale=True)
    with session_factory() as db:
        assert _discover_jobs(db) == []  # 10 minutes old, interval 60

        snap = db.scalar(select(LeaderboardSnapshot))
        snap.fetched_at = NOW - timedelta(minutes=61)
        db.commit()
    worker.tick_watch_discover(session_factory, only_if_stale=True)
    with session_factory() as db:
        assert len(_discover_jobs(db)) == 1
