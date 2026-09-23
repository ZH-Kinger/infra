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

---
---

# 第二轮：API 建的定义为什么在审批后台列表里看不到？

> 调研日期 2026-09-23 / researcher。**全程只读**：只查了官方文档、下载并解包了官方 SDK `lark-oapi 1.7.3` 读它的请求体 model、抓了一份帮助中心 HTML 解析正文。**没有调用任何飞书接口（读接口也没调），没有创建/修改/覆盖任何审批定义。**

## 结论先行

| 问题 | 结论 | 可信度 |
|---|---|---|
| 「没设分组 → 后台列表看不见」这个假设 | **证伪**。原生「创建审批定义」接口**根本没有分组字段**（不是漏传，是不存在）；而「分组」管的是**审批发起页**的分区，不是后台列表 | 【文档】+【SDK 实测】 |
| `form_widget_relation` 里的 `groups` 是不是「审批分组」 | **不是**。官方对它的定义是「组件之间值关联关系」，即控件联动/选项关联。与审批分组无关 | 【文档】 |
| 能不能用接口给原生定义设分组 | **不能**。`group_code`/`group_name` 只存在于**三方审批定义** `POST /approval/v4/external_approvals`，原生定义没有对应参数 | 【文档】+【SDK 实测】 |
| 有没有别的接口能给已存在的定义挪分组 | **没有**。approval v4 的「审批定义」资源下总共只有 4 个端点：create / get / subscribe / unsubscribe | 【SDK 实测】+【文档】 |
| 那真正原因是什么 | **极可能是「这条定义没有任何流程管理员」**。后台能看到并管理哪些审批，由**管理员权限范围**决定；`process_manager_ids`（= 审批流程管理员）不传 = 谁都不是这条定义的流程管理员 | 【文档】，**待一次只读 GET 坐实** |
| 怎么让它出现在后台 | **创建时传 `process_manager_ids`**（覆盖重建时补上）。这是接口侧唯一能影响「谁能在后台管这条定义」的字段 | 【文档】+【推测：能否因此出现在列表未经实证】 |
| `icon` 不传 | 默认 0，只影响图标长相，**与列表可见性无关** | 【文档】 |

---

## ① 「分组」假设：证伪

### 1.1 原生创建接口的请求体，一个字段不漏

我把官方 Python SDK `lark-oapi 1.7.3` 下载解包（只读，装在 scratchpad，没进项目），读 `lark_oapi/api/approval/v4/model/approval_create.py` 的字段声明 —— SDK 的 model 是按官方 API spec 生成的，比网页渲染更不容易漏：

```
ApprovalCreate 的全部字段（11 个）：
  approval_name / approval_code / description / viewers / form /
  node_list / settings / config / icon / i18n_resources / process_manager_ids
```

**没有 group / group_code / group_name / category / folder / 任何近似字段。**（`help_url` 在 `config` 里，不是顶层。）

查询参数（`create_approval_request.py`）只有两个：`user_id_type`、`department_id_type`。

→ 你查到的入参清单是全的，**不是你漏看了**。【SDK 实测】

### 1.2 分组只属于「三方审批定义」

同一个 SDK，`external_approval.py`（`POST /approval/v4/external_approvals` 的请求体）：

```
approval_name / approval_code / group_code / group_name / description /
external / viewers / managers
```

官方对这两个字段的原文【文档】：

> `group_code`：审批定义所属审批分组，用户自定义。**如果传入的 group_code 当前不存在，则会新建审批分组**
> `group_name`：审批分组名称，**审批发起页**的审批定义分组名称来自该字段

注意最后五个字：**审批发起页**。分组管的是发起页的分区渲染 —— 而发起页**你本来就看得到**。这条假设自己把自己否掉了：如果缺分组会让发起页渲染不出来，你那两条新定义在「发起审批」列表里也该消失，但实测是能发起的。

三方审批定义概述页同样把分组归在「三方审批接入飞书**审批中心**后，在审批中心内的名称、分组、可见范围等基础信息」里。【文档】

### 1.3 `form_widget_relation.groups` 不是它

「查看指定审批定义」对该字段的原文描述是【文档】：

> `form_widget_relation`：**组件之间值关联关系**

