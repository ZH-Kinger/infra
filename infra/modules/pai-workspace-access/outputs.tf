output "member_ids" {
  description = "PAI 侧成员 ID，格式 <workspace_id>-<user_id>，import 与排错时需要"
  value       = { for k, m in alicloud_pai_workspace_member.this : k => m.id }
}

output "members_by_role" {
  description = "按角色反查成员，评审权限变更时比原始 map 更好读"
  value = {
    for role in [
      "PAI.AlgoDeveloper",
      "PAI.AlgoOperator",
      "PAI.LabelManager",
      "PAI.MaxComputeDeveloper",
      "PAI.WorkspaceAdmin",
      "PAI.WorkspaceGuest",
      "PAI.WorkspaceOwner",
      ] : role => sort([
        for k, m in var.members : k if contains(m.roles, role)
    ]) if length([for k, m in var.members : k if contains(m.roles, role)]) > 0
  }
}

output "admin_members" {
  description = "持有 WorkspaceAdmin/Owner 的成员，应保持尽可能少"
  value       = sort(local.admin_members)
}
