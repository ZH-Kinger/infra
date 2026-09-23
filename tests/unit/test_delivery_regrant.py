"""发出去之后还能改什么：`delivery.regrant` 的拒绝理由、算出来的新文档、以及 diff。

这个模块存在的理由就是 2026-09-23 那次故障：凭证发出去之后策略就冻住了，
而当天代码里的动作表还缺 `oss:GetBucketLocation`，lakeFS 连不上杭州那个桶，
只能有人 ssh 上服务器手改云上的策略。所以这里的断言都往两个方向钉：

  · **改的是策略不是凭证** —— 算出来的文档必须用**当前代码**的动作表
    （今天缺的那两个动作 `oss:GetBucketLocation` / `oss:GetObjectVersion` 直接点名断言）；
  · **这条路不能变成一个静默的扩权通道** —— 只有长期凭证能改、caps 不许超出模板、
    生效时间读不回来就拒（不许拿 now 兜底）、总时长不许翻过模板那道闸。

策略文档本身的形状（桶信息条不叠前缀、每条叠时间窗、末尾 Deny）在
`test_delivery_credentials.py` 里已经逐条钉过，这里不重复造，只锁「regrant 确实
把当前代码那张表算进去了」。

本模块**一个云调用都没有**，所以整份测试没有任何替身。数据全部虚构。
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import grants as grants_mod
from delivery import regrant

BUCKET = "wuji-bucket-hangzhou"
PREFIX = "team/lakefs/"
ARN = f"acs:oss:*:*:{BUCKET}"
OBJ_ARN = f"{ARN}/{PREFIX}*"

#: 2026-09-23 前后的一个固定时刻。全部用参数传进去，不读真实时钟
NOW = 1_790_121_600.0
NB = NOW - 3600.0  # 已经生效一小时
EXP = NOW + 20 * 86400.0  # 还有 20 天到期
HOUR = 3600.0


def tpl(**over):
    """一张「数据桶访问凭证」模板。字段照 `catalog.Template` 的默认值来，只覆盖凭证相关的。"""
    base = dict(
        id="oss-credential",
        kind=catalog_mod.KIND_CREDENTIAL,
        platform="aliyun",
        account="1000000000000001",
        title="数据桶访问凭证",
        role_arn="acs:ram::1000000000000001:role/wuji-panel-reader",
        max_hours=24 * 30,
        caps=("list", "download", "write"),
        buckets=((BUCKET, "cn-hangzhou"),),
    )
    base.update(over)
    return catalog_mod.Template(**base)


def ticket(**over):
    """一张已经发出去的长期凭证单。`cred_user` 有值 = 云上真有个子账号和一条策略。"""
    payload = {"bucket": BUCKET, "prefix": PREFIX, "hours": 24 * 30}
    payload.update(over.pop("payload", {}) or {})
    base = {
        "id": "REQ-0001",
        "kind": catalog_mod.KIND_CREDENTIAL,
        "cred_user": "staff-lisi-a1b2c3",
        "cred_not_before": NB,
        "expires_at_ts": EXP,
        "payload": payload,
    }
    base.update(over)
    return base


def doc(caps=("list", "download", "write"), *, not_before=NB, expire=EXP, prefix=PREFIX):
    """和 regrant 应该算出来的一模一样的文档 —— 用来构造 `before`。"""
    return grants_mod.build_policy(
        "aliyun", BUCKET, prefix=prefix, caps=caps, not_before=not_before, expire=expire
    )


def allow_actions(policy):
    """文档里所有 Allow 语句的动作。**只看 Allow** —— 末尾那条兜底 Deny 里的
    `oss:PutObjectAcl` 会让「上传权限回弹了吗」这类子串断言假阳性。"""
    out = set()
    for st in policy.get("Statement") or []:
        if st.get("Effect") == "Allow":
            out.update(st.get("Action") or [])
    return out


def obj_stmt(*actions):
    """一条和「下载 / 上传」同键的对象级语句：Allow + 对象 ARN + 不叠前缀条件。

    `build_policy` 一个文档最多产出两条同键语句，所以「同组三条以上」只能手搓。
    """
    return {
        "Effect": "Allow",
        "Action": list(actions),
        "Resource": [OBJ_ARN],
        "Condition": {"DateGreaterThan": {"acs:CurrentTime": grants_mod.iso8601_bj(NB)}},
    }


def actions_of(policy, resource, *, with_prefix):
    """按 (Resource, 有没有前缀条件) 取出那一条 Allow 的动作表。"""
    for st in policy["Statement"]:
        if st.get("Effect") != "Allow" or st.get("Resource") != [resource]:
            continue
        if bool((st.get("Condition") or {}).get("StringLike")) is with_prefix:
            return st
    raise AssertionError(f"没有 Resource={resource} with_prefix={with_prefix} 的 Allow 语句")


class LongTermOnlyTests(unittest.TestCase):
    """**只有长期凭证能改，判据是 `cred_user` 而不是申请时填的小时数。**

    ≤12 小时走的是 STS：云上既没有策略对象也没有子账号，什么都改不了。
    而「这把凭证实际走了哪条路」只有 `cred_user` 说了算 —— hours 是申请当时的数字，
    模板改过、走过重试、被管理员改过时长，它都可能和现实对不上。
    """

    def test_short_hours_but_a_real_subuser_can_still_be_repaired(self):
        """按 hours 判会把这张单误判成 STS —— 可它云上真有个子账号和一条策略。"""
        t = ticket(payload={"hours": 6})
        p = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertTrue(p.ok, p.why)
        self.assertTrue(p.policy)

    def test_long_hours_without_a_subuser_is_refused(self):
        """反过来：单子上写着 30 天，但 `cred_user` 是空的 —— 云上没有可改的东西。"""
        t = ticket(cred_user="", payload={"hours": 24 * 30})
        p = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertFalse(p.ok)
        self.assertIn("12 小时", p.why)
        self.assertIn("重新申请", p.why)
        self.assertEqual(p.policy, {})

    def test_missing_cred_user_key_is_refused_too(self):
        """字段压根不存在（老单子）和空串一样 —— 不许当成「大概是长期的」。"""
        t = ticket()
        t.pop("cred_user")
        self.assertFalse(regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW).ok)

    def test_refusal_carries_a_human_reason(self):
        """拒绝要给人话：管理员看到之后的下一步动作取决于原因。"""
        p = regrant.plan(ticket(cred_user=""), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertEqual(p.summary, p.why)
        self.assertGreater(len(p.why), 10)


class PreconditionTests(unittest.TestCase):
    """改之前必须成立的几件事：认识的改法、凭证单、桶还在模板里。"""

    def test_unknown_mode_is_refused_and_lists_the_real_ones(self):
        p = regrant.plan(ticket(), tpl(), mode="extend", now=NOW)
        self.assertFalse(p.ok)
        for mode in regrant.MODES:
            self.assertIn(mode, p.why)

    def test_the_three_modes_are_exactly_repair_expire_caps(self):
        """改法是接口上的取值，加一种就意味着多一条要单独审的路。"""
        self.assertEqual(regrant.MODES, ("repair", "expire", "caps"))

    def test_non_credential_tickets_have_no_policy_to_change(self):
        """权限单 / 开账号单没有自己的那条策略，这条路对它们没有意义。"""
        p = regrant.plan(
            ticket(kind=catalog_mod.KIND_PERMISSION), tpl(), mode=regrant.MODE_REPAIR, now=NOW
        )
        self.assertFalse(p.ok)
        self.assertIn("凭证单", p.why)

    def test_a_ticket_without_a_bucket_is_refused(self):
        """没有桶名就算不出 Resource —— 猜一个出来就是凭空发权限。"""
        t = ticket()
        t["payload"] = {"prefix": PREFIX}
        p = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertFalse(p.ok)
        self.assertIn("桶名", p.why)

    def test_a_bucket_dropped_from_the_template_is_refused(self):
        """模板里把桶删掉 = 这个桶不再从这张模板发权限了，重算也不行。"""
        p = regrant.plan(
            ticket(),
            tpl(buckets=(("wuji-other-bucket", "cn-hangzhou"),)),
            mode=regrant.MODE_REPAIR,
            now=NOW,
        )
        self.assertFalse(p.ok)
        self.assertIn(BUCKET, p.why)
        self.assertIn("重新申请", p.why)


class NotBeforeTests(unittest.TestCase):
    """**生效时间只读不写，读不回来就整个拒掉。**

    拿 `now` 兜底等于把一张「尚未生效」的凭证提前生效 —— 那是扩权，而且是静默的：
    页面上什么都不会提示，策略里那个 `DateGreaterThan` 就这么被推前了。
    """

    def test_missing_not_before_is_refused(self):
        t = ticket()
        t.pop("cred_not_before")
        p = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertFalse(p.ok)
        self.assertIn("生效时间", p.why)
        self.assertEqual(p.not_before, 0.0)

    def test_zero_and_none_and_garbage_are_all_refused(self):
        """0 / None / 非数字串，三种「读不回来」的形状都不能走到重算。"""
        for bad in (0, 0.0, None, "", "unknown", "2026-09-23"):
            with self.subTest(cred_not_before=bad):
                p = regrant.plan(
                    ticket(cred_not_before=bad), tpl(), mode=regrant.MODE_REPAIR, now=NOW
                )
                self.assertFalse(p.ok)
                self.assertIn("生效时间", p.why)

    def test_now_is_never_used_as_a_fallback(self):
        """点名这条：拒绝理由里必须说清为什么不兜底，否则下一个人就会顺手补上。"""
        t = ticket()
        t.pop("cred_not_before")
        p = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertFalse(p.ok)
        self.assertIn("提前生效", p.why)

    def test_explicit_not_before_wins_over_the_ticket(self):
        """调用方从云上 `GetPolicy` 读回来的那个值，比单子上记的更可信。"""
        cloud_nb = NB - 7 * 86400
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW, not_before=cloud_nb)
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.not_before, cloud_nb)
        cond = actions_of(p.policy, ARN, with_prefix=False)["Condition"]
        self.assertEqual(
            cond["DateGreaterThan"]["acs:CurrentTime"], grants_mod.iso8601_bj(cloud_nb)
        )

    def test_explicit_not_before_of_zero_is_still_refused(self):
        """传参优先，但传个 0 进来不是「没传」，是「读回来的是 0」 —— 一样不敢改。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW, not_before=0)
        self.assertFalse(p.ok)
        self.assertIn("生效时间", p.why)

    def test_first_read_is_written_back_to_the_ticket_only_once(self):
        """第一次从云上读回来就顺手记进单子；第二次就不该再出现在回写字段里。"""
        t = ticket()
        t.pop("cred_not_before")
        first = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW, not_before=NB)
        self.assertEqual(first.fields.get("cred_not_before"), NB)
        again = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertNotIn("cred_not_before", again.fields)


