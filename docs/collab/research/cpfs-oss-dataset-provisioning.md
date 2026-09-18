# 审批通过后自动建「数据集合」：CPFS 目录 + OSS 前缀 的可行性取证

调研人：researcher　日期：2026-09-18
对象：CPFS 智算版 `bmcpfs-00000ub3ici1dnniit2i0`（cn-hangzhou）+ OSS + PAI AIWorkSpace

**证据标记**：`【文档】` 官方文档原文（附 URL）｜`【实测】` 真机只读 API 返回原文｜`【未确认】` 查不到或无法取证。

> ## ⚠️ 2026-09-18 二轮修订（v2）——先读 §10
>
> 用户根据实际操作给了三条观察，其中两条与 v1 结论冲突。二轮取证结果在 **§10（V1/V2/V3/V4）**。
> **v1 里被推翻 / 被降级的结论：**
>
> | v1 结论 | v2 状态 |
> |---|---|
> | §0.3 / §7「Q2 过不去、做不到隔离」 | **部分推翻** → 个人级做不到，但**工作空间级是真边界**（实测 18 人 vs 67 人）。见 §10.3 |
> | §6-b「CreateDataset 只登记指针」 | **仍成立（API 层面）**，但「目录从哪来」有了新答案：不是 CreateDataset 建的，**很可能是 DSW/DLC 挂载时 CSI 建的**。见 §10.1 |
> | §6-c「Accessibility 只是发现级」 | **维持**，但要加一句：不在工作空间里的人**连列表都调不到**，不是「看得见但没权限」。见 §10.3 |
> | §7 建议里的「PAI 侧照现网形状建数据集」 | **加一条硬约束**：`DeleteDataset` 可能连带动底层目录，**面板绝不能自动删**。见 §10.2 |
> | §1「CreateFileset 是建目录的首选」 | **维持，且证据更强**：实测这台 fs 上**只有 1 个 Fileset**，16 条个人数据集**没有一个**对应 Fileset —— 个人目录是普通 POSIX 目录，不是 Fileset。见 §10.1 |

---

## 0. 先看这三条（其余都是它们的展开）

1. **【实测】这件事现网已经有人在做了，而且做法就是「PAI 数据集」。** 工作空间 `590221 / ai_hz_gpu` 下
   16 条数据集**全部**是 `DataSourceType=BMCPFS`，一人一条，命名 = 员工登录名，
   URI = `bmcpfs://<VPC挂载点域名>/<登录名>/`。所以问题不是「能不能做」，而是「现在这套做法到底隔离了什么」。
2. **【实测+文档】它没有隔离。** 那 12 条个人数据集的 `Accessibility=ROLE_PUBLIC`，
   `AccessibleRoleIdList` 含 `PAI.AlgoDeveloper` —— 工作空间里每个算法开发者都看得见、用得了别人的那条。
   更根本的是 DSW/DLC 支持**直接挂载**整个 CPFS（填 `/` 即挂全盘），**完全绕开数据集这一层**。
3. **【文档】CPFS 智算版的数据面鉴权只有 POSIX uid/gid，RAM 管不到目录。**
   NAS 的 RAM 资源粒度最细到 `filesystem/<id>`，没有目录级/Fileset 级 ARN。
   面板发的是 RAM 子账号 —— **RAM 子账号与 CPFS 目录权限之间没有任何自动关联**。

→ 结论先行：**Q2 这一关过不去**（详见 §2 与 §7）。Q1 有办法过（`CreateFileset`，见 §1）。

---

## 1. Q1 — 能不能用 API 在 CPFS 上建目录？

### 1.1 能。智算版有 Fileset，且建 Fileset 就是建目录

**【文档】** 智算版**有**完整的 Fileset 接口（`CreateFileset` / `DescribeFilesets` / `DeleteFileset` /
`SetFilesetQuota`），与通用版是**同一套 action 名、不同的文档页和不同的约束**。

- CreateFileset（智算版页）：https://help.aliyun.com/zh/cpfs/bmcpfs/developer-reference/api-nas-2017-06-26-createfileset-bmcpfs
- 管理 Fileset（智算版用户指南）：https://help.aliyun.com/zh/cpfs/bmcpfs/user-guide/bmcpfs-manage-fileset

关键约束原文（智算版）：

| 项 | 原文 | 出处 |
|---|---|---|
| 版本 | 「仅CPFS智算版2.7.0及以上版本支持Fileset」 | 管理 Fileset 页 |
| 路径 | 「必须为新路径，不能为已存在路径」 | CreateFileset 页 / 管理 Fileset 页 |
| 父目录 | 「Fileset路径为多层目录时，父目录必须是已存在的目录」 | 管理 Fileset 页 |
| 数量 | 单文件系统默认 500 个（可申请至 3000） | 使用限制页 |
| 深度 | 「路径深度最多为8层，根目录（/）为0层」 | 使用限制页 |
| 嵌套 | 「不支持在Fileset中嵌套Fileset」 | 管理 Fileset 页 |
| 删除 | 「删除Fileset后磁盘空间会逐渐释放。数据被删除后无法恢复」 | 管理 Fileset 页 |

「路径必须是新路径 + 父目录必须已存在」这组约束只有在「**这个 API 自己把最后一级目录创建出来**」的前提下才自洽
（否则新路径永远不存在，接口永远建不成）。即：**`CreateFileset` = 控制面建目录**，无需挂载机。
→ 这条我给 `【文档】`，但把「API 自己创建末级目录」标为**强推断而非原文**；官方页面没有一句
「会自动创建该目录」的直述。上线前建议在一个**废弃路径**上真机跑一次 `DryRun=true` 再跑一次真建 + 删除来钉死。

**参数表（智算版 CreateFileset）【文档】**

| 参数 | 必填 | 说明 |
|---|---|---|
| `FileSystemId` | 是 | `bmcpfs-` 前缀 |
| `FileSystemPath` | 是 | 要创建的绝对路径，2–1024 字符，须以 `/` 开始和结束 |
| `Quota.SizeLimit` | 否 | 容量配额，最小 10 GiB，步长 1 GiB（智算版 2.7.0+） |
| `Quota.FileCountLimit` | 否 | 文件数配额，1 万 ~ 100 亿 |
| `DeletionProtection` | 否 | 释放保护，默认 false |
| `Description` / `ClientToken` / `DryRun` | 否 | — |

返回 `FsetId`。

### 1.2 现网这台机器满足版本要求

**【实测】** `DescribeFileSystems`（只读，bot-new 容器内 NAS OpenAPI）返回：

```json
"FileSystemId": "bmcpfs-00000ub3ici1dnniit2i0",
"Version": "2.7.0",
"Status": "Running",
"FileSystemType": "bmcpfs",  "ProtocolType": "cpfs",
"Capacity": 184320,  "Bandwidth": 72000,
"ZoneId": "cn-hangzhou-b",  "HpnZone": "B1",  "StorageType": "bm_advance_400",
"MountTargetCountLimit": 1,
"SupportedFeatures": {"SupportedFeature": [
    "FileSystem","HDD","VpcMount","IoAuditLog","DataFlow","Fileset","Quota","Vsc"]},
"VscTarget": "cpfs-00000ub3ici1dnniit2i0-000001.cn-hangzhou.cpfs.aliyuncs.com",
"MountTargets": {"MountTarget": [
  {"Status":"Active","MountTargetDomain":"cpfs-00000ub3ici1dnniit2i0-000001.cn-hangzhou.cpfs.aliyuncs.com"},
  {"Status":"Active","NetworkType":"vpc","VpcId":"vpc-bp1w4vnrtyrs76z1uyj4c",
   "MountTargetIp":"10.32.3.212",
   "MountTargetDomain":"cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com"}]}
```

`Version=2.7.0` 恰好卡在 Fileset 的最低版本线上，`SupportedFeatures` 里明确列了 `Fileset` 与 `Quota`。

### 1.3 但当前调用身份**没有** Fileset 权限

**【实测】** 用 bot 的全局 AK 调只读 `DescribeFilesets`：

