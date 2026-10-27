from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from pocketwatcher.rules.base import (
    CostRow,
    Finding,
    WorkspaceRef,
    entity_label,
    entity_link,
    usd,
)


def evaluate(rows: list[CostRow], ws: WorkspaceRef, day: date, top_n: int = 10) -> Finding:
    """Weekly top-N cost drivers for the 7 days ending `day`, with week-over-week change."""
    this_start, prev_start = day - timedelta(days=6), day - timedelta(days=13)
    this_week: dict[tuple[str, str], float] = defaultdict(float)
    prev_week: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        if this_start <= r.usage_date <= day:
            this_week[r.entity] += r.cost
        elif prev_start <= r.usage_date < this_start:
            prev_week[r.entity] += r.cost
    total, prev_total = sum(this_week.values()), sum(prev_week.values())
    top = sorted(this_week.items(), key=lambda kv: -kv[1])[:top_n]

    offenders: list[dict[str, Any]] = [
        {
            "entity": e,
            "label": entity_label(e),
            "cost": c,
            "prev_cost": prev_week.get(e, 0.0),
            "link": entity_link(ws.host, e),
        }
        for e, c in top
    ]
    change = f"{(total - prev_total) / prev_total:+.0%} vs prior week" if prev_total else "no prior week"
    year, week, _ = day.isocalendar()
    return Finding(
        rule="digest",
        fingerprint=f"digest:{ws.id}:{year}-W{week:02d}",
        severity="info",
        title=f"{ws.name}: {usd(total)} spent {this_start:%b %d} to {day:%b %d} ({change})",
        summary=f"Top {len(offenders)} cost drivers this week.",
        impact_usd=total,
        details=[(o["label"], f"{usd(o['cost'])} (was {usd(o['prev_cost'])})") for o in offenders],
        data={"total": total, "prev_total": prev_total, "offenders": offenders},
    )
