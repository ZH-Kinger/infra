# 阿里云 RAM「用户 SSO」开关语义 — 取证核实

调研人：researcher　日期：2026-09-16　对象：**用户 SSO（User-based SSO，NameID 匹配到具体 RAM 用户名）**，不是角色 SSO。
用途：生产变更决策。每条结论标可信度：`【文档】`=官方明说并附原文；`【推理】`=由文档原文推导、文档未逐字说；`【拿不准】`=文档未覆盖，需另行取证。

---

## 0. 主证据页（全文我已用 curl 抓到服务端渲染正文，非搜索摘要）

| # | 标题 | URL |
|---|------|-----|
| D1 | 进行用户SSO时阿里云SP的SAML配置（中文） | https://help.aliyun.com/zh/ram/configure-alibaba-cloud-saml-settings-for-role-based-sso （旧地址同文：https://help.aliyun.com/document_detail/93685.html ） |
| D2 | Configure SAML on Alibaba Cloud for user SSO（英文，Last Updated: Jul 17, 2026） | https://www.alibabacloud.com/help/en/ram/user-guide/configure-alibaba-cloud-saml-settings-for-role-based-sso |
| D3 | SSO概览（含「SSO 方式比较」表） | https://help.aliyun.com/zh/ram/user-guide/sso-overview |
| D4 | 使用 Microsoft Entra ID 进行用户 SSO 的示例（步骤六有锁死警告） | https://help.aliyun.com/zh/ram/implement-user-based-sso-by-using-azure-ad |
| D5 | 管理 RAM 用户登录设置 | https://help.aliyun.com/zh/ram/manage-console-logon-settings-for-a-ram-user-1 |
| D6 | SetUserSsoSettings - 设置用户SSO身份提供商信息 | https://help.aliyun.com/zh/ram/developer-reference/api-ims-2019-08-15-setuserssosettings |
| D7 | GetUserSsoSettings - 查询用户SSO身份提供商信息 | https://help.aliyun.com/zh/ram/developer-reference/api-ims-2019-08-15-getuserssosettings |
| D8 | 单点登录（SSO）常见问题 | https://help.aliyun.com/zh/ram/support/faq-about-sso |
| D9 | 用户SSO的SAML响应 | https://help.aliyun.com/zh/ram/user-guide/saml-response-for-user-based-sso |
| D10 | 什么是用户SSO（概览） | https://help.aliyun.com/zh/ram/overview-of-user-based-sso |

注意 D1 的 URL slug 写着 `for-role-based-sso`，但页面标题与正文是**用户 SSO**（阿里云文档 slug 历史遗留错配）。抓到的页面标题：「进行用户SSO时阿里云SP的SAML配置-访问控制(RAM)-阿里云帮助中心」，面包屑：访问控制 > 配置单点登录 > **用户SSO**。

---

## 1. 开关打开后，RAM 用户还能不能用「用户名 + 密码」登录控制台？

**结论：不能，密码登录被关闭，不是并存。【文档】**

D1 原文（控制台「SSO 功能状态」两个取值）：

> **关闭**（默认值）：RAM 用户可以使用密码登录，所有 SSO 设置不生效。
> **开启**：开启后，统一跳转到企业 IdP 登录服务进行身份认证，RAM 用户密码登录方式将会被关闭。

D2 英文对应原文：

> **Disabled** (default): RAM users log on with passwords, and SSO settings are ignored.
> **Enabled**: All RAM users must authenticate through your IdP. Password-based logon is disabled.

D3「SSO 方式比较」表把这一点当作用户 SSO 与角色 SSO 的分水岭：

| SSO 方式 | SP 发起的 SSO | IdP 发起的 SSO | **使用 RAM 用户账号和密码登录** | 一次性配置 IdP 关联多个阿里云账号 | 多个 IdP |
|---|---|---|---|---|---|
| **用户 SSO** | 支持 | 支持 | **不支持** | 不支持 | 不支持 |
| 角色 SSO | 不支持 | 支持 | **支持** | 支持 | 支持 |

→ **用户 SSO 与角色 SSO 在这一点上结论相反**：角色 SSO 只是多加一条登录通道，原有 RAM 用户名密码登录照常；用户 SSO 是把密码登录这条路关掉。

---

## 2. 有没有按用户 / 按用户组的例外机制？

