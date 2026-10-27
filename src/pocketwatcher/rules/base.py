from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

Severity = Literal["info", "warning", "critical"]

ENTITY_PATHS = {
    "job": "/jobs/{id}",
    "cluster": "/compute/clusters/{id}",
    "warehouse": "/sql/warehouses/{id}",
    "pipeline": "/pipelines/{id}",
    "endpoint": "/ml/endpoints/{id}",
}


@dataclass(frozen=True)
class CostRow:
    """One day of estimated cost for one entity/SKU, after the workspace discount."""

    usage_date: date
    entity_type: str
    entity_id: str
    sku_name: str
    cost: float
    run_as: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    is_photon: bool = False
    is_serverless: bool = False
    product: str | None = None  # billing_origin_product, e.g. JOBS, SQL, ALL_PURPOSE
    workspace_id: int | None = None  # our workspace id, set when rows from several are combined

    @property
    def entity(self) -> tuple[str, str]:
        return (self.entity_type, self.entity_id)


@dataclass
class Finding:
    rule: str
    fingerprint: str
    severity: Severity
    title: str
    summary: str  # why it fired, in one or two sentences
    impact_usd: float = 0.0
    entity: tuple[str, str] | None = None
    owner: str | None = None
    link: str | None = None
    details: list[tuple[str, str]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    # None: send once per fingerprint (date/week-keyed findings). Otherwise re-send while the
    # condition persists, at most this often.
    repeat_after_hours: int | None = None


@dataclass(frozen=True)
class WorkspaceRef:
    id: int
    name: str
    host: str


def entity_link(host: str, entity: tuple[str, str]) -> str | None:
    kind, entity_id = entity
    path = ENTITY_PATHS.get(kind)
    return f"{host.rstrip('/')}{path.format(id=entity_id)}" if path and entity_id else None


def entity_label(entity: tuple[str, str]) -> str:
    kind, entity_id = entity
    return f"{kind} {entity_id}" if entity_id else f"untracked {kind} usage"


def usd(amount: float) -> str:
    return f"${amount:,.0f}" if abs(amount) >= 100 else f"${amount:,.2f}"
