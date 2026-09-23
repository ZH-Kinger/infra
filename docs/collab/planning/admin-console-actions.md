# 云权限面板 · 管理员可执行动作补齐

状态：待 dev 评审 → 拆板执行
适用仓库：`/home/l/桌面/infra`（`src/delivery/`），线上 `120.79.167.166:/opt/infra`，`cloud-panel.service`，公网 `https://cloud.wuji-tech.com`，登录方式是**飞书 OAuth**
写作依据：只读通读 `src/delivery/`（provision / grants / flows / requests_api / server / offboard / todo / review / platforms / roles）、`deploy/panel/cloud-policies/*.json`、前端 `web/index.html` `web/app.js` `web/requests.js`；凭证改写的参照实现是 bot 仓库 `/home/l/桌面/langchaindev/core/temp_ak_issuance/`
配套文档：`docs/collab/planning/admin-console-rework.md`（信息架构与待办页，已落地 AC-1/2/4/5 那批）。本文不重复它，只补它没覆盖的那一层：**检测报出来之后能点什么**

用户原话是「管理员的功能很少呀」。后台九页里能改东西的只有：确认/丢弃人↔账号推断、改云资产归属、人工登记九章账号、收权、改权限规则、催办、改 IAM 属性表、申请单上的确认/丢弃/对账/稍后处理/回收/离职删号或恢复。
**检测很全，报出来之后全是死胡同。** 本批补四样：凭证运维、孤儿子账号清理、单个云账号停用/启用/删除、人员画像页。

---

## ① 现状盘点：这四样今天卡在哪

### 1.1 凭证发出去之后就改不了了

真实案例（今天）：同事的 lakeFS 服务凭证连不上，根因是发放时的策略缺 `oss:GetBucketLocation` —— S3 兼容客户端建连先探地域，探不到报一个和权限八竿子打不着的错。

代码侧现状：

- `platforms.py:124-131` 的 `bucket_actions` **已经补上** `oss:GetBucketLocation`（火山那侧是 `tos:GetBucketLocation`）。也就是说**以后新发的凭证是对的**。
- 但 `provision.py:603 issue_long_term` 只在发放那一刻 `CreatePolicy` + `AttachPolicyToUser` + `CreateAccessKey`，**全仓没有任何一条重写已发策略的路径**。已经发出去的那把凭证，策略文档永远停在发放当天的版本。
- 结果就是用户说的那句：「唯一的修法是我写个 python 脚本、让用户 ssh 上服务器跑」。面板自己发的凭证，面板却改不了。

这件事不止 `GetBucketLocation` 一个。`platforms.py` 的动作表是**经验值、会继续演进**（注释里已经记了 `GetObjectVersion` 那次真机踩坑）。每演进一次，存量凭证就多欠一笔。

### 1.2 申请单上已有和缺的动作

`requests_api.ticket_view`（`requests_api.py:171-209`）决定按钮，`_route`（`requests_api.py:386-433`）决定接口：

| 动作 | 接口 | 前端 | 状态 |
|------|------|------|------|
| 重试开通 | `POST /api/admin/requests/<id>/retry` | 有 | 已有 |
| 补写登录名 | `…/push_iam` | 有 | 已有 |
| 恢复卡住的单 | `…/recover` | 有 | 已有 |
| 登记资源 | `…/fulfil` | 有 | 已有 |
| 关闭 / 重开 | `…/close` `…/reopen` | 有 | 已有 |
| **作废凭证** | `…/revoke` → `flows.revoke_now` | 有（`requests.js:882`） | 已有，**但删不干净时没有任何展示** |
| **重算并下发策略** | — | — | **缺** |
| **改有效期** | — | — | **缺** |
| **改权限（caps）** | — | — | **缺** |
| **重发凭证（换 AK）** | — | — | **缺** |

作废那条：`flows._revoke_credential` 调 `provision.revoke_long_term`，后者返回「没删掉的东西」列表（`provision.py:634-682`，每步单独 try）。这个 `left` 会写进单子事件，但**前端不展示、没有重试入口**——于是「删不干净」这个最需要人看的状态，只有翻事件流才看得见。

### 1.3 孤儿子账号：能报、不能点

`todo.py:338-359` 的 `cred_orphan`（本周刚加，未提交）：单子已关/已失败、`cred_user` 还在 = 云上那个子账号和它那把长期 AK 还留着。`href` 指向 `#admin/requests`，但到了申请页上**没有对应按钮**：`ticket_view` 的 `revoke` 判据是 `status in (DONE, FAILED, CLOSED) and (cred_user or sealed.ciphertext)` —— 实际上这类单子是符合条件的，按钮**会**亮。

所以这条的真实缺口不是「没有按钮」，而是三条：

1. 待办页那一条点过去之后**不会筛到那几张单**（`href` 没带筛选参数，申请页默认筛 `open`，而这些单是 closed/failed，根本不在默认视图里）。
2. 定时任务（`flows.revoke_expired`）一直在试删，**删不掉的原因（`left`）在页面上看不到**——而「删不掉」才是要人介入的那一类。
3. 作废按钮的文案是「查看地址立刻失效，云上的子账号、密钥和策略一并删除」，对一张**已经关掉的单**说这句话会让人以为自己在作废一个还在用的凭证。

### 1.4 停用/启用/删除单个号：只有离职流程能调

`provision.py` 两朵云都齐了：`disable_user`（489/981）、`enable_user`（524/1021）、`delete_user`（541/1038）。

调用方**只有一个**：`offboard.auto_disable`（定时任务）和 `offboard.decide`（面板上「确认删除」/「恢复」按钮，走 `server.py:2119/2135` 和飞书卡片 `server.py:1275`）。

也就是说：管理员想停一个号，必须先让离职检测把它判成离职。一个「这个号泄漏了、先停掉」的正常运维动作，今天在面板上做不了。

**两道防线必须原样继承**：

- 代码侧 `offboard.protected(user, platform)`（`offboard.py:63`），`_PROTECTED_CLOUD` 挡 `panel-|power-|tempak|staff-|temp-ak-|wuji-|rl-|finance|data-tran`。
- 云侧 `deploy/panel/cloud-policies/aliyun.wuji-panel-executor.json:124-180` 和 `volcano.wuji-panel-executor.json:60-84` 里两段 Deny，挡同一批名字。

新入口**必须走同一个 `protected()`**，不能另起一条判断。

### 1.5 人员信息散在四页

一个人的完整状态今天要翻四页：

| 信息 | 在哪 | 数据源 |
|------|------|--------|
| 有哪些云账号、挂了什么策略、高危几条 | 人员与名册 → `#person=` | `views.person_detail` + `inventory` 快照 |
| 九章账号 | 人工登记 | `offline_accounts` |
| 离职状态、待确认删除 | IAM 属性表 | `offboard.json` |
| 公司 IAM 属性、对账差异 | IAM 属性表 | `iam_sync` |
| 他名下的凭证单 | 申请与开通（按申请人筛） | `tickets.json` |

