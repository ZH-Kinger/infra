"""数据迁移的编排。

纯逻辑，一行云都不调。重点在**状态机不会倒着走**和**量不出来时不放行**——
这两条错了，表现分别是「失败的任务看起来又活了」和「100TB 被当成 0 字节直接搬」。
"""

import unittest

from delivery import moves
from delivery.errors import DeliveryError


class PlanTests(unittest.TestCase):
    def test_into_oss_goes_to_the_alibaba_service(self):
        for src in ("oss://a/x/", "tos://a/x/"):
            got = moves.plan(src, "oss://b/y/")
            self.assertEqual(got["engine"], "mgw", src)

    def test_into_tos_goes_to_the_volcano_service(self):
        for src in ("oss://a/x/", "tos://a/x/"):
            self.assertEqual(moves.plan(src, "tos://b/y/")["engine"], "dms", src)

    def test_a_direction_we_cannot_do_is_refused_not_left_undecided(self):
        """返回 None 的分支迟早会被当成「没问题」传下去。"""
        for src, dst in (
            # CPFS 只和阿里 OSS 流动、vePFS 只和火山 TOS 流动 —— 交叉的那几对搬不了
            ("oss://a/x/", "vepfs://b/y/"),
            ("cpfs://a/x/", "tos://b/y/"),
            ("cpfs://a/x/", "vepfs://b/y/"),
            # 两个并行文件系统之间没有直连，要三段（沉降→跨云→预热），面板没接
            ("cpfs://a/x/", "cpfs://b/y/"),
            ("vepfs://a/x/", "vepfs://b/y/"),
        ):
            with self.assertRaises(DeliveryError, msg=f"{src}→{dst}"):
                moves.plan(src, dst)

    def test_preheat_and_sink_pick_the_dataflow_engines(self):
        """**方向由地址推出来，不让人选。** 让人选「这是预热还是沉降」的后果不是报错，
        是把数据往相反方向覆盖一遍 —— 那不可逆。"""
        cases = (
            ("oss://b/p/", "cpfs://bmcpfs-1/d/", "nas", "Import"),
            ("cpfs://bmcpfs-1/d/", "oss://b/p/", "nas", "Export"),
            ("tos://b/p/", "vepfs://vepfs-1/d/", "vepfs", "Import"),
            ("vepfs://vepfs-1/d/", "tos://b/p/", "vepfs", "Export"),
        )
        for src, dst, engine, action in cases:
            got = moves.plan(src, dst)
            self.assertEqual(got["engine"], engine, f"{src}→{dst}")
            self.assertEqual(got["action"], action, f"{src}→{dst}")

    def test_the_filesystem_side_and_the_storage_side_are_told_apart(self):
        """引擎要分别知道「文件系统是哪个」和「对象存储那头是哪个」——
        方向变了这两个不能跟着换位置，否则沉降会被当成预热提上去。"""
        for src, dst in (
            ("oss://b/p/", "cpfs://bmcpfs-1/d/"),
            ("cpfs://bmcpfs-1/d/", "oss://b/p/"),
        ):
            got = moves.plan(src, dst)
            self.assertEqual(got["fs"]["bucket"], "bmcpfs-1", f"{src}→{dst}")
            self.assertEqual(got["store"]["bucket"], "b", f"{src}→{dst}")

    def test_a_path_without_a_bucket_is_refused(self):
        for bad in ("oss://", "oss:///x/", "justtext", ""):
            with self.assertRaises(DeliveryError, msg=bad):
                moves.plan(bad, "oss://b/y/")


class ReviewGateTests(unittest.TestCase):
    def test_a_big_move_needs_a_look(self):
        self.assertTrue(moves.needs_review(2 * 1024**4, known=True))

    def test_a_small_move_does_not(self):
        self.assertFalse(moves.needs_review(1024**3, known=True))

    def test_an_unmeasurable_move_is_treated_as_big(self):
        """bot 那边量不出来返回 (0,0)，于是判定恒为 False ——
        一个 100TB 的任务会被当成 0 字节直接放行。不知道多大必须当大的。"""
        self.assertTrue(moves.needs_review(0, known=False))


