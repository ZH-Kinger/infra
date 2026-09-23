# 「这个工作空间有没有可用的算力队列（卡）」该调哪个接口

调研人 researcher · 2026-09-23 · 面向「云权限面板自动识别未登记工作空间」

标注约定：【文档】官方文档明说（带 URL）/【实测】本次用面板现有只读凭证真机反查所得
（全部只读 GET，无任何写操作）/【推测】未证实。

---

## 0. 一句话结论

**阿里云**：调 `GET https://pai.<region>.aliyuncs.com/api/v1/quotas/`
（产品 PaiStudio，`x-acs-version: 2022-01-12`，ROA 签名，和现有 `_pai_page` 一样的写法），
逐地域拉全量，然后按返回里每条配额自带的 `Workspaces[].WorkspaceId` 反向建索引。
判「有卡」= 该 workspace 下存在一条 `QuotaDetails.ActualMinQuota.GPU > 0` 的配额。
权限：`pai:ListQuotas` —— **`panel-collector` 现在就能调通，不用补权限**。

**火山引擎**：调 `ListResourceQueues`（Service `ml_platform`，Version `2024-07-01`，
SDK 包 `volcenginesdkmlplatform20240701`，已随 `volcengine-python-sdk` 装好），
看 `Items[].QuotaCapability.GpuCount` 和 `GpuCountInfos[]{GpuType,Count}`。
权限：`ml_platform:ListResourceQueues` —— **三把面板凭证全都没有，必须补**。

**但是**（见 §4）：面板现在列出来的「火山 3 个工作空间」根本不是算力工作空间，
是**托管 Prometheus（VMP）的工作区**；而阿里那 7 个的来源本次没能复现，
资源中心快照里一条 PAI 工作空间都没有。先把这两件事弄清楚，再谈过滤。

---

## 1. 阿里云 PAI

### 1.1 三个 `quotas` / `resources` 接口，只有一个是对的

你们试过的两个都不是要找的东西，原因是**产品不同、路径撞名**：

| 你调的 | 产品 | 真实含义 |
|---|---|---|
| `aiworkspace.<r>.aliyuncs.com` `GET /api/v1/quotas` | AIWorkSpace 2021-02-04 | **MaxCompute / 公共资源**配额。文档标题原文就是「使用 ListQuotas 获取 **MaxCompute GPU 资源组**的资源配额列表」 |
| `aiworkspace.<r>.aliyuncs.com` `GET /api/v1/resources?WorkspaceId=` | AIWorkSpace 2021-02-04 | 工作空间关联的「资源」（MaxCompute/Flink 这类），和云原生算力无关，返回 0 是正常的 |
| `aiworkspace…/api/v1/resourcegroups` | —— | 不存在，404 `InvalidAction.NotFound` 是对的 |
| **`pai.<r>.aliyuncs.com` `GET /api/v1/quotas/`** | **PaiStudio 2022-01-12** | **要找的：云原生资源配额（通用计算 ECS + 灵骏 Lingjun）** |
| `pai.<r>.aliyuncs.com` `GET /api/v1/resources` | PaiStudio 2022-01-12 | `ListResourceGroups`，**路径叫 resources**（这就是 `resourcegroups` 404 的原因） |

【实测】同一把 `panel-collector` 凭证、同一地域 cn-hangzhou，两个 `quotas` 返回完全不相交：

```
# aiworkspace 2021-02-04 /api/v1/quotas  ← 你们看到的那条
{"Quotas":[{"Name":"aliyun_pai_public","QuotaType":"PAI","Mode":"share",
            "Specs":null,"ProductCode":"PAI_share","Id":"780843392557152", ...}]}

# paistudio 2022-01-12 /api/v1/quotas/   ← 真正的算力队列
TotalCount = 3：wuji-hz-5090 / wuji-general-gpu / ai_pai_quota
```

【文档】产品概念：「根据资源类型，分为以下两种类型的资源配额：**云原生资源配额**（灵骏智算
和通用计算）…和**大数据引擎类型的资源配额**（MaxCompute、Flink）」
— <https://help.aliyun.com/zh/pai/user-guide/resource-quota/>

### 1.2 ListQuotas 调用方式

- 产品：**PaiStudio**，version **`2022-01-12`**
- Endpoint：**`pai.<region>.aliyuncs.com`**（公网）/ `pai-vpc.<region>.aliyuncs.com`（VPC）
  【文档】<https://help.aliyun.com/zh/pai/user-guide/api-paistudio-2021-02-02-endpoint>
