"""Web app flows with a fake identity provider, fake preflight and an outbox sender."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pocketwatcher import crypto
from pocketwatcher.db.models import Base, Channel, User, Workspace
from pocketwatcher.onboarding.preflight import Check, PreflightResult
from pocketwatcher.web.app import create_app
from pocketwatcher.web.auth import Identity
from tests.conftest import WORKSPACE_ID

APP_ID = "8d3c1f2a-1b2c-4d5e-8f90-0123456789ab"
HOST = "https://adb-1234567890123456.7.azuredatabricks.net"
SLACK = "https://hooks.slack.com/services/T000/B000/XXXX"
# Same wording as pocketwatcher.onboarding.preflight produces.
USAGE_DENIED = "[INSUFFICIENT_PERMISSIONS] User does not have USE CATALOG on Catalog 'system'."
AUTO_STOP_30 = "'pocketwatcher' auto-stops after 30 min, so each sync keeps it running that long."


class FakeAuth:
    def authorization_url(self, state, redirect_uri):
        return f"https://auth.example/authorize?state={state}&redirect_uri={redirect_uri}"

    def authenticate(self, code):
        user_id, email = code.split(":")
        return Identity(user_id=user_id, email=email, name=None, session_id=f"session_{user_id}")

    def logout_url(self, session_id, return_to):
        return f"https://auth.example/logout?session_id={session_id}&return_to={return_to}"


class FakePreflight:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.workspace_id = WORKSPACE_ID

    def __call__(self, host, client_id, secret, warehouse_id, today):
        self.calls.append((host, client_id, secret, warehouse_id))
        if self.fail:
            return PreflightResult(
                [
                    Check(
                        "Read system.billing.usage",
                        "fail",
                        USAGE_DENIED,
                        "Run the GRANT statements from the setup step as a metastore admin.",
                    )
                ]
            )
        return PreflightResult(
            [
                Check("Service principal login", "ok", "ok"),
                Check(
                    "SQL warehouse access",
                    "warn",
                    AUTO_STOP_30,
                    "Set auto-stop to 1 minute; PocketWatcher queries take seconds.",
                ),
            ],
            workspace_id=self.workspace_id,
            cloud="AZURE",
        )


@pytest.fixture
def env():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    preflight, outbox = FakePreflight(), []
    app = create_app(sessions, FakeAuth(), preflight, lambda kind, target, msg: outbox.append((kind, target, msg)))
    return sessions, app, preflight, outbox


def sign_in(app, code="user_a:alice@acme.com") -> TestClient:
    client = TestClient(app, base_url="https://testserver")  # session cookie is https-only
    login = client.get("/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    assert client.get("/callback", params={"code": code, "state": state}, follow_redirects=False).status_code == 303
    return client


def csrf(client: TestClient, path: str = "/") -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def connect(client, **overrides):
    form = {
        "name": "acme-prod",
        "host": HOST,
        "client_id": APP_ID,
        "client_secret": "dose-secret-1",
        "warehouse_id": "wh1",
        "csrf": csrf(client, "/workspaces/new"),
        **overrides,
    }
    return client.post("/workspaces", data=form, follow_redirects=False)


# --- auth -------------------------------------------------------------------


def test_signed_out_sees_landing_and_protected_pages_redirect_to_login(env):
    _, app, _, _ = env
    client = TestClient(app, base_url="https://testserver")
    assert "Sign in" in client.get("/").text
    r = client.get("/channels", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_callback_rejects_a_forged_state(env):
    _, app, _, _ = env
    client = TestClient(app, base_url="https://testserver")
    client.get("/login", follow_redirects=False)
    assert client.get("/callback", params={"code": "u:x@y.com", "state": "forged"}).status_code == 400


def test_first_sign_in_creates_a_tenant_and_second_reuses_it(env):
    sessions, app, _, _ = env
    sign_in(app)
    sign_in(app)
    with sessions() as s:
        (user,) = s.scalars(select(User)).all()
        assert user.email == "alice@acme.com" and user.tenant_id


def test_post_without_csrf_token_is_rejected(env):
    _, app, _, _ = env
    client = sign_in(app)
    r = client.post("/channels", data={"kind": "slack", "name": "x", "target": SLACK})
    assert r.status_code == 403


def test_sign_out_also_ends_the_identity_provider_session(env):
    _, app, _, _ = env
    client = sign_in(app)
    r = client.post("/logout", data={"csrf": csrf(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert (
        r.headers["location"]
        == "https://auth.example/logout?session_id=session_user_a&return_to=https://app.pocketwatcher.test/"
    )
    # Our own session is gone regardless of what the provider does next.
    assert client.get("/channels", follow_redirects=False).headers["location"] == "/login"


def test_session_id_is_read_from_the_access_token_sid_claim():
    import base64
    import json

    from pocketwatcher.web.auth import session_id_from

    claims = base64.urlsafe_b64encode(json.dumps({"sid": "session_01ABC", "sub": "user_1"}).encode()).decode()
    assert session_id_from(f"eyJhbGciOiJSUzI1NiJ9.{claims.rstrip('=')}.signature") == "session_01ABC"
    assert session_id_from("not-a-jwt") is None
    assert session_id_from(None) is None


# --- connect wizard -----------------------------------------------------------


def test_setup_commands_are_shown_for_an_application_id(env):
    _, app, _, _ = env
    page = sign_in(app).get("/workspaces/new", params={"app_id": APP_ID}).text
    assert f"GRANT SELECT ON SCHEMA system.billing TO `{APP_ID}`;" in page


def test_failed_preflight_saves_nothing_and_never_echoes_the_secret(env):
    sessions, app, preflight, _ = env
    client = sign_in(app)
    preflight.fail = True
    r = connect(client)
    assert r.status_code == 422 and "INSUFFICIENT_PERMISSIONS" in r.text and "Run the GRANT statements" in r.text
    assert "dose-secret-1" not in r.text
    with sessions() as s:
        assert s.scalars(select(Workspace)).all() == []


def test_successful_connect_stores_encrypted_secret_and_detected_ids(env):
    sessions, app, preflight, _ = env
    client = sign_in(app)
    r = connect(client, host="adb-1234567890123456.7.azuredatabricks.net/")
    assert r.status_code == 303
    page = client.get(r.headers["location"]).text
    assert "Connected." in page and "auto-stops after 30 min" in page  # warnings are shown after connecting
    assert preflight.calls[0][0] == HOST  # normalized before use
    with sessions() as s:
        ws = s.scalars(select(Workspace)).one()
        assert ws.databricks_workspace_id == WORKSPACE_ID and ws.cloud == "AZURE"
        assert ws.client_secret_enc != "dose-secret-1" and crypto.decrypt(ws.client_secret_enc) == "dose-secret-1"
        assert ws.next_billing_at is None  # due immediately for the worker


@pytest.mark.parametrize(
    "host",
    [
        "http://adb-1.azuredatabricks.net",
        "https://169.254.169.254",
        "https://azuredatabricks.net.evil.com",
        "https://adb-1.azuredatabricks.net:8443",
    ],
)
def test_non_databricks_hosts_are_rejected_before_any_request(env, host):
    _, app, preflight, _ = env
    r = connect(sign_in(app), host=host)
    assert r.status_code == 400 and preflight.calls == []


def test_settings_are_saved_and_invalid_values_are_not(env):
    sessions, app, _, _ = env
    client = sign_in(app)
    url = connect(client).headers["location"]
    token = csrf(client, url)
    client.post(
        f"{url}/settings",
        data={
            "csrf": token,
            "daily_usd": "$1,500",
            "monthly_usd": "30000",
            "required_tags": "team, cost_center",
            "discount_percent": "20",
        },
    )
    with sessions() as s:
        ws = s.scalars(select(Workspace)).one()
        assert ws.rule_config["budget"] == {"daily_usd": 1500.0, "monthly_usd": 30000.0}
        assert ws.rule_config["untagged"]["required_tags"] == ["team", "cost_center"]
        assert ws.discount_multiplier == pytest.approx(0.8)

    r = client.post(f"{url}/settings", data={"csrf": token, "daily_usd": "-5"})  # follows the redirect
    assert "Not saved: Budgets must be positive." in r.text
    with sessions() as s:
        assert s.scalars(select(Workspace)).one().rule_config["budget"]["daily_usd"] == 1500.0


def test_a_rejected_setting_comes_back_as_typed_and_marked_invalid(env):
    _, app, _, _ = env
    client = sign_in(app)
    url = connect(client).headers["location"]
    form = {"csrf": csrf(client, url), "monthly_usd": "25,000", "daily_usd": "abc", "required_tags": "team"}
    page = client.post(f"{url}/settings", data=form).text
    assert "Not saved: &#39;abc&#39; is not a number." in page
    daily = re.search(r'<input id="daily_usd"[^>]*>', page).group(0)
    assert 'value="abc"' in daily and 'aria-invalid="true"' in daily
    assert 'aria-describedby="daily_usd-error daily_usd-hint"' in daily
    assert '<span class="field-error" id="daily_usd-error">&#39;abc&#39; is not a number.</span>' in page
    monthly = re.search(r'<input id="monthly_usd"[^>]*>', page).group(0)
    assert 'value="25,000"' in monthly and "aria-invalid" not in monthly  # the other values are kept too
    assert 'value="team"' in page
    # Shown once: a reload is back to the saved settings.
    again = client.get(url).text
    assert "aria-invalid" not in again and 'value="abc"' not in again


def test_a_sync_that_found_no_billing_records_is_not_shown_as_zero_spend(env):
    from datetime import UTC, datetime

    sessions, app, _, _ = env
    client = sign_in(app)
    url = connect(client).headers["location"]
    assert "Awaiting sync" in client.get(url).text and "First sync within minutes" in client.get(url).text
    with sessions() as s:
        s.scalars(select(Workspace)).one().billing_synced_at = datetime.now(UTC)
        s.commit()
    page = client.get(url).text
    assert "No billing data yet" in page and "Databricks has no billing records yet" in page
    assert "$0" not in page


def test_connecting_the_same_workspace_again_keeps_one_copy(env):
    sessions, app, _, _ = env
    client = sign_in(app)
    first = connect(client).headers["location"]
    r = connect(client, name="second try", host=HOST + "/")
    assert r.status_code == 303 and r.headers["location"] == first
    assert "already connected as &#39;acme-prod&#39;" in client.get(first).text
    with sessions() as s:
        assert len(s.scalars(select(Workspace)).all()) == 1


def test_removing_a_workspace_deletes_its_data_but_keeps_shared_channels(env):
    from datetime import UTC, date, datetime

    from pocketwatcher.db.models import AlertLog, DailyCost, Mute

    sessions, app, preflight, _ = env
    client = sign_in(app)
    url = connect(client).headers["location"]
    preflight.workspace_id = WORKSPACE_ID + 1
    other_url = connect(client, name="acme-dev", host="https://adb-99.7.azuredatabricks.net").headers["location"]
    assert other_url != url
    ws_id, other_id = int(url.rsplit("/", 1)[1]), int(other_url.rsplit("/", 1)[1])
    now = datetime.now(UTC)
    with sessions() as s:
        shared = Channel(tenant_id=1, name="#all", kind="slack", target_enc=crypto.encrypt(SLACK))
        scoped = Channel(tenant_id=1, workspace_id=ws_id, name="#prod", kind="slack", target_enc=crypto.encrypt(SLACK))
        s.add_all([shared, scoped])
        s.flush()
        for wid in (ws_id, other_id):
            s.add(
                DailyCost(
                    workspace_id=wid,
                    usage_date=date(2026, 10, 1),
                    sku_name="S",
                    entity_type="job",
                    entity_id="j",
                    tags={},
                    quantity=1,
                    list_cost=10,
                )
            )
            s.add(Mute(workspace_id=wid, fingerprint="f", until=now))
            s.add(
                AlertLog(
                    workspace_id=wid,
                    channel_id=shared.id,
                    fingerprint="f",
                    rule="r",
                    severity="info",
                    impact_usd=1,
                    title="t",
                    status="sent",
                    created_at=now,
                )
            )
        s.add(
            AlertLog(
                workspace_id=ws_id,
                channel_id=scoped.id,
                fingerprint="g",
                rule="r",
                severity="info",
                impact_usd=1,
                title="t",
                status="sent",
                created_at=now,
            )
        )
        s.commit()

    r = client.post(f"{url}/delete", data={"csrf": csrf(client, url), "confirm": "acme-pro"})
    assert "Not removed: type the workspace name exactly." in r.text
    r = client.post(f"{url}/delete", data={"csrf": csrf(client, url), "confirm": " acme-prod "})
    assert "Removed &#39;acme-prod&#39;" in r.text and f"service principal {APP_ID}" in r.text
    assert client.get(url).status_code == 404

    with sessions() as s:
        assert [w.id for w in s.scalars(select(Workspace))] == [other_id]
        assert [c.name for c in s.scalars(select(Channel))] == ["#all"]
        for model in (DailyCost, Mute, AlertLog):
            assert {row.workspace_id for row in s.scalars(select(model))} == {other_id}


# --- channels ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("slack", "https://hooks.slack.com.evil.com/services/x"),
        ("slack", "http://hooks.slack.com/services/x"),
        ("teams", "https://10.0.0.5/hook"),
        ("teams", "https://user:pw@prod-1.westus.logic.azure.com/x"),
        ("email", "not-an-email"),
    ],
)
def test_unsafe_or_invalid_targets_are_rejected(env, kind, target):
    sessions, app, _, _ = env
    client = sign_in(app)
    client.post("/channels", data={"csrf": csrf(client), "kind": kind, "name": "x", "target": target})
    with sessions() as s:
        assert s.scalars(select(Channel)).all() == []


def test_channel_add_test_and_remove(env):
    sessions, app, _, outbox = env
    client = sign_in(app)
    connect(client)
    client.post("/channels", data={"csrf": csrf(client), "kind": "slack", "name": "#costs", "target": SLACK})
    with sessions() as s:
        ch = s.scalars(select(Channel)).one()
        assert crypto.decrypt(ch.target_enc) == SLACK

    page = client.post(f"/channels/{ch.id}/test", data={"csrf": csrf(client)}).text
    assert "Test alert sent to &#39;#costs&#39;." in page
    assert outbox[0][:2] == ("slack", SLACK) and outbox[0][2].title == "PocketWatcher is connected to acme-prod"

    client.post(f"/channels/{ch.id}/delete", data={"csrf": csrf(client)})
    with sessions() as s:
        assert s.scalars(select(Channel)).one().enabled is False


def test_failed_test_alert_shows_the_error(env):
    sessions, _, _, _ = env
    app_fail = create_app(
        sessions, FakeAuth(), FakePreflight(), lambda *a: (_ for _ in ()).throw(RuntimeError("404 no_service"))
    )
    client = sign_in(app_fail)
    client.post("/channels", data={"csrf": csrf(client), "kind": "slack", "name": "#c", "target": SLACK})
    with sessions() as s:
        ch_id = s.scalars(select(Channel.id)).one()
    assert "failed: 404 no_service" in client.post(f"/channels/{ch_id}/test", data={"csrf": csrf(client)}).text


def test_tenants_cannot_see_or_touch_each_others_resources(env):
    sessions, app, _, _ = env
    alice = sign_in(app)
    ws_url = connect(alice).headers["location"]
    alice.post("/channels", data={"csrf": csrf(alice), "kind": "slack", "name": "#a", "target": SLACK})
    with sessions() as s:
        ch_id = s.scalars(select(Channel.id)).one()

    bob = sign_in(app, "user_b:bob@other.com")
    assert bob.get(ws_url).status_code == 404
    assert bob.post(f"{ws_url}/settings", data={"csrf": csrf(bob), "daily_usd": "1"}).status_code == 404
    assert bob.post(f"{ws_url}/delete", data={"csrf": csrf(bob), "confirm": "acme-prod"}).status_code == 404
    assert alice.post(f"{ws_url}/delete", data={"confirm": "acme-prod"}).status_code == 403  # no CSRF token
    assert bob.post(f"/channels/{ch_id}/test", data={"csrf": csrf(bob)}).status_code == 404
    assert bob.post(f"/channels/{ch_id}/delete", data={"csrf": csrf(bob)}).status_code == 404
    assert "acme-prod" not in bob.get("/").text
    assert "<td>#a</td>" not in bob.get("/channels").text


# --- sync now ---------------------------------------------------------------------


SYNC_OFF = 'aria-describedby="sync-hint" disabled'


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def test_sync_now_queues_both_tasks_and_the_worker_runs_them(env):
    from datetime import date

    from pocketwatcher.db.models import DailyCost
    from pocketwatcher.worker import BILLING_INTERVAL, LIVE_INTERVAL, run_once
    from tests.fixtures.system_tables import SystemTables
    from tests.integration.test_worker import FakeConnector

    sessions, app, _, _ = env
    client = sign_in(app)
    url = connect(client).headers["location"]
    now = datetime.now(UTC)
    last_sync = now - timedelta(hours=1)
    with sessions() as s:  # a scheduled sync ran an hour ago; nothing is due for a while
        ws = s.scalars(select(Workspace)).one()
        ws.billing_synced_at, ws.next_billing_at, ws.next_live_at = (
            last_sync,
            now + timedelta(hours=5),
            now + LIVE_INTERVAL,
        )
        s.commit()
    page = client.get(url).text
    assert "Sync now" in page and SYNC_OFF not in page
    assert "Databricks writes billing records hours after usage" in page

    page = client.post(f"{url}/sync", data={"csrf": csrf(client, url)}).text
    assert "Sync queued. The worker picks it up within a minute if it is running" in page
    assert "A sync is already queued" in page and SYNC_OFF in page  # the button is off and says why
    with sessions() as s:
        ws = s.scalars(select(Workspace)).one()
        after = datetime.now(UTC)
        assert _utc(ws.next_billing_at) <= after and _utc(ws.next_live_at) <= after

    # A second click before the worker gets to it doesn't queue anything more.
    page = client.post(f"{url}/sync", data={"csrf": csrf(client, url)}).text
    assert "A sync is already queued; the worker picks it up within a minute if it is running." in page

    tables = SystemTables(workspace_id=WORKSPACE_ID)
    tables.add(date.today() - timedelta(days=1), 100)
    assert run_once(sessions, FakeConnector(tables), lambda k, t, m: None) == 2  # billing and live
    with sessions() as s:
        ws = s.scalars(select(Workspace)).one()
        synced = _utc(ws.billing_synced_at)
        assert synced > last_sync
        assert _utc(ws.next_billing_at) > now + BILLING_INTERVAL - timedelta(minutes=1)
        assert _utc(ws.next_live_at) > now + LIVE_INTERVAL - timedelta(minutes=1)
        assert s.scalars(select(DailyCost)).first() is not None

    # Straight after a sync, another one is refused until the cool-down has passed.
    page = client.post(f"{url}/sync", data={"csrf": csrf(client, url)}).text
    assert f"A sync ran at {synced:%H:%M} UTC; try again after" in page
    with sessions() as s:
        assert _utc(s.scalars(select(Workspace)).one().next_billing_at) > now + timedelta(hours=5)


def test_sync_now_is_tenant_scoped_and_needs_a_csrf_token(env):
    sessions, app, _, _ = env
    alice = sign_in(app)
    url = connect(alice).headers["location"]
    later = datetime.now(UTC) + timedelta(hours=5)
    with sessions() as s:
        s.scalars(select(Workspace)).one().next_billing_at = later
        s.commit()
    bob = sign_in(app, "user_b:bob@other.com")
    assert bob.post(f"{url}/sync", data={"csrf": csrf(bob)}).status_code == 404
    assert alice.post(f"{url}/sync", data={}).status_code == 403
    with sessions() as s:
        assert _utc(s.scalars(select(Workspace)).one().next_billing_at) == later  # nothing queued


SYNC_NOW = datetime(2026, 10, 19, 8, 17, tzinfo=UTC)


def _at(hh_mm: str, day_offset: timedelta = timedelta(0)) -> datetime:
    h, m = hh_mm.split(":")
    return SYNC_NOW.replace(hour=int(h), minute=int(m), second=0) + day_offset


@pytest.mark.parametrize(
    ("synced", "next_billing", "expected"),
    [
        # New workspace, overdue schedule, or a click already waiting for the worker.
        (None, None, "A sync is already queued"),
        (_at("02:00"), SYNC_NOW, "A sync is already queued"),
        (_at("02:00"), _at("07:00"), "A sync is already queued"),
        # Claimed (next pushed one interval ahead) after the last finished sync: still running.
        (_at("02:00"), _at("08:16") + timedelta(hours=6), "A sync started at 08:16 UTC and has not finished yet."),
        (None, _at("08:10") + timedelta(hours=6), "A sync started at 08:10 UTC and has not finished yet."),
        # Claimed so long ago that its worker must have died: allowed again.
        (_at("02:00"), _at("07:40") + timedelta(hours=6), None),
        # Last sync failed and is waiting to retry: allowed.
        (_at("02:00"), SYNC_NOW + timedelta(minutes=20), None),
        # Finished within the cool-down. "after 08:21" is rounded up from 08:20:30.
        (_at("08:15"), _at("08:15") + timedelta(hours=6), "A sync ran at 08:15 UTC; try again after 08:20 UTC."),
        (
            _at("08:15") + timedelta(seconds=30),
            _at("08:15") + timedelta(hours=6),
            "A sync ran at 08:15 UTC; try again after 08:21 UTC.",
        ),
        # Naive datetimes, as SQLite returns them, are read as UTC.
        (
            _at("08:15").replace(tzinfo=None),
            (_at("08:15") + timedelta(hours=6)).replace(tzinfo=None),
            "A sync ran at 08:15 UTC; try again after 08:20 UTC.",
        ),
        # Cool-down over: exactly five minutes, and more.
        (_at("08:12"), _at("08:12") + timedelta(hours=6), None),
        (_at("03:00"), _at("03:00") + timedelta(hours=6), None),
    ],
)
def test_when_a_manual_sync_is_refused(synced, next_billing, expected):
    from pocketwatcher.worker import sync_refusal

    refusal = sync_refusal(Workspace(billing_synced_at=synced, next_billing_at=next_billing), SYNC_NOW)
    if expected is None:
        assert refusal is None
    else:
        assert refusal is not None and refusal.startswith(expected)


# --- analytics ------------------------------------------------------------------


def seed_costs(sessions, ws_id: int):
    from datetime import UTC, datetime, timedelta

    from pocketwatcher.db.models import DailyCost

    today = datetime.now(UTC).date()
    with sessions() as s:
        ws = s.get(Workspace, ws_id)
        ws.billing_synced_at = datetime.now(UTC)
        for i in range(1, 40):
            d = today - timedelta(days=i)
            s.add(
                DailyCost(
                    workspace_id=ws_id,
                    usage_date=d,
                    sku_name="PREMIUM_JOBS_COMPUTE",
                    product="JOBS",
                    entity_type="job",
                    entity_id="nightly-etl",
                    run_as="etl@acme.com",
                    tags={"team": "data"},
                    quantity=10,
                    list_cost=100,
                )
            )
            s.add(
                DailyCost(
                    workspace_id=ws_id,
                    usage_date=d,
                    sku_name="SERVERLESS_SQL",
                    product="SQL",
                    entity_type="warehouse",
                    entity_id="bi",
                    run_as='=HYPERLINK("http://x")',
                    tags={},
                    is_serverless=True,
                    quantity=1,
                    list_cost=50,
                )
            )
        s.commit()


def test_analytics_shows_breakdowns_for_the_tenant(env):
    sessions, app, _, _ = env
    client = sign_in(app)
    ws_id = int(connect(client).headers["location"].rsplit("/", 1)[1])
    seed_costs(sessions, ws_id)

    page = client.get("/analytics", params={"range": "30d"}).text
    assert "Cost analytics" in page and "$4,500" in page  # 30 days x $150
    assert "Spend by team" in page and "Untagged" in page and "33%" in page  # $1,500 of $4,500 untagged
    assert "SQL warehouses" in page and "job nightly-etl" in page
    assert "View as table" in page


def test_analytics_and_export_are_tenant_scoped(env):
    sessions, app, _, _ = env
    alice = sign_in(app)
    ws_id = int(connect(alice).headers["location"].rsplit("/", 1)[1])
    seed_costs(sessions, ws_id)
    bob = sign_in(app, "user_b:bob@other.com")
    assert bob.get("/analytics", params={"ws": ws_id}).status_code == 404
    assert bob.get("/analytics/export.csv", params={"ws": ws_id}).status_code == 404
    assert "nightly-etl" not in bob.get("/analytics/export.csv").text


def test_csv_export_has_every_row_and_neutralises_formulas(env):
    import csv as csv_mod
    import io

    sessions, app, _, _ = env
    client = sign_in(app)
    ws_id = int(connect(client).headers["location"].rsplit("/", 1)[1])
    seed_costs(sessions, ws_id)

    r = client.get("/analytics/export.csv", params={"range": "30d"})
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="pocketwatcher-costs-' in r.headers["content-disposition"]
    rows = list(csv_mod.DictReader(io.StringIO(r.text)))
    assert len(rows) == 60  # 30 days x 2 resources
    assert sum(float(x["estimated_cost_usd"]) for x in rows) == pytest.approx(4500)
    owners = {x["owner"] for x in rows}
    assert '\'=HYPERLINK("http://x")' in owners  # leading quote stops spreadsheets running it


def test_analytics_without_workspaces_shows_how_to_start(env):
    _, app, _, _ = env
    page = sign_in(app).get("/analytics").text
    assert "No data yet" in page and "Connect a workspace" in page
