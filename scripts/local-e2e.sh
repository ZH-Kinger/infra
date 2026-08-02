#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/dataset-sink-e2e.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM

source_dir="$work_dir/source"
cpfs_root="$work_dir/cpfs"
release_dir="$cpfs_root/robotics/commit-e2e-001"
mkdir -p "$source_dir/raw" "$cpfs_root"
printf data > "$source_dir/raw/sample.bin"

PYTHONPATH=src python3 -m dataset_sink.cli materialize \
  --dataset robotics \
  --repository robotics-data \
  --commit commit-e2e-001 \
  --lakefs-tag robotics-v-e2e \
  --paimon-snapshot-id 1842 \
  --manifest examples/manifest.jsonl \
  --source local \
  --local-source-root "$source_dir" \
  --target-root "$cpfs_root"

PYTHONPATH=src python3 -m dataset_sink.cli verify \
  "$release_dir" --deep

manifest_sha256=$(PYTHONPATH=src python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["manifest_sha256"])' \
  "$release_dir/release.json")

DATASET_ROOT="$release_dir" \
DATASET_COMMIT=commit-e2e-001 \
DATASET_MANIFEST_SHA256="$manifest_sha256" \
PAIMON_SNAPSHOT_ID=1842 \
PYTHONPATH=src python3 -m dataset_sink.cli training-guard

PYTHONPATH=src python3 -m dataset_sink.cli pai-request \
  "$release_dir" \
  --dataset-id d-example \
  --region cn-hangzhou \
  --filesystem-id cpfs-example \
  --filesystem-path /datasets/robotics/commit-e2e-001 \
  --uri nas://cpfs-example.cn-hangzhou/datasets/robotics/commit-e2e-001/ \
  --output "$work_dir/pai-request.json"

printf '\nE2E passed. Generated request: %s\n' "$work_dir/pai-request.json"
