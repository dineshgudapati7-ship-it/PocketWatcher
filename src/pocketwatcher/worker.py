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
    sync_billing(session, ws, connector.warehouse(ws), today)
    rows = load_costs(session, ws, *billing_window(today))
    cfg = RuleConfig.model_validate(ws.rule_config or {})
    findings = run_billing_rules(rows, cfg, _ref(ws), today, include_digest=today.weekday() == 0)
    dispatch(session, ws, findings, send, now)


def live_task(session: Session, ws: Workspace, connector: Connector, send: Sender, now: datetime) -> None:
    snap = connector.snapshot(ws, now)
    cfg = RuleConfig.model_validate(ws.rule_config or {})
    dispatch(session, ws, live.evaluate(snap, cfg, _ref(ws), recent_daily_cost(session, ws, now)), send, now)


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
                try:
                    task(session, ws, connector, send, current)
                except Exception:
                    log.exception("%s failed for workspace %s", task.__name__, ws.id)
                    session.rollback()
                    setattr(ws, column.key, current + RETRY_AFTER_ERROR)
                    session.commit()
                ran += 1
    return ran


def _ref(ws: Workspace) -> WorkspaceRef:
    return WorkspaceRef(ws.id, ws.name, ws.host)


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sessions = make_sessionmaker(make_engine())
    with httpx.Client() as http:
        send, connector = make_sender(http), DatabricksConnector()
        while True:
            if not run_once(sessions, connector, send):
                time.sleep(10)


if __name__ == "__main__":
    main()
