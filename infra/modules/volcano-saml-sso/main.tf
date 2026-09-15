# 火山引擎 SAML SSO：身份提供商 + （角色 SSO 时）一组可被假设的角色。
#
# 一个资源覆盖两种 SSO，靠 sso_type 区分（1=角色 SSO，2=用户 SSO）。这点和阿里云
# 不同：阿里云把两者拆成了两套东西——角色 SSO 是 `alicloud_ram_saml_provider`，
# 用户 SSO 走 IMS 的 SetUserSsoSettings，**Terraform 里没有对应资源**。
#
# 和阿里云那个模块是镜像关系，但有三处**不能照抄**的差异：
#
#   ① 字段名差一个下划线：火山是 encoded_saml_metadata_document，
#      阿里云是 encodedsaml_metadata_document。抄错了 terraform 会报
#      "Unsupported argument"，还算好查。
#
#   ② 火山的身份提供商有 sso_type 字段，角色 SSO 必须显式传 1。
#      传 2（用户 SSO）意味着完全不同的语义——见 variables.tf 里的警告。
#
#   ③ 资源标识符前缀是 trn: 不是 acs:，格式
#      trn:iam::<accountID>:saml-provider/<name>。拼给 IdP 的属性值时别搞混。

locals {
  # 火山的信任策略结构与阿里云一致（都借鉴了 AWS），但 Principal 里的键是
  # Federated + trn。这里同样锁 saml:recipient——断言在浏览器里流转，
  # 不锁接收方等于任何拿到这份签名断言的服务方都能来换我们的角色。
  trust_policy = jsonencode({
    Statement = [
      {
        Effect = "Allow"
        Action = ["sts:AssumeRole"]
        Principal = {
          Federated = [volcengine_iam_saml_provider.this.trn]
        }
        Condition = {
          StringEquals = {
            "saml:recipient" = var.saml_recipient
          }
        }
      },
    ]
  })
}

resource "volcengine_iam_saml_provider" "this" {
  saml_provider_name = var.provider_name
  description        = var.description

  sso_type = var.sso_type

  # 只有用户 SSO 才有 status。取值里的 2 会**禁用其他控制台登录方式**——
  # 也就是全员从此只能走 SSO，配错就是集体锁在门外。所以：
  #   · 默认 3（停用）：配好但不生效，跟阿里云「先传元数据、后开开关」同一个思路；
  #   · 想启用要显式写 1；
  #   · 写 2 会被变量校验拦住，必须同时把 i_understand_lockout 设成 true。
  # 角色 SSO 传 null，字段不生效。
  status = var.sso_type == 2 ? var.user_sso_status : null

  # 同阿里云：变量收原始 XML，在这里编码。好处是 git 里存的是可评审的 XML，
  # 且变量校验能真正检查里面有没有 EntityDescriptor 和签名证书。
  encoded_saml_metadata_document = base64encode(var.metadata_document)
}

resource "volcengine_iam_role" "this" {
  for_each = var.roles

  role_name             = each.key
  display_name          = coalesce(each.value.display_name, each.key)
  description           = each.value.description
  max_session_duration  = each.value.max_session_duration
  trust_policy_document = local.trust_policy
}

resource "volcengine_iam_role_policy_attachment" "this" {
  for_each = local.attachments

  role_name   = volcengine_iam_role.this[each.value.role].role_name
  policy_name = each.value.policy_name
  policy_type = each.value.policy_type
}

locals {
  # 摊平成稳定的 "<角色>/<策略>" 键。用序号当键的话，中间删一条策略会让
  # 后面所有条目键位移，plan 里显示成一堆无关资源的重建。
  attachments = merge([
    for role, cfg in var.roles : merge(
      { for p in cfg.system_policies : "${role}/${p}" => {
        role = role, policy_name = p, policy_type = "System"
      } },
      { for p in cfg.custom_policies : "${role}/${p}" => {
        role = role, policy_name = p, policy_type = "Custom"
      } },
    )
  ]...)
}

# status = 2 会禁用全员的其他控制台登录方式。变量级校验做不了跨变量检查，
# 所以用 lifecycle precondition 把这道闸门放在 plan 阶段——它会在 plan 输出里
# 就报错，而不是等 apply 到一半才失败。
resource "terraform_data" "lockout_guard" {
  count = var.sso_type == 2 && var.user_sso_status == 2 ? 1 : 0

  lifecycle {
    precondition {
      condition = var.i_understand_lockout
      error_message = join(" ", [
        "user_sso_status = 2 会禁用全员的其他控制台登录方式，配错即集体锁在门外。",
        "确实要这么做的话，同时设 i_understand_lockout = true。",
      ])
    }
  }
}
