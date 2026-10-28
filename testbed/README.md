# Testbed: a known workload for checking PocketWatcher

A tiny Databricks bundle that creates billing records with known tags, so PocketWatcher's
figures and alerts can be checked against a real workspace (runbook section 6). It is not part
of the product and is never deployed to customers.

| Piece | Tags | What it exercises |
|---|---|---|
| Job `testbed-nightly-etl` (serverless, about 1 minute) | `team=data-platform`, `env=prod` | Daily cost per job, chargeback by team, spikes and the digest over time |
| Cluster `testbed-photon-dev` (created by hand, see below) | `env=dev`, `team=data-platform` | Live alerts: "auto-terminates never" and "Photon on dev" |
| Queries on the SQL warehouse | none | Warehouse cost and the untagged-spend share |

## Run

From this folder, with a CLI profile for the workspace (`TestProfile` here):

```powershell
databricks bundle deploy --profile TestProfile
databricks bundle run nightly_etl --profile TestProfile
```

In `dev` mode the job is named `[dev <user>] testbed-nightly-etl` and has no schedule; run it
again on later days to build history for the spike rule.

## The live-alert cluster

Single node `Standard_D4ds_v5`, runtime 18 LTS with Photon, `autotermination_minutes: 0`, tags
`env=dev`. Its VM bills to the Azure subscription while it runs, so **terminate it as soon as the
alerts have arrived**:

```powershell
databricks clusters delete <cluster-id> --profile TestProfile   # terminates; can be restarted
```

## Remove everything

```powershell
databricks bundle destroy --profile TestProfile
```
