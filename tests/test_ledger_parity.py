"""M8 exit criterion: the cash curve computed from LEGS equals the M2 curve
to the micro, for every recorded Kalshi and Polymarket fixture.

Three paths are compared:
  legacy  ledger.legacy_build_cash_curve over the normalized rows (M2 formula)
  legs    ledger.build_cash_curve (legs built in memory)
  stored  ingest.sync_account writes fills, settlements AND their legs to the
          database; the curve is rebuilt from ledger_entries alone.
No live calls: connectors read the fixtures through respx (invariant 6)."""

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from alphamarket import ingest
from alphamarket.connectors.base import (
    Action,
    NormalizedBalance,
    NormalizedFill,
    NormalizedSettlement,
    Side,
)
from alphamarket.connectors.kalshi import KalshiConnector
from alphamarket.connectors.polymarket import PolymarketConnector
from alphamarket.ledger import (
    balance_vector,
    build_cash_curve,
    cash_curve_from_groups,
    legacy_build_cash_curve,
    legs_for_settlement,
    nonzero_legs,
)
from alphamarket.models import Fill, LedgerEntry, Settlement, Strategy, User, VenueAccount
from alphamarket.vault import encrypt_secret

FIXTURES = Path(__file__).parent / "fixtures"
KALSHI = "https://demo-api.kalshi.co/trade-api/v2"
PM_DATA = "https://data-api.polymarket.com"
PM_CLOB = "https://clob.polymarket.com"
PM_GAMMA = "https://gamma-api.polymarket.com"
PM_ADDRESS = "0x124628e686db22da39b6ee43ca786c8a074bd6f2"


def _load(pattern: str) -> list:
    """Every fixture file matching `pattern`, rows concatenated."""
    rows: list = []
    for path in sorted(FIXTURES.glob(pattern)):
        payload = json.loads(path.read_text())
        rows.extend(payload)
    return rows


def _kalshi_rows():
    fills = [r for p in sorted(FIXTURES.glob("kalshi_fills*.json")) for r in _json(p)["fills"]]
    stls = [
        r
        for p in sorted(FIXTURES.glob("kalshi_settlements*.json"))
        for r in _json(p)["settlements"]
    ]
    return fills, stls


def _json(path: Path):
    return json.loads(path.read_text())


