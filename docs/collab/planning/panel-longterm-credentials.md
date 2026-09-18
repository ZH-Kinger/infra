# 交付面板：长期凭证（>12h 自动转 RAM 子账号 + 长期 AK）实施计划

planner 起草，2026-09-16。只读了源码，没有改任何代码。
涉及仓库：`/home/l/桌面/infra`（面板）。参考实现在 `/home/l/桌面/langchaindev/core/temp_ak_issuance/` 与 `core/ram_approval.py`（**不同仓库，不能 import，只能照抄逻辑**）。

> 修订记录
> - 2026-09-16 r2：用户定下交付方式——**凭证 secret 走飞书审批评论，面板不回写、不落库**。第 2/3/4/5 节按此重写；连带取消"申请人到面板领取"这一环，状态机因此变简单（见 §4.1）。
> - 2026-09-16 r3：评论身份定为管理员（王梓涵）`ou_d3f646711beed260666c4c4c15c56ea7`，配置项 `DELIVERY_APPROVAL_COMMENT_OPEN_ID`；补入"评论无法以应用名义发"这一硬约束、正文格式硬要求、以及 open_id 跨应用的启动自检（§4.2/§4.3）。

## 0. 复核结论（只记与需求描述不同或需要补充的事实）

- `catalog.py:42 MAX_CREDENTIAL_HOURS = 12`，`parse_template` 的 credential 分支强制 `1 ≤ max_hours ≤ 12`、`role_arn` 必填且账号必须匹配、`groups` 必须为空。
- `flows.py:681-698`：审批通过的凭证单转 `CLAIMABLE` 并写 `valid_until_ts`；`flows.py:782` 的 `execute()` 开头直接 `raise FlowError("访问凭证申请不需要开通，审批通过后直接领取")`；`flows.py:1318 claim_credential` 才真正 `assume_role`。
- `tickets.py:58-73 TRANSITIONS`：已有 `APPROVED → EXECUTING`、`EXECUTING → DONE|FAILED`、`FAILED → EXECUTING`、`DONE → REVOKED`。改成"审批通过即发放"之后，credential 走的全是这些现成转换，**`TRANSITIONS` 不需要改**。
- `flows.py:1009 revoke_expired` 只扫 `kind == KIND_PERMISSION` 且 `status == DONE`；`remind_expiring`（`flows.py:955`）同样只认 permission。
- `provision.py:446 executor_from_env` 只按平台分派，两个执行器都只读 `DELIVERY_EXEC_<平台>_<账号>_*`（`exec_env_prefix`）。仓库里 **`DELIVERY_ISSUER_*` 目前零引用**——服务器上那把 issuer AK 现在没有任何代码在读。
- `provision.py:76 executor_configured()` 已存在，`flows.options()`（`flows.py:213`）用它把未配执行身份的模板置灰。长期凭证要照抄这个模式做 `issuer_configured()`。
- `provision.py:216 AliyunExecutor.assume_role` 只传 `RoleArn/RoleSessionName/DurationSeconds`，**没有 `Policy` 参数**，短期凭证的实际权限完全等于角色本身。
- `approval.py:204 FeishuApproval` 现有方法只有 `create/fetch/cancel/status/verify_approved/widgets`，**没有发评论的能力**，也没有"以某人身份发评论"的配置项。`_http`（`approval.py:168`）不支持 query 参数拼装（评论接口要 `?user_id_type=&user_id=`）。
- **飞书审批评论接口无法以应用/机器人名义发**（已查官方文档）：`POST /open-apis/approval/v4/instances/{instance_id}/comments` 的 query 参数 `user_id` 是**必填**。评论必然挂在某个自然人名下——这是接口的设计约束，不是我们的选择。由此引出 §4.2 的正文来源标注硬要求。
- `clouds/volcano.py` 刚修了写操作的成功判定（成功不返回 `Result` 被误判为失败）。本计划 v1 只做阿里云，但这条修复是火山侧做 issuer 的前提，记在 Q6。
- `tickets.json` 是文件存储、**没有 TTL、没有归档功能**。这是面板天然优于 bot 的地方，但不是自动安全的，见 §1.3。
- 命名冲突（新发现，必须处理）：bot 的方案 B 在**同一个主账号**下已经用 `tempak-*` 建号、`temp-ak-auto-*` 建策略，而 `panel-issuer` 授权的资源也是这两个前缀，**两套系统共用一个命名空间**。见 §6 与 CRED-2。

---

## 1. 模板与数据模型

### 1.1 `catalog.py` 的 credential 模板字段

保留 `role_arn` / `max_hours`，新增四个字段（写进 `_KNOWN`、`Template`、`parse_template` 的 `KIND_CREDENTIAL` 分支、`Template.public()`）：

| 字段 | 含义 | 校验 |
|---|---|---|
| `max_days` | 申请人可选的最长有效期（天）。**0 = 这个模板不开放长期**，行为与今天一致 | `_int(..., 0, 0, LONGTERM_MAX_DAYS)`，建议 `LONGTERM_MAX_DAYS = 60` |
| `bucket` | 长期路径生成 policy 的目标 OSS 桶 | `max_days > 0` 时必填；桶名正则 `^[a-z0-9][a-z0-9-]{1,62}$` |
| `prefix` | 目录前缀，写进 policy 的 `oss:Prefix` 与对象 ARN | `max_days > 0` 时必填（见 Q3）；禁 `..`、禁前导 `/`、必须以 `/` 结尾 |
| `caps` | `["read","download","write"]` 的非空子集 | `max_days > 0` 时必填；去重、顺序归一 |
| `region` | 桶所在地域（裸写法，如 `cn-shenzhen`），用于评论正文的连接信息三行 | `max_days > 0` 时必填；见 §4.2 与 Q9 |

三条硬校验：

1. `max_days > 0` 且缺 `bucket`/`caps`/`region` → `CatalogError`。缺了就没有 policy 或没有可用的连接信息，会拖到审批通过后才炸，白等一轮。
2. `max_days > 0` 时 `max_hours` 必须省略或等于 12（省略则置 12）。否则出现语义空洞：`max_hours=4, max_days=30` 时申请 8 小时算合法（≤ max_days×24）却又超过 `max_hours`，两个上限打架。
3. `valid_days` **对 credential 失去意义**（没有领取窗口了）。字段保留解析以免老模板报错，`Template.public()` 里对 credential 不再输出，前端也不再显示"批准后 N 天内可领"。

`Template.public()` 新增 `bucket/prefix/caps/max_days`（申请页要显示"这份凭证能动哪个目录"），`role_arn` 仍不输出。

**为什么不是"申请人多选权限"**：三个 STS 角色 `panel-oss-list / panel-oss-download / panel-oss-write` 各只覆盖一种能力，session policy 只能收窄不能扩权，"下载+上传"这种组合在短期路径上没有角色能承载。v1 保持"模板即权限"（一个模板一个 cap 集，对应一个角色），`caps` 只是把这份权限**再写一份机器可读的描述**给长期路径用。真正的多选 UI 需要云上先建一个并集角色，见 Q1。副作用是 `caps` 与 `role_arn` 的实际权限一致性只能靠管理员保证，代码校验不了（CRED-12 审计重点）。

