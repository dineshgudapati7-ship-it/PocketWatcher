"""Billing collector against schema-accurate system tables, with hand-computed expected totals."""

from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from pocketwatcher.collectors.billing import load_costs, sync_billing
from pocketwatcher.db.models import DailyCost
from tests.fixtures.system_tables import ALL_PURPOSE, JOBS_COMPUTE, FakeWarehouse

TODAY = date(2026, 10, 20)


def day_total(session, workspace, day: date) -> float:
    return (
        session.scalar(
            select(func.sum(DailyCost.list_cost)).where(
                DailyCost.workspace_id == workspace.id, DailyCost.usage_date == day
            )
        )
        or 0.0
    )


def test_aggregates_cost_per_entity_with_list_prices(session, workspace, tables):
    d = date(2026, 10, 19)
    tables.add(d, 10, job_id="j1", tags={"team": "a"})
    tables.add(d, 5, job_id="j1", tags={"team": "a"}, hour=3)
    tables.add(d, 4, ALL_PURPOSE, cluster_id="c1", product="ALL_PURPOSE")
    tables.add(d, 2, "UNPRICED_SKU", warehouse_id="w1")

    result = sync_billing(session, workspace, FakeWarehouse(tables), TODAY)

    costs = {(r.entity_type, r.entity_id): r for r in session.scalars(select(DailyCost))}
    assert costs[("job", "j1")].quantity == pytest.approx(15)
    assert costs[("job", "j1")].list_cost == pytest.approx(15 * 0.30)
    assert costs[("job", "j1")].tags == {"team": "a"}
    assert costs[("cluster", "c1")].list_cost == pytest.approx(4 * 0.55)
    assert costs[("warehouse", "w1")].list_cost == 0
    assert result.unpriced_quantity == pytest.approx(2)


def test_job_usage_is_attributed_to_the_job_not_its_cluster(session, workspace, tables):
    tables.add(date(2026, 10, 19), 10, job_id="j1", cluster_id="job-cluster-1")
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    row = session.scalars(select(DailyCost)).one()
    assert (row.entity_type, row.entity_id) == ("job", "j1")


def test_other_workspaces_are_excluded(session, workspace, tables):
    tables.add(date(2026, 10, 19), 10, job_id="mine")
    tables.add(date(2026, 10, 19), 99, job_id="theirs", workspace_id=999)
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    assert [r.entity_id for r in session.scalars(select(DailyCost))] == ["mine"]


def test_retractions_and_restatements_net_out(session, workspace, tables):
    d = date(2026, 10, 18)
    tables.add(d, 10, job_id="j1")
    tables.add(d, -10, job_id="j1", record_type="RETRACTION", ingested=date(2026, 10, 19))
    tables.add(d, 8, job_id="j1", record_type="RESTATEMENT", ingested=date(2026, 10, 19))
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    assert day_total(session, workspace, d) == pytest.approx(8 * 0.30)


def test_price_change_applies_from_its_start_time(session, workspace, tables):
    tables.prices = [p for p in tables.prices if p["sku_name"] != JOBS_COMPUTE]
    tables.price(JOBS_COMPUTE, 0.30, start=datetime(2020, 1, 1), end=datetime(2026, 10, 15))
    tables.price(JOBS_COMPUTE, 0.40, start=datetime(2026, 10, 15))
    tables.add(date(2026, 10, 14), 10, job_id="j1")
    tables.add(date(2026, 10, 15), 10, job_id="j1")
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    assert day_total(session, workspace, date(2026, 10, 14)) == pytest.approx(3.0)
    assert day_total(session, workspace, date(2026, 10, 15)) == pytest.approx(4.0)


def test_late_records_for_old_days_are_picked_up_without_double_counting(session, workspace, tables):
    old, recent = date(2026, 10, 5), date(2026, 10, 19)
    tables.add(old, 10, job_id="j1")
    tables.add(recent, 10, job_id="j1")
    wh = FakeWarehouse(tables)
    sync_billing(session, workspace, wh, TODAY)
    workspace.billing_synced_at = datetime(2026, 10, 20, 6)

    # A record for Oct 5 lands on Oct 21, outside the "recent days" window.
    tables.add(old, 5, job_id="j1", ingested=date(2026, 10, 21))
    wh.load(tables)
    result = sync_billing(session, workspace, wh, date(2026, 10, 21))

    assert old in result.dates
    assert day_total(session, workspace, old) == pytest.approx(15 * 0.30)
    assert day_total(session, workspace, recent) == pytest.approx(10 * 0.30)


def test_resync_is_idempotent(session, workspace, tables):
    tables.add(date(2026, 10, 19), 10, job_id="j1")
    wh = FakeWarehouse(tables)
    sync_billing(session, workspace, wh, TODAY)
    sync_billing(session, workspace, wh, TODAY)
    assert day_total(session, workspace, date(2026, 10, 19)) == pytest.approx(3.0)
    assert session.scalar(select(func.count()).select_from(DailyCost)) == 1


def test_same_tags_in_different_order_merge_into_one_row(session, workspace, tables):
    d = date(2026, 10, 19)
    tables.add(d, 10, job_id="j1", tags={"team": "a", "env": "prod"})
    tables.add(d, 10, job_id="j1", tags={"env": "prod", "team": "a"}, hour=4)
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    row = session.scalars(select(DailyCost)).one()
    assert row.quantity == pytest.approx(20)


def test_load_costs_applies_discount_and_window(session, workspace, tables):
    tables.add(date(2026, 10, 18), 10, job_id="j1")
    tables.add(date(2026, 10, 19), 10, job_id="j1")
    sync_billing(session, workspace, FakeWarehouse(tables), TODAY)
    workspace.discount_multiplier = 0.8
    rows = load_costs(session, workspace, date(2026, 10, 19), TODAY)
    assert [(r.usage_date, round(r.cost, 6)) for r in rows] == [(date(2026, 10, 19), 2.4)]
