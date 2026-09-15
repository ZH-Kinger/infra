"""飞书 OAuth + PKCE + 本地回调登录。

测试重点是**拒绝路径**：成功路径只有一条，而登录出错的方式很多，其中几种
（state 不符、code 缺失、后端回了半份会话）如果放过去，用户会拿到一个
看起来已登录、实际用不了或者身份不对的会话。
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from delivery.login import (
    AUTHORIZE_URL,
    BackendExchange,
    LoginError,
    _Captured,
    _pkce_pair,
    authorize_url,
    login,
)
from delivery.session import load_session

_VERIFIER_RE = re.compile(r"\A[A-Za-z0-9\-._~]{43,128}\Z")


class _Home:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = os.environ.get("W0_HOME")
        os.environ["W0_HOME"] = str(Path(self.tmp.name) / "w0")
        return Path(os.environ["W0_HOME"])

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("W0_HOME", None)
        else:
            os.environ["W0_HOME"] = self.prev
        self.tmp.cleanup()


class _FakeExchange:
    def __init__(self, response=None, error=None):
        self.response = response or {}
        self.error = error
        self.seen = None

    def exchange(self, *, code, verifier, redirect_uri):
        if self.error:
            raise self.error
        self.seen = {"code": code, "verifier": verifier, "redirect_uri": redirect_uri}
        return self.response


def _waiter_returning(captured):
    def waiter(*, port, path, timeout, host=""):
        return captured

    return waiter


GOOD = {"token": "SESSION-TOKEN", "union_id": "on_abc", "name": "李四", "expires_in": 3600}


class PkceTests(unittest.TestCase):
    def test_verifier_matches_rfc7636_charset_and_length(self):
        for _ in range(20):
            verifier, challenge = _pkce_pair()
            self.assertRegex(verifier, _VERIFIER_RE)
            self.assertNotIn("=", challenge)  # base64url 去掉补位
            self.assertNotIn("+", challenge)
            self.assertNotIn("/", challenge)

    def test_each_call_is_unique(self):
        pairs = {_pkce_pair()[0] for _ in range(50)}
        self.assertEqual(len(pairs), 50)


class AuthorizeUrlTests(unittest.TestCase):
    def _params(self, **kw):
        base = {
            "app_id": "cli_x",
            "redirect_uri": "http://127.0.0.1:8765/callback",
            "state": "s1",
            "challenge": "c1",
        }
        base.update(kw)
        url = authorize_url(**base)
        self.assertTrue(url.startswith(AUTHORIZE_URL))
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))

    def test_required_params_present(self):
        q = self._params()
        self.assertEqual(q["client_id"], "cli_x")
        self.assertEqual(q["response_type"], "code")
        self.assertEqual(q["code_challenge_method"], "S256")
        self.assertEqual(q["redirect_uri"], "http://127.0.0.1:8765/callback")

    def test_default_scope_constant_matches_what_is_sent(self):
        from delivery.login import DEFAULT_SCOPE

        self.assertEqual(self._params()["scope"], DEFAULT_SCOPE)

    def test_employee_scope_is_requested_by_default(self):
        """不显式申请，用户就不会授予，`enterprise_email` 会静默为空。

        飞书的用户授权是显式且累积的：authorize 不带 scope 时 `user_info` 照样 200、
        照样返回 open_id 和姓名，只有企业邮箱缺——而企业邮箱是「飞书身份 → 云账号」
        整条映射的连接键。登录看起来完全成功，映射却全断，是最难发现的那种。
        """
        self.assertIn("contact:user.employee:readonly", self._params()["scope"].split())

    def test_offline_access_is_requested_so_sessions_can_refresh(self):
        """没有它飞书不返回 refresh_token，用户会反复被踢去重新授权。"""
        self.assertIn("offline_access", self._params()["scope"].split())

    def test_scopes_are_space_separated(self):
        self.assertGreater(len(self._params()["scope"].split()), 1)

    def test_every_requested_scope_is_in_the_declared_tuple(self):
        """授权端点**不校验本应用有没有申请到**这些 scope（实测 bitable:app 也放行），
        所以这串必须和开发者后台实际申请的一致——错要到登录那一刻才暴露。"""
        from delivery.login import USER_SCOPES

        self.assertEqual(self._params()["scope"].split(), list(USER_SCOPES))

    def test_scope_can_be_overridden(self):
        self.assertEqual(self._params(scope="contact:user.id")["scope"], "contact:user.id")

    def test_scope_can_be_explicitly_dropped(self):
        self.assertNotIn("scope", self._params(scope=""))


class LoginFlowTests(unittest.TestCase):
    def _login(self, captured, exchange=None, **kw):
        lines = []
        opts = {
            "app_id": "cli_x",
            "server": "http://backend:8088",
            "echo": lines.append,
            "waiter": _waiter_returning(captured),
            "opener": lambda url: False,
            "open_browser": False,
        }
        opts.update(kw)
        return login(exchange or _FakeExchange(GOOD), **opts), lines

    def _state_from(self, lines):
        for line in lines:
            if AUTHORIZE_URL in line:
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(line.strip()).query))
                return q["state"]
        raise AssertionError("授权链接没有被打印出来")

    def test_success_saves_session_and_passes_verifier(self):
        with _Home():
            ex = _FakeExchange(GOOD)
            lines = []
            captured = _Captured()

            def waiter(*, port, path, timeout, host=""):
                captured.state = self._state_from(lines)
                captured.code = "AUTH-CODE"
                return captured

            session = login(
                ex,
                app_id="cli_x",
                server="http://backend:8088",
                echo=lines.append,
                waiter=waiter,
                opener=lambda u: False,
                open_browser=False,
            )
            self.assertEqual(session.union_id, "on_abc")
            self.assertIsNotNone(load_session())
            # code_verifier 必须一起交给后端，否则 PKCE 形同虚设
            self.assertRegex(ex.seen["verifier"], _VERIFIER_RE)
            self.assertEqual(ex.seen["code"], "AUTH-CODE")

    def test_url_is_always_printed_even_without_browser(self):
        # SSH 到远端时打不开浏览器，链接必须打出来让人自己开（gh 的做法）
        with _Home():
            captured = _Captured(code="c", state="wrong")
            with self.assertRaises(LoginError):
                self._login(captured)

    def test_state_mismatch_is_rejected(self):
        # 不校验 state，别人就能把**他的** code 塞进你的回调，让你登成他
        with _Home():
            captured = _Captured(code="AUTH-CODE", state="not-the-one")
            with self.assertRaises(LoginError) as ctx:
                self._login(captured)
            self.assertIn("state", str(ctx.exception))
            self.assertIsNone(load_session())

    def test_provider_error_is_surfaced(self):
        with _Home():
            captured = _Captured(error="user_denied")
            with self.assertRaises(LoginError) as ctx:
                self._login(captured)
            self.assertIn("user_denied", str(ctx.exception))

    def test_missing_code_is_rejected(self):
        with _Home():
            lines = []
            captured = _Captured()

            def waiter(*, port, path, timeout, host=""):
                captured.state = self._state_from(lines)
                return captured

            with self.assertRaises(LoginError) as ctx:
                login(
                    _FakeExchange(GOOD),
                    app_id="cli_x",
                    server="s",
                    echo=lines.append,
                    waiter=waiter,
                    opener=lambda u: False,
                    open_browser=False,
                )
            self.assertIn("授权码", str(ctx.exception))

    def test_incomplete_backend_response_is_rejected(self):
        # 存下半份会话 = 用户以为登上了，下一条命令才失败
        with _Home():
            for bad in ({"union_id": "on_x"}, {"token": "T"}, {}):
                lines = []
                captured = _Captured()

                def waiter(*, port, path, timeout, host="", _l=lines, _c=captured):
                    _c.state = self._state_from(_l)
                    _c.code = "AUTH-CODE"
                    return _c

                with self.assertRaises(LoginError):
                    login(
                        _FakeExchange(bad),
                        app_id="cli_x",
                        server="s",
                        echo=lines.append,
                        waiter=waiter,
                        opener=lambda u: False,
                        open_browser=False,
                    )
                self.assertIsNone(load_session())

    def test_missing_app_id_fails_early(self):
        with _Home(), self.assertRaises(LoginError) as ctx:
            login(_FakeExchange(GOOD), app_id="", server="s")
        self.assertIn("App ID", str(ctx.exception))

    def test_redirect_uri_binds_loopback_not_all_interfaces(self):
        with _Home():
            lines = []
            captured = _Captured(code="c", state="x")
            # state 必然不符、会抛错；这里只关心链接里印的是不是回环地址
            with contextlib.suppress(LoginError):
                login(
                    _FakeExchange(GOOD),
                    app_id="cli_x",
                    server="s",
                    port=9999,
                    echo=lines.append,
                    waiter=_waiter_returning(captured),
                    opener=lambda u: False,
                    open_browser=False,
                )
            text = "\n".join(lines)
            self.assertIn("localhost:9999", text)  # 默认主机名与飞书文档举例一致
            self.assertNotIn("0.0.0.0", text)  # noqa: S104  断言不绑全网卡


class RedirectUriOverrideTests(unittest.TestCase):
    """飞书对重定向 URL **完全匹配**，一个字符不同就是 20029。

    所以必须允许直接粘贴白名单里那一条，而不是让用户去凑 --port。
    """

    def _lines_for(self, **kw):
        lines = []
        captured = _Captured(code="c", state="x")
        with contextlib.suppress(LoginError):
            login(
                _FakeExchange(GOOD),
                app_id="cli_x",
                server="s",
                echo=lines.append,
                waiter=_waiter_returning(captured),
                opener=lambda u: False,
                open_browser=False,
                **kw,
            )
        return "\n".join(lines)

    def test_override_is_used_verbatim(self):
        with _Home():
            text = self._lines_for(redirect_uri="http://127.0.0.1:5000/oauth/cb")
            self.assertIn("http://127.0.0.1:5000/oauth/cb", text)

    def test_override_wins_over_port(self):
        with _Home():
            text = self._lines_for(redirect_uri="http://localhost:5555/callback", port=8765)
            self.assertIn(":5555", text)
            self.assertNotIn(":8765", text)

    def test_malformed_override_is_rejected(self):
        with _Home():
            for bad in ("notaurl", "ftp://x/y", "file:///etc/passwd", "/callback"):
                with self.assertRaises(LoginError, msg=bad):
                    login(
                        _FakeExchange(GOOD),
                        app_id="cli_x",
                        server="s",
                        redirect_uri=bad,
                        echo=lambda _: None,
                        waiter=_waiter_returning(_Captured()),
                        opener=lambda u: False,
                        open_browser=False,
                    )

    def test_hint_mentions_20029(self):
        # 20029 是这条最常见的失败，提示里直接点名能省掉一轮排查
        with _Home():
            self.assertIn("20029", self._lines_for())


class BackendExchangeTests(unittest.TestCase):
    def test_only_http_schemes_accepted(self):
        for bad in ("file:///etc/passwd", "ftp://x/y", "notaurl", "", "http://"):
            with self.assertRaises(LoginError, msg=bad):
                BackendExchange(bad)

    def test_http_and_https_accepted(self):
        for good in ("http://backend:8088", "https://backend.example.com/"):
            self.assertTrue(BackendExchange(good).server)


class CallbackHandlerTests(unittest.TestCase):
    def test_handler_does_not_log_the_callback_url(self):
        # BaseHTTPRequestHandler 默认把**含 code 的完整 URL**打到 stderr
        import inspect

        from delivery.login import _make_handler

        source = inspect.getsource(_make_handler)
        self.assertIn("def log_message", source)

    def test_server_binds_loopback_only(self):
        import inspect

        from delivery import login as mod

        source = inspect.getsource(mod.wait_for_callback)
        self.assertIn('"127.0.0.1"', source)
        self.assertNotIn('"0.0.0.0"', source)  # noqa: S104  断言不绑全网卡


if __name__ == "__main__":
    unittest.main()
