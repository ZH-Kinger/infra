"""迁移的定时任务入口。

这一层的活是「挑单子、凑参数、写回结果」，所以测的也是这三件：
挑错了会去动不该动的单子，参数凑错了会搬到别的地域，写回错了会让单子永远在途。
"""

import unittest

from delivery import catalog as catalog_mod
from delivery import mover, moves
from delivery import tickets as t
from delivery.errors import DeliveryError

TEMPLATE = {
    "id": "oss-move",
    "kind": "transfer",
    "platform": "aliyun",
    "account": "1704065796538912",
    "buckets": [
        {"name": "src-b", "region": "cn-shenzhen"},
        {"name": "dst-b", "region": "cn-hangzhou"},
        {"name": "tos-b", "region": "cn-shanghai"},
    ],
}


def ticket(**kw) -> dict:
    got = {
        "id": "REQ-1",
        "status": t.FULFILLING,
        "kind": catalog_mod.KIND_TRANSFER,
        "template": dict(TEMPLATE),
        "payload": {"source": "oss://src-b/a/", "dest": "oss://dst-b/b/", "overwrite": "skip"},
    }
    got.update(kw)
    return got


class Store:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.writes = []

    def all(self):
        return list(self.rows)

    def update(self, ticket_id, *, actor, expect, event, to=None, note="", fields=None):
        self.writes.append({"id": ticket_id, "to": to, "event": event, **(fields or {})})
        for row in self.rows:
            if row["id"] == ticket_id:
                row.update(fields or {})
                if to:
                    row["status"] = to
        return {}


class PickingTests(unittest.TestCase):
    def test_only_approved_transfer_tickets_are_touched(self):
        """按 kind 挑，不按模板 id —— 以后多一个迁移模板不该因为写死了 id 就永远不被搬。"""
        rows = [
            ticket(),
            ticket(id="REQ-2", status=t.PENDING),  # 还没批
            ticket(id="REQ-3", kind=catalog_mod.KIND_RESOURCE),  # 不是迁移
            ticket(id="REQ-4", status=t.DONE),
        ]
        self.assertEqual([x["id"] for x in mover.pending(Store(*rows))], ["REQ-1"])


class RegionTests(unittest.TestCase):
    def test_the_region_comes_from_the_template_snapshot(self):
        """路径串 `oss://桶/目录/` 里带不出地域，而两个引擎都要它。"""
        plan = mover._locate(moves.plan("oss://src-b/a/", "oss://dst-b/b/"), ticket())
        self.assertEqual(plan["src"]["region"], "cn-shenzhen")
        self.assertEqual(plan["dest"]["region"], "cn-hangzhou")

    def test_a_name_that_exists_in_two_regions_stops_the_move(self):
        """模板的桶清单只有名字和地域、没有云的标识，而同一个名字两朵云都可能有
        （`wuji-ego-processed`：阿里杭州一个、火山上海一个）。静默取一个的后果是
        搬到错的云、错的地域，而且一路都不报错。"""
        both = ticket(
            template={
                **TEMPLATE,
                "buckets": [
                    {"name": "src-b", "region": "cn-shenzhen"},
                    {"name": "src-b", "region": "cn-shanghai"},
                    {"name": "dst-b", "region": "cn-hangzhou"},
                ],
            }
        )
        with self.assertRaises(DeliveryError) as caught:
            mover._locate(moves.plan("oss://src-b/a/", "oss://dst-b/b/"), both)
        self.assertIn("同名", str(caught.exception))

    def test_the_same_name_listed_twice_with_one_region_is_fine(self):
        """重复登记同一个桶不是歧义，别把它也拦下来。"""
        dup = ticket(
            template={
                **TEMPLATE,
                "buckets": TEMPLATE["buckets"] + [{"name": "src-b", "region": "cn-shenzhen"}],
            }
        )
        self.assertEqual(
            mover._locate(moves.plan("oss://src-b/a/", "oss://dst-b/b/"), dup)["src"]["region"],
            "cn-shenzhen",
        )

    def test_a_bucket_missing_from_the_template_stops_the_move(self):
        """拿空地域往下走的话，火山会在一个错误的地域里建任务 —— 任务真的建出来了，
        但之后每次查进度都返回「查不到」，而查不到不算失败。"""
        bad = ticket(template={**TEMPLATE, "buckets": [{"name": "src-b", "region": "cn-shenzhen"}]})
        with self.assertRaises(DeliveryError):
            mover._locate(moves.plan("oss://src-b/a/", "oss://dst-b/b/"), bad)


