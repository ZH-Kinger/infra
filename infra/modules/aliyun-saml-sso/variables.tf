variable "provider_name" {
  description = "RAM 身份提供商名称。它会出现在角色 ARN 里，改名等于换一个提供商，飞书那边的属性值要跟着改"
  type        = string
  default     = "feishu"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,128}$", var.provider_name))
    error_message = "身份提供商名只能含字母、数字、点、下划线和连字符，长度 1-128。"
  }
}

variable "description" {
  description = "身份提供商说明，写清楚谁是 IdP、谁在维护"
  type        = string
  default     = "飞书 SSO（AnyCross 集成平台），由 Terraform 管理"
}

variable "metadata_document" {
  description = <<-EOT
    IdP 元数据 XML 的**内容**（不是路径）。从飞书集成平台
    「身份集成 > 单点登录 > 应用管理 > SAML 2.0 协议端点 > 下载元数据文档」导出。

    调用方通常写成 file("${path.module}/../../idp/feishu-metadata.xml")。
    元数据里只有 IdP 的公钥和登录地址，**不含任何密钥**，可以进 git。
  EOT
  type        = string

  validation {
    condition     = can(regex("(?i)<EntityDescriptor", var.metadata_document))
    error_message = "元数据必须是 SAML EntityDescriptor XML；传路径或空串都会在 apply 时才报错，这里先拦住。"
  }

  validation {
    condition     = can(regex("(?i)<X509Certificate>", var.metadata_document))
    error_message = "元数据里没有 X509Certificate——缺了签名证书，阿里云无法验证断言，登录必然失败。"
  }
}

variable "saml_recipient" {
  description = <<-EOT
    信任策略里锁定的断言接收方，必须与阿里云 SP 的断言消费地址一致。

    角色 SSO 固定是 https://signin.aliyun.com/saml-role/sso。
    不锁它的话，任何拿到同一份 IdP 签名断言的服务方都能来换我们账号的角色——
    而断言是会在浏览器里流转的。
  EOT
  type        = string
  default     = "https://signin.aliyun.com/saml-role/sso"
}

variable "account_uid" {
  description = "阿里云主账号 UID，仅用于拼出给飞书粘贴的属性值。留空则输出里该项为空"
  type        = string
  default     = ""

  validation {
    condition     = var.account_uid == "" || can(regex("^[0-9]{16}$", var.account_uid))
    error_message = "阿里云主账号 UID 是 16 位数字。"
  }
}

variable "roles" {
  description = <<-EOT
    要创建的角色，key 是角色名。

    每个角色对应飞书那边的一个 SSO 应用——因为飞书的自定义属性值是**静态字面量**，
    一个应用只能带一个 Role 属性，也就只能登进一个角色。要按人分角色，就在飞书
    「访问授权 > 应用访问控制」里限定每个应用的可访问成员。

    system_policies 是阿里云托管策略名（如 ReadOnlyAccess），
    custom_policies 是本账号已存在的自定义策略名（本模块不创建策略）。
  EOT
  type = map(object({
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
      for name, cfg in var.roles :
      length(cfg.system_policies) + length(cfg.custom_policies) > 0
    ])
    error_message = "每个角色至少要挂一条策略；没有策略的角色登进去什么都做不了，通常是漏配而不是本意。"
  }
}
