data "alicloud_account" "current" {}

# GitHub Actions 的信任锚。
#
# 这是整条流水线唯一的身份来源：有了它，GitHub 的 OIDC token 才能换成
# 阿里云 STS 临时凭证，从此不需要在 CI 里保存任何长期 AccessKey。
#
# 反过来说，谁能改这个资源就能重定向整条信任链，所以它只存在于
# bootstrap 层，且 TerraformAccessApplyRole 被显式禁止修改它。
resource "alicloud_ims_oidc_provider" "github" {
  oidc_provider_name = var.oidc_provider_name
  issuer_url         = "https://token.actions.githubusercontent.com"
  fingerprints       = var.oidc_thumbprints
  client_ids         = [var.oidc_audience]
  description        = "GitHub Actions OIDC，仓库 ${var.github_repo}；由 infra/bootstrap 管理"
}

locals {
  account_id = data.alicloud_account.current.id

  state_bucket_arn = "acs:oss:*:${local.account_id}:${var.state_bucket}"
  state_object_arn = "acs:oss:*:${local.account_id}:${var.state_bucket}/*"
  lock_table_arn   = "acs:ots:*:${local.account_id}:instance/${var.lock_instance_name}/table/${var.lock_table_name}"

  ci_role_names = [
    "TerraformPlanRole",
    "TerraformPlatformApplyRole",
    "TerraformAccessApplyRole",
  ]

  # 自身 ARN 集合：用于「禁止修改自己」的 Deny 语句。
  ci_role_arns   = [for n in local.ci_role_names : "acs:ram:*:${local.account_id}:role/${n}"]
  ci_policy_arns = [for n in local.ci_role_names : "acs:ram:*:${local.account_id}:policy/${n}Policy"]

  # state 读 + 锁。plan 也会加锁，所以 plan 角色同样需要锁表的行读写权限。
  state_read_statements = [
    {
      Sid      = "ReadState"
      Effect   = "Allow"
      Action   = ["oss:GetObject", "oss:GetObjectMeta", "oss:ListObjects", "oss:GetBucketInfo"]
      Resource = [local.state_bucket_arn, local.state_object_arn]
    },
    {
      Sid      = "AcquireAndReleaseStateLock"
      Effect   = "Allow"
      Action   = ["ots:GetRow", "ots:PutRow", "ots:DeleteRow", "ots:DescribeTable", "ots:ListTable"]
      Resource = [local.lock_table_arn]
    },
  ]

  state_write_statement = {
    Sid      = "WriteState"
    Effect   = "Allow"
    Action   = ["oss:PutObject", "oss:DeleteObject"]
    Resource = [local.state_object_arn]
  }

  # 只读观测面：plan 需要能 refresh 全部被管资源。
  read_only_statement = {
    Sid    = "ReadOnlyObservation"
    Effect = "Allow"
    Action = [
      "oss:ListBuckets",
      "oss:GetBucketInfo",
      "oss:GetBucketAcl",
      "oss:GetBucketVersioning",
      "oss:GetBucketLifecycle",
      "nas:DescribeFileSystems",
      "nas:DescribeMountTargets",
      "nas:DescribeFilesets",
      "nas:DescribeProtocolService",
      "ram:GetRole",
      "ram:ListRoles",
      "ram:GetPolicy",
      "ram:ListPolicies",
      "ram:ListPoliciesForRole",
      "ram:ListPoliciesForGroup",
      "ram:GetGroup",
      "ram:ListGroups",
      "ram:ListUsers",
      "paiworkspace:GetWorkspace",
      "paiworkspace:ListWorkspaces",
      "paiworkspace:ListMembers",
      "paiworkspace:GetMember",
      "paidataset:GetDataset",
      "paidataset:ListDatasets",
      "paidataset:ListDatasetVersions",
      "vpc:DescribeVpcs",
      "vpc:DescribeVSwitches",
    ]
    Resource = ["*"]
  }

  # -------------------------------------------------------------------------
  # TerraformPlanRole：只读 + 读 state + 加锁。
  # 它跑在 PR 上，触发者是任何能开 PR 的人，所以必须完全无写权限。
  # -------------------------------------------------------------------------
  plan_policy = jsonencode({
    Version = "1"
    Statement = concat(
      local.state_read_statements,
      [
        local.read_only_statement,
        {
          # 兜底：即使上面的 Allow 写宽了，这条 Deny 也保证 plan 角色改不动任何东西。
          Sid    = "DenyAllMutations"
          Effect = "Deny"
          Action = [
            "ram:Create*",
            "ram:Update*",
            "ram:Delete*",
            "ram:Attach*",
            "ram:Detach*",
            "ram:Add*",
            "ram:Remove*",
            "ims:Create*",
            "ims:Update*",
            "ims:Delete*",
            "oss:PutBucket*",
            "oss:DeleteBucket*",
            "nas:Create*",
            "nas:Modify*",
            "nas:Delete*",
            "ots:Create*",
            "ots:Delete*",
            "ots:Update*",
            "paidataset:Create*",
            "paidataset:Update*",
            "paidataset:Delete*",
            "paiworkspace:Create*",
            "paiworkspace:Update*",
            "paiworkspace:Delete*",
            "paiworkspace:AddMemberRole",
            "paiworkspace:RemoveMemberRole",
            "paidlc:CreateJob",
          ]
          Resource = ["*"]
        },
        {
          # state 桶之外不许写任何对象。
          Sid      = "DenyObjectWritesOutsideState"
          Effect   = "Deny"
          Action   = ["oss:PutObject", "oss:DeleteObject"]
          Resource = ["*"]
        },
      ],
    )
  })

  # -------------------------------------------------------------------------
  # TerraformPlatformApplyRole：管基础设施，**不管权限**。
  #
  # 关键隔离：Deny 掉整个 ram/ims 写面。基础设施变更再频繁，也不该顺手
  # 带上一次提权；权限变更必须走另一条流水线和另一批审批人。
  # -------------------------------------------------------------------------
  platform_apply_policy = jsonencode({
    Version = "1"
    Statement = concat(
      local.state_read_statements,
      [
        local.state_write_statement,
        local.read_only_statement,
        {
          Sid    = "ManagePlatformStorage"
          Effect = "Allow"
          Action = [
            "oss:PutBucket",
            "oss:PutBucketAcl",
            "oss:PutBucketVersioning",
            "oss:PutBucketLifecycle",
            "oss:PutBucketEncryption",
            "oss:PutBucketTagging",
            "oss:GetBucketTagging",
          ]
          Resource = ["*"]
        },
        {
          Sid    = "DenyAllIdentityAndPermissionWrites"
          Effect = "Deny"
          Action = [
            "ram:Create*",
            "ram:Update*",
            "ram:Delete*",
            "ram:Attach*",
            "ram:Detach*",
            "ram:Add*",
            "ram:Remove*",
            "ims:*",
            "paiworkspace:CreateMember",
            "paiworkspace:DeleteMembers",
            "paiworkspace:AddMemberRole",
            "paiworkspace:RemoveMemberRole",
          ]
          Resource = ["*"]
        },
        {
          # CPFS 与 PAI Dataset 是 data source 只引用，不该被这个角色改动。
          Sid    = "DenyStatefulStorageMutation"
          Effect = "Deny"
          Action = [
            "nas:CreateFileSystem",
            "nas:DeleteFileSystem",
            "nas:DeleteFileset",
            "nas:DeleteMountTarget",
            "oss:DeleteBucket",
            "paidataset:CreateDataset",
            "paidataset:DeleteDataset",
            "paidataset:DeleteDatasetVersion",
          ]
          Resource = ["*"]
        },
      ],
    )
  })

  # -------------------------------------------------------------------------
  # TerraformAccessApplyRole：管权限，**不能管自己**。
  #
  # 如果这个角色能改自己的信任策略或自己的权限策略，它就具备无限提权能力：
  # 一次 apply 就能把自己变成 AdministratorAccess。所以下面用两条 Deny
  # 把三个 Terraform CI 角色及其策略排除在可管理范围之外。
  # 它们的定义只存在于 bootstrap 层，而 bootstrap 只有管理员能跑。
  # -------------------------------------------------------------------------
  access_apply_policy = jsonencode({
    Version = "1"
    Statement = concat(
      local.state_read_statements,
      [
        local.state_write_statement,
        local.read_only_statement,
        {
          Sid    = "ManageDataPlanePermissions"
          Effect = "Allow"
          Action = [
            "ram:CreatePolicy",
            "ram:CreatePolicyVersion",
            "ram:DeletePolicy",
            "ram:DeletePolicyVersion",
            "ram:SetDefaultPolicyVersion",
            "ram:CreateRole",
            "ram:UpdateRole",
            "ram:DeleteRole",
            "ram:AttachPolicyToRole",
            "ram:DetachPolicyFromRole",
            "ram:CreateGroup",
            "ram:UpdateGroup",
            "ram:DeleteGroup",
            "ram:AttachPolicyToGroup",
            "ram:DetachPolicyFromGroup",
            "ram:AddUserToGroup",
            "ram:RemoveUserFromGroup",
          ]
          Resource = ["*"]
        },
        {
          Sid    = "ManagePaiWorkspaceMembership"
          Effect = "Allow"
          Action = [
            "paiworkspace:CreateMember",
            "paiworkspace:DeleteMembers",
            "paiworkspace:AddMemberRole",
            "paiworkspace:RemoveMemberRole",
          ]
          Resource = ["*"]
        },
        {
          Sid      = "DenySelfModificationOfCiRoles"
          Effect   = "Deny"
          Action   = ["ram:UpdateRole", "ram:DeleteRole", "ram:AttachPolicyToRole", "ram:DetachPolicyFromRole"]
          Resource = local.ci_role_arns
        },
        {
          Sid      = "DenySelfModificationOfCiPolicies"
          Effect   = "Deny"
          Action   = ["ram:CreatePolicyVersion", "ram:DeletePolicy", "ram:DeletePolicyVersion", "ram:SetDefaultPolicyVersion"]
          Resource = local.ci_policy_arns
        },
        {
          # 信任锚只归 bootstrap。改掉它等于把整条流水线的身份来源换掉。
          # ims 与 ram 两个前缀都拒绝：OIDC 相关 API 在阿里云历史上归属过
          # 不同产品命名空间，宁可多拒。
          Sid      = "DenyTrustAnchorAndLongLivedCredentials"
          Effect   = "Deny"
          Action   = ["ims:*", "ram:CreateUser", "ram:DeleteUser", "ram:CreateAccessKey", "ram:UpdateAccessKey", "ram:AttachPolicyToUser", "ram:DetachPolicyFromUser"]
          Resource = ["*"]
        },
        {
          # 权限流水线不该顺手改基础设施。
          Sid    = "DenyInfrastructureMutation"
          Effect = "Deny"
          Action = [
            "oss:PutBucket*",
            "oss:DeleteBucket*",
            "nas:Create*",
            "nas:Modify*",
            "nas:Delete*",
            "ots:Create*",
            "ots:Delete*",
            "paidlc:CreateJob",
            "paidataset:CreateDatasetVersion",
            "paidataset:DeleteDataset",
          ]
          Resource = ["*"]
        },
      ],
    )
  })
}

