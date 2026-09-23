"""到期提醒发失败之后到底会不会重试（`Flows.remind_expiring` 的失败半边）。

`_reminded()` 会把失败的那一档**放回去**（「记了几次」减「失败几次」），
注释和单子上的文案都写着「下一轮会再试」。这个文件盯的是：**放回去之后真的发了吗**。

为什么值得单独一个文件
──────────────────────
1 天档那次是最后一次来得及补救的通知。它在一次飞书抖动里丢掉，表现是
「单子上写着已提醒、人没收到、权限到点断了」—— 而排查的人看到台账上的
`expiry_reminded:1` 会直接认定「提醒过了，是他自己没看」。

这里有两条**方向相反**的要求，必须同时成立，所以两边都锁：

- **永久性失败**（申请人压根没有飞书标识）只记一次。定时任务是 1 分钟一轮，
  每轮追加事件的话一张单子 7 天能攒出两万条。
- **真·偶发失败**（飞书抖一下）必须继续重试，直到发出去为止。

两者在代码里只靠「上一条失败事件的原因是不是同一句」区分 —— 这个判据本身
是不是够用，见 `TransientTests`。

飞书全部替换，时钟可控，不碰网络。
"""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from delivery import notify as n

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import Harness, MemberExecutor

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

DAY = 86400


class Base(unittest.TestCase):
    def harness(self):
        h = Harness()
        h.executor = MemberExecutor()
        h.rec = n.RecordingNotifier()
        h.flows._notify = h.rec
        return h

    def granted(self, h, days=30):
        ticket = h.submit(payload={"cloud_user": "lisi", "days": days})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        return ticket["id"]

    def events(self, h, tid):
        return [e["event"] for e in h.store.get(tid)["events"]]

    def notes(self, h, tid, prefix="expiry_remind"):
        return [
            e.get("note") for e in h.store.get(tid)["events"] if str(e["event"]).startswith(prefix)
        ]

    def break_feishu(self, h, reason="feishu down"):
        def fail(event, ticket):
            raise RuntimeError(reason)

        h.flows._notify = fail

    def sweep(self, h, **kw):
        """跑一轮定时任务。失败路径会往 stderr 打日志，这里一并吞掉。"""
        with mock.patch("sys.stderr"):
            return h.flows.remind_expiring(**kw)

    def count(self, h, tid, event):
        return self.events(h, tid).count(event)

    @contextlib.contextmanager
    def racing(self, h, snapshot):
        """让这一段里的 `store.all()` 返回 `snapshot`，`update()` 照旧打到真存储。

        这正是两个 sweep 并发时**输家**看到的景象：它是在对方写标记之前读的单子，
        所以过得了「提醒过了吗」那道门，等自己写完标记才发现晚了一步。
        真起线程去撞这个窗口是不可复现的，而这里要锁的恰恰是撞上之后的记账。
        """

        class _Stale:
            def __init__(self, real):
                self._real = real

            def all(self):
                return snapshot

            def __getattr__(self, name):
                return getattr(self._real, name)

        real = h.flows.store
        h.flows.store = _Stale(real)
        try:
            yield
        finally:
            h.flows.store = real


