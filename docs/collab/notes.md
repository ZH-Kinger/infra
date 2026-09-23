# 协作记录

## 2026-09-18 面板交付批 · 审计结论

三轮审计,最后一轮报 1 个**阻塞项**,已修并真机验证:

**高-1 `approval._field` 没处理单选控件** —— `WIDGET_PICK`/`WIDGET_MULTI` 定义了但没人引用,
配置里一出现 `radioV2` 就撞 fail-closed,**拒发整张单子**(不是某个字段空)。而本批新增的
`delivery approval widgets --write` 正是把飞书返回的真实类型写进配置 —— 两件事是同一个开关的两端。
两个常量还各漏了旧名 `radio` / `checkbox`(后台人工建的控件可能回旧名)。

发现时线上**正在流血**:配置已经是 radioV2,代码还是没修的那版。修完立刻推,并对飞书真发了两张
测试单验证(`REQ-20260918-PROBE001` / `PROBE002`),确认 `radioV2` 和 `date` 都收得下、
选项 key 渲染成中文。**这两张测试单删不掉** —— 飞书没有删除审批实例的接口,它们提交后立刻
变成 `APPROVED`(定义里还没配审批人),撤回只对进行中的有效。标题里标了「测试单,请忽略」。

教训:这一批我"验证部署"的方式是加载配置看有没有报错,**从来没真提交过一张单**。
同类问题今天出现三次(`DELIVERY_PROXY_HOPS` 默认值、火山采集用了写权限身份、90 天上限没部署)。
部署后要验的不是「服务起来了吗」,是「这个功能真的能用吗」。

### 同批修掉的其它

- **中-1** `holdings` 绕过了 `/api/requests` 那道空 union_id 硬门 → 下沉到 `tickets.Store.mine`
- **中-2** `MAX_CREDENTIAL_HOURS` 是**加载期**硬校验不是提交时:调小它时**模板必须先于代码上线**,
  否则 `catalog.load` 整个失败、申请页整页死。注释原先说反了
- **中-3** `refresh` 没有 `--tickets`,台账读不到时静默返回空集 → 每个新增子账号都被报成
  「来路不明」,那一栏天天全量误报很快没人看。改成:文件不存在安静跳过,**读不了则进告警**
- **中-4** 审批单「云账号」那一栏送的是 `platform`,两个阿里云主账号(1949)分不开 →
  改送 `平台/账号`,审批定义的选项 key 同步
- **中-5** `approval.example.json`(identity/ 下唯一入库的文件)没跟上新形状,
  照它起新环境体检开机就红、六个新字段全静默丢失
- **中-6** `extra` 的键必须**逐字等于** `custom_id`,对不上静默丢弃 —— 这是设计,
  代价是「拼错」和「定义还没加」长得一样。`widgets` 命令现在会把缺的可选字段报出来

### 记账(下一批)

- 资产页那一大块(`holdings_view` / `is_asset` / `category_of` / `_resource_ids` / 批量指派)
  **单测零覆盖**
- 低危 9 条:`category_of` 对火山类型的子串误判、`_claim_resources` 整段吞异常且无日志、
  批量指派是 N 次独立读-改-写(非原子)、holdings 不判过期、`--write` 整表覆盖会丢手工配的条目、
  `web/assets.js` 的 `chips()` 是死代码 + 超 500 行的勾选看不见却会被指派
- `_EXEC_FIELDS` 接线前要把 `options` 和 `params` 一起纳入
- 部署 checklist 要写死:模板先于代码上线;「到期日」控件一旦被改成日期区间,资源单立刻发不出去

## [2026-09-18] [TESTER] 「重新打开已关闭的申请单」补测
- 新增 `tests/unit/test_delivery_request_reopen.py`（18 条）。全量 `make test`：1698 passed / 0 failed / 2 skipped。
- 安全面已锁：自审批被关的单子重开后 `execute` 仍抛 `SelfApprovalError`、审批事后撤销仍抛 `ApprovalError`、
  模板被改仍 409，三条都断言云侧零调用。重开不放宽任何门禁。
