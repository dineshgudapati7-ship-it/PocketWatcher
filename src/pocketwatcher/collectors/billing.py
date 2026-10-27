"""Pulls system.billing usage into daily per-entity cost aggregates."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pocketwatcher.databricks.client import Warehouse, load_sql
from pocketwatcher.db.models import DailyCost, Workspace
from pocketwatcher.rules.base import CostRow

log = logging.getLogger(__name__)

RECENT_DAYS = 3  # always re-read; billing records for these days are still arriving
# A year of history on first connect: system.billing.usage keeps about 365 days, and the analytics
# page compares months and quarters. Later syncs only re-read recent and corrected days.
FIRST_SYNC_DAYS = 365


@dataclass
class SyncResult:
    dates: list[date]
    rows: int
    unpriced_quantity: float


def sync_billing(session: Session, workspace: Workspace, warehouse: Warehouse, today: date) -> SyncResult:
    """Re-aggregate every usage date that may have changed and replace those days atomically."""
    if workspace.billing_synced_at is None:
        recent_from, since = today - timedelta(days=FIRST_SYNC_DAYS), today - timedelta(days=FIRST_SYNC_DAYS)
    else:
        recent_from = today - timedelta(days=RECENT_DAYS)
        since = workspace.billing_synced_at.date() - timedelta(days=1)

    ws_id = workspace.databricks_workspace_id
    dates = sorted(
        r["usage_date"]
        for r in warehouse.query(
            load_sql(
                "touched_usage_dates",
                workspace_id=ws_id,
                until=today + timedelta(days=1),
                recent_from=recent_from,
                since=since,
            )
        )
    )
    if not dates:
        workspace.billing_synced_at = datetime.now(UTC)
        session.commit()
        return SyncResult([], 0, 0.0)

    merged: dict[tuple, DailyCost] = {}
    unpriced = 0.0
    for r in warehouse.query(load_sql("daily_cost_by_entity", workspace_id=ws_id, dates=dates)):
        tags = _parse_tags(r["tags_json"])
        key = (
            r["usage_date"],
            r["sku_name"],
            r["entity_type"],
            r["entity_id"],
            r["run_as"],
            json.dumps(tags, sort_keys=True),
            bool(r["is_photon"]),
            bool(r["is_serverless"]),
        )
        unpriced += float(r["unpriced_quantity"] or 0)
        if key in merged:  # same tags serialized in a different key order
            merged[key].quantity += float(r["quantity"])
            merged[key].list_cost += float(r["list_cost"])
            continue
        merged[key] = DailyCost(
            workspace_id=workspace.id,
            usage_date=r["usage_date"],
            sku_name=r["sku_name"],
            product=r["product"],
            entity_type=r["entity_type"],
            entity_id=r["entity_id"],
            run_as=r["run_as"],
            tags=tags,
            is_photon=bool(r["is_photon"]),
            is_serverless=bool(r["is_serverless"]),
            quantity=float(r["quantity"]),
            list_cost=float(r["list_cost"]),
        )
    if unpriced:
        log.warning("workspace %s: %.2f usage units had no matching list price", workspace.id, unpriced)

    session.execute(delete(DailyCost).where(DailyCost.workspace_id == workspace.id, DailyCost.usage_date.in_(dates)))
    session.add_all(merged.values())
    workspace.billing_synced_at = datetime.now(UTC)
    session.commit()
    return SyncResult(dates, len(merged), unpriced)


def load_costs(session: Session, workspace: Workspace, start: date, end: date) -> list[CostRow]:
    """Cost rows with usage_date in [start, end), with the workspace discount applied."""
    rows = session.scalars(
        select(DailyCost).where(
            DailyCost.workspace_id == workspace.id,
            DailyCost.usage_date >= start,
            DailyCost.usage_date < end,
        )
    )
    m = workspace.discount_multiplier
    return [
        CostRow(
            usage_date=r.usage_date,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            sku_name=r.sku_name,
            cost=r.list_cost * m,
            run_as=r.run_as,
            tags=r.tags,
            is_photon=r.is_photon,
            is_serverless=r.is_serverless,
            product=r.product,
            workspace_id=r.workspace_id,
        )
        for r in rows
    ]


def _parse_tags(raw: str | dict | None) -> dict[str, str]:
    if not raw:
        return {}
    tags = json.loads(raw) if isinstance(raw, str) else raw
    return {str(k): str(v) for k, v in sorted(tags.items())}
