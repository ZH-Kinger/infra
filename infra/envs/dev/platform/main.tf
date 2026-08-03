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
    key    = "dev/platform.tfstate"
    prefix = "terraform"
    acl    = "private"
  }
}

provider "alicloud" {
  region = var.region
}

locals {
  tags = merge(var.tags, {
    # 这里曾经硬编码成 "prod"（从 prod 目录复制过来没改），于是 dev 的资源
    # 全部被标成生产。标签是出账和审计的依据，标错比不标更糟。
    Environment = "dev"
    Layer       = "platform"
  })

  # 归档桶额外要这个标签，否则 CPFS 数据流动直接拒绝建立。理由见 prod/platform。
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
    # dev 也保留 prevent_destroy：这个桶里躺着被 lakeFS 零拷贝 import 引用的
    # 对象，删桶会让 dev 的 Commit 集体悬空。要拆 dev 环境请显式改这里，
    # 而不是让一次无关的 apply 顺手把它带走。
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
