"""Access to a customer's Databricks workspace as a read-only service principal."""

import json
import time
from datetime import date, datetime
from decimal import Decimal
from importlib import resources
from typing import Any, Protocol

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format, StatementResponse, StatementState


class Warehouse(Protocol):
    def query(self, sql: str) -> list[dict[str, Any]]: ...


def load_sql(name: str, **params: Any) -> str:
    """Load a packaged query and render its parameters as SQL literals."""
    template = resources.files("pocketwatcher.databricks.sql").joinpath(f"{name}.sql").read_text()
    return template.format(**{k: sql_literal(v) for k, v in params.items()})


def sql_literal(value: Any) -> str:
    # Only typed values are accepted, so nothing user-supplied is ever spliced in as text.
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, date) and not isinstance(value, datetime):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError("empty list literal")
        return ", ".join(sql_literal(v) for v in value)
    raise TypeError(f"unsupported SQL literal type: {type(value).__name__}")


class StatementFailed(RuntimeError):
    pass


class DatabricksWarehouse:
    """Runs SQL through the Statement Execution API on the customer's SQL warehouse."""

    def __init__(self, client: WorkspaceClient, warehouse_id: str, timeout_s: int = 600):
        self.client = client
        self.warehouse_id = warehouse_id
        self.timeout_s = timeout_s

    @classmethod
    def connect(cls, host: str, client_id: str, client_secret: str, warehouse_id: str) -> "DatabricksWarehouse":
        client = WorkspaceClient(host=host, client_id=client_id, client_secret=client_secret, auth_type="oauth-m2m")
        return cls(client, warehouse_id)

    def query(self, sql: str) -> list[dict[str, Any]]:
        api = self.client.statement_execution
        resp = api.execute_statement(
            statement=sql,
            warehouse_id=self.warehouse_id,
            wait_timeout="30s",
            disposition=Disposition.INLINE,
            format=Format.JSON_ARRAY,
        )
        statement_id = resp.statement_id or ""
        deadline = time.monotonic() + self.timeout_s
        while _state(resp) in (StatementState.PENDING, StatementState.RUNNING):
            if time.monotonic() > deadline:
                api.cancel_execution(statement_id)
                raise StatementFailed(f"statement {statement_id} timed out")
            time.sleep(2)
            resp = api.get_statement(statement_id)
        if _state(resp) != StatementState.SUCCEEDED:
            error = resp.status.error if resp.status else None
            raise StatementFailed(error.message if error and error.message else f"statement {_state(resp)}")

        schema = resp.manifest.schema if resp.manifest else None
        schema_columns = (schema.columns if schema else None) or []
        columns = [(c.name or "", c.type_name.value if c.type_name else "STRING") for c in schema_columns]
        rows: list[dict[str, Any]] = []
        result = resp.result
        while result is not None:
            for raw in result.data_array or []:
                rows.append({name: _convert(v, t) for (name, t), v in zip(columns, raw, strict=True)})
            if result.next_chunk_index is None:
                break
            result = api.get_statement_result_chunk_n(statement_id, result.next_chunk_index)
        return rows


def _state(resp: StatementResponse) -> StatementState | None:
    return resp.status.state if resp.status else None


def _convert(value: str | None, type_name: str) -> Any:
    """JSON_ARRAY results arrive as strings; restore the column's type."""
    if value is None:
        return None
    match type_name:
        case "BOOLEAN":
            return value.lower() == "true"
        case "BYTE" | "SHORT" | "INT" | "LONG":
            return int(value)
        case "FLOAT" | "DOUBLE":
            return float(value)
        case "DECIMAL":
            return float(Decimal(value))
        case "DATE":
            return date.fromisoformat(value)
        case "TIMESTAMP":
            return datetime.fromisoformat(value)
        case "MAP" | "STRUCT" | "ARRAY":
            return json.loads(value)
        case _:
            return value
