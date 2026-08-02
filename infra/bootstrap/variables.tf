variable "region" {
  description = "阿里云地域。注意与资源实际所在地域一致；aliyun CLI profile 的默认地域可能不同"
  type        = string
  default     = "cn-hangzhou"
}

variable "state_bucket" {
  description = "存放 Terraform state 的 OSS 桶名，全局唯一"
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", var.state_bucket))
    error_message = "OSS 桶名只能含小写字母、数字和连字符，长度 3-63，首尾必须是字母或数字。"
  }
}

variable "lock_instance_name" {
  description = "Tablestore 实例名，用于 Terraform state 锁"
  type        = string
  default     = "tf-state-lock"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,14}[a-z0-9]$", var.lock_instance_name))
    error_message = "Tablestore 实例名 3-16 字符，小写字母开头，只含小写字母、数字和连字符。"
  }
}

variable "lock_table_name" {
  description = "state 锁表名。表结构固定：单一主键 LockID(String)，由 OSS backend 约定"
  type        = string
  default     = "terraform_state_lock"
}

variable "github_repo" {
  description = "GitHub 仓库，格式 <org>/<repo>。决定哪个仓库可以假设 CI 角色"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo 必须是 <org>/<repo> 形式。"
  }
}

variable "github_environment" {
  description = "承载生产审批的 GitHub Environment 名称。apply 类角色只信任这个 Environment 的 OIDC token"
  type        = string
  default     = "production"
}

variable "oidc_provider_name" {
  description = "RAM OIDC 身份提供商名称"
  type        = string
  default     = "GitHubActions"
}

variable "oidc_audience" {
  description = "允许的 OIDC aud，需与 workflow 里 configure-aliyun-credentials-action 的 audience 一致"
  type        = string
  default     = "github-actions"
}

variable "oidc_thumbprints" {
  description = <<-EOT
    GitHub OIDC HTTPS CA 证书指纹（SHA-1）列表。
    GitHub 轮换证书时需要更新，建议同时保留新旧两个值以免中断。
    查询方式见 docs/runbook.md。
  EOT
  type        = list(string)

  validation {
    condition     = length(var.oidc_thumbprints) > 0
    error_message = "至少需要一个指纹，否则 OIDC 身份提供商无法验证 GitHub 的证书。"
  }
}

variable "state_retention_days" {
  description = "state 桶历史版本保留天数。state 是排查和回滚的最后依据，不要设得太短"
  type        = number
  default     = 365
}

variable "tags" {
  description = "附加到支持标签的资源上的标签"
  type        = map(string)
  default = {
    Project   = "dataset-sink"
    ManagedBy = "Terraform"
    Layer     = "bootstrap"
  }
}
