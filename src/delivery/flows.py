"""申请流程：提交 → 发起飞书审批 → 同步审批状态 → 开通 → 领取。

面板和 CLI 都只调这里，规则只写一份。每个会写云的入口（execute、claim）都先
`approval.verify_approved()`：实时查飞书，状态、审批定义、发起人、申请单号都对才放行。

模板在提交时做快照存进申请单；开通前要求当前模板和快照一致——审批人批的是提交时的内容，
管理员在审批期间改了模板（比如多加一个用户组），这张单子就不能按新内容开通。
"""

from __future__ import annotations

import contextlib
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from typing import Callable, Optional

from . import catalog as catalog_mod
from . import policies as policies_mod
from . import tickets as t
from .approval import STATUS_APPROVED, STATUS_PENDING, Applicant, FeishuApproval, instance_links
from .errors import DeliveryError
from .provision import ProvisionError, describe_error

_REASON_MIN = 5
_REASON_MAX = 500
_SYNC_INTERVAL = 10
#: 授权天数的硬上限：模板 max_days=0（不限）时也不能填出溢出时间戳的天数
_MAX_DAYS = 3650
#: 「开通中 / 提交中」超过这么久没进展就当作中断
_STUCK_AFTER = 30 * 60
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
    "valid_days",
    "max_days",
    "username_pattern",
    "console_login",
)
#: 领取凭证前核对：扮演哪个角色（时长另按当前模板上限截断）
_CLAIM_FIELDS = ("kind", "platform", "account", "role_arn")
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
    # 分类只是申请页的展示，改分类不应让审批中的单子因「模板变了」而不能开通
    data.pop("category", None)
    data["groups"] = list(data["groups"])
    return data