class MarkerAccountingTests(Base):
    """`_reminded()` 的算术：记了几次减失败几次。"""

    def test_a_failed_tier_is_released(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h)
        h.now[0] += 25 * DAY
        with mock.patch("sys.stderr"):
            h.flows.remind_expiring(tiers=(7,))
        self.assertEqual(h.flows._reminded(h.store.get(tid)), set(), "失败的那一档要放回去")

    def test_a_successful_tier_is_held(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 25 * DAY
        h.flows.remind_expiring(tiers=(7,))
        self.assertEqual(h.flows._reminded(h.store.get(tid)), {7})

    def test_a_success_after_a_failure_holds_the_tier_again(self):
        """失败一次再成功一次 = 已提醒。两次都算进去的话这一档会一直开着，
        每轮定时任务都再发一条。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        ticket = h.store.get(tid)
        ticket = dict(ticket)
        ticket["events"] = [
            {"event": "expiry_reminded:7"},
            {"event": "expiry_remind_failed:7"},
            {"event": "expiry_reminded:7"},
        ]
        self.assertEqual(h.flows._reminded(ticket), {7})


class RetryTests(Base):
    def test_a_failed_tier_is_actually_retried_next_round(self):
        """回归（曾是缺陷）：抖一下之后的那一轮真的把提醒补发出去。

        那道防并发的闸（先记 `expiry_reminded:<档>`，再数这个事件几条，`>1` 就认定
        另一个定时任务在发）原先数的是**原始条数**，和重试正好打架：
          第 1 轮 发失败 → 事件 = [reminded:7, remind_failed:7]
          第 2 轮 `_reminded` 放行（1 记 − 1 失败 = 0）→ 又写一条 reminded:7
                  → 计数 2 > 1 → continue，通知一次都没发
          第 3 轮 `_reminded` = {7}（2 记 − 1 失败 > 0）→ 永远不再试
        净效果比不修更糟：人没收到，台账上却多了**第二条**「已提醒申请人」。
        现在那道闸和 `_reminded` 用同一套算术（记数减失败数）。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h)
        h.now[0] += 25 * DAY
        self.sweep(h, tiers=(7,))
        h.flows._notify = h.rec  # 飞书恢复了
        self.assertEqual(len(h.flows.remind_expiring(tiers=(7,))), 1)
        self.assertEqual(h.rec.events(), ["done", "expiring"])
        self.assertIn("expiry_reminded:7", self.events(h, tid))

    def test_the_other_tier_still_works_after_a_failure(self):
        """一档失败不该毒到另一档 —— 这条今天是好的，锁住别退化。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h)
        h.now[0] += 25 * DAY
        with mock.patch("sys.stderr"):
            h.flows.remind_expiring(tiers=(7,))
        h.flows._notify = h.rec
        h.now[0] += 4.5 * DAY
        self.assertEqual(len(h.flows.remind_expiring(tiers=(1,))), 1)
        self.assertIn("expiry_reminded:1", self.events(h, tid))


class NoteTests(Base):
    """台账上那句话要**照实说剩多久**，不能照档位说。"""

    def test_the_note_states_the_real_remaining_time_not_the_tier(self):
        """剩 12 小时的单子在 7 天档被提醒时，台账写「还有 7 天」的话，
        以后查「为什么没提醒到」会被这句话直接带偏。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 29.5 * DAY  # 还有半天，但 7 天档的窗口也命中
        h.flows.remind_expiring(tiers=(7,))
        said = self.notes(h, tid)
        self.assertEqual(len(said), 1)
        self.assertIn("小时", said[0])
        self.assertNotIn("7 天", said[0])

    def test_a_whole_number_of_days_is_stated_in_days(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 25 * DAY  # 还有 5 天
        h.flows.remind_expiring(tiers=(7,))
        self.assertIn("5 天", self.notes(h, tid)[0])

    def test_the_failure_note_carries_the_real_time_left_and_the_reason(self):
        """失败那条台账是运维读到的唯一线索，两样东西都得在里面：

        - **还剩多久**（不是档位数字）：决定这事今晚要不要有人管，还是明天再说；
        - **为什么没发出去**：区分「这个人没有飞书账号」和「飞书挂了」，
          前者要去补人事信息，后者等一轮就自己好了。

        原先这句话写的是固定的「到期提醒没有发出去」，两样都没有。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h, reason="no feishu identity")
        h.now[0] += 25 * DAY  # 还有 5 天
        self.sweep(h, tiers=(7,))
        failed = [
            e["note"] for e in h.store.get(tid)["events"] if e["event"] == "expiry_remind_failed:7"
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("还有 5 天", failed[0])
        self.assertNotIn("7 天", failed[0])
        self.assertIn("no feishu identity", failed[0])

    def test_the_failure_note_states_hours_when_only_hours_are_left(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h)
        h.now[0] += 29.5 * DAY  # 还有 12 小时
        self.sweep(h, tiers=(1,))
        failed = [
            e["note"] for e in h.store.get(tid)["events"] if e["event"] == "expiry_remind_failed:1"
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("还有 12 小时", failed[0])


class PermanentFailureTests(Base):
    """永久性失败只记一次。

    线上原型：申请人在通讯录里查无此人（离职、外包、邮箱写错），飞书那边
    **永远**发不出去。定时任务 `OnUnitActiveSec=1min`，原先每轮往单子上追加
    两条事件 —— 一张单子 7 天攒了两万条，单子页面直接打不开。
    """

    def test_five_rounds_of_the_same_permanent_failure_record_one_event(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h, reason="no feishu identity")
        h.now[0] += 25 * DAY
        for _ in range(5):
            self.sweep(h, tiers=(7,))
        self.assertEqual(self.count(h, tid, "expiry_remind_failed:7"), 1, "同一个原因只记一次")
        self.assertEqual(h.rec.events(), ["done"], "一张卡都没发出去")

    def test_the_ticket_stops_growing_after_the_failure_is_recorded(self):
        """真正要挡的是**事件表无限增长**，不是某一个事件名的条数。

        只数 `expiry_remind_failed` 的话，换个事件名每轮照样追加，这条测试还是绿的。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h, reason="no feishu identity")
        h.now[0] += 25 * DAY
        self.sweep(h, tiers=(7,))
        self.sweep(h, tiers=(7,))
        settled = len(self.events(h, tid))
        for _ in range(5):
            self.sweep(h, tiers=(7,))
        self.assertEqual(len(self.events(h, tid)), settled, "记过之后每轮都不该再写")

    def test_a_different_reason_is_still_worth_recording(self):
        """换了错误内容就再记一条：那是新信息。

        「同一个原因」是这里唯一的判据，宽到把不同原因也吞掉的话，
        单子上会只剩第一次那个已经过时的错误。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h, reason="no feishu identity")
        h.now[0] += 25 * DAY
        self.sweep(h, tiers=(7,))
        self.break_feishu(h, reason="feishu 500")
        self.sweep(h, tiers=(7,))
        notes = self.notes(h, tid, prefix="expiry_remind_failed")
        self.assertEqual(len(notes), 2)
        self.assertIn("feishu 500", notes[1])


class TransientTests(Base):
    """偶发失败必须继续重试 —— 这是上面那条「只记一次」的反方向。"""

    def test_a_one_round_blip_is_retried_and_eventually_delivered(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h)
        h.now[0] += 25 * DAY
        self.sweep(h, tiers=(7,))
        h.flows._notify = h.rec  # 飞书恢复了
        out = h.flows.remind_expiring(tiers=(7,))
        self.assertEqual(len(out), 1)
        self.assertEqual(h.rec.events(), ["done", "expiring"], "补发出去了")
        self.assertEqual(h.flows._reminded(h.store.get(tid)), {7}, "发到了就别再发")

    def test_an_outage_lasting_two_rounds_is_still_delivered_after_recovery(self):
        """回归（曾是缺陷）：飞书连挂两轮（≈2 分钟）之后，这一档永远发不出去了。

        判据是「上一条失败事件的原因是不是同一句」，而飞书挂着的时候每一轮的
        错误原因当然一模一样 —— 所以第 2 轮就被判成「永久性失败」。
        坏就坏在那一轮**已经先写了 `expiry_reminded:7`**（防并发的标记），
        走进抑制分支后直接 `continue`，没有写配对的 `expiry_remind_failed:7`：

          第 1 轮 失败 → [reminded:7, failed:7]      → 记 1 失 1，放回去 ✓
          第 2 轮 失败 → [reminded:7, failed:7, reminded:7] → 记 2 失 1 → **{7}**
          第 3 轮起    → `_reminded` 说提醒过了，连试都不试；汇总行也不再出现这张单

        `src/delivery/flows.py:1687`（先写标记）+ `:1722-1731`（抑制分支只 return
        不记账）。定时任务是 1 分钟一轮，所以「连挂两轮」= 任何超过一分钟的抖动。

        修好之前：一张卡都没发，而台账最后一句是「已提醒申请人：还有 5 天到期」——
        正是这次改动要消灭的那种误导。
        修法只有一条路走得通：把「上一轮同因失败」的判断**提到写标记之前** ——
        这一轮不写任何标记，直接试着发，发成了再补标记。
        在抑制分支里补写一条配对的 `expiry_remind_failed:7`（照并发输家那段抄）
        记账是对了，但每轮又变成追加两条事件，`PermanentFailureTests::
        test_the_ticket_stops_growing_after_the_failure_is_recorded` 会挡住 ——
        那正是这次改动要修的第三个缺陷。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        self.break_feishu(h)
        h.now[0] += 25 * DAY
        self.sweep(h, tiers=(7,))
        self.sweep(h, tiers=(7,))
        h.flows._notify = h.rec  # 飞书恢复了
        h.flows.remind_expiring(tiers=(7,))
        self.assertIn("expiring", h.rec.events(), "飞书好了就该把这一档补发出去")
        # 台账按净额读：记了 2 条「已提醒」、1 条「没发出去」，净 1 条真的送达。
        # 读起来是「已提醒 → 没发出去 → 已提醒（重试成功）」，每一步都发生过，不是重复记账
        net = self.count(h, tid, "expiry_reminded:7") - self.count(h, tid, "expiry_remind_failed:7")
        self.assertEqual(net, 1)
        self.assertEqual(h.rec.events().count("expiring"), 1, "只该真正发出去一次")


