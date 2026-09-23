# PAI 工作空间 `ai_hz_gpu`（590221）能不能沿用杭州那套存储

调研人：researcher ｜ 日期：2026-09-23 ｜ 面向：把 590221 登记进 `identity/workspaces.json`

取证方式：全程**只读 GET**，面板机 `120.79.167.166` 上用 `/etc/delivery/refresh.env` 的
`panel-collector` 凭证（只读身份）调云 OpenAPI。**没有创建/修改/删除任何云资源，没有改任何代码或配置文件。**

标注：【实测】本次真机调接口所得（附接口名 + 返回的关键字段）｜【文档】官方明写｜【推测】未证实。

---

## 0. 一句话结论

**能沿用，网络层面是三重实锤，但有一个非网络的硬阻塞：`panel-executor` 不是 590221 的成员。**

| # | 问题 | 结论 |
|---|---|---|
| 1 | CPFS `bmcpfs-00000ub3ici1dnniit2i0` 在 590221 可见可挂？ | **可以**。590221 的配额 UserVpc 的 **vSwitch 和 CPFS 的 VPC 挂载点是同一个** `vsw-bp1fv5lj7oh7hbii0j0ae`；590221 里已有 16 条数据集全指这个挂载点；82 个 DSW 实例里有 2 个**此刻 Running** 且挂着它 |
| 2 | 桶 `wuji-algo-dev-hz` 在 590221 可用？ | **没有冲突**。两个空间都没有任何「默认存储」类配置；590221 现存 DSW 实例已在挂别的 OSS 桶并跑着。唯一未验到的是「公网 endpoint 的 OSS 数据集在灵骏节点上挂」这一步（详见 §3） |
| 3 | 三件套角色在 590221 有效？ | **有效**。590221 的 20 个成员里，8 人正是 `PAI.AlgoDeveloper`+`PAI.AlgoOperator`+`PAI.LabelManager` 三件套 |
| 4 | 现网数据集形状？ | 见 §5。**顺带查出一个已存在的坑**：面板建的数据集 `ImportInfo` 是 `None`，而人工建的都有 —— 这正是 `assets.create_dataset` 自己 docstring 警告的「看得见但挂不上」 |
| 5 | 640957 和 590221 什么关系？ | **都不是空壳，都在用**。640957 是账号的**默认**工作空间（67 人/105 数据集/G59 32 卡 + GU8T 112 卡），590221 是灵骏专用（20 人/16 数据集/GU8T 144 卡/82 个 DSW 实例）。**「只有 590221 有专属配额」这个前提是错的** |

**开工前必须先做的一件事（要人去控制台点，代码改不了）**：
把 `panel-executor` 加进工作空间 590221，角色 `PAI.WorkspaceAdmin`。详见 §6。

---

## 1. 两个工作空间的基本面【实测】

`GET https://aiworkspace.cn-hangzhou.aliyuncs.com/api/v1/workspaces?PageSize=50&Verbose=true`
（ROA，`x-acs-version: 2021-02-04`）→ `TotalCount: 2`，杭州确实只有这两个。

| | `ai_hz_gpu` | `wuji_general_gpu` |
|---|---|---|
| WorkspaceId | **590221** | 640957 |
| 建于 | 2026-04-04 | 2026-07-10 |
| `IsDefault` | `false` | **`true`** |
| `Status` | ENABLED | ENABLED |
| `ResourceGroupId` | rg-acfm2mg3izraeia | rg-acfm2mg3izraeia（同） |
| 成员数 | 20 | 67 |
| 数据集数 | 16（全 BMCPFS） | 105（BMCPFS 57 / OSS 48） |
| DSW 实例 | **82**（3 Running / 4 Failed / 75 Stopped） | 查不到（采集身份在该空间只有 LabelManager，见 §7） |
| `AdminNames` | 深圳舞肌科技、heguanqi、**panel-collector** | 深圳舞肌科技、heguanqi、**panel-executor** + 7 个 PAI 服务角色 |

注意最后一行：**两个面板身份各在一个空间里，正好交叉错开**。

---

## 2. Q1 —— CPFS 在 590221 可见可挂（三重证据）

### 2.1 网络：配额的 vSwitch 就是 CPFS 挂载点的 vSwitch【实测】

