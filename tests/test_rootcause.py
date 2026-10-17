import pandas as pd

from finops_agent.detect.rootcause import explain

DAYS = 70


def frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in ["SubAccountName", "ServiceName", "PricingCategory", "Tags.environment", "Tags.project"]:
        df[col] = df.get(col, "x")
    return df


def base_rows() -> list[dict]:
    rows = []
    for d in range(DAYS):
        for k in range(6):
            rows.append(
                {
                    "d": d,
                    "ResourceId": f"r{k}",
                    "RegionId": "nz",
                    "Tags.team": "web" if k < 3 else "data",
                    "EffectiveCost": 20.0 + (d % 3),
                }
            )
    return rows


def test_one_resource_spiking_is_named():
    rows = base_rows()
    for d in range(60, 63):
        rows.append({"d": d, "ResourceId": "r4", "RegionId": "nz", "Tags.team": "data", "EffectiveCost": 80.0})
    root = explain(frame(rows), [60, 61, 62], DAYS)
    assert root and root.dimension == "ResourceId" and root.values == ["r4"]


def test_a_new_region_is_named_rather_than_its_resources():
    rows = base_rows()
    for d in range(60, 63):
        for k in range(4):
            rows.append(
                {"d": d, "ResourceId": f"eu{k}", "RegionId": "westeurope", "Tags.team": "web", "EffectiveCost": 15.0}
            )
    root = explain(frame(rows), [60, 61, 62], DAYS)
    assert root and root.dimension == "RegionId" and root.values == ["westeurope"]


def test_untagged_sprawl_is_named_by_the_missing_tag():
    rows = base_rows()
    for d in range(60, 63):
        for k in range(8):
            rows.append(
                {"d": d, "ResourceId": f"exp{k}", "RegionId": "nz", "Tags.team": "(untagged)", "EffectiveCost": 6.0}
            )
    root = explain(frame(rows), [60, 61, 62], DAYS)
    assert root and root.dimension == "Tags.team" and root.values == ["(untagged)"]


def test_no_increase_means_no_root():
    assert explain(frame(base_rows()), [60, 61, 62], DAYS) is None
