# 九章（AlayaNeW）开账号申请接进面板

状态：待评审（planner 出，dev 收口）
涉及仓库：`infra`（面板，本仓）+ **飞书审批定义**（控制台，人工一次性，且必须先于代码）

## 1. 要解决什么

员工在面板申请页上找不到九章的入口 —— 查实确实没有，15 条模板全是阿里云和火山的。
用户要的形状已经定死：**「九章和阿里云一样是申请账号」**，即做成 `kind: account`，
和 `aliyun-new-user` / `volcano-new-user` 并排在「开账号」那一栏里，不另起一类。

但九章和那两朵云有一条本质差别，整份方案都是围着它转的：

> **面板没有任何九章的开通接口。号只能由管理员在九章控制台手工建。**

所以这件事真正的难点不是「加一条模板」，而是**不让「批准」被误读成「开好了」**。
这个项目里出过「报成功但其实没做」的事故（`offboard.MANUAL_PLATFORMS` 的注释里记着那次：
一个离职的人在九章上的号没有任何地方提醒去停）。这次要在一开始就把两类账号在
台账、卡片、文案、待办上分清楚，并且给出**管理员被通知到、干完能销账、没销账看得见**的闭环。

## 2. 现状（读代码查实，不是推断）

九章在面板里现有的东西比想象中多，**大部分可以直接复用**：

| 位置 | 现状 | 对本方案的意义 |
|---|---|---|
| `platforms.py:195` | `NAMES.update({"jiuzhang": "九章"})`，**只有显示名，刻意不进 `ALL`/`IDS`** | 保持不进。`ALL`/`IDS` 的语义是「面板能调接口的云」，一旦放进去，catalog / health / iam_sync / inventory 会一起把它当真云 |
| `platforms.get("jiuzhang")` | **抛 `PlatformError`** | 凡是对模板平台调 `platforms.get()` 的地方都要查一遍（第 6 节列全） |
| `offboard.py:47` | `MANUAL_PLATFORMS = ("jiuzhang",)`，`manual()` / `targets_of()` / `decide()` 全认它 | 离职那一半已经做完了，不用重做 |
| `offline_accounts.py` + `identity/offline-accounts.json` | 人工登记 `jiuzhang/wuji` 18 个号，登录名 `wuji-<拼音>`，`login_prefix: "wuji-"` | **名册、「我的账号」、离职检查全靠它**。回填必须写回这里，否则新开的号在离职检查里不存在 |
| `notify.manual_platform()` | 已经委托给 `offboard.manual()` | 好榜样，新代码照这个写，别再抄一份 |
| `flows.options()` 的 `KIND_ACCOUNT` 分支 | 名册里有 → `owned`；有一张 DONE 的同平台同账号单 → `owned`；有 OPEN 单 → `pending` | **「已拥有」判定基本零改动**，见 4.4 |
| `_validate()` 的 `if mine: raise "你在这个云账号下已经有子账号了"` | 走名册 | 天然生效，不用加 |
| `catalog.awaits_human(tpl)` | 「**按模板判，不按类型判**」（docstring 原话） | 九章该走的就是这条路，形状上完全吻合 |
| `flows.fulfil()` + `POST /api/admin/requests/<id>/fulfil` | **现成的回填入口**，但硬编码 `kind != KIND_RESOURCE → 409` | 复用它，别新造端点（回答「有没有现成回填入口」：有） |
| `notify.notify_admins(notifier, union_ids, card)` + `roles.load_admins("identity/admins.json").union_ids` | 现成的**私聊管理员**通道，`_cmd_identity_iam_remind` / `_move_announce` 都在用 | 通知复用它，不新造 |
| `iam_sync.claim_remind(paths, sig, hours=)` | 按签名的提醒去重，**读不到状态时照常提醒** | 正是「宁可重复也别漏报」，重复提醒直接复用 |

### 2.1 三个必须先解掉的硬卡点（不解就上不了线）

**① 飞书审批的「云账号」是单选控件，送进去一个不在选项里的值 = 整张单提交失败。**
`flows._approval_fields()` 第 349 行把 `f"{platform}/{account}"` 当作单选控件的选项 key 送出去，
而 `KIND_SERVICE` 那个分支的注释已经把后果写死了：「送一个不在选项里的值，飞书拒的是整张表单
（不是这一个字段），单子直接落『提交失败』」。所以**飞书审批定义里必须先加一个
`jiuzhang/wuji` 选项**，顺序是审批定义先于代码上线 —— 和 MLflow 那条 SA-9、
和 `MAX_CREDENTIAL_HOURS`「模板必须先于代码」是同一类教训。

**② `catalog._ACCOUNT` 要求 account 是 6–20 位数字，九章的是 `wuji`。** 模板加载期就会被拒。

