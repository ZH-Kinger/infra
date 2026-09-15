"""本地开发服务器：路由、会话、以及几条不能退让的安全性质。"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _Pending, _WebSession, make_handler


class _Live:
    """起一个真服务器，用 http.client 打它——比 mock handler 更接近真实行为。"""

    def __init__(self, **kw):
        registry = PlatformRegistry.load()
        self.store = Store()
        opts = {"app_id": "cli_demo", "app_secret": "s3cret", "base_url": "http://127.0.0.1"}
        opts.update(kw)
        handler = make_handler(registry, self.store, **opts)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, *, cookie="", follow=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Cookie": f"{COOKIE_NAME}={cookie}"} if cookie else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace")
        out = (resp.status, dict(resp.getheaders()), body)
        conn.close()
        return out

    def post(self, path, payload):
        import json as _json

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            path,
            body=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read().decode(errors="replace")
        out = (resp.status, body)
        conn.close()
        return out


class RoutingTests(unittest.TestCase):
    def test_healthz(self):
        with _Live() as live:
            status, _, body = live.get("/healthz")
            self.assertEqual(status, 200)
            self.assertIn("true", body)

    def test_root_serves_frontend_with_csp(self):
        with _Live() as live:
            status, headers, body = live.get("/")
            self.assertEqual(status, 200)
            self.assertIn("/app.js", body)
            self.assertIn("script-src 'self'", headers.get("Content-Security-Policy", ""))
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_static_assets_are_whitelisted_not_mapped(self):
        """前端文件走白名单：任何其它路径都 404，谈不上目录穿越。"""
        with _Live() as live:
            self.assertEqual(live.get("/app.js")[0], 200)
            self.assertEqual(live.get("/app.css")[0], 200)
            for probe in ("/web/app.js", "/../server.py", "/%2e%2e/server.py", "/server.py"):
                self.assertEqual(live.get(probe)[0], 404, probe)

    def test_unknown_path_404(self):
        with _Live() as live:
            self.assertEqual(live.get("/nope")[0], 404)

    def test_pages_are_not_cacheable(self):
        # 看板显示账号与权限，进了缓存就可能被下一个人看到
        with _Live() as live:
            _, headers, _ = live.get("/")
            self.assertIn("no-store", headers.get("Cache-Control", ""))


class LoginRedirectTests(unittest.TestCase):
    def test_login_redirects_to_feishu_with_pkce(self):
        with _Live(base_url="http://127.0.0.1:1") as live:
            status, headers, _ = live.get("/auth/login")
            self.assertEqual(status, 302)
            loc = headers["Location"]
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
            self.assertEqual(q["client_id"], "cli_demo")
            self.assertEqual(q["code_challenge_method"], "S256")
            self.assertTrue(q["state"])

    def test_missing_app_id_fails_clearly(self):
        with _Live(app_id="") as live:
            status, _, body = live.get("/auth/login")
            self.assertEqual(status, 500)
            self.assertIn("DELIVERY_FEISHU_APP_ID", body)

    def test_forged_state_is_rejected(self):
        with _Live() as live:
            status, _, body = live.get("/auth/callback?code=x&state=forged")
            self.assertEqual(status, 400)
            self.assertIn("state", body)

    def test_state_is_single_use(self):
        # 授权码流程里 state 必须一次性，否则回调可被重放
        store = Store()
        store.put_pending("s1", _Pending(verifier="v", redirect_uri="u"))
        self.assertIsNotNone(store.take_pending("s1"))
        self.assertIsNone(store.take_pending("s1"))

    def test_provider_error_is_surfaced(self):
        with _Live() as live:
            status, _, body = live.get("/auth/callback?error=access_denied")
            self.assertEqual(status, 400)
            self.assertIn("access_denied", body)


class SessionTests(unittest.TestCase):
    def _logged_in(self, live):
        sid = "test-session-id"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(open_id="ou_1", union_id="on_1", name="李四")
        )
        return sid

    def test_session_reports_identity_and_role(self):
        with _Live() as live:
            sid = self._logged_in(live)
            status, _, body = live.get("/api/session", cookie=sid)
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(data["authenticated"])
            self.assertEqual(data["union_id"], "on_1")
            self.assertEqual(data["role"], "user")

    def test_unknown_cookie_is_anonymous(self):
        with _Live() as live:
            _, _, body = live.get("/api/session", cookie="not-a-real-session")
            self.assertFalse(json.loads(body)["authenticated"])

    def test_logout_clears_cookie_and_session(self):
        with _Live() as live:
            sid = self._logged_in(live)
            status, headers, _ = live.get("/auth/logout", cookie=sid)
            self.assertEqual(status, 302)
            self.assertIn("Max-Age=0", headers.get("Set-Cookie", ""))
            self.assertNotIn(sid, live.store.sessions)

    def test_expired_session_is_not_accepted(self):
        with _Live() as live:
            sid = "old"
            stale = _WebSession(user=FeishuUser("ou", "on", "旧"), created=0.0)
            live.store.sessions[sid] = stale
            _, _, body = live.get("/api/session", cookie=sid)
            self.assertFalse(json.loads(body)["authenticated"])
            self.assertEqual(live.get("/api/me", cookie=sid)[0], 401)


class ExchangeEndpointTests(unittest.TestCase):
    """给 CLI 用的端点：app_secret 只在这一侧。"""

    def test_rejects_non_json(self):
        with _Live() as live:
            conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=5)
            conn.request("POST", "/auth/exchange", body=b"{bad")
            self.assertEqual(conn.getresponse().status, 400)
            conn.close()

    def test_requires_code_and_redirect_uri(self):
        with _Live() as live:
            for payload in ({}, {"code": "c"}, {"redirect_uri": "u"}):
                status, body = live.post("/auth/exchange", payload)
                self.assertEqual(status, 400, payload)
                self.assertIn("缺少", body)

    def test_wrong_method_on_exchange_path(self):
        with _Live() as live:
            self.assertEqual(live.get("/auth/exchange")[0], 404)


class SecurityPropertyTests(unittest.TestCase):
    def test_handler_does_not_log_callback_urls(self):
        import inspect

        from delivery import server as mod

        self.assertIn("def log_message", inspect.getsource(mod.make_handler))

    def test_serve_binds_loopback_by_default(self):
        import inspect

        from delivery import server as mod

        sig = inspect.signature(mod.serve)
        self.assertEqual(sig.parameters["host"].default, "127.0.0.1")

    def test_session_cookie_is_httponly_and_samesite(self):
        import inspect

        from delivery import server as mod

        source = inspect.getsource(mod.make_handler)
        self.assertIn("HttpOnly", source)
        self.assertIn("SameSite", source)


if __name__ == "__main__":
    unittest.main()


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
            "union_id": "on_1",
            "name": "李四",
            "email": "li.si@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": "100", "name": "lisi"}],
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
                {"name": "lisi", "policies": ["AliyunOSSReadOnlyAccess"]},
                {"name": "xints", "policies": []},
            ],
        }
    ],
}


class ApiTests(unittest.TestCase):
    """权限接口：未登录 401、非管理员 403、用户只看得到自己、按 union_id 认人。"""

    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.dir = d
        (d / "people.json").write_text(json.dumps(PEOPLE, ensure_ascii=False), encoding="utf-8")
        (d / "inventory.json").write_text(json.dumps(SNAPSHOT), encoding="utf-8")
        (d / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), encoding="utf-8")
        self.backend = Backend(
            inventory_path=str(d / "inventory.json"),
            people_path=str(d / "people.json"),
            bindings_path=str(d / "bindings.json"),
            admins_path=str(d / "admins.json"),
            platforms={"aliyun": "阿里云"},
        )

    def _live(self):
        return _Live(backend=self.backend)

    def _login(self, live, uid, email="", sid=None):
        sid = sid or f"sid-{uid}"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(
                open_id="ou_x", union_id=uid, name="某人", email=email, enterprise_email=email
            )
        )
        return sid

    def _get(self, live, path, sid=""):
        status, _, body = live.get(path, cookie=sid)
        return status, json.loads(body)

    def test_large_get_json_is_gzipped_only_when_accepted(self):
        import gzip

        from delivery import server as server_mod

        with self._live() as live, mock.patch.object(server_mod, "_GZIP_MIN", 10):
            sid = self._login(live, "on_admin")
            conn = http.client.HTTPConnection("127.0.0.1", live.port, timeout=5)
            conn.request(
                "GET",
                "/api/admin/overview",
                headers={"Cookie": f"{COOKIE_NAME}={sid}", "Accept-Encoding": "gzip"},
            )
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            self.assertEqual(resp.getheader("Content-Encoding"), "gzip")
            self.assertEqual(resp.getheader("Vary"), "Accept-Encoding")
            self.assertIn("totals", json.loads(gzip.decompress(raw)))
            # 不声明 gzip 的客户端（CLI 用的 urllib）照常拿明文
            status, headers, body = live.get("/api/admin/overview", cookie=sid)
            self.assertNotIn("Content-Encoding", headers)
            self.assertIn("totals", json.loads(body))

    def test_anonymous_gets_401_everywhere(self):
        with self._live() as live:
            for path in (
                "/api/me",
                "/api/admin/overview",
                "/api/admin/people",
                "/api/admin/people/on_1",
                "/api/admin/health",
            ):
                self.assertEqual(live.get(path)[0], 401, path)

    def test_non_admin_gets_403_on_admin_routes(self):
        with self._live() as live:
            sid = self._login(live, "on_1")
            for path in (
                "/api/admin/overview",
                "/api/admin/people",
                "/api/admin/people/on_admin",
                "/api/admin/health",
            ):
                status, body = self._get(live, path, sid)
                self.assertEqual(status, 403, path)
                self.assertNotIn("boss", json.dumps(body))

    def test_me_returns_only_my_accounts(self):
        with self._live() as live:
            sid = self._login(live, "on_1")
            status, body = self._get(live, "/api/me", sid)
            self.assertEqual(status, 200)
            self.assertEqual([a["name"] for a in body["accounts"]], ["lisi"])
            self.assertNotIn("boss", json.dumps(body))

    def test_me_ignores_email_for_already_bound_people(self):
        """拿别人的企业邮箱登录（邮箱复用场景）不能认领已绑定的人。"""
        with self._live() as live:
            sid = self._login(live, "on_newcomer", email="li.si@wuji.tech")
            _, body = self._get(live, "/api/me", sid)
            self.assertEqual(body["binding"], "conflict")
            self.assertEqual(body["accounts"], [])

    def test_first_login_binds_by_enterprise_email_once(self):
        with self._live() as live:
            sid = self._login(live, "on_new", email="xin.tongshi@wuji.tech")
            _, first = self._get(live, "/api/me", sid)
            _, second = self._get(live, "/api/me", sid)
        self.assertEqual(first["binding"], "bound_now")
        self.assertEqual(second["binding"], "union_id")
        self.assertEqual([a["name"] for a in second["accounts"]], ["xints"])
        saved = json.loads((self.dir / "bindings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["schema"], "wuji-bindings@2")
        self.assertEqual(saved["bindings"]["on_new"]["email"], "xin.tongshi@wuji.tech")
        self.assertEqual(saved["bindings"]["on_new"]["accounts"], ["aliyun/100/xints"])

    def test_admin_email_does_not_grant_admin_via_contact_email(self):
        """管理员名单里的邮箱只和企业邮箱比；个人联系邮箱不能冒充。"""
        (self.dir / "admins.json").write_text(json.dumps({"emails": ["admin@wuji.tech"]}))
        with self._live() as live:
            sid = "sid-contact"
            live.store.sessions[sid] = _WebSession(
                user=FeishuUser(
                    open_id="ou_y",
                    union_id="on_y",
                    name="x",
                    email="admin@wuji.tech",
                    contact_email="admin@wuji.tech",
                )
            )
            self.assertEqual(live.get("/api/admin/overview", cookie=sid)[0], 403)

    def test_admin_routes(self):
        with self._live() as live:
            sid = self._login(live, "on_admin")
            status, overview = self._get(live, "/api/admin/overview", sid)
            self.assertEqual(status, 200)
            self.assertEqual(overview["totals"]["people"], 3)
            _, people = self._get(live, "/api/admin/people?filter=unbound", sid)
            self.assertEqual([p["key"] for p in people["people"]], ["email:xin.tongshi@wuji.tech"])
            status, detail = self._get(
                live, "/api/admin/people/" + urllib.parse.quote("email:xin.tongshi@wuji.tech"), sid
            )
            self.assertEqual(status, 200)
            self.assertEqual([a["name"] for a in detail["accounts"]], ["xints"])
            self.assertEqual(self._get(live, "/api/admin/people/on_nobody", sid)[0], 404)
            self.assertEqual(self._get(live, "/api/admin/people?filter=bogus", sid)[0], 400)
            status, state = self._get(live, "/api/admin/health", sid)
            self.assertEqual(status, 200)
            self.assertIn("summary", state)

    def test_broken_snapshot_is_an_error_not_empty_data(self):
        (self.dir / "inventory.json").write_text("{", encoding="utf-8")
        with self._live() as live:
            sid = self._login(live, "on_1")
            status, body = self._get(live, "/api/me", sid)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "面板数据暂不可用，请联系管理员查看服务端日志")
        # 不回显服务器路径
        self.assertNotIn("inventory.json", body["error"])
        self.assertNotIn(str(self.dir), json.dumps(body, ensure_ascii=False))

    def test_missing_roster_is_500_with_explicit_message(self):
        (self.dir / "people.json").unlink()
        with self._live() as live:
            sid = self._login(live, "on_1")
            status, body = self._get(live, "/api/me", sid)
            self.assertEqual(status, 500)
            self.assertTrue(body["error"].startswith("人员名册还没生成"))
            self.assertNotIn(str(self.dir), body["error"])
            admin = self._login(live, "on_admin")
            status, body = self._get(live, "/api/admin/overview", admin)
            self.assertEqual(status, 500)
            self.assertTrue(body["error"].startswith("人员名册还没生成"))

    def test_broken_roster_does_not_echo_path(self):
        (self.dir / "people.json").write_text("{", encoding="utf-8")
        with self._live() as live:
            sid = self._login(live, "on_1")
            status, body = self._get(live, "/api/me", sid)
        self.assertEqual(status, 500)
        self.assertEqual(body["error"], "面板数据暂不可用，请联系管理员查看服务端日志")

    def test_old_bindings_file_is_500_not_silently_unbound(self):
        (self.dir / "bindings.json").write_text(
            json.dumps({"schema": "wuji-bindings@1", "bindings": {}}), encoding="utf-8"
        )
        with self._live() as live:
            sid = self._login(live, "on_1")
            status, body = self._get(live, "/api/me", sid)
        self.assertEqual(status, 500)
        self.assertNotIn("bindings.json", body["error"])

    def test_incomplete_snapshot_detail_hidden_from_non_admin(self):
        snap = dict(SNAPSHOT)
        snap["accounts"] = SNAPSHOT["accounts"] + [
            {
                "platform": "volcano",
                "account": "200",
                "error": "`ListUsers` 被拒（AccessDenied）：内部细节 /srv/secret/path",
            }
        ]
        (self.dir / "inventory.json").write_text(
            json.dumps(snap, ensure_ascii=False), encoding="utf-8"
        )
        with self._live() as live:
            _, mine = self._get(live, "/api/me", self._login(live, "on_1"))
            _, admin = self._get(live, "/api/me", self._login(live, "on_admin"))
        self.assertEqual(mine["snapshot_incomplete"], ["volcano/200：本次未采集完整"])
        self.assertNotIn("AccessDenied", json.dumps(mine, ensure_ascii=False))
        self.assertEqual(len(admin["snapshot_incomplete"]), 1)
        self.assertIn("AccessDenied", admin["snapshot_incomplete"][0])

    def test_snapshot_reloads_when_file_changes(self):
        import os

        with self._live() as live:
            sid = self._login(live, "on_1")
            _, before = self._get(live, "/api/me", sid)
            changed = dict(SNAPSHOT, captured_at="2026-09-16T10:00:00+08:00")
            path = self.dir / "inventory.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            st = path.stat()
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            _, after = self._get(live, "/api/me", sid)
        self.assertNotEqual(before["captured_at"], after["captured_at"])


class ApiErrorEdgeTests(unittest.TestCase):
    def test_delivery_error_with_empty_message_still_returns_500_json(self):
        """已知 bug（server.py:472 `str(exc).splitlines()[0]`）：DeliveryError 消息为空时
        在 except 分支里再抛 IndexError，连接被直接断开，浏览器拿不到 500 JSON。
        （与 inventory_collect.build_snapshot 已修的是同一类问题。）"""
        from delivery.errors import DeliveryError

        class Broken(Backend):
            def people(self):
                raise DeliveryError("")

        backend = Broken(platforms={"aliyun": "阿里云"})
        with _Live(backend=backend) as live:
            live.store.sessions["sid"] = _WebSession(
                user=FeishuUser(open_id="ou_x", union_id="on_1", name="某人")
            )
            try:
                status, _, body = live.get("/api/me", cookie="sid")
            except (http.client.HTTPException, ConnectionError):
                self.fail("连接被断开，没有返回 500 JSON")
        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(body))
