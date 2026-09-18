"""申请相关的 HTTP 接口。server.py 只做登录、角色、CSRF，然后把请求交给这里。

员工接口                                   管理员接口
  GET  /api/policies                        （权限列表：全部权限策略对本人的状态）
  GET  /api/requests/options                GET  /api/admin/requests?status=
  GET  /api/requests                        GET  /api/admin/requests/<id>
  POST /api/requests                        POST /api/admin/requests/<id>/retry
  GET  /api/requests/<id>                   POST /api/admin/requests/<id>/close
                                            POST /api/admin/requests/<id>/recover（卡住的单子）
  POST /api/requests/<id>/withdraw          POST /api/admin/requests/<id>/fulfil
                                            （资源开通：管理员登记实例信息）
                                            POST /api/admin/requests/<id>/revoke
                                            （凭证外泄：删密文 + 删云上的号和密钥）
  POST /api/requests/<id>/password          （领取一次性初始密码）

员工视角的申请单去掉模板里的角色 ARN、执行细节；事件里的操作人显示成「你 / 飞书 / 系统 / 管理员」，
不把管理员的 union_id 暴露给员工。
"""

from __future__ import annotations

import contextlib
import re
import sys
from dataclasses import dataclass, replace
from typing import Callable, Optional

from . import platforms
from . import tickets as t
from .approval import Applicant, ApprovalError
from .catalog import KIND_LABELS
from .errors import DeliveryError
from .flows import FlowError, Flows, password_claims

#: 申请单号的形状。server.py 的查看凭证接口也用它校验，别在那边再写一份
#: 尾锚用 \Z 不用 $ —— $ 会放过结尾的换行，"REQ-…-AAAAAAAA\n" 能过校验
TICKET_ID = re.compile(r"\AREQ-\d{8}-[0-9A-F]{8}\Z")
_ID = TICKET_ID
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
    "fulfilling": "等待管理员开通",
    "execute_start": "开始开通",
    "execute_done": "开通完成",
    "execute_failed": "开通失败",
    "credential_sealed": "凭证已加密存放",
    "credential_viewed": "查看凭证",
    "cred_user_reserved": "准备发放凭证",
    "credential_issued": "发放凭证",
    "credential_failed": "签发凭证失败",
    "credential_revoked": "凭证未送达，已作废",
    "credential_orphaned": "凭证未送达，作废时没清干净",
    "credential_sealed_dropped": "管理员作废，查看地址已失效",
    "credential_reclaimed": "已清理没送达的凭证",
    "await_fulfil": "等待管理员开通",
    "fulfilled": "管理员已登记开通结果",
    "password_issued": "领取初始密码",
    "password_failed": "初始密码未生成",
    "user_created": "新建子账号",
    "linked": "对应到申请人",
    "link_needed": "待管理员对应到申请人",
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
        "expires_at": ticket.get("expires_at", ""),
        "result": ticket.get("result", ""),
        "events": events,
        "actions": {
            "withdraw": own and status == t.PENDING,
            # 凭证不在面板上领取：审批通过即签发，审批评论里给一个带密钥的查看地址
            "password": own
            and status == t.DONE
            and ticket.get("kind") == "account"
            and bool(tpl.get("console_login"))
            and not claimed_password,
            "retry": viewer.admin and status == t.FAILED,
            "fulfil": viewer.admin and status == t.FULFILLING and ticket.get("kind") == "resource",
            "close": viewer.admin and status in (t.FAILED, t.FULFILLING),
            "recover": viewer.admin and status in (t.EXECUTING, t.SUBMITTING),
            # 链接外泄时管理员要能立刻掐掉。删密文 + 删云上的子账号和密钥，
            # 不等到期。没有这个按钮的话，唯一的办法是手改申请单或去云控制台。
            # 要求「还有东西可作废」：清干净之后按钮还亮着的话，再点一次就是空转
            # 申请人也能作废自己的：发现外泄的第一个人通常是他，
            # 让他等管理员等于把泄漏窗口拉长；而作废只会减少权限，没有提权风险
            "revoke": (viewer.admin or own)
            and ticket.get("kind") == "credential"
            and status in (t.DONE, t.FAILED, t.CLOSED)
            and bool(ticket.get("cred_user") or (ticket.get("sealed") or {}).get("ciphertext")),
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
        claim_resources: Optional[Callable[[str, str, list, str], None]] = None,
    ):
        self._flows = flows
        self._account_label = account_label
        #: 登记开通结果后把实例指给申请人。由 server 注入（它才知道归属表在哪）
        self._claim = claim_resources

    def _claim_resources(self, ticket: dict) -> None:
        """把刚登记的实例指给申请人。没配归属表路径、或申请人没邮箱，就安静跳过。"""
        claim = self._claim
        if claim is None:
            return
        ids = ticket.get("resource_ids") or []
        email = str((ticket.get("applicant") or {}).get("email") or "")
        tpl = ticket.get("template") or {}
        if not ids or not email:
            return
        # 归属写不进去不该让登记这件事失败：单子已经 DONE 了，归属可以事后在资产页补指，
        # 而把一张已经开通的单子回滚成失败要糟糕得多
        with contextlib.suppress(Exception):
            claim(str(tpl.get("platform") or ""), str(tpl.get("account") or ""), list(ids), email)

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
                        if item.get("status") == t.PENDING:
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
            if ticket.get("status") == t.PENDING:
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
        if admin and action == "fulfil":
            note = str(body.get("note") or "")[:500]
            done = flows.fulfil(
                ticket_id,
                actor=caller.union_id,
                note=note,
                resource_ids=body.get("resource_ids") or (),
            )
            # 登记完当场把归属指给申请人。**这是唯一一个「开通那一刻就知道主人是谁」的时机**，
            # 错过了就只能事后靠人去猜。写失败不影响登记本身：单子已经是 DONE 了，
            # 归属可以在资产页补指，而把一张已经开通的单子回滚成失败要糟糕得多
            self._claim_resources(done)
            return 200, {"request": self._view(flows, done, caller)}
        if admin and action == "revoke":
            return 200, {
                "request": self._view(
                    flows, flows.revoke_now(ticket_id, actor=caller.union_id), caller
                )
            }
        if admin and action == "close":
            note = str(body.get("note") or "")[:200]
            return 200, {
                "request": self._view(
                    flows, flows.close(ticket_id, actor=caller.union_id, note=note), caller
                )
            }
        if not admin and action == "revoke":
            # 申请人只能作废自己的（归属检查在 flows.revoke_mine 里，按 union_id）
            return 200, {
                "request": self._view(
                    flows, flows.revoke_mine(ticket_id, union_id=caller.union_id), caller
                )
            }
        if not admin and action == "withdraw":
            done = flows.withdraw(ticket_id, union_id=caller.union_id)
            return 200, {"request": self._view(flows, done, caller)}
        if not admin and action == "password":
            ticket, password = flows.claim_password(ticket_id, union_id=caller.union_id)
            tpl = ticket["template"]
            login = platforms.get(tpl["platform"]).login_url(tpl["account"])
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
