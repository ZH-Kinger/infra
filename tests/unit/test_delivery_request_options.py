"""申请页的「每项对我是什么状态」与模板分类。

申请页靠 options() 告诉员工：这项能不能申请、是不是已经有了、是不是正在申请。
「已拥有」只是提示，不能挡住续期；快照缺失或没采全时一律按「未知」处理，不能误报已拥有。
数据全部虚构。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import tickets as t
from delivery.approval import FeishuApproval
from delivery.flows import Flows

from .test_delivery_access_requests import (
    CONFIG,
    LI,
    NEW,
    TEMPLATES,
    FakeExecutor,
    FakeFeishu,
    _roster,
)


def _templates(**category):
    data = json.loads(json.dumps(TEMPLATES))
    for tpl in data["templates"]:
        if tpl["id"] in category:
            tpl["category"] = category[tpl["id"]]
    return data


class Env:
    def __init__(self, groups=None):
        self.dir = Path(tempfile.mkdtemp())
        self.templates = _templates(**{"oss-read": "存储"})
        self.feishu = FakeFeishu()
        self.executor = FakeExecutor()
        self.groups = groups
        self.now = [1_800_000_000.0]
        self.store = t.TicketStore(str(self.dir / "tickets.json"), clock=lambda: self.now[0])
        approval = FeishuApproval(CONFIG, lambda: "tenant-token", transport=self.feishu)
        self.flows = Flows(
            store=self.store,
            catalog=lambda: catalog_mod.parse(self.templates),
            approval=lambda: approval,
            roster=_roster,
            executor=lambda platform, account: self.executor,
            current_groups=lambda platform, account, name: self.groups,
            clock=lambda: self.now[0],
        )

    def options(self, union_id="on_li"):
        return {o["id"]: o for o in self.flows.options(union_id)}

    def submit(self, template="oss-read", payload=None):
        payload = payload if payload is not None else {"cloud_user": "lisi", "days": 30}
        return self.flows.submit(
            applicant=LI,
            email="li.si@wuji.tech",
            template_id=template,
            payload=payload,
            reason="项目需要读取训练数据",
        )

    def approve(self, ticket):
        self.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"


class CategoryTests(unittest.TestCase):
    def test_category_is_optional_and_public(self):
        cat = catalog_mod.parse(_templates(**{"oss-read": "存储"}))
        self.assertEqual(cat.get("oss-read").public()["category"], "存储")
        self.assertEqual(cat.get("dev-sts").public()["category"], "")

    def test_policy_template_id_is_reserved(self):
        data = _templates()
        data["templates"][0]["id"] = "policy"
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse(data)

    def test_overlong_category_rejected(self):
        with self.assertRaises(catalog_mod.CatalogError):
            catalog_mod.parse(_templates(**{"oss-read": "很长" * 20}))

    def test_changing_category_does_not_block_approved_ticket(self):
        env = Env()
        ticket = env.submit()
        env.templates = _templates(**{"oss-read": "对象存储"})
        env.approve(ticket)
        done = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertIn(("add", "lisi", "grp-oss-read"), env.executor.actions)


class OptionStateTests(unittest.TestCase):
    def test_available_when_groups_unknown(self):
        opts = Env(groups=None).options()
        self.assertEqual(opts["oss-read"]["state"], "available")
        self.assertTrue(opts["oss-read"]["available"])
        self.assertEqual(opts["oss-read"]["cloud_user"], "lisi")

    def test_owned_when_snapshot_has_all_groups_case_insensitive(self):
        opts = Env(groups={"GRP-OSS-READ", "other"}).options()
        self.assertEqual(opts["oss-read"]["state"], "owned")
        # 已拥有仍可提交（续期）
        self.assertTrue(opts["oss-read"]["available"])
        self.assertIn("lisi", opts["oss-read"]["state_note"])

    def test_partial_groups_is_not_owned(self):
        env = Env(groups={"grp-oss-read"})
        env.templates["templates"][0]["groups"] = ["grp-oss-read", "grp-extra"]
        self.assertEqual(env.options()["oss-read"]["state"], "available")

    def test_owned_shows_expiry_of_active_grant(self):
        env = Env()
        ticket = env.submit()
        env.approve(ticket)
        env.flows.sync(ticket["id"], force=True)
        env.groups = {"grp-oss-read"}
        opt = env.options()["oss-read"]
        self.assertEqual(opt["state"], "owned")
        self.assertTrue(opt["expires_at"])

    def test_pending_ticket_blocks_duplicate_and_links_request(self):
        env = Env(groups={"grp-oss-read"})
        ticket = env.submit()
        opt = env.options()["oss-read"]
        self.assertEqual(opt["state"], "pending")
        self.assertEqual(opt["request_id"], ticket["id"])
        self.assertFalse(opt["available"])
        # 别人的申请不影响我
        self.assertNotEqual(env.options("on_new")["oss-read"]["state"], "pending")

    def test_claimable_credential_is_ready(self):
        env = Env()
        ticket = env.submit("dev-sts", {"hours": 2})
        env.approve(ticket)
        env.flows.sync(ticket["id"], force=True)
        opt = env.options()["dev-sts"]
        self.assertEqual(opt["state"], "ready")
        self.assertEqual(opt["request_id"], ticket["id"])

    def test_no_account_is_unavailable_even_with_open_ticket_elsewhere(self):
        opts = Env(groups={"grp-oss-read"}).options(NEW.union_id)
        self.assertEqual(opts["oss-read"]["state"], "unavailable")
        self.assertFalse(opts["oss-read"]["available"])
        self.assertTrue(opts["oss-read"]["unavailable_reason"])
        self.assertEqual(opts["new-user"]["state"], "available")

    def test_existing_account_is_owned_but_not_requestable(self):
        opt = Env().options()["new-user"]
        self.assertEqual(opt["state"], "owned")
        self.assertFalse(opt["available"])
        self.assertIn("lisi", opt["unavailable_reason"])

    def test_account_done_before_roster_refresh_is_owned(self):
        env = Env()
        ticket = env.flows.submit(
            applicant=NEW,
            email="new@wuji.tech",
            template_id="new-user",
            payload={"username": "xinren"},
            reason="新同事入职开通控制台",
        )
        env.approve(ticket)
        self.assertEqual(env.flows.sync(ticket["id"], force=True)["status"], t.DONE)
        opt = env.options(NEW.union_id)["new-user"]
        self.assertEqual(opt["state"], "owned")
        self.assertFalse(opt["available"])
        self.assertIn("xinren", opt["state_note"])

    def test_snapshot_error_is_unknown_not_500(self):
        env = Env()

        def broken(*a):
            raise ValueError("snapshot corrupted")

        env.flows._current_groups = broken
        self.assertEqual(env.options()["oss-read"]["state"], "available")

    def test_empty_union_id_sees_no_tickets(self):
        env = Env()
        env.submit()
        for opt in env.flows.options(""):
            self.assertNotIn(opt["state"], ("pending", "ready"))


if __name__ == "__main__":
    unittest.main()