class ExpireTests(unittest.TestCase):
    """**到期时间：能改，但翻不过模板那道闸。**

    不卡总时长的话，「先发 30 天、再延一次到一年」就绕过了发放时审批人批的那个上限 ——
    而那个上限正是审批时唯一被批下来的数字。
    """

    def test_expire_mode_without_a_new_value_is_refused(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_EXPIRE, now=NOW)
        self.assertFalse(p.ok)
        self.assertIn("到期时间", p.why)

    def test_a_non_timestamp_is_refused(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_EXPIRE, now=NOW, expire="下周五")
        self.assertFalse(p.ok)
        self.assertIn("时间戳", p.why)

    def test_an_expiry_in_the_past_is_refused(self):
        for bad in (NOW - 1, NOW):
            with self.subTest(expire=bad):
                p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_EXPIRE, now=NOW, expire=bad)
                self.assertFalse(p.ok)
                self.assertIn("过去", p.why)

    def test_an_expiry_before_the_start_is_refused(self):
        """还没生效的凭证，新到期落在生效之前 —— 这样的凭证一秒都用不了。"""
        future_nb = NOW + 10 * 86400
        p = regrant.plan(
            ticket(cred_not_before=future_nb, expires_at_ts=future_nb + 86400),
            tpl(),
            mode=regrant.MODE_EXPIRE,
            now=NOW,
            expire=NOW + 3600,
        )
        self.assertFalse(p.ok)
        self.assertIn("生效时间", p.why)

    def test_total_window_may_not_exceed_the_template_cap(self):
        """从**生效**算起，不是从现在算起 —— 从现在算的话每延一次都白送已经用掉的那段。"""
        cap = 24 * 30
        p = regrant.plan(
            ticket(),
            tpl(max_hours=cap),
            mode=regrant.MODE_EXPIRE,
            now=NOW,
            expire=NB + cap * HOUR + 2,
        )
        self.assertFalse(p.ok)
        self.assertIn(str(cap), p.why)
        self.assertIn("重新申请", p.why)

    def test_exactly_the_cap_is_allowed(self):
        """边界放行：卡在恰好等于上限会让「续到上限」这个正常操作报错。"""
        cap = 24 * 30
        exact = NB + cap * HOUR
        p = regrant.plan(
            ticket(), tpl(max_hours=cap), mode=regrant.MODE_EXPIRE, now=NOW, expire=exact
        )
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.expire, exact)

    def test_the_one_second_slack_is_slack_not_a_loophole(self):
        """源码里那个 `+1` 是留给时间戳取整的抖动的，不是一格可以往上加的额度。"""
        cap = 24 * 30
        ok = regrant.plan(
            ticket(),
            tpl(max_hours=cap),
            mode=regrant.MODE_EXPIRE,
            now=NOW,
            expire=NB + cap * HOUR + 1,
        )
        self.assertTrue(ok.ok, ok.why)
        over = regrant.plan(
            ticket(),
            tpl(max_hours=cap),
            mode=regrant.MODE_EXPIRE,
            now=NOW,
            expire=NB + cap * HOUR + 61,
        )
        self.assertFalse(over.ok)

    def test_shortening_is_always_allowed(self):
        """收窄有效期不需要过闸 —— 它只会让凭证更早失效。"""
        shorter = NOW + 86400
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_EXPIRE, now=NOW, expire=shorter)
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.fields["expires_at_ts"], shorter)
        self.assertEqual(p.what_changes(), ["到期时间"])
        cond = actions_of(p.policy, ARN, with_prefix=False)["Condition"]
        self.assertEqual(cond["DateLessThan"]["acs:CurrentTime"], grants_mod.iso8601_bj(shorter))

    def test_expire_mode_does_not_touch_caps(self):
        """只改到期就只改到期：caps 原样带过去，回写字段里不该出现权限。"""
        p = regrant.plan(
            ticket(cred_caps=["list"]),
            tpl(),
            mode=regrant.MODE_EXPIRE,
            now=NOW,
            expire=NOW + 86400,
        )
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.caps, ("list",))
        self.assertNotIn("cred_caps", p.fields)

    def test_repair_keeps_the_window_untouched(self):
        """repair 零参数、零扩权面：窗一秒都不动。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertTrue(p.ok, p.why)
        self.assertEqual((p.not_before, p.expire), (NB, EXP))
        self.assertNotIn("expires_at_ts", p.fields)


class CapsWithinTemplateTests(unittest.TestCase):
    """**caps 不许超出模板。** 模板 caps = 那张飞书批条批准的范围；
    面板单方面扩大就是绕过审批 —— 审批人再也没机会看见这次扩权。"""

    def test_widening_beyond_the_template_is_refused_and_names_the_capability(self):
        """理由里要点名是哪一项超了，否则管理员只能逐个试。"""
        p = regrant.plan(
            ticket(),
            tpl(caps=("list", "download")),
            mode=regrant.MODE_CAPS,
            now=NOW,
            caps=["list", "download", "write"],
        )
        self.assertFalse(p.ok)
        self.assertIn(catalog_mod.CAP_LABELS["write"], p.why)
        self.assertNotIn(catalog_mod.CAP_LABELS["list"], p.why)
        self.assertIn("重新申请", p.why)

    def test_every_capability_over_the_line_is_named(self):
        p = regrant.plan(
            ticket(),
            tpl(caps=("list",)),
            mode=regrant.MODE_CAPS,
            now=NOW,
            caps=["list", "download", "write"],
        )
        self.assertFalse(p.ok)
        for cap in ("download", "write"):
            self.assertIn(catalog_mod.CAP_LABELS[cap], p.why)

    def test_narrowing_is_allowed(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_CAPS, now=NOW, caps=["list"])
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.caps, ("list",))
        self.assertEqual(p.fields["cred_caps"], ["list"])
        self.assertEqual(p.what_changes(), ["权限"])

    def test_widening_back_up_to_the_template_is_allowed(self):
        """收窄过以后再回到模板范围内，不算扩权 —— 那本来就是批过的。"""
        p = regrant.plan(
            ticket(cred_caps=["list"]),
            tpl(),
            mode=regrant.MODE_CAPS,
            now=NOW,
            caps=["list", "download"],
        )
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.caps, ("list", "download"))

    def test_caps_mode_without_new_caps_is_refused(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_CAPS, now=NOW)
        self.assertFalse(p.ok)
        self.assertIn("能力集", p.why)

    def test_an_empty_cap_set_is_refused(self):
        """清空权限不是「改权限」，那是撤销 —— 走另一条路，别让它变成一把没用的活凭证。

        拒绝这一步由 `grants.check_caps` 自己完成，所以理由就是它那句
        「至少要选一项权限」。这里把来源钉住：谁哪天在 regrant 里补一句更贴切的
        提示，会先看到这条红，而不是写完才发现那句话永远到不了。
        """
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_CAPS, now=NOW, caps=[])
        self.assertFalse(p.ok)
        self.assertIn("至少要选一项权限", p.why)

    def test_unknown_capability_names_never_reach_the_policy(self):
        """造出来的能力名只会被丢掉，绝不能原样落进策略文档。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_CAPS, now=NOW, caps=["list", "oss:*"])
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.caps, ("list",))
        self.assertNotIn("oss:*", allow_actions(p.policy))

    def test_caps_mode_does_not_touch_the_window(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_CAPS, now=NOW, caps=["list"])
        self.assertEqual((p.not_before, p.expire), (NB, EXP))
        self.assertNotIn("expires_at_ts", p.fields)


