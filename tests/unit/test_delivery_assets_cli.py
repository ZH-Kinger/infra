"""云账号资产（资源中心采集、员工与管理员粒度）、火山 POST 签名、统一 CLI 客户端。数据虚构。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from delivery import assets
from delivery.clouds import aliyun, volcano

ACC = "1000000000000001"


class AliyunAssetTests(unittest.TestCase):
    def transport(self, pages):
        calls = []

        def send(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(q)
            if q["Action"] == "GetCallerIdentity":
                return 200, {"AccountId": ACC}
            return 200, pages[q.get("NextToken", "")]

        return send, calls

    def test_paginates_and_normalizes(self):
        pages = {
            "": {
                "Resources": [
                    {
                        "ResourceType": "ACS::ECS::Instance",
                        "ResourceId": "i-1",
                        "RegionId": "cn-hangzhou",
                        "Tags": [{"Key": "env", "Value": "prod"}],
                    }
                ],
                "NextToken": "t2",
            },
            "t2": {"Resources": [{"ResourceType": "ACS::OSS::Bucket", "ResourceId": "b-1"}]},
        }
        send, calls = self.transport(pages)
        account, items = assets.collect_aliyun(aliyun.Credentials("id", "sk"), transport=send)
        self.assertEqual(account, ACC)
        self.assertEqual([r["id"] for r in items], ["i-1", "b-1"])
        self.assertEqual(items[0]["tags"], {"env": "prod"})

    def test_missing_resources_is_error_not_empty(self):
        send, _ = self.transport({"": {"RequestId": "x"}})
        with self.assertRaises(assets.AssetError):
            assets.collect_aliyun(aliyun.Credentials("id", "sk"), transport=send)

    def test_stuck_token_is_error(self):
        send, _ = self.transport(
            {
                "": {"Resources": [], "NextToken": "same"},
                "same": {"Resources": [], "NextToken": "same"},
            }
        )
        with self.assertRaises(assets.AssetError):
            assets.collect_aliyun(aliyun.Credentials("id", "sk"), transport=send)

    def test_snapshot_records_failure_and_scrubs(self):
        def fail():
            raise aliyun.AliyunError(
                "SignatureDoesNotMatch AccessKeyId=LTAIsecret123 string to sign xx"
            )

        data = assets.build_snapshot([("aliyun", "ALIYUN", fail)])
        self.assertIn("error", data["accounts"][0])
        self.assertNotIn("LTAIsecret123", json.dumps(data))


class VolcanoPostSigningTests(unittest.TestCase):
    def test_post_json_signs_body_and_host(self):
        seen = {}

        def send(url, headers, data=None):
            seen.update(url=url, headers=headers, data=data)
            return 200, {"Result": {"Resources": []}}

        creds = volcano.Credentials("AK", "SK")
        volcano.call(
            *assets.VOLCANO_RC,
            "SearchResources",
            {},
            body={"MaxResults": 100},
            creds=creds,
            transport=send,
        )
        import hashlib

        self.assertEqual(
            seen["headers"]["x-content-sha256"], hashlib.sha256(seen["data"]).hexdigest()
        )
        self.assertEqual(seen["headers"]["Content-Type"], "application/json")
        # 签名必须按实际域名算：换域名签名要变
        a = volcano.sign(
            params={"Action": "X"}, secret="s", region="r", service="sts", xdate="20260101T000000Z"
        )
        b = volcano.sign(
            params={"Action": "X"},
            secret="s",
            region="r",
            service="sts",
            xdate="20260101T000000Z",
            host="sts.volcengineapi.com",
        )
        self.assertNotEqual(a, b)

    def test_sts_call_uses_sts_host(self):
        from delivery.provision import VolcanoExecutor

        urls = []

        def send(url, headers, data=None):
            urls.append((url, headers["host"]))
            if "ListUsers" in url:
                return 200, {"Result": {"UserMetadata": [{"AccountId": "2000000001"}]}}
            return 200, {
                "Result": {
                    "Credentials": {
                        "AccessKeyId": "a",
                        "SecretAccessKey": "s",
                        "SessionToken": "t",
                        "ExpiredTime": "x",
                    }
                }
            }

        ex = VolcanoExecutor("2000000001", volcano.Credentials("AK", "SK"), transport=send)
        ex.assume_role("trn:iam::2000000001:role/r", "li.si@wuji.tech", 1)
        sts_url, sts_host = urls[-1]
        self.assertTrue(sts_url.startswith("https://sts.volcengineapi.com/"))
        self.assertEqual(sts_host, "sts.volcengineapi.com")


class SummaryViewTests(unittest.TestCase):
    DATA = {
        "captured_at": "2026-09-15T10:00:00+08:00",
        "accounts": [
            {
                "platform": "aliyun",
                "account": ACC,
                "resources": [
                    {
                        "type": "ACS::ECS::Instance",
                        "id": "i-1",
                        "name": "secret-db",
                        "region": "cn-hangzhou",
                    },
                    {
                        "type": "ACS::ECS::Instance",
                        "id": "i-2",
                        "name": "web",
                        "region": "cn-hangzhou",
                    },
                ],
            },
            {"platform": "volcano", "account": "2000000001", "error": "AccessDenied 细节"},
        ],
    }

    def label(self, platform, account):
        return f"{platform} {account}"

    def test_employee_sees_counts_only_for_own_accounts(self):
        view = assets.summary_view(self.DATA, scopes={("aliyun", ACC)}, labels=self.label)
        self.assertEqual(len(view["accounts"]), 1)
        acc = view["accounts"][0]
        self.assertEqual(acc["by_type"], [{"type": "ECS Instance", "count": 2}])
        self.assertNotIn("resources", acc)
        self.assertNotIn("secret-db", json.dumps(view))

    def test_admin_sees_details_and_errors(self):
        view = assets.summary_view(self.DATA, scopes=None, labels=self.label)
        self.assertEqual(len(view["accounts"][0]["resources"]), 2)
        self.assertIn("AccessDenied", view["accounts"][1]["error"])

    def test_employee_error_is_generic(self):
        view = assets.summary_view(self.DATA, scopes={("volcano", "2000000001")}, labels=self.label)
        self.assertEqual(view["accounts"][0]["error"], "本次未采集完整")


class PanelClientTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                seen.append((self.path, dict(self.headers), self.rfile.read(length)))
                if self.path.endswith("/withdraw"):
                    return self._reply(
                        200,
                        {"request": {"id": "REQ-20260915-0000000A", "status_label": "已撤回"}},
                    )
                return self._reply(403, {"error": "只能给名册里确认属于你自己的子账号申请权限"})

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.home = tempfile.mkdtemp()
        Path(self.home).chmod(0o700)
        from delivery.session import Session, save_session

        with mock.patch.dict(os.environ, {"W0_HOME": self.home}):
            save_session(
                Session(
                    "on_li",
                    "李四",
                    "session-token",
                    4_102_444_800,
                    f"http://127.0.0.1:{self.server.server_port}",
                )
            )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_cli(self, *argv):
        from delivery import cli

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {"W0_HOME": self.home}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_requests_carry_bearer_and_panel_header(self):
        """会话令牌走 Authorization，且带 X-Panel-Request —— 后者是服务端识别 CLI 的依据。

        原来这条用 `delivery creds` 测。凭证已改成审批评论下发、CLI 不再有这条路，
        但这两个请求头仍然是所有 CLI 写操作的前提，换个还在的命令继续锁住。
        """
        code, out, err = self.run_cli("request", "withdraw", "REQ-20260915-0000000A")
        self.assertEqual(code, 0, err)
        path, headers, _ = self.seen[-1]
        self.assertTrue(path.endswith("/withdraw"), path)
        self.assertEqual(headers.get("Authorization"), "Bearer session-token")
        self.assertEqual(headers.get("X-Panel-Request"), "1")

    def test_creds_subcommand_is_gone(self):
        """凭证不再由 CLI 领取。留着这条是防有人「顺手」把它加回来。"""
        with self.assertRaises(SystemExit) as caught:  # argparse 对未知子命令直接退出
            self.run_cli("creds", "REQ-20260915-0000000A")
        self.assertNotEqual(caught.exception.code, 0)

    def test_server_error_message_shown(self):
        code, out, err = self.run_cli(
            "request", "new", "oss-read", "--reason", "需要读取数据", "--user", "someone"
        )
        self.assertEqual(code, 2)
        self.assertIn("只能给名册里确认属于你自己的子账号申请权限", err + out)

    def test_plain_http_to_remote_refused(self):
        from delivery.cli_requests import ClientError, PanelClient

        with self.assertRaises(ClientError):
            PanelClient("http://panel.example.com", "tok")


if __name__ == "__main__":
    unittest.main()
