import calendar
from datetime import date, timedelta

from pocketwatcher.rules.base import CostRow, Finding, WorkspaceRef, usd
from pocketwatcher.rules.config import BudgetConfig


def month_outlook(by_day: dict[date, float], day: date) -> tuple[float, float, float]:
    """(month to date, 7-day average per day, month-end forecast) through the completed `day`."""
    month_start = day.replace(day=1)
    mtd = sum(c for d, c in by_day.items() if month_start <= d <= day)
    last7 = sum(by_day.get(day - timedelta(days=i), 0.0) for i in range(7)) / 7
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    return mtd, last7, mtd + last7 * (days_in_month - day.day)


def evaluate(rows: list[CostRow], cfg: BudgetConfig, ws: WorkspaceRef, day: date) -> list[Finding]:
    """Budget checks for the completed `day`: daily overrun, month-to-date and month-end forecast."""
    findings: list[Finding] = []
    by_day: dict[date, float] = {}
    for r in rows:
        by_day[r.usage_date] = by_day.get(r.usage_date, 0.0) + r.cost

    spent = by_day.get(day, 0.0)
    if cfg.daily_usd and spent > cfg.daily_usd:
        findings.append(
            Finding(
                rule="budget.daily",
                fingerprint=f"budget.daily:{ws.id}:{day}",
                severity="critical" if spent >= 1.5 * cfg.daily_usd else "warning",
                title=f"{ws.name}: {usd(spent)} spent on {day:%b %d}, over the {usd(cfg.daily_usd)} daily budget",
                summary=f"Estimated spend was {spent / cfg.daily_usd:.0%} of the daily budget.",
                impact_usd=spent - cfg.daily_usd,
                details=[("Spent", usd(spent)), ("Daily budget", usd(cfg.daily_usd))],
            )
        )

    if cfg.monthly_usd:
        mtd, last7, forecast = month_outlook(by_day, day)
        month = f"{day:%Y-%m}"
        if mtd > cfg.monthly_usd:
            findings.append(
                Finding(
                    rule="budget.monthly",
                    fingerprint=f"budget.monthly:{ws.id}:{month}:exceeded",
                    severity="critical",
                    title=f"{ws.name}: monthly budget exceeded ({usd(mtd)} of {usd(cfg.monthly_usd)})",
                    summary=f"Month-to-date spend through {day:%b %d} is over the monthly budget.",
                    impact_usd=mtd - cfg.monthly_usd,
                    details=[
                        ("Month to date", usd(mtd)),
                        ("Monthly budget", usd(cfg.monthly_usd)),
                        ("Forecast", usd(forecast)),
                    ],
                )
            )
        elif forecast > cfg.monthly_usd:
            findings.append(
                Finding(
                    rule="budget.monthly",
                    fingerprint=f"budget.monthly:{ws.id}:{month}:forecast",
                    severity="warning",
                    title=f"{ws.name}: on track to spend {usd(forecast)} this month (budget {usd(cfg.monthly_usd)})",
                    summary=(
                        f"At the 7-day average of {usd(last7)}/day, month-end spend will exceed "
                        f"the budget by {usd(forecast - cfg.monthly_usd)}."
                    ),
                    impact_usd=forecast - cfg.monthly_usd,
                    details=[
                        ("Month to date", usd(mtd)),
                        ("7-day avg", f"{usd(last7)}/day"),
                        ("Forecast", usd(forecast)),
                        ("Monthly budget", usd(cfg.monthly_usd)),
                    ],
                )
            )
    return findings
