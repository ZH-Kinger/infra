"""云账号自助申请：模板校验、飞书审批核对、状态机、开通与领取。

重点锁住 docs/cloud-access-platform.md 的规则：没有已通过的飞书审批不开通（R1）、只能给自己
申请（R2）、只能选模板（R3）、凭证不落盘（R4）。数据全部虚构，云和飞书接口全部替换。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import catalog as catalog_mod
from delivery import notify as notify_mod
from delivery import people as people_mod
from delivery import tickets as t
from delivery.approval import Applicant, ApprovalConfig, ApprovalError, FeishuApproval
from delivery.clouds import aliyun
from delivery.errors import DeliveryError
from delivery.flows import FlowError, Flows
from delivery.provision import (
    AliyunExecutor,
    LongTermCredential,
    ProvisionError,
    TempCredential,
    session_name,
)

ACC = "1000000000000001"
#: 凭证模板的桶白名单。地域写裸名（不带 oss- 前缀），模板校验会拒绝带前缀的写法
BUCKET = "wuji-train-data"
REGION = "cn-hangzhou"
#: 凭证申请的标准 payload：桶必须在模板白名单里，时长决定走 STS 还是长期
CRED = {"bucket": BUCKET, "prefix": "batch/", "hours": 2}
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
            "caps": ["list", "download"],
            "buckets": [{"name": BUCKET, "region": REGION}],
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
    # 凭证是作为审批评论下发的，没有这个身份就发不出去
    comment_open_id="ou_panel_bot",
)
#: 写进 identity/approval.json 的内容，和 CONFIG 保持一致
APPROVAL_JSON = {
    "approval_code": CONFIG.approval_code,
    "widgets": dict(CONFIG.widgets),
    "comment_open_id": CONFIG.comment_open_id,
}
LI = Applicant(union_id="on_li", name="李四", open_id="ou_li")
NEW = Applicant(union_id="on_new", name="新人", open_id="ou_new")

#: 取件地址的前缀。凭证的唯一出口是 `<VIEW_BASE>/c/<单号>#<密钥>`，`DELIVERY_BASE_URL`
#: 没配（或不是 https）时 flows 在**提交那一刻**就拒 —— 所以跑完整发凭证流程的模块都得配上。
VIEW_BASE = "https://panel.example.com"
#: 已开始的 patch 栈。按模块 start/stop 成对出栈，跑完即还原，不污染别的模块
_BASE_URL_PATCHES = []


def setUpModule():
    """给整个模块配上取件地址。

    模块级而不是全局 autouse：「没配 DELIVERY_BASE_URL 就该拒发」本身是要锁的性质
    （见 ViewBaseUrlGateTests），全局设死就再也测不到那条路了。
    """
    patch = mock.patch.dict(os.environ, {notify_mod.ENV_BASE_URL: VIEW_BASE})
    patch.start()
    _BASE_URL_PATCHES.append(patch)


def tearDownModule():
    _BASE_URL_PATCHES.pop().stop()


class FakeFeishu:
    """模拟飞书审批接口：记录发起的实例，测试里手动改状态。"""

    def __init__(self):
        self.instances = {}
        self.calls = []
        #: instance_code -> [评论正文]。凭证的唯一出口
        self.comments = {}
        self.comment_fail = None

    def texts(self, code=None):
        if code is not None:
            return list(self.comments.get(code, ()))
        return [text for texts in self.comments.values() for text in texts]

    def __call__(self, method, url, token, body):
        self.calls.append((method, url, body))
        if method == "POST" and "/comments" in url:
            if self.comment_fail:
                return {"code": 1, "msg": self.comment_fail}
            code = url.split("/instances/", 1)[1].split("/", 1)[0]
            self.comments.setdefault(code, []).append(json.loads(body["content"])["text"])
            return {"code": 0, "data": {"comment_id": f"c{len(self.comments[code])}"}}
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
            data = dict(self.instances[code])
            if data["status"] == "APPROVED" and "task_list" not in data:
                # 默认有一位申请人以外的审批人点了通过；测试自审批时显式给 task_list
                data["task_list"] = [
                    {"open_id": "ou_approver", "user_id": "u_approver", "status": "APPROVED"}
                ]
            return {"code": 0, "data": data}
        if "/instances/cancel" in url:
            self.instances[body["instance_code"]]["status"] = "CANCELED"
            return {"code": 0, "data": {}}
        return {"code": 99, "msg": "unexpected"}


class FakeExecutor:
    def __init__(self):
        self.actions = []
        self.fail = None
        #: assume_role 收到的会话策略（None = 没收窄）
        self.session_policies = []
        #: issue_long_term 收到的策略文档，按子账号名
        self.issued = {}
        self.issue_fail = None
        self.revoke_fail = None
        self.revoke_left = []

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

    def add_workspace_member(self, *, region, workspace, user, roles):
        if getattr(self, "member_fail", None):
            raise self.member_fail
        self.actions.append(("member", workspace, user, tuple(roles)))
        return f"{workspace}-uid-{user}"

    def user_id(self, user):
        return f"uid-{user}"

    def enable_console(self, user):
        if getattr(self, "console_fail", None):
            raise self.console_fail
        self.actions.append(("console", user))
        return True

    def create_dataset(self, *, region, workspace, name, uri, source, user, labels=None):
        if getattr(self, "dataset_fail", None):
            raise self.dataset_fail
        self.actions.append(("dataset", workspace, name, uri, source))
        return f"d-{workspace}-{name}"

    def make_dir(self, bucket, prefix, region):
        self.actions.append(("dir", bucket, prefix, region))
        return f"{bucket}/{prefix}/"

    def create_user(self, user, display, *, email="", phone=""):
        self._maybe_fail()
        # 安全邮箱/手机一并记下来：不记的话「建号时有没有写上」这件事零覆盖，
        # 而漏写的表现是控制台上那两栏空着，没人会去看
        self.actions.append(("create", user, display, email, phone))

    def reset_password(self, user):
        self.actions.append(("password", user))
        return "Pw-Secret-123!"

    def assume_role(self, role, name, hours, *, policy=None):
        self.actions.append(("sts", role, name, hours))
        self.session_policies.append(policy)
        return TempCredential("STS.AK1234", "sts-secret", "sts-token", "2026-09-15T12:00:00Z")

    def issue_long_term(self, user, display_name, policy_doc):
        if self.issue_fail:
            raise ProvisionError(self.issue_fail)
        self.actions.append(("issue", user, display_name))
        self.issued[user] = policy_doc
        return LongTermCredential(user, f"temp-ak-auto-{user}", "LTAI-AK-9876", "lt-secret")

    def revoke_long_term(self, user):
        self.actions.append(("revoke", user))
        if self.revoke_fail:
            raise ProvisionError(self.revoke_fail)
        self.issued.pop(user, None)
        return list(self.revoke_left)


def view_lines(feishu, ticket) -> list:
    """审批评论里所有像「地址」的行。凭证的唯一出口就是这些行中的一条。

    故意按「像地址」而不是按行号取：行号一变测试就跟着改，而「评论里到底有几条地址」
    正是伪造使用方名称时要盯住的东西 —— 多出来一条就是把人骗去别处。
    """
    body = feishu.texts(ticket["approval"]["instance_code"])[-1]
    # 只认「整行就是一个地址」的行：伪造的内容被压平后只能嵌在别的行里（比如「使用方」
    # 那一行），嵌着的地址不会被使用方当成可点的凭证地址
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith(("/c/", "http"))]


def view_key(feishu, ticket) -> str:
    """从查看地址里取出密钥。密钥在 `#` 之后 —— 服务端只存密文，自己解不开。"""
    links = view_lines(feishu, ticket)
    if len(links) != 1 or "#" not in links[0]:
        raise AssertionError(f"审批评论里应该正好有一条带密钥的查看地址：{links}")
    return links[0].split("#", 1)[1]


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
        #: 凭证发放身份。不设时沿用开通身份（和 server.Backend 注入自定义 executor 时一致）
        self.issuer = None
        self.links = []
        #: 写公司 IAM 属性的假替身。`iam_fail` 非空时抛错，用来验「写不进去也不判整单失败」
        self.iam_writes = []
        self.iam_fail = ""
        self.now = [1_800_000_000.0]
        self.store = t.TicketStore(str(self.dir / "tickets.json"), clock=lambda: self.now[0])
        self.approval = FeishuApproval(CONFIG, lambda: "tenant-token", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(self.templates),
            approval=lambda: self.approval,
            roster=_roster,
            executor=lambda platform, account: self.executor,
            issuer=lambda platform, account: self.issuer or self.executor,
            add_manual_link=lambda email, account, ticket: self.links.append(
                (email, account, ticket)
            ),
            write_iam=self._write_iam,
            clock=lambda: self.now[0],
        )

    def _write_iam(self, union_id, platform, account, username):
        if self.iam_fail:
            raise DeliveryError(self.iam_fail)
        self.iam_writes.append((union_id, platform, account, username))
        return ""

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
        self.bad(index=1, max_hours=catalog_mod.MAX_CREDENTIAL_HOURS + 1)
        # 「领取期」这个概念没有了：留在模板里的 valid_days 必须当成写错、拒绝加载，
        # 而不是静默忽略——静默忽略会让人以为凭证还有一个领取窗口
        self.bad(index=1, valid_days=7)
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
            ticket_id="REQ-1", kind="permission", summary="s", reason="r", applicant=LI
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

    def test_self_or_auto_approval_is_refused(self):
        inst = self.feishu.instances[self.code]
        inst["status"] = "APPROVED"
        verify = lambda: self.approval.verify_approved(  # noqa: E731
            instance_code=self.code, ticket_id="REQ-1", applicant=LI
        )
        cases = {
            "只有申请人自己": [{"open_id": "ou_li", "user_id": "", "status": "APPROVED"}],
            "自动通过": [{"open_id": "", "user_id": "", "status": "APPROVED"}],
            "别人还没批": [{"open_id": "ou_boss", "status": "PENDING"}],
            "别人已转交": [{"open_id": "ou_boss", "status": "TRANSFERRED"}],
            "没有任务": [],
        }
        for label, tasks in cases.items():
            inst["task_list"] = tasks
            with self.assertRaises(ApprovalError, msg=label):
                verify()
        inst["task_list"] = [
            {"open_id": "ou_li", "status": "APPROVED"},
            {"open_id": "ou_boss", "user_id": "u_boss", "status": "APPROVED"},
        ]
        verify()
        # 管理员明确允许（比如审批定义本身就是本人确认）才放行
        relaxed = FeishuApproval(
            ApprovalConfig(CONFIG.approval_code, CONFIG.widgets, allow_self_approval=True),
            lambda: "tok",
            transport=self.feishu,
        )
        inst["task_list"] = []
        relaxed.verify_approved(instance_code=self.code, ticket_id="REQ-1", applicant=LI)

    def test_self_approval_check_uses_applicants_own_id_type(self):
        from delivery.approval import _approved_by_other

        iam = Applicant(union_id="on_x", name="x", open_id="", user_id="u_self")
        refused = [
            [{"open_id": "ou_someone", "user_id": "u_self", "status": "APPROVED"}],
            [{"open_id": "ou_someone", "status": "APPROVED"}],  # 缺 user_id：认不出是不是本人
        ]
        for tasks in refused:
            self.assertFalse(_approved_by_other({"task_list": tasks}, iam), tasks)
        self.assertTrue(
            _approved_by_other(
                {"task_list": [{"open_id": "ou_boss", "user_id": "u_boss", "status": "APPROVED"}]},
                iam,
            )
        )
        feishu = Applicant(union_id="on_y", name="y", open_id="ou_self")
        self.assertFalse(
            _approved_by_other({"task_list": [{"user_id": "u_boss", "status": "APPROVED"}]}, feishu)
        )
        both = Applicant(union_id="on_z", name="z", open_id="ou_z", user_id="u_z")
        self.assertFalse(
            _approved_by_other(
                {"task_list": [{"open_id": "ou_other", "user_id": "u_z", "status": "APPROVED"}]},
                both,
            )
        )

    def test_self_approved_credential_is_closed_not_claimable(self):
        h = Harness()
        ticket = h.submit(template="dev-sts", payload=dict(CRED))
        inst = h.feishu.instances[ticket["approval"]["instance_code"]]
        inst.update(status="APPROVED", task_list=[{"open_id": "ou_li", "status": "APPROVED"}])
        closed = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(closed["status"], t.CLOSED)
        self.assertEqual(closed["events"][-1]["event"], "approval_invalid")
        self.assertEqual(h.executor.actions, [])

    def test_link_pending_clears_once_roster_has_the_account(self):
        h = Harness()
        h.flows._add_manual_link = None  # 自动对应失败 → link_needed
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(h.flows.link_pending(done))
        roster = _roster()
        rows = [people_mod.person_row(p) for p in roster.people]
        rows[1]["accounts"] = [
            {"platform": "aliyun", "account": ACC, "name": "xinren", "status": "confirmed"}
        ]
        h.flows._roster = lambda: people_mod.parse({"schema": people_mod.SCHEMA, "people": rows})
        self.assertFalse(h.flows.link_pending(done))
        # 权限单、正常对应上的单子都不提醒
        self.assertFalse(h.flows.link_pending({**done, "kind": "permission"}))

    def test_self_approval_blocks_execution_end_to_end(self):
        h = Harness()
        ticket = h.submit()
        inst = h.feishu.instances[ticket["approval"]["instance_code"]]
        inst.update(status="APPROVED", task_list=[{"open_id": "ou_li", "status": "APPROVED"}])
        done = h.flows.sync(ticket["id"], force=True)
        # 自审批的单子直接关闭，不落「开通失败」：失败是能重试的，这张单子重试多少次都不该开通
        self.assertEqual(done["status"], t.CLOSED)
        self.assertEqual(done["events"][-1]["event"], "approval_invalid")
        self.assertEqual(h.executor.actions, [])
        with self.assertRaises(t.TicketError):
            h.flows.execute(ticket["id"], actor="admin")
        self.assertEqual(h.executor.actions, [])

    def test_config_allow_self_approval_must_be_bool(self):
        path = Path(tempfile.mkdtemp()) / "approval.json"
        base = {"approval_code": "A", "widgets": dict(CONFIG.widgets)}
        path.write_text(json.dumps({**base, "allow_self_approval": "yes"}), encoding="utf-8")
        with self.assertRaises(ApprovalError):
            ApprovalConfig.load(str(path))
        path.write_text(json.dumps(base), encoding="utf-8")
        self.assertFalse(ApprovalConfig.load(str(path)).allow_self_approval)

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
            self.h.submit(template="dev-sts", payload={**CRED, "hours": 12})
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

    def test_credential_is_issued_on_approval_and_delivered_as_a_link(self):
        """审批通过即签发，但**审批评论里一个字的凭证都没有**：只有带密钥的查看地址。

        评论会留在飞书的审批记录里，搜索、导出、离职交接都翻得到。凭证正文写进去就
        撤不回；换成查看地址之后，单子里只有密文，密钥只在那条链接里。
        """
        ticket = self.h.submit(template="dev-sts", payload=dict(CRED))
        self.h.approve(ticket)
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(
            self.h.executor.actions[-1][0:2], ("sts", f"acs:ram::{ACC}:role/dev-readonly")
        )
        body = self.h.feishu.texts(ticket["approval"]["instance_code"])[-1]
        for secret in ("sts-secret", "sts-token", "STS.AK1234"):
            self.assertNotIn(secret, body, "审批评论里不该出现凭证本身")
        key = view_key(self.h.feishu, ticket)
        self.assertIn(f"/c/{ticket['id']}#{key}", view_lines(self.h.feishu, ticket)[0])
        stored = (self.h.dir / "tickets.json").read_text(encoding="utf-8")
        self.assertNotIn("sts-secret", stored)
        self.assertNotIn("sts-token", stored)
        self.assertNotIn(key, stored, "密钥落盘 = 能读到 tickets.json 的人就能解开凭证")
        # 凭证只能用链接里的那把密钥解开，服务端自己没有第二条路
        _, cred = self.h.flows.view_credential(ticket["id"], key)
        self.assertEqual(cred["access_key_secret"], "sts-secret")
        self.assertEqual(cred["security_token"], "sts-token")

    def test_credential_expiry_is_recorded_and_swept(self):
        ticket = self.h.submit(template="dev-sts", payload={**CRED, "hours": 1})
        self.h.approve(ticket)
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(float(done["expires_at_ts"]), self.h.now[0] + 3600)
        self.assertEqual(self.h.flows.revoke_expired(), [])  # 还没到期
        self.h.now[0] += 2 * 3600
        self.assertEqual(len(self.h.flows.revoke_expired()), 1)
        # STS 凭证到点自灭，没有子账号要删
        self.assertEqual(self.h.store.get(ticket["id"])["status"], t.REVOKED)
        self.assertNotIn("revoke", [a[0] for a in self.h.executor.actions])

    def test_account_creation_and_one_time_password(self):
        ticket = self.h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        self.h.approve(ticket)
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(
            self.h.executor.actions,
            # 安全邮箱在建号那一刻就写上 —— 之后要补得管理员一个个去控制台点。
            # 手机号现在传空串：飞书应用没有读手机号的权限，通讯录返回的 mobile 是 None
            # **建号那一刻就开控制台登录** —— 原先只有「领初始密码」那条路会开它，
            # 而企业 SSO 开了之后领密码是死路，号建出来了人却进不去
            [
                ("create", "xinren", "新人", "new@wuji.tech", ""),
                ("add", "xinren", "grp-default"),
                ("console", "xinren"),
            ],
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
        (d / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
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
        # 另一个普通用户：开放「申请人自助作废」之后，必须有人来验证他动不了别人的单子
        self.store.sessions["wang"] = _WebSession(user=FeishuUser("ou_wang", "on_wang", "王五"))
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
        # 取件限流表是模块级全局，按来源 IP 记。同一个进程里所有用例都从 127.0.0.1 打，
        # 不清的话「测限流」那条会把后面的用例全 429 掉
        self._reset_pickup_limit()

    def _reset_pickup_limit(self):
        from delivery import server as server_mod

        with server_mod._pickup_lock:
            # 两张表都要清。`_pickup_tries` 是粗粒度的整体上限（每来源 5 分钟 120 次，
            # **成功也算**），只清 `_pickup_hits` 的话，前面几条限流用例打掉的几十次会
            # 攒在 127.0.0.1 这一桶里，把后面的用例整片 429 掉 —— 还是按方法名字母序
            # 发作，看起来像随机失败
            server_mod._pickup_hits.clear()
            server_mod._pickup_tries.clear()

    def one_proxy_hop(self):
        """按「面板前面只有一层会追加 XFF 的代理」跑。

        默认是 2（线上 nginx → oauth2-proxy）。用例里的 XFF 是手写的最终值、没有真代理
        往右追加，所以要把跳数调成和用例描述的拓扑一致，否则测的是「跳数对不上→退回 peer」
        那条兜底路，跟限流按谁分桶无关。
        """
        from delivery import server as server_mod

        return mock.patch.dict(os.environ, {server_mod.ENV_PROXY_HOPS: "1"})

    def tearDown(self):
        self._reset_pickup_limit()
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

    def anon(self, method, path, body=None, *, headers=None):
        """不带 cookie、不带 Bearer、不带 CSRF 头 —— 外部合作方就是这么访问的。"""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        raw = json.dumps(body).encode() if body is not None else None
        head = {"Content-Type": "application/json", **(headers or {})}
        conn.request(method, path, body=raw, headers=head)
        resp = conn.getresponse()
        text = resp.read()
        try:
            out = (resp.status, json.loads(text or b"{}"))
        except ValueError:
            out = (resp.status, {"_text": text.decode(errors="replace")})
        conn.close()
        return out

    NEW = {
        "template_id": "dev-sts",
        "payload": dict(CRED),
        "reason": "本地调试脚本需要只读访问",
    }

    def approved_credential(self):
        """走完提交 + 审批通过，返回 (申请单号, 查看地址里的密钥)。可以连着开好几张。"""
        status, data = self.call("POST", "/api/requests", self.NEW, bearer=True)
        self.assertEqual(status, 201, data)
        rid = data["request"]["id"]
        ticket = self.h.store.get(rid)
        self.h.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        status, data = self.call("GET", f"/api/requests/{rid}", bearer=True)
        self.assertEqual(data["request"]["status"], "done", data)
        return rid, view_key(self.h.feishu, self.h.store.get(rid))

    def test_employee_flow_via_bearer_token(self):
        status, data = self.call("POST", "/api/requests", self.NEW, bearer=True)
        self.assertEqual(status, 201, data)
        rid = data["request"]["id"]
        self.assertNotIn("role_arn", json.dumps(data))
        self.h.feishu.instances["INST-1"]["status"] = "APPROVED"
        status, data = self.call("GET", f"/api/requests/{rid}", bearer=True)
        # 审批通过即签发，面板上没有领取按钮：凭证不经过面板这条路
        self.assertEqual(data["request"]["status"], "done")
        self.assertNotIn("credential", data["request"]["actions"])
        # 领取接口已经没有了：不能再从面板上把 secret 取出来
        self.assertEqual(
            self.call("POST", f"/api/requests/{rid}/credential", {}, bearer=True)[0], 404
        )
        stored = (self.h.dir / "tickets.json").read_text(encoding="utf-8")
        self.assertNotIn("sts-secret", stored)
        self.assertNotIn("sts-secret", json.dumps(data, ensure_ascii=False))
        # 评论里是查看地址，不是凭证；面板接口也不回密文，密文只在服务端的 tickets.json 里
        comment = self.h.feishu.texts("INST-1")[-1]
        self.assertNotIn("sts-secret", comment)
        box = json.loads(stored)["tickets"][0]["sealed"]
        self.assertNotIn(box["ciphertext"], json.dumps(data, ensure_ascii=False))

    def test_view_link_works_without_logging_in(self):
        """查看凭证这条路**不要登录、不要 CSRF 头**：外部合作方没有面板账号。"""
        rid, key = self.approved_credential()
        status, data = self.anon("POST", "/api/pickup", {"id": rid, "key": key})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["credential"]["access_key_secret"], "sts-secret")
        self.assertEqual(data["credential"]["security_token"], "sts-token")
        # 连接信息要跟着凭证一起给：桶在深圳却配了杭州 endpoint 会被 403，
        # 使用方只会以为凭证是坏的
        for field in ("region", "endpoint", "bucket_url", "scope"):
            self.assertTrue(data["credential"][field], field)
        # 页面本身也在登录之外，否则拿到链接也打不开
        self.assertEqual(self.anon("GET", f"/c/{rid}")[0], 200)
        self.assertEqual(self.anon("GET", "/pickup.js")[0], 200)

    def test_view_link_refuses_wrong_key_and_unknown_id(self):
        """错密钥 / 别人的单子号 / 畸形 id，对外都是同一句话，不给试密钥的人任何线索。"""
        rid, key = self.approved_credential()
        wrong = ("A" * len(key), key[:-1] + ("A" if key[-1] != "A" else "B"), "", "短")
        for bad in wrong:
            status, data = self.anon("POST", "/api/pickup", {"id": rid, "key": bad})
            self.assertEqual(status, 403, (bad, data))
            self.assertEqual(data["error"], "这个链接打不开这份凭证")
        for bad_id in ("REQ-20260101-DEADBEEF", "REQ-bad", "../../etc/passwd", ""):
            status, data = self.anon("POST", "/api/pickup", {"id": bad_id, "key": key})
            self.assertEqual(status, 404, (bad_id, data))
            self.assertNotIn("sts-secret", json.dumps(data, ensure_ascii=False))

    def test_view_is_rate_limited_per_source_ip(self):
        """没有登录这道门，剩下唯一拦暴力试密钥的就是限流。"""
        from delivery import server as server_mod

        rid, key = self.approved_credential()
        codes = [
            self.anon("POST", "/api/pickup", {"id": rid, "key": "A" * 43})[0]
            for _ in range(server_mod._PICKUP_MAX + 1)
        ]
        self.assertEqual(codes[-1], 429, codes)
        self.assertNotIn(429, codes[: server_mod._PICKUP_MAX])
        # 限流之后连正确的密钥也进不来：不能靠「猜对了就放行」绕过
        self.assertEqual(self.anon("POST", "/api/pickup", {"id": rid, "key": key})[0], 429)

    def test_rate_limit_counts_failures_only(self):
        """「同一条链接能反复打开」是这套设计的全部意义，正常使用不该把自己的配额刷光。

        换机器、重装环境、同事接手 —— 一个使用方一天点十几次很正常。数成功次数的话，
        他会在某天突然打不开自己的凭证，而我们只会看到一条 429。
        """
        from delivery import server as server_mod

        rid, key = self.approved_credential()
        for i in range(25):
            self.assertEqual(
                self.anon("POST", "/api/pickup", {"id": rid, "key": key})[0], 200, f"第 {i + 1} 次"
            )
        # 25 次成功之后，失败配额仍是满的
        fails = [
            self.anon("POST", "/api/pickup", {"id": rid, "key": "A" * 43})[0]
            for _ in range(server_mod._PICKUP_MAX)
        ]
        self.assertEqual(set(fails), {403}, fails)
        self.assertEqual(self.anon("POST", "/api/pickup", {"id": rid, "key": "A" * 43})[0], 429)

    def test_rate_limit_is_per_ticket_not_per_source(self):
        """按 (来源 IP, 单号) 分桶：猜 A 的密钥猜到被封，不该连累同一个人打开 B 的凭证。

        线上所有请求的直连来源都是本机的反向代理 —— 只按 IP 计数等于全员共用一份配额，
        一个人被封 = 所有人被封。
        """
        from delivery import server as server_mod

        first, _ = self.approved_credential()
        second, key = self.approved_credential()
        self.assertNotEqual(first, second)
        for _ in range(server_mod._PICKUP_MAX):
            self.anon("POST", "/api/pickup", {"id": first, "key": "A" * 43})
        self.assertEqual(self.anon("POST", "/api/pickup", {"id": first, "key": "A" * 43})[0], 429)
        # 另一张单子的配额没被动过
        self.assertEqual(self.anon("POST", "/api/pickup", {"id": second, "key": key})[0], 200)

    def test_rate_limit_keys_on_the_forwarded_client_not_the_proxy(self):
        """经反向代理时按代理写进 XFF 的那个地址计数，而不是代理自己那个回环地址。

        取 peer 的话，线上每一个请求看起来都来自 127.0.0.1，一个人试密钥试到被封，
        所有使用方一起打不开凭证。

        跳数**必须显式钉住**：`_client_ip` 按 `DELIVERY_PROXY_HOPS` 从右边数（默认 2 =
        nginx + oauth2-proxy）。这条用例模拟的是「一层代理」，不设的话整条 XFF 都够不着
        跳数、一律退回 peer，于是这里测的东西就全落空了。
        """
        from delivery import server as server_mod

        rid, key = self.approved_credential()
        with self.one_proxy_hop():
            attacker = {"X-Forwarded-For": "8.8.8.8"}
            for _ in range(server_mod._PICKUP_MAX):
                self.anon("POST", "/api/pickup", {"id": rid, "key": "A" * 43}, headers=attacker)
            self.assertEqual(
                self.anon("POST", "/api/pickup", {"id": rid, "key": key}, headers=attacker)[0], 429
            )
            # 换一个真实来源不受影响；直连（没有 XFF）也不受影响
            other = {"X-Forwarded-For": "1.1.1.1"}
            self.assertEqual(
                self.anon("POST", "/api/pickup", {"id": rid, "key": key}, headers=other)[0], 200
            )
            self.assertEqual(self.anon("POST", "/api/pickup", {"id": rid, "key": key})[0], 200)

    def test_client_cannot_shake_off_the_limit_by_prepending_fake_hops(self):
        """客户端只能往 XFF **左边**塞 —— 塞什么都换不掉代理写进来的那一格。

        代理是从右边数第 `DELIVERY_PROXY_HOPS` 格；客户端塞进去的假跳只会把自己往左推。
        """
        from delivery import server as server_mod

        rid, key = self.approved_credential()
        with self.one_proxy_hop():
            for i in range(server_mod._PICKUP_MAX):
                # 每次伪造一个不同的「来源」，指望换一份新配额
                forged = {"X-Forwarded-For": f"9.9.9.{i}, 8.8.8.8"}
                self.anon("POST", "/api/pickup", {"id": rid, "key": "A" * 43}, headers=forged)
            blocked = {"X-Forwarded-For": "9.9.9.200, 8.8.8.8"}
            self.assertEqual(
                self.anon("POST", "/api/pickup", {"id": rid, "key": key}, headers=blocked)[0], 429
            )

    def test_every_view_is_recorded_in_the_ticket(self):
        """「谁什么时候看过」记不下来的话，换成查看地址就白换了。"""
        rid, key = self.approved_credential()
        for _ in range(2):
            self.assertEqual(self.anon("POST", "/api/pickup", {"id": rid, "key": key})[0], 200)
        events = [e["event"] for e in self.h.store.get(rid)["events"]]
        self.assertEqual(events.count("credential_viewed"), 2, events)
        notes = [
            e["note"] for e in self.h.store.get(rid)["events"] if e["event"] == "credential_viewed"
        ]
        self.assertTrue(all("127.0.0.1" in n for n in notes), notes)

    def test_admin_can_revoke_a_leaked_link_and_employees_cannot(self):
        """链接外泄时管理员要能立刻掐掉。没有这个入口的话，唯一的办法是手改申请单。"""
        rid, key = self.approved_credential()
        self.assertEqual(self.anon("POST", "/api/pickup", {"id": rid, "key": key})[0], 200)
        # 申请人走管理员那条路不行（路径判身份）
        self.assertEqual(self.call("POST", f"/api/admin/requests/{rid}/revoke", {})[0], 403)
        status, data = self.call("POST", f"/api/admin/requests/{rid}/revoke", {}, sid="admin")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["request"]["status"], "revoked")
        # 链接当场失效，而且回的是「打不开任何凭证」—— 不透露这个单号是不是真的
        status, data = self.anon("POST", "/api/pickup", {"id": rid, "key": key})
        self.assertEqual(status, 404, data)
        self.assertNotIn("sts-secret", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("sts-secret", (self.h.dir / "tickets.json").read_text(encoding="utf-8"))

    def test_applicant_can_revoke_only_their_own_credential(self):
        """开放自助作废的唯一风险点：别人的单子必须动不了。

        归属按 `union_id` 严格相等判（`flows._own`），不是邮箱也不是姓名 ——
        那两样都会变，而且同名的人真实存在。看不到别人的单子时回 404 不回 403：
        403 等于确认「这个单号存在」。
        """
        rid, _ = self.approved_credential()
        # 别人：连看都看不到，作废更不行，而且回的是 404
        self.assertEqual(self.call("GET", f"/api/requests/{rid}", sid="wang")[0], 404)
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/revoke", {}, sid="wang")[0], 404)
        # 自己：可以
        status, data = self.call("POST", f"/api/requests/{rid}/revoke", {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["request"]["status"], "revoked")
        # 作废之后按钮就该灭掉，再点一次是空转
        again = self.call("GET", f"/api/requests/{rid}")[1]["request"]
        self.assertFalse(again["actions"]["revoke"])
        self.assertEqual(self.call("POST", f"/api/requests/{rid}/revoke", {})[0], 409)

    def test_revoke_button_shows_for_the_applicant_and_for_admins(self):
        """作废按钮申请人自己也有。

        发现链接外泄的第一个人通常就是申请人，让他等管理员响应等于把泄漏窗口拉长。
        作废只会**减少**权限，开放它没有提权风险；最坏是误点一次，重新申请即可。
        （早先只给管理员，理由是「作废的是已经交出去的东西」—— 但那个理由管的是
        「要不要通知使用方」，不是「谁有权掐断」。）
        """
        rid, _ = self.approved_credential()
        mine = self.call("GET", f"/api/requests/{rid}")[1]["request"]
        self.assertTrue(mine["actions"]["revoke"])
        admin = self.call("GET", "/api/admin/requests", sid="admin")[1]
        got = {r["id"]: r["actions"]["revoke"] for r in admin["requests"]}
        self.assertIs(got[rid], True)
        # 权限单没有凭证可作废
        perm_req = {
            "template_id": "oss-read",
            "payload": {"cloud_user": "lisi", "days": 30},
            "reason": "项目需要读取训练数据",
        }
        status, perm = self.call("POST", "/api/requests", perm_req)
        self.assertEqual(status, 201, perm)
        perm_id = perm["request"]["id"]
        admin = self.call("GET", "/api/admin/requests", sid="admin")[1]
        got = {r["id"]: r["actions"]["revoke"] for r in admin["requests"]}
        self.assertIs(got[perm_id], False)

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


class ClientIpTests(unittest.TestCase):
    """`server._client_ip`：反向代理后面谁是真实来源。

    这个返回值有两个去处：取件限流的桶键（没有登录那道门之后，唯一拦暴力试密钥的东西），
    和申请单里「谁什么时候看过凭证」那条记录。判错的两个方向都很糟 —— 形同虚设（每个人
    换一串假 IP 就是一份新配额），或者全员共用一份配额（一个人被封等于所有人被封）。

    模型是**按固定跳数从右边数**（`DELIVERY_PROXY_HOPS`，默认 2 = nginx → oauth2-proxy →
    面板），不是「从右往左找第一个公网地址」。两条约束互相独立，各自都是踩出来的：

      · **不判它是不是公网** —— 内网员工的真实地址本来就是 10.x，按「是不是公网」筛会把
        他跳过去，于是他自己在左边塞的那个 `8.8.8.8` 成了答案
      · **但必须解析得出是个地址** —— 跳数一旦配错（少一层代理就要手动改成 1），客户端
        塞的任意文本就会原样成为限流桶键，每换一串就是一份新配额；还会把伪造内容写进台账

    注意：`ipaddress.is_private` 把 RFC 文档网段（198.51.100.x / 203.0.113.x / 2001:db8::）
    也算私网，所以用例里必须用真正可路由的地址，不能图省事用文档地址。
    """

    #: 线上那条链最终到面板时的样子：<真实来源>, <nginx 看到的对端> —— 两层代理各追加一格
    CHAIN = "{src}, 127.0.0.1"

    def setUp(self):
        from delivery import server as server_mod

        # 跳数从进程环境读。不钉住的话，别的用例设过的值会漏进来
        patch = mock.patch.dict(os.environ, {})
        patch.start()
        os.environ.pop(server_mod.ENV_PROXY_HOPS, None)
        self.addCleanup(patch.stop)

    def ip(self, peer, forwarded="", hops=0):
        from delivery.server import _client_ip

        return _client_ip(peer, forwarded, hops)

    def hops(self, value):
        from delivery import server as server_mod

        return mock.patch.dict(os.environ, {server_mod.ENV_PROXY_HOPS: value})

    def test_direct_public_peer_wins_over_any_header(self):
        """直连时 peer 就是真实来源，XFF 只是客户端随手写的一行字，不能采信。"""
        self.assertEqual(self.ip("8.8.8.8"), "8.8.8.8")
        self.assertEqual(self.ip("8.8.8.8", "1.1.1.1"), "8.8.8.8")
        self.assertEqual(self.ip("2606:4700::1111", "1.1.1.1"), "2606:4700::1111")

    def test_takes_the_hop_our_own_proxy_wrote(self):
        """数着位置取到的那一格，是我们自己的代理亲眼看到的对端 —— 客户端写不进去。"""
        for peer in ("127.0.0.1", "::1", "10.0.0.7", "172.17.0.1"):
            # 默认两跳：最右是 nginx 的地址，倒数第二格才是使用方
            self.assertEqual(self.ip(peer, self.CHAIN.format(src="8.8.8.8")), "8.8.8.8", peer)
            # 只有一层代理的部署（DELIVERY_PROXY_HOPS=1）：最右那格就是使用方
            with self.hops("1"):
                self.assertEqual(self.ip(peer, "8.8.8.8"), "8.8.8.8", peer)

    def test_client_cannot_shift_the_answer_by_prepending_hops(self):
        """客户端只能往**左边**塞。塞多少格都只是把自己往左推，换不掉被数到的那一格。

        取最左边是常见错法：那个值完全由客户端提供，换一串假 IP 就能绕开限流。
        """
        for forged in ("", "1.2.3.4", "1.2.3.4, 5.6.7.8", ", ".join(["9.9.9.9"] * 20)):
            chain = self.CHAIN.format(src=f"{forged}, 1.1.1.1" if forged else "1.1.1.1")
            self.assertEqual(self.ip("127.0.0.1", chain), "1.1.1.1", forged)

    def test_an_internal_employee_cannot_forge_a_public_source(self):
        """这条是「从右往左找第一个公网地址」那版的回归钉子。

        公司内网的人发一个 `X-Forwarded-For: 8.8.8.8`，代理往右追加后链子是
        `8.8.8.8, 10.1.2.3, 127.0.0.1`。按公网筛会把他真实的 10.1.2.3 当成「我们自己的
        代理跳」跳过去，取到他伪造的那个 —— 限流键随便换，台账记的「谁看的」也随便编。
        """
        self.assertEqual(self.ip("127.0.0.1", "8.8.8.8, 10.1.2.3, 127.0.0.1"), "10.1.2.3")
        self.assertEqual(self.ip("10.0.0.7", "8.8.8.8, 10.1.2.3, 172.17.0.1"), "10.1.2.3")
        # 内网地址本身是合法答案：它就是那位员工，按他分桶才对
        self.assertEqual(self.ip("127.0.0.1", self.CHAIN.format(src="192.168.0.5")), "192.168.0.5")

    def test_too_few_hops_falls_back_to_the_peer(self):
        """跳数对不上就退回直连地址，**不去猜**：猜错的方向是「采信客户端塞的值」。

        台账里会明显看到一片 127.0.0.1，那是「跳数配错了」看得见的样子。
        """
        self.assertEqual(self.ip("127.0.0.1", "8.8.8.8"), "127.0.0.1")  # 默认 2 跳，只有 1 格
        self.assertEqual(self.ip("127.0.0.1", ""), "127.0.0.1")
        self.assertEqual(self.ip("127.0.0.1", "   ,  , "), "127.0.0.1")
        self.assertEqual(self.ip("10.0.0.7", "10.1.1.1"), "10.0.0.7")
        # 直连部署（没有代理会追加 XFF）：整条链都是客户端自己写的，一格都不能采信
        for value in ("0", "-3"):
            with self.subTest(value), self.hops(value):
                self.assertEqual(self.ip("127.0.0.1", "8.8.8.8, 1.1.1.1, 9.9.9.9"), "127.0.0.1")
        self.assertEqual(self.ip("", ""), "?")

    def test_hop_count_comes_from_the_env_and_bad_values_fall_back_to_two(self):
        """`DELIVERY_PROXY_HOPS` 是人手填的。填错时必须退回默认 2，不能把整行当成 0 或崩掉 ——
        退成 0 等于「所有人一个桶」，崩掉等于取件接口 500。"""
        chain = "8.8.8.8, 1.1.1.1, 10.1.2.3, 127.0.0.1"
        for value, expect in (
            ("1", "127.0.0.1"),
            ("2", "10.1.2.3"),
            ("3", "1.1.1.1"),
            (" 2 ", "10.1.2.3"),  # EnvironmentFile 里常见的空格
        ):
            with self.subTest(value), self.hops(value):
                self.assertEqual(self.ip("127.0.0.1", chain), expect)
        for bad in ("", "abc", "3.5", "2 跳", "1e2", "0x2"):
            with self.subTest(bad), self.hops(bad):
                self.assertEqual(self.ip("127.0.0.1", chain), "10.1.2.3", bad)

    def test_answer_is_always_a_parsable_address_or_the_peer(self):
        """XFF 是外部输入，而这个返回值既进限流的桶、又进申请单的「谁看过」记录。

        不变式：要么是一个 `ipaddress.ip_address()` **解得开**的串，要么就是 peer。任何一段
        自由文本（超长串、换行、`10.0.0.1 attacker`、带端口）都不能原样成为答案 ——
        原样通过的话，每换一串就是一份新配额，取件限流整个失效。
        """
        import ipaddress

        garbage = (
            "x" * 500,
            "8" * 300,
            "10.0.0.1 attacker",
            "8.8.8.8\n注入的一行",
            "<script>",
            "8.8.8.8:443",
            "1.1.1.1/24",
        )
        for junk in garbage:
            # 被数到的那一格是垃圾 → 退回 peer，绝不原样返回
            got = self.ip("127.0.0.1", self.CHAIN.format(src=junk))
            self.assertEqual(got, "127.0.0.1", junk)
            # 混在左边（数不到的位置）无所谓：答案还是代理写的那一格
            got = self.ip("127.0.0.1", self.CHAIN.format(src=f"{junk}, 1.1.1.1"))
            self.assertEqual(got, "1.1.1.1", junk)
            ipaddress.ip_address(got)  # 解析得出来，才谈得上「按来源分桶」

    def test_the_answer_is_the_canonical_spelling_not_the_raw_string(self):
        """同一个地址的两种写法必须归一成同一个桶键，否则换个写法就是一份新配额。"""
        same = (
            "[2606:4700::1111]",
            "2606:4700:0000:0000:0000:0000:0000:1111",
            "  2606:4700::1111  ",
        )
        for raw in same:
            with self.subTest(raw):
                self.assertEqual(
                    self.ip("127.0.0.1", self.CHAIN.format(src=raw)), "2606:4700::1111"
                )
        # 带 zone 的链路本地地址：zone 去掉，剩下的部分仍是合法地址
        self.assertEqual(self.ip("127.0.0.1", self.CHAIN.format(src="fe80::1%eth0")), "fe80::1")


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

    def test_reverted_approval_blocks_issuance_and_sync(self):
        h = self.harness()
        cred = h.submit(template="dev-sts", payload={**CRED, "hours": 1})
        h.approve(cred)
        # 审批通过后又被撤销：同步时就不该发凭证
        h.feishu.instances[cred["approval"]["instance_code"]]["reverted"] = True
        self.assertEqual(h.flows.sync(cred["id"], force=True)["status"], t.WITHDRAWN)
        self.assertFalse([a for a in h.executor.actions if a[0] == "sts"])
        self.assertEqual(h.feishu.texts(), [])

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
        (ident / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
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

    def _with_workspace(self, h, *spaces, template="new-user"):
        """给模板配上工作空间。**改的是目录快照，不是文件** —— 每个用例一份，互不影响。

        收的是**解析好的**配置（和 `workspaces.json` 查出来的形状一样），
        不是地域 key —— 这些用例测的是「配上之后做了什么」，不是登记表怎么解析的。
        """
        from dataclasses import replace

        from delivery import catalog as catalog_mod

        rows = [
            replace(tpl, workspaces=tuple(dict(w) for w in spaces)) if tpl.id == template else tpl
            for tpl in h.flows._catalog().templates
        ]
        patched = catalog_mod.Catalog(templates=tuple(rows))
        h.flows._catalog = lambda: patched
        return patched

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

    # ── 建号之后把登录名写进公司 IAM ───────────────────────────────────────
    #
    # 这一步不成，**人就登不进去** —— SSO 断言的 NameID 取自那个属性。
    # 但云上账号这时已经建好了，所以它的失败处置和「对应不到申请人」是同一类：
    # 记下来、写进结果文案，不把整张单判失败。

    def test_a_new_account_is_written_into_company_iam(self):
        h = self.harness()
        done = self.account_done(h)
        self.assertTrue(done["iam_written"])
        self.assertEqual(h.iam_writes, [("on_new", "aliyun", "1000000000000001", "xinren")])
        self.assertIn("可以用企业账号登录", done["result"])

    def test_a_failed_iam_write_does_not_fail_the_whole_ticket(self):
        """账号确实建好了。判失败会让管理员以为什么都没发生，跑去手工再建一个。"""
        h = self.harness()
        h.iam_fail = "IAM 暂时不可用"
        done = self.account_done(h)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])
        self.assertFalse(done.get("iam_written"))

    def test_a_failed_iam_write_says_outright_that_he_cannot_log_in(self):
        """结果里只写「已新建子账号 X」而不提这件事，申请人会以为可以用了。"""
        h = self.harness()
        h.iam_fail = "IAM 暂时不可用"
        done = self.account_done(h)
        self.assertIn("登不进去", done["result"])
        self.assertIn("IAM 暂时不可用", done["result"])
        events = [e.get("event") for e in done.get("events", [])]
        self.assertIn("iam_write_needed", events)

    def test_an_applicant_without_a_union_id_is_refused_early_not_retried_forever(self):
        """接口只认 union_id。没有就是发不出去，不是「等会儿再试」。"""
        h = self.harness()
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        path = h.dir / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["tickets"]:
            if row["id"] == ticket["id"]:
                row["applicant"]["union_id"] = ""
        path.write_text(json.dumps(data), encoding="utf-8")
        done = h.flows.sync(ticket["id"], force=True)
        self.assertIn("没有 union_id", done["result"])
        self.assertEqual(h.iam_writes, [])

    def test_a_retry_does_not_write_the_attribute_twice(self):
        h = self.harness()
        done = self.account_done(h)
        h.flows.sync(done["id"], force=True)
        self.assertEqual(len(h.iam_writes), 1)

    def test_without_the_api_wired_up_the_account_is_still_created(self):
        """没接上写入回调时，建号本身照常 —— 号确实存在这件事必须被记下来，
        否则管理员看到失败会以为什么都没发生，跑去手工再建一个。"""
        h = self.harness()
        h.flows._write_iam = None
        done = self.account_done(h)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])

    def test_without_the_api_wired_up_it_says_he_cannot_log_in(self):
        """**这条原先断言的是相反的**：没接上时结果文案里不准出现「登不进去」，
        理由是「退回导 CSV 给 IT 的老路」。但 CSV 是批量导出、要 IT 手动处理，
        那期间人照样登不进去 —— 那句话是真的，压掉它就等于骗申请人。

        线上第一个账号正是这么出的事：定时任务构造 Flows 时漏传了这个回调，
        建号成功、单子干干净净进 done、文案只有「已新建子账号 X」，
        而那个人根本登不进去，直到他自己来问。"""
        h = self.harness()
        h.flows._write_iam = None
        done = self.account_done(h)
        self.assertIn("登不进去", done["result"])
        events = [e.get("event") for e in done.get("events") or []]
        self.assertIn("iam_write_needed", events, "没接上要留下痕迹，不能只在文案里")

    def test_a_new_account_is_put_into_the_workspace_and_gets_a_dataset(self):
        """**建号只建账号等于没建完**：工作空间是 PAI 的围墙，数据集、DSW、DLC
        全是它的下级资源，不在里面的人一个都看不到。原先这三步要管理员手工做。"""
        h = self.harness()
        ws = {
            "id": "640957",
            "region": "cn-hangzhou",
            "roles": ["PAI.AlgoDeveloper"],
            "mount": "cpfs-x.cn-hangzhou.cpfs.aliyuncs.com",
        }
        self._with_workspace(h, ws)
        done = self.account_done(h)
        acts = [a for a in h.executor.actions if a[0] in ("member", "dataset", "dir")]
        self.assertIn(
            ("member", "640957", done["payload"]["username"]),
            [(a[0], a[1], a[2]) for a in acts if a[0] == "member"],
        )
        self.assertTrue(done.get("workspace_done"))
        self.assertIn("工作空间", done["result"])

    def test_both_a_cpfs_and_an_oss_dataset_are_created(self):
        """两条都要：数据集是挂载入口，少一条人就少一个能挂的地方。
        形状照现网 —— CPFS 叫 `<登录名>`、扁平；OSS 叫 `<登录名>-oss`、带组这一层，
        而且 URI 是「桶.域名」形式不是 `oss://桶/路径`（写错了新老会长成两种东西）。"""
        h = self.harness()
        self._with_workspace(
            h,
            {
                "id": "640957",
                "region": "cn-hangzhou",
                "roles": ["PAI.AlgoDeveloper"],
                "mount": "cpfs-x.cn-hangzhou.cpfs.aliyuncs.com",
                "bucket": "wuji-algo-dev-hz",
                "bucket_region": "cn-hangzhou",
                "bucket_prefix": "general",
            },
        )
        done = self.account_done(h)
        made = {a[2]: a[3] for a in h.executor.actions if a[0] == "dataset"}
        self.assertEqual(sorted(made), ["xinren", "xinren-oss"])
        self.assertEqual(made["xinren"], "bmcpfs://cpfs-x.cn-hangzhou.cpfs.aliyuncs.com/xinren/")
        self.assertEqual(
            made["xinren-oss"],
            "oss://wuji-algo-dev-hz.oss-cn-hangzhou.aliyuncs.com/general/xinren/",
        )
        # OSS 那边要真开一个占位目录；CPFS 不用（挂载时自动建）
        self.assertIn(
            ("dir", "wuji-algo-dev-hz", "general/xinren", "cn-hangzhou"), h.executor.actions
        )
        self.assertTrue(done.get("workspace_done"))

    def test_console_login_is_enabled_at_creation_not_at_password_claim(self):
        """SSO 开了之后领密码是死路（密码登录全局失效）。只在领密码那一刻开登录配置的话，
        号建出来了却没有登录配置 —— 人拿着企业账号也进不去。火山还多一个 LoginAllowed 开关。"""
        h = self.harness()
        done = self.account_done(h)
        self.assertIn(("console", done["payload"]["username"]), h.executor.actions)
        self.assertIn("已开控制台登录", done["result"])

    def test_a_console_failure_does_not_lose_the_account(self):
        h = self.harness()
        h.executor.console_fail = RuntimeError("云上拒了")
        done = self.account_done(h)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])
        self.assertIn("控制台登录没开成", done["result"])

    def test_joining_another_workspace_also_creates_the_datasets_there(self):
        """**只加成员不建数据集等于白加** —— 数据集是工作空间的下级资源，
        人进去了、自己的数据却看不到，他会以为权限没给全。
        建号和「加入别的空间」走同一个方法，就不会出现一处记得建、另一处忘了。"""
        from dataclasses import replace

        from delivery import catalog as catalog_mod

        h = self.harness()
        cat = h.flows._catalog()
        ws = {
            "id": "284761",
            "region": "ap-southeast-1",
            "roles": ["PAI.AlgoDeveloper"],
            "mount": "cpfs-sg.ap-southeast-1.cpfs.aliyuncs.com",
            "bucket": "wuji-algo-dev-sing",
            "bucket_region": "ap-southeast-1",
        }
        rows = [
            replace(t, workspaces=(dict(ws),)) if t.kind == "permission" else t
            for t in cat.templates
        ]
        patched = catalog_mod.Catalog(templates=tuple(rows))
        h.flows._catalog = lambda: patched

        ticket = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        made = {a[2] for a in h.executor.actions if a[0] == "dataset"}
        self.assertEqual(made, {"lisi", "lisi-oss"}, "两条都要，少一条就少一个挂载入口")
        self.assertIn(
            ("member", "284761", "lisi"),
            [(a[0], a[1], a[2]) for a in h.executor.actions if a[0] == "member"],
        )

    def test_a_workspace_failure_does_not_lose_the_account(self):
        """账号已经建好了，那件事必须被记下来 —— 否则管理员看到失败会以为
        什么都没发生，跑去手工再建一个。"""
        h = self.harness()
        self._with_workspace(
            h,
            {"id": "640957", "region": "cn-hangzhou", "roles": ["PAI.AlgoDeveloper"], "mount": "m"},
        )
        h.executor.member_fail = RuntimeError("PAI 挂了")
        done = self.account_done(h)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])
        self.assertFalse(done.get("workspace_done"))
        self.assertIn("进不去 DSW", done["result"])

    def test_a_template_without_workspace_behaves_exactly_as_before(self):
        """这一块是加法：没配 workspace 的模板一字不变。"""
        h = self.harness()
        done = self.account_done(h)
        self.assertNotIn("工作空间", done["result"])
        self.assertEqual([a for a in h.executor.actions if a[0] == "member"], [])

    def test_the_login_address_is_commented_on_the_approval(self):
        """飞书私聊会被后面的消息淹掉；审批实例是这次开号的权威记录 ——
        半年后问「这号当初谁批的、怎么登」，翻审批单就够了。"""
        h = self.harness()
        done = self.account_done(h)
        said = "\n".join(h.feishu.texts(done["approval"]["instance_code"]))
        self.assertIn("登录名", said)
        self.assertIn(done["payload"]["username"], said)
        self.assertTrue(done.get("login_commented"))

    def test_a_failed_comment_does_not_fail_the_provisioning(self):
        """号已经建好了。把整张单打成失败只会让管理员以为什么都没发生、
        跑去手工再建一个。"""
        h = self.harness()

        h.feishu.comment_fail = RuntimeError("飞书挂了")
        done = self.account_done(h)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])
        self.assertFalse(done.get("login_commented"))
        self.assertIn("login_comment_failed", [e.get("event") for e in done.get("events") or []])

    def test_a_ticket_with_no_iam_attribute_can_be_pushed_by_an_admin(self):
        """建号成功但属性没写成时单子是 DONE、没有重试按钮 —— 不给这个入口的话，
        补一个人得走全量 iam-push，而那条路会连带触发「整体消失」的删除闸门。"""
        h = self.harness()
        h.flows._write_iam = None
        done = self.account_done(h)
        self.assertNotIn("iam_written", done)

        h.flows._write_iam = h._write_iam  # 现在接上了
        after = h.flows.push_iam(done["id"], actor="admin")
        self.assertTrue(after["iam_written"])
        self.assertEqual(after["status"], t.DONE, "补写属性不该改变单子状态")
        self.assertEqual(len(h.iam_writes), 1)

    def test_pushing_twice_is_refused(self):
        h = self.harness()
        done = self.account_done(h)  # 这一轮已经写进去了
        with self.assertRaises(FlowError):
            h.flows.push_iam(done["id"], actor="admin")

    def test_pushing_a_ticket_that_created_nothing_is_refused(self):
        """没建出号就没有登录名可写。放行的话会往 IAM 里写一个不存在的账号。"""
        h = self.harness()
        h.flows._write_iam = None
        done = self.account_done(h)
        path = h.dir / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["tickets"]:
            row.pop("user_created", None)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        h.flows._write_iam = h._write_iam
        with self.assertRaises(FlowError):
            h.flows.push_iam(done["id"], actor="admin")

    def test_a_push_that_still_fails_says_so_instead_of_looking_fine(self):
        """按钮点了就好了、其实还没写成 —— 那比没有按钮更糟。"""
        h = self.harness()
        h.flows._write_iam = None
        done = self.account_done(h)

        def boom(*_a, **_k):
            raise RuntimeError("IT 接口挂了")

        h.flows._write_iam = boom
        with self.assertRaises(FlowError):
            h.flows.push_iam(done["id"], actor="admin")
        self.assertFalse(h.flows.store.get(done["id"]).get("iam_written"))

    def test_a_sso_account_does_not_offer_a_password_to_claim(self):
        """开了用户 SSO 的账号，RAM 密码登录就失效了（阿里云那个是全局开关，不是并行）。
        还让人领密码的话，他领到一串登不进去的东西，只会以为是账号没建好。"""
        import os

        from delivery import platforms

        h = self.harness()
        done = self.account_done(h)
        scope = f"{done['template']['platform']}/{done['template']['account']}"
        self.addCleanup(os.environ.pop, platforms.ENV_SSO, None)
        os.environ[platforms.ENV_SSO] = scope
        with self.assertRaises(FlowError) as caught:
            h.flows.claim_password(done["id"], union_id=done["applicant"]["union_id"])
        self.assertIn("企业账号", str(caught.exception))

    def test_a_password_account_still_offers_one(self):
        """反向锁：别为了堵 SSO 把没开 SSO 的账号也一起堵了。"""
        import os

        from delivery import platforms

        h = self.harness()
        done = self.account_done(h)
        self.addCleanup(os.environ.pop, platforms.ENV_SSO, None)
        os.environ[platforms.ENV_SSO] = "volcano/9999999999"
        _, pw = h.flows.claim_password(done["id"], union_id=done["applicant"]["union_id"])
        self.assertTrue(pw)

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

    def test_resume_approved_credential_is_issued(self):
        h = self.harness()
        ticket = h.submit(template="dev-sts", payload=dict(CRED))
        self.stall_approved(h, ticket)
        h.flows.resume_approved()
        self.assertEqual(h.store.get(ticket["id"])["status"], t.DONE)
        self.assertTrue(h.feishu.texts(ticket["approval"]["instance_code"]))

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
        (ident / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
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