class JobNameTests(unittest.TestCase):
    def test_it_comes_from_the_ticket_id(self):
        """迁移服务按名字幂等，所以重试同一张单不会搬第二遍，
        而且云上那个任务名能直接对回台账。"""
        self.assertEqual(moves.job_name("req-a3f2"), "panel-req-a3f2")

    def test_odd_characters_are_stripped(self):
        """任务名会进 URL 路径。`.` 也去掉 —— 留着就有 `..` 的可能。"""
        self.assertEqual(moves.job_name("req/../a b"), "panel-reqab")
        self.assertNotIn("..", moves.job_name("a..b"))

    def test_an_empty_id_is_refused(self):
        with self.assertRaises(DeliveryError):
            moves.job_name("")

    def test_a_retry_gets_a_new_name_so_it_does_not_reuse_the_failed_task(self):
        """两朵云都是「找到同名的就复用」。重试还用原名的话，捞回来的是那个
        已经失败的任务 —— 单子在 NEW → FAILED 之间空转，一次都没重新跑过。"""
        self.assertEqual(moves.job_name("req-1", 1), "panel-req-1")
        self.assertEqual(moves.job_name("req-1", 2), "panel-req-1-r2")
        self.assertNotEqual(moves.job_name("req-1", 2), moves.job_name("req-1", 3))

    def test_a_long_id_is_trimmed_without_eating_the_retry_suffix(self):
        """截整个串的话 `-r2` 会被削掉，于是重试又用回原名。"""
        got = moves.job_name("x" * 200, 2)
        self.assertLessEqual(len(got), 60)
        self.assertTrue(got.endswith("-r2"), got)


class StartTests(unittest.TestCase):
    TICKET = {"id": "req-1", "payload": {"source": "oss://a/x/", "dest": "oss://b/y/"}}

    def test_it_submits_once_and_records_the_job(self):
        seen = []
        out = moves.start(
            self.TICKET, submit=lambda p, n: seen.append((p["engine"], n)), now=1000.0
        )
        self.assertEqual(seen, [("mgw", "panel-req-1")])
        self.assertEqual(out["move_stage"], moves.STAGE_RUNNING)
        self.assertEqual(out["move_job"], "panel-req-1")

    def test_the_handle_the_service_gave_back_is_what_gets_polled(self):
        """火山返回的是数字 task_id，不是任务名。丢掉它、拿任务名去查进度的话，
        得到的是「任务号不是数字」—— 那句话走「查进度失败」分支、不算失败，
        于是所有搬进 TOS 的单子永远停在在途。"""
        out = moves.start(self.TICKET, submit=lambda p, n: 577732)
        self.assertEqual(out["move_ref"], "577732")
        self.assertEqual(out["move_job"], "panel-req-1")

    def test_an_engine_that_returns_nothing_falls_back_to_the_job_name(self):
        """阿里那条返回的就是任务名，两者相等。"""
        self.assertEqual(
            moves.start(self.TICKET, submit=lambda p, n: None)["move_ref"], "panel-req-1"
        )

    def test_a_failed_move_is_not_restarted_under_the_same_name(self):
        """直接再提一次很自然，但那样 attempt 不变、任务名不变，
        两朵云都会把那个已经失败的任务原样捞回来。"""
        seen = []
        with self.assertRaises(DeliveryError):
            moves.start(
                {**self.TICKET, "move_stage": moves.STAGE_FAILED},
                submit=lambda p, n: seen.append(n),
            )
        self.assertEqual(seen, [])

    def test_a_running_move_is_not_started_again(self):
        seen = []
        with self.assertRaises(DeliveryError):
            moves.start(
                {**self.TICKET, "move_stage": moves.STAGE_RUNNING},
                submit=lambda p, n: seen.append(n),
            )
        self.assertEqual(seen, [])

    def test_a_finished_move_is_not_started_again(self):
        with self.assertRaises(DeliveryError):
            moves.start({**self.TICKET, "move_stage": moves.STAGE_DONE}, submit=lambda p, n: None)


