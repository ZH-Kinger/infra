"""到期提醒分两档（`Flows.remind_expiring`）。

为什么要两档：90 天的服务凭证只提前 3 天喊一声太晚 —— 负责人可能正在休假，
而到期那天断的是服务。所以第一档（7 天）留出换凭证的时间，第二档（1 天）是最后通知。

这一层有两个最容易写坏的地方：

1. **「提醒过了」这个标记的粒度**：只记一个通用标记的话，第一档发完第二档就永远
   发不出去，而最后那次才是真正来得及补救的。所以每档记自己的事件（`expiry_reminded:7`）。
2. **每一档的下界**：档位描述的是一个区间，不是「小于等于几天」。少了下界，一张
   只剩 12 小时的单子会同时落进 7 天档和 1 天档，一轮发出**两张一模一样的卡**
   —— 卡面是按 `expires_at_ts` 算的，两张都写「还有 12 小时」。定时任务停过几天
   再开起来就是这个样子（审计现场复现过）。所以每档的下界 = 下一个更近的档，
   最近的那一档不设下界。

配套的另一条：台账和汇总行里写的是**真实剩余时间**，不是档位数字。查「为什么没
提醒到我」的人读的就是这句话，写「还有 7 天」而实际只剩 12 小时会把人带偏。

飞书全部替换，时钟可控，不碰网络。
"""

from __future__ import annotations

import unittest
from unittest import mock

from delivery import notify as n
from delivery import tickets as t

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
        """开通一张 days 天的权限单，返回单号。"""
        ticket = h.submit(payload={"cloud_user": "lisi", "days": days})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        return ticket["id"]

    def events(self, h, tid):
        return [e["event"] for e in h.store.get(tid)["events"]]

    def reminders(self, h, tid):
        return [e for e in self.events(h, tid) if e.startswith("expiry_reminded")]

    def notes(self, h, tid):
        return [
            e.get("note")
            for e in h.store.get(tid)["events"]
            if str(e["event"]).startswith("expiry_reminded")
        ]

    def at(self, h, tid, left):
        """把时钟拨到「离到期还剩 left 秒」那一刻。

        直接按 `expires_at_ts` 反推，不用 `now += N*DAY` 攒误差 —— 边界用例要的
        就是正好 7d0h、正好 1d0h 这几个点，差一秒结论就反过来。
        """
        h.now[0] = float(h.store.get(tid)["expires_at_ts"]) - left


