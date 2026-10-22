"""money.fixed_to_atoms / fixed_to_decimal_str (docs/onchain_perps_plan.md
S7): exact integer strings at 10**scale (GMX 30-decimal USD, Nado x18) to
atoms. Every expected value is worked by hand in the comment beside it."""

import re
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path

import pytest

from alphamarket.money import fixed_to_atoms, fixed_to_decimal_str

# GMX fixture basePnlUsd (30 dp): 51,033.091798973216164946606156791229 USD.
GMX_PNL = "51033091798973216164946606156791229"
# GMX fixture sizeDeltaInTokens (18-dp index token):
# 690631526897078958132301 / 1e18 = 690,631.526897078958132301 tokens.
GMX_SIZE = "690631526897078958132301"


def test_exact_values_convert_without_rounding():
    # 1.5 at x18 -> 1_500_000 micros (1.5 x 10^6)
    assert fixed_to_atoms("1500000000000000000", 18, 6) == 1_500_000
    # -0.000001 at x18 = -10^12 / 10^18 -> -1 micro
    assert fixed_to_atoms("-1000000000000", 18, 6) == -1
    # a scale below the target scales up: 12.34 at 2 dp -> 12.340000 at 6 dp
    assert fixed_to_atoms("1234", 2, 6) == 12_340_000
    assert fixed_to_atoms("0", 30, 6) == 0
    assert fixed_to_atoms("+42", 0, 12) == 42 * 10**12


def test_inexact_values_raise_by_default():
    with pytest.raises(ValueError, match="finer than 6 decimals"):
        fixed_to_atoms(GMX_PNL, 30, 6)
    with pytest.raises(ValueError):
        fixed_to_atoms("1000000000001", 18, 6)  # 0.000001000000001


def test_gmx_pnl_half_even_at_6dp():
    # 51,033.091798|973216... : the dropped part 0.973... of a micro is over
    # half, so it rounds up: 51,033.091799 = 51_033_091_799 micros.
    assert fixed_to_atoms(GMX_PNL, 30, 6, rounding=ROUND_HALF_EVEN) == 51_033_091_799
    # ROUND_DOWN truncates toward zero: 51,033.091798.
    assert fixed_to_atoms(GMX_PNL, 30, 6, rounding=ROUND_DOWN) == 51_033_091_798
    # Negative values mirror (toward zero / half-even on the magnitude).
    assert fixed_to_atoms("-" + GMX_PNL, 30, 6, rounding=ROUND_HALF_EVEN) == -51_033_091_799
    assert fixed_to_atoms("-" + GMX_PNL, 30, 6, rounding=ROUND_DOWN) == -51_033_091_798


def test_half_way_ties_go_to_even():
    # 0.0000025 at 7 dp ("25") -> 2.5 micros: tie, to the even 2.
    assert fixed_to_atoms("25", 7, 6, rounding=ROUND_HALF_EVEN) == 2
    # 0.0000035 -> 3.5 micros: tie, to the even 4.
    assert fixed_to_atoms("35", 7, 6, rounding=ROUND_HALF_EVEN) == 4
    assert fixed_to_atoms("-35", 7, 6, rounding=ROUND_HALF_EVEN) == -4
    # 2.5000...01 micros is past half: 3.
    assert fixed_to_atoms("2500000000000001", 21, 6, rounding=ROUND_HALF_EVEN) == 3


def test_gmx_quantity_truncates_at_12dp():
    # 690,631.526897078958|132301 tokens: 12 dp keep ...078958, ROUND_DOWN
    # drops 132301 -> 690631526897078958 atoms at 12 dp.
    assert fixed_to_atoms(GMX_SIZE, 18, 12, rounding=ROUND_DOWN) == 690_631_526_897_078_958
    with pytest.raises(ValueError):
        fixed_to_atoms(GMX_SIZE, 18, 12)


def test_bad_inputs_are_refused():
    with pytest.raises(TypeError):
        fixed_to_atoms(1500, 3, 6)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        fixed_to_atoms(1.5, 0, 6)  # type: ignore[arg-type]
    for bad in ("1.5", "", "-", "1e18", " 12 3", "١٢"):
        with pytest.raises(ValueError):
            fixed_to_atoms(bad, 18, 6)
    with pytest.raises(ValueError):
        fixed_to_atoms("1", 18, 13)  # beyond MAX_DECIMALS
    with pytest.raises(ValueError):
        fixed_to_atoms("1", 18, 6, rounding=ROUND_HALF_UP)  # only half-even or down


def test_fixed_to_decimal_str_is_exact():
    assert fixed_to_decimal_str(GMX_PNL, 30) == "51033.091798973216164946606156791229"
    assert fixed_to_decimal_str("1500000000000000000", 18) == "1.5"
    assert fixed_to_decimal_str("-5", 2) == "-0.05"
    assert fixed_to_decimal_str("7000", 3) == "7"
    assert fixed_to_decimal_str("12", 0) == "12"
    assert fixed_to_decimal_str("0", 30) == "0"


def test_only_the_gmx_connector_passes_rounding():
    """S7: quantizing is a published per-venue rule (GMX's 30-dp USD legs and
    18-dp quantities). No other module may pass `rounding=` to fixed_to_atoms."""
    src = Path(__file__).resolve().parent.parent / "src" / "alphamarket"
    allowed = {src / "connectors" / "gmx.py"}
    call = re.compile(r"fixed_to_atoms\((?:[^()]|\([^()]*\))*rounding\s*=", re.S)
    offenders = [
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if p not in allowed and p.name != "money.py" and call.search(p.read_text())
    ]
    assert offenders == []
