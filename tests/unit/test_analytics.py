"""Analytics figures against hand-computed totals; every breakdown must add up to the period total."""

from datetime import date, timedelta

import pytest

from pocketwatcher.analytics import Period, build_report, period_for
from pocketwatcher.rules.base import CostRow

TODAY = date(2026, 10, 27)


def row(d: date, cost: float, entity="j1", kind="job", product="JOBS", ws=1, **kw) -> CostRow:
    return CostRow(d, kind, entity, "SKU", cost, product=product, workspace_id=ws, **kw)


def mixed_rows() -> list[CostRow]:
    rows = []
    for i in range(1, 61):
        d = TODAY - timedelta(days=i)
        rows += [
            row(d, 100, "etl", tags={"team": "data"}, run_as="etl@acme.com"),
            row(d, 40, "bi", "warehouse", "SQL", tags={"team": "analytics"}, is_serverless=True, is_photon=True),
            row(d, 25, "scratch", "cluster", "ALL_PURPOSE", run_as="alice@acme.com", is_photon=True),  # untagged
            row(d, 5, "x", "endpoint", "MODEL_SERVING", ws=2, tags={"team": "ml"}),
        ]
    return rows


# --- periods -------------------------------------------------------------------


def test_month_to_date_excludes_today_and_compares_to_the_same_days_last_month():
    p = period_for("mtd", TODAY)
    assert (p.start, p.end, p.days) == (date(2026, 10, 1), TODAY, 26)
    prev = p.previous()
    assert (prev.start, prev.end) == (date(2026, 9, 1), date(2026, 9, 27))


def test_month_to_date_on_the_first_falls_back_to_last_month():
    p = period_for("mtd", date(2026, 11, 1))
    assert (p.start, p.end, p.key) == (date(2026, 10, 1), date(2026, 11, 1), "last_month")


def test_last_month_compares_to_the_whole_month_before():
    p = period_for("last_month", TODAY)
    assert (p.start, p.end) == (date(2026, 9, 1), date(2026, 10, 1))
    assert (p.previous().start, p.previous().end) == (date(2026, 8, 1), date(2026, 9, 1))


def test_rolling_ranges_compare_to_the_equal_period_before():
    p = period_for("30d", TODAY)
    assert (p.start, p.end) == (TODAY - timedelta(days=30), TODAY)
    assert (p.previous().start, p.previous().end) == (TODAY - timedelta(days=60), TODAY - timedelta(days=30))
    assert period_for("bogus", TODAY) == p


# --- totals reconcile -------------------------------------------------------------


@pytest.mark.parametrize("key", ["mtd", "last_month", "30d", "90d"])
def test_every_breakdown_adds_up_to_the_total(key):
    r = build_report(mixed_rows(), period_for(key, TODAY), TODAY, tag_key="team", budgets={1: None, 2: None})
    assert r.total == pytest.approx(sum(b.total for b in r.buckets))
    assert r.total == pytest.approx(sum(s.cost for s in r.products))
    assert r.total == pytest.approx(sum(s.cost for s in r.compute))
    assert r.total == pytest.approx(sum(s.cost for s in r.tags))
    assert sum(s.share for s in r.products) == pytest.approx(1.0)


def test_thirty_day_figures():
    r = build_report(mixed_rows(), period_for("30d", TODAY), TODAY, tag_key="team", budgets={1: None, 2: None})
    assert r.total == pytest.approx(30 * 170)
    assert r.prev_total == pytest.approx(30 * 170) and r.change == pytest.approx(0)
    assert r.per_day == pytest.approx(170)
    assert {s.key: round(s.cost) for s in r.products} == {"jobs": 3000, "sql": 1200, "all_purpose": 750, "serving": 150}
    assert {s.key: round(s.cost) for s in r.compute} == {"serverless": 1200, "photon": 750, "standard": 3150}
    assert [(s.key, round(s.cost)) for s in r.tags] == [
        ("data", 3000),
        ("analytics", 1200),
        ("ml", 150),
        ("Untagged", 750),
    ]
    assert r.untagged_share == pytest.approx(750 / 5100)
    assert [s.entity[1] for s in r.spenders] == ["etl", "bi", "scratch", "x"]
    assert r.owners[0].key == "etl@acme.com"


