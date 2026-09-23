"""服务访问单怎么提交：`Flows._validate` 的 `KIND_SERVICE` 分支 + 到期时间。

为什么单独有一条分支（**这条分支就是这个文件要锁的东西**）
────────────────────────────────────────────────────────
自建服务没有云账号、没有子账号、没有桶，表单上只有申请理由（和可选的到期日）。
在这条分支加进去之前，service 模板的提交会一路掉进最后那个「开账号」兜底，去校验一个
服务访问根本不存在的 `username` —— 结果是**每一次提交都报「用户名不符合规则」**，
这条链根本走不通。所以这里锁两件事：

  · service 单**不要求也不接受** `username`（掉回兜底会当场红）；
  · 期限的四种情形（长期 / 长期却填了日期 / 有上限却没填 / 填超上限）各自的结果。

到期时间为什么值得一起锁
────────────────────────
`_expires_at` 决定单子写不写 `expires_at_ts`，而**网关那道门就是看这个字段判过没过期的**
（`service_access.allowed`）。两边对不上的表现是「批了却进不去」或「过期了还进得去」，
两种都不会报错。所以这里把「提交 → 开通 → 网关判定」串起来验一遍。

夹具沿用 `test_delivery_access_requests`（同一套 Harness / FakeFeishu / 假执行身份）。
离线，数据虚构。
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from delivery import catalog as catalog_mod
from delivery import platforms as platforms_mod
from delivery import service_access as sa
from delivery import tickets as t
from delivery.approval import ApprovalConfig, FeishuApproval
from delivery.flows import FlowError
from delivery.server import Backend

from .test_delivery_access_requests import CONFIG, LI, TEMPLATES, FakeFeishu, Harness

SERVICE = "mlflow"
LONG = "svc-mlflow"  # max_days=0：长期占用，当前线上的 MLflow 模板就是这个
DEFINITION = "service"  # 服务访问自己那条审批定义（表单只有服务名 + 期限，没有云账号）
DATED = "svc-mlflow-90"  # max_days=30：有上限，必须选到期日
BJ = timezone(timedelta(hours=8))


def _templates():
    data = json.loads(json.dumps(TEMPLATES))
    data["templates"].extend(
        [
            {
                "id": LONG,
                "kind": catalog_mod.KIND_SERVICE,
                "platform": platforms_mod.INTERNAL,
                "title": "MLflow 访问",
                "service": SERVICE,
                "max_days": 0,
                # 这条模板走哪条飞书审批定义。空 = 老那条；写了就必须配得出来
                "approval": DEFINITION,
            },
            {
                "id": DATED,
                "kind": catalog_mod.KIND_SERVICE,
                "platform": platforms_mod.INTERNAL,
                "title": "MLflow 访问（限期）",
                "service": SERVICE,
                "max_days": 30,
            },
        ]
    )
    return data


class ServiceRequestTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(_templates())

    def today(self):
        return datetime.fromtimestamp(self.h.now[0], BJ).date()

    def day(self, offset: int) -> str:
        return (self.today() + timedelta(days=offset)).isoformat()

    def submit(self, template=LONG, payload=None):
        return self.h.submit(applicant=LI, template=template, payload=payload or {})

    def finish(self, ticket):
        """审批通过 → 开通。service 单面板一行云都不写，直接到「已完成」。"""
        self.h.approve(ticket)
        return self.h.flows.sync(ticket["id"], force=True)

    # ── username：这条分支存在的理由 ──

    def test_empty_form_is_a_valid_service_request(self):
        """**回归**：没有 `KIND_SERVICE` 分支时，这一句会掉进「开账号」兜底、
        报「用户名不符合规则」—— 整条链提交不了。"""
        ticket = self.submit()
        self.assertEqual(ticket["kind"], catalog_mod.KIND_SERVICE)
        self.assertEqual(ticket["payload"], {})
        self.assertEqual(ticket["template"]["service"], SERVICE)

    def test_username_is_not_accepted_either(self):
        """不要求 `username`，也**不收**：塞进来的字段不会进单子。

        存下来就会在台账和审批单上显示一个「子账号名」，而这张单和任何子账号都无关。
        """
        ticket = self.submit(payload={"username": "hacker", "cloud_user": "lisi", "days": 999})
        self.assertEqual(ticket["payload"], {})
        self.assertNotIn("hacker", json.dumps(ticket, ensure_ascii=False))

    def test_the_account_branch_is_still_strict(self):
        """上面那条不是「校验被放松了」，只是 service 走的是另一条路 ——
        开账号单交空表单照样报「用户名不符合规则」。"""
        with self.assertRaises(FlowError) as caught:
            self.h.submit(applicant=LI, template="new-user", payload={})
        self.assertIn("用户名", str(caught.exception))

    # ── 期限 ──

    def test_long_term_template_refuses_a_date(self):
        """`max_days=0` = 长期占用。填了到期日要当场说清楚，别默默忽略 ——
        忽略的话申请人以为自己申的是「用到某天」，实际拿到的是长期。"""
        with self.assertRaises(FlowError) as caught:
            self.submit(payload={"until": self.day(10)})
        self.assertIn("长期", str(caught.exception))

    def test_dated_template_requires_a_date(self):
        with self.assertRaises(FlowError) as caught:
            self.submit(template=DATED)
        self.assertIn("哪天", str(caught.exception))

    def test_dated_template_takes_a_valid_date(self):
        ticket = self.submit(template=DATED, payload={"until": self.day(10)})
        self.assertEqual(ticket["payload"], {"days": 10})
        self.assertIn(self.day(10), ticket["summary"])

    def test_dated_template_refuses_out_of_range_dates(self):
        for offset, hint in ((31, "最长"), (90, "最长"), (0, "晚于今天"), (-1, "晚于今天")):
            with self.subTest(offset=offset):
                with self.assertRaises(FlowError) as caught:
                    self.submit(template=DATED, payload={"until": self.day(offset)})
                self.assertIn(hint, str(caught.exception))

    def test_dated_template_refuses_a_malformed_date(self):
        for bad in ("2026/12/31", "明天", "20261231", "2026-13-01", ""):
            with self.subTest(bad), self.assertRaises(FlowError):
                self.submit(template=DATED, payload={"until": bad})

    # ── 到期时间 ──

    def test_long_term_ticket_never_expires(self):
        """长期单不写 `expires_at_ts`，所以**不会被到期扫描收走** ——
        它本来就没有「到期」这回事，被收走的表现是「用着用着突然进不去了」。"""
        done = self.finish(self.submit())
        self.assertEqual(done["status"], t.DONE)
        self.assertNotIn("expires_at_ts", done)
        self.assertNotIn("expires_at", done)
        self.assertEqual(self.h.flows._expires_at(done, self.h.now[0]), 0.0)

    def test_expiry_scan_leaves_service_tickets_alone(self):
        done = self.finish(self.submit())
        self.h.now[0] += 3650 * 86400  # 十年后再扫一次
        self.assertEqual(self.h.flows.revoke_expired(), [])
        self.assertEqual(self.h.store.get(done["id"])["status"], t.DONE)
        self.assertEqual(self.h.executor.actions, [])

    def test_dated_ticket_expires_by_days(self):
        ticket = self.submit(template=DATED, payload={"until": self.day(10)})
        done = self.finish(ticket)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(done["expires_at_ts"], self.h.now[0] + 10 * 86400)

    def test_expired_service_ticket_is_pushed_to_revoked_without_touching_the_cloud(self):
        """到期的限期单由定时任务推到 REVOKED，**一行云都不调**。

        为什么必须推状态而不是就地不管：网关判的是「有没有一张 done 且没过期的单」，
        停在 DONE 的单子对判定层来说仍然是「有权限」—— 靠 `expires_at_ts` 也能挡住，
        但台账上那张单会一直显示「已完成」，和事实不符。
        """
        done = self.finish(self.submit(template=DATED, payload={"until": self.day(1)}))
        self.h.now[0] += 2 * 86400
        self.assertEqual(len(self.h.flows.revoke_expired()), 1)
        after = self.h.store.get(done["id"])
        self.assertEqual(after["status"], t.REVOKED)
        self.assertEqual(self.h.executor.actions, [], "服务访问没有云上资源要收")
        self.assertFalse(self.decide().allowed)

    def test_reclaiming_an_expired_service_ticket_is_idempotent(self):
        """定时任务每轮都会跑。第二轮不能再报一次、更不能抛 ——
        `_needs_reclaim` 只认 DONE，已经收回的单子直接跳过。"""
        self.finish(self.submit(template=DATED, payload={"until": self.day(1)}))
        self.h.now[0] += 2 * 86400
        self.assertEqual(len(self.h.flows.revoke_expired()), 1)
        self.assertEqual(self.h.flows.revoke_expired(), [])

    # ── 和网关那道门对得上 ──

    def decide(self, *, now=None):
        """拿真实落盘的单子去问判定层 —— 两边对字段名的理解必须一致。"""
        return sa.allowed(
            union_id=LI.union_id,
            service=SERVICE,
            tickets=self.h.store.all(),
            now=self.h.now[0] if now is None else now,
        )

    def test_a_finished_long_term_ticket_opens_the_gate(self):
        """提交 → 审批 → 开通 → 网关问「能不能进」= 能。

        这条把三层串起来：模板里的 `service`、单子的 `kind/status/applicant.union_id`、
        判定层比的那几个字段，任何一处改名都会在这里红，而线上表现只是「批了却进不去」。
        """
        self.assertFalse(self.decide().allowed)  # 还没提交
        ticket = self.submit()
        self.assertFalse(self.decide().allowed, "还没批就不该放行")
        self.finish(ticket)
        got = self.decide()
        self.assertTrue(got.allowed)
        self.assertEqual(got.ticket_id, ticket["id"])
        self.assertEqual(got.expires_at, 0.0)
        self.assertTrue(self.decide(now=time.time() + 3650 * 86400).allowed, "长期单不该过期")

    def test_a_dated_ticket_closes_the_gate_when_it_expires(self):
        self.finish(self.submit(template=DATED, payload={"until": self.day(3)}))
        self.assertTrue(self.decide().allowed)
        self.assertFalse(self.decide(now=self.h.now[0] + 3 * 86400 + 1).allowed)

    def test_admin_revoke_closes_the_gate(self):
        """管理员点「收回」→ 单子进 REVOKED → 网关下一次问就答不行。

        这个按钮的唯一场景是「现在就要挡住」，所以它必须把单子推出 DONE：
        只把单子关掉（CLOSED）或就地留在 DONE 的话，判定层照样认它是一张有效授权。
        """
        ticket = self.finish(self.submit())
        self.assertTrue(self.decide().allowed)
        after = self.h.flows.revoke_now(ticket["id"], actor="on_admin")
        self.assertEqual(after["status"], t.REVOKED)
        self.assertFalse(self.decide().allowed)
        self.assertEqual(self.decide().reason, sa.NO_GRANT)
        self.assertEqual(self.h.executor.actions, [], "收回服务访问不该碰云")

    def test_another_service_key_does_not_open_this_gate(self):
        """模板里的 `service` 就是网关比的那个键 —— 写错一个字母，批了也进不去。"""
        self.finish(self.submit())
        other = sa.allowed(
            union_id=LI.union_id,
            service="tensorboard",
            tickets=self.h.store.all(),
            now=self.h.now[0],
        )
        self.assertFalse(other.allowed)


class ServiceApprovalDefinitionTests(unittest.TestCase):
    """服务访问走**自己那条**飞书审批定义，而不是所有类型共用的老那条。

    为什么值得锁：一条定义的表单要伺候所有申请类型，「申请类型」那一栏在单选里没有
    对应选项时是**静默留空**的 —— 审批人看到一张类型不明的单子，而他正是那道门。

    这里锁三件事：
      · 提交时把「用的是哪条定义」记进单子（`approval.definition`）；
      · **回写审批状态时不能把它抹掉**（`TicketStore.update(fields=)` 是浅覆盖，
        直接写 `{instance_code, status}` 会让 definition 消失，而核对正是在这之后
        才去读它 —— 发起用 A、核对用 B，在途的单子会全部被拒）；
      · 名字配不出来时**直接拒发**，不静默回落到老那条。
    """

    def setUp(self):
        self.h = Harness(_templates())
        # Harness 默认只有老那条定义（`_approvals=None` → 忽略名字）。这里把「按名字取」
        # 接上，模拟线上 approval.json 里多了一条 `definitions.service`
        self.svc_feishu = FakeFeishu()
        self.svc = FeishuApproval(
            ApprovalConfig(
                approval_code="APPROVAL-SERVICE",
                widgets=dict(CONFIG.widgets),
                comment_open_id=CONFIG.comment_open_id,
            ),
            lambda: "tenant-token",
            transport=self.svc_feishu,
        )
        self.table = {"": self.h.approval, DEFINITION: self.svc}
        self.h.flows._approvals = lambda name: self.table.get(str(name or ""))

    def submit(self, template=LONG):
        return self.h.submit(applicant=LI, template=template, payload={})

    def set_status(self, ticket, status):
        """在**服务那条定义**的假飞书里改状态（老那条的实例表里根本没有这张单）。"""
        self.svc_feishu.instances[ticket["approval"]["instance_code"]]["status"] = status

    def test_submit_goes_through_the_service_definition(self):
        ticket = self.submit()
        self.assertEqual(ticket["approval"]["definition"], DEFINITION)
        self.assertIn(ticket["approval"]["instance_code"], self.svc_feishu.instances)
        self.assertEqual(self.h.feishu.instances, {}, "不该发到老那条定义上")
        inst = self.svc_feishu.instances[ticket["approval"]["instance_code"]]
        self.assertEqual(inst["approval_code"], "APPROVAL-SERVICE")

    def test_approving_keeps_the_definition_on_the_ticket(self):
        """**回归**：`_approval_field` 要把 definition 带上。丢了的表现是下一次核对
        按老那条定义去拉实例 —— 单子永远卡在「审批中」，而两边都不报错。"""
        ticket = self.submit()
        self.set_status(ticket, "APPROVED")
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["approval"]["definition"], DEFINITION)
        self.assertEqual(done["approval"]["instance_code"], ticket["approval"]["instance_code"])
        self.assertEqual(done["status"], t.DONE)

    def test_rejecting_keeps_the_definition_too(self):
        for status, expect in (("REJECTED", t.REJECTED), ("CANCELED", t.WITHDRAWN)):
            with self.subTest(status):
                self.setUp()
                ticket = self.submit()
                self.set_status(ticket, status)
                after = self.h.flows.sync(ticket["id"], force=True)
                self.assertEqual(after["status"], expect)
                self.assertEqual(after["approval"]["definition"], DEFINITION)

    def test_an_unconfigured_definition_refuses_to_submit(self):
        """模板指向的定义配不出来 → 直接拒，**不回落到老那条**。

        回落的表现是：模板写着走新定义、单子却发进了老定义的表单（类型单选里没有这个
        选项 → 静默留空），页面和日志里一切正常 —— 那正是多定义要消灭的场景。
        """
        self.table.pop(DEFINITION)
        before = len(self.h.store.all())
        with self.assertRaises(FlowError) as caught:
            self.submit()
        self.assertIn(DEFINITION, str(caught.exception))
        self.assertEqual(len(self.h.store.all()), before, "拒发时不该留下半张单子")
        self.assertEqual(self.svc_feishu.instances, {})
        self.assertEqual(self.h.feishu.instances, {})

    def test_the_ticket_keeps_its_definition_even_if_the_template_moves(self):
        """核对按**单子记的那条**走，不按当前配置：模板改指到别条定义之后，
        在途的单子照样核对得过。"""
        ticket = self.submit()
        self.h.templates["templates"] = [
            {**x, "approval": ""} if x["id"] == LONG else x for x in self.h.templates["templates"]
        ]
        self.set_status(ticket, "APPROVED")
        done = self.h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(done["approval"]["definition"], DEFINITION)

    def test_the_gate_opens_after_the_service_definition_approves(self):
        """整条链的终点还是那一问：网关问「能不能进」= 能。"""
        ticket = self.submit()
        self.set_status(ticket, "APPROVED")
        self.h.flows.sync(ticket["id"], force=True)
        got = sa.allowed(
            union_id=LI.union_id,
            service=SERVICE,
            tickets=self.h.store.all(),
            now=self.h.now[0],
        )
        self.assertTrue(got.allowed)
        self.assertEqual(got.ticket_id, ticket["id"])


class BackendApprovalTableTests(unittest.TestCase):
    """`Backend.approvals(name)`：按名字取审批定义。

    **名字不认识时返回 None，不回落到老那条。** 回落的表现是：模板写着走新定义、
    单子却发进了老定义的表单（「申请类型」单选里没有这个选项 → 静默留空），
    而页面和日志里一切正常 —— 那正是多定义这套东西要消灭的场景。
    """

    OLD = "301E99EB-A4BC-4F08-AFAF-46906A006C08"
    SVC = "9A1B2C3D-0000-4F08-AFAF-46906A006C08"

    def backend(self, data, *, token=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "approval.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return Backend(
            approval_path=str(path),
            platforms={},
            feishu_token=(lambda: "tenant-token") if token else None,
        )

    def config(self, code):
        return {
            "approval_code": code,
            "widgets": {"ticket_id": "w1", "kind": "w2", "summary": "w3", "reason": "w4"},
        }

    def full(self):
        return dict(self.config(self.OLD), definitions={DEFINITION: self.config(self.SVC)})

    def test_each_name_gets_its_own_definition(self):
        backend = self.backend(self.full())
        self.assertEqual(backend.approvals("").config.approval_code, self.OLD)
        self.assertEqual(backend.approvals(DEFINITION).config.approval_code, self.SVC)

    def test_an_unknown_name_is_none_not_the_old_one(self):
        backend = self.backend(self.full())
        for name in ("nope", DEFINITION.upper(), f" {DEFINITION}", "service2"):
            with self.subTest(name):
                self.assertIsNone(backend.approvals(name))

    def test_a_name_that_is_not_configured_yet_is_none(self):
        """只有老那条时，问服务访问那条要得到 None —— 不能把单子发进老定义。"""
        backend = self.backend(self.config(self.OLD))
        self.assertIsNotNone(backend.approvals(""))
        self.assertIsNone(backend.approvals(DEFINITION))

    def test_without_feishu_credentials_there_is_no_definition_at_all(self):
        backend = self.backend(self.full(), token=False)
        self.assertIsNone(backend.approvals(""))
        self.assertIsNone(backend.approvals(DEFINITION))
