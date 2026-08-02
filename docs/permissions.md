# 权限模型

相关：[架构](architecture.md)｜[CI/CD](cicd.md)｜[运维](runbook.md)｜[使用入门](onboarding.md)

---

## 1. 四套授权面

一次操作要成功，**每一层都必须放行**；任意一层拒绝就失败。它们互不替代：

```
有效权限 = RAM ∩ PAI Workspace 角色 ∩ CPFS Fileset/POSIX ∩ lakeFS 权限
显式 Deny 优先于任何 Allow
```

| 层 | 回答的问题 | 不回答的问题 |
|---|---|---|
| ① 阿里云 RAM | 能不能调这个云 API | 工作空间内的算法权限细分 |
| ② PAI Workspace 角色 | 在这个工作空间里是什么身份 | 文件系统里实际能看到什么 |
| ③ CPFS Fileset / POSIX | 哪些目录能读写 | 能不能提交作业 |
| ④ lakeFS | 能读哪个 repo 的哪个 ref | 阿里云侧任何权限 |

两个最常见的排查陷阱：

- **RAM 在、PAI 成员被删** → 能登录 PAI，但进不了目标工作空间。
- **PAI 成员在、RAM 被撤** → 在成员列表里，但调 API 全失败。

所以人员进出必须**同时**改 RAM 授权和 PAI 成员，且放在同一个 PR。

---

## 2. 一个必须知道的平台硬限制

2026-08-02 读取阿里云官方系统策略 `AliyunPAIFullAccess` 确认：

```
Effect: Allow   Resource: *   Action: [paidlc:*, paidataset:*, paiworkspace:*]
```

**`paidlc`、`paidataset`、`paiworkspace` 三个命名空间在官方定义中全部是
`Resource: "*"`。** 也就是说，RAM 层面**无法**把权限限定到某个 Dataset、
某个 Job 或某个 Workspace。

这不是我们偷懒，是平台能力的边界。因此这三类权限的收敛必须靠另外三层：

1. **收窄 Action 列表** —— `deploy/ram/*.json` 在做的事；
2. **PAI Workspace 成员角色** —— 决定这个人在这个空间里是谁；
3. **流水线 Environment 审批** —— 决定这次操作要不要人点头。

评审策略时看到 `Resource: "*"` 出现在 `pai*` 前缀上，是正常的；出现在 `oss:` 上，
就要追问为什么——OSS 是可以精确到桶和前缀的。

---

## 3. 身份清单

### 数据面（5 个）

| 身份 | 能做 | 明确不能做 | 谁能扮演 |
|---|---|---|---|
| `dataset-sink-<env>-materializer` | 读 lakeFS 后端固定 Commit、写 CPFS release、读写 staging 前缀 | 注册 PAI 版本、提交训练 | GitHub OIDC，且 sub 必须是 `environment:<审批环境>` |
| `dataset-sink-<env>-register` | `CreateDatasetVersion`、`ListDatasetVersions` | 读 lakeFS 后端桶、提交训练 | 同上 |
| `dataset-sink-<env>-dlc-submit` | 提交/观察 DLC Job、解析 Dataset 版本 | 改写数据版本、读 lakeFS 后端 | 同上 |
| `dataset-sink-<env>-training-runtime` | 只读已发布归档、写 output 前缀 | 读 lakeFS 后端与 staging、注册版本、提交作业 | **PAI 服务**（`pai-dlc` / `pai-dsw`），不是 CI |
| `pai-<env>-developers`（用户组） | 浏览 Workspace、数据集、作业日志 | 取长期密钥、改写数据版本、读原始数据 | RAM 用户加组 |

拆分原则：**沉降、注册、训练是三个不同的信任级别**。能写数据的不能宣布数据可用，
能宣布可用的不能提交训练，能训练的不能碰训练集。任何单个身份泄露都不足以完成
一次完整的数据污染。

训练运行角色**刻意不走 OIDC**：如果 CI 也能假设它，流水线就获得了读训练数据的
能力，「运行身份」和「交付身份」的隔离就没了。

### 交付面（3 个，由 bootstrap 管理）

| 角色 | 能做 | 明确不能做 |
|---|---|---|
| `TerraformPlanRole` | 只读全部被管资源、读 state、加锁 | 任何写操作（有兜底 Deny） |
| `TerraformPlatformApplyRole` | 管 OSS 桶等平台资源、读写 state | 改任何 RAM/IMS、改 PAI 成员、删有状态存储 |
| `TerraformAccessApplyRole` | 管 RAM 策略/角色/组、管 PAI 成员 | **改自己**、改信任锚、建长期密钥、改基础设施 |