**③ `health.collect()` 很可能今天就已经是炸的，加模板后必炸。**
`health.py:206` 把**每条模板**的 `(platform, account)` 塞进 `accounts`，`health.py:273` 把
**快照里每个用户**的 `(platform, account)` 也塞进去（九章的 18 个号经 `offline_accounts.snapshot_accounts`
进了快照），然后第 339–342 行对每一个调 `platforms.cred_env_names(platform, …)` → `platforms.get()`
→ `PlatformError`。**这段不在 `_safe()` 里**，异常会穿出整个 `collect()` → `/api/admin/health` 整页挂掉。
这不是「报一个永远修不好的『九章执行身份没配』」那么轻 —— 是体检页打不开。
**请 dev/tester 先复现一次再动手**（我只读代码，没跑）。

## 3. 形状（定板）

```
员工在申请页选「九章 AlayaNeW 开账号」，填登录名 wuji-<拼音>
   → 飞书审批（走全局那条定义，模板不填 approval 字段）
   → 通过 → flows.execute() → _run() 里 jiuzhang 分支：**一个云接口都不调**，
      只返回一句「面板没有开号，等管理员在九章控制台建 wuji-xxx 后回填」
   → 单子落 FULFILLING（待开通），**绝不落 DONE**
   → 同一刻 _emit("fulfilling") → 私聊管理员：谁、开什么名、单号、回填深链
   → 管理员去九章控制台建号
   → 回到面板点「登记开通结果」，填实际登录名 + 邮箱
   → 并进 identity/offline-accounts.json（名册/离职检查立刻认得）
   → 单子 DONE，申请人收到「账号已开好」
```

**一条红线，写在任何实现前面**：`_run()` 里九章那条分支**必须插在
`ex = self._executor(tpl.platform, tpl.account)`（flows.py:2587）之前**。
九章没有执行身份，`executor_from_env` 对它直接 `ProvisionError("不支持的平台")`。
分支放晚一行，整条链在构造执行器那一刻就炸成 FAILED，而 FAILED 的文案会让管理员
以为「开通失败、重试就好」，跑去点重试，再炸一次。
现成的先例：`KIND_CREDENTIAL` 那条「只发取件地址，不碰云 —— 所以这里刻意不构造执行器」。

## 4. 「批了 ≠ 开好了」怎么不混（这一节是本方案的重点）

面板现有的词汇表里其实已经有这个区分，不需要发明新概念：
`FULFILLING`（待开通）就是「审批过了，但东西还不存在，等人去做」。
资源开通那一类用的就是它，`_run()` 里那句注释说得很准：
**「直接置成『已完成』是在台账里说谎 —— 没有任何东西因为这次点击而存在」**。

所以做法是把九章接到这条既有的路上，并在**六个出口**上各补一句人话：

### 4.1 单子状态：`FULFILLING`，且判据放在 `awaits_human()` 里

```python
def awaits_human(tpl) -> bool:
    kind = ...
    if kind == KIND_ACCOUNT:
        return platforms.is_manual(tpl.platform)   # 新增这两行
    if kind not in AWAIT_FULFIL:
        return False
    ...
```

**不要把 `KIND_ACCOUNT` 塞进 `AWAIT_FULFIL`**：那个元组的语义是「这一整类都要人」，
而阿里/火山的开账号是全自动的。塞进去之后，任何直接读 `AWAIT_FULFIL`（今天只有
`flows.options():604`）的地方都会跟着变，而那不是我们要的。
**风险**：将来有人再写一处直接读 `AWAIT_FULFIL` 而不调 `awaits_human()`，两边就会分叉。
所以 JZ-3 的验收里要求 grep 一遍直接读者并在 `AWAIT_FULFIL` 上加一行注释。

### 4.2 台账里那句 `result`：**不许出现「已开通」「已新建」**

`_run()` 返回的字符串会进 `result` 字段、进事件表、进卡片。九章那句写成：

> 审批通过。**面板没有建号** —— 九章没有接口，要管理员在九章控制台建 `wuji-xxx`，
> 建好后回到这张单点「登记开通结果」。

验收标准写成显式断言：`result` 里不含「已新建」「已开通」，且含「面板没有建号」。
靠肉眼看文案是会退化的，这句必须锁在用例里。

### 4.3 给申请人的卡片：两处都要改

- `notify.message("fulfilling")` 今天说「这类**资源**由管理员按流程开通」——
  对开账号要换成「九章的账号由管理员在控制台手工开，开好后你会收到通知；
  **在此之前你还没有账号**」。最后那半句是关键：不写的话，人会去试登录，登不进去又来问。
- `notify._account_how(tpl)`（`done` 卡片上那句「怎么登」）对 `console_login=false`
  今天返回「这个账号不开控制台登录，**只能用访问凭证**」—— 对九章是**假的**，
  九章根本不发访问凭证。要加 manual 平台分支，写九章控制台的登录方式。

### 4.4 「已拥有」判定：几乎零改动（好消息）

`options()` 的 `KIND_ACCOUNT` 分支已经是三条路，对九章全都天然成立：

