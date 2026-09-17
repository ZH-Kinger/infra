"""身份对账。

这一层的产出会决定「谁需要去补邮箱」，判错的代价是让人白跑一趟，或者更糟——
把一个真人当成服务号忽略掉，等 SSO 上线那天他登不进去还没人知道。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery.identity import (
    CLASS_MISMATCH,
    CLASS_MISSING_EMAIL,
    CLASS_OK,
    CLASS_SERVICE,
    AccountUser,
    audit,
    classify,
)
from delivery.identity.collect import (
    CollectError,
    PermissionDeniedError,
    collect_aliyun,
    load_json,
)
from delivery.identity.report import render, to_csv

DOMAIN = "@wuji.tech"


def u(name, email="", display=""):
    return AccountUser(
        platform="aliyun", account="default", name=name, display_name=display, email=email
    )


class ClassifyTests(unittest.TestCase):
    def test_exact_match_is_ok(self):
        self.assertEqual(classify(u("tom", "tom@wuji.tech"), domain=DOMAIN).verdict, CLASS_OK)

    def test_missing_email(self):
        f = classify(u("wangwu", "", "王五"), domain=DOMAIN)
        self.assertEqual(f.verdict, CLASS_MISSING_EMAIL)

    def test_dotted_email_prefix_is_a_mismatch(self):
        # 真实数据里最大的一类：用户名拼音连写，邮箱是 姓.名
        f = classify(u("wangwu", "wang.wu@wuji.tech"), domain=DOMAIN)
        self.assertEqual(f.verdict, CLASS_MISMATCH)
        self.assertIn("wang.wu", f.note)

    def test_personal_email_is_flagged_not_silently_dropped(self):
        # 个人邮箱不在本轮范围，但必须如实列出，否则这些人会在上线时静默掉队
        f = classify(u("zhaoxl", "zhaoxl@gmail.com"), domain=DOMAIN)
        self.assertEqual(f.verdict, CLASS_MISMATCH)
        self.assertIn("非企业邮箱", f.note)

    def test_malformed_email(self):
        self.assertEqual(classify(u("x", "not-an-email"), domain=DOMAIN).verdict, CLASS_MISMATCH)

    def test_service_prefixes(self):
        for name in ("tempak-abc-123", "mes-read", "codex-test", "rl-demo", "wuji-demo"):
            self.assertEqual(classify(u(name), domain=DOMAIN).verdict, CLASS_SERVICE, name)

    def test_service_names(self):
        self.assertEqual(classify(u("finance"), domain=DOMAIN).verdict, CLASS_SERVICE)

    def test_explicit_service_list_overrides_heuristic(self):
        # 启发式判不准时的兜底：显式点名
        f = classify(
            u("data-sync", "", "数据同步专用用户"), domain=DOMAIN, service_names=["data-sync"]
        )
        self.assertEqual(f.verdict, CLASS_SERVICE)

    def test_a_person_whose_name_starts_like_a_service_is_still_checked(self):
        # 反向：不在名单、不匹配前缀的人不该被当服务号
        self.assertNotEqual(
            classify(u("wujie", "wu.jie@wuji.tech"), domain=DOMAIN).verdict, CLASS_SERVICE
        )

    def test_label_prefers_display_name(self):
        self.assertEqual(u("wangwu", display="王五").label, "王五")
        self.assertEqual(u("wangwu").label, "wangwu")


class ReportTests(unittest.TestCase):
    def _report(self):
        return audit(
            [
                u("tom", "tom@wuji.tech"),
                u("wangwu", "wang.wu@wuji.tech", "王五"),
                u("xiaoerchen", "", "陈小二"),
                u("tempak-x-1"),
            ],
            domain=DOMAIN,
        )

    def test_summary_counts(self):
        counts = self._report().summary()
        self.assertEqual(counts[CLASS_OK], 1)
        self.assertEqual(counts[CLASS_MISMATCH], 1)
        self.assertEqual(counts[CLASS_MISSING_EMAIL], 1)
        self.assertEqual(counts[CLASS_SERVICE], 1)

    def test_not_ready_while_anyone_mismatches(self):
        self.assertFalse(self._report().ready)

    def test_ready_when_all_people_match(self):
        report = audit([u("tom", "tom@wuji.tech"), u("tempak-x-1")], domain=DOMAIN)
        self.assertTrue(report.ready)

    def test_service_accounts_do_not_block_readiness(self):
        # 服务号没有邮箱是正常的，不该拦住 SSO 上线
        report = audit([u("tom", "tom@wuji.tech"), u("mes-read"), u("wuji-demo")], domain=DOMAIN)
        self.assertTrue(report.ready)

    def test_empty_input_is_not_ready(self):
        # 一个人都没有就宣布「可以开 SSO」是危险的假阳性
        self.assertFalse(audit([], domain=DOMAIN).ready)

    def test_render_puts_actionable_buckets_first(self):
        text = render(self._report())
        self.assertLess(text.index("需本人补"), text.index("可直接用于 SSO"))
        self.assertIn("账户不存在", text)  # 说清后果

    def test_render_limit_reports_remainder(self):
        many = [u(f"user{i}", display=f"人{i}") for i in range(30)]
        text = render(audit(many, domain=DOMAIN), limit=5)
        self.assertIn("另有 25 条", text)

    def test_csv_has_header_and_one_row_per_finding(self):
        csv_text = to_csv(self._report())
        lines = [ln for ln in csv_text.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 5)  # 表头 + 4 条
        self.assertIn("显示名", lines[0])


class CollectPermissionTests(unittest.TestCase):
    """401/403 绝不能长得像「这人没填邮箱」。

    真实事故：`GetUser` 被拒 → 响应里没有 `User` 键 → 旧代码 `.get("User") or {}`
    把它读成空邮箱 → 报告显示「全部通过」，而实际上十个人的邮箱根本没读到。
    假绿比报错危险得多，所以这里全部要求**中断**。
    """

    def test_denied_on_listusers_aborts(self):
        def runner(command):
            return json.dumps({"Code": "NoPermission", "Message": "no permission to ram:ListUsers"})

        with self.assertRaises(PermissionDeniedError) as ctx:
            collect_aliyun(runner=runner)
        self.assertIn("ram:ListUsers", str(ctx.exception))

    def test_denied_on_getuser_aborts_instead_of_blanking_email(self):
        def runner(command):
            if command[2] == "ListUsers":
                return json.dumps({"Users": {"User": [{"UserName": "a"}]}, "IsTruncated": False})
            return json.dumps({"Code": "Forbidden.RAM", "Message": "user has no permission"})

        with self.assertRaises(PermissionDeniedError) as ctx:
            collect_aliyun(runner=runner)
        self.assertIn("ram:GetUser", str(ctx.exception))

    def test_permission_error_is_a_collect_error(self):
        """调用方只 catch CollectError 时也不会漏掉它。"""
        self.assertTrue(issubclass(PermissionDeniedError, CollectError))

    def test_nonzero_exit_with_denied_text_is_classified(self):
        def runner(command):
            raise AssertionError("unused")

        from delivery.identity import collect as mod

        err = mod._denied("ram:GetUser", "AccessDenied: ...")
        self.assertIsInstance(err, PermissionDeniedError)
        self.assertTrue(mod._is_denied("ErrorCode: AccessDenied"))
        self.assertFalse(mod._is_denied("Throttling.User: too fast"))

    def test_missing_expected_key_is_not_treated_as_empty(self):
        """非权限类的畸形响应同样中断——宁可没结论，也别要错结论。"""

        def runner(command):
            return json.dumps({"SomethingElse": 1})

        with self.assertRaises(CollectError) as ctx:
            collect_aliyun(runner=runner)
        self.assertIn("Users", str(ctx.exception))

    def test_transient_error_is_not_mislabelled_as_permission(self):
        def runner(command):
            return json.dumps({"Code": "ServiceUnavailable", "Message": "try again"})

        with self.assertRaises(CollectError) as ctx:
            collect_aliyun(runner=runner)
        self.assertNotIsInstance(ctx.exception, PermissionDeniedError)

    def test_genuinely_empty_email_still_collects(self):
        """真的没填邮箱 —— 这个必须继续，不能跟着一起报错。"""

        def runner(command):
            if command[2] == "ListUsers":
                return json.dumps({"Users": {"User": [{"UserName": "a"}]}, "IsTruncated": False})
            return json.dumps({"User": {"DisplayName": "某人"}})

        users = collect_aliyun(runner=runner)
        self.assertEqual(users[0].email, "")

    def test_action_name_extraction(self):
        from delivery.identity import collect as mod

        self.assertEqual(
            mod._action_of(["aliyun", "ram", "GetUser", "--UserName", "a"]), "ram:GetUser"
        )


class CollectTests(unittest.TestCase):
    def test_aliyun_pagination_and_detail_lookup(self):
        calls = []

        def runner(command):
            calls.append(list(command))
            assert command[0] == "aliyun" and command[1] == "ram"
            if command[2] == "ListUsers":
                if "--Marker" not in command:
                    return json.dumps(
                        {
                            "Users": {"User": [{"UserName": "a"}]},
                            "IsTruncated": True,
                            "Marker": "m1",
                        }
                    )
                return json.dumps({"Users": {"User": [{"UserName": "b"}]}, "IsTruncated": False})
            return json.dumps({"User": {"DisplayName": "某人", "Email": "a@wuji.tech"}})

        users = collect_aliyun(runner=runner)
        self.assertEqual([x.name for x in users], ["a", "b"])
        self.assertEqual(users[0].email, "a@wuji.tech")
        self.assertTrue(any("GetUser" in c for c in calls))

    def test_truncated_without_marker_does_not_loop_forever(self):
        def runner(command):
            if command[2] == "ListUsers":
                return json.dumps({"Users": {"User": [{"UserName": "a"}]}, "IsTruncated": True})
            return json.dumps({"User": {}})

        self.assertEqual(len(collect_aliyun(runner=runner)), 1)

    def test_bad_json_from_cli_is_a_clear_error(self):
        with self.assertRaises(CollectError) as ctx:
            collect_aliyun(runner=lambda c: "not json")
        self.assertIn("JSON", str(ctx.exception))

    def test_load_json_roundtrip(self):
        payload = {
            "platform": "volcano",
            "account": "default",
            "users": [{"name": "x", "email": "x@wuji.tech", "display_name": "小 X"}],
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "u.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            users = load_json(str(path))
        self.assertEqual(users[0].platform, "volcano")
        self.assertEqual(users[0].email, "x@wuji.tech")

    def test_load_json_rejects_missing_name(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "u.json"
            path.write_text(json.dumps({"users": [{"email": "a@b"}]}), encoding="utf-8")
            with self.assertRaises(CollectError):
                load_json(str(path))

    def test_load_json_rejects_missing_users_array(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "u.json"
            path.write_text(json.dumps({"platform": "x"}), encoding="utf-8")
            with self.assertRaises(CollectError):
                load_json(str(path))

    def test_missing_file_is_a_clear_error(self):
        with self.assertRaises(CollectError):
            load_json("/nonexistent/users.json")


if __name__ == "__main__":
    unittest.main()
