from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from pocketwatcher import crypto
from pocketwatcher.alerts.dispatcher import dispatch
from pocketwatcher.db.models import AlertLog, Channel, Mute, Workspace
from pocketwatcher.rules.base import Finding

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


class Outbox:
    def __init__(self, fail_kinds: set[str] | None = None):
        self.sent: list[tuple[str, str, str]] = []
        self.fail_kinds = fail_kinds or set()

    def __call__(self, kind, target, msg):
        if kind in self.fail_kinds:
            raise RuntimeError(f"{kind} is down")
        self.sent.append((kind, target, msg.title))


def finding(fp="anomaly:1:job:j1:2026-10-19", impact=300.0, severity="warning", **kw) -> Finding:
    return Finding(
        rule=kw.pop("rule", "anomaly.spike"),
        fingerprint=fp,
        severity=severity,
        title="Spend spike: job j1",
        summary="3x baseline",
        impact_usd=impact,
        **kw,
    )


@pytest.fixture
def channels(session, workspace: Workspace):
    slack = Channel(
        tenant_id=workspace.tenant_id,
        name="#data-costs",
        kind="slack",
        target_enc=crypto.encrypt("https://hooks.slack.com/services/T/B/x"),
        min_severity="warning",
    )
    email = Channel(
        tenant_id=workspace.tenant_id,
        workspace_id=workspace.id,
        name="finance",
        kind="email",
        target_enc=crypto.encrypt("finance@acme.com"),
        min_severity="critical",
    )
    session.add_all([slack, email])
    session.commit()
    return slack, email


def test_new_finding_goes_to_channels_by_severity(session, workspace, channels):
    out = Outbox()
    dispatch(session, workspace, [finding(severity="warning")], out, NOW)
    assert [k for k, _, _ in out.sent] == ["slack"]  # email only takes critical

    out = Outbox()
    dispatch(session, workspace, [finding(fp="other", severity="critical")], out, NOW)
    assert sorted(k for k, _, _ in out.sent) == ["email", "slack"]
    assert ("email", "finance@acme.com", "Spend spike: job j1") in out.sent  # target was decrypted


def test_same_finding_is_not_resent(session, workspace, channels):
    dispatch(session, workspace, [finding()], Outbox(), NOW)
    out = Outbox()
    result = dispatch(session, workspace, [finding()], out, NOW + timedelta(hours=6))
    assert out.sent == [] and result.skipped == 1


def test_growing_impact_is_sent_as_update(session, workspace, channels):
    dispatch(session, workspace, [finding(impact=300)], Outbox(), NOW)
    out = Outbox()
    dispatch(session, workspace, [finding(impact=400)], out, NOW + timedelta(hours=6))  # +33%: quiet
    assert out.sent == []
    dispatch(session, workspace, [finding(impact=460)], out, NOW + timedelta(hours=12))
    assert [t for _, _, t in out.sent] == ["Update: Spend spike: job j1"]


def test_repeating_findings_resend_after_their_interval(session, workspace, channels):
    f = finding(fp="cluster.autotermination:1:c1", repeat_after_hours=24)
    dispatch(session, workspace, [f], Outbox(), NOW)
    out = Outbox()
    dispatch(session, workspace, [f], out, NOW + timedelta(hours=23))
    assert out.sent == []
    dispatch(session, workspace, [f], out, NOW + timedelta(hours=24))
    assert len(out.sent) == 1


def test_muted_fingerprint_is_not_sent_until_mute_expires(session, workspace, channels):
    session.add(Mute(workspace_id=workspace.id, fingerprint="m", until=NOW + timedelta(days=7)))
    session.commit()
    out = Outbox()
    dispatch(session, workspace, [finding(fp="m")], out, NOW)
    assert out.sent == []
    dispatch(session, workspace, [finding(fp="m")], out, NOW + timedelta(days=8))
    assert len(out.sent) == 1


def test_failed_channel_is_retried_next_run_without_resending_to_others(session, workspace, channels):
    crit = finding(severity="critical")
    first = dispatch(session, workspace, [crit], Outbox(fail_kinds={"email"}), NOW)
    assert (first.sent, first.failed) == (1, 1)
    failed = session.scalars(select(AlertLog).where(AlertLog.status == "failed")).one()
    assert failed.error == "email is down"

    out = Outbox()
    dispatch(session, workspace, [crit], out, NOW + timedelta(minutes=15))
    assert [k for k, _, _ in out.sent] == ["email"]


def test_channels_of_other_tenants_and_workspaces_are_ignored(session, workspace, channels):
    from pocketwatcher.db.models import Tenant

    other = Tenant(name="Other")
    session.add(other)
    session.flush()
    session.add(Channel(tenant_id=other.id, name="x", kind="slack", target_enc=crypto.encrypt("u")))
    session.add(
        Channel(
            tenant_id=workspace.tenant_id,
            workspace_id=workspace.id + 100,
            name="y",
            kind="slack",
            target_enc=crypto.encrypt("u"),
        )
    )
    session.commit()
    out = Outbox()
    dispatch(session, workspace, [finding()], out, NOW)
    assert len(out.sent) == 1


def test_digest_is_sent_once_per_week_even_if_totals_change(session, workspace, channels):
    d = finding(fp="digest:1:2026-W43", rule="digest", severity="warning", impact=1000)
    dispatch(session, workspace, [d], Outbox(), NOW)
    out = Outbox()
    dispatch(
        session,
        workspace,
        [finding(fp="digest:1:2026-W43", rule="digest", severity="warning", impact=5000)],
        out,
        NOW + timedelta(hours=6),
    )
    assert out.sent == []


def test_info_digest_reaches_channels_whose_minimum_is_higher(session, workspace, channels):
    out = Outbox()
    dispatch(session, workspace, [finding(fp="digest:1:2026-W43", rule="digest", severity="info")], out, NOW)
    assert sorted(k for k, _, _ in out.sent) == ["email", "slack"]


def test_learning_a_previously_unknown_cost_is_not_an_escalation(session, workspace, channels):
    dispatch(session, workspace, [finding(impact=0)], Outbox(), NOW)
    out = Outbox()
    dispatch(session, workspace, [finding(impact=1100)], out, NOW + timedelta(hours=6))
    assert out.sent == []
