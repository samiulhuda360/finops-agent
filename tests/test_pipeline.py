from finops_agent.detect import detect
from finops_agent.evaluate import match, score_savings
from finops_agent.prices import PriceBook
from finops_agent.recommend import recommend


def test_labels_and_truth_are_consistent(dataset):
    assert len(dataset.labels) == 10
    assert {lab["type"] for lab in dataset.labels} >= {
        "forgotten_dev_vm",
        "egress_spike",
        "log_ingestion_explosion",
        "autoscaler_misconfig",
        "untagged_sprawl",
        "reservation_expiry",
        "region_mistake",
    }
    assert all(lab["true_excess_to_date"] > 0 and lab["resources"] for lab in dataset.labels)


def test_reservation_expiry_is_found_named_and_explained(dataset, prepared):
    incidents = detect(prepared, "seasonal")
    label = next(lab for lab in dataset.labels if lab["type"] == "reservation_expiry")
    inc = next(i for i in incidents if i.start == label["start"])
    assert inc.root_dimension == "ResourceId" and sorted(inc.root_values) == sorted(label["root_values"])
    assert "commitment" in inc.cause and inc.ongoing
    assert abs(inc.impact_to_date - label["true_excess_to_date"]) / label["true_excess_to_date"] < 0.05


def test_seasonal_detector_scores_on_the_development_seed(dataset, prepared):
    r = match(detect(prepared, "seasonal"), dataset.labels)
    assert r["recall"] >= 0.9 and r["precision"] >= 0.7


def test_idle_vm_saving_is_priced_from_the_price_list(dataset, prepared):
    recs = recommend(prepared, dataset.metrics)
    idle = next(r for r in recs if r.resource_name == "vm-reporting-old-01")
    price = PriceBook().vm("Microsoft", "newzealandnorth", "Standard_D4s_v5")["ondemand_hourly"]
    disk = PriceBook().azure()["disk_month"]["P10"]  # a staging VM gets a P10 OS disk
    assert idle.type == "idle"
    assert abs(idle.monthly_saving - (price * 730.5 + disk)) < 2.0


def test_tiering_saving_nets_off_retrieval(dataset, prepared):
    recs = recommend(prepared, dataset.metrics)
    lake = next(r for r in recs if r.resource_name == "tw-data-lake")
    ev = lake.evidence
    expected = ev["stored_gb"] * (ev["price_now_per_gb"] - ev["price_target_per_gb"])
    expected -= ev["read_gb_per_month"] * ev["retrieval_per_gb"]
    assert lake.type == "tiering" and abs(lake.monthly_saving - expected) < 1.0


def test_recommendations_match_the_planted_truth(dataset, prepared):
    s = score_savings(recommend(prepared, dataset.metrics), dataset.opportunities)
    assert s["recall"] >= 0.9 and s["precision"] >= 0.8
    assert s["median_ape"] < s["baseline_median_ape"]