class AdvanceTests(unittest.TestCase):
    RUNNING = {"move_stage": moves.STAGE_RUNNING, "move_job": "panel-req-1"}

    def test_progress_is_recorded_without_changing_the_stage(self):
        out = moves.advance(
            self.RUNNING, {"status": "Transferring", "bytes": 5, "objects": 2}, now=1000.0
        )
        self.assertNotIn("move_stage", out)
        self.assertEqual((out["move_bytes"], out["move_objects"]), (5, 2))

    def test_a_poll_that_read_nothing_does_not_count_as_progress(self):
        """`stuck()` 的判据是 move_updated_ts。轮询失败也刷它的话，任务被人删掉、
        或者返回一个我们不认识的终态，单子会永远在途而且永远不上「卡住」清单。"""
        out = moves.advance(self.RUNNING, {"status": "", "error": "连不上"}, now=1000.0)
        self.assertNotIn("move_updated_ts", out)
        self.assertEqual(out["move_polled_ts"], 1000.0)

    def test_a_good_poll_clears_a_stale_error(self):
        """不清的话，一次网络抖动会在单子上留一条永久的红字。"""
        out = moves.advance(
            {**self.RUNNING, "move_error": "上一轮连不上"}, {"status": "Transferring", "bytes": 1}
        )
        self.assertEqual(out["move_error"], "")

    def test_done_is_done(self):
        out = moves.advance(
            self.RUNNING, {"status": "Success", "done": True, "bytes": 9}, now=1000.0
        )
        self.assertEqual(out["move_stage"], moves.STAGE_DONE)
        self.assertEqual(out["move_done_ts"], 1000.0)

    def test_a_finished_move_that_lost_objects_says_so(self):
        """搬完了也可能有对象没搬过去。丢掉这句话，台账上就是一个干净的「已完成」，
        而少掉的那几个文件要等几个月后训练读到才发现。"""
        out = moves.advance(
            self.RUNNING, {"status": "Success", "done": True, "error": "3 个对象失败"}
        )
        self.assertEqual(out["move_stage"], moves.STAGE_DONE)
        self.assertIn("3 个对象失败", out["move_error"])

    def test_a_failure_carries_the_reason(self):
        out = moves.advance(self.RUNNING, {"failed": True, "error": "源桶没权限"})
        self.assertEqual(out["move_stage"], moves.STAGE_FAILED)
        self.assertIn("源桶没权限", out["move_error"])

    def test_a_polling_error_is_not_a_task_failure(self):
        """查进度失败 ≠ 任务失败。轮询是网络抖动的高发点。"""
        out = moves.advance(self.RUNNING, {"status": "", "error": "连不上"})
        self.assertNotIn("move_stage", out)
        self.assertIn("连不上", out["move_error"])

    def test_a_finished_move_never_goes_back_to_running(self):
        """一次网络抖动不该让失败的任务看起来又活了。"""
        for stage in (moves.STAGE_DONE, moves.STAGE_FAILED):
            self.assertEqual(
                moves.advance(
                    {"move_stage": stage}, {"status": "Success", "bytes": 1, "done": True}
                ),
                {},
                stage,
            )


class RetryTests(unittest.TestCase):
    def test_only_a_failed_move_can_be_retried(self):
        for stage in (moves.STAGE_NEW, moves.STAGE_RUNNING, moves.STAGE_DONE):
            with self.assertRaises(DeliveryError, msg=stage):
                moves.retry({"move_stage": stage})

    def test_a_retry_clears_the_job_so_a_fresh_one_is_submitted(self):
        out = moves.retry(
            {"move_stage": moves.STAGE_FAILED, "move_job": "panel-x", "move_error": "炸了"}
        )
        self.assertEqual(out["move_stage"], moves.STAGE_NEW)
        self.assertEqual(out["move_job"], "")
        self.assertEqual(out["move_error"], "")

    def test_a_retry_bumps_the_attempt_so_the_next_submit_gets_a_new_name(self):
        out = moves.retry({"move_stage": moves.STAGE_FAILED, "move_attempt": 2})
        self.assertEqual(out["move_attempt"], 3)

    def test_a_ticket_that_never_recorded_an_attempt_still_bumps(self):
        """老单子没有这个字段。当成 1 之后加到 2，而不是崩掉或退回 1。"""
        self.assertEqual(moves.retry({"move_stage": moves.STAGE_FAILED})["move_attempt"], 2)

    def test_the_retried_submit_actually_uses_the_new_name(self):
        """整条链走一遍：失败 → 重试 → 再提交，云上拿到的必须是个新名字。"""
        seen = []
        ticket = {"id": "req-1", "payload": {"source": "oss://a/x/", "dest": "oss://b/y/"}}
        moves.start(ticket, submit=lambda p, n: seen.append(n))
        after = moves.retry({**ticket, "move_stage": moves.STAGE_FAILED})
        moves.start(
            {**ticket, **after, "move_stage": moves.STAGE_NEW}, submit=lambda p, n: seen.append(n)
        )
        self.assertEqual(seen, ["panel-req-1", "panel-req-1-r2"])


class PendingTests(unittest.TestCase):
    def test_only_running_moves_with_a_job_are_polled(self):
        rows = [
            {"move_stage": moves.STAGE_RUNNING, "move_job": "panel-1", "move_ref": "1"},
            {"move_stage": moves.STAGE_RUNNING, "move_job": ""},  # 还没提上去
            {"move_stage": moves.STAGE_DONE, "move_job": "panel-2"},
            {},
        ]
        self.assertEqual([t["move_job"] for t in moves.pending(rows)], ["panel-1"])

    def test_stuck_is_only_a_hint_and_needs_a_running_move(self):
        """真判失败要有迁移服务给的终态，不能靠「我们这边没收到消息」。"""
        old = {"move_stage": moves.STAGE_RUNNING, "move_updated_ts": 0.0}
        self.assertTrue(moves.stuck(old, now=moves.STALE_AFTER + 1))
        self.assertFalse(
            moves.stuck({**old, "move_stage": moves.STAGE_FAILED}, now=moves.STALE_AFTER + 1)
        )


if __name__ == "__main__":
    unittest.main()
