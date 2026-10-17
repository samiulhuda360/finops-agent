"""Daily simulation: FOCUS billing rows, monitoring metrics, anomaly labels and the savings ground truth."""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ..prices import HOURS_PER_MONTH, PriceBook
from .anomalies import Planted, planned_launch, plant
from .company import (
    AWS_BILLING,
    AZURE_BILLING,
    REGION_NAMES,
    Resource,
    inventory,
)

START = dt.date(2025, 10, 1)
DAYS = 365
DAY_HOURS = 24.0
MONTH_DAYS = 365.25 / 12
RESERVATION_ID = (
    "/providers/microsoft.capacity/reservationorders/5b1e0c7a-93d2-4f4e-a1b8-0c6d2e9f7a31/reservations/ri-sql-prod-2025"
)

FAMILIES = {
    "Microsoft": [
        ["Standard_D2s_v5", "Standard_D4s_v5", "Standard_D8s_v5", "Standard_D16s_v5"],
        ["Standard_E2s_v5", "Standard_E4s_v5", "Standard_E8s_v5"],
        ["Standard_B2s", "Standard_B2ms"],
    ],
    "AWS": [
        ["m6i.large", "m6i.xlarge", "m6i.2xlarge", "m6i.4xlarge"],
        ["r6i.large", "r6i.xlarge", "r6i.2xlarge"],
        ["c6i.large", "c6i.xlarge"],
        ["t3.medium", "t3.large"],
    ],
}
CPU_TARGET = 70.0  # a size fits when the projected p95 CPU stays at or under this
MEM_TARGET = 80.0


def family_of(provider: str, size: str) -> list[str]:
    return next(f for f in FAMILIES[provider] if size in f)


def is_month_end(d: dt.date) -> bool:
    return d.day >= calendar.monthrange(d.year, d.month)[1] - 1


def traffic(rng: np.random.Generator) -> np.ndarray:
    """Business traffic: 1.5% monthly growth, quieter weekends, a pre-Christmas peak, a summer-holiday dip
    and noise. The peak is real business (not labelled), so flagging it counts as a false positive."""
    t = np.empty(DAYS)
    for i in range(DAYS):
        d = START + dt.timedelta(days=i)
        f = 1.015 ** (i / MONTH_DAYS)
        f *= (1.0, 1.0, 1.0, 1.0, 1.0, 0.78, 0.72)[d.weekday()]
        if (d.month == 12 and d.day >= 23) or (d.month == 1 and d.day <= 4):
            f *= 0.75
        if (d.month == 11 and d.day >= 24) or (d.month == 12 and d.day <= 5):
            f *= 1.35  # Black Friday to early December freight peak
        t[i] = f
    return t * rng.lognormal(0.0, 0.04, DAYS)


def hours_run(res: Resource, d: dt.date, rng: np.random.Generator) -> float:
    if res.schedule == "workhours":
        if d.weekday() >= 5:
            return 0.0
        return 10.0 + (2.0 if rng.random() < 0.1 else 0.0)  # someone occasionally works late
    if res.schedule == "nightly":
        return 20.0 if is_month_end(d) else 6.0  # month-end billing run
    return DAY_HOURS


def true_monthly_hours(res: Resource) -> float:
    if res.schedule == "workhours":
        return 10.0 * 261 / 12
    if res.schedule == "nightly":
        return 6.0 * (MONTH_DAYS - 2) + 20.0 * 2
    return HOURS_PER_MONTH


@dataclass
class Dataset:
    focus: pd.DataFrame
    metrics: pd.DataFrame
    labels: list[dict[str, Any]]
    opportunities: list[dict[str, Any]]
    seed: int
    expected_changes: list[dict[str, Any]]

    def write(self, out: Any) -> None:
        from pathlib import Path

        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        self.focus.to_csv(out / "focus.csv.gz", index=False)
        self.metrics.to_csv(out / "metrics.csv.gz", index=False)
        truth = {
            "seed": self.seed,
            "anomalies": self.labels,
            "opportunities": self.opportunities,
            "expected_changes": self.expected_changes,
        }
        (out / "truth.json").write_text(json.dumps(truth, indent=1), encoding="utf-8")


def _sku(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12].upper()


