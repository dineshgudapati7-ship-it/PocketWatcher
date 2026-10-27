"""Exact, copy-pasteable setup steps for a customer's admin. Everything granted is read-only."""

import re
from dataclasses import dataclass

# Schemas PocketWatcher reads. compute/lakeflow are for names and live context, billing for cost.
SYSTEM_SCHEMAS = ("billing", "compute", "lakeflow")

_APP_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass
class SetupSteps:
    grants_sql: str
    warehouse_json: str
    warehouse_permission_json: str


def setup_steps(application_id: str) -> SetupSteps:
    """`application_id` is the service principal's application (client) ID, a UUID."""
    if not _APP_ID.match(application_id.lower()):
        raise ValueError("expected the service principal's application ID (a UUID)")
    principal = f"`{application_id.lower()}`"
    grants = [
        "-- Run as a metastore admin in the SQL editor. Grants are read-only (USE + SELECT).",
        "-- Note: system.billing holds usage for every workspace in the account; PocketWatcher",
        "-- only queries rows for the workspace you connect.",
        f"GRANT USE CATALOG ON CATALOG system TO {principal};",
    ]
    for schema in SYSTEM_SCHEMAS:
        grants += [
            f"GRANT USE SCHEMA ON SCHEMA system.{schema} TO {principal};",
            f"GRANT SELECT ON SCHEMA system.{schema} TO {principal};",
        ]
    warehouse = """{
  "name": "pocketwatcher",
  "cluster_size": "2X-Small",
  "enable_serverless_compute": true,
  "warehouse_type": "PRO",
  "auto_stop_mins": 1,
  "max_num_clusters": 1,
  "tags": {"custom_tags": [{"key": "purpose", "value": "pocketwatcher"}]}
}"""
    permission = f"""{{
  "access_control_list": [
    {{"service_principal_name": "{application_id.lower()}", "permission_level": "CAN_USE"}}
  ]
}}"""
    return SetupSteps("\n".join(grants), warehouse, permission)
