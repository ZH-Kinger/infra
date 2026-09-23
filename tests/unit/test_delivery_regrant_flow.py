"""改一把已发出去的长期凭证：`flows.regrant_credential` 的编排那一半。

纯逻辑那一半在 `test_delivery_regrant.py`（拒绝理由、算出来的文档、diff）。这里锁的是
**有 I/O 的那三步**，以及三步之间崩掉会留下什么：

    1) 单子记意图   cred_regrant_requested + fields{cred_regrant_pending, cred_policy_prev}
    2) 写云         issuer.rewrite_policy(user, doc)
    3) 单子写结果   回写到期/权限/生效时间，清 pending，cred_regrant_done

这份文件存在的直接理由：这条路**一次都没被执行过**。审计前它里面调着一个根本不存在的
方法 `self._template_of`，任何一次真实调用都会 `AttributeError`，而全量 3201 条测试全绿。
所以这里的每一条都往「真的跑过一遍」上钉，不做纯参数校验。

按「改错了会怎样」组织，不按函数组织：

  · **模板快照 vs 活模板**（最要紧）：`_approved` 只认单子里那份快照。读活模板的话，
    有人把模板加宽 → 管理员对一张老单点一次「重算」→ **那把已经发出去的 AK 当场拿到
    写权限**，而且新旧 caps 都来自同一份活模板，`fields` 里不会有 `cred_caps`、页面上
    一个字都不会提到权限变了。这条是整条路最容易被将来「顺手简化成读活模板」的地方。
  · **归属判据是单子、不是前缀**：内部前缀 2026-09-23 才从 `tempak-` 改成 `staff-`，
    线上有 `cred_user` 的单子全是老前缀。加一道 `startswith("staff-")` 会把**当前全部
    可改的凭证**挡在外面，其中两把正是这个功能的起因。
  · **三种崩法**逐个锁：写云失败 / 写云成功但回写失败 / 记意图之后进程直接死。
    每一种留下的痕迹不一样，而「有人改过但没改成」这条痕迹正是事后最要紧的。
  · **互斥**：pending 非空时再来一次不许打任何云调用。

云全部替换（`RecordingIssuer` 记下调用顺序），时钟可控，数据全部虚构。
单子是**真的走完一遍发放流程**拿到的，不是手搓的 dict —— 手搓的话，字段名写错
（比如把 `cred_user` 写在 payload 里）测试照样全绿，而线上那条路会整个哑掉。
"""

from __future__ import annotations

import copy
import json
import unittest

from delivery import grants as grants_mod
from delivery import notify as notify_mod
from delivery import regrant as regrant_mod
from delivery import tickets as t
from delivery.flows import FlowError, _approved, _window_start
from delivery.provision import ProvisionError

from . import test_delivery_access_requests as base
from . import test_delivery_credentials as cred_base
from .test_delivery_access_requests import BUCKET, FakeExecutor

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

DAY = 86400.0
#: 30 天的长期凭证：超过 STS_MAX_HOURS 才会走「建子账号 + 挂自定义策略」那条路，
#: 而只有那条路上的凭证有东西可改
LONG = {"bucket": BUCKET, "prefix": "batch/", "hours": 720}
#: 2026-09-23 那次故障里缺的那个动作。S3 兼容客户端建连先探地域，缺了就连不上
LOCATION = "oss:GetBucketLocation"


class RecordingIssuer(FakeExecutor):
    """凭证发放身份的替身：策略文档存在内存里，**调用顺序逐条记下来**。

    顺序是这条路的安全性本身（「先云后账」），只断言最终状态的话，把第 2 步和第 3 步
    调换位置照样全绿 —— 而调换之后，缩短有效期时回写失败会把号提前删掉。
    """

    def __init__(self):
        super().__init__()
        #: [("read"|"rewrite", user)]，按发生顺序
        self.calls = []
        #: 云上现在那份，按子账号名
        self.policies = {}
        #: rewrite_policy 收到的文档（**包括失败那次的**）
        self.written = []
        self.read_fail = None
        self.rewrite_fail = None
        #: 写云那一刻回调，用来看「单子此时长什么样」
        self.on_rewrite = None

    def issue_long_term(self, user, display_name, policy_doc):
        out = super().issue_long_term(user, display_name, policy_doc)
        self.policies[user] = copy.deepcopy(policy_doc)
        return out

    def read_policy(self, user):
        self.calls.append(("read", user))
        if self.read_fail:
            raise self.read_fail
        return copy.deepcopy(self.policies.get(user) or {})

    def rewrite_policy(self, user, doc):
        self.calls.append(("rewrite", user))
        self.written.append((user, copy.deepcopy(doc)))
        if self.on_rewrite:
            self.on_rewrite()
        if self.rewrite_fail:
            # 没落地：云上还是旧的那份
            raise self.rewrite_fail
        self.policies[user] = copy.deepcopy(doc)


def allow_actions(doc) -> set:
    """文档里所有 Allow 语句的动作。

    **只看 Allow** —— 末尾那条兜底 Deny 里有 `oss:PutObjectAcl`，按子串找「上传权限
    回弹了吗」会假阳性。
    """
    out = set()
    for st in (doc or {}).get("Statement") or []:
        if st.get("Effect") == "Allow":
            out.update(st.get("Action") or [])
    return out


