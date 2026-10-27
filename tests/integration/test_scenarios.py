"""End to end: raw system-table rows -> sync -> rules, for the incidents the product exists to catch."""

from datetime import date, timedelta

from pocketwatcher.collectors.billing import load_costs, sync_billing
from pocketwatcher.rules.base import WorkspaceRef
from pocketwatcher.rules.config import BudgetConfig, RuleConfig, UntaggedConfig
from pocketwatcher.rules.engine import billing_window, run_billing_rules
from tests.fixtures.system_tables import ALL_PURPOSE, FakeWarehouse

TODAY = date(2026, 10, 20)


def run(session, workspace, tables, cfg: RuleConfig):
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    rows = load_costs(session, workspace, *billing_window(TODAY))
    ws = WorkspaceRef(workspace.id, workspace.name, workspace.host)
    return run_billing_rules(rows, cfg, ws, TODAY, include_digest=True)


def steady_team(tables, days: int = 30):
    for i in range(1, days + 1):
        d = TODAY - timedelta(days=i)
        tables.add(d, 200, job_id="nightly-etl", tags={"team": "data", "env": "prod"})  # $60/day
        tables.add(d, 50, ALL_PURPOSE, cluster_id="shared", product="ALL_PURPOSE", tags={"team": "data"})


def test_photon_left_on_in_a_dev_poc(session, workspace, tables):
    steady_team(tables)
    for i in (1, 2):  # a POC cluster started two days ago, Photon on, ~$1,100/day
        tables.add(
            TODAY - timedelta(days=i),
            2000,
            ALL_PURPOSE,
            cluster_id="poc-photon",
            product="ALL_PURPOSE",
            photon=True,
            tags={"team": "data", "env": "dev"},
            run_as="intern@acme.com",
        )
    findings = run(session, workspace, tables, RuleConfig(budget=BudgetConfig(daily_usd=500)))
    fired = {(f.rule, f.entity) for f in findings}

    assert ("anomaly.new", ("cluster", "poc-photon")) in fired
    assert ("dev_premium", ("cluster", "poc-photon")) in fired
    assert ("budget.daily", None) in fired
    poc = next(f for f in findings if f.rule == "anomaly.new")
    assert poc.owner == "intern@acme.com"
    digest = next(f for f in findings if f.rule == "digest")
    assert digest.data["offenders"][0]["entity"] == ("cluster", "poc-photon")


def test_quiet_week_only_produces_the_digest(session, workspace, tables):
    steady_team(tables)
    cfg = RuleConfig(
        budget=BudgetConfig(daily_usd=500, monthly_usd=5000), untagged=UntaggedConfig(required_tags=["team"])
    )
    findings = run(session, workspace, tables, cfg)
    assert [f.rule for f in findings] == ["digest"]


def test_runaway_job_after_a_code_change(session, workspace, tables):
    steady_team(tables)
    tables.add(TODAY - timedelta(days=1), 3000, job_id="nightly-etl", tags={"team": "data", "env": "prod"})
    findings = run(session, workspace, tables, RuleConfig())
    (spike,) = [f for f in findings if f.rule == "anomaly.spike"]
    assert spike.entity == ("job", "nightly-etl")
    assert round(spike.impact_usd) == 900  # $960 that day vs a $60 median