- Method / Path：`GET /api/v1/quotas/`（【实测】结尾带不带 `/` 都通）
- 签名：ROA（ACS 1.0），**和 `assets.py::_pai_page` 用的 `aliyun.call_roa` 完全一样**，
  只需把 host 换成 `pai.` 前缀、把 version 换成 `2022-01-12`。
- 无必填参数。分页 `PageNumber`/`PageSize`，响应有 `TotalCount`。

关键可选参数【文档】<https://help.aliyun.com/zh/pai/developer-reference/api-paistudio-2022-01-12-listquotas>：

| 参数 | 说明 |
|---|---|
| `WorkspaceIds` | 「按 WorkspaceIds 过滤，只支持精确匹配，最多同时支持 10 个」，逗号分隔 |
| `ResourceType` | `Lingjun` / `ECS`（文档写默认 ECS） |
| `Statuses` | 如 `Ready` |
| `Verbose` | `true` 时多返 `NodeStatistics`、`AllocatedQuota` 等 |
| `GPUType` / `ClusterType` / `QuotaIds` / `QuotaName` / `ParentQuotaId` / `Labels` | 其余过滤 |

【实测】`WorkspaceIds` 真的生效且是精确匹配：
- `WorkspaceIds=640957` → TotalCount 2
- `WorkspaceIds=640957,590221` → TotalCount 3
- `WorkspaceIds=999999`（不存在）→ TotalCount 0，**不报错**

【实测·与文档不符】`ResourceType` 文档说「默认 ECS」，但**不传时返回的是全部**
（杭州 3 条 = 1 条 ECS + 2 条 Lingjun）；显式传 `Lingjun` 得 2 条、传 `ECS` 得 1 条。
→ 采集时**就不要传 `ResourceType`**，别信「默认 ECS」那句，也别为了保险分两次调。
（传 `ACS` 会 400 `Miss parameter: Produ…`，不是合法值。）

### 1.3 返回里哪个字段能看出「有没有卡、什么卡、几张」

【实测】杭州一条灵骏配额的真实返回（截取关键字段）：

```jsonc
{
  "QuotaId": "quotai7l7gqmouqw",
  "QuotaName": "ai_pai_quota",
  "ResourceType": "Lingjun",            // Lingjun=灵骏智算 / ECS=通用计算（都是专有，不是公共）
  "GPUType": "GU8T",                    // 卡型内部代号，不是 A100/H200 这种市场名
  "Status": "Ready",
  "Min": {"NodeSpecs": [{"Count": 18, "Type": "ml.gu8tf.8.46xlarge"}]},  // 机型+台数
  "QuotaDetails": {
    "ActualMinQuota":  {"CPU":"3312","GPU":"144","GPUMemory":"13824Gi","Memory":"32400Gi"},
    "DesiredMinQuota": {"CPU":"3312","GPU":"144","GPUType":"GU8T","Memory":"32400Gi"},
    "UsedQuota":       {"CPU":"103","GPU":"5","Memory":"558080Mi"},
    "NodeStatistics":  {"ActualMinNodeNum":18,"AllocatedNodeNum":1,"EmptyNodeNum":17}  // 需 Verbose=true
  },
  "ResourceGroupIds": ["lingj11mrdoqesmf"],
  "Workspaces": [{"WorkspaceId": "590221", "WorkspaceName": "ai_hz_gpu"}],
  "QuotaConfig": {"WorkloadTypes": ["DLC","DSW","EAS","TENSORBOARD"], "ClusterId": "...", ...},
  "SubQuotas": []
}
```

判据建议（按可靠性排）：

1. **`QuotaDetails.ActualMinQuota.GPU`**（字符串，要 `int()`）—— 实际分到的卡数。
   `> 0` 即「这个空间有卡」。用 Actual 不用 Desired：Desired 是申请值，
   配额建了但还没交付时 Desired 有值而 Actual 为 0。
2. `GPUType` + `Min.NodeSpecs[].Type/Count` —— 卡型与机型台数，给人看的展示字段。
3. `Status == "Ready"` —— 非 Ready 的配额不该算「可用」（还有 `ReasonCode`/`ReasonMessage`）。
4. `ResourceType` —— `Lingjun` / `ECS` 都是**专有**资源。
   公共资源组根本不在这个接口里（它在 aiworkspace 那条 `aliyun_pai_public`），
   所以**「出现在 PaiStudio ListQuotas 里」本身就已经排除了公共资源组**，不用额外过滤。

