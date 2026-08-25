# 数据湖集成实验：复现步骤与问题记录

本文记录如何在阿里云上复现一条最小但接近生产形态的数据湖链路：

`GitHub Actions → ACK Pro → Airflow → 4 CPU 节点 → Spark/Iceberg → 5 存储节点 MinIO → OSS`

目标不是做性能结论，而是先验证控制面、计算面、元数据和五节点对象存储能否完整闭环。MinIO 只模拟
本地 S3 合约和节点故障，不代表 H3C 混闪的 HDD/NVMe 性能。本地 NFS 多协议互通、lakeFS 和专线同步
属于下一阶段，不混入本实验，否则失败时无法定位层次。

## 1. 实验范围

本实验验证以下行为：

1. Terraform 能以 OIDC 临时身份创建隔离的 VPC、ACK Pro 和四个私有 OSS Bucket。
2. Airflow 能在 Kubernetes 内提交并跟踪 `SparkApplication`。
3. Spark 能访问五节点 MinIO，并创建 Iceberg Format V2 表。
4. 作业能执行幂等写入、当前快照读取和指定 Snapshot ID 的历史读取。
5. 验证结果能通过阿里云内网端点写回 OSS Result Bucket，失败能在 CI 中返回非零退出码。
6. 环境能通过独立的审批式工作流整体销毁，不在控制台逐个删除资源。

不验证以下内容：lakeFS 分支与合并、本地 H3C 混闪、多协议互通、5 Gbps 专线吞吐、真实 MCAP 或
LeRobot v3 数据处理、训练集群读取。这些项目要在基础链路通过后分阶段接入。

## 2. 固定版本与资源规格

| 项目 | 实验值 |
|---|---|
| 地域 / 可用区 | `cn-hangzhou` / `cn-hangzhou-k` |
| ACK | ACK Pro；Kubernetes 版本由 ACK 选择当前可创建版本 |
| CPU 节点池 | 4 节点；优先 `ecs.g8i.2xlarge`，按变量中的列表回退 |
| 存储节点池 | 5 节点；优先 `ecs.g8i.xlarge`，按变量中的列表回退 |
| 本地 S3 模拟 | MinIO 5 成员；每成员独立 200 GiB ESSD PVC |
| Airflow Helm Chart | `1.22.0` |
| Airflow | `3.2.2`，LocalExecutor |
| Spark Operator | `2.5.2` |
| Spark | `3.5.5` |
| Iceberg | `1.11.0` |
| Hadoop Aliyun Connector | `3.3.4` |
| 首轮数据规模 | 1,000,000 行合成 episode 索引 |
| Spark 并行度 | 4 executors × 2 cores；每个 executor 6 GiB |

实例库存随时间变化。表内型号是实验时的选择，不是长期采购承诺。每次 Apply 前必须重新检查目标区库存。

## 3. 资源与数据边界

Terraform 代码位于 `infra/tests/datalake`，创建：

- 1 个专用 VPC 和 1 个 vSwitch；
- 1 个 ACK Pro 托管集群；
- 1 个四节点 CPU 池和 1 个五节点存储池；
- 4 个启用版本控制、AES-256 服务端加密的私有 Bucket：`landing`、`lakefs`、`iceberg`、`result`。

五节点 MinIO 保存本地 Iceberg 表，OSS 在本实验中只承担归档和结果层。`lakefs` Bucket 在第一阶段只预留，
不部署 lakeFS。测试数据没有业务价值，Bucket 标记为 `Ephemeral=true`。不要上传生产原始数据。

## 4. 前置条件

### 4.1 阿里云账号

- ACK 服务已经开通；
- 主账号已完成 ACK 默认服务角色的一键授权，至少存在 `AliyunCSDefaultRole`；
- OSS、ECS、VPC、ACK 在目标地域可用；
- 管理员已经从 `infra/bootstrap` 创建 `TerraformITestApplyRole`；
- 该角色只信任 GitHub `development` Environment，ACK/VPC 生命周期受 `Environment=itest` 标签约束，
  OSS 访问只允许 `dataset-sink-itest-*` 前缀。

检查默认角色时只查看元数据，不输出凭证：

```bash
aliyun ram GetRole \
  --RoleName AliyunCSDefaultRole \
  --profile <ADMIN_PROFILE> \
  --region cn-hangzhou
```

### 4.2 GitHub Variables

仓库级变量：

