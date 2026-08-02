variable "region" {
  description = "阿里云地域，必须与 CPFS 和 PAI Workspace 实际所在地域一致"
  type        = string
}

variable "dataset_bucket" {
  description = "数据集归档/输出桶名，全局唯一"
  type        = string
}

variable "cpfs_filesystem_id" {
  description = <<-EOT
    已存在的 CPFS 文件系统 ID。留空则跳过查询（例如 CPFS 服务尚未开通时）。

    本层只查询不创建：见 main.tf 中的说明。若环境是灵骏 BMCPFS，它走 eflo
    而非 nas API，此处需改用灵骏侧的数据源——接入前先确认是哪一种。
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  description = "附加到支持标签的资源上的标签"
  type        = map(string)
  default = {
    Project   = "dataset-sink"
    ManagedBy = "Terraform"
  }
}
