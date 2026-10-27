from datetime import timedelta
from typing import Any

from pocketwatcher.collectors.live import LiveSnapshot
from pocketwatcher.rules.base import Finding, WorkspaceRef, entity_link, usd
from pocketwatcher.rules.config import RuleConfig
from pocketwatcher.rules.dev_premium import is_dev

# Entity-state findings repeat daily while the condition persists, unless muted.
REPEAT_HOURS = 24


def evaluate(
    snap: LiveSnapshot, cfg: RuleConfig, ws: WorkspaceRef, daily_cost: dict[tuple[str, str], float]
) -> list[Finding]:
    """Rules on current cluster/warehouse state. `daily_cost` is each entity's recent average
    estimated $/day from billing, used only to show what the problem is likely costing."""
    c = cfg.compute
    findings: list[Finding] = []

    for cl in snap.clusters:
        entity = ("cluster", cl.cluster_id)
        common: dict[str, Any] = {
            "entity": entity,
            "owner": cl.creator,
            "link": entity_link(ws.host, entity),
            "repeat_after_hours": REPEAT_HOURS,
        }
        burn = daily_cost.get(entity)
        burn_detail = [("Recent cost", f"{usd(burn)}/day")] if burn else []
        hours = (snap.taken_at - cl.running_since) / timedelta(hours=1) if cl.running_since else 0.0

        if cl.autotermination_minutes == 0 or cl.autotermination_minutes > c.max_autotermination_minutes:
            setting = "never" if cl.autotermination_minutes == 0 else f"after {cl.autotermination_minutes} min"
            findings.append(
                Finding(
                    rule="cluster.autotermination",
                    fingerprint=f"cluster.autotermination:{ws.id}:{cl.cluster_id}",
                    severity="critical" if cl.autotermination_minutes == 0 else "warning",
                    title=f"Cluster '{cl.name}' is running and auto-terminates {setting}",
                    summary=(
                        "An idle all-purpose cluster keeps billing until it terminates. "
                        f"Set auto-termination to {c.max_autotermination_minutes} minutes or less."
                    ),
                    impact_usd=burn or 0.0,
                    details=[("Auto-terminate", setting), ("Running for", f"{hours:.1f} h"), *burn_detail],
                    **common,
                )
            )
        # Only when auto-termination looks fine: otherwise that alert already explains it.
        elif hours >= c.long_running_hours:
            findings.append(
                Finding(
                    rule="cluster.long_running",
                    fingerprint=f"cluster.long_running:{ws.id}:{cl.cluster_id}",
                    severity="warning",
                    title=f"Cluster '{cl.name}' has been running for {hours:.0f} hours",
                    summary="Clusters running this long are often forgotten, or kept alive by an idle notebook.",
                    impact_usd=burn or 0.0,
                    details=[("Running since", f"{cl.running_since:%b %d %H:%M} UTC"), *burn_detail],
                    **common,
                )
            )
        if cfg.dev_premium.flag_photon and cl.is_photon and is_dev(cl.tags, cfg.dev_premium):
            findings.append(_dev_premium(ws, entity, cl.name, "photon", cl.creator, burn))

    for wh in snap.warehouses:
        entity = ("warehouse", wh.warehouse_id)
        burn = daily_cost.get(entity)
        if wh.auto_stop_mins == 0 or wh.auto_stop_mins > c.max_warehouse_auto_stop_minutes:
            setting = "never" if wh.auto_stop_mins == 0 else f"after {wh.auto_stop_mins} min"
            findings.append(
                Finding(
                    rule="warehouse.auto_stop",
                    fingerprint=f"warehouse.auto_stop:{ws.id}:{wh.warehouse_id}",
                    severity="critical" if wh.auto_stop_mins == 0 else "warning",
                    title=f"SQL warehouse '{wh.name}' is running and auto-stops {setting}",
                    summary=(
                        f"Warehouses bill while idle until they stop. Set auto-stop to "
                        f"{c.max_warehouse_auto_stop_minutes} minutes or less."
                    ),
                    impact_usd=burn or 0.0,
                    entity=entity,
                    owner=wh.creator,
                    link=entity_link(ws.host, entity),
                    details=[("Auto-stop", setting), ("Size", wh.size or "?")],
                    repeat_after_hours=REPEAT_HOURS,
                )
            )
        if cfg.dev_premium.flag_serverless and wh.is_serverless and is_dev(wh.tags, cfg.dev_premium):
            findings.append(_dev_premium(ws, entity, wh.name, "serverless", wh.creator, burn))
    return findings


def _dev_premium(
    ws: WorkspaceRef, entity: tuple[str, str], name: str, feature: str, owner: str | None, burn: float | None
) -> Finding:
    # Same fingerprint as the billing-side rule, so one problem never alerts twice.
    return Finding(
        rule="dev_premium",
        fingerprint=f"dev_premium:{ws.id}:{entity[0]}:{entity[1]}:{feature}",
        severity="warning",
        title=f"{feature.capitalize()} on dev: {entity[0]} '{name}' is running",
        summary=f"This compute is tagged as dev but runs on {feature}, which is billed at a premium.",
        impact_usd=burn or 0.0,
        entity=entity,
        owner=owner,
        link=entity_link(ws.host, entity),
        details=[("Feature", feature)] + ([("Recent cost", f"{usd(burn)}/day")] if burn else []),
        repeat_after_hours=REPEAT_HOURS,
    )
