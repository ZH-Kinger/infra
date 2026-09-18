# 云账号邮箱三分支：企业邮箱 / 外部人员个人邮箱 / 员工个人邮箱

规划人：planner　日期：2026-09-16　仓库：`/home/l/桌面/infra`（`src/delivery/`）
状态：**待 dev 确认 + 待用户回答第 10 节的开放问题**。本文只做规划，未改任何源码。

---

## 1. 现状核实（我逐条打开源码确认过，供 dev 复核）

用户给的现状全部属实，另有两处需要一并处理的发现：

| # | 事实 | 位置 |
|---|------|------|
| 1 | 第一轮只收企业邮箱：`if not addr.endswith(suffix): continue`，`suffix = "@" + domain` | `identity/ssomap.py:163,181` |
| 2 | 个人邮箱账号与「压根没填邮箱」合流：`if not corp: no_email.append(acc)` | `ssomap.py:186-187` |
| 3 | 掉进 `no_email` 后走第二轮**按显示名猜**，条件满足时甚至直接 `STATUS_CONFIRMED` | `ssomap.py:236-267` |
| 4 | unlinked 文案硬编码「账号上没有企业邮箱」，对有 gmail 的账号是误导 | `ssomap.py:253,280,286` |
| 5 | `classify` 把非企业邮箱判 `CLASS_MISMATCH`，理由「非企业邮箱：xxx」 | `identity/audit.py:99-102` |
| 6 | 服务号机制：`manual-links.json` 的 `services` 是扁平数组 → `_manual_services`（`people.py:669`）→ `apply_manual` 的 `keep()` 从三处摘掉（`people.py:580-606`）→ `build` 写成 `unlinked[].kind="service"`（`people.py:756-767`）→ 面板 pill（`web/app.js:918`） | 见左 |
| 7 | 属性表按 `person.union_id` 决定 `match_by`，没有 union_id 的回落「email（存量回填）」 | `iam_sync.py:152` |
| **8（新发现，必须同批修）** | `classify` 用的是**裸** `endswith(domain)`，而 `identity audit` 的默认 `--domain` 是 `@wuji.tech`、`sso-map`/`refresh` 的默认是 `wuji.tech`（无 @）。两个模块对同一个参数的含义不一致；一旦有人给 `classify` 传不带 `@` 的域，`foo@evil-wuji.tech` 会被判成企业邮箱 | `audit.py:99` vs `ssomap.py:163`；`cli.py:115,140,311` |
| **9（新发现，影响方案）** | `directory.from_feishu` 把飞书的**个人邮箱塞进 `enterprise_email` 字段**：`enterprise_email=str(u.get("enterprise_email") or u.get("email") or "")`。今天靠 `people.build` 的域后缀过滤（`people.py:691`）兜住了 union_id 回填，但 `build` 末尾「只在通讯录里的人」那段（`people.py:728-741`）**不过滤域**，所以 people.json 里今天就可能存在 email 是私人地址的人员行 | `identity/directory.py:125` |

补充确认（决定方案边界的三条）：

- **外部人员不会登录本面板**。面板身份来自飞书登录（`feishu.py:125-136`，`enterprise_email` 严格取飞书企业邮箱、不回落个人邮箱）或代理头（`proxy_auth.py:254` 有 `email_domains` 白名单）。外部审计没有飞书账号 → 他们只需要能登**云控制台**，不需要在面板里有"人"。
- **`build_rows` 只遍历 `index.people`**（`iam_sync.py:149`），unlinked 里的账号天然不进属性表。所以"外部人员不进属性表"是零改动的默认行为。
- **`finance` 已经在 `SERVICE_NAMES` 里**（`audit.py:29`）→ 它现在被判服务号。本期**不动它**，理由见 6.3。

---

## 2. 概念定义：「外部人员」是什么

三类实体的边界，按「有没有自然人」和「这个自然人在不在公司通讯录里」切：

| 类别 | 是不是人 | 在飞书通讯录 | 要不要登控制台 | 要不要 cloud_accounts 属性 | 认人依据 | 数据落点 |
|------|---------|------------|--------------|--------------------------|---------|---------|
| **员工** | 是 | 在 | 要 | 要 | `union_id`（唯一绑定键） | `people.json` 的 `people[]` |
| **外部人员** | 是 | **不在** | **要** | **要（但不由本面板产出，见第 8 节）** | 管理员人工背书（姓名 + 担保人 + 到期日） | `manual-links.json` 的 `external`；名册里落 `unlinked[].kind="external"` |
| **服务号** | 否 | 不在 | 不要 | 不要 | 前缀/名单/人工标记 | `manual-links.json` 的 `services`；名册 `unlinked[].kind="service"` |

**外部人员与服务号的本质区别**（决定了不能直接抄 `services` 的扁平数组）：服务号只需要"这个 key 是服务号"一个 bit；外部人员背后有一个真人，需要知道**是谁、谁担保、什么时候到期**——否则标记一次就永久静默一个能登控制台的账号，没人再看它。所以 `external` 必须是**带负载的对象**，不是扁平数组。