### 1.4 「配额 → 绑了哪些工作空间」不用反查，返回里就有

`Workspaces` 是响应字段【文档】：数组，每项 `{WorkspaceId, WorkspaceName}`。

所以推荐的采集形状是**反过来做**：一次 `ListQuotas`（不带 `WorkspaceIds`）拿全量，
在内存里 `quota → Workspaces[]` 展开成 `workspace_id → [quota…]` 索引。
比「对每个 workspace 各调一次」少 N-1 次请求，而且天然覆盖到
「工作空间在快照里、但没登记」的那些。

### 1.5 DLC 的 `ResourceQuotaId` 从哪来

DLC `CreateJob` 的参数名是 **`ResourceId`**，不是 `ResourceQuotaId`。
【文档】原文：「资源组 ID，可选参数。参数值为空表示**提交到公共资源组**。如果当前工作空间
已经绑定的资源配额，此处可以指定对应的**资源配额 ID**」
— <https://www.alibabacloud.com/help/zh/pai/developer-reference/api-pai-dlc-2020-12-03-createjob>

也就是说它吃的就是 ListQuotas 返回的 `QuotaId`（`quota…`）。

**顺带一个对「空壳」定义的提醒**：按这段文档，*没有任何配额的工作空间照样能提交 DLC 任务*
——落到公共资源组（按量付费共享）。所以「没有专有配额」≠「完全不能用」，
只是「没有属于自己的卡」。面板文案建议写「无专属算力配额」而不是「不可用」。

### 1.6 ⚠️ 陷阱一：ListQuotas 的返回**受工作空间 RBAC 裁剪**

【实测】同一地域，换三把凭证调同一个接口：

| 身份 | cn-hangzhou 可见工作空间 | cn-hangzhou 可见配额 |
|---|---|---|
| `panel-collector` | 590221, 640957 | wuji-hz-5090 / wuji-general-gpu / ai_pai_quota（3） |
| `DELIVERY_EXEC_ALIYUN_…` | 640957 | wuji-hz-5090 / wuji-general-gpu（2） |
| `DELIVERY_ISSUER_ALIYUN_…` | —— 空 —— | —— 空 —— |

三者**都返回 HTTP 200**，没有一个报权限错。exec 那把能看见 640957 的两条配额、
看不见 590221 的那条，和它的工作空间成员身份严格一致。

这条的后果对本需求是致命的：**采集身份不是某个工作空间的成员时，
`ListQuotas` 返回空，长得和「这个空间真的没有卡」一模一样**。
而本需求恰恰是要判定「没登记进 workspaces.json 的那些空间」——
那些空间 `panel-collector` 大概率也不在里面。

→ 必须**区分「查不到」和「没有」**：
- 稳妥做法：对每个待判定的 workspace，先确认采集身份在其成员表里
  （`aiworkspace` `GET /api/v1/workspaces/{id}/members`，`provision.py` 已在用）；
  不在 → 判「**未知**」，照常列出来并标注「采集身份看不见该空间，卡数待人工确认」，
  **绝不判成空壳而过滤掉**。
- 或者：把 `panel-collector` 加进所有工作空间（只读角色），代价是要有人维护这件事，
  而且新建的空间又会漏。
- 【推测·未证实】主账号 AK 或带 `AliyunPAIFullAccess` 的身份能越过 RBAC 看全量。
  没验，因为手上没有这类凭证，也不该为验这个去拿。

### 1.7 ⚠️ 陷阱二：地域错误不能当成「没有卡」

【实测】按 `assets.PAI_REGIONS` 逐个打 `pai.<region>.aliyuncs.com`：

| 地域 | 结果 |
|---|---|
| cn-hangzhou | 200，3 条配额（2 个工作空间） |
| ap-southeast-1 | 200，1 条 `ai_pai_quota_h200`（Lingjun / GPUType `L20X` / GPU 48 / ws 284761） |
| cn-shanghai / cn-beijing / cn-shenzhen / cn-wulanchabu | 200，0 条（也 0 个工作空间） |
| **cn-heyuan** | **DNS 解析不了** —— `pai.cn-heyuan.aliyuncs.com` 不存在，PAI 服务接入点表里也没有河源 |
| **cn-zhangjiakou** | **持续 HTTP 503 ServiceUnavailable**（连打两次），疑似该地域未开通 PAI |

