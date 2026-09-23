"""「号建好了、登录名没写进公司 IAM」自动补写（`Flows.retry_iam_writes` + sweep）。

这一步失败的原因大多是临时的（IAM 接口抖、token 过期、那一刻网不通），
后果却是**那个人登不进云控制台**，而单子是绿的 DONE —— 不是 FAILED。
DONE 的单子在任何一页上都不显眼，也没有重试按钮（只有管理员手点那一个）。
线上真有一张单这么挂了两天（REQ-20260921）。

所以判据有三条：
  1. **挑得准**：只挑 `kind=account` + `DONE` + 建出了号 + 还没写属性的单子；
  2. **不重复写**：`iam_written` 守着，重跑安全（定时任务每轮都会跑一遍）；
  3. **一张失败不挡其余**：自动任务里一张脏单子挡住后面所有人，等于这个功能没上。

外加一条：**状态一动不动**。`DONE` 在状态机里只通向 `REVOKED`，
而且这一步补的是附加属性，不该改变「这张单办完了没有」的结论。

飞书、云、审批全部替换，不碰网络。
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from delivery import tickets as t
from delivery.approval import Applicant
from delivery.errors import DeliveryError

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import NEW, TEMPLATES, Harness

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule


class Base(unittest.TestCase):
    def harness(self, templates=TEMPLATES):
        return Harness(templates)

    def stranded(self, h, username="xinren", applicant=NEW):
        """开一张「号建好了、属性没写成」的单子：写入回调没接上那条真实路径。"""
        h.flows._write_iam = None
        ticket = h.flows.submit(
            applicant=applicant,
            email=f"{applicant.union_id}@wuji.tech",
            template_id="new-user",
            payload={"username": username},
            reason="新人入职，需要一个云账号",
        )
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])
        self.assertFalse(done.get("iam_written"))
        h.flows._write_iam = h._write_iam  # 现在接上了
        return done

    def events(self, h, tid):
        return [e["event"] for e in h.store.get(tid)["events"]]


class PickTests(Base):
    def test_a_stranded_account_ticket_is_pushed(self):
        h = self.harness()
        done = self.stranded(h)
        out = h.flows.retry_iam_writes()
        self.assertEqual(len(out), 1)
        self.assertIn(done["id"], out[0])
        self.assertTrue(h.store.get(done["id"])["iam_written"])
        self.assertEqual(len(h.iam_writes), 1)

    def test_the_login_name_actually_written_is_the_one_on_the_ticket(self):
        """写错名字比没写更糟：IAM 属性表里挂着一个不存在的登录名，
        SSO 把他映射到那个号上，而那个号根本没建出来。"""
        h = self.harness()
        done = self.stranded(h, username="xinren")
        h.flows.retry_iam_writes()
        union_id, platform, _account, username = h.iam_writes[0]
        self.assertEqual(username, "xinren")
        self.assertEqual(union_id, done["applicant"]["union_id"])
        self.assertEqual(platform, done["template"]["platform"])

    def test_the_ticket_status_does_not_move(self):
        h = self.harness()
        done = self.stranded(h)
        h.flows.retry_iam_writes()
        self.assertEqual(h.store.get(done["id"])["status"], t.DONE)

    def test_a_ticket_that_already_has_the_attribute_is_left_alone(self):
        h = self.harness()
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertTrue(done["iam_written"])
        self.assertEqual(h.flows.retry_iam_writes(), [])
        self.assertEqual(len(h.iam_writes), 1, "开通时写过的那一次，别再写第二次")

    def test_running_twice_writes_once(self):
        """定时任务每轮都跑。不幂等的话，IAM 那边每几分钟收到一次同样的写入。"""
        h = self.harness()
        self.stranded(h)
        h.flows.retry_iam_writes()
        self.assertEqual(h.flows.retry_iam_writes(), [])
        self.assertEqual(len(h.iam_writes), 1)

    def test_a_ticket_that_created_nothing_is_skipped(self):
        """没建出号就没有登录名可写。补写的话，IAM 里会多出一个不存在的账号 ——
        而 SSO 会照着它把人往一个不存在的号上映射。"""
        h = self.harness()
        done = self.stranded(h)
        path = h.dir / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["tickets"]:
            row.pop("user_created", None)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(h.flows.retry_iam_writes(), [])
        self.assertEqual(h.iam_writes, [])
        self.assertFalse(h.store.get(done["id"]).get("iam_written"))

    def test_a_permission_ticket_is_never_touched(self):
        """只有开账号的单子有登录名。权限单/凭证单没有 `username`，
        放进来的结果是往 IAM 里写空串。"""
        h = self.harness()
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 30})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        self.assertEqual(h.flows.retry_iam_writes(), [])
        self.assertEqual(h.iam_writes, [])

    def test_an_unfinished_ticket_is_never_touched(self):
        """还没开通完的单子由正常那条路负责。这里插一脚会和开通并发写同一个属性。"""
        h = self.harness()
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        self.assertEqual(h.store.get(ticket["id"])["status"], t.PENDING)
        self.assertEqual(h.flows.retry_iam_writes(), [])
        self.assertEqual(h.iam_writes, [])


class FailureTests(Base):
    def test_a_failing_push_is_reported_and_retried_next_round(self):
        """补不成只记一行，下一轮再试 —— 这正是它存在的理由（原因大多是临时的）。"""
        h = self.harness()
        done = self.stranded(h)
        h.iam_fail = "IT 接口挂了"
        out = h.flows.retry_iam_writes()
        self.assertEqual(len(out), 1)
        self.assertIn(done["id"], out[0])
        self.assertFalse(h.store.get(done["id"]).get("iam_written"))

        h.iam_fail = ""  # 接口好了
        self.assertEqual(len(h.flows.retry_iam_writes()), 1)
        self.assertTrue(h.store.get(done["id"])["iam_written"])

    def test_a_failing_push_does_not_fail_the_ticket(self):
        """判 FAILED 会让管理员以为号没建出来，跑去手工再建一个。"""
        h = self.harness()
        done = self.stranded(h)
        h.iam_fail = "IT 接口挂了"
        h.flows.retry_iam_writes()
        self.assertEqual(h.store.get(done["id"])["status"], t.DONE)

    def test_one_bad_ticket_does_not_block_the_others(self):
        """自动任务里一张脏单子挡住后面所有人 = 这个功能等于没上，
        而且没有任何地方会说「因为第 1 张挂了，后面 12 张没跑」。"""
        h = self.harness()
        first = self.stranded(h, username="xinren")
        h.flows._write_iam = None
        # **换一个人**：同一个人在同一个云账号下只能有一张开账号申请（flows.py:893）
        other = Applicant(union_id="on_two", name="另一个新人", open_id="ou_two")
        second = self.stranded(h, username="xinren2", applicant=other)

        def picky(union_id, platform, account, username):
            if username == "xinren":
                raise DeliveryError("这一张写不进去")
            h.iam_writes.append((union_id, platform, account, username))
            return ""

        h.flows._write_iam = picky
        out = h.flows.retry_iam_writes()
        self.assertEqual(len(out), 2)
        self.assertFalse(h.store.get(first["id"]).get("iam_written"))
        self.assertTrue(h.store.get(second["id"])["iam_written"])

    def test_an_unexpected_exception_is_caught_too(self):
        """写入回调是外部接线（`iam_sync.writer`），抛什么都有可能。
        漏接住的话整轮定时任务在这里断掉，连后面的到期回收都不跑了。"""
        h = self.harness()
        done = self.stranded(h)

        def boom(*_a, **_k):
            raise RuntimeError("socket 炸了")

        h.flows._write_iam = boom
        out = h.flows.retry_iam_writes()
        self.assertEqual(len(out), 1)
        self.assertFalse(h.store.get(done["id"]).get("iam_written"))

    def test_the_line_says_which_ticket_and_that_it_failed(self):
        """这行字会出现在定时任务日志里，是唯一的线索。"""
        h = self.harness()
        done = self.stranded(h)
        h.iam_fail = "IT 接口挂了"
        said = h.flows.retry_iam_writes()[0]
        self.assertIn(done["id"], said)
        self.assertIn("IAM", said)


class SweepWiringTests(unittest.TestCase):
    """写对了但没接进定时任务 = 线上还是靠人点按钮。"""

    def test_the_sweep_calls_it(self):
        import argparse
        import os
        import tempfile
        from pathlib import Path

        from delivery import cli_requests
        from delivery import flows as flows_mod
        from delivery import people as people_mod
        from delivery.approval import FeishuApproval

        from .test_delivery_access_requests import APPROVAL_JSON, _roster

        h = Harness()
        work = Path(tempfile.mkdtemp())
        ident = work / "identity"
        ident.mkdir()
        rows = [people_mod.person_row(p) for p in _roster().people]
        (ident / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (ident / "templates.json").write_text(json.dumps(TEMPLATES), encoding="utf-8")
        (ident / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
        (ident / "tickets.json").write_text(
            json.dumps({"schema": t.SCHEMA, "tickets": []}), encoding="utf-8"
        )
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

        called = []
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
                mock.patch.object(
                    flows_mod.Flows,
                    "retry_iam_writes",
                    lambda self: called.append(1) or [],
                ),
            ):
                cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        self.assertEqual(called, [1], "定时任务里没有补写这一步")


if __name__ == "__main__":
    unittest.main()
