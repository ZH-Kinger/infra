#!/bin/sh
# 只读探测阿里云资源 ID，输出 terraform.tfvars 草稿。
#
# 用途：接入一个新账号时，把「填哪些 ID」这件事从翻控制台变成跑一条命令。
# 本脚本**只调用只读 API**，不创建、不修改、不删除任何资源。
#
# 用法：
#   ALIYUN_PROFILE=my-profile REGION=cn-hangzhou ./scripts/discover-aliyun-ids.sh
#
# 两个必须注意的坑（都踩过）：
#   1. aliyun CLI profile 的默认 region 未必是资源所在 region，所有命令显式带 --region。
#   2. aiworkspace 产品必须显式 --endpoint aiworkspace.<region>.aliyuncs.com，
#      否则报 "unknown endpoint"。
set -eu

PROFILE=${ALIYUN_PROFILE:-}
REGION=${REGION:-cn-hangzhou}
OUT_DIR=${OUT_DIR:-.discovery}

command -v aliyun >/dev/null 2>&1 || {
  echo "未找到 aliyun CLI。安装：brew install aliyun-cli" >&2
  exit 1
}

if [ -n "$PROFILE" ]; then
  P="--profile $PROFILE"
else
  P=""
fi

AIWS_ENDPOINT="aiworkspace.${REGION}.aliyuncs.com"
mkdir -p "$OUT_DIR"

section() {
  printf '\n========== %s ==========\n' "$1"
}

section "调用身份"
# shellcheck disable=SC2086
identity=$(aliyun sts GetCallerIdentity $P 2>&1) || {
  echo "$identity" >&2
  echo "凭证不可用。先执行 aliyun configure 或设置 profile。" >&2
  exit 1
}
echo "$identity"
account_id=$(printf '%s' "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccountId"])')
arn=$(printf '%s' "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')

case "$arn" in
  *:root)
    echo
    echo "!! 当前是**主账号 root**。不要用它跑 Terraform 或日常操作。"
    echo "   先创建专用 RAM 用户或走 OIDC 角色，见 docs/permissions.md。"
    ;;
esac

section "PAI Workspace（region=$REGION）"
# shellcheck disable=SC2086
workspaces=$(aliyun aiworkspace GET /api/v1/workspaces $P --region "$REGION" --endpoint "$AIWS_ENDPOINT" 2>&1) || true
echo "$workspaces" | head -40
workspace_id=$(printf '%s' "$workspaces" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(""); raise SystemExit
ws=d.get("Workspaces") or []
print(ws[0]["WorkspaceId"] if ws else "")
' 2>/dev/null || echo "")

if [ -n "$workspace_id" ]; then
  section "Workspace $workspace_id 的成员"
  # shellcheck disable=SC2086
  aliyun aiworkspace GET "/api/v1/workspaces/$workspace_id/members" $P \
    --region "$REGION" --endpoint "$AIWS_ENDPOINT" 2>&1 | head -40

  section "Workspace $workspace_id 的数据集"
  # shellcheck disable=SC2086
  aliyun aiworkspace GET /api/v1/datasets --WorkspaceId "$workspace_id" $P \
    --region "$REGION" --endpoint "$AIWS_ENDPOINT" 2>&1 | head -30
else
  echo "该 region 下没有 Workspace。若资源在别的 region，用 REGION=<其他> 重跑。"
fi

section "CPFS / NAS 文件系统"
# CPFS 走 nas API 的 FileSystemType=cpfs；灵骏 BMCPFS 走 eflo，不在这里。
# shellcheck disable=SC2086
aliyun nas DescribeFileSystems --FileSystemType cpfs $P --region "$REGION" 2>&1 | head -30 || true

section "OSS 桶"
# shellcheck disable=SC2086
aliyun oss ls $P 2>&1 | tail -20 || true

section "已有 RAM 角色（本项目相关）"
# shellcheck disable=SC2086
aliyun ram ListRoles $P 2>&1 | grep -E 'RoleName' | grep -viE 'aliyunservicerole|aliyun[a-z]*default' | head -20 || true

section "OIDC 身份提供商"
# shellcheck disable=SC2086
aliyun ims ListOIDCProviders $P 2>&1 | head -20 || true

section "RAM 用户（填 pai_members 的 user_id 用）"
# shellcheck disable=SC2086
aliyun ram ListUsers $P 2>&1 | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("(无法解析，可能无权限)"); raise SystemExit
for u in d.get("Users",{}).get("User",[]):
    print(f"  {u.get(\"UserName\",\"?\"):24} UserId={u.get(\"UserId\",\"?\")}")
' 2>&1 | head -30 || true

draft="$OUT_DIR/access.auto.tfvars.draft"
cat > "$draft" <<EOF
# 由 scripts/discover-aliyun-ids.sh 于探测时生成的草稿。
# 核对后复制进 infra/envs/<env>/access/terraform.tfvars。
#
# 注意：.discovery/ 已被 .gitignore 忽略（含账号 ID），不要提交。

region     = "$REGION"
account_id = "$account_id"

oidc_provider_arn = "acs:ram::$account_id:oidc-provider/GitHubActions"
oidc_audience     = "github-actions"

github_repo        = "REPLACE-ME-org/repo"
github_environment = "production"

lakefs_backend_bucket = "REPLACE-ME"
dataset_bucket        = "REPLACE-ME"

pai_workspace_id = "${workspace_id:-REPLACE-ME}"

# user_id 填上面「RAM 用户」一节里的 UserId（纯数字），不是登录名。
pai_members = {
  # someone = {
  #   user_id = "REPLACE-ME"
  #   roles   = ["PAI.AlgoDeveloper"]
  # }
}
EOF

section "完成"
echo "草稿已写入：$draft"
echo "下一步：核对后填入 infra/envs/<env>/access/terraform.tfvars。"
echo "提醒：本脚本只做了只读查询，没有创建或修改任何资源。"
