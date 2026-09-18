# 火山引擎有没有「PAI 数据集」的对应物 —— 取证报告

> 对照基准：`docs/collab/research/cpfs-oss-dataset-provisioning.md`（阿里侧：CPFS 目录 + OSS 前缀 + PAI `CreateDataset` 登记 + 面板按 `UserId` 采台账）。
> 结论标记：**【文档】**=官方文档明说（附 URL）／**【实测】**=本次真机只读探针所得（附方法与原文片段）／**【推测】**=未证实。
> 本次全部探针均为只读（List/Get/Describe），未创建、未删除、未提交任务、未 DryRun。

---

## 0. 先看这四条

1. **火山没有「数据集」这个一等公民对象。** veMLP 的公开 OpenAPI 只有 `ml_platform` / `2024-07-01` 这**一个**版本，
   里面**没有任何 Dataset 操作**（`ListDatasets` 实测 404 `InvalidActionOrVersion`，而同版本 `ListResourceQueues` 200）。
   IAM 系统策略里确实出现过 `ml_platform:ListDatasets` 这个 action 名，说明**平台内部有过这个模块**，但它没有对外暴露 API。
   → **PAI 数据集那一层，在火山上做不出来。面板只能做「目录 + 自己的台账」。**
2. **训练任务直接填存储地址。** 实测线上 job / 开发机的 `StorageConfig.Storages[]` 形如
   `{Type: "Tos", MountPath: "/tos", Config.Tos:{Bucket, Prefix}}` 与 `{Type: "Vepfs", MountPath: "/wuji-vefps", Config.Vepfs:{Id, SubPath}}` ——
   没有中间对象，没有属主字段。属主信息只在 job 本身的 `CreatedBy`（`trn:iam::2111674479:user/ChaoguiHuang`）上。
3. **权限结构和阿里同构，甚至更糟。** `ServiceRoleForMLPlatform`（服务关联角色）挂的 `ServiceRolePolicyForMLPlatform` 里
   `tos:PutObject/GetObject/DeleteObject/ListBucket/...` 全部 `Resource: ["*"]`、**无 Condition** ——
   和 `AliyunPAIDSWDefaultRole` 一模一样的形状。**「按人收 TOS 前缀」在 veMLP 里管不住。**
   更糟的是现网 `舞肌算法组` 这个 IAM 用户组直接挂着 `TOSFullAccess` + `vePFSFullAccess`，收前缀在**组策略层面**就已经无意义。
4. **但火山有一件阿里没有的东西：vePFS Fileset 挂载权限。**
   `ml_platform:CreateVepfsFilesetPermission` 能把某个 vePFS Fileset 目录授权给**指定子账号 / 用户组 / 队列**，分**只读 / 读写**。
   这正是阿里 CPFS 卡死的那道关口（阿里侧只有 POSIX uid/gid、与 RAM 无映射）。
   **代价**：这个 API **只有 Create 和 Delete，没有任何 List/Get**（实测逐个 action 名试过，见 §4.3）——
   面板无法回读「现在谁有哪块目录的权限」，只能自己记账，且与云上真实状态**无法对账**。

---

## 1. 调用方式（先说怎么调，别重复造轮子）

**【实测】** 容器 `aiops-bot` 里已装 `volcengine-python-sdk 5.0.41`，其中包含子包 `volcenginesdkmlplatform20240701`
（与 `core/transfer/engine_tos.py` 用的 `volcenginesdkdms`、`core/vepfs_dataflow/engine_vepfs.py` 用的 `volcenginesdkvepfs` 同一个 monolith wheel）。
→ **veMLP 不需要新依赖、不需要 rebuild 镜像。**

- service code：`ml_platform`（注意有下划线，不是 `mlplatform`）
- version：`2024-07-01`（**实测只有这一个版本可用**，见 §2.2）
- 传输：**POST + JSON body**，`Action`/`Version` 仍在 query string 里
- 签名：与 `src/delivery/clouds/volcano.py` 的 `sign()` 完全一致（scope 结尾 `request`、四层 HMAC、secret 不加前缀）
- region：生产资源在 **`cn-shanghai`**（签名 region 要跟着写 `cn-shanghai`）

面板侧**可以直接复用 `clouds/volcano.call()`**：它已经支持 `body=` → 走 POST JSON 的分支
（`volcano.py:139` 注释「`body` 不为空时发 POST JSON（资源中心这类新接口）」）。本次探针就是这么打通的。

凭证：`Credentials.from_env()` 的 `TOS_ACCESS_KEY` / `TOS_SECRET_KEY` 回退路径直接可用，实测这把 AK 能读 veMLP / vePFS / IAM。

### 1.1 一个复用的小技巧：404 / 405 判别法

**【实测】** 对 `2024-07-01` 用 **GET** 发请求：
- action 存在 → `HTTP 405 MethodNotAllowed`（因为该版本只收 POST）
- action 不存在 → `HTTP 404`，`Code: InvalidActionOrVersion`，`Message: Could not find operation <X> for version 2024-07-01`

这给了一条**完全只读、不产生任何副作用**的「某个 action 到底存不存在」探测手段——连写操作的名字都能安全试探
（GET 打过去只会被 405 挡住，不会执行）。本报告里所有「某 action 不存在」的结论都由它得出。

---

## 2. Q1 —— 火山有没有「数据集」这个一等公民对象？

### 2.1 答案：没有对外可用的。

**【实测】SDK 面**：`volcenginesdkmlplatform20240701/models/` 下全部 `*_request.py` 共 74 个 action，
**没有一个带 Dataset**。完整清单里与数据相关的只有存储挂载和 vePFS 权限两类：

