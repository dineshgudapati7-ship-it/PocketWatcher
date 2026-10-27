---
version: 1
slug: "src-pocketwatcher-web-templates"
primary_target: "src/pocketwatcher/web/templates"
related_targets: []
---

# PocketWatcher web app

Scope: every server-rendered screen (landing, overview, analytics, workspace, connect wizard, channels, mute). Mode: Operate for the app; the landing page is Persuade inside the same system.

Audience and job: an engineering or data lead (and the finance partner they report to) checks whether the month will hold, where the money goes and who owns it; engineers do the one-time setup.

Constraints: every dollar figure is an estimated list-price DBU cost; read-only product; mute acts only on POST; standard web controls only.

## Direction contract

THESIS: The category standard played straight, at Stripe/Linear craft: a calm, precise workspace where the month's spend against budget and its drivers are obvious in seconds. Refuses novelty theming in a corporate tool.

OWN-WORLD: Near-white page (#f7f7f8) with white cards and 1px #e6e6ea hairlines; one indigo accent (#5146d8) for primary actions and selection; status green/amber/red only with a label; Inter for all interface text, tabular figures in columns; dataviz reference palette for charts.

STORY: The lead opens Overview, reads each workspace's month-to-date, forecast and budget meter, scans ranked alerts, then opens Analytics for trend, chargeback by team, product mix and top spenders, and exports CSV for finance.

FIRST VIEWPORT: Left sidebar (Overview, Analytics, Channels, workspace list with status dots). Overview: page header with View analytics and Connect workspace; one summary card per workspace; Recent alerts table ranked by cost.

FORM: canon (category standard, chosen by the user with Stripe/Linear as the bar); replaces the boarding-pass challenger direction; seed:6bb8099e4d72a75a

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
