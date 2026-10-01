# Shannon / GLM-5.3 安全回归报告

日期：2026-09-30  
项目：云权限面板（`infra`）  
执行环境：本地隔离副本，未连接生产数据和生产凭证

## 本轮修复

1. 面板代理鉴权增加可选的身份二次校验。开启
   `DELIVERY_PROXY_VERIFY_IDENTITY=1` 后，代理注入的 `X-Panel-Union-Id` 必须和
   `X-Panel-Access-Token` 对应 IAM userinfo 返回的 `feishu_union_id` 一致；缺少令牌、
   令牌属于其他用户或 IAM 校验失败都会按未登录处理。
2. GitHub Actions 的 `workflow_dispatch` / `workflow_call` 输入不再直接插入 `run:`
   shell。输入先进入工作流级 `INPUT_*` 环境变量，再由双引号包裹的 shell 展开，避免
   引号、空格和 shell 元字符改变命令结构。
3. 工作流新增回归检查，覆盖“run 块不得出现 `${{ inputs.* }}`”以及环境变量引用必须
   保持双引号两个条件。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 代理鉴权单元测试 | 38 passed |
| 后端回归测试 | 149 passed |
| 后端参数化子测试 | 46 passed |
| 前端测试 | 121 passed |
| 工作流 shell 注入回归测试 | 3 passed |
| GitHub Actions YAML 解析 | 两个工作流均通过 PyYAML 解析 |
| Ruff | 通过 |
| `git diff --check` | 通过 |

代理行为也做了黑盒确认：匿名访问管理接口返回 `401`，带测试身份且通过 IAM
userinfo 对账返回 `200`；不匹配的 union id/token 组合被拒绝。

## Shannon 扫描状态

Shannon 使用 Bailian 的 GLM-5.3，在隔离副本上运行，目标是动态本地面板。此前的
预扫描曾发现一条 GitHub Actions 输入进入 shell 的候选链路；该链路已按上面的方式
修复，并由 3 个回归测试覆盖。

本轮 Shannon worker 已完成侦察阶段并生成
`pre_recon_deliverable.md`，但在进入后续验证时出现工具侧的 `set_authentication`
载荷重试，当前没有生成完整的最终 exploit 报告。因此本文件不把预扫描候选标成已
确认漏洞，也不声称 Shannon 的完整攻击链已经通过。面板自身的鉴权和回归测试结果
是已完成、可复核的结果；完整 Shannon 报告需要重新启动一次不依赖该失败步骤的扫描。

## 后续复核结果

### 门户操作接口管理员边界

已修复并回归验证。`GET /api/operations` 和 `POST /api/operations` 现在都要求
ChatGPT 登录身份，并且邮箱必须命中 `OPS_ADMIN_EMAILS`；未登录返回 401，普通员工
返回 403，均不会读取或写入 D1，也不会触发 GitHub Actions。写请求还要求 JSON
Content-Type 和同源 Origin，`execute` 必须是真正的布尔值。门户授权回归测试 6/6
通过。

### 生产登录 cookie

已修复并回归验证。当前生产实际使用的是飞书登录（`DELIVERY_AUTH=feishu`，
`DELIVERY_BASE_URL=https://cloud.wuji-tech.com`），面板现在根据受信任的 HTTPS
配置给登录和退出 cookie 加 `Secure`，同时保留 `HttpOnly`、`SameSite=Lax` 和 8 小时
有效期。oauth2-proxy 模板也显式要求 `Secure`、`HttpOnly` 和 `SameSite=Lax`；线上
当前没有运行 oauth2-proxy，因此模板不是线上生效配置。HTTPS 配置路径的 cookie 测试通过。

### 同机备份保护

已加强作业隔离并完成恢复演练：备份脚本显式生成 0600 文件，拒绝符号链接备份目录；
systemd 模板改为 root 持有备份目录，并限制网络、设备、内核参数和写入路径，面板的
`delivery` 用户不能删除历史副本。测试确认锁文件不进入归档，JSON 可完整恢复。
线上只读核验时目录仍是旧配置（`delivery:delivery`），需要部署这个 unit 后才会切换。
备份仍是同机快照，磁盘被整体取得时需要另行配置异地加密副本。

### 外部服务令牌轮换

已补上配置门禁并验证现有轮换流程：令牌文件不是 0600 时面板直接拒绝使用；令牌按
服务隔离，使用常量时间比较；轮换支持“新旧并存 → 切换网关 → 删除旧令牌”，按文件
mtime 热加载，不需要重启。服务令牌接口测试 47 个、57 个子测试通过。

本轮组合回归结果：面板、部署安全、令牌和工作流测试共 129 个、57 个子测试通过，
门户授权测试 6 个通过，YAML 解析、Ruff 和 `git diff --check` 通过。Shannon worker 的侦察阶段已经
完成，但后续仍因工具侧 `set_authentication` 载荷错误中断；完整攻击链扫描需要在
Shannon 修复该工具错误后，针对当前副本重新运行。

门户目录当前没有安装 `node_modules`，因此没有伪报 `npm test` 的完整构建结果；本轮
已直接运行两组 Node 授权回归测试（6/6）和静态页面测试（2/2）。安装依赖后应再补跑
门户的正式 build/lint。

## 部署核验

2026-09-30 17:25（Asia/Shanghai）已将面板 `src/`、备份脚本和
`delivery-backup.service` 部署到 `120.79.167.166`。部署过程没有同步 `identity/`。
线上 `cloud-panel` 已重启并保持 active，`/healthz` 返回 `{"ok": true}`；线上代码已
确认包含 HTTPS cookie、服务令牌权限检查和代理身份校验。备份 unit 已切换为 root、
定时器保持 active，手动运行成功并生成新的 root:root、0600 快照，保留 48 份。
