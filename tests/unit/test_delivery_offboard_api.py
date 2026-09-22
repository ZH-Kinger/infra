"""离职停号的两个入口：定时任务 `identity iam-remind` 与面板 `/api/admin/iam-attributes`。

  · 定时任务：强信号（IT 的 IAM 标离职 / 飞书状态已离职）→ 自动停用 + 发卡片；
    超过上限一个都不停、卡片里说清楚；弱信号（通讯录里找不到）只记 suspect、不停。
  · 面板：`offboard_delete` / `offboard_restore` 只认记录文件里的 key；`reclaim` 删完登录名
    再删云上的号；GET 预览带 `offboard` 待办。管理员 + 同源，和其它操作一样。

离线，数据虚构。执行身份一律换成假的（`executor_from_env` 被 patch）。
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delivery import iam_api, offboard
from delivery.errors import DeliveryError
from delivery.feishu import FeishuUser
from delivery.people import AccountRef, Person
from delivery.provision import ProvisionError
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _WebSession, make_handler

ALI_SCOPE, ALI_APP = next((k, v) for k, v in iam_api.APPS.items() if k.startswith("aliyun/"))
VOLC_SCOPE, VOLC_APP = next((k, v) for k, v in iam_api.APPS.items() if k.startswith("volcano/"))
ALI_ACC = ALI_SCOPE.split("/", 1)[1]
VOLC_ACC = VOLC_SCOPE.split("/", 1)[1]
IAM = "/api/admin/iam-attributes"


class FakeEx:
    def __init__(self, book, platform, account):
        self.book, self.platform, self.account = book, platform, account

    def disable_user(self, user):
        self.book.calls.append(("disable", self.platform, self.account, user))
        if user in self.book.fail:
            raise ProvisionError(f"{user} 停用失败")
        return {"login": True, "keys": [f"AK-{user}"]}

    def enable_user(self, user, *, login, keys):
        self.book.calls.append(("enable", self.platform, self.account, user, login, tuple(keys)))

    def delete_user(self, user):
        self.book.calls.append(("delete", self.platform, self.account, user))
        return list(self.book.left.get(user, []))

    def __getattr__(self, name):
        # 任何别的方法（尤其是碰数据的）被调就记下来，用例据此断言「没碰数据」
        def other(*a, **k):
            self.book.calls.append(("OTHER:" + name, self.platform, self.account, a))
            raise AssertionError(f"离职流程不该调 {name}")

        return other


class Book:
    def __init__(self):
        self.calls, self.fail, self.left = [], set(), {}

    def factory(self, platform, account, **kw):
        self.calls.append(("factory", platform, account, tuple(sorted(kw))))
        return FakeEx(self, platform, account)

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def others(self):
        return [c for c in self.calls if c[0].startswith("OTHER:")]


def ref(platform, name):
    return AccountRef(platform, ALI_ACC if platform == "aliyun" else VOLC_ACC, name)


def person(name, uid, *refs, email=""):
    return Person(name=name, email=email or f"{uid}@wuji.tech", union_id=uid, accounts=tuple(refs))


# ── 定时任务 ─────────────────────────────────────────────────────────────


class RemindOffboardTests(unittest.TestCase):
    """`_cmd_identity_iam_remind` 的强/弱两路。驱动方式同 test_delivery_not_in_directory。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "people.json").write_text("{}", encoding="utf-8")
        (self.dir / "admins.json").write_text("{}", encoding="utf-8")
        self.path = offboard.path_beside(str(self.dir / "people.json"))
        self.book = Book()

    def run_remind(self, roster, *, drift=(), statuses=None, staff=None, status_error=None):
        from delivery import cli, server  # noqa: F401 — 先导入，别在 patch 期间导入

        args = SimpleNamespace(
            people=str(self.dir / "people.json"),
            attributes=str(self.dir / "attrs.json"),
            admins=str(self.dir / "admins.json"),
            every_hours=24.0,
        )
        report = {
            "apps": [
                {
                    "app": ALI_APP,
                    "drift": [
                        {"kind": "inactive", "union_id": uid, "name": uid, "theirs": f"{uid}@x"}
                        for uid in drift
                    ],
                }
            ]
        }
        sent = []
        notifier = mock.Mock()
        notifier.send.side_effect = lambda uid, card, id_type: sent.append(card)
        env = {"DELIVERY_FEISHU_APP_ID": "a", "DELIVERY_FEISHU_APP_SECRET": "b"}
        # 通讯录要「够全」才会判弱信号：默认让名册里所有人都在
        if staff is None:
            staff = {p.email.lower(): {"union_id": p.union_id} for p in roster}
        status_kw = (
            {"side_effect": status_error} if status_error else {"return_value": statuses or {}}
        )
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("delivery.iam_sync.reconcile_report", return_value=report),
            mock.patch("delivery.iam_sync.load_snooze", return_value=set()),
            mock.patch("delivery.people.load", return_value=SimpleNamespace(people=roster)),
            mock.patch("delivery.identity.directory.staff_index", return_value=staff),
            mock.patch("delivery.identity.directory.status_of", **status_kw),
            mock.patch(
                "delivery.roles.load_admins", return_value=SimpleNamespace(union_ids={"on_admin"})
            ),
            mock.patch("delivery.notify.FeishuNotifier", return_value=notifier),
            mock.patch("delivery.server._tenant_token_cache", return_value=lambda: "t"),
            mock.patch("delivery.provision.executor_from_env", side_effect=self.book.factory),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli._cmd_identity_iam_remind(args)
        return code, sent

    def texts(self, sent):
        return [json.dumps(c, ensure_ascii=False) for c in sent]

    def test_iam_inactive_drift_disables_records_and_sends_card(self):
        gone = person("离职甲", "on_g", ref("aliyun", "jia"), ref("volcano", "jia"))
        stay = person("在职乙", "on_s", ref("aliyun", "yi"))
        code, sent = self.run_remind([gone, stay], drift=["on_g"])
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted((c[1], c[3]) for c in self.book.of("disable")),
            [("aliyun", "jia"), ("volcano", "jia")],
        )
        recs = offboard.load(self.path)
        self.assertEqual(set(recs), {f"aliyun/{ALI_ACC}/jia", f"volcano/{VOLC_ACC}/jia"})
        self.assertTrue(all(r["state"] == "disabled" for r in recs.values()))
        self.assertEqual(recs[f"aliyun/{ALI_ACC}/jia"]["signal"], "IT 的 IAM 标记离职")
        cards = [t for t in self.texts(sent) if "已停用" in t and "离职甲" in t]
        self.assertEqual(len(cards), 1, self.texts(sent))
        self.assertIn("已停用 2 个离职人员的云账号", cards[0])
        # 停用走开通身份，不走凭证发放身份
        self.assertTrue(all(c[3] == () for c in self.book.of("factory")))
        self.assertEqual(self.book.of("delete"), [])
        self.assertEqual(self.book.others(), [])
        # 留痕：review.log 一号一行
        log = (self.dir / "review.log").read_text(encoding="utf-8").splitlines()
        ops = [json.loads(line)["op"] for line in log]
        self.assertEqual(ops.count("offboard_disable"), 2)

    def test_feishu_resigned_status_disables(self):
        gone = person("离职丙", "on_r", ref("aliyun", "bing"))
        code, sent = self.run_remind([gone], statuses={"on_r": {"is_resigned": True}})
        self.assertEqual(code, 0)
        self.assertEqual([c[3] for c in self.book.of("disable")], ["bing"])
        rec = offboard.load(self.path)[f"aliyun/{ALI_ACC}/bing"]
        self.assertIn("飞书状态", rec["signal"])
        self.assertTrue(any("离职丙" in t for t in self.texts(sent)))

    def test_over_limit_disables_nobody_and_card_says_so(self):
        n = offboard.MAX_AUTO_PEOPLE + 1
        roster = [person(f"人{i}", f"on_{i}", ref("aliyun", f"u{i}")) for i in range(n)]
        code, sent = self.run_remind(
            roster, statuses={f"on_{i}": {"is_resigned": True} for i in range(n)}
        )
        self.assertEqual(self.book.of("disable"), [])
        # M-1：一个都没停，但都记成嫌疑，面板上能点
        recs = offboard.load(self.path)
        self.assertEqual(len(recs), n)
        self.assertTrue(all(r["state"] == "suspect" for r in recs.values()))
        self.assertTrue(all("这一轮判离职的人太多" in r["signal"] for r in recs.values()))
        texts = [t for t in self.texts(sent) if "超过自动停用的上限" in t]
        self.assertEqual(len(texts), 1, self.texts(sent))
        self.assertIn(f"这一轮有 {n} 人被判离职", texts[0])
        self.assertIn("离职停号需要你看一下", texts[0])
        self.assertEqual(code, 0)
        # 同一批被拦下的，24 小时内不再发卡；也还是一个都不停
        _code, again = self.run_remind(
            roster, statuses={f"on_{i}": {"is_resigned": True} for i in range(n)}
        )
        self.assertFalse(any("超过自动停用的上限" in t for t in self.texts(again)))
        self.assertEqual(self.book.of("disable"), [])

    def test_held_batch_change_sends_again(self):
        """去重按「这一批是谁」签名：多了一个人就是新的一批，要再说。"""
        n = offboard.MAX_AUTO_PEOPLE + 1
        roster = [person(f"人{i}", f"on_{i}", ref("aliyun", f"u{i}")) for i in range(n + 1)]
        self.run_remind(roster, statuses={f"on_{i}": {"is_resigned": True} for i in range(n)})
        _code, sent = self.run_remind(
            roster, statuses={f"on_{i}": {"is_resigned": True} for i in range(n + 1)}
        )
        self.assertTrue(any("超过自动停用的上限" in t for t in self.texts(sent)))

    def test_rounds_that_disable_always_send(self):
        """停了号每次都要说，不走 24 小时去重。"""
        a = person("离职甲", "on_a", ref("aliyun", "jia"))
        b = person("离职乙", "on_b", ref("aliyun", "yi"))
        self.run_remind([a, b], statuses={"on_a": {"is_resigned": True}})
        _code, sent = self.run_remind(
            [a, b], statuses={"on_a": {"is_resigned": True}, "on_b": {"is_resigned": True}}
        )
        self.assertTrue(
            any("已停用 1 个离职人员的云账号" in t and "离职乙" in t for t in self.texts(sent))
        )

    def test_frozen_status_records_suspect_never_disables(self):
        """H-1：冻结不是离职（长假、临时封禁）→ 只记嫌疑，不停。"""
        f = person("冻结戊", "on_f", ref("aliyun", "wu"), ref("volcano", "wu"))
        e = person("退出己", "on_e", ref("aliyun", "ji"))
        code, _sent = self.run_remind(
            [f, e], statuses={"on_f": {"is_frozen": True}, "on_e": {"is_exited": True}}
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.book.of("disable"), [])
        self.assertEqual(self.book.of("factory"), [])
        recs = offboard.load(self.path)
        self.assertEqual(
            {k: (r["state"], r["signal"]) for k, r in recs.items()},
            {
                f"aliyun/{ALI_ACC}/wu": ("suspect", "飞书状态：账号被冻结（没有自动停用）"),
                f"volcano/{VOLC_ACC}/wu": ("suspect", "飞书状态：账号被冻结（没有自动停用）"),
                f"aliyun/{ALI_ACC}/ji": ("suspect", "飞书状态：已退出企业（没有自动停用）"),
            },
        )

    def test_frozen_then_resigned_is_upgraded_to_disabled(self):
        f = person("冻结戊", "on_f", ref("aliyun", "wu"))
        self.run_remind([f], statuses={"on_f": {"is_frozen": True}})
        self.run_remind([f], statuses={"on_f": {"is_resigned": True}})
        self.assertEqual([c[3] for c in self.book.of("disable")], ["wu"])
        self.assertEqual(offboard.load(self.path)[f"aliyun/{ALI_ACC}/wu"]["state"], "disabled")

    def test_disable_failure_is_reported_and_exit_nonzero(self):
        self.book.fail.add("bad")
        gone = person("离职丁", "on_d", ref("aliyun", "bad"), ref("volcano", "ok"))
        code, sent = self.run_remind([gone], drift=["on_d"])
        self.assertEqual(code, 1)
        recs = offboard.load(self.path)
        self.assertEqual(recs[f"volcano/{VOLC_ACC}/ok"]["state"], "disabled")
        # 一步都没做成 → 嫌疑 + incomplete，下一轮再试
        bad = recs[f"aliyun/{ALI_ACC}/bad"]
        self.assertEqual(bad["state"], "suspect")
        self.assertIn("停用失败", bad["incomplete"])
        self.assertTrue(any("停用失败" in t for t in self.texts(sent)))
        self.book.fail.clear()
        self.book.calls.clear()
        code, _ = self.run_remind([gone], drift=["on_d"])
        self.assertEqual(code, 0)
        self.assertEqual([c[3] for c in self.book.of("disable")], ["bad"])
        self.assertEqual(offboard.load(self.path)[f"aliyun/{ALI_ACC}/bad"]["state"], "disabled")

    def test_failure_only_card_is_deduped(self):
        self.book.fail.add("bad")
        gone = person("离职丁", "on_d", ref("aliyun", "bad"))
        _c, first = self.run_remind([gone], drift=["on_d"])
        self.assertTrue(any("停用失败" in t for t in self.texts(first)))
        _c, second = self.run_remind([gone], drift=["on_d"])
        self.assertFalse(any("停用失败" in t for t in self.texts(second)))
        # 卡片去重了，但每一轮都还在重试
        self.assertEqual(len(self.book.of("disable")), 2)

    def test_second_run_does_not_disable_again(self):
        gone = person("离职甲", "on_g", ref("aliyun", "jia"))
        self.run_remind([gone], drift=["on_g"])
        self.book.calls.clear()
        _code, sent = self.run_remind([gone], drift=["on_g"])
        self.assertEqual(self.book.of("disable"), [])
        self.assertFalse(any("已停用" in t and "个离职人员" in t for t in self.texts(sent)))

    def test_protected_in_roster_never_disabled(self):
        svc = person("服务号", "on_p", ref("aliyun", "panel-executor"), ref("volcano", "power-x"))
        self.run_remind([svc], drift=["on_p"])
        self.assertEqual(self.book.of("disable"), [])
        self.assertEqual(offboard.load(self.path), {})

    def test_weak_signal_records_suspect_but_disables_nothing(self):
        peers = [person(f"同事{i}", f"on_p{i}", ref("aliyun", f"p{i}")) for i in range(8)]
        missing = Person(name="找不到", email="lost@wuji.tech", accounts=(ref("aliyun", "lost"),))
        staff = {p.email.lower(): {} for p in peers}
        code, sent = self.run_remind([*peers, missing], staff=staff)
        self.assertEqual(code, 0)
        self.assertEqual(self.book.of("disable"), [])
        self.assertEqual(self.book.of("factory"), [])
        recs = offboard.load(self.path)
        self.assertEqual(list(recs), [f"aliyun/{ALI_ACC}/lost"])
        self.assertEqual(recs[f"aliyun/{ALI_ACC}/lost"]["state"], "suspect")
        self.assertTrue(any("找不到" in t and "没有自动停用" in t for t in self.texts(sent)))

    def test_status_lookup_failure_still_uses_iam_drift(self):
        """飞书在职状态查不成时，IAM 那一路照样停；退出码非零（这一路没结论）。"""
        gone = person("离职甲", "on_g", ref("aliyun", "jia"))
        code, _sent = self.run_remind([gone], drift=["on_g"], status_error=OSError("net"))
        self.assertEqual(code, 1)
        self.assertEqual([c[3] for c in self.book.of("disable")], ["jia"])

    def test_nothing_to_do_sends_nothing(self):
        stay = person("在职乙", "on_s", ref("aliyun", "yi"))
        code, sent = self.run_remind([stay], statuses={"on_s": {}})
        self.assertEqual((code, sent), (0, []))
        self.assertEqual(self.book.calls, [])

    def test_corrupt_offboard_file_does_not_swallow_other_reminders(self):
        """回归（曾是 bug）：offboard.json 读坏时 `auto_disable` 抛 `OffboardError`，cli.py 没接，
        整个 iam-remind 崩掉 —— 连改动前就有的「已离职、云登录名还挂着」那张卡也发不出去。
        期望：这一路报错、退出码非零，其余提醒照常发。cli.py `_cmd_identity_iam_remind`
        里 `offboard.auto_disable(...)` / `offboard.note_suspects(...)` 两处。"""
        self.path.write_text("{broken", encoding="utf-8")
        gone = person("离职甲", "on_g", ref("aliyun", "jia"))
        code, sent = self.run_remind([gone], drift=["on_g"])
        self.assertEqual(code, 1)
        self.assertTrue(any("已离职" in t for t in self.texts(sent)), self.texts(sent))


