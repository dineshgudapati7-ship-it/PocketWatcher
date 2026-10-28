"""Background worker: billing syncs, live checks and alert dispatch for every active workspace.

Run with `python -m pocketwatcher.worker`. Any number of workers can run side by side: each
due task is claimed with SELECT ... FOR UPDATE SKIP LOCKED and its next run time is pushed
forward before the work starts, so a workspace is never processed twice at once.
"""

import logging
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from pocketwatcher import crypto
from pocketwatcher.alerts.dispatcher import Sender, dispatch, make_sender
from pocketwatcher.collectors.billing import load_costs, sync_billing
from pocketwatcher.collectors.live import LiveSnapshot, take_snapshot
from pocketwatcher.databricks.client import DatabricksWarehouse, Warehouse
from pocketwatcher.db.models import Workspace
from pocketwatcher.db.session import make_engine, make_sessionmaker
from pocketwatcher.rules import live
from pocketwatcher.rules.base import WorkspaceRef
from pocketwatcher.rules.config import RuleConfig
from pocketwatcher.rules.engine import billing_window, run_billing_rules

log = logging.getLogger(__name__)

# Billing data is hours behind, so syncing more often only costs the customer warehouse time.
BILLING_INTERVAL = timedelta(hours=6)
LIVE_INTERVAL = timedelta(minutes=15)
RETRY_AFTER_ERROR = timedelta(minutes=30)
BURN_DAYS = 7
# "Sync now": each billing sync wakes the customer's SQL warehouse, so manual syncs are spaced out.
MANUAL_SYNC_COOLDOWN = timedelta(minutes=5)
# A claimed billing sync that has not finished after this long is taken to have died with its worker.
SYNC_PRESUMED_LOST = timedelta(minutes=30)


class Connector(Protocol):
    def warehouse(self, ws: Workspace) -> Warehouse: ...
    def snapshot(self, ws: Workspace, now: datetime) -> LiveSnapshot: ...


class DatabricksConnector:
    def warehouse(self, ws: Workspace) -> Warehouse:
        return DatabricksWarehouse.connect(ws.host, ws.client_id, crypto.decrypt(ws.client_secret_enc), ws.warehouse_id)

    def snapshot(self, ws: Workspace, now: datetime) -> LiveSnapshot:
        wh = DatabricksWarehouse.connect(ws.host, ws.client_id, crypto.decrypt(ws.client_secret_enc), ws.warehouse_id)
        return take_snapshot(wh.client, now)


def billing_task(session: Session, ws: Workspace, connector: Connector, send: Sender, now: datetime) -> None:
    today = now.date()
    synced = sync_billing(session, ws, connector.warehouse(ws), today)
    rows = load_costs(session, ws, *billing_window(today))
    cfg = RuleConfig.model_validate(ws.rule_config or {})
    findings = run_billing_rules(rows, cfg, _ref(ws), today, include_digest=today.weekday() == 0)
    sent = dispatch(session, ws, findings, send, now)
    log.info(
        "billing sync ok: workspace %s '%s': %d day(s) read, %d cost row(s), %d finding(s), "
        "%d alert(s) sent, %d failed",
        ws.id,
        ws.name,
        len(synced.dates),
        synced.rows,
        len(findings),
        sent.sent,
        sent.failed,
    )
    if synced.unpriced_quantity:
        log.warning(
            "workspace %s: %.2f units had no list price, so their cost counts as $0", ws.id, synced.unpriced_quantity
        )


def live_task(session: Session, ws: Workspace, connector: Connector, send: Sender, now: datetime) -> None:
    snap = connector.snapshot(ws, now)
    cfg = RuleConfig.model_validate(ws.rule_config or {})
    findings = live.evaluate(snap, cfg, _ref(ws), recent_daily_cost(session, ws, now))
    sent = dispatch(session, ws, findings, send, now)
    log.info(
        "live check ok: workspace %s '%s': %d running cluster(s), %d running warehouse(s), %d finding(s), "
        "%d alert(s) sent",
        ws.id,
        ws.name,
        len(snap.clusters),
        len(snap.warehouses),
        len(findings),
        sent.sent,
    )


