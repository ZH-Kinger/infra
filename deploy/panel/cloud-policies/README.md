# 执行身份（panel-executor）的现网策略副本

2026-09-22 从云上直接导出，**以云上为准**。改策略后重新导出一份覆盖这里。

- `aliyun.wuji-panel-executor.json`：阿里主账号 1704065796538912。
- `volcano.wuji-panel-executor.json`：火山主账号 2111674479。

离职停号用到的权限：关登录、禁 / 删 AK、移出用户组、摘策略、删用户。
受保护的号（面板自己、服务号、临时凭证号）在两份策略里都有 Deny，
名单要和 `src/delivery/offboard.py` 的 `PROTECTED` 对齐，改一边就改另一边。

`identity/executor-policy.*.json` 是早期手写的参考，不在 git 里，已经和云上不一致，别照它下发。
