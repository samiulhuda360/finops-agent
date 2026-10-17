import datetime as dt

import numpy as np

from finops_agent.detect.methods import Params, robust_z, seasonal

DATES = [dt.date(2025, 10, 1) + dt.timedelta(days=i) for i in range(200)]
P = Params(z=4.0, min_excess=10.0)


def weekday_series(seed: int = 1) -> np.ndarray:
    """A dev workload: about $100 on weekdays, $10 at weekends."""
    rng = np.random.default_rng(seed)
    base = np.array([100.0 if d.weekday() < 5 else 10.0 for d in DATES])
    return base * rng.lognormal(0, 0.03, len(DATES))


def test_seasonal_detector_is_quiet_on_a_normal_weekly_pattern():
    assert seasonal(weekday_series(), DATES, P) == []


def test_a_weekend_spike_is_seen_only_by_the_seasonal_detector():
    x = weekday_series()
    sat = next(i for i in range(120, 140) if DATES[i].weekday() == 5)
    x[sat] = 70.0  # a busy Saturday: below the weekday median, far above a normal Saturday
    assert any(f.day == sat for f in seasonal(x, DATES, P))
    assert not any(f.day == sat for f in robust_z(x, DATES, P))


def test_both_detectors_flag_a_large_spike_on_the_day():
    x = weekday_series()
    day = next(i for i in range(100, 120) if DATES[i].weekday() == 2)
    x[day] *= 3
    assert any(f.day == day for f in seasonal(x, DATES, P))
    assert any(f.day == day for f in robust_z(x, DATES, P))


def test_cusum_catches_a_small_sustained_shift():
    rng = np.random.default_rng(3)
    x = 200 * rng.lognormal(0, 0.05, len(DATES))
    x[120:] += 25  # +12.5%, about two noise widths: rarely extreme on any one day
    flags = seasonal(x, DATES, P)
    assert any(120 <= f.day <= 135 for f in flags)
    assert all(f.day >= 115 for f in flags)


def test_month_end_peaks_are_learned_after_two_months():
    x = np.full(len(DATES), 100.0)
    for i, d in enumerate(DATES):
        nxt = d + dt.timedelta(days=2)
        if nxt.month != d.month:  # the last two days of each month
            x[i] = 160.0
    late = [f for f in seasonal(x, DATES, P) if f.day >= 120]
    assert late == []
