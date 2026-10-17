"""From daily FOCUS rows to explained incidents.

1. Build one daily cost series per service, sub-account, region and tag value.
2. Score every series with a detector (`methods.py`); a run of flagged days is a segment.
3. Drill each segment down to its root cause (`rootcause.py`) and merge segments with the same root.
4. For each incident, estimate the start date, the cost impact so far and the monthly projection from the
   root's own daily cost.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .methods import METHODS, TUNED, Flag, Params
from .rootcause import Root, daily_cube, explain

SERIES_DIMENSIONS = ["ServiceName", "SubAccountName", "RegionId", "Tags.team", "Tags.environment", "Tags.project"]
SEGMENT_GAP = 3
MERGE_GAP = 14
MONTH_DAYS = 365.25 / 12
DIM_LABELS = {
    "ResourceId": "resource",
    "RegionId": "region",
    "SubAccountName": "account",
    "ServiceName": "service",
    "PricingCategory": "pricing category",
    "Tags.team": "team tag",
    "Tags.environment": "environment tag",
    "Tags.project": "project tag",
}


@dataclass
class Incident:
    id: str
    method: str
    detected: str
    start: str
    end: str | None
    ongoing: bool
    root_dimension: str
    root_values: list[str]
    root_names: list[str]
    explained_share: float
    cause: str
    provider: str
    flagged_series: list[str]
    expected_daily: float
    actual_daily: float
    impact_to_date: float
    monthly_projection: float
    teams: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)  # resources carrying at least 10% of the increase
    segments: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Segment:
    series: tuple[str, str]
    flags: list[Flag]

    @property
    def first(self) -> int:
        return self.flags[0].day

    @property
    def last(self) -> int:
        return self.flags[-1].day


class Detector:
    def __init__(self, df: pd.DataFrame, method: str = "seasonal", params: Params | None = None) -> None:
        self.method = method
        self.params = params or TUNED[method]
        self.dates: list[dt.date] = [d.date() for d in pd.date_range(df["date"].min(), df["date"].max())]
        self.n = len(self.dates)
        day0 = pd.Timestamp(self.dates[0])
        self.df = df.assign(d=((df["date"] - day0).dt.days).astype(int))
        self.names = self.df.drop_duplicates("ResourceId").set_index("ResourceId")["ResourceName"].to_dict()

    # -------------------------------------------------------------- series and flags
    def series(self) -> dict[tuple[str, str], np.ndarray]:
        out = {}
        for dim in SERIES_DIMENSIONS:
            cube = daily_cube(self.df, dim, self.n)
            for value in cube.columns:
                x = cube[value].to_numpy(dtype=float)
                if x.sum() >= 30:
                    out[(dim, str(value))] = x
        return out

    def segments(self) -> list[Segment]:
        fn = METHODS[self.method]
        segs: list[Segment] = []
        for key, x in self.series().items():
            current: list[Flag] = []
            for f in fn(x, self.dates, self.params):
                if current and f.day - current[-1].day > SEGMENT_GAP:
                    segs.append(Segment(key, current))
                    current = []
                current.append(f)
            if current:
                segs.append(Segment(key, current))
        return sorted(segs, key=lambda s: s.first)

    # -------------------------------------------------------------- incidents
    def run(self) -> list[Incident]:
        groups: list[tuple[Root, list[Segment]]] = []
        for seg in self.segments():
            dim, value = seg.series
            days = list(range(seg.first, min(seg.last, seg.first + 6) + 1))
            # Drill down across everything the flagged provider(s) bill, so the flagged slice competes on equal
            # terms with every other way of cutting the same increase.
            providers = self.df.loc[self.df[dim] == value, "ServiceProviderName"].unique()
            scope = self.df[self.df["ServiceProviderName"].isin(providers)]
            root = explain(scope, days, self.n) or Root(dim, [value], 1.0, 0.0)
            for g_root, g_segs in groups:
                if g_root.key == root.key and seg.first <= max(s.last for s in g_segs) + MERGE_GAP:
                    g_segs.append(seg)
                    break
            else:
                groups.append((root, [seg]))
        incidents = sorted((self._incident(root, segs) for root, segs in groups), key=lambda x: x.detected)
        incidents = [i for i in self._dedupe(incidents) if i.impact_to_date >= self.params.min_impact]
        for n, inc in enumerate(incidents, 1):
            inc.id = f"{self.method[:1].upper()}{n:03d}"
        return incidents

    @staticmethod
    def _dedupe(incidents: list[Incident]) -> list[Incident]:
        """One event seen through several series can drill down to slightly different roots (one or both of two
        VMs, say). Incidents that start within three days of each other and share a contributing resource are the
        same event; the root seen through the most series is kept."""
        groups: list[list[Incident]] = []
        for inc in incidents:
            for g in groups:
                if any(
                    abs((dt.date.fromisoformat(inc.start) - dt.date.fromisoformat(o.start)).days) <= 3
                    and set(inc.resources) & set(o.resources)
                    for o in g
                ):
                    g.append(inc)
                    break
            else:
                groups.append([inc])
        out = []
        for g in groups:
            best = max(g, key=lambda i: (i.segments, i.impact_to_date))
            best.flagged_series = sorted({s for i in g for s in i.flagged_series})
            best.detected = min(i.detected for i in g)
            best.segments = sum(i.segments for i in g)
            out.append(best)
        return sorted(out, key=lambda x: x.detected)

    def root_rows(self, root: Root) -> pd.DataFrame:
        return self.df[self.df[root.dimension].isin(root.values)]

    def _incident(self, root: Root, segs: list[Segment]) -> Incident:
        rows = self.root_rows(root)
        x = rows.groupby("d")["EffectiveCost"].sum().reindex(range(self.n), fill_value=0.0).to_numpy()
        t0 = min(s.first for s in segs)
        # Reference: the same weekday over the four weeks that end two weeks before detection.
        ref = [d for d in range(t0 - 42, t0 - 14) if d >= 0]
        dow = np.array([d.weekday() for d in self.dates])
        baseline = np.zeros(self.n)
        for k in range(7):
            vals = [x[d] for d in ref if dow[d] == k]
            baseline[dow == k] = float(np.median(vals)) if vals else 0.0
        excess = x - baseline
        threshold = max(0.3 * float(np.max(excess[t0 : t0 + 3])), self.params.min_excess / 3, 1.0)
        start = t0
        while start - 1 >= max(0, t0 - 14) and excess[start - 1] > threshold:
            start -= 1
        end, quiet = t0, 0
        for d in range(t0, self.n):
            if excess[d] > threshold:
                end, quiet = d, 0
            else:
                quiet += 1
                if quiet >= 3:
                    break
        ongoing = end >= self.n - 2
        impact = float(np.clip(excess[start : end + 1], 0, None).sum())
        projection = float(np.clip(excess[-7:], 0, None).mean() * MONTH_DAYS) if ongoing else 0.0
        window = rows[(rows["d"] >= start) & (rows["d"] <= end)]
        names = [self.names.get(v, v) if root.dimension == "ResourceId" else v for v in root.values]
        return Incident(
            id="",
            method=self.method,
            detected=str(self.dates[t0]),
            start=str(self.dates[start]),
            end=None if ongoing else str(self.dates[end]),
            ongoing=ongoing,
            root_dimension=root.dimension,
            root_values=list(root.values),
            root_names=names,
            explained_share=root.explained,
            cause=self._cause(rows, root, start, baseline, x),
            provider=", ".join(sorted(window["ServiceProviderName"].unique())) or "",
            flagged_series=sorted({f"{s.series[0]}={s.series[1]}" for s in segs}),
            expected_daily=round(float(baseline[t0]), 2),
            actual_daily=round(float(x[t0]), 2),
            impact_to_date=round(impact, 2),
            monthly_projection=round(projection, 2),
            teams=sorted(window["Tags.team"].unique().tolist()),
            services=window.groupby("ServiceName")["EffectiveCost"]
            .sum()
            .sort_values(ascending=False)
            .index[:3]
            .tolist(),
            resources=self._contributors(rows, ref, dow, start, end),
            segments=len(segs),
        )

    def _contributors(self, rows: pd.DataFrame, ref: list[int], dow: np.ndarray, start: int, end: int) -> list[str]:
        cube = daily_cube(rows, "ResourceId", self.n)
        days = list(range(start, min(end, start + 6) + 1))
        contrib = pd.Series(0.0, index=cube.columns)
        for d in days:
            same = [r for r in ref if dow[r] == dow[d]]
            base = cube.iloc[same].median(axis=0) if same else 0.0
            contrib += cube.iloc[d] - base
        contrib = contrib.clip(lower=0)
        total = float(contrib.sum())
        if total <= 0:
            return []
        return sorted(str(r) for r, v in contrib.items() if v >= 0.1 * total)

    def _cause(self, rows: pd.DataFrame, root: Root, start: int, baseline: np.ndarray, x: np.ndarray) -> str:
        before = rows[(rows["d"] >= start - 28) & (rows["d"] < start)]
        after = rows[(rows["d"] >= start) & (rows["d"] < start + 7)]
        if before["EffectiveCost"].sum() < 1.0:
            return "new spend that did not exist before"
        pc_before = set(before["PricingCategory"])
        pc_after = set(after["PricingCategory"])
        if "Committed" in pc_before and "Committed" not in pc_after:
            return "a commitment discount ended, so the same usage is billed at pay-as-you-go prices"
        new = set(after["ResourceId"]) - set(before["ResourceId"])
        new_cost = after.loc[after["ResourceId"].isin(new), "EffectiveCost"].sum()
        if new and new_cost > 0.5 * max(after["EffectiveCost"].sum() - before["EffectiveCost"].sum() / 4, 1e-9):
            return f"{len(new)} new resources appeared"
        q_before = before.groupby("d")["ConsumedQuantity"].sum().mean()
        q_after = after.groupby("d")["ConsumedQuantity"].sum().mean()
        c_before = before.groupby("d")["EffectiveCost"].sum().mean()
        c_after = after.groupby("d")["EffectiveCost"].sum().mean()
        if q_before > 0 and q_after / q_before > 1.2:
            return "usage went up"
        if c_before > 0 and c_after / c_before > 1.2:
            return "the price per unit went up"
        return "spend rose above its usual level"


def detect(df: pd.DataFrame, method: str = "seasonal", params: Params | None = None) -> list[Incident]:
    return Detector(df, method, params).run()