1. 名册里有 `jiuzhang/wuji/wuji-xxx` → `owned`。名册来自
   `offline-accounts.json → snapshot_accounts → inventory → ssomap（按邮箱）→ person.accounts`，
   所以**只要回填写回了登记表，这条就自动亮**。
2. 有一张 DONE 的同平台同账号单 → `owned`（名册还没刷新时的兜底）。
3. 单子在 FULFILLING 期间属于 `t.OPEN` → `pending`「已提交，还没处理完」，不会重复申请。

**唯一的硬前提**：模板的 `account` 字面值必须和 `offline-accounts.json` 里的 `account`
（`wuji`）逐字相等 —— 名册键是 `platform/account/user`，差一个字就全对不上，
而表现是「开完号还显示可申请」，没有任何报错。所以 JZ-3 要求 catalog 校验
`account == platforms.manual_account(platform)`，把这个字面值收进注册表。

### 4.5 管理员一侧：`options()` 不会误判成「执行身份没配」（但要上锁）

查实：`options()` 里 `if tpl.kind == KIND_ACCOUNT:` 是**第一条分支**，
`elif self._executor_ready(...)` 那条 elif 根本走不到，所以不会出现
「九章执行身份没配置，请联系管理员」这种永远修不好的置灰。
**但这是巧合性的正确** —— 谁重排一下 if 顺序，`provision.executor_configured("jiuzhang", …)`
就会抛 `PlatformError`，而那不是置灰，是**整个申请页 500**。
所以：① 在 `options()` 里显式短路 manual 平台，把巧合写成明规则；② 上一条变异测试用例。

### 4.6 待办与体检：这类单必须能被一眼数出来

- `todo.collect_tickets()` 今天只收 `failed` / `submit_failed`。加一类
  `request_manual_account`（分组 **URGENT**，理由和 `request_failed` 一样：
  「申请人以为在走流程，其实卡住了」），标题「N 张单等你去九章控制台开号」，
  `href` 直达 `#admin/requests?state=fulfilling`。
- `todo.py:206` 今天硬编码 `r.get("platform") in ("jiuzhang",)` —— 这是第三份九章副本
  （另两份：`platforms.NAMES`、`offboard.MANUAL_PLATFORMS`；前端 `iam.js:470` 的
  `BY_HAND = new Set(["jiuzhang"])` 是第四份）。收敛见 JZ-1 / 二期。

## 5. 审批通过就通知管理员（用户硬需求）

### 5.1 触发点：**搭现成的 `fulfilling` 事件，不新造触发器**

`flows.execute()` 在把单子写成 `FULFILLING` 之后立刻 `self._emit("fulfilling", done)`。
这一行**两条路径都会走到**：飞书审批回调那条同步路径，和 `resume_approved()` 那条
无人值守补救路径。所以把管理员通知挂在这个事件上，就等于「审批通过那一刻就推」，
既不用等定时任务扫，也不会因为走了另一条路而漏推。**不要新增触发点。**

### 5.2 推给谁：复用 `notify_admins` + `identity/admins.json`

现成通道，三处已经在用（`_cmd_identity_iam_remind`、`_move_announce`、`mover`）：

```python
admins   = roles.load_admins(path).union_ids          # identity/admins.json
notifier = notify.FeishuNotifier(_tenant_token_cache(app_id, secret), base_url)
notify.notify_admins(notifier, admins, card)          # 逐个私聊，返回失败行
```

**发私聊不发群** —— 理由抄 `_move_announce` 的注释：「这是要人去做的事，发群等于发给没有人」。
（`notify.AdminAlert` 那个群 webhook 只认 `failed` 事件，而且是群，不适合派活。）

面板进程里 `Backend.admins()` 和 `_tenant_token_cache` 都已经有了，接线成本低。
**实现上建议**：给 `Flows` 加一个可选依赖 `announce_manual(ticket) -> None`，
在 server.py 和 `cli_requests._sweep` 两处各注入一次。
**不要给它默认值**（照 `approvals` 那条注释的教训：可选依赖漏传会静默降级，
表现是「面板那条路有通知、无人值守那条路没有」，而没人会发现）。

### 5.3 卡片内容：照着就能干活

一张卡要回答四件事，缺一不可：

| 写什么 | 从哪来 |
|---|---|
| 谁要账号（姓名 + 企业邮箱） | `ticket["applicant"]["name"]` / `flows._applicant_email(ticket)` |
| 开什么登录名 | `payload["username"]`（提交时已按 `username_pattern` 校验过，就是 `wuji-<拼音>`） |
| 哪个平台/主账号 | `jiuzhang / wuji`，**写全**，别只写「九章」 |
| 单号 + 回填入口 | `notify.request_link(base_url, ticket_id, admin=True)` 做成「去面板回填」链接按钮 |

外加一句「**面板开不了九章的号，这一步必须你去控制台做**」——
管理员收到的所有其它开通类卡片都是「已经开好了，知会一声」，
不写这句，这张卡会被当成知会扫过去。

