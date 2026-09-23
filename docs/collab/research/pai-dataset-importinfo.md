# PAI 数据集 `ImportInfo` 缺失到底有没有后果 —— 真机验证

调研人：researcher ｜ 日期：2026-09-23 ｜ 面向：面板开号建数据集漏传 `ImportInfo`

取证方式：面板机 `120.79.167.166` 上调云 OpenAPI（两份策略都带 `acs:SourceIp` 条件，只能在那台机上发）。
本轮做了**三个写操作**：建/删两台临时 DSW 实例（`panel-importinfo-suspect` / `panel-importinfo-control`，均已删）、
对 `zhuang.yihong-oss` 一条数据集发了两次 `UpdateDataset`/`UpdateDatasetVersion`（结果是静默 no-op，见 §2）。
**没有删除任何数据集、没有建新数据集、没有动 RAM、没有改任何配置文件。**

标注：【实测】本次真机所得（附接口 + 返回原文）｜【文档】官方明写（附 URL）｜【推测】未证实。

---

## 0. 一句话结论

| 问题 | 结论 |
|---|---|
| Q1 `ImportInfo=None` 的数据集挂不挂得上？ | **挂得上。**【实测】拿面板 9/22 建的那两条（CPFS `zhuang.yihong` + OSS `zhuang.yihong-oss`）挂进一台新 DSW，实例正常 `Running`，CPFS 挂载点和 ossfs2 都正常挂起。对照组（同工作空间、同文件系统、**带** `ImportInfo` 的 `caomaosong-cpfs`/`-oss`）同样 `Running`。**两边都成功 ⇒ `ImportInfo` 不是挂载必需**，`assets.create_dataset` 那句「不带很可能建出一条看得见但挂不上的数据集」**在挂载这件事上不成立**，注释该改 |
| Q2 `UpdateDataset` 能补写 `ImportInfo` 吗？ | **不能，而且是静默无效。**【实测】`PUT /api/v1/datasets/{id}` 和 `PUT /api/v1/datasets/{id}/versions/v1` 都只回一个 `RequestId`、`GmtModifiedTime` 确实被刷新了（说明请求被受理），但回读 `ImportInfo` 仍是 `null`。和已知的 `UserId` 那个坑**完全同一个形状**。【文档】两个接口的参数表里本来就没有 `ImportInfo`。**唯一可能的补救是 `CreateDatasetVersion`（v2），它的参数表里有 `ImportInfo`** —— 本轮没执行（超出批准的写范围） |
| Q3 那 4 条是哪条路径建的？ | **管理员手跑的 `delivery requests backfill --region hz --who … --apply`（`cli_requests.py:1946`），不是开号主流程。** 证据链见 §3。**主流程当时压根没跑这一步** —— 两张单的模板快照里**根本没有 `workspaces` 字段**，`workspaces` 这个能力是 9/22 的提交 `4c71320` 才加的，比两张单晚了一天 |

**附带捞到的两条，比上面三问更值钱：**

- **【实测】`RequestedResource.Memory` 不带 `GB` 单位会把实例弄挂，而报错完全指向别处。** 我头两次实验都失败在这里，差点误判成「数据集挂不上」。详见 §4。
- **【实测】`panel-executor` 在 640957 上能 `CreateInstance` / `DeleteInstance` DSW**，权限比 `identity/executor-policy.aliyun-workspaces.json` 那份参考副本宽得多（那份里一条 `paidsw:*` 都没有）。给 auditor 记一笔。

---

## 1. Q1 —— 真机挂载实验

### 1.1 为什么对照组不用 590221，改用 640957 里的 9/18 批

原计划是拿 `ai_hz_gpu`(590221) 里人工建的数据集做对照。查下来有更好的对照**就在同一个工作空间里**：

| | 嫌疑组 | 对照组（本轮采用） | 原计划对照组（弃用） |
|---|---|---|---|
| 数据集 | `zhuang.yihong` / `zhuang.yihong-oss` | `caomaosong-cpfs` / `caomaosong-oss` | 590221 的 `xiaoxiong` 等 16 条 |
| 工作空间 | 640957 | **640957（同）** | 590221（不同） |
| `ProviderType` | `Ecs` | **`Ecs`（同）** | `Lingjun`（不同） |
| `MountAccess` | `RW` | **`RW`（同）** | `RO`（不同） |
| `Labels` | kind/owner | **kind/owner（同）** | 空 |
| 文件系统 | `bmcpfs-00000ub3ici1dnniit2i0` | **同** | 同 |
| **`ImportInfo`** | **`null`** | **有** | 有 |