**一条硬原则（写进代码注释，auditor 按它验收）：**

> **「对不上通讯录」不等于「外部人员」。** 对不上通讯录同时也是这些情况的表现：通讯录采集权限不足、新同事还没入职通讯录、离职后被移出、显示名写错。把"证据缺失"当成"外部人员"的授权依据，等于允许任何一个来源不明的个人邮箱账号获得登录通道。**自动分类只能产出「待定」，「外部人员」必须由管理员显式标记。**

---

## 3. 判定规则（四态，不是三态）

输入：一个 `CloudAccount`（`scope/name/display_name/emails`）+ 企业域 + 通讯录证据集。

```
A. 账号上有企业邮箱（任意一条 claim 以 @domain 结尾）
   → 现有第一轮逻辑，逐字不变（confirmed / review / 多企业邮箱拦截）

B. 账号上一条邮箱都没有
   → 现有第二轮逻辑，逐字不变（显示名 → 用户名 → unlinked，reason 保持「账号上没有企业邮箱」）

C. 账号上只有个人邮箱
   C1. 被管理员标记为 external 且未过期
       → kind="external"，不进任何人名下，不再计入"未关联"告警
   C2. 命中通讯录（是在职同事）
       → Unlinked，reason：「账号上登记的是个人邮箱 <addr>，但通讯录里有同名/同邮箱的在职同事
                            <姓名>，请改用企业邮箱后重新刷新」
       → **不再走第二轮显示名对应**（这正是"不能拿个人邮箱去自动对应"）
   C3. 对不上通讯录，且通讯录证据可用
       → Unlinked，reason：「账号上只有个人邮箱 <addr>，通讯录里查不到对应的同事。
                            若确为外部人员，请在名册审核里标记；否则请补企业邮箱」
   C4. 通讯录证据不可用（没拿到通讯录 / 采集失败 / 为空）
       → Unlinked，reason：「账号上只有个人邮箱 <addr>，本次没有通讯录数据，无法判断内外」
       → **fail-closed**：既不判外部、也不判同事，更不自动对应
```

**通讯录证据的强弱分级**（`is_colleague` 的判据，从强到弱）：

1. 个人邮箱 == 通讯录某人的**个人邮箱字段**（飞书 `user.email`）→ 硬证据，判 C2。**前提是这个字段真的采得到，见 R1。**
2. `_name_key(display_name)`（`identity/mapping.py:84`）命中通讯录姓名 → 判 C2（这是把"同事拿个人邮箱"揪出来的主力，因为云上显示名基本都是真名）。
3. 用户名可由通讯录某人的企业邮箱前缀推出（`mapping.candidates`）→ 判 C2。

1 是精确匹配，2/3 会有重名/巧合误判。**误判方向是安全的**：把外部人员误判成同事 → 该账号停在"待确认"、管理员手工标 external 即可；反过来把同事误判成外部人员才是危险的（给他开了一条不经 union_id 的通道）。所以宁可宽判 C2。

**通讯录证据源（两条，按可用性取）：**

- 主源：`identity/directory.py` 的 `DirectoryEntry` 列表（`--directory feishu` 或 `csv:`）。
- **降级源：上一份 `people.json` 里 `union_id` 非空的人**。理由：`refresh` 的 `--directory` 默认就是 `none`（`cli.py:308`），若只认主源，线上定时刷新永远落 C4、三分支等于没上线。名册里有 union_id 的人本来就是从通讯录回填来的，拿它当同事名单是合理的历史快照；弱点是漏掉刚入职、还没进过名册的同事 → 落 C3/C4，方向安全。

---

## 4. 数据落点（精确到文件和字段）

### 4.1 `identity/manual-links.json` 新增 `external`

```json
{
  "links":    { "tom@wuji.tech": { "name": "…", "accounts": ["aliyun/<UID>/tom"] } },
  "rejected": { "aliyun/<UID>/tom2": ["tom@wuji.tech"] },
  "services": ["aliyun/<UID>/ci-bot"],
  "external": {
    "aliyun/<UID>/kpmg-li": {
      "name":    "李某（某会计师事务所）",
      "email":   "li@gmail.com",
      "sponsor": "tom@wuji.tech",
      "expires": "2026-12-31",
      "note":    "年度审计，工单 REQ-123"
    }
  }
}
```

- 键：账号 key `平台/账号ID/用户名`，与 `rejected` 同形（`people._manual_key`，`people.py:649`）。
- `name` / `sponsor` / `expires` **必填**；`email` 选填（填了就与账号上的个人邮箱做一致性校验，不一致拒绝写入——防手滑标错账号）；`sponsor` 必须是名册里存在的企业邮箱。
- `expires` 过期后，`propose` **不再**把它当 external（落回 C3），面板重新显示"待确认"、`refresh` 重新告警。**到期逻辑只写在分类器一处**，不散在告警代码里。

### 4.2 提案 `identity/sso-map.proposal.json` 新增顶层 `external`

`Proposal` 加 `external: tuple`，`to_dict()` 输出 `"external": [{"scope","name","display_name","subject","expires"}]`，`counts` 加一项。schema 串 `wuji-sso-map/proposal@1` 是否升版本见 T2 验收。