```
create_deployment / create_dev_instance / create_job / create_resource_group / create_resource_queue /
create_resource_reservation_plan / create_service / create_tele_op_task /
create_vepfs_fileset_permission / delete_vepfs_user_permission /
get_job / list_jobs / list_dev_instances / list_resource_queues / list_resource_groups / ...
```

**【实测】线上 API 面**（比 SDK 更权威，SDK 可能滞后）：

```
POST ml_platform/2024-07-01 ListDatasets      -> HTTP 404 InvalidActionOrVersion
                                                 "Could not find operation ListDatasets for version 2024-07-01"
POST ml_platform/2024-07-01 ListResourceQueues -> HTTP 200  Result{Items,TotalCount,PageNumber,PageSize}   ← 对照组
```

同样 404 的还有：`ListDataset` / `ListDataSets` / `ListCustomDatasets` / `ListAnnotationDatasets` /
`GetDataset` / `ListAssets` / `ListModels` / `ListImages`。

**【实测】版本面**：拿已知存在的 `ListResourceQueues` 遍历 12 个候选版本串，只有 `2024-07-01` 回 405（=版本有效），
其余 `2021-01-01 / 2021-07-01 / 2021-08-01 / 2022-01-01 / 2022-07-01 / 2023-01-01 / 2023-07-01 / 2024-01-01 / 2025-01-01 / 2025-07-01 / 2020-12-01` 全部 404。
→ **不存在「老版本 API 里有数据集」这条退路**（至少在 `open.volcengineapi.com` + service `ml_platform` 这个组合下）。

**【文档】产品面**：veMLP《功能总览》列出的模块是
资源组管理 / 队列 / 实例 / 镜像仓库 / 开发机 / 自定义训练 / 模型管理 / 在线服务 / SDK·命令行·OpenAPI —— **没有数据集模块**。
https://docs.volcengine.com/docs/6459/72379
《常用概念》里数据与存储一节只有 TOS / CloudFS / vePFS / NAS，**没有「数据集」「项目」「工作空间」**。
https://docs.volcengine.com/docs/6459/76491

### 2.2 但要诚实说明一个矛盾点

**【实测】** IAM 系统策略 `ServiceRolePolicyForMLPlatform` 的第 13 条语句里，**逐字出现**这些 action：

```json
{"Effect":"Allow","Resource":["*"],
 "Action":["ml_platform:ListAnnotationSetsV2","ml_platform:CreateAnnotationSet",
           "ml_platform:ListDatasets","ml_platform:ListResourceQueues",
           "ml_platform:ListFlavorsV2","ml_platform:CreateCustomTask","ml_platform:*"]}
```

系统策略 `MLPlatformDeveloperAccess` 里也有通配段 `ml_platform:*Dataset*`、`ml_platform:*Annotation*`、
`ml_platform:*Asset*`、`ml_platform:*Pipeline*`、`ml_platform:*Tracking*`。

**解释【推测】**：veMLP 1.0 时代（数据托管 / 标注 / 流水线 / 实验管理）确实有「数据集」模块，
IAM 的 action 命名空间保留了下来，但 **2.0 的公开 OpenAPI 没有承接它**，现在应当是控制台内部接口 / 已下线。
**没有证实的办法**（探不到任何可用版本），所以标【推测】。

**对方案的影响是零**：不管它内部是否还活着，**面板调不到它**，就不能用它做台账。

### 2.3 那火山上训练任务怎么挂数据？——直接填地址

**【实测】** 真实 job `t-20260916210341-tjbcn`（`GetJob`）的 `StorageConfig`：

```
storages:
  - type: Tos    mount_path: /tos
    config.tos:   {bucket: 'ml-platform-auto-created-required-2111674479-cn-shanghai', prefix: '/'}
  - type: Vepfs  mount_path: /wuji-vefps   read_only: False
    config.vepfs: {id: 'vepfs-cnsh4bb0c73b50ae', file_system_name: 'wuji-vefps-D',
                   host_path: '/mnt/vepfs-cnsh4bb0c73b50ae', sub_path: ''}
```

**【实测】** 真实开发机 `di-20260916205153-69fvs`：

```
Tos   /tos                    bucket=ml-platform-auto-created-required-2111674479-cn-shanghai prefix=/
Tos   /tos-wuji-dc-shanghai   bucket=wuji-dc-shanghai                                         prefix=/
Vepfs /wuji-vefps/wuji-il     id=vepfs-cnsh4bb0c73b50ae sub_path=wuji-il  read_only=False
```

**【实测】SDK 模型**（`CreateJobRequest` → `StorageConfigForCreateJobInput`）：

```
StorageConfig = {credential, sidecar_memory_ratio, storages[]}
Storage       = {type, mount_path, read_only, config}
Config        = {tos, tos_ap, vepfs, vepfs_ap, nas, nas_ap, cfs, efs, efs_ap, sfcs}   ← 十选一的 union
Tos           = {bucket, prefix}
Vepfs         = {id, file_system_name, host_path, sub_path}
Nas           = {id, addr, file_system_name, nas_type, sub_path}
TosAP         = {access_point_id, access_point_name, accelerator_id, accelerator_name, region, server}
VepfsAP       = {access_point_id, id, use_eic}
```

→ **挂载点 = 桶名+前缀 / 文件系统 ID+子路径。没有属主，没有可见范围，没有标签，没有 ID。**
一条挂载配置是 job 的一个字段，不是一个能被 List 出来的资源。

### 2.4 顺带排除的两个「像但不是」的东西

- **火山方舟 Ark 的 Dataset**：`volcenginesdkark` 里有 `dataset_for_create_model_customization_job_input` 等 ——
  那是**大模型精调任务的入参子结构**，不是独立资源，且绑的是方舟而不是 veMLP。**不等价。**
