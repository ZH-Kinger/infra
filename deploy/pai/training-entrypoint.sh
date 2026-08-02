#!/bin/sh
set -eu

# DLC/DSW must mount the exact PAI Dataset Version at DATASET_ROOT as read-only.
# The job submitter injects the immutable identity from the release pipeline.
: "${DATASET_ROOT:=/mnt/dataset}"
: "${DATASET_COMMIT:?DATASET_COMMIT is required}"

set -- training-guard \
  --dataset-root "$DATASET_ROOT" \
  --expected-commit "$DATASET_COMMIT"
if [ -n "${DATASET_MANIFEST_SHA256:-}" ]; then
  set -- "$@" --expected-manifest-sha256 "$DATASET_MANIFEST_SHA256"
fi
if [ -n "${PAIMON_SNAPSHOT_ID:-}" ]; then
  set -- "$@" --expected-paimon-snapshot-id "$PAIMON_SNAPSHOT_ID"
fi
dataset-sink "$@"

# Replace this with torchrun/deepspeed. Keep model output on another writable mount.
exec python /workspace/train.py --dataset "$DATASET_ROOT" --output "${OUTPUT_ROOT:-/mnt/output}"
