# 仓库约定

本文是人和编码代理（Claude Code / codex）共用的约定。`CLAUDE.md` 直接引用本文，不要两处分别维护。

## 这个仓库是什么

把 lakeFS 的不可变 Commit 沉降成 CPFS 上可被阿里云 PAI 只读挂载的数据集版本。完整设计见 [docs/architecture.md](docs/architecture.md)。

## 硬规则

违反以下任一条的改动一律不接受：

1. **不写入任何凭证。** 不在代码、注释、示例、提交信息里出现 AccessKey / SecretKey / lakeFS 凭证。云侧一律走 RAM Role + STS/OIDC 临时身份，`aliyun` CLI 走默认凭证链或 `--profile`。
2. **不在本地跑 `terraform apply` / `destroy` / `import` / `state`。** 这些只经由流水线在审批后执行。`.claude/settings.json` 已 deny，别绕过。
3. **`register-pai` 默认 dry-run。** 任何默认执行真实写操作的改动都是错的；`--execute` 必须是显式的。
4. **已发布的 release 目录不可改写。** 同 Commit 重复沉降是幂等 no-op；同 Commit 不同 Manifest 必须报错，不许覆盖。
5. **不引入运行时第三方依赖。** 核心逻辑保持零依赖（`pyproject.toml` 的 `dependencies = []`）；lakeFS/boto3 只在 optional extras 里，并在导入处做降级处理。
6. **不挂载可变引用。** 代码和模板里不得出现 `latest`、Branch 名或日期作为数据版本标识；只用 Commit ID。
7. **已发布的 release 路径上禁止配 CPFS 数据流动的 AutoRefresh。**
   `AutoRefreshPolicy = ImportChanged` 会让 CPFS 目录跟随 OSS 变化，直接破坏
   release 的不可变性。预热（`Import`）和沉淀（`Export`）是一次性任务，可以用；
   自动刷新不行。

## 常用命令

```bash
make test          # 单元测试（离线，无凭证）
make lint          # ruff check
make fmt           # ruff format + terraform fmt
make e2e           # 本地全链路演练（临时目录模拟 CPFS）
make tf-validate   # 逐目录 terraform init -backend=false + validate
make hooks         # pre-commit run --all-files
make preflight     # 只读体检：换账号前先跑这个，看还差什么（需凭证）
```

`make test` 和 `make e2e` 必须始终能在没有网络、没有云凭证的机器上通过。任何需要真实环境的测试放 `tests/integration/`，并在缺少环境变量时 skip。

## 目录约定

| 路径 | 内容 | 注意 |
|---|---|---|
| `src/dataset_sink/` | 全部 Python 逻辑 | src 布局，不要在仓库根放模块 |
| `tests/unit/` | 离线单元测试 | 新增测试默认放这里 |
| `tests/integration/` | 需要真实 lakeFS/CPFS/PAI | 缺环境变量必须 skip 而非失败 |
| `infra/bootstrap/` | 本地 state，自举 State 后端与 OIDC | 只有管理员执行 |
| `infra/modules/` | 可复用 Terraform 模块 | 变量要有 `description` 和 `type` |
| `infra/envs/<env>/{platform,access}/` | 环境层，双 state | `access` 变更需更严格审批 |
| `deploy/ram/` | Terraform 渲染出的 RAM 策略副本 | **不要手改**，改模块后跑 `scripts/render-ram-policies.sh` |
| `deploy/pai/` | DLC/DSW 模板与训练入口 | 模板里的路径是容器内路径 |
| `docs/` | 架构与运维文档 | README 只做导航，细节写这里 |
| `scripts/` | 本地演练与只读探测 | 只读脚本才可进 settings 的 allow |
| `skills/` | 面向用户 Agent 的可安装 Skill | 不放凭证；Workflow/权限/挂载契约变化时同步更新 |

## 代码风格