### 1.2 工单上存什么

payload 不变（仍是 `{"hours": int}`，单一单位，避免 days/hours 两套单位在校验和分流之间漂移）。新增工单**顶层** `grant` 字段，短期长期都写：

```
"grant": {
  "mode": "ram" | "sts",
  "platform": "aliyun", "account": "1704065796538912",
  "user": "tempak-panel-lisi-9f3a21",           # sts 模式为空
  "policy": "temp-ak-auto-tempak-panel-9f3a21",  # sts 模式为空
  "ak_id": "LTAI...",                            # 只存 ID。ID 不是密钥，撤销和排障都要它
  "bucket": "...", "prefix": "...", "caps": [...], "region": "cn-shenzhen",
  "not_before_ts": 0, "expire_ts": 0,
  "user_created": false, "policy_put": false, "ak_created": false,
  "comment_id": "",                              # 交付评论的 id，重试时覆盖同一条
  "delivered_ts": 0,
  "revoked_at_ts": 0, "revoke_progress": []
}
```

复用现有字段：`expires_at` / `expires_at_ts`（驱动 `remind_expiring` 与 `revoke_expired`）、`done_at_ts`、`result`。

四条不变量：

- **secret / SecurityToken 绝不写进工单、事件 note、日志、飞书私信通知、HTTP 响应**。全仓唯一出口是审批评论正文。`grant` 里只有 `ak_id`。
- `grant` 在**调用 CreateUser 之前**就落盘（intent-first，照 `flows._run` 记 `preexisting_groups` 的做法）。中途崩溃留下 `user_created=false` 的记录，比"云上有号、本地啥也没有"好排查。
- 三个步骤标记支持重试续做，但 **`ak_created=true` 且已交付之后禁止重试建 AK**：secret 只在 CreateAccessKey 返回那一次存在，再建一把就多一把没人知道 secret 的幽灵 AK（RAM 每用户上限 2 把）。
- `sts` 模式的 grant 建完即写 `revoked_at_ts = issued_ts`（没有要清的东西），这样回收扫描天然跳过它，不需要在扫描条件里另写 if。

### 1.3 怎么保证不重蹈"记录比凭证先过期"

bot 的坑是 Redis `temp_ak:grant:*` 30 天 TTL 对上最长 2 个月的凭证，记录先没了 → `sweep_expired` 扫不到 → 云上永久残留。面板用四道防线，任何一道单独失效都不会导致永久残留：

1. **存储层无过期**：`tickets.json` 没有 TTL、没有归档。把这条写成硬规：将来任何归档/裁剪功能必须跳过 `has_live_grant(ticket)`（新增到 `flows.py` 的模块级函数：`grant` 存在且 `revoked_at_ts == 0`）。现在没有归档功能，所以只需加函数 + 文档 + 一条测试锁。
2. **回收判据不看工单状态**：`revoke_expired` 的 credential 分支遍历"有 `grant` 且 `mode=="ram"` 且 `revoked_at_ts == 0` 且 `expire_ts <= now`"的单子，**与 `status` 无关**。status 只决定要不要顺带翻 `REVOKED`（`DONE` 才翻；`FAILED`/`CLOSED` 照样清云上、只更新 `grant` 和事件）。bot 那条"只扫 `stage == ISSUED`"在面板里对应的坑是"只扫 `status == DONE`"——发放中途崩溃被 `recover_stuck` 标成 `FAILED` 的单子，云上号已经建出来了，按 status 扫就永远清不掉。
3. **云上对账**（面板相对 bot 多出来的一层，也是唯一能发现"连记录都没写成"的机制）：新增 `flows.reconcile_credentials()`，`ListUsers` 取 `tempak-panel-` 前缀的用户，与 tickets.json 里所有 `grant.user` 双向比对：云上有单子没有（孤儿）、单子有云上没有（已被人工删，标记 grant）。**只报不删**（同前缀下还有 bot 的号，见 §6）。
4. **policy 自带时间窗**：`DateLessThan` 让服务端在到期后直接拒绝调用。清理失败只意味着残留 artifact，不意味着权限还在。这句要写进 runbook，否则运维看到"回收失败"会按"权限泄漏"处理。

---

## 2. 自动分流的判据放在哪一层

**判在发放那一刻，而发放那一刻 = 飞书审批通过被 `sync` 观察到的那一刻。判据是申请单里的时长（duration），不是日历窗口。**

- 申请人填的是"用多久"（`payload.hours`），不是"用到几号"。duration 不随审批拖延而变化，所以"审批过几天才通过"这个问题在数据模型层就消失了。
- 改成评论交付后，领取环节没有了（§4.1），发放时刻就钉死在审批通过那一刻，连"领取窗口"这个变量也一并消失。`expire_ts = 发放时刻 + hours*3600`，`not_before_ts = 发放时刻`。
- 提交时（`flows._validate` credential 分支）只做**边界校验**：`1 ≤ hours ≤ (max_days*24 if max_days else max_hours)`。同时把预判模式写进 `summary`（"超过 12 小时，系统会单独建一个子账号，到期自动删除"）——审批人看的就是这段，纯展示。
- 发放时（新增 `flows._issue_credential`，由 `execute()` 调用）判：

```
STS_MAX_SECONDS = 43200   # 阿里 AssumeRole 硬顶，写死常量，不做成可配
def classify_mode(hours): return "sts" if hours*3600 <= STS_MAX_SECONDS else "ram"
```

- 模板核对从 `_CLAIM_FIELDS` 改成走 `_EXEC_FIELDS`（credential 现在也走 `execute()`）。`_EXEC_FIELDS` 要补 `bucket`、`prefix`、`caps`、`region`、`max_days`。
  **语义变化要写进文档**：`_EXEC_FIELDS` 是严格相等比对，所以管理员在审批期间调低 `max_hours`/`max_days` 会让单子 409「模板被修改，请重新提交」，而不是像今天 `claim_credential`（`flows.py:1324`）那样静默截断。这是 fail-closed 方向，且与 permission/account 的行为一致，建议接受。

---

## 3. 改动清单（按文件，标依赖与"必须同批"）

### A 组：纯逻辑，可独立先落（无中间态风险）

1. **新增 `src/delivery/credpolicy.py`** — 照抄 bot `core/temp_ak_issuance/policy.py` 的逻辑，零依赖重写：
   - `iso8601_bj(epoch)`：`+08:00` ISO8601，RAM 的 `Date*` 条件要求带时区。
   - `build_policy(bucket, *, prefix, caps, not_before, expire, source_ips=None) -> dict`：三条能力正交（`read` 只给 `ListObjects`/`GetBucketMultipartUploads`、`download` 只给 `GetObject`、`write` 给 `PutObject/AbortMultipartUpload/ListParts` 且**无任何 delete**）；桶元信息 `GetBucketInfo/GetBucketStat/GetBucketAcl` **独立成一条、Resource=桶、绝不叠 `oss:Prefix`**（桶级请求不带 prefix，叠上去会被判假拒绝——bot 线上"拿了凭证访问不了桶"就是这个）；每条叠 `DateGreaterThan`/`DateLessThan` 时间窗。
   - `build_session_policy(...)`：同一份文档 + `len(json.dumps(...)) <= 2048` 硬校验。
   - `user_name_for(email_or_union, *, rand)` / `policy_name_for(user)`：`tempak-panel-<ascii-slug>-<6hex>` / `temp-ak-auto-<user>`。**面板零依赖，没有 pypinyin**，非 ASCII 一律退企业邮箱前缀，再不行退 `ext`。
   依赖：无。