class ReviewGateTests(unittest.TestCase):
    """飞书审批人看到的是两个路径，不是多少数据。以为 100GB 实际 100TB 的单子，
    批的时候看不出区别。"""

    def _start(self, size, known):
        self.addCleanup(setattr, mover, "_measure", mover._measure)
        self.addCleanup(setattr, mover, "_creds", mover._creds)
        mover._measure = lambda plan, **kw: (size, 1, known)
        mover._creds = lambda *a, **kw: object()
        return mover.start_one(ticket(), config=mover.Config(oss_role="r"))

    def test_a_small_move_goes_straight_through(self):
        self.addCleanup(setattr, mover, "_submit", mover._submit)
        mover._submit = lambda *a, **kw: "panel-REQ-1"
        self.assertEqual(self._start(1024, True)["move_stage"], moves.STAGE_RUNNING)

    def test_a_big_move_waits_for_a_person_and_does_not_submit(self):
        seen = []
        self.addCleanup(setattr, mover, "_submit", mover._submit)
        mover._submit = lambda *a, **kw: seen.append(1)
        out = self._start(9 * 1024**4, True)
        self.assertEqual(out["move_stage"], mover.STAGE_REVIEW)
        self.assertEqual(seen, [])
        self.assertIn("TB", out["move_error"])

    def test_a_move_we_cannot_measure_is_treated_as_big(self):
        """量不出来返回 (0,0) 的话判定恒为 False —— 100TB 当成 0 字节直接放行。"""
        self.assertEqual(self._start(0, False)["move_stage"], mover.STAGE_REVIEW)

    def test_an_admin_confirmation_skips_the_gate(self):
        self.addCleanup(setattr, mover, "_submit", mover._submit)
        self.addCleanup(setattr, mover, "_creds", mover._creds)
        mover._creds = lambda *a, **kw: object()
        mover._submit = lambda *a, **kw: "panel-REQ-1"
        out = mover.start_one(ticket(move_reviewed=True), config=mover.Config(oss_role="r"))
        self.assertEqual(out["move_stage"], moves.STAGE_RUNNING)


class SweepTests(unittest.TestCase):
    def _fake(self, **kw):
        for name, value in kw.items():
            self.addCleanup(setattr, mover, name, getattr(mover, name))
            setattr(mover, name, value)

    def test_a_ticket_that_finished_moves_the_whole_request_to_done(self):
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="577732", move_engine="dms"))
        self._fake(
            _poll=lambda tk, **kw: {"status": "Success", "done": True, "bytes": 9, "objects": 2}
        )
        self.assertEqual(mover.sweep(store, config=mover.Config(), log=lambda *a: None), 0)
        self.assertEqual(store.rows[0]["status"], t.DONE)

    def test_a_ticket_still_running_stays_where_it_is(self):
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1", move_engine="dms"))
        self._fake(_poll=lambda tk, **kw: {"status": "Transferring", "bytes": 5})
        mover.sweep(store, config=mover.Config(), log=lambda *a: None)
        self.assertEqual(store.rows[0]["status"], t.FULFILLING)
        self.assertEqual(store.rows[0]["move_bytes"], 5)

    def test_one_broken_ticket_does_not_stop_the_others(self):
        """这条定时任务是无人值守的。一张卡住的单子会让所有在途迁移集体停摆，
        而且没人会发现。"""
        store = Store(
            ticket(id="REQ-BAD", move_stage=moves.STAGE_RUNNING, move_ref="1"),
            ticket(id="REQ-OK", move_stage=moves.STAGE_RUNNING, move_ref="2"),
        )
        seen = []

        def poll(tk, **kw):
            if tk["id"] == "REQ-BAD":
                raise RuntimeError("炸了")
            seen.append(tk["id"])
            return {"status": "Transferring", "bytes": 1}

        self._fake(_poll=poll)
        problems = mover.sweep(store, config=mover.Config(), log=lambda *a: None)
        self.assertEqual(problems, 1)
        self.assertEqual(seen, ["REQ-OK"])
        self.assertIn("炸了", store.rows[0]["move_error"])

    def test_a_ticket_waiting_for_a_person_is_left_alone(self):
        """标成 review 的单子在等管理员点头，定时器不该替他点。"""
        store = Store(ticket(move_stage=mover.STAGE_REVIEW))
        self._fake(_poll=_boom, start_one=_boom)
        self.assertEqual(mover.sweep(store, config=mover.Config(), log=lambda *a: None), 0)
        self.assertEqual(store.writes, [])

    def test_a_failed_move_is_not_retried_on_its_own(self):
        """自动重试会把失败的任务原样捞回来空转。重试是人点的，那一步会换任务名。"""
        store = Store(ticket(move_stage=moves.STAGE_FAILED, move_error="源桶没权限"))
        self._fake(_poll=_boom, start_one=_boom)
        mover.sweep(store, config=mover.Config(), log=lambda *a: None)
        self.assertEqual(store.writes, [])