---

## 4. 防提权设计

这是整套权限模型里最容易被做错的部分。

### 4.1 plan 角色必须完全只读

`TerraformPlanRole` 的 OIDC `sub` 是 `repo:<org>/<repo>:pull_request`。
**fork 仓库发起的 PR 也会产生这个 sub。** 所以任何能给这个仓库开 PR 的人，
都能拿到这个角色。它必须一点写权限都没有——策略里除了精确的 Allow，
还有一条覆盖所有写 Action 的兜底 Deny。

### 4.2 权限层与基础设施层必须分开

`TerraformPlatformApplyRole` 被 Deny 了整个 `ram:*` / `ims:*` 写面。

理由：基础设施变更远比权限变更频繁。如果两者共用一个角色，一次「改个 tag」
的 PR 就有能力顺手改一条 RAM 策略，而审批人正盯着 tag 看。分开之后，
任何权限变更都只能出现在 access 层的 plan 里，无处藏身。

### 4.3 权限角色不能改自己

`TerraformAccessApplyRole` 管 RAM 策略。如果它能改自己的信任策略或权限策略，
它就具备无限提权能力——一次 apply 就能把自己变成 `AdministratorAccess`。

所以策略里有两条针对性的 Deny：

```json
{ "Effect": "Deny",
  "Action": ["ram:UpdateRole","ram:DeleteRole","ram:AttachPolicyToRole","ram:DetachPolicyFromRole"],
  "Resource": ["acs:ram:*:<账号>:role/TerraformPlanRole", "...PlatformApplyRole", "...AccessApplyRole"] }

{ "Effect": "Deny",
  "Action": ["ram:CreatePolicyVersion","ram:DeletePolicy","ram:DeletePolicyVersion","ram:SetDefaultPolicyVersion"],
  "Resource": ["acs:ram:*:<账号>:policy/TerraformPlanRolePolicy", "..."] }
```

再加一条 `Deny ims:*`：OIDC 身份提供商是整条流水线的信任锚，谁能改它就能重定向
整条信任链。它只归 bootstrap 层管，而 bootstrap 只有管理员能跑。

### 4.4 审批和授权是同一件事

apply 角色的信任策略只接受 `sub = repo:<org>/<repo>:environment:<name>`。
而这个 sub 只有在 Job 声明了 `environment: <name>` 时才会产生，
声明了就会触发该 Environment 的 required reviewers。

**不批准 → 没有 token → 拿不到凭证。** 审批不是流程上的一道门，
它在密码学上就是获得权限的前提。

`subjects` 变量因此有硬性校验：不允许出现 `*`。一个
`repo:org/repo:*` 就等于把生产权限开放给该仓库的任意分支和任意 fork PR。

### 4.5 管理员数量护栏

PAI Workspace 管理员能修改成员关系，等于能给自己加权限。
`pai-workspace-access` 模块用 `terraform_data` 上的 `lifecycle precondition`
限制人数：生产超过 1 人时 plan **报错并退出码 1**。

这里必须用 `precondition` 而不是 `check` 块。实测（terraform 1.15.8）：

| 机制 | 失败时 | plan 退出码 | 能否阻断 apply |
|---|---|---|---|
| `check` 块 | Warning | 0 | **否** |
| `lifecycle precondition` | Error | 1 | 是 |

一个只会打印警告的「护栏」比没有护栏更糟——它让人以为有保护。
本仓库里 `check` 只用于**漂移检测**（例如发现有人手工往用户组里加人），
那种场景告警才是正确形态，因为首次 apply 时资源尚未创建，用 `precondition`
会让第一次 apply 直接失败。

### 4.6 生产删除护栏

`deny_destructive` 策略对生产的所有角色 Deny 掉删除类 Action。
副作用是「必须删除后重建」的变更也会失败——**这正是想要的**：
这类变更应该被迫走 BreakGlass 审批，而不是被流水线静默执行。

Terraform 侧还有 `prevent_destroy`。两层各拦一次：
RAM 是云平台侧保护，`prevent_destroy` 是 Terraform 侧保护。

---

## 5. 已有 RAM 用户怎么接入（继承）

账号里通常已经有一批 RAM 用户，他们各自挂着与本项目无关的策略。
本项目**不接管**这些用户，也不改动他们已有的权限。

### 接入点是用户组，不是用户

```
已有 RAM 用户（Terraform 不接管）
        ↓ alicloud_ram_user_group_attachment
pai-<env>-developers 用户组（Terraform 管理）
        ↓ alicloud_ram_group_policy_attachment
本项目的策略（Terraform 管理）
```

在 `infra/envs/<env>/access/terraform.tfvars` 里：

