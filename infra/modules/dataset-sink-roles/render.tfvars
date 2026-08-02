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
