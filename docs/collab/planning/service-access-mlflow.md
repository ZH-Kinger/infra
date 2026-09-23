# 自建服务接入面板审批 —— 第一个是 MLflow

状态：待评审（planner 出，dev 收口）
涉及仓库：`infra`（面板，本仓）+ `39.108.82.208` 上那个飞书 OAuth 代理（**源码不在本仓**）

## 1. 要解决什么

MLflow 现在是「飞书认证过就能进」。认证只回答「你是不是本公司的人」，不回答「你该不该用这个服务」。
要把后一问交给面板的申请审批流程，而且**全程没有任何人要手工登记用户** —— 这是用户的硬要求
（原话：「这个平台我不想要手动去登记用户」）。所以审批通过的那一刻门就开了，不允许出现
「管理员再去某个地方把人加进白名单」这一步。

第一个接的是 MLflow。设计要做成「一类自建服务」而不是「MLflow 专用」，但本期只上 MLflow 一个。

## 2. 现状（实测，不是推断）

MLflow 跑在 `39.108.82.208`，域名 `tensorboard.wuji-tech.com`（域名叫 tensorboard、服务是 MLflow，历史原因）。

```
nginx/1.24.0 → 自建的飞书 OAuth 代理（/oauth2/start、/oauth2/callback）→ MLflow
```

`GET /oauth2/start` 实测 307 跳到
`https://accounts.feishu.cn/open-apis/authen/v1/authorize?client_id=cli_aa2d10ccd0b9dbb7&redirect_uri=https%3A%2F%2Ftensorboard.wuji-tech.com%2Foauth2%2Fcallback&response_type=code&state=<签名过的 JSON>`

几条要点：

- `cli_aa2d10ccd0b9dbb7` **就是面板线上在用的那个飞书应用**（面板 `DELIVERY_AUTH=feishu`，同一个 app id）。
- 那个代理不是标准 oauth2-proxy（上游没有飞书 provider），是自己写的或改的。**源码不在本仓**，
  涉及它的部分本文写成「给对方的需求说明」（第 6 节，可整页转发）。
- authorize 那一跳没有 `code_challenge` —— 没走 PKCE。下面的契约按「无 PKCE」定。
- 面板这边登录方式是飞书 OAuth；公司 IAM（Authentik OIDC）**只有模板没启用**（4180 端口空着、
  没有 oauth2-proxy 进程、`DELIVERY_IAM_API_TOKEN` 是空的）。**方案里不出现 Authentik**，它不在这条链路上。

## 3. 形状（已定板）

**代理问面板**，不是面板往那台机写白名单文件。面板本来就是公网 HTTPS，那台机来读它即可，
不用新开任何入站通道、不用求 IT；反过来要新开一条写入通道，是新的攻击面。

在此基础上，用户又定了一条：**面板那个飞书应用的 secret 不许出现在第二台机器上**。
所以 code→token 的交换也挪到面板做，顺带把门禁那一问合并进同一次调用：

```
用户 → 代理 /oauth2/start → 飞书 authorize（这一跳不用改，见下）
     → 飞书回调 /oauth2/callback，代理拿到 code
     → 代理 POST 面板 /api/service-access/exchange {code, service:"mlflow"}
     → 面板用自己的 app secret 换 user_access_token → 拿 union_id → 查名册 → 查授权
     → 回 {allowed, union_id, name} 或 {allowed:false, reason, apply_url, message}
     → 代理照结果建自己的会话放行，或渲染一页「你还没权限，去这里申请」
```

为什么合并成一次调用：

- app secret 只留在面板机。那个 app 不只用于登录 —— 面板拿它取 tenant token、读通讯录、
  以管理员身份私聊人。secret 放在 `39.108.82.208` 上，等于那台机被拿下就能读公司通讯录、
  以面板身份给任何人发飞书消息。
- 代理手里只剩一个调面板的 token，泄了**最多能查「某人能不能用 mlflow」**，读不了通讯录、发不了消息。
- 少一次往返。

**边界（写清楚，免得对方以为整条链要重写）**：`/oauth2/start` 那一跳只要 app id + redirect_uri，
**不需要 secret，不用改，也不用经过面板**。只有回调之后「拿 code 换身份」那一步改成调面板。

代价：代理那边要从「自己换 token」改成「让面板换」，改动比「只加一次查询」多一点。这写进第 6 节。

## 4. 接口契约（最要紧的一段）

### 4.1 端点与认证

