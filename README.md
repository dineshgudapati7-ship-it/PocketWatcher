# PocketWatcher

Cost guardrails for Databricks workspaces. PocketWatcher connects as a **read-only service principal**,
reads `system.billing.*` through the customer's SQL warehouse, and sends opinionated alerts:
budget overruns, spend spikes, new high spenders, Photon/serverless on dev, untagged spend, and a
weekly top-10 digest.

## What the numbers mean

- **Estimated list cost.** Usage is priced with `system.billing.list_prices` and multiplied by the
  workspace's `discount_multiplier`. Cloud VM cost for classic compute is *not* included.
- **Billing data is hours behind.** Billing rules evaluate the last completed UTC day. Each sync
  re-reads the last 3 days plus any older day that received late or corrected records.
- **Customer cost.** Billing syncs run every 6 hours on the customer's warehouse (~$10/month on a
  2X-Small serverless warehouse with 1-minute auto-stop).

## Running

```sh
uv run alembic upgrade head
uv run python -m pocketwatcher.worker      # any number of workers; tasks are claimed with SKIP LOCKED
uv run uvicorn pocketwatcher.web.app:app
```

## Development

```sh
uv sync
uv run pytest           # SQLite + DuckDB-backed fake system tables; no workspace needed
uv run ruff check src tests && uv run mypy
```

Renderer golden files live in `tests/snapshots/`; after a deliberate format change run
`PW_UPDATE_SNAPSHOTS=1 uv run pytest` and review the diff.
Set `PW_TEST_DATABASE_URL` to run the suite against Postgres (CI does both).
Migrations: `uv run alembic upgrade head` (uses `PW_DATABASE_URL`).

## Layout

| Path | Purpose |
|---|---|
| `src/pocketwatcher/databricks/` | Statement Execution client and the production SQL |
| `src/pocketwatcher/collectors/billing.py` | Incremental sync into daily per-entity cost rows |
| `src/pocketwatcher/collectors/live.py` | Running clusters/warehouses from the REST APIs (free, current) |
| `src/pocketwatcher/rules/` | Pure rule functions: budget, anomaly, dev_premium, untagged, digest, live |
| `src/pocketwatcher/alerts/` | Dispatcher (dedupe, repeats, escalation, mutes) and Slack/Teams/email renderers |
| `src/pocketwatcher/onboarding/` | Customer setup grants, preflight checks, test alert |
| `src/pocketwatcher/worker.py` | Scheduler loop: billing every 6 h, live checks every 15 min |
| `src/pocketwatcher/web/` | FastAPI app (mute links so far) |
| `tests/fixtures/system_tables.py` | Schema-accurate fake `system.billing` tables in DuckDB |