def window(doc) -> tuple:
    """(生效, 到期) 两个 ISO 串，从第一条 Allow 的 Condition 上取。"""
    for st in (doc or {}).get("Statement") or []:
        if st.get("Effect") != "Allow":
            continue
        cond = st.get("Condition") or {}
        return (
            next(iter((cond.get("DateGreaterThan") or {}).values()), ""),
            next(iter((cond.get("DateLessThan") or {}).values()), ""),
        )
    raise AssertionError("文档里一条 Allow 都没有")


class Base(unittest.TestCase):
    """一张**真的发出去过**的 30 天长期凭证单。"""

    def setUp(self):
        self.env = cred_base.Env()
        self.issuer = RecordingIssuer()
        self.env.issuer = self.issuer
        self.tid = self.env.run(template="ali-data", payload=dict(LONG))["id"]
        self.user = self.ticket()["cred_user"]
        # 发放时那次调用不该算进后面的断言里
        self.issuer.calls.clear()
        self.issued_actions = len(self.issuer.actions)

    # ── 取状态 ────────────────────────────────────────────────────────
    def ticket(self) -> dict:
        return self.env.store.get(self.tid)

    def events(self) -> list:
        return [e["event"] for e in self.ticket()["events"]]

    def cloud(self) -> dict:
        return self.issuer.policies[self.user]

    def regrant(self, mode=regrant_mod.MODE_REPAIR, **kw):
        kw.setdefault("actor", "on_admin")
        return self.env.flows.regrant_credential(self.tid, mode=mode, **kw)

    # ── 造现场 ────────────────────────────────────────────────────────
    def make_stale(self) -> dict:
        """把云上那份改成「旧代码发的」：缺 `oss:GetBucketLocation`。

        这就是 2026-09-23 那次故障的现场，也是 `repair` 唯一真正要干的事。
        不造这个漂移的话，算出来的和云上一模一样，`changed=False` 会短路掉整条路。
        """
        doc = self.cloud()
        for st in doc["Statement"]:
            if LOCATION in (st.get("Action") or []):
                st["Action"] = [a for a in st["Action"] if a != LOCATION]
        self.issuer.policies[self.user] = doc
        self.assertNotIn(LOCATION, allow_actions(self.cloud()))
        return copy.deepcopy(doc)

    def widen_live_template(self, *caps):
        """把**活模板**的 caps 改宽。单子里那份快照不动。"""
        for spec in self.env.templates["templates"]:
            if spec["id"] == "ali-data":
                spec["caps"] = list(caps)
        # 前提：单子里那份快照确实还是窄的，否则下面的断言什么也证明不了
        self.assertEqual(tuple(_approved(self.ticket()).caps), ("list", "download"))

    def set_field(self, **fields):
        """直接改单子上的字段（造历史数据用）。"""
        return self.env.store.update(
            self.tid,
            actor="test",
            expect=[t.DONE],
            event="test_fixture",
            note="造现场",
            fields=fields,
        )


# ── 1. happy path：四步真的都执行了 ──────────────────────────────────────


