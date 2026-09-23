# 云权限面板：公司 IAM 登录（方案，**线上未启用**）

> **先看这一段。** 线上现在跑的是**飞书登录**（`DELIVERY_AUTH=feishu`，systemd 里也是
> `--auth feishu`），不是本文写的 IAM 登录。本文这套（oauth2-proxy + OIDC）代码和配置模板都在，
> 服务器上连共享密钥的路径都留好了，但 oauth2-proxy 没装没跑、`DELIVERY_IAM_USERINFO_URL` 是空的。
> 照着本文改配置之前先确认你是在**启用**它，而不是以为它已经在跑。
> 启用还缺 IT 给三样：Issuer、client_id、client_secret（见下一节）。

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

## 升级前先跑一遍前置检查

面板有几处「缺了就整个功能不可用、但服务照样起得来」的依赖。**在目标服务器上**跑：

```bash
python3 deploy/panel/preflight.py \
  --env /etc/delivery/panel.env --env /etc/delivery/sweep.env \
  --tickets identity/tickets.json
```

只读，不改任何东西，不打印任何密钥的值。退出码非 0 就别继续部署。它查四件事：

| 查什么 | 缺了会怎样 |
|---|---|
| `cryptography` 装没装 | 面板长期是零运行时依赖，这版起访问凭证要加密存。缺了的话凭证类申请在**提交那一刻**就被拒，而面板本身一切正常，看不出所以然 |
| `DELIVERY_BASE_URL` 有没有、是不是 https、是不是指向本机 | 凭证的查看地址靠它拼。指向本机的话，发出去的链接使用方打不开 |
| `DELIVERY_ISSUER_*` 在不在 | 长期凭证发不出去；**定时任务的环境里漏了的话，到期的子账号和密钥永远不会被删** |
| 现有申请单的状态新代码认不认得 | 停在已删除状态（`claimable` / `expired`）的单子升上去就读不出来了 |

两个 `--env` 分别给面板和定时任务：**这两边的环境经常不一样**，而漏的那一边通常是定时任务。

`/health` 页面在运行时也查前两项（「凭证交付」分组）。

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
| `DELIVERY_BASE_URL` | 面板对外地址，如 `https://<面板域名>`。三处都要它：写操作的 Origin 校验（nginx 改写了 `Host` 时不设就一律 403）、通知卡的跳转按钮、**访问凭证的查看地址**（拼不出来就一律不受理凭证申请）。定时任务的 EnvironmentFile 里也要写 |

## nginx 那一层

仓库里没有 nginx 配置示例，但有一行**必须**按这里写：

```nginx
location / {
    proxy_pass http://127.0.0.1:4180;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    # 覆盖，不是追加。默认那个 $proxy_add_x_forwarded_for 会把客户端自带的
    # X-Forwarded-For 原样保留、再往后追加，于是客户端能在左边塞任意假地址
    proxy_set_header X-Forwarded-For   $remote_addr;
}
```

面板按**固定跳数**从 X-Forwarded-For 右边数，取我们自己的代理写进去的那一个
（`DELIVERY_PROXY_HOPS`，默认 2 = nginx + oauth2-proxy）。少一层代理就设成 1。
设错的表现是「查看凭证」的台账里来源全是 `127.0.0.1`、取件限流变成全员共用一份配额。

这个值同时用于两件事：取件接口的限流分桶，和申请单里 `credential_viewed` 记的「谁看的」。

**面板必须只监听回环**（`delivery serve` 默认 `--host 127.0.0.1`）。面板判断「这个请求是不是
从我们自己的代理来的」用的是「直连对端是不是内网地址」，而内网包含整个 RFC1918 段 ——
一旦把面板绑到 LAN 地址，同网段的人就能自己造 X-Forwarded-For 冒充任意来源，
限流和「谁看过凭证」的记录一起失效。要绑 LAN 就得先把这条信任链重新想清楚。

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

   **发放访问凭证用的是另一把 AK**，同样每个云账号一个，只用来建 `tempak-*` 子账号、
   建时间窗策略、建和删密钥。和开通身份分开是因为开通身份**刻意没有**建用户密钥的权限——
   共用一把等于把那道闸拆了。

   | 平台 | 变量 |
   |---|---|
   | 阿里云 | `DELIVERY_ISSUER_ALIYUN_<UID>_ACCESS_KEY_ID` / `_ACCESS_KEY_SECRET` |
   | 火山 | `DELIVERY_ISSUER_VOLCANO_<账号ID>_ACCESS_KEY` / `_SECRET_KEY` |

   不配的话访问凭证类申请在提交时就会被拒，`/health` 体检页也会标红。**定时任务
   （`delivery requests sweep`）的 EnvironmentFile 里同样要有这两个**：到期删子账号和密钥
   走的是发放身份，缺了的话长期凭证到期不会被清掉，只剩策略里的时间窗兜着。

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

