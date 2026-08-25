from __future__ import annotations

from datetime import datetime, timedelta, timezone

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import (
    SparkKubernetesOperator,
)


with DAG(
    dag_id="datalake_spark_iceberg_itest",
    description="Validate Airflow -> Spark on Kubernetes -> Iceberg on OSS.",
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "data-platform",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["datalake", "spark", "iceberg", "itest"],
) as dag:
    SparkKubernetesOperator(
        task_id="spark_iceberg_round_trip",
        namespace="datalake-itest",
        application_file="/opt/airflow/dags/repository/spark-iceberg-itest.yaml",
        kubernetes_conn_id="kubernetes_default",
        in_cluster=True,
        get_logs=True,
        delete_on_termination=False,
        do_xcom_push=False,
    )
