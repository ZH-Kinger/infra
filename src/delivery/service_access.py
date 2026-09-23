"""某个人能不能用某个自建服务。**纯逻辑，不碰网络、不读文件。**

用在哪
──────
MLflow 这类自建服务前面有个网关（`tensorboard.wuji-tech.com` 那台），nginx 用
`auth_request` 让它对**每一个请求**回答一次「这个人能不能进」。它现在已经会查
「在不在禁用名单」，这里要回答的是新的一问：「面板批准过他用这个服务吗」。

判据就是申请单本身，不另存一份名单
──────────────────────────────────
**没有 `identity/service-access.json` 这种东西。** 两处真相只会被修一边，而漏的那次
表现是「单子撤了、人还能进」—— 没有任何地方会报错，也没有任何人会发现。
所以「能不能用」= 这个人名下有没有一张「已完成、没过期、没被撤」的服务访问单。

离职接在这里，不接在撤单那边
────────────────────────────
离职的人直接拒，**不依赖有人去把他的单子撤掉**。靠「离职时记得撤单」的话，
漏掉的那一次没有任何人会发现 —— 而那一次正是最不该漏的。

fail 的方向和「禁用名单」相反
─────────────────────────────
网关里已有的 `is_disabled()` 查库失败时返回 False（放行），那是对的：禁用名单查不到
不该误伤正常人。但**授权名单必须相反** —— 查不到就拒绝，否则面板一挂，这道门就等于
不存在。这两个相邻的函数一个 fail-open 一个 fail-closed，是**有意的**，别顺手统一。
（本模块只做判定；真正的 fail-closed 在调用方 —— 它拿不到结果时不该放行。）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

#: 拒绝的原因。给网关渲染不同的页面用 —— 三种情形下人该做的事完全不同
NO_GRANT = "no_grant"  # 没申请过 / 已过期 / 已撤销 → 给他申请入口
NOT_IN_ROSTER = "not_in_roster"  # 名册里查无此人 → 是数据没同步，不是他的错
BLOCKED = "blocked"  # 离职或被停用 → 不给申请入口


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    #: 批准这次访问的那张单子（给台账和排查用，**不回给网关**）
    ticket_id: str = ""
    #: 到期时间（epoch 秒），0 = 不设期限
    expires_at: float = 0.0

    @property
    def message(self) -> str:
        """给人看的一句话。**三种拒绝说的不是同一件事**，混成一句会让人做错事：
        没申请的该去申请，名册没同步的该找管理员，离职的谁也帮不了他。"""
        if self.allowed:
            return ""
        if self.reason == NOT_IN_ROSTER:
            return "名册里还没有你的记录，请联系管理员（不是权限问题）"
        if self.reason == BLOCKED:
            return "你的账号已停用"
        return "你还没有这个服务的访问权限"


def active_grants(tickets: Iterable[dict], now: float):
    """**「一条单子算不算当前有效的授权」的唯一判据**，产出
        `(单子, 到期时间戳, union_id, 服务名)`。

    **身份和服务名带多余空白的单子在这里就被当脏行拒掉**，两个消费者因此对它给出
        同一个结论（都说「没有」）。

        为什么是拒、不是 strip 了照用：**union_id 是精确标识，不做归一化** —— 归一化过的
        比对早晚会把两个人认成一个，而这套判定的全部意义就是「按 union_id 严格相等」。
        原先的 bug 是不对称：`allowed()` strip 入参、却拿去和单子里的原始值比，于是台账里
        带空白的那一行 `holdings` 算「他有」、`allowed` 永远算「他没有」。修法是让两边
        对它**同样地说没有**，而不是让两边同样地放宽。
        代价是那张单谁也用不了 —— 台账里本来就不该有这种行，而 fail-closed 是安全那侧。

        放行判定（`allowed`）和「这个人手上有哪些服务」（`holdings`，离职回收和
        面板展示都用它）**必须共用这一段** —— 两处各判各的话，最先背离的就是
        「放行说有、回收说没有」，而那一次没有任何地方会报错。

        到期时间戳 0 = 不设期限。
    """
    for ticket in tickets or ():
        # **一行脏数据不该锁死所有人。** 这里每一步都先确认形状再取字段：
        # 直接 `.get()` 的话，`template` 或 `applicant` 不是 dict（手工改坏、
        # 旧版本写下的单子）就会 AttributeError → 整个判定 500 → 网关 fail-closed
        # → **所有人**都进不去，而罪魁只是台账里的一行
        if not isinstance(ticket, Mapping):
            continue
        if ticket.get("kind") != "service" or ticket.get("status") != "done":
            continue
        if not isinstance(ticket.get("template"), Mapping):
            continue
        if not isinstance(ticket.get("applicant"), Mapping):
            continue
        raw = ticket.get("expires_at_ts")
        expires = _ts(raw)
        # **坏数据当作没有授权，不当作「不限期」。** 0 在这里的语义是「永久有效」，
        # 所以把解析不出来的值兜底成 0 等于给一条脏记录发永久通行证
        # （`expires_at_ts: "2026-10-01"` 这种写法就会踩上）。撤销那边同一个兜底
        # 的方向是「宁可多拒」，这边也必须是 —— 同一个字段两种方向，迟早有人统一错
        if raw not in (None, "", 0) and not expires:
            continue
        if expires and expires <= now:
            continue
        uid = str((ticket.get("applicant") or {}).get("union_id") or "")
        service = str((ticket.get("template") or {}).get("service") or "")
        if not uid or not service:
            continue
        # 带多余空白 = 脏行，两边一致地拒（理由见 docstring）。**不要改成 strip 了照用**
        if uid != uid.strip() or service != service.strip():
            continue
        yield ticket, expires, uid, service


def holdings(tickets: Iterable[dict], now: float) -> dict:
    """`{union_id: [(服务名, 单号, 到期时间戳)]}` —— 每个人手上现在有哪些服务访问。

    离职回收和面板的「这个人有什么」共用它。判据和放行侧是同一段
    （`active_grants`），所以不会出现「页面上写着有、网关说没有」。
    """
    out: dict = {}
    for ticket, expires, uid, service in active_grants(tickets, now):
        out.setdefault(uid, []).append((service, str(ticket.get("id") or ""), expires))
    return out


def allowed(
    *,
    union_id: str,
    service: str,
    tickets: Iterable[dict],
    now: float,
    in_roster: bool = True,
    offboarded: bool = False,
) -> Decision:
    """判定。调用方负责把这几样准备好，这里只算。

    `tickets` 给全量即可（百量级），本函数自己筛。不在这里读文件是为了让它可测、
    也让调用方能用自己的缓存 —— 网关那边是**每个请求**都会问一次的。
    """
    union_id = str(union_id or "").strip()
    service = str(service or "").strip()
    if not union_id or not service:
        # 拿不到身份或服务名就拒。**不是「默认放行」** —— 见模块说明
        return Decision(False, NO_GRANT)
    if offboarded:
        return Decision(False, BLOCKED)
    if not in_roster:
        return Decision(False, NOT_IN_ROSTER)

    best: Optional[dict] = None
    for ticket, expires, uid, svc in active_grants(tickets, now):
        # **按 union_id 严格相等**（两边都已归一化）。邮箱会变、姓名会重，union_id 不会
        if svc != service or uid != union_id:
            continue
        # 有多张有效的就挑到期最晚的那张：续期时新旧会并存一段。
        # **「不限期最优」这一条靠下面那个 break 兜住**：去掉 break 的话，
        # 一张不限期的单之后再来一张有期限的，`_ts(best)=0 < expires` 成立、
        # 会把不限期那张顶掉，回一个偏早的 expires_at
        if best is None or _ts(best.get("expires_at_ts")) < expires or not expires:
            best = ticket
            if not expires:  # 不设期限，不会有比它更好的
                break
    if best is None:
        return Decision(False, NO_GRANT)
    return Decision(
        True,
        ticket_id=str(best.get("id") or ""),
        expires_at=_ts(best.get("expires_at_ts")),
    )


def _ts(value: object) -> float:
    """到期时间戳。解析不出来返回 0，调用方把 0 之外的"解析失败"当脏行拒掉。

    **`nan` / `inf` 也算解析不出来**：`float("nan")` 是合法的，而 `nan <= now`
    恒为假 —— 那张单就成了永不过期的通行证，且台账上看着是个正常数字。
    负数同理（1970 年之前的到期时间只可能是写坏的）。
    """
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")) or out < 0:
        return 0.0
    return out