### B 组：三个能力各自独立（2/3/4 可并行，都在 C 组之前）

2. **`src/delivery/provision.py`** — 新增 `AliyunIssuer`，**独立类，不继承也不复用 `AliyunExecutor`**：
   - `issuer_env_prefix(platform, account)` → `DELIVERY_ISSUER_ALIYUN_<UID>`；`issuer_from_env(...)`；`issuer_configured(platform, account, environ=None)`（只看环境变量在不在，与 `executor_configured` 同规格）。
   - 方法只有六个：`create_temp_user` / `put_policy`（CreatePolicy 或 CreatePolicyVersion+set_as_default）/ `attach_policy_to_temp_user` / `create_access_key` / `revoke_user`（停用+删 AK → detach → 删策略版本+策略 → 删 user）/ `list_temp_users`（对账，走 `clouds/aliyun.paginate` 的 `ListUsers`）。
   - **不实现** `reset_password` / `add_to_group` / 通用 `attach_policy` / `assume_role`。issuer 云上策略 Deny 控制台登录、Deny 挂系统策略，代码侧也别留调用点。
   - `_check_account()` 照抄 `AliyunExecutor._check_account`（`GetCallerIdentity` 比 `AccountId`）。
   - `issuer_from_env` 里加**硬门**：同账号的 `DELIVERY_ISSUER_..._ACCESS_KEY_ID == DELIVERY_EXEC_..._ACCESS_KEY_ID` → `ProvisionError` 拒绝构造。见 §6。
   - 同文件：`AliyunExecutor.assume_role(role_arn, name, hours, policy: Optional[dict] = None)`，`policy` 非空时以 `Policy=json.dumps(...)` 传给 AssumeRole；默认 `None` = 现状逐字不变。
3. **`src/delivery/approval.py`** — 新增评论能力（详见 §4.3）：
   - 评论身份配置项 `DELIVERY_APPROVAL_COMMENT_OPEN_ID`（环境变量优先），可在 `identity/approval.json` 里放同名字段作为回退。值已定：`ou_d3f646711beed260666c4c4c15c56ea7`（管理员 王梓涵，面板应用 `cli_aa2d10ccd0b9dbb7` 下的 open_id）。
   - `FeishuApproval.comment(instance_code, text, *, comment_id="") -> str`：`POST {API}/approval/v4/instances/{code}/comments?user_id_type=open_id&user_id=<open_id>`，body `{"content": json.dumps({"text": text}), "disable_bot": False}`，返回 `comment_id`。`_http` 需要支持带 query 的 URL（现在是固定前缀拼接）。
   - `comment_ready() -> bool`：配了 open_id 且有 token；给 `flows.options()` 与 `health.py` 用。
   - `verify_comment_identity() -> str`：用面板应用的 token 反查这个 open_id 是否可解析（见 §4.3 的启动自检），返回空串表示正常、非空为人话错误。
   - **错误必须能区分**：`99992361 open_id cross app` 要给出人话（"这个 open_id 不属于面板的飞书应用 `cli_aa2d10ccd0b9dbb7`"），否则排查要半天。
4. **`src/delivery/catalog.py`** — §1.1 的五个字段 + 三条硬校验 + `public()`。

### C 组：流程主体（必须同批上线，拆开中间态会坏）

5. **`src/delivery/flows.py`**：
   - `Flows.__init__` 新增 `issuer`、`issuer_ready`（与 `executor`/`executor_ready` 并列、完全独立）。
   - `options()`：credential 模板在 `issuer_ready` 为假（仅 `max_days>0` 的模板）或评论身份不可用（所有 credential 模板）时置灰，照 `flows.py:213` 的写法。理由同那段注释：别让人白提一轮。
   - `_validate` credential 分支：上限按 §2；`summary` 写明预计路径、到期行为、以及"凭证会发到本审批的评论里"。
   - `execute()`：删掉开头那句 `raise FlowError("访问凭证申请不需要开通…")`，`_run()` 增加 credential 分支 → `_issue_credential(tpl, ticket)`。
   - `_issue_credential`：前置检查（评论身份 + `instance_code`，缺任一 **fail-closed 不发放**）→ `classify_mode` → `_issue_sts` / `_issue_longterm` → 立即贴评论 → 成功才写 `grant.ak_id/comment_id/delivered_ts`、`expires_at_ts`。失败回滚见 §4.4。
   - `sync()`（681-698）：credential 分支保留 `_verify_approval` + `SelfApprovalError → _close_self_approved`（这条比 execute 的通用失败路径更准确），之后改成 `return self.execute(ticket_id, actor="system")`，不再转 `CLAIMABLE`、不再写 `valid_until`。
   - `resume_approved()`（892）：删掉 credential 特例，全部走 `execute()`。
   - `claim_credential()`：删除（遗留单处理见 §4.5）。`_CLAIM_FIELDS` 删除，`_EXEC_FIELDS` 补五个字段。
   - `_revoke_credential(ticket, now)`（新，不塞进 `_revoke`）：见 §5。
   - `revoke_expired()`：按 kind 分派，扫描条件按 §1.3 第 2 条改。
   - `remind_expiring()`：放行 credential，但只对 `grant.mode == "ram"`（12 小时的 STS 没必要提前 3 天提醒）。
   - `reconcile_credentials()`、`has_live_grant(ticket)`（新）。
6. **`src/delivery/server.py:614 flows()`** — 注入 `issuer=self._issuer`、`issuer_ready=provision_issuer_configured`；新增 `_issuer` 方法（与 `_executor` 并列、各自独立）。
7. **`src/delivery/requests_api.py`**：
   - `_EVENT_LABELS` 补 `credential_issued`（文案改"已发放并送达审批评论"）、`credential_delivered`、`credential_rolled_back`、`credential_revoked`、`credential_revoke_failed`；`_ERROR_NOTES` 补后两者。
   - `POST /api/requests/<id>/credential` 语义改为**重新签发**：响应里**不含任何凭证字段**（删掉 `requests_api.py:349-357` 那个 `credential` 块），只回 request view，凭证仍走评论。长期单在 `grant` 未回收时拒绝（409）。
   - `_view.actions.credential` 改为"可重新签发"：短期 `status == DONE 且 grant.mode == "sts"`；长期恒 False。
8. **`src/delivery/web/requests.js`**：
   - 申请抽屉 credential 分支（约 379-384）：`max_days > 0` 时时长选择器改预设（1/4/8/12 小时、1/3/7/14/30 天，按 `max_days` 截断）；预览文案改成"审批通过后凭证会直接发到这条飞书审批的评论里"，并说明 >12h 会建独立子账号、到期自动删除。删掉"批准后 N 天内可领"。
   - `credentialAction`（753）改成"重新签发（发到飞书审批评论）"，成功后提示去飞书看；`showCredential`（约 780-797）**整段删除**——面板不再显示任何 secret。
