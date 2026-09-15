import io
import unittest
from contextlib import redirect_stdout

from delivery.access import STATE_ACTION, STATE_BLOCKED, STATE_READY, guide
from delivery.cli import _width, _wrap, main
from delivery.registry import PlatformRegistry


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.registry = PlatformRegistry.load()

    def test_sso_platform_with_account_is_ready(self):
        g = guide(self.registry.get("aliyun"))
        self.assertEqual(g.state, STATE_READY)
        self.assertTrue(g.ready)
        self.assertIn("不需要输入密码", g.hint)

    def test_sso_without_account_is_blocked_not_actionable(self):
        # 没有子账号是用户自己解决不了的，不该给他一条做不到的「下一步」。
        g = guide(self.registry.get("aliyun"), has_account=False)
        self.assertEqual(g.state, STATE_BLOCKED)
        self.assertEqual(g.next_command, "")

    def test_sso_not_yet_live_tells_user_to_use_password(self):
        g = guide(self.registry.get("aliyun"), sso_enabled=False)
        self.assertEqual(g.state, STATE_ACTION)
        self.assertIn("账号密码", g.hint)

    def test_bind_platform_unbound_explains_why_and_how(self):
        g = guide(self.registry.get("jiuzhang"), bound=False)
        self.assertEqual(g.state, STATE_ACTION)
        # 必须如实说明接不了飞书，而不是含糊成「请配置凭证」。
        self.assertIn("不支持飞书登录", g.headline)
        self.assertIn("无法自动化", g.hint)
        # 三步引导缺一不可，否则用户不知道 AK/SK 从哪来。
        for step in ("1.", "2.", "3."):
            self.assertIn(step, g.hint)
        self.assertEqual(g.next_command, "delivery bind jiuzhang")

    def test_bind_platform_hint_uses_short_name(self):
        # 句子里塞全名（含括号英文）读起来很别扭，必须用简称。
        g = guide(self.registry.get("jiuzhang"), bound=False)
        self.assertIn("九章不支持", g.headline)
        self.assertNotIn("(Alaya New)不支持", g.headline)

    def test_bind_platform_bound_is_ready(self):
        g = guide(self.registry.get("jiuzhang"), bound=True)
        self.assertEqual(g.state, STATE_READY)
        self.assertIn("无需登录", g.headline)
        self.assertIn("--rotate", g.next_command)

    def test_cert_platform_ready(self):
        g = guide(self.registry.get("xiwang-baremetal"))
        self.assertEqual(g.state, STATE_READY)
        self.assertIn("到期自动失效", g.hint)

    def test_cert_platform_without_access_is_blocked(self):
        g = guide(self.registry.get("xiwang-baremetal"), has_account=False)
        self.assertEqual(g.state, STATE_BLOCKED)

    def test_to_dict_is_json_safe(self):
        import json

        g = guide(self.registry.get("jiuzhang"))
        json.dumps(g.to_dict(), ensure_ascii=False)  # 看板直接消费，不能有非序列化字段

    def test_every_shipped_platform_produces_guidance(self):
        for platform in self.registry:
            g = guide(platform)
            self.assertTrue(g.headline, platform.id)
            self.assertTrue(g.action_label, platform.id)


class DisplayWidthTests(unittest.TestCase):
    def test_cjk_counts_as_two_columns(self):
        self.assertEqual(_width("阿里云"), 6)
        self.assertEqual(_width("abc"), 3)

    def test_wrap_respects_display_width_not_char_count(self):
        # 这正是 textwrap 不能用的原因：36 个中文字 = 72 列，不是 36 列。
        text = "中" * 36
        lines = _wrap(text, 72)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(_wrap("中" * 37, 72)), 2)

    def test_wrap_never_returns_empty_list(self):
        self.assertEqual(_wrap("", 72), [""])


class CliTests(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(argv)
        return code, buf.getvalue()

    def test_platforms_table(self):
        code, out = self._run(["platforms"])
        self.assertEqual(code, 0)
        self.assertIn("阿里云", out)
        self.assertIn("pending-verification", out)

    def test_platforms_json(self):
        import json

        code, out = self._run(["--json", "platforms"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(any(p["id"] == "jiuzhang" for p in data))

    def test_login_guide_lists_todo(self):
        code, out = self._run(["login-guide"])
        self.assertEqual(code, 0)
        self.assertIn("需要你做一次的", out)
        self.assertIn("九章", out)

    def test_login_guide_marks_bound_platform_ready(self):
        _, out = self._run(["login-guide", "--bound", "jiuzhang"])
        self.assertIn("已托管", out)

    def test_unknown_platform_in_flag_is_rejected(self):
        # 拼错平台名若被静默忽略，用户会以为自己绑过了。
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["login-guide", "--bound", "jiuzang"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
