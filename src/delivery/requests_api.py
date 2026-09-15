"""申请相关的 HTTP 接口。server.py 只做登录、角色、CSRF，然后把请求交给这里。

员工接口                                   管理员接口
  GET  /api/policies                        （权限列表：全部权限策略对本人的状态）
  GET  /api/requests/options                GET  /api/admin/requests?status=
  GET  /api/requests                        GET  /api/admin/requests/<id>
  POST /api/requests                        POST /api/admin/requests/<id>/retry
  GET  /api/requests/<id>                   POST /api/admin/requests/<id>/close
                                            POST /api/admin/requests/<id>/recover（卡住的单子）
  POST /api/requests/<id>/withdraw
  POST /api/requests/<id>/credential        （领取临时凭证，响应里直接给，不落盘）
  POST /api/requests/<id>/password          （领取一次性初始密码）

员工视角的申请单去掉模板里的角色 ARN、执行细节；事件里的操作人显示成「你 / 飞书 / 系统 / 管理员」，
不把管理员的 union_id 暴露给员工。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from typing import Callable, Optional

from . import tickets as t
from .approval import Applicant, ApprovalError
from .catalog import KIND_LABELS
from .errors import DeliveryError
from .flows import FlowError, Flows, password_claims

_ID = re.compile(r"^REQ-\d{8}-[0-9A-F]{8}$")
_EVENT_LABELS = {
    "created": "提交申请",
    "approval_created": "发起飞书审批",
    "submit_failed": "发起审批失败",
    "approval_approved": "审批通过",
    "approval_rejected": "审批拒绝",
    "approval_canceled": "审批已撤销",
    "approval_deleted": "审批已删除",
    "approval_reverted": "审批通过后被撤销",
    "approval_invalid": "审批未经他人同意，已关闭",
    "withdrawn": "撤回申请",
    "claimable": "可以领取",
    "execute_start": "开始开通",
    "execute_done": "开通完成",
    "execute_failed": "开通失败",
    "credential_issued": "领取临时凭证",
    "credential_failed": "签发凭证失败",
    "password_issued": "领取初始密码",
    "password_failed": "初始密码未生成",
    "user_created": "新建子账号",
    "linked": "对应到申请人",
    "link_needed": "待管理员对应到申请人",
    "expired": "已过期",
    "closed": "已关闭",
    "groups_checked": "核对原有用户组",
    "policies_checked": "核对原有策略",
    "revoked": "到期回收",
    "revoke_failed": "到期回收失败",
    "expiry_remind_failed": "到期提醒没有发出去",
    "expiry_reminded": "提醒即将到期",
}
#: 这些事件的 note 是云接口 / 飞书接口的原始错误，只给管理员看；员工看到的是下面的说明
_ERROR_NOTES = {
    "execute_failed": "管理员会查看原因并处理",
    "submit_failed": "请稍后重新提交，或联系管理员",
    "revoke_failed": "系统会自动重试",
    "credential_failed": "可以稍后再领取，仍失败请联系管理员",
    "password_failed": "可以稍后再领取，仍失败请联系管理员",
    "link_needed": "管理员会把新账号对应到你名下",
}


@dataclass(frozen=True)
class Caller:
    union_id: str
    name: str
    email: str
    open_id: str
    user_id: str
    admin: bool

    @property
    def applicant(self) -> Applicant:
        return Applicant(
            union_id=self.union_id, name=self.name, open_id=self.open_id, user_id=self.user_id
        )


def ticket_view(ticket: dict, *, viewer: Caller, links: Optional[dict] = None) -> dict:
    """links：飞书审批实例的跳转链接 {"pc", "mobile"}，只给申请人本人和管理员。"""
    tpl = ticket.get("template") or {}
    own = ticket.get("applicant", {}).get("union_id") == viewer.union_id

    def actor(value: str) -> str:
        if value == viewer.union_id:
            return "你"
        if value in ("feishu", "system"):
            return {"feishu": "飞书", "system": "系统"}[value]
        if value == ticket.get("applicant", {}).get("union_id"):
            return ticket["applicant"].get("name") or "申请人"
        return value if viewer.admin else "管理员"

    status = ticket.get("status", "")
    events = [
        {
            "at": e.get("at", ""),
            "actor": actor(str(e.get("actor", ""))),
            "event": e.get("event", ""),
            "label": _EVENT_LABELS.get(e.get("event", ""), e.get("event", "")),
            "note": (
                e.get("note", "")
                if viewer.admin or e.get("event") not in _ERROR_NOTES
                else _ERROR_NOTES[e.get("event")]
            ),
        }
        for e in ticket.get("events", [])
    ]
    claimed_password = password_claims(ticket) > 0
    view = {
        "id": ticket.get("id"),
        "kind": ticket.get("kind"),
        "kind_label": KIND_LABELS.get(ticket.get("kind", ""), ""),
        "status": status,
        "status_label": t.LABELS.get(status, status),
        "open": status in t.OPEN,
        "template": {
            "id": tpl.get("id"),
            "title": tpl.get("title"),
            "platform": tpl.get("platform"),
            "account": tpl.get("account"),
            "groups": tpl.get("groups", []),
            "policies": [
                {"type": p.get("type"), "name": p.get("name"), "risk": p.get("risk", "low")}
                for p in tpl.get("policies") or []
            ],
            "risk": tpl.get("risk", "low"),
        },
        "applicant": {
            "name": ticket.get("applicant", {}).get("name", ""),
            "email": ticket.get("applicant", {}).get("email", ""),
        },
        "payload": ticket.get("payload", {}),
        "summary": ticket.get("summary", ""),
        "reason": ticket.get("reason", ""),
        "created_at": ticket.get("created_at", ""),
        "updated_at": ticket.get("updated_at", ""),
        "valid_until": ticket.get("valid_until", ""),
        "expires_at": ticket.get("expires_at", ""),
        "result": ticket.get("result", ""),
        "events": events,
        "actions": {
            "withdraw": own and status == t.PENDING,
            "credential": own and status == t.CLAIMABLE,
            "password": own
            and status == t.DONE
            and ticket.get("kind") == "account"
            and bool(tpl.get("console_login"))
            and not claimed_password,
            "retry": viewer.admin and status == t.FAILED,
            "close": viewer.admin and status in (t.FAILED, t.CLAIMABLE),
            "recover": viewer.admin and status in (t.EXECUTING, t.SUBMITTING),
        },
    }
    links = links if (own or viewer.admin) and links else {}
    view["approval_url"] = links.get("pc", "")
    view["approval_url_mobile"] = links.get("mobile", "")
    if viewer.admin:
        view["applicant"]["union_id"] = ticket.get("applicant", {}).get("union_id", "")
        view["approval"] = ticket.get("approval", {})
    return view


def _quiet_sync(flows: Flows, ticket_id: str) -> Optional[dict]:
    """列表和详情里顺手同步审批状态：飞书暂时查不通、某张单子数据异常，都不能让页面打不开，下次再试。"""
    try:
        return flows.sync(ticket_id)
    except Exception as exc:  # noqa: BLE001 — 只影响这一张单子的顺手同步
        first = next((ln for ln in str(exc).splitlines() if ln.strip()), "")
        print(
            f"[requests] 同步 {ticket_id} 失败：{type(exc).__name__}: {first[:120]}",
            file=sys.stderr,
        )
        return None


class RequestsApi:
    def __init__(
        self,
        flows: Callable[[], Optional[Flows]],
        *,
        account_label: Optional[Callable[[str, str], str]] = None,
    ):
        self._flows = flows
        self._account_label = account_label

    def handle(
        self, method: str, path: str, query: dict, body: Optional[dict], caller: Caller
    ) -> tuple:
        """返回 (HTTP 状态码, JSON)。"""
        if not caller.union_id:
            # 空 union_id 会匹配到所有 union_id 为空的申请单：宁可拒绝
            return 403, {"error": "登录信息里没有 union_id，不能使用申请功能"}
        flows = self._flows()
        if flows is None:
            return 503, {"error": "申请功能还没有配置（缺申请单存储路径）"}
        try:
            return self._route(flows, method, path, query, body or {}, caller)
        except (FlowError, t.TicketError) as exc:
            return exc.status, {"error": str(exc)}
        except ApprovalError as exc:
            return 502, {"error": f"飞书审批：{exc}"}
        except DeliveryError as exc:
            first = next((ln for ln in str(exc).splitlines() if ln.strip()), "操作失败")
            return 502, {"error": first[:300]}

    @staticmethod
    def _view(flows: Flows, ticket: dict, caller: Caller) -> dict:
        view = ticket_view(ticket, viewer=caller, links=flows.approval_links(ticket))
        if caller.admin:
            view["link_pending"] = flows.link_pending(ticket)
        return view

    def _policies(self, flows: Flows, method: str, caller: Caller) -> tuple:
        if method != "GET":
            return 405, {"error": "不支持的方法"}
        data = flows.policy_options(caller.union_id)
        for acc in data["accounts"]:
            label = ""
            if self._account_label is not None:
                try:
                    label = self._account_label(acc["platform"], acc["account"])
                except Exception:  # noqa: BLE001 — 标签只是展示
                    label = ""
            acc["account_label"] = label or acc["account"]
        return 200, data

    def _route(self, flows: Flows, method, path, query, body, caller: Caller) -> tuple:
        if path.rstrip("/") == "/api/policies":
            return self._policies(flows, method, replace(caller, admin=False))
        if path.startswith("/api/policies"):
            return 404, {"error": "没有这个接口"}
        if path.rstrip("/") == "/api/admin/policies":
            if not caller.admin:
                return 403, {"error": "需要管理员权限"}
            if method != "GET":
                return 405, {"error": "不支持的方法"}
            data = flows.policy_rules_overview()
            for acc in data["accounts"]:
                try:
                    label = (
                        self._account_label(acc["platform"], acc["account"])
                        if self._account_label
                        else ""
                    )
                except Exception:  # noqa: BLE001 — 标签只是展示
                    label = ""
                acc["account_label"] = label or acc["account"]
            return 200, data
        if path.startswith("/api/admin/policies"):
            return 404, {"error": "没有这个接口"}
        parts = [p for p in path.split("/") if p]  # api, [admin], requests, ...
        admin = len(parts) > 1 and parts[1] == "admin"
        # 只接 /api/requests… 和 /api/admin/requests…：别的前缀不能落进申请单接口
        index = 2 if admin else 1
        if len(parts) <= index or parts[index] != "requests":
            return 404, {"error": "没有这个接口"}
        rest = parts[3:] if admin else parts[2:]
        if admin and not caller.admin:
            return 403, {"error": "需要管理员权限"}
        # 员工接口里即使调用者是管理员，也按员工视角返回：管理操作只在管理后台出现
        caller = caller if admin else replace(caller, admin=False)

        if not rest:
            if method == "GET":
                if admin:
                    wanted = (query.get("status") or [""])[0]
                    items = flows.store.all()
                    if wanted == "open":
                        items = [x for x in items if x.get("status") in t.OPEN]
                    elif wanted:
                        items = [x for x in items if x.get("status") == wanted]
                else:
                    for item in flows.store.mine(caller.union_id):
                        if item.get("status") in (t.PENDING, t.CLAIMABLE):
                            _quiet_sync(flows, item["id"])
                    items = flows.store.mine(caller.union_id)
                items = sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)
                return 200, {"requests": [self._view(flows, x, caller) for x in items]}
            if method == "POST" and not admin:
                ticket = flows.submit(
                    applicant=caller.applicant,
                    email=caller.email,
                    template_id=str(body.get("template_id") or ""),
                    payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
                    reason=str(body.get("reason") or ""),
                )
                return 201, {"request": self._view(flows, ticket, caller)}
            return 405, {"error": "不支持的方法"}

        if not admin and rest == ["options"] and method == "GET":
            return 200, {
                "options": flows.options(caller.union_id),
                "my_accounts": flows.my_accounts(caller.union_id),
            }

        ticket_id = rest[0]
        if not _ID.match(ticket_id):
            return 404, {"error": "没有这张申请单"}
        ticket = flows.store.get(ticket_id)
        if not admin and ticket["applicant"].get("union_id") != caller.union_id:
            return 404, {"error": "没有这张申请单"}
        action = rest[1] if len(rest) > 1 else ""

        if not action and method == "GET":
            if ticket.get("status") in (t.PENDING, t.CLAIMABLE):
                ticket = _quiet_sync(flows, ticket_id) or ticket
            return 200, {"request": self._view(flows, ticket, caller)}
        if method != "POST":
            return 405, {"error": "不支持的方法"}

        if admin and action == "retry":
            return 200, {
                "request": self._view(
                    flows, flows.execute(ticket_id, actor=caller.union_id), caller
                )
            }
        if admin and action == "recover":
            flows.recover_stuck(ticket_id, actor=caller.union_id)
            return 200, {"request": self._view(flows, flows.store.get(ticket_id), caller)}
        if admin and action == "close":
            note = str(body.get("note") or "")[:200]
            return 200, {
                "request": self._view(
                    flows, flows.close(ticket_id, actor=caller.union_id, note=note), caller
                )
            }
        if not admin and action == "withdraw":
            done = flows.withdraw(ticket_id, union_id=caller.union_id)
            return 200, {"request": self._view(flows, done, caller)}
        if not admin and action == "credential":
            hours = body.get("hours")
            ticket, cred = flows.claim_credential(
                ticket_id, union_id=caller.union_id, hours=hours if isinstance(hours, int) else None
            )
            tpl = ticket["template"]
            return 200, {
                "request": self._view(flows, ticket, caller),
                "credential": {
                    "platform": tpl["platform"],
                    "account": tpl["account"],
                    "access_key_id": cred.access_key_id,
                    "access_key_secret": cred.secret,
                    "security_token": cred.token,
                    "expiration": cred.expiration,
                },
            }
        if not admin and action == "password":
            ticket, password = flows.claim_password(ticket_id, union_id=caller.union_id)
            tpl = ticket["template"]
            login = {
                "aliyun": f"https://signin.aliyun.com/{tpl['account']}.onaliyun.com/login.htm",
                "volcano": f"https://console.volcengine.com/auth/login/user/{tpl['account']}",
            }.get(tpl["platform"], "")
            return 200, {
                "request": self._view(flows, ticket, caller),
                "login": {
                    "username": ticket["payload"]["username"],
                    "password": password,
                    "login_url": login,
                    "must_change": True,
                },
            }
        return 404, {"error": "没有这个操作"}
