# OSS / CPFS 数据管理与生命周期

本文是本项目关于**数据放在哪里、谁能写、如何进入训练、何时回收**的权威说明。
架构、权限、运行时和运维文档若有歧义，以本文和代码中的强制校验为准。

相关：[整体架构](architecture.md)｜[权限模型](permissions.md)｜[运行时](pai-runtime.md)｜[运维](runbook.md)

---

## 1. 核心模型

```text
OSS          持久字节、归档、跨生命周期保存
lakeFS       Commit / Tag 与版本真相
CPFS         训练所在 VPC 内的高吞吐热副本
PAI Dataset  把一个 CPFS release 暴露给 DSW/DLC
```

CPFS 不是 OSS 的替代品，OSS 也不是 GPU 训练的默认数据面：

```text
持久层 OSS → lakeFS Commit → CPFS 热副本 → PAI 只读挂载 → DSW/DLC
```

正式训练数据必须同时满足：

1. 能解析到不可变 lakeFS Commit；
2. CPFS 路径为 `/datasets/<dataset>/<commit>/`；
3. `release.json`、Manifest 与 `_READY` 完整；
4. 已注册为对应的 PAI Dataset Version；
5. 在 DSW/DLC 中以 `RO` 挂载到 `/mnt/dataset`。

---

## 2. 两种“注册”不要混淆

项目里存在两种完全不同的注册：

| 注册 | 管什么 | 在哪里声明 | 发生在什么时候 |
|---|---|---|---|
| 数据源注册表 | 哪些 OSS Bucket/Prefix 能被扫描、归档或当工作区 | Terraform `data_sources` | 接入一个存储位置时 |
| PAI Dataset Version | 哪个不可变 CPFS release 能被训练挂载 | `dataset-release.yml` | Commit 发布并校验完成后 |

所以“OSS 还没有注册为 PAI 数据集”不是问题。存量 OSS 的正确路径是：先登记为平台
数据源，再零拷贝纳入 lakeFS，沉降到 CPFS，最后注册**CPFS release** 为 PAI Version。
正式训练不需要、也不应该把裸 OSS 前缀直接注册为训练数据集。

---

## 3. OSS 逻辑分区

这些是逻辑边界，可以位于同一个桶的不同前缀，也可以使用不同桶。生产环境应优先用
不同桶或至少使用独立前缀、Bucket Policy 和生命周期策略。

```text
oss://<bucket>/
├── staging/          接入与归档前的临时数据，可变
├── datasets/         dataset-sink 维护的持久归档
├── output/           DLC 输出和 Checkpoint
└── workspace/        用户或团队临时工作区

外部存量桶：
└── <registered-prefix>/   被 lakeFS import 后必须冻结为只读

lakeFS 后端桶：
└── <opaque-blockstore>/   只允许 lakeFS 服务与必要的沉降身份访问
```

### 3.1 分区规则

| 区域 | 内容是否可变 | 谁能写 | 能否直接作为正式训练输入 |
|---|---:|---|---:|
| `staging/` | 是 | 接入方 / Materializer | 否 |
| `datasets/` 归档 | 否 | 仅 Materializer 创建新对象 | 否，先沉降 CPFS |
| 被 import 的外部前缀 | **否** | 没有人 | 否，先沉降 CPFS |
| `workspace/` | 是 | 对应用户或团队 | **否** |
| `output/` | 是 | 对应训练运行身份 | 否，发布后才能变成新输入 |
| lakeFS 后端 | 内部管理 | lakeFS 服务 | 严禁绕过 lakeFS 裸读 |

### 3.2 数据源注册表的三个 mode

管理员在 `infra/envs/<env>/access/terraform.tfvars` 声明：

```hcl
data_sources = [
  { name = "legacy-robotics", bucket = "legacy-data", prefix = "robotics", mode = "readonly" },
  { name = "sink-archive", bucket = "dataset-archive", prefix = "datasets", mode = "archive" },
  { name = "team-scratch", bucket = "dataset-work", prefix = "workspace", mode = "workspace" },
]
```

| mode | 谁能写 | 能否扫描 | 能否当 lakeFS Commit 物理来源 |
|---|---|---:|---:|
| `readonly` | 没有人 | 是 | 是 |
| `archive` | Materializer | 是 | 是 |
| `workspace` | 研发用户 | 是 | **否** |

同一份 Terraform 声明生成 RAM 策略与 `deploy/data-sources.json`。不要手工修改渲染
产物，也不要绕过 `--registry`：CLI 校验负责报清楚的错误，RAM 负责最终拒绝。

### 3.3 零拷贝 import 后必须冻结