### 4.3 名册 `identity/people.json`

外部人员进 `unlinked[]`，`kind="external"`，`reason` 写「外部人员（<name>，担保人 <sponsor>，<expires> 到期）」。

**本期不给外部人员建 `people[]` 行**——理由见 8.2。

---

## 5. 改动清单（按模块 + 依赖顺序）

```
Phase 0 调研（不改码）
  R1 飞书通讯录个人邮箱字段可用性        researcher
  R2 线上"只有个人邮箱"的账号盘点        researcher（只读探针）
  R3 IT 侧外部人员 IAM 账号的匹配键      dev 去问 IT/用户

Phase 1 分类与标记（本期主体）
  T1 数据模型        people.py: _manual_external / apply_manual / build
  T2 分类器          identity/ssomap.py: propose 四态 + Proposal.external + render
  T3 通讯录证据      identity/ssomap.py: is_colleague + cli.py/refresh.py 注入
  T4 审核操作        review.py: op="external" 全链路
  T5 面板前端        web/app.js: pill + 标记表单 + 人工记录表
  T6 告警            refresh.py: _unlinked_keys 放行未过期 external
  T7 CLI 报告        identity/audit.py: CLASS_EXTERNAL + 域名归一化（现状 #8）
  T8 外部人员清单导出 iam_sync.py 旁路（只读，不动 CSV 表头）
  T9 文档            docs/permissions.md / admin-guide.md / cloud-access-platform.md

Phase 2 属性表一等公民（待 R3 + 开放问题 Q2 回答后才开工）
  T10 external 进 people[] + match_by 新键 + CSV 加列 + 基线迁移
```

依赖图：`T1 → T2 → {T3, T4}`；`T4 → T5`；`T2 → T6`；`T7` 独立可并行；`T8` 依赖 `T1`；`T9` 依赖 T1–T8 全绿；`T10` 依赖 R3 + Q2 + Phase 1 全部上线。

---

## 6. Task 明细

约定：owner 里的 `dev` 是唯一源码改写者；`tester` 只写 `tests/`；`auditor` 只读复审。工作量按"一个人一次专注会话"估：S ≈ 半天内，M ≈ 一天，L ≈ 多天。

### R1 — 飞书通讯录能不能给出个人邮箱
- owner: researcher　blockedBy: 无　工作量: S　风险: 低
- 做什么：查飞书 `contact/v3/users/find_by_department` 返回体里 `email` 字段的语义与填充条件；确认现有 scope（`login.py:88` 已声明 `contact:user.email:readonly`）是否覆盖通讯录批量接口（而不只是登录时的 `user_info`）。
- 验收：给出官方文档原文 + 字段名 + 权限项名；明确回答「通讯录批量接口能否稳定拿到个人邮箱」。拿不准就写【拿不准】，不猜。产出写 `docs/collab/research/`。

### R2 — 线上"只有个人邮箱"的账号盘点
- owner: researcher（只读探针）　blockedBy: 无　工作量: S　风险: 低（只读）
- 做什么：用现成只读路径（`delivery identity sso-map` 的采集侧，或直接读最近一份 `identity/sso-map.proposal.json` + 云上 `GetUser`/`GetVerificationInfo` 结果）统计：① 只登记个人邮箱的账号共几个、分布在哪朵云；② 其中显示名能对上通讯录/名册的有几个（= 第 3 节的 C2，也就是"要被拒掉"的那批）；③ 完全对不上的有几个（候选外部人员）。
- 验收：一张表，逐行 `scope/name/display_name/个人邮箱域名/是否命中通讯录`。**个人邮箱地址本体不要写进 `research/`**（那目录不像 `identity/` 那样整体 gitignore），只写域名和计数。
- 为什么重要：C2 那批的规模直接决定开放问题 Q3 的答案（是"逼他们改邮箱"还是"先标红再改"）。

### T1 — `external` 数据模型与提案合并
- owner: dev　blockedBy: 无（可与 R1/R2 并行）　工作量: M　风险: 中
- 改：`src/delivery/people.py`
  - 新增 `_manual_external(value) -> dict`，照 `_manual_rejected`（`people.py:656`）的写法做严校验：键过 `_manual_key`；值必须是对象且含非空 `name`/`sponsor`/`expires`；`expires` 必须是 `YYYY-MM-DD`；格式不对一律 `raise PeopleError`（沿用本仓"宁可整份拒绝"的口径）。
  - `apply_manual`：`external` 的账号加入 `keep()` 的排除集（`people.py:580-582`），即从别人的 `links`、`unlinked`、`services` 三处一并摘掉；并在输出里补齐 `out["external"]`（对齐 `services` 在 `people.py:623-628` 的补齐逻辑）。
  - 互斥校验：同一账号同时出现在 `links`/`services`/`external` 任意两处 → `raise PeopleError`（照 `people.py:576-578` 那条已有的 links×services 互斥）。
  - `build`：把 `proposal["external"]` 写进 `unlinked[]`，`kind="external"`，`reason` 带 name/sponsor/expires。
