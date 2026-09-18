# Authentik 用户属性写入 API — 规格核实

调研人：researcher　日期：2026-09-16　对象：IT 侧 Authentik（证书 issuer `CN = authentik 2025.6.4`）
用途：向 IT 提出「具体到接口和权限项」的接口申请。**本次全程只读**：所有结论来自 authentik 官方文档、GitHub 上 `version-2025.6` / `version-2026.8` 分支源码、GitHub Security Advisory，以及 Django/DRF 上游源码。**没有访问 `iam.wuji-tech.com`，没有调用任何真实 IAM 接口。**

可信度标记：`【文档】`官方文档明说并附原文；`【源码】`读 authentik/Django/DRF 源码得出，附文件+行号+分支；`【推理】`由源码/文档推导、未逐字写明；`【拿不准】`需另行取证。

---

## 0. 主证据清单

| # | 内容 | 出处 |
|---|------|------|
| S1 | `authentik/core/api/users.py`（UsersFilter / UserSerializer / UserViewSet） | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/core/api/users.py |
| S2 | `authentik/core/models.py`（User/Token/ExpiringModel/AttributesMixin） | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/core/models.py |
| S3 | `authentik/core/api/tokens.py`（TokenSerializer/TokenViewSet） | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/core/api/tokens.py |
| S4 | `authentik/rbac/permissions.py` + `authentik/rbac/filters.py` | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/rbac/permissions.py |
| S5 | `authentik/events/middleware.py` + `authentik/events/utils.py` + `authentik/events/api/events.py` | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/events/middleware.py |
| S6 | `authentik/root/settings.py`（REST_FRAMEWORK / 限流） | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/root/settings.py |
| S7 | `authentik/api/authentication.py`（Bearer 解析） | https://github.com/goauthentik/authentik/blob/version-2025.6/authentik/api/authentication.py |
| D1 | API 认证方式（Bearer） | https://api.goauthentik.io/authentication |
| D2 | Service accounts / API token 发放与吊销 | https://docs.goauthentik.io/sys-mgmt/service-accounts/ |
| D3 | 权限模型（全局 vs 对象级） | https://docs.goauthentik.io/docs/users-sources/access-control/permissions |
| D4 | 系统设置（default token duration / event retention） | https://docs.goauthentik.io/docs/sys-mgmt/settings |
| D5 | 用户属性参考（保留属性键） | https://docs.goauthentik.io/docs/users-sources/user/user_ref |
| V1 | CVE-2026-40172 / GHSA-h6x7-hjjc-wjc9「Privilege Escalation via User PATCH」 | https://github.com/goauthentik/authentik/security/advisories/GHSA-h6x7-hjjc-wjc9 |
| V2 | GHSA-h6c5-mpvq-j4jc「Privilege escalation via delegated group and user management」 | https://github.com/goauthentik/authentik/security/advisories/GHSA-h6c5-mpvq-j4jc |

---

## 1. 版本坐标：2025.6.4 是什么位置

- 2025.6 线最后一个补丁版就是 **2025.6.4**（GitHub tag 列表里 `version/2025.6.0 … 2025.6.4` 之后直接进入 2025.8.0）。【源码/tags】
- 现行版本线：2025.8 → 2025.10 → 2025.12 → **2026.2 / 2026.5 / 2026.8**，文档站当前版本 2026.8，最新补丁 tag `version/2026.8.2`。【文档】https://docs.goauthentik.io/docs/releases
- 2025.12 起「access control is fully role-based」：权限必须挂在 Role 上，`Group.parent` 改成多父 `Group.parents`，用户与 Permission 的直连关系保留但不再使用。【文档】https://docs.goauthentik.io/docs/releases/2025.12

**与本需求相关的 2025.6 → 2026.x 差异**（都已逐条核过源码）：

| 点 | 2025.6.4 | 2026.8 |
|---|---|---|
| `?attributes=` JSON 过滤 | 有，行为一致 | 有，行为逐字一致 |
| `search_fields` 是否含 `attributes` | **含**（可用 `?search=` 兜底搜属性值） | **不含**（`["email","name","uuid","username"]`），兜底方案失效 |
| User 序列化器是否拦「把用户加进 superuser 组」 | **不拦**（只有 `validate_path`/`validate_type`/`validate`） | 拦（`validate_groups` 要求 `authentik_core.enable_group_superuser`；`validate_roles` 要求 `authentik_rbac.change_role`） |
| User 上有无 `last_updated` 字段 | 无 | 有（可做乐观并发检测） |
| 用户与角色 | 只有 groups | 多了 `roles` / `roles_obj` |
| 限流 | 仅 `AnonRateThrottle` | 仅 `LocalAnonRateThrottle`，策略同 |

