"""FOCUS (FinOps Open Cost and Usage Specification) cost-and-usage columns used by this project.

The column names follow FOCUS 1.4 (focus.finops.org). Only the columns the analysis needs are produced and
required; any other FOCUS column in an input file is kept but ignored. Tags is a JSON object string, as the
specification defines it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pandas as pd

FOCUS_VERSION = "1.4"

COLUMNS = [
    "BillingAccountId",
    "BillingAccountName",
    "BillingCurrency",
    "BillingPeriodStart",
    "BillingPeriodEnd",
    "ChargePeriodStart",
    "ChargePeriodEnd",
    "ChargeCategory",
    "ChargeFrequency",
    "ChargeDescription",
    "BilledCost",
    "EffectiveCost",
    "ListCost",
    "ContractedCost",
    "ListUnitPrice",
    "ConsumedQuantity",
    "ConsumedUnit",
    "PricingQuantity",
    "PricingUnit",
    "PricingCategory",
    "CommitmentDiscountId",
    "CommitmentDiscountType",
    "CommitmentDiscountStatus",
    "ServiceProviderName",
    "HostProviderName",
    "InvoiceIssuerName",
    "ServiceName",
    "ServiceCategory",
    "RegionId",
    "RegionName",
    "ResourceId",
    "ResourceName",
    "ResourceType",
    "SkuId",
    "SkuMeter",
    "SubAccountId",
    "SubAccountName",
    "Tags",
]

REQUIRED = [
    "ChargePeriodStart",
    "ChargeCategory",
    "BilledCost",
    "EffectiveCost",
    "ListCost",
    "ServiceProviderName",
    "ServiceName",
    "RegionId",
    "ResourceId",
    "SubAccountName",
    "PricingCategory",
    "Tags",
]

CHARGE_CATEGORIES = {"Usage", "Purchase", "Tax", "Credit", "Adjustment"}
PRICING_CATEGORIES = {"Standard", "Dynamic", "Committed", "Other"}
SERVICE_CATEGORIES = {
    "AI and Machine Learning",
    "Analytics",
    "Business Applications",
    "Compute",
    "Databases",
    "Developer Tools",
    "Multicloud",
    "Identity",
    "Integration",
    "Internet of Things",
    "Management and Governance",
    "Media",
    "Migration",
    "Mobile",
    "Networking",
    "Security",
    "Storage",
    "Web",
    "Other",
}

# Tag keys the company requires on every resource; the analysis turns them into Tags.<key> columns.
TAG_KEYS = ["team", "environment", "project"]
UNTAGGED = "(untagged)"


def validate(df: pd.DataFrame) -> list[str]:
    """Conformance problems with the FOCUS rules this project relies on (empty list when it conforms)."""
    problems = [f"missing required column {c}" for c in REQUIRED if c not in df.columns]
    if problems:
        return problems
    bad = set(df["ChargeCategory"].dropna()) - CHARGE_CATEGORIES
    if bad:
        problems.append(f"ChargeCategory has values outside the specification: {sorted(bad)}")
    bad = set(df["PricingCategory"].dropna()) - PRICING_CATEGORIES
    if bad:
        problems.append(f"PricingCategory has values outside the specification: {sorted(bad)}")
    if "ServiceCategory" in df.columns:
        bad = set(df["ServiceCategory"].dropna()) - SERVICE_CATEGORIES
        if bad:
            problems.append(f"ServiceCategory has values outside the specification: {sorted(bad)}")
    for col in ("BilledCost", "EffectiveCost", "ListCost"):
        if not pd.api.types.is_numeric_dtype(df[col]):
            problems.append(f"{col} is not numeric")
    committed = df["PricingCategory"] == "Committed"
    if "CommitmentDiscountId" in df.columns and df.loc[committed, "CommitmentDiscountId"].isna().any():
        problems.append("Committed rows must carry a CommitmentDiscountId")
    tags = df["Tags"].dropna().head(200)
    for t in tags:
        try:
            if not isinstance(json.loads(t), dict):
                raise ValueError
        except ValueError:
            problems.append("Tags must be a JSON object string")
            break
    return problems


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Typed copy with a `date` column and one Tags.<key> column per required tag key."""
    out = df.copy()
    out["date"] = pd.to_datetime(out["ChargePeriodStart"], utc=True).dt.tz_localize(None).dt.normalize()
    for col in ("BilledCost", "EffectiveCost", "ListCost", "ConsumedQuantity"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    parsed = out["Tags"].fillna("{}").map(_tags)
    for key in TAG_KEYS:
        out[f"Tags.{key}"] = parsed.map(lambda t, k=key: t.get(k) or UNTAGGED)
    for col in ("RegionId", "SubAccountName", "ServiceName", "PricingCategory", "ResourceId"):
        out[col] = out[col].fillna("(none)")
    return out


def _tags(raw: str) -> dict[str, str]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def load(paths: Iterable[Path]) -> pd.DataFrame:
    """Read FOCUS CSV files, or raw Azure cost exports (detected by their columns and mapped to FOCUS)."""
    from .azure_export import is_azure_export, to_focus

    frames = []
    for path in paths:
        raw = pd.read_csv(path, low_memory=False)
        frames.append(to_focus(raw) if is_azure_export(raw) else raw)
    df = pd.concat(frames, ignore_index=True)
    problems = validate(df)
    if problems:
        raise ValueError("input is not valid FOCUS: " + "; ".join(problems))
    return prepare(df)
