"""管理后台系统状态：只看本地配置和快照，缺什么、旧不旧、有没有卡住的单子。数据全部虚构。"""

from __future__ import annotations

import json
import os
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

from .test_delivery_access_requests import APPROVAL_JSON, TEMPLATES

NOW = 1_800_000_000.0
ACC = "1000000000000001"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class FakeBackend:
    def __init__(self, **over):
        d = Path(tempfile.mkdtemp())
        self.approval_path = str(d / "approval.json")
        # 和线上那份 identity/approval.json 一样含 comment_open_id：**少了它凭证申请一提交
        # 就被拒**（评论是凭证的唯一出口）。夹具不配的话，这一页每一条「全绿」都是假的 ——
        # 线上正是这么漏过去的：体检 12 项全绿、测试全绿，凭证申请却一张都发不出来
        Path(self.approval_path).write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
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
#: 面板的对外地址。`TEMPLATES` 里有凭证模板，凭证一律要它 —— 没配就是一条 crit（这是对的：
#: 有凭证模板却没配对外地址，凭证申请一提交就会被拒，那种平台本来就不算 ready）。
#: 它由 `flows.view_base(env)` 读**体检自己那份注入环境**（`collect(environ=)`），
#: 和这一页其余每一项一致 —— 所以要放进传给 `run()` 的那个 dict，patch 进程环境没有用。
BASE_URL = "https://panel.example.com"

#: 进程环境里那个**不该被读到**的值。setUp 把它钉上去，于是这一页每一条
#: 「查看地址 = crit」的断言同时也是一条「注入的环境赢过 os.environ」的锁：
#: 哪天 view_base 又退回去读进程环境，它们会一起从 crit 变 ok。
LEAKED_BASE_URL = "https://leaked-from-process-env.example.com"


def with_base(value=BASE_URL, env=EXEC):
    """注入给 `collect` 的那份环境，外加一个对外地址。空串 = 没配（`view_base` 一视同仁）。"""
    return {**env, health.ENV_BASE_URL: value}


def run(backend, env=None):
    data = health.collect(backend, auth_mode="feishu", environ=env or {}, clock=lambda: NOW)
    return data, {(c["group"], c["title"]): c for c in data["checks"]}


