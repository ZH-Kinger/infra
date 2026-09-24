"""刷新告警的分级：谁值得打扰人、标题和正文是不是同一句话。

线上的样子（2026-09-24）
────────────────────────
面板自己开了一个子账号 `chi.xuemin`，管理员几分钟前刚在飞书上批过。当晚的刷新
私聊了他一条：标题「云权限面板数据刷新**异常**」、正文第一行「刷新**完成**」。
两个毛病叠在一起：

  · **标题写死了**：`_cmd_refresh` 里的字面量和 `render()` 里算出来的那句各写一份，
    于是同一条消息自相矛盾。收到自相矛盾的告警，人学到的是「这个机器人不可信」；
  · **面板自己开的号不该推**：它是几分钟前刚批的，再私聊一条「新增子账号」纯属噪音。
    而告警一旦变成噪音，真出事那条（控制台里手工开的、来路不明的号）也一起被忽略。

**改之前这两条一个用例都没有** —— 157 个相关用例全绿，这正是它能悄悄变成噪音的原因。
所以这里把两边都钉住：哪些情况才值得打扰人、标题从哪来。

纯离线，飞书接口替换成记录器。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from delivery import cli, inventory
from delivery.refresh import RefreshReport, diff_snapshots

from .test_delivery_refresh import _snap


def report(**kw) -> RefreshReport:
    return RefreshReport(**kw)


class TitleTests(unittest.TestCase):
    """标题和正文首行**必须是同一个来源**。"""

    def test_a_clean_report_is_titled_done(self):
        self.assertEqual(report().title, "云权限面板数据刷新完成")

    def test_a_report_with_problems_is_titled_abnormal(self):
        self.assertEqual(report(problems=["火山采集失败"]).title, "云权限面板数据刷新异常")

    def test_the_title_is_the_first_line_of_the_body(self):
        """两处各写一份的下场就是线上那条：标题说异常、正文说完成。"""
        for rep in (
            report(),
            report(problems=["火山采集失败"]),
            report(added_users=["aliyun/100/chi.xuemin"]),
            report(problems=["x"], removed_users=["aliyun/100/y"], people_count=3),
        ):
            with self.subTest(problems=rep.problems):
                self.assertEqual(rep.render().splitlines()[0], rep.title)

    def test_only_problems_make_it_abnormal(self):
        """有变化不等于有异常。把「新增了一个号」也说成异常，人就没法从标题分轻重。"""
        rep = report(added_users=["aliyun/100/x"], added_unregistered=["aliyun/100/x"])
        self.assertTrue(rep.ok)
        self.assertEqual(rep.title, "云权限面板数据刷新完成")


class WhoDeservesAMessageTests(unittest.TestCase):
    def test_a_user_the_panel_issued_itself_is_not_worth_a_message(self):
        """**这就是线上那条噪音。** 管理员几分钟前刚在飞书上批过这个号，
        当晚再私聊他一遍「新增子账号 chi.xuemin」，除了训练他忽略这个机器人没别的作用。"""
        rep = report(added_users=["aliyun/100/chi.xuemin"], added_unregistered=[])
        self.assertFalse(rep.needs_attention)

    def test_a_user_created_by_hand_in_the_console_is(self):
        """云上没有任何字段记着「这个号是谁为谁开的」，手工开的事后查不清来历 ——
        当天问还有人记得，隔一个月就没人说得清了。"""
        rep = report(
            added_users=["aliyun/100/chi.xuemin", "aliyun/100/ghost"],
            added_unregistered=["aliyun/100/ghost"],
        )
        self.assertTrue(rep.needs_attention)

    def test_problems_always_deserve_a_message(self):
        rep = report(problems=["volcano/default：超时"])
        self.assertTrue(rep.needs_attention)
        self.assertIn("异常", rep.title)

    def test_the_other_categories_still_trigger(self):
        """改的只是「新增子账号」这一类。顺手把别的也一起收窄的话，
        删号、掉 union_id、新出现的未关联账号就再也没人知道了。"""
        for field in (
            "new_high_risk",
            "removed_users",
            "new_unlinked",
            "lost_union_ids",
        ):
            with self.subTest(field=field):
                self.assertTrue(report(**{field: ["aliyun/100/x"]}).needs_attention)

    def test_a_quiet_run_says_nothing(self):
        self.assertFalse(report(people_count=50, people_written=True).needs_attention)

    def test_added_users_still_show_up_in_the_body(self):
        """收窄的是**触发条件**，不是内容。别的原因把消息发出去时，
        面板自己开的那些号照样要列出来 —— 那是这一轮到底发生了什么的记录。"""
        rep = report(
            problems=["volcano/default：超时"],
            added_users=["aliyun/100/chi.xuemin"],
            added_unregistered=[],
        )
        text = rep.render()
        self.assertIn("新增子账号", text)
        self.assertIn("aliyun/100/chi.xuemin", text)


class DiffTests(unittest.TestCase):
    """`diff_snapshots(known=…)`：两栏是怎么分出来的。"""

    def _diff(self, known=None):
        before = inventory.parse(_snap([("aliyun", "100", "a", [])]))
        after = inventory.parse(
            _snap(
                [
                    ("aliyun", "100", "a", []),
                    ("aliyun", "100", "chi.xuemin", []),
                    ("aliyun", "100", "ghost", []),
                ]
            )
        )
        rep = RefreshReport()
        diff_snapshots(before, after, rep, known=known)
        return rep

    def test_only_the_unregistered_one_is_flagged(self):
        rep = self._diff(known={"aliyun/100/chi.xuemin"})
        self.assertEqual(rep.added_users, ["aliyun/100/chi.xuemin", "aliyun/100/ghost"])
        self.assertEqual(rep.added_unregistered, ["aliyun/100/ghost"])
        self.assertTrue(rep.needs_attention)

    def test_an_all_panel_issued_round_is_silent(self):
        rep = self._diff(known={"aliyun/100/chi.xuemin", "aliyun/100/ghost"})
        self.assertEqual(len(rep.added_users), 2)
        self.assertEqual(rep.added_unregistered, [])
        self.assertFalse(rep.needs_attention, "全是面板自己开的，没什么要问的")

    def test_without_a_ledger_everything_is_reported(self):
        """**台账读不到时照常全量告警。** 反过来（当成「都是面板开的」）的话，
        这次改动就成了一个静默的漏报开关：台账一坏，手工开的号再也不会被报出来。"""
        for known in (None, set()):
            with self.subTest(known=known):
                rep = self._diff(known=known)
                self.assertEqual(rep.added_unregistered, rep.added_users)
                self.assertTrue(rep.needs_attention)


class PanelIssuedUsersTests(unittest.TestCase):
    """`cli._panel_issued_users`：那份「面板开过哪些号」是从台账来的。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "tickets.json"

    def args(self):
        return argparse.Namespace(tickets=str(self.path))

    def ticket(self, **kw):
        base = {
            "id": "t-1",
            "status": "done",
            "template": {"platform": "aliyun", "account": "100"},
            "payload": {"username": "chi.xuemin"},
            # **`user_created` 是「号真的建出来了」的事实**（`create_user` 成功之后才写），
            # 不是申请人填的愿望。没有它这张单不算「面板开过」
            "user_created": True,
        }
        base.update(kw)
        return base

    def write(self, *tickets):
        self.path.write_text(json.dumps({"tickets": list(tickets)}), encoding="utf-8")

    def test_a_panel_issued_account_is_recognised(self):
        self.write(self.ticket())
        problems = []
        self.assertEqual(
            cli._panel_issued_users(self.args(), problems), {"aliyun/100/chi.xuemin"}
        )
        self.assertEqual(problems, [])

    def test_a_credential_user_counts_too(self):
        self.write(self.ticket(payload={}, user_created=False, cred_user="staff-lisi-9a1b2c"))
        self.assertEqual(
            cli._panel_issued_users(self.args(), []), {"aliyun/100/staff-lisi-9a1b2c"}
        )

    def test_a_ticket_that_never_created_anything_does_not_count(self):
        """被拒 / 撤回 / 提交失败 / 开通失败的单子里那个号根本没建出来。
        算进来的话，事后真有人手工用同名建了号，反而不会被提示补登记。"""
        for status in ("rejected", "withdrawn", "submit_failed", "failed"):
            with self.subTest(status=status):
                self.write(self.ticket(status=status, user_created=False))
                self.assertEqual(cli._panel_issued_users(self.args(), []), set())

    def test_a_pending_ticket_cannot_launder_a_hand_made_account(self):
        """**这是一条绕过路径，不只是边界。** 用户名来自申请人**提交时**自己填的
        `payload["username"]` —— 判据要是「不是被拒/撤回就算面板开的」，那谁想让
        自己在控制台手工开的号不被点名，只要先提一张同名的单放着不管就行了。

        所以判据必须是「号真的建出来了」（`user_created` / `cred_user`），
        不是「有人提过这么一张单」。
        """
        self.write(self.ticket(status="pending", user_created=False))
        self.assertEqual(cli._panel_issued_users(self.args(), []), set())

    def test_an_approved_but_not_yet_executed_ticket_does_not_count_either(self):
        """已通过、还没开通的单子同样什么都没建出来。"""
        for status in ("approved", "executing"):
            with self.subTest(status=status):
                self.write(self.ticket(status=status, user_created=False))
                self.assertEqual(cli._panel_issued_users(self.args(), []), set())

    def test_a_brand_new_deployment_is_quiet(self):
        """还没有台账时本来也没有「面板开的号」，不该记成故障。"""
        problems = []
        self.assertEqual(cli._panel_issued_users(self.args(), problems), set())
        self.assertEqual(problems, [])

    def test_an_unreadable_ledger_is_a_real_problem_and_still_reports_everything(self):
        """台账在但读不了是真故障。不报的话，每个新增子账号都会被报成「来路不明」，
        那一栏天天全量误报，两周后就没人看了 —— 功能等于白做。"""
        self.path.write_text("{坏掉的 json", encoding="utf-8")
        problems = []
        self.assertEqual(cli._panel_issued_users(self.args(), problems), set())
        self.assertEqual(len(problems), 1)
        self.assertIn("申请单读不了", problems[0])


