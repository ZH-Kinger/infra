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

variable "github_oidc_repo" {
  description = "GitHub 不可变 OIDC 仓库段，格式 <owner>@<owner-id>/<repo>@<repo-id>"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+@[0-9]+/[A-Za-z0-9._-]+@[0-9]+$", var.github_oidc_repo))
    error_message = "github_oidc_repo 必须是 <owner>@<owner-id>/<repo>@<repo-id> 形式。"
  }
}

variable "platform_github_environments" {
  description = "可假设 PlatformApplyRole 的 GitHub Environments；必须与 terraform.yml 矩阵一致"
  type        = set(string)
  default     = ["development", "production"]

  validation {
    condition = length(var.platform_github_environments) > 0 && alltrue([
      for name in var.platform_github_environments : can(regex("^[A-Za-z0-9._-]+$", name))
    ])
    error_message = "platform_github_environments 不能为空，且只能包含字母、数字、点、下划线和连字符。"
  }
}

variable "access_github_environments" {
  description = "可假设 AccessApplyRole 的 GitHub Environments；生产权限应使用独立审批环境"
  type        = set(string)
  default     = ["development", "production-access"]

  validation {
    condition = length(var.access_github_environments) > 0 && alltrue([
      for name in var.access_github_environments : can(regex("^[A-Za-z0-9._-]+$", name))
    ])
    error_message = "access_github_environments 不能为空，且只能包含字母、数字、点、下划线和连字符。"
  }
}

variable "github_environment" {
  description = "已弃用，仅为兼容旧 tfvars；改用 platform_github_environments 与 access_github_environments"
  type        = string
  default     = null
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

variable "managed_name_prefixes" {
  description = <<-EOT
    本项目管理的资源名前缀。CI 角色的写权限**只覆盖名字以这些前缀开头的资源**，
    这是单账号内与其他项目隔离的核心机制。

    为什么需要它：没有这个约束时，PlatformApplyRole 能改账号里任意 OSS 桶的
    ACL，AccessApplyRole 能删账号里任意自定义 RAM 策略——包括其他项目的。
    「CI 只碰自己的资源」不能只靠 Terraform 代码写得对，必须在权限层强制。

    代价是命名纪律：本项目创建的桶、角色、策略、用户组都必须以这些前缀开头，
    否则 apply 会因权限不足失败（这正是想要的失败方式——早失败，且原因明确）。

    默认值覆盖两类命名：dataset-sink-*（角色与策略）和 pai-*（研发用户组）。
  EOT
  type        = list(string)
  default     = ["dataset-sink-", "pai-"]

  validation {
    condition     = length(var.managed_name_prefixes) > 0
    error_message = "至少要有一个前缀，否则 CI 角色将没有任何写权限。"
  }

  validation {
    condition     = alltrue([for p in var.managed_name_prefixes : length(p) >= 4])
    error_message = "前缀至少 4 个字符。太短的前缀（如 p-）起不到隔离作用。"
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
