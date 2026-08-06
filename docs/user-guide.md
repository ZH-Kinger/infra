# 用户手册

面向算法工程师、数据工程师和训练任务提交者。这里讲“怎么使用平台”，不要求理解
Terraform、RAM 策略或 CPFS API。管理员配置、审批和排错见[管理员手册](admin-guide.md)。

## 1. 登录后能看到什么

控制台顶部导航直接对应六类工作：

| 页面 | 主要内容 | 普通用户可以做什么 |
|---|---|---|
| 运营总览 | 数据量、CPFS 使用率、活动实例和最近操作 | 查看状态、进入常用操作 |
| 数据资产 | 数据集、lakeFS Commit、容量、版本和热数据比例 | 查找可训练的固定版本 |
| 存量纳管 | OSS 或 CPFS 数据接入 | 填少量参数并生成计划 |
| 容量与生命周期 | DataFlow 预热、沉淀、Evict 候选 | 查看计划；真实回收由管理员审批 |
| DSW / DLC | 受控开发环境和训练任务 | 选择数据版本与计算 Profile |
| 权限与审计 | RAM、PAI、挂载边界和操作记录 | 查看自己的操作和合规状态 |

页面上的资产数字如果标注为演示数据，不代表真实云资源；操作记录来自平台数据库。

## 2. 普通用户的标准流程

### 使用已有数据训练

1. 在“数据资产”选择数据集和固定 Commit。
2. 进入“DSW / DLC”，选择 DSW 或 DLC、镜像 Profile 和算力 Profile。
3. 先保持“仅生成计划”，核对数据集版本、只读挂载、网络和资源规格。
4. 提交执行后等待审批；平台创建实例并把数据只读挂到 `/mnt/dataset`。
5. 训练输出写 `/mnt/workspace` 或 `/mnt/output`，不要写 `/mnt/dataset`。

训练启动前会运行 `training-guard`。Commit、Manifest 或 Paimon Snapshot 不一致时，
任务会直接停止，不能由用户绕过。完整示例见[使用入门](onboarding.md)和
[DSW/DLC 自助使用](pai-runtime.md)。

### 纳管已有 OSS 数据

在“存量纳管”选择 OSS，只填写：

- 数据集短名称；
- lakeFS Repository；
- `oss://bucket/prefix`；
- 发布 Tag。

平台自动补齐 Workflow、RAM Role、Region、CPFS、PAI Dataset ID 和
`transfer_mode=dataflow`。执行链路为：

```text
OSS 扫描 → lakeFS 零拷贝 Commit → DataFlow Import 预热
→ 完整性校验 → CPFS 不可变 release → PAI Dataset Version
```

OSS 前缀必须先由管理员登记为只读数据源。用户不能从表单注入任意 Bucket 权限、
RAM Role 或 PAI Dataset ID。

### 纳管已有 CPFS 数据

在“存量纳管”选择 CPFS，只填写数据集、Repository、已有目录、Tag 和归档前缀。
平台默认调用 DataFlow `Export` 沉淀到 OSS，再建立 lakeFS Commit；原目录需要保留时，
使用 `cpfs-adopt`，不会把原目录直接 rename 掉。

源目录必须位于管理员预先建立的 Fileset/DataFlow 覆盖范围内。未覆盖时流水线会失败，
不会偷偷退回到 runner 做 TB 级复制。

## 3. 计划与执行的区别

| 模式 | 是否修改云资源 | 谁能用 |
|---|---|---|
| 仅生成计划 | 否；保存请求和审计记录 | 已登录用户 |
| 提交执行 | 触发受控 GitHub Workflow | 管理员白名单用户 |
| Environment 审批后执行 | 创建版本、实例或执行回收 | GitHub 审批人 |

控制台登录不等于获得阿里云高权限。站点只负责生成受约束的请求，真正的云操作使用
GitHub OIDC 换取短期 RAM Role。

## 4. 权限申请

普通研发通常申请 `PAI.AlgoDeveloper`；排查训练申请 `PAI.AlgoOperator`；只读查看申请
`PAI.WorkspaceGuest`。不要申请长期 AccessKey，也不要在阿里云控制台手工修改成员。
权限变更通过 `infra/envs/<env>/access` 的 PR 和安全审批完成。

## 5. 常见问题

| 现象 | 处理方式 |
|---|---|
| 看不到 PAI 工作空间 | 申请 PAI 成员关系 |
| 看得到版本但训练启动失败 | 检查 Commit、Manifest 和 `_READY` |
| DataFlow 提示路径未覆盖 | 联系管理员补 Fileset/DataFlow，不要改用裸 OSS 训练 |
| 只能生成计划 | 当前账号不在执行白名单，属于正常权限隔离 |
| CPFS 数据读不到 | 检查 PAI 挂载、Fileset/POSIX 权限和版本状态 |

