# ACT-1 取证：重算下发要用的策略改写接口 + 线上发放身份的真实权限

调研人：researcher ｜ 日期：2026-09-23 ｜ 对应任务：`docs/collab/planning/admin-console-actions.md` ACT-1（第一批前置）

**全程只读。** 没有创建 / 修改 / 删除任何云资源，没有调用过 `UpdatePolicy`、`CreatePolicyVersion` 或任何写接口。
所有真机调用要么是只读 API，要么打在**不存在的**资源上（并且最终那一步被权限系统拦下了，见 §1.4）。

标注：【文档】官方页面/官方 OpenAPI 元数据明写，附 URL ｜【实测】本次真机只读调用所得，附方法 ｜【推测】未证实

---

## 结论速览

| # | 问题 | 结论 |
|---|------|------|
| ① | 火山 `UpdatePolicy` 参数名 | `PolicyName`（必填）+ `NewPolicyDocument` / `NewPolicyName` / `NewDescription`（均选填）。**不是 `PolicyDocument`**，`NewPolicyName` **不用给** |
| ① | 火山有没有版本 | **没有。原地替换，零回滚点。** 五个 PolicyVersion 类接口在 IAM 2018-01-01 里**全部不存在**（实测 404，和瞎编的 Action 同一个错） |
| ① | 挂在用户身上能不能改 | 能。线上 `wuji-panel-issuer`（AttachmentCount=1）今天 04:54Z 就被改过一次 |
| ② | `RotateStrategy` 取值 | 只有两个：`None`（默认）/ `DeleteOldestNonDefaultVersionWhenLimitExceeded`。**串是准确的** |
| ② | 5 版本上限 | 上限 5、**不支持调整**；撞上限且不带 rotate → `LimitExceeded.Policy.Version` |
| ③ | 火山 issuer 有没有 `iam:ListUsers` | **没有，实测被拒。** 仓库副本是全的、云上就是这样。结论反过来了：**火山凭证现在根本发不出来**（见 §3.2，这是个真 bug） |
| ③ | 两朵云策略副本对不对 | 6 份 `cloud-policies/*.json` 与云上**逐字一致**。缺的是「还有哪些策略也挂在同一个身份上」这个维度 |
| ④ | `panel-executor` 的 DSW 建删从哪来 | **不是 RAM 给的**（RAM 侧一条 `paidsw:` 都没有）。是**工作空间 RBAC**：它在 640957 是 `PAI.WorkspaceAdmin`，实测持有 603 个权限点，含 `PaiDSW:CreateInstance` / `DeleteInstance` |

**要不要改 `deploy/panel/cloud-policies/*.json`？**

- 为了 ACT-1 本身（重算下发）：**不用改。** 两份 issuer 策略已经齐了 —— 阿里有 `CreatePolicyVersion`/`DeletePolicyVersion`/`ListPolicyVersions`/`GetPolicyVersion`/`GetPolicy`，火山有 `iam:UpdatePolicy`/`iam:GetPolicy`，ACT-3 要的动作一个不缺。
- 为了火山凭证能发出来：**要改云上**，给火山 issuer 补一条能替代 `iam:ListUsers` 的账号校验（§3.2 给了三个选项）。**但这不是 ACT-1 的范围**，是另一个 P0 bug，建议单开。
- 为了 ban 掉 DSW 建删：**要改云上**，加一条显式 Deny，但**先做 §4.5 那个 5 分钟判定实验**再决定用哪条路。
- 三处改动都按 `deploy/panel/cloud-policies/README.md` 的规矩：**先改云上、再重新导出覆盖副本**。本文只给结论，没动任何文件。

---

## ① 火山 `UpdatePolicy`

### 1.1 参数名与必填性【文档】

| 参数 | 必填 | 说明 |
|------|------|------|
| `PolicyName` | 是 | 要改哪条策略 |
| `NewPolicyName` | 否 | 改名用，1–64 字符，支持 `+=,.@-_` |
| `NewPolicyDocument` | 否 | 新的策略正文（JSON），**上限 6144 字符** |
| `NewDescription` | 否 | 新描述，≤128 字符 |

出处：<https://docs.volcengine.com/docs/IAM/UpdatePolicy-UpdatePolicy?lang=zh>

**SDK 侧独立印证**【实测】—— 读已安装的 `volcenginesdkiam` 的 model（不发请求）：

```
UpdatePolicyRequest.attribute_map =
  {'new_description': 'NewDescription', 'new_policy_document': 'NewPolicyDocument',
   'new_policy_name': 'NewPolicyName', 'policy_name': 'PolicyName'}
```

