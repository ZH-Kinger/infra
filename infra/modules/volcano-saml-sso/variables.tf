variable "provider_name" {
  description = "IAM 身份提供商名称。它会出现在角色信任策略和 TRN 里，改名等于换一个提供商"
  type        = string
  default     = "feishu"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.provider_name))
    error_message = "身份提供商名只能含字母、数字、点、下划线和连字符。"
  }
}

variable "description" {
  description = "身份提供商说明，写清楚谁是 IdP、谁在维护"
  type        = string
  default     = "飞书 SSO（AnyCross 集成平台），由 Terraform 管理"
}

variable "metadata_document" {
  description = <<-EOT
    IdP 元数据 XML 的**内容**（不是路径，也不要预先 base64——模块内部会编码）。

    从飞书集成平台「身份集成 > 单点登录 > 应用管理 > SAML 2.0 协议端点
    > 下载元数据文档」导出。里面只有 IdP 公钥和登录地址，不含密钥，可以进 git。
  EOT
  type        = string

  validation {
    condition     = can(regex("(?i)<EntityDescriptor", var.metadata_document))
    error_message = "元数据必须是 SAML EntityDescriptor XML。传了路径、空串或已编码的 base64 都会命中这条。"
  }

  validation {
    condition     = can(regex("(?i)<X509Certificate>", var.metadata_document))
    error_message = "元数据里没有 X509Certificate——缺签名证书，火山无法验证断言，登录必然失败。"
  }
}

variable "saml_recipient" {
  description = <<-EOT
    信任策略里锁定的断言接收方，必须与火山 SP 的断言消费地址一致。

    不锁它的话，任何拿到同一份 IdP 签名断言的服务方都能来换我们账号的角色——
    而断言会在浏览器里流转。留空则不加这条 Condition（**不建议**）。
  EOT
  type        = string
  default     = "https://console.volcengine.com/auth/login/saml"
}

variable "account_id" {
  description = "火山主账号 ID，仅用于拼出给飞书粘贴的属性值。留空则该输出为空"
  type        = string
  default     = ""

  validation {
    condition     = var.account_id == "" || can(regex("^[0-9]{6,20}$", var.account_id))
    error_message = "火山主账号 ID 是一串数字。"
  }
}

variable "roles" {
  description = <<-EOT
    要创建的角色，key 是角色名。

    每个角色对应飞书那边的一个 SSO 应用——飞书的自定义属性值是**静态字面量**，
    一个应用只能带一个 Role 属性，也就只能登进一个角色。要按人分角色，就在飞书
    「访问授权 > 应用访问控制」里限定每个应用的可访问成员。

    system_policies 是火山托管策略名（如 ReadOnlyAccess），
    custom_policies 是本账号已存在的自定义策略名（本模块不创建策略）。
  EOT
  type = map(object({
    display_name         = optional(string)
    description          = optional(string, "由 Terraform 管理的 SSO 角色")
    max_session_duration = optional(number, 3600)
    system_policies      = optional(list(string), [])
    custom_policies      = optional(list(string), [])
  }))
  default = {}

  validation {
    condition = alltrue([
      for _, cfg in var.roles :
      cfg.max_session_duration >= 3600 && cfg.max_session_duration <= 43200
    ])
    error_message = "max_session_duration 取值 3600-43200 秒（1-12 小时）。"
  }

  validation {
    condition = alltrue([
      for _, cfg in var.roles :
      length(cfg.system_policies) + length(cfg.custom_policies) > 0
    ])
    error_message = "每个角色至少要挂一条策略；没有策略的角色登进去什么都做不了，通常是漏配。"
  }
}


variable "sso_type" {
  description = <<-EOT
    SSO 类型：1 = 角色 SSO，2 = 用户 SSO。

    用户 SSO：员工登进去就是自己的 IAM 账号，既有权限原样生效，但要求断言里的
    身份标识能对上 IAM 用户名。
    角色 SSO：员工扮演一个角色，不依赖用户名对齐，但所有人共用角色权限。
  EOT
  type        = number
  default     = 2

  validation {
    condition     = contains([1, 2], var.sso_type)
    error_message = "sso_type 只能是 1（角色 SSO）或 2（用户 SSO）。"
  }
}

variable "user_sso_status" {
  description = <<-EOT
    仅 sso_type = 2 时生效。

      1  启用
      2  启用，并**禁用其他控制台登录方式**  ← 全员从此只能走 SSO
      3  停用（默认）

    默认 3 是刻意的：先把元数据和属性都配好、验证断言正确，再单独一次改动开启。
    这样唯一有破坏性的动作被压缩成一行 diff，评审时一眼能看见。
  EOT
  type        = number
  default     = 3

  validation {
    condition     = contains([1, 2, 3], var.user_sso_status)
    error_message = "user_sso_status 只能是 1（启用）、2（启用并禁用其他登录方式）或 3（停用）。"
  }
}

variable "i_understand_lockout" {
  description = <<-EOT
    把 user_sso_status 设成 2 时必须同时设成 true。

    存在的意义只有一个：让「禁用全员其他登录方式」这件事**没法顺手做掉**。
    改一个数字和改一个数字加一句自白，在 code review 里是完全不同的两件事。
  EOT
  type        = bool
  default     = false
}
