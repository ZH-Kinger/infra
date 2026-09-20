"""看板的两个视角：用户看自己，管理员看全局。产出就是 API 返回的 JSON。

分开的理由不是界面美观，是**数据边界**
────────────────────────────────────
用户视图里出现的每一条数据都必须是「这个登录者自己的」。做成同一个接口加个
`if is_admin` 隐藏几行，早晚会有人在某个分支里漏掉判断——而漏掉的后果是全员的账号、
邮箱、权限清单摊给任意一个能登录的人。

所以 `person_detail()` 只接收**一个已经认出来的人**（按 union_id 认，见 `people.py`），
拿不到名册；`admin_*()` 才接收整本名册。调用方要么调这个要么调那个，没有中间态。
"""

from __future__ import annotations

import urllib.parse
from typing import Mapping, Optional

from .inventory import Snapshot, is_high_risk
from .people import BIND_CONFLICT, BIND_NONE, AccountRef, PeopleIndex, Person

FILTERS = ("all", "multi", "high_risk", "unbound", "no_account")


class Labels:
    """平台显示名与云账号标签。

    账号标签来自 gitignored 的 `identity/accounts.json`（形如
    `{"aliyun/1704…": "阿里云主账号"}`）：账号 ID 不写进公开仓库的代码里。

    值也可以写成对象，额外给这个云账号配一条「用公司账号进控制台」的入口（IdP 发起的 SSO 链接）::

        {"aliyun/1704…": {"label": "阿里云主账号",
                          "console_url": "https://iam.example.com/application/saml/x/sso/binding/init/"}}

    只放行 https：这个地址会直接变成员工页面上的按钮。
    """

    def __init__(self, platforms: Optional[Mapping] = None, accounts: Optional[Mapping] = None):
        self.platforms = dict(platforms or {})
        self.accounts = dict(accounts or {})

    def platform(self, pid: str) -> str:
        return self.platforms.get(pid, pid)

    def _entry(self, pid: str, account: str) -> Mapping:
        value = self.accounts.get(f"{pid}/{account}")
        return value if isinstance(value, Mapping) else {"label": value or ""}

    def account(self, pid: str, account: str) -> str:
        entry = self._entry(pid, account)
        label = entry.get("label") if isinstance(entry.get("label"), str) else ""
        return label or f"{self.platform(pid)} {account}"

    def console_url(self, pid: str, account: str) -> str:
        """员工点「进入控制台」用的 IdP 发起登录地址；没配或不是 https 就没有这个按钮。"""
        url = self._entry(pid, account).get("console_url")
        if not isinstance(url, str) or not url:
            return ""
        parsed = urllib.parse.urlsplit(url)
        bad = any(c in url for c in ('"', "'", "<", ">", " ", "\\"))
        return url if parsed.scheme == "https" and parsed.netloc and not bad else ""


def _account_card(snapshot: Optional[Snapshot], ref: AccountRef, labels: Labels) -> dict:
    card = {
        "platform": ref.platform,
        "platform_display": labels.platform(ref.platform),
        "account": ref.account,
        "account_label": labels.account(ref.platform, ref.account),
        "console_url": labels.console_url(ref.platform, ref.account),
        "name": ref.name,
        "display_name": "",
        "email": "",
        "in_snapshot": False,
        "groups": [],
        "direct_policies": [],
        "group_policies": [],
        "effective_policies": [],
        "high_risk": [],
    }
    user = snapshot.user(ref.platform, ref.account, ref.name) if snapshot else None
    if user is None:
        return card
    groups = snapshot.groups_of(user)
    high = [p for p in user.policies if is_high_risk(p)]
    for g in groups:
        high += [f"{p}（经组 {g.name}）" for p in g.policies if is_high_risk(p)]
    card.update(
        display_name=user.display_name,
        # 云上这个号自己登记的邮箱：只作展示，认人不看它
        email=user.email,
        in_snapshot=True,
        groups=sorted({g.name for g in groups} | set(user.groups)),
        direct_policies=list(user.policies),
        group_policies=[{"group": g.name, "policies": list(g.policies)} for g in groups],
        effective_policies=list(snapshot.effective_policies(user)),
        high_risk=list(dict.fromkeys(high)),
    )
    return card


