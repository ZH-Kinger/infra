variable "workspace_id" {
  description = <<-EOT
    PAI Workspace ID，例如 617398。

    本模块**只管理成员关系，不管理 Workspace 本身**：Workspace 通常在开通 PAI
    时自动创建，纳管进 state 后一次属性漂移就可能触发替换，破坏半径远大于收益。
  EOT
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.workspace_id))
    error_message = "workspace_id 是纯数字（如 617398），不是 Workspace 名称。"
  }
}

variable "members" {
  description = <<-EOT
    成员到角色的映射。key 是给人看的标识（通常是登录名），value 里：
      user_id = RAM 用户 ID（纯数字，不是登录名，用 aliyun ram ListUsers 查）
      roles   = PAI 角色列表

    这份 map 就是「谁能进这个工作空间、能做什么」的唯一答案。
    人员离场时从这里删除，同时要撤销对应的 RAM 授权——两者必须同一个 PR。
  EOT
  type = map(object({
    user_id = string
    roles   = set(string)
  }))
  default = {}

  validation {
    condition = alltrue([
      for m in var.members : can(regex("^[0-9]+$", m.user_id))
    ])
    error_message = "user_id 必须是纯数字的 RAM 用户 ID，不能填登录名或邮箱。"
  }

  validation {
    condition = alltrue(flatten([
      for m in var.members : [
        for r in m.roles : contains([
          "PAI.AlgoDeveloper",
          "PAI.AlgoOperator",
          "PAI.LabelManager",
          "PAI.MaxComputeDeveloper",
          "PAI.WorkspaceAdmin",
          "PAI.WorkspaceGuest",
          "PAI.WorkspaceOwner",
        ], r)
      ]
    ]))
    error_message = "roles 只能取 PAI.AlgoDeveloper / PAI.AlgoOperator / PAI.LabelManager / PAI.MaxComputeDeveloper / PAI.WorkspaceAdmin / PAI.WorkspaceGuest / PAI.WorkspaceOwner（已于 2026-08-02 用 aliyun aiworkspace AddMemberRole --help 核实）。"
  }

  validation {
    condition = alltrue([
      for m in var.members : length(m.roles) > 0
    ])
    error_message = "成员至少要有一个角色；不需要任何权限就应该直接从 members 里删掉。"
  }
}

variable "max_admins" {
  description = <<-EOT
    允许持有 PAI.WorkspaceAdmin 或 PAI.WorkspaceOwner 的成员数上限。
    生产环境建议设为 1~2：管理员能改成员关系，等于能给自己加权限。
  EOT
  type        = number
  default     = 2
}
