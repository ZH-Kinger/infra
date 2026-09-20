# 阿里云 PAI 的 RAM 动作前缀与「禁删除」Deny 语句核实

调研人：researcher ｜ 日期：2026-09-20
用途：修改生产自定义策略 `pai平台权限`（挂算法组），目标是禁掉删除（尤其删数据集）。
取证方式：全部来自阿里云官方帮助中心页面原文（`curl` 抓 HTML 后解析表格，非二手转述）。**无真机 RAM 调用**（环境拦截了线上只读 RAM 查询，见文末「没做到的取证」）。

标注约定：【文档】= 官方页面明写并附 URL；【推断】= 我从文档事实推出、官方没直说；【存疑】= 两份官方文档互相矛盾，未解决。

---

## 结论速览（先看这 5 条）

1. **`pai:` / `paidataset:` / `paiworkspace:` 是三个独立的 RAM 产品代码（ram-code），互不包含。`pai:*` 不匹配 `paidataset:DeleteDataset`。**【文档 + 强推断】
2. **删数据集的准确动作名是 `paidataset:DeleteDataset`**（HTTP `DELETE /api/v1/datasets/{DatasetId}`，访问级别 delete，资源类型「全部资源」）。【文档】
3. **Deny 语句绝对不要带 `pai:Accessibility` / `pai:EntityAccessType` 条件**——两份官方文档对这两个条件键是否作用于 `paidataset:*` 互相矛盾；带条件的 Deny 一旦条件键没被传入就**不命中**，正好踩中「以为禁了删除、其实没禁」。【存疑 → 按 fail-safe 处理】
4. **「PUBLIC → 谁都能删」这句话对数据集是错的**：PAI 官方角色权限表里，`PaiDataset:DeleteDataset` 只给「工作空间负责人/管理员」+「各角色自己创建的 Private 数据集」；**Public 数据集连创建者本人都删不了，只有工作空间管理员能删**。（Designer/Flow 类资源则相反，确实是「Public 他人创建的也能删」。）面板 UI 文案需要按资源类型分开写。【文档】
5. **Deny 优先，且跨策略生效**：显式 Deny 会压过同时挂着的 `AliyunPAIFullAccess`。唯一陷阱是**授权范围（账号级 / 资源组级）**——Deny 必须挂在账号级别，否则账号级的 Allow 先判定结束，资源组级的 Deny 根本不会被评估。【文档】

---

## 1. PAI 的 RAM 动作前缀到底有几个

### 1.1 RAM 的 Action 语法（前提）

> Action/NotAction 元素的格式：`<ram-code>:<action-name>`。
> ram-code：云服务的 RAM 代码。
> Action/NotAction 元素的值在大部分情况下不区分大小写，但为了保持业务行为的一致性，请按照云服务提供的鉴权文档使用准确的操作前缀。

【文档】<https://help.aliyun.com/zh/ram/user-guide/policy-elements>（章节「操作（Action/NotAction）」）

### 1.2 PAI 有 25 个并列的 ram-code

系统策略 `AliyunPAIFullAccess` 的第一条语句把它们**逐个列了出来**：

```
"pai:*", "paiplugin:*", "eas:*", "paiflow:*", "paidesigner:*", "paidlc:*",
"paidsw:*", "paiimage:*", "paicodesource:*", "paidataset:*", "paitrainingservice:*",
"paimodel:*", "paicomponentmanagement:*", "paiautoml:*", "paillmtrace:*",
"pailangstudio:*", "paiartlab:*", "paiexperiment:*", "paiworkspace:*", "paiitag:*",
"featurestore:*", "paiabtest:*", "pairec:*", "modelservice:*", "agenticpai:*"
```

【文档】<https://help.aliyun.com/zh/ram/developer-reference/aliyunpaifullaccess>

**推断依据**：如果 `pai:*` 能覆盖 `paidataset:*`，阿里云不会在同一条语句里把 `pai:*` 和另外 24 个前缀并列写出来——那 24 条全是冗余。结合 1.1 的「Action = `<ram-code>:<action-name>`」（冒号是字面量，`pai:` 这个字面前缀无法匹配以 `paid…` 开头的串），**`pai:*` 只匹配 ram-code 恰为 `pai` 的动作**。【推断（强）】