```
POST https://cloud.wuji-tech.com/api/service-access/exchange
Authorization: Bearer <服务令牌>
Content-Type: application/json
```

- **机器对机器，不是用户会话。** 面板现有的两道门都不适用：`_require(admin=True)`（`src/delivery/server.py:1512`）
  认的是浏览器会话；`_same_origin_json()`（同文件 1818）是给本站前端防 CSRF 的，要求
  `X-Panel-Request: 1` 和同源 Origin —— 另一台机器上的进程两样都给不出，硬凑只会把这道门变成摆设。
  所以单开一条，参照 bot 仓库 `core/feishu_bot/routes.py` 里 `/api/ram/user`（`_ram_api_authorized`）
  和 `/gpu/distribution`（`_gpu_dist_authorized`）那两个专用 token 门禁的写法：**只认专用 token，
  绝不回退到别的已有密钥**。
- 令牌配置：`DELIVERY_SERVICE_TOKENS_FILE` 指向一个 JSON（600，属主 `delivery`）：

  ```json
  {"mlflow": {"tokens": ["<新>", "<旧>"],
              "redirect_uri": "https://tensorboard.wuji-tech.com/oauth2/callback"}}
  ```

  - `tokens` 是数组，为的是**轮换期间新旧并存**：写新的 → 改代理 → 删旧的，全程不断线。
  - 比对用 `hmac.compare_digest`，不用 `==`。
  - **令牌本身就决定 service**：认出是哪个令牌，就只能问那个 service。请求体里的 `service`
    必须与之相等，否则 403。不这样的话，mlflow 的令牌泄漏就能查所有服务的授权名单。
  - **文件没配 → 这个端点对所有请求回 503**，不是「放行」也不是「404」。（和 `_feishu_approval`
    未配 verify token 时一律拒绝同一个取向。）
  - 文件按 mtime 缓存重读（面板 `Backend._cached` 已有这个套路）：轮换令牌 = 改文件，**不用重启服务**。

- **redirect_uri 由面板从配置里取，不从请求体读。** 少一个字段、少一条可被构造的输入；
  换 token 时必须和 authorize 时逐字一致，写死在面板这边反而不会漂。

### 4.2 请求体

```json
{"code": "<飞书一次性授权码>", "service": "mlflow"}
```

- **入参是一次性授权码，不是用户标识。** 代理不再自己换 token，所以它手里没有 union_id。
- **防重放要面板自己做，不依赖飞书。** 飞书的 code 本来就是一次性的，但「对方那边也管着」
  从来不是一道门。面板侧：记下已用过的 code 的哈希（`sha256`，不存原文），TTL 10 分钟，
  进程内字典 + 定期清理（面板是单进程，重启即忘 —— 可接受：飞书 code 本身 5 分钟过期）。
  重复提交同一个 code → 400 `code_invalid`，**不去打飞书**。
- 失败计数按来源 IP，复用 `_pickup_too_many_tries` / `_pickup_record_failure` 那套（`server.py:268`）。
  令牌是 32 字节随机数，猜不出来，但一个能无限重试的公网 POST 口不该留着。
- 请求体上限沿用 `_json_body` 的默认值即可，code 只有几十字节。

### 4.3 响应

**允许**（HTTP 200）：

```json
{"allowed": true, "union_id": "on_xxx", "name": "张三", "expires_in": 43200}
```

**不允许**（HTTP 200，`allowed:false`）：

```json
{"allowed": false,
 "reason": "no_grant",
 "name": "张三",
 "apply_url": "https://cloud.wuji-tech.com/#requests",
 "message": "你还没有 MLflow 的使用权限。到云权限面板提一张「MLflow 使用权限」申请，审批通过后直接登录即可，不用再找人。"}
```

拒绝也回 200：它是业务结果，不是错误。代理按 `allowed` 分支，不用去猜状态码。

**只给这几个字段。明确不给**：邮箱、手机号、工号、部门、所在云账号、是不是管理员、
申请单号、名册里的任何其它字段。调用方是另一台机器上的进程，给多了就是白给。
（`name` 可以给：它是登录者本人的名字，代理原先自己换 token 时一样拿得到，MLflow 页面上要显示。
邮箱**不给** —— 如果对方证明代理真的需要邮箱才能建会话，单开一条评审，不要顺手加。）

`reason` 三态，**分开三种文案，不能混**：