### 5.4 回填闭环：复用 `fulfil`，卡片上给深链而不是输入框

- 后端：`flows.fulfil()` 放开 kind 白名单（`KIND_RESOURCE` 或「`KIND_ACCOUNT` 且模板平台是 manual」），
  `requests_api` 的 `actions["fulfil"]` 同步放开（今天写死 `kind == "resource"`）。
- 前端：复用资源那张登记抽屉，字段换成**实际登录名**（默认填 `payload["username"]`，可改）
  + **实际邮箱**（默认填申请人企业邮箱，可改）+ 备注。
  留可改是必要的：九章控制台上可能重名，管理员会改一个名字建出来，
  而单子上必须记**真实建出来的那个**，否则名册、离职回收全指向一个不存在的号。
- **为什么不做卡片内输入框**：飞书卡片按钮收不了自由文本（要上表单卡 + 新回调 + 新状态机），
  而回填是有副作用的写操作，面板那条路有现成的会话鉴权（`_require(admin=True)`），
  卡片回调是另一套鉴权。一期用链接按钮，成本几乎为零。列进不做清单。

**回填时同步做三件事**（顺序有讲究）：

1. 先 `offline_accounts.merge_one()` 把这个号并进登记表 —— **失败就不要把单子推成 DONE**。
   理由：登记表是名册和离职检查的唯一来源，写不进去 = 这个号对离职流程不存在，
   而单子已经绿了没人会再看。常见失败是登录名不以 `wuji-` 开头（`parse()` 会按
   `login_prefix` 拒），报错文案要直接说「九章登录名必须以 `wuji-` 开头」。
2. 写 `manual_created: True` + `manual_created_at`，**不要写 `user_created`**（理由见 7.1）。
3. `store.update(... to=DONE, event="fulfilled")`，`result` 写「管理员已在九章控制台建号 `wuji-xxx`」。

**`merge_one()` 要新写在 `offline_accounts.py` 里**，不能直接用 `save_account()`：
后者是**整体替换**语义，而且会把 `as_of` 刷成今天 —— 那是说谎（其余 17 个人的名单
还是上次从控制台导出的那份）。`merge_one()` 只追加/更新一个 user，**保持 `as_of` 不动**，
history 里记一条 `by="面板回填 REQ-xxxx"`。并发安全靠已有的 `fcntl` 文件锁。
（人工登记页随后做整体替换时会不会把它冲掉？会 —— 但那说明九章控制台上真没有这个号，
冲掉是对的。这个交互要写进 docstring。）

### 5.5 没销账要能看见（三层，一层都不能省）

| 层 | 做什么 | 为什么不能只靠上一层 |
|---|---|---|
| 即时 | 5.1 的私聊 | 可能发失败、可能被刷掉 |
| 每日 | `_sweep` 每轮扫「FULFILLING + manual 平台的开账号单」，用 `iam_sync.claim_remind(sig, hours=24)` 去重，**同一批 24 小时再推一次**，直到 DONE/CLOSED | 一次性通知漏了就永远漏了 |
| 常驻 | `todo` 里那条 URGENT（4.6） | 通知全挂了的时候，面板上还看得见 |

**通知发不出去怎么办**（协调人点名的问题）：

- **绝不**因为通知失败改单子状态 —— 沿用 `_emit` 既有规矩（「通知永远不能影响申请单」）。
- 但 `_emit` 今天只 `print` 到 stderr，对这类单**不够**：它是「有人要干活」的唯一信号。
  所以在单子上记一条 `manual_notice_failed` 事件（成功则记 `manual_notice_sent`），
  待办和详情页都能看见。
- sweep 每轮重试，直到有一次成功；成功之后仍按 24h 节奏提醒，直到回填完成。
  去重状态读不到 → **照常发**（`claim_remind` 本来就是这个取向，注释里写着
  「多提醒一次的代价是一条消息，漏提醒的代价是……」）。这就是 `ram_approval`
  那条「宁可重复也别漏报」在本仓的对应物。
- 没配飞书凭证 / `admins.json` 里没有 `union_id` → sweep 打印一行明确日志，
  **且这行必须命中 `flows.TROUBLE_WORDS`**（`失败 / 中断 / 发不出去 / 出错 / 还是没写进`），
  否则 systemd 那套退出码分级会把它当成「干净一轮」退 0，告警不响。
  建议行文直接用「……通知**发不出去**（…）」。

## 6. 牵动面（按文件列全，漏一处的表现各不相同）

**`src/delivery/platforms.py`**
1. 新增 manual 平台注册表：`is_manual(pid)` / `manual_account(pid)` / `manual_login_prefix(pid)` /
   显示名。**仍然不进 `ALL`/`IDS`**，文件头注释要说明「两张表各回答一个问题：
   `ALL` = 面板能调接口的云；manual = 面板知道但动不了的平台」。