---

## 2. Q1：按 attributes 里的字段查用户

### 结论：支持，且是官方 filter，不需要全量拉回来自己筛。【源码】

`UsersFilter` 里有一个名为 `attributes` 的 CharFilter，方法 `filter_attributes`，原文（`version-2025.6` 与 `version-2026.8` 逐字相同）：

```python
def filter_attributes(self, queryset, name, value):
    """Filter attributes by query args"""
    try:
        value = loads(value)
    except ValueError:
        raise ValidationError(detail="filter: failed to parse JSON") from None
    if not isinstance(value, dict):
        raise ValidationError(detail="filter: value must be key:value mapping")
    qs = {}
    for key, _value in value.items():
        qs[f"attributes__{key}"] = _value
    try:
        _ = len(queryset.filter(**qs))
        return queryset.filter(**qs)
    except ValueError:
        return queryset
```

即：query 参数值是一段 **JSON 对象**，每个 key 拼成 Django 的 `attributes__<key>` 查询。

**可用请求（顶层键，我方主用法）**：

```
GET /api/v3/core/users/?attributes=%7B%22feishu_union_id%22%3A%22on_xxxxxxxx%22%7D&include_groups=false
Authorization: Bearer <token>
```

未编码形态：`?attributes={"feishu_union_id":"on_xxxxxxxx"}&include_groups=false`

返回体是 authentik 自己的分页格式：`{"pagination": {...}, "results": [ {...user...} ]}`，分页参数 `page` / `page_size`。【源码】`authentik/api/pagination.py`

`include_groups=false` 是 2025.6 就有的参数，跳过 groups 序列化，减小响应。【源码】S1 `UserSerializer._should_include_groups`

**嵌套键过滤**（例如直接按 `cloud_accounts.aliyun-main` 反查）：写成 `?attributes={"cloud_accounts__aliyun-main":"wangzihan@...onaliyun.com"}`。Django 的 `JSONField.get_transform` 对任意名字返回 `KeyTransformFactory`，所以 `attributes__cloud_accounts__aliyun-main` 会被解析成两级 key transform；Python 也允许非标识符字符串作为 `**kwargs` 键（本地实测 `f(**{"attributes__cloud_accounts__aliyun-main": "x"})` 正常）。【推理】（源码链路读通 + 本地 Python 验证，但未在真 authentik 上跑）

注意两个坑：
- 若传 `{"cloud_accounts": {"aliyun-main": "..."}}`，语义是**整个子对象精确相等**（`attributes__cloud_accounts = {...}` 的 JSON exact），不是包含匹配。【推理】
- key 里如果自带 `__`，会被 Django 当成分隔符，无法表达。我方的键名 `feishu_union_id` / `cloud_accounts` / `aliyun-main` 都安全。

**兜底方案（只在 2025.6 可用）**：`GET /api/v3/core/users/?search=on_xxxxxxxx`。2025.6 的 `search_fields` 含 `"attributes"`，DRF 的 SearchFilter 会生成 `attributes__icontains`，Django 的 `JSONField` 注册过 `JSONIContains` 会转成文本 ILIKE。【源码】S1 + Django 5.1 `django/db/models/fields/json.py:349`。但这是**子串模糊匹配**，可能命中别的字段，只适合人工排查，不适合程序判定；且 IT 升级到 2026.x 后该字段被移出 search_fields，会静默失效。所以正式实现只用 `?attributes=`。

**是否需要 IT 额外给稳定标识**：不需要。只要 `feishu_union_id` 确实写在每个用户的 attributes 顶层且唯一，`?attributes=` 就够。要向 IT 确认的只有两点：① 属性键的准确拼写与层级（是否真在顶层，而不是 `attributes.feishu.union_id` 之类）；② 是否保证唯一（我方实现会在命中 0 条或 >1 条时拒绝写入并告警）。

---

## 3. Q2：PATCH 传 attributes 是替换还是合并 —— 关键结论

### 结论：整体替换。只传 `{"attributes": {"cloud_accounts": {...}}}` 会把该用户 attributes 里的其它键（工号、飞书 id 等）全部清掉。【源码，高置信】

证据链三段：

