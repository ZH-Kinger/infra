# lakeFS → CPFS Dataset Sink

把 lakeFS 的不可变 Commit 沉降成 CPFS 上可供阿里云 PAI DSW/DLC 只读挂载的数据集版本，
并用 Terraform + GitHub Actions 交付这套系统的基础设施与权限。

这个项目解决的是**发布边界**，不是用 CPFS 替代 lakeFS：

```text
  存量数据已在 OSS      CPFS 上处理完的新数据        已在 lakeFS 的数据
  scan-oss → commit     scan → archive → commit        （已有 Commit）
  （字节一个不动）       （唯一一次数据搬运）
          ↓                       ↓                          ↓
              lakeFS Commit + Paimon Snapshot + Manifest
                             ↓
                 certify（零拷贝）/ materialize（拷贝）
                             ↓
                    大小 / SHA-256 完整性校验
                             ↓
             <cpfs-root>/<dataset>/<commit>/_READY
                             ↓
                   PAI Dataset Version 注册
                             ↓
              DSW / DLC 只读挂载 + 启动门禁
```

存量数据**不需要迁移**：lakeFS import 是零拷贝的，只记录对象的物理地址。代价是那些
前缀从此变成只读区——删掉对象会让 Commit 悬空，而且当时不会报错。

对象存储是**冷归档与版本真相的物理载体**，CPFS 上的 release 是**为训练速度存在的
热副本**——可以随时淘汰，需要时重新沉降回来。

要立的规矩只有一条：

> **任何投喂给训练的数据集，必须对应 lakeFS 上的一个 Commit Hash；
> 严禁裸读 OSS，严禁挂载可变 Branch 或 `latest`。**

`training-guard` 让这条规矩成为技术强制点，而不是流程约定——校验不过，训练不启动。

---

## 文档

| 你是谁 / 想干什么 | 看这篇 |
|---|---|
| 想用已发布的数据集训练 | [使用入门](docs/onboarding.md) |
| 想理解整体设计和取舍 | [整体架构](docs/architecture.md) |
| 关心权限怎么隔离、怎么防提权 | [权限模型](docs/permissions.md) |
| 关心流水线怎么跑、审批在哪 | [CI/CD](docs/cicd.md) |
| 要初始化环境或排查故障 | [运维手册](docs/runbook.md) |
| 要改这个仓库的代码 | [仓库约定](AGENTS.md) |

---

## 快速开始

不装任何第三方包即可跑通全部本地验证：

```bash
make test    # 57 个单元测试，离线、无需云凭证
make e2e     # 全链路演练：三条入口 → 深度校验 → 训练门禁 → 回收 → PAI 请求
make help    # 全部可用目标
```

`make e2e` 在临时 POSIX 目录里模拟 CPFS，不连接阿里云，不产生费用。

---

## 命令一览

```bash
# 存量数据已在 OSS：列举 + 算 SHA-256，然后零拷贝 import 建 Commit（不搬字节）
dataset-sink scan-oss --bucket legacy-data \
  --endpoint-url https://oss-cn-hangzhou.aliyuncs.com \
  --prefix legacy/robotics --destination datasets/robotics \
  --output /work/manifest.jsonl

dataset-sink commit --repository robotics-data --branch main \
  --object-store-uri s3://legacy-data \
  --prefix legacy/robotics --destination datasets/robotics \
  --manifest /work/manifest.jsonl --tag robotics-v2026.08.02.1

# CPFS 上处理完的新数据接入版本体系：扫描 → 归档 → 建 Commit
dataset-sink scan /mnt/cpfs/staging/batch-001 --output /work/manifest.jsonl

dataset-sink archive /mnt/cpfs/staging/batch-001 \
  --manifest /work/manifest.jsonl --prefix staging/batch-001 \
  --target oss --bucket dataset-sink-archive \
  --endpoint-url https://oss-cn-hangzhou.aliyuncs.com

dataset-sink commit --repository robotics-data --branch main \
  --object-store-uri s3://dataset-sink-archive \
  --prefix staging/batch-001 --destination datasets/robotics \
  --manifest /work/manifest.jsonl --tag robotics-v2026.08.02.1

# 从 lakeFS 沉降并发布
dataset-sink materialize --dataset robotics --repository robotics-data \
  --ref robotics-v2026.08.02.1 --manifest /work/manifest.jsonl \
  --source lakefs-s3 --target-root /mnt/cpfs/datasets --workers 32

# 数据已在 CPFS Staging：零复制原子发布（同文件系统 rename，秒级）
dataset-sink certify --prepared-dir /mnt/cpfs/staging/batch-001 \
  --target-root /mnt/cpfs/datasets --dataset robotics \
  --repository robotics-data --commit 6f2b7c91c2 \
  --source-reference robotics-v2026.08.02.1 --manifest /work/manifest.jsonl

# 回收不再需要的 CPFS release（默认 dry-run；删除前核对 Commit 是否仍在 lakeFS）
dataset-sink reclaim /mnt/cpfs/datasets \
  --lakefs-api-endpoint https://lakefs.internal --min-age-days 14 --keep-last 2

# 校验（--deep 重算全部 SHA-256）
dataset-sink verify /mnt/cpfs/datasets/robotics/6f2b7c91c2 --deep

# 生成 PAI 注册请求（纯本地计算，不需要云权限）
dataset-sink pai-request /mnt/cpfs/datasets/robotics/6f2b7c91c2 \
  --dataset-id d-example --region cn-hangzhou \
  --filesystem-id cpfs-example \
  --filesystem-path /datasets/robotics/6f2b7c91c2 \
  --uri nas://cpfs-example.cn-hangzhou/datasets/robotics/6f2b7c91c2/

# 注册（默认 dry-run，--execute 才真正写入，且按 Commit 幂等查重）
dataset-sink register-pai /work/pai-request.json --region cn-hangzhou --execute

# 训练容器内的启动门禁
dataset-sink training-guard --dataset-root /mnt/dataset \
  --expected-commit "$DATASET_COMMIT" \
  --expected-manifest-sha256 "$DATASET_MANIFEST_SHA256"
```

