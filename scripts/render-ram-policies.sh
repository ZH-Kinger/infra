#!/bin/sh
# 从 Terraform 渲染 deploy/ram/*.json。
#
# 为什么需要这个脚本：deploy/ram/ 下的 JSON 是给人看的策略副本（评审、
# 审计、对外说明都看它），而实际生效的策略由 Terraform 生成。两份内容
# 一旦不一致，评审看的就是过期文档——这比没有文档更危险。
#
# 实现要点：用 `terraform console` 求值模块里的 local.policy_documents。
# console 只需要 init（-backend=false），**不需要 state，也不需要云凭证**，
# 所以这个脚本能在 CI 的 PR 阶段离线跑，成为一致性门禁。
#
# 变量取自 render.tfvars 里的固定占位符，保证渲染结果逐字节可复现。
#
# 注意：bootstrap 层的三个 CI 角色策略不在这里渲染——它们的 locals 引用了
# data.alicloud_account，求值需要真实凭证。那三份策略以 Terraform 代码
# 本身为准，评审时直接看 infra/bootstrap/oidc.tf。
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
module_dir="$project_dir/infra/modules/dataset-sink-roles"
out_dir="$project_dir/deploy/ram"
var_file="$module_dir/render.tfvars"

command -v terraform >/dev/null 2>&1 || {
  echo "未找到 terraform。安装：brew install hashicorp/tap/terraform" >&2
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "未找到 python3" >&2
  exit 1
}

test -f "$var_file" || {
  echo "缺少 $var_file" >&2
  exit 1
}

echo "==> terraform init（-backend=false，不需要凭证）"
terraform -chdir="$module_dir" init -backend=false -input=false -no-color >/dev/null

echo "==> 求值 local.policy_documents"
# 注意：不能用 `terraform -chdir=... console -var-file=<绝对路径以外的路径>`，
# -chdir 会让 -var-file 相对新工作目录解析。这里直接进目录，用相对文件名。
raw=$(cd "$module_dir" && printf 'local.policy_documents\n' \
  | terraform console -var-file=render.tfvars)

mkdir -p "$out_dir"

# 把 console 输出落到临时文件再交给 Python。
# 不能写成 `printf '%s' "$raw" | python3 - ... <<'PY'`：heredoc 会占用 stdin，
# 于是 python 从 heredoc 读程序、管道里的数据被丢弃，sys.stdin.read() 读到空。
console_out=$(mktemp "${TMPDIR:-/tmp}/ram-console.XXXXXX")
trap 'rm -f "$console_out"' EXIT HUP INT TERM
printf '%s' "$raw" > "$console_out"

# terraform console 输出的是 HCL 风格的 map，值是 JSON 字符串。
# 交给 Python 解析并逐个写文件，顺便统一格式化，避免无意义的 diff。
python3 - "$out_dir" "$console_out" <<'PY'
import json
import re
import sys
from pathlib import Path

out_dir = Path(sys.argv[1])
raw = Path(sys.argv[2]).read_text(encoding="utf-8")

# 形如：  "policy-name" = "{\"Version\":\"1\",...}"
pattern = re.compile(r'^\s*"([^"]+)"\s*=\s*("(?:[^"\\]|\\.)*")\s*$', re.M)
matches = pattern.findall(raw)
if not matches:
    sys.stderr.write("未能从 terraform console 输出中解析出任何策略：\n")
    sys.stderr.write(raw[:2000] + "\n")
    raise SystemExit(1)

written = []
for name, quoted in matches:
    document = json.loads(json.loads(quoted))
    target = out_dir / f"{name}.json"
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written.append(target.name)

# 清理已经不再由 Terraform 产出的旧策略文件，否则删掉一个角色后
# 它的策略副本会永远留在仓库里，误导评审。
keep = set(written) | {"README.md"}
for stale in sorted(out_dir.glob("*.json")):
    if stale.name not in keep:
        stale.unlink()
        print(f"  removed stale {stale.name}")

for name in sorted(written):
    print(f"  wrote {name}")
PY

# 数据源注册表与 RAM 策略同源于 var.data_sources，一起渲染，避免两边漂移：
# 「CLI 说没注册」和「RAM 拒绝访问」必须永远是同一个判断。
echo "==> 渲染数据源注册表"
ds_raw=$(cd "$module_dir" && printf 'local.data_sources_document\n' \
  | terraform console -var-file="$var_file")
ds_out=$(mktemp "${TMPDIR:-/tmp}/ds-console.XXXXXX")
printf '%s' "$ds_raw" > "$ds_out"

python3 - "$project_dir/deploy/data-sources.json" "$ds_out" <<'PYEOF'
import json
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
raw = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").strip()
# terraform console 把 jsonencode 的结果作为「带引号的字符串」输出，要解两层。
document = json.loads(json.loads(raw)) if raw.startswith('"') else json.loads(raw)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(
    json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print("  wrote " + target.name)
PYEOF
rm -f "$ds_out"

echo "==> 完成。deploy/ram/ 与 deploy/data-sources.json 现在与 Terraform 一致。"