- `closed_from` 缺失或被改坏（`""`/`done`/`revoked`/非字符串）一律回 FAILED，不做状态机旁路。
- 凭证已签发（`cred_user` 或 `sealed.ciphertext`）拒重开：两个判据各自单独验过。
- 做了 6 组变异检查（砍掉 CLOSED 出边 / 忽略 closed_from / 去掉凭证守卫 / 盲信 closed_from /
  跳过 `_verify_approval` / 放宽按钮判据），每组都有用例变红。
- **阻塞 `make lint`**：`src/delivery/requests_api.py:9` 那行新增的接口清单注释 104 > 100 字符（E501），
  归 dev 改，tester 不碰源码。
- 顺手修既存失败：`tests/unit/test_delivery_workspace_tree.py::CustomDatasetTests.ok()` 缺
  `custom_dataset.validate()` 新增的 `oss_region`，补默认值并加一条「OSS 地域必须带 oss- 前缀」的用例。

[2026-09-18 AUDITOR] infra 体检新增「没登记的桶」+ OSS ListBuckets：无阻塞项、无需回滚（桶级签名逐字未变、多层传参三层已 AST 全量核过、无泄漏面）。3 MED：① load_registered_buckets 一坏一好时静默按单来源判且不进 skipped ② registered 为空集合能过 None 门→全量误报 ③ 桶清单只覆盖第一个阿里云 profile、Finding.account 为空（成员账号启用后会静默漏报）。3 LOW：--skip-pai 顺带关掉列桶 / cri-·oss-pai- 过滤吞多少无人知 / oss.call 空桶名由响亮失败退化为静默空清单。

[2026-09-18 DEV] 上面 6 条已处理 5 条：MED-1（缺/坏来源进 skipped，load_registered_buckets 改返 (names, notes)）、MED-2（抽不出名字即 None，空集合不再过门）、LOW-4（列桶挪出 --skip-pai）、LOW-6（服务级请求带 key 直接抛）、LOW-7（UnicodeDecodeError 一并接住，不再让体检页整页打不开）。
未做，记账：
· MED-3 桶清单只覆盖第一个阿里云 profile。成员账号（executor-policy.aliyun-collector-members.json 里配了 3 个）目前未启用，启用时必须同时把桶清单改成按账号收 {"account": uid, "buckets": [...]} 并给 Finding 填上 account，否则成员账号里的野桶永远不出现、也不留 skipped。
· LOW-5 cri-/oss-pai- 过滤吞掉的条数不出现在任何地方。线上那条真线索 h2r-dlc-<uid>-cn-shanghai 说明这份前缀表是经验值、会演进，应当让过滤可审计（section note 后追一句「另有 N 个云产品自建桶未列出」）。缺的是展示通道，Report/sections 要加字段，故未顺手做。

[2026-09-20 DEV] 管理员收权（面板上直接撤策略/用户组）落地。新增 src/delivery/revoke.py（纯逻辑，四条拒绝）、provision.attached()（两家云，实时读云上）、POST /api/admin/access/revoke（默认预演，apply 才动且理由必填）、人员页「收权」一节、review.log 留痕。
tester 报了 1 个阻塞 bug + 3 个缺口 + 2 个 nit，全部已修：
· 阻塞：server.py 用了 session.union_id，_WebSession 只有 .user —— apply 每次必崩，而且是**云上已经撤完之后**才崩，管理员会以为没撤成再点一次，且 review.log 一行都没有。dry-run 完全看不出来。
· 缺口①：火山 attached() 没按 PolicyScope 过滤。项目范围的策略 DetachUserPolicy 撤不掉，而 detach_policy 撤前先 has_policy（只认 Global）→ 查不到就 return → 面板记 done、界面说「已撤掉」、云上一动没动。
· 缺口②：granted_by_panel 的到期判据是 expires <= 0（「有没有填到期时间」），不是 expires <= now（「过没过期」），真实数据里恒假 —— 回收失败留下的残留被当成「面板发的」永远撤不掉。
· 缺口③：_all_tickets() 读失败返回 []，等于「面板一条都没发过」= 什么都不拒。一个坏掉的 tickets.json 就能让台账保护静默消失。改成返回 None、调用方 503 拒绝整次。
· nit：两家云 attached() 对「返回缺键/null」都改成抛，不再静默当空（静默会让页面显示「这个人什么都没挂」，而他可能挂着超管）；502 分支补脱敏。
未做，记账：
· 组带来的管理员权限不参与 last_admin 判断（_admin_holders 只看 user.policies）。火山 wuji-opration 组就是发超管的，那个组的成员算不进 holders。
· 前端 revokeBox 没有测试：app.js 零 export 且 import 即 boot()，要测得先把它挪进可导入位置。