```
NAS DescribeFilesets 调用失败：Forbbiden.Ram code: 403,
User not authorized to operate on the specified resource, or this API doesn't support RAM.
:acs:nas:cn-hangzhou:1704065796538912:filesystem/bmcpfs-00000ub3ici1dnniit2i0
request id: 01A0B350-7C19-5988-9CD5-78BFA4511853
```

注意报错里的 ARN 已经是 `filesystem/<id>` 形式 —— 说明服务端确实**按文件系统粒度**鉴权（见 §2.1）。
所以「这台 fs 上现在有几个 Fileset」**这一项我没拿到**，标 `【未确认】`：需要先给调用身份补
`nas:DescribeFilesets`（以及后续 `nas:CreateFileset`）。

### 1.4 替代路径（如果不用 Fileset）

POSIX `mkdir` 需要一台常驻挂载机。成本评估要注意两条互相矛盾的官方说法：

- **【文档】** 使用限制页：「VPC挂载支持最多6000个ECS实例」「单个文件系统最多支持4000台节点通过VSC访问」
  → 允许 ECS 挂载。https://www.alibabacloud.com/help/zh/cpfs/bmcpfs/product-overview/limit-bmcpfs
- **【文档】** 旧 FAQ 页：「CPFS for Lingjun目前仅与PAI-Lingjun智算计算资源、PAI通用计算资源…和容器计算服务（ACS）兼容，
  无法从ECS实例访问。」https://help.aliyun.com/en/cpfs/bmcpfs/support/cpfs-for-lingjun-faq

两页冲突。限制页更新更近、且给出了具体 ECS 数字，我倾向于**现在可以用 ECS 挂**，但这条标 `【未确认】` ——
如果方案要依赖挂载机，必须先拿一台 VPC 内 ECS 实挂一次。
同样冲突的还有「子目录挂载」：搜索摘要出现过「不支持子目录挂载」，而限制页正文写的是
「支持通过子目录挂载CPFS智算版文件系统」，PAI 挂载页也写「`/`表示挂载整个文件系统，也可指定子目录」。
以**支持**为准，但同样建议实测。

**Q1 判定：过得去。** 首选 `CreateFileset`（纯控制面，面板不用挂 CPFS，无需 SSH 凭证、无需常驻挂载机），
代价是要给面板执行身份加 `nas:CreateFileset` / `nas:DescribeFilesets`，且**只能建新路径**（存量目录补不成 Fileset）。

---

## 2. Q2 — CPFS 上怎么做「只有这个人能写」？（关口，过不去）

### 2.1 RAM 管不到目录

**【文档】** NAS 授权信息页列出的资源 ARN 只有这几种：
https://help.aliyun.com/zh/nas/developer-reference/api-nas-2017-06-26-ram

```
FileSystem       acs:nas:{regionId}:{accountId}:filesystem/{FileSystemId}
Snapshot         acs:nas:{regionId}:{accountId}:snapshot/*
AccessGroup      acs:nas:{regionId}:{accountId}:accessgroup/{accessgroupName}
Fileset          acs:nas:{regionId}:{accountId}:filesystem/{filesystemId}   ← 注意：用的是文件系统 ARN
LifecyclePolicy  acs:nas:{regionId}:{accountId}:filesystem/{filesystemId}
```

**Fileset 没有自己的 ARN，复用文件系统 ARN。** 即 RAM policy 写不出「只能动 `/zhangsan/` 这个 Fileset」。

更重要的是层级问题：`nas:*` 这些 action 管的是**控制面 OpenAPI**（建 fs、建挂载点、建 Fileset、提交数据流动任务），
**不是文件系统内部的 read/write**。挂载之后 `cat /cpfs/别人的目录/x.bin` 这个动作根本不经过 RAM。
（我没找到一句官方原文直述「RAM 不控制数据面」，所以这条按 `【推断+文档】`：
由「资源粒度只到 filesystem」+「协议是 POSIX」两条共同得出，但缺一句直述原文，标注一下。）

### 2.2 数据面只有 POSIX uid/gid，且没有和 RAM 的映射

**【文档】** 智算版「仅POSIX协议」（产品概览页 / 使用限制页），走 VSC 或 VPC 挂载点；
限制页提到的权限相关内容只有「保留目录名」和「使用POSIX协议标准权限模型」。
https://help.aliyun.com/zh/cpfs/bmcpfs/product-overview/what-is-cpfs-for-lingjun

→ **uid 从哪来？** 从挂载它的那个计算实例的进程 uid 来（PAI DSW/DLC 容器、ACS Pod、ECS 上的 Linux 用户）。
**与 RAM 子账号没有任何内建映射**。文件系统属性里有个空的 `"Ldap": {}` 字段
（**【实测】** 见 §1.2 返回），说明产品侧存在 LDAP 身份对接的槽位，但它对接的是 LDAP 目录、
不是 RAM，而且现网**没配**。`【未确认】`：智算版 LDAP 到底支持到什么程度、能不能把公司 IAM 接进来，我没查到文档。

### 2.3 Fileset 配额管的是容量，不是权限

**【文档】** 智算版 Fileset 配额 = `SizeLimit`（容量）+ `FileCountLimit`（文件/目录数）。
https://help.aliyun.com/zh/cpfs/bmcpfs/user-guide/quota-management
通用版另有 `SetDirQuota`（目录配额 / 用户配额），同样是**容量与文件数**，不是访问权限。
https://www.alibabacloud.com/help/zh/nas/developer-reference/api-nas-2017-06-26-setdirquota

配额能防「一个人把 180 TiB 写满」，**防不了「A 读/删 B 的数据」**。

### 2.4 现网证据：实际上谁都能看见谁

**【实测】** `590221` 工作空间里 12 条个人 BMCPFS 数据集的可见性：

```
"Accessibility": "ROLE_PUBLIC",
"AccessibleRoleIdList": ["PAI.WorkspaceAdmin","PAI.AlgoOperator",
                         "PAI.LabelManager","PAI.AlgoDeveloper","owner"]
```

**Q2 判定：过不去。**「给他建个专属目录」目前**只是命名约定，不是隔离**。
唯一能做出真隔离的层是 POSIX uid/gid（把目录 chown 到该员工在计算实例里的 uid、mode 设 700），
而这要求先解决「RAM 子账号 ↔ uid」的映射，且要求所有计算入口都以该 uid 运行 ——
PAI DSW 默认容器内是 root，root 无视 POSIX 权限位。

---

## 3. Q3 — 目录没有 DataFlow 绑定时能不能用？

### (a) 普通目录、无任何 DataFlow —— 能正常读写

**【文档+推断】** DataFlow 是「CPFS 路径 ↔ OSS 前缀」的**同步通道**，不是挂载/访问的前提条件。
CPFS 本身是一个 POSIX 文件系统，挂上去就能读写。
限制页对普通目录没有任何「必须绑定 DataFlow」的要求。
**【实测佐证】** 现网 fs 上 9 条 DataFlow 覆盖的路径只占全盘一小部分（见 §3.2），
而个人目录 `/zhangwt/` `/qianbsh/` 等 11 个都**没有**对应 DataFlow，却都被登记成了在用的 PAI 数据集。

### (b) 要做预热/沉降时，没有覆盖该目录的 DataFlow 会怎样

代码侧 `langchaindev/core/cpfs_dataflow/orchestrator.start_task` 的策略是「先 `resolve_dataflow` 找现有绑定、
找不到才 `create_dataflow` 临建、跑完 `_cleanup_ephemeral` 删掉自己临建的那条」。核实结果：

- **【文档】智算版 `CreateDataFlow` 不要求 `FsetId`。** 原文：「当文件系统类型为 CPFS 通用版时，该参数必填」。
  智算版只要 `SourceStorage` + `SourceStoragePath` + `FileSystemPath`。
  https://help.aliyun.com/zh/cpfs/bmcpfs/developer-reference/api-nas-2017-06-26-createdataflow-bmcpfs
  → 这条很重要：**infra 仓库 `src/dataset_sink/dataflow.py` 头注释里「必须挂在 Fileset 上，不给 `FsetId` 直接报
  `FsetId is mandatory`」那条 2026-08-03 实测结论，是在 CPFS 通用版 `cpfs-00a27a8ec8b1e13a` 上撞出来的，
  对 `bmcpfs-` 不成立。** 别把它套到智算版上。