```hcl
developer_group_name = "pai-prod-developers"
developer_user_names = ["alice", "bob"]   # 已存在的 RAM 用户登录名
```

用组而不是逐个给用户附策略，是为了让「谁有这个权限」有唯一答案：
看组成员，而不是逐个用户翻策略。N 个用户共用 1 套策略，改策略只改一处。

### 最重要的一点：Deny 会覆盖他们已有的 Allow

RAM 的有效权限是所有 Allow 的**并集**，但**显式 Deny 优先于任何 Allow**。

所以用户一旦入组，本项目策略里的 Deny 会覆盖他通过其他策略已经拿到的权限。
举个真实的例子：某用户挂着 `AliyunOSSFullAccess`，入组后本项目的
`DenyDataPlaneAccess` 会让他**读不了 lakeFS 后端桶**——即使他有 FullAccess。

这既是保护（防止有人绕过发布协议直接读原始数据），也是风险（可能意外阻断
他的本职工作）。**加人之前先审计他现有的权限**：

```bash
aliyun ram ListPoliciesForUser --UserName <用户名>
```

如果他的本职工作需要访问被我们 Deny 的资源，就不要把他放进这个组——
给他单独建一个组，或者调整策略的资源范围。

### 手工加人会被发现

`alicloud_ram_user_group_attachment` 是逐用户绑定的，不具备「权威集合」语义。
为此模块里有一个 `check` 块：用 `data "alicloud_ram_users"` 读取组的实际成员，
与 `developer_user_names` 比对，发现多出来的人就告警。

它是**警告不是错误**（原因见 4.5），所以不会阻断流水线，但会出现在 plan 输出里，
评审时能看到。

### 迁移建议

面对一个已经有几十个 RAM 用户的账号，不要一次性全塞进组：

1. **先审计**：列出每个用户现有的策略，标出谁挂着 `AliyunRAMFullAccess`、
   `AdministratorAccess` 这类能绕过整套设计的权限。
2. **先分类**：谁只需要用数据集（进 developers 组），谁需要运维作业
   （PAI.AlgoOperator），谁只需要看（Guest）。
3. **小批量迁移**：先放 2～3 个人，确认他们的日常工作没被 Deny 影响。
4. **再收窄**：逐步移除他们直接附加的宽泛策略，让权限只来自组。
5. **最后清理**：撤销长期 AccessKey。

第 1 步不能跳过。**持有 `AliyunRAMFullAccess` 的用户能直接删掉我们所有的
Deny 语句**，对他而言这套权限设计等于不存在。这类账号要么降权，要么就得承认
整套模型在他身上不生效。

## 6. 资源隔离：CI/CD 怎么保证只碰自己的资源

账号里往往同时跑着别的项目。本项目的 CI 拿到的是能改基础设施和权限的角色，
必须回答一个问题：**它凭什么碰不到别人的资源？**

答案不能是「因为 Terraform 代码只写了自己的资源」。代码会写错，PR 会被绕过，
`terraform import` 能把任意资源拉进 state。隔离必须在**权限层**强制。

### 六层隔离，从弱到强

| 层 | 机制 | 挡住什么 | 强度 |
|---|---|---|---|
| 1 | Terraform state 分层 | 一层的误操作波及不到另一层的资源 | 弱：只防意外，不防越权 |
| 2 | 资源名前缀 + ARN 限定 | CI 角色够不到不以本项目前缀开头的资源 | **中：本仓库的主力机制** |
| 3 | 角色职责分离 | 基础设施角色改不了权限，权限角色改不了基础设施 | 中 |
| 4 | 显式 Deny 护栏 | 删除类操作即使被 Allow 也执行不了 | 中 |
| 5 | 资源组（Resource Group） | 官方的账号内隔离单元，可按资源组授权 | 强（本仓库**尚未实现**） |
| 6 | 独立阿里云账号 | 跨账号默认不可达 | 最强 |

### 第 2 层：本仓库实际怎么做的

`infra/bootstrap` 的 `managed_name_prefixes`（默认 `["dataset-sink-", "pai-"]`）
把 CI 角色的写权限限定到匹配前缀的 ARN：

```hcl
managed_bucket_arns = ["acs:oss:*:<账号>:dataset-sink-*", "acs:oss:*:<账号>:dataset-sink-*/*", ...]
managed_policy_arns = ["acs:ram:*:<账号>:policy/dataset-sink-*", ...]
managed_role_arns   = ["acs:ram:*:<账号>:role/dataset-sink-*", ...]
managed_group_arns  = ["acs:ram:*:<账号>:group/pai-*", ...]
```

于是：

