# 管理员手册

面向平台管理员、数据管理员、安全审核人和 SRE。用户操作见[用户手册](user-guide.md)；
底层初始化和故障命令继续以[运维手册](runbook.md)为准。

## 1. 管理边界

管理平台不是新的高权限控制面。它把用户输入收敛为仓库内固定 Workflow，请求经过：

```text
站点身份 → 服务端字段校验 → 管理员白名单 → GitHub Workflow
→ OIDC/STS 临时 RAM Role → Environment 审批 → 阿里云 API
```

门户不保存阿里云 AccessKey，用户不能提交任意 Role ARN、PAI Dataset ID、VPC、镜像地址
或挂载路径。所有写操作默认 plan-only。

### 用户 Agent Skill 的管理

仓库内的 [`dataset-platform-user`](../skills/dataset-platform-user/SKILL.md) 是用户侧统一
入口。管理员应把它与平台代码一起评审和发布，不要在个人提示词里维护另一套规则。

Skill 只生成计划和升级请求，不能创建 RAM/PAI/CPFS 资源。变更下列契约时必须同步更新
Skill 及其引用：Workflow 输入、运行时 Profile、数据源目录、挂载路径、DataFlow 默认值、
权限申请流程和常见错误。合并前运行仓库内的 Skill 契约测试；CI 的 `make test` 会执行
同一个测试文件：

```bash
python3 tests/unit/test_user_skill.py
```

## 2. 控制台页面与运维责任

| 页面 | 管理员关注点 |
|---|---|
| 运营总览 | 数据量、CPFS 水位、活动实例、策略违规和失败操作 |
| 数据资产 | Commit/Manifest 一致性、PAI Version 注册状态、热副本比例 |
| 存量纳管 | 数据源登记、Fileset/DataFlow 覆盖、发布审批 |
| 容量与生命周期 | Import、Export、Evict、保护期、活动作业引用 |
| DSW / DLC | Profile、ACR 镜像白名单、网络、SSH、公网策略和最长运行时间 |
| 权限与审计 | RAM、PAI 成员、POSIX、挂载合同与 D1 操作记录 |

## 3. 门户运行配置

托管环境至少配置以下服务端变量：

| 变量 | 用途 |
|---|---|
| `GITHUB_TOKEN` | 只用于触发目标仓库 Workflow；不发送到浏览器 |
| `OPS_ADMIN_EMAILS` | 允许提交真实执行的账号白名单，逗号分隔 |
| `GITHUB_REPOSITORY` | 默认 `ZH-Kinger/infra` |
| `GITHUB_REF` | 默认 `main` |

D1 保存操作类型、请求参数、操作人、Workflow、状态和链接。敏感凭证不得进入 D1、日志、
示例或提交记录。

## 4. Workflow 映射

| 门户操作 | Workflow | 默认行为 |
|---|---|---|
| OSS/CPFS 纳管与发布 | `dataset-release.yml` | `transfer_mode=dataflow` |
| DSW/DLC 创建 | `pai-runtime.yml` | 先生成完整请求，执行需审批 |
| 生命周期 | `dataset-lifecycle.yml` | 手动触发默认只生成计划，Evict 需审批 |
| 权限与挂载审计 | `pai-mount-audit.yml` | 只读 |

发布流水线不会接受 Branch、`latest` 或日期作为训练版本，只接受不可变 Commit/Tag。

### DSW/DLC 的 CI/CD 创建链路

管理员应把 DSW/DLC 当作流水线按次创建的运行时，而不是让用户在控制台自由组合参数：

```text
workflow_dispatch / 门户请求
  → request Job 无云身份渲染并校验完整 OpenAPI Body
  → Artifact 固化审批对象
  → pai-runtime Environment required reviewers
  → OIDC 按 runtime 选择 DswSubmitRole 或 DlcSubmitRole
  → 同一 Artifact 调用 CreateInstance / CreateJob
```

审批时至少核对 Commit、PAI Dataset Version、镜像 Digest、算力规格、网络、RO/RW 挂载、
DSW 所有者或 DLC 命令，以及 TTL。不要只看用户表单；最终审批对象是当前执行 Run 的
`runtime-envelope.json` 和 `runtime-request.json`。`execute=false` 的历史预览不能代替
当前执行 Run 的审批，因为 Run ID、输出路径和过期时间会变化。