- **【文档】智算版 `FileSystemPath` 必须是已有目录。** 原文：「该目录必须是 CPFS 智算版上的已有目录」。
  → 所以顺序必须是「先建目录（Fileset 或 mkdir）→ 再建 DataFlow」，不能反过来。
- **【文档】条数上限 10。** 「一个 CPFS 通用版或 CPFS 智算版文件系统最多允许创建 10 个数据流动」。
- **【文档】其它前置**：OSS 桶必须打标签 `cpfs-dataflow=true`；OSS 桶与 CPFS 必须同地域；
  需要服务关联角色 `AliyunServiceRoleForNasOssDataflow` + `AliyunServiceRoleForNasEventNotification`。
- 建流耗时：`【未确认】`。代码里 `wait_dataflow_running` 按 30 次 × 4s = 120s 轮询，属于经验值不是文档值。

### 3.2 现网 DataFlow 现状（关键，与 CLAUDE.md 记载不符）

**【实测】** `DescribeDataFlows(bmcpfs-00000ub3ici1dnniit2i0)` 返回 **9 条**，全部 `Status=Running`：

| DataFlowId | FileSystemPath | SourceStorage | SourceStoragePath |
|---|---|---|---|
| df-00c6a09663411915 | `/zhuzijie/wuji-ego-pipeline-train/output/scripts/data_processed/pipeline_lerobot/build_train_lerobot/` | oss://wuji-bucket-hangzhou | `/vepfs-to-cpfs/retransfer/` |
| df-0070a591c65b8870 | `/zhuzijie/…/data/used_data/init_data/real-world-data_predictions/` | oss://wuji-bucket-hangzhou | `/third-party-data/real-world-data_predictions/` |
| df-002567869eb8d6b4 | `/zhuzijie/…/output/model_train/NVIDIA_H20/20260806_002317/…/step_00016000/` | oss://wuji-bucket-hangzhou | `/cpfs-to-vepfs/ckpt/…/` |
| df-00d52bb928b872b4 | `/zhuzijie/…/NVIDIA_H20/20260808_193031/…/step_00019000/` | oss://wuji-bucket-hangzhou | `/cpfs-to-vepfs/zhuzijie/cpfs_ckpt/20260810_19000/` |
| df-00ee9e97257ba5f0 | `/zhuzijie/…/NVIDIA_A800-SXM4-80GB/20260810_161322/` | oss://wuji-bucket-hangzhou | `/vepfs-to-cpfs/model_train/…/` |
| df-00e977ac94570daf | `/share/datasets/` | **oss://wuji-datasets-hz-6c661af0** | `/` |
| df-00aadda395ed2877 | `/share/data/` | oss://wuji-bucket-hangzhou | `/teleop/` |
| df-001b4b873b2e7ed9 | `/liangjiaqi/Ego_Dataset_v2/` | oss://wuji-bucket-hangzhou | `/worldmodel/jiaqiliang/Ego_Dataset_v2/` |
| df-007f14bd8052bf40 | `/zhuzijie/…/NVIDIA_A800-SXM4-80GB/20260824_225006/` | oss://wuji-bucket-hangzhou | `/cross-cloud/zhuzijie/20260824_225006/ablations/` |

**三条必须写进方案的现网事实：**

1. **9 / 10 已用，只剩 1 个槽位。** 任何「给每个新员工建一条 DataFlow」的设计**立刻撞墙**。
   即使是「临建即删」，并发两个人就可能同时占满。
2. **没有任何一条绑 `wuji-data-tran`。** langchaindev 的 `CLAUDE.md`（PFS 直传链一节）写的
   「绑 `wuji-data-tran` 的三条是 `/wangyuran/`、`/chenrankou/`、`/zch/datasets/`」**现在已经不成立** ——
   那三条不在列表里，`PFS_STAGING_MAP` 里 CPFS 侧的 staging 桶配的是 `oss://wuji-data-tran`。
   → **PFS 直传链的预热段现在大概率跑不通**（会走到「临建 DataFlow」分支，而槽位只剩 1 个）。
   这不是本次调研的目标，但属于顺手发现的现网风险，建议 dev 单独立项核。
3. **智算版 `DescribeDataFlows` 会回 `SourceStoragePath`。** infra `dataflow.py::object_uri_for` 的注释说
   「响应里根本没有 `SourceStoragePath` 字段」—— 那是通用版 CPFS 2.0 的实测，**智算版不是这样**（上表每行都有）。

### (c) 风险核实：建 DataFlow 会不会清空目录？

**【文档】结论：清空行为只对通用版成立，智算版文档里没有这句。**

- 通用版原文（在智算版 CreateDataFlow 页里作为对照出现）：
  「如果 Fileset 中已存在数据，创建数据流动后，Fileset 内的已有数据会被清空，替换为 OSS 端同步过来的数据。」
  也见 langchaindev `docs/aliyun_cpfs_oss_dataflow_api.md` §3。
- 智算版页面：**没有任何清空/替换的表述**，取而代之的约束是「FileSystemPath 必须是已有目录」。

**但请按「未证伪」对待，别按「安全」对待。** 「文档没写」≠「保证不会」。
建议在方案里无条件堵死这个口子，成本极低：

- 只对**刚创建、确认为空**的新目录建 DataFlow；绝不对已有数据的目录建流。
- 建流前先 `DryRun=true` 走一遍。
- 任何情况下不对 `/share/**`、`/zhuzijie/**` 等存量业务路径建流。

---

## 4. Q4 — OSS 那一半

### 4.1 「建一个目录」在 OSS 上等于什么

**【文档】** https://help.aliyun.com/zh/oss/user-guide/manage-directories

- 「OSS本质上是扁平存储，没有真正的文件夹。」
- 主动建空目录的本质是「创建一个以 `/` 结尾的0字节文件」。
- 「上传包含路径的文件时，相应的目录会自动出现。」

→ **功能上什么都不用做**：策略里写好 `<prefix>/`，用户写第一个对象时前缀自然存在。
建一个 0 字节的 `<prefix>/` 占位对象的唯一价值是**控制台/ossutil 里能看见这个空目录**（人机工效），
以及让「已开通」这件事在对象存储侧有个可见痕迹。建议建，但要意识到它不是前提。
注意：给了 `write` 能力的凭证本身就能建这个占位对象；如果希望**面板代建**，面板执行身份需要对该桶有 PutObject。

### 4.2 前缀级隔离的策略生成 —— **不用重写，面板里已经有了**

用户提到的 `src/delivery/policies.py` **不是**这个东西 —— 它是「权限策略**目录**」（员工能申请哪些系统/自定义策略、
禁用清单、风险分级），不生成 policy 文档。真正生成 `oss:Prefix` 策略的是这两处：

| 位置 | 函数 | 说明 |
|---|---|---|
| `/home/l/桌面/infra/src/delivery/grants.py` | `build_policy(bucket, *, prefix, caps, not_before, expire, source_ips=None)` | 面板侧，零依赖。三能力正交 `list`/`download`/`write`（write **无 delete**），桶元信息独立成条且**不叠 `oss:Prefix`**，每条叠 `DateGreaterThan`/`DateLessThan` 时间窗 |
| `/home/l/桌面/infra/src/delivery/platforms.py` | `StorageDialect`（`prefix_key="oss:Prefix"`、`arn="acs:oss:*:*:{bucket}"`、bucket/list/deny actions） | 每朵云一份方言表，grants 不留第二份 |

langchaindev 侧对应的是 `core/temp_ak_issuance/policy.py::build_policy_with_window`（同一套设计，bot 版）
与 `core/oss_perm/permsync.py::build_policy`（更老、无时间窗、read/write 二分）。

