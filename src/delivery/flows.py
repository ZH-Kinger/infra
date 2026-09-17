"""申请流程：提交 → 发起飞书审批 → 同步审批状态 → 开通 → 领取。

面板和 CLI 都只调这里，规则只写一份。每个会写云的入口（execute、claim）都先
`approval.verify_approved()`：实时查飞书，状态、审批定义、发起人、申请单号都对才放行。

模板在提交时做快照存进申请单；开通前要求当前模板和快照一致——审批人批的是提交时的内容，
管理员在审批期间改了模板（比如多加一个用户组），这张单子就不能按新内容开通。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import MISSING, asdict, fields
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Optional

from . import catalog as catalog_mod
from . import grants as grants_mod
from . import notify as notify_mod
from . import platforms as platforms_mod
from . import policies as policies_mod
from . import sealed
from . import tickets as t
from .approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    Applicant,
    FeishuApproval,
    SelfApprovalError,
    instance_links,
)
from .errors import DeliveryError
from .provision import ProvisionError, describe_error

_BJ = timezone(timedelta(hours=8))
_REASON_MIN = 5
_REASON_MAX = 500
_SYNC_INTERVAL = 10
#: 授权天数的硬上限：模板 max_days=0（不限）时也不能填出溢出时间戳的天数
_MAX_DAYS = 3650
#: 「开通中 / 提交中」超过这么久没进展就当作中断
_STUCK_AFTER = 30 * 60
#: 云上可能还有东西要收的状态。FAILED / CLOSED 是给「凭证签发出来了却没送达」那种单子留的
_RECLAIMABLE = (t.DONE, t.FAILED, t.CLOSED)
#: 本机地址：调试时 safe_base_url 放行，但凭证的查看地址不能是它
_LOCAL_HOST = re.compile(r"\A(localhost|127\.\d+\.\d+\.\d+|::1|\[::1\])\Z", re.I)
#: 开账号后初始密码的领取期限
_PASSWORD_WINDOW_DAYS = 7
#: 云上用户名的硬字符集：模板的 username_pattern 再宽也不能超出
_SAFE_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
#: 开通前核对：模板里决定「往云上写什么」的字段
_EXEC_FIELDS = (
    "kind",
    "platform",
    "account",
    "groups",
    "role_arn",
    "max_hours",
    "caps",
    "buckets",
    "allow_prefix",
    "max_days",
    "specs",
    "resource_type",
    "region",
    "username_pattern",
    "console_login",
)
#: 使用方名称：进 RAM 登录名和审批评论，不当标识符用，但也不能任意长
_SUBJECT_MAX = 40
#: 成本归属自己填时的长度上限（「其他」的 id 在 catalog.COST_OTHER）
_COST_NAME_MAX = 40
#: 从权限策略目录里挑策略申请时的「模板」id：不在模板目录里，按策略目录和策略规则现场生成
POLICY_TEMPLATE = "policy"
_POLICY_TITLE = "权限策略"
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


class FlowError(t.TicketError):
    """申请流程拒绝操作。"""


def _applicant_dict(a: Applicant, email: str) -> dict:
    return {**asdict(a), "email": email}


def _applicant(ticket: dict) -> Applicant:
    a = ticket["applicant"]
    return Applicant(
        union_id=a["union_id"],
        name=a.get("name", ""),
        open_id=a.get("open_id", ""),
        user_id=a.get("user_id", ""),
    )


def _snapshot(template: catalog_mod.Template) -> dict:
    data = asdict(template)
    data.pop("extra", None)
    # 分类、输入框提示只是申请页的展示，改它们不应让审批中的单子因「模板变了」而不能开通
    data.pop("category", None)
    data.pop("spec_hint", None)
    # asdict 保留元组；快照要和 JSON 里读回来的那份逐字相等，否则每张单子都会被判成「模板被改」
    data["groups"] = list(data["groups"])
    data["caps"] = list(data["caps"])
    data["buckets"] = [{"name": n, "region": r} for n, r in template.buckets]
    data["cost_centers"] = [{"id": i, "label": label} for i, label in template.cost_centers]
    data["params"] = dict(template.params)
    # 选项轴连 params 一起进快照：审批人批的是这份参数，开通前要核对它没被改过。
    # 只存 id 和名字的话，管理员在审批期间把「通用 2核8G」的镜像换掉，这张单子照开
    data["options"] = [
        {
            "id": a.id,
            "label": a.label,
            "choices": [{"id": c, "label": cl, "params": dict(cp)} for c, cl, cp in a.choices],
            "number": None if a.number is None else asdict(a.number),
            "hidden_when": {k: list(v) for k, v in a.hidden_when},
        }
        for a in template.options
    ]
    return data


#: 模板快照里各字段的默认值。**给 _EXEC_FIELDS 加字段时，旧单子的快照里没有这个键**，
#: 直接 .get() 会拿到 None，而新快照给的是 [] / True —— 严格相等永不成立，
#: 结果是部署当天所有在途单子全部报「模板在审批期间被修改」，还把人引去查模板。
#: 按 Template 的字段默认值补齐再比：缺的键说明它当时就是默认值。
_FIELD_DEFAULTS = {
    f.name: (list(f.default) if isinstance(f.default, tuple) else f.default)
    for f in fields(catalog_mod.Template)
    if f.default is not MISSING
}


def _effective(template: dict, names: tuple) -> dict:
    return {k: template.get(k, _FIELD_DEFAULTS.get(k)) for k in names}


def _ts(iso: object) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _policy_key(policy: dict) -> str:
    return f"{policy['type']}:{policy['name']}"


def _policy_keys(ticket: dict) -> list:
    """申请单里要授予的策略（Type:Name）；模板权限单没有这一项。"""
    return [_policy_key(p) for p in (ticket.get("template") or {}).get("policies") or []]


def _same_grantee(other: dict, template: dict, user: str) -> bool:
    """other 是不是给同一个云账号下同一个子账号的权限申请。"""
    tpl = other.get("template") or {}
    return (
        other.get("kind") == catalog_mod.KIND_PERMISSION
        and tpl.get("platform") == template["platform"]
        and tpl.get("account") == template["account"]
        and (other.get("payload") or {}).get("cloud_user") == user
    )


def _duration(hours: int) -> str:
    """8 → 「8 小时」；720 → 「30 天」。整天的时长写成小时，人一眼看不出是多久。"""
    if hours >= 24 and hours % 24 == 0:
        return f"{hours // 24} 天"
    return f"{hours} 小时"


def view_base(env: Optional[Mapping] = None) -> str:
    """查看地址的前缀。**拿不到就直接失败**。

    地址是凭证的唯一出口。没配 `DELIVERY_BASE_URL` 时贴一条相对路径 `/c/...` 出去，
    凭证照发、单子照样 DONE，使用方拿到一个点不开的链接 —— 而密钥只在那条评论里，
    我们自己也解不开密文，只能整单重来。

    **本机地址也不行**，虽然 `safe_base_url` 为了本地调试放行它：漏配时 serve 会回填
    `http://localhost:<端口>`，那就从「一眼看得出坏了的相对路径」变成「一个像模像样、
    但使用方点开是他自己 8765 端口」的绝对地址 —— 比原来更难发现。
    """
    raw = (env if env is not None else os.environ).get(notify_mod.ENV_BASE_URL) or ""
    base = notify_mod.safe_base_url(raw)
    if base and _LOCAL_HOST.match(urllib.parse.urlsplit(base).hostname or ""):
        raise FlowError(
            f"{notify_mod.ENV_BASE_URL} 指向本机（{base}），"
            f"这样发出去的查看地址使用方打不开。请配成面板的对外 https 地址",
            503,
        )
    if not base:
        raise FlowError(
            f"没有配置 {notify_mod.ENV_BASE_URL}（或不是 https 地址），查看凭证的地址拼不出来", 503
        )
    return base


def _view_comment(*, ticket: dict, tpl: catalog_mod.Template, key: str, cred: dict) -> str:
    """审批评论。**这里没有 AK/SK** —— 只有一个带密钥的查看地址。

    地址长这样：`/c/<申请单号>#<密钥>`。密钥放在 `#` 之后是因为浏览器不会把 fragment
    发给服务端 —— 它不进访问日志、不进 Referer，只在使用方自己的浏览器里。
    """
    payload = ticket["payload"]
    link = f"{view_base()}/c/{ticket['id']}#{key}"
    return "\n".join(
        [
            f"申请单    {ticket['id']}",
            f"使用方    {payload.get('subject') or ''}",
            f"权限      {'、'.join(cred.get('caps') or [])}",
            f"范围      {cred.get('scope') or ''}",
            f"有效期    {grants_mod.bj_time(cred['not_before'])} → "
            f"{grants_mod.bj_time(cred['expire'])}（{_duration(int(payload['hours']))}）",
            "",
            "查看凭证：",
            link,
            "",
            "这个地址可以反复打开，每次打开都会记录在申请单里。",
            "密钥只在链接的 # 之后，服务端存的是密文、自己也解不开 —— 链接丢了只能重新申请。",
            "请勿转发：拿到链接就等于拿到凭证。",
        ]
    )


def _imported(ticket: dict) -> bool:
    """这张单子是从别处（bot 的发放记录、云上对账）导进来的台账，不是面板发的。

    判据是事件表里有 `imported` —— 导入时写的，之后只增不改。这类记录云上那份东西
    归发放方管，面板不碰：它们没有 `cred_user`，所以本来也删不动，这里只是别让
    台账写出「已到期回收」这种面板根本没做过的事。
    """
    return any(e.get("event") == "imported" for e in ticket.get("events") or ())


def password_claims(ticket: dict) -> int:
    """有效的初始密码领取次数：领取事件减去失败作废的事件。"""
    events = [e.get("event") for e in ticket.get("events", [])]
    return events.count("password_issued") - events.count("password_failed")


class Flows:
    def __init__(
        self,
        *,
        store: t.TicketStore,
        catalog: Callable[[], catalog_mod.Catalog],
        approval: Callable[[], Optional[FeishuApproval]],
        roster: Callable[[], object],
        executor: Callable[[str, str], object],
        #: 凭证发放身份。只有长期凭证的建号 / 清理走它，和开通身份是两把不同的 AK
        issuer: Optional[Callable[[str, str], object]] = None,
        add_manual_link: Optional[Callable[[str, str, str], None]] = None,
        current_groups: Optional[Callable[[str, str, str], Optional[set]]] = None,
        policy_snapshot: Optional[Callable[[], Optional[dict]]] = None,
        policy_rules: Optional[Callable[[], policies_mod.Rules]] = None,
        current_policies: Optional[policies_mod.CurrentPolicies] = None,
        notify: Optional[Callable[[str, dict], None]] = None,
        executor_ready: Optional[Callable[[str, str], bool]] = None,
        #: (platform, account) -> bool；这个云账号的凭证发放身份配了没有。None = 不判断
        issuer_ready: Optional[Callable[[str, str], bool]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self._catalog = catalog
        self._approval = approval
        self._roster = roster
        self._executor = executor
        self._issuer = issuer
        self._add_manual_link = add_manual_link
        self._current_groups = current_groups
        self._policy_snapshot = policy_snapshot or (lambda: None)
        self._policy_rules = policy_rules or policies_mod.Rules
        self._current_policies = current_policies
        self._notify = notify
        #: (platform, account) -> bool；这个云账号的开通身份配了没有。None = 不判断
        self._executor_ready = executor_ready
        self._issuer_ready = issuer_ready
        self._clock = clock
        self._last_sync: dict = {}

    def _issuer_for(self, platform: str, account: str):
        """凭证发放身份。没接就回落开通身份。

        **所有**用到发放身份的地方都走这里：签发、失败清理、到期回收。分散写
        `self._issuer or self._executor` 的话，某条路会写成 `else None`，那条路上的
        清理就被静默跳过 —— 云上留一把没人知道的 AK，台账上一个字都没有。
        """
        if self._issuer is None:
            return self._executor(platform, account)
        return self._issuer(platform, account)

    # ── 员工能申请什么 ────────────────────────────────────────────────────
    def my_accounts(self, union_id: str) -> list:
        """本人名册里已确认的云子账号：权限申请只能选这些。"""
        person = self._roster().resolve(union_id=union_id).person
        if person is None:
            return []
        return [
            {"platform": r.platform, "account": r.account, "name": r.name} for r in person.accounts
        ]

    def options(self, union_id: str) -> list:
        """每个模板对本人的状态，申请页据此显示「可申请 / 已拥有 / 申请中 / 不可申请」。

        state:
          available    可以提交
          owned        已经有了（权限：快照里子账号已在全部用户组；开账号：名册里已有子账号）。
                       权限类仍可提交，用于续期
          pending      有一张同模板的申请还没结束（request_id 指向它）
          unavailable  不能申请，原因见 state_note
        「已拥有」只是提示：快照可能过期，真正开通前执行器会再查一次云上现状。
        """
        names = {(a["platform"], a["account"]): a["name"] for a in self.my_accounts(union_id)}
        tickets = self.store.mine(union_id) if union_id else []
        now = self._clock()
        out = []
        for tpl in self._catalog().templates:
            item = tpl.public()
            name = names.get((tpl.platform, tpl.account), "")
            same = [x for x in tickets if (x.get("template") or {}).get("id") == tpl.id]
            open_ = next((x for x in reversed(same) if x.get("status") in t.OPEN), None)
            state, note, ref, expires = "available", "", "", ""
            if tpl.kind == catalog_mod.KIND_ACCOUNT:
                created = next(
                    (
                        x
                        for x in reversed(tickets)
                        if x.get("kind") == catalog_mod.KIND_ACCOUNT
                        and x.get("status") == t.DONE
                        and (x.get("template") or {}).get("platform") == tpl.platform
                        and (x.get("template") or {}).get("account") == tpl.account
                    ),
                    None,
                )
                if name:
                    state, note = "owned", f"你已有子账号 {name}"
                elif created is not None:
                    username = (created.get("payload") or {}).get("username", "")
                    state, note = "owned", f"子账号 {username} 已开通，名册刷新后显示"
            elif tpl.kind == catalog_mod.KIND_RESOURCE:
                pass  # 面板不创建资源，既不需要开通身份，也不要求申请人先有子账号
            elif self._executor_ready is not None and not self._executor_ready(
                tpl.platform, tpl.account
            ):
                # 执行身份没配就别让人白提一轮：提交、等审批、最后卡在开通那步失败。
                # 注入而不是直接读环境变量：测试和 CLI 里没有这套环境，不该因此把模板全置灰
                state, note = "unavailable", "这个云账号还没配置开通身份，请联系管理员"
            elif (
                tpl.kind == catalog_mod.KIND_CREDENTIAL
                and not tpl.role_arn
                and self._issuer_ready is not None
                and not self._issuer_ready(tpl.platform, tpl.account)
            ):
                # 没配角色的模板只能走长期路径，而长期路径要发放身份。配了角色的模板
                # 不在这里置灰：12 小时以内的申请照样能办，超时长的在提交时才拦
                state, note = "unavailable", "这个云账号还没配置凭证发放身份，请联系管理员"
            elif not name and tpl.kind != catalog_mod.KIND_CREDENTIAL:
                state, note = "unavailable", "你在这个云账号下还没有子账号，先申请开账号"
            elif tpl.kind == catalog_mod.KIND_PERMISSION and self._has_groups(tpl, name):
                state, note = "owned", f"子账号 {name} 已有这项权限"
                grant = next(
                    (
                        x
                        for x in reversed(same)
                        if x.get("status") == t.DONE
                        and (x.get("payload") or {}).get("cloud_user") == name
                        and float(x.get("expires_at_ts") or 0) > now
                    ),
                    None,
                )
                if grant:
                    expires = str(grant.get("expires_at") or "")
            if open_ is not None and state != "unavailable":
                ref = open_["id"]
                state, note = "pending", "已提交，还没处理完"
            item.update(
                state=state,
                state_note=note,
                request_id=ref,
                expires_at=expires,
                cloud_user=name,
                available=state in ("available", "owned")
                and not (tpl.kind == catalog_mod.KIND_ACCOUNT and state == "owned"),
            )
            item["unavailable_reason"] = "" if item["available"] else note
            out.append(item)
        return out

    def _has_groups(self, tpl: catalog_mod.Template, name: str) -> bool:
        if self._current_groups is None:
            return False
        try:
            current = self._current_groups(tpl.platform, tpl.account, name)
        except Exception:  # noqa: BLE001 — 只是提示，快照读不了就当「未知」，不能让申请页 500
            return False
        if current is None:
            return False
        have = {g.lower() for g in current}
        return all(g.lower() in have for g in tpl.groups)

    def policy_options(self, union_id: str) -> dict:
        """权限列表页：本人有子账号的每个云账号里，全部权限策略对本人的状态。

        state: available 可申请 / owned 已有（快照里直接授予或经用户组继承）/
               pending 在进行中的申请里 / unavailable 规则不开放（原因见 state_note）。
        快照没有或没采全时「已有」未知，按可申请显示；真正开通前执行器会再查云上现状。
        """
        rules = self._policy_rules()
        data = self._policy_snapshot()
        tickets = self.store.mine(union_id) if union_id else []
        now = self._clock()
        seen, accounts = set(), []
        for mine in self.my_accounts(union_id) if union_id else []:
            key = (mine["platform"], mine["account"])
            if key in seen:
                continue  # 同一云账号有多个子账号：列表按第一个算
            seen.add(key)
            accounts.append(self._policy_account(mine, data, rules, tickets, now))
        return {
            "captured_at": str((data or {}).get("captured_at") or ""),
            "max_per_request": rules.max_per_request,
            "accounts": accounts,
        }

    def policy_rules_overview(self) -> dict:
        """管理后台：每个已采集云账号里，哪些策略对员工开放、哪些不开放（和原因），以及当前规则。

        只读。给管理员对照真实策略列表检查禁用清单有没有漏网的高风险策略。
        """
        rules = self._policy_rules()
        data = self._policy_snapshot()
        accounts = []
        for acc in (data or {}).get("accounts") or []:
            item = {
                "platform": str(acc.get("platform") or ""),
                "account": str(acc.get("account") or ""),
                "error": str(acc.get("error") or ""),
                "stale": bool(acc.get("stale")),
                "policies": [],
            }
            items = acc.get("policies")
            for p in items if isinstance(items, list) else []:
                if not isinstance(p, dict):
                    continue
                ptype, name = str(p.get("type") or ""), str(p.get("name") or "")
                denied = rules.denied(ptype, name)
                item["policies"].append(
                    {
                        "type": ptype,
                        "name": name,
                        "description": str(p.get("description") or ""),
                        "service": str(p.get("service") or ""),
                        "risk": rules.risk_of(ptype, name),
                        "max_days": rules.max_days_of(ptype, name),
                        "open": not denied,
                        "reason": denied,
                    }
                )
            item["policies"].sort(
                key=lambda r: (r["open"], r["service"].lower(), r["name"].lower())
            )
            accounts.append(item)
        return {
            "captured_at": str((data or {}).get("captured_at") or ""),
            # 和管理员那条 /api/admin/policies/rules 用同一份视图：两处各拼一遍的话，
            # 前端按其中一份判断「这条改不改得动」，迟早对不上
            "rules": policies_mod.rules_view(rules),
            "accounts": accounts,
        }

    def _policy_account(self, mine: dict, data, rules, tickets: list, now: float) -> dict:
        platform, account, user = mine["platform"], mine["account"], mine["name"]
        out = {
            "platform": platform,
            "account": account,
            "cloud_user": user,
            "error": "",
            "stale": False,
            "total": 0,
            "policies": [],
        }
        entry = policies_mod.account_entry(data, platform, account)
        # 最近一次采集失败、沿用的是之前的列表：照常能申请，页面上提示可能不是最新
        out["stale"] = bool(entry and entry.get("stale"))
        items = policies_mod.directory(data, platform, account)
        if items is None:
            out["error"] = "权限列表还没采集" if entry is None else "权限列表暂时不可用"
            return out
        try:
            current = (
                self._current_policies(platform, account, user) if self._current_policies else None
            )
        except Exception:  # noqa: BLE001 — 只是提示，快照读不了就当「未知」
            current = None
        pending, expires, granted = {}, {}, {}
        for x in tickets:
            tpl = x.get("template") or {}
            if (
                tpl.get("id") != POLICY_TEMPLATE
                or (tpl.get("platform"), tpl.get("account")) != (platform, account)
                or (x.get("payload") or {}).get("cloud_user") != user
            ):
                continue
            for key in _policy_keys(x):
                if x.get("status") in t.OPEN:
                    pending[key] = x["id"]
                elif x.get("status") == t.DONE and float(x.get("expires_at_ts") or 0) > now:
                    expires[key] = str(x.get("expires_at") or "")
                    granted[key] = x["id"]
        rows = []
        for p in items:
            ptype, name = p["type"], p["name"]
            key = f"{ptype}:{name}"
            state, note, ref = "available", "", ""
            denied = rules.denied(ptype, name)
            have = (current or {}).get(name.lower())
            if denied:
                state, note = "unavailable", denied
            elif key in pending:
                state, note, ref = "pending", "已提交，还没处理完", pending[key]
            elif have:
                state, note = "owned", have[1]
            elif key in granted:
                # 刚通过申请开通、权限快照还没刷新：按申请单算已拥有
                state, note, ref = "owned", "通过申请开通", granted[key]
            rows.append(
                {
                    "type": ptype,
                    "name": name,
                    "description": p.get("description", ""),
                    "service": p.get("service", ""),
                    "risk": rules.risk_of(ptype, name),
                    "max_days": rules.max_days_of(ptype, name),
                    "state": state,
                    "state_note": note,
                    "request_id": ref,
                    "expires_at": expires.get(key, "") if state == "owned" else "",
                }
            )
        rows.sort(key=lambda r: (r["service"].lower(), r["name"].lower()))
        out.update(total=len(rows), policies=rows)
        return out

    def approval_links(self, ticket: dict) -> dict:
        """申请单对应飞书审批实例的跳转链接；审批没配置或实例编号缺失时为空串。"""
        code = (ticket.get("approval") or {}).get("instance_code")
        try:
            approval = self._approval()
        except Exception:  # noqa: BLE001 — 链接只是便利，审批配置读不了也不能让详情页打不开
            approval = None
        return instance_links(approval.config if approval else None, code)

    # ── 提交 ──────────────────────────────────────────────────────────────
    def submit(
        self, *, applicant: Applicant, email: str, template_id: str, payload: dict, reason: str
    ) -> dict:
        approval = self._approval()
        if approval is None:
            raise FlowError("还没有配置飞书审批，暂时不能提交申请", 503)
        reason = str(reason or "").strip()
        if str(template_id or "") == POLICY_TEMPLATE:
            snapshot, clean, summary = self._validate_policy(applicant, payload)
        else:
            tpl = self._catalog().get(str(template_id or ""))
            if tpl is None:
                raise FlowError("没有这个申请模板")
            clean, summary = None, None
            snapshot = _snapshot(tpl)
        if not _REASON_MIN <= len(reason) <= _REASON_MAX:
            raise FlowError(f"申请理由需要 {_REASON_MIN}–{_REASON_MAX} 个字")
        if clean is None:
            clean, summary = self._validate(tpl, applicant, payload)
        for other in self.store.mine(applicant.union_id):
            if (
                other.get("status") in t.OPEN
                and other.get("template", {}).get("id") == snapshot["id"]
                and other.get("payload") == clean
            ):
                raise FlowError(f"已有一张相同的申请 {other['id']} 还没结束", 409)

        ticket = self.store.create(
            {
                "kind": snapshot["kind"],
                "template": snapshot,
                "applicant": _applicant_dict(applicant, email),
                "payload": clean,
                "reason": reason,
                "summary": summary,
            },
            actor=applicant.union_id,
        )
        try:
            code = approval.create(
                ticket_id=ticket["id"],
                kind_label=catalog_mod.KIND_LABELS[snapshot["kind"]],
                summary=summary,
                reason=reason,
                applicant=applicant,
            )
        except Exception as exc:  # noqa: BLE001 — 任何异常都要落到「提交失败」，不能卡在「提交中」
            return self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.SUBMITTING],
                to=t.SUBMIT_FAILED,
                event="submit_failed",
                note=describe_error(exc) or type(exc).__name__,
            )
        return self.store.update(
            ticket["id"],
            actor="system",
            expect=[t.SUBMITTING],
            to=t.PENDING,
            event="approval_created",
            note="已发起飞书审批",
            fields={"approval": {"instance_code": code, "status": STATUS_PENDING}},
        )

    def _validate_policy(self, applicant: Applicant, payload: object) -> tuple:
        """从策略目录申请：返回 (模板快照, 规范化的 payload, 审批摘要)。

        每条策略都必须在**当前**策略目录里、没被规则禁用；天数不超过所选策略里最短的上限。
        """
        payload = payload if isinstance(payload, dict) else {}
        platform = str(payload.get("platform") or "")
        account = str(payload.get("account") or "")
        user = str(payload.get("cloud_user") or "")
        mine = {
            a["name"]
            for a in self.my_accounts(applicant.union_id)
            if (a["platform"], a["account"]) == (platform, account)
        }
        if not user or user not in mine:
            raise FlowError("只能给名册里确认属于你自己的子账号申请权限")
        rules = self._policy_rules()
        items = policies_mod.directory(self._policy_snapshot(), platform, account)
        if items is None:
            raise FlowError("这个云账号的权限列表还没采集，暂时不能按策略申请", 409)
        wanted = payload.get("policies")
        if not isinstance(wanted, list) or not wanted:
            raise FlowError("至少选择一条权限策略")
        chosen = {}
        for w in wanted:
            if not isinstance(w, dict):
                raise FlowError("权限策略格式不对")
            ptype, name = str(w.get("type") or ""), str(w.get("name") or "")
            if policies_mod.find(items, ptype, name) is None:
                raise FlowError(f"权限列表里没有策略 {name[:80]}（{ptype}），请刷新后重选")
            denied = rules.denied(ptype, name)
            if denied:
                raise FlowError(f"{name}：{denied}")
            chosen[(ptype, name)] = {"type": ptype, "name": name}
        if len(chosen) > rules.max_per_request:
            raise FlowError(f"一次最多申请 {rules.max_per_request} 条策略")
        limit = min(rules.max_days_of(p, n) for p, n in chosen)
        days = payload.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= limit:
            raise FlowError(f"授权天数必须在 1–{limit} 之间（按所选策略里风险最高的算）")
        ordered = sorted(chosen.values(), key=lambda p: (p["type"], p["name"].lower()))
        for other in self.store.mine(applicant.union_id):
            o_tpl = other.get("template") or {}
            if (
                other.get("status") in t.OPEN
                and o_tpl.get("id") == POLICY_TEMPLATE
                and (o_tpl.get("platform"), o_tpl.get("account")) == (platform, account)
                and (other.get("payload") or {}).get("cloud_user") == user
            ):
                overlap = set(_policy_keys(other)) & {_policy_key(p) for p in ordered}
                if overlap:
                    names = "、".join(sorted(k.split(":", 1)[1] for k in overlap))
                    raise FlowError(f"{names} 已在申请 {other['id']} 中，等它结束后再申请", 409)
        risky = [{**p, "risk": rules.risk_of(p["type"], p["name"])} for p in ordered]
        highest = max((p["risk"] for p in risky), key=lambda r: _RISK_ORDER[r])
        snapshot = {
            "id": POLICY_TEMPLATE,
            "kind": catalog_mod.KIND_PERMISSION,
            "platform": platform,
            "account": account,
            "title": _POLICY_TITLE,
            "groups": [],
            "policies": risky,
            "max_days": limit,
            "risk": highest,
        }
        lines = "；".join(
            f"{p['name']}（{policies_mod.TYPE_LABELS[p['type']]}，"
            f"{policies_mod.RISK_LABELS[p['risk']]}）"
            for p in risky
        )
        summary = (
            f"云账号权限「{_POLICY_TITLE}」：给 {platform}/{account} 的子账号 {user} "
            f"授予 {len(risky)} 条策略，{days} 天：{lines}"
        )
        clean = {"cloud_user": user, "days": days, "policies": ordered}
        return snapshot, clean, summary

    def _validate(self, tpl: catalog_mod.Template, applicant: Applicant, payload: dict) -> tuple:
        payload = payload if isinstance(payload, dict) else {}
        where = f"{catalog_mod.KIND_LABELS[tpl.kind]}「{tpl.title}」"
        mine = [
            a
            for a in self.my_accounts(applicant.union_id)
            if (a["platform"], a["account"]) == (tpl.platform, tpl.account)
        ]
        if tpl.kind == catalog_mod.KIND_PERMISSION:
            user = str(payload.get("cloud_user") or "")
            if user not in {a["name"] for a in mine}:
                raise FlowError("只能给名册里确认属于你自己的子账号申请权限")
            days = payload.get("days", tpl.max_days or 0)
            if (
                not isinstance(days, int)
                or isinstance(days, bool)
                or not 0 <= days <= _MAX_DAYS
                or (tpl.max_days and not 1 <= days <= tpl.max_days)
            ):
                raise FlowError(
                    f"授权天数必须在 1–{tpl.max_days} 之间" if tpl.max_days else "授权天数不对"
                )
            groups = "、".join(tpl.groups)
            return (
                {"cloud_user": user, "days": days},
                f"{where}：把 {tpl.platform}/{tpl.account} 的子账号 {user} 加入用户组 {groups}"
                + (f"，{days} 天" if days else ""),
            )
        if tpl.kind == catalog_mod.KIND_CREDENTIAL:
            # 刻意**不要求**申请人在这个云账号下已有子账号：访问凭证常常是替外部合作方申请的，
            # 凭证本来就不是发给申请人自己用。bot 那条老路也没有这个门槛，迁过来不该变严。
            # 把关的是审批 + 模板白名单（只有模板列出的桶能选）。
            return self._validate_credential(tpl, applicant, payload, where)
        if tpl.kind == catalog_mod.KIND_RESOURCE:
            # 资源申请不要求申请人在这个云账号下有子账号：面板一行云都不写，
            # 只是登记 + 走审批，拿这个当门槛只会把提需求的人挡在外面
            return self._validate_resource(tpl, payload, where)
        username = str(payload.get("username") or "").strip()
        if not re.fullmatch(tpl.username_pattern, username) or not _SAFE_USERNAME.match(username):
            raise FlowError(
                "用户名不符合规则：小写字母或数字开头，只用小写字母、数字、点、下划线和横线"
            )
        if mine:
            raise FlowError("你在这个云账号下已经有子账号了")
        for other in self.store.all():
            if other.get("kind") != catalog_mod.KIND_ACCOUNT:
                continue
            o_tpl = other.get("template") or {}
            if (o_tpl.get("platform"), o_tpl.get("account")) != (tpl.platform, tpl.account):
                continue
            status = other.get("status")
            if other["applicant"].get("union_id") == applicant.union_id and (
                status in t.OPEN or status == t.DONE
            ):
                raise FlowError(f"你已经有一张这个云账号的开账号申请 {other['id']}", 409)
            same_name = (other.get("payload") or {}).get("username") == username
            # 平台建过的用户名不再复用：否则旧单子的申请人仍是名册里的主人，可以领到新账号的密码
            if same_name and (status in t.OPEN or other.get("user_created")):
                raise FlowError("这个用户名已经被别的申请占用，请换一个", 409)
        groups = "、".join(tpl.groups) or "无"
        return (
            {"username": username},
            f"{where}：在 {tpl.platform}/{tpl.account} 新建子账号 {username}，默认用户组 {groups}"
            + ("，开通控制台登录" if tpl.console_login else ""),
        )

    def _validate_credential(
        self, tpl: catalog_mod.Template, applicant: Applicant, payload: dict, where: str
    ) -> tuple:
        """访问凭证：桶 + 目录 + 时长 + 使用方。

        **不问「要短期还是长期」**——那是实现细节，申请人只关心用多久、能干什么。
        时长决定用哪条路（≤12 小时 STS，超过就建带时间窗的子账号），权限由模板决定。
        """
        regions = dict(tpl.buckets)
        bucket = str(payload.get("bucket") or "")
        if bucket not in regions:
            raise FlowError("请从这个模板允许的桶里选一个")
        raw_prefix = str(payload.get("prefix") or "").strip()
        if raw_prefix and not tpl.allow_prefix:
            raise FlowError("这个模板不支持只开放某个子目录")
        try:
            prefix = grants_mod.check_prefix(raw_prefix)
        except grants_mod.GrantError as exc:
            raise FlowError(str(exc)) from None
        hours = payload.get("hours", 1)
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= tpl.max_hours:
            raise FlowError(f"有效时长必须在 1–{tpl.max_hours} 小时之间")
        # 使用方名称会原样进审批评论。留着换行就能在评论里伪造一整行假的
        # 「AccessKeySecret：…」，把使用方骗到别处去——压成单空格，不留这个面
        subject = re.sub(r"\s+", " ", str(payload.get("subject") or "")).strip()
        subject = subject or (applicant.name or "")
        if not subject or len(subject) > _SUBJECT_MAX:
            raise FlowError(f"使用方名称不能为空，最长 {_SUBJECT_MAX} 个字")
        # 会话策略有 2048 字符硬上限，而目录名越长策略越大。**在这里先算一遍**：
        # 不算的话，超长要等到提交成功、审批通过、开通那一刻才炸，白等一轮审批，
        # 还留一张要人工处理的失败单。同时也顺带验了桶名/目录/能力集能不能拼出合法策略
        long_term = hours > catalog_mod.STS_MAX_HOURS or not tpl.role_arn
        now = self._clock()
        try:
            doc = grants_mod.build_policy(
                tpl.platform,
                bucket,
                prefix=prefix,
                caps=tpl.caps,
                not_before=now,
                expire=now + hours * 3600,
            )
            if not long_term:
                grants_mod.session_policy(doc)
        except grants_mod.GrantError as exc:
            raise FlowError(f"这个申请拼不出合法的权限策略：{exc}") from None
        if (
            long_term
            and self._issuer_ready is not None
            and not self._issuer_ready(tpl.platform, tpl.account)
        ):
            raise FlowError(
                f"这个云账号还没配置凭证发放身份，暂时只能申请 {catalog_mod.STS_MAX_HOURS} "
                f"小时以内的凭证，请联系管理员"
                if tpl.role_arn
                else "这个云账号还没配置凭证发放身份，请联系管理员"
            )
        # 凭证只有审批评论这一条出口。通道没配就别让人提了等审批 —— 走到开通那步
        # 才失败，这轮审批白等，还留一张要人工处理的失败单
        approval = self._approval()
        if approval is not None and not approval.config.comment_open_id:
            raise FlowError("还没配置凭证下发的评论身份，暂时不能申请访问凭证，请联系管理员", 503)
        # 同理：取件地址拼不出来、加密库不在，也都是「审批通过那一刻才炸」
        view_base()
        try:
            sealed.selfcheck()
        except sealed.SealError as exc:
            raise FlowError(f"面板的加密功能不可用，暂时不能申请访问凭证：{exc}", 503) from None
        caps = "、".join(catalog_mod.CAP_LABELS[c] for c in tpl.caps)
        # scheme 按平台取，别写死 oss://——火山的桶写成 oss:// 会让审批人以为申请错了云
        scheme = platforms_mod.get(tpl.platform).storage_scheme
        scope = f"{scheme}://{bucket}/{prefix}" if prefix else f"{scheme}://{bucket}/（整桶）"
        return (
            {"bucket": bucket, "prefix": prefix, "hours": hours, "subject": subject},
            f"{where}：给「{subject}」发一份 {tpl.platform}/{tpl.account} 的访问凭证，"
            f"有效期 {_duration(hours)}，范围 {scope}，权限 {caps}。"
            f"凭证以本审批的评论下发，不回写面板。",
        )

    def _validate_resource(self, tpl: catalog_mod.Template, payload: dict, where: str) -> tuple:
        """资源开通：能选的一律选，不让填。

        自由填写的规格调不了云 API（镜像、交换机、安全组全缺），也让审批人没法判断批的
        到底是什么。所以配了选项轴的模板一律走选择；没配的才回落成一段自由描述，
        那种模板本来也是人工开通。
        """
        picked, numbers, parts = {}, {}, []
        if tpl.options:
            raw = payload.get("choices")
            if not isinstance(raw, dict):
                raise FlowError("请把每一项都选上")
            numbers_in = payload.get("numbers")
            numbers_in = numbers_in if isinstance(numbers_in, dict) else {}
            # 被隐藏的轴不校验也不进摘要：选了「不要公网 IP」就不该再问计费方式。
            # 按当前选择算，不是按模板静态算 —— 隐藏与否取决于别的轴选了什么
            skip = tpl.hidden_axes({k: str(v) for k, v in raw.items()})
            for axis in tpl.options:
                if axis.id in skip:
                    continue
                if axis.number is not None:
                    try:
                        value = axis.number.clean(numbers_in.get(axis.id, axis.number.default))
                    except catalog_mod.CatalogError as exc:
                        raise FlowError(f"「{axis.label}」{exc}") from None
                    numbers[axis.id] = value
                    if value == 0 and axis.number.omit_zero:
                        parts.append(f"{axis.label} 不要")
                    else:
                        parts.append(f"{axis.label} {value}{axis.number.unit}")
                    continue
                choice = axis.choice(str(raw.get(axis.id) or ""))
                if choice is None:
                    raise FlowError(f"「{axis.label}」没选，或选了一个不存在的项")
                picked[axis.id] = choice[0]
                parts.append(f"{axis.label} {choice[1]}")
            spec = "、".join(parts)
        else:
            spec = re.sub(r"\s+", " ", str(payload.get("spec") or "")).strip()
            if not 2 <= len(spec) <= catalog_mod.SPEC_MAX:
                raise FlowError(f"规格需要 2–{catalog_mod.SPEC_MAX} 个字")

        cost_center, cost_label = "", ""
        if tpl.cost_centers:
            cost_center = str(payload.get("cost_center") or "")
            cost_label = next((n for i, n in tpl.cost_centers if i == cost_center), "")
            if not cost_label and tpl.cost_center_other and cost_center == catalog_mod.COST_OTHER:
                # 清单外的自己填。压平换行同 spec / detail —— 它一样会进审批摘要
                cost_label = re.sub(r"\s+", " ", str(payload.get("cost_center_name") or "")).strip()
                if not 2 <= len(cost_label) <= _COST_NAME_MAX:
                    raise FlowError(f"请写清楚算在谁头上，2–{_COST_NAME_MAX} 个字")
            if not cost_label:
                raise FlowError("请选择成本归属")

        # 和 spec / subject 同样压平：它会原样进审批摘要，留着换行就能伪造一整行
        # 看起来像平台自己写的声明，审批人未必分得出来
        detail = re.sub(r"\s+", " ", str(payload.get("detail") or "")).strip()
        if len(detail) > catalog_mod.SPEC_MAX:
            raise FlowError(f"补充说明最长 {catalog_mod.SPEC_MAX} 个字")

        until, days = self._until(tpl, payload)
        clean = {
            "spec": spec,
            "detail": detail,
            "days": days,
            "until": until,
            "choices": picked,
            "numbers": numbers,
            "cost_center": cost_center,
            "cost_center_name": cost_label,
        }
        summary = (
            f"{where}：在 {tpl.platform}/{tpl.account} 开通 {spec}"
            + (f"，成本归属 {cost_label}" if cost_label else "")
            + (f"，用到 {until}" if until else "，长期")
            + (f"。说明：{detail}" if detail else "")
        )
        return clean, summary

    def _until(self, tpl: catalog_mod.Template, payload: dict) -> tuple:
        """(到期日 YYYY-MM-DD, 天数)。

        选日期而不是填天数：「用到几月几号」是人真正在想的事，「用 97 天」不是。
        天数仍然留着 —— 到期时间按它算，模板的上限也是按天写的。
        """
        raw = str(payload.get("until") or "").strip()
        if not tpl.max_days:
            if raw:
                raise FlowError("这个模板是长期占用，不用填到期日期")
            return "", 0
        if not raw:
            raise FlowError("请选择用到哪天")
        try:
            end = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise FlowError("到期日期格式不对，应该是 2026-12-31 这样") from None
        today = datetime.fromtimestamp(self._clock(), _BJ).date()
        days = (end - today).days
        if days < 1:
            raise FlowError("到期日期要晚于今天")
        if days > tpl.max_days:
            raise FlowError(f"最长只能用 {tpl.max_days} 天，到期日期不能晚于这个范围")
        return raw, days

    # ── 同步审批 ──────────────────────────────────────────────────────────
    def sync(self, ticket_id: str, *, force: bool = False) -> dict:
        ticket = self.store.get(ticket_id)
        if ticket.get("status") != t.PENDING:
            return ticket
        now = self._clock()
        if not force and now - self._last_sync.get(ticket_id, 0) < _SYNC_INTERVAL:
            return ticket
        self._last_sync[ticket_id] = now
        if len(self._last_sync) > 2000:
            self._last_sync = {k: v for k, v in self._last_sync.items() if now - v < _SYNC_INTERVAL}
        approval = self._approval()
        if approval is None:
            return ticket
        code = ticket["approval"]["instance_code"]
        status = approval.status(
            instance_code=code, ticket_id=ticket_id, applicant=_applicant(ticket)
        )
        if status == STATUS_PENDING:
            return ticket
        if status != STATUS_APPROVED:
            to = t.REJECTED if status == "REJECTED" else t.WITHDRAWN
            ticket = self.store.update(
                ticket_id,
                actor="feishu",
                expect=[t.PENDING],
                to=to,
                event="approval_" + status.lower(),
                fields={"approval": {"instance_code": code, "status": status}},
            )
            return self._emit("rejected" if to == t.REJECTED else "withdrawn", ticket)
        ticket = self.store.update(
            ticket_id,
            actor="feishu",
            expect=[t.PENDING],
            to=t.APPROVED,
            event="approval_approved",
            note="飞书审批已通过",
            fields={"approval": {"instance_code": code, "status": status}},
        )
        return self.execute(ticket_id, actor="system")

    def _expires_at(self, ticket: dict, now: float) -> float:
        """开通后什么时候到期。权限 / 资源按天，访问凭证按小时；0 = 不设期限。"""
        payload = ticket.get("payload") or {}
        kind = ticket.get("kind")
        if kind == catalog_mod.KIND_CREDENTIAL:
            return now + int(payload.get("hours") or 0) * 3600
        if kind in (catalog_mod.KIND_PERMISSION, catalog_mod.KIND_RESOURCE):
            return now + int(payload.get("days") or 0) * 86400 if payload.get("days") else 0.0
        return 0.0

    def _close_self_approved(self, ticket_id: str, exc: Exception) -> dict:
        closed = self.store.update(
            ticket_id,
            actor="system",
            expect=[t.APPROVED],
            to=t.CLOSED,
            event="approval_invalid",
            note=describe_error(exc) or "审批没有经过申请人以外的审批人同意",
        )
        return self._emit("rejected", closed)

    # ── 开通 ──────────────────────────────────────────────────────────────
    def _verify_approval(self, ticket: dict) -> None:
        approval = self._approval()
        if approval is None:
            raise FlowError("没有配置飞书审批，不能开通", 503)
        approval.verify_approved(
            instance_code=ticket["approval"]["instance_code"],
            ticket_id=ticket["id"],
            applicant=_applicant(ticket),
        )

    def _verify(self, ticket: dict, *, fields: tuple = _EXEC_FIELDS) -> catalog_mod.Template:
        """实时核对飞书审批，并确认模板里决定「往云上写什么」的字段和提交时一致。

        只比 fields：标题、说明、风险标签这类展示字段改了不影响已批准的单子。
        """
        self._verify_approval(ticket)
        if ticket["template"].get("id") == POLICY_TEMPLATE:
            return self._verify_policy(ticket)
        tpl = self._catalog().get(ticket["template"]["id"])
        if tpl is None or _effective(_snapshot(tpl), fields) != _effective(
            ticket["template"], fields
        ):
            raise FlowError("申请模板在审批期间被修改或删除，请重新提交申请", 409)
        return tpl

    def _verify_policy(self, ticket: dict) -> catalog_mod.Template:
        """按策略申请的单子：审批期间策略从目录里没了、被规则禁用、上限天数被调低，都不能开通。

        规则放宽不影响（审批人批的是提交时的内容，放宽不会多给）。
        """
        tpl = ticket["template"]
        rules = self._policy_rules()
        items = policies_mod.directory(self._policy_snapshot(), tpl["platform"], tpl["account"])
        if items is None:
            raise FlowError("这个云账号的权限列表不可用，暂时不能开通", 409)
        user = ticket["payload"].get("cloud_user")
        owned = {
            a["name"]
            for a in self.my_accounts(ticket["applicant"]["union_id"])
            if (a["platform"], a["account"]) == (tpl["platform"], tpl["account"])
        }
        if user not in owned:
            raise FlowError(f"子账号 {user} 在名册里已不属于申请人，不能开通", 409)
        days = int(ticket["payload"].get("days") or 0)
        for policy in tpl.get("policies") or []:
            ptype, name = policy["type"], policy["name"]
            if policies_mod.find(items, ptype, name) is None:
                raise FlowError(f"策略 {name} 已不在权限列表里，请重新提交申请", 409)
            if rules.denied(ptype, name):
                raise FlowError(f"策略 {name} 在审批期间被设为不开放申请，不能开通", 409)
            if days > rules.max_days_of(ptype, name):
                raise FlowError(f"策略 {name} 的最长授权天数在审批期间被调低，请重新提交申请", 409)
        return catalog_mod.Template(
            id=POLICY_TEMPLATE,
            kind=catalog_mod.KIND_PERMISSION,
            platform=tpl["platform"],
            account=tpl["account"],
            title=_POLICY_TITLE,
        )

    def execute(self, ticket_id: str, *, actor: str) -> dict:
        ticket = self.store.get(ticket_id)
        if ticket.get("status") not in (t.APPROVED, t.FAILED):
            status = ticket.get("status")
            raise FlowError(f"申请单当前是「{t.LABELS.get(status, status)}」，不能开通", 409)
        try:
            tpl = self._verify(ticket)
        except SelfApprovalError as exc:
            # 审批「通过」了但只有申请人自己点的同意：这单不作数，直接关闭。
            # 不落「开通失败」——失败是可以重试的，而这张单子无论重试多少次都不该开通
            if ticket.get("status") == t.APPROVED:
                return self._close_self_approved(ticket_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001 — 已通过的单子核对不过要落到「开通失败」，不能停在「已通过」
            if ticket.get("status") == t.APPROVED:
                ticket = self.store.update(
                    ticket_id,
                    actor=actor,
                    expect=[t.APPROVED],
                    to=t.EXECUTING,
                    event="execute_start",
                )
                failed = self.store.update(
                    ticket_id,
                    actor=actor,
                    expect=[t.EXECUTING],
                    to=t.FAILED,
                    event="execute_failed",
                    note=describe_error(exc) or "",
                )
                return self._emit("failed", failed)
            raise
        # 先占住状态再写云：两个人同时点「重试」只会有一个真的执行
        self.store.update(
            ticket_id,
            actor=actor,
            expect=[t.APPROVED, t.FAILED],
            to=t.EXECUTING,
            event="execute_start",
        )
        try:
            result = self._run(tpl, ticket)
            now = self._clock()
            fields = {"result": result, "done_at_ts": now}
            expires = self._expires_at(ticket, now)
            if expires:
                fields.update(expires_at=t.now_iso(lambda: expires), expires_at_ts=expires)
        except Exception as exc:  # noqa: BLE001 — 任何异常都要落到「开通失败」，不能卡在「开通中」
            failed = self.store.update(
                ticket_id,
                actor=actor,
                expect=[t.EXECUTING],
                to=t.FAILED,
                event="execute_failed",
                note=describe_error(exc) or type(exc).__name__,
            )
            return self._emit("failed", failed)
        # 资源开通面板一行云都不写：停在「待开通」，等管理员按 IaC 建好回来登记实例信息。
        # 直接置成「已完成」是在台账里说谎 —— 没有任何资源因为这次点击而存在
        resource = ticket["kind"] == catalog_mod.KIND_RESOURCE
        if resource:
            fields.pop("done_at_ts", None)
            fields.pop("expires_at", None)
            fields.pop("expires_at_ts", None)
        done = self.store.update(
            ticket_id,
            actor=actor,
            expect=[t.EXECUTING],
            to=t.FULFILLING if resource else t.DONE,
            event="await_fulfil" if resource else "execute_done",
            note=result,
            fields=fields,
        )
        return self._emit("fulfilling" if resource else "done", done)

    def fulfil(self, ticket_id: str, *, actor: str, note: str) -> dict:
        """管理员登记资源开通结果：实例 ID / 规格 / 地域。到期时间从这一刻起算。"""
        ticket = self.store.get(ticket_id)
        if ticket.get("kind") != catalog_mod.KIND_RESOURCE:
            raise FlowError("只有资源开通申请需要登记开通结果", 409)
        note = str(note or "").strip()
        if not note:
            raise FlowError("请写明开通了什么（实例 ID、规格、地域），这行会进台账")
        now = self._clock()
        fields = {"result": note[:500], "done_at_ts": now}
        days = int((ticket.get("payload") or {}).get("days") or 0)
        if days:
            expires = now + days * 86400
            fields.update(expires_at=t.now_iso(lambda: expires), expires_at_ts=expires)
        done = self.store.update(
            ticket_id,
            actor=actor,
            expect=[t.FULFILLING],
            to=t.DONE,
            event="fulfilled",
            note=note[:500],
            fields=fields,
        )
        return self._emit("done", done)

    def recover_stuck(
        self, ticket_id: str = "", *, actor: str, min_age: float = _STUCK_AFTER
    ) -> list:
        """「开通中 / 提交中」超过 min_age 秒没有进展（进程在写云或调飞书途中退出）：
        标成失败，交给管理员核对云上 / 飞书里的实际状态后再重试或关闭。

        ticket_id 为空时处理全部（定时任务用）；指定时只处理这一张，不满足条件就报错。
        """
        now = self._clock()
        out = []
        for ticket in self.store.all():
            if ticket_id and ticket.get("id") != ticket_id:
                continue
            status = ticket.get("status")
            if status not in (t.EXECUTING, t.SUBMITTING):
                if ticket_id:
                    raise FlowError("这张申请单没有卡在「开通中」或「提交中」", 409)
                continue
            if now - _ts(ticket.get("updated_at")) < min_age:
                if ticket_id:
                    raise FlowError(
                        f"这张申请单 {int(min_age // 60)} 分钟内还有进展，先等等再处理", 409
                    )
                continue
            executing = status == t.EXECUTING
            try:
                updated = self.store.update(
                    ticket["id"],
                    actor=actor,
                    expect=[status],
                    to=t.FAILED if executing else t.SUBMIT_FAILED,
                    event="execute_failed" if executing else "submit_failed",
                    note="处理中断，长时间没有完成。请先核对云上和飞书里的实际状态再处理",
                )
            except t.TicketError:
                continue  # 别的进程刚处理完
            if executing:
                self._emit("failed", updated)
            out.append(f"{ticket['id']}：{'开通' if executing else '提交'}中断，已标为失败")
        if ticket_id and not out:
            raise FlowError("这张申请单刚被处理过，请刷新", 409)
        return out

    def resume_approved(self, *, min_age: float = _STUCK_AFTER) -> list:
        """「已通过」却停住的单子（进程在审批通过和开通之间退出）：继续处理。

        三类都走 execute()，里面会重新实时核对飞书审批 —— 凭证也一样，签发就在那一步。
        """
        now = self._clock()
        out = []
        for ticket in self.store.all():
            if ticket.get("status") != t.APPROVED or now - _ts(ticket.get("updated_at")) < min_age:
                continue
            try:
                after = self.execute(ticket["id"], actor="system")
            except Exception as exc:  # noqa: BLE001 — 一张单子出错不影响其他单子
                out.append(f"{ticket['id']}：审批通过后继续处理失败（{describe_error(exc)}）")
                continue
            status = after.get("status")
            if status in (t.FAILED, t.WITHDRAWN, t.REJECTED):
                out.append(
                    f"{ticket['id']}：审批通过后中断，继续处理失败（{t.LABELS.get(status)}）"
                )
            else:
                out.append(
                    f"{ticket['id']}：审批通过后停住的单子已接着处理（{t.LABELS.get(status)}）"
                )
        return out

    # ── 通知 ──────────────────────────────────────────────────────────────
    def _emit(self, event: str, ticket: dict) -> dict:
        """状态已经写进申请单之后再通知。通知失败只打日志，不改申请单、不记事件。"""
        if self._notify is not None:
            try:
                self._notify(event, ticket)
            except Exception as exc:  # noqa: BLE001 — 通知永远不能影响申请单
                reason = describe_error(exc) or type(exc).__name__
                print(f"[notify] {ticket.get('id')} {event} 发送失败：{reason}", file=sys.stderr)
        return ticket

    def remind_expiring(self, days: int = 3) -> list:
        """有期限的权限在 days 天内到期：提醒申请人一次（记 expiry_reminded 事件，不重复提醒）。

        没开通知时什么都不做，也不记事件：以后开了通知，快到期的单子还能收到提醒。
        """
        # 只能发到管理员群（比如这次拿不到飞书令牌）时不提醒，也不记事件，下次再试
        if self._notify is None or not getattr(self._notify, "reaches_applicant", True):
            return []
        now = self._clock()
        out = []
        for ticket in self.store.all():
            expires = float(ticket.get("expires_at_ts") or 0)
            done_at = float(ticket.get("done_at_ts") or 0)
            if (
                ticket.get("kind")
                not in (
                    catalog_mod.KIND_PERMISSION,
                    catalog_mod.KIND_CREDENTIAL,
                    catalog_mod.KIND_RESOURCE,
                )
                or ticket.get("status") != t.DONE
                or not now < expires <= now + days * 86400
                # 本来就只开了几天的权限：开通消息里已经写了到期时间，不再紧接着提醒
                or (done_at and expires - done_at <= days * 86400)
                or any(e.get("event") == "expiry_reminded" for e in ticket.get("events") or [])
            ):
                continue
            try:
                # 先记事件再发：并发的两次定时任务只会有一次发出去
                updated = self.store.update(
                    ticket["id"],
                    actor="system",
                    expect=[t.DONE],
                    event="expiry_reminded",
                    note="已提醒申请人即将到期",
                )
            except t.TicketError:
                continue
            if sum(e.get("event") == "expiry_reminded" for e in updated.get("events") or []) > 1:
                continue
            try:
                self._notify("expiring", updated)
            except Exception as exc:  # noqa: BLE001 — 发送失败只记一笔，不改状态
                reason = describe_error(exc) or type(exc).__name__
                print(f"[notify] {ticket['id']} expiring 发送失败：{reason}", file=sys.stderr)
                with contextlib.suppress(t.TicketError):
                    self.store.update(
                        ticket["id"],
                        actor="system",
                        expect=[t.DONE],
                        event="expiry_remind_failed",
                        note="到期提醒没有发出去",
                    )
                out.append(f"{ticket['id']}：到期提醒发送失败")
                continue
            out.append(f"{ticket['id']}：已提醒即将到期")
        return out

    # ── 到期回收 ──────────────────────────────────────────────────────────
    def revoke_expired(self) -> list:
        """把到期的权限移出用户组。返回处理结果（每张单子一行）。

        不移出：开通前原本就在的组；同一个子账号还有别的未到期（或正在开通）的申请也给了这个组。
        回收是收权限，不需要审批；失败只记事件，下次再试。一张单子出错不影响其他单子。
        """
        now = self._clock()
        tickets = self.store.all()
        out = []
        for ticket in tickets:
            kind = ticket.get("kind")
            # 资源单刻意不在这里：ECS/RDS 到期只提醒，删机器这种事不能由定时任务替人决定
            if kind not in (catalog_mod.KIND_PERMISSION, catalog_mod.KIND_CREDENTIAL):
                continue
            if not self._needs_reclaim(ticket, now):
                continue
            try:
                if kind == catalog_mod.KIND_CREDENTIAL:
                    out.append(self._revoke_credential(ticket))
                else:
                    out.append(self._revoke(ticket, now))
            except Exception as exc:  # noqa: BLE001 — 一张单子出错不能挡住其他单子回收
                out.append(f"{ticket['id']}：回收失败（{describe_error(exc)}），下次重试")
        return out

    def revoke_now(self, ticket_id: str, *, actor: str) -> dict:
        """管理员手动作废一份已发出的访问凭证。链接外泄时用这个。

        两件事一起做，缺一不可：
          · **把密文清掉** —— 链接立刻打不开了，不依赖任何状态判断，也不依赖云那边成功
          · 删云上的子账号、密钥和策略（短期凭证没有残留，只清密文）

        密文先清：云那边失败还能靠定时任务重试，而链接在那之前一直是活的。

        **云那边没删成就抛错**。管理员点了「作废」，看到 200 就会认为事情办完了；而这个
        按钮唯一的使用场景就是链接外泄，报一个没发生的成功是最坏的那种谎。
        """
        ticket = self.store.get(ticket_id)
        if ticket.get("kind") != catalog_mod.KIND_CREDENTIAL:
            raise FlowError("这不是访问凭证申请", 409)
        if ticket.get("status") == t.REVOKED:
            raise FlowError("这份凭证已经作废了", 409)
        if ticket.get("status") not in _RECLAIMABLE:
            raise FlowError("这张申请单还没发出凭证", 409)
        # 没东西可作废就别做。前端按钮已经按同样的条件藏起来了，但接口自己也得挡：
        # 空转一次对失败单是有代价的（它会把单子往终态推，重试按钮就没了）
        if not ticket.get("cred_user") and not (ticket.get("sealed") or {}).get("ciphertext"):
            raise FlowError("这张申请单已经没有可作废的凭证了", 409)
        if ticket.get("sealed"):
            ticket = self.store.update(
                ticket_id,
                actor=actor,
                expect=_RECLAIMABLE,
                event="credential_sealed_dropped",
                note="管理员作废：密文已删除，查看地址立刻失效",
                fields={"sealed": {}},
            )
        self._revoke_credential(ticket, why="管理员作废")
        after = self.store.get(ticket_id)
        # 收干净了的两种样子：已完成的单子转成「已到期回收」；失败 / 已关闭的单子留在原状态，
        # 但 cred_user 被清掉（那条单子还要留着给管理员重试，不能推到终态，见 _revoke_credential）
        if after.get("status") != t.REVOKED and after.get("cred_user"):
            # 云上还留着东西。密文已经清掉了（链接死了），但账号还活着 ——
            # 定时任务会继续重试（_needs_reclaim 认「密文没了但 cred_user 还在」这个状态）
            last = (after.get("events") or [{}])[-1]
            raise FlowError(
                f"查看地址已经失效，但云上的子账号没能删掉：{last.get('note') or '原因未知'}。"
                f"定时任务会继续重试，也可以稍后再点一次",
                502,
            )
        return after

    def _needs_reclaim(self, ticket: dict, now: float) -> bool:
        """这张单子云上还有东西要收吗。

        正常情况是「已开通且到期」。凭证单多两条，都不等到期：

          · **交付失败或被关掉、但子账号已经建出来** —— 查看密钥只在那条没发出去的评论里，
            谁都用不了，留着只是一把没人知道的长期 AK
          · **管理员点过作废、但云那边没删成** —— 认得出来是因为密文已经清了
            （`sealed` 空）而 `cred_user` 还在。正常的已完成凭证单一定有密文。
            不认这一条的话，`revoke_now` 那句「云那边失败还能靠定时任务重试」就是空话：
            凭证没到期，按 expires_at_ts 永远扫不到。
        """
        status = ticket.get("status")
        if ticket.get("kind") == catalog_mod.KIND_CREDENTIAL and ticket.get("cred_user"):
            if status in (t.FAILED, t.CLOSED):
                return True
            if status == t.DONE and not (ticket.get("sealed") or {}).get("ciphertext"):
                return True
        if status != t.DONE:
            return False
        return bool(ticket.get("expires_at_ts")) and float(ticket["expires_at_ts"]) <= now

    def _revoke_failed(self, ticket: dict, note: str) -> str:
        """回收失败：同样的失败只记一次，不让每轮定时任务都往事件表里追加一条。"""
        last = (ticket.get("events") or [{}])[-1]
        if not (last.get("event") == "revoke_failed" and last.get("note") == note):
            with contextlib.suppress(t.TicketError):
                self.store.update(
                    ticket["id"],
                    actor="system",
                    expect=_RECLAIMABLE,
                    event="revoke_failed",
                    note=note,
                )
        return f"{ticket['id']}：回收失败，下次重试"

    def _mark_revoked(self, ticket: dict, note: str) -> str:
        try:
            revoked = self.store.update(
                ticket["id"],
                actor="system",
                expect=_RECLAIMABLE,
                to=t.REVOKED,
                event="revoked",
                note=note,
            )
        except t.TicketError:
            return f"{ticket['id']}：已由其他进程处理"
        self._emit("revoked", revoked)
        return f"{ticket['id']}：{note}"

    def _revoke_credential(self, ticket: dict, *, why: str = "到期") -> str:
        """凭证清理：到期，或管理员手动作废。

        **真正的到期控制是策略里的时间窗**：服务端每次调用按当前时间判，过期即拒，
        不依赖这个定时任务准时跑。这里只是删残留——子账号、密钥、策略。
        短期凭证（STS）本来就到点自灭，没有残留可删。

        贯穿这个方法的一条不变式：**没送达的单子永不进终态**。失败 / 已关闭的单子
        管理员还要能点重试，而 REVOKED 没有出边，进去就只能重走一轮审批。
        """
        tpl = ticket["template"]
        done = ticket.get("status") == t.DONE
        user = str(ticket.get("cred_user") or "")
        if not user:
            # 云上没有残留：STS 到点自灭，或者根本没建出子账号就失败了。
            # **只有已完成的单子才推终态** —— 失败单推过去的话，一张从没发出过凭证的单子
            # 会在台账上写成「已到期回收」，还给申请人推一张「已到期收回」的卡，
            # 而重试按钮（要求 status == FAILED）就此消失
            if not done:
                return f"{ticket['id']}：云上没有要清理的东西"
            if _imported(ticket):
                # 别人发的凭证，面板只是台账。写「已到期自动失效」是说谎：面板什么都没做，
                # 而这类凭证（bot 的方案 B）也不会自己失效，是靠 bot 的定时任务硬删的
                return self._mark_revoked(
                    ticket, "记录到期。这份凭证不是面板发的，清理由发放方负责，请去那边确认"
                )
            # 这支不跟 why 拼：拼出来是「临时凭证已管理员作废失效」，不是人话
            note = (
                "临时凭证已到期自动失效"
                if why == "到期"
                else "管理员作废：临时凭证本就到点自灭，云上没有残留"
            )
            return self._mark_revoked(ticket, note)
        # 删之前复核一次状态。revoke_expired 先 store.all() 拿快照、再逐张删，
        # 而失败单现在保持可重试 —— 管理员在这个窗口里点了重试的话，刚发出去的新 AK
        # 会被这一轮 sweep 删掉，而后面那次 update 又因状态已变被静静吞掉
        try:
            if self.store.get(ticket["id"]).get("status") != ticket.get("status"):
                return f"{ticket['id']}：状态已变，本轮不清理"
        except t.TicketError:
            return f"{ticket['id']}：单子已不在"
        try:
            issuer = self._issuer_for(tpl["platform"], tpl["account"])
            left = issuer.revoke_long_term(user)
        except Exception as exc:  # noqa: BLE001 — 失败记一次事件，下次定时任务再试
            return self._revoke_failed(ticket, describe_error(exc) or type(exc).__name__)
        if left:
            return self._revoke_failed(ticket, f"{user} 没清干净：{'；'.join(left)}")
        if not done:
            # **不推到终态**（同上那条不变式）。清掉 cred_user 有两个作用：
            # 下一轮 sweep 不再空转，重试时会重新起名建号。
            # 另外申请人从没拿到过这份凭证，给他推一条「已到期回收」也没道理。
            #
            # 密文也一起清掉：子账号刚被这轮删了，那团密文指向一个云上已经不存在的凭证，
            # 留着只会让「作废凭证」按钮永远亮着、点一次空转一次。
            #
            # 清它不损失任何还能用的东西，依据是 **view_credential 要求 status == DONE**：
            # 失败 / 已关闭的单子，那条链接本来就一律打不开，密文早就是不可达数据。
            # （别把依据写成「贴评论是最后一步所以没送达」—— 那句有反例：评论发出去了、
            # 紧接着写 DONE 没写成，单子会被 recover_stuck 标成 FAILED，密文其实送达过。）
            with contextlib.suppress(t.TicketError):
                self.store.update(
                    ticket["id"],
                    actor="system",
                    expect=(t.FAILED, t.CLOSED),
                    event="credential_reclaimed",
                    note=f"已删除没送达的凭证子账号 {user} 及其密钥和策略",
                    fields={"cred_user": "", "cred_ak_id": "", "sealed": {}},
                )
            return f"{ticket['id']}：已删除没送达的凭证子账号 {user}"
        return self._mark_revoked(ticket, f"{why}：已删除子账号 {user} 及其密钥和策略")

    def _revoke(self, ticket: dict, now: float) -> str:
        tpl = ticket["template"]
        user = ticket["payload"]["cloud_user"]
        keep = set(ticket.get("preexisting_groups") or [])
        keep_policies = set(ticket.get("preexisting_policies") or [])
        # 移出前重新读一次：批量开始后才开通的续期单也要算进来，避免把刚续上的组移掉
        for other in self.store.all():
            if other.get("id") == ticket["id"] or not _same_grantee(other, tpl, user):
                continue
            status = other.get("status")
            # 开通中的单子要等它记下开通前基线才算：没记基线就中断的单子，重试时会把本单还在的组 /
            # 策略当成「原有」，这里再替它保留就会变成永久权限
            recorded = "preexisting_groups" in other or "preexisting_policies" in other
            active = (status == t.EXECUTING and recorded) or (
                status == t.DONE
                and (not other.get("expires_at_ts") or float(other["expires_at_ts"]) > now)
            )
            if active:
                keep.update(other["template"].get("groups") or [])
                keep_policies.update(_policy_keys(other))
        remove = [g for g in tpl.get("groups") or [] if g not in keep]
        detach = [k for k in _policy_keys(ticket) if k not in keep_policies]
        try:
            ex = self._executor(tpl["platform"], tpl["account"])
            for group in remove:
                ex.remove_from_group(user, group)
            for key in detach:
                ptype, name = key.split(":", 1)
                ex.detach_policy(user, ptype, name)
        except Exception as exc:  # noqa: BLE001 — 失败记事件，下次定时任务再试
            note = describe_error(exc) or type(exc).__name__
            last = (ticket.get("events") or [{}])[-1]
            # 同样的失败只记一次，不让每次定时任务都往事件表里追加
            if not (last.get("event") == "revoke_failed" and last.get("note") == note):
                self.store.update(
                    ticket["id"], actor="system", expect=[t.DONE], event="revoke_failed", note=note
                )
            return f"{ticket['id']}：回收失败，下次重试"
        parts = []
        if tpl.get("groups"):
            kept = [g for g in tpl["groups"] if g in keep]
            parts.append(
                f"已移出 {'、'.join(remove) or '无'}"
                + (f"；保留 {'、'.join(kept)}" if kept else "")
            )
        if _policy_keys(ticket):
            kept = [k.split(":", 1)[1] for k in _policy_keys(ticket) if k in keep_policies]
            gone = [k.split(":", 1)[1] for k in detach]
            parts.append(
                f"已撤销 {'、'.join(gone) or '无'}" + (f"；保留 {'、'.join(kept)}" if kept else "")
            )
        note = "；".join(parts) or "无需回收"
        try:
            revoked = self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.DONE],
                to=t.REVOKED,
                event="revoked",
                note=note,
            )
        except t.TicketError:
            return f"{ticket['id']}：已由其他进程处理"
        self._emit("revoked", revoked)
        return f"{ticket['id']}：{note}"

    def _run(self, tpl: catalog_mod.Template, ticket: dict) -> str:
        payload = ticket["payload"]
        if tpl.kind == catalog_mod.KIND_RESOURCE:
            # 面板一行云都不写：只把批准的内容落成台账，等管理员按 IaC 建好回来登记
            return (
                f"审批通过：{payload['spec']}"
                + (f"（{payload['detail']}）" if payload.get("detail") else "")
                + "。等待管理员按 IaC 流程创建后回填实例信息"
            )
        if tpl.kind == catalog_mod.KIND_CREDENTIAL:
            # 只发取件地址，不碰云 —— 所以这里刻意不构造执行器
            return self._offer_credential(tpl, ticket)
        ex = self._executor(tpl.platform, tpl.account)
        if tpl.kind == catalog_mod.KIND_PERMISSION:
            user = payload["cloud_user"]
            keys = _policy_keys(ticket)
            done = []
            if tpl.groups:
                # 只在第一次开通时记：重试时组可能是上一次部分成功加进去的，不能算「原有」
                if "preexisting_groups" not in ticket:
                    existing = self._preexisting(ex, tpl, ticket, user)
                    self.store.update(
                        ticket["id"],
                        actor="system",
                        expect=[t.EXECUTING],
                        event="groups_checked",
                        note=f"开通前已在：{'、'.join(existing) or '无'}",
                        fields={"preexisting_groups": existing},
                    )
                for group in tpl.groups:
                    ex.add_to_group(user, group)
                done.append(f"已把 {user} 加入 {'、'.join(tpl.groups)}")
            if keys:
                # 同上：只在第一次记「开通前已直接授予」的策略
                if "preexisting_policies" not in ticket:
                    existing = self._preexisting_policies(ex, tpl, ticket, user, keys)
                    self.store.update(
                        ticket["id"],
                        actor="system",
                        expect=[t.EXECUTING],
                        event="policies_checked",
                        note="开通前已授予："
                        + ("、".join(k.split(":", 1)[1] for k in existing) or "无"),
                        fields={"preexisting_policies": existing},
                    )
                for key in keys:
                    ptype, name = key.split(":", 1)
                    ex.attach_policy(user, ptype, name)
                done.append(f"已给 {user} 授予 {'、'.join(k.split(':', 1)[1] for k in keys)}")
            return "；".join(done)
        username = payload["username"]
        # 重试时不再建号：上一次已经建好（由这张单子建的），直接补后面的步骤
        if not ticket.get("user_created"):
            ex.create_user(username, ticket["applicant"].get("name") or username)
            self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.EXECUTING],
                event="user_created",
                note=f"已新建子账号 {username}",
                fields={"user_created": True},
            )
        for group in tpl.groups:
            ex.add_to_group(username, group)
        result = f"已新建子账号 {username}" + (
            f"，加入 {'、'.join(tpl.groups)}" if tpl.groups else ""
        )
        return result + self._link_account(tpl, ticket, username)

    def _offer_credential(self, tpl: catalog_mod.Template, ticket: dict) -> str:
        """审批通过就签发，把凭证**密封**起来存，评论里给一个带密钥的查看地址。

        为什么不把凭证直接写进评论
        ──────────────────────────
        写进去它就永远躺在飞书的审批记录里：搜索、导出、离职交接都能翻到，撤不回，
        而且谁看过一次都不知道。换成查看地址之后：

          · 申请单里只有密文，**密钥只在链接里**，服务端自己也解不开
          · 每次打开都记一笔（谁、什么时候、从哪），台账上看得见
          · 使用方换台机器、重装环境、同事接手，都能再打开看 —— 不是只显示一次

        代价是链接丢了就真的取不回来（我们没有主密钥），只能重新申请。
        """
        approval = self._approval()
        if approval is None:
            raise FlowError("没有配置飞书审批，查看地址发不出去", 503)
        code = str((ticket.get("approval") or {}).get("instance_code") or "")
        if not code:
            raise FlowError("这张申请单没有飞书审批实例，查看地址无处下发", 409)
        # 送不出去的东西就别建。下面两条都在**签发之前**查：查晚了的代价是云上已经建好了
        # 子账号和长期 AK，要靠失败清理去收，申请人白等一轮审批
        view_base()
        sealed.selfcheck()

        cred = self._sign_credential(tpl, ticket)
        # cloud_user 这个字段是给交付失败时的清理用的，不单独列进密文。
        # **但子账号名本来就写在 summary 里**，所以它照样会出现在密文和取件响应里 ——
        # 这不是泄漏：使用方手上的 AK 就属于这个子账号，名字对他不是秘密。
        shown = {k: v for k, v in cred.items() if k != "cloud_user"}
        try:
            box = sealed.seal(json.dumps(shown, ensure_ascii=False))
            self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.EXECUTING],
                event="credential_sealed",
                note="凭证已加密存放，密钥只在查看地址里",
                fields={"sealed": box.stored()},
            )
            approval.comment(code, _view_comment(ticket=ticket, tpl=tpl, key=box.key, cred=shown))
        except Exception as exc:
            # 凭证已经在云上了却没能交付出去：作废它，否则就是一把**没人知道的**密钥 ——
            # 密钥只在那条没发出去的评论里，服务端自己解不开密文。
            #
            # 用户名和 AK 都从**签发结果**里取。回头读 ticket 是错的：那两个字段是
            # _sign_credential 通过 store.update 写进存储那份的，store.update 不回写
            # 调用方手里的 dict，读出来恒为空 —— 整段清理会变成死代码。
            user = str(cred.get("cloud_user") or "")
            issued = str(cred.get("access_key_id") or "")
            if user:
                try:
                    issuer = self._issuer_for(tpl.platform, tpl.account)
                except Exception as build:  # noqa: BLE001 — 造不出发放身份就清不掉，如实记成孤儿
                    self._record_cleanup(
                        ticket,
                        user,
                        exc,
                        left=[],
                        failed=describe_error(build) or type(build).__name__,
                        ak=issued,
                    )
                else:
                    self._cleanup_issued(ticket, issuer, user, exc, ak=issued)
            raise
        return cred["summary"] + "，查看地址已发到审批评论"

    def view_credential(self, ticket_id: str, key: str, *, who: str = "") -> tuple:
        """用链接里的密钥解开凭证。返回 (申请单, 凭证 dict)。

        **每次都记一笔**：谁、什么时候看的。这是把「凭证躺在飞书评论里、谁看过都不知道」
        换成查看地址的主要收益之一，不记就白换了。

        解不开的原因（密钥不对、密文被改、单子不在可查看状态）对外都是同一句话：
        逐一区分等于告诉试密钥的人「这个 id 是对的，接着试」。
        """
        # 打不开的原因（单号不存在、不是凭证单、密钥不对、密文被改）**对外都是同一句**。
        # 逐一区分等于告诉试密钥的人「这个单号是对的，接着试」
        try:
            ticket = self.store.get(ticket_id)
        except t.TicketError:
            raise FlowError("这个链接打不开任何凭证", 404) from None
        box = ticket.get("sealed") or {}
        if ticket.get("kind") != catalog_mod.KIND_CREDENTIAL or not box.get("ciphertext"):
            raise FlowError("这个链接打不开任何凭证", 404)
        if ticket.get("status") != t.DONE:
            raise FlowError("这份凭证已经失效", 409)
        try:
            payload = sealed.unseal(str(key or ""), box.get("nonce", ""), box["ciphertext"])
            cred = json.loads(payload)
        except (sealed.SealError, ValueError):
            raise FlowError("这个链接打不开这份凭证", 403) from None
        with contextlib.suppress(t.TicketError):
            self.store.update(
                ticket_id,
                actor="view",
                expect=[t.DONE],
                event="credential_viewed",
                note=f"查看凭证（{who or '未知来源'}）",
            )
        return ticket, cred

    def _sign_credential(self, tpl: catalog_mod.Template, ticket: dict) -> dict:
        """真正去云上签发。长短期不是两种申请，是同一份权限的两种实现：

          ≤12 小时  STS 临时凭证 + 会话策略收窄（到点自灭，云上没有残留）
          >12 小时  建子账号 + 时间窗策略 + 长期 AK（服务端逐次判时间，到期自动失效）

        12 是阿里云 AssumeRole 的硬顶，不是我们的选择。没配角色的平台（火山）一律走
        长期路径，哪怕只申请一小时 —— 那边的凭证同样有时间窗策略兜着。
        """
        payload = ticket["payload"]
        hours = int(payload["hours"])
        bucket = str(payload["bucket"])
        prefix = str(payload.get("prefix") or "")
        subject = str(payload["subject"])
        region = dict(tpl.buckets).get(bucket, "")
        if not region:
            raise FlowError("模板里已经没有这个桶了，请重新提交申请", 409)
        now = self._clock()
        expire = now + hours * 3600
        doc = grants_mod.build_policy(
            tpl.platform, bucket, prefix=prefix, caps=tpl.caps, not_before=now, expire=expire
        )
        cloud = platforms_mod.get(tpl.platform)
        out = {
            "platform": tpl.platform,
            "account": tpl.account,
            "subject": subject,
            "caps": [catalog_mod.CAP_LABELS[c] for c in tpl.caps],
            "scope": f"{cloud.storage_scheme}://{bucket}/{prefix}",
            "region": region,
            "endpoint": cloud.endpoint.format(region=region),
            "bucket_url": cloud.bucket_url(bucket, region),
            "not_before": now,
            "expire": expire,
            "hours": hours,
        }
        # 只有 STS 这条路要开通身份（扮演模板角色的权限在它那边）。放在分支外面构造的话，
        # 火山的凭证模板会被迫要求 DELIVERY_EXEC_VOLCANO_* 存在 —— 而火山根本没有 STS，
        # 它的凭证模板连 role_arn 都不允许配，这条路它永远走不到
        if hours <= catalog_mod.STS_MAX_HOURS and tpl.role_arn:
            ex = self._executor(tpl.platform, tpl.account)
            cred = ex.assume_role(tpl.role_arn, subject, hours, policy=doc)
            with contextlib.suppress(t.TicketError):
                self.store.update(
                    ticket["id"],
                    actor="system",
                    expect=[t.EXECUTING],
                    event="credential_issued",
                    note=f"临时凭证 AK …{cred.access_key_id[-4:]}",
                    fields={"cred_ak_id": cred.access_key_id},
                )
            out.update(
                access_key_id=cred.access_key_id,
                access_key_secret=cred.secret,
                security_token=cred.token,
                long_term=False,
                summary=(
                    f"已发放 {_duration(hours)} 临时凭证（AK …{cred.access_key_id[-4:]}），"
                    f"到点自动失效"
                ),
            )
            return out

        # 先把要建的子账号名记进单子再动云：进程在建号途中挂掉时，云上留下的东西
        # 还能按这个名字找回来清掉；不记的话就是一个查无此人的残留账号
        issuer = self._issuer_for(tpl.platform, tpl.account)
        user = str(ticket.get("cred_user") or "") or grants_mod.user_name(subject)
        if ticket.get("cred_user") != user:
            self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.EXECUTING],
                event="cred_user_reserved",
                note=f"准备建长期凭证子账号 {user}",
                fields={"cred_user": user},
            )
        issued_ak = ""
        try:
            cred = issuer.issue_long_term(user, f"{subject}-面板数据访问", doc)
            issued_ak = cred.access_key_id
            self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.EXECUTING],
                event="credential_issued",
                note=f"已建 {user}，AK …{cred.access_key_id[-4:]}",
                fields={"cred_ak_id": cred.access_key_id},
            )
        except Exception as exc:
            # 建到一半失败：把已经建出来的部分清掉，否则重试必撞「子账号已存在」
            self._cleanup_issued(ticket, issuer, user, exc, ak=issued_ak)
            raise
        out.update(
            access_key_id=cred.access_key_id,
            access_key_secret=cred.access_key_secret,
            security_token="",
            long_term=True,
            cloud_user=user,
            summary=(
                f"已发放 {_duration(hours)} 长期凭证（子账号 {user}，"
                f"AK …{cred.access_key_id[-4:]}），到期自动失效并清理"
            ),
        )
        return out

    def _cleanup_issued(
        self, ticket: dict, issuer, user: str, cause: Exception, *, ak: str = ""
    ) -> None:
        """长期凭证发出来了但没能送达：删掉它，并**如实记录删没删干净**。

        以前这里是 `contextlib.suppress(Exception)` 吞掉一切、然后无条件记「已作废并清理」。
        那有三重后果，一重比一重糟：台账说谎；`cred_ak_id` 被清空、残留 AK 的编号从此查不到；
        而失败单的状态是 FAILED，`revoke_expired` 只扫 DONE —— 那把长期 AK 永远不会被
        任何定时任务清理，只剩策略里的时间窗兜底。
        """
        left, failed = [], ""
        try:
            left = issuer.revoke_long_term(user) or []
        except Exception as exc:  # noqa: BLE001 — 清理失败不能盖住原始错误
            failed = describe_error(exc) or type(exc).__name__
        self._record_cleanup(ticket, user, cause, left=left, failed=failed, ak=ak)

    def _record_cleanup(
        self, ticket: dict, user: str, cause: Exception, *, left: list, failed: str, ak: str
    ) -> None:
        """把清理结果如实写进单子。和上面分开，是因为「连发放身份都造不出来」这条路
        根本没机会调 revoke_long_term，但它同样是一把孤儿 AK，台账上必须看得见。"""
        if failed or left:
            note = f"凭证没能送达，作废 {user} 时没清干净：{failed or '；'.join(left)}"
            # 残留的 AK 单独记一份。只留 cred_ak_id 是不够的：管理员点一次重试、这回成功了，
            # credential_issued 会用新 AK 覆盖同一个字段，上一把残留的编号就只剩在事件里了
            orphans = [x for x in (ticket.get("orphan_ak_ids") or []) if isinstance(x, str)]
            fields = {"orphan_ak_ids": [*orphans, ak]} if ak and ak not in orphans else None
        else:
            note = f"凭证没能送达，已作废并清理 {user}"
            # 只有真发出过 AK 才需要清这个字段。建号中途就失败时根本没有 AK，
            # 往单子里写一个空值只会让「有没有发出过凭证」这件事更难看清
            fields = {"cred_ak_id": ""} if ak else None
        with contextlib.suppress(t.TicketError):
            self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.EXECUTING],
                event="credential_orphaned" if (failed or left) else "credential_revoked",
                note=f"{note}（原因：{describe_error(cause) or type(cause).__name__}）"[:500],
                fields=fields,
            )

    def _preexisting(self, ex, tpl: catalog_mod.Template, ticket: dict, user: str) -> list:
        """开通前子账号本来就在、到期不该移出的组。

        组如果是别的申请单加进去的（那张单子还没回收，或正在开通、开通失败），就不算原有：
        否则到期前续期，续期单会把上一张单子加的组当成「原有」，到期后永远不会移出。
        """
        snap = {"platform": tpl.platform, "account": tpl.account}
        others = [
            x
            for x in self.store.all()
            if x.get("id") != ticket["id"]
            and _same_grantee(x, snap, user)
            # 关闭的失败单也算：它可能已经加了组，那个组不能被下一张单子当成「原有」
            and x.get("status") in (t.DONE, t.EXECUTING, t.FAILED, t.CLOSED)
            and "preexisting_groups" in x
        ]
        out = []
        for group in tpl.groups:
            if not ex.in_group(user, group):
                continue
            granters = [x for x in others if group in (x["template"].get("groups") or [])]
            if all(group in (x.get("preexisting_groups") or []) for x in granters):
                out.append(group)
        return out

    def _preexisting_policies(
        self, ex, tpl: catalog_mod.Template, ticket: dict, user: str, keys: list
    ) -> list:
        """开通前子账号就已直接授予、到期不该撤销的策略。和 _preexisting 同一个道理：
        别的申请单授予的（没回收、正在开通、开通失败、失败后关闭），不算原有。"""
        snap = {"platform": tpl.platform, "account": tpl.account}
        others = [
            x
            for x in self.store.all()
            if x.get("id") != ticket["id"]
            and _same_grantee(x, snap, user)
            and x.get("status") in (t.DONE, t.EXECUTING, t.FAILED, t.CLOSED)
            and "preexisting_policies" in x
        ]
        out = []
        for key in keys:
            ptype, name = key.split(":", 1)
            if not ex.has_policy(user, ptype, name):
                continue
            granters = [x for x in others if key in _policy_keys(x)]
            if all(key in (x.get("preexisting_policies") or []) for x in granters):
                out.append(key)
        return out

    def _applicant_email(self, ticket: dict) -> str:
        """名册人工对应用的企业邮箱：优先按 union_id 从名册取，名册里没有才用提交时记的企业邮箱。"""
        try:
            person = self._roster().resolve(union_id=ticket["applicant"]["union_id"]).person
        except DeliveryError:
            person = None
        if person is not None and person.email:
            return person.email
        return str(ticket["applicant"].get("email") or "")

    def _link_account(self, tpl: catalog_mod.Template, ticket: dict, username: str) -> str:
        """新账号对应给申请人。对应不上不算开通失败（账号已经建好），记事件交给管理员。"""
        if ticket.get("linked"):
            return ""
        account = f"{tpl.platform}/{tpl.account}/{username}"
        email = self._applicant_email(ticket)
        problem = ""
        if self._add_manual_link is None:
            problem = "没有配置名册人工记录"
        elif not email:
            problem = "名册里查不到申请人的企业邮箱"
        else:
            try:
                self._add_manual_link(email, account, ticket["id"])
            except (DeliveryError, OSError) as exc:
                problem = describe_error(exc) or type(exc).__name__
        if problem:
            self.store.update(
                ticket["id"],
                actor="system",
                expect=[t.EXECUTING],
                event="link_needed",
                note=f"新账号 {account} 没能自动对应给申请人（{problem}），请在名册审核里处理",
            )
            return "；还没对应到申请人名下，需管理员在名册审核里处理"
        self.store.update(
            ticket["id"],
            actor="system",
            expect=[t.EXECUTING],
            event="linked",
            note=f"新账号 {account} 已对应给申请人（名册下次刷新生效）",
            fields={"linked": True},
        )
        return ""

    def link_pending(self, ticket: dict) -> bool:
        """开账号单子建好了号，但没能自动对应到申请人，而且名册里到现在也还没对应上。

        管理员在「人员与名册」里确认之后名册会更新，这里随之变成 False；不依赖单子上补事件。
        """
        if ticket.get("kind") != catalog_mod.KIND_ACCOUNT or not ticket.get("user_created"):
            return False
        events = [e.get("event") for e in ticket.get("events") or []]
        if "link_needed" not in events or (
            "linked" in events and events[::-1].index("linked") < events[::-1].index("link_needed")
        ):
            return False
        tpl = ticket["template"]
        key = (tpl["platform"], tpl["account"], (ticket.get("payload") or {}).get("username"))
        try:
            people = self._roster().people
        except DeliveryError:
            return True  # 名册读不了：保持提醒
        return not any(
            (ref.platform, ref.account, ref.name) == key
            for person in people
            for ref in person.accounts
        )

    def _account_owner_ok(self, ticket: dict, username: str) -> bool:
        """名册里这个子账号要么还没对应给人，要么对应的就是申请人。"""
        tpl = ticket["template"]
        applicant = ticket["applicant"]
        for person in self._roster().people:
            for ref in person.accounts:
                if (ref.platform, ref.account, ref.name) != (
                    tpl["platform"],
                    tpl["account"],
                    username,
                ):
                    continue
                if person.union_id:
                    return person.union_id == applicant["union_id"]
                email = str(applicant.get("email") or "").lower()
                return bool(email) and person.email.lower() == email
        return True

    # ── 员工操作 ──────────────────────────────────────────────────────────
    def _own(self, ticket_id: str, union_id: str) -> dict:
        if not union_id:
            raise FlowError("没有这张申请单", 404)
        ticket = self.store.get(ticket_id)
        if ticket["applicant"].get("union_id") != union_id:
            raise FlowError("没有这张申请单", 404)
        return ticket

    def withdraw(self, ticket_id: str, *, union_id: str) -> dict:
        ticket = self._own(ticket_id, union_id)
        if ticket.get("status") != t.PENDING:
            raise FlowError("只有待审批的申请可以撤回", 409)
        approval = self._approval()
        if approval is not None:
            approval.cancel(ticket["approval"]["instance_code"], _applicant(ticket))
        return self.store.update(
            ticket_id,
            actor=union_id,
            expect=[t.PENDING],
            to=t.WITHDRAWN,
            event="withdrawn",
            note="申请人撤回",
        )

    def claim_password(self, ticket_id: str, *, union_id: str):
        """开账号申请：领取一次性初始密码（强制首次登录修改）。返回 (申请单, 密码)。

        只在开通后 _PASSWORD_WINDOW_DAYS 天内、子账号确实由这张单子新建、名册里没有对应给别人时
        才能领：否则一张很久以前没领的单子，可以把后来同名的别人的账号密码重置掉。
        """
        ticket = self._own(ticket_id, union_id)
        if ticket["kind"] != catalog_mod.KIND_ACCOUNT or ticket.get("status") != t.DONE:
            raise FlowError("这张申请单没有可领取的初始密码", 409)
        if not ticket["template"].get("console_login"):
            raise FlowError("这个模板没有开通控制台登录", 409)
        if password_claims(ticket) > 0:
            raise FlowError("初始密码已经领取过。忘记密码请联系管理员重置", 409)
        if not ticket.get("user_created"):
            raise FlowError(
                "这个子账号不是由这张申请单新建的，不能在这里领取密码，请联系管理员", 409
            )
        done_at = float(ticket.get("done_at_ts") or 0)
        if not done_at or self._clock() - done_at > _PASSWORD_WINDOW_DAYS * 86400:
            raise FlowError(
                f"初始密码只能在开通后 {_PASSWORD_WINDOW_DAYS} 天内领取，已超过。请联系管理员重置",
                409,
            )
        username = ticket["payload"]["username"]
        tpl_key = (ticket["template"]["platform"], ticket["template"]["account"])
        for other in self.store.all():
            if (
                other.get("id") != ticket["id"]
                and other.get("kind") == catalog_mod.KIND_ACCOUNT
                and other.get("user_created")
                and (other["template"]["platform"], other["template"]["account"]) == tpl_key
                and (other.get("payload") or {}).get("username") == username
            ):
                raise FlowError(
                    "这个用户名也被别的申请单新建过，不能在这里领取密码，请联系管理员", 409
                )
        if not self._account_owner_ok(ticket, username):
            raise FlowError("这个子账号在名册里已经对应给别人，不能领取密码，请联系管理员", 409)
        self._verify_approval(ticket)
        platform, account = ticket["template"]["platform"], ticket["template"]["account"]
        # 先记事件再签发：并发领取时两边都会看到计数 > 1，一起作废，稍后重试
        ticket = self.store.update(
            ticket_id, actor=union_id, expect=[t.DONE], event="password_issued", note="领取初始密码"
        )
        if password_claims(ticket) > 1:
            self.store.update(
                ticket_id,
                actor="system",
                expect=[t.DONE],
                event="password_failed",
                note="同时有多次领取，本次作废",
            )
            raise FlowError("正在领取初始密码，请稍后刷新重试", 409)
        try:
            password = self._executor(platform, account).reset_password(username)
        except Exception as exc:  # noqa: BLE001 — 失败撤销这次领取记录，允许重试
            note = describe_error(exc) or type(exc).__name__
            self.store.update(
                ticket_id, actor="system", expect=[t.DONE], event="password_failed", note=note
            )
            raise ProvisionError(f"生成初始密码失败：{note}。可以稍后重试") from None
        return ticket, password

    def close(self, ticket_id: str, *, actor: str, note: str) -> dict:
        return self.store.update(
            ticket_id,
            actor=actor,
            expect=[t.FAILED, t.FULFILLING],
            to=t.CLOSED,
            event="closed",
            note=note or "管理员关闭",
        )