即飞书帮助中心说的「选项关联 / 控件联动」（选了 A 控件某项，联动限制 B 控件的可选范围）。它的 `groups` 是**联动规则分组**，和审批分组没有任何关系。

而且它**是创建接口可以传的**：SDK 的 `ApprovalForm` 有两个字段 `form_content` 和 `widget_relation`。所以老定义 `{"groups":[]}` / 新定义 `null` 这个差异的解释是：**老那条被人在后台图形化编辑器里保存过**（编辑器会写出一个空的联动规则容器），纯 API 建的不写这个字段就是 null。**它是「被后台编辑过」的痕迹，不是「可见性」的原因** —— 因果方向反了。【文档】+【推测：后台保存会写出空容器，未实证】

---

## ② 真正的解释：**流程管理员 / 权限范围**（你的新假设，文档层面站得住）

### 2.1 `process_manager_ids` 就是「审批流程管理员」

创建接口原文【文档】：

> `process_manager_ids`：**审批流程管理员**的用户 ID 列表。**ID 类型与查询参数 `user_id_type` 取值一致**，列表最大长度为 200

- 类型：string 数组，**选填**；
- id 形态由 query 参数 `user_id_type` 决定，**默认 `open_id`** —— 也就是说你不显式传 `user_id_type` 时，数组里要放 `ou_xxx` 的 open_id；
- 不传 = 不设置任何流程管理员。文档没有一句「不传会自动把创建者/应用设为管理员」。

### 2.2 后台能看到/管到哪些审批，由权限范围决定（官方表格）

帮助中心《管理员设置审批管理员》原文表格（我抓 HTML 解出来的正文，逐字）【文档】：

> 在飞书审批中，审批管理员可以通过审批管理后台对企业中的审批进行管理，包括表单设计、流程设计、数据查看等。审批管理员分为以下 3 类：

| 类型 | 说明 | **权限范围** | 操作权限 |
|---|---|---|---|
| **审批应用管理员** | 飞书管理后台中设置的：超级管理员 / 权限范围包括「审批」的管理员 | **全部审批** | 创建新审批；编辑/停用/删除审批（全部）；查看/导出数据（全部）；增删流程管理员（全部）；增删子管理员 |
| **审批子管理员** | 审批管理后台中设置的子管理员 | **全部审批 或 限制范围（仅自己作为负责人的审批）** | 创建新审批；编辑/停用/删除（全部或限定）；查看/导出（限定）；增删流程管理员（全部或限定）。不可增删子管理员 |
| **审批流程管理员** | 审批管理后台中，**针对单个审批**所设置的流程管理员 | **单个审批** | 编辑/停用/删除审批（限定）；查看/导出数据（限定）；增删流程管理员（限定）。不可创建新审批、不可增删子管理员 |

> 设置审批流程管理员：进入 **审批管理后台 > 审批管理** 页面 …… 在 **基础信息** 页面，找到 **流程管理员** 设置项

出处：https://www.feishu.cn/hc/zh-CN/articles/360035662614

**这张表直接解释了三个现象**：

1. 用户如果不是「审批应用管理员（权限范围=全部审批）」，而是**子管理员（限制范围）或只是某些审批的流程管理员**，那他在后台看到的就只有「自己有份」的那些审批；
2. 老定义 `301E99EB` 他能看见、能编辑、能改审批人 → 他是**那一条**的流程管理员（或那条在他的负责范围内）；
3. 新建的两条没有任何流程管理员 → 不在他的范围内 → **列表里没有，直开编辑页 `noPermission`**。

`noPermission` 这个结果尤其关键：**它是权限判定，不是渲染缺失**。分组缺失不可能产出 `noPermission`（顶多是不分区/落到默认区）。你的判断是对的，**分组那条线可以放下了**。

### 2.3 一个必须一起排掉的替代解释

上表同时说明：**如果用户本人是超级管理员 / 审批应用管理员，权限范围是「全部审批」，那他应该看得见所有审批（包括 API 建的）**。所以在下结论前请确认一句：

> 用户在 **飞书管理后台 > 企业设置 > 管理员权限** 里是不是超级管理员、或有没有含「审批」的管理员角色？

