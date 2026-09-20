# DSW 实例数据源（挂载）盘点：OpenAPI、字段、RAM 动作、溯源

调研人：researcher ｜ 日期：2026-09-20
场景：有人把 CPFS **根目录**绑进 DSW 实例的数据源配置，并通过**克隆别人的旧配置**扩散。PAI 数据集侧已确认干净（144 条路径深度均为 1–2），所以目标对象是 **DSW 实例的 `Datasets[]` 挂载配置**。

取证方式：① 官方帮助中心页面原文；② **本地已装 SDK `alibabacloud_pai_dsw20220101` 的源码/模型反查**（只读，没发任何云请求）。
标注：【文档】官方明写 ｜【实测】SDK 反查所得 ｜【推断】我推的，未证实。

---

## 0. 结论速览

- 正确调用：**endpoint `pai-dsw.<region>.aliyuncs.com`，version `2022-01-01`，ROA 风格，`GET /api/v2/instances`，action `ListInstances`**。你试的 `/api/v1/instances` 是**旧路径**，当前是 **v2**。
- 挂载配置在 `Instances[].Datasets[]`，判根目录看 **`Uri`**（`DatasetId` 为空、`Uri` 直接是 `cpfs://…` 且路径段为空 ⇒ 根绑定）。**还有第二个藏身处**：`DynamicMount.MountPoints[].RootPath`，别只扫 `Datasets`。
- RAM 动作：`paidsw:ListInstances` + `paidsw:GetInstance`；**但要看到别人的私有实例，需要 `paidsw:ListAllInstances`**（另一个权限点，很多人会漏）。
- **克隆来源：API 里没有任何字段**。能拿到的只有 `UserId`/`UserName`/`GmtCreateTime`；真正能重建传播链的是**操作审计 ActionTrail**（PAI-DSW 在支持列表里）。
- **DSW 可以不经数据集直接挂 NAS/CPFS，这是产品设计**（`Datasets[].Uri` 与 `DatasetId` 互斥，官方原文「实现直接挂载」）。**RAM 里没有任何动作能单独禁掉「挂 NAS/CPFS」**——所以「不允许直接挂 NAS」的限制几乎不可能是 RAM 实现的，极可能只是控制台前端不给入口，**OpenAPI 直调 / 克隆既有配置都能绕过**。

---

## 1. 列 DSW 实例的 OpenAPI

| 项 | 值 |
|---|---|
| Endpoint | `pai-dsw.<region>.aliyuncs.com`（如 `pai-dsw.cn-hangzhou.aliyuncs.com`） |
| Version | `2022-01-01` |
| 风格 | **ROA**（RESTful，不是 RPC —— 不要在 query 里传 `Action=`） |
| Action / 路径 | `ListInstances` — `GET /api/v2/instances` |
| 详情 | `GetInstance` — `GET /api/v2/instances/{InstanceId}` |
| 建实例 | `CreateInstance` — `POST /api/v2/instances` |
| Python SDK | `alibabacloud-pai-dsw20220101`（`Client.list_instances` / `.get_instance`） |

- 【文档】请求语法 `GET /api/v2/instances HTTP/1.1`：<https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-listinstances>
- 【文档】`POST /api/v2/instances`：<https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-createinstance>
- 【实测】SDK `list_instances_with_options` 里的 `Params`：`action='ListInstances', version='2022-01-01', protocol='HTTPS', pathname='/api/v2/instances', method='GET', auth_type='AK', style='ROA'`；`get_instance` 为 `/api/v2/instances/{instance_id}`。

**你三次失败的原因**（【推断】，但与报错吻合）：
- `pai.cn-hangzhou.aliyuncs.com` + `2022-01-01` → `InvalidVersion`：`pai` 是另一个产品（AIWorkspace/PAI 通用），它没有 2022-01-01 这个版本。端点错了，不是版本错了。
- `pai-dsw.cn-hangzhou.aliyuncs.com` → `InvalidAction.NotFound`：端点对了，但走了 `/api/v1/instances`（v1 已不存在）或按 RPC 风格发（ROA 接口按路由找不到 action）。
- `aiworkspace.…` → 该端点只服务工作空间/数据集/模型那套 API，没有 instances。

### 盘点时必须注意的三个默认值坑（漏扫就等于没查）【文档】