class TierTests(Base):
    def test_default_is_two_tiers(self):
        self.assertEqual(tuple(sorted(Harness().flows.REMIND_TIERS)), (1, 7))

    def test_each_tier_fires_once_and_records_its_own_marker(self):
        """两档各发一次，各记各的标记 —— 这是整条功能的核心不变量。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        self.assertEqual(h.flows.remind_expiring(), [])  # 还有 30 天，两档都不到

        h.now[0] += 25 * DAY  # 还有 5 天：落进 7 天档
        first = h.flows.remind_expiring()
        self.assertEqual(len(first), 1)
        # 汇总行写**真实剩余时间**，不是档位数字：这张单子是被 7 天档点到的，
        # 但它剩的是 5 天，写「还有 7 天」就是假的
        self.assertIn("还有 5 天", first[0])
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:7"])

        self.assertEqual(h.flows.remind_expiring(), [], "同一天再跑一次不该重复提醒")

        h.now[0] += 4.5 * DAY  # 还有半天：落进 1 天档
        last = h.flows.remind_expiring()
        self.assertEqual(len(last), 1)
        self.assertIn("还有 12 小时", last[0])
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:7", "expiry_reminded:1"])
        self.assertEqual(h.rec.events(), ["done", "expiring", "expiring"])

    def test_the_second_tier_is_not_blocked_by_the_first(self):
        """回归锁：这正是改成分档要修的那个 bug。

        标记不带档位时，第一档发完 `expiry_reminded` 一写，最后那次通知就永远发不出去了。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 25 * DAY
        h.flows.remind_expiring()
        h.now[0] += 4.5 * DAY
        h.flows.remind_expiring()
        self.assertEqual(len(self.reminders(h, tid)), 2)

    def test_an_explicit_days_argument_means_that_single_tier(self):
        # 定时任务之外还有命令行：`--days 3` 要还是老语义，只跑那一档
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 28 * DAY  # 还有 2 天
        self.assertEqual(len(h.flows.remind_expiring(days=3)), 1)
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:3"])

    def test_tiers_argument_wins_over_days(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 28 * DAY
        h.flows.remind_expiring(days=3, tiers=(2,))
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:2"])

    def test_duplicate_tiers_are_collapsed(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 28 * DAY
        h.flows.remind_expiring(tiers=(3, 3, 3))
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:3"])

    def test_only_the_nearest_tier_in_range_fires(self):
        """回归（曾是缺陷）：剩 12 小时时只响 1 天档，7 天档被自己的下界挡在外面。

        档位从宽到窄跑，下界取自下一个更近的档 —— 所以「宽档先跑」这件事现在的
        可观察结果不再是「先发宽的再发窄的」，而是**宽的那一档根本不发**。
        原先没有下界：同一张单子先被 7 天档点一次、再被 1 天档点一次。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 29.5 * DAY  # 还有半天：两档的**上界**都命中
        out = h.flows.remind_expiring()
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:1"], "只该被最近的那一档点到")
        self.assertEqual(len(out), 1)
        self.assertIn("还有 12 小时", out[0])

    def test_a_backlog_sends_exactly_one_card_not_one_per_tier(self):
        """回归（曾是缺陷，审计现场复现）：定时任务停了几天再开起来，
        一张单子**只收到一张卡**。

        原先一轮发两张：卡面是按 `expires_at_ts` 算的，所以两张的正文一模一样，
        收到的人只会觉得机器人坏了。去重不能放在通知那一侧 —— 那时候两次调用
        已经各自带着「已提醒」的台账了。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 29.5 * DAY
        out = h.flows.remind_expiring()
        self.assertEqual(len(out), 1)
        self.assertEqual(h.rec.events(), ["done", "expiring"])
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:1"])
        # 连续跑几轮也不会再补一张（最近那档已提醒过，远档被下界挡着）
        self.assertEqual(h.flows.remind_expiring(), [])
        self.assertEqual(h.rec.events(), ["done", "expiring"])

    def test_three_tiers_each_get_the_next_nearer_one_as_their_floor(self):
        """下界是「下一个更近的档」，不是写死的 1 天：中间那档也有下界。

        自定义档位是命令行才走的路，但下界的算法是同一份 —— 三档时中间那档
        没下界的话，剩 5 天的单子会同时被 30 档和 7 档点到。
        """
        h = self.harness()
        tid = self.granted(h, days=60)
        tiers = (30, 7, 1)
        for left, want, said in (
            (10 * DAY, "expiry_reminded:30", "还有 10 天"),  # 30 档，下界 7 天
            (5 * DAY, "expiry_reminded:7", "还有 5 天"),  # 7 档，下界 1 天
            (12 * 3600, "expiry_reminded:1", "还有 12 小时"),  # 1 档，没有下界
        ):
            self.at(h, tid, left)
            out = h.flows.remind_expiring(tiers=tiers)
            self.assertEqual(len(out), 1, f"剩 {left} 秒时只该有一档响")
            self.assertIn(said, out[0])
            self.assertEqual(self.reminders(h, tid)[-1], want)
        self.assertEqual(len(self.reminders(h, tid)), 3)


class BoundaryTests(Base):
    """档位窗口的四个边界点。

    档位是半开区间 `(now + 下界, now + 档位]`，两端都是「差一秒结论就反过来」的地方：
    上界算错 → 到期前一天才第一次提醒；下界算错 → 同一轮两张一样的卡。
    """

    def fire(self, h, tid, left, tiers=()):
        self.at(h, tid, left)
        return h.flows.remind_expiring(tiers=tiers)

    def test_exactly_seven_days_is_inside_the_wide_tier(self):
        """上界是闭的：正好 7 天整要响，不能等到 6d23h。

        `<` 写成 `<=` 之外的另一种写法（`expires - now < 7d`）会让定时任务
        每天固定时刻跑时**整整错过一天** —— 快到期那几天正好是最要紧的。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        out = self.fire(h, tid, 7 * DAY)
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:7"])
        self.assertIn("还有 7 天", out[0])
        self.assertIn("还有 7 天", self.notes(h, tid)[0])

    def test_one_second_past_the_wide_tier_is_not_reminded_yet(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.assertEqual(self.fire(h, tid, 7 * DAY + 1), [])
        self.assertEqual(self.reminders(h, tid), [])

    def test_six_days_twentythree_hours_says_six_days_not_seven(self):
        """还在 7 天档里，但剩的是 6 天多 —— 台账按**实际**取整到天。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        out = self.fire(h, tid, 6 * DAY + 23 * 3600)
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:7"])
        self.assertIn("还有 6 天", out[0])
        self.assertIn("还有 6 天", self.notes(h, tid)[0])

    def test_exactly_one_day_falls_to_the_near_tier_not_the_wide_one(self):
        """下界是开的：正好 1 天整时 7 天档已经不管了，归 1 天档。

        两边都算进去的话就是那张重复卡；两边都不算就是**谁都不发**，
        而这一天恰恰是最后一次来得及补救的通知。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        out = self.fire(h, tid, DAY)
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:1"])
        self.assertEqual(len(out), 1)
        self.assertIn("还有 1 天", out[0])
        self.assertIn("还有 1 天", self.notes(h, tid)[0])

    def test_twentythree_hours_is_stated_in_hours(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        out = self.fire(h, tid, 23 * 3600)
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:1"])
        self.assertIn("还有 23 小时", out[0])
        self.assertIn("还有 23 小时", self.notes(h, tid)[0])
        self.assertNotIn("天", self.notes(h, tid)[0])

    def test_every_boundary_sends_exactly_one_card(self):
        """四个边界点各自只发一张卡 —— 每个点都换一张新单子，互不干扰。"""
        for left in (7 * DAY, 6 * DAY + 23 * 3600, DAY, 23 * 3600):
            h = self.harness()
            tid = self.granted(h, days=30)
            self.assertEqual(len(self.fire(h, tid, left)), 1, f"剩 {left} 秒")
            self.assertEqual(h.rec.events(), ["done", "expiring"], f"剩 {left} 秒")


class WindowTests(Base):
    def test_expired_tickets_are_left_to_revocation(self):
        """已经过期的不提醒：那时候该做的是回收，不是「你快到期了」。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        h.now[0] += 31 * DAY
        self.assertEqual(h.flows.remind_expiring(), [])
        self.assertEqual(self.reminders(h, tid), [])

    def test_a_short_grant_skips_the_wide_tier_but_still_gets_the_last_call(self):
        """开通时就只给了 2 天：开通消息里已经写了到期时间，7 天档不用再喊一遍。

        但 1 天档要照发 —— 那是最后通知，和开通时那句隔了一整天。
        """
        h = self.harness()
        tid = self.granted(h, days=2)
        h.now[0] += 60
        self.assertEqual(h.flows.remind_expiring(), [], "刚开通不该紧接着提醒")
        h.now[0] += 1.5 * DAY  # 还有半天
        h.flows.remind_expiring()
        self.assertEqual(self.reminders(h, tid), ["expiry_reminded:1"])

    def test_pending_tickets_are_never_reminded(self):
        # 还没开通 = 现在没有权限会断
        h = self.harness()
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 30})
        h.now[0] += 25 * DAY
        self.assertEqual(h.flows.remind_expiring(), [])
        self.assertEqual(self.reminders(h, ticket["id"]), [])


