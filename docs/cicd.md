# CI/CD

相关：[架构](architecture.md)｜[权限](permissions.md)｜[运维](runbook.md)｜[使用入门](onboarding.md)

---

## 三条流水线

| 流水线 | 触发 | 用途 | 是否需要云凭证 |
|---|---|---|---|
| [`ci.yml`](../.github/workflows/ci.yml) | 每个 PR / push main | 代码校验 | **否** |
| [`terraform.yml`](../.github/workflows/terraform.yml) | `infra/**` 变更 | 基础设施与权限交付 | 是（OIDC） |
| [`dataset-release.yml`](../.github/workflows/dataset-release.yml) | 手动触发 | 数据集发布 | 是（OIDC，每步换角色） |

刻意让 `ci.yml` 完全不碰凭证：绝大多数 PR 只需要代码校验，没有理由让它们
经过任何有权限的路径。

---

## 1. ci.yml —— 六个门禁

| Job | 检查什么 |
|---|---|
| `lint` | `ruff check` + `ruff format --check` |
| `test` | 单元测试，Python 3.9 与 3.11 双版本（3.9 是 `requires-python` 下限） |
| `e2e` | `make e2e`：临时目录模拟 CPFS，跑完整发布链路 |
| `shell` | `shellcheck` |
| `terraform` | `fmt -check` + 逐目录 `init -backend=false` + `validate` |
| `ram-policies-in-sync` | 重新渲染 `deploy/ram/*.json`，要求无 diff |

最后一个值得说明：`deploy/ram/` 是给人评审和审计看的策略副本，实际生效的策略由
Terraform 生成。两份内容一旦不一致，评审看的就是过期文档——**这比没有文档更危险**。
所以 CI 重新渲染一遍并要求 `git diff --exit-code`。

渲染能在无凭证的 PR 阶段跑，是因为 `render-ram-policies.sh` 用的是
`terraform console` 求值 `local.policy_documents`——它只需要 `init -backend=false`，
不需要 state，也不需要云凭证。

---

## 2. terraform.yml —— 审批内容 == 执行内容

```
PR (infra/**)
  → 假设 TerraformPlanRole（只读）
  → init / plan -out=tfplan
  → tfplan 上传为 artifact，plan 文本评论到 PR
  → 检测 destroy / replace 并告警
  → 代码评审（CODEOWNERS：权限面需安全团队）

合并 main
  → GitHub Environment 人工审批（阻塞）
  → 假设对应的 Apply 角色
  → 下载 plan 阶段那一个 tfplan
  → apply 该 tfplan
```

### 为什么 apply 不重新 plan

如果 apply 阶段重新跑一次 plan，审批时看到的变更和实际执行的变更就可能不一致
——中间任何状态变化（别人改了资源、data source 结果变了）都会让实际动作偏离。
那样审批就只是仪式。

所以 apply 消费的是 plan 阶段产出的**同一个 tfplan 文件**。

### 审批即授权

apply Job 声明 `environment: production`，这做了两件事：

1. 触发该 Environment 的 required reviewers，Job 阻塞等待；
2. 让 GitHub 签发的 OIDC token 的 `sub` 变成
   `repo:<org>/<repo>:environment:production`。

而 apply 角色的信任策略**只接受这个 sub**。不批准 → 没有 token → 拿不到凭证。
审批不是流程上的一道门，它在密码学上就是获得权限的前提。

### 四个矩阵条目，两个 Environment

| layer | Environment | apply 角色 |
|---|---|---|
| `dev-platform` / `dev-access` | `development` | 平台 / 权限 |
| `prod-platform` | `production` | 平台 |
| `prod-access` | `production-access` | 权限 |

生产权限层用单独的 Environment，可以配置和基础设施不同的审批人
（安全团队）。`max-parallel: 1` 保证两层不并发写 state。

### 并发与锁

```yaml
concurrency:
  group: terraform-${{ github.ref }}
  cancel-in-progress: false
```

`cancel-in-progress` 必须是 `false`。取消一个正在 apply 的 Job 会留下脏的
state 锁，还可能让 state 和实际资源不一致。宁可排队。

---

## 3. dataset-release.yml —— 每步换身份

手动触发（数据集发布是有意为之的动作，不该由代码推送触发）。

```
preflight       ← 校验配置齐全、拦截 latest/main 这类可变 ref
  ↓
materialize     ← DatasetMaterializerRole，自托管 runner（要挂 CPFS）
  ↓ verify --deep
build-request   ← 无云权限，产出可人工审阅的 JSON
  ↓
register-dry-run ← DatasetRegisterRole，只 dryrun
  ↓
  人工审批（dataset-release Environment）
  ↓
register        ← DatasetRegisterRole，--execute
  ↓
smoke-test      ← DlcSubmitRole，提交冒烟训练
```

### preflight 存在的理由

没有它的话，少配一个变量会表现为流水线中段莫名其妙的失败，排查时分不清是
「配置没做好」还是「数据有问题」。preflight 把这两类问题在第一步就分开，
并给出可执行的报错。

它也拦截可变引用：`ref` 填 `latest` / `main` / `master` / `HEAD` 直接失败。

### 为什么 materialize 需要自托管 runner

CPFS 在 VPC 内，**GitHub 托管 runner 到不了**。preflight 检查
`CPFS_RUNNER_LABEL` 变量，没配就明确报错并说明原因，而不是让流水线跑到一半
在挂载失败上崩掉。

替代方案是把沉降改成在 ACK 里起 Job，流水线只负责触发和收结果。

### 为什么 dry-run 和 execute 分成两个 Job

dry-run 让审批人看到将要发出的**确切请求体**，而不是一段描述。
审批的对象是具体内容，不是意图。

`register-pai` 本身默认就是 dry-run，`--execute` 必须显式给出——
即使有人绕过流水线手工跑，默认也不会改动 PAI。

### 幂等

`register-pai --execute` 会先按 lakeFS Commit `ListDatasetVersions` 查重：

- 已存在同 Commit 的版本 → 返回 `EXISTS`，不重复创建；
- 已存在但 `manifest_sha256` 不一致 → 抛 `ReleaseConflictError` 拒绝执行。

所以重跑流水线是安全的，而「同一个 Commit 对应两份不同数据」会被当场拦下。

---

## 4. 需要配置的变量

见[运维手册 1.4 节](runbook.md)。分三类：

- **仓库级 Variables**：OIDC Provider ARN、plan 角色 ARN、region、state 后端参数。
- **Environment 级 Variables**：各环境的 apply 角色 ARN。
- **Secrets**：只有 lakeFS 凭证（`LAKEFS_ACCESS_KEY_ID` / `LAKEFS_SECRET_ACCESS_KEY`）。
  阿里云侧**没有任何 Secret**——全部走 OIDC 临时凭证。

如果哪天发现仓库里多了一个阿里云 AccessKey 类型的 Secret，那说明有人绕过了
这套设计，应该当作事故处理。

---

## 5. 分支保护

流水线本身不是安全边界，除非仓库配置到位：

- `main` 禁止直接推送；
- PR 需要评审，`.github/CODEOWNERS` 对 `infra/envs/*/access/`、
  `infra/modules/*-roles/`、`deploy/ram/`、`.github/workflows/` 要求安全团队评审；
- Environment 配置 required reviewers；
- fork PR 不授予任何有写权限的角色。