9. **`src/delivery/cli_requests.py`**：
   - `_creds`（343）：不再打印凭证，改为提示"凭证已发到飞书审批评论"，可选打印审批跳转链接（`flows.approval_links`）。`delivery creds` 的 `eval "$(...)"` 用法要从 `_detail`（334 行那句提示）和文档里去掉。
   - `_sweep`（371）：构造 `Flows` 时注入 issuer，`steps` 末尾追加 `flows.reconcile_credentials`。
10. **`src/delivery/notify.py`** — `_message` 的 `done` 分支加 credential 文案（"凭证已发放，请到飞书审批的评论里取，secret 只显示一次"）；`expiring` / `revoked` 文案对凭证改写（现在写的是"权限已按申请时的期限收回"）。`claimable` 事件对 credential 不再触发，`EVENTS` 保留该项不动。

### D 组：可观测与部署（C 之后单独落）

11. **`src/delivery/health.py`** — "执行身份"那段（259-281）之后加一组：
    - 有 `max_days>0` 模板的云账号缺 `DELIVERY_ISSUER_*` → CRIT；与 `DELIVERY_EXEC_*` 的 AK ID 相同 → CRIT（文案直接写"两把 AK 必须分开持有"）。
    - `DELIVERY_APPROVAL_COMMENT_OPEN_ID` 未配置 → CRIT（"凭证无法交付，所有凭证申请都会失败"）。
    - **评论身份可解析性自检**（§4.3）：用面板应用 token 反查该 open_id，失败（尤其 `open_id cross app`）→ CRIT，文案直接说"这个 open_id 不属于面板应用，请用面板应用重新换取"。这一项**必须在系统状态页显示**，不能等到发凭证那一刻才失败。注意 `health.py` 现在的设计是"不调外部接口"（见 `docs/cloud-access-platform.md:124`），所以这一项要么做成**带缓存的可选检查**（结果缓存到 `identity/` 下，刷新由 sweep 触发），要么单列一个 `delivery doctor` 子命令并在状态页显示最近一次结果。选型交 dev，但"配错了能在页面上看见"是硬要求。
    - 待回收凭证（`grant` 未回收且过期超 1 小时）计数 > 0 → WARN（说明 sweep 没跑或一直失败）。
12. **`deploy/panel/issuer-policy.aliyun.example.json`**（新）+ `deploy/panel/README.md`（凭证变量表加 ISSUER 一行、写明 SourceIp 限制的后果、评论 open_id 怎么取）+ `.env.example` + `docs/cloud-access-platform.md`（R4 措辞、第 3 节申请类型表、第 4 节状态图、第 7 节现状表）。docwriter 主责。

### 必须同批（拆开会坏）

- **4 + 5 + 8**：模板加了 `caps/bucket` 而 flows 不校验 → 长期路径拿不到 scope；flows 改了上限而前端还在渲染 `1..max_hours` 的 select → 申请人选不到 >12h。
- **3 + 5 + 7 + 8**：交付通道从"页面返回"切到"审批评论"是一次原子切换。只改后端不改前端 → 前端拿不到 `credential` 字段会报错；只改前端不改后端 → 凭证发不出去。
- **5(发放) + 5(回收) + 9(sweep 接线)**：**绝不能先上发放、后上回收**。中间窗口里发出去的每一份长期凭证都是永久的云上残留，且当时没人会发现。这是本次唯一一条"顺序错了就出事故"的依赖。
- 2 可以先单独上（纯新增类 + 一个默认 None 的参数），把 issuer 凭证的联调风险提前。

---

## 4. 凭证交付：飞书审批评论

### 4.1 取消"领取"环节，审批通过即发放（建议采纳）

交付通道已经不是面板页面了，"领取"这一步就只剩"让申请人点一下再触发"这一个作用。建议**取消**，审批通过 → `sync` → `execute()` → 发放 + 贴评论 → 工单直接 `DONE`。

取消的理由：

1. **申请人根本不会回面板**。凭证在飞书审批评论里，他收到的通知也是"去飞书看"。再要求他先回面板点一次"领取"，最常见的失败模式就是"申请人一直不点，以为凭证还没发"，而面板这边显示"可领取"、谁都不觉得有问题。
2. **和另外两类申请对齐**。permission / account 就是"审批通过 → execute() → DONE"。credential 并进来之后，`recover_stuck`（卡在 EXECUTING 30 分钟）、`resume_approved`（审批通过后进程退出）、管理员重试（`FAILED → EXECUTING`）、`_verify(_EXEC_FIELDS)` 这四套已经写好并测过的容错逻辑全部自动覆盖凭证发放。保留领取环节的话，这四套要在 CLAIMABLE 分支上再实现一遍。
3. **状态机不用动**。走 `APPROVED → EXECUTING → DONE → REVOKED` 全是现成转换；`CLAIMABLE`/`valid_until`/`_maybe_expire`/`EXPIRED` 这一整套对 credential 不再需要。

保留领取的唯一理由是"批了没人用，白建一个号"。代价评估：这个号有时间窗 policy，到期硬删，白建的成本是一个会自己消失的对象；而它换来的是上面三条。**不值得为它保留一个状态分支。**

副作用（要写进文档和 summary）：申请人在提交时就要知道"审批通过后凭证会自动发到审批评论里"，否则他会等一个不会出现的"领取"按钮。前端预览文案 + `done` 通知各写一次。

### 4.2 评论正文（格式是硬要求）

单条评论，纯文本（`content = json.dumps({"text": ...})`）。因为飞书不允许以应用名义发评论（§0），这条评论会挂在管理员本人名下，混在人工评论里。**正文必须自带来源标注**，否则以后翻审批记录分不清哪条是人手贴的、哪条是系统发的——这是硬要求，不是文案偏好。

正文顺序固定，五段：

```
[云账号平台自动发放] 申请单 REQ-20260916-XXXXXXXX
本条由云账号交付面板自动发出，非人工填写。

云账号      阿里云 1704065796538912
可访问      oss://<bucket>/<prefix>（列清单、下载）
有效期      2026-09-16 18:00:00 → 2026-10-16 18:00:00（+08:00）
凭证类型    长期 AccessKey（权限内嵌生效/到期时间，到期后调用被拒并自动清理）
            / STS 临时凭证（含 SecurityToken，到点自动失效）

AccessKey ID      LTAI...
AccessKey Secret  <只此一处>
SecurityToken     <只此一处，仅短期凭证有>

地域            cn-shenzhen
外网 Endpoint   oss-cn-shenzhen.aliyuncs.com
桶域名          <bucket>.oss-cn-shenzhen.aliyuncs.com

Secret 只在本条评论中出现一次，请立即保存到密码管理器。
到期后凭证自动失效、子账号会被删除；需要继续用请重新申请。
```

规则：