- Python 目标版本 `py39`，行宽 100，`ruff` 管 lint 和 format，配置在 `pyproject.toml`。
- 新代码沿用现有风格：`from __future__ import annotations`、`dataclass(frozen=True)` 表示结果对象、错误统一抛 `DatasetSinkError` 子类。
- Terraform 用 `snake_case`，两空格缩进，变量必须有 `description` 和 `type`。
- RAM 策略用 `jsonencode` 的 `locals`，不用 `data "alicloud_ram_policy_document"`：前者的渲染结果直接就是 `deploy/ram/*.json` 需要的内容，且不依赖 Provider data source schema。
- Shell 脚本用 `#!/bin/sh` + `set -eu`，通过 `shellcheck`。

## 已知踩坑

- **CPFS 开通本来就要很久（一小时以上是正常的）。** 长时间 `Pending` 不是卡住，
  不要据此判定失败去删——而且 `Pending` 状态下 `DeleteFileSystem` 会报
  `OperationDenied.InvalidState`，删也删不掉。要等就老实等，别自作聪明加自动清理。
- **CPFS 最小容量 3600 GiB**，且 `advance_100` / `advance_200` 按可用区分别有库存，
  `Resource.OutOfStock` 很常见；先用 `--DryRun true` 逐个可用区探库存再真建。
- **CPFS 只支持部分可用区**（本账号 cn-hangzhou 只有 g/h/i），而现有 vSwitch 往往
  不在这些区里。vSwitch 免费，缺就建。
- **`nas` 系 API 的 `Description` 不接受中文，也不接受空格**，都报 `IllegalCharacters`。
  2026-08-03 逐个试出来：`"cpfs verify dryrun"` 被拒，`"cpfs-verify"` 通过。
  这个错会盖住真正的问题——比如库存不足，得先把 Description 弄干净才看得到 `Resource.OutOfStock`。
- **`aliyun` CLI 的 profile 默认 region 可能与资源所在 region 不一致**（本账号 profile 是 `cn-shanghai`，PAI Workspace 在 `cn-hangzhou`）。所有命令显式带 `--region`。
- **`aiworkspace` 产品必须显式 `--endpoint aiworkspace.<region>.aliyuncs.com`**，否则报 unknown endpoint。
- **zsh 不做未加引号变量的单词切分。** 不要把多个 CLI flag 塞进一个变量再展开，会被当成单个参数。**注意 `/bin/sh` 脚本里同样的写法是安全的**（sh 会切分），所以 `scripts/*.sh` 里的 `$P` 能用；在交互式 zsh 里手敲同一条命令却会失败。
- **`cmd | while read` 的循环体在子 shell 里，里面的变量赋值出了循环就丢。** 计数器、标志位全部归零，而循环体本身的输出一切正常——症状是「逐条都打印了，汇总却是 0」。要在主 shell 里累加就写成 `while ... done < 文件`。
- **`$VAR` 后面紧跟中文必须写成 `${VAR}`。** `/bin/sh` 会把全角字符的字节当成变量名的一部分，配合 `set -u` 报 `VAR?: unbound variable`。
- **Python 3.12 之前，f-string 表达式里不能有反斜杠转义的引号。** 在 `python3 -c '...'` 里尤其容易踩到，先取值到变量再格式化。
- **heredoc 会占用 stdin。** `printf '%s' "$x" | python3 - <<'PY'` 里 python 从 heredoc 读程序，管道数据被丢弃；要传数据就写临时文件。
- **PAI Dataset 的 Uri scheme 必须与 DataSourceType 严格对应**：OSS→`oss://`、
  NAS→`nas://`、CPFS→`cpfs://`、BMCPFS→`bmcpfs://`。**官方文档说 CPFS 用
  `nas://` 是错的**（2026-08-03 真实账号实测）。`Property=DIRECTORY` 还要求
  Uri 以 `/` 结尾。
