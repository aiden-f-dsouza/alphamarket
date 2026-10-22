"""The Lighter explorer walk (_Walk) under rows arriving between requests.

The explorer pages newest first by offset, so every row that arrives shifts
every offset. These tests serve a synthetic newest-first log from a list and
add rows before each request is answered, at the rates that livelocked the
first version of the walk (more than the 9-row page overlap per request in
forward mode; rows arriving mid-bisect in locate mode). Each walk must
finish (the index is caught up, so the pass can end and open positions get
funded) and apply every row exactly once, oldest first. No HTTP at all: the
connector's one explorer call (`_logs`) is replaced."""

from datetime import UTC, datetime, timedelta

import pytest

from alphamarket.connectors.lighter import (
    LOGS_LIMIT,
    LOGS_STEP,
    LighterConnector,
    LighterError,
    _locate,
    _Walk,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
ADDR = "0x00000000000000000000000000000000000000b2"


class Log:
    """A newest-first log; `per_request` rows arrive before each request is
    served (the worst case: the walk never sees a quiet log)."""

    def __init__(self, rows: int, per_request: int = 0) -> None:
        self.rows: list[dict] = []
        self.count = 0
        self.per_request = per_request
        self.offsets: list[int] = []
        self.add(rows)

    def add(self, n: int) -> None:
        new = [
            {
                "hash": f"h{self.count + i:06d}",
                "time": (T0 + timedelta(seconds=self.count + i)).isoformat(),
                "pubdata_type": "L2UpdateLeverage",
            }
            for i in range(n)
        ]
        self.count += n
        self.rows = list(reversed(new)) + self.rows

    def logs(self, index: int, offset: int) -> list[dict]:
        self.add(self.per_request)
        self.offsets.append(offset)
        return self.rows[offset : offset + LOGS_LIMIT]


def walker(log: Log) -> LighterConnector:
    conn = LighterConnector(ADDR)
    conn._logs = log.logs  # type: ignore[method-assign]
    return conn


def walk_to_done(conn: LighterConnector, walk: _Walk, limit: int) -> tuple[list[str], int]:
    applied: list[str] = []
    for page in range(1, limit + 1):
        rows, done = conn._walk_page(1, walk)
        applied.extend(r["hash"] for r in rows)
        if done:
            return applied, page
    raise AssertionError(f"walk not done after {limit} pages: {walk.to_json()}")


def every_row(log: Log) -> list[str]:
    return [f"h{i:06d}" for i in range(log.count)]


@pytest.mark.parametrize("rows", [150, 1000])
@pytest.mark.parametrize("per_request", [0, 9, 10, 12, 15, 30, 60])
def test_forward_walk_finishes_however_many_rows_arrive_per_request(rows, per_request):
    """10 or more rows per request used to make the walk alternate between
    two offsets, taking only the rows that had just arrived. The walk now
    steps by the drift it saw, so it takes about LOGS_STEP rows a request:
    net progress LOGS_STEP - per_request > 0, so it reaches offset 0."""
    log = Log(rows, per_request)
    conn = walker(log)
    applied, pages = walk_to_done(conn, _Walk(), limit=2000)
    assert applied == every_row(log)  # every row once, oldest first
    # rows / (LOGS_STEP - per_request) forward pages, plus the locate pages
    # and the misses while the drift estimate settles.
    assert pages <= log.count // (LOGS_STEP - per_request) + 20


@pytest.mark.parametrize("rows", [5000, 20000])
def test_locate_finishes_while_rows_arrive_mid_bisect(rows):
    """60 rows per request fill the "empty" upper bound of the bisection;
    the first version converged on hi = lo + 1 and asked for offset lo
    forever (stuck at ('locate', 5599, 5599, 5600) for 5,000 rows). A
    stale bound is now dropped and the search gallops on from lo."""
    log = Log(rows, 60)
    conn = walker(log)
    applied, pages = walk_to_done(conn, _Walk(), limit=3000)
    assert applied == every_row(log)
    # Not stuck on one offset: no offset is asked for more than twice in a row.
    offsets = log.offsets
    assert not any(offsets[i] == offsets[i + 1] == offsets[i + 2] for i in range(len(offsets) - 2))


def test_a_stale_upper_bound_is_dropped():
    """The state the first version froze in: lo = 5599 full, hi = 5600
    "empty" (no longer: rows arrived). A full page at lo proves at least
    lo + 100 rows, so hi is stale: drop it and gallop from lo."""
    walk = _Walk(mode="locate", offset=5599, lo=5599, hi=5600, step=800)
    full = [{"hash": f"x{i}", "time": T0.isoformat()} for i in range(LOGS_LIMIT)]
    assert _locate(walk, full) == ([], False)
    assert (walk.lo, walk.hi, walk.offset) == (5599, None, 5599 + LOGS_LIMIT)
    # An empty page bounds the search again; a page of 40 is the oldest.
    _locate(walk, [])
    assert (walk.lo, walk.hi, walk.offset) == (5599, 5699, 5649)
    oldest = full[:40]
    rows, done = _locate(walk, oldest)
    assert [r["hash"] for r in rows] == [f"x{i}" for i in reversed(range(40))]
    assert (walk.mode, walk.anchor, walk.pos, walk.offset) == ("forward", "x0", 5649, 5559)
    assert done is False


def test_head_page_and_galloping_offsets():
    """A quiet log of 1,000 rows: offset 0 (full), then 100, 200, 400, 800
    (full, full, full, full), 1600 (empty), 1200 (empty), 1000 (empty), 900
    (the oldest 100 rows: full), 950 (50 rows: the oldest page)."""
    log = Log(1000)
    walk_to_done(walker(log), _Walk(), limit=100)
    assert log.offsets[:10] == [0, 100, 200, 400, 800, 1600, 1200, 1000, 900, 950]


@pytest.mark.parametrize("burst", [5, 95, 150, 1000])
def test_a_later_pass_seeks_past_a_burst_while_rows_keep_arriving(burst):
    """After a finished pass, `burst` rows arrive, then 12 more per request:
    the next pass starts at offset 0, finds pages newer than its anchor,
    jumps a page at a time until it finds it, then walks forward."""
    log = Log(300)
    conn = walker(log)
    walk = _Walk()
    first, _ = walk_to_done(conn, walk, limit=100)
    log.add(burst)
    log.per_request = 12
    second, _ = walk_to_done(conn, walk, limit=500)
    assert first + second == every_row(log)


def test_a_vanished_anchor_raises_rather_than_guessing():
    """The history changed under the walk (the anchor is gone and the page
    reaches past its time): the sync stops, it does not guess."""
    log = Log(150)
    conn = walker(log)
    walk = _Walk()
    walk_to_done(conn, walk, limit=50)
    log.rows = [r for r in log.rows if r["hash"] != walk.anchor]
    with pytest.raises(LighterError, match="lost row"):
        walk_to_done(conn, walk, limit=5)


def test_a_repeated_hash_in_a_page_stops_the_walk():
    """One row per hash is what the fill ids and the anchor rely on; a page
    that repeats one raises (no numbering by position, which would change
    with page boundaries) [VERIFY multi-row transactions]."""
    log = Log(20)
    log.rows[3] = dict(log.rows[3], hash=log.rows[4]["hash"])
    with pytest.raises(LighterError, match="repeated"):
        walker(log)._walk_page(1, _Walk())


def test_old_cursors_without_the_new_fields_still_load():
    walk = _Walk.from_json({"a": "h", "t": T0.isoformat(), "o": 270, "m": "forward"})
    assert (walk.pos, walk.drift, walk.step) == (270, 0, LOGS_LIMIT)