**`src/delivery/offboard.py`**
2. `MANUAL_PLATFORMS` 改为从 `platforms` 派生（值逐字不变）。**只改来源，不改行为。**

**`src/delivery/catalog.py`**
3. `_ACCOUNT` 校验按平台分支：manual 平台走 `^[a-z0-9][a-z0-9_-]{0,31}$`，
   且必须等于 `platforms.manual_account(platform)`。
4. `platform` 白名单：`kind == KIND_ACCOUNT and platforms.is_manual(platform)` 放行
   （946 行 `internal` 那个口子的同款写法）。
5. **拒掉对它无意义的字段**：manual 平台的开账号模板不许配 `groups` / `workspaces` /
   `console_login: true`。理由不是洁癖：`groups`/`workspaces` 配了没有任何代码会读
   （`_run` 直接 return），而 `console_login: true` 会让申请人在 DONE 之后看到
   「领取初始密码」按钮（`requests_api.actions["password"]`），点下去走
   `_executor("jiuzhang", …)` → `ProvisionError`。报错还算好的，最怕的是有人
   以为「按钮在就是有这功能」。
6. `awaits_human()` 加 `KIND_ACCOUNT` 分支（4.1）。

**`src/delivery/flows.py`**
7. `_run()`：九章分支，**插在 2587 行 `ex = self._executor(...)` 之前**（第 3 节红线）。
8. `options()`：manual 平台显式短路（4.5）。
9. `fulfil()`：放开 kind 白名单 + account 分支（5.4）。
10. `_EXEC_FIELDS`：**不动**。`platform` 已经在里面，模板被改到别的平台会被核对拦住。
11. `_approval_fields()`：account 分支照送 `jiuzhang/wuji`，**前提是审批定义里已有这个选项**（2.1①）。
12. `announce_manual` 依赖注入（5.2），`_emit("fulfilling")` 时按 `awaits_human + is_manual` 分流。
13. `link_pending()` / `_validate()` 里读 `user_created` 的地方要把 `manual_created` 一并算上（7.1）。

**`src/delivery/health.py`**
14. 第 339 行那个循环之前，把 `accounts` 过滤成 `platforms.IDS` 里的（2.1③）。
    顺手确认 269–273 行那条路也被同一个过滤覆盖。

**`src/delivery/todo.py`**
15. 新增 `request_manual_account`（4.6）。`todo.py:206` 的硬编码收敛列二期。

**`src/delivery/notify.py`**
16. `message("fulfilling")` 的 account 分支、`_account_how()` 的 manual 分支（4.3）；
    新增一张「去九章开号」的管理员卡（照 `move_card` 写）。

**`src/delivery/requests_api.py`**
17. `actions["fulfil"]` 放开；`_view` 带一个 `manual_platform: bool`
    （**别让前端自己硬编码 `"jiuzhang"`**，那会是第五份副本）；
    新事件名进 `EVENT_LABELS`，否则事件流里显示原始英文键。

**`src/delivery/offline_accounts.py`**
18. 新增 `merge_one()`（5.4）。

**`src/delivery/web/requests.js`**
19. `fulfilling` 那条 banner（915 行）按 `manual_platform` 分流文案；登记抽屉加 account 分支表单。

**`identity/request-templates.json`**
20. 加一条九章模板。**活数据，只能合并、绝不整份覆盖**；服务器上改必须 `sudo -u delivery`
    （root 写会把属主改掉，面板从此读不了，且没有任何告警）。

**飞书审批定义（控制台，人工一次性，先于代码）**
21. 「云账号」单选控件加 `jiuzhang/wuji` 选项（2.1①）。发一张真实测试单确认不被 fail-closed 拒。

## 7. 三个容易踩的坑（写清楚，别在实现时重新发明）

### 7.1 别复用 `user_created`

`user_created` 在阿里/火山的语义是「**这张单在云上建过号**」，被四处读：
`claim_password`（领初始密码）、`push_iam` / `retry_iam_writes`（补写公司 IAM 属性）、
`reopen`（防重复建号）、`link_pending`。

给九章单置 `user_created=True` 的后果最坏的是第二条：
`retry_iam_writes()` 是**定时任务**，会自动对「`kind=account` + DONE + `user_created` + 未 `iam_written`」
的单子调 `push_iam` → `_write_iam(union_id, "jiuzhang", "wuji", "wuji-xxx")`，
而 `iam_sync.py:223` 明确跳过不在 `IDS` 里的平台。结果是**每一轮都失败一行日志、
永远补不上**，而那行会命中 `TROUBLE_WORDS` → 定时任务每天报一次假故障。

所以：用独立字段 `manual_created`。代价是 `link_pending()` 和 `_validate()` 的用户名占用
判据要把它一并算上（第 6 节第 13 条）—— 漏了 `_validate` 那处的表现是
「别人能拿同一个登录名再提一张单」。

### 7.2 别调 `_write_iam_attr` / `_link_account` / `_comment_login`

这三个是阿里/火山建号链的尾巴，九章一个都不该走：