`pai` 自己确实是一个真实存在、有自己动作的 ram-code，不是「PAI 全家桶的总称」。官方授权信息表里属于 `pai:` 的动作只有 8 个，全是账号级杂项：

```
pai:AdministratePAI   pai:CreateOrder      pai:ListQuotas       pai:ListFeatures
pai:ListProducts      pai:ListUserConfigs  pai:SetUserConfigs   pai:DeleteUserConfig
```

【文档】<https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-ram>（「RAM 权限策略支持的 Action…」表，共 121 行，我已全量解析）

另一个独立佐证：`AliyunPAIReadOnlyAccess` 只写了 `pai:List*` / `pai:Get*` / `paiplugin:*` / `eas:*` / `paiitag:*` / `featurestore:*` / `paiabtest:*` / `pailangstudio:*`，**完全没有 `paidataset:*`**。也就是说挂 ReadOnly 的人连 `ListDatasets` 都调不了——这只有在「`pai:List*` 不覆盖 `paidataset:ListDatasets`」时才说得通。【文档】<https://help.aliyun.com/zh/ram/developer-reference/aliyunpaireadonlyaccess>

### 1.3 各模块归属（按官方授权信息表实测统计）

| ram-code | 覆盖范围 | 出处页面 |
|---|---|---|
| `paiworkspace` (82 条) / `paidataset` (60) / `paimodel` (28) / `paiexperiment` (24) / `paiimage` (18) / `pai` (16) / `paicodesource` (12) | AI 资产管理 + 工作空间（AIWorkspace 2021-02-04） | [api-aiworkspace-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-ram) |
| `paidlc` | 分布式训练 DLC | [api-pai-dlc-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dlc-2020-12-03-ram) |
| `paidsw` | 交互式建模 DSW | [api-pai-dsw-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-ram) |
| `paidesigner` | 可视化建模 Designer（PAIStudio 2021-02-02） | [api-paistudio-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-paistudio-2021-02-02-ram) |
| `eas` | 模型在线服务 EAS（注意：**不是** `paieas`） | [api-eas-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-eas-2021-07-01-ram) |