def _effective(template: dict, fields: tuple) -> dict:
    return {k: template.get(k) for k in fields}


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
        add_manual_link: Optional[Callable[[str, str, str], None]] = None,
        current_groups: Optional[Callable[[str, str, str], Optional[set]]] = None,
        policy_snapshot: Optional[Callable[[], Optional[dict]]] = None,
        policy_rules: Optional[Callable[[], policies_mod.Rules]] = None,
        current_policies: Optional[policies_mod.CurrentPolicies] = None,
        notify: Optional[Callable[[str, dict], None]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self._catalog = catalog
        self._approval = approval
        self._roster = roster
        self._executor = executor
        self._add_manual_link = add_manual_link
        self._current_groups = current_groups
        self._policy_snapshot = policy_snapshot or (lambda: None)
        self._policy_rules = policy_rules or policies_mod.Rules
        self._current_policies = current_policies
        self._notify = notify
        self._clock = clock
        self._last_sync: dict = {}

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
          ready        凭证已批准、可领取（request_id 指向它）
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
            elif not name:
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
                if open_.get("status") == t.CLAIMABLE:
                    state, note = "ready", "已批准，可以领取"
                else:
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
            "rules": {
                "deny": list(rules.deny),
                "deny_families": list(policies_mod.DEFAULT_DENY_FAMILIES),
                "allow": list(rules.allow),
                "allow_custom": rules.allow_custom,
                "max_days": dict(rules.max_days),
                "max_per_request": rules.max_per_request,
            },
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
            if not mine:
                raise FlowError("你在这个云账号下还没有子账号，不能申请它的访问凭证")
            hours = payload.get("hours", 1)
            if (
                not isinstance(hours, int)
                or isinstance(hours, bool)
                or not 1 <= hours <= tpl.max_hours
            ):
                raise FlowError(f"单次有效时长必须在 1–{tpl.max_hours} 小时之间")
            return (
                {"hours": hours},
                f"{where}：{tpl.valid_days} 天内可领取 {tpl.platform}/{tpl.account} 的临时凭证，"
                f"每次最长 {hours} 小时",
            )
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

    # ── 同步审批 ──────────────────────────────────────────────────────────
    def sync(self, ticket_id: str, *, force: bool = False) -> dict:
        ticket = self.store.get(ticket_id)
        if ticket.get("status") == t.CLAIMABLE:
            return self._maybe_expire(ticket)
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
        if ticket["kind"] == catalog_mod.KIND_CREDENTIAL:
            valid_until = now + ticket["template"]["valid_days"] * 86400
            ticket = self.store.update(
                ticket_id,
                actor="system",
                expect=[t.APPROVED],
                to=t.CLAIMABLE,
                event="claimable",
                fields={
                    "valid_until": t.now_iso(lambda: valid_until),
                    "valid_until_ts": valid_until,
                },
            )
            return self._emit("claimable", ticket)
        return self.execute(ticket_id, actor="system")

    def _maybe_expire(self, ticket: dict) -> dict:
        if self._clock() < float(ticket.get("valid_until_ts") or 0):
            return ticket
        return self.store.update(
            ticket["id"], actor="system", expect=[t.CLAIMABLE], to=t.EXPIRED, event="expired"
        )

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
        if ticket["kind"] == catalog_mod.KIND_CREDENTIAL:
            raise FlowError("访问凭证申请不需要开通，审批通过后直接领取")
        if ticket.get("status") not in (t.APPROVED, t.FAILED):
            status = ticket.get("status")
            raise FlowError(f"申请单当前是「{t.LABELS.get(status, status)}」，不能开通", 409)
        try:
            tpl = self._verify(ticket)
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
            days = (
                ticket["payload"].get("days")
                if ticket["kind"] == catalog_mod.KIND_PERMISSION
                else 0
            )
            if days:
                expires = now + days * 86400
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
        done = self.store.update(
            ticket_id,
            actor=actor,
            expect=[t.EXECUTING],
            to=t.DONE,
            event="execute_done",
            note=result,
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

        权限 / 开账号走 execute()，里面会重新实时核对飞书审批；凭证核对审批后转为可领取，
        领取期从审批通过时算起。
        """
        now = self._clock()
        out = []
        for ticket in self.store.all():
            if ticket.get("status") != t.APPROVED or now - _ts(ticket.get("updated_at")) < min_age:
                continue
            try:
                if ticket.get("kind") == catalog_mod.KIND_CREDENTIAL:
                    self._verify_approval(ticket)
                    valid_until = _ts(ticket.get("updated_at")) + (
                        ticket["template"]["valid_days"] * 86400
                    )
                    after = self._emit(
                        "claimable",
                        self.store.update(
                            ticket["id"],
                            actor="system",
                            expect=[t.APPROVED],
                            to=t.CLAIMABLE,
                            event="claimable",
                            fields={
                                "valid_until": t.now_iso(lambda v=valid_until: v),
                                "valid_until_ts": valid_until,
                            },
                        ),
                    )
                else:
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
                ticket.get("kind") != catalog_mod.KIND_PERMISSION
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
            if (
                ticket.get("kind") != catalog_mod.KIND_PERMISSION
                or ticket.get("status") != t.DONE
                or not ticket.get("expires_at_ts")
                or float(ticket["expires_at_ts"]) > now
            ):
                continue
            try:
                out.append(self._revoke(ticket, now))
            except Exception as exc:  # noqa: BLE001 — 一张单子出错不能挡住其他单子回收
                out.append(f"{ticket['id']}：回收失败（{describe_error(exc)}），下次重试")
        return out

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
        ex = self._executor(tpl.platform, tpl.account)
        payload = ticket["payload"]
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

    def claim_credential(self, ticket_id: str, *, union_id: str, hours: Optional[int] = None):
        """返回 (申请单, TempCredential)。凭证不写进申请单。"""
        ticket = self._maybe_expire(self._own(ticket_id, union_id))
        if ticket.get("status") != t.CLAIMABLE:
            raise FlowError("这张申请单现在不能领取凭证", 409)
        tpl = self._verify(ticket, fields=_CLAIM_FIELDS)
        limit = min(int(ticket["payload"]["hours"]), tpl.max_hours)
        want = hours or limit
        if not isinstance(want, int) or isinstance(want, bool) or not 1 <= want <= limit:
            raise FlowError(f"单次时长不能超过 {limit} 小时")
        name = ticket["applicant"].get("email") or ticket["applicant"]["union_id"]
        # 先记事件再签发：签发后单子即使马上被关闭或过期，审计里也有这次领取
        ticket = self.store.update(
            ticket_id,
            actor=union_id,
            expect=[t.CLAIMABLE],
            event="credential_issued",
            note=f"签发 {want} 小时临时凭证",
        )
        try:
            cred = self._executor(tpl.platform, tpl.account).assume_role(tpl.role_arn, name, want)
        except Exception as exc:  # noqa: BLE001 — 失败要留痕，细节只取脱敏后的第一行
            note = describe_error(exc) or type(exc).__name__
            with contextlib.suppress(t.TicketError):
                self.store.update(
                    ticket_id,
                    actor="system",
                    expect=[t.CLAIMABLE, t.EXPIRED, t.CLOSED],
                    event="credential_failed",
                    note=note,
                )
            raise ProvisionError(f"签发凭证失败：{note}") from None
        return ticket, cred

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
            expect=[t.FAILED, t.CLAIMABLE],
            to=t.CLOSED,
            event="closed",
            note=note or "管理员关闭",
        )
