from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from databricks.sdk.service import sql
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pocketwatcher import crypto
from pocketwatcher.alerts.links import mute_token
from pocketwatcher.db.models import AlertLog, Base, Channel, Mute, Tenant, Workspace
from pocketwatcher.onboarding.preflight import cloud_from_host, run_preflight
from pocketwatcher.onboarding.setup_sql import setup_steps
from pocketwatcher.onboarding.test_alert import send_test_alert
from pocketwatcher.web.app import create_app
from tests.conftest import HOST, WORKSPACE_ID
from tests.fixtures.system_tables import FakeWarehouse

APP_ID = "8d3c1f2a-1b2c-4d5e-8f90-0123456789ab"
TODAY = date(2026, 10, 20)


# --- setup steps ------------------------------------------------------------


def test_setup_grants_are_read_only_and_scoped_to_the_principal():
    steps = setup_steps(APP_ID.upper())
    lines = [line for line in steps.grants_sql.splitlines() if not line.startswith("--")]
    assert lines[0] == f"GRANT USE CATALOG ON CATALOG system TO `{APP_ID}`;"
    assert all(line.startswith(("GRANT USE ", "GRANT SELECT ")) for line in lines)
    assert f"GRANT SELECT ON SCHEMA system.billing TO `{APP_ID}`;" in lines
    assert '"auto_stop_mins": 1' in steps.warehouse_json
    assert '"permission_level": "CAN_USE"' in steps.warehouse_permission_json


def test_setup_rejects_anything_but_an_application_id():
    with pytest.raises(ValueError):
        setup_steps("x`; DROP TABLE y; --")


# --- preflight --------------------------------------------------------------


def fake_client(*, auth_error=None, wh: sql.GetWarehouseResponse | None = None, clusters_error=None):
    def me():
        if auth_error:
            raise auth_error
        return SimpleNamespace(display_name="pocketwatcher-sp", user_name=APP_ID)

    def clusters_list():
        if clusters_error:
            raise clusters_error
        return iter([])

    return SimpleNamespace(
        current_user=SimpleNamespace(me=me),
        get_workspace_id=lambda: WORKSPACE_ID,
        warehouses=SimpleNamespace(
            get=lambda _id: (
                wh
                or sql.GetWarehouseResponse(
                    name="pocketwatcher", cluster_size="2X-Small", auto_stop_mins=1, enable_serverless_compute=True
                )
            ),
            list=lambda: iter([]),
        ),
        clusters=SimpleNamespace(list=clusters_list),
    )


class DeniedWarehouse:
    def query(self, sql_text):
        raise RuntimeError("[INSUFFICIENT_PERMISSIONS] User does not have USE CATALOG on Catalog 'system'.")


def statuses(result):
    return {c.name: c.status for c in result.checks}


def test_preflight_passes_and_detects_workspace_and_cloud(tables):
    tables.add(date(2026, 10, 19), 10, job_id="j1")
    result = run_preflight(fake_client(), FakeWarehouse(tables), "wh", HOST, TODAY)
    assert result.ok and set(statuses(result).values()) == {"ok"}
    assert (result.workspace_id, result.cloud) == (WORKSPACE_ID, "AZURE")


def test_preflight_stops_at_bad_credentials_with_a_fix():
    result = run_preflight(fake_client(auth_error=RuntimeError("invalid_client")), DeniedWarehouse(), "wh", HOST, TODAY)
    (check,) = result.checks
    assert check.status == "fail" and "OAuth secret" in check.fix
    assert not result.ok


def test_preflight_reports_missing_grants(tables):
    result = run_preflight(fake_client(), DeniedWarehouse(), "wh", HOST, TODAY)
    s = statuses(result)
    assert s["Read system.billing.usage"] == "fail" and s["Read system.billing.list_prices"] == "fail"
    assert "GRANT" in next(c.fix for c in result.checks if c.name == "Read system.billing.usage")


def test_preflight_warns_about_an_expensive_warehouse_and_empty_usage(tables):
    wh = sql.GetWarehouseResponse(name="big", cluster_size="Large", auto_stop_mins=0, enable_serverless_compute=True)
    result = run_preflight(fake_client(wh=wh), FakeWarehouse(tables), "wh", HOST, TODAY)
    s = statuses(result)
    assert s["SQL warehouse access"] == "warn" and s["Read system.billing.usage"] == "warn"
    assert result.ok  # warnings don't block connecting


def test_preflight_reports_missing_workspace_entitlement(tables):
    result = run_preflight(fake_client(clusters_error=RuntimeError("403")), FakeWarehouse(tables), "wh", HOST, TODAY)
    assert statuses(result)["List clusters"] == "fail"


