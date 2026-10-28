from datetime import date

import pytest

from pocketwatcher.analytics import Bucket, Burn, short_day
from pocketwatcher.rules.base import usd
from pocketwatcher.web.charts import burn_chart, column_chart, compact_usd, nice_ticks, usd0


@pytest.mark.parametrize(
    ("peak", "ticks"),
    [
        (0, [0, 1]),
        (87, [0, 25, 50, 75, 100]),
        (3400, [0, 1000, 2000, 3000, 4000]),
        (1.2e6, [0, 5e5, 1e6, 1.5e6]),
    ],
)
def test_nice_ticks(peak, ticks):
    assert nice_ticks(peak) == ticks


@pytest.mark.parametrize(("v", "s"), [(950, "$950"), (1000, "$1K"), (2500, "$2.5K"), (32900, "$33K"), (2e6, "$2M")])
def test_compact_usd(v, s):
    assert compact_usd(v) == s


def test_column_segments_stay_in_fixed_group_order_and_scale_to_axis_top():
    chart = column_chart(
        [
            Bucket(date(2026, 10, 1), "Oct 01", {"sql": 50, "jobs": 100}),
            Bucket(date(2026, 10, 2), "Oct 02", {"jobs": 150}),
        ]
    )
    assert [s.key for s in chart.columns[0].segments] == ["jobs", "sql"]  # entity order, not size or input order
    assert chart.columns[1].segments[0].pct == pytest.approx(100)  # 150 on clean ticks 0/50/100/150
    assert chart.legend == [("jobs", "Jobs", 1), ("sql", "SQL warehouses", 2)]
    assert [label for _, label in chart.ticks] == ["$0", "$50", "$100", "$150"]


def test_burn_chart_marks_over_budget_and_places_the_forecast_at_month_end():
    burn = Burn(
        date(2026, 10, 1),
        date(2026, 10, 31),
        date(2026, 10, 3),
        [(date(2026, 10, 1), 100), (date(2026, 10, 2), 200), (date(2026, 10, 3), 300)],
        forecast=3100,
        budget=3000,
        budget_note=None,
    )
    c = burn_chart(burn)
    assert c.over_budget and c.forecast_x == pytest.approx(100)
    assert c.budget_y is not None and c.forecast_y < c.budget_y  # forecast plots above the budget line
    assert c.actual_path.startswith("M0.0,")


@pytest.mark.parametrize(
    ("v", "s"),
    [
        (0, "$0"),
        (4.5, "$4.50"),
        (9.99, "$9.99"),
        (10, "$10"),
        (88.01, "$88"),
        (243.4, "$243"),
        (21723.4, "$21,723"),
        (-88.01, "-$88"),
        (-4.5, "-$4.50"),
    ],
)
def test_usd0_shows_whole_dollars_and_cents_only_below_ten(v, s):
    assert usd0(v) == s


def test_alerts_keep_their_own_money_format():
    assert usd(88.01) == "$88.01" and usd(21723.4) == "$21,723"  # unchanged by the web format


def test_dates_are_not_zero_padded():
    assert short_day(date(2026, 10, 1)) == "Oct 1" and short_day(date(2026, 10, 1), year=True) == "Oct 1, 2026"


def test_month_columns_get_short_labels_and_the_last_is_flagged():
    months = [Bucket(date(2026, m, 1), f"{date(2026, m, 1):%b %Y}", {"jobs": 10}) for m in range(5, 11)]
    months[-1] = Bucket(date(2026, 10, 1), "Oct 2026 (to date)", {"jobs": 5})
    chart = column_chart(months, "month")
    assert [c.axis_label for c in chart.columns] == ["May", "Jun", "Jul", "Aug", "Sep", "Oct"]
    assert chart.columns[-1].label == "Oct 2026 (to date)"  # the tooltip and table keep the full label
    assert chart.columns[-1].last and not any(c.alt for c in chart.columns)  # six labels fit without thinning


def test_many_day_labels_are_thinned_on_narrow_screens_but_never_the_last():
    days = [Bucket(date(2026, 10, d), short_day(date(2026, 10, d)), {"jobs": 1}) for d in range(1, 31)]
    shown = [c for c in column_chart(days).columns if c.show_label]
    assert [c.axis_label for c in shown][:2] == ["Oct 1", "Oct 5"]
    assert [c.alt for c in shown] == [False, True] * 4