- **AI 数据湖服务 LAS（docs 6492）**：有「数据集」概念，基于 Gravitino catalog，能对接 TOS。
  **【实测】** 现网 IAM 组 `舞肌产品组` / `舞肌算法组` 已挂 `LASFullAccess` / `LASAIFullAccess`，桶里也有 `las-datastore`，说明**已开通**。
  **【未确认】** 它是不是「一个指向 TOS/vePFS 路径、带属主、能在 veMLP 训练任务里挂载」的对象——
  LAS 是独立产品、数据面走 LAS-SDK / fsspec，**不是 veMLP 的挂载源**（veMLP 的 Config union 里没有 LAS 项，见 §2.3）。
  要下判断需要单独一轮取证（LAS 的 service code / version / dataset API / 是否收费）。**本轮不下结论。**

---

## 3. Q2 —— 火山的「工作空间」对应物

火山把阿里 PAI 工作空间的职能**拆成了两层**，两层都不是 veMLP 自己的对象：

### 3.1 第一层：IAM 项目（Project）—— 账号级资源分组

**【文档】** 《项目和标签的关系》：「项目是若干资源形成的一个资源集合，**支持项目管理的资源只能加入到唯一项目中**」，
「支持按照 IAM 身份（用户、用户组、角色）授予项目权限」。
https://docs.volcengine.com/docs/6649/94340

**【实测】** API 可用，**service `iam` / version `2021-08-01` / GET 查询串风格**（跟 veMLP 不是一套）：

```
GET open.volcengineapi.com/?Action=ListProjects&Version=2021-08-01   (region cn-beijing) -> 200
Result.Projects = [
  {ProjectName:"default",     DisplayName:"默认项目",   Description:"",           Status:"active"},
  {ProjectName:"UMI",         DisplayName:"UMI",        Description:"数采产品线",  Status:"active"},
  {ProjectName:"egocentric",  DisplayName:"ego",        Description:"算法",       Status:"active"},
  {ProjectName:"infra",       DisplayName:"wuji infra", Description:"舞肌ai infra",Status:"active"}
]
```

**【文档】** 项目管理 API 全集：`CreateProject` / `UpdateProject` / `DeleteProject` / `ListProjects` / `GetProject` /
`MoveProjectResource` / `ListProjectResources`（资源管理产品线，docs 6649）。

**注意**：`volcengine-python-sdk 5.0.41` 的 `volcenginesdkiam` / `organization` / `resourcecenter` / `resourceshare` / `tag`
**都没有 Project 类**（实测 `[n for n in dir(m) if 'Project' in n]` 全空）→ 只能手签调，正好走 `volcano.call()`。

**与 veMLP 的连接**：`CreateJobRequest` / `CreateResourceQueueRequest` / `ListJobsRequest` 都有 `project_name` 字段；
队列策略里的 Resource 也出现 `trn:iam::2111674479:project/*`。
**【实测】但现网所有 veMLP 资源的 `project_name` 全是 `"default"`** —— 4 个队列、4 个资源组、抽查的 job 均如此。
→ **项目这一层现网等于没在用。**

### 3.2 第二层：veMLP 队列（ResourceQueue）+ 自动生成的 IAM 用户组 —— 这才是实际的成员边界

**【文档】** veMLP《权限管理》：队列采用角色化成员管理，平台**自动创建两个 IAM 用户组** `q-xxx-admin-group` / `q-xxx-developer-group`
并挂上对应的自定义策略；队列管理员管理队列成员与负载生命周期。
https://docs.volcengine.com/docs/6459/74615

**【实测】现网确实是这样**，`ListGroups` 回的 6 个组里有：

```
q-20260510135940-8j4r6-admin-group      描述「队列CoRL的管理员用户组」
q-20260510135940-8j4r6-developer-group  描述「队列CoRL的成员用户组」
q-20260331160347-rhpkp-admin-group      描述「队列resource_queue_pkn9h的管理员用户组」
wuji-opration (显示名 infra)            -> AdministratorAccess
wuji_product_team (舞肌产品组)
wuji_group (舞肌算法组)
```

**【实测】自动生成的队列策略原文**（`GetPolicy q-20260326171540-flw2n-admin-policy`，Custom）：

```json
{"Statement":[
 {"Effect":"Allow",
  "Action":["ml_platform:*DevInstance*","ml_platform:*Job*","ml_platform:*Service*",
            "ml_platform:*Deployment*","ml_platform:*CustomTask*"],
  "Resource":["trn:ml_platform:cn-shanghai:2111674479:devinstance/*",
              "trn:ml_platform:cn-shanghai:2111674479:job/*",
              "trn:ml_platform:cn-shanghai:2111674479:service/*",
              "trn:ml_platform:cn-shanghai:2111674479:deployment/*"],
  "Condition":{"StringEquals":{"volc:ResourceTag/sys:ml_platform:resource_queue_id":"q-20260326171540-flw2n"}}},
 {"Effect":"Allow",
  "Action":[... ,"ml_platform:*ResourceQueue*"],
  "Resource":["trn:iam::2111674479:project/*",
              "trn:ml_platform:cn-shanghai:2111674479:resourcequeue/q-20260326171540-flw2n"]},
 {"Effect":"Allow",
  "Action":["iam:AddUserToGroup","iam:RemoveUserFromGroup","iam:ListUsersForGroup","iam:ListGroups",
            "iam:ListUsers","iam:GetGroup","iam:GetPolicy","iam:AttachUserGroupPolicy",
            "iam:DetachUserGroupPolicy","iam:ListEntitiesForPolicy"],
  "Resource":["*"]}]}
```

对应的 developer 策略只多不少一条 `ml_platform:GetResourceQueue`，且**没有 tag Condition**。