class HealthTests(unittest.TestCase):
    def setUp(self):
        # 进程环境钉成一个「配好了、但是错的」地址：体检读的是注入那份，这里放什么都不该影响
        # 结论。不钉的话，别的模块跑过之后留下（或没留下）DELIVERY_BASE_URL 就能让这一组
        # 用例时绿时红 —— 而那种「时绿时红」恰恰是在掩盖「读错了环境」这个 bug
        patch = mock.patch.dict(os.environ, {health.ENV_BASE_URL: LEAKED_BASE_URL})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_injected_environment_wins_over_the_process_environment(self):
        """体检页每一项都看 `collect(environ=)` 那份，查看地址不能是唯一的例外。

        它以前恒读 `os.environ`：拿体检去查定时任务的 EnvironmentFile 时，那一项报的是
        面板进程自己的环境 —— 结论看着正常，而被查的那份可能根本没配。
        """
        # 注入里没配 → crit，尽管进程环境里有一个像模像样的 https 地址（setUp 钉的）
        self.assertEqual(os.environ[health.ENV_BASE_URL], LEAKED_BASE_URL)
        _, checks = run(self.credential_backend(), EXEC)
        check = checks[("凭证交付", "查看地址")]
        self.assertEqual(check["level"], health.CRIT)
        self.assertNotIn(LEAKED_BASE_URL, json.dumps(check, ensure_ascii=False))
        # 反过来：注入里配好了 → ok，尽管进程环境里是空的
        with mock.patch.dict(os.environ, {health.ENV_BASE_URL: ""}):
            _, checks = run(self.credential_backend(), with_base())
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.OK)

    def test_ready_platform_has_no_crit_and_never_leaks_secrets(self):
        data, checks = run(FakeBackend(), with_base())
        self.assertEqual(data["summary"]["crit"], 0, data)
        self.assertEqual(checks[("执行身份", f"阿里云 {ACC}")]["level"], health.OK)
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.OK)
        self.assertNotIn("secret-value-never-shown", json.dumps(data, ensure_ascii=False))

    def test_a_platform_with_credentials_but_no_public_address_is_not_ready(self):
        """同一份 fixture、只差没配对外地址 —— 必须是 crit，而不是「看起来一切正常」。

        症状（「凭证申请一提交就被拒」）离原因很远，体检页不点名就没人能指回来。
        """
        data, checks = run(FakeBackend(), EXEC)
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.CRIT)
        self.assertEqual(data["summary"]["crit"], 1, data)

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

    def write_approval(self, backend, **changes):
        """改这个 backend 的 identity/approval.json。值给 None = 删掉这个键。"""
        conf = json.loads(Path(backend.approval_path).read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                conf.pop(key, None)
            else:
                conf[key] = value
        Path(backend.approval_path).write_text(json.dumps(conf), encoding="utf-8")
        return backend

    def test_self_approval_allowed_warns(self):
        backend = self.write_approval(FakeBackend(), allow_self_approval=True)
        _, checks = run(backend, EXEC)
        self.assertEqual(checks[("登录与审批", "飞书审批")]["level"], health.WARN)

    # ── 下发凭证的评论身份 ────────────────────────────────────────────────
    #
    # 线上真出过：identity/approval.json 从来没配过 comment_open_id，于是**访问凭证申请
    # 在提交那一刻就被拒**（凭证的唯一出口是审批评论，而飞书评论接口的 user_id 必填、
    # 没有「以应用名义发」的选项）。而当时体检 12 项全绿、测试全绿 —— 症状离原因很远，
    # 体检页不点名就没人指得回来。下面三条把「点名」这件事钉死。

    def test_credential_templates_without_a_comment_identity_are_crit(self):
        backend = self.write_approval(FakeBackend(), comment_open_id=None)
        _, checks = run(backend, with_base())
        check = checks[("登录与审批", "飞书审批")]
        self.assertEqual(check["level"], health.CRIT)
        self.assertIn("凭证", check["detail"])
        # 修法要能照着做：改哪个文件的哪个键、为什么非得有一个自然人身份
        self.assertIn("comment_open_id", check["fix"])
        self.assertIn("user_id", check["fix"])
        key = ("登录与审批", "飞书审批")
        # 空串和没有这个键一样：JSON 里留个 "" 是最常见的「配了但没填」
        blank = self.write_approval(FakeBackend(), comment_open_id="")
        self.assertEqual(run(blank, with_base())[1][key]["level"], health.CRIT)
        # 同时还开了自审批时报更严重的那条，不能被 warn 盖掉
        both = self.write_approval(FakeBackend(), comment_open_id=None, allow_self_approval=True)
        self.assertEqual(run(both, with_base())[1][key]["level"], health.CRIT)
        # 配上就恢复原样（这项不会因为别的原因常驻 crit）
        self.assertEqual(run(FakeBackend(), with_base())[1][key]["level"], health.OK)

    def test_without_credential_templates_the_comment_identity_is_not_required(self):
        """一个凭证模板都没有的部署根本不发凭证，缺评论身份不该拦着它全绿。"""
        backend = self.write_approval(self.no_credential_backend(), comment_open_id=None)
        data, checks = run(backend, with_base())
        self.assertEqual(checks[("登录与审批", "飞书审批")]["level"], health.OK)
        self.assertEqual(data["summary"]["crit"], 0, data)

    def test_unreadable_templates_make_the_comment_check_strict(self):
        """模板读不出来时从严：宁可多报一条，也别因为读不到模板就默认「这里不发凭证」。"""
        backend = self.write_approval(
            FakeBackend(_catalog=DeliveryError("模板 x：groups 必须是用户组名数组")),
            comment_open_id=None,
        )
        with mock.patch("sys.stderr"):
            _, checks = run(backend, with_base())
        check = checks[("登录与审批", "飞书审批")]
        self.assertEqual(check["level"], health.CRIT)
        self.assertIn("凭证", check["detail"])

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

    def credential_backend(self):
        """加一个只能走长期凭证的模板（没配角色）。"""
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"].append(
            {
                "id": "volc-data",
                "kind": "credential",
                "platform": "volcano",
                "account": "2000000001",
                "title": "火山数据访问凭证",
                "max_hours": 720,
                "caps": ["download"],
                "buckets": [{"name": "wuji-tos-data", "region": "cn-beijing"}],
            }
        )
        return FakeBackend(_catalog=catalog_mod.parse(data))

    def sts_only_backend(self):
        """只有短期凭证模板：配了角色、`max_hours` 在 12 小时以内，一个子账号都不会建。

        这种部署的「发放身份」那张表是空的 —— 而它同样要查看地址和加密才发得出凭证。
        """
        data = json.loads(json.dumps(TEMPLATES))
        for tpl in data["templates"]:
            if tpl["kind"] == "credential":
                tpl["max_hours"] = catalog_mod.STS_MAX_HOURS
        return FakeBackend(_catalog=catalog_mod.parse(data))

    def no_credential_backend(self):
        """一个凭证模板都没有：权限包 + 开账号。这两项才该整组消失。"""
        data = json.loads(json.dumps(TEMPLATES))
        data["templates"] = [t for t in data["templates"] if t["kind"] != "credential"]
        return FakeBackend(_catalog=catalog_mod.parse(data))

    def test_credential_delivery_group_reports_the_view_address(self):
        """缺取件地址的症状是「凭证申请一提交就被拒」—— 很难指回原因，所以体检页要点名。"""
        _, checks = run(self.credential_backend(), EXEC)
        check = checks[("凭证交付", "查看地址")]
        self.assertEqual(check["level"], health.CRIT)
        self.assertIn("DELIVERY_BASE_URL", check["fix"])
        # 加密自检是另一项：装了 cryptography 就该是 OK
        self.assertEqual(checks[("凭证交付", "加密")]["level"], health.OK)
        _, checks = run(self.credential_backend(), with_base())
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.OK)
        # http + 外网域名不算配好：取件页和密文会在明文 HTTP 上裸奔
        _, checks = run(self.credential_backend(), with_base("http://panel.example.com"))
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.CRIT)
        # 本机地址同样不算配好：serve 漏配时会回填 http://localhost:8765，它过得了
        # safe_base_url，于是体检页全绿、评论里贴出一个使用方点开是他自己端口的地址
        for label, value in (
            ("serve 回填的本机地址", "http://localhost:8765"),
            ("回环 IP", "http://127.0.0.1:8765"),
            ("本机地址套 https", "https://localhost:8765"),
        ):
            with self.subTest(label):
                _, checks = run(self.credential_backend(), with_base(value))
                self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.CRIT, label)
                self.assertTrue(checks[("凭证交付", "查看地址")]["fix"], label)

    def test_credential_delivery_group_covers_sts_only_platforms_too(self):
        """判据是「有没有凭证模板」，不是「有没有云账号需要发放身份」。

        挂在发放身份那张表下面的话，一个配了角色、`max_hours=12` 的纯 STS 部署一项都不检 ——
        而 `_offer_credential` 对 STS 凭证同样要 `view_base()` + 加密自检，照样发不出去。
        """
        backend = self.sts_only_backend()
        _, checks = run(backend, EXEC)
        self.assertNotIn(("发放身份", f"阿里云 {ACC}"), checks)  # 这张表确实是空的
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.CRIT)
        self.assertEqual(checks[("凭证交付", "加密")]["level"], health.OK)
        _, checks = run(self.sts_only_backend(), with_base())
        self.assertEqual(checks[("凭证交付", "查看地址")]["level"], health.OK)

    def test_credential_delivery_group_is_absent_without_credential_templates(self):
        """这两项和云账号无关、装完就不再变。一个凭证模板都没有时不该挂上去刷存在感。"""
        _, checks = run(self.no_credential_backend(), EXEC)
        self.assertNotIn(("凭证交付", "查看地址"), checks)
        self.assertNotIn(("凭证交付", "加密"), checks)

    def test_encryption_failure_is_crit_and_does_not_break_the_page(self):
        """加密库不在的话凭证一律发不出去 —— 但体检页本身不能跟着挂掉。"""
        with mock.patch("delivery.sealed.selfcheck", side_effect=RuntimeError("缺少 cryptography")):
            data, checks = run(self.credential_backend(), EXEC)
        check = checks[("凭证交付", "加密")]
        self.assertEqual(check["level"], health.CRIT)
        self.assertIn("cryptography", check["fix"])
        self.assertTrue(data["checks"])  # 整页还在

    def test_broken_file_is_crit_not_500(self):
        _, checks = run(
            FakeBackend(_catalog=DeliveryError("模板 x：groups 必须是用户组名数组")), EXEC
        )
        self.assertEqual(checks[("申请内容", "申请模板")]["level"], health.CRIT)
        _, checks = run(FakeBackend(_catalog=ValueError("boom secret")), EXEC)
        self.assertNotIn("boom secret", checks[("申请内容", "申请模板")]["detail"])


if __name__ == "__main__":
    unittest.main()
