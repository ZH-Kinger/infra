# 变更记录

采用 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，版本号遵循语义化版本。

## [未发布]

### 新增

- 整体架构文档 `docs/architecture.md`：分层架构、端到端数据流、CPFS 发布协议、
  RAM×PAI×CPFS×lakeFS 四套授权面模型、Terraform 三层 state 与两条流水线的交付架构。
- 工程规范配置：`.editorconfig`、`.dockerignore`、`.pre-commit-config.yaml`、
  `.terraform-version`、`.tflint.hcl`、`pyproject.toml` 的 ruff 配置。
- 仓库约定 `AGENTS.md`（`CLAUDE.md` 引用同一份），含 6 条硬规则与已知踩坑清单。
- 项目级 `.claude/settings.json`：只读命令 allow，`terraform apply/destroy/state`
  与凭证类文件读取 deny。
- `.github/CODEOWNERS`、PR 模板（含权限变更专用检查项）、`dependabot.yml`。

### 变更

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