[2026-09-20 DEV] 其他：AK 台账加「自己去控制台换」引导（platforms.key_console，面板不代办 —— 代办要经手 secret，与 sealed 的前提冲突）；「没采到密钥」提示原来写死阿里云动作名 ram:ListAccessKeys，而报出来的可能是火山账号，已按平台说。火山采集身份缺 iam:ListAccessKeys（实测 AccessDenied），45 个火山用户的 AK 台账全空 —— 体检的「AK 该换/没人用」两类对火山是瞎的，等加权限。

[2026-09-22] [AUDITOR] infra 面板复审：闸门不通过——新 High 4 条（dataflow 被 _locate 挡死 / CPFS 目录未转相对路径 / 重签凭证废掉云上已登记 key / 整桶闸填 "/" 可绕过），B1/H1/H2/H3 已确认修复。

[2026-09-22 DEV] 上面 4 条 High 已修，另修 M-1～M-4、L-1，已送第三轮复审。
· CPFS 数据流动真机：绑定的 OSS 前缀在 SourceStoragePath，不在 SourceStorage；任务目录必须相对绑定根。DryRun=true 会回一个 task-… 号，但不会真的建任务（按 DataFlowIds 列任务核过）。bot 的 start_task 不分方向，只有沉降那条真机验过。
· vePFS DescribeDataFlowTasks：DataFlowTaskIds 要传字符串，并且必须带分页（真机报 InvalidParameter 后改的）。IAM 动作名已由报错确认：vepfs:DescribeDataFlowTasks / DescribeFileSystems。
· 在面板机上用 root 跑写 identity/ 的脚本，会把 tickets.json 改成 root 属主，面板挂了 100 分钟。写操作一律 sudo -u delivery。

[2026-09-22] [AUDITOR] infra 面板二次复审：闸门通过。H-A/B/C/D、M-1~M-4、L-1 确认修复；遗留 Low：dms.find_task 翻页上限返回 None 可致 H-C 复现、复用分支未记钥匙名、reclaim 事件刷屏、Import 方向仅 DryRun 验证。

[2026-09-22 DEV] 面板切到 https://cloud.wuji-tech.com（Let's Encrypt，certbot.timer 续期 + deploy hook reload nginx）。按 IP 访问保留自签证书，因为已发出的取件链接写的是 IP。DELIVERY_BASE_URL 已改，飞书应用 cli_aa2d10ccd0b9dbb7 的重定向 URL 已加新地址。

[2026-09-22] [AUDITOR] infra 面板三审（R1-R4）：闸门通过。遗留两处小瑕疵——move_cred_left 存的是原始错误文本，文本每次不同时仍会每轮写一次；关单确认文案没区分同云和数据流动的情况；首单真实预热要用小目录盯着跑完。

[2026-09-22 DEV] 两处小瑕疵已修：move_cred_left 落盘前经 mover._stable 抹掉 RequestId/UUID；关单文案区分跨云（钥匙撤、任务失败）与同云/数据流动（云上任务照常跑完）。

[2026-09-22 DEV] CPFS 预热 / 沉降真机实跑通过（走面板 mover 代码路径）：oss://wuji-datasets-hz-6c661af0/_panel-selftest/20260922/ → cpfs /share/datasets/_panel-selftest/20260922/（task-00524a83c0d04041，90s，43B/1 文件），再沉降回 _panel-selftest/20260922-sink/（task-00bbc6b6551ce689），原件与回程逐字节一致。OSS 测试对象已删；CPFS 上留一个 43B 的 hello.txt（面板无删 CPFS 权限，刻意）。vePFS 方向仍只过了只读接口，未实跑。

