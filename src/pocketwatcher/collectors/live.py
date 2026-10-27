"""Point-in-time state of clusters and SQL warehouses, read from the REST APIs.

Billing tables lag by hours; these APIs are current and free to call, so they back the
rules that need to catch a problem while it is still burning money.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from databricks.sdk import WorkspaceClient
from databricks.sdk.service import compute, sql

ALL_PURPOSE_SOURCES = {compute.ClusterSource.UI, compute.ClusterSource.API}
ACTIVE_CLUSTER_STATES = [compute.State.RUNNING, compute.State.RESIZING]


@dataclass(frozen=True)
class ClusterState:
    cluster_id: str
    name: str
    running_since: datetime | None
    autotermination_minutes: int  # 0 = never
    is_photon: bool
    creator: str | None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WarehouseState:
    warehouse_id: str
    name: str
    size: str | None
    auto_stop_mins: int  # 0 = never
    is_serverless: bool
    creator: str | None
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveSnapshot:
    taken_at: datetime
    clusters: list[ClusterState]  # running all-purpose clusters only
    warehouses: list[WarehouseState]  # running warehouses only


def take_snapshot(client: WorkspaceClient, now: datetime | None = None) -> LiveSnapshot:
    clusters = client.clusters.list(filter_by=compute.ListClustersFilterBy(cluster_states=ACTIVE_CLUSTER_STATES))
    return LiveSnapshot(
        taken_at=now or datetime.now(UTC),
        clusters=[c for c in (cluster_state(d) for d in clusters) if c],
        warehouses=[w for w in (warehouse_state(e) for e in client.warehouses.list()) if w],
    )


def cluster_state(d: compute.ClusterDetails) -> ClusterState | None:
    if d.state not in ACTIVE_CLUSTER_STATES or d.cluster_source not in ALL_PURPOSE_SOURCES:
        return None
    started = d.last_restarted_time or d.start_time
    return ClusterState(
        cluster_id=d.cluster_id or "",
        name=d.cluster_name or d.cluster_id or "",
        running_since=datetime.fromtimestamp(started / 1000, UTC) if started else None,
        autotermination_minutes=d.autotermination_minutes or 0,
        # Older runtimes signal Photon only through the version string.
        is_photon=d.runtime_engine == compute.RuntimeEngine.PHOTON or "photon" in (d.spark_version or ""),
        creator=d.creator_user_name or d.single_user_name,
        tags=dict(d.custom_tags or {}),
    )


def warehouse_state(e: sql.EndpointInfo) -> WarehouseState | None:
    if e.state != sql.State.RUNNING:
        return None
    pairs = (e.tags.custom_tags if e.tags else None) or []
    return WarehouseState(
        warehouse_id=e.id or "",
        name=e.name or e.id or "",
        size=e.cluster_size,
        auto_stop_mins=e.auto_stop_mins or 0,
        is_serverless=bool(e.enable_serverless_compute),
        creator=e.creator_name,
        tags={p.key: p.value for p in pairs if p.key and p.value is not None},
    )
