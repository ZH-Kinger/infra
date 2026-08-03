# 仅供 scripts/render-ram-policies.sh 渲染 deploy/ram/*.json 使用。
#
# 这里的值必须是**固定的占位符**，不是任何真实环境的配置：
# 渲染结果要在任何机器、任何时间都逐字节一致，CI 的一致性检查才成立。
# 真实环境的值在 infra/envs/*/access/terraform.tfvars 里。

environment           = "prod"
project               = "dataset-sink"
account_id            = "ACCOUNT_ID"
oidc_provider_arn     = "acs:ram::ACCOUNT_ID:oidc-provider/GitHubActions"
oidc_audience         = "github-actions"
github_repo           = "ORG/REPO"
github_environment    = "production"
lakefs_backend_bucket = "LAKEFS_BACKEND_BUCKET"
lakefs_backend_prefix = ""
dataset_bucket        = "DATASET_BUCKET"
developer_group_name  = "pai-prod-developers"
deny_destructive      = true

# 放一条占位前缀，好让 deploy/ram/*.json 里能看到「存量前缀被 import 之后
# 变成只读区」这组语句的实际形状——评审看的是渲染结果，不是变量默认值。
imported_data_prefixes = [
  { bucket = "LEGACY_BUCKET", prefix = "LEGACY_PREFIX" },
]

# 占位数据源，让 deploy/data-sources.json 和渲染出的策略里能看到这组语句的形状。
data_sources = [
  { name = "legacy-readonly", bucket = "LEGACY_BUCKET", prefix = "LEGACY_PREFIX", mode = "readonly" },
  { name = "sink-archive", bucket = "ARCHIVE_BUCKET", prefix = "releases", mode = "archive" },
  { name = "team-workspace", bucket = "WORKSPACE_BUCKET", prefix = "scratch", mode = "workspace" },
]