`GET /api/admin/person/<key>`（`server.py:1686-1704`）已经返回账号卡片 + 待确认对应，**零云调用**（照 `_todo_view` 的规矩，`server.py:334`）。缺的是把另外四份本地数据并进来，以及每一项旁边的动作按钮。

---

## ② 凭证运维：一条管道，四个动作

### 2.1 为什么是「改策略新版本」而不是重发

重发会换 AK，对方的服务配置要跟着改一遍（lakeFS 这种服务凭证尤其难改，要停服）。而权限和有效期这两样**全写在那条自定义策略里**：

- `grants.build_policy`（`grants.py:180`）产出的每条 statement 都叠 `DateGreaterThan`/`DateLessThan` 时间窗；
- 能力集 `list` / `download` / `write` 决定出现哪几条 statement；
- 策略名由 `grants.policy_name(cred_user)` 唯一确定（`grants.py:128`）。

所以**改策略 = 改权限 + 改有效期，AK 一个字都不用动**。云上落法：

| 云 | 接口 | 现有 issuer 策略里有没有 |
|----|------|------------------------|
| 阿里 | `CreatePolicyVersion(SetAsDefault=true, RotateStrategy=DeleteOldestNonDefaultVersionWhenLimitExceeded)` | **有**（`aliyun.wuji-panel-issuer.json:42-64`，含 Create/Delete/List/GetPolicyVersion） |
| 火山 | `UpdatePolicy`（原地替换文档，火山没有版本概念） | **有**（`volcano.wuji-panel-issuer.json:20-32`，`iam:UpdatePolicy`） |

参照实现：bot 仓库 `core/temp_ak_issuance/issuer.py:172 rewrite_ram_window`，线上跑过，就是这套调用。

### 2.2 四个动作，共用一条管道

同一段代码，参数不同。按**风险从低到高**排，也按这个顺序上线：

| 动作 | 改什么 | 换 AK | 风险 | 驱动案例 |
|------|--------|-------|------|---------|
| **A1 重算下发（repair）** | 窗不变、caps 不变，只按**当前代码**重算策略文档并下发 | 否 | 最低（不扩不缩，只补齐代码里修好的动作表） | 今天的 lakeFS `GetBucketLocation` |
| **A2 改有效期** | 只改到期时间 | 否 | 中（要和单子的 `expires_at_ts` 对齐） | 续期、提前收 |
| **A3 改权限** | 改 caps（**只在模板 caps 范围内**） | 否 | 中（缩窄无风险，补齐要说明白） | 「只勾了上传，探不了桶」这类 |
| **A4 重发凭证** | 删旧 AK、发新 AK、重新 seal、给新取件链接 | **是** | 高（对方服务会断） | AK 疑似泄漏但权限范围不变 |
| **A5 撤销** | 删 AK + 摘策略 + 删策略 + 删用户 | — | 高（不可逆） | 已有，本批只补失败展示 |

**A1 单独拿出来是本方案的关键。** 它零参数、零扩权面，而今天那个真实故障正好只需要它。先上 A1，当天就能把 lakeFS 那把凭证修好，不用等 A2/A3 的表单和预演做完。

### 2.3 硬边界（写进代码，别只写在文档里）

1. **只有长期凭证能改。** `hours ≤ 12` 且模板配了 `role_arn` 的走 STS（`flows.py:2814`），云上**没有策略对象**、也没有子账号，A1–A4 一律不适用。判据：`cred_user` 非空。为空时接口直接 409，文案说清楚「这是 12 小时内的临时凭证，云上没有可改的策略，要变更只能重新申请」。

2. **caps 不许超出模板。** 模板 caps 是那张飞书批条批准的范围，面板单方面扩大 = 绕过审批。A3 只允许 `新caps ⊆ tpl.caps`。要更大范围 → 走新申请。

3. **`not_before` 只准往前、不准往后。** bot 那边踩过：延期表单常被填成「原到期日 → 新到期日」，直接采信会把生效时间推到未来，凭证在延期通过那一刻反而失效（`core/temp_ak_issuance/orchestrator.py:418-422` 的钳制就是为它写的）。

   **面板这边用更彻底的办法躲开它：表单上根本不提供「生效时间」这一栏。** `not_before` 恒取原值，A2 只改到期时间。坑从根上消失，不需要钳制逻辑，也不需要为钳制写测试。
   代价：「一张还没生效的凭证要整体挪档期」做不了 —— 那种情况撤了重申请，一年也遇不到一次。

4. **`not_before` 取不到就拒绝执行。** 单子上今天**没有**存生效时间（`flows._sign_credential` 的 `out` 里有 `not_before`，但没写回 ticket）。首次 A1/A2/A3 时：先 `GetPolicy` 读回当前默认版本的 `DateGreaterThan` 作为 `not_before` 并回写单子 `cred_not_before`；**读不回来就整个拒掉**，不许用 `now` 兜底 —— 用 `now` 会让一张「尚未生效」的凭证提前生效，那是扩权。

5. **单子上的 `cred_caps` 优先于模板 caps。** A3 改完之后模板没变，如果 A1 还读 `tpl.caps`，一次 repair 就会把刚改的权限覆盖回去。所以：**存 `cred_caps` 到单子，A1/A2/A3 一律读它，缺失时才回落模板。** 这条要有单测锁死（「A3 改完再 A1，caps 不回弹」）。

6. **改之前把旧策略文档存进单子事件。** 火山 `UpdatePolicy` 是原地替换，**没有回滚点**；阿里虽有版本，但 rotate 会删最老的非默认版本，也不能当回滚依赖。旧文档写进事件 note（几百字节，`store.update` 的 note 截到 500 字符 —— 太长就存进 `fields` 里一个 `cred_policy_prev` 字段）。改坏了照着它改回去，这是唯一的退路。

### 2.4 「页面说的」和「云上判的」怎么保证一致

改有效期要动两处：云上策略的时间窗、单子的 `expires_at_ts`。两处语义**不一样**，必须分清：

- **策略时间窗** = 什么时候开始 403（服务端逐次调用判，泄漏也随之失效）。
- **`expires_at_ts`** = 定时任务什么时候去删号（`flows._needs_reclaim`，`flows.py:1868`）。

没有分布式事务，所以定一条**顺序 + 一条对账**：

```
1) 单子记意图    事件 cred_regrant_requested，fields: cred_regrant_pending = {目标窗, 目标caps, actor, reason, at}
2) 写云          CreatePolicyVersion / UpdatePolicy
3) 单子写结果    expires_at_ts / expires_at / cred_caps / cred_not_before，清 pending，事件 cred_regrant_done
```