- 如果**是**超级管理员却仍然 `noPermission` → 「没有流程管理员」解释不了，得换方向（那时更可能是「API 建的定义在后台被整体排除在管理面之外」，见 ③）；
- 如果**不是**（只是子管理员/流程管理员）→ 流程管理员假设基本坐实，补 `process_manager_ids` 大概率能解决。

---

## ③ 决定性的只读验证（**一条 GET 就能判**，我没替你跑）

「查看指定审批定义」有个默认关闭的开关【文档】：

> `approval_admin_ids`：**有数据管理权限的审批流程管理员的 open_id**，由参数 `with_admin_id` 控制是否返回（默认 `false`）

```
GET /open-apis/approval/v4/approvals/301E99EB-A4BC-4F08-AFAF-46906A006C08?with_admin_id=true
GET /open-apis/approval/v4/approvals/98039779-906B-493F-946A-B96E685FB640?with_admin_id=true
```

判读：

- 老那条返回**非空** `approval_admin_ids`（里面应该有用户自己的 open_id）、新那条**空/缺** → **假设坐实**，直接补 `process_manager_ids` 重建；
- 两条**都空** → 老那条的可见性来自别处（用户是子管理员且那条在他负责范围内、或他其实是应用管理员），补 `process_manager_ids` 不一定管用 —— 那就别赌，见 ④ 的兜底路径。

> 顺带修正上一轮的一个细节：你那份 `/tmp/approval-backup-*.json` 只有 6 个键、没有 `approval_admin_ids`，**不是因为老定义没有管理员**，而是因为当时没传 `with_admin_id=true`。那份备份对这个问题**不能作证**。

---

## ④ 回答第二轮提的 5 个问题

### Q1 管理员字段叫什么、什么形状

`process_manager_ids`，顶层字段，string 数组，最大 200；id 类型跟随 query 参数 `user_id_type`（默认 `open_id`）。建议**显式**带上 `?user_id_type=open_id`，别吃默认值 —— 这类默认值改起来你不会收到通知。

```
POST /open-apis/approval/v4/approvals?user_id_type=open_id
{
  ...,
  "process_manager_ids": ["ou_xxxxxxxx"]      // 用户本人的 open_id；可多个
}
```

### Q2 不传的后果，官方有没有明说

**没有明说**。文档只说这个字段是选填、是「审批流程管理员的用户 ID 列表」，**没有**任何一句「不传则某某人自动成为管理员」，也**没有**一句「不传则后台不可见」。

「不传 = 这条定义没有流程管理员」是字面推论【文档】；「没有流程管理员 ⇒ 非应用管理员看不见也进不去」是把上面那张权限范围表接上去的推论【推测，但与你观察到的三个现象全部吻合】。

### Q3 后台列表是不是只显示「我是管理员」的那些

**按权限范围显示**，不完全等于「我是管理员的那些」：

- 审批应用管理员（超管/带审批权限的角色）：**全部审批**；
- 子管理员：全部 或 仅自己作为负责人的；
- 流程管理员：仅被指定的单个审批。

所以准确表述是：**列表是按当前操作者的权限范围过滤的**。对非应用管理员来说，效果就是「只看得到我有份的」。【文档】

### Q4 覆盖重建时该一起补的

| 字段 | 要不要补 | 说明 |
|---|---|---|
| `process_manager_ids` | **必补** | 本轮的核心修复；放用户本人 open_id（建议把值班的第二个人也加上，避免单点） |
| `user_id_type=open_id` | **必带**（query） | 决定上面那个数组的 id 形态 |
| `icon` | 可补 | 枚举 0~24，**默认 0**。原文：「审批图标枚举，默认为 0」。**只影响图标，与列表可见性无关** —— 不传不会导致看不见 |
| `kind` 控件 | 必补 | 见下 |
| `summary` 控件 | 必补 | 见下 |
| `form.widget_relation` | 不用 | 「组件之间值关联关系」，你没有控件联动需求就别传 |
| `config.can_update_form/process/viewer/revert` | 建议 `true` | 语义是「**允许在后台修改**表单/流程/可见范围/撤回设置」。你已经试过它不能解决可见性，但一旦可见性修好，它决定后台还能不能改 —— 保持 true |
| `description` / `settings` | 按原样给 | 全量覆盖，不给就没了 |