`GET https://pai.cn-hangzhou.aliyuncs.com/api/v1/quotas/?WorkspaceIds=590221&Verbose=true`
（PaiStudio，`x-acs-version: 2022-01-12`）：

```
QuotaName        ai_pai_quota      QuotaId quotai7l7gqmouqw
ResourceType     Lingjun           GPUType GU8T   HyperZones ["B1"]
ResourceGroupIds ["lingj11mrdoqesmf"]
QuotaConfig.ClusterId  c6bf9d1b87c1b47ed85ba9fac942316a2
QuotaConfig.UserVpc.VpcId            vpc-bp1w4vnrtyrs76z1uyj4c
QuotaConfig.UserVpc.SwitchId         vsw-bp1fv5lj7oh7hbii0j0ae     ←←←
QuotaConfig.UserVpc.SecurityGroupId  sg-bp1hzayjxnkruu2rg26j
QuotaConfig.WorkloadTypes  ["DLC","DSW","EAS","TENSORBOARD"]
QuotaDetails.ActualMinQuota.GPU 144   AllocatedQuota.GPU 133（18 节点，已分配 17）
```

`nas.cn-hangzhou.aliyuncs.com` / `2017-06-26` / RPC `DescribeFileSystems`
`FileSystemId=bmcpfs-00000ub3ici1dnniit2i0&FileSystemType=cpfs`：

```
FileSystemType bmcpfs   Status Running   RegionId cn-hangzhou
ZoneId cn-hangzhou-b    HpnZone B1       StorageType bm_advance_400
Capacity 184320 (GiB)   Bandwidth 72000  MountTargetCountLimit 1
MountTargets[0]  MountTargetDomain cpfs-00000ub3ici1dnniit2i0-000001.cn-hangzhou.cpfs.aliyuncs.com
                 （Vsc 接入点，Status Active，无 VpcId）
MountTargets[1]  MountTargetDomain cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com
                 NetworkType vpc   Status Active   MountTargetIp 10.32.3.212
                 VpcId vpc-bp1w4vnrtyrs76z1uyj4c
                 VswId vsw-bp1fv5lj7oh7hbii0j0ae                  ←←← 同一个
```

**登记表里 `hz` 那条写的 `mount` 就是第二个挂载点域名**，而它落在
`vsw-bp1fv5lj7oh7hbii0j0ae` 上，正是 590221 那条灵骏配额 `UserVpc.SwitchId` 的值。
同 VPC 同 vSwitch，IP `10.32.3.212` 就在这个交换机网段里 —— 不需要任何 `ExtendedCIDRs` 兜底。

对照 640957 的两条配额：同 VPC `vpc-bp1w4vnrtyrs76z1uyj4c`、同安全组，但 vSwitch 是
`vsw-bp1xeradsv3dr8qxmiarn`（灵骏 112 卡）/ `vsw-bp1lfi9ptl53nemgqqzji`（ECS 5090），
它们靠 `UserVpc.ExtendedCIDRs`（含 `10.32.0.0/20`，覆盖 10.32.3.212）打通。
**590221 的网络条件比已登记的 640957 更直接，不是更差。**

另：CPFS 的 `HpnZone` 是 `B1`，590221 配额的 `HyperZones` 也是 `["B1"]` —— 灵骏超级区一致。

`MountTargetCountLimit: 1` ⇒ 这台 CPFS **只能有一个 VPC 挂载点**，也就是说不存在「给 590221
另开一个挂载点」这条路；幸好不需要。

### 2.2 存量数据集：590221 里 16 条全指这个挂载点【实测】

`GET /api/v1/datasets?WorkspaceId=590221` → 16 条，`DataSourceType` 全是 `BMCPFS`，
`Uri` 的 host 全是 `cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com`，
`ProviderType` 全是 `Lingjun`。最早 2026-07-20，最近 2026-09-04。

### 2.3 运行时：此刻有实例正挂着它【实测】

`GET https://pai-dsw.cn-hangzhou.aliyuncs.com/api/v2/instances?WorkspaceId=590221&ResourceId=ALL`
（`2022-01-01`，ROA）→ `TotalCount 82`。三个 `Running` 的：

