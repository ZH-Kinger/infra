output "materializer_role_arn" {
  description = "沉降角色 ARN，填入发布流水线 materialize 步骤的 role-to-assume"
  value       = module.materializer_role.role_arn
}

output "register_role_arn" {
  description = "注册角色 ARN，填入 register-pai 步骤的 role-to-assume"
  value       = module.register_role.role_arn
}

output "dlc_submit_role_arn" {
  description = "作业提交角色 ARN，填入冒烟训练步骤的 role-to-assume"
  value       = module.dlc_submit_role.role_arn
}

output "training_runtime_role_arn" {
  description = "训练运行角色 ARN，填入 DLC CreateJob 的运行身份，不要给 CI"
  value       = alicloud_ram_role.training_runtime.arn
}

output "developer_group_name" {
  description = "研发用户组名，人员进出项目只需增删组成员"
  value       = var.developer_group_name == "" ? null : alicloud_ram_group.developers[0].group_name
}

output "developer_user_names" {
  description = "该组的权威成员列表。审计「谁能用这个环境的数据集」时看这里"
  value       = var.developer_group_name == "" ? [] : sort(var.developer_user_names)
}

output "policy_documents" {
  description = <<-EOT
    渲染后的全部策略 JSON，key 为 RAM 策略名。
    scripts/render-ram-policies.sh 消费这个 output 生成 deploy/ram/*.json，
    使文档副本和实际生效的策略不会不一致。
  EOT
  value       = local.policy_documents
}

output "assume_role_policies" {
  description = "各角色的信任策略 JSON，评审时用来确认 OIDC sub 没有被放宽"
  value = {
    materializer     = module.materializer_role.assume_role_policy
    register         = module.register_role.assume_role_policy
    dlc_submit       = module.dlc_submit_role.assume_role_policy
    training_runtime = local.training_runtime_trust
  }
}