- 验收：
  1. `apply_manual` 后，被标 external 的账号不出现在任何人的 `links`、不出现在 `services`、只出现在 `external`。
  2. 同一账号既在 `links` 又在 `external` → 抛 `PeopleError`，消息点名该账号。
  3. `expires` 写成 `2026/12/31`、空串、缺 `sponsor` → 各自抛 `PeopleError`。
  4. `build` 产出的 `people.json` 能被 `parse()` 加载（自检不回归）。
  5. 老的 `manual-links.json`（无 `external` 键）行为逐字不变。

### T2 — `propose` 四态分类
- owner: dev　blockedBy: T1　工作量: M　风险: **高（核心判定）**
- 改：`src/delivery/identity/ssomap.py`
  - `propose(...)` 新增参数 `external: Mapping = {}`（来自 manual）、`colleagues: Colleagues|None = None`（T3 提供）、`today: date`（可注入，测试要能锁到期行为）。
  - 把 `if not corp: no_email.append(acc)`（`ssomap.py:186`）拆成三路：有企业邮箱 / 有邮箱但都不是企业域 / 一条邮箱都没有。**第三路的行为、文案、状态判定必须逐字不变**（回归基线）。
  - 第二路按第 3 节 C1–C4 处理；C2/C3/C4 产出 `Unlinked` 并**不进第二轮**。
  - `Proposal` 加 `external` 字段；`to_dict()`/`render()` 增一节「外部人员」，`counts` 加一项。
  - 到期判定只在这里：`expires < today` → 该记录不生效，落 C3 并在 reason 里写「外部人员标记已于 <date> 到期，请续期或改判」。
- 验收：
  1. 账号只有 gmail + 显示名能对上一个有企业邮箱的人 → **不再**产出 `Link`（回归旧行为的反向断言），产出 reason 含"请改用企业邮箱"的 `Unlinked`。
  2. 账号既有企业邮箱又有 gmail → 走第一轮、结论与改动前逐字相同。
  3. 账号一条邮箱都没有 → 第二轮结论与改动前逐字相同（建议 tester 用改动前的一组快照做金标准对比）。
  4. 标了 external 且未过期 → 进 `proposal["external"]`，不在 `links`/`unlinked`。
  5. 标了 external 但 `expires` 是昨天 → 落 `unlinked`，reason 含"已到期"。
  6. `colleagues=None`（没有通讯录）→ 一律 C4，**没有任何账号被判成 external 或被判成同事**。
- 风险点：第 3 条最容易回归，第 6 条是 fail-closed 的生命线。

### T3 — 通讯录证据源与注入
- owner: dev　blockedBy: T2　工作量: M　风险: 中
- 改：
  - `identity/ssomap.py` 新增纯函数 `build_colleagues(entries, *, domain, fallback_people=())` → 返回 `{personal_emails:set, name_keys:set, username_candidates:set}`；`is_colleague(account, colleagues)` 按第 3 节的 1/2/3 级判据。**纯数据、不联网**，以便单测。
  - `cli.py:_cmd_identity_ssomap`（`cli.py:930`）：把 `--directory` 的 entries 传给 `propose`。注意该子命令当前**没有** `--directory` 参数，需要加（默认 `none`）。
  - `cli.py:_refresh_locked`（`cli.py:1417`）+ `refresh.run`（`refresh.py:167`）：当前顺序是 `proposal = collect_proposal()` 然后 `entries = list(directory())`（`refresh.py:221-222`）。要改成先取 entries、再把它传进 `collect_proposal(entries)`。**改签名要连 `tests/unit/test_delivery_refresh.py` 里的桩一起改。**
  - 降级源：`--directory none` 时用 `previous_people` 里 `union_id` 非空的人构造 `fallback_people`。
- 验收：
  1. `--directory feishu` 且通讯录里有同名的人 → 该个人邮箱账号判 C2。
  2. `--directory none` + 上一份名册里有同名且有 union_id 的人 → 同样判 C2（降级源生效）。
  3. `--directory none` + 没有上一份名册 → 全部 C4，零 external、零 C2。
  4. 通讯录接口抛 `FeishuError` → `refresh` 按现有规矩进 `problems` 且**名册不重建**（`refresh.py:213-218` 的纪律不被绕过）。

### T4 — 名册审核加 `external` 操作
- owner: dev　blockedBy: T1（可与 T3 并行）　工作量: M　风险: **高（授权边界）**
- 改：`src/delivery/review.py`
  - `OPS` 加 `"external"`（`review.py:52`）。
  - `_edit_manual`（`review.py:153`）：新增 `drop_external()`；**`confirm`/`assign`/`undo` 三个分支都要调它**，否则一个账号会同时是"某人的"和"外部人员的"。`service` 分支也要 drop external，反之 `external` 分支要 `drop_links()` + drop service。
  - `external` 分支要求 `action` 里带 `name`/`sponsor`/`expires`（新的入参字段），走 T1 的校验。
  - `_proposal_accounts`（`review.py:131`）：把 `external` 也算进"提案里有这个账号"，否则已标 external 的账号再也做不了 `undo`/改判（`review.py:391` 那道门会拦掉）。
  - `_check_target`（`review.py:315`）：`assign`/`confirm` 的目标账号若已在 `manual["external"]` → 拒绝，提示先撤销（对齐 `review.py:333` 对 owner 的处理）。
  - `add_link`（`review.py:436`，开号申请成功后自动挂号用）：已标 external 的账号**拒绝自动对应**（现有那条 `or account in manual["services"]` 旁边加一项）。
  - `records()`（`review.py:472`）：列出 external 记录，`kind="external"`，带 name/sponsor/expires 供撤销。
