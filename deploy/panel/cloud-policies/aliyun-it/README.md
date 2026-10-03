# 阿里云信息技术部账号（1339279783371949）面板策略

从现网 1704065796538912 的面板策略复制，已将账号 UID 替换为 1339279783371949。

在第二个阿里云主账号中导入：

- `aliyun-it.wuji-panel-collector.json` 绑定 RAM 用户 `panel-collector`
- `aliyun-it.wuji-panel-executor.json` 绑定 RAM 用户 `panel-executor`
- `aliyun-it.wuji-panel-issuer.json` 绑定 RAM 用户 `panel-issuer`
- `role-panel-oss-list.policy.json` 创建角色 `panel-oss-list`
- `role-panel-oss-download.policy.json` 创建角色 `panel-oss-download`
- `role-panel-oss-write.policy.json` 创建角色 `panel-oss-write`
- `role-panel-oss.trust.json` 作为以上三个角色的信任策略

角色和用户名称必须保持一致。导入前请确认资源 ARN 中的 UID 已是 1339279783371949。
