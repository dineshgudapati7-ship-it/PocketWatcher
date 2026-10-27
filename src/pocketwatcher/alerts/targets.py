"""Validation of customer-supplied alert destinations.

The server POSTs to webhook URLs that tenants enter, so an arbitrary URL would let a tenant
make our server call internal addresses (SSRF). Only the official webhook hosts are allowed.
"""

import re
from urllib.parse import urlsplit

SLACK_HOSTS = ("hooks.slack.com",)
# Teams Workflows webhooks are served by Power Automate / Logic Apps; legacy connectors by Office.
TEAMS_HOST_SUFFIXES = (".logic.azure.com", ".powerplatform.com", ".webhook.office.com")
EMAIL = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def validate_target(kind: str, target: str) -> str:
    """Normalized target, or ValueError with a message fit to show the user."""
    target = target.strip()
    if kind == "email":
        addresses = [a.strip() for a in target.split(",") if a.strip()]
        if not addresses or not all(EMAIL.match(a) for a in addresses):
            raise ValueError("Enter one or more email addresses, separated by commas.")
        return ", ".join(addresses)

    parts = urlsplit(target)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("Webhook URLs must be plain https:// URLs.")
    if kind == "slack" and host in SLACK_HOSTS and parts.path.startswith("/services/"):
        return target
    if kind == "teams" and host.endswith(TEAMS_HOST_SUFFIXES):
        return target
    if kind == "slack":
        raise ValueError("Use a Slack incoming webhook URL (https://hooks.slack.com/services/...).")
    if kind == "teams":
        raise ValueError("Use the URL from a Teams Workflows 'webhook request received' trigger.")
    raise ValueError(f"Unknown channel type {kind!r}.")