- 验收：
  1. `op=external` 后再 `op=assign` 给某人 → 409，提示先撤销。
  2. `op=assign` 之后再 `op=external` → external 生效且 `links` 里那条被摘掉（`drop_links` 生效），名册里该账号不再挂在那人名下。
  3. `op=undo` 能清掉 external 记录并回到规则推断结果。
  4. `add_link`（模拟开号申请成功）对已标 external 的账号抛 `ReviewError(409)`。
  5. 审核日志 `review.log` 记下了 op/account/actor/sponsor/expires。
  6. 缺 `sponsor` 或 `expires` 的 `op=external` 请求 → 400，不写任何文件。
- 风险点：第 2 条是"中间态会坏"的典型——只加 `OPS` 不改 `_edit_manual` 的 drop，就会出现同一账号两处登记，`people.build` 的结果取决于遍历顺序。

### T5 — 面板前端
- owner: dev　blockedBy: T4　工作量: M　风险: 低
- 改：`src/delivery/web/app.js`
  - `unlinkedSection`（`app.js:863`）：加「标为外部人员」按钮，弹一个要填 `使用人姓名 / 担保人（下拉取 assignable） / 到期日` 的小表单；`kind === "external"` 时渲染 pill「外部人员（<到期日>到期）」而不是「待确认」（`app.js:918`）。
  - `RECORD_KIND`（`app.js:708`）加 `external: "外部人员"`；顶部提示语 map（`app.js:24`）加一条。
  - `views.py:_unlinked_rows`（`views.py:189`）已经透传 `kind`/`reason`，**大概率不用改**；若要在前端显示 sponsor/expires，则 `people.Unlinked` 要加字段并在 `views` 透传（评估后决定，别为了显示把 dataclass 改肥）。
- 验收：① 未关联区能一键标记，二次确认文案写明"标记后该账号不再计入未关联告警，直到 <到期日>"；② 已标记的行显示外部人员 pill 且没有"分配给"按钮；③ 人工记录表能撤销。

### T6 — 刷新告警口径
- owner: dev　blockedBy: T2　工作量: S　风险: 低
- 改：`refresh.py:_unlinked_keys`（`refresh.py:157`）：`if u.get("kind") not in ("service", "external")`。
- 验收：① 标了 external 的账号不再出现在「新出现的未关联账号」；② 标记到期后的下一次刷新，它**重新**出现在该告警里（靠 T2 的到期落回，本任务不写到期逻辑）。

### T7 — CLI 审计报告口径 + 域名归一化
- owner: dev　blockedBy: 无（可并行）　工作量: S　风险: 中（含一个真 bug）
- 改：`src/delivery/identity/audit.py`
  - 新增 `CLASS_EXTERNAL = "external"`，`classify` 在"非企业邮箱"分支按传入的 external 名单/通讯录判据细分，note 分别写「外部人员（已登记）」/「在职同事使用个人邮箱，请改用企业邮箱」/「个人邮箱，内外未定」。
  - **修现状 #8**：`classify` 内部先做 `suffix = "@" + domain.lstrip("@").lower()` 再 `endswith(suffix)`，与 `ssomap.py:163` 统一；`AuditReport.ready`（`audit.py:74`）要明确 external 算不算"就绪"（建议算，外部人员不阻塞 SSO 开关，但要在 `summary()` 里单列）。
  - `identity/report.py` 的渲染与 CSV 增一类。
- 验收：① `--domain wuji.tech`（不带 @）时 `x@evil-wuji.tech` 被判非企业邮箱（回归断言）；② 四类计数在 `summary()` 里齐；③ `ready` 的语义变化在 docstring 里写清楚。

### T8 — 外部人员清单导出（属性表旁路）
- owner: dev　blockedBy: T1　工作量: S　风险: 低
- 做什么：不动 `CSV_HEADER`、不动 `build_rows`。新增一个只读清单（CLI 子命令 `delivery identity external-list` 或 IAM 面板上的一个只读区块），输出 `平台 / 账号 / 用户名 / 使用人 / 个人邮箱 / 担保人 / 到期日 / 应配的 cloud_accounts 值`，交给 IT 手工在公司 IAM 里配。
- 写盘必须过 `cli._require_identity_dir`（含个人邮箱）。
- 验收：① 清单内容与 `manual-links.json` 的 `external` 一致；② 文件权限 0600 且落在 `identity/` 下；③ 属性表 CSV 的表头、行数、内容与本改动前逐字相同（回归断言，证明"零污染"）。