| reason | 什么情况 | 文案要说什么 |
|---|---|---|
| `no_grant` | 名册里有这个人，但没有有效的 MLflow 授权（没申请过 / 到期 / 被撤销） | 去申请，给链接 |
| `not_in_roster` | 名册里查不到这个 union_id | 名册还没同步到你，把 union_id 发给管理员登记 —— **不是**让他去申请 |
| `blocked` | 名册判冲突，或离职流程已停用 | 联系管理员，别给申请链接 |

「查不到这个人」既不能当成允许，也不该和「不允许」共用一句话：前者是名册没同步（管理员的事），
后者是他没申请（他自己的事）。说错了，人会照着错的那条路走一圈才发现走错。

### 4.4 错误分类（三种必须分得清）

| HTTP | `error` | 含义 | 代理该做什么 |
|---|---|---|---|
| 400 | `code_invalid` | code 无效 / 过期 / 已经换过一次 | 让用户重走一次 `/oauth2/start`，**但要带失败计数**（见下） |
| 400 | `bad_request` | 缺字段、service 不认识 | 配置错了，渲染错误页，别重定向 |
| 401 | `bad_token` | 服务令牌不对 | 配置错了，渲染错误页，**绝不重定向** |
| 403 | `service_mismatch` | 令牌与 service 对不上 | 同上 |
| 502 | `upstream` | 飞书那边超时 / 5xx | 渲染「登录服务暂时不可用，稍后再试」，**不要**自动重定向 |
| 503 | `not_configured` | 面板没配服务令牌 | 同上 |
| 500 | `server_error` | 面板自己炸了 | 同上 |

**死循环防护是必须的**：`code_invalid` → 重新登录 → 又失败 → 再重新登录，用户会看到浏览器
无限跳转。代理侧必须对「因失败而重走登录」计数（同一浏览器会话 2 次就停下来显示错误页）。
这条写进第 6 节，它是最容易漏、最难排查的一条。

### 4.5 面板不可用时怎么办

**新用户一律拒绝（fail-closed）。** 理由：这道门唯一的作用就是挡人，挂了就放行等于它从不存在；
而 MLflow 内部没有任何权限模型（第 8 节），放进去一个不该进的人，他能改能删所有人的实验。

**但已经登录的人不受影响**：代理认证通过后签发**自己的**会话（cookie），有效期建议 **12 小时**，
期间不再问面板。所以面板重启、抖动、部署，只影响「那几分钟里第一次登录的人」。

- 面板挂掉期间新人进不去 —— **可接受**，写进已知限制。面板本身就是公司申请权限的唯一入口，
  它挂了的时候本来也没法申请。
- 会话 12 小时是权衡：会话越长，撤销生效越慢。12 小时 = 一个工作日内重认一次，
  撤销后最坏 12 小时生效（第 5.5 节会再说一遍，撤销页面上要写出来）。
- **不要在代理侧另外缓存「允许/拒绝」的结果再复用给下一次登录**：那等于把会话长度悄悄加倍。
  缓存就是会话本身，一处。

## 5. 面板侧：申请 → 自动开门

### 5.1 新增一个 kind，不复用现有的

**推荐：新增 `KIND_SERVICE = "service"`（标签「服务访问」）。**

不能走 `AWAIT_FULFIL`（`catalog.py:66`，`KIND_RESOURCE`/`KIND_STORAGE`/`KIND_TRANSFER` 审批后
停在「待开通」等人点）—— 用户明确不要手工登记，这条直接排除。

不复用 `KIND_PERMISSION` 的理由（这条最容易被将来的人翻出来重提，写足）：

- 它绑死在云上：`_run` 那一支（`flows.py:2121`）拿 `payload["cloud_user"]` 去 `add_to_group` /
  `attach_policy`，`_revoke` 那一支反着来。MLflow 的访问权和 RAM 用户毫无关系。
- 它要求申请人**先有云子账号**：`options()` 第 504 行，没有子账号的模板一律置灰成
  「你在这个云账号下还没有子账号，先申请开账号」。而最需要 MLflow 的人里，有相当一部分
  根本没有云账号 —— 复用等于把他们挡在门外。
- 加载期就拦：权限模板「至少要有一个用户组，或者配一个 workspaces」（`catalog.py:930`）。
  一个既没有组也没有工作空间的 permission 模板根本加载不进去，得为它开第三种形状 ——
  那已经是「一个穿着权限外衣的新 kind」了，不如直接建。

### 5.2 牵动面（列全，漏一处的表现各不相同）

