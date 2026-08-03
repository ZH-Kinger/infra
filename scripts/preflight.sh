#!/bin/sh
# 接入新账号前的只读体检。
#
# 这个仓库里每一条「已知踩坑」都是在真实账号上撞出来的，代价是几十分钟到几小时。
# 本脚本把它们变成自动检查：**换一个账号时，先跑这个，而不是先跑流水线。**
#
# 与 discover-aliyun-ids.sh 的分工：
#   discover  回答「ID 填什么」——输出 tfvars 草稿
#   preflight 回答「能不能跑」——输出还差什么
#
# 本脚本**只调用只读 API**，不创建、不修改、不删除任何资源。
#
# 用法：
#   ALIYUN_PROFILE=my-profile REGION=cn-hangzhou ./scripts/preflight.sh
#
# 退出码：
#   0  全部通过（可能有 WARN）
#   1  有 FAIL，链路跑不起来
set -eu

PROFILE=${ALIYUN_PROFILE:-}
REGION=${REGION:-cn-hangzhou}
REGISTRY=${REGISTRY:-deploy/data-sources.json}

PASS_N=0
WARN_N=0
FAIL_N=0

# 计数器必须在主 shell 里累加。`cmd | while read` 会把循环体放进子 shell，
# 里面的 PASS_N=$((PASS_N+1)) 出了循环就丢——结果是逐条检查都打印了，
# 末尾的汇总却是 0，看起来「什么都没检查」。所以所有循环一律 `while ... done < 文件`。
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM

# 注意：不要把多个 flag 塞进一个变量在 zsh 下展开（zsh 不做单词切分）。
# 这里是 /bin/sh，会切分，所以下面的 $P 用法是安全的——但只在本脚本内安全。
if [ -n "$PROFILE" ]; then
  P="--profile $PROFILE"
else
  P=""
fi

AIWS_ENDPOINT="aiworkspace.${REGION}.aliyuncs.com"

section() { printf '\n\033[1m========== %s ==========\033[0m\n' "$1"; }
pass() { PASS_N=$((PASS_N + 1)); printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
warn() { WARN_N=$((WARN_N + 1)); printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
fail() { FAIL_N=$((FAIL_N + 1)); printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

command -v aliyun >/dev/null 2>&1 || {
  echo "未找到 aliyun CLI。安装：brew install aliyun-cli" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "未找到 python3。" >&2
  exit 1
}

# ---------------------------------------------------------------------------
section "1. 凭证与身份"
# ---------------------------------------------------------------------------
# shellcheck disable=SC2086
if ! identity=$(aliyun sts GetCallerIdentity $P 2>&1); then
  fail "凭证不可用"
  note "$identity"
  note "先执行 aliyun configure，或用 ALIYUN_PROFILE 指定 profile。"
  exit 1
fi
ACCOUNT_ID=$(printf '%s' "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccountId"])')
ARN=$(printf '%s' "$identity" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Arn"])')
pass "账号 ${ACCOUNT_ID}"

case "$ARN" in
*:root)
  warn "当前是主账号 root"
  note "root 绕过一切 RAM 限制，这套权限设计对它不生效。"
  note "建专用 RAM 用户或走 OIDC 角色，见 docs/permissions.md。"
  ;;
*) pass "非 root 身份：${ARN}" ;;
esac

# ---------------------------------------------------------------------------
section "2. NAS / CPFS 服务与文件系统 (region=${REGION})"
# ---------------------------------------------------------------------------
# shellcheck disable=SC2086
cpfs_raw=$(aliyun nas DescribeFileSystems --FileSystemType cpfs --region "$REGION" $P 2>&1) || true

case "$cpfs_raw" in
*User.Disabled* | *"not been activated"*)
  fail "NAS/CPFS 服务未开通"
  note "到控制台开通文件存储 NAS。未开通时所有 CPFS 检查都无法进行。"
  cpfs_raw=""
  ;;
esac