→ 采集必须按地域分别 try/except，把「DNS 失败 / 5xx / 权限拒」记成 **skipped**，
判定时对 skipped 地域的工作空间一律给「未知」，不给「无卡」。
这是 `collect_pai_datasets` 已有的写法，照抄即可。

### 1.8 权限

| Action | 级别 | 资源 | 现状 |
|---|---|---|---|
| `pai:ListQuotas` | list | `*` | 【实测】**`panel-collector` 已经能调通，不用补** |
| `pai:ListResourceGroups` | list | `*` | 【实测】**被拒**：`RAMForbidden … Action pai:ListResourceGroups permission denied, reason: Code:[4001], Message:[NoPermission]` |

【文档】两个 action 名都取自各自 API 页的「授权信息」表，
资源类型都是「*全部资源」、不支持资源级授权：
- <https://help.aliyun.com/zh/pai/developer-reference/api-paistudio-2022-01-12-listquotas>
- <https://help.aliyun.com/zh/pai/developer-reference/api-paistudio-2022-01-12-listresourcegroups>

注意前缀：这次是 **`pai:`**，不是 `paiworkspace:` / `paidataset:` / `paidlc:`。
四种前缀在同一个产品里并存，`executor-policy.aliyun-collector-pai.json` 里
已经为此写过一段警告——ListQuotas 属于 `pai:` 这一族。

**本需求不需要 `pai:ListResourceGroups`**：资源组 ID 在 ListQuotas 返回的
`ResourceGroupIds` 里已经有了，而卡数/卡型 ListQuotas 也都给了。除非以后要展示
资源组的到期时间/计费方式，否则不用为它补权限。

---

## 2. 火山引擎机器学习平台

### 2.1 接口

- Service **`ml_platform`**，Version **`2024-07-01`**，Action **`ListResourceQueues`**
  （火山的「队列 Queue」就是阿里「资源配额 Quota」的对位概念）
- SDK：**`volcenginesdkmlplatform20240701`**，是已装的 `volcengine-python-sdk`
  的子包（和 `volcenginesdkdms`、`volcenginesdkvepfs` 同一个 monolith）。
  **不需要新依赖。** API 类名是 `MLPLATFORM20240701Api`（不是 `MLPLATFORMApi`）。
- 文档索引：<https://www.volcengine.com/docs/6459/72379>（功能总览）；
  API 明细页 JS 渲染、WebFetch 抓不到正文，下面的字段全部来自 **SDK model 反查**。
- 相关 API 全家【文档·搜索结果确认名称】：`CreateResourceQueue` / `GetResourceQueue` /
  `ListResourceQueues` / `UpdateResourceQueue` / `PauseResourceQueue` /
  `ResumeResourceQueue` / `DeleteResourceQueue`；资源组侧 `ListResourceGroups` /
  `GetResourceGroup` 等。

### 2.2 请求字段【实测·SDK 反查】

`ListResourceQueuesRequest.attribute_map`：

```
ChargeType, Ids, NameContains, PageNumber, PageSize, ProjectName,
ResourceGroupIds, Shareable, SortBy, SortOrder, Status, WorkloadTypes, ZoneIds
```

**没有 WorkspaceId**——火山 veMLP 里没有「工作空间」这个对象（SDK 里 `Workspace` 一个类都没有）。
最接近的归属维度是 **`ProjectName`（火山 IAM 项目）**，队列返回里也带 `ProjectName`。
地域不是请求参数，走 `Configuration.region`，所以要**逐地域调**。

### 2.3 响应字段【实测·SDK 反查】

`ItemForListResourceQueuesOutput.swagger_types` 关键项：

```
Id, Name, Description, ProjectName, ResourceGroupId, ChargeType, ZoneIds,
Shareable, IsOverQuota, Status,
QuotaCapability, QuotaAllocated, QuotaIdle, SharedQuotaAllocated, SystemQuotaAllocated,
ComputeResources[], VolumeResources[], WorkloadInfos[], Rules, ...
```

判「有卡」看这三个：

| 字段 | 结构 | 用途 |
|---|---|---|
| `QuotaCapability.GpuCount` | `float` | **队列总卡数 → `>0` 即有卡** |
| `QuotaCapability.GpuCountInfos[]` | `{GpuType: str, Count: float}` | **按卡型拆的卡数**（展示用） |
| `ComputeResources[]` | `{InstanceTypeId, Count, ZoneId}` | 机型 + 台数 + 可用区 |