用 590221 那批的话，一次实验里同时变了四个量（工作空间 / ProviderType / MountAccess / ImportInfo），
失败了也说不清是哪个造成的。9/18 那批（CLI `delivery workspaces --apply` 建的）**只差 `ImportInfo` 一个变量**。

顺带一个此前没人提过的事实：**640957 里 73 台 DSW 实例挂过的数据集，全部是 `ProviderType=Lingjun` 的那批人工数据集；
面板/CLI 建的 57 条（`-oss`/`-cpfs` 后缀 + 那 4 条新的）一条都没被挂过。** 也就是说
「带 ImportInfo 的面板数据集能挂」在本轮之前也只是假设，不是既成事实 —— 这正是必须跑对照组的理由。

### 1.2 实验配置

```
POST https://pai-dsw.cn-hangzhou.aliyuncs.com/api/v2/instances   (ROA, x-acs-version: 2022-01-01)
{
  "InstanceName": "panel-importinfo-suspect",
  "WorkspaceId": "640957",
  "ResourceId": "quotauwiiveke73h",          // 灵骏配额 wuji-general-gpu
  "Accessibility": "PRIVATE",
  "ImageId": "image-q2wuy9roda8t5rcpeu",     // 官方 python:3.12.12-cpu-ubuntu22.04, 1.28GB
  "Priority": 1,
  "RequestedResource": {"CPU":"4","GPU":"0","Memory":"16GB","SharedMemory":"16GB"},
  "Datasets": [
    {"DatasetId":"d-rn9gtup4dk0ywl61db","DatasetVersion":"v1","MountPath":"/mnt/a","MountAccess":"RW"},
    {"DatasetId":"d-v51blhtpthmbzse0jt","DatasetVersion":"v1","MountPath":"/mnt/c","MountAccess":"RW"}
  ],
  "UserVpc": {"VpcId":"vpc-bp1w4vnrtyrs76z1uyj4c","VSwitchId":"vsw-bp1xeradsv3dr8qxmiarn",
              "SecurityGroupId":"sg-bp1hzayjxnkruu2rg26j",
              "ExtendedCIDRs":["10.32.255.248/29","10.32.129.0/24","10.32.0.0/20"]}
}
```

- 选灵骏配额而不是 ECS 配额 `wuji-hz-5090`：这个配额上**已经有跑着的实例在挂同一个 `bmcpfs://…-vpc-egtdgw` 挂载点**，
  网络路径是被现网证明过的；换 ECS 配额就又多一个没验过的变量。
- `GPU: 0` 有现网先例（`pengjw-data-dist_4`，同配额，`AcceleratorType=CPU`），不占卡。
- `UserVpc` 照抄配额自己的 `QuotaConfig.UserVpc`，`ExtendedCIDRs` 含 `10.32.0.0/20`，覆盖 CPFS 挂载点 IP `10.32.3.212`。

### 1.3 结果

**嫌疑组（`ImportInfo=null`）→ `Running`**，实例 `dsw-rjrnbqojiy2ots7q15`：

```
Queuing → EnvPreparing → Running        ReasonCode=""  ReasonMessage=""
Datasets: [{DatasetId: d-rn9gtup4dk0ywl61db, MountPath: /mnt/a, ActualMountAccess: RW},
           {DatasetId: d-v51blhtpthmbzse0jt, MountPath: /mnt/c, ActualMountAccess: RW}]
```

`GetInstanceEvents` 原文（关键三行）：

```
17:16:42: VSC mountpoints attached with filesystem IDs: [bmcpfs-00000ub3ici1dnniit2i0]
17:16:42: All mountpoints have been successfully attached to VSC or VPC vsc-bp10n3w9mz7ys4uslb1mal in 3.425432987s.
17:17:04: Started container fluid-fuse-0          ← 镜像 pai-common/ossfs2:2.0.2-251209，OSS 那条
17:17:08: Started container dsw-notebook
```

**对照组（带 `ImportInfo`）→ 同样 `Running`**，实例 `dsw-j04be12w0lkqkz1ni1`，事件序列一致。

