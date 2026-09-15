output "saml_provider_arn" {
  description = "身份提供商 ARN"
  value       = alicloud_ram_saml_provider.this.arn
}

output "role_arns" {
  description = "角色名 → 角色 ARN"
  value       = { for k, r in alicloud_ram_role.this : k => r.arn }
}

output "feishu_role_attribute" {
  description = <<-EOT
    **直接粘贴到飞书**的 Role 属性值，角色名 → 属性值。

    飞书集成平台里加一条自定义 SAML 属性：
      name   https://www.aliyun.com/SAML-Role/Attributes/Role
      format uri
      value  这里的值

    格式是 `<角色ARN>,<提供商ARN>`，逗号连接、中间不能有空格。
    手拼这个串最容易出错（少个冒号、多个空格、两个 ARN 顺序反了），
    而错了之后阿里云只会回一句含糊的登录失败，所以这里直接生成。
  EOT
  value = {
    for k, r in alicloud_ram_role.this :
    k => "${r.arn},${alicloud_ram_saml_provider.this.arn}"
  }
}

output "feishu_attribute_checklist" {
  description = "飞书那边要配的三条属性，照着填即可"
  value = {
    "https://www.aliyun.com/SAML-Role/Attributes/Role" = "见 feishu_role_attribute（每个角色一个 SSO 应用）"
    "https://www.aliyun.com/SAML-Role/Attributes/RoleSessionName" = (
      "选预设值「邮箱名（不含 @ 及后缀）」或「工号」。"
      # 阿里云对这个值有字符集限制，中文姓名会被直接拒绝，
      # 而错误信息不会告诉你是因为姓名里有中文。
    )
    "https://www.aliyun.com/SAML-Role/Attributes/SessionDuration" = "可选，秒，须 ≤ 角色的 max_session_duration"
  }
}

output "sp_metadata_url" {
  description = "阿里云 SP 元数据地址，在飞书侧用「快捷导入」自动回填 ACS/Audience"
  value = (
    var.account_uid == ""
    ? "https://signin.aliyun.com/saml-role/sp-metadata.xml"
    : "https://signin.aliyun.com/saml/SpMetadata.xml?tenantID=${var.account_uid}"
  )
}
