"""Public list prices, from the Azure Retail Prices API and the AWS bulk price list files.

`snapshot.json` is a committed copy of a real fetch, so the generator, the recommender and CI run offline.
`finops-agent prices --refresh` fetches the same prices again and rewrites it.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import aws, azure

SNAPSHOT = Path(__file__).with_name("snapshot.json")
AZURE_HOME = "newzealandnorth"
AZURE_OTHER = "westeurope"  # the region of the planted region mistake
AWS_HOME = "ap-southeast-6"  # Asia Pacific (New Zealand)
HOURS_PER_MONTH = 730.0


def refresh(cache_dir: Path, out: Path = SNAPSHOT) -> dict[str, Any]:
    data = {
        "fetched": dt.date.today().isoformat(),
        "currency": "USD",
        "sources": {
            "azure": azure.API + f"?api-version={azure.API_VERSION}",
            "aws": aws.BASE + "/offers/v1.0/aws/{AmazonEC2,AmazonS3,AWSDataTransfer,AmazonCloudWatch}/current/ "
            "and /savingsPlan/v1.0/aws/AWSComputeSavingsPlan/current/",
        },
        "notes": [
            "Linux, pay-as-you-go list prices in USD. Tiered meters use the first paid tier.",
            "Azure reservation_1y_hourly is the 1-year term price divided by 8760 hours.",
            "AWS savings_plan_1y_hourly is the Linux rate under a 1-year, no-upfront Compute Savings Plan.",
        ],
        "azure": {
            AZURE_HOME: azure.region_prices(AZURE_HOME, cache_dir),
            AZURE_OTHER: azure.region_prices(AZURE_OTHER, cache_dir),
        },
        "aws": {AWS_HOME: aws.region_prices(AWS_HOME, cache_dir)},
    }
    out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    load.cache_clear()
    return data


@lru_cache(maxsize=1)
def load(path: Path = SNAPSHOT) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class PriceBook:
    """Typed lookups over the snapshot."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or load()

    @property
    def fetched(self) -> str:
        return str(self.data["fetched"])

    def vm(self, provider: str, region: str, size: str) -> dict[str, float]:
        if provider == "Microsoft":
            return dict(self.data["azure"][region]["vm"][size])
        return dict(self.data["aws"][region]["ec2"][size])

    def vm_sizes(self, provider: str, region: str) -> dict[str, dict[str, float]]:
        if provider == "Microsoft":
            return dict(self.data["azure"][region]["vm"])
        return dict(self.data["aws"][region]["ec2"])

    def azure(self, region: str = AZURE_HOME) -> dict[str, Any]:
        return dict(self.data["azure"][region])

    def aws(self, region: str = AWS_HOME) -> dict[str, Any]:
        return dict(self.data["aws"][region])

    def commitment_hourly(self, provider: str, region: str, size: str) -> tuple[str, float] | None:
        """The cheapest 1-year commitment rate for a VM size, and its name."""
        p = self.vm(provider, region, size)
        options = []
        if provider == "Microsoft":
            if "reservation_1y_hourly" in p:
                options.append(("1-year reservation", p["reservation_1y_hourly"]))
            if "savings_plan_1y_hourly" in p:
                options.append(("1-year savings plan", p["savings_plan_1y_hourly"]))
        elif p.get("savings_plan_1y_hourly"):
            options.append(("1-year Compute Savings Plan", p["savings_plan_1y_hourly"]))
        return min(options, key=lambda o: o[1]) if options else None
