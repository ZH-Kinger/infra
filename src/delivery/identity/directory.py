"""通讯录：拿到每个人的 union_id，用来回填人员名册。

两个来源，任选其一：
  · 飞书通讯录接口（需要应用的「通讯录权限范围」覆盖全员，外加
    `contact:contact.base:readonly`、`contact:user.employee:readonly`）
  · IT 从 WUJI IAM 导出的对照表 CSV（规范里写明可以找 IT 要：union_id / 工号 / 邮箱 / 姓名）

两者产出同样的 `people.DirectoryEntry`。离职的人不进名册。
"""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from ..feishu import FeishuError, _post_json
from ..people import DirectoryEntry

TENANT_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"  # noqa: S105
API = "https://open.feishu.cn/open-apis"
_TIMEOUT = 15

Getter = Callable[[str, str], dict]
Progress = Callable[[str], None]


def tenant_token(app_id: str, app_secret: str) -> str:
    if not app_id or not app_secret:
        raise FeishuError("缺少 DELIVERY_FEISHU_APP_ID / DELIVERY_FEISHU_APP_SECRET")
    data = _post_json(TENANT_TOKEN_URL, {"app_id": app_id, "app_secret": app_secret})
    token = data.get("tenant_access_token")
    if data.get("code") not in (0, None) or not token:
        raise FeishuError(
            f"取 tenant_access_token 失败：code={data.get('code')} msg={data.get('msg')}"
        )
    return str(token)


