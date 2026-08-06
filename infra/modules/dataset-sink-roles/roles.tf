# 策略先集中创建一次，再按角色附加。
#
# 不让每个角色模块各自创建策略，是因为 deny-destructive 这类护栏要挂到多个角色，
# 各建一份就会撞 RAM 策略名（策略名在账号内唯一）。
resource "alicloud_ram_policy" "this" {
  for_each = local.policy_documents

  policy_name     = each.key
  policy_document = each.value
  description     = "${each.key}｜由 Terraform 管理，请勿在控制台手改；改动走 infra/envs/*/access 的 PR"
  force           = true
}

locals {
  # 生产护栏策略名（未启用时为空列表）。
  guardrail_policy_names = var.deny_destructive ? ["${local.name_prefix}-deny-destructive"] : []

  # OIDC sub 白名单。
  #
  # 沉降与注册是写操作，必须绑定到 Environment（这样 GitHub 的人工审批才在
  # 信任链上生效）。作业提交同理。只读的 plan 类身份不在这个模块里。
  #
  # 注意：这里刻意不包含 `repo:<org>/<repo>:pull_request`——fork 的 PR 也会
  # 产生该 sub，等于把写权限交给任何能开 PR 的人。
  write_subjects = ["repo:${var.github_oidc_repo}:environment:${var.github_environment}"]
  runtime_subjects = distinct(concat(local.write_subjects, [
    "repo:${var.github_oidc_repo}:environment:${var.runtime_github_environment}",
  ]))
  audit_subjects = ["repo:${var.github_oidc_repo}:ref:refs/heads/main"]
}

module "materializer_role" {
  source = "../ci-oidc-role"

  role_name   = "${local.name_prefix}-materializer"
  description = "沉降角色：读 lakeFS 后端固定 Commit 并写 CPFS release。不得注册 PAI 版本或提交训练。"

  oidc_provider_arn    = var.oidc_provider_arn
  audience             = var.oidc_audience
  subjects             = local.write_subjects
  max_session_duration = var.max_session_duration

  attach_custom_policy_names = concat(
    ["${local.name_prefix}-materializer"],
    local.guardrail_policy_names,
  )

  depends_on = [alicloud_ram_policy.this]
}

module "register_role" {
  source = "../ci-oidc-role"

  role_name   = "${local.name_prefix}-register"
  description = "注册角色：把已校验的 release 登记为 PAI Dataset Version。不得读裸 OSS 或提交训练。"

  oidc_provider_arn    = var.oidc_provider_arn
  audience             = var.oidc_audience
  subjects             = local.write_subjects
  max_session_duration = var.max_session_duration

  attach_custom_policy_names = concat(
    ["${local.name_prefix}-register"],
    local.guardrail_policy_names,
  )

  depends_on = [alicloud_ram_policy.this]
}

module "dlc_submit_role" {
  source = "../ci-oidc-role"

  role_name   = "${local.name_prefix}-dlc-submit"
  description = "作业提交角色：提交绑定已审批 Dataset Version 的 DLC Job。不得改写数据版本。"

  oidc_provider_arn    = var.oidc_provider_arn
  audience             = var.oidc_audience
  subjects             = local.runtime_subjects
  max_session_duration = var.max_session_duration

  attach_custom_policy_names = concat(
    ["${local.name_prefix}-dlc-submit"],
    local.guardrail_policy_names,
  )

  depends_on = [alicloud_ram_policy.this]
}

module "dsw_submit_role" {
  source = "../ci-oidc-role"

  role_name   = "${local.name_prefix}-dsw-submit"
  description = "DSW 提交角色：按受控 Profile 为指定 RAM 用户创建私有 DSW。不得改写数据版本或提交 DLC。"

  oidc_provider_arn = var.oidc_provider_arn
  audience          = var.oidc_audience
  subjects = [
    "repo:${var.github_oidc_repo}:environment:${var.runtime_github_environment}",
  ]
  max_session_duration = var.max_session_duration

  attach_custom_policy_names = concat(
    ["${local.name_prefix}-dsw-submit"],
    local.guardrail_policy_names,
  )

  depends_on = [alicloud_ram_policy.this]
}

module "pai_mount_audit_role" {
  source = "../ci-oidc-role"

  role_name   = "${local.name_prefix}-pai-mount-audit"
  description = "只读审计角色：定时检查 DLC/DSW 挂载是否来自不可变 Dataset Version。"

  oidc_provider_arn    = var.oidc_provider_arn
  audience             = var.oidc_audience
  subjects             = local.audit_subjects
  max_session_duration = var.max_session_duration