**`src/delivery/catalog.py`**
1. `KIND_SERVICE` 常量 + 进 `KINDS` + `KIND_LABELS`（「服务访问」）。
2. **不**进 `AWAIT_FULFIL`；`awaits_human()` 对它返回 False（走默认分支即可）。
3. `_BY_KIND[KIND_SERVICE] = {"service", "max_days"}`。
4. 新字段 `service: str = ""`（服务键，如 `mlflow`），正则 `^[a-z][a-z0-9-]{1,31}$`，
   KIND_SERVICE 必填、其它 kind 不许填。
5. **`platform` / `account` 的校验要按 kind 放开**：现在 `platform` 必须在 `platforms_mod.IDS`
   （只有 aliyun/volcano）、`account` 必须是 6–20 位数字。自建服务两样都没有。
   推荐做法：KIND_SERVICE 要求 `platform == "internal"`（在 `platforms.NAMES` 里加一条
   「自建服务」，**不进 `ALL`/`IDS`** —— `jiuzhang` 已经是这个先例），`account` 对它**不必填**
   （`_str(..., required=(kind != KIND_SERVICE))`，填了仍走原正则）。
   *被否的做法*：借一个真实云账号 ID 当占位。台账和审批单上会显示「阿里云 · 某账号」，
   而这张单和那个云账号毫无关系 —— 在台账里说假话，日后对账的人要为此查半天。
6. `public()` 带出 `service`；`max_days` 的那一行判断要把 KIND_SERVICE 加进去
   （现在是 `if self.kind in (KIND_PERMISSION, KIND_RESOURCE)`）。

**`src/delivery/flows.py`**
7. `_EXEC_FIELDS`（第 75 行）**加上 `service`**。不加的后果是安全性的：审批期间管理员把模板的
   `service` 从 `mlflow` 改成别的，这张单照开 —— 审批人批的是 A，开出来的是 B。
   `Template.service` 有 dataclass 默认值 `""`，所以 `_FIELD_DEFAULTS`（第 160 行）自动兜住旧快照，
   **不会**出现「部署当天在途单子全报模板被改」。
8. `_approval_fields()`（236 行）加分支：送 `{"scope": "<服务名>", "valid": "<天数>"}` 之类。
   **不要送 `account`** —— 审批定义里「云账号」是单选控件，送 `internal/` 匹配不到任何选项。
9. `_run()`（2057 行）加分支：**不碰云，直接返回一句台账文案**，随后状态机置 DONE。
   授权在哪见 5.3。
10. `options()`：第 489 行（执行身份没配就置灰）和第 504 行（没有子账号就置灰）**都要给
    KIND_SERVICE 放行**。漏了的表现是模板永远灰着、没人能申请，而页面上给的理由
    （「先申请开账号」）会把人引到一条完全无关的路上。
11. `_expires_at()`（1344 行）把 KIND_SERVICE 纳入按 `max_days` 算到期。
12. `revoke_expired()`（1792 行）的 kind 白名单加它；新增 `_revoke_service()` ——
    只改状态，不调任何云接口。
13. `revoke_now()`（1834 行）目前只认凭证单；要么放开给服务单，要么另给一个入口（见 5.5）。

**`src/delivery/todo.py`**：`collect_tickets` / `collect_expiring` 的分类里加上它
（否则服务单的到期不会出现在管理员待办上）。

**前端 `src/delivery/web/requests.js`**：`KIND_ORDER` / `KIND_INFO` / `KIND_KEY_LABEL` 三张表
（第 13–23 行）、表单分支、预览文案。`accountLabel()`（64 行）在 `account` 为空时会渲染出
一个尾随的 `·`，顺手处理。

**`identity/request-templates.json`**：加一条 MLflow 模板。**这个文件是活数据，只能合并、
绝不整份覆盖**，而且服务器上改它必须 `sudo -u delivery`（root 写会把属主改成 root，
面板从此读不了，且没有任何告警）。

**飞书审批定义（控制台，人工一次性）**：「申请类型」那个单选控件要**先加一个 `service` 选项**。
不加的后果参照 2026-09-18 那次（`approval._field` 撞单选控件 fail-closed，拒发整张单子）。
顺序是**审批定义先于代码上线**，和 `MAX_CREDENTIAL_HOURS` 那条「模板必须先于代码」同类。
不需要新建审批定义 —— 面板所有 kind 共用一个 `approval_code`，字段按 `custom_id` 映射、
定义里没有的键静默跳过（`approval.py:357`）。

### 5.3 授权记在哪：就是申请单台账，不另存一份