- `TerraformPlatformApplyRole` 只能改 `dataset-sink-*` 开头的桶。别的项目的桶，
  连 `PutBucketAcl` 都调不动。
- `TerraformAccessApplyRole` 只能增删 `dataset-sink-*` 的策略和角色、`pai-*` 的组。
  别的项目的自定义策略，它删不掉。

**代价是命名纪律**：本项目创建的资源必须以这些前缀开头，否则 apply 会因权限
不足失败。这是刻意的——早失败，且失败原因明确。`dataset_bucket` 变量因此有
`startswith` 校验，在 plan 阶段就拦住不合规的名字，而不是等到 apply 报一个
难懂的权限错误。

### 一个无法按前缀收窄的地方

`ram:AddUserToGroup` / `ram:RemoveUserFromGroup` 的资源涉及用户，而要把**已有**
用户加进本项目的组，就必须能指向任意用户，没法按前缀限制。

约束由组 ARN 承担：即使能指向任意用户，也只能把他加进 `pai-*` 开头的组，
因为别的组这个角色碰不到。残余风险是可以把某人从本项目的组里移除——
影响面限于本项目。

### PAI 侧没有资源级隔离

前面第 2 节说过：`paidataset:*`、`paidlc:*`、`paiworkspace:*` 在官方定义里都是
`Resource: "*"`。所以**如果账号里有别的团队也在用 PAI，本项目的角色在 RAM 层
挡不住它去动别人的 Dataset 和 Job**。

这种情况下只能靠：

- PAI Workspace 成员角色（别的团队用别的 Workspace，本项目的角色不是其成员）；
- 或者干脆分账号。

如果 PAI 上有多团队共存且数据敏感，**分账号是唯一可靠的答案**，不要指望 RAM。

### 什么时候该升级到第 5、6 层

| 情况 | 建议 |
|---|---|
| 账号里只有本项目 | 现有的前缀隔离足够 |
| 账号里有别的项目，但资源类型不重叠 | 现有机制足够，保持命名纪律 |
| 账号里有别的团队也在用 PAI | 上资源组，或分账号 |
| 生产数据受合规约束 | 分账号，用资源目录统一管理 |
| 需要按项目出账 | 资源组或分账号 |

资源组的接入方式：给所有资源加 `resource_group_id`，RAM 策略用
`acs:rm:*:<账号>:resourcegroup/<id>` 或 `Condition: acs:ResourceGroupId`。
本仓库预留了 `tags` 但**没有实现资源组**——需要的话再加，改动集中在
`infra/bootstrap` 的 ARN 构造和各资源的 `resource_group_id` 参数。

## 7. 长期凭证

整套设计不使用任何长期 AccessKey：

- CI 走 GitHub OIDC → RAM 角色 → STS 临时凭证（默认 1 小时）。
- 训练任务走 PAI 服务角色。
- 人走 RAM 用户 + 控制台/CLI 登录。

研发用户组的策略里 `ram:CreateAccessKey` / `ram:UpdateAccessKey` 是显式 Deny 的。
原因很直接：一旦有人给自己建了长期 AK，基于短期凭证的审计、轮转和撤销
全部失效，而且这个 AK 可能存活数年无人知晓。

唯一的例外是 bootstrap 阶段的临时管理员用户——跑完就应该降权或删除。

---

## 8. 变更流程

| 变更类型 | 路径 | 审批 |
|---|---|---|
| 加/减 PAI 成员 | 改 `infra/envs/<env>/access/terraform.tfvars` 的 `pai_members` | CODEOWNERS 安全评审 + `production-access` Environment |
| 改 RAM 策略 | 改 `policies.tf` + `make render-ram` | 同上 |
| 改 CI 角色/信任锚 | 改 `infra/bootstrap/` | 管理员手工执行，不走流水线 |
| 紧急删除生产资源 | BreakGlass 角色 | 双人审批，用完即撤 |

**不要在控制台手工加权限。** 那样加的权限不在代码里，下一次 apply 会被
Terraform 收回，而且没有任何记录说明是谁、为什么加的。

---

## 9. 审计

| 看什么 | 在哪看 |
|---|---|
| 谁在什么时候调了什么 API | ActionTrail |
| 当前谁有什么权限 | `infra/envs/*/access/terraform.tfvars` + `terraform output` |
| 生效的策略内容 | `deploy/ram/*.json`（自动生成，CI 保证与 Terraform 一致） |
| 谁是 Workspace 管理员 | `terraform output pai_admin_members` |
| 权限是谁改的 | git log + PR 评审记录 |
| 有没有绕过流水线的手工改动 | `terraform plan` 出现 drift |
