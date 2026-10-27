"""Decides which findings reach which channels, sends them, and records every attempt.

A finding is sent to a channel when:
  - it has never been sent there, or
  - it repeats (`repeat_after_hours`) and that long has passed since the last send, or
  - its dollar impact has grown by ESCALATION_RATIO since the last send (sent as an "Update").
Muted fingerprints are never sent. Failed sends leave no "sent" record, so the next run retries.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pocketwatcher import crypto
from pocketwatcher.alerts import email, links, slack, teams
from pocketwatcher.alerts.message import Message, build_message
from pocketwatcher.db.models import AlertLog, Channel, Mute, Workspace
from pocketwatcher.rules.base import Finding, WorkspaceRef
from pocketwatcher.settings import Settings, get_settings

log = logging.getLogger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
ESCALATION_RATIO = 1.5
ESCALATION_MIN_USD = 25.0

Sender = Callable[[str, str, Message], None]  # (kind, decrypted target, message)


@dataclass
class DispatchResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0


def dispatch(
    session: Session,
    workspace: Workspace,
    findings: list[Finding],
    send: Sender,
    now: datetime | None = None,
) -> DispatchResult:
    now = now or datetime.now(UTC)
    result = DispatchResult()
    if not findings:
        return result
    ws = WorkspaceRef(workspace.id, workspace.name, workspace.host)
    fingerprints = {f.fingerprint for f in findings}

    channels = session.scalars(
        select(Channel).where(
            Channel.tenant_id == workspace.tenant_id,
            Channel.enabled.is_(True),
            or_(Channel.workspace_id.is_(None), Channel.workspace_id == workspace.id),
        )
    ).all()
    muted = set(
        session.scalars(
            select(Mute.fingerprint).where(
                Mute.workspace_id == workspace.id, Mute.fingerprint.in_(fingerprints), Mute.until > now
            )
        )
    )
    last_sent: dict[tuple[str, int], AlertLog] = {}
    for entry in session.scalars(
        select(AlertLog)
        .where(
            AlertLog.workspace_id == workspace.id,
            AlertLog.fingerprint.in_(fingerprints),
            AlertLog.status == "sent",
        )
        .order_by(AlertLog.created_at)
    ):
        last_sent[(entry.fingerprint, entry.channel_id)] = entry  # ordered, so the latest wins

    for finding in findings:
        if finding.fingerprint in muted:
            result.skipped += 1
            continue
        for channel in channels:
            # The weekly digest is informational but goes to every channel.
            below = SEVERITY_RANK[finding.severity] < SEVERITY_RANK.get(channel.min_severity, 1)
            if below and finding.rule != "digest":
                continue
            previous = last_sent.get((finding.fingerprint, channel.id))
            decision = _decide(finding, previous, now)
            if decision is None:
                result.skipped += 1
                continue
            msg = build_message(
                finding, ws, update=decision == "update", mute_link=links.mute_url(workspace.id, finding.fingerprint)
            )
            entry = AlertLog(
                workspace_id=workspace.id,
                channel_id=channel.id,
                fingerprint=finding.fingerprint,
                rule=finding.rule,
                severity=finding.severity,
                impact_usd=finding.impact_usd,
                title=msg.title[:500],
                created_at=now,
            )
            try:
                send(channel.kind, crypto.decrypt(channel.target_enc), msg)
                entry.status = "sent"
                result.sent += 1
            except Exception as exc:  # one broken channel must not block the others
                log.warning("alert to channel %s failed: %s", channel.id, exc)
                entry.status, entry.error = "failed", str(exc)[:1000]
                result.failed += 1
            session.add(entry)
            session.commit()
    return result


def _decide(finding: Finding, previous: AlertLog | None, now: datetime) -> str | None:
    if previous is None:
        return "new"
    # $0 means the cost was unknown when first sent (e.g. live check before billing caught up);
    # learning the cost is not an escalation. Repeating findings carry the new figure anyway.
    if (
        finding.rule != "digest"
        and previous.impact_usd > 0
        and finding.impact_usd >= max(previous.impact_usd * ESCALATION_RATIO, previous.impact_usd + ESCALATION_MIN_USD)
    ):
        return "update"
    if finding.repeat_after_hours is not None:
        sent_at = previous.created_at
        if sent_at.tzinfo is None:  # SQLite drops tzinfo; stored values are UTC
            sent_at = sent_at.replace(tzinfo=UTC)
        if now - sent_at >= timedelta(hours=finding.repeat_after_hours):
            return "repeat"
    return None


# --- delivery ---------------------------------------------------------------

RETRY_STATUSES = {429, 500, 502, 503, 504}


def post_json(http: httpx.Client, url: str, payload: dict, attempts: int = 3) -> None:
    """POST with retries on throttling and transient server errors; raises on final failure."""
    for attempt in range(1, attempts + 1):
        try:
            resp = http.post(url, json=payload, timeout=15)
        except httpx.TransportError:
            if attempt == attempts:
                raise
        else:
            if resp.is_success:
                return
            if resp.status_code not in RETRY_STATUSES or attempt == attempts:
                raise RuntimeError(f"webhook returned {resp.status_code}: {resp.text[:200]}")
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                time.sleep(min(int(retry_after), 30))
                continue
        time.sleep(2**attempt)


def make_sender(http: httpx.Client, settings: Settings | None = None) -> Sender:
    settings = settings or get_settings()

    def send(kind: str, target: str, msg: Message) -> None:
        match kind:
            case "slack":
                post_json(http, target, slack.render(msg))
            case "teams":
                post_json(http, target, teams.render(msg))
            case "email":
                email.send(target, msg, settings)
            case _:
                raise ValueError(f"unknown channel kind {kind!r}")

    return send