两台实例验完立即 `DELETE /api/v2/instances/{id}`，复查 `ListInstances` 已无 `panel-importinfo-*` 残留。

### 1.4 这个结论能撑多重

**撑得住的部分**：数据集挂载在 DSW 里是 pod 启动的硬前提 —— CSI/VSC 挂载点挂不上、fuse 起不来，
pod 会卡在 `ContainerCreating` 或直接 `FailedMount`，实例不可能进 `Running`。
本轮事件里既没有任何 `FailedMount`，也明确出现了「挂载点已挂上」和「ossfs2 容器已启动」。
所以「`ImportInfo=None` ⇒ 挂不上」**被证伪**。

**没验到的部分（要如实说）**：我**没能在容器里 `ls /mnt/a`**。试过三条路，都不通：
- `GetToken`（`GET /api/v2/tokens`）拿到的 token，往 JupyterLab 网关
  `https://dsw-gateway-cn-hangzhou.data.aliyuncs.com/dsw-2205951/api/contents/...` 发
  —— cookie / `Authorization: Bearer` / `x-dsw-token` / query 四种写法全部被重定向到**阿里云控制台登录页**（返回的是 HTML 登录页，HTTP 200）。
- `ListSystemLogs`（`GET /api/v2/systemlogs`）在 `pai-dsw` 2022-01-01 上返回 `404 InvalidAction.NotFound`。
- `UserCommand.OnStart` 能下发命令，但 `GetUserCommand` 只回显命令正文（`on_start.content`），**不回执行输出**。

唯一还能拿到容器内 `ls` 的办法是：① 开 `UserVpc.ForwardInfos` 的 SSH 转发（会在他们 VPC 上新建 NAT DNAT + EIP、
把一个公网可达的 SSH 端口开进生产 VPC）；或 ② 让 OnStart 把 `ls` 结果写进某个挂上来的 OSS 目录再从外面读
（会往别人的个人目录里留一个删不掉的文件 —— 开通身份没有 `oss:DeleteObject`）。
两条都超出了批准的写范围，我没做。**要补这一步，最省事的是让有控制台权限的人开一台实例、看一眼 `/mnt` 截个图。**

补充一句：就算真进去看了，`zhuang.yihong` 那两个目录本来就是**空的**（CPFS 目录挂载时才自动建、OSS 前缀下只有一个 0 字节占位对象），
「看得到内容」这件事在这两条上本来就没什么可看 —— 真要验「内容可见」，得挑一条底下有数据的。

---

## 2. Q2 —— `UpdateDataset` 补不了 `ImportInfo`

【文档】两个接口的请求参数表里都没有 `ImportInfo`：
- UpdateDataset：`PUT /api/v1/datasets/{DatasetId}`，只收 `Name` / `Description` / `Options` / `Edition` /
  `MountAccessReadWriteRoleIdList` / `SharingConfig`
  — <https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-updatedataset>
- UpdateDatasetVersion：`PUT /api/v1/datasets/{DatasetId}/versions/{VersionName}`，只收 `Options` / `Description` /
  `DataSize` / `DataCount` / `DatasetTaskRamRole` / `UserMetricsEndpoints`
  — <https://www.alibabacloud.com/help/en/pai/developer-reference/api-aiworkspace-2021-02-04-updatedatasetversion>

【实测】照样发了一遍（拿 `zhuang.yihong-oss` = `d-v51blhtpthmbzse0jt`），两次都是**收下、不报错、什么都没改**：

```
[before] dataset.ImportInfo=None  version.ImportInfo=None  GmtModified=2026-09-22T05:44:24.612Z
PUT /api/v1/datasets/d-v51blhtpthmbzse0jt
     body={"ImportInfo":"{\"bucket\":\"wuji-algo-dev-hz\",\"path\":\"general/zhuang.yihong/\",\"region\":\"cn-hangzhou\"}"}
RESP: {"RequestId":"01A0CD92-7A8E-5AAC-A3A5-5B4BE43F2C98"}
[after]  dataset.ImportInfo=None  version.ImportInfo=None  GmtModified=2026-09-23T09:22:04.193Z   ← 时间变了，值没变

PUT /api/v1/datasets/d-v51blhtpthmbzse0jt/versions/v1     （同一份 body）
RESP: {"RequestId":"01A0CD92-9381-59DC-820E-CAD420BEFE67"}
[after]  dataset.ImportInfo=None  version.ImportInfo=None  GmtModified=2026-09-23T09:22:10.608Z   ← 同上
```