# ── 面板接口 ─────────────────────────────────────────────────────────────


class _Live:
    """真 HTTP、真 handler（同 test_delivery_offline_registration 的夹具）。"""

    def __init__(self, backend):
        self.store = Store()
        handler = make_handler(
            PlatformRegistry.load(),
            self.store,
            app_id="cli_demo",
            app_secret="s",
            base_url="http://127.0.0.1",
            backend=backend,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, *, cookie="", payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        head = dict(headers or {})
        if cookie:
            head["Cookie"] = f"{COOKIE_NAME}={cookie}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            if headers is None:
                head.setdefault("Content-Type", "application/json")
                head.setdefault("X-Panel-Request", "1")
        conn.request(method, path, body=body, headers=head)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, raw.decode(errors="replace")


class OffboardApiTests(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        d = self.root / "identity"
        d.mkdir()
        (d / "attrs.json").write_text(json.dumps({ALI_SCOPE: "aliyun_username"}), "utf-8")
        (d / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), "utf-8")
        (d / "people.json").write_text(
            json.dumps(
                {
                    "schema": "wuji-people@1",
                    "people": [
                        {"union_id": "on_admin", "name": "管理员", "email": "admin@wuji.tech"},
                        {
                            "union_id": "on_1",
                            "name": "李四",
                            "email": "li.si@wuji.tech",
                            # 名册里的拼写：火山那个号大小写和 IAM 属性值不一样
                            "accounts": [
                                {"platform": "aliyun", "account": ALI_ACC, "name": "lisi"},
                                {"platform": "volcano", "account": VOLC_ACC, "name": "LiSi"},
                            ],
                        },
                        {
                            "union_id": "on_2",
                            "name": "张三",
                            "email": "zhang.san@wuji.tech",
                            "accounts": [
                                {"platform": "aliyun", "account": ALI_ACC, "name": "zhangsan"}
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            "utf-8",
        )
        (d / "proposal.json").write_text(
            json.dumps({"domain": "wuji.tech", "people": [], "unlinked": [], "services": []}),
            "utf-8",
        )
        (d / "manual.json").write_text(json.dumps({"links": []}), "utf-8")
        self.dir = d
        self.backend = Backend(
            people_path="identity/people.json",
            bindings_path="identity/bindings.json",
            admins_path="identity/admins.json",
            proposal_path="identity/proposal.json",
            manual_path="identity/manual.json",
            iam_spec_path="identity/attrs.json",
            iam_out_path="identity/iam-attributes.csv",
            platforms={"aliyun": "阿里云", "volcano": "火山引擎"},
        )
        self.path = offboard.path_beside("identity/people.json")
        self.book = Book()
        patcher = mock.patch("delivery.server.executor_from_env", side_effect=self.book.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, live, uid="on_admin"):
        sid = f"sid-{uid}"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(
                open_id="ou_x", union_id=uid, name="某人", email="", enterprise_email=""
            )
        )
        return sid

    def seed_disabled(self, user="lisi", platform="aliyun"):
        offboard.auto_disable(
            self.path,
            [(person("李四", "on_1", ref(platform, user)), "IT 的 IAM 标记离职")],
            lambda p, a: FakeEx(Book(), p, a),
        )
        return offboard.key_of(platform, ALI_ACC if platform == "aliyun" else VOLC_ACC, user)

    def post(self, live, payload, **kw):
        return live.request(
            "POST", IAM, cookie=kw.pop("cookie", self.login(live)), payload=payload, **kw
        )

    # ── offboard_delete / offboard_restore ──

    def test_delete_by_key(self):
        key = self.seed_disabled()
        with _Live(self.backend) as live:
            status, body = self.post(live, {"op": "offboard_delete", "key": key})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "deleted")
        self.assertEqual(body["decided_by"], "admin:on_admin")
        self.assertEqual(self.book.of("delete"), [("delete", "aliyun", ALI_ACC, "lisi")])
        self.assertEqual(self.book.others(), [])
        log = (self.dir / "review.log").read_text(encoding="utf-8").splitlines()
        row = json.loads(log[-1])
        self.assertEqual(
            (row["op"], row["user"], row["actor"]), ("offboard_delete", "lisi", "admin:on_admin")
        )

    def test_delete_unknown_key_is_409_and_no_cloud_call(self):
        """请求里给不了任意用户名：不在记录文件里的 key 一律拒。"""
        self.seed_disabled()
        with _Live(self.backend) as live:
            for key in (f"aliyun/{ALI_ACC}/wangwu", "", "../../etc/passwd", None, 123):
                status, body = self.post(live, {"op": "offboard_delete", "key": key})
                self.assertEqual(status, 409, (key, body))
            # 用 user / union_id 之类的字段绕也不行
            status, _ = self.post(
                live, {"op": "offboard_delete", "user": "wangwu", "platform": "aliyun"}
            )
            self.assertEqual(status, 409)
        self.assertEqual(self.book.of("delete"), [])
        self.assertEqual(self.book.of("factory"), [])

    def test_delete_with_leftovers_is_409_and_kept_pending(self):
        key = self.seed_disabled()
        self.book.left["lisi"] = ["摘策略 X：Throttling"]
        with _Live(self.backend) as live:
            status, body = self.post(live, {"op": "offboard_delete", "key": key})
            self.assertEqual(status, 409)
            self.assertIn("没删干净", body["error"])
            _s, preview = live.request("GET", IAM, cookie=self.login(live))
        rows = preview["offboard"]
        self.assertEqual([r["user"] for r in rows], ["lisi"])
        self.assertEqual(rows[0]["left"], ["摘策略 X：Throttling"])

    def test_restore_disabled_reenables_recorded_keys(self):
        key = self.seed_disabled()
        with _Live(self.backend) as live:
            status, body = self.post(live, {"op": "offboard_restore", "key": key})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "restored")
        self.assertEqual(
            self.book.of("enable"), [("enable", "aliyun", ALI_ACC, "lisi", True, ("AK-lisi",))]
        )
        self.assertEqual(self.book.of("delete"), [])

    def test_restore_suspect_is_dismissed_without_cloud(self):
        offboard.note_suspects(self.path, [person("李四", "on_1", ref("volcano", "lisi"))])
        key = offboard.key_of("volcano", VOLC_ACC, "lisi")
        with _Live(self.backend) as live:
            status, body = self.post(live, {"op": "offboard_restore", "key": key})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "dismissed")
        self.assertEqual(self.book.of("enable"), [])

    def test_second_decision_is_refused(self):
        key = self.seed_disabled()
        with _Live(self.backend) as live:
            self.assertEqual(self.post(live, {"op": "offboard_delete", "key": key})[0], 200)
            status, body = self.post(live, {"op": "offboard_restore", "key": key})
        self.assertEqual(status, 409)
        self.assertIn("已经处理过", body["error"])
        self.assertEqual(self.book.of("enable"), [])

    def test_non_admin_is_403_and_nothing_happens(self):
        key = self.seed_disabled()
        with _Live(self.backend) as live:
            status, _ = self.post(
                live, {"op": "offboard_delete", "key": key}, cookie=self.login(live, "on_1")
            )
            self.assertEqual(status, 403)
            status, _ = live.request("GET", IAM, cookie=self.login(live, "on_1"))
            self.assertEqual(status, 403)
            status, _ = live.request("POST", IAM, payload={"op": "offboard_delete", "key": key})
            self.assertEqual(status, 401)
        self.assertEqual(self.book.calls, [])
        self.assertEqual(offboard.load(self.path)[key]["state"], "disabled")

    def test_cross_site_is_refused(self):
        key = self.seed_disabled()
        with _Live(self.backend) as live:
            sid = self.login(live)
            for headers in (
                {"Content-Type": "application/json"},  # 缺 X-Panel-Request
                {"Content-Type": "text/plain", "X-Panel-Request": "1"},
                {
                    "Content-Type": "application/json",
                    "X-Panel-Request": "1",
                    "Origin": "https://evil.example",
                },
                {
                    "Content-Type": "application/json",
                    "X-Panel-Request": "1",
                    "Sec-Fetch-Site": "cross-site",
                },
            ):
                status, _ = live.request(
                    "POST",
                    IAM,
                    cookie=sid,
                    payload={"op": "offboard_delete", "key": key},
                    headers=headers,
                )
                self.assertEqual(status, 403, headers)
        self.assertEqual(self.book.calls, [])

    def test_get_preview_lists_only_pending(self):
        k1 = self.seed_disabled("lisi")
        offboard.note_suspects(self.path, [person("王五", "on_5", ref("volcano", "wangwu"))])
        self.seed_disabled("zhaoliu")
        offboard.decide(
            self.path,
            offboard.key_of("aliyun", ALI_ACC, "zhaoliu"),
            "delete",
            lambda p, a: FakeEx(Book(), p, a),
            actor="x",
        )
        with _Live(self.backend) as live:
            status, body = live.request("GET", IAM, cookie=self.login(live))
        self.assertEqual(status, 200, body)
        got = {(r["user"], r["state"]) for r in body["offboard"]}
        self.assertEqual(got, {("lisi", "disabled"), ("wangwu", "suspect")})
        self.assertIn(
            k1, {offboard.key_of(r["platform"], r["account"], r["user"]) for r in body["offboard"]}
        )

    def test_get_preview_with_unreadable_file_still_200(self):
        """离职记录坏了不该让整页打不开：`offboard: []` + `offboard_error`。"""
        self.path.write_text("{broken", encoding="utf-8")
        with _Live(self.backend) as live:
            status, body = live.request("GET", IAM, cookie=self.login(live))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["offboard"], [])
        self.assertIn("离职记录读不了", body["offboard_error"])
        self.assertNotIn("\n", body["offboard_error"])

    def test_get_preview_without_file_is_empty_list(self):
        with _Live(self.backend) as live:
            status, body = live.request("GET", IAM, cookie=self.login(live))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["offboard"], [])
        self.assertNotIn("offboard_error", body)

    # ── reclaim：删登录名之后再删云上的号 ──

    def reclaim(self, row, *, raises=None):
        kw = {"side_effect": raises} if raises else {"return_value": row}
        with (
            mock.patch("delivery.iam_sync.confirm_reclaim", **kw) as conf,
            _Live(self.backend) as live,
        ):
            status, body = self.post(
                live,
                {
                    "op": "reclaim",
                    "union_id": row.get("union_id", ""),
                    "app": row.get("app", ""),
                },
            )
        return status, body, conf

    def row(self, app=ALI_APP, previous="lisi@1000.onaliyun.com", **over):
        r = {
            "app": app,
            "union_id": "on_1",
            "username": "lisi",
            "name": "李四",
            "value": previous,
            "previous": previous,
            "why": "管理员确认离职",
        }
        r.update(over)
        return r

    def test_reclaim_deletes_cloud_user_on_mapped_account(self):
        status, body, conf = self.reclaim(self.row())
        self.assertEqual(status, 200, body)
        self.assertEqual(conf.call_args.kwargs["actor"], "on_admin")
        self.assertEqual(self.book.of("delete"), [("delete", "aliyun", ALI_ACC, "lisi")])
        self.assertIn("已删除", body["cloud"])
        rec = offboard.load(self.path)[offboard.key_of("aliyun", ALI_ACC, "lisi")]
        self.assertEqual(rec["state"], "deleted")
        self.assertEqual(rec["signal"], "管理员确认离职")

    def test_reclaim_volcano_bare_name_uses_roster_spelling(self):
        """M-2：归属比对不分大小写，删的时候用名册里的拼写。"""
        status, body, _ = self.reclaim(self.row(app=VOLC_APP, previous="lisi"))
        self.assertEqual(status, 200, body)
        self.assertEqual(self.book.of("delete"), [("delete", "volcano", VOLC_ACC, "LiSi")])
        self.assertIn(offboard.key_of("volcano", VOLC_ACC, "LiSi"), offboard.load(self.path))

    def test_reclaim_account_not_owned_is_not_deleted(self):
        """M-2：IAM 属性值被导错成别人的号（这里是张三的）→ 不删，只记嫌疑。"""
        status, body, _ = self.reclaim(self.row(previous="zhangsan@1000.onaliyun.com"))
        self.assertEqual(status, 200, body)
        self.assertIn("不在他名下", body["cloud"])
        self.assertEqual(self.book.calls, [])
        rec = offboard.load(self.path)[offboard.key_of("aliyun", ALI_ACC, "zhangsan")]
        self.assertEqual(rec["state"], "suspect")
        self.assertEqual(rec["signal"], "管理员确认离职，但名册里这个号不归他（没删）")

    def test_reclaim_person_not_in_roster_is_not_deleted(self):
        status, body, _ = self.reclaim(self.row(union_id="on_ghost"))
        self.assertEqual(status, 200, body)
        self.assertIn("不在他名下", body["cloud"])
        self.assertEqual(self.book.calls, [])

    def test_reclaim_account_on_other_cloud_account_is_not_deleted(self):
        """同名但归属在另一个云平台：(platform, account, user) 三者都要对上。"""
        status, body, _ = self.reclaim(self.row(app=VOLC_APP, previous="zhangsan"))
        self.assertEqual(status, 200, body)
        self.assertIn("不在他名下", body["cloud"])
        self.assertEqual(self.book.calls, [])

    def test_reclaim_already_deleted_is_not_deleted_again(self):
        key = self.seed_disabled()
        offboard.decide(self.path, key, "delete", lambda p, a: FakeEx(Book(), p, a), actor="x")
        status, body, _ = self.reclaim(self.row())
        self.assertEqual(status, 200, body)
        self.assertIn("之前已经删掉了", body["cloud"])
        self.assertEqual(self.book.calls, [])
        self.assertEqual(offboard.load(self.path)[key]["decided_by"], "x")

    def test_reclaim_uses_disabled_record_keys(self):
        """已被自动停过的号走 reclaim：沿用原记录（keys 不丢），然后删。"""
        key = self.seed_disabled()
        status, body, _ = self.reclaim(self.row())
        self.assertEqual(status, 200, body)
        rec = offboard.load(self.path)[key]
        self.assertEqual((rec["state"], rec["keys"]), ("deleted", ["AK-lisi"]))

    def test_reclaim_protected_user_is_not_deleted(self):
        status, body, _ = self.reclaim(self.row(previous="panel-executor@1.onaliyun.com"))
        self.assertEqual(status, 200, body)
        self.assertIn("受保护", body["cloud"])
        self.assertEqual(self.book.calls, [])
        self.assertEqual(offboard.load(self.path), {})

    def test_reclaim_without_user_value_touches_nothing(self):
        status, body, _ = self.reclaim(self.row(previous="", value=""))
        self.assertEqual(status, 200, body)
        self.assertIn("没认出", body["cloud"])
        self.assertEqual(self.book.calls, [])

    def test_reclaim_unknown_app_touches_nothing(self):
        status, body, _ = self.reclaim(self.row(app="jiuzhang-main"))
        self.assertEqual(status, 200, body)
        self.assertIn("没认出", body["cloud"])
        self.assertEqual(self.book.calls, [])

    def test_reclaim_refused_by_iam_never_reaches_cloud(self):
        """IAM 那边说他还在职 → 登录名不删，云上的号更不能动。"""
        status, body, _ = self.reclaim(self.row(), raises=DeliveryError("还是**在职**状态"))
        self.assertEqual(status, 409, body)
        self.assertEqual(self.book.calls, [])
        self.assertEqual(offboard.load(self.path), {})

    def test_reclaim_cloud_leftovers_reported_not_hidden(self):
        """登录名删了、云账号没删干净：回 200 但 `cloud` 里说清楚，记录留在待办。"""
        self.book.left["lisi"] = ["移出用户组 g：Throttling"]
        status, body, _ = self.reclaim(self.row())
        self.assertEqual(status, 200, body)
        self.assertIn("没删成", body["cloud"])
        self.assertEqual([r["user"] for r in offboard.pending(self.path)], ["lisi"])

    def test_reclaim_executor_not_configured_is_reported(self):
        def broken(platform, account, **kw):
            raise ProvisionError("没配执行身份")

        with mock.patch("delivery.server.executor_from_env", side_effect=broken):
            status, body, _ = self.reclaim(self.row())
        self.assertEqual(status, 200, body)
        self.assertIn("没删成", body["cloud"])

    def test_unknown_op_lists_new_ops(self):
        with _Live(self.backend) as live:
            status, body = self.post(live, {"op": "offboard_nuke"})
        self.assertEqual(status, 400)
        self.assertIn("offboard_delete", body["error"])
        self.assertIn("offboard_restore", body["error"])
        self.assertEqual(self.book.calls, [])


if __name__ == "__main__":
    unittest.main()
