"""管理员纳管存量用户：绕过飞书审批直接开通（`Flows.grandfather_service` + CLI）。

这条路径是什么
──────────────
「发权限」原本只有一个入口：飞书审批通过。现在多了第二个 —— 管理员对**已经在用**
某个自建服务的人直接开通。背景是 MLflow 这类服务「先有、门后建」：已经在用的那批人
不该为「继续用」重走一遍审批，真正需要审批的是**新开**。

正因为它是第二个入口，它要被测得比一般功能狠。这个文件锁四件事：

**① 台账必须说实话。** 纳管出来的单没有 `approval`、有 `granted`，事件里没有任何
`approval_*`，状态 `SUBMITTING → DONE` 中间不经过「待审批」「已通过」。最要紧的是
`AuditorCanTellTests`：半年后审计这张台账的人只想知道一件事 —— 哪些是批下来的、
哪些是管理员加的。借 PENDING/APPROVED 的壳会让台账说假话，那两个状态的意思是
「在等审批」「审批通过了」，而纳管根本没走过审批。

**② 除此之外它和批下来的单必须是同一个物种。** 放行（`service_access.allowed`）、
持有（`holdings`）、离职回收（`offboard`）、撤销（`revoke_now`）、到期回收
（`revoke_expired`）任何一处把它当成另一个物种，都是 bug —— 授权只有一份真相。

**③ 这条路不能被拿来发别的权限。** 四道门逐个验：模板存在 / `kind == service` /
**模板必须是长期的**（限期模板纳管出来的单恒为长期 → 静默抹掉上限）/ 理由 5–500 字 /
`union_id` 不许带空白（带空白的行判定侧当脏数据，会造出「台账写着有、网关说没有」的幽灵单）。

**④ 幂等、重复、以及两处按现状锁住的已知缺口**（纳管不看离职记录 / 查重与建单跨两把锁）。
用例里写的是**观察到的行为**，docstring 里写明是缺口；该不该修由 auditor 判。

状态机那条边（`SUBMITTING → DONE`）是按 kind 额外放开的
（`tickets.EXTRA_TRANSITIONS_BY_KIND`），`TransitionEdgeTests` 用一张全矩阵把
「只对 service 开、且只加不减」钉死。

夹具沿用 `test_delivery_access_requests` 的 Harness / `test_delivery_service_request`
的服务模板。离线，数据虚构，不碰网络、不碰云。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import datetime
import inspect
import io
import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from delivery import catalog as catalog_mod
from delivery import cli_requests, offboard
from delivery import datatypes as datatypes_mod
from delivery import notify as notify_mod
from delivery import people as people_mod
from delivery import service_access as sa
from delivery import tickets as t
from delivery.approval import Applicant
from delivery.cli import build_parser
from delivery.errors import DeliveryError
from delivery.flows import _EXEC_FIELDS, FlowError, Flows, _effective, _snapshot
from delivery.server import Backend

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import ACC, LI, NEW, Harness
from .test_delivery_datatypes import TABLE as TYPES_TABLE
from .test_delivery_service_request import DATED, LONG, SERVICE, _templates

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

DAY = 86400
#: 同一个服务的**第二条长期模板**：锁「幂等是按服务算的，不是按模板算的」
ALT = "svc-mlflow-alt"
#: 第二个服务
TB = "svc-tb"
TB_SERVICE = "tensorboard"
#: 生产上真的要用的那句理由（两个人，MLflow）
REAL_REASON = "存量：认领时已在用 MLflow"
ADMIN = "admin"

#: 除了 service 之外的每一类模板，**八类一个不缺**（`OnlyServiceTests` 会断言这一点）。
#: 这些模板本身要能通过 `catalog.parse` —— 拿一个解析不了的模板去试，
#: 撞的是「没有这个申请模板」，证不了「kind 不对所以拒」
EXTRA_TEMPLATES = [
    {
        "id": ALT,
        "kind": "service",
        "platform": "internal",
        "title": "MLflow 访问（另一条长期模板）",
        "service": SERVICE,
        "max_days": 0,
    },
    {
        "id": TB,
        "kind": "service",
        "platform": "internal",
        "title": "TensorBoard 访问",
        "service": TB_SERVICE,
        "max_days": 0,
    },
    {
        "id": "ecs-box",
        "kind": "resource",
        "platform": "aliyun",
        "account": ACC,
        "title": "ECS 开发机",
        "max_days": 90,
    },
    {
        "id": "oss-dir",
        "kind": "storage",
        "platform": "aliyun",
        "account": ACC,
        "title": "新建数据目录",
        "buckets": [{"name": "wuji-raw", "region": "cn-hangzhou"}],
    },
    {
        "id": "oss-move",
        "kind": "transfer",
        "platform": "aliyun",
        "account": ACC,
        "title": "数据迁移",
        "buckets": [{"name": "wuji-raw", "region": "cn-hangzhou"}],
    },
    {
        "id": "data-type-new",
        "kind": "datatype",
        "platform": "aliyun",
        "account": ACC,
        "title": "新增数据类型",
        "buckets": [{"name": "wuji-raw", "region": "cn-hangzhou"}],
    },
]

SRC_DIR = Path(inspect.getfile(t)).resolve().parent


def all_templates() -> dict:
    """服务模板（长期 ×3 + 限期 ×1）+ 其余七类各一个。"""
    data = _templates()
    data["templates"].extend(copy.deepcopy(EXTRA_TEMPLATES))
    return data


def harness() -> Harness:
    """Harness + 数据类型词表（`storage` / `datatype` 模板解析要用）。"""
    h = Harness(all_templates())
    types = datatypes_mod.parse(TYPES_TABLE)
    h.flows._catalog = lambda: catalog_mod.parse(h.templates, None, types)  # noqa: SLF001
    return h


#: 默认的「离职记录里一个人都没有」。`offboarded` 是**必传**参数（见
#: `OffboardGateTests`），所以每个调用点都得给一个 —— 这个常量就是「没人离职」那一份
NOBODY_GONE = frozenset()


def gone_checker(*union_ids):
    """造一个 `offboarded(union_id)`：给谁就拒谁。"""
    gone = set(union_ids)
    return lambda uid: uid in gone


def grandfather(
    h,
    *,
    template=LONG,
    applicant=LI,
    reason=REAL_REASON,
    actor=ADMIN,
    email="",
    offboarded=None,
):
    return h.flows.grandfather_service(
        template_id=template,
        applicant=applicant,
        email=email,
        actor=actor,
        reason=reason,
        offboarded=offboarded if offboarded is not None else gone_checker(*NOBODY_GONE),
    )


def approved(h, *, template=LONG, applicant=LI, payload=None):
    """走完整审批那条路：提交 → 飞书通过 → 开通。对照组。"""
    ticket = h.submit(applicant=applicant, template=template, payload=payload or {})
    h.approve(ticket)
    return h.flows.sync(ticket["id"], force=True)


def went_through_approval(ticket: dict) -> bool:
    """**只看单子内容**，判断它走没走过审批 —— 审计这张台账的人唯一想知道的那件事。

    判据是两条互补的事实，任一为真就说明它走过审批：有 `approval`（飞书实例号），
    或者事件里有 `approval_*`。纳管那条路两样都没有，换来一条 `granted`。
    """
    if isinstance(ticket.get("approval"), dict) and ticket["approval"].get("instance_code"):
        return True
    return any(str(e.get("event") or "").startswith("approval") for e in ticket.get("events") or ())


def event_names(ticket: dict) -> list:
    return [str(e.get("event") or "") for e in ticket.get("events") or ()]


def day_after(h, offset: int) -> str:
    bj = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.fromtimestamp(h.now[0], bj).date()
    return (today + datetime.timedelta(days=offset)).isoformat()


# ── ① 台账说实话 ──────────────────────────────────────────────────────────


class LedgerTellsTheTruthTests(unittest.TestCase):
    """纳管单**长什么样**（字面）。这条路径存在的全部前提就是台账看得出它没走过审批。"""

    def setUp(self):
        self.h = harness()

    def test_it_opens_the_service_and_returns_the_ticket(self):
        """前提：这条路真的开通了（下面每条才有意义）。"""
        got = grandfather(self.h)
        self.assertEqual(got["status"], t.DONE)
        self.assertEqual(got["kind"], catalog_mod.KIND_SERVICE)
        self.assertEqual(got["template"]["service"], SERVICE)
        self.assertEqual(self.h.store.get(got["id"])["status"], t.DONE)

    def test_there_is_no_approval_field(self):
        """没有实例号可写，**写个空壳更糟** —— 空壳会让「有 approval」不再等于
        「走过审批」，而那正是审计时唯一能用的判据。"""
        got = grandfather(self.h)
        self.assertNotIn("approval", got)
        self.assertNotIn("approval", self.h.store.get(got["id"]))

    def test_the_ledger_row_is_exactly_this(self):
        """台账**字面**断言：三个键齐全、`by` 就是 actor、理由逐字、摘要说清楚。

        只断言「有 granted」「truthy」是不够的 —— 半年后审计的人读的是这几个字段的
        具体内容，写了个空壳或把理由截断了，断言照样绿而台账没用。
        """
        got = grandfather(self.h, actor="on_admin", reason=REAL_REASON)
        self.assertEqual(sorted(got["granted"]), ["at", "by", "reason"])
        self.assertEqual(got["granted"]["by"], "on_admin")
        self.assertEqual(got["granted"]["reason"], REAL_REASON)
        self.assertEqual(got["granted"]["at"], t.now_iso(lambda: self.h.now[0]))
        self.assertEqual(got["reason"], REAL_REASON)
        self.assertEqual(got["summary"], "管理员纳管：MLflow 访问（存量用户，未走审批）")
        self.assertIn("纳管", got["summary"])
        self.assertIn("未走审批", got["summary"])
        self.assertEqual(got["result"], f"已开通 {SERVICE} 的访问权限（纳管）")

    def test_no_event_mentions_approval(self):
        """两条事件，一条都不姓 approval；事件序列本身也是字面锁。"""
        got = grandfather(self.h, reason="存量在用中")
        self.assertEqual(event_names(got), ["created", "admin_granted"])
        self.assertEqual([e for e in event_names(got) if "approval" in e], [])
        notes = [e["note"] for e in got["events"]]
        self.assertEqual(
            notes, ["管理员纳管（不是本人提交的）", "管理员纳管，未经审批：存量在用中"]
        )
        self.assertEqual({e["actor"] for e in got["events"]}, {ADMIN})

    def test_status_never_passes_through_pending_or_approved(self):
        """**每一次落盘**都查一遍状态：只出现过「提交中」和「已完成」。

        看最终状态是不够的 —— 借 PENDING/APPROVED 的壳走一圈再回到 DONE，
        最终状态一模一样，而台账里会留下两条假事实。
        """
        seen = []
        real = t.TicketStore._write  # noqa: SLF001

        def spy(store, data):
            seen.append([(x.get("id"), x.get("status")) for x in data["tickets"]])
            return real(store, data)

        with mock.patch.object(t.TicketStore, "_write", spy):
            got = grandfather(self.h)
        statuses = [status for snapshot in seen for _id, status in snapshot]
        self.assertEqual(statuses, [t.SUBMITTING, t.DONE], seen)
        self.assertNotIn(t.PENDING, statuses)
        self.assertNotIn(t.APPROVED, statuses)
        self.assertEqual(self.h.store.get(got["id"])["status"], t.DONE)

    def test_the_applicant_is_the_person_not_the_admin(self):
        """单子挂在**被纳管的人**名下（判定侧按 `applicant.union_id` 找），
        管理员只出现在 `granted.by` 和事件的 actor 上。"""
        got = grandfather(self.h, applicant=NEW, actor="on_admin", email="new@wuji.tech")
        self.assertEqual(got["applicant"]["union_id"], NEW.union_id)
        self.assertEqual(got["applicant"]["email"], "new@wuji.tech")
        self.assertEqual(got["granted"]["by"], "on_admin")


class AuditorCanTellTests(unittest.TestCase):
    """**区分性**：给一张纳管单和一张批下来的单，只看单子内容能不能分辨。

    这是审计这张台账的人唯一想知道的事。分辨不出来 = 这条路径不该存在。
    """

    def setUp(self):
        self.h = harness()
        self.granted = grandfather(self.h, template=TB, reason=REAL_REASON)
        self.approved = approved(self.h, template=LONG)

    def test_the_predicate_separates_them(self):
        self.assertFalse(went_through_approval(self.granted))
        self.assertTrue(went_through_approval(self.approved))

    def test_they_are_otherwise_the_same_shape(self):
        """上一条不能只是「两张单子哪里都不一样」—— 它们在**除审批痕迹之外**
        的每一处都该是一样的，否则那条判据可能只是碰巧命中了别的差异。"""
        for key in ("kind", "status"):
            self.assertEqual(self.granted[key], self.approved[key], key)
        self.assertEqual(
            self.granted["applicant"]["union_id"], self.approved["applicant"]["union_id"]
        )
        held = sa.holdings(self.h.store.all(), self.h.now[0])[LI.union_id]
        self.assertEqual(
            sorted(held),
            sorted([(TB_SERVICE, self.granted["id"], 0.0), (SERVICE, self.approved["id"], 0.0)]),
        )

    def test_the_only_extra_fields_are_the_audit_trail(self):
        """两张单子的字段差集**恰好**是审批痕迹那几样，不多不少。

          · 纳管多 `granted`（谁加的、为什么）和 `expires_at_ts`（显式 0 = 不限期）；
          · 批下来的多 `approval`（飞书实例号）。

        `done_at_ts` **两边都有**（纳管也写），所以它不在差集里 —— 那正是
        `SameSpeciesTests.test_both_kinds_of_ticket_are_reminded_the_same_way` 要的前提。

        钉死这个差集是为了挡住「以后顺手往纳管单上多写/少写一个字段」——
        多出来的那个字段会让两种单子在别处（提醒、页面、回收）开始分叉。
        """
        only_granted = set(self.granted) - set(self.approved)
        only_approved = set(self.approved) - set(self.granted)
        self.assertEqual(only_granted, {"granted", "expires_at_ts"})
        self.assertEqual(only_approved, {"approval"})
        self.assertIn("done_at_ts", self.granted)
        self.assertIn("done_at_ts", self.approved)

    def test_an_auditor_can_filter_the_whole_ledger(self):
        """在整份台账上过一遍：挑出来的「没走审批」的单子正好是纳管那几张。"""
        second = grandfather(self.h, applicant=NEW, template=LONG, reason="存量第二个人")
        rows = self.h.store.all()
        self.assertEqual(
            {x["id"] for x in rows if not went_through_approval(x)},
            {self.granted["id"], second["id"]},
        )
        self.assertEqual(
            {x["id"] for x in rows if "granted" in x}, {self.granted["id"], second["id"]}
        )


# ── ② 判定侧和批下来的单一模一样 ──────────────────────────────────────────


class SameSpeciesTests(unittest.TestCase):
    """纳管单在判定/回收侧必须和批下来的单**不可区分**。任何一处特殊对待都是 bug。"""

    def setUp(self):
        self.h = harness()

    def decide(self, service=SERVICE, *, now=None, applicant=LI, **kw):
        return sa.allowed(
            union_id=applicant.union_id,
            service=service,
            tickets=self.h.store.all(),
            now=self.h.now[0] if now is None else now,
            **kw,
        )

    def test_the_gate_opens(self):
        self.assertFalse(self.decide().allowed)
        got = grandfather(self.h)
        decision = self.decide()
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.ticket_id, got["id"])
        self.assertEqual(decision.expires_at, 0.0)
        self.assertTrue(self.decide(now=self.h.now[0] + 3650 * DAY).allowed)

    def test_holdings_and_allowed_agree_on_a_mixed_ledger(self):
        """混一份台账（纳管 + 批下来 + 撤销过的），两侧给的答案必须完全一致。

        背离的表现是「页面/离职回收说他有、网关说他没有」，没有任何地方会报错。
        """
        grandfather(self.h, template=LONG)
        approved(self.h, template=TB)
        gone = grandfather(self.h, applicant=NEW, template=TB, reason="存量后来又收回")
        self.h.flows.revoke_now(gone["id"], actor=ADMIN)
        rows = self.h.store.all()
        held = {
            (uid, svc) for uid, xs in sa.holdings(rows, self.h.now[0]).items() for svc, _, _ in xs
        }
        opened = {
            (uid, svc)
            for uid in (LI.union_id, NEW.union_id)
            for svc in (SERVICE, TB_SERVICE)
            if sa.allowed(union_id=uid, service=svc, tickets=rows, now=self.h.now[0]).allowed
        }
        self.assertEqual(held, opened)
        self.assertEqual(held, {(LI.union_id, SERVICE), (LI.union_id, TB_SERVICE)})

    def test_the_gate_gives_the_same_answer_for_both_kinds_of_ticket(self):
        """同一个人、同一个服务，一张纳管单和一张批下来的单**各自单独**放进台账，
        判定结果除了单号之外必须逐字相同。"""
        one = harness()
        got = grandfather(one)
        first = sa.allowed(
            union_id=LI.union_id, service=SERVICE, tickets=one.store.all(), now=one.now[0]
        )
        two = harness()
        ok = approved(two)
        second = sa.allowed(
            union_id=LI.union_id, service=SERVICE, tickets=two.store.all(), now=two.now[0]
        )
        self.assertEqual(
            (first.allowed, first.reason, first.expires_at),
            (second.allowed, second.reason, second.expires_at),
        )
        self.assertEqual((first.ticket_id, second.ticket_id), (got["id"], ok["id"]))

    def test_admin_revoke_closes_the_gate(self):
        got = grandfather(self.h)
        after = self.h.flows.revoke_now(got["id"], actor="on_admin")
        self.assertEqual(after["status"], t.REVOKED)
        self.assertFalse(self.decide().allowed)
        self.assertEqual(self.decide().reason, sa.NO_GRANT)
        self.assertEqual(self.h.executor.actions, [], "服务访问没有云上资源要收")
        self.assertIn("service_revoked", event_names(self.h.store.get(got["id"])))

    def test_revoking_twice_is_refused_the_same_way(self):
        got = grandfather(self.h)
        self.h.flows.revoke_now(got["id"], actor=ADMIN)
        with self.assertRaises(FlowError):
            self.h.flows.revoke_now(got["id"], actor=ADMIN)

    def test_the_expiry_scan_leaves_a_long_term_ticket_alone(self):
        """不限期的纳管单十年后也不该被收走 —— 和批下来的长期单同样处理。"""
        grandfather(self.h)
        approved(self.h, template=TB)
        self.h.now[0] += 3650 * DAY
        self.assertEqual(self.h.flows.revoke_expired(), [])
        self.assertTrue(self.decide().allowed)
        self.assertTrue(self.decide(TB_SERVICE).allowed)

    def test_a_grandfathered_ticket_is_always_long_term(self):
        """纳管单写的是 `expires_at_ts = 0`（= 不限期）。

        这是「限期模板不给纳管」那道门的另一面：纳管的 `payload` 是空的，
        `_expires_at` 对 service 读 `payload["days"]` → 恒为 0。所以**能纳管的模板
        必须本来就是长期的**，否则模板上的期限会被静默抹掉（见 `OnlyServiceTests`）。
        """
        got = grandfather(self.h)
        self.assertEqual(got["expires_at_ts"], 0.0)
        self.assertEqual(self.h.flows._expires_at(got, self.h.now[0]), 0.0)  # noqa: SLF001

    def test_with_the_same_expiry_both_are_reclaimed_the_same_way(self):
        """给纳管单装上到期时间（今天这条路不会自己装）后，到期回收对它和对
        批下来的单说的是**同一句话**、做的是同一件事。"""
        got = grandfather(self.h, template=TB)
        self.h.store.update(
            got["id"],
            actor="system",
            expect=[t.DONE],
            event="probe",
            fields={"expires_at_ts": self.h.now[0] + 3 * DAY},
        )
        ok = approved(self.h, template=DATED, payload={"until": day_after(self.h, 3)})
        self.h.now[0] += 4 * DAY
        lines = sorted(self.h.flows.revoke_expired())
        self.assertEqual(
            lines,
            sorted([f"{got['id']}：服务访问已到期收回", f"{ok['id']}：服务访问已到期收回"]),
        )
        for tid in (got["id"], ok["id"]):
            self.assertEqual(self.h.store.get(tid)["status"], t.REVOKED, tid)
        self.assertEqual(self.h.executor.actions, [])
        self.assertFalse(self.decide().allowed)
        self.assertFalse(self.decide(TB_SERVICE).allowed)

    def test_a_grandfathered_ticket_records_when_it_was_opened(self):
        """纳管**要写** `done_at_ts`（开通时刻），和批下来的单一样。

        今天用不上它（纳管单恒不限期，提醒/回收都不会看），但缺了它就埋着一处分叉：
        `_remind_tier` 的「开通时就只剩这么几天，不再重复提醒」判据是
        `expires_at_ts - done_at_ts` —— 缺 `done_at_ts` 时这条判据对纳管单永远不成立。
        """
        got = grandfather(self.h)
        self.assertEqual(got["done_at_ts"], self.h.now[0])
        self.assertIn("done_at_ts", approved(self.h, template=TB))

    def test_both_kinds_of_ticket_are_reminded_the_same_way(self):
        """**同一个窗口，同一个结论**：一张纳管单和一张批下来的单都是「开通时就只剩
        3 天」，两边都**不**再发提醒（开通消息里已经写了到期时间）。

        这是 `done_at_ts` 那条的行为面。缺 `done_at_ts` 的那版里，纳管那张会多发一张
        「还有 3 天到期」的卡，而批下来的不会 —— 两种单子在提醒上分叉。
        今天这条走的是人工装上到期时间的路（纳管自己发不出限期单），
        它锁的是「以后给纳管加期限时，提醒不会分叉」。
        """
        one = harness()
        one.flows._notify = notify_mod.RecordingNotifier()  # noqa: SLF001
        got = grandfather(one, template=TB)
        one.store.update(
            got["id"],
            actor="system",
            expect=[t.DONE],
            event="probe",
            fields={"expires_at_ts": one.now[0] + 3 * DAY},
        )
        self.assertEqual(one.flows.remind_expiring(tiers=(7, 1)), [])

        two = harness()
        two.flows._notify = notify_mod.RecordingNotifier()  # noqa: SLF001
        approved(two, template=DATED, payload={"until": day_after(two, 3)})
        self.assertEqual(two.flows.remind_expiring(tiers=(7, 1)), [])

    def test_a_long_running_grant_is_still_reminded_like_an_approved_one(self):
        """上一条不能只是「两边都不提醒」—— 窗口够长时**两边都该提醒**，
        否则那条用例可能只是证明了「提醒这套东西整个没跑起来」。"""
        one = harness()
        one.flows._notify = notify_mod.RecordingNotifier()  # noqa: SLF001
        got = grandfather(one, template=TB)
        one.store.update(
            got["id"],
            actor="system",
            expect=[t.DONE],
            event="probe",
            fields={"expires_at_ts": one.now[0] + 30 * DAY},
        )
        one.now[0] += 24 * DAY  # 还剩 6 天 → 落进 7 天那一档
        self.assertEqual(len(one.flows.remind_expiring(tiers=(7, 1))), 1)

        two = harness()
        two.flows._notify = notify_mod.RecordingNotifier()  # noqa: SLF001
        ok = approved(two, template=DATED, payload={"until": day_after(two, 30)})
        two.now[0] = float(two.store.get(ok["id"])["expires_at_ts"]) - 6 * DAY
        self.assertEqual(len(two.flows.remind_expiring(tiers=(7, 1))), 1)

    def test_a_wired_notifier_gets_the_same_done_card(self):
        self.h.rec = notify_mod.RecordingNotifier()
        self.h.flows._notify = self.h.rec  # noqa: SLF001
        got = grandfather(self.h)
        self.assertEqual(self.h.rec.events(), ["done"])
        self.assertEqual(self.h.rec.sent[0][1], got["id"])

    def test_a_notifier_that_blows_up_does_not_undo_the_grant(self):
        """通知失败只打日志：单子已经写进台账了，不能因为发不出卡片就回滚。"""

        def boom(event, ticket):
            raise RuntimeError("飞书挂了")

        self.h.flows._notify = boom  # noqa: SLF001
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = grandfather(self.h)
        self.assertEqual(self.h.store.get(got["id"])["status"], t.DONE)
        self.assertIn("飞书挂了", err.getvalue())
        self.assertTrue(self.decide().allowed)


class OffboardReclaimsItTests(unittest.TestCase):
    """离职回收收得掉纳管单 —— 端到端：CLI 发单 → 网关放行 → 离职 → 网关拒。

    这条链上「没有记录」和「没有东西要收」长得一模一样（见
    `test_delivery_service_offboard`），所以必须从「他现在确实进得去」验起。
    """

    def setUp(self):
        self.work = new_workdir(self)
        self.people_path = self.work / "identity" / "people.json"
        self.tickets_path = self.work / "identity" / "tickets.json"
        self.assertEqual(run_cli(self.work, union_ids=["on_li"], apply=True)[0], 0)
        self.record_path = offboard.path_beside(str(self.people_path))
        self.cloud_calls = []

    def gate(self):
        backend = Backend(
            people_path=str(self.people_path),
            tickets_path=str(self.tickets_path),
            platforms={"aliyun": "阿里云"},
        )
        return backend.service_access(union_id="on_li", service=SERVICE)

    def dispatch(self, platform, account):
        """和线上一样：internal 走 ServiceAccess，别的落到云 —— 落到云就是错的。"""
        if platform == offboard.SERVICE_PLATFORM:
            return offboard.ServiceAccess()
        self.cloud_calls.append((platform, account))
        raise AssertionError(f"服务回收不该调云执行体：{platform}/{account}")

    def person(self):
        return people_mod.load(str(self.people_path)).people[0]

    def holdings(self):
        rows = t.TicketStore(str(self.tickets_path)).all()
        return sa.holdings(rows, 1_800_000_000.0)

    def offboard_him(self):
        return offboard.auto_disable(
            self.record_path,
            [(self.person(), "飞书状态：已离职")],
            self.dispatch,
            holdings=self.holdings(),
        )

    def test_offboarding_blocks_a_grandfathered_ticket(self):
        self.assertTrue(self.gate().allowed, "前提：他现在确实进得去")
        self.assertEqual(list(self.holdings()), ["on_li"])
        report = self.offboard_him()
        self.assertEqual(report["skipped"], [])
        self.assertEqual(len(report["done"]), 1)
        self.assertEqual(self.cloud_calls, [], "internal 不该落到云执行体上")
        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, "on_li")
        record = offboard.load(self.record_path)[key]
        self.assertEqual(record["state"], offboard.DISABLED)
        self.assertEqual(record["union_id"], "on_li")
        decision = self.gate()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, sa.BLOCKED)

    def test_grandfathering_a_disabled_person_is_refused(self):
        """离职过的人再跑纳管 → **拒**，台账一个字不加。

        这一步本来就不会发新单（那张 DONE 还在 → 幂等跳过），但拒的是更前面那道门：
        `✗ … 在离职记录里，不纳管`。两者的区别在下一条用例上才看得出来。
        """
        self.offboard_him()
        self.assertFalse(self.gate().allowed)
        code, _out, err = run_cli(self.work, union_ids=["on_li"], apply=True)
        self.assertEqual(code, 1)
        self.assertIn("在离职记录里", err)
        self.assertEqual(len(t.TicketStore(str(self.tickets_path)).all()), 1)
        self.assertFalse(self.gate().allowed)
        self.assertEqual(self.gate().reason, sa.BLOCKED)

    def test_revoked_then_grandfathered_again_is_refused_too(self):
        """**回归 —— 那个「离职收回了、纳管又加回来」的循环**：单子被撤销之后
        （幂等判定不再认它）再纳管，**仍然拒**，因为离职记录还在。

        修之前这里会发出第二张 DONE 单。当时人确实进不去（判定侧那道离职门兜着），
        但代价是**台账失真**：上面写着一张没人审过的有效授权 —— 而那道门本身是
        可撤销的（离职记录被改成 `restored` 是正常操作，当初也可能就是误判），
        一撤销他立刻进得去、中间零审批。
        """
        self.offboard_him()
        store = t.TicketStore(str(self.tickets_path))
        first = store.all()[0]["id"]
        flows_for(self.work).revoke_now(first, actor=ADMIN)
        self.assertEqual(store.get(first)["status"], t.REVOKED)
        self.assertEqual(sa.holdings(store.all(), 1_800_000_000.0), {}, "前提：幂等不再拦他")

        code, out, err = run_cli(
            self.work, union_ids=["on_li"], apply=True, reason="收回之后又加回来"
        )
        self.assertEqual(code, 1)
        self.assertIn("在离职记录里", err)
        self.assertNotIn("✓", out)
        rows = store.all()
        self.assertEqual([x["status"] for x in rows], [t.REVOKED], "不该多出一张单")
        self.assertFalse(self.gate().allowed)

    def test_a_restored_person_can_be_grandfathered_again(self):
        """判据和 `service_access` 的 `gone` 一致：只有 `disabled`/`deleted` 拦。

        管理员判过「这人没走」（`restored`）之后就该能正常纳管 —— 否则这道门会变成
        「一旦被离职流程碰过就永远加不回来」，而误判是常事。
        """
        self.offboard_him()
        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, "on_li")
        with offboard._locked(self.record_path) as box:  # noqa: SLF001
            box["records"][key]["state"] = offboard.RESTORED
        store = t.TicketStore(str(self.tickets_path))
        flows_for(self.work).revoke_now(store.all()[0]["id"], actor=ADMIN)

        code, out, _err = run_cli(self.work, union_ids=["on_li"], apply=True, reason="判过他没走")
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        self.assertEqual([x["status"] for x in store.all()], [t.REVOKED, t.DONE])
        self.assertTrue(self.gate().allowed)


# ── ③ 这条路不能被拿来发别的权限 ──────────────────────────────────────────


class OffboardGateTests(unittest.TestCase):
    """`offboarded(union_id)` 这道门（方法级）。**它是必传参数，没有默认值。**

    为什么必须在这儿拦、而不是靠判定侧那道离职门：那道门**是可撤销的** ——
    离职记录被改成 `restored` 是正常操作、当初也可能就是误判。一旦撤销，台账上那张
    没人审过的单立刻生效。所以纳管本身就不该把人加回来。

    为什么不给默认值：可选依赖 + 忘传 = 静默失效，和 `Flows.approvals` 是同一个教训
    （那次漏传的表现是「面板一切正常、无人值守那条路每分钟一条假报错」）。

    **「哪几种离职状态算拦」在这一层测不到**（方法只是调调用方给的那个函数），
    所以那条判据锁在 CLI 那组：`CliTests.test_someone_in_the_offboard_records_is_refused`
    / `..._a_deleted_person_is_refused_...` / `..._suspect_and_restored_are_not_blocked`
    用的是真的 `identity/offboard.json`。
    """

    def setUp(self):
        self.h = harness()

    def test_someone_in_the_offboard_records_is_refused(self):
        with self.assertRaises(FlowError) as caught:
            grandfather(self.h, offboarded=gone_checker(LI.union_id))
        self.assertIn("离职", str(caught.exception))
        self.assertEqual(self.h.store.all(), [], "拒的时候一张单都不该落盘")

    def test_it_asks_about_this_person_not_someone_else(self):
        """传进去的是**这张单的 union_id**（拿错人问的话，这道门等于没有）。"""
        asked = []

        def spy(uid):
            asked.append(uid)
            return False

        grandfather(self.h, applicant=NEW, offboarded=spy)
        self.assertEqual(asked, [NEW.union_id])

    def test_someone_else_being_gone_does_not_block_this_one(self):
        self.assertIsNotNone(grandfather(self.h, offboarded=gone_checker(NEW.union_id)))

    def test_forgetting_the_argument_is_a_TypeError(self):
        """**没有默认值**。加回默认值的表现是：新调用方忘了传 → 这道门静默消失，
        而页面、日志、台账上都看不出任何异常。"""
        with self.assertRaises(TypeError):
            self.h.flows.grandfather_service(
                template_id=LONG, applicant=LI, actor=ADMIN, reason=REAL_REASON
            )
        self.assertEqual(self.h.store.all(), [])
        params = inspect.signature(self.h.flows.grandfather_service).parameters
        self.assertIs(params["offboarded"].default, inspect.Parameter.empty)

    def test_the_gate_is_checked_before_anything_is_written(self):
        """顺序：先问离职、再查重、最后才建单。反过来的话会先落一张单再撞门。"""
        order = []
        real = t.TicketStore.create

        def create(store, ticket, *, actor, note="提交申请"):
            order.append("create")
            return real(store, ticket, actor=actor, note=note)

        def gone(uid):
            order.append("offboarded")
            return True

        with mock.patch.object(t.TicketStore, "create", create), self.assertRaises(FlowError):
            grandfather(self.h, offboarded=gone)
        self.assertEqual(order, ["offboarded"])


class OnlyServiceTests(unittest.TestCase):
    """五道门：模板存在 / kind / 模板必须长期 / 理由 / union_id（离职那道见上一组）。

    每一条都同时断言**失败不留垃圾单**（`store.all() == []`）——
    半张单子留在「提交中」比直接拒更糟：它进不了任何流程，也没人会去收。
    """

    def setUp(self):
        self.h = harness()

    def test_every_other_kind_is_refused_one_by_one(self):
        """八类模板逐个试，除 `service` 外一律拒。"""
        catalog = self.h.flows._catalog()  # noqa: SLF001
        others = [x for x in catalog.templates if x.kind != catalog_mod.KIND_SERVICE]
        self.assertEqual(
            {x.kind for x in others},
            set(catalog_mod.KINDS) - {catalog_mod.KIND_SERVICE},
            "八类模板要一个不缺，否则这条用例是在自我安慰",
        )
        for tpl in others:
            with self.subTest(kind=tpl.kind, template=tpl.id):
                with self.assertRaises(FlowError) as caught:
                    grandfather(self.h, template=tpl.id)
                self.assertIn("内部服务", str(caught.exception))
                self.assertEqual(self.h.store.all(), [])

    def test_an_unknown_template_is_refused(self):
        for bad in ("nope", "", None, f"{LONG} ", LONG.upper(), "svc-mlflow/../oss-read"):
            with self.subTest(repr(bad)):
                with self.assertRaises(FlowError) as caught:
                    grandfather(self.h, template=bad)
                self.assertIn("没有这个申请模板", str(caught.exception))
        self.assertEqual(self.h.store.all(), [])

    def test_a_limited_template_is_refused(self):
        """**限期模板不给纳管。**

        纳管的 `payload` 是空的，而 `_expires_at` 对 service 读的是 `payload["days"]`
        → 纳管出来的单恒为「长期」，`revoke_expired` 永远扫不到它。对长期模板没问题；
        对限期模板等于**静默把模板上那个期限抹掉** —— 而那是这类授权唯一的时间限制。
        限期授权本来就该走申请。
        """
        with self.assertRaises(FlowError) as caught:
            grandfather(self.h, template=DATED)
        self.assertIn("30 天", str(caught.exception))
        self.assertIn("长期模板", str(caught.exception))
        self.assertEqual(self.h.store.all(), [])
        # 同一个服务的长期模板照常 —— 拒的是「限期」这件事，不是这个服务
        self.assertIsNotNone(grandfather(self.h, template=LONG))

    def test_the_limit_is_checked_before_the_reason(self):
        """门的顺序也锁一下：限期模板 + 短理由 → 说的是期限那件事。
        顺序反了的话，管理员会先去改理由、改完再撞一次墙。"""
        with self.assertRaises(FlowError) as caught:
            grandfather(self.h, template=DATED, reason="短")
        self.assertIn("期限", str(caught.exception))

    def test_a_too_short_or_too_long_reason_is_refused(self):
        """理由是这条路径留下的唯一交代：5–500 字。

        只校验「非空」是不够的 —— `--reason x` 一个字符就能过，而半年后那一个字符
        什么也说明不了。
        """
        for bad in ("", " ", "\t\n", None, "短", "四个字符", "x" * 501):
            with self.subTest(repr(bad if bad is None else bad[:8])):
                with self.assertRaises(FlowError) as caught:
                    grandfather(self.h, reason=bad)
                self.assertIn("纳管理由", str(caught.exception))
        self.assertEqual(self.h.store.all(), [])

    def test_the_reason_boundaries_are_inclusive(self):
        """边界两侧各一个：5 个字过、500 个字过（501 在上一条里被拒）。"""
        self.assertIsNotNone(grandfather(self.h, template=LONG, reason="五个字符了"))
        self.assertIsNotNone(grandfather(self.h, template=TB, reason="x" * 500))

    def test_the_reason_is_stripped_before_it_is_measured_and_stored(self):
        got = grandfather(self.h, reason=f"  {REAL_REASON}  ")
        self.assertEqual(got["granted"]["reason"], REAL_REASON)
        self.assertEqual(got["reason"], REAL_REASON)

    def test_a_union_id_with_whitespace_is_refused(self):
        """**幽灵单**：`service_access.active_grants` 刻意把带首尾空白的行当脏数据拒掉。

        放进去的话，台账上写着「已开通」、判定侧永远说「没有」，而且查重也永远不命中
        —— 每跑一次多一张，没有任何地方报错。所以在入口就拒。
        """
        for bad in (" on_li", "on_li ", "on_li\n", "\ton_li", "", "   ", None):
            with self.subTest(repr(bad)):
                with self.assertRaises(FlowError) as caught:
                    grandfather(self.h, applicant=Applicant(union_id=bad, name="李四"))
                self.assertIn("union_id", str(caught.exception))
        self.assertEqual(self.h.store.all(), [])

    def test_the_template_snapshot_is_frozen_at_grandfather_time(self):
        """模板在纳管**之后**被改了，单子上的快照还是纳管那一刻的那份。

        和审批单同一个语义（`_EXEC_FIELDS`）：批的是哪份模板是一条历史事实。
        读活模板的话，有人把 `service` 从 mlflow 改成别的，这张老单当场
        变成另一个服务的通行证 —— 而页面上一个字都不会变。
        """
        got = grandfather(self.h)
        catalog = self.h.flows._catalog()  # noqa: SLF001
        # 快照要和**从 JSON 里读回来的那份**逐字相等（`_snapshot` 里有元组，
        # 落盘一趟会变成列表）—— 否则每张单子都会被判成「模板被改」
        fresh = json.loads(json.dumps(_snapshot(catalog.get(LONG)), ensure_ascii=False))
        self.assertEqual(got["template"], fresh)

        for spec in self.h.templates["templates"]:
            if spec["id"] == LONG:
                spec["service"] = "grafana"
                spec["title"] = "改过的标题"
        after = self.h.store.get(got["id"])
        self.assertEqual(after["template"], got["template"])
        self.assertEqual(after["template"]["service"], SERVICE)
        changed = _snapshot(self.h.flows._catalog().get(LONG))  # noqa: SLF001
        self.assertNotEqual(
            _effective(after["template"], _EXEC_FIELDS), _effective(changed, _EXEC_FIELDS)
        )
        # 网关认的还是老服务名，改模板不会把这张单挪到另一个服务上
        rows = self.h.store.all()
        self.assertTrue(
            sa.allowed(
                union_id=LI.union_id, service=SERVICE, tickets=rows, now=self.h.now[0]
            ).allowed
        )
        self.assertFalse(
            sa.allowed(
                union_id=LI.union_id, service="grafana", tickets=rows, now=self.h.now[0]
            ).allowed
        )

    def test_the_snapshot_has_the_same_fields_as_an_approved_one(self):
        """快照的形状必须和审批那条路一致，否则 `_verify` / `regrant` 这类
        「拿快照和活模板比」的地方会对纳管单报「模板被修改」。"""
        got = grandfather(self.h, template=TB)
        ok = approved(self.h, template=LONG)
        self.assertEqual(set(got["template"]), set(ok["template"]))

    def test_it_is_not_reachable_from_the_http_surface(self):
        """**结构锁**：纳管只有 CLI 一个入口（管理员在面板机上手敲）。

        接上 HTTP 就等于把「绕过审批发权限」挂到网上，那需要另一套门禁 ——
        今天没有，所以这里先把「没有」钉住。

        按 AST 找真正的**调用点和定义**，不按文本 grep —— 注释里提一句它的名字
        （`tickets.py` 就提了）不该让这条用例红。
        """
        callers = set()
        for path in sorted(SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                named = (
                    isinstance(node, ast.Attribute) and node.attr == "grandfather_service"
                ) or (isinstance(node, ast.FunctionDef) and node.name == "grandfather_service")
                if named:
                    callers.add(path.name)
        self.assertEqual(sorted(callers), ["cli_requests.py", "flows.py"], sorted(callers))

    def test_the_method_does_not_check_the_roster_itself(self):
        """**已知缺口（Med-3），按现状锁**：名册校验只在 CLI 里，方法本身不看。

        今天只有一个调用方（CLI），它挡了。但「谁调这个方法谁记得挡」是一条靠默契的
        保证，而不是代码里的保证 —— 新增第二个调用方（面板按钮、定时任务）时，
        漏挡的表现是台账上多一张永远不生效的单（`in_roster` 那道门在判定侧）。
        """
        ghost = Applicant(union_id="on_ghost", name="查无此人")
        got = grandfather(self.h, applicant=ghost)
        self.assertIsNotNone(got, "方法本身不看名册 —— 这就是那个缺口")
        rows = self.h.store.all()
        self.assertTrue(
            sa.allowed(
                union_id="on_ghost", service=SERVICE, tickets=rows, now=self.h.now[0]
            ).allowed
        )
        blocked = sa.allowed(
            union_id="on_ghost",
            service=SERVICE,
            tickets=rows,
            now=self.h.now[0],
            in_roster=False,
        )
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, sa.NOT_IN_ROSTER)


# ── ④ 幂等与重复 ──────────────────────────────────────────────────────────


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.h = harness()

    def test_the_same_person_and_service_twice_does_not_pile_up_tickets(self):
        first = grandfather(self.h)
        self.assertIsNone(grandfather(self.h, reason="再跑一次试试"))
        self.assertEqual([x["id"] for x in self.h.store.all()], [first["id"]])

    def test_idempotency_is_by_service_not_by_template(self):
        """`svc-mlflow` 和 `svc-mlflow-alt` 是同一个服务的两条长期模板。
        已经有有效授权就不该再发一张 —— 判据是「他手上有没有这个**服务**」。"""
        grandfather(self.h, template=LONG)
        self.assertIsNone(grandfather(self.h, template=ALT))
        self.assertEqual(len(self.h.store.all()), 1)

    def test_an_approved_ticket_also_counts_as_already_having_it(self):
        """已经走审批批下来的人再被纳管一次 → 跳过。两种单子是同一份真相。"""
        approved(self.h, template=LONG)
        self.assertIsNone(grandfather(self.h, template=LONG))
        self.assertEqual(len(self.h.store.all()), 1)

    def test_different_services_get_their_own_tickets(self):
        one = grandfather(self.h, template=LONG)
        two = grandfather(self.h, template=TB)
        self.assertNotEqual(one["id"], two["id"])
        held = sa.holdings(self.h.store.all(), self.h.now[0])[LI.union_id]
        self.assertEqual(sorted(svc for svc, _t, _e in held), [SERVICE, TB_SERVICE])

    def test_different_people_get_their_own_tickets(self):
        grandfather(self.h, applicant=LI)
        grandfather(self.h, applicant=NEW)
        self.assertEqual(len(self.h.store.all()), 2)

    def test_an_expired_grant_can_be_renewed(self):
        """授权过期之后再纳管**会**再发一张（续期，应该允许）。

        今天这条路自己发不出带期限的单（限期模板被拒、`payload` 恒空），所以这里
        手工给它装一个到期时间来验这条语义 —— 以后若给纳管加上期限，它就是真实场景了。
        """
        first = grandfather(self.h, template=TB)
        self.h.store.update(
            first["id"],
            actor="system",
            expect=[t.DONE],
            event="probe",
            fields={"expires_at_ts": self.h.now[0] + DAY},
        )
        self.assertIsNone(grandfather(self.h, template=TB), "还没过期就不该再发")
        self.h.now[0] += 2 * DAY
        second = grandfather(self.h, template=TB, reason="过期之后续一次")
        self.assertIsNotNone(second)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(self.h.store.all()), 2)

    def test_after_a_revoke_a_new_one_is_issued(self):
        """**观察到的行为**：撤销过的单不算「有效授权」，所以纳管会再发一张。
        这条边的两种场景（发错人收回 / 离职收回）意义相反，见
        `OffboardReclaimsItTests.test_revoked_then_grandfathered_again_writes_a_fresh_grant`。
        """
        first = grandfather(self.h)
        self.h.flows.revoke_now(first["id"], actor=ADMIN)
        second = grandfather(self.h, reason="收回之后又加回来")
        self.assertIsNotNone(second)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual([x["status"] for x in self.h.store.all()], [t.REVOKED, t.DONE])

    def test_a_skipped_grandfather_writes_nothing_at_all(self):
        """跳过那次是真的什么都没写：不加事件、不改 `updated_at`。"""
        first = grandfather(self.h)
        before = self.h.store.get(first["id"])
        self.h.now[0] += 1000
        self.assertIsNone(grandfather(self.h, reason="再跑一次试试"))
        self.assertEqual(self.h.store.get(first["id"]), before)

    def test_two_concurrent_runs_would_write_two_tickets(self):
        """**已知缺口（Low-3），按现状锁**：查重和建单跨了两把锁。

        `holdings`（读锁）和 `create`（写锁）之间有窗口，两个进程同时跑就是两张单。
        这里用一个**确定性的交错**复现：在第一次 `create` 落盘之前，插进去另一次完整的
        纳管（模拟另一个进程），它查重时看到的还是空台账。

        今天风险低 —— 这条命令是管理员手工跑的，不在定时任务里。但接进定时任务或
        面板按钮之前必须先解决：那时「同一个人两张有效授权单」会静默堆积。
        """
        nested = []
        real = t.TicketStore.create

        def create(store, ticket, *, actor, note="提交申请"):
            if not nested:
                nested.append(True)
                # 另一个进程在我们查完重、还没落盘时整趟跑完
                nested.append(grandfather(self.h, reason="另一个进程同时在跑"))
            return real(store, ticket, actor=actor, note=note)

        with mock.patch.object(t.TicketStore, "create", create):
            mine = grandfather(self.h)
        other = nested[1]
        self.assertIsNotNone(mine)
        self.assertIsNotNone(other)
        self.assertNotEqual(mine["id"], other["id"])
        self.assertEqual(len(self.h.store.all()), 2, "同一个人、同一个服务、两张有效单")
        # 判定侧只认其中一张（挑到期最晚的），另一张就是台账里的一行噪音
        held = sa.holdings(self.h.store.all(), self.h.now[0])[LI.union_id]
        self.assertEqual(len(held), 2)


# ── ⑤ CLI ────────────────────────────────────────────────────────────────


def new_workdir(case: unittest.TestCase) -> Path:
    """一套最小的 identity/：名册 + 模板 + 数据类型词表。

    **必须叫 identity/**：`_grandfather` 对三个路径都过一遍 `_require_identity_dir`
    （含员工姓名邮箱的文件只许落在那儿）。
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    work = Path(tmp.name).resolve()
    ident = work / "identity"
    ident.mkdir()
    rows = [
        {"union_id": "on_li", "name": "李四", "email": "li.si@wuji.tech", "accounts": []},
        {"union_id": "on_new", "name": "新人", "email": "new@wuji.tech", "accounts": []},
        {"union_id": "on_noemail", "name": "没邮箱", "email": "", "accounts": []},
    ]
    (ident / "people.json").write_text(
        json.dumps({"schema": people_mod.SCHEMA, "people": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    (ident / "templates.json").write_text(
        json.dumps(all_templates(), ensure_ascii=False), encoding="utf-8"
    )
    # 模板文件旁边的数据类型词表：`catalog.load` 会自己找它（datatype 模板要用）
    (ident / datatypes_mod.FILENAME).write_text(
        json.dumps(TYPES_TABLE, ensure_ascii=False), encoding="utf-8"
    )
    return work


def write_offboard(work: Path, **states) -> Path:
    """在 `identity/offboard.json` 里给某几个人写一条离职记录。

    **走的是 `offboard` 自己的常量和键格式**（`key_of` / `DISABLED` …）——
    手抄一份状态字符串的话，改名之后这里还是绿的，而线上那道门已经失效了。
    """
    path = offboard.path_beside(str(work / "identity" / "people.json"))
    records = {
        offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, uid): {
            "platform": offboard.SERVICE_PLATFORM,
            "account": SERVICE,
            "user": uid,
            "union_id": uid,
            "state": state,
        }
        for uid, state in states.items()
    }
    path.write_text(json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8")
    return path


def cli_args(
    *,
    union_ids=("on_li",),
    template=LONG,
    reason=REAL_REASON,
    apply=False,
    actor=ADMIN,
    tickets="identity/tickets.json",
) -> argparse.Namespace:
    return argparse.Namespace(
        tickets=tickets,
        templates="identity/templates.json",
        people="identity/people.json",
        template_id=template,
        union_id=list(union_ids),
        reason=reason,
        actor=actor,
        apply=apply,
    )


def run_cli(work: Path, **kw):
    """在 `work` 里跑一次 `delivery requests grandfather`，返回 `(退出码, stdout, stderr)`。

    **顺带钉住「这条命令不碰网络」**：整段跑在一个会炸的 `urlopen` 下面。
    纳管不发飞书、不调云 —— 真去调的话这里当场红。
    """
    out, err = io.StringIO(), io.StringIO()

    def no_network(*a, **kwargs):
        raise AssertionError("纳管不该访问网络")

    cwd = Path.cwd()
    os.chdir(work)
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(urllib.request, "urlopen", no_network))
            # 临时目录不在任何 git 工作树里；和 sweep 那组用例一样把探测短路掉
            stack.enter_context(mock.patch("delivery.cli._git_toplevel", lambda path: None))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = cli_requests._grandfather(cli_args(**kw))  # noqa: SLF001
    finally:
        os.chdir(cwd)
    return code, out.getvalue(), err.getvalue()


def flows_for(work: Path) -> Flows:
    """和 `_grandfather` 里那份一样的 Flows（撤销/查状态时借用）。"""
    ident = work / "identity"
    return Flows(
        store=t.TicketStore(str(ident / "tickets.json")),
        catalog=lambda: catalog_mod.load(str(ident / "templates.json")),
        approval=lambda: None,
        approvals=lambda _name: None,
        roster=lambda: people_mod.load(str(ident / "people.json")),
        executor=lambda platform, account: None,
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.work = new_workdir(self)
        self.ledger = self.work / "identity" / "tickets.json"

    def tickets(self) -> list:
        return t.TicketStore(str(self.ledger)).all()

    # ── 预演 ──

    def test_a_dry_run_writes_nothing(self):
        code, out, err = run_cli(self.work)
        self.assertEqual(code, 0)
        self.assertIn("（预演）会纳管 李四（li.si@wuji.tech） → mlflow", out)
        self.assertIn("没有写盘", out)
        self.assertEqual(err, "")
        self.assertFalse(self.ledger.exists(), "预演连文件都不该建")

    def test_the_dry_run_prints_what_the_template_actually_grants(self):
        """预演要打出**服务名和期限** —— 那才是「这条命令到底发什么」的答案，
        而它们只存在于模板里。不打出来的话，模板被改过在预演里看不出来。"""
        code, out, _err = run_cli(self.work)
        self.assertEqual(code, 0)
        self.assertIn("模板 svc-mlflow", out)
        self.assertIn(f"service={SERVICE}", out)
        self.assertIn("期限=长期", out)

    def test_a_dry_run_leaves_an_existing_ledger_byte_for_byte(self):
        run_cli(self.work, union_ids=["on_new"], apply=True)
        before = self.ledger.read_bytes()
        code, _out, _err = run_cli(self.work, union_ids=["on_li"])
        self.assertEqual(code, 0)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_a_dry_run_rejects_a_template_that_cannot_be_grandfathered(self):
        """预演走**同一条**模板校验：打错 id、指到别的类型，当场拒、退出码非零。

        这条路径没有审批人，「管理员先看一眼预演」就是它唯一的人工闸门 ——
        预演排练的必须是将要发生的那件事。
        """
        for template, hint in (
            ("nope", "没有模板 nope"),
            ("oss-read", "只有「内部服务」能纳管"),
            ("dev-sts", "只有「内部服务」能纳管"),
        ):
            with self.subTest(template):
                code, out, err = run_cli(self.work, template=template)
                self.assertEqual(code, 1)
                self.assertIn(hint, err)
                self.assertNotIn("会纳管", out)
                self.assertFalse(self.ledger.exists())

    def test_a_dry_run_rejects_a_limited_template_too(self):
        """**三道模板级的门在预演里都要生效**，不能只有两道。

        预演是这条路唯一的人工闸门，漏一道就等于排练的不是将要发生的那件事：
        管理员看到「会纳管 …」，`--apply` 时才撞墙。
        """
        code, out, err = run_cli(self.work, template=DATED)
        self.assertEqual(code, 1)
        self.assertIn("30 天的使用期限", err)
        self.assertIn("长期模板", err)
        self.assertNotIn("会纳管", out)
        self.assertFalse(self.ledger.exists())

        code, _out, err = run_cli(self.work, template=DATED, apply=True)
        self.assertEqual(code, 1)
        self.assertIn("长期模板", err)
        self.assertEqual(self.tickets(), [])

    def test_a_dry_run_still_catches_someone_missing_from_the_roster(self):
        code, _out, err = run_cli(self.work, union_ids=["on_ghost"])
        self.assertEqual(code, 1)
        self.assertIn("✗ on_ghost", err)
        self.assertFalse(self.ledger.exists())

    def test_a_dry_run_refuses_someone_in_the_offboard_records(self):
        """离职那道门也在预演里生效 —— 同上：预演排练的必须是将要发生的那件事。"""
        write_offboard(self.work, on_li=offboard.DISABLED)
        code, out, err = run_cli(self.work)
        self.assertEqual(code, 1)
        self.assertIn("在离职记录里", err)
        self.assertNotIn("会纳管", out)
        self.assertFalse(self.ledger.exists())

    def test_a_dry_run_marks_people_who_already_have_it(self):
        run_cli(self.work, apply=True)
        code, out, _err = run_cli(self.work)
        self.assertEqual(code, 0)
        self.assertIn("已经有有效授权", out)
        self.assertNotIn("会纳管 李四", out)

    # ── --apply ──

    def test_apply_writes_exactly_one_ticket(self):
        code, out, _err = run_cli(self.work, apply=True)
        self.assertEqual(code, 0)
        rows = self.tickets()
        self.assertEqual(len(rows), 1)
        self.assertIn(f"✓ 李四（li.si@wuji.tech）：{rows[0]['id']}", out)
        self.assertNotIn("预演", out)
        row = rows[0]
        self.assertEqual(row["status"], t.DONE)
        self.assertEqual(row["applicant"]["union_id"], "on_li")
        self.assertEqual(row["applicant"]["email"], "li.si@wuji.tech")
        self.assertEqual(row["granted"]["by"], ADMIN)
        self.assertEqual(row["granted"]["reason"], REAL_REASON)
        self.assertNotIn("approval", row)
        self.assertEqual(event_names(row), ["created", "admin_granted"])

    def test_the_actor_goes_into_the_ledger(self):
        run_cli(self.work, apply=True, actor="on_wang")
        row = self.tickets()[0]
        self.assertEqual(row["granted"]["by"], "on_wang")
        self.assertEqual({e["actor"] for e in row["events"]}, {"on_wang"})

    def test_running_it_twice_skips_the_second_time(self):
        run_cli(self.work, apply=True)
        code, out, _err = run_cli(self.work, apply=True)
        self.assertEqual(code, 0)
        self.assertIn("已经有有效授权", out)
        self.assertEqual(len(self.tickets()), 1)

    def test_several_union_ids_in_one_go(self):
        code, out, _err = run_cli(self.work, union_ids=["on_li", "on_new"], apply=True)
        self.assertEqual(code, 0)
        self.assertEqual([x["applicant"]["union_id"] for x in self.tickets()], ["on_li", "on_new"])
        self.assertEqual(out.count("✓"), 2)

    def test_the_same_union_id_twice_in_one_go_makes_one_ticket(self):
        code, out, _err = run_cli(self.work, union_ids=["on_li", "on_li"], apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.tickets()), 1)
        self.assertIn("已经有有效授权", out)

    def test_someone_missing_from_the_roster_is_skipped_with_a_non_zero_code(self):
        """名册里没有的人不纳管：判定侧那道 `in_roster` 门会把他拒掉，
        加了也进不去，只会在台账上留一张永远不生效的单。"""
        code, out, err = run_cli(self.work, union_ids=["on_ghost"], apply=True)
        self.assertEqual(code, 1)
        self.assertIn("名册里没有这个人", err)
        self.assertNotIn("✗", out, "所有 ✗ 都走 stderr —— 见 test_every_failure_goes_to_stderr")
        self.assertEqual(self.tickets(), [])

    def test_one_bad_id_does_not_stop_the_rest(self):
        """部分成功：坏的那个报错、退出码非零，**好的那些照样开通**。

        反过来（一个坏 id 让整批不做）会让管理员在漏了一个人的名单上反复重试。
        """
        code, out, err = run_cli(self.work, union_ids=["on_ghost", "on_new"], apply=True)
        self.assertEqual(code, 1)
        self.assertIn("✗ on_ghost", err)
        self.assertIn("✓ 新人", out)
        self.assertEqual([x["applicant"]["union_id"] for x in self.tickets()], ["on_new"])

    def test_a_refusal_on_one_person_does_not_stop_the_rest(self):
        """一个人被流程拒掉（这里：理由太短会对**每个人**都拒）不该中断循环 ——
        循环里抓的是 `DeliveryError`（`FlowError` 是 `TicketError` 的子类，
        只抓 `FlowError` 的话台账损坏那类错会裸奔出去、还把后面的人吞掉）。"""
        code, out, err = run_cli(self.work, union_ids=["on_li", "on_new"], reason="短", apply=True)
        self.assertEqual(code, 1)
        self.assertEqual(err.count("✗"), 2, "第一个人被拒之后要接着处理第二个")
        self.assertNotIn("✗", out)
        self.assertEqual(self.tickets(), [])

    def test_someone_without_an_email_is_shown_by_union_id(self):
        code, out, _err = run_cli(self.work, union_ids=["on_noemail"], apply=True)
        self.assertEqual(code, 0)
        self.assertIn("没邮箱（on_noemail）", out)

    # ── 离职记录 ──

    def test_someone_in_the_offboard_records_is_refused(self):
        """CLI 自己读 `identity/offboard.json`，用**和判定侧、离职回收同一份**数据。"""
        write_offboard(self.work, on_li=offboard.DISABLED)
        code, out, err = run_cli(self.work, union_ids=["on_li"], apply=True)
        self.assertEqual(code, 1)
        self.assertIn("在离职记录里", err)
        self.assertNotIn("✓", out)
        self.assertEqual(self.tickets(), [])

    def test_a_deleted_person_is_refused_and_the_rest_still_run(self):
        write_offboard(self.work, on_li=offboard.DELETED)
        code, out, err = run_cli(self.work, union_ids=["on_li", "on_new"], apply=True)
        self.assertEqual(code, 1)
        self.assertIn("在离职记录里", err)
        self.assertEqual([x["applicant"]["union_id"] for x in self.tickets()], ["on_new"])
        self.assertIn("✓ 新人", out)

    def test_suspect_and_restored_are_not_blocked(self):
        """判据和 `service_access` 的 `gone` 一致：只有 `disabled`/`deleted` 拦人。

        嫌疑不拦是有意的 —— 飞书对「已离职」和「不在应用可见范围内」返回同一个结果，
        拿嫌疑拦人会误伤在职的。
        """
        for state in (offboard.SUSPECT, offboard.RESTORED, offboard.DISMISSED):
            with self.subTest(state=state):
                work = new_workdir(self)
                write_offboard(work, on_li=state)
                code, out, err = run_cli(work, union_ids=["on_li"], apply=True)
                self.assertEqual(code, 0, err)
                self.assertIn("✓", out)

    def test_every_failure_goes_to_stderr(self):
        """**所有 ✗ 都走 stderr**：一半走 stdout 一半走 stderr 的话，
        管理员 `2>/dev/null`、或者只把 stdout 接进管道时，会漏掉其中一类失败。"""
        write_offboard(self.work, on_new=offboard.DISABLED)
        cases = (
            {"template": "nope"},  # 模板不存在
            {"template": "oss-read"},  # kind 不对
            {"template": DATED},  # 限期模板
            {"union_ids": ["on_ghost"]},  # 名册里没有
            {"union_ids": ["on_new"]},  # 离职记录里
            {"reason": "短"},  # 理由太短（这条只有 --apply 才撞上）
        )
        for kw in cases:
            with self.subTest(**kw):
                code, out, err = run_cli(self.work, apply=True, **kw)
                self.assertEqual(code, 1)
                self.assertIn("✗", err)
                self.assertNotIn("✗", out)
        self.assertEqual(self.tickets(), [])

    # ── 路径与损坏 ──

    def test_a_ledger_outside_identity_is_refused(self):
        """**路径写错的后果很阴**：`TicketStore` 会静默新建一个空台账 ——
        屏幕上一排 ✓，当事人依然进不去，真台账上一个字没有。所以三个路径都过一道门。
        """
        outside = str(self.work / "tickets.json")
        with self.assertRaises(DeliveryError) as caught:
            run_cli(self.work, tickets=outside, apply=True)
        self.assertIn("identity", str(caught.exception))
        self.assertFalse((self.work / "tickets.json").exists())

    def test_a_broken_ledger_stops_the_whole_run(self):
        """**观察到的行为**：台账读不了时 `_grandfather` 直接抛 `TicketError`
        （`DeliveryError` 的子类）—— `cli.main` 把它翻成退出码 2。

        不是静默跳过：一份读不了的台账意味着查重也做不了，继续往下写就是在
        一个未知状态上发权限。
        """
        self.ledger.write_text("{ 坏掉的 json", encoding="utf-8")
        with self.assertRaises(DeliveryError):
            run_cli(self.work, apply=True)

    def test_the_production_run(self):
        """生产上要跑的那条：两个人、MLflow、那句理由。"""
        code, out, _err = run_cli(
            self.work, union_ids=["on_li", "on_new"], template=LONG, reason=REAL_REASON, apply=True
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.count("✓"), 2)
        rows = self.tickets()
        self.assertEqual({x["granted"]["reason"] for x in rows}, {REAL_REASON})
        self.assertEqual({x["status"] for x in rows}, {t.DONE})
        for row in rows:
            self.assertFalse(went_through_approval(row))
        opened = {
            uid
            for uid in ("on_li", "on_new")
            if sa.allowed(union_id=uid, service=SERVICE, tickets=rows, now=1_800_000_000.0).allowed
        }
        self.assertEqual(opened, {"on_li", "on_new"})


def grandfather_options() -> set:
    """`requests grandfather` 这条命令认识哪些开关。"""
    parser = build_parser()
    [commands] = parser._subparsers._group_actions  # noqa: SLF001
    requests = commands.choices["requests"]
    [rsub] = requests._subparsers._group_actions  # noqa: SLF001
    gf = rsub.choices["grandfather"]
    return {opt for action in gf._actions for opt in action.option_strings}  # noqa: SLF001


class CliParserTests(unittest.TestCase):
    """命令行本身：必填项、默认值、以及 `dispatch` 把它路由到哪。"""

    def parse(self, *argv):
        return build_parser().parse_args(["requests", "grandfather", *argv])

    def test_the_defaults(self):
        args = self.parse("--template-id", LONG, "--union-id", "on_li", "--reason", "r")
        self.assertEqual(args.command, "requests")
        self.assertEqual(args.requests_command, "grandfather")
        self.assertEqual(args.union_id, ["on_li"])
        self.assertEqual(args.actor, ADMIN)
        self.assertFalse(args.apply, "不给 --apply 就是预演 —— 默认必须是不写盘那一侧")
        self.assertEqual(args.tickets, "identity/tickets.json")
        self.assertEqual(args.templates, "identity/request-templates.json")
        self.assertEqual(args.people, "identity/people.json")

    def test_union_id_repeats(self):
        args = self.parse(
            "--template-id", LONG, "--union-id", "a", "--union-id", "b", "--reason", "r"
        )
        self.assertEqual(args.union_id, ["a", "b"])

    def test_every_required_flag_is_required(self):
        """三个必填项各缺一次。缺了要当场报错，不能默默按空值跑。"""
        full = {"--template-id": LONG, "--union-id": "on_li", "--reason": "r"}
        for missing in full:
            with self.subTest(missing=missing):
                argv = [x for k, v in full.items() if k != missing for x in (k, v)]
                with (
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as caught,
                ):
                    self.parse(*argv)
                self.assertEqual(caught.exception.code, 2)

    def test_there_is_no_batch_switch(self):
        """**不提供「把名册里所有人都加上」**：那种开关一旦写错来源，就是一次性
        给一批人发权限，而且看起来一切正常。"""
        options = grandfather_options()
        self.assertEqual(options & {"--all", "--everyone", "--from-file", "--file"}, set())
        self.assertLessEqual(
            {"--template-id", "--union-id", "--reason", "--actor", "--apply"}, options
        )

    def test_dispatch_routes_to_grandfather_not_sweep(self):
        """`dispatch` 里 `grandfather` 的分支**必须在兜底的 `_sweep` 之前命中**。
        漏了那一行的表现是：敲纳管命令，跑的是定时同步，而且退出码是 0。"""
        args = self.parse("--template-id", LONG, "--union-id", "on_li", "--reason", "r")
        boom = AssertionError("不该跑")
        with (
            mock.patch.object(cli_requests, "_grandfather", return_value=7) as gf,
            mock.patch.object(cli_requests, "_sweep", side_effect=boom),
        ):
            self.assertEqual(cli_requests.dispatch(args), 7)
        self.assertEqual(gf.call_count, 1)


# ── ⑥ `create` 的 note 参数没有碰到既有调用点 ────────────────────────────


class CreateNoteTests(unittest.TestCase):
    def test_the_default_is_unchanged_verbatim(self):
        default = inspect.signature(t.TicketStore.create).parameters["note"].default
        self.assertEqual(default, "提交申请")

    def test_a_normal_submission_still_says_the_same_thing(self):
        h = harness()
        ticket = h.submit(applicant=LI, template=LONG, payload={})
        self.assertEqual(ticket["events"][0]["note"], "提交申请")
        self.assertEqual(ticket["events"][0]["event"], "created")
        self.assertEqual(ticket["events"][0]["actor"], LI.union_id)

    def test_only_the_grandfather_call_site_passes_a_note(self):
        """**结构锁**：源码里只有纳管那一处给 `create` 传 `note=`。

        别处开始传的话，「created 的说法 = 这张单怎么来的」就不再是判据了。
        """
        passing = []
        for path in sorted(SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                        continue
                    if node.func.attr == "create" and any(k.arg == "note" for k in node.keywords):
                        passing.append((path.name, func.name))
        self.assertEqual(passing, [("flows.py", "grandfather_service")], passing)

    def test_the_grandfather_note_says_it_was_not_submitted_by_the_person(self):
        got = grandfather(harness())
        self.assertEqual(got["events"][0]["note"], "管理员纳管（不是本人提交的）")


# ── ⑦ 那条状态边：只对 service 开，而且只加不减 ──────────────────────────


#: 这张表是**测试自己写死的期望**，不从 `tickets` 里读 —— 读过来就成了同义反复。
#: 含义：除了 `TRANSITIONS` 里本来就有的边之外，这几个 (kind, 起点) 还能多走这些终点。
EXTRA_EDGES = {("service", t.SUBMITTING): {t.DONE}}


class TransitionEdgeTests(unittest.TestCase):
    """`SUBMITTING → DONE` 按 kind 额外放开（`tickets.EXTRA_TRANSITIONS_BY_KIND`）。

    全局放开那条边等于让任何一类单子都能跳过开通前的全部核对
    （`permission` 跳过云上授权、`credential` 跳过签发），而那些核对才是流程的本体。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "tickets.json"
        self.store = t.TicketStore(str(self.path))

    def can(self, kind, status, to) -> bool:
        """这张 kind 的单子，能不能从 status 转到 to。"""
        self.path.write_text(
            json.dumps(
                {
                    "schema": t.SCHEMA,
                    "tickets": [{"id": "REQ-1", "kind": kind, "status": status, "events": []}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            self.store.update("REQ-1", actor="probe", expect=[status], to=to, event="probe")
        except t.TicketError:
            return False
        return True

    #: 探针覆盖的 kind：八类 + 两个「模板里不该出现」的写法
    KINDS = (*catalog_mod.KINDS, "", "Service")

    def test_the_whole_matrix(self):
        """kind × 起点 × 终点 全矩阵：允许的边 = `TRANSITIONS` + 上面那张 `EXTRA_EDGES`。

        一条一条列规则是列不完的，所以直接把整张表算出来比。以后谁给
        `EXTRA_TRANSITIONS_BY_KIND` 加一条边（或者顺手把某条边从 `TRANSITIONS`
        挪进去），这里会精确指出是哪个 (kind, 起点, 终点)。
        """
        statuses = sorted(t.TRANSITIONS)
        for kind in self.KINDS:
            for status in statuses:
                extra = EXTRA_EDGES.get((kind, status), set())
                for to in statuses:
                    want = to == status or to in (t.TRANSITIONS[status] | extra)
                    with self.subTest(kind=kind, status=status, to=to):
                        self.assertEqual(self.can(kind, status, to), want)

    def test_only_service_can_jump_from_submitting_to_done(self):
        """读起来最直白的那条（上面的矩阵已经覆盖，但这条是给人看的）。"""
        for kind in catalog_mod.KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(
                    self.can(kind, t.SUBMITTING, t.DONE), kind == catalog_mod.KIND_SERVICE
                )

    def test_the_extra_table_only_adds_never_restricts(self):
        """**只加不减**：按 kind 那张表不能用来收紧 `TRANSITIONS`。

        能收紧的话，「这张单能不能这么转」就要同时看两张表，而漏看一张的后果是
        静默放行（或静默拒绝）。这里从两头验：
          · 结构上，表里每条边都是 `TRANSITIONS` 的**净增量**（否则那条目是死的、
            读代码的人会以为它在限制什么）；
          · 行为上，`TRANSITIONS` 里的每条边，对**每个 kind** 都照样走得通。
        """
        for kind, table in t.EXTRA_TRANSITIONS_BY_KIND.items():
            for status, targets in table.items():
                self.assertIn(status, t.TRANSITIONS, (kind, status))
                self.assertTrue(targets - t.TRANSITIONS[status], (kind, status, targets))
        for kind in self.KINDS:
            for status, targets in t.TRANSITIONS.items():
                for to in sorted(targets):
                    with self.subTest(kind=kind, status=status, to=to):
                        self.assertTrue(self.can(kind, status, to))

    def test_the_table_today_is_exactly_one_edge(self):
        """今天表里只有一条边。新增一条是**刻意的动作**，该在这里被看见一次。"""
        self.assertEqual(t.EXTRA_TRANSITIONS_BY_KIND, {"service": {t.SUBMITTING: {t.DONE}}})
        self.assertEqual(t.TRANSITIONS[t.SUBMITTING], {t.PENDING, t.SUBMIT_FAILED})
        for kind, table in t.EXTRA_TRANSITIONS_BY_KIND.items():
            self.assertIn(kind, catalog_mod.KINDS, kind)
            for status, targets in table.items():
                self.assertIn(status, t.TRANSITIONS, status)
                self.assertTrue(targets <= set(t.TRANSITIONS), targets)

    def test_only_grandfather_service_uses_that_edge(self):
        """**结构锁**：源码里把单子从「提交中」直接推到「已完成」的，只有纳管那一处。

        形式和「`decide(` 的调用点不许直接喂 `executor_from_env`」那条一样：
        找出所有 `update(expect=[…SUBMITTING…], to=…DONE)` 的调用点，看它们在哪个函数里。
        """
        found = []
        for path in sorted(SRC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    kwargs = {kw.arg: kw.value for kw in node.keywords}
                    if not _mentions(kwargs.get("to"), t.DONE):
                        continue
                    if not _mentions(kwargs.get("expect"), t.SUBMITTING):
                        continue
                    found.append((path.name, func.name))
        self.assertEqual(found, [("flows.py", "grandfather_service")], found)


def _mentions(node, status: str) -> bool:
    """这个实参有没有提到某个状态常量（`t.DONE` / `DONE` / `[t.SUBMITTING]` …）。

    按名字比、不按值比：源码是文本，能拿到的只有名字。状态常量的名字就是它的大写形式
    （`done` → `DONE`），所以这个换算是稳的。
    """
    if node is None:
        return False
    want = status.upper()
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == want:
            return True
        if isinstance(child, ast.Name) and child.id == want:
            return True
    return False


class ReadOnlyFieldsTests(unittest.TestCase):
    """`TicketStore.update` 的禁写字段名单。**`kind` 是这批改动新加进去的。**

    它现在决定这张单能走哪些状态边（`EXTRA_TRANSITIONS_BY_KIND`）—— 能改 kind
    就等于能给自己挑一条更宽的状态机，那条「只对 service 放开」的边就形同虚设。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "tickets.json"
        self.store = t.TicketStore(str(self.path))
        self.path.write_text(
            json.dumps(
                {
                    "schema": t.SCHEMA,
                    "tickets": [
                        {
                            "id": "REQ-1",
                            "kind": "permission",
                            "status": t.SUBMITTING,
                            "template": {"id": "oss-read", "kind": "permission"},
                            "applicant": {"union_id": "on_li"},
                            "created_at": "2026-01-01T00:00:00+08:00",
                            "events": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def write(self, **fields):
        return self.store.update(
            "REQ-1", actor="probe", expect=[t.SUBMITTING], event="probe", fields=fields
        )

    def test_each_read_only_field_is_refused(self):
        for key, value in (
            ("id", "REQ-2"),
            ("status", t.DONE),
            ("events", []),
            ("applicant", {"union_id": "on_other"}),
            ("created_at", "2020-01-01T00:00:00+08:00"),
            ("kind", "service"),
        ):
            with self.subTest(key):
                with self.assertRaises(t.TicketError) as caught:
                    self.write(**{key: value})
                self.assertIn(key, str(caught.exception))
                self.assertEqual(caught.exception.status, 500)

    def test_ordinary_fields_are_still_writable(self):
        """名单是**窄的**：别的字段照常写得进去，否则开通流程整条会断。"""
        got = self.write(result="开通了", expires_at_ts=123.0)
        self.assertEqual(got["result"], "开通了")
        self.assertEqual(got["expires_at_ts"], 123.0)

    def test_a_permission_ticket_cannot_rename_itself_into_a_service_one(self):
        """**这条是承重的**：改不了 kind，就拿不到 service 那条更宽的边。

        两步都要验（组合用例）：**第一步就得挂**，而不是「改成功了、跳转那步才拦住」。
        以后有人为了某个需求把 `kind` 从名单里拿掉，这里会红 —— 而线上表现是一张
        `permission` 单可以把自己改姓 service，然后从「提交中」直接跳到「已完成」，
        云上什么都没做，台账上却写着开通了。
        """
        with self.assertRaises(t.TicketError):
            self.write(kind="service")
        self.assertEqual(self.store.get("REQ-1")["kind"], "permission")
        with self.assertRaises(t.TicketError):
            self.store.update(
                "REQ-1", actor="probe", expect=[t.SUBMITTING], to=t.DONE, event="probe"
            )
        self.assertEqual(self.store.get("REQ-1")["status"], t.SUBMITTING)

    @unittest.expectedFailure
    def test_the_template_snapshot_should_be_read_only_too(self):
        """**已知缺口，留给下一批**：`template` 同样该是只读的。

        它是提交那一刻冻下来的快照，开通前的核对（`_verify`）全靠它和活模板比对 ——
        能改它就等于能绕过那道核对。今天没禁，是因为测试里有个 `set_field` 辅助
        （`test_delivery_regrant_flow.py`）专门靠改它来造「老单子的旧快照」场景，
        禁了会挡掉一个合法的测试手法。要禁得先给那批用例换一种造夹具的办法。

        这条用例**跑绿了就说明缺口补上了** —— 到时候把 `expectedFailure` 去掉。
        """
        with self.assertRaises(t.TicketError):
            self.write(template={"id": "oss-read", "kind": "service", "service": "mlflow"})


if __name__ == "__main__":
    unittest.main()