**结论：没有。它是账号级全局开关，单值 true/false，无任何白名单/豁免维度。【文档】**

D1 原文「说明」块：

> 用户 SSO 是一个**全局功能**，开启后，**所有 RAM 用户**都需要使用 SSO 登录。用户 SSO 功能不影响阿里云账号（主账号）的登录，也不影响使用 AccessKey 发起的 OpenAPI 调用。

D4（Entra ID 示例，步骤六）措辞更精确，把作用域限定在「通过控制台登录的 RAM 用户」：

> 用户 SSO 是一个全局功能，开启后，**所有通过控制台登录的 RAM 用户**都需要使用 SSO 登录。如果您是通过 RAM 用户配置的，请先保留为关闭状态，您需要先完成 RAM 用户的创建，**以免配置错误导致自己无法登录**。您也可以通过阿里云账号进行配置来规避此问题。

API 侧佐证（D6 `SetUserSsoSettings` 的**全部**请求参数，一个 per-user/per-group 字段都没有）：

- `MetadataDocument`（IdP 元数据，Base64）
- `SsoEnabled` boolean —「是否开启 RAM 用户的 SSO 功能。取值：true：开启。false（默认值）：关闭。」
- `AuxiliaryDomain`（辅助域名）
- `SsoLoginWithDomain` boolean（NameID 是否带域名后缀，默认 true）
- `AuthnSignAlgo`（签名算法，见第 7 节）

**特别提醒：不要指望用 RAM 用户级的「控制台访问 / MFA」设置去做豁免。** D5 原文：

> **开启 RAM 用户 SSO 登录后，上述登录设置（是否允许控制台登录、是否要求 MFA 等）不生效。**

也就是说：既不能靠单用户设置绕开 SSO，也不能靠它给某人保留密码登录。

**真要留例外，只有账号级的办法**（均属【推理】，文档未把它们作为「例外机制」列出）：
1. 用主账号（root）作为唯一的非 SSO 入口 —— 这是官方在 D4 里推荐的规避姿势；
2. 把需要密码登录的人放到**另一个阿里云账号**里；
3. 改用**角色 SSO**（保留密码登录，见 D3 比较表），但那样 NameID 就不再匹配到具体 RAM 用户，与你们当前选型不符。

---

## 3. 阿里云账号（主账号 / root）登录是否受影响？

**结论：不受影响，主账号照常密码登录，这就是回滚路径。【文档】**

D1：「用户 SSO 功能**不影响阿里云账号（主账号）的登录**，也不影响使用 AccessKey 发起的 OpenAPI 调用。」
D2：「This does not affect **Alibaba Cloud account logon** or AccessKey-based API calls.」
D4 进一步把主账号当成防锁死的操作身份：「您也可以**通过阿里云账号进行配置**来规避此问题。」

**回滚路径（生产必须先确认可用）**：主账号登录 → RAM 控制台 → 左侧「集成管理 > SSO 管理」→「用户 SSO」页签 →「SSO 功能状态」改为**关闭**（导航路径出自 D1 操作步骤）。
或 OpenAPI：`SetUserSsoSettings` 传 `SsoEnabled=false`（D6）。

> 变更前的硬前置：确认主账号密码 + 主账号 MFA/安全手机在手且可用。开启用户 SSO 后，主账号是你唯一还能进控制台的身份；主账号进不去 = 无人能关掉这个开关。

---

## 4. AK/SK 调 API、STS AssumeRole 是否受影响？

**结论：AK/SK 调 OpenAPI 明确不受影响【文档】；STS AssumeRole 官方没有逐字点名，但按文档口径属于「AccessKey 发起的 OpenAPI 调用」，不受影响【推理】。**

- 【文档】D1 / D2：「也不影响使用 **AccessKey 发起的 OpenAPI 调用**」/「or **AccessKey-based API calls**」。
- 【文档】D5 同向佐证：「控制台登录设置仅影响 RAM 用户的**控制台登录行为**，**不影响通过访问密钥（AccessKey）进行的程序化访问**。」
- 【推理】`sts:AssumeRole` 是用 RAM 用户/角色的 AK 签名、打到 `sts.aliyuncs.com` 的一次普通 OpenAPI 调用，落在上面那句话的范围内；而用户 SSO 开关只作用在 `signin.aliyun.com` 的控制台登录链路（D1 的原文说的是「统一跳转到企业 IdP 登录服务进行身份认证」）。官方没有一句话写「用户 SSO 不影响 STS」，所以我只能标【推理】，不标【文档】。
- 【文档，避免混淆】角色 SSO 用的是 `AssumeRoleWithSAML`（另一条链路，由 SAML 身份提供商 + RAM 角色信任策略控制，D3），与**用户 SSO 开关无关**。用户 SSO 开关不会打开或关闭它。

