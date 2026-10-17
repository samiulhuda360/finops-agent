from finops_agent.explain import check
from finops_agent.explain.grounding import ungrounded

FACTS = {"extra_cost_so_far": 2856.39, "share": 0.034, "count": 2}


def test_numbers_from_the_facts_pass_in_any_display_format():
    assert ungrounded("It cost $2,856.39, about $2,856 or $2.9k, across 2 VMs; 3.4% of reads.", FACTS) == []


def test_an_invented_number_is_caught():
    assert ungrounded("It cost $3,100 so far.", FACTS) == ["$3,100"]
    assert ungrounded("Usage went up 40%.", FACTS) == ["40%"]


def test_dates_are_not_numbers():
    assert ungrounded("It started on 10 May 2026 (2026-05-10) and ran into June 2026.", FACTS) == []


def test_names_with_digits_are_ignored_when_listed():
    text = "vm-sql-prod-01 moved to Standard_D16s_v5 and saved $2,856."
    assert ungrounded(text, FACTS) == ["01"]
    assert ungrounded(text, FACTS, ignore=["vm-sql-prod-01"]) == []


def test_check_reads_money_strings_and_ignores_names_in_the_facts():
    facts = {"resource": "vm-sql-prod-01, vm-sql-prod-02", "monthly_saving": "$301.67", "started_on": "10 May 2026"}
    assert check("Buy a reservation for vm-sql-prod-02 to save $302 a month from 10 May 2026.", facts) == []
    assert check("Save $350 a month on vm-sql-prod-02.", facts) == ["$350"]