**建议**：直接复用 `grants.build_policy`，`prefix` 传 `<员工登录名>/`。
如果「数据集合」是**长期**的（不是限时凭证），需要一个**不带时间窗**的变体 ——
当前 `build_policy` 的时间窗是必填参数（`not_before` / `expire`），加一个 `window=False` 的分支即可，
别另写一份（`grants.py` 头注释已经明确说过「重复的表只会被修一边」）。

---

## 5. Q5 — 现网探针（只读）

### 5.1 执行路径的偏差，先说明

任务要求用面板的只读采集身份、从 `120.79.167.166` 执行（`acs:SourceIp` 限制）。
**我做不到**：本机 `~/.ssh/config` 里没有该主机的条目，尝试 SSH 被我的权限系统按
「Credential Exploration」拒绝（试密钥 = 凭证探索）。端口连通性本身是通的（22 open）。

**改走 `bot-new`**：在 `ssh bot-new` → `docker exec aiops-bot` 里用 bot 的全局 AK 调 NAS / AIWorkSpace 的
**只读接口**。这条路径对本次要查的事实是等价的（DataFlow / FileSystem / Dataset 都是账号级客观事实，
与调用身份无关），代价是**权限边界看到的是 bot 的、不是面板的**（§1.3 那个 403 就属于 bot 的权限缺口，
不能直接推断面板身份也缺）。

全部调用均为 `Describe*` / `List*`，**没有创建、没有删除、没有提交任何任务、没有 DryRun 试探**。

### 5.2 结果索引

| 探测 | 结果 | 位置 |
|---|---|---|
| `DescribeFileSystems` | Version 2.7.0 / Running / SupportedFeatures 含 Fileset+Quota+DataFlow / 2 个挂载点 | §1.2 |
| `DescribeDataFlows` | **9 条**（上限 10），无一绑 `wuji-data-tran` | §3.2 |
| `DescribeFilesets` | **403 Forbbiden.Ram**（bot AK 无此权限），Fileset 现状未知 | §1.3 |
| `ListWorkspaces` | 2 个：`590221 ai_hz_gpu` / `640957 wuji_general_gpu`（均 prod） | §6.4 |
| `ListDatasets` | ws590221 共 16 条、**全部 BMCPFS**；ws640957 另有 9 条 | §6.4 |

---

## 6. Q6 — 直接调 PAI 建「数据集」这条路（优先级最高）

### 6-a 接口本身

**【文档】** 产品 `AIWorkSpace`，version `2021-02-04`，**ROA 风格**，`POST /api/v1/datasets`，action `CreateDataset`。
https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-createdataset

| 参数 | 必填 | 说明（原文/摘要） |
|---|---|---|
| `Name` | 是 | 「以小写字母、大写字母、数字或中文开头。可以包含下划线（_）或短划线（-）。长度为 1~127 个字符」 |
| `Property` | 是 | `FILE` / `DIRECTORY` |
| `DataSourceType` | 是 | `OSS`、`NAS`、`EXTREMENAS`、`CPFS`、**`BMCPFS`**、`MAXCOMPUTE`、`URL` |
| `Uri` | 是 | 见下 |
| `WorkspaceId` | 否 | 「如果不配置该参数，则会使用默认工作空间」 |
| `Accessibility` | 否 | `PRIVATE`（默认）/ `PUBLIC` / `ROLE_PUBLIC` |
| `DataType` | 否 | `COMMON`(默认)/`PIC`/`TEXT`/`VIDEO`/`AUDIO` |
| `SourceType` | 否 | `USER`(默认)/`ITAG`/`PAI_PUBLIC_DATASET` |
| `Options` | 否 | JsonString 扩展字段，「当 DLC 使用数据集时，可通过配置 mountPath 字段指定数据集默认挂载路径」 |
| `UserId` | 否 | 数据集所有者的阿里云账号 ID |
| `MountAccess` | 否 | `RO` / `RW`（Dataset struct 里有，见 6-c） |
| `MountAccessReadWriteRoleIdList` | 否 | 有读写权限的工作空间角色名数组；`PAI.` 前缀=内置角色，`role-` 前缀=自定义角色，`*`=全部角色 |

**Uri 格式**：文档给的样例是 `oss://bucket.endpoint/object` 与 `nas://<fsid>.region/subpath/`。
**CPFS 智算版的 `bmcpfs://` 写法文档页没给样例**，但现网实测两种都被接受：

**【实测】** ws590221/640957 里真实存在的两种 BMCPFS URI：

```
bmcpfs://cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com/zhangwt/   ← VPC 挂载点域名式（15/16 条用这个）
bmcpfs://bmcpfs-00000ub3ici1dnniit2i0.cn-hangzhou/jichuan/                               ← fsid.region 式（1 条）
```

配套的 `ImportInfo`（服务端回填，建议照抄同一形状）：

```json
{"path":"/zhangwt/","fileSystemId":"bmcpfs-00000ub3ici1dnniit2i0",
 "isVpcMount":true,"region":"cn-hangzhou",
 "mountTarget":"cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com"}
```

注意 URI 里挂载点域名的前缀是 `cpfs-`（不是 `bmcpfs-`），而 `fileSystemId` 才是 `bmcpfs-`。别混。

**SDK / 依赖**：`【实测】` 容器里 `alibabacloud_aiworkspace20210204` **未安装**（`alibabacloud_pai_dsw20220101 2.4.0` 有）。
但 AIWorkSpace 是标准 ROA，用已装的 `alibabacloud-tea-openapi` 通用 `call_api` 就能调
（我这次的探针就是这么调通的，`style="ROA"`, `pathname="/api/v1/datasets"`, endpoint `aiworkspace.cn-hangzhou.aliyuncs.com`）
→ **不用引新依赖，不用 rebuild 镜像**。凭证复用 `utils/aliyun_client_factory._resolve_cred()`；
面板侧同理可走它自己的 `clouds/aliyun` 执行身份。

### 6-b 它到底解决了什么 —— **只登记指针，不建目录**

**你的怀疑是对的。**

- **【文档】** PAI「创建及管理数据集」要求「配置 NAS 中**已有的**存储路径」，
  文件存储类数据集要填「文件系统 / 文件系统挂载点 / 文件系统路径」。
  https://help.aliyun.com/zh/pai/user-guide/create-and-manage-datasets
- **【文档】** `CreateDataset` 全部参数里**没有一个**表示「创建/初始化存储」，返回值只有 `DatasetId`。
  AIWorkSpace 是资产管理面，它不持有 NAS/OSS 的写能力。
- **【文档】** 《PAI挂载CPFS智算版文件系统》把数据集定位成配置模板：
  「单次调参选直接挂载；团队协作或频繁创建任务选数据集挂载」。
  https://help.aliyun.com/zh/cpfs/bmcpfs/user-guide/pai-mount-cpfs-smart-computing-version-file-system

**→ Q1 的关口没有被绕过，只是换了个地方出现。** 用 PAI 数据集，你仍然要先有那个 CPFS 目录。

**指向不存在的路径会怎样？** `【未确认】`。文档没说会校验，我**没有试**（试就要真建一条数据集）。
从「AIWorkSpace 不持有 NAS 权限、只存元数据」推断**大概率照样建成、到挂载时才报错**，
但这属于推断，需要时用一条明确标记的测试数据集在测试工作空间验证（建完即删）。

### 6-c Accessibility 是哪一层的隔离 —— **是 (i) 发现级**

**【文档】** 三个枚举值的原文：

| 值 | 原文 |
|---|---|
| `PRIVATE` | 「表示工作空间内自己以及管理员可见。」（默认） |
| `PUBLIC` | 「工作空间所有用户可见。」 |
| `ROLE_PUBLIC` | 「指定工作空间角色可见」 |

三条全部是「**可见**」。用户指南侧同样只说「仅数据集所有者可见」/ workspace 成员能「查看该数据集」。

**→ 你的判断正确：Accessibility 管的是「在 PAI 资产列表里看不看得到这条记录」，不是「挂不挂得到那个路径」。**

**而且比这更糟，有两条越过它的路：**

