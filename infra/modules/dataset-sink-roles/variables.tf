variable "environment" {
  description = "环境名，会拼进所有角色名和策略名，用于区分 dev/prod"
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment 只允许 dev 或 prod。新增环境时请同步更新审批流程。"
  }
}

variable "project" {
  description = "项目标识，用于资源命名和标签"
  type        = string
  default     = "dataset-sink"
}

variable "account_id" {
  description = "阿里云账号 ID，用于拼 OSS 资源 ARN"
  type        = string
}

variable "oidc_provider_arn" {
  description = "RAM OIDC 身份提供商 ARN，CI 角色的信任锚"
  type        = string
}

variable "oidc_audience" {
  description = "OIDC aud，需与 workflow 里 configure-aliyun-credentials-action 的 audience 一致"
  type        = string
}

variable "github_repo" {
  description = "GitHub 仓库，格式 <org>/<repo>。用于构造 OIDC sub 白名单"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo 必须是 <org>/<repo> 形式。"
  }
}

variable "github_environment" {
  description = <<-EOT
    允许假设写入类角色的 GitHub Environment 名称。发布流水线必须声明
    `environment: <这个值>`，人工审批才会生效；OIDC sub 也据此收紧。
  EOT
  type        = string
}

variable "lakefs_backend_bucket" {
  description = "lakeFS 的 OSS 后端桶名。沉降角色只读这里，其他角色一律禁止读"
  type        = string
}

variable "lakefs_backend_prefix" {
  description = "允许沉降角色读取的对象前缀（不含前导斜杠，留空表示整桶）"
  type        = string
  default     = ""
}

variable "dataset_bucket" {
  description = "数据集归档/输出桶名"
  type        = string
}

variable "dataset_staging_prefix" {
  description = "Staging 前缀，沉降角色可读写"
  type        = string
  default     = "staging"
}

variable "dataset_output_prefix" {
  description = "训练输出/checkpoint 前缀，训练运行角色可写"
  type        = string
  default     = "output"
}

variable "dataset_release_prefix" {
  description = "已发布数据集归档前缀，训练运行角色只读"
  type        = string
  default     = "datasets"
}

variable "training_runtime_service_principals" {
  description = <<-EOT
    训练运行角色的可信服务主体。PAI DLC/DSW 以服务身份扮演该角色，
    默认给 DLC 与 DSW。不要在这里加 CI 相关主体：运行身份和 CI 身份
    必须分离，否则流水线就能读训练数据。
  EOT
  type        = list(string)
  default     = ["pai-dlc.aliyuncs.com", "pai-dsw.aliyuncs.com"]
}

variable "deny_destructive" {
  description = <<-EOT
    是否为所有角色附加「禁止删除」策略。生产环境应保持 true：
    普通新增和配置更新照常，但 Terraform 计划删除或替换关键资源时会失败，
    需要改用单独审批的 BreakGlass 身份。
  EOT
  type        = bool
  default     = true
}

variable "max_session_duration" {
  description = "CI 角色的 STS 会话最长有效期（秒）"
  type        = number
  default     = 3600
}

variable "developer_group_name" {
  description = "研发人员 RAM 用户组名。人员加组即获得只读消费权限"
  type        = string
  default     = ""
}

variable "tags" {
  description = "附加到支持标签的资源上的标签"
  type        = map(string)
  default     = {}
}
