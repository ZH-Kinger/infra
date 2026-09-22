"""IT 云账号属性接口客户端。

重点不在「能调通」——那要真 token。重点在**出错时不静默、token 不外泄、一行坏了不挡其余**。
"""

import json
import os
import unittest
from unittest import mock

from delivery import iam_api

CFG = iam_api.Config(base="https://iam.example.com/ext/cloud-accounts", token="tok-SECRET-123")
UID = "on_14bd658615310d3cebe62eb4cd1a41c1"


def fake(status, body, *, seen=None):
    def send(method, url, headers, payload):
        if seen is not None:
            seen.append((method, url, headers, payload))
        return status, body

    return send


class ConfigTests(unittest.TestCase):
    def test_a_missing_token_is_an_error_not_an_anonymous_call(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaises(iam_api.IamApiError) as caught,
        ):
            iam_api.Config.from_env()
        self.assertIn(iam_api.ENV_TOKEN, str(caught.exception))

    def test_plain_http_is_refused_outright(self):
        """明文传 token 等于送出去。**不给降级开关** —— 有开关就会有人在排查时打开它。"""
        env = {iam_api.ENV_TOKEN: "t", iam_api.ENV_BASE: "http://iam.example.com/x"}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(iam_api.IamApiError):
            iam_api.Config.from_env()

    def test_the_token_never_survives_scrubbing(self):
        text = f"upstream said Authorization: Bearer {CFG.token} is fine"
        self.assertNotIn(CFG.token, CFG.scrub(text))


