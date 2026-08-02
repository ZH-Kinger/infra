# RAM 策略副本（自动生成，请勿手改）

本目录下的 `*.json` 由 `scripts/render-ram-policies.sh` 从
`infra/modules/dataset-sink-roles/policies.tf` 渲染而来，用于**评审和审计时直接阅读**。
实际生效的策略由 Terraform 创建，不是这些文件。

手改这里不会改变任何线上权限，只会让 CI 的一致性检查失败。要改策略：

```bash
# 1. 改 infra/modules/dataset-sink-roles/policies.tf
# 2. 重新渲染
make render-ram
# 3. 两处改动放同一个 PR 提交
```

## 占位符说明

渲染时用的是 `infra/modules/dataset-sink-roles/render.tfvars` 里的固定占位符，
不是任何真实环境的值——这样渲染结果在任何机器上都逐字节一致，CI 才能用
`git diff --exit-code` 做门禁。对照表：

| 占位符 | 真实值来源 |
|---|---|
| `ACCOUNT_ID` | `infra/envs/<env>/access/terraform.tfvars` 的 `account_id` |
| `LAKEFS_BACKEND_BUCKET` | 同上的 `lakefs_backend_bucket` |
| `DATASET_BUCKET` | 同上的 `dataset_bucket` |
| `ORG/REPO` | 同上的 `github_repo` |

另外这里只渲染 `prod` 变体：dev 的策略结构完全相同，差异只有 `deny_destructive`
（dev 为 `false`，因此没有 `deny-destructive` 那一份）和名字前缀。

## 为什么很多语句是 `Resource: "*"`

不是偷懒。2026-08-02 读取阿里云官方系统策略 `AliyunPAIFullAccess` 确认：
`paidataset:*`、`paidlc:*`、`paiworkspace:*` 在官方定义中**全部**是 `Resource: "*"`，
RAM 层面无法限定到某个 Dataset、Job 或 Workspace。

所以这三类权限的收敛只能靠另外三层：

1. 收窄 Action 列表（本目录的策略在做的事）；
2. PAI Workspace 成员角色（`infra/modules/pai-workspace-access`）；
3. 流水线的 GitHub Environment 人工审批。

OSS 相关语句则都是资源级限定的，可以也应该精确到桶和前缀。

## bootstrap 层的三个 CI 角色策略不在这里

`TerraformPlanRole` / `TerraformPlatformApplyRole` / `TerraformAccessApplyRole`
的策略引用了 `data.alicloud_account`，离线求值不出来，因此没有副本。
评审时直接看 [`infra/bootstrap/oidc.tf`](../../infra/bootstrap/oidc.tf)。
