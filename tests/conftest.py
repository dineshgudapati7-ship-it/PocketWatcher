from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pocketwatcher.db.models import Base, Tenant, Workspace
from tests.fixtures.system_tables import SystemTables

WORKSPACE_ID = 1234567890123456  # Azure-style 16-digit id
HOST = "https://adb-1234567890.1.azuredatabricks.net"


TEST_FERNET_KEY = "x4m2b3R1Jx0kQmQ5dS0yZ2V0a2V5Zm9ydGVzdHMxMjM="


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    from pocketwatcher.settings import get_settings

    monkeypatch.setenv("PW_FERNET_KEY", TEST_FERNET_KEY)
    monkeypatch.setenv("PW_APP_BASE_URL", "https://app.pocketwatcher.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def session() -> Iterator[Session]:
    # SQLite locally; CI also runs the suite against Postgres via PW_TEST_DATABASE_URL.
    import os

    engine = create_engine(os.environ.get("PW_TEST_DATABASE_URL", "sqlite://"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s
    Base.metadata.drop_all(engine)


@pytest.fixture
def workspace(session: Session) -> Workspace:
    tenant = Tenant(name="Acme Data")
    ws = Workspace(
        tenant=tenant,
        name="acme-prod",
        host=HOST,
        databricks_workspace_id=WORKSPACE_ID,
        cloud="AZURE",
        client_id="sp-client",
        client_secret_enc="x",
        warehouse_id="wh1",
        discount_multiplier=1.0,
        rule_config={},
    )
    session.add_all([tenant, ws])
    session.commit()
    return ws


@pytest.fixture
def tables() -> SystemTables:
    return SystemTables(workspace_id=WORKSPACE_ID)