def _pending(ref: AccountRef, labels: Labels) -> dict:
    return {
        "platform": ref.platform,
        "platform_display": labels.platform(ref.platform),
        "account": ref.account,
        "account_label": labels.account(ref.platform, ref.account),
        "name": ref.name,
        "status": ref.status,
    }


def _meta(snapshot: Optional[Snapshot]) -> dict:
    return {
        "captured_at": snapshot.captured_at if snapshot else "",
        "snapshot_incomplete": list(snapshot.incomplete) if snapshot else [],
    }


def person_detail(
    person: Optional[Person],
    snapshot: Optional[Snapshot],
    labels: Labels,
    *,
    binding: str,
    note: str = "",
    fallback: Optional[Mapping] = None,
    include_pending: bool = False,
) -> dict:
    """一个人的权限详情。

    `include_pending` 只有管理员接口传 True：待确认的对应可能是错的，给本人看等于
    可能把别人的账号名给他看。
    """
    # 管理员看某人详情（include_pending）时，未绑定只是状态，账号照常列出；
    # 本人视角下未绑定/冲突则一条都不给。
    denied = (
        person is None or binding == BIND_CONFLICT or (binding == BIND_NONE and not include_pending)
    )
    who = fallback or {}
    cards = [] if denied else [_account_card(snapshot, ref, labels) for ref in person.accounts]
    return {
        "person": {
            "name": (person.name if person else "") or str(who.get("name") or ""),
            "email": (person.email if person else "") or str(who.get("email") or ""),
            "union_id": (person.union_id if person else "") or str(who.get("union_id") or ""),
        },
        "binding": binding,
        "binding_note": note,
        **_meta(snapshot),
        "summary": {
            "account_count": len(cards),
            "platforms": len({c["platform"] for c in cards}),
            "policy_count": sum(len(c["effective_policies"]) for c in cards),
            "high_risk_count": sum(len(c["high_risk"]) for c in cards),
        },
        "accounts": cards,
        "pending": (
            [_pending(r, labels) for r in person.pending]
            if (include_pending and person is not None)
            else []
        ),
    }


_ROW_KEYS = ("platform", "platform_display", "account", "account_label", "name", "in_snapshot")


def _person_row(person: Person, snapshot: Optional[Snapshot], labels: Labels) -> dict:
    cards = [_account_card(snapshot, ref, labels) for ref in person.accounts]
    scopes = [ref.scope for ref in person.accounts]
    dups = sorted({s for s in scopes if scopes.count(s) > 1})
    return {
        "key": person.key,
        "name": person.name,
        "email": person.email,
        "union_id": person.union_id,
        "bound": bool(person.union_id),
        "account_count": len(cards),
        "accounts": [{k: c[k] for k in _ROW_KEYS} for c in cards],
        "same_account_duplicates": dups,
        "high_risk": list(dict.fromkeys(p for c in cards for p in c["high_risk"])),
        "policy_count": sum(len(c["effective_policies"]) for c in cards),
        "pending_count": len(person.pending),
        "pending": [_pending(r, labels) for r in person.pending],
    }


def _unlinked_rows(snapshot: Optional[Snapshot], index: PeopleIndex, labels: Labels) -> list:
    """快照里有、但名册里没对应到任何人的子账号。名册的 unlinked 提供类型和原因。"""
    known = {(u.platform, u.account, u.name): u for u in index.unlinked}
    linked = {
        (r.platform, r.account, r.name) for p in index.people for r in (*p.accounts, *p.pending)
    }
    rows = []
    for user in snapshot.users if snapshot else ():
        key = (user.platform, user.account, user.name)
        if key in linked:
            continue
        meta = known.get(key)
        effective = snapshot.effective_policies(user)
        rows.append(
            {
                "platform": user.platform,
                "platform_display": labels.platform(user.platform),
                "account": user.account,
                "account_label": labels.account(user.platform, user.account),
                "name": user.name,
                "display_name": user.display_name,
                "kind": meta.kind if meta else "unknown",
                "reason": meta.reason if meta else "名册里没有这个账号（可能是名册生成后新建的）",
                "policy_count": len(effective),
                "high_risk": [p for p in effective if is_high_risk(p)],
            }
        )
    rows.sort(
        key=lambda r: (-len(r["high_risk"]), r["kind"] != "unknown", r["platform"], r["name"])
    )
    return rows