### T9 — 文档
- owner: docwriter　blockedBy: T1–T8 全绿　工作量: S　风险: 低
- 改 `docs/permissions.md`、`docs/admin-guide.md`（名册审核多了一种操作）、`docs/cloud-access-platform.md`（三分支规则 + 外部人员 SSO 怎么走）。
- 验收：管理员按文档能独立完成"标记一个外部人员 → 导清单给 IT → 到期续期"全流程。

### T10 — （Phase 2）外部人员进属性表
- owner: dev　blockedBy: R3 + 开放问题 Q2　工作量: L　风险: **高**
- 详见第 8 节。**在 Q2 有答案前不要开工**，否则大概率返工。

### T-TEST — 测试包
- owner: tester　blockedBy: 每个 T 各自完成　工作量: M
- 落点：`tests/unit/test_delivery_ssomap.py`（T2/T3）、`test_delivery_people.py`（T1）、`test_delivery_review.py`（T4）、`test_delivery_refresh.py`（T3/T6）、`test_delivery_identity.py`（T7）、`test_delivery_iam_panel.py` 或 `test_delivery_iam_export.py`（T8 的零变化回归）、`test_delivery_views.py`（T5 的后端部分）。
- 必须有的对抗用例：通讯录为空 → 零 external；到期边界（今天 = expires 当天算不算过期，选一个并锁死）；同一账号双重登记 → 抛错；`add_link` 撞 external → 409；旧 `manual-links.json` 零回归。

---

## 7. 必须同批改的集合（拆开会产生坏中间态）

| 组 | 成员 | 拆开会怎样 |
|----|------|-----------|
| **G1** | `people._manual_external` + `apply_manual` 的 `keep()` + `build` 的 unlinked 映射 | 只加解析不加 `keep()`：标记写进了文件但完全不生效，账号照样被对应给人，管理员以为标好了 |
| **G2** | `review.OPS` + `_edit_manual` 的 `drop_external`/`drop_links` 互摘 + `_proposal_accounts` | 只加 OPS：同一账号同时挂在某人名下和 external 里，名册结果取决于遍历顺序；`_proposal_accounts` 没加 → 标记后再也 undo 不了 |
| **G3** | `review._check_target` 的 assign 拦截 + `review.add_link` 的拦截 | 只改面板不改 `add_link`：开号申请流程仍能把一个已标外部人员的账号自动挂到员工名下（`server.py:540` / `cli_requests.py:413` 两个调用点） |
| **G4** | `ssomap.propose` 的 C 分支 + `Proposal.to_dict` + `render` + `people.build` | 提案里有 `external` 但 `build` 不认 → 名册里这些账号凭空消失（既不在 people 也不在 unlinked），面板上看不见 = 一个能登控制台的账号彻底脱管 |
| **G5**（Phase 2） | `CSV_HEADER` + `read_csv` + `check_baseline` + `adopt_result` + 一次 re-baseline | 加列即让 `read_csv`（`iam_sync.py:228`）拒绝所有历史基线，属性表流程当场停摆 |

---

## 8. 专题：外部人员怎么进 `cloud_accounts`

### 8.1 问题本身

属性表的每一行是「某个人在某个应用上的 cloud_accounts 值」。它的**全部安全性建立在 `union_id` 上**：`iam_export.diff` 把无 union_id 的行按邮箱认领，并有一条硬规则——「基线里这个邮箱属于另一个 union_id，当前这行没有 union_id → 判邮箱可能已复用 → skip」（`iam_export.py:49,196-208`）。这套逻辑的隐含前提是：**邮箱是公司分配的、一人一个、不共享**。

外部人员既没有 `union_id`，也没有企业邮箱。他们的公司 IAM 账号由 IT **本地建**（不是飞书同步来的）。

### 8.2 推荐方案：**本期 O1 —— 不进属性表，出一张清单给 IT 手工配；同时外部人员也不进 `people[]`**

理由（按重要性排）：

1. **零污染邮箱键空间。** 一旦让个人邮箱进 `email` 列，`diff` 的按邮箱认领与"邮箱复用"判定就要面对 gmail/共享邮箱这种本来就可能一址多人的东西。而"头号真实用例"恰好是 `finance` 这种**多名外部审计共用**的场景——一个邮箱对多个人，正是这套逻辑唯一无法表达的形状。
2. **零污染认领路径。** 若外部人员进 `people[]`，`PeopleIndex._by_email`（`people.py:199-200`）就含个人邮箱，而 `resolve()`（`people.py:228`）**自身不校验邮箱域**，只靠调用方传的是企业邮箱（`server.py:771` 传 `user.enterprise_email`、`proxy_auth.py:254` 有域白名单）把住。今天这两道门都在，但这是"靠调用方自觉"的安全，多一个个人邮箱身份行就多一分风险。何况现状 #9 说明 `people.json` 里今天就可能混进私人地址的人员行——不要往这个方向加压。
3. **代价可接受且有界。** 外部人员是少数（R2 会给出确切数字），一年新增个位数，IT 本来就在手工建他们的 IAM 账号，顺手配一个属性不是新增负担；而属性表这套增量/基线/mass-remove 阈值机制是为**全员规模**设计的。
4. **零 CSV 表头变更**，历史基线全部继续可用（见 G5）。
5. 外部人员本来就不登录本面板（第 1 节），不进 `people[]` 不会让谁"看不到自己的权限"。