class HappyPathTests(Base):
    """`repair` 跑通一次，逐步验：读了云、写的是算出来的新文档、单子上两条事件按序出现。"""

    def test_repair_reads_cloud_writes_new_doc_then_settles_ledger(self):
        stale = self.make_stale()
        seen = {}
        self.issuer.on_rewrite = lambda: seen.update(
            events=self.events(), pending=dict(self.ticket().get("cred_regrant_pending") or {})
        )

        out = self.regrant()

        # ① 云调用：先读后写，各一次，收的都是单子上那个子账号名
        self.assertEqual(self.issuer.calls, [("read", self.user), ("rewrite", self.user)])
        # ② 写下去的是**算出来的新文档**，不是读回来那份原样回写
        ((_, doc),) = self.issuer.written
        self.assertIn(LOCATION, allow_actions(doc))
        self.assertNotEqual(doc, stale)
        self.assertEqual(self.cloud(), doc)
        # ③ 顺序恒为「先云后账」：写云那一刻，意图已记、结果还没记
        self.assertEqual(seen["events"][-1], "cred_regrant_requested")
        self.assertEqual(seen["pending"]["mode"], regrant_mod.MODE_REPAIR)
        self.assertEqual(self.events()[-2:], ["cred_regrant_requested", "cred_regrant_done"])
        self.assertNotIn("cred_regrant_failed", self.events())
        # ④ 收尾：pending 清空，`cred_policy_prev` 是**现场读回来那份**（不是按当前代码
        #    重算的「理论旧文档」—— 云上那份可能被人在控制台改过，存重算的等于存了个假回滚点）
        self.assertEqual(out["cred_regrant_pending"], {})
        self.assertEqual(out["cred_policy_prev"], stale)
        self.assertNotEqual(out["cred_policy_prev"], doc)

    def test_repair_changes_neither_ak_nor_expiry(self):
        """`repair` 不扩不缩：AK 不动、到期时间不动、权限不动。

        「AK 不动」是这条路存在的全部理由 —— 对方的服务不用改配置、不用停服。
        真把 AK 换了的话（比如实现成「撤了重发」），单子上一个字都不会变，
        只有使用方会在某个半夜发现连不上。
        """
        before = self.ticket()
        self.make_stale()
        after = self.regrant()

        self.assertEqual(after["cred_ak_id"], before["cred_ak_id"])
        self.assertEqual(after["cred_user"], before["cred_user"])
        self.assertEqual(after["expires_at_ts"], before["expires_at_ts"])
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertNotIn("cred_caps", after)
        # 发放身份上除了读/写策略，没干别的（没建号、没删号、没发新 AK）
        self.assertEqual(self.issuer.actions[self.issued_actions :], [])

    def test_repair_caches_window_start_on_the_ticket(self):
        """生效时间**第一次从云上读回来时顺手记进单子**。

        单子上历史没存过它，而重算策略必须知道它（不许拿 now 兜底，那是静默扩权）。
        记下来之后，下一次就不再依赖「云上那份读得回来」。
        """
        self.assertNotIn("cred_not_before", self.ticket())
        self.make_stale()
        out = self.regrant()

        nb = out["cred_not_before"]
        self.assertEqual(nb, out["done_at_ts"])
        self.assertEqual(window(self.cloud())[0], grants_mod.iso8601_bj(nb))

    def test_expire_moves_both_date_fields_and_the_cloud_window(self):
        new = self.ticket()["expires_at_ts"] + 30 * DAY
        out = self.regrant(regrant_mod.MODE_EXPIRE, expire=new, reason="对方项目延期一个月")

        self.assertEqual(out["expires_at_ts"], new)
        # 和开通那条路同一种写法：两处格式不一样的话，同一张单子上的两条日期
        # 看起来像来自两个系统
        self.assertEqual(out["expires_at"], t.now_iso(lambda: new))
        self.assertEqual(window(self.cloud())[1], grants_mod.iso8601_bj(new))
        # 生效时间没跟着动
        self.assertEqual(window(self.cloud())[0], grants_mod.iso8601_bj(out["cred_not_before"]))
        self.assertEqual(out["cred_regrant_pending"], {})

    def test_caps_narrowing_lands_on_cloud_and_does_not_bounce_back(self):
        """收窄权限之后再点一次「重算」，收掉的权限不许回弹。

        回弹的路子是 `effective_caps` 先读模板：模板没变，一次「无害的重算」
        就把刚收窄的权限悄悄放回去了。这里从 flows 这一层再钉一遍。
        """
        out = self.regrant(regrant_mod.MODE_CAPS, caps=["list"], reason="对方不再需要下载")

        self.assertEqual(out["cred_caps"], ["list"])
        self.assertNotIn("oss:GetObject", allow_actions(self.cloud()))
        self.assertIn("oss:ListObjects", allow_actions(self.cloud()))

        self.make_stale()
        self.regrant()
        self.assertIn(LOCATION, allow_actions(self.cloud()))
        self.assertNotIn("oss:GetObject", allow_actions(self.cloud()))
        self.assertEqual(self.ticket()["cred_caps"], ["list"])


class GateTests(Base):
    """进门前的几道闸。每条都对应一个「点错了会发生什么」。"""

    def test_only_done_tickets(self):
        self.env.store.update(
            self.tid, actor="test", expect=[t.DONE], to=t.REVOKED, event="test_fixture"
        )
        with self.assertRaises(FlowError) as got:
            self.regrant()
        self.assertEqual(got.exception.status, 409)
        self.assertEqual(self.issuer.calls, [])

    def test_widening_modes_require_a_reason(self):
        """延长有效期 / 改权限要写明理由：事后要回答「谁在什么时候为什么改的」。

        `repair` 不扩不缩，不强求 —— 它是故障处置，多一道填空只会让人绕过它。
        """
        for mode, kw in (
            (regrant_mod.MODE_EXPIRE, {"expire": self.ticket()["expires_at_ts"] + DAY}),
            (regrant_mod.MODE_CAPS, {"caps": ["list"]}),
        ):
            with self.assertRaises(FlowError) as got:
                self.regrant(mode, reason="   ", **kw)
            self.assertEqual(got.exception.status, 400, mode)
            self.assertEqual(self.issuer.calls, [], mode)
        self.make_stale()
        self.regrant()  # repair 没有理由也放行
        self.assertIn(LOCATION, allow_actions(self.cloud()))

    def test_snapshot_without_platform_or_account_is_refused(self):
        ticket = self.ticket()
        ticket["template"].pop("account")
        self.set_field(template=ticket["template"])
        with self.assertRaises(FlowError) as got:
            self.regrant()
        self.assertEqual(got.exception.status, 409)
        self.assertEqual(self.issuer.calls, [])


# ── 2. 模板快照 vs 活模板（审计 M-2）─────────────────────────────────────


