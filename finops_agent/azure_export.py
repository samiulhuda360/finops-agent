"""Raw Azure Cost Management exports (amortized cost, EA/MCA column names) mapped to FOCUS columns.

Azure can export FOCUS directly; many teams still have the older "amortized cost" CSV exports, so those are
accepted too. `from_focus` produces that layout from FOCUS rows (the generator uses it to write a sample),
and `to_focus` maps it back; a test checks that the round trip keeps every value the analysis uses.
"""

from __future__ import annotations

import json
import re

import pandas as pd

AZURE_COLUMNS = [
    "BillingAccountId",
    "BillingAccountName",
    "BillingPeriodStartDate",
    "BillingPeriodEndDate",
    "Date",
    "SubscriptionId",
    "SubscriptionName",
    "ResourceGroup",
    "ResourceId",
    "ResourceLocation",
    "MeterCategory",
    "MeterSubCategory",
    "MeterName",
    "MeterId",
    "ProductName",
    "ConsumedService",
    "Quantity",
    "UnitOfMeasure",
    "PayGPrice",
    "UnitPrice",
    "CostInBillingCurrency",
    "BillingCurrencyCode",
    "ChargeType",
    "Frequency",
    "PricingModel",
    "BenefitId",
    "BenefitName",
    "Tags",
]

SERVICE_CATEGORY = {
    "Virtual Machines": "Compute",
    "Storage": "Storage",
    "Bandwidth": "Networking",
    "Log Analytics": "Management and Governance",
}
RESOURCE_TYPE = {
    "microsoft.compute/virtualmachines": "Virtual Machine",
    "microsoft.compute/virtualmachinescalesets": "Virtual Machine Scale Set",
    "microsoft.compute/disks": "Disk",
    "microsoft.storage/storageaccounts": "Storage Account",
    "microsoft.operationalinsights/workspaces": "Log Analytics Workspace",
}
PRICING_MODEL = {"OnDemand": "Standard", "Reservation": "Committed", "SavingsPlan": "Committed", "Spot": "Dynamic"}
COMMITMENT_TYPE = {"Reservation": "Reservation", "SavingsPlan": "Savings Plan"}
REGION_NAMES = {"newzealandnorth": "New Zealand North", "westeurope": "West Europe"}


def is_azure_export(df: pd.DataFrame) -> bool:
    return {"MeterCategory", "CostInBillingCurrency", "SubscriptionName"} <= set(df.columns)


def _iso(date: str) -> str:
    """Azure exports write dates as MM/DD/YYYY."""
    return pd.to_datetime(date, format="%m/%d/%Y").strftime("%Y-%m-%dT00:00:00Z")


def _tags(raw: object) -> str:
    """Azure writes tags as '"key": "value","key2": "value2"' (no braces); FOCUS wants a JSON object."""
    if not isinstance(raw, str) or not raw.strip():
        return "{}"
    text = raw.strip()
    if not text.startswith("{"):
        text = "{" + text + "}"
    try:
        return json.dumps(json.loads(text), sort_keys=True)
    except ValueError:
        return "{}"


def _rtype(resource_id: str) -> str:
    m = re.search(r"/providers/([^/]+/[^/]+)/", resource_id.lower())
    return RESOURCE_TYPE.get(m.group(1), "Other") if m else "Other"


