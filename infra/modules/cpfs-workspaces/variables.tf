variable "filesystem_id" {
  description = "CPFS 文件系统 ID。本模块只引用不创建——存储本身由 platform 层管理或 data source 引用"
  type        = string
}

variable "user_workspaces" {
  description = <<-EOT
    个人工作区，一人一个 Fileset。

    `name` 会成为路径段（`<users_root>/<name>/`），所以只允许小写字母、数字、
    连字符和下划线——路径里出现空格或中文会让后续的挂载和数据流动都很难排查。

    `size_limit_gib` / `file_count_limit` 是配额。**Provider 目前不支持在
    Fileset 上直接设配额**（`alicloud_nas_fileset` 没有 quota 属性），所以这两个
    值只被输出成命令，需要另外执行 `aliyun nas SetFilesetQuota`。这是已知缺口，
    不是遗漏——见 output `quota_commands`。
  EOT
  type = list(object({
    name             = string
    size_limit_gib   = optional(number)
    file_count_limit = optional(number)
  }))
  default = []

  validation {
    condition = alltrue([
      for w in var.user_workspaces : can(regex("^[a-z0-9][a-z0-9_-]*$", w.name))
    ])
    error_message = "user_workspaces 的 name 只能包含小写字母、数字、连字符和下划线，且以字母或数字开头。"
  }

  validation {
    condition     = length(distinct([for w in var.user_workspaces : w.name])) == length(var.user_workspaces)
    error_message = "user_workspaces 的 name 必须唯一。"
  }
}

variable "users_root" {
  description = "个人区根路径（文件系统内部视角，不带尾斜杠）"
  type        = string
  default     = "/users"
}

variable "shared_root" {
  description = "公共可读写区根路径。留空则不创建——公共区「全体可读写」意味着任何人可以改任何人的东西，要显式开启"
  type        = string
  default     = ""
}

variable "releases_root" {
  description = <<-EOT
    已发布 release 的根路径。必须与工作区分开：配额独立、只读挂载、
    且数据流动只绑这里。
  EOT
  type        = string
  default     = "/datasets"
}

variable "archive_bucket" {
  description = <<-EOT
    归档 OSS 桶名，用于建立发布区与对象存储之间的数据流动。留空则不建。

    这个桶必须满足两条（由 platform 层管理桶时保证，否则数据流动会失败）：
      1. 打上 `cpfs-dataflow` 标签，否则 CreateDataFlow 报
         "The OSS Bucket tag cpfs-dataflow is missing"；
      2. 开启版本控制，否则 Export（沉淀）报 InvalidSourceStorage.NeedVersioning。
    两条都是 2026-08-03 在真实 CPFS 上撞出来的，官方文档没写全。
  EOT
  type        = string
  default     = ""
}

variable "dataflow_throughput" {
  description = "数据流动吞吐上限（MB/s），只接受 600 / 1200 / 1500，且要小于文件系统 I/O 吞吐"
  type        = number
  default     = 600

  validation {
    condition     = contains([600, 1200, 1500], var.dataflow_throughput)
    error_message = "dataflow_throughput 只能是 600、1200 或 1500。"
  }
}

variable "deletion_protection" {
  description = "个人区与公共区是否开启删除保护。里面可能有还没归档的实验数据"
  type        = bool
  default     = true
}
