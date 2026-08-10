# 运维手册

存储分区、两种注册、DataFlow 语义、PAI 挂载合同和回收硬规则统一见
[OSS / CPFS 数据管理与生命周期](storage-lifecycle.md)。本手册只写具体操作与排错。

面向维护这套系统的人：怎么初始化、怎么日常运维、出事怎么办。

管理员阅读入口见[管理员手册](admin-guide.md)。相关：[架构](architecture.md)｜
[权限](permissions.md)｜[CI/CD](cicd.md)｜[用户手册](user-guide.md)｜[使用入门](onboarding.md)

---

## 0. 当前状态与前置阻塞

### 先跑体检

换账号时**第一件事**是跑只读体检，而不是跑流水线：

```bash
ALIYUN_PROFILE=<profile> REGION=cn-hangzhou make preflight
```

它把下表以及散落在 [AGENTS.md 已知踩坑](../AGENTS.md) 里的前提变成自动检查，
逐条告诉你差什么、为什么要它。有 FAIL 就退出码 1。检查项：

| # | 检查 | 为什么 |
|---|---|---|
| 1 | 是不是 root | root 绕过一切 RAM 限制，这套设计对它不生效 |
| 2 | CPFS 服务开通 / 文件系统存在 | 没有它 `materialize` 无处可写 |
| 2 | `bmcpfs-` 前缀 → 智算版 | 智算版**不支持 Evict**，`reclaim --strategy cpfs-evict` 用不了 |
| 3 | Fileset 存在 | 数据流动第一条前提：`FsetId` 必填 |
| 4 | 注册表里的桶真实存在 | 早失败，且报错清楚 |
| 4 | `archive` 桶有 `cpfs-dataflow` 标签 | 没有它 `CreateDataFlow` 直接拒绝，且官方文档不显眼 |
| 4 | `archive` 桶开了版本控制 | Export（沉淀）要求；Import 不要求 |
| 5 | PAI Workspace 存在 | `register-pai` 需要 WorkspaceId，且 Workspace 分 region |
| 6 | 谁持有 `AliyunRAMFullAccess` | **他能删掉我们所有 Deny**，见 [权限 §5](permissions.md) |
| 7 | GitHub OIDC Provider | 没有它 CI 拿不到临时凭证 |

体检**只调用只读 API**，不创建、不修改任何资源，可以放心在生产账号跑。

它测不到的是三件只能在提交时暴露的事：`Throughput` 只接受 600/1200/1500、
相关资源未就绪时报的 `OperationDenied.InvalidState`（这个错会盖住真正的原因）、
以及同一 DataFlow 的任务必须串行。

### 当前 dev 环境与前置阻塞

2026-08-04 已用 `make preflight` 重新做只读确认：CPFS
`cpfs-00a27a8ec8b1e13a` 为 Running、已有一个 Fileset，PAI Workspace `617398`
与 GitHub OIDC Provider 均存在。当前没有控制面缺失项。

接入真实环境前，先确认下面几件事，否则会在中途卡住：

| 阻塞项 | 现状 | 解除方式 |
|---|---|---|
| CPFS 挂载点库存 | cn-hangzhou-i 文件系统可用，但此前 `CreateMountTarget` 返回 `Resource.OutOfStock` | 在同区先做挂载点库存探测；未恢复前不能验收 runner/DSW/DLC 数据面 |
| 存量非空目录 | 已有数据的路径不能原地补 Fileset | 按 [Fileset 迁移手册](cpfs-fileset-migration.md) 迁入预建空 Fileset |
| 执行身份 | 当前只读体检仍是主账号 root | 日常 CI 与训练改用 OIDC/RAM Role；root 只做账号级应急操作 |
| PAI Dataset | 目标 Workspace 里 0 个 | 先建一个，`register-pai` 需要 DatasetId。注意 PAI 会自动带一个 `v1`，所以首次注册拿到的是 `v2` |
| CI runner | GitHub 托管 runner 到不了 VPC 内 CPFS | 准备自托管 runner 或改用 ACK Job |

---

