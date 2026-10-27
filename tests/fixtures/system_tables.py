"""Schema-accurate synthetic system tables, served from DuckDB.

Column names and nesting follow the documented system.billing.usage / list_prices schemas,
so the production SQL in pocketwatcher/databricks/sql runs unchanged against them.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import duckdb

USAGE_DDL = """
CREATE TABLE fake_system.billing.usage (
  record_id VARCHAR,
  account_id VARCHAR,
  workspace_id BIGINT,
  sku_name VARCHAR,
  cloud VARCHAR,
  usage_start_time TIMESTAMPTZ,
  usage_end_time TIMESTAMPTZ,
  usage_date DATE,
  custom_tags MAP(VARCHAR, VARCHAR),
  usage_unit VARCHAR,
  usage_quantity DECIMAL(38, 18),
  usage_metadata STRUCT(
    cluster_id VARCHAR, job_id VARCHAR, warehouse_id VARCHAR, instance_pool_id VARCHAR,
    node_type VARCHAR, job_run_id VARCHAR, notebook_id VARCHAR, dlt_pipeline_id VARCHAR,
    endpoint_name VARCHAR, endpoint_id VARCHAR
  ),
  identity_metadata STRUCT(run_as VARCHAR, owned_by VARCHAR, created_by VARCHAR),
  record_type VARCHAR,
  ingestion_date DATE,
  billing_origin_product VARCHAR,
  product_features STRUCT(
    jobs_tier VARCHAR, sql_tier VARCHAR, dlt_tier VARCHAR, is_serverless BOOLEAN, is_photon BOOLEAN
  ),
  usage_type VARCHAR
)
"""

PRICES_DDL = """
CREATE TABLE fake_system.billing.list_prices (
  price_start_time TIMESTAMPTZ,
  price_end_time TIMESTAMPTZ,
  account_id VARCHAR,
  sku_name VARCHAR,
  cloud VARCHAR,
  currency_code VARCHAR,
  usage_unit VARCHAR,
  pricing STRUCT("default" DECIMAL(30, 18), promotional STRUCT("default" DECIMAL(30, 18)),
                 effective_list STRUCT("default" DECIMAL(30, 18)))
)
"""

METADATA_KEYS = (
    "cluster_id",
    "job_id",
    "warehouse_id",
    "instance_pool_id",
    "node_type",
    "job_run_id",
    "notebook_id",
    "dlt_pipeline_id",
    "endpoint_name",
    "endpoint_id",
)

JOBS_COMPUTE = "PREMIUM_JOBS_COMPUTE"
JOBS_PHOTON = "PREMIUM_JOBS_COMPUTE_(PHOTON)"
ALL_PURPOSE = "PREMIUM_ALL_PURPOSE_COMPUTE"
SERVERLESS_SQL = "PREMIUM_SERVERLESS_SQL_COMPUTE_US_EAST"

DEFAULT_PRICES = {JOBS_COMPUTE: 0.30, JOBS_PHOTON: 0.30, ALL_PURPOSE: 0.55, SERVERLESS_SQL: 0.70}


@dataclass
class SystemTables:
    workspace_id: int = 1234567890
    cloud: str = "AZURE"
    usage: list[dict[str, Any]] = field(default_factory=list)
    prices: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.prices:
            for sku, price in DEFAULT_PRICES.items():
                self.price(sku, price, start=datetime(2020, 1, 1))

    def price(self, sku: str, per_dbu: float, start: datetime, end: datetime | None = None) -> None:
        self.prices.append(
            {
                "price_start_time": start,
                "price_end_time": end,
                "account_id": "acct",
                "sku_name": sku,
                "cloud": self.cloud,
                "currency_code": "USD",
                "usage_unit": "DBU",
                "pricing": {
                    "default": per_dbu,
                    "promotional": {"default": per_dbu},
                    "effective_list": {"default": per_dbu},
                },
            }
        )

    def add(
        self,
        day: date,
        dbus: float,
        sku: str = JOBS_COMPUTE,
        *,
        tags: dict[str, str] | None = None,
        run_as: str = "etl@example.com",
        photon: bool = False,
        serverless: bool = False,
        product: str = "JOBS",
        hour: int = 2,
        ingested: date | None = None,
        record_type: str = "ORIGINAL",
        workspace_id: int | None = None,
        **metadata: str,
    ) -> None:
        start = datetime.combine(day, time(hour))
        self.usage.append(
            {
                "record_id": str(uuid.uuid4()),
                "account_id": "acct",
                "workspace_id": workspace_id or self.workspace_id,
                "sku_name": sku,
                "cloud": self.cloud,
                "usage_start_time": start,
                "usage_end_time": start + timedelta(hours=1),
                "usage_date": day,
                "custom_tags": tags or {},
                "usage_unit": "DBU",
                "usage_quantity": dbus,
                "usage_metadata": {k: metadata.get(k) for k in METADATA_KEYS},
                "identity_metadata": {"run_as": run_as, "owned_by": None, "created_by": None},
                "record_type": record_type,
                "ingestion_date": ingested or day,
                "billing_origin_product": product,
                "product_features": {
                    "jobs_tier": None,
                    "sql_tier": None,
                    "dlt_tier": None,
                    "is_serverless": serverless,
                    "is_photon": photon,
                },
                "usage_type": "COMPUTE_TIME",
            }
        )


class FakeWarehouse:
    """Implements pocketwatcher.databricks.client.Warehouse on top of DuckDB."""

    def __init__(self, tables: SystemTables):
        self.con = duckdb.connect()
        # DuckDB reserves the catalog name "system", so the tables live in "fake_system" and
        # queries have only that catalog prefix rewritten; the rest of the SQL runs as written.
        self.con.execute("ATTACH ':memory:' AS fake_system")
        self.con.execute("CREATE SCHEMA fake_system.billing")
        self.con.execute(USAGE_DDL)
        self.con.execute(PRICES_DDL)
        self.statements: list[str] = []
        self.load(tables)

    def load(self, tables: SystemTables) -> None:
        self.con.execute("DELETE FROM fake_system.billing.usage")
        self.con.execute("DELETE FROM fake_system.billing.list_prices")
        for row in tables.usage:
            tags = row["custom_tags"]
            values = {**row, "tag_keys": list(tags), "tag_values": list(tags.values())}
            del values["custom_tags"]
            cols = [c for c in values if c not in ("tag_keys", "tag_values")]
            self.con.execute(
                f"INSERT INTO fake_system.billing.usage ({', '.join(cols)}, custom_tags) "
                f"VALUES ({', '.join('$' + c for c in cols)}, MAP($tag_keys::VARCHAR[], $tag_values::VARCHAR[]))",
                values,
            )
        for row in tables.prices:
            cols = list(row)
            self.con.execute(
                f"INSERT INTO fake_system.billing.list_prices ({', '.join(cols)}) "
                f"VALUES ({', '.join('$' + c for c in cols)})",
                row,
            )

    def query(self, sql: str) -> list[dict[str, Any]]:
        self.statements.append(sql)
        cur = self.con.execute(re.sub(r"\bsystem\.", "fake_system.", sql))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