- `ALIBABA_CLOUD_OIDC_PROVIDER_ARN`
- `ALIBABA_CLOUD_PLAN_ROLE_ARN`
- `ALIBABA_CLOUD_REGION`
- `TF_STATE_BUCKET`
- `TF_STATE_LOCK_ENDPOINT`
- `TF_STATE_LOCK_TABLE`
- `TFVARS_ITEST_DATALAKE_JSON`

`development` Environment 变量：

- `ALIBABA_CLOUD_ITEST_APPLY_ROLE_ARN`

不得创建长期 AccessKey Secret。创建、部署和测试都使用 GitHub OIDC 换取的短期 STS 会话。

### 4.3 本地只读检查

本地只做格式、语法和离线测试，禁止执行本地 `terraform apply/destroy/import/state`：

```bash
make lint
make test
make e2e
make tf-validate
shellcheck deploy/datalake-itest/*.sh
```

## 5. 实验步骤

### 步骤 0：记录实验批次

在实验记录中填写日期、操作者、Git Commit、地域、可用区、节点规格和 GitHub Run URL。每次调整只改一个
变量，例如先改行数，再改 executor 数，不能同时调整数据规模和集群规格。

### 步骤 1：检查库存和 ACK 前置授权

使用只读身份检查目标区候选实例是否为 `Available/WithStock`。不要只检查第一候选，至少保留两个回退型号。

然后调用 ACK 版本元数据接口。如果返回 `EntityNotExist.Role`，不要继续 Apply；先完成第 8.1 节的角色授权。

```bash
aliyun cs DescribeKubernetesVersionMetadata \
  --Region cn-hangzhou \
  --ClusterType ManagedKubernetes \
  --Mode creatable \
  --Profile Default \
  --profile <READONLY_PROFILE> \
  --region cn-hangzhou
```

### 步骤 2：生成并审核 Terraform Plan

在 GitHub Actions 手动运行 `Terraform`：

- `target = itest-datalake`
- `confirm_apply = false`

下载并保存 `tfplan-itest-datalake-<commit>` 产物。首次基线预期为：

```text
Plan: 13 to add, 0 to change, 0 to destroy.
```

必须逐项确认只有测试 VPC、vSwitch、ACK、两个节点池、四个 Bucket 和四个 ACL。若出现现有资源的
`change`、`replace` 或 `destroy`，本轮实验停止。

### 步骤 3：审批并创建基础设施

代码合并到 `main` 后，再次手动运行 `Terraform`：

- `target = itest-datalake`
- `confirm_apply = true`

Apply Job 会停在 `development` Environment 审批。审批人核对 Plan artifact 与步骤 2 一致后放行。

创建完成后记录 Terraform 输出：ACK Cluster ID、四个 Bucket 名、VPC ID 和 vSwitch ID。不要记录
kubeconfig、STS Token 或任何密钥。

### 步骤 4：检查 Kubernetes 基线

运行 `Data lake integration test`，先选择 `status`。工作流会获取有效期 90 分钟的临时 kubeconfig。

预期：

- 9 个节点均为 `Ready`；
- 4 个 CPU 节点带 `workload=cpu`；
- 5 个存储节点带 `workload=storage` 和对应 `NoSchedule` taint；
- 不存在持续重启或 `NotReady` 节点。

### 步骤 5：部署 Airflow 和 Spark Operator

再次运行 `Data lake integration test`：

- `action = deploy-only`
- `row_count = 1000000`

工作流将执行 `deploy/datalake-itest/deploy.sh`：

1. 创建 `datalake-itest` 命名空间与最小 RBAC；
2. 为 MinIO 生成一次性随机凭证，并部署 5 成员 StatefulSet；
3. 为每个存储成员创建独立 200 GiB PVC，等待五个成员就绪并创建 `landing/iceberg` Bucket；
4. 将短期阿里云 STS 写入 Kubernetes Secret；
5. 创建 Spark 作业代码和运行参数 ConfigMap；
6. 安装 Spark Operator；
7. 安装 Airflow 和内部 PostgreSQL；
8. 等待 scheduler 就绪。

部署后应保存：Helm release 状态、所有 Pod 状态、异常 Pod 的 `describe` 和日志。

### 步骤 6：执行端到端 Smoke Test

运行：

- `action = deploy-and-test`
- `row_count = 1000000`

Airflow 创建一个带时间戳的 `SparkApplication`。Spark 作业依次执行：