配套还有 `QuotaCapability.{Cpu, MemoryGiB, VolumeSizeGiB, GpuMemoryInfos, GpuRdmaInfos}`，
以及 `QuotaAllocated`（已分配）/ `QuotaIdle`（空闲）同构字段，可直接做「在用 / 空闲」展示。
`Status` 是 `{State, SecondaryState, Message}`，类比阿里的 `Status: Ready`。

### 2.4 权限：三把面板凭证全都没有

【实测】三把火山凭证（collect / exec / issuer）调 `ListResourceQueues`，返回体逐字一致：

```json
{"ResponseMetadata":{"Action":"ListResourceQueues","Version":"2024-07-01",
 "Service":"ml_platform","Region":"cn-beijing",
 "Error":{"CodeN":100013,"Code":"AccessDenied",
          "Message":"User is not authorized to perform: ml_platform:ListResourceQueues on resource: "}}}
```

`ListResourceGroups` 同样被拒，报 `ml_platform:ListResourceGroups`。

→ IAM action 名由报错原文坐实（不是猜的）：**`ml_platform:ListResourceQueues`**。
要用就得给 `panel-collector` 对应的火山 IAM 身份加这一条（只读 list 级）。
`identity/executor-policy.volcano-collector.json` 里加即可。

---

## 3. ⚠️ 本次顺带发现的两个前提问题（比接口本身更要紧）

### 3.1 面板说的「火山 3 个工作空间」是**托管 Prometheus 的工作区**，不是算力空间

【实测】把火山资源中心 `SearchResources` 的**原始记录**打出来（`assets.collect_volcano`
只保留了 4 个字段，把关键的 `Service`/`TypeName` 丢了）：

```json
{"Service":"vmp","ResourceType":"Workspace","TypeName":"Volcengine::VMP::Workspace",
 "Region":"cn-shanghai","ResourceName":"h20-VLA","ResourceID":"449de3dd-…"}
{"Service":"vmp",…,"Region":"cn-shanghai","ResourceName":"data-infra",…}
{"Service":"vmp",…,"Region":"cn-beijing", "ResourceName":"WujiGrasp",…}
```

`vmp` = **托管 Prometheus**【文档】<https://www.volcengine.com/docs/6731/106522>（创建通用工作区）。
这三个是监控工作区，和 GPU、和 PAI 式的工作空间毫无关系。

根因在 `assets.py`：

```python
WORKSPACE_TYPES = frozenset({"ACS::PAIWorkspace::Workspace", "Workspace"})
```

火山那一侧用的是裸串 `"Workspace"`，而火山资源中心里**任何产品**的 Workspace 型资源
都叫这个名字。`PAI_TYPES` 上面那句注释写着「**必须全名匹配**：按子串 `"pai"` 匹配
会把火山的 `keypair` 算进来（真踩过）」——这里是同一个坑的另一面：
裸串 `Workspace` 不够全名。应该改成按 `TypeName` 匹配 `Volcengine::<Service>::Workspace`，
并且先想清楚火山侧到底要不要有「工作空间」这个概念（veMLP 没有，只有 Project + Queue）。

### 3.2 「阿里 7 个工作空间」本次复现不出来

【实测】用 `panel-collector`：

- `assets.collect_aliyun()` 全量跑完 = 399 条资源，其中
  **`ACS::PAIWorkspace::Workspace` 0 条**（资源中心根本没收录 PAI 工作空间）。
- 逐地域打 `aiworkspace /api/v1/workspaces` = **一共 3 个**：
  `590221 ai_hz_gpu`、`640957 wuji_general_gpu`（cn-hangzhou）、
  `284761 ai_sg_wj_workspace`（ap-southeast-1）。加 `Verbose=true` 也还是 3 个。

而 `cli_requests._discover_regions` → `assets.unregistered_workspaces` 读的是
**资产快照**，也就是资源中心那一路。按本次结果，阿里侧那条路现在返回 0。

所以「7 个」要么来自服务器上一份更旧/更全的 `identity/assets.json`，
要么来自另一把更有权限的凭证，要么来自你们另写的原型。
**开工前先确认这 7 个的来源和 ID 清单**——否则「过滤掉没卡的」可能是在过滤一个
本身就取错了的集合（比如混进了别的产品的 Workspace，像火山那 3 个一样）。

