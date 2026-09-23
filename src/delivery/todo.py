"""待办：把「现在轮到你处理什么」算成一张有序的清单。

为什么要有这一层
────────────────
管理后台有八页，每页回答一个问题，但**没有一页回答「现在该干什么」**。
离职待确认在 IAM 属性表页、开通失败在申请页、无主账号在人员页和体检页各有一份、
密钥老化在体检页 —— 一件事散在几页，结果是：除非管理员挨页翻，否则什么都不会被处理。

**排序和分组算在服务端**：飞书卡片、命令行、网页要给出同一个「最要紧的是什么」。
放前端的话，三处各写一份，迟早互相矛盾，而矛盾那天没人知道该信哪个。

分组只看后果，不看数据来源
──────────────────────────
  · `URGENT` 安全敞口（离职的人还能登）或有人被卡住（开通失败、登不进去）
  · `NORMAL` 台账漂移（对应不上、没登记、增量没发）—— 不处理不会立刻出事，但会越积越乱
  · `CHORE`  卫生（密钥老化）—— 该做，但不该抢在前两类前面

**依据旧了就降级**：比如这一轮没查飞书在职状态，那么基于「通讯录里找不到」的事项
一律降到 `NORMAL` 并标注「本次没查，不作数」。拿不到依据却排在第一位，
和体检那条「查不了就不判断离职」的规矩正好相反。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

URGENT = "urgent"
NORMAL = "normal"

#: 单子已经走完的状态。走到这里还留着 `cred_user`，说明云上那个号没收掉。
#: 用字面量而不是 import tickets：todo 是纯展示层，不该反向依赖状态机。
#:
#: **刻意不含 `revoked`，别顺手补上去。** `cred_user` 记的是「当初建的号叫什么」，
#: 不是「云上还有没有这个号」—— `flows._mark_revoked` 收成功后并不清它。
#: 把 `revoked` 加进来，历史上每一张**正常收回**的凭证单都会变成「云上的号还在」，
#: 而且是 URGENT。判据向 `flows._needs_reclaim` 对齐（它认的也只有 FAILED/CLOSED）。
_CLOSED_STATES = ("closed", "failed")
CHORE = "chore"
#: 展示顺序，也是严重度顺序
GROUPS = (URGENT, NORMAL, CHORE)
GROUP_NAMES = {URGENT: "要紧的", NORMAL: "该处理的", CHORE: "顺手做的"}

#: 同一类超过这么多条就折成一行汇总。待办页一屏看得完才叫待办页
FOLD_AFTER = 5

#: 各数据源多旧算旧（秒）。超了就在「数据可信度」里标黄，并给依赖它的事项降级
STALE = {
    "snapshot": 24 * 3600,
    "reconcile": 3 * 24 * 3600,
    "offline": 30 * 24 * 3600,
    "assets": 3 * 24 * 3600,
}


@dataclass
class Item:
    """一条待办。`what` 写「发生了什么 + 不处理会怎样」，不写状态词。"""

    kind: str
    group: str
    title: str
    what: str
    #: 主操作的文案和去处（面板内 hash 路由）
    action: str = ""
    href: str = ""
    #: 这条结论依据的是哪份数据、采于何时
    source: str = ""
    source_at: str = ""
    #: 事项自己的时间戳（秒）。越久没处理排越前
    since: Optional[float] = None
    #: 能一次处理一批的排前面 —— 让人点一次清一批，而不是被一条条的事耗掉耐心
    batch: bool = False
    #: 依据不足时标上，前端标灰
    weak: str = ""
    count: int = 1

    def row(self) -> dict:
        return {
            "kind": self.kind,
            "group": self.group,
            "title": self.title,
            "what": self.what,
            "action": self.action,
            "href": self.href,
            "source": self.source,
            "source_at": self.source_at,
            "age_days": None
            if self.since is None
            else max(0, int((time.time() - self.since) / 86400)),
            "batch": self.batch,
            "weak": self.weak,
            "count": self.count,
        }


@dataclass
class Report:
    items: list = field(default_factory=list)
    #: 每份数据源的新鲜度：`{名字: {"at": ISO, "stale": bool, "note": str}}`
    freshness: dict = field(default_factory=dict)
    #: 哪一类没算成。**不是空列表就说明这张清单不完整**
    errors: list = field(default_factory=list)

    def add(self, item: Item) -> None:
        self.items.append(item)

    def note_source(self, name: str, at: str, *, stale: bool = False, note: str = "") -> None:
        self.freshness[name] = {"at": at, "stale": stale, "note": note}

    def view(self) -> dict:
        rows = [i.row() for i in sorted(self.items, key=_order)]
        groups = []
        for g in GROUPS:
            mine = [r for r in rows if r["group"] == g]
            if mine:
                groups.append({"group": g, "name": GROUP_NAMES[g], "items": mine})
        urgent = sum(r["count"] for r in rows if r["group"] == URGENT)
        normal = sum(r["count"] for r in rows if r["group"] == NORMAL)
        return {
            "groups": groups,
            "counts": {"urgent": urgent, "normal": normal, "total": sum(r["count"] for r in rows)},
            "headline": _headline(urgent, normal, self.errors),
            "freshness": self.freshness,
            "errors": self.errors,
            "fold_after": FOLD_AFTER,
        }


def _order(item: Item) -> tuple:
    """分组 → 挂了多久（久的在前）→ 能不能批量（能的在前）。

    `since` 是**时间戳**，越老的数越小，所以直接升序就是「久的在前」。
    曾经写成 `-since`，结果今天刚出现的排在第一位 —— 而这一页的全部意义
    就是「最该先处理的排第一」。没有时间戳的沉底（不知道挂了多久，不能假装它很急）。
    """
    return (
        GROUPS.index(item.group) if item.group in GROUPS else len(GROUPS),
        item.since if item.since is not None else float("inf"),
        0 if item.batch else 1,
        item.kind,
    )


def _headline(urgent: int, normal: int, errors) -> str:
    if errors and not (urgent or normal):
        return "有几类没算出来，见下面的说明 —— 不代表没有要处理的事。"
    if not (urgent or normal):
        return "没有需要你处理的。"
    bits = []
    if urgent:
        bits.append(f"{urgent} 件要紧的")
    if normal:
        bits.append(f"{normal} 件可以顺手处理的")
    return "今天有 " + "、".join(bits) + "。"


def weaken(item: Item, why: str) -> Item:
    """依据不足：降到「该处理的」并标注。**不是删掉** —— 提醒还要给，只是不占第一位。"""
    item.weak = why
    if item.group == URGENT:
        item.group = NORMAL
    return item


def days_ago(iso: str, now: Optional[float] = None) -> Optional[float]:
    """ISO 时间 → 秒级时间戳。认不出返回 None（调用方据此当作「不知道多旧」）。"""
    text = str(iso or "").strip()
    if not text:
        return None
    import datetime

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            got = datetime.datetime.strptime(text[:32], fmt)
        except ValueError:
            continue
        if got.tzinfo is None:
            got = got.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
        return got.timestamp()
    return None


def stale(name: str, iso: str, now: Optional[float] = None) -> bool:
    """这份数据是不是旧到不该拿来下结论了。认不出时间一律当旧的 —— 见 hygiene 的同款规矩。"""
    at = days_ago(iso)
    if at is None:
        return True
    limit = STALE.get(name, 3 * 24 * 3600)
    return ((now if now is not None else time.time()) - at) > limit


# ── 数据源 → 待办项 ────────────────────────────────────────────────────────
#
# 每个 collect_* 只认它那一份数据，**任何一个读失败只让那一类缺席**（调用方记进 errors），
# 不让整张清单打不开。这和体检里「拿不到就跳过并说明」是同一条规矩。


def collect_offboard(report: Report, pending: list, now: Optional[float] = None) -> None:
    """离职待处理。按人归拢：一个人名下几个号是一件事，不是三件。"""
    by_person: dict = {}
    for r in pending or ():
        by_person.setdefault(r.get("person") or r.get("user") or "", []).append(r)
    for who, mine in by_person.items():
        manual = [r for r in mine if r.get("platform") in ("jiuzhang",)]
        auto = [r for r in mine if r not in manual]
        # 只拿认得出来的时间算。有一条脏时间戳就 `or 0 → or None` 的写法会把整条
        # 待办的年龄抹掉，而没有年龄的项排在最后 —— 挂了 30 天的离职会沉到最底
        seen = [x for x in (days_ago(r.get("at", "")) for r in mine) if x]
        oldest = min(seen) if seen else None
        if auto:
            stopped = sum(1 for r in auto if r.get("state") == "disabled")
            report.add(
                Item(
                    kind="offboard_pending",
                    group=URGENT,
                    title=f"{who}：{len(auto)} 个云账号等着删",
                    what=(
                        f"已停用 {stopped} 个，停用可逆、删号才算收干净。"
                        if stopped
                        else "面板还没停过这些号，他现在仍然能登。"
                    ),
                    action="去处理",
                    href="#admin/iam",
                    source="离职记录",
                    source_at=str(mine[0].get("at") or ""),
                    since=oldest,
                    batch=len(auto) > 1,
                    count=len(auto),
                )
            )
        if manual:
            report.add(
                Item(
                    kind="offboard_manual",
                    group=NORMAL,
                    title=f"{who}：九章账号要你去控制台停",
                    what="九章没有接口，面板动不了它。停完回来点一下销账，它才会从待办里消失。",
                    action="去销账",
                    href="#admin/iam",
                    source="离职记录",
                    source_at=str(manual[0].get("at") or ""),
                    since=oldest,
                    count=len(manual),
                )
            )


def collect_iam_drift(report: Report, cached: dict, held: set) -> None:
    """IT 的 IAM 说已离职、云登录名还挂着。**他现在还能 SSO 进控制台**，所以是要紧的。"""
    rows = [
        (e, d)
        for e in (cached.get("apps") or ())
        for d in (e.get("drift") or ())
        if d.get("kind") == "inactive"
        and f"{e.get('app')}/{d.get('union_id')}" not in (held or set())
    ]
    if not rows:
        return
    at = str(cached.get("checked_at") or "")
    names = "、".join(sorted({str(d.get("name") or d.get("username") or "") for _e, d in rows})[:3])
    item = Item(
        kind="iam_inactive",
        group=URGENT,
        title=f"{len(rows)} 个云登录名还挂在已离职的人身上",
        what=f"{names} 等人在公司 IAM 里已标离职，登录名没回收，他们现在还能 SSO 进控制台。",
        action="确认离职并回收",
        href="#admin/iam",
        source="IAM 对账",
        source_at=at,
        since=days_ago(at),
        batch=len(rows) > 1,
        count=len(rows),
    )
    # 依据旧了就降级：一份 12 天前的对账结果不该排在今天刚发生的事情前面，
    # 更不该把导航上那个红点点亮 —— 红点是拿过期数据点亮的话，它就不再有意义
    if stale("reconcile", at):
        item = weaken(item, "对账数据旧了，先刷新一次再动手；这条暂不算要紧的")
    report.add(item)


def collect_tickets(report: Report, rows: list) -> None:
    """申请单：开通失败、登录名没写进 IAM、卡住不动。**这三类都有人在等**。

    `submit_failed`（提交给审批那一步就断了）也算 —— 它和 `failed` 一样是「有人在等、
    而且不会自己好」，但它**不会推飞书**（`flows.recover_stuck` 只在 `executing` 分支
    调 `_emit`）。只挑 `failed` 的话，这类单子在任何地方都不会出现（审计 Med-1）。
    """
    stuck_states = ("failed", "submit_failed")
    failed = [t for t in rows or () if t.get("state") in stuck_states]
    if failed:
        oldest = min((days_ago(t.get("created_at", "")) or 0) for t in failed) or None
        report.add(
            Item(
                kind="request_failed",
                group=URGENT,
                title=f"{len(failed)} 张单审批过了但开通失败",
                what="申请人以为在走流程，其实卡住了。看一眼失败原因，能重试的重试。",
                action="去处理",
                href="#admin/requests?state=failed",
                source="申请单",
                source_at=str(failed[0].get("created_at") or ""),
                since=oldest,
                count=len(failed),
            )
        )
    waiting = [t for t in rows or () if t.get("needs_iam")]
    if waiting:
        report.add(
            Item(
                kind="request_no_iam",
                group=URGENT,
                title=f"{len(waiting)} 个号建好了，登录名还没发给 IT",
                what="号是开出来了，但公司 IAM 里没有他的登录名 —— 他登不进去，会来问你。",
                action="补写登录名",
                href="#admin/iam",
                source="申请单",
                source_at="",
                since=None,
                batch=len(waiting) > 1,
                count=len(waiting),
            )
        )


def collect_expiring(report: Report, rows: list, now: Optional[float] = None) -> None:
    """快到期和已经过期的凭证 / 权限。

    **飞书私聊是提醒本人换，这一条是提醒你这个管理员**：本人可能在休假、可能已经离职，
    而到期那天断的是服务。两边说的是同一件事，但收件人不同，少哪一边都会漏。
    """
    at = now if now is not None else time.time()

    # **单子已经走完、云上那个子账号还在 = 现在就该看，和到期时间无关。**
    # `flows._needs_reclaim` 对这类单子根本不看 `expires_at_ts`，定时任务从关单那天起
    # 每分钟试删一次；而这里要是按到期时间筛，「今天关掉的 90 天凭证单」要等 83 天
    # 才在页面上出现。也不能把它塞进「已过期」那一类 —— 它没过期，那是句不准的话。
    def _orphan(r) -> bool:
        return bool(r.get("cred_user")) and r.get("state") in _CLOSED_STATES

    orphan = [r for r in rows or () if _orphan(r)]
    # 取反判据，不写 `r not in orphan` —— 那是 O(n²)，而且「不会误剔」要靠
    # 「判据是行内容的纯函数」这段推理才成立。读的人不该被要求做这段推理（审计 Low-4）
    rest = [r for r in rows or () if not _orphan(r)]
    soon = [r for r in rest if at < (r.get("expires_at_ts") or 0) <= at + 7 * 86400]
    dead = [r for r in rest if 0 < (r.get("expires_at_ts") or 0) <= at]
    if orphan:
        report.add(
            Item(
                kind="cred_orphan",
                group=URGENT,
                title=f"{len(orphan)} 张凭证的单子已经关了，云上的号还在",
                what=(
                    "单子走完了，子账号和它那把长期 AK 还留在云上 —— 定时任务在试着删，"
                    "删不掉才是要你看的。收不掉的原因通常是号被手工改过（改了名、挂了别的策略）。"
                ),
                action="去看",
                href="#admin/requests",
                source="申请单",
                source_at="",
                batch=len(orphan) > 1,
                count=len(orphan),
            )
        )
    if soon:
        first = min(soon, key=lambda r: r.get("expires_at_ts") or 0)
        days = max(0, int(((first.get("expires_at_ts") or at) - at) / 86400))
        who = str(first.get("who") or "")
        report.add(
            Item(
                kind="cred_expiring",
                group=NORMAL,
                title=f"{len(soon)} 张凭证 7 天内到期，最近的还有 {days} 天",
                what=(
                    f"最早到期的是{'：' + who if who else '那张'}。"
                    "服务在用的凭证到点会直接断 —— 该续的续，不用了就让它过期。"
                ),
                action="去看",
                href="#admin/requests",
                source="申请单",
                source_at="",
                batch=len(soon) > 1,
                count=len(soon),
            )
        )
    if dead:
        report.add(
            Item(
                kind="cred_expired",
                group=NORMAL,
                title=f"{len(dead)} 张凭证已经过期，还没收回",
                what="过期的凭证留在云上就是一把没人管的密钥。定时任务会收，收不掉的要人看一眼。",
                action="去看",
                href="#admin/requests",
                source="申请单",
                source_at="",
                batch=len(dead) > 1,
                count=len(dead),
            )
        )


def collect_roster(report: Report, pending_links: int, unlinked: int, filtered: int = 0) -> None:
    """名册：待确认的对应、对不上人的号。"""
    if pending_links:
        report.add(
            Item(
                kind="mapping_review",
                group=NORMAL,
                title=f"{pending_links} 个号推断出了主人，等你确认",
                what="没确认就不会写进 IAM 属性表，那个人也就 SSO 进不去。",
                action="去确认",
                href="#admin",
                source="名册",
                source_at="",
                batch=True,
                count=pending_links,
            )
        )
    if unlinked:
        tail = f"（另有 {filtered} 个程序发的号没算在内）" if filtered else ""
        report.add(
            Item(
                kind="unlinked_account",
                group=NORMAL,
                title=f"{unlinked} 个云账号对不上任何人{tail}",
                what="出了事找不到人，那个人离职时也漏得掉 —— 名册里根本没有他这个号。",
                action="去认领",
                href="#admin",
                source="名册",
                source_at="",
                batch=True,
                count=unlinked,
            )
        )


def collect_iam_files(report: Report, preview: dict) -> None:
    """属性表：有变化没发、发出去没回确认。"""
    counts = (preview.get("increment") or {}).get("counts") or {}
    changes = int(counts.get("set") or 0) + int(counts.get("remove") or 0)
    if changes:
        removes = int(counts.get("remove") or 0)
        tail = f"，其中 {removes} 行是删号" if removes else ""
        report.add(
            Item(
                kind="iam_increment",
                group=NORMAL,
                title=f"{changes} 行属性变化还没发给 IT{tail}",
                what="不发的话，新人登不进去、离职的人属性一直留在公司 IAM 里。",
                action="生成增量",
                href="#admin/iam",
                source="属性表",
                source_at="",
                batch=True,
                count=changes,
            )
        )
    for row in (preview.get("baseline") or {}).get("pending") or ():
        at = str(row.get("created_at") or row.get("at") or "")
        when = days_ago(at)
        if when is None or (time.time() - when) < 7 * 86400:
            continue
        report.add(
            Item(
                kind="iam_stale_pending",
                group=NORMAL,
                title="有一份增量发出去很久了，IT 还没回确认",
                what="确认之前基线不动，之后每次算出来的差异都是虚的 —— 去催一下，或者作废重发。",
                action="去看",
                href="#admin/iam",
                source="属性表",
                source_at=at,
                since=when,
            )
        )


def collect_keys(report: Report, rotate: int, unused: int) -> None:
    """密钥卫生。**汇总成一行**：十几条密钥提醒会把真正要紧的事挤没。"""
    if not (rotate or unused):
        return
    bits = []
    if rotate:
        bits.append(f"{rotate} 把该换了")
    if unused:
        bits.append(f"{unused} 把没人用")
    report.add(
        Item(
            kind="keys",
            group=CHORE,
            title="密钥：" + "、".join(bits),
            what="提醒本人换，别代劳 —— 新旧两把并存一段时间才不会断服务。",
            action="去资产页",
            href="#admin/hygiene",
            source="体检",
            source_at="",
            batch=True,
            count=rotate + unused,
        )
    )


def collect_workspaces(report: Report, rows: list) -> None:
    """云上有、登记表里没有的工作空间。`rows` 来自 `assets.unregistered_workspaces`。

    **按工作空间 ID，不按地域**：一个地域可以有好几个工作空间（线上杭州就有两个），
    按地域比的话，只要那个地域登记过一个，其余的全被算成已登记而漏掉。
    """
    if not rows:
        return
    names = "、".join(w.get("name") or w.get("id") or "" for w in rows[:3])
    report.add(
        Item(
            kind="workspace_unregistered",
            group=NORMAL,
            title=f"{len(rows)} 个工作空间没登记：{names}",
            what=(
                "面板不知道它们存在 —— 申请页上选不到、新开的号不会自动进去、"
                "人走了也扫不到里面的权限。要用就登记进 workspaces.json，不用就清掉。"
            ),
            action="看资产",
            href="#admin/assets",
            source="资产快照",
            source_at="",
            count=len(rows),
        )
    )


def collect_regions(report: Report, rows: list) -> None:
    """有资源、但面板没登记的地域。`rows` 来自 `assets.unregistered_regions`。

    **这不是「该纳管」，是「你知道那儿有东西吗」。** 没登记的地域意味着：
    那里的机器和桶不在任何申请流程里、没人被指为属主、离职回收也扫不到 ——
    出了事找不到人，而面板连它存在都不知道。
    """
    if not rows:
        return
    where = "、".join(w.split("/", 1)[-1] for w, _ in rows[:4])
    total = sum(sum(kinds.values()) for _, kinds in rows)
    report.add(
        Item(
            kind="region_unregistered",
            group=NORMAL,
            title=f"{len(rows)} 个地域有 PAI 工作空间，但没登记：{where}",
            what=(
                f"那里有 {total} 个 PAI 工作空间 / 数据集，而面板不知道它们存在 ——"
                "申请页上选不到、新开的号不会自动进去、人走了也扫不到。"
                "要用就登记进 workspaces.json，不用就清掉。"
            ),
            action="看资产",
            href="#admin/assets",
            source="资产快照",
            source_at="",
            count=len(rows),
        )
    )


def collect_config(report: Report, checks) -> None:
    """配置缺失卡住整条链路的那种。来自系统状态里的 crit。"""
    bad = [c for c in checks or () if getattr(c, "level", "") == "crit"]
    for c in bad[:3]:
        report.add(
            Item(
                kind="config_blocking",
                group=URGENT,
                title=str(getattr(c, "title", "") or "配置缺失"),
                what=str(getattr(c, "detail", "") or "这一项没配好，相关功能发不出去。"),
                action="看系统状态",
                href="#admin/health",
                source="系统状态",
                source_at="",
            )
        )