### 3.3 成员管理 API —— 就是 IAM，没有 veMLP 专用接口

**【实测】** `CreateResourceQueueRequest` / `UpdateResourceQueueRequest` / `GetResourceQueueResponse` 的字段里
**完全没有成员相关字段**（只有 name/quota/rules/shareable/workload_infos/project_name/compute_resources/volume_resources）。
**【实测】** `ml_platform:JoinResourceQueue` 这个 action 名出现在 `MLPlatformDeveloperAccess` 策略里，
但 `2024-07-01` 上 **404，不存在**。

→ **「给队列加人」= 往 `q-<queue-id>-developer-group` 这个 IAM 用户组里加人**，用标准 IAM 接口：

| 操作 | service/version | Action | 备注 |
|---|---|---|---|
| 加人 | `iam` / `2021-08-01` | `AddUserToGroup` | 参数 `UserName` + `UserGroupName` |
| 移除 | `iam` / `2021-08-01` | `RemoveUserFromGroup` | |
| 列成员 | `iam` / `2021-08-01` | `ListUsersForGroup` | **返回键是 `Users`** |
| 列组 | `iam` / `2021-08-01` | `ListGroups` | **返回键是 `UserGroups`**，字段是 `UserGroupName`/`DisplayName`，**没有 `GroupName`** |
| 列组内策略 | `iam` / `2021-08-01` | `ListAttachedUserGroupPolicies` | |

（`ListGroups` 返回键与字段名这两条坑，`clouds/volcano.py::paginate` 的 docstring 里已经记过一次，这次再次踩到：
`UserGroupForListGroupsOutput` 没有 `group_name` 属性，必须用 `user_group_name`。）

**角色枚举**：只有两个，`admin` / `developer`，体现为用户组名后缀，**没有角色枚举 API**。

### 3.4 现网的一个异常，值得单独报给 dev

**【实测】** cn-shanghai 的 4 个生产队列（`wuji-A800-D` / `wuji-H20-D` / `wuji-4090D-A` / `wuji-quota-cpu`）
**没有一个有对应的 `q-*-group` 用户组**；仅有的两组 `q-*` 用户组对应的队列 ID（`q-20260510135940-8j4r6` = CoRL、
`q-20260331160347-rhpkp` = cn-beijing 的测试队列）不在生产队列列表里。

而生产队列的策略是**直接挂到 `舞肌算法组` 上**的：该组身上有 22 条 `q-*-admin-policy` / `q-*-developer-policy`
（含已删除队列的遗留策略），外加 `TOSFullAccess` / `vePFSFullAccess` / `MLPlatformDeveloperAccess` /
`MLPlatformMemberAccess` / `AdministratorAccess` 级别的一大堆 FullAccess。

→ **「队列成员」这个边界在现网是失效的**：全组共享所有队列的 admin+developer 策略。
面板若要按队列建台账，先得知道**现在不是这么用的**。

---

## 4. Q3 —— 权限模型：训练任务用谁的身份访问 TOS / vePFS

### 4.1 TOS：默认走服务关联角色，结构与阿里 PAI 完全同构

**【实测】** `CreateJobRequest.StorageConfig.Credential` 的字段：

```
ConvertCredentialForCreateJobInput = {access_key: str, secret_access_key: str, use_service_linked_role: bool}
```

→ 提交任务的人**二选一**：① 显式塞自己的 AK/SK；② `use_service_linked_role=true` 用平台的服务关联角色。

**【实测】** 该服务关联角色在现网存在：

```
RoleName: ServiceRoleForMLPlatform
DisplayName: 机器学习平台（veMLP）服务关联角色
TrustPolicy: {"Statement":[{"Effect":"Allow","Action":["sts:AssumeRole"],"Principal":{"Service":["ml_platform"]}}]}
AttachedPolicy: ServiceRolePolicyForMLPlatform (System)
```

**【实测】** `ServiceRolePolicyForMLPlatform` 共 18 条语句，**每一条的 `Resource` 都是 `["*"]`，18 条全部没有 `Condition`**。
其中 TOS 那条逐字如下：

```json
{"Effect":"Allow","Resource":["*"],
 "Action":["tos:HeadBucket","tos:HeadObject","tos:PutObject","tos:GetObject","tos:DeleteBucket",
           "tos:ListBucketMultipartUploads","tos:ListMultipartUploadParts","tos:ListBucket","tos:ListBuckets",
           "tos:CreateBucket","tos:PutBucketPolicy","tos:DeleteObject","tos:AbortMultipartUpload",
           "tos:GetAccessPoint","tos:ListAccessPoint","tos:GetAccelerator","tos:ListAccelerator",
           "tos:ListBindAccessPointForAccelerator","tos:ListBindAcceleratorForAccessPoint"]}
```

**→ 和阿里 `AliyunPAIDSWDefaultRole`（`Resource: *`、无条件）是同一种结构。**
只要任务用服务关联角色挂 TOS，**用户自己 IAM 身份上的 `tos:` 前缀策略就不参与鉴权**，「按人收前缀」在 veMLP 内无效。

**比阿里多一个口子**：`use_service_linked_role` 是**提交任务的人自己选的字段**，不是管理员的开关。
就算你把某人的 IAM 收得很紧，他也可以选服务角色路径；反过来他也可以填自己的 AK/SK。
**无论哪条，都不是管理员能强制的边界。**

**还有两个附带的坑**（都在同一份策略里，Resource 全是 `*`、无条件）：
- `iam:UpdatePolicy` —— 服务角色能改 IAM 策略；
- `tos:PutBucketPolicy` + `tos:DeleteBucket` —— 服务角色能改桶策略、删桶。