**推荐：不新建 `identity/service-access.json`。** 判据是「存在一张
`kind=service`、`template.service=mlflow`、`applicant.union_id=本人`、`status=DONE`、未过期
的申请单」。`tickets.Store.mine(union_id)` 已经是按 `applicant.union_id` 严格相等取单的
（`tickets.py:160`，空 union_id 直接返回空 —— 这道门已经在了）。

理由：

- **单一真相源。** 另存一份名单就有两处真相，而两处真相只会被修一边。撤销、到期、重开、
  状态机回滚，每一条路都要记得同步那份文件，漏一条就是「单子已撤销，人还能进」。
- 热路径不慢：面板 `Backend._cached(name, stamp, build)` 是按**文件 mtime** 做键的现成缓存，
  用 `tickets.json` 的 mtime 建一张 `{(service, union_id): 到期时间}` 索引即可。
  台账一变自动失效，不用手动清。
- 应急可查：出问题时看的是申请单，和管理员在页面上看到的是同一个东西。

判定逻辑抽成一个纯函数模块 `src/delivery/service_access.py`：
`allowed(tickets, roster, offboard_rows, *, union_id, service, now) -> (bool, reason)`，
不碰 HTTP、不碰文件，可以完整单测。server 那一层只负责换 token、认令牌、拼响应。

### 5.4 到期与离职回收

- **有效期**：模板配 `max_days`。建议 **365 天**（一年一复核）。`max_days: 0`（不限）在
  schema 上是允许的，但这个模板别这么配 —— 不限期的访问权没有任何时刻会被重新看一眼。
  到期前的分级提醒走现成的 `remind_expiring()`，不用新写。
- **到期**：`revoke_expired()` 走 `_revoke_service()`，只把单子置 REVOKED。没有云资源要收，
  所以这一步不会失败、不需要重试逻辑。
- **离职**：**要接，而且接在判定这一侧，不是「离职时记得去撤单」那一侧。**
  `service_access.allowed()` 除了看单子，再查一道离职记录：这个 union_id 在 `offboard.json` 里
  是 `disabled` 或 `deleted` → 直接 `blocked`，不看他有没有有效单子。
  （`offboard.py:439/453` 的记录里带 `union_id`，够用。dev 上手前请确认无云账号的人被记 suspect
  时这个字段也填得上 —— 见第 11 节开放问题。）
  理由：靠「离职时记得去撤单」的流程，漏的那一次没有任何人会发现。放在判定处是 fail-closed 的同一处判断。
  另外在离职的管理员卡片/待办上列出「他还有哪些服务访问」，让确认删号的人知道会连带关掉什么。

### 5.5 撤销入口与生效时延

- 管理员：申请单详情页上的「撤销」按钮（服务单的 `revoke_now` 分支）。
- 申请人本人：可以撤自己的（`revoke_mine` 已有归属检查，按 union_id 严格相等）。不加提权风险，
  撤销只会减少权限。
- **生效时延要显式写在按钮旁边**：「撤销后，对方最长还能用 12 小时（他那边的登录会话有效期），
  之后再登录就进不去了。要立刻切断，只能去那台机器上踢会话。」
  不写的话，管理员点完看到 200 就以为人已经被挡在外面了 —— 而这个按钮最要紧的使用场景
  恰恰是「现在就要把人挡住」。
- 「立刻踢下线」本期**不做**（要代理提供一个额外接口）。列进不做清单。

## 6. 给代理那边的需求说明（这一节可以整页转发）

