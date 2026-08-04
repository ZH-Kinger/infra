# 存量 CPFS 目录迁入 Fileset

相关：[架构](architecture.md)｜[运维](runbook.md)｜[权限](permissions.md)

## 为什么必须迁移

CPFS 数据流动要求 `FsetId`，而 Fileset 只能创建在新的空目录上。已经有文件的
`/users/<name>/`、`/shared/` 或 release 根目录，不能原地补成 Fileset。因此正确顺序是：

> 先创建空 Fileset，再向其中写数据；存量目录必须迁到新的 Fileset 路径。

不要把 DataFlow 的 `AutoRefreshPolicy=ImportChanged` 配到 release 路径。release 必须
保持不可变；预热 Import、沉淀 Export 和 Evict 都只能是显式的一次性任务。

## 迁移边界

每次只迁一个目录或一个团队，源与目标必须都在同一个已确认的 CPFS 文件系统内：

```text
源：/users/alice/                 已有数据，非 Fileset
目标：/filesets/users/alice/      Terraform 先创建，初始为空
```

跨 Fileset 的 `rename` 是否仍为纯元数据操作，必须用同规格 canary 实测，不能因为
“同一文件系统”就假定零拷贝。无法证明时一律走复制、校验、切换、延迟清理。

## 七步迁移

1. **盘点**：记录源路径的所有者、POSIX UID/GID、容量、文件数、当前 DSW/DLC 挂载、
   DataFlow 绑定和写入方。任何未知写入方都视为阻塞。
2. **建空目标**：通过 Terraform 的 `cpfs-workspaces` 模块创建 Fileset、导出和所需
   DataFlow。只允许流水线审批后 apply；不要在本地 apply。
3. **canary**：用独立小目录测复制吞吐、权限保留、校验耗时，以及跨 Fileset rename
   的真实行为。canary 失败不进入正式迁移。
4. **冻结写入**：停止写入作业，关闭或转只读 DSW 会话，确认没有活跃 DLC/DSW 使用源
   路径。记录冻结时间；冻结之后源目录只读。
5. **复制并校验**：复制到目标临时目录，至少核对文件集合、总字节数和 SHA-256
   manifest；保留 UID/GID/mode。校验完成前不注册新的 PAI Dataset Version。
6. **切换**：把目标封印为 `<dataset>/<commit>/`，生成 `_READY`，注册新的 PAI
   Dataset Version，并用 `MountAccess=RO` 做 DSW canary 与 DLC smoke test。
7. **观察与清理**：旧路径保持只读，至少跨过约定的回滚窗口。PAI 审计连续无引用、
   新路径训练稳定后，另开审批变更清理旧目录；迁移流程本身不自动删除源数据。

## 切换门禁

只有全部满足才允许切换：

- 源路径从冻结时刻起没有变化；
- 目标文件集合、字节数与 manifest 哈希全部一致；
- 目标在预建 Fileset 内，DataFlow 覆盖关系按路径边界校验通过；
- PAI Version 的 `SourceId`、`lakefs_commit`、`manifest_sha256` 和 URI 末级 Commit 一致；
- DSW/DLC 挂载为 RO，`training-guard` 通过；
- 回滚负责人和旧路径保留期限已记录。

任何一项不满足都保留旧路径，不做“先切过去再观察”。

## 回滚

回滚只改变 PAI 作业使用的 Dataset Version，不覆盖新旧目录。停止新作业后重新选择旧
Version，确认旧路径仍为只读且 `_READY`/manifest 完整，再恢复训练。已经发布的新
release 不得改写；问题修复后发布新的 Commit，而不是修补原目录。

## 当前 dev 环境注意事项

2026-08-04 只读体检确认 CPFS `cpfs-00a27a8ec8b1e13a`（cn-hangzhou-i）Running，
并已有一个 Fileset。但此前真实测试表明该区挂载点库存不足：文件系统存在不代表
DSW/DLC/runner 能挂载。正式迁移前必须先验证同可用区挂载点和训练算力都可用；
库存未恢复时只做盘点与控制面只读测试，不启动数据复制或切换。