1. 序列化器里 `attributes` 是一个普通 DRF JSON 字段，没有任何自定义 `update`/`to_internal_value`：
   `attributes = JSONDictField(required=False)`（S1），而 `JSONDictField` 的全部内容是
   ```python
   class JSONDictField(JSONField):
       """JSON Field which only allows dictionaries"""
       default_validators = [is_dict]
   ```
   （`authentik/core/api/utils.py`）
2. `UserSerializer.update()` 只额外处理 blueprint 场景的 `password`/`permissions`，其余交给 `super().update()`；DRF 3.16 的 `ModelSerializer.update()` 原文是
   ```python
   for attr, value in validated_data.items():
       if attr in info.relations and info.relations[attr].to_many:
           m2m_fields.append((attr, value))
       else:
           setattr(instance, attr, value)
   instance.save()
   ```
   （https://github.com/encode/django-rest-framework/blob/3.16.0/rest_framework/serializers.py#L1024-L1044）——对 `attributes` 就是一次整体赋值。
   （authentik 2025.6 固定的是 DRF 3.16 的一个 fork，fork 相对上游只 revert 了一个 unique-together 优化提交，序列化语义与上游一致。【源码】`pyproject.toml` + fork commit `896722ba`）
3. **反证最有力**：authentik 自己有一套「正确合并 attributes」的实现，但**只给同步路径用，没给 API 用**：
   ```python
   def update_attributes(self, properties: dict[str, Any]):
       """Update fields and attributes, but correctly by merging dicts"""
       ...
       MERGE_LIST_UNIQUE.merge(final_attributes, self.attributes)
       MERGE_LIST_UNIQUE.merge(final_attributes, properties.get("attributes", {}))
   ```
   （S2 `AttributesMixin.update_attributes`，`MERGE_LIST_UNIQUE` 是 `deepmerge.Merger`，见 `authentik/lib/merge.py`）。调用方是 LDAP 等 source 同步（`User.update_or_create_attributes`）。REST API 不走这条路。

### 由此得到的安全写法

必须 **GET → 本地合并 → PATCH 整个 attributes**：

```
1) GET  /api/v3/core/users/?attributes={"feishu_union_id":"on_xxx"}&include_groups=false
        取到 pk 与当前 attributes（要求命中且仅命中 1 条，否则中止）
2) 本地深拷贝 attributes，只改 attributes["cloud_accounts"]["aliyun-main"] = "..."
3) PATCH /api/v3/core/users/<pk>/
        {"attributes": <合并后的完整 attributes 对象>}
4) GET 回读校验：cloud_accounts 正确，且步骤 1 读到的其它顶层键一个不少
```

配套硬规矩：

- **只用 PATCH，绝不用 PUT**。`groups` 字段定义为 `PrimaryKeyRelatedField(..., default=list)`：PATCH（`partial=True`）时 DRF 对缺省字段直接 `SkipField`，不套用 default（`rest_framework/fields.py::validate_empty_values`）；但 PUT 会把 default 生效，**一次不带 groups 的 PUT 会清空该用户的全部组关系**。【源码+推理】
- **写入前必须对 attributes 做形状检查**：GET 回来的不是 dict、或缺少预期的既有键（如 `feishu_union_id` 本身），一律中止不写。宁可漏写也不能拿一个残缺对象去覆盖。
- **并发覆盖风险真实存在，且服务端不提供任何保护**：`/api/v3/core/users/` 没有 ETag / If-Match 条件请求（全仓 ETag 只出现在 SCIM 等模块，core users API 无）。GET 与 PATCH 之间如果有 IT 侧同步、管理员手改、或我方另一进程写入，后写的整体覆盖先写的。【推理】
  缓解：① 我方侧对同一 `pk` 加互斥锁，串行化写入；② 写入后立即回读比对，不一致就告警而不是重试覆盖；③ 尽量把写入时机压到审批通过后的一次性动作，不做周期性全量刷写；④ 若 IT 将来升级到 2026.x，可用响应里的 `last_updated` 做乐观并发（GET 时记下，PATCH 后回读比对）。
- **写入只碰 `cloud_accounts` 这一个顶层键**，代码层面做白名单，任何其它键原样回写。
- 别去动 `goauthentik.io/user/*` 命名空间的保留属性（`can-change-username`、`token-expires`、`token-maximum-lifetime` 等）。【文档】D5

### 顺带一条好消息

IT 侧若用 source 同步 / `user_write` stage 维护用户，它们写属性走的是深合并或按路径写单键（`set_path_in_dict`），不会因为一次同步把我方写的 `cloud_accounts` 抹掉——除非 IT 的 property mapping 本身就产出 `cloud_accounts` 这个键。【源码】S2 + `authentik/stages/user_write/stage.py::write_attribute`。这点仍建议向 IT 口头确认一句。

