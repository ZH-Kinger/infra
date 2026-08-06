# Dataset Ops Console

训练数据基础设施的管理与运维控制台。它不直接持有阿里云高权限，而是把经过服务端
校验的操作转换成仓库中已有的 GitHub Actions Workflow 请求。

## 能力

- 数据资产与版本概览；
- 已有 OSS、CPFS、PAI 数据集纳管；
- Dataset release、PAI runtime、Dataset lifecycle 与挂载审计入口；
- D1 持久化操作审计；
- 默认 plan-only，真实执行要求站点身份、管理员白名单和 GitHub Environment 审批。

## 运行配置

复制 `.env.example` 到本地环境配置。`GITHUB_TOKEN` 只由服务端读取，至少需要目标仓库
Actions workflow 的写权限；不要把 Token 提交到仓库。未配置 Token 时平台仍可生成并
保存计划，但不会触发真实 Workflow。