def _has_any(row: dict) -> bool:
    return bool(row["account_count"] or row["pending_count"])


def admin_overview(
    snapshot: Optional[Snapshot], index: PeopleIndex, labels: Labels, *, warnings=()
) -> dict:
    rows = [_person_row(p, snapshot, labels) for p in index.people]
    per_account: dict = {}
    if snapshot:
        for key in snapshot.accounts():
            per_account[key] = {"users": 0, "groups": 0, "high_risk_users": 0}
        for u in snapshot.users:
            slot = per_account[(u.platform, u.account)]
            slot["users"] += 1
            if any(is_high_risk(p) for p in snapshot.effective_policies(u)):
                slot["high_risk_users"] += 1
        for g in snapshot.groups:
            per_account[(g.platform, g.account)]["groups"] += 1
    return {
        **_meta(snapshot),
        "totals": {
            "people": sum(1 for r in rows if _has_any(r)),
            "cloud_users": len(snapshot.users) if snapshot else 0,
            "unlinked_accounts": len(_unlinked_rows(snapshot, index, labels)),
            "multi_account_people": sum(1 for r in rows if r["account_count"] >= 2),
            "high_risk_people": sum(1 for r in rows if r["high_risk"]),
            "unbound_people": sum(1 for r in rows if _has_any(r) and not r["bound"]),
            "no_account_people": sum(1 for r in rows if not _has_any(r)),
        },
        "accounts": [
            {
                "platform": platform,
                "platform_display": labels.platform(platform),
                "account": account,
                "account_label": labels.account(platform, account),
                **counts,
            }
            for (platform, account), counts in sorted(per_account.items())
        ],
        "warnings": list(warnings) + list(index.warnings),
    }


def admin_people(
    snapshot: Optional[Snapshot], index: PeopleIndex, labels: Labels, *, filter: str = "all"
) -> dict:
    if filter not in FILTERS:
        raise ValueError(f"filter 只能是 {', '.join(FILTERS)}")
    rows = [_person_row(p, snapshot, labels) for p in index.people]
    pick = {
        "all": _has_any,
        "multi": lambda r: r["account_count"] >= 2,
        "high_risk": lambda r: bool(r["high_risk"]),
        "unbound": lambda r: _has_any(r) and not r["bound"],
        "no_account": lambda r: not _has_any(r),
    }[filter]
    picked = [r for r in rows if pick(r)]
    picked.sort(key=lambda r: (-len(r["high_risk"]), -r["account_count"], r["name"]))
    # 名册审核「分配给」的候选：全员（含没有云账号的新人），只给有邮箱的
    assignable = sorted(
        ({"name": p.name, "email": p.email.lower()} for p in index.people if p.email),
        key=lambda x: (x["name"], x["email"]),
    )
    return {
        "people": picked,
        "unlinked_accounts": _unlinked_rows(snapshot, index, labels),
        "assignable": assignable,
    }


def _key_console(platform: str) -> str:
    """这朵云自助管理 AccessKey 的控制台地址。认不出的平台返回空串，前端就不画链接。"""
    from . import platforms as platforms_mod

    try:
        return platforms_mod.get(platform).key_console
    except Exception:  # noqa: BLE001 — 新平台还没登记时不该让整页打不开
        return ""