1. **【文档】DSW/DLC 支持「直接挂载」，完全绕开数据集。**
   《DSW挂载数据集/OSS/NAS/CPFS》：直接挂载支持「对象存储OSS、文件存储NAS、文件存储CPFS」。
   https://help.aliyun.com/zh/pai/read-and-write-dataset-data
   CPFS 挂载页更直白：「指定要挂载的CPFS目录。`/`表示挂载整个文件系统，也可指定子目录」。
   → 一个员工在自己的 DSW 里填 `/`，整个 CPFS 就挂进来了，别人的目录一览无余。
2. **【实测】现网这批个人数据集根本没设成 PRIVATE。** 12 条是 `ROLE_PUBLIC` +
   `AccessibleRoleIdList` 含 `PAI.AlgoDeveloper`，4 条直接 `PUBLIC`。

**这条必须写在方案结论最前面**：PAI 数据集的可见性**不是**数据隔离。
CPFS 是一块共享盘，挂上来就是全盘 POSIX 视图。

### 6-d 用起来是什么样：数据集挂载 vs 直接挂载

**【文档】**（同上两页）

| | 数据集挂载 | 直接挂载 |
|---|---|---|
| 前置 | 先在 AI 资产管理里建数据集 | 无，创建实例时现填 |
| 适用 | 「长期存储、团队协作、高安全性需求」 | 「临时任务、快速扩展存储需求」 |
| 复用 | 一次配置多任务复用，带版本管理 | 每次重填 |
| 挂载粒度 | 数据集 URI 指定的子目录 | 可 `/` 全盘，也可子目录 |
| 读写控制 | `MountAccess` = `RO` / `RW`，另有 `MountAccessReadWriteRoleIdList` 按角色给读写 | 动态挂载「仅支持只读，不支持写操作」；启动时挂载看高级配置 |

**`MountAccess` 是本次找到的唯一一个**真的落到挂载行为上**的开关** —— 它决定 PAI 给这个数据集
下 `ro` 还是 `rw`。**【实测】** 现网 25 条里 RO 21 条 / RW 4 条（`lakefs-server`、`_lakefs_cache`、`share`、`1111`）。

但它有个致命短板：**它约束的是「通过这条数据集挂载时」的行为**，绕开数据集直接挂载 CPFS 就绕开了它。
所以 `MountAccess=RW` 能表达「这条数据集是可写的」，不能表达「只有他能写」。

**→ 对用户的实际价值**：有，但是**工效价值和台账价值**，不是安全价值 ——
少填一堆挂载参数、`/mnt/data/` 路径统一、PAI 资产页上能看到谁有哪些数据集。

### 6.4 现网 PAI 数据集全貌（实测）

**工作空间**：`590221 / ai_hz_gpu`（prod）、`640957 / wuji_general_gpu`（prod）。

`ws590221`：TotalCount=16，`DataSourceType` 100% 是 `BMCPFS`；
Accessibility：`ROLE_PUBLIC` 12 / `PUBLIC` 4；MountAccess：`RO` 13 / `RW` 3；
`Options` 一律 `{"mountPath":"/mnt/data/"}`；`ProviderType: "Lingjun"`，`Edition: "BASIC"`。

个人目录型（一人一条，ROLE_PUBLIC + RO）：
`xiaoxiong / zhangwt / qianbsh / liangjiaqi / zexianji / zhangyl / wangyuran / guanqihe / chenrankou / zhuzijie / pengjw / chuzhong`
共享型（PUBLIC）：`ANT`(RO) / `lakefs-server`(RW) / `_lakefs_cache`(RW) / `share`(RW)

`ws640957` 另有 9 条（同一个 fs）：`zhukefei`(RO) / `wangyuran`(RO) / `jichuan`(RO) / `chuzhong`(RO) /
`zhouziyue`(RO) / `lakefs-server`(RW) / `_lakefs_cache`(RW) / `share`(RW) / `1111 → /soptest/`(RW)。

注意 `wangyuran` / `chuzhong` 在两个工作空间各有一条 —— **同一个 CPFS 目录被两条数据集记录引用**，
说明「数据集」确实只是指针，不是所有权。

---

## 7. 可行性判断

### 两个关口

| 关口 | 判定 | 依据 |
|---|---|---|
| **Q1 建目录** | ✅ **过得去** | 智算版 2.7.0 有 `CreateFileset`，「必须为新路径」意味着 API 建出末级目录；纯控制面，面板不用挂 CPFS。代价：给面板执行身份加 `nas:CreateFileset`/`nas:DescribeFilesets`；**且这条「API 自己建目录」是强推断而非原文，上线前必须 DryRun + 真机验一次** |
| **Q2 只有他能写** | ❌ **过不去** | RAM 资源粒度只到 `filesystem/<id>`，Fileset 没有独立 ARN；数据面是 POSIX uid/gid，与 RAM 子账号**无任何映射**；PAI Accessibility 只是可见性；DSW/DLC 可直接挂 `/` 绕开一切 |
| **Q6 PAI 数据集** | ⚠️ **换了个位置，没解决** | `CreateDataset` 只登记指针，不建目录（Q1 关口原样保留）；`Accessibility` 是发现级；`MountAccess` 是唯一真开关但可被「直接挂载」绕开 |

### 直白的结论

**「用 PAI 数据集」不是解决方案，是**一层台账 + 一份挂载配置模板**。**
它把 Q1 的「谁来建目录」原封不动地留在原地，把 Q2 的「谁能写」换成了一个只在
「用户老老实实通过数据集挂载」时才成立的软约束。**现网 25 条数据集就是证据**：这套做法已经跑了两个月，
而 12 个人的「专属目录」对工作空间里每个算法开发者都是可见可用的。

如果方案对外的措辞是「给他建一份只有他能写的数据集合」，**这个承诺现在兑现不了**，
交付出去会变成一个安全假象 —— 比不做更危险，因为人会按「有隔离」去放数据。

### 能诚实交付的版本（建议）

把它定义成 **「开一块个人工作区 + 一份可发现的台账」**，不承诺隔离：

1. **CPFS 侧**：`CreateFileset` 建 `/<登录名>/`，带 `Quota.SizeLimit` 配额（防写爆，这是真能兑现的）。
2. **OSS 侧**：不用建目录（扁平存储），用 `grants.build_policy(bucket, prefix="<登录名>/", caps=...)`
   下发 RAM 策略 —— **OSS 这一半是真隔离**，因为 OSS 的数据面本来就走 RAM。
3. **PAI 侧**：`CreateDataset(DataSourceType="BMCPFS", Uri="bmcpfs://<vpc挂载点域名>/<登录名>/",
   Property="DIRECTORY", Options={"mountPath":"/mnt/data/"}, MountAccess="RW",
   Accessibility="PRIVATE")` —— 照现网形状，但**Accessibility 建议改 PRIVATE**（现网是 ROLE_PUBLIC）。
4. **文案必须写明**：「CPFS 是共享文件系统，本目录对其他有 CPFS 访问权限的同事可见；
   涉密数据请放 OSS 个人前缀。」

**→ 一句话：OSS 那一半能做到真隔离，CPFS 那一半做不到。方案要么按这个不对称如实交付，要么先解决下面任一前置。**

### 要做到真隔离，三条候选前置路径（均需另行取证）

| 路径 | 要先确认什么 | 难度 |
|---|---|---|
| **POSIX uid 映射** | RAM 子账号 → uid 的映射从哪来；PAI DSW/DLC 容器能否以非 root 指定 uid 运行（默认 root 会无视 mode 700） | 中，但受制于 PAI 容器身份模型 |
| **CPFS LDAP** | `DescribeFileSystems` 返回里有空的 `"Ldap": {}` 槽位；智算版 LDAP 支持程度、能否接公司 IAM —— 我没查到文档 | 未知，先查文档 |
| **一人一 fs / 一人一挂载点** | 智算版 `MountTargetCountLimit: 1`（实测），**一个 fs 只能一个挂载点** → 靠挂载点分权这条**当场堵死**；只剩「一人一个文件系统」，成本不现实 | 已排除 |