```
codex_hz_vitra_validate  贺贯齐  Datasets[0].Uri bmcpfs://cpfs-…-vpc-egtdgw.…/  → /cpfs/
ant_hz_h20_4g_try1       贺贯齐  同上（GU8T 卡上跑）                            → /cpfs/
data-line-test           主账号  bmcpfs://cpfs-…-000001.…/ + 4 个 oss://…-internal 桶
```

**这是最强的一条**：不是「配置看着对」，是数据面此刻真的挂着在跑。

唯一那台挂 `DatasetId`（而不是裸 URI）的实例 `w0_sft_zhangyulin_clone5` 状态是 `Failed`，
但原因是 `ReasonCode: InternalError` / `ReasonMessage: Resource queue timeout, lack of
sufficient resources` —— **排队等卡超时，不是挂载失败**。不构成反证。

---

## 3. Q2 —— 桶 `wuji-algo-dev-hz`

- 桶存在且在杭州【实测】：`ListBuckets`（region `oss-cn-hangzhou`）返回
  `{'name': 'wuji-algo-dev-hz', 'region': 'oss-cn-hangzhou', 'created': '2026-09-18T08:43:06.000Z'}`。
- **两个工作空间都没有「默认存储」类配置**【实测】：
  `GET /api/v1/workspaces/590221/configs` → 只有 2 条 `slsConfigDLC` / `slsConfigDSW`
  （都指向 SLS project `ai-pai-gpu-hz-sls`，与对象存储无关）；
  `GET /api/v1/workspaces/640957/configs` → `TotalCount 0`。
  **所以不存在「590221 绑了别的默认桶会冲突」这回事。**
- OSS 桶是账号级资源，PAI 侧没有任何「桶↔工作空间」绑定关系【文档常识 + 上面 configs 实测佐证】。
- 590221 的实例**已经在挂 OSS**【实测】：`data-line-test`（Running）同时挂了
  `wuji-bucket-hangzhou` / `wuji-data-tran` / `wuji-ego-processed` / `wuji-egocentric-processed`
  四个桶，用的是 `oss-cn-hangzhou-internal.aliyuncs.com` 内网 endpoint。

**唯一没验到的一格**【推测】：590221 里目前**一条 OSS 数据集都没有**（16 条全是 BMCPFS），
而面板 `datasets_for()` 写的 URI 用的是**公网** endpoint
`oss://<桶>.oss-cn-hangzhou.aliyuncs.com/<组>/<人>/`。
640957 里 48 条这种形状的 OSS 数据集都是这么写的，而 640957 的灵骏配额
和 590221 **同一个资源组 `lingj11mrdoqesmf`、同一个集群 `c6bf9d1b…`**，
所以几乎可以肯定没问题 —— 但这一条是推的，不是测的。**要证实只能真建一条，那是写操作，本次没做。**

- 顺带：`panel-collector` 读不了这个桶的对象列表（`oss.list_prefixes` 回
  `AccessDenied: The bucket you access does not belong to you`）—— 采集身份的 OSS 策略里没有它，
  与本次结论无关，只是说明「桶内目录清单」这条证据链我拿不到。写侧的 `panel-executor` 有
  （见 §6 的 `panel-workspaces` 策略）。

---

## 4. Q3 —— 590221 的成员与角色

`GET /api/v1/workspaces/590221/members` → 20 条【实测】。角色分布：

| 角色组合 | 人数 |
|---|---|
| `PAI.AlgoDeveloper` + `PAI.AlgoOperator` + `PAI.LabelManager`（**面板的三件套**） | 8 |
| `PAI.AlgoDeveloper` + `PAI.LabelManager` | 6 |
| `PAI.AlgoDeveloper` | 3 |
| `PAI.LabelManager`+`PAI.AlgoOperator`+`PAI.AlgoDeveloper`+`PAI.WorkspaceAdmin` | 1（heguanqi） |
| `PAI.WorkspaceOwner` + `PAI.WorkspaceAdmin` | 1（主账号，AccountType=1） |
| `PAI.WorkspaceAdmin` | 1（**panel-collector**） |

⇒ `workspaces.DEFAULT_ROLES` 那三个名字在 590221 里**逐字有效、且是最常见的那一套**。