## 1. 初始化（一次性）

### 1.1 建 Terraform 专用身份

**不要用主账号 root 跑 Terraform。** root 无法被 RAM 策略约束，出事也无法定位到人。

```bash
aliyun ram CreateUser --UserName terraform-bootstrap
aliyun ram AttachPolicyToUser --UserName terraform-bootstrap \
  --PolicyType System --PolicyName AdministratorAccess   # 仅 bootstrap 期间
aliyun ram CreateAccessKey --UserName terraform-bootstrap
```

bootstrap 跑完后**立即**把这个用户降权或删除——它只需要在创建 State 后端和
OIDC 信任锚时存在一次。之后所有变更都走 OIDC 临时凭证。

### 1.2 获取 GitHub OIDC 指纹

`infra/bootstrap` 的 `oidc_thumbprints` 需要 GitHub OIDC 服务证书的 SHA-1 指纹：

```bash
host=token.actions.githubusercontent.com
openssl s_client -servername "$host" -showcerts -connect "$host":443 </dev/null 2>/dev/null \
  | openssl x509 -fingerprint -sha1 -noout \
  | sed 's/.*=//; s/://g' \
  | tr 'A-Z' 'a-z'
```

GitHub 轮换证书时流水线会突然全部失败。建议**同时保留新旧两个指纹**，
并在证书到期前更新。

### 1.3 跑 bootstrap

```bash
cd infra/bootstrap
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars

terraform init
terraform plan     # 逐条看清楚要创建什么
terraform apply
```

产出：State 桶、Tablestore 锁表、OIDC 身份提供商、三个 CI 角色。

**bootstrap 用的是本地 state。** 跑完把 `terraform.tfstate` 归档到安全位置
（内部密钥库或加密存储），不要提交进 git，也不要留在个人电脑上。

### 1.4 配置 GitHub

把 bootstrap 的 output 填进仓库配置：

```bash
terraform -chdir=infra/bootstrap output
```

仓库级 Variables（Settings → Secrets and variables → Actions → Variables）：

| 变量 | 来源 |
|---|---|
| `ALIBABA_CLOUD_OIDC_PROVIDER_ARN` | `oidc_provider_arn` |
| `ALIBABA_CLOUD_PLAN_ROLE_ARN` | `plan_role_arn` |
| `ALIBABA_CLOUD_REGION` | 你的 region |
| `TF_STATE_BUCKET` / `TF_STATE_LOCK_ENDPOINT` / `TF_STATE_LOCK_TABLE` | `backend_config` |

Environment 级 Variables（Settings → Environments）：

| Environment | 变量 | 说明 |
|---|---|---|
| `development` | `ALIBABA_CLOUD_PLATFORM_APPLY_ROLE_ARN`、`ALIBABA_CLOUD_ACCESS_APPLY_ROLE_ARN` | 可不设审批人 |
| `production` | 同上 | **必须**设 required reviewers |
| `production-access` | 同上 | 审批人应包含安全团队 |
| `dataset-release` | — | 数据集发布的审批点 |

Environment 名不是随便起的：它会成为 OIDC token 的 `sub`
（`repo:<owner>@<owner-id>/<repo>@<repo-id>:environment:<name>`），而 apply 角色的信任策略只接受这个
`sub`。改 Environment 名等于改信任边界，必须同步改 `infra/bootstrap`。

bootstrap 用 `platform_github_environments` 和 `access_github_environments` 分别声明
两个 Apply Role 可以接受的 Environment。默认值与 workflow 矩阵一致：Platform 为
`development`/`production`，Access 为 `development`/`production-access`。仓库迁移时
还必须更新 `github_repo` 与 `github_oidc_repo`；后者绑定 GitHub 的不可变 Owner ID 和
Repository ID，仓库改名不会改变信任主体。只改 Git remote 不会更新云端 OIDC 信任策略。

同时开启分支保护：`main` 禁止直接推送，PR 需评审，`.github/CODEOWNERS` 生效。

### 1.5 跑 envs