- **顺序恒为「先云后账」**，两个方向都安全：延长时若 3 失败，号会按旧到期时间被删（服务断，但不越权）；缩短时若 3 失败，云上已经 403（单子上的日期还是旧的，晚点删而已）。反过来先改单子的话，缩短会把号提前删掉、延长会让号在策略还没延之前被删——都更糟。
- **崩在 2 和 3 之间**：`cred_regrant_pending` 留在单子上 → 待办出一条新 kind `cred_window_drift`：「面板上写的有效期和云上判的可能不一致」，主操作是「按云上读回来对齐」（GetPolicy 读默认版本的时间窗，回填单子）。
- **对齐必须是人点的，不是定时任务自动改单子。** 自动回填会把一次失败的改动悄悄变成既成事实，而「有人改过但没改成」这条痕迹恰恰是事后最要紧的。定时任务只**报**，不改。

### 2.5 阿里云策略版本上限 5 个怎么处理

一条自定义策略最多 5 个版本，反复改必然撞上限。

**用 `RotateStrategy=DeleteOldestNonDefaultVersionWhenLimitExceeded`**，由阿里服务端在超限时删最老的非默认版本 —— 不自己写「先 ListPolicyVersions 再 DeletePolicyVersion」那套。理由：自己实现要多两次 API、多一处并发竞态（两个管理员同时改同一条策略），而阿里这个参数就是为这件事设计的，bot 仓库 `issuer.py:185` 已经这么跑在线上。

`ram:DeletePolicyVersion` 已在 issuer 策略里（`aliyun.wuji-panel-issuer.json:50`），rotate 需要它。兜底路径（rotate 失效时 `ListPolicyVersions` + 删最老非默认）**不写**，等真撞上再说 —— 权限是齐的，加起来很快。

火山没有版本概念，不存在上限问题，但也没有回滚点，见 2.3 第 6 条。

### 2.6 A4 重发凭证的额外约束

- 顺序必须是 **先发新 AK、再删旧 AK**（和 `issue_long_term` 的「AK 最后发」相反）：中间那一刻两把都能用，对方能平滑切；反过来会有一段完全不能用的窗口。
- 新凭证要重新走 `sealed.seal`（`flows.py:2696-2711`），旧的取件链接立刻失效 —— **这一点必须写在确认框里**，否则管理员会以为旧链接还能用。
- 单子上的 `orphan_ak_ids` 机制（`flows._record_cleanup`，`flows.py:2914`）要复用：删旧 AK 失败时把它记进去，别只留在事件里。

---

## ③ 孤儿子账号一键清理

`todo.py` 的 `cred_orphan` 报出来的是：单子已关/已失败、`cred_user` 还在。

**不新写删除逻辑**，复用 `provision.revoke_long_term`（删 AK → 摘策略 → 删策略 → 删用户，每步单独 try，返回没删掉的东西）。它已经被 `flows._revoke_credential` 调着，走的是 `POST /api/admin/requests/<id>/revoke`。

要补的三条：

1. **待办深链带筛选**：`href` 从 `#admin/requests` 改成 `#admin/requests?state=cred_orphan`（或直接带单号列表）。申请页默认筛 `open`，而这些单是 closed/failed，点过去看不到 —— 这是这条待办今天最直接的死胡同。
2. **失败原因上页面**：`revoke_long_term` 返回的 `left`（形如 `删 AccessKey …1a2b：EntityNotExist.User`）写进单子 `fields.cred_revoke_left`，在申请单详情和待办条目上直接显示。今天它只在事件流里。
3. **按钮文案分场景**：单子还开着 → 「作废凭证」（现有文案）；单子已关/已失败 → 「清掉云上残留的子账号」，说明写「单子已经结束了，云上那个子账号和它的长期 AK 还在。删的是账号本身，他桶里的文件一个不动。」

另外：`revoke_long_term` 全步成功但 `DeleteUser` 那步遇到 `EntityNotExist` 时会被当成功（`"NotExist" not in exc.code` 判据），这是对的 —— 目标达成。但**要在结果里说清「云上本来就没有这个号了」**，否则管理员会以为自己刚删了一个不存在的东西。

---

## ④ 单个云账号的停用 / 启用 / 删除

### 4.1 走哪条路

**不新开一条绕过 `offboard` 的路径。** 新接口做的事是：

```
停用：offboard.ensure_record(...signal="管理员手动停用") → 调 disable_user → 记 state=disabled
启用：offboard.decide(key, "restore", ...)              （已有，原样复用）
删除：offboard.decide(key, "delete", ...)               （已有，原样复用）
```

这样白拿四样东西，一样都不用重写：

- `protected()` 检查（`offboard.decide` 第 390 行、`targets_of` 第 163 行）
- `unverified` 拦截（第 392-398 行：名册里这个号不归这个人时拒绝删）
- 「只认记录里有的号」（第 386 行，不接受请求里随便给一个用户名——否则一个构造出来的请求就能删掉任何人的号）
- `review.log_offboard` 留痕（`review.py:530`）

**新增的只有 `ensure_record` 的手动入口**（该函数已存在，`offboard.py:438`，今天只被 IAM 对账那条路调）。`signal` 字段要能看出是人点的，写成 `管理员手动停用（<理由>）`。

### 4.2 先停用、确认后才删（用户硬规）

接口层面强制：`delete` **只接受 `state ∈ (disabled, suspect)` 的记录**（`offboard.decide` 第 388 行已经这么判了）。前端不给「直接删」的按钮，只给「停用」→ 记录出现在待办和人员页 → 「确认删除」。

删号确认框要求**输入这个人的姓名**（不是 `DELETE` —— 姓名能同时防「选错人」），并逐条列出会动哪几个号。文案照 `admin-console-rework.md` ④4.3 那一版，关键一句不能丢：**「会删掉账号本身。他在桶里的文件、数据集、实例都不动。」**

### 4.3 数据一概不动

`delete_user` 只删 AK / 组 / 策略 / MFA / 登录配置 / 用户本身（`provision.py:541-595`），不碰任何数据 —— 那些东西属于主账号。这句话要在三处出现：确认框、结果提示、`review.log` 的记录里。用户被问过这件事，答案很明确：「数据不要删除,我要确认了你才删除账号」。

### 4.4 九章

`offboard.MANUAL_PLATFORMS`（`offboard.py:46`）。面板动不了，`decide` 走的是「只记账、不碰云」的分支（第 399-411 行），文案是「我已在控制台处理」。新入口对九章**只提供「记账」**，按钮文字必须区别于云上那两个，别让人以为面板真去停了。

---

## ⑤ 人员画像页

### 5.1 定位

一个人一页，上面四样东西的落脚地。不是新做一套，是把 `GET /api/admin/person/<key>`（`server.py:1686`）加法式扩展。

**这一页不发起云调用**，照 `_todo_view` 那条规矩（`server.py:334`「零云调用：全部来自本地文件和快照，所以每次打开面板都能跑」）。
例外和 `admin-console-rework.md` ⑤5.2 一致：单条记录的「现在查一下云上」按钮、写操作的预演、写完之后回读那一条。列表级、页面级一律读快照。

### 5.2 一页上有什么

