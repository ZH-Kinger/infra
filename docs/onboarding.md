# 使用入门（算法同学看这篇）

这篇讲**怎么用已经发布好的数据集**。你不需要懂 Terraform，也不需要碰阿里云控制台。

用户完整操作入口见[用户手册](user-guide.md)。相关：[架构](architecture.md)｜
[存储生命周期](storage-lifecycle.md)｜[权限](permissions.md)｜[CI/CD](cicd.md)｜
[管理员手册](admin-guide.md)

---

## 一句话规矩

> 训练只能挂载 `<dataset>/<commit>/` 这样的目录。不许挂 `latest`，不许挂 Branch，不许直接读 OSS。

不是流程要求，是技术强制：训练入口的 `training-guard` 校验不过就不会启动训练。

---

## 我要一份数据集，怎么开始

### 1. 看有哪些版本

```bash
aliyun aiworkspace GET /api/v1/datasets/<DatasetId>/versions \
  --region cn-hangzhou \
  --endpoint aiworkspace.cn-hangzhou.aliyuncs.com
```

每个版本的 `SourceId` 就是 lakeFS Commit，`Labels` 里有 `lakefs_commit` 和
`manifest_sha256`。挑版本时认 Commit，不要认创建时间。

在 PAI 控制台看也行：工作空间 → 数据集 → 版本列表。

### 2. 在 DSW 里用

优先从 GitHub Actions 手动运行 **PAI runtime**，选择 `dsw`、数据集、Commit、镜像
Profile 和 `gpu-dev`。第一次保持 `execute=false` 核对挂载计划，确认后再执行并审批。
流水线会把数据集具体版本只读挂载到 `/mnt/dataset`，个人工作区读写挂载到
`/mnt/workspace`；不要在控制台另填裸 OSS/CPFS URI。

进入 DSW 后先确认：

```bash
cat /mnt/dataset/release.json
ls /mnt/dataset/_READY   # 不存在就说明这个目录不可用，不要读
```

`release.json` 告诉你这份数据到底是什么：

```json
{
  "commit_id": "6f2b7c91c2",
  "repository": "robotics-data",
  "lakefs_tag": "robotics-v2026.08.02.1",
  "manifest_sha256": "c8f5409b...",
  "paimon_snapshot_id": "1842",
  "status": "READY"
}
```

写论文或写实验记录时，记 `commit_id` 和 `manifest_sha256`，别记「8 月 2 号那版」。

### 3. 在 DLC 里跑训练

从 GitHub Actions 手动运行 **PAI runtime**，选择 `dlc`、数据集、Commit、镜像 Profile、
`gpu-training` 和训练命令。流水线依据
[`runtime-profiles.json`](../deploy/pai/runtime-profiles.json) 生成请求。三个地方不能改：

```json
"DataSources": [{
  "DataSourceId": "${PAI_DATASET_ID}",
  "DataSourceVersion": "${PAI_DATASET_VERSION}",   ← 固定版本，不是 latest
  "MountPath": "/mnt/dataset",
  "MountAccess": "RO"                              ← 只读
}]
```

训练入口先跑门禁再跑训练，见
[`deploy/pai/training-entrypoint.sh`](../deploy/pai/training-entrypoint.sh)：

```sh
dataset-sink training-guard \
  --dataset-root /mnt/dataset \
  --expected-commit "$DATASET_COMMIT" \
  --expected-manifest-sha256 "$DATASET_MANIFEST_SHA256" \
  --expected-paimon-snapshot-id "$PAIMON_SNAPSHOT_ID"

exec python /workspace/train.py --dataset /mnt/dataset --output /mnt/output
```

**输出写到另一个可写挂载**（如 `/mnt/output`），不要试图写 `/mnt/dataset`——它是只读的，
而且已发布目录不可改写。

### 4. 你实际需要记住的三个路径

| 路径 | 用途 | 权限 |
|---|---|---|
| `/mnt/dataset` | 指定 Commit 的正式训练数据 | RO |
| `/mnt/workspace` | DSW 个人工作区 | RW |
| `/mnt/output` | DLC Checkpoint 和结果 | RW |

OSS Bucket、CPFS URI、PAI Dataset ID、RAM User ID、VPC 和安全组由平台补齐，不是用户
参数。可选 `/mnt/oss-workspace` 尚未实现；即使以后提供，也只能作为临时区，不能替代
`/mnt/dataset`。

---

## 门禁报错了怎么办

`training-guard` 是 fail-closed 的：它宁可不让你训练，也不让你在错的数据上训练。