class SnapshotNotLiveTemplateTests(Base):
    """**`_approved` 只认单子里那份快照，一个字都不读活模板。**

    审批卡上写的是提交那一刻的 caps 和桶清单，那才是批准的范围。这一条要是被将来
    「顺手简化成读活模板」，症状是完全静默的：`fields` 里不会有 `cred_caps`
    （新旧都来自同一份活模板），页面上不会提权限变了，事件里也看不出来 ——
    只有云上那把已经发出去的 AK 悄悄多了写权限。
    """

    def test_widened_live_template_does_not_leak_write_into_a_repair(self):
        self.widen_live_template("list", "download", "write")
        self.make_stale()

        out = self.regrant()

        # 重算该补的补上了（这次改动的正事）
        self.assertIn(LOCATION, allow_actions(self.cloud()))
        # 但活模板新加的「上传」一个都不许下去
        self.assertNotIn("oss:PutObject", allow_actions(self.cloud()))
        self.assertNotIn("oss:AbortMultipartUpload", allow_actions(self.cloud()))
        self.assertEqual(
            allow_actions(self.cloud()) & {"oss:PutObject", "oss:PutObjectTagging"}, set()
        )
        # 而且单子上也不该多出一条「权限变了」—— 它本来就没变
        self.assertNotIn("cred_caps", out)

    def test_caps_beyond_the_snapshot_are_refused_even_when_the_live_template_allows(self):
        """管理员显式点「加上传」也不行：模板 caps 是那张飞书批条批准的范围。

        面板单方面扩大 = 绕过审批。要更大权限只能重新申请（重新走一次审批）。
        """
        self.widen_live_template("list", "download", "write")

        with self.assertRaises(FlowError) as got:
            self.regrant(
                regrant_mod.MODE_CAPS, caps=["list", "download", "write"], reason="对方要写"
            )

        self.assertEqual(got.exception.status, 409)
        self.assertIn("超出", str(got.exception))
        # 读了云（算 diff 要），但一个字都没写下去
        self.assertEqual(self.issuer.calls, [("read", self.user)])
        self.assertNotIn("oss:PutObject", allow_actions(self.cloud()))
        self.assertNotIn("cred_regrant_requested", self.events())

    def test_bucket_removed_from_the_snapshot_blocks_the_regrant(self):
        """快照里的桶清单同理：桶被移出之后就不该再照着它发权限了。"""
        ticket = self.ticket()
        ticket["template"]["buckets"] = [{"name": "some-other-bucket", "region": "cn-hangzhou"}]
        self.set_field(template=ticket["template"])

        with self.assertRaises(FlowError) as got:
            self.regrant()
        self.assertEqual(got.exception.status, 409)
        self.assertEqual(self.issuer.written, [])

    def test_old_snapshot_missing_fields_neither_crashes_nor_gets_generous(self):
        """老快照缺字段（`_EXEC_FIELDS` 后来加过东西）：`_approved` 不崩，而且**偏保守**。

        缺 `buckets` → 没有任何桶能过白名单；缺 `caps` → 读不出这把凭证现在能干什么；
        两种都拒。缺 `max_hours` 退回 `Template` 的默认值 **1 小时**，于是延期只能延到
        「生效后 1 小时」之内 —— 等于延不了。三种都是「不确定就不动」。
        """
        snap = self.ticket()["template"]
        for gone in ("buckets", "caps", "max_hours"):
            with self.subTest(missing=gone):
                short = {k: v for k, v in snap.items() if k != gone}
                self.set_field(template=short)
                tpl = _approved(self.ticket())  # 不抛
                self.assertEqual(tpl.platform, "aliyun")
                if gone == "max_hours":
                    self.assertEqual(tpl.max_hours, 1)
                    with self.assertRaises(FlowError) as got:
                        self.regrant(
                            regrant_mod.MODE_EXPIRE,
                            expire=self.ticket()["expires_at_ts"] + DAY,
                            reason="延一天",
                        )
                    self.assertIn("模板允许的 1 小时", str(got.exception))
                else:
                    with self.assertRaises(FlowError):
                        self.regrant()
                self.assertEqual(self.issuer.written, [])


# ── 3. 归属判据是单子、不是前缀 ──────────────────────────────────────────


class OwnershipByTicketTests(Base):
    """`user` 来自 `ticket["cred_user"]` —— 面板发放时自己写进去的，本身就是
    「这个号是我们发的」的权威证据。**别在这里加前缀门。**"""

    def test_legacy_tempak_user_can_still_be_regranted(self):
        """内部前缀 2026-09-23 才从 `tempak-` 改成 `staff-`。

        线上有 `cred_user` 的单子全是老前缀，一道 `startswith(USER_PREFIX)` 会把
        **当前全部可改的凭证**挡在外面，其中两把正是这个功能的起因（策略缺
        `oss:GetBucketLocation`）。前缀会变，单子不会。
        """
        legacy = "tempak-lisi-a1b2c3"
        # 前提：这个名字确实过不了「新前缀」那道门，否则本用例什么也证明不了
        self.assertFalse(legacy.startswith(grants_mod.USER_PREFIX))
        self.issuer.policies[legacy] = self.issuer.policies.pop(self.user)
        self.set_field(cred_user=legacy)
        self.user = legacy
        self.make_stale()

        self.regrant()

        self.assertEqual(self.issuer.calls, [("read", legacy), ("rewrite", legacy)])
        self.assertIn(LOCATION, allow_actions(self.cloud()))

    def test_short_term_ticket_has_nothing_to_change(self):
        """≤12 小时走 STS：云上既没有策略对象也没有子账号，什么都改不了。

        判据同样是 `cred_user`（空），不是申请时填的小时数 —— 模板改过、走过重试时
        小时数和现实对不上。
        """
        self.set_field(cred_user="")
        with self.assertRaises(FlowError) as got:
            self.regrant()
        self.assertEqual(got.exception.status, 409)
        self.assertEqual(self.issuer.calls, [])