> **背景**：MLflow（`tensorboard.wuji-tech.com`）现在是「飞书认证过就放行」。我们要把「谁能用」
> 交给云权限面板（`https://cloud.wuji-tech.com`）的申请审批来决定。用户申请、审批通过，
> 门自动开，中间没有人工登记这一步。
>
> **要改的只有回调之后那一步。**`/oauth2/start` → 飞书 authorize 那一跳**不用动**
> （它只要 app id 和 redirect_uri，不需要 app secret）。
>
> **改成这样**：`/oauth2/callback` 拿到 `code` 之后，不要自己去飞书换 token，
> 改成把 code 交给面板：
>
> ```
> POST https://cloud.wuji-tech.com/api/service-access/exchange
> Authorization: Bearer <我们发给你的服务令牌>
> Content-Type: application/json
>
> {"code": "<回调里的 code>", "service": "mlflow"}
> ```
>
> 面板用它自己的 app secret 换 token、拿到 union_id、查名册和授权，回：
>
> - `200 {"allowed": true, "union_id": "...", "name": "...", "expires_in": 43200}`
>   → 建你自己的会话放行。
> - `200 {"allowed": false, "reason": "...", "name": "...", "apply_url": "...", "message": "..."}`
>   → 渲染一页拒绝页，把 `message` 原样显示，`apply_url` 做成链接（`reason` 为
>   `not_in_roster` / `blocked` 时面板不会给 `apply_url`，那时只显示 `message`）。
>
> **为什么要你把 secret 交出来**：那个飞书应用不只用于登录 —— 面板拿它取 tenant token、
> 读公司通讯录、以管理员身份私聊人。secret 放在你那台机器上，等于那台机被拿下就能读通讯录、
> 以面板身份给任何人发消息。改完之后你手里只有一个调面板的令牌，它泄了最多能查
> 「某人能不能用 mlflow」。
>
> **会话**：认证通过后**你自己签会话**（cookie），建议 12 小时，期间不要再问面板。
> 这样面板重启或抖动只影响「那几分钟里第一次登录的人」。
> 不要在会话之外再缓存一份「允许/拒绝」的结果 —— 那等于把会话时长悄悄翻倍，
> 而撤销权限的生效时间取决于这个会话长度。
>
> **面板不可用时（超时 / 5xx / 连不上）：拒绝，不要放行。** 这道门唯一的作用就是挡人，
> 挂了就放行等于它从不存在。已经登录的人不受影响（他们的会话还在）。
> 拒绝页文案写「登录服务暂时不可用，稍后再试」，**不要**自动跳回飞书重登。
>
> **错误分三类，反应不一样**：
>
> | 面板回 | 你该做什么 |
> |---|---|
> | 400 `code_invalid` | code 过期或已用过。让用户重走一次 `/oauth2/start`，**但要计数：同一浏览器会话连续 2 次就停下来显示错误页**，否则会无限跳转 |
> | 401 `bad_token` / 403 `service_mismatch` / 400 `bad_request` | 配置问题。直接显示错误页，**绝不重定向** |
> | 502 `upstream` / 503 / 500 | 面板或飞书暂时不可用。显示错误页，不重定向 |
>
> **令牌**：我们会单独给你，放在你那台机器上一个 600 权限的文件里，别写进代码仓库、
> 别写进 nginx 配置、别打进日志。我们轮换时会先给你新的，你改完我们再废旧的，不会断线。
>
> **HTTPS 证书校验必须开**（别 `verify=False`）。请求超时建议 5 秒，失败按上面那张表处理。
>
> **不要记日志的**：`code`、令牌。可以记：union_id、允许/拒绝、原因。
>
> **另外**：MLflow 自己没有权限模型，进去的人都是管理员，能看能改能删所有实验。这次做的是
> 「能不能进」这道门，进去之后的事这套方案管不了。

## 7. 安全

- **飞书应用共用面板那个（`cli_aa2d10ccd0b9dbb7`），不另建** —— 用户已拍板。风险靠
  「secret 不出第二台机器」这个形状化解（第 3 节），不靠再建一个应用。
- **服务令牌**：`python3 -c "import secrets;print(secrets.token_urlsafe(32))"` 生成。
  存 `/etc/cloud-panel/service-tokens.json`，属主 `delivery`、权限 600，
  systemd 里用 `DELIVERY_SERVICE_TOKENS_FILE` 指过去。
  **轮换**：文件里 `tokens` 是数组 → 写入新令牌（两个并存）→ 通知对方改 → 删旧的。
  文件按 mtime 重读，不用重启服务。
- **日志**：绝不记 code、绝不记令牌。要记 `service` + `union_id` + `allowed` + `reason`
  —— 这是「谁在什么时候进了 MLflow」的唯一审计记录，别省。
- 面板现有的 `log_message` 已经被覆盖成空（`server.py:1384`，默认实现会把含授权码的完整 URL
  打到 stderr）。新端点在那条路径上，天然受益，但新写的日志别自己把 code 打出来。
- 端点必须在**会话鉴权之前**分流（它没有 cookie、没有 Origin），别让 `_same_origin_json`
  或 `_require` 半路拦下来。

## 8. 已知限制（要写进申请页文案，不是藏在文档里）

- **MLflow 没有自己的权限模型。** 批准之后这个人在 MLflow 里就是管理员：所有实验、所有 run
  都能看、能改、能删（包括别人的）。这套方案只做「能不能进」这一道门。
  申请页的说明里必须直说 —— 别让人以为申请到的是「只读」。
