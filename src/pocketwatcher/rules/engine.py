from datetime import date, timedelta

from pocketwatcher.rules import anomaly, budget, dev_premium, untagged
from pocketwatcher.rules import digest as digest_rule
from pocketwatcher.rules.base import CostRow, Finding, WorkspaceRef
from pocketwatcher.rules.config import RuleConfig

LOOKBACK_DAYS = 35  # covers month-to-date plus the 14-day same-weekday check


def billing_window(today: date) -> tuple[date, date]:
    """Cost rows [start, end) the billing rules need when evaluated on `today`."""
    return today - timedelta(days=LOOKBACK_DAYS), today


def run_billing_rules(
    rows: list[CostRow], cfg: RuleConfig, ws: WorkspaceRef, today: date, *, include_digest: bool = False
) -> list[Finding]:
    """Evaluate billing rules on the last completed day (UTC); today's partial usage is ignored."""
    day = today - timedelta(days=1)
    rows = [r for r in rows if r.usage_date <= day]
    findings = [
        *budget.evaluate(rows, cfg.budget, ws, day),
        *anomaly.evaluate(rows, cfg.anomaly, ws, day),
        *dev_premium.evaluate(rows, cfg.dev_premium, ws, day),
        *untagged.evaluate(rows, cfg.untagged, ws, day),
    ]
    if include_digest:
        findings.append(digest_rule.evaluate(rows, ws, day, cfg.digest_top_n))
    return findings
