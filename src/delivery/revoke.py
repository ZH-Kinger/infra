"""管理员收权：撤掉子账号身上**不是面板发的**那些策略和用户组。

面板原本只回收自己发出去的（`flows._revoke_grant`，到期驱动，且只按工单模板算）。
存量权限——建号之前就堆在人身上的那些——面板一条都不碰。而真正扎眼的恰恰是存量：
一个人身上 22 条策略里有 `AdministratorAccess`，另外 21 条被它完全覆盖、一条都不起作用，
没人能一眼看出这个号到底有什么。

**这一层只调 detach / remove，没有任何授予路径**（授予必须走审批 `flows.submit`）。
但**「只减不增就没有提权风险」这个说法是错的**，别照着它放心：

RAM 里 Deny 优先且跨策略生效，所以**撤掉一条含 Deny 的策略 = 提权**。本仓库自己
就有现成的反例：`identity/deny-pai-delete.example.json` 是一条专门用来禁删数据集的
Deny 策略，一旦挂上，从这里把它勾掉，那个人的删除能力就恢复了 —— 而界面显示的是
「会撤掉 1 条」，看起来在收紧。

挡它的是 `PROTECTED` 名单（见下）。**彻底的做法是撤之前读一次策略正文、见到
`"Effect": "Deny"` 就拒**，那需要给执行身份加 `ram:GetPolicy` / `ram:GetPolicyVersion`
两个只读动作 —— 还没加，所以现在靠名单兜底。名单是人维护的，会漏。

## 四条拒绝，每条都有它防的具体事故

1. **面板自己的身份**（执行/发放/采集）：撤了它们，面板当场瘫，而且**连带失去把它加回来的能力**
   （加回来要 `ram:AttachPolicyToUser`，那正是执行身份的策略给的）。这是唯一一条
   「撤了就自己救不回来」的，所以写死在代码里，不做成配置。
2. **面板通过工单发出去的**：那些有到期时间、有台账。从这里撤掉的话，工单还显示「已开通」，
   而云上已经没了 —— 台账开始说谎，比权限多给更难查。走工单回收。
3. **最后一个管理员**：不硬拦，但要**显式确认**（`confirm_last_admin`）。
   一度写成硬拒，理由是「面板从此改不动任何人的权限」—— 那是错的：执行身份挂的是它自己的
   `wuji-panel-executor` 策略，和有没有人类管理员无关；主账号也照常能进控制台。
   真正的代价只是「控制台里没有子账号能做管理员操作了」，恢复得用主账号。
   所以这是「别手滑」，不是「不许做」—— 硬拦一个合法且可恢复的操作，
   只会逼人绕开面板去控制台点，那才是真的失控。
4. **本来就没挂**：不报错，但要说出来 —— 静默成功会让人以为撤掉了。

## 撤不了的那一类：资源组级授权

`ram:DetachPolicyFromUser` 只管账号级。`AdministratorAccess @资源组:rg-xxx` 那种走的是
ResourceManager 的接口，执行身份没有那个权限。**必须显式报出来**，不能当成「撤完了」——
少撤一条资源组级的 `AdministratorAccess`，和没撤是一样的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

#: 面板自己的身份。**前缀匹配，写死不做配置** —— 见模块开头第 1 条
PANEL_PREFIX = "panel-"
#: 等同管理员的系统策略
ADMIN_POLICIES = ("AdministratorAccess",)

#: 不许从面板撤的策略名。**前缀匹配，不要在这里写 `*`** —— 用的是 `startswith`，
#: 写了星号会去匹配字面量星号，于是永远不命中，而且一声不响。
#: 这些策略含 Deny，撤掉等于放权。
#: 名单是人维护的、会漏 —— 真正的修法是读策略正文判 Effect，见模块开头
PROTECTED = ("wuji-deny-", "deny-")

REFUSE_PROTECTED = "protected"
REFUSE_DENY = "deny"
REFUSE_PANEL = "panel"
REFUSE_TICKET = "ticket"
REFUSE_LAST_ADMIN = "last_admin"
REFUSE_ABSENT = "absent"
REFUSE_SCOPED = "scoped"

_WHY = {
    REFUSE_DENY: "这条策略里有 Deny，撤掉等于放权，不从这里撤。"
    "（读不出正文的也按这条拒 —— 读不出来不能当成没有 Deny）",
    REFUSE_PROTECTED: "这条是护栏策略（含 Deny），撤掉等于放权 —— 不从这里撤，"
    "要改去控制台，并记录为什么",
    REFUSE_PANEL: "这是面板自己的身份，撤了面板当场瘫，而且没法自己加回来",
    REFUSE_TICKET: "这条是面板通过申请单发的，从这里撤会让台账说谎。去那张单子上回收",
    REFUSE_LAST_ADMIN: "这是这个云账号最后一个管理员。要撤请显式确认——"
    "撤完只剩主账号能做控制台管理操作（面板不受影响，它用的是自己的策略）",
    REFUSE_ABSENT: "这个号上本来就没有它",
    REFUSE_SCOPED: "资源组级授权，`ram:DetachPolicyFromUser` 管不了（要 ResourceManager 接口）",
}


@dataclass(frozen=True)
class Item:
    """一条要撤的东西。`kind` 是 policy / group。"""

    kind: str
    name: str
    policy_type: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}（{self.policy_type}）" if self.policy_type else self.name


@dataclass
class Plan:
    user: str
    remove: list = field(default_factory=list)
    #: [(Item, 原因码), …]
    refused: list = field(default_factory=list)
    #: 撤完之后这个人还剩什么 —— **一定要给**：收权最常见的事故不是撤错，
    #: 是撤完才发现他连活都干不了了
    remaining: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.remove)

    def why(self, code: str) -> str:
        return _WHY.get(code, code)


def is_panel_identity(login: str) -> bool:
    """是不是面板自己的身份。

    **注意这行 `startswith` 在火山侧是唯一的一道门**：阿里那边另有云端兜底
    （`identity/executor-policy.aliyun.json` 对 `user/panel-*` 三个身份的 Deny），
    火山的执行身份策略是 `Resource: ["*"]`，一条 self-deny 都没有。
    """
    return str(login or "").strip().lower().startswith(PANEL_PREFIX)


def granted_by_panel(
    tickets: Optional[Iterable],
    platform: str,
    account: str,
    user: str,
    *,
    now: Optional[float] = None,
) -> set:
    """面板通过**还在生效的**申请单发给这个人的 groups/policies。

    只看还在生效的：已经到期或已回收的那些，云上本来就该没有了；要是还在，
    那是回收失败留下的残留，**正是应该从这里撤掉的**。

    曾经把到期判成 `expires <= 0` —— 那是「有没有填到期时间」，不是「过没过期」，
    真实数据里恒假。后果：单子还挂着 `done`、早就过期、每轮回收都失败
    （`flows._revoke_failed` 会把状态留在 `done`）的那种残留，被判成「面板发的」
    而永远撤不掉 —— 和这个函数自己的说明正好相反。
    """
    import time

    now = now if now is not None else time.time()
    out: set = set()
    for t in tickets or ():
        if not isinstance(t, Mapping) or t.get("status") != "done":
            continue
        tpl = t.get("template") or {}
        if (tpl.get("platform"), tpl.get("account")) != (platform, account):
            continue
        # **开账号单用的是 `username`，不是 `cloud_user`**（见 flows.py:1770）。
        # 只看 cloud_user 的话，开账号单加的组从这里撤不被拒 —— 而那张单子
        # 仍写着「已加入 wuji_Algorithm」，台账开始说谎
        payload = t.get("payload") or {}
        if (payload.get("cloud_user") or payload.get("username")) != user:
            continue
        try:
            expires = float(t.get("expires_at_ts") or 0)
        except (TypeError, ValueError):
            expires = 0.0  # 坏数据当「没到期」：宁可多拒，也别把还在生效的权限撤掉
        if expires and expires <= now:
            continue
        for g in tpl.get("groups") or ():
            out.add(("group", str(g)))
        for p in tpl.get("policies") or ():
            name = p.get("name") if isinstance(p, Mapping) else str(p)
            if name:
                out.add(("policy", str(name)))
    return out


def plan(
    *,
    user: str,
    attached: Iterable,
    groups: Iterable,
    wanted: Iterable,
    admin_holders: Iterable = (),
    panel_granted: Iterable = (),
    admin_groups: Iterable = (),
    #: 挂着护栏策略（含 Deny）的用户组。移出这种组同样等于放权
    protected_groups: Iterable = (),
    #: `{策略名: 有没有 Deny}`。**None 表示读不出来，一样要拒**
    deny_of: Optional[Mapping] = None,
    confirm_last_admin: bool = False,
) -> Plan:
    """算出这次会撤掉什么、拒掉什么、还剩什么。**纯函数，不碰云。**

    `attached` 是**实时从云上读的**账号级策略 `[{"PolicyName","PolicyType"}, …]`，
    不是快照 —— 拿快照算，可能会去撤一条五分钟前刚发的策略。
    `admin_holders` 是这个云账号里还持有管理员策略的登录名集合。
    """
    out = Plan(user=str(user))
    have_pol = {str(p.get("PolicyName") or ""): str(p.get("PolicyType") or "") for p in attached}
    have_grp = {str(g) for g in groups}
    panel_set = set(panel_granted)
    holders = {str(h) for h in admin_holders}
    #: 本身就发管理员权限的用户组。移出这种组同样等于撤管理员
    admin_groups_set = {str(g) for g in admin_groups}
    protected_groups_set = {str(g) for g in protected_groups}

    for item in wanted:
        if is_panel_identity(user):
            out.refused.append((item, REFUSE_PANEL))
            continue
        # 组也要判：护栏策略一旦挂到组上，「把人移出这个组」就绕过了按策略名的判断 ——
        # 和「移出发超管的组不弹 last_admin」是同一种不对称
        if item.kind == "group" and item.name in protected_groups_set:
            out.refused.append((item, REFUSE_PROTECTED))
            continue
        if item.kind == "policy" and item.name.lower().startswith(PROTECTED):
            out.refused.append((item, REFUSE_PROTECTED))
            continue
        # 读出来有 Deny、或者**问了却读不出来**（值是 None），都拒。
        # 「读不出来」当成「没有 Deny」是这里最危险的默认值。
        # **只对问过的那些生效**（`in deny_of`）：调用方只问自定义策略，
        # 系统策略压根不在表里，用 `.get()` 会把它们全判成「读不出来」而误伤
        if (
            item.kind == "policy"
            and item.name in (deny_of or {})
            and deny_of[item.name] is not False
        ):
            out.refused.append((item, REFUSE_DENY))
            continue
        if "@" in item.name or "资源组" in item.name:
            out.refused.append((item, REFUSE_SCOPED))
            continue
        if (item.kind, item.name) in panel_set:
            out.refused.append((item, REFUSE_TICKET))
            continue
        if item.kind == "group":
            if item.name not in have_grp:
                out.refused.append((item, REFUSE_ABSENT))
                continue
        else:
            if item.name not in have_pol:
                out.refused.append((item, REFUSE_ABSENT))
                continue
        out.remove.append(
            Item(item.kind, item.name, have_pol.get(item.name, "") if item.kind == "policy" else "")
        )

    # **last-admin 要在算完之后整体判一次，不能塞在 policy 那条分支里。**
    # 塞在那里的那一版，对「把人移出发超管的用户组」完全不触发 —— 而火山
    # `wuji-opration` 组挂的正是 AdministratorAccess：把它的两个成员移出去，
    # 全账号再没有子账号管理员，全程零确认。护栏在真实拓扑下 100% 不生效，
    # 而代码注释还声称它生效，这比没有护栏更糟
    if not confirm_last_admin and holders <= {str(user)}:
        loses_admin = any(
            (i.kind == "policy" and i.name in ADMIN_POLICIES)
            or (i.kind == "group" and i.name in admin_groups_set)
            for i in out.remove
        )
        if loses_admin:
            kept, dropped = [], []
            for i in out.remove:
                drop = (i.kind == "policy" and i.name in ADMIN_POLICIES) or (
                    i.kind == "group" and i.name in admin_groups_set
                )
                (dropped if drop else kept).append(i)
            out.remove = kept
            for i in dropped:
                out.refused.append((i, REFUSE_LAST_ADMIN))

    gone = {(i.kind, i.name) for i in out.remove}
    out.remaining = sorted(
        [f"用户组 {g}" for g in have_grp if ("group", g) not in gone]
        + [f"{n}（{t}）" for n, t in have_pol.items() if ("policy", n) not in gone]
    )
    return out
