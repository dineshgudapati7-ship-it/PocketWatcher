from collections import defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Any

from pocketwatcher.rules.base import (
    CostRow,
    Finding,
    WorkspaceRef,
    entity_label,
    entity_link,
    usd,
)
from pocketwatcher.rules.config import AnomalyConfig

BASELINE_DAYS = 7
MAD_SCALE = 1.4826  # makes MAD comparable to a standard deviation for normal data


def evaluate(rows: list[CostRow], cfg: AnomalyConfig, ws: WorkspaceRef, day: date) -> list[Finding]:
    """Flag entities whose cost on `day` is far above their own recent baseline.

    Baseline is the median of the previous 7 days (missing days count as $0) with MAD-based
    spread, which a single earlier spike can't inflate. Weekly jobs are protected by also
    requiring the day to exceed the same weekday in the previous two weeks.
    """
    daily: dict[tuple[str, str], dict[date, float]] = defaultdict(lambda: defaultdict(float))
    owners: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        if r.usage_date <= day:
            daily[r.entity][r.usage_date] += r.cost
            if r.usage_date == day and r.run_as:
                owners[r.entity][r.run_as] += r.cost

    findings = []
    for entity, costs in daily.items():
        today = costs.get(day, 0.0)
        if today < cfg.min_cost_usd:
            continue
        owner_costs = owners[entity]
        owner = max(owner_costs, key=lambda o: owner_costs[o]) if owner_costs else None
        first_seen = min(d for d, c in costs.items() if c > 0)
        history_days = (day - first_seen).days
        common: dict[str, Any] = {
            "entity": entity,
            "owner": owner,
            "link": entity_link(ws.host, entity),
            "fingerprint": f"anomaly:{ws.id}:{entity[0]}:{entity[1]}:{day}",
        }

        if history_days < cfg.min_history_days:
            if today >= cfg.new_entity_usd:
                findings.append(
                    Finding(
                        rule="anomaly.new",
                        severity="critical" if today >= 5 * cfg.new_entity_usd else "warning",
                        title=f"New spender: {entity_label(entity)} cost {usd(today)} on {day:%b %d}",
                        summary=(
                            f"First seen {history_days} day(s) ago and already above {usd(cfg.new_entity_usd)}/day."
                        ),
                        impact_usd=today,
                        details=[("Cost", usd(today)), ("First seen", f"{first_seen:%b %d}")],
                        **common,
                    )
                )
            continue

        window = [costs.get(day - timedelta(days=i), 0.0) for i in range(1, BASELINE_DAYS + 1)]
        med = median(window)
        mad = median(abs(x - med) for x in window) * MAD_SCALE
        same_weekday = max(costs.get(day - timedelta(days=7), 0.0), costs.get(day - timedelta(days=14), 0.0))
        if today <= med + cfg.k_mad * mad or today < cfg.min_ratio * med or today < cfg.min_ratio * same_weekday:
            continue
        excess = today - med
        findings.append(
            Finding(
                rule="anomaly.spike",
                severity="critical" if excess >= 500 else "warning",
                title=f"Spend spike: {entity_label(entity)} cost {usd(today)} on {day:%b %d}",
                summary=(
                    f"{today / med:.1f}x its 7-day median of {usd(med)}."
                    if med
                    else f"Usually near $0 over the last 7 days; {usd(excess)} above baseline."
                ),
                impact_usd=excess,
                details=[
                    ("Cost", usd(today)),
                    ("7-day median", usd(med)),
                    ("Same weekday, prior 2 wks", usd(same_weekday)),
                ],
                **common,
            )
        )
    return sorted(findings, key=lambda f: -f.impact_usd)