@pytest.mark.parametrize(
    ("host", "cloud"),
    [
        ("https://adb-1.2.azuredatabricks.net", "AZURE"),
        ("https://1.gcp.databricks.com", "GCP"),
        ("https://dbc-1.cloud.databricks.com", "AWS"),
    ],
)
def test_cloud_from_host(host, cloud):
    assert cloud_from_host(host) == cloud


# --- test alert + mute flow ---------------------------------------------------


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
                id=7,
                tenant_id=tenant.id,
                name="acme-prod",
                host=HOST,
                databricks_workspace_id=WORKSPACE_ID,
                cloud="AZURE",
                client_id="sp",
                client_secret_enc="x",
                warehouse_id="wh",
            )
        )
        s.commit()
    return factory


def test_test_alert_is_sent_to_the_decrypted_target(sessions):
    sent = []
    with sessions() as s:
        ws = s.get(Workspace, 7)
        ch = Channel(tenant_id=ws.tenant_id, name="#c", kind="slack", target_enc=crypto.encrypt("https://hook"))
        send_test_alert(ch, ws, lambda kind, target, msg: sent.append((kind, target, msg.title)))
    assert sent == [("slack", "https://hook", "PocketWatcher is connected to acme-prod")]


def test_mute_link_get_only_confirms_and_post_mutes(sessions):
    client = TestClient(create_app(sessions))
    token = mute_token(7, "anomaly:7:job:j1:2026-10-19")

    page = client.get("/mute", params={"t": token})
    assert page.status_code == 200 and "Mute this alert for 7 days?" in page.text
    with sessions() as s:
        assert s.scalars(select(Mute)).all() == []  # a link preview must not mute

    done = client.post("/mute", data={"t": token})
    assert done.status_code == 200 and "Alert muted" in done.text
    with sessions() as s:
        mute = s.scalars(select(Mute)).one()
        assert mute.fingerprint == "anomaly:7:job:j1:2026-10-19"
        remaining = mute.until.replace(tzinfo=UTC) - datetime.now(UTC)
        assert timedelta(days=7) - timedelta(minutes=1) < remaining <= timedelta(days=7)


def test_mute_page_names_the_alert_it_mutes(sessions):
    fingerprint = "anomaly:7:job:j1:2026-10-19"
    with sessions() as s:
        ch = Channel(tenant_id=1, name="#c", kind="slack", target_enc="x")
        s.add(ch)
        s.flush()
        for hours, title in (
            (30, "Spend spike: job j1 cost $90 on Oct 18"),
            (2, "Spend spike: job j1 cost $640 on Oct 19"),
        ):
            s.add(
                AlertLog(
                    workspace_id=7,
                    channel_id=ch.id,
                    fingerprint=fingerprint,
                    rule="anomaly",
                    severity="warning",
                    impact_usd=640,
                    title=title,
                    status="sent",
                    created_at=datetime.now(UTC) - timedelta(hours=hours),
                )
            )
        s.commit()
    client = TestClient(create_app(sessions))
    token = mute_token(7, fingerprint)
    page = client.get("/mute", params={"t": token}).text
    assert "Spend spike: job j1 cost $640 on Oct 19" in page and "$90" not in page  # the latest send
    assert 'href="/">Open PocketWatcher' in page
    assert "Spend spike: job j1 cost $640 on Oct 19" in client.post("/mute", data={"t": token}).text


def test_mute_page_without_a_logged_alert_still_works(sessions):
    page = TestClient(create_app(sessions)).get("/mute", params={"t": mute_token(7, "budget:7:month")})
    assert page.status_code == 200 and "Mute this alert for 7 days?" in page.text and "muted-alert" not in page.text


def test_tampered_mute_link_is_rejected(sessions):
    client = TestClient(create_app(sessions))
    assert client.get("/mute", params={"t": "garbage"}).status_code == 400
    assert client.post("/mute", data={"t": "garbage"}).status_code == 400


@pytest.mark.parametrize(
    ("auto_stop", "expected"),
    [
        (0, "'big' never auto-stops, so it bills while idle."),
        (30, "'big' auto-stops after 30 min, so each sync keeps it running that long."),
    ],
)
def test_warehouse_auto_stop_warning_reads_as_a_sentence(tables, auto_stop, expected):
    wh = sql.GetWarehouseResponse(
        name="big", cluster_size="Small", auto_stop_mins=auto_stop, enable_serverless_compute=True
    )
    result = run_preflight(fake_client(wh=wh), FakeWarehouse(tables), "wh", HOST, TODAY)
    check = next(c for c in result.checks if c.name == "SQL warehouse access")
    assert (check.status, check.detail) == ("warn", expected)
