"""Two online anomaly detectors for a daily cost series. Both only look at days before the one they score.

* `robust_z`  (baseline): trailing 28-day median and MAD; flags a day whose robust z-score is high.
* `seasonal`  : an online seasonal-trend decomposition (weekday profile, month-end profile and a robust
  level) whose residual is scored with a robust z-score, plus a CUSUM change-point detector on the same
  standardised residuals to catch sustained shifts that are too small to stand out on any single day.

STL proper is centred and uses future days, so it cannot score today's cost the way a daily job must;
this decomposition uses trailing windows only.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass

import numpy as np

MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Params:
    z: float = 4.0  # robust z-score that flags a single day
    min_excess: float = 10.0  # ignore days whose cost is less than this many dollars above expected
    cusum_k: float = 1.0  # CUSUM slack, in standard deviations
    cusum_h: float = 8.0  # CUSUM alarm threshold
    min_impact: float = 50.0  # drop incidents whose total extra cost is below this
    window: int = 28
    history: int = 56


@dataclass
class Flag:
    day: int
    actual: float
    expected: float
    score: float
    reason: str  # "spike" or "shift"

    @property
    def excess(self) -> float:
        return self.actual - self.expected


def _scale(residuals: np.ndarray, level: float) -> float:
    if residuals.size == 0:
        return max(0.02 * level, 1.0)
    mad = float(np.median(np.abs(residuals - np.median(residuals))))
    return max(MAD_TO_SIGMA * mad, 0.02 * abs(level), 1.0)


def robust_z(x: np.ndarray, dates: list[dt.date], p: Params) -> list[Flag]:
    flags = []
    for t in range(p.window, len(x)):
        hist = x[t - p.window : t]
        med = float(np.median(hist))
        scale = _scale(hist, med)
        z = (x[t] - med) / scale
        if z >= p.z and x[t] - med >= p.min_excess:
            flags.append(Flag(t, float(x[t]), med, float(z), "spike"))
    return flags


def _month_end(d: dt.date) -> bool:
    return d.day >= calendar.monthrange(d.year, d.month)[1] - 1


def seasonal(x: np.ndarray, dates: list[dt.date], p: Params) -> list[Flag]:
    n = len(x)
    dow = np.array([d.weekday() for d in dates])
    month_end = np.array([_month_end(d) for d in dates])
    base = np.full(n, np.nan)  # expected cost before the month-end factor
    expected = np.full(n, np.nan)
    spike = np.zeros(n, dtype=bool)  # isolated flagged days, left out of the baseline
    flags: list[Flag] = []
    regime = 0  # first day of the current level; moved forward when a change point is confirmed
    cusum, cusum_from, run_excess = 0.0, 0, 0.0
    for t in range(p.window, n):
        lo = max(0, t - p.history)
        # The weekday profile is read from one regime only: mixing days from before and after a change
        # point would distort it. Use the current regime once it has four weeks, else the one before it.
        if t - regime >= p.window:
            w_lo, w_hi = max(lo, regime), t
        elif regime - lo >= 14:
            w_lo, w_hi = lo, regime
        else:
            w_lo, w_hi = lo, t
        keep = ~spike[w_lo:w_hi] & ~month_end[w_lo:w_hi]
        h_norm, d_norm = x[w_lo:w_hi][keep], dow[w_lo:w_hi][keep]
        overall = float(np.median(h_norm)) if h_norm.size else 0.0
        weekday = np.ones(7)
        if overall > 0:
            for k in range(7):
                sel = h_norm[d_norm == k]
                if sel.size >= 2:
                    weekday[k] = float(np.median(sel)) / overall
        # Level: robust median of de-seasonalised cost since the last change point (up to `window` days).
        r_lo = max(lo, t - p.window, regime)
        days = np.arange(r_lo, t)
        days = days[~spike[days] & ~month_end[days] & (weekday[dow[days]] > 0.05)]
        level = float(np.median(x[days] / weekday[dow[days]])) if days.size else overall
        base[t] = level * weekday[dow[t]]
        factor = 1.0
        if month_end[t]:
            lo_me = max(p.window, t - 120)
            past = np.nonzero(month_end[lo_me:t] & (base[lo_me:t] > 0))[0] + lo_me
            if past.size >= 2:
                factor = float(np.median(x[past] / base[past]))
        expected[t] = base[t] * factor
        done = np.arange(max(p.window, t - p.history), t)
        done = done[~spike[done]]
        if done.size < 14:
            continue  # warm-up: not enough residuals yet to know what normal noise looks like
        scale = _scale(x[done] - expected[done], level)
        excess = x[t] - expected[t]
        z = excess / scale
        if z >= p.z and excess >= p.min_excess:
            flags.append(Flag(t, float(x[t]), float(expected[t]), float(z), "spike"))
            spike[t] = True
            if t >= 2 and spike[t - 1] and spike[t - 2]:
                # Three high days in a row is a new level, not a spike: restart the baseline there.
                regime = t - 2
                spike[t - 2 : t + 1] = False
            cusum, run_excess = 0.0, 0.0
            continue
        if cusum == 0.0:
            cusum_from = t
        cusum = max(0.0, cusum + min(float(z), 8.0) - p.cusum_k)
        run_excess = run_excess + excess if cusum > 0 else 0.0
        if cusum >= p.cusum_h and run_excess >= 3 * p.min_excess and excess > 0:
            flags.append(Flag(t, float(x[t]), float(expected[t]), float(cusum), "shift"))
            regime = cusum_from  # the change point: where the cumulative sum started to climb
            cusum, run_excess = 0.0, 0.0
    return flags


METHODS = {"robust_z": robust_z, "seasonal": seasonal}

# Chosen on the development seed by `finops-agent eval` (see eval/results.json, "tuning").
TUNED = {
    "robust_z": Params(z=4.0, min_excess=5.0),
    "seasonal": Params(z=5.0, min_excess=5.0),
}