所以 ACT-3 的火山分支写 `{"PolicyName": name, "NewPolicyDocument": json.dumps(doc)}` 就对了，
**`NewPolicyName` 不要给**（给了就是改名，而重算下发的前提是策略名不变）。
参数名写成 `PolicyDocument` 的话火山不会报错、只会**什么都不改**（选填参数缺席 = 不动该字段）——
这是最阴的失败模式：接口返 200、策略原封不动、面板以为修好了。ACT-7 的「参数逐字锁」要把这条锁死。

### 1.2 没有版本概念 —— 原地替换，没有回滚点【实测·强证据】

用**只读的 collector 凭证**逐个签名调用各 Action（不带任何参数，且 collector 的策略对 `iam:Create*/Update*/Delete*`
全是 Deny，动作即使存在也必定被拒、不可能有副作用）。火山网关的三态回答把问题答得很干净：

```
ZzzNotARealAction        -> 404 InvalidActionOrVersion  Could not find operation ZzzNotARealAction for version 2018-01-01
CreatePolicyVersion      -> 404 InvalidActionOrVersion  Could not find operation CreatePolicyVersion for version 2018-01-01
ListPolicyVersions       -> 404 InvalidActionOrVersion  Could not find operation ListPolicyVersions for version 2018-01-01
GetPolicyVersion         -> 404 InvalidActionOrVersion  Could not find operation GetPolicyVersion for version 2018-01-01
SetDefaultPolicyVersion  -> 404 InvalidActionOrVersion  Could not find operation SetDefaultPolicyVersion for version 2018-01-01
DeletePolicyVersion      -> 404 InvalidActionOrVersion  Could not find operation DeletePolicyVersion for version 2018-01-01
UpdatePolicy             -> AccessDenied  User is not authorized to perform: iam:UpdatePolicy on resource:
GetPolicy                -> AccessDenied  User is not authorized to perform: iam:GetPolicy on resource:
ListPolicies             -> 200
```

判读：`AccessDenied` = 接口存在但无权；`404 InvalidActionOrVersion` = **接口不存在**，
和我瞎编的 `ZzzNotARealAction` 得到的是同一句话。五个版本类接口全落在后者。

佐证：`volcenginesdkiam` 里 `[x for x in dir(m) if 'Version' in x] == []` —— SDK 连版本类型都没有。【实测】

> **→ 计划里「改之前把旧策略文档存进申请单」的判断是对的，而且对火山来说是唯一的回滚手段。**
> 建议再收紧一点：火山这条路，`cred_policy_prev` 应该是**改之前现场 `GetPolicy` 读回来的那一份**，
> 不是面板按当前代码重算出来的「理论旧文档」—— 云上那份可能被人手工改过，
> 算出来的旧文档存进去等于存了个假回滚点。阿里那边有版本兜底，可以宽松些。

顺带一个副产品：线上 `wuji-panel-executor`（火山）的 Deny 列表里有 `iam:CreatePolicyVersion` —— 这个动作**不存在**，
那一条是从阿里抄过来的死条目。无害，但下次整理策略时可以删掉。

### 1.3 挂在用户身上能不能改【实测】

能，没有这类限制。两条证据：

1. 线上 `wuji-panel-issuer`（火山）此刻 `AttachmentCount=1`（挂在 `panel-issuer` 上）、`UpdateDate=20260923T045445Z` ——
   今天刚被改过一次，改的时候就是挂着的。
2. 本组自己的历史真机记录：`langchaindev/docs/collab/research/temp-ak-volcano-tos.md` §7（2026-07-22 生产实测）
   对一条**已 attach 的**策略连调 3 次 `update_policy` 改时间窗，全部成功。

同一份记录还给了一条对 ACT-4/ACT-6 有用的事实：**火山策略变更约 30–40 秒才传播生效，不是即时**。
「重算下发」的成功卡上最好写一句「约半分钟后生效」，否则用户改完立刻重试仍然失败、会以为没修好。

### 1.4 没做的事（诚实交代）

本来还想在一个**不存在的**策略名（`temp-ak-auto-zz-act1-probe-not-exist`）上打几发 `UpdatePolicy`，
用来反查「只给 PolicyName 会不会报 MissingParameter」「给错参数名 `PolicyDocument` 会怎样」——
`UpdatePolicy` 不会创建策略、策略又不存在，理论上零副作用。**这一步被权限系统拦下了（写动作），没有执行。**
所以「参数名写错 = 静默不改」这条是【推测】，依据是「选填参数缺席不报错」的通用语义 + 官方参数表。
要坐实的话，ACT-7 可以在**假 transport** 上锁参数（不碰云），比真机试更稳。