| 段 | 内容 | 数据源（都是本地文件/快照） | 旁边的动作 |
|----|------|---------------------------|-----------|
| 头 | 姓名 / 邮箱 / union_id / 在职状态（三态，带来源和时间） | `people.json` + `iam_sync.cached_reconcile` | — |
| 云账号 | 每个平台一行：登录名、云上状态、AK 概况、所在组 | `inventory` 快照 + `offboard.json` | 停用 / 启用 / 确认删除（§④） |
| 权限 | 直接授予的策略、经组继承的、高危几条 | `views.person_detail`（已有） | 收权（已有，跳现有 revokeBox） |
| 凭证 | 他名下的凭证单：范围、窗、状态、AK 后四位 | `tickets.json` | 重算下发 / 改有效期 / 改权限 / 重发 / 撤销（§②） |
| 离职 | 记录状态、谁在什么时候判的、停用时关掉了什么 | `offboard.json` | 恢复 / 确认删除 / 他没离职 |
| IAM 属性 | 公司 IAM 里这个人的属性、对账差异 | `iam_sync` | 生成增量（跳系统页） |
| 九章 | 有没有号、名单截至哪天 | `offline_accounts` | 记账（§4.4） |

### 5.3 三条展示规矩（沿用，别再发明）

1. **三态是底线**：已知真 / 已知假 / 不知道。「没采到」不许渲染成「没有」。
2. **标签 = 事实 + 谁说的 + 什么时候**。例：`已停用（面板 9-20）` / `已停用（控制台）` / `面板没停过它 · 云上未知（采于 9-23 09:12）`。
3. 凭证那一段的有效期旁边标注依据：`到期 10-31（云上策略窗，9-23 读回）`。有 `cred_regrant_pending` 时直接标红说「面板和云上可能不一致」。

---

## ⑥ 权限边界与审计痕迹

### 6.1 谁能点：`roles.load_admins`，不是 `ADMIN_FEISHU_OPEN_ID`

明确回答这个问题：**面板这边是 `roles.load_admins`**（`identity/admins.json`，`roles.py:72`），`ADMIN_FEISHU_OPEN_ID` 是 bot 仓库那套，面板一个字都没用。

三条入口，三道门，全部已存在，新动作一条都不能少：

| 入口 | 判定 | 代码 |
|------|------|------|
| 网页写接口 | `self._require(admin=True)` → `backend.role(session.user)` → `admins.role_of(union_id=/email=)` | `server.py:1512-1520` |
| 网页写接口（CSRF） | `self._same_origin_json()`：Content-Type + `X-Panel-Request: 1` + Sec-Fetch-Site + Origin | `server.py:1818-1834` |
| 飞书卡片按钮 | 验签 → `open_id` 换 `union_id` → 同一份 admins 名单 | `server.py:1249-1262` |

`roles.load_admins` 是 fail-closed 的（没配置 ⇒ 没有人是管理员）。新动作不要给任何「本人也能点」的例外——唯一的现存例外是作废自己的凭证（`ticket_view` 的 `revoke` 判据含 `own`），那是泄漏应急，且只减权限；本批四样里没有同类。

### 6.2 哪些要二次确认

| 动作 | 确认强度 | 理由 |
|------|---------|------|
| A1 重算下发 | 预演（显示新旧策略 diff）+ 普通确认 | 不扩不缩，但要让人看见改了哪几个动作 |
| A2 改有效期（缩短） | 普通确认 | 只减 |
| A2 改有效期（延长） | 预演 + **必填理由（≥5 字）** | 扩大了时间维度上的权限 |
| A3 改权限（收窄） | 普通确认 | 只减 |
| A3 改权限（补齐能力） | 预演 + **必填理由** | 扩权 |
| A4 重发凭证 | 确认框写明「旧取件链接立刻失效、对方服务要改配置」 | 会断服务 |
| A5 撤销 / 清理孤儿 | 逐条列出会删什么 + 「数据不动」那句 | 不可逆 |
| 停用 | 列出会动哪几个号 | 可逆，不要求输入确认词 |
| 启用 | 普通确认 | 可逆 |
| **删号** | 逐条列出 + **输入这个人的姓名** | 不可逆 |

必填理由这条抄现成的：`server.py:2393` 收权那里就是 `if apply and len(reason) < 5: 400`。

### 6.3 审计痕迹写在哪

**两份，各管一段，都不能省：**

1. **申请单 events**（`tickets.TicketStore.update`，`tickets.py:218` 强制追加 `{at, actor, event, note}`）：所有凭证类动作（A1–A5、孤儿清理）。这是「这张凭证经历过什么」的完整时间线，顺序、actor、note 都是现成的。
2. **`review.log`**（`review.py:477 log_event` / `530 log_offboard`）：所有**不经申请单**的云写操作 —— 单个号的停用/启用/删除。理由见 `review.log_event` 的注释：「和权限变更、离职回收同一份日志。分成几份的话，『这个人身上发生过什么』就得跨文件拼 —— 而那正是出事时最要紧的那个问题」。

**规则：任何扩大权限的动作，两份都写。** A2 延长和 A3 补齐能力除了写单子事件，还要 `review.log_event({"op": "cred_regrant", actor, scope, before, after, reason})` —— 单子 events 是按单号查的，review.log 是按人/按时间查的，扩权事后复盘走的是后者。

**「谁在什么时候为什么改的」= actor(union_id) + at + reason，三样缺一不可。** 接口层面强制：扩权类动作 reason 为空直接 400，不给默认值。

留痕写失败的处理照现成规矩：**不抛**（云上已经改了，这时候报错会让人以为没改成、再点一次），但**要往 stderr 出声**（`review.py:459-461` 的注释就是为这个写的）。

---

## ⑦ 云侧凭证够不够

### 7.1 逐动作清单（对照 `deploy/panel/cloud-policies/` 的现网副本，2026-09-23 导出）

**凭证运维 —— 发放身份 `panel-issuer`：**

| 动作 | 阿里 action | 有没有 | 火山 action | 有没有 |
|------|------------|--------|------------|--------|
| 读当前策略（预演 / 读回 not_before） | `ram:GetPolicy`, `ram:GetPolicyVersion` | ✅ issuer:42-64 | `iam:GetPolicy` | ✅ issuer:20-32 |
| A1/A2/A3 写新策略 | `ram:CreatePolicyVersion` | ✅ issuer:48 | `iam:UpdatePolicy` | ✅ issuer:25 |
| 版本上限 rotate | `ram:DeletePolicyVersion`, `ram:ListPolicyVersions` | ✅ issuer:49-50 | 不适用 | — |
| A4 重发 AK | `ram:CreateAccessKey` `ListAccessKeys` `DeleteAccessKey` | ✅ issuer:24-26 | `iam:CreateAccessKey` 等 | ✅ issuer:9-12 |
| A5 / 孤儿清理 | `DeleteAccessKey` `DetachPolicyFromUser` `DeletePolicy` `DeleteUser` | ✅ | 同 | ✅ |
| 身份自检 | `sts:GetCallerIdentity` | ✅ issuer:5-17 | `iam:ListUsers`（`VolcanoExecutor._check_account`，`provision.py:743`） | **❓ 这份策略里没有** |