### 4.2 现网：收前缀在到达 veMLP 之前就已经没意义了

**【实测】** IAM 用户组 `wuji_group`（显示名「舞肌算法组」）身上直接挂着 **`TOSFullAccess`**、`TOSReadOnlyAccess`、
`vePFSFullAccess`、`vePFSReadOnlyAccess`、`FileNASFullAccess`、`MLPlatformDeveloperAccess`、`MLPlatformMemberAccess`……
共 80+ 条系统策略。`wuji_product_team`（舞肌产品组）也挂着 `TOSFullAccess`。

**【实测】** 实际挂载形态印证了这一点：线上 job / 开发机挂 TOS 一律 `prefix: '/'`（整桶），
挂 vePFS 常见 `sub_path: ''`（**整个文件系统根**）且 `read_only: False`。

→ 现状是「人人整桶读写 + 人人整盘读写」。任何前缀级设计要先解决组策略，不是先解决 veMLP。

### 4.3 vePFS：这里火山确实有阿里没有的东西 —— Fileset 挂载权限

**【文档】** veMLP《vePFS》页（https://docs.volcengine.com/docs/6459/145549）：
授权范围可选「主账号内所有成员：主账号下所有子账号都能挂载该目录」，或指定**队列 / 用户组 / 特定子账号**；
权限分**读写**「授权成员有授权目录的读写权限」与**只读**「授权成员有授权目录的只读权限」。
前置策略：`MLPlatformAdminAccess`；要按用户组配置还需 `IAMReadOnlyAccess`（用于拉组列表）；
配置非根目录挂载权限需要 `vePFSFullAccess` 以便在 vePFS 控制台建 Fileset。

**【实测】API 入参**（SDK model，`ml_platform` / `2024-07-01`）：

```
CreateVepfsFilesetPermissionRequest = {
  az, vepfs_id, vepfs_name, fileset_id, fileset_dir, sub_dir,
  read_only_owner:  {authorized_all: bool, users[], user_groups[], resource_queues[]},
  read_write_owner: {authorized_all: bool, users[], user_groups[], resource_queues[]}
}
User / UserGroup / ResourceQueue = {id, name}
CreateVepfsFilesetPermissionResponse = {permission_id}

DeleteVepfsUserPermissionRequest = 同样的形状（az/vepfs_id/fileset_id/fileset_dir/sub_dir/read_only_owner/read_write_owner）
```

**【实测】致命短板：没有读接口。** 用 §1.1 的 404/405 判别法逐个试：

```
CreateVepfsFilesetPermission      -> 405  存在
DeleteVepfsUserPermission         -> 405  存在
ListVepfsFilesetPermissions       -> 404  不存在
ListVepfsFilesetPermission        -> 404  不存在
GetVepfsFilesetPermission         -> 404  不存在
DescribeVepfsFilesetPermissions   -> 404  不存在
ListVepfsUserPermissions          -> 404  不存在
GetVepfsUserPermission            -> 404  不存在
UpdateVepfsFilesetPermission      -> 404  不存在（虽然 IAM 策略里有这个 action 名）
DeleteVepfsFilesetPermission      -> 404  不存在（同上）
```

注：`MLPlatformMemberAccess` 的 Deny 段里**逐字列着** `ml_platform:UpdateVepfsFilesetPermission` 和
`ml_platform:DeleteVepfsFilesetPermission`，但公开 API 上没有 —— 又一个「IAM 命名空间领先于 OpenAPI」的例子。

→ **面板能下发授权，但读不回来。** 台账只能靠自己在 Redis/DB 里记，且**无法与云上真实状态对账**；
控制台上被人手工改过，面板永远发现不了。这个缺口必须在方案里明说。

**【未确认】** 这道权限到底约束到哪一层：
它是「veMLP 在拉起容器时决定挂不挂 / 挂成 ro 还是 rw」，还是「vePFS 服务端真的拒绝」？
从 `ServiceRolePolicyForMLPlatform` 里那条 `vepfs:ClientMount` / `vepfs:ClientWrite` / `vepfs:ClientRootAccess`
（Resource `*`、无条件）看，**大概率是前者 —— veMLP 侧的准入，不是文件系统侧的强制**。
若成立，则任何绕开 veMLP 的挂载路径（自己在 ECS 上 mount、或从别的产品挂同一个 vePFS）都不受它约束。
**这条要真机验证前必须当「未确认」处理，别写进承诺。**

### 4.4 一条尚未评估的可能出路：TOS 接入点（Access Point）

**【文档】** TOS 有单区域接入点（SRAP）/ 多区域接入点（MRAP），支持**接入点策略**。
https://www.volcengine.com/docs/6349 （对象存储，「单区域接入点概述 / 创建单区域接入点」等章节）
**【实测】** veMLP 的挂载 Config union 里**确实有 `tos_ap`**（`TosAPForCreateJobInput = {access_point_id, access_point_name, accelerator_id, accelerator_name, region, server}`），
说明训练任务能挂接入点而不是裸桶。

**【推测，未验证】** 「一人一个接入点 + 接入点策略锁前缀」有可能成为 TOS 侧真正的前缀边界——
因为接入点策略是**资源侧策略**，理论上对服务角色也生效（TOS 鉴权说明称显式 Deny 覆盖显式 Allow）。
但这需要单独一轮取证：接入点策略的语法、是否能对 `ServiceRoleForMLPlatform` 生效、
以及 `tos:GetAccessPoint`/`ListAccessPoint` 已在服务角色里（能不能被它自己绕过）。**本轮不下结论。**

---

## 5. Q4 —— 最小权限 IAM action 名（**全部取自官方策略文档原文，未推断**）

