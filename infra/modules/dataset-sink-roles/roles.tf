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
  write_subjects = ["repo:${var.github_repo}:environment:${var.github_environment}"]
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
  subjects             = local.write_subjects
  max_session_duration = var.max_session_duration

  attach_custom_policy_names = concat(
    ["${local.name_prefix}-dlc-submit"],
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
