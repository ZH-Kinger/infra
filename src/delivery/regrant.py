"""凭证发出去之后还能改什么，以及改成什么样。**纯逻辑，一个云调用都没有。**

为什么要有这个模块
──────────────────
面板发出去的凭证，发完就改不了了。2026-09-23 同事的 lakeFS 连不上杭州那个桶，
根因是发放当天代码里的动作表还缺 `oss:GetBucketLocation`（S3 兼容客户端建连先探地域）。
代码第二天就补上了，**但已经发出去那把凭证的策略还是旧的** —— 唯一的修法是有人
写个脚本 ssh 上服务器去改云上的策略。面板自己发的东西，面板却够不着。

改的是**策略，不是凭证**
────────────────────────
权限和有效期全写在那条自定义策略里（`grants.build_policy`：caps 决定出现哪几条
statement，时间窗叠在每一条的 Condition 上）。所以改策略 = 改权限 + 改有效期，
而 **AK 一个字都不用动** —— 对方的服务配置不用改，不用停服。这是整条路的前提。

三种改法，同一条管道：

    repair  窗不变、caps 不变，只按**当前代码**重算一遍策略文档
    expire  只改到期时间
    caps    只改能力集（且只能在模板批准的范围内）

`repair` 零参数、零扩权面 —— 它不扩不缩，只补齐代码里后来修好的动作表。今天那个
真实故障正好只需要它。

这里只负责算出「新文档长什么样、会变哪些字段、为什么不能改」。真正写云和回写单子
在 `flows.regrant_credential`，那边才有 I/O。分开是为了让所有拒绝理由都能被单测钉住，
而不用起一个假云。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import catalog as catalog_mod
from . import grants as grants_mod

#: 三种改法。字面量同时是接口上的 `mode` 取值
MODE_REPAIR = "repair"
MODE_EXPIRE = "expire"
MODE_CAPS = "caps"
MODES = (MODE_REPAIR, MODE_EXPIRE, MODE_CAPS)


@dataclass(frozen=True)
class Plan:
    """算出来的结果。`ok=False` 时只有 `why` 有意义。

    **拒绝要给出人话理由**：这个接口的使用者是管理员，他看到「不能改」之后会做的
    下一件事取决于原因 —— 是该让人重新申请，还是该去查别的地方。只回一个 False
    等于把他推回到猜。
    """

    ok: bool
    why: str = ""
    #: 新的策略文档。`ok=True` 时一定有
    policy: dict = field(default_factory=dict)
    #: 现在云上那份（调用方读回来给我们的），用来算 diff；读不到就是空
    before: dict = field(default_factory=dict)
    #: 要回写进申请单的字段。**只放真的变了的** —— 见 `changed`
    fields: dict = field(default_factory=dict)
    #: 生效 / 到期（epoch 秒）。`not_before` 恒等于原值，见模块说明
    not_before: float = 0.0
    expire: float = 0.0
    caps: tuple = ()
    #: 文档真的变了吗。repair 在「代码没变过」时会算出和云上一模一样的文档，
    #: 那时候不该去云上写一个新版本 —— 白白吃掉一格版本配额（阿里上限 5 个）
    changed: bool = True

    @property
    def summary(self) -> str:
        """一句话说清这次会发生什么。给预演页和审计事件共用。"""
        if not self.ok:
            return self.why
        if not self.changed:
            return "算出来的策略和云上现在那份一模一样，不用改"
        return "；".join(self.what_changes()) or "策略文档有变化"

    def what_changes(self) -> list:
        """逐条说会变什么。**空列表不等于没变** —— 文档内容变了但窗和 caps 都没动
        （正是 repair 的典型情况）时，这里是空的而 `changed` 是 True。"""
        out = []
        if "expires_at_ts" in self.fields:
            out.append("到期时间")
        if "cred_caps" in self.fields:
            out.append("权限")
        return out


def effective_caps(ticket: dict, tpl) -> tuple:
    """这把凭证**现在**的能力集。

    **单子上的 `cred_caps` 优先于模板。** 顺序反过来的话：管理员用 A3 把 caps 改窄，
    模板没变，下一次 repair 又读模板 —— 一次「无害的重算」把刚收窄的权限悄悄放回去了。
    这条要有单测钉死（「改完权限再重算，不回弹」）。

    缺 `cred_caps` 才回落模板：历史单子都没有这个字段，那时候它确实等于模板 caps。
    """
    got = ticket.get("cred_caps")
    if isinstance(got, (list, tuple)) and got:
        return grants_mod.check_caps(got)
    return grants_mod.check_caps(tpl.caps)


def _reject(why: str) -> Plan:
    return Plan(ok=False, why=why)


def plan(
    ticket: dict,
    tpl,
    *,
    mode: str,
    now: float,
    not_before: Optional[float] = None,
    expire: Optional[float] = None,
    caps: Optional[Iterable[str]] = None,
    before: Optional[dict] = None,
) -> Plan:
    """算一次改动。不碰网络。

    `not_before` 由调用方从**云上那份策略**读回来传进来（`GetPolicy` 的
    `DateGreaterThan`），或者从单子的 `cred_not_before` 取。这里只负责在它缺席时拒绝。
    `before` 是云上现在那份文档，只用来判「算出来的和现在的一不一样」，可以不给。
    """
    if mode not in MODES:
        return _reject(f"不认识的改法 {mode!r}，只有 {' / '.join(MODES)}")

    # ── 只有长期凭证能改 ─────────────────────────────────────────────
    # ≤12 小时且模板配了角色的走 STS：那是一把会话凭证，云上**没有策略对象、也没有
    # 子账号**，没有任何东西可以改。判据用 `cred_user` 而不是 hours —— hours 是申请
    # 当时的，而这把凭证实际走了哪条路只有 `cred_user` 说了算
    user = str(ticket.get("cred_user") or "")
    if not user:
        return _reject(
            "这是 12 小时内的临时凭证，云上没有可改的策略（它到点自己失效）。要变更只能重新申请"
        )
    if ticket.get("kind") != catalog_mod.KIND_CREDENTIAL:
        return _reject("只有凭证单能改策略")

    payload = ticket.get("payload") or {}
    bucket = str(payload.get("bucket") or "")
    prefix = str(payload.get("prefix") or "")
    if not bucket:
        return _reject("单子上没有桶名，改不了")
    # 模板里桶被删掉之后就不该再照着它发权限了 —— 发放那条路也是这么判的
    if bucket not in dict(tpl.buckets):
        return _reject(f"模板里已经没有 {bucket} 这个桶了，要变更请重新申请")

    # ── 生效时间：只读不写，取不到就整个拒掉 ─────────────────────────
    # **不许用 now 兜底。** 一张「还没到生效时间」的凭证，拿 now 当 not_before 重算
    # 就是让它提前生效 —— 那是扩权，而且是静默的
    nb = not_before if not_before is not None else ticket.get("cred_not_before")
    try:
        nb = float(nb)
    except (TypeError, ValueError):
        nb = 0.0
    if nb <= 0:
        return _reject(
            "读不回这把凭证的生效时间（云上策略里的 DateGreaterThan），不敢改。"
            "用当前时间兜底会让一张尚未生效的凭证提前生效"
        )

    # ── 到期时间 ────────────────────────────────────────────────────
    old_expire = ticket.get("expires_at_ts")
    try:
        old_expire = float(old_expire or 0)
    except (TypeError, ValueError):
        old_expire = 0.0
    new_expire = old_expire
    if mode == MODE_EXPIRE:
        if expire is None:
            return _reject("改有效期要给新的到期时间")
        try:
            new_expire = float(expire)
        except (TypeError, ValueError):
            return _reject("新的到期时间不是个时间戳")
        if new_expire <= now:
            return _reject("新的到期时间已经过去了")
        # 总时长受发放侧同一个上限约束。不卡的话「先发 30 天再延到一年」就能绕过
        # 模板上那道闸 —— 而那道闸正是审批时批的东西
        cap_hours = int(getattr(tpl, "max_hours", 0) or 0)
        if cap_hours and (new_expire - nb) > cap_hours * 3600 + 1:
            return _reject(
                f"从生效到新到期超过了模板允许的 {cap_hours} 小时。要更长的有效期请重新申请"
            )
    if new_expire <= nb:
        return _reject("到期时间不晚于生效时间，这样的凭证一秒都用不了")

    # ── 能力集 ──────────────────────────────────────────────────────
    try:
        old_caps = effective_caps(ticket, tpl)
    except grants_mod.GrantError as exc:
        # 单子上的 `cred_caps` 是脏的。今天写它的只有本模块自己所以不可达，但哪天
        # 页面或 flows 允许人工回填，这里就会把异常直接抛给调用方 —— 而本模块的契约是
        # 「所有拒绝都给一句人话」，抛异常等于把管理员推回到看 traceback
        return _reject(f"单子上记的权限读不出来（{exc}），请人工核对后重新申请")
    new_caps = old_caps
    if mode == MODE_CAPS:
        if caps is None:
            return _reject("改权限要给新的能力集")
        try:
            # 空集合由 `check_caps` 自己拒（「至少要选一项权限」）。这里不再多写一句
            # 「要停掉请用撤销」—— 那句永远到不了，`check_caps` 先抛
            new_caps = grants_mod.check_caps(caps)
        except grants_mod.GrantError as exc:
            return _reject(str(exc))
        allowed = set(grants_mod.check_caps(tpl.caps))
        extra = [c for c in new_caps if c not in allowed]
        if extra:
            names = "、".join(catalog_mod.CAP_LABELS.get(c, c) for c in extra)
            # 模板 caps 是那张飞书批条批准的范围。面板单方面扩大 = 绕过审批
            return _reject(f"{names} 超出了这张单子批准的范围，要更大权限请重新申请")

    # ── 算新文档 ────────────────────────────────────────────────────
    try:
        doc = grants_mod.build_policy(
            tpl.platform,
            bucket,
            prefix=prefix,
            caps=new_caps,
            not_before=nb,
            expire=new_expire,
        )
    except grants_mod.GrantError as exc:
        return _reject(str(exc))

    fields = {}
    if new_expire != old_expire:
        fields["expires_at_ts"] = new_expire
    if tuple(new_caps) != tuple(old_caps):
        fields["cred_caps"] = list(new_caps)
    # 生效时间第一次被读回来时顺手记进单子：下次就不用再问云了，
    # 而「读不回来就拒绝」那条规矩也就只在第一次生效
    if not ticket.get("cred_not_before"):
        fields["cred_not_before"] = nb

    return Plan(
        ok=True,
        policy=doc,
        before=dict(before or {}),
        fields=fields,
        not_before=nb,
        expire=new_expire,
        caps=tuple(new_caps),
        changed=bool(before is None or doc != before),
    )


def statement_diff(before: dict, after: dict) -> list:
    """逐条对比两份策略文档，给页面渲染用。

    返回 `[{"how": 加/删/改, "action": [...], "resource": [...], ...}]`。

    **按 (Effect, Resource, 有没有 Prefix 条件) 分组，不按下标** —— statement 的顺序由
    caps 决定，改 caps 时中间会少一条，按下标对的话后面全部错位、页面上看起来像是每一条
    都变了。

    **同一组里可能不止一条**：「下载」和「上传」在 `build_policy` 里都是对象级 ARN、
    都不叠 `oss:Prefix`，键完全相同。早先这里用 `{key: st}` 建字典，于是上传那条把下载
    那条覆盖掉 —— 管理员收掉「下载」权限时，预演页显示**「没有变化」**，而他提交的改动
    会真的收掉使用方的下载权（反方向更糟：显示成一条「改」，真相是删一条加一条）。
    所以组内再**按动作集的重合度配对**：`{GetObject}` → `{GetObject, GetObjectVersion}`
    重合度高，配成一条「改」；`{GetObject…}` 和 `{PutObject…}` 零重合，各自算删和加。
    """

    def key(st: dict) -> tuple:
        cond = st.get("Condition") or {}
        return (
            str(st.get("Effect") or ""),
            tuple(st.get("Resource") or []),
            bool(cond.get("StringLike")),
        )

    def acts(st) -> frozenset:
        return frozenset((st or {}).get("Action") or [])

    def group(doc) -> dict:
        out: dict = {}
        for st in (doc or {}).get("Statement") or []:
            out.setdefault(key(st), []).append(st)
        return out

    old_g, new_g = group(before), group(after)
    out = []
    for k in list(old_g) + [k for k in new_g if k not in old_g]:
        olds, news = list(old_g.get(k, [])), list(new_g.get(k, []))
        # 组内贪心配对：每次挑重合度最高的一对。重合为 0 的不配对 —— 那是两件不同的事，
        # 硬配成「改」会把「删掉下载 + 加上上传」说成「动作变了」，掩盖掉其中一半
        while olds and news:
            best = max(
                ((i, j) for i in range(len(olds)) for j in range(len(news))),
                key=lambda ij: len(acts(olds[ij[0]]) & acts(news[ij[1]])),
            )
            if not (acts(olds[best[0]]) & acts(news[best[1]])):
                break
            a, b = olds.pop(best[0]), news.pop(best[1])
            if a != b:
                out.append(_row(k, a, b))
        out.extend(_row(k, a, None) for a in olds)
        out.extend(_row(k, None, b) for b in news)
    return out


def _row(k: tuple, a, b) -> dict:
    return {
        "how": "删" if b is None else ("加" if a is None else "改"),
        "effect": k[0],
        "resource": list(k[1]),
        "before": sorted((a or {}).get("Action") or []),
        "after": sorted((b or {}).get("Action") or []),
    }