取证方法：不靠命名惯例猜，而是 `GetPolicy` 把火山**自己生成的**策略拉下来读原文
（`MLPlatformDeveloperAccess` / `MLPlatformMemberAccess` / `ServiceRolePolicyForMLPlatform` / 队列自动策略）。
这正好回避了阿里那次 `pai:ListDatasets` vs `paidataset:ListDatasets` 的坑。

### 5.1 服务前缀

**`ml_platform`**（有下划线）。资源 TRN 形如 `trn:ml_platform:<region>:<accountId>:<resourceType>/<id>`，
实测出现过的 resourceType：`devinstance` / `job` / `service` / `deployment` / `resourcequeue`。
项目是 IAM 的资源：`trn:iam::<accountId>:project/*`。

### 5.2 面板要用的 action（逐条出处）

| 面板动作 | Action | 出处 |
|---|---|---|
| 列数据集 | **不存在** | §2.1 实测 404；`ml_platform:ListDatasets` 仅存在于 IAM 命名空间（`ServiceRolePolicyForMLPlatform` 原文），无对应 API |
| 建数据集 | **不存在** | 同上 |
| 列队列（≈列工作空间） | `ml_platform:ListResourceQueues` | 【实测】POST 200；被 `ml_platform:*ResourceQueue*` 覆盖（队列策略原文） |
| 查单个队列 | `ml_platform:GetResourceQueue` | 【文档/原文】`q-*-developer-policy` 逐字列出 |
| 建队列 | `ml_platform:CreateResourceQueue` | 【文档/原文】`MLPlatformMemberAccess` 的 Deny 段逐字列出 |
| 列资源组 | `ml_platform:ListResourceGroups` | 被 `ml_platform:*ResourceGroup*` 覆盖；实测调用 200 |
| 建/改/删资源组 | `ml_platform:CreateResourceGroup` / `ModifyResourceGroup` / `TerminateResourceGroup` / `DeleteResourceGroup` | 【文档/原文】`MLPlatformMemberAccess` Deny 段逐字 |
| 列任务 / 查任务 | `ml_platform:ListJobs` / `ml_platform:GetJob` | 被 `ml_platform:*Job*` 覆盖（队列策略原文）；实测调用 200 |
| 列开发机 | `ml_platform:ListDevInstances` | 被 `ml_platform:*DevInstance*` 覆盖；实测调用 200 |
| **下发 vePFS 目录授权** | `ml_platform:CreateVepfsFilesetPermission` | 【文档/原文】`MLPlatformMemberAccess` Deny 段逐字；实测 405（存在） |
| **撤销 vePFS 目录授权** | `ml_platform:DeleteVepfsUserPermission` | 同上 |
| 回读 vePFS 目录授权 | **不存在** | §4.3 实测全部 404 |
| 列工作空间成员 | `iam:ListUsersForGroup` | 【文档/原文】队列自动策略第 3 条逐字 |
| 加成员 / 移除成员 | `iam:AddUserToGroup` / `iam:RemoveUserFromGroup` | 同上 |
| 列用户组 / 列用户 | `iam:ListGroups` / `iam:ListUsers` / `iam:GetGroup` | 同上 |
| 列项目 | `iam:ListProjects`（**推断**，API 实测可用但未见策略原文） | 【实测】API 200；action 名【推测】—— TRN 是 `trn:iam::...:project/*` 故前缀应为 `iam`，但未在任何策略原文里见到，**用之前要在自定义策略里试一次** |
| 列 vePFS 文件系统 / Fileset | `vepfs:ListFS` / `vepfs:ListFileset` / `vepfs:Describe*` | 【文档/原文】`ServiceRolePolicyForMLPlatform` 第 8 条逐字 |
| 列 TOS 桶 / 读对象 | `tos:ListBuckets` / `tos:ListBucket` / `tos:GetObject` / `tos:HeadBucket` | 【文档/原文】同策略第 2 条逐字 |

### 5.3 现成的系统策略（想省事时）

| 策略 | 内容要点（原文引自 `GetPolicy`） |
|---|---|
| `MLPlatformAdminAccess` | 【文档】「全读写策略。赋予该策略时可读写主账号下所有资源」 |
| `MLPlatformDeveloperAccess` | `ml_platform:Get*/List*/Describe*` 全账号 + `*Job*/*DevInstance*/*Service*/*Deployment*/*ResourceQueue*/*ResourceGroup*` **带 Condition `"volc:PrincipalTrn": "${volc:ResourceTag/sys:ml_platform:createdby}"`（只能动自己建的）** + `tos:GetObject/PutObject/...` `Resource:*` |
| `MLPlatformMemberAccess` | `ml_platform:*` 全放 + 一条 Deny 段禁掉建资源组/建队列/vePFS 权限管理/网络与镜像配置等 28 个写 action |
| `MLPlatformReadonlyAccess` | 只读 |

**注意 `MLPlatformDeveloperAccess` 里那条 tag Condition** —— `sys:ml_platform:createdby` 是平台自动打的系统标签，
这是火山上**唯一一个真正按人生效的 veMLP 边界**，但它管的是「任务/开发机资源」，**不是数据**。

---

## 6. Q5 —— 现网探针结果（只读，主账号 2111674479）

执行路径：`ssh bot-new` → `docker exec -i aiops-bot python -`，凭证取容器内 `TOS_ACCESS_KEY` / `TOS_SECRET_KEY`
（AK 尾 4 位 `lZTc`，长度 47；**未打印、未落盘、未进本文件**）。

