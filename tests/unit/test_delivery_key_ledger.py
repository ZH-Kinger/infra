"""AK 台账：持有人自己看得见「这把密钥建了多久、上次什么时候用过」。

这批数据云上一直采得到，但以前只有命令行看得见。结果是：**持有人不知道手上那把
密钥有多老**，「该轮换了」的提醒推过去也没有地方可点、可核对，第二次就没人看了。

三件事必须钉住，改坏了都不会报错：

1. **三态不能混**（同 `hygiene.py` 开头那条）：有几把 / 确实一把都没有 /
   **没采到**。混成一个「0 把」的话，采集身份哪天掉了 `ram:ListAccessKeys`，
   全员都会看到「你没有访问密钥」——而那恰恰是最该报警的状态。
2. **只看得到自己的**。这一页上出现别人的子账号名或密钥，就是越权。
3. **判据只有一处定义**。台账说「该换了」而体检清单里没有它，两边就都不可信了。

离线，数据虚构。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from delivery import hygiene, inventory, views
from delivery.people import AccountRef, Person

from .test_delivery_asset_owners import ACC, ME, _Live, _PanelBase

DAY = 86400
NOW = 1_800_000_000.0  # 固定时钟，别让用例随日期漂
LABELS = views.Labels({"aliyun": "阿里云"}, {f"aliyun/{ACC}": "阿里云主账号"})

#: 接口用例走的是**真实时钟**（服务端自己取 now），所以这里用一个绝对的远古日期，
#: 而不是相对 NOW 算出来的时间 —— 否则用例的结论会随着今天是几号而变
ANCIENT = {
    "id": "MINE0000",
    "status": "Active",
    "created": "2020-01-01T00:00:00Z",
    "last_used": "N/A",
}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key(kid, *, status="Active", age_days=1, used_days_ago=1):
    return {
        "id": kid,
        "status": status,
        "created": iso(NOW - age_days * DAY),
        "last_used": "N/A" if used_days_ago is None else iso(NOW - used_days_ago * DAY),
    }


def snapshot(users):
    return inventory.parse(
        {
            "captured_at": "2026-09-18T00:00:00+08:00",
            "accounts": [{"platform": "aliyun", "account": ACC, "groups": [], "users": users}],
        }
    )


def person(*names):
    return Person(
        name="李四",
        email=ME,
        union_id="on_1",
        accounts=tuple(AccountRef("aliyun", ACC, n) for n in names),
    )


class StaticAssetTests(unittest.TestCase):
    def test_every_web_module_is_registered_for_serving(self):
        """`app.js` 里是**顶层 import**，漏登记一个 .js 不是「某个 tab 打不开」，
        是**整站白屏** —— 浏览器加载模块图的时候就 404 了，什么都画不出来。

        按文件逐条写用例的话，下一个新页面还是会漏；这里扫目录，一劳永逸。
        （`.html` 不一起扫：`pickup.html` 走 `_VIEW_PREFIX` 特例，不在这两张表里。）
        """
        from delivery.server import _PICKUP_STATIC, _STATIC, WEB_DIR

        served = {name for name, _ in (*_STATIC.values(), *_PICKUP_STATIC.values())}
        missing = sorted(p.name for p in WEB_DIR.glob("*.js") if p.name not in served)
        self.assertEqual(missing, [], f"{missing} 没登记进 _STATIC，面板会整站白屏")


class LedgerTests(unittest.TestCase):
    def view(self, users, *, names=("lisi",), **kw):
        return views.my_keys(person(*names), snapshot(users), LABELS, now=NOW, **kw)

    def test_age_and_idle_are_spelled_out_in_days(self):
        """台账的全部意义就是这两个数字。少了它们这一页只是「你有一把密钥」。"""
        out = self.view(
            [{"name": "lisi", "keys": [key("LTAI0001", age_days=400, used_days_ago=3)]}]
        )
        (row,) = out["keys"]
        self.assertEqual(row["age_days"], 400)
        self.assertEqual(row["idle_days"], 3)
        self.assertFalse(row["never_used"])
        self.assertEqual(row["id"], "LTAI0001")

    def test_a_key_never_used_says_so_instead_of_showing_zero(self):
        """阿里云对从没用过的返回 `N/A`，会解析成 0。渲染成「0 天前用过」正好说反了。"""
        out = self.view(
            [{"name": "lisi", "keys": [key("LTAI0002", age_days=300, used_days_ago=None)]}]
        )
        (row,) = out["keys"]
        self.assertTrue(row["never_used"])
        self.assertIsNone(row["idle_days"])

    def test_flags_match_the_hygiene_verdict_exactly(self):
        """台账和体检清单必须是同一套判据 —— 各算各的，两边就都不可信了。"""
        users = [
            {
                "name": "lisi",
                "keys": [
                    key("OLDUSED0", age_days=400, used_days_ago=1),  # 老、天天用 → 该换
                    key("NEVER000", age_days=300, used_days_ago=None),  # 老、没人用 → 两样都占
                    key("FRESH000", age_days=3, used_days_ago=1),  # 新、在用 → 不打扰
                ],
            }
        ]
        flags = {r["id"]: r["flags"] for r in self.view(users)["keys"]}
        self.assertEqual(flags["OLDUSED0"], ["rotate"])
        self.assertEqual(flags["NEVER000"], ["unused", "rotate"])
        self.assertEqual(flags["FRESH000"], [])

        report = hygiene.build(snapshot(users), [], services=[], now=NOW)
        self.assertEqual({f.subject for f in report.rotate}, {"lisi"})
        self.assertEqual(
            sorted(f.why.split()[1] for f in report.rotate), sorted(["NEVER000…", "OLDUSED0…"])
        )

    def test_a_key_whose_cloud_cannot_answer_is_never_flagged_unused(self):
        """**体检里「没用过」从 23 条跳到 67 条那次，多出来的 44 条就是这个形状。**

        火山的 `ListAccessKeys` 不返回最近使用时间，`last_used` 只能留空 ——
        而空串一度被当成「从来没用过」。台账的徽章和体检清单是同一套判据，
        所以两边一起锁：`unused` 一条都不能有，`rotate` 照常（年龄自己算得出来）。
        判据看的是 `last_used_known` 这个标记、不是平台名，所以用例不必换平台。
        """
        users = [
            {
                "name": "lisi",
                "keys": [
                    {
                        "id": "AKLT0001",
                        "status": "Active",
                        "created": iso(NOW - 400 * DAY),
                        "last_used": "",
                        "last_used_known": False,
                    }
                ],
            }
        ]
        (row,) = self.view(users)["keys"]
        self.assertEqual(row["flags"], ["rotate"])
        self.assertIs(row["last_used_known"], False)
        self.assertFalse(row["never_used"], "「这朵云查不到」不能记成「从来没用过」")

        report = hygiene.build(snapshot(users), [], services=[], now=NOW)
        self.assertEqual(report.unused, [], "拿不到最近使用时间，就不该报「没人用」")
        self.assertEqual({f.subject for f in report.rotate}, {"lisi"})

    def test_a_disabled_key_is_listed_but_not_nagged_about(self):
        """停用 ≠ 不存在：台账要让人看见自己有什么。但别催他轮换一把已经停用的。"""
        out = self.view(
            [
                {
                    "name": "lisi",
                    "keys": [key("DEAD0000", status="Inactive", age_days=900, used_days_ago=None)],
                }
            ]
        )
        (row,) = out["keys"]
        self.assertFalse(row["active"])
        self.assertEqual(row["flags"], [])
        self.assertEqual(row["age_days"], 900)  # 年龄照样显示

    def test_the_most_urgent_key_lands_on_the_first_row(self):
        out = self.view(
            [
                {
                    "name": "lisi",
                    "keys": [
                        key("FRESH000", age_days=3),
                        key("DEAD0000", status="Inactive", age_days=900),
                        key("OLDEST00", age_days=500),
                    ],
                }
            ]
        )
        # 启用的在前（按年龄降序），停用的垫底
        self.assertEqual([r["id"] for r in out["keys"]], ["OLDEST00", "FRESH000", "DEAD0000"])

    # ── 三态 ──────────────────────────────────────────────────────────────
    def test_no_keys_at_all_is_not_the_same_as_not_collected(self):
        got = self.view([{"name": "lisi", "keys": []}])
        self.assertEqual(got["keys"], [])
        self.assertEqual(got["uncollected"], [])
        self.assertEqual(got["accounts"], 1)  # 有账号、确实没密钥 —— 页面上该说一句

    def test_not_collected_shows_up_as_uncollected_not_as_zero(self):
        """采集身份掉了 ram:ListAccessKeys 时，绝不能让全员看到「你没有访问密钥」。"""
        got = self.view([{"name": "lisi"}])  # 快照里没有 keys 这一项 = 没采到
        self.assertEqual(got["keys"], [])
        self.assertEqual([u["user"] for u in got["uncollected"]], ["lisi"])

    def test_a_person_with_no_cloud_account_gets_a_silent_empty_view(self):
        """没有云账号的人不该看到一张空的密钥卡。`accounts` 是前端区分两者的依据。"""
        got = views.my_keys(person(), snapshot([]), LABELS, now=NOW)
        self.assertEqual((got["keys"], got["uncollected"], got["accounts"]), ([], [], 0))

    def test_no_snapshot_at_all_reports_nothing_rather_than_everything(self):
        got = views.my_keys(person("lisi"), None, LABELS, now=NOW)
        self.assertEqual((got["keys"], got["uncollected"], got["accounts"]), ([], [], 0))

    # ── 数据边界 ──────────────────────────────────────────────────────────
    def test_other_peoples_keys_never_appear(self):
        """这一页只能有登录者自己的东西 —— 越权在这里是「看见别人的密钥 ID」。"""
        out = self.view(
            [
                {"name": "lisi", "keys": [key("MINE0000", age_days=400)]},
                {"name": "boss", "keys": [key("NOTMINE0", age_days=400)]},
            ]
        )
        self.assertEqual([r["id"] for r in out["keys"]], ["MINE0000"])
        self.assertNotIn("NOTMINE0", json.dumps(out))

    def test_the_ledger_passes_the_id_through_and_never_lengthens_it(self):
        """截断成前 8 位是**采集那一步**做的（见 test_delivery_access_keys 里的
        CollectTests）。台账这一层只负责原样带过去 —— 不能在这里反过来拼长、
        也不能去别处补全：完整 AKId 不该出现在任何一个会被传阅的地方。"""
        out = self.view([{"name": "lisi", "keys": [{"id": "LTAI0123"}]}])
        self.assertEqual(out["keys"][0]["id"], "LTAI0123")
        self.assertEqual(json.dumps(out).count("LTAI0123"), 1)

    def test_an_overlong_id_is_clamped_not_passed_through(self):
        """页面上写着「只显示前 8 位」，这个承诺该由渲染方自己保证。

        截断本来在采集那一步做（`inventory_collect`），但只要有人手工拼过一份快照、
        或者将来加一朵云的采集忘了截，那句话就变成假的、而且没人会发现。
        """
        out = self.view([{"name": "lisi", "keys": [{"id": "LTAI5tFULLKEYIDLEAKED123"}]}])
        self.assertEqual(out["keys"][0]["id"], "LTAI5tFU")
        self.assertNotIn("LTAI5tFULLKEYIDLEAKED123", json.dumps(out))

    def test_no_secret_anywhere(self):
        out = self.view([{"name": "lisi", "keys": [key("LTAI0001")]}])
        for banned in ("secret", "Secret", "sk"):
            self.assertNotIn(banned, json.dumps(out), banned)

    def test_thresholds_are_reported_so_the_page_can_say_them_out_loud(self):
        out = self.view([{"name": "lisi", "keys": []}], stale_days=30, unused_days=7)
        self.assertEqual((out["stale_days"], out["unused_days"]), (30, 7))
        self.assertEqual(out["captured_at"], "2026-09-18T00:00:00+08:00")


class StatusCacheTests(unittest.TestCase):
    """飞书在职状态的缓存：查一次是几十个串行请求，管理员连点几下不该每次都打一遍。"""

    def setUp(self):
        from delivery import server as server_mod
        from delivery.identity import directory

        self.server_mod = server_mod
        self.calls = []

        def fake_status_of(uids, app_id, app_secret, **kw):
            self.calls.append(tuple(uids))
            return {u: {"is_resigned": False} for u in uids}

        self._real = directory.status_of
        directory.status_of = fake_status_of
        self.addCleanup(setattr, directory, "status_of", self._real)
        # 模块级缓存，用例之间会互相污染 —— 每次进来清干净
        server_mod._status_cache.update(key=None, at=0.0, value=None)
        self.addCleanup(server_mod._status_cache.update, key=None, at=0.0, value=None)

    def roster(self, *people):
        return [Person(name=n, email=f"{n}@x", union_id=u) for n, u in people]

    def test_the_second_call_within_the_ttl_does_not_hit_feishu_again(self):
        who = self.roster(("a", "on_a"), ("b", "on_b"))
        first = self.server_mod._employment_statuses(who, "cli_x", "s")
        second = self.server_mod._employment_statuses(who, "cli_x", "s")
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 1, "同一批人短时间内查了两遍飞书")

    def test_a_different_app_is_a_different_question(self):
        """换了飞书应用就是另一套可用范围，拿旧结果等于拿别人的答案。"""
        who = self.roster(("a", "on_a"))
        self.server_mod._employment_statuses(who, "cli_x", "s")
        self.server_mod._employment_statuses(who, "cli_OTHER", "s")
        self.assertEqual(len(self.calls), 2)

    def test_adding_someone_without_a_union_id_keeps_the_cache_but_changes_the_count(self):
        """没 union_id 的人查不了，但**得被数出来** —— 页面靠这个数说「另有 N 人没查」。
        这个数不能进缓存，否则名册加了人、页面还在报旧数字。"""
        who = self.roster(("a", "on_a"))
        _, asked, missing = self.server_mod._employment_statuses(who, "cli_x", "s")
        self.assertEqual((asked, missing), (1, 0))

        who.append(Person(name="新人", email="n@x", union_id=""))
        _, asked2, missing2 = self.server_mod._employment_statuses(who, "cli_x", "s")
        self.assertEqual((asked2, missing2), (1, 1))
        self.assertEqual(len(self.calls), 1, "uid 集合没变，不该再打一次飞书")


class ReportViewTests(unittest.TestCase):
    """体检清单的 JSON 形态：网页和命令行读的是同一份结论。"""

    def test_the_threshold_actually_used_shows_up_in_the_title(self):
        """`--stale-days 30` 跑出来的清单，标题却印着默认的 180 天，
        看的人会以为这批 AK 老得多、按错的紧迫度处理。"""
        users = [{"name": "svc", "keys": [key("OLD00000", age_days=60, used_days_ago=30)]}]
        report = hygiene.build(
            snapshot(users), [], services=["svc"], now=NOW, stale_days=30, unused_days=7
        )
        titles = [s["title"] for s in hygiene.view(report)["sections"]]
        self.assertIn("AK 建出来超过 30 天，该换了", titles)
        self.assertIn("AK 超过 7 天没用过", titles)
        # 60 天的 AK 按默认 180 天不算旧，按 30 天算 —— 阈值确实生效了，不只是标题好看
        self.assertEqual(len(report.rotate), 1)
        self.assertIn("超过 30 天", report.render())

    def test_render_and_view_never_drift(self):
        """两套输出共用 `_SECTIONS`。各写各的，迟早会出现
        「网页上说该停用、命令行说该轮换」。"""
        users = [{"name": "svc", "keys": [key("OLD00000", age_days=400, used_days_ago=None)]}]
        report = hygiene.build(snapshot(users), [], services=["svc"], now=NOW)
        text = report.render()
        for section in hygiene.view(report)["sections"]:
            if section["count"]:
                self.assertIn(section["title"], text)
                self.assertIn(section["note"], text)

    def test_checking_nobody_is_not_the_same_as_finding_nobody(self):
        """名册里一个 union_id 都没有时 `statuses` 是 `{}`（非 None）—— 不说的话，
        `render()` 打出「没有发现需要处理的」、`_hygiene` 退出码 0，
        定时任务据此判定「一切正常」，而离职这一类**一个人都没查**。"""
        report = hygiene.build(snapshot([]), [], statuses={}, services=[], now=NOW)
        self.assertTrue(any("一个人都没查到" in s for s in report.skipped), report.skipped)
        self.assertTrue(hygiene.view(report)["summary"]["incomplete"])

    def test_skipped_survives_the_json_trip(self):
        """少算了一整类却不说，一份残缺清单看起来和一份干净清单一模一样。"""
        out = hygiene.view(hygiene.build(None, []))
        self.assertTrue(out["skipped"])
        self.assertTrue(out["summary"]["incomplete"])


class KeyApiTests(_PanelBase):
    """`GET /api/assets` 带上自己的密钥台账；管理员视图不受影响。"""

    def make_backend(self, **kw):
        path = self.dir / "inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "wuji-inventory@1",
                    "captured_at": "2026-09-18T00:00:00+08:00",
                    "accounts": [
                        {
                            "platform": "aliyun",
                            "account": ACC,
                            "groups": [],
                            "users": [
                                {"name": "lisi", "keys": [ANCIENT]},
                                {"name": "boss", "keys": [dict(ANCIENT, id="BOSS0000")]},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        kw.setdefault("inventory_path", str(path))
        return super().make_backend(**kw)

    def test_the_employee_sees_their_own_key_age(self):
        with _Live(self.backend) as live:
            status, view = live.request("GET", "/api/assets", cookie=self.login(live, "on_1", ME))
            self.assertEqual(status, 200, view)
            keys = view["keys"]["keys"]
            self.assertEqual([k["id"] for k in keys], ["MINE0000"])
            # 接口走真实时钟，所以钉一个「怎么跑都成立」的下界，别让用例随日期漂
            self.assertGreater(keys[0]["age_days"], 1000)

    def test_the_employee_never_sees_somebody_elses(self):
        with _Live(self.backend) as live:
            _, view = live.request("GET", "/api/assets", cookie=self.login(live, "on_1", ME))
            self.assertNotIn("BOSS0000", json.dumps(view))

    def test_the_admin_asset_view_is_unchanged(self):
        """管理员那一页讲的是「这个账号有什么资源」，不该被塞进个人密钥台账。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            _, view = live.request("GET", "/api/admin/assets", cookie=admin)
            self.assertNotIn("keys", view)


