# 运维手册

面向维护这套系统的人：怎么初始化、怎么日常运维、出事怎么办。

相关：[架构](architecture.md)｜[权限](permissions.md)｜[CI/CD](cicd.md)｜[使用入门](onboarding.md)

---

## 0. 当前状态与前置阻塞

接入真实环境前，先确认下面几件事，否则会在中途卡住：

| 阻塞项 | 现状 | 解除方式 |
|---|---|---|
| CPFS 服务 | 本机可登录的账号未开通 NAS（`User.Disabled`） | 在**目标账号**开通 NAS/CPFS，或确认用的是灵骏 BMCPFS |
| CPFS 类型 | 未确认是标准 CPFS 还是灵骏 BMCPFS | 标准 CPFS 走 `nas` API；BMCPFS 走 `eflo`，Terraform 数据源不同 |
| 目标账号 | 本机 profile 登录的不是目标账号 | 在目标账号跑 `make discover` |
| 执行身份 | 探测到的是主账号 root | 先建 Terraform 专用 RAM 用户，见下一节 |
| PAI Dataset | 目标 Workspace 里可能还没有 Dataset | 先建一个，`register-pai` 需要 DatasetId |
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
（`repo:<org>/<repo>:environment:<name>`），而 apply 角色的信任策略只接受这个
`sub`。改 Environment 名等于改信任边界，必须同步改 `infra/bootstrap`。

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
→ Environment 审批 → apply（执行 plan 阶段那一份 tfplan）
```

### 2.2 改权限（更严格）

```
改 infra/envs/*/access/terraform.tfvars → PR
→ CODEOWNERS 要求安全团队评审
→ plan 输出必须显式列出：新增了谁、移除了谁、授予/收回了哪些 Action
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

Actions → Dataset release → Run workflow，填参数。流水线会在 dry-run 后停下等审批，
此时在日志里能看到将要发出的**确切请求体**。确认无误再批。

选哪个 `mode`：

| 情况 | mode | 额外要填 |
|---|---|---|
| CPFS 上刚处理完一批新数据，还没有 Commit | `cpfs-ingest` | `prepared_dir`、`archive_prefix`；`ref` 填**要创建的** Tag 名 |
| CPFS staging 已就绪，Commit 已存在 | `certify` | `prepared_dir`、`manifest_path` |
| 数据在 lakeFS，要拷到 CPFS | `materialize` | `manifest_path` |

`cpfs-ingest` 模式额外需要这几个仓库变量：

| 变量 | 说明 |
|---|---|
| `ARCHIVE_BUCKET` | 归档桶名，必须以 `dataset-sink-` 开头 |
| `ARCHIVE_ENDPOINT_URL` | OSS 的 S3 兼容端点 |
| `ARCHIVE_OBJECT_STORE_URI` | 桶级 URI，如 `s3://dataset-sink-archive`，供 lakeFS import 使用 |
| `LAKEFS_API_ENDPOINT` | lakeFS API 地址 |

**staging 目录必须是干净的**：只包含数据集内容。有 `.DS_Store`、`_READY`、
`release.json` 之类的残留，`scan` 会在第一步就失败并给出清理命令——这是有意的，
否则要到归档完一整轮之后才在 `certify` 撞上报错。

### 2.5 定期检查

| 频率 | 检查项 | 命令 |
|---|---|---|
| 每次权限变更 | plan 里有没有意料之外的 Action | PR 里看 |
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