O1 的代价，要明写进文档：外部人员的 cloud_accounts 属性**不在本系统的对账闭环里**——他们离场时，属性表不会自动生成 remove 行。**缓解**：`external` 记录的 `expires` 到期后会重新变成"待确认"并进告警（T2 + T6），管理员据此去催 IT 清理。这是"用告警补对账"，比默认静默好，但不等价，auditor 要知道这个缺口是**明知故留**。

### 8.3 Phase 2 的目标态：**O3 —— 用 IT 给的本地 IAM 登录名做匹配键**

若将来要把外部人员纳入闭环，走这条：在 `external` 记录里多存一个 `iam_login`（IT 建号后回给我们的登录名），属性表新增一列或把 `match_by` 扩成第三种取值 `iam_login`，值仍是云用户名。好处：**不复用 email 键空间、不伪造 union_id**，`diff` 里按 `(iam_login, app)` 独立成一个匹配域，与员工那套互不干扰。代价：CSV 表头变更 + 一次 re-baseline（G5），且要 IT 同意按这个键导入（Q2）。

### 8.4 被否掉的方案

| 方案 | 否掉原因 |
|------|---------|
| **O2 按个人邮箱进属性表**（`match_by="email"`，值是个人邮箱） | ① 把"个人邮箱"提升成匹配键，恰好是 `people.py` 开头那条规范禁止的方向；② `finance` 这类共享号一址多人，`diff` 的邮箱认领会把两个人的值混成一条，而 `adopt_result` 还有一条"同一应用同一值属于两个人 → 整份拒绝"（`iam_export.py:382-386`）会让**整份属性表卡住**，一个外部人员能拖垮全员同步；③ IT 能否按个人邮箱在 IAM 里唯一定位本地用户，未经证实 |
| **O4 给外部人员造假 union_id**（如 `ext:<hash>`） | union_id 是飞书的全局标识，本系统所有"这是不是同一个人"的判定（`_apply_bindings`、`carry_identities`、`diff` 的 vanished 检查）都默认它来自飞书。伪造值一旦和真值同域，出问题时没人分得清是数据坏了还是人造的。**明确否决，别在任何分支里偷偷实现** |
| **O5 让外部人员也走飞书（发个外部协作账号）** | 不是技术方案是组织方案，且把外部人拉进公司通讯录会让他们变成第 2 节里的"员工"、进入全员名册与统计。若组织上真要这么做，本需求从根上就不存在——写进 Q1 让用户判断 |
| **O6 自动把"通讯录对不上"的都判成外部人员并放行** | 第 2 节那条硬原则。通讯录采集权限不足时会把全公司判成外部人员 |

---

## 9. 风险与 auditor 重点

**安全边界：谁能认领一个云账号。** 本需求所有改动都压在这条线上，auditor 按下面逐条验：

| # | 检查点 | 怎么验 |
|---|--------|-------|
| A1 | **没有任何代码路径能自动产出 external** | 全仓搜 `external` 的写入点，确认只有 `review.apply(op="external")` 一处；`propose` 只读 manual、不写 |
| A2 | **通讯录不可用时 fail-closed** | 构造 `colleagues` 为空/None，断言零 external、零 C2、且个人邮箱账号**不**走第二轮显示名对应 |
| A3 | **个人邮箱账号不再被自动对应给任何人** | 这是需求的核心诉求，也是最容易被第二轮 `no_email` 路径悄悄绕过的地方（`ssomap.py:236`）。断言 C 分支的账号永不出现在 `links` 里 |
| A4 | **external 与 links/services 三方互斥，且四个改写点都摘干净** | G2/G3 那两组；重点看 `_edit_manual` 的每个分支和 `add_link` |
| A5 | **属性表零变化**（本期） | T8 的回归断言：同一份 people.json，改动前后 `build_rows` 输出逐行相同 |
| A6 | **`resolve()` 的邮箱认领没被新数据污染** | 确认 external 没有进 `people[]`；顺带评估要不要给 `resolve()` 加一道"只接受企业域邮箱"的硬校验（今天靠 `server.py`/`proxy_auth.py` 两个调用方把门，属于纵深不足）——**建议作为一条独立的加固项记账**，不混进本需求 |
| A7 | **到期逻辑只有一处实现** | 搜 `expires` 的比较点，应只在 `propose`；`refresh._unlinked_keys` 只看 `kind`、不自己算日期 |
| A8 | **写盘守卫** | `external` 记录含真实姓名 + 个人邮箱 → `manual-links.json` 和 T8 的清单都必须过 `_require_identity_dir` + 0600（`cli.py:971`、`people.write_private_json`） |
| A9 | **日志与错误信息不泄漏个人邮箱** | `review.log` 是 JSON 行（`review.py:123`），会记 email；确认它落在 `identity/` 下且 0600（现状是 `paths.log` = 名册同目录，符合），并确认告警文案（`refresh.render()` → 飞书 webhook）**不带个人邮箱**——告警是发到群里的，这是新引入的泄漏面 |
| A10 | **`finance` 等共享号的归类没被意外改动** | 本期不动 `SERVICE_NAMES`；断言 `finance` 仍判 service |