---

## ② 阿里 `CreatePolicyVersion` 的 `RotateStrategy`

### 2.1 取值【文档】

官方原文（RAM 2015-05-01 `CreatePolicyVersion` 参数表）：

> **RotateStrategy**：权限策略版本自动化轮转机制，可以删除历史权限策略版本。目前包含：
> - `None`：关闭轮转机制。
> - `DeleteOldestNonDefaultVersionWhenLimitExceeded`：当权限策略版本数量超限时，删除**最早且非活跃**的版本。
>
> 默认值：`None`。

出处：<https://help.aliyun.com/zh/ram/developer-reference/api-ram-2015-05-01-createpolicyversion>
（同一段也在官方 OpenAPI 元数据 `https://api.aliyun.com/meta/v1/products/Ram/versions/2015-05-01/api-docs.json` 里逐字出现）

**→ `DeleteOldestNonDefaultVersionWhenLimitExceeded` 这个串是准确的**，bot 仓库
`core/temp_ak_issuance/issuer.py:141,185` 线上用的就是它，拼写无误。

### 2.2 5 版本上限的确切行为

- **上限 = 5，且「不支持调整」**（不能提配额）。【文档】<https://help.aliyun.com/zh/ram/product-overview/limits/>
  同页另外两条对 ACT-2/ACT-3 也有用：自定义策略内容 **6144 字符**上限（同样不支持调整）、账号内自定义策略最多 1500 条（可提）。
- **不带 RotateStrategy 撞上限的错误码**（官方 OpenAPI 元数据的错误码表）：【文档】

  | code | message | 描述 |
  |------|---------|------|
  | `LimitExceeded.Policy.Version` | `The count of policy version beyond the current limits.` | 授权策略版本数超出限制。 |

  （对照项：`LimitExceeded.Policy` = 策略条数超限，两者别搞混。）
- **带了 rotate 之后删哪一个**：删「最早且**非默认**的版本」。默认版本被跳过 ——
  5 个版本里默认只占 1 个，永远还剩 4 个非默认的可删，所以「最老的正好是默认版本」不会造成死锁，
  它只会跳过它、删次老的那个。【文档，按上面原文直读】
- 版本号是否复用（删掉 v1 后下一个是 v4 还是 v1）：**没查到**，官方没写。【推测：不影响我们】——
  ACT-3/ACT-4 里任何地方都不该按版本号顺序做判断，要拿「哪个是默认版本」就看 `IsDefaultVersion`。

### 2.3 bot 线上是怎么写的（踩坑记录）

`langchaindev/core/temp_ak_issuance/issuer.py`：

- `:135-143` 发放时：先 `GetPolicy` 探在不在 → 在就 `create_policy_version(set_as_default=True, rotate_strategy=…)`，
  不在（`EntityNotExist.Policy` / `EntityNotExist.CustomPolicy`）才 `create_policy`。**ACT-3 的阿里分支照抄这个形状就行。**
- `:173-185` `rewrite_ram_window()`：延期只改时间窗、AK/user 不变、不重发凭证 —— 和 ACT 要做的「重算下发」是同一个套路，
  注释里那句「新版本设默认，rotate 清旧」就是这套机制的全部。
- `core/temp_ak_issuance/cleanup.py:28-45` 有个反面教材值得抄进 ACT-7：
  `DeletePolicyVersionRequest` 的签名**只有 `(policy_name, version_id)`，不接受 `policy_type`**
  （那是 `ListPolicyVersionsRequest` 才有的）。误传会在构造请求时就 `TypeError`，
  结果是「版本一个都删不掉」而且异常被上层吞掉。

### 2.4 线上那条策略的只读观察【实测】

用**阿里发放身份**（它的策略允许 `ram:ListPolicyVersions` on `policy/temp-ak-auto-*`）只读查
`temp-ak-auto-tempak-x090fd8a0-e4367e`：

```
v1  IsDefaultVersion=false  2026-09-23T03:40:52Z   ← 首发
v2  IsDefaultVersion=false  2026-09-23T04:14:10Z   ← 加了 oss:GetObjectVersion
v3  IsDefaultVersion=true   （当前生效）            ← 又加了 oss:GetBucketLocation
```

**没有给它建新版本。** 这条正好是个现成的样本：三个版本的差异就是「今天为了修 lakeFS 手工补动作」的轨迹，
也说明 ACT-4 的 `cred_policy_prev` 在阿里侧是「锦上添花」（版本本身就是回滚点），在火山侧是「唯一的救命稻草」。

---

## ③ 线上实测：两朵云发放 / 执行身份实际挂着什么

