"""Known-answer tests for each billing rule, including the cases that must stay quiet."""

from datetime import date, timedelta

import pytest

from pocketwatcher.rules import anomaly, budget, dev_premium, digest, untagged
from pocketwatcher.rules.base import CostRow, WorkspaceRef
from pocketwatcher.rules.config import (
    AnomalyConfig,
    BudgetConfig,
    DevPremiumConfig,
    RuleConfig,
    UntaggedConfig,
)
from pocketwatcher.rules.engine import run_billing_rules

WS = WorkspaceRef(id=1, name="acme-prod", host="https://adb-1.azuredatabricks.net")
DAY = date(2026, 10, 19)  # a Monday


def cost(day_offset: int, amount: float, entity: str = "j1", kind: str = "job", **kw) -> CostRow:
    return CostRow(DAY + timedelta(days=day_offset), kind, entity, "PREMIUM_JOBS_COMPUTE", amount, **kw)


def series(amounts: list[float], entity: str = "j1", **kw) -> list[CostRow]:
    """amounts[-1] is DAY, earlier entries are the preceding days."""
    n = len(amounts)
    return [cost(i - n + 1, a, entity, **kw) for i, a in enumerate(amounts) if a]


# --- budget ---------------------------------------------------------------


def test_daily_budget_fires_only_when_exceeded():
    cfg = BudgetConfig(daily_usd=100)
    (f,) = budget.evaluate([cost(0, 80), cost(0, 40, "j2")], cfg, WS, DAY)
    assert f.rule == "budget.daily" and f.impact_usd == pytest.approx(20) and f.severity == "warning"
    assert budget.evaluate([cost(0, 99)], cfg, WS, DAY) == []


def test_daily_budget_is_critical_at_150_percent():
    (f,) = budget.evaluate([cost(0, 150)], BudgetConfig(daily_usd=100), WS, DAY)
    assert f.severity == "critical"


def test_monthly_forecast_uses_7_day_average():
    # Oct 1..19 at $100/day = $1,900 MTD; 12 days left at $100 -> $3,100 forecast.
    rows = [cost(-i, 100) for i in range(19)]
    (f,) = budget.evaluate(rows, BudgetConfig(monthly_usd=3000), WS, DAY)
    assert f.fingerprint.endswith(":forecast")
    assert f.impact_usd == pytest.approx(100)
    assert budget.evaluate(rows, BudgetConfig(monthly_usd=3200), WS, DAY) == []


def test_monthly_exceeded_replaces_forecast_alert():
    rows = [cost(-i, 100) for i in range(19)]
    (f,) = budget.evaluate(rows, BudgetConfig(monthly_usd=1500), WS, DAY)
    assert f.fingerprint.endswith(":exceeded") and f.impact_usd == pytest.approx(400)


def test_no_budget_configured_means_no_findings():
    assert budget.evaluate([cost(0, 10_000)], BudgetConfig(), WS, DAY) == []


# --- anomaly --------------------------------------------------------------


def test_spike_against_steady_baseline_fires_with_excess_as_impact():
    rows = series([100] * 7 + [400])
    (f,) = anomaly.evaluate(rows, AnomalyConfig(), WS, DAY)
    assert f.rule == "anomaly.spike"
    assert f.impact_usd == pytest.approx(300)
    assert f.link == "https://adb-1.azuredatabricks.net/jobs/j1"


def test_normal_variation_stays_quiet():
    rows = series([90, 110, 95, 105, 100, 98, 102, 130])
    assert anomaly.evaluate(rows, AnomalyConfig(), WS, DAY) == []


def test_earlier_spike_does_not_mask_a_new_one():
    # A mean/stddev baseline would be inflated by the 900; the median/MAD one is not.
    rows = series([100, 100, 900, 100, 100, 100, 100, 400])
    (f,) = anomaly.evaluate(rows, AnomalyConfig(), WS, DAY)
    assert f.impact_usd == pytest.approx(300)


def test_weekly_job_is_not_an_anomaly():
    # Runs every Monday for $500, nothing in between.
    rows = series([500] + [0] * 6 + [500] + [0] * 6 + [500])
    assert anomaly.evaluate(rows, AnomalyConfig(), WS, DAY) == []


def test_weekly_job_that_doubles_does_fire():
    rows = series([500] + [0] * 6 + [500] + [0] * 6 + [1500])
    (f,) = anomaly.evaluate(rows, AnomalyConfig(), WS, DAY)
    assert f.rule == "anomaly.spike"