[2026-09-22] [AUDITOR] infra 数据类型词表：闸门通过（无 High）。Medium 两条待修——server.catalog 缓存只看模板文件的修改时间，data-types.json 和 workspaces.json 改了不会生效（新类型批完选不到）；datatypes.append 不加锁，并发会丢写。Low：写成功但单子没落盘时重试会误判失败、note 没写进词表、datatype 模板被子账号门挡住。

[2026-09-22] [AUDITOR] infra 九章人工登记 + M-1/M-2 修复：闸门通过（无 High）。M-A：inventory.parse 丢了 source，页面看不出登记表过期，漏报风险在登记之后新开的号；Low：离职提示没说去九章控制台、iam 导出多出 skip 行、前端没有九章显示名、登记表权限应为 600。

[2026-09-22] [AUDITOR] infra 九章人工登记修复（M-A, L-a~L-d）：闸门通过。L-d 登记表权限不对就整轮不刷新，这个取舍可以接受——每轮都会发飞书告警，快照和名册保持不动；遗留：失败时报告在日志里打印两次，refresh 单元的 OnFailure 兜底还没启用（老问题）。

[2026-09-22 DEV] 上面各轮的 Medium / Low 已修（遗留的两条小瑕疵除外）。新建 wuji-provider-hz / wuji-processed-hz / wuji-processed-sing 三个桶；wuji-bucket-hangzhou 不配旧版本清理（用户决定）。九章 18 人按邮箱关联进名册，账号 ID 暂填 wuji，待确认。

