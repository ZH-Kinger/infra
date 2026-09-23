# 执行身份（panel-executor）的现网策略副本

2026-09-22 从云上直接导出，**以云上为准**。改策略后重新导出一份覆盖这里。

- `aliyun.wuji-panel-executor.json`：阿里主账号 1704065796538912（开通身份）。
- `volcano.wuji-panel-executor.json`：火山主账号 2111674479（开通身份）。
- `aliyun.wuji-panel-issuer.json`：阿里发放身份（发长期凭证用）。
- `volcano.wuji-panel-issuer.json`：火山发放身份。**火山的每一张凭证都走长期路径**
  （没有 STS 兜底），所以这份的前缀漏改一次，火山凭证就是 100% 发不出来。

导出于 2026-09-23。

离职停号用到的权限：关登录、禁 / 删 AK、移出用户组、摘策略、删用户。
受保护的号（面板自己、服务号、临时凭证号）在两份策略里都有 Deny，
名单要和 `src/delivery/offboard.py` 的 `PROTECTED` 对齐，改一边就改另一边。

**子账号前缀改名时要同时动四处**：`grants.ISSUED_PREFIXES`（代码里唯一一份前缀表）、
两份 issuer 策略的 Allow（不改就发不出凭证）、两份 executor 策略的 Deny（不改就丢掉保护）。
2026-09-23 把内部凭证从 `tempak-` 改成 `staff-` 时，后两处都漏了 —— 审计才抓出来。

`identity/executor-policy.*.json` 是早期手写的参考，不在 git 里，已经和云上不一致，别照它下发。
