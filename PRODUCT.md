# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: the engineering or data lead of a small data team (1–10 data engineers) on Azure or AWS Databricks, usually with no FinOps person. They care about the monthly number and whether budgets will hold, more than about individual clusters. They check in roughly weekly, and otherwise arrive from an alert (Slack, Teams or email) asking "is this real, how bad, who owns it?".

Secondary: the data engineers who own the workspace. They do the one-time setup (service principal, grants, warehouse) and act on alerts: fix the job, set auto-termination, or mute.

## Product Purpose

PocketWatcher watches a Databricks workspace's spend and warns the team the day a problem starts, not weeks later on the invoice. It flags budget overruns and month-end forecasts, spend spikes against each job's or cluster's own baseline, new high spenders, clusters and warehouses that never stop, Photon or serverless on dev compute, untagged spend, and it sends a weekly top-10 digest. Success: a lead trusts the alerts enough to act on them, and runaway spend is caught within a day.

## Positioning

Opinionated, low-noise alerts in about 10 minutes of setup, delivered where the team already works. It does not compete on dashboards: Databricks ships cost dashboards and budgets, and enterprise tools (CloudZero, Unravel, Datadog) are priced for larger companies. Each alert says what fired, how much it costs, against what baseline, and who owns it, with a link to the resource and a one-click mute.

## Operating Context

- Hosted, multi-tenant web app. Sign-in through WorkOS AuthKit; each sign-up gets its own account space.
- A customer connects a workspace with a read-only OAuth service principal, grants `SELECT` on `system.billing`, `system.compute` and `system.lakeflow`, and gives it a small serverless SQL warehouse. Preflight checks verify each permission before anything is saved.
- Billing is synced every 6 hours (billing data is hours behind anyway); running clusters and warehouses are checked every 15 minutes through the REST APIs.
- Alert channels: Slack incoming webhooks, Teams Workflows webhooks, email (SMTP).
- Server-rendered pages (FastAPI + Jinja). Screens: landing, dashboard, connect-workspace wizard, workspace settings and alert history, channels, mute confirmation.

## Capabilities and Constraints

- Cost analytics page: spend trend against budget and forecast, change vs the previous period and its drivers, chargeback by a tag (team / cost_center), product and compute-type mix, top spenders and owners, CSV export. Comparisons are suppressed when stored history does not cover the previous period.

- Costs are **estimated list-price DBU cost** from `system.billing.list_prices`, reduced by a per-workspace discount. Cloud VM charges for classic compute and negotiated rates are not included; the UI must never present these numbers as invoice totals.
- Read-only: PocketWatcher never starts, stops or changes anything in the customer's workspace.
- Mute links in alerts must not act on GET (link previews and mail scanners follow them).
- Undecided: pricing and plans ($99/workspace/month and $249 multi-workspace were floated; not built), team invites, Stripe billing.

## Brand Commitments

- Name: PocketWatcher. No logo, colours or fonts exist yet.
- Voice: plain and precise, like a senior engineer's code review: specific numbers, what to do next, no hype, no exclamation marks.
- Visual standard (chosen 2026-10-27): professional and corporate, executed to the craft level of Stripe and Linear. A neutral palette, one indigo accent, colour only for status and data, an Inter interface face, dense readable tables. The earlier boarding-pass direction was dropped because it read as unusual in corporate offices.

## Evidence on Hand

None yet: no customers, testimonials, logos, benchmarks or case studies. Do not fabricate any. The motivating incident (a five-figure bill from Photon left on in a small proof of concept) is reported second-hand and unverified.

## Product Principles

1. Every number is honest about what it is: estimated, list price, as of the last sync.
2. Signal over volume: an alert that fires should be worth acting on; a quiet week stays quiet.
3. Lead with the consequence (dollars, forecast against budget), then the cause, then the owner and the fix.
4. Setup is part of the product: every failure says exactly what to run or change.
5. Read-only and least privilege, stated plainly wherever trust is asked for.