| 项 | 结果 |
|---|---|
| veMLP 数据集 | **0 —— 接口不存在，无从谈起** |
| IAM 项目 | 4：`default`(默认项目) / `UMI`(数采产品线) / `egocentric`(算法) / `infra`(舞肌ai infra)；**veMLP 资源全在 `default`** |
| veMLP 队列 | cn-shanghai 4 个：`wuji-A800-D` / `wuji-H20-D` / `wuji-4090D-A` / `wuji-quota-cpu`；cn-beijing 1 个：`resource_queue_pkn9h` |
| veMLP 资源组 | cn-shanghai 4 个（与队列同名一一对应）；cn-beijing 1 个 |
| veMLP 任务 | cn-shanghai `TotalCount=1017` |
| veMLP 开发机 | cn-shanghai `TotalCount=131` |
| IAM 用户 | 45（`ListUsers` limit=100 一页取回） |
| IAM 用户组 | 6（见 §3.2） |
| vePFS 文件系统 | cn-shanghai 3 个：`vepfs-cnsh3d6f174c85e5`(data-infra-D) / `vepfs-cnsh4bb0c73b50ae`(wuji-vefps-D) / `vepfs-cnshef4f4b647664`(wuji-vepfs)，均 Running |
| vePFS Fileset | `wuji-vefps-D`：`wuji-rl`(/wuji-rl, 898) + `wuji-il`(/wuji-il, 145646)；`wuji-vepfs`：`machine_workspace_0`(/wuji_volc_workspace_0) + `test`(/test)；`data-infra-D`：**0 个** |
| vePFS Fileset 授权现状 | **无法读取**（无 List API，§4.3） |
| TOS 桶 | 17 个：`data-infra-sha` `data-sync-b2d` `data-tran` `ego-output` `eog-pretrain-code` `las-datastore` `ml-platform-auto-created-required-2111674479-{cn-beijing,cn-guangzhou,cn-shanghai}` `umi-tos-internet` `umi-tos-internet-lance` `umi-tos-lance` `umi-tos-lerobot` `umi-tos-raw` `wuji-dc-shanghai` `wuji-ego-processed` `wuji-egocentric-data` |
| 属主信息可得性 | job/开发机的 `CreatedBy` / `CreatorTrn` = `trn:iam::2111674479:user/<UserName>`（实测样本 `ChaoguiHuang`）。**存储对象本身没有属主。** |

**未探的（不确定是否只读，按约定记为未知）**：
LAS 的任何接口（service code 未知）；TOS 接入点列表（`tos:ListAccessPoint` 走 TOS 而非 OpenAPI 网关，本轮没试）；
`GetDevInstance` 之外的 dev 详情字段（已取到 storages 即止）。

---

## 7. 直白的判断

### 能不能做成和阿里云一样的形态？—— **不能。差的是「数据集」那一层，它在火山根本不存在。**

阿里那套是三件东西叠起来的：
① CPFS/OSS 上的目录 → ② PAI `CreateDataset` 登记一条指针（带 `UserId` / `Accessibility` / `MountAccess`）→ ③ 面板 List 回来当台账。
火山上 **②** 整层没有：没有对象、没有 API、没有 ID、没有属主字段、没有可见范围、没有标签。
`ml_platform:ListDatasets` 这个 action 名存在于 IAM 命名空间，但任何版本的 OpenAPI 都调不到它（实测遍历 12 个版本串）。

**这不是「没找对名字」**——我用的是「已装 SDK 反查 + 服务端 404/405 判别 + 遍历版本 + 读官方自动生成的策略原文」四路取证，
不是命名惯例推断。四路结论一致。

### 差在哪（逐条对照）

| 阿里 PAI | 火山 veMLP | 差异 |
|---|---|---|
| `CreateDataset` 登记指针 | **无** | **整层缺失** |
| 数据集带 `UserId`（属主自带，免人工登记） | **无** | 属主只在 job/开发机的 `CreatedBy` 上，**跟着任务走，不跟着数据走** |
| `Accessibility` PRIVATE/PUBLIC/ROLE_PUBLIC | **无** | — |
| `MountAccess` RO/RW | 挂载配置里的 `Storage.ReadOnly`（**每个任务自己填**） | 从「数据的属性」降级成「任务的参数」，谁挂谁定，不是管控 |
| 工作空间（成员+角色） | IAM 项目（现网全 `default`，形同虚设）+ veMLP 队列 & 自动 IAM 用户组（现网也没在用，策略直接挂大组） | 有对应物，但**现网两层都是空转** |
| PAI 服务角色 `Resource:*` 无条件 → 收前缀无效 | `ServiceRoleForMLPlatform` 同样 `Resource:*`、18 条语句全无 Condition | **一模一样，且多一个「提交者自选 AK/SK 或服务角色」的口子** |
| CPFS 目录权限：只有 POSIX uid/gid，与 RAM 无映射（阿里的死关口） | **`CreateVepfsFilesetPermission`：按子账号/用户组/队列，分 ro/rw** | **火山在这一点上强于阿里** |

### 退而求其次 —— 建议做成这样

**能诚实交付的版本（三件，都不依赖不存在的东西）：**

1. **目录仍然建，台账自己记。**
   TOS 侧 = 建前缀（本来就是零成本的 key 前缀，`oss-least-privilege-proposal.md` 里那套策略生成逻辑面板已有）；
   vePFS 侧 = `vepfs:CreateFileset`（SDK 已装，`volcenginesdkvepfs.create_fileset`）或直接建目录。
   **属主关系只能由面板自己落库** —— 因为云上没有任何地方能挂这个字段。
   这跟阿里其实差别不大：阿里的 `CreateDataset` 也只是**登记指针、不建目录**（见对照文档 §6-b、§10.1），
   我们无非是把那张指针表从 PAI 搬到面板自己的库里。

