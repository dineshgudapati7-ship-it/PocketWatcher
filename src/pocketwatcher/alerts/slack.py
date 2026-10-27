"""Slack incoming-webhook messages in Block Kit."""

from typing import Any

from pocketwatcher.alerts.message import SEVERITY_ICON, Message

# Block Kit limits: header text 150 chars, section text 3000, section fields 10, field text 2000.
HEADER_MAX, TEXT_MAX, FIELDS_MAX, FIELD_MAX = 150, 3000, 10, 2000


def render(msg: Message) -> dict[str, Any]:
    header = f"{SEVERITY_ICON.get(msg.severity, '')} {msg.title}".strip()
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _clip(header, HEADER_MAX), "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _clip(escape(msg.summary), TEXT_MAX)}},
    ]
    if msg.facts:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": _clip(f"*{escape(k)}*\n{escape(v)}", FIELD_MAX)}
                    for k, v in msg.facts[:FIELDS_MAX]
                ],
            }
        )
    if msg.ranking:
        lines = [
            f"{i}. {f'<{url}|{escape(label)}>' if url else escape(label)}: {escape(value)}"
            for i, (label, value, url) in enumerate(msg.ranking, 1)
        ]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip("\n".join(lines), TEXT_MAX)}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": escape(msg.context)}]})
    if msg.links:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": link.label}, "url": link.url}
                    for link in msg.links
                ],
            }
        )
    # `text` is the notification/fallback text shown where blocks can't render.
    return {"text": _clip(msg.title, TEXT_MAX), "blocks": blocks}


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