**判定标准必须是回读**：两次调用都「成功」，`GmtModifiedTime` 也确实被刷新了（证明请求真的落到了服务端），
只有回读才看得出 `ImportInfo` 一动没动。这和 `assets.create_dataset` 注释里记的 `UserId` 那个坑是同一个形状 ——
**PAI 的 Update 系接口对不认识的字段是静默丢弃，不是报错。**

> 副作用披露：这条数据集的 `GmtModifiedTime` 被我刷成了 `2026-09-23T09:22:10.608Z`（原 `2026-09-22T05:44:24.612Z`）。
> 其余字段（含 `ImportInfo`、`UserId`、`Uri`、`Labels`、`Accessibility`）逐字未变。另外三条**没动**。

**还剩一条路（【文档】，本轮未执行）**：`CreateDatasetVersion`
（`POST /api/v1/datasets/{DatasetId}/versions`）的参数表里**有 `ImportInfo`**，还要求 `Uri` / `DataSourceType` / `Property`
— <https://www.alibabacloud.com/help/en/pai/developer-reference/api-aiworkspace-2021-02-04-createdatasetversion>。
即「补不了 v1，但可以补一个带 `ImportInfo` 的 v2」。**两个要先想清楚的问题**：
① DSW 挂载默认吃 `DatasetVersion: v1`，加 v2 不会让已有挂载自动受益；
② `LatestVersion` 会变成 v2，控制台和台账里这条数据集会长出第二个版本，和现网其它数据集不一样。
鉴于 §1 已证明挂载不需要 `ImportInfo`，**我的建议是存量 4 条不动**，只修代码让以后建的带上。

---

## 3. Q3 —— 那 4 条是哪条路径建的

**结论：管理员在 9/22 手跑 `delivery requests backfill --region hz --who … --apply`
（`src/delivery/cli_requests.py:1946`，它调的就是 `flows.provision_workspace`）。开号主流程那一步当时根本没跑。**

### 3.1 五条证据

**① 两张单的 `workspace_done` 是 `null`，events 里没有 `workspace_done`/`workspace_needed`**【实测，读 `/opt/infra/identity/tickets.json`】

`flows.Flows._provision_workspace` 只要真跑过，**无论成败都会写一条 ticket 事件**（成功 `workspace_done`、失败 `workspace_needed`）。
两张单的事件从头到尾是：

```
REQ-20260921-C22BACDF  (zhuang.yihong, 2026-09-21 21:30)
  created → approval_created → approval_approved → execute_start
  → user_created「已新建子账号 zhuang.yihong」
  → iam_written → linked
  → execute_done「已新建子账号 zhuang.yihong，加入 wuji_Algorithm；已写进公司 IAM，可以用企业账号登录了」

REQ-20260921-05259199  (huang.zenan, 2026-09-21 15:23)
  created → approval_created → approval_approved → execute_start
  → user_created → linked → execute_done「已新建子账号 huang.zenan」
  → (9/23 12:47) iam_written
```

`execute_done` 的文案里**一个字都没有工作空间**，而主流程的文案是 `result += space` 拼出来的。

**② 两张单的模板快照里根本没有 `workspaces` 这个键**【实测】

`flows._snapshot()` 走的是 `asdict(template)`，`Template.workspaces` 是 dataclass 字段，**只要它存在就会进快照**。
而两张单存的模板 dict 的键是：
`id/kind/platform/account/title/description/risk/groups/role_arn/max_hours/caps/buckets/allow_prefix/stages/max_days/options/params/cost_centers/cost_center_other/resource_type/region/username_pattern/console_login`
—— **没有 `workspaces`**。而服务器上**现在**的 `identity/request-templates.json` 里 `aliyun-new-user` 是有 `"workspaces": ["hz"]` 的。

**③ 代码时间线对得上**【实测，`git log`】

`workspaces` 字段（`catalog.py`）与 `provision_workspace`（`flows.py`）**同一个提交引入**：

```
4c71320  2026-09-22 17:00:11 +0800  feat(delivery): 数据迁移引擎 + 预热沉降 + 地域登记表 + 建号初始化补全
```

比两张单（9/21）晚一天，也比 4 条数据集的创建时刻（9/22 13:44 CST）晚三个多小时
—— 即**代码是先部署到面板机、手工跑过，当天傍晚才提交的**。服务器上 `/opt/infra/identity/workspaces.json`
的 mtime 是 `2026-09-22 15:05:58`，同一天。

