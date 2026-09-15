# 阿里云角色 SSO：SAML 身份提供商 + 一组可被假设的角色。
#
# 为什么是角色 SSO 而不是用户 SSO
# ────────────────────────────────
# 用户 SSO 要求断言里的 NameID 精确等于 `<RAM用户名>@<域名>`。实测飞书能放进
# NameID 的只有邮箱、企业邮箱、工号、姓名、别名等固定字段，**没有按人做字符串
# 变换的能力**——而我们的 RAM 用户名是「邮箱前缀去点」（zhang.san → zhangsan），
# 任何一个预设字段都对不上。
#
# 角色 SSO 不看 NameID（阿里云文档明确说明），只看两个自定义属性，而飞书支持
# 自定义 SAML 属性（最多 20 条，format 可选 uri）。所以这条路现在就能通。
#
# 用户 SSO 什么时候能做：飞书那边把每人的「别名」填成 RAM 用户名，或者我们把
# RAM 用户名改成带点的形式。两者都是人的决策，不该由这个模块替他们决定。

locals {
  # 角色 SSO 的信任策略：可信主体是身份提供商，不是某个账号。
  # 条件里锁 saml:recipient——不锁的话，任何拿到同一份 IdP 签名断言的
  # 服务方都能拿它来换我们账号的角色。断言是会在浏览器里流转的。
  assume_role_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Principal = {
          Federated = [alicloud_ram_saml_provider.this.arn]
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

resource "alicloud_ram_saml_provider" "this" {
  saml_provider_name = var.provider_name
  description        = var.description

  # 字段名里的 "encoded" 是字面意思：阿里云要的是 **Base64 编码后**的元数据。
  # 变量收原始 XML、在这里编码，有两个好处：
  #   ① git 里存的是可读可评审的 XML，而不是一坨看不出内容的 base64；
  #   ② 变量校验能真正检查「里面有没有 EntityDescriptor 和签名证书」——
  #      对 base64 串做这种检查是做不到的，只能等 apply 时阿里云报一句含糊的错。
  encodedsaml_metadata_document = base64encode(var.metadata_document)
}

resource "alicloud_ram_role" "this" {
  for_each = var.roles

  role_name                   = each.key
  assume_role_policy_document = local.assume_role_policy
  description                 = each.value.description
  max_session_duration        = each.value.max_session_duration

  # 刻意**不设** force = true。这些是人真正登进去用的角色，
  # 带着策略被 destroy 掉意味着一批人突然进不来；宁可让 destroy 失败、
  # 逼人显式先解绑，也不要静默连带删除。
}

resource "alicloud_ram_role_policy_attachment" "system" {
  for_each = local.system_attachments

  policy_name = each.value.policy_name
  policy_type = "System"
  role_name   = alicloud_ram_role.this[each.value.role].role_name
}

resource "alicloud_ram_role_policy_attachment" "custom" {
  for_each = local.custom_attachments

  policy_name = each.value.policy_name
  policy_type = "Custom"
  role_name   = alicloud_ram_role.this[each.value.role].role_name
}

locals {
  # for_each 要求 key 在 plan 期就能确定，所以把「角色 × 策略」摊平成
  # 稳定的 "<角色>/<策略>" 键。用 index 当键的话，增删一条策略会让后面
  # 所有条目的键位移，plan 里显示成一堆无关的重建。
  system_attachments = merge([
    for role, cfg in var.roles : {
      for p in cfg.system_policies : "${role}/${p}" => { role = role, policy_name = p }
    }
  ]...)

  custom_attachments = merge([
    for role, cfg in var.roles : {
      for p in cfg.custom_policies : "${role}/${p}" => { role = role, policy_name = p }
    }
  ]...)
}