class AnnounceTests(unittest.TestCase):
    """搬运卡到人这一步时要私聊管理员 —— 不然状态只写在单子里，
    没人主动去看就等于没发生。但五分钟一轮，不去重就是一天 288 条。"""

    def _fake(self, **kw):
        for name, value in kw.items():
            self.addCleanup(setattr, mover, name, getattr(mover, name))
            setattr(mover, name, value)

    def _sweep(self, store, seen):
        return mover.sweep(
            store,
            config=mover.Config(),
            log=lambda *a: None,
            announce=lambda stage, tk: seen.append((stage, tk["id"])),
        )

    def test_a_move_waiting_for_a_person_is_announced(self):
        store = Store(ticket())
        seen = []
        self._fake(
            start_one=lambda tk, **kw: {
                "move_stage": mover.STAGE_REVIEW,
                "move_error": "9.0 TB，超过 1.0 TB",
            }
        )
        self._sweep(store, seen)
        self.assertEqual(seen, [(mover.STAGE_REVIEW, "REQ-1")])

    def test_a_failure_is_announced(self):
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1"))
        seen = []
        self._fake(
            _poll=lambda tk, **kw: {"status": "Failure", "failed": True, "error": "源桶没权限"}
        )
        self._sweep(store, seen)
        self.assertEqual(seen, [(moves.STAGE_FAILED, "REQ-1")])

    def test_the_same_thing_is_not_announced_every_five_minutes(self):
        """一个每天响 288 次的通知等于没有通知。"""
        store = Store(ticket())
        seen = []
        self._fake(
            start_one=lambda tk, **kw: {"move_stage": mover.STAGE_REVIEW, "move_error": "太大了"}
        )
        self._sweep(store, seen)
        self._sweep(store, seen)
        self._sweep(store, seen)
        self.assertEqual(len(seen), 1)

    def test_a_second_failure_after_a_retry_is_announced_again(self):
        """去重标记不清的话，重试后又失败的单子从此再也不提醒 ——
        而第二次失败恰恰更需要人看一眼。"""
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1"))
        seen = []
        self._fake(_poll=lambda tk, **kw: {"status": "Failure", "failed": True, "error": "炸了"})
        self._sweep(store, seen)
        # 管理员点了重试：回到 new，下一轮重新提交
        store.rows[0].update(moves.retry(store.rows[0]))
        self._fake(
            start_one=lambda tk, **kw: {
                "move_stage": moves.STAGE_RUNNING,
                "move_job": "panel-REQ-1-r2",
                "move_ref": "2",
            }
        )
        self._sweep(store, seen)
        self._fake(_poll=lambda tk, **kw: {"status": "Failure", "failed": True, "error": "又炸了"})
        self._sweep(store, seen)
        self.assertEqual(seen, [(moves.STAGE_FAILED, "REQ-1"), (moves.STAGE_FAILED, "REQ-1")])

    def test_a_transient_error_does_not_swallow_the_real_failure_later(self):
        """**这个漏能让一张单子的真失败永远不被通知。**

        轮 1 轮询抛异常（配置错、网络抖）→ 记去重标记；
        轮 2 恢复、任务还在跑 → 没有状态变化，标记清不掉；
        轮 3 任务真失败 → 标记还等于 failed，`was != stage` 不成立 → **一个字都不发**。
        而这条定时任务无人值守，飞书私聊是它唯一的出口。"""
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1"))
        seen = []
        self._fake(advance_one=_boom)
        self._sweep(store, seen)  # 轮 1：这轮没跑成
        self.assertEqual(seen, [(mover.STAGE_ERROR, "REQ-1")])
        self.assertEqual(
            store.rows[0]["move_stage"], moves.STAGE_RUNNING, "瞬时错误不该改单子的状态"
        )

        self._fake(advance_one=lambda tk, **kw: {})  # 轮 2：查成了，什么都没变
        self._sweep(store, seen)
        self.assertEqual(store.rows[0].get("move_notified"), "", "恢复之后陈旧的去重标记要清掉")

        self._fake(
            advance_one=lambda tk, **kw: {
                "move_stage": moves.STAGE_FAILED,
                "move_error": "真失败了",
            }
        )
        self._sweep(store, seen)  # 轮 3：真失败
        self.assertEqual(seen[-1], (moves.STAGE_FAILED, "REQ-1"))

    def test_a_transient_error_is_not_announced_every_five_minutes_either(self):
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1"))
        seen = []
        self._fake(advance_one=_boom)
        self._sweep(store, seen)
        self._sweep(store, seen)
        self.assertEqual(len(seen), 1)

    def test_a_notification_that_fails_to_send_does_not_break_the_move(self):
        """通知发不出去不该让搬运本身算失败 —— 那会让一张搬得好好的单子被标成有问题。"""
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1"))
        self._fake(_poll=lambda tk, **kw: {"status": "Failure", "failed": True, "error": "炸了"})
        problems = mover.sweep(store, config=mover.Config(), log=lambda *a: None, announce=_boom)
        self.assertEqual(problems, 1)  # 搬运确实失败了，这个 1 是它
        self.assertEqual(store.rows[0]["move_stage"], moves.STAGE_FAILED)


