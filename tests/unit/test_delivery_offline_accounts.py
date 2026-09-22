"""没有采集接口的平台（九章）：账号靠人工登记，采集时合进快照和名册。

不登记的话，名册、「我的账号」、离职检查都看不到九章 —— 一个离职的人在九章上的号
永远没人提醒去停。
"""

import unittest

from delivery import offline_accounts as off
from delivery.identity.ssomap import propose

ROW = {
    "platform": "jiuzhang",
    "account": "wuji",
    "source": "九章控制台导出（测试）",
    "as_of": "2026-09-22",
    "users": [
        {
            "name": "huangzenan",
            "login": "wuji-huangzenan",
            "display_name": "黄 泽楠",
            "email": "Huang.Zenan@wuji.tech",
            "status": "正常",
            "created": "2026-09-21 15:01:19",
        },
        {
            "name": "gone",
            "login": "wuji-gone",
            "display_name": "离职的",
            "email": "gone@wuji.tech",
            "status": "禁用",
        },
    ],
}


class ParseTests(unittest.TestCase):
    def test_a_good_table_loads_and_names_are_tidied(self):
        rows = off.parse({"accounts": [ROW]})
        u = rows[0]["users"][0]
        self.assertEqual(u["display_name"], "黄泽楠", "控制台导出的姓名中间带空格")
        self.assertEqual(u["email"], "huang.zenan@wuji.tech")

    def test_phone_numbers_are_refused(self):
        """最常见的就是把手机号一起粘进来。名册按邮箱关联，用不上它；多存一份就多一份泄漏面。"""
        bad = {"accounts": [dict(ROW, users=[dict(ROW["users"][0], phone="17800000000")])]}
        with self.assertRaises(off.OfflineError) as caught:
            off.parse(bad)
        self.assertIn("手机号", str(caught.exception))

    def test_collected_platforms_cannot_be_hand_registered(self):
        """阿里和火山有采集接口。两个来源同时说一个账号是什么样，以谁为准就说不清了。"""
        with self.assertRaises(off.OfflineError):
            off.parse({"accounts": [dict(ROW, platform="aliyun")]})

    def test_the_same_account_twice_is_refused(self):
        with self.assertRaises(off.OfflineError):
            off.parse({"accounts": [ROW, ROW]})

    def test_a_missing_file_means_nothing_registered(self):
        self.assertEqual(off.load("/nonexistent/offline-accounts.json"), [])


