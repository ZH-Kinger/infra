#!/bin/sh
set -eu

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
dag_id=datalake_spark_iceberg_itest
interop_id="${GITHUB_RUN_ID:-manual}"
s3_payload="s3-to-posix-${interop_id}"
posix_payload="posix-to-s3-${interop_id}"
s3_key="interop/${interop_id}/s3-to-posix.txt"
posix_key="interop/${interop_id}/posix-to-s3.txt"
nfs_payload="s3-to-nfs-${interop_id}"
nfs_write_payload="nfs-to-s3-${interop_id}"
nfs_key="interop/${interop_id}/s3-to-nfs.txt"
nfs_write_key="interop/${interop_id}/nfs-to-s3.txt"

# The quoted script must expand inside the remote container, not in this shell.
# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-s3-client -- \
  env OBJECT_KEY="$s3_key" EXPECTED="$s3_payload" /bin/sh -ec '
    mc alias set jfs "$S3_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
    printf "%s" "$EXPECTED" | mc pipe "jfs/factory/$OBJECT_KEY"
  '

# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-posix-client -- \
  env FILE="/jfs/$s3_key" EXPECTED="$s3_payload" /bin/sh -ec '
    attempt=0
    while [ "$attempt" -lt 60 ]; do
      actual=$(cat "$FILE" 2>/dev/null || true)
      [ "$actual" = "$EXPECTED" ] && exit 0
      attempt=$((attempt + 1))
      sleep 2
    done
    printf "S3-written object was not visible through JuiceFS POSIX mount: %s\n" "$FILE" >&2
    exit 1
  '

# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-posix-client -- \
  env FILE="/jfs/$posix_key" EXPECTED="$posix_payload" /bin/sh -ec '
    mkdir -p "$(dirname "$FILE")"
    printf "%s" "$EXPECTED" > "$FILE"
    sync
  '

# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-s3-client -- \
  env OBJECT_KEY="$posix_key" EXPECTED="$posix_payload" /bin/sh -ec '
    attempt=0
    while [ "$attempt" -lt 60 ]; do
      actual=$(mc cat "jfs/factory/$OBJECT_KEY" 2>/dev/null || true)
      [ "$actual" = "$EXPECTED" ] && exit 0
      attempt=$((attempt + 1))
      sleep 2
    done
    printf "POSIX-written file was not visible through JuiceFS S3 Gateway: %s\n" "$OBJECT_KEY" >&2
    exit 1
  '

printf '%s\n' "JuiceFS S3 <-> POSIX shared-namespace test completed."

# Exercise the real NFSv4 wire protocol, not merely another FUSE/POSIX client.
# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-s3-client -- \
  env OBJECT_KEY="$nfs_key" EXPECTED="$nfs_payload" /bin/sh -ec '
    printf "%s" "$EXPECTED" | mc pipe "jfs/factory/$OBJECT_KEY"
  '

# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-nfs-client -- \
  env FILE="/nfs/$nfs_key" EXPECTED="$nfs_payload" /bin/sh -ec '
    attempt=0
    while [ "$attempt" -lt 60 ]; do
      actual=$(cat "$FILE" 2>/dev/null || true)
      [ "$actual" = "$EXPECTED" ] && exit 0
      attempt=$((attempt + 1))
      sleep 2
    done
    printf "S3-written object was not visible through NFSv4: %s\n" "$FILE" >&2
    exit 1
  '

# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-nfs-client -- \
  env FILE="/nfs/$nfs_write_key" EXPECTED="$nfs_write_payload" /bin/sh -ec '
    mkdir -p "$(dirname "$FILE")"
    printf "%s" "$EXPECTED" > "$FILE"
    sync
  '

# shellcheck disable=SC2016
kubectl -n datalake-itest exec deployment/juicefs-s3-client -- \
  env OBJECT_KEY="$nfs_write_key" EXPECTED="$nfs_write_payload" /bin/sh -ec '
    attempt=0
    while [ "$attempt" -lt 60 ]; do
      actual=$(mc cat "jfs/factory/$OBJECT_KEY" 2>/dev/null || true)
      [ "$actual" = "$EXPECTED" ] && exit 0
      attempt=$((attempt + 1))
      sleep 2
    done
    printf "NFSv4-written file was not visible through S3: %s\n" "$OBJECT_KEY" >&2
    exit 1
  '

printf '%s\n' "JuiceFS S3 <-> NFSv4 shared-namespace test completed."

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