module "plan_role" {
  source = "../modules/ci-oidc-role"

  role_name   = "TerraformPlanRole"
  description = "Terraform plan（PR 触发）：只读 + 读 state + 加锁，无任何写权限"

  oidc_provider_arn = alicloud_ims_oidc_provider.github.arn
  audience          = var.oidc_audience

  # PR 触发，包括 fork 的 PR，因此这个 sub 只能配只读权限。
  subjects = ["repo:${var.github_repo}:pull_request"]

  policy_documents = {
    TerraformPlanRolePolicy = local.plan_policy
  }
}

module "platform_apply_role" {
  source = "../modules/ci-oidc-role"

  role_name   = "TerraformPlatformApplyRole"
  description = "Terraform apply（基础设施层）：管存储与平台资源，禁止改 RAM 与 PAI 成员"

  oidc_provider_arn = alicloud_ims_oidc_provider.github.arn
  audience          = var.oidc_audience
  subjects          = ["repo:${var.github_repo}:environment:${var.github_environment}"]

  policy_documents = {
    TerraformPlatformApplyRolePolicy = local.platform_apply_policy
  }
}

module "access_apply_role" {
  source = "../modules/ci-oidc-role"

  role_name   = "TerraformAccessApplyRole"
  description = "Terraform apply（权限层）：管 RAM 与 PAI 成员，禁止修改自身及信任锚"

  oidc_provider_arn = alicloud_ims_oidc_provider.github.arn
  audience          = var.oidc_audience
  subjects          = ["repo:${var.github_repo}:environment:${var.github_environment}"]

  policy_documents = {
    TerraformAccessApplyRolePolicy = local.access_apply_policy
  }
}