def to_focus(raw: pd.DataFrame) -> pd.DataFrame:
    a = raw.copy()
    model = a["PricingModel"].fillna("OnDemand")
    qty = pd.to_numeric(a["Quantity"], errors="coerce").fillna(0.0)
    cost = pd.to_numeric(a["CostInBillingCurrency"], errors="coerce").fillna(0.0)
    payg = pd.to_numeric(a["PayGPrice"], errors="coerce").fillna(0.0)
    committed_usage = (model != "OnDemand") & (a["ChargeType"] == "Usage")
    start = a["Date"].map(_iso)
    end = (pd.to_datetime(start) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%dT00:00:00Z")
    region = a["ResourceLocation"].str.lower().str.replace(" ", "", regex=False)
    rid = a["ResourceId"].str.lower()
    out = pd.DataFrame(
        {
            "BillingAccountId": a["BillingAccountId"],
            "BillingAccountName": a["BillingAccountName"],
            "BillingCurrency": a["BillingCurrencyCode"],
            "BillingPeriodStart": a["BillingPeriodStartDate"].map(_iso),
            "BillingPeriodEnd": a["BillingPeriodEndDate"].map(_iso),
            "ChargePeriodStart": start,
            "ChargePeriodEnd": end,
            "ChargeCategory": a["ChargeType"].map({"Usage": "Usage", "Purchase": "Purchase"}).fillna("Adjustment"),
            "ChargeFrequency": a["Frequency"]
            .map({"UsageBased": "Usage-Based", "OneTime": "One-Time"})
            .fillna("Recurring"),
            "ChargeDescription": a["MeterName"] + " usage",
            # An amortized export spreads prepaid commitments over usage; the invoice shows 0 for those rows.
            "BilledCost": cost.where(~committed_usage, 0.0),
            "EffectiveCost": cost,
            "ListCost": (payg * qty).round(6),
            "ContractedCost": (payg * qty).round(6),
            "ListUnitPrice": payg,
            "ConsumedQuantity": qty,
            "ConsumedUnit": a["UnitOfMeasure"],
            "PricingQuantity": qty,
            "PricingUnit": a["UnitOfMeasure"],
            "PricingCategory": model.map(PRICING_MODEL).fillna("Other"),
            "CommitmentDiscountId": a["BenefitId"].where(model != "OnDemand"),
            "CommitmentDiscountType": model.map(COMMITMENT_TYPE),
            "CommitmentDiscountStatus": committed_usage.map({True: "Used", False: None}),
            "ServiceProviderName": "Microsoft",
            "HostProviderName": "Microsoft",
            "InvoiceIssuerName": "Microsoft",
            "ServiceName": a["MeterCategory"],
            "ServiceCategory": a["MeterCategory"].map(SERVICE_CATEGORY).fillna("Other"),
            "RegionId": region,
            "RegionName": region.map(REGION_NAMES).fillna(a["ResourceLocation"]),
            "ResourceId": rid,
            "ResourceName": rid.str.rsplit("/", n=1).str[-1],
            "ResourceType": rid.map(_rtype),
            "SkuId": a["MeterId"],
            "SkuMeter": a["MeterName"],
            "SubAccountId": a["SubscriptionId"],
            "SubAccountName": a["SubscriptionName"],
            "Tags": a["Tags"].map(_tags),
        }
    )
    return out


def _us_date(iso: pd.Series) -> pd.Series:
    return pd.to_datetime(iso.str[:10]).dt.strftime("%m/%d/%Y")


def from_focus(focus: pd.DataFrame) -> pd.DataFrame:
    f = focus[focus["ServiceProviderName"] == "Microsoft"].reset_index(drop=True)
    model = (
        f["CommitmentDiscountType"]
        .map({"Reservation": "Reservation", "Savings Plan": "SavingsPlan"})
        .fillna("OnDemand")
    )
    tags = f["Tags"].map(lambda t: json.dumps(json.loads(t), separators=(",", ": "))[1:-1] if t else "")
    return pd.DataFrame(
        {
            "BillingAccountId": f["BillingAccountId"],
            "BillingAccountName": f["BillingAccountName"],
            "BillingPeriodStartDate": _us_date(f["BillingPeriodStart"]),
            "BillingPeriodEndDate": _us_date(f["BillingPeriodEnd"]),
            "Date": _us_date(f["ChargePeriodStart"]),
            "SubscriptionId": f["SubAccountId"],
            "SubscriptionName": f["SubAccountName"],
            "ResourceGroup": f["ResourceId"].str.extract(r"/resourcegroups/([^/]+)/", expand=False),
            "ResourceId": f["ResourceId"],
            "ResourceLocation": f["RegionId"],
            "MeterCategory": f["ServiceName"],
            "MeterSubCategory": "",
            "MeterName": f["SkuMeter"],
            "MeterId": f["SkuId"],
            "ProductName": f["ServiceName"] + " " + f["SkuMeter"],
            "ConsumedService": f["ResourceId"].str.extract(r"/providers/([^/]+)/", expand=False),
            "Quantity": f["ConsumedQuantity"],
            "UnitOfMeasure": f["ConsumedUnit"],
            "PayGPrice": f["ListUnitPrice"],
            "UnitPrice": (f["EffectiveCost"] / f["ConsumedQuantity"]).round(8),
            "CostInBillingCurrency": f["EffectiveCost"],
            "BillingCurrencyCode": f["BillingCurrency"],
            "ChargeType": f["ChargeCategory"],
            "Frequency": "UsageBased",
            "PricingModel": model,
            "BenefitId": f["CommitmentDiscountId"],
            "BenefitName": f["CommitmentDiscountId"].str.rsplit("/", n=1).str[-1],
            "Tags": tags,
        },
        columns=AZURE_COLUMNS,
    )