class RaceTests(Base):
    """两个 sweep 撞在一起：输家要把自己写的那条标记**抵消掉**。

    不抵消的话，「记数」多出一条而没有对应的「失败数」，这一档就被算成已提醒 ——
    可实际上输家一张卡都没发。赢家同时也失败的时候，这一档就此作废、再也不重试。
    """

    def race(self, h, stale, outcome=None):
        """赢家发送到一半时，输家那一轮插进来。

        顺序是真实的：赢家写完标记 → 开始发 → 输家（拿着更早的快照）也写了标记
        → 输家发现记数 2 > 1 → 抵消 → 赢家这边的发送才有结果。
        """
        ran = []

        def winner(event, ticket):
            if event == "expiring" and not ran:
                ran.append(event)
                with self.racing(h, stale):
                    h.flows.remind_expiring(tiers=(7,))
            if outcome is not None:
                outcome()
            h.rec(event, ticket)

        h.flows._notify = winner

    def test_the_loser_compensates_and_only_one_card_goes_out(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 25 * DAY
        stale = h.store.all()  # 两个 sweep 都在对方写标记之前读到的单子
        self.race(h, stale)
        out = h.flows.remind_expiring(tiers=(7,))
        self.assertEqual(len(out), 1, "只有赢家报「已提醒」")
        self.assertEqual(h.rec.events(), ["done", "expiring"], "申请人只收到一张卡")
        self.assertEqual(self.count(h, tid, "expiry_reminded:7"), 2, "两边都写了标记")
        self.assertEqual(self.count(h, tid, "expiry_remind_failed:7"), 1, "输家把自己那条抵消掉")
        self.assertEqual(h.flows._reminded(h.store.get(tid)), {7}, "赢家发到了：这一档就此打住")
        h.flows._notify = h.rec
        self.assertEqual(h.flows.remind_expiring(tiers=(7,)), [], "下一轮不该再发一遍")

    def test_when_the_winner_also_fails_the_tier_goes_back_for_retry(self):
        """抵消那一条的价值全在这儿：赢家也没发出去时，两条标记两条失败**刚好抵平**，
        这一档回到「没提醒过」，下一轮重试。

        少了输家那条抵消，记数 2 − 失败 1 = 1 > 0 —— 一次谁都没发出去的提醒
        会被算成已送达，而 1 天档那次是最后一次来得及补救的通知。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 25 * DAY
        stale = h.store.all()

        def boom():
            raise RuntimeError("feishu down")

        self.race(h, stale, outcome=boom)
        with mock.patch("sys.stderr"):
            h.flows.remind_expiring(tiers=(7,))
        self.assertEqual(h.rec.events(), ["done"], "这一轮一张卡都没发出去")
        self.assertEqual(self.count(h, tid, "expiry_reminded:7"), 2)
        self.assertEqual(self.count(h, tid, "expiry_remind_failed:7"), 2, "抵消 + 真失败")
        self.assertEqual(h.flows._reminded(h.store.get(tid)), set(), "记账抵平，回到没提醒过")
        h.flows._notify = h.rec  # 飞书恢复了
        self.assertEqual(len(h.flows.remind_expiring(tiers=(7,))), 1)
        self.assertEqual(h.rec.events(), ["done", "expiring"])

    def test_the_loser_writes_nothing_else_and_reports_nothing(self):
        """输家不报「已提醒」也不报「失败」：汇总行是给人看的，
        它这一轮既没发成也没出错，多一行只会让人以为发了两次。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 25 * DAY
        stale = h.store.all()
        seen = []

        def winner(event, ticket):
            if event == "expiring" and not seen:
                seen.append(event)
                with self.racing(h, stale):
                    seen.append(h.flows.remind_expiring(tiers=(7,)))
            h.rec(event, ticket)

        h.flows._notify = winner
        h.flows.remind_expiring(tiers=(7,))
        self.assertEqual(seen[1], [], "输家这一轮什么都不报")
        compensation = self.notes(h, tid, prefix="expiry_remind_failed")[0]
        self.assertIn("作废", compensation, "抵消那条要写明为什么，别让人当成发送失败")


if __name__ == "__main__":
    unittest.main()