- **第一行必须是来源标注 + 申请单号**，第二行明说"自动发出、非人工填写"。
- **secret 在正文里只出现一次**，不做"摘要里再写一遍"之类的重复（评论对所有能看到实例的人可见，见 §4.6）。
- **连接信息三行必须给**（地域 / 外网 Endpoint / 桶域名），照抄 bot `core/temp_ak_issuance/delivery.py:107 _access_lines` 的形态。bot 在这里踩过两个坑，面板都要避开：① 深圳的桶用杭州 endpoint 会被 OSS 回 403 `must be addressed using the specified endpoint`，使用方会以为凭证无效；② 仓库里存在两套地域写法（带 `oss-` 前缀 vs 裸 region），不归一会拼出 `oss-oss-ap-southeast-1.aliyuncs.com` 这种解析不了的域名。面板的解法是模板里直接存**裸 region**（§1.1 的 `region` 字段）并在拼装处做一次 `startswith("oss-")` 归一；region 缺失时**不猜**，三行退化成一行"未知，请按控制台上该桶的外网 Endpoint 连接"。
- 不带任何换行敏感的用户输入。申请理由之类若要进正文，先把 `\s+` 压成单空格——bot 踩过"备注里塞一行伪造的 `AccessKey Secret：`"这个洞。
- 正文生成放独立函数 `flows._credential_comment_text(ticket, grant, creds)`（或单独 `src/delivery/credtext.py`），**纯函数、可单测**，这样"secret 只出现一次""第一行是来源标注"能被测试锁死。

### 4.3 评论身份

- **值已定**：`ou_d3f646711beed260666c4c4c15c56ea7`（管理员 王梓涵）。配置项 `DELIVERY_APPROVAL_COMMENT_OPEN_ID`（环境变量优先，`identity/approval.json` 同名字段作回退）。
- **必然挂在自然人名下**：`POST /open-apis/approval/v4/instances/{instance_id}/comments` 的 query 参数 `user_id` 必填，接口没有"以机器人/应用身份发"的选项（已查官方文档）。所以"评论看起来是王梓涵发的"这件事改不了，只能靠 §4.2 的来源标注区分。
- **必须是面板应用 `cli_aa2d10ccd0b9dbb7` 下的 open_id**。open_id 是「应用 × 用户」维度的标识，跨应用不通用——今天已实测：拿 bot 应用下的 open_id 去面板应用查，飞书回 `open_id cross app`（99992361）。**不要直接复制 bot 的 `ADMIN_FEISHU_OPEN_ID`。**
- **前置检查（启动自检）**：面板启动 / 系统状态页要能显示"这个 open_id 在面板应用下解析得到"。配错的后果是所有凭证申请在审批通过那一刻失败，而那时申请人已经等了一轮审批——必须提前暴露，不能等到发凭证那一刻。实现见 §3 第 11 项（`health.py` 不调外部接口的约定要一并处理）。
- 只从环境变量 / `approval.json` 读，**不接受请求参数传入**（防止被构造成以别人身份发评论）。日志里可以打 open_id（不是密钥），不打 token。
- **fail-closed**：评论身份没配 / 自检没过 / `instance_code` 为空 → **不发放**（不建号、不 AssumeRole），单子转 `FAILED` 并说明原因。对应 bot 的 `_assert_account_delivery_ready`。绝不能先建号后发现评论发不出去——那样 secret 永久丢失、云上还留一个号。

### 4.4 交付失败怎么办（关键路径）

发放顺序固定，**建 AK 是最后一步，建完立刻发评论**：

1. 前置：评论身份 + `instance_code` + `verify_approved` 都过。
2. 建 user → 建/挂 policy（这两步可重试、不产生 secret）。
3. `CreateAccessKey`，secret 只在内存。
4. **立刻 `approval.comment(...)`**。
5. 成功后才写 `grant.ak_id / comment_id / delivered_ts` + 转 `DONE`。

第 4 步失败的处置（必须实现，不能只写在注释里）：

- **长期（ram）**：secret 已不可取回 → **立即回滚**：删掉刚建的 AK（`revoke_user` 的第一段就够，也可以整个 user 一起删），工单转 `FAILED`，事件 `credential_rolled_back`（note 写"凭证已作废，可重试"）。重试会重新走一遍、建新 AK。
- **短期（sts）**：token 自灭，不需要回滚。工单转 `FAILED`，重试重签。
- 回滚本身也失败 → 工单 `FAILED` + `grant` 保持未回收 → 由 §5 的回收路径和 §1.3 的对账兜住（`expire_ts` 已写，到点会被清）。

重试时带上 `grant.comment_id` 覆盖同一条评论（bot 的 `_send_approval_comment(comment_id=...)` 就是这个用法），避免历史评论里留着已作废的 secret。`comment_id` 的确切语义（覆盖 vs 回复）由 CRED-1 真机确认——如果是"回复"而不是"覆盖"，那就改成在旧评论下补一条"上一条凭证已作废"，并在计划里记一笔。

### 4.5 遗留的 CLAIMABLE 单子

上线前先查 `identity/tickets.json` 里有没有 `kind == "credential"` 且 `status == "claimable"` 的在途单：有就先让管理员 `close` 或等它 `EXPIRED`，然后**一次性删掉 `claim_credential` 整条路径**。养一条只服务几张老单子的遗留分支不划算。这条进发布检查单（CRED-10 验收项）。

### 4.6 风险：评论对所有能看到该审批实例的人可见

审批实例的评论，**审批人、抄送人、以及任何有权查看该实例的人都能看到**，不只是申请人。bot 那边接受了这个折中（凭证本来就由管理员发放、审批人知情），面板沿用。三条约束：

- 评论正文里 secret **只出现一次**，不重复、不摘要。
- 上线前确认这条审批流的**抄送范围**：不要配全组/全公司抄送。这是飞书审批后台的配置，代码管不了（Q8）。
- 到期回收后**不删除**那条评论（审计需要留痕），凭证届时已失效；正文里已写明"到期自动失效"。

### 4.7 短期 STS 是否一并改成评论交付：建议改

**建议一并改。** 决定性理由不是洁癖，而是需求本身：短期还是长期**由系统自动判、对申请人透明**。如果短期在面板页面领、长期在审批评论看，申请人就**必须先知道自己会拿到哪一种**才知道去哪儿取——分流对他就不透明了，那条需求等于没实现。

改动量其实是减法：删 `claim_credential`、删 `CLAIMABLE` 分支与 `valid_until`、删前端 `showCredential`、删 CLI `_creds` 的凭证输出。新增的只有评论调用（长期路径本来也要写）。

代价（必须写进文档，也是唯一的真实退化）：短期凭证的 12 小时从"申请人点领取时起算"变成"审批通过时起算"。审批通过时人不在场的话，会浪费掉一部分有效期。两个缓解手段：

- 申请时长按"批准后需要用多久"填（提交页文案改一句即可）；
- 保留**重新签发**入口（`POST /api/requests/<id>/credential` 的新语义，§3 第 7 项）：短期单在 `DONE` 之后仍可重新签发一份，结果同样贴到审批评论，不回面板。长期单没有这个按钮（会多建一个号）。

如果用户不接受这个退化，替代方案是"短期留页面领取、长期走评论"，但要明确接受透明度损失。见 Q5。

