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
`pai-workspace-access` 模块里有 `check` 块：生产环境超过 1 个管理员时 plan 直接失败。

### 4.6 生产删除护栏

`deny_destructive` 策略对生产的所有角色 Deny 掉删除类 Action。
副作用是「必须删除后重建」的变更也会失败——**这正是想要的**：
这类变更应该被迫走 BreakGlass 审批，而不是被流水线静默执行。

Terraform 侧还有 `prevent_destroy`。两层各拦一次：
RAM 是云平台侧保护，`prevent_destroy` 是 Terraform 侧保护。

---

## 5. 长期凭证

整套设计不使用任何长期 AccessKey：

- CI 走 GitHub OIDC → RAM 角色 → STS 临时凭证（默认 1 小时）。
- 训练任务走 PAI 服务角色。
- 人走 RAM 用户 + 控制台/CLI 登录。

研发用户组的策略里 `ram:CreateAccessKey` / `ram:UpdateAccessKey` 是显式 Deny 的。
原因很直接：一旦有人给自己建了长期 AK，基于短期凭证的审计、轮转和撤销
全部失效，而且这个 AK 可能存活数年无人知晓。

唯一的例外是 bootstrap 阶段的临时管理员用户——跑完就应该降权或删除。

---

## 6. 变更流程

| 变更类型 | 路径 | 审批 |
|---|---|---|
| 加/减 PAI 成员 | 改 `infra/envs/<env>/access/terraform.tfvars` 的 `pai_members` | CODEOWNERS 安全评审 + `production-access` Environment |
| 改 RAM 策略 | 改 `policies.tf` + `make render-ram` | 同上 |
| 改 CI 角色/信任锚 | 改 `infra/bootstrap/` | 管理员手工执行，不走流水线 |
| 紧急删除生产资源 | BreakGlass 角色 | 双人审批，用完即撤 |

**不要在控制台手工加权限。** 那样加的权限不在代码里，下一次 apply 会被
Terraform 收回，而且没有任何记录说明是谁、为什么加的。

---

## 7. 审计

| 看什么 | 在哪看 |
|---|---|
| 谁在什么时候调了什么 API | ActionTrail |
| 当前谁有什么权限 | `infra/envs/*/access/terraform.tfvars` + `terraform output` |
| 生效的策略内容 | `deploy/ram/*.json`（自动生成，CI 保证与 Terraform 一致） |
| 谁是 Workspace 管理员 | `terraform output pai_admin_members` |
| 权限是谁改的 | git log + PR 评审记录 |
| 有没有绕过流水线的手工改动 | `terraform plan` 出现 drift |
