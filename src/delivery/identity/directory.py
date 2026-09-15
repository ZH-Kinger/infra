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
    departments = ["0"] + [
        str(d.get("open_department_id") or "")
        for d in _pages(
            "/contact/v3/departments/0/children",
            {"fetch_child": "true", "department_id_type": "open_department_id"},
            token,
            get,
        )
    ]
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
    return list(seen.values())


_CSV_COLUMNS = {
    "union_id": ("feishu_union_id", "union_id"),
    "name": ("name", "姓名"),
    "email": ("email", "邮箱", "enterprise_email", "企业邮箱"),
    "employee_no": ("employee_no", "工号"),
}


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
