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
          "login_prefix": "wuji-",
          "users": [
            {"name": "wuji-huangzenan", "display_name": "黄泽楠",
             "email": "huang.zenan@wuji.tech", "status": "正常", "created": "2026-09-21 15:01:19"}
          ]
        }
      ]
    }

`account` 是**主账号名**（九章是 `wuji`），`name` 是**用户的登录名**（`wuji-huangzenan`）。
`login_prefix` 写明登录名的固定前缀，用来挡「把用户名那一列当成登录名填进来」。

**名单本身就是确认**：登录名和邮箱由管理员成对给出，名册不再对它做服务号识别、
也不做「用户名能不能由邮箱推出」的判断（`wuji-` 恰好也是服务号前缀，一猜就全错）。

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
ACTIVE = frozenset({"正常", "启用", "已启用", "active", "enabled", "Active", "Enabled"})

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
        prefix = str(acc.get("login_prefix") or "").strip()
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
            if prefix and not name.startswith(prefix):
                # 登记的是登录名。少了前缀多半是把控制台里的「用户名」那一列当成了登录名
                raise OfflineError(f"{uat}.name 要填登录名（以 {prefix} 开头），收到 {name!r}")
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
                "login_prefix": prefix,
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

    邮箱来自管理员给的名单，**名单本身就是确认**（见模块开头）。
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


# ── 管理后台：粘贴名单 → 预览差异 → 保存 ─────────────────────────────────────
#
# 名单就是从平台控制台的用户列表整页复制下来的那种表格（九章那份：序号、用户名、登录用户名、
# 姓名、手机号、邮箱、状态、创建时间，每行后面还跟着「重置密码」「禁用」两个按钮）。
# **按内容认列，不按位置认列**：控制台改一下列的顺序、多一列少一列，按位置解析就会把
# 手机号当成邮箱、把姓名当成登录名，而且不报错。

#: 手机号，含控制台打码的写法（`178****1111`、`(+86)178…`）。认不出来的话，
#: 姓名列为空的行里手机号会被当成姓名存下来
_PHONE = re.compile(r"\A[\d*+()（） -]{7,}\Z")
_DIGITS_RUN = re.compile(r"[\d*]{7,}")
_HUMAN = re.compile(r"[A-Za-z一-鿿]")
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
_STATUS_WORDS = (
    "正常",
    "启用",
    "已启用",
    "禁用",
    "已禁用",
    "停用",
    "已停用",
    "锁定",
    "active",
    "disabled",
)
#: 一次保存最多移除多少比例的人，超了要显式确认。粘漏半页的后果是半个名单从名册里消失，
#: 离职检查也就再也看不到他们
MASS_REMOVE_RATIO = 0.3
HISTORY_KEEP = 20


def _scrub(line: str) -> str:
    """警告里带的原文：截短，并把像手机号的数字串抹掉。"""
    return _DIGITS_RUN.sub("…", line)[:60]


def parse_paste(text: str, *, login_prefix: str = "") -> tuple:
    """控制台里复制出来的表格 → `(users, warnings)`。手机号那一列**丢掉，不保存**。

    只认带邮箱的行：表头、「重置密码」「禁用」这些按钮行都没有 `@`，自然被跳过。
    """
    users, warnings, seen = [], [], set()
    for no, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if "@" not in line:
            continue
        cells = [c.strip() for c in re.split(r"\t+| {2,}", line) if c.strip()]
        email = next((c.lower() for c in cells if _EMAIL.match(c)), "")
        if not email:
            warnings.append(f"第 {no} 行有 @ 但认不出邮箱，跳过：{_scrub(line)}")
            continue
        if login_prefix:
            login = next((c for c in cells if c.startswith(login_prefix)), "")
        else:
            login = next(
                (c for c in cells if _NAME.match(c) and not c.isdigit() and "@" not in c), ""
            )
        if not login or not _NAME.match(login):
            hint = f"以 {login_prefix} 开头的" if login_prefix else ""
            warnings.append(f"第 {no} 行找不到{hint}登录名，跳过：{_scrub(line)}")
            continue
        if login in seen:
            warnings.append(f"第 {no} 行 {login} 重复出现，只留第一次")
            continue
        seen.add(login)
        idx = cells.index(login)
        display = ""
        if idx + 1 < len(cells):
            nxt = cells[idx + 1]
            if (
                not _PHONE.match(nxt)
                and not _EMAIL.match(nxt)
                and nxt not in _STATUS_WORDS
                and _HUMAN.search(nxt)
            ):
                display = re.sub(r"\s+", "", nxt)
        # 状态只在创建时间**之前**找：之后是「重置密码」「禁用」这些按钮。状态列写的是
        # 没见过的词时，从按钮里捞到「禁用」会让一个正常的人从名册和离职检查里消失
        when = _WHEN.search(line)
        before = cells
        if when:
            cut = next((i for i, c in enumerate(cells) if when.group(0) in c), len(cells))
            before = cells[:cut]
        status = next((c for c in before if c in _STATUS_WORDS), "")
        users.append(
            {
                "name": login,
                "display_name": display,
                "email": email,
                "status": status,
                "created": when.group(0) if when else "",
            }
        )
    return users, warnings