---

## 4. Q3：认证方式（token 怎么发、绑谁、多久过期、怎么吊销）

- **Header 形式**：`Authorization: Bearer <token>`。【文档】D1 原文：“For any of the token-based methods, set the `Authorization` header to `Bearer <token>`.” 源码侧 `validate_auth()` 只接受 `Bearer` 前缀，其它类型抛 `AuthenticationFailed`。【源码】S7
- **API base**：`https://<authentik>/api/v3/`，每个实例自带 API 浏览器 `https://<authentik>/api/v3/`。【文档】https://api.goauthentik.io/
- **token 绑定到「用户」**，不是绑应用。推荐做法是先建一个 **service account**（Directory > Users > New User > Service Account，用户 `type=service_account`，`path=goauthentik.io/service-accounts`，密码设为不可用），再为它建 token。【文档】D2 +【源码】S1 `service_account` action
- **发放步骤**（IT 侧 UI）：Directory > Tokens and App passwords > Create > 填唯一 `Identifier` > `User` 选该 service account > `Intent` 选 **API Token** > Create。【文档】D2
- **过期时间**：
  - intent=`api` 的 token，**`expires` 不能由请求指定**，服务端强制覆盖：`elif attrs.get("intent") == TokenIntents.INTENT_API: attrs["expires"] = default_token_duration()`。【源码】S3
  - `default_token_duration()` 取租户设置 `default_token_duration`，2025.6 代码默认值 `DEFAULT_TOKEN_DURATION = "days=1"`；文档站（2026.8）写的是 `minutes=30`。两者都很短，**实际值以 IT 的 System > Settings 为准，必须问**。【源码】S2 + 【文档】D4
  - 真正决定「长期有效」的是 `expiring` 布尔字段：`is_expired` 里 `if not self.expiring: return False`。【源码】S2 `ExpiringModel`
  - 过期后的表现：认证路径 `filter_not_expired()` 会把过期且 expiring 的记录直接 `delete()`；后台清理任务 `clean_expired_models` 则对 intent=api 的 token 调 `expire_action()` → **重新生成 key 并发 `secret_rotate` 事件**（不是删）。两条路径谁先跑不确定，但对调用方来说结果一样：**旧 token 静默失效，返回 401 `Token invalid/expired`**。【源码】S2 + `authentik/core/tasks.py`
  - 文档里「service account 默认 360 天」指的是建 service account 时附带的那把 **app password**（intent=app_password），不是我们要用的 API token。【文档】D2 +【源码】S1 `service_account` 里 `expires = now() + timedelta(days=360)`、`intent=INTENT_APP_PASSWORD`
- **查看 / 复制 key**：Directory > Tokens and App passwords 的 copy 动作，受 `authentik_core.view_token_key` 权限控制，且每次查看都会记一条 `secret_view` 事件。【文档】D2 +【源码】S3
- **吊销**：删除该 token 即可（UI 删除，或 `DELETE /api/v3/core/tokens/{identifier}/`）。禁用整个 service account（`is_active=false`）是更彻底的一刀。【文档】D2
- 另有一条路：OAuth2 客户端申请 `goauthentik.io/api` scope，用 access token 调 API。【文档】D1。对我方场景（后台服务、无用户交互）不如静态 API token 直接，但如果 IT 更愿意走 OIDC client_credentials，这条可作备选。【推理】

---

## 5. Q4：权限能收窄到什么程度 —— IT 的说法核实

### 结论：IT 说的「粒度是用户级、没有属性级」**属实**，2025.6 与 2026.8 都成立。【源码】

- authentik 的权限就是 Django 的 `<app_label>.<action>_<model>` 加若干自定义权限。User 模型的自定义权限总共 6 个，没有任何一个跟 attributes 有关：
  ```python
  permissions = [
      ("reset_user_password", _("Reset Password")),
      ("impersonate", _("Can impersonate other users")),
      ("assign_user_permissions", _("Can assign permissions to users")),
      ("unassign_user_permissions", _("Can unassign permissions from users")),
      ("preview_user", _("Can preview user data sent to providers")),
      ("view_user_applications", _("View applications the user has access to")),
  ]
  ```
  【源码】S2。**不存在 `can_change_user_attributes` 之类的字段级权限**，2026.8 也没有新增。
- 官方权限文档只描述两种粒度：global permissions（对某类型的所有对象）与 object permissions（对单个具体对象），通篇没有字段/属性级。【文档】D3

