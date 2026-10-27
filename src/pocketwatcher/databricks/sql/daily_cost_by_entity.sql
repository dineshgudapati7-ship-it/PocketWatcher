-- Estimated list cost per day and entity. Retractions/restatements carry signed quantities,
-- so a plain SUM yields the corrected total. Prices are matched on the usage start time.
WITH u AS (
  SELECT
    usage_date,
    sku_name,
    cloud,
    usage_unit,
    usage_start_time,
    usage_quantity,
    billing_origin_product AS product,
    CASE
      WHEN usage_metadata.job_id IS NOT NULL THEN 'job'
      WHEN usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
      WHEN usage_metadata.warehouse_id IS NOT NULL THEN 'warehouse'
      WHEN usage_metadata.endpoint_id IS NOT NULL THEN 'endpoint'
      WHEN usage_metadata.cluster_id IS NOT NULL THEN 'cluster'
      ELSE 'other'
    END AS entity_type,
    COALESCE(
      usage_metadata.job_id,
      usage_metadata.dlt_pipeline_id,
      usage_metadata.warehouse_id,
      usage_metadata.endpoint_id,
      usage_metadata.cluster_id,
      ''
    ) AS entity_id,
    identity_metadata.run_as AS run_as,
    to_json(custom_tags) AS tags_json,
    COALESCE(product_features.is_photon, FALSE) AS is_photon,
    COALESCE(product_features.is_serverless, FALSE) AS is_serverless
  FROM system.billing.usage
  WHERE workspace_id = {workspace_id}
    AND usage_date IN ({dates})
)
SELECT
  u.usage_date,
  u.sku_name,
  u.product,
  u.entity_type,
  u.entity_id,
  u.run_as,
  u.tags_json,
  u.is_photon,
  u.is_serverless,
  -- DOUBLE, not DECIMAL: DECIMAL(38,18) * DECIMAL(30,18) overflows on large quantities under ANSI mode.
  SUM(CAST(u.usage_quantity AS DOUBLE)) AS quantity,
  SUM(
    CAST(u.usage_quantity AS DOUBLE)
    * CAST(COALESCE(p.pricing.effective_list.default, p.pricing.default, 0) AS DOUBLE)
  ) AS list_cost,
  SUM(CASE WHEN p.sku_name IS NULL THEN CAST(u.usage_quantity AS DOUBLE) ELSE 0 END) AS unpriced_quantity
FROM u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name
  AND u.cloud = p.cloud
  AND u.usage_unit = p.usage_unit
  AND u.usage_start_time >= p.price_start_time
  AND (p.price_end_time IS NULL OR u.usage_start_time < p.price_end_time)
GROUP BY
  u.usage_date, u.sku_name, u.product, u.entity_type, u.entity_id,
  u.run_as, u.tags_json, u.is_photon, u.is_serverless