class CliTests(unittest.TestCase):
    """命令行入口。**每个参数都要真跑一遍** —— 一个只在带某个 flag 时才走到的
    NameError，单元测试全绿、定时器每五分钟静静失败一次，没人会看见。"""

    def _run(self, *argv) -> int:
        import os
        import tempfile
        from pathlib import Path

        from delivery.cli import main

        with tempfile.TemporaryDirectory() as box:
            # 申请单文件不存在 = 一张单子都没有，是正常状态，不用造
            (Path(box) / "identity").mkdir()
            here = Path.cwd()
            os.chdir(box)
            try:
                return main(["requests", "moves", "--tickets", "identity/tickets.json", *argv])
            finally:
                os.chdir(here)

    def test_the_dry_run_works(self):
        self.assertEqual(self._run("--dry-run"), 0)

    def test_dry_run_and_confirm_together_are_refused(self):
        """`--dry-run` 的全部含义就是「这次别改任何东西」，而放行会真写盘。
        用 dry-run 确认自己没写错单号的人，恰好会被这条坑到 ——
        他以为在预览，实际已经把一个几十 TB 的搬运放行了。"""
        self.assertEqual(self._run("--dry-run", "--confirm", "REQ-1"), 2)

    def test_the_sources_flag_works(self):
        """这条路只有带 --sources 才走到，本来漏了一个 import。"""
        self.assertEqual(self._run("--sources", "identity/transfer-sources.json", "--dry-run"), 0)

    def test_dry_run_and_retry_together_are_refused(self):
        self.assertEqual(self._run("--dry-run", "--retry", "REQ-1"), 2)

    def test_retry_and_confirm_together_are_refused(self):
        self.assertEqual(self._run("--retry", "REQ-1", "--confirm", "REQ-1"), 2)

    def test_retrying_an_unknown_ticket_says_so(self):
        self.assertEqual(self._run("--retry", "REQ-nope"), 1)