class CallTests(unittest.TestCase):
    def test_it_sends_a_bearer_token_and_json(self):
        seen = []
        iam_api.call(
            "PUT",
            "/users/x/aliyun-main",
            {"value": "a"},
            cfg=CFG,
            transport=fake(200, {}, seen=seen),
        )
        method, url, headers, payload = seen[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(url, "https://iam.example.com/ext/cloud-accounts/users/x/aliyun-main")
        self.assertEqual(headers["Authorization"], f"Bearer {CFG.token}")
        self.assertEqual(payload, b'{"value": "a"}')

    def test_a_get_sends_no_body_and_no_content_type(self):
        seen = []
        iam_api.call("GET", "/users/x", cfg=CFG, transport=fake(200, {}, seen=seen))
        _, _, headers, payload = seen[0]
        self.assertEqual(payload, b"")
        self.assertNotIn("Content-Type", headers)

    def test_an_error_carries_the_code_so_callers_can_tell_retry_from_go_fix_it(self):
        with self.assertRaises(iam_api.IamApiError) as caught:
            iam_api.call(
                "PUT",
                "/users/x/aliyun-main",
                {"value": "a"},
                cfg=CFG,
                transport=fake(409, {"error": "value_taken", "detail": "A00099 占用"}),
            )
        self.assertEqual(caught.exception.code, "value_taken")
        self.assertTrue(caught.exception.terminal)

    def test_upstream_errors_are_retriable_not_terminal(self):
        with self.assertRaises(iam_api.IamApiError) as caught:
            iam_api.call(
                "GET",
                "/users/x",
                cfg=CFG,
                transport=fake(502, {"error": "upstream_error", "detail": "busy"}),
            )
        self.assertFalse(caught.exception.terminal)
        self.assertIn(caught.exception.code, iam_api.RETRIABLE)

    def test_the_token_is_scrubbed_out_of_an_echoed_error(self):
        """上游把 Authorization 头回显进 detail 里 —— 异常消息不能带着它到处跑。"""
        with self.assertRaises(iam_api.IamApiError) as caught:
            iam_api.call(
                "GET",
                "/users/x",
                cfg=CFG,
                transport=fake(400, {"error": "bad_request", "detail": f"got Bearer {CFG.token}"}),
            )
        self.assertNotIn(CFG.token, str(caught.exception))
        self.assertNotIn(CFG.token, caught.exception.detail)

    def test_a_non_json_body_still_produces_a_typed_error(self):
        with self.assertRaises(iam_api.IamApiError) as caught:
            iam_api.call("GET", "/users/x", cfg=CFG, transport=fake(502, {"_raw": "<html>502"}))
        self.assertEqual(caught.exception.code, "upstream_error")


class ListTests(unittest.TestCase):
    def test_a_missing_users_key_raises_instead_of_looking_empty(self):
        """把「读不到」渲染成「IAM 里一个人都没有」，对账那边会得出「全员都该删」。"""
        with self.assertRaises(iam_api.IamApiError):
            iam_api.list_by_app("aliyun-main", cfg=CFG, transport=fake(200, {"count": 0}))

    def test_an_actually_empty_list_is_fine(self):
        got = iam_api.list_by_app("aliyun-main", cfg=CFG, transport=fake(200, {"users": []}))
        self.assertEqual(got, [])


class AppMappingTests(unittest.TestCase):
    def test_scope_maps_to_the_api_identifier(self):
        self.assertEqual(iam_api.app_of("aliyun/1704065796538912"), "aliyun-main")
        self.assertEqual(iam_api.app_of("volcano-main"), "volcano-main")

    def test_an_unknown_platform_is_refused_not_guessed(self):
        """拼错了不能静默跳过：那可能把一个平台的登录名写进另一个平台。"""
        with self.assertRaises(iam_api.IamApiError):
            iam_api.app_of("aliyun/9999999999")


class ApplyTests(unittest.TestCase):
    @staticmethod
    def row(action, app="aliyun/1704065796538912", uid=UID, value="zhangsan@x.onaliyun.com"):
        return {
            "feishu_union_id": uid,
            "app": app,
            "value": value,
            "action": action,
            "name": "张三",
            "email": "z@x.com",
        }

    def test_skip_rows_are_never_sent(self):
        """skip 是比对阶段标出「要人核对」的。下发它等于绕过核对。"""
        sent = []
        out = iam_api.apply_rows([self.row("skip")], cfg=CFG, transport=fake(200, {}, seen=sent))
        self.assertEqual(out, [])
        self.assertEqual(sent, [])

    def test_one_bad_row_does_not_stop_the_rest(self):
        """per-row 包 try，不是整批包一个。一条脏记录不该让这一轮剩下的全部不发。"""
        calls = {"n": 0}

        def flaky(method, url, headers, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                return 409, {"error": "value_taken", "detail": "A00099"}
            return 200, {"previous": "old"}

        rows = [self.row("set", uid=UID), self.row("set", uid="on_" + "b" * 32)]
        out = iam_api.apply_rows(rows, cfg=CFG, transport=flaky)
        self.assertEqual([o.ok for o in out], [False, True])
        self.assertEqual(out[0].code, "value_taken")
        self.assertEqual(out[1].previous, "old")

    def test_a_row_without_a_union_id_fails_loudly_instead_of_being_dropped(self):
        out = iam_api.apply_rows([self.row("set", uid="")], cfg=CFG, transport=fake(200, {}))
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].ok)
        self.assertEqual(out[0].code, "bad_request")

    def test_dry_run_sends_nothing(self):
        sent = []
        out = iam_api.apply_rows(
            [self.row("set"), self.row("remove")],
            cfg=CFG,
            transport=fake(200, {}, seen=sent),
            dry_run=True,
        )
        self.assertEqual(sent, [])
        self.assertTrue(all(o.ok for o in out))

    def test_remove_calls_delete_and_keeps_the_previous_value(self):
        """`previous` 是回滚时唯一的依据 —— 删掉之后再也问不到旧值了。"""
        seen = []
        out = iam_api.apply_rows(
            [self.row("remove")],
            cfg=CFG,
            transport=fake(200, {"previous": "zhangsan@x.onaliyun.com"}, seen=seen),
        )
        self.assertEqual(seen[0][0], "DELETE")
        self.assertEqual(out[0].previous, "zhangsan@x.onaliyun.com")

    def test_an_echoed_token_does_not_leak_into_an_outcome_message(self):
        out = iam_api.apply_rows(
            [self.row("set")],
            cfg=CFG,
            transport=fake(400, {"error": "bad_request", "detail": f"Bearer {CFG.token}"}),
        )
        self.assertNotIn(CFG.token, out[0].message)


