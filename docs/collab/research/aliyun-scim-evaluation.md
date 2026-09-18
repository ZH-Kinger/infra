# 阿里云 RAM SCIM 账户同步 — 能否替代/补充现有 SSO 方案

调研人：researcher　日期：2026-09-16　对象：阿里云 RAM 的 SCIM 账户同步（`https://scim.aliyun.com`），以及它与我方 Authentik 2025.6.4 + SAML 用户 SSO + 交付面板的关系。

可信度标记：`【文档】` 官方文档明说、附原文；`【源码】` 读 Authentik 开源代码所得；`【推理】` 由文档/源码推导、原文未逐字说；`【未写】` 翻遍相关文档没有写，必须实测。

未调用任何云 API（含未授权的 discovery 端点），全部结论来自文档正文与开源代码。

---

## 0. 证据页清单

| # | 标题 | URL |
|---|------|-----|
| A1 | 通过 SCIM 协议将企业内部账号同步到阿里云 RAM（**主文档，接口级最全**） | https://help.aliyun.com/zh/ram/synchronize-accounts-from-an-enterprise-internal-system-to-ram-based-on-scim （旧址 https://help.aliyun.com/document_detail/162674.html ） |
| A2 | 在阿里云 IDaaS 中通过 SCIM 同步账户到阿里云 RAM（任务给的起点页） | https://help.aliyun.com/zh/ram/use-scim-to-synchronize-accounts-to-ram |
| A3 | 通过 SCIM 协议同步 Okta 用户到阿里云 RAM（**唯一的第三方 IdP 对接范例**） | https://help.aliyun.com/zh/ram/synchronize-okta-users-to-alibaba-cloud-ram-through-scim |
| A4 | IDaaS 同步到应用 - SCIM（含**跨云厂商支持矩阵**） | https://help.aliyun.com/zh/idaas/eiam/user-guide/account-provisioning-using-scim |
| A5 | IDaaS 同步删除 RAM 用户失败如何解决？ | https://help.aliyun.com/zh/idaas/eiam/support/idaas-failed-to-delete-ram-account-synchronously |
| A6 | OAuth 应用概览 | https://help.aliyun.com/zh/ram/overview-of-oauth-applications |
| A7 | 云 SSO 支持的 SCIM 2.0 接口（**另一条路，语义与 RAM 不同**） | https://help.aliyun.com/zh/cloudsso/user-guide/scim-2-0-interfaces-supported-by-sso |
| A8 | 什么是 RAM 的单点登录（SSO）概览 | https://help.aliyun.com/zh/ram/user-guide/sso-overview |
| K1 | authentik SCIM Provider 文档（当前版） | https://docs.goauthentik.io/add-secure-apps/providers/scim/ |
| K2 | authentik Release 2025.10 | https://docs.goauthentik.io/releases/2025.10/ |
| K3 | authentik 源码 `providers/scim/clients/users.py`（tag `version-2025.6`） | https://raw.githubusercontent.com/goauthentik/authentik/version-2025.6/authentik/providers/scim/clients/users.py |
| K4 | authentik 源码 `providers/scim/clients/base.py`（tag `version-2025.6`） | https://raw.githubusercontent.com/goauthentik/authentik/version-2025.6/authentik/providers/scim/clients/base.py |
| K5 | authentik 默认 SCIM 属性映射 `blueprints/system/providers-scim.yaml`（tag `version-2025.6`） | https://raw.githubusercontent.com/goauthentik/authentik/version-2025.6/blueprints/system/providers-scim.yaml |
| S1 | 本组前序调研：阿里云「用户 SSO」开关语义 | `/home/l/桌面/infra/docs/collab/research/aliyun-ram-user-sso-switch.md` |

先说一条容易被起点页 A2 误导的事实：**A2 讲的是「阿里云 IDaaS 作为 SCIM 客户端推给 RAM」，不是通用对接指南。** 通用的接口级文档是 A1，第三方 IdP 范例是 A3。这三页的 SCIM 服务端是同一个（`https://scim.aliyun.com`），差别只在客户端是谁。任务里说 A2「内容偏薄」，原因就在这里。

---

## 1. SCIM 与用户 SSO 的关系

**结论：两套完全独立的机制，互不依赖，也没有冲突。开 SCIM 不需要上传 IdP 元数据、不需要打开用户 SSO 开关；反之亦然。【文档】+【推理】**

证据：

- A1 的「前提条件」一节全文只有一句：「本文中所有 RAM 控制台的操作，推荐使用 RAM 管理员或具有 OAuth 管理权限的 RAM 用户完成。」整页没有出现 SSO、元数据、SAML 任何一个词。它依赖的是 OAuth 应用（`/acs/scim` 范围），走的是 `oauth.aliyun.com` + `scim.aliyun.com` 两个端点，跟 `SetUserSsoSettings`（S1 第 2 节列过它的全部参数）没有交集。【文档】
- 反向：A8「什么是 RAM 的单点登录（SSO）」整页无 SCIM。【文档】
- 两者是互补的：SSO 解决「人怎么登进来」，SCIM 解决「云上有没有这个人」。用户 SSO 的 NameID 必须**精确匹配一个已存在的 RAM 用户名**，而 RAM 用户从哪来（控制台建、API 建、SCIM 推）它不关心。【推理】