def recent_daily_cost(session: Session, ws: Workspace, now: datetime) -> dict[tuple[str, str], float]:
    """Average $/day over the days each entity actually ran in the last week. Averaging over
    all 7 days would understate a cluster started yesterday, and make the billing view of the
    same problem look like a 7x escalation."""
    today = now.date()
    by_day: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(float))
    for r in load_costs(session, ws, today - timedelta(days=BURN_DAYS), today):
        by_day[r.entity][r.usage_date] += r.cost
    return {e: sum(days.values()) / len(days) for e, days in by_day.items()}


Task = Callable[[Session, Workspace, Connector, Sender, datetime], None]
# Billing first, so live alerts on a newly connected workspace already show recent cost.
TASKS: list[tuple[InstrumentedAttribute, timedelta, Task]] = [
    (Workspace.next_billing_at, BILLING_INTERVAL, billing_task),
    (Workspace.next_live_at, LIVE_INTERVAL, live_task),
]


def run_once(sessions: sessionmaker, connector: Connector, send: Sender, now: datetime | None = None) -> int:
    """Claim and run every task that is due; returns how many ran."""
    ran = 0
    for column, interval, task in TASKS:
        while True:
            current = now or datetime.now(UTC)
            with sessions() as session:
                ws = session.scalars(
                    select(Workspace)
                    .where(Workspace.active.is_(True), or_(column.is_(None), column <= current))
                    .order_by(column.nulls_first())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ).first()
                if ws is None:
                    break
                setattr(ws, column.key, current + interval)
                session.commit()  # releases the row lock; the pushed-forward time keeps others off it
                ws_id = ws.id
                try:
                    task(session, ws, connector, send, current)
                except Exception:
                    log.exception("%s failed for workspace %s", task.__name__, ws_id)
                    session.rollback()
                    # Removed by its owner while the task ran: nothing left to retry. Ask the
                    # database, not the session, which may still hold the deleted row.
                    if session.scalar(select(Workspace.id).where(Workspace.id == ws_id)) is not None:
                        setattr(ws, column.key, current + RETRY_AFTER_ERROR)
                        session.commit()
                ran += 1
    return ran


def sync_refusal(ws: Workspace, now: datetime) -> str | None:
    """Why a manual sync can't be queued right now, or None if it can. Read from the schedule
    columns alone: a due next_billing_at is a sync waiting for a worker; a next_billing_at one
    interval after a claim newer than the last finished sync is a sync still running."""
    next_billing, synced = _utc(ws.next_billing_at), _utc(ws.billing_synced_at)
    if next_billing is None or next_billing <= now:
        return "A sync is already queued; the worker picks it up within a minute if it is running."
    claimed = next_billing - BILLING_INTERVAL
    if now - SYNC_PRESUMED_LOST < claimed <= now and (synced is None or claimed > synced):
        return f"A sync started at {claimed:%H:%M} UTC and has not finished yet."
    if synced is not None and now < synced + MANUAL_SYNC_COOLDOWN:
        ready = synced + MANUAL_SYNC_COOLDOWN
        if ready.second or ready.microsecond:  # round up: "after 08:20" must really mean allowed then
            ready = ready.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return f"A sync ran at {synced:%H:%M} UTC; try again after {ready:%H:%M} UTC."
    return None


def queue_sync(ws: Workspace, now: datetime) -> None:
    """Make the billing sync and live check due now. The worker claims them as usual, so a
    manual sync never runs in the web process or alongside a worker's own run of the same task."""
    ws.next_billing_at = now
    ws.next_live_at = now


def _utc(value: datetime | None) -> datetime | None:
    # SQLite hands back naive datetimes; they are stored as UTC.
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _ref(ws: Workspace) -> WorkspaceRef:
    return WorkspaceRef(ws.id, ws.name, ws.host)


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and a Slack or Teams webhook URL is a secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log.info("worker started: billing every %s, live checks every %s", BILLING_INTERVAL, LIVE_INTERVAL)
    sessions = make_sessionmaker(make_engine())
    with httpx.Client() as http:
        send, connector = make_sender(http), DatabricksConnector()
        while True:
            if not run_once(sessions, connector, send):
                time.sleep(10)


if __name__ == "__main__":
    main()