名单（AccountName）：zhangxiaoxiong、zhangwentao、qianbinsheng、leyang、liangjiaqi、caomaosong、
jake、jizexian、zhangyulin、chuzhong、heguanqi、wangzihan、kouchenran、zhuzijie、pengjingwei、
zhuang.yihong、power-application-user、panel-collector、主账号，外加 1 条
`AccountName: null`（UserId `208310175366414861`，疑似已删号或非 RAM 用户）。

**20 人 vs 640957 的 67 人，是子集关系但不完全**：caomaosong 在两边都有；
而 640957 里的 huangsiqiao、yubohua、wubo 等大批人不在 590221 里。

**一个顺带的观察（不是本次任务，但该记一笔）**：`panel-collector` 在 590221 是
`PAI.WorkspaceAdmin`。它的 RAM 策略是纯只读（`wuji-panel-collector` 显式 `Deny` 一切写动作），
所以现在没有实际风险，但工作空间角色给到 Admin 比采集需要的多。要收的话给 `PAI.AlgoDeveloper`
够用 —— 但注意收了之后 §2.3 那条 DSW 实例证据链就拿不到了（列别人的实例要 Admin）。

---

## 5. Q4 —— 现网数据集的形状（照着填最保险的那份）

### 5.1 590221 的 16 条（全是人工在控制台建的）【实测】

```
Name              xiaoxiong / zhangwt / qianbsh / … / share / lakefs-server / _lakefs_cache / ANT
DataSourceType    BMCPFS
Property          DIRECTORY
Uri               bmcpfs://cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com/<目录>/
ImportInfo        {"path":"/<目录>/","fileSystemId":"bmcpfs-00000ub3ici1dnniit2i0",
                   "isVpcMount":true,"region":"cn-hangzhou",
                   "mountTarget":"cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com"}
Options           {"mountPath":"/mnt/data/"}
Accessibility     ROLE_PUBLIC ×12 / PUBLIC ×4
AccessibleRoleIdList  ["PAI.WorkspaceAdmin","PAI.AlgoOperator","PAI.LabelManager","PAI.AlgoDeveloper","owner"]
MountAccess       RO ×13 / RW ×3
ProviderType      Lingjun（16/16）
Labels            []（全空 —— 590221 里一条带 kind/owner 标签的都没有）
```

命名是**扁平的个人目录名**（`xiaoxiong`、`zhangwt`、`qianbsh` 这种缩写），
和面板的 `<登录名>` 规则不完全一致，但那是历史人工命名，面板按自己的规则建不冲突。

### 5.2 640957 里**面板建的**那批，和人工建的不一样 —— 这是个真坑【实测】

| 数据集 | 建于 | 建的人 | `ImportInfo` |
|---|---|---|---|
| `wzh`（人工） | 2026-07-20 | 人 | ✅ 完整 |
| `yujichuan-cpfs` | 2026-09-20 | 人/脚本 | ✅ 完整 |
| `wangzihan-oss` | 2026-09-18 | 面板批量 | ✅ `{"bucket":"wuji-algo-dev-hz","path":"general/wangzihan/","region":"cn-hangzhou"}` |
| **`zhuang.yihong`**（BMCPFS） | 2026-09-22 | **面板** | ❌ **`None`** |
| **`zhuang.yihong-oss`** | 2026-09-22 | **面板** | ❌ **`None`** |
| **`huang.zenan`**（BMCPFS） | 2026-09-22 | **面板** | ❌ **`None`** |
| **`huang.zenan-oss`** | 2026-09-22 | **面板** | ❌ **`None`** |

`assets.create_dataset` 的 `import_info` 参数**有默认值 `None`，而 `provision.create_dataset`
根本不传它** —— 于是面板最近建的每一条数据集都没有 `ImportInfo`。
而该函数自己的注释写着：

> `ImportInfo`：**PAI 真正拿去挂载的东西。** 不带的话很可能建出一条「看得见但挂不上」的数据集

也就是说，**任务描述里担心的那个「建的时候像成功、挂载时才炸」，在 640957 里可能已经发生了**，
和登不登记 590221 无关。9/18 那批 48 条 OSS 是带 `ImportInfo` 的（大概是另一条代码路径或手工脚本），
9/22 那 4 条不带。

