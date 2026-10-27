import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest

from pocketwatcher.alerts import dispatcher, email, links, slack, teams
from pocketwatcher.alerts.message import build_message
from pocketwatcher.rules import digest
from pocketwatcher.rules.base import CostRow, Finding, WorkspaceRef

SNAPSHOTS = Path(__file__).parent.parent / "snapshots"
WS = WorkspaceRef(id=7, name="acme-prod", host="https://adb-1.azuredatabricks.net")
SPIKE = Finding(
    rule="anomaly.spike",
    fingerprint="anomaly:7:job:j1:2026-10-19",
    severity="critical",
    title="Spend spike: job 42 cost $960 on Oct 19",
    summary="16.0x its 7-day median of $60.00. <script> & co",
    impact_usd=900,
    entity=("job", "42"),
    owner="etl@acme.com",
    link="https://adb-1.azuredatabricks.net/jobs/42",
    details=[("Cost", "$960"), ("7-day median", "$60.00")],
)


def digest_finding() -> Finding:
    rows = [CostRow(date(2026, 10, 19), "job", f"j{i}", "SKU", 100.0 * i) for i in range(1, 13)]
    rows += [CostRow(date(2026, 10, 12), "job", "j12", "SKU", 600.0)]
    return digest.evaluate(rows, WS, date(2026, 10, 19))


def check_snapshot(name: str, payload) -> None:
    """Golden-file check; set PW_UPDATE_SNAPSHOTS=1 to rewrite after a deliberate change."""
    path = SNAPSHOTS / name
    text = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) if not isinstance(payload, str) else payload
    )
    if os.environ.get("PW_UPDATE_SNAPSHOTS") or not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def msg(f=SPIKE, update=False):
    return build_message(f, WS, update=update, mute_link=links.mute_url(WS.id, f.fingerprint))


def test_slack_payload_snapshot_and_block_kit_limits():
    payload = slack.render(msg())
    check_snapshot("slack_spike.json", {**payload, "blocks": [b for b in payload["blocks"] if b["type"] != "actions"]})
    header = payload["blocks"][0]["text"]["text"]
    assert header.startswith("🔴 ") and len(header) <= 150
    assert "&lt;script&gt; &amp; co" in payload["blocks"][1]["text"]["text"]
    buttons = payload["blocks"][-1]["elements"]
    assert [b["text"]["text"] for b in buttons] == ["Open in Databricks", "Mute for 7 days"]
    assert buttons[1]["url"].startswith("https://app.pocketwatcher.test/mute?t=")


def test_slack_clips_long_titles():
    long = Finding(rule="x", fingerprint="x", severity="info", title="a" * 400, summary="s")
    payload = slack.render(msg(long))
    assert len(payload["blocks"][0]["text"]["text"]) == 150


def test_digest_renders_a_ranked_linked_list_without_mute():
    m = msg(digest_finding())
    payload = slack.render(m)
    check_snapshot("slack_digest.json", payload)
    ranking = next(b for b in payload["blocks"] if b["type"] == "section" and b["text"]["text"].startswith("1."))
    lines = ranking["text"]["text"].splitlines()
    assert len(lines) == 10
    assert lines[0] == "1. <https://adb-1.azuredatabricks.net/jobs/j12|job j12>: $1,200 (+$600 vs last week)"
    assert all(link.label != "Mute for 7 days" for link in m.links)


def test_teams_adaptive_card_snapshot():
    payload = teams.render(msg(update=True))
    card = payload["attachments"][0]["content"]
    check_snapshot("teams_spike.json", {**card, "actions": [a["title"] for a in card["actions"]]})
    assert payload["type"] == "message"
    assert card["body"][0]["text"].startswith("Update: ") and card["body"][0]["color"] == "Attention"


def test_email_has_text_and_escaped_html_parts():
    e = email.render(msg(), "PocketWatcher <a@pw.dev>", ["x@acme.com", "y@acme.com"])
    assert e["To"] == "x@acme.com, y@acme.com"
    text = e.get_body(("plain",)).get_content()
    html = e.get_body(("html",)).get_content()
    assert "7-day median: $60.00" in text and "https://adb-1.azuredatabricks.net/jobs/42" in text
    assert "&lt;script&gt;" in html and "<script>" not in html


def test_mute_token_round_trips_and_rejects_tampering():
    token = links.mute_token(7, "fp:1")
    assert links.read_mute_token(token) == {"ws": 7, "fp": "fp:1", "days": 7}
    assert links.read_mute_token(token[:-4] + "AAAA") is None


# --- delivery retries -----------------------------------------------------


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(dispatcher.time, "sleep", lambda s: None)


def client(statuses: list[int]) -> tuple[httpx.Client, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(statuses[len(calls) - 1], text="ok")

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_post_retries_transient_errors_then_succeeds():
    http, calls = client([503, 429, 200])
    dispatcher.post_json(http, "https://hooks.example/x", {})
    assert len(calls) == 3


def test_post_does_not_retry_client_errors():
    http, calls = client([404, 200])
    with pytest.raises(RuntimeError, match="404"):
        dispatcher.post_json(http, "https://hooks.example/x", {})
    assert len(calls) == 1


def test_post_gives_up_after_attempts():
    http, calls = client([500, 500, 500, 500])
    with pytest.raises(RuntimeError, match="500"):
        dispatcher.post_json(http, "https://hooks.example/x", {})
    assert len(calls) == 3
