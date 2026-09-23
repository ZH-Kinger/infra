# 变更记录

采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循语义化版本。

## [未发布]

### 新增

- 云权限面板**待办页**（`src/delivery/todo.py`、`GET /api/admin/todo`、`web/todo.js`）：
  管理后台默认落地页，按「要紧的 / 该处理的 / 顺手做的」分组，排序算在服务端，
  依据数据旧了就降级并标注，零云调用。
- 云权限面板**离职停号**（`src/delivery/offboard.py`、`delivery identity iam-remind`）：
  强信号（飞书状态已离职 / 公司 IAM 标离职）自动停用阿里云与火山账号（关登录、禁 AK，可恢复），
  一轮超过 3 人则一个都不停；弱信号只记待确认；九章没有接口，只提醒去控制台并支持人工销账；
  删号必须管理员确认，且只删账号、不碰数据；受保护名单在代码和云策略两处。
- **飞书卡片按钮回调**（`src/delivery/card_hook.py`、`POST /feishu/card`）：管理员在飞书卡片上
  直接点「确认删除 / 没离职」。验签 + Verification Token + 管理员名单三道门，
  没配 `DELIVERY_FEISHU_CARD_ENCRYPT_KEY` 一律拒绝。
- 定时单元**失败告警**：refresh / sweep / assets / buckets / moves / iam-remind / backup 七个单元
  都接 `OnFailure=delivery-unit-failed@%n.service`，崩溃或被杀时私聊管理员。
- 凭证**到期提醒两档**：申请人飞书私聊在到期前 7 天、1 天各一次；待办页同时列快到期与已过期。
- 凭证模板「服务访问凭证 · 程序用」：给 lakeFS 这类长期在跑的程序用，有效期上限 90 天
  （`catalog.MAX_CREDENTIAL_HOURS` 硬顶），用途里要写服务名和负责人。
- 整体架构文档 `docs/architecture.md`：分层架构、端到端数据流、CPFS 发布协议、
  RAM×PAI×CPFS×lakeFS 四套授权面模型、Terraform 三层 state 与两条流水线的交付架构。
- 工程规范配置：`.editorconfig`、`.dockerignore`、`.pre-commit-config.yaml`、
  `.terraform-version`、`.tflint.hcl`、`pyproject.toml` 的 ruff 配置。
- 仓库约定 `AGENTS.md`（`CLAUDE.md` 引用同一份），含 6 条硬规则与已知踩坑清单。
- 项目级 `.claude/settings.json`：只读命令 allow，`terraform apply/destroy/state`
  与凭证类文件读取 deny。
- `.github/CODEOWNERS`、PR 模板（含权限变更专用检查项）、`dependabot.yml`。

### 变更

- **凭证前缀内外分家**：面板发给内部同事的子账号改用 `staff-*`、策略 `staff-oss-auto-*`；
  机器人发给外部方的保持 `tempak-*` / `temp-ak-auto-*`，跨云迁移交给对方云的源端钥匙按外部算。
  云上 `wuji-panel-issuer` 策略已按前缀收窄，副本在 `deploy/panel/cloud-policies/`；
  **改前缀必须同步改云策略**。撤销与到期清理两套前缀都认，存量不改名、到期自然清理。
- 凭证策略补三个 OSS 动作（真机结论）：`oss:GetBucketLocation`（S3 兼容客户端建连要探地域）、
  `oss:GetObjectVersion`（桶开版本控制时带 version id 的请求走它）。另确认
  `HeadObject`/`GetObjectMeta` 都映射到 `oss:GetObject`，**做不到「只给元信息不给下载」**。
- 文档更正：线上是**飞书登录**（`delivery serve --auth feishu`），公司 IAM 登录
  （oauth2-proxy + OIDC）只有代码和配置模板，尚未启用。
- 仓库纳入 git 版本控制（此前无版本控制）。
- 测试拆分为 `tests/unit/`（离线）与 `tests/integration/`（需真实环境，缺环境变量时 skip）。
- PAI 作业模板从 `examples/pai/` 迁至 `deploy/pai/`，`examples/` 只保留数据样例。
- `Makefile` 扩展为 `test / compile / lint / fmt / e2e / tf-fmt / tf-validate / hooks /
  discover / render-ram`，并提供 `make help`。

## [0.1.0] - 2026-08-02

首版，实现 lakeFS Commit → CPFS 不可变 release → PAI Dataset Version 的发布链路。

### 新增

- lakeFS Tag/Branch/Ref 解析为固定 Commit ID。
- 经 lakeFS S3 Gateway 读取固定 Commit 并并行沉降至 CPFS。
- `certify`：CPFS Staging 已有数据的零复制原子发布（同文件系统 rename）。
- 文件集合、`size_bytes`、SHA-256 三重校验；`verify --deep` 全量重算哈希。
- `<dataset>/<commit>` 不可变目录、`.locks` 进程锁、`release.json`、`_READY` 发布协议。
- 同 Commit 幂等；同 Commit 不同 Manifest 抛 `ReleaseConflictError` 拒绝覆盖。
- 生成 PAI `CreateDatasetVersion` 请求，并经阿里云 CLI 注册（默认 dry-run，
  `--execute` 前按 lakeFS Commit 查重）。
- `training-guard`：训练启动前校验 Commit、`manifest_sha256`、Paimon Snapshot，
  不匹配则 fail-closed。
- DLC 只读挂载模板、训练入口脚本、最小 RAM 策略样例、本地 E2E 演练脚本。