一个容易误读的点：A3 的 Okta 教程把 SCIM 配置**挂在一个 SAML 应用上**（步骤二先建 SAML 2.0 app，步骤三在同一个 app 上 `Enable SCIM provisioning`），A2 的 IDaaS 教程也要求先添加「阿里云用户 SSO」应用模板再切到「账户同步」页签。这是 **IdP 侧的产品打包方式**（Okta/IDaaS 都把 provisioning 挂在 application 对象上），不是阿里云侧的依赖。阿里云侧看到的只是一串带 Bearer token 的 HTTP 请求。【推理】

**顺带重申 S1 的结论不受影响**：用户 SSO 开关一旦打开，所有 RAM 用户密码登录关闭、无按用户例外。SCIM 不提供也不缓解这个约束。

---

## 2. 匹配与冲突处理

这是全篇最关键、也是官方文档最不肯写清楚的一节。

### 2.1 阿里云侧的身份标识

A1 明确列了 RAM SCIM User 资源支持的**全部四个字段**，没有第五个：

> `id`：RAM 用户 ID，全局唯一，服务端生成。
> `externalId`：RAM 用户外键，用户级别唯一，客户端指定，用于将阿里云的 RAM 用户和企业内部系统中的用户关联。
> **说明：通过控制台创建的 RAM 用户无 `externalId` 字段。**
> `userName`：RAM 用户名称，用户级别唯一，客户端指定。
> `displayName`：RAM 用户显示名称，客户端指定。

【文档】那句加粗的「说明」是本节的核心事实：**存量的 67 个用户（控制台/API 建的）一律没有 `externalId`**，所以 SCIM 客户端无法用 `externalId` 找到它们，只能用 `userName` 找。

检索能力也被限死：

> 对于 SCIM Filter，阿里云仅支持对 `id`、`userName` 和 `externalId` 进行 `and` 和 `eq` 操作。（A1）

即：能 `GET /Users?filter=userName eq "xxx"`，不能按邮箱、显示名、模糊匹配找人。【文档】

### 2.2 POST 一个已存在的 userName 会怎样

**官方文档没写。【未写】**

A1 只给了 POST 成功的请求/返回示例，对重名场景一个字没有，也没有任何错误码表。这是必须实测的第一条（见第 10 节）。

可以拿来推断的旁证有三条：

1. **同产品的另一条 SCIM 端点写了、而且语义是「认领」。** A7（云 SSO，注意是**另一个产品**）`POST /Users` 的「使用约束」写着：

   > 如果云 SSO 中存在同名的手动方式创建的用户，则会将该手动用户更改为 SCIM 同步用户。

   云 SSO 明写了，RAM 文档没写。同一家公司的两个 SCIM 服务端，一个写了一个没写，**不能假设行为相同**。【文档 + 推理】

2. **阿里云自家 IDaaS 给出的支持矩阵直接判 RAM 不行。** A4 末尾「IDaaS（EIAM）SCIM 支持情况」表，逐列抄录：

   | 平台 | 是否支持 SCIM | 是否支持检索存量用户 | 存量用户是否支持变更 | 最终存量用户是否关联成功 |
   |---|---|---|---|---|
   | 阿里云 RAM | 支持 | 支持 | **不支持** | **不支持** |
   | 阿里云 CloudSSO | 支持 | 不支持 | 不支持 | 支持（通过 CloudSSO 同名覆盖逻辑隐式支持） |
   | 华为云 IAM | 不支持 | | | |
   | 华为云 IAM 身份中心 | 支持 | 支持 | 支持 | 支持 |
   | 腾讯云 CAM | 不支持 | | | |
   | 腾讯云集团管理 | 支持 | 支持 | 支持 | 支持（用户名不支持变更） |
   | **火山引擎 IAM** | **不支持** | | | |
   | 火山引擎云身份中心 | 支持 | 不支持 | 不支持 | 不支持 |
   | AWS/国际站 IAM | 不支持 | | | |
   | AWS/国际站 IAM Identity Center | 支持 | 支持 | 支持 | 支持 |

   【文档】对 RAM 这一行的读法：能查到存量用户（filter 可用），但**无法把存量用户变更为受管用户、最终关联不成功**。也就是说，连阿里云自家的 IdP 都做不到「认领存量 RAM 用户」。这基本可以把「RAM 会像云 SSO 那样自动认领同名用户」的期待排除掉。【推理】

3. **RAM 控制台有一个「同步类型」列。** A3 的「验证结果」：

   > 同步成功的用户**同步类型**标识为「SCIM 同步」。

   说明 RAM 用户上有一个区分「SCIM 同步 / 非 SCIM」的标记位。这个标记位如何设置、能否后加，文档没写。【文档 +【未写】】

### 2.3 有没有「先匹配后创建」

阿里云服务端**没有**提供这个机制（没有 PATCH-by-filter、没有 PUT-by-userName，PUT 只接受 `/Users/{id}`）。【文档】

「先匹配后创建」只能由**客户端**实现：先 `GET /Users?filter=userName eq "x"`，拿到 `id` 再决定 POST 还是 PUT。A1 的接口面足以支持自己写一个这样的客户端。Authentik 恰好也实现了一个类似逻辑，但有版本坑，见第 8 节。【推理】

---