DSW 与 DLC 使用不同 Submit Role。策略设计要求两个 Role 不能修改 Dataset Version、
RAM Policy、网络或镜像白名单，Profile 与 Repository Variables 的变更必须走代码评审；
但在真实账号完成授权对照测试前，不能把 DSW `CreateInstance` 的 Action 收敛描述为已经
验证的强制隔离。创建响应和后续状态仍应关联 GitHub Run ID，供门户审计和资源回收使用。

发布门禁必须同时包含：CI 对渲染后 RAM 策略的检查，以及真实账号下的成功/拒绝对照。
受控请求应由对应 Submit Role 创建成功；修改 Dataset Version、RAM Policy、网络、镜像
白名单或使用错误运行时角色的请求必须得到 `AccessDenied`。对照结果未留档前，不开放
生产 `pai-runtime` Environment 的执行权限。

## 5. DataFlow 自动化

生产发布默认调用 CPFS 数据流动：

```text
沉淀：CPFS → Export → OSS
预热：OSS → Import → CPFS .materializing
发布：Manifest/SHA-256 校验 → rename → _READY
回收：确认可恢复与未被占用 → Evict
```

必须同时满足：

1. 操作路径已经属于 Fileset；
2. DataFlow 覆盖该路径，且绑定到正确 OSS 桶根；
3. OSS 桶带 `cpfs-dataflow` 标签；
4. Export 的目标桶开启版本控制；
5. 协议服务和 DataFlow 已经 Running；
6. 同一 DataFlow 的任务串行执行。

`Import`/`Export` 只搬字节，不证明内容正确，所以完成后仍执行全量校验。找不到覆盖路径
必须失败，禁止自动回退到客户端复制。迁移期如确需兼容，由管理员显式选择
`transfer_mode=client` 并记录原因。

## 6. 权限隔离

| 身份 | 允许 | 明确不允许 |
|---|---|---|
| Materializer | OSS 归档、CPFS 发布、DataFlow Task | PAI 注册、训练提交 |
| Register | 创建 PAI Dataset Version | 读取或修改数据 |
| Lifecycle | 查询占用、执行受保护 Evict | OSS 删除、硬删除 release |
| DSW/DLC Submit | 按 Profile 提交实例 | 修改数据版本、任意镜像和网络 |
| Audit | 查询 PAI、CPFS、挂载和 DataFlow | 所有写操作 |

RAM 控制云 API，PAI 成员控制工作空间能力，Fileset/POSIX 控制目录读写，lakeFS 控制
数据版本；四者不能互相替代。详细策略见[权限模型](permissions.md)。

## 7. 日常运维顺序

1. `make preflight` 做真实环境只读体检。
2. 检查 Fileset、DataFlow、OSS 标签/版本控制和同可用区挂载。
3. 在控制台生成计划，核对 Workflow 请求。
4. 由白名单管理员提交执行。
5. 在 GitHub Environment 完成人工审批。
6. 检查 DataFlow Task、`verify --deep`、PAI Version 和冒烟训练结果。
7. 在“权限与审计”确认操作人、状态和请求参数已记录。

## 8. 发布前验证

```bash
make test
make lint
make e2e
actionlint .github/workflows/*.yml
cd portal && npm run lint && npm run build
```

真实 DataFlow/PAI 测试放在 `tests/integration/`，没有显式环境变量时必须 skip。不要为了
测试在本地执行 `terraform apply`、`destroy`、`import` 或修改 state。

## 9. 故障入口

| 问题 | 首先检查 |
|---|---|
| DataFlow `InvalidState` | 协议服务、文件系统和上一个任务是否已到终态 |
| DataFlow 路径未覆盖 | Fileset、FileSystemPath 与挂载路径坐标转换 |
| Import 完成但校验失败 | OSS 前缀映射、Manifest、Directory 是否为相对 DataFlow 路径 |
| PAI 注册 URI 错误 | DataSourceType 与 `cpfs://`/`nas://`/`bmcpfs://` 是否匹配 |
| DSW/DLC 挂载失败 | 可用区、vSwitch、协议服务、Dataset Version 和 RO 合同 |
| 只能生成计划 | 站点白名单、登录身份和服务端 `GITHUB_TOKEN` |

更详细的云端踩坑和恢复步骤见[运维手册](runbook.md)、
[存储生命周期](storage-lifecycle.md)和[CI/CD](cicd.md)。
