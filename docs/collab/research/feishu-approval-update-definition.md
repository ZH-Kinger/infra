# 飞书审批定义能不能用 API 改？——给 `301E99EB-A4BC-4F08-AFAF-46906A006C08` 加 kind 选项的取证

> 调研日期 2026-09-23 / researcher。**全程只读**：只 GET 了文档、只读了面板机上那份备份，没有发起任何写请求。
> 结论先行：**不要用 API 改这条定义。去审批后台（devMode）手工加四个选项。** 理由见 ③④⑤。

---

## 结论摘要（先看这段）

| 问题 | 结论 | 可信度 |
|---|---|---|
| 有没有「更新审批定义」接口 | **没有独立的 update/patch**。只有 `POST /open-apis/approval/v4/approvals`（创建）和 `GET /open-apis/approval/v4/approvals/:approval_code`（查看） | 【文档】 |
| 传已存在的 `approval_code` 会怎样 | **会更新，且是「覆盖原定义内容的全量更新」**（官方原话）。不是报错、不是建新定义 | 【文档】 |
| 「只能改 API 建的定义」这类限制 | 文档**没有**这条限制。文档有的是反过来的那条：**API 建出来的定义，后台和 API 都停用不了、删不掉** | 【文档】 |
| 改表单后 widget id 会不会变 | **官方明说「不能保证原有控件的默认 ID 不变」**；`custom_id` **保证不变**。面板代码里那条注释是对的 | 【文档】 |
| 在途实例受不受影响 | 官方无明确表述。飞书审批 2025.06 起定义有**版本管理**，且「流程中的审批不支持修改」——在途单按发起时版本走的可能性大，但**不能当保证** | 【推测】 |
| 最小请求体 | 不存在「只改一个控件」的最小体。`approval_name`/`viewers`/`form`/`node_list`/`i18n_resources` **全是必填**，全量覆盖 | 【文档】 |
| 那到底怎么改 | **审批管理后台 + `&devMode=on` 手工加选项**，改完重跑 `delivery approval widgets --write` | — |

---

## ① 接口面：只有 create，传 code 即全量覆盖

- 创建：`POST https://open.feishu.cn/open-apis/approval/v4/approvals`
- 查看：`GET https://open.feishu.cn/open-apis/approval/v4/approvals/:approval_code`
- **没有** update / patch / 修改 端点（审批概述页的审批定义分组下只有这两个）。

`approval_code` 参数官方原文（【文档】）：

> 该参数不传值时，表示新建审批定义，最终响应结果会返回由系统自动生成的审批定义 Code。
> **该参数传入指定审批定义 Code 时，表示调用该接口更新该审批定义内容，更新方式为覆盖原定义内容的全量更新。**

同页另外两条限制（【文档】，都指向「别用」）：

> **通过该 API 创建的审批定义，无法从审批管理后台或以 API 方式停用、删除，请谨慎调用。**

> **不推荐企业自建应用使用该 API 创建审批定义**，如有需要，尽量联系企业管理员在审批管理后台创建定义。

> **API 方式不支持设置条件分支**，如需设置条件分支请前往审批后台创建审批定义。

权限要求：`approval:approval` 或 `approval:definition`（面板 app 当前有没有这两个 scope 没查——没查是因为查它要么翻开发者后台、要么发请求，两者都超出只读范围；**没有 scope 的话调用会直接被拒，这反而是安全侧**）。

出处：
- https://open.feishu.cn/document/server-docs/approval-v4/approval/create?lang=zh-CN
- https://open.larksuite.com/document/server-docs/approval-v4/approval/create（英文站同页，「不支持条件分支」这句在这版里更显眼）
- https://open.feishu.cn/document/server-docs/approval-v4/approval-overview?lang=zh-CN

**没有**找到任何「只能更新由本 API 创建的定义 / 后台建的定义不能用 API 更新」的表述。也就是说：**接口很可能真的会让你覆盖这条后台建的生产定义** —— 这不是保护，这是风险。

---

## ② widget id 会不会变：会。custom_id 不会。

官方接入指南原文（【文档】）：

> **审批定义在修改并发布后不能保证原有控件的默认 ID 不变**，但是**自定义 ID 会保证不随着定义的修改而变动**，所以我们更推荐使用自定义 ID 来进行开发。

> 在一个审批定义中自定义 ID 不可重复。（自定义 ID 在审批后台地址栏加 `&devMode=on` 进开发者模式后可编辑）

出处：https://feishu.apifox.cn/doc-1974108 （飞书官方 API 文档 apifox 镜像，「原生审批接入指南」第 2 步）