class ReclaimCardTests(unittest.TestCase):
    """回收结果的飞书卡片。"""

    @staticmethod
    def card(**report):
        from delivery import notify

        base = {"done": [], "held": [], "failed": []}
        return notify.reclaim_card({**base, **report}, base_url="https://panel.example.com")

    def test_held_items_turn_the_header_orange_even_when_everything_else_succeeded(self):
        """头色按「有没有待办」定，不按「回收了多少」。

        全自动做完是常态，不需要显眼；**有人要去问一句**才需要。
        """
        quiet = self.card(done=[{"username": "A1", "name": "张三", "app": "a", "previous": "x"}])
        loud = self.card(
            done=[{"username": "A1", "name": "张三", "app": "a", "previous": "x"}],
            held=[{"username": "A2", "name": "李四", "app": "a", "value": "y"}],
        )
        self.assertEqual(quiet["header"]["template"], "blue")
        self.assertEqual(loud["header"]["template"], "orange")

    def test_done_and_held_are_never_merged_into_one_count(self):
        """合成一句「本轮处理 2 人」的话，那件要人做的事就没人做了。"""
        card = self.card(
            done=[{"username": "A1", "name": "张三", "app": "a", "previous": "x"}],
            held=[{"username": "A2", "name": "李四", "app": "a", "value": "y"}],
        )
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("已回收 1 人", blob)
        self.assertIn("1 人待确认", blob)

    def test_it_always_says_the_cloud_account_is_still_there(self):
        """只删了属性。看卡片的人不能以为账号也收了。"""
        card = self.card(done=[{"username": "A1", "name": "张三", "app": "a", "previous": "x"}])
        self.assertIn("云上的 RAM/IAM 账号还在", json.dumps(card, ensure_ascii=False))

    def test_a_non_https_base_url_produces_no_button(self):
        from delivery import notify

        card = notify.reclaim_card(
            {"done": [], "held": [], "failed": []}, base_url="http://evil.example.com"
        )
        self.assertNotIn("action", [e.get("tag") for e in card["elements"]])

    def test_an_empty_round_still_renders(self):
        card = self.card()
        self.assertEqual(card["header"]["template"], "grey")
        self.assertIn("没有要回收的", json.dumps(card, ensure_ascii=False))


class ReclaimTests(unittest.TestCase):
    """离职回收：**两个信号都指向离职才自动做**。"""

    @staticmethod
    def them(uid, value, *, active, username="A001", name="某人"):
        return {
            "union_id": uid,
            "username": username,
            "name": name,
            "is_active": active,
            "value": value,
        }

    def test_authentik_alone_is_enough_to_reclaim(self):
        """**以 IT 的 Authentik 为准。** 它说离职就回收，不再要求名册也同意。"""
        got = iam_api.reclaim_plan(
            [self.them("on_a", "x@y", active=False)], app="aliyun-main", roster_uids=set()
        )
        self.assertEqual([r.sure for r in got], [True])

    def test_a_stale_roster_does_not_block_the_reclaim_but_is_flagged(self):
        """飞书通讯录里还有他 → **照样回收**，但标出来。

        那不是「要不要删」的分歧，是**飞书那边该同步离职了** —— 人还在部门树里、
        还在收内部消息。回收做完了不代表这件事没了。
        """
        got = iam_api.reclaim_plan(
            [self.them("on_a", "x@y", active=False)], app="aliyun-main", roster_uids={"on_a"}
        )
        self.assertEqual([r.sure for r in got], [True])
        self.assertTrue(got[0].stale_roster)
        self.assertIn("飞书", got[0].why)

    def test_an_active_person_is_never_touched(self):
        got = iam_api.reclaim_plan(
            [self.them("on_a", "x@y", active=True)], app="aliyun-main", roster_uids=set()
        )
        self.assertEqual(got, [])

    def test_someone_with_no_value_has_nothing_to_reclaim(self):
        """已经没有属性了就不该再产生一条「要删」—— 那会让每天的报告永远不空。"""
        got = iam_api.reclaim_plan(
            [self.them("on_a", "", active=False)], app="aliyun-main", roster_uids=set()
        )
        self.assertEqual(got, [])

    def test_the_ones_needing_a_follow_up_sort_last(self):
        """名册没同步的排后面 —— 前面那些是干净做完的，不用再看。"""
        rows = [
            self.them("on_h", "h@y", active=False, username="B"),
            self.them("on_s", "s@y", active=False, username="A"),
        ]
        got = iam_api.reclaim_plan(rows, app="aliyun-main", roster_uids={"on_h"})
        self.assertEqual([r.username for r in got], ["A", "B"])
        self.assertEqual([r.stale_roster for r in got], [False, True])