### 我方需要的最小权限组合

`PATCH /api/v3/core/users/<pk>/` 的实际判定链（`DEFAULT_PERMISSION_CLASSES = ObjectPermissions`，`DEFAULT_FILTER_BACKENDS` 头一个是 `ObjectFilter`）：

- 列表 / 取详情：`ObjectFilter` 先看有没有**全局** `authentik_core.view_user`；有就放行整个 queryset，没有就退回 guardian 的逐对象 view 权限，两者都没有直接 `PermissionDenied`。【源码】S4
- 写：`ObjectPermissions.has_object_permission` 用 DRF 的 perms_map，PATCH 需要 `authentik_core.change_user`；同样是「全局优先，其次逐对象」。【源码】S4

所以最小可用组合是：

| 权限项 | 用途 | 能否再收窄 |
|---|---|---|
| `authentik_core.view_user` | 按 `?attributes=` 查人、GET 回读 | 可换成逐对象 view，但见下 |
| `authentik_core.change_user` | PATCH 写 attributes | 可换成逐对象 change，但见下 |

**不需要**：`add_user`、`delete_user`、`reset_user_password`、`impersonate`、`view_token_key`、任何 group/role 相关权限。（若 IT 同意做本地 finance 账号，才额外要 `add_user`，见 Q6。）

### 对象级收窄为什么在这个场景不成立

authentik 确实支持把 `change_user` 只授予到**具体某几个用户对象**（django-guardian，UI: 用户详情 > Permissions，或 API `/api/v3/rbac/permissions/assigned_by_roles/...`）。但我方要写的是**将来才入职的新员工**——对象权限必须对每个新用户逐个授予，没有「对某个 group 内的全部用户」这种规则型授权（`InitialPermissions` 只对「由该角色成员自己创建的对象」生效，而新员工是 IT 侧同步创建的，不是我方创建的）。【源码】S4 `assign_initial_permissions`、【推理】
所以现实可选项只有两个：
- A. 全局 `view_user` + 全局 `change_user`（能用，但爆炸半径见 §9）；
- B. 我方不拿写权限，改由 IT 侧提供一个窄接口（见 §8 备选方案）。

### 必须同时告知 IT 的一条：在 2025.6.4 上，`change_user` ≈ 管理员

**CVE-2026-40172 / GHSA-h6x7-hjjc-wjc9**（2026-05-12，high）原文：

> `PATCH /api/v3/core/users/{pk}/` allows a caller with `change_user` on a target user to assign arbitrary `groups` through `UserSerializer`, including groups with `is_superuser=True`, without requiring `enable_group_superuser`.
> Users with permissions to update groups or permissions to update users are able to add themselves or other users they have permissions on to users which have superuser permissions.

影响范围标注为 `<= 2025.12.4` 与 `<= 2026.2.2` —— **2025.6.4 在范围内**。源码侧也对得上：2025.6 的 `UserSerializer` 只有 `validate_path` / `validate_type` / `validate`，没有 `validate_groups`；而 2026.8 补上了 `validate_groups`（要求 `authentik_core.enable_group_superuser`）。【源码】S1 两分支对比

后续的 **GHSA-h6c5-mpvq-j4jc**（2026-09-09，high）说明这类问题到 2026.2.7 / 2026.5.7 / 2026.8.2 才算收干净，官方给出的 workaround 原文：

> Restrict the permissions to create and modify groups, to modify users, and to add users to groups, so that all of them are held only by accounts that are already full administrators.

结论要说白：在 IT 当前版本上，把 `change_user` 发给一个非管理员主体，技术上等于发了一张「可以自我提权为管理员」的通行证。这不是我方要求造成的，是该版本的已知缺陷。处理办法二选一：IT 升级到已修复版本；或者 IT 明确接受该风险，并配合 §9 的补偿控制。这条必须写进申请文档，不能让 IT 事后才发现。

---

## 6. Q5：审计

- **属性变更会进 Events**。`AuditMiddleware` 挂 `post_save`，非忽略模型的更新一律产出 `EventAction.MODEL_UPDATED`，动作串是 **`model_updated`**。【源码】S5
- **事件里没有字段级 diff**。存进 context 的只有 `model_to_dict(instance)`，而 authentik 自己的 `model_to_dict` 只返回四个键：
  ```python
  return {"app": model._meta.app_label, "model_name": model._meta.model_name, "pk": model.pk, "name": name}
  ```
  【源码】`authentik/events/utils.py`。也就是说：事件能证明「谁在什么时候、从哪个 IP 改了用户 X」，**不能证明改了什么值**。我方必须自建带前后值的审计日志。