- 撤销 / 到期后，对方那边最长还能用满一个会话（建议 12 小时）。
- 面板不可用期间，**新登录一律进不去**（已登录的不受影响）。
- 审批走的是现有那条飞书审批定义，审批人和别的申请是同一批。要单独的审批人，
  需要在飞书那边另配条件分支 —— 本期不做。

## 9. 任务拆解

工作量是粗估，单位：0.5 天 = 半个工作日。

| ID | 任务 | owner | 依赖 | 估 | 验收标准 |
|---|---|---|---|---|---|
| SA-1 | 确认代理那边愿意按第 6 节改（谁维护、排期、有没有别的约束） | dev（找人确认） | — | 0.5 | 对方书面认下这个形状；拿到对方的联系人和上线窗口 |
| SA-2 | `service_access.py` 纯逻辑：`allowed(tickets, roster, offboard, union_id, service, now)` | dev | — | 0.5 | 三态（`ok`/`no_grant`/`not_in_roster`/`blocked`）各有明确出口；不碰 HTTP、不碰文件；到期、已撤销、已关闭的单子一律不算数 |
| SA-3 | SA-2 的单测 | tester | SA-2 | 0.5 | 覆盖：无单 / 有效单 / 到期单 / 已撤销 / 名册无此人 / 离职 disabled / 离职 deleted / 同人多张单取最晚到期 / union_id 为空必拒 |
| SA-4 | 新 kind `service` 落地（`catalog.py` 全部 6 处 + `_EXEC_FIELDS` + `_approval_fields` + `_run` + `options()` 两处放行 + `_expires_at` + `revoke_expired` + `_revoke_service`） | dev | — | 1.5 | 一条 MLflow 模板能被 `catalog.load` 接受；提交一张单能走到 DONE 且不碰任何云接口；`options()` 对**没有云账号**的人显示「可申请」 |
| SA-5 | SA-4 的单测 | tester | SA-4 | 1 | 含变异检查：把 `service` 从 `_EXEC_FIELDS` 拿掉、把 KIND_SERVICE 塞进 `AWAIT_FULFIL`、去掉 `options()` 的放行 —— 每一项都要有用例变红。另测旧快照（没有 `service` 键）不报「模板被改」 |
| SA-6 | 令牌门禁 + `/api/service-access/exchange` 端点（含换 token、防重放、失败计数、错误分类） | dev | SA-2, SA-4 | 1.5 | 无令牌 503/401、错令牌 401、service 对不上 403、同一 code 两次 400 `code_invalid` 且第二次不打飞书；响应体字段与 4.3 逐字一致 |
| SA-7 | SA-6 的单测 | tester | SA-6 | 1 | 飞书调用打桩；断言响应里**不含**邮箱/工号/部门/云账号/管理员标识（这条要写成显式断言，不是靠肉眼） |
| SA-8 | 前端：kind 三张表 + 申请表单 + 预览文案 + 申请单详情页的「撤销」和时延提示 | dev | SA-4 | 1 | 申请页能选到「服务访问 · MLflow」并提交；说明里有第 8 节第一条（全员管理员）；撤销按钮旁写明生效时延 |
| SA-9 | 飞书审批定义加 `service` 选项 + 发一张真实测试单验证 | dev（控制台） | — | 0.5 | 真发一张单，确认「申请类型」渲染成中文、不报 fail-closed。**先于 SA-4 上线** |
| SA-10 | 生成服务令牌、配置文件、systemd 环境变量、上线 | dev | SA-6 | 0.5 | `curl` 用真实 code 打通一次（拿 code 的方法：浏览器手动走一次 authorize，从跳转地址栏抄 code，30 秒内 curl）；令牌文件 600 且属主 `delivery` |
| SA-11 | 把第 6 节发给代理维护方，跟进改造与联调 | dev | SA-10 | 1 | 端到端：一个没申请过的人登录 → 看到拒绝页 → 点链接申请 → 审批通过 → **不做任何手工操作**直接重登成功 |
| SA-12 | 安全复审 | auditor | SA-6, SA-8 | 0.5 | 无阻塞项。重点看：响应体最小化、防重放、fail-closed、令牌比对是否常量时间、日志里没有 code/令牌、`_EXEC_FIELDS` 是否真的挡住模板被改 |
| SA-13 | 离职联动：`allowed()` 查离职记录 + 离职卡片列出服务访问 | dev | SA-2 | 0.5 | 把某人标成 `disabled` 后，他的服务访问立刻判 `blocked`（不依赖撤单）；有单测 |
| SA-14 | 文档：`docs/` 里写清这条链路、令牌轮换步骤、撤销时延 | docwriter | SA-11 | 0.5 | 别人照着能独立完成一次令牌轮换 |

