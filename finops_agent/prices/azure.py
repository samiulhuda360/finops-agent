"""Azure Retail Prices API client (public, no key): https://prices.azure.com/api/retail/prices

Only the meters this project prices are fetched. Each raw API page is cached on disk, so a refresh that is
interrupted resumes without repeating requests.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"  # this version adds the savingsPlan prices to each VM item

# Sizes used by the invented company. The API has no vCPU or memory figures, so these come from the
# published size tables (Dsv5, Esv5 and Bs series).
VM_SPECS: dict[str, tuple[int, int]] = {
    "Standard_B2s": (2, 4),
    "Standard_B2ms": (2, 8),
    "Standard_D2s_v5": (2, 8),
    "Standard_D4s_v5": (4, 16),
    "Standard_D8s_v5": (8, 32),
    "Standard_D16s_v5": (16, 64),
    "Standard_E2s_v5": (2, 16),
    "Standard_E4s_v5": (4, 32),
    "Standard_E8s_v5": (8, 64),
}
DISKS = ["P10", "P15", "P20", "P30"]


def _get(url: str, cache_dir: Path) -> dict[str, Any]:
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = cache_dir / f"azure-{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": "finops-agent/1.0"})
    for wait in (0, 5, 15, 30, 60):
        time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:  # the API throttles bursts with 429
            if e.code != 429 or wait == 60:
                raise
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def query(filter_: str, cache_dir: Path) -> list[dict[str, Any]]:
    """All items matching an OData filter, following NextPageLink."""
    url: str | None = f"{API}?" + urllib.parse.urlencode({"api-version": API_VERSION, "$filter": filter_})
    items: list[dict[str, Any]] = []
    while url:
        page = _get(url, cache_dir)
        items += page.get("Items", [])
        url = page.get("NextPageLink")
    return items


def _first_paid_tier(items: list[dict[str, Any]]) -> float:
    """Tiered meters list a free tier first; the first non-zero tier is the marginal price at small volumes."""
    paid = sorted((i for i in items if i["retailPrice"] > 0), key=lambda i: i["tierMinimumUnits"])
    if not paid:
        raise ValueError("no paid tier found")
    return float(paid[0]["retailPrice"])


def vm_prices(region: str, cache_dir: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    skus = " or ".join(f"armSkuName eq '{s}'" for s in VM_SPECS)
    all_items = query(f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and ({skus})", cache_dir)
    for sku, (vcpu, mem) in VM_SPECS.items():
        linux = [
            i
            for i in all_items
            if i["armSkuName"] == sku
            if "Windows" not in i["productName"] and "Spot" not in i["skuName"] and "Low Priority" not in i["skuName"]
        ]
        od = [i for i in linux if i["type"] == "Consumption"]
        ri = [i for i in linux if i["type"] == "Reservation" and i.get("reservationTerm") == "1 Year"]
        if not od:
            continue
        entry: dict[str, float] = {"ondemand_hourly": float(od[0]["retailPrice"]), "vcpu": vcpu, "memory_gb": mem}
        for sp in od[0].get("savingsPlan") or []:
            if sp.get("term") == "1 Year":
                entry["savings_plan_1y_hourly"] = float(sp["retailPrice"])
        if ri:
            # Reservation items carry the price of the whole term, although the unit says "1 Hour".
            entry["reservation_1y_total"] = float(ri[0]["retailPrice"])
            entry["reservation_1y_hourly"] = round(float(ri[0]["retailPrice"]) / 8760, 6)
        out[sku] = entry
    return out


def region_prices(region: str, cache_dir: Path, full: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {"vm": vm_prices(region, cache_dir)}
    if not full:
        return data
    disks = query(
        f"serviceName eq 'Storage' and armRegionName eq '{region}' and productName eq 'Premium SSD Managed Disks'",
        cache_dir,
    )
    data["disk_month"] = {
        d: float(next(i["retailPrice"] for i in disks if i["meterName"] == f"{d} LRS Disk")) for d in DISKS
    }
    blob = query(
        f"armRegionName eq '{region}' and productName eq 'General Block Blob v2' "
        "and (skuName eq 'Hot LRS' or skuName eq 'Cool LRS')",
        cache_dir,
    )

    def meter(name: str) -> float:
        return _first_paid_tier([i for i in blob if i["meterName"] == name])

    data["blob"] = {
        "hot_gb_month": meter("Hot LRS Data Stored"),
        "cool_gb_month": meter("Cool LRS Data Stored"),
        "cool_retrieval_gb": meter("Cool Data Retrieval"),
        "hot_read_10k": meter("Hot Read Operations"),
        "cool_read_10k": meter("Cool Read Operations"),
    }
    logs = query(f"armRegionName eq '{region}' and serviceName eq 'Log Analytics'", cache_dir)
    data["log_ingestion_gb"] = _first_paid_tier([i for i in logs if i["meterName"] == "Analytics Logs Data Ingestion"])
    bw = query(f"serviceName eq 'Bandwidth' and armRegionName eq '{region}'", cache_dir)
    internet = [
        i
        for i in bw
        if i["productName"] == "Bandwidth - Routing Preference: Internet"
        and i["meterName"] == "Standard Data Transfer Out"
    ]
    data["egress_gb"] = _first_paid_tier(internet)
    inter = [i for i in bw if i["meterName"] == "Standard Inter-Region Data Transfer"]
    data["inter_region_gb"] = _first_paid_tier(inter)
    return data