- **CPFS 里已经有文件的目录不能注册成 Fileset。** Fileset 只能建在新的/空的路径上，
  没法把一个已有数据的目录「就地」变成 Fileset。
  **这条的连锁后果比它看起来大**：数据流动的第一条前提是必须挂 Fileset（`FsetId`
  必填），所以**存量 CPFS 数据无法直接接入数据流动**——必须先把它搬进一个新建的
  Fileset 路径。同一文件系统内的 `rename` 是元数据操作（秒级），但跨 Fileset 边界
  未必，规划迁移时要先确认这一点，别假设一定是零拷贝。
  推论：`cpfs-workspaces` 模块要在**目录还空着的时候**就建好 Fileset。等用户
  往 `/users/<name>/` 写了东西再补，就来不及了。
- **CPFS 不能跨可用区挂载。** 挂载点的 vSwitch 必须与文件系统同可用区，否则
  `CreateProtocolMountTarget` 直接报 `VSwitchZoneMismatch.InvalidParam`
  （`VSwitch Zone should be same with filesystem`）。
- **「文件系统建得出来」不等于「挂得上」——挂载点是另一份库存。**
  2026-08-03 实测：cn-hangzhou-i 有文件系统库存（`CreateFileSystem` 成功），
  但 `CreateMountTarget` 报 `Resource.OutOfStock`（`The inventory of the
  specified zone is insufficient`）。
  症状很误导：协议服务显示 `Running` 但 `MountTargetCount` 恒为 0，
  `CreateProtocolMountTarget` 返回的导出显示 `AVAILABLE`，可挂载域名被阿里云
  VPC DNS 解析成 **`127.0.1.255`**（NOERROR、真实 A 记录、TTL 10），
  客户端表现为 `mount.nfs: Connection refused` 或超时。
  **看着像网络/端口/权限问题，实际是后端没配出来。** 排查时先试
  `CreateMountTarget` 看是否 OutOfStock，比对着 NFS 参数试半天快得多。
  同区、`FsetId` 导出、`VSwitchIds` 都试过，都不是原因。
- **`CreateProtocolMountTarget` 的 `VSwitchIds`（复数）实际不可用**：带上它报
  「`VSwitchId` 和 `VSwitchIds` 只能二选一」，去掉 `VSwitchId` 又报
  「`VSwitchId` 必填」。只能用单数。
  **架构后果**：CPFS 只支持部分可用区（本账号 cn-hangzhou 只有 g/h/i），
  而挂载又必须同区，所以**所有要挂 CPFS 的东西——自托管 runner、DSW、DLC——
  都必须落在 CPFS 所在的那个可用区**。选可用区时要同时满足「CPFS 有库存」和
  「算力资源可用」，这两个条件的交集可能很小。2026-08-03 实测。
- **`Throughput` 在 CPFS 的两个 API 上要求正好相反。**
  `CreateProtocolService --ProtocolSpec General` **不能**传 `Throughput`，传了报
  `PermissionDenied.ThroughputInvalid`（`Standard protocol service should not
  specified throughput`）；而 `CreateDataFlow` **必须**传，且只接受 600/1200/1500。
  两个都在 `nas` 名下、参数同名、要求相反，很容易照着另一个抄错。
- **建 DataFlow 之前协议服务必须已经 Running。** 协议服务还在 `Creating` 时建
  DataFlow 会报 `OperationDenied.InvalidState`——就是下面那条说的「会盖住真正原因」
  的典型：报的是文件系统状态不对，实际原因是另一个资源没就绪。2026-08-03 实测。
- **CPFS 数据流动有六条前提**（2026-08-03 真实 CPFS 2.0 上逐条撞出来）：
  必须挂 Fileset（`FsetId` 必填）；`Throughput` 必填且只接受 600/1200/1500；
  **OSS 桶必须打 `cpfs-dataflow` 标签**否则拒绝；相关资源未就绪时报
  `OperationDenied.InvalidState`——这个错会盖住真正的原因，排查要逐个参数剥离；
  **Export 要求源桶开版本控制**（Import 不要求）；**同一 DataFlow 的任务串行**，
  前一个没到终态就提交下一个会被拒。
