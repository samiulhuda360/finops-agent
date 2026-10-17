"""AWS public bulk price list files (no key): https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/

The S3, data-transfer and CloudWatch files for one region are small (0.2 to 1.6 MB) and are cached whole.
The EC2 file (about 57 MB for ap-southeast-6) and the Compute Savings Plans file (about 92 MB) are large, so each
is streamed once with ijson and only the instance types and EBS volume type this project uses are kept; the
whole file never touches the disk. In the New Zealand region, shared-tenancy EC2 commitments are sold as
Savings Plans, so the 1-year no-upfront Compute Savings Plan rate is the commitment price used.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://pricing.us-east-1.amazonaws.com"
INSTANCE_TYPES = [
    "t3.medium",
    "t3.large",
    "m6i.large",
    "m6i.xlarge",
    "m6i.2xlarge",
    "m6i.4xlarge",
    "r6i.large",
    "r6i.xlarge",
    "r6i.2xlarge",
    "c6i.large",
    "c6i.xlarge",
]


def _open(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "finops-agent/1.0"})
    return urllib.request.urlopen(req, timeout=120)


def offer_url(service: str, region: str) -> str:
    with _open(f"{BASE}/offers/v1.0/aws/{service}/current/region_index.json") as resp:
        index = json.loads(resp.read().decode("utf-8"))
    return BASE + str(index["regions"][region]["currentVersionUrl"])


def small_offer(service: str, region: str, cache_dir: Path) -> dict[str, Any]:
    path = cache_dir / f"aws-{service}-{region}.json"
    if not path.exists():
        with _open(offer_url(service, region)) as resp:
            body = resp.read()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return json.loads(path.read_text(encoding="utf-8"))


def _ondemand(offer: dict[str, Any], sku: str) -> list[dict[str, Any]]:
    dims: list[dict[str, Any]] = []
    for term in offer["terms"]["OnDemand"].get(sku, {}).values():
        dims += list(term["priceDimensions"].values())
    return dims


def _paid(dims: list[dict[str, Any]]) -> float:
    paid = sorted((d for d in dims if float(d["pricePerUnit"]["USD"]) > 0), key=lambda d: float(d["beginRange"]))
    return float(paid[0]["pricePerUnit"]["USD"])


def _find(offer: dict[str, Any], **attrs: str) -> list[dict[str, Any]]:
    dims: list[dict[str, Any]] = []
    for sku, product in offer["products"].items():
        a = product["attributes"]
        if all(a.get(k) == v or (k == "usagetype" and str(a.get(k, "")).endswith(v)) for k, v in attrs.items()):
            dims += _ondemand(offer, sku)
    return dims


def s3_prices(region: str, cache_dir: Path) -> dict[str, float]:
    offer = small_offer("AmazonS3", region, cache_dir)
    return {
        "standard_gb_month": _paid(_find(offer, usagetype="-TimedStorage-ByteHrs", volumeType="Standard")),
        "standard_ia_gb_month": _paid(
            _find(offer, usagetype="-TimedStorage-SIA-ByteHrs", volumeType="Standard - Infrequent Access")
        ),
        "ia_retrieval_gb": _paid(_find(offer, usagetype="-Retrieval-SIA")),
        "get_per_1k": _paid(_find(offer, usagetype="-Requests-Tier2", group="S3-API-Tier2")) * 1000,
    }


def transfer_prices(region: str, cache_dir: Path) -> dict[str, float]:
    offer = small_offer("AWSDataTransfer", region, cache_dir)
    return {"egress_gb": _paid(_find(offer, usagetype="-DataTransfer-Out-Bytes", toLocation="External"))}


def cloudwatch_prices(region: str, cache_dir: Path) -> dict[str, float]:
    offer = small_offer("AmazonCloudWatch", region, cache_dir)
    return {
        "log_ingestion_gb": _paid(_find(offer, usagetype="-DataProcessing-Bytes", group="Ingested Logs")),
        "log_storage_gb_month": _paid(_find(offer, usagetype="-TimedStorage-ByteHrs")),
    }


def ec2_prices(region: str, cache_dir: Path) -> dict[str, Any]:
    """Stream the EC2 regional file and keep Linux on-demand, 1-year no-upfront standard RI and gp3 prices."""
    path = cache_dir / f"aws-AmazonEC2-{region}-extract.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    import ijson  # streamed parsing is only needed for this refresh

    wanted: dict[str, str] = {}  # sku -> instance type, or "gp3"
    specs: dict[str, dict[str, Any]] = {}
    ondemand: dict[str, float] = {}
    reserved: dict[str, float] = {}

    def on_product(sku: str, product: dict[str, Any]) -> None:
        a = product.get("attributes", {})
        fam = product.get("productFamily")
        if (
            fam == "Compute Instance"
            and a.get("instanceType") in INSTANCE_TYPES
            and a.get("operatingSystem") == "Linux"
            and a.get("tenancy") == "Shared"
            and a.get("preInstalledSw") == "NA"
            and a.get("capacitystatus") == "Used"
            and a.get("licenseModel") == "No License required"
        ):
            wanted[sku] = a["instanceType"]
            specs[a["instanceType"]] = {"vcpu": int(a["vcpu"]), "memory_gb": float(str(a["memory"]).split()[0])}
        elif fam == "Storage" and a.get("volumeApiName") == "gp3":
            wanted[sku] = "gp3"

    def on_terms(kind: str, sku: str, terms: dict[str, Any]) -> None:
        for t in terms.values():
            ta = t.get("termAttributes", {})
            for d in t["priceDimensions"].values():
                price = float(d["pricePerUnit"]["USD"])
                if kind == "OnDemand":
                    ondemand[wanted[sku]] = price
                elif (
                    ta.get("LeaseContractLength") == "1yr"
                    and ta.get("PurchaseOption") == "No Upfront"
                    and ta.get("OfferingClass") == "standard"
                    and d["unit"] == "Hrs"
                ):
                    reserved[wanted[sku]] = price

    # One streamed pass: products come before terms in the file, so the SKUs to keep are known by the time
    # their terms arrive. Only the sub-objects of interest are built in memory.
    builder: Any = None
    current = ""
    skipping = ""
    with _open(offer_url("AmazonEC2", region)) as resp:
        for prefix, event, value in ijson.parse(resp, use_float=True):
            if skipping:
                if prefix == skipping and event == "end_map":
                    skipping = ""
                continue
            if builder is not None:
                builder.event(event, value)
                if prefix == current and event == "end_map":
                    parts = current.split(".")
                    if parts[0] == "products":
                        on_product(parts[1], builder.value)
                    else:
                        on_terms(parts[1], parts[2], builder.value)
                    builder = None
                continue
            if event != "start_map":
                continue
            parts = prefix.split(".")
            if (
                len(parts) == 2
                and parts[0] == "products"
                or (len(parts) == 3 and parts[0] == "terms" and parts[1] in ("OnDemand", "Reserved"))
            ):
                if parts[0] == "terms" and parts[2] not in wanted:
                    skipping = prefix
                    continue
                builder = ijson.ObjectBuilder()
                builder.event(event, value)
                current = prefix
    out: dict[str, Any] = {"instances": {}, "ebs_gp3_gb_month": ondemand.get("gp3")}
    for itype in INSTANCE_TYPES:
        if itype in ondemand:
            out["instances"][itype] = {
                "ondemand_hourly": ondemand[itype],
                "ri_1y_no_upfront_hourly": reserved.get(itype),
                **specs[itype],
            }
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def savings_plan_rates(region: str, cache_dir: Path) -> dict[str, float]:
    """Linux hourly rates under a 1-year, no-upfront Compute Savings Plan, per instance type."""
    path = cache_dir / f"aws-ComputeSavingsPlan-{region}-extract.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    import ijson

    with _open(f"{BASE}/savingsPlan/v1.0/aws/AWSComputeSavingsPlan/current/region_index.json") as resp:
        index = json.loads(resp.read().decode("utf-8"))
    url = BASE + next(r["versionUrl"] for r in index["regions"] if r["regionCode"] == region)
    rates: dict[str, float] = {}
    with _open(url) as resp:
        for term in ijson.items(resp, "terms.savingsPlan.item"):
            if term.get("description") != "1 year No Upfront Compute Savings Plan":
                continue
            for r in term["rates"]:
                itype = r.get("discountedInstanceType")
                if (
                    itype in INSTANCE_TYPES
                    and r["discountedOperation"] == "RunInstances"
                    and "-BoxUsage:" in r["discountedUsageType"]
                ):
                    rates[itype] = float(r["discountedRate"]["price"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rates, indent=1), encoding="utf-8")
    return rates


def region_prices(region: str, cache_dir: Path) -> dict[str, Any]:
    ec2 = ec2_prices(region, cache_dir)
    sp = savings_plan_rates(region, cache_dir)
    for itype, entry in ec2["instances"].items():
        entry.pop("ri_1y_no_upfront_hourly", None)
        if itype in sp:
            entry["savings_plan_1y_hourly"] = sp[itype]
    return {
        "ec2": ec2["instances"],
        "ebs_gp3_gb_month": ec2["ebs_gp3_gb_month"],
        "s3": s3_prices(region, cache_dir),
        **transfer_prices(region, cache_dir),
        "cloudwatch": cloudwatch_prices(region, cache_dir),
    }