[2026-09-22] [AUDITOR] infra 告警兜底（OnFailure + 私聊管理员 + 退出码 3）：闸门通过。退出码 3 不会掩盖崩溃（告警送到才返回 3，送不到返回 1 触发 OnFailure）；_admin_alert 断网时最坏耗时约「管理员数 × 15 秒」，建议循环前先取一次 token 快速失败；moves/sweep/buckets/iam-remind 也该接上 OnFailure。
- [2026-09-22] [AUDITOR] infra delivery: directory-departure check + 九章 manual registration page passed audit; deferred M-4 per-person snooze, M-7 check prod offline-accounts.json format before first panel save, L-12/13/14.
- [2026-09-22] [AUDITOR] infra 离职停号复审：闸门通过。H-1/M-1/M-2 与 L-1~L-4/L-6/L-7 确认修复；遗留 Low：火山 disable 的 gone 判定建议再用 GetUser 复核、超限和冻结产生的待确认记录不会自动清、L-5/L-8、iam-remind 仍未接 OnFailure。（火山保护 Deny 已补 RemoveUserFromGroup/DetachUserPolicy）
- [2026-09-23] [AUDITOR] infra 卡片回调 + 告警分级 + 九章纳入离职：闸门通过，无 High，不需回滚。随提交修了 Med-1（强信号检出的九章账号进不了卡片）、Low-1（unverified 闸门排到 manual 之前）、Low-2（同一个号在卡片上出现两行）、Low-3/4（验签排到解密与 token 之前）、Low-5（NaN 时间戳）。遗留 Low：claim 非原子且只在内存（真正的闸门是 offboard.decide 的 flock + 状态机）、card2 里还留着 1.0 的 wide_screen_mode、_admin_alert 卡片发失败时未退回纯文本。
- [2026-09-23] 真机发现（Card JSON 2.0）：没有 note 组件（回 200861，灰字只能用 markdown 的 font 标签）；没有 action 容器（按钮直接是元素，并排用 column_set）。1.0 的卡（reclaim/drift/build/move）保持原样，别顺手改成 2.0 元素。
- [2026-09-23] [TESTER] infra 待办聚合（todo.py / /api/admin/todo / web/todo.js）+ 两档到期提醒：新增 5 个测试文件、131 条用例。三个真实缺陷：① `todo._order` 用 `-(since)` 排序 → **最新的排最前**，与「挂得久的在前」的设计文档相反（todo.py:127，真机复现：9 天的离职待办排在 12 天的对账漂移前面）；② 模块开头写的「依据旧了降级到 NORMAL」**从未实现**，`weaken()` 全仓库零引用 → 12 天前的对账结果仍占 URGENT 并点亮导航红点（todo.py:233/145）；③ AC-5 口径在「待确认归属」这一维仍然分叉：views 把 pending 当已对上、hygiene 只看 accounts（views.py:210 vs hygiene.py:597）。均已留 expectedFailure 用例 + 配对的「当前行为锁」。到期提醒：发送失败那一档**会被消耗、永不重试**（先记事件再发），老单子升级后会多收一条 7 天档提醒 —— 两条都按当前行为锁住并写明代价。
- [2026-09-23] 真机发现（OSS 权限）：① `HeadObject`/`GetObjectMeta` 在 RAM 里都映射到 `oss:GetObject`，「只给元信息不给下载」做不到；② 桶开了版本控制时，带 version id 的 GET/HEAD 走的是 `oss:GetObjectVersion`，缺它的表现是「能下载、带版本号的元信息 403」（lakeFS 这类 S3 兼容客户端默认带版本号，线上踩过）；③ `oss:GetBucketLocation` 缺失时 S3 客户端建连失败，报错与权限无关。三条都已补进 platforms.py 与 bot 的 policy.py（bot 侧待部署）。
- [2026-09-23] 凭证内外分家：面板发给内部同事的用 `staff-` / `staff-oss-auto-`，机器人发给外部方的保持 `tempak-` / `temp-ak-auto-`，跨云迁移交给对方云的钥匙也算外部。云上 wuji-panel-issuer 策略已同步放开新前缀（副本在 deploy/panel/cloud-policies/）。存量 10 个不改名，到期自然清理。
- [2026-09-23] [TESTER] 采集登录状态 + 地域/工作空间对账：补 49 条用例（夹具学会 GetLoginProfile；三态「不知道」绝不退化成「否」；按工作空间 ID 对账而非按地域；地域带平台前缀）。做了变异验证：把三处实现故意改坏，12 条用例报红，全部挡住。未发现 src 真 bug。
- [2026-09-23] 真机：采集身份原本没有 ram:GetLoginProfile / iam:GetLoginProfile，登录状态那一列上线后会静默全是「不知道」。两朵云都补了只读权限并验证拿得到真值（副本入库 deploy/panel/cloud-policies/）。顺带发现 wuji-ci 这个 CI 服务号开着控制台登录。
- [2026-09-23] 工作空间识别**暂不进待办页**：没有算力队列的工作空间是空壳（线上 10 个里多数是），全报出来会把待办页变回清单页。判断「有没有卡」的 PAI 配额接口还没确认，见 docs/collab/research/pai-quota-discovery.md。在那之前只留命令行入口 `requests regions --discover`。
- [2026-09-23] [TESTER] 三轮共补 92 条用例（登录三态 / 地域-工作空间对账 / VMP 类型名 + 配额三态），并对 7 个变异做了验证（把 src 复制到临时目录改坏，28 条用例报红，全部挡住）。未发现 src 真 bug。轨迹 2864 → 2967。
- [2026-09-23] 真机修正两处我报错的数字：① 火山那 3 个「工作空间」其实是 `Volcengine::VMP::Workspace`（托管 Prometheus 的监控工作区），根因是判据写了裸的 `Workspace`，而火山任何产品的工作区在 `ResourceType` 里都叫这个名 —— 现改用带产品前缀的 `TypeName` 并保留 `service` 字段；② 按地域比会漏掉同地域的第二个工作空间（杭州的 ai_hz_gpu，144 张 GU8T），改成按工作空间 ID 比。
- [2026-09-23] PAI 配额接口（研究员查证 + 真机验证）：PaiStudio `GET pai.<region>.aliyuncs.com/api/v1/quotas/`，version 2022-01-12，采集身份零改动可调；每条配额自带 `Workspaces[]`，不用反查。**返回按调用者的工作空间成员身份裁剪** —— 「查不到配额」和「真没卡」长得一样，而要判的恰恰是采集身份多半不在其中的未登记空间，所以必须三态（yes/no/unknown），unknown 照列不过滤。没配额的空间照样能跑任务（落公共资源组按量付费），文案写「没有专属算力配额」而不是「不可用」。
- [2026-09-23] [AUDITOR] 复审：闸门通过，无 High。H-1/H-2 与 M-1~M-5 逐条关闭。本轮新发现已随提交修掉：Med-1 远档缺下界（剩 12 小时会一轮发两张一样的卡）、Med-2 永久失败被 1 分钟一轮的 sweep 无限重试（7 天堆两万条事件）、Low-3/4/7/8。遗留不拦提交：火山 executor 的 Deny 比阿里少 CreateLoginProfile/AttachUserPolicy/AddUserToGroup（既存）、TOS 版本删除动作待真机确认。
- [2026-09-23] 教训两条：① 送审期间不要再改同一棵树 —— 审计那 30→11→7→0 的失败是在移动的代码上跑出来的，不是套件不稳；② 修 Med-2 时我把「同因失败只记一次」放在了写标记之后，结果飞书抖动超过一分钟这一档就永远发不出去，比原问题更糟（tester 复现）。判断必须排在写标记之前：重试那一轮不写任何标记，发成了再补。

