"""管理后台系统状态：只看本地配置和快照，缺什么、旧不旧、有没有卡住的单子。数据全部虚构。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delivery import catalog as catalog_mod
from delivery import health
from delivery import tickets as t
from delivery.errors import DeliveryError

from .test_delivery_access_requests import CONFIG, TEMPLATES

NOW = 1_800_000_000.0
ACC = "1000000000000001"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class FakeBackend:
    def __init__(self, **over):
        d = Path(tempfile.mkdtemp())
        self.approval_path = str(d / "approval.json")
        Path(self.approval_path).write_text(
            json.dumps({"approval_code": CONFIG.approval_code, "widgets": dict(CONFIG.widgets)}),
            encoding="utf-8",
        )
        self._catalog = catalog_mod.parse(TEMPLATES)
        self._snapshot = SimpleNamespace(
            captured_at=_iso(NOW - 3600),
            users=(SimpleNamespace(platform="aliyun", account=ACC),),
            incomplete=(),
        )
        self._policies = {
            "captured_at": _iso(NOW - 3600),
            "accounts": [{"platform": "aliyun", "account": ACC, "policies": []}],
        }
        self._assets = None
        self.store = t.TicketStore(str(d / "tickets.json"), clock=lambda: NOW)
        self.__dict__.update(over)

    def catalog(self):
        if isinstance(self._catalog, Exception):
            raise self._catalog
        return self._catalog

    def snapshot(self):
        return self._snapshot

    def policies(self):
        return self._policies

    def assets(self):
        return self._assets

    def flows(self):
        return SimpleNamespace(store=self.store)


EXEC = {
    f"DELIVERY_EXEC_ALIYUN_{ACC}_ACCESS_KEY_ID": "id",
    f"DELIVERY_EXEC_ALIYUN_{ACC}_ACCESS_KEY_SECRET": "secret-value-never-shown",
}


def run(backend, env=None):
    data = health.collect(backend, auth_mode="feishu", environ=env or {}, clock=lambda: NOW)
    return data, {(c["group"], c["title"]): c for c in data["checks"]}


class HealthTests(unittest.TestCase):
    def test_ready_platform_has_no_crit_and_never_leaks_secrets(self):
        data, checks = run(FakeBackend(), EXEC)
        self.assertEqual(data["summary"]["crit"], 0, data)
        self.assertEqual(checks[("执行身份", f"阿里云 {ACC}")]["level"], health.OK)
        self.assertNotIn("secret-value-never-shown", json.dumps(data, ensure_ascii=False))

    def test_missing_pieces_are_crit_with_fix(self):
        backend = FakeBackend(_snapshot=None)
        Path(backend.approval_path).unlink()
        _, checks = run(backend, {})
        for key in (
            ("登录与审批", "飞书审批"),
            ("数据", "权限快照"),
            ("执行身份", f"阿里云 {ACC}"),
        ):
            self.assertEqual(checks[key]["level"], health.CRIT, key)
            self.assertTrue(checks[key]["fix"], key)

    def test_stale_and_partial_data_warn(self):
        backend = FakeBackend(
            _snapshot=SimpleNamespace(
                captured_at=_iso(NOW - 3 * 86400), users=(), incomplete=("aliyun/x：失败",)
            ),
            _policies={
                "captured_at": _iso(NOW - 3600),
                "accounts": [{"platform": "aliyun", "account": ACC, "stale": True, "policies": []}],
            },
        )
        _, checks = run(backend, EXEC)
        self.assertEqual(checks[("数据", "权限快照")]["level"], health.WARN)
        self.assertEqual(checks[("申请内容", "权限策略列表")]["level"], health.WARN)
        self.assertIn("沿用旧列表", checks[("申请内容", "权限策略列表")]["detail"])

    def test_stuck_and_failed_tickets(self):
        backend = FakeBackend()
        ticket = backend.store.create({"kind": "permission", "template": {}}, actor="x")
        _, checks = run(backend, EXEC)
        self.assertEqual(checks[("申请单", "申请单")]["level"], health.OK)
        later = lambda: NOW + 3600  # noqa: E731
        data = health.collect(backend, auth_mode="feishu", environ=EXEC, clock=later)
        stuck = {c["title"]: c for c in data["checks"]}["申请单"]
        self.assertEqual(stuck["level"], health.CRIT)
        backend.store.update(
            ticket["id"],
            actor="s",
            expect=[t.SUBMITTING],
            to=t.SUBMIT_FAILED,
            event="submit_failed",
        )
        _, checks = run(backend, EXEC)
        self.assertEqual(checks[("申请单", "申请单")]["level"], health.WARN)

    def test_config_without_feishu_credentials_is_crit(self):
        data = health.collect(
            FakeBackend(), auth_mode="proxy", approval_ready=False, environ=EXEC, clock=lambda: NOW
        )
        check = {c["title"]: c for c in data["checks"]}["飞书审批"]
        self.assertEqual(check["level"], health.CRIT)
        self.assertIn("凭证", check["detail"])

    def test_file_errors_hide_server_paths(self):
        err = DeliveryError(
            "读不了模板目录 /srv/panel/identity/request-templates.json：Expecting value"
        )
        with mock.patch("sys.stderr"):
            _, checks = run(FakeBackend(_catalog=err), EXEC)
        detail = checks[("申请内容", "申请模板")]["detail"]
        self.assertNotIn("/srv/panel", detail)
        self.assertNotIn("Expecting", detail)

    def test_self_approval_allowed_warns(self):
        backend = FakeBackend()
        conf = json.loads(Path(backend.approval_path).read_text(encoding="utf-8"))
        Path(backend.approval_path).write_text(
            json.dumps({**conf, "allow_self_approval": True}), encoding="utf-8"
        )
        _, checks = run(backend, EXEC)
        self.assertEqual(checks[("登录与审批", "飞书审批")]["level"], health.WARN)

    def test_notify_switches(self):
        _, checks = run(FakeBackend(), EXEC)
        self.assertEqual(checks[("通知", "申请状态通知")]["level"], health.OFF)
        on = {**EXEC, "DELIVERY_NOTIFY": "1", "DELIVERY_BASE_URL": "http://panel.example.com"}
        _, checks = run(FakeBackend(), on)
        self.assertEqual(checks[("通知", "申请人私信")]["level"], health.WARN)
        good = {
            **on,
            "DELIVERY_BASE_URL": "https://panel.example.com",
            "DELIVERY_FEISHU_APP_ID": "cli_x",
            "DELIVERY_FEISHU_APP_SECRET": "s",
        }
        _, checks = run(FakeBackend(), good)
        self.assertEqual(checks[("通知", "申请人私信")]["level"], health.OK)

    def test_broken_file_is_crit_not_500(self):
        _, checks = run(
            FakeBackend(_catalog=DeliveryError("模板 x：groups 必须是用户组名数组")), EXEC
        )
        self.assertEqual(checks[("申请内容", "申请模板")]["level"], health.CRIT)
        _, checks = run(FakeBackend(_catalog=ValueError("boom secret")), EXEC)
        self.assertNotIn("boom secret", checks[("申请内容", "申请模板")]["detail"])


if __name__ == "__main__":
    unittest.main()