# ── 4. 三种崩法 ─────────────────────────────────────────────────────────


class CrashTests(Base):
    """三步之间崩掉，各留下什么痕迹。三种现场不一样，管理员的下一步动作也不一样。"""

    def test_cloud_write_fails_keeps_pending_and_leaves_the_ledger_untouched(self):
        """写云失败：pending **留着**，单子上的到期/权限一个字没动。

        pending 是「有人改过但没改成」的唯一痕迹，清掉它等于把这次失败抹干净。
        """
        before = self.ticket()
        self.make_stale()
        self.issuer.rewrite_fail = ProvisionError("NoPermission: ram:CreatePolicyVersion")

        with self.assertRaises(FlowError) as got:
            self.regrant(
                regrant_mod.MODE_EXPIRE,
                expire=before["expires_at_ts"] + 10 * DAY,
                reason="对方项目延期",
            )

        self.assertEqual(got.exception.status, 502)
        self.assertIn("CreatePolicyVersion", str(got.exception))
        after = self.ticket()
        self.assertEqual(after["cred_regrant_pending"]["mode"], regrant_mod.MODE_EXPIRE)
        self.assertEqual(self.events()[-2:], ["cred_regrant_requested", "cred_regrant_failed"])
        # 台账一个字没动：延长失败时号会按**旧的**到期时间被回收 —— 服务断，但没越权
        self.assertEqual(after["expires_at_ts"], before["expires_at_ts"])
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertNotIn("cred_caps", after)
        # 云上也没落地
        self.assertNotIn(LOCATION, allow_actions(self.cloud()))

    def test_cloud_written_but_ledger_write_fails(self):
        """**最危险的那种**：云上已经是新的，单子还是旧的。

        现状（如实锁住，不是背书）：
          · 第 3 步的异常**直接抛给调用方**，不被吞掉 —— 管理员看到的是一个错误，
            不是「已完成」。这一点是对的。
          · `cred_regrant_pending` 留在单子上 → 待办页据此报一条「面板上写的有效期和
            云上判的可能不一致」。**对齐必须是人点的**，不能让定时任务自动回填：
            自动回填会把一次没改成的改动悄悄变成既成事实。
          · 他**再点一次会被 409 挡住**（互斥闸），必须先处理那条 pending。
            也就是说这条路今天没有「自助恢复」的按钮，只能人工核对云上现状。
          · 单子上的 `expires_at_ts` 仍是旧值，所以到期回收会按**旧**时间来：
            延长的场景下号会被提前回收（服务断，不越权），缩短的场景下云上已经 403。
        """
        before = self.ticket()
        new_expire = before["expires_at_ts"] + 10 * DAY
        real = self.env.store.update

        def flaky(ticket_id, *, actor, expect, event, note="", fields=None):
            if event == "cred_regrant_done":
                raise t.TicketError("申请单写不进去（磁盘满）", 500)
            return real(
                ticket_id, actor=actor, expect=expect, event=event, note=note, fields=fields
            )

        self.env.store.update = flaky
        self.addCleanup(setattr, self.env.store, "update", real)

        with self.assertRaises(t.TicketError):
            self.regrant(regrant_mod.MODE_EXPIRE, expire=new_expire, reason="对方项目延期")

        # 云上是新的
        self.assertEqual(window(self.cloud())[1], grants_mod.iso8601_bj(new_expire))
        # 单子是旧的，pending 还在
        after = self.ticket()
        self.assertEqual(after["expires_at_ts"], before["expires_at_ts"])
        self.assertEqual(after["cred_regrant_pending"]["expire"], new_expire)
        self.assertEqual(self.events()[-1], "cred_regrant_requested")
        self.assertNotIn("cred_regrant_done", self.events())
        # 再点一次：被互斥闸挡住，云上不会被改第二次
        self.env.store.update = real
        with self.assertRaises(FlowError) as got:
            self.regrant(regrant_mod.MODE_EXPIRE, expire=new_expire, reason="再试一次")
        self.assertEqual(got.exception.status, 409)
        self.assertEqual(len(self.issuer.written), 1)

    def test_process_dies_right_after_recording_the_intent(self):
        """记完意图、还没写云就整个死掉（容器被 kill / OOM）。

        用 `KeyboardInterrupt` 模拟：它是 `BaseException`，不会被那句
        `except Exception` 吞掉，效果等同于进程在 `rewrite_policy` 里没回来。
        留下的现场：pending + `cred_policy_prev` 都在，云上一个字没动，
        而且**没有** `cred_regrant_failed` —— 和「写云失败」是两种现场。
        """
        stale = self.make_stale()
        self.issuer.rewrite_fail = KeyboardInterrupt("容器被 kill")

        with self.assertRaises(KeyboardInterrupt):
            self.regrant()

        after = self.ticket()
        self.assertEqual(after["cred_regrant_pending"]["mode"], regrant_mod.MODE_REPAIR)
        self.assertEqual(after["cred_policy_prev"], stale)
        self.assertEqual(self.events()[-1], "cred_regrant_requested")
        self.assertNotIn("cred_regrant_failed", self.events())
        self.assertEqual(self.cloud(), stale)

    def test_read_policy_failure_never_records_an_intent(self):
        """连云上那份都读不回来：第 1 步都不该写 —— 什么都还没发生。"""
        self.issuer.read_fail = ProvisionError("EntityNotExist.Policy")
        with self.assertRaises(ProvisionError):
            self.regrant()
        self.assertNotIn("cred_regrant_requested", self.events())
        self.assertEqual(self.ticket().get("cred_regrant_pending"), None)


