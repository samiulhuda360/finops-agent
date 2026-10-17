import json

import pandas as pd

from finops_agent import focus
from finops_agent.azure_export import from_focus, is_azure_export, to_focus


def test_generated_billing_is_valid_focus(dataset):
    assert focus.validate(dataset.focus) == []
    assert set(focus.COLUMNS) <= set(dataset.focus.columns)


def test_validation_catches_bad_values(dataset):
    bad = dataset.focus.head(50).copy()
    bad.loc[bad.index[0], "ChargeCategory"] = "Spend"
    bad.loc[bad.index[1], "PricingCategory"] = "Committed"
    bad.loc[bad.index[1], "CommitmentDiscountId"] = None
    problems = focus.validate(bad)
    assert any("ChargeCategory" in p for p in problems)
    assert any("CommitmentDiscountId" in p for p in problems)


def test_prepare_adds_tag_columns_and_marks_untagged(prepared):
    assert {"Tags.team", "Tags.environment", "Tags.project"} <= set(prepared.columns)
    untagged = prepared[prepared["ResourceName"] == "test-instance"]
    assert (untagged["Tags.team"] == focus.UNTAGGED).all()


def test_azure_export_round_trip_keeps_every_value_the_analysis_uses(dataset):
    azure = dataset.focus[dataset.focus["ServiceProviderName"] == "Microsoft"].reset_index(drop=True)
    raw = from_focus(azure)
    assert is_azure_export(raw)
    back = to_focus(raw)
    for col in ["ChargePeriodStart", "ServiceName", "RegionId", "ResourceId", "SubAccountName", "PricingCategory"]:
        assert (back[col].astype(str) == azure[col].astype(str)).all(), col
    for col in ["BilledCost", "EffectiveCost", "ConsumedQuantity"]:
        assert (back[col] - azure[col]).abs().max() < 1e-6, col
    assert [json.loads(t) for t in back["Tags"].head(20)] == [json.loads(t) for t in azure["Tags"].head(20)]


def test_load_accepts_raw_azure_exports(dataset, tmp_path):
    raw = from_focus(dataset.focus.head(400))
    path = tmp_path / "azure.csv"
    raw.to_csv(path, index=False)
    df = focus.load([path])
    assert len(df) == len(raw) and (df["ServiceProviderName"] == "Microsoft").all()
    assert pd.api.types.is_datetime64_any_dtype(df["date"])
