"""Planted anomalies: the labelled cost incidents the detector is evaluated against.

Each seed places ten incidents of eight kinds at random dates (after a 75-day warm-up, so a baseline exists)
and with random sizes and durations. A label records the true start, the true end (or "ongoing"), the
resources involved and the root cause in the same (dimension, values) form the drill-down reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .company import AW, AWS_DATA, AWS_DEV, AZ, AZ_NONPROD, AZ_PROD, Resource
from .company import AWS_PROD as AWS_PROD_ACCT

WARMUP = 75


@dataclass
class Effect:
    resource: str
    kind: str  # "mult" multiplies usage, "min_nodes" raises an autoscaler floor, "ri_end" ends a reservation
    start: int
    end: int
    value: float = 1.0


@dataclass
class Planted:
    id: str
    type: str
    title: str
    provider: str
    start: int
    end: int  # inclusive day index; == last day means still running at the end of the data
    root_dimension: str
    root_values: list[str]
    resources: list[str] = field(default_factory=list)
    new_resources: list[Resource] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)


def plant(rng: np.random.Generator, days: int, inv: dict[str, Resource]) -> list[Planted]:
    last = days - 1
    out: list[Planted] = []

    def start_day() -> int:
        return int(rng.integers(WARMUP, days - 12))

    def until(start: int, lo: int, hi: int) -> int:
        return min(last, start + int(rng.integers(lo, hi + 1)) - 1)

    # 1-2. Forgotten dev VMs, one per cloud: created for a test and left running around the clock.
    for n, (provider, account, region, sizes) in enumerate(
        [
            ("AWS", AWS_DEV, AW, ["m6i.2xlarge", "r6i.xlarge", "m6i.xlarge"]),
            ("Microsoft", AZ_NONPROD, AZ, ["Standard_D8s_v5", "Standard_E4s_v5", "Standard_D4s_v5"]),
        ]
    ):
        s = start_day()
        e = until(s, 25, 120)
        size = str(rng.choice(sizes))
        if provider == "AWS":
            name = f"i-0lt{n}{rng.integers(10**11, 10**12):x}"[:19]
            label = "loadtest-ml-tmp"
        else:
            name = f"vm-ml-loadtest-{rng.integers(10, 99)}"
            label = name
        vm = Resource(
            name,
            provider,
            "vm",
            account,
            region,
            size,
            {"team": "ml", "environment": "dev", "project": "routing"},
            cpu=(1.5, 4.0),
            mem_p95=10,
            start_day=s,
            end_day=e,
            extra={"label": label},
        )
        out.append(
            Planted(
                f"A{len(out) + 1:02d}",
                "forgotten_dev_vm",
                f"Dev VM {label} ({size}) left running",
                provider,
                s,
                e,
                "ResourceId",
                [vm.resource_id],
                [vm.name],
                new_resources=[vm],
            )
        )

    # 3-4. Egress spikes on a public bucket / storage account (a partner re-downloading an export).
    for target in ("tw-media-exports-egress", "stwebassetsprod-egress"):
        res = inv[target]
        s = start_day()
        e = until(s, 2, 4)
        mult = float(rng.uniform(8, 30))
        out.append(
            Planted(
                f"A{len(out) + 1:02d}",
                "egress_spike",
                f"Data egress spike on {res.parent} (x{mult:.0f})",
                res.provider,
                s,
                e,
                "ResourceId",
                [res.resource_id],
                [res.name],
                effects=[Effect(target, "mult", s, e, mult)],
            )
        )

    # 5-6. Log ingestion explosions (debug logging left on after an incident).
    for target in ("law-acme-prod", "/acme/prod/app"):
        res = inv[target]
        s = start_day()
        e = until(s, 5, 16)
        mult = float(rng.uniform(4, 9))
        out.append(
            Planted(
                f"A{len(out) + 1:02d}",
                "log_ingestion_explosion",
                f"Log ingestion on {res.name} up x{mult:.0f}",
                res.provider,
                s,
                e,
                "ResourceId",
                [res.resource_id],
                [res.name],
                effects=[Effect(target, "mult", s, e, mult)],
            )
        )

    # 7. Misconfigured autoscaler: the AKS node pool minimum set far too high.
    res = inv["aks-prod-nodepool1"]
    s = start_day()
    e = until(s, 8, 24)
    floor = float(rng.integers(9, 14))
    out.append(
        Planted(
            f"A{len(out) + 1:02d}",
            "autoscaler_misconfig",
            f"AKS node pool minimum raised to {floor:.0f} nodes",
            "Microsoft",
            s,
            e,
            "ResourceId",
            [res.resource_id],
            [res.name],
            effects=[Effect(res.name, "min_nodes", s, e, floor)],
        )
    )

    # 8. Untagged resource sprawl: experiment instances launched without tags over about two weeks.
    s = start_day()
    count = int(rng.integers(8, 15))
    ramp = int(rng.integers(8, 16))
    e = until(s, 45, 160)
    sprawl = []
    for k in range(count):
        size = str(rng.choice(["t3.large", "m6i.large", "m6i.xlarge"]))
        sprawl.append(
            Resource(
                f"i-0exp{k:02d}{rng.integers(10**9, 10**10):x}"[:19],
                "AWS",
                "vm",
                AWS_DATA,
                AW,
                size,
                {},
                cpu=(25, 50),
                start_day=s + (k * ramp) // count,
                end_day=e,
                extra={"label": f"exp-{k:02d}"},
            )
        )
    out.append(
        Planted(
            f"A{len(out) + 1:02d}",
            "untagged_sprawl",
            f"{count} untagged experiment instances launched",
            "AWS",
            s,
            e,
            "Tags.team",
            ["(untagged)"],
            [x.name for x in sprawl],
            new_resources=sprawl,
        )
    )

    # 9. A reservation expiring: the two SQL VMs fall back to pay-as-you-go prices.
    s = start_day()
    vms = [inv["vm-sql-prod-01"], inv["vm-sql-prod-02"]]
    out.append(
        Planted(
            f"A{len(out) + 1:02d}",
            "reservation_expiry",
            "Reservation ri-sql-prod-2025 expired; SQL VMs now pay-as-you-go",
            "Microsoft",
            s,
            last,
            "ResourceId",
            sorted(v.resource_id for v in vms),
            [v.name for v in vms],
            effects=[Effect(v.name, "ri_end", s, last) for v in vms],
        )
    )

    # 10. Region mistake: a routing stack deployed to West Europe instead of New Zealand North.
    s = start_day()
    e = until(s, 21, 90)
    wrong = "westeurope"
    tags = {"team": "platform", "environment": "prod", "project": "routing"}
    stack = [
        Resource(
            "vm-routing-weu-01",
            "Microsoft",
            "vm",
            AZ_PROD,
            wrong,
            "Standard_D4s_v5",
            dict(tags),
            cpu=(30, 55),
            start_day=s,
            end_day=e,
        ),
        Resource(
            "vm-routing-weu-02",
            "Microsoft",
            "vm",
            AZ_PROD,
            wrong,
            "Standard_D4s_v5",
            dict(tags),
            cpu=(30, 55),
            start_day=s,
            end_day=e,
        ),
        Resource(
            "vm-routing-weu-db",
            "Microsoft",
            "vm",
            AZ_PROD,
            wrong,
            "Standard_E4s_v5",
            dict(tags),
            cpu=(35, 60),
            start_day=s,
            end_day=e,
        ),
        Resource(
            "stroutingweu",
            "Microsoft",
            "blob",
            AZ_PROD,
            wrong,
            gb=900,
            growth_gb_day=5,
            access_ratio=1.0,
            tags=dict(tags),
            start_day=s,
            end_day=e,
        ),
        Resource(
            "stroutingweu-xregion",
            "Microsoft",
            "egress",
            AZ_PROD,
            wrong,
            daily_gb=float(rng.uniform(150, 400)),
            parent="stroutingweu",
            tags=dict(tags),
            start_day=s,
            end_day=e,
            extra={"inter_region": True},
        ),
    ]
    out.append(
        Planted(
            f"A{len(out) + 1:02d}",
            "region_mistake",
            "Routing stack deployed to West Europe",
            "Microsoft",
            s,
            e,
            "RegionId",
            [wrong],
            [x.name for x in stack],
            new_resources=stack,
        )
    )
    return out


def planned_launch(rng: np.random.Generator, days: int) -> tuple[int, list[Resource]]:
    """A legitimate, planned change (not an anomaly): the customs-clearance service goes live on AWS.

    It is not labelled, so a detector that flags it scores a false positive, as it would in real life
    without a change calendar.
    """
    s = int(rng.integers(WARMUP, days - 30))
    tags = {"team": "web", "environment": "prod", "project": "customs"}
    stack = [
        Resource(
            f"i-0cus{k}{rng.integers(10**10, 10**11):x}"[:19],
            "AWS",
            "vm",
            AWS_PROD_ACCT,
            AW,
            "m6i.xlarge",
            dict(tags),
            cpu=(35, 58),
            start_day=s,
            extra={"label": f"customs-api-{k}"},
        )
        for k in (1, 2)
    ]
    stack.append(
        Resource(
            "tw-customs-docs",
            "AWS",
            "s3",
            AWS_PROD_ACCT,
            AW,
            gb=400,
            growth_gb_day=6,
            access_ratio=1.2,
            tags=dict(tags),
            start_day=s,
        )
    )
    return s, stack


__all__ = ["AZ_PROD", "Effect", "Planted", "plant", "planned_launch"]