## 3. RAM 用户名由什么决定，能不能映射成自定义属性

**结论：RAM 用户名 = SCIM 请求体里的 `userName` 字段，没有第二个来源。客户端想放什么就放什么。【文档】**

A1 的 POST 示例逐字：

```
"userName": "j2gg0screatedbyscim_exa****"
"displayName": "j2gg0s_****"
"externalId": "6e74eec4-ddb5-4e74-bd12-5e7b99b2****"
```

阿里云侧**没有**「字段映射」这个概念——映射发生在客户端（IdP）。A2 里那句「支持自定义 SCIM 字段映射关系」是在讲 **IDaaS 的界面**，不是 RAM 的能力。IDaaS 默认的三条映射是（A4）：

- `appUser.externalId` ← 字段 `userId`（必填）
- `appUser.userName` ← 字段 `账户名`
- `appUser.displayName` ← 表达式 `Coalesce(user.displayName, user.username)`

所以「能不能把 RAM 用户名映射成 IdP 用户的某个自定义属性」这个问题，**答案完全取决于 IdP 端的映射表达能力，与阿里云无关**。对 Authentik 而言是可以的，见第 8.3 节。【推理】

三条 RAM 侧的加工规则，会影响我方 `cloud_accounts["aliyun-main"]` 里那个「完整 NameID」能否原样落地：

- **强制转小写。** A4 注意事项：「在进行 RAM 或 Cloud SSO 应用 SCIM 同步时，由于阿里云 RAM 对大小写不敏感，为了避免冲突，账户字段值将**全部转小写**后同步到 RAM 中。」这条是 IDaaS 侧的行为描述，raw API 是否也转，【未写】。【文档】
- **域名后缀会被改写。** A3 背景信息：「Okta 分配用户到应用后，RAM 会自动创建同名用户，**Okta 用户名的域名后缀将会自动替换为 RAM 用户域名**。」即 `x@okta.com` → `x@<uid>.onaliyun.com`。是阿里云服务端做的还是 Okta 连接器做的，文档没区分，【未写】。【文档】
- A1 的示例 `userName` 是**裸名不带域名**，A3 的是带域名的。两种都能进，说明服务端对两种形态都接受。【推理】

对我方的直接含义：`cloud_accounts["aliyun-main"]` 存的是完整 NameID（形如 `wangzihan@1704065796538912.onaliyun.com`）。把它原样作为 `userName` 推过去，落到 RAM 的用户名理论上就是我们想要的那个。但「转小写」这条要当心——`ZihanWang` 这类混合大小写的值在火山那边有用，在阿里这边会被压平；好在阿里云本来就对用户名大小写不敏感。【推理】

---

## 4. 删除与停用语义

**结论：DELETE = 硬删除，没有停用、没有软删除、没有回收站。但删除会被「有 AK / 有用户组 / 绑了 MFA」挡住而失败。【文档】**

### 4.1 硬删除，无软删除

A1 结尾的「说明」：

> 阿里云暂时不支持软删除。如果您的系统支持软删除，建议先将软删除映射成硬删除，再同步到阿里云。

`DELETE /Users/{id}` 返回 204 即删除成功。【文档】

### 4.2 RAM 用户没有「停用」状态

A3 背景信息：

> 当您在 Okta 删除用户或取消用户分配，Okta 会将用户状态置为 `active=false`。**RAM 用户无启用、禁用状态，不支持 Okta 对用户 Inactive 的状态同步，因此同步至 RAM 的用户不做变更。**

【文档】这条很重要，它意味着两件事：

1. 走「Okta 式」连接器时，IdP 停用一个人 → RAM 什么都不会发生，人还在，AK 还能用。安全上这是个**缺口**（离职人员的 AK 不会随停用失效），运维上这是个**保护**（不会误删）。
2. SCIM 标准的 `active: false` 在 RAM 这里是空操作。想真正回收，只能发 `DELETE`。

### 4.3 删除会不会连带删 AccessKey / 策略授权

**不会连带删，而是整个删除操作失败。【文档】**

A5（IDaaS 同步删除 RAM 用户失败）「可能原因」第一条：

> RAM 用户处于特殊状态（如绑定 MFA、配置 AccessKey 或加入用户组）。

解决方案是**人工**逐项清理：从所有用户组移除 → 解绑 MFA → 删除所有 AccessKey → 再手动删用户。A5「注意事项」还写了：

> 删除 RAM 用户后，该账号及其扮演的角色将被强制退出登录状态，且无法自动恢复。

【文档】这构成了一道**事实上的护栏**：任何挂着 AccessKey 的 RAM 用户，SCIM 删不掉。我方那 17 个服务号（`tempak-*`、`wuji-ci`、`Data-tran`、`finance`）**按定义都持有 AK**——`temp_ak` 方案 B 的号就是「RAM 子用户 + 长期 AK」——所以即使 SCIM 真的对它们发了 DELETE，也会失败而不是删成功。

这道护栏的三个破绽，必须记住：