对照 `src/delivery/approval.py:480-483` 那条注释：**核实通过，说法成立**（措辞可以更准一点：不是「一定会重新生成」，是「不保证不变」；后果一样，必须当会变处理）。

### 但这条约束可以从代码侧消掉（建议 dev 收进 backlog）

飞书官方在同一份指南里给了正解：**发起实例和读实例都可以直接用 custom_id 顶替默认 id**。

> 这里也可以用到我们在第 2 步设置的自定义表单 ID 和节点 ID 来进行创建，比如说我们指定了数字控件的自定义 ID 为『number』……则可以将下面的请求中『form』字段里的数字控件默认 ID 替换为『number』

读侧也有依据：`GET /approval/v4/instances/:instance_code` 返回的 `form` 里每个控件**带 `custom_id`**（官方响应示例：`[{"id":"widget1","custom_id":"user_info","name":"Item application","type":"textarea"}]`；文档注明「如果没设置自定义 ID，则不返回该参数值」）。
出处：https://open.feishu.cn/document/server-docs/approval-v4/instance/get?lang=zh-CN

含义：

- `ApprovalClient.create()` 发 form 时把 `id` 填 **custom_id**（`ticket_id`/`kind`/…）即可，不必先查 widget_map；
- `_form_value()`（`approval.py:542`）改成**优先按 `custom_id` 匹配**、匹配不到再回落 `id`，那条「改过表单必须停机重跑 `widgets --write`，否则所有审批被判单号不一致」的悬崖就没了；
- 这一条是**加法式**改动，可以和本次加选项解耦、分开做。**注意**：`_wid()` 还要从配置里取控件 `type`（单选/多选/日期的 value 形状不同），所以 widget_map 那份配置不能整份删，只是 `id` 那一半不再是承重件。
- 本条属于「文档支持 + 未在本项目真机验证」，落地前请 tester 用一张测试单子实测一次（【文档】+待实测）。

---

## ③ 在途实例：没有官方保证，按「不确定」处理

- 官方 API 文档、帮助中心都**没有**一句「更新定义不影响已发起实例」。
- 侧面证据（【文档】）：飞书审批 2025.06 更新日志——「审批编辑支持版本管理，管理员可在审批编辑页面点击左上角版本信息，查看并切换历史版本」→ 说明定义是**带版本**的，实例大概率绑定发起时的版本。
  出处：https://www.feishu.cn/hc/zh-CN/articles/360049067392
- 另一条（【文档】）：「对于流程中的审批：不支持修改」（指审批单本身不能改），出处 https://www.feishu.cn/hc/zh-CN/articles/230164856321
- 【推测】**只加一个单选选项**这种纯增量改动，对在途单子的表单渲染和流转几乎不可能有影响（老单子的 value 仍是四个旧 key 之一，选项表里仍有）。真正的风险不在「加选项」，在「全量覆盖」把审批人/分支/控件一起换掉。

**面板侧还有一条自己的硬约束**（`approval.py:420-427` 注释，历史已踩）：换定义 code 会让在途单子全部卡在「申请单号不一致」；当年是靠「切换时在途正好是 0 张」躲过去的。现在有 14 张活单 —— **只要 widget id 漂了、配置没同步，同样的事会立刻重演**（14 张全废）。

---

## ④ 最小请求体：不存在「只改一栏」的最小体

必填字段（【文档】，创建审批定义请求体）：

| 字段 | 必填 | 备注 |
|---|---|---|
| `approval_name` | 是 | 必须是 `@i18n@` 开头的 i18n key（≥9 字符），配合 `i18n_resources` |
| `viewers` | 是 | 最多 200 条 |
| `form.form_content` | 是 | JSON 数组压缩转义成 string |
| `node_list` | 是 | START 必须第一个、END 必须最后一个 |
| `i18n_resources` | 是 | `locale`/`texts[{key,value}]`/`is_default` |
| `approval_code` | 否 | 传了=更新 |
| `description` / `settings` / `config` / `icon` / `process_manager_ids` | 否 | `config.can_update_form/process/viewer/revert` 控制后台还能不能改 |

所以「漏掉的控件会不会被删」这个问题不成立：**全量覆盖 = 你没传的东西就没了**；而 `node_list` 是必填，你不传直接报参数错误，传了就等于重写审批流。

---

## ⑤ 致命点：GET 的返回**喂不回** POST（这才是不能用 API 改的真正原因）

我读了面板机上那份备份 `/tmp/approval-backup-1790162106.json`（只读，6601B，`data` 下 6 个键：`approval_name` / `form` / `form_widget_relation` / `node_list` / `status` / `viewers`）。逐项对照创建接口的入参：

**(a) `node_list` 形状完全不同，而且丢审批人 —— 最危险的一条**