def test_growth_shows_up_as_change_and_drivers():
    rows = mixed_rows() + [row(TODAY - timedelta(days=i), 300, "new-job", run_as="bob@acme.com") for i in range(1, 6)]
    r = build_report(rows, period_for("30d", TODAY), TODAY, tag_key="team", budgets={1: None})
    assert r.change == pytest.approx(1500 / 5100)
    (top_driver, *_) = r.drivers
    assert top_driver.entity == ("job", "new-job") and top_driver.change is None  # new: no prior cost
    assert r.spenders[0].entity == ("job", "etl")


def test_tag_key_defaults_to_team_like_keys():
    r = build_report(mixed_rows(), period_for("30d", TODAY), TODAY, tag_key="nope", budgets={1: None})
    assert r.tag_key == "team" and r.tag_keys == ["team"]


def test_buckets_switch_from_days_to_weeks_to_months():
    rows = mixed_rows()
    assert build_report(rows, period_for("30d", TODAY), TODAY, tag_key=None, budgets={}).bucket_unit == "day"
    assert build_report(rows, period_for("90d", TODAY), TODAY, tag_key=None, budgets={}).bucket_unit == "week"
    six = build_report(rows, period_for("6m", TODAY), TODAY, tag_key=None, budgets={})
    assert six.bucket_unit == "month" and six.buckets[-1].label == "Oct 2026 (to date)"


def test_burn_forecast_and_partial_budget_note():
    r = build_report(mixed_rows(), period_for("mtd", TODAY), TODAY, tag_key=None, budgets={1: 4000.0, 2: None})
    b = r.burn
    assert (b.month_start, b.through, b.month_end) == (date(2026, 10, 1), date(2026, 10, 26), date(2026, 10, 31))
    assert b.cumulative[-1][1] == pytest.approx(26 * 170)
    assert b.forecast == pytest.approx(31 * 170)
    assert b.budget == 4000.0 and b.budget_note == "Budget covers 1 of 2 workspaces."


def test_empty_period_is_explicit():
    r = build_report([], Period(date(2026, 1, 1), date(2026, 1, 31), "30d"), TODAY, tag_key=None, budgets={1: None})
    assert r.total == 0 and r.change is None and r.burn is None and r.notes == ["No usage in this period yet."]


def test_month_ranges_cover_whole_calendar_months():
    p = period_for("6m", TODAY)
    assert (p.start, p.end) == (date(2026, 5, 1), TODAY)
    assert period_for("12m", TODAY).start == date(2025, 11, 1)


def test_many_tag_values_fold_into_other_with_untagged_last():
    rows = [row(TODAY - timedelta(days=1), 100 + i, f"j{i}", tags={"team": f"t{i:02d}"}) for i in range(25)]
    rows.append(row(TODAY - timedelta(days=1), 50, "loose"))
    r = build_report(rows, period_for("30d", TODAY), TODAY, tag_key="team", budgets={1: None})
    assert len(r.tags) == 11  # 9 named + Other + Untagged
    assert r.tags[-2].label == "Other (16 values)" and r.tags[-1].key == "Untagged"
    assert r.total == pytest.approx(sum(s.cost for s in r.tags))


def test_no_change_figures_without_a_full_previous_period():
    rows = [row(TODAY - timedelta(days=i), 100) for i in range(1, 66)]  # 65 days of history
    r = build_report(rows, period_for("30d", TODAY), TODAY, tag_key=None, budgets={1: None})
    assert r.comparable and r.change == pytest.approx(0)
    r = build_report(rows, period_for("90d", TODAY), TODAY, tag_key=None, budgets={1: None})
    assert not r.comparable and r.change is None and r.drivers == []
    assert "no full previous period" in r.notes[-1]
