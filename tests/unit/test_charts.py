from datetime import date

import pytest

from pocketwatcher.analytics import Bucket, Burn
from pocketwatcher.web.charts import burn_chart, column_chart, compact_usd, nice_ticks


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
