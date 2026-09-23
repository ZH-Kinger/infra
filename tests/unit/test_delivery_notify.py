"""申请状态通知：卡片内容、发送错误、Flows 在状态变化后通知、到期提醒、管理员告警。

重点：通知发不出去不影响申请单；员工收到的消息不带错误原文；用户可控文本只进 plain_text。
数据全部虚构，飞书接口全部替换。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import notify as n
from delivery import people as people_mod
from delivery import tickets as t

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import (
    APPROVAL_JSON,
    CRED,
    TEMPLATES,
    Harness,
    MemberExecutor,
    _roster,
)

#: 取件地址（DELIVERY_BASE_URL）是凭证的唯一出口：没配 flows 在提交那一刻就拒。
#: 模块级设、跑完还原 —— 不用全局 autouse，否则「没配就该拒」那条路再也测不到。
setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

BASE = "https://panel.example.com"


def _ticket(**over):
    base = {
        "id": "REQ-20260101-ABCDEF12",
        "kind": "permission",
        "template": {"id": "oss-read", "title": "OSS 只读", "platform": "aliyun"},
        "payload": {"cloud_user": "lisi", "days": 30},
        "applicant": {"union_id": "on_li", "name": "李四", "open_id": "ou_li"},
        "expires_at": "2026-02-01T10:00:00+08:00",
        "expires_at_ts": 1_769_911_200.0,
        "valid_until": "2026-01-08T10:00:00+08:00",
        "events": [],
    }
    base.update(over)
    return base


def _texts(card):
    out = [card["header"]["title"]["content"]]
    for el in card["elements"]:
        if el["tag"] == "div":
            out.append(el["text"]["content"])
    return out


def _tags(value):
    """卡片里所有文本节点的 tag：只允许 plain_text。"""
    if isinstance(value, dict):
        if "content" in value and "tag" in value:
            yield value["tag"]
        for v in value.values():
            yield from _tags(v)
    elif isinstance(value, list):
        for v in value:
            yield from _tags(v)


class CardTests(unittest.TestCase):
    def test_each_event_has_colour_title_and_plain_text(self):
        colours = {
            "done": "green",
            "failed": "red",
            "rejected": "orange",
            "withdrawn": "grey",
            "expiring": "orange",
            "revoked": "grey",
        }
        for event, colour in colours.items():
            card = n.build_card(event, _ticket(), base_url=BASE, now=1_769_700_000.0)
            self.assertEqual(card["header"]["template"], colour, event)
            self.assertIn("OSS 只读", card["header"]["title"]["content"], event)
            self.assertEqual(set(_tags(card)), {"plain_text"}, event)
            self.assertTrue(2 <= len(card["elements"]) <= 6, event)

    def test_done_mentions_expiry_and_account_password(self):
        texts = _texts(n.build_card("done", _ticket(), base_url=BASE))
        self.assertIn("02-01 10:00 到期，到期自动收回。", texts)
        tpl = {"id": "new-user", "title": "新员工子账号", "platform": "aliyun"}
        account = _ticket(
            kind="account",
            payload={"username": "xinren"},
            expires_at="",
            template={**tpl, "console_login": True},
        )
        self.assertTrue(
            any("初始密码" in x for x in _texts(n.build_card("done", account, base_url=BASE)))
        )
        no_console = _ticket(
            kind="account", payload={"username": "xinren"}, expires_at="", template=tpl
        )
        self.assertFalse(
            any("初始密码" in x for x in _texts(n.build_card("done", no_console, base_url=BASE)))
        )

    def test_expiring_counts_days(self):
        ticket = _ticket(expires_at_ts=1_000_000 + 2.5 * 86400)
        texts = _texts(n.build_card("expiring", ticket, base_url=BASE, now=1_000_000))
        self.assertTrue(texts[1].startswith("3 天后"), texts)

    def test_failed_never_includes_raw_error(self):
        ticket = _ticket(
            events=[{"event": "execute_failed", "note": "AccessKeyId=LTAIsecret NoPermission"}]
        )
        card = json.dumps(n.build_card("failed", ticket, base_url=BASE), ensure_ascii=False)
        self.assertNotIn("LTAI", card)
        self.assertNotIn("NoPermission", card)
        self.assertIn("管理员会处理", card)

    def test_policy_title_and_truncation(self):
        policies = [{"name": "A" * 100, "type": "System"}, {"name": "B", "type": "System"}]
        ticket = _ticket(template={"id": "policy", "platform": "aliyun", "policies": policies})
        title = n.build_card("done", ticket, base_url=BASE)["header"]["title"]["content"]
        self.assertLessEqual(len(title), len("已开通：") + 60)
        self.assertTrue(title.endswith("…"))
        short = _ticket(template={"id": "policy", "policies": [{"name": "X"}, {"name": "Y"}]})
        self.assertIn(
            "X 等 2 项", n.build_card("done", short, base_url=BASE)["header"]["title"]["content"]
        )

    def test_markdown_in_title_stays_plain(self):
        ticket = _ticket(template={"title": "**点我** [x](https://evil.example)"})
        card = n.build_card("done", ticket, base_url=BASE)
        self.assertEqual(set(_tags(card)), {"plain_text"})

    def test_button_only_for_safe_base_url(self):
        def button(base):
            card = n.build_card("done", _ticket(), base_url=base)
            actions = [el for el in card["elements"] if el["tag"] == "action"]
            return actions[0]["actions"][0]["url"] if actions else ""

        self.assertEqual(button(BASE), f"{BASE}/#request=REQ-20260101-ABCDEF12")
        self.assertEqual(
            button("http://127.0.0.1:8765/"), "http://127.0.0.1:8765/#request=REQ-20260101-ABCDEF12"
        )
        for bad in (
            "http://panel.example.com",
            "javascript:alert(1)",
            "",
            "https://x.example/?a=1",
            'https://x"y',
        ):
            self.assertEqual(button(bad), "", bad)
        weird = n.build_card("done", _ticket(id="A&B#C"), base_url=BASE)
        url = [el for el in weird["elements"] if el["tag"] == "action"][0]["actions"][0]["url"]
        self.assertTrue(url.endswith("#request=A%26B%23C"))

    def test_unknown_event_rejected(self):
        with self.assertRaises(n.NotifyError):
            n.build_card("nope", _ticket(), base_url=BASE)


class SendTests(unittest.TestCase):
    def test_sends_interactive_card_to_open_id(self):
        calls = []

        def transport(method, url, token, body):
            calls.append((method, url, token, body))
            return {"code": 0, "data": {}}

        notifier = n.FeishuNotifier(lambda: "tok-123", BASE, transport=transport)
        notifier("done", _ticket())
        method, url, token, body = calls[0]
        self.assertEqual((method, token), ("POST", "tok-123"))
        self.assertTrue(url.endswith("receive_id_type=open_id"))
        self.assertEqual(body["receive_id"], "ou_li")
        self.assertEqual(body["msg_type"], "interactive")
        self.assertIn("OSS 只读", json.loads(body["content"])["header"]["title"]["content"])

    def test_repeated_failure_messages_employee_only_once(self):
        calls = []
        notifier = n.FeishuNotifier(
            lambda: "tok", BASE, transport=lambda *a: calls.append(a) or {"code": 0}
        )
        once = [{"event": "execute_failed", "note": "x"}]
        notifier("failed", _ticket(events=once))
        notifier("failed", _ticket(events=once * 2))
        self.assertEqual(len(calls), 1)

    def test_user_id_fallback_for_iam_login(self):
        calls = []
        notifier = n.FeishuNotifier(
            lambda: "tok", BASE, transport=lambda *a: calls.append(a) or {"code": 0}
        )
        notifier("done", _ticket(applicant={"union_id": "on_x", "user_id": "u123"}))
        _, url, _, body = calls[0]
        self.assertTrue(url.endswith("receive_id_type=user_id"))
        self.assertEqual(body["receive_id"], "u123")

    def test_only_a_union_id_still_reaches_the_applicant(self):
        """老单子和从别处同步来的单子常常只有 union_id。原先直接跳过，而调用方
        拿不到异常 → 单子记成「已提醒」、人根本没收到（线上两张到期凭证就是这样）。"""
        calls = []

        def transport(method, url, token, payload):
            calls.append((url, payload))
            return {"code": 0}

        notifier = n.FeishuNotifier(lambda: "tok", BASE, transport=transport)
        notifier("done", _ticket(applicant={"union_id": "on_x"}))
        self.assertEqual(len(calls), 1)
        self.assertIn("union_id", calls[0][0])
        self.assertEqual(calls[0][1]["receive_id"], "on_x")

    def test_no_feishu_id_at_all_raises_instead_of_silently_skipping(self):
        notifier = n.FeishuNotifier(lambda: "tok", BASE, transport=lambda *a: {"code": 0})
        with self.assertRaises(n.NotifyError):
            notifier("done", _ticket(applicant={"email": "x@wuji.tech"}))

    def test_errors_are_scrubbed(self):
        refused = n.FeishuNotifier(
            lambda: "tok-secret-9",
            BASE,
            transport=lambda *a: {"code": 230013, "msg": "bad tok-secret-9"},
        )
        with self.assertRaises(n.NotifyError) as ctx:
            refused("done", _ticket())
        self.assertIn("230013", str(ctx.exception))
        self.assertNotIn("tok-secret-9", str(ctx.exception))

        def boom(*a):
            raise RuntimeError("Authorization: Bearer tok-secret-9")

        with self.assertRaises(n.NotifyError) as ctx:
            n.FeishuNotifier(lambda: "tok-secret-9", BASE, transport=boom)("done", _ticket())
        self.assertNotIn("tok-secret-9", str(ctx.exception))
        for bad in ({}, {"code": "0"}, [], None):
            with self.assertRaises(n.NotifyError):
                n.FeishuNotifier(lambda: "t", BASE, transport=lambda *a, b=bad: b)(
                    "done", _ticket()
                )


class AdminAlertTests(unittest.TestCase):
    def test_failed_alert_has_scrubbed_reason_and_admin_link(self):
        sent = []
        alert = n.AdminAlert(
            "https://open.feishu.cn/open-apis/bot/v2/hook/abcdefgh",
            "s",
            BASE,
            send=lambda text, **kw: sent.append((text, kw)),
        )
        ticket = _ticket(events=[{"event": "execute_failed", "note": "AddUserToGroup 超时"}])
        alert("done", ticket)
        self.assertEqual(sent, [])
        alert("failed", ticket)
        text, kw = sent[0]
        self.assertIn("REQ-20260101-ABCDEF12 开通失败", text)
        self.assertIn("原因：AddUserToGroup 超时", text)
        self.assertIn(f"{BASE}/#admin/request=REQ-20260101-ABCDEF12", text)
        self.assertEqual(kw["secret"], "s")
        sent.clear()
        alert("failed", _ticket(applicant={"name": '<at user_id="all">所有人</at>'}, events=[]))
        self.assertNotIn("<at", sent[0][0])

    def test_from_env_switches(self):
        env = {"DELIVERY_BASE_URL": BASE}
        self.assertIsNone(n.from_env(env, token=lambda: "t"))
        on = {**env, "DELIVERY_NOTIFY": "1"}
        self.assertIsNotNone(n.from_env(on, token=lambda: "t"))
        # 没应用凭证、没告警群：什么都不开
        self.assertIsNone(n.from_env(on, token=None))
        # 只有告警群
        hook = {
            "DELIVERY_NOTIFY": "1",
            "DELIVERY_ALERT_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/abcdefgh",
        }
        self.assertIsNotNone(n.from_env(hook, token=None))
        # 面板地址不是 https：不给申请人发（按钮会指向不安全的地址）
        self.assertIsNone(
            n.from_env(
                {"DELIVERY_NOTIFY": "1", "DELIVERY_BASE_URL": "http://panel.example.com"},
                token=lambda: "t",
            )
        )

    def test_combine_sends_all_then_raises(self):
        seen = []

        def bad(event, ticket):
            raise n.NotifyError("down")

        both = n.combine(bad, lambda e, tk: seen.append(e))
        with self.assertRaises(n.NotifyError):
            both("failed", _ticket())
        self.assertEqual(seen, ["failed"])
        self.assertIsNone(n.combine(None, None))


class FlowNotifyTests(unittest.TestCase):
    def harness(self):
        h = Harness(TEMPLATES)
        h.executor = MemberExecutor()
        h.rec = n.RecordingNotifier()
        h.flows._notify = h.rec
        return h

    def test_done_rejected_withdrawn(self):
        h = self.harness()
        ticket = h.submit()
        self.assertEqual(h.rec.events(), [])  # 提交本身不通知（员工自己刚点的）
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        cred = h.submit(template="dev-sts", payload=dict(CRED))
        h.approve(cred)
        h.flows.sync(cred["id"], force=True)
        rejected = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(rejected, "REJECTED")
        h.flows.sync(rejected["id"], force=True)
        canceled = h.submit(payload={"cloud_user": "lisi", "days": 8})
        h.approve(canceled, "CANCELED")
        h.flows.sync(canceled["id"], force=True)
        # 凭证单和权限单一样走到「已完成」，不再有「可领取」这一档
        self.assertEqual(h.rec.events(), ["done", "done", "rejected", "withdrawn"])
        card = n.build_card("done", h.store.get(cred["id"]), base_url=BASE)
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("审批", body)  # 提示去审批里看凭证
        self.assertNotIn("sts-secret", body)
        self.assertNotIn("STS.AK1234", body)

    def test_failed_then_retry_done(self):
        h = self.harness()
        h.executor.fail_on = ("add", "grp-oss-read")
        ticket = h.submit()
        h.approve(ticket)
        self.assertEqual(h.flows.sync(ticket["id"], force=True)["status"], t.FAILED)
        h.executor.fail_on = None
        h.flows.execute(ticket["id"], actor="admin")
        self.assertEqual(h.rec.events(), ["failed", "done"])

    def test_unverifiable_approval_notifies_failed(self):
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
        h.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "PENDING"
        h.flows.execute(ticket["id"], actor="system")
        self.assertEqual(h.rec.events(), ["failed"])

    def test_revoked_and_stuck(self):
        h = self.harness()
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 1})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        h.now[0] += 2 * 86400
        h.flows.revoke_expired()
        self.assertEqual(h.rec.events(), ["done", "revoked"])

        stuck = h.submit(payload={"cloud_user": "lisi", "days": 5})
        h.approve(stuck)
        h.store.update(
            stuck["id"],
            actor="feishu",
            expect=[t.PENDING],
            to=t.APPROVED,
            event="approval_approved",
        )
        h.store.update(
            stuck["id"], actor="system", expect=[t.APPROVED], to=t.EXECUTING, event="execute_start"
        )
        h.now[0] += 31 * 60
        h.flows.recover_stuck(actor="system")
        self.assertEqual(h.rec.events()[-1], "failed")

    def test_notify_failure_never_changes_state(self):
        h = self.harness()

        def broken(event, ticket):
            raise RuntimeError("feishu down tok-secret")

        h.flows._notify = broken
        ticket = h.submit()
        h.approve(ticket)
        with mock.patch("sys.stderr") as err:
            done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(h.store.get(ticket["id"])["events"][-1]["event"], "execute_done")
        self.assertTrue(err.write.called)

    def test_remind_expiring_once_within_window(self):
        h = self.harness()
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 10})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        self.assertEqual(h.flows.remind_expiring(days=3), [])  # 还有 10 天
        h.now[0] += 8 * 86400
        self.assertEqual(len(h.flows.remind_expiring(days=3)), 1)
        self.assertEqual(h.flows.remind_expiring(days=3), [])  # 同一档不重复提醒
        self.assertEqual(h.rec.events(), ["done", "expiring"])
        events = [e["event"] for e in h.store.get(ticket["id"])["events"]]
        self.assertEqual(events.count("expiry_reminded:3"), 1)
        h.now[0] += 3 * 86400  # 已经过期：交给回收，不提醒
        self.assertEqual(h.flows.remind_expiring(days=3), [])

    def test_admin_only_channel_does_not_consume_reminder(self):
        h = self.harness()
        admin_only = n.combine(n.AdminAlert("https://x", "s", BASE, send=lambda *a, **k: None))
        self.assertFalse(admin_only.reaches_applicant)
        h.flows._notify = admin_only
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 10})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        h.now[0] += 8 * 86400
        self.assertEqual(h.flows.remind_expiring(days=3), [])
        h.flows._notify = h.rec  # 下次飞书恢复：照常提醒
        self.assertEqual(len(h.flows.remind_expiring(days=3)), 1)

    def test_short_grant_is_not_reminded_right_after_done(self):
        h = self.harness()
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 1})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        h.now[0] += 60
        self.assertEqual(h.flows.remind_expiring(days=3), [])

    def test_failed_reminder_is_recorded(self):
        h = self.harness()

        def down(event, ticket):
            if event == "expiring":
                raise n.NotifyError("down")

        h.flows._notify = down
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 10})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        h.now[0] += 8 * 86400
        with mock.patch("sys.stderr"):
            self.assertIn("失败", h.flows.remind_expiring(days=3)[0])
        events = [e["event"] for e in h.store.get(ticket["id"])["events"]]
        self.assertIn("expiry_remind_failed:3", events)  # 档位跟着记，失败那档下轮会重试
        self.assertEqual(h.store.get(ticket["id"])["status"], t.DONE)

    def test_remind_without_notifier_records_nothing(self):
        h = self.harness()
        h.flows._notify = None
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 2})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        self.assertEqual(h.flows.remind_expiring(days=3), [])
        events = [e["event"] for e in h.store.get(ticket["id"])["events"]]
        self.assertNotIn("expiry_reminded", events)


class SweepNotifyTests(unittest.TestCase):
    def test_sweep_reminds_even_when_revoke_step_breaks(self):
        import argparse
        import os
        import time

        from delivery import cli_requests

        h = Harness(TEMPLATES)
        h.executor = MemberExecutor()
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
        h.now[0] = time.time() - 8 * 86400  # 8 天前开通的 10 天权限：还剩 2 天
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 10})
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
        rec = n.RecordingNotifier()
        tokens = []

        def fake_from_env(environ, *, token):
            tokens.append(token)
            return rec

        def broken_revoke(self):
            raise RuntimeError("revoke broke")

        cwd = Path.cwd()
        os.chdir(work)
        try:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "DELIVERY_FEISHU_APP_ID": "cli_x",
                        "DELIVERY_FEISHU_APP_SECRET": "s",
                        "DELIVERY_NOTIFY": "1",
                    },
                ),
                mock.patch("delivery.identity.directory.tenant_token", lambda *a: "tok"),
                mock.patch("delivery.notify.from_env", fake_from_env),
                mock.patch("delivery.flows.Flows.revoke_expired", broken_revoke),
                mock.patch("delivery.provision.executor_from_env", lambda p, a: h.executor),
                mock.patch("delivery.cli._git_toplevel", lambda path: None),
            ):
                code = cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 1)  # 回收步骤出错算一个问题
        self.assertEqual(rec.events(), ["expiring"])
        self.assertIsNotNone(tokens[0])
        self.assertEqual(tokens[0](), "tok")


class AccountDoneCardTests(unittest.TestCase):
    """开完号那张卡：**告诉他怎么登、把地址给他。**

    原先这两样都没有 —— 卡上写死「初始密码请在 7 天内到平台领取」（SSO 开了之后
    那是条死路，领到的密码登不进去），而且整张卡一个链接都没有，人得自己去问「在哪登」。
    """

    def _card(self, *, sso: bool, console_login: bool = True):
        import os

        from delivery import platforms

        scope = "aliyun/1704065796538912"
        self.addCleanup(os.environ.pop, platforms.ENV_SSO, None)
        os.environ[platforms.ENV_SSO] = scope if sso else "volcano/9999"
        ticket = {
            "id": "REQ-1",
            "kind": "account",
            "template": {
                "title": "阿里云子账号",
                "platform": "aliyun",
                "account": "1704065796538912",
                "console_login": console_login,
            },
        }
        return n.build_card("done", ticket, base_url="https://panel.example.com")

    def _text(self, card):
        return "".join(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")

    def _buttons(self, card):
        return [
            (a["text"]["content"], a.get("url", ""))
            for e in card["elements"]
            if e.get("tag") == "action"
            for a in e["actions"]
        ]

    def test_an_sso_account_is_not_told_to_fetch_a_password(self):
        """SSO 开着时 RAM 密码登录全局失效。还让他去领密码的话，
        他领到一串登不进去的东西，只会以为是账号没建好。"""
        text = self._text(self._card(sso=True))
        self.assertNotIn("初始密码", text)
        self.assertIn("公司账号", text)

    def test_a_password_account_still_gets_the_old_instruction(self):
        """反向锁：别为了堵 SSO 把没开 SSO 的账号也一起改了。"""
        self.assertIn("初始密码", self._text(self._card(sso=False)))

    def test_the_console_address_is_on_the_card(self):
        """这是他最需要的一个东西，原先整张卡里一个链接都没有。"""
        urls = dict((label, url) for label, url in self._buttons(self._card(sso=True)))
        self.assertIn("去登录控制台", urls)
        self.assertIn("signin.aliyun.com", urls["去登录控制台"])

    def test_an_account_without_console_login_gets_no_login_button_text(self):
        text = self._text(self._card(sso=True, console_login=False))
        self.assertIn("只能用访问凭证", text)


if __name__ == "__main__":
    unittest.main()


class CardLinkTests(unittest.TestCase):
    """卡片上那个按钮必须真能到「权限对账」页。

    前端 `app.js:parseHash` 只认 `admin/iam` 这一个串 —— 写成别的（比如 `iam`）
    会掉到兜底分支、落在「我的」页面上，而**按钮看起来完全正常**，
    点了只是没到该到的地方，没有任何报错。这个错发生过一次：
    两张卡片各写各的锚点，一张对一张错。
    """

    REPORT = {
        "apps": [
            {
                "app": "aliyun-main",
                "drift": [
                    {
                        "kind": "inactive",
                        "name": "某人",
                        "username": "A1",
                        "union_id": "u1",
                        "theirs": "someone@x",
                    }
                ],
            }
        ]
    }

    def _urls(self, card):
        return [
            a["url"]
            for e in card["elements"]
            if e.get("tag") == "action"
            for a in e["actions"]
            if a.get("url")
        ]

    def test_every_card_points_at_the_same_real_route(self):
        base = "https://panel.example.com"
        cards = [
            n.drift_card(self.REPORT, base_url=base),
            n.reclaim_card(
                {
                    "done": [],
                    "held": [{"name": "某人", "app": "aliyun-main", "value": "someone@x"}],
                },
                base_url=base,
            ),
        ]
        for card in cards:
            for url in self._urls(card):
                self.assertTrue(url.endswith(n.IAM_PAGE), url)

    def test_without_a_base_url_there_is_no_button(self):
        """拼不出地址时不要放一个点了没反应的按钮。"""
        self.assertEqual(self._urls(n.drift_card(self.REPORT, base_url="")), [])
        self.assertEqual(n.page_link(""), "")
