## 改了什么

<!-- 一两句说明意图，不要罗列文件列表 -->

## 为什么

<!-- 触发这次改动的问题或需求 -->

## 变更类型

- [ ] Python 逻辑（`src/`）
- [ ] Terraform 基础设施（`infra/envs/*/platform/`）
- [ ] **权限**（`infra/envs/*/access/`、`infra/modules/*-roles/`、`deploy/ram/`）
- [ ] 流水线（`.github/workflows/`）
- [ ] 文档（`docs/`、`README.md`）

## 验证

- [ ] `make test` 通过
- [ ] `make lint` 通过
- [ ] `make e2e` 通过（涉及沉降/发布逻辑时必填）
- [ ] `make tf-validate` 通过（涉及 `infra/` 时必填）
- [ ] `deploy/ram/` 与 Terraform 渲染结果一致（改了策略时必填）

## 权限变更专用（非权限变更请删除本节）

- [ ] Plan 输出已附在下方，明确列出**新增了谁、移除了谁、授予/收回了哪些 Action**
- [ ] 遵循最小权限：没有新增 `AdministratorAccess` / `Aliyun*FullAccess` / `PAI.WorkspaceOwner`
- [ ] 没有让任何 Apply Role 获得修改自身 Policy 的能力
- [ ] 生产环境的删除类 Action 仍被显式 Deny 覆盖
- [ ] 已确认对应的 PAI Workspace 成员关系与 RAM 授权同步变更

```
<!-- 粘贴 terraform plan 的关键片段 -->
```

## 风险与回滚

<!-- 出问题怎么回滚；是否涉及不可逆操作（资源删除/替换） -->

## 自检

- [ ] 没有提交任何 AccessKey / SecretKey / lakeFS 凭证
- [ ] 没有提交 `*.tfstate`、`terraform.tfvars`、`backend.hcl`
- [ ] 没有引入新的运行时第三方依赖