**提交闸门（硬规）**：任何改动必须 auditor 审计通过、无阻塞项才能 `git commit`。SA-12 不是可选项。

### 并行与顺序

- **能并行**：SA-1（找人）、SA-2（纯逻辑）、SA-4（kind）、SA-9（飞书控制台）互不依赖，一起开。
- **必须串行**：SA-9 **先于** SA-4 上线（审批定义先于代码，和 `MAX_CREDENTIAL_HOURS` 那条同类教训）；
  SA-6 要等 SA-2 + SA-4；SA-11 要等 SA-10。
- **面板侧可以整条先上、代理先不动**：SA-2/4/6/8/10 做完，用 curl 就能验完整条链路，
  这期间 MLflow 行为一字不变。对方什么时候改都行，不卡我们。

### 先做什么

1. SA-9（飞书审批定义加选项）—— 它是别人的系统、有等待成本，而且必须排在代码前面。
2. SA-2 + SA-4 —— 判定逻辑和 kind 是后面所有东西的地基。
3. SA-6 + SA-10 —— 接口能 curl 通，这一刻起就可以把第 6 节发出去了（SA-11 可以并行等对方）。
4. SA-8 / SA-13 / SA-12 收口。

## 10. 不做什么（明确排除，别在评审里重开）

- **不给 MLflow 单建飞书应用** —— 用户已拍板共用面板那个，靠「secret 不出第二台机器」化解风险。
- **不接 Authentik / 公司 IAM** —— 它不在这条链路上（线上是飞书登录，IAM 只有模板没启用）。
- **不让面板往 `39.108.82.208` 写白名单文件** —— 那要新开一条入站写入通道，是新的攻击面。
- **不做 MLflow 内部的细粒度权限**（只读 / 按实验授权）—— MLflow 没有这个模型，做不了。
- **不做「立刻踢下线」** —— 要代理再提供一个接口，本期不加。撤销的生效时延写进文案。
- **不新建 `identity/service-access.json`** —— 见 5.3，不制造第二处真相。
- **不在响应里给邮箱** —— 除非对方证明建会话离不开它，届时单独评审。

## 11. 开放问题（交 dev 去问用户 / 对方）

1. **谁维护那个代理？**排期怎么算？它是自己写的还是改的某个开源件？（影响 SA-11 的工期，
   也影响第 6 节要写得多细。）
2. **会话 12 小时能接受吗？**它直接等于「撤销后最长还能用多久」。要更快就得缩短会话，
   代价是用户每天多登几次。
3. **有效期 365 天合适吗？**还是「不限期 + 每年体检里提醒复核」？前者是自动的，后者靠人。
4. **`offboard.json` 里对「没有任何云账号的人」还会不会记录、`union_id` 填不填得上？**
   SA-13 的正确性依赖这一点。dev 上手前花十分钟核实，别猜。
5. **申请页深链格式**：`requests.js` 有按 id 选中模板的逻辑（第 106 行），确认一下
   `#requests?template=...` 这类锚点能不能直接用；拿不准就先给申请页根地址，别给一条打不开的链接。
6. **还有哪些自建服务要走同一条路？**（Grafana？JupyterHub？）本期只上 MLflow，但如果
   一个月内还有第二个，`service` 这个维度的设计现在就要多看一眼。

## 12. 上线步骤（部署惯例，别漏）

1. 飞书审批定义加 `service` 选项（SA-9），发测试单确认。
2. 服务器上 `sudo -u delivery` 编辑 `identity/request-templates.json`，**合并**加入 MLflow 模板
   —— 这是活数据，**绝不整份覆盖**。
3. 生成服务令牌，写 `/etc/cloud-panel/service-tokens.json`（属主 `delivery`，600）。
4. 同步代码：面板的惯例是**只同步 `src/`**。
5. 加 `DELIVERY_SERVICE_TOKENS_FILE` 到 panel.env → **`systemctl restart cloud-panel`**
   （环境变量变了，restart 不是 reload）。
   本方案**没有**新增 systemd 单元或 timer；如果后续加了，记住 `deploy/panel/*.service`
   这类要单独拷到服务器 + `systemctl daemon-reload`，只同步 `src/` 拷不过去。
6. `curl` 验一次真实 code（方法见 SA-10 的验收标准）。
7. 本地绿不算数：上线后在**线上**再验一遍拒绝页和放行路径。