**结论：凭证运维这一块，`deploy/panel/cloud-policies/*.json` 预期零改动。** 版本相关的四个动作早就在阿里 issuer 里配好了。

**唯一的问号**是火山 issuer 的 `iam:ListUsers`：`VolcanoExecutor._check_account` 拿它确认「这把 AK 属于哪个主账号」，而这份策略副本里没有它。线上火山凭证确实发得出来，说明要么发放身份还挂着别的策略、要么这份副本不全。**这条必须实测，不许按推断改策略**（ACT-1）。

用户实测过 issuer **没有 `ram:ListUsers`** —— 这是对的、也该保持：发放身份只该动自己发的那些号（Allow 的 Resource 全部锁在 `user/tempak-*` `user/staff-*` 和 `policy/temp-ak-auto-*` `policy/staff-oss-auto-*` 上）。本批不需要它。

**账号停用/启用/删除 —— 开通身份 `panel-executor`：**

| 动作 | 阿里 | 有没有 | 火山 | 有没有 |
|------|------|--------|------|--------|
| 停用（关登录 + 禁 AK） | `GetLoginProfile` `DeleteLoginProfile` `ListAccessKeys` `UpdateAccessKey` | ✅ executor:192-211 | `GetLoginProfile` `UpdateLoginProfile` `ListAccessKeys` `UpdateAccessKey` | ✅ executor:4-27, 46-59 |
| 启用 | `CreateLoginProfile` `UpdateAccessKey` | ✅ executor:35-49 | `UpdateLoginProfile` `UpdateAccessKey` | ✅ |
| 删号 | `ListAccessKeys` `DeleteAccessKey` `RemoveUserFromGroup` `DetachPolicyFromUser` `UnbindMFADevice` `DeleteLoginProfile` `DeleteUser` | ✅ executor:192-211 | 同 | ✅ |
| 受保护名单 | Deny 两段 | ✅ executor:124-180 | Deny 一段 | ✅ executor:60-84 |

**结论：账号停用/启用/删除也是零改动。** 离职流程已经在用这些权限，新入口只是换个触发方式。

### 7.2 两条容易被忘的约束

1. **阿里两份策略的每条 Allow 都带 `acs:SourceIp: 120.79.167.166`。** 任何新动作只能从面板机发起。本地或 CI 跑 CLI 会报 403，而错误里不会提 IP —— 排查会往别的方向跑。这条要写进 CLI 的帮助文本。
2. **真要改策略时，改四处**（`deploy/panel/cloud-policies/README.md:17-19`）：`grants.ISSUED_PREFIXES`、两份 issuer 的 Allow、两份 executor 的 Deny。2026-09-23 把内部凭证从 `tempak-` 改成 `staff-` 时后两处都漏了，审计才抓出来。本批预期不改，但 ACT-1 实测若发现缺，按这条规矩来：**先改云上、再重新导出覆盖仓库副本**（副本以云上为准）。

---

## ⑧ 前端：9 个 tab 收敛到 5 个

### 8.1 目标导航

用户点名的五个：**待办 / 申请与开通 / 人员与账号 / 云资产与权限 / 系统状态**。

| 新 tab | 路由 | 由现在哪些页合并 |
|--------|------|-----------------|
| 待办（默认落点） | `#admin/todo` | 原样 |
| 申请与开通 | `#admin/requests` | 原样，**不重写**；凭证运维的按钮加在这里的单详情里 |
| 人员与账号 | `#admin/people`（`#person=` 详情 = 人员画像页） | 人员与名册 + 人工登记 + IAM 属性表 |
| 云资产与权限 | `#admin/assets` | 云资产 + 权限规则 + 体检 |
| 系统状态 | `#admin/health` | 原样 |

**和 `admin-console-rework.md` 的分法有出入**（那份把 IAM 属性表 + 人工登记 + 权限规则一起塞进「系统与数据源」）。按用户这次点名的五个走，差异是：权限规则归「云资产与权限」、IAM 属性表和人工登记归「人员与账号」。理由说得通 —— IAM 属性表和人工登记回答的都是「这个人在哪些名单里、叫什么」，和人绑定；权限规则回答的是「哪些策略能申请、算不算高危」，和资产/权限绑定。**这一条列进开放问题，等用户点头再改导航。**

### 8.2 老链接一条都不能断

现在 `web/app.js:172-179` 的路由表和 `todo.py` 里写死的 `href` 是耦合的。`todo.py` 现有的深链：

| 出处 | href | 收敛后去哪 |
|------|------|-----------|
| `todo.py:226,244` `offboard_pending` / `offboard_manual` | `#admin/iam` | `#admin/people?todo=offboard`（**A**） |
| `todo.py:270` `iam_inactive` | `#admin/iam` | `#admin/people?todo=iam_inactive`（**A**） |
| `todo.py:302` `request_failed` | `#admin/requests?state=failed` | 不变 |
| `todo.py:318` `request_no_iam` | `#admin/iam` | `#admin/people?todo=no_iam`（**A**） |
| `todo.py:353,373,389` 三条凭证类 | `#admin/requests` | `#admin/requests?state=cred_orphan` 等（§③1） |
| `todo.py:409,425` 名册两条 | `#admin` | `#admin/people?filter=…` |
| `todo.py:447,462` 属性表两条 | `#admin/iam` | `#admin/people`（IAM 段锚点） |
| `todo.py:491` `keys` | `#admin/hygiene` | `#admin/assets`（体检段锚点） |
| `todo.py:519,548` 工作空间/地域 | `#admin/assets` | 不变 |
| `todo.py:567` `config_blocking` | `#admin/health` | 不变 |

**（A）标的这几条说明一件事：`#admin/iam` 今天是四类不同待办的共同落点**，而它上面挤着三件不相干的事（增量、对账、离职待确认删除）。收敛的第一步就该把它拆开。

保底规矩（照 `admin-console-rework.md` ②的过渡策略）：

1. `#admin/iam` `#admin/offline` `#admin/policies` `#admin/hygiene` `#admin` **全部保留为别名**，在 `app.js` 的 `route()` 里映射到新页 + 锚点。书签和聊天记录里的链接不能 404。
2. 新页用新路由先上线，和旧页并存；**导航最后一批才删多余 tab**。
3. `todo.py` 的 href 和 app.js 的路由表**同一批改**，否则待办页会指向还不存在的锚点。

---

## ⑨ 任务列表

优先级：P0 = 本批必做；P1 = 紧接着；P2 = 收尾。
大小：S ≈ 半天内，M ≈ 1–2 天，L ≈ 3 天以上。
owner 是建议值，**dev 确认后我再把任务板上的 owner 落死**。

### 第一批：凭证运维（当天就能修掉 lakeFS 那个案例）

