"""没有采集接口的平台：账号靠人工登记，采集时合进快照和名册。

九章（AlayaNeW）是第一个。它没有能列用户的开放接口，而名册、「我的账号」、离职检查
都只认快照里有的账号 —— 不登记的话，一个离职的人在九章上的号永远没人提醒去停。

登记表 `identity/offline-accounts.json`（放在模板文件旁边的 identity/ 里）::

    {
      "accounts": [
        {
          "platform": "jiuzhang",
          "account": "wuji",
          "source": "九章控制台导出",
          "as_of": "2026-09-22",
          "users": [
            {"name": "huangzenan", "login": "wuji-huangzenan", "display_name": "黄泽楠",
             "email": "huang.zenan@wuji.tech", "status": "正常", "created": "2026-09-21 15:01:19"}
          ]
        }
      ]
    }

**不登记手机号。** 名册按企业邮箱关联到人，用不上手机号；多存一份就多一份泄漏面。

**人工登记的数据会过时**，而且过时的方向是**漏报**：登记之后九章上新开的号不在表里，
这个人离职时离职检查完全看不到它。所以每个账号**必须**带 `as_of`（截至哪天），
快照里带着 `source` / `as_of`，体检页标出「人工登记，截至某天」，
超过 `STALE_DAYS` 天没更新就明说「可能漏报」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .errors import DeliveryError

FILENAME = "offline-accounts.json"

#: 登记表里「能用」的状态。其余（禁用、已删除……）不进快照 —— 名册里不该出现
#: 一个人已经登不进去的号，离职检查也不该让人去停一个已经停了的号
ACTIVE = frozenset({"正常", "active", "enabled", "Active", "Enabled"})

_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@-]{0,63}\Z")
_EMAIL = re.compile(r"\A[^@\s]+@[^@\s]+\.[^@\s]+\Z")
_PLATFORM = re.compile(r"\A[a-z][a-z0-9-]{1,23}\Z")
#: 这几个平台有采集接口，**不许人工登记** —— 两个来源同时说一个账号是什么样，
#: 以谁为准就说不清了
_COLLECTED = frozenset({"aliyun", "volcano"})
_USER_KEYS = {"name", "login", "display_name", "email", "status", "created"}
_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
#: 登记表多少天没更新就提示「可能漏报」
STALE_DAYS = 30


class OfflineError(DeliveryError):
    """人工登记表不合法。"""


def parse(data, where: str = "人工登记表") -> list:
    if not isinstance(data, dict):
        raise OfflineError(f"{where}：顶层要是对象")
    rows = data.get("accounts")
    if not isinstance(rows, list):
        raise OfflineError(f"{where}：缺 accounts 数组")
    out, seen = [], set()
    for i, acc in enumerate(rows):
        at = f"{where}：accounts[{i}]"
        if not isinstance(acc, dict):
            raise OfflineError(f"{at} 必须是对象")
        platform = str(acc.get("platform") or "").strip()
        account = str(acc.get("account") or "").strip()
        if not _PLATFORM.match(platform):
            raise OfflineError(f"{at}.platform 不合法：{platform!r}")
        if platform in _COLLECTED:
            raise OfflineError(f"{at}：{platform} 有采集接口，不能人工登记（两个来源会打架）")
        if not account or "/" in account:
            raise OfflineError(f"{at}.account 要填，不能带 /")
        as_of = str(acc.get("as_of") or "").strip()
        if not _DATE.match(as_of):
            # 没有日期的话，没人知道这份名单是哪天的 —— 页面也就没法提醒它过期了
            raise OfflineError(f"{at}.as_of 要填登记日期，形如 2026-09-22")
        if (platform, account) in seen:
            raise OfflineError(f"{at}：{platform}/{account} 登记了两次")
        seen.add((platform, account))
        users, names = [], set()
        for j, u in enumerate(acc.get("users") or []):
            uat = f"{at}.users[{j}]"
            if not isinstance(u, dict):
                raise OfflineError(f"{uat} 必须是对象")
            unknown = sorted(set(u) - _USER_KEYS)
            if unknown:
                # 最常见的就是顺手把手机号也粘进来了 —— 拒掉，名册用不上它
                raise OfflineError(f"{uat} 里有不收的字段 {'、'.join(unknown)}（手机号不登记）")
            name = str(u.get("name") or "").strip()
            if not _NAME.match(name):
                raise OfflineError(f"{uat}.name 不合法：{name!r}")
            if name in names:
                raise OfflineError(f"{uat}：{name} 重复了")
            names.add(name)
            email = str(u.get("email") or "").strip().lower()
            if email and not _EMAIL.match(email):
                raise OfflineError(f"{uat}.email 不合法：{email!r}")
            users.append(
                {
                    "name": name,
                    "login": str(u.get("login") or "").strip(),
                    "display_name": re.sub(r"\s+", "", str(u.get("display_name") or "")),
                    "email": email,
                    "status": str(u.get("status") or "").strip(),
                    "created": str(u.get("created") or "").strip(),
                }
            )
        out.append(
            {
                "platform": platform,
                "account": account,
                "source": str(acc.get("source") or "人工登记").strip()[:80],
                "as_of": as_of,
                "users": users,
            }
        )
    return out


def load(path: Optional[str]) -> list:
    """文件不在 = 没有人工登记的账号，不是错误。"""
    if not path or not Path(path).exists():
        return []
    mode = Path(path).stat().st_mode & 0o777
    if mode & 0o077:
        # 表里是员工的企业邮箱和姓名。同机其他用户读得到，就和明文贴在群里差不多
        raise OfflineError(f"人工登记表 {path} 权限是 {mode:o}，改成 600 再刷新")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OfflineError(f"读不了人工登记表 {path}：{exc}") from exc
    return parse(data, where=f"人工登记表 {path}")


def beside(templates_path: Optional[str]) -> Optional[str]:
    if not templates_path:
        return None
    return str(Path(templates_path).with_name(FILENAME))


def active(user: dict) -> bool:
    return not user.get("status") or user["status"] in ACTIVE


def snapshot_accounts(rows: list) -> list:
    """快照格式的账号条目。人工登记的账号没有权限信息（策略、用户组都是空的）。"""
    return [
        {
            "platform": acc["platform"],
            "account": acc["account"],
            "source": acc["source"],
            "as_of": acc["as_of"],
            "users": [
                {
                    "name": u["name"],
                    "display_name": u["display_name"],
                    "email": u["email"],
                    "policies": [],
                    "groups": [],
                }
                for u in acc["users"]
                if active(u)
            ],
            "groups": [],
        }
        for acc in rows
    ]


def cloud_accounts(rows: list) -> list:
    """给名册匹配用的账号（`identity.ssomap.CloudAccount`）。

    邮箱来自平台控制台的导出、由管理员录入，**按可信处理** —— 和 RAM 用户基本信息里
    管理员写的邮箱同一个道理。
    """
    from .identity.ssomap import SOURCE_ADMIN_EXPORT, CloudAccount, EmailClaim

    out = []
    for acc in rows:
        scope = f"{acc['platform']}/{acc['account']}"
        for u in acc["users"]:
            if not active(u):
                continue
            emails = (EmailClaim(u["email"], SOURCE_ADMIN_EXPORT, True),) if u["email"] else ()
            out.append(CloudAccount(scope, u["name"], u["display_name"], emails))
    return out
