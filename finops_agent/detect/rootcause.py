"""Root-cause drill-down: which dimension and values explain a cost increase.

The scoring follows Adtributor (Bhagwan et al., NSDI 2014). For each candidate dimension, every value gets
  * explanatory power (EP): its share of the total increase, and
  * surprise: the Jensen-Shannon term between its share of expected cost and its share of actual cost.
A dimension explains the incident when at most three of its values, taken in order of surprise, cover at
least two thirds of the increase. Named resources are preferred when three or fewer explain it; otherwise the
dimension with the highest surprise wins, and when nothing is surprising the flagged series itself is reported.
One exception keeps the answer at the level a person would name: when a single value that was almost absent
before (under 5% of expected cost, such as a new region or an untagged slice) explains the increase and more
than one resource would be needed, that value is the root.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DIMENSIONS = [
    "ResourceId",
    "RegionId",
    "SubAccountName",
    "ServiceName",
    "PricingCategory",
    "Tags.team",
    "Tags.environment",
    "Tags.project",
]
EP_TARGET = 0.67
EP_MIN = 0.10
MAX_ELEMENTS = 3
MIN_SURPRISE = 0.002
NEW_SHARE = 0.05


@dataclass
class Root:
    dimension: str
    values: list[str]
    explained: float  # share of the increase the values explain
    surprise: float

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return self.dimension, tuple(sorted(self.values))


def _js(p: float, q: float) -> float:
    m = (p + q) / 2
    out = 0.0
    if p > 0:
        out += 0.5 * p * np.log(p / m)
    if q > 0:
        out += 0.5 * q * np.log(q / m)
    return float(out)


def same_weekday_baseline(cube: pd.DataFrame, days: list[int]) -> pd.Series:
    """Expected cost per value over `days`: the mean of the same weekday in the four weeks before."""
    values = cube.to_numpy()
    total = np.zeros(values.shape[1])
    for d in days:
        prev = [d - 7 * k for k in range(1, 5) if d - 7 * k >= 0]
        if prev:
            total += values[prev].mean(axis=0)
    return pd.Series(total, index=cube.columns)


def daily_cube(rows: pd.DataFrame, dim: str, n_days: int) -> pd.DataFrame:
    """Day x value matrix of EffectiveCost."""
    cube = rows.groupby(["d", dim])["EffectiveCost"].sum().unstack(fill_value=0)
    return cube.reindex(range(n_days), fill_value=0.0)


def explain(scope: pd.DataFrame, days: list[int], n_days: int, exclude: str | None = None) -> Root | None:
    """Explain the increase in `scope` (FOCUS rows with a day index column `d`) over `days`."""
    candidates: list[tuple[int, Root, float]] = []
    for dim in DIMENSIONS:
        if dim == exclude:
            continue
        cube = daily_cube(scope, dim, n_days)
        actual = cube.iloc[days].sum(axis=0)
        forecast = same_weekday_baseline(cube, days)
        a_tot, f_tot = float(actual.sum()), float(forecast.sum())
        delta = a_tot - f_tot
        if delta <= 0:
            return None
        rows = []
        for value in cube.columns:
            ep = (actual[value] - forecast[value]) / delta
            p = forecast[value] / f_tot if f_tot > 0 else 0.0
            q = actual[value] / a_tot if a_tot > 0 else 0.0
            rows.append((str(value), float(ep), _js(p, q), p))
        rows = [r for r in rows if r[1] >= EP_MIN]
        rows.sort(key=lambda r: r[2], reverse=True)
        chosen: list[str] = []
        ep_sum, s_sum, p_first = 0.0, 0.0, 1.0
        for value, ep, s, p in rows:
            if not chosen:
                p_first = p
            chosen.append(value)
            ep_sum += ep
            s_sum += s
            if ep_sum >= EP_TARGET or len(chosen) == MAX_ELEMENTS:
                break
        if ep_sum >= EP_TARGET:
            candidates.append((len(chosen), Root(dim, chosen, round(ep_sum, 3), s_sum), p_first))
    by_resource = [c[1] for c in candidates if c[1].dimension == "ResourceId"]
    if not by_resource or len(by_resource[0].values) > 1:
        new_values = [c[1] for c in candidates if c[0] == 1 and c[2] < NEW_SHARE and c[1].dimension != "ResourceId"]
        if new_values:
            return max(new_values, key=lambda r: r.surprise)
    if by_resource:
        return by_resource[0]
    others = [c[1] for c in candidates if c[1].surprise >= MIN_SURPRISE]
    return max(others, key=lambda r: r.surprise) if others else None