class RetryEntryTests(unittest.TestCase):
    """**失败卡上写着「决定重试还是关掉这张单」，所以重试必须真的存在。**

    在这之前 `moves.retry()` 一个生产调用方都没有 —— 人照着卡片去找重试，
    找不到，只能关单重提一张新的、重走一遍飞书审批。
    """

    def _retry(self, store, want="REQ-1") -> int:
        from delivery.cli_requests import _move_retry

        return _move_retry(store, want)

    def test_a_failed_move_goes_back_into_the_queue(self):
        store = Store(
            ticket(
                move_stage=moves.STAGE_FAILED,
                move_error="炸了",
                move_job="panel-REQ-1",
                move_ref="1",
            )
        )
        self.assertEqual(self._retry(store), 0)
        row = store.rows[0]
        self.assertEqual(row["move_stage"], moves.STAGE_NEW)
        self.assertEqual(row["move_error"], "")
        self.assertEqual(row["move_attempt"], 2, "次数不加的话会把那个失败的同名任务原样捞回来")
        self.assertEqual(row["move_job"], "")

    def test_a_running_move_is_not_retried(self):
        """在途的重试等于并行跑两份，台账会有两条互相覆盖的进度。"""
        store = Store(ticket(move_stage=moves.STAGE_RUNNING, move_ref="1"))
        self.assertEqual(self._retry(store), 1)
        self.assertEqual(store.writes, [])

    def test_the_retry_is_written_to_the_ledger(self):
        """谁在什么时候重试的要留痕 —— 一张反复重试的单子，台账是唯一能看出来的地方。"""
        store = Store(ticket(move_stage=moves.STAGE_FAILED, move_error="炸了"))
        self._retry(store)
        self.assertEqual([w["event"] for w in store.writes], ["move_retried"])


def _boom(*_a, **_k):
    raise AssertionError("这张单子不该被碰")


if __name__ == "__main__":
    unittest.main()