```bash
terraform -chdir=infra/bootstrap output -raw backend_config > backend.hcl

cd infra/envs/dev/platform
cp terraform.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars
terraform init -backend-config=../../../../backend.hcl
terraform plan
```

先 dev 后 prod，先 platform 后 access（access 需要 platform 的桶名）。
确认 dev 全链路通了再动 prod。

---

## 2. 日常运维

### 2.1 改基础设施

```
改 infra/envs/*/platform/ → PR → 自动 plan 并评论到 PR → 评审 → 合并 main
→ Actions 手动运行 Terraform（main，confirm_apply=true）→ 重新生成 plan
→ Environment 审批 → apply（执行该手动运行生成的同一份 tfplan）
```

### 2.2 改权限（更严格）

```
改 infra/envs/*/access/terraform.tfvars → PR
→ CODEOWNERS 要求安全团队评审
→ plan 输出必须显式列出：新增了谁、移除了谁、授予/收回了哪些 Action
→ 合并后由管理员在 main 手动运行 Terraform，并勾选 confirm_apply
→ production-access Environment 审批
→ apply
```

人员**进出项目必须同时改两处**：`pai_members`（PAI 侧）和 RAM 用户组成员（RAM 侧）。
只改一处会留下「在成员列表里但调不通 API」或反过来的半吊子状态。

### 2.3 改 RAM 策略

```bash
$EDITOR infra/modules/dataset-sink-roles/policies.tf
make render-ram        # 同步 deploy/ram/*.json
git add -A && git commit
```

漏掉 `make render-ram` 的话 CI 的 `ram-policies-in-sync` 会失败并告诉你怎么做。

### 2.4 发布数据集