lakeFS import 只记录对象物理地址，不复制字节。对象被覆盖或删除时 Commit 不会同步
报错，却会在未来 materialize 或深度校验时损坏。因此一个前缀完成 import 后必须：

- 加入 `imported_data_prefixes`，对本项目身份统一 Deny 写删；
- 启用 OSS 版本控制；
- 生产数据使用 Bucket Policy 和合规保留策略（WORM）兜底；
- 停止所有历史写入方，不把“没人应该写”当成技术控制。

---

## 4. CPFS 目录与 Fileset

```text
/mnt/cpfs/
├── users/<user>/               个人开发区
├── shared/                     团队共享素材区
├── staging/<dataset>/<batch>/  发布前临时区
├── datasets/<dataset>/<commit>/正式不可变 release
└── output/<user>/<run-id>/      训练输出（需要 CPFS 输出时）
```

| 目录 | 权限 | 是否可变 | 是否可直接训练 | 管理机制 |
|---|---|---:|---:|---|
| `/users/<user>/` | 本人 RW | 是 | 否 | Fileset、UID/GID、配额 |
| `/shared/` | 团队 RW | 是 | 否 | Fileset、POSIX 组、配额 |
| `/staging/` | 发布身份 RW | 是 | 否 | 独立 Fileset、发布锁 |
| `/datasets/<ds>/<commit>/` | 训练 RO | **否** | 是 | `_READY`、Manifest、原子发布 |
| `/output/<user>/<run-id>/` | 任务 RW | 是 | 否 | 每用户/任务边界、配额 |

RAM 只能控制 NAS/CPFS 管理 API，不能表达目录级权限。目录隔离必须由 Fileset、POSIX
UID/GID、PAI `MountAccess` 和配额共同完成。

Fileset 必须在目录为空时预先创建。已有文件的目录无法原地变成 Fileset，迁移按
[Fileset 迁移手册](cpfs-fileset-migration.md)执行。CPFS 总容量由所有目录共享，没有
配额时一个用户就能写满文件系统，导致全体发布和训练失败。

---

## 5. 三类数据入口

### 5.1 存量数据已经在 OSS

```text
冻结 OSS 前缀
→ scan-oss：列举、大小、SHA-256
→ lakeFS zero-copy import + Commit
→ materialize / DataFlow Import 到 CPFS
→ verify --deep
→ 原子发布 + _READY
→ 注册 PAI Dataset Version
```

建 Commit 不搬字节；沉降到 CPFS 时才搬一次。前缀必须已登记为 `readonly` 或
`archive`，完成 import 后不得恢复写入。

### 5.2 数据在 CPFS 工作区或 staging

```text
CPFS 可写目录
→ scan：生成 Manifest
→ archive / DataFlow Export 到 OSS
→ lakeFS zero-copy import + Commit
→ 新 staging 用 certify；存量目录用 materialize
→ _READY
→ 注册 PAI Dataset Version
```

不能让 Commit 直接指向可写工作区；必须先归档到稳定 OSS 位置。`certify` 的 rename
只适合允许被移动的新 staging。纳管已有 CPFS 或已有 PAI Version 背后的目录时使用
`cpfs-adopt`：扫描与归档后从 lakeFS 重新物化 release，原目录保持不动。原 PAI Version
只是历史元数据，不能替代 Commit、Manifest 和 `_READY` 校验。

### 5.3 已经存在 lakeFS Commit

```text
固定 Commit
→ materialize / DataFlow Import
→ verify --deep
→ CPFS release
→ PAI Dataset Version
```

Branch、`latest`、日期目录和其他可变引用不得进入训练请求。

---

## 6. DataFlow 的语义

| 动作 | 方向 | 是否移动数据 | 是否释放 CPFS 空间 |
|---|---|---:|---:|
| `Import` | OSS → CPFS | 否，复制/预热 | 否 |
| `Export` | CPFS → OSS | 否，复制/沉淀 | 否 |
| `Evict` | 释放 CPFS 数据块 | 不删 OSS | **是** |

因此“沉降”不是移动：正确顺序是 `Export → 校验 → Evict`。只做 Export 不会减少
CPFS 用量；只做 Evict 则必须先证明 OSS 中存在可恢复副本。

正式 release 路径禁止配置 `AutoRefreshPolicy=ImportChanged`。自动刷新会让同一个
Commit 目录跟随 OSS 变化，直接破坏不可变性。一次性 Import、Export 和 Evict 可以用，
release 上的 AutoRefresh 不可以用。

DataFlow 只负责搬字节，不负责验证内容。Import 完成后仍必须核对文件集合、大小与
SHA-256，并通过原子发布协议写 `_READY`。

