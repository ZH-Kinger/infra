# DSW/DLC 自助运行

运行时只负责消费已发布版本。OSS/CPFS 分区、数据进入训练的路径和回收规则见
[存储生命周期](storage-lifecycle.md)。

用户不需要填写阿里云 `CreateInstance` / `CreateJob` 的全部字段。在 GitHub Actions
手动运行 **PAI runtime**，只选择：

- `runtime`：`dsw` 或 `dlc`；
- `dataset` 与不可变 lakeFS `commit`；
- `image_profile` 与 `compute_profile`；
- DLC 可选填写训练命令。

第一次运行保持 `execute=false`。流水线只生成完整 OpenAPI 请求并上传 Artifact，不获取
云身份、不产生费用。确认请求后再用 `execute=true` 运行；写操作进入 `pai-runtime`
Environment 审批，通过后才使用 OIDC 临时角色创建资源。

## 从提交到创建的 CI/CD 流程

```text
GitHub Actions / 门户提交少量参数
  → request：检出代码并运行 dataset-sink runtime-request
  → 校验 Commit、数据集映射、GitHub 用户映射和 Profile 组合
  → 从 Repository Variables 与 runtime-profiles.json 补齐平台默认值
  → 生成 runtime-envelope.json + runtime-request.json
  → Artifact 保留 30 天，供用户和审批人核对
  → execute=false：到此结束
  → execute=true：等待 pai-runtime Environment 审批
  → OIDC/STS 获取对应的 DSW 或 DLC 最小权限角色
  → 使用本次 Run 的 runtime-request.json 调用阿里云 OpenAPI
```

DSW 分支执行 `pai-dsw CreateInstance`，DLC 分支执行 `pai-dlc CreateJob`。执行 Job 只下载
`request` Job 生成的 Artifact，不根据用户输入临时拼装第二份请求。因此同一次执行 Run
中，审批对象和发给阿里云的 OpenAPI Body 一致。

`execute=false` 的预览 Run 和之后的 `execute=true` Run 不是同一个 Run：后者会重新生成
请求，以取得新的 Run ID、输出路径和过期时间。审批人应核对执行 Run 自己的 Artifact，
不能把先前预览 Run 当作最终审批对象。

### 谁负责什么

| 层 | 职责 |
|---|---|
| Terraform | 建设 Workspace、网络、OIDC Provider、RAM Role、GitHub Variables/Environment 所需基础配置 |
| 用户或门户 | 提供运行类型、数据集、Commit、镜像 Profile、算力 Profile 和可选 DLC 命令 |
| request Job | 无云身份生成、校验并固化完整请求 |
| GitHub Environment | 由审批人决定本次请求能否取得云写权限 |
| execute Job | 用 OIDC 临时角色创建 DSW/DLC，不保存 AccessKey |
| PAI | 执行实例/作业，并按照请求挂载已发布 Dataset Version |

## 平台默认值

[`runtime-profiles.json`](../deploy/pai/runtime-profiles.json) 是受代码评审的控制面：

- 镜像只允许配置的 ACR Registry，并必须固定到 `@sha256:<digest>`；
- DSW/DLC 固定 Workspace、资源组、VPC、vSwitch、安全组和私网默认路由；
- release Dataset Version 固定为 Commit ID，并只读挂载到 `/mnt/dataset`；
- DSW 的个人 CPFS 工作区读写挂载到 `/mnt/workspace`；
- DLC 的独立输出目录读写挂载到 `/mnt/output`；
- DSW 是 `PRIVATE`，`UserId` 从 GitHub 用户到 RAM User ID 的受控映射取得；
- 不生成 `ForwardInfos`，因此默认不开 SSH 端口转发；
- DLC 用户命令不能绕过 `training-entrypoint.sh`，必须先通过 `training-guard`；
- Profile 固定实例规格、Pod 数量和 TTL；DLC TTL 由 API 的最大运行时间强制执行。

### 标准挂载

| 路径 | 来源 | 权限 | 状态 |
|---|---|---|---|
| `/mnt/dataset` | 已注册的 CPFS Dataset Version | RO | 已实现 |
| `/mnt/workspace` | 用户 CPFS/NAS 工作区 | RW | DSW 已实现 |
| `/mnt/output` | 任务独立 CPFS/NAS/OSS 输出前缀 | RW | DLC 已实现 |
| `/mnt/oss-workspace` | 受控 OSS 用户前缀 | RW/RO | 尚未实现 |

未注册为 PAI Dataset 的 OSS 可以作为未来的附加工作区，但不能替代 `/mnt/dataset`。
正式训练数据必须先完成 lakeFS Commit、CPFS release 和 PAI Version 注册。用户永远不能
从运行表单提交任意 OSS URI。

DSW 的 `RUNTIME_EXPIRES_AT` 会写入实例环境变量供回收审计使用；当前版本尚未实现定时
StopInstance，同时也写入控制面的 `expires_at` Label；当前版本尚未实现定时回收，因此
它只是可审计的到期标记，不应声称已经自动停机。

阿里云当前的 CreateInstance API 文档将该操作标为“无 RAM 授权信息”。因此
`dsw-submit` 的 Action 收敛必须在真实账号中通过一次 AccessDenied/成功对照测试确认，
不能只看策略 JSON 就声称 RAM 已经完成强制隔离。DSW 所有者 `UserId`、PAI Workspace
成员关系、受保护 Environment 和请求白名单仍是必需的其他边界。

## GitHub Repository Variables

| Variable | 用途 |
|---|---|
| `PAI_WORKSPACE_ID`, `PAI_DATASET_IDS_JSON` | 工作空间与“数据集短名称 → 已有 PAI Dataset ID”映射；单数据集可暂用 `PAI_DATASET_ID` |
| `PAI_ACR_REGISTRY`, `TRAINING_IMAGE` | ACR 白名单前缀与 Digest 镜像地址 |
| `DSW_ECS_SPEC`, `DSW_RESOURCE_ID` | `gpu-dev` Profile |
| `DLC_ECS_SPEC`, `PAI_RESOURCE_ID` | `gpu-training` Profile |
| `VPC_ID`, `VSWITCH_ID`, `SECURITY_GROUP_ID` | 固定网络边界 |
| `VPC_EXTENDED_CIDRS` | 逗号分隔的 VPC CIDR；固定 vSwitch 时 API 要求提供 |
| `PAI_WORKSPACE_URI_PREFIX`, `PAI_OUTPUT_URI_PREFIX` | CPFS/NAS URI 前缀，不含用户名或 run ID |
| `PAI_DSW_OWNER_USER_ID` | 当前 GitHub 用户对应的 RAM User ID |
| `DSW_SUBMIT_ROLE_ARN`, `DLC_SUBMIT_ROLE_ARN` | 审批后使用的两个最小权限 OIDC 角色 |
| `ALIBABA_CLOUD_OIDC_PROVIDER_ARN`, `ALIBABA_CLOUD_REGION` | OIDC 与地域 |

当前配置只登记了 `ZH-Kinger`。新增用户必须通过 PR 在 `users` 中登记 GitHub 用户名与
RAM User ID，并同步准备其 CPFS Fileset/POSIX 权限。用户不能在运行表单里指定别人的
User ID 或工作区路径。

`DefaultRoute=eth1` 假定 VPC 已配置私有 NAT/专用公网网关。如果环境没有该能力，先由
平台管理员完成网络建设；不要为了让创建成功改成 `eth0`，后者会走公共网关。