  attach_custom_policy_names = concat(
    ["${local.name_prefix}-pai-mount-audit"],
    local.guardrail_policy_names,
  )

  depends_on = [alicloud_ram_policy.this]
}

# ---------------------------------------------------------------------------
# 训练运行角色：由 PAI 服务扮演，不是 CI 身份，所以不走 OIDC 模块。
#
# 这个分离很重要：如果训练运行角色也能被 CI 假设，那么流水线就获得了读取
# 训练数据的能力，「运行身份」和「交付身份」的隔离就没了。
# ---------------------------------------------------------------------------
locals {
  training_runtime_trust = jsonencode({
    Version = "1"
    Statement = [
      {
        Effect = "Allow"
        Action = "sts:AssumeRole"
        Principal = {
          Service = var.training_runtime_service_principals
        }
      },
    ]
  })
}

resource "alicloud_ram_role" "training_runtime" {
  role_name                   = "${local.name_prefix}-training-runtime"
  assume_role_policy_document = local.training_runtime_trust
  description                 = "训练运行角色：容器内身份，只读已发布归档、写自己的输出目录。不得访问 lakeFS 后端或 staging。"

  # 数据面角色不设 force=true：销毁前必须先手工确认没有作业在用它。
  force = false
}

resource "alicloud_ram_role_policy_attachment" "training_runtime" {
  for_each = toset(concat(
    ["${local.name_prefix}-training-runtime"],
    local.guardrail_policy_names,
  ))

  policy_name = each.value
  policy_type = "Custom"
  role_name   = alicloud_ram_role.training_runtime.role_name

  depends_on = [alicloud_ram_policy.this]
}

# ---------------------------------------------------------------------------
# 研发用户组：人员加组即获得只读消费权限，离场从组里移除即可。
#
# 用组而不是直接给用户附策略，是为了让「谁有权限」这个问题有唯一答案：
# 看组成员，而不是逐个用户翻策略。
# ---------------------------------------------------------------------------
resource "alicloud_ram_group" "developers" {
  count = var.developer_group_name == "" ? 0 : 1

  # Provider 1.245.0 起 name 已废弃，改用 group_name。
  group_name = var.developer_group_name
  comments   = "${var.environment} 环境算法研发：可使用已发布数据集版本，不可取长期密钥或改写发布物"
  force      = true
}

# 组成员：已有的 RAM 用户通过这里接入本项目。
#
# Terraform **不接管用户本身**，也不碰他们已有的其他策略，只决定他属不属于
# 这个组。用户离场时从列表里删掉即可，不影响他的其他权限。
#
# 用 alicloud_ram_user_group_attachment 而不是 alicloud_ram_group_membership：
# 后者虽然能整体管理成员集合（更权威），但自 Provider 1.267.0 起已废弃、
# 将来会被移除，不值得用一个注定要迁移的资源换这点便利。
#
# 代价是逐个绑定不具备「权威集合」语义：有人在控制台手工加进这个组，
# Terraform 看不见。下面的 check 块负责把这个盲区暴露出来。
resource "alicloud_ram_user_group_attachment" "developers" {
  for_each = var.developer_group_name == "" ? toset([]) : toset(var.developer_user_names)

  group_name = alicloud_ram_group.developers[0].group_name
  user_name  = each.value
}

# 组成员漂移检测。
#
# 这里用 check 而非 precondition 是有意的：check 失败只产生警告，不阻断。
# 因为首次 apply 时上面的绑定尚未建立，数据源读到的成员必然少于声明值，
# 用 precondition 会让第一次 apply 直接失败。漂移检测的正确形态就是告警。
data "alicloud_ram_users" "in_developer_group" {
  count = var.developer_group_name == "" ? 0 : 1

  group_name = alicloud_ram_group.developers[0].group_name
}

check "developer_group_has_no_unmanaged_members" {
  assert {
    condition = var.developer_group_name == "" ? true : length(setsubtract(
      toset([for u in data.alicloud_ram_users.in_developer_group[0].users : u.name]),
      toset(var.developer_user_names),
    )) == 0
    error_message = "用户组 ${var.developer_group_name} 里存在不在 developer_user_names 中的成员，说明有人绕过 Terraform 在控制台手工加了人。请把他们补进 tfvars，或从组里移除。"
  }
}

resource "alicloud_ram_group_policy_attachment" "developers" {
  for_each = var.developer_group_name == "" ? toset([]) : toset(concat(
    ["${local.name_prefix}-developer"],
    local.guardrail_policy_names,
  ))

  policy_name = each.value
  policy_type = "Custom"
  group_name  = alicloud_ram_group.developers[0].group_name

  depends_on = [alicloud_ram_policy.this]
}