class AsOfTests(unittest.TestCase):
    """人工登记会过时，而且过时的方向是**漏报**：登记之后新开的号不在表里，
    那个人离职时离职检查完全看不到它。所以日期必填，太旧要明说。"""

    def test_the_date_is_required(self):
        with self.assertRaises(off.OfflineError):
            off.parse({"accounts": [dict(ROW, as_of="")]})

    def test_a_readable_by_others_file_is_refused(self):
        """表里是员工的企业邮箱和姓名。"""
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as box:
            path = Path(box) / off.FILENAME
            path.write_text(json.dumps({"accounts": [ROW]}, ensure_ascii=False), encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(off.OfflineError):
                off.load(str(path))
            path.chmod(0o600)
            self.assertEqual(len(off.load(str(path))), 1)

    def _report(self, as_of, *, gone=True):
        from delivery import hygiene, inventory

        data = {
            "captured_at": "2026-10-30T00:00:00Z",
            "accounts": off.snapshot_accounts(off.parse({"accounts": [dict(ROW, as_of=as_of)]})),
        }
        snap = inventory.parse(data)
        person = type(
            "P",
            (),
            {
                "union_id": "on_1",
                "name": "黄泽楠",
                "email": "huang.zenan@wuji.tech",
                "accounts": [],
                "pending": [],
            },
        )()
        # 名册按 (platform, account, name) 找人，这里只要能对上就行
        import time

        now = time.mktime(time.strptime("2026-10-30", "%Y-%m-%d"))
        return hygiene.build(snap, [], statuses=None, now=now), snap, person

    def test_an_old_table_is_called_out(self):
        report, _snap, _p = self._report("2026-09-01")
        self.assertTrue(
            any("人工登记" in x and "可能漏报" in x for x in report.skipped), report.skipped
        )

    def test_a_fresh_table_is_not(self):
        report, _snap, _p = self._report("2026-10-25")
        self.assertFalse(any("可能漏报" in x for x in report.skipped), report.skipped)

    def test_findings_say_where_to_go_and_how_old_the_data_is(self):
        """不说的话，体检页上九章的号和阿里的号长得一样：人会以为面板能收，
        或者以为这是实时数据。"""
        report, _snap, _p = self._report("2026-10-25")
        hits = [f for f in report.orphan if f.platform == "jiuzhang"]
        self.assertTrue(hits)
        self.assertIn("九章控制台", hits[0].detail)
        self.assertIn("2026-10-25", hits[0].detail)

    def test_the_snapshot_remembers_which_accounts_are_hand_registered(self):
        _report, snap, _p = self._report("2026-10-25")
        self.assertEqual(snap.offline["jiuzhang/wuji"]["as_of"], "2026-10-25")


class SnapshotTests(unittest.TestCase):
    def test_disabled_users_stay_out_of_the_snapshot(self):
        """名册里不该出现一个人已经登不进去的号；离职检查也不该让人去停一个已经停了的号。"""
        got = off.snapshot_accounts(off.parse({"accounts": [ROW]}))
        self.assertEqual([u["name"] for u in got[0]["users"]], ["huangzenan"])
        self.assertEqual(got[0]["source"], "九章控制台导出（测试）", "要看得出这是人工登记的")

    def test_the_snapshot_parses(self):
        from delivery import inventory

        data = {
            "captured_at": "2026-09-22T00:00:00Z",
            "accounts": off.snapshot_accounts(off.parse({"accounts": [ROW]})),
        }
        snap = inventory.parse(data)
        self.assertEqual(list(snap.incomplete), [])


class RosterTests(unittest.TestCase):
    def test_accounts_link_to_people_by_email(self):
        """不关联到人的话，「谁拥有这个九章号」永远对不上，离职检查也查不到它。"""
        got = propose(off.cloud_accounts(off.parse({"accounts": [ROW]})), domain="wuji.tech")
        people = got.to_dict()["people"]
        self.assertEqual(len(people), 1)
        link = people[0]["links"][0]
        self.assertEqual(
            (people[0]["email"], link["scope"], link["status"]),
            ("huang.zenan@wuji.tech", "jiuzhang/wuji", "confirmed"),
        )
        self.assertNotIn("已验证", link["evidence"][0], "管理员录入的邮箱没有「本人验证」这回事")


class RefreshMergeTests(unittest.TestCase):
    def test_cloud_accounts_are_untouched_and_offline_ones_appended(self):
        from delivery.cli import _with_offline

        cloud = {
            "captured_at": "x",
            "accounts": [{"platform": "aliyun", "account": "1", "users": []}],
        }
        got = _with_offline(cloud, off.parse({"accounts": [ROW]}))
        self.assertEqual([a["platform"] for a in got["accounts"]], ["aliyun", "jiuzhang"])
        self.assertEqual(len(cloud["accounts"]), 1, "原来那份快照不能被改")


if __name__ == "__main__":
    unittest.main()


class EntryPointOrderTests(unittest.TestCase):
    """`python -m delivery.cli` 是从上往下执行的：写在 `if __name__ == "__main__"` 后面的函数，
    `main()` 跑的时候还不存在。单测是 import 后调用，走不到这条路 —— 线上刷新却会
    NameError（真发生过：`_with_offline` 加在了文件末尾，整轮刷新失败）。"""

    def test_nothing_is_defined_after_the_entry_point(self):
        import ast
        from pathlib import Path

        import delivery.cli as cli

        for mod in (cli,):
            tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
            entry = next(
                (
                    i
                    for i, n in enumerate(tree.body)
                    if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)
                ),
                None,
            )
            if entry is None:
                continue
            after = [
                n.name
                for n in tree.body[entry + 1 :]
                if isinstance(n, (ast.FunctionDef, ast.ClassDef))
            ]
            self.assertEqual(after, [], f"{mod.__name__} 入口之后还定义了 {after}")
