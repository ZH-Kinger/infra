"""`GET /api/admin/todo` 和导航角标。

三条硬约束，每一条都能把这一页毁掉，而且毁掉的方式都不显眼：

1. **只有管理员能看**。这一页把离职名单、申请人邮箱、无主账号摊在一起 ——
   它比任何单独一页都更值得越权去拉一次。
2. **一次云调用都不发**。待办页是管理后台的落地页，每个管理员每次打开面板都会走它。
   往里塞一次 `ListUsers`，面板就变成「打开要等 8 秒，而且云那边一抖就白屏」。
3. **一个数据源读坏了只让那一类缺席**。整页 500 的后果不是「少看一类」，
   是管理员**看不到任何待办**，包括那条「有人离职了号还在」。

离线：临时目录当仓库根，数据虚构，网络整个拔掉。
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from delivery import server as server_mod
from delivery import tickets as tickets_mod
from delivery import todo as todo_mod
from delivery.errors import DeliveryError
from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _WebSession, make_handler

TODO = "/api/admin/todo"
ACC = "1000000000000001"
DAY = 86400


def iso(ts: float) -> str:
    import datetime

    return (
        datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


class _Live:
    """起一个真的 HTTP 服务：鉴权、路由、JSON 编码都要一起过一遍。"""

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

    def get(self, path, cookie=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        head = {"Cookie": f"{COOKIE_NAME}={cookie}"} if cookie else {}
        conn.request("GET", path, headers=head)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, {}


class Base(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        self.id_dir = self.root / "identity"
        self.id_dir.mkdir()
        self.write("admins.json", {"union_ids": ["on_admin"]})
        self.write(
            "people.json",
            {
                "schema": "wuji-people@1",
                "people": [
                    {
                        "name": "李四",
                        "email": "l@wuji.tech",
                        "union_id": "on_L",
                        "accounts": [{"platform": "aliyun", "account": ACC, "name": "lisi"}],
                        "pending": [],
                    }
                ],
            },
        )
        self.write("attrs.json", {f"aliyun/{ACC}": "aliyun_username"})
        self.write(
            "inventory.json",
            {
                "captured_at": iso(self.clock() - 600),
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": ACC,
                        "users": [
                            {"name": "lisi", "display_name": "李四", "policies": []},
                            {"name": "guikai", "display_name": "鬼", "policies": []},
                            {"name": "tempak-abc", "display_name": "发的", "policies": []},
                        ],
                        "groups": [],
                    }
                ],
            },
        )
        self.backend = self.make_backend()

    @staticmethod
    def clock() -> float:
        import time

        return time.time()

    def write(self, name, data):
        (self.id_dir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def make_backend(self, **over) -> Backend:
        kw = {
            "inventory_path": "identity/inventory.json",
            "people_path": "identity/people.json",
            "admins_path": "identity/admins.json",
            "iam_spec_path": "identity/attrs.json",
            "iam_out_path": "identity/iam-attributes.csv",
            "services_path": None,
            "platforms": {"aliyun": "阿里云"},
            # 开通身份：待办页碰它就说明有人往这条路径里塞了云调用
            "executor": self._no_cloud,
            "feishu_token": self._no_feishu,
        }
        kw.update(over)
        return Backend(**kw)

    @staticmethod
    def _no_cloud(platform, account):
        raise AssertionError(f"待办页不该要云执行器（{platform}/{account}）")

    @staticmethod
    def _no_feishu():
        raise AssertionError("待办页不该要飞书令牌")

    def offboard(self, *records):
        box = {f"{r['platform']}/{r['account']}/{r['user']}": r for r in records}
        self.write("offboard.json", {"records": box})

    def rec(self, **over):
        base = {
            "person": "李四",
            "user": "lisi",
            "platform": "aliyun",
            "account": ACC,
            "state": "disabled",
            "at": iso(self.clock() - 3 * DAY),
        }
        base.update(over)
        return base

    def tickets(self, *rows):
        path = self.root / "tickets.json"
        path.write_text(
            json.dumps({"schema": tickets_mod.SCHEMA, "tickets": list(rows)}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.backend = self.make_backend(tickets_path=str(path))

    def view(self) -> dict:
        return server_mod._todo_view(self.backend)

    def admin_session(self, live, uid="on_admin"):
        sid = f"sid-{uid}"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(open_id=f"ou_{uid}", union_id=uid, name="某人")
        )
        return sid

    def kinds(self, view=None) -> list:
        view = self.view() if view is None else view
        return [r["kind"] for g in view["groups"] for r in g["items"]]

    def item(self, kind, view=None):
        view = self.view() if view is None else view
        for g in view["groups"]:
            for r in g["items"]:
                if r["kind"] == kind:
                    return r
        self.fail(f"待办里没有 {kind}：{self.kinds(view)}")


class AccessTests(Base):
    def test_admin_gets_the_list(self):
        with _Live(self.backend) as live:
            status, data = live.get(TODO, self.admin_session(live))
        self.assertEqual(status, 200)
        self.assertIn("groups", data)
        self.assertIn("headline", data)

    def test_a_normal_employee_is_refused(self):
        """不是「看到一张空清单」，是**拒绝**。

        返回空清单的话，将来给这一页加一条按人名的明细，越权的人就直接看到别人的邮箱了 ——
        而那一刻不会有任何人重新想一遍鉴权。
        """
        with _Live(self.backend) as live:
            status, data = live.get(TODO, self.admin_session(live, "on_L"))
        self.assertEqual(status, 403)
        self.assertNotIn("groups", data)

    def test_no_session_is_refused(self):
        with _Live(self.backend) as live:
            status, _ = live.get(TODO)
        self.assertIn(status, (401, 403))


class NoCloudTests(Base):
    def test_the_whole_view_runs_with_the_network_pulled(self):
        """**零云调用**：把 urllib 整个拔掉，这一页照样出得来。

        面板里所有云 API 和飞书调用都走 `urllib.request.urlopen`（aliyun/volcano/feishu/
        approval/notify 无一例外），所以拔掉它就是「这次没联网」的充分证据。
        """
        self.offboard(self.rec())

        def boom(*a, **k):
            raise AssertionError("待办页发起了网络请求")

        with mock.patch.object(urllib.request, "urlopen", boom):
            view = self.view()
        self.assertIn("offboard_pending", self.kinds(view))

    def test_the_nav_badge_runs_with_the_network_pulled_too(self):
        # 角标在**每次拿会话时**都会跑。它要是打网络，整个面板的每一次点击都会卡
        user = FeishuUser(open_id="ou_a", union_id="on_admin", name="管理员")

        def boom(*a, **k):
            raise AssertionError("角标发起了网络请求")

        with mock.patch.object(urllib.request, "urlopen", boom):
            self.assertIn("total", self.backend.admin_todo(user))


class BadgeTests(Base):
    def test_badge_and_page_are_the_same_computation(self):
        """角标写 3、点进去列 5 —— 那之后没人会再看角标。

        所以这里断言的不是某个数字，而是「两处读的是同一份 counts」。
        """
        self.offboard(self.rec(user="a"), self.rec(user="b"), self.rec(person="王五", user="c"))
        user = FeishuUser(open_id="ou_a", union_id="on_admin", name="管理员")
        badge = self.backend.admin_todo(user)
        counts = self.view()["counts"]
        self.assertEqual(badge["urgent"], counts["urgent"])
        self.assertEqual(badge["total"], counts["total"])
        self.assertGreater(badge["urgent"], 0)

    def test_legacy_key_still_answers_for_old_front_ends(self):
        # 老前端读的是 iam_pending。删掉它会让缓存里的旧 app.js 角标恒为 0，
        # 而那看起来完全正常（「今天没事」），没人会去查
        self.offboard(self.rec())
        user = FeishuUser(open_id="ou_a", union_id="on_admin", name="管理员")
        badge = self.backend.admin_todo(user)
        self.assertEqual(badge["iam_pending"], badge["urgent"])

    def test_non_admin_gets_no_badge_payload(self):
        user = FeishuUser(open_id="ou_l", union_id="on_L", name="李四")
        self.assertEqual(self.backend.admin_todo(user), {})

    def test_badge_never_breaks_the_session(self):
        """角标算崩了返回空，而不是把登录整个搞失败。

        这条以前咬过人：一份读坏的名册会让**所有人**登不进面板，而错误信息指向的是会话。
        """
        user = FeishuUser(open_id="ou_a", union_id="on_admin", name="管理员")
        with mock.patch.object(server_mod, "_todo_view", side_effect=RuntimeError("炸了")):
            self.assertEqual(self.backend.admin_todo(user), {})


class ResilienceTests(Base):
    def test_one_broken_source_only_loses_that_category(self):
        """离职记录读坏 → 少那一类 + 一条 errors，其余照出、状态码仍是 200。"""
        (self.id_dir / "offboard.json").write_text("{ 不是 JSON", encoding="utf-8")
        with _Live(self.backend) as live:
            status, data = live.get(TODO, self.admin_session(live))
        self.assertEqual(status, 200)
        self.assertTrue(data["errors"], "读坏了必须说出来")
        self.assertIn("离职待办", data["errors"][0])
        self.assertNotIn("offboard_pending", self.kinds(data))
        # 名册那一类照常算出来（快照里有两个对不上人的号）
        self.assertIn("unlinked_account", self.kinds(data))

    def test_a_broken_roster_does_not_take_the_page_down(self):
        (self.id_dir / "people.json").write_text("{", encoding="utf-8")
        with _Live(self.backend) as live:
            status, data = live.get(TODO, self.admin_session(live))
        self.assertEqual(status, 200)
        self.assertTrue(any("名册" in e for e in data["errors"]))

    def test_a_broken_snapshot_is_reported_not_raised(self):
        (self.id_dir / "inventory.json").write_text("nope", encoding="utf-8")
        view = self.view()
        self.assertTrue(any("快照" in e for e in view["errors"]))
        self.assertTrue(view["freshness"]["权限快照"]["stale"])

    def test_errors_never_read_as_all_clear(self):
        """每一类都读坏时，标题必须说「没算出来」而不是「没有需要你处理的」。

        这是整页最危险的一种失败：全挂 = 一片干净，看起来和「今天真没事」一模一样。
        """
        (self.id_dir / "people.json").write_text("{", encoding="utf-8")
        (self.id_dir / "inventory.json").write_text("{", encoding="utf-8")
        (self.id_dir / "offboard.json").write_text("{", encoding="utf-8")
        view = self.view()
        self.assertNotIn("没有需要你处理的", view["headline"])
        self.assertIn("没算出来", view["headline"])

    def test_every_source_failing_still_returns_200(self):
        def boom(*a, **k):
            raise RuntimeError("这一类算不出来")

        with (
            mock.patch.object(todo_mod, "collect_offboard", boom),
            mock.patch.object(todo_mod, "collect_roster", boom),
            mock.patch.object(todo_mod, "collect_keys", boom),
            mock.patch.object(todo_mod, "collect_iam_files", boom),
            mock.patch.object(todo_mod, "collect_iam_drift", boom),
            _Live(self.backend) as live,
        ):
            status, data = live.get(TODO, self.admin_session(live))
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["errors"]), 4)

    def test_error_text_does_not_leak_the_raw_exception_body(self):
        # 只报类型名：读文件的异常里会带绝对路径，而这一页是网页
        def boom(*a, **k):
            raise RuntimeError("/root/secret/path 读不了")

        with mock.patch.object(todo_mod, "collect_keys", boom):
            view = self.view()
        self.assertTrue(any("密钥" in e for e in view["errors"]))
        self.assertNotIn("/root/secret/path", json.dumps(view, ensure_ascii=False))

    def test_no_iam_paths_means_those_categories_are_simply_absent(self):
        """没配属性表路径（新部署的常态）：不报错、不假装一切正常，那几类就是没有。"""
        backend = self.make_backend(iam_spec_path=None, iam_out_path=None)
        self.assertIsNone(backend.iam_paths())
        view = server_mod._todo_view(backend)
        kinds = self.kinds(view)
        self.assertNotIn("iam_inactive", kinds)
        self.assertNotIn("offboard_pending", kinds)
        self.assertEqual(view["errors"], [])

    def test_no_tickets_path_means_no_ticket_rows(self):
        view = self.view()
        self.assertNotIn("request_failed", self.kinds(view))
        self.assertEqual(view["errors"], [])


class WiringTests(Base):
    """接线：每一类确实接到了它那份数据上（写得对但没接上，页面永远是空的）。"""

    def test_offboard_records_reach_the_page(self):
        self.offboard(self.rec(user="a"), self.rec(user="b"))
        got = self.item("offboard_pending")
        self.assertEqual(got["count"], 2)
        self.assertIn("李四", got["title"])

    def test_unlinked_accounts_reach_the_page_with_the_filtered_count(self):
        got = self.item("unlinked_account")
        # 快照里三个号：lisi 对上了人，guikai 没有，tempak-abc 是程序发的
        self.assertEqual(got["count"], 1)
        self.assertIn("另有 1 个", got["title"])

    def test_failed_tickets_reach_the_page(self):
        self.tickets(
            {
                "id": "REQ-1",
                "kind": "permission",
                "status": tickets_mod.FAILED,
                "created_at": iso(self.clock() - 2 * DAY),
            }
        )
        self.assertEqual(self.item("request_failed")["count"], 1)

    def test_done_ticket_without_iam_written_is_flagged(self):
        """号建出来了、登录名没写进公司 IAM —— 状态是 DONE，任何一页都不显眼。"""
        self.tickets(
            {
                "id": "REQ-2",
                "kind": "account",
                "status": tickets_mod.DONE,
                "user_created": True,
                "iam_written": False,
                "created_at": iso(self.clock()),
            }
        )
        self.assertEqual(self.item("request_no_iam")["count"], 1)

    def test_iam_written_ticket_is_not_flagged(self):
        self.tickets(
            {
                "id": "REQ-3",
                "kind": "account",
                "status": tickets_mod.DONE,
                "user_created": True,
                "iam_written": True,
                "created_at": iso(self.clock()),
            }
        )
        self.assertNotIn("request_no_iam", self.kinds())

    def test_expiring_only_counts_done_tickets(self):
        """还没开通的单子不该报「快到期」——它现在根本没有权限可断。"""
        soon = iso(self.clock() + 2 * DAY)
        self.tickets(
            {
                "id": "REQ-4",
                "kind": "permission",
                "status": tickets_mod.PENDING,
                "expires_at": soon,
                "expires_at_ts": self.clock() + 2 * DAY,
                "created_at": iso(self.clock()),
            }
        )
        self.assertNotIn("cred_expiring", self.kinds())

    def test_reconcile_drift_reaches_the_page(self):
        self.write(
            "iam-reconcile.json",
            {
                "checked_at": iso(self.clock() - 3600),
                "apps": [
                    {
                        "app": "aliyun_username",
                        "drift": [{"kind": "inactive", "union_id": "on_X", "name": "赵六"}],
                    }
                ],
            },
        )
        got = self.item("iam_inactive")
        self.assertIn("赵六", got["what"])
        self.assertEqual(self.view()["freshness"]["IAM 对账"]["stale"], False)

    def test_snapshot_freshness_is_always_published(self):
        """所有结论都建立在快照上：它多旧必须印在页面上。

        不印的话，「没发现无主账号」和「这次根本没采到」在页面上长得一模一样。
        """
        fresh = self.view()["freshness"]["权限快照"]
        self.assertFalse(fresh["stale"])
        self.assertTrue(fresh["at"])

    def test_old_snapshot_is_marked_stale(self):
        self.write(
            "inventory.json",
            {"captured_at": iso(self.clock() - 5 * DAY), "accounts": []},
        )
        self.assertTrue(self.view()["freshness"]["权限快照"]["stale"])

    def test_missing_snapshot_is_stale_and_says_why(self):
        (self.id_dir / "inventory.json").unlink()
        fresh = self.view()["freshness"]["权限快照"]
        self.assertTrue(fresh["stale"])
        self.assertIn("20 分钟", fresh["note"])


class AcFiveTests(Base):
    """AC-5：人员页的「对不上人的号」和体检页的「无主账号」必须是同一个数字。

    两页各算一份的后果不是「数字不同」那么简单 —— 是**管理员不知道该信哪一个**，
    而页面上没有任何地方解释差在哪。所以这里直接把两个函数摆在一起比。
    """

    def both(self, roster, services=()):
        from delivery import hygiene, views
        from delivery.inventory import parse
        from delivery.people import parse as pparse

        snap = parse(json.loads((self.id_dir / "inventory.json").read_text(encoding="utf-8")))
        index = pparse(roster)
        labels = views.Labels({"aliyun": "阿里云"}, {})
        rows, filtered = views.unlinked_rows(snap, index, labels, services=services)
        report = hygiene.build(snap, index.people, services=services)
        return rows, filtered, report.orphan

    def roster(self, *people):
        return {"schema": "wuji-people@1", "people": list(people)}

    def person(self, name, uid, accounts=(), pending=()):
        return {
            "name": name,
            "email": f"{uid}@wuji.tech",
            "union_id": uid,
            "accounts": list(accounts),
            "pending": list(pending),
        }

    def ali(self, name):
        return {"platform": "aliyun", "account": ACC, "name": name}

    def test_same_number_on_both_pages(self):
        rows, _filtered, orphan = self.both(
            self.roster(self.person("李四", "on_L", [self.ali("lisi")]))
        )
        self.assertEqual(len(rows), len(orphan), "两页的无主账号数必须一致")
        self.assertEqual({r["name"] for r in rows}, {f.subject for f in orphan})

    def test_program_accounts_are_filtered_on_both_sides(self):
        """`tempak-` 开头的是程序自己发的临时凭证号，两边都不该报成无主。"""
        rows, filtered, orphan = self.both(
            self.roster(self.person("李四", "on_L", [self.ali("lisi")]))
        )
        self.assertNotIn("tempak-abc", {r["name"] for r in rows})
        self.assertNotIn("tempak-abc", {f.subject for f in orphan})
        self.assertEqual(filtered, 1, "滤掉几个要数出来，否则两页数字对不上时没人解释得了")

    def test_registered_service_accounts_are_filtered_on_both_sides(self):
        rows, filtered, orphan = self.both(
            self.roster(self.person("李四", "on_L", [self.ali("lisi")])), services=["guikai"]
        )
        self.assertEqual(len(rows), len(orphan))
        self.assertEqual(len(rows), 0)
        self.assertEqual(filtered, 2)

    def test_service_match_is_case_insensitive_on_both_sides(self):
        rows, _f, orphan = self.both(
            self.roster(self.person("李四", "on_L", [self.ali("lisi")])), services=["GuiKai"]
        )
        self.assertEqual(len(rows), len(orphan))
        self.assertEqual(len(rows), 0)

    def test_no_snapshot_means_zero_on_both_sides(self):
        from delivery import hygiene, views
        from delivery.people import parse as pparse

        index = pparse(self.roster())
        rows, filtered = views.unlinked_rows(None, index, views.Labels({}, {}), services=())
        report = hygiene.build(None, index.people, services=())
        self.assertEqual((rows, filtered), ([], 0))
        self.assertEqual(report.orphan, [])

    def test_a_pending_link_is_counted_the_same_way_on_both_pages(self):
        """**回归（曾是缺陷）**：待确认的归属会让 AC-5 的两个数字重新分叉。

        `src/delivery/views.py:210` 的 `_unlinked_rows` 把 `p.accounts` 和 `p.pending`
        合起来算「已对上」，而 `src/delivery/hygiene.py:597` 建 `by_account` 时**只用
        `person.accounts`**。于是一个只有待确认归属的号：
          · 人员页「对不上人的号」：0
          · 体检页「认不出属主的云账号」：1
        `views.unlinked_rows` 这次只统一了「程序号」那一维，pending 这一维没动 ——
        而它恰恰是最常见的一种：名册刚推断完、管理员还没点确认，那段时间每个新号都在这个状态。

        两边必须相等。
        修法二选一（要在两处同时定口径，不能各改各的）：
          · 体检的 `by_account` 也纳入 `person.pending`；或
          · `views._unlinked_rows` 的 `linked` 去掉 `p.pending`，改由 `mapping_review`
            那一类单独承载「待确认」。
        """
        rows, _filtered, orphan = self.both(
            self.roster(
                self.person("李四", "on_L", [self.ali("lisi")]),
                self.person("鬼", "on_G", pending=[self.ali("guikai")]),
            )
        )
        self.assertEqual(len(rows), len(orphan))


class ExecutorTests(Base):
    def test_snapshot_read_error_is_a_delivery_error_not_a_crash(self):
        """`backend.snapshot()` 外面只包了 `DeliveryError`。

        这条用例是那层 except 的守卫：哪天快照读取改成抛别的异常类型，
        待办页会直接 500 —— 而 500 的表现是「整个待办页打不开」。
        """
        (self.id_dir / "inventory.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(DeliveryError):
            self.backend.snapshot()
        self.assertTrue(any("快照" in e for e in self.view()["errors"]))


if __name__ == "__main__":
    unittest.main()
