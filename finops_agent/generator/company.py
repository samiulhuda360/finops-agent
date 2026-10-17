"""The invented company: Acme Freight, an Auckland logistics firm running on Azure and AWS.

The inventory is fixed; each seed varies the noise and the timing and size of the planted anomalies.
Every resource carries its true behaviour (schedule, utilisation, access pattern), from which the
generator derives both the bill and the ground truth for the savings recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AZ_PROD = ("3f1c9a52-7d4e-4b8a-9c61-5e2d8f0a1b37", "tw-prod")
AZ_NONPROD = ("8a27d6e4-1c95-4f03-b7a8-2e6c4d9f5013", "tw-nonprod")
AWS_PROD = ("402817365519", "tw-aws-prod")
AWS_DATA = ("402817365520", "tw-aws-data")
AWS_DEV = ("402817365521", "tw-aws-dev")
AZURE_BILLING = ("7104-2286-5531", "Acme Freight (Azure)")
AWS_BILLING = ("402817365500", "Acme Freight (AWS)")

AZ = "newzealandnorth"
AW = "ap-southeast-6"
REGION_NAMES = {
    "newzealandnorth": "New Zealand North",
    "westeurope": "West Europe",
    "ap-southeast-6": "Asia Pacific (New Zealand)",
}


@dataclass
class Resource:
    name: str
    provider: str  # FOCUS ServiceProviderName: "Microsoft" or "AWS"
    kind: str  # vm, nodepool, disk, ebs, blob, s3, egress, logs
    account: tuple[str, str]
    region: str
    size: str = ""  # VM size or disk tier
    tags: dict[str, str] = field(default_factory=dict)
    schedule: str = "always"  # always, workhours, nightly
    cpu: tuple[float, float] = (30.0, 55.0)  # true daily mean and p95 CPU %
    mem_p95: float = 50.0
    gb: float = 0.0  # stored GB at the start (storage) or provisioned GB (EBS)
    growth_gb_day: float = 0.0
    access_ratio: float = 0.5  # GB read per month / GB stored
    daily_gb: float = 0.0  # egress or log GB per day at traffic factor 1
    attached: bool = True
    parent: str = ""  # the storage resource an egress meter belongs to
    reservation: str = ""  # commitment id covering this VM (until the planted expiry)
    start_day: int = 0
    end_day: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def resource_id(self) -> str:
        if self.provider == "Microsoft":
            sub = self.account[0]
            rg = {
                "vm": "compute",
                "nodepool": "aks",
                "disk": "compute",
                "blob": "storage",
                "egress": "storage",
                "logs": "monitoring",
            }.get(self.kind, "net")
            rtype = {
                "vm": "Microsoft.Compute/virtualMachines",
                "nodepool": "Microsoft.Compute/virtualMachineScaleSets",
                "disk": "Microsoft.Compute/disks",
                "blob": "Microsoft.Storage/storageAccounts",
                "egress": "Microsoft.Storage/storageAccounts",
                "logs": "Microsoft.OperationalInsights/workspaces",
            }[self.kind]
            name = self.parent or self.name
            return f"/subscriptions/{sub}/resourceGroups/rg-{rg}-{self.region}/providers/{rtype}/{name}".lower()
        acct = self.account[0]
        if self.kind in ("s3", "egress"):
            return f"arn:aws:s3:::{self.parent or self.name}"
        if self.kind == "logs":
            return f"arn:aws:logs:{self.region}:{acct}:log-group:{self.name}"
        if self.kind == "ebs":
            return f"arn:aws:ec2:{self.region}:{acct}:volume/{self.name}"
        return f"arn:aws:ec2:{self.region}:{acct}:instance/{self.name}"


def _t(team: str, env: str, project: str) -> dict[str, str]:
    return {"team": team, "environment": env, "project": project}


def inventory() -> list[Resource]:
    r: list[Resource] = []
    # ---- Azure, production
    for i in (1, 2, 3):
        r.append(
            Resource(
                f"vm-web-prod-0{i}",
                "Microsoft",
                "vm",
                AZ_PROD,
                AZ,
                "Standard_D4s_v5",
                _t("web", "prod", "tracking"),
                cpu=(34, 58),
                mem_p95=55,
            )
        )
    for i in (1, 2):
        r.append(
            Resource(
                f"vm-api-prod-0{i}",
                "Microsoft",
                "vm",
                AZ_PROD,
                AZ,
                "Standard_D8s_v5",
                _t("platform", "prod", "routing"),
                cpu=(12, 24),
                mem_p95=30,
            )
        )
    for i in (1, 2):
        r.append(
            Resource(
                f"vm-sql-prod-0{i}",
                "Microsoft",
                "vm",
                AZ_PROD,
                AZ,
                "Standard_D16s_v5",
                _t("data", "prod", "billing"),
                cpu=(45, 72),
                mem_p95=78,
                reservation="ri-sql-prod-2025",
            )
        )
    r.append(
        Resource(
            "vm-etl-prod-01",
            "Microsoft",
            "vm",
            AZ_PROD,
            AZ,
            "Standard_E8s_v5",
            _t("data", "prod", "analytics"),
            cpu=(40, 74),
            mem_p95=72,
        )
    )
    r.append(
        Resource(
            "vm-jump-01",
            "Microsoft",
            "vm",
            AZ_PROD,
            AZ,
            "Standard_B2s",
            _t("platform", "prod", "ops"),
            cpu=(8, 30),
            mem_p95=60,
        )
    )
    r.append(
        Resource(
            "vm-reporting-old-01",
            "Microsoft",
            "vm",
            AZ_PROD,
            AZ,
            "Standard_D4s_v5",
            _t("data", "staging", "analytics"),
            cpu=(1.0, 2.5),
            mem_p95=12,
        )
    )
    r.append(
        Resource(
            "aks-prod-nodepool1",
            "Microsoft",
            "nodepool",
            AZ_PROD,
            AZ,
            "Standard_D4s_v5",
            _t("platform", "prod", "tracking"),
            cpu=(48, 70),
            mem_p95=65,
            extra={"min": 3, "max": 10},
        )
    )
    # ---- Azure, non-production (dev machines are shut down outside working hours)
    r.append(
        Resource(
            "vm-dev-web-01",
            "Microsoft",
            "vm",
            AZ_NONPROD,
            AZ,
            "Standard_D2s_v5",
            _t("web", "dev", "tracking"),
            schedule="workhours",
            cpu=(20, 45),
        )
    )
    r.append(
        Resource(
            "vm-dev-api-01",
            "Microsoft",
            "vm",
            AZ_NONPROD,
            AZ,
            "Standard_D4s_v5",
            _t("platform", "dev", "routing"),
            schedule="workhours",
            cpu=(25, 50),
        )
    )
    r.append(
        Resource(
            "vm-dev-data-01",
            "Microsoft",
            "vm",
            AZ_NONPROD,
            AZ,
            "Standard_E4s_v5",
            _t("data", "dev", "analytics"),
            schedule="workhours",
            cpu=(30, 60),
            mem_p95=70,
        )
    )
    r.append(
        Resource(
            "vm-ml-train-dev-01",
            "Microsoft",
            "vm",
            AZ_NONPROD,
            AZ,
            "Standard_D8s_v5",
            _t("ml", "dev", "routing"),
            schedule="workhours",
            cpu=(6, 11),
            mem_p95=20,
        )
    )
    # ---- Azure disks
    for vm in [x for x in r if x.kind == "vm"]:
        tier = "P30" if "sql" in vm.name else ("P20" if vm.tags.get("environment") == "prod" else "P10")
        r.append(
            Resource(
                f"disk-{vm.name.removeprefix('vm-')}-os",
                "Microsoft",
                "disk",
                vm.account,
                AZ,
                tier,
                dict(vm.tags),
                parent="",
                extra={"vm": vm.name},
            )
        )
    r.append(
        Resource(
            "disk-orphan-migration-01",
            "Microsoft",
            "disk",
            AZ_PROD,
            AZ,
            "P20",
            _t("platform", "prod", "ops"),
            attached=False,
        )
    )
    r.append(
        Resource(
            "disk-orphan-test-02",
            "Microsoft",
            "disk",
            AZ_NONPROD,
            AZ,
            "P10",
            _t("web", "dev", "tracking"),
            attached=False,
        )
    )
    # ---- Azure storage, bandwidth and logs
    r.append(
        Resource(
            "stwebassetsprod",
            "Microsoft",
            "blob",
            AZ_PROD,
            AZ,
            gb=2100,
            growth_gb_day=3,
            access_ratio=2.5,
            tags=_t("web", "prod", "tracking"),
        )
    )
    r.append(
        Resource(
            "starchivelogsprod",
            "Microsoft",
            "blob",
            AZ_PROD,
            AZ,
            gb=18000,
            growth_gb_day=20,
            access_ratio=0.01,
            tags=_t("platform", "prod", "ops"),
        )
    )
    r.append(
        Resource(
            "stbackupsprod",
            "Microsoft",
            "blob",
            AZ_PROD,
            AZ,
            gb=6200,
            growth_gb_day=8,
            access_ratio=0.02,
            tags=_t("data", "prod", "billing"),
        )
    )
    r.append(
        Resource(
            "stwebassetsprod-egress",
            "Microsoft",
            "egress",
            AZ_PROD,
            AZ,
            daily_gb=60,
            parent="stwebassetsprod",
            tags=_t("web", "prod", "tracking"),
        )
    )
    r.append(
        Resource("law-acme-prod", "Microsoft", "logs", AZ_PROD, AZ, daily_gb=6, tags=_t("platform", "prod", "ops"))
    )
    # ---- AWS, production
    for i in (1, 2, 3, 4):
        r.append(
            Resource(
                f"i-0app{i}a7c3e91f0d2{i}",
                "AWS",
                "vm",
                AWS_PROD,
                AW,
                "m6i.xlarge",
                _t("web", "prod", "tracking"),
                cpu=(32, 55),
                mem_p95=58,
                extra={"label": f"app-prod-{i}"},
            )
        )
    for i in (1, 2):
        r.append(
            Resource(
                f"i-0wrk{i}b81d24c6e9{i}",
                "AWS",
                "vm",
                AWS_PROD,
                AW,
                "c6i.xlarge",
                _t("platform", "prod", "routing"),
                cpu=(40, 66),
                mem_p95=50,
                extra={"label": f"worker-{i}"},
            )
        )
    r.append(
        Resource(
            "i-0ana1c5e7f3a9b40",
            "AWS",
            "vm",
            AWS_DATA,
            AW,
            "r6i.2xlarge",
            _t("data", "prod", "analytics"),
            cpu=(10, 20),
            mem_p95=30,
            extra={"label": "analytics-1"},
        )
    )
    r.append(
        Resource(
            "i-0bat1d9e2a6c7f51",
            "AWS",
            "vm",
            AWS_DATA,
            AW,
            "m6i.2xlarge",
            _t("data", "prod", "billing"),
            schedule="nightly",
            cpu=(55, 85),
            mem_p95=60,
            extra={"label": "batch-1"},
        )
    )
    r.append(
        Resource(
            "i-0dev1e3f5a7b9c62",
            "AWS",
            "vm",
            AWS_DEV,
            AW,
            "t3.large",
            _t("web", "dev", "tracking"),
            schedule="workhours",
            cpu=(15, 40),
        )
    )
    r.append(
        Resource(
            "i-0dev2f4a6b8c0d73",
            "AWS",
            "vm",
            AWS_DEV,
            AW,
            "m6i.large",
            _t("platform", "dev", "routing"),
            schedule="workhours",
            cpu=(20, 45),
        )
    )
    r.append(
        Resource(
            "i-0cir1a2b3c4d5e84",
            "AWS",
            "vm",
            AWS_DEV,
            AW,
            "c6i.large",
            _t("platform", "dev", "ops"),
            cpu=(1.2, 3.0),
            mem_p95=15,
            extra={"label": "ci-runner-1"},
        )
    )
    r.append(
        Resource(
            "i-0tst9f8e7d6c5b95",
            "AWS",
            "vm",
            AWS_DEV,
            AW,
            "t3.medium",
            {},
            cpu=(18, 40),
            extra={"label": "test-instance"},
        )
    )
    # ---- AWS volumes
    for vm in [x for x in r if x.kind == "vm" and x.provider == "AWS"]:
        r.append(
            Resource(
                f"vol-{vm.name.removeprefix('i-')}",
                "AWS",
                "ebs",
                vm.account,
                AW,
                gb=200 if vm.tags.get("environment") == "prod" else 100,
                tags=dict(vm.tags),
                extra={"vm": vm.name},
            )
        )
    r.append(
        Resource(
            "vol-0orphan5c3d1e2f9",
            "AWS",
            "ebs",
            AWS_DATA,
            AW,
            gb=500,
            tags=_t("data", "prod", "analytics"),
            attached=False,
        )
    )
    # ---- AWS storage, transfer and logs
    r.append(
        Resource(
            "tw-media-exports",
            "AWS",
            "s3",
            AWS_PROD,
            AW,
            gb=3000,
            growth_gb_day=4,
            access_ratio=1.8,
            tags=_t("web", "prod", "tracking"),
        )
    )
    r.append(
        Resource(
            "tw-media-exports-egress",
            "AWS",
            "egress",
            AWS_PROD,
            AW,
            daily_gb=40,
            parent="tw-media-exports",
            tags=_t("web", "prod", "tracking"),
        )
    )
    r.append(
        Resource(
            "tw-data-lake",
            "AWS",
            "s3",
            AWS_DATA,
            AW,
            gb=25000,
            growth_gb_day=30,
            access_ratio=0.03,
            tags=_t("data", "prod", "analytics"),
        )
    )
    r.append(
        Resource(
            "tw-logs-archive",
            "AWS",
            "s3",
            AWS_PROD,
            AW,
            gb=8000,
            growth_gb_day=10,
            access_ratio=0.005,
            tags=_t("platform", "prod", "ops"),
        )
    )
    r.append(Resource("/acme/prod/app", "AWS", "logs", AWS_PROD, AW, daily_gb=10, tags=_t("platform", "prod", "ops")))
    return r
