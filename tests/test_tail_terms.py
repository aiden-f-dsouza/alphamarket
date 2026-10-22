"""default_drawdown_bps / drawdown_note: hand-computed against DRAWDOWN_CHOICES_BPS
(500, 1000, 1500, 2000, 2500, 3000, 4000, 5000)."""

import pytest

from alphamarket.tail_terms import default_drawdown_bps, drawdown_note


@pytest.mark.parametrize(
    ("worst", "expected"),
    [
        (None, 1500),
        (0.0, 500),
        (0.04, 500),
        (0.05, 1000),  # strictly greater: exactly on a choice goes up one
        (0.10, 1500),
        (0.15, 2000),
        (0.183, 2000),
        (0.30, 4000),
        (0.49, 5000),
        (0.62, 5000),  # worse than every choice: the loosest
    ],
)
def test_default_drawdown_bps(worst, expected):
    assert default_drawdown_bps(worst) == expected


def test_drawdown_note_only_when_limit_is_not_looser_than_worst():
    assert drawdown_note(None, 500) is None
    assert drawdown_note(0.183, 2000) is None
    assert "18.3%" in drawdown_note(0.183, 1500)
    assert drawdown_note(0.15, 1500) is not None  # equal counts as tight