- **可以通过 API 拉取**（只读凭证够用）：
  ```
  GET /api/v3/events/events/?action=model_updated&context_model_app=authentik_core&context_model_name=user&username=<服务账号用户名>
  GET /api/v3/events/events/?action=model_updated&context_model_pk=<user-pk>
  ```
  过滤器字段：`action`（icontains）、`username`、`context_model_app`、`context_model_name`、`context_model_pk`、`client_ip`、`brand_name`。【源码】S5 `EventsFilter`
  需要权限 `authentik_events.view_event`。注意 EventViewSet 是 ModelViewSet（不是只读视图集），所以别顺手把 `delete_event` 也给了。【源码】S5
- **保留期**：租户设置 `event_retention`，默认 `days=365`。【源码】`authentik/tenants/models.py` +【文档】D4
- 另有 `secret_view`（有人查看 token key）与 `secret_rotate`（token 到期轮换）两类事件，值得纳入监控。【源码】S2/S3

---

## 7. Q6：通过 API 建本地用户（finance 共享账号）

可行。【源码】S1

- 建用户：`POST /api/v3/core/users/`
  ```json
  {"username": "finance", "name": "财务共享账号", "type": "internal",
   "path": "users", "is_active": true, "attributes": {}}
  ```
  必填只有 `username`（唯一）和 `name`（允许空串）；`type` 取值 `internal` / `external` / `service_account`（`internal_service_account` 禁止外部设置）；`path` 模型默认 `"users"`；`email` 可选。权限：`authentik_core.add_user`。
- 设密码：`POST /api/v3/core/users/<pk>/set_password/`，body `{"password": "..."}`，权限 `authentik_core.reset_user_password`，成功返回 204。会走 authentik 的密码策略校验，不合规返回 400。【源码】S1
- **「本地 vs 来自某个 source」怎么标**：User 模型上没有 source 字段，来源关系是独立的 `UserSourceConnection` 表（`sources = ManyToManyField("Source", through="UserSourceConnection")`）。通过 API 建的用户天然没有任何 source connection，即「本地用户」，不需要额外标记。【源码】S2 +【推理】
- 若这个账号只用于程序调用而非人登录，更合适的是 `POST /api/v3/core/users/service_account/`（body `name` / `create_group` / `expiring` / `expires`），一次性返回 app password；权限要求 `authentik_core.add_user` + `authentik_core.add_token`。【源码】S1
- 【拿不准】`internal` 类型用户会计入企业版 license 计数，`external` 不计。如果 IT 是企业版，建 finance 账号前问一句用哪种 type 更合适。

---

## 8. Q7：限流

- **authentik 内建限流只覆盖匿名请求**。`REST_FRAMEWORK` 里：
  ```python
  "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
  "DEFAULT_THROTTLE_RATES": {"anon": CONFIG.get("throttle.default")},
  ```
  2026.8 换成 `authentik.api.throttle.LocalAnonRateThrottle`，策略结构不变。【源码】S6
- 默认速率来自配置 `throttle.default`，`authentik/lib/default.yml` 里是 `1000/second`（另有 `throttle.providers.oauth2.device: 20/hour`）。对应环境变量 `AUTHENTIK_THROTTLE__DEFAULT`。【源码】`authentik/lib/default.yml`
- **带 Bearer token 的已认证请求不受这条限流**（`AnonRateThrottle` 只对未认证请求生效）。【推理】
- 【拿不准】IT 的反向代理 / WAF 可能另有限流，需要问。我方无论如何自行限速：单次审批触发一次写入，失败退避重试，不做轮询式全量扫描。

---

## 9. 可直接贴进申请单的段落