备份里的三个节点长这样（user 字段已打码，这里是原样结构）：

```
{"approver_chosen_multi": false, "custom_node_id": "approve", "empty_assignee_list": [],
 "empty_auto_pass": true, "name": "审批", "need_approver": false,
 "node_id": "04bb6f50f68a9e9b0ad4d4231b9f3a21", "node_type": "AND", "require_signature": false}
```

- GET 给的是 `node_id`(hash) / `custom_node_id` / `need_approver` / `empty_auto_pass` / `require_signature`；
- POST 要的是 `id`("START"/"END") / `name`(`@i18n@` key) / **`approver[]`** / **`privilege_field{writable,readable}`**；
- **GET 根本不返回 `approver`**（审批人是谁，备份里没有，文档的响应 schema 里也没有这个字段）。

也就是说：**拿这份备份重建，必然重建出一条「没有审批人」的审批流**。而这个节点带着 `empty_auto_pass: true`（审批人为空时自动通过）。面板的唯一放行条件是「飞书审批实例级 APPROVED」（`docs/cloud-access-platform.md` R1 / `verify_approved`）——把审批人清空 + 自动通过，等于**整个云权限发放的门禁被拆掉，而且外表看起来一切正常**。这是本次调研里唯一一条「猜错代价无法承受」的风险。

**(b) `form` 的控件形状也不同**

备份里 15 个控件，全是后台建的样子：id 形如 `widget17897096511`（时间戳+序号），并且带 `display_condition` / `visible` / `printable` / `enable_default_value` / `widget_default_value` / `default_value_type` 这些**创建接口文档里没有的字段**；`name` 是中文明文。

而创建接口要的是：`name` 用 `@i18n@` key，单选选项按文档示例是 `"value":[{"key":"1","text":"@i18n@choice1"}]`。
备份里 `kind` 控件的选项则是：

```json
"option": [{"value":"credential","text":"访问凭证"}, {"value":"account","text":"开账号"},
           {"value":"permission","text":"云账号权限"}, {"value":"resource","text":"资源开通"}]
```

两种写法不一致（`option[].value/text` vs `value[].key/text`，明文 vs i18n key）。原样回传是「赌飞书两种都吃」，赌输的后果是 15 个控件的 id 全变。

**(c) `form_widget_relation` 传不回去**

GET 返回它（本定义是 `{"groups":[]}`，即没有联动规则），但**创建接口的请求体里没有这个字段** → 无法提交。今天是空的所以不丢东西，但这也说明 GET/POST 不是一对。

**(d) 条件分支**

文档明说 API 不支持条件分支。GET 的 `node_list` **也看不出**有没有分支 —— 所以你无法在覆盖前确认「这条定义有没有分支要丢」。`src/delivery/approval.py:354` 和 `flows.py:312` 两处注释都提到「条件分支正是按 kind 分流的」（`planning/service-access-mlflow.md:383` 又说「需要在飞书那边另配条件分支 —— 本期不做」），真实情况待人工在后台确认；不管有没有，**盲覆盖**这件事本身不可接受。

**(e) 不可逆**

API 建出来的定义「无法从审批管理后台或以 API 方式停用、删除」。一旦覆盖出问题，你不能删掉重来，只能再覆盖一次 —— 而你手上的备份**不足以复原**（见 a/b/c）。`config.can_update_form/can_update_process` 若没传对，还可能把后台的编辑入口一起关掉。

**顺带一条好消息**：widget id 是后台时间戳样式 + 每个控件都有 custom_id → 这条定义是**在后台用 devMode 建的**，面板代码里也没有任何调 `POST /approvals` 的地方（`grep` 只有 `widgets()` 那一处 GET）。所以它现在是「可在后台自由编辑」的状态，别把它变成 API 定义。

---

## 可执行方案（推荐）

### 做法：审批管理后台手工加选项

1. 打开审批后台该定义的编辑页，地址栏末尾加 `&devMode=on` 重载（开发者模式，右侧栏会出现自定义 ID 输入框）。
2. 选中「申请类型」控件（`custom_id=kind`），在选项列表里**追加**四条（**只加，不动已有四条的顺序和 value**）：

   | 选项 value（必须精确） | 显示文案（建议，与 `catalog.KIND_LABELS` 对齐） |
   |---|---|
   | `storage` | 数据目录 |
   | `transfer` | 数据迁移 |
   | `datatype` | 数据类型 |
   | `service` | （按 catalog 里新 kind 的 label 填） |

   > 后台选项编辑框里填的是「显示文案」，选项 key/value 需要在 devMode 下单独指定。**必须逐个核对 value 是英文标识**，填成中文的话面板送 `storage` 过去照样匹配不到 —— 那就等于什么都没改。
   > 注：`service` 这个 kind 在 `src/delivery/catalog.py:KINDS` 里目前**还不存在**（现有七个是 account/permission/credential/storage/transfer/resource/datatype）。加选项前先确认代码侧 kind 已定义，否则定义和代码又错位一轮。

