"""Invariant #1: exact integer micros, never floats.

Hand-computed expected values, per the project convention that every ledger
formula gets a test with numbers worked out by hand.
"""

from decimal import Decimal

import pytest

from alphamarket.money import (
    MICROS_PER_UNIT,
    format_micros,
    from_micros,
    notional_micros,
    to_micros,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.9800", 980_000),  # yes_price_dollars from a real fill
        ("0.139800", 139_800),  # fee_cost: 13.98 cents, NOT 14
        ("101.89", 101_890_000),  # count_fp: fractional contracts
        ("1101.8980", 1_101_898_000),  # balance_dollars
        ("0", 0),
        ("0.000001", 1),  # one micro, the finest exact value
        ("-2.5", -2_500_000),  # sells/debits are negative
    ],
)
def test_to_micros_exact(text, expected):
    assert to_micros(text) == expected


def test_to_micros_accepts_decimal():
    assert to_micros(Decimal("0.139800")) == 139_800


@pytest.mark.parametrize("bad", [0.98, 1, True, 0])
def test_to_micros_rejects_floats_and_ints(bad):
    """Floats can't hold decimals exactly; bare ints are ambiguous about scale."""
    with pytest.raises(TypeError):
        to_micros(bad)


def test_to_micros_raises_rather_than_rounding_away_precision():
    """If the venue ever exceeds 1e-6, we want a loud failure, not silent loss."""
    with pytest.raises(ValueError, match="finer than 1e-6"):
        to_micros("0.0000001")


def test_round_trip():
    assert from_micros(to_micros("0.139800")) == Decimal("0.1398")
    assert format_micros(1_101_898_000) == "1101.8980"
    assert format_micros(139_800, places=6) == "0.139800"


def test_notional_matches_the_real_fill_by_hand():
    """101.89 contracts x $0.9800 = $99.852200 exactly."""
    count = to_micros("101.89")
    price = to_micros("0.9800")
    assert notional_micros(price, count) == 99_852_200
    assert from_micros(notional_micros(price, count)) == Decimal("99.8522")


def test_the_real_demo_fill_reconciles_to_the_penny():
    """The whole reason for micros: this ledger closes exactly, and would not
    close if fees were rounded to whole cents.

    starting 1201.890000 - (99.852200 + 0.139800) = 1101.898000 = balance
    """
    debit = notional_micros(to_micros("0.9800"), to_micros("101.89")) + to_micros("0.139800")
    assert debit == 99_992_000
    assert to_micros("1201.890000") - debit == to_micros("1101.8980")


def test_cents_would_not_reconcile():
    """Documents the bug we are avoiding, so nobody 'simplifies' back to cents."""
    fee_in_whole_cents = round(Decimal("0.139800") * 100)  # 14
    assert fee_in_whole_cents == 14
    assert Decimal(fee_in_whole_cents) / 100 != Decimal("0.139800")


def test_scale_constant():
    assert MICROS_PER_UNIT == 1_000_000