1. 在五节点 MinIO 创建 `datalake.robotics.episode_index` Iceberg V2 表；
2. 删除同 `batch_id` 的旧记录，保证重试幂等；
3. 生成并追加 100 万行；
4. 查询当前表并校验行数；
5. 读取最新 Snapshot ID；
6. 按 Snapshot ID 进行 time-travel 读取并再次校验行数；
7. 将 JSON 报告写入 Result Bucket。

通过日志至少应包含：

```json
{
  "status": "passed",
  "row_count": 1000000,
  "snapshot_count": 1000000,
  "snapshot_id": "<ICEBERG_SNAPSHOT_ID>"
}
```

工作流最终输出 `Airflow -> Spark -> Iceberg -> OSS integration test completed.`。只有 Airflow DAG、
SparkApplication 和结果对象三者同时存在，才算链路通过。

### 步骤 7：重试与幂等验证

保持 `row_count` 不变再运行一次。新运行使用新的 `batch_id`，不得覆盖前一批结果。若对同一批次人工重试，
最终行数仍应等于目标行数，而不是翻倍。

### 步骤 8：扩大数据量

Smoke Test 通过后按顺序测试 1M、10M、50M 行。每档记录：

- Airflow 排队时间；
- SparkApplication 提交到 Running 的时间；
- 作业总耗时；
- executor CPU、内存峰值与重启次数；
- OSS 读写流量；
- Iceberg metadata、manifest 和 data file 数量；
- 当前读取与 time-travel 读取耗时。

本阶段的数据是窄表索引，不能用其吞吐推导 MCAP、视频解码或大量小文件的业务性能。

### 步骤 9：完整清理

实验结束后运行 `Destroy data lake integration environment`，输入：

```text
DESTROY-DATALAKE-ITEST
```

先审核 Destroy Plan，再通过 `development` Environment 审批。完成后确认 ACK、ECS、SLB/EIP、VPC、
vSwitch、NAT 和四个测试 Bucket 均已消失，Terraform state 中不再有该测试资源。禁止在控制台只删节点，
否则容易残留 EIP、NAT、云盘或负载均衡计费项。

## 6. 验收标准

| 检查项 | 通过条件 |
|---|---|
| IaC 安全 | Plan 无非预期 change/destroy；Apply 使用审批过的 Plan |
| 身份 | 无长期 AccessKey；仅使用 OIDC、短期 STS 和短期 kubeconfig |
| Kubernetes | 9 节点 Ready；4 个 CPU 节点与 5 个存储节点隔离 |
| 本地对象存储 | 5 个 MinIO Pod 分布在 5 个不同节点，PVC 全部 Bound |
| Airflow | DAG 成功创建并跟踪 SparkApplication |
| Spark | Driver/Executor 正常结束，无 OOM、无限 Pending 或重复重启 |
| Iceberg | 当前读取与指定 Snapshot ID 读取均等于写入行数 |
| OSS | Result Bucket 出现对应批次的 passed 报告 |
| 幂等 | 重试不会让同一批次行数翻倍 |
| 清理 | Destroy 后无测试资源和附属计费项残留 |

## 7. 实验记录模板

| 字段 | 记录 |
|---|---|
| 实验编号 | DL-ITEST-YYYYMMDD-NN |
| Git Commit |  |
| GitHub Plan Run |  |
| GitHub Apply Run |  |
| GitHub Test Run |  |
| 地域 / 可用区 |  |
| Kubernetes 版本 |  |
| CPU 节点实际型号 × 数量 |  |
| 存储节点实际型号 × 数量 |  |
| row_count |  |
| executor 配置 |  |
| SparkApplication 名称 |  |
| Iceberg Snapshot ID |  |
| 总耗时 |  |
| 结果对象路径 |  |
| 结论 | PASS / FAIL / BLOCKED |
| 问题编号 |  |
| Destroy Run |  |

问题记录统一使用以下格式：

```text
时间：
阶段：
现象和完整错误码：
影响范围：
已排除项：
根因：
修复：
修复后证据：
是否需要沉淀为自动检查：
```

## 8. 本次实际遇到的问题

### 8.1 ACK 已开通，但默认服务角色不存在

- 阶段：前置检查。
- 现象：`DescribeKubernetesVersionMetadata` 返回 `EntityNotExist.Role`，缺少
  `acs:ram::<ACCOUNT_ID>:role/aliyuncsdefaultrole`。