[2026-09-23] [AUDITOR] 面板告警闭环 + 待办覆盖 + cred_orphan：无阻塞，4 中 9 低。
  核过：revoke_expired 的 11 条成功/中性返回行全部不被 is_trouble 误判；AST 扫描覆盖五个步骤
  函数链路的全部产出行；_CLOSED_STATES 不含 revoked 正确（_mark_revoked 不清 cred_user）；
  StateDirectory 写法正确、过渡（丢旧冷却记录 ≤6 条多余告警）可接受；rest 的 dict 相等不会误剔。
  提交前已修：① 兜底告警文案不再教人加 SuccessExitStatus（照做会关掉安全网，已加回归锁）；
  ② UNIT_ALERT_STATE 优先读 systemd 注入的 $STATE_DIRECTORY（只推 src/ 不更新 unit 时不至于
  静默失效）；③ server 的 live 元组改引用 todo._CLOSED_STATES（那个含 DONE 的死分支是诱饵）；
  ④ orphan 剔除改取反判据；⑤ unit 注释「没有 root 改属主老坑」说得太满 + EnvironmentFile
  覆盖 Environment= 的坑；⑥ sweep.timer 描述「每 10 分钟」→「每分钟」。
  下一批（审计同意可延后）：Med-3 submit_failed 是无出边终态、报上待办页后没有任何按钮能处理；
  Med-4「管理员点了作废、云那边没删成」（DONE + cred_user + sealed 已清）这第三种残留仍不上页；
  Low-1 provision.remove_from_group 不吞 EntityNotExist.Group → 组被删后永久「回收失败」且无人能关掉；
  Low-2 AST 扫描跳过 _mark_revoked/_revoke_failed 的透传 note（已漏一条未签字）。

- [2026-09-23] [TESTER] 演习标记 / `_how_it_died` 的测试补齐：新增 `tests/unit/test_delivery_alert_drill.py`
  （17 用例 + 26 subtest）——演习标记在标题和正文两处、真故障绝不写成演习、退出码/信号透传、
  `MONITOR_*` 畸形值（空串/非数字/带换行/五万字符）不许把告警通道带塌、演习照常记账。
  另加单元文件那半边：所有挂 `OnFailure=delivery-unit-failed@%n.service` 的单元必须 `Type=oneshot`
  且用 `@%n` 模板（破了 → systemd 不注入 `MONITOR_*` → **真故障的标题变成「告警演习」**）。
  全量 `pytest -q tests/unit` 3105 passed / 567 subtests（基线 3088/541），node 83 pass，ruff 干净。
  顺手收掉两条测试卫生：`UnitFailedTests.setUp` 的 mkdtemp 没回收；cooldown 里两处「状态文件落在
  identity/ 下」的过时 docstring。两个既有文件的 setUp 另加了 `MONITOR_*` 环境隔离（否则跑测试的
  shell 里有这几个变量就会换一条分支测）。**未真机验证**：面板机上还没跑过一次真演习/真失败。