def my_keys(
    person: Optional[Person],
    snapshot: Optional[Snapshot],
    labels: Labels,
    *,
    now: Optional[float] = None,
    stale_days: int = 0,
    unused_days: int = 0,
) -> dict:
    """这个人自己的 AK 台账：每把建了多久、上次什么时候用过。

    **只列本人子账号上的**（`person.accounts`，按 union_id 认出来的那些），和
    `person_detail` 同一份依据 —— 这一页上不该出现任何别人的东西。

    三态在返回值里必须分得开（同 `hygiene.py` 开头那条规矩）：
      · `keys` 里有条目               → 采到了，就是这些
      · `keys` 空且 `uncollected` 空  → 采到了，确实一把都没有
      · `uncollected` 里有            → **这个子账号的 AK 没采到**，不是「他没有 AK」

    混成一个「0 把」的话，采集身份哪天掉了 `ram:ListAccessKeys`，全员都会看到
    「你没有访问密钥」——而那恰恰是最该被发现的状态。

    已停用的 AK 照列（`active=False`、不打标）：体检清单不管它们（换一把停用的
    没有意义），但**台账要让人看见自己有什么**，停用 ≠ 不存在。
    """
    import time

    from . import hygiene

    stale_days = stale_days or hygiene.STALE_KEY_DAYS
    unused_days = unused_days or hygiene.UNUSED_KEY_DAYS
    now = now if now is not None else time.time()

    def days_since(ts: float) -> Optional[int]:
        return int((now - ts) // 86400) if ts else None

    rows: list = []
    uncollected: list = []
    seen = 0
    for ref in person.accounts if person else ():
        user = snapshot.user(ref.platform, ref.account, ref.name) if snapshot else None
        if user is None:
            continue
        seen += 1
        where = {
            "platform": ref.platform,
            "platform_display": labels.platform(ref.platform),
            "account": ref.account,
            "account_label": labels.account(ref.platform, ref.account),
            "user": ref.name,
            # 自助换密钥去哪。**面板只领路不代办** —— 代办要经手用户的 secret，
            # 而 sealed 那一整套存在的理由就是面板不持有明文
            "key_console": _key_console(ref.platform),
        }
        if user.keys is None:
            uncollected.append(dict(where))
            continue
        # 判据只在 inventory.stale_keys / unused_keys 里定义一次，这里不重算阈值：
        # 台账上说「该换了」而体检清单里没有它，两边就都不可信了
        stale = set(user.stale_keys(days=stale_days, now=now))
        unused = set(user.unused_keys(days=unused_days, now=now))
        for k in user.keys:
            flags = []
            # 顺序有意义：没人用的先问「还要不要」，要留着才谈轮换
            if k in unused:
                flags.append("unused")
            if k in stale:
                flags.append("rotate")
            rows.append(
                {
                    **where,
                    # **截断在这里也做一次**：采集那一步已经只存前 8 位，但页面上写着
                    # 「只显示前 8 位」，这个承诺该由渲染方自己保证 —— 将来有人手工拼过
                    # 一份快照、或者新加一朵云的采集忘了截断，那句话就变成假的且没人发现
                    "id": k.id[:8],
                    "status": k.status,
                    "active": k.active,
                    "created": k.created,
                    "age_days": days_since(k.created_ts),
                    "last_used": k.last_used,
                    "idle_days": days_since(k.last_used_ts),
                    # 「这朵云查不到」和「从来没用过」是两件事，前端要分开说
                    "last_used_known": k.last_used_known,
                    "never_used": k.last_used_known and not k.last_used_ts,
                    "flags": flags,
                }
            )
    # 启用的排前面，同组里越老越靠前 —— 最该处理的那把落在第一行
    rows.sort(key=lambda r: (not r["active"], -(r["age_days"] or 0), r["id"]))
    return {
        "captured_at": snapshot.captured_at if snapshot else "",
        "stale_days": stale_days,
        "unused_days": unused_days,
        # 快照里认出来的本人子账号个数。**前端要靠它区分两件事**：
        # 「你有账号但一把密钥都没有」（该说，是好事）和「你压根没有云账号」（不用说）
        "accounts": seen,
        "keys": rows,
        "uncollected": uncollected,
    }


__all__ = ["FILTERS", "Labels", "admin_overview", "admin_people", "my_keys", "person_detail"]