---

## 5. 到期回收

**单独一条路径 `_revoke_credential`，不复用 `_revoke`。** `_revoke` 的复杂度全在"哪些组/策略不能收"（`preexisting_groups`、同一子账号的其它未到期单子、`_same_grantee`），而长期凭证的号是这张单子**独占新建**的一次性身份，没有任何"原有权限"要保留。把删号塞进去会让两套保留判断互相污染，且删号的部分失败语义与 detach 完全不同。共用的是**外壳**：`revoke_expired` 的扫描 + per-ticket try + 事件去重 + `_emit("revoked", ...)`。

删除顺序（照 bot `cleanup.revoke_grant`，按 RAM 的依赖前置排的）：

1. `ListAccessKeys(user)` → 每把 `UpdateAccessKey(status="Inactive")` → `DeleteAccessKey`。**先停用再删**：删失败时至少已经失效。
2. `DetachPolicyFromUser`。
3. `ListPolicyVersions` → 删非默认版本 → `DeletePolicy`。（bot 踩过的坑：`DeletePolicyVersion` 只接受 `policy_name + version_id`，多传 `policy_type` 会构造即抛。）
4. `DeleteUser`。

失败处理：

- **部分失败不翻 `REVOKED`**。保持原状态，记一次 `credential_revoke_failed`（同一错误只记一次，照 `flows.py:1063-1068` 的去重写法），已完成的步骤追加进 `grant.revoke_progress`，下一轮 sweep 从断点接着做。只有"user 已确认不存在"才算完成，此时写 `grant.revoked_at_ts`，并在 `status == DONE` 时翻 `REVOKED`。
- 每张单子独立 try（`revoke_expired` 已经是这个结构，`flows.py:1026-1029`）。bot 的教训是"try 按账号包住整个循环，一条脏记录中断整批"——改的时候别把 credential 分支写成一个大循环包一个 try。
- `AliyunDenied` 单独标注：`panel-issuer` 策略限 SourceIp `120.79.167.166`，在别的机器上跑 sweep 会整批鉴权失败。错误文案直接提这一点，否则运维会去翻策略。

两个附带入口：

- 管理员 `close` 一张仍有未回收 `grant` 的单子（`flows.close`，`expect=[FAILED, CLAIMABLE]`——新流程里 credential 的失败态是 `FAILED`，仍在 expect 里）→ 不阻止关闭，但 `revoke_expired` 不看 status，到期照样清。加测试锁住。
- 申请人"作废我的凭证"：直接走 `_revoke_credential`（收权限不需要审批），作废后该单可重新签发或重新申请。做进 CRED-9。

sweep 接线：`cli_requests._sweep` 的 `steps` 里 `flows.revoke_expired` 已经在了，只要内部分派即可；新增的是末尾的 `flows.reconcile_credentials`。10 分钟的频率对回收足够——第一道闸是 policy 时间窗，不是这个定时器。

---

## 6. 安全边界：issuer / executor / 评论身份

`panel-issuer` 能建号、造策略、挂策略、删号；`panel-executor` 能加组、挂系统策略、AssumeRole。任一方拿到另一方的能力就接近管理员。代码上的四层保证：

1. **env 前缀分离**：`exec_env_prefix` → `DELIVERY_EXEC_*`，新增 `issuer_env_prefix` → `DELIVERY_ISSUER_*`。`executor_from_env` 与 `issuer_from_env` 各自只读自己那组，**互不回退**。今天 `executor_from_env`（`provision.py:446`）只按平台分派，不会误读 ISSUER；要确保新增代码不给它加"找不到就退到 issuer"这类便利路径。
2. **类型隔离**：`AliyunIssuer` 不继承 `AliyunExecutor`，不实现 `assume_role/add_to_group/reset_password/attach_policy(通用)`。`Flows` 里 `self._executor` / `self._issuer` 完全独立：`_run` 的 permission/account 分支与 `_revoke`、`_issue_sts` 只碰 executor；`_issue_longterm`、`_revoke_credential`、`reconcile_credentials` 只碰 issuer。
3. **同 AK 硬门**：`issuer_from_env` 构造时比对同账号的 EXEC AK ID，相同则 `ProvisionError`（只比环境变量，不调云，零成本）；`health.py` 同步 CRIT。配错这件事静默运行的后果是"最小权限"整个失效且没人知道。
4. **测试锁**（比代码约定更耐改）：
   - 给 `Flows` 注入"任何方法调用即 `AssertionError`"的假 executor，跑完整条长期发放 + 回收，必须全绿。
   - 反向：毒化的假 issuer，跑 permission 开通 / 到期回收 / 短期凭证发放，必须全绿。
   - 断言 `AliyunIssuer` 上不存在 `reset_password` / `assume_role` 属性。

评论身份不是云凭证，但同样是敏感配置：它决定"以谁的名义把 secret 贴进审批实例"，而且因为飞书不支持应用身份，这条评论会永远挂在一个真人名下。约束见 §4.3（只从配置读、不接受请求参数、启动自检、fail-closed）。

命名空间冲突（必须在 CRED-2 定掉）：bot 已经在同一主账号下用 `tempak-*` 建号。面板统一用 **`tempak-panel-` 前缀**（策略名 `temp-ak-auto-tempak-panel-*`）——它是 `panel-issuer` 已授权的 `tempak-*` / `temp-ak-auto-*` 的子集，**不需要改云上策略**，同时让对账能把"面板建的"和"bot 建的"分开。`reconcile_credentials` **只在 `tempak-panel-` 前缀内判孤儿**，绝不因为看见 bot 的号就报警，更不能自动删。

---

## 7. Task 拆解

owner：dev（面板源码）、tester（`tests/unit/**`）、auditor（只读复审）、researcher（云侧/飞书规格取证）、docwriter（文档）。工作量按"dev 一次连续工作"估：S ≈ 半天内，M ≈ 1 天，L ≈ 2 天以上。