4. **资产与权限列表**：两家控制台分别开通「资源中心」，给**只读采集身份**加资源中心只读权限
   （阿里 `ResourceCenter` 只读、火山 `resourcecenter:SearchResources`）。

   **采集一定要用只读身份**，别图省事用开通身份 —— 那把 AK 能建号、能授权。线上曾经
   就是开通身份在采集，是为了加资产采集才发现的。火山那把只读身份还要有 IAM 的
   **只读**动作（`iam:ListUsers` / `ListGroups` / `ListUsersForGroup` / `ListPolicies` /
   `ListAttachedUserPolicies` / `ListAttachedUserGroupPolicies` / `ListUserGroupsForUser` /
   `GetUser` / `GetUserGroup`），否则权限快照采不全。**兜底 Deny 里不能写 `iam:*`** ——
   Deny 压过 Allow，写成通配等于把采集自己掐死（踩过）。

   **资源归属**：资源中心不告诉你一台机器是谁的（实测 6319 个资源里归属类标签一个都没有），
   所以归属由管理员在资产页上逐个指派，记在 `identity/asset-owners.json`（gitignored，0600）。
   员工在资产页只看得到**指给自己**的资源明细，其余只有数量和地域分布。
   没指派过的显示「未指定」—— **面板不按名字或创建时间去猜**，猜错一次这张表就没人信了。

   `identity/` 下会出现 `asset-owners.lock` / `policy-rules.lock` 这类空文件（0600），
   那是写归属表和规则文件时的互斥锁，别当脏文件删 —— 删了不影响正确性，但会短暂丢互斥。

   权限列表要采集策略目录，只读身份再加阿里云 `ram:ListPolicies`、火山 `iam:ListPolicies`：

   ```bash
   delivery policies collect      # 写 identity/policies.json
   ```

   能申请哪些策略由 `identity/policy-rules.json` 决定（可选，格式见 `identity/policy-rules.example.json`）。
   不写时用内置规则：AdministratorAccess、RAM / IAM 完全控制、STS、资源目录、资源管理、云 SSO、账单、
   操作审计类策略不开放（这几类产品线只放只读策略，密钥管理只读也不放）；自定义策略默认**不开放**，要开放的逐条写进 `allow`，按高风险计天数。**不要**把执行身份、采集身份用的自定义策略写进 `allow`，更不要设 `allow_custom: true`——那等于让员工申请到平台自己的管理权限。规则文件只能追加禁用，放开内置禁用项要逐条写进 `allow`。
   风险决定最长授权天数（默认低 180 / 中 90 / 高 30 天）。

   **这份规则可以在管理后台的「权限规则」页上直接改**（每条策略行上有按钮），不用上服务器。
   改动追加进 `identity/policy-rules.log`（0600，谁、何时、加了删了什么）。页面上改不动的有三样，
   都是刻意的：

   | 改不动的 | 为什么 |
   |---|---|
   | 内置禁用 | 那是代码里的地板。规则文件只能往上加，不能往下挖 —— 无论怎么改，`AdministratorAccess` 都申请不到 |
   | 平台自己的策略（`wuji-panel-*` / `wuji-oss-auto-*` / `temp-ak-auto-*`） | 放开它们＝员工能申请到平台自己的管理权限。接口直接拒 |
   | `allow_custom` | 等于「自定义策略全部放开」，而执行身份、发放身份的策略都是自定义策略。只能上服务器改文件，那是一道人肉门槛 |

   写入前先跑一遍完整校验，验不过一个字节都不落盘 —— 规则文件写坏会让整个权限列表加载失败。
   「放开一条被内置规则挡住的策略」在页面上要**手打策略名确认**。

5. **定时任务**：在 `delivery-refresh.service` 之外再加两条（同一个 EnvironmentFile）：

   ```bash
   delivery requests sweep     # 同步飞书审批、到期回收权限和凭证、开账号后对应到名册（建议每 10 分钟）
   delivery assets collect     # 采集资产快照（每天一次，用只读采集身份的环境文件）
   delivery policies collect   # 采集权限策略目录（每天一次）
   ```

   审批通过后，员工或管理员打开申请单时也会实时同步，定时任务是兜底。
   开启通知后，定时任务还会给 3 天内到期的权限发一次到期提醒。

