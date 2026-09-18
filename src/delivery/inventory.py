"""权限快照：云上「谁有什么权限」的离线副本。

为什么看板读快照、不直接调云 API
──────────────────────────────
① 一个能读 RAM/IAM 的看板进程，本身就是值得攻击的东西。读快照意味着看板**根本
   不持有云凭证**——拿下它最多看到一份已经渲染给人看的数据，拿不到往下打的能力。
② 全量拉一次两朵云要几十秒（邮箱得逐个 GetUser）。挂在 HTTP 请求上必然超时。
③ 采集端和展示端能分开部署：采集在有凭证的地方跑，看板可以放在任何地方。

代价是数据有延迟，所以 `Snapshot.captured_at` 是必填字段，页面必须显示它。
「不知道这份数据多旧」比「数据旧」危险得多。

快照格式（采集器产出，见 CLI `delivery inventory`）::

    {
      "captured_at": "2026-09-14T15:00:00+08:00",
      "accounts": [
        {"platform": "aliyun", "account": "default",
         "users": [{"name": ..., "display_name": ..., "email": ...,
                    "policies": [...], "groups": [...]}],
         "groups": [{"name": ..., "display_name": ..., "policies": [...],
                     "members": [...]}]}
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .errors import DeliveryError

#: 视为高危的权限名片段（小写子串匹配）。
#: 只列「能自我提权或等价于超管」的，不列 `ECSFullAccess` 这种业务全权——
#: 把业务全权也标红会让红色失去意义，运维就学会忽略它了。
HIGH_RISK_MARKERS = (
    "administratoraccess",
    "ramfullaccess",
    "iamfullaccess",
)


class InventoryError(DeliveryError):
    """权限快照不合法。"""


@dataclass(frozen=True)
class AccessKey:
    """一把 AK 的台账信息。**不含 secret**，接口本来也不给。

    `id` 只存前 8 位：够在两次采集之间认出是同一把，又不至于把完整 AKId
    写进一个会被传阅的快照文件。
    """

    id: str
    status: str = ""
    created: str = ""
    last_used: str = ""

    @property
    def active(self) -> bool:
        return self.status.lower() == "active"

    @property
    def created_ts(self) -> float:
        return _ts(self.created)

    @property
    def last_used_ts(self) -> float:
        """最后一次使用。阿里云对从没用过的返回 `N/A`，那会解析成 0。"""
        return _ts(self.last_used)


def _ts(iso: str) -> float:
    from datetime import datetime

    text = str(iso or "").strip().replace("Z", "+00:00")
    if not text or text.upper() == "N/A":
        return 0.0
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class UserPermissions:
    platform: str
    account: str
    name: str
    display_name: str = ""
    email: str = ""
    policies: tuple = ()
    groups: tuple = ()
    #: 这个子账号的 AK。`None` 表示**没采到**（采集身份没有 ListAccessKeys 权限），
    #: 空元组才是「确实一把都没有」—— 两者的处置完全不同，不能混成一个
    keys: Optional[tuple] = None

    @property
    def high_risk(self) -> tuple:
        return tuple(p for p in self.policies if is_high_risk(p))

    def stale_keys(self, *, days: int = 180, now: Optional[float] = None) -> tuple:
        """该轮换的 AK：启用中、且建出来超过 `days` 天。

        **只看「建了多久」不看「用没用」** —— 一把天天在用的老 AK 才是最该换的那种，
        按「最近没用过」筛会正好把它漏掉。没用过的是另一类问题（该停用），
        由 `unused_keys` 管。
        """
        import time

        cut = (now if now is not None else time.time()) - days * 86400
        return tuple(
            k for k in (self.keys or ()) if k.active and k.created_ts and k.created_ts < cut
        )

    def unused_keys(self, *, days: int = 90, now: Optional[float] = None) -> tuple:
        """启用中但很久没用过的 AK。从没用过的也算。

        这类不该「提醒轮换」——换一把同样没人用的没有意义。该问的是「还要不要」，
        不要就停用。**停用不是删除**：停用之后如果有人报障，还能立刻改回来。
        """
        import time

        cut = (now if now is not None else time.time()) - days * 86400
        return tuple(
            k
            for k in (self.keys or ())
            if k.active and (not k.last_used_ts or k.last_used_ts < cut)
        )


@dataclass(frozen=True)
class GroupPermissions:
    platform: str
    account: str
    name: str
    display_name: str = ""
    policies: tuple = ()
    members: tuple = ()

    @property
    def high_risk(self) -> tuple:
        return tuple(p for p in self.policies if is_high_risk(p))


@dataclass(frozen=True)
class Snapshot:
    captured_at: str
    users: tuple = ()
    groups: tuple = ()
    #: 采集时出过问题的账号。**不是空列表就说明这份快照不完整**，
    #: 展示层必须显示出来——否则「某人没有权限」和「这个账号没采到」长得一样。
    incomplete: tuple = field(default=())

    @property
    def complete(self) -> bool:
        return not self.incomplete

    def user(self, platform: str, account: str, name: str) -> Optional[UserPermissions]:
        """按「平台 + 云账号 + 用户名」精确取一个子账号。

        不提供按邮箱取：邮箱不是稳定身份（WUJI IAM 接入规范），人和云账号的对应
        一律走人员名册（`people.py`），名册按 union_id 认人。
        """
        for u in self.users:
            if u.platform == platform and u.account == account and u.name == name:
                return u
        return None

    def accounts(self) -> tuple:
        """快照里出现过的 (platform, account)，含采集失败的。"""
        seen = dict.fromkeys((u.platform, u.account) for u in self.users)
        for g in self.groups:
            seen.setdefault((g.platform, g.account), None)
        return tuple(seen)

    def groups_of(self, user: UserPermissions) -> tuple:
        """这个用户所在组的权限。组权限和用户级权限是**叠加**的，只看一边会少算。"""
        wanted = {g.lower() for g in user.groups}
        return tuple(
            g
            for g in self.groups
            if g.platform == user.platform
            and g.account == user.account
            and (g.name.lower() in wanted or user.name in g.members)
        )

    def effective_policies(self, user: UserPermissions) -> tuple:
        """用户级 + 组级，去重后排序。这才是这个人真正能干的事。"""
        seen = dict.fromkeys(user.policies)
        for group in self.groups_of(user):
            for policy in group.policies:
                seen.setdefault(policy, None)
        return tuple(seen)


def is_high_risk(policy: str) -> bool:
    """这条策略是否等价于超管 / 能自我提权。"""
    low = (policy or "").lower()
    return any(m in low for m in HIGH_RISK_MARKERS)


def _keys(raw, who: str) -> Optional[tuple]:
    """AK 清单。**缺这个键表示没采到**（老快照就是这样），空数组才是「一把都没有」。

    两者的处置完全相反：没采到要去查采集身份的权限，一把都没有是好事。
    混成同一个值的话，升级那天所有人都会显示成「零 AK」，而那正是最该被发现的状态。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise InventoryError(f"{who}.keys 必须是数组")
    out = []
    for k in raw:
        if not isinstance(k, dict) or not k.get("id"):
            raise InventoryError(f"{who}.keys 里有缺 `id` 的条目")
        out.append(
            AccessKey(
                id=str(k["id"]),
                status=str(k.get("status") or ""),
                created=str(k.get("created") or ""),
                last_used=str(k.get("last_used") or ""),
            )
        )
    return tuple(out)