| ID | 任务 | owner | blockedBy | 量 | 风险 |
|---|---|---|---|---|---|
| CRED-1 | 规格取证：① 用面板应用 token 反查 `ou_d3f646711beed260666c4c4c15c56ea7` 是否可解析，并对一条测试审批实例真发一条评论；② `comments` 接口传 `comment_id` 是覆盖还是回复；③ AssumeRole 的 `Policy` 参数在 v1.0 签名下的传法；④ `CreatePolicyVersion` 的 `RotateStrategy` 取值；⑤ `ListUsers` 与 `clouds/aliyun.paginate` 的 `MaxItems/Marker` 是否兼容；⑥ `panel-issuer` 在面板服务器上能否真调通（SourceIp） | researcher | — | M | 低 |
| CRED-2 | 定名：`tempak-panel-` 前缀方案确认，与 bot 命名空间隔离结论 | planner/dev | CRED-1 | S | 中（定错要改云上策略） |
| CRED-3 | `credpolicy.py`：policy 构造 + session policy 2048 校验 + 命名函数 | dev | — | M | 低 |
| CRED-4 | `credpolicy` + 评论正文生成函数的单测 | tester | CRED-3 | S | 低 |
| CRED-5 | `provision.AliyunIssuer` + `issuer_from_env/issuer_configured` + 同 AK 硬门 + `assume_role(policy=)` | dev | CRED-1, CRED-2 | M | 中 |
| CRED-6 | `approval.FeishuApproval.comment()` + `DELIVERY_APPROVAL_COMMENT_OPEN_ID` + `comment_ready()` + `verify_comment_identity()` + 跨应用错误人话 | dev | CRED-1 | M | 中 |
| CRED-7 | `catalog.py` credential 字段扩展与三条硬校验 | dev | — | S | 低 |
| CRED-8 | `flows` + `server` 主体：分流、`_issue_credential`、走 execute、评论交付与回滚、正文生成、删 claim 路径、`_revoke_credential`、`revoke_expired` 分派、`remind_expiring`、`reconcile_credentials`、`has_live_grant` | dev | CRED-3, CRED-5, CRED-6, CRED-7 | L | 高 |
| CRED-9 | 前端 + CLI + notify：申请表单时长预设与新文案、删 `showCredential`、重新签发按钮、"作废我的凭证"、`delivery creds` 改提示、`done/expiring/revoked` 文案 | dev | CRED-8 | M | 中 |
| CRED-10 | sweep 接线 + `health.py` 四项检查（含评论身份可解析性）+ 遗留 CLAIMABLE 单发布检查单 | dev | CRED-8 | M | 中 |
| CRED-11 | 测试批（见下） | tester | CRED-8 | L | 中 |
| CRED-12 | 审计复审（重点见下） | auditor | CRED-11 | M | — |
| CRED-13 | 文档：`cloud-access-platform.md`（R4 措辞、申请类型表、状态图、现状表）、`deploy/panel/README.md`、`issuer-policy.aliyun.example.json`、`.env.example` | docwriter | CRED-8 | M | 低 |

可并行：CRED-3 / CRED-5 / CRED-6 / CRED-7 互不依赖；CRED-4 与 CRED-6/7 并行；CRED-13 在 CRED-8 定型后与 9/10/11 并行。
必须串行：CRED-8 是汇合点，它之前不要动 `flows.py`（避免与 dev 抢文件）。

### 验收标准

**CRED-1**：产出 `docs/collab/research/` 一篇，六个问题各带出处（官方文档链接或真机返回片段）。第 ① 项必须有一条**真发成功**的测试评论的响应体，并写明 open_id 的取得/校验方式（可复现）。第 ② 项要明确：带 `comment_id` 重发是覆盖原文还是新增一条回复——这决定 §4.4 的重试能不能清掉旧 secret。

**CRED-2**：一句话结论 + 一条断言"`tempak-panel-<...>` 落在 `panel-issuer` 策略的 `tempak-*` 资源内，无需改云上策略"。若结论是要改策略，拆出一条前置人工任务。

**CRED-3 / CRED-4**：
- `caps={"write"}` 生成的文档里**没有任何 Delete 动作**，且**有**独立的桶元信息语句。
- 桶元信息语句的 `Condition` 里**没有** `oss:Prefix`，但**有**时间窗。
- `caps={"read"}` 不含 `GetObject`；`caps={"download"}` 不含 `ListObjects`。
- 时间窗字符串形如 `2026-09-16T10:00:00+08:00`，跨年不崩。
- `build_session_policy` 在超长 prefix 下抛错而不是静默截断。
- `user_name_for` 对中文姓名、纯符号、超长邮箱都产出匹配 `^tempak-panel-[a-z0-9.-]{1,40}-[0-9a-f]{6}$` 的名字，两次调用不相同。
- 评论正文函数：第一行含"云账号平台自动发放"与申请单号；secret 在全文中 `count == 1`；短期凭证多一行 SecurityToken、长期没有；`region` 为 `cn-shenzhen` 时 endpoint 是 `oss-cn-shenzhen.aliyuncs.com`（不出现 `oss-oss-`），`region` 为空时三行退化成"未知"那一行；含换行的输入被压平。

**CRED-5**：
- ISSUER 与 EXEC 的 AK ID 相同 → `ProvisionError`，异常消息里不含 AK。
- 首次写操作前调 `GetCallerIdentity`，AccountId 不符即中断（假 transport 断言调用序列）。
- `assume_role(policy=None)` 的请求参数与改动前**逐字一致**；`policy={...}` 时多且只多一个 `Policy` 参数。
- `hasattr(AliyunIssuer, "reset_password") is False`。

**CRED-6**：
- `comment()` 打到 `…/approval/v4/instances/{code}/comments?user_id_type=open_id&user_id=ou_…`，body 的 `content` 是 `{"text": ...}` 的 JSON 字符串。
- 飞书返回 `code=99992361` 时错误消息里出现"不属于面板的飞书应用"，不是原始 code。
- `DELIVERY_APPROVAL_COMMENT_OPEN_ID` 未配置时 `comment_ready()` 为 False，且 `comment()` 抛错而不是静默跳过。
- `verify_comment_identity()` 在假 transport 返回 `open_id cross app` 时给出人话错误，正常时返回空串。
- 正文里的 secret 不出现在任何日志里（捕获 stderr 的用例锁住）。

**CRED-7**：
- `max_days>0` 缺 `bucket` / `caps` / `region` → `CatalogError`，消息点名缺哪个字段。
- `max_days>0` 且 `max_hours=4` → `CatalogError`。
- `max_days=0` 的老模板解析结果与改动前**逐字相等**（用现有 `identity/request-templates.example.json` 回归）。
- `public()` 不含 `role_arn`，含新字段，credential 不含 `valid_days`。

**CRED-8**（最重的一条，验收拆成八组）：
1. 分流：`hours=12` → STS 路径，executor 被调用、issuer 零调用；`hours=13` → RAM 路径，issuer 被调用、executor 零调用。两者都贴了评论。
2. 自动发放：`sync` 观察到 APPROVED 后，credential 单直接走到 `DONE`，**中途不经过 `CLAIMABLE`**；`valid_until` 字段不再被写。
3. 交付前置：评论身份未配 / `instance_code` 为空 → 单子 `FAILED`，且**云上零调用**（断言 issuer/executor 都没被碰过）。
4. 交付失败回滚：`comment()` 抛错的桩下，长期路径必须调用删 AK（断言调用序列），单子 `FAILED`，`grant.ak_id` 未写入；重试能重新走通并贴评论。
5. secret 不落地：跑完一次成功发放，断言 `tickets.json` 全文、所有事件 note、捕获的 stdout/stderr、HTTP 响应体里都不含 secret 串。
6. 崩溃：在 `create_access_key` 抛错的桩下发放 → 单子 `FAILED`、`grant.user_created/policy_put` 为真、`ak_created` 为假；随后 `revoke_expired`（把 `expire_ts` 调到过去）能清掉云上残留，且**不改它的 status**。
7. 回收：正常到期 → 调用序列严格是 停用AK → 删AK → detach → 删版本 → 删策略 → 删用户；任一步抛错 → 状态保持 `DONE`、记一次 `credential_revoke_failed`、**再跑一次 sweep 不重复记同一条事件**、`grant.revoked_at_ts` 仍为 0；修好后再跑能收敛到 `REVOKED`。
8. 对账：云上有一个 tickets 里没有的 `tempak-panel-*` → 报一行孤儿；云上有 `tempak-别的-*`（模拟 bot 的号）→ **不报**；`reconcile_credentials` 全程零删除调用。

