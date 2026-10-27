"""Channel-neutral view of a finding that each renderer formats for its own medium."""

from dataclasses import dataclass, field

from pocketwatcher.rules.base import Finding, WorkspaceRef, usd

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


@dataclass
class Link:
    label: str
    url: str


@dataclass
class Message:
    title: str
    summary: str
    severity: str
    facts: list[tuple[str, str]]
    context: str
    links: list[Link] = field(default_factory=list)
    # Digest only: ranked lines, each optionally linked.
    ranking: list[tuple[str, str, str | None]] = field(default_factory=list)


def build_message(finding: Finding, ws: WorkspaceRef, *, update: bool, mute_link: str | None) -> Message:
    title = f"Update: {finding.title}" if update else finding.title
    context = " · ".join(
        p
        for p in (
            ws.name,
            f"Owner: {finding.owner}" if finding.owner else None,
            "Costs are estimated at list price",
        )
        if p
    )
    links = []
    if finding.link:
        links.append(Link("Open in Databricks", finding.link))
    if mute_link and finding.rule != "digest":
        links.append(Link("Mute for 7 days", mute_link))

    ranking = []
    facts = finding.details
    if finding.rule == "digest":
        facts = []
        for o in finding.data.get("offenders", []):
            delta = o["cost"] - o["prev_cost"]
            trend = f"{'+' if delta >= 0 else '-'}{usd(abs(delta))} vs last week" if o["prev_cost"] else "new"
            ranking.append((o["label"], f"{usd(o['cost'])} ({trend})", o.get("link")))

    return Message(
        title=title,
        summary=finding.summary,
        severity=finding.severity,
        facts=facts,
        context=context,
        links=links,
        ranking=ranking,
    )
