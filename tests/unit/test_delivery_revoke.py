"""管理员收权：面板上直接撤掉子账号身上的策略和用户组。

**这是整个面板里唯一一条不经审批就改云上权限的通道**，所以这份用例按「撤错了会怎样」
来组织，而不是按函数来组织：

  · 四条拒绝（面板身份 / 资源组级 / 面板工单发的 / 本来就没挂）逐条单独锁。
    每条都对应一个具体事故，去掉任何一条都必须有用例变红 —— 合在一条用例里锁的话，
    删掉其中三条判断仍然只红一条，看的人会以为只坏了一处。
  · 「最后一个管理员」两个方向都锁：不确认时拦住、确认后放行、还有别人时不该弹。
    只锁前者的话，把它改成硬拒（合法操作从此做不了）照样全绿。
  · `remaining`（撤完还剩什么）单独锁：这是界面上给人做判断的唯一依据，
    算错了人会基于错的信息点「执行」。
  · 接口层锁的是「预演绝不能真撤」和「一条失败不挡住其余」——
    前者错了等于没有预演，后者错了等于撤到一半停住、云上和台账都对不上。

离线，数据全部虚构，云接口用假 transport / 假执行器。
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from delivery import inventory, revoke
from delivery import review as review_mod
from delivery.clouds import aliyun, volcano
from delivery.feishu import FeishuUser
from delivery.provision import AliyunExecutor, ProvisionError, VolcanoExecutor
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _WebSession, make_handler

ACC = "1000000000000001"
USER = "heguanqi"

ADMIN = "AdministratorAccess"
OSS = "AliyunOSSFullAccess"
RAM = "AliyunRAMFullAccess"
AUTO = "wuji-oss-auto-x"
GRP = "wuji_Algorithm"


def pol(name: str, kind: str = "System") -> dict:
    """云上返回的一条账号级授权。"""
    return {"PolicyName": name, "PolicyType": kind}


def want(*names) -> list:
    """把 `"策略名"` / `("group", "组名")` 写法统一成 `Item`。"""
    out = []
    for n in names:
        if isinstance(n, tuple):
            out.append(revoke.Item(n[0], n[1]))
        else:
            out.append(revoke.Item("policy", n))
    return out


def codes(plan) -> list:
    return [(i.name, code) for i, code in plan.refused]


def names(plan) -> list:
    return [i.name for i in plan.remove]


ATTACHED = [pol(ADMIN), pol(OSS), pol(RAM), pol(AUTO, "Custom")]


def make_plan(**over):
    kw = dict(
        user=USER,
        attached=ATTACHED,
        groups=[GRP],
        wanted=want(OSS),
        admin_holders={USER, "other"},
        panel_granted=set(),
        confirm_last_admin=False,
    )
    kw.update(over)
    return revoke.plan(**kw)


# ── 纯逻辑：四条拒绝 ─────────────────────────────────────────────────────


class RefusePanelTests(unittest.TestCase):
    """面板自己的身份一条都撤不了 —— 撤了面板当场瘫，而且**没法自己加回来**
    （加回来要 `ram:AttachPolicyToUser`，那正是执行身份的策略给的）。"""

    def test_panel_identities_cannot_lose_anything(self):
        for who in ("panel-executor", "panel-collector", "panel-issuer"):
            plan = make_plan(
                user=who,
                wanted=want(OSS, RAM, AUTO, ("group", GRP)),
                admin_holders={"someone-else"},
            )
            self.assertEqual(names(plan), [], who)
            self.assertEqual({c for _, c in plan.refused}, {revoke.REFUSE_PANEL}, who)
            self.assertFalse(plan.ok, who)

    def test_panel_prefix_beats_every_other_verdict(self):
        """哪怕选中的是一条「本来就没挂」的策略，也要报「这是面板身份」。
        先报 ABSENT 的话，管理员会以为「换一条挂着的就能撤」，然后去撤真的那条。"""
        plan = make_plan(user="panel-executor", wanted=want("不存在的策略"))
        self.assertEqual(codes(plan), [("不存在的策略", revoke.REFUSE_PANEL)])

    def test_prefix_match_not_exact_match(self):
        """面板身份是按前缀认的：以后加 `panel-reader` 之类不用回来改代码。"""
        self.assertTrue(revoke.is_panel_identity("panel-whatever-new"))
        self.assertFalse(revoke.is_panel_identity(USER))
        self.assertFalse(revoke.is_panel_identity(""))
        self.assertFalse(revoke.is_panel_identity(None))
        # 「名字里带 panel-」不算：只有开头才是面板身份
        self.assertFalse(revoke.is_panel_identity("x-panel-executor"))

    def test_case_and_spaces_do_not_get_past_it(self):
        """登录名从云上、快照、请求体三处来，大小写和空格都可能不一致。

        **这一行在火山侧是唯一的一道门**：阿里的执行身份策略里另有对
        `user/panel-*` 的 Deny 兜底，火山那份是 `Resource: ["*"]`，
        一条 self-deny 都没有 —— 这里漏过去就真的撤下去了。
        """
        for who in ("Panel-Executor", " panel-issuer ", "PANEL-COLLECTOR", "\tpanel-x\n"):
            self.assertTrue(revoke.is_panel_identity(who), who)
            plan = make_plan(user=who, wanted=want(OSS), admin_holders={"other"})
            self.assertEqual(codes(plan), [(OSS, revoke.REFUSE_PANEL)], who)

    def test_normal_user_is_not_protected(self):
        """反向：普通用户不能被这条拦住，否则收权功能整个失效（且悄无声息）。"""
        plan = make_plan(wanted=want(OSS))
        self.assertEqual(names(plan), [OSS])
        # 归一化别过头：这些不是面板身份
        for who in ("panelexecutor", "pane l-x", "x panel-executor", "user-panel-"):
            self.assertFalse(revoke.is_panel_identity(who), who)


class RefuseProtectedTests(unittest.TestCase):
    """护栏策略（含 Deny）撤不得 —— **「只减不增就没有提权风险」这句话是错的**。

    RAM 里 Deny 优先且跨策略生效，所以撤掉一条含 Deny 的策略就是提权：
    `identity/deny-pai-delete.example.json` 那条一旦被勾掉，那个人的删除能力就回来了，
    而界面上显示的是「会撤掉 1 条」，看起来在收紧。
    """

    def test_protected_prefixes_are_refused(self):
        for name in ("wuji-deny-pai-delete", "deny-everything", "WUJI-Deny-Pai-Delete"):
            plan = make_plan(
                attached=[pol(name, "Custom")], wanted=want(name), admin_holders={"other"}
            )
            self.assertEqual(codes(plan), [(name, revoke.REFUSE_PROTECTED)], name)

    def test_the_names_are_prefixes_not_globs(self):
        """**名单里不要写 `*`**：用的是 `startswith`，写了星号会去匹配字面量星号，
        于是永远不命中 —— 而且一声不响，看起来像配好了。

        所以两头都锁：`wuji-deny-*` 这种**带星号的策略名**不该被当成通配命中
        （它只是一个名字很怪的普通策略），而真名 `wuji-deny-pai-delete` 必须命中。
        """
        plan = make_plan(
            attached=[pol("wuji-deny-pai-delete", "Custom")],
            wanted=want("wuji-deny-pai-delete"),
            admin_holders={"other"},
        )
        self.assertEqual([c for _, c in plan.refused], [revoke.REFUSE_PROTECTED])
        self.assertNotIn("*", "".join(revoke.PROTECTED), "PROTECTED 里写了星号就永远不命中")

    def test_only_the_beginning_counts(self):
        """名字中间带 `deny-` 的不算护栏 —— 否则一条普普通通的
        `team-deny-list-reader` 会永远撤不掉，而没人知道为什么。"""
        for name in ("team-deny-list-reader", "no-deny", "denylist-reader"):
            plan = make_plan(
                attached=[pol(name, "Custom")], wanted=want(name), admin_holders={"other"}
            )
            self.assertEqual(names(plan), [name], name)

    def test_protected_groups_are_refused_too(self):
        """**LOW-A**：护栏策略挂在**组**上时，「把人移出这个组」绕过了按策略名的判断。
        和「移出发超管的组不弹 last_admin」是同一种不对称 —— 单撤策略撤不动，
        换个入口把人从组里摘出来就成了。"""
        plan = make_plan(
            groups=[GRP, "wuji-guardrail"],
            wanted=want(("group", "wuji-guardrail")),
            protected_groups={"wuji-guardrail"},
        )
        self.assertEqual(names(plan), [])
        self.assertEqual(codes(plan), [("wuji-guardrail", revoke.REFUSE_PROTECTED)])

    def test_ordinary_groups_are_not_refused(self):
        """反向：没挂护栏的组照常能撤，否则这个功能对组整个失效。"""
        plan = make_plan(wanted=want(("group", GRP)), protected_groups={"别的组"})
        self.assertEqual(names(plan), [GRP])

    def test_protected_beats_absent(self):
        """护栏策略已经被人从控制台摘掉了（云上没有了）：仍要说「这是护栏」。
        报「本来就没有」会让人以为一切正常 —— 而那条 Deny 确实已经不在了，
        该去查是谁摘的。"""
        plan = make_plan(wanted=want("wuji-deny-pai-delete"), admin_holders={"other"})
        self.assertEqual(codes(plan), [("wuji-deny-pai-delete", revoke.REFUSE_PROTECTED)])


class RefuseDenyTests(unittest.TestCase):
    """读策略正文判 Deny（`deny_of`）—— 名单是人维护的、会漏，这一层才是真判据。"""

    def plan_with(self, value, name=AUTO):
        return make_plan(
            wanted=want(name),
            deny_of={name: value},
            admin_holders={"other"},
        )

    def test_deny_inside_refuses(self):
        plan = self.plan_with(True)
        self.assertEqual(names(plan), [])
        self.assertEqual(codes(plan), [(AUTO, revoke.REFUSE_DENY)])

    def test_unreadable_also_refuses(self):
        """**「读不出来」当成「没有 Deny」是这里最危险的默认值。**
        一条读不了正文的策略会被当成普通 Allow 放行 —— 而读不出来的原因
        （权限不够、策略被删到一半、接口抖）没有一个能推出「它是安全的」。"""
        plan = self.plan_with(None)
        self.assertEqual(names(plan), [])
        self.assertEqual(codes(plan), [(AUTO, revoke.REFUSE_DENY)])

    def test_no_deny_lets_it_through(self):
        self.assertEqual(names(self.plan_with(False)), [AUTO])

    def test_policies_nobody_asked_about_are_not_treated_as_unreadable(self):
        """**反向锁（这个坑真踩过）**：判据必须是「问过、且答案不是 False」，
        不能写成 `deny_of.get(name) is not False`。

        调用方只问自定义策略（系统策略都是纯 Allow，问 60 次会让预演慢到没人点），
        所以系统策略压根不在这张表里。用 `.get()` 的那一版把它们全判成「读不出来」，
        于是**一条都撤不动**，而给出的理由是「这条策略里有 Deny」—— 完全的误导。
        """
        plan = make_plan(
            wanted=want(OSS, RAM),
            deny_of={AUTO: True},  # 只问过这一条自定义策略
            admin_holders={"other"},
        )
        self.assertEqual(names(plan), [OSS, RAM])
        self.assertEqual(plan.refused, [])

    def test_empty_or_missing_table_is_not_a_blanket_refusal(self):
        """没有这张表（老调用方、或者这朵云还没接 has_deny）时照常放行 ——
        否则这个功能上线当天就整个失效，而失败模式是「全都撤不了」，
        看起来像权限问题，没人会往这里查。"""
        for table in (None, {}):
            plan = make_plan(wanted=want(OSS), deny_of=table, admin_holders={"other"})
            self.assertEqual(names(plan), [OSS], table)

    def test_groups_are_not_checked_against_the_policy_table(self):
        """同名的组不该被策略表连累 —— 组挂没挂护栏由 `protected_groups` 判。"""
        plan = make_plan(groups=[AUTO], wanted=want(("group", AUTO)), deny_of={AUTO: True})
        self.assertEqual(names(plan), [AUTO])


class RefuseScopedTests(unittest.TestCase):
    """资源组级授权撤不掉（`ram:DetachPolicyFromUser` 只管账号级）。

    **必须显式报出来**：少撤一条资源组级的 `AdministratorAccess`，和没撤是一样的，
    而界面上却显示「已撤掉」。列了却撤不掉比不列更误导。
    """

    def test_resource_group_scoped_is_refused(self):
        for name in (
            f"{ADMIN} @资源组:rg-xxx",
            "AliyunOSSFullAccess@rg-2",
            "某策略（资源组级）",
        ):
            plan = make_plan(wanted=want(name), admin_holders={"other"})
            self.assertEqual(codes(plan), [(name, revoke.REFUSE_SCOPED)], name)

    def test_scoped_groups_too(self):
        plan = make_plan(wanted=want(("group", "组@资源组:rg-1")))
        self.assertEqual([c for _, c in plan.refused], [revoke.REFUSE_SCOPED])

    def test_scoped_beats_absent(self):
        """资源组级的授权本来就不在 `attached` 里（那是账号级清单）。
        先判 ABSENT 的话会报成「这个号上本来就没有它」—— 那是假话，
        人会以为已经干净了，实际那条超管还挂着。"""
        plan = make_plan(wanted=want(f"{ADMIN} @资源组:rg-xxx"), admin_holders={"other"})
        self.assertEqual([c for _, c in plan.refused], [revoke.REFUSE_SCOPED])
        self.assertIn("资源组", plan.why(revoke.REFUSE_SCOPED))

    def test_scoped_beats_last_admin_prompt(self):
        """资源组级的超管：不是「确认一下就能撤」，是**这里根本撤不掉**。
        报成 last_admin 会让人点了确认、然后拿到一个什么都没发生的成功。"""
        plan = make_plan(wanted=want(f"{ADMIN} @资源组:rg-1"), admin_holders={USER})
        self.assertEqual([c for _, c in plan.refused], [revoke.REFUSE_SCOPED])


class RefuseTicketTests(unittest.TestCase):
    """面板通过申请单发的那些有到期时间、有台账，从这里撤会让台账说谎。"""

    def test_panel_granted_group_and_policy_are_refused(self):
        plan = make_plan(
            wanted=want(OSS, ("group", GRP)),
            panel_granted={("group", GRP), ("policy", OSS)},
        )
        self.assertEqual(names(plan), [])
        self.assertEqual(
            set(codes(plan)), {(GRP, revoke.REFUSE_TICKET), (OSS, revoke.REFUSE_TICKET)}
        )
        self.assertIn("申请单", plan.why(revoke.REFUSE_TICKET))

    def test_kind_is_part_of_the_key(self):
        """同名的组和策略是两回事：工单发的是**组** `x`，不该连累直接授予的**策略** `x`。"""
        plan = revoke.plan(
            user=USER,
            attached=[pol("x")],
            groups=["x"],
            wanted=want("x"),
            panel_granted={("group", "x")},
            admin_holders={"other"},
        )
        self.assertEqual(names(plan), ["x"])

    def test_ticket_beats_absent_so_the_message_points_at_the_ticket(self):
        """工单发的组已经被人在控制台里摘掉了（云上没有了）：这里要说「去那张单子上回收」，
        而不是「本来就没有」—— 后者会让人以为不用管，那张单子会一直显示「已开通」。"""
        plan = make_plan(
            groups=[],
            wanted=want(("group", GRP)),
            panel_granted={("group", GRP)},
        )
        self.assertEqual(codes(plan), [(GRP, revoke.REFUSE_TICKET)])


class RefuseAbsentTests(unittest.TestCase):
    """本来就没挂：不报错，但要说出来。静默成功会让人以为撤掉了。"""

    def test_absent_policy_and_group(self):
        plan = make_plan(wanted=want("不存在的策略", ("group", "不存在的组")))
        self.assertEqual(names(plan), [])
        self.assertEqual({c for _, c in plan.refused}, {revoke.REFUSE_ABSENT})

    def test_policy_and_group_namespaces_do_not_leak(self):
        """把组名当策略选（或反过来）要判成「没有」，不能拿另一个命名空间里的同名条目
        当成撤掉了 —— 那会真的去 detach 一条不存在的策略，然后报成功。"""
        plan = make_plan(wanted=want(GRP, ("group", OSS)))
        self.assertEqual(names(plan), [])
        self.assertEqual({c for _, c in plan.refused}, {revoke.REFUSE_ABSENT})

    def test_absent_never_reaches_the_cloud_call(self):
        """没挂的东西不进 remove —— 进了的话接口层会去调一次 detach，
        云那边对「本来就没授予」是静默成功的，于是台账记了一条根本没发生的撤销。"""
        plan = make_plan(wanted=want("不存在的策略", OSS))
        self.assertEqual(names(plan), [OSS])


class LastAdminTests(unittest.TestCase):
    """最后一个管理员：**不硬拦，但要显式确认**。

    硬拦一个合法且可恢复的操作，只会逼人绕开面板去控制台点，那才是真的失控；
    不拦则是另一头 —— 手滑一下这个云账号就没有子账号能做管理操作了。
    """

    def test_last_admin_is_refused_without_confirmation(self):
        plan = make_plan(wanted=want(ADMIN), admin_holders={USER})
        self.assertEqual(names(plan), [])
        self.assertEqual(codes(plan), [(ADMIN, revoke.REFUSE_LAST_ADMIN)])
        self.assertIn("确认", plan.why(revoke.REFUSE_LAST_ADMIN))

    def test_confirmation_lets_it_through(self):
        plan = make_plan(wanted=want(ADMIN), admin_holders={USER}, confirm_last_admin=True)
        self.assertEqual(names(plan), [ADMIN])
        self.assertEqual(plan.refused, [])

    def test_someone_else_is_admin_so_no_prompt(self):
        """还有别的管理员时**不该**触发这条 —— 每次都弹确认，确认框就没人看了。"""
        plan = make_plan(wanted=want(ADMIN), admin_holders={USER, "other"})
        self.assertEqual(names(plan), [ADMIN])
        self.assertEqual(plan.refused, [])

    def test_unknown_holders_still_asks(self):
        """算不出谁是管理员（快照没生成 / 读失败，`_admin_holders` 返回空集）时
        按「他可能是最后一个」处理。fail-safe 方向：多问一次，而不是默默撤掉。"""
        plan = make_plan(wanted=want(ADMIN), admin_holders=set())
        self.assertEqual(codes(plan), [(ADMIN, revoke.REFUSE_LAST_ADMIN)])

    def test_only_admin_policies_trigger_it(self):
        """普通策略不受这条影响，否则一个号上只剩一条权限时也会弹确认。"""
        plan = make_plan(wanted=want(OSS, AUTO), admin_holders={USER})
        self.assertEqual(names(plan), [OSS, AUTO])

    def test_confirmation_does_not_unlock_the_other_three(self):
        """确认的是「最后一个管理员」，不是「所有拒绝」。
        把 `confirm_last_admin` 当总开关用的话，勾上它就能撤面板身份了。"""
        plan = make_plan(user="panel-executor", wanted=want(OSS), confirm_last_admin=True)
        self.assertEqual(codes(plan), [(OSS, revoke.REFUSE_PANEL)])
        plan = make_plan(
            wanted=want(f"{ADMIN} @资源组:rg-1", "不存在的策略", RAM),
            panel_granted={("policy", RAM)},
            confirm_last_admin=True,
            admin_holders={USER},
        )
        self.assertEqual(names(plan), [])
        self.assertEqual(
            [c for _, c in plan.refused],
            [revoke.REFUSE_SCOPED, revoke.REFUSE_ABSENT, revoke.REFUSE_TICKET],
        )

    def test_moving_someone_out_of_an_admin_group_counts_too(self):
        """**火山 `wuji-opration` 组挂的就是 `AdministratorAccess`。**

        只按策略名判的那一版，对「把人移出发超管的组」完全不触发 ——
        把那个组的成员挨个移出去，全账号再没有子账号管理员，全程零确认。
        护栏在真实拓扑下 100% 不生效，而注释还声称它生效，那比没有护栏更糟。
        """
        plan = make_plan(
            groups=[GRP, "wuji-opration"],
            wanted=want(("group", "wuji-opration")),
            admin_groups={"wuji-opration"},
            admin_holders={USER},
        )
        self.assertEqual(names(plan), [])
        self.assertEqual(codes(plan), [("wuji-opration", revoke.REFUSE_LAST_ADMIN)])

    def test_an_ordinary_group_does_not_trigger_it(self):
        """反向：普通组照撤。每移出一个组都弹一次确认，确认框就没人看了。"""
        plan = make_plan(wanted=want(("group", GRP)), admin_groups={"别的组"}, admin_holders={USER})
        self.assertEqual(names(plan), [GRP])

    def test_confirmed_admin_group_goes_through(self):
        plan = make_plan(
            groups=["wuji-opration"],
            wanted=want(("group", "wuji-opration")),
            admin_groups={"wuji-opration"},
            admin_holders={USER},
            confirm_last_admin=True,
        )
        self.assertEqual(names(plan), ["wuji-opration"])

    def test_only_the_admin_part_is_held_back(self):
        """一次勾了四条，其中只有两条会让他失去管理员：**另外两条照撤**。

        整批拒掉的话，管理员会去掉那两条重来一次 —— 而重来时最容易顺手勾上
        「我确认」把四条一起撤了。拒绝面越准，人越会认真看那句确认。
        `remaining` 也要跟着对：被拦下的那两条还在他身上。
        """
        plan = make_plan(
            attached=ATTACHED,
            groups=[GRP, "wuji-opration"],
            wanted=want(OSS, ADMIN, ("group", GRP), ("group", "wuji-opration")),
            admin_groups={"wuji-opration"},
            admin_holders={USER},
        )
        self.assertEqual(names(plan), [OSS, GRP])
        self.assertEqual({c for _, c in plan.refused}, {revoke.REFUSE_LAST_ADMIN})
        self.assertEqual(sorted(i.name for i, _ in plan.refused), sorted([ADMIN, "wuji-opration"]))
        self.assertIn(f"{ADMIN}（System）", plan.remaining)
        self.assertIn("用户组 wuji-opration", plan.remaining)
        self.assertNotIn(f"{OSS}（System）", plan.remaining)


# ── 纯逻辑：撤完还剩什么 ─────────────────────────────────────────────────


class RemainingTests(unittest.TestCase):
    """`remaining` 是界面上给人做判断的唯一依据。算错了，人会基于错的信息点执行。"""

    def test_manual_scenario(self):
        """线下手工验过的那一单，逐字对上（会撤什么 / 拒什么 / 剩什么）。"""
        plan = revoke.plan(
            user=USER,
            attached=ATTACHED,
            groups=[GRP],
            wanted=want(OSS, RAM, f"{ADMIN} @资源组:rg-xxx", "不存在的策略", ("group", GRP)),
            admin_holders={USER, "other"},
            panel_granted={("group", GRP)},
        )
        self.assertEqual(
            [(i.name, i.policy_type) for i in plan.remove], [(OSS, "System"), (RAM, "System")]
        )
        self.assertEqual(
            codes(plan),
            [
                (f"{ADMIN} @资源组:rg-xxx", revoke.REFUSE_SCOPED),
                ("不存在的策略", revoke.REFUSE_ABSENT),
                (GRP, revoke.REFUSE_TICKET),
            ],
        )
        self.assertEqual(
            plan.remaining, [f"{ADMIN}（System）", f"{AUTO}（Custom）", f"用户组 {GRP}"]
        )

    def test_refused_things_stay_in_remaining(self):
        """拒掉的当然还在身上。漏掉它们，界面会告诉管理员「超管已经没了」。"""
        plan = make_plan(wanted=want(ADMIN), admin_holders={USER})
        self.assertIn(f"{ADMIN}（System）", plan.remaining)

    def test_removed_things_leave_remaining(self):
        plan = make_plan(wanted=want(OSS, ("group", GRP)), admin_holders={"other"})
        self.assertNotIn(f"{OSS}（System）", plan.remaining)
        self.assertNotIn(f"用户组 {GRP}", plan.remaining)
        self.assertEqual(
            plan.remaining, [f"{ADMIN}（System）", f"{RAM}（System）", f"{AUTO}（Custom）"]
        )

    def test_remaining_is_sorted_and_labels_groups(self):
        """组和策略混在一起，不标「用户组」就分不清 —— 同名时更是两条看起来一样的行。"""
        plan = revoke.plan(user=USER, attached=[pol("b")], groups=["b"], wanted=[])
        self.assertEqual(plan.remaining, ["b（System）", "用户组 b"])

    def test_empty_everything(self):
        plan = revoke.plan(user=USER, attached=[], groups=[], wanted=[])
        self.assertEqual((plan.remove, plan.refused, plan.remaining), ([], [], []))
        self.assertFalse(plan.ok)

    def test_duplicates_are_kept_as_is(self):
        """**当前语义，不是 bug**：`wanted` 里重复的条目会重复进 `remove`，
        接口层就对同一条撤两次（两家云的 detach 对「本来就没授予」都静默成功，所以无害），
        `done` 里也会出现两次。

        为什么要锁住：将来有人「顺手去重」会改变 `done` 的条数和 `review.log` 里
        那一行的内容 —— 那是留痕，得是有意为之，而不是重构的副作用。
        `remaining` 不受影响（撤掉就是撤掉，不论请求里写了几遍）。
        """
        plan = make_plan(wanted=want(OSS, OSS), admin_holders={"other"})
        self.assertEqual(names(plan), [OSS, OSS])
        self.assertNotIn(f"{OSS}（System）", plan.remaining)

    def test_plan_does_not_mutate_its_inputs(self):
        """接口层拿同一份 `attached` 渲染响应。就地改的话，显示出来的「还剩什么」
        会是被改过的那份。"""
        attached = [pol(OSS), pol(AUTO, "Custom")]
        groups = [GRP]
        wanted = want(OSS)
        revoke.plan(user=USER, attached=attached, groups=groups, wanted=wanted, admin_holders={"o"})
        self.assertEqual(attached, [pol(OSS), pol(AUTO, "Custom")])
        self.assertEqual(groups, [GRP])
        self.assertEqual([(i.kind, i.name) for i in wanted], [("policy", OSS)])


class ItemAndWhyTests(unittest.TestCase):
    def test_label_shows_policy_type(self):
        self.assertEqual(revoke.Item("policy", OSS, "System").label, f"{OSS}（System）")
        self.assertEqual(revoke.Item("group", GRP).label, GRP)

    def test_item_is_hashable_and_frozen(self):
        item = revoke.Item("policy", OSS, "System")
        self.assertIn(item, {revoke.Item("policy", OSS, "System")})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            item.name = "别的"

    def test_every_refusal_code_has_a_human_reason(self):
        """界面直接显示 `why(code)`。缺一条就会把原因码 `scoped` 原样甩给人看。"""
        for code in (
            revoke.REFUSE_PANEL,
            revoke.REFUSE_TICKET,
            revoke.REFUSE_LAST_ADMIN,
            revoke.REFUSE_ABSENT,
            revoke.REFUSE_SCOPED,
        ):
            reason = revoke.Plan(user=USER).why(code)
            self.assertNotEqual(reason, code, code)
            self.assertGreater(len(reason), 8, code)

    def test_unknown_code_falls_back_to_itself(self):
        self.assertEqual(revoke.Plan(user=USER).why("nope"), "nope")

    def test_policy_type_comes_from_the_cloud_not_from_the_request(self):
        """自定义策略要按 `Custom` 去撤。类型丢了就按 `System` 调，云那边撤的是
        另一条（或报错），而面板照样记「已撤掉」。"""
        plan = make_plan(wanted=want(AUTO), admin_holders={"other"})
        self.assertEqual([(i.name, i.policy_type) for i in plan.remove], [(AUTO, "Custom")])


# ── 纯逻辑：面板工单发的是哪些 ───────────────────────────────────────────


def ticket(**over) -> dict:
    t = {
        "id": "REQ-1",
        "status": "done",
        "template": {
            "id": "oss-read",
            "platform": "aliyun",
            "account": ACC,
            "groups": [GRP],
            "policies": [{"type": "System", "name": OSS}],
        },
        "payload": {"cloud_user": USER},
        "expires_at_ts": 1_900_000_000.0,
    }
    t.update(over)
    return t


class GrantedByPanelTests(unittest.TestCase):
    def call(self, tickets, platform="aliyun", account=ACC, user=USER):
        return revoke.granted_by_panel(tickets, platform, account, user)

    def test_done_ticket_contributes_groups_and_policies(self):
        self.assertEqual(self.call([ticket()]), {("group", GRP), ("policy", OSS)})

    def test_policies_may_be_plain_strings(self):
        t = ticket()
        t["template"]["policies"] = [OSS, {"name": RAM}, {"type": "System"}, ""]
        self.assertEqual(self.call([t]), {("group", GRP), ("policy", OSS), ("policy", RAM)})

    def test_only_done_counts(self):
        """**只看还在生效的**。已回收 / 被关掉 / 还没批的单子发的那些，云上本来就该没有；
        要是还在，那是回收失败留下的残留，**正是应该从这里撤掉的** ——
        把它们也算成「工单发的」，残留就永远撤不掉了（去那张单子上按回收也没用，
        它已经是终态）。"""
        for status in ("revoked", "closed", "failed", "pending_approval", "rejected", ""):
            self.assertEqual(self.call([ticket(status=status)]), set(), status)

    def test_other_account_or_platform_does_not_count(self):
        """另一个云账号里同名的组不能连累这边 —— 那会让一个本该能撤的授权永远撤不掉。"""
        self.assertEqual(self.call([ticket()], platform="volcano"), set())
        self.assertEqual(self.call([ticket()], account="2000000000000002"), set())

    def test_other_user_does_not_count(self):
        self.assertEqual(self.call([ticket()], user="someone"), set())
        self.assertEqual(self.call([ticket(payload={})]), set())

    def test_new_account_tickets_use_username_not_cloud_user(self):
        """**开账号单的字段名是 `username`**（flows.py:1770），不是 `cloud_user`。

        只认 `cloud_user` 的话，开账号时一起加的组从这里撤**不会被拒** ——
        而那张单子仍写着「已加入 wuji_Algorithm」，台账开始说谎。
        新人开号必定带组，所以这不是边角情况，是最常见的那一类单子。
        """
        self.assertEqual(
            self.call([ticket(payload={"username": USER})]),
            {("group", GRP), ("policy", OSS)},
        )
        # 认的仍然是这个人，不是「有 username 就算」
        self.assertEqual(self.call([ticket(payload={"username": "someone"})]), set())

    def test_cloud_user_wins_when_both_are_present(self):
        """两个字段都有时以 `cloud_user` 为准（它是「给哪个子账号开权限」的那一个）。"""
        both = ticket(payload={"cloud_user": USER, "username": "someone"})
        self.assertEqual(self.call([both]), {("group", GRP), ("policy", OSS)})

    def test_garbage_rows_do_not_crash(self):
        """申请单文件是长期累积的，里面可能有旧格式 / 手工改坏的行。
        这里一崩，收权接口整个 500，而它本该只是少拒一条。"""
        rows = [None, "x", 42, {}, {"status": "done"}, ticket(template={}), ticket(payload=None)]
        self.assertEqual(self.call(rows), set())

    def test_no_tickets_at_all(self):
        self.assertEqual(self.call(None), set())
        self.assertEqual(self.call([]), set())


# ── 云侧：此刻挂着什么 ───────────────────────────────────────────────────


class AliyunAttachedTests(unittest.TestCase):
    """`attached()` 读的是**实时**状态。拿快照算，可能会去撤一条五分钟前刚发的策略。"""

    def executor(self, responses):
        calls = []

        def send(url):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            return responses.get(query["Action"], (200, {}))

        base = {"GetCallerIdentity": (200, {"AccountId": ACC})}
        ex = AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send)
        return ex, calls, {**base, **responses}

    def test_shape(self):
        ex, calls, _ = self.executor(
            {
                "GetCallerIdentity": (200, {"AccountId": ACC}),
                "ListPoliciesForUser": (
                    200,
                    {
                        "Policies": {
                            "Policy": [
                                {"PolicyName": ADMIN, "PolicyType": "System"},
                                {"PolicyName": AUTO, "PolicyType": "Custom"},
                            ]
                        }
                    },
                ),
                "ListGroupsForUser": (
                    200,
                    {"Groups": {"Group": [{"GroupName": GRP}, {"GroupName": ""}]}},
                ),
            }
        )
        pols, groups = ex.attached(USER)
        self.assertEqual(pols, [pol(ADMIN), pol(AUTO, "Custom")])
        # 名字为空的组要丢掉：它会在界面上渲染成一个勾得中、撤不掉的空条目
        self.assertEqual(groups, [GRP])
        actions = [c["Action"] for c in calls]
        self.assertIn("ListPoliciesForUser", actions)
        self.assertIn("ListGroupsForUser", actions)
        # 执行身份必须先核对属于目标云账号，否则可能去另一个账号上撤同名的号
        self.assertEqual(actions[0], "GetCallerIdentity")
        for call in calls:
            if call["Action"].startswith("List"):
                self.assertEqual(call["UserName"], USER)

    def test_resource_group_scoped_grants_are_not_in_this_list(self):
        """阿里云的 `ListPoliciesForUser` 本来就只给账号级。这条用例是**契约说明**：
        界面上没有资源组级那些，是因为这里撤不掉它们（不是漏采）。"""
        ex, _, _ = self.executor(
            {
                "GetCallerIdentity": (200, {"AccountId": ACC}),
                "ListPoliciesForUser": (
                    200,
                    {"Policies": {"Policy": [{"PolicyName": OSS, "PolicyType": "System"}]}},
                ),
                "ListGroupsForUser": (200, {"Groups": {"Group": []}}),
            }
        )
        pols, groups = ex.attached(USER)
        self.assertEqual([p["PolicyName"] for p in pols], [OSS])
        self.assertEqual(groups, [])

    def test_wrong_account_stops_before_reading_anything(self):
        ex, calls, _ = self.executor({"GetCallerIdentity": (200, {"AccountId": "9999"})})
        with self.assertRaises(ProvisionError):
            ex.attached(USER)
        self.assertEqual([c["Action"] for c in calls], ["GetCallerIdentity"])

    def test_api_error_surfaces(self):
        """读不到就要报错。吞掉错误返回空清单 = 界面显示「这个人什么权限都没有」，
        而他其实挂着超管。"""
        ex, _, _ = self.executor(
            {
                "GetCallerIdentity": (200, {"AccountId": ACC}),
                "ListPoliciesForUser": (403, {"Code": "NoPermission", "Message": "x"}),
            }
        )
        with self.assertRaises(aliyun.AliyunError):
            ex.attached(USER)

    def test_broken_policy_list_is_an_error_not_an_empty_list(self):
        """`{"Policies": null}`、缺 `Policies` 键、`Policy` 为 null —— 三种都要抛。

        两种坏法各有各的坑：静默返回空 = 页面告诉管理员「这个人已经很干净了」；
        而 `null` 那种曾经是 `AttributeError` 直接穿到 500（不是 `DeliveryError`，
        接口层的 `except DeliveryError` 接不住），连「读不到」都说不清楚。
        """
        for bad in ({"Policies": None}, {}, {"Policies": {}}, {"Policies": {"Policy": None}}):
            ex, _, _ = self.executor(
                {
                    "GetCallerIdentity": (200, {"AccountId": ACC}),
                    "ListPoliciesForUser": (200, bad),
                    "ListGroupsForUser": (200, {"Groups": {"Group": []}}),
                }
            )
            with self.assertRaises(ProvisionError, msg=bad):
                ex.attached(USER)

    def test_broken_group_list_is_an_error_too(self):
        """用户组那半边要和策略侧**对称**地抛，别留一边。

        组读空的后果和策略读空同级：界面上一个组都不显示、`remaining` 里也没有
        「用户组 X」→ 管理员撤完看到「什么都不剩」，而这个人还从组里继承着一堆权限。
        三种坏法都要抛（曾经分别是 `AttributeError` 穿到 500 / `TypeError` / 静默空表）。
        """
        for bad in ({"Groups": None}, {}, {"Groups": {}}, {"Groups": {"Group": None}}):
            ex, _, _ = self.executor(
                {
                    "GetCallerIdentity": (200, {"AccountId": ACC}),
                    "ListPoliciesForUser": (200, {"Policies": {"Policy": []}}),
                    "ListGroupsForUser": (200, bad),
                }
            )
            with self.assertRaises(ProvisionError, msg=bad):
                ex.attached(USER)

    def test_genuinely_empty_group_list_is_not_an_error(self):
        """反向：`{"Group": []}` 是「他确实不在任何组」，正常返回空。
        把「读不到」收得太宽（比如按 falsy 判）的话，一个没入过组的新人就再也收不了权。"""
        ex, _, _ = self.executor(
            {
                "GetCallerIdentity": (200, {"AccountId": ACC}),
                "ListPoliciesForUser": (200, {"Policies": {"Policy": []}}),
                "ListGroupsForUser": (200, {"Groups": {"Group": []}}),
            }
        )
        self.assertEqual(ex.attached(USER), ([], []))


class VolcanoAttachedTests(unittest.TestCase):
    def executor(self, handler):
        calls = []

        def send(url, headers, data=None):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            if query["Action"] == "ListUsers":
                return 200, {"Result": {"UserMetadata": [{"AccountId": "2000000001"}]}}
            return handler(query)

        return VolcanoExecutor("2000000001", volcano.Credentials("AK", "SK"), transport=send), calls

    def test_shape(self):
        def handler(q):
            if q["Action"] == "ListAttachedUserPolicies":
                return 200, {
                    "Result": {
                        "AttachedPolicyMetadata": [
                            {"PolicyName": "TOSFullAccess", "PolicyType": "System"},
                            {"PolicyName": "team-reader", "PolicyType": "Custom"},
                        ]
                    }
                }
            if q["Action"] == "ListGroupsForUser":
                return 200, {"Result": {"UserGroupMetadata": [{"UserGroupName": "algo"}, {}]}}
            return 400, {}

        ex, _ = self.executor(handler)
        pols, groups = ex.attached("lisi")
        self.assertEqual(pols, [pol("TOSFullAccess"), pol("team-reader", "Custom")])
        self.assertEqual(groups, ["algo"])

    def test_groups_are_paged(self):
        """火山的 `ListGroupsForUser` 按页返回。只读第一页的话，第 101 个组起
        在界面上根本不出现 —— 收权时看到的是一份**不全**的清单。"""
        pages = [
            [{"UserGroupName": f"g{i}"} for i in range(100)],
            [{"UserGroupName": f"g{i}"} for i in range(100, 150)],
        ]
        seen = []

        def handler(q):
            if q["Action"] == "ListAttachedUserPolicies":
                return 200, {"Result": {"AttachedPolicyMetadata": []}}
            if q["Action"] == "ListGroupsForUser":
                offset = int(q.get("Offset") or 0)
                seen.append(offset)
                page = pages[offset // 100] if offset // 100 < len(pages) else []
                return 200, {"Result": {"UserGroupMetadata": page}}
            return 400, {}

        ex, _ = self.executor(handler)
        _, groups = ex.attached("lisi")
        self.assertEqual(len(groups), 150)
        self.assertEqual(groups[-1], "g149")
        self.assertEqual(seen, [0, 100])

    def test_endless_paging_stops_instead_of_looping_forever(self):
        """接口一直回满页（分页参数没生效之类）时要停下来报错，不能把面板挂死。"""

        def handler(q):
            if q["Action"] == "ListAttachedUserPolicies":
                return 200, {"Result": {"AttachedPolicyMetadata": []}}
            if q["Action"] == "ListGroupsForUser":
                return 200, {
                    "Result": {
                        "UserGroupMetadata": [{"UserGroupName": f"g{i}"} for i in range(100)]
                    }
                }
            return 400, {}

        ex, _ = self.executor(handler)
        with self.assertRaises(ProvisionError):
            ex.attached("lisi")

    def test_wrong_account_stops(self):
        ex = VolcanoExecutor(
            "2000000009",
            volcano.Credentials("AK", "SK"),
            transport=lambda url, headers, data=None: (
                200,
                {"Result": {"UserMetadata": [{"AccountId": "2000000001"}]}},
            ),
        )
        with self.assertRaises(ProvisionError):
            ex.attached("lisi")

    def policies(self, *items):
        def handler(q):
            if q["Action"] == "ListAttachedUserPolicies":
                return 200, {"Result": {"AttachedPolicyMetadata": list(items)}}
            if q["Action"] == "ListGroupsForUser":
                return 200, {"Result": {"UserGroupMetadata": []}}
            return 400, {}

        return handler

    def test_project_scoped_policies_are_not_listed(self):
        """项目范围的授权不进清单 —— 判据要和 `has_policy` **完全一致**。

        不一致的后果不是「多列一条」：`detach_policy` 撤之前先问 `has_policy`，
        而它只认 Global，于是项目范围那条会被「查不到 → 直接 return」，
        面板把它记进 `done`、界面显示「已撤掉」，云上一动没动。
        列了却撤不掉，比不列更误导。
        """
        ex, _ = self.executor(
            self.policies(
                {"PolicyName": "TOSFullAccess", "PolicyType": "System"},
                {
                    "PolicyName": "VPCFullAccess",
                    "PolicyType": "System",
                    "PolicyScope": [{"PolicyScopeType": "Project", "ProjectName": "p1"}],
                },
            )
        )
        pols, _ = ex.attached("lisi")
        self.assertEqual([p["PolicyName"] for p in pols], ["TOSFullAccess"])

    def test_explicit_global_scope_is_kept(self):
        """反向：显式标了 Global 的、以及一条里既有 Global 又有 Project 的，都要留下。
        只按「有没有 PolicyScope 字段」过滤的话，这两种会被连坐撤不了。"""
        ex, _ = self.executor(
            self.policies(
                {
                    "PolicyName": "TOSFullAccess",
                    "PolicyType": "System",
                    "PolicyScope": [{"PolicyScopeType": "Global"}],
                },
                {
                    "PolicyName": "ECSFullAccess",
                    "PolicyType": "System",
                    "PolicyScope": [
                        {"PolicyScopeType": "Project", "ProjectName": "p1"},
                        {"PolicyScopeType": "Global"},
                    ],
                },
                {"PolicyName": "VPCReadOnly", "PolicyType": "System", "PolicyScope": []},
            )
        )
        pols, _ = ex.attached("lisi")
        # PolicyScope 为空按 Global 处理（火山对全局授权就是不给这个字段）
        self.assertEqual(
            [p["PolicyName"] for p in pols], ["TOSFullAccess", "ECSFullAccess", "VPCReadOnly"]
        )

    def test_missing_policy_list_is_an_error_not_an_empty_list(self):
        """返回里缺 `AttachedPolicyMetadata` 时要抛，不能当作「这个人什么都没挂」——
        界面会照着这份空清单告诉管理员「已经很干净了」，而他可能挂着超管。"""

        def handler(q):
            if q["Action"] == "ListAttachedUserPolicies":
                return 200, {"Result": {}}
            return 200, {"Result": {"UserGroupMetadata": []}}

        ex, _ = self.executor(handler)
        with self.assertRaises(ProvisionError):
            ex.attached("lisi")


# ── 留痕 ─────────────────────────────────────────────────────────────────


class LogRevokeTests(unittest.TestCase):
    """收权是唯一一条不经申请单的减权通道，事后能查到的只有 review.log 里那一行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.paths = review_mod.ReviewPaths(
            proposal=str(self.dir / "proposal.json"),
            manual=str(self.dir / "manual.json"),
            people=str(self.dir / "people.json"),
        )

    def log_lines(self) -> list:
        path = self.dir / "review.log"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def write(self, **over):
        kw = dict(
            actor="on_admin",
            platform="aliyun",
            account=ACC,
            user=USER,
            done=[OSS],
            failed=[{"name": RAM, "error": "超时"}],
            reason="离职交接，收回存量权限",
        )
        kw.update(over)
        review_mod.log_revoke(self.paths, **kw)

    def test_records_who_what_and_why(self):
        self.write()
        (row,) = self.log_lines()
        self.assertEqual(row["event"], "revoke")
        self.assertEqual(row["actor"], "on_admin")
        self.assertEqual(row["scope"], f"aliyun/{ACC}/{USER}")
        self.assertEqual(row["removed"], [OSS])
        self.assertEqual(row["failed"], [RAM])
        self.assertEqual(row["reason"], "离职交接，收回存量权限")
        self.assertTrue(row["at"])

    def test_appends_instead_of_overwriting(self):
        self.write()
        self.write(done=[RAM], failed=[])
        self.assertEqual([r["removed"] for r in self.log_lines()], [[OSS], [RAM]])

    def test_file_is_private(self):
        """这行里有云账号、子账号名和收权理由，别让同机其他账号读。"""
        self.write()
        self.assertEqual((self.dir / "review.log").stat().st_mode & 0o777, 0o600)

    def test_newlines_in_reason_cannot_forge_a_log_line(self):
        """理由是管理员随手填的自由文本。JSON 行 + ensure_ascii 之后，
        里面的换行不可能造出第二条日志。"""
        self.write(reason='真理由\n{"event": "revoke", "actor": "someone-else"}')
        rows = self.log_lines()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("someone-else", json.dumps(rows[0]["actor"]))

    def test_no_paths_is_a_noop(self):
        """名册路径没配（`review_paths()` 返回 None）时不能炸 —— 权限已经撤掉了。"""
        review_mod.log_revoke(
            None,
            actor="a",
            platform="aliyun",
            account=ACC,
            user=USER,
            done=[],
            failed=[],
            reason="x",
        )

    def test_unwritable_log_does_not_raise(self):
        """写不进去**不抛**：权限已经在云上撤掉了，这时候报错会让人以为没撤成、再点一次
        —— 而第二次点下去，界面上那些条目已经不在了，人只会更糊涂。"""
        (self.dir / "review.log").mkdir()  # 写日志会 IsADirectoryError
        self.write()
        self.assertEqual(self.log_lines(), [])


