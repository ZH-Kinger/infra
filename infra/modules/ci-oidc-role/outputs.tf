output "role_arn" {
  description = "角色 ARN，填进 GitHub Environment 的 ALIBABA_CLOUD_ROLE_ARN 变量"
  value       = alicloud_ram_role.this.arn
}

output "role_name" {
  description = "角色名"
  value       = alicloud_ram_role.this.role_name
}

output "assume_role_policy" {
  description = "渲染后的信任策略 JSON，便于审计和评审时直接比对"
  value       = local.assume_role_policy
}

output "policy_documents" {
  description = "附加的自定义策略 JSON，key 为策略名"
  value       = var.policy_documents
}
