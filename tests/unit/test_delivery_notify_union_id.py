"""只有 `union_id` 的申请人也要收得到消息（`notify.FeishuNotifier.__call__`）。

线上两张到期凭证栽在这儿：老单子和从别处同步过来的单子常常只有 `union_id`，
而原先这里**静默跳过** —— 调用方拿不到异常，于是
`Flows.remind_expiring` 照常记下 `expiry_reminded`，单子上写着「已提醒」，人根本没收到。

所以这一层有两条不变量：
  1. 有 `union_id` 就发得出去（退回 `union_id` 这种收件人类型）；
  2. **一个飞书标识都没有时抛异常，不是返回** —— 「发不出去」必须让调用方知道，
     否则台账会记成已通知。漏报一次通知的代价，远小于让台账说谎。

第 2 条改的是 `__call__` 的契约：以前它从不抛，现在会抛。
所有调用点都得能接住，所以这里也把「调用方接得住」一起锁上。

数据虚构，飞书接口全部替换。
"""

from __future__ import annotations

import json
import unittest

from delivery import notify as n

from . import test_delivery_access_requests as base
from .test_delivery_notify import BASE, _ticket

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

NOW = 1_769_911_200.0 - 7 * 86400  # 到期前 7 天整


class Sent:
    """记下每一次真发出去的请求（收件人类型 + 收件人 + 卡片）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, method, url, token, payload):
        self.calls.append((url.rsplit("=", 1)[-1], payload["receive_id"], payload["content"]))
        return {"code": 0}

    @property
    def kinds(self):
        return [k for k, _, _ in self.calls]

    def card(self, index=0):
        return json.loads(self.calls[index][2])

    def lines(self, index=0):
        card = self.card(index)
        return [el["text"]["content"] for el in card["elements"] if el.get("tag") == "div"]


def notifier(sent, *, clock=None):
    kw = {"transport": sent}
    if clock is not None:
        kw["clock"] = clock
    return n.FeishuNotifier(lambda: "tok", BASE, **kw)


class RoutingTests(unittest.TestCase):
    def test_open_id_is_preferred(self):
        sent = Sent()
        notifier(sent)("done", _ticket(applicant={"open_id": "ou_1", "union_id": "on_1"}))
        self.assertEqual(sent.kinds, ["open_id"])
        self.assertEqual(sent.calls[0][1], "ou_1")

    def test_user_id_is_used_when_there_is_no_open_id(self):
        sent = Sent()
        notifier(sent)("done", _ticket(applicant={"user_id": "u_1", "union_id": "on_1"}))
        self.assertEqual(sent.kinds, ["user_id"])
        self.assertEqual(sent.calls[0][1], "u_1")

    def test_union_id_is_the_last_resort(self):
        """三者的优先级要固定。飘的话，同一个人会在不同事件里收到两条一模一样的卡
        （飞书按收件人类型分会话），而他没法知道这是同一件事。"""
        sent = Sent()
        notifier(sent)("done", _ticket(applicant={"union_id": "on_1"}))
        self.assertEqual(sent.kinds, ["union_id"])
        self.assertEqual(sent.calls[0][1], "on_1")

    def test_blank_strings_do_not_count_as_an_identity(self):
        """同步过来的单子里常常是空串而不是缺字段。当成有值的话，
        飞书会回一个 400，而那时异常文本里没有任何线索说是「收件人是空的」。"""
        sent = Sent()
        notifier(sent)("done", _ticket(applicant={"open_id": "", "user_id": "", "union_id": "on"}))
        self.assertEqual(sent.kinds, ["union_id"])

    def test_no_identity_at_all_raises(self):
        sent = Sent()
        with self.assertRaises(n.NotifyError):
            notifier(sent)("done", _ticket(applicant={"email": "x@wuji.tech"}))
        self.assertEqual(sent.calls, [], "发都没发，别记成已通知")

    def test_an_empty_applicant_raises_too(self):
        sent = Sent()
        for applicant in ({}, None, {"name": "李四"}):
            with self.assertRaises(n.NotifyError):
                notifier(sent)("done", _ticket(applicant=applicant))


class CallerContractTests(unittest.TestCase):
    """`__call__` 现在会抛。调用方要接得住，而且要**记成失败**。"""

    def test_the_flow_records_a_failure_instead_of_claiming_success(self):
        """这正是线上那两张单的场景：没有飞书标识 → 以前记「已提醒」，
        现在要记「没发出去」。记错的代价是：人没收到，而台账说他收到了。"""
        from unittest import mock

        from .test_delivery_access_requests import Harness, MemberExecutor

        h = Harness()
        h.executor = MemberExecutor()
        h.flows._notify = notifier(Sent())
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 30})
        h.approve(ticket)
        h.flows.sync(ticket["id"], force=True)
        # 把申请人的飞书标识抹掉，模拟从别处同步进来的老单子。
        # **直接改文件**：`applicant` 是 TicketStore 明令不许改的字段（tickets.py:215），
        # 而线上那两张单的数据就是这样进来的 —— 不是面板写出来的
        path = h.dir / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["tickets"]:
            row["applicant"] = {"name": "李四", "email": "li.si@wuji.tech"}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        h.now[0] += 28 * 86400
        with mock.patch("sys.stderr"):
            out = h.flows.remind_expiring(tiers=(7,))
        events = [e["event"] for e in h.store.get(ticket["id"])["events"]]
        self.assertIn("expiry_remind_failed:7", events)
        self.assertTrue(out and "失败" in out[0], out)


class SuppressionTests(unittest.TestCase):
    """管理员重试又失败时，不再重复打扰申请人（管理员群照发）。"""

    FAILED_TWICE = [{"event": "execute_failed"}, {"event": "execute_failed"}]

    def test_a_repeated_failure_is_suppressed_for_open_id(self):
        sent = Sent()
        notifier(sent)("failed", _ticket(applicant={"open_id": "ou_1"}, events=self.FAILED_TWICE))
        self.assertEqual(sent.calls, [])

    def test_a_first_failure_is_not_suppressed(self):
        sent = Sent()
        notifier(sent)(
            "failed",
            _ticket(applicant={"open_id": "ou_1"}, events=[{"event": "execute_failed"}]),
        )
        self.assertEqual(sent.kinds, ["open_id"])

    def test_a_repeated_failure_is_suppressed_for_union_id_too(self):
        """**缺陷**：退回 `union_id` 那条分支在 `src/delivery/notify.py:850-856`
        里发完就 `return`，**走在** `:861` 那道「重试又失败就不再打扰」的闸**之前**。

        于是同一张单每被管理员重试失败一次，只有 `union_id` 的申请人就多收到一张
        「开通失败」卡；有 `open_id` 的人一张都不会多收。而
        `union_id` 恰恰是老单子和外部同步单的常态 —— 也就是最可能被反复重试的那批。

        期望：两条路径对同一张单给出同样的结论（重试失败不重复发）。
        实际：`open_id` 发 0 次，`union_id` 发 1 次。
        修法：把 `union_id` 并进底下那条收件人选择里（`send(...)` 之前先过
        失败抑制和 `now=self._clock()`），而不是提前 return 一条独立路径。
        """
        sent = Sent()
        notifier(sent)("failed", _ticket(applicant={"union_id": "on_1"}, events=self.FAILED_TWICE))
        self.assertEqual(sent.calls, [])


class ClockTests(unittest.TestCase):
    """卡片上那句「还有几天到期」按哪个时钟算。"""

    def test_the_open_id_path_uses_the_injected_clock(self):
        sent = Sent()
        notifier(sent, clock=lambda: NOW)("expiring", _ticket(applicant={"open_id": "ou_1"}))
        self.assertIn("7 天后", " ".join(sent.lines()))

    def test_the_union_id_path_uses_the_injected_clock_too(self):
        """**缺陷（今天只是隐患）**：`src/delivery/notify.py:853` 那条
        `build_card(event, ticket, base_url=self.base_url)` 漏了 `now=self._clock()`，
        而 `open_id` 那条（`:864`）带着。

        线上暂时看不出来：`FeishuNotifier` 的默认 `clock` 就是 `time.time`，
        而 `message()` 在 `now=None` 时也回落到 `time.time`。
        但这是「两条路径本该同一份卡片」里唯一不一致的一处 —— 哪天给通知接上
        统一时钟（补发历史通知、定时任务用固定 now、测试里冻结时间），
        只有 `union_id` 的人会收到一张写着错误天数的卡，而且没人会想到去查这里。

        期望：两条路径算出同一句话。
        实际：`open_id` 说「7 天后」，`union_id` 按墙上时钟算，说的是另一个数。
        修法：和上面那条缺陷同一个修法 —— 别让 `union_id` 走独立分支。
        """
        sent = Sent()
        notifier(sent, clock=lambda: NOW)("expiring", _ticket(applicant={"union_id": "on_1"}))
        self.assertIn("7 天后", " ".join(sent.lines()))

    def test_both_paths_render_the_same_card_apart_from_the_clock(self):
        """卡片内容本身（标题、按钮、申请单号）两条路径必须一样 ——
        不一样的话，同一件事在两个人那里长得不同，而没有任何地方说明为什么。"""
        a, b = Sent(), Sent()
        notifier(a)("done", _ticket(applicant={"open_id": "ou_1"}))
        notifier(b)("done", _ticket(applicant={"union_id": "on_1"}))
        self.assertEqual(a.card(), b.card())


if __name__ == "__main__":
    unittest.main()
