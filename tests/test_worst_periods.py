"""stats.worst_periods: dated drawdown episodes for strategy pages."""

from datetime import date, timedelta

from alphamarket.stats import worst_periods


def test_worst_periods_are_runs_below_the_peak_deepest_first():
    d = [date(2026, 3, 1) + timedelta(days=i) for i in range(10)]
    # Equity 100, 90, 80, 100 | 110, 99, 110 | 110, 104.5, 99: episodes
    # (d0 -> d2, 20%, back on d3), (d4 -> d5, 10%, back on d6) and the open
    # one (d7 -> d9, 10%, not recovered). Ties keep the earlier episode.
    dd = [0.0, 0.1, 0.2, 0.0, 0.0, 0.1, 0.0, 0.0, 0.05, 0.1]
    assert worst_periods(list(zip(d, dd, strict=True)), n=3) == [
        (d[0], d[2], 0.2, d[3]),
        (d[4], d[5], 0.1, d[6]),
        (d[7], d[9], 0.1, None),
    ]
    assert worst_periods(list(zip(d, dd, strict=True)))[1] == (d[4], d[5], 0.1, d[6])
    assert worst_periods([(d[0], 0.0), (d[1], 0.0)]) == []