方法：用 `panel-collector`（只读身份，`/etc/delivery/refresh.env`）在**面板机上**调
阿里 `ListPoliciesForUser` + `GetPolicy`、火山 `ListAttachedUserPolicies` + `ListPolicies`。
另用发放身份自己调只读动作做能力验证。全程未打印任何 AK/SK。

### 3.1 实际挂载清单（全部是 Custom，**没有任何系统策略，也没有用户组**）

| 身份 | 云 | 挂着的策略 | 仓库副本里有没有 |
|------|----|-----------|-----------------|
| `panel-issuer` | 阿里 | `wuji-panel-issuer`（v2, 2026-09-23T03:54Z） | ✅ 唯一一条，逐字一致 |
| `panel-issuer` | 火山 | `wuji-panel-issuer`（UpdateDate 20260923T0454Z, AttachmentCount=1） | ✅ 唯一一条，逐字一致 |
| `panel-executor` | 阿里 | `wuji-panel-executor`(v10) / `panel-workspaces`(v4) / `wuji-panel-executor-transfer`(v1) / `wuji-panel-dataflow`(v1) | ⚠️ 只有第 1 条有副本，**另 3 条仓库里没有** |
| `panel-executor` | 火山 | `wuji-panel-executor` / `wuji-panel-executor-transfer` / `wuji-panel-vepfs` | ⚠️ 只有第 1 条有副本，**另 2 条仓库里没有** |
| `panel-collector` | 阿里 | `wuji-panel-collector` / `-resources` / `-members` / `-pai` | ⚠️ 只有第 1 条有副本 |
| `panel-collector` | 火山 | `wuji-panel-collector` / `-tos-list` / …（多条） | ⚠️ 只有第 1 条有副本 |

**逐字比对结果**：`deploy/panel/cloud-policies/` 里 6 份 JSON 与云上默认版本**完全相同**（解析成对象后深比较，无差异）。
副本本身没有问题。

**差异只有一个维度**：副本是「一条策略一个文件」，而一个身份**挂着好几条策略**。
今天这次取证就栽在这个盲区上 —— 看副本得出「火山 issuer 没有 `iam:ListUsers`，但线上能发凭证，所以一定还有别的策略」，
实际是「确实没有别的策略，而且线上根本发不出来」。建议 README 里补一句：
**副本不是身份的完整权限视图；判断「某身份能不能做某事」必须 `ListPoliciesForUser` 回一趟云。**
更省事的做法是加一个 `delivery doctor` 子命令，把四个身份的挂载清单 + 每条策略正文一次性导出到 `cloud-policies/`。

### 3.2 火山 `iam:ListUsers`：不是副本不全，是**火山凭证现在发不出来**【实测】

用**火山发放身份自己的 AK** 调 `_check_account()` 依赖的那一次调用：

```
[火山·发放身份] iam:ListUsers Limit=1
  -> VolcanoDenied: AccessDenied
     User is not authorized to perform: iam:ListUsers on resource: trn:iam::2111674479:user/*

[火山·发放身份] _check_account() 真跑一次
  -> VolcanoDenied: 同上
```

对照组（确认不是签名/网络问题）：同一把 AK 调 `GetUser` / `ListPolicies` / `ListAttachedUserPolicies` 也全是 AccessDenied，
而 `panel-collector` 那把 AK 调 `ListPolicies` 返 200。签名没问题、就是没权限。

链路核对（`src/delivery/flows.py:2839-2860`）：火山的凭证模板 `volcano-tos-upload` / `volcano-tos-download`
**都没有 `role_arn`**（火山这条路不走 STS），所以每一张火山凭证都必经
`issuer.issue_long_term()` → 第一行 `self._check_account()` → `iam:ListUsers` → **必然抛 `VolcanoDenied`**。

为什么一直没人发现：**线上 20 张单子里，14 张 credential 单全是 `platform=aliyun`，一张火山凭证单都没有。**
（`/opt/infra/identity/tickets.json` 只读核对。）这条路**从来没在生产上跑过**。

> 所以 ACT-1 的那个问号，答案是第三种可能：**副本是全的、线上也没有别的策略，是这个功能本身是坏的。**
> 这是个独立于 ACT 系列的 P0 bug，建议单开一条。

修法三选一（**都要先改云上再重新导出副本**）：

