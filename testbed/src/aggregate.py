# Databricks notebook source
# Synthetic ETL: enough work to show up in system.billing.usage, nothing else.
from pyspark.sql import functions as F

rows = int(dbutils.widgets.get("rows"))  # noqa: F821 - provided by Databricks

orders = (
    spark.range(rows)  # noqa: F821
    .withColumn("customer", F.col("id") % 100_000)
    .withColumn("amount", (F.col("id") * 7919 % 10_000) / 100)
)
summary = orders.groupBy("customer").agg(F.sum("amount").alias("total"), F.count("*").alias("orders"))
print(f"{summary.count()} customers, {summary.agg(F.sum('total')).first()[0]:,.2f} total")
