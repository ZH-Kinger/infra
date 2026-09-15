# 云权限面板：公司 IAM 登录

```
浏览器 ──HTTPS──> nginx / SLB ──> oauth2-proxy (127.0.0.1:4180) ──> 面板 (127.0.0.1:8765)
                                   公司 IAM OIDC 登录                 delivery serve --auth proxy
```

面板不处理 OIDC，只认代理注入的请求头；代理和面板之间用共享密钥确认请求确实来自代理。

## 需要 IT 提供

`cloud-panel` OIDC 应用（交接文档第 6 节）：Issuer、client_id、client_secret。
Redirect URI 填代理地址：`https://<面板域名>/oauth2/callback`。

另外要 IT 书面确认两件事：

1. **访问策略**：`cloud-panel` 绑定了「全体在职员工」。代理配置是 `--email-domain='*'`，
   不绑定策略时所有 IAM 账号（含服务账号、外部账号）都能登录面板。
2. **邮箱来源**：IAM 用户的 `email` 从飞书同步、用户自己改不了，userinfo 返回
   `email_verified: true`。如果用户能改邮箱，不要配 `DELIVERY_IAM_USERINFO_URL`，
   改为先用 `delivery identity people --directory feishu` 回填全员 union_id。
   面板要求 `email_verified` 为 true，但 IAM 通常对所有邮箱都返回 true，这一项本身证明不了什么，
   关键是用户不能改邮箱。

## 切换前检查 union_id 一致

面板现有的管理员名单、登录绑定、名册里的 union_id 都来自面板自己的飞书应用；
IAM 给的 `feishu_union_id` 来自 IAM 的飞书应用。两个应用属于同一企业时 union_id 相同，
否则切换后管理员会失去权限、已绑定的人全部冲突。

切换前拿同一个人核对一次：飞书模式登录面板看 `/api/session` 的 `union_id`，
和 IAM 里该用户的 `feishu_union_id` 属性比对，一致再切。

## 启动

```bash
# 1. 共享密钥，代理和面板用同一个值
python -c "import secrets; print(secrets.token_urlsafe(32))" > proxy_secret && chmod 600 proxy_secret

# 2. 面板：只监听本机，由代理转发
DELIVERY_AUTH=proxy \
DELIVERY_PROXY_SECRET_FILE=proxy_secret \
DELIVERY_IAM_USERINFO_URL=https://<IAM 域名>/application/o/userinfo/ \
DELIVERY_IAM_EMAIL_DOMAINS=wuji.tech \
DELIVERY_LOGOUT_URL='/oauth2/sign_out?rd=<URL 编码的 IAM 退出地址>' \
delivery serve --port 8765

# 3. oauth2-proxy：参数见 oauth2-proxy.example.yaml 顶部注释
PANEL_PROXY_SECRET="$(cat proxy_secret)" oauth2-proxy --alpha-config=oauth2-proxy.yaml ...
```

| 环境变量 | 说明 |
|---|---|
| `DELIVERY_AUTH` | `proxy`；不设或 `feishu` 为原来的飞书登录 |
| `DELIVERY_PROXY_SECRET` / `_FILE` | 共享密钥，至少 32 个 ASCII 字符，二选一 |
| `DELIVERY_IAM_USERINFO_URL` | 可选。名册里还没有某人 union_id 时，查 IAM 邮箱做首次关联 |
| `DELIVERY_IAM_EMAIL_DOMAINS` | 配了 userinfo 就必填，逗号分隔。只接受这些域名、且 `email_verified` 为 true 的邮箱 |
| `DELIVERY_LOGOUT_URL` | 可选，默认 `/oauth2/sign_out`（只清代理会话，不退出 IAM） |
| `DELIVERY_BASE_URL` | 面板对外地址，如 `https://<面板域名>`。名册审核等写操作会校验请求的 Origin，nginx 改写了 `Host` 时必须设置，否则写操作一律 403 |

## 安全要点

- 面板和代理端口都只监听本机，HTTPS 终止在前面的 nginx / SLB。密钥对不上的请求一律按未登录处理。
- 代理 `emailClaim` 指向 `feishu_union_id`：IAM 没返回 union_id 的用户建不了会话；没有邮箱的员工照常登录。
- 客户端伪造 `X-Panel-*` 请求头无效，代理会删掉客户端带的同名头再注入。
- 代理模式下管理员只按 `admins.json` 的 `union_ids` 判断，邮箱条目不生效。
- IAM 邮箱只用于名册首次关联，不用于判断权限。查 userinfo 不跟随重定向，避免 token 被带到别的地址。
- 会话最长 8 小时（`--cookie-expire=8h`）。IAM 停用某人后，他已有的会话最多还能用到过期。
- 代理模式下面板的 `/auth/*`（飞书登录、CLI 换票）全部关闭。CLI 走 IAM 另做。