**对本项目（AIOps bot）的具体影响面【推理】**：
- bot 的 `ALIYUN_BOT_MASTER_AK_*` → `sts:AssumeRole` → `aliyun_client_factory` 全链路是 AK 程序化调用 → **不受影响**。
- 六条搬运链、临时 AK 发放、RAM/IAM 建号、飞书审批回调等全部走 OpenAPI → **不受影响**。
- 唯一有感知的是**人**：任何还在用「RAM 用户名 + 密码」进阿里云控制台的同事（含运维自己、含临时 AK 发放链里那些开了控制台登录的 `tempak-*` 子用户）在开关打开后会被挡在门外。`tempak-*` 这类外采用户如果只拿 AK 调 OSS、从不进控制台，则无感；如果有人给他们开过控制台登录，就会受影响 —— 上线前建议清点一遍开了控制台登录的 RAM 用户名单。

---

## 5. 关闭开关是否立即恢复密码登录？回滚是否即时、有无缓存延迟？

**结论：关闭 = 回到默认态、密码登录可用【文档】；但「即时生效 / 无缓存 / 已登录会话如何处理」官方没有任何表述【拿不准】。**

- 【文档】D1：「关闭（默认值）：RAM 用户可以使用密码登录，所有 SSO 设置不生效。」D2：「Disabled (default): RAM users log on with passwords, and SSO settings are ignored.」
  即：关闭后密码登录恢复，且 SSO 配置（元数据等）保留但不生效 —— 不需要删配置就能回滚。
- 【未核实】搜索引擎摘要里出现过一句「如果再次关闭，用户密码登录方式自动恢复」，我在现行中/英文页面正文里**没有抓到这句原文**，疑似旧版文案或摘要模型的改写。**不要把它当官方原话引用。**
- 【拿不准】生效延迟：D1/D2/D6 均未提到生效时间、缓存或最终一致性。对照参考：RAM 授权本身是有同步延迟的 —— RAM 用户常见问题页明说「RAM 采用多地域部署…遵从最终一致性」（https://help.aliyun.com/zh/ram/support/faq-about-ram-users ），但那是**权限**不是**登录开关**，不能直接套用。
- 【拿不准】已登录会话：文档只在「禁用控制台访问」那里写了强制下线 —— D5 原文：「禁用控制台访问后，该 RAM 用户及该 RAM 用户当前扮演的 RAM 角色会被**强制退出登录状态**。」**用户 SSO 开关没有对应表述**，所以「开启 SSO 时已登录的会话会不会被踢」「关闭 SSO 时旧会话状态如何」都无从判断。

---

## 6. 开关打开后，没配 SAML 属性、无法通过 SSO 登录的 RAM 用户会怎样？

**结论：彻底登不进控制台，RAM 侧没有任何兜底入口。【文档 + 推理】**

- 【文档】D1：开启后「**统一跳转**到企业 IdP 登录服务进行身份认证」——登录页不再提供本地密码通道。
- 【文档】D8 FAQ 明确了失败表现：「**用户 SSO 时，报错"该用户不存在"，怎么办？**」列出的原因是 —— IdP 侧用户名后缀与 RAM 用户 UPN 后缀不一致；RAM 里根本没建这个用户，或用户名与 IdP 不一致；RAM 用户名字符集限制（只能英文字母/数字/`-_.`，≤64 字符）导致无法匹配；SCIM 同步失败；`Audience` 里 accountId 配错。
- 【文档】D9：NameID 必须是 `<username>@<域别名 | 辅助域名 | 默认域名>`，阿里云靠它定位 RAM 用户。IdP 侧没有给这个人配出合法 NameID → 匹配不到 → 登录失败。
- 【文档】D5：这类用户的「控制台访问 / MFA」等 RAM 侧登录设置在 SSO 开启后**不生效**，所以也不存在「给他单独开个密码登录」的兜底。
- 【推理】兜底只有两条：① 在 IdP 侧补齐该用户的属性/授权（正道）；② 主账号把用户 SSO 开关关掉（回滚，见第 3 节）。
- 【拿不准】**Passkey（通行密钥）、钉钉扫码登录、阿里云 App 登录在 SSO 开启后是否仍可用，官方文档没说。** 这三种是 RAM 用户登录控制台的其他方式（见 https://help.aliyun.com/zh/ram/user-guide/log-on-to-the-alibaba-cloud-management-console-as-a-ram-user ）。D1/D2 的措辞只提到「密码登录方式将会被关闭 / Password-based logon is disabled」，D3 比较表里那一列也只写「使用 RAM 用户账号和**密码**登录：不支持」。这留了一个语义缝隙：**要么它们也被一并关掉（多半如此），要么它们是个未记载的旁路**。生产决策若依赖「SSO 开启后确实没有任何旁路」，必须实测，不能按文档推。