- [2026-09-23] [TESTER] dev 修了两条现状固定条，测试已翻面并复核通过：`MONITOR_UNIT=""` 现在算真故障
  （`_is_drill()` 判 `is None`，方向对 —— 真故障被写成演习比反过来危险）；带换行的值被 `_flat()` 压平，
  「去哪看日志」+ 三条来源不再被挤出卡片（承重墙仍在：五万字符 → 卡片 849 字节 / 7 元素 / 最长 234）。
  新增 `IsDrillJudgementTests`（判据与正文解耦）+ `FallbackCoverageTests`（每个 timer 的 service 必须挂
  `OnFailure`，防新增定时任务静默无兜底；7 个 timer 全合规）。演习 how-to（用 `drill.service`、别拿真单元名演）
  写进了 drill 测试的 module docstring。全量 3109 passed / 585 subtests，node 83 pass，ruff 干净。
- [2026-09-23] [TESTER] 判据第二次重写（审计推翻 v250/oneshot 两个假设）后用例已跟到新语义：
  新增 `OldSystemdTests`（**Med-1 核心**：只有 `INVOCATION_ID` → 标题仍是「定时任务没跑成」+ stderr 留痕
  + 冷却记在真单元键下）、`_is_drill` 四层真值表 + `drill` 名精确匹配（`drill2` 不算）、
  `MONITOR_SERVICE_RESULT` 兜底（start-limit-hit 等四种 + 三者齐全时退出码优先）；
  `DrillAccountingTests` 整组翻成「演习记在 `drill:<unit>`、真单元的冷却和连号一点不动」（9/23 事故回归锁）。
  **发现并修掉一个环境依赖**：本机 shell 有 `INVOCATION_ID`（实测），cooldown/fallback 两个文件正是靠它
  才走到真故障分支——CI 上没有的话键会变成 `drill:...`、满屏 KeyError。两个 setUp 已显式钉成真故障环境。
  三个告警文件 69 passed / 82 subtests（drill 一个文件 29/63）；全量 3180 passed / 1 xfailed / 615 subtests（xfail 属
  `test_delivery_regrant.py`，不是本条），node 83 pass，ruff 干净。

[2026-09-23] [AUDITOR] 演习标记复审：0 高 1 中 5 低，无阻塞。
  Med-1 推翻了两个写进注释的「依据」（审计查了 systemd v255 源码）：① MONITOR_* 是 **v251** 起
  不是 250 —— 本项目开发机就是 249，前提在爆炸半径内已有一台机器不成立；② 真正条件不是
  「触发方 Type=oneshot」而是「同一 handler 实例只能有一个触发方」（service.c:1574），即 @%n 保证的事。
  前提一破 = 所有真告警标题变「告警演习」，是最坏的失效方向。已改成「演习自报家门」三层判据：
  MONITOR_UNIT 在→真故障；实例名是 drill/drill.service→演习；有 INVOCATION_ID（v232 起，
  对任何 systemd 单元都注入、与 MONITOR_* 条件独立）→真故障并留一行 stderr；都没有→演习。
  另采纳审计给的第三条路：**演习记在 `drill:<unit>` 另一把键下** —— 9/23 我拿真单元名演了两次、
  把 delivery-sweep 静音到 22:55，现在这件事是代码关掉的，不靠人记文档。
  补 MONITOR_SERVICE_RESULT 兜底（start-limit-hit 这类「主进程压根没起来」的真故障，
  EXIT_CODE/STATUS 都拿不到，以前正文一个字不说）。
  EnvironmentFile 会覆盖 systemd 注入的 MONITOR_*（exec-invoke.c:4505 merge 后者胜），
  已并进单元文件里那段覆盖警告。
  测试侧抓到一个环境依赖的假绿：**本机 shell 自带 INVOCATION_ID**，两个兄弟文件今天绿是靠它
  把自己送进真故障分支，干净 CI 容器里会满屏 KeyError —— 已在 setUp 里把环境钉死。
  未做：真机端到端（面板机上跑一次 @drill.service 看标题、让 sweep 真退一次非零看退出码），
  以及面板机 systemd 版本是否 ≥251（<251 走的是「标题对、正文缺怎么死的、stderr 有留痕」那条路）。