class AlertTitleTests(unittest.TestCase):
    """跑一遍 `_cmd_refresh`，看私聊出去的标题是哪一句。"""

    def setUp(self):
        self.sent = []
        for name in ("_refresh_locked", "_admin_alert"):
            self.addCleanup(setattr, cli, name, getattr(cli, name))
        self.addCleanup(os.environ.pop, "DELIVERY_ALERT_WEBHOOK", None)
        os.environ.pop("DELIVERY_ALERT_WEBHOOK", None)
        cli._admin_alert = lambda title, text, path="": self.sent.append((title, text)) or ""

    def _run(self, fill):
        def fake_locked(args, rep):
            fill(rep)
            return 0

        cli._refresh_locked = fake_locked
        return cli._cmd_refresh(argparse.Namespace(no_alert=False, admins="identity/admins.json"))

    def test_a_run_that_is_only_noteworthy_is_not_titled_abnormal(self):
        """**线上那条消息的正脸**：标题说「异常」、正文第一行说「完成」。"""

        def fill(rep):
            rep.added_users = ["aliyun/100/ghost"]
            rep.added_unregistered = ["aliyun/100/ghost"]

        code = self._run(fill)
        title, text = self.sent[0]
        self.assertEqual(title, "云权限面板数据刷新完成")
        self.assertEqual(text.splitlines()[0], title, "标题和正文首行必须一致")
        self.assertEqual(code, 0, "没有采集问题就不是失败")

    def test_a_real_failure_is_still_titled_abnormal(self):
        self._run(lambda rep: rep.problems.append("火山采集失败"))
        self.assertEqual(self.sent[0][0], "云权限面板数据刷新异常")

    def test_a_round_of_panel_issued_accounts_sends_nothing(self):
        """端到端的那条：面板自己开的号不该惊动任何人。"""

        def fill(rep):
            rep.added_users = ["aliyun/100/chi.xuemin"]
            rep.added_unregistered = []

        self.assertEqual(self._run(fill), 0)
        self.assertEqual(self.sent, [])
