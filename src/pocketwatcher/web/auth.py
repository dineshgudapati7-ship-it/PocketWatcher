"""Sign-in through a hosted identity provider (WorkOS AuthKit). We never store passwords."""

import base64
import json
from dataclasses import dataclass
from typing import Protocol

from pocketwatcher.settings import get_settings


@dataclass(frozen=True)
class Identity:
    user_id: str
    email: str
    name: str | None
    # The provider's own sign-in session. Ending only our session would leave this one active,
    # and the next "Sign in" would return straight to the app without asking.
    session_id: str | None = None


class AuthProvider(Protocol):
    def authorization_url(self, state: str, redirect_uri: str) -> str: ...
    def authenticate(self, code: str) -> Identity: ...
    def logout_url(self, session_id: str, return_to: str) -> str: ...


class WorkOSAuth:
    def __init__(self, api_key: str | None = None, client_id: str | None = None):
        from workos import WorkOSClient

        settings = get_settings()
        self.client = WorkOSClient(
            api_key=api_key or settings.workos_api_key, client_id=client_id or settings.workos_client_id
        )

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        return self.client.user_management.get_authorization_url(
            provider="authkit", redirect_uri=redirect_uri, state=state
        )

    def authenticate(self, code: str) -> Identity:
        resp = self.client.user_management.authenticate_with_code(code=code)
        user = resp.user
        name = " ".join(p for p in (user.first_name, user.last_name) if p) or None
        return Identity(user_id=user.id, email=user.email, name=name, session_id=session_id_from(resp.access_token))

    def logout_url(self, session_id: str, return_to: str) -> str:
        return self.client.user_management.get_logout_url(session_id=session_id, return_to=return_to)


def session_id_from(access_token: str | None) -> str | None:
    """The `sid` claim of a WorkOS access token.

    The signature isn't checked: the token comes straight from WorkOS in our server-to-server
    code exchange over TLS, and the session id is only used to build the logout redirect.
    """
    try:
        payload = access_token.split(".")[1]  # type: ignore[union-attr]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None
    sid = claims.get("sid")
    return sid if isinstance(sid, str) else None