if [ -n "$cpfs_raw" ]; then
  fs_summary=$(printf '%s' "$cpfs_raw" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for fs in d.get("FileSystems", {}).get("FileSystem", []):
    print("\t".join([
        str(fs.get("FileSystemId", "")),
        str(fs.get("Status", "")),
        str(fs.get("ZoneId", "")),
        str(fs.get("ProtocolType", "")),
    ]))
' 2>/dev/null) || fs_summary=""

  if [ -z "$fs_summary" ]; then
    fail "没有 CPFS 文件系统"
    note "沉降目标不存在，materialize 无处可写。"
    note "注意 CPFS 最小容量 3600 GiB，且开通耗时经常超过一小时——"
    note "长时间 Pending 是正常的，不要当成失败去删（Pending 下也删不掉）。"
  else
    printf '%s\n' "$fs_summary" >"$TMP/fs"
    while IFS="$(printf '\t')" read -r fsid status zone proto; do
      [ -n "$fsid" ] || continue
      case "$fsid" in
      bmcpfs-*)
        warn "${fsid} 是 CPFS 智算版（LINGJUN）"
        note "智算版需要邀测开通，只能挂 PAI/ACS，挂不了 ECS。"
        note "而且**不支持 Evict**——reclaim 的 cpfs-evict 策略在它上面用不了，"
        note "只能用 hard-delete。"
        ;;
      cpfs-*) pass "${fsid} 是 CPFS 2.0（通用版）" ;;
      *) warn "${fsid} 不像 CPFS（普通 NAS？）" ;;
      esac
      if [ "$status" = "Running" ]; then
        pass "  状态 Running，可用区 ${zone}（协议 ${proto}）"
      else
        warn "  状态 ${status}，还不可用（Pending 请耐心等，不要删）"
      fi
    done <"$TMP/fs"
  fi
fi

# ---------------------------------------------------------------------------
section "3. CPFS 数据流动的前提"
# ---------------------------------------------------------------------------
# 六条前提是在真实 CPFS 2.0 上逐条撞出来的。这里能自动查的是三条：
# Fileset 存在、桶有 cpfs-dataflow 标签、桶开了版本控制。
# 另外三条（Throughput 取值、资源就绪、任务串行）只能在提交时体现。
if [ -n "${cpfs_raw:-}" ] && [ -n "${fs_summary:-}" ]; then
  first_fs=$(printf '%s' "$fs_summary" | head -1 | cut -f1)
  # shellcheck disable=SC2086
  fset_raw=$(aliyun nas DescribeFilesets --FileSystemId "$first_fs" --region "$REGION" $P 2>&1) || fset_raw=""
  fset_n=$(printf '%s' "$fset_raw" | python3 -c '
import json, sys
try:
    print(len(json.load(sys.stdin).get("Entries", {}).get("Entrie", [])))
except Exception:
    print(-1)
' 2>/dev/null || echo -1)
  if [ "$fset_n" -gt 0 ] 2>/dev/null; then
    pass "${first_fs} 上有 ${fset_n} 个 Fileset"
  elif [ "$fset_n" = "0" ]; then
    fail "${first_fs} 上没有 Fileset"
    note "数据流动的第一条前提：FsetId 必填，没有 Fileset 就建不了 DataFlow。"
    note "用 infra/modules/cpfs-workspaces 建。"
  else
    warn "无法读取 Fileset（可能是权限或该文件系统不支持）"
  fi
else
  note "跳过：没有可用的 CPFS 文件系统。"
fi

# ---------------------------------------------------------------------------
section "4. 数据源注册表里的桶"
# ---------------------------------------------------------------------------
if [ ! -f "$REGISTRY" ]; then
  warn "找不到注册表 ${REGISTRY}"
  note "跑 make render-ram 生成。"
else
  python3 -c '
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
for s in doc.get("data_sources", []):
    print(s.get("bucket", "") + "\t" + s.get("mode", "readonly"))
' "$REGISTRY" | sort -u >"$TMP/buckets"

  while IFS="$(printf '\t')" read -r bucket mode; do
    [ -n "$bucket" ] || continue
    # 渲染用的占位符不是真桶，跳过。
    case "$bucket" in
    *_BUCKET | LEGACY_* | ARCHIVE_* | WORKSPACE_* | DATASET_*)
      note "跳过占位符 ${bucket}（来自 render.tfvars，不是真实桶）"
      continue
      ;;
    esac

    # shellcheck disable=SC2086
    if ! aliyun oss stat "oss://${bucket}" --region "$REGION" $P >/dev/null 2>&1; then
      fail "桶 ${bucket} 不存在或不可访问"
      continue
    fi
    pass "桶 ${bucket}（mode=${mode}）存在"

    # archive 模式的桶要参与 CPFS 数据流动的沉淀（Export），有两条硬前提。
    if [ "$mode" = "archive" ]; then
      # shellcheck disable=SC2086
      tags=$(aliyun oss bucket-tagging --method get "oss://${bucket}" --region "$REGION" $P 2>&1) || tags=""
      case "$tags" in
      *cpfs-dataflow*) pass "  有 cpfs-dataflow 标签" ;;
      *)
        fail "  缺 cpfs-dataflow 标签"
        note "  没有这个标签，CreateDataFlow 会直接拒绝。这条在官方文档里很不显眼。"
        ;;
      esac

      # shellcheck disable=SC2086
      ver=$(aliyun oss bucket-versioning --method get "oss://${bucket}" --region "$REGION" $P 2>&1) || ver=""
      case "$ver" in
      *Enabled*) pass "  已开版本控制" ;;
      *)
        fail "  未开版本控制"
        note "  Export（沉淀）要求源桶开版本控制；Import（预热）不要求。"
        note "  它同时也是存量数据被 import 引用后的删除兜底。"
        ;;
      esac
    fi
  done <"$TMP/buckets"