2. **用 `CreateVepfsFilesetPermission` 做 vePFS 的按人授权 —— 这是火山这边唯一的真收益，但要带三条免责。**
   - **只能写不能读**：面板必须自己维护授权台账，且**明确告诉运维「本表可能与云上不一致」**，
     因为控制台手工改动面板感知不到。别把它做成一个看起来能对账、实际不能的界面。
   - **【未确认】它约束到哪一层**：很可能只是 veMLP 侧的挂载准入，不是 vePFS 服务端强制（§4.3）。
     上线前必须人工在测试 Fileset + 测试子账号上验一次：授了只读的人，能不能在 veMLP 开发机里写进去；
     以及绕开 veMLP 直接挂同一个 vePFS 能不能写。
   - **前置**：调用身份需 `MLPlatformAdminAccess`（按用户组授权还要 `IAMReadOnlyAccess`），
     建 Fileset 还要 `vePFSFullAccess`。

3. **TOS 侧先别承诺前缀隔离，先把组策略问题摆上台面。**
   现网 `舞肌算法组` 挂着 `TOSFullAccess`，`舞肌产品组` 也挂着 —— **在这个前提下任何前缀策略都是装饰**。
   顺序应该是：先收组策略 → 再谈按人发前缀 → 再评估 TOS 接入点（§4.4，需另一轮取证）能不能对服务角色生效。
   在这之前，面板 TOS 那半只能诚实地定位成**目录规范 + 容量台账**，不是隔离。

**明确不建议的：** 为了「和阿里长得一样」去 LAS 建数据集充当那一层。
LAS 是另一个产品、另一套数据面（Gravitino / LAS-SDK / fsspec），**veMLP 的挂载 union 里没有 LAS 项**，
挂不进训练任务 = 拿不到阿里数据集那份「工效价值」（少填挂载参数、路径统一），只剩一张更贵的台账表。
要用它必须先单独取证（§2.4），**不能顺手就上**。

### 一句话

火山这边能做「按人建目录 + 面板台账 + vePFS 按人授权」，做不了「在平台里登记一条带属主的数据集」。
好消息是阿里那条 `CreateDataset` 本来也只是登记指针、不建目录、不做隔离（见对照文档 §6-b/§6-c），
**我们在火山失去的，主要是形式上的对齐和一点工效，不是已经拿到手的安全能力**；
而火山反而多给了一件阿里没有的 —— vePFS Fileset 按人授权，尽管它只能写不能读。

---

## 8. 出处清单

**官方文档**
- veMLP 功能总览（无数据集模块）https://docs.volcengine.com/docs/6459/72379
- veMLP 常用概念（无数据集/项目/工作空间）https://docs.volcengine.com/docs/6459/76491
- veMLP 权限管理（系统策略、自定义策略、`ml_platform:*` 通配、TRN 格式、队列→IAM 用户组）https://docs.volcengine.com/docs/6459/74615
- veMLP 权限管理（另一版，同主题）https://docs.volcengine.com/docs/6459/1535142
- veMLP vePFS Fileset 挂载权限（授权范围 / ro·rw / 所需策略）https://docs.volcengine.com/docs/6459/145549
- veMLP 快速入门（YAML `Storages: - Type: "Tos" MountPath: "/data00" Bucket: ...`）https://docs.volcengine.com/docs/6459/80586
- veMLP 快速入门-管理员版（资源组→队列，无项目一层）https://docs.volcengine.com/docs/6459/1582713
- 资源管理 · 项目和标签的关系（项目定义、唯一归属、按 IAM 身份授权、项目管理 API 清单）https://docs.volcengine.com/docs/6649/94340
- 文件存储 vePFS · CreateFileset https://www.volcengine.com/docs/6645/143666
- 对象存储 TOS · 接入点章节（SRAP/MRAP 与接入点策略）https://www.volcengine.com/docs/6349
- 对象存储 TOS · 鉴权说明（显式 Deny 覆盖显式 Allow）https://www.volcengine.com/docs/6349/1183370
- AI 数据湖服务 LAS（§2.4 未确认项）https://www.volcengine.com/docs/6492/1264538

**真机只读取证（全部在 `aiops-bot` 容器内执行）**
- SDK 反查：`volcenginesdkmlplatform20240701` 的 74 个 `*_request.py`、`CreateJobRequest` / `StorageConfigForCreateJobInput` /
  `ConfigForCreateJobInput` / `TosForCreateJobInput` / `VepfsForCreateJobInput` / `TosAPForCreateJobInput` /
  `CreateVepfsFilesetPermissionRequest` 等的 `swagger_types`
- 全 SDK 搜索：`ls volcenginesdk*/models/*dataset*` → 只有 `volcenginesdkark`（方舟精调任务的入参子结构，非独立资源）
- 服务端 404/405 判别：`ml_platform`/`2024-07-01` 上 14 个候选 action 名 + 12 个候选 version 串
- `GetPolicy`：`ServiceRolePolicyForMLPlatform`(System, 18 条语句) / `MLPlatformDeveloperAccess` / `MLPlatformMemberAccess` /
  `q-20260326171540-flw2n-admin-policy`(Custom) / `q-20260326171540-flw2n-developer-policy`(Custom)
- `ListRoles` / `ListAttachedRolePolicies` / `ListGroups` / `ListAttachedUserGroupPolicies` / `ListUsers`（IAM 2021-08-01）
- `ListProjects`（iam / 2021-08-01，GET 风格）
- `ListResourceQueues` / `ListResourceGroups` / `GetResourceGroup` / `GetResourceQueue` / `ListJobs` / `GetJob` /
  `ListDevInstances` / `GetDevInstance`（ml_platform / 2024-07-01，POST 风格）
- `DescribeFileSystems` / `DescribeFilesets`（vepfs，cn-shanghai）
- TOS `list_buckets`（`utils.volcano_client_factory.get_tos_client`）
