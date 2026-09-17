"""飞书 OAuth 服务端部分的**真 HTTP 往返**。

单测把 `_post_json` 换成假函数能验逻辑，但验不了真正会翻车的那一层：
请求体长什么样、头带没带、飞书那种「HTTP 200 但 code != 0」的错怎么处理。
这里起一个站在 localhost 的假飞书，让真实的 urllib 代码整条跑一遍。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from delivery import feishu
from delivery.feishu import FeishuError, exchange_code, fetch_user

_SECRET = "super-secret-value-do-not-leak"


class _FakeFeishu(BaseHTTPRequestHandler):
    token_response = (200, {"code": 0, "access_token": "u-token-abc"})
    user_response = (
        200,
        {
            "code": 0,
            "data": {
                "open_id": "ou_x",
                "union_id": "on_y",
                "name": "李四",
                "enterprise_email": "li.si@wuji.tech",
            },
        },
    )
    seen: dict = {}

    def log_message(self, *a):  # 别把回调 URL 打进测试输出
        pass

    def _reply(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _FakeFeishu.seen["token_body"] = json.loads(self.rfile.read(n) or b"{}")
        _FakeFeishu.seen["token_headers"] = dict(self.headers)
        self._reply(*_FakeFeishu.token_response)

    def do_GET(self):
        _FakeFeishu.seen["user_headers"] = dict(self.headers)
        self._reply(*_FakeFeishu.user_response)


class RoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _FakeFeishu)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls._token, cls._user = feishu.TOKEN_URL, feishu.USER_INFO_URL
        feishu.TOKEN_URL = base + "/token"
        feishu.USER_INFO_URL = base + "/user"

    @classmethod
    def tearDownClass(cls):
        feishu.TOKEN_URL, feishu.USER_INFO_URL = cls._token, cls._user
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _FakeFeishu.seen = {}
        _FakeFeishu.token_response = (200, {"code": 0, "access_token": "u-token-abc"})
        _FakeFeishu.user_response = (
            200,
            {
                "code": 0,
                "data": {
                    "open_id": "ou_x",
                    "union_id": "on_y",
                    "name": "李四",
                    "enterprise_email": "li.si@wuji.tech",
                },
            },
        )

    def _exchange(self, **kw):
        args = dict(
            app_id="cli_test",
            app_secret=_SECRET,
            code="auth-code-1",
            redirect_uri="http://localhost:8765/auth/callback",
            code_verifier="verifier-1",
        )
        args.update(kw)
        return exchange_code(**args)

    # ── 请求形状 ────────────────────────────────────────────────────────
    def test_token_request_carries_every_required_field(self):
        self.assertEqual(self._exchange(), "u-token-abc")
        body = _FakeFeishu.seen["token_body"]
        self.assertEqual(body["grant_type"], "authorization_code")
        self.assertEqual(body["client_id"], "cli_test")
        self.assertEqual(body["client_secret"], _SECRET)
        self.assertEqual(body["code"], "auth-code-1")
        self.assertEqual(body["redirect_uri"], "http://localhost:8765/auth/callback")

    def test_pkce_verifier_is_sent_when_present(self):
        self._exchange()
        self.assertEqual(_FakeFeishu.seen["token_body"]["code_verifier"], "verifier-1")

    def test_verifier_is_omitted_not_sent_empty(self):
        """空串会被飞书当成一次失败的 PKCE 校验，不如不发。"""
        self._exchange(code_verifier="")
        self.assertNotIn("code_verifier", _FakeFeishu.seen["token_body"])

    def test_content_type_is_json(self):
        self._exchange()
        ct = _FakeFeishu.seen["token_headers"].get("Content-Type", "")
        self.assertIn("application/json", ct)

    def test_user_info_sends_bearer_token(self):
        fetch_user("u-token-abc")
        self.assertEqual(
            _FakeFeishu.seen["user_headers"].get("Authorization"), "Bearer u-token-abc"
        )

    # ── 响应解析 ────────────────────────────────────────────────────────
    def test_user_is_parsed_with_enterprise_email(self):
        """企业邮箱是身份映射的连接键——丢了它整条 SSO 就接不上云账号。"""
        user = fetch_user("t")
        self.assertEqual(user.email, "li.si@wuji.tech")
        self.assertEqual(user.identity, "on_y")  # union_id 优先
        self.assertEqual(user.name, "李四")

    def test_identity_falls_back_to_open_id(self):
        _FakeFeishu.user_response = (
            200,
            {"code": 0, "data": {"open_id": "ou_only", "name": "x"}},
        )
        self.assertEqual(fetch_user("t").identity, "ou_only")

    def test_user_without_any_id_is_rejected(self):
        _FakeFeishu.user_response = (200, {"code": 0, "data": {"name": "x"}})
        with self.assertRaises(FeishuError):
            fetch_user("t")

    # ── 错误路径 ────────────────────────────────────────────────────────
    def test_http_200_with_nonzero_code_is_an_error(self):
        """飞书最常见的报错形状：HTTP 200，code 非 0。当成成功会把空 token 传下去。"""
        _FakeFeishu.token_response = (200, {"code": 20029, "msg": "redirect_uri mismatch"})
        with self.assertRaises(FeishuError) as ctx:
            self._exchange()
        self.assertIn("20029", str(ctx.exception))

    def test_http_error_surfaces_feishu_code_and_msg(self):
        _FakeFeishu.token_response = (400, {"code": 20001, "msg": "bad app secret"})
        with self.assertRaises(FeishuError) as ctx:
            self._exchange()
        self.assertIn("20001", str(ctx.exception))

    def test_missing_access_token_is_rejected(self):
        _FakeFeishu.token_response = (200, {"code": 0})
        with self.assertRaises(FeishuError):
            self._exchange()

    def test_user_info_permission_error_says_so(self):
        _FakeFeishu.user_response = (200, {"code": 99991672, "msg": "no permission"})
        with self.assertRaises(FeishuError) as ctx:
            fetch_user("t")
        self.assertIn("权限", str(ctx.exception))

    def test_unreachable_provider_is_a_clear_error(self):
        saved = feishu.TOKEN_URL
        feishu.TOKEN_URL = "http://127.0.0.1:1/token"
        try:
            with self.assertRaises(FeishuError) as ctx:
                self._exchange()
            self.assertIn("连不上飞书", str(ctx.exception))
        finally:
            feishu.TOKEN_URL = saved

    # ── 泄漏 ────────────────────────────────────────────────────────────
    def test_error_message_never_contains_the_secret_or_the_code(self):
        """错误会被打进日志、贴进工单。请求体里有 client_secret 和授权码。"""
        for resp in ((400, {"code": 1, "msg": "x"}), (200, {"code": 1, "msg": "x"})):
            _FakeFeishu.token_response = resp
            with self.assertRaises(FeishuError) as ctx:
                self._exchange()
            text = str(ctx.exception)
            self.assertNotIn(_SECRET, text)
            self.assertNotIn("auth-code-1", text)

    def test_missing_credentials_fail_before_any_network_call(self):
        with self.assertRaises(FeishuError) as ctx:
            self._exchange(app_secret="")
        self.assertIn("App Secret", str(ctx.exception))
        self.assertEqual(_FakeFeishu.seen, {})


if __name__ == "__main__":
    unittest.main()
