terraform {
  required_version = ">= 1.5.0"

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = ">= 1.252.0, < 2.0.0"
    }
  }

  # state 后端参数由 bootstrap 输出，通过 backend.hcl 注入：
  #   terraform init -backend-config=../../../backend.hcl
  backend "oss" {
    key    = "prod/platform.tfstate"
    prefix = "terraform"
    acl    = "private"
  }
}

provider "alicloud" {
  region = var.region
}

locals {
  tags = merge(var.tags, {
    Environment = "prod"
    Layer       = "platform"
  })

  # 归档桶额外要这个标签，否则 CPFS 数据流动直接拒绝建立。
  #
  # 这是六条前提里最不显眼的一条：`CreateDataFlow` 会检查源 OSS 桶上有没有
  # `cpfs-dataflow` 标签，没有就报错，而报错信息不会告诉你缺的是标签。
  # 2026-08-03 在真实 CPFS 2.0 上撞出来的。
  #
  # 建桶时就打上，而不是等 preflight 报 FAIL 再手工补——我们本来就能避免的
  # 问题不该让人再排查一遍。标签值本身不重要，存在即可。
  dataset_bucket_tags = merge(local.tags, {
    "cpfs-dataflow" = "true"
  })
}

# ---------------------------------------------------------------------------
# 数据集归档桶：staging / datasets / output 三个前缀，权限在 access 层按前缀切分。
# ---------------------------------------------------------------------------
resource "alicloud_oss_bucket" "dataset" {
  bucket = var.dataset_bucket

  versioning {
    status = "Enabled"
  }

  server_side_encryption_rule {
    sse_algorithm = "AES256"
  }

  lifecycle_rule {
    id      = "abort-incomplete-multipart-uploads"
    enabled = true

    abort_multipart_upload {
      days = 7
    }
  }

  tags = local.dataset_bucket_tags

  lifecycle {
    # 生产数据集归档桶。RAM 侧也 Deny 了 oss:DeleteBucket，两层都拦。
    prevent_destroy = true
  }
}

resource "alicloud_oss_bucket_acl" "dataset" {
  bucket = alicloud_oss_bucket.dataset.bucket
  acl    = "private"
}

# ---------------------------------------------------------------------------
# CPFS 只引用，不纳管。
#
# CPFS 是有状态存储，纳管进 state 后一次属性漂移就可能触发 replace，
# 而 replace 一个装着训练数据的文件系统是灾难性的。这里只做存在性查询，
# 把 ID 传给下游，真正的目录级权限由 CPFS Fileset 和 POSIX 权限决定。
# ---------------------------------------------------------------------------
data "alicloud_nas_file_systems" "cpfs" {
  count = var.cpfs_filesystem_id == "" ? 0 : 1

  ids = [var.cpfs_filesystem_id]
}
