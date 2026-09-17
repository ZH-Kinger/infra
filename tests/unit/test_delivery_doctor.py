"""飞书应用体检。

判定逻辑里最容易写反的一条：**「其它错误码」要算权限通过**。飞书的权限校验发生在
参数校验之前，所以探针故意传最小/无效参数——回「用户不存在」恰恰证明已经越过权限
闸门。把它判成失败，体检就会天天报一堆假红，人很快学会忽略整个报告。
"""

from __future__ import annotations

import unittest

from delivery.doctor import (
    DENIED,
    FAILED,
    NOTE,
    OK,
    DoctorError,
    render,
    run,
    tenant_token,
)

TOKEN_PATH = "/auth/v3/tenant_access_token/internal"


class _Fake:
    """按 (method, path 片段) 返回预置响应。"""

    def __init__(self, responses=None, token_ok=True):
        self.responses = responses or {}
        self.token_ok = token_ok
        self.calls = []

    def __call__(self, method, url, headers, payload):
        self.calls.append((method, url, headers, payload))
        if TOKEN_PATH in url:
            if not self.token_ok:
                return 200, {"code": 10003, "msg": "app not found"}
            return 200, {"code": 0, "tenant_access_token": "t-abc"}
        for key, resp in self.responses.items():
            if key in url:
                return resp
        return 200, {"code": 0}


class TokenTests(unittest.TestCase):
    def test_missing_credentials_fail_before_any_call(self):
        fake = _Fake()
        with self.assertRaises(DoctorError):
            tenant_token(app_id="", app_secret="x", caller=fake)
        self.assertEqual(fake.calls, [])

    def test_bad_credentials_say_what_to_check(self):
        with self.assertRaises(DoctorError) as ctx:
            tenant_token(app_id="a", app_secret="b", caller=_Fake(token_ok=False))
        msg = str(ctx.exception)
        self.assertIn("app_id", msg)
        self.assertIn("发布版本", msg)

    def test_secret_is_never_echoed_back(self):
        """体检报告会被贴进群和工单。"""
        with self.assertRaises(DoctorError) as ctx:
            tenant_token(app_id="a", app_secret="SECRET-XYZ", caller=_Fake(token_ok=False))
        self.assertNotIn("SECRET-XYZ", str(ctx.exception))


class ProbeTests(unittest.TestCase):
    def test_denied_is_reported_with_feishus_own_wording(self):
        fake = _Fake(
            {
                "im/v1/messages": (
                    403,
                    {"code": 99991672, "msg": "需要以下任一权限：im:message:send_as_bot"},
                )
            }
        )
        probes = run(app_id="a", app_secret="b", caller=fake)
        p = next(x for x in probes if x.name == "im/v1/messages")
        self.assertEqual(p.status, DENIED)
        self.assertIn("im:message:send_as_bot", p.detail)

    def test_other_error_codes_count_as_permission_ok(self):
        """核心：探针传的是不存在的 receive_id，「用户不存在」证明越过了权限闸门。"""
        fake = _Fake({"im/v1/messages": (400, {"code": 230001, "msg": "user not found"})})
        p = next(
            x for x in run(app_id="a", app_secret="b", caller=fake) if x.name == "im/v1/messages"
        )
        self.assertEqual(p.status, OK)

    def test_code_zero_is_ok(self):
        p = next(
            x for x in run(app_id="a", app_secret="b", caller=_Fake()) if x.name == "im/v1/chats"
        )
        self.assertEqual(p.status, OK)

    def test_message_probe_targets_a_nonexistent_user(self):
        """探针绝不能真的给人发一条 'probe'。"""
        fake = _Fake()
        run(app_id="a", app_secret="b", caller=fake)
        sent = [c for c in fake.calls if "im/v1/messages" in c[1]]
        self.assertEqual(len(sent), 1)
        self.assertIn("nonexistent", sent[0][3]["receive_id"])

    def test_network_failure_is_marked_failed_not_ok(self):
        def boom(method, url, headers, payload):
            if TOKEN_PATH in url:
                return 200, {"code": 0, "tenant_access_token": "t"}
            raise DoctorError("连不上飞书：timeout")

        p = next(
            x for x in run(app_id="a", app_secret="b", caller=boom) if x.name == "im/v1/messages"
        )
        self.assertEqual(p.status, FAILED)

    def test_probes_carry_the_bearer_token(self):
        fake = _Fake()
        run(app_id="a", app_secret="b", caller=fake)
        probe_calls = [c for c in fake.calls if TOKEN_PATH not in c[1]]
        self.assertTrue(probe_calls)
        for _, _, headers, _ in probe_calls:
            self.assertEqual(headers["Authorization"], "Bearer t-abc")


