#!/bin/sh
set -eu

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
dag_id=datalake_spark_iceberg_itest

dag_ready=false
attempt=0
while [ "$attempt" -lt 60 ]; do
  if kubectl -n airflow exec statefulset/airflow-scheduler -- \
    airflow dags list 2>/dev/null | grep -Fq "$dag_id"; then
    dag_ready=true
    break
  fi
  attempt=$((attempt + 1))
  sleep 5
done

if [ "$dag_ready" != true ]; then
  printf 'Airflow did not register DAG %s within five minutes.\n' "$dag_id" >&2
  kubectl -n airflow exec statefulset/airflow-scheduler -- \
    airflow dags list-import-errors >&2 || true
  kubectl -n airflow logs deployment/airflow-dag-processor \
    --tail=300 >&2 || true
  exit 1
fi

kubectl -n airflow exec statefulset/airflow-scheduler -- \
  airflow dags unpause "$dag_id"
kubectl -n airflow exec statefulset/airflow-scheduler -- \
  airflow dags trigger "$dag_id"

application=""
attempt=0
while [ -z "$application" ] && [ "$attempt" -lt 60 ]; do
  application=$(kubectl -n datalake-itest get sparkapplications \
    -o jsonpath='{range .items[*]}{.metadata.creationTimestamp}{" "}{.metadata.name}{"\n"}{end}' \
    | awk -v started="$started_at" '$1 >= started { print $2 }' | tail -n 1)
  attempt=$((attempt + 1))
  [ -n "$application" ] || sleep 5
done

if [ -z "$application" ]; then
  printf '%s\n' "Airflow did not create a SparkApplication within five minutes." >&2
  kubectl -n airflow logs statefulset/airflow-scheduler --tail=300 >&2 || true
  exit 1
fi

printf 'Monitoring SparkApplication %s\n' "$application"
attempt=0
while [ "$attempt" -lt 240 ]; do
  state=$(kubectl -n datalake-itest get sparkapplication "$application" \
    -o jsonpath='{.status.applicationState.state}')
  case "$state" in
    COMPLETED)
      driver=$(kubectl -n datalake-itest get sparkapplication "$application" \
        -o jsonpath='{.status.driverInfo.podName}')
      [ -z "$driver" ] || kubectl -n datalake-itest logs "$driver"
      printf '%s\n' "Airflow -> Spark -> Iceberg -> OSS integration test completed."
      exit 0
      ;;
    FAILED|FAILURE|FAILED_SUBMISSION|SUBMISSION_FAILED)
      kubectl -n datalake-itest describe sparkapplication "$application" >&2 || true
      driver=$(kubectl -n datalake-itest get sparkapplication "$application" \
        -o jsonpath='{.status.driverInfo.podName}')
      [ -z "$driver" ] || kubectl -n datalake-itest logs "$driver" >&2 || true
      exit 1
      ;;
  esac
  printf 'state=%s\n' "${state:-PENDING}"
  attempt=$((attempt + 1))
  sleep 10
done

printf '%s\n' "SparkApplication did not finish within 40 minutes." >&2
kubectl -n datalake-itest describe sparkapplication "$application" >&2 || true
exit 1