class HygieneApiTests(_PanelBase):
    """`GET /api/admin/hygiene`：只读、管理员专属、默认不打飞书。"""

    def make_backend(self, **kw):
        path = self.dir / "inventory.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "wuji-inventory@1",
                    "captured_at": "2026-09-18T00:00:00+08:00",
                    "accounts": [
                        {
                            "platform": "aliyun",
                            "account": ACC,
                            "groups": [],
                            "users": [
                                {"name": "lisi", "keys": [ANCIENT]},
                                {"name": "nobody", "keys": []},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        kw.setdefault("inventory_path", str(path))
        return super().make_backend(**kw)

    def kinds(self, body):
        return {s["kind"]: s["count"] for s in body["sections"]}

    def test_admin_gets_the_same_list_the_cli_prints(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, body = live.request("GET", "/api/admin/hygiene", cookie=admin)
            self.assertEqual(status, 200, body)
            counts = self.kinds(body)
            self.assertEqual(counts["rotate"], 1)  # lisi 那把 400 天的
            self.assertEqual(counts["orphan"], 1)  # nobody 名册里认不出
            self.assertEqual(body["captured_at"], "2026-09-18T00:00:00+08:00")

    def test_employment_status_is_not_checked_unless_asked(self):
        """默认不查飞书：几十个串行请求挂在页面加载上会让后台无故卡十几秒。
        没查就**必须**在 skipped 里说出来，否则「没发现离职」会被当成「没人离职」。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            _, body = live.request("GET", "/api/admin/hygiene", cookie=admin)
            self.assertFalse(body["status_checked"])
            self.assertTrue(any("在职" in s for s in body["skipped"]), body["skipped"])

    def test_employees_are_refused(self):
        """体检清单里有全员的云账号名和归属，不是员工该看的东西。"""
        with _Live(self.backend) as live:
            status, _ = live.request(
                "GET", "/api/admin/hygiene", cookie=self.login(live, "on_1", ME)
            )
            self.assertIn(status, (401, 403))

    def test_saying_it_checked_must_say_how_many(self):
        """「已查过在职状态」不能盖住「其实一多半人根本没查」。

        只有绑过 union_id 的人查得到 —— 没登录过面板的人名册里就没有。
        「查了 12 人没发现离职」和「查了 60 人没发现离职」是两回事，页面必须分得开。
        """
        from delivery import server as server_mod

        calls = []

        def fake(roster, app_id, app_secret):
            calls.append(app_id)
            uids = [p.union_id for p in roster if p.union_id]
            missing = sum(1 for p in roster if not p.union_id)
            return {u: {"is_resigned": False} for u in uids}, len(uids), missing

        original = server_mod._employment_statuses
        server_mod._employment_statuses = fake
        self.addCleanup(setattr, server_mod, "_employment_statuses", original)

        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            _, body = live.request("GET", "/api/admin/hygiene?status=1", cookie=admin)
            self.assertTrue(body["status_checked"])
            self.assertEqual(body["status_asked"], 5)  # 名册里 5 个人都有 union_id
            self.assertEqual(body["status_missing_uid"], 0)
            self.assertEqual(calls, ["cli_demo"])

    def test_a_flaky_feishu_never_takes_the_whole_page_down(self):
        """飞书读超时抛的是 TimeoutError、响应不是 JSON 抛的是 ValueError ——
        都不是 DeliveryError。只 catch DeliveryError 的话它们会穿到兜底、把整页
        渲染成「面板数据暂不可用」，而真实原因只是飞书慢了一下。"""
        from delivery import server as server_mod

        def boom(roster, app_id, app_secret):
            raise TimeoutError("read timed out")

        original = server_mod._employment_statuses
        server_mod._employment_statuses = boom
        self.addCleanup(setattr, server_mod, "_employment_statuses", original)

        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, body = live.request("GET", "/api/admin/hygiene?status=1", cookie=admin)
            self.assertEqual(status, 200, body)  # 页面照出，别整页 500
            self.assertFalse(body["status_checked"])
            self.assertIn("timed out", body["status_error"])
            self.assertEqual(self.kinds(body)["rotate"], 1)  # 其余几类照算

    def test_it_is_read_only(self):
        """这一页不能有任何处置动作 —— 自动误伤一次，所有人就学会忽略这类通知了。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, _ = live.request(
                "POST", "/api/admin/hygiene", cookie=admin, payload={"op": "disable"}
            )
            self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