1. **不是所有服务号都一定有 AK。** 万一有哪个号只挂了 policy、没有 AK、也不在任何用户组里，它就是可删的。A5 只列了 MFA/AK/用户组三种特殊状态，**没提「附加了自定义策略」算不算**，【未写】。
2. **删除失败是静默的。** A5 描述的现象就是「未能同步删除」，没有告警。反过来说，删成功也同样安静。
3. **`temp_ak` 清理链恰好会制造一个删除窗口。** `cleanup.revoke_grant` 的顺序是「删 AK → 删 policy → 删 user」。在删掉 AK 之后、删掉 user 之前的那个瞬间，这个号是**没有 AK、可被外部删除**的状态。窗口极短且此时本来就要删，风险不高，但说明「有 AK 所以删不掉」不是一个恒真的不变式。【推理】

---

## 5. 同步范围能不能限定 / 会不会误删服务号

拆成两个独立的问题，结论不同。

### 5.1 范围能限定 —— 能【文档】

- IDaaS 侧：A2 步骤二「切换至**账户同步**页签，设置**同步范围**后单击保存」；A4「全量推送范围：当进行一键推送（即全量同步）时，只会推送该应用**同步范围内的**、全量推送范围所选数据类型的数据到下游应用」。范围按 IDaaS 组织节点划。
- Authentik 侧：K1「User filtering」——

  > When a SCIM provider is configured as a backchannel provider for an application, only users who are bound to that application are synchronized. A common setup is to bind a group to the SCIM application so that all members of that group are synchronized.
  > **If no users are bound to the SCIM application, the SCIM provider synchronizes all users.**

  另有 `Exclude service accounts` 开关，以及在 property mapping 里 `raise SkipObject` 的逐个排除手段。【文档】

  注意那句加粗的默认行为：**不绑任何人 = 同步所有人**。这是个 fail-open 默认值，配置时必须显式绑组。

### 5.2 SCIM 会不会删除「IdP 里没有」的 RAM 用户 —— 不会【源码】+【文档】

这是任务里点名的风险核心，所以拿源码证了一遍，不靠文档措辞。

Authentik 2025.6 的 `SCIMUserClient.delete()`（K3）：

```python
def delete(self, obj: User):
    scim_user = SCIMProviderUser.objects.filter(provider=self.provider, user=obj).first()
    if not scim_user:
        self.logger.debug("User does not exist in SCIM, skipping")
        return None
    response = self._request("DELETE", f"/Users/{scim_user.scim_id}")
```

三点：

1. 删除的入参是一个 **authentik 侧的 User 对象**。整个同步任务是「遍历 authentik 的用户」，不存在「遍历远端用户找孤儿」这个动作——SCIM 协议本身是单向 push，Authentik 的 provider 也没实现 reconcile-and-prune。**云上存在但 IdP 里不存在的 RAM 用户，压根不在任何一个循环里。**
2. 即便在循环里，也要先能查到一条 `SCIMProviderUser` 连接记录（记录了 authentik user ↔ 远端 scim_id 的绑定）。没有这条记录直接 skip，不发 DELETE。这条记录只在 authentik 自己创建（或认领，见 8.2）该用户时才会写。
3. DELETE 用的是 `scim_id`（阿里云侧的 `id`），不是 userName。不存在「按名字误删」。

同样的结论对 IDaaS 路径成立：A4 描述的操作是订阅「账户创建/更新/删除」事件 + 一键全量**推送**，没有任何一处提到「清理下游多余数据」。【文档】

**所以：`tempak-*` / `wuji-ci` / `Data-tran` / `finance` 这些只存在于云上、不存在于 Authentik 的号，不会被 SCIM 删除。** 第 4.3 节的 AK 护栏是第二道保险，不是唯一一道。

但有一个**真实的误删路径**，见第 9.1 节——风险不在「IdP 里没有的用户」，而在「IdP 里曾经有、后来被移出应用绑定的用户」：

> When a user is no longer assigned to the application—either directly or through a group—and a SCIM sync task runs, that user is **deprovisioned** from the target SCIM endpoint.（K1）

---

## 6. 用户组与策略授权同步不同步

**结论：RAM 的 SCIM 端点只有 `/Users`，没有 `/Groups`；策略授权在 SCIM 协议里根本不存在。我方面板挂的授权不会被 SCIM 覆盖。【文档】**

- A4 注意事项：「IDaaS EIAM 当前同时支持组和账号同步，但实际能否同步到下游应用取决于该应用的对接能力。**当前阿里云 RAM 仅支持账户同步**，阿里云 Cloud SSO 支持账户和组同步。」
- A3 背景信息：「**不支持同步 Okta 的用户组。**」
- A1 全文只给了 `/Users` 的 CRUD，没有 `/Groups` 任何示例。
- SCIM 2.0 核心 schema（RFC 7643）里没有「授权策略」这种资源，阿里云也没定义扩展 schema 来承载它。RAM policy 只能通过 RAM OpenAPI（`AttachPolicyToUser` 等）操作，与 SCIM 是两条完全不相干的通道。【推理】

一个副作用值得记一笔：既然 SCIM 不推组，用户组这个维度就完全归我方面板管；而第 4.3 节说「加入用户组」会让 SCIM 删除失败——也就是说，**面板把人放进用户组这个动作，顺手给这个人加了一层防误删**。

---

## 7. 阿里云侧接入的技术参数（备查）