---

## 7. PAI 挂载合同

| 容器路径 | 来源 | 权限 | 用途 |
|---|---|---|---|
| `/mnt/dataset` | 已注册的 CPFS Dataset Version | `RO` | 唯一正式训练输入 |
| `/mnt/workspace` | 用户 CPFS Fileset | `RW` | DSW 代码、中间结果、探索 |
| `/mnt/output` | 独立 CPFS/OSS 输出前缀 | `RW` | DLC Checkpoint 与结果 |
| `/mnt/oss-workspace` | 受控 OSS 用户前缀 | `RW/RO` | 可选临时区，**当前未实现** |

用户不能填写 Dataset ID、OSS/CPFS URI、RAM User ID、VPC、安全组或挂载权限。这些由
受评审 Profile 和用户映射生成。即使未来支持 `/mnt/oss-workspace`，也只能选择平台
白名单中的用户前缀，且不得作为 `/mnt/dataset` 或正式产出的版本依据。

训练入口先执行 `training-guard`，核对 Commit、Manifest 和 `_READY`，通过后才运行
用户命令。DSW 是交互式环境，无法从原理上阻止用户读取其有 POSIX 权限的工作区；
正式结果必须在登记时关联 Commit，并由挂载审计发现直接 URI、RW 训练集等违规配置。

---

## 8. CPFS 回收

CPFS release 是可重建热副本，也是整个体系中唯一被设计成可回收的数据层。回收前按
顺序检查：

1. `.keep` 是否人工置顶；
2. `_READY` 是否存在；
3. 是否在保护期内；
4. 是否属于每个数据集最近保留的版本；
5. 是否被活动 DSW/DLC 使用；
6. lakeFS Commit 和 OSS 字节是否仍可恢复。

任何检查不确定都保留。`reclaim` 默认 dry-run，`--execute` 才执行。

`dataset-lifecycle.yml` 在 Actions 中人工触发，默认只生成 dry-run 报告。真实 Evict 只能
显式打开 `execute` 并经过 `dataset-lifecycle` Environment 审批。审批后流水线不会直接使用旧计划，
而是重新检查保护期、最近版本、活动 DSW/DLC 与 lakeFS 可恢复性，避免执行过期结论。

| 策略 | 结果 | 再次训练 |
|---|---|---|
| `cpfs-evict` | 目录和 PAI Version 仍有效，只释放数据块 | 首次访问从 OSS 冷加载 |
| `hard-delete` | CPFS 目录消失，PAI Version 暂时悬空 | 重新 materialize 后恢复 |

找不到覆盖路径的 DataFlow 时禁止从 Evict 静默降级为硬删除。灵骏 BMCPFS 不支持
Evict，只能按硬删除规则处理。

---

## 9. 管理职责

| 责任方 | 管什么 |
|---|---|
| Terraform `platform` | OSS 桶属性、版本控制、标签、CPFS/Fileset、网络 |
| Terraform `access` | 数据源注册表、RAM 策略、PAI 成员与提交角色 |
| Dataset release 流水线 | 扫描、归档、Commit、沉降、校验、注册 PAI Version |
| PAI runtime 流水线 | 根据 Profile 生成 DSW/DLC 挂载，不接受任意 URI |
| 算法用户 | 只写个人工作区和任务输出，正式训练只选已发布版本 |
| 运维人员 | 容量、配额、DataFlow、挂载审计与回收 dry-run |

这套流程不依赖 Runtime Portal。GitHub Actions 可以作为当前入口；未来无论换成 Portal、
CLI 还是内部平台，后端都必须遵守同一份存储和挂载合同。

---

## 10. 不可违反的规则

1. 正式训练输入必须对应 lakeFS Commit，禁止 Branch、`latest` 和裸 OSS。
2. 被零拷贝 import 引用的 OSS 前缀不可覆盖或删除。
3. `workspace` 只能作为素材区，不能直接成为 Commit 的物理来源。
4. release 目录以 Commit ID 命名，`_READY` 最后写入，发布后不可改写。
5. `/mnt/dataset` 永远只读；输出必须写到独立 RW 挂载。
6. release 路径禁止 AutoRefresh。
7. Import/Export 不释放空间；回收必须显式 Evict 或经过保护的 hard-delete。
8. CPFS 目录隔离使用 Fileset、POSIX 和配额，不能只依赖 RAM。
9. 用户不能提交任意 OSS/CPFS URI，也不能指定别人的工作区或 RAM User ID。
10. 回收无法证明可恢复时一律保留，不允许猜测性删除。