---

## 7. 签名算法在哪里配？叫什么？

**结论：官方文档里只有 OpenAPI 参数 `AuthnSignAlgo`，取值 `rsa-sha256` / `rsa-sha1`（默认 `rsa-sha1`）；控制台文档没有记载这个选项。【文档】**

D6 `SetUserSsoSettings` 请求参数 / D7 `GetUserSsoSettings` 返回参数原文：

> **AuthnSignAlgo** string 否　阿里云 SP 支持的签名算法。取值：
> `rsa-sha256`
> `rsa-sha1`（默认值）

返回示例：
```json
{"UserSsoSettings":{"AuxiliaryDomain":"example.com","MetadataDocument":"PD94bWwgdmVy****",
 "SsoEnabled":true,"SsoLoginWithDomain":true,"AuthnSignAlgo":"rsa-sha1"},
 "RequestId":"87F2E3F6-28A0-43F3-A77F-F7760E62F61E"}
```

几点必须分清：

1. **它不是 IdP 元数据里带的。** 它是阿里云侧（SP 侧）的账号级设置，只能通过 `SetUserSsoSettings` 写、`GetUserSsoSettings` 读。【文档】
2. **控制台上没有这个选项（按文档）。** D1 的控制台操作步骤只列了三项：`SSO 功能状态`、`元数据文档`、`辅助域名`。`AuthnSignAlgo` 和 `SsoLoginWithDomain` 都只出现在 OpenAPI 文档里。【文档】→ 【拿不准】控制台实际界面是否已经有而文档没跟上，需要人工看一眼页面。
3. **它管的是「阿里云 SP 自己签名用什么算法」，不是「验 IdP 断言用什么算法」。** 参数名 `Authn`* 指向 SAML AuthnRequest 的签名。对照 D9：「阿里云要求 SAML **断言**必须被签名以确保没有篡改，`Signature` 及其包含的元素必须包含签名值、**签名算法**等信息」——断言那一侧的签名算法由 **IdP 生成断言时决定**，阿里云通过你上传的 IdP 元数据里的 X.509 公钥去验签。所以：**IdP → 阿里云方向的算法在 IdP 侧配；阿里云 → IdP 方向（AuthnRequest）的算法才是 `AuthnSignAlgo`。** 第 3 点中「它管哪一侧」这层解读官方没有逐字写明，标【推理】。
4. 默认值 `rsa-sha1` 偏弱。若企业 IdP 要求/只接受 SHA-256 签名的 AuthnRequest，需要显式调 `SetUserSsoSettings` 把它设成 `rsa-sha256`。【推理，基于 D6 的取值表】

> 调 `SetUserSsoSettings` 时注意：**它是一次整体覆盖式写入**（参数全在一个请求里）。文档没写「未传的字段保持不变」，所以只改签名算法时，建议先 `GetUserSsoSettings` 读回当前值，把 `MetadataDocument` / `AuxiliaryDomain` / `SsoLoginWithDomain` / `SsoEnabled` 一并回传，避免把元数据冲掉。这条是**基于 API 形状的谨慎推断【推理】**，不是文档明说。

---

## 8. 用户 SSO vs 角色 SSO —— 上述各点的结论差异

