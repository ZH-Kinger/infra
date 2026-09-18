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