> ⚠️ 现网策略的 Resource 写的是 `acs:pai:*:*:workspace/*/*`，Resource 的第二段同样是 ram-code。按 PAI 自己的格式说明，资源 ARN 应为 `acs:<子产品ram-code>:<region>:<account-id>:workspace/<工作空间ID>/<资源名复数>/<资源ID>`（【文档】[基于 RAM 的 Condition 属性设置资源的访问权限](https://help.aliyun.com/zh/pai/set-access-permissions-for-resources-based-on-the-condition-attribute-of-ram)「关键说明」）。数据集应当是 `acs:paidataset:…`。**现网这条策略的 Action 和 Resource 两处都指向 ram-code `pai`，按字面语义它几乎什么都没授权**（`pai:` 那 8 个动作的资源类型又都是「全部资源」，被 `workspace/*/*` 这个 ARN 挡掉）。算法组现在能正常用 PAI，多半是权限来自别处（系统策略 / 工作空间成员角色）。**改策略前先确认这些人身上还挂了什么**，否则你在一条无效策略上加 Deny，效果是「Deny 生效了、Allow 本来就没生效」——这次是好事，但说明对现状的认知是错的。【推断】

---

## 2. 删除数据集的准确动作名

| Action | 访问级别 | 资源类型 | 条件关键字 | 关联操作 |
|---|---|---|---|---|
| `paidataset:DeleteDataset` | delete | `*` 全部资源 | 无 | 无 |

API：`DELETE /api/v1/datasets/{DatasetId}`

【文档】<https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-deletedataset>（「授权信息」表），中英两个站点（help.aliyun.com / alibabacloud.com）表格内容一致。同一行也出现在汇总表 [api-aiworkspace-2021-02-04-ram](https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-ram) 里。

**不是 `pai:DeleteDataset`**——该名字在任何一张官方授权信息表里都不存在。

另有一份独立佐证用的是首字母大写写法 `PaiDataset:DeleteDataset`（RAM 对 Action 大小写大部分情况下不敏感，见 1.1）：【文档】[附录：角色及权限列表](https://help.aliyun.com/zh/pai/appendix-list-of-roles-and-permissions)「AI 资产 - 数据集管理」表，「权限点字段（通过 RAM 管理）」列。

---

## 3. 值得一并禁的「删除」类动作（官方原文动作名）

下面全部摘自各模块授权信息表的「操作」列（访问级别 delete，或语义为删除的 Remove/Detach）。

**数据集（AI 资产）**
```
paidataset:DeleteDataset                 删除数据集
paidataset:DeleteDatasetVersion          删除数据集版本
paidataset:DeleteDatasetLabels           删除数据集标签
paidataset:DeleteDatasetVersionLabels    删除数据集版本标签
paidataset:DeleteDatasetFileMetas        删除数据集文件元数据
paidataset:DeleteDatasetJobConfig        删除数据集任务配置
paidataset:DeleteDatasetJob              删除数据集任务   ← 见下方「表不全」提示
```

**工作空间 / 成员**
```
paiworkspace:DeleteWorkspace             删除工作空间（唯一支持资源级鉴权的一条：
                                          acs:paiworkspace:{#regionId}:{#accountId}:workspace/{#WorkspaceId}）
paiworkspace:DeleteMembers               删除成员
paiworkspace:RemoveMemberRole            删除成员角色
paiworkspace:RemoveWorkspaceRole         删除自定义角色
paiworkspace:DeleteWorkspaceResource     删除工作空间内资源（通用删除入口，别漏）
paiworkspace:DeleteConfig / DeleteConnection / DeletePrompt
```

**模型 / 实验 / 代码 / 镜像**
```
paimodel:DeleteModel        paimodel:DeleteModelVersion
paimodel:DeleteModelLabels  paimodel:DeleteModelVersionLabels
paiexperiment:DeleteExperiment  paiexperiment:DeleteRun
paiexperiment:DeleteExperimentLabel  paiexperiment:DeleteRunLabel
paicodesource:DeleteCodeSource
paiimage:RemoveImage        paiimage:RemoveImageLabels
```

**任务 / 实例（「删任务」按模块分三处）**
```
paidlc:DeleteJob            paidlc:DeleteJobTemplate      paidlc:DeleteTensorboard
paidsw:DeletePostPaidInstance   paidsw:DeleteInstanceSnapshot
paidsw:DeleteInstanceLabels     paidsw:DeleteIdleInstanceCuller
paidsw:DeleteInstanceShutdownTimer
paidesigner:DeletePipelineDraft  paidesigner:DeletePipelineDraftFolder
paidesigner:DeleteTemplate       paidesigner:DeleteTemplateLabels   (后两条只在角色表里)
paiflow:DeletePipeline           paiflow:DeletePipelineRun          (只在角色表里)
eas:DeleteService / DeleteServiceInstances / DeleteServiceMirror /
eas:DeleteServiceAutoScaler / DeleteServiceCronScaler / DeleteServiceLabel /
eas:DeleteResource / DeleteResourceInstances / DeleteResourceLog / DeleteResourceDLink /
eas:DeleteGateway / DeleteGatewayIntranetLinkedVpc / DeleteGatewayIntranetLinkedVpcPeer /
eas:DeleteAclPolicy / DeleteBenchmarkTask / DetachGatewayDomain
```

**⚠️ 授权信息表并不全 → 用通配符，别枚举**：`PaiDataset:DeleteDatasetJob`（删除数据集任务）出现在[角色及权限列表](https://help.aliyun.com/zh/pai/appendix-list-of-roles-and-permissions)里，但**不在** [api-aiworkspace-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-ram) 的 121 行表里；`paidesigner:DeleteTemplate`、`paiflow:DeletePipeline*` 同理。两张官方表互有缺漏 ⇒ **任何靠手工枚举的 Deny 都会漏**。【文档对比实测】
Action 支持通配符（官方示例 `ecs:Describe*`；`AliyunPAIReadOnlyAccess` 里甚至有中缀通配 `paiitag:*Get*`），所以用 `paidataset:Delete*` 这类写法。【文档】

**语义上"等于删"但名字不带 Delete 的，单独决策**（我没放进下面的推荐语句，因为一禁就影响日常使用）：
`paidataset:PublishDataset`（发布后按角色表创建者自己就再也删不掉了）、`paidataset:ChangeDatasetOwner`（转移所有者）、`paidlc:StopJob`、`paidsw:StopInstance`、`eas:StopService` / `eas:ReleaseService`、`paidesigner:StopPipelineDraft`。

---

## 4. `pai:Accessibility` / `pai:EntityAccessType` 对 `paidataset:*` 生效吗

### 4.1 两份官方文档互相矛盾【存疑】

**A. PAI 侧说「生效」**（[基于 RAM 的 Condition 属性设置资源的访问权限](https://help.aliyun.com/zh/pai/set-access-permissions-for-resources-based-on-the-condition-attribute-of-ram)）原文：

> 所有接入工作空间的 PAI 子产品资源操作，均通过工作空间接口代理至 RAM 进行鉴权。
> `pai:Accessibility`：资源的可见性，取值 PUBLIC / PRIVATE。
> `pai:EntityAccessType`：资源的创建者属性，取值 CREATOR / OTHERS。
> 当 RAM 用户尝试访问 API 对象时，PAI 服务将调用 RAM 接口进行鉴权，并在鉴权过程中设置两个 Condition 属性值。
> **如果用户被授权策略中的 Condition 属性不包括 `pai:Accessibility` 和 `pai:EntityAccessType`，则 RAM 对该属性不做检查。**

该页的「工作空间算法开发角色 Policy 示例」与现网 `pai平台权限` 的两条语句逐字同构（PRIVATE+CREATOR 一条、PUBLIC 一条）——**现网策略就是照抄这一页来的**。注意该示例自身也存在前后不一致：Action 写 `pai:*`，Resource 却写 `acs:paidsw:*:*:*`（ram-code 对不上，见 1.3 的红字）。

**B. RAM 授权信息表说「没有条件键」**（[api-aiworkspace-…-ram](https://help.aliyun.com/zh/pai/developer-reference/api-aiworkspace-2021-02-04-ram)）：该页 121 行里 **120 行的「条件关键字」列都是「无」**，「资源类型」列 115 行是「全部资源」（只有 5 条 workspace 级动作有 ARN）；页面并另写一句「人工智能平台 PAI 未定义产品级别的条件关键字」。DLC / DSW / Designer / EAS 四张表同样全是「无」。

**我的推断**：授权信息表是按 OpenAPI 元数据自动生成的，收录不了 PAI 工作空间代理层在鉴权时额外塞进去的上下文；A 描述的是真实运行时行为。但这只是推断，**没有真机验证**，而且 A 页面自己就有 ram-code 写错的硬伤，可信度打折。【推断（弱）】

### 4.2 对「要写的 Deny」的直接影响（这条是硬结论，不依赖上面的存疑）

**Deny 语句不要带 Condition。** 理由是条件键的判定方向：带条件的 Deny 只有在**请求确实携带了该条件键且取值匹配**时才命中；只要某条调用路径没传 `pai:Accessibility`（比如不走工作空间代理的直调 OpenAPI，或 PAI 后续改了实现），Deny 就静默失效 —— 这正是「以为禁了删除、其实没禁」的那个失败模式。**无条件 Deny 在两种世界里都成立**，是唯一 fail-safe 的写法。【推断（强），基于 4.1 A 段原文的判定语义】

同理，Deny 的 Resource 写 `"*"`：授权信息表说这些动作只支持「全部资源」（B），而若真实鉴权带的是 `acs:paidataset:…:workspace/…` 形式的 ARN（A），`"*"` 照样匹配。写成 `acs:pai:*:*:workspace/*/*` 则两种世界里都可能不命中。

### 4.3 面板 UI 那句「PUBLIC → 谁都能删」要改

[附录：角色及权限列表](https://help.aliyun.com/zh/pai/appendix-list-of-roles-and-permissions) 的「AI 资产 - 数据集管理」表，列分组是：工作空间负责人/管理员 ｜ 算法开发(Private自己/Private他人/Public自己/Public他人) ｜ 算法运维(Private自己/Private他人/Public) ｜ 标注管理员(同) ｜ 访客(同)。我把该表所有「删除」行解码后：

```
PaiDataset:DeleteDataset              → 管理员 ✅ ｜ 算法开发:仅 Private自己 ｜ 算法运维:仅 Private自己 ｜ 标注管理员:仅 Private自己 ｜ 访客:✗
（DeleteDatasetVersion / DatasetLabels / DatasetVersionLabels / DatasetFileMetas /
  DatasetJobConfig / DatasetJob 六条的勾选分布与上行完全一致）
对照组：
PaiDesigner:DeletePipelineDraft       → 管理员 ✅ ｜ 算法开发:Private自己 + Public自己 + **Public他人** ｜ …
Paiflow:DeletePipeline                → 同上
PaiWorkspace:DeleteMembers / RemoveMemberRole / DeleteWorkspaceResource → **仅管理员**
```

**所以：数据集不存在「PUBLIC 谁都能删」——恰恰相反，数据集一旦 Public，连创建者自己都删不了，只剩工作空间负责人/管理员能删**；而 Designer 草稿 / Flow 工作流确实是「Public 的别人也能删」。【文档】

同时要分清两层，UI 文案别混：
- **PAI 平台角色模型**（上表）：数据集 Public = 更难删。
- **现网这条自定义策略的字面语义**：第二条语句 `PUBLIC` 不带 `EntityAccessType`，所以**凡是被这条语句覆盖到的动作**（若 `pai:*` 真能覆盖删除动作的话），对任何人创建的 PUBLIC 资源都放行、包括删除。

建议 UI 改成按资源类型分述，并且注明「以工作空间角色为准；自定义策略的 PUBLIC 语句不区分创建者」。

---

## 5. Deny 与 Allow 的优先级 / 能不能压住 `AliyunPAIFullAccess`

**能。**【文档】

> 当权限策略中既有 Allow 又有 Deny 时，遵循 Deny 优先原则。
> —— <https://help.aliyun.com/zh/ram/user-guide/policy-elements>「效果（Effect）」

> 最小单元判定流程：权限判定遵循 Deny 优先原则，优先检查访问请求是否命中 Deny 语句。是 → 判定结束，返回 Explicit Deny。
> 判定结果说明：一旦访问请求命中了权限策略中的 Deny 语句…**即使此时访问请求同时命中了 Allow 语句**，但遵循 Deny 优先原则，Deny 语句优先级高于 Allow 语句，判定结果仍为显式拒绝。
> 对于 RAM 用户，基于身份的策略**包括直接授权的策略和从 RAM 用户组中继承的策略**。
> —— <https://help.aliyun.com/zh/ram/policy-evaluation-process>

即：同一个 RAM 用户身上的「系统策略 + 自定义策略 + 用户组继承策略」在同一次「最小单元判定」里合并评估，自定义策略里的 Deny 压过 `AliyunPAIFullAccess` 的 `paidataset:*` Allow。

### ⚠️ 唯一能让这条失效的坑：授权范围（Scope）

同一页原文：

> 基于身份的策略因授权范围不同，又分为**账号级别和资源组级别，账号级别的策略优先级高于资源组级别的策略**。
> 检查…是否拥有账号级别的基于身份的策略：如果判定结果是 Explicit Deny 或 **Allow：基于身份策略的判定结束**…如果判定结果是 Implicit Deny：继续进行下一步（资源组级别）判定。

**推论**：如果 `AliyunPAIFullAccess` 挂在账号级别（默认授权方式），而你的 Deny 只挂在某个**资源组**范围，那么账号级的 Allow 一命中就判定结束，资源组级的 Deny 压根不会被评估 → Deny 形同虚设。**务必把带 Deny 的自定义策略按「整个云账号」范围授权**，并在 RAM 控制台该用户的「权限管理」页逐条确认「授权范围」列都是「整个云账号」。【文档】

补充（不影响本次，但别踩）：`acs:SourceIp` 之类的全局 Deny 会连带影响控制台操作；资源目录管控策略（Control Policy）的判定在身份策略之前，若上层有管控策略也可能提前结束判定。

---

## 6. 推荐的 Deny 语句（可直接并入 `pai平台权限`）

原则：**无 Condition + `Resource: "*"` + 按 ram-code 用 `Delete*` 通配**（理由见 3 的「表不全」与 4.2）。Deny 语句作为**新增的第三条 Statement** 加进现有策略即可，前两条 Allow 不用动。

```json
{
  "Effect": "Deny",
  "Action": [
    "paidataset:Delete*",
    "paimodel:Delete*",
    "paiexperiment:Delete*",
    "paicodesource:Delete*",
    "paiimage:Remove*",
    "paiworkspace:Delete*",
    "paiworkspace:Remove*",
    "paidlc:Delete*",
    "paidsw:Delete*",
    "paidesigner:Delete*",
    "paiflow:Delete*",
    "eas:Delete*",
    "pai:Delete*"
  ],
  "Resource": "*"
}
```

说明与副作用（改之前逐条确认，别一把梭）：

- `pai:Delete*` 这条**只为兜底**：万一 4.1 A 段那个「Action 写 `pai:*`」的示例反映的是 PAI 真的用 ram-code `pai` 代理鉴权（我判断不是，但没真机验证），这条能接住。它实际只覆盖 `pai:DeleteUserConfig`，误伤面极小，留着划算。
- `paiworkspace:Delete*` / `Remove*` 会一并禁掉 `DeleteMembers`、`RemoveMemberRole`、`RemoveWorkspaceRole`、`DeleteWorkspaceResource`（题目要求的「删工作空间成员」在这里），但也会禁掉 `DeleteConfig` / `DeleteConnection` / `DeletePrompt`、`RemoveWorkspaceQuota` —— 算法组本来大多也没这些权限（角色表里仅管理员有），影响面应该为零，但建议先确认没人靠脚本改 Connection/Prompt。
- `paidsw:Delete*` 会连带禁掉 `DeleteInstanceLabels`、`DeleteIdleInstanceCuller`、`DeleteInstanceShutdownTimer` —— 后两条是「关闭自动停机配置」，禁了意味着用户不能取消闲置停机策略。**如果 DSW 那边靠这个做自助运维，把 `paidsw:Delete*` 换成精确的 `paidsw:DeletePostPaidInstance` + `paidsw:DeleteInstanceSnapshot`。**
- `eas:Delete*` 会禁掉在线服务的全部删除；若算法组日常要自己下线服务，注意 `eas:StopService` / `eas:ReleaseService` **不在** Deny 里（它们访问级别是 update），下线仍可用、只是删不掉。
- 没有包含 `paidataset:PublishDataset` / `ChangeDatasetOwner` / `paidlc:StopJob` / `paidsw:StopInstance`（见 3 末尾）—— 需要的话单独加。
- **验证方式**：改完让一个算法组成员在控制台点「删除数据集」，期望报 `NoPermission` 且 `NoPermissionType = ExplicitDeny`（[如何排查 RAM 无权限问题](https://help.aliyun.com/zh/ram/support/how-to-troubleshoot-an-access-denied-error)）。看到 `ExplicitDeny` 才算真禁住；若看到 `ImplicitDeny` 说明只是本来就没授权，Deny 其实没命中（可能挂错了授权范围，或动作名/前缀不对）。

---

## 7. 没做到的取证（诚实交代）

- **未做真机 RAM 校验**：计划用线上 bot 容器里的只读 RAM 凭证跑 `ListPolicies(System)` + `GetPolicyVersion`，交叉核对 `AliyunPAIFullAccess` 的真实文档、并看看 `pai平台权限` 这条自定义策略目前还挂给了谁 —— 被环境的权限闸门拦下（Production Reads）。本报告全部结论因此只基于公开文档。需要的话给我放行，我可以补：① 现网这条自定义策略的实际 JSON 与挂载对象；② 这些子账号身上还挂着哪些 PAI 系统策略（决定 Deny 是否必要）；③ 各条授权的「授权范围」是账号级还是资源组级（决定第 5 节那个坑会不会踩到）。
- **未验证**「`pai:*` 是否真的匹配不到 `paidataset:*`」的运行时行为。文档层面证据很强（1.2），但要 100% 确定只有一条路：拿一个测试子账号只挂 `{"Action":"pai:*","Resource":"*"}`，去调 `ListDatasets` 看是否 `NoPermission`。这属于写操作 + 生产账号改动，不在我的边界内，建议 dev 或运维在测试子账号上做一次，十分钟能定论。
- **未解决** 4.1 的文档矛盾（条件键是否作用于 `paidataset:*`）。第 6 节的写法（无 Condition）刻意做成「两种答案下都正确」，所以这个矛盾不阻塞本次改动；但如果将来要写「只禁删别人的、允许删自己的」这种带 Condition 的策略，**必须先真机验证**，否则就是一个隐形的 fail-open。
