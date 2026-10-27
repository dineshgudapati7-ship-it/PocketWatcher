"""Signed links embedded in alerts, so a recipient can act without logging in."""

import json
from datetime import timedelta
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken

from pocketwatcher.settings import get_settings

MUTE_DAYS = 7
MUTE_LINK_TTL = timedelta(days=30)


def mute_token(workspace_id: int, fingerprint: str, key: str | None = None) -> str:
    payload = json.dumps({"ws": workspace_id, "fp": fingerprint, "days": MUTE_DAYS})
    return Fernet((key or get_settings().fernet_key).encode()).encrypt(payload.encode()).decode()


def read_mute_token(token: str, key: str | None = None) -> dict | None:
    """Payload of a valid, unexpired token; None if it was tampered with or is too old."""
    try:
        raw = Fernet((key or get_settings().fernet_key).encode()).decrypt(
            token.encode(), ttl=int(MUTE_LINK_TTL.total_seconds())
        )
    except InvalidToken:
        return None
    return json.loads(raw)


def mute_url(workspace_id: int, fingerprint: str) -> str | None:
    settings = get_settings()
    if not settings.fernet_key:
        return None
    query = urlencode({"t": mute_token(workspace_id, fingerprint)})
    return f"{settings.app_base_url.rstrip('/')}/mute?{query}"
