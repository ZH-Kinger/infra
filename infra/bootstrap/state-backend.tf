# Terraform state 后端：OSS 存 state，Tablestore 存锁。

resource "alicloud_oss_bucket" "state" {
  bucket = var.state_bucket

  # ACL 走独立的 alicloud_oss_bucket_acl 资源：内联 acl 字段自 Provider
  # 1.220.0 起已废弃。

  # 版本控制是 state 损坏时的唯一救命手段：误 apply 后可以取回上一版。
  versioning {
    status = "Enabled"
  }

  server_side_encryption_rule {
    sse_algorithm = "AES256"
  }

  lifecycle_rule {
    id      = "expire-noncurrent-state-versions"
    enabled = true

    noncurrent_version_expiration {
      days = var.state_retention_days
    }
  }

  tags = var.tags

  lifecycle {
    # 删掉 state 桶等于丢掉全部基础设施的账本。
    prevent_destroy = true
  }
}

# 阻断公共访问：即使有人误改 ACL 或加了公开策略，这一层仍然拦住。
resource "alicloud_oss_bucket_acl" "state" {
  bucket = alicloud_oss_bucket.state.bucket
  acl    = "private"
}

# ---------------------------------------------------------------------------
# state 锁。
#
# 没有锁时，两条流水线同时 apply 会互相覆盖 state，产生「资源实际存在但
# state 里没有」或反之的漂移，且很难事后还原。OSS backend 用 Tablestore
# 实现锁，表结构是固定约定：单一主键 LockID，类型 String。
# ---------------------------------------------------------------------------
resource "alicloud_ots_instance" "lock" {
  name        = var.lock_instance_name
  description = "Terraform state 锁（OSS backend 依赖），不要用于业务数据"

  # 锁表读写量极小，容量型实例足够且更便宜。
  instance_type = "Capacity"
  accessed_by   = "Any"

  tags = var.tags
}

resource "alicloud_ots_table" "lock" {
  instance_name = alicloud_ots_instance.lock.name
  table_name    = var.lock_table_name

  primary_key {
    name = "LockID"
    type = "String"
  }

  time_to_live                  = -1 # 锁记录不过期，靠 Terraform 自己释放
  max_version                   = 1
  deviation_cell_version_in_sec = "86400"

  lifecycle {
    prevent_destroy = true
  }
}