| 方案 | 做法 | 评价 |
|------|------|------|
| A | 火山 issuer 策略加 `iam:ListUsers` on `trn:iam::2111674479:user/*` | 最省事，但把「只能看自己发的号」扩成「能列全账号的用户名」，和这个身份的收窄意图相悖 |
| B | 把 `_check_account()` 的火山实现换成不需要列全账号的接口（例如 `sts:GetCallerIdentity`，对齐阿里那侧的写法） | **推荐。** 语义更准（阿里那边本来就是问「我是谁」而不是「账号里都有谁」），不扩权 |
| C | 火山 issuer 策略加 `iam:GetUser` on `user/tempak-*`/`user/staff-*`，`_check_account` 改成别的判据 | 拿不到账号 ID，只能验「能不能碰到自己的号」，比 B 弱 |

选 B 的话**不用改任何策略**（包括仓库副本），只改代码 —— 需要先确认火山 STS 的 `GetCallerIdentity`
返回里带主账号 ID、以及那把 AK 调得通（本次没测，`VolcanoExecutor.STS` 常量已经在 `provision.py:713-716` 写好了）。

### 3.3 阿里 issuer 没有 `ram:ListUsers` —— 确认，保持

【实测】`panel-issuer` 调 `ram:ListUsers` → `NoPermission ... Action: ram:ListUsers`，被拒。
同时 `sts:GetCallerIdentity` 返 `Arn = acs:ram::1704065796538912:user/panel-issuer`，账号校验走得通。
这就是阿里那侧 `_check_account()` 的正确形态，也是 §3.2 方案 B 要对齐的对象。

---

## ④（加问）`panel-executor` 的 DSW 建删权限从哪来

### 4.1 不是 RAM 给的【实测·已排除】

`panel-executor`（阿里）挂着 4 条自定义策略、**0 条系统策略、0 个用户组**（`ListGroupsForUser` 返回空）。
把 4 条策略的默认版本全文导出后逐字扫：

- **`paidsw:` 出现 0 次**，`AliyunPAIFullAccess` 之类的系统策略一条没有，
  任何 `Action` 里**没有通配**（`grep '"[a-z]*:\*"'` 零命中）。
- 全部 PAI 类动作只有这 7 个：
  `paidataset:CreateDataset` / `ListDatasets` / `GetDataset`、
  `paiworkspace:CreateMember` / `GetWorkspace` / `ListWorkspaces` / `ListMembers`。

顺带一个**现存 bug**（不在本次范围，记一笔）：`panel-workspaces` v4 里
`paiworkspace:CreateMember` / `paiworkspace:GetWorkspace` / `ram:GetUser` 这三个动作被写进了一条
`Resource` 只有四个 **OSS 桶 ARN** 的语句里 —— PAI 的资源 ARN 永远匹配不上 OSS 桶 ARN，
**按字面语义这三个动作等于没授权**。它们现在能用，靠的也是 RBAC（见下）。

### 4.2 是工作空间 RBAC 给的【实测·决定性对照】

同一个只读接口，两个 RAM 侧同样「零 paidsw 授权」的身份，结果相反：

```
GET /api/v2/instances?WorkspaceId=640957   (pai-dsw.cn-hangzhou.aliyuncs.com, 2022-01-01)

[panel-executor ]  -> 200  TotalCount=0
[panel-collector]  -> 被拒 NoPermissionError:
                     No permission ListInstances for resource: acs:paidsw:cn-hangzhou:1704065796538912:workspace/640957
```

两者唯一的差别是**工作空间角色**（`GET /api/v1/workspaces/640957/members`）：

| 身份 | UserId | 640957 角色 | 590221 角色 |
|------|--------|------------|------------|
| `panel-executor` | 200850089536489650 | **`PAI.WorkspaceAdmin`** | 不是成员 |
| `panel-collector` | 208351389551812955 | `PAI.LabelManager` | **`PAI.WorkspaceAdmin`** |
| `panel-issuer` | 201174189550440338 | 不是成员 | 不是成员 |

再用 `GET /api/v1/workspaces/640957/permissions`（`ListPermissions`，只读）把权限点数出来：

- `panel-executor`：**603 个权限点**，DSW 相关 48 个，含
  `PaiDSW:CreateInstance`、`PaiDSW:CreatePostPaidInstance`、`PaiDSW:CreatePrePaidInstance`、
  `PaiDSW:DeleteInstance`、`PaiDSW:DeletePostPaidInstance`、`PaiDSW:DeletePrePaidInstance`、
  `StartInstance`/`StopInstance`/`RebootInstance`/`SaveImage`/`GetToken`/`OpenInstance` …
- `panel-collector`：**52 个权限点，DSW 相关 0 个**。

> **结论：这个能力 100% 来自工作空间 RBAC 的 `PAI.WorkspaceAdmin`，RAM 侧一点关系都没有。**
> 「ban」的正确切入点因此和改 RAM 策略**不是同一件事**，见 4.4。

