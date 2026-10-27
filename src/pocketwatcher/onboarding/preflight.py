"""Checks run when a workspace is connected, each with a concrete fix when it fails."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from databricks.sdk.service import sql

from pocketwatcher.databricks.client import Warehouse, load_sql

Status = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str | None = None


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)
    workspace_id: int | None = None
    cloud: str | None = None

    @property
    def ok(self) -> bool:
        return all(c.status != "fail" for c in self.checks)


def cloud_from_host(host: str) -> str:
    if "azuredatabricks.net" in host:
        return "AZURE"
    if "gcp.databricks.com" in host:
        return "GCP"
    return "AWS"


# The server authenticates against this host with the customer's secret, so only real
# Databricks workspace domains are accepted (no internal addresses, no lookalike domains).
WORKSPACE_DOMAINS = (".azuredatabricks.net", ".cloud.databricks.com", ".gcp.databricks.com")


def normalize_host(raw: str) -> str:
    raw = raw.strip()
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or not host.endswith(WORKSPACE_DOMAINS):
        raise ValueError("Enter your workspace URL, e.g. https://adb-1234567890123456.7.azuredatabricks.net")
    return f"https://{host}"


def run_preflight(client: Any, warehouse: Warehouse, warehouse_id: str, host: str, today: date) -> PreflightResult:
    """`client` is a databricks.sdk WorkspaceClient authenticated as the service principal."""
    result = PreflightResult(cloud=cloud_from_host(host))
    checks = result.checks

    try:
        me = client.current_user.me()
        result.workspace_id = int(client.get_workspace_id())
        checks.append(Check("Service principal login", "ok", f"Signed in as {me.display_name or me.user_name}"))
    except Exception as exc:
        checks.append(
            Check(
                "Service principal login",
                "fail",
                _short(exc),
                "Check the workspace URL, client ID and OAuth secret. Secrets expire; create a new one.",
            )
        )
        return result  # nothing else can work

    try:
        wh = client.warehouses.get(warehouse_id)
        checks.append(_warehouse_check(wh))
    except Exception as exc:
        checks.append(
            Check("SQL warehouse access", "fail", _short(exc), "Grant the service principal CAN USE on the warehouse.")
        )
        return result

    try:
        rows = warehouse.query(
            load_sql("preflight_usage", workspace_id=result.workspace_id, since=today - timedelta(days=7))
        )
        n, latest = rows[0]["records"], rows[0]["latest"]
        if n:
            checks.append(Check("Read system.billing.usage", "ok", f"{n:,} records in 7 days, latest {latest}"))
        else:
            checks.append(
                Check(
                    "Read system.billing.usage",
                    "warn",
                    "No usage for this workspace in 7 days.",
                    "Normal for a brand-new workspace. Otherwise, check that this is the right workspace.",
                )
            )
    except Exception as exc:
        checks.append(
            Check(
                "Read system.billing.usage",
                "fail",
                _short(exc),
                "Run the GRANT statements from the setup step as a metastore admin.",
            )
        )

    try:
        warehouse.query("SELECT sku_name FROM system.billing.list_prices LIMIT 1")
        checks.append(Check("Read system.billing.list_prices", "ok", "Prices available"))
    except Exception as exc:
        checks.append(
            Check(
                "Read system.billing.list_prices",
                "fail",
                _short(exc),
                "Run the GRANT statements from the setup step as a metastore admin.",
            )
        )

    for name, call in (
        ("List clusters", lambda: next(iter(client.clusters.list()), None)),
        ("List SQL warehouses", lambda: next(iter(client.warehouses.list()), None)),
    ):
        try:
            call()
            checks.append(Check(name, "ok", "Allowed"))
        except Exception as exc:
            checks.append(
                Check(
                    name,
                    "fail",
                    _short(exc),
                    "Add the service principal to the workspace with the Workspace access entitlement.",
                )
            )
    return result


def _warehouse_check(wh: sql.GetWarehouseResponse) -> Check:
    name = "SQL warehouse access"
    stop = wh.auto_stop_mins or 0
    if not wh.enable_serverless_compute:
        return Check(
            name,
            "warn",
            f"'{wh.name}' is not serverless, so every sync waits for it to start.",
            "Use a 2X-Small serverless warehouse with 1-minute auto-stop (about $10/month).",
        )
    if stop == 0 or stop > 10:
        return Check(
            name,
            "warn",
            f"'{wh.name}' never auto-stops, so it bills while idle."
            if stop == 0
            else f"'{wh.name}' auto-stops after {stop} min, so each sync keeps it running that long.",
            "Set auto-stop to 1 minute; PocketWatcher queries take seconds.",
        )
    return Check(name, "ok", f"'{wh.name}' ({wh.cluster_size}, serverless, auto-stop {stop} min)")


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text[:300]
