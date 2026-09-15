"""CLI 用公司 IAM 设备码登录（RFC 8628）。IAM 接口全部替换，令牌是自造的未签名 JWT。"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from delivery import cli, cli_requests, iam_device
from delivery.cli_requests import ClientError, PanelClient
from delivery.session import load_session

ISSUER = "https://iam.example.com/application/o/delivery-cli"


def jwt(**claims) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeIam:
    def __init__(self, polls=("authorization_pending",), *, device=True, issuer=ISSUER):
        self.polls = list(polls)
        self.device = device
        self.issuer = issuer
        self.calls = []
        self.exp = 2_000_000_000

    def __call__(self, method, url, form):
        self.calls.append((method, url, dict(form or {})))
        if url.endswith("/.well-known/openid-configuration"):
            doc = {
                "issuer": self.issuer,
                "token_endpoint": "https://iam.example.com/application/o/token/",
            }
            if self.device:
                doc["device_authorization_endpoint"] = (
                    "https://iam.example.com/application/o/device/"
                )
            return 200, doc
        if url.endswith("/device/"):
            return 200, {
                "device_code": "dev-123",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://iam.example.com/device",
                "verification_uri_complete": "https://iam.example.com/device?code=ABCD-EFGH",
                "interval": 2,
                "expires_in": 60,
            }
        if url.endswith("/token/"):
            if form["grant_type"] == "refresh_token":
                if form["refresh_token"] != "rt-1":
                    return 400, {"error": "invalid_grant"}
                return 200, {
                    "access_token": jwt(sub="u1", feishu_union_id="on_li", exp=self.exp + 3600),
                    "id_token": jwt(sub="u1", exp=self.exp + 3600),
                }
            if self.polls:
                return 400, {"error": self.polls.pop(0)}
            return 200, {
                "access_token": jwt(
                    sub="u1", name="李四", feishu_union_id="on_li", exp=self.exp, kind="access"
                ),
                "id_token": jwt(sub="u1", name="李四", feishu_union_id="on_li", exp=self.exp),
                "refresh_token": "rt-1",
            }
        return 404, {}


class Clock:
    def __init__(self):
        self.now = 1_000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class DeviceFlowTests(unittest.TestCase):
    def test_discover_requires_matching_issuer_https_and_device_endpoint(self):
        ends = iam_device.discover(ISSUER, transport=FakeIam())
        self.assertTrue(ends.device_authorization.endswith("/device/"))
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.discover(ISSUER, transport=FakeIam(issuer="https://evil.example.com"))
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.discover(ISSUER, transport=FakeIam(device=False))
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.discover("http://iam.example.com", transport=FakeIam())

    def test_poll_handles_pending_and_slow_down(self):
        fake = FakeIam(polls=["authorization_pending", "slow_down", "authorization_pending"])
        ends = iam_device.discover(ISSUER, transport=fake)
        code = iam_device.start(ends, "cli-app", transport=fake)
        clock = Clock()
        tokens = iam_device.poll(
            ends, "cli-app", code, transport=fake, clock=clock, sleep=clock.sleep
        )
        self.assertEqual(clock.sleeps, [2, 2, 7, 7])
        self.assertEqual(tokens.claims["feishu_union_id"], "on_li")
        self.assertEqual(tokens.refresh_token, "rt-1")
        self.assertEqual(tokens.expires_ts, fake.exp)
        grant = [c for c in fake.calls if c[2].get("grant_type")]
        self.assertTrue(all(c[2]["grant_type"] == iam_device.GRANT_DEVICE for c in grant))

    def test_poll_denied_and_expired(self):
        for error in ("access_denied", "expired_token", "invalid_grant", "invalid_client"):
            fake = FakeIam(polls=[error])
            ends = iam_device.discover(ISSUER, transport=fake)
            code = iam_device.start(ends, "cli-app", transport=fake)
            clock = Clock()
            with self.assertRaises(iam_device.IamLoginError, msg=error):
                iam_device.poll(
                    ends, "cli-app", code, transport=fake, clock=clock, sleep=clock.sleep
                )
        # 一直 pending 到过期
        fake = FakeIam(polls=["authorization_pending"] * 100)
        ends = iam_device.discover(ISSUER, transport=fake)
        code = iam_device.start(ends, "cli-app", transport=fake)
        clock = Clock()
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.poll(ends, "cli-app", code, transport=fake, clock=clock, sleep=clock.sleep)

    def test_prefers_jwt_access_token_falls_back_to_id_token(self):
        body = {"access_token": jwt(kind="access", exp=5), "id_token": jwt(kind="id", exp=5)}
        self.assertEqual(iam_device._tokens(body, lambda: 0).claims["kind"], "access")
        opaque = {"access_token": "opaque-token", "id_token": jwt(kind="id", exp=5)}
        self.assertEqual(iam_device._tokens(opaque, lambda: 0).claims["kind"], "id")

    def test_default_scope_requests_custom_claims_and_refresh(self):
        fake = FakeIam()
        ends = iam_device.discover(ISSUER, transport=fake)
        iam_device.start(ends, "cli-app", transport=fake)
        scope = next(c[2]["scope"] for c in fake.calls if c[1].endswith("/device/"))
        self.assertIn("wuji", scope.split())
        self.assertIn("offline_access", scope.split())

    def test_bad_tokens_rejected(self):
        with self.assertRaises(iam_device.IamLoginError):
            iam_device._tokens({"access_token": "x"}, lambda: 0)
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.jwt_claims("not-a-jwt")

    def test_refresh_keeps_old_refresh_token_when_not_rotated(self):
        fake = FakeIam()
        tokens = iam_device.refresh(
            "https://iam.example.com/application/o/token/", "cli-app", "rt-1", transport=fake
        )
        self.assertEqual(tokens.refresh_token, "rt-1")
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.refresh(
                "https://iam.example.com/application/o/token/", "cli-app", "bad", transport=fake
            )
        with self.assertRaises(iam_device.IamLoginError):
            iam_device.refresh(
                "https://iam.example.com/application/o/token/", "cli-app", "", transport=fake
            )


class CliLoginTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(os.environ, {"W0_HOME": str(self.home / ".w0")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, fake):
        args = argparse.Namespace(
            iam=True,
            issuer=ISSUER,
            client_id="cli-app",
            server="https://panel.example.com",
            no_browser=True,
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli._cmd_login_iam(args, args.server, transport=fake, sleep=lambda s: None)
        return code, out.getvalue()

    def test_login_saves_private_session_without_printing_tokens(self):
        code, output = self.login(FakeIam(polls=[]))
        self.assertEqual(code, 0)
        self.assertIn("ABCD-EFGH", output)
        self.assertNotIn("rt-1", output)
        session = load_session()
        self.assertNotIn(session.token, output)
        self.assertNotIn(session.token.split(".")[1], output)
        session = load_session()
        self.assertEqual((session.kind, session.union_id, session.name), ("iam", "on_li", "李四"))
        self.assertEqual(session.refresh_token, "rt-1")
        path = self.home / ".w0" / "session.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_client_uses_id_token_and_refreshes_near_expiry(self):
        fake = FakeIam(polls=[])
        fake.exp = 100  # 已经快过期
        self.login(fake)
        refreshed = []

        def refresher(endpoint, client_id, refresh_token):
            refreshed.append((endpoint, client_id, refresh_token))
            return iam_device.refresh(endpoint, client_id, refresh_token, transport=FakeIam())

        client = PanelClient.from_session(refresher=refresher)
        self.assertEqual(refreshed[0][1:], ("cli-app", "rt-1"))
        self.assertEqual(iam_device.jwt_claims(client._token)["exp"], FakeIam().exp + 3600)
        self.assertEqual(load_session().token, client._token)  # 续期结果写回本机

    def test_401_triggers_one_refresh_and_retry(self):
        self.login(FakeIam(polls=[]))
        calls = []

        def refresher(endpoint, client_id, refresh_token):
            calls.append(refresh_token)
            return iam_device.refresh(endpoint, client_id, refresh_token, transport=FakeIam())

        client = PanelClient.from_session(refresher=refresher)
        self.assertEqual(calls, [])  # 还没到期，不续
        sent = []

        def once(method, path, body=None):
            sent.append(client._token)
            if len(sent) == 1:
                raise cli_requests._Unauthorized()
            return {"ok": True}

        client._request_once = once
        self.assertEqual(client.request("GET", "/api/requests"), {"ok": True})
        self.assertEqual(calls, ["rt-1"])
        self.assertNotEqual(sent[0], sent[1])

    def test_other_process_already_refreshed(self):
        import dataclasses

        from delivery.session import save_session

        fake = FakeIam(polls=[])
        fake.exp = 100
        self.login(fake)
        stale = load_session()
        # 另一个进程已经续过：文件里是新令牌
        newer = dataclasses.replace(
            stale, token=jwt(sub="u1", exp=4_000_000_000), refresh_token="rt-2", expires_ts=4e9
        )
        save_session(newer)

        def must_not_refresh(*a):
            raise AssertionError("不该再续期")

        with mock.patch("delivery.cli_requests.load_session", return_value=stale):
            client = PanelClient.from_session(refresher=must_not_refresh)
        self.assertEqual(client._token, newer.token)

    def test_refresh_failure_keeps_unexpired_token(self):
        import dataclasses
        import time

        from delivery.session import save_session

        self.login(FakeIam(polls=[]))
        soon = dataclasses.replace(load_session(), expires_ts=time.time() + 60)
        save_session(soon)

        def down(*a):
            raise iam_device.IamLoginError("续期失败：HTTP 503，稍后再试")

        client = PanelClient.from_session(refresher=down)
        self.assertEqual(client._token, soon.token)

    def test_session_write_is_atomic_and_private(self):
        self.login(FakeIam(polls=[]))
        base = self.home / ".w0"
        self.assertEqual([p.name for p in base.iterdir() if p.name.endswith(".tmp")], [])
        self.assertEqual(stat.S_IMODE((base / "session.json").stat().st_mode), 0o600)

    def test_redirect_is_not_followed_with_token(self):
        import http.server
        import threading

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/steal")
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        client = PanelClient(f"http://127.0.0.1:{server.server_port}", "secret-token")
        with self.assertRaises(ClientError) as ctx:
            client.request("GET", "/api/requests")
        self.assertIn("跳转", str(ctx.exception))

    def test_refresh_server_error_is_not_reported_as_expired(self):
        def five_hundred(method, url, form):
            return 503, {}

        with self.assertRaises(iam_device.IamLoginError) as ctx:
            iam_device.refresh(
                "https://iam.example.com/t/", "cli-app", "rt", transport=five_hundred
            )
        self.assertNotIn("过期", str(ctx.exception))

    def test_login_rejects_plain_http_server(self):
        args = argparse.Namespace(
            iam=True,
            issuer=ISSUER,
            client_id="cli-app",
            server="http://panel.example.com",
            no_browser=True,
        )
        with self.assertRaises(Exception) as ctx:
            cli._cmd_login_iam(args, args.server, transport=FakeIam(polls=[]), sleep=lambda s: None)
        self.assertIn("https", str(ctx.exception))

    def test_refresh_failure_asks_to_login_again(self):
        fake = FakeIam(polls=[])
        fake.exp = 100
        self.login(fake)

        def boom(*a):
            raise iam_device.IamLoginError("登录已过期，请重新 delivery login --iam")

        with self.assertRaises(ClientError):
            PanelClient.from_session(refresher=boom)

    def test_missing_config_is_clear_error(self):
        args = argparse.Namespace(
            iam=True, issuer="", client_id="", server="https://p", no_browser=True
        )
        with (
            mock.patch.dict(
                os.environ, {"DELIVERY_IAM_ISSUER": "", "DELIVERY_IAM_CLI_CLIENT_ID": ""}
            ),
            self.assertRaises(Exception) as ctx,
        ):
            cli._cmd_login_iam(args, "https://p")
        self.assertIn("DELIVERY_IAM_ISSUER", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
