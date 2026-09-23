"""定时任务的告警通道。

两件事：
  · **没配群机器人 webhook 时私聊管理员。** 线上一直没配，原先这时只打一行
    「没设置 webhook」就退出 —— 刷新出的每一个问题都只进了日志，从来没有人被通知过。
  · **退出码要把「告警已送到」和「告警没发出去」分开。** 前者算正常结束（3），
    后者才触发 systemd 的 OnFailure 兜底；不分的话同一件事会收到两条。
"""

import argparse
import json
import os
import unittest

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

    def test_it_names_the_unit_and_where_to_look(self):
        seen = {}

        def fake(title, text, path=""):
            seen.update(title=title, text=text)
            return ""

        cli._admin_alert = fake
        code = cli.main(["unit-failed", "--unit", "delivery-refresh.service"])
        self.assertEqual(code, 0)
        self.assertIn("delivery-refresh.service", seen["title"])
        self.assertIn("journalctl -u delivery-refresh.service", seen["text"])

    def test_it_fails_loudly_when_it_cannot_send(self):
        cli._admin_alert = lambda *a, **k: "管理员名单里没有 union_id"
        self.assertEqual(cli.main(["unit-failed", "--unit", "x.service"]), 1)


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

    def test_on_failure_sits_in_the_unit_section(self):
        """写在 [Service] 段里的 OnFailure systemd 直接忽略 —— 原先那行注释就是这么放的。"""
        text = self._read("delivery-refresh.service")
        unit = text.split("\n[Service]\n")[0]  # 按整行切 —— 注释里也可能提到这个段名
        self.assertIn("OnFailure=delivery-unit-failed@%n.service", unit)

    def test_the_reported_exit_code_counts_as_success(self):
        text = self._read("delivery-refresh.service")
        line = next(ln for ln in text.splitlines() if ln.startswith("SuccessExitStatus="))
        self.assertIn(str(cli.EXIT_REPORTED), line.split("=", 1)[1].split())