- `_write_iam_attr`：见 7.1，公司 IAM 不收九章的登录名。
- `_comment_login`：里面 `platforms_mod.get(tpl.platform)`（flows.py:3090）→ `PlatformError`。
- `_link_account`：它写的是「人工记录」那份真相；而九章走的是 `offline-accounts.json`
  那份（模块注释明写「**名单本身就是确认**」）。两处都写就是两处真相，
  日后只会被修一边。**只写登记表。**

### 7.3 别让「回填」变成第二条开通路径

回填只做三件事：写登记表、记字段、推 DONE。**不许在这一步调任何云接口，
也不许在这一步做审批复核**（审批复核已经在 `execute()` 里做过了，
`_verify_approval` 那套回拉实例详情、只认实例级 `APPROVED` 的硬门）。
把复核挪到回填处，等于给一条已经通过的单子加第二道语义不同的门，
而两道门迟早不一致。

## 8. 任务拆解

工作量粗估，单位 0.5 天 = 半个工作日。**P0 = 本期必须；P1 = 二期**。

| ID | P | 任务 | owner | 依赖 | 估 | 验收标准 |
|---|---|---|---|---|---|---|
| JZ-0 | P0 | 飞书审批定义「云账号」单选控件加 `jiuzhang/wuji` 选项 | dev（控制台） | — | 0.5 | 真发一张测试单，确认渲染成中文、**不被 fail-closed 拒整张表单**。**必须先于 JZ-9 上线** |
| JZ-1 | P0 | `platforms.py` manual 注册表（`is_manual` / `manual_account` / `login_prefix`）+ `offboard.MANUAL_PLATFORMS` 改派生 | dev | — | 0.5 | `is_manual("jiuzhang") is True`；`"jiuzhang" not in IDS`；`MANUAL_PLATFORMS == ("jiuzhang",)` 逐字不变；**offboard 现有用例一行不改照绿** |
| JZ-2 | P0 | 体检修复：`health.collect()` 的执行身份循环过滤非 `IDS` 平台 | dev | JZ-1 | 0.5 | **先复现**：喂一份含 jiuzhang 用户的快照 + 一条 jiuzhang 模板，今天 `collect()` 抛 `PlatformError`；修完正常返回，且结果里**没有**「九章 执行身份」这一项 |
| JZ-3 | P0 | `catalog.py`：manual 平台 account 校验 + platform 放行 + 拒 `groups`/`workspaces`/`console_login` + `awaits_human` 分支 | dev | JZ-1 | 1 | 五条用例：① `account: "wuji"` 能加载；② `account: "wuji2"`（≠注册值）被拒；③ 配 `groups` 被拒；④ 配 `console_login: true` 被拒；⑤ `awaits_human(九章模板) is True` 且 `awaits_human(阿里开账号模板) is False` |
| JZ-4 | P0 | `flows._run()` 九章分支 + `options()` 短路 | dev | JZ-3 | 1 | 跑一张单到底：状态落 **FULFILLING** 不是 DONE；`result` 含「面板没有建号」、**不含**「已新建」「已开通」；全程 `self._executor` **一次都没被调用**（用桩计数断言） |
| JZ-5 | P0 | 管理员通知：`announce_manual` 注入（server + sweep 两处）+ 卡片 + `fulfilling`/`_account_how` 文案分支 | dev | JZ-4 | 1 | 审批通过那一刻触发一次私聊；卡片上有姓名、企业邮箱、`wuji-xxx`、单号、可点的回填深链、以及「面板开不了九章的号」这句；申请人卡片**不含**「只能用访问凭证」 |
| JZ-6 | P0 | 通知兜底：`manual_notice_sent/failed` 事件 + sweep 24h 重复提醒（`claim_remind`）+ 问题行命中 `TROUBLE_WORDS` | dev | JZ-5 | 0.5 | 通知全失败时：单子状态不变、事件表里有 `manual_notice_failed`、sweep 下一轮**还会再推**、日志行命中 `is_trouble()`；连续两轮不重复推（24h 窗内） |
| JZ-7 | P0 | 回填：`fulfil()` 放开 + `manual_created` + `offline_accounts.merge_one()` + `actions["fulfil"]` + 前端表单 | dev | JZ-4 | 1.5 | 回填后：登记表里多了一条（`as_of` **没被改动**、history 多一条带单号）；单子 DONE；`options()` 对本人显示 **owned**；登录名不带 `wuji-` 前缀时**报错且单子留在 FULFILLING** |
| JZ-8 | P0 | 待办：`todo` 新增 `request_manual_account`（URGENT） | dev | JZ-4 | 0.5 | 有 N 张 FULFILLING 九章单时待办页出现「N 张单等你去九章控制台开号」，点进去是筛好的列表；回填后计数减一 |
| JZ-9 | P0 | 模板落地（`identity/request-templates.json` 合并）+ 线上真单验收 | dev | JZ-0, JZ-3…JZ-8 | 0.5 | 线上走通一遍：提交 → 审批 → 管理员收到私聊 → 控制台建号 → 回填 → 申请人看到 owned。**本地绿不算数** |
| JZ-10 | P0 | 单测（含变异检查） | tester | JZ-3…JZ-8 | 1.5 | 变异清单每一条都要有用例变红：① 把 `_run` 的九章分支挪到 `ex = self._executor(...)` **之后**；② `awaits_human` 对九章返回 False（单子会落 DONE）；③ 去掉 catalog 的 `console_login` 拒绝；④ 去掉 `options()` 短路并重排 if 顺序；⑤ `merge_one` 改成刷新 `as_of`；⑥ 回填改成写 `user_created` |
| JZ-11 | P0 | 安全/一致性审计 | auditor | JZ-9 | 0.5 | 无阻塞项。重点：`_run` 全程不碰云；`fulfil` 没有变成第二条开通路径；台账文案不说「已开通」；通知失败不改状态但留痕；`merge_one` 的锁与校验；九章单拿不到初始密码/IAM 补写/重试这三个按钮 |
| JZ-12 | P0 | 文档：`docs/` 里写「九章开号怎么办」（管理员视角一页） | docwriter | JZ-9 | 0.5 | 新管理员照着能独立完成一次：收到卡 → 控制台建号 → 回填 → 确认名册里出现 |
| JZ-13 | P1 | 收敛九章硬编码：`todo.py:206`、`web/iam.js:470 BY_HAND` 改读统一判定 | dev | JZ-1 | 0.5 | 全仓 grep `"jiuzhang"` 只剩注册表一处（数据文件除外） |
| JZ-14 | P1 | 体检项：模板 account / `username_pattern` 前缀 与 `offline-accounts.json` 的 `account` / `login_prefix` 不一致时报 WARN | dev | JZ-3 | 0.5 | 故意改成不一致 → 体检页出现明确一条，写明后果是「名册对不上、开完号还显示可申请」 |
| JZ-15 | P1 | 管理员通知扩面到其它 `FULFILLING`（resource / storage） | dev | JZ-5 | 0.5 | 先评估：transfer 的 FULFILLING 由 `mover` 自动推进，**不能**发给管理员，否则是噪音 |