- **lakeFS import 源的 scheme 要匹配 lakeFS 的 blockstore adapter 类型，不是云厂商名字。**
  阿里云 OSS 是通过 lakeFS 的 `s3` adapter 访问的（`blockstore.type: s3` + OSS 的
  S3 兼容端点），所以 `commit --object-store-uri` 必须写 **`s3://`**，写 `oss://`
  会在服务端报 `invalid storage scheme oss: invalid address`（2026-08-03 真实
  lakeFS 1.84.1 + OSS 后端实测）。这个错要等 import 任务跑起来才出现，堆栈指向
  lakeFS SDK 内部，看不出真正原因。`commit` 现在会在本地先拦一次。
- **lakeFS 启动时会校验已有 repo 的 adapter 类型。** 已经有 `local` namespace 的
  repo 时，改成 `blockstore.type: s3` 会直接 fatal：`Mismatched adapter detected`。
  想在同一台机上同时试两种后端，就另起一个实例（独立 `database.local.path` +
  独立 `listen_address`），别去改现有实例的配置。
- **lakeFS 的配置项都能用环境变量覆盖**（`LAKEFS_` + 路径大写下划线，如
  `LAKEFS_BLOCKSTORE_S3_CREDENTIALS_SESSION_TOKEN`）。临时验证用它，
  凭证就只存在于进程环境里，不必写进配置文件。
- **`CreateDataFlowTask` 的 `Directory` 是相对 DataFlow 的 `FileSystemPath` 的路径，
  不是绝对的文件系统内部路径。** 这是第三个坐标系。2026-08-03 真机对照实测
  （`FileSystemPath = /verify/`）：
  `Directory=/verify/imp-test/` → **Failed，progress 0**；
  `Directory=/imp-test/` → **Completed，progress 100**。
  **传绝对路径不会报参数错误**——任务被正常受理，几秒后变 Failed，`ProgressStats`
  是空的、没有任何 ErrorMessage。纯静默失败。`dataflow.py` 原来就是传绝对路径，
  单元测试还把它固化成了期望值（注入的 runner 只检查我们发了什么，不知道服务端
  怎么解释）。
- **`DescribeDataFlows` 不返回 `SourceStoragePath`。** 创建时传了也读不回来。
  于是「CPFS 路径 → OSS 前缀」的换算**无法在运行时校验**：如果 DataFlow 建的
  时候设了 `SourceStoragePath`，我们算出的 URI 会静默少掉那一段。
  本仓库的 `cpfs-workspaces` 只绑桶根，所以自建的没问题；接手别人建的必须人工确认。
- **CPFS 挂载路径 ≠ CPFS 文件系统内部路径。** `pai-request` 的 `release_dir` 是挂载视角，`--filesystem-path` 是文件系统内部视角，两者不能混用。
- **`terraform` 不在 homebrew-core**（BUSL 许可），需 `brew install hashicorp/tap/terraform`。
- **本环境 `registry.terraform.io` 被网络策略拦截**，Provider schema 只能靠 `terraform validate` 核对，不要凭记忆写字段。查资源真名的最快方式是 `terraform providers schema -json` 后用 Python 过滤。
- **`check` 块失败只产生 Warning，plan 退出码仍是 0，挡不住 apply。** 要真正阻断必须用 `lifecycle precondition`（Error，退出码 1）。`check` 只适合漂移检测这类「首次 apply 时必然不成立」的场景。
- **`terraform init` 只设 `TF_PLUGIN_CACHE_DIR` 不够**：它仍会去 GitHub 取 `SHA256SUMS`。完全离线要用 `filesystem_mirror` + `TF_CLI_CONFIG_FILE`。

## 提交约定

- 提交信息用中文，首行 `<type>: <做了什么>`，正文说明**为什么**而不是罗列改了哪些文件。
- 结构性移动（`git mv`）单独成 commit，不要和逻辑改动混在一起。
- 改了 `infra/` 或 `deploy/ram/` 的 PR 需要 CODEOWNERS 评审。