| 报错 | 含义 | 怎么办 |
|---|---|---|
| `_READY marker is missing` | 挂的目录不是一个完成的 release | 检查挂载路径是不是挂到了数据集根目录或 `.materializing` |
| `commit mismatch` | 挂的版本和作业里声明的 Commit 不一致 | 对齐 `DataSourceVersion` 和 `DATASET_COMMIT` 两处 |
| `manifest checksum mismatch` | 数据或清单被改动过 | **不要绕过**，报给数据平台，可能是发布出了问题 |
| `paimon snapshot mismatch` | 语义层坐标对不上 | 确认你要的是哪一批数据，可能拿错版本了 |

想在本地复现校验，不必等训练：

```bash
dataset-sink verify /mnt/dataset          # 快速：只校验元数据一致性
dataset-sink verify /mnt/dataset --deep   # 彻底：重算所有文件的 SHA-256（慢）
```

`--deep` 适合排查和发布门禁，不建议每个训练任务启动都跑全量。

---

## 我需要一份新数据集怎么办

你不自己发布——发布走流水线，因为它涉及写 CPFS 和注册 PAI 版本，这两件事用的是
不同的受限身份（见[权限](permissions.md)）。

流程：

1. 数据侧（Paimon/Flink）产出 `manifest.jsonl` 和 Paimon Snapshot ID。
2. 在 lakeFS 上打一个 Tag。
3. 到 Actions 里手动触发 **Dataset release** 流水线，填数据集名、Repository、Tag、
   Manifest 路径、Snapshot ID。
4. 流水线跑到 dry-run 后**停下等审批**，此时你能看到将要发出的确切请求。
5. 审批通过，版本注册完成，你就能在 PAI 里挑到它。

详见 [CI/CD](cicd.md)。

### 数据已经在 OSS，但没有 PAI Dataset 怎么办

不需要先把裸 OSS 注册为 PAI Dataset。请管理员先把 Bucket/Prefix 登记进 Terraform
`data_sources`，再用 Dataset release 的 `oss-ingest`：

```text
注册 OSS 数据源 → scan-oss → lakeFS Commit → CPFS release
→ 注册 CPFS release 为 PAI Dataset Version
```

“OSS 数据源注册”和“PAI Dataset Version 注册”是两件事，完整说明见
[存储生命周期 §2](storage-lifecycle.md#2-两种注册不要混淆)。

---

## 权限不够怎么办

先判断是哪一层拒绝了你——阿里云 RAM 和 PAI 工作空间是**两套独立**的权限：

| 现象 | 大概率原因 |
|---|---|
| 能登录 PAI，但看不到目标工作空间 | PAI 成员关系没有（或被移除） |
| 在成员列表里，但调 API 全失败 | RAM 授权没有（或被撤销） |
| 能看到数据集，但读不到文件 | CPFS Fileset / POSIX 权限，或 OSS 前缀没授权 |
| 提交 DLC 报权限错 | 你用的身份不该提交训练——训练提交是流水线的事 |

申请方式：提 PR 改 `infra/envs/<env>/access/terraform.tfvars` 里的 `pai_members`，
同时说明需要哪个角色。**不要**在控制台手工加人——那样加的权限不在代码里，
下一次 apply 会被 Terraform 收回，而且没人知道是谁加的。

角色怎么选：

| 你要做的事 | 申请角色 |
|---|---|
| 写代码、跑实验、用已有数据集 | `PAI.AlgoDeveloper` |
| 看作业状态、排查线上训练 | `PAI.AlgoOperator` |
| 只是想看看 | `PAI.WorkspaceGuest` |
| 数据标注 | `PAI.LabelManager` |

`PAI.WorkspaceAdmin` 和 `PAI.WorkspaceOwner` 不对个人开放：管理员能改成员关系，
等于能自我提权，生产环境最多 1 人（模块里有 `check` 块硬性拦截）。

---

## 不要做的事

- **不要**申请长期 AccessKey。所有云访问走 STS 临时凭证；研发用户组的策略里
  `ram:CreateAccessKey` 是显式 Deny 的。
- **不要**为了图快直接读 lakeFS 后端桶或 landing 桶。那样绕过了整个版本协议，
  而且策略里对这些桶是 Deny。
- **不要**在训练脚本里把 `--expected-commit` 之类的校验去掉。它拦住的是
  「你以为在用 A 版本，实际在用 B 版本」这类最难查的问题。
- **不要**手改 `deploy/ram/*.json`。那些是自动生成的副本，改了不影响线上权限，
  只会让 CI 失败。