**④ 4 条数据集是一个进程里连着建出来的，而两张单相隔 6 小时**【实测，`GetDataset.GmtCreateTime`】

```
2026-09-22T05:44:23.759Z  zhuang.yihong       (BMCPFS)
2026-09-22T05:44:24.612Z  zhuang.yihong-oss   (OSS)
2026-09-22T05:44:26.219Z  huang.zenan         (BMCPFS)
2026-09-22T05:44:27.010Z  huang.zenan-oss     (OSS)
```

**两个人前后差 2.4 秒**，而他们的开号单执行时刻分别是 9/21 21:30 和 9/21 15:24（差 6 小时）。
一个 `for name in names:` 循环，正是 `backfill` 的形状。另外「CPFS 在前、OSS 在后」也精确对应
`flows.datasets_for()` 里 `mount` 判断写在 `bucket` 判断前面。

工作空间成员那一步也是同样的形状 —— 但**更早一批**：

```
GmtCreateTime 2026-09-21T13:49:23Z  zhuang.yihong  roles=[LabelManager, AlgoOperator, AlgoDeveloper]
GmtCreateTime 2026-09-21T13:49:40Z  huang.zenan    同上
```

（= 9/21 21:49 CST，两人相隔 17 秒，同样是一个批次。）
`provision_workspace` 的顺序是 **加成员 → 建 OSS 个人目录 → 建数据集**，中间 `make_dir` 一失败就 `return`。
所以 9/21 21:49 那次多半是跑到 `make_dir` 挂了（那会儿开通身份可能还没有那个桶的 `oss:PutObject`），
9/22 13:44 修好后重跑才把目录和数据集补齐 —— 成员这步幂等，不会刷新 `GmtCreateTime`。

OSS 占位对象的 `LastModified` 还更晚一点（`general/huang.zenan/` = 05:54:10Z、`general/zhuang.yihong/` = 05:56:46Z），
说明 13:54–13:56 又跑过一两次（`put_folder` 会覆盖同名 0 字节对象、刷新时间，而数据集重名被
`provision.Executor.create_dataset` 当成功吞掉、不会重建）。**总之是反复手跑，不是一次流程。**

**⑤ 产物指纹只可能来自 `flows.datasets_for` + `provision.Executor.create_dataset`**【实测 + 读码】

| 特征 | 那 4 条 | `flows.datasets_for` + `provision.create_dataset` | CLI `delivery workspaces --apply`（9/18 那 48+24 条） |
|---|---|---|---|
| `ImportInfo` | `null` | **不传** ✅ | 传 `t.import_info` ❌ |
| 名字 | `zhuang.yihong` / `zhuang.yihong-oss` | `<登录名>` / `<登录名>-oss` ✅ | `<人名 slug>-oss` / `-cpfs` ❌ |
| OSS 前缀 | `general/zhuang.yihong/` | `<bucket_prefix>/<登录名>/` ✅ | `pretrain/caomaosong/` 这类分组布局 ❌ |
| Labels | `kind=personal` + `owner=<登录名>` | ✅ | 也是 kind/personal+owner（不区分） |
| `UserId` | 本人 RAM UserId | ✅ | ✅ |

`ImportInfo=null` 这一条就把 CLI `workspaces` 那条路排除干净了。
剩下会调 `provision_workspace` 的只有两处：`flows.Flows._provision_workspace`（**必写 ticket 事件**，证据①排除）
和 `cli_requests.py:1946` 的 `requests backfill`（源码注释原文：**「补齐不写申请单」**）。

### 3.2 所以「主流程那一步是不是压根没跑」

**没跑，而且当时也跑不了** —— 9/21 执行这两张单的时候，`workspaces` 这个能力还不存在。
`_provision_workspace` 的第一行就是 `if not spaces or ticket.get("workspace_done"): return ""`，
模板没配 `workspaces` 就是**一个云接口都不打、一条事件都不写、彻底静默**。

**但这件事现在已经变了**：`aliyun-new-user` 模板现在有 `"workspaces": ["hz"]`，
所以**从 9/22 之后建的每一个新账号，都会走主流程建出两条没有 `ImportInfo` 的数据集**。
漏传这个 bug 是活的，不是历史遗留。