**「还有没有别的不传就没法管理/没法显示的字段」**：把 11 个字段过了一遍，**没有第二个**。`viewers` 管的是「谁能发起/看到发起入口」（你已实测正常）；`config` 管「后台能改什么」；`icon` 管图标；其余都是内容。**唯一与「谁能在后台管这条定义」相关的，就是 `process_manager_ids`。**【SDK 实测 + 文档】

#### `kind` / `summary` 控件形状

控件 JSON 的可用 type【文档】：`input` / `textarea` / `text`(纯说明) / `number` / `amount` / `telephone` / `date` / `dateInterval` / `radioV2` / `checkboxV2` / `contact` / `address` / `image` / `attachmentV2` / `connect` / `fieldList`。

面板侧的约束（`src/delivery/approval.py`）：

- `WIDGET_TEXT = ("input", "textarea")`、`WIDGET_PICK = ("radioV2", "radio")`；
- `_DEFAULT_TYPES = {"summary": "textarea", "reason": "textarea"}` —— **`summary` 走 `textarea`，配置里不写 type 也是这个默认**；
- `kind` 没有默认映射 → 落到 `"input"`。

所以最省事、与面板现状**完全对齐**的形状就是：

```json
{"id":"kind",   "type":"input",    "required":true, "name":"@i18n@w_kind"}
{"id":"summary","type":"textarea", "required":true, "name":"@i18n@w_summary"}
```

（`form_content` 里的 `id` 用可读串 = 它就是稳定的 custom_id；`name` 必须 `@i18n@` 开头，中文放 `i18n_resources`。）

**`kind` 要不要做成只有一个选项的 `radioV2`？——不建议。**

- 面板 `_field()` 对 `radioV2` 和 `input` 的 value 形状处理不同（`WIDGET_PICK` 那条分支），做成单选就得同步配置里的 `type`，多一处能漂的东西；
- 单选只有一个选项对填单人没有任何约束价值，反而在后台编辑时更难加值；
- `kind` 是**面板自己塞进去的**机器字段（`service`），不是人填的 —— 用 `input` + 建议加 `"required":false` 或直接 `required:true` 由面板写死值即可。

> 如果希望人工发起时也不误填，正解是用 `"type":"text"`（纯说明控件）另加一行提示，而不是把 `kind` 改成单选。

### Q5 覆盖时这些字段要不要全量给

**要。** 创建接口原文【文档】：

> 该参数传入指定审批定义 Code 时，表示调用该接口更新该审批定义内容，**更新方式为覆盖原定义内容的全量更新**。

「全量覆盖」= **你没传的就没了**。具体到这次：

- 不传 `process_manager_ids` → 管理员清空（这正是你现在的状态）；
- 不传 `icon` → 回落默认 0；
- 不传 `description` / `settings` / `config` → 按未设置处理；
- `approval_name` / `viewers` / `form` / `node_list` / `i18n_resources` 是**必填**，漏了直接参数错误。

另外两条覆盖时必须心里有数的（上一轮已详述，这里只列结论）：

- **widget id 会漂**：官方只保证 `custom_id` 不变，不保证默认 id 不变。覆盖后**必须重跑 `delivery approval widgets --write`**，否则面板会拿旧 id 取单号 → 所有审批被判「申请单号不一致」；
- **API 建的定义，后台和 API 都停用不了、删不掉**（官方原文）。覆盖失败你没有「删了重来」这条退路，只能再覆盖一次。

---

## ⑤ 那到底「有没有办法让 API 建的定义出现在后台列表里」

给一个分层的答复，按确定性从高到低：