| 项 | 值 | 出处 |
|---|---|---|
| SCIM 端点 | `https://scim.aliyun.com` | A1 |
| OAuth token 端点 | `https://oauth.aliyun.com/v1/token` | A1 |
| OAuth 授权端点（3-legged 才用） | `https://signin.aliyun.com/oauth2/v1/auth` | A3 |
| OAuth 范围 | `/acs/scim`，并需在「第三方应用授权」页选中「阿里云跨域身份管理服务」 | A1 |
| 应用类型 | 2-legged（无人值守）用 **Server 应用**；3-legged（Okta 那套）用 **Native 应用** | A1 / A3 |
| 授权模式 | `grant_type=client_credentials`，`Authorization: Basic base64(client_id:AppSecretValue)` | A1 |
| access_token 有效期 | 默认 3600 秒，**可设范围 900～10800 秒**（上限 3 小时） | A2 |
| 支持的资源 | 仅 `/Users`（POST / GET / PUT / DELETE）+ discovery `/ResourceTypes` `/Schemas` | A1 |
| 支持的用户字段 | `id`(服务端生成) / `externalId` / `userName` / `displayName`，**手机号和邮箱不支持映射** | A1 / A2 |
| Filter 能力 | 仅 `id` / `userName` / `externalId`，仅 `eq` 与 `and` | A1 |
| 密钥 | AppSecretValue **仅创建时可见，不支持二次查询** | A1 |
| 产品状态 | 控制台入口是「OAuth 应用（**公测**）」 | A1 / A2 / A6 |

A6 对 Server 应用的定义值得单独抄一句，它说明这条路是专为 SCIM 设计的：

> Server 应用：指直接访问阿里云服务，而无需依赖用户登录的应用。**目前仅支持基于 SCIM 协议的用户同步应用。**

---

## 8. Authentik 侧可行性

### 8.1 认证方式 —— 我方当前版本不兼容【文档】+【源码】

阿里云要求：先拿 client_id/secret 去 `oauth.aliyun.com/v1/token` 换 access_token（最长 3 小时），再把它当 Bearer 用。

Authentik 2025.6.4 的 SCIM provider 只有**静态 token** 一种模式。K4 源码里 token 是构造时从 provider 字段读死的：

```python
self.token = provider.token
...
headers={"Authorization": f"Bearer {self.token}", ...}
```

没有任何 token 刷新逻辑。把一个 3 小时后过期的 access_token 填进去，最多跑 3 小时就全线 401。

OAuth 模式是 **2025.10 才加的，且是 Enterprise（付费）功能**。K2 原文：

> **SCIM provider OAuth support** `Enterprise` — SCIM providers can now use OAuth sources to authenticate to SCIM endpoints. This requires support in the remote system for OAuth authentication. Using an OAuth source provides improved security due to not requiring long-lived static tokens. This is supported by applications such as Slack and Salesforce.

对应 changelog 条目：`enterprise/providers/scim: Add SCIM OAuth support (#16903)`。K1 进一步说明它支持 `grant_type: client_credentials` 和「Silent OAuth（自动获取/刷新，无需管理员交互）」——**功能上正好匹配阿里云的 2-legged 模式**。

**所以这条是版本 + 授权双重阻塞：要用 Authentik 原生对接阿里云 SCIM，必须 ① 升到 2025.10+ ② 买 Enterprise license。** 二者缺一，就只能自己写同步器（阿里云 SCIM API 很简单，A1 那几个 curl 就是全部，自建客户端工作量不大）。

### 8.2 存量用户认领 —— 逻辑存在，但 2025.6 有 bug【源码】

K3 的 `create()` 实现了「先创建、409 后按 userName 认领」：

```python
except ObjectExistsSyncException as exc:
    if not self._config.filter.supported:
        raise exc
    users = self._request("GET", f"/Users?{urlencode({'filter': f'userName eq {scim_user.userName}'})}")
    users_res = users.get("Resources", [])
    if len(users_res) < 1:
        raise exc
    return SCIMProviderUser.objects.create(provider=..., user=user, scim_id=users_res[0]["id"], ...)
```

（`ObjectExistsSyncException` 在 K4 里映射的是 HTTP **409 Conflict**。）

两个前置条件，在 2025.6 对阿里云**都很可能不成立**：

1. **`self._config.filter.supported` 必须为真。** 它来自 `GET /ServiceProviderConfig`；K4 里该请求失败会 fallback 到 `ServiceProviderConfiguration.default()`，而 default 把 `filter`/`patch`/`bulk` 全部置 `supported=False`。**A1 只记载了 `/ResourceTypes` 和 `/Schemas` 两个 discovery 端点，没有 `/ServiceProviderConfig`。** 如果阿里云没实现它，认领分支直接不进。【未写 → 必须实测】
2. **filter 字符串在 2025.6 里没加引号。** 2025.6 发的是 `userName eq j2gg0s`，2025.10 才改成 `userName eq "j2gg0s"`（changelog：`providers/scim: fix string formatting for SCIM user filter (#16465)`；我 diff 了两个 tag 的 `users.py`，改动就这一行）。而 A1 的示例用的是**带引号**的形式（`filter=userName%20eq%20%22...%22`）。裸值能不能被阿里云接受，【未写】。

再叠加第 2.2 节 A4 那张矩阵对 RAM 判的「存量用户最终关联成功：不支持」——**「让 Authentik 自动认领 67 个存量 RAM 用户」这条路，目前应当按走不通来规划。**