class ReconcileTests(unittest.TestCase):
    @staticmethod
    def theirs(uid, value, *, active=True, username="A001", name="某人"):
        return {
            "union_id": uid,
            "username": username,
            "name": name,
            "is_active": active,
            "value": value,
        }

    @staticmethod
    def ours(uid, value):
        return {"feishu_union_id": uid, "value": value, "action": "set"}

    def test_matching_rows_produce_no_drift(self):
        got = iam_api.reconcile(
            [self.theirs("on_a", "x@y")], [self.ours("on_a", "x@y")], app="aliyun-main"
        )
        self.assertEqual(got, [])

    def test_a_departed_user_who_still_holds_a_cloud_login_is_its_own_category(self):
        """文档明说 is_active=false 的是「已离职但云上登录名尚未回收」——
        这不是普通的多一条，是**该去云上禁用那个 RAM 用户**的信号。"""
        got = iam_api.reconcile([self.theirs("on_a", "x@y", active=False)], [], app="aliyun-main")
        self.assertEqual([d.kind for d in got], [iam_api.DRIFT_INACTIVE])

    def test_the_same_person_with_two_different_values_is_flagged(self):
        got = iam_api.reconcile(
            [self.theirs("on_a", "old@y")], [self.ours("on_a", "new@y")], app="aliyun-main"
        )
        self.assertEqual([d.kind for d in got], [iam_api.DRIFT_DIFFERENT])
        self.assertEqual((got[0].theirs, got[0].ours), ("old@y", "new@y"))

    def test_ours_only_and_theirs_only_do_not_collapse_into_one_bucket(self):
        got = iam_api.reconcile(
            [self.theirs("on_a", "a@y")], [self.ours("on_b", "b@y")], app="aliyun-main"
        )
        self.assertEqual({d.kind for d in got}, {iam_api.DRIFT_LEFT, iam_api.DRIFT_MISSING})

    def test_rows_without_a_union_id_are_reported_separately_not_silently_dropped(self):
        """接口只认 union_id，所以这些行既发不出去也比不了。

        混进「对不上 0 条」的话，读的人会以为两边一致 —— 而实际上有人压根没进过比对，
        他的 SSO 也登不进去。真机第一次跑就撞到两个（张子超、练秋酉）。
        """
        ours = [
            {"feishu_union_id": "", "value": "a@y", "action": "set", "name": "张三"},
            {"feishu_union_id": "on_a", "value": "b@y", "action": "set"},
            {"feishu_union_id": "", "value": "", "action": "skip"},
        ]
        blind = iam_api.uncomparable(ours)
        self.assertEqual([r["name"] for r in blind], ["张三"])
        # 而且它们不会污染 drift：没有 union_id 就不该变成一条 missing
        drift = iam_api.reconcile([self.theirs("on_a", "b@y")], ours, app="aliyun-main")
        self.assertEqual(drift, [])

    def test_skip_rows_on_our_side_are_not_treated_as_expected_state(self):
        """skip 意味着「还没核对，先别动」。拿它当期望值会把对账结果变成一堆假的 different。"""
        ours = [{"feishu_union_id": "on_a", "value": "", "action": "skip"}]
        got = iam_api.reconcile([self.theirs("on_a", "x@y")], ours, app="aliyun-main")
        self.assertEqual([d.kind for d in got], [iam_api.DRIFT_LEFT])


