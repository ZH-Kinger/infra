output "materializer_role_arn" {
  description = "沉降角色 ARN，填入发布流水线 materialize/certify 步骤"
  value       = module.roles.materializer_role_arn
}

output "register_role_arn" {
  description = "注册角色 ARN，填入 register-pai 步骤"
  value       = module.roles.register_role_arn
}

output "dlc_submit_role_arn" {
  description = "作业提交角色 ARN，填入冒烟训练步骤"
  value       = module.roles.dlc_submit_role_arn
}

output "training_runtime_role_arn" {
  description = "训练运行角色 ARN，填入 DLC CreateJob 的运行身份。不要配给 CI"
  value       = module.roles.training_runtime_role_arn
}

output "policy_documents" {
  description = "渲染后的全部 RAM 策略，供 scripts/render-ram-policies.sh 生成 deploy/ram/*.json"
  value       = module.roles.policy_documents
}

output "assume_role_policies" {
  description = "各角色信任策略，评审时确认 OIDC sub 未被放宽"
  value       = module.roles.assume_role_policies
}

output "pai_members_by_role" {
  description = "按 PAI 角色反查成员，比原始 map 更适合在 PR 里核对"
  value       = module.pai_access.members_by_role
}

output "pai_admin_members" {
  description = "持有 WorkspaceAdmin/Owner 的成员，生产应保持最多 1 人"
  value       = module.pai_access.admin_members
}

output "data_sources_document" {
  description = "数据源注册表 JSON，与 RAM 策略同源"
  value       = module.roles.data_sources_document
}
