# lakeFS → CPFS Dataset Sink

把 lakeFS 的不可变 Commit 沉降成 CPFS 上可供阿里云 PAI DSW/DLC 只读挂载的数据集版本。

这个程序解决的是发布边界，而不是用 CPFS 代替 lakeFS：

```text
lakeFS Commit + Paimon Snapshot + JSONL Manifest
                    ↓
        CPFS .materializing 临时目录
                    ↓
         大小/SHA-256 完整性校验
                    ↓
    <cpfs-root>/<dataset>/<commit>/_READY
                    ↓
          PAI Dataset Version 请求
```

## 当前包含

- lakeFS Tag/Branch 到 Commit ID 的解析。
- 通过 lakeFS S3 Gateway 读取固定 Commit。
- 本地源适配器，方便开发和测试。
- 并行写入 CPFS 临时目录。
- CPFS-first 场景下校验 Staging 后零复制原子发布。
- 文件大小和 SHA-256 校验。
- 同一 Commit 幂等、不同 Manifest 禁止覆盖。
- CPFS 上的进程锁和原子目录发布。
- `release.json`、`manifest.jsonl` 和 `_READY` 发布协议。
- 生成并通过阿里云 CLI 注册 PAI `CreateDatasetVersion`，默认仅 dry-run。
- DLC/DSW 训练启动门禁：校验 Commit、Manifest checksum 和 Paimon Snapshot。
- DLC 只读数据集挂载、RAM 最小权限及本地 E2E 示例。

程序不保存阿里云凭证。生产环境由 CI/CD 使用 RAM Role 的临时身份执行阿里云 CLI；不要把 AccessKey 写入仓库、镜像或聊天记录。

## Manifest 格式

Manifest 是 JSONL，每行一个源对象：

```json
{"source_key":"raw/episode-000001.tar","target_path":"shards/train-000001.tar","size_bytes":1073741824,"sha256":"<64 hex chars>"}
```

字段：

- `source_key`：lakeFS Repository 内的逻辑对象路径。
- `target_path`：CPFS Release 内的相对路径。
- `size_bytes`：可选但生产环境建议必填。
- `sha256`：可选但生产环境建议必填。

Paimon/Flink 导出任务负责根据动作类型、质量分和标注状态生成该 Manifest，并把对应的 Paimon Snapshot ID 传给沉降任务。

## 本地验证

不安装任何第三方包即可执行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

本地模拟沉降：

```bash
mkdir -p /tmp/dataset-source/raw
printf data > /tmp/dataset-source/raw/sample.bin

PYTHONPATH=src python3 -m dataset_sink.cli materialize \
  --dataset robotics \
  --repository robotics-data \
  --commit 6f2b7c91c2 \
  --manifest examples/manifest.jsonl \
  --source local \
  --local-source-root /tmp/dataset-source \
  --target-root /tmp/cpfs
```

验证 Release：

```bash
PYTHONPATH=src python3 -m dataset_sink.cli verify \
  /tmp/cpfs/robotics/6f2b7c91c2 --deep
```

默认验证只检查 `release.json`、Manifest 和 `_READY` 的一致性；`--deep` 会重新读取并哈希所有数据文件，适合发布门禁或抽样调度，不建议每个训练任务启动时执行全量深度校验。

## 连接 lakeFS

安装可选依赖：

```bash
python -m pip install -e '.[all]'
```

通过环境变量注入 lakeFS 凭证：

```bash
export LAKEFS_API_ENDPOINT=https://lakefs.internal
export LAKEFS_S3_ENDPOINT=https://s3.lakefs.internal
export LAKEFS_ACCESS_KEY_ID='<runtime secret>'
export LAKEFS_SECRET_ACCESS_KEY='<runtime secret>'
```

使用 Tag 触发沉降：

```bash
dataset-sink materialize \
  --dataset robotics \
  --repository robotics-data \
  --ref robotics-v2026.08.02.1 \
  --lakefs-tag robotics-v2026.08.02.1 \
  --paimon-snapshot-id 1842 \
  --manifest /work/manifest.jsonl \
  --source lakefs-s3 \
  --target-root /mnt/cpfs/datasets \
  --workers 32
```