class RenderTests(unittest.TestCase):
    def test_missing_scopes_are_listed_for_copy_paste(self):
        fake = _Fake({"im/v1/messages": (403, {"code": 99991672, "msg": "denied"})})
        text = render(run(app_id="a", app_secret="b", caller=fake))
        self.assertIn("im:message:send_as_bot", text)
        self.assertIn("创建版本并发布", text)

    def test_denied_rows_sort_first(self):
        fake = _Fake({"im/v1/chats": (403, {"code": 99991672, "msg": "denied"})})
        text = render(run(app_id="a", app_secret="b", caller=fake))
        self.assertLess(text.index("im/v1/chats"), text.index("im/v1/messages"))

    def test_all_clear_still_warns_about_the_user_scope(self):
        """应用身份全通不代表能登录——企业邮箱那条是用户身份权限，探不到。"""
        text = render(run(app_id="a", app_secret="b", caller=_Fake()))
        self.assertIn("全通", text)
        self.assertIn("contact:user.employee:readonly", text)
        self.assertIn("飞书邮箱服务", text)

    def test_report_never_contains_the_token(self):
        text = render(run(app_id="a", app_secret="b", caller=_Fake()))
        self.assertNotIn("t-abc", text)


if __name__ == "__main__":
    unittest.main()


class SendTestTests(unittest.TestCase):
    """真发一条的判定。

    这一组全部来自一次真实误报：飞书回 `230001 invalid receive_id`（参数错，
    说明权限闸门**已经越过**），旧代码把它归成 FAILED，报告于是写着
    「要申请的 scope：im:message:send_as_bot」——让人去申请一条自己已经有的权限。
    """

    def _run(self, resp):
        fake = _Fake({"receive_id_type=email": resp})
        return run(app_id="a", app_secret="b", caller=fake, send_to="x@wuji.tech")

    def _send_probe(self, probes):
        return next(p for p in probes if "真发" in p.name)

    def test_parameter_error_is_not_a_missing_scope(self):
        p = self._send_probe(self._run((400, {"code": 230001, "msg": "invalid receive_id"})))
        self.assertEqual(p.status, NOTE)
        self.assertFalse(p.needs_scope)

    def test_report_does_not_tell_you_to_apply_for_a_scope_you_have(self):
        text = render(self._run((400, {"code": 230001, "msg": "invalid receive_id"})))
        self.assertNotIn("要申请的 scope", text)

    def test_invalid_receive_id_explains_the_real_cause(self):
        p = self._send_probe(self._run((400, {"code": 230001, "msg": "invalid receive_id"})))
        self.assertIn("open_id", p.detail)

    def test_real_denial_still_reports_the_scope(self):
        text = render(self._run((403, {"code": 99991672, "msg": "no permission"})))
        self.assertIn("要申请的 scope", text)
        self.assertIn("im:message:send_as_bot", text)

    def test_success_says_enterprise_email_works(self):
        p = self._send_probe(self._run((200, {"code": 0})))
        self.assertEqual(p.status, OK)
        self.assertIn("receive_id", p.detail)

    def test_network_failure_is_not_a_missing_scope_either(self):
        def boom(method, url, headers, payload):
            if TOKEN_PATH in url:
                return 200, {"code": 0, "tenant_access_token": "t"}
            if "receive_id_type=email" in url:
                raise DoctorError("连不上飞书：timeout")
            return 200, {"code": 0}

        probes = run(app_id="a", app_secret="b", caller=boom, send_to="x@wuji.tech")
        text = render(probes)
        self.assertNotIn("要申请的 scope", text)
        self.assertIn("不是权限问题", text)

    def test_no_send_without_the_flag(self):
        fake = _Fake()
        run(app_id="a", app_secret="b", caller=fake)
        self.assertFalse([c for c in fake.calls if "receive_id_type=email" in c[1]])

    def test_send_targets_exactly_the_given_address(self):
        fake = _Fake()
        run(app_id="a", app_secret="b", caller=fake, send_to="only.me@wuji.tech")
        sent = [c for c in fake.calls if "receive_id_type=email" in c[1]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][3]["receive_id"], "only.me@wuji.tech")
