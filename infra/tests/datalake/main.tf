data "alicloud_account" "current" {}

locals {
  bucket_names = {
    landing = "${var.bucket_prefix}-${data.alicloud_account.current.id}-landing"
    lakefs  = "${var.bucket_prefix}-${data.alicloud_account.current.id}-lakefs"
    iceberg = "${var.bucket_prefix}-${data.alicloud_account.current.id}-iceberg"
    result  = "${var.bucket_prefix}-${data.alicloud_account.current.id}-result"
  }
}

resource "alicloud_vpc" "this" {
  vpc_name   = "${var.cluster_name}-vpc"
  cidr_block = "10.88.0.0/16"
  tags       = var.tags
}

resource "alicloud_vswitch" "nodes" {
  vpc_id       = alicloud_vpc.this.id
  zone_id      = var.zone_id
  cidr_block   = "10.88.0.0/20"
  vswitch_name = "${var.cluster_name}-nodes"
  tags         = var.tags
}

resource "alicloud_cs_managed_kubernetes" "this" {
  name                 = var.cluster_name
  profile              = "Default"
  cluster_spec         = "ack.pro.small"
  version              = var.kubernetes_version == "" ? null : var.kubernetes_version
  vswitch_ids          = [alicloud_vswitch.nodes.id]
  service_cidr         = "10.89.0.0/20"
  pod_cidr             = "10.90.0.0/16"
  new_nat_gateway      = true
  slb_internet_enabled = true
  enable_rrsa          = true
  deletion_protection  = false
  timezone             = "Asia/Shanghai"
  tags                 = var.tags

  addons {
    name = "flannel"
  }

  addons {
    name = "csi-plugin"
  }

  addons {
    name = "csi-provisioner"
  }

  addons {
    name   = "nginx-ingress-controller"
    config = jsonencode({ IngressSlbNetworkType = "intranet" })
  }

  timeouts {
    create = "90m"
    update = "60m"
    delete = "60m"
  }
}

resource "alicloud_cs_kubernetes_node_pool" "system" {
  cluster_id            = alicloud_cs_managed_kubernetes.this.id
  node_pool_name        = "system"
  vswitch_ids           = [alicloud_vswitch.nodes.id]
  instance_types        = var.system_instance_types
  desired_size          = tostring(var.system_node_count)
  image_type            = "AliyunLinux3ContainerOptimized"
  system_disk_category  = "cloud_essd"
  system_disk_size      = 120
  runtime_name          = "containerd"
  install_cloud_monitor = true
  tags                  = var.tags

  labels {
    key   = "workload"
    value = "platform"
  }

  management {
    enable          = true
    auto_repair     = true
    auto_upgrade    = false
    max_unavailable = 1
  }

  depends_on = [alicloud_cs_managed_kubernetes.this]
}

resource "alicloud_cs_kubernetes_node_pool" "spark" {
  cluster_id            = alicloud_cs_managed_kubernetes.this.id
  node_pool_name        = "spark"
  vswitch_ids           = [alicloud_vswitch.nodes.id]
  instance_types        = var.spark_instance_types
  desired_size          = tostring(var.spark_node_count)
  image_type            = "AliyunLinux3ContainerOptimized"
  system_disk_category  = "cloud_essd"
  system_disk_size      = 300
  runtime_name          = "containerd"
  install_cloud_monitor = true
  tags                  = var.tags

  labels {
    key   = "workload"
    value = "spark"
  }

  taints {
    key    = "workload"
    value  = "spark"
    effect = "NoSchedule"
  }

  management {
    enable          = true
    auto_repair     = true
    auto_upgrade    = false
    max_unavailable = 1
  }

  depends_on = [alicloud_cs_kubernetes_node_pool.system]
}

resource "alicloud_oss_bucket" "data" {
  for_each = local.bucket_names

  bucket        = each.value
  storage_class = "Standard"
  force_destroy = true
  tags          = merge(var.tags, { DataLayer = each.key })

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
      days = 3
    }
  }
}

resource "alicloud_oss_bucket_acl" "data" {
  for_each = alicloud_oss_bucket.data
  bucket   = each.value.bucket
  acl      = "private"
}