程序会先解析 Tag，后续所有读取都使用 Commit ID，而不会继续读取可变 Branch。

## 数据已经在 CPFS：零复制发布

如果采集和预处理结果已经写入 CPFS Staging，目录内部应直接采用 Manifest 的 `target_path` 布局：

```text
/mnt/cpfs/staging/batch-20260802-001/
└── shards/
    └── train-000000.bin
```

完成 OSS 归档和 lakeFS Commit 后执行：

```bash
dataset-sink certify \
  --prepared-dir /mnt/cpfs/staging/batch-20260802-001 \
  --target-root /mnt/cpfs/datasets \
  --dataset robotics \
  --repository robotics-data \
  --source-reference robotics-v2026.08.02.1 \
  --commit 6f2b7c91c2 \
  --lakefs-tag robotics-v2026.08.02.1 \
  --paimon-snapshot-id 1842 \
  --manifest /work/manifest.jsonl
```

该命令会全量检查 Staging 中的文件集合、大小和 SHA-256，然后在同一 CPFS 文件系统内通过目录 rename 发布，不会再从 OSS 读取或复制数据。发布后原 Staging 目录不再存在。

## CPFS 发布结果

```text
/mnt/cpfs/datasets/
├── .locks/
├── .materializing/
└── robotics/
    └── 6f2b7c91c2/
        ├── manifest.jsonl
        ├── release.json
        ├── shards/
        └── _READY
```

PAI 只能挂载 `robotics/6f2b7c91c2/`，不能挂载 `.materializing`、`latest` 或 lakeFS Branch 名称。

## 生成 PAI Dataset Version 请求

CPFS 在执行沉降程序的机器上可能挂载到 `/mnt/cpfs`，但 PAI OpenAPI 需要 CPFS 文件系统内部路径，所以两者分开传递：

```bash
dataset-sink pai-request \
  /mnt/cpfs/datasets/robotics/6f2b7c91c2 \
  --dataset-id d-example \
  --region cn-hangzhou \
  --filesystem-id cpfs-example \
  --filesystem-path /datasets/robotics/6f2b7c91c2 \
  --uri nas://cpfs-example.cn-hangzhou/datasets/robotics/6f2b7c91c2/ \
  --output /work/pai-request.json
```

输出中包含：

- `POST /api/v1/datasets/{DatasetId}/versions`
- `DataSourceType=CPFS`
- `SourceId=<lakeFS commit>`
- `lakefs_commit` 和 `manifest_sha256` 标签
- CPFS `ImportInfo`

CI/CD 再使用独立的 `DatasetRegisterRole` 调用 PAI OpenAPI。该角色只需要 `paidataset:CreateDatasetVersion`，不需要裸 OSS 读取权限。

先 dry-run 检查阿里云 CLI 的最终请求：

```bash
dataset-sink register-pai /work/pai-request.json \
  --region cn-hangzhou \
  --profile dataset-register
```

审批通过后才真正注册；执行前会按 lakeFS Commit 查询已有版本，保证幂等，并拒绝相同 Commit 对应不同 Manifest：

```bash
dataset-sink register-pai /work/pai-request.json \
  --region cn-hangzhou \
  --profile dataset-register \
  --execute
```

最小 RAM Policy 见 [`deploy/ram/dataset-register-policy.json`](deploy/ram/dataset-register-policy.json)。由于当前 PAI OpenAPI 对这两个 Action 标记为 All Resource，RAM Policy 本身不能进一步限定 Dataset ID；还需要通过 PAI Workspace 成员角色、独立 CI Role 和流水线环境审批做第二层约束。

## PAI 训练启动约束

DSW/DLC 挂载 CPFS Dataset Version 后，训练入口执行：