# ── 5. 互斥（审计 M-4）──────────────────────────────────────────────────


class MutexTests(Base):
    """pending 当互斥量用：上一次没做完就不许再来一次。

    没有跨三步的事务，两个管理员同时点的话会是「云上是 B 写的、单子是 A 写的、
    谁都不知道」。顺带让「有人改过但没改成」这条痕迹变成**会拦路的**信号，
    而不是一条没人看的字段。
    """

    def test_pending_blocks_the_next_call_before_any_cloud_call(self):
        self.set_field(cred_regrant_pending={"mode": "repair", "actor": "on_other"})

        for mode, kw in (
            (regrant_mod.MODE_REPAIR, {}),
            (regrant_mod.MODE_EXPIRE, {"expire": self.ticket()["expires_at_ts"] + DAY}),
            (regrant_mod.MODE_CAPS, {"caps": ["list"]}),
        ):
            with self.subTest(mode=mode), self.assertRaises(FlowError) as got:
                self.regrant(mode, reason="再来一次", **kw)
            self.assertEqual(got.exception.status, 409, mode)
            # **一次都没碰云** —— 连 read_policy 都不许打
            self.assertEqual(self.issuer.calls, [], mode)
        self.assertNotIn("cred_regrant_requested", self.events())

    def test_a_settled_empty_pending_does_not_block(self):
        """成功那次留下的是 `{}`，不该被当成「有一次没做完」。"""
        self.make_stale()
        self.regrant()
        self.assertEqual(self.ticket()["cred_regrant_pending"], {})
        # 第二次照常受理（这次算出来没变化，见 NoOpTests）
        self.regrant()


# ── 6. _window_start：纯函数 ────────────────────────────────────────────


class WindowStartTests(unittest.TestCase):
    """从策略文档读回生效时间。**这是「不许拿 now 兜底」那条规矩唯一的落地点。**

    只认**所有** Allow 语句都同意的那个值：不一致说明这条策略被人在控制台改过，
    那时候「生效时间是几点」本身没有唯一答案，宁可说不知道。
    """

    NB = 1_800_000_000.0
    EXP = NB + 30 * DAY

    def real_doc(self, **kw):
        kw.setdefault("caps", ("list", "download"))
        kw.setdefault("not_before", self.NB)
        kw.setdefault("expire", self.EXP)
        return grants_mod.build_policy("aliyun", BUCKET, prefix="batch/", **kw)

    def test_real_build_policy_output_round_trips(self):
        """**边界**：真实文档喂进去必须拿得到值。

        只测手搓的文档，`build_policy` 哪天换个时间键名（或某条语句不再叠时间窗），
        这里照样全绿，而线上每一次 regrant 都会被拒。所以两朵云的真实产出都过一遍。
        """
        for platform in ("aliyun", "volcano"):
            with self.subTest(platform=platform):
                doc = grants_mod.build_policy(
                    platform,
                    BUCKET,
                    prefix="batch/",
                    caps=("list", "download", "write"),
                    not_before=self.NB,
                    expire=self.EXP,
                )
                self.assertEqual(_window_start(doc), self.NB)

    def test_every_cap_combination_still_round_trips(self):
        """caps 决定出现哪几条语句。任何一条漏了时间窗，这里就读不回来了。"""
        for caps in (("list",), ("download",), ("write",), ("list", "write")):
            with self.subTest(caps=caps):
                self.assertEqual(_window_start(self.real_doc(caps=caps)), self.NB)

    def test_disagreeing_allow_statements_return_none(self):
        doc = self.real_doc()
        doc["Statement"][0]["Condition"]["DateGreaterThan"]["acs:CurrentTime"] = (
            grants_mod.iso8601_bj(self.NB + 3600)
        )
        self.assertIsNone(_window_start(doc))

    def test_no_time_condition_returns_none(self):
        doc = self.real_doc()
        for st in doc["Statement"]:
            st.pop("Condition", None)
        self.assertIsNone(_window_start(doc))

    def test_list_value_returns_none(self):
        """控制台上手改过的策略可能把值写成数组：读不出唯一时刻，就别猜。"""
        doc = self.real_doc()
        for st in doc["Statement"]:
            cond = st.get("Condition") or {}
            if "DateGreaterThan" in cond:
                cond["DateGreaterThan"]["acs:CurrentTime"] = [grants_mod.iso8601_bj(self.NB)]
        self.assertIsNone(_window_start(doc))

    def test_deny_statements_do_not_participate(self):
        """末尾那条兜底 Deny 不该影响判断 —— 它本来就不带时间窗。

        两个方向都锁：Deny 上挂一个**不一样**的时间不该把结论变成 None；
        而只有 Deny 带时间时，结论必须是 None（那不是生效时间）。
        """
        doc = self.real_doc()
        denies = [st for st in doc["Statement"] if st.get("Effect") == "Deny"]
        self.assertTrue(denies, "build_policy 末尾应当有兜底 Deny")
        for st in denies:
            st["Condition"] = {"DateGreaterThan": {"acs:CurrentTime": grants_mod.iso8601_bj(0)}}
        self.assertEqual(_window_start(doc), self.NB)

        only_deny = {"Version": "1", "Statement": denies}
        self.assertIsNone(_window_start(only_deny))

    def test_utc_z_suffix_parses(self):
        """`Z` 结尾的写法也要认：同一条策略可能是别的工具写上去的。"""
        doc = {
            "Version": "1",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["oss:ListObjects"],
                    "Resource": ["acs:oss:*:*:b"],
                    "Condition": {"DateGreaterThan": {"acs:CurrentTime": "2027-01-15T08:00:00Z"}},
                }
            ],
        }
        self.assertEqual(_window_start(doc), 1_800_000_000.0)

    def test_garbage_and_empty_documents_return_none(self):
        for doc in (None, {}, {"Statement": []}, {"Statement": None}):
            with self.subTest(doc=doc):
                self.assertIsNone(_window_start(doc))
        bad = self.real_doc()
        for st in bad["Statement"]:
            cond = st.get("Condition") or {}
            if "DateGreaterThan" in cond:
                cond["DateGreaterThan"]["acs:CurrentTime"] = "前天下午"
        self.assertIsNone(_window_start(bad))


