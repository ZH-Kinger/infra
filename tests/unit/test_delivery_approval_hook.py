"""飞书审批回调。

这是个**免登录的公网 POST 入口**，所以测试的重点全在「谁能触发它」上。
"""

import unittest

from delivery.approval_hook import DEDUP_WINDOW, Hook


class TokenTests(unittest.TestCase):
    def test_nothing_passes_when_no_token_is_configured(self):
        """没配 token 就拒绝一切。不是「先放行等配好」——
        一个没有鉴权的公网入口，谁都能拿它触发同步。"""
        hook = Hook(verify_token="")
        self.assertFalse(hook.configured)
        self.assertFalse(hook.check_token({"token": "anything"}))
        self.assertFalse(hook.check_token({"header": {"token": ""}}))
        self.assertFalse(hook.check_token({}))

    def test_both_schema_versions_are_accepted(self):
        """2.0 在 header.token，旧版在顶层 token。只看一处的话另一种每次都被拒，
        而表现是「回调好像没生效」。"""
        hook = Hook(verify_token="secret")
        self.assertTrue(hook.check_token({"header": {"token": "secret"}}))
        self.assertTrue(hook.check_token({"token": "secret"}))

    def test_a_wrong_token_is_refused(self):
        hook = Hook(verify_token="secret")
        self.assertFalse(hook.check_token({"token": "secre"}))
        self.assertFalse(hook.check_token({"token": "secret "}))
        self.assertFalse(hook.check_token({"token": ""}))

    def test_a_non_dict_body_does_not_blow_up(self):
        hook = Hook(verify_token="secret")
        for body in ([], "x", None, 3):
            self.assertFalse(hook.check_token(body))
            self.assertEqual(hook.instance_of(body), "")
            self.assertIsNone(hook.challenge(body))


class ChallengeTests(unittest.TestCase):
    def test_challenge_is_echoed_even_without_a_token(self):
        """飞书后台填地址时先发 challenge。这一步要能过，否则先有鸡还是先有蛋。"""
        hook = Hook(verify_token="")
        self.assertEqual(hook.challenge({"type": "url_verification", "challenge": "abc"}), "abc")

    def test_an_ordinary_event_is_not_a_challenge(self):
        hook = Hook(verify_token="t")
        self.assertIsNone(hook.challenge({"event": {"instance_code": "ABC123"}}))


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.hook = Hook(verify_token="t")

    def test_it_finds_the_instance_in_the_usual_places(self):
        for body in (
            {"event": {"instance_code": "ABC-123_x"}},
            {"event": {"object": {"instance_code": "ABC-123_x"}}},
            {"instance_code": "ABC-123_x"},
            {"event": {"instance_id": "ABC-123_x"}},
        ):
            self.assertEqual(self.hook.instance_of(body), "ABC-123_x", body)

    def test_a_malformed_instance_is_ignored_not_passed_through(self):
        """实例号会被拿去查台账。外部数据不该决定查什么形状的键。"""
        for bad in ("../../etc", "a b", "x" * 200, "", "短"):
            self.assertEqual(self.hook.instance_of({"event": {"instance_code": bad}}), "")


class DedupTests(unittest.TestCase):
    def test_a_replay_within_the_window_is_dropped(self):
        """飞书没收到 200 会重发。重复同步会重复打飞书接口。"""
        now = [1000.0]
        hook = Hook(verify_token="t", clock=lambda: now[0])
        self.assertTrue(hook.claim("INST"))
        self.assertFalse(hook.claim("INST"))
        now[0] += DEDUP_WINDOW + 1
        self.assertTrue(hook.claim("INST"))

    def test_different_instances_do_not_block_each_other(self):
        hook = Hook(verify_token="t")
        self.assertTrue(hook.claim("A"))
        self.assertTrue(hook.claim("B"))

    def test_an_empty_instance_is_never_claimed(self):
        self.assertFalse(Hook(verify_token="t").claim(""))

    def test_the_dedup_table_does_not_grow_without_bound(self):
        now = [1000.0]
        hook = Hook(verify_token="t", clock=lambda: now[0])
        for i in range(2100):
            now[0] += DEDUP_WINDOW + 1
            hook.claim(f"INST{i}")
        self.assertLess(len(hook._seen), 2100)


class AllowlistTests(unittest.TestCase):
    """只处理面板自己那个审批定义的事件。

    别人的请假、报销不该进来 —— 飞书按定义订阅，理论上本来也不会推，
    但「理论上不会来」和「来了也不处理」是两件事。
    """

    CODE = "301E99EB-A4BC-4F08-AFAF-46906A006C08"

    def hook(self, codes=None):
        return Hook(verify_token="t", codes=self.CODE if codes is None else codes)

    def test_our_own_definition_passes(self):
        h = Hook(verify_token="t", codes=[self.CODE])
        self.assertTrue(h.mine({"event": {"approval_code": self.CODE}}))
        self.assertTrue(h.mine({"event": {"object": {"approval_code": self.CODE}}}))
        self.assertTrue(h.mine({"approval_code": self.CODE}))

    def test_somebody_elses_leave_request_is_dropped(self):
        h = Hook(verify_token="t", codes=[self.CODE])
        self.assertFalse(h.mine({"event": {"approval_code": "SOMEONE-ELSES-LEAVE"}}))

    def test_an_event_without_a_definition_code_is_not_ours(self):
        h = Hook(verify_token="t", codes=[self.CODE])
        self.assertFalse(h.mine({"event": {"instance_code": "ABC123"}}))

    def test_nothing_passes_before_the_allowlist_is_configured(self):
        """fail-closed。「先放行等配好」意味着这期间全公司的审批都会打进来。"""
        h = Hook(verify_token="t", codes=())
        self.assertFalse(h.mine({"event": {"approval_code": self.CODE}}))

    def test_a_non_dict_body_does_not_blow_up(self):
        h = Hook(verify_token="t", codes=[self.CODE])
        for body in ([], "x", None, 3):
            self.assertFalse(h.mine(body))


if __name__ == "__main__":
    unittest.main()
