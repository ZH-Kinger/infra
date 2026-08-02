output "dataset_bucket" {
  description = "数据集桶名，填进 access 层的同名变量"
  value       = alicloud_oss_bucket.dataset.bucket
}

output "cpfs_filesystem_id" {
  description = "CPFS 文件系统 ID（未配置时为 null）"
  value       = var.cpfs_filesystem_id == "" ? null : var.cpfs_filesystem_id
}

output "cpfs_found" {
  description = "查询结果是否命中。false 说明 ID 写错或服务未开通，此时不要继续接入发布流水线"
  value       = var.cpfs_filesystem_id == "" ? null : length(data.alicloud_nas_file_systems.cpfs[0].ids) > 0
}
