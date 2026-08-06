variable "region" {
  description = "阿里云地域，必须与 PAI Workspace 实际所在地域一致"
  type        = string
}

variable "project" {
  description = "项目标识，用于角色和策略命名"
  type        = string
  default     = "dataset-sink"
}

variable "account_id" {
  description = "阿里云账号 ID，取自 bootstrap 的 account_id output"
  type        = string
}

variable "oidc_provider_arn" {
  description = "OIDC 身份提供商 ARN，取自 bootstrap 的 oidc_provider_arn output"
  type        = string
}

variable "oidc_audience" {
  description = "OIDC aud，需与 workflow 里的 audience 一致"
  type        = string
  default     = "github-actions"
}

variable "github_repo" {
  description = "GitHub 仓库，格式 <org>/<repo>"
  type        = string
}

variable "github_oidc_repo" {
  description = "GitHub 不可变 OIDC 仓库段，格式 <owner>@<owner-id>/<repo>@<repo-id>"
  type        = string
}

variable "github_environment" {
  description = "该环境对应的 GitHub Environment 名称"
  type        = string
  default     = "development"
}

variable "lakefs_backend_bucket" {
  description = "lakeFS 的 OSS 后端桶名。只有沉降角色可读，其余角色显式 Deny"
  type        = string
}

variable "lakefs_backend_prefix" {
  description = "允许沉降角色读取的对象前缀，留空表示整桶"
  type        = string
  default     = ""
}

variable "dataset_bucket" {
  description = "数据集归档桶名，取自 platform 层的 dataset_bucket output"
  type        = string
}

variable "developer_group_name" {
  description = "研发 RAM 用户组名，留空则不创建该组"
  type        = string
  default     = ""
}

variable "pai_workspace_id" {
  description = "PAI Workspace ID（纯数字）。本层只管成员关系，不管 Workspace 本身"
  type        = string
}

variable "pai_members" {
  description = <<-EOT
    PAI Workspace 成员到角色的映射。这份 map 就是「谁能进生产工作空间」
    的唯一答案，评审时重点看这里的增删。

    user_id 是 RAM 用户 ID（纯数字），用 `aliyun ram ListUsers` 查，
    或直接跑 `make discover`。
  EOT
  type = map(object({
    user_id = string
    roles   = set(string)
  }))
  default = {}
}

variable "developer_user_names" {
  description = <<-EOT
    研发用户组的成员，填已存在的 RAM 用户登录名。这份列表是权威的：
    手工加进组的人会在下次 plan 里被标记为移除。
  EOT
  type        = list(string)
  default     = []
}

variable "imported_data_prefixes" {
  description = <<-EOT
    已被 lakeFS 零拷贝 import 引用的存量数据前缀。

    存量数据不需要迁移：scan-oss 列举出 manifest，commit 零拷贝 import 建
    Commit，全程不搬字节。但 Commit 只记录对象的物理地址，字节仍然只有原处
    那一份——删除或覆盖其中的对象会让已发布的 Commit 悬空，且当时不报错。

    在这里登记的前缀会：给沉降角色只读权限，并对全部五个身份 Deny 写删。
    这只约束本项目管理的身份，兜底仍需桶级 Policy + 版本控制 + WORM，
    见 docs/permissions.md 第 6.1 节。
  EOT
  type = list(object({
    bucket = string
    prefix = string
  }))
  default = []
}

variable "data_sources" {
  description = <<-EOT
    数据源注册表：管理员声明哪些对象存储位置可以作为数据源，用户只能从中选。

    这是管理面与用户面的分界。一份声明同时决定：沉降角色能读哪些前缀、
    哪些前缀禁止写删、以及导出给 CLI 本地校验的 deploy/data-sources.json。
    改它要走 access 层的 PR + 安全团队评审——因为它就是在改 RAM 策略。

    mode: readonly（只读，存量数据）/ archive（可写，归档前缀）。
  EOT
  type = list(object({
    name   = string
    bucket = string
    prefix = optional(string, "")
    mode   = optional(string, "readonly")
  }))
  default = []
}