- 过程：`OpenAckService --type propayasgo` 返回成功，但再次查询仍缺角色。
- 根因：ACK 产品开通与 ACK 默认服务角色快速授权是两步；开通 API 不等于主账号已同意 RAM 授权。
- 处理：主账号进入 ACK/RAM 快速授权页，确认创建和绑定默认服务角色。角色存在前禁止创建集群。
- 当前状态：已完成；`AliyunCSDefaultRole` 于 2026-08-25 创建，ACK 可创建版本接口已正常返回。

### 8.2 现有 Platform Apply Role 无权创建 ACK/VPC

- 阶段：权限设计。
- 现象：现有 `TerraformPlatformApplyRole` 只管理既有平台存储，且显式禁止 RAM 写入，不具备本实验权限。
- 根因：这是原有 CI 的正确安全边界，不应为了实验把常规平台角色扩成管理员。
- 处理：新增 `TerraformITestApplyRole`，只信任 `development` Environment；ACK/VPC 要求
  `Environment=itest` 标签，OSS 仅允许测试前缀，并继续 Deny 全部 RAM/IMS 写操作。
- 当前状态：代码已合并；角色需要管理员执行一次 bootstrap 后才存在。

### 8.3 PR 自动计划了未配置的旧环境

- 阶段：CI Plan。
- 现象：`itest-datalake` Plan 成功，但 dev/prod 的无关 Job 因缺少各自 tfvars 被标红。
- 根因：原工作流在所有 PR 上默认展开 `all`，没有按变更目录筛选。
- 处理：PR 只计划本次发生变化的环境目录；手动运行的 `all` 行为保持不变。
- 修复证据：修复后 PR 只运行 `Plan itest-datalake`，结果通过。

### 8.4 新增 Terraform Module 后 validate 报未安装

- 阶段：本地静态校验。
- 现象：`Module not installed`，指向新加的 `itest_apply_role`。
- 根因：Terraform modules 缓存尚未刷新，不是 HCL 语法错误。
- 处理：只执行允许的 `terraform init -backend=false`，再运行 `terraform validate`；禁止因此执行本地 Apply。

### 8.5 OSS Connector 与短期凭证

- 阶段：Spark 作业设计。
- 风险：Spark 镜像本身不包含 Iceberg Runtime 和 Hadoop Aliyun Connector；短期 STS 还会过期。
- 处理：作业固定下载 `iceberg-spark-runtime-3.5_2.12:1.11.0` 和 `hadoop-aliyun:3.3.4`，使用
  `org.apache.hadoop.fs.aliyun.oss.AliyunCredentialsProvider`。每次部署都刷新 Secret，不复用过期会话。
- 复现判断：若 Maven 下载失败，Driver 会在业务代码前失败；若 STS 过期，OSS 请求会出现认证错误。两者要分开排查。

### 8.6 节点库存不是静态配置

- 阶段：资源选择。
- 结果：2026-08-25 检查杭州 K 区时，配置中的六个候选规格均返回 `Available/WithStock`。
- 注意：该结果只代表当时库存。Plan 或 Apply 间隔过长仍可能售罄，因此变量使用按顺序回退的实例列表。

### 8.7 VPC 创建成功后因刷新权限失败

- 阶段：首次 4 CPU + 5 存储拓扑 Apply。
- 现象：VPC 创建 API 成功，但 Terraform 随后的 `DescribeVpcAttribute` 返回 `Forbidden.RAM`；四个 OSS
  Bucket 已进入 state，VPC 因创建后读取失败成为 state 外孤立资源。
- 根因：最小权限策略只列出了部分 VPC List/Describe API，未覆盖 Provider 创建后的单资源 Attribute 查询。
- 处理：只读观测面改为 `vpc:Describe*`；在重试前按资源 ID、名称和 `Environment=itest` 标签核验并删除
  无依赖的孤立 VPC，避免留下重复网络和计费附属资源。
- 修复验收：新的 Plan 必须只包含剩余资源，且 Apply 可以完成 VPC 创建后的 refresh。

### 8.8 Plan 禁用锁仍需读取锁表元数据

- 阶段：修复 VPC/OSS 只读权限后的 Plan backend 初始化。
- 现象：工作流使用 `terraform plan -lock=false`，但 OSS backend 初始化仍因 `OTSNoPermissionAccess` 无法
  `DescribeTable`。
- 根因：`-lock=false` 禁止获取状态锁，不代表 backend 不读取锁表是否存在及其元数据。
- 处理：Plan 角色仅增加 `ots:DescribeTable` 和 `ots:ListTable`；不恢复 `PutRow/DeleteRow`，继续保证 Plan
  无法获取、修改或释放锁记录。