# ── 接口层 ───────────────────────────────────────────────────────────────

API = "/api/admin/access/revoke"

PEOPLE = {
    "schema": "wuji-people@1",
    "people": [
        {
            "union_id": "on_admin",
            "name": "管理员",
            "email": "admin@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": ACC, "name": "boss"}],
        },
        {
            "union_id": "on_1",
            "name": "何观奇",
            "email": "he@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": ACC, "name": USER}],
        },
    ],
}


def snapshot(*users, groups=None) -> dict:
    return {
        "captured_at": "2026-09-20T10:00:00+08:00",
        "accounts": [
            {
                "platform": "aliyun",
                "account": ACC,
                "users": list(users) or [{"name": USER, "policies": [ADMIN], "groups": [GRP]}],
                "groups": (
                    list(groups)
                    if groups is not None
                    else [{"name": GRP, "policies": [OSS], "members": [USER]}]
                ),
            }
        ],
    }


class FakeExecutor:
    """假执行器：记下每一次云调用。**预演的用例靠它断言 detach 一次都没被调用。**"""

    def __init__(self):
        self.policies = [pol(ADMIN), pol(OSS), pol(RAM), pol(AUTO, "Custom")]
        self.groups = [GRP]
        self.calls = []
        self.attached_error = None
        self.fail_on = set()
        #: {策略名: True/False/None}。没登记的按「读出来了，里面没有 Deny」算
        self.deny = {}

    def attached(self, user):
        self.calls.append(("attached", user))
        if self.attached_error is not None:
            raise self.attached_error
        return [dict(p) for p in self.policies], list(self.groups)

    def has_deny(self, policy_type, policy):
        """策略正文里有没有 Deny。**None = 读不出来**，和真实实现同一套三态。"""
        self.calls.append(("has_deny", policy_type, policy))
        return self.deny.get(policy, False)

    @property
    def deny_questions(self) -> list:
        return [(c[1], c[2]) for c in self.calls if c[0] == "has_deny"]

    def detach_policy(self, user, policy_type, policy):
        self.calls.append(("detach", user, policy_type, policy))
        if policy in self.fail_on:
            raise ProvisionError(f"DetachPolicyFromUser 失败：{policy} 超时")

    def remove_from_group(self, user, group):
        self.calls.append(("ungroup", user, group))
        if group in self.fail_on:
            raise ProvisionError(f"RemoveUserFromGroup 失败：{group} 超时")

    @property
    def writes(self) -> list:
        return [c for c in self.calls if c[0] in ("detach", "ungroup")]