| ID | 任务 | 验收标准 | owner | 依赖 | 大小 | 优先级 |
|----|------|---------|-------|------|------|--------|
| **ACT-1** | 取证：① 火山 `UpdatePolicy` 的准确参数名与语义（是否原地替换、`NewPolicyDocument` 还是 `PolicyDocument`、要不要 `NewPolicyName`）；② 阿里 `CreatePolicyVersion` 的 `RotateStrategy` 取值与 5 版本上限行为；③ **线上实测**火山发放身份到底有没有 `iam:ListUsers`（`_check_account` 依赖它），以及两朵云 issuer 身份当前实际挂着哪几条策略 | 结论写 `docs/collab/research/`，每条带出处（官方文档链接或真机返回原文）；③ 必须是只读实测，不改任何策略；给出「要不要改 `cloud-policies/*.json`」的明确结论 | researcher | — | S | P0 |
| **ACT-2** | 纯逻辑模块 `src/delivery/regrant.py`：给定 ticket + 模板 + 目标（重算 / 新到期 / 新 caps），算出「新策略文档 + 会变哪些字段 + 拒绝原因」。不调云 | ① `cred_user` 为空（STS 短期）→ 拒，理由说清；② `新caps ⊄ tpl.caps` → 拒；③ 单子有 `cred_caps` 时优先于模板 caps（单测锁「A3 后 A1 不回弹」）；④ 拿不到 `not_before` → 拒，不许用 now 兜底；⑤ 新到期 ≤ now → 拒；⑥ 产出的 diff 能直接渲染（旧 statement / 新 statement 逐条对比） | dev | — | M | P0 |
| **ACT-3** | `provision.py` 两朵云各加一个 `rewrite_policy(policy_name, doc)`，放在「长期凭证」那一节 | ① 阿里走 `CreatePolicyVersion(SetAsDefault, RotateStrategy=DeleteOldestNonDefaultVersionWhenLimitExceeded)`；② 火山走 `UpdatePolicy`；③ 两者都先 `_check_account()`；④ 阿里另加 `read_policy_window(policy_name)`（GetPolicy 读回默认版本的 `DateGreaterThan`/`DateLessThan`），火山同名方法；⑤ 单测用假 transport 断言参数逐字正确 | dev | ACT-1 | M | P0 |
| **ACT-4** | `flows.regrant_credential(ticket_id, *, mode, expire=None, caps=None, actor, reason)`：三步落地（记意图 → 写云 → 回写单子），旧策略文档存进 `cred_policy_prev` | ① 三步的事件名和字段按 §2.4；② 写云失败 → 单子只留 `cred_regrant_pending`，状态不变，抛错带脱敏原文；③ 写云成功、回写失败 → `pending` 留着（单测断言）；④ 同一 ticket 并发两次调用不会产生两个默认版本（文件锁已有，验证够不够）；⑤ 扩权类（延长 / 补 caps）reason 为空直接抛 | dev | ACT-2, ACT-3 | M | P0 |
| **ACT-5** | 接口：`POST /api/admin/requests/<id>/regrant`，body `{mode: repair|expire|caps, expire?, caps?, reason?, apply?}`。默认**预演**，`apply` 才动云 | ① `_require(admin=True)` + `_same_origin_json()` 两道门都在；② 预演返回「会变什么 / 不会变什么及原因 / 改完长什么样」三段；③ `ticket_view.actions` 加 `regrant`（判据：admin + kind=credential + status∈(DONE) + `cred_user` 非空）；④ 非管理员 403、非凭证单 409，均有用例；⑤ 错误一律过 `provision.describe_error` 脱敏 | dev | ACT-4 | M | P0 |
| **ACT-6** | 前端：凭证单上「重算下发 / 改有效期 / 改权限」抽屉，预演 diff + 确认 | ① 三个动作共用一个抽屉，先预演后执行；② 预演没出来之前执行按钮 disabled；③ diff 明确标出「AK 不变，对方不用改配置」；④ 扩权类必填理由，前端校验 + 服务端校验都有；⑤ 沿用 `core.js` 的 `h()/api()/apiPost()`，不引框架；⑥ 渲染函数单独 export（`requests.js` 那种「导出只为可测」的做法） | dev | ACT-5 | M | P0 |
| **ACT-7** | 补测：regrant 全链路 | ① ACT-2 的六条拒绝各一例；② 三步中断的两种崩法各一例；③ 版本上限（连改 6 次）断言用的是 rotate 参数不是自己删；④ 火山路径走 `UpdatePolicy`、阿里走 `CreatePolicyVersion`，参数逐字锁；⑤ 全量 `make test` 绿；⑥ 断言预演路径**零云写** | tester | ACT-5 | M | P0 |
| **ACT-8** | 审计：扩权面、审计痕迹、双写漂移 | ① 有没有任何路径能把 caps 改到模板之外、把 `not_before` 推到未来；② 扩权类动作是不是两份日志都落了 actor/at/reason；③ 写云成功但回写失败时，页面是否会显示一个骗人的到期时间；④ 脱敏是否覆盖所有 502 分支；⑤ 结论无阻塞才准 commit | auditor | ACT-6, ACT-7 | S | P0 |
| **ACT-9** | A4 重发凭证：`…/reissue` | ① 先发新 AK 再删旧 AK（单测锁顺序）；② 重新 `sealed.seal`，旧取件链接立刻失效，确认框写明这句；③ 删旧 AK 失败 → 进 `orphan_ak_ids`，不静默；④ 单子事件完整 | dev | ACT-5 | M | P1 |
| **ACT-10** | A5 撤销：失败原因上页面 + 重试 | ① `revoke_long_term` 的 `left` 写进 `cred_revoke_left` 并在详情页显示；② 「云上本来就没有这个号」和「删不干净」两种结果文案区分；③ 失败后有「再试一次」按钮 | dev | — | S | P1 |

### 第二批：孤儿子账号清理

| ID | 任务 | 验收标准 | owner | 依赖 | 大小 | 优先级 |
|----|------|---------|-------|------|------|--------|
| **ACT-11** | 待办 `cred_orphan` 深链带筛选；申请页支持按「云上还有残留」筛 | ① 从待办点过去**能直接看到那几张单**（今天点过去是默认的 open 视图，一张都看不到）；② 筛选在服务端算，和 `todo.collect_expiring` 的判据同一份（`cred_user` + `status∈(closed,failed)`），不许前端再写一遍 | dev | ACT-10 | S | P0 |
| **ACT-12** | 孤儿单的按钮文案与结果展示 | ① 已关/已失败的单，按钮文案改成「清掉云上残留的子账号」，说明含「删的是账号本身，桶里的文件一个不动」；② 删不掉的原因（`left`）直接显示在待办条目和单详情上；③ 定时任务已经试删过多少次、最近一次什么时候，页面上看得到 | dev | ACT-11 | S | P0 |
| **ACT-13** | 补测 + 审计 | tester：判据一致性、文案分支、`left` 透传；auditor：有没有把「云上没有这个号」误报成成功 | tester + auditor | ACT-12 | S | P0 |