fi

# ---------------------------------------------------------------------------
section "5. PAI Workspace"
# ---------------------------------------------------------------------------
# aiworkspace 必须显式带 endpoint，否则报 unknown endpoint。
# shellcheck disable=SC2086
ws_raw=$(aliyun aiworkspace GET /api/v1/workspaces --region "$REGION" --endpoint "$AIWS_ENDPOINT" $P 2>&1) || ws_raw=""
ws_list=$(printf '%s' "$ws_raw" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for w in d.get("Workspaces", []):
    print(str(w.get("WorkspaceId", "")) + "\t" + str(w.get("WorkspaceName", "")))
' 2>/dev/null) || ws_list=""

if [ -z "$ws_list" ]; then
  fail "在 ${REGION} 没找到 PAI Workspace"
  note "register-pai 需要 WorkspaceId。注意 Workspace 是分 region 的，"
  note "profile 的默认 region 未必是资源所在 region。"
else
  printf '%s\n' "$ws_list" >"$TMP/ws"
  while IFS="$(printf '\t')" read -r wsid wsname; do
    [ -n "$wsid" ] && pass "Workspace ${wsid} (${wsname})"
  done <"$TMP/ws"
fi

# ---------------------------------------------------------------------------
section "6. RAM 用户审计"
# ---------------------------------------------------------------------------
# 目的只有一个：找出那些**能删掉我们所有 Deny 语句**的人。
# 对他们而言这套权限设计等于不存在，把他们加进 developers 组是自欺欺人。
# shellcheck disable=SC2086
users=$(aliyun ram ListUsers $P 2>&1 | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for u in d.get("Users", {}).get("User", []):
    print(u.get("UserName", ""))
' 2>/dev/null) || users=""

if [ -z "$users" ]; then
  note "没有 RAM 用户，或没有 ram:ListUsers 权限。"
else
  risky=0
  for u in $users; do
    # shellcheck disable=SC2086
    pols=$(aliyun ram ListPoliciesForUser --UserName "$u" $P 2>&1 | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
print(" ".join(p.get("PolicyName", "") for p in d.get("Policies", {}).get("Policy", [])))
' 2>/dev/null) || pols=""
    case "$pols" in
    *AdministratorAccess* | *AliyunRAMFullAccess*)
      risky=$((risky + 1))
      warn "${u} 持有 ${pols}"
      note "他能直接删掉本项目的 Deny 语句。加进 developers 组之前先降权，"
      note "否则要承认整套模型在他身上不生效。"
      ;;
    esac
  done
  [ "$risky" = "0" ] && pass "没有用户持有 AdministratorAccess / AliyunRAMFullAccess"
fi

# ---------------------------------------------------------------------------
section "7. GitHub OIDC 信任锚"
# ---------------------------------------------------------------------------
# shellcheck disable=SC2086
oidc=$(aliyun ims ListOIDCProviders $P 2>&1) || oidc=""
case "$oidc" in
*token.actions.githubusercontent.com*)
  pass "GitHub Actions OIDC Provider 已存在"
  ;;
*)
  warn "没有 GitHub Actions 的 OIDC Provider"
  note "CI 拿不到临时凭证。由 infra/bootstrap 创建（管理员手工跑一次）。"
  ;;
esac

# ---------------------------------------------------------------------------
printf '\n\033[1m========== 结果 ==========\033[0m\n'
printf '  PASS %s   WARN %s   FAIL %s\n\n' "$PASS_N" "$WARN_N" "$FAIL_N"

if [ "$FAIL_N" -gt 0 ]; then
  echo "有 FAIL，链路跑不起来。按上面的提示逐条处理。"
  exit 1
fi
if [ "$WARN_N" -gt 0 ]; then
  echo "没有阻塞项，但有 WARN 需要你确认是否可接受。"
fi
echo "体检通过。"