class LegacyTests(Base):
    def legacy(self, h, tid):
        """把一张单子改成「老代码提醒过」的样子：只有不带档位的 `expiry_reminded`。"""
        h.store.update(
            tid,
            actor="system",
            expect=[t.DONE],
            event="expiry_reminded",
            note="老版本记的",
        )

    def test_a_legacy_marker_counts_as_the_three_day_tier(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.legacy(h, tid)
        self.assertEqual(h.flows._reminded(h.store.get(tid)), {3})
        h.now[0] += 28 * DAY  # 还有 2 天，落进 3 天档
        self.assertEqual(h.flows.remind_expiring(days=3), [], "老标记要挡住同一档")

    def test_a_legacy_marker_does_not_block_the_last_call(self):
        """老单子仍然收得到 1 天档那次 —— 那次本来就没发过。"""
        h = self.harness()
        tid = self.granted(h, days=30)
        self.legacy(h, tid)
        h.now[0] += 29.5 * DAY
        h.flows.remind_expiring(tiers=(1,))
        self.assertIn("expiry_reminded:1", self.reminders(h, tid))

    def test_unparsable_tier_suffix_is_ignored_not_fatal(self):
        # 事件名是从存储里读出来的。将来有人手写一条 `expiry_reminded:soon`，
        # 不该让整轮提醒抛异常
        h = self.harness()
        tid = self.granted(h, days=30)
        h.store.update(
            tid, actor="system", expect=[t.DONE], event="expiry_reminded:soon", note="手写的"
        )
        self.assertEqual(h.flows._reminded(h.store.get(tid)), set())
        h.now[0] += 25 * DAY
        self.assertEqual(len(h.flows.remind_expiring()), 1)

    def test_a_legacy_marker_does_not_trigger_a_far_tier_reminder(self):
        """回归（曾是缺陷）：老标记按 3 档算，而默认档位是 (7, 1)。没有下界时，
        一张「还有 2 天到期、上周已提醒过」的单子会再收到一条写着「还有 7 天」的卡 ——
        内容是错的，台账记的也是错的。现在「更近的档提醒过就不补远档」。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        self.legacy(h, tid)
        h.now[0] += 28 * DAY  # 还有 2 天：3 档已提醒过，7 档不该再发
        self.assertEqual(h.flows.remind_expiring(), [])
        h.now[0] += 1.5 * DAY  # 进了 1 档：这一档还没提醒过，该发
        out = h.flows.remind_expiring()
        self.assertEqual(len(out), 1)
        self.assertIn("还有 12 小时", out[0], "说剩多久，不是说这是几天档")


class FailureTests(Base):
    def broken(self, h):
        def fail(event, ticket):
            raise RuntimeError("feishu down")

        h.flows._notify = fail

    def test_send_failure_is_recorded_on_the_ticket(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.broken(h)
        h.now[0] += 25 * DAY
        with mock.patch("sys.stderr"):
            out = h.flows.remind_expiring()
        self.assertEqual(out, [f"{tid}：到期提醒发送失败"])
        self.assertIn("expiry_remind_failed:7", self.events(h, tid))

    def test_a_failed_send_is_retried_next_round(self):
        """回归（曾是缺陷）：发送失败那一档要放回去，下一轮真的补发。

        1 天档那次是最后一次来得及补救的通知，丢在一次飞书抖动里就没了；
        而单子上还留着「已提醒」，排查的人会据此认定「提醒过了，是他自己没看」。
        细节见 test_delivery_remind_retry.py::RetryTests。
        """
        h = self.harness()
        self.granted(h, days=30)
        self.broken(h)
        h.now[0] += 25 * DAY
        with mock.patch("sys.stderr"):
            h.flows.remind_expiring()
        h.flows._notify = h.rec  # 飞书恢复了
        out = h.flows.remind_expiring()
        self.assertEqual(len(out), 1, "失败那一档下一轮要补发")
        self.assertIn("expiring", h.rec.events(), "申请人这次真的收到了")

    def test_failure_of_one_tier_does_not_poison_the_other(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        self.broken(h)
        h.now[0] += 25 * DAY
        with mock.patch("sys.stderr"):
            h.flows.remind_expiring(tiers=(7,))
        h.flows._notify = h.rec
        h.now[0] += 4.5 * DAY
        self.assertEqual(len(h.flows.remind_expiring(tiers=(1,))), 1)
        self.assertIn("expiry_reminded:1", self.reminders(h, tid))

    def test_admin_only_channel_consumes_nothing_in_either_tier(self):
        """只剩管理员群可发时（拿不到飞书令牌）：什么都不做，也**不记事件**。

        记了事件的话，飞书恢复之后这张单子就再也提醒不了了 —— 而这条路径
        恰恰是「飞书出问题」的那一天必经的。
        """
        h = self.harness()
        tid = self.granted(h, days=30)
        admin_only = n.combine(
            n.AdminAlert("https://x", "s", "https://panel.example.com", send=lambda *a, **k: None)
        )
        self.assertFalse(admin_only.reaches_applicant)
        h.flows._notify = admin_only
        h.now[0] += 25 * DAY
        self.assertEqual(h.flows.remind_expiring(), [])
        self.assertEqual(self.reminders(h, tid), [])
        h.flows._notify = h.rec
        self.assertEqual(len(h.flows.remind_expiring()), 1)

    def test_no_notifier_at_all_is_a_no_op(self):
        h = self.harness()
        tid = self.granted(h, days=30)
        h.flows._notify = None
        h.now[0] += 25 * DAY
        self.assertEqual(h.flows.remind_expiring(), [])
        self.assertEqual(self.reminders(h, tid), [])


if __name__ == "__main__":
    unittest.main()