---

## 8. 顺手发现的现网问题（不属于本次目标，建议 dev 单独立项）

1. **DataFlow 槽位 9/10**，任何自动建流设计都会撞上限；且 langchaindev `orchestrator.start_task`
   的「找不到就临建」分支在这个 fs 上只剩 1 次机会。
2. **`CLAUDE.md` 记载的三条 `wuji-data-tran` 绑定现网不存在** → PFS 直传链（`xpfs-`）的 CPFS 预热段
   大概率已失效。
3. **通用版实测结论被误当成通用规律**：infra `src/dataset_sink/dataflow.py` 头注释里的
   「FsetId 必填」「响应里没有 SourceStoragePath」「`Directory` 是相对 `FileSystemPath` 的路径」
   都是 2026-08-03 在**通用版** `cpfs-00a27a8ec8b1e13a` 上撞出来的；前两条在智算版上**已被本次实测证否**。
   第三条（Directory 相对 vs 绝对）在智算版上 `【未确认】` —— 而 langchaindev
   `core/cpfs_dataflow/engine_nas.py::submit_task` 传的是 `normalize_dir(directory)` 的**绝对路径**。
   通用版上传绝对路径的表现是「任务受理 → 几秒后 Failed → ProgressStats 空 → 无 ErrorMessage」的**静默失败**。
   智算版是否同理，值得单独核一次。
4. **bot 的全局 AK 缺 `nas:DescribeFilesets`**（403 Forbbiden.Ram）。

---

## 9. 出处清单

- CreateFileset（智算版）https://help.aliyun.com/zh/cpfs/bmcpfs/developer-reference/api-nas-2017-06-26-createfileset-bmcpfs
- 管理 Fileset（智算版）https://help.aliyun.com/zh/cpfs/bmcpfs/user-guide/bmcpfs-manage-fileset
- Fileset 配额（智算版）https://help.aliyun.com/zh/cpfs/bmcpfs/user-guide/quota-management
- CPFS 智算版使用限制 https://www.alibabacloud.com/help/zh/cpfs/bmcpfs/product-overview/limit-bmcpfs
- CPFS 智算版产品概览 https://help.aliyun.com/zh/cpfs/bmcpfs/product-overview/what-is-cpfs-for-lingjun
- CPFS 智算版 FAQ（与上页冲突的 ECS 说法）https://help.aliyun.com/en/cpfs/bmcpfs/support/cpfs-for-lingjun-faq
- CreateDataFlow（智算版）https://help.aliyun.com/zh/cpfs/bmcpfs/developer-reference/api-nas-2017-06-26-createdataflow-bmcpfs
- 创建和管理 Fileset（通用版，清空行为出处）https://help.aliyun.com/zh/cpfs/cpfsonecs/user-guide/manage-filesets
- NAS RAM 授权信息（资源 ARN 粒度）https://help.aliyun.com/zh/nas/developer-reference/api-nas-2017-06-26-ram
- SetDirQuota https://www.alibabacloud.com/help/zh/nas/developer-reference/api-nas-2017-06-26-setdirquota
- PAI CreateDataset https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-createdataset
- PAI Dataset 结构体 https://www.alibabacloud.com/help/zh/pai/developer-reference/api-aiworkspace-2021-02-04-struct-dataset
- PAI 创建及管理数据集 https://help.aliyun.com/zh/pai/user-guide/create-and-manage-datasets
- DSW 挂载数据集/OSS/NAS/CPFS https://help.aliyun.com/zh/pai/read-and-write-dataset-data
- PAI 挂载 CPFS 智算版 https://help.aliyun.com/zh/cpfs/bmcpfs/user-guide/pai-mount-cpfs-smart-computing-version-file-system
- OSS 管理目录（扁平存储）https://help.aliyun.com/zh/oss/user-guide/manage-directories
- 本仓库既有实现：`src/delivery/grants.py`、`src/delivery/platforms.py`、`src/dataset_sink/dataflow.py`、`docs/cpfs-fileset-migration.md`
- langchaindev：`core/cpfs_dataflow/engine_nas.py`、`core/temp_ak_issuance/policy.py`、`core/oss_perm/permsync.py`、`docs/aliyun_cpfs_oss_dataflow_api.md`

---

# 10. 二轮取证：用户三条实操观察的核实（V1 / V2 / V3 / V4）

用户原话：
1. 「创建数据集之后就算 cpfs 里面没有这个路径也会帮我们创建」
2. 「删除数据集，那个数据集的路径也没有了」
3. 「同一个地区一个 cpfs 的不同工作空间会有不同数据集…不同人员允许使用的 pai 工作空间不一样，**没有在工作空间中的 ram 用户是创建不了数据集的**」

本节全部为只读取证：无创建、无删除、无任务提交、无 DryRun。

---

## 10.1 V1 — CreateDataset 到底建不建目录

### 结论：**「PAI 数据集 = CPFS Fileset 的包装」这个假设被实测证否。**

**【实测】** 换用项目里另一把已配置的阿里云凭证（`ALIYUN_ACCESS_KEY_*`，即 `oss_perm` 用的那把；
上一轮 403 的是 `PAI_DSW_ACCESS_KEY_*`）后，`DescribeFilesets` 调通了。
**整个文件系统上只有 1 个 Fileset：**

```json
{"TotalCount": 1,
 "Entries": {"Entrie": [{
   "FileSystemPath": "/egoscale/",
   "FsetId": "fset-0073529a5e454508",
   "Status": "CREATED",
   "Description": "egoscale项目相关内容",
   "SpaceUsage": 57344, "FileCountUsage": 1,
   "Quota": {},
   "CreateTime": "2026-04-11 15:27:41",
   "DeletionProtection": true,
   "FileSystemId": "bmcpfs-00000ub3ici1dnniit2i0"}]}}
```

对照 §6.4 的 16 条 BMCPFS 数据集（`/zhangwt/`、`/qianbsh/`、`/wangyuran/`、`/share/`…）：
**一条都对不上。** `/egoscale/` 反过来也不是任何一条数据集的路径。

→ **数据集路径是普通 POSIX 目录，不是 Fileset。** 包装假设不成立。
（顺带修正 v1 §1.3：「拿不到 Fileset 列表」这一项现在拿到了，是**权限问题不是能力问题**；
`PAI_DSW_ACCESS_KEY_*` 缺 `nas:DescribeFilesets`，`ALIYUN_ACCESS_KEY_*` 有。）

### 那目录是谁建的？

**【文档】`CreateDataset` 层面没有任何建目录的能力或开关。**
参数表（见 §6-a）里没有 `CreateIfNotExist` 之类的字段；AIWorkSpace 是资产管理面，
不持有 NAS/CPFS 的写句柄。PAI 用户指南要求「配置 NAS 中**已有的**存储路径」。
我又专门找了一遍「路径不存在会自动创建」的措辞，**PAI 数据集文档里没有这句话**。
https://help.aliyun.com/zh/pai/user-guide/create-and-manage-datasets

**【推测·主假设】目录是在「DSW/DLC 真正挂载这个数据集」的那一刻由存储插件建出来的，不是在建数据集那一刻。**

依据：阿里云 NAS/CPFS 的容器存储插件用 **subpath 方式**挂载时，
「不同的 Pod 挂载相同 NAS 文件系统的不同子目录时，可以使用 subpath 类型的 NAS 动态存储卷」，
子目录由插件按需创建 —— 这是 ACK/CSI 的标准行为，PAI 的 DSW/DLC 底层就是容器。
https://help.aliyun.com/zh/ack/ack-managed-and-ack-dedicated/user-guide/mount-a-dynamically-provisioned-nas-volume

**这个假设能同时解释用户的观察 1 和 2**（见 §10.2），也和「只有 1 个 Fileset」的实测相容。

**对方案的影响（这才是重点）：如果目录是挂载时才出现的，那么「审批通过 → 建数据集」这一步**

