"""Pacifica through the shared pipeline: its profile and registry entries,
terms marker, demo and seed rows, the watch path (privacy flag, anchor), the
discover refresh (privacy rows never reach a snapshot), the opt-out by ed25519
signature, and the nightly reconcile of a fixture account through the real
ledger. Network is respx only (invariant 6)."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy import select

from alphamarket import discover, ingest, reconcile, watch_pages, watchlist
from alphamarket.config import get_settings
from alphamarket.connectors import pacifica, registry
from alphamarket.connectors.addresses import AddressKind, b58encode, is_valid
from alphamarket.connectors.base import (
    CredentialKind,
    OwnershipProof,
    PaperFeeModel,
    ReadOnlyProof,
    StreamMode,
)
from alphamarket.demo import synthetic_solana_address
from alphamarket.models import (
    AnchorBalance,
    AssetFill,
    AssetReconciliation,
    LeaderboardSnapshot,
    LedgerEntry,
    LedgerQuarantine,
    Strategy,
    StrategyKind,
    VenueAccount,
    Watch,
    WatchOptout,
)
from test_onchain_scaffold import _user, _watched_account, solana_wallet
from test_pacifica_connector import fx, mock
from test_watchlist import signup

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
M = 1_000_000
BOARD = fx("leaderboard")["data"]
PRIVATE_ADDRESS = BOARD[0]["address"]  # privacy_mode true
PUBLIC_ADDRESS = BOARD[2]["address"]  # privacy_mode false


def _preflight():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pacifica_open(monkeypatch):
    """Pacifica open for watching and discover (prod keeps it out until counsel)."""
    settings = get_settings()
    venues = ("hyperliquid", "polymarket", "pacifica")
    monkeypatch.setattr(watchlist, "WATCHABLE_VENUES", venues)
    monkeypatch.setattr(watch_pages, "WATCHABLE_VENUES", venues)
    monkeypatch.setattr(settings, "watch_venues", "pacifica")
    monkeypatch.setattr(settings, "watch_discover_enabled", True)
    monkeypatch.setattr(settings, "watch_discover_min_account_value", 10_000)


# ---- the profile and the registry blocks ----


def test_profile_is_a_watch_only_solana_address_venue():
    p = registry.get_profile("pacifica")
    assert (p.display_name, p.envs, p.quote_asset) == ("Pacifica", ("prod",), "USDC")
    assert p.credential is CredentialKind.ADDRESS and p.secret_label is None
    assert p.address_kind is AddressKind.SOLANA
    assert p.ownership is OwnershipProof.SIGNED_MESSAGE and p.readonly_proof is ReadOnlyProof.NONE
    assert p.requires_partner_approval is True  # no wallet connect: the watch path only
    assert p.balance_observable and p.transfers_observable and p.history_depth == "all"
    # level-0 taker 0.0004 = 4 bps exactly (docs "Get Fee Levels", /info/fees 2026-10-22)
    levels = fx("info_fees")["data"]
    assert levels[0]["taker_fee_rate"] == "0.0004" and p.fee_rate_bps == 4
    assert p.paper_fee_model is PaperFeeModel.BPS_OF_NOTIONAL and p.asset_classes == ("crypto",)
    assert p.leaderboard_periods == ("day", "week", "month", "all")
    assert (p.stream_mode, p.stream_users_per_ip) == (StreamMode.FILLS, 90)
    assert (p.tick_micros, p.contract_step_micros, p.fee_rounding_micros) == (1, 1, 1)
    assert p.tos_doc == "docs/venues/pacifica.md" and (ROOT / p.tos_doc).exists()
    assert p.rate_budgets == (pacifica.RATE_BUDGET,)
    assert "Solana address" in p.key_help and "Sign Message" in p.key_help


def test_registry_builds_the_connector_and_market_data():
    conn = registry.build_connector("pacifica", "prod", PUBLIC_ADDRESS)
    assert isinstance(conn, pacifica.PacificaConnector) and conn.address == PUBLIC_ADDRESS
    with pytest.raises(ValueError):
        registry.build_connector("pacifica", "prod", "0x" + "ab" * 20)  # not a Solana address
    with pytest.raises(ValueError, match="no 'testnet' environment"):
        registry.build_connector("pacifica", "testnet", PUBLIC_ADDRESS)
    md = registry.build_market_data("pacifica", "prod")
    assert isinstance(md, pacifica.PacificaMarketData)
    s = get_settings()
    assert md.leaderboard_keep == 2 * s.watch_discover_rows
    assert md.leaderboard_min_account_value == str(s.watch_discover_min_account_value)
    # USDC needs no entry of its own in CASH_AT_PAR
    assert "USDC" in registry.CASH_AT_PAR


def test_terms_doc_keeps_pacifica_out_of_prod_until_counsel_answers():
    preflight = _preflight()
    doc = (ROOT / "docs/venues/pacifica.md").read_text()
    section = preflight.tos_position(doc)
    assert section is not None and "Position: PENDING COUNSEL" in section
    # The terms were found and read: they are not UNREAD, and they do not ban the API.
    assert "Position: UNREAD" not in section
    assert "https://app.pacifica.fi/term" in section and "June 11, 2026" in section
    [problem] = preflight.watch_venue_problems(SimpleNamespace(watch_venues="pacifica"))
    assert "pacifica" in problem and "Position: PENDING COUNSEL" in problem


# ---- demo and seed rows ----


def test_demo_discover_sample_has_solana_pacifica_rows():
    from alphamarket import demo

    assert "pacifica" in demo._DISCOVER_VENUES
    view = demo.discover_sample("pacifica", "week", "pnl")
    assert view.venue == "pacifica" and len(view.rows) == demo._DISCOVER_ROWS
    assert all(is_valid(AddressKind.SOLANA, r.address) for r in view.rows)
    assert all(r.address.startswith("De3o") for r in view.rows)  # obviously invented


def test_seed_rows_are_synthetic_solana_addresses():
    spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "scripts" / "seed_demo.py")
    seed_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_demo)
    assert synthetic_solana_address("pacifica-watch") == seed_demo.PACIFICA_WATCH_ADDRESS
    assert is_valid(AddressKind.SOLANA, seed_demo.PACIFICA_WATCH_ADDRESS)
    assert seed_demo.PACIFICA_WATCH_ADDRESS.startswith("De3o")
    [creator] = [c for c in seed_demo.CREATORS if c.venue == "pacifica"]
    assert creator.watch and creator.key_id == seed_demo.PACIFICA_WATCH_ADDRESS
    assert ("investor", creator.handle, "Solana perp book to study") in seed_demo.WATCHES
    board = seed_demo.LEADERBOARD_SEEDS["pacifica"]
    addresses = [board(i) for i in range(24)]
    assert addresses[2] == seed_demo.PACIFICA_WATCH_ADDRESS  # rank 3 is the watched one
    assert len(set(addresses)) == 24 and all(is_valid(AddressKind.SOLANA, a) for a in addresses)


# ---- the watch path ----


@respx.mock
def test_watching_a_public_address_anchors_it_and_a_private_one_is_refused(
    session_factory, pacifica_open
):
    fake = mock(
        account=fx("account_small"),
        positions=fx("positions_small"),
        trades__history=fx("trades_history_small"),  # take_anchor checks for a fill nearby
        leaderboard=fx("leaderboard"),
    )
    with session_factory() as db:
        user = _user(db, "u@example.com", email_verified_at=NOW)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            watchlist.add_watch(
                db, user, watchlist.WatchIn(venue="pacifica", address=PRIVATE_ADDRESS)
            )
        assert exc.value.status_code == 422
        assert exc.value.detail == "The owner of this address has turned on privacy on Pacifica."
        assert db.scalars(select(VenueAccount)).all() == []  # nothing stored
        assert db.scalars(select(WatchOptout)).all() == []  # the flag can be turned off again

        watched = watchlist.add_watch(
            db, user, watchlist.WatchIn(venue="pacifica", address=f"  {PUBLIC_ADDRESS}  ")
        )
        account = watched.account
        assert account.key_id == PUBLIC_ADDRESS  # case intact, whitespace gone
        assert account.is_watch and account.claimed_at is None
        assert account.anchor_balance_micros == 4_999_073_822  # the USDC cash
        [anchor] = db.scalars(select(AnchorBalance)).all()  # flat: only the cash
        assert (anchor.asset, anchor.atoms, anchor.decimals) == ("USDC", 4_999_073_822, 6)
        assert db.scalar(select(Strategy)).kind == StrategyKind.WATCH
    # every request was a body-less GET to the API
    assert all(r.method == "GET" and r.content == b"" for r in fake.requests)


def test_a_non_solana_or_unknown_address_is_the_kind_hint(pacifica_open):
    for bad in ("0x" + "ab" * 20, "short", "O" * 44):
        with pytest.raises(HTTPException) as exc:
            watchlist.normalize_address("pacifica", bad)
        assert exc.value.detail == "Enter a Solana address (32-44 characters, case-sensitive)"


@respx.mock
def test_a_missing_account_cannot_be_watched(session_factory, pacifica_open):
    mock(
        account=httpx.Response(404, json=fx("account_not_found")),
        positions=fx("positions_small"),
        leaderboard=fx("leaderboard"),
    )
    with session_factory() as db:
        user = _user(db, "u@example.com", email_verified_at=NOW)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            watchlist.add_watch(
                db,
                user,
                watchlist.WatchIn(venue="pacifica", address=synthetic_solana_address("nobody")),
            )
        assert exc.value.status_code in (400, 422, 502)
        assert db.scalars(select(VenueAccount)).all() == []


# ---- discover: privacy rows never reach a snapshot ----


@respx.mock
def test_refresh_stores_the_board_without_privacy_rows_and_sweeps_their_watches(
    session_factory, pacifica_open
):
    mock(leaderboard=fx("leaderboard"))
    with session_factory() as db:
        system = _user(db, "system@koine.invalid", is_system=True)
        user = _user(db, "u@example.com")
        _watched_account(db, system, user, "pacifica", PRIVATE_ADDRESS)
        _watched_account(db, system, user, "pacifica", PUBLIC_ADDRESS)
        db.commit()
        ctx = SimpleNamespace(db=db, limiter=None, clock=lambda: NOW, settings=get_settings())
        counts = discover.refresh(ctx)
        # the $10,000 floor leaves 8 rows in ALL (ranks 1, 4, 6, 7, 8, 9, 11, 12) and 6 in DAY
        assert counts[("pacifica", "all")] == 8 and counts[("pacifica", "day")] == 6
        assert counts[("pacifica", "private_removed")] == 1  # the flagged address's watch
        assert ("pacifica", "invalid_addresses") not in counts
        snaps = db.scalars(
            select(LeaderboardSnapshot).where(LeaderboardSnapshot.venue == "pacifica")
        ).all()
        assert {s.period for s in snaps} == {"day", "week", "month", "all"}
        private = {r["address"] for r in BOARD if r["privacy_mode"]}
        for snap in snaps:
            stored = {r["address"] for r in snap.rows}
            assert not stored & private  # a privacy row never reaches a snapshot
            assert all(is_valid(AddressKind.SOLANA, a) for a in stored)  # case kept, never lowered
            assert all(r["roi"] is None for r in snap.rows)  # the board has no roi
        top = max((s for s in snaps if s.period == "all"), key=lambda s: len(s.rows)).rows[0]
        assert (top["address"], top["rank"]) == (PUBLIC_ADDRESS, 1)
        assert top["pnl_micros"] == 2_544_489_280_320  # 2544489.28032 at 6 dp
        # the flagged address's watch is gone without an opt-out row; the other stays
        left = (
            db.execute(
                select(VenueAccount.key_id)
                .join(Strategy)
                .join(Watch, Watch.strategy_id == Strategy.id)
            )
            .scalars()
            .all()
        )
        assert left == [PUBLIC_ADDRESS]
        assert db.scalars(select(WatchOptout)).all() == []


# ---- opt-out by ed25519 signature ----


def test_the_owner_opts_out_with_an_ed25519_signature(client, session_factory, pacifica_open):
    key, address = solana_wallet()
    with session_factory() as db:
        system = _user(db, "system@koine.invalid", is_system=True)
        watcher = _user(db, "w@example.com")
        account = _watched_account(db, system, watcher, "pacifica", address)
        db.commit()
        account_id = account.id
    signup(client, "owner@example.com")
    proof = client.post("/watch/optout", data={"venue": "pacifica", "address": address})
    assert proof.status_code == 200, proof.text
    assert 'data-claim-kind="solana"' in proof.text and "Phantom" in proof.text
    message = proof.text.split('data-claim-message="', 1)[1].split('"', 1)[0]
    message = message.replace("&#10;", "\n").replace("&#34;", '"')
    assert message.splitlines()[1] == "Venue: Pacifica"
    assert message.splitlines()[2] == f"Address: {address}"

    other_key, _ = solana_wallet()
    wrong = client.post(
        "/watch/optout/verify",
        data={
            "venue": "pacifica",
            "address": address,
            "signature": b58encode(other_key.sign(message.encode())),
        },
    )
    assert wrong.status_code == 422 and "does not match" in wrong.text
    with session_factory() as db:
        assert db.scalars(select(WatchOptout)).all() == []

    done = client.post(
        "/watch/optout/verify",
        data={
            "venue": "pacifica",
            "address": address,
            "signature": b58encode(key.sign(message.encode("utf-8"))),
        },
    )
    assert done.status_code == 200, done.text
    assert "Opted out." in done.text
    with session_factory() as db:
        [optout] = db.scalars(select(WatchOptout)).all()
        assert (optout.venue, optout.address) == ("pacifica", address)
        assert db.scalars(select(Watch)).all() == []
        assert db.get(VenueAccount, account_id) is not None  # imported rows stay (append-only)


# ---- the fixture account through the real ledger and reconcile ----


def _account(db) -> VenueAccount:
    system = _user(db, "system@koine.invalid", is_system=True)
    address = synthetic_solana_address("pacifica-small")
    acct = VenueAccount(
        user_id=system.id,
        venue="pacifica",
        venue_env="prod",
        key_id=address,
        is_watch=True,
        anchor_balance_micros=0,
        anchor_at=datetime(2025, 9, 1, tzinfo=UTC),
    )
    db.add(acct)
    db.flush()
    db.add(
        Strategy(
            user_id=system.id,
            venue_account_id=acct.id,
            name="Pacifica small",
            slug="watch-small",
            kind=StrategyKind.WATCH,
            public=False,
        )
    )
    db.add(
        AnchorBalance(
            venue_account_id=acct.id,
            asset="USDC",
            atoms=0,
            decimals=6,
            as_of=datetime(2025, 9, 1, tzinfo=UTC),
        )
    )
    db.commit()
    return acct


@respx.mock
def test_the_small_account_syncs_and_reconciles_through_the_real_ledger(session_factory):
    fake = mock(
        trades__history=fx("trades_history_small"),
        funding__history=fx("funding_history_small"),
        account__balance__history=fx("balance_history_small"),
        account=fx("account_small"),
        positions=fx("positions_small"),
    )
    with session_factory() as db:
        acct = _account(db)
        snapshot = reconcile.reconcile_asset_account(db, acct)
        # Replayed cash 4999.036776 (5000 - 0.176423 - 0.303393 - 0.014988 - 0.46842) against
        # the venue's 4999.073822: 37,046 atoms, the loan interest the default balance
        # history does not list (docs/venues/pacifica.md "Known gaps"), inside 0.1%.
        assert snapshot.reconciled is True
        assert snapshot.computed_cash_micros == 4_999_036_776
        assert snapshot.venue_balance_micros == 4_999_073_822
        assert snapshot.divergence_micros == 37_046 and snapshot.tolerance_micros == 4_999_073
        assert snapshot.equity_reconciled is True  # account_equity against the marked ledger
        rows = {r.asset: r for r in db.scalars(select(AssetReconciliation))}
        assert rows["USDC"].reconciled and rows["SOL-PERP"].computed_atoms == 0  # flat, as venue
        assert db.scalars(select(LedgerQuarantine)).all() == []
        fills = db.scalars(select(AssetFill).order_by(AssetFill.filled_at, AssetFill.id)).all()
        assert [f.venue_fill_id for f in fills] == ["10407603", "10407605", "10419061"]
        usdc = sum(e.delta_atoms for e in db.scalars(select(LedgerEntry)) if e.asset == "USDC")
        assert usdc == 4_999_036_776

        # A second sync re-reads the overlap: the same rows come back and the ledger dedupes
        # them (an exact duplicate is not a quarantine).
        before = db.scalar(select(LedgerEntry.id).order_by(LedgerEntry.id.desc()).limit(1))
        ingest.sync_account(db, acct)
        db.commit()
        assert db.scalar(select(LedgerEntry.id).order_by(LedgerEntry.id.desc()).limit(1)) == before
        assert db.scalars(select(LedgerQuarantine)).all() == []
    assert all(r.method == "GET" and r.content == b"" for r in fake.requests)


@respx.mock
def test_a_fill_the_ledger_cannot_book_is_flagged_not_dropped(session_factory):
    rows = fx("trades_history_small")
    rows["data"][0] = {**rows["data"][0], "cause": "game_settlement"}  # the close
    mock(
        trades__history=rows,
        funding__history=fx("funding_history_small"),
        account__balance__history=fx("balance_history_small"),
        account=fx("account_small"),
        positions=fx("positions_small"),
    )
    with session_factory() as db:
        acct = _account(db)
        snapshot = reconcile.reconcile_asset_account(db, acct)
        # Without the close the ledger is still long 5.52 SOL while the venue is flat:
        # reconcile quarantines it, and the flag says why.
        assert snapshot.reconciled is False
        from alphamarket import activity_flags

        [flag] = activity_flags.flags_for(db, acct.id)
        assert (flag.venue_ref, flag.activity_type, flag.symbol) == (
            "fill:10419061",
            "cause:game_settlement",
            "SOL",
        )
