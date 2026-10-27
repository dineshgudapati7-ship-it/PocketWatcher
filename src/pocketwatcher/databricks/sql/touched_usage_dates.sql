-- Usage dates that may have changed since the last sync: recent days, plus any older day
-- that received late-arriving or corrected records (ingestion_date >= {since}).
SELECT DISTINCT usage_date
FROM system.billing.usage
WHERE workspace_id = {workspace_id}
  AND usage_date < {until}
  AND (usage_date >= {recent_from} OR ingestion_date >= {since})
