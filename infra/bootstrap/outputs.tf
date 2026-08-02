output "account_id" {
  description = "当前阿里云账号 ID，填进 envs 层的 account_id 变量"
  value       = local.account_id
}

output "oidc_provider_arn" {
  description = "OIDC 身份提供商 ARN，填进 envs 层与 GitHub Environment 变量"
  value       = alicloud_ims_oidc_provider.github.arn
}

output "plan_role_arn" {
  description = "GitHub 仓库变量 ALIBABA_CLOUD_PLAN_ROLE_ARN 的值"
  value       = module.plan_role.role_arn
}

output "platform_apply_role_arn" {
  description = "GitHub Environment 变量 ALIBABA_CLOUD_PLATFORM_APPLY_ROLE_ARN 的值"
  value       = module.platform_apply_role.role_arn
}

output "access_apply_role_arn" {
  description = "GitHub Environment 变量 ALIBABA_CLOUD_ACCESS_APPLY_ROLE_ARN 的值"
  value       = module.access_apply_role.role_arn
}

output "backend_config" {
  description = <<-EOT
    envs 层 backend.hcl 的内容。用法：
      terraform init -backend-config=../../../backend.hcl
    backend.hcl 已被 .gitignore 忽略（含账号内资源名，不必进仓库）。
  EOT
  value       = <<-EOT
    bucket              = "${alicloud_oss_bucket.state.bucket}"
    region              = "${var.region}"
    tablestore_endpoint = "https://${alicloud_ots_instance.lock.name}.${var.region}.ots.aliyuncs.com"
    tablestore_table    = "${alicloud_ots_table.lock.table_name}"
    encrypt             = true
  EOT
}

output "ci_role_trust_policies" {
  description = "三个 CI 角色的信任策略，评审时确认 OIDC sub 是否被放宽"
  value = {
    plan           = module.plan_role.assume_role_policy
    platform_apply = module.platform_apply_role.assume_role_policy
    access_apply   = module.access_apply_role.assume_role_policy
  }
}
