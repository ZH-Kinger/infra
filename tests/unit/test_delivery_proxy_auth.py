"""公司 IAM 登录（oauth2-proxy 代理模式）：请求头认人、共享密钥、邮箱查询、路由开关。"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from delivery.errors import DeliveryError
from delivery.proxy_auth import (
    DEFAULT_LOGOUT_URL,
    H_NAME,
    H_SECRET,
    H_TOKEN,
    H_UNION_ID,
    LOGIN_URL,
    ProxyAuthConfig,
    ProxyAuthError,
    ProxyIdentity,
)
from delivery.registry import PlatformRegistry
from delivery.server import Backend, Store, make_handler, serve

SECRET = "s" * 40


def _headers(**kw):
    out = {H_SECRET: SECRET, H_UNION_ID: "on_1", H_NAME: "Alice"}
    out.update(kw)
    return {k: v for k, v in out.items() if v is not None}


class ConfigTests(unittest.TestCase):
    def test_short_secret_rejected(self):
        with self.assertRaises(ProxyAuthError):
            ProxyAuthConfig(secret="short")

    def test_secret_must_be_plain_ascii(self):
        for bad in ("密" * 40, " " + "s" * 40, "s" * 20 + " " + "s" * 20):
            with self.assertRaises(ProxyAuthError, msg=bad):
                ProxyAuthConfig(secret=bad)

    def test_userinfo_must_be_https_unless_loopback(self):
        dom = ("wuji.tech",)
        with self.assertRaises(ProxyAuthError):
            ProxyAuthConfig(
                secret=SECRET, userinfo_url="http://iam.example.com/userinfo", email_domains=dom
            )
        ProxyAuthConfig(secret=SECRET, userinfo_url="https://iam.example.com/u", email_domains=dom)
        ProxyAuthConfig(secret=SECRET, userinfo_url="http://127.0.0.1:18080/u", email_domains=dom)

    def test_userinfo_requires_email_domains(self):
        with self.assertRaises(ProxyAuthError):
            ProxyAuthConfig(secret=SECRET, userinfo_url="https://iam.example.com/userinfo")
        cfg = ProxyAuthConfig.from_env(
            {
                "DELIVERY_PROXY_SECRET": SECRET,
                "DELIVERY_IAM_USERINFO_URL": "https://iam.example.com/userinfo",
                "DELIVERY_IAM_EMAIL_DOMAINS": " @Wuji.Tech , ",
            }
        )
        self.assertEqual(cfg.email_domains, ("wuji.tech",))

    def test_logout_must_be_site_path(self):
        for bad in ("https://evil.example.com", "//evil.example.com", "/a\\b"):
            with self.assertRaises(ProxyAuthError, msg=bad):
                ProxyAuthConfig(secret=SECRET, logout_url=bad)
        ProxyAuthConfig(secret=SECRET, logout_url="/oauth2/sign_out?rd=https%3A%2F%2Fiam")

    def test_from_env_requires_secret(self):
        with self.assertRaises(ProxyAuthError):
            ProxyAuthConfig.from_env({})

    def test_from_env_secret_and_file_are_exclusive(self):
        d = Path(tempfile.mkdtemp())
        (d / "secret").write_text(SECRET + "\n", encoding="utf-8")
        cfg = ProxyAuthConfig.from_env({"DELIVERY_PROXY_SECRET_FILE": str(d / "secret")})
        self.assertEqual(cfg.secret, SECRET)
        self.assertEqual(cfg.logout_url, DEFAULT_LOGOUT_URL)
        with self.assertRaises(ProxyAuthError):
            ProxyAuthConfig.from_env(
                {"DELIVERY_PROXY_SECRET": SECRET, "DELIVERY_PROXY_SECRET_FILE": str(d / "secret")}
            )


class IdentityTests(unittest.TestCase):
    def _identity(self, fetch=None, userinfo="https://iam.example.com/userinfo", clock=None):
        calls = []

        def default_fetch(url, token):
            calls.append((url, token))
            return {"feishu_union_id": "on_1", "email": "Alice@Wuji.Tech", "email_verified": True}

        kw = {"fetch": fetch or default_fetch}
        if clock:
            kw["clock"] = clock
        cfg = ProxyAuthConfig(
            secret=SECRET, userinfo_url=userinfo, email_domains=("wuji.tech",) if userinfo else ()
        )
        return ProxyIdentity(cfg, **kw), calls

    def test_valid_headers_give_user_without_email(self):
        ident, calls = self._identity()
        user = ident.user(_headers(**{H_TOKEN: "tok"}))
        self.assertEqual(user.union_id, "on_1")
        self.assertEqual(user.name, "Alice")
        # 认人不查 userinfo，也不带邮箱：管理员判断只认 union_id
        self.assertEqual(user.enterprise_email, "")
        self.assertEqual(calls, [])

    def test_missing_or_wrong_secret_is_anonymous(self):
        ident, _ = self._identity()
        self.assertIsNone(ident.user(_headers(**{H_SECRET: None})))
        self.assertIsNone(ident.user(_headers(**{H_SECRET: "x" * 40})))
        self.assertIsNone(ident.user(_headers(**{H_SECRET: SECRET + " "})))

    def test_header_names_case_insensitive(self):
        from email.message import Message

        msg = Message()
        msg["x-panel-proxy-secret"] = SECRET
        msg["X-PANEL-UNION-ID"] = "on_1"
        ident, _ = self._identity()
        self.assertEqual(ident.user(msg).union_id, "on_1")

    def test_bad_union_id_is_anonymous(self):
        ident, _ = self._identity()
        for bad in ("", "  ", "on 1", "on_1;drop", "a" * 129):
            self.assertIsNone(ident.user(_headers(**{H_UNION_ID: bad})), bad)

    def test_utf8_name_from_proxy_restored(self):
        ident, _ = self._identity()
        raw = "李四".encode().decode("latin-1")  # http.server 的解码方式
        self.assertEqual(ident.user(_headers(**{H_NAME: raw})).name, "李四")

    def test_email_lowercased_and_cached(self):
        ident, calls = self._identity()
        for _ in range(3):
            email = ident.email(_headers(**{H_TOKEN: "tok"}), "on_1")
        self.assertEqual(email, "alice@wuji.tech")
        self.assertEqual(len(calls), 1)

    def test_email_rejected_when_not_trustworthy(self):
        cases = {
            "别人的 token": {
                "feishu_union_id": "on_2",
                "email": "a@wuji.tech",
                "email_verified": True,
            },
            "缺 union_id": {"email": "a@wuji.tech", "email_verified": True},
            "未验证": {"feishu_union_id": "on_1", "email": "a@wuji.tech", "email_verified": False},
            "缺 verified": {"feishu_union_id": "on_1", "email": "a@wuji.tech"},
            "verified 是字符串": {
                "feishu_union_id": "on_1",
                "email": "a@wuji.tech",
                "email_verified": "true",
            },
            "外部域名": {"feishu_union_id": "on_1", "email": "a@gmail.com", "email_verified": True},
            "子域名": {"feishu_union_id": "on_1", "email": "a@x.wuji.tech", "email_verified": True},
            "非字符串": {
                "feishu_union_id": "on_1",
                "email": ["a@wuji.tech"],
                "email_verified": True,
            },
        }
        for label, data in cases.items():
            ident, _ = self._identity(fetch=lambda url, tok, d=data: d)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ident.email(_headers(**{H_TOKEN: "tok"}), "on_1"), "", label)

    def test_cache_does_not_cross_union_ids(self):
        ident, calls = self._identity()
        self.assertEqual(ident.email(_headers(**{H_TOKEN: "tok"}), "on_1"), "alice@wuji.tech")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ident.email(_headers(**{H_TOKEN: "tok"}), "on_2"), "")
        self.assertEqual(len(calls), 2)

    def test_userinfo_failure_is_empty_and_does_not_log_token(self):
        errors = [
            OSError("connection refused Bearer tok-secret"),
            http.client.IncompleteRead(b"Bearer tok-secret"),
            ValueError("bad json tok-secret"),
        ]
        for exc in errors:

            def boom(url, tok, exc=exc):
                raise exc

            ident, _ = self._identity(fetch=boom)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(ident.email(_headers(**{H_TOKEN: "tok-secret"}), "on_1"), "")
            self.assertNotIn("tok-secret", err.getvalue())
            self.assertIn(type(exc).__name__, err.getvalue())

    def test_failure_cached_for_a_minute(self):
        now = [1000.0]
        calls = []

        def boom(url, tok):
            calls.append(tok)
            raise OSError("down")

        ident, _ = self._identity(fetch=boom, clock=lambda: now[0])
        with contextlib.redirect_stderr(io.StringIO()):
            ident.email(_headers(**{H_TOKEN: "tok"}), "on_1")
            now[0] += 59
            ident.email(_headers(**{H_TOKEN: "tok"}), "on_1")
            self.assertEqual(len(calls), 1)
            now[0] += 2
            ident.email(_headers(**{H_TOKEN: "tok"}), "on_1")
        self.assertEqual(len(calls), 2)

    def test_success_cache_expires(self):
        now = [1000.0]
        ident, calls = self._identity(clock=lambda: now[0])
        ident.email(_headers(**{H_TOKEN: "tok"}), "on_1")
        now[0] += 301
        ident.email(_headers(**{H_TOKEN: "tok"}), "on_1")
        self.assertEqual(len(calls), 2)

    def test_cache_bounded(self):
        from delivery import proxy_auth

        ident, _ = self._identity()
        for i in range(proxy_auth._CACHE_MAX + 5):
            ident.email(_headers(**{H_TOKEN: f"tok{i}"}), "on_1")
        self.assertLessEqual(len(ident._emails), proxy_auth._CACHE_MAX)

    def test_no_userinfo_url_or_token_never_fetches(self):
        def fail(url, tok):
            raise AssertionError("不该查 userinfo")

        ident, _ = self._identity(fetch=fail, userinfo="")
        self.assertEqual(ident.email(_headers(**{H_TOKEN: "tok"}), "on_1"), "")
        ident, _ = self._identity(fetch=fail)
        self.assertEqual(ident.email(_headers(), "on_1"), "")


class FetchTests(unittest.TestCase):
    """真 HTTP：不跟随重定向（Authorization 会被带走），响应大小有上限。"""

    def _serve(self, handler_cls):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_redirect_not_followed(self):
        from http.server import BaseHTTPRequestHandler

        from delivery.proxy_auth import _fetch_userinfo

        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/userinfo":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        base = self._serve(Handler)
        with self.assertRaises(OSError):
            _fetch_userinfo(f"{base}/userinfo", "tok")
        self.assertEqual([p for p, _ in seen], ["/userinfo"])

    def test_oversized_body_rejected(self):
        from http.server import BaseHTTPRequestHandler

        from delivery.proxy_auth import _MAX_BODY, _fetch_userinfo

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b" " * (_MAX_BODY + 10))

            def log_message(self, *a):
                pass

        with self.assertRaises(ValueError):
            _fetch_userinfo(f"{self._serve(Handler)}/userinfo", "tok")


PEOPLE = {
    "schema": "wuji-people@1",
    "people": [
        {
            "union_id": "on_admin",
            "name": "管理员",
            "email": "admin@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": "100", "name": "boss"}],
        },
        {
            "union_id": "",
            "name": "新同事",
            "email": "xin.tongshi@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": "100", "name": "xints"}],
        },
    ],
}
SNAPSHOT = {
    "captured_at": "2026-09-15T10:00:00+08:00",
    "accounts": [
        {
            "platform": "aliyun",
            "account": "100",
            "users": [
                {"name": "boss", "policies": ["AdministratorAccess"]},
                {"name": "xints", "policies": []},
            ],
        }
    ],
}


class ProxyModeServerTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        (d / "people.json").write_text(json.dumps(PEOPLE, ensure_ascii=False), encoding="utf-8")
        (d / "inventory.json").write_text(json.dumps(SNAPSHOT), encoding="utf-8")
        admins = {"union_ids": ["on_admin"], "emails": ["admin@wuji.tech"]}
        (d / "admins.json").write_text(json.dumps(admins), encoding="utf-8")
        backend = Backend(
            inventory_path=str(d / "inventory.json"),
            people_path=str(d / "people.json"),
            bindings_path=str(d / "bindings.json"),
            admins_path=str(d / "admins.json"),
            platforms={"aliyun": "阿里云"},
        )
        self.dir = d
        emails = {"on_new": "xin.tongshi@wuji.tech", "on_mallory": "admin@wuji.tech"}
        self.fetches = []

        def fetch(url, tok):
            self.fetches.append(tok)
            return {"feishu_union_id": tok, "email": emails.get(tok, ""), "email_verified": True}

        ident = ProxyIdentity(
            ProxyAuthConfig(
                secret=SECRET,
                userinfo_url="https://iam.example.com/userinfo",
                logout_url="/oauth2/sign_out?rd=https%3A%2F%2Fiam.example.com%2Fend",
                email_domains=("wuji.tech",),
            ),
            fetch=fetch,
        )
        self.store = Store()
        handler = make_handler(
            PlatformRegistry.load(),
            self.store,
            app_id="cli_demo",
            app_secret="s3cret",
            base_url="http://127.0.0.1",
            backend=backend,
            proxy=ident,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _req(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request(method, path, body=b"{}" if method == "POST" else None, headers=headers or {})
        resp = conn.getresponse()
        out = (resp.status, resp.read().decode(errors="replace"))
        conn.close()
        return out

    def test_session_from_headers(self):
        status, body = self._req("GET", "/api/session", _headers(**{H_UNION_ID: "on_admin"}))
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["role"], "admin")
        self.assertTrue(data["logout_url"].startswith("/oauth2/sign_out"))

    def test_without_secret_is_anonymous_with_proxy_login_url(self):
        headers = _headers(**{H_SECRET: None, H_UNION_ID: "on_admin"})
        data = json.loads(self._req("GET", "/api/session", headers)[1])
        self.assertFalse(data["authenticated"])
        self.assertEqual(data["login_url"], LOGIN_URL)
        self.assertEqual(self._req("GET", "/api/admin/overview", headers)[0], 401)

    def test_feishu_cookie_session_not_accepted(self):
        from delivery.feishu import FeishuUser
        from delivery.server import COOKIE_NAME, _WebSession

        self.store.sessions["sid"] = _WebSession(user=FeishuUser("ou", "on_admin", "管理员"))
        status, _ = self._req("GET", "/api/admin/overview", {"Cookie": f"{COOKIE_NAME}=sid"})
        self.assertEqual(status, 401)

    def test_feishu_routes_closed(self):
        for path in ("/auth/login", "/auth/callback?code=x&state=y", "/auth/logout"):
            self.assertEqual(self._req("GET", path, _headers())[0], 404, path)
        self.assertEqual(self._req("POST", "/auth/exchange", _headers())[0], 404)

    def test_first_login_binds_by_iam_email(self):
        headers = _headers(**{H_UNION_ID: "on_new", H_TOKEN: "on_new"})
        status, body = self._req("GET", "/api/me", headers)
        self.assertEqual(status, 200, body)
        self.assertIn("xints", body)
        bindings = json.loads((self.dir / "bindings.json").read_text(encoding="utf-8"))
        self.assertIn("on_new", bindings["bindings"])
        # 绑定之后按 union_id 命中，不再查 userinfo
        self._req("GET", "/api/me", headers)
        self.assertEqual(self.fetches, ["on_new"])

    def test_no_usable_iam_email_note_names_iam(self):
        headers = _headers(**{H_UNION_ID: "on_x", H_TOKEN: "on_x"})
        status, body = self._req("GET", "/api/me", headers)
        self.assertEqual(status, 200, body)
        self.assertIn("公司 IAM", json.loads(body)["binding_note"])

    def test_bound_user_never_fetches_userinfo(self):
        headers = _headers(**{H_UNION_ID: "on_admin", H_TOKEN: "on_admin"})
        for path in ("/api/session", "/api/me", "/api/admin/overview"):
            self.assertEqual(self._req("GET", path, headers)[0], 200, path)
        self.assertEqual(self.fetches, [])

    def test_admin_email_does_not_grant_admin_in_proxy_mode(self):
        # IAM 邮箱等于管理员名单里的邮箱，但 union_id 不在名单里
        headers = _headers(**{H_UNION_ID: "on_mallory", H_TOKEN: "on_mallory"})
        self.assertEqual(json.loads(self._req("GET", "/api/session", headers)[1])["role"], "user")
        self.assertEqual(self._req("GET", "/api/admin/overview", headers)[0], 403)
        self._req("GET", "/api/me", headers)
        self.assertEqual(self._req("GET", "/api/admin/overview", headers)[0], 403)

    def test_duplicate_union_id_headers_use_first(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.putrequest("GET", "/api/session")
        conn.putheader(H_SECRET, SECRET)
        conn.putheader(H_UNION_ID, "on_x")
        conn.putheader(H_UNION_ID, "on_admin")
        conn.endheaders()
        data = json.loads(conn.getresponse().read())
        conn.close()
        # 代理会删掉客户端的同名头；这里只锁定面板自身的行为是确定的
        self.assertEqual(data["union_id"], "on_x")
        self.assertEqual(data["role"], "user")

    def test_non_admin_forbidden(self):
        status, _ = self._req("GET", "/api/admin/people", _headers(**{H_UNION_ID: "on_x"}))
        self.assertEqual(status, 403)


class FeishuModeUnchangedTests(unittest.TestCase):
    def test_feishu_session_urls(self):
        from delivery.feishu import FeishuUser
        from delivery.server import COOKIE_NAME, _WebSession

        store = Store()
        handler = make_handler(
            PlatformRegistry.load(),
            store,
            app_id="cli_demo",
            app_secret="s3cret",
            base_url="http://127.0.0.1",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", "/api/session")
            self.assertEqual(json.loads(conn.getresponse().read())["login_url"], "/auth/login")
            store.sessions["sid"] = _WebSession(user=FeishuUser("ou", "on_1", "李四"))
            conn.request("GET", "/api/session", headers={"Cookie": f"{COOKIE_NAME}=sid"})
            data = json.loads(conn.getresponse().read())
            self.assertEqual(
                (data["login_url"], data["logout_url"]), ("/auth/login", "/auth/logout")
            )
            # 飞书模式不认代理头
            conn.request("GET", "/api/session", headers=_headers(**{H_UNION_ID: "on_admin"}))
            self.assertFalse(json.loads(conn.getresponse().read())["authenticated"])
            conn.request("GET", "/auth/login")
            self.assertNotEqual(conn.getresponse().status, 404)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


class ServeModeTests(unittest.TestCase):
    def test_unknown_auth_mode_rejected(self):
        with self.assertRaises(DeliveryError):
            serve(auth="saml", registry=PlatformRegistry.load(), echo=lambda *_: None)

    def test_proxy_mode_without_secret_fails_before_listening(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ProxyAuthError):
            serve(auth="proxy", port=0, registry=PlatformRegistry.load(), echo=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
