"""The worker loop end to end: fake workspace in, alerts out, schedules respected."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pocketwatcher import crypto
from pocketwatcher.collectors.live import ClusterState, LiveSnapshot
from pocketwatcher.db.models import AlertLog, Base, Channel, Tenant, Workspace
from pocketwatcher.worker import BILLING_INTERVAL, LIVE_INTERVAL, run_once
from tests.conftest import HOST, WORKSPACE_ID
from tests.fixtures.system_tables import ALL_PURPOSE, FakeWarehouse, SystemTables

NOW = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)  # a Monday: digest day


class FakeConnector:
    def __init__(self, tables: SystemTables, clusters: list[ClusterState] | None = None, broken: bool = False):
        self.wh = FakeWarehouse(tables)
        self.clusters = clusters or []
        self.broken = broken

    def warehouse(self, ws):
        if self.broken:
            raise RuntimeError("401 invalid client secret")
        return self.wh

    def snapshot(self, ws, now):
        return LiveSnapshot(taken_at=now, clusters=self.clusters, warehouses=[])


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        tenant = Tenant(name="Acme")
        s.add(tenant)
        s.flush()
        s.add(
            Workspace(
                tenant_id=tenant.id,
                name="acme-prod",
                host=HOST,
                databricks_workspace_id=WORKSPACE_ID,
                cloud="AZURE",
                client_id="sp",
                client_secret_enc=crypto.encrypt("secret"),
                warehouse_id="wh",
                rule_config={"budget": {"daily_usd": 500}},
            )
        )
        s.add(Channel(tenant_id=tenant.id, name="#costs", kind="slack", target_enc=crypto.encrypt("https://hook")))
        s.commit()
    return factory


def tables_with_runaway_poc() -> SystemTables:
    t = SystemTables(workspace_id=WORKSPACE_ID)
    for i in range(1, 30):
        t.add(NOW.date() - timedelta(days=i), 200, job_id="nightly", tags={"env": "prod"})
    t.add(NOW.date() - timedelta(days=1), 2000, ALL_PURPOSE, cluster_id="poc", photon=True, tags={"env": "dev"})
    return t


def test_full_cycle_sends_billing_and_live_alerts_once(sessions):
    sent = []
    forgotten = ClusterState("poc", "poc", NOW - timedelta(hours=30), 0, True, "intern@acme.com", {"env": "dev"})
    connector = FakeConnector(tables_with_runaway_poc(), [forgotten])

    assert run_once(sessions, connector, lambda k, t, m: sent.append(m.title), NOW) == 2
    titles = "\n".join(sent)
    assert "over the $500 daily budget" in titles
    assert "New spender: cluster poc" in titles
    assert "auto-terminates never" in titles
    assert "spent Oct 12 to Oct 18" in titles  # Monday digest
    # Photon-on-dev was seen by the live check and by billing, but is one alert.
    assert sum("Photon on dev" in t for t in sent) == 1

    # Nothing is due again until the intervals pass, and nothing is re-sent.
    sent.clear()
    assert run_once(sessions, connector, lambda k, t, m: sent.append(m.title), NOW + timedelta(minutes=5)) == 0
    assert run_once(sessions, connector, lambda k, t, m: sent.append(m.title), NOW + LIVE_INTERVAL) == 1
    assert sent == []

    with sessions() as s:
        ws = s.scalars(select(Workspace)).one()
        assert ws.next_billing_at.replace(tzinfo=UTC) == NOW + BILLING_INTERVAL


def test_failing_workspace_is_retried_later_and_does_not_crash_the_loop(sessions):
    connector = FakeConnector(tables_with_runaway_poc(), broken=True)
    assert run_once(sessions, connector, lambda *a: None, NOW) == 2
    with sessions() as s:
        ws = s.scalars(select(Workspace)).one()
        assert ws.next_billing_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=30)
        assert s.scalars(select(AlertLog)).all() == []