> 这条**超出本次取证范围**，我没有验证「`ImportInfo=None` 的数据集到底挂不挂得上」——
> 那要真起一个 DSW 实例去挂，是写操作。建议 dev 找 zhuang.yihong / huang.zenan 两位实地试一次，
> 或者直接把 `import_info` 补进 `provision.create_dataset` 的调用（照 §5.1 的 JSON 形状拼）。
> **登记 590221 会让这个已有缺陷影响到第二个空间，所以最好先修再登记。**

另：面板建的 BMCPFS 数据集 `ProviderType` 是 `Ecs`，人工建的是 `Lingjun`。这个字段是 PAI 自己
推的，`CreateDataset` 请求里没有它，【推测】跟调用者所在空间的默认配额类型有关，暂不认为是问题。

---

## 6. 硬阻塞：`panel-executor` 不是 590221 的成员

### 6.1 事实【实测】

- 590221 的 20 个成员里**没有 `panel-executor`**（名单见 §4），`AdminNames` 里也没有。
- 640957 的成员里有：`panel-executor | 云账号面板执行身份 | AccountType 5 | PAI.WorkspaceAdmin`。
- PAI 的鉴权是 **RAM ∧ 工作空间 RBAC 双层**。同一把凭证换个空间就被拒，本次直接复现了：
  `panel-collector` 能列 590221 的 DSW 实例（它在那儿是 Admin），
  在 640957（只有 `PAI.LabelManager`）就被拒：

  ```
  NoPermissionError: No permission ListInstances for resource:
  acs:paidsw:cn-hangzhou:1704065796538912:workspace/640957
  ```

  `assets.py` 里那段注释说的 `denied by RAM and AIWorkspace Rbac` 是同一回事。

### 6.2 RAM 侧**不用改**【实测】

`panel-executor` 挂着 4 条自定义策略，其中 `panel-workspaces`（v4）正文：

```json
{"Action":["oss:GetBucketInfo","oss:GetBucketLocation","oss:GetObject","oss:ListObjects",
           "oss:PutObject","paiworkspace:CreateMember","paiworkspace:GetWorkspace","ram:GetUser"],
 "Resource":["acs:oss:*:*:wuji-algo-dev-hz","acs:oss:*:*:wuji-algo-dev-hz/*",
             "acs:oss:*:*:wuji-algo-dev-sing","acs:oss:*:*:wuji-algo-dev-sing/*"],
 "Effect":"Allow","Condition":{"IpAddress":{"acs:SourceIp":["120.79.167.166"]}}},
{"Action":["paidataset:CreateDataset","paidataset:ListDatasets","paidataset:GetDataset",
           "paiworkspace:ListWorkspaces","paiworkspace:ListMembers"],
 "Resource":"*","Effect":"Allow","Condition":{…同上 IP 限制…}}
```

`paiworkspace:CreateMember` / `paidataset:CreateDataset` 的 Resource 都不带工作空间维度
（`CreateMember` 那条虽然和 OSS 资源写在同一条 statement 里、Resource 只列了桶，
但 PAI 动作对 OSS ARN 不匹配 ⇒ 这条对 PAI 实际等价于「有这个动作但 Resource 对不上」—— 
真正生效的是第二条 `Resource:"*"` 里的 `paidataset:*`；`CreateMember` 目前**只有**
被桶 ARN 限住的那一条）。

**这条路跑通过，有现网证据**【实测】：新号 `zhuang.yihong`（REQ-20260921-C22BACDF）和
`huang.zenan`（REQ-20260921-05259199）现在都是 640957 的成员，角色**正好是
`PAI.AlgoDeveloper`+`PAI.AlgoOperator`+`PAI.LabelManager` 三件套**（= `DEFAULT_ROLES`），
并且 2026-09-22T05:44 各有两条数据集 `<登录名>` / `<登录名>-oss`（= `datasets_for()` 的命名规则），
`UserId` 记的是他们本人的 RAM UserId 而不是面板 —— 而设 `UserId` 要求调用者是该空间的 Owner/Admin。
⇒ **`paiworkspace:CreateMember` + `paidataset:CreateDataset` 在 RAM 这一层是通的，
挡住 590221 的只有工作空间 RBAC 这一层。**

（顺带一个观察，不在本次范围：这两张**开号工单的 events 里没有任何「已加入工作空间/已开个人目录/
已建数据集」记录**，而资源是 9/22 才出现的 —— 看起来当时是靠 `backfill` 补的，
开号主流程那一步没跑或没落事件。dev 可以顺手看一眼。）

