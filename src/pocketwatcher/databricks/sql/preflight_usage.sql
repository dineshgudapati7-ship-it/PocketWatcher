SELECT COUNT(*) AS records, MAX(usage_date) AS latest
FROM system.billing.usage
WHERE workspace_id = {workspace_id}
  AND usage_date >= {since}
