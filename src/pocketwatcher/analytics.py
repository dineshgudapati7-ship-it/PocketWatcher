"""Cost analytics for an organisation: where the money goes, who owns it, and how it is trending.

Pure functions over CostRow, so every figure can be tested against hand-computed totals. All
amounts are estimated list-price DBU cost after each workspace's discount.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from pocketwatcher.rules.base import CostRow, entity_label

# Product groups in a fixed order: colour follows the group, never its rank.
PRODUCT_GROUPS: list[tuple[str, str]] = [
    ("jobs", "Jobs"),
    ("sql", "SQL warehouses"),
    ("all_purpose", "All-purpose compute"),
    ("pipelines", "Pipelines"),
    ("serving", "Model serving"),
    ("other", "Other"),
]
PRODUCT_MAP = {
    "JOBS": "jobs",
    "SQL": "sql",
    "ALL_PURPOSE": "all_purpose",
    "INTERACTIVE": "all_purpose",
    "DLT": "pipelines",
    "LAKEFLOW_CONNECT": "pipelines",
    "MODEL_SERVING": "serving",
    "VECTOR_SEARCH": "serving",
    "AI_GATEWAY": "serving",
}
COMPUTE_TYPES: list[tuple[str, str]] = [
    ("serverless", "Serverless"),
    ("photon", "Classic with Photon"),
    ("standard", "Classic"),
]
UNTAGGED = "Untagged"
OTHER_KEY = "__other__"

RANGES: dict[str, str] = {
    "mtd": "This month",
    "last_month": "Last month",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "6m": "Last 6 months",
    "12m": "Last 12 months",
}


def short_day(d: date, year: bool = False) -> str:
    """ "Oct 1", not "Oct 01". `%-d` is not portable (Windows), so the day is formatted by hand."""
    return f"{d:%b} {d.day}, {d.year}" if year else f"{d:%b} {d.day}"


def product_group(product: str | None) -> str:
    return PRODUCT_MAP.get((product or "").upper(), "other")


def compute_type(r: CostRow) -> str:
    if r.is_serverless:
        return "serverless"
    return "photon" if r.is_photon else "standard"


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive
    key: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def label(self) -> str:
        last = self.end - timedelta(days=1)
        dash = chr(0x2013)  # en dash for ranges
        if self.start.year == last.year:
            return f"{short_day(self.start)} {dash} {short_day(last, year=True)}"
        return f"{short_day(self.start, year=True)} {dash} {short_day(last, year=True)}"

    @property
    def short_label(self) -> str:
        """Without the year, for a line that sits under the full label."""
        return f"{short_day(self.start)} {chr(0x2013)} {short_day(self.end - timedelta(days=1))}"

    def previous(self) -> "Period":
        """The equal-length period just before; for whole months, the previous whole month."""
        if self.key in ("mtd", "last_month"):
            prev_end = self.start
            prev_start = (self.start - timedelta(days=1)).replace(day=1)
            if self.key == "mtd":  # compare like with like: the same number of days into last month
                prev_end = min(prev_start + timedelta(days=self.days), self.start)
            return Period(prev_start, prev_end, "prev")
        return Period(self.start - timedelta(days=self.days), self.start, "prev")


def period_for(key: str, today: date) -> Period:
    """Completed UTC days only: `today` is excluded because its usage is still arriving."""
    end = today
    if key == "mtd":
        start = today.replace(day=1)
        if start == today:  # the 1st: nothing completed yet this month, show last month
            return period_for("last_month", today)
        return Period(start, end, key)
    if key == "last_month":
        first = today.replace(day=1)
        return Period((first - timedelta(days=1)).replace(day=1), first, key)
    months = {"6m": 6, "12m": 12}.get(key)
    if months:
        start = today.replace(day=1)
        for _ in range(months - 1):
            start = (start - timedelta(days=1)).replace(day=1)
        return Period(start, end, key)
    days = {"30d": 30, "90d": 90}.get(key)
    if days is None:
        return period_for("30d", today)
    return Period(end - timedelta(days=days), end, key)


@dataclass
class Share:
    key: str
    label: str
    cost: float
    prev: float
    share: float  # of the period total, 0..1

    @property
    def change(self) -> float | None:
        return (self.cost - self.prev) / self.prev if self.prev else None


@dataclass
class Spender:
    entity: tuple[str, str]
    label: str
    cost: float
    prev: float
    owner: str | None
    workspace_id: int | None
    product: str

    @property
    def change(self) -> float | None:
        return (self.cost - self.prev) / self.prev if self.prev else None


@dataclass
class Bucket:
    start: date
    label: str
    by_group: dict[str, float]

    @property
    def total(self) -> float:
        return sum(self.by_group.values())


@dataclass
class Burn:
    """Cumulative spend through the current month against budget, with the month-end forecast."""

    month_start: date
    month_end: date
    through: date
    cumulative: list[tuple[date, float]]
    forecast: float
    budget: float | None
    budget_note: str | None


@dataclass
class Report:
    period: Period
    previous: Period
    total: float
    prev_total: float
    buckets: list[Bucket]
    bucket_unit: str  # "day" | "week" | "month"
    products: list[Share]
    compute: list[Share]
    tags: list[Share]
    tag_key: str | None
    tag_keys: list[str]
    untagged_share: float | None
    spenders: list[Spender]
    owners: list[Share]
    drivers: list[Spender]  # biggest increases vs the previous period
    burn: Burn | None
    workspace_count: int
    comparable: bool = True  # stored history covers the whole previous period
    notes: list[str] = field(default_factory=list)

    @property
    def change(self) -> float | None:
        if not self.comparable or not self.prev_total:
            return None
        return (self.total - self.prev_total) / self.prev_total

    @property
    def per_day(self) -> float:
        return self.total / self.period.days if self.period.days else 0.0


def build_report(
    rows: list[CostRow],
    period: Period,
    today: date,
    *,
    tag_key: str | None,
    budgets: dict[int, float | None],
    top_n: int = 10,
) -> Report:
    """`rows` must cover both `period` and `period.previous()`; `budgets` maps every workspace in
    scope to its monthly budget (None when it has none)."""
    prev = period.previous()
    cur = [r for r in rows if period.start <= r.usage_date < period.end]
    old = [r for r in rows if prev.start <= r.usage_date < prev.end]
    total, prev_total = _sum(cur), _sum(old)

    tag_keys = sorted({k for r in cur for k in r.tags})
    if tag_key not in tag_keys:
        tag_key = next(
            (k for k in ("team", "cost_center", "costcenter", "project") if k in tag_keys),
            tag_keys[0] if tag_keys else None,
        )

    products = _shares(cur, old, total, lambda r: product_group(r.product), PRODUCT_GROUPS)
    compute = _shares(cur, old, total, compute_type, COMPUTE_TYPES)
    tags: list[Share] = []
    untagged_share = None
    if tag_key:
        tagged = _shares(cur, old, total, lambda r: r.tags.get(tag_key) or UNTAGGED, None)
        tags = sorted((s for s in tagged if s.key != UNTAGGED), key=lambda s: -s.cost)
        if len(tags) > top_n:
            rest = tags[top_n - 1 :]
            other = Share(
                OTHER_KEY,
                f"Other ({len(rest)} values)",
                sum(s.cost for s in rest),
                sum(s.prev for s in rest),
                sum(s.share for s in rest),
            )
            tags = [*tags[: top_n - 1], other]
        untagged = next((s for s in tagged if s.key == UNTAGGED), None)
        if untagged:
            tags.append(untagged)  # always last, so it reads as the remainder it is
        untagged_share = untagged.share if untagged else 0.0

    spenders = _spenders(cur, old)
    drivers = sorted((s for s in spenders if s.cost - s.prev > 0), key=lambda s: -(s.cost - s.prev))[:5]
    earliest_row = min((r.usage_date for r in rows), default=None)
    if earliest_row is None or earliest_row > prev.start:
        drivers = []
    owners = sorted(_shares(cur, old, total, lambda r: r.run_as or "Unknown", None), key=lambda s: -s.cost)

    notes = []
    if not cur:
        notes.append("No usage in this period yet.")
    earliest = min((r.usage_date for r in rows), default=None)
    comparable = earliest is not None and earliest <= prev.start
    if cur and earliest is not None and not comparable:
        starts = short_day(earliest, year=True)
        notes.append(f"History starts {starts}, so there is no full previous period to compare with.")
    return Report(
        period=period,
        previous=prev,
        total=total,
        prev_total=prev_total,
        buckets=_buckets(cur, period),
        bucket_unit=_bucket_unit(period),
        products=[s for s in products if s.cost or s.prev],
        compute=[s for s in compute if s.cost or s.prev],
        tags=tags,
        tag_key=tag_key,
        tag_keys=tag_keys,
        untagged_share=untagged_share,
        spenders=sorted(spenders, key=lambda s: -s.cost)[:top_n],
        owners=owners[:top_n],
        drivers=drivers,
        burn=_burn(rows, today, budgets),
        workspace_count=len(budgets),
        comparable=comparable,
        notes=notes,
    )


def _sum(rows: list[CostRow]) -> float:
    return sum(r.cost for r in rows)


def _shares(cur, old, total, key_fn, order: list[tuple[str, str]] | None) -> list[Share]:
    now: dict[str, float] = defaultdict(float)
    before: dict[str, float] = defaultdict(float)
    for r in cur:
        now[key_fn(r)] += r.cost
    for r in old:
        before[key_fn(r)] += r.cost
    keys = [(k, label) for k, label in order] if order else [(k, k) for k in sorted(set(now) | set(before))]
    return [
        Share(k, label, now.get(k, 0.0), before.get(k, 0.0), now.get(k, 0.0) / total if total else 0.0)
        for k, label in keys
    ]


def _spenders(cur: list[CostRow], old: list[CostRow]) -> list[Spender]:
    cost: dict[tuple, float] = defaultdict(float)
    prev: dict[tuple, float] = defaultdict(float)
    owner_cost: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    meta: dict[tuple, tuple[int | None, str]] = {}
    for r in cur:
        key = (r.workspace_id, r.entity)
        cost[key] += r.cost
        if r.run_as:
            owner_cost[key][r.run_as] += r.cost
        meta.setdefault(key, (r.workspace_id, product_group(r.product)))
    for r in old:
        key = (r.workspace_id, r.entity)
        prev[key] += r.cost
        meta.setdefault(key, (r.workspace_id, product_group(r.product)))
    out = []
    for key in set(cost) | set(prev):
        ws_id, group = meta[key]
        owners = owner_cost.get(key)
        out.append(
            Spender(
                entity=key[1],
                label=entity_label(key[1]),
                cost=cost.get(key, 0.0),
                prev=prev.get(key, 0.0),
                owner=max(owners, key=lambda o: owners[o]) if owners else None,
                workspace_id=ws_id,
                product=group,
            )
        )
    return out


def _bucket_unit(period: Period) -> str:
    if period.days <= 62:
        return "day"
    if period.days <= 120:
        return "week"
    return "month"


def _bucket_start(d: date, unit: str) -> date:
    if unit == "day":
        return d
    if unit == "week":
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def _buckets(cur: list[CostRow], period: Period) -> list[Bucket]:
    unit = _bucket_unit(period)
    starts: list[date] = []
    d = _bucket_start(period.start, unit)
    while d < period.end:
        starts.append(d)
        if unit == "day":
            d += timedelta(days=1)
        elif unit == "week":
            d += timedelta(days=7)
        else:
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    by: dict[date, dict[str, float]] = {s: defaultdict(float) for s in starts}
    for r in cur:
        by[_bucket_start(r.usage_date, unit)][product_group(r.product)] += r.cost
    last_day = period.end - timedelta(days=1)

    def label(s: date) -> str:
        partial = (
            unit == "month"
            and s.month == last_day.month
            and s.year == last_day.year
            and last_day.day < calendar.monthrange(s.year, s.month)[1]
        )
        if unit == "month":
            return f"{s:%b %Y}" + (" (to date)" if partial else "")
        return f"Wk of {short_day(s)}" if unit == "week" else short_day(s)

    return [Bucket(s, label(s), dict(by[s])) for s in starts]


def _burn(rows: list[CostRow], today: date, budgets: dict[int, float | None]) -> Burn | None:
    through = today - timedelta(days=1)
    month_start = through.replace(day=1)
    month_end = through.replace(day=calendar.monthrange(through.year, through.month)[1])
    daily: dict[date, float] = defaultdict(float)
    for r in rows:
        if month_start <= r.usage_date <= through:
            daily[r.usage_date] += r.cost
    if not daily:
        return None
    running, cumulative = 0.0, []
    d = month_start
    while d <= through:
        running += daily.get(d, 0.0)
        cumulative.append((d, running))
        d += timedelta(days=1)
    pace = sum(daily.get(through - timedelta(days=i), 0.0) for i in range(7)) / 7
    forecast = running + pace * (month_end - through).days

    set_budgets = [b for b in budgets.values() if b]
    budget = sum(set_budgets) if set_budgets else None
    note = None
    if set_budgets and len(set_budgets) < len(budgets):
        note = f"Budget covers {len(set_budgets)} of {len(budgets)} workspaces."
    return Burn(month_start, month_end, through, cumulative, forecast, budget, note)


CSV_COLUMNS = [
    "usage_date",
    "workspace",
    "product",
    "sku",
    "entity_type",
    "entity_id",
    "owner",
    "photon",
    "serverless",
    "tags",
    "estimated_cost_usd",
]