> ### 关于云账号交付面板写入 IAM 用户属性的接口申请
>
> #### 背景
>
> 员工申请云账号，审批通过后，我方在阿里云和火山引擎上创建 RAM / IAM 子账号。SSO 要认到这个人，需要把他在云上的账号名写回 IAM 里该用户的 `cloud_accounts` 属性。目前这一步靠人工导 CSV 交给贵方，希望改成审批通过后由我方服务实时写入。属性形状沿用双方已约定的：
>
> ```json
> "cloud_accounts": {
>   "aliyun-main":  "wangzihan@1704065796538912.onaliyun.com",
>   "volcano-main": "ZihanWang"
> }
> ```
>
> #### 需要的凭证
>
> 一个专用的 service account（不复用任何人的个人账号、也不复用其它系统的账号），外加一个 Intent 为 API Token 的 token。请求两点：
>
> 1. 把 token 的 Expiring 设为关闭，或者告知贵方 System > Settings 里 Default token duration 的当前值，以便我方安排轮换。该版本对 API Token 的有效期由服务端强制取系统默认值，创建时填的过期时间不生效，而系统默认值通常只有一天量级，若不关闭过期，token 会在一天后静默失效。
> 2. 告知 token 的吊销方式与联系人，以便我方在怀疑泄漏时第一时间请贵方删除。
>
> 调用形式为 `Authorization: Bearer <token>`，API 根路径 `/api/v3/`。
>
> #### 需要的权限项
>
> 只需要两项，均针对 User 模型：
>
> - `authentik_core.view_user` —— 用于按属性检索用户和写入后回读校验
> - `authentik_core.change_user` —— 用于更新用户的 attributes
>
> 明确不需要：`add_user`、`delete_user`、`reset_user_password`、`impersonate`、`view_token_key`，以及任何 group / role 相关权限。
>
> 如果贵方希望额外给一个只读凭证用于我方自查审计，再加 `authentik_events.view_event` 即可，也可以不给。
>
> 另有一个独立的小需求：我方有一个 finance 共享账号场景，需要创建一个不来自任何 source 的本地用户。如果贵方同意由我方创建，需要 `authentik_core.add_user`，以及设置初始密码所需的 `authentik_core.reset_user_password`；如果贵方更希望自己建，我方不需要这两项。
>
> #### 我方会调用的接口
>
> | 方法 | 路径 | 用途 |
> |---|---|---|
> | GET | `/api/v3/core/users/?attributes={"feishu_union_id":"<union_id>"}&include_groups=false` | 按飞书 union id 定位用户，取 pk 与当前 attributes |
> | PATCH | `/api/v3/core/users/<pk>/` | 写回合并后的完整 attributes |
> | GET | `/api/v3/core/users/<pk>/?include_groups=false` | 写入后回读校验 |
> | GET | `/api/v3/events/events/?action=model_updated&context_model_app=authentik_core&context_model_name=user` | 可选，我方自查用 |
>
> 频率：每次账号审批通过触发一次，日均个位数，不做轮询扫描。
>
> #### 我方的写入方式
>
> 该版本的 PATCH 对 attributes 是整体替换，不是深合并，直接提交一个只含 `cloud_accounts` 的 attributes 会清掉用户其它属性。因此我方固定按以下流程写入：
>
> 1. 先 GET 取回该用户完整的 attributes；命中 0 条或多于 1 条时直接中止并告警，不写；
> 2. 在我方内存中合并，只修改 `cloud_accounts` 一个顶层键，其余键原样保留，代码层面对可写键做白名单；
> 3. PATCH 提交合并后的完整 attributes 对象；只用 PATCH，不使用 PUT（PUT 会因为 groups 字段的默认值清空该用户的组关系）；
> 4. 写入后立即回读比对，`cloud_accounts` 正确且原有键一个不少才算成功，不一致则告警人工处理，不自动重试覆盖；
> 5. 同一用户的写入在我方侧串行化，避免并发相互覆盖。该接口不支持 If-Match 之类的条件请求，读改写之间如果贵方侧同步或管理员手改，存在互相覆盖的可能，第 4 步的回读就是为此设置的。
>
> 我方会自建带前后值的写入审计日志（谁触发、改了哪个用户、旧值新值、结果），保留期与我方其它审计一致。
>
> #### 需要贵方确认的三件事
>
> 1. 用户 attributes 里飞书 union id 的准确键名与层级，以及能否保证唯一。
> 2. 贵方同步用户时是否会写 `cloud_accounts` 这个键。若不会，我方写入不会被同步覆盖。
> 3. 当前版本为 2025.6.4。该版本存在一个已公开的问题 CVE-2026-40172（GHSA-h6x7-hjjc-wjc9）：持有 `change_user` 的调用方可以通过 PATCH 用户的 groups 字段把任意用户加入带 superuser 的组，绕过 `enable_group_superuser` 检查，影响范围标注为 2025.12.4 及以前。也就是说在该版本上，`change_user` 实际具备提权到管理员的能力，这不是本申请引入的，但会因为本申请多出一个持有该权限的主体。上游在 2026.x 分支加了 `validate_groups` 校验修复，随后的 GHSA-h6c5-mpvq-j4jc 给出的临时处置是把用户与组的修改权限只留给完整管理员。请贵方评估是升级版本，还是接受风险并配合下列补偿措施：
>    - 限制该 token 只能从我方服务的固定出口 IP 使用；
>    - 对该 service account 产生的 `model_updated` 事件做监控，尤其是涉及 group 的变更（我方只会改 attributes，任何组变更都应视为异常）；
>    - 约定 token 轮换周期，以及泄漏时的紧急吊销流程。

