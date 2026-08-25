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
  -e "s/__OSS_ENDPOINT__/oss-cn-hangzhou-internal.aliyuncs.com/g" \
  "$root_dir/spark-iceberg-itest.yaml" > "$rendered_spark_application"

kubectl apply -f "$root_dir/namespace-rbac.yaml"

if ! kubectl -n datalake-itest get secret datalake-itest-minio >/dev/null 2>&1; then
  minio_password=$(openssl rand -hex 24)
  kubectl -n datalake-itest create secret generic datalake-itest-minio \
    --from-literal=MINIO_ROOT_USER=itestadmin \
    --from-literal=MINIO_ROOT_PASSWORD="$minio_password" \
    --from-literal=MINIO_ENDPOINT=http://minio.datalake-itest.svc.cluster.local:9000
fi

kubectl -n datalake-itest delete job minio-bootstrap --ignore-not-found

# The first failed deployment may have created unbound PVCs before an ESSD
# StorageClass was specified. PVC storageClassName is immutable, so repair only
# that empty, Pending test fixture. Never delete a Bound volume here.
repair_minio_pvcs=false
for ordinal in 0 1 2 3 4; do
  pvc="data-minio-$ordinal"
  phase=$(kubectl -n datalake-itest get pvc "$pvc" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  storage_class=$(kubectl -n datalake-itest get pvc "$pvc" -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true)
  if [ "$phase" = "Pending" ] && [ -z "$storage_class" ]; then
    repair_minio_pvcs=true
  fi
done
if [ "$repair_minio_pvcs" = true ]; then
  kubectl -n datalake-itest delete statefulset minio --ignore-not-found
  for ordinal in 0 1 2 3 4; do
    kubectl -n datalake-itest delete pvc "data-minio-$ordinal" --ignore-not-found
  done
fi

kubectl apply -f "$root_dir/minio.yaml"
kubectl -n datalake-itest rollout status statefulset/minio --timeout=20m
kubectl -n datalake-itest wait --for=condition=complete job/minio-bootstrap --timeout=10m

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

# A previous test release may have created immutable Airflow volume claim
# templates without a StorageClass. Repair only empty, Pending test PVCs;
# Bound volumes and PVCs with an explicit class are never removed here.
repair_airflow_pvcs=false
for pvc in data-airflow-postgresql-0 logs-airflow-scheduler-0 logs-airflow-triggerer-0; do
  phase=$(kubectl -n airflow get pvc "$pvc" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  storage_class=$(kubectl -n airflow get pvc "$pvc" -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true)
  if [ "$phase" = "Pending" ] && [ -z "$storage_class" ]; then
    repair_airflow_pvcs=true
  fi
done
if [ "$repair_airflow_pvcs" = true ]; then
  kubectl -n airflow delete statefulset \
    airflow-postgresql airflow-scheduler airflow-triggerer \
    --ignore-not-found
  for pvc in data-airflow-postgresql-0 logs-airflow-scheduler-0 logs-airflow-triggerer-0; do
    phase=$(kubectl -n airflow get pvc "$pvc" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    storage_class=$(kubectl -n airflow get pvc "$pvc" -o jsonpath='{.spec.storageClassName}' 2>/dev/null || true)
    if [ "$phase" = "Pending" ] && [ -z "$storage_class" ]; then
      kubectl -n airflow delete pvc "$pvc"
    fi
  done
fi

# A canceled GitHub Actions run can leave Helm's first install locked in a
# pending state. This is a disposable integration release, so remove only that
# incomplete release before retrying. Deployed releases are upgraded normally.
airflow_release_status=$(helm status airflow --namespace airflow 2>/dev/null |
  awk '/^STATUS:/ {print $2}' || true)
case "$airflow_release_status" in
  pending-install|pending-upgrade|pending-rollback)
    helm uninstall airflow --namespace airflow
    ;;
esac

helm upgrade --install airflow apache-airflow/airflow \
  --version 1.22.0 \
  --namespace airflow \
  --values "$root_dir/airflow-values.yaml" \
  --wait --timeout 25m

kubectl apply -f "$root_dir/namespace-rbac.yaml"
kubectl -n airflow rollout status deployment/airflow-scheduler --timeout=10m
