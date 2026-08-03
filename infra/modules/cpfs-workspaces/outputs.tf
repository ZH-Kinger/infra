output "user_fileset_ids" {
  description = "个人区 Fileset ID，按用户名索引"
  value       = { for k, v in alicloud_nas_fileset.user : k => v.fileset_id }
}

output "releases_fileset_id" {
  description = "发布区 Fileset ID"
  value       = alicloud_nas_fileset.releases.fileset_id
}

output "dataflow_id" {
  description = "发布区与归档桶之间的数据流动 ID"
  value       = try(alicloud_nas_data_flow.releases[0].data_flow_id, null)
}

output "quota_commands" {
  description = <<-EOT
    设置个人区配额的命令。

    **Provider 不支持在 Fileset 上直接设配额**（`alicloud_nas_fileset` 没有
    quota 属性），所以这一步落在 Terraform 之外。输出成命令而不是静默忽略，
    是为了让「配额没设」这件事在 apply 之后立刻可见——没有配额的话，一个人
    写满就把整个文件系统写满了，而 CPFS 的容量是所有人共享的。
  EOT
  value = [
    for w in var.user_workspaces : join(" ", compact([
      "aliyun nas SetFilesetQuota",
      "--FileSystemId ${var.filesystem_id}",
      "--FsetId ${alicloud_nas_fileset.user[w.name].fileset_id}",
      w.size_limit_gib == null ? "" : "--SizeLimit ${w.size_limit_gib * 1024 * 1024 * 1024}",
      w.file_count_limit == null ? "" : "--FileCountLimit ${w.file_count_limit}",
    ]))
    if w.size_limit_gib != null || w.file_count_limit != null
  ]
}