两处坐标系容易混：

- `release_dir`（执行机上的挂载视角）与 `--filesystem-path`（CPFS 文件系统内部视角）；
- manifest 的 `target_path`（release 内路径）与 `source_key`（**Commit 内**路径）——
  所以 `scan-oss` 和 `commit` 的 `--destination` 必须填同一个值，填错会让
  `materialize` 全量 404。`commit` 会在建 Commit 之前拦下这种情况。

连接真实 lakeFS 需要 `pip install -e '.[all]'` 并注入
`LAKEFS_API_ENDPOINT` / `LAKEFS_S3_ENDPOINT` / `LAKEFS_ACCESS_KEY_ID` /
`LAKEFS_SECRET_ACCESS_KEY`。程序本身不保存任何凭证。

---

## 发布结果与协议

```text
/mnt/cpfs/datasets/
├── .locks/                       # 进程锁
├── .materializing/               # 未完成的沉降只存在于此
├── .trash/                       # 回收时先原子改名到这里，再慢慢删
└── robotics/
    └── 6f2b7c91c2/               # 目录名就是 lakeFS Commit，不可变
        ├── shards/
        ├── manifest.jsonl
        ├── release.json          # commit / manifest_sha256 / paimon_snapshot_id
        ├── _READY                # 最后写入；没有它的目录一律视为不可用
        └── .keep                 # 可选：人工置顶，reclaim 永不触碰
```

CPFS 上的 release 是**唯一被设计成可删的一层**。`reclaim` 删除前必须确认「删了能
重建」——核对 Commit 是否仍在 lakeFS，确认不了就一律保留。宁可漏删，不可错删。

同一 Commit 重复沉降是幂等 no-op；同一 Commit 携带不同 Manifest 会报
`ReleaseConflictError` 而不是覆盖。PAI 只能挂载 `<dataset>/<commit>/`。

---

## 目录结构

```
src/dataset_sink/   Python 逻辑（零运行时依赖，lakeFS/boto3 在 optional extras）
tests/unit/         离线单元测试
tests/integration/  需真实环境，缺环境变量时 skip
infra/bootstrap/    本地 state：state 后端 + OIDC 信任锚 + 三个 CI 角色
infra/modules/      ci-oidc-role / dataset-sink-roles / pai-workspace-access
infra/envs/         dev|prod × platform|access，四套独立 state
deploy/ram/         RAM 策略副本（自动生成，勿手改）
deploy/pai/         DLC 作业模板与训练入口
scripts/            本地演练、策略渲染、只读探测
docs/               架构 / 权限 / CI/CD / 运维 / 使用
```

---

## 身份隔离摘要

沉降、注册、训练是**三个不同的信任级别**，任何单个身份泄露都不足以完成一次完整的
数据污染：

| 身份 | 能做 | 明确不能做 |
|---|---|---|
| Materializer | 读 lakeFS 固定 Commit、写 CPFS release | 注册 PAI 版本、提交训练 |
| Register | 注册 Dataset Version | 读 lakeFS 后端、提交训练 |
| DlcSubmit | 提交绑定已审批版本的 DLC Job | 改写数据版本、读 lakeFS 后端 |
| TrainingRuntime | 只读已发布归档、写自己的输出 | 读 lakeFS 后端与 staging |
| 研发用户组 | 使用已发布版本 | 取长期密钥、改写发布物 |

CI 侧全部走 GitHub OIDC → RAM 角色 → STS 临时凭证，阿里云侧**没有任何长期
AccessKey**。完整模型（四套授权面、防提权设计、平台硬限制）见
[权限模型](docs/permissions.md)。

---

## 接入真实环境需要什么

本地可以完成全部逻辑与测试；联调必须在与 CPFS/PAI 网络相通的环境进行。
只需提供资源标识和临时授权方式，**不要发送 AK/SK**：

- Region、PAI Workspace ID、Dataset ID、DLC Resource/Quota ID
- CPFS/BMCPFS 文件系统 ID、文件系统内部路径、VPC 挂载点、PAI 所在 VPC/vSwitch
- lakeFS 内网 API/S3 Gateway 地址、测试 Repository 与 Tag（凭证走 Secret 注入）
- Paimon 的真实 Manifest 样例与 Snapshot ID
- 能挂载 CPFS 的自托管 runner 或 ACK Job 环境

在目标账号执行 `make discover` 可以自动探测大部分 ID 并生成 tfvars 草稿。
当前已知阻塞项与解除方式见[运维手册](docs/runbook.md)第 0 节。
