#!/bin/sh
set -eu

: "${LANDING_BUCKET:?LANDING_BUCKET is required}"
: "${LAKEFS_BUCKET:?LAKEFS_BUCKET is required}"
: "${ICEBERG_BUCKET:?ICEBERG_BUCKET is required}"
: "${RESULT_BUCKET:?RESULT_BUCKET is required}"
: "${ALIBABA_CLOUD_ACCESS_KEY_ID:?temporary STS access key is required}"
: "${ALIBABA_CLOUD_ACCESS_KEY_SECRET:?temporary STS secret is required}"
: "${ALIBABA_CLOUD_SECURITY_TOKEN:?temporary STS token is required}"

root_dir=$(CDPATH='' cd -- "$(dirname "$0")" && pwd)
rendered_spark_application=$(mktemp)
trap 'rm -f "$rendered_spark_application"' EXIT HUP INT TERM

sed \
  -e "s/__ICEBERG_BUCKET__/$ICEBERG_BUCKET/g" \
  -e "s/__OSS_ENDPOINT__/oss-cn-hangzhou-internal.aliyuncs.com/g" \
  "$root_dir/spark-iceberg-itest.yaml" > "$rendered_spark_application"

kubectl apply -f "$root_dir/namespace-rbac.yaml"

kubectl -n datalake-itest create configmap datalake-itest-runtime \
  --from-literal=LANDING_BUCKET="$LANDING_BUCKET" \
  --from-literal=LAKEFS_BUCKET="$LAKEFS_BUCKET" \
  --from-literal=ICEBERG_BUCKET="$ICEBERG_BUCKET" \
  --from-literal=RESULT_BUCKET="$RESULT_BUCKET" \
  --from-literal=OSS_ENDPOINT="oss-cn-hangzhou-internal.aliyuncs.com" \
  --from-literal=ROW_COUNT="${ROW_COUNT:-1000000}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n datalake-itest create secret generic datalake-itest-oss-sts \
  --from-literal=ALIBABA_CLOUD_ACCESS_KEY_ID="$ALIBABA_CLOUD_ACCESS_KEY_ID" \
  --from-literal=ALIBABA_CLOUD_ACCESS_KEY_SECRET="$ALIBABA_CLOUD_ACCESS_KEY_SECRET" \
  --from-literal=ALIBABA_CLOUD_SECURITY_TOKEN="$ALIBABA_CLOUD_SECURITY_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n datalake-itest create configmap datalake-itest-spark-code \
  --from-file=iceberg_itest.py="$root_dir/jobs/iceberg_itest.py" \
  --dry-run=client -o yaml | kubectl apply -f -

helm repo add --force-update spark-operator https://kubeflow.github.io/spark-operator
helm repo add --force-update apache-airflow https://airflow.apache.org
helm repo update

helm upgrade --install spark-operator spark-operator/spark-operator \
  --version 2.5.2 \
  --namespace spark-operator \
  --create-namespace \
  --values "$root_dir/spark-operator-values.yaml" \
  --wait --timeout 15m

kubectl create namespace airflow --dry-run=client -o yaml | kubectl apply -f -
kubectl -n airflow create configmap datalake-itest-airflow-dags \
  --from-file=datalake_itest.py="$root_dir/dags/datalake_itest.py" \
  --from-file=spark-iceberg-itest.yaml="$rendered_spark_application" \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install airflow apache-airflow/airflow \
  --version 1.22.0 \
  --namespace airflow \
  --values "$root_dir/airflow-values.yaml" \
  --wait --timeout 25m

kubectl apply -f "$root_dir/namespace-rbac.yaml"
kubectl -n airflow rollout status deployment/airflow-scheduler --timeout=10m