def _strs(values, what: str) -> tuple:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise InventoryError(f"{what} 必须是数组")
    out = []
    for v in values:
        if not isinstance(v, str):
            raise InventoryError(f"{what} 里有非字符串项：{v!r}")
        out.append(v)
    return tuple(out)


def parse(data: Mapping) -> Snapshot:
    if not isinstance(data, dict):
        raise InventoryError("快照顶层必须是对象")
    captured = data.get("captured_at")
    if not isinstance(captured, str) or not captured.strip():
        # 没有采集时间的快照不能用：看的人无从判断它是一小时前的还是三个月前的，
        # 而基于陈旧权限数据做的判断比没数据更危险。
        raise InventoryError("快照缺少 `captured_at`，无法判断数据新旧，拒绝加载")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        raise InventoryError("快照缺少 `accounts` 数组")

    users, groups, incomplete = [], [], []
    for idx, acc in enumerate(accounts):
        if not isinstance(acc, dict):
            raise InventoryError(f"accounts[{idx}] 必须是对象")
        platform = str(acc.get("platform") or "").strip()
        account = str(acc.get("account") or "default").strip()
        if not platform:
            raise InventoryError(f"accounts[{idx}] 缺少 `platform`")
        if acc.get("error"):
            incomplete.append(f"{platform}/{account}：{acc['error']}")
            continue
        for u in acc.get("users") or []:
            if not isinstance(u, dict) or not u.get("name"):
                raise InventoryError(f"{platform}/{account} 的 users 里有缺 `name` 的条目")
            users.append(
                UserPermissions(
                    platform=platform,
                    account=account,
                    name=str(u["name"]),
                    display_name=str(u.get("display_name") or ""),
                    email=str(u.get("email") or ""),
                    policies=_strs(u.get("policies"), f"{u['name']}.policies"),
                    groups=_strs(u.get("groups"), f"{u['name']}.groups"),
                    keys=_keys(u.get("keys"), str(u["name"])),
                )
            )
        for g in acc.get("groups") or []:
            if not isinstance(g, dict) or not g.get("name"):
                raise InventoryError(f"{platform}/{account} 的 groups 里有缺 `name` 的条目")
            groups.append(
                GroupPermissions(
                    platform=platform,
                    account=account,
                    name=str(g["name"]),
                    display_name=str(g.get("display_name") or ""),
                    policies=_strs(g.get("policies"), f"{g['name']}.policies"),
                    members=_strs(g.get("members"), f"{g['name']}.members"),
                )
            )
    return Snapshot(
        captured_at=captured.strip(),
        users=tuple(users),
        groups=tuple(groups),
        incomplete=tuple(incomplete),
    )


def load(path: str) -> Snapshot:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InventoryError(f"读不了权限快照 {path}：{exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InventoryError(f"{path} 不是合法 JSON：{exc}") from exc
    return parse(data)


def load_optional(path: Optional[str]) -> Optional[Snapshot]:
    """没配快照路径就返回 None——看板照常起，只是权限区显示「未接入」。

    注意和「文件存在但读不了」的区别：那种要抛，不能降级成「没有权限数据」。
    """
    if not path:
        return None
    return load(path)


def high_risk_holders(snapshot: Snapshot) -> Sequence:
    """所有持有高危权限的人，用户级和组级都算。管理员视图的主表。"""
    out = []
    for user in snapshot.users:
        direct = user.high_risk
        via_group = tuple(
            f"{p}（经组 {g.name}）" for g in snapshot.groups_of(user) for p in g.high_risk
        )
        if direct or via_group:
            out.append((user, direct + via_group))
    out.sort(key=lambda x: (-len(x[1]), x[0].platform, x[0].name))
    return out
