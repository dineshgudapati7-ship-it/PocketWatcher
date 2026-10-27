"""Microsoft Teams messages as Adaptive Cards, for a Teams Workflows ("Post to a channel when a
webhook request is received") webhook."""

from typing import Any

from pocketwatcher.alerts.message import Message

COLOR = {"critical": "Attention", "warning": "Warning", "info": "Accent"}


def render(msg: Message) -> dict[str, Any]:
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": msg.title,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "color": COLOR.get(msg.severity, "Default"),
        },
        {"type": "TextBlock", "text": msg.summary, "wrap": True},
    ]
    if msg.facts:
        body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in msg.facts]})
    if msg.ranking:
        lines = [
            f"{i}. {f'[{label}]({url})' if url else label}: {value}"
            for i, (label, value, url) in enumerate(msg.ranking, 1)
        ]
        body.append({"type": "TextBlock", "text": "\n\n".join(lines), "wrap": True})
    body.append({"type": "TextBlock", "text": msg.context, "isSubtle": True, "size": "Small", "wrap": True})

    card = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "msteams": {"width": "Full"},
        "body": body,
        "actions": [{"type": "Action.OpenUrl", "title": link.label, "url": link.url} for link in msg.links],
    }
    return {
        "type": "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}
        ],
    }