```bash
dataset-sink training-guard \
  --dataset-root /mnt/dataset \
  --expected-commit "$DATASET_COMMIT" \
  --expected-manifest-sha256 "$DATASET_MANIFEST_SHA256" \
  --expected-paimon-snapshot-id "$PAIMON_SNAPSHOT_ID"
```

完整启动脚本见 [`examples/pai/training-entrypoint.sh`](examples/pai/training-entrypoint.sh)，DLC `CreateJob` 请求骨架见 [`examples/pai/dlc-create-job.template.json`](examples/pai/dlc-create-job.template.json)。其中 `DataSourceId + DataSourceVersion` 固定数据版本、`MountAccess=RO`，挂载到 `/mnt/dataset`；输出目录使用另一个可写挂载。

训练 Runtime Role 不授予 `oss:GetObject` 到 Landing Bucket 或 lakeFS 后端 Bucket。CPFS 的实际可见范围还要由 PAI 的存储权限资源组和 CPFS Fileset/POSIX 权限约束；RAM 只负责“能否提交作业”，不能替代文件系统权限。

## 身份与权限隔离

| 身份 | 能做什么 | 明确不能做什么 |
|---|---|---|
| `DatasetMaterializerRole` | 读取 lakeFS Gateway 的固定 Commit；写 CPFS staging/release | 注册 PAI 版本、提交 GPU 训练、覆盖已发布目录 |
| `DatasetRegisterRole` | `ListDatasetVersions`、`CreateDatasetVersion` | 读取裸 OSS、写 CPFS、提交 DLC |
| `DlcSubmitRole` | 提交绑定已审批 Dataset Version 的 DLC Job | 改写数据版本、读取 lakeFS/OSS；Policy 见 [`deploy/ram/dlc-submit-policy.json`](deploy/ram/dlc-submit-policy.json) |
| `TrainingRuntimeRole` | 只读挂载某个 CPFS release；写独立 output/checkpoint | 访问 landing/lakeFS 后端、写训练集 |
| 研发 RAM 用户 | 在 PAI Workspace 内使用已发布版本 | 获取长期 lakeFS/OSS 密钥、直接改生产 CPFS release |

lakeFS Repository 权限、PAI Workspace 权限和阿里云 RAM 是三套不同的授权面，不能互相替代。CI 中使用短期 STS/OIDC 或可信运行时身份；lakeFS 凭证放 Secret 管理服务并只注入沉降任务。

## 本地端到端演练

```bash
make test
make e2e
```

`make e2e` 会在临时 POSIX 目录中模拟 CPFS，依次完成沉降、深度校验、训练门禁和 PAI 请求生成，不会连接阿里云，也不会产生云资源费用。

## 生产接入顺序

1. Paimon/Flink 导出带 checksum 的 JSONL Manifest。
2. 在 ACK 或 PAI CPU Job 中挂载 CPFS，执行 `dataset-sink materialize`。
3. 使用只读 lakeFS Credential，仅允许读取指定 Repository。
4. 沉降成功后生成 PAI Dataset Version 请求。
5. CI/CD 使用独立 RAM Role 注册版本。
6. DSW/DLC 只读挂载该版本，并校验 `_READY` 和 Commit ID。

## 接入真实环境所需信息

本地可以完成逻辑和测试；联调必须在与 CPFS/PAI 网络相通的测试环境运行。只需要提供资源标识和临时授权方式，不要发送 AK/SK：

- Region、PAI Workspace ID、现有 Dataset ID、DLC Resource/Quota ID。
- CPFS/BMCPFS 文件系统 ID、文件系统内部 release 根路径、VPC 挂载点，以及 PAI 所在 VPC/vSwitch。
- lakeFS 内网 API/S3 Gateway 地址、测试 Repository 和一个测试 Tag；凭证通过 Secret/环境注入。
- Paimon Manifest 的真实导出样例与 Snapshot ID。
- CI 平台及它如何扮演 `DatasetMaterializerRole`、`DatasetRegisterRole`、`DlcSubmitRole`。

优先在非生产 Repository、非生产 CPFS Fileset 和独立 PAI Dataset 中跑首轮联调。