3. 顺手核对：**不要**删除/重建任何控件（删了再加会让 custom_id 也断，那才是真的全盘重配）。
4. 保存发布。
5. **立刻**重跑 `delivery approval widgets --code 301E99EB-A4BC-4F08-AFAF-46906A006C08 --write <approval.json>`，比对 diff：
   - 15 个 custom_id 一个不少；
   - 哪些 widget id 变了（官方不保证不变）；
   - `kind` 控件 type 仍是 `radioV2`。
6. 重启/重载面板使新配置生效后，**先发一张测试单验证**：`kind=storage` 能发出去、审批人看得到「数据目录」、`verify()` 能通过单号核对。
7. 14 张在途单子：第 5 步的 diff 若显示 **widget id 有变化**，说明旧配置已失效 —— 在第 5、6 步之间的窗口里，任何在途单子的 `verify()` 都会被判「申请单号不一致」。**所以第 5 步必须紧跟第 4 步做，窗口越短越好**；最稳是选在没人操作的时间窗，并提前通知审批人先别批。

### 前置 / 后置动作清单

- 前置：备份已在手（`/tmp/approval-backup-1790162106.json`）；再**额外截图**后台的审批流程节点（审批人是谁、有没有条件分支）—— 备份里没有这部分，出事时截图才是唯一复原依据。
- 前置：确认 `service` kind 在 `catalog.py` 已落地（或明确这次只加三个）。
- 后置：`widgets --write` + diff + 测试单（上面 5、6 步）。
- 后置：在 `notes.md` 记一行改动时间，方便和 14 张在途单的异常时间点对齐。

### 回滚

- **改动是「加选项」，回滚就是「删掉新加的那几个选项」**，在后台原地操作即可，不需要备份。
- 备份 JSON 的真实用途是**核对**（改完再 GET 一次，和备份 diff，确认只有 `kind.option` 多了四条、15 个 id/custom_id 没动），**不是复原**：如前所述，它喂不回创建接口，复原不了审批人和分支。
- 如果 diff 显示有非预期变化（控件被删、id 大面积漂移），立刻在后台用**版本管理**（编辑页左上角版本信息，2025.06 起支持）切回上一个历史版本，这是真正的回滚路径。

### 不推荐的做法（写下来防止以后有人再捡起来）

`POST /approval/v4/approvals` 带上 `approval_code` 全量覆盖。**技术上做得到**（文档明说传 code 即更新），但要自己把审批人、条件分支、控件的后台专属属性全部重写一遍，其中审批人**备份里没有**；一旦写成空审批人 + `empty_auto_pass`，面板的唯一门禁失效且无声；且 API 定义后台删不掉、停不了。收益（省几次点击）和风险（拆掉生产门禁、废掉 14 张活单）完全不成比例。

---

## 出处一览

- 创建审批定义（含 `approval_code` 全量更新、API 定义不可删、不推荐自建应用使用、不支持条件分支、必填字段、请求示例、权限 scope）
  https://open.feishu.cn/document/server-docs/approval-v4/approval/create?lang=zh-CN ／ https://open.larksuite.com/document/server-docs/approval-v4/approval/create
- 查看指定审批定义（响应 schema：`form`/`node_list`/`viewers`/`form_widget_relation`，无 approver）
  https://open.feishu.cn/document/server-docs/approval-v4/approval/get?lang=zh-CN
- 审批概述（审批定义分组下只有 create + get）
  https://open.feishu.cn/document/server-docs/approval-v4/approval-overview?lang=zh-CN
- 原生审批接入指南（devMode、默认 ID 不保证不变、custom_id 保证不变、实例可用 custom_id 顶替 id）
  https://feishu.apifox.cn/doc-1974108
- 获取单个审批实例详情（实例 form 含 custom_id；未设置则不返回）
  https://open.feishu.cn/document/server-docs/approval-v4/instance/get?lang=zh-CN
- 审批定义表单控件参数（控件类型清单含 radioV2/checkboxV2/dateInterval/fieldList；id 不可重复、custom_id 可选）
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/approval-v4/approval/approval-definition-form-control-parameters
- 飞书审批功能更新日志（2025.06 审批编辑支持版本管理）
  https://www.feishu.cn/hc/zh-CN/articles/360049067392
- 管理员设置审批修改规则（流程中的审批不支持修改）
  https://www.feishu.cn/hc/zh-CN/articles/230164856321