**提交闸门（硬规）**：任何改动必须 auditor 审计通过、无阻塞项才能 `git commit`。JZ-11 不是可选项。

### 并行与顺序

- **立刻能并行**：JZ-0（飞书控制台，别人的系统，有等待成本）、JZ-1（纯注册表）、
  JZ-2（体检修复，独立 bug）。
- **必须串行**：JZ-0 **先于** JZ-9 上线（审批定义先于代码，同 MLflow 的 SA-9）；
  JZ-3 → JZ-4 → {JZ-5, JZ-7, JZ-8}；JZ-5 → JZ-6。
- **JZ-2 可以单独先提交**：它是一个既存 bug 的修复，和本方案其余部分无耦合，
  也不需要等飞书那边。

### 先做什么

1. **JZ-0**（飞书审批定义）—— 别人的系统、有等待成本，且必须排在代码前面。
   不做这一步，模板上线当天所有九章申请都会「提交失败」，而错误信息指向的是飞书表单，
   没人会想到是单选控件少了个选项。
2. **JZ-2** —— 独立 bug，先复现先修，顺手把体检页救回来。
3. **JZ-1 + JZ-3 + JZ-4** —— 地基，决定单子会不会错落 DONE。
4. **JZ-5 + JZ-6 + JZ-7 + JZ-8** —— 闭环（通知 / 回填 / 看得见），四条一起才算完整。
5. JZ-9 → JZ-11 → JZ-12 收口。

## 9. 不做什么（明确排除，别在评审里重开）

- **不给九章写任何云接口适配器** —— 它没有开放 API，也开不了通用服务器（只有机器学习大卡）。
  `platforms.ALL` 不加它。
- **不在飞书卡片里做回填输入框** —— 卡片收不了自由文本，要上表单卡 + 新回调 + 新鉴权，
  一期用「去面板回填」深链，成本几乎为零（5.4）。
- **不新建审批定义、不定新审批人** —— 全局那条 `approval_code` 够用，模板 `approval` 字段留空。
  要给九章单独的审批人，得在飞书那边配条件分支，本期不做。
- **不复用 `user_created`**（7.1）、**不走 `_write_iam_attr` / `_link_account` / `_comment_login`**（7.2）。
- **不给九章开 `console_login`** —— 面板发不出九章的初始密码，按钮在就会有人点。
- **不把 `KIND_ACCOUNT` 塞进 `AWAIT_FULFIL`**（4.1）。
- **不在回填那一步做审批复核**（7.3）。
- **不做九章号的到期自动回收** —— 开账号类本来就没有到期（`_expires_at` 对 account 返回空），
  回收靠离职流程，那条路已经通了（`offboard.MANUAL_PLATFORMS`）。