class _Live:
    def __init__(self, backend):
        self.store = Store()
        handler = make_handler(
            PlatformRegistry.load(),
            self.store,
            app_id="cli_demo",
            app_secret="s",
            base_url="http://127.0.0.1",
            backend=backend,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, payload, *, cookie="", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        head = dict(headers or {})
        if cookie:
            head["Cookie"] = f"{COOKIE_NAME}={cookie}"
        if headers is None:
            head.setdefault("Content-Type", "application/json")
            head.setdefault("X-Panel-Request", "1")
        conn.request("POST", path, body=json.dumps(payload).encode(), headers=head)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, raw.decode(errors="replace")


class RevokeApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.write("people.json", PEOPLE)
        self.write("admins.json", {"union_ids": ["on_admin"]})
        self.write("inventory.json", snapshot())
        self.write(
            "proposal.json", {"domain": "wuji.tech", "people": [], "unlinked": [], "services": []}
        )
        self.write("manual.json", {"links": []})
        self.tickets([])
        self.executor = FakeExecutor()
        self.backend = self.make_backend()

    def write(self, name, data):
        (self.dir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def tickets(self, rows):
        self.write("tickets.json", {"schema": "wuji-tickets@1", "tickets": rows})

    def make_backend(self, **over):
        opts = dict(
            people_path=str(self.dir / "people.json"),
            admins_path=str(self.dir / "admins.json"),
            inventory_path=str(self.dir / "inventory.json"),
            proposal_path=str(self.dir / "proposal.json"),
            manual_path=str(self.dir / "manual.json"),
            tickets_path=str(self.dir / "tickets.json"),
            platforms={"aliyun": "阿里云", "volcano": "火山引擎"},
            executor=lambda platform, account: self.executor,
        )
        opts.update(over)
        return Backend(**opts)

    def login(self, live, uid):
        sid = f"sid-{uid}"
        email = "admin@wuji.tech" if uid == "on_admin" else "he@wuji.tech"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(
                open_id="ou_x", union_id=uid, name="某人", email=email, enterprise_email=email
            )
        )
        return sid

    def call(self, live, sid=None, *, headers=None, **payload):
        body = {"platform": "aliyun", "account": ACC, "user": USER, "policies": [OSS]}
        body.update(payload)
        if sid is None:
            sid = self.login(live, "on_admin")
        return live.post(API, body, cookie=sid, headers=headers)

    def log_rows(self) -> list:
        path = self.dir / "review.log"
        if not path.is_file():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    # ── 谁能调 ───────────────────────────────────────────────────────────
    def test_anonymous_is_401_and_touches_no_cloud(self):
        with _Live(self.backend) as live:
            status, _ = live.post(API, {"platform": "aliyun", "account": ACC, "user": USER})
            self.assertEqual(status, 401)
            self.assertEqual(self.executor.calls, [])

    def test_applicant_is_403(self):
        """收权改的是别人的云上权限，只有管理员能做。
        非管理员连**预演**都不给：预演会把别人身上挂了什么原样回给调用方。"""
        with _Live(self.backend) as live:
            status, body = self.call(live, self.login(live, "on_1"))
            self.assertEqual(status, 403, body)
            self.assertEqual(self.executor.calls, [])

    def test_csrf_headers_required(self):
        """只靠 Cookie 的话，管理员点开任意外部页面就可能被替他撤掉一批权限。"""
        with _Live(self.backend) as live:
            sid = self.login(live, "on_admin")
            for headers in (
                {"Content-Type": "application/json"},  # 缺 X-Panel-Request
                {"Content-Type": "text/plain", "X-Panel-Request": "1"},
                {
                    "Content-Type": "application/json",
                    "X-Panel-Request": "1",
                    "Sec-Fetch-Site": "cross-site",
                },
            ):
                status, body = self.call(
                    live, sid, headers=headers, apply=True, reason="收回存量权限"
                )
                self.assertEqual(status, 403, headers)
            self.assertEqual(self.executor.calls, [])

    # ── 参数校验 ─────────────────────────────────────────────────────────
    def test_missing_target_is_400(self):
        with _Live(self.backend) as live:
            sid = self.login(live, "on_admin")
            for missing in ({"platform": ""}, {"account": ""}, {"user": ""}, {"user": None}):
                status, body = self.call(live, sid, **missing)
                self.assertEqual(status, 400, (missing, body))
            self.assertEqual(self.executor.calls, [])

    def test_nothing_selected_is_400(self):
        """一条都没勾时不能返回一个「会撤 0 条」的成功 —— 那张空预演看起来像
        「这个人没什么可撤的」，而其实是没选。"""
        with _Live(self.backend) as live:
            sid = self.login(live, "on_admin")
            for sel in (
                {"policies": [], "groups": []},
                {"policies": [""], "groups": ["  "]},
                {"policies": None, "groups": None},
            ):
                status, body = self.call(live, sid, **sel)
                self.assertEqual(status, 400, (sel, body))
            self.assertEqual(self.executor.calls, [])

    def test_apply_needs_a_real_reason(self):
        """真撤必须写清楚为什么：没有申请单兜着，理由是事后唯一查得到的东西。"""
        with _Live(self.backend) as live:
            sid = self.login(live, "on_admin")
            for reason in ("", "   ", "收权", "四个字啊", "  短  "):
                status, body = self.call(live, sid, apply=True, reason=reason)
                self.assertEqual(status, 400, (reason, body))
                self.assertIn("理由", body["error"])
            self.assertEqual(self.executor.writes, [])
            self.assertEqual(self.log_rows(), [])

    def test_dry_run_does_not_need_a_reason(self):
        """预演是给人看清楚再决定的，要求先写理由会让人先随便填一个占位。"""
        with _Live(self.backend) as live:
            status, body = self.call(live, reason="")
            self.assertEqual(status, 200, body)
            self.assertFalse(body["applied"])

    # ── 预演 ─────────────────────────────────────────────────────────────
    def test_dry_run_never_touches_the_cloud(self):
        """**这条错了等于没有预演。** 点「预演」把权限真撤了，人还以为只是看看。"""
        with _Live(self.backend) as live:
            status, body = self.call(live, policies=[OSS, RAM], groups=[GRP])
            self.assertEqual(status, 200, body)
            self.assertEqual(body["applied"], False)
            self.assertEqual([i["name"] for i in body["remove"]], [OSS, RAM, GRP])
            self.assertEqual(self.executor.writes, [], "预演不许调用任何写接口")
            # 预演只读两样：此刻挂着什么 + 自定义策略里有没有 Deny
            self.assertEqual({c[0] for c in self.executor.calls}, {"attached", "has_deny"})
            self.assertNotIn("done", body)

    def test_only_custom_policies_are_asked_about_deny(self):
        """系统策略一条都不问。阿里云的系统策略都是纯 Allow，而这里每问一次就是一个
        云 API 往返 —— 60 条系统策略问一遍，预演会慢到没人愿意点，
        然后大家就直接点「执行」了。那才是这条优化真正要防的事。
        """
        with _Live(self.backend) as live:
            self.call(live, policies=[OSS])
        self.assertEqual(self.executor.deny_questions, [("Custom", AUTO)])

    def test_dry_run_reads_live_state_not_the_snapshot(self):
        """快照里这个人只有 `AdministratorAccess`，云上实际还挂着 OSS/RAM。
        照快照算的话，刚发下去还没进快照的权限会被判成「本来就没挂」。"""
        with _Live(self.backend) as live:
            _, body = self.call(live, policies=[RAM])
            self.assertEqual([i["name"] for i in body["remove"]], [RAM])
            self.assertEqual(body["refused"], [])

    def test_response_carries_the_reasons_and_what_is_left(self):
        self.tickets([ticket()])
        with _Live(self.backend) as live:
            _, body = self.call(
                live, policies=[OSS, f"{ADMIN} @资源组:rg-1", "不存在的"], groups=[GRP]
            )
            got = {(r["name"], r["code"]) for r in body["refused"]}
            self.assertEqual(
                got,
                {
                    (OSS, "ticket"),  # 工单发的
                    (f"{ADMIN} @资源组:rg-1", "scoped"),
                    ("不存在的", "absent"),
                    (GRP, "ticket"),
                },
            )
            for row in body["refused"]:
                self.assertTrue(row["why"] and row["why"] != row["code"], row)
            self.assertEqual(
                body["remaining"],
                [
                    f"{ADMIN}（System）",
                    f"{OSS}（System）",
                    f"{RAM}（System）",
                    f"{AUTO}（Custom）",
                    f"用户组 {GRP}",
                ],
            )

    def test_panel_identity_is_refused_through_the_api(self):
        with _Live(self.backend) as live:
            _, body = self.call(live, user="panel-executor", apply=True, reason="清理面板身份")
            self.assertEqual(body["remove"], [])
            self.assertEqual([r["code"] for r in body["refused"]], ["panel"])
            self.assertEqual(self.executor.writes, [])

    def test_last_admin_needs_confirm_through_the_api(self):
        """快照里只有他一个人持有超管 → 先拒；带上确认再来才放行。"""
        with _Live(self.backend) as live:
            _, body = self.call(live, policies=[ADMIN])
            self.assertEqual([r["code"] for r in body["refused"]], ["last_admin"])
            _, body = self.call(live, policies=[ADMIN], confirm_last_admin=True)
            self.assertEqual([i["name"] for i in body["remove"]], [ADMIN])

    def with_ops_group(self, group_policies):
        """快照：USER 直挂超管；opsguy 只经 `wuji-opration` 组拿权限。"""
        self.write(
            "inventory.json",
            {
                "captured_at": "2026-09-20T10:00:00+08:00",
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": ACC,
                        "users": [
                            {"name": USER, "policies": [ADMIN], "groups": []},
                            {"name": "opsguy", "policies": [], "groups": ["wuji-opration"]},
                        ],
                        "groups": [
                            {
                                "name": "wuji-opration",
                                "policies": list(group_policies),
                                "members": ["opsguy"],
                            }
                        ],
                    }
                ],
            },
        )

    def test_group_granted_admins_count_as_holders(self):
        """经用户组继承的超管也算 holder。火山的 `wuji-opration` 组本身就挂着超管。

        只看 `user.policies` 的话，「最后一个管理员」这道确认会变成**狼来了**：
        账号里明明还有别的管理员，面板照样弹「撤完只剩主账号能做管理操作」。
        弹多了就没人看，真到最后一个时也会被一路点过去 —— 护栏就是这么失效的。
        """
        self.with_ops_group([ADMIN])
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[ADMIN])
        self.assertEqual([i["name"] for i in body["remove"]], [ADMIN])
        self.assertEqual(body["refused"], [])

    def test_a_group_without_admin_does_not_make_its_members_holders(self):
        """反向锁：组里没有超管时，组成员**不是** holder。

        少了这条，把「算不算 holder」写成「在任何组里就算」也能绿 —— 那样几乎人人都是
        管理员，「最后一个管理员」的确认从此再也不会弹，等于这道护栏被悄悄删掉。
        """
        self.with_ops_group([OSS])  # 这个组只发 OSS 权限
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[ADMIN])
        self.assertEqual([r["code"] for r in body["refused"]], ["last_admin"])
        self.assertEqual(body["remove"], [])

    def break_effective_policies(self):
        """让 `effective_policies` 抛异常 —— 只有这样才跑得到那条退回分支。"""
        # 不用嵌套 with：括号版多上下文管理器是 3.10+ 语法，而这个仓库的 floor 是 py39
        patcher = mock.patch.object(
            inventory.Snapshot, "effective_policies", side_effect=RuntimeError("算不出来")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_uncomputable_effective_policies_falls_back_to_direct_ones(self):
        """`effective_policies` 算不出来（快照缺组、数据形状怪）时退回**直挂的那些**，
        而不是退回「当他没有任何权限」。

        这两种退法在「只有一个直挂管理员」的局面下表现一模一样（都弹确认），
        所以必须拿**两个直挂管理员**来分辨：退回直挂 → 不该弹；当没权限 → 会弹，
        而那等于每次快照出点小毛病，收权就退化成「逢超管必问」。
        """
        self.write(
            "inventory.json",
            snapshot(
                {"name": USER, "policies": [ADMIN], "groups": []},
                {"name": "boss", "policies": [ADMIN], "groups": []},
            ),
        )
        self.break_effective_policies()
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[ADMIN])
        self.assertEqual([i["name"] for i in body["remove"]], [ADMIN])

    def test_the_fallback_asks_more_never_less(self):
        """退回之后组继承的超管算不出来 → holders 只会更少 → 更容易弹确认。
        方向反了（算不出就当人人都是管理员）就会在真的最后一个时默默撤掉。"""
        self.with_ops_group([ADMIN])
        self.break_effective_policies()
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[ADMIN])
        # opsguy 的超管是经组继承的 → 退回直挂后他不算 holder → 只剩 USER → 要确认
        self.assertEqual([r["code"] for r in body["refused"]], ["last_admin"])

    def test_another_admin_in_the_snapshot_means_no_prompt(self):
        self.write(
            "inventory.json",
            snapshot(
                {"name": USER, "policies": [ADMIN], "groups": []},
                {"name": "boss", "policies": [ADMIN], "groups": []},
            ),
        )
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[ADMIN])
            self.assertEqual([i["name"] for i in body["remove"]], [ADMIN])

    # ── 护栏：Deny 与组 ──────────────────────────────────────────────────
    def test_a_custom_policy_with_deny_is_refused(self):
        """真机验过的那两条：`pai平台权限` → False（可撤）、
        `wuji-panel-executor` → True（拒）。这里锁的是「读出来有 Deny 就拒」。"""
        self.executor.deny = {AUTO: True}
        with _Live(self.backend) as live:
            _, body = self.call(live, policies=[AUTO], apply=True, reason="收回存量权限")
        self.assertEqual([(r["name"], r["code"]) for r in body["refused"]], [(AUTO, "deny")])
        self.assertEqual(self.executor.writes, [])

    def test_an_unreadable_policy_is_refused_too(self):
        """读不出正文（缺 `ram:GetPolicy` 权限、策略被删到一半、接口抖）→ 一样拒。
        当成「没有 Deny」放行的话，一条读不了的护栏策略会被当普通 Allow 撤掉。"""
        self.executor.deny = {AUTO: None}
        with _Live(self.backend) as live:
            _, body = self.call(live, policies=[AUTO])
        self.assertEqual([r["code"] for r in body["refused"]], ["deny"])
        self.assertIn("Deny", body["refused"][0]["why"])

    def test_a_clean_custom_policy_goes_through(self):
        """反向：问过、答案是「没有 Deny」的自定义策略照常可撤 ——
        否则自定义策略从此一条都撤不掉，而那正是存量里最该清的一类。"""
        with _Live(self.backend) as live:
            _, body = self.call(live, policies=[AUTO])
        self.assertEqual([i["name"] for i in body["remove"]], [AUTO])

    def test_a_group_carrying_a_guardrail_policy_is_refused(self):
        """护栏策略挂在组上：移出这个组同样等于放权（`_protected_groups` 从快照算）。"""
        self.write(
            "inventory.json",
            snapshot(
                {"name": USER, "policies": [ADMIN], "groups": ["wuji-guardrail"]},
                groups=[
                    {"name": GRP, "policies": [OSS], "members": [USER]},
                    {
                        "name": "wuji-guardrail",
                        "policies": ["wuji-deny-pai-delete"],
                        "members": [USER],
                    },
                ],
            ),
        )
        self.executor.groups = [GRP, "wuji-guardrail"]
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[], groups=["wuji-guardrail", GRP])
        self.assertEqual(
            [(r["name"], r["code"]) for r in body["refused"]], [("wuji-guardrail", "protected")]
        )
        self.assertEqual([i["name"] for i in body["remove"]], [GRP])

    def test_a_group_granting_admin_needs_confirmation(self):
        """快照里 `wuji-opration` 组挂着超管、只有这一个来源 →
        把人移出去要确认；确认后放行。"""
        self.write(
            "inventory.json",
            snapshot(
                {"name": USER, "policies": [], "groups": ["wuji-opration"]},
                groups=[{"name": "wuji-opration", "policies": [ADMIN], "members": [USER]}],
            ),
        )
        self.executor.groups = ["wuji-opration"]
        with _Live(self.make_backend()) as live:
            _, body = self.call(live, policies=[], groups=["wuji-opration"])
            self.assertEqual([r["code"] for r in body["refused"]], ["last_admin"])
            _, body = self.call(
                live, policies=[], groups=["wuji-opration"], confirm_last_admin=True
            )
            self.assertEqual([i["name"] for i in body["remove"]], ["wuji-opration"])

    # ── 真撤 ─────────────────────────────────────────────────────────────
    def test_apply_detaches_with_the_type_from_the_cloud(self):
        with _Live(self.backend) as live:
            status, body = self.call(
                live, policies=[OSS, AUTO], groups=[GRP], apply=True, reason="离职交接，收回权限"
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["applied"], True)
            self.assertEqual(body["done"], [OSS, AUTO, GRP])
            self.assertEqual(body["failed"], [])
            self.assertEqual(
                self.executor.writes,
                [
                    ("detach", USER, "System", OSS),
                    ("detach", USER, "Custom", AUTO),  # 自定义策略要按 Custom 撤
                    ("ungroup", USER, GRP),
                ],
            )

    def test_a_policy_without_a_type_is_not_guessed(self):
        """云上没返回 `PolicyType` 时**不许猜 System**。

        猜错的后果是「静默的假成功」：阿里会吞 `EntityNotExist.User.Policy`、
        火山的 `has_policy` 要求类型精确相等、查不到就直接 return —— 两边都不报错，
        于是这条进了 `done`，界面说撤了，云上一动没动。宁可记一条 failed 让人去看。
        """
        self.executor.policies = [{"PolicyName": OSS, "PolicyType": ""}]
        with _Live(self.backend) as live:
            _, body = self.call(live, policies=[OSS], apply=True, reason="收回存量权限")
        self.assertEqual(body["done"], [])
        self.assertEqual([f["name"] for f in body["failed"]], [OSS])
        self.assertIn("不敢猜", body["failed"][0]["error"])
        self.assertEqual(self.executor.writes, [], "类型不明就别调 detach")

    def test_apply_only_touches_what_the_plan_allows(self):
        """勾了一批，其中几条要拒。拒掉的绝不能被调用 —— 面板工单发的那条一旦被撤，
        台账还显示「已开通」，云上已经没了。"""
        self.tickets([ticket()])
        with _Live(self.backend) as live:
            _, body = self.call(
                live,
                policies=[OSS, RAM, "不存在的"],
                groups=[GRP],
                apply=True,
                reason="收回存量权限",
            )
            self.assertEqual(body["done"], [RAM])
            self.assertEqual(self.executor.writes, [("detach", USER, "System", RAM)])

    def test_one_failure_does_not_block_the_rest(self):
        """一条撤不动（限流 / 那条策略正好被人删了）不能让后面的都不撤：
        停在中间的话，云上和界面上的都对不上，而人只看到一个报错。"""
        self.executor.fail_on = {OSS}
        with _Live(self.backend) as live:
            status, body = self.call(
                live, policies=[OSS, RAM], groups=[GRP], apply=True, reason="收回存量权限"
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["done"], [RAM, GRP])
            self.assertEqual([f["name"] for f in body["failed"]], [OSS])
            self.assertIn("超时", body["failed"][0]["error"])
            self.assertEqual([c[-1] for c in self.executor.writes], [OSS, RAM, GRP])

    def test_failures_are_recorded_too(self):
        """留痕里要同时有「撤掉的」和「没撤掉的」。只记成功的话，
        事后看日志会以为全撤干净了。"""
        self.executor.fail_on = {GRP}
        with _Live(self.backend) as live:
            self.call(live, policies=[OSS], groups=[GRP], apply=True, reason="离职交接，收回权限")
        (row,) = self.log_rows()
        self.assertEqual(row["event"], "revoke")
        self.assertEqual(row["actor"], "on_admin")
        self.assertEqual(row["scope"], f"aliyun/{ACC}/{USER}")
        self.assertEqual(row["removed"], [OSS])
        self.assertEqual(row["failed"], [GRP])
        self.assertEqual(row["reason"], "离职交接，收回权限")

    def test_dry_run_writes_no_log(self):
        with _Live(self.backend) as live:
            self.call(live, reason="就看看")
        self.assertEqual(self.log_rows(), [])

    def test_unwritable_log_does_not_turn_a_done_revoke_into_an_error(self):
        """日志写不进去时报 500 的话，人会以为没撤成、再点一次 ——
        而权限其实已经撤了。"""
        (self.dir / "review.log").mkdir()
        with _Live(self.backend) as live:
            status, body = self.call(live, apply=True, reason="收回存量权限")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["done"], [OSS])
        self.assertEqual(self.executor.writes, [("detach", USER, "System", OSS)])

    def test_no_place_to_log_means_no_revoking(self):
        """**拿不到留痕目的地就不执行。**

        这和「日志写不进去不报错」不矛盾，两者的时序正好相反：
          · 上一条是**已经撤完了**才发现写不进去 —— 那时候报错只会让人再点一次。
          · 这一条是**动手之前**就知道没地方记 —— 那就别动手。
        `review_paths()` 在名册审核那几个路径缺任一时返回 None，所以一个没开名册
        审核的部署，收权会一条日志都不留，而响应里看不出任何异常。
        """
        backend = self.make_backend(proposal_path=None, manual_path=None)
        with _Live(backend) as live:
            status, body = self.call(live, apply=True, reason="收回存量权限")
        self.assertEqual(status, 503, body)
        self.assertIn("留痕", body["error"])
        self.assertEqual(self.executor.writes, [], "没地方留痕就不该动云上的东西")

    def test_missing_log_path_still_allows_a_dry_run(self):
        """反向锁：拦的是「执行」，不是「看一眼」。预演什么都不改、也不需要留痕，
        把它一起拦掉的话，管理员连这个人挂了什么都看不到了。"""
        backend = self.make_backend(proposal_path=None, manual_path=None)
        with _Live(backend) as live:
            status, body = self.call(live)
        self.assertEqual(status, 200, body)
        self.assertEqual([i["name"] for i in body["remove"]], [OSS])

    def test_cloud_read_failure_is_502_and_revokes_nothing(self):
        """读不到「此刻挂着什么」就不能撤：拿不到实时状态时动手，等于照着猜撤。"""
        self.executor.attached_error = ProvisionError("执行身份不属于目标云账号，已停止")
        with _Live(self.backend) as live:
            status, body = self.call(live, apply=True, reason="收回存量权限")
        self.assertEqual(status, 502, body)
        self.assertEqual(self.executor.writes, [])
        self.assertEqual(self.log_rows(), [])

    def test_missing_snapshot_does_not_break_the_page(self):
        """快照还没生成时收权仍要可用（只是「最后一个管理员」一律要确认）。"""
        backend = self.make_backend(inventory_path=str(self.dir / "nope.json"))
        with _Live(backend) as live:
            status, body = self.call(live, policies=[ADMIN])
            self.assertEqual(status, 200, body)
            self.assertEqual([r["code"] for r in body["refused"]], ["last_admin"])

    def test_broken_tickets_file_is_refused_not_crashed(self):
        """申请单台账读坏 → **503，不是 500、也不是一份「什么都拒」的预演**。

        两头都要钉住：
          · 不能 500/崩 —— 那只会让人以为是面板坏了，跑去重试。
          · 不能照常出预演结果 —— 「所有条目都被拒、原因还和条目本身无关」的预演，
            就是穿了件外套的 503；管理员唯一能做的事是先把 tickets.json 修好，
            那就直说。
        """
        (self.dir / "tickets.json").write_text("{", encoding="utf-8")
        with _Live(self.make_backend()) as live:
            status, body = self.call(live)
        self.assertEqual(status, 503, body)
        self.assertIn("申请单", body["error"])
        self.assertEqual(self.executor.writes, [])

    def test_unreadable_tickets_refuse_the_whole_revoke(self):
        """读不到台账就分不清哪些是面板发的，`REFUSE_TICKET` 这条护栏静默消失 ——
        而收权不可逆。所以 apply 这条路同样 503、一条都不撤、不留痕。

        （`_all_tickets()` 读失败返回 `None` 而不是 `[]`：`[]` 的含义是
        「面板一条权限都没发过」= 什么都不用拒，方向正好相反。）
        """
        self.tickets([ticket()])
        (self.dir / "tickets.json").write_text("{", encoding="utf-8")
        with _Live(self.make_backend()) as live:
            status, body = self.call(live, policies=[OSS], apply=True, reason="收回存量权限")
        self.assertEqual(status, 503, body)
        self.assertIn("申请单", body["error"])
        self.assertEqual(self.executor.writes, [])
        self.assertEqual(self.log_rows(), [])

    def test_missing_tickets_file_says_so_and_offers_a_way_out(self):
        """文件**不在**和**读坏了**都拒，但必须说成两件事。

        「不在」是全新部署的正常初始状态，而「清理存量权限」恰恰是全新部署最先要做的事 ——
        所以不能只甩一句「读不了台账」让人去查一个根本不存在的故障。
        出路要写在文案里：建一个空台账再来。**这样「相信这里真的什么都没发过」
        就成了一次显式的人工动作**，而不是代码替人默认（代码默认过一次，
        护栏就永远地、静默地消失了）。
        """
        (self.dir / "tickets.json").unlink()
        with _Live(self.make_backend()) as live:
            status, body = self.call(live)
        self.assertEqual(status, 503, body)
        self.assertIn("不存在", body["error"])
        self.assertIn("空台账", body["error"], "要给出路，否则人只能去查一个不存在的故障")
        self.assertNotIn("读不了", body["error"], "「不在」和「读坏了」得分开说")
        self.assertEqual(self.executor.writes, [])

    def test_cloud_error_text_is_scrubbed_before_it_goes_back(self):
        """502 的文案会原样显示在页面上。云侧报错里可能回显 AccessKeyId
        （`SignatureDoesNotMatch` 就带），别让它从这里漏出去。"""
        self.executor.attached_error = ProvisionError(
            "ListPoliciesForUser 失败：AccessKeyId=LTAI5tSECRETKEY 签名不匹配"
        )
        with _Live(self.backend) as live:
            status, body = self.call(live)
        self.assertEqual(status, 502, body)
        self.assertNotIn("LTAI5tSECRETKEY", body["error"])