class WindowStartFallbackTests(Base):
    """`_window_start` 的 docstring 说「返回 None 时 `regrant.plan` 会据此整个拒掉」——
    **实际不是**：`plan` 会回落单子上缓存的 `cred_not_before`。

    行为本身是安全的（缓存的是**真实发放时刻**，不是 `now`，所以不存在「让尚未生效的
    凭证提前生效」那种静默扩权），但文档过度承诺了。这里两条路都锁住，免得将来
    有人照着 docstring 把回落删掉 —— 删掉之后，任何一条被人在控制台动过窗口的策略
    就再也修不了了，而那恰恰是最需要修的那种。
    """

    def mangle_cloud_window(self):
        """把云上那份改成「两条 Allow 的生效时间不一致」= 控制台手改过。"""
        doc = self.cloud()
        allows = [st for st in doc["Statement"] if st.get("Effect") == "Allow"]
        allows[0]["Condition"]["DateGreaterThan"]["acs:CurrentTime"] = grants_mod.iso8601_bj(
            self.ticket()["done_at_ts"] - 7 * DAY
        )
        self.issuer.policies[self.user] = doc
        self.assertIsNone(_window_start(self.cloud()))

    def test_cached_not_before_carries_the_regrant_when_the_cloud_window_is_unreadable(self):
        nb = self.ticket()["done_at_ts"]
        self.set_field(cred_not_before=nb)
        self.mangle_cloud_window()

        self.regrant()

        self.assertEqual(window(self.cloud())[0], grants_mod.iso8601_bj(nb))
        self.assertEqual(self.ticket()["cred_regrant_pending"], {})

    def test_without_a_cached_value_it_really_is_refused(self):
        self.assertNotIn("cred_not_before", self.ticket())
        self.mangle_cloud_window()

        with self.assertRaises(FlowError) as got:
            self.regrant()
        self.assertEqual(got.exception.status, 409)
        self.assertIn("生效时间", str(got.exception))
        self.assertEqual(self.issuer.written, [])


# ── 7. changed=False 短路 ───────────────────────────────────────────────


class NoOpTests(Base):
    """算出来和云上一模一样时不写云。

    阿里一条策略只有 5 个版本，白写一次就白吃一格 —— 点五次「重算」就再也改不了了。
    """

    def test_identical_policy_skips_the_cloud_write(self):
        out = self.regrant()

        self.assertEqual(self.issuer.calls, [("read", self.user)])
        self.assertEqual(self.issuer.written, [])
        self.assertEqual(out, self.ticket())

    @unittest.expectedFailure
    def test_known_gap_no_op_is_indistinguishable_from_a_real_change(self):
        """**已知待改，不是背书。**

        这条路既不记事件、返回值又和成功路径一样（都是那张单子），于是调用方
        （页面 / CLI）分不出「已经改好了」和「根本没改」。管理员点完「重算」看到
        一条成功提示，然后去查为什么使用方还是连不上 —— 而真相是这次什么都没发生。

        期望：要么留一条事件（比如 `cred_regrant_noop`），要么把「没有变化」
        回给调用方。现状两样都没有，所以本用例预期失败。

        复现：对一张策略已经是最新的单子调 `regrant_credential(mode="repair")`，
        对比事件列表 —— 调用前后完全一致。
        """
        before = self.events()
        self.regrant()
        self.assertNotEqual(self.events(), before)


# ── 8. 延期之后的到期提醒（已知缺陷）───────────────────────────────────