| 问题 | 用户 SSO（你们用的） | 角色 SSO |
|---|---|---|
| RAM 用户名+密码登录 | **不支持**（开关打开即关闭密码登录）【文档 D3】 | **支持**（并存，只是多一条登录通道）【文档 D3】 |
| 作用域 | 账号级全局开关，所有 RAM 用户【文档 D1/D4】 | 按身份提供商 + RAM 角色信任策略，天然按角色隔离【文档 D3】 |
| 是否有「开/关」总开关 | 有（`SsoEnabled`）【文档 D6】 | 无同类全局开关；创建/删除 SAML 身份提供商即可（https://help.aliyun.com/zh/ram/configure-the-saml-settings-of-alibaba-cloud-for-role-based-sso ）【文档】 |
| 一个 IdP 关联多个阿里云账号 / 多个 IdP | 不支持 / 不支持【文档 D3】 | 支持 / 支持【文档 D3】 |
| 登录后身份 | 就是那个 RAM 用户（NameID 匹配 UPN）【文档 D9/D10】 | 扮演 RAM 角色，拿 STS Token【文档 D3】 |
| AK/STS 程序化调用 | 不受影响【文档 D1】 | 不受影响（本就无关）【推理】 |
| 主账号登录 | 不受影响【文档 D1】 | 不受影响【推理】 |

---

## 9. 生产变更前的待确认项 + 建议的取证路径

我尝试在 bot-new 的 `aiops-bot` 容器里跑只读 `GetUserSsoSettings` 拿当前基线，**被本会话的权限策略拦截（Production Reads）**，所以下面第 1 条是空的，需要 dev 或有权限的人执行。

| # | 待确认 | 取证方式（全是只读/可逆） |
|---|---|---|
| 1 | 当前账号 `SsoEnabled` / `AuthnSignAlgo` / `SsoLoginWithDomain` / 是否已传过元数据的基线 | 只读调 `GetUserSsoSettings`（`ram:GetUserSsoSettings`，RAMReadOnly 级别）。本仓已有现成通道：`core.ram_approval._call_ims_api("GetUserSsoSettings", {})`（IMS `2019-08-15` RPC）。**打印时把 `MetadataDocument` 换成长度，别把元数据整段打到日志。** |
| 2 | 控制台「集成管理 > SSO 管理 > 用户 SSO」页签上到底有几个可配项、有没有「签名算法」 | 主账号登录控制台，人工看一眼并截图。文档只记了 3 项。 |
| 3 | SSO 开启后，Passkey / 钉钉扫码 / 阿里云 App 是否仍能登录（第 6 节的语义缝隙） | 在**测试阿里云账号**上开关一次实测；或提工单让阿里云书面确认。**不要在生产账号上试。** |
| 4 | 关闭开关后密码登录恢复的延迟、以及开关翻转时已登录会话是否被踢 | 同上，测试账号上掐表实测。 |
| 5 | 本账号下「已开启控制台登录」的 RAM 用户清单（谁会在开关打开后被挡住） | 只读 `ListUsers` + 逐个 `GetLoginProfile`（`ram:GetLoginProfile`）。注意火山那边 `get_login_profile` 对无登录配置返全零 stub 的坑是火山 IAM 的，阿里 RAM 不适用，但仍建议按「有无 LoginProfile」而不是按字段值判断。 |
| 6 | 主账号密码 + MFA/安全手机确实可用（回滚能力） | 变更**前**用主账号实登一次控制台，别只凭记忆。 |

## 10. 变更建议（不是文档结论，是我的操作建议）

1. **用主账号操作这个开关，不要用 RAM 用户操作** —— 官方 D4 直接点名了这个锁死风险。
2. 顺序：先上传 IdP 元数据 + 配好辅助域名/域别名 → 在 IdP 侧配好所有人的 NameID（`<RAM用户名>@<域名>`）→ 用**一个测试 RAM 用户**验证 SSO 能登进去 → 最后才把 `SSO 功能状态` 打开。开关是最后一步，不是第一步。
3. 提前广播：开关打开的那一刻，所有还在用密码登控制台的人立刻失去入口，且他们自己无法自救。
4. 回滚演练：把「主账号 → RAM 控制台 → 集成管理 > SSO 管理 > 用户 SSO → 关闭」这条路径写进变更单，并确认执行人手里有主账号二次验证手段。
5. 程序化调用（bot / 六条搬运链 / 临时 AK 发放）按文档不受影响，但仍建议开关打开后立刻跑一次 `/health` + 一条只读云 API 冒烟，用事实确认而不是用文档确认。