class EffectiveCapsTests(unittest.TestCase):
    """**单子上的 `cred_caps` 优先于模板 caps。** 这条最容易被写反，而写反的后果是静默的：

    管理员刚把权限改窄，模板没变；下一次「无害的重算」再去读模板，
    就把刚收掉的权限悄悄放回去了 —— 页面上显示的是「repair」，实际发生的是扩权。
    """

    def test_ticket_caps_beat_the_template(self):
        self.assertEqual(regrant.effective_caps({"cred_caps": ["list"]}, tpl()), ("list",))

    def test_template_caps_are_only_the_fallback(self):
        """历史单子没有这个字段，那时候它确实等于模板 caps。"""
        for t in ({}, {"cred_caps": []}, {"cred_caps": None}):
            with self.subTest(ticket=t):
                self.assertEqual(
                    regrant.effective_caps(t, tpl(caps=("list", "download"))),
                    ("list", "download"),
                )

    def test_ticket_caps_are_normalised_to_the_canonical_order(self):
        got = regrant.effective_caps({"cred_caps": ["write", "list"]}, tpl())
        self.assertEqual(got, ("list", "write"))

    def test_narrowing_then_repairing_does_not_rebound(self):
        """全流程复现那条最危险的顺序：改窄 → 回写单子 → 再 repair 一次。"""
        wide = tpl(caps=("list", "download", "write"))
        narrowed = regrant.plan(ticket(), wide, mode=regrant.MODE_CAPS, now=NOW, caps=["list"])
        self.assertEqual(narrowed.fields["cred_caps"], ["list"])

        # 把回写字段落到单子上，然后跑一次「无害的重算」
        after = ticket(**narrowed.fields)
        again = regrant.plan(after, wide, mode=regrant.MODE_REPAIR, now=NOW)

        self.assertTrue(again.ok, again.why)
        self.assertEqual(again.caps, ("list",))
        self.assertNotIn("cred_caps", again.fields)  # 没变就不该回写
        self.assertEqual(again.policy, narrowed.policy)
        allowed = allow_actions(again.policy)
        self.assertNotIn("oss:GetObject", allowed)  # 下载没有回弹
        self.assertNotIn("oss:PutObject", allowed)  # 上传没有回弹

    def test_a_corrupt_cred_caps_is_a_refusal_not_a_traceback(self):
        """单子上的 `cred_caps` 脏了（人工回填、迁移脚本写歪）也要给一句人话。

        `effective_caps` 走的是 `grants.check_caps`，它对认不出的能力集是**抛异常**的；
        异常穿出 `plan()` 就等于把管理员推回去看 traceback —— 而他这时候需要知道的是
        「这张单要人工核对」，不是栈帧。
        """
        for bad in (["bogus"], ["LIST"], ["oss:GetObject"]):
            with self.subTest(cred_caps=bad):
                p = regrant.plan(ticket(cred_caps=bad), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
                self.assertFalse(p.ok)
                self.assertIn("权限", p.why)
                self.assertIn("人工核对", p.why)
                self.assertEqual(p.policy, {})

    def test_a_corrupt_cred_caps_blocks_every_mode(self):
        """包括 caps 模式：读不出「现在是什么」就算不出「变了没有」，
        回写一个空的 `cred_caps` 差异比拒绝更糟。"""
        for mode, kw in (
            (regrant.MODE_EXPIRE, {"expire": NOW + 86400}),
            (regrant.MODE_CAPS, {"caps": ["list"]}),
        ):
            with self.subTest(mode=mode):
                p = regrant.plan(ticket(cred_caps=["bogus"]), tpl(), mode=mode, now=NOW, **kw)
                self.assertFalse(p.ok)
                self.assertIn("人工核对", p.why)

    def test_repair_uses_the_template_when_the_ticket_never_recorded_caps(self):
        p = regrant.plan(
            ticket(), tpl(caps=("list", "download")), mode=regrant.MODE_REPAIR, now=NOW
        )
        self.assertEqual(p.caps, ("list", "download"))
        self.assertNotIn("cred_caps", p.fields)


class ChangedTests(unittest.TestCase):
    """**`changed` 决定要不要真去云上写一个新版本。** 阿里的策略版本上限只有 5 个，
    白写一次就白吃一格 —— 而 repair 在「代码没变过」时算出来的就是和云上一模一样的文档。"""

    def test_identical_document_is_not_a_change(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW, before=doc())
        self.assertTrue(p.ok, p.why)
        self.assertFalse(p.changed)
        self.assertIn("不用改", p.summary)
        self.assertEqual(p.what_changes(), [])

    def test_unknown_before_counts_as_changed(self):
        """读不到云上那份就只能当作要写 —— 白写一格版本，好过漏掉一次该打的补丁。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertTrue(p.changed)
        self.assertEqual(p.before, {})

    def test_an_empty_before_is_not_the_same_as_no_before(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW, before={})
        self.assertTrue(p.changed)

    def test_a_stale_document_is_a_change(self):
        """今天那个真实故障：云上那份是缺了动作的旧文档。"""
        stale = doc()
        bucket_stmt = actions_of(stale, ARN, with_prefix=False)
        bucket_stmt["Action"] = [a for a in bucket_stmt["Action"] if a != "oss:GetBucketLocation"]
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW, before=stale)
        self.assertTrue(p.changed)
        self.assertEqual(p.before, stale)

    def test_empty_what_changes_does_not_mean_nothing_happened(self):
        """**空列表 ≠ 没变。** repair 时文档换了、窗和 caps 都没动，
        所以 `what_changes()` 是空的而 `changed` 是 True —— 谁把「空就是没事」写进
        调用方，就会跳过那次本该打的补丁。"""
        stale = doc()
        stale["Statement"] = stale["Statement"][:1]
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW, before=stale)
        self.assertTrue(p.changed)
        self.assertEqual(p.what_changes(), [])
        self.assertNotIn("不用改", p.summary)
        self.assertTrue(p.summary.strip())

    def test_summary_lists_both_axes_when_both_move(self):
        t = ticket(cred_caps=["list", "download", "write"])
        narrowed = regrant.plan(t, tpl(), mode=regrant.MODE_CAPS, now=NOW, caps=["list"])
        self.assertEqual(narrowed.summary, "权限")
        later = regrant.plan(t, tpl(), mode=regrant.MODE_EXPIRE, now=NOW, expire=NOW + 86400)
        self.assertEqual(later.summary, "到期时间")


class FieldsTests(unittest.TestCase):
    """**回写字段里只放真的变了的。** 多写一个字段，台账上就多一次没发生过的变更记录。"""

    def test_a_plain_repair_writes_nothing_back(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.fields, {})

    def test_the_first_repair_records_the_start_time_and_nothing_else(self):
        t = ticket()
        t.pop("cred_not_before")
        p = regrant.plan(t, tpl(), mode=regrant.MODE_REPAIR, now=NOW, not_before=NB)
        self.assertEqual(p.fields, {"cred_not_before": NB})
        self.assertNotIn("expires_at_ts", p.fields)
        self.assertNotIn("cred_caps", p.fields)

    def test_setting_the_same_expiry_is_not_a_field_change(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_EXPIRE, now=NOW, expire=EXP)
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.fields, {})

    def test_setting_the_same_caps_is_not_a_field_change(self):
        p = regrant.plan(
            ticket(cred_caps=["list", "write"]),
            tpl(),
            mode=regrant.MODE_CAPS,
            now=NOW,
            caps=["write", "list"],
        )
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.fields, {})


class PolicyDocumentTests(unittest.TestCase):
    """算出来的文档用的是**当前代码**的动作表 —— 这正是整个模块的存在理由。

    文档形状（桶信息条不叠前缀、每条叠时间窗、末尾 Deny）在
    `test_delivery_credentials.py` 里已经钉过，这里只做一遍不重复的抽查。
    """

    def test_todays_two_missing_actions_are_in_the_repaired_document(self):
        """2026-09-23 那把凭证缺的正是这两个：
        `GetBucketLocation`（S3 兼容客户端建连先探地域）和
        `GetObjectVersion`（桶开了版本控制，带 version id 的 GET/HEAD 走它）。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertTrue(p.ok, p.why)
        info = actions_of(p.policy, ARN, with_prefix=False)["Action"]
        download = actions_of(p.policy, OBJ_ARN, with_prefix=False)["Action"]
        self.assertIn("oss:GetBucketLocation", info)
        self.assertIn("oss:GetObjectVersion", download)

    def test_repair_matches_what_the_issuing_path_would_build_today(self):
        """重算 = 「用今天的代码重发一遍这张单」，不多不少。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertEqual(p.policy, doc())

    def test_the_bucket_info_statement_carries_no_prefix_condition(self):
        """叠了 `oss:Prefix` 的桶级请求会被服务端判否 —— 表现是「凭证发了但什么也干不了」。"""
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        info = actions_of(p.policy, ARN, with_prefix=False)
        self.assertNotIn("StringLike", info["Condition"])
        listing = actions_of(p.policy, ARN, with_prefix=True)
        self.assertEqual(listing["Condition"]["StringLike"]["oss:Prefix"], [PREFIX, PREFIX + "*"])

    def test_every_allow_carries_the_window_and_the_deny_closes_the_document(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        allows = [s for s in p.policy["Statement"] if s["Effect"] == "Allow"]
        self.assertEqual(len(allows), 4)  # 桶信息 + 清单 + 下载 + 上传
        for st in allows:
            self.assertEqual(
                st["Condition"]["DateGreaterThan"]["acs:CurrentTime"], grants_mod.iso8601_bj(NB)
            )
            self.assertEqual(
                st["Condition"]["DateLessThan"]["acs:CurrentTime"], grants_mod.iso8601_bj(EXP)
            )
        self.assertEqual(p.policy["Statement"][-1]["Effect"], "Deny")

    def test_narrowed_caps_drop_the_matching_statements(self):
        p = regrant.plan(ticket(), tpl(), mode=regrant.MODE_CAPS, now=NOW, caps=["list"])
        kinds = [(s["Effect"], s["Resource"][0]) for s in p.policy["Statement"]]
        self.assertNotIn(("Allow", OBJ_ARN), kinds)

    def test_a_bad_prefix_on_the_ticket_is_a_refusal_not_a_crash(self):
        """目录前缀会被带进 Resource，脏值必须在这里被挡住。"""
        p = regrant.plan(
            ticket(payload={"prefix": "../../etc/"}), tpl(), mode=regrant.MODE_REPAIR, now=NOW
        )
        self.assertFalse(p.ok)
        self.assertIn("目录前缀", p.why)

    def test_a_whole_bucket_ticket_keeps_working(self):
        """目录留空 = 整桶（模板另有 `whole_bucket` 那道闸，不归这里管）。"""
        p = regrant.plan(ticket(payload={"prefix": ""}), tpl(), mode=regrant.MODE_REPAIR, now=NOW)
        self.assertTrue(p.ok, p.why)
        self.assertEqual(p.policy, doc(prefix=""))


class StatementDiffTests(unittest.TestCase):
    """**按 (Effect, Resource, 有没有前缀条件) 配对，不按下标。**

    statement 的顺序由 caps 决定，改 caps 时中间会少一条；按下标对的话后面全部错位，
    页面上看起来像是每一条都变了 —— 管理员于是学会了忽略这个 diff。
    """

    def test_a_statement_removed_from_the_middle_does_not_shift_the_rest(self):
        """去掉「清单」这条：它在中间，后面两条原地不动。按下标对会报出三条假变更。"""
        before = doc(("list", "download", "write"))
        after = doc(("download", "write"))
        got = regrant.statement_diff(before, after)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["how"], "删")
        self.assertEqual(got[0]["resource"], [ARN])
        self.assertIn("oss:ListObjects", got[0]["before"])
        self.assertEqual(got[0]["after"], [])

    def test_an_added_statement_shows_up_as_added(self):
        got = regrant.statement_diff(doc(("download", "write")), doc(("list", "download", "write")))
        self.assertEqual([d["how"] for d in got], ["加"])
        self.assertEqual(got[0]["before"], [])
        self.assertIn("oss:ListObjects", got[0]["after"])

    def test_a_reworded_statement_shows_up_as_modified(self):
        """动作表变了但 Resource 没变 —— 正是今天那个补丁的形状。"""
        stale = doc(("list",))
        actions_of(stale, ARN, with_prefix=False)["Action"] = ["oss:GetBucketInfo"]
        got = regrant.statement_diff(stale, doc(("list",)))
        self.assertEqual([d["how"] for d in got], ["改"])
        self.assertEqual(got[0]["before"], ["oss:GetBucketInfo"])
        self.assertIn("oss:GetBucketLocation", got[0]["after"])

    def test_changing_only_the_window_reports_every_touched_statement_as_modified(self):
        got = regrant.statement_diff(doc(("list",)), doc(("list",), expire=EXP + 86400))
        self.assertTrue(got)
        self.assertEqual({d["how"] for d in got}, {"改"})
        # 条件变了、动作没变：before/after 的动作表应当一致
        for d in got:
            self.assertEqual(d["before"], d["after"])

    def test_identical_documents_produce_no_diff(self):
        self.assertEqual(regrant.statement_diff(doc(), doc()), [])

    def test_missing_documents_are_tolerated(self):
        """云上那份读不回来时页面还要能渲染，不能在这里炸。"""
        self.assertEqual([d["how"] for d in regrant.statement_diff({}, doc(("list",)))], ["加"] * 3)
        self.assertEqual(regrant.statement_diff(None, None), [])
        self.assertEqual(regrant.statement_diff({"Statement": None}, {}), [])


class SameKeyGroupTests(unittest.TestCase):
    """**一个键下可能不止一条 statement。**

    「下载」和「上传」在 `build_policy` 里都是对象级 ARN、都不叠 `oss:Prefix`，
    (Effect, Resource, 有没有前缀条件) 三元组完全相同。早先这里用 `{key: st}` 建字典，
    上传那条把下载那条覆盖掉，于是「收掉下载权限」在预演页上显示成**「没有变化」**。
    修法是组内按动作集重合度贪心配对，下面这一组就是钉它的。
    """

    def test_dropping_download_must_not_look_like_nothing_changed(self):
        """回归锁：去掉「下载」必须报出来，且报的是删而不是别的什么。

        管理员看着「没有变化」点下确认、使用方第二天丢了下载权 —— 这个页面存在的
        全部意义就是别让这件事发生。
        """
        got = regrant.statement_diff(doc(("list", "download", "write")), doc(("list", "write")))
        self.assertEqual([d["how"] for d in got], ["删"], f"下载语句的消失没被报出来：{got}")
        self.assertEqual(got[0]["before"], ["oss:GetObject", "oss:GetObjectVersion"])
        self.assertEqual(got[0]["after"], [])
        self.assertEqual(got[0]["resource"], [OBJ_ARN])

    def test_dropping_upload_is_reported_too(self):
        """反过来去掉「上传」同样要报 —— 被覆盖掉的是哪一条取决于文档里的先后。"""
        got = regrant.statement_diff(doc(("list", "download", "write")), doc(("list", "download")))
        self.assertEqual([d["how"] for d in got], ["删"], got)
        self.assertIn("oss:PutObject", got[0]["before"])

    def test_download_swapped_for_upload_is_one_removal_and_one_addition(self):
        """零重合的两条**不配对**：硬配成一条「改」会把「删下载 + 加上传」说成
        「动作变了」，两件事被压成一件，其中一半就此隐形。"""
        got = regrant.statement_diff(doc(("list", "download")), doc(("list", "write")))
        self.assertEqual([d["how"] for d in got], ["删", "加"], got)
        removed, added = got
        self.assertEqual(removed["before"], ["oss:GetObject", "oss:GetObjectVersion"])
        self.assertEqual(removed["after"], [])
        self.assertEqual(added["before"], [])
        self.assertIn("oss:PutObject", added["after"])

    def test_the_real_incident_shape_pairs_download_with_download(self):
        """2026-09-23 那个补丁的形状：下载条多了 `GetObjectVersion`，上传条一字未动。

        重合度配对必须把「老下载」配给「新下载」（重合 1）而不是配给上传（重合 0），
        否则页面会说「下载没了、上传变了」—— 和真相正好错开。
        """
        stale = doc(("download", "write"))
        actions_of(stale, OBJ_ARN, with_prefix=False)["Action"] = ["oss:GetObject"]
        got = regrant.statement_diff(stale, doc(("download", "write")))
        self.assertEqual([d["how"] for d in got], ["改"], got)
        self.assertEqual(got[0]["before"], ["oss:GetObject"])
        self.assertEqual(got[0]["after"], ["oss:GetObject", "oss:GetObjectVersion"])

    def test_three_statements_in_one_group_pair_by_overlap_not_by_position(self):
        """同一键下三条：**位置全不一样**，配对只能看动作集。

        按位置对的话 `a`→`c`、`b`→`a`、`c`→`d`，三条全是假的「改」。

        顺带钉住**那个 `break` 的真实语义**（容易被误读成「碰到一对零重合就收工」，
        进而被好心人「修」坏）：`best` 是在**当前剩余的全部 (i, j) 组合**上取 max 的，
        所以退出条件是「剩下所有对里最高的重合度都是 0」—— 那时确实一对都不该配。
        这一组就是反例：`b` 和 `c'` 零重合、而且排在 `c↔c'` 前面，配对照样往下走，
        先把 `a↔a`、`c↔c'` 配掉，只剩真正没有对手的 `b`、`d` 才拆成删+加。
        """
        before = {"Statement": [obj_stmt("a1", "a2"), obj_stmt("b1"), obj_stmt("c1", "c2")]}
        after = {"Statement": [obj_stmt("c1", "c2", "c3"), obj_stmt("a1", "a2"), obj_stmt("d1")]}
        got = regrant.statement_diff(before, after)
        # a 两边一模一样（位置不同）→ 不该出现；c 重合 2 → 改；b 无对手 → 删；d 无对手 → 加
        self.assertEqual(
            [(d["how"], d["before"], d["after"]) for d in got],
            [
                ("改", ["c1", "c2"], ["c1", "c2", "c3"]),
                ("删", ["b1"], []),
                ("加", [], ["d1"]),
            ],
        )

    def test_a_group_with_no_overlap_at_all_keeps_a_stable_order(self):
        """全零重合 → 全部拆成删+加。**输出顺序要稳**：页面直接按这个顺序渲染，
        顺序一跳，管理员每刷新一次看到的「变更清单」就换个样子。"""
        before = {"Statement": [obj_stmt("x1"), obj_stmt("x2")]}
        after = {"Statement": [obj_stmt("y1"), obj_stmt("y2")]}
        rows = [(d["how"], d["before"], d["after"]) for d in regrant.statement_diff(before, after)]
        self.assertEqual(
            rows,
            [
                ("删", ["x1"], []),
                ("删", ["x2"], []),
                ("加", [], ["y1"]),
                ("加", [], ["y2"]),
            ],
        )
        # 同样的输入跑几遍必须逐字一样（配对里有 max()，平手时的取值不能随集合序漂）
        for _ in range(5):
            again = regrant.statement_diff(before, after)
            self.assertEqual([(d["how"], d["before"], d["after"]) for d in again], rows)

    def test_identical_statements_in_a_group_produce_nothing(self):
        """组内两条原样不动 —— 一条都不该报，哪怕它们的键相同。"""
        self.assertEqual(
            regrant.statement_diff(doc(("download", "write")), doc(("download", "write"))), []
        )


class NoCloudCallTests(unittest.TestCase):
    """**这个模块一个云调用都没有** —— 所以它的每一条拒绝理由都能被单测钉住，
    不用起一个假云。哪天有人往里面 import 了会出网的东西，这条先响。"""

    def test_it_only_imports_sibling_pure_logic_modules(self):
        src = Path(inspect.getsourcefile(regrant)).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                imported.update(a.name for a in node.names)  # from . import x
        self.assertEqual(imported - {"__future__", "dataclasses", "typing"}, {"catalog", "grants"})

    def test_plan_never_reads_the_clock_itself(self):
        """`now` 只能由调用方传进来：自己读钟的纯逻辑没法测「到期那一刻」。"""
        sig = inspect.signature(regrant.plan)
        self.assertEqual(sig.parameters["now"].default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
