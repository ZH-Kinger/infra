"""登录用的映射提案：飞书企业邮箱 → 各云账号用户名。

这张表最终要给身份桥在**登录时**使用：查到谁，就以谁的身份进云控制台。
所以它和看板用的 `mapping.py` 要求不同——看板映射错了是显示错，
登录映射错了是登进别人的账号。

两条原则
────────
1. **这里只产出提案，不产出最终表。** 推断结果分成「已确认 / 需确认 / 拦截」，
   经人工确认后才进冻结映射表，登录只读冻结表。
2. **确认需要两个独立证据同时成立。**
     证据一：用户名能由企业邮箱推出（邮箱候选集规则）
     证据二：邮箱来源可信，或显示名与此人另一个已确认账号一致
   只有一个证据的一律「需确认」，交给人判断。

为什么显示名单独不够：云上显示名任何有 RAM/IAM 写权限的人都能改，而且会重名。
为什么未验证邮箱单独不够：安全邮箱 pending 表示本人从未点过验证链接，
可能是别人替他填的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .audit import SERVICE_NAMES, SERVICE_PREFIXES
from .mapping import _name_key, candidates

STATUS_CONFIRMED = "confirmed"
STATUS_REVIEW = "review"
STATUS_BLOCKED = "blocked"

SOURCE_RAM_EMAIL = "ram_email"  # RAM 用户基本信息里的邮箱，由管理员或审批建号流程写入
SOURCE_SECURITY_EMAIL = "security_email"  # 阿里云安全邮箱（IMS），有已验证/未验证之分
SOURCE_IAM_EMAIL = "iam_email"  # 火山 IAM 用户邮箱，有 EmailIsVerify
#: 没有采集接口的平台（九章），邮箱来自控制台导出、由管理员录入 —— 见 offline_accounts
SOURCE_ADMIN_EXPORT = "admin_export"

_SOURCE_LABEL = {
    SOURCE_RAM_EMAIL: "RAM 用户邮箱字段",
    SOURCE_SECURITY_EMAIL: "安全邮箱",
    SOURCE_IAM_EMAIL: "IAM 用户邮箱",
    SOURCE_ADMIN_EXPORT: "控制台导出（管理员录入）",
}


@dataclass(frozen=True)
class EmailClaim:
    """云账号上登记的一个邮箱，以及它可不可信。"""

    address: str
    source: str
    verified: bool

    @property
    def trusted(self) -> bool:
        # RAM 用户邮箱字段没有验证状态，但它由管理员或审批建号流程写入，
        # 视为可信；安全邮箱、火山邮箱以本人是否验证为准。
        if self.source == SOURCE_RAM_EMAIL:
            return True
        return self.verified

    def describe(self) -> str:
        label = _SOURCE_LABEL.get(self.source, self.source)
        # 这两类没有「本人验证」这回事，由管理员写入 —— 再挂一个「（已验证）」是在编造状态
        if self.source in (SOURCE_RAM_EMAIL, SOURCE_ADMIN_EXPORT):
            return label
        return f"{label}（{'已验证' if self.verified else '未验证'}）"


@dataclass(frozen=True)
class CloudAccount:
    scope: str  # 形如 aliyun/<主账号 UID>
    name: str
    display_name: str = ""
    emails: tuple = ()


@dataclass(frozen=True)
class Link:
    email: str
    scope: str
    name: str
    display_name: str
    status: str
    evidence: tuple = ()

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "name": self.name,
            "display_name": self.display_name,
            "status": self.status,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Unlinked:
    scope: str
    name: str
    display_name: str
    reason: str


@dataclass(frozen=True)
class Proposal:
    domain: str
    links: tuple = ()
    unlinked: tuple = ()
    services: tuple = field(default=())

    def by_status(self, status: str) -> list:
        return [x for x in self.links if x.status == status]

    def people(self) -> dict:
        out: dict = {}
        for link in self.links:
            out.setdefault(link.email, []).append(link)
        return out

    def to_dict(self) -> dict:
        return {
            "schema": "wuji-sso-map/proposal@1",
            "domain": self.domain,
            "counts": {
                "confirmed": len(self.by_status(STATUS_CONFIRMED)),
                "review": len(self.by_status(STATUS_REVIEW)),
                "blocked": len(self.by_status(STATUS_BLOCKED)),
                "unlinked": len(self.unlinked),
                "services": len(self.services),
            },
            "people": [
                {"email": email, "links": [x.to_dict() for x in sorted(ls, key=lambda k: k.scope)]}
                for email, ls in sorted(self.people().items())
            ],
            "unlinked": [
                {
                    "scope": u.scope,
                    "name": u.name,
                    "display_name": u.display_name,
                    "reason": u.reason,
                }
                for u in self.unlinked
            ],
            "services": [{"scope": s.scope, "name": s.name} for s in self.services],
        }


def is_service(
    name: str, *, extra_prefixes: Sequence[str] = (), extra_names: Sequence[str] = ()
) -> bool:
    lowered = name.lower()
    prefixes = tuple(SERVICE_PREFIXES) + tuple(p.lower() for p in extra_prefixes)
    names = set(SERVICE_NAMES) | {n.lower() for n in extra_names}
    return lowered.startswith(prefixes) or lowered in names


def propose(
    accounts: Iterable[CloudAccount],
    *,
    domain: str,
    service_prefixes: Sequence[str] = (),
    service_names: Sequence[str] = (),
    trust_unverified_when_derivable: bool = False,
) -> Proposal:
    suffix = "@" + domain.lstrip("@").lower()
    people_accounts: list = []
    services: list = []
    for acc in accounts:
        if is_service(acc.name, extra_prefixes=service_prefixes, extra_names=service_names):
            services.append(acc)
        else:
            people_accounts.append(acc)

    links: list = []
    unlinked: list = []
    no_email: list = []

    # ── 第一轮：账号自己登记了企业邮箱 ─────────────────────────────────
    for acc in people_accounts:
        corp: dict = {}
        for claim in acc.emails:
            addr = (claim.address or "").strip().lower()
            if not addr.endswith(suffix):
                continue
            # 同一地址出现在多个字段时，取可信的那条作为依据
            if addr not in corp or (claim.trusted and not corp[addr].trusted):
                corp[addr] = claim
        if not corp:
            no_email.append(acc)
            continue
        if len(corp) > 1:
            unlinked.append(
                Unlinked(
                    acc.scope,
                    acc.name,
                    acc.display_name,
                    f"账号上登记了多个不同的企业邮箱：{', '.join(sorted(corp))}，无法判断属于谁",
                )
            )
            continue
        addr, claim = next(iter(corp.items()))
        cands = candidates(addr.split("@", 1)[0])
        rule = acc.name.lower() in cands
        evidence = [f"邮箱 {addr} 来自{claim.describe()}"]
        if rule:
            evidence.append("用户名可由邮箱推出")
            if claim.trusted:
                status = STATUS_CONFIRMED
            elif trust_unverified_when_derivable:
                # 放宽策略：未验证的企业邮箱 + 用户名可由它推出，视为两个证据成立。
                # 冒用者要在同一朵云拿到和目标同名的账号，而该用户名已被本人占用；
                # 即便造出第二个能对上的账号，也会被下面的重名拦截挡住。
                status = STATUS_CONFIRMED
                evidence.append("邮箱未验证，按「企业邮箱 + 用户名可推出」策略确认")
            else:
                status = STATUS_REVIEW
                evidence.append("需确认：邮箱未经本人验证")
        else:
            status = STATUS_REVIEW
            evidence.append(
                f"需确认：用户名 {acc.name} 无法由邮箱推出（规则给出 {' / '.join(sorted(cands))}）"
            )
        links.append(Link(addr, acc.scope, acc.name, acc.display_name, status, tuple(evidence)))

    # 显示名索引：只取第一轮对上的账号（有企业邮箱作锚点的）
    display_to_people: dict = {}
    confirmed_names: dict = {}
    for link in links:
        key = _name_key(link.display_name)
        if not key:
            continue
        display_to_people.setdefault(key, set()).add(link.email)
        if link.status == STATUS_CONFIRMED:
            confirmed_names.setdefault(link.email, set()).add(key)
    known_people = sorted({link.email for link in links})

    # ── 第二轮：账号上没有企业邮箱 ─────────────────────────────────────
    for acc in no_email:
        key = _name_key(acc.display_name)
        owners = display_to_people.get(key, set()) if key else set()
        if len(owners) > 1:
            unlinked.append(
                Unlinked(
                    acc.scope,
                    acc.name,
                    acc.display_name,
                    f"显示名「{acc.display_name}」对应多个人：{', '.join(sorted(owners))}",
                )
            )
            continue
        if len(owners) == 1:
            email = next(iter(owners))
            rule = acc.name.lower() in candidates(email.split("@", 1)[0])
            corroborated = key in confirmed_names.get(email, set())
            evidence = [f"账号上没有企业邮箱，按显示名「{acc.display_name}」对到 {email}"]
            if rule:
                evidence.append("用户名可由邮箱推出")
            if corroborated and rule:
                evidence.append("此人另一个账号已确认，显示名一致")
                status = STATUS_CONFIRMED
            else:
                status = STATUS_REVIEW
                if not rule:
                    evidence.append(f"需确认：用户名 {acc.name} 无法由邮箱推出")
                if not corroborated:
                    evidence.append("需确认：此人没有其他已确认的账号可以佐证")
            links.append(
                Link(email, acc.scope, acc.name, acc.display_name, status, tuple(evidence))
            )
            continue
        # 显示名对不上任何人，最后看用户名能不能唯一对上某个已知的人
        hits = [e for e in known_people if acc.name.lower() in candidates(e.split("@", 1)[0])]
        if len(hits) == 1:
            links.append(
                Link(
                    hits[0],
                    acc.scope,
                    acc.name,
                    acc.display_name,
                    STATUS_REVIEW,
                    (
                        f"账号上没有企业邮箱，显示名也对不上，仅用户名可由 {hits[0]} 推出",
                        "需确认：只有一个证据",
                    ),
                )
            )
            continue
        reason = "账号上没有企业邮箱"
        reason += f"，显示名「{acc.display_name}」找不到对应的人" if key else "，显示名为空"
        unlinked.append(Unlinked(acc.scope, acc.name, acc.display_name, reason))

    # ── 拦截：同一个人在同一朵云上对到多个账号 ─────────────────────────
    per_scope: dict = {}
    for link in links:
        per_scope.setdefault((link.email, link.scope), []).append(link)
    final: list = []
    for link in links:
        group = per_scope[(link.email, link.scope)]
        if len(group) > 1:
            names = ", ".join(sorted(x.name for x in group))
            final.append(
                Link(
                    link.email,
                    link.scope,
                    link.name,
                    link.display_name,
                    STATUS_BLOCKED,
                    link.evidence
                    + (
                        f"拦截：{link.email} 在 {link.scope} 对到多个账号（{names}），"
                        "登录无法确定用哪个",
                    ),
                )
            )
        else:
            final.append(link)

    order = {STATUS_BLOCKED: 0, STATUS_REVIEW: 1, STATUS_CONFIRMED: 2}
    final.sort(key=lambda x: (order[x.status], x.email, x.scope))
    unlinked.sort(key=lambda u: (u.scope, u.name))
    return Proposal(domain.lstrip("@"), tuple(final), tuple(unlinked), tuple(services))


def render(proposal: Proposal, *, show_confirmed: bool = False) -> str:
    """给人审的清单。先列要处理的，已确认的默认只给数字。"""
    blocked = proposal.by_status(STATUS_BLOCKED)
    review = proposal.by_status(STATUS_REVIEW)
    confirmed = proposal.by_status(STATUS_CONFIRMED)
    lines = [
        f"映射提案（{proposal.domain}）",
        f"  已确认 {len(confirmed)}　需确认 {len(review)}　拦截 {len(blocked)}"
        f"　未对上 {len(proposal.unlinked)}　服务号 {len(proposal.services)}",
        "",
    ]

    def block(title: str, items: Sequence) -> None:
        if not items:
            return
        lines.append(title)
        for x in items:
            shown = f"「{x.display_name}」" if x.display_name else ""
            lines.append(f"  {x.scope}  {x.name} {shown} → {x.email}")
            for ev in x.evidence:
                lines.append(f"      · {ev}")
        lines.append("")

    block("拦截（必须先处理）", blocked)
    block("需确认", review)
    if proposal.unlinked:
        lines.append("未对上（这些人暂时无法用 SSO 登录）")
        for u in proposal.unlinked:
            shown = f"「{u.display_name}」" if u.display_name else ""
            lines.append(f"  {u.scope}  {u.name} {shown}　{u.reason}")
        lines.append("")
    if show_confirmed:
        block("已确认", confirmed)
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CloudAccount",
    "EmailClaim",
    "Link",
    "Proposal",
    "Unlinked",
    "SOURCE_IAM_EMAIL",
    "SOURCE_RAM_EMAIL",
    "SOURCE_SECURITY_EMAIL",
    "STATUS_BLOCKED",
    "STATUS_CONFIRMED",
    "STATUS_REVIEW",
    "is_service",
    "propose",
    "render",
]
