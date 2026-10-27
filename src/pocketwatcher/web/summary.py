"""What a workspace's pass shows: the month so far, where it is heading, and against what budget."""

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from pocketwatcher.collectors.billing import load_costs
from pocketwatcher.db.models import Workspace
from pocketwatcher.rules.budget import month_outlook
from pocketwatcher.rules.config import RuleConfig


@dataclass
class Outlook:
    through: date  # last completed day included
    month_end: date
    month_to_date: float
    forecast: float
    budget: float | None
    has_data: bool

    @property
    def status(self) -> str:
        if not self.has_data:
            return "awaiting"
        if self.budget is None:
            return "no_budget"
        if self.month_to_date > self.budget:
            return "over"
        if self.forecast > self.budget:
            return "at_risk"
        return "on_track"


STATUS_LABELS = {
    "awaiting": "Awaiting sync",
    "no_budget": "No budget",
    "over": "Over budget",
    "at_risk": "At risk",
    "on_track": "On track",
}


def workspace_outlook(db: Session, ws: Workspace, now: datetime) -> Outlook:
    # Same basis as the budget rule: completed UTC days only, today's partial usage excluded.
    day = now.date() - timedelta(days=1)
    month_start = day.replace(day=1)
    by_day: dict[date, float] = {}
    for r in load_costs(db, ws, min(month_start, day - timedelta(days=6)), day + timedelta(days=1)):
        by_day[r.usage_date] = by_day.get(r.usage_date, 0.0) + r.cost
    mtd, _, forecast = month_outlook(by_day, day)
    budget = RuleConfig.model_validate(ws.rule_config or {}).budget.monthly_usd
    month_end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    return Outlook(day, month_end, mtd, forecast, budget, has_data=ws.billing_synced_at is not None)