### 8.3 自定义 userName —— 完全可以【文档】+【源码】

Authentik 的 property mapping 是 Python 表达式，返回一个 dict。默认的用户映射（K5）是：

```python
return {
    "userName": request.user.username,
    "name": {...}, "displayName": request.user.name,
    "photos": photos, "locale": locale,
    "active": request.user.is_active, "emails": emails,
}
```

K1 说明多条映射「are applied in the order of their name, and are deeply merged onto the final user data」。所以加一条名字排序在后的自定义映射返回 `{"userName": ...}`，即可覆盖默认值。取嵌套 JSON 属性在表达式里就是普通的字典取值（K1 的官方示例本身就在用 `request.user.attributes.get("phone", "")`），取 `cloud_accounts` 里的 `aliyun-main` 没有任何障碍。

**但这带出一个必须处理的坑**：默认映射会往外发 `emails`、`photos`、`locale`、`name`、`active`——这些字段阿里云 RAM 一个都不支持（A1 只认四个字段，A2 明写「不支持映射用户的手机号和邮箱属性」）。K1 又说：

> The final data is then validated against the SCIM schema, and if the data is not valid, **the sync is stopped**.

阿里云收到超纲字段是忽略还是 400，【未写】。稳妥做法是自定义一条映射只返回 `userName` / `displayName` / `externalId`，并把默认映射从 provider 上摘掉。

### 8.4 一个必须知道的行为差异

K1「Sync behavior」：

> Once an hour, all SCIM providers are fully synchronized.

Authentik 每小时会做一次全量同步。这意味着任何配置错误（比如绑错了组）**最长一小时内就会被放大到全量**，不是只影响增量事件。

---

## 9. 用 SCIM 替代现方案的可行性判断

先把两个方案在管什么上摆清楚：

| 能力 | 现方案（属性映射 + SAML 用户 SSO） | SCIM 能提供的 |
|---|---|---|
| 登录时把人对到正确的 RAM 用户 | `cloud_accounts["aliyun-main"]` → NameID | 不管登录 |
| 云上有没有这个 RAM 用户 | 面板按审批建号 | 自动建号 |
| 离职时回收 | 面板/人工 | DELETE（且被 AK 挡住） |
| 授权 | 面板按审批挂策略 | 不管授权 |
| 用户名不规范 | 靠属性表吸收差异，不改名 | 不解决，只是把不规范的名字改成由 IdP 决定 |

**两者在功能上几乎不重叠。SCIM 不会让属性映射变得多余——因为 SCIM 不参与登录，用户 SSO 仍然需要 NameID 精确命中 RAM 用户名。**

### 情形 (a)：完全替代属性映射

**不成立，技术上就走不通。**

「替代」的唯一可能形态是：让 SCIM 用一个规范化的 userName（比如统一成邮箱前缀）去重建所有 RAM 用户，从此 NameID 直接用 `user.username`、不再需要 `cloud_accounts` 属性。这要求：

1. 重命名或重建 50 个真人的 RAM 用户 —— 与「已决定不改名」直接冲突；
2. 存量用户认领不可用（第 2.2、8.2 节），只能删旧建新；而删旧 = 连带清 AK、清授权、清用户组（第 4.3 节），等于把 67 个号的权限现状全部推倒重来；
3. 即便不重命名，把每个人的 `cloud_accounts["aliyun-main"]` 推成 userName，属性表**仍然要存在**（因为火山那边的名字不同、且 SCIM 不同步到火山 IAM，见第 11 节），只是从「SAML 取值用」变成「SCIM 取值用」。属性表一条都没省掉。

结论：这条路成本极高、收益为零。

### 情形 (b)：并存 —— SCIM 管生命周期、属性映射管 NameID

**技术上自洽，但当前被两个硬阻塞卡住，且与面板职责重叠。**

自洽的部分：SCIM 推 `userName = cloud_accounts["aliyun-main"]`、`externalId = authentik uid`；SAML 的 NameID 表达式继续从同一个属性取值，两边天然对齐，不会漂移。新人入职 → Authentik 建号并绑组 → SCIM 自动在阿里云建出同名 RAM 用户 → SAML 登录直接命中。

阻塞的部分：

1. **Authentik 2025.6.4 + 非 Enterprise = 无法对接**（第 8.1 节）。要么升级 2025.10+ 并购买 Enterprise，要么自建同步器。
2. **存量 67 个用户一个都进不来**（第 2.2、8.2 节）。SCIM 只能管新建的人。于是会出现「50 个老用户走面板、之后的新人走 SCIM」的双轨状态，比单轨更难维护。

重叠的部分：**「审批通过后建 RAM 用户」这件事，面板已经在做，而且做得比 SCIM 更符合我方需求**——它挂策略、它受审批门禁、它知道账号档案（默认档 / 1949 档）。SCIM 只能建一个光秃秃的用户，策略还得面板补。让两个系统都能建 RAM 用户，反而增加了「这个号是谁建的、谁负责删」的不确定性。

有价值的部分只剩一处：**离职回收**。SCIM 能在 Authentik 停用/移出绑定时自动发 DELETE。但第 4.2 节说 RAM 没有停用状态、`active=false` 是空操作；第 4.3 节说有 AK 的号删不掉。也就是说这个唯一的增量收益，恰好在最需要它的场景（离职人员持有 AK）失效。