def test_cheap_entities_are_ignored():
    rows = series([1] * 7 + [40])
    assert anomaly.evaluate(rows, AnomalyConfig(min_cost_usd=50), WS, DAY) == []


def test_new_high_spender_fires_without_baseline():
    rows = series([0, 0, 0, 0, 0, 0, 1200, 1500], entity="poc-cluster", kind="cluster")
    (f,) = anomaly.evaluate(rows, AnomalyConfig(), WS, DAY)
    assert f.rule == "anomaly.new" and f.severity == "critical"
    assert f.link == "https://adb-1.azuredatabricks.net/compute/clusters/poc-cluster"


def test_new_small_spender_is_quiet():
    rows = series([0, 0, 0, 0, 0, 0, 0, 120])
    assert anomaly.evaluate(rows, AnomalyConfig(), WS, DAY) == []


def test_owner_is_largest_run_as_on_the_day():
    rows = [*series([100] * 7), cost(0, 300, run_as="alice@x.com"), cost(0, 100, run_as="bob@x.com")]
    (f,) = anomaly.evaluate(rows, AnomalyConfig(), WS, DAY)
    assert f.owner == "alice@x.com"


# --- untagged -------------------------------------------------------------


def test_untagged_share_over_threshold_fires():
    rows = [cost(0, 800, tags={"team": "a"}), cost(-1, 200, "j2")]
    (f,) = untagged.evaluate(rows, UntaggedConfig(required_tags=["team"]), WS, DAY)
    assert f.data["share"] == pytest.approx(0.2) and f.impact_usd == pytest.approx(200)


def test_untagged_share_under_threshold_or_rule_disabled_is_quiet():
    rows = [cost(0, 950, tags={"team": "a"}), cost(0, 50, "j2")]
    assert untagged.evaluate(rows, UntaggedConfig(required_tags=["team"]), WS, DAY) == []
    assert untagged.evaluate(rows, UntaggedConfig(), WS, DAY) == []


def test_empty_tag_value_counts_as_untagged():
    rows = [cost(0, 500, tags={"team": ""}), cost(0, 500, "j2", tags={"team": "a"})]
    assert untagged.evaluate(rows, UntaggedConfig(required_tags=["team"]), WS, DAY)


# --- dev premium ----------------------------------------------------------


def test_photon_on_dev_fires_and_prod_does_not():
    rows = [
        cost(0, 300, "dev-c", "cluster", tags={"env": "Dev"}, is_photon=True),
        cost(0, 900, "prod-c", "cluster", tags={"env": "prod"}, is_photon=True),
    ]
    (f,) = dev_premium.evaluate(rows, DevPremiumConfig(), WS, DAY)
    assert f.entity == ("cluster", "dev-c") and f.fingerprint.endswith(":photon")


def test_dev_premium_respects_floor_and_flags():
    rows = [cost(0, 5, tags={"env": "dev"}, is_serverless=True)]
    assert dev_premium.evaluate(rows, DevPremiumConfig(), WS, DAY) == []
    rows = [cost(0, 50, tags={"env": "dev"}, is_serverless=True)]
    assert dev_premium.evaluate(rows, DevPremiumConfig(flag_serverless=False), WS, DAY) == []


# --- digest ---------------------------------------------------------------


def test_digest_ranks_top_offenders_with_week_over_week():
    rows = [cost(-i, 100, "big") for i in range(7)] + [cost(-i, 10, "small") for i in range(7)]
    rows += [cost(-7 - i, 50, "big") for i in range(7)]
    f = digest.evaluate(rows, WS, DAY, top_n=1)
    assert [o["entity"] for o in f.data["offenders"]] == [("job", "big")]
    assert f.data["total"] == pytest.approx(770) and f.data["prev_total"] == pytest.approx(350)


# --- engine ---------------------------------------------------------------


def test_engine_evaluates_yesterday_and_ignores_todays_partial_usage():
    today = DAY + timedelta(days=1)
    rows = [*series([100] * 8), CostRow(today, "job", "j1", "PREMIUM_JOBS_COMPUTE", 5000)]
    cfg = RuleConfig(budget=BudgetConfig(daily_usd=150))
    assert run_billing_rules(rows, cfg, WS, today) == []