@respx.mock
def _kalshi_normalized() -> tuple[list[NormalizedFill], list[NormalizedSettlement]]:
    fills, stls = _kalshi_rows()
    respx.get(f"{KALSHI}/portfolio/fills").mock(
        return_value=httpx.Response(200, json={"fills": fills, "cursor": ""})
    )
    respx.get(f"{KALSHI}/portfolio/settlements").mock(
        return_value=httpx.Response(200, json={"settlements": stls, "cursor": ""})
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    conn = KalshiConnector(base_url=KALSHI, key_id="k", private_key=key)
    return conn.fetch_fills(), conn.fetch_settlements()


@respx.mock
def _polymarket_normalized() -> tuple[list[NormalizedFill], list[NormalizedSettlement]]:
    trades = _load("polymarket_activity_trades*.json")
    redeems = _load("polymarket_activity_redeems*.json")
    respx.get(f"{PM_DATA}/activity", params__contains={"type": "TRADE"}).mock(
        return_value=httpx.Response(200, json=trades)
    )
    respx.get(f"{PM_DATA}/activity", params__contains={"type": "REDEEM"}).mock(
        return_value=httpx.Response(200, json=redeems)
    )
    market = _json(FIXTURES / "polymarket_market.json")
    respx.get(url__startswith=f"{PM_CLOB}/markets/").mock(
        return_value=httpx.Response(200, json=market)
    )
    conn = PolymarketConnector(PM_ADDRESS, PM_DATA, PM_CLOB, PM_GAMMA)
    return conn.fetch_fills(), conn.fetch_settlements()


class ScriptedConnector:
    venue = "scripted"

    def __init__(self, fills, settlements):
        self.fills, self.settlements = fills, settlements

    def check_connection(self):
        return NormalizedBalance(balance_micros=0, as_of=datetime.now(UTC))

    def check_write_scope(self):
        return False

    def fetch_fills(self, since=None):
        return list(self.fills)

    def fetch_settlements(self, since=None):
        return list(self.settlements)

    def fetch_positions(self):
        return []

    def fetch_balance(self):
        return self.check_connection()


def _anchors(fills, settlements) -> list[datetime]:
    """Before everything, and in the middle (exercises the anchor cut)."""
    times = sorted([f.filled_at for f in fills] + [s.settled_at for s in settlements])
    return [times[0] - timedelta(seconds=1), times[(len(times) - 1) // 2]]


def _stored_path(session_factory, monkeypatch, venue, key_id, fills, settlements):
    """Ingest through sync_account, then rebuild every curve input from the DB."""
    monkeypatch.setattr(
        ingest, "build_connector", lambda *a, **k: ScriptedConnector(fills, settlements)
    )
    db = session_factory()
    user = User(email=f"{venue}@example.com", password_hash="x", display_name="P")
    db.add(user)
    db.flush()
    account = VenueAccount(
        user_id=user.id,
        venue=venue,
        venue_env="demo" if venue == "kalshi" else "prod",
        key_id=key_id,
        encrypted_private_key=encrypt_secret("pem") if venue == "kalshi" else None,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=account.id, name="P", slug=venue)
    db.add(strategy)
    db.commit()
    ingest.sync_account(db, account)
    # A second sync must add no legs (idempotent).
    n_legs = len(db.scalars(select(LedgerEntry)).all())
    ingest.sync_account(db, account)
    assert len(db.scalars(select(LedgerEntry)).all()) == n_legs
    return db, account, strategy


def _assert_parity(session_factory, monkeypatch, venue, key_id, fills, settlements):
    assert fills and settlements, "fixture missing"
    anchor = 1_000_000_000
    db, account, strategy = _stored_path(
        session_factory, monkeypatch, venue, key_id, fills, settlements
    )
    stored_fills = db.scalars(select(Fill).order_by(Fill.id)).all()
    stored_stls = db.scalars(select(Settlement).order_by(Settlement.id)).all()
    groups = ingest.load_leg_groups(db, account.id)
    # A settlement that moves nothing (no contracts, no cash: Kalshi lists
    # markets closed out before settling) writes no legs, so the stored curve
    # has no point for it. Every cash value is still identical; the legacy
    # reference simply omits those no-op rows.
    moving = [s for s in settlements if nonzero_legs(legs_for_settlement(s, venue))]
    for anchor_at in _anchors(fills, settlements):
        legacy = legacy_build_cash_curve(anchor, anchor_at, fills, settlements)
        assert legacy, "anchor excluded everything"
        assert build_cash_curve(anchor, anchor_at, fills, settlements) == legacy
        assert legacy_build_cash_curve(anchor, anchor_at, stored_fills, stored_stls) == legacy
        expected = legacy_build_cash_curve(anchor, anchor_at, fills, moving)
        assert cash_curve_from_groups(anchor, anchor_at, groups) == expected
        assert expected[-1].cash_after_micros == legacy[-1].cash_after_micros
    # The stored USD legs sum to the total cash flow of every row.
    usd = balance_vector(db.scalars(select(LedgerEntry).where(LedgerEntry.asset == "USD")).all())[
        "USD"
    ]
    full = legacy_build_cash_curve(0, _anchors(fills, settlements)[0], fills, settlements)
    assert usd.atoms == full[-1].cash_after_micros
    db.close()


def test_kalshi_fixtures_parity(session_factory, monkeypatch):
    fills, settlements = _kalshi_normalized()
    _assert_parity(session_factory, monkeypatch, "kalshi", "k", fills, settlements)


def test_polymarket_fixtures_parity(session_factory, monkeypatch):
    fills, settlements = _polymarket_normalized()
    _assert_parity(session_factory, monkeypatch, "polymarket", PM_ADDRESS, fills, settlements)


def test_kalshi_demo_account_reconciles_through_stored_legs(session_factory, monkeypatch):
    """The live demo numbers (test_ledger): 1101.8980 before a 0.84 NO @ 0.57
    buy (fee 0.0145) that lost -> 1101.4047, now from ledger_entries."""
    t0 = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
    fills = [
        NormalizedFill(
            venue_fill_id="demo-fill",
            ticker="KXETH15M-26AUG312315-15",
            side=Side.NO,
            action=Action.BUY,
            count_micros=840_000,
            price_micros=570_000,
            fee_micros=14_500,
            filled_at=t0 + timedelta(minutes=1),
        )
    ]
    settlements = [
        NormalizedSettlement(
            venue_settlement_id="demo-stl",
            ticker="KXETH15M-26AUG312315-15",
            market_result="yes",
            no_count_micros=840_000,
            revenue_micros=0,
            fee_micros=14_500,
            settled_at=t0 + timedelta(minutes=15),
        )
    ]
    db, account, _ = _stored_path(session_factory, monkeypatch, "kalshi", "k", fills, settlements)
    curve = cash_curve_from_groups(1_101_898_000, t0, ingest.load_leg_groups(db, account.id))
    assert curve[-1].cash_after_micros == 1_101_404_700
    # Netted contract position: -0.84 then +0.84 -> 0.
    vector = balance_vector(db.scalars(select(LedgerEntry)).all())
    assert vector["KXETH15M-26AUG312315-15"].atoms == 0


@pytest.mark.parametrize("venue", ["kalshi", "polymarket"])
def test_generated_history_parity(session_factory, monkeypatch, venue):
    """A deterministic 200-event history with fractional counts, sub-cent
    fees, reported notionals and shared timestamps."""
    rng = random.Random(8)
    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    tickers = [f"MKT-{i}" for i in range(6)]
    fills, settlements = [], []
    for i in range(160):
        at = t0 + timedelta(minutes=rng.randrange(0, 600))
        count = rng.randrange(1, 50_000) * 100  # 0.0001-contract steps
        price = rng.randrange(1, 99) * 10_000
        notional = None
        if venue == "polymarket" and rng.random() < 0.5:
            notional = count * price // 1_000_000
        elif (count * price) % 1_000_000:
            count = count // 10_000 * 10_000 or 10_000
        fills.append(
            NormalizedFill(
                venue_fill_id=f"g{i}",
                ticker=rng.choice(tickers),
                side=rng.choice([Side.YES, Side.NO]),
                action=rng.choice([Action.BUY, Action.SELL]),
                count_micros=count,
                price_micros=price,
                fee_micros=rng.randrange(0, 20_000),
                filled_at=at,
                notional_micros=notional,
            )
        )
    for i in range(40):
        yes = rng.randrange(0, 10) * 1_000_000
        settlements.append(
            NormalizedSettlement(
                venue_settlement_id=f"gs{i}",
                ticker=rng.choice(tickers),
                market_result="yes",
                yes_count_micros=yes,
                no_count_micros=rng.randrange(0, 3) * 1_000_000,
                revenue_micros=yes,
                settled_at=t0 + timedelta(minutes=rng.randrange(0, 600)),
            )
        )
    key = PM_ADDRESS if venue == "polymarket" else "k"
    _assert_parity(session_factory, monkeypatch, venue, key, fills, settlements)
