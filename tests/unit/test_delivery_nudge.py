"""提醒本人：面板 + 飞书双发。

重点在**节流的边界**和**先发后记的顺序** —— 这两处错了，表现都是「以为通知过了，其实没有」。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from delivery import nudge
from delivery.errors import DeliveryError


class FakeNotifier:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, receive_id, card, *, id_type="open_id", by_user_id=False):
        if self.fail:
            raise RuntimeError("飞书炸了")
        self.sent.append((receive_id, id_type, card))


class NudgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "identity").mkdir()
        self.people = str(self.root / "identity" / "people.json")
        self._cwd = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)

    def go(self, notifier, **kw):
        return nudge.send(
            notifier,
            people_path=self.people,
            union_id="on_a",
            topic="rotate",
            subject="aliyun/100/zhangsan",
            why="AK 建于 2025-01-01",
            **kw,
        )

    def test_it_goes_out_by_union_id(self):
        """名册里只有 union_id，没有 open_id。"""
        n = FakeNotifier()
        self.go(n)
        self.assertEqual(n.sent[0][0], "on_a")
        self.assertEqual(n.sent[0][1], "union_id")

    def test_the_card_leads_with_what_to_do(self):
        """人先看到的是第一行。「你的密钥建了 300 天」看完不知道该干嘛。"""
        card = nudge.card("rotate", subject="s", why="w", detail="")
        first = json.dumps(card["elements"][0], ensure_ascii=False)
        self.assertIn("建一把新的", first)
        self.assertIn("再停用旧的", first)

    def test_the_same_thing_twice_is_throttled(self):
        n = FakeNotifier()
        self.go(n)
        with self.assertRaises(DeliveryError) as caught:
            self.go(n)
        self.assertEqual(len(n.sent), 1)
        self.assertIn("天内不重复发", str(caught.exception))

    def test_two_different_keys_of_the_same_person_are_two_things(self):
        """同一个人的两把密钥是两件事。节流不该把第二把一起挡掉。"""
        n = FakeNotifier()
        self.go(n, ref="AAAA1111")
        self.go(n, ref="BBBB2222")
        self.assertEqual(len(n.sent), 2)

    def test_a_changing_detail_does_not_reopen_the_throttle(self):
        """detail 里写的是「最近用过 X」这种会变的文本。

        拿它当身份的话，这个人每用一次密钥键就变一次，14 天的窗口永远不生效 ——
        表现是每天都收到同一条提醒。
        """
        n = FakeNotifier()
        self.go(n, ref="AAAA1111", detail="最近用过 2026-09-20")
        with self.assertRaises(DeliveryError):
            self.go(n, ref="AAAA1111", detail="最近用过 2026-09-21")
        self.assertEqual(len(n.sent), 1)

    def test_throttling_expires(self):
        n = FakeNotifier()
        self.go(n, now=1_000_000.0)
        self.go(n, now=1_000_000.0 + (nudge.THROTTLE_DAYS + 1) * 86400)
        self.assertEqual(len(n.sent), 2)

    def test_force_skips_the_throttle_but_still_logs(self):
        n = FakeNotifier()
        logged = []
        self.go(n, log=logged.append)
        self.go(n, force=True, log=logged.append)
        self.assertEqual(len(n.sent), 2)
        self.assertEqual([x["op"] for x in logged], ["nudge", "nudge"])

    def test_a_failed_send_does_not_start_the_throttle(self):
        """先记后发的话，发失败了节流还是生效了 —— 这个人接下来两周都收不到提醒，
        而没有任何地方显示「其实没发出去」。"""
        bad = FakeNotifier(fail=True)
        with self.assertRaises(RuntimeError):
            self.go(bad)
        good = FakeNotifier()
        self.go(good)  # 不该被挡
        self.assertEqual(len(good.sent), 1)

    def test_a_missing_union_id_is_refused_outright(self):
        with self.assertRaises(DeliveryError):
            nudge.send(
                FakeNotifier(),
                people_path=self.people,
                union_id="",
                topic="rotate",
                subject="s",
                why="w",
            )

    def test_an_unknown_topic_is_refused(self):
        with self.assertRaises(DeliveryError):
            nudge.send(
                FakeNotifier(),
                people_path=self.people,
                union_id="on_a",
                topic="做点什么",
                subject="s",
                why="w",
            )

    def test_a_non_https_base_url_produces_no_button(self):
        card = nudge.card("rotate", subject="s", why="w", detail="", base_url="http://evil")
        self.assertNotIn("action", [e.get("tag") for e in card["elements"]])

    def test_every_topic_renders(self):
        """加了新 topic 忘了写文案的话，这条会先炸。"""
        for topic in nudge.TOPICS:
            card = nudge.card(topic, subject="s", why="w", detail="d")
            self.assertTrue(card["header"]["title"]["content"])
            self.assertTrue(card["elements"])


if __name__ == "__main__":
    unittest.main()