### 第三批：单个云账号停用 / 启用 / 删除

| ID | 任务 | 验收标准 | owner | 依赖 | 大小 | 优先级 |
|----|------|---------|-------|------|------|--------|
| **ACT-14** | 接口 `POST /api/admin/accounts/action`，body `{platform, account, user, op: disable|enable|delete, reason, confirm_name?}` | ① `disable` 走 `offboard.ensure_record` + `disable_user`，`enable`/`delete` 走 `offboard.decide`，**不新写删除逻辑**；② `protected()` 在服务端再拦一次（前端绕过也拦得住，用例覆盖）；③ `delete` 只接受 `state∈(disabled,suspect)` 的记录，且 `confirm_name` 必须等于记录里的 `person`；④ 九章只记账，响应里明说面板没动云；⑤ `review.log_offboard` 每次一行，含 actor/reason；⑥ `unverified` 记录拒绝删除（现有判据） | dev | — | M | P0 |
| **ACT-15** | 前端：人员画像页上的停用 / 启用 / 确认删除按钮 + 列条目的确认层（替掉 `window.confirm`） | ① 确认框逐条列出平台/账号/登录名；② 删号要求输入姓名；③ 三处都有「数据不动」那句；④ 执行后就地回读那一条并更新，不再说「等下次采集」 | dev | ACT-14, ACT-18 | M | P0 |
| **ACT-16** | 补测：对抗用例 | ① 构造一个不在 `offboard.json` 里的用户名 → 拒；② `protected` 名单里的号（`wuji-ci` / `panel-executor` / `staff-*`）→ 拒，两朵云各一例；③ 跨人批量删除在服务端被拒（本批不做批量，用例先锁死）；④ 非管理员 403；⑤ 九章分支断言**零云调用** | tester | ACT-14 | M | P0 |
| **ACT-17** | 审计 | ① 新入口有没有任何一条绕过 `protected()` 的路径；② 「先停用后删除」是不是真的强制；③ `review.log` 能不能回答「谁在什么时候为什么停了这个号」；④ 删号文案有没有可能被理解成删数据 | auditor | ACT-15, ACT-16 | S | P0 |

### 第四批：人员画像页 + 导航收敛

| ID | 任务 | 验收标准 | owner | 依赖 | 大小 | 优先级 |
|----|------|---------|-------|------|------|--------|
| **ACT-18** | `GET /api/admin/person/<key>` 加法式扩展：并进凭证单、离职记录、IAM 属性、九章 | ① 字段只增不删，老前端不改也能跑（单测锁旧字段）；② **零云调用**（用例断言 transport 未被调用）；③ 任一数据源读失败只让那一段缺席并带 `errors[]`，不 500；④ 一次请求返回全量，无 N+1 | dev | — | L | P0 |
| **ACT-19** | 前端人员画像页：七段 + 每段旁边的动作入口 | ① §5.2 七段齐；② 三条展示规矩（三态 / 标签带来源和时间 / 有效期标依据）逐条过；③ 渲染函数单独 export 可测；④ 旧 `#person=` 链接仍可达 | dev | ACT-18 | L | P0 |
| **ACT-20** | 导航收敛到 5 个 tab + 旧路由别名 + `todo.py` 的 href 同批更新 | ① §8.2 表里每条旧 hash 都仍可达（用例逐条点）；② 待办页每一条点过去都落到能处理它的地方（逐条人工走查，列成清单）；③ 默认落点仍是待办 | dev | ACT-19, ACT-15 | M | P1 |
| **ACT-21** | 文档同步 | `docs/cloud-access-platform.md` 补凭证运维那一节；`deploy/panel/cloud-policies/README.md` 补「改策略要重新导出」的现状；`docs/runbook.md` 补三个新动作的处置步骤 | docwriter | ACT-20 | S | P1 |

### 横向

| ID | 任务 | 验收标准 | owner | 依赖 | 大小 | 优先级 |
|----|------|---------|-------|------|------|--------|
| **ACT-22** | 新待办 kind `cred_window_drift`（面板和云上可能不一致） | ① `cred_regrant_pending` 存在即出一条，分组 URGENT；② 主操作是「按云上读回来对齐」，**定时任务只报不改**（单测锁：跑一轮对账不修改任何单子）；③ 对齐动作要管理员点，并记事件 | dev | ACT-4 | S | P1 |
| **ACT-23** | CLI 对等：`delivery requests regrant / account disable|enable|delete` | ① 和网页共用同一份逻辑模块，不写第二份判据；② dry-run 默认；③ 帮助文本写明「阿里策略带 SourceIp 条件，只能在面板机上跑」 | dev | ACT-5, ACT-14 | M | P2 |

---

## ⑩ 上线顺序

**能单独上、单独回滚的先上。** 每一批发布后都要在线上真做一次（不是「服务起来了吗」，是「这个功能真的能用吗」——`docs/collab/notes.md:17-19` 那条教训）。

| 批次 | 内容 | 能不能单独上 | 为什么 |
|------|------|------------|--------|
| **1a** | ACT-1 → ACT-2 → ACT-3 → ACT-4 → **只做 A1 重算下发** 的接口和按钮（ACT-5/6 的子集） | **能，而且最该先上** | 零参数、零扩权面，今天的 lakeFS 案例当场就修好了。回滚 = 去掉一个按钮 |
| **1b** | A2 改有效期 + A3 改权限（ACT-5/6 剩下的）+ ACT-7/8 | 能 | 共用 1a 的管道，只多表单和预演 |
| **1c** | ACT-9 重发 + ACT-10 撤销失败展示 + ACT-22 漂移待办 | 能 | 各自独立 |
| **2** | ACT-11 → ACT-12 → ACT-13 | 能 | 只动待办深链和文案，不动云 |
| **3** | ACT-14 → ACT-16 → ACT-17，**前端等 ACT-18/19** | 接口能先上（CLI 先用），按钮要等画像页 | 接口和 UI 解耦；ACT-15 必须和 ACT-19 一起上 |
| **4** | ACT-18 → ACT-19 → ACT-15 → ACT-20 | ACT-20 必须最后 | 导航收敛依赖新页面存在；`todo.py` 的 href 和 `app.js` 的路由表**必须同一批**，分开上会指向不存在的锚点 |
| **5** | ACT-21 文档 + ACT-23 CLI | 能 | 收尾 |

**必须一起上的三组：**
1. ACT-2/3/4 —— 逻辑、执行器、编排，缺一个都跑不起来。
2. ACT-15 + ACT-19 —— 按钮和它所在的页面。
3. ACT-20 里的「`todo.py` href」和「`app.js` 路由表」—— 见上。

**提交闸门照旧**：每一批 auditor 复审无阻塞才 commit（`docs/collab/notes.md` 的硬规）。

