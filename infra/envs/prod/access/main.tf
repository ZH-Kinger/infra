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
  backend "oss" {
    key    = "prod/access.tfstate"
    prefix = "terraform"
    acl    = "private"
  }
}

provider "alicloud" {
  region = var.region
}

module "roles" {
  source = "../../../modules/dataset-sink-roles"

  environment = "prod"
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

  # 生产必须开启删除护栏：Terraform 计划删除或替换关键资源时直接失败，
  # 迫使这类变更走单独审批的 BreakGlass 身份，而不是被流水线静默执行。
  deny_destructive = true
}

module "pai_access" {
  source = "../../../modules/pai-workspace-access"

  workspace_id = var.pai_workspace_id
  members      = var.pai_members

  # 生产的管理员越少越好：管理员能改成员关系，等于能自我提权。
  max_admins = 1
}