**CRED-9**：
- `max_days>0` 的模板能选到 >12h 预设，预览文案提到"发到飞书审批评论"与"到期删除子账号"。
- `POST .../credential` 的响应体里**没有**任何凭证字段（回归断言，防止有人改回去）。
- 长期单的重新签发按钮不可用；短期单可用且触发新评论。
- `delivery creds` 不再打印任何凭证，提示里给审批链接。
- `done` 通知正文提到去审批评论取，且不含凭证。

**CRED-10**：
- 缺 `DELIVERY_ISSUER_*`（且有 `max_days>0` 模板）/ 两把 AK 相同 / 评论 open_id 未配置 / 评论 open_id 解析失败 → 各一条 CRIT，且**在系统状态页可见**（不是只写日志）。
- 造一张过期超 1 小时未回收的单子 → WARN 且给出数量。
- `_sweep` 在 issuer 未配置时不崩（打印一行跳过），其它步骤照常跑（对齐 `cli_requests.py:394` "飞书不可用不影响回收"的取舍）。
- 发布检查单：`identity/tickets.json` 里无在途 `credential + claimable` 单。

**CRED-11**：上面各条落地 + 全量测试零失败 + 明确列出既存失败基线。

**CRED-12 审计重点**：
1. `_issue_credential` 的失败路径有没有可能"AK 已建、评论没发出去、单子却成了 DONE"，或者"AK 已建、回滚也失败、grant 却被标成已回收"。
2. secret 的全路径追踪：从 `create_access_key` / `assume_role` 返回到 `approval.comment()` 之间，有没有经过任何 `store.update` / `print` / `describe_error` / `notify` / HTTP 响应。
3. `revoke_expired` 的 credential 分支是否真的**不看 status**；`DONE/FAILED/CLOSED/REVOKED` 四种各构造一条走查。
4. `grant` 的读-改-写是否存在 last-write-wins 覆盖（`store.update(fields={"grant": ...})` 是整块覆盖）。
5. `_EXEC_FIELDS` 增减是否造成"审批期间管理员扩大 scope 却仍能发放"。
6. issuer/executor 调用面是否真的互斥（跑毒化用例，不只看代码）。
7. policy 文档里 `write` 是否混进 delete 语义；桶元信息语句是否误叠 `oss:Prefix`。
8. 评论正文：来源标注在不在第一行；secret 是否只出现一次；外部可控字段是否做了空白压缩；region 归一有没有拼出 `oss-oss-`。
9. `reconcile_credentials` 是否严格只读。

---

## 8. 优先级与顺序

1. CRED-1 / CRED-2（规格与命名）。成本最低、错了最贵：评论 open_id 验不通就整条交付通道不成立，命名定错要动云上策略。
2. CRED-3 / CRED-5 / CRED-6 / CRED-7（并行）。四块都是加法式新增，不改现有行为，可以先落先过审，把 CRED-8 的面积压小。
3. CRED-8。本次唯一的高风险改动，`flows.py` 单文件改十余处，期间冻结该文件。
4. CRED-11 + CRED-12。提交闸门在这里。
5. CRED-9 / CRED-10 / CRED-13。用户可见面与运维面，可与 4 并行推进，在 4 之后合入。

不建议：先上"能发长期凭证"、回收下一轮再做（理由见 §3 同批清单最后一条）。

---

## 9. 要问用户的问题

Q1 **要不要真正的"多选权限"**。现在三个 STS 角色各覆盖一种能力，session policy 只能收窄，"下载+上传"在短期路径上没有角色可用。v1 保持"一个模板 = 一种权限"。如果要在一个模板里勾选 read/download/write，需要先在云上建一个覆盖三者并集的角色（如 `panel-oss-rw`），短期路径 assume 它再用 session policy 收窄。做不做、谁去建？

Q2 **长期凭证的最长有效期上限**（`max_days` 硬顶）。bot 实际发到过 2 个月，面板建议 60 天。要不要更短（比如 30 天）？

Q3 **prefix 是模板写死还是申请人可填子目录**。写死最安全但一个桶下每个目录都要一个模板；允许填则要在 `_validate` 做前缀白名单（禁 `..`、必须落在模板 prefix 之下）。倾向 v1 写死。

Q4 **"审批通过即发放"是否确认**（§4.1）。取消领取环节意味着批了没人用也会建号（到期自动删）。确认采纳，还是坚持让申请人点一下？

Q5 **短期 STS 是否一并改成评论交付**（§4.7）。建议改（否则自动分流对申请人不透明）。代价是 12 小时从"领取时起算"变成"审批通过时起算"。接受吗？

Q6 **火山这边怎么算**。需求描述说"火山没有 STS，那边所有凭证都是长期形态"，但 `provision.py:396 VolcanoExecutor.assume_role` 确实在调 `sts.volcengineapi.com` 的 AssumeRole，`catalog._ROLE["volcano"]` 也接受 `trn:...:role/...`。这两件事对不上。火山的 credential 模板现在到底是哪种形态？本次要不要把火山纳入长期路径（需要 `VolcanoIssuer` + IAM 时间条件 policy，工作量约等于再做一遍 CRED-3/5/8；好在 `clouds/volcano.py` 的写操作成功判定刚修好，前置已通）？建议 v1 只做阿里云。

Q7 **到期前允不允许"延期"**。bot 有一条延长/撤销审批，改写 policy 时间窗、AK 不变。面板 v1 计划只支持"重新申请"。若要做延期，要连带处理 bot 刚踩过的坑：延期表单的起始时间会把 `not_before` 推到未来，凭证在延期通过那一刻反而失效（`langchaindev/core/temp_ak_issuance/orchestrator.py:404-414`）。

Q8 **审批流的抄送范围**（§4.6）。评论对所有能看到实例的人可见，而且署名是王梓涵本人。这条审批定义现在配了哪些抄送人？需不需要为凭证类单独建一条抄送范围更窄的审批定义？

Q9 **`region` 由谁填**。计划让管理员在模板里配裸 region（§1.1），发放时不查云。另一种做法是发放时用 issuer AK 调 `GetBucketLocation` 现查（多一个云调用、多一份权限、还要处理 bot 那个"探不到就说未知、绝不猜"的分支）。倾向模板里配，确认一下。

Q10 **孤儿号的处置权**。`reconcile_credentials` 只报不删。报出来之后谁去删、走什么流程（管理后台一个按钮，还是人工进控制台）？另外确认：bot 的 `tempak-*` 号是不是也在 `1704065796538912` 下——如果是，对账报告里要不要顺带把它们列成"另一套系统管理，勿动"。

Q11 **sweep 连续失败的告警**。回收连续 N 轮失败（SourceIp 配错、issuer AK 被轮换）目前只会打进 systemd 日志。要不要接 `DELIVERY_ALERT_WEBHOOK` 的管理员群告警，阈值多少？