`wuji-algo-dev-hz` 已经在桶白名单里 ⇒ **个人目录（`make_dir` → `oss:PutObject`）不用加权限。**

### 6.3 要做什么（人工，一次性，云控制台）

> 这是**写操作，我没做也不该做**。列在这里交给用户/dev。

1. PAI 控制台 → 工作空间 `ai_hz_gpu`（590221）→ 成员管理 → 添加成员
   `panel-executor`，角色勾 **`PAI.WorkspaceAdmin`**。
   - 为什么必须是 Admin 而不是 AlgoDeveloper：`assets.create_dataset` 要显式传 `UserId`
     把数据集属主记成申请人，而**设 `UserId` 要求调用者是该工作空间的 Owner 或 Admin**
     （代码注释里写着「线上已经因为这个返工过一次，49 条属主全是 panel-executor」）。
     给低了不会报错，会静默把属主记成面板自己，且事后改不回来。
2. 做完可以只读复验（我可以代跑）：
   `GET /api/v1/workspaces/590221/members` 里出现 `panel-executor` / `PAI.WorkspaceAdmin`。

---

## 7. Q5 —— 640957 和 590221 到底什么关系

**两个都是活的，谁也不是空壳。** 分工看起来是：

| | 590221 `ai_hz_gpu` | 640957 `wuji_general_gpu` |
|---|---|---|
| 定位 | 灵骏 GU8T 专用（建于 4 月，比 640957 早 3 个月） | 账号**默认**工作空间，面板开号的落点 |
| 配额【实测】 | `ai_pai_quota` Lingjun GU8T **144 卡**（18×`ml.gu8tf.8.46xlarge`），已分配 133 | `wuji-general-gpu` Lingjun GU8T **112 卡**（14 节点）已分配 99；`wuji-hz-5090` ECS G59 **32 卡**（4×`ecs.ebmgn9t.48xlarge`）已分配 20 |
| 灵骏资源组 | `lingj11mrdoqesmf` | `lingj11mrdoqesmf`（**同一个**） |
| 灵骏集群 | `c6bf9d1b87c1b47ed85ba9fac942316a2` | 同一个 |
| 人 | 20（老班底 + 主账号 + panel-collector） | 67（全员 + 一堆服务角色 + panel-executor） |
| 数据 | 16 条，全 CPFS，无标签 | 105 条，CPFS 57 + OSS 48，面板建的带 `kind/owner/group` 标签 |

**要纠正任务背景里的一个前提**：「590221 挂着 144 张 GU8T，另外几个空间没有专属配额」
—— 前半句对，后半句不对。640957 有 112 张 GU8T + 32 张 G59，两边加起来 288 卡，
都挂在**同一个灵骏资源组**下，只是切成了两条队列。所以这不是「真有卡的 vs 空壳」，
是**同一批卡按队列分给了两拨人**。

【推测】历史顺序大概是：4 月先有 `ai_hz_gpu` 跑灵骏 → 7 月建 `wuji_general_gpu` 当默认空间、
把大部分人和数据搬过去（16 条数据集里有 11 个目录名在 640957 里有同名的） →
9 月面板接手开号，落点选了默认空间 640957。**登记 590221 = 让新人同时进这条更老、更专的灵骏队列。**

---

## 8. 可以粘进 `identity/workspaces.json` 的那段

> **我没有改这个文件。** 下面是建议内容，请 dev 自行决定 key 名和是否加 `bucket_prefix`。

`workspaces` 下加一条（key 受 `workspaces._KEY` 约束：小写字母开头、只能小写字母/数字/横线、≤24 字符）：

```json
    "hz-gpu": {
      "label": "杭州·灵骏",
      "id": "590221",
      "region": "cn-hangzhou",
      "mount": "cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com",
      "bucket": "wuji-algo-dev-hz",
      "bucket_region": "cn-hangzhou"
    }
```

逐字段核对过 `workspaces._one()` 的校验：
- `id` 全数字 ✅
- `region` 匹配 `^[a-z]{2}-[a-z]+(-\d+)?$`、不带 `oss-` 前缀 ✅
- `mount` 匹配 `^[A-Za-z0-9][A-Za-z0-9.\-]*\.cpfs\.aliyuncs\.com$`、不带 `bmcpfs://` ✅
  （和 `hz` 那条**逐字相同** —— 同一台 CPFS 同一个挂载点，见 §2.1）