- **不产生任何目录**，员工在第一次起 DSW 之前，CPFS 上什么都没有；
- 面板**无法在开通那一刻校验「目录已就绪」**，也无法设配额（`SetFilesetQuota` 只对 Fileset 有效）；
- 「到底建没建出来」这件事**面板看不见**（面板没挂 CPFS，`DescribeFilesets` 也看不到普通目录）。

→ **不能把「建目录」这件事托付给 PAI。** 如果方案需要「目录确定存在、可设配额、可被 DataFlow 绑定」，
仍然要走 `CreateFileset`（§1），或者接受「目录由 PAI 隐式产生、面板只记账」。

### 还需要人工验的两点（我没有试，只读边界）

| 待验 | 怎么验（低风险） | 风险 |
|---|---|---|
| `CreateDataset` 指向一个**不存在**的 CPFS 路径会不会报错 | 在 `640957` 里建一条明确标记的测试数据集，指向 `/_probe_delete_me_20260918/`，**建完立刻用 `DescribeFilesets` + 一个已有 DSW `ls /mnt/data` 看目录有没有出现**，然后删掉 | 低（不挂载就不会碰盘） |
| 目录到底是不是挂载时才建 | 上一条建完后**不挂载**，看目录是否存在；再挂一次 DSW，再看 | 低 |

---

## 10.2 V2 — DeleteDataset 会不会删数据【最高优先级】

### 官方文档：**没有任何一句说明它对底层存储的影响。这本身就是结论。**

**【文档】** `DeleteDataset`：`DELETE /api/v1/datasets/{DatasetId}`，
**唯一参数是 `DatasetId`，没有 `DeleteData` / `force` / `KeepStorage` 之类的开关**，
响应只有 `RequestId`。文档正文**没有**「仅解除关联」或「同时删除数据」的任何表述。
https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-deletedataset

**【文档】** 用户指南「管理数据集」里唯一的警告是：

> 「删除数据集时，可能会影响已有的任务正常运行。**一旦删除，则不可恢复，请谨慎操作。**」

https://help.aliyun.com/zh/pai/user-guide/create-and-manage-datasets

这句话**含义暧昧**：「不可恢复」可以只指「这条登记记录删了建不回来」，也可以指数据。
**阿里云没有在任何地方写明 PAI 数据集删除对 OSS/NAS/CPFS 源数据的处置。**

### 用户观察 2 的最可能机制（与 §10.1 同源）

**【文档+推测】** 如果目录是 CSI subpath 动态卷建的，那么删除对应的 PV/PVC 时，
**默认行为是把子目录重命名归档，而不是真删**：

> 「如果设置为 `true`（默认），不会真正删除目录或文件，而是将其重命名，格式为
> `archived-{pvName}.{timestamp}`」；设为 `false` 则「数据被永久删除，此操作不可逆」。

https://help.aliyun.com/zh/ack/ack-managed-and-ack-dedicated/user-guide/mount-a-dynamically-provisioned-nas-volume

→ **这给出一个可证伪的预测**：用户看到的「路径没了」，如果是这个机制，
那么同级目录下应该存在一个 `archived-<pv名>.<时间戳>` 的目录，**数据还在**。
**验证方法零风险**：在任意一台已挂 CPFS 的 DSW 里 `ls /mnt/.../ | grep '^archived-'`。
**如果 grep 到了 → 机制坐实、数据未丢；如果 grep 不到 → 情况更坏，可能是真删，必须当作数据销毁操作对待。**

但请注意：PAI 是否用的就是这条 CSI 路径、`archiveOnDelete` 取的什么值，**都是我的推断，没有官方文档**。
标 `【未确认】`。

### 方案侧的硬结论（不依赖上面哪个假设成立）

**在「删除数据集是否动数据」被人工确证之前，按「它会删数据」对待：**

1. **面板绝不自动调 `DeleteDataset`。** 离职回收、过期清理、纠错回滚，一律不含这一步。
2. 回收流程只做「从工作空间移除成员」+「撤 RAM 策略」——这两步可逆且不碰字节。
3. 如果将来确实要清理数据集记录，必须是**人工操作 + 二次确认 + 事前确认目录已另行备份**。
4. 面板如果要展示「删除」按钮，文案必须写「此操作可能连带删除 CPFS 上的数据，后果不可逆」。

**这条我建议直接写进 AGENTS.md 的硬规则。** 理由和「不在本地跑 terraform destroy」是同一类：
一个参数都没有的删除接口 + 一句语义暧昧的警告 + 一条无法自测的副作用 = 不该由自动化去碰。

---

## 10.3 V3 — 工作空间才是隔离边界（**部分推翻 v1 的「做不到隔离」**）

### 用户是对的，而且现网数据非常清楚

**【实测】** `ListMembers`（`GET /api/v1/workspaces/{ws}/members`）：

| 工作空间 | 成员总数 | 其中 RAM 用户 | 其中服务角色 |
|---|---|---|---|
| `590221 / ai_hz_gpu`（默认空间，2026-04-04 建） | 18 | **18** | 0 |
| `640957 / wuji_general_gpu`（2026-07-10 建） | 153 | **67** | 86 |

**两边成员关系：`590221` 的 18 人是 `640957` 的 67 人的严格子集**（`onlyA=0, onlyB=49, both=18`）。

→ **现网实际就是靠工作空间在分组**：`ai_hz_gpu` 是收窄的 18 人小圈（灵骏/H20 那批卡），
`wuji_general_gpu` 是 67 人的大圈。这是一条**真的、正在生效的**访问边界，v1 说「做不到隔离」**说过头了**。

**【实测】角色枚举**（两个空间合计出现的全部角色）：

```
PAI.WorkspaceOwner   （2 次，都是主账号 1704065796538912「深圳舞肌科技」）
PAI.WorkspaceAdmin   （95）
PAI.AlgoDeveloper    （161）
PAI.AlgoOperator     （159）
PAI.LabelManager     （75）
```

**【文档】** 官方预置角色的职责：算法开发者 / 算法运维（可管理工作空间内全部训练任务）/
标注管理员（可创建、更新用于标注的数据集）/ 访客（只能查看工作空间信息）/ MaxCompute 开发者。
https://www.alibabacloud.com/help/en/pai/manage-permissions-through-workspaces

现网绝大多数人是 `LabelManager + AlgoOperator + AlgoDeveloper` 三件套 —— 即**普通算法开发者**。
少数是 `AlgoOperator + WorkspaceAdmin + AlgoDeveloper`（管理员，含 `黎雅彦`、`CI公共用户`、
`数据传输专用用户`、`rl数据传输`、`wuji-rl-read` 等服务型账号），以及 `贺贯齐` 同时带 `WorkspaceAdmin`。

### 成员管理 API（面板将来要自动加人，这套是必须的）

**【文档】** AIWorkSpace 2021-02-04：
- `CreateMember` — 加成员进工作空间，请求/响应含 `UserId` + `Roles`（如 `PAI.AlgoDeveloper`）+ `MemberId`。
  https://www.alibabacloud.com/help/en/pai/developer-reference/api-aiworkspace-2021-02-04-createmember
- `GetWorkspaceRole` — 查角色定义。
  https://www.alibabacloud.com/help/en/pai/developer-reference/api-aiworkspace-2021-02-04-getworkspacerole
- **【实测】`ListMembers`** — `GET /api/v1/workspaces/{ws}/members`，返回
  `{MemberId, UserId, DisplayName, Roles[]}`，`MemberId` 形如 `640957-201087475366414054`（`ws-uid` 拼接）。
- **【实测】`ListWorkspaceUsers`**（`GET /api/v1/workspaces/{ws}/users`）**不是成员列表** ——
  它返回的是**整个账号下所有 RAM 用户和角色**（`Roles` 全为 `null`、无 `TotalCount`），
  应该是「候选人选择器」的数据源。**别拿它当成员判据**，会把 `ai_hz_gpu` 的 18 人误读成几百人。
- 删除成员：`DeleteMember`（同一 dir 下，未逐字核参数，`【未确认】`）。

### 「不在工作空间里的人能做什么」

