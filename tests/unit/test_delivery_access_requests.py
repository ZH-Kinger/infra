"""云账号自助申请：模板校验、飞书审批核对、状态机、开通与领取。

重点锁住 docs/cloud-access-platform.md 的规则：没有已通过的飞书审批不开通（R1）、只能给自己
申请（R2）、只能选模板（R3）、凭证不落盘（R4）。数据全部虚构，云和飞书接口全部替换。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import people as people_mod
from delivery import tickets as t
from delivery.approval import Applicant, ApprovalConfig, ApprovalError, FeishuApproval
from delivery.clouds import aliyun
from delivery.flows import FlowError, Flows
from delivery.provision import AliyunExecutor, ProvisionError, TempCredential, session_name

ACC = "1000000000000001"
TEMPLATES = {
    "schema": catalog_mod.SCHEMA,
    "templates": [
        {
            "id": "oss-read",
            "kind": "permission",
            "platform": "aliyun",
            "account": ACC,
            "title": "OSS 只读",
            "groups": ["grp-oss-read"],
            "max_days": 90,
        },
        {
            "id": "dev-sts",
            "kind": "credential",
            "platform": "aliyun",
            "account": ACC,
            "title": "开发临时凭证",
            "role_arn": f"acs:ram::{ACC}:role/dev-readonly",
            "max_hours": 4,
            "valid_days": 7,
        },
        {
            "id": "new-user",
            "kind": "account",
            "platform": "aliyun",
            "account": ACC,
            "title": "新员工子账号",
            "groups": ["grp-default"],
            "console_login": True,
        },
    ],
}
CONFIG = ApprovalConfig(
    approval_code="APPROVAL-1",
    widgets={"ticket_id": "w1", "kind": "w2", "summary": "w3", "reason": "w4"},
)
LI = Applicant(union_id="on_li", name="李四", open_id="ou_li")
NEW = Applicant(union_id="on_new", name="新人", open_id="ou_new")


class FakeFeishu:
    """模拟飞书审批接口：记录发起的实例，测试里手动改状态。"""

    def __init__(self):
        self.instances = {}
        self.calls = []

    def __call__(self, method, url, token, body):
        self.calls.append((method, url, body))
        if method == "POST" and url.endswith("/approval/v4/instances"):
            code = f"INST-{len(self.instances) + 1}"
            ids = {k: body[k] for k in ("open_id", "user_id") if k in body}
            self.instances[code] = {
                "approval_code": body["approval_code"],
                "status": "PENDING",
                "form": body["form"],
                **ids,
            }
            return {"code": 0, "data": {"instance_code": code}}
        if method == "GET" and "/approval/v4/instances/" in url:
            code = url.rsplit("/", 1)[1]
            return {"code": 0, "data": dict(self.instances[code])}
        if "/instances/cancel" in url:
            self.instances[body["instance_code"]]["status"] = "CANCELED"
            return {"code": 0, "data": {}}
        return {"code": 99, "msg": "unexpected"}


class FakeExecutor:
    def __init__(self):
        self.actions = []
        self.fail = None

    def _maybe_fail(self):
        if self.fail:
            raise ProvisionError(self.fail)

    members = set()

    def in_group(self, user, group):
        return (user, group) in self.members

    def add_to_group(self, user, group):
        self._maybe_fail()
        self.actions.append(("add", user, group))

    def remove_from_group(self, user, group):
        self._maybe_fail()
        self.actions.append(("remove", user, group))

    def create_user(self, user, display):
        self._maybe_fail()
        self.actions.append(("create", user, display))

    def reset_password(self, user):
        self.actions.append(("password", user))
        return "Pw-Secret-123!"

    def assume_role(self, role, name, hours):
        self.actions.append(("sts", role, name, hours))
        return TempCredential("STS.AK1234", "sts-secret", "sts-token", "2026-09-15T12:00:00Z")


def _roster():
    proposal = {
        "domain": "wuji.tech",
        "people": [
            {
                "email": "li.si@wuji.tech",
                "links": [{"scope": f"aliyun/{ACC}", "name": "lisi", "status": "confirmed"}],
            }
        ],
        "unlinked": [],
        "services": [],
    }
    built = people_mod.build(proposal, [])
    built["people"][0]["union_id"] = "on_li"
    built["people"].append(
        {
            "union_id": "on_new",
            "name": "新人",
            "email": "new@wuji.tech",
            "accounts": [],
            "pending": [],
        }
    )
    return people_mod.parse(built)


class Harness:
    def __init__(self, templates=TEMPLATES):
        self.dir = Path(tempfile.mkdtemp())
        self.templates = json.loads(json.dumps(templates))
        self.feishu = FakeFeishu()
        self.executor = FakeExecutor()
        self.links = []
        self.now = [1_800_000_000.0]
        self.store = t.TicketStore(str(self.dir / "tickets.json"), clock=lambda: self.now[0])
        self.approval = FeishuApproval(CONFIG, lambda: "tenant-token", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(self.templates),
            approval=lambda: self.approval,
            roster=_roster,
            executor=lambda platform, account: self.executor,
            add_manual_link=lambda email, account, ticket: self.links.append(
                (email, account, ticket)
            ),
            clock=lambda: self.now[0],
        )

    def submit(
        self, applicant=LI, template="oss-read", payload=None, reason="项目需要读取训练数据"
    ):
        payload = payload if payload is not None else {"cloud_user": "lisi", "days": 30}
        email = "li.si@wuji.tech" if applicant is LI else "new@wuji.tech"
        return self.flows.submit(
            applicant=applicant, email=email, template_id=template, payload=payload, reason=reason
        )

    def approve(self, ticket, status="APPROVED"):
        self.feishu.instances[ticket["approval"]["instance_code"]]["status"] = status


class CatalogTests(unittest.TestCase):
    def test_valid_catalog_loads(self):
        cat = catalog_mod.parse(TEMPLATES)
        self.assertEqual([x.id for x in cat.templates], ["oss-read", "dev-sts", "new-user"])
        self.assertNotIn("role_arn", cat.get("dev-sts").public())

    def bad(self, **changes):
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"][changes.pop("index", 0)].update(changes)
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse(data)

    def test_rejects_unsafe_or_wrong_templates(self):
        self.bad(kind="admin")
        self.bad(platform="aws")
        self.bad(groups=[])
        self.bad(groups=["bad group"])
        self.bad(typo_field=1)
        self.bad(index=1, role_arn="acs:ram::999999999:role/other-account")
        self.bad(index=1, max_hours=24)
        self.bad(index=2, username_pattern="[a-z]+")
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"].append(dict(data["templates"][0]))
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse(data)

    def test_missing_file_means_no_templates(self):
        self.assertEqual(catalog_mod.load("/nonexistent/templates.json").templates, ())


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.feishu = FakeFeishu()
        self.approval = FeishuApproval(CONFIG, lambda: "tok", transport=self.feishu)
        self.code = self.approval.create(
            ticket_id="REQ-1", kind_label="权限", summary="s", reason="r", applicant=LI
        )

    def test_create_uses_ticket_as_idempotency_key_and_form(self):
        body = self.feishu.calls[0][2]
        self.assertEqual(body["uuid"], "REQ-1")
        self.assertEqual(body["open_id"], "ou_li")
        self.assertIn('"value": "REQ-1"', body["form"])

    def test_verify_requires_approved(self):
        with self.assertRaises(ApprovalError):
            self.approval.verify_approved(instance_code=self.code, ticket_id="REQ-1", applicant=LI)
        self.feishu.instances[self.code]["status"] = "APPROVED"
        self.approval.verify_approved(instance_code=self.code, ticket_id="REQ-1", applicant=LI)

    def test_instance_must_match_ticket_applicant_and_definition(self):
        self.feishu.instances[self.code]["status"] = "APPROVED"
        with self.assertRaises(ApprovalError):  # 套用到另一张申请单
            self.approval.verify_approved(instance_code=self.code, ticket_id="REQ-2", applicant=LI)
        with self.assertRaises(ApprovalError):  # 别人发起的实例
            self.approval.verify_approved(instance_code=self.code, ticket_id="REQ-1", applicant=NEW)
        self.feishu.instances[self.code]["approval_code"] = "OTHER-FLOW"
        with self.assertRaises(ApprovalError):  # 别的审批流里通过的实例
            self.approval.verify_approved(instance_code=self.code, ticket_id="REQ-1", applicant=LI)

    def test_unknown_status_and_api_errors_refused(self):
        self.feishu.instances[self.code]["status"] = "WEIRD"
        with self.assertRaises(ApprovalError):
            self.approval.status(instance_code=self.code, ticket_id="REQ-1", applicant=LI)
        broken = FeishuApproval(
            CONFIG, lambda: "tok", transport=lambda *a: {"code": 1, "msg": "no"}
        )
        with self.assertRaises(ApprovalError):
            broken.fetch("X")

    def test_applicant_without_feishu_id_cannot_submit(self):
        with self.assertRaises(ApprovalError):
            Applicant(union_id="on_x", name="x").id_fields()


class StoreTests(unittest.TestCase):
    def test_transitions_enforced_and_events_appended(self):
        store = t.TicketStore(str(Path(tempfile.mkdtemp()) / "tickets.json"))
        ticket = store.create({"kind": "permission", "applicant": {"union_id": "u"}}, actor="u")
        with self.assertRaises(t.TicketError):
            store.update(ticket["id"], actor="u", expect=[t.SUBMITTING], to=t.DONE, event="x")
        ticket = store.update(
            ticket["id"], actor="u", expect=[t.SUBMITTING], to=t.PENDING, event="p"
        )
        self.assertEqual([e["event"] for e in ticket["events"]], ["created", "p"])
        with self.assertRaises(t.TicketError):
            store.update(
                ticket["id"], actor="u", expect=[t.PENDING], event="x", fields={"applicant": {}}
            )
        self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def test_permission_happy_path(self):
        ticket = self.h.submit()
        self.assertEqual(ticket["status"], t.PENDING)
        self.assertEqual(self.h.flows.sync(ticket["id"], force=True)["status"], t.PENDING)
        self.h.approve(ticket)
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(self.h.executor.actions, [("add", "lisi", "grp-oss-read")])

    def test_expired_permission_revoked_but_preexisting_kept(self):
        self.h.executor.members = {("lisi", "grp-oss-read")}  # 开通前就在这个组
        ticket = self.h.submit(payload={"cloud_user": "lisi", "days": 1})
        self.h.approve(ticket)
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["preexisting_groups"], ["grp-oss-read"])
        self.assertEqual(self.h.flows.revoke_expired(), [])  # 没到期
        self.h.now[0] += 2 * 86400
        result = self.h.flows.revoke_expired()
        self.assertEqual(len(result), 1)
        self.assertNotIn(("remove", "lisi", "grp-oss-read"), self.h.executor.actions)
        self.assertEqual(self.h.store.get(ticket["id"])["status"], t.REVOKED)

    def test_expired_permission_removed_unless_other_active_grant(self):
        first = self.h.submit(payload={"cloud_user": "lisi", "days": 1})
        self.h.approve(first)
        self.h.flows.sync(first["id"], force=True)
        self.h.now[0] += 2 * 86400
        self.h.flows.revoke_expired()
        self.assertIn(("remove", "lisi", "grp-oss-read"), self.h.executor.actions)

        self.h.executor.actions.clear()
        a = self.h.submit(payload={"cloud_user": "lisi", "days": 1})
        self.h.approve(a)
        self.h.flows.sync(a["id"], force=True)
        b = self.h.submit(payload={"cloud_user": "lisi", "days": 30})
        self.h.approve(b)
        self.h.flows.sync(b["id"], force=True)
        self.h.now[0] += 2 * 86400
        self.h.flows.revoke_expired()
        self.assertNotIn(("remove", "lisi", "grp-oss-read"), self.h.executor.actions)
        self.assertEqual(self.h.store.get(a["id"])["status"], t.REVOKED)
        self.assertEqual(self.h.store.get(b["id"])["status"], t.DONE)

    def test_revoke_failure_retried_later(self):
        ticket = self.h.submit(payload={"cloud_user": "lisi", "days": 1})
        self.h.approve(ticket)
        self.h.flows.sync(ticket["id"], force=True)
        self.h.now[0] += 2 * 86400
        self.h.executor.fail = "RemoveUserFromGroup 超时"
        self.h.flows.revoke_expired()
        self.assertEqual(self.h.store.get(ticket["id"])["status"], t.DONE)
        self.h.executor.fail = None
        self.h.flows.revoke_expired()
        self.assertEqual(self.h.store.get(ticket["id"])["status"], t.REVOKED)

    def test_rejected_never_executes(self):
        ticket = self.h.submit()
        self.h.approve(ticket, "REJECTED")
        self.assertEqual(self.h.flows.sync(ticket["id"], force=True)["status"], t.REJECTED)
        with self.assertRaises(t.TicketError):
            self.h.flows.execute(ticket["id"], actor="admin")
        self.assertEqual(self.h.executor.actions, [])

    def test_execute_without_approval_refused(self):
        ticket = self.h.submit()
        with self.assertRaises((ApprovalError, t.TicketError)):
            self.h.flows.execute(ticket["id"], actor="admin")
        self.assertEqual(self.h.executor.actions, [])

    def test_cannot_request_for_someone_elses_account(self):
        with self.assertRaises(FlowError):
            self.h.submit(payload={"cloud_user": "wangwu", "days": 30})
        with self.assertRaises(FlowError):
            self.h.submit(applicant=NEW, payload={"cloud_user": "lisi", "days": 30})

    def test_payload_limits(self):
        with self.assertRaises(FlowError):
            self.h.submit(payload={"cloud_user": "lisi", "days": 365})
        with self.assertRaises(FlowError):
            self.h.submit(template="dev-sts", payload={"hours": 12})
        with self.assertRaises(FlowError):
            self.h.submit(reason="短")
        with self.assertRaises(FlowError):
            self.h.submit(template="no-such-template")

    def test_duplicate_open_request_refused(self):
        self.h.submit()
        with self.assertRaises(FlowError):
            self.h.submit()

    def test_template_changed_during_approval_blocks_execution(self):
        ticket = self.h.submit()
        self.h.templates["templates"][0]["groups"].append("grp-admin")
        self.h.approve(ticket)
        failed = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertIn("模板", failed["events"][-1]["note"])
        self.assertEqual(self.h.executor.actions, [])

    def test_execution_failure_then_admin_retry(self):
        ticket = self.h.submit()
        self.h.approve(ticket)
        self.h.executor.fail = "AddUserToGroup 失败：AccessKeyId=LTAI-secret 超时"
        failed = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertNotIn("LTAI-secret", json.dumps(failed, ensure_ascii=False))
        self.h.executor.fail = None
        self.assertEqual(self.h.flows.execute(ticket["id"], actor="on_admin")["status"], t.DONE)

    def test_retry_rechecks_approval(self):
        ticket = self.h.submit()
        self.h.approve(ticket)
        self.h.executor.fail = "boom"
        self.h.flows.sync(ticket["id"], force=True)
        self.h.approve(ticket, "CANCELED")  # 飞书里审批被撤销
        self.h.executor.fail = None
        with self.assertRaises(ApprovalError):
            self.h.flows.execute(ticket["id"], actor="on_admin")
        self.assertEqual(self.h.executor.actions, [])

    def test_credential_claim_is_not_stored(self):
        ticket = self.h.submit(template="dev-sts", payload={"hours": 2})
        self.h.approve(ticket)
        ready = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(ready["status"], t.CLAIMABLE)
        _, cred = self.h.flows.claim_credential(ticket["id"], union_id="on_li")
        self.assertEqual(cred.secret, "sts-secret")
        stored = (self.h.dir / "tickets.json").read_text(encoding="utf-8")
        self.assertNotIn("sts-secret", stored)
        self.assertNotIn("sts-token", stored)
        self.assertEqual(
            self.h.executor.actions[-1][0:2], ("sts", f"acs:ram::{ACC}:role/dev-readonly")
        )
        with self.assertRaises(FlowError):  # 不能超过申请的时长
            self.h.flows.claim_credential(ticket["id"], union_id="on_li", hours=3)
        with self.assertRaises(FlowError):  # 别人不能领
            self.h.flows.claim_credential(ticket["id"], union_id="on_new")

    def test_credential_expires(self):
        ticket = self.h.submit(template="dev-sts", payload={"hours": 1})
        self.h.approve(ticket)
        self.h.flows.sync(ticket["id"], force=True)
        self.h.now[0] += 8 * 86400
        with self.assertRaises(FlowError):
            self.h.flows.claim_credential(ticket["id"], union_id="on_li")
        self.assertEqual(self.h.store.get(ticket["id"])["status"], t.EXPIRED)

    def test_account_creation_and_one_time_password(self):
        ticket = self.h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        self.h.approve(ticket)
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(
            self.h.executor.actions,
            [("create", "xinren", "新人"), ("add", "xinren", "grp-default")],
        )
        self.assertEqual(self.h.links, [("new@wuji.tech", f"aliyun/{ACC}/xinren", ticket["id"])])
        _, pw = self.h.flows.claim_password(ticket["id"], union_id="on_new")
        self.assertEqual(pw, "Pw-Secret-123!")
        self.assertNotIn("Pw-Secret", (self.h.dir / "tickets.json").read_text(encoding="utf-8"))
        with self.assertRaises(FlowError):
            self.h.flows.claim_password(ticket["id"], union_id="on_new")

    def test_account_request_rules(self):
        with self.assertRaises(FlowError):  # 已经有子账号
            self.h.submit(template="new-user", payload={"username": "lisi2"})
        with self.assertRaises(FlowError):  # 用户名不合规则
            self.h.submit(applicant=NEW, template="new-user", payload={"username": "Bad Name"})

    def test_withdraw_cancels_feishu_instance(self):
        ticket = self.h.submit()
        with self.assertRaises(FlowError):
            self.h.flows.withdraw(ticket["id"], union_id="on_new")
        done = self.h.flows.withdraw(ticket["id"], union_id="on_li")
        self.assertEqual(done["status"], t.WITHDRAWN)
        self.assertEqual(self.h.feishu.instances["INST-1"]["status"], "CANCELED")

    def test_submit_failure_recorded(self):
        self.h.approval = FeishuApproval(
            CONFIG, lambda: "tok", transport=lambda *a: {"code": 60001, "msg": "approval not found"}
        )
        ticket = self.h.submit()
        self.assertEqual(ticket["status"], t.SUBMIT_FAILED)

    def test_no_approval_config_blocks_everything(self):
        self.h.approval = None
        with self.assertRaises(FlowError):
            self.h.submit()

    def test_options_mark_availability(self):
        opts = {o["id"]: o for o in self.h.flows.options("on_li")}
        self.assertTrue(opts["oss-read"]["available"])
        self.assertFalse(opts["new-user"]["available"])
        opts = {o["id"]: o for o in self.h.flows.options("on_new")}
        self.assertFalse(opts["oss-read"]["available"])
        self.assertTrue(opts["new-user"]["available"])


class ExecutorTests(unittest.TestCase):
    def transport(self, responses):
        calls = []

        def send(url):
            import urllib.parse

            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            action = query["Action"]
            result = responses.get(action, (200, {}))
            return result(query) if callable(result) else result

        return send, calls

    def test_refuses_when_credentials_belong_to_other_account(self):
        send, _ = self.transport({"GetCallerIdentity": (200, {"AccountId": "999"})})
        ex = AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send)
        with self.assertRaises(ProvisionError):
            ex.add_to_group("lisi", "grp")

    def test_create_user_never_takes_over_existing(self):
        send, calls = self.transport(
            {"GetCallerIdentity": (200, {"AccountId": ACC}), "GetUser": (200, {"User": {}})}
        )
        ex = AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send)
        with self.assertRaises(ProvisionError):
            ex.create_user("lisi", "李四")
        self.assertNotIn("CreateUser", [c["Action"] for c in calls])

    def test_add_to_group_idempotent(self):
        send, _ = self.transport(
            {
                "GetCallerIdentity": (200, {"AccountId": ACC}),
                "GetUser": (200, {"User": {}}),
                "AddUserToGroup": (409, {"Code": "EntityAlreadyExists.User.Group", "Message": "x"}),
            }
        )
        AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send).add_to_group(
            "lisi", "g"
        )

    def test_assume_role_parameters(self):
        send, calls = self.transport(
            {
                "GetCallerIdentity": (200, {"AccountId": ACC}),
                "AssumeRole": (
                    200,
                    {
                        "Credentials": {
                            "AccessKeyId": "STS.x",
                            "AccessKeySecret": "s",
                            "SecurityToken": "tok",
                            "Expiration": "2026-01-01T00:00:00Z",
                        }
                    },
                ),
            }
        )
        ex = AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send)
        cred = ex.assume_role(f"acs:ram::{ACC}:role/r", "li.si@wuji.tech", 3)
        call = next(c for c in calls if c["Action"] == "AssumeRole")
        self.assertEqual(
            (call["DurationSeconds"], call["RoleSessionName"]), ("10800", "li.si@wuji.tech")
        )
        self.assertNotIn("tok", repr(cred))

    def test_session_name_sanitized(self):
        self.assertEqual(session_name("张三 <x>"), "----x-")
        self.assertEqual(session_name("a"), "u-a")


if __name__ == "__main__":
    unittest.main()


class RequestsHttpTests(unittest.TestCase):
    """HTTP 层：员工 / 管理员视角分开、CSRF、CLI Bearer 令牌、凭证只在响应里。"""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from delivery.feishu import FeishuUser
        from delivery.registry import PlatformRegistry
        from delivery.server import Backend, Store, _WebSession, make_handler

        self.h = Harness()
        d = self.h.dir
        roster = _roster()
        rows = [people_mod.person_row(p) for p in roster.people]
        (d / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (d / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}))
        (d / "templates.json").write_text(json.dumps(TEMPLATES), encoding="utf-8")
        (d / "approval.json").write_text(
            json.dumps({"approval_code": CONFIG.approval_code, "widgets": dict(CONFIG.widgets)})
        )
        backend = Backend(
            people_path=str(d / "people.json"),
            bindings_path=str(d / "bindings.json"),
            admins_path=str(d / "admins.json"),
            tickets_path=str(d / "tickets.json"),
            templates_path=str(d / "templates.json"),
            approval_path=str(d / "approval.json"),
            feishu_token=lambda: "tok",
            executor=lambda p, a: self.h.executor,
            approval_transport=self.h.feishu,
        )
        self.store = Store()
        self.store.sessions["li"] = _WebSession(user=FeishuUser("ou_li", "on_li", "李四"))
        self.store.sessions["admin"] = _WebSession(
            user=FeishuUser("ou_admin", "on_admin", "管理员")
        )
        handler = make_handler(
            PlatformRegistry.load(),
            self.store,
            app_id="cli_demo",
            app_secret="s",
            base_url="http://127.0.0.1",
            backend=backend,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, *, sid="li", bearer=False, headers=None):
        import http.client

        from delivery.server import COOKIE_NAME

        head = {"X-Panel-Request": "1", "Content-Type": "application/json"}
        if bearer:
            head["Authorization"] = f"Bearer {sid}"
        else:
            head["Cookie"] = f"{COOKIE_NAME}={sid}"
        head.update(headers or {})
        head = {k: v for k, v in head.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        raw = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=head)
        resp = conn.getresponse()
        out = (resp.status, json.loads(resp.read() or b"{}"))
        conn.close()
        return out

    NEW = {
        "template_id": "dev-sts",
        "payload": {"hours": 2},
        "reason": "本地调试脚本需要只读访问",
    }

    def test_employee_flow_via_bearer_token(self):
        status, data = self.call("POST", "/api/requests", self.NEW, bearer=True)
        self.assertEqual(status, 201, data)
        rid = data["request"]["id"]
        self.assertNotIn("role_arn", json.dumps(data))
        self.h.feishu.instances["INST-1"]["status"] = "APPROVED"
        status, data = self.call("GET", f"/api/requests/{rid}", bearer=True)
        self.assertEqual(data["request"]["status"], "claimable")
        status, data = self.call("POST", f"/api/requests/{rid}/credential", {}, bearer=True)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["credential"]["access_key_secret"], "sts-secret")
        stored = (self.h.dir / "tickets.json").read_text(encoding="utf-8")
        self.assertNotIn("sts-secret", stored)

    def test_csrf_required_for_cookie_posts(self):
        for headers in (
            {"X-Panel-Request": None},
            {"Content-Type": "text/plain"},
            {"Origin": "https://evil.example.com"},
        ):
            status, _ = self.call("POST", "/api/requests", self.NEW, headers=headers)
            self.assertEqual(status, 403, headers)
        self.assertFalse((self.h.dir / "tickets.json").exists())

    def test_admin_space_and_employee_space_separated(self):
        status, data = self.call("POST", "/api/requests", self.NEW)
        rid = data["request"]["id"]
        self.assertEqual(self.call("GET", "/api/admin/requests")[0], 403)
        self.assertEqual(self.call("POST", f"/api/admin/requests/{rid}/retry", {})[0], 403)
        status, data = self.call("GET", "/api/admin/requests", sid="admin")
        self.assertEqual(status, 200)
        self.assertEqual(data["requests"][0]["applicant"]["union_id"], "on_li")
        # 员工看不到别人的申请单，也不暴露管理员身份
        self.assertEqual(self.call("GET", f"/api/requests/{rid}", sid="admin")[0], 404)
        status, data = self.call("GET", f"/api/requests/{rid}")
        self.assertNotIn("union_id", data["request"]["applicant"])
        self.assertNotIn("approval", data["request"])

    def test_unknown_or_malformed_ids(self):
        self.assertEqual(self.call("GET", "/api/requests/../../admin")[0], 404)
        self.assertEqual(self.call("GET", "/api/requests/REQ-bad")[0], 404)
        self.assertEqual(self.call("GET", "/api/requests/options")[0], 200)

    def test_anonymous_refused(self):
        self.assertEqual(self.call("GET", "/api/requests", sid="nobody")[0], 401)
        self.assertEqual(
            self.call("POST", "/api/requests", self.NEW, sid="nobody", bearer=True)[0], 401
        )

    def test_login_without_union_id_refused(self):
        from delivery.feishu import FeishuUser
        from delivery.server import _WebSession

        self.store.sessions["nouid"] = _WebSession(user=FeishuUser("ou_x", "", "无名"))
        self.assertEqual(self.call("GET", "/api/requests", sid="nouid")[0], 403)
        self.assertEqual(self.call("POST", "/api/requests", self.NEW, sid="nouid")[0], 403)
        self.assertFalse((self.h.dir / "tickets.json").exists())


class MemberExecutor(FakeExecutor):
    """记录真实成员关系的执行器：加组、移出组会改变 in_group 的结果。"""

    def __init__(self):
        super().__init__()
        self.members = set()
        self.fail_on = None  # (动作, 组名) → 只让这一步失败

    def add_to_group(self, user, group):
        if self.fail_on == ("add", group):
            raise ProvisionError(f"AddUserToGroup {group} 超时")
        super().add_to_group(user, group)
        self.members.add((user, group))

    def remove_from_group(self, user, group):
        super().remove_from_group(user, group)
        self.members.discard((user, group))


TWO_GROUPS = json.loads(json.dumps(TEMPLATES))
TWO_GROUPS["templates"][0]["groups"] = ["grp-a", "grp-b"]
UNLIMITED = json.loads(json.dumps(TEMPLATES))
UNLIMITED["templates"][0]["max_days"] = 0


class AuditRegressionTests(unittest.TestCase):
    """平台审计发现的问题：每条锁一个回归测试。"""

    def harness(self, templates=TEMPLATES):
        h = Harness(templates)
        h.executor = MemberExecutor()
        return h

    def grant(self, h, days):
        ticket = h.submit(payload={"cloud_user": "lisi", "days": days})
        h.approve(ticket)
        return h.flows.sync(ticket["id"], force=True)

    def test_renewal_before_expiry_does_not_become_permanent(self):
        h = self.harness()
        a = self.grant(h, 7)
        h.now[0] += 86400
        # 同模板同子账号有进行中的单子会被判重；A 已完成，可以续期
        b = self.grant(h, 30)
        self.assertEqual(b["status"], t.DONE)
        self.assertEqual(b["preexisting_groups"], [])  # 是 A 加的，不算原有
        h.now[0] += 7 * 86400
        h.flows.revoke_expired()
        self.assertEqual(h.store.get(a["id"])["status"], t.REVOKED)
        self.assertIn(("lisi", "grp-oss-read"), h.executor.members)  # B 还没到期
        h.now[0] += 30 * 86400
        h.flows.revoke_expired()
        self.assertEqual(h.store.get(b["id"])["status"], t.REVOKED)
        self.assertNotIn(("lisi", "grp-oss-read"), h.executor.members)

    def test_truly_preexisting_group_still_kept_after_renewal(self):
        h = self.harness()
        h.executor.members.add(("lisi", "grp-oss-read"))
        self.grant(h, 7)
        h.now[0] += 86400
        b = self.grant(h, 30)
        self.assertEqual(b["preexisting_groups"], ["grp-oss-read"])
        h.now[0] += 60 * 86400
        h.flows.revoke_expired()
        self.assertIn(("lisi", "grp-oss-read"), h.executor.members)

    def test_partial_failure_retry_keeps_original_baseline(self):
        h = self.harness(TWO_GROUPS)
        h.executor.fail_on = ("add", "grp-b")
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 1})
        h.approve(ticket)
        self.assertEqual(h.flows.sync(ticket["id"], force=True)["status"], t.FAILED)
        h.executor.fail_on = None
        done = h.flows.execute(ticket["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(done["preexisting_groups"], [])
        h.now[0] += 2 * 86400
        h.flows.revoke_expired()
        self.assertEqual(h.executor.members, set())

    def test_unexpected_exception_marks_failed_not_executing(self):
        h = self.harness()

        def boom(user, group):
            raise ValueError("非 JSON 响应")

        h.executor.add_to_group = boom
        ticket = h.submit()
        h.approve(ticket)
        self.assertEqual(h.flows.sync(ticket["id"], force=True)["status"], t.FAILED)

    def test_huge_days_refused_even_without_template_limit(self):
        h = self.harness(UNLIMITED)
        with self.assertRaises(FlowError):
            h.submit(payload={"cloud_user": "lisi", "days": 10**9})
        self.assertEqual(
            h.submit(payload={"cloud_user": "lisi", "days": 3650})["status"], t.PENDING
        )

    def test_revoke_errors_isolated_and_not_repeated(self):
        h = self.harness()
        first = self.grant(h, 1)
        h.now[0] += 2 * 86400

        def broken(user, group):
            raise KeyError("unexpected")

        h.executor.remove_from_group = broken
        self.assertIn("失败", h.flows.revoke_expired()[0])
        h.flows.revoke_expired()
        events = [e["event"] for e in h.store.get(first["id"])["events"]]
        self.assertEqual(events.count("revoke_failed"), 1)

    def test_account_retry_after_partial_failure_does_not_recreate(self):
        h = self.harness()
        h.executor.fail_on = ("add", "grp-default")
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        self.assertEqual(h.flows.sync(ticket["id"], force=True)["status"], t.FAILED)
        h.executor.fail_on = None
        self.assertEqual(h.flows.execute(ticket["id"], actor="on_admin")["status"], t.DONE)
        creates = [a for a in h.executor.actions if a[0] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(h.links), 1)

    def test_link_failure_does_not_fail_account_and_is_flagged(self):
        h = self.harness()

        def refuse(email, account, ticket_id):
            from delivery.review import ReviewError

            raise ReviewError("账号已有人工记录", 409)

        h.flows._add_manual_link = refuse
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertIn("link_needed", [e["event"] for e in done["events"]])

    def test_link_uses_roster_email_not_submitted_email(self):
        h = self.harness()
        ticket = h.flows.submit(
            applicant=NEW,
            email="",  # 公司 IAM 模式下提交时拿不到邮箱
            template_id="new-user",
            payload={"username": "xinren"},
            reason="新同事入职开通控制台",
        )
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        self.assertEqual(h.links, [("new@wuji.tech", f"aliyun/{ACC}/xinren", ticket["id"])])

    def test_second_account_request_or_taken_username_refused(self):
        h = self.harness()
        h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        with self.assertRaises(FlowError):
            h.submit(applicant=NEW, template="new-user", payload={"username": "xinren2"})
        with self.assertRaises(FlowError):  # 大写、斜杠之类不安全字符
            h.submit(applicant=NEW, template="new-user", payload={"username": "a/b"})

    def test_reverted_approval_blocks_claims_and_sync(self):
        h = self.harness()
        cred = h.submit(template="dev-sts", payload={"hours": 1})
        h.approve(cred)
        h.flows.sync(cred["id"], force=True)
        h.feishu.instances[cred["approval"]["instance_code"]]["reverted"] = True
        with self.assertRaises(ApprovalError):
            h.flows.claim_credential(cred["id"], union_id="on_li")
        self.assertFalse([a for a in h.executor.actions if a[0] == "sts"])

        pending = h.submit()
        h.approve(pending)
        h.feishu.instances[pending["approval"]["instance_code"]]["reverted"] = True
        self.assertEqual(h.flows.sync(pending["id"], force=True)["status"], t.WITHDRAWN)
        self.assertEqual(h.executor.actions, [])

    def account_done(self, h):
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        return h.flows.sync(ticket["id"], force=True)

    def test_stale_password_claim_refused(self):
        h = self.harness()
        ticket = self.account_done(h)
        h.now[0] += 8 * 86400
        with self.assertRaises(FlowError):
            h.flows.claim_password(ticket["id"], union_id="on_new")
        self.assertNotIn(("password", "xinren"), h.executor.actions)

    def test_password_claim_refused_when_account_belongs_to_someone_else(self):
        h = self.harness()
        ticket = self.account_done(h)
        roster = _roster()
        other = people_mod.person_row(roster.people[0])
        other["accounts"] = [
            {"platform": "aliyun", "account": ACC, "name": "xinren", "status": "confirmed"}
        ]
        rows = [other, people_mod.person_row(roster.people[1])]
        h.flows._roster = lambda: people_mod.parse({"schema": people_mod.SCHEMA, "people": rows})
        with self.assertRaises(FlowError):
            h.flows.claim_password(ticket["id"], union_id="on_new")

    def test_password_reset_failure_can_be_retried(self):
        h = self.harness()
        ticket = self.account_done(h)

        def fail(user):
            raise ProvisionError("UpdateLoginProfile 超时")

        h.executor.reset_password = fail
        with self.assertRaises(ProvisionError):
            h.flows.claim_password(ticket["id"], union_id="on_new")
        del h.executor.reset_password
        _, pw = h.flows.claim_password(ticket["id"], union_id="on_new")
        self.assertEqual(pw, "Pw-Secret-123!")
        with self.assertRaises(FlowError):
            h.flows.claim_password(ticket["id"], union_id="on_new")

    def test_presentation_edit_does_not_block_but_group_edit_does(self):
        h = self.harness()
        ticket = h.submit()
        h.approve(ticket)
        h.templates["templates"][0]["title"] = "OSS 只读（训练数据）"
        self.assertEqual(h.flows.sync(ticket["id"], force=True)["status"], t.DONE)

    def test_stuck_executing_recovered(self):
        h = self.harness()
        ticket = h.submit()
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)  # DONE；再造一张卡在开通中的
        stuck = h.submit(payload={"cloud_user": "lisi", "days": 10})
        h.approve(stuck)
        h.store.update(stuck["id"], actor="feishu", expect=[t.PENDING], to=t.APPROVED, event="x")
        h.store.update(stuck["id"], actor="system", expect=[t.APPROVED], to=t.EXECUTING, event="y")
        with self.assertRaises(FlowError):  # 刚更新过，不急着判中断
            h.flows.recover_stuck(stuck["id"], actor="on_admin")
        h.now[0] += 3600
        self.assertEqual(len(h.flows.recover_stuck(actor="system")), 1)
        self.assertEqual(h.store.get(stuck["id"])["status"], t.FAILED)

    def test_employee_view_hides_raw_cloud_errors(self):
        from delivery.requests_api import Caller, ticket_view

        h = self.harness()
        h.executor.fail_on = ("add", "grp-oss-read")
        ticket = h.submit()
        h.approve(ticket)
        failed = h.flows.sync(ticket["id"], force=True)
        me = Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "", False)
        admin = Caller("on_admin", "管理员", "", "ou_a", "", True)
        self.assertNotIn("超时", json.dumps(ticket_view(failed, viewer=me), ensure_ascii=False))
        self.assertIn("超时", json.dumps(ticket_view(failed, viewer=admin), ensure_ascii=False))


class SweepTests(unittest.TestCase):
    def test_sweep_links_new_account_and_isolates_failures(self):
        import argparse
        import os
        from unittest import mock

        from delivery import cli_requests

        h = Harness()
        work = Path(tempfile.mkdtemp())
        ident = work / "identity"
        ident.mkdir()
        roster = _roster()
        rows = [people_mod.person_row(p) for p in roster.people]
        (ident / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (ident / "templates.json").write_text(json.dumps(TEMPLATES), encoding="utf-8")
        (ident / "approval.json").write_text(
            json.dumps({"approval_code": CONFIG.approval_code, "widgets": dict(CONFIG.widgets)})
        )
        store = t.TicketStore(str(ident / "tickets.json"))
        h.flows.store = h.store = store
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        # 一张核对不过的单子：同步报错，不能挡住后面的单子
        bad = h.submit()
        h.feishu.instances[bad["approval"]["instance_code"]]["approval_code"] = None
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/templates.json",
            approval="identity/approval.json",
            people="identity/people.json",
            proposal="identity/sso-map.proposal.json",
            manual="identity/manual-links.json",
        )

        class Approval(FeishuApproval):
            def __init__(self, config, token):
                super().__init__(config, token, transport=h.feishu)

        cwd = Path.cwd()
        os.chdir(work)
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {"DELIVERY_FEISHU_APP_ID": "cli_x", "DELIVERY_FEISHU_APP_SECRET": "s"},
                ),
                mock.patch("delivery.identity.directory.tenant_token", lambda *a: "tok"),
                mock.patch("delivery.approval.FeishuApproval", Approval),
                mock.patch("delivery.provision.executor_from_env", lambda p, a: h.executor),
                mock.patch("delivery.cli._git_toplevel", lambda path: None),
            ):
                code = cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 1)  # 坏单子算一个问题
        self.assertEqual(store.get(ticket["id"])["status"], t.DONE)
        manual = json.loads((ident / "manual-links.json").read_text(encoding="utf-8"))
        self.assertEqual(manual["links"]["new@wuji.tech"]["accounts"], [f"aliyun/{ACC}/xinren"])


class ReauditRegressionTests(unittest.TestCase):
    """复审发现的问题：每条锁一个回归测试。"""

    def harness(self, templates=TEMPLATES):
        h = Harness(templates)
        h.executor = MemberExecutor()
        return h

    def test_closed_partial_grant_is_not_preexisting_for_next_ticket(self):
        h = self.harness(TWO_GROUPS)
        h.executor.fail_on = ("add", "grp-b")
        first = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(first)
        self.assertEqual(h.flows.sync(first["id"], force=True)["status"], t.FAILED)
        h.flows.close(first["id"], actor="admin", note="不要了")
        h.executor.fail_on = None
        second = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(second)
        done = h.flows.sync(second["id"], force=True)
        self.assertEqual(done["preexisting_groups"], [])
        h.now[0] += 8 * 86400
        h.flows.revoke_expired()
        self.assertNotIn(("lisi", "grp-a"), h.executor.members)

    def account_done(self, h, applicant=NEW, username="xinren"):
        ticket = h.submit(applicant=applicant, template="new-user", payload={"username": username})
        h.approve(ticket)
        return h.flows.sync(ticket["id"], force=True)

    def test_username_created_by_platform_is_never_reused(self):
        h = self.harness()
        self.assertTrue(self.account_done(h)["user_created"])
        third = Applicant(union_id="on_third", name="第三人", open_id="ou_third")
        with self.assertRaises(FlowError):
            h.submit(applicant=third, template="new-user", payload={"username": "xinren"})

    def test_password_claim_refused_when_same_username_created_later(self):
        h = self.harness()
        ticket = self.account_done(h)
        path = h.dir / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        later = json.loads(json.dumps(data["tickets"][0]))
        later.update(id="REQ-LATER", done_at_ts=float(ticket["done_at_ts"]) + 60)
        later["applicant"] = {**later["applicant"], "union_id": "on_third"}
        data["tickets"].append(later)
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(FlowError):
            h.flows.claim_password(ticket["id"], union_id="on_new")
        self.assertNotIn(("password", "xinren"), h.executor.actions)

    def stall_approved(self, h, ticket):
        h.approve(ticket)
        h.store.update(
            ticket["id"],
            actor="feishu",
            expect=[t.PENDING],
            to=t.APPROVED,
            event="approval_approved",
        )
        h.now[0] += 31 * 60

    def test_resume_approved_executes_after_reverifying(self):
        h = self.harness()
        ticket = h.submit()
        self.stall_approved(h, ticket)
        h.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "PENDING"
        lines = h.flows.resume_approved()
        self.assertIn("失败", lines[0])  # 飞书里已经不是通过状态：不开通
        self.assertEqual(h.executor.actions, [])
        self.assertEqual(h.store.get(ticket["id"])["status"], t.FAILED)
        h.approve(ticket)
        # 转成执行失败后由管理员重试，重试时再核对一次审批
        self.assertEqual(h.flows.execute(ticket["id"], actor="admin")["status"], t.DONE)
        self.assertIn(("lisi", "grp-oss-read"), h.executor.members)

    def test_resume_approved_credential_becomes_claimable(self):
        h = self.harness()
        ticket = h.submit(template="dev-sts", payload={"hours": 2})
        self.stall_approved(h, ticket)
        h.flows.resume_approved()
        self.assertEqual(h.store.get(ticket["id"])["status"], t.CLAIMABLE)

    def test_resume_approved_ignores_fresh_tickets(self):
        h = self.harness()
        ticket = h.submit()
        h.approve(ticket)
        h.store.update(
            ticket["id"],
            actor="feishu",
            expect=[t.PENDING],
            to=t.APPROVED,
            event="approval_approved",
        )
        self.assertEqual(h.flows.resume_approved(), [])

    def test_sweep_still_revokes_when_feishu_token_fails(self):
        import argparse
        import os
        import time
        from unittest import mock

        from delivery import cli_requests

        h = self.harness()
        work = Path(tempfile.mkdtemp())
        ident = work / "identity"
        ident.mkdir()
        rows = [people_mod.person_row(p) for p in _roster().people]
        (ident / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (ident / "templates.json").write_text(json.dumps(TEMPLATES), encoding="utf-8")
        (ident / "approval.json").write_text(
            json.dumps({"approval_code": CONFIG.approval_code, "widgets": dict(CONFIG.widgets)})
        )
        h.store = t.TicketStore(str(ident / "tickets.json"), clock=lambda: h.now[0])
        h.flows.store = h.store
        h.now[0] = time.time() - 3 * 86400
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 1})
        h.approve(ticket)
        self.assertEqual(h.flows.sync(ticket["id"], force=True)["status"], t.DONE)
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/templates.json",
            approval="identity/approval.json",
            people="identity/people.json",
            proposal="identity/sso-map.proposal.json",
            manual="identity/manual-links.json",
        )

        def token_down(*a):
            raise OSError("token endpoint down")

        cwd = Path.cwd()
        os.chdir(work)
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {"DELIVERY_FEISHU_APP_ID": "cli_x", "DELIVERY_FEISHU_APP_SECRET": "s"},
                ),
                mock.patch("delivery.identity.directory.tenant_token", token_down),
                mock.patch("delivery.provision.executor_from_env", lambda p, a: h.executor),
                mock.patch("delivery.cli._git_toplevel", lambda path: None),
            ):
                code = cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 1)
        self.assertEqual(h.store.get(ticket["id"])["status"], t.REVOKED)
        self.assertNotIn(("lisi", "grp-oss-read"), h.executor.members)


class VolcanoExecutorTests(unittest.TestCase):
    ACCOUNT = "2000000001"

    def executor(self, handler):
        from delivery.clouds import volcano
        from delivery.provision import VolcanoExecutor

        calls = []

        def send(url, headers, data=None):
            import urllib.parse

            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            return handler(query)

        return VolcanoExecutor(self.ACCOUNT, volcano.Credentials("AK", "SK"), transport=send), calls

    def test_account_check_fails_closed_when_unconfirmed(self):
        for users in ([], [{"UserName": "x"}], [{"AccountId": "999"}]):
            ex, calls = self.executor(lambda q, u=users: (200, {"Result": {"UserMetadata": u}}))
            with self.assertRaises(ProvisionError):
                ex.in_group("lisi", "grp")
            self.assertEqual([c["Action"] for c in calls], ["ListUsers"])

    def test_account_from_trn_accepted(self):
        def handler(q):
            if q["Action"] == "ListUsers":
                return 200, {
                    "Result": {"UserMetadata": [{"Trn": f"trn:iam::{self.ACCOUNT}:user/a"}]}
                }
            return 200, {"Result": {"UserGroupMetadata": []}}

        ex, _ = self.executor(handler)
        self.assertFalse(ex.in_group("lisi", "grp"))

    def test_remove_with_wrong_group_name_is_an_error(self):
        def error(code):
            return 404, {"ResponseMetadata": {"Error": {"Code": code, "Message": "x"}}}

        def handler(q):
            if q["Action"] == "ListUsers":
                return 200, {"Result": {"UserMetadata": [{"AccountId": self.ACCOUNT}]}}
            if q["Action"] == "ListGroupsForUser":
                return 200, {"Result": {"UserGroupMetadata": [{"UserGroupName": "grp-typo"}]}}
            return error("EntityNotExist.UserGroup")

        ex, _ = self.executor(handler)
        from delivery.clouds import volcano

        with self.assertRaises(volcano.VolcanoError):
            ex.remove_from_group("lisi", "grp-typo")

        def gone(q):
            if q["Action"] == "ListUsers":
                return 200, {"Result": {"UserMetadata": [{"AccountId": self.ACCOUNT}]}}
            return error("EntityNotExist.User")

        ex, _ = self.executor(gone)
        ex.remove_from_group("lisi", "grp")  # 子账号已删：当作已回收