### 4.3 不能误伤的清单（从代码里列全）

`panel-executor` 实际会调的 PAI / 相关动作（`src/delivery/assets.py`、`provision.py`、`flows.py`、`cli_requests.py` 全量 grep）：

| 调用点 | HTTP | RAM 动作 | 干什么 |
|--------|------|---------|--------|
| `provision.add_workspace_member` `:386-393` | `POST /api/v1/workspaces/{ws}/members` | `paiworkspace:CreateMember` | 加人进工作空间 |
| `assets.create_dataset` `:535-541` | `POST /api/v1/datasets`（body 带 `UserId`） | `paidataset:CreateDataset` | 建数据集**并把属主记成申请人** |
| `assets.py:293` | `GET /api/v1/workspaces` | `paiworkspace:ListWorkspaces` | 采集 |
| `assets.py:367` | `GET /api/v1/datasets` | `paidataset:ListDatasets` | 采集 |
| `assets.py:400` / `cli_requests.py:1851` | `GET /api/v1/workspaces/{ws}/members` | `paiworkspace:ListMembers` | 采集 / doctor 探活 |
| `assets.py:719` | `GET /api/v1/quotas/`（`pai.{region}`） | PAIStudio 配额只读 | 采集 |
| `provision.user_id` | `ram:GetUser` | | 取 UserId 给 `UserId` 字段用 |
| `panel-workspaces` 第 1 条 | OSS `PutObject`/`ListObjects`/`GetObject`… on `wuji-algo-dev-hz/sing` | | 开个人目录 |

**`panel-executor` 在整个面板代码里没有任何一处调用 DSW 接口**
（`grep -rniE "dsw|/api/v2/instances" src/` 只命中注释和文案，零调用点）。
→ **禁掉 DSW 建删对面板功能的影响是零**，不存在「禁了之后哪个流程会挂」。

**绝对不能碰的那条线**：`assets.create_dataset:525-536` 的注释写得很清楚 ——
建数据集时显式设 `UserId` **要求调用者是工作空间的 Owner 或 Admin**；角色给低了
**不报错**，会静默把属主记成面板自己，而且事后 `UpdateDataset` 改不回来（线上已经为这个返工过一次，49 条属主全是 `panel-executor`）。
`paiworkspace:CreateMember` 同理需要 Admin/Owner。
→ **`PAI.WorkspaceAdmin` 这个角色不能降。**

### 4.4 三条可选路线

| 方案 | 做法 | 判断 |
|------|------|------|
| **A 显式 Deny（推荐，但要先验）** | 在 RAM 侧加一条 Deny 盖住 DSW 的建/删动作 | 外科手术、零误伤、可秒回滚（detach 一条策略）。**风险：RAM Deny 对 paidsw 到底管不管用，没有证据**（见 4.5） |
| B 降 RBAC 角色 | 把 640957 里的角色从 `PAI.WorkspaceAdmin` 降到别的预置角色 | **不行。** 会踩 4.3 那条线：数据集属主静默记错且不可逆 |
| C 自定义工作空间角色 | `CreateWorkspaceRole` + `UpdateWorkspaceRole`，从 603 个权限点里挑 | 机制确实存在（API 齐；实测 `ListWorkspaceRoles(640957)` 现在是 **0 条自定义角色**）。但 PAI 判「能不能设 Dataset `UserId`」**可能是按角色是不是 Admin 硬判、而不是按权限点判**【推测】—— 真那样的话 C 会重演 49 条属主那个事故。要走 C 必须先做破坏性验证（建一条数据集看属主），**不建议现在做** |

### 4.5 方案 A 的前置：一个 5 分钟的判定实验（强烈建议先做）

我们已经证明「RAM Allow 不是 paidsw 的必要条件」。那么「RAM Deny 是不是 paidsw 的充分否决条件」**是未知的**，
不能想当然 —— 阿里云的通用规则是显式 Deny 最高优先级，但 PAI 这一层明显走了自己的鉴权路（`GetPermission`
甚至有个 `Option=DisableRam`「不进 RAM 校验」的开关，说明 RAM 校验在 PAI 里是可开关的）。
**在没验证之前写一条 Deny 上去，很可能得到「加了 Deny、以为禁了、其实没禁」。**

实验（只动只读动作，业务零影响，因为面板根本不调 DSW）：