### 8.9 vSwitch 创建不支持 RequestTag 条件

- 阶段：VPC 已成功创建并进入 state，创建 vSwitch 时失败。
- 现象：策略允许 `vpc:CreateVSwitch`，但带 `acs:RequestTag/Environment=itest` 条件后仍返回
  `Forbidden.RAM`。
- 根因：当前 Provider/API 创建 vSwitch 的调用没有向 RAM 条件评估暴露 RequestTag，条件无法命中。
- 处理：仅将 `vpc:CreateVSwitch` 的创建动作设为无标签条件；`DeleteVSwitch`、修改和标签操作继续要求
  `acs:ResourceTag/Environment=itest`。因此角色可以创建测试 vSwitch，但不能修改或删除未标记的现有网络。

### 8.10 新建 vSwitch 的首次标签写入死锁

- 阶段：vSwitch 创建 API 已成功，Provider 紧接着调用 `TagResources`。
- 现象：新 vSwitch 尚无资源标签，要求 `acs:ResourceTag/Environment=itest` 的 `TagResources` Allow 无法命中。
- 根因：资源标签条件适合约束已有资源的后续变更，不适合约束资源的第一次标签写入。
- 处理：首次 `TagResources` 改用 `acs:RequestTag/Environment=itest`；删除、修改和取消标签仍使用
  `acs:ResourceTag/Environment=itest`。失败遗留的无标签 vSwitch 经确认无 ECS 依赖后已删除。

### 8.11 ACK 权限必须按完整生命周期审计

- 阶段：vSwitch 权限修复后、首次 ACK Create 之前的主动审计。
- 发现：原策略逐项允许 `CreateCluster`、节点池增删改等动作，却遗漏了 ACK 系统权限基线包含的
  `ram:PassRole`；同时 `Deny ram:*` 会覆盖后续新增的 Allow。继续按 API 报错逐项补权限会造成多轮
  bootstrap，并可能在创建成功后卡在刷新、取 kubeconfig 或销毁阶段。
- 处理：专用 `TerraformITestApplyRole` 按阿里云 `AliyunCSFullAccess` 的动作基线允许 `cs:*`，并只在
  `acs:Service=cs.aliyuncs.com` 时允许 `ram:PassRole`。RAM 角色、策略和 OIDC 身份的创建、修改、绑定与
  删除仍由显式 Deny 禁止。角色的 OIDC 信任仍只接受本仓库受保护的 `development` Environment。
- 验收：单元测试同时断言 ACK 全生命周期、受限 PassRole 和身份管理 Deny，避免将来再次误改为
  `Deny ram:*` 而破坏 ACK。

### 8.12 ACK Plan refresh 需要完整只读 API 基线

- 阶段：ACK 控制面创建成功、服务角色已授权后的下一次 Terraform Plan。
- 现象：Plan 在刷新已有集群时调用 `cs:CheckControlPlaneLogEnable`，`TerraformPlanRole` 只有
  `cs:Describe*`，因此返回 `Forbidden.RAM`；Apply 尚未开始。
- 根因：ACK Provider 的读取路径不仅使用 `Describe*`。阿里云系统策略
  `AliyunCSReadOnlyAccess` 的 CS 只读基线还包含 `CheckServiceRole`、
  `CheckControlPlaneLogEnable`、`Get*`、`List*` 和 `Query*`。
- 处理：Plan/Apply 共用的只读观测面同步上述完整 CS 只读动作；Plan 角色仍保留
  `DenyAllMutations`，不获得 ACK 创建、修改或删除权限。
- 验收：已有 ACK 集群及节点池的 refresh 全部完成，Plan 可以进入 diff 计算；策略变更不应引入
  任何 `cs:Create*`、`cs:Update*` 或 `cs:Delete*` Allow。

### 8.13 OIDC 凭证不会自动为 aliyun CLI 设置区域

- 阶段：9 台 ECS 已创建并加入 ACK 后，首次运行数据湖集成工作流。
- 现象：`DescribeClusterUserKubeconfig` 在发出 API 请求前报 `region can't be empty`；MinIO、Spark 和
  Airflow 均尚未部署。
- 根因：GitHub OIDC Action 只导出短期 AccessKey 与 SecurityToken，不会为 aliyun CLI 配置默认区域。
  Terraform Provider 从 backend/变量获得区域，不能推断 CLI 子进程也具备相同配置。
