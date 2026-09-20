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


class LastUsedUnknownTests(unittest.TestCase):
    """**「这朵云查不到」不是「从来没用过」。**

    火山的 `ListAccessKeys` 不返回最近使用时间，也没有阿里那种 `GetAccessKeyLastUsed`
    可以补问。第一版把 `last_used` 填成空串，而下游把空串当「从来没用过」——
    体检里「超过 90 天没用过」当场从 23 条涨到 67 条，多出来的 44 条全是火山的，
    没有一条是真的。**44 条同时出现的假线索，足以让这一栏从此没人看。**

    所以判据是：拿不到最近使用时间的，`unused` 一律不算；
    而「该换了」只看**年龄**，不受影响 —— 火山那些老 AK 照样会被提醒换。
    """

    def user(self, keys):
        return snapshot([{"name": "u", "policies": [], "groups": [], "keys": keys}]).users[0]

    def vkey(self, kid="V", created_days=300):
        """火山采上来的那种：值是空的，且明确标了「这朵云查不到」。"""
        return {
            "id": kid,
            "status": "Active",
            "created": iso(NOW - created_days * DAY),
            "last_used": "",
            "last_used_known": False,
        }

    def test_old_snapshots_default_to_known(self):
        """老快照里没有 `last_used_known` 这一项。默认成 False 的话，阿里那 67 把
        AK 会一夜之间全部失去「没人用」的判断 —— 这一栏直接空掉，
        而空的那一栏看起来和「大家都很健康」一模一样。"""
        u = self.user([{"id": "A", "status": "Active", "created": iso(NOW - 300 * DAY)}])
        self.assertIs(u.keys[0].last_used_known, True)
        self.assertEqual([k.id for k in u.unused_keys(now=NOW)], ["A"])

    def test_unknown_is_not_counted_as_unused(self):
        self.assertEqual(self.user([self.vkey()]).unused_keys(now=NOW), ())

    def test_but_it_still_needs_rotating(self):
        """反向锁：别把「不知道用没用」顺手变成「这把 AK 一概不管」。
        年龄是自己算得出来的，火山那 44 把老 AK 照样该换。"""
        self.assertEqual([k.id for k in self.user([self.vkey()]).stale_keys(now=NOW)], ["V"])

    def test_same_data_two_clouds_different_verdict(self):
        """**同样的数据，判断不同 —— 差别只在这朵云给不给最近使用时间。**

        阿里能问出「从没用过」（`GetAccessKeyLastUsed` 回 `N/A`），那是事实，该报；
        火山问不出来，那是我们的盲区，不该报。两者摆在一起，免得有人「统一一下」
        把其中一边改成另一边 —— 无论统一成哪边都是错的。
        """
        both = self.user(
            [
                # 阿里：确实从来没用过
                {
                    "id": "ALI",
                    "status": "Active",
                    "created": iso(NOW - 300 * DAY),
                    "last_used": "N/A",
                },
                self.vkey("VOL"),  # 火山：这朵云根本不给
            ]
        )
        self.assertEqual([k.id for k in both.unused_keys(now=NOW)], ["ALI"])
        self.assertEqual(sorted(k.id for k in both.stale_keys(now=NOW)), ["ALI", "VOL"])

    def test_collected_volcano_keys_are_not_reported_as_unused_end_to_end(self):
        """从采集一路走到判定：火山采上来的 AK 不会出现在「没人用」里。

        中间任何一环把标记丢掉（采集不写、`parse` 不读、`unused_keys` 不看）
        都会让这条红 —— 只锁其中一环的话，另外两环随时能把 bug 放回来。
        """
        from delivery.clouds import volcano
        from delivery.inventory_collect import collect_volcano

        from .test_delivery_inventory_collect import volcano_fixture

        fake = volcano_fixture(
            keys={
                "ShenYi": [
                    # 2024 年建的，够老 → 该换；但「用没用过」这朵云答不上来
                    {
                        "AccessKeyId": "AKLT0001",
                        "Status": "Active",
                        "CreateDate": "20240204T104530Z",
                    }
                ]
            }
        )
        data = collect_volcano(volcano.Credentials("ak", "sk"), transport=fake)
        snap = inventory.parse({"captured_at": "x", "accounts": [data]})
        shen = next(u for u in snap.users if u.name == "ShenYi")
        self.assertEqual([k.id for k in shen.keys], ["AKLT0001"])
        self.assertIs(shen.keys[0].last_used_known, False)
        self.assertEqual(shen.unused_keys(now=NOW), (), "火山给不了最近使用时间，不该报「没人用」")
        self.assertEqual([k.id for k in shen.stale_keys(now=NOW)], ["AKLT0001"])


class TimestampTests(unittest.TestCase):
    """两朵云的时间格式不一样，`_ts` 两种都得认。

    火山给的是紧凑格式 `20260204T104530Z`，`fromisoformat` 不认 —— 原来一律解析成 0，
    于是**年龄也跟着错**：一把 2024 年建的 AK 算出来的 `created_ts` 是 0，
    「建了多久」这一栏失真，而那正是火山 AK 唯一还判得准的那一维。
    """

    def ts(self, text):
        return inventory._ts(text)

    def test_volcano_compact_format(self):
        from datetime import datetime, timezone

        expect = datetime(2026, 2, 4, 10, 45, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.ts("20260204T104530Z"), expect)

    def test_both_formats_agree_on_the_same_instant(self):
        """同一时刻的两种写法必须解析成同一个数。当成本地时区解析就会差 8 小时，
        「建了多久」在月底那几天会整整飘一天。"""
        self.assertEqual(self.ts("20260204T104530Z"), self.ts("2026-02-04T10:45:30Z"))
        self.assertEqual(self.ts("2026-01-07T02:11:03Z"), self.ts("20260107T021103Z"))

    def test_unparsable_is_zero_not_a_crash(self):
        """认不出就返回 0：年龄变成「不详」而不是「很老」，方向安全（不会凭空造出
        一条「该换了」）。抛异常则是整页打不开。"""
        for bad in ("", None, "N/A", "n/a", "下周", "2026-13-45", "20260204104530", 12345):
            self.assertEqual(self.ts(bad), 0.0, bad)

    def test_a_volcano_key_gets_a_real_age(self):
        """整条链路：紧凑格式 → `created_ts` → 「该换了」。
        `_ts` 认不出的话这里会安静地退化成「不详」，而不是报错。"""
        u = snapshot(
            [
                {
                    "name": "u",
                    "policies": [],
                    "groups": [],
                    "keys": [
                        {
                            "id": "V",
                            "status": "Active",
                            "created": "20240204T104530Z",
                            "last_used_known": False,
                        }
                    ],
                }
            ]
        ).users[0]
        self.assertGreater(u.keys[0].created_ts, 0)
        self.assertEqual([k.id for k in u.stale_keys(now=NOW)], ["V"])


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
