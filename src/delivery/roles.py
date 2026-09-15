"""谁是管理员。

为什么是显式白名单，不从云上权限反推
────────────────────────────────────
「在任一朵云上有 AdministratorAccess 就算看板管理员」看着省事、还能自动跟着漂移，
但它把两件不同的事绑死了：**能改云资源** 和 **能看全公司的权限分布**。后果是任何人
只要给自己挂上云超管，就同时拿到了看板上所有人的账号、邮箱、权限清单——而云上的
超管本来就是要收敛的对象（实测：阿里 4 个、火山 5 个用户级超管 + 4 个靠 IAMFullAccess
变相拿到的）。

所以这里要一份**小而可审计**的名单，改它要过代码评审，不是在云控制台点两下。

fail-closed：没配置 ⇒ **没有人**是管理员，而不是所有人。配错方向的代价不对称——
少给权限只是有人看不到页面，多给权限是把全员身份数据摊开。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .errors import DeliveryError

ROLE_ADMIN = "admin"
ROLE_USER = "user"

#: 白名单文件路径的环境变量。留空则用仓库内的 `identity/admins.json`。
ENV_ADMINS = "DELIVERY_ADMINS_FILE"


class RoleError(DeliveryError):
    """管理员名单不合法。"""


@dataclass(frozen=True)
class Admins:
    """管理员名单。邮箱一律小写比对——飞书返回的大小写不保证稳定。"""

    emails: frozenset = frozenset()
    union_ids: frozenset = frozenset()

    def role_of(self, *, email: str = "", union_id: str = "") -> str:
        if union_id and union_id in self.union_ids:
            return ROLE_ADMIN
        if email and email.strip().lower() in self.emails:
            return ROLE_ADMIN
        return ROLE_USER

    def is_admin(self, *, email: str = "", union_id: str = "") -> bool:
        return self.role_of(email=email, union_id=union_id) == ROLE_ADMIN

    @property
    def configured(self) -> bool:
        return bool(self.emails or self.union_ids)


def _clean(values: Iterable, field: str, *, lower: bool) -> frozenset:
    out = set()
    for raw in values:
        if not isinstance(raw, str):
            raise RoleError(f"`{field}` 里有非字符串项：{raw!r}")
        value = raw.strip()
        if not value:
            raise RoleError(f"`{field}` 里有空字符串")
        out.add(value.lower() if lower else value)
    return frozenset(out)


def load_admins(path: Optional[str] = None) -> Admins:
    """读管理员名单。

    文件不存在**不是错误**——返回空名单，即「没有人是管理员」。这是有意的：
    本地开发跑起来不该被一个还没建的文件挡住，而空名单本身是安全的那一侧。
    格式错误则是错误：那说明有人想配、但配歪了，静默当成空名单会让他以为配上了。
    """
    target = path or os.environ.get(ENV_ADMINS, "") or "identity/admins.json"
    file = Path(target)
    if not file.exists():
        return Admins()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RoleError(f"读不了 {target}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise RoleError(f"{target} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise RoleError(f'{target} 的顶层必须是对象，形如 {{"emails": [...]}}')
    unknown = set(data) - {"emails", "union_ids", "_note"}
    if unknown:
        raise RoleError(f"{target} 有未知字段 {sorted(unknown)}；只认 emails / union_ids")
    for field in ("emails", "union_ids"):
        if field in data and not isinstance(data[field], list):
            raise RoleError(f"{target}: `{field}` 必须是数组")
    return Admins(
        emails=_clean(data.get("emails") or [], "emails", lower=True),
        union_ids=_clean(data.get("union_ids") or [], "union_ids", lower=False),
    )
