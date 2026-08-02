terraform {
  required_version = ">= 1.5.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = ">= 1.252.0, < 2.0.0"
    }
  }

  # 与 platform 层**分开的 state**。
  #
  # 权限变更比基础设施变更敏感得多：需要不同审批人、不同节奏、不同的
  # apply 角色（TerraformAccessApplyRole）。混在一个 state 里会导致
  # 「改一个 tag 顺手带上一次提权」这种无法在审批里被看见的变更。
  #
  # dev 与 prod 的 .tf 内容刻意保持一致，差异只体现在 backend key、
  # environment、护栏松紧和 tfvars。这样「dev 能跑通 = prod 结构没问题」。
  backend "oss" {
    key    = "dev/access.tfstate"
    prefix = "terraform"
    acl    = "private"
  }
}

provider "alicloud" {
  region = var.region
}

module "roles" {
  source = "../../../modules/dataset-sink-roles"

  environment = "dev"
  project     = var.project
  account_id  = var.account_id

  oidc_provider_arn  = var.oidc_provider_arn
  oidc_audience      = var.oidc_audience
  github_repo        = var.github_repo
  github_environment = var.github_environment

  lakefs_backend_bucket = var.lakefs_backend_bucket
  lakefs_backend_prefix = var.lakefs_backend_prefix
  dataset_bucket        = var.dataset_bucket

  imported_data_prefixes = var.imported_data_prefixes

  developer_group_name = var.developer_group_name
  developer_user_names = var.developer_user_names

  # dev 允许删除：开发环境需要能重建资源做实验。
  # 这也是 dev/prod 唯一的实质性权限差异，改动它等于改变环境定位。
  deny_destructive = false
}

module "pai_access" {
  source = "../../../modules/pai-workspace-access"

  workspace_id = var.pai_workspace_id
  members      = var.pai_members

  # dev 放宽到 2 人，避免单点阻塞开发。
  max_admins = 2
}