`ListInstances` 请求参数原文：

> **WorkspaceId**：按工作空间 ID 筛选。**默认值为空，此时会使用当前子账号的"默认工作空间"**。如果需要查询所有工作空间的实例（即不针对某个特定的工作空间进行筛选，而是列出该 region 跨工作空间的所有实例），**请填写 `ALL`**。
> **ResourceId**：按资源配额 ID 筛选。**默认值为空，此时只返回后付费资源组的实例**。填 `ALL` 会返回所有的实例。

⇒ 盘点必须显式传 `WorkspaceId=ALL` **且** `ResourceId=ALL`，否则会漏掉「别的工作空间」和「专有资源组/灵骏」上的实例——而 CPFS 挂载恰恰多发生在专有资源组上。另外 **endpoint 带 region，要逐个地域扫**（`cn-hangzhou` / `cn-shanghai` / `cn-wulanchabu` …），再加 `PageNumber`/`PageSize` 翻页。

> 注：仓库 `langchaindev` 里 `tools/aliyun/pai_dsw.py` 的 `action="list"` 走的是 `.env` 里的默认 workspace/resource id，**不是 `ALL`**，直接拿它当盘点工具会漏。

---

## 2. 挂载配置在返回里的字段

`ListInstances` → `Instances[].Datasets[]`，`GetInstance` → `Datasets[]`，结构一致：

| 字段 | 类型 | 含义 |
|---|---|---|
| `DatasetId` | string | 数据集 ID（走"数据集"方式挂载时才有） |
| `DatasetVersion` | string | 数据集版本，默认 `v1` |
| `Uri` | string | **存储服务目录的 Uri，实现直接挂载；与 `DatasetId` 互斥** |
| `MountPath` | string | 容器内挂载路径，如 `/mnt/data` |
| `MountAccess` | string | `RW` 读写 / `RO` 只读 |
| `Options` | string | 自定义挂载属性（仅 OSS） |
| `OptionType` | string | 已废弃 |
| `Dynamic` | boolean | 是否动态挂载 |

【文档】字段表与返回示例：[ListInstances](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-listinstances)、[CreateInstance](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-createinstance)；【实测】SDK 模型 `ListInstancesResponseBodyInstancesDatasets` / `GetInstanceResponseBodyDatasets`（后者多一个 `ActualMountAccess`）字段与之逐一对应。

返回示例片段【文档】：
```json
"Datasets": [
  { "DatasetId": "d-vsqjvsjp4orp5l206u", "DatasetVersion": "v1",
    "Uri": "oss://bucket-name.oss-cn-shanghai-internal.aliyuncs.com/data/path/",
    "MountPath": "/mnt/data", "MountAccess": "RW", "Options": "{...}" }
]
```

### 2.1 各类存储的 Uri 格式（官方原文）【文档】

```
OSS:        oss://bucket-name.oss-cn-shanghai-internal.aliyuncs.com/data/path/
NAS:        nas://29**d-b12****446.cn-hangzhou.nas.aliyuncs.com/data/path/
极速 NAS:   nas://29****123-y**r.cn-hangzhou.extreme.nas.aliyuncs.com/data/path/
CPFS:       cpfs://cpfs-213****87.cn-wulanchabu/ptc-292*****cbb/exp-290********03e/data/path/
智算 CPFS:  bmcpfs://cpfs-290******foflh-vpc-x****8r.cn-wulanchabu.cpfs.aliyuncs.com/data/path/
```

### 2.2 怎么判「挂的是根目录」

按 scheme 剥掉「定位段」，看剩下的相对路径：

| scheme | 定位段 | 根目录长什么样 |
|---|---|---|
| `cpfs://` | `<fs-id>.<region>` + `/<ptc-xxx>` + `/<exp-xxx>` | `cpfs://cpfs-xxx.cn-wulanchabu/ptc-xxx/exp-xxx/` —— **exp 段之后没有任何目录** |
| `bmcpfs://` | `<host>`（一个域名，含 `.cpfs.aliyuncs.com`） | `bmcpfs://cpfs-xxx-vpc-xxx.cn-wulanchabu.cpfs.aliyuncs.com/` |
| `nas://` | `<host>`（`.nas.aliyuncs.com`） | `nas://xxx.cn-hangzhou.nas.aliyuncs.com/` |
| `oss://` | `<bucket>.<endpoint>` | `oss://bucket.oss-xxx.aliyuncs.com/` |