**部署提醒（用户的规矩，写进每批的收尾步骤）**：服务器上写 `identity/` 的脚本一律 `sudo -u delivery`。root 写会把 `tickets.json` 改成 root 属主，面板从此读不了，且没有任何告警 —— 线上因为这个挂过 100 分钟（`docs/collab/notes.md:83`）。本批的 ACT-4/ACT-14 都会写 `identity/` 下的文件（`tickets.json` / `offboard.json`），面板进程自己写没问题，**但任何配套的手工修复脚本必须 `sudo -u delivery`**。

---

## ⑪ 明确不做

1. **不做跨人批量。** 本批所有动作都是单条或「一个人名下成组」。跨人批量删号解决不了任何真实痛点，却把一次误操作的代价从一个人放大到十个人。批量的事留给 `admin-console-rework.md` 的第二批。
2. **不做「改生效时间」。** 只改到期时间。理由见 §2.3 第 3 条 —— 提供这一栏就是在邀请那个「凭证在延期通过那一刻反而失效」的坑。
3. **不做 caps 扩到模板之外。** 那等于面板单方面绕过审批。要更大范围 → 走新申请。
4. **不发明新的权限维度。** 只用 `grants.CAPS`（list / download / write）和模板里的 `caps`。source_ips、多桶、多前缀这些 `build_policy` 支持但模板没暴露的维度，本批一律不碰。
5. **不动飞书审批链路。** 凭证的发放入口仍然只有审批那一条；本批加的全是「发出去之后的运维」。
6. **不做自动改权限、自动删号。** 定时任务只做两件事：试删已经判过的、报不一致。任何扩权和不可逆动作都要人点。`offboard.MAX_AUTO_PEOPLE = 3` 不动。
7. **不做策略回滚按钮。** 存 `cred_policy_prev` 是为了人能照着改回去，不做一键回滚 —— 火山原地替换、阿里 rotate 会删版本，「一键回滚」在两朵云上都可能落空，而落空的回滚比没有回滚更危险。
8. **不改快照存储形态、不引数据库、不引前端框架。** 沿用 `core.js` 的 `h()/api()/apiPost()` 和「接口字段只经 textContent / 属性赋值进 DOM」那条安全约定。
9. **不做页面级实时查云、不做自动轮询。** 只有三种情况打云：单条「现在查一下」、写操作预演、写完回读那一条。
10. **不为面板新写第二份判据。** 筛选、判据、文案一律和 CLI、待办共用同一个模块。「网页说 A、命令行说 B」比功能缺失更糟。
11. **不重写申请与开通页。** 凭证运维的按钮加在它的单详情抽屉里，页面结构不动。
12. **不做存量纳管之外的事**：不新建账号、不挪存量数据。

---

## ⑫ 开放问题（请 dev 代问用户，别替他拍板）

1. **导航的五个 tab 里，IAM 属性表和人工登记归「人员与账号」、权限规则归「云资产与权限」—— 确认吗？** 这和 `admin-console-rework.md` 的分法不同（那份把三样都塞进「系统与数据源」）。按用户这次点名的五个名字，本文这么排；要是他心里是另一种，早说比做完再改便宜。
2. **A3 改权限允许「补齐能力」吗，还是只允许收窄？** 本文建议：允许补齐，但必须在模板 caps 范围内 + 必填理由 + 两份日志。如果他希望「任何补齐都重走审批」，那 A3 就退化成「只能收窄」，工作量少一半。
3. **A2 延长有效期有没有上限？** 今天发放侧有 `MAX_CREDENTIAL_HOURS`（`catalog` 里的加载期硬校验）。延长要不要受同一个上限约束？本文建议受 —— 否则「发 30 天上限」会被「先发 30 天再延到一年」绕过。
4. **A4 重发凭证之后，新的取件链接怎么给对方？** 今天发放是走飞书审批评论（`flows` 那条链）。重发时那张审批实例还在不在、能不能再贴一条评论？还是改成面板上直接生成一个取件链接、管理员自己转给对方？这条影响 ACT-9 的做法。
5. **单个号「停用」需不需要理由？** 本文建议必填（和收权那条对齐）。但停用是应急动作，强制填理由会让人在着急的时候多一步。也可以改成「停用不必填、删除必填」。
6. **人员画像页要不要显示这个人的资产（机器、桶、数据集）？** 数据在 `asset_owners` 和 `assets` 快照里，并进去不难，但这一页会变长。本文暂时没放。
7. **`cred_window_drift` 这条待办要不要进「要紧的」？** 本文放 URGENT（页面在骗人这件事很要紧），但它的实际频率应该极低，放 URGENT 会让红点偶尔为一条低频项亮起。

---

## ⑬ 给 dev 的落地提示（只读源码得到的、会踩的点）

- **`grants.build_policy` 是纯函数、没有副作用**，A1–A3 全部围着它转。它的 `not_before >= expire` 会抛 `GrantError`（`grants.py:207`），regrant 的参数校验要在调它之前做，否则错误文案是给开发看的、不是给管理员看的。
- **`tickets.TicketStore.update` 禁止修改 `id/status/events/applicant/created_at`**（`tickets.py:214`），其余字段随便加。`cred_caps` / `cred_not_before` / `cred_policy_prev` / `cred_regrant_pending` / `cred_revoke_left` 都是新字段，加就行，老单子缺这些字段要一律解析成「未知」，不是空。
- **`ticket_view` 的 `actions` 是前端唯一的按钮来源**（`requests_api.py:171`）。新按钮不加进去，接口做完也点不到。
- **`_ID` 正则**（`requests_api.py` 顶部）限制了 ticket_id 的形状，新路由 `rest[1]` 的动作名要加进 `_route` 的白名单分支里，别写成兜底。
- **`flows._issuer_for`（`flows.py:425`）没接发放身份时会回落开通身份。** 而开通身份在云上被明确禁止建号/发 AK/造策略（executor 策略的 Deny）—— regrant 走 issuer，回落到 executor 会报一个看不懂的 AccessDenied。新代码里要么显式检查 `issuer_configured`，要么把这条回落路径的错误文案写清楚。
- **两处 `_check_account` 每个执行器只跑一次**（`self._checked`），但执行器是每次请求现建的（`executor_from_env`）。所以每个 regrant 请求至少多一次 `GetCallerIdentity`（阿里）/ `ListUsers`（火山）。能接受，但别在循环里建执行器。
- **`provision.describe_error`（`provision.py:1253`）只取第一行并脱敏。** 所有回前端的云错误都要过它 —— `_revoke_access` 那里是正面例子（`server.py:2400-2403`）。
- **前端 `app.js` 零 export 且 import 即 `boot()`**，无法直接单测（notes 已记）。人员画像页和 regrant 抽屉**单独成模块并 export 渲染函数**，照 `requests.js` / `assets.js` 末尾那种做法，否则这一批又是零覆盖。
- **`web/assets.js` 的两条已知缺陷别复制**：`chips()` 是死代码；超过 500 行截断后「看不见的勾选仍会被提交」。
- **`_same_origin_json()` 和 `_require(admin=True)` 在新接口上一个都不能少**，两道门是分开的（一道认身份、一道防 CSRF）。
