# CPFS 上的三类区域。
#
# 发布协议只管「已发布的不可变 release」，但用户总得有地方干活——预处理、
# 做实验、存中间结果。这个模块管的就是那部分，以及它和发布区的边界。
#
#   /users/<name>/            个人区    本人可读写      Fileset + POSIX UID
#   /shared/                  公共区    全体可读写      Fileset + POSIX 组
#   /datasets/<ds>/<commit>/  已发布    没有人可写      Fileset + _READY 协议
#
# **这层隔离靠 CPFS Fileset + POSIX，不是 RAM。** RAM 只回答「能不能看到这个
# 文件系统」，实际能读写哪些目录由文件系统自己决定——这是四套授权面里最容易
# 被忽略的一条，见 docs/permissions.md。
#
# 用户在自己区里怎么折腾都是自由的。要守的只有一条：**投喂给训练的必须是已
# 发布的不可变 release，不能是任何可写目录**。这条由 training-guard 在训练
# 容器内 fail-closed 强制，而不是靠这里的权限设置。

locals {
  # 个人区一人一个 Fileset。用 Fileset 而不是普通目录，是为了拿到配额能力——
  # 没有配额的话，一个人写满就把整个文件系统写满了，而 CPFS 最小 3600 GiB
  # 的容量是所有人共享的。
  user_filesets = {
    for w in var.user_workspaces : w.name => w
  }
}

resource "alicloud_nas_fileset" "user" {
  for_each = local.user_filesets

  file_system_id   = var.filesystem_id
  file_system_path = "${var.users_root}/${each.key}/"
  description      = "workspace for ${each.key}"

  # 个人区里可能有还没归档的实验数据，误删代价高。
  deletion_protection = var.deletion_protection
}

resource "alicloud_nas_fileset" "shared" {
  count = var.shared_root == "" ? 0 : 1

  file_system_id      = var.filesystem_id
  file_system_path    = "${var.shared_root}/"
  description         = "shared read-write scratch"
  deletion_protection = var.deletion_protection
}

# 发布区单独一个 Fileset。它必须和工作区分开，原因有三个：
#   1. 配额独立——工作区写满不能影响已发布数据；
#   2. 挂载方式不同——训练作业只读挂这里；
#   3. 数据流动只能绑在 Fileset 上，而只有发布区需要和归档桶联动。
resource "alicloud_nas_fileset" "releases" {
  file_system_id      = var.filesystem_id
  file_system_path    = "${var.releases_root}/"
  description         = "published immutable releases"
  deletion_protection = true # 已发布数据，永远开
}

# ---------------------------------------------------------------------------
# 数据流动：发布区 ↔ 归档桶
#
# 六条前提里的第一条就是「必须挂在 Fileset 上」，所以它绑的是上面那个
# releases Fileset。另外两条前提（桶要打 cpfs-dataflow 标签、要开版本控制）
# 由 platform 层管归档桶时保证——见 var.archive_bucket 的说明。
# ---------------------------------------------------------------------------
resource "alicloud_nas_data_flow" "releases" {
  count = var.archive_bucket == "" ? 0 : 1

  file_system_id = var.filesystem_id
  fset_id        = alicloud_nas_fileset.releases.fileset_id
  source_storage = "oss://${var.archive_bucket}"

  # 只接受 600 / 1200 / 1500，且要小于文件系统本身的 I/O 吞吐。
  throughput = var.dataflow_throughput

  description = "releases <-> ${var.archive_bucket}"
}