class Simulator:
    def __init__(self, seed: int, prices: PriceBook | None = None) -> None:
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.prices = prices or PriceBook()
        self.inv = {r.name: r for r in inventory()}
        self.planted = plant(self.rng, DAYS, self.inv)
        self.launch_day, launch = planned_launch(self.rng, DAYS)
        for r in launch:
            self.inv[r.name] = r
        for p in self.planted:
            for r in p.new_resources:
                r.extra["label_id"] = p.id
                self.inv[r.name] = r
        self.effects: dict[str, list[tuple[Planted, Any]]] = {}
        for p in self.planted:
            for e in p.effects:
                self.effects.setdefault(e.resource, []).append((p, e))
        self.traffic = traffic(self.rng)
        self.rows: list[dict[str, Any]] = []
        self.metric_rows: list[dict[str, Any]] = []
        self.excess: dict[str, float] = {p.id: 0.0 for p in self.planted}
        self.excess_by_day: dict[str, np.ndarray] = {p.id: np.zeros(DAYS) for p in self.planted}

    # ---------------------------------------------------------------- pricing helpers
    def unit_price(self, res: Resource) -> tuple[float, str, str]:
        """(list unit price, pricing unit, SKU meter) for one unit of the resource's metered quantity."""
        if res.provider == "Microsoft":
            az = self.prices.azure(res.region if res.region in self.prices.data["azure"] else "newzealandnorth")
            if res.kind in ("vm", "nodepool"):
                return az["vm"][res.size]["ondemand_hourly"], "Hours", res.size.removeprefix("Standard_")
            if res.kind == "disk":
                return az["disk_month"][res.size], "Months", f"{res.size} LRS Disk"
            if res.kind == "blob":
                return az["blob"]["hot_gb_month"], "GB-Months", "Hot LRS Data Stored"
            if res.kind == "egress":
                if res.extra.get("inter_region"):
                    return az["inter_region_gb"], "GB", "Standard Inter-Region Data Transfer"
                return az["egress_gb"], "GB", "Standard Data Transfer Out"
            if res.kind == "logs":
                return az["log_ingestion_gb"], "GB", "Analytics Logs Data Ingestion"
        else:
            aw = self.prices.aws(res.region)
            if res.kind == "vm":
                return aw["ec2"][res.size]["ondemand_hourly"], "Hours", f"BoxUsage:{res.size}"
            if res.kind == "ebs":
                return aw["ebs_gp3_gb_month"], "GB-Months", "EBS:VolumeUsage.gp3"
            if res.kind == "s3":
                return aw["s3"]["standard_gb_month"], "GB-Months", "TimedStorage-ByteHrs"
            if res.kind == "egress":
                return aw["egress_gb"], "GB", "DataTransfer-Out-Bytes"
            if res.kind == "logs":
                return aw["cloudwatch"]["log_ingestion_gb"], "GB", "DataProcessing-Bytes"
        raise ValueError(f"no price for {res.kind}")

    # ---------------------------------------------------------------- per-day quantities
    def _mult(self, res: Resource, i: int, kind: str) -> tuple[float, Planted | None]:
        for p, e in self.effects.get(res.name, []):
            if e.kind == kind and e.start <= i <= e.end:
                return e.value, p
        return (1.0 if kind == "mult" else 0.0), None

    def quantity(self, res: Resource, i: int, d: dt.date) -> tuple[float, float, Planted | None]:
        """(actual quantity, counterfactual quantity without planted effects, the incident causing a difference)."""
        if res.kind == "vm":
            h = hours_run(res, d, self.rng)
            return h, h, None
        if res.kind == "nodepool":
            lo, hi = res.extra["min"], res.extra["max"]
            demand = 2.6 * self.traffic[i] + float(self.rng.normal(0, 0.3))
            base = float(np.clip(lo + demand, lo, hi))
            floor, p = self._mult(res, i, "min_nodes")
            actual = max(base, floor) if p else base
            return actual * DAY_HOURS, base * DAY_HOURS, p
        if res.kind in ("disk",):
            return DAY_HOURS / HOURS_PER_MONTH, DAY_HOURS / HOURS_PER_MONTH, None
        if res.kind in ("ebs",):
            q = res.gb * DAY_HOURS / HOURS_PER_MONTH
            return q, q, None
        if res.kind in ("blob", "s3"):
            age = i - res.start_day
            q = (res.gb + res.growth_gb_day * age) * DAY_HOURS / HOURS_PER_MONTH
            return q, q, None
        if res.kind in ("egress", "logs"):
            base = res.daily_gb * self.traffic[i] * float(self.rng.lognormal(0, 0.12))
            m, p = self._mult(res, i, "mult")
            return base * m, base, p
        raise ValueError(res.kind)

    # ---------------------------------------------------------------- rows
    def emit(self, res: Resource, i: int, d: dt.date) -> None:
        q, q0, cause = self.quantity(res, i, d)
        if q <= 0:
            return
        price, unit, meter = self.unit_price(res)
        list_cost = q * price
        pricing, commit_id, commit_type, commit_status = "Standard", None, None, None
        billed = effective = list_cost
        counterfactual = q0 * price
        label = res.extra.get("label_id")
        if res.reservation:
            ri_end = next((e.start for _p, e in self.effects.get(res.name, []) if e.kind == "ri_end"), DAYS)
            ri_rate = self.prices.vm("Microsoft", res.region, res.size)["reservation_1y_hourly"]
            if i < ri_end:
                pricing, commit_id, commit_type, commit_status = "Committed", RESERVATION_ID, "Reservation", "Used"
                billed = 0.0
                effective = q * ri_rate
                counterfactual = effective
            else:
                counterfactual = q0 * ri_rate
                cause = next(p for p, e in self.effects[res.name] if e.kind == "ri_end")
        if label:
            counterfactual = 0.0
            cause = next(p for p in self.planted if p.id == label)
        if cause is not None:
            extra = effective - counterfactual
            self.excess[cause.id] += extra
            self.excess_by_day[cause.id][i] += extra
        billing = AZURE_BILLING if res.provider == "Microsoft" else AWS_BILLING
        month_start = d.replace(day=1)
        month_end = (month_start + dt.timedelta(days=32)).replace(day=1)
        service, category, rtype = self.service(res)
        self.rows.append(
            {
                "BillingAccountId": billing[0],
                "BillingAccountName": billing[1],
                "BillingCurrency": "USD",
                "BillingPeriodStart": f"{month_start}T00:00:00Z",
                "BillingPeriodEnd": f"{month_end}T00:00:00Z",
                "ChargePeriodStart": f"{d}T00:00:00Z",
                "ChargePeriodEnd": f"{d + dt.timedelta(days=1)}T00:00:00Z",
                "ChargeCategory": "Usage",
                "ChargeFrequency": "Usage-Based",
                "ChargeDescription": f"{meter} usage",
                "BilledCost": round(billed, 6),
                "EffectiveCost": round(effective, 6),
                "ListCost": round(list_cost, 6),
                "ContractedCost": round(list_cost, 6),
                "ListUnitPrice": price,
                "ConsumedQuantity": round(q, 6),
                "ConsumedUnit": unit,
                "PricingQuantity": round(q, 6),
                "PricingUnit": unit,
                "PricingCategory": pricing,
                "CommitmentDiscountId": commit_id,
                "CommitmentDiscountType": commit_type,
                "CommitmentDiscountStatus": commit_status,
                "ServiceProviderName": res.provider,
                "HostProviderName": res.provider,
                "InvoiceIssuerName": res.provider,
                "ServiceName": service,
                "ServiceCategory": category,
                "RegionId": res.region,
                "RegionName": REGION_NAMES[res.region],
                "ResourceId": res.resource_id,
                "ResourceName": res.extra.get("label", res.parent or res.name),
                "ResourceType": rtype,
                "SkuId": _sku(res.provider, res.region, meter),
                "SkuMeter": meter,
                "SubAccountId": res.account[0],
                "SubAccountName": res.account[1],
                "Tags": json.dumps(res.tags, sort_keys=True),
            }
        )

    @staticmethod
    def service(res: Resource) -> tuple[str, str, str]:
        if res.provider == "Microsoft":
            return {
                "vm": ("Virtual Machines", "Compute", "Virtual Machine"),
                "nodepool": ("Virtual Machines", "Compute", "Virtual Machine Scale Set"),
                "disk": ("Storage", "Storage", "Disk"),
                "blob": ("Storage", "Storage", "Storage Account"),
                "egress": ("Bandwidth", "Networking", "Storage Account"),
                "logs": ("Log Analytics", "Management and Governance", "Log Analytics Workspace"),
            }[res.kind]
        return {
            "vm": ("Amazon Elastic Compute Cloud", "Compute", "Instance"),
            "ebs": ("Amazon Elastic Compute Cloud", "Storage", "Volume"),
            "s3": ("Amazon Simple Storage Service", "Storage", "Bucket"),
            "egress": ("Amazon Simple Storage Service", "Storage", "Bucket"),
            "logs": ("Amazon CloudWatch", "Management and Governance", "Log Group"),
        }[res.kind]

    def metrics(self, res: Resource, i: int, d: dt.date) -> None:
        rng = self.rng
        row: dict[str, Any] = {"date": str(d), "ResourceId": res.resource_id}
        if res.kind in ("vm", "nodepool"):
            if res.schedule == "workhours" and d.weekday() >= 5:
                return  # switched off: no metrics
            mean, p95 = res.cpu
            if res.schedule == "nightly" and is_month_end(d):
                p95 = min(98.0, p95 * 1.1)
            row["cpu_avg"] = round(float(np.clip(rng.normal(mean, max(0.4, mean * 0.08)), 0, 100)), 2)
            row["cpu_p95"] = round(float(np.clip(rng.normal(p95, max(0.8, p95 * 0.07)), 0, 100)), 2)
            row["mem_p95"] = round(float(np.clip(rng.normal(res.mem_p95, 3), 0, 100)), 2)
        elif res.kind in ("disk", "ebs"):
            vm = self.inv.get(res.extra.get("vm", ""))
            row["attached_to"] = vm.resource_id if (vm and res.attached and self.alive(vm, i)) else ""
        elif res.kind in ("blob", "s3"):
            gb = res.gb + res.growth_gb_day * (i - res.start_day)
            row["read_gb"] = round(gb * res.access_ratio / MONTH_DAYS * float(rng.lognormal(0, 0.35)), 3)
        else:
            return
        self.metric_rows.append(row)

    def alive(self, res: Resource, i: int) -> bool:
        return res.start_day <= i and (res.end_day is None or i <= res.end_day)

    def run(self) -> Dataset:
        for i in range(DAYS):
            d = START + dt.timedelta(days=i)
            for res in self.inv.values():
                if not self.alive(res, i):
                    continue
                self.emit(res, i, d)
                self.metrics(res, i, d)
        focus = pd.DataFrame(self.rows)
        metrics = pd.DataFrame(self.metric_rows)
        changes = [
            {"what": "customs service launch (planned)", "start": str(START + dt.timedelta(days=self.launch_day))},
            {"what": "pre-Christmas freight peak (business)", "start": "2025-11-24"},
        ]
        return Dataset(focus, metrics, self.labels(), self.truth_opportunities(), self.seed, changes)

    # ---------------------------------------------------------------- ground truth
    def labels(self) -> list[dict[str, Any]]:
        out = []
        for p in self.planted:
            by_day = self.excess_by_day[p.id]
            ongoing = p.end >= DAYS - 1
            tail = by_day[-7:].mean() if ongoing else 0.0
            out.append(
                {
                    "id": p.id,
                    "type": p.type,
                    "title": p.title,
                    "provider": p.provider,
                    "start": str(START + dt.timedelta(days=p.start)),
                    "end": str(START + dt.timedelta(days=p.end)),
                    "ongoing": ongoing,
                    "root_dimension": p.root_dimension,
                    "root_values": p.root_values,
                    "resources": sorted({self.inv[n].resource_id for n in p.resources}),
                    "true_excess_to_date": round(self.excess[p.id], 2),
                    "true_monthly_projection": round(float(tail) * MONTH_DAYS, 2),
                }
            )
        return out

    def _vm_monthly(self, res: Resource, size: str | None = None) -> float:
        return self.prices.vm(res.provider, res.region, size or res.size)["ondemand_hourly"] * true_monthly_hours(res)

    def truth_opportunities(self) -> list[dict[str, Any]]:
        last = DAYS - 1
        alive = [r for r in self.inv.values() if self.alive(r, last)]
        opps: list[dict[str, Any]] = []
        attached_storage: dict[str, float] = {}
        for r in alive:
            if r.kind == "disk" and r.extra.get("vm"):
                attached_storage[r.extra["vm"]] = self.prices.azure()["disk_month"][r.size]
            if r.kind == "ebs" and r.extra.get("vm"):
                attached_storage[r.extra["vm"]] = r.gb * self.prices.aws()["ebs_gp3_gb_month"]
        for r in alive:
            if r.kind == "vm":
                age = last - r.start_day + 1
                idle = r.cpu[0] < 3.0
                if idle:
                    opps.append(
                        {
                            "type": "idle",
                            "resource_id": r.resource_id,
                            "monthly_saving": round(self._vm_monthly(r) + attached_storage.get(r.name, 0.0), 2),
                        }
                    )
                    continue
                target = self.true_size(r)
                if target != r.size:
                    saving = self._vm_monthly(r) - self._vm_monthly(r, target)
                    opps.append(
                        {
                            "type": "rightsize",
                            "resource_id": r.resource_id,
                            "target": target,
                            "monthly_saving": round(saving, 2),
                        }
                    )
                    continue
                if r.schedule == "always" and r.tags.get("environment") == "prod" and age >= 60:
                    commit = self.prices.commitment_hourly(r.provider, r.region, r.size)
                    if commit:
                        od = self.prices.vm(r.provider, r.region, r.size)["ondemand_hourly"]
                        opps.append(
                            {
                                "type": "commitment",
                                "resource_id": r.resource_id,
                                "offer": commit[0],
                                "monthly_saving": round((od - commit[1]) * HOURS_PER_MONTH, 2),
                            }
                        )
            elif r.kind == "nodepool":
                commit = self.prices.commitment_hourly(r.provider, r.region, r.size)
                assert commit
                od = self.prices.vm(r.provider, r.region, r.size)["ondemand_hourly"]
                opps.append(
                    {
                        "type": "commitment",
                        "resource_id": r.resource_id,
                        "offer": commit[0],
                        "monthly_saving": round(r.extra["min"] * (od - commit[1]) * HOURS_PER_MONTH, 2),
                    }
                )
            elif r.kind in ("disk", "ebs") and not r.attached:
                monthly = (
                    self.prices.azure()["disk_month"][r.size]
                    if r.kind == "disk"
                    else r.gb * self.prices.aws()["ebs_gp3_gb_month"]
                )
                opps.append({"type": "idle", "resource_id": r.resource_id, "monthly_saving": round(monthly, 2)})
            elif r.kind in ("blob", "s3") and r.access_ratio < 0.1:
                gb = r.gb + r.growth_gb_day * (last - r.start_day)
                if r.kind == "blob":
                    p = self.prices.azure(r.region)["blob"]
                    hot, cool, retr = p["hot_gb_month"], p["cool_gb_month"], p["cool_retrieval_gb"]
                else:
                    p = self.prices.aws(r.region)["s3"]
                    hot, cool, retr = p["standard_gb_month"], p["standard_ia_gb_month"], p["ia_retrieval_gb"]
                saving = gb * (hot - cool) - gb * r.access_ratio * retr
                opps.append({"type": "tiering", "resource_id": r.resource_id, "monthly_saving": round(saving, 2)})
            if r.kind != "egress" and any(not r.tags.get(k) for k in ("team", "environment", "project")):
                opps.append({"type": "missing_tags", "resource_id": r.resource_id, "monthly_saving": 0.0})
        return opps

    def true_size(self, r: Resource) -> str:
        """Smallest size in the family whose projected p95 CPU and memory stay under the targets."""
        sizes = self.prices.vm_sizes(r.provider, r.region)
        cur = sizes[r.size]
        need_cpu = r.cpu[1] / 100 * cur["vcpu"]
        need_mem = r.mem_p95 / 100 * cur["memory_gb"]
        family = family_of(r.provider, r.size)
        for s in family[: family.index(r.size) + 1]:  # only ever smaller (or the same)
            spec = sizes[s]
            if need_cpu / spec["vcpu"] * 100 <= CPU_TARGET and need_mem / spec["memory_gb"] * 100 <= MEM_TARGET:
                return s
        return r.size


def generate(seed: int = 0, prices: PriceBook | None = None) -> Dataset:
    return Simulator(seed, prices).run()
