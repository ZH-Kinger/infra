# 五个身份的最小权限策略。
#
# 这里用 jsonencode 而不是 data "alicloud_ram_policy_document"，原因有两个：
#   1. 渲染结果就是 deploy/ram/*.json 需要的内容，scripts/render-ram-policies.sh
#      直接读 output 即可，不需要二次转换，也就不会出现两份不一致的策略。
#   2. 不依赖 Provider 的 data source schema，换 Provider 版本不会破。
#
# 关键约束一（2026-08-03 实测 AliyunPAIFullAccess 的 PolicyDocument）：
# PAI 的动作在官方定义里是 `pai:*`，且 Resource 为 `"*"`。
# **无法用 RAM Policy 限定到某个 Dataset / Job / Workspace**。所以这类权限的
# 收敛只能靠：收窄 Action 列表 + PAI Workspace 成员角色 + 流水线环境审批。
# 不要在评审时误以为 Resource: "*" 是偷懒，它是平台的硬限制。
#
# 关键约束二：**动作命名空间必须按下面的 ds_ns/job_ns/ws_ns 成对写**，
# 原因见 locals 里那段长注释。这条比约束一更容易出事，因为它静默失效。

locals {
  name_prefix = "${var.project}-${var.environment}"

  # ---------------------------------------------------------------------------
  # PAI 动作的命名空间：**必须两个都写**
  #
  # 2026-08-03 实测 `ram GetPolicyVersion --PolicyName AliyunPAIFullAccess`，
  # 官方策略授的是：
  #     "Action": ["pai:*", "paiplugin:*", "eas:*"]
  # 而不是本文件早先假定的 paidlc:* / paidataset:* / paiworkspace:*。
  # 把账号里所有含 PAI 的系统策略翻了一遍，没有一条用那三个命名空间。
  # ActionTrail 里 aiworkspace 的调用也记成 serviceName = "PAI"。
  #
  # 这件事的严重性在于：**RAM 按字面匹配动作串，命名空间不同就完全不匹配。**
  # 一个持有 AliyunPAIFullAccess（= pai:*）的人，被我们 Deny 掉
  # `paidataset:DeleteDataset` 是**没有任何效果**的——Deny 写错命名空间等于没写，
  # 而且不会报错、不会有任何迹象，看策略还以为拦住了。
  #
  # 无法离线确证 paidataset:/paidlc:/paiworkspace: 到底是「不存在」还是
  # 「存在但系统策略不用」，所以两个命名空间都写。取舍是明确的：
  #     多余的 Deny 无害（匹配不到任何请求）
  #     漏掉的 Deny 致命（静默失效）
  # Allow 侧同理，多写一个不存在的动作不会放大权限。
  #
  # 换账号时用 scripts/preflight.sh 复核一次：如果哪天官方策略改了命名空间，
  # 这里也要跟着加，而不是替换。
  # ---------------------------------------------------------------------------
  # 按动作族配对命名空间，而不是每个动作都乘以全部命名空间：
  # RAM 自定义策略文档有长度上限，全交叉会迅速把它撑爆，而且
  # `paidataset:CreateJob` 这种组合本身就是无意义的。
  ds_ns  = ["pai", "paidataset"]
  job_ns = ["pai", "paidlc"]
  ws_ns  = ["pai", "paiworkspace"]

  # ---- Deny 用（写错命名空间会静默失效，所以这几组最要紧）----------------

  # 数据集改写类。**CreateDataset 之前漏了**——只 Deny 了 CreateDatasetVersion，
  # 于是研发能新建一个 Dataset 指向任意 URI（包括可变位置），再在 GUI 的
  # DSW/DLC 里把它挂上去。这是「用 GUI 绕过流水线」最直接的一条路。
  pai_dataset_mutation_actions = flatten([
    for a in [
      "CreateDataset",
      "CreateDatasetVersion",
      "UpdateDataset",
      "UpdateDatasetVersion",
      "DeleteDataset",
      "DeleteDatasetVersion",
    ] : [for ns in local.ds_ns : "${ns}:${a}"]
  ])

  # 作业提交类。研发组之前只是「没 Allow」CreateJob，靠隐式拒绝——
  # 但隐式拒绝只在本组策略是他唯一策略时成立。既有用户往往还挂着
  # AliyunPAIFullAccess，并集里 CreateJob 就是放行的，所以必须显式 Deny。
  pai_job_submit_actions = flatten([
    for a in ["CreateJob", "UpdateJob"] : [for ns in local.job_ns : "${ns}:${a}"]
  ])

  # ---- Allow 用 ----------------------------------------------------------
  # 这几组写错命名空间的后果不同：Deny 是静默失效，Allow 是角色**完全不能工作**，
  # 一跑就报 AccessDenied。响亮的失败比静默的失效好，但两个都写就都不会发生。
  pai_dataset_read_actions = flatten([
    for a in ["GetDataset", "ListDatasets", "ListDatasetVersions", "GetDatasetVersion"] :
    [for ns in local.ds_ns : "${ns}:${a}"]
  ])
  pai_dataset_register_actions = flatten([
    for a in ["CreateDatasetVersion"] : [for ns in local.ds_ns : "${ns}:${a}"]
  ])
  pai_workspace_read_actions = flatten([
    for a in ["GetWorkspace", "ListWorkspaces"] : [for ns in local.ws_ns : "${ns}:${a}"]
  ])
  pai_workspace_browse_actions = flatten([
    for a in ["GetWorkspace", "ListWorkspaces", "ListMembers"] :
    [for ns in local.ws_ns : "${ns}:${a}"]
  ])
  pai_job_read_actions = flatten([
    for a in ["GetJob", "ListJobs", "GetPodLogs"] : [for ns in local.job_ns : "${ns}:${a}"]
  ])
  pai_job_operate_actions = flatten([
    for a in ["CreateJob", "GetJob", "ListJobs", "StopJob", "GetPodLogs"] :
    [for ns in local.job_ns : "${ns}:${a}"]
  ])
  pai_dsw_operate_actions = flatten([
    for a in ["CreateInstance", "GetInstance", "ListInstances", "StopInstance"] : [
      "pai:${a}",
      "paidsw:${a}",
    ]
  ])
  pai_dsw_mutation_actions = flatten([
    for a in ["CreateInstance", "UpdateInstance", "StartInstance", "StopInstance", "DeleteInstance"] : [
      "pai:${a}",
      "paidsw:${a}",
    ]
  ])
  # 检测性控制：DLC/DSW 的挂载来源审计。PAI 官方策略长期存在 pai:* 与
  # 产品命名空间不一致的问题，所以仍然成对写，理由同上。
  pai_dsw_audit_actions = flatten([
    for a in ["ListInstances", "GetInstance"] : [
      "pai:${a}",
      "paidsw:${a}",
    ]
  ])

  # OSS 资源 ARN：桶本身和桶内对象要分别声明，只写其中一个会导致
  # ListObjects 或 GetObject 之一失败。
  lakefs_bucket_arn = "acs:oss:*:${var.account_id}:${var.lakefs_backend_bucket}"
  lakefs_object_arn = var.lakefs_backend_prefix == "" ? "acs:oss:*:${var.account_id}:${var.lakefs_backend_bucket}/*" : "acs:oss:*:${var.account_id}:${var.lakefs_backend_bucket}/${var.lakefs_backend_prefix}/*"

  dataset_bucket_arn = "acs:oss:*:${var.account_id}:${var.dataset_bucket}"
  staging_object_arn = "acs:oss:*:${var.account_id}:${var.dataset_bucket}/${var.dataset_staging_prefix}/*"
  release_object_arn = "acs:oss:*:${var.account_id}:${var.dataset_bucket}/${var.dataset_release_prefix}/*"
  output_object_arn  = "acs:oss:*:${var.account_id}:${var.dataset_bucket}/${var.dataset_output_prefix}/*"

  # ---------------------------------------------------------------------------
  # 数据源注册表 → ARN
  #
  # 沉降角色只能读注册过的前缀。这比「给整个桶的读权限」窄得多，代价是新增
  # 数据源要改这里——但那正是我们想要的：谁能决定「什么算数据源」应该有一个
  # 明确的、需要评审的地方。
  # ---------------------------------------------------------------------------
  registered_object_arns = [
    for s in var.data_sources :
    s.prefix == "" ? "acs:oss:*:${var.account_id}:${s.bucket}/*" : "acs:oss:*:${var.account_id}:${s.bucket}/${s.prefix}/*"
  ]
  registered_bucket_arns = distinct([
    for s in var.data_sources : "acs:oss:*:${var.account_id}:${s.bucket}"
  ])
  # readonly 的数据源：禁止一切写删。archive 的不在此列，因为要往里归档。
  readonly_object_arns = [
    for s in var.data_sources : (
      s.prefix == "" ? "acs:oss:*:${var.account_id}:${s.bucket}/*" : "acs:oss:*:${var.account_id}:${s.bucket}/${s.prefix}/*"
    ) if s.mode == "readonly"
  ]

  read_registered_statements = length(var.data_sources) == 0 ? [] : [
    {
      Sid      = "ReadRegisteredDataSources"
      Effect   = "Allow"
      Action   = ["oss:GetObject", "oss:GetObjectMeta", "oss:HeadObject"]
      Resource = local.registered_object_arns
    },
    {
      Sid      = "ListRegisteredDataSourceBuckets"
      Effect   = "Allow"
      Action   = ["oss:ListObjects", "oss:GetBucketInfo"]
      Resource = local.registered_bucket_arns
    },
  ]

  # workspace 的数据源：用户自己的工作区，研发组在这里可读写。
  #
  # 这是整套策略里唯一给**人**（而不是给 CI 角色）的写权限。理由是发布协议
  # 只管「已发布的不可变 release」，但用户总得有地方做预处理和实验；不给一个
  # 合法的可写位置，他们就会往别处写，而别处往往是没人看得住的地方。
  #
  # 边界很清楚：可写 ≠ 可发布。工作区能被 scan-oss 扫描，但 `commit` 会拒绝
  # 以它为物理位置——见 registry.assert_commit_source。
  workspace_object_arns = [
    for s in var.data_sources : (
      s.prefix == "" ? "acs:oss:*:${var.account_id}:${s.bucket}/*" : "acs:oss:*:${var.account_id}:${s.bucket}/${s.prefix}/*"
    ) if s.mode == "workspace"
  ]
  workspace_bucket_arns = distinct([
    for s in var.data_sources : "acs:oss:*:${var.account_id}:${s.bucket}" if s.mode == "workspace"
  ])

  workspace_rw_statements = length(local.workspace_object_arns) == 0 ? [] : [
    {
      Sid    = "ReadWriteWorkspaceDataSources"
      Effect = "Allow"
      Action = [
        "oss:GetObject",
        "oss:GetObjectMeta",
        "oss:HeadObject",
        "oss:PutObject",
        "oss:DeleteObject",
        "oss:AbortMultipartUpload",
      ]
      Resource = local.workspace_object_arns
    },
    {
      Sid      = "ListWorkspaceDataSourceBuckets"
      Effect   = "Allow"
      Action   = ["oss:ListObjects", "oss:GetBucketInfo"]
      Resource = local.workspace_bucket_arns
    },
  ]

  deny_readonly_source_statements = length(local.readonly_object_arns) == 0 ? [] : [
    {
      Sid    = "DenyMutatingReadonlyDataSources"
      Effect = "Deny"
      Action = [
        "oss:PutObject",
        "oss:DeleteObject",
        "oss:DeleteObjects",
        "oss:AbortMultipartUpload",
        "oss:PutObjectTagging",
        "oss:RestoreObject",
      ]
      Resource = local.readonly_object_arns
    },
  ]

  # 存量数据前缀：被 lakeFS 零拷贝 import 引用之后即为只读区。
  # 桶 ARN 单独列一份，ListObjects 作用在桶上而不是对象上。
  imported_object_arns = [
    for p in var.imported_data_prefixes :
    "acs:oss:*:${var.account_id}:${p.bucket}/${p.prefix}/*"
  ]
  imported_bucket_arns = distinct([
    for p in var.imported_data_prefixes :
    "acs:oss:*:${var.account_id}:${p.bucket}"
  ])

  # 有前缀才生成语句：Resource 为空列表的 Statement 在 RAM 里是非法的。
  read_imported_statements = length(var.imported_data_prefixes) == 0 ? [] : [
    {
      Sid      = "ReadImportedLegacyObjects"
      Effect   = "Allow"
      Action   = ["oss:GetObject", "oss:GetObjectMeta", "oss:HeadObject"]
      Resource = local.imported_object_arns
    },
    {
      Sid      = "ListImportedLegacyBuckets"
      Effect   = "Allow"
      Action   = ["oss:ListObjects", "oss:GetBucketInfo"]
      Resource = local.imported_bucket_arns
    },
  ]

  # 显式 Deny 优先于任何 Allow，所以这条同时也压住了上面那条读权限之外的一切写操作。
  deny_imported_mutation_statements = length(var.imported_data_prefixes) == 0 ? [] : [
    {
      Sid    = "DenyMutatingImportedLegacyObjects"
      Effect = "Deny"
      Action = [
        "oss:PutObject",
        "oss:DeleteObject",
        "oss:DeleteObjects",
        "oss:AbortMultipartUpload",
        "oss:PutObjectTagging",
        "oss:RestoreObject",
      ]
      Resource = local.imported_object_arns
    },
  ]

  # ---------------------------------------------------------------------------
  # 1. 沉降角色：读 lakeFS 后端固定 Commit，写 CPFS。
  #    明确不给 PAI 注册和训练提交权限——能写数据的不能宣布数据可用。
  # ---------------------------------------------------------------------------
  materializer_policy = jsonencode({
    Version = "1"
    Statement = concat([
      {
        Sid    = "ReadLakeFSBackendObjects"
        Effect = "Allow"
        Action = [
          "oss:GetObject",
          "oss:GetObjectMeta",
          "oss:HeadObject",
        ]
        Resource = [local.lakefs_object_arn]
      },
      {
        Sid    = "ListLakeFSBackendBucket"
        Effect = "Allow"
        Action = [
          "oss:ListObjects",
          "oss:GetBucketInfo",
        ]
        Resource = [local.lakefs_bucket_arn]
      },
      {
        Sid    = "ReadWriteStagingArchive"
        Effect = "Allow"
        Action = [
          "oss:GetObject",
          "oss:PutObject",
          "oss:ListObjects",
        ]
        Resource = [local.dataset_bucket_arn, local.staging_object_arn]
      },
      {
        # CPFS 挂载需要能查文件系统与挂载点。真正能读写哪些目录由 CPFS
        # Fileset 和 POSIX 权限决定，RAM 在这里只回答「能不能看到这个文件系统」。
        Sid    = "DescribeCpfsForMount"
        Effect = "Allow"
        Action = [
          "nas:DescribeFileSystems",
          "nas:DescribeMountTargets",
          "nas:DescribeFilesets",
          "nas:DescribeProtocolService",
        ]
        Resource = ["*"]
      },
      {
        Sid      = "DenyDatasetRegistrationAndTraining"
        Effect   = "Deny"
        Action   = concat(local.pai_dataset_mutation_actions, local.pai_job_submit_actions)
        Resource = ["*"]
      },
      ],
      local.read_registered_statements,
      local.read_imported_statements,
      local.deny_readonly_source_statements,
    local.deny_imported_mutation_statements)
  })

  # ---------------------------------------------------------------------------
  # 2. 注册角色：只把已经校验过的 release 登记成 PAI Dataset Version。
  #    明确不给裸 OSS 读取——它不需要看见数据本身，只需要写元数据。
  # ---------------------------------------------------------------------------
  register_policy = jsonencode({
    Version = "1"
    Statement = concat([
      {
        # Resource 只能是 "*"，见文件顶部说明。
        Sid      = "RegisterDatasetVersion"
        Effect   = "Allow"
        Action   = concat(local.pai_dataset_register_actions, local.pai_dataset_read_actions)
        Resource = ["*"]
      },
      {
        Sid      = "ReadWorkspaceContext"
        Effect   = "Allow"
        Action   = local.pai_workspace_read_actions
        Resource = ["*"]
      },
      {
        Sid    = "DenyRawDataAccess"
        Effect = "Deny"
        Action = [
          "oss:GetObject",
          "oss:PutObject",
          "oss:DeleteObject",
        ]
        Resource = [
          local.lakefs_bucket_arn,
          local.lakefs_object_arn,
        ]
      },
      {
        Sid      = "DenyTrainingSubmission"
        Effect   = "Deny"
        Action   = local.pai_job_submit_actions
        Resource = ["*"]
      },
      ],
      local.deny_readonly_source_statements,
    local.deny_imported_mutation_statements)
  })

  # ---------------------------------------------------------------------------
  # 3. 作业提交角色：提交绑定已审批 Dataset Version 的 DLC Job。
  #    明确不给改写数据版本的权限——能训练的不能改训练集。
  # ---------------------------------------------------------------------------
  dlc_submit_policy = jsonencode({
    Version = "1"
    Statement = concat([
      {
        Sid      = "SubmitAndObserveTrainingJobs"
        Effect   = "Allow"
        Action   = local.pai_job_operate_actions
        Resource = ["*"]
      },
      {
        # 提交作业时需要按 Commit 找到对应的 Dataset Version，只需读。
        Sid      = "ResolveDatasetVersion"
        Effect   = "Allow"
        Action   = local.pai_dataset_read_actions
        Resource = ["*"]
      },
      {
        Sid      = "DenyDatasetMutation"
        Effect   = "Deny"
        Action   = local.pai_dataset_mutation_actions
        Resource = ["*"]
      },
      {
        Sid    = "DenyLakeFSBackendAccess"
        Effect = "Deny"
        Action = ["oss:GetObject", "oss:ListObjects"]
        Resource = [
          local.lakefs_bucket_arn,
          local.lakefs_object_arn,
        ]
      },
      ],
      local.deny_readonly_source_statements,
    local.deny_imported_mutation_statements)
  })

  # ---------------------------------------------------------------------------
  # 3b. DSW 提交角色：CI 只按受控 Profile 创建/查看/停止实例。
  #     CreateInstance 的 UserId 由受评审的用户映射注入，实例保持 PRIVATE。
  # ---------------------------------------------------------------------------
  dsw_submit_policy = jsonencode({
    Version = "1"
    Statement = concat([
      {
        Sid      = "CreateAndObserveDswInstances"
        Effect   = "Allow"
        Action   = local.pai_dsw_operate_actions
        Resource = ["*"]
      },
      {
        Sid      = "ResolveDatasetVersion"
        Effect   = "Allow"
        Action   = local.pai_dataset_read_actions
        Resource = ["*"]
      },
      {
        Sid      = "DenyDatasetAndDlcMutation"
        Effect   = "Deny"
        Action   = concat(local.pai_dataset_mutation_actions, local.pai_job_submit_actions)
        Resource = ["*"]
      },
      {
        Sid    = "DenyLakeFSBackendAccess"
        Effect = "Deny"
        Action = ["oss:GetObject", "oss:ListObjects"]
        Resource = [
          local.lakefs_bucket_arn,
          local.lakefs_object_arn,
        ]
      },
      ],
      local.deny_readonly_source_statements,
    local.deny_imported_mutation_statements)
  })

  # ---------------------------------------------------------------------------
  # 4. 挂载审计角色：定时检查 DLC/DSW 是否绕过不可变 release。
  #    这是纯检测身份，不复用能 CreateJob 的 dlc-submit 角色。
  # ---------------------------------------------------------------------------
  pai_mount_audit_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        Sid    = "ReadPaiMountConfiguration"
        Effect = "Allow"
        Action = concat(
          local.pai_job_read_actions,
          local.pai_dsw_audit_actions,
          local.pai_dataset_read_actions,
          local.pai_workspace_read_actions,
        )
        Resource = ["*"]
      },
      {
        Sid      = "DenyDatasetAndJobMutation"
        Effect   = "Deny"
        Action   = concat(local.pai_dataset_mutation_actions, local.pai_job_submit_actions)
        Resource = ["*"]
      },
      {
        Sid    = "DenyDataPlaneAccess"
        Effect = "Deny"
        Action = [
          "oss:GetObject",
          "oss:PutObject",
          "oss:DeleteObject",
          "oss:ListObjects",
          "nas:CreateDataFlowTask",
        ]
        Resource = ["*"]
      },
    ]
  })

  # ---------------------------------------------------------------------------
  # 5. 训练运行角色：容器内实际使用的身份。
  #    只读已发布归档 + 写自己的输出目录；绝不给 landing/lakeFS 后端。
  # ---------------------------------------------------------------------------
  training_runtime_policy = jsonencode({
    Version = "1"
    Statement = concat([
      {
        Sid    = "ReadPublishedReleaseArchive"
        Effect = "Allow"
        Action = [
          "oss:GetObject",
          "oss:GetObjectMeta",
          "oss:ListObjects",
        ]
        Resource = [local.dataset_bucket_arn, local.release_object_arn]
      },
      {
        Sid    = "WriteTrainingOutput"
        Effect = "Allow"
        Action = [
          "oss:PutObject",
          "oss:GetObject",
          "oss:DeleteObject",
          "oss:ListObjects",
          "oss:AbortMultipartUpload",
        ]
        Resource = [local.dataset_bucket_arn, local.output_object_arn]
      },
      {
        Sid    = "MountPublishedDataset"
        Effect = "Allow"
        Action = [
          "nas:DescribeFileSystems",
          "nas:DescribeMountTargets",
        ]
        Resource = ["*"]
      },
      {
        # 训练任务读到 lakeFS 后端就等于绕过了整个发布协议。
        Sid    = "DenyLakeFSBackendAndStaging"
        Effect = "Deny"
        Action = ["oss:GetObject", "oss:PutObject", "oss:ListObjects", "oss:DeleteObject"]
        Resource = [
          local.lakefs_bucket_arn,
          local.lakefs_object_arn,
          local.staging_object_arn,
        ]
      },
      {
        Sid      = "DenyDatasetAndJobMutation"
        Effect   = "Deny"
        Action   = concat(local.pai_dataset_mutation_actions, local.pai_job_submit_actions)
        Resource = ["*"]
      },
      ],
      local.deny_readonly_source_statements,
    local.deny_imported_mutation_statements)
  })

  # ---------------------------------------------------------------------------
  # 5. 研发用户组：在 PAI 里使用已发布版本，不碰原始数据、不碰生产 release。
  # ---------------------------------------------------------------------------
  developer_policy = jsonencode({
    Version = "1"
    Statement = concat([
      {
        Sid    = "BrowsePaiWorkspaceAndDatasets"
        Effect = "Allow"
        Action = concat(
          local.pai_workspace_browse_actions,
          local.pai_dataset_read_actions,
          local.pai_job_read_actions,
        )
        Resource = ["*"]
      },
      {
        Sid    = "DenyDataPlaneAccess"
        Effect = "Deny"
        Action = ["oss:GetObject", "oss:PutObject", "oss:DeleteObject", "oss:ListObjects"]
        Resource = [
          local.lakefs_bucket_arn,
          local.lakefs_object_arn,
          local.staging_object_arn,
        ]
      },
      {
        # 长期密钥是这套设计的最大威胁：一旦有人给自己建 AK，
        # 所有基于 STS 短期凭证的审计和轮转都失效。
        Sid    = "DenyLongLivedCredentials"
        Effect = "Deny"
        Action = [
          "ram:CreateAccessKey",
          "ram:UpdateAccessKey",
          "ram:ListAccessKeys",
          "ram:CreateUser",
          "ram:AttachPolicyToUser",
          "ram:CreatePolicy",
          "ram:CreateRole",
          "ram:UpdateRole",
        ]
        Resource = ["*"]
      },
      {
        # 这条是「用户拿 GUI 绕过流水线」的主要强制点，不只是防误删。
        #
        # 研发不能建 Dataset，也不能建 Version，意味着**他在 DSW/DLC 控制台的
        # 数据集下拉框里只能看到 register 角色发布过的东西**——而那些全都是
        # 以 Commit ID 命名的不可变 release。于是「GUI 挂载」这条路本身是安全的：
        # 菜单里没有可变引用可选。
        #
        # 拦不住的是「在 DSW 里直接填一个 OSS 路径挂载」，见 docs/permissions.md
        # 的 §10——那条只能靠检测和产出侧把关，RAM 表达不了。
        Sid      = "DenyDatasetMutation"
        Effect   = "Deny"
        Action   = local.pai_dataset_mutation_actions
        Resource = ["*"]
      },
      {
        # 研发组不提交训练作业——作业由流水线用 dlc-submit 角色提交，
        # 这样「提交了什么」有记录、有审批。
        #
        # 必须**显式** Deny 而不是靠「没 Allow」：隐式拒绝只在本组策略是他
        # 唯一策略时成立，而 §5 明确说了我们不动用户已有的策略。谁还挂着
        # AliyunPAIFullAccess，并集里 CreateJob 就是放行的。
        Sid      = "DenyTrainingJobSubmission"
        Effect   = "Deny"
        Action   = local.pai_job_submit_actions
        Resource = ["*"]
      },
      {
        # DSW 也必须走 Profile 流水线，否则持有旧的 AliyunPAIFullAccess 的用户
        # 可以在控制台重新打开公网/SSH、换任意镜像或挂裸存储 URI。
        Sid      = "DenyDswInstanceMutation"
        Effect   = "Deny"
        Action   = local.pai_dsw_mutation_actions
        Resource = ["*"]
      },
      ],
      local.workspace_rw_statements,
      local.deny_readonly_source_statements,
    local.deny_imported_mutation_statements)
  })

  # ---------------------------------------------------------------------------
  # 生产护栏：删除类操作一律 Deny。
  #
  # 显式 Deny 优先于任何 Allow，所以这条附加到哪个角色，那个角色就无法删除。
  # 代价是「必须删除后重建」的变更也会失败——这正是我们想要的：
  # 这类变更应该被迫走 BreakGlass 审批，而不是被流水线静默执行。
  # ---------------------------------------------------------------------------
  deny_destructive_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        Sid    = "DenyDestructiveOperations"
        Effect = "Deny"
        Action = [
          "nas:DeleteFileSystem",
          "nas:DeleteFileset",
          "nas:DeleteMountTarget",
          "oss:DeleteBucket",
          "pai:DeleteDataset",
          "pai:DeleteDatasetVersion",
          "pai:DeleteWorkspace",
          "pai:DeleteMembers",
          "paidataset:DeleteDataset",
          "paidataset:DeleteDatasetVersion",
          "paiworkspace:DeleteWorkspace",
          "paiworkspace:DeleteMembers",
          "pai:DeleteInstance",
          "paidsw:DeleteInstance",
        ]
        Resource = ["*"]
      },
    ]
  })

  # 导出给 CLI 做本地校验。和上面的 RAM 策略同源于 var.data_sources，
  # 所以「CLI 说没注册」和「RAM 拒绝访问」永远是同一个判断，不会漂移。
  data_sources_document = jsonencode({
    data_sources = [
      for s in var.data_sources : {
        name   = s.name
        bucket = s.bucket
        prefix = s.prefix
        mode   = s.mode
      }
    ]
  })

  # 供 outputs 与 render 脚本统一消费。key 即最终的 RAM 策略名。
  policy_documents = merge(
    {
      "${local.name_prefix}-materializer"     = local.materializer_policy
      "${local.name_prefix}-register"         = local.register_policy
      "${local.name_prefix}-dlc-submit"       = local.dlc_submit_policy
      "${local.name_prefix}-dsw-submit"       = local.dsw_submit_policy
      "${local.name_prefix}-pai-mount-audit"  = local.pai_mount_audit_policy
      "${local.name_prefix}-training-runtime" = local.training_runtime_policy
    },
    var.developer_group_name == "" ? {} : {
      "${local.name_prefix}-developer" = local.developer_policy
    },
    var.deny_destructive ? {
      "${local.name_prefix}-deny-destructive" = local.deny_destructive_policy
    } : {},
  )
}
