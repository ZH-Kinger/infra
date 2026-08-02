#!/bin/sh
# 本地全链路演练：不连阿里云、不连 lakeFS、不产生任何费用。
#
# 覆盖两条路径：
#   A. CPFS 上处理完的数据 → scan → archive → certify 零拷贝发布
#   B. 从 lakeFS 沉降 → materialize
# 然后共用后半段：深度校验 → 训练门禁 → 生成 PAI 请求。
#
# 唯一没覆盖的是 `dataset-sink commit`（lakeFS 零拷贝 import），它需要
# 一个真实的 lakeFS 服务。这里用一个固定的 commit id 代替，正好也验证了
# certify 对「Commit 由外部产生」这个前提的处理。
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/dataset-sink-e2e.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT HUP INT TERM

export PYTHONPATH=src
sink() { python3 -m dataset_sink.cli "$@"; }

# ---------------------------------------------------------------------------
# 路径 A：CPFS 上处理完的数据接入版本体系
# ---------------------------------------------------------------------------
printf '\n===== A0. scan 必须拦住不属于数据集的文件 =====\n'
staging="$work_dir/cpfs-staging/batch-20260802-001"
mkdir -p "$staging/shards"
printf 'episode-000001-payload' > "$staging/shards/train-000000.bin"
printf 'episode-000002' > "$staging/shards/train-000001.bin"
# 这两个不是数据集内容。certify 发布前会因目录与 manifest 不一致而拒绝，
# 所以 scan 必须在最早的一步就拦下来，而不是让人白白归档一整轮。
printf 'junk' > "$staging/.DS_Store"
printf '{}' > "$staging/_READY"

if sink scan "$staging" --output "$work_dir/manifest.jsonl" >/dev/null 2>&1; then
  printf 'FAIL: scan 放行了含有 .DS_Store / _READY 的 staging\n' >&2
  exit 1
fi
printf 'scan 正确拒绝了不干净的 staging\n'

printf '\n===== A1. 清理后重新 scan =====\n'
rm -f "$staging/.DS_Store" "$staging/_READY"
sink scan "$staging" --output "$work_dir/manifest.jsonl"

printf '\n===== A2. archive：归档到对象存储（本地目录模拟）=====\n'
sink archive "$staging" \
  --manifest "$work_dir/manifest.jsonl" \
  --prefix "staging/batch-20260802-001" \
  --target local \
  --local-root "$work_dir/oss"

printf '\n===== A3. archive 幂等性：重跑应全部跳过 =====\n'
sink archive "$staging" \
  --manifest "$work_dir/manifest.jsonl" \
  --prefix "staging/batch-20260802-001" \
  --target local \
  --local-root "$work_dir/oss"

printf '\n===== A4. certify：CPFS 内零拷贝原子发布 =====\n'
# 真实环境里这个 commit id 来自 `dataset-sink commit`（lakeFS 零拷贝 import）。
commit_a=commit-from-cpfs-001
cpfs_root="$work_dir/cpfs/datasets"
mkdir -p "$cpfs_root"
sink certify \
  --prepared-dir "$staging" \
  --target-root "$cpfs_root" \
  --dataset robotics \
  --repository robotics-data \
  --commit "$commit_a" \
  --source-reference robotics-v-e2e-cpfs \
  --manifest "$work_dir/manifest.jsonl" \
  --lakefs-tag robotics-v-e2e-cpfs \
  --paimon-snapshot-id 1842

release_a="$cpfs_root/robotics/$commit_a"

printf '\n===== A5. 深度校验（重算全部 SHA-256）=====\n'
sink verify "$release_a" --deep

# ---------------------------------------------------------------------------
# 路径 B：从 lakeFS 沉降（用本地源适配器模拟 S3 Gateway）
# ---------------------------------------------------------------------------
printf '\n===== B1. materialize：从源沉降并发布 =====\n'
source_dir="$work_dir/source"
mkdir -p "$source_dir/raw"
printf data > "$source_dir/raw/sample.bin"

sink materialize \
  --dataset robotics \
  --repository robotics-data \
  --commit commit-e2e-001 \
  --lakefs-tag robotics-v-e2e \
  --paimon-snapshot-id 1842 \
  --manifest examples/manifest.jsonl \
  --source local \
  --local-source-root "$source_dir" \
  --target-root "$cpfs_root"

release_b="$cpfs_root/robotics/commit-e2e-001"

printf '\n===== B2. 深度校验 =====\n'
sink verify "$release_b" --deep

# ---------------------------------------------------------------------------
# 共用后半段：训练门禁 + PAI 请求
# ---------------------------------------------------------------------------
printf '\n===== C1. 训练启动门禁（fail-closed）=====\n'
manifest_sha256=$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["manifest_sha256"])' \
  "$release_a/release.json")

DATASET_ROOT="$release_a" \
DATASET_COMMIT="$commit_a" \
DATASET_MANIFEST_SHA256="$manifest_sha256" \
PAIMON_SNAPSHOT_ID=1842 \
  sink training-guard

printf '\n===== C2. 门禁必须拦住错误的 Commit =====\n'
if DATASET_ROOT="$release_a" \
   DATASET_COMMIT=wrong-commit \
   DATASET_MANIFEST_SHA256="$manifest_sha256" \
     sink training-guard >/dev/null 2>&1; then
  printf 'FAIL: 门禁放行了不匹配的 Commit\n' >&2
  exit 1
fi
printf '门禁正确拒绝了不匹配的 Commit\n'

printf '\n===== C3. 生成 PAI Dataset Version 请求 =====\n'
sink pai-request "$release_a" \
  --dataset-id d-example \
  --region cn-hangzhou \
  --filesystem-id cpfs-example \
  --filesystem-path "/datasets/robotics/$commit_a" \
  --uri "nas://cpfs-example.cn-hangzhou/datasets/robotics/$commit_a/" \
  --output "$work_dir/pai-request.json"

printf '\nE2E 全部通过。生成的请求: %s\n' "$work_dir/pai-request.json"