先区分两件事：`data_sources` 登记 OSS 前缀是否允许接入；PAI Dataset Version 则在
CPFS release 校验完成后创建。裸 OSS 不需要先注册成 PAI Dataset。完整边界见
[存储生命周期 §2](storage-lifecycle.md#2-两种注册不要混淆)。

Actions → Dataset release → Run workflow，填参数。流水线会在 dry-run 后停下等审批，
此时在日志里能看到将要发出的**确切请求体**。确认无误再批。

选哪个 `mode`：

| 情况 | mode | 额外要填 |
|---|---|---|
| CPFS 上刚处理完一批新数据，还没有 Commit | `cpfs-ingest` | `prepared_dir`、`archive_prefix`；`ref` 填**要创建的** Tag 名 |
| 已有 CPFS 或已有 PAI Version 背后的目录，原路径不能移动 | `cpfs-adopt` | `prepared_dir` 填现有挂载路径、`archive_prefix` 填新归档前缀 |
| **数据本来就在 OSS 上**（存量数据，最常见） | `oss-ingest` | `source_bucket`、`source_prefix`；`ref` 填**要创建的** Tag 名 |
| CPFS staging 已就绪，Commit 已存在 | `certify` | `prepared_dir`、`manifest_path` |
| 数据在 lakeFS，要拷到 CPFS | `materialize` | `manifest_path` |

已有 PAI Dataset 只需要把短名称映射到它的 ID，不要让用户从表单提交 ID：

```json
{"robotics":"d-existing-robotics","vision":"d-existing-vision"}
```

把这段配置成 Repository Variable `PAI_DATASET_IDS_JSON`。流水线按 `dataset` 名称选择
目标容器；未知名称会在 preflight 失败。单数据集环境仍可暂时使用旧变量
`PAI_DATASET_ID`。既有 PAI Version 如果没有 lakeFS Commit 和 `_READY`，不能直接视为
已纳管，仍要根据底层位置走 `oss-ingest` 或 `cpfs-adopt`。

### 2.4.1 已有 CPFS / PAI 数据集

选择 `cpfs-adopt`。它会扫描现有目录、归档 OSS、创建 lakeFS Commit，再物化到新的
`/datasets/<dataset>/<commit>/`。它不会像 `cpfs-ingest` 的 `certify` 那样 rename
原目录，因此旧作业和旧 PAI Version 不会突然失去路径。纳管成功并完成使用方切换后，
再单独决定旧目录何时下线。

### 2.4.2 存量数据已经在 OSS 上

这是最省事的一条：**不需要迁移，不需要归档**。

**优先走流水线的 `oss-ingest` 模式**——它把下面三步串好了，而且每步换身份、
有审批、有留痕。手敲命令行只适合摸底和排查，因为它绕过了这些。

下面是这条路径实际在做的事（流水线内部就是这三步）：

```bash
# ① 列举 + 算 SHA-256（会完整读一遍数据，TB 级请预留时间）
dataset-sink scan-oss \
  --bucket legacy-data --endpoint-url https://oss-cn-hangzhou.aliyuncs.com \
  --prefix legacy/robotics --destination datasets/robotics \
  --output /work/manifest.jsonl

# ② 零拷贝 import 建 Commit（秒级，不搬字节）
#    注意是 s3:// 而不是 oss://——scheme 要匹配 lakeFS 的 blockstore adapter
#    类型，OSS 是走 lakeFS 的 s3 adapter 的。写 oss:// 会在服务端报
#    `invalid storage scheme oss`，commit 现在会在本地先拦下。
dataset-sink commit --repository robotics-data --branch main \
  --object-store-uri s3://legacy-data \
  --prefix legacy/robotics --destination datasets/robotics \
  --manifest /work/manifest.jsonl --tag robotics-v2026.08.02.1

# ③ 从 lakeFS 按 Commit 内路径取数，落到 CPFS 发布
#    注意**不要**传 --commit-prefix：scan-oss 的 source_key 已经是 Commit 内路径了
dataset-sink materialize --dataset robotics --repository robotics-data \
  --commit <上一步产生的 commit_id> --lakefs-tag robotics-v2026.08.02.1 \
  --manifest /work/manifest.jsonl --source lakefs-s3 --target-root /mnt/cpfs/datasets
```

四件必须注意的事：

0. **`--object-store-uri` 用 `s3://`，不是 `oss://`。** scheme 要匹配 lakeFS 的
   blockstore adapter 类型而不是云厂商名字。2026-08-03 在真实 lakeFS 1.84.1 +
   OSS 后端上实测确认。
1. **`--destination` 两处必须一致。** manifest 的 `source_key` 是 Commit 内路径，
   填错会让后面的 `materialize` 全量 404。`commit` 会在建 Commit 之前拦下。
2. **扫描期间前缀必须冻结写入。** `scan-oss` 会核对列举时的 size 与读取时的实际
   字节数，对不上直接失败——但那已经是事后发现。正确做法是先停掉写入方。
3. **import 之后原前缀就是只读区。** 删除或覆盖其中的对象会让 Commit 悬空，
   且当时不会报错。把这些前缀登记进 `infra/envs/*/access/terraform.tfvars` 的
   `imported_data_prefixes`，Terraform 会对本模块管理的身份统一 Deny 写删。

   ```hcl
   imported_data_prefixes = [
     { bucket = "legacy-data", prefix = "legacy/robotics" },
   ]
   ```

   注意这只约束本项目管理的身份。持有 `AliyunOSSFullAccess` 的既有 RAM 用户仍然
   能删——兜底要靠桶级 Policy + 版本控制 + 合规保留策略。

`--no-digest` 可以跳过哈希计算，但那样发布出来的 release **永久**失去内容校验能力
（`verify --deep` 和 `training-guard --deep` 退化成只比大小），因为 manifest 随发布
固化、事后补不上。只用它来先摸清前缀里有什么。

`cpfs-ingest` 和 `cpfs-adopt` 模式额外需要这几个仓库变量：

| 变量 | 说明 |
|---|---|
| `ARCHIVE_BUCKET` | 归档桶名，必须以 `dataset-sink-` 开头 |
| `ARCHIVE_ENDPOINT_URL` | OSS 的 S3 兼容端点 |
| `ARCHIVE_OBJECT_STORE_URI` | 桶级 URI，如 `s3://dataset-sink-archive`，供 lakeFS import 使用 |
| `LAKEFS_API_ENDPOINT` | lakeFS API 地址 |

`cpfs-adopt` 还需要 `LAKEFS_S3_ENDPOINT`，因为它归档建 Commit 后会重新物化，而不是
移动原目录。

`oss-ingest` 模式**不需要** `ARCHIVE_*`（它不归档），需要的是：

| 变量 | 说明 |
|---|---|
| `OSS_ENDPOINT_URL` | 区域级 OSS S3 兼容端点。留空回落到 `ARCHIVE_ENDPOINT_URL`——同 region 的桶本来就是同一个地址 |
| `LAKEFS_API_ENDPOINT` | lakeFS API 地址，import 用 |
| `LAKEFS_S3_ENDPOINT` | lakeFS S3 Gateway，之后 `materialize` 取数用 |

触发时填 `source_bucket` + `source_prefix`，**不要**填 `prepared_dir` / `archive_prefix`
（填了会被 preflight 拒绝——那说明模式选错了）。这个前缀必须已经在数据源注册表里，
且 mode 不是 `workspace`。

**staging 目录必须是干净的**：只包含数据集内容。有 `.DS_Store`、`_READY`、
`release.json` 之类的残留，`scan` 会在第一步就失败并给出清理命令——这是有意的，
否则要到归档完一整轮之后才在 `certify` 撞上报错。

### 2.4.3 用 CPFS 数据流动搬字节

CLI 单独运行时，`archive` 和 `materialize` 默认 `--via client`，方便离线演练；
`dataset-release.yml` 的生产默认则是 `transfer_mode=dataflow`，由 CPFS 服务端搬运。
只有尚未完成 DataFlow 绑定的迁移环境才显式选择 `client`：

```bash
# 沉淀：CPFS staging → OSS
dataset-sink archive /mnt/cpfs/staging/batch-001 --manifest /work/manifest.jsonl \
  --via dataflow --cpfs-filesystem-id cpfs-xxxxxxxxxxxxxxxx \
  --cpfs-mount-prefix /mnt/cpfs --region cn-hangzhou

# 预热：OSS → CPFS，然后照常全量校验 + 原子发布
dataset-sink materialize --dataset robotics --repository robotics-data \
  --commit 6f2b7c91c2 --manifest /work/manifest.jsonl --target-root /mnt/cpfs/datasets \
  --via dataflow --cpfs-filesystem-id cpfs-xxxxxxxxxxxxxxxx \
  --cpfs-mount-prefix /mnt/cpfs --region cn-hangzhou
```

**`--prefix` 在 `--via dataflow` 下无效。** 数据流动把 `FileSystemPath` 和
`SourceStoragePath` 死绑在一起，目标前缀只能由绑定推导。命令会把真实落点作为
`object_store_uri` 回报出来——直接拿它喂给 `commit --object-store-uri`。

这条路要求环境先满足六条前提，见[架构](architecture.md)。其中三条是 Terraform
该管的：数据集根目录是 Fileset、归档桶打 `cpfs-dataflow` 标签、归档桶开版本控制。
流水线不会在找不到覆盖路径时自动回退到客户端，否则一次配置错误会悄悄变成 runner
上的 TB 级复制；必须先补齐 Fileset/DataFlow，或由运维人员明确选择兼容模式。

**沉淀不释放 CPFS 空间**——它只是在 OSS 多存一份。要腾容量得再跑
`reclaim --strategy cpfs-evict`。

### 2.5 回收 CPFS 容量

CPFS release 只增不减，写满之后 `materialize` 会直接失败。**这是必须定期做的事**，
不是可选优化。

正常入口是 Actions → **Dataset lifecycle**：手动运行时默认只上传 dry-run Artifact；
需要腾空间时显式打开 `execute`，随后在 `dataset-lifecycle` Environment 审批。
配置以下 Repository Variables：

| 变量 | 说明 |
|---|---|
| `DATASET_LIFECYCLE_ROLE_ARN` | Terraform 输出的生命周期 Evict 角色 |
| `PAI_MOUNT_AUDIT_ROLE_ARN` | 定时计划使用的只读 PAI 审计角色 |
| `CPFS_TARGET_ROOT`、`CPFS_MOUNT_PREFIX` | runner 挂载视角的数据集根和 CPFS 根 |
| `CPFS_FILESYSTEM_ID`、`PAI_WORKSPACE_ID` | 占用和 DataFlow 检查坐标 |

执行 Job 会重新计算计划，不会直接执行审批前的 Artifact。下面的 CLI 主要用于排错：

```bash
# ① 先看计划（默认就是 dry-run，什么都不删）
dataset-sink reclaim /mnt/cpfs/datasets \
  --lakefs-api-endpoint "$LAKEFS_API_ENDPOINT" \
  --pai-usage-workspace-id 617398 --pai-usage-region cn-hangzhou

# ② 逐条看 reclaim[] 里的 release 和 retain[] 里的理由，确认无误
# ③ 真删
dataset-sink reclaim /mnt/cpfs/datasets \
  --lakefs-api-endpoint "$LAKEFS_API_ENDPOINT" \
  --pai-usage-workspace-id 617398 --pai-usage-region cn-hangzhou \
  --sweep-trash --execute
```

PAI 占用探针只读取活动 DLC/DSW 的挂载配置，把 Dataset Version 的 `SourceId`、
`lakefs_commit` Label 和 URI 末级目录映射回 Commit。活动作业命中的 release 一律保留；
查询失败时 fail-closed，全部保留；终态作业不会永久占住历史版本。它不替代 lakeFS
可重建性检查、保护期与 `keep-last`，而是额外的交集条件。不传 Workspace 参数时保持
原行为，仅依赖其他门禁。

只想腾出指定容量（从最旧的开始删，够了就停）：

```bash
dataset-sink reclaim /mnt/cpfs/datasets --reclaim-bytes $((2 * 1024**4)) ...
```

**默认要连 lakeFS**，因为回收的安全前提是「删了能重建」，而这靠核对 Commit 是否
还在。连不上或查不到就一律保留。`--assume-recoverable` 能跳过这个检查，但它是整个
流程里**唯一能造成不可逆数据丢失**的开关，只有在你另有依据确认归档还在时才用。

保护某个 release 不被回收：

```bash
touch /mnt/cpfs/datasets/robotics/<commit>/.keep
```

几个默认值和它们的理由：

| 参数 | 默认 | 为什么 |
|---|---|---|
| `--min-age-days` | 14 | **没接占用探测时，这是唯一挡在回收和运行中训练之间的东西**，别调小 |
| `--keep-last` | 2 | 保证任何数据集都不会被清空 |
| `--include-incomplete` | 关 | 缺 `_READY` 的可能是正在排查的发布残骸，不自动删 |

如果 `--execute` 中途被杀，`.trash/` 里会留下残骸——它们已经不在数据集命名空间里，
不影响正确性，下次带 `--sweep-trash` 跑一遍即可。

#### 用 CPFS Evict 代替硬删

如果 CPFS 上这些 release 已经由数据流动（DataFlow）管理，优先用 `cpfs-evict`：

```bash
dataset-sink reclaim /mnt/cpfs/datasets \
  --strategy cpfs-evict \
  --cpfs-filesystem-id cpfs-xxxxxxxxxxxxxxxx \
  --cpfs-mount-prefix /mnt/cpfs \
  --region cn-hangzhou \
  --lakefs-api-endpoint "$LAKEFS_API_ENDPOINT" --execute
```

它只释放数据块、保留元数据，所以 **PAI Dataset Version 不会失效**，下次训练访问
时 CPFS 自动从 OSS 加载，不用重跑 `materialize`。代价是第一次访问会慢。

`--cpfs-mount-prefix` 用来把挂载视角路径换算成文件系统内部路径，**填错会作用到
错误的目录上**。找不到覆盖该路径的 DataFlow 时命令会失败，不会退化成硬删。

灵骏 BMCPFS 不支持 Evict，只能用 `hard-delete`。

### 2.5.1 回收后要不要同步 PAI

`hard-delete` 只删 CPFS 上的目录，**不动 PAI Dataset Version**，所以会出现
「版本记录还在、挂载会失败」的状态。（用 `cpfs-evict` 就没这个问题，元数据还在，
挂载仍然有效。）两种处理方式：

- **留着**：版本记录本身是有价值的审计痕迹，且重新 `materialize` 回来之后就恢复可用。
- **删掉**：需要先确认没有作业引用它。注意 `deny_destructive` 策略 Deny 了
  `paidataset:DeleteDatasetVersion`，普通角色删不掉——这是有意的。

现在推荐留着。真正的判据是「Commit 还在不在」，而不是「CPFS 上有没有」。

### 2.6 定期检查

| 频率 | 检查项 | 命令 |
|---|---|---|
| 每次权限变更 | plan 里有没有意料之外的 Action | PR 里看 |
| 每周 | CPFS 剩余容量与可回收量 | `dataset-sink reclaim <root> ...`（dry-run） |
| 每月 | 有没有人在控制台手工加了权限 | `terraform plan` 应无 drift |
| 每月 | 谁是 Workspace 管理员 | `terraform output pai_admin_members` |
| 每季度 | 有没有长期 AccessKey 存活 | `aliyun ram ListAccessKeys --UserName <each>` |
| 每季度 | GitHub OIDC 证书是否临近轮换 | 见 1.2 |
| 证书轮换前 | 更新 `oidc_thumbprints` | 改 bootstrap 后 apply |

---

## 3. 排错

### 3.1 权限被拒，先定位是哪一层

按顺序排查（详见[权限](permissions.md)）：

1. 身份对吗？`aliyun sts GetCallerIdentity`
2. RAM 允许这个 API 吗？看有没有 Deny 命中
3. 在 PAI Workspace 成员里吗？`ListMembers`
4. PAI 角色允许这个操作吗？
5. CPFS Fileset / POSIX 权限允许吗？
6. OSS 前缀授权了吗？
7. ActionTrail 里被拒的是哪个 Action、哪个身份？

**记住显式 Deny 优先于任何 Allow。** 加了 Allow 还是不通，就去找是哪条 Deny 命中了。

### 3.2 OIDC 假设角色失败

| 报错 | 原因 |
|---|---|
| `The OIDC Provider you want to use is not exist` | Provider ARN 填错，或 bootstrap 没跑 |
| `sub mismatch` / 条件不满足 | Job 没声明 `environment:`，或 Environment 名与信任策略不符 |
| `aud mismatch` | workflow 的 `audience` 与 `client_ids` 不一致 |
| `iat` 相关错误 | 时钟或 `issuance_limit_time` 问题 |
| 签名验证失败 | GitHub 轮换了证书，指纹过期，见 1.2 |

### 3.3 Terraform state 锁没释放

Job 被强制取消会留下脏锁。**先确认真的没有 apply 在跑**，再解锁：

```bash
terraform -chdir=infra/envs/prod/platform force-unlock <LOCK_ID>
```

这就是 `terraform.yml` 里 `cancel-in-progress: false` 的原因——取消一个正在
apply 的 Job 既留脏锁，又可能让 state 和实际资源不一致。

### 3.4 plan 里出现 destroy / replace

生产环境这是**红灯**：

1. 先搞清楚为什么。多数情况是某个 ForceNew 属性被改了。
2. 如果不该发生，回退那次改动。
3. 如果确实必须删除重建，走 BreakGlass：`deny_destructive` 策略会让普通
   apply 角色执行失败，这是设计如此，不要为了让流水线过而关掉它。

### 3.5 发布出来的版本挂不上 / 门禁不过

| 现象 | 排查 |
|---|---|
| `_READY` 缺失 | 沉降没完成，检查 materialize 那一步的日志 |
| `manifest checksum mismatch` | 数据在发布后被改动过——严重问题，先冻结该版本 |
| PAI 挂载路径找不到 | `--filesystem-path` 和挂载路径混用了，见[架构第 5 节](architecture.md) |
| 注册报 `ReleaseConflictError` | 同一 Commit 已注册且 manifest 不同，说明有人改了数据后重发 |

### 3.6 沉降卡住或很慢

- `materialize` 是从 lakeFS 拷贝，受网络带宽限制，TB 级数据本来就慢。
- 如果数据已经在 CPFS Staging，改用 `certify`：同文件系统内 rename，
  秒级完成，不产生数据拷贝。前提是 Staging 目录已按 Manifest 的
  `target_path` 布局组织好。
- `--workers` 默认 8，CPFS 场景可以调到 32。

---

## 4. 应急

### 4.1 疑似凭证泄露

1. 撤销：删掉相关 RAM 用户的 AccessKey；OIDC 临时凭证最长 1 小时自动失效。
2. 收紧信任：临时把对应角色的 `subjects` 改成空或不存在的 sub，切断假设路径。
3. 查影响：ActionTrail 按身份筛调用记录。
4. 查数据：`dataset-sink verify <release> --deep` 确认已发布数据没被改动。
5. 复盘：泄露的是什么身份？它能做什么？边界是否足够窄？

### 4.2 误删了资源

- State 桶开了版本控制，可取回上一版 state。
- OSS 数据桶也开了版本控制。
- CPFS 上的 release 目录**没有版本控制**——这是最需要小心的地方，
  也是 `deny_destructive` 存在的理由。

### 4.3 需要绕过护栏（BreakGlass）

不要给日常角色加权限来「让流水线过去」。正确做法是建一个单独的
BreakGlass 角色，满足：

- 信任主体是具体的人，不是 CI；
- 有效期短；
- 使用需要双人审批并留记录；
- 用完即撤。

---

## 5. 本地验证

不需要任何云凭证：

```bash
make test          # 单元测试
make e2e           # 全链路演练（临时目录模拟 CPFS）
make lint          # ruff
make tf-fmt        # Terraform 格式
make tf-validate    # 逐目录 init -backend=false + validate
make render-ram    # 重新渲染 RAM 策略副本
```

### 离线校验 Terraform

alicloud provider 约 60MB，网络慢时 `terraform init` 会很久。下过一次后可以做
本地镜像，之后 init 是秒级：

```bash
# provider 已下载过的情况下，复制到镜像目录
mkdir -p ~/.terraform.d/plugin-cache/registry.terraform.io/aliyun/alicloud
cp -R <某个已 init 目录>/.terraform/providers/registry.terraform.io/aliyun/alicloud/<版本> \
      ~/.terraform.d/plugin-cache/registry.terraform.io/aliyun/alicloud/

cat > /tmp/tf-mirror.tfrc <<'EOF'
provider_installation {
  filesystem_mirror {
    path    = "/Users/<你>/.terraform.d/plugin-cache"
    include = ["registry.terraform.io/aliyun/alicloud"]
  }
  direct {
    exclude = ["registry.terraform.io/aliyun/alicloud"]
  }
}
EOF
export TF_CLI_CONFIG_FILE=/tmp/tf-mirror.tfrc
```

只设 `TF_PLUGIN_CACHE_DIR` 不够：terraform 仍会去 GitHub 取 `SHA256SUMS` 做校验，
网络不通时照样卡住。必须用 `filesystem_mirror` 才能完全离线。

---

## 6. 一些容易忘的事实

- `aliyun` CLI profile 的默认 region 未必是资源所在 region，命令一律显式 `--region`。
- `aiworkspace` 产品必须显式 `--endpoint aiworkspace.<region>.aliyuncs.com`。
- zsh 不对未加引号的变量做单词切分，别把多个 flag 塞一个变量里再展开。
- `terraform` 不在 homebrew-core（BUSL 许可），用 `brew install hashicorp/tap/terraform`。
- `-chdir` 会让 `-var-file` 相对新工作目录解析。
- `paidataset:*` / `paidlc:*` / `paiworkspace:*` 在官方定义里都是 `Resource: "*"`，
  RAM 层做不了资源级收敛。
