"""定时刷新与告警：不完整不重建名册、比对基线、身份信息沿用、飞书签名。数据虚构。"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import inventory
from delivery import people as people_mod
from delivery.alerts import AlertError, send_feishu, sign
from delivery.refresh import RefreshReport, brief, diff_snapshots, next_baseline, run

HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def _snap(users, *, errors=(), groups=(), captured="2026-09-15T10:00:00+08:00"):
    accounts = {}
    for platform, account, name, policies, *rest in users:
        entry = {"name": name, "policies": policies}
        if rest:
            entry["groups"] = rest[0]
        accounts.setdefault((platform, account), {"users": [], "groups": []})["users"].append(entry)
    for platform, account, name, policies, members in groups:
        accounts.setdefault((platform, account), {"users": [], "groups": []})["groups"].append(
            {"name": name, "policies": policies, "members": members}
        )
    data = {
        "captured_at": captured,
        "accounts": [{"platform": p, "account": a, **v} for (p, a), v in accounts.items()],
    }
    for platform, account in errors:
        data["accounts"].append({"platform": platform, "account": account, "error": "超时"})
    return data


def link(name, status="confirmed"):
    return {"scope": "aliyun/100", "name": name, "status": status, "display_name": ""}


PROPOSAL = {
    "schema": "wuji-sso-map/proposal@1",
    "domain": "wuji.tech",
    "people": [{"email": "li.si@wuji.tech", "links": [link("lisi")]}],
    "unlinked": [{"scope": "aliyun/100", "name": "ghost", "display_name": "", "reason": "无邮箱"}],
    "services": [],
}


class _Sink:
    def __init__(self):
        self.snapshot = self.baseline = self.proposal = self.people = None

    def kw(self):
        return {
            "write_snapshot": lambda d: setattr(self, "snapshot", d),
            "write_baseline": lambda d: setattr(self, "baseline", d),
            "write_proposal": lambda d: setattr(self, "proposal", d),
            "write_people": lambda d: setattr(self, "people", d),
        }


def _run(snapshot, sink=None, **kw):
    sink = sink or _Sink()
    opts = {
        "collect_snapshot": lambda: snapshot,
        "collect_proposal": lambda: PROPOSAL,
        "directory": lambda: [],
        "manual": None,
        "previous_baseline": None,
        "previous_people": None,
        "carry_over": False,
        **sink.kw(),
    }
    opts.update(kw)
    return run(**opts), sink


class RunTests(unittest.TestCase):
    def test_complete_run_writes_everything(self):
        report, sink = _run(_snap([("aliyun", "100", "lisi", [])]))
        self.assertTrue(report.ok)
        self.assertTrue(report.snapshot_written and report.people_written)
        self.assertEqual(sink.people["stats"]["people"], 1)
        self.assertIsNotNone(sink.proposal)
        self.assertIsNotNone(sink.baseline)

    def test_incomplete_snapshot_keeps_old_people(self):
        def boom():
            raise AssertionError("不完整时不该再采集提案")

        report, sink = _run(
            _snap([("aliyun", "100", "lisi", [])], errors=[("volcano", "default")]),
            collect_proposal=boom,
        )
        self.assertFalse(report.ok)
        self.assertTrue(report.snapshot_written)
        self.assertIsNone(sink.people)
        self.assertIsNone(sink.proposal)
        self.assertIn("volcano/default：超时", report.problems)
        self.assertIn("名册未更新", report.render())

    def test_snapshot_failure_writes_nothing(self):
        def fail():
            raise RuntimeError("\n凭证缺失\nAccessKeyId=xxx")

        report, sink = _run(None, collect_snapshot=fail)
        self.assertIsNone(sink.snapshot)
        self.assertEqual(report.problems, ["权限快照采集失败：RuntimeError: 凭证缺失"])

    def test_proposal_or_directory_failure_keeps_old_people_and_proposal(self):
        def fail():
            raise RuntimeError("ListUsers 超时")

        for kw in ({"collect_proposal": fail}, {"directory": fail}):
            report, sink = _run(_snap([("aliyun", "100", "lisi", [])]), **kw)
            self.assertTrue(report.snapshot_written)
            self.assertIsNone(sink.people)
            self.assertIsNone(sink.proposal, kw)
            self.assertIn("名册重建失败", report.problems[0])

    def test_unreadable_previous_people_blocks_rebuild(self):
        report, sink = _run(
            _snap([("aliyun", "100", "lisi", [])]),
            carry_over=True,
            previous_errors=["上一份名册读不了：JSONDecodeError"],
        )
        self.assertFalse(report.ok)
        self.assertTrue(report.needs_attention)
        self.assertIsNone(sink.people)

    def test_write_failure_keeps_report_state(self):
        def fail(d):
            raise OSError("disk full")

        report, _ = _run(_snap([("aliyun", "100", "lisi", [])]), write_people=fail)
        self.assertTrue(report.snapshot_written)
        self.assertFalse(report.people_written)
        self.assertIn("名册写入失败：OSError: disk full", report.problems)

    def test_manual_links_applied(self):
        manual = {"links": {"li.si@wuji.tech": {"accounts": ["aliyun/100/ghost"]}}}
        _, sink = _run(_snap([("aliyun", "100", "lisi", [])]), manual=manual)
        names = [a["name"] for a in sink.people["people"][0]["accounts"]]
        self.assertEqual(sorted(names), ["ghost", "lisi"])

    def test_new_unlinked_reported_against_previous(self):
        report, _ = _run(
            _snap([("aliyun", "100", "lisi", [])]), previous_people={"people": [], "unlinked": []}
        )
        self.assertEqual(report.new_unlinked, ["aliyun/100/ghost"])
        self.assertTrue(report.needs_attention)

    def test_first_run_does_not_flood(self):
        report, _ = _run(_snap([("aliyun", "100", "lisi", [])]))
        self.assertEqual(report.new_unlinked, [])
        self.assertFalse(report.needs_attention)


class CarryTests(unittest.TestCase):
    """people.carry_identities：不带通讯录重建时不能丢身份信息，也不能多带。"""

    def build(self, people=None):
        proposal = dict(PROPOSAL)
        if people is not None:
            proposal["people"] = people
        return people_mod.build(proposal, [])

    def row(self, uid, email="li.si@wuji.tech", accounts=("lisi",), **extra):
        return {
            "union_id": uid,
            "email": email,
            "name": extra.pop("name", email),
            "accounts": [{"platform": "aliyun", "account": "100", "name": n} for n in accounts],
            "pending": [],
            **extra,
        }

    def test_same_email_and_accounts_carries(self):
        new = self.build()
        stats = people_mod.carry_identities(new, [self.row("on_1", accounts=("LiSi",))])
        self.assertEqual(stats["carried"], 1)
        self.assertEqual(new["people"][0]["union_id"], "on_1")
        self.assertEqual(new["stats"]["with_union_id"], 1)

    def test_changed_accounts_do_not_carry_and_are_reported(self):
        new = self.build()
        stats = people_mod.carry_identities(new, [self.row("on_1", accounts=("other",))])
        self.assertEqual(new["people"][0]["union_id"], "")
        self.assertEqual(stats["lost"], ["li.si@wuji.tech"])

    def test_empty_previous_accounts_do_not_carry(self):
        new = self.build([{"email": "li.si@wuji.tech", "links": [link("x", "review")]}])
        people_mod.carry_identities(new, [self.row("on_1", accounts=())])
        self.assertEqual(new["people"][0]["union_id"], "")

    def test_email_collision_flag_carried(self):
        new = self.build()
        people_mod.carry_identities(new, [self.row("", email_collision=True)])
        self.assertTrue(new["people"][0]["email_collision"])
        index = people_mod.parse(new)
        found = index.resolve(union_id="on_y", enterprise_email="li.si@wuji.tech")
        self.assertEqual(found.binding, "conflict")

    def test_duplicate_union_id_never_carried(self):
        new = self.build(
            [
                {"email": "li.si@wuji.tech", "links": [link("lisi")]},
                {"email": "b@wuji.tech", "links": [link("bb")]},
            ]
        )
        previous = [self.row("on_dup"), self.row("on_dup", email="b@wuji.tech", accounts=("bb",))]
        stats = people_mod.carry_identities(new, previous)
        self.assertEqual([p["union_id"] for p in new["people"]], ["", ""])
        self.assertEqual(stats["duplicates"], ["on_dup"])

    def test_ambiguous_email_does_not_carry(self):
        new = self.build()
        people_mod.carry_identities(new, [self.row("on_1"), self.row("on_2")])
        self.assertEqual(new["people"][0]["union_id"], "")

    def test_directory_only_people_kept(self):
        new = self.build()
        previous = [self.row("on_new", email="new@wuji.tech", accounts=(), name="新人")]
        people_mod.carry_identities(new, previous)
        self.assertEqual([p["union_id"] for p in new["people"] if p["union_id"]], ["on_new"])
        self.assertEqual(new["stats"]["people"], 2)


class DiffTests(unittest.TestCase):
    def test_added_removed_and_new_high_risk(self):
        before = inventory.parse(_snap([("aliyun", "100", "a", []), ("aliyun", "100", "b", [])]))
        after = inventory.parse(
            _snap([("aliyun", "100", "a", ["AdministratorAccess"]), ("aliyun", "100", "c", [])])
        )
        report = RefreshReport()
        diff_snapshots(before, after, report)
        self.assertEqual(report.added_users, ["aliyun/100/c"])
        self.assertEqual(report.removed_users, ["aliyun/100/b"])
        self.assertEqual(report.new_high_risk, ["aliyun/100/a：AdministratorAccess"])

    def test_high_risk_via_group_and_group_rename_not_new(self):
        admin = ["AdministratorAccess"]
        before = inventory.parse(
            _snap(
                [("aliyun", "100", "a", [], ["g1"])],
                groups=[("aliyun", "100", "g1", admin, ["a"])],
            )
        )
        renamed = inventory.parse(
            _snap(
                [("aliyun", "100", "a", [], ["g2"])],
                groups=[("aliyun", "100", "g2", admin, ["a"])],
            )
        )
        report = RefreshReport()
        diff_snapshots(before, renamed, report)
        self.assertEqual(report.new_high_risk, [])
        plain = inventory.parse(_snap([("aliyun", "100", "a", [])]))
        report = RefreshReport()
        diff_snapshots(plain, before, report)
        self.assertEqual(report.new_high_risk, ["aliyun/100/a：AdministratorAccess"])

    def test_failed_platform_on_either_side_excluded(self):
        full = inventory.parse(_snap([("volcano", "2", "x", []), ("aliyun", "100", "a", [])]))
        broken = inventory.parse(
            _snap([("aliyun", "100", "a", [])], errors=[("volcano", "default")])
        )
        for before, after in ((full, broken), (broken, full)):
            report = RefreshReport()
            diff_snapshots(before, after, report)
            self.assertEqual((report.added_users, report.removed_users), ([], []))

    def test_baseline_only_advances_for_successful_platforms(self):
        # 第一次：阿里云采集失败，期间有人被授予了超管
        base = _snap([("aliyun", "100", "a", []), ("volcano", "2", "x", [])])
        failing = _snap([("volcano", "2", "x", [])], errors=[("aliyun", "ALIYUN")])
        report, sink = _run(failing, previous_baseline=base)
        self.assertEqual(report.new_high_risk, [])
        # 第二次：阿里云恢复。基线里阿里云还是旧数据，新授予的超管能报出来
        recovered = _snap(
            [("aliyun", "100", "a", ["AdministratorAccess"]), ("volcano", "2", "x", [])]
        )
        report, _ = _run(recovered, previous_baseline=sink.baseline)
        self.assertEqual(report.new_high_risk, ["aliyun/100/a：AdministratorAccess"])
        self.assertEqual(report.added_users, [])

    def test_next_baseline_drops_error_entries(self):
        data = next_baseline(None, _snap([], errors=[("aliyun", "ALIYUN")]))
        self.assertEqual(data["accounts"], [])

    def test_brief_skips_blank_lines(self):
        self.assertEqual(brief(RuntimeError("\n\n  真正的原因\n细节")), "RuntimeError: 真正的原因")


class AlertTests(unittest.TestCase):
    def test_signature_matches_feishu_algorithm(self):
        expected = base64.b64encode(
            hmac.new(b"1700000000\nsec", b"", digestmod=hashlib.sha256).digest()
        ).decode()
        self.assertEqual(sign(1700000000, "sec"), expected)

    def test_payload_signed_and_truncated(self):
        sent = {}

        def post(url, payload):
            sent.update(url=url, payload=payload)
            return {"code": 0}

        send_feishu("x" * 5000, webhook=HOOK, secret="sec", post=post, clock=lambda: 1700000000)
        self.assertEqual(sent["payload"]["timestamp"], "1700000000")
        self.assertEqual(sent["payload"]["sign"], sign(1700000000, "sec"))
        self.assertLess(len(sent["payload"]["content"]["text"]), 4100)

    def test_status_code_zero_accepted(self):
        send_feishu("t", webhook=HOOK, secret="s", post=lambda u, p: {"StatusCode": 0})

    def test_rejects_bad_webhooks(self):
        for bad in (
            "https://evil.example.com/hook",
            "http://open.feishu.cn/open-apis/bot/v2/hook/0f1e2d3c-4b5a",
            HOOK + " x",
            HOOK + "/../../evil",
            HOOK + "\n",
            "https://open.feishu.cn/open-apis/bot/v2/hook/",
        ):
            with self.assertRaises(AlertError, msg=bad):
                send_feishu("t", webhook=bad, secret="s", post=lambda u, p: {"code": 0})

    def test_requires_secret(self):
        with self.assertRaises(AlertError):
            send_feishu("t", webhook=HOOK, secret="", post=lambda u, p: {"code": 0})

    def test_errors_never_echo_webhook(self):
        import http.client

        errors = (
            OSError(f"failed {HOOK}"),
            http.client.IncompleteRead(HOOK.encode()),
            KeyError(HOOK),
        )
        for exc in errors:

            def post(url, payload, exc=exc):
                raise exc

            with self.assertRaises(AlertError) as ctx:
                send_feishu("t", webhook=HOOK, secret="s", post=post)
            self.assertNotIn("0f1e2d3c", str(ctx.exception))

    def test_unclear_responses_are_failures(self):
        for data in ({}, {"code": 19021, "msg": "sign"}, [1], {"StatusCode": 1}, {"code": False}):
            with self.assertRaises(AlertError, msg=data):
                send_feishu("t", webhook=HOOK, secret="s", post=lambda u, p, d=data: d)


class CliRefreshTests(unittest.TestCase):
    """delivery refresh：离线，临时目录 chdir 当仓库根，云采集和告警全部替换。"""

    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        os.chdir(self.root)
        self.id_dir = self.root / "identity"
        self.id_dir.mkdir()
        self.sent = []
        self.snapshot = _snap([("aliyun", "100", "lisi", [])])
        self.aliyun_calls = []

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def _main(self, *argv, send=None, alert=True):
        from delivery import alerts, cli, inventory_collect
        from delivery.identity import cloudcollect, ssomap

        proposal = mock.Mock()
        proposal.to_dict.return_value = PROPOSAL
        environ = {
            "ALIYUN_ACCESS_KEY_ID": "id",
            "ALIYUN_ACCESS_KEY_SECRET": "sk",
            "ALIYUN_B_ACCESS_KEY_ID": "id",
            "ALIYUN_B_ACCESS_KEY_SECRET": "sk",
            "VOLCANO_ACCESS_KEY": "id",
            "VOLCANO_SECRET_KEY": "sk",
        }
        if alert:
            environ.update({"DELIVERY_ALERT_WEBHOOK": HOOK, "DELIVERY_ALERT_SECRET": "s"})

        def collect_aliyun(creds, **kw):
            self.aliyun_calls.append(creds)
            return []

        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(inventory_collect, "build_snapshot", lambda *a, **k: self.snapshot),
            mock.patch.object(cloudcollect, "collect_aliyun", collect_aliyun),
            mock.patch.object(cloudcollect, "collect_volcano", lambda *a, **k: []),
            mock.patch.object(ssomap, "propose", lambda *a, **k: proposal),
            mock.patch.object(
                alerts, "send_feishu", send or (lambda text, **kw: self.sent.append(text))
            ),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli.main(["refresh", *argv])
        return code, out.getvalue()

    def test_complete_run_writes_private_files_without_alert(self):
        code, out = self._main()
        self.assertEqual(code, 0, out)
        names = (
            "inventory.json",
            "inventory.baseline.json",
            "sso-map.proposal.json",
            "people.json",
        )
        for name in names:
            path = self.id_dir / name
            self.assertTrue(path.exists(), name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, name)
        self.assertEqual(self.sent, [])

    def test_incomplete_run_alerts_and_keeps_people(self):
        (self.id_dir / "people.json").write_text(
            '{"schema": "wuji-people@1", "people": []}', encoding="utf-8"
        )
        self.snapshot = _snap([("aliyun", "100", "lisi", [])], errors=[("volcano", "default")])
        code, _ = self._main()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("volcano/default：超时", self.sent[0])
        self.assertEqual(
            (self.id_dir / "people.json").read_text(encoding="utf-8"),
            '{"schema": "wuji-people@1", "people": []}',
        )

    def test_corrupt_previous_people_alerts_and_keeps_file(self):
        (self.id_dir / "people.json").write_text("{broken", encoding="utf-8")
        code, _ = self._main()
        self.assertEqual(code, 1)
        self.assertIn("上一份名册读不了", self.sent[0])
        self.assertEqual((self.id_dir / "people.json").read_text(encoding="utf-8"), "{broken")

    def test_wrong_shape_previous_people_alerts_and_keeps_file(self):
        for bad in ("{}", '{"schema": "wuji-people@1", "people": {"a": 1}}'):
            (self.id_dir / "people.json").write_text(bad, encoding="utf-8")
            self.sent.clear()
            code, _ = self._main()
            self.assertEqual(code, 1, bad)
            self.assertIn("上一份名册读不了", self.sent[0])
            self.assertEqual((self.id_dir / "people.json").read_text(encoding="utf-8"), bad)

    def test_corrupt_previous_baseline_alerts(self):
        (self.id_dir / "inventory.baseline.json").write_text("{broken", encoding="utf-8")
        code, _ = self._main()
        self.assertEqual(code, 1)
        self.assertIn("上一份比对基线读不了", self.sent[0])

    def test_second_run_reports_new_high_risk(self):
        self._main()
        self.snapshot = _snap([("aliyun", "100", "lisi", ["AdministratorAccess"])])
        code, _ = self._main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("aliyun/100/lisi：AdministratorAccess", self.sent[0])

    def test_all_aliyun_profiles_used_for_proposal(self):
        code, out = self._main("--aliyun-profile", "ALIYUN", "--aliyun-profile", "ALIYUN_B")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.aliyun_calls), 2)

    def test_alert_failure_fails_the_run(self):
        def fail(text, **kw):
            raise AlertError("告警发送失败：URLError")

        self.snapshot = _snap([], errors=[("aliyun", "ALIYUN")])
        code, out = self._main(send=fail)
        self.assertEqual(code, 1)
        self.assertIn("告警发送失败", out)

    def test_attention_without_alert_config_fails(self):
        self.snapshot = _snap([], errors=[("aliyun", "ALIYUN")])
        code, out = self._main(alert=False)
        self.assertEqual(code, 1)
        self.assertIn("没设置", out)

    def test_no_alert_flag(self):
        self.snapshot = _snap([], errors=[("aliyun", "ALIYUN")])
        code, _ = self._main("--no-alert")
        self.assertEqual(code, 1)
        self.assertEqual(self.sent, [])

    def test_guard_failure_is_alerted(self):
        outside = Path(tempfile.mkdtemp()) / "people.json"
        code, _ = self._main("--people", str(outside))
        self.assertEqual(code, 1)
        self.assertIn("刷新中断", self.sent[0])

    def test_concurrent_run_skipped(self):
        fd = os.open(self.id_dir / ".refresh.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out = self._main()
        finally:
            os.close(fd)
        self.assertEqual(code, 75)
        self.assertIn("跳过", out)
        self.assertEqual(self.sent, [])

    def test_carries_union_id_with_directory_none(self):
        previous = {
            "schema": "wuji-people@1",
            "people": [
                {
                    "union_id": "on_1",
                    "email": "li.si@wuji.tech",
                    "accounts": [{"platform": "aliyun", "account": "100", "name": "lisi"}],
                }
            ],
        }
        (self.id_dir / "people.json").write_text(json.dumps(previous), encoding="utf-8")
        code, out = self._main()
        self.assertEqual(code, 0, out)
        data = json.loads((self.id_dir / "people.json").read_text(encoding="utf-8"))
        self.assertEqual(data["people"][0]["union_id"], "on_1")


if __name__ == "__main__":
    unittest.main()
