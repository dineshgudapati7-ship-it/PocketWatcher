from pocketwatcher import crypto
from pocketwatcher.alerts.dispatcher import Sender
from pocketwatcher.alerts.message import build_message
from pocketwatcher.db.models import Channel, Workspace
from pocketwatcher.rules.base import Finding, WorkspaceRef


def send_test_alert(channel: Channel, workspace: Workspace, send: Sender) -> None:
    """Send a sample alert so the customer sees the format before anything real fires. Raises on failure."""
    finding = Finding(
        rule="test",
        fingerprint=f"test:{workspace.id}",
        severity="info",
        title=f"PocketWatcher is connected to {workspace.name}",
        summary="Alerts for this workspace will look like this. No action needed.",
        details=[("Billing checks", "every 6 hours"), ("Live checks", "every 15 minutes")],
        link=workspace.host,
    )
    ws = WorkspaceRef(workspace.id, workspace.name, workspace.host)
    send(channel.kind, crypto.decrypt(channel.target_enc), build_message(finding, ws, update=False, mute_link=None))