判定式（【推断】，由上面的格式推出，建议先拿一条已知正常实例的 Uri 对一遍）：剥定位段后 `path.strip('/') == ''` ⇒ 根绑定；`path` 的目录层数 = 绑定深度。注意 `cpfs://` 比 `bmcpfs://` 多两段（`ptc-`/`exp-`），**用通用 URL 解析器会把 `ptc-xxx` 当成第一级目录 ⇒ 把根绑定误判成深度 2**，这正好是「数据集侧看起来都是深度 1–2」那种假象的来源之一。**这条一定要按 scheme 分开写。**

同时记录 `MountAccess`：`RW` + 根 Uri = 容器里对整个文件系统根可写（叠加 DSW 容器默认 root + POSIX mkdir，RAM 完全管不着，这点你已经知道）。

### 2.3 第二个藏身处：DynamicMount【实测】

实例对象上除了 `Datasets[]` 还有 `DynamicMount`：

```
DynamicMount { enable: bool, mount_points: [ DynamicMountPoint { root_path: str (required), options: str } ] }
```

字段名字就叫 **`RootPath`**。官方文档页把 `DynamicMount` 只描述成「动态挂载配置」、没展开子字段，所以很容易被忽略。盘点脚本要同时扫 `Datasets[].Uri` 和 `DynamicMount.MountPoints[].RootPath`。
（缓和因素：文档明写动态挂载**仅只读**、仅支持 OSS/NAS、不支持灵骏智算资源 —— 【文档】[DSW挂载数据集/OSS/NAS/CPFS](https://help.aliyun.com/zh/pai/read-and-write-dataset-data)。所以它不太可能是本次 CPFS 根绑定的载体，但仍要扫，别留死角。）

---

## 3. 调这些接口要什么 RAM 动作

| 目的 | 动作 | 出处 |
|---|---|---|
| 列实例（Public + 自己创建的） | `paidsw:ListInstances` | 【文档】授权信息表，访问级别 get，资源类型「全部资源」，无条件关键字 |
| **列工作空间下所有实例（含别人的私有实例）** | **`paidsw:ListAllInstances`** | 【文档】[附录：角色及权限列表](https://help.aliyun.com/zh/pai/appendix-list-of-roles-and-permissions) DSW 表：「查看实例列表（工作空间下所有）」 |
| 看实例详情 | `paidsw:GetInstance` | 【文档】授权信息表 |
| （参考）建实例 | `paidsw:CreatePostPaidInstance` / `paidsw:CreatePrePaidInstance` | 【文档】角色及权限列表；**这两条不在 OpenAPI 授权信息表里**（表不全） |

出处：<https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-ram>（29 行全表我已解析）与上面的角色附录。

**给采集身份加权限就加这三条**（最小集）：
```json
{ "Effect": "Allow",
  "Action": ["paidsw:ListInstances", "paidsw:ListAllInstances", "paidsw:GetInstance"],
  "Resource": "*" }
```
- `Resource` 必须是 `"*"`：授权信息表里这些动作的「资源类型」全是「全部资源」。（角色附录里同一批动作却写着 `dswinstance/{instanceId}`——两份官方文档对资源级鉴权的说法不一致，详见姊妹报告 `pai-ram-action-prefixes-and-delete-deny.md` 第 4 节；写 `"*"` 在两种情况下都成立。）
- **只给 `ListInstances` 不给 `ListAllInstances` 是本次调查最容易踩的坑**：采集身份会"成功"跑完、返回一份看起来完整的列表，但**别人创建的私有实例根本不在里面**——而越权实例大概率正是别人的私有实例。跑完务必对一下总数（`TotalCount`）与控制台看到的数量。
- `paidsw:ListAllInstances` 与 `ListInstances` 的触发关系（传 `WorkspaceId=ALL` 时是否即检查 `ListAllInstances`）文档没写死，属【推断】。实操上两条都给，别猜。

---

## 4. 追溯「这个实例是从哪个实例克隆来的」

**API 里查不到。**【实测】我把 `alibabacloud_pai_dsw20220101.models` 全文搜过 `clone/Clone/source/Source/origin`，`GetInstanceResponseBody` 的 52 个字段里**没有任何克隆来源/父实例字段**；`ListInstances` 同理。

API 能给的溯源信息只有：

- `UserId` / `UserName` —— 创建者（`ListInstances` 支持 `CreateUserId` 过滤）。
- `GmtCreateTime` / `GmtModifiedTime` —— 创建、最后修改时间。**按时间排序 + 同一个 Uri 分组，就能画出扩散时间线**（谁最早、之后谁跟着建），这是不靠审计日志也能做的近似传播链。
- `Labels`（自定义 key/value）、`Tags` —— 若克隆会复制标签，可能自带线索。【推断】
- `WorkspaceId` / `ResourceId` —— 扩散范围。

**真正的传播链要靠操作审计 ActionTrail**：
- 【文档】PAI 在 ActionTrail 支持列表内，明确含 **PAI-DSW**：<https://help.aliyun.com/zh/actiontrail/product-overview/audit-events-for-machine-learning>（原文列出 PAIStudio、PAI-AI WorkSpace、PAI-EAS、PAI-Flow、PAI-DatasetAcc、PAI-DLC、PAI-Designer、**PAI-DSW**、PAI-Plugin、PAI-FeatureStore、PAI-iTAG 的审计事件）。
- 事件里带调用者身份、来源 IP、请求参数 —— CreateInstance 的请求体含完整 `Datasets`，**能直接看到当时提交的 Uri**，于是可以按时间排出「谁把根 Uri 第一次写进来、后面谁复制了它」。
- ⚠️ 该页把所有 PAI 子产品的事件名合并成一张字母序表，`CreateInstance` 那行的释义写的是「创建数据集加速实例」（属 PAI-DatasetAcc），**没有单独的 DSW 建实例事件名**。所以「DSW 建实例在 ActionTrail 里记作哪个 eventName」我**没能从文档确定**（可能就是 `CreateInstance`，按 serviceName 区分子产品）。【推断】→ 建议直接在 ActionTrail 控制台按时间窗 + 服务名 `PaiDsw`（或搜 `dsw-` 实例 ID）实查一次，比继续查文档快。
- 另一条现成的证据源：`GetInstanceEvents`（`paidsw:GetInstanceEvents`）给的是实例生命周期事件，**不是审计事件、不含调用者**，别指望它做溯源。

---

## 5. DSW 能不能绕过"数据集"直接挂 NAS/CPFS

**能，而且是产品设计。**【文档】`CreateInstance` 的 `Datasets[].Uri` 字段原文：

> **Uri**：存储服务目录的 Uri，**实现直接挂载**，该字段与 DatasetId 互斥。
> （随后列出 OSS / NAS / 极速 NAS / CPFS / 智算 CPFS 五种 Uri 格式）

产品文档也把两者并列成两种正式用法【文档】[DSW挂载数据集/OSS/NAS/CPFS](https://help.aliyun.com/zh/pai/read-and-write-dataset-data)：

> **挂载数据集与直接挂载存储路径区别**：如果需要长期存储、团队协作，选择挂载数据集；如果只是临时任务、快速扩展存储，选择直接挂载存储路径。
> 支持的云产品：对象存储 OSS、文件存储 NAS、文件存储 CPFS。

**这解释了为什么数据集那侧 144 条全是干净的**：根绑定根本没有以「数据集」对象的形式存在过，它只是某个实例 `Datasets[].Uri` 里的一个字符串。**任何只盘点数据集的巡检都永远看不到它。**【推断（强）】

### 「不允许直接挂 NAS」的限制是什么实现的

按我查到的范围，**RAM 侧不存在这种粒度**：
- DSW 授权信息表 29 条、角色附录 40+ 条权限点里，**没有任何一条是关于"挂载哪种存储"的**；挂载只是 `CreateInstance`/`UpdateInstance` 请求体里的一个字段，RAM 只能管住「能不能建/改实例」，管不住「建的时候挂什么」。【文档 + 推断】
- 也没有 `paidsw:*` 的条件关键字可用（授权信息表 29 行的条件关键字列全是「无」）。【文档】

所以那个限制最可能是**控制台前端限制**：官方控制台文档的「直接挂载存储路径」一节**只演示 OSS**（原文：「以挂载对象存储 OSS 为例」「找到**存储挂载**参数。点击 **OSS**，然后选择已创建的 OSS Bucket 路径」），而 NAS/CPFS 在控制台被引导到「数据集挂载」那条路（需要先在 AI 资产管理里建数据集）。【文档（原文）+【推断】（"因此前端不给 NAS/CPFS 直接挂入口"是我的推断，没找到明写"禁止"的条款）】

**由此推出本次事件的机制（【推断（强）】，与用户描述完全吻合）**：
1. 控制台不给"直接挂 CPFS 根目录"的新建入口 → 人们以为挂不了；
2. 但**克隆**会把旧实例的 `Datasets[]`（含 `Uri: cpfs://…/`）原样复制进新实例的 CreateInstance 请求 → 前端校验被绕过，配置照样生效；
3. OpenAPI 直调 `POST /api/v2/instances` 更是完全不受前端约束。

**⚠️ 必须写进结论给决策者的一句话**：只要限制在前端，**关掉控制台入口没有任何防护价值**——克隆和 OpenAPI 两条路都还开着。真正能落地的管控只有三类（都不在 RAM 的「挂载」粒度上）：
1. **网络/存储侧**：CPFS 挂载要求实例 VPC 与文件系统一致（【文档】同上「CPFS 数据集：DSW 实例的 VPC 必须与 CPFS 文件系统一致，否则实例将创建失败」）→ 用 VPC/安全组或 CPFS 侧的导出目录（export/ptc）把可挂范围限死在子目录，是唯一硬约束；
2. **RAM 侧只能一刀切**：Deny `paidsw:CreatePostPaidInstance`/`CreatePrePaidInstance`/`UpdatePostPaidInstance`/`UpdatePrePaidInstance`，改为走审批代建（代价大）；
3. **检测兜底**：定时跑第 1–2 节那套盘点，发现根绑定就告警/清理（治标，但成本最低、今天就能上）。

---

## 6. 盘点脚本要点（给 dev，伪代码）

```python
# 只读。alibabacloud-pai-dsw20220101 已在环境里。
for region in REGIONS:                       # endpoint 带 region，逐地域
    client = Client(Config(ak, sk, region_id=region,
                           endpoint=f"pai-dsw.{region}.aliyuncs.com"))
    page = 1
    while True:
        r = client.list_instances(ListInstancesRequest(
                workspace_id="ALL",          # ← 不给就只看默认工作空间
                resource_id="ALL",           # ← 不给就只看后付费资源组
                page_size=100, page_number=page))
        for inst in r.body.instances or []:
            for ds in inst.datasets or []:
                yield region, inst.instance_id, inst.instance_name, \
                      inst.user_name, inst.gmt_create_time, \
                      ds.dataset_id, ds.uri, ds.mount_path, ds.mount_access
            dm = inst.dynamic_mount
            for mp in (dm.mount_points or []) if dm and dm.enable else []:
                yield ... mp.root_path ...
        if page * 100 >= (r.body.total_count or 0): break
        page += 1
```
判根：按 2.2 的表分 scheme 剥定位段。排序输出按 `gmt_create_time` 升序 → 直接得到扩散时间线。
需要的权限见第 3 节（**别忘 `paidsw:ListAllInstances`**）。

---

## 7. 没查到 / 没验证的

- **克隆动作本身在 API 层是什么**：控制台的"克隆"大概率就是把旧配置回填进 `CreateInstance`（没有 CloneInstance 接口，SDK 里也没有）【实测：SDK 方法列表里无 clone】，但没有文档明写，属【推断】。
- **ActionTrail 里 DSW 建实例的 eventName**：见第 4 节，未确定，建议控制台实查。
- **控制台"存储挂载"是否真的只给 OSS**：文档只演示 OSS，没有明确的"禁止 NAS/CPFS"条款。要确证，让一个有权限的人打开新建实例页截图「存储挂载」区可选项即可（一分钟的事，比我继续查文档可靠）。
- **未做任何真机调用**：本轮我没有跑 `ListInstances`（环境的生产只读闸门拦了 RAM 类调用，且这属于 dev 的采集动作）。上面所有 API 事实来自文档 + 本地 SDK 源码反查，**参数名/路径可以直接照用**。