class TicketFreshnessTests(unittest.TestCase):
    """「面板工单发的」这条拒绝只该挡住**还在生效**的那些。"""

    def test_revoked_ticket_residue_can_be_cleaned_up(self):
        """单子已经回收了，云上却还挂着（回收当时失败、后来单子被人工置成终态）：
        这正是收权功能要清的东西。"""
        granted = revoke.granted_by_panel([ticket(status="revoked")], "aliyun", ACC, USER)
        plan = revoke.plan(
            user=USER,
            attached=[pol(OSS)],
            groups=[GRP],
            wanted=want(OSS, ("group", GRP)),
            panel_granted=granted,
            admin_holders={"other"},
        )
        self.assertEqual(names(plan), [OSS, GRP])

    def test_expired_ticket_residue_can_be_cleaned_up(self):
        """单子还是 `done`、但早就过了到期时间，而回收每轮都失败
        （`flows._revoke_failed` 会把状态留在 `done`）—— 云上那条就是「回收失败的残留」，
        必须能从这里撤掉。

        判据曾经写成 `expires <= 0`（那是「有没有填到期时间」，不是「过没过期」），
        真实数据里恒假 → 这类残留永远清不掉。所以这条同时钉住**判据本身**：
        用一个显式的 `now` 跨过到期时刻，两边结果必须不同。
        """
        expired = ticket(expires_at_ts=1_000_000.0)
        self.assertEqual(revoke.granted_by_panel([expired], "aliyun", ACC, USER), set())
        # 同一张单子，在它还没到期的那个时刻看：照旧受保护（不是「永远不算」）
        self.assertEqual(
            revoke.granted_by_panel([expired], "aliyun", ACC, USER, now=999_999.0),
            {("group", GRP), ("policy", OSS)},
        )

    def test_expired_residue_is_actually_revocable_end_to_end(self):
        granted = revoke.granted_by_panel([ticket(expires_at_ts=1_000_000.0)], "aliyun", ACC, USER)
        plan = revoke.plan(
            user=USER,
            attached=[pol(OSS)],
            groups=[GRP],
            wanted=want(OSS, ("group", GRP)),
            panel_granted=granted,
            admin_holders={"other"},
        )
        self.assertEqual(names(plan), [OSS, GRP])

    def test_a_grant_that_is_still_running_stays_protected(self):
        """反向锁：没到期的照旧拒。把过期判反（`>= now`）的话，正在生效的授权
        就能从这里撤掉，而那张单子还显示「已开通」。"""
        granted = revoke.granted_by_panel(
            [ticket(expires_at_ts=2_000_000.0)], "aliyun", ACC, USER, now=1_000_000.0
        )
        self.assertEqual(granted, {("group", GRP), ("policy", OSS)})

    def test_no_expiry_means_permanent_not_expired(self):
        """长期授权（没有 `expires_at_ts` / 为 0）永远算「还在生效」。
        把「没填」当成「已过期」的话，面板发出去的长期权限全都能从这里撤。"""
        for value in (None, 0, 0.0, ""):
            granted = revoke.granted_by_panel(
                [ticket(expires_at_ts=value)], "aliyun", ACC, USER, now=9_999_999_999.0
            )
            self.assertEqual(granted, {("group", GRP), ("policy", OSS)}, value)

    def test_unparsable_expiry_is_treated_as_not_expired(self):
        """到期时间是坏数据（手改过、旧格式）时宁可多拒：算错方向是「撤掉了还在生效的权限」，
        不可逆；多拒只是让人去那张单子上回收。"""
        for value in ("下周", [], {"ts": 1}, float("nan")):
            granted = revoke.granted_by_panel(
                [ticket(expires_at_ts=value)], "aliyun", ACC, USER, now=9_999_999_999.0
            )
            self.assertEqual(granted, {("group", GRP), ("policy", OSS)}, value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
