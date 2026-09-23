"""待办聚合（`delivery.todo`）：排序、分组、依据旧了怎么办、每个采集器各自的边界。

为什么这一层值得钉死
────────────────────
待办页是管理员的落地页，**它说第一条是什么，第一条就是今天被处理的那件事**。
所以顺序不是排版问题：排错了等于替管理员做了「先干哪件」的决定，而且没人会发现 ——
页面照样打得开，每一条也都是真的，只是最该先做的那件排在第七位。

同理，「依据旧了」必须影响结论的位置和措辞。拿三天前的对账结果说「他现在还能登控制台」，
说对了是运气，说错了下次就没人信这一页了。

数据全部虚构，不碰文件、不碰网络。
"""

from __future__ import annotations

import time
import unittest

from delivery import todo

DAY = 86400


def iso(ts: float) -> str:
    """时间戳 → 面板里各数据源实际写出来的那种带时区 ISO（`timespec="seconds"`）。"""
    import datetime

    return (
        datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def item(**over) -> todo.Item:
    base = {"kind": "k", "group": todo.NORMAL, "title": "t", "what": "w"}
    base.update(over)
    return todo.Item(**base)


def titles(view: dict, group: str) -> list:
    for g in view["groups"]:
        if g["group"] == group:
            return [r["title"] for r in g["items"]]
    return []


class OrderTests(unittest.TestCase):
    """排序：分组 → 挂得久的在前 → 能批量的在前 → kind。"""

    def test_groups_come_out_in_severity_order_and_empty_ones_vanish(self):
        r = todo.Report()
        r.add(item(kind="c", group=todo.CHORE, title="顺手"))
        r.add(item(kind="u", group=todo.URGENT, title="要紧"))
        view = r.view()
        self.assertEqual([g["group"] for g in view["groups"]], [todo.URGENT, todo.CHORE])
        # 空分组不占一张卡：一张写着「0 项」的卡片和一件待办长得一样显眼
        self.assertNotIn(todo.NORMAL, [g["group"] for g in view["groups"]])
        self.assertEqual([g["name"] for g in view["groups"]], ["要紧的", "顺手做的"])

    def test_unknown_group_sinks_to_the_bottom_instead_of_crashing(self):
        # 将来加一类忘了登记进 GROUPS：它不该把整页排序搞崩，也不该混进前两组里
        r = todo.Report()
        r.add(item(kind="x", group="whatever", title="没登记的"))
        r.add(item(kind="u", group=todo.URGENT, title="要紧"))
        view = r.view()
        self.assertEqual([g["group"] for g in view["groups"]], [todo.URGENT])
        self.assertNotIn("没登记的", str(view["groups"]))
        # 但它仍然计进总数 —— 角标少数一件比多数一件危险
        self.assertEqual(view["counts"]["total"], 2)

    def test_batchable_first_when_equally_old(self):
        now = time.time()
        r = todo.Report()
        r.add(item(kind="a1", title="一条一条点", since=now - DAY, batch=False))
        r.add(item(kind="a2", title="一次清一批", since=now - DAY, batch=True))
        self.assertEqual(titles(r.view(), todo.NORMAL), ["一次清一批", "一条一条点"])

    def test_kind_is_the_last_tiebreak_so_the_order_is_stable(self):
        # 同组、同龄、同批量属性时必须有确定的顺序：否则每次刷新页面顺序都在跳，
        # 而「跳来跳去的清单」等于没有清单
        r = todo.Report()
        r.add(item(kind="zeta", title="Z"))
        r.add(item(kind="alpha", title="A"))
        self.assertEqual(titles(r.view(), todo.NORMAL), ["A", "Z"])

    def test_items_without_a_timestamp_sort_after_dated_ones(self):
        # 不知道挂了多久的事项排在知道的后面：它可能是刚出现的，不该抢占第一位
        now = time.time()
        r = todo.Report()
        r.add(item(kind="b", title="没时间戳"))
        r.add(item(kind="a", title="有时间戳", since=now - DAY))
        self.assertEqual(titles(r.view(), todo.NORMAL), ["有时间戳", "没时间戳"])

    def test_oldest_pending_item_comes_first(self):
        """**已知 BUG**：`todo._order` 把最新的排在最前，和文档里写的正好相反。

        `src/delivery/todo.py:127` 用 `-(item.since or 0)` 当排序键。`since` 是**时间戳**
        （`Item.row` 拿它算 `age_days` 可证），越老的时间戳越小，取负之后越大 ——
        升序排下来「今天刚出现的」在最前，「挂了 30 天没人管的」沉到最后。

        期望：挂了 30 天的排第一。实际：挂了 1 小时的排第一。
        修法：键改成 `item.since if item.since is not None else float("inf")`
        （保持「没时间戳的沉底」这条，见上一条用例）。
        """
        now = time.time()
        r = todo.Report()
        r.add(item(kind="a", title="挂了一小时", since=now - 3600))
        r.add(item(kind="b", title="挂了三十天", since=now - 30 * DAY))
        self.assertEqual(titles(r.view(), todo.NORMAL), ["挂了三十天", "挂了一小时"])


class CountTests(unittest.TestCase):
    def test_counts_add_up_the_件数_not_the_行数(self):
        # 一行汇总了 12 把密钥，角标就该知道是 12 件事而不是 1 行 ——
        # 前端角标读的就是这个 counts
        r = todo.Report()
        r.add(item(kind="k", group=todo.URGENT, count=3))
        r.add(item(kind="m", group=todo.NORMAL, count=5))
        r.add(item(kind="n", group=todo.CHORE, count=12))
        counts = r.view()["counts"]
        self.assertEqual(counts, {"urgent": 3, "normal": 5, "total": 20})

    def test_fold_after_is_published_so_the_page_and_the_card_fold_alike(self):
        self.assertEqual(todo.Report().view()["fold_after"], todo.FOLD_AFTER)
        self.assertGreaterEqual(todo.FOLD_AFTER, 1)


class HeadlineTests(unittest.TestCase):
    def test_nothing_to_do(self):
        self.assertEqual(todo.Report().view()["headline"], "没有需要你处理的。")

    def test_errors_only_never_says_all_clear(self):
        # 「没算出来」和「没有问题」在页面上必须分得开。混成一句的那天，
        # 一次采集失败就会被读成「今天很干净」
        r = todo.Report()
        r.errors.append("申请单没算出来：OSError")
        head = r.view()["headline"]
        self.assertIn("没算出来", head)
        self.assertNotIn("没有需要你处理的", head)

    def test_both_buckets_are_named(self):
        r = todo.Report()
        r.add(item(group=todo.URGENT, kind="a", count=2))
        r.add(item(group=todo.NORMAL, kind="b", count=1))
        self.assertEqual(r.view()["headline"], "今天有 2 件要紧的、1 件可以顺手处理的。")

    def test_errors_do_not_hide_the_real_headline(self):
        r = todo.Report()
        r.add(item(group=todo.URGENT, kind="a"))
        r.errors.append("密钥没算出来：DeliveryError")
        self.assertIn("要紧的", r.view()["headline"])

    def test_chore_only_reads_as_nothing_urgent(self):
        """只有杂活时标题说「没有需要你处理的」，但 total 仍然是 1。

        这不是笔误：杂活不该让人以为今天有火要救。锁住是因为前端角标读 total、
        标题读 headline，两者对不上的时候得有个地方说明这是有意的。
        """
        r = todo.Report()
        r.add(item(group=todo.CHORE, kind="k", count=4))
        view = r.view()
        self.assertEqual(view["headline"], "没有需要你处理的。")
        self.assertEqual(view["counts"], {"urgent": 0, "normal": 0, "total": 4})


class TimeTests(unittest.TestCase):
    def test_the_iso_shapes_the_panel_actually_writes_all_parse(self):
        for text in (
            "2026-09-01T00:00:00+08:00",  # assets/policies/inventory 的 _now()
            "2026-09-01T00:00:00+0800",  # iam_sync 的 time.strftime("%z")
            "2026-09-01T00:00:00Z",
            "2026-09-01 00:00:00",
            "2026-09-01",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(todo.days_ago(text), f"{text} 应该认得出来")

    def test_unparsable_time_is_none_not_zero(self):
        # 返回 0 的话就是「1970 年」，挂了两万天 —— 那条会被顶到清单最前面
        for text in ("", None, "昨天", "2026/09/01", "not a time"):
            with self.subTest(text=text):
                self.assertIsNone(todo.days_ago(text))

    def test_microsecond_iso_is_not_recognised(self):
        """`datetime.isoformat()` 默认带微秒，这里认不出来 → 一律当「旧的」。

        方向是安全的（当旧的只会降级，不会误报），但代价是：以后哪个采集器忘了写
        `timespec="seconds"`，那份数据就会**永远**显示成「旧了」，而页面上没有任何线索
        指向真正的原因。现在面板里所有写时间的地方都带 timespec，所以这里只是把
        边界钉住，不算线上缺陷。
        """
        self.assertIsNone(todo.days_ago("2026-09-01T00:00:00.123456+08:00"))
        self.assertTrue(todo.stale("snapshot", "2026-09-01T00:00:00.123456+08:00"))

    def test_stale_uses_per_source_limits(self):
        now = time.time()
        # 快照 24 小时：采集每 20 分钟跑一次，隔了一天就是采集挂了
        self.assertFalse(todo.stale("snapshot", iso(now - 23 * 3600), now=now))
        self.assertTrue(todo.stale("snapshot", iso(now - 25 * 3600), now=now))
        # 对账 3 天
        self.assertFalse(todo.stale("reconcile", iso(now - 2 * DAY), now=now))
        self.assertTrue(todo.stale("reconcile", iso(now - 4 * DAY), now=now))
        # 人工登记 30 天
        self.assertFalse(todo.stale("offline", iso(now - 20 * DAY), now=now))

    def test_unknown_source_falls_back_to_three_days(self):
        now = time.time()
        self.assertFalse(todo.stale("没登记过的源", iso(now - 2 * DAY), now=now))
        self.assertTrue(todo.stale("没登记过的源", iso(now - 4 * DAY), now=now))

    def test_missing_timestamp_counts_as_stale(self):
        # 和体检那条「查不了就不下结论」同一条规矩
        self.assertTrue(todo.stale("snapshot", ""))
        self.assertTrue(todo.stale("snapshot", "谁知道"))


class RowTests(unittest.TestCase):
    def test_age_days_is_none_when_we_do_not_know(self):
        self.assertIsNone(item().row()["age_days"])

    def test_age_days_never_goes_negative(self):
        # 机器时钟往回跳、或者数据源写了个未来时间：写成「挂了 -3 天」看的人只会当页面坏了
        self.assertEqual(item(since=time.time() + 10 * DAY).row()["age_days"], 0)

    def test_age_days_floors(self):
        self.assertEqual(item(since=time.time() - 3.9 * DAY).row()["age_days"], 3)

    def test_row_carries_everything_the_page_renders(self):
        row = item(
            kind="k",
            group=todo.URGENT,
            title="标题",
            what="后果",
            action="去处理",
            href="#admin/iam",
            source="对账",
            source_at="2026-09-01T00:00:00+08:00",
            batch=True,
            weak="数据旧了",
            count=7,
        ).row()
        self.assertEqual(
            set(row),
            {
                "kind",
                "group",
                "title",
                "what",
                "action",
                "href",
                "source",
                "source_at",
                "age_days",
                "batch",
                "weak",
                "count",
            },
        )


class WeakenTests(unittest.TestCase):
    def test_weaken_demotes_urgent_and_says_why(self):
        got = todo.weaken(item(group=todo.URGENT), "本次没查在职状态")
        self.assertEqual(got.group, todo.NORMAL)
        self.assertEqual(got.weak, "本次没查在职状态")

    def test_weaken_never_deletes_the_item(self):
        # 降级不是删除：提醒还要给，只是不占第一位
        got = todo.weaken(item(group=todo.NORMAL, title="还在"), "依据不足")
        self.assertEqual(got.title, "还在")
        self.assertEqual(got.group, todo.NORMAL)

    def test_weaken_leaves_chores_alone(self):
        got = todo.weaken(item(group=todo.CHORE), "依据不足")
        self.assertEqual(got.group, todo.CHORE)


class OffboardTests(unittest.TestCase):
    def rec(self, **over):
        base = {
            "person": "李四",
            "user": "lisi",
            "platform": "aliyun",
            "account": "A1",
            "state": "disabled",
            "at": iso(time.time() - 5 * DAY),
        }
        base.update(over)
        return base

    def one(self, rows, kind):
        r = todo.Report()
        todo.collect_offboard(r, rows)
        got = [i for i in r.items if i.kind == kind]
        self.assertEqual(len(got), 1, f"{kind} 应当只有一条：{[i.kind for i in r.items]}")
        return got[0]

    def test_one_person_three_accounts_is_one_row(self):
        """一个人名下三个号是**一件事**，不是三件。

        拆成三条的后果：离职这一类会把别的类挤出屏幕，而管理员处理它们本来就是一次点完。
        """
        rows = [self.rec(user=f"lisi{i}") for i in range(3)]
        got = self.one(rows, "offboard_pending")
        self.assertEqual(got.count, 3)
        self.assertTrue(got.batch, "三个号要标成可批量，才会排在同组前面")
        self.assertIn("李四", got.title)
        self.assertEqual(got.group, todo.URGENT)

    def test_single_account_is_not_advertised_as_batchable(self):
        got = self.one([self.rec()], "offboard_pending")
        self.assertFalse(got.batch)
        self.assertEqual(got.count, 1)

    def test_two_people_are_two_rows(self):
        r = todo.Report()
        todo.collect_offboard(r, [self.rec(), self.rec(person="王五", user="wangwu")])
        self.assertEqual(len([i for i in r.items if i.kind == "offboard_pending"]), 2)

    def test_disabled_and_untouched_say_different_things(self):
        """「已停用但没删」和「面板压根没动过」的紧迫程度不一样，文案必须分开。

        混成一句的话，管理员会以为那个人已经登不进去了 —— 而 suspect 那种他还能登。
        """
        stopped = self.one([self.rec(state="disabled")], "offboard_pending")
        self.assertIn("停用可逆", stopped.what)
        fresh = self.one([self.rec(state="suspect")], "offboard_pending")
        self.assertIn("仍然能登", fresh.what)

    def test_jiuzhang_is_split_out_as_a_manual_chore(self):
        """九章没接口，面板动不了 —— 它和「点一下就删掉」必须是两条。

        并到一起的话，管理员点了「去处理」会发现按钮对九章那个号没反应，
        而页面没有任何地方解释为什么。
        """
        r = todo.Report()
        todo.collect_offboard(r, [self.rec(), self.rec(platform="jiuzhang", user="lisi-jz")])
        kinds = sorted(i.kind for i in r.items)
        self.assertEqual(kinds, ["offboard_manual", "offboard_pending"])
        manual = [i for i in r.items if i.kind == "offboard_manual"][0]
        self.assertEqual(manual.group, todo.NORMAL, "面板做不了的事不该排在能一键做的前面")
        self.assertIn("控制台", manual.title)
        self.assertIn("面板动不了", manual.what)
        auto = [i for i in r.items if i.kind == "offboard_pending"][0]
        self.assertEqual(auto.count, 1, "九章那条不该重复计进可自动处理的那条")

    def test_only_jiuzhang_means_no_auto_row(self):
        r = todo.Report()
        todo.collect_offboard(r, [self.rec(platform="jiuzhang")])
        self.assertEqual([i.kind for i in r.items], ["offboard_manual"])

    def test_empty_input_adds_nothing(self):
        for rows in ([], None):
            r = todo.Report()
            todo.collect_offboard(r, rows)
            self.assertEqual(r.items, [])

    def test_age_comes_from_the_oldest_record_of_that_person(self):
        old = time.time() - 40 * DAY
        r = todo.Report()
        todo.collect_offboard(
            r, [self.rec(user="a", at=iso(time.time() - DAY)), self.rec(user="b", at=iso(old))]
        )
        got = [i for i in r.items if i.kind == "offboard_pending"][0]
        self.assertAlmostEqual(got.since, old, delta=2)

    def test_one_unreadable_timestamp_does_not_drop_the_whole_age(self):
        """回归（曾是缺陷）：一条记录时间认不出来，不该把同一个人另一条「挂了 40 天」
        的事实一起抹掉 —— 抹掉之后这条待办会沉到最底，而它本该排第一。"""
        r = todo.Report()
        todo.collect_offboard(
            r,
            [
                self.rec(user="a", at="坏掉的时间"),
                self.rec(user="b", at=iso(time.time() - 40 * DAY)),
            ],
        )
        item = r.items[0]
        self.assertIsNotNone(item.since)
        self.assertEqual(item.row()["age_days"], 40)


class IamDriftTests(unittest.TestCase):
    def stale_cached(self, *drift):
        """一份十天前的对账结果（`STALE["reconcile"]` 是 3 天）。"""
        return self.cached(*drift, checked_at=iso(time.time() - 10 * DAY))

    def cached(self, *drift, checked_at=None):
        return {
            "checked_at": checked_at if checked_at is not None else iso(time.time() - 3600),
            "apps": [{"app": "aliyun_username", "drift": list(drift)}],
        }

    def d(self, union_id="on_a", name="李四", kind="inactive"):
        return {"kind": kind, "union_id": union_id, "name": name}

    def test_inactive_logins_are_urgent(self):
        r = todo.Report()
        todo.collect_iam_drift(r, self.cached(self.d()), set())
        got = r.items[0]
        self.assertEqual(got.kind, "iam_inactive")
        self.assertEqual(got.group, todo.URGENT)
        self.assertIn("SSO", got.what, "要说清后果：他现在还能进控制台")
        self.assertIn("李四", got.what)

    def test_other_drift_kinds_are_ignored(self):
        # 这一条只管「人已离职、登录名还在」。把「两边没同步」混进来会稀释掉要紧的那类
        r = todo.Report()
        todo.collect_iam_drift(r, self.cached(self.d(kind="missing")), set())
        self.assertEqual(r.items, [])

    def test_snoozed_entries_drop_out(self):
        """点过「稍后处理」的不再出现 —— 否则那个按钮等于没有。"""
        r = todo.Report()
        todo.collect_iam_drift(r, self.cached(self.d()), {"aliyun_username/on_a"})
        self.assertEqual(r.items, [])

    def test_snooze_key_is_app_scoped(self):
        # 键是 `<app>/<union_id>`：只 snooze 了火山那条，不该把阿里那条也一起藏掉
        r = todo.Report()
        todo.collect_iam_drift(r, self.cached(self.d()), {"volcano_username/on_a"})
        self.assertEqual(len(r.items), 1)

    def test_no_drift_no_row(self):
        r = todo.Report()
        todo.collect_iam_drift(r, self.cached(), set())
        todo.collect_iam_drift(r, {}, set())
        self.assertEqual(r.items, [])

    def test_at_most_three_names_are_spelled_out(self):
        r = todo.Report()
        todo.collect_iam_drift(
            r, self.cached(*[self.d(union_id=f"on_{i}", name=f"人{i}") for i in range(6)]), set()
        )
        got = r.items[0]
        self.assertEqual(got.count, 6)
        self.assertEqual(got.what.count("、"), 2, "只点名三个，其余用「等人」带过")
        self.assertIn("等人", got.what)

    def test_stale_reconcile_is_flagged(self):
        r = todo.Report()
        todo.collect_iam_drift(r, self.stale_cached(self.d()), set())
        self.assertIn("刷新", r.items[0].weak)

    def test_fresh_reconcile_is_not_flagged(self):
        r = todo.Report()
        todo.collect_iam_drift(r, self.cached(self.d()), set())
        self.assertEqual(r.items[0].weak, "")

    def test_stale_reconcile_is_demoted_out_of_urgent(self):
        """**已知缺口**：模块开头写着「依据旧了就降级到 NORMAL」，但没有人调用 `weaken`。

        `src/delivery/todo.py:233` 的 `collect_iam_drift` 只填了 `weak=` 文案，
        `group` 仍然是 `URGENT`。后果有两层：
          · 一条十天前的对账结果排在今天刚发生的事情前面；
          · 它还会计进 `counts.urgent` → 导航角标那个红点，是拿一份过期数据点亮的。
        模块 docstring（`src/delivery/todo.py:18-20`）明确说反了这件事，而 `weaken`
        （`src/delivery/todo.py:145`）写好了从未被调用 —— 全仓库零处引用。

        期望：group == NORMAL。实际：group == URGENT。
        修法：`collect_iam_drift` 里 stale 时 `report.add(todo.weaken(item, "..."))`。
        """
        r = todo.Report()
        todo.collect_iam_drift(r, self.stale_cached(self.d()), set())
        self.assertEqual(r.items[0].group, todo.NORMAL)


class TicketTests(unittest.TestCase):
    def test_failed_and_needs_iam_are_two_different_rows(self):
        r = todo.Report()
        todo.collect_tickets(
            r,
            [
                {"state": "failed", "created_at": iso(time.time() - 2 * DAY)},
                {"state": "done", "needs_iam": True},
            ],
        )
        self.assertEqual(sorted(i.kind for i in r.items), ["request_failed", "request_no_iam"])
        for got in r.items:
            self.assertEqual(got.group, todo.URGENT, "两类都有人在等，都是要紧的")

    def test_failed_row_points_at_the_filtered_list(self):
        r = todo.Report()
        todo.collect_tickets(r, [{"state": "failed", "created_at": iso(time.time())}])
        self.assertIn("state=failed", r.items[0].href)

    def test_needs_iam_is_batchable_only_when_there_are_several(self):
        r = todo.Report()
        todo.collect_tickets(r, [{"needs_iam": True}])
        self.assertFalse(r.items[0].batch)
        r = todo.Report()
        todo.collect_tickets(r, [{"needs_iam": True}, {"needs_iam": True}])
        self.assertTrue(r.items[0].batch)

    def test_nothing_pending_nothing_added(self):
        r = todo.Report()
        todo.collect_tickets(r, [{"state": "done"}, {"state": "pending"}])
        todo.collect_tickets(r, None)
        self.assertEqual(r.items, [])

    def test_age_is_the_oldest_failed_ticket(self):
        old = time.time() - 9 * DAY
        r = todo.Report()
        todo.collect_tickets(
            r,
            [
                {"state": "failed", "created_at": iso(time.time() - DAY)},
                {"state": "failed", "created_at": iso(old)},
            ],
        )
        self.assertAlmostEqual(r.items[0].since, old, delta=2)


class ExpiringTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def rows(self, *offsets):
        return [{"expires_at_ts": self.NOW + o, "who": f"u{i}"} for i, o in enumerate(offsets)]

    def kinds(self, *offsets):
        r = todo.Report()
        todo.collect_expiring(r, self.rows(*offsets), now=self.NOW)
        return {i.kind: i for i in r.items}

    def test_inside_seven_days_is_expiring(self):
        got = self.kinds(3 * DAY)
        self.assertIn("cred_expiring", got)
        self.assertEqual(got["cred_expiring"].group, todo.NORMAL)

    def test_exactly_seven_days_is_still_inside(self):
        self.assertIn("cred_expiring", self.kinds(7 * DAY))

    def test_just_past_seven_days_is_out(self):
        self.assertEqual(self.kinds(7 * DAY + 1), {})

    def test_exactly_now_counts_as_expired_not_expiring(self):
        """到期那一刻属于「已过期」。

        两边都算的话，同一张凭证会在页面上同时出现在两行里；两边都不算的话，
        它在到期那一刻从清单上消失 —— 恰恰是最该被看见的时候。
        """
        got = self.kinds(0)
        self.assertEqual(list(got), ["cred_expired"])

    def test_already_expired_is_its_own_row(self):
        got = self.kinds(-2 * DAY)
        self.assertIn("cred_expired", got)
        self.assertIn("没人管的密钥", got["cred_expired"].what)

    def test_both_rows_can_coexist(self):
        got = self.kinds(-DAY, 2 * DAY)
        self.assertEqual(sorted(got), ["cred_expired", "cred_expiring"])

    def test_headline_counts_the_nearest_one(self):
        r = todo.Report()
        todo.collect_expiring(r, self.rows(6 * DAY, 2 * DAY), now=self.NOW)
        got = r.items[0]
        self.assertEqual(got.count, 2)
        self.assertIn("还有 2 天", got.title)
        self.assertIn("u1", got.what, "最早到期那张的归属要写出来")

    def test_missing_expiry_is_ignored_rather_than_treated_as_1970(self):
        # `expires_at_ts` 缺失 → 0。当成「1970 年就过期了」会让每张没期限的单子
        # 都被报成已过期
        r = todo.Report()
        todo.collect_expiring(r, [{"who": "x"}, {"expires_at_ts": None}], now=self.NOW)
        self.assertEqual(r.items, [])


class RosterTests(unittest.TestCase):
    def test_pending_links_and_unlinked_are_two_rows(self):
        r = todo.Report()
        todo.collect_roster(r, 2, 3)
        self.assertEqual([i.kind for i in r.items], ["mapping_review", "unlinked_account"])
        self.assertEqual([i.count for i in r.items], [2, 3])
        for got in r.items:
            self.assertTrue(got.batch, "两类都是一次确认一批")
            self.assertEqual(got.group, todo.NORMAL)

    def test_filtered_count_is_spelled_out(self):
        """滤掉了几个程序号要说出来 —— 否则人员页和体检页的数字对不上，
        而页面上没有任何地方解释为什么（AC-5 的下半句）。"""
        r = todo.Report()
        todo.collect_roster(r, 0, 3, 7)
        self.assertIn("另有 7 个", r.items[0].title)

    def test_no_filtered_no_parenthesis(self):
        r = todo.Report()
        todo.collect_roster(r, 0, 3, 0)
        self.assertNotIn("另有", r.items[0].title)

    def test_zeros_add_nothing(self):
        r = todo.Report()
        todo.collect_roster(r, 0, 0, 5)
        self.assertEqual(r.items, [])


class IamFilesTests(unittest.TestCase):
    def preview(self, *, set_=0, remove=0, skip=0, pending=()):
        return {
            "increment": {"counts": {"set": set_, "remove": remove, "skip": skip}},
            "baseline": {"pending": list(pending)},
        }

    def test_set_and_remove_add_up_skip_does_not(self):
        """skip 是「这一行没变」，把它算进「还没发的变化」会凭空造出待办。"""
        r = todo.Report()
        todo.collect_iam_files(r, self.preview(set_=2, remove=1, skip=40))
        self.assertEqual(r.items[0].count, 3)
        self.assertIn("3 行", r.items[0].title)

    def test_removes_are_called_out_because_they_are_the_ones_that_get_dropped(self):
        r = todo.Report()
        todo.collect_iam_files(r, self.preview(set_=1, remove=2))
        self.assertIn("2 行是删号", r.items[0].title)

    def test_no_changes_no_row(self):
        r = todo.Report()
        todo.collect_iam_files(r, self.preview(skip=99))
        todo.collect_iam_files(r, {})
        self.assertEqual(r.items, [])

    def test_pending_older_than_a_week_is_chased(self):
        r = todo.Report()
        todo.collect_iam_files(
            r, self.preview(pending=[{"created_at": iso(time.time() - 9 * DAY)}])
        )
        self.assertEqual([i.kind for i in r.items], ["iam_stale_pending"])
        self.assertIn("基线不动", r.items[0].what)

    def test_recent_pending_is_left_alone(self):
        # 刚发出去两天就催 IT，等于教人忽略这一页
        r = todo.Report()
        old = iso(time.time() - 2 * DAY)
        todo.collect_iam_files(r, self.preview(pending=[{"created_at": old}]))
        self.assertEqual(r.items, [])

    def test_pending_without_a_readable_time_is_skipped(self):
        # 认不出时间 → 不催。这里和 stale() 的 fail-safe 方向相反，是有意的：
        # 催错人的代价是噪音，而这一类本来就不紧急
        r = todo.Report()
        todo.collect_iam_files(r, self.preview(pending=[{"created_at": "谁知道"}, {}]))
        self.assertEqual(r.items, [])

    def test_pending_falls_back_to_the_at_field(self):
        r = todo.Report()
        todo.collect_iam_files(r, self.preview(pending=[{"at": iso(time.time() - 30 * DAY)}]))
        self.assertEqual(len(r.items), 1)


class KeyTests(unittest.TestCase):
    def test_everything_collapses_into_one_chore_row(self):
        """十几条密钥提醒会把真正要紧的事挤出屏幕 —— 所以只出一行。"""
        r = todo.Report()
        todo.collect_keys(r, 12, 5)
        self.assertEqual(len(r.items), 1)
        got = r.items[0]
        self.assertEqual(got.group, todo.CHORE)
        self.assertEqual(got.count, 17)
        self.assertIn("12 把该换了", got.title)
        self.assertIn("5 把没人用", got.title)

    def test_only_the_non_zero_half_is_mentioned(self):
        r = todo.Report()
        todo.collect_keys(r, 3, 0)
        self.assertIn("3 把该换了", r.items[0].title)
        self.assertNotIn("没人用", r.items[0].title)

    def test_clean_means_no_row(self):
        r = todo.Report()
        todo.collect_keys(r, 0, 0)
        self.assertEqual(r.items, [])

    def test_it_tells_you_not_to_rotate_for_people(self):
        # 代人换密钥会断服务。这句必须留在文案里
        r = todo.Report()
        todo.collect_keys(r, 1, 0)
        self.assertIn("别代劳", r.items[0].what)


class _Check:
    def __init__(self, level, title="标题", detail="细节"):
        self.level = level
        self.title = title
        self.detail = detail


class ConfigTests(unittest.TestCase):
    def test_only_crit_gets_in(self):
        """warn 级不进待办：系统状态页上一堆黄的是常态，全搬过来这一页就没法看了。"""
        r = todo.Report()
        todo.collect_config(r, [_Check("warn"), _Check("ok"), _Check("crit", "缺凭证")])
        self.assertEqual([i.title for i in r.items], ["缺凭证"])
        self.assertEqual(r.items[0].group, todo.URGENT)

    def test_at_most_three(self):
        # 配置全没配好的新部署会出几十条 crit，全列出来会把别的类顶掉
        r = todo.Report()
        todo.collect_config(r, [_Check("crit", f"第{i}条") for i in range(9)])
        self.assertEqual(len(r.items), 3)

    def test_none_and_empty_are_fine(self):
        r = todo.Report()
        todo.collect_config(r, None)
        todo.collect_config(r, [])
        self.assertEqual(r.items, [])

    def test_objects_without_the_expected_attributes_do_not_crash(self):
        # checks 是别的模块的数据结构，字段改名不该让待办页整页挂掉
        r = todo.Report()
        todo.collect_config(r, [_Check("crit", title="", detail="")])
        self.assertEqual(r.items[0].title, "配置缺失")
        self.assertIn("发不出去", r.items[0].what)


class FreshnessTests(unittest.TestCase):
    def test_sources_are_reported_with_their_verdict(self):
        r = todo.Report()
        r.note_source("权限快照", "2026-09-01T00:00:00+08:00", stale=True, note="采集任务挂了")
        got = r.view()["freshness"]["权限快照"]
        self.assertEqual(
            got, {"at": "2026-09-01T00:00:00+08:00", "stale": True, "note": "采集任务挂了"}
        )

    def test_a_source_with_no_timestamp_is_still_listed(self):
        """「这次没采到」必须出现在页面上。不列的话，「没查」和「查了没问题」长得一模一样。"""
        r = todo.Report()
        r.note_source("权限快照", "", stale=True)
        self.assertIn("权限快照", r.view()["freshness"])


if __name__ == "__main__":
    unittest.main()