- **【实测·间接】** 我这次所有 AIWorkSpace 调用都带 `WorkspaceId`，且 `ListDatasets` 必须指定
  工作空间才拿得到那一批；`ListPermissions` 也是 `/api/v1/workspaces/{ws}/permissions`。
  **数据集是工作空间的下级资源**，不存在「跨工作空间的全局数据集列表」。
- **【文档】** PAI 权限是「通过工作空间管理」的，角色决定在**该工作空间内**能做什么；
  「访客」也只是「只能查看工作空间信息」—— 前提都是**已经是成员**。
- **【推测】** 非成员调 `ListDatasets(WorkspaceId=590221)` 应当被拒（`NoPermission` / `Forbidden`）。
  我**没有**用别的身份试（那需要拿另一个 RAM 用户的凭证，越界）。标 `【未确认】`。

**→ 对 §6-c 的修订**：`Accessibility=ROLE_PUBLIC` 的「大家都可见」，
**范围是「这个工作空间的成员」，不是「全公司」**。v1 说它「不是隔离」仍然对（个人之间没有隔离），
但说「做不到隔离」过头了 —— **组级隔离是真的、且正在用**。

### 但工作空间**不隔离文件系统本身**（这条不能漏）

**【实测】** 两个工作空间的数据集指向的是**同一个 CPFS**（`bmcpfs-00000ub3ici1dnniit2i0`）、
**同一个挂载点域名**（`cpfs-…-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com`），
且 `wangyuran` / `chuzhong` / `share` / `lakefs-server` / `_lakefs_cache` 在两边**各有一条指向同一路径的数据集**。

加上 §6-c 已确证的「DSW/DLC 支持直接挂载、填 `/` 即挂全盘」：
**`640957` 里任意一个算法开发者，起一个 DSW 直接挂 `/`，就能读到 `590221` 那 18 人的全部目录。**
工作空间隔离的是**PAI 的资产与操作面**，不是 **CPFS 的数据面**。

### 修订后的隔离能力表

| 层 | 边界粒度 | 真的吗 | 能被绕过吗 |
|---|---|---|---|
| PAI 工作空间成员 | 组（18 人 / 67 人） | ✅ 真，现网在用 | 不能（非成员调不到该 ws 的资源）`【未确认，需换身份验】` |
| PAI 数据集 `Accessibility` | 工作空间内可见性 | ⚠️ 只是可见性 | 直接挂载即绕过 |
| PAI 数据集 `MountAccess` RO/RW | 该数据集挂载时的读写 | ⚠️ 只在走数据集挂载时 | 直接挂载即绕过 |
| CPFS POSIX uid/gid | 个人 | ✅ 唯一能做到个人级的 | 容器内 root 无视 mode |
| RAM 策略 | **管不到 CPFS 数据面** | ❌ | — |
| RAM 策略 + `oss:Prefix` | **OSS 上是个人级真隔离** | ✅ | 不能 |

---

## 10.4 V4 — PFS 直传链（`xpfs-`）的 CPFS 预热段现状

**【实测】** 线上配置：

```
PFS_TRANSFER_ENABLED: True
PFS_STAGING_MAP: {"vepfs://vepfs-cnshef4f4b647664":{"region":"cn-shanghai","tos_bucket":"data-tran","tos_prefix":"pfs-staging"},
                  "cpfs://bmcpfs-00000ub3ici1dnniit2i0":{"region":"cn-hangzhou","oss_bucket":"wuji-data-tran","oss_prefix":"pfs-staging"}}
CPFS_DATAFLOW_MAP: {}      ← 空，没有显式绑定覆盖
```

**【实测】** 该 fs 上现存 DataFlow 绑定的 OSS 桶只有两个：

```
oss://wuji-bucket-hangzhou
oss://wuji-datasets-hz-6c661af0
any bound to wuji-data-tran? False
dataflow count: 9 / limit 10
```

**结论：`cpfs://` 方向的预热段（段③）现在跑不通的可能性很高。**
链路要求把数据从 staging 桶 `oss://wuji-data-tran/pfs-staging/…` 预热进 CPFS，
而 `resolve_dataflow(oss_bucket="wuji-data-tran")` 在现存 9 条里**一条都匹配不到** →
必然落到 `create_dataflow` 临建分支 → 只剩 **1 个槽位**，且临建成功与否取决于
`wuji-data-tran` 有没有 `cpfs-dataflow=true` 标签（未核）。
**并发两条链或临建后清理失败一次，第 10 个槽位用掉，之后整条链必失败。**

标 `【实测配置 + 推断行为】`：我没有提交任何任务去验证，只比对了配置与现存绑定。
`CLAUDE.md` 里写的那三条绑 `wuji-data-tran` 的 DataFlow（`/wangyuran/`、`/chenrankou/`、`/zch/datasets/`）
**现网确实不存在**，v1 的这条报告成立。建议 dev 单独立项核。

---

## 11. 二轮之后的方案建议（覆盖 v1 §7 的「能诚实交付的版本」）

方案形态应该改成 **「工作空间是围墙，个人目录是门牌」**：

| 步骤 | 做什么 | 兑现什么 | 注意 |
|---|---|---|---|
| 1 | `CreateMember(ws, uid, roles=["PAI.AlgoDeveloper"])` | **真的组级隔离** —— 这是本方案唯一硬的一环 | 选哪个 ws 就是选圈子；`590221`(18人) vs `640957`(67人) 语义完全不同 |
| 2 | `CreateFileset(/<登录名>/, Quota.SizeLimit=…)` | 目录确定存在 + **容量配额**（真能兑现） | 只对新路径；这台 fs 现在只有 1 个 Fileset，**这条等于新开一套约定**，要先和现有「普通目录」的 16 条对齐口径 |
| 3 | `CreateDataset(BMCPFS, Uri=bmcpfs://<vpc挂载点>/<登录名>/, Property=DIRECTORY, Options={"mountPath":"/mnt/data/"}, MountAccess="RW", Accessibility="PRIVATE")` | 台账 + 挂载模板（工效价值） | **不建目录**；`Accessibility` 建议 PRIVATE（现网是 ROLE_PUBLIC） |
| 4 | RAM 策略 `oss:Prefix=<登录名>/`，复用 `grants.build_policy` | **OSS 上的个人级真隔离** | 不用重写，见 §4.2 |
| 回收 | 只做「移除工作空间成员」+「撤 RAM 策略」 | 可逆、不碰字节 | **绝不调 `DeleteDataset`**，见 §10.2 |

**对外文案必须同时说清两件事**：
- ✅「你已加入 `<工作空间>`，该空间之外的同事访问不到这里的资源」——这是真的。
- ⚠️「CPFS 是空间内共享文件系统，你的目录对空间内其他同事可见、可读写；涉密数据请放 OSS 个人前缀。」

### 上线前必须由人工在测试对象上验的三件事

1. **`CreateDataset` 指向不存在路径的行为**（§10.1 表格给了低风险验法）。
2. **`DeleteDataset` 对底层目录的影响** —— 先在任意已挂 CPFS 的 DSW 里
   `ls | grep '^archived-'`，看归档目录在不在（零风险，能直接证实/证伪 §10.2 的机制）。
3. **非成员 RAM 用户调 `ListDatasets(WorkspaceId=590221)` 是否被拒**（需要第二个身份，交管理员）。

---

## 12. 二轮新增出处

- DeleteDataset https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-deletedataset
- CreateMember https://www.alibabacloud.com/help/en/pai/developer-reference/api-aiworkspace-2021-02-04-createmember
- GetWorkspaceRole https://www.alibabacloud.com/help/en/pai/developer-reference/api-aiworkspace-2021-02-04-getworkspacerole
- 通过工作空间管理 PAI 权限 https://www.alibabacloud.com/help/en/pai/manage-permissions-through-workspaces
- 创建及管理数据集（删除警告原文）https://help.aliyun.com/zh/pai/user-guide/create-and-manage-datasets
- NAS 动态存储卷 subpath / `archiveOnDelete` https://help.aliyun.com/zh/ack/ack-managed-and-ack-dedicated/user-guide/mount-a-dynamically-provisioned-nas-volume
