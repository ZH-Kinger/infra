# PAI Workspace 成员与角色。
#
# 这是四套授权面里的第 ② 层（见 docs/permissions.md）。它不能替代 RAM：
#   - RAM 权限还在、这里被删 → 用户能登录 PAI 但进不了工作空间。
#   - 这里还在、RAM 授权被撤 → 用户在成员列表里但调 API 全失败。
# 所以人员进出必须同时改这里和 RAM 授权，且放在同一个 PR 里。

locals {
  admin_roles = ["PAI.WorkspaceAdmin", "PAI.WorkspaceOwner"]

  admin_members = [
    for key, m in var.members : key
    if length(setintersection(m.roles, toset(local.admin_roles))) > 0
  ]
}

# 管理员数量护栏：管理员能改成员关系，等于能自己提权。
# 用 check 块而不是 variable validation，因为这个约束跨多个成员、
# 需要在计划阶段整体评估。
check "admin_count_within_limit" {
  assert {
    condition     = length(local.admin_members) <= var.max_admins
    error_message = "Workspace ${var.workspace_id} 的管理员数量 ${length(local.admin_members)} 超过上限 ${var.max_admins}：${join(", ", local.admin_members)}。管理员能修改成员关系，等于能自我提权，请收敛后再合并。"
  }
}

resource "alicloud_pai_workspace_member" "this" {
  for_each = var.members

  workspace_id = var.workspace_id
  user_id      = each.value.user_id
  roles        = each.value.roles
}