class CrossCloudCredTests(unittest.TestCase):
    """跨云那把源端钥匙：**现场签、只读、搬完撤**。

    原先这里交的是面板的开通身份（能 `ram:CreateUser`），而跨云迁移会把它
    明文写进对方云的任务配置里长期留存 —— 撤不回来，只能轮换。
    """

    class Issuer:
        def __init__(self):
            self.issued, self.revoked = [], []

        def __call__(self, platform, account):
            self.platform, self.account = platform, account
            return self

        def issue_long_term(self, user, display, doc):
            self.issued.append((user, doc))
            return type("C", (), {"access_key_id": "AK-mint", "access_key_secret": "SK-mint"})()

        def revoke_long_term(self, user):
            self.revoked.append(user)
            return []

    #: 云上是否已有这张单的任务（假 dms.existing 的返回值）
    already = None

    def _cross(self, **kw):
        return ticket(
            payload={"source": "oss://src-b/a/", "dest": "tos://tos-b/", "overwrite": "skip"},
            **kw,
        )

    def _fake_dms(self, seen):
        """换掉火山 DMS 的提交。

        **要同时改包属性，不能只塞 sys.modules。** `from .clouds import dms` 先看
        `delivery.clouds` 这个包对象上有没有 `dms` 属性 —— 别的用例真导入过一次之后
        那个属性就在了，于是我们塞进 sys.modules 的假货根本走不到。
        单独跑这个文件时它是绿的，全量跑就挂 —— 而那种绿最误导人。
        """
        import sys
        import types

        from delivery import clouds as clouds_pkg

        mod = types.ModuleType("delivery.clouds.dms")
        mod.submit = lambda **kw: seen.append(kw) or "task-1"
        mod.existing = lambda **kw: self.already
        had = getattr(clouds_pkg, "dms", None)
        self.addCleanup(sys.modules.pop, "delivery.clouds.dms", None)
        if had is None:
            self.addCleanup(
                lambda: delattr(clouds_pkg, "dms") if hasattr(clouds_pkg, "dms") else None
            )
        else:
            self.addCleanup(setattr, clouds_pkg, "dms", had)
        sys.modules["delivery.clouds.dms"] = mod
        clouds_pkg.dms = mod

    def test_the_key_handed_to_the_other_cloud_is_the_minted_one(self):
        """**不是开通身份那把。** 断言的是真正塞进 dms.submit 的那串。"""
        seen, iss = [], self.Issuer()
        self._fake_dms(seen)
        store = Store(self._cross(move_reviewed=True))
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=iss,
            executor=lambda p, a: type("E", (), {"_creds": object()})(),
        )
        self.assertEqual(seen[0]["src_key"], "AK-mint")
        self.assertEqual(seen[0]["src_secret"], "SK-mint")
        self.assertEqual(iss.platform, "aliyun", "源端是阿里，就该签阿里那边的")

    def test_the_minted_user_is_recorded_before_it_can_be_lost(self):
        """不记的话，云上留下一个谁也对不上的子账号。"""
        seen, iss = [], self.Issuer()
        self._fake_dms(seen)
        store = Store(self._cross(move_reviewed=True))
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=iss,
            executor=lambda p, a: type("E", (), {"_creds": object()})(),
        )
        self.assertEqual(store.rows[0]["move_cred_user"], iss.issued[0][0])
        self.assertEqual(store.rows[0]["move_cred_cloud"], "aliyun")

    def test_finishing_the_move_revokes_the_key(self):
        """**不等时间窗到期。** 一把还能用两周的钥匙躺在对方云的任务配置里，
        和「我们已经搬完了」这件事没有任何关系。"""
        iss = self.Issuer()
        store = Store(
            self._cross(
                move_stage=moves.STAGE_RUNNING,
                move_ref="1",
                move_cred_user="tempak-move-x",
                move_cred_cloud="aliyun",
            )
        )
        self.addCleanup(setattr, mover, "_poll", mover._poll)
        mover._poll = lambda tk, **kw: {"status": "Done", "done": True, "bytes": 1, "objects": 1}
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=iss,
        )
        self.assertEqual(iss.revoked, ["tempak-move-x"])
        self.assertEqual(store.rows[0]["move_cred_user"], "")

    def test_a_failed_move_revokes_it_too(self):
        """失败更要撤 —— 那把钥匙已经在对方云上了，任务失不失败它都还能用。"""
        iss = self.Issuer()
        store = Store(
            self._cross(
                move_stage=moves.STAGE_RUNNING,
                move_ref="1",
                move_cred_user="tempak-move-x",
                move_cred_cloud="aliyun",
            )
        )
        self.addCleanup(setattr, mover, "_poll", mover._poll)
        mover._poll = lambda tk, **kw: {"status": "Failure", "failed": True, "error": "炸了"}
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=iss,
        )
        self.assertEqual(iss.revoked, ["tempak-move-x"])

    def test_a_revoke_that_fails_is_recorded_not_swallowed(self):
        """撤不掉不该让一次成功的搬运变成失败，但**必须留痕** ——
        不留的话云上多一把没人知道的长期钥匙。"""

        class Bad(self.Issuer):
            def revoke_long_term(self, user):
                raise RuntimeError("没权限")

        store = Store(
            self._cross(
                move_stage=moves.STAGE_RUNNING,
                move_ref="1",
                move_cred_user="tempak-move-x",
                move_cred_cloud="aliyun",
            )
        )
        self.addCleanup(setattr, mover, "_poll", mover._poll)
        mover._poll = lambda tk, **kw: {"status": "Done", "done": True, "bytes": 1, "objects": 1}
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=Bad(),
        )
        self.assertEqual(store.rows[0]["move_stage"], moves.STAGE_DONE, "搬运本身是成功的")
        self.assertTrue(store.rows[0].get("move_cred_left"), "没撤掉要在单子上留痕")
        self.assertEqual(store.rows[0]["move_cred_user"], "tempak-move-x", "没撤掉就别清空名字")

    def test_an_existing_cloud_task_is_reused_without_minting(self):
        """上一轮任务其实建出来了（回包丢了、或写单子失败）。这一轮先签新钥匙的话，
        会把那个任务正在用的钥匙撤掉 —— 任务照样跑，然后全部 403（审计 H-C）。"""
        seen, iss = [], self.Issuer()
        self._fake_dms(seen)
        self.already = "9876"
        store = Store(self._cross(move_reviewed=True))
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=iss,
            executor=lambda p, a: type("E", (), {"_creds": object()})(),
        )
        self.assertEqual(iss.issued, [], "云上已有任务，不该签新钥匙")
        self.assertEqual(iss.revoked, [], "更不该撤掉它正在用的那把")
        self.assertEqual(seen, [], "也不该再提交一次")
        self.assertEqual(store.rows[0]["move_ref"], "9876")
        # 复用也要记下钥匙名 —— 上一轮可能在「签出来」和「写回单子」之间被杀掉，
        # 没有名字的话 reclaim 永远找不到它（审计 R2）
        self.assertTrue(store.rows[0].get("move_cred_user", "").startswith("tempak-move-"))
        self.assertEqual(store.rows[0]["move_cred_cloud"], "aliyun")

    def test_a_failed_submit_still_records_the_minted_key_and_the_root_cause(self):
        """只写进文案的话单子上查不到钥匙名，搬运结束时没人去撤；
        `from None` 还会把真正的根因吞掉（审计 M-1）。"""
        import sys
        import types

        from delivery import clouds as clouds_pkg

        iss = self.Issuer()
        mod = types.ModuleType("delivery.clouds.dms")
        mod.existing = lambda **kw: None

        def boom(**kw):
            raise RuntimeError("boom-root-cause")

        mod.submit = boom
        had = getattr(clouds_pkg, "dms", None)
        self.addCleanup(sys.modules.pop, "delivery.clouds.dms", None)
        self.addCleanup(setattr, clouds_pkg, "dms", had) if had else None
        sys.modules["delivery.clouds.dms"] = mod
        clouds_pkg.dms = mod

        store = Store(self._cross(move_reviewed=True))
        mover.sweep(
            store,
            config=mover.Config(volcano_account="2111674479"),
            log=lambda *a: None,
            issuer=iss,
            executor=lambda p, a: type("E", (), {"_creds": object()})(),
        )
        row = store.rows[0]
        self.assertEqual(row.get("move_cred_user"), iss.issued[0][0])
        self.assertIn("boom-root-cause", row.get("move_error", ""))

    def test_same_cloud_moves_mint_nothing(self):
        """同云不该平白多建一个子账号 —— 阿里那条源用 RAM 角色，
        火山那条的钥匙从头到尾没离开火山。"""
        iss = self.Issuer()
        seen = []
        self.addCleanup(setattr, mover, "_submit", mover._submit)
        mover._submit = lambda *a, **kw: seen.append(kw) or "job-1"
        store = Store(ticket(move_reviewed=True))  # oss -> oss
        mover.sweep(store, config=mover.Config(), log=lambda *a: None, issuer=iss)
        self.assertEqual(iss.issued, [])
        self.assertEqual(store.rows[0].get("move_cred_user", ""), "")


