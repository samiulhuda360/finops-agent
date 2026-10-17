"""Savings recommendations priced from the public price lists.

Inputs are the FOCUS billing rows, a monitoring export (daily CPU and memory per VM, disk attachment, GB read
per storage account or bucket) and the price snapshot. Five kinds of recommendation are made:

* idle        VMs averaging under 3% CPU for 30 days, and disks or volumes attached to nothing;
* rightsize   the smallest size in the same family that keeps p95 CPU at or under 70% and p95 memory at
              or under 80%;
* commitment  production VMs (and an AKS node pool's always-on floor) that ran at least 95% of the hours in
              the last 60 days, priced at the cheapest 1-year reservation or savings plan, with break-even;
* tiering     storage reading less than 10% of what it holds per month, moved to a cool or infrequent-access
              tier, net of retrieval charges;
* missing_tags resources without the team, environment and project tags (no saving; ownership).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ..focus import TAG_KEYS, UNTAGGED
from ..generator.simulate import FAMILIES, family_of
from ..prices import HOURS_PER_MONTH, PriceBook

MONTH_DAYS = 365.25 / 12
LOOKBACK = 30
COMMIT_LOOKBACK = 60
IDLE_CPU = 3.0
CPU_TARGET = 70.0
MEM_TARGET = 80.0
STEADY_SHARE = 0.95
COOL_RATIO = 0.10
COMMIT_ENVIRONMENTS = {"prod"}


@dataclass
class Recommendation:
    id: str
    type: str
    resource_id: str
    resource_name: str
    provider: str
    region: str
    team: str
    current: str
    target: str
    monthly_cost_now: float
    monthly_saving: float
    action: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def vm_size(provider: str, meter: str) -> str:
    """FOCUS SkuMeter to the size name in the price list ("D4s v5"/"D4s_v5" -> "Standard_D4s_v5")."""
    if provider == "Microsoft":
        return "Standard_" + re.sub(r"\s+", "_", meter.strip())
    return meter.split(":", 1)[-1]


class Recommender:
    def __init__(self, df: pd.DataFrame, metrics: pd.DataFrame, prices: PriceBook | None = None) -> None:
        self.prices = prices or PriceBook()
        self.end = df["date"].max()
        self.df = df
        self.recent = df[df["date"] > self.end - pd.Timedelta(days=LOOKBACK)]
        self.long = df[df["date"] > self.end - pd.Timedelta(days=COMMIT_LOOKBACK)]
        m = metrics.copy()
        m["date"] = pd.to_datetime(m["date"])
        self.metrics = m[m["date"] > self.end - pd.Timedelta(days=LOOKBACK)]
        last = df[df["date"] == self.end]
        self.alive = set(last["ResourceId"])
        self.info = last.drop_duplicates("ResourceId").set_index("ResourceId")
        self.recs: list[Recommendation] = []

    # ------------------------------------------------------------------ helpers
    def _monthly_cost(self, rid: str) -> float:
        cost = self.recent.loc[self.recent["ResourceId"] == rid, "EffectiveCost"].sum()
        return float(cost) * MONTH_DAYS / LOOKBACK

    def _add(
        self,
        kind: str,
        rid: str,
        current: str,
        target: str,
        saving: float,
        action: str,
        cost_now: float | None = None,
        **ev: Any,
    ) -> None:
        row = self.info.loc[rid]
        self.recs.append(
            Recommendation(
                id="",
                type=kind,
                resource_id=rid,
                resource_name=str(row["ResourceName"]),
                provider=str(row["ServiceProviderName"]),
                region=str(row["RegionId"]),
                team=str(row["Tags.team"]),
                current=current,
                target=target,
                monthly_cost_now=round(self._monthly_cost(rid) if cost_now is None else cost_now, 2),
                monthly_saving=round(saving, 2),
                action=action,
                evidence={k: (round(v, 2 if abs(v) >= 1 else 6) if isinstance(v, float) else v) for k, v in ev.items()},
            )
        )

    def _attached_storage(self) -> dict[str, list[str]]:
        disks = self.metrics.dropna(subset=["attached_to"]) if "attached_to" in self.metrics else pd.DataFrame()
        out: dict[str, list[str]] = {}
        if disks.empty:
            return out
        latest = disks.sort_values("date").drop_duplicates("ResourceId", keep="last")
        for _, r in latest.iterrows():
            if isinstance(r["attached_to"], str) and r["attached_to"]:
                out.setdefault(r["attached_to"], []).append(str(r["ResourceId"]))
        return out

    # ------------------------------------------------------------------ rules
    def run(self) -> list[Recommendation]:
        self.vms()
        self.node_pools()
        self.unattached()
        self.tiering()
        self.tags()
        self.recs.sort(key=lambda r: r.monthly_saving, reverse=True)
        for n, r in enumerate(self.recs, 1):
            r.id = f"R{n:03d}"
        return self.recs

    def vms(self) -> None:
        attached = self._attached_storage()
        vm_rows = self.recent[self.recent["ResourceType"].isin(["Virtual Machine", "Instance"])]
        util = self.metrics.groupby("ResourceId").agg(
            cpu_avg=("cpu_avg", "mean"),
            cpu_p95=("cpu_p95", lambda s: float(np.percentile(s.dropna(), 95)) if s.notna().any() else np.nan),
            mem_p95=("mem_p95", lambda s: float(np.percentile(s.dropna(), 95)) if s.notna().any() else np.nan),
            days=("cpu_avg", "count"),
        )
        for rid, rows in vm_rows.groupby("ResourceId"):
            if rid not in self.alive or rid not in util.index:
                continue
            row = self.info.loc[rid]
            provider, region = str(row["ServiceProviderName"]), str(row["RegionId"])
            size = vm_size(provider, str(row["SkuMeter"]))
            sizes = self.prices.vm_sizes(provider, region)
            if size not in sizes:
                continue
            u = util.loc[rid]
            hours = float(rows["ConsumedQuantity"].sum())
            monthly_hours = hours * MONTH_DAYS / LOOKBACK
            od = sizes[size]["ondemand_hourly"]
            if u["cpu_avg"] < IDLE_CPU:
                disk_cost = sum(self._monthly_cost(d) for d in attached.get(str(rid), []))
                vm_cost = self._monthly_cost(str(rid))
                self._add(
                    "idle",
                    str(rid),
                    size,
                    "stop or delete",
                    od * monthly_hours + disk_cost,
                    "Average CPU stayed under 3% for 30 days: confirm with the owner, snapshot, then delete it.",
                    cost_now=vm_cost + disk_cost,
                    cpu_avg_pct=float(u["cpu_avg"]),
                    hours_last_30_days=hours,
                    attached_disks=len(attached.get(str(rid), [])),
                )
                continue
            target = self._fit(provider, size, sizes, float(u["cpu_p95"]), float(u["mem_p95"]))
            if target != size:
                new = sizes[target]["ondemand_hourly"]
                self._add(
                    "rightsize",
                    str(rid),
                    size,
                    target,
                    (od - new) * monthly_hours,
                    f"Resize from {size} to {target}; projected p95 CPU stays under {CPU_TARGET:.0f}%.",
                    cpu_p95_pct=float(u["cpu_p95"]),
                    mem_p95_pct=float(u["mem_p95"]),
                    projected_cpu_p95_pct=float(u["cpu_p95"]) * sizes[size]["vcpu"] / sizes[target]["vcpu"],
                    hourly_price_now=od,
                    hourly_price_target=new,
                    monthly_hours=monthly_hours,
                )
                continue
            self._commitment(str(rid), provider, region, size, count=1.0)

    def _fit(self, provider: str, size: str, sizes: dict[str, dict[str, float]], cpu: float, mem: float) -> str:
        cur = sizes[size]
        family = family_of(provider, size)
        for s in family[: family.index(size) + 1]:
            spec = sizes[s]
            if (
                cpu * cur["vcpu"] / spec["vcpu"] <= CPU_TARGET
                and mem * cur["memory_gb"] / spec["memory_gb"] <= MEM_TARGET
            ):
                return s
        return size

    def _commitment(self, rid: str, provider: str, region: str, size: str, count: float) -> None:
        row = self.info.loc[rid]
        if row["Tags.environment"] not in COMMIT_ENVIRONMENTS:
            return
        hist = self.long[self.long["ResourceId"] == rid]
        if row["PricingCategory"] == "Committed":
            return
        if count == 1.0:
            first_day = self.df.loc[self.df["ResourceId"] == rid, "date"].min()
            if first_day > self.end - pd.Timedelta(days=COMMIT_LOOKBACK - 1):
                return
            if hist["ConsumedQuantity"].sum() < STEADY_SHARE * 24 * COMMIT_LOOKBACK:
                return
        offer = self.prices.commitment_hourly(provider, region, size)
        if not offer:
            return
        od = self.prices.vm(provider, region, size)["ondemand_hourly"]
        saving = count * (od - offer[1]) * HOURS_PER_MONTH
        break_even = offer[1] / od
        what = f"{count:.0f} x {size}" if count > 1 else size
        self._add(
            "commitment",
            rid,
            f"pay-as-you-go {what}",
            offer[0],
            saving,
            f"Buy a {offer[0]} for {what}; it pays off if the capacity runs more than {break_even:.0%} of the time.",
            hourly_on_demand=od,
            hourly_committed=offer[1],
            break_even_utilisation_pct=break_even * 100,
            months_to_recover_a_year=12 * break_even,
            covered_instances=count,
        )

    def node_pools(self) -> None:
        pools = self.long[self.long["ResourceType"] == "Virtual Machine Scale Set"]
        for rid, rows in pools.groupby("ResourceId"):
            if rid not in self.alive:
                continue
            row = self.info.loc[rid]
            provider, region = str(row["ServiceProviderName"]), str(row["RegionId"])
            size = vm_size(provider, str(row["SkuMeter"]))
            nodes = rows.groupby("date")["ConsumedQuantity"].sum() / 24
            floor = float(np.floor(np.percentile(nodes, 5)))
            if floor >= 1:
                self._commitment(str(rid), provider, region, size, count=floor)

    def unattached(self) -> None:
        if "attached_to" not in self.metrics:
            return
        disks = self.metrics.dropna(subset=["attached_to"]).copy()
        disks["attached"] = disks["attached_to"].fillna("").astype(str).str.len() > 0
        state = disks.groupby("ResourceId")["attached"].any()
        for rid, used in state.items():
            if used or rid not in self.alive:
                continue
            monthly = self._monthly_cost(str(rid))
            self._add(
                "idle",
                str(rid),
                str(self.info.loc[rid, "SkuMeter"]),
                "delete (snapshot first)",
                monthly,
                "Attached to nothing for 30 days: take a snapshot, then delete it.",
                days_unattached=LOOKBACK,
            )

    def tiering(self) -> None:
        if "read_gb" not in self.metrics:
            return
        reads = self.metrics.dropna(subset=["read_gb"]).groupby("ResourceId")["read_gb"].sum()
        stores = self.recent[self.recent["ResourceType"].isin(["Storage Account", "Bucket"])]
        stores = stores[stores["ConsumedUnit"] == "GB-Months"]
        last = stores[stores["date"] == self.end]
        for rid, rows in last.groupby("ResourceId"):
            gb = float(rows["ConsumedQuantity"].sum()) * HOURS_PER_MONTH / 24
            if gb <= 0 or rid not in reads.index:
                continue
            read_month = float(reads[rid]) * MONTH_DAYS / LOOKBACK
            ratio = read_month / gb
            if ratio >= COOL_RATIO:
                continue
            provider, region = str(rows["ServiceProviderName"].iloc[0]), str(rows["RegionId"].iloc[0])
            if provider == "Microsoft":
                p = self.prices.azure(region)["blob"]
                hot, cool, retr, tier = p["hot_gb_month"], p["cool_gb_month"], p["cool_retrieval_gb"], "Cool"
            else:
                p = self.prices.aws(region)["s3"]
                hot, cool, retr = p["standard_gb_month"], p["standard_ia_gb_month"], p["ia_retrieval_gb"]
                tier = "Standard-IA"
            saving = gb * (hot - cool) - read_month * retr
            if saving <= 0:
                continue
            self._add(
                "tiering",
                str(rid),
                "Hot" if provider == "Microsoft" else "Standard",
                tier,
                saving,
                f"Move to the {tier} tier with a lifecycle rule; it reads {ratio:.1%} of its data a month.",
                stored_gb=gb,
                read_gb_per_month=read_month,
                price_now_per_gb=hot,
                price_target_per_gb=cool,
                retrieval_per_gb=retr,
            )

    def tags(self) -> None:
        last = self.df[self.df["date"] == self.end]
        for rid, rows in last.groupby("ResourceId"):
            r = rows.iloc[0]
            missing = [k for k in TAG_KEYS if r[f"Tags.{k}"] == UNTAGGED]
            if missing:
                self._add(
                    "missing_tags",
                    str(rid),
                    "tags missing: " + ", ".join(missing),
                    "add owner tags",
                    0.0,
                    "Add team, environment and project tags so the cost has an owner.",
                    missing_tags=", ".join(missing),
                )


def recommend(df: pd.DataFrame, metrics: pd.DataFrame, prices: PriceBook | None = None) -> list[Recommendation]:
    return Recommender(df, metrics, prices).run()


__all__ = ["FAMILIES", "Recommendation", "recommend"]
