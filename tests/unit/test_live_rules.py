"""Live rules against REST-shaped responses, parsed with the SDK's own dataclasses."""

import json
from datetime import UTC, datetime
from pathlib import Path

from databricks.sdk.service import compute, sql

from pocketwatcher.collectors.live import LiveSnapshot, cluster_state, warehouse_state
from pocketwatcher.rules import live
from pocketwatcher.rules.base import WorkspaceRef
from pocketwatcher.rules.config import RuleConfig

API = Path(__file__).parent.parent / "fixtures" / "api"
WS = WorkspaceRef(id=1, name="acme-prod", host="https://adb-1.azuredatabricks.net")
NOW = datetime.fromtimestamp(1793000000, UTC)  # 2026-10-26 ~08:53 UTC


def snapshot() -> LiveSnapshot:
    clusters = [
        compute.ClusterDetails.from_dict(c) for c in json.loads((API / "clusters_list.json").read_text())["clusters"]
    ]
    whs = [sql.EndpointInfo.from_dict(w) for w in json.loads((API / "warehouses_list.json").read_text())["warehouses"]]
    return LiveSnapshot(
        taken_at=NOW,
        clusters=[c for c in map(cluster_state, clusters) if c],
        warehouses=[w for w in map(warehouse_state, whs) if w],
    )


def by_rule(findings):
    return {(f.rule, f.entity[1]) for f in findings}


def test_snapshot_keeps_only_running_all_purpose_compute():
    snap = snapshot()
    assert {c.cluster_id for c in snap.clusters} == {
        "0101-000000-forgotten",
        "0101-000000-photondev",
        "0101-000000-oldphoton",
        "0101-000000-longrun",
    }
    assert {w.warehouse_id for w in snap.warehouses} == {"wh-never-stops", "wh-dev-serverless", "wh-ok"}


def test_photon_detected_from_runtime_engine_or_legacy_version_string():
    photon = {c.cluster_id for c in snapshot().clusters if c.is_photon}
    assert photon == {"0101-000000-photondev", "0101-000000-oldphoton"}


def test_restart_time_wins_over_start_time():
    c = next(c for c in snapshot().clusters if c.cluster_id == "0101-000000-oldphoton")
    assert c.running_since == datetime.fromtimestamp(1792990000, UTC)


def test_live_findings():
    findings = live.evaluate(snapshot(), RuleConfig(), WS, {("cluster", "0101-000000-forgotten"): 240.0})
    assert by_rule(findings) == {
        ("cluster.autotermination", "0101-000000-forgotten"),  # never auto-terminates
        ("cluster.long_running", "0101-000000-longrun"),  # 27.8 h with 60 min auto-terminate
        ("dev_premium", "0101-000000-photondev"),
        ("dev_premium", "0101-000000-oldphoton"),  # env tag matched case-insensitively
        ("warehouse.auto_stop", "wh-never-stops"),
        ("dev_premium", "wh-dev-serverless"),
    }
    forgotten = next(f for f in findings if f.rule == "cluster.autotermination")
    assert forgotten.severity == "critical" and forgotten.impact_usd == 240.0
    assert forgotten.owner == "alice@acme.com"
    assert forgotten.link == "https://adb-1.azuredatabricks.net/compute/clusters/0101-000000-forgotten"
    assert all(f.repeat_after_hours == 24 for f in findings)


def test_forgotten_cluster_gets_one_alert_not_two():
    rules = [f.rule for f in live.evaluate(snapshot(), RuleConfig(), WS, {}) if f.entity[1] == "0101-000000-forgotten"]
    assert rules == ["cluster.autotermination"]


def test_live_and_billing_dev_premium_share_a_fingerprint():
    from datetime import date

    from pocketwatcher.rules import dev_premium
    from pocketwatcher.rules.base import CostRow

    (billing,) = dev_premium.evaluate(
        [
            CostRow(
                date(2026, 10, 25), "cluster", "0101-000000-photondev", "X", 100, tags={"env": "dev"}, is_photon=True
            )
        ],
        RuleConfig().dev_premium,
        WS,
        date(2026, 10, 25),
    )
    live_fp = {f.fingerprint for f in live.evaluate(snapshot(), RuleConfig(), WS, {})}
    assert billing.fingerprint in live_fp
