# GitHub Actions OIDC 信任角色。
#
# 信任策略用 iss + aud + sub 三重约束。三者缺一不可：
#   - 只校验 iss：任何 GitHub 仓库都能假设这个角色。
#   - 只校验 iss + aud：aud 是我们自己填的字符串，其他仓库照样能填一样的值。
#   - 加上 sub：把可信主体钉到「某个仓库的某个 environment / 某个事件」。
#
# 因此 subjects 变量有 no-wildcard 校验：一个 repo:org/repo:* 就等于把
# 生产权限开放给该仓库的任意分支和任意 fork 的 PR。
locals {
  assume_role_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        Effect = "Allow"
        Action = "sts:AssumeRoleWithOIDC"
        Principal = {
          Federated = [var.oidc_provider_arn]
        }
        Condition = {
          StringEquals = {
            "oidc:iss" = var.issuer_url
            "oidc:aud" = var.audience
            "oidc:sub" = var.subjects
          }
        }
      },
    ]
  })
}

resource "alicloud_ram_role" "this" {
  # 字段名以 Provider 1.252.0 为准：旧的 name/document 已废弃，
  # 改用 role_name/assume_role_policy_document。
  role_name                   = var.role_name
  assume_role_policy_document = local.assume_role_policy
  description                 = var.description
  max_session_duration        = var.max_session_duration

  # force = true 允许在角色仍有策略附加时销毁。CI 角色是可重建的，
  # 但数据面角色不该这样配置，所以这个模块只用于 CI 角色。
  force = true
}

resource "alicloud_ram_policy" "this" {
  for_each = var.policy_documents

  policy_name     = each.key
  policy_document = each.value
  description     = "${var.role_name} 的最小权限策略，由 Terraform 管理，请勿在控制台手改"
  force           = true
}

resource "alicloud_ram_role_policy_attachment" "owned" {
  for_each = var.policy_documents

  policy_name = alicloud_ram_policy.this[each.key].policy_name
  policy_type = "Custom"
  role_name   = alicloud_ram_role.this.role_name
}

# 共享策略（如生产 deny-destructive 护栏）由调用方创建，这里只做附加。
resource "alicloud_ram_role_policy_attachment" "shared" {
  for_each = toset(var.attach_custom_policy_names)

  policy_name = each.value
  policy_type = "Custom"
  role_name   = alicloud_ram_role.this.role_name
}
