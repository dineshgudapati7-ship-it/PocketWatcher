from collections import defaultdict
from datetime import date

from pocketwatcher.rules.base import (
    CostRow,
    Finding,
    WorkspaceRef,
    entity_label,
    entity_link,
    usd,
)
from pocketwatcher.rules.config import DevPremiumConfig


def is_dev(tags: dict[str, str], cfg: DevPremiumConfig) -> bool:
    values = {v.lower() for v in cfg.tag_values}
    return any(tags.get(k, "").lower() in values for k in cfg.tag_keys)


def evaluate(rows: list[CostRow], cfg: DevPremiumConfig, ws: WorkspaceRef, day: date) -> list[Finding]:
    """Photon or serverless spend on dev-tagged compute during `day`."""
    spend: dict[tuple[tuple[str, str], str], float] = defaultdict(float)
    owners: dict[tuple[str, str], str | None] = {}
    for r in rows:
        if r.usage_date != day or not is_dev(r.tags, cfg):
            continue
        if cfg.flag_photon and r.is_photon:
            spend[(r.entity, "photon")] += r.cost
        if cfg.flag_serverless and r.is_serverless:
            spend[(r.entity, "serverless")] += r.cost
        owners.setdefault(r.entity, r.run_as)

    findings = []
    for (entity, feature), cost in spend.items():
        if cost < cfg.min_cost_usd:
            continue
        findings.append(
            Finding(
                rule="dev_premium",
                # No date: one alert per entity and feature, repeated daily while it persists.
                fingerprint=f"dev_premium:{ws.id}:{entity[0]}:{entity[1]}:{feature}",
                severity="warning",
                title=f"{feature.capitalize()} on dev: {entity_label(entity)} cost {usd(cost)} on {day:%b %d}",
                summary=f"This compute is tagged as dev but runs on {feature}, which is billed at a premium.",
                impact_usd=cost,
                entity=entity,
                owner=owners.get(entity),
                link=entity_link(ws.host, entity),
                details=[("Cost", usd(cost)), ("Feature", feature)],
                repeat_after_hours=24,
            )
        )
    return sorted(findings, key=lambda f: -f.impact_usd)
