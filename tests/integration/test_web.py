"""Web app flows with a fake identity provider, fake preflight and an outbox sender."""

import re
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
            workspace_id=WORKSPACE_ID,
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
    assert bob.post(f"/channels/{ch_id}/test", data={"csrf": csrf(bob)}).status_code == 404
    assert bob.post(f"/channels/{ch_id}/delete", data={"csrf": csrf(bob)}).status_code == 404
    assert "acme-prod" not in bob.get("/").text
    assert "<td>#a</td>" not in bob.get("/channels").text


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