class RemindAfterExtendTests(Base):
    """延期之后，到期提醒不会再发。

    `Flows._reminded` 按**事件条数**算「这一档提醒过了没有」，而延期不重置这些标记。
    于是一把「快到期 → 提醒 → 延长 90 天」的凭证，在新的到期日前**一声不吭**地断。
    这正是到期提醒要防的那件事本身。
    """

    def setUp(self):
        super().setUp()
        self.rec = notify_mod.RecordingNotifier()
        self.env.flows._notify = self.rec

    def at(self, left):
        """把时钟拨到「离到期还剩 left 秒」。"""
        self.env.now[0] = float(self.ticket()["expires_at_ts"]) - left

    def reminders(self) -> list:
        return [e for e in self.events() if e.startswith("expiry_reminded")]

    def extend(self, days):
        new = self.ticket()["expires_at_ts"] + days * DAY
        self.regrant(regrant_mod.MODE_EXPIRE, expire=new, reason=f"对方项目延期 {days} 天")

    def test_tiers_fire_before_the_extension(self):
        """前提：延期之前，两档提醒都是正常发得出去的。

        没有这一条的话，下面那个 expectedFailure 可能只是「提醒本来就没跑起来」。
        """
        self.at(5 * DAY)
        self.env.flows.remind_expiring()
        self.assertEqual(self.reminders(), ["expiry_reminded:7"])
        self.at(0.5 * DAY)
        self.env.flows.remind_expiring()
        self.assertEqual(self.reminders(), ["expiry_reminded:7", "expiry_reminded:1"])
        self.assertEqual([e for e, _, _ in self.rec.sent], ["expiring", "expiring"])

    @unittest.expectedFailure
    def test_known_bug_no_reminder_after_an_extension(self):
        """**已知缺陷，源码未修。**

        复现：
          1. 一张 30 天的长期凭证单，剩 5 天时定时任务发了 7 天档提醒，
             剩 12 小时时发了 1 天档提醒（`expiry_reminded:7` / `expiry_reminded:1`）；
          2. 管理员用 `regrant_credential(mode="expire")` 把到期时间延后 60 天
             （这正是最常见的时机 —— 就是因为快到期了才延）；
          3. 把时钟拨到**新**到期日前 5 天，跑 `remind_expiring()`。

        期望：再发一次 7 天档提醒（新的一轮到期，和上一轮无关）。
        实际：一条都不发。`_reminded()` 仍然返回 `{7, 1}`，而 `_remind_tier` 里
        `any(done <= days for done in self._reminded(ticket))` 对两档都成立 →
        两档都被跳过。凭证在新的到期日**静默**失效。

        建议的修法（供 dev 判断）：`regrant_credential` 改到期成功时，往单子上补一条
        `expiry_remind_failed:<tier>` 抵消计数，或者给 `_reminded` 加一个「只数
        `expires_at_ts` 最后一次变更之后的事件」的下界。后者更干净，但要在单子上
        记下那次变更的时间点。
        """
        self.at(5 * DAY)
        self.env.flows.remind_expiring()
        self.at(0.5 * DAY)
        self.env.flows.remind_expiring()
        sent_before = len(self.rec.sent)

        self.extend(60)

        self.at(5 * DAY)
        self.env.flows.remind_expiring()
        self.assertEqual(len(self.rec.sent), sent_before + 1)

    def test_known_bug_scope_only_the_seven_day_tier_is_lost_when_one_tier_fired(self):
        """缺陷范围的下限（**这条是现状，通过**）：只发过 7 天档就延期时，
        7 天档从此哑掉，1 天档还活着 —— 使用方仍会收到最后 24 小时的那一声。

        单独写出来是为了让上面那条 expectedFailure 的严重性有个准确的边界：
        「两档都发过之后再延期」才是彻底静默的那种，而那正是最常见的时机。
        """
        self.at(5 * DAY)
        self.env.flows.remind_expiring()
        self.assertEqual(self.reminders(), ["expiry_reminded:7"])

        self.extend(60)

        self.at(5 * DAY)
        self.env.flows.remind_expiring()
        self.assertEqual(self.reminders(), ["expiry_reminded:7"])  # 7 天档没再发
        self.at(0.5 * DAY)
        self.env.flows.remind_expiring()
        self.assertEqual(self.reminders()[-1], "expiry_reminded:1")  # 1 天档还在


# ── 单子上不许出现密钥 ──────────────────────────────────────────────────


class NoSecretsTests(Base):
    """改策略这条路上新增了两个会落盘的字段（`cred_regrant_pending` /
    `cred_policy_prev`）。策略文档里本来就没有密钥，但**新字段一律要过这道检查** ——
    `tickets.json` 泄漏等于凭证泄漏。"""

    def test_nothing_secret_lands_in_the_ticket_file(self):
        self.make_stale()
        self.regrant(
            regrant_mod.MODE_CAPS, caps=["list"], reason="对方不再需要下载", actor="on_admin"
        )
        raw = json.dumps(self.ticket(), ensure_ascii=False)
        for bad in ("lt-secret", "sts-secret", "secret_access_key", "AccessKeySecret"):
            self.assertNotIn(bad, raw, bad)