- 处理：kubeconfig 命令显式使用 `aliyun --region "$ALIBABA_CLOUD_REGION"`，区域仍来自受控的 GitHub
  Environment 变量；增加工作流契约测试防止参数再次丢失。

### 8.14 ACK 无默认 StorageClass，且 Docker Hub 拉取不稳定

- 阶段：kubeconfig 获取成功后的首轮组件部署。
- 现象：5 个 MinIO Pod 因 PVC 未绑定保持 Pending；集群提供 `alicloud-disk-essd` 等 CSI 存储类，但
  没有默认类。同时 Docker Hub 上的 `minio/mc` 多次出现连接超时。
- 根因：MinIO `volumeClaimTemplates` 未声明 `storageClassName`；国内节点直接访问 Docker Hub 不是稳定
  的可重复实验依赖。
- 处理：MinIO PVC 显式使用 `alicloud-disk-essd`；MinIO/MC 使用官方 Quay，Spark、Airflow 与 PostgreSQL
  使用已在相同 ACK 节点完成拉取探针的国内镜像代理；Spark Operator 的 GHCR registry 同样切换到
  `ghcr.m.daocloud.io`，独立节点拉取探针约一分钟完成。部署脚本只清理“Pending 且 storageClass 为空”
  的失败测试 PVC，绝不删除 Bound 数据卷。

### 8.15 Airflow 三类 PVC 未绑定

- 阶段：MinIO 与 Spark Operator 就绪后的 Airflow Helm 安装。
- 现象：PostgreSQL、LocalExecutor Scheduler 和 Triggerer PVC 均为 Pending，事件为
  `no storage class is set`。
- 根因：这三类持久卷分别由 PostgreSQL 子 Chart、worker/LocalExecutor 和 Triggerer 参数控制，
  不能依赖 ACK 存在默认 StorageClass。
- 处理：三处均显式指定 `alicloud-disk-essd`。部署脚本仅在测试 PVC 同时满足“Pending 且
  storageClass 为空”时删除对应 StatefulSet 和 PVC 以重建；已绑定卷不受影响。

### 8.16 取消运行遗留 Helm pending-install

- 现象：Airflow 空 PVC 已安全清理，但下一次 `helm upgrade --install` 返回
  `another operation (install/upgrade/rollback) is in progress`。
- 根因：上一轮 GitHub Actions 在首次安装 Airflow 时被取消，Helm release v1 保持
  `pending-install`，后续升级无法取得 release 锁。
- 处理：部署前读取 Airflow 测试 release 状态；仅当状态为 `pending-install`、
  `pending-upgrade` 或 `pending-rollback` 时卸载该未完成 release，再执行全新安装。正常
  `deployed` release 仍走原地升级。

## 9. 当前实验状态

截至 2026-08-25：

- 第一版 3+3 拓扑的本地校验和云端 Terraform Plan 已通过：`13 add / 0 change / 0 destroy`；
- 4 CPU + 5 存储 ECS 拓扑已进入云端创建阶段；VPC 与四个 OSS Bucket 已在 Terraform state 中；
- ACK 默认服务角色已完成授权，可创建 Kubernetes 集群；
- 尚未创建 ACK/ECS，尚未产生真实 Spark/Iceberg 运行结果；
- ACK 完整生命周期权限已在第 8.11 节统一审计，策略更新后再继续剩余资源 Apply。

后续记录必须区分 `PLAN PASSED` 与 `RUNTIME PASSED`，不能把 Terraform Plan 成功写成数据湖链路已经通过。

### DL-ITEST-20260825-01

| 字段 | 本次记录 |
|---|---|
| 目标 | 建立首次云上 Spark/Iceberg 集成实验基线 |
| 合并 Commit | `6411b341716c1a12d127a51202d3403628f7442c` |
| 代码评审 | [infra PR #9](https://github.com/ZH-Kinger/infra/pull/9) |
| CI Run | [32826625793](https://github.com/ZH-Kinger/infra/actions/runs/32826625793) |
| Terraform Plan Run | [32826625799](https://github.com/ZH-Kinger/infra/actions/runs/32826625799) |
| Plan 结果 | 13 add / 0 change / 0 destroy |
| 库存检查 | 杭州 K 区六个候选规格均为 Available/WithStock |
| ACK 服务开通 | `OpenAckService(propayasgo)` 成功 |
| ACK 默认角色 | 已授权；该问题已关闭 |
| 实际基础设施 | 未创建 |
| Runtime 结果 | 未执行 |
| 结论 | BLOCKED（权限自举，不是数据链路失败） |
