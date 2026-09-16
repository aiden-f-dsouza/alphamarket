"""Identity marks and chart shading are deterministic functions of the data."""

from alphamarket.charts import drawdown_window, window_x
from alphamarket.marks import sigil, tag_color


def test_drawdown_window_finds_worst_peak_to_trough():
    # peak 120 at i=2, trough 90 at i=4 (25%); the later 130->122 dip (6%) is smaller
    assert drawdown_window([100, 110, 120, 100, 90, 130, 125, 122]) == (2, 4)
    assert drawdown_window([100, 110, 120]) == (0, 0)  # never falls
    assert drawdown_window([]) == (0, 0)
    assert window_x([100, 110, 120, 100, 90, 130, 125, 122], 140) == (40.0, 80.0)  # 140 * 2/7, 4/7
    assert window_x([100], 120) == (0.0, 0.0)


def test_sigil_is_stable_and_distinct():
    a, b = sigil("weather-cpi-mean-reversion"), sigil("election-polling-arbitrage")
    assert a == sigil("weather-cpi-mean-reversion")
    assert a != b
    assert a.startswith('<svg class="sigil"') and "<rect" in a
    assert 'aria-label="Mark for weather-cpi-mean-reversion"' in a
    assert "&lt;" in sigil("<x>")  # seed is escaped


def test_tag_color_known_families_and_fallback():
    assert tag_color("Weather") == tag_color("weather") == "#2f7fb0"
    assert tag_color("politics") != tag_color("sports")
    assert tag_color("obscure-thing") == tag_color("obscure-thing")
    assert tag_color("obscure-thing").startswith("#")