## 定时刷新数据

面板读三个文件：权限快照 `identity/inventory.json`、映射提案、人员名册 `identity/people.json`。
`delivery refresh` 一次跑完三步，适合交给 systemd timer（见 `delivery-refresh.service` / `.timer`）。

```bash
delivery refresh --trust-unverified-when-derivable   # 和手动生成提案时的选项保持一致
```

| 情况 | 行为 |
|---|---|
| 全部采集成功 | 快照、提案、名册都更新；有新增高危权限、增删子账号、新的未关联账号、没能沿用 union_id 的人时发告警 |
| 某朵云采集失败 | 快照照写（面板会标出没采全），**名册和提案不重建**，发告警，退出码 1 |
| 上一份名册或比对基线存在但读不了 | 名册不重建，发告警，退出码 1 |
| 上一次还没跑完 | 跳过，退出码 75 |
| 需要告警但没配机器人 / 告警没发出去 | 退出码 1，systemd 日志里能看到 |

变化比对用单独的基线 `identity/inventory.baseline.json`，只有采集成功的平台才更新，
某朵云临时采集失败期间发生的变化，恢复后照样能报出来。

`--directory` 默认 `none`：不调飞书通讯录，从上一份名册带过来：
- union_id：邮箱唯一对上、已确认账号非空且完全没变的人才沿用；上一份里重复出现的 union_id 不沿用
- 「通讯录里多人共用这个邮箱」的标记：照带，否则这一行会变成谁先登录谁认领
- 只在通讯录里、没有云账号的人：保留

有通讯录权限时用 `--directory feishu`。多个阿里云账号用 `--aliyun-profile` 逐个列出，快照和提案都按这些账号采集。

环境变量：两朵云只读凭证、`DELIVERY_ALERT_WEBHOOK`、`DELIVERY_ALERT_SECRET`（机器人必须开签名校验）。
告警内容含云账号用户名，只发到管理员所在的内部群。

## 云账号申请（开账号、云账号权限、访问凭证）

设计与安全规则见 [docs/cloud-access-platform.md](../../docs/cloud-access-platform.md)。上线前按顺序准备：

1. **飞书审批**：在飞书审批后台建一个审批定义，表单放 4 个控件：申请单号（单行文本）、申请类型（单行文本）、
   申请内容（多行文本）、申请理由（多行文本），审批人按公司流程配。**审批人不能只有申请人本人，也不要开「审批人为空时自动通过」**：
   开通前会核对至少有一位申请人以外的审批人点了通过，否则一律不开通（`allow_self_approval: true` 才放开，不建议）。面板的飞书应用要有
   `approval:approval` 权限。查控件 ID，填进 `identity/approval.json`（格式见 `identity/approval.example.json`）：

   ```bash
   delivery approval widgets --code <审批定义编号>
   ```

   公司 IAM 登录时，oauth2-proxy 要把 `feishu_user_id` 传给面板（示例配置已包含），否则无法以员工身份发起审批。

2. **申请模板**：`identity/request-templates.json`（格式见 `identity/request-templates.example.json`）。
   员工只能从这里选，模板里写死用户组、角色和时长上限。加载时逐项校验，写错会拒绝加载。

3. **执行身份**：每个云账号一个，只给开号、加组、扮演模板角色这几个动作。阿里云策略示例见
   `executor-policy.aliyun.example.json`（把用户组和角色收窄到模板里用到的）。环境变量：

   | 平台 | 变量 |
   |---|---|
   | 阿里云 | `DELIVERY_EXEC_ALIYUN_<UID>_ACCESS_KEY_ID` / `_ACCESS_KEY_SECRET` |
   | 火山 | `DELIVERY_EXEC_VOLCANO_<账号ID>_ACCESS_KEY` / `_SECRET_KEY` |

   每次写云前会先确认凭证属于目标云账号，配错账号时直接失败（火山确认不了归属时同样失败）。
   凭证文件权限 600，只放在面板服务器上。

   注意影响范围：示例里 `CreateLoginProfile` / `UpdateLoginProfile` 作用于 `user/*`，
   执行身份泄露时可以重置任何 RAM 用户的控制台密码（包括管理员）。能统一新账号前缀时，
   把 Resource 收窄到该前缀；模板里的角色要把「最大会话时间」设到不小于模板的 `max_hours`。

   **按策略申请权限**（权限列表页）需要执行身份再有授予 / 撤销策略的权限：阿里云
   `ram:ListPoliciesForUser`、`ram:AttachPolicyToUser`、`ram:DetachPolicyFromUser`；火山
   `iam:ListAttachedUserPolicies`、`iam:AttachUserPolicy`、`iam:DetachUserPolicy`。
   **能授予策略就约等于管理员**：执行身份泄露后可以给任何子账号任何权限。服务端禁用清单
   （`identity/policy-rules.json`，见下一步）是第一道闸；阿里云还能在执行身份的策略里把可授予的
   策略收窄到列出的策略 ARN（`acs:ram:*:system:policy/<名称>`、`acs:ram:*:<UID>:policy/<名称>`），
   并对 AdministratorAccess 等写 Deny（示例已包含）。执行凭证按最高敏感度保管。

