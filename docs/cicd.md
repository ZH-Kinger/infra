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

手动触发（数据集发布是有意为之的动作，不该由代码推送触发）。四种模式：

| mode | 适用场景 | 前置条件 |
|---|---|---|
| `cpfs-ingest` | CPFS 上处理完的**新数据**，还没有 Commit | staging 目录已按 release 布局组织好 |
| `oss-ingest` | **数据本来就在对象存储上**（存量数据的主路径） | 该前缀已在数据源注册表里，且 mode 不是 workspace |
| `certify` | CPFS staging 已就绪，且 Commit **已存在** | 有 Commit 和 manifest |
| `materialize` | 从 lakeFS 拷贝到 CPFS | 有 Commit/Tag 和 manifest |

```
preflight        ← 校验配置齐全、按模式校验必填参数、拦截 latest/main 这类可变 ref
  │                oss-ingest 还会校验数据源已注册且能作为 Commit 来源
  │
  ├─ [仅 cpfs-ingest]
  │  ingest-archive   ← DatasetMaterializerRole，CPFS runner
  │  │                  scan（无云权限）→ archive 到对象存储（幂等可续传）
  │  │
  ├─ [仅 oss-ingest]
  │  oss-scan         ← DatasetMaterializerRole，CPFS runner（走 OSS 内网端点）
  │  │                  scan-oss：列举存量前缀 + 算 SHA-256。**没有 archive**
  │  ↓
  │  ingest-commit    ← **无任何阿里云身份**，只有 lakeFS 凭证，托管 runner
  │                     lakeFS 零拷贝 import → Commit（+ Tag）
  │                     两条 ingest 路径共用，只有「import 的源在哪」不同
  ↓
publish          ← DatasetMaterializerRole，CPFS runner
                   四种模式在这里汇合：certify 或 materialize
  ↓ verify --deep
build-request    ← 无云权限，产出可人工审阅的 JSON
  ↓
register-dry-run ← DatasetRegisterRole，只 dryrun
  ↓
  人工审批（dataset-release Environment）
  ↓
register         ← DatasetRegisterRole，--execute
  ↓
smoke-test       ← DlcSubmitRole，提交冒烟训练
```

### 为什么 `ingest-commit` 单独成一个 job

它是整条流水线里唯一**不假设任何阿里云角色**的写操作步骤：

```yaml
ingest-commit:
  runs-on: ubuntu-latest
  permissions:
    contents: read      # 刻意不要 id-token
```

建 Commit 这个动作只需要 lakeFS 凭证，不需要碰数据。如果把它并进
`ingest-archive`，它就顺带获得了 OSS 写权限——而它并不需要。
**不需要碰数据的步骤，就不该有碰数据的能力。**

顺带一个好处：它不需要 CPFS，所以能跑在 GitHub 托管 runner 上，
不占用稀缺的自托管 runner。

### scan 为什么放在假设角色之前

`scan` 纯本地计算，不需要云权限。放在假设角色之前，"staging 不干净"
这类错误就不会白白消耗一次 STS 凭证，也让最常见的失败最早发生。

### 四种模式怎么汇合

`publish` 用 `needs: [preflight, ingest-commit]`。`certify` / `materialize` 模式下
`ingest-commit` 是 skipped，而 GitHub 默认会连带跳过下游 job，所以要显式接受：

```yaml
if: >-
  ${{ !cancelled()
  && needs.preflight.result == 'success'
  && (needs.ingest-commit.result == 'success' || needs.ingest-commit.result == 'skipped') }}
```

`ingest-commit` 自己也有同样的问题，而且更绕：它 `needs` 两条 ingest 前置，
但每次只有一条真正跑，另一条必然 skipped。所以它的 `if` 要同时接受两者的
skipped，否则 `oss-ingest` 会因为 `ingest-archive` 被跳过而连带不跑。

Commit 的来源也按模式解析——两条 ingest 路径都来自 import：

```yaml
RESOLVED_COMMIT: >-
  ${{ (inputs.mode == 'cpfs-ingest' || inputs.mode == 'oss-ingest')
  && needs.ingest-commit.outputs.commit_id || inputs.ref }}
```

### oss-ingest 为什么没有 archive 步骤

那正是它的价值。字节已经在持久位置上，而 lakeFS import 是零拷贝的——
**整条路径不把任何字节搬到新位置**。`cpfs-ingest` 必须先归档，只是因为
CPFS staging 不是持久位置。

但它仍要把每个对象**读**一遍算 SHA-256（对象存储不提供）。这是全链路唯一的
一次全量读，所以放在 VPC 内的 runner 上走内网端点。`--no-digest` 能跳过，
但 manifest 随发布固化，事后补不上——那个 release 永久失去深度校验能力。

### oss-ingest 的 URI 必须是 s3:// 而不是 oss://

这条我第一版就写错了。lakeFS import 源的 scheme 要匹配 **lakeFS 自己的
blockstore adapter 类型**，不是云厂商的名字——OSS 是通过 lakeFS 的 `s3` adapter
访问的（`blockstore.type: s3` + OSS 的 S3 兼容端点）。

2026-08-03 在真实 lakeFS 1.84.1 + OSS 后端上对照实测：

| `--object-store-uri` | 结果 |
|---|---|
| `oss://<bucket>` | 失败：`invalid storage scheme oss: invalid address` |
| `s3://<bucket>` | 成功，4 个对象零拷贝 import |

坏在这个错**要等 import 任务在服务端跑起来才出现**，堆栈指向 lakeFS SDK 内部，
看不出真正原因是 scheme 写错。所以 `commit` 现在会在本地先校验一次
（`assert_lakefs_import_scheme`），把它变成一句能直接照做的话。
`tests/unit/test_ingest.py` 里有一条测试直接读这个 workflow 文件，确认它用的是
`s3://`——这一行写错的后果是整条 oss-ingest 在服务端失败，而普通单元测试
不会有任何反应。

### oss-ingest 为什么两次校验注册表

`preflight` 查一次，`ingest-commit` 里 `commit --registry` 再查一次。
两者之间隔着一次 scan（可能跑很久），注册表有可能在中间被改。

`cpfs-ingest` 的 `commit` **刻意不传 `--registry`**：它的来源是上一步刚写进去的
staging 前缀（如 `staging/batch-...`），而注册表登记的是归档前缀（如 `releases/`）。
传了会因为「staging 没注册」误报失败。那个位置的内容稳定性由「刚归档完、
只有本流水线写过」保证，不需要注册表判断。

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
