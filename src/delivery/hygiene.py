"""该有人看一眼的东西：人走了号还在、AK 太久没换、AK 根本没人用。

**这个模块只算清单，不做任何处置。** 停用一把 AK、收一个账号，都要人点头 ——
自动做的话，第一次误伤就会让所有人学会忽略这类通知，那比不做还糟。

三类各自的判据都写在对应函数里，这里先说共同的一条：
**「没采到」绝不能算成「没有问题」**。快照不完整、通讯录没拉全的时候，这些函数一律
返回空清单并说明原因 —— 报一个建立在残缺数据上的结论，比什么都不报危险得多：
通讯录拉一半就说「这 30 个人都离职了」，照着做一遍公司就停摆了。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from . import inventory

#: AK 建出来多久算该轮换。看的是**年龄**不是「最近用没用」——
#: 一把天天在用的老 AK 才是最该换的那种
STALE_KEY_DAYS = 180
#: 多久没用过算「没人用」。从没用过的也算。这类不该提醒轮换 ——
#: 换一把同样没人用的没有意义，该问的是「还要不要」
UNUSED_KEY_DAYS = 90
#: 程序发出去的临时凭证子账号。它们**有到期时间、由清理任务负责**，不是「无主」——
#: 混进无主清单里会让那一栏的一半都是噪音，而一份一半是噪音的清单没人会看第二遍
_ISSUED_PREFIXES = ("tempak-", "temp-ak-", "panel-")


@dataclass(frozen=True)
class Finding:
    """一条待人处理的线索。`why` 是给人看的一句话，不是错误码。"""

    kind: str
    platform: str
    account: str
    subject: str
    why: str
    owner: str = ""
    detail: str = ""

    @property
    def scope(self) -> str:
        return f"{self.platform}/{self.account}/{self.subject}"


#: 五类各自的标题和那句给人看的话。**只在这里写一份** —— 命令行和面板是同一批结论，
#: 各写各的迟早会出现「网页上说该停用、命令行说该轮换」这种自相矛盾的提示。
#: 顺序就是展示顺序：人的问题排在密钥问题前面。
#: 标题和说明会过一次 `str.format`（填阈值），所以**正文里不能出现裸的花括号**。
_SECTIONS = (
    (
        "left",
        "人已经不在通讯录里，云账号还在",
        "确认离职后再收。名字对不上也可能是没绑 union_id，别直接删。",
    ),
    (
        "unknown",
        "飞书里查不到这个人",
        "可能已离职，也可能只是不在应用可用范围内 —— 飞书两种情况回同一个错误码。"
        "**先确认再动手**。",
    ),
    ("orphan", "认不出属主的云账号", "出了事找不到人，这批最该先补上归属。"),
    (
        "rotate",
        "AK 建出来超过 {stale_days} 天，该换了",
        "提醒本人换，别代劳：新旧两把并存一段时间才不会断服务。",
    ),
    (
        "unused",
        "AK 超过 {unused_days} 天没用过",
        "这批该问「还要不要」。不要就**停用**（可逆），别直接删。",
    ),
)


@dataclass
class Report:
    #: 人已经不在通讯录里，云账号还在
    left: list = field(default_factory=list)
    #: AK 该轮换
    rotate: list = field(default_factory=list)
    #: AK 没人用
    unused: list = field(default_factory=list)
    #: 认不出属主的云账号（最该先处理的那批：出了事找不到人）
    orphan: list = field(default_factory=list)
    #: 在飞书里**查不到**的人。注意：飞书对「不在应用可用范围内」和「已被移出通讯录」
    #: 回的是同一个错误码，所以这批只能是「要人确认」，不能当成已离职
    unknown: list = field(default_factory=list)
    #: 为什么某一类没算出来。**不是空列表就说明这份清单不完整**
    skipped: list = field(default_factory=list)
    #: 这次实际用的阈值。**要记在报告里**：`--stale-days 30` 跑出来的清单，
    #: 标题却印着默认的 180 天，看的人会以为这批 AK 老得多、按错的紧迫度处理
    stale_days: int = STALE_KEY_DAYS
    unused_days: int = UNUSED_KEY_DAYS

    @property
    def total(self) -> int:
        return (
            len(self.left)
            + len(self.rotate)
            + len(self.unused)
            + len(self.orphan)
            + len(self.unknown)
        )

    def sections(self) -> list:
        """[(kind, 标题, 说明, 条目), ...]，空的那几类也在里面（计数用）。"""
        fmt = {"stale_days": self.stale_days, "unused_days": self.unused_days}
        return [
            (kind, title.format(**fmt), note.format(**fmt), getattr(self, kind))
            for kind, title, note in _SECTIONS
        ]

    def render(self) -> str:
        lines = ["云账号体检"]
        for _, title, note, items in self.sections():
            if not items:
                continue
            lines.append(f"\n{title}（{len(items)}）")
            lines.append(f"  {note}")
            for f in items:
                who = f" · {f.owner}" if f.owner else ""
                lines.append(f"  · {f.scope}{who}")
                lines.append(f"      {f.why}" + (f"（{f.detail}）" if f.detail else ""))
        for note in self.skipped:
            lines.append(f"\n⚠ {note}")
        if not self.total and not self.skipped:
            lines.append("\n没有发现需要处理的。")
        return "\n".join(lines)


def load_service_names(path: Optional[str]) -> list:
    """`identity/services.json` 里人工登记的服务号清单。没配就是空清单。

    **坏文件要抛，不能当空清单**：读成空 = 那几十个服务号全部涌进「无主」那一栏，
    而一份一半是噪音的清单没人会看第二遍。缺文件才是正常的初始状态。
    """
    import json
    from pathlib import Path

    from .errors import DeliveryError

    if not path or not Path(path).exists():
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"读不了服务号名单 {path}：{exc}") from exc
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise DeliveryError(f'{path} 格式应为 {{"names": ["服务号", ...]}}')
    return [n.strip() for n in names]


def _is_service(name: str, services: set) -> bool:
    """服务号（程序用的，本来就没有「属主」这个概念）。

    两个来源：`identity/services.json` 里人工登记的清单，以及程序自己发的临时凭证号。
    它们不进「无主」那一栏 —— 但**AK 那两栏照样管它们**：一把服务号的 AK 放两年不换，
    和一个人的一样危险，甚至更危险，因为没人会主动想起它。
    """
    low = name.lower()
    return low in services or low.startswith(_ISSUED_PREFIXES)


#: 飞书用户状态里，哪几种算「这个人已经不该有云账号了」。
#: 三者含义不同，分开说出来 —— 「冻结」和「离职」的处置不一样
_GONE = (
    ("is_resigned", "已离职"),
    ("is_exited", "已退出企业"),
    ("is_frozen", "账号被冻结"),
)


def _gone(status: Mapping) -> str:
    """这个人是不是已经不该留着云账号了。返回一句话，在职返回空串。"""
    for key, label in _GONE:
        if status.get(key):
            return f"飞书状态：{label}"
    return ""


def _owner_of(person) -> str:
    if person is None:
        return ""
    return f"{person.name} {person.email}".strip()


def build(
    snapshot: Optional[inventory.Snapshot],
    people: Iterable,
    *,
    directory_uids: Optional[set] = None,
    statuses: Optional[Mapping] = None,
    services: Optional[Iterable] = None,
    now: Optional[float] = None,
    stale_days: int = STALE_KEY_DAYS,
    unused_days: int = UNUSED_KEY_DAYS,
) -> Report:
    """算出待办清单。

    `directory_uids` 是**当前在职**的人的 union_id 集合（通讯录里已离职的会被跳过，
    见 identity/directory.py）。给 `None` 表示这次没拿到通讯录 —— 那就不算离职那一类，
    并在 `skipped` 里说明。拿不到通讯录却照算，等于把全公司报成离职。
    """
    report = Report(stale_days=stale_days, unused_days=unused_days)
    now = now if now is not None else time.time()
    known_services = {str(x or "").lower() for x in (services or ())} - {""}

    if snapshot is None:
        report.skipped.append("没有权限快照，AK 和归属这两类都没法算")
        return report
    if not snapshot.complete:
        # 采集失败的账号里，用户列表是空的 —— 照算会把那个账号的所有人报成「已离职」
        report.skipped.append(
            f"权限快照不完整（{'、'.join(snapshot.incomplete[:3])}），本次只看采到的部分"
        )

    by_account: dict = {}
    for person in people:
        for ref in getattr(person, "accounts", ()):
            by_account[(ref.platform, ref.account, ref.name)] = person

    for user in snapshot.users:
        key = (user.platform, user.account, user.name)
        person = by_account.get(key)
        owner = _owner_of(person)

        if person is None and _is_service(user.name, known_services):
            # 服务号不进无主清单，但下面的 AK 检查照常 —— 服务号的 AK 才最容易被忘掉
            pass
        elif person is None:
            report.orphan.append(
                Finding(
                    kind="orphan",
                    platform=user.platform,
                    account=user.account,
                    subject=user.name,
                    why="名册里认不出这个号是谁的",
                    detail=user.display_name or "",
                )
            )
        elif person.union_id and statuses is not None and person.union_id in statuses:
            st = statuses[person.union_id]
            akn = f"{len(user.keys or ())} 把 AK" if user.keys else "没有 AK"
            if st is None:
                report.unknown.append(
                    Finding(
                        kind="unknown",
                        platform=user.platform,
                        account=user.account,
                        subject=user.name,
                        owner=owner,
                        why="飞书里查不到这个人（不在应用可用范围内，或已被移出通讯录）",
                        detail=akn,
                    )
                )
            elif _gone(st):
                report.left.append(
                    Finding(
                        kind="left",
                        platform=user.platform,
                        account=user.account,
                        subject=user.name,
                        owner=owner,
                        why=_gone(st),
                        detail=akn,
                    )
                )

        for k in user.stale_keys(days=stale_days, now=now):
            report.rotate.append(
                Finding(
                    kind="rotate",
                    platform=user.platform,
                    account=user.account,
                    subject=user.name,
                    owner=owner,
                    why=f"AK {k.id[:8]}… 建于 {k.created[:10] or '未知'}",
                    detail=f"最近用过：{k.last_used[:10] or '从没用过'}",
                )
            )
        for k in user.unused_keys(days=unused_days, now=now):
            report.unused.append(
                Finding(
                    kind="unused",
                    platform=user.platform,
                    account=user.account,
                    subject=user.name,
                    owner=owner,
                    why=f"AK {k.id[:8]}… "
                    + ("从来没用过" if not k.last_used_ts else f"最后一次用是 {k.last_used[:10]}"),
                    detail=f"建于 {k.created[:10] or '未知'}",
                )
            )

    if statuses is None and directory_uids is None:
        report.skipped.append("没查飞书在职状态，这次不判断谁离职了")
    elif statuses is not None and not statuses:
        # **查了、但一个人都查不成**（名册里没有 union_id —— 没登录过面板的人就是这样）。
        # 不说的话：`render()` 打出「没有发现需要处理的」、`_hygiene` 退出码 0、
        # `summary()["incomplete"]` 为 False —— 定时任务据此判定「一切正常」，
        # 而实际上离职这一类**一个人都没查**
        report.skipped.append("在职状态一个人都没查到（名册里没有 union_id），本次不判断谁离职")
    if all(u.keys is None for u in snapshot.users) and snapshot.users:
        report.skipped.append("一个账号的 AK 都没采到 —— 检查采集身份有没有 ram:ListAccessKeys")

    for bucket in (report.left, report.orphan, report.rotate, report.unused):
        bucket.sort(key=lambda f: (f.platform, f.account, f.subject))
    return report


def directory_uids(entries: Iterable) -> set:
    """通讯录里还在职的人的 union_id。`identity/directory.py` 已经跳过了离职的。"""
    return {str(getattr(e, "union_id", "") or "") for e in entries} - {""}


def summary(report: Report) -> Mapping:
    """给飞书卡片/接口用的计数。"""
    return {
        "left": len(report.left),
        "orphan": len(report.orphan),
        "rotate": len(report.rotate),
        "unused": len(report.unused),
        "incomplete": bool(report.skipped),
    }


def view(report: Report) -> dict:
    """同一份结论的 JSON 形态，给面板的只读接口用。

    `skipped` 照原样带出去，**不能在接口这一层丢掉**：网页上只显示条目、不显示
    「这次没算某一类」的话，一份残缺清单看起来会和一份干净清单一模一样。
    """
    return {
        "summary": dict(summary(report)),
        "total": report.total,
        "stale_days": report.stale_days,
        "unused_days": report.unused_days,
        "sections": [
            {
                "kind": kind,
                "title": title,
                "note": note,
                "count": len(items),
                "items": [
                    {
                        "platform": f.platform,
                        "account": f.account,
                        "subject": f.subject,
                        "scope": f.scope,
                        "owner": f.owner,
                        "why": f.why,
                        "detail": f.detail,
                    }
                    for f in items
                ],
            }
            for kind, title, note, items in report.sections()
        ],
        "skipped": list(report.skipped),
    }