1. **【文档】接口层面唯一能试的就是 `process_manager_ids`。** 它是 11 个字段里唯一与「谁能在后台管这条定义」挂钩的，语义（审批流程管理员）与后台权限范围表严丝合缝。建议**先跑 ③ 那条 GET 坐实，再覆盖重建**。
2. **【文档】「分组」这条路不存在**，别再找了：原生创建接口没有该字段，也没有任何 update/patch/分组端点（approval v4 定义资源只有 create/get/subscribe/unsubscribe）。
3. **【文档】官方从未承诺 API 建的定义会出现在后台列表里。** 官方对这类定义的唯一明确表述是两条**负面**的：
   > 通过该 API 创建的审批定义，**无法从审批管理后台或以 API 方式停用、删除**，请谨慎调用。
   > **不推荐企业自建应用使用该 API 创建审批定义**，如有需要，尽量联系企业管理员在审批管理后台创建定义。

   这两句**不构成**「后台根本看不到」的明说 —— 「无法从后台停用、删除」在字面上更像是「看得到但这两个操作不给你」。但它确实表明：**后台对 API 建的定义有专门的、被削弱的管理面**，所以即使补了流程管理员，**也不保证 `停用/删除` 按钮会出现**（编辑大概率可以，因为老那条就是被编辑过的）。
4. **【推测】补了 `process_manager_ids` 之后仍然看不见**，是有可能的。真到那一步，**兜底路径只有一条**：**让企业管理员在审批管理后台手工建这条定义**（官方推荐路径），面板只消费它的 `approval_code`；代价是表单/流程改动要人工同步，收益是它从此是一条「正常」的后台定义，可编辑、可停用、可删、有版本管理。

**一句话给用户**：分组不是原因，别等分组；原因大概率是「这条定义没有流程管理员」，补 `process_manager_ids` 重建一次即可；如果补了还看不见，就改走「请管理员在后台手工建」这条官方推荐路径，不要继续在 API 上试。

---

## ⑥ 残余不确定（明确列出来，别当已证实）

| 项 | 状态 |
|---|---|
| 补了 `process_manager_ids` 后，定义是否真的出现在后台列表 | **未实证**。文档无明说，只有权限范围表的推论 |
| 用户当前到底是哪一类管理员 | **未确认**，决定 ②2.3 的分支。请在 飞书管理后台 > 企业设置 > 管理员权限 看一眼 |
| 后台保存会写出 `form_widget_relation={"groups":[]}` | 【推测】。是对老/新定义差异的最合理解释，但没实证 |
| 即使可见，`停用/删除` 按钮是否可用 | **官方明说不可用**（对 API 建的定义） |
| 覆盖后 widget id 会不会漂 | 官方只保证 custom_id 不变 → **按会漂处理**，覆盖完必须重跑 widgets |

---

## 第二轮出处一览

- 创建审批定义（`process_manager_ids` 原文、`icon` 默认 0、query 参数 `user_id_type`/`department_id_type`、全量覆盖、API 定义不可停用删除、不推荐自建应用使用）
  https://open.feishu.cn/document/server-docs/approval-v4/approval/create?lang=zh-CN
- 查看指定审批定义（`approval_admin_ids` = 审批流程管理员 open_id，由 `with_admin_id` 控制返回，默认 false；`form_widget_relation` = 组件之间值关联关系）
  https://open.feishu.cn/document/server-docs/approval-v4/approval/get?lang=zh-CN
- 创建三方审批定义（`group_code`/`group_name` 原文：不存在则新建分组；发起页分组名来自 group_name）
  https://open.feishu.cn/document/server-docs/approval-v4/external_approval/create?lang=zh-CN
- 三方审批定义概述（分组/名称/可见范围属于「审批中心」基础信息）
  https://open.feishu.cn/document/server-docs/approval-v4/external_approval/overview?lang=zh-CN
- 审批概述（v4 端点全集；审批定义下只有 create + get + subscribe + unsubscribe）
  https://open.feishu.cn/document/server-docs/approval-v4/approval-overview?lang=zh-CN
- **管理员设置审批管理员**（3 类管理员与权限范围表；流程管理员在「审批管理 > 基础信息」设置）
  https://www.feishu.cn/hc/zh-CN/articles/360035662614
- 管理员设置审批选项关联（控件联动 = form_widget_relation 的业务含义）
  https://www.feishu.cn/hc/zh-CN/articles/270021758316
- 审批定义表单控件参数（可用 type 清单）
  https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/approval-v4/approval/approval-definition-form-control-parameters
- 官方 SDK `lark-oapi` 1.7.3（请求体 model 逐字段：`ApprovalCreate` 11 字段无分组；`ExternalApproval` 有 group_code/group_name；approval 资源只有 create/get/subscribe/unsubscribe）
  https://pypi.org/project/lark-oapi/