---

## 4. 附带捞到的两个坑（比上面三问更容易再踩）

### 4.1 `RequestedResource.Memory` 必须带 `GB`，不带的表现是「容器起不来」【实测】

SDK 注释写的是 "The memory size. **Unit: GB**"，读起来像「填数字就行」。
实际上填 `"Memory": "16"`（或 `"8"`）**API 照收不误**，`GetInstance` 原样回显 `"Memory": "16"`，
然后实例走到 `EnvPreparing` → **`Failed`**，而 `ReasonCode` / `ReasonMessage` 全是空字符串。
只有 `GetInstanceEvents` 里才看得到真正的报错：

```
Error: failed to create containerd task: failed to create shim task: Failed to Create Container: container create
Caused by:
    0: create container
    1: run with reconnect
    2: "create conainer failed err: start container dsw-notebook
       Caused by:
           0: notify child parent ready to
           1: process: 182 failed to receive async message from peer: got msg length: 0, expected: 4, destroy failed too
       Caused by:
           0: check all process killed
           1: Failed to kill all processes": internal
```

这段报错里**没有一个字提到内存**，指向的是 containerd/rund 沙箱，任何人第一反应都会去怀疑镜像、节点或者……挂载。
我前两次实验就是挂在这上面，如果当时收手，结论会是「`ImportInfo=None` 的数据集挂不上」——**一个完全错误的结论**。
改成 `"Memory": "16GB"`、`"SharedMemory": "16GB"` 之后，同样的镜像、同样的节点、同样的两条数据集，一次就 `Running`。

现网所有活着的实例回显的都是 `"Memory": "128GB"` 这种带单位的写法 —— **照现网抄，别照文档抄。**

### 4.2 `panel-executor` 的 DSW 权限比仓库里那份参考策略宽得多【实测】

| 调用 | panel-collector | panel-executor |
|---|---|---|
| `ListInstances` @ 640957 | ❌ `NoPermissionError ... acs:paidsw:cn-hangzhou:1704065796538912:workspace/640957` | ✅ |
| `ListInstances` @ 590221 | ✅ | ❌ 同款 `NoPermissionError` |
| `CreateInstance` / `GetInstanceEvents` / `DeleteInstance` @ 640957 | 未试 | ✅ **全部可用** |

`identity/executor-policy.aliyun-workspaces.json` 里一条 `paidsw:*` 都没有，但云上实际能建能删 DSW 实例。
和 memory 里那条「`identity/executor-policy.*.json` 只是给人看的参考副本，云上实际策略与它不一致」完全吻合。
**给 auditor**：面板开通身份能建/删任意 DSW 实例，这个面不在任何参考副本里、也不在面板的功能范围内，值得单独评估。
（另外能看到什么实例受 PAI 工作空间 RBAC 约束，两个身份正好各管一个空间、交叉错开。）

---

## 5. 那 `ImportInfo` 到底是干什么的

【实测】已知**不是**挂载必需（§1）。剩下的只能给推测，标清楚：

- **【推测】主要是控制台/台账的展示与「数据集详情」页**：`ImportInfo` 里的
  `fileSystemId` / `mountTarget` / `isVpcMount` / `region`（CPFS）和 `bucket` / `path` / `region`（OSS）
  基本是 `Uri` 的结构化重复 —— `bmcpfs://<挂载点域名>/<路径>/` 里已经含了挂载点和路径。
  PAI 服务端显然是从 `Uri` 解析出挂载参数的（否则 §1 的实验不可能成功）。
- **【推测】可能影响控制台建实例时的数据集选择器**（比如按文件系统过滤、或者「数据集加速」之类的下游功能）。
  这个只能让有控制台权限的人打开新建实例页看一眼「数据集」下拉里这 4 条在不在。
- **【实测，但因果未定】`ProviderType`**：带 `ImportInfo` 的人工 CPFS 数据集是 `Lingjun`，
  面板/CLI 建的（不论带不带 `ImportInfo`）全是 `Ecs`。所以 `ProviderType` **不是**由 `ImportInfo` 决定的
  —— 9/18 那批带 `ImportInfo` 也还是 `Ecs`。是什么决定的没查出来。这一条也已经用对照组实验证明**不影响挂载**。