诊断命令（只读）：
```bash
delivery assets collect          # 看 PAIWorkspace::Workspace 到底有没有
python - <<'PY'  # 或者直接逐地域打 aiworkspace，和快照对一下
# 见 src/delivery/assets.py::collect_pai_datasets 里现成的写法
PY
```

---

## 4. 可执行结论

**面板要判断「这个工作空间有没有卡」：**

### 阿里云
1. 对 `PAI_REGIONS`（**去掉 `cn-heyuan`**，它没有 `pai.` 接入点）逐地域调
   `aliyun.call_roa(f"pai.{region}.aliyuncs.com", "2022-01-12", "/api/v1/quotas/",
   {"PageNumber": n, "PageSize": 100}, creds=creds)`。
   **不要传 `ResourceType`。**
2. 把每条 quota 按 `Workspaces[].WorkspaceId` 展开成 `workspace_id → [quota]` 索引。
3. 判定：存在任一 quota 满足 `Status == "Ready"` 且
   `int(QuotaDetails.ActualMinQuota.GPU or 0) > 0` → **有卡**；
   展示用 `GPUType` + `ActualMinQuota.GPU` + `Min.NodeSpecs[].{Type,Count}`。
4. **fail-open 地对待未知**：地域调用失败（DNS/5xx）、或采集身份不是该空间成员，
   一律判「未知」并照常列出、标注原因；只有「确实查到了、而且 GPU 都是 0」才算空壳。
5. 权限：`pai:ListQuotas`（`panel-collector` 已有，**零改动**）。

### 火山引擎
1. 用 `volcenginesdkmlplatform20240701.MLPLATFORM20240701Api.list_resource_queues`
   逐地域调，取 `Items[].QuotaCapability.GpuCount > 0`；
   卡型看 `GpuCountInfos[]{GpuType, Count}`；归属维度是 `ProjectName`（不是 workspace）。
2. 权限：**要补 `ml_platform:ListResourceQueues`** 到火山采集身份。
3. 在补权限之前，先修 §3.1 —— 现在列出来的 3 个「火山工作空间」是 Prometheus 工作区，
   过滤它们的正确方式不是「查有没有卡」，是**根本不该把 `vmp` 的 Workspace 算进来**。

### 开工顺序建议
先 §3.2（确认那 7 个是什么）→ §3.1（修 `WORKSPACE_TYPES` 全名匹配）
→ 再接阿里 ListQuotas → 最后补火山权限接 ListResourceQueues。
顺序反了的话，卡数过滤会盖住集合本身取错的问题。

---

## 5. 待确认 / 未验证

- 【推测】主账号或 `AliyunPAIFullAccess` 身份的 `ListQuotas` 是否能越过工作空间 RBAC 看全量。
  没验（手上没这类凭证）。这决定了 §1.6 要走「加成员」还是「换身份」。
- 【推测】`cn-zhangjiakou` 的持续 503 是「该地域未开通 PAI」还是真故障。
  两次都 503，但 503 不是标准的「未开通」错误码，值得隔天再探一次。
- `GPUType` 的代号（`GU8T` / `G59` / `L20X`）到市场卡型名（H20/H200/5090/L20…）
  的对照表没找到官方来源。要在面板上显示人话卡型，得自己维护一张表，
  或者用 `Min.NodeSpecs[].Type`（`ml.gu8tf.8.46xlarge` / `ecs.ebmgn9t.48xlarge`）
  去查 ECS 实例规格族文档。
- 火山 `ListResourceQueues` 的真实返回没见过（无权限），字段全部来自 SDK model；
  `Status.State` 的终态枚举串、`GpuCountInfos` 在没有 GPU 的队列里是 `[]` 还是 `null`，
  都要等权限补上后真机确认一次。
- `tools/aliyun/gpu_distribution.py` 里的 Prometheus 指标前缀 `AliyunPaiquota_`
  确实对应本文的「PAI 资源配额」，`QUOTA_GPU_ACCELERATOR_DUTTY_UTIL` 这种指标
  是同一批数据的监控投影。面板不接 Prometheus，直接用 ListQuotas 即可，
  `ActualMinQuota.GPU` 对应 `NODE_GPU_ACCELERATOR_TOTAL`、
  `UsedQuota.GPU` 对应 `NODE_GPU_ACCELERATOR_REQUEST`（【推测】，量级吻合但未逐值比对）。
