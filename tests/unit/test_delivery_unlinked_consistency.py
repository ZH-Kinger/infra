"""「对不上人的云账号」这个数，三页要一模一样（总览 / 人员页 / 待办页）。

为什么单独锁
────────────
这个数字是管理员判断「还有多少活」的锚。三页各算一份的时候，实际发生的是：
总览写 3、人员页列 5、待办页角标写 4 —— **没有任何一页解释差在哪**，
于是人只能全都不信，这一栏就废了。

判据只有一份：`views.unlinked_rows(..., services=)`，它把程序发的号
（`staff-` / `tempak-` / `temp-ak-` / `panel-`）和登记过的服务号滤掉，
并且**把滤掉几条也说出来** —— 滤掉而不说，等于换了一种方式让数字对不上。

这里刻意走**真的 HTTP 接口**，不是直接调函数：漏接一个 `services=` 参数
在函数级看不出来（默认值会让它静默退回不过滤），而那正是这一类 bug 的出现方式。

离线：临时目录当仓库根，数据虚构，网络拔掉。
"""

from __future__ import annotations

import unittest
import urllib.request
from unittest import mock

from delivery import grants
from delivery import server as server_mod
from delivery.views import admin_overview, admin_people

from .test_delivery_todo_api import ACC, Base, _Live, iso

#: 快照里放的几个号：一个对得上人、一个真无主、每种程序前缀各一个
PROGRAM = tuple(f"{p}alice-9a1b2c" for p in grants.ISSUED_PREFIXES)


class ConsistencyBase(Base):
    def setUp(self):
        super().setUp()
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
                            {"name": "mes-sync", "display_name": "服务号", "policies": []},
                            *({"name": n, "display_name": "发的", "policies": []} for n in PROGRAM),
                        ],
                        "groups": [],
                    }
                ],
            },
        )
        self.write("services.json", {"names": ["mes-sync"]})
        self.backend = self.make_backend(services_path="identity/services.json")

    def overview(self) -> dict:
        return admin_overview(
            self.backend.snapshot(),
            self.backend.people(),
            self.backend.labels(),
            services=self.backend.service_names(),
        )

    def people_page(self) -> dict:
        return admin_people(
            self.backend.snapshot(),
            self.backend.people(),
            self.backend.labels(),
            services=self.backend.service_names(),
        )

    def todo_count(self) -> int:
        for group in server_mod._todo_view(self.backend)["groups"]:
            for item in group["items"]:
                if item["kind"] == "unlinked_account":
                    return item["count"]
        return 0


class ThreePagesAgreeTests(ConsistencyBase):
    def test_all_three_report_the_same_number(self):
        """一个真无主（guikai）。`lisi` 对上了人，服务号和程序发的号被滤掉。"""
        overview = self.overview()
        people = self.people_page()
        self.assertEqual(overview["totals"]["unlinked_accounts"], 1)
        self.assertEqual(len(people["unlinked_accounts"]), 1)
        self.assertEqual(self.todo_count(), 1)

    def test_all_three_report_the_same_filtered_count(self):
        """滤掉几条也要一致，而且要**说出来** —— 不说的话，
        「快照里 7 个号，这里只列 1 个」看起来就像丢数据。"""
        filtered = len(PROGRAM) + 1  # 程序发的 + 一个登记过的服务号
        self.assertEqual(self.overview()["totals"]["unlinked_filtered"], filtered)
        self.assertEqual(self.people_page()["unlinked_filtered"], filtered)

    def test_the_only_listed_account_is_the_real_orphan(self):
        """数字对上了还不够：列出来的得是同一个号。
        两页各滤各的、恰好滤掉一样多，数字也会对上。"""
        rows = self.people_page()["unlinked_accounts"]
        self.assertEqual([r["name"] for r in rows], ["guikai"])

    def test_no_program_account_leaks_into_any_page(self):
        """程序发的号进了这一栏 = 管理员每次都得先把它们挑出来，第二次就没人看了。"""
        listed = {r["name"] for r in self.people_page()["unlinked_accounts"]}
        for name in PROGRAM:
            self.assertNotIn(name, listed, name)

    def test_the_numbers_track_a_new_orphan_together(self):
        """加一个真无主的号，三处要一起变 —— 只对一次快照的数字是巧合，
        跟着动才说明是同一份计算。"""
        before = self.overview()["totals"]["unlinked_accounts"]
        data = self.backend.snapshot()  # 先读一次，确认基线
        self.assertTrue(data.users)
        self.write(
            "inventory.json",
            {
                "captured_at": iso(self.clock() - 600),
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": ACC,
                        "users": [
                            {"name": "lisi", "policies": []},
                            {"name": "guikai", "policies": []},
                            {"name": "xinren", "policies": []},
                            {"name": PROGRAM[0], "policies": []},
                        ],
                        "groups": [],
                    }
                ],
            },
        )
        self.backend = self.make_backend(services_path="identity/services.json")
        self.assertEqual(self.overview()["totals"]["unlinked_accounts"], before + 1)
        self.assertEqual(len(self.people_page()["unlinked_accounts"]), before + 1)
        self.assertEqual(self.todo_count(), before + 1)


class ApiTests(ConsistencyBase):
    """走真的 HTTP：漏传 `services=` 在函数级看不出来，接口级才看得出来。"""

    def get(self, path):
        with _Live(self.backend) as live:
            return live.get(path, self.admin_session(live))

    def test_the_overview_endpoint_filters_program_accounts(self):
        status, data = self.get("/api/admin/overview")
        self.assertEqual(status, 200)
        self.assertEqual(data["totals"]["unlinked_accounts"], 1)
        self.assertEqual(data["totals"]["unlinked_filtered"], len(PROGRAM) + 1)

    def test_the_people_endpoint_filters_program_accounts(self):
        status, data = self.get("/api/admin/people")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in data["unlinked_accounts"]], ["guikai"])
        self.assertEqual(data["unlinked_filtered"], len(PROGRAM) + 1)

    def test_the_two_endpoints_agree(self):
        _, overview = self.get("/api/admin/overview")
        _, people = self.get("/api/admin/people")
        self.assertEqual(overview["totals"]["unlinked_accounts"], len(people["unlinked_accounts"]))
        self.assertEqual(overview["totals"]["unlinked_filtered"], people["unlinked_filtered"])

    def test_none_of_this_needs_the_network(self):
        """这三页都是管理员的落地页。往里塞一次云调用，打开面板就要等 8 秒，
        而云那边一抖就白屏。"""

        def boom(*a, **k):
            raise AssertionError("这一页发起了网络请求")

        with mock.patch.object(urllib.request, "urlopen", boom):
            self.assertEqual(self.overview()["totals"]["unlinked_accounts"], 1)
            self.assertEqual(len(self.people_page()["unlinked_accounts"]), 1)
            self.assertEqual(self.todo_count(), 1)


if __name__ == "__main__":
    unittest.main()