6. **状态通知**（可选）：环境变量 `DELIVERY_NOTIFY=1`（面板和定时任务的 EnvironmentFile 都要有）。
   - **申请人**：开通完成、凭证已发放、开通失败、审批未通过 / 被撤销、即将到期、到期收回时收到飞书机器人私信，
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
# 访问凭证不在命令行里领：审批通过即签发，查看地址发在对应飞书审批的评论里
delivery assets
```

CLI 在飞书登录模式下走面板的会话令牌（`delivery login`）；公司 IAM（代理）模式下用 `delivery login --iam`，见下一节。

## CLI 登录（公司 IAM 模式）

浏览器登录走 oauth2-proxy 的 Cookie，CLI 没有 Cookie。CLI 用 OAuth 2.0 设备码授权（RFC 8628）向公司 IAM
要令牌，请求时带 `Authorization: Bearer <access_token>`；oauth2-proxy 开 `--skip-jwt-bearer-tokens`
后校验这个 JWT（签名、签发方、audience、过期），按和浏览器登录**同一套** `emailClaim` / `additionalClaims`
注入身份头。面板不发令牌、不改信任模型，仍只认代理注入的头。

员工用法：

```bash
export DELIVERY_SERVER=https://<面板域名>
export DELIVERY_IAM_ISSUER=https://<IAM 域名>/application/o/cloud-panel/
export DELIVERY_IAM_CLI_CLIENT_ID=cloud-panel
delivery login --iam          # 终端显示验证码，浏览器里用公司账号确认
delivery request list
```

令牌存 `~/.w0/session.json`（0600）。快过期时自动用 refresh_token 续期，收到 401 也会续期重试一次。

**oauth2-proxy**：在现有参数上加一个（这类参数只能写在命令行或旧版 TOML，写进 alpha YAML 会启动失败）：

```bash
  --skip-jwt-bearer-tokens=true
  # --bearer-token-login-fallback 保持默认 true：/api/ 下令牌无效或过期回 401（CLI 据此续期）
```

**需要 IT 在 IAM（Authentik）上做**，方案 A（推荐）：把现有 `cloud-panel` 应用改成支持设备码，
CLI 和浏览器用同一个客户端，令牌的 audience 一致，代理走主 provider 的校验路径、身份头和浏览器登录完全一样。

1. 告诉我们 Authentik 版本（设备码的客户端认证和错误码因版本不同）。
2. 建一个设备码流程（Stage Configuration，要求已登录），在 System > Brands 里设为 **Default code flow**。
3. `cloud-panel` provider：Grant Types 勾 **Device Code** 和 **Refresh Token**；客户端类型改 **Public**
   （2026.5 起机密客户端的设备码请求必须带 client secret，CLI 带不了）。oauth2-proxy 的 `clientSecretFile`
   仍需非空，保留占位文件即可；浏览器登录继续用 PKCE S256。
4. 配 **Signing Key**（RS256）。没配时 Authentik 用 HS256 签名，代理拿不到公钥无法校验。
5. 保持 **Include claims in id_token** 开启；给这个 provider 分配 scope 映射 `openid`、`profile`、`email`、
   `wuji`（含 `feishu_union_id`、`feishu_user_id`、`name`）和 `offline_access`。
6. **Access code validity** 调到 10 分钟左右（默认 1 分钟，来不及在浏览器里确认）。这个值同时决定浏览器登录授权码的有效期，
   浏览器登录有 PKCE S256 绑定，授权码被截获也换不到令牌。
7. 授权流程里带明确的同意页：防设备码钓鱼（员工能看到自己在授权哪个应用）。
8. 应用的访问策略绑定「全体在职员工」，设备码确认同样受它约束。
9. 用设备码登录一次，给我们一份解码后的 access_token 样例：确认有 `iss`、`aud=cloud-panel`、
   `feishu_union_id`、`feishu_user_id`、`name`。

方案 B（不改现有应用）：单独建公开客户端 `cloud-panel-cli`，两个 provider 用同一把签名密钥，
主 provider 加 `extraAudiences: [cloud-panel-cli]` 和 `insecureSkipIssuerVerification: true`。
**不要**用 `--extra-jwt-issuers`：那条路径只读 `sub/email/groups`，不读 `feishu_union_id` 等自定义声明，
身份头会是空的，面板一律按未登录处理。

令牌里没有 `feishu_union_id` 时，代理不会注入 `X-Panel-Union-Id`，面板直接按未登录处理（不会退回用 `sub` 认人）；
`delivery login --iam` 登录成功后也会提示这种情况。

## 回退

去掉 `DELIVERY_AUTH=proxy`，重新配飞书应用环境变量，访问面板端口即可。