4. **资产与权限列表**：两家控制台分别开通「资源中心」，给权限快照用的只读身份加资源中心只读权限。
   权限列表要采集策略目录，只读身份再加阿里云 `ram:ListPolicies`、火山 `iam:ListPolicies`：

   ```bash
   delivery policies collect      # 写 identity/policies.json
   ```

   能申请哪些策略由 `identity/policy-rules.json` 决定（可选，格式见 `identity/policy-rules.example.json`）。
   不写时用内置规则：AdministratorAccess、RAM / IAM 完全控制、STS、资源目录、资源管理、云 SSO、账单、
   操作审计类策略不开放（这几类产品线只放只读策略，密钥管理只读也不放）；自定义策略默认**不开放**，要开放的逐条写进 `allow`，按高风险计天数。**不要**把执行身份、采集身份用的自定义策略写进 `allow`，更不要设 `allow_custom: true`——那等于让员工申请到平台自己的管理权限。规则文件只能追加禁用，放开内置禁用项要逐条写进 `allow`。
   风险决定最长授权天数（默认低 180 / 中 90 / 高 30 天）。

5. **定时任务**：在 `delivery-refresh.service` 之外再加两条（同一个 EnvironmentFile）：

   ```bash
   delivery requests sweep     # 同步飞书审批、到期回收权限、标记过期凭证、开账号后对应到名册（建议每 10 分钟）
   delivery assets collect     # 采集资产快照（每天一次）
   delivery policies collect   # 采集权限策略目录（每天一次）
   ```

   审批通过后，员工或管理员打开申请单时也会实时同步，定时任务是兜底。
   开启通知后，定时任务还会给 3 天内到期的权限发一次到期提醒。

6. **状态通知**（可选）：环境变量 `DELIVERY_NOTIFY=1`（面板和定时任务的 EnvironmentFile 都要有）。
   - **申请人**：开通完成、凭证可领取、开通失败、审批未通过 / 被撤销、即将到期、到期收回时收到飞书机器人私信，
     卡片带「查看申请」按钮。需要 `DELIVERY_FEISHU_APP_ID/SECRET` 和 `https://` 开头的 `DELIVERY_BASE_URL`；
     飞书应用要开通 `im:message:send_as_bot` 权限，**应用可用范围要覆盖全部员工**（不在范围内的人收不到）。公司 IAM 登录模式下没有 open_id，按 IAM 传来的飞书 user_id 发送（oauth2-proxy 要注入 `X-Panel-Feishu-User-Id`，否则收不到）。到期提醒只在能发给申请人时才记「已提醒」。
   - **管理员**：开通失败时发到 `DELIVERY_ALERT_WEBHOOK` 的告警群，带脱敏后的失败原因和管理后台链接。
   通知发不出去只打日志，不影响申请单；员工收到的失败消息里不带任何错误原文。
   「开通中 / 提交中」超过 30 分钟没有进展的单子，定时任务会标成失败（管理后台也可以手动处理），
   请先核对云上和飞书里的实际状态再重试或关闭。

CLI（`delivery login` 之后）：

```bash
delivery request templates
delivery request new aliyun-oss-read --user <你的子账号> --days 30 --reason "..."
delivery request list
delivery request policies --search oss
delivery request grant --account aliyun/<UID> --policy AliyunOSSReadOnlyAccess --days 30 --reason "..."
eval "$(delivery creds <申请单号>)"     # 临时凭证写进当前 shell，官方 aliyun / ve CLI 直接可用
delivery assets
```

CLI 走面板的会话令牌，目前只支持飞书登录模式；公司 IAM（代理）模式下 CLI 登录另做。

## 回退

去掉 `DELIVERY_AUTH=proxy`，重新配飞书应用环境变量，访问面板端口即可。