### 情形 (c)：不用

**推荐这个。** 维持交接文档「不对阿里云、火山启用 SCIM 同步」的原判断。

理由按权重排：

1. **收益点几乎全被面板覆盖**。建号、授权、账号档案分派、审批门禁，面板已经做了，且是按我方的审批语义做的。SCIM 能补的只有「自动建号」，而自动建号在一个需要审批的环境里本来就不是优点。
2. **唯一不重叠的收益（离职自动回收）在 RAM 上恰好残废**：无停用语义 + 有 AK 删不掉。想要回收，写一个「Authentik 停用 → 调 RAM API 删 AK + 删用户」的面板任务，比接 SCIM 直接、可控、可审计得多——而且这条路不需要 Enterprise license。
3. **接入成本不低**：升级 Authentik 到 2025.10+（跨 2025.8/2025.10 两个大版本，2025.10 还有 Redis 移除这个 breaking change）+ 购买 Enterprise + OAuth 应用仍是「公测」状态。
4. **存量认领不可用**，注定双轨，长期看是负资产。
5. **风险不对称**：做对了省的是「新人入职少点几下」；做错了动的是生产 RAM 用户。

如果将来确实要做自动化生命周期，**更可能正确的方向不是 RAM SCIM，而是阿里云「云 SSO」**（A7）：它的 SCIM 端点是**静态 Bearer 凭证**（`--header 'Authorization: Bearer <your scim credential>'`，端点 `https://cloudsso-scim-<regionId>.aliyun.com/scim/v2/`）——Authentik 社区版就能对接，不需要 Enterprise；而且它明写了同名认领语义（第 2.2 节）、支持用户组同步。代价是云 SSO 是**另一套身份平面**（依赖资源目录、发的是 SSO 用户和访问配置，不是 RAM 用户），我方面板、`temp_ak`、`oss_perm` 全部建立在 RAM 用户之上，迁过去是一次大改造。**本次不建议动，但值得作为「如果有一天要重做身份层」的备选记一笔。**

---

## 10. 风险

### 10.1 服务号被误删 —— 低，但不是零

**主结论（第 5.2 节）：SCIM 不会去删「IdP 里不存在」的 RAM 用户**，因为 Authentik 的删除是遍历 authentik 用户 + 查连接记录，云上孤儿根本不进循环；IDaaS 侧也只有推送、没有 prune。加上第 4.3 节的 AK 护栏，`tempak-*` / `wuji-ci` / `Data-tran` / `finance` 在正常配置下安全。

**真正的误删路径是这条**（K1 原文）：

> When a user is no longer assigned to the application—either directly or through a group—and a SCIM sync task runs, that user is deprovisioned from the target SCIM endpoint.

触发它需要两步凑齐：**① 某个服务号在 Authentik 里也有对应账号（或将来有人为了方便给它建了一个）；② SCIM 曾把它和某个 RAM 用户关联上（新建或认领）。** 之后任何一次「把这个号移出同步组」的操作，都会变成对生产 RAM 用户的 DELETE。

叠加几个放大因子：

- K1 那条 fail-open 默认值：**「If no users are bound to the SCIM application, the SCIM provider synchronizes all users.」** 忘了绑组 = 全量同步所有 authentik 用户。
- Authentik 每小时一次全量同步（第 8.4 节），配置错误会在一小时内铺开。
- 删除成功/失败都没有告警（第 4.3 节）。
- `temp_ak` 清理链在「删 AK 之后、删 user 之前」有一个短暂的「无 AK 可删」窗口（第 4.3 节）。

**如果最终决定试点，以下是必须的前置条件，不是可选项：**

1. Authentik 侧显式绑定同步组，**永不留空**；开启 `Exclude service accounts`。
2. 先在**一个独立的测试阿里云账号**上跑通全流程，绝不拿 `1704065796538912` 做第一次实验。
3. 试点期把 provider 设为 **dry-run**（K4 里 `provider.dry_run` 会拒绝一切非安全方法并抛 `DryRunRejected`，即只读观察），看清楚它到底想发哪些请求再放开。
4. 服务号一律不在 Authentik 中建对应账号。这是最有效的一条——没有 authentik 侧对象，就没有任何删除路径。

### 10.2 开启 SCIM 那一刻，存量 67 个用户会发生什么

按现有证据推断（【推理】，必须实测确认）：

- **不会被删。** 它们没有 `SCIMProviderUser` 连接记录，不在任何删除路径上。
- **不会被改。** SCIM 的 PUT 只能打 `/Users/{id}`，而 Authentik 手里没有它们的 id。
- **会被重复创建，或者创建失败。** 首次全量同步时，Authentik 对每个绑定用户发 POST。如果推的 `userName` 与云上某个存量用户重名：
  - 若阿里云返 409 且 `/ServiceProviderConfig` 声明 filter 支持 → 走认领分支（但 2025.6 的 filter 少引号，大概率还是失败，第 8.2 节）；
  - 若阿里云返 409 而 filter 不支持 → 抛异常，该用户同步失败，云上不变；
  - **若阿里云不返 409、而是直接建了一个新用户**（比如大小写/域名后缀被归一化后变成一个不同的名字）→ **同一个人在 RAM 里出现两个号**。这是最坏且最可能的结果，A4 矩阵里 RAM「存量用户最终关联成功：不支持」指向的正是这个。