**给 dev 的直接建议**：把 `assets.create_dataset` 那句
「不带的话很可能建出一条『看得见但挂不上』的数据集」改掉 —— 它现在是一句**被真机证伪的告警**，
留着会让后面的人在排查真问题时往错的方向走（我这轮就差点）。改成类似
「`ImportInfo` 是 PAI 的结构化存储信息。**实测挂载不需要它**（2026-09-23，`docs/collab/research/pai-dataset-importinfo.md`），
但现网人工建的和 CLI 建的都带着，不带会让同一个工作空间里的数据集长成两种形状，而且**事后补不上**
（`UpdateDataset` 静默无效，只能 `CreateDatasetVersion` 加个 v2）—— 所以建的时候就带上。」

---

## 6. 要修 `flows.datasets_for` 的话，`fs_id` 从哪来

### 6.1 缺口

```
flows.datasets_for(ws, username)      → 只产出 name / source / uri
provision.Executor.create_dataset()   → 签名里没有 import_info
assets.create_dataset(import_info=None) → body 不带 ImportInfo
```

而 `provision_tree.oss_import()` / `cpfs_import()` 现成：

```python
oss_import(bucket, region, prefix)              # → {bucket, path, region}
cpfs_import(fs_id, region, mount_target, prefix) # → {path, fileSystemId, isVpcMount, region, mountTarget}
```

对上 `identity/workspaces.json` 的 schema（`workspaces.py::_KEYS`）：

| `cpfs_import` 要的 | 登记表里有没有 |
|---|---|
| `region` | ✅ `ws["region"]`（`cn-hangzhou`，裸的） |
| `mount_target` | ✅ `ws["mount"]`（`cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com`） |
| `prefix` | ✅ `username` |
| **`fs_id`** | ❌ **没有** |

OSS 那半边**不缺任何东西**：`oss_import(ws["bucket"], ws["bucket_region"], f"{bucket_prefix}/{username}")` 直接能用
（`bucket_region` 在登记表里已经被 `_REGION` 正则强制成裸写法 `cn-hangzhou`，正好是 `oss_import` 要的；
而 `Uri` 里那个 `oss-cn-hangzhou` 是拼的时候加的 —— 两套写法已经分得很干净，不用动）。

### 6.2 两个方案

**A. 从 `mount` 推导**：`cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com`
→ 取第一段 → 去掉 `-vpc-<随机>` → `cpfs-00000ub3ici1dnniit2i0` → 换前缀 → `bmcpfs-00000ub3ici1dnniit2i0`。

**B. 给 `identity/workspaces.json` 加一个显式字段 `fs_id`**，`_KEYS` 里加上，配一条正则校验。

### 6.3 我推荐 B（加字段），四个理由

1. **仓库里已经有一条明写的「别这么推导」的结论，A 等于和自己打架。**
   `assets.py::_pai_store` 的注释原文：
   > **不把 `cpfs-<id>-vpc-x` 归一成 `bmcpfs-<id>`**：智算版这两个确实指同一个文件系统，
   > 但通用版只有 `cpfs-` 这一种写法，归一的规则对两种版本不一样，写错了比不归一更糟。

   这句话就是 A 的反例：**同样是 `cpfs-xxx…` 开头的挂载点域名，智算版要换成 `bmcpfs-`、通用版不能换**，
   而登记表里没有任何字段告诉你这是哪一版。推导必然要猜。现在只有一个智算版文件系统所以怎么写都对，
   哪天登记一个通用版 CPFS，推导会静默产出一个不存在的 `fileSystemId`。

2. **推导出来的值无法被校验，填进去的可以。** `workspaces.py` 的加载器已经对 `mount`/`bucket`/`region`
   各有一条正则（`_MOUNT`/`_BUCKET`/`_REGION`），加一条 `_FS_ID = re.compile(r"\A(bm)?cpfs-[a-z0-9]+\Z")`
   就能在**加载期**把写错的拦住 —— 这正是那个模块的设计意图（「格式错在加载时就报了」）。
   推导路径上错了只有在云上建出错数据集之后才看得见，而那时**改不回来**（§2）。

3. **登记表本来就是人工一次性登记的东西**，`mount` 那一长串域名都是手填的，多填一个 `bmcpfs-00000ub3ici1dnniit2i0`
   成本接近零；而它是**唯一**还缺的那个字段。

4. **错的代价不对称**：`fs_id` 写错 = 又造出一条「`ImportInfo` 内容是错的、而且事后补不上」的数据集，
   比起「登记表多一个字段」贵得多。

