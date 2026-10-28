# PocketWatcher runbook

How to understand, run, check and fix this project yourself.

Last verified: 2026-10-28, from a fresh clone of `origin/development` (commit `a5c4f4e`), on Windows 11 with
PowerShell and uv 0.11. Commands below are PowerShell; run them from the project folder (the one that
contains `pyproject.toml`). Anything in section 10 that says "not verified" has never run against the real
thing.

## Contents

1. [What PocketWatcher is](#1-what-pocketwatcher-is)
2. [Quick start](#2-quick-start-about-10-minutes)
3. [Settings reference](#3-settings-reference)
4. [Running it day to day](#4-running-it-day-to-day)
5. [See the product with sample data](#5-see-the-product-with-sample-data)
6. [Connect a real Databricks workspace](#6-connect-a-real-databricks-workspace)
7. [How it works](#7-how-it-works)
8. [Everyday commands](#8-everyday-commands)
9. [Troubleshooting](#9-troubleshooting)
10. [What is verified and what is not](#10-what-is-verified-and-what-is-not)
11. [Before putting it on a server](#11-before-putting-it-on-a-server)

---

## 1. What PocketWatcher is

PocketWatcher watches a company's Databricks spend and warns the team the day a problem starts. A customer
connects a workspace with a **read-only service principal**. PocketWatcher reads that workspace's billing
tables, estimates cost at Databricks list price, and posts alerts to Slack, Teams or email: budget at risk,
spend spike, cluster left running, Photon on a dev cluster, untagged spend. A web page shows the numbers and
a cost analytics view for finance.

There are two programs and one database:

```
 Databricks (the customer's)             PocketWatcher
 +--------------------------+      +-----------------------------------------+
 | system.billing.* tables  |<-----| WORKER   python -m pocketwatcher.worker |---> Slack
 | clusters, SQL warehouses |  read| - billing sync + rules every 6 hours    |---> Teams
 +--------------------------+  only| - running-compute checks every 15 min   |---> email
                                    +------------------+----------------------+
                                                       | reads and writes
                                                +------v------+
                                                |  DATABASE   |  SQLite on your machine,
                                                +------^------+  Postgres on a server
                                                       |
 You, in a browser --sign-in via WorkOS--> WEB APP   uvicorn pocketwatcher.web.app:app
```

| Part | What it does | Talks to Databricks? |
|---|---|---|
| **Web app** | Sign-in, connect wizard, overview, analytics, channels, settings, mute links | Only during the connect check |
| **Worker** | Pulls billing data, runs the alert rules, sends alerts | Yes, all the reading |
| **Database** | Accounts, workspaces (secrets encrypted), daily costs, channels, alert history | No |

Words used in this document:

| Term | Meaning |
|---|---|
| Service principal | A non-human Databricks login. PocketWatcher uses one per workspace, with read-only rights. |
| System tables | Tables Databricks keeps itself, e.g. `system.billing.usage` (what was used) and `system.billing.list_prices`. |
| DBU | The unit Databricks bills compute in. Cost estimate = DBUs x list price x (1 - your discount). |
| Account space ("tenant") | Each person who signs up gets their own. Teammate invites do not exist yet. |
| Fingerprint | A string that identifies "the same alert", used to avoid sending it twice. |

Costs are always **estimates at list price**. They leave out cloud VM charges for classic compute and any
negotiated rates, so they are never an invoice total.

---

## 2. Quick start (about 10 minutes)

You need: Git, [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you), a browser, and a free
[WorkOS](https://workos.com) account for sign-in. You do **not** need Databricks, Postgres, Slack or email
to get the app running.

**1. Get the code and install it.** If you already have the folder, skip the clone.

```powershell
git clone https://github.com/dineshgudapati7-ship-it/PocketWatcher.git
cd PocketWatcher
git checkout development
uv sync
```

**2. Run the tests.** This proves your install works, with no accounts or network needed.

```powershell
uv run pytest -q
```

Expected: `131 passed` (the number grows as tests are added; there must be no failures).

**3. Create your settings file.**

```powershell
Copy-Item .env.example .env
```

Open `.env` in VS Code and set these four things (all variables are explained in section 3):

1. `PW_DATABASE_URL=sqlite:///pocketwatcher.db`. The template already has this; leave it. Edit the value only,
   never the name: a line like `PW_DATABASE_URL=PW_DATABASE_URL=...` fails with `Could not parse SQLAlchemy URL`.
2. `PW_FERNET_KEY=` a new key. Generate one and paste it after the `=`:
   ```powershell
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
3. `PW_SESSION_SECRET=` any long random string:
   ```powershell
   uv run python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```
4. `PW_WORKOS_API_KEY=` and `PW_WORKOS_CLIENT_ID=` from the WorkOS dashboard (next step).

Keep `PW_SECURE_COOKIES=false` and `PW_APP_BASE_URL=http://localhost:8000` for local use.

`.env` holds secrets and is git-ignored. Never commit it or paste its contents into chats or tickets.

**4. Set up WorkOS (sign-in).** In the WorkOS dashboard, use the **Staging** environment:

- **API Keys** page: copy the API key (starts `sk_test_`) and the Client ID (starts `client_`) into `.env`.
  Both must come from the same environment.
- **Redirects**: add `http://localhost:8000/callback` as a redirect URI. It must match exactly: `http`,
  `localhost` (not `127.0.0.1`), port 8000, path `/callback`.
- Also add `http://localhost:8000/` as the **sign-out redirect**.
- **Authentication**: turn on the sign-in methods you want (Google, Microsoft, email code, password).

**5. Create the database tables.**

```powershell
uv run alembic upgrade head
```

Expected last line: `Running upgrade  -> 52e1debb3263, initial schema`. Windows PowerShell may print this
in red as a `NativeCommandError`. That is only alembic's normal progress message, not a failure.

**6. Start the web app** in one terminal, leave it running:

```powershell
uv run uvicorn pocketwatcher.web.app:app --port 8000
```

**7. Start the worker** in a second terminal (it does nothing until a workspace is connected, so you can
skip it for now):

```powershell
uv run python -m pocketwatcher.worker
```

**8. Open http://localhost:8000.** You should see the landing page. Click **Sign in**, sign in at WorkOS, and
you land on **Overview** with "Connect your first workspace". Your first sign-in creates your account space.

Quick checks: `http://localhost:8000/healthz` returns `{"status":"ok"}`. If the page loads unstyled,
`http://localhost:8000/static/app.css` should return the stylesheet.

---

## 3. Settings reference

Settings come from `.env` (or real environment variables, which win over `.env`). Both are read **once at
start-up**, so restart after changing them. In PowerShell, `$env:PW_SECURE_COOKIES = "false"` sets one for
the current terminal only.

| Variable | Needed | What it does and what to watch for |
|---|---|---|
| `PW_DATABASE_URL` | Yes | Where data lives. Local: `sqlite:///pocketwatcher.db` (a file in the folder you run from, so always run both programs from the project folder). Server: `postgresql+psycopg://user:password@host:5432/dbname`. |
| `PW_FERNET_KEY` | Yes | Encrypts workspace secrets, webhook URLs and mute-link tokens. **If you lose or change it, stored secrets cannot be read** and you must reconnect workspaces and re-add channels. Back it up. |
| `PW_SESSION_SECRET` | Yes | Signs the login cookie. If empty, a random one is made at every start, so everyone is signed out on each restart. Changing it signs everyone out. |
| `PW_APP_BASE_URL` | Yes | The address people use to reach the app. Used for the sign-in return address and for mute links in alerts. Must match the WorkOS redirect. |
| `PW_SECURE_COOKIES` | Yes | `false` for `http://localhost`, `true` (the default) behind HTTPS. |
| `PW_WORKOS_API_KEY`, `PW_WORKOS_CLIENT_ID` | Yes | Sign-in. Without them `/login` returns an error. |
| `PW_SMTP_HOST`, `PW_SMTP_PORT`, `PW_SMTP_USER`, `PW_SMTP_PASSWORD`, `PW_SMTP_FROM`, `PW_SMTP_STARTTLS` | Only for email alerts | Standard SMTP login. Port 587 with STARTTLS by default. With no host, email alerts show as Failed. |
| `PW_TEST_DATABASE_URL` | Tests only | Set to a Postgres URL to run the test suite on Postgres instead of SQLite. |
| `PW_UPDATE_SNAPSHOTS` | Tests only | Set to `1` to rewrite the alert-format golden files in `tests/snapshots/`. |

---

## 4. Running it day to day

| I want to | Do this |
|---|---|
| Start | Two terminals in the project folder: the web command and the worker command from section 2, steps 6 and 7. |
| Stop | `Ctrl+C` in each terminal. |
| Check the web app is up | Open `http://localhost:8000/healthz`. |
| Check the worker is alive | It prints **nothing while idle**. That is normal. Failures print as `ERROR ... failed for workspace N` followed by the error. A working sync shows on the Overview card as "Synced <time> UTC". |
| Sync a workspace now instead of waiting | On the workspace page, click **Sync now**. It queues a billing sync and a live check; the worker (which must be running) picks them up within about 10 seconds. Refresh to see "Synced <time> UTC" change. It is refused for 5 minutes after a billing sync finishes, while one is queued, and while one is running, because each sync runs queries on the customer's SQL warehouse. It cannot bring in billing records sooner: Databricks writes them hours after usage. Only the live check (running clusters and warehouses) is up to the minute. |
| Use another port | Add `--port 8001` and update `PW_APP_BASE_URL` and the WorkOS redirect URIs to match. |
| Reload on code changes | Add `--reload` to the uvicorn command while you are editing code. |

Run **one** worker while using SQLite. Several workers are safe only on Postgres.

If the project folder is inside OneDrive: it will try to sync the `.venv` folder (thousands of files) and
the SQLite file. That can be slow and can occasionally lock the database. If you see `database is locked`,
pause OneDrive sync or keep a working copy outside OneDrive.

---

## 5. See the product with sample data

Without a Databricks workspace the Overview and Analytics pages show their empty states. To see them full,
add a sample workspace with 120 days of invented cost data. It is marked inactive, so the worker ignores it.

1. Sign in once (section 2, step 8) so your account exists.
2. In the project folder run:

```powershell
@'
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pocketwatcher import crypto
from pocketwatcher.db.models import DailyCost, User, Workspace
from pocketwatcher.db.session import make_engine

random.seed(1)
# (type, id, product, team tag, owner, photon, serverless, base $/day)
RESOURCES = [
    ("job", "nightly-etl", "JOBS", "data-platform", "etl@acme.com", False, False, 210),
    ("job", "customer-360", "JOBS", "data-platform", "etl@acme.com", True, False, 120),
    ("warehouse", "bi-warehouse", "SQL", "analytics", "dana@acme.com", True, True, 140),
    ("cluster", "alice-scratch", "ALL_PURPOSE", None, "alice@acme.com", True, False, 60),
    ("pipeline", "orders-dlt", "DLT", "data-platform", "etl@acme.com", False, False, 55),
    ("endpoint", "churn-model", "MODEL_SERVING", "ml", "ml-svc@acme.com", False, True, 35),
]

with Session(make_engine()) as db:
    user = db.scalars(select(User).order_by(User.id)).first()
    if user is None:
        raise SystemExit("Sign in once at http://localhost:8000 first, so there is an account to attach the data to.")
    now = datetime.now(UTC)
    ws = Workspace(
        tenant_id=user.tenant_id,
        name="demo-workspace",
        host="https://adb-1234567890123456.7.azuredatabricks.net",
        databricks_workspace_id=1234567890123456,
        cloud="AZURE",
        client_id="demo",
        client_secret_enc=crypto.encrypt("demo"),
        warehouse_id="demo",
        rule_config={"budget": {"monthly_usd": 21000}},
        billing_synced_at=now,
        active=False,  # inactive, so the worker never tries to sync it
    )
    db.add(ws)
    db.flush()
    for days_ago in range(1, 121):
        day = (now - timedelta(days=days_ago)).date()
        weekend = 0.75 if day.weekday() >= 5 else 1.0
        growth = 1 + (120 - days_ago) / 120 * 0.3
        for kind, rid, product, team, owner, photon, serverless, base in RESOURCES:
            db.add(
                DailyCost(
                    workspace_id=ws.id,
                    usage_date=day,
                    sku_name="SKU",
                    product=product,
                    entity_type=kind,
                    entity_id=rid,
                    run_as=owner,
                    tags={"team": team} if team else {},
                    is_photon=photon,
                    is_serverless=serverless,
                    quantity=1,
                    list_cost=base * growth * weekend * random.uniform(0.85, 1.15),
                )
            )
    db.commit()
    print(f"Added demo-workspace with 120 days of sample data for {user.email}.")
'@ | uv run python -
```

Expected output: `Added demo-workspace with 120 days of sample data for <your email>.`

3. Reload the pages. What you should see (exact dollars shift with today's date):

| Page | What to look for |
|---|---|
| **Overview** | A `demo-workspace` card. Month to date around $20K, forecast above the $21,000 budget, so the badge reads **At risk** (or **Over budget** late in the month). The sidebar shows the workspace with a coloured dot. |
| **Analytics**, "This month" | Total, daily average, forecast against budget, untagged share around 10%. Spend by team: data-platform, analytics, ml, then Untagged. |
| **Analytics**, "Last 6 months" | "Not enough history yet" under "What changed", and no change percentages. This is correct: the sample data covers only 120 days, so there is no full previous period to compare with. |
| **Analytics**, Export CSV | One row per day and resource. |
| **Recent alerts** | Empty. Alerts are created by the worker, which ignores this inactive sample workspace. |

To remove the sample data, stop both programs, delete the database file, and recreate it. This also deletes
your account row; signing in again recreates it.

```powershell
Remove-Item pocketwatcher.db
uv run alembic upgrade head
```

---

## 6. Connect a real Databricks workspace

**You need:** a Premium (or Enterprise) Databricks workspace on Azure, AWS or GCP with Unity Catalog, the
`system.billing` tables readable, a **workspace admin** (to create the service principal) and a **metastore
admin** (to run the grants). A free or trial workspace may not expose the billing system tables.

In the app: **Overview > Connect workspace** (or the `+` next to Workspaces). The wizard gives you the exact
commands. In short:

1. Create a service principal in the workspace, give it *Workspace access*, and create an **OAuth secret**.
   Copy its Application ID (a UUID) and the secret.
2. Paste the Application ID into the wizard. It generates the `GRANT` statements (read-only: `USE` and
   `SELECT` on `system.billing`, `system.compute`, `system.lakeflow`). A metastore admin runs them in the
   Databricks SQL editor.
3. Create a small warehouse for PocketWatcher: 2X-Small, serverless, auto-stop 1 minute, and give the
   service principal **Can use** on it. Expected cost to the customer: roughly $10 a month.
4. Fill in the form: a name, the workspace URL, the Application ID, the OAuth secret, the warehouse ID.
   Press **Check and connect**.

The check tests, in order: sign-in as the service principal, warehouse access, reading `system.billing.usage`
and `list_prices`, and listing clusters and warehouses. If any step fails, **nothing is saved** and the page
shows which step failed and how to fix it. Warnings (for example "warehouse auto-stops after 30 min") do not
block connecting.

Only real Databricks workspace addresses are accepted (`*.azuredatabricks.net`, `*.cloud.databricks.com`,
`*.gcp.databricks.com`).

**After connecting** (worker must be running):

- Within about 10 seconds the worker picks the workspace up and runs its first billing sync, which loads
  up to **365 days** of history. On a large account that can take a while. The Overview card shows "First
  sync within minutes" until it finishes.
- Rules run on the last **completed** UTC day. Today's partial usage is ignored, and Databricks billing data
  is itself hours behind.
- Add at least one channel (**Channels**, section 7) or nothing will be sent. The Overview warns you.
- Set a monthly budget on the workspace page. Without one, budget alerts are off.

**Check it is right** (live validation; none of this has been done yet):

1. The connect check passes with only the documented grants, and fails clearly when you revoke one.
2. Start an all-purpose cluster with auto-termination off. The "never auto-terminates" alert arrives within
   about 15 minutes.
3. Start a Photon cluster tagged `env=dev`. The "Photon on dev" alert arrives.
4. Pick one complete past day and compare PocketWatcher's total with the same day computed directly from
   `system.billing.usage` and `list_prices` in Databricks. They should agree within about 1% when no
   discount is set.
5. After a week, confirm the warehouse PocketWatcher uses costs only a few dollars.

---

## 7. How it works

### The path of one sync

1. The worker loop asks the database for a workspace whose next run time has passed. It pushes the next run
   time forward first (6 hours for billing, 15 minutes for live checks), so no second worker takes the same
   job. On failure it retries in 30 minutes. **Sync now** on the workspace page only sets both next run times
   to now; the web app never runs a sync itself. It is refused within 5 minutes of the last finished billing
   sync (`MANUAL_SYNC_COOLDOWN` in `worker.py`), while a sync is queued, and while one is running (a claim
   newer than the last finished sync, up to 30 minutes old).
2. **Billing task** (`collectors/billing.py`): signs in to Databricks as the service principal, runs SQL on
   the customer's warehouse, and rewrites the affected days in the `daily_costs` table. It always re-reads
   the last 3 days plus any older day that received late or corrected records, so totals stay right.
3. The rules (`rules/`) read `daily_costs` and produce **findings**: title, dollar impact, owner, link.
4. The **dispatcher** (`alerts/dispatcher.py`) decides which findings go to which channels, sends them, and
   records every attempt in `alert_log`.
5. **Live task**: lists running all-purpose clusters and SQL warehouses through the REST API and runs the
   live rules the same way.

### Where things are

| Path | What is in it |
|---|---|
| `src/pocketwatcher/web/` | The web app: `app.py` (all pages and forms), `templates/`, `static/` (CSS, fonts), `charts.py`, `summary.py`, `auth.py` (WorkOS) |
| `src/pocketwatcher/worker.py` | The scheduler loop |
| `src/pocketwatcher/collectors/` | `billing.py` (billing sync), `live.py` (running compute) |
| `src/pocketwatcher/databricks/` | The Databricks client and the SQL it runs (`sql/*.sql`) |
| `src/pocketwatcher/rules/` | One file per alert rule, plus `config.py` (thresholds) and `engine.py` |
| `src/pocketwatcher/alerts/` | Dispatcher, Slack/Teams/email formats, mute links, allowed webhook hosts |
| `src/pocketwatcher/analytics.py` | All figures on the Analytics page, as plain functions |
| `src/pocketwatcher/onboarding/` | Setup commands for the wizard, connect checks, test alert |
| `src/pocketwatcher/db/models.py` | The database tables |
| `migrations/` | Database version history (alembic) |
| `tests/` | `unit/`, `integration/`, `fixtures/` (fake billing tables), `snapshots/` (alert formats) |
| `PRODUCT.md` | Who the product is for and the principles. Read this before changing the design. |

### Database tables

| Table | Holds |
|---|---|
| `tenants`, `users` | Account spaces and who belongs to them |
| `workspaces` | One row per connected workspace: URL, encrypted secret, warehouse, discount, rule settings, next run times |
| `daily_costs` | Estimated cost per day, workspace and resource (job, cluster, warehouse...) |
| `channels` | Slack, Teams and email destinations (encrypted) |
| `alert_log` | Every delivery attempt: sent or failed, with the error |
| `mutes` | Alerts a person muted, until when |

### The alert rules and their defaults

| Alert | Fires when | Default |
|---|---|---|
| Daily budget | Yesterday's spend is over the daily budget | Off until you set a budget |
| Monthly budget | Month to date is over budget (critical), or the forecast is (warning). Forecast = month to date + last-7-day average x days left | Off until you set a budget |
| Spend spike | A job/cluster/warehouse spends at least 2x its previous-7-day median, well above its normal variation, and above last week's and the week before's same weekday | At least $50 that day |
| New high spender | Something first seen under 5 days ago spends $200 or more in a day | $200 |
| Untagged spend | More than 10% of the last 7 days' spend lacks a required tag | Off until you list required tags |
| Photon or serverless on dev | Compute tagged `env` or `environment` = dev, development, sandbox or test uses Photon or serverless, at $10 a day or more (billing) or right now (live) | On |
| Cluster never auto-terminates | An all-purpose cluster is running with auto-termination off or over 120 minutes | On |
| Cluster running a long time | Running over 12 hours (only when auto-termination looks fine) | On |
| Warehouse never auto-stops | A SQL warehouse is running with auto-stop off or over 60 minutes | On |
| Weekly digest | Mondays: the top 10 cost drivers of the last week | On |

How alerts are delivered: each finding goes to each matching channel once. Ongoing live problems repeat after
24 hours. An alert is re-sent as "Update" if its cost grows by at least 50% and $25. A channel only receives
alerts at or above its chosen severity (the digest always goes). A failed send is retried on the next run.
**Mute for 7 days** in an alert opens a confirmation page; the mute happens only when you press the button
(link previews and mail scanners open links automatically, so one click must never mute).

Which settings the UI can change: monthly and daily budget, discount, required tags, and the values of the
`env` tag that mean "dev". Everything else is in `src/pocketwatcher/rules/config.py`. Note that the defaults
are **copied into each workspace when it is connected**, so editing `config.py` later only affects workspaces
connected afterwards.

---

## 8. Everyday commands

| Task | Command |
|---|---|
| Run all tests | `uv run pytest -q` |
| Run one file | `uv run pytest tests/unit/test_rules.py -q` |
| Run tests matching a name | `uv run pytest -q -k "budget"` |
| Lint, format, types (what CI also runs) | `uv run ruff check src tests` then `uv run ruff format src tests` then `uv run mypy` |
| Add a library | `uv add <package>`, then commit `pyproject.toml` and `uv.lock` |
| Change the database | Edit `db/models.py`, then `uv run alembic revision --autogenerate -m "what changed"`, **read the new file in `migrations/versions/`**, then `uv run alembic upgrade head` |
| Undo the last migration | `uv run alembic downgrade -1` |
| Rewrite alert-format golden files | `$env:PW_UPDATE_SNAPSHOTS = "1"; uv run pytest -q; Remove-Item Env:PW_UPDATE_SNAPSHOTS`, then review the diff in `tests/snapshots/` |
| Run tests on Postgres | `$env:PW_TEST_DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/test_db"; uv run pytest -q` |
| Reset the local database | Stop both programs, `Remove-Item pocketwatcher.db`, `uv run alembic upgrade head` |

**Never edit the first migration** (`migrations/versions/52e1debb3263_initial_schema.py`). It was regenerated
during development, but it is pushed now. Every later change must be a new migration.

Git: work happens on `development`. Pushing runs the CI workflow (`.github/workflows/ci.yml`): lint, types,
tests on SQLite and Postgres, and a migration up-and-down check. Results appear under the repository's
**Actions** tab: `https://github.com/dineshgudapati7-ship-it/PocketWatcher/actions`.

---

## 9. Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| A command (`alembic upgrade head`, or starting the app) stops after about 20 seconds with `connection timeout expired` | `PW_DATABASE_URL` points at a Postgres server that is not running or not reachable. | Start that server, or set `PW_DATABASE_URL=sqlite:///pocketwatcher.db` in `.env` for local use. |
| `Could not parse SQLAlchemy URL from given URL string` | The `PW_DATABASE_URL` value is malformed, typically the name pasted twice (`PW_DATABASE_URL=PW_DATABASE_URL=...`). | Fix the line in `.env`. **If it still fails**, the terminal holds an old copy of the setting, which takes priority over `.env` (the VS Code Python extension copies `.env` into new terminals). Run `Remove-Item Env:PW_DATABASE_URL`, or open a new terminal. |
| `/login` shows "Internal Server Error". The server log ends with `ValueError: WorkOS requires either an API key or a client ID` | The WorkOS keys in `.env` are empty. Seen on a fresh clone. | Fill `PW_WORKOS_API_KEY` and `PW_WORKOS_CLIENT_ID`, restart the web app. |
| WorkOS page says `redirect-uri-invalid` | The return address is not registered. | WorkOS dashboard, Staging, Redirects: add `http://localhost:8000/callback`. |
| WorkOS shows an error after you press Sign out | The sign-out return address is not registered. | Add `http://localhost:8000/` as a sign-out redirect. |
| The page says `Sign-in expired; please try again.`, or sign-in seems to work but you land on the public page again | The login cookie was not kept. Likely causes: you started on `127.0.0.1` while the redirect says `localhost`, or `PW_SECURE_COOKIES` is `true` while using plain `http`. | Always use `http://localhost:8000`. Keep `PW_SECURE_COOKIES=false` locally. Clear the site's cookies and try again. |
| You are signed out every time the web app restarts | `PW_SESSION_SECRET` is empty. | Set it in `.env`. |
| `RuntimeError: PW_FERNET_KEY is not set` (connecting a workspace, adding a channel) | No encryption key. | Generate one (section 2, step 3), restart. |
| `InvalidToken` when the worker or app reads a secret | The Fernet key was changed or lost. Stored secrets cannot be decrypted. | Restore the old key, or reconnect workspaces and re-add channels. |
| Red `NativeCommandError` text during migration in Windows PowerShell | PowerShell shows alembic's normal progress log as an error when stderr is redirected. | Ignore it. Look for `Running upgrade`. |
| Overview says "First sync within minutes" for a long time | The worker is not running, or the sync keeps failing. | Start the worker. Read its terminal for `failed for workspace N` and the error. |
| Worker log: `billing_task failed ...` | Common causes: expired or wrong OAuth secret (`invalid_client`, 401), missing grants (`INSUFFICIENT_PERMISSIONS`), wrong warehouse ID, warehouse deleted. | Fix the cause. The worker retries by itself every 30 minutes. |
| No alerts arrive | No channel, channel set to a higher severity than the alert, the alert is muted, the worker is not running, or sending failed. | Check **Channels** and **Send test**. In Overview, "Delivery" shows Sent or Failed; hover Failed for the error. |
| Slack or Teams channel will not save | Only official webhook addresses are accepted. | Slack: `https://hooks.slack.com/services/...`. Teams: the URL of a "When a Teams webhook request is received" workflow (`logic.azure.com`, `powerplatform.com` or `webhook.office.com`). |
| Email shows Failed | No SMTP settings. | Fill the `PW_SMTP_*` settings and restart. |
| Analytics shows "No data yet" | No workspace, or its first sync has not finished. | See section 6, or use sample data (section 5). |
| Analytics shows "Not enough history yet" or no change percentages | The saved history does not cover the previous period. | Expected. It fills in as history accumulates, or pick a shorter range. |
| uvicorn cannot start because port 8000 is already taken | Another copy is still running. | Stop it, or use `--port 8001` (and update the base URL and WorkOS redirects). |
| `database is locked` (SQLite) | Two writers at once, or OneDrive syncing the file. | Run one worker; pause OneDrive sync for the folder. |

---

## 10. What is verified and what is not

**Verified**

- A fresh clone of the pushed branch installs in seconds and `uv run pytest` passes (131 tests, SQLite).
- The same 131 tests and the migration pass on Postgres 16, run locally with a throwaway server. CI had been
  failing on Postgres because of one test (it used a workspace id that did not exist, which SQLite allows);
  fixed on 2026-10-28.
- Real Slack delivery (2026-10-28): one alert of each type (daily budget, new spender, spend spike, Photon on
  dev, untagged spend, weekly digest, cluster without auto-termination, warehouse without auto-stop) went
  through the real worker cycle and Slack sender, and Slack accepted all 8. The workspace data was fake, so the
  "Open in Databricks" and "Mute" buttons in those test messages pointed nowhere.
- On that clone: migration creates all tables; the web app starts and serves the landing page, styles and
  health check; protected pages redirect to sign-in; the worker starts and idles without errors.
- Real sign-in through WorkOS (Staging) worked on a developer machine, including the session and account
  creation. Sign-out through WorkOS was fixed after a bug report, but a later confirmation was not seen.
- Every page was inspected in a real browser at desktop and phone widths, light and dark.
- The sample-data steps in section 5 produce the pages described there.

**Not verified. Treat as unproven until you have tried it:**

| Item | Why it matters |
|---|---|
| **Anything against a real Databricks workspace** | All billing logic was tested against fake tables built to match Databricks' documented columns. Real column names may differ (for example `ingestion_date` and `pricing.effective_list.default` are assumed). |
| Deep links in alerts into Databricks (`/jobs/{id}`, `/compute/clusters/{id}`, `/sql/warehouses/{id}`, `/pipelines/{id}`, `/ml/endpoints/{id}`) | The paths were not checked on a live workspace. |
| That a service principal can list **all** clusters without extra permission | If not, the live checks will see only some clusters. |
| Idle detection | Databricks gives no "last activity" field, so alerts are based on auto-termination settings and run time, not on proven idleness. |
| **Real Teams and SMTP delivery** | Message formats are snapshot-tested and sending is tested with mocks, but nothing has been delivered to a real Teams channel or mailbox. Use **Send test** first. |
| The Mute button in a real alert | It needs a real workspace; the Slack test above used a fake one. |
| The WorkOS sign-out round trip after the fix | See above. |

**Not built yet** (from the plan): hosting and a deploy setup (no Dockerfile), billing and plans (Stripe),
teammate invites, and cloud (VM) cost alongside DBU cost.

---

## 11. Before putting it on a server

Nothing here is built or tested. It is the list of things that must change:

- A Postgres 16 database. Set `PW_DATABASE_URL`, and run `uv run alembic upgrade head` on every deploy
  before starting the new version.
- HTTPS in front of the web app. Set `PW_APP_BASE_URL=https://your.address` and `PW_SECURE_COOKIES=true`.
- WorkOS **Production** environment: its own keys, redirect URIs for the real address, and your own Google
  and Microsoft sign-in apps if you offer those.
- A real `PW_FERNET_KEY` and `PW_SESSION_SECRET` kept in a secret store, with the Fernet key backed up
  separately from the database.
- SMTP credentials, if you want email alerts.
- At least one web process and one worker process. More than one worker is safe on Postgres.
- Database backups, and somewhere to read the worker's logs.
- A test of the checklist in section 6 against a real workspace first.