def diff(old_users: list, new_users: list) -> dict:
    """按登录名比。返回 `{added, removed, changed}`，每项是登录名列表。"""
    before = {u["name"]: u for u in old_users or ()}
    after = {u["name"]: u for u in new_users or ()}
    keys = ("display_name", "email", "status")
    changed = sorted(
        n
        for n in before.keys() & after.keys()
        if any((before[n].get(k) or "") != (after[n].get(k) or "") for k in keys)
    )
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": changed,
    }


def read_raw(path: Optional[str]) -> dict:
    """原样读出来（带 history 这些 parse 不关心的字段）。文件不在 = 空表。"""
    if not path or not Path(path).exists():
        return {"accounts": []}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_account(
    path: str,
    *,
    platform: str,
    account: str,
    login_prefix: str,
    users: list,
    source: str,
    as_of: str,
    actor: str,
    allow_mass_remove: bool = False,
) -> dict:
    """用一份新名单**整体替换**这个账号的登记。返回这次的差异。

    **整体替换，不是追加**：粘进来的就是控制台里的完整名单，不在里面的就是已经没有了。

    · 持文件锁读改写（面板和刷新是两个进程）
    · 写之前整张表 parse 一遍，写进去的一定能加载
    · 一次移除超过三成的人要显式确认 —— 粘漏半页的后果是半个名单从名册里消失
    · 每次保存记一条 history：谁、什么时候、加了几个减了几个
    """
    import fcntl
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    if not users:
        raise OfflineError("一个人都没解析出来，不保存（整体替换会把这个账号清空）")
    target = Path(path)
    with Path(f"{target}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_raw(path)
        rows = data.setdefault("accounts", [])
        cur = next(
            (a for a in rows if a.get("platform") == platform and a.get("account") == account), None
        )
        change = diff((cur or {}).get("users") or [], users)
        before = len((cur or {}).get("users") or [])
        if (
            before
            and len(change["removed"]) > max(2, before * MASS_REMOVE_RATIO)
            and not allow_mass_remove
        ):
            raise OfflineError(
                f"这次会移除 {len(change['removed'])} 人（原来 {before} 人）。"
                "如果是名单真的变了，勾选「确认移除」再保存；如果是粘漏了，重新复制完整名单"
            )
        if cur is None:
            cur = {"platform": platform, "account": account}
            rows.append(cur)
        cur.update(
            source=source[:80] or "人工登记",
            as_of=as_of,
            login_prefix=login_prefix,
            users=users,
        )
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        history = list(cur.get("history") or [])
        history.append(
            {
                "at": stamp,
                "by": str(actor or "")[:60],
                "total": len(users),
                "added": len(change["added"]),
                "removed": len(change["removed"]),
                "changed": len(change["changed"]),
            }
        )
        cur["history"] = history[-HISTORY_KEEP:]
        parse(data)  # 写之前整张表过一遍
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".offline-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(target)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
    return change
