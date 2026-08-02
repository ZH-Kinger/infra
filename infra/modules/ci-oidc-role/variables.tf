variable "role_name" {
  description = "RAM 角色名，例如 TerraformPlanRole"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z][A-Za-z0-9.-]{0,63}$", var.role_name))
    error_message = "role_name 必须以字母开头，只含字母、数字、点和连字符，长度不超过 64。"
  }
}

variable "description" {
  description = "角色用途说明，会写入 RAM 控制台，便于审计时判断这个角色该不该存在"
  type        = string
}

variable "oidc_provider_arn" {
  description = "RAM OIDC 身份提供商 ARN，形如 acs:ram::<账号ID>:oidc-provider/<名称>"
  type        = string
}

variable "issuer_url" {
  description = "OIDC Issuer，GitHub Actions 固定为 https://token.actions.githubusercontent.com"
  type        = string
  default     = "https://token.actions.githubusercontent.com"
}

variable "audience" {
  description = "允许的 OIDC aud。必须与 workflow 里 configure-aliyun-credentials-action 的 audience 一致"
  type        = string
}

variable "subjects" {
  description = <<-EOT
    允许假设本角色的 OIDC sub 白名单。这是信任边界的核心：
    只写完全限定的形式，例如
      repo:<org>/<repo>:environment:production
      repo:<org>/<repo>:pull_request
      repo:<org>/<repo>:ref:refs/heads/main
    禁止使用 repo:<org>/<repo>:* 之类的通配，否则任意分支都能假设该角色。
  EOT
  type        = list(string)

  validation {
    condition     = length(var.subjects) > 0
    error_message = "subjects 不能为空，否则该角色没有任何可信主体。"
  }

  validation {
    condition     = alltrue([for s in var.subjects : !strcontains(s, "*")])
    error_message = "subjects 不允许包含通配符 *，必须写完全限定的 sub。"
  }

  validation {
    condition     = alltrue([for s in var.subjects : startswith(s, "repo:")])
    error_message = "subjects 必须以 repo: 开头（GitHub OIDC sub 的格式）。"
  }
}

variable "policy_documents" {
  description = <<-EOT
    由本模块创建并附加的自定义策略，key 是策略名，value 是策略 JSON 字符串。
    适用于「这个策略只服务于这一个角色」的情况。
  EOT
  type        = map(string)
  default     = {}
}

variable "attach_custom_policy_names" {
  description = <<-EOT
    附加到该角色的**已存在**自定义策略名。适用于一份策略要挂到多个角色的情况
    （例如生产的 deny-destructive 护栏），避免每个角色各建一份同名策略而冲突。
    调用方负责保证这些策略先被创建（用 depends_on 或资源引用建立顺序）。
  EOT
  type        = list(string)
  default     = []
}

variable "max_session_duration" {
  description = "STS 会话最长有效期（秒）。CI 用途取尽量小的值，默认 1 小时"
  type        = number
  default     = 3600

  validation {
    condition     = var.max_session_duration >= 3600 && var.max_session_duration <= 43200
    error_message = "max_session_duration 必须在 3600 到 43200 秒之间（阿里云 RAM 限制）。"
  }
}
