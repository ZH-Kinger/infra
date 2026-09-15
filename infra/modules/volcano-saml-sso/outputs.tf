output "saml_provider_trn" {
  description = "身份提供商 TRN（火山的资源标识符，前缀是 trn: 不是 acs:）"
  value       = volcengine_iam_saml_provider.this.trn
}

output "role_trns" {
  description = "角色名 → 角色 TRN"
  value       = { for k, r in volcengine_iam_role.this : k => r.trn }
}

output "feishu_role_attribute" {
  description = <<-EOT
    **直接粘贴到飞书**的 Role 属性值，角色名 → 属性值。

    飞书集成平台里加一条自定义 SAML 属性：
      name   https://www.volcengine.com/SAML/Attributes/Role
      format uri
      value  这里的值

    格式是 `<角色TRN>,<提供商TRN>`，逗号连接、中间不能有空格。
  EOT
  value = {
    for k, r in volcengine_iam_role.this :
    k => "${r.trn},${volcengine_iam_saml_provider.this.trn}"
  }
}

output "trust_policy" {
  description = "渲染后的信任策略 JSON，便于评审时直接比对"
  value       = local.trust_policy
}