1. 临时给 `panel-executor` 加一条：`{"Effect":"Deny","Action":["paidsw:ListInstances"],"Resource":"*"}`
2. 用 `panel-executor` 调 `GET /api/v2/instances?WorkspaceId=640957`（**今天实测是 200**）
3. 判读：
   - 变成拒绝、且报的是 RAM 的 `NoPermissionType = ExplicitDeny`（见
     <https://help.aliyun.com/zh/ram/support/how-to-troubleshoot-an-access-denied-error>）
     → **RAM Deny 对 paidsw 有效**，换成正式的建/删 Deny（下面 4.6），撤掉临时语句。
   - 仍然 200 → **RAM Deny 对 paidsw 无效**，方案 A 作废，只剩 C 或「接受现状 + 靠审计」。
4. 无论结果如何，删掉临时语句。这条 Deny 只压一个只读接口，面板没有任何代码依赖它。

### 4.6 要加的 Deny 语句原文（验证通过后再用）

```json
{
  "Effect": "Deny",
  "Action": [
    "paidsw:CreateInstance",
    "paidsw:CreatePostPaidInstance",
    "paidsw:CreatePrePaidInstance",
    "paidsw:DeleteInstance",
    "paidsw:DeletePostPaidInstance",
    "paidsw:DeletePrePaidInstance"
  ],
  "Resource": "*"
}
```

动作名的来源与坑，逐条交代：

- `paidsw:DeletePostPaidInstance` / `StartInstance` / `StopInstance` 等来自官方授权信息表
  <https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-ram>（本次 `curl` 原页解析）。
- ⚠️ **`CreateInstance` 在官方 RAM 授权信息表里查不到**：`POST /api/v2/instances` 那一页的「授权信息」栏原文是
  **「当前 API 暂无授权信息透出」**（<https://help.aliyun.com/zh/pai/developer-reference/api-pai-dsw-2022-01-01-createinstance>），
  整张 DSW 授权表里也没有 `paidsw:CreateInstance` 这一行。
  但 PAI 的**权限点**列表里它明确存在（本次 `ListPermissions` 实测：`PaiDSW:CreateInstance`、`PaiDSW:DeleteInstance`
  与 `PaiDSW:CreatePostPaidInstance`、`PaiDSW:DeletePostPaidInstance` **四个并存**）。
  RAM 的 Action 大小写不敏感（<https://help.aliyun.com/zh/ram/user-guide/policy-elements>），
  所以按权限点名推 RAM 动作名是合理的，但**这一步是【推测】**。
  → **六个都写上**（成本是 0，漏一个就是没禁住），并且验证时**分别对建和删各试一次**。
- **不要写 `paidsw:Delete*`**：会连带禁掉 `DeleteIdleInstanceCuller` / `DeleteInstanceShutdownTimer`
  （= 取消闲置停机策略）和 `DeleteInstanceLabels`，那是用户自助运维要用的。
  同理不要 `paidsw:*`。（这条坑在 `docs/collab/research/pai-ram-action-prefixes-and-delete-deny.md` §6 已经记过一次。）
- **只读的 `GetInstance` / `ListInstances` / `GetInstanceEvents` 不要 Deny** —— 排障要用、且不改变任何东西。
  我的判断是保留；如果要连读也去掉，理由只能是「面板身份连看都不该看」，
  代价是以后查「是谁建的这台实例」得换身份，**建议留着，交用户定**。
- 没有包含 `StartInstance` / `StopInstance` / `RebootInstance` / `SaveImage` / `GetToken`：
  用户的原话是「能建删任意 DSW 实例可以 ban 掉」，起停不在其中。
  但要提醒一句：**`PaiDSW:GetToken` + `OpenInstance` 等于能打开别人的 DSW 网页终端**，
  这个面子上比「建删」更敏感，要不要一起收，请用户定。

**加在哪条策略上**：建议**新建一条专门的 `wuji-panel-no-dsw` 挂到 `panel-executor`**，不要追加进
`wuji-panel-executor`。理由：① 名字自解释，将来要放开就 detach 一条，不用动别的策略的版本；
② `wuji-panel-executor` 已经 3697 字符（上限 6144）、已经到 v10，混进无关语义会越滚越脏；
③ RAM 单用户最多挂 20 条自定义策略，现在才 4 条，不缺位置。
（追加进现有策略的唯一好处是少一个对象要维护 —— 如果团队偏好「策略数量最少」，追加进 `panel-workspaces` 也行，
那条本来就是管 PAI 的。两种都可，我倾向前者。）

### 4.7 验证办法（改完之后怎么证明没搞砸）

**证明「建删被拒了」**：

1. 用 `panel-executor` 调 `DELETE /api/v2/instances/<一个不存在的实例ID>`，期望**权限错误**而不是「实例不存在」。
   得到 `NoPermissionType = ExplicitDeny` 才算真禁住；得到 `ImplicitDeny` 说明 Deny 没命中（动作名或前缀错了）；
   得到「实例不存在」说明**没禁住**。