class DataflowSweepTests(unittest.TestCase):
    """预热 / 沉降单要能走完 sweep → start_one → 提交。

    **原来每一张都在 `_locate` 被挡死**：文件系统 id 不在桶表里，
    每 5 分钟报一次「查不到桶的地域」（审计 H-A）。单元测试没有一条让 dataflow 单
    走过 sweep，所以全绿。"""

    def test_a_preheat_ticket_reaches_the_engine(self):
        tpl = dict(
            TEMPLATE, filesystems=[{"id": "bmcpfs-1", "region": "cn-hangzhou", "cloud": "aliyun"}]
        )
        store = Store(
            ticket(
                template=tpl,
                move_reviewed=True,
                payload={
                    "source": "oss://src-b/a/",
                    "dest": "cpfs://bmcpfs-1/d/",
                    "overwrite": "skip",
                },
            )
        )
        seen = []
        self.addCleanup(setattr, mover, "_submit_dataflow", mover._submit_dataflow)
        mover._submit_dataflow = lambda plan, tk, **kw: seen.append(plan) or "task-x"
        problems = mover.sweep(store, config=mover.Config(), log=lambda *a: None)
        self.assertEqual(problems, 0, store.rows[0].get("move_error"))
        self.assertEqual(seen[0]["engine"], "nas")
        self.assertEqual(seen[0]["fs"]["region"], "cn-hangzhou", "地域要从 filesystems 里取")
        self.assertEqual(store.rows[0]["move_stage"], moves.STAGE_RUNNING)


