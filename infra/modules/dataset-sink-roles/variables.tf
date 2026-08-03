variable "environment" {
  description = "环境名，会拼进所有角色名和策略名，用于区分 dev/prod"
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment 只允许 dev 或 prod。新增环境时请同步更新审批流程。"
  }
}

variable "project" {
  description = "项目标识，用于资源命名和标签"
  type        = string
  default     = "dataset-sink"
}

variable "account_id" {
  description = "阿里云账号 ID，用于拼 OSS 资源 ARN"
  type        = string
}

variable "oidc_provider_arn" {
  description = "RAM OIDC 身份提供商 ARN，CI 角色的信任锚"
  type        = string
}

variable "oidc_audience" {
  description = "OIDC aud，需与 workflow 里 configure-aliyun-credentials-action 的 audience 一致"
  type        = string
}

variable "github_repo" {
  description = "GitHub 仓库，格式 <org>/<repo>。用于构造 OIDC sub 白名单"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.github_repo))
    error_message = "github_repo 必须是 <org>/<repo> 形式。"
  }
}

variable "github_environment" {
  description = <<-EOT
    允许假设写入类角色的 GitHub Environment 名称。发布流水线必须声明
    `environment: <这个值>`，人工审批才会生效；OIDC sub 也据此收紧。
  EOT
  type        = string
}

variable "lakefs_backend_bucket" {
  description = "lakeFS 的 OSS 后端桶名。沉降角色只读这里，其他角色一律禁止读"
  type        = string
}

variable "lakefs_backend_prefix" {
  description = "允许沉降角色读取的对象前缀（不含前导斜杠，留空表示整桶）"
  type        = string
  default     = ""
}

variable "dataset_bucket" {
  description = "数据集归档/输出桶名"
  type        = string
}

variable "dataset_staging_prefix" {
  description = "Staging 前缀，沉降角色可读写"
  type        = string
  default     = "staging"
}

variable "dataset_output_prefix" {
  description = "训练输出/checkpoint 前缀，训练运行角色可写"
  type        = string
  default     = "output"
}

variable "dataset_release_prefix" {
  description = "已发布数据集归档前缀，训练运行角色只读"
  type        = string
  default     = "datasets"
}

variable "data_sources" {
  description = <<-EOT
    数据源注册表：管理员声明哪些对象存储位置可以作为数据源，用户只能从中选。

    这一份声明同时决定三件事，所以它是管理面和用户面的分界：
      1. 沉降角色能读哪些前缀（不再是整桶）
      2. 哪些前缀禁止写删（readonly——通常是已被 lakeFS 零拷贝 import 引用的
         存量数据，改了会让 Commit 悬空且当时不报错）
      3. 导出 deploy/data-sources.json 供 CLI 本地校验，让用户拿到
         「这个前缀没注册」而不是难懂的 AccessDenied

    mode:
      readonly  只读，禁止写删。存量数据前缀属于这类。
      archive   dataset-sink 写入的归档前缀。这类桶还需要打 cpfs-dataflow 标签
                并开启版本控制，否则 CPFS 数据流动的沉淀用不了——见 platform 层。
  EOT
  type = list(object({
    name   = string
    bucket = string
    prefix = optional(string, "")
    mode   = optional(string, "readonly")
  }))
  default = []

  validation {
    condition     = alltrue([for s in var.data_sources : contains(["readonly", "archive"], s.mode)])
    error_message = "data_sources 的 mode 只能是 readonly 或 archive。"
  }

  validation {
    condition     = length(distinct([for s in var.data_sources : s.name])) == length(var.data_sources)
    error_message = "data_sources 的 name 必须唯一。"
  }
}

variable "imported_data_prefixes" {
  description = <<-EOT
    已被 lakeFS 零拷贝 import 引用的存量数据前缀。

    存量数据大多本来就在 OSS 上。这类数据不需要再归档一遍：直接 import 建
    Commit 即可，全程不搬字节。但 import 是**零拷贝**的——Commit 只记录对象的
    物理地址，字节仍然只有原处那一份。因此这些前缀一旦被 import，就必须变成
    只读区：删除或覆盖其中的对象，等于让已发布的 Commit 悬空，版本记录还在但
    数据没了，且没有任何东西会在当时报错。

    在这里列出的前缀会：给沉降角色只读权限（scan-oss 需要列举和读取），
    并对全部五个身份 Deny 写入与删除。

    注意这只约束本模块管理的身份。持有 AliyunOSSFullAccess 的既有 RAM 用户
    仍然能删——真正的兜底是桶级 Policy + 版本控制 + 合规保留策略，
    见 docs/permissions.md。
  EOT
  type = list(object({
    bucket = string
    prefix = string
  }))
  default = []
}

variable "training_runtime_service_principals" {
  description = <<-EOT
    训练运行角色的可信服务主体。PAI DLC/DSW 以服务身份扮演该角色，
    默认给 DLC 与 DSW。不要在这里加 CI 相关主体：运行身份和 CI 身份
    必须分离，否则流水线就能读训练数据。
  EOT
  type        = list(string)
  default     = ["pai-dlc.aliyuncs.com", "pai-dsw.aliyuncs.com"]
}

variable "deny_destructive" {
  description = <<-EOT
    是否为所有角色附加「禁止删除」策略。生产环境应保持 true：
    普通新增和配置更新照常，但 Terraform 计划删除或替换关键资源时会失败，
    需要改用单独审批的 BreakGlass 身份。
  EOT
  type        = bool
  default     = true
}

variable "max_session_duration" {
  description = "CI 角色的 STS 会话最长有效期（秒）"
  type        = number
  default     = 3600
}

variable "developer_group_name" {
  description = "研发人员 RAM 用户组名。人员加组即获得只读消费权限"
  type        = string
  default     = ""
}

variable "developer_user_names" {
  description = <<-EOT
    该组的成员，填**已存在**的 RAM 用户登录名（不是 UserId）。

    这份列表是**权威的**：`alicloud_ram_group_membership` 管理整个成员集合，
    有人在控制台手工加进这个组，下次 plan 就会显示「要把他移除」。
    这正是我们要的——「谁有这个权限」只有一个答案，就是这份列表。

    重要：Terraform **不接管**这些用户本身，也不碰他们已有的其他策略。
    但要注意权限叠加：RAM 的有效权限是所有 Allow 的并集，而**显式 Deny
    优先于任何 Allow**。所以用户一旦入组，本项目策略里的 Deny（例如禁止
    访问 lakeFS 后端桶、禁止创建长期 AccessKey）会**覆盖**他通过其他策略
    已经拿到的 Allow。加人之前先确认这不会影响他的本职工作。
  EOT
  type        = list(string)
  default     = []

  validation {
    condition     = length(distinct(var.developer_user_names)) == length(var.developer_user_names)
    error_message = "developer_user_names 中有重复项。"
  }
}

variable "tags" {
  description = "附加到支持标签的资源上的标签"
  type        = map(string)
  default     = {}
}