- **17 个服务号完全不受影响**（前提是它们不在 Authentik 里）。

净效果：开 SCIM 那一刻，大概率既没解决存量，又制造了一批重复号。这也是第 9 节推荐 (c) 的直接依据。

### 10.3 其他

- **OAuth 应用仍是「公测（beta）」**（A1/A2/A6 控制台入口均标注），行为可能变更，SLA 不明。
- **AppSecretValue 仅创建时可见**，丢了只能重建密钥，重建期间同步中断。
- **Authentik 若升到 2025.10+**：该版本移除了 Redis 依赖，属于 breaking change，升级本身是一次独立的基建变更，不应与 SCIM 评估捆在一起决策。

---

## 11. 火山引擎

**火山引擎 IAM 不支持 SCIM。【文档】**

A4 的支持矩阵里，「火山引擎 IAM」这一行「是否支持 SCIM」直接写着**不支持**。同表里「火山引擎云身份中心」支持 SCIM，但三列后续能力（检索存量 / 存量变更 / 最终关联）**全是「不支持」**——比阿里云 RAM 还差一档。

我方火山侧用的是**企业联邦登录 + IAM 用户**（`core/ram_approval.py` 建的是 IAM 用户），正对应「火山引擎 IAM」那一行。所以火山这边**没有 SCIM 这个选项可选**，`cloud_accounts["volcano-main"]` 属性映射是目前唯一可行的方案。

阿里云侧即使上了 SCIM，火山侧也上不了 → **属性表无论如何都得留着**。这是第 9 节情形 (a) 不成立的另一个独立理由。

---

## 12. 落地前必须实测验证的点

只有在决定推进情形 (b) 时才需要做。全部应在**独立测试阿里云账号**上做，禁止碰 `1704065796538912`。

| # | 问题 | 文档状态 | 验证方法 | 影响 |
|---|---|---|---|---|
| V1 | `POST /Users` 推一个**已存在的 userName**，返回什么？409 / 200 / 其他？会不会建出第二个号？ | A1 完全未写 | 测试账号手工建 `dup-test`，再用 SCIM POST 同名 | 决定存量 50 人能否认领，也决定 10.2 的最坏情形是否发生 |
| V2 | `GET /ServiceProviderConfig` 是否实现？`filter.supported` 是否为 true？ | A1 只记了 `/ResourceTypes` `/Schemas` | 带 token `curl -s https://scim.aliyun.com/ServiceProviderConfig` | 决定 Authentik 的认领分支是否会被执行（第 8.2 节） |
| V3 | filter 值**不带引号**（`userName eq foo`）能否被接受？ | A1 示例带引号，未说裸值行为 | 两种写法各发一次 GET | 决定 2025.6 是否必须升级才能认领 |
| V4 | POST 体里带 `emails` / `active` / `name` / `photos` 等 RAM 不支持的字段，是忽略还是 400？ | A2 只说「不支持映射手机号和邮箱」，未说超纲字段处理 | 用 Authentik 默认映射的完整 payload 发一次 | 决定能否直接用默认映射，还是必须自写精简映射（第 8.3 节） |
| V5 | userName **是否被强制转小写**？带域名后缀 / 不带，分别落成什么？ | A4 只说 IDaaS 路径转小写，raw API 未写；A3 说域名后缀会被替换但没说谁做的 | 推 `TestUser`、`TestUser@x.com`、`testuser` 三种，查 RAM 控制台实际用户名 | 决定 `cloud_accounts["aliyun-main"]` 能否原样落地、NameID 会不会对不上（第 3 节） |
| V6 | SCIM 建出来的用户，**有没有控制台登录配置（LoginProfile）**？用户 SSO 开启后能否直接登录？ | 所有文档均未提 | 建一个，查 `GetLoginProfile`，再走一次 SAML 登录 | 决定 SCIM 建号后是否还需要面板补一步 |
| V7 | `DELETE /Users/{id}` 对**挂着 AccessKey** 的用户，具体返回什么错误码？ | A5 只说「同步删除失败」，无错误码 | 测试账号建号 + 建 AK + DELETE | 量化第 4.3 节那道护栏的强度，以及 Authentik 会不会把它当瞬时错误无限重试 |
| V8 | `DELETE` 对只挂了**自定义策略**（无 AK、无组、无 MFA）的用户，能否删成功？ | A5 的「特殊状态」清单里没有「附加策略」 | 同上 | 这是 10.1 里护栏最可能的破绽 |
| V9 | 控制台里的「同步类型」列，非 SCIM 用户能否变成「SCIM 同步」？ | A3 只提到它存在 | 观察 V1 的结果 | 与 V1 同一个问题的另一个观测面 |
| V10 | Authentik 2025.10 Enterprise 的 Silent OAuth 能否真的跑通 `oauth.aliyun.com`（Basic 认证 + `client_credentials` + 3 小时过期后的自动续期） | K1/K2 说支持 client_credentials，未针对阿里云验证 | 需先有 2025.10 + license 的测试实例 | 决定情形 (b) 是「配置」还是「自研同步器」 |

V1 一条就能定生死：如果它的答案是「建出第二个号」，情形 (b) 也基本可以放弃了。