**其他风险：**

- **R-1 行为回归（中高）**：C2 让一批现在能自动对应上的账号变成"需处理"。R2 会给出规模。若这批人不小，SSO 上线那天他们登不进控制台 → 必须先跑一轮通知 + 改邮箱，不能改完代码就直接上。
- **R-2 `refresh` 签名变更（中）**：T3 要调整 `refresh.run` 的调用顺序与 `collect_proposal` 签名，`tests/unit/test_delivery_refresh.py` 里有直接打桩的用例，改签名必崩一批测试——这是预期的，但要和"真回归"分清楚。
- **R-3 标记即静默（中）**：标 external = 该账号退出告警视野。`expires` 必填 + 到期回落是唯一的兜底，别为了"用起来方便"把它改成选填。
- **R-4 前端表单校验不是安全边界（低但常犯）**：`sponsor`/`expires` 的校验必须在 `review.py`/`people.py` 服务端做，前端只是体验。

---

## 10. 需要问用户的开放问题（dev 转达，planner 不替用户拍板）

| # | 问题 | 为什么必须先答 | 我的倾向 |
|---|------|--------------|---------|
| **Q1** | `finance` 这个**多名外部审计共用**的账号，在用户 SSO 打开后怎么登？公司 IAM 会为它建**一个共用的本地账号**（多人共享一套凭证），还是给**每个外部审计人各建一个 IAM 账号**、云上也各建一个 RAM 用户？ | 这是头号真实用例，答案决定 `external` 是**账号级标记**（一个账号一条记录）还是**人级记录**（一个账号多个使用人）。前者是本文的设计，后者要改数据结构 | 各建各的号（共享凭证在审计场景本身就是问题）。但若组织上就是要共用，`external` 的 `name` 要允许写多人并显式标 `shared: true` |
| **Q2** | 外部人员的 cloud_accounts 属性，**要不要**由本面板产出？如果要，IT 那边能按什么键在公司 IAM 里定位这个本地用户——个人邮箱？还是 IT 分配的登录名？ | 决定 8.2（O1，本期）还是 8.3（O3，Phase 2）；O3 要改 CSV 表头并 re-baseline，是个有成本的单向决定 | 本期 O1（清单给 IT 手工配），等 IT 回答后再定要不要 O3 |
| **Q3** | **在职员工的账号只登记了个人邮箱**时，要的是"拒绝自动对应、逼他改企业邮箱"（用户原话的直译），还是"仍然对应上但在面板标红催改"？ | 前者会让这些人在 SSO 上线那天登不进控制台。规模见 R2 | 拒绝自动对应（符合原话），但**保留管理员人工 `assign` 的逃生口**，并在面板上给出明确的"请改用企业邮箱"指引 |
| **Q4** | `external` 标记要不要**强制到期日**？默认多久（90 天 / 180 天 / 跟审计周期）？谁有权标——所有管理员，还是要更高一级？ | 决定 T1 的校验强度与 T4 的权限判定 | 强制到期、默认 180 天、沿用现有管理员角色（`server.py:589` 的 `role_of`），不新增角色 |
| **Q5** | 定时刷新（`delivery refresh`）能不能改成走 `--directory feishu`？现在默认是 `none`（`cli.py:308`） | 决定三分支是靠真通讯录还是靠"上一份名册"降级源。降级源会漏掉刚入职的同事 | 改成 feishu（需要应用的通讯录权限范围覆盖全员，见 `directory.py` 的报错提示）；在那之前降级源够用 |
| **Q6** | 三分支要不要也改 `delivery identity`（`audit.classify`）这条 CLI 报告线，还是只改 sso-map/名册那条？ | 决定 T7 的范围 | 要改（口径不一致会让人拿两份报告对不上），但它只是报告、无授权后果，优先级可以排在 T1–T6 之后 |

---

## 11. 建议的推进顺序

1. **先并行发 R1 + R2**（researcher），同时把 Q1–Q6 交给用户。**不要在 Q1/Q3 有答案前开 T2**——它们直接改判定规则。
2. Q 有答案后：`T1 → T2 → T3/T4 并行 → T5 → T6`，`T7` 全程可并行插空。
3. 每个 T 完成即走标准流水线（tester 补测 + auditor 过闸），**别攒到最后一起审**——G1–G4 这几组的坏中间态正是攒批提交时最容易漏的。
4. `T8` 在 T1 之后随时可做（它只读）。`T9` 最后。
5. `T10`/Phase 2 等 Q2 + R3。

---

计划文件：`/home/l/桌面/infra/docs/collab/planning/external-person-email-classification.md`
