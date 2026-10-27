from collections import defaultdict
from datetime import date, timedelta

from pocketwatcher.rules.base import CostRow, Finding, WorkspaceRef, entity_label, usd
from pocketwatcher.rules.config import UntaggedConfig

WINDOW_DAYS = 7


def evaluate(rows: list[CostRow], cfg: UntaggedConfig, ws: WorkspaceRef, day: date) -> list[Finding]:
    """Share of the last 7 days of spend missing any required tag (e.g. team, cost_center)."""
    if not cfg.required_tags:
        return []
    start = day - timedelta(days=WINDOW_DAYS - 1)
    window = [r for r in rows if start <= r.usage_date <= day]
    total = sum(r.cost for r in window)
    offenders: dict[tuple[str, str], float] = defaultdict(float)
    for r in window:
        if any(not r.tags.get(t) for t in cfg.required_tags):
            offenders[r.entity] += r.cost
    untagged = sum(offenders.values())
    if not total or untagged < cfg.min_cost_usd or untagged / total <= cfg.max_share:
        return []

    top = sorted(offenders.items(), key=lambda kv: -kv[1])[:5]
    year, week, _ = day.isocalendar()
    return [
        Finding(
            rule="untagged",
            fingerprint=f"untagged:{ws.id}:{year}-W{week:02d}",
            severity="warning",
            title=f"{ws.name}: {untagged / total:.0%} of the last 7 days of spend ({usd(untagged)}) is untagged",
            summary=(
                f"Missing one of the required tags: {', '.join(cfg.required_tags)}. "
                "Untagged spend can't be charged back to a team."
            ),
            impact_usd=untagged,
            details=[(entity_label(e), usd(c)) for e, c in top],
            data={"share": untagged / total},
        )
    ]
