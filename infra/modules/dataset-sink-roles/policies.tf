# 五个身份的最小权限策略。
#
# 这里用 jsonencode 而不是 data "alicloud_ram_policy_document"，原因有两个：
#   1. 渲染结果就是 deploy/ram/*.json 需要的内容，scripts/render-ram-policies.sh
#      直接读 output 即可，不需要二次转换，也就不会出现两份不一致的策略。
#   2. 不依赖 Provider 的 data source schema，换 Provider 版本不会破。
#
# 关键约束（2026-08-02 通过读取官方系统策略 AliyunPAIFullAccess 核实）：
# paidataset:*、paidlc:*、paiworkspace:* 在阿里云官方定义中全部是 Resource: "*"，
# **无法用 RAM Policy 限定到某个 Dataset / Job / Workspace**。所以这三类权限的
# 收敛只能靠：收窄 Action 列表 + PAI Workspace 成员角色 + 流水线环境审批。
# 不要在评审时误以为 Resource: "*" 是偷懒，它是平台的硬限制。

locals {
  name_prefix = "${var.project}-${var.environment}"

  # OSS 资源 ARN：桶本身和桶内对象要分别声明，只写其中一个会导致
  # ListObjects 或 GetObject 之一失败。
  lakefs_bucket_arn = "acs:oss:*:${var.account_id}:${var.lakefs_backend_bucket}"
  lakefs_object_arn = var.lakefs_backend_prefix == "" ? "acs:oss:*:${var.account_id}:${var.lakefs_backend_bucket}/*" : "acs:oss:*:${var.account_id}:${var.lakefs_backend_bucket}/${var.lakefs_backend_prefix}/*"

  dataset_bucket_arn = "acs:oss:*:${var.account_id}:${var.dataset_bucket}"
  staging_object_arn = "acs:oss:*:${var.account_id}:${var.dataset_bucket}/${var.dataset_staging_prefix}/*"
  release_object_arn = "acs:oss:*:${var.account_id}:${var.dataset_bucket}/${var.dataset_release_prefix}/*"
  output_object_arn  = "acs:oss:*:${var.account_id}:${var.dataset_bucket}/${var.dataset_output_prefix}/*"

  # ---------------------------------------------------------------------------
  # 1. 沉降角色：读 lakeFS 后端固定 Commit，写 CPFS。
  #    明确不给 PAI 注册和训练提交权限——能写数据的不能宣布数据可用。
  # ---------------------------------------------------------------------------
  materializer_policy = jsonencode({
    Version = "1"
    Statement = [
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
        Sid    = "DenyDatasetRegistrationAndTraining"
        Effect = "Deny"
        Action = [
          "paidataset:CreateDatasetVersion",
          "paidataset:DeleteDatasetVersion",
          "paidlc:CreateJob",
          "paidlc:StopJob",
        ]
        Resource = ["*"]
      },
    ]
  })

  # ---------------------------------------------------------------------------
  # 2. 注册角色：只把已经校验过的 release 登记成 PAI Dataset Version。
  #    明确不给裸 OSS 读取——它不需要看见数据本身，只需要写元数据。
  # ---------------------------------------------------------------------------
  register_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        # Resource 只能是 "*"，见文件顶部说明。
        Sid    = "RegisterDatasetVersion"
        Effect = "Allow"
        Action = [
          "paidataset:CreateDatasetVersion",
          "paidataset:ListDatasetVersions",
          "paidataset:GetDatasetVersion",
          "paidataset:GetDataset",
          "paidataset:ListDatasets",
        ]
        Resource = ["*"]
      },
      {
        Sid    = "ReadWorkspaceContext"
        Effect = "Allow"
        Action = [
          "paiworkspace:GetWorkspace",
          "paiworkspace:ListWorkspaces",
        ]
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
        Action   = ["paidlc:CreateJob"]
        Resource = ["*"]
      },
    ]
  })

  # ---------------------------------------------------------------------------
  # 3. 作业提交角色：提交绑定已审批 Dataset Version 的 DLC Job。
  #    明确不给改写数据版本的权限——能训练的不能改训练集。
  # ---------------------------------------------------------------------------
  dlc_submit_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        Sid    = "SubmitAndObserveTrainingJobs"
        Effect = "Allow"
        Action = [
          "paidlc:CreateJob",
          "paidlc:GetJob",
          "paidlc:ListJobs",
          "paidlc:StopJob",
          "paidlc:GetPodLogs",
        ]
        Resource = ["*"]
      },
      {
        # 提交作业时需要按 Commit 找到对应的 Dataset Version，只需读。
        Sid    = "ResolveDatasetVersion"
        Effect = "Allow"
        Action = [
          "paidataset:GetDataset",
          "paidataset:ListDatasetVersions",
          "paidataset:GetDatasetVersion",
        ]
        Resource = ["*"]
      },
      {
        Sid    = "DenyDatasetMutation"
        Effect = "Deny"
        Action = [
          "paidataset:CreateDatasetVersion",
          "paidataset:UpdateDatasetVersion",
          "paidataset:DeleteDatasetVersion",
          "paidataset:DeleteDataset",
        ]
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
    ]
  })

  # ---------------------------------------------------------------------------
  # 4. 训练运行角色：容器内实际使用的身份。
  #    只读已发布归档 + 写自己的输出目录；绝不给 landing/lakeFS 后端。
  # ---------------------------------------------------------------------------
  training_runtime_policy = jsonencode({
    Version = "1"
    Statement = [
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
        Sid    = "DenyDatasetAndJobMutation"
        Effect = "Deny"
        Action = [
          "paidataset:CreateDatasetVersion",
          "paidataset:DeleteDatasetVersion",
          "paidlc:CreateJob",
        ]
        Resource = ["*"]
      },
    ]
  })

  # ---------------------------------------------------------------------------
  # 5. 研发用户组：在 PAI 里使用已发布版本，不碰原始数据、不碰生产 release。
  # ---------------------------------------------------------------------------
  developer_policy = jsonencode({
    Version = "1"
    Statement = [
      {
        Sid    = "BrowsePaiWorkspaceAndDatasets"
        Effect = "Allow"
        Action = [
          "paiworkspace:GetWorkspace",
          "paiworkspace:ListWorkspaces",
          "paiworkspace:ListMembers",
          "paidataset:GetDataset",
          "paidataset:ListDatasets",
          "paidataset:ListDatasetVersions",
          "paidataset:GetDatasetVersion",
          "paidlc:GetJob",
          "paidlc:ListJobs",
          "paidlc:GetPodLogs",
        ]
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
        Sid    = "DenyDatasetMutation"
        Effect = "Deny"
        Action = [
          "paidataset:CreateDatasetVersion",
          "paidataset:UpdateDatasetVersion",
          "paidataset:DeleteDatasetVersion",
          "paidataset:DeleteDataset",
        ]
        Resource = ["*"]
      },
    ]
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
          "paidataset:DeleteDataset",
          "paidataset:DeleteDatasetVersion",
          "paiworkspace:DeleteWorkspace",
          "paiworkspace:DeleteMembers",
        ]
        Resource = ["*"]
      },
    ]
  })

  # 供 outputs 与 render 脚本统一消费。key 即最终的 RAM 策略名。
  policy_documents = merge(
    {
      "${local.name_prefix}-materializer"     = local.materializer_policy
      "${local.name_prefix}-register"         = local.register_policy
      "${local.name_prefix}-dlc-submit"       = local.dlc_submit_policy
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