def _get(url: str, token: str) -> dict:
    # url 由本模块拼接的飞书端点，不接受外部输入
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode(errors="replace") or "{}")
        except ValueError:
            return {"code": exc.code, "msg": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        raise FeishuError(f"连不上飞书：{exc.reason}") from exc


def _scopes(token: str, get: Getter) -> tuple:
    """应用的通讯录可见范围，翻页取全。返回 `(部门, 直接授权的人)`。

    **这个接口会分页，而且三张表（部门/人/用户组）合计不超过 page_size。**
    只取一页的话，范围配得多一点就会静默丢人 —— 丢掉的那个人表现为「名册里没有 union_id」，
    和「权限没配」长得一模一样。这正是这次要修的那个坑，别在这里原地复发一次。
    """
    depts: list = []
    users: list = []
    page_token = ""
    for _ in range(200):
        query = {
            "department_id_type": "open_department_id",
            "user_id_type": "union_id",
            "page_size": 100,
        }
        if page_token:
            query["page_token"] = page_token
        data = _one("/contact/v3/scopes", query, token, get)
        depts += [str(d or "") for d in (data.get("department_ids") or []) if d]
        users += [str(u or "") for u in (data.get("user_ids") or []) if u]
        if not data.get("has_more"):
            return list(dict.fromkeys(depts)), list(dict.fromkeys(users))
        page_token = str(data.get("page_token") or "")
        if not page_token:
            raise FeishuError(
                "/contact/v3/scopes 返回 has_more 但没有 page_token，可见范围不完整，已中断"
            )
    raise FeishuError("/contact/v3/scopes 翻页超过 200 页，已中断")


def _one(path: str, params: dict, token: str, get: Getter) -> dict:
    """取一页，返回 `data`。和 `_pages` 共用同一套错误处理 ——
    **失败一律抛错，绝不返回空字典**：把「问不到」渲染成「什么都没有」是这个模块最贵的错。"""
    body = get(f"{API}{path}?{urllib.parse.urlencode(params)}", token)
    if body.get("code") != 0:
        raise FeishuError(
            f"{path} 失败：code={body.get('code')} msg={body.get('msg')}。"
            "常见原因：应用的通讯录权限范围没配，或缺 contact 相关权限。"
            "这不是「没有人」——已中断。"
        )
    return body.get("data") or {}


def _pages(path: str, params: dict, token: str, get: Getter) -> list:
    items, page_token = [], ""
    for _ in range(500):
        query = dict(params, page_size=50)
        if page_token:
            query["page_token"] = page_token
        body = get(f"{API}{path}?{urllib.parse.urlencode(query)}", token)
        if body.get("code") != 0:
            raise FeishuError(
                f"{path} 失败：code={body.get('code')} msg={body.get('msg')}。"
                "常见原因：应用的通讯录权限范围没覆盖全员，或缺 contact 相关权限。"
                "这不是「没有人」——已中断。"
            )
        data = body.get("data") or {}
        items += data.get("items") or []
        if not data.get("has_more"):
            return items
        page_token = str(data.get("page_token") or "")
        if not page_token:
            raise FeishuError(f"{path} 返回 has_more 但没有 page_token，数据不完整，已中断")
    raise FeishuError(f"{path} 翻页超过 500 页，已中断")


def from_feishu(
    app_id: str,
    app_secret: str,
    *,
    get: Optional[Getter] = None,
    token: Optional[str] = None,
    progress: Optional[Progress] = None,
) -> list:
    get = get or _get
    token = token or tenant_token(app_id, app_secret)
    # **先问飞书「我能看到哪些部门」，不要从根部门 0 开始爬。**
    #
    # 应用设了通讯录可见范围之后，`/departments/0/children` 会回
    # `40004 no dept authority` —— 而那一声失败会让整次刷新回退成「沿用上一份名册」，
    # 日志上只印一行「沿用上一份名册的 union_id N 人」，看起来像正常状态。
    # 真机实测：这个回退持续了很久，新入职的两个人（张子超、练秋酉）因此一直没有 union_id，
    # 既发不进公司 IAM 也对不了账，而没有任何地方显示这件事正在发生。
    #
    # `/contact/v3/scopes` 返回的就是「这个应用被授权看到的部门和人」。
    # 全员可见的应用它会回根部门，范围受限的应用回那几个部门 —— **两种情况同一套代码**。
    roots, direct = _scopes(token, get)
    if not roots and not direct:
        raise FeishuError(
            "/contact/v3/scopes 返回的可见范围是空的。这不是「公司没有人」——"
            "应用的通讯录可见范围没配，或者缺 contact 权限。已中断。"
        )
    departments = list(roots)
    for root in roots:
        departments += [
            str(d.get("open_department_id") or "")
            for d in _pages(
                f"/contact/v3/departments/{root}/children",
                {"fetch_child": "true", "department_id_type": "open_department_id"},
                token,
                get,
            )
        ]
    departments = list(dict.fromkeys(d for d in departments if d))
    seen: dict = {}
    for i, dept in enumerate(d for d in departments if d):
        if progress:
            progress(f"通讯录 部门 {i + 1}/{len(departments)}")
        users = _pages(
            "/contact/v3/users/find_by_department",
            {
                "department_id": dept,
                "department_id_type": "open_department_id",
                "user_id_type": "union_id",
            },
            token,
            get,
        )
        for u in users:
            status = u.get("status") or {}
            if status.get("is_resigned"):
                continue
            uid = str(u.get("union_id") or "")
            if not uid or uid in seen:
                continue
            seen[uid] = DirectoryEntry(
                union_id=uid,
                name=str(u.get("name") or ""),
                enterprise_email=str(u.get("enterprise_email") or u.get("email") or ""),
                employee_no=str(u.get("employee_no") or ""),
            )
    # 直接授权到个人的：他们不在任何可见部门里，上面那一圈遍历不到。
    # **漏了就是名册少人**，而少的那个人表现为「没有 union_id」—— 和权限没配长得一样
    missing = [u for u in direct if u not in seen]
    for batch in (missing[i : i + 50] for i in range(0, len(missing), 50)):
        data = _one(
            "/contact/v3/users/batch",
            [("user_ids", x) for x in batch] + [("user_id_type", "union_id")],
            token,
            get,
        )
        for u in data.get("items") or []:
            status = u.get("status") or {}
            if status.get("is_resigned"):
                continue
            uid = str(u.get("union_id") or "")
            if uid and uid not in seen:
                seen[uid] = DirectoryEntry(
                    union_id=uid,
                    name=str(u.get("name") or ""),
                    enterprise_email=str(u.get("enterprise_email") or u.get("email") or ""),
                    employee_no=str(u.get("employee_no") or ""),
                )
    return list(seen.values())


#: 逐个查状态时，飞书对「不在应用可用范围内」和「人已经被移出通讯录」回的是**同一个**
#: 错误码。所以它只能是「查不到，要人确认」，绝不能当成「已离职」—— 那会把范围外的
#: 同事一起报成离职，而第一次误伤就会让这份清单失去可信度
NO_AUTHORITY = 41050


def status_of(union_ids, app_id: str, app_secret: str, *, get=None, token: str = "") -> dict:
    """按 union_id 逐个查在职状态。`{union_id: 状态字典 或 None}`，None 表示查不到。

    为什么不用 `/contact/v3/users/find_by_department` 拉全量：那个接口要部门权限
    （`no dept authority`），而应用的通讯录可用范围通常不覆盖全员。逐个查只要
    `contact:user.base:readonly`，而且我们本来就只关心名册里那几十个有云账号的人。

    返回的状态里 `is_resigned` 才是「离职」，`is_frozen` 是暂停、`is_exited` 是已退出租户。
    三者含义不同，判定留给调用方。
    """
    get = get or _get
    token = token or tenant_token(app_id, app_secret)
    out = {}
    for uid in union_ids:
        uid = str(uid or "").strip()
        if not uid:
            continue
        body = get(
            f"{API}/contact/v3/users/{urllib.parse.quote(uid, safe='')}?user_id_type=union_id",
            token,
        )
        if not body.get("code"):
            out[uid] = ((body.get("data") or {}).get("user") or {}).get("status") or {}
        elif body.get("code") == NO_AUTHORITY:
            out[uid] = None
        else:
            raise FeishuError(
                f"查 {uid[:12]}… 的在职状态失败：code={body.get('code')} msg={body.get('msg')}。"
                f"这不是「这个人不在」——已中断，免得把查询故障当成离职。"
            )
    return out


_CSV_COLUMNS = {
    "union_id": ("feishu_union_id", "union_id"),
    "name": ("name", "姓名"),
    "email": ("email", "邮箱", "enterprise_email", "企业邮箱"),
    "employee_no": ("employee_no", "工号"),
}


def departments(app_id: str, app_secret: str, *, get=None, token: str = "") -> dict:
    """走完整棵部门树。返回 `{open_department_id: (部门名, 上级 id)}`。

    **必须逐层 `children` 递归**，两个坑都实测踩过：
      · `/contact/v3/departments` **只返回顶层**（现网 12 个），底下还有 15 个看不到；
      · `find_by_department` 的 `fetch_child=true` **不往下钻** —— 加不加都只回直属成员。
    只看顶层的话，50 个人里只有 6 个能对上部门，而真实数字是 49。

    根部门 `0` 通常不在应用的通讯录范围里（`no dept authority`），所以从「能列出来的
    顶层」开始走，不从根走。
    """
    get = get or _get
    token = token or tenant_token(app_id, app_secret)
    tree: dict = {}
    queue = [(d, "") for d in _pages("/contact/v3/departments", {}, token, get)]
    while queue:
        dep, parent = queue.pop(0)
        did = str(dep.get("open_department_id") or "")
        if not did or did in tree:
            continue
        tree[did] = (str(dep.get("name") or ""), parent)
        for kid in _pages(f"/contact/v3/departments/{did}/children", {}, token, get):
            queue.append((kid, did))
    return tree


def staff_index(app_id: str, app_secret: str, *, get=None, token: str = "") -> dict:
    """走完整棵部门树，按**公司邮箱**建索引。

    `{邮箱小写: {"union_id", "name", "department", "department_id"}}`

    为什么用邮箱而不是 union_id：新人**没登录过面板就没有 union_id**，而目录这件事
    不该等他登录。名册里本来就有公司邮箱，飞书的部门成员对象里也带
    `enterprise_email` —— 两边直接对得上，一个人都不用等。

    **别用 `batch_get_id` 按邮箱反查**：那个接口查的是 `email` 字段（个人邮箱），
    而公司邮箱在 `enterprise_email` 里。实测全员 `email` 都是空的，所以反查恒返空，
    而且返回 `code: 0 success` —— 查不到和查到空长得一模一样。正着扫反而简单可靠。

    顺带把 `union_id` 也带出来：名册里缺 union_id 的人可以据此补上。
    """
    get = get or _get
    token = token or tenant_token(app_id, app_secret)
    tree = departments(app_id, app_secret, get=get, token=token)

    def depth(did: str) -> int:
        n, cur = 0, did
        while cur and cur in tree:
            n, cur = n + 1, tree[cur][1]
        return n

    best: dict = {}
    for did, (name, _parent) in tree.items():
        here = depth(did)
        for m in _pages(
            "/contact/v3/users/find_by_department",
            {"department_id": did, "user_id_type": "union_id"},
            token,
            get,
        ):
            mail = str(m.get("enterprise_email") or m.get("email") or "").strip().lower()
            if not mail:
                continue
            # 一个人挂在多个部门时取**最深的那个**：`算法组/预训练组` 比 `算法组`
            # 有信息量，而路径只能有一段
            if here <= best.get(mail, (0,))[0]:
                continue
            best[mail] = (
                here,
                {
                    "union_id": str(m.get("union_id") or ""),
                    "name": str(m.get("name") or ""),
                    "department": name,
                    "department_id": did,
                },
            )
    return {mail: info for mail, (_d, info) in best.items()}


def from_csv(path: str) -> list:
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            fields = reader.fieldnames or []
            rows = list(reader)
    except OSError as exc:
        raise FeishuError(f"读不了对照表 {path}：{exc}") from exc
    except csv.Error as exc:
        raise FeishuError(f"{path} 不是合法 CSV：{exc}") from exc
    # 表头从 fieldnames 取，不从首行取：首行列数多于表头时多出的值挂在 None 键下
    header = {h.strip().lower(): h for h in fields if isinstance(h, str)}

    def col(field: str) -> Optional[str]:
        for alias in _CSV_COLUMNS[field]:
            if alias.lower() in header:
                return header[alias.lower()]
        return None

    uid_col = col("union_id")
    if uid_col is None:
        # 只有表头的空模板也要查：表头写错不能静默当成「通讯录没人」
        raise FeishuError(f"{path} 缺 union_id 列（认 feishu_union_id / union_id）")
    name_col, email_col, emp_col = col("name"), col("email"), col("employee_no")
    out = []
    for row in rows:
        uid = (row.get(uid_col) or "").strip()
        if not uid:
            continue
        out.append(
            DirectoryEntry(
                union_id=uid,
                name=(row.get(name_col) or "").strip() if name_col else "",
                enterprise_email=(row.get(email_col) or "").strip() if email_col else "",
                employee_no=(row.get(emp_col) or "").strip() if emp_col else "",
            )
        )
    return out


__all__ = ["from_csv", "from_feishu", "tenant_token"]
