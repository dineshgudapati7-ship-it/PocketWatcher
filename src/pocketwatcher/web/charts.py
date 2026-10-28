"""View models for the analytics charts. Geometry is computed here so templates only lay it out."""

import json
import math
from dataclasses import dataclass, field

from pocketwatcher.analytics import PRODUCT_GROUPS, Bucket, Burn, short_day

# Categorical slots in validated order (dataviz reference palette); "other" folds to neutral.
GROUP_SLOT = {"jobs": 1, "sql": 2, "all_purpose": 3, "pipelines": 4, "serving": 5, "other": 0}


def usd0(v: float) -> str:
    """Money in the web UI: whole dollars, so "+$88" sits next to "+$243". Cents only below $10,
    where they are a real share of the amount. Alerts keep `rules.base.usd`."""
    sign = "-" if v < 0 else ""
    a = abs(v)
    if 0 < a < 10:
        return f"{sign}${a:,.2f}"
    return f"{sign}${a:,.0f}"


def compact_usd(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"${v / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 10_000:
        return f"${v / 1000:.0f}K"
    if a >= 1000:
        return f"${v / 1000:.1f}K".replace(".0K", "K")
    return f"${v:.0f}"


def nice_ticks(peak: float, count: int = 4) -> list[float]:
    """Clean axis steps (1, 2, 2.5, 5 x 10^n) covering `peak`, starting at 0."""
    if peak <= 0:
        return [0.0, 1.0]
    raw = peak / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    top = math.ceil(peak / step) * step
    return [i * step for i in range(round(top / step) + 1)]


@dataclass
class Segment:
    key: str
    label: str
    slot: int
    value: float
    pct: float  # of the axis top


@dataclass
class Column:
    label: str
    total: float
    segments: list[Segment]  # bottom to top
    show_label: bool
    tooltip: str  # JSON for the hover layer
    axis_label: str = ""  # short form under the column; the tooltip and table keep the full label
    alt: bool = False  # every other shown label, dropped on narrow screens
    last: bool = False  # the right-most column: its label is right-anchored so it stays in the card


@dataclass
class ColumnChart:
    columns: list[Column]
    ticks: list[tuple[float, str]]  # (pct from bottom, label)
    legend: list[tuple[str, str, int]]  # (key, label, slot)


def column_chart(buckets: list[Bucket], unit: str = "day") -> ColumnChart:
    labels = dict(PRODUCT_GROUPS)
    present = [k for k, _ in PRODUCT_GROUPS if any(b.by_group.get(k) for b in buckets)]
    ticks = nice_ticks(max((b.total for b in buckets), default=0))
    top = ticks[-1]
    every = max(1, math.ceil(len(buckets) / 8))  # at most ~8 x labels
    thin = math.ceil(len(buckets) / every) > 6  # more than 6 labels: drop every other one when narrow
    cols = []
    for i, b in enumerate(buckets):
        segs = [
            Segment(k, labels[k], GROUP_SLOT[k], b.by_group[k], b.by_group[k] / top * 100)
            for k in present
            if b.by_group.get(k)
        ]
        tip = {
            "title": b.label,
            "total": usd0(b.total),
            "rows": [{"label": s.label, "value": usd0(s.value), "slot": s.slot} for s in reversed(segs)],
        }
        # Months read as "Oct"; the partial month is footnoted in the card subtitle instead.
        axis = f"{b.start:%b}" if unit == "month" else short_day(b.start) if unit == "week" else b.label
        cols.append(
            Column(
                b.label,
                b.total,
                segs,
                show_label=i % every == 0,
                tooltip=json.dumps(tip),
                axis_label=axis,
                alt=thin and (i // every) % 2 == 1 and i != len(buckets) - 1,
                last=i == len(buckets) - 1,
            )
        )
    return ColumnChart(
        columns=cols,
        ticks=[(t / top * 100, compact_usd(t)) for t in ticks],
        legend=[(k, labels[k], GROUP_SLOT[k]) for k in present],
    )


@dataclass
class BurnChart:
    actual_path: str  # in a 0..1000 x 0..100 viewBox, y down
    area_path: str
    forecast_path: str
    end_x: float  # % positions for HTML overlays
    end_y: float
    forecast_x: float
    forecast_y: float
    budget_y: float | None
    ticks: list[tuple[float, str]]
    x_labels: list[tuple[float, str]]
    points: str  # JSON [{x, label, value}] for the crosshair
    over_budget: bool
    burn: Burn = field(repr=False)


def burn_chart(burn: Burn) -> BurnChart:
    days_in_month = (burn.month_end - burn.month_start).days + 1
    peak = max(burn.forecast, burn.cumulative[-1][1], burn.budget or 0)
    ticks = nice_ticks(peak * 1.05)
    top = ticks[-1]

    def x(day_index: float) -> float:  # 0-based day of month -> 0..1000
        return day_index / max(days_in_month - 1, 1) * 1000

    def y(v: float) -> float:  # value -> 0..100, y down
        return 100 - v / top * 100

    pts = [(x(i), y(v)) for i, (_, v) in enumerate(burn.cumulative)]
    actual = "M" + " L".join(f"{px:.1f},{py:.2f}" for px, py in pts)
    area = f"{actual} L{pts[-1][0]:.1f},100 L0,100 Z"
    fx, fy = x(days_in_month - 1), y(burn.forecast)
    forecast = f"M{pts[-1][0]:.1f},{pts[-1][1]:.2f} L{fx:.1f},{fy:.2f}"
    x_labels = [
        (x(i) / 10, short_day(burn.month_start.replace(day=i + 1)))
        for i in sorted({0, (days_in_month - 1) // 2, days_in_month - 1})
    ]
    points = [
        {"x": px / 10, "label": short_day(d), "value": usd0(v)}
        for (d, v), (px, _) in zip(burn.cumulative, pts, strict=True)
    ]
    return BurnChart(
        actual_path=actual,
        area_path=area,
        forecast_path=forecast,
        end_x=pts[-1][0] / 10,
        end_y=pts[-1][1],
        forecast_x=fx / 10,
        forecast_y=fy,
        budget_y=y(burn.budget) if burn.budget else None,
        ticks=[(100 - t / top * 100, compact_usd(t)) for t in ticks],
        x_labels=x_labels,
        points=json.dumps(points),
        over_budget=bool(burn.budget and burn.forecast > burn.budget),
        burn=burn,
    )
