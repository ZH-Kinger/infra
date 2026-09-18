"""子账号 AK 的采集与判定。

这块的核心是**三种状态不能混**：
  · 采到了，有几把      → keys 是元组
  · 采到了，一把都没有  → keys 是空元组（这是好事）
  · **没采到**          → keys 是 None（采集身份缺权限，要去查）
混成一个值的话，上线那天所有人都会显示成「零 AK」，而那正是最该被发现的状态。

另一个容易写反的地方：「该轮换」看的是**建了多久**，不是「最近用没用」。
一把天天在用的老 AK 才是最该换的那种，按「最近没用过」筛会正好把它漏掉。
"""

from __future__ import annotations

import unittest

from delivery import inventory

from .test_delivery_inventory_collect import UID, aliyun_fixture

DAY = 86400
NOW = 1_800_000_000.0  # 固定时钟，别让用例随日期漂


def key(kid, *, status="Active", created="", last_used=""):
    return {
        "AccessKeyId": kid,
        "Status": status,
        "CreateDate": created,
        "LastUsedDate": last_used,
    }


def iso(ts):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(users):
    return inventory.parse(
        {
            "captured_at": "2026-09-18T00:00:00+08:00",
            "accounts": [
                {"platform": "aliyun", "account": UID, "groups": [], "users": users},
            ],
        }
    )


class ParseTests(unittest.TestCase):
    def test_missing_key_means_not_collected_not_empty(self):
        """老快照没有 `keys` 这一项 —— 那是「没采到」，不是「没有 AK」。"""
        snap = snapshot([{"name": "old", "policies": [], "groups": []}])
        self.assertIsNone(snap.users[0].keys)

    def test_empty_list_means_really_no_keys(self):
        snap = snapshot([{"name": "clean", "policies": [], "groups": [], "keys": []}])
        self.assertEqual(snap.users[0].keys, ())

    def test_a_key_without_id_is_refused(self):
        with self.assertRaises(inventory.InventoryError):
            snapshot([{"name": "x", "policies": [], "groups": [], "keys": [{"status": "Active"}]}])

    def test_secret_is_never_part_of_the_model(self):
        """AK 的 secret 不该出现在快照里 —— 接口本来也不给，模型上也不留位置。"""
        snap = snapshot([{"name": "x", "policies": [], "groups": [], "keys": [{"id": "LTAI1234"}]}])
        fields = snap.users[0].keys[0].__dataclass_fields__
        self.assertNotIn("secret", fields)
        self.assertNotIn("access_key_secret", fields)


class JudgementTests(unittest.TestCase):
    def user(self, keys):
        return snapshot([{"name": "u", "policies": [], "groups": [], "keys": keys}]).users[0]

    def test_an_old_key_in_daily_use_still_needs_rotating(self):
        """最该换的就是这种。按「最近没用过」筛会正好漏掉它 —— 这条用来钉住判据。"""
        u = self.user(
            [
                {
                    "id": "A",
                    "status": "Active",
                    "created": iso(NOW - 400 * DAY),
                    "last_used": iso(NOW - DAY),
                }
            ]
        )
        self.assertEqual([k.id for k in u.stale_keys(now=NOW)], ["A"])
        self.assertEqual(u.unused_keys(now=NOW), ())

    def test_a_key_never_used_is_unused_not_just_stale(self):
        """阿里云对从没用过的返回 `N/A`。这类不该提醒轮换 —— 换一把同样没人用的没意义。"""
        u = self.user(
            [{"id": "B", "status": "Active", "created": iso(NOW - 300 * DAY), "last_used": "N/A"}]
        )
        self.assertEqual([k.id for k in u.unused_keys(now=NOW)], ["B"])
        self.assertEqual([k.id for k in u.stale_keys(now=NOW)], ["B"])  # 也确实够老

    def test_a_fresh_key_bothers_nobody(self):
        u = self.user(
            [
                {
                    "id": "C",
                    "status": "Active",
                    "created": iso(NOW - 3 * DAY),
                    "last_used": iso(NOW - DAY),
                }
            ]
        )
        self.assertEqual(u.stale_keys(now=NOW), ())
        self.assertEqual(u.unused_keys(now=NOW), ())

    def test_an_inactive_key_is_not_reported(self):
        """已经停用的不用再提醒 —— 提醒它等于教人忽略这类通知。"""
        u = self.user(
            [{"id": "D", "status": "Inactive", "created": iso(NOW - 900 * DAY), "last_used": "N/A"}]
        )
        self.assertEqual(u.stale_keys(now=NOW), ())
        self.assertEqual(u.unused_keys(now=NOW), ())

    def test_not_collected_reports_nothing_rather_than_everything(self):
        """没采到时两个清单都得是空 —— 报成「全员都该轮换」比不报更糟。"""
        u = snapshot([{"name": "u", "policies": [], "groups": []}]).users[0]
        self.assertEqual(u.stale_keys(now=NOW), ())
        self.assertEqual(u.unused_keys(now=NOW), ())


class CollectTests(unittest.TestCase):
    def collect(self, **kw):
        from delivery.clouds import aliyun
        from delivery.inventory_collect import collect_aliyun

        fake = aliyun_fixture(**kw)
        data = collect_aliyun(aliyun.Credentials("ak", "sk"), transport=fake)
        return inventory.parse({"captured_at": "x", "accounts": [data]}), fake

    def test_keys_come_through_with_last_used(self):
        snap, _ = self.collect(
            keys={
                "alice": [key("LTAI0001", created=iso(NOW - 400 * DAY), last_used=iso(NOW - DAY))]
            }
        )
        alice = next(u for u in snap.users if u.name == "alice")
        self.assertEqual([k.id for k in alice.keys], ["LTAI0001"])
        self.assertEqual(alice.keys[0].status, "Active")
        self.assertTrue(alice.keys[0].last_used)

    def test_only_the_prefix_of_the_key_id_is_stored(self):
        """完整 AKId 不进快照：快照会被传阅，而前 8 位足够认出是不是同一把。"""
        snap, _ = self.collect(keys={"alice": [key("LTAI0123456789ABCDEF")]})
        alice = next(u for u in snap.users if u.name == "alice")
        self.assertEqual(alice.keys[0].id, "LTAI0123")
        self.assertNotIn("ABCDEF", alice.keys[0].id)

    def test_permission_denied_reports_not_collected_not_empty(self):
        """缺 ListAccessKeys 权限时是「没采到」，不能装成「这个人没有 AK」。"""
        snap, _ = self.collect(
            overrides={
                "ListAccessKeys": lambda q: (
                    403,
                    {"Code": "NoPermission", "Message": "You are not authorized"},
                )
            }
        )
        self.assertTrue(all(u.keys is None for u in snap.users))

    def test_users_with_no_keys_are_an_empty_tuple(self):
        snap, _ = self.collect(keys={})
        self.assertTrue(all(u.keys == () for u in snap.users))


if __name__ == "__main__":
    unittest.main()