**具体改法（给 dev，未实现，我只读源码）**：

- `identity/workspaces.json`：`hz` 加 `"fs_id": "bmcpfs-00000ub3ici1dnniit2i0"`；
  `sing` 那条同理（它的 `mount` 是 `cpfs-07001v48jdw7tt8jhw0df-vpc-1xh1pi.ap-southeast-1.cpfs.aliyuncs.com`，
  **fs_id 需要人去控制台/`DescribeFileSystems` 确认一下是不是 `bmcpfs-07001v48jdw7tt8jhw0df`，别照着 hz 推**）。
- `workspaces.py`：`_KEYS` 加 `fs_id`；有 `mount` 就要求有 `fs_id`（反之不必），加正则校验。
- `flows.datasets_for(ws, username)`：每条 spec 多返回一个 `import_info`
  —— CPFS 那条 `provision_tree.cpfs_import(ws["fs_id"], ws["region"], ws["mount"], username)`，
  OSS 那条 `provision_tree.oss_import(ws["bucket"], ws["bucket_region"], path)`。
- `provision.Executor.create_dataset(...)`：加 `import_info=None` 形参，原样透传给 `assets.create_dataset`。
- `flows.provision_workspace`：调用处把 `spec["import_info"]` 传进去。
- **别顺手做的事**：不要给存量那 4 条补版本 / 不要删了重建（§2 已证明补不了，而 §1 已证明不影响挂载）。
  真要统一形状，等有了明确收益再说。

**折中（如果坚决不想动登记表 schema）**：`fs_id` 可选，缺省时按 A 推导**但必须打一条显式告警**，
并且只在 `mount` 的第一段以 `cpfs-` 开头且能匹配 `-vpc-` 时才推 —— 其余情况宁可不带 `ImportInfo`（反正不影响挂载）。
我不推荐这个，因为「带了一个错的 `fileSystemId`」比「不带」更难查。

---

## 7. 本轮的写操作与清理

| 动作 | 对象 | 现状 |
|---|---|---|
| `CreateInstance` ×3 | `dsw-51dlwhtzc2iazuy1t0` / `dsw-ga6ygia5c504vilfgn`（都因 §4.1 的内存单位失败）、`dsw-rjrnbqojiy2ots7q15`（Running） | **全部 `DeleteInstance` 已删**，`ListInstances` 复查无 `panel-importinfo-*` |
| `CreateInstance` ×1 | `dsw-j04be12w0lkqkz1ni1`（对照组，Running） | **已删** |
| `UpdateDataset` / `UpdateDatasetVersion` ×2 | `d-v51blhtpthmbzse0jt`（`zhuang.yihong-oss`） | **无实际变更**；唯一副作用是 `GmtModifiedTime` 变成 `2026-09-23T09:22:10.608Z` |
| 面板机临时脚本 | `/root/rz/*.py` | **已 `rm -rf`** |

没有动过：任何数据集的删除 / 新建、`identity/` 下任何文件、RAM、`workspaces.json`、`request-templates.json`。
所有临时脚本都跑在 `root` 家目录下，**没有以 root 写过 `identity/`**。

---

## 8. 还没验到的

- **容器内 `ls /mnt/a`**：见 §1.4，三条 API 路都不通，要控制台。
- **控制台「新建实例 → 数据集」下拉里，`ImportInfo=null` 的这 4 条在不在**：这是「看得见但挂不上」这句话**唯一还可能成立的含义**，
  而它只能用眼睛验。建议找个有 640957 权限的人花一分钟看一眼 —— 如果下拉里没有，那面板建的数据集对普通用户来说就是**用不了**的，
  即使 OpenAPI 挂得上。
- **`CreateDatasetVersion` 加 v2 是否真能带上 `ImportInfo`**：只有文档，没实测（超出批准的写范围）。
- **`ProviderType` 由什么决定**：`Lingjun` vs `Ecs` 的分界没查出来；已证明与 `ImportInfo` 无关、与挂载无关。
- **9/21 21:49 那次 `backfill` 为什么只加到成员就停了**：`make_dir` 失败是最合理的解释（顺序 + OSS 占位对象晚了 16 小时），
  但那次运行没有任何日志留下来（`journalctl -u delivery` 在 9/22 那段查不到相关行，也没有 shell history），所以是【推测】。