## 10. 开放问题（交 dev 去问用户 / 去核实，别猜）

1. **九章控制台建号时，登录名能不能由申请人指定？** 整套「申请时填 `wuji-<拼音>` →
   管理员照着建」都建立在「能指定」之上。如果九章是系统生成名字，
   那申请表单上就不该有登录名输入框，回填时才知道名字 —— 表单和文案都要改。
   **这条是整个方案的地基，请先确认。**
2. **谁是九章的管理员？** `identity/admins.json` 里那批人是面板管理员，
   和「有九章控制台账号、能建号的人」是不是同一批？不是的话，通知收件人要单独一份名单
   （建议仍放 `admins.json`，加一个角色字段，别新造文件）。
3. **`offline-accounts.json` 里的 `account` 确认是 `wuji`？** 模板要和它逐字相等（4.4）。
   顺便确认九章的主账号在飞书审批定义里该叫什么（JZ-0 要用）。
4. **九章号的邮箱** 是不是一律等于公司企业邮箱？名册靠邮箱匹配，
   如果管理员建号时用别的邮箱，回填表单必须能改（5.4 已按「能改」设计，请确认）。
5. **要不要给九章单设一个「大约多久能开好」的承诺？**（申请人卡片上写不写「一般 1 个工作日内」）
   写了就是承诺，不写申请人会反复催管理员。
6. **除了九章，还有没有第二个「面板开不了的平台」？** 如果一个月内还有第二个，
   manual 注册表的设计现在就要多看一眼（JZ-1 已按注册表做，但表单文案还是九章专属）。

## 11. 上线步骤（部署惯例，别漏）

1. **飞书审批定义**加 `jiuzhang/wuji` 选项（JZ-0），发测试单确认不被拒。
2. 服务器上 `sudo -u delivery` 编辑 `identity/request-templates.json`，**合并**加入九章模板
   —— 活数据，**绝不整份覆盖**。
3. 同步代码：面板惯例是**只同步 `src/`**，`rsync` 必须 `--chown=delivery`。
4. 本方案**不新增 systemd 单元、不新增环境变量**（通知复用 `DELIVERY_FEISHU_APP_ID/SECRET`
   + `DELIVERY_BASE_URL` + `identity/admins.json`）。所以 `systemctl restart cloud-panel` 即可；
   如果实现中新增了环境变量，记住 **restart 重读 panel.env，但新增单元/timer 要单独拷 + `daemon-reload`**。
5. 跑一次 `deploy/panel/preflight.py --env /etc/delivery/panel.env`，确认 rc=0。
6. 打开 `/api/admin/health` 确认体检页能开（JZ-2 的线上验证点）。
7. **线上**走一张真单到底（JZ-9 的验收），包括「管理员真的收到了私聊」这一步 ——
   本地绿不算数，通知这条链只有在线上才有真的 token 和真的收件人。

---

## 追加（2026-09-24，真机查实）：云账号选项值对不上，必须加一层映射

飞书审批定义 `301E99EB-A4BC-4F08-AFAF-46906A006C08` 的「云账号」单选，
**新加的两个选项值是飞书自动生成的 ID，不是 `平台/账号`**：

```
值='aliyun/1704065796538912'      显示='阿里云主账号 1704065796538912'   ← 建定义时用 API 指定的
值='volcano/2111674479'           显示='火山引擎主账号 2111674479'        ← 同上
值='muf5nlly-chtvxx138id-1'       显示='九章主账号 wuji'                  ← 后台手工加的
值='muf8o2yb-hk8w346dj6-1'        显示='TurboAI（曦望）Wuji-Algorithm@wuji.tech'
```

实测：飞书后台编辑器**只能改显示名、改不了选项值**（用户试过）。有意义的值只有
建定义时通过 API 指定才行，而这条定义**用户明确要求不要动**。

后果：面板 `flows._approval_fields()` 送的是 `f"{platform}/{account}"` →
送 `jiuzhang/wuji` 而选项里没有这个值 → **飞书拒掉整张表单、单子落「提交失败」**，
报错和「云账号填错了」毫无关系，排查会往别处跑。

**做法**：`identity/approval.json` 加一张 `account_options` 对照，送审批前查表；
查不到就照旧送 `平台/账号`（阿里/火山行为逐字不变）。

**两条硬要求**：
1. 选项被删掉重加时飞书会换 ID，映射随之失效 —— 症状又是「提交失败」。
   所以送出前要**校验映射里的 ID 仍存在于当前定义中**，对不上就**拒绝提交并说明**，
   不要发出去让飞书拒（那样错误信息没有指向性）。
2. 这张表属于「别人系统里的标识」，和 `identity/` 里其它活数据一样不进 git。

顺带记：`account` 字段现在被 `catalog._ACCOUNT` 要求是 6–20 位数字，
而 `wuji` 和 `Wuji-Algorithm@wuji.tech` 都过不了，放开校验是 JZ-1 的一部分。