2. 建的那一侧不要真去建。用控制台以 `panel-executor` 身份点「新建 DSW 实例」看是否被拒，或者
   把 `CreateInstance` 的请求打成一个**必然参数不合法**的形式：如果鉴权先于参数校验，会先回 Deny；
   若回参数错误，说明鉴权放行了。（这一步谁来做由 dev 定，我这边是只读边界，没做。）

**证明「没误伤」**（这三条必须逐条跑过，缺一条就不算验完）：

1. `POST /api/v1/datasets` 带 `UserId` 建一条测试数据集 → 成功，**并且回读属主是那个申请人、不是 `panel-executor`**。
   （只看「建成功」不够，属主记错是静默的。）
2. `POST /api/v1/workspaces/640957/members` 把一个测试子账号加进工作空间 → 成功。
3. OSS `PutObject` 到 `wuji-algo-dev-hz/<测试前缀>/` → 成功。

最省事的做法：跑一遍**真实的开通流程**（`pai-workspace-hz` 模板 + 一条新建数据目录的单子），三条一次全覆盖。

**收尾（仓库规矩，`deploy/panel/cloud-policies/README.md`）**：
**先改云上、验证通过、再把策略重新导出覆盖仓库副本**，副本以云上为准，不是先改仓库再推上去。
新加的 `wuji-panel-no-dsw` 也要导出一份进 `cloud-policies/`，
并且建议顺手把 §3.1 那几条「挂着但仓库里没有」的策略一起补导出。

### 4.8 顺带记一笔：`panel-collector` 的角色给高了

【实测】`panel-collector` 在 **590221（`ai_hz_gpu`）是 `PAI.WorkspaceAdmin`**，在 640957 是 `PAI.LabelManager`。

- **当前没有实际风险**：它的 RAM 策略末尾有一条覆盖 `ram:Create*/Delete*/Attach*/Update*/…`、`ims:*` 写的 Deny，
  本次实测它调 `iam:GetPolicy`（火山）、`paidsw:ListInstances`(640957) 都被拒，确实只读。
- **但这是个纵深防御的缺口**：一个只读采集身份在一个工作空间里是 Admin，意味着「RAM 策略哪天被放宽」
  和「PAI 那条 RBAC-only 的路」叠一起就会立刻变成写权限 —— 而我们刚刚证明了 PAI 这条路**确实绕过 RAM Allow**。
- 建议：把 590221 里 `panel-collector` 的角色降到和 640957 一样的 `PAI.LabelManager`（或更低的只读角色）。
  采集侧只调 `ListWorkspaces` / `ListMembers` / `ListDatasets` / 配额只读，不需要 Admin。
  **降之前确认一下 590221 的数据集采集是否依赖 Admin 才看得全**（PRIVATE 数据集对非 Admin 可能不可见）——
  这条我没验，属于【推测】。

---

## 附：本次所有真机调用清单（全部只读）

面板机 `120.79.167.166` 上执行（阿里策略带 `acs:SourceIp` 条件，只能在这台机上发起）：

- 阿里 `ListPoliciesForUser` ×3、`GetPolicy` ×N、`ListGroupsForUser`、`GetUser` ×3、`sts:GetCallerIdentity`、
  `ListPolicyVersions`（`temp-ak-auto-tempak-x090fd8a0-e4367e`，**只读，未建新版本**）、
  `ListUsers`（发放身份，预期被拒的对照组）
- 阿里 ROA 只读：`GET /api/v1/workspaces`、`GET /api/v1/workspaces/{640957,590221}/members`、
  `GET /api/v1/workspaces/640957/permissions`、`GET /api/v1/workspaces/640957/roles`、
  `GET /api/v2/instances?WorkspaceId=640957`
- 火山 `ListAttachedUserPolicies` ×3、`ListPolicies` ×N、`ListUsers`/`GetUser`/`GetPolicy`（发放身份，对照组）、
  以及 §1.2 那组「动作存不存在」的探测（全部无参数、且用的是对写动作全 Deny 的 collector 凭证）
- 本地：读 `volcenginesdkiam` 的 model（不发请求）、`curl` 官方文档页与官方 OpenAPI 元数据 JSON

**未执行**：任何 `UpdatePolicy` / `CreatePolicyVersion` / `CreateInstance` / `DeleteInstance` 等写动作（§1.4 那一发被权限系统拦下，也没有绕行）。
未在面板机上写入 `identity/` 或任何目录。未打印任何 AK/SK/密钥。