class ReclaimTests(unittest.TestCase):
    """单子结束了、跨云钥匙还挂着的，每轮都再撤一次（审计 M-2）。

    漏撤的后果：策略 14 天后失效，但子账号和 AK 永久留在云上，
    而体检的孤儿报告按 `tempak-` 前缀排除了它们 —— 没有任何报表会报出来。
    """

    class Issuer:
        def __init__(self, fail=False):
            self.revoked, self.fail = [], fail

        def __call__(self, platform, account):
            return self

        def revoke_long_term(self, user):
            self.revoked.append(user)
            if self.fail:
                raise RuntimeError("没权限")
            return []

    def _run(self, iss, **kw):
        row = ticket(move_cred_user="tempak-move-x", move_cred_cloud="aliyun", **kw)
        store = Store(row)
        mover.reclaim(store, config=mover.Config(), issuer=iss, log=lambda *a: None)
        return store

    def test_a_closed_ticket_gets_its_key_revoked(self):
        """在途时被人关掉 —— 钥匙和对方云上的任务都还在。"""
        iss = self.Issuer()
        store = self._run(iss, status=t.CLOSED, move_stage=moves.STAGE_RUNNING)
        self.assertEqual(iss.revoked, ["tempak-move-x"])
        self.assertEqual(store.rows[0]["move_cred_user"], "")

    def test_a_done_ticket_whose_first_revoke_failed_is_retried(self):
        iss = self.Issuer()
        self._run(iss, status=t.DONE, move_stage=moves.STAGE_DONE, move_cred_left="没权限")
        self.assertEqual(iss.revoked, ["tempak-move-x"])

    def test_a_running_move_keeps_its_key(self):
        """钥匙正在被对方云的任务用着 —— 撤了它就是亲手把搬运弄成全部 403。"""
        iss = self.Issuer()
        store = self._run(iss, move_stage=moves.STAGE_RUNNING)
        self.assertEqual(iss.revoked, [])
        self.assertEqual(store.rows[0]["move_cred_user"], "tempak-move-x")

    def test_a_revoke_that_keeps_failing_keeps_the_name(self):
        """撤不掉就别清名字 —— 清了下一轮就不知道该撤谁了。"""
        store = self._run(self.Issuer(fail=True), status=t.CLOSED)
        self.assertEqual(store.rows[0]["move_cred_user"], "tempak-move-x")
        self.assertTrue(store.rows[0].get("move_cred_left"))

    def test_a_key_that_keeps_failing_does_not_flood_the_ledger(self):
        iss = self.Issuer(fail=True)
        row = ticket(status=t.CLOSED, move_cred_user="tempak-move-x", move_cred_cloud="aliyun")
        store = Store(row)
        for _ in range(5):
            mover.reclaim(store, config=mover.Config(), issuer=iss, log=lambda *a: None)
        self.assertEqual(len(iss.revoked), 5, "每一轮都要再试")
        self.assertEqual(len(store.writes), 1, "但只在第一次（字段变了）写单子")


class StableErrorTests(unittest.TestCase):
    """`reclaim` 靠「字段没变就不写」防刷屏。原始错误里带 RequestId 的话每一轮都不一样，
    那道判断永远不成立 —— 所以落进单子之前要抹掉会变的部分。"""

    def test_request_ids_are_blanked_so_the_same_failure_reads_the_same(self):
        one = mover._stable("Forbidden.RAM request id: 01A0C824-7E50-5628-8AB1-C1A3CF60F7C2")
        two = mover._stable("Forbidden.RAM request id: 9F11AB00-0000-4444-8888-ABCDEF012345")
        self.assertEqual(one, two)
        self.assertIn("Forbidden.RAM", one, "错误码要留着，那是人要看的")

    def test_a_plain_message_is_left_alone(self):
        self.assertEqual(mover._stable("没权限"), "没权限")