---

## 10. 风险清单

### 这个 token 泄漏或被滥用能造成什么

| 风险 | 成因 | 严重度 |
|---|---|---|
| 全量读取员工目录 | `view_user` 是全局的，可分页拉全部用户：用户名、姓名、邮箱、全部 attributes（含工号、飞书 id、我方写的云账号名）、组关系 | 高，属于人事/身份数据批量泄漏 |
| 篡改任意用户的 SSO 关键字段 | `change_user` 可改 `username` / `email` / `is_active` / `attributes`。改 `username` 或清空 `cloud_accounts` 会直接让该员工云端 SSO 失效；给 A 写上 B 的云账号名则等于把 B 的云权限交给 A | 高，且我方的 NameID 映射表达式正是读这个属性 |
| 批量停用账号 | `is_active=false` 可批量下发 | 高，等同定向 DoS |
| 提权为管理员 | CVE-2026-40172：PATCH `groups` 把自己或他人加进 superuser 组，在 2025.6.4 上未修 | 严重，等同 IdP 完全失守 |
| 静默失效导致交付中断 | token 到期被删除或轮换 key，调用方只看到 401 | 中，影响可用性不影响安全 |
| 误覆盖属性 | 直接 PATCH 不带完整 attributes，或 GET/PATCH 之间被并发写 | 中高，破坏的是别人的数据且不易察觉 |

### 我方应当自我施加的限制

1. token 存密文（复用现有 Fernet/密钥管理那套），只在服务端进程内解密，绝不落日志、绝不进前端、绝不进 git。
2. 出网固定出口 IP，请 IT 在其侧做 IP 限制（我方无法自证，需要服务端配合）。
3. 代码层面把可写键写死为 `cloud_accounts`：合并函数只接受这一个顶层键的变更，其余键逐字回写；单测锁住「传入含额外键的 payload 时拒绝写入」。
4. 只用 PATCH，禁用 PUT；HTTP 客户端层面把该 base URL 的 PUT/DELETE 方法直接封掉，防止将来有人顺手调用。
5. 每次写入前后落自建审计（触发的审批单号、目标 pk、旧 attributes、新 attributes、结果），并定期用 `authentik_events.view_event` 拉 Events 与自建日志对账，出现我方未记录的 `model_updated` 即告警。
6. 单用户写入串行化 + 写后回读校验，失败只告警不自动覆盖重试。
7. 限速与重试退避，绝不做轮询式全量扫描（该接口对已认证请求没有服务端限流，打爆是我方的责任）。
8. token 轮换排期（若 IT 给的是不过期 token，我方自定 90 天主动申请轮换），并保留紧急吊销联系人。
9. 上线前用一个测试用户走通「写入 → 回读 → 其余属性未丢」的全流程，别拿真人账号试。

### 若 IT 不愿意发 change_user（备选方案）

由 IT 在其侧提供一个窄接口，我方 POST `{union_id, cloud_accounts}`，IT 服务内部完成合并写入。爆炸半径从「整个用户目录可读可改」缩到「一个只能写 cloud_accounts 的函数」，也绕开了 CVE-2026-40172。代价是 IT 要写一小段代码。如果 IT 对发 `change_user` 犹豫，这是更该推的方案，建议在申请单里作为备选一并提出。【推理】

---

## 11. 仍未取证的点

- 【拿不准】IT 实例的 System > Settings 里 `Default token duration` 与 `Event retention` 的实际取值 —— 只能问 IT，不能靠猜（代码默认 `days=1`，2026.8 文档写 `minutes=30`）。
- 【拿不准】IT 的用户来自哪种 source（LDAP / SCIM / OAuth / 自研同步），其 property mapping 是否会写 `cloud_accounts`。
- 【拿不准】IT 侧反向代理是否另有限流 / IP 白名单能力。
- 【推理，未真机验证】嵌套属性过滤 `?attributes={"cloud_accounts__aliyun-main":"..."}`；源码链路与 Python/Django 语义都已核实，但没有在真实实例上跑过。我方实现不依赖它（只用顶层 `feishu_union_id`）。
- 【拿不准】`internal` 与 `external` 用户类型在 IT 的 license 口径下的差别。
