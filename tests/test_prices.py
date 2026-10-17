from finops_agent.prices import PriceBook, load


def test_snapshot_has_both_clouds_and_a_fetch_date():
    data = load()
    assert data["fetched"] and data["currency"] == "USD"
    assert "newzealandnorth" in data["azure"] and "ap-southeast-6" in data["aws"]


def test_azure_reservation_hourly_is_the_term_price_spread_over_a_year():
    vm = PriceBook().vm("Microsoft", "newzealandnorth", "Standard_D4s_v5")
    assert abs(vm["reservation_1y_hourly"] - vm["reservation_1y_total"] / 8760) < 1e-6
    assert vm["reservation_1y_hourly"] < vm["savings_plan_1y_hourly"] < vm["ondemand_hourly"]


def test_cheapest_commitment_is_chosen():
    book = PriceBook()
    name, rate = book.commitment_hourly("Microsoft", "newzealandnorth", "Standard_D4s_v5")
    assert name == "1-year reservation"
    name, rate = book.commitment_hourly("Microsoft", "newzealandnorth", "Standard_B2s")
    assert name == "1-year savings plan"  # B-series has no reservation in the price list
    name, rate = book.commitment_hourly("AWS", "ap-southeast-6", "m6i.xlarge")
    assert name == "1-year Compute Savings Plan"
    assert rate < book.vm("AWS", "ap-southeast-6", "m6i.xlarge")["ondemand_hourly"]