class GoneTests(unittest.TestCase):
    """IAM 里挂着一个登录名，但云上已经没有那个账号了。

    后果很隐蔽：人在职、属性也在，但 SSO 匹配到一个不存在的用户 —— 登录失败，
    而排查的人看到「属性明明写着呢」就卡住了。
    """

    THEIRS = [
        {
            "union_id": "u1",
            "username": "A1",
            "name": "甲",
            "value": "zhangsan@1704065796538912.onaliyun.com",
            "is_active": True,
        }
    ]
    OURS = [
        {
            "action": "set",
            "feishu_union_id": "u1",
            "app": "aliyun-main",
            "value": "zhangsan@1704065796538912.onaliyun.com",
        }
    ]

    def test_a_login_that_no_longer_exists_on_the_cloud_is_reported(self):
        got = iam_api.reconcile(self.THEIRS, self.OURS, app="aliyun-main", cloud_users={"lisi"})
        self.assertEqual([d.kind for d in got], [iam_api.DRIFT_GONE])

    def test_a_login_that_still_exists_is_not_reported(self):
        got = iam_api.reconcile(self.THEIRS, self.OURS, app="aliyun-main", cloud_users={"zhangsan"})
        self.assertEqual(got, [])

    def test_without_a_cloud_snapshot_the_whole_check_is_skipped(self):
        """拿不到快照却照判，等于把全公司报成「账号已删」——
        一次凭证过期就能刷出几十条假线索，而假线索会让这一栏从此没人看。"""
        self.assertEqual(iam_api.reconcile(self.THEIRS, self.OURS, app="aliyun-main"), [])

    def test_the_volcano_style_bare_username_is_handled(self):
        """火山那边属性值就是裸的登录名，没有 @ 后缀。"""
        theirs = [
            {
                "union_id": "u1",
                "username": "A1",
                "name": "甲",
                "value": "zhangsan",
                "is_active": True,
            }
        ]
        ours = [
            {"action": "set", "feishu_union_id": "u1", "app": "volcano-main", "value": "zhangsan"}
        ]
        self.assertEqual(
            [
                d.kind
                for d in iam_api.reconcile(theirs, ours, app="volcano-main", cloud_users={"lisi"})
            ],
            [iam_api.DRIFT_GONE],
        )
        self.assertEqual(
            iam_api.reconcile(theirs, ours, app="volcano-main", cloud_users={"zhangsan"}), []
        )

    def test_a_departed_person_is_still_reported_as_inactive_not_gone(self):
        """离职那一类排在前面：该做的事是去云上禁用，不是「账号已删」。"""
        theirs = [dict(self.THEIRS[0], is_active=False)]
        got = iam_api.reconcile(theirs, self.OURS, app="aliyun-main", cloud_users={"lisi"})
        self.assertEqual([d.kind for d in got], [iam_api.DRIFT_INACTIVE])


class RemindDedupTests(unittest.TestCase):
    """同一批人 24 小时只提醒一次。天天重复的提醒等于没有提醒。"""

    def paths(self, box):
        from delivery import iam_sync

        return iam_sync.SyncPaths(
            people=str(box / "people.json"), attributes=str(box / "attrs.json")
        )

    def test_the_same_batch_is_not_reminded_twice_in_the_window(self):
        import tempfile
        from pathlib import Path

        from delivery import iam_sync

        with tempfile.TemporaryDirectory() as box:
            p = self.paths(Path(box))
            self.assertTrue(iam_sync.claim_remind(p, "sig-a", hours=24, now=1000.0))
            self.assertFalse(iam_sync.claim_remind(p, "sig-a", hours=24, now=1000.0 + 3600))
            self.assertTrue(iam_sync.claim_remind(p, "sig-a", hours=24, now=1000.0 + 25 * 3600))

    def test_a_different_batch_is_reminded_right_away(self):
        """又走了一个人就是新的一批，不该被上一批的冷却挡住。"""
        import tempfile
        from pathlib import Path

        from delivery import iam_sync

        with tempfile.TemporaryDirectory() as box:
            p = self.paths(Path(box))
            self.assertTrue(iam_sync.claim_remind(p, "sig-a", hours=24, now=1000.0))
            self.assertTrue(iam_sync.claim_remind(p, "sig-b", hours=24, now=1000.0))

    def test_unreadable_state_still_reminds(self):
        """漏提醒的代价是一个离职的人的云账号一直挂着没人知道；
        多提醒一次的代价只是一条消息。两边不对称。"""
        from delivery import iam_sync

        p = iam_sync.SyncPaths(
            people="/nonexistent/people.json", attributes="/nonexistent/attrs.json"
        )
        self.assertTrue(iam_sync.claim_remind(p, "sig", hours=24, now=1000.0))


if __name__ == "__main__":
    unittest.main()
