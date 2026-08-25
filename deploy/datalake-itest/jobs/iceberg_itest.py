from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from pyspark.sql import SparkSession
from pyspark.sql import functions as F


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is empty: {name}")
    return value


batch_id = required("BATCH_ID")
result_bucket = required("RESULT_BUCKET")
row_count = int(os.environ.get("ROW_COUNT", "1000000"))

spark = SparkSession.builder.appName(f"iceberg-itest-{batch_id}").getOrCreate()
hadoop = spark.sparkContext._jsc.hadoopConfiguration()
hadoop.set("fs.oss.endpoint", required("OSS_ENDPOINT"))
hadoop.set("fs.oss.accessKeyId", required("ALIBABA_CLOUD_ACCESS_KEY_ID"))
hadoop.set("fs.oss.accessKeySecret", required("ALIBABA_CLOUD_ACCESS_KEY_SECRET"))
hadoop.set("fs.oss.securityToken", required("ALIBABA_CLOUD_SECURITY_TOKEN"))
hadoop.set(
    "fs.oss.credentials.provider",
    "org.apache.hadoop.fs.aliyun.oss.AliyunCredentialsProvider",
)
hadoop.set("fs.s3a.access.key", required("MINIO_ROOT_USER"))
hadoop.set("fs.s3a.secret.key", required("MINIO_ROOT_PASSWORD"))
hadoop.set("fs.s3a.endpoint", required("MINIO_ENDPOINT"))
hadoop.set("fs.s3a.path.style.access", "true")
hadoop.set("fs.s3a.connection.ssl.enabled", "false")
hadoop.set(
    "fs.s3a.aws.credentials.provider",
    "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
)

table = "datalake.robotics.episode_index"
spark.sql("CREATE NAMESPACE IF NOT EXISTS datalake.robotics")
spark.sql(
    f"""
    CREATE TABLE IF NOT EXISTS {table} (
      episode_id BIGINT,
      batch_id STRING,
      robot_id STRING,
      duration_ms BIGINT,
      qc_score DOUBLE,
      passed BOOLEAN,
      created_at TIMESTAMP
    ) USING iceberg
    PARTITIONED BY (bucket(32, episode_id))
    TBLPROPERTIES ('format-version'='2')
    """
)

# A retry is idempotent: remove only this immutable batch, then append it again.
spark.sql(f"DELETE FROM {table} WHERE batch_id = '{batch_id}'")
rows = (
    spark.range(0, row_count)
    .withColumnRenamed("id", "episode_id")
    .withColumn("batch_id", F.lit(batch_id))
    .withColumn("robot_id", F.format_string("robot-%04d", F.col("episode_id") % 1000))
    .withColumn("duration_ms", F.lit(151000) + (F.col("episode_id") % 10000))
    .withColumn("qc_score", (F.col("episode_id") % 1000) / F.lit(1000.0))
    .withColumn("passed", F.col("qc_score") >= F.lit(0.2))
    .withColumn("created_at", F.current_timestamp())
)
rows.writeTo(table).append()

batch_count = spark.sql(
    f"SELECT count(*) AS n FROM {table} WHERE batch_id = '{batch_id}'"
).first()["n"]
if batch_count != row_count:
    raise RuntimeError(f"round-trip count mismatch: expected={row_count} actual={batch_count}")

snapshot = spark.sql(
    f"SELECT snapshot_id, committed_at FROM {table}.snapshots "
    "ORDER BY committed_at DESC LIMIT 1"
).first()
snapshot_id = int(snapshot["snapshot_id"])
snapshot_count = (
    spark.read.option("snapshot-id", str(snapshot_id))
    .table(table)
    .where(F.col("batch_id") == batch_id)
    .count()
)
if snapshot_count != row_count:
    raise RuntimeError(
        f"snapshot read mismatch: expected={row_count} actual={snapshot_count}"
    )

report = {
    "batch_id": batch_id,
    "table": table,
    "row_count": row_count,
    "snapshot_id": snapshot_id,
    "snapshot_count": snapshot_count,
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "status": "passed",
}
report_json = json.dumps(report, sort_keys=True)
(
    spark.createDataFrame(
        [(batch_id, snapshot_id, report_json)],
        ["batch_id", "snapshot_id", "report"],
    )
    .coalesce(1)
    .write.mode("overwrite")
    .json(f"oss://{result_bucket}/results/{batch_id}")
)
print(report_json)
spark.stop()