- `bucket` 匹配桶名正则 ✅；给了 `bucket` 就必须给 `bucket_region` ✅
- `roles` / `bucket_prefix` 省略 → 走 `defaults`（三件套 + `general`），三件套在 590221 有效（§4）
- `quota_gib` 可省；顺带一提这个字段**只在 `workspaces.py` 里被解析，全仓库没有第二处引用**，
  填了也不起作用

**还要改第二处**：`identity/request-templates.json` 里 `aliyun-new-user` 模板的
`"workspaces": ["hz"]` → `["hz", "hz-gpu"]`，否则登记表加了也不会有人被开进去。
（`workspaces.py` 的模块注释写着「加地域 = 加一条 + 在模板里填个 key」。）

**已有账号要补**：`delivery requests backfill --region hz-gpu`（先不带 `--apply` 看一遍）。

### 副作用提醒（不是问题，但要知道）

1. 同一个人会在 **590221 和 640957 各有一套同名数据集** —— 数据集名在空间内唯一，不冲突，
   但控制台上会看到两份。
2. OSS 个人目录用的是**同一个桶同一个前缀**（`wuji-algo-dev-hz/general/<人>/`），
   `make_dir` 是 `PutObject` 一个 0 字节占位，重复执行无副作用。
3. 两个空间的 CPFS 个人目录也是**同一个物理目录**（同一台 CPFS 同一路径）—— 这大概正是想要的效果。

---

## 9. 采集身份（`panel-collector`）的权限缺口清单

本次被拒的只读接口，**都不影响上面的结论**，列出来供决定要不要补：

| 接口 | 报错 | 缺的动作 | 要不要补 |
|---|---|---|---|
| `nas:DescribeMountTargets` | `Forbbiden.Ram` on `acs:nas:cn-hangzhou:…:filesystem/bmcpfs-…` | `nas:DescribeMountTargets` | **不用** —— `DescribeFileSystems` 已经把 `MountTargets[]` 全带出来了 |
| `nas:DescribeDataFlows` / `nas:DescribeFilesets` | 同上 | 对应动作 | 看 CPFS 预热/沉降那条线要不要采集，本次不需要 |
| `pai:ListResourceGroups`（`GET pai.…/api/v1/resources`） | `RAMForbidden … Code:[4001]` | `pai:ListResourceGroups` | 想采「资源组 ↔ 配额」拓扑才需要；`ListQuotas` 已够 |
| `paidsw:ListInstances` on workspace **640957** | `NoPermissionError` | **不是 RAM 问题，是工作空间角色**（在 640957 只有 LabelManager） | 要盘点 DSW 挂载就得升角色，见 §4 末尾的取舍 |
| `oss:ListObjects` on `wuji-algo-dev-hz` | `AccessDenied: bucket does not belong to you` | `oss:ListObjects` + `oss:GetBucketInfo` 该桶 | 本次不需要 |

`nas:DescribeFileSystems`、`pai:ListQuotas`、`paiworkspace:*` 只读、`paidataset:ListDatasets`、
`ram:ListUsers/GetPolicy/…` **现在都通**，不用补。

---

## 10. 没验到的 / 结论的边界

1. **公网 endpoint 的 OSS 数据集在 590221 的灵骏节点上挂不挂得上** —— 只能靠建一条真数据集
   + 起一个实例验，是写操作。旁证很强（同集群同资源组的 640957 有 48 条在用），但没直测。
2. **`ImportInfo=None` 的数据集到底挂不挂得上** —— 同上，要真挂一次。640957 里有 4 条现成样本
   （zhuang.yihong / huang.zenan 各两条），找他们试最省事。
3. **`panel-executor` 加进 590221 之后 `CreateMember` / `CreateDataset` 是否真能过** ——
   我只读了策略正文，没法在只读前提下确认 PAI 动作对桶 ARN 那条 statement 的匹配行为（§6.2）。
   第一次开号时盯一眼日志就知道。
4. 本次只看了 `cn-hangzhou`。新加坡 284761 没动。
