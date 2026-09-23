"""定时任务的告警通道。

三件事：
  · **没配群机器人 webhook 时私聊管理员。** 线上一直没配，原先这时只打一行
    「没设置 webhook」就退出 —— 刷新出的每一个问题都只进了日志，从来没有人被通知过。
  · **退出码要把「告警已送到」和「告警没发出去」分开。** 前者算正常结束（3），
    后者才触发 systemd 的 OnFailure 兜底；不分的话同一件事会收到两条。
  · **同一个单元 6 小时只私聊一次，但冷却坏了要照发。** 2026-09-23 线上 sweep
    一分钟一轮、每轮非零退出 → 兜底告警一分钟一条；被刷屏的人第二天就会把机器人
    折叠，之后真出事也没人看。冷却只是为了少刷屏，它自己坏掉时**不许**把告警通道
    一起带走。这里锁的是它的基本行为（只发一次 / 按单元分开 / 文件坏了照发）；
    跨 6 小时边界、连号、写不下去、状态文件各种畸形，在
    `test_delivery_alert_cooldown.py` 里用可控时钟逐个过。
"""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from delivery import cli, notify


def _args(**kw):
    base = dict(no_alert=False, admins="identity/admins.json")
    base.update(kw)
    return argparse.Namespace(**base)


class RefreshAlertTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        for name in ("_refresh_locked", "_admin_alert"):
            self.addCleanup(setattr, cli, name, getattr(cli, name))
        self.addCleanup(os.environ.pop, "DELIVERY_ALERT_WEBHOOK", None)
        os.environ.pop("DELIVERY_ALERT_WEBHOOK", None)

    def _run(self, *, problems, alert_result=""):
        def fake_locked(args, report):
            report.problems.extend(problems)
            return 0

        cli._refresh_locked = fake_locked
        cli._admin_alert = lambda title, text, path="": self.sent.append(title) or alert_result
        return cli._cmd_refresh(_args())

    def test_without_a_webhook_the_admins_get_a_private_message(self):
        code = self._run(problems=["火山采集失败"])
        self.assertEqual(self.sent, ["云权限面板数据刷新异常"])
        self.assertEqual(code, cli.EXIT_REPORTED, "告警已送到：算正常结束，别再触发 OnFailure")

    def test_an_undeliverable_alert_is_a_real_failure(self):
        """告警没发出去就必须以失败退出 —— 那正是 OnFailure 兜底要接住的情况。"""
        code = self._run(problems=["火山采集失败"], alert_result="没配 DELIVERY_FEISHU_APP_ID")
        self.assertEqual(code, 1)

    def test_a_clean_run_sends_nothing(self):
        self.assertEqual(self._run(problems=[]), 0)
        self.assertEqual(self.sent, [])


class UnitFailedTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, cli, "_admin_alert", cli._admin_alert)
        # 每个用例一份**自己的**冷却记录。默认值是机器上的真路径
        # `/var/lib/delivery/alert-state.json`（`DELIVERY_ALERT_STATE` 可覆盖，
        # systemd 单元里就是这么指的）—— 不隔离的话：① 同一次 pytest 里后面的用例
        # 会被前面的用例写下的冷却期压住（症状是莫名其妙的「仍在失败，第 4 次」）；
        # ② 真往那个路径写，写得进去之后**接下来 6 小时每次跑都红**，删掉才恢复。
        # 两个都不是被测代码的错，是测试没给它一个临时的家。
        # （默认值从前是仓库相对路径 `identity/alert-state.json`，2026-09-23 改掉，
        #   理由和配对关系见 `test_delivery_alert_cooldown.py::UnitFileTests`。）
        self.state = str(Path(tempfile.mkdtemp()) / "alert-state.json")

    def unit_failed(self, unit):
        return cli.main(["unit-failed", "--unit", unit, "--state", self.state])

    def test_it_names_the_unit_and_where_to_look(self):
        seen = {}

        def fake(title, text, path=""):
            seen.update(title=title, text=text)
            return ""

        cli._admin_alert = fake
        code = self.unit_failed("delivery-refresh.service")
        self.assertEqual(code, 0)
        self.assertIn("delivery-refresh.service", seen["title"])
        self.assertIn("journalctl -u delivery-refresh.service", seen["text"])

    def test_it_fails_loudly_when_it_cannot_send(self):
        cli._admin_alert = lambda *a, **k: "管理员名单里没有 union_id"
        self.assertEqual(self.unit_failed("x.service"), 1)

    def test_the_same_unit_failing_again_does_not_private_message_twice(self):
        """一分钟一轮的定时任务坏着不动时，管理员该收到**一条**，不是一分钟一条。

        这是 2026-09-23 那次刷屏的第二道闸（第一道是退出码分级）。
        """
        sent = []
        cli._admin_alert = lambda title, text, path="": sent.append(title) or ""
        self.assertEqual(self.unit_failed("delivery-sweep.service"), 0)
        for _ in range(5):
            self.assertEqual(self.unit_failed("delivery-sweep.service"), 0)
        self.assertEqual(len(sent), 1, "冷却期内只私聊一次")

    def test_a_different_unit_is_not_silenced_by_the_cooldown(self):
        """冷却是**按单元**记的。记成全局的话，A 坏着的这 6 小时里 B 崩了没人知道。"""
        sent = []
        cli._admin_alert = lambda title, text, path="": sent.append(title) or ""
        self.unit_failed("delivery-sweep.service")
        self.unit_failed("delivery-refresh.service")
        self.assertEqual(len(sent), 2)

    def test_an_unreadable_state_file_still_lets_the_alert_through(self):
        """冷却记录只是为了少刷屏，它坏了不该把告警通道一起带走 ——
        宁可重复私聊，也别因为一个缓存文件把真出事那条咽掉。"""
        Path(self.state).write_text("{坏掉的 json", encoding="utf-8")
        sent = []
        cli._admin_alert = lambda title, text, path="": sent.append(title) or ""
        self.assertEqual(self.unit_failed("delivery-sweep.service"), 0)
        self.assertEqual(len(sent), 1)

    def test_the_text_does_not_guess_the_cause(self):
        """回归（曾是缺陷）：文案不许替 systemd 猜原因。

        原先写死「没来得及出报告（崩溃 / 超时被杀 / 依赖导入失败）」，而 2026-09-23
        线上真正的原因是第四种：任务跑完了、报告也出全了，只是退出码非零。
        三句猜测全错，人照着去查日志什么也查不到 —— 告警说假话比不报还糟。
        """
        seen = {}
        cli._admin_alert = lambda title, text, path="": seen.update(text=text) or ""
        self.unit_failed("delivery-sweep.service")
        self.assertNotIn("没来得及出报告", seen["text"])
        # 第四种可能要说出来：任务跑完了、报告也出全了，只是这一轮有活没干成
        self.assertIn("任务跑完了", seen["text"], "要把第四种可能说出来")
        for what in ("到期回收没收干净", "审批同步整步跳过", "读不了申请单存储"):
            self.assertIn(what, seen["text"], f"退 1 的三种真实来源要列全，缺了 {what}")
        # **不许教人加 SuccessExitStatus**（审计 Med-1）：这批打完之后 sweep 最常见的
        # 失败就是「到期回收没收干净」，日志里没 traceback、报告也出全了 —— 完全符合
        # 「那就是退出码的事」的判据。照做 = SuccessExitStatus=1，而 1 同时是「整步崩了」
        # 「存储读不了」的退出码，兜底告警从此永久失效。告警不该给出会关掉自己的建议。
        self.assertNotIn("SuccessExitStatus", seen["text"])


class CardTests(unittest.TestCase):
    def test_a_long_report_is_trimmed_but_says_so(self):
        card = notify.alert_card("标题", "\n".join(f"第 {i} 行" for i in range(50)))
        elements = card["body"]["elements"]
        self.assertEqual(len(elements), 25)  # 24 行正文 + 一行「还有多少行」
        self.assertIn("还有 26 行", json.dumps(elements[-1], ensure_ascii=False))
        # 真出事才是红的；待办类的卡是蓝的（见 notify._TEMPLATES）
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["schema"], "2.0")


class UnitFileTests(unittest.TestCase):
    """单元文件本身。两个写错了不报错、只是永远不生效的地方。"""

    def _read(self, name):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        return (root / "deploy" / "panel" / name).read_text(encoding="utf-8")

    #: 两个都会用 EXIT_REPORTED 报「跑完了、有问题但已经报出来了」。
    #: sweep 是一分钟一轮的那个 —— 2026-09-23 的刷屏就出在它身上，别漏。
    REPORTING_UNITS = ("delivery-refresh.service", "delivery-sweep.service")

    def test_on_failure_sits_in_the_unit_section(self):
        """写在 [Service] 段里的 OnFailure systemd 直接忽略 —— 原先那行注释就是这么放的。"""
        for name in self.REPORTING_UNITS:
            with self.subTest(unit=name):
                text = self._read(name)
                unit = text.split("\n[Service]\n")[0]  # 按整行切 —— 注释里也可能提到这个段名
                self.assertIn("OnFailure=delivery-unit-failed@%n.service", unit)

    def test_the_reported_exit_code_counts_as_success(self):
        """退出码和单元里的声明是**一对**：哪边单独改了，假告警就悄悄回来，
        而代码和测试全是绿的 —— 只有线上管理员每分钟收到一条。"""
        for name in self.REPORTING_UNITS:
            with self.subTest(unit=name):
                text = self._read(name)
                line = next(
                    (ln for ln in text.splitlines() if ln.startswith("SuccessExitStatus=")), ""
                )
                self.assertTrue(line, f"{name} 少了 SuccessExitStatus")
                self.assertIn(str(cli.EXIT_REPORTED), line.split("=", 1)[1].split())
