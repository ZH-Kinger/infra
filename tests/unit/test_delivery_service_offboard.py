"""自建服务（MLflow 这类）进离职回收：`service_access.holdings` → `offboard` → 判定侧。

这个洞是什么
────────────
离职回收原先只按**云子账号**记（`key_of(platform, account, user)`，来源是
`person.accounts`），而自建服务的目标人群**恰恰是没有云子账号的那批人**。他们离职后
`targets_of` 返空 → `auto_disable` 把人记进 `report["skipped"]` → **一条记录都不写** →
MLflow 照常进得去，而且没有任何地方会报错（审计 High-3）。

「没有记录」和「没有东西要收」长得一模一样，这是它最难被发现的原因 —— 所以下面
`TheHoleTests` 用一条端到端用例把修前修后的两种表现都钉住。

另一半：两处判据必须是同一段
────────────────────────────
放行（`allowed`，网关每个请求问一次）和持有（`holdings`，离职回收 + 面板展示）
一旦各判各的，最先背离的就是「页面/回收说有、网关说没有」，而那种不一致
**没有任何地方会报错**。所以这里不逐条列规则，而是直接做**性质测试**：
枚举一大批单子（各种 kind/状态/过期/脏行/坏 `expires_at_ts`），断言
`holdings` 里出现的 `(union_id, 服务)` **恰好等于** `allowed` 为真的那批。
规则以后怎么改都行，这条性质破了就是背离。

离线，数据虚构。
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delivery import cli as cli_mod
from delivery import offboard, provision
from delivery import service_access as sa
from delivery import tickets as tickets_mod
from delivery.people import AccountRef, Person
from delivery.server import Backend

NOW = 1_700_000_000.0
UID = "on_u1"
OTHER = "on_u2"
SERVICE = "mlflow"
OTHER_SERVICE = "tensorboard"
ALI_ACC = "1000000000000001"


def ticket(
    *,
    tid: str = "REQ-1",
    kind: str = "service",
    status: str = tickets_mod.DONE,
    service: str = SERVICE,
    union_id: str = UID,
    expires: object = None,
    **extra,
) -> dict:
    """一张服务访问单。默认「批下来了、没到期、是这个人的」。"""
    row = {
        "id": tid,
        "kind": kind,
        "status": status,
        "template": {"id": "svc-mlflow", "kind": "service", "service": service},
        "applicant": {"union_id": union_id, "name": "李四", "email": "li.si@wuji.tech"},
        "payload": {"days": 30},
    }
    if expires is not None:
        row["expires_at_ts"] = expires
    row.update(extra)
    return row


def person(name="李四", uid=UID, *refs, email="li.si@wuji.tech"):
    return Person(name=name, email=email, union_id=uid, accounts=tuple(refs))


def ref(platform="aliyun", name="lisi", account=ALI_ACC, status="confirmed"):
    return AccountRef(platform, account, name, status)


# ── 判据只有一处：holdings ↔ allowed 的性质 ───────────────────────────────


#: 性质测试的取值面。**脏数据是主角**：这条链上「判不出来」的那一次不会报错，
#: 只会表现成两边给的答案不一样
_UIDS = (UID, OTHER, "")
_SERVICES = (SERVICE, OTHER_SERVICE, "")
_STATUSES = (
    tickets_mod.DONE,
    tickets_mod.REVOKED,
    tickets_mod.PENDING,
    tickets_mod.CLOSED,
    "DONE",  # 大小写不同 = 另一个状态，不该放行
    "",
)
_KINDS = ("service", "permission", "credential", "", None)
#: `expires_at_ts` 的取值。`"2026-10-01"` 这种**解析不出来**的值是重点：
#: 兜底成 0 等于发永久通行证，两侧的兜底方向必须一致
_EXPIRES = (
    None,
    0,
    "",
    NOW - 1,
    NOW,
    NOW + 1,
    NOW + 86400,
    "2026-10-01",
    "abc",
    [1],
    "nan",  # 合法的 float，但 `nan <= now` 恒假 → 曾是永不过期的通行证
    "inf",
    -1,
)


def _dirty_rows() -> list:
    """结构坏掉的单子。一行脏数据既不该放行，也不该让任何一侧抛异常。"""
    rows = [
        None,
        "不是对象",
        123,
        ["列表"],
        {},
        ticket(template=None),
        ticket(template="mlflow"),
        ticket(template=["mlflow"]),
        ticket(applicant=None),
        ticket(applicant="on_u1"),
        ticket(applicant=[UID]),
    ]
    no_tpl = ticket()
    no_tpl.pop("template")
    rows.append(no_tpl)
    no_who = ticket()
    no_who.pop("applicant")
    rows.append(no_who)
    rows.append(ticket(template={"id": "svc", "kind": "service"}))  # 模板里没有 service
    rows.append(ticket(applicant={"name": "李四"}))  # 申请人里没有 union_id
    return rows


def _pairs_from_holdings(held: dict) -> set:
    return {(uid, svc) for uid, rows in held.items() for svc, _tid, _exp in rows}


def _pairs_from_allowed(rows, universe) -> set:
    """放行侧放行了哪些 `(union_id, 服务)`，**按归一化后的值记账**。

    归一化（strip）由 `active_grants` 统一做，两侧拿的是同一份值 —— 所以拿
    `" on_x "` 去问和拿 `"on_x"` 去问是同一件事，这里要跟着归一化，否则比的是
    「我问的时候怎么写的」而不是「两侧判得一不一致」。
    """
    out = set()
    for uid, svc in universe:
        if sa.allowed(union_id=uid, service=svc, tickets=rows, now=NOW).allowed:
            out.add((str(uid).strip(), str(svc).strip()))
    return out


class HoldingsMatchesAllowedTests(unittest.TestCase):
    """**性质**：`holdings` 里出现的 `(union_id, 服务)` 恰好是 `allowed` 放行的那批。

    背离的代价：面板/离职回收说「他有 MLflow」而网关说「他没有」（或反过来）。
    两边都不会报错，发现它的方式只有人去比对 —— 而没有人会去比对。
    """

    #: 性质要在这些 `(union_id, 服务)` 上成立。空串两边都该判「没有」，也要覆盖
    UNIVERSE = tuple(itertools.product(_UIDS, _SERVICES))

    def check(self, rows, msg=""):
        held = sa.holdings(rows, NOW)
        self.assertEqual(
            _pairs_from_holdings(held),
            _pairs_from_allowed(rows, self.UNIVERSE),
            msg or repr(rows),
        )
        return held

    def test_single_ticket_across_the_whole_value_space(self):
        """逐张单子过一遍取值面（状态 × kind × 服务 × 人 × 到期）。"""
        combos = itertools.product(_STATUSES, _KINDS, _SERVICES, _UIDS, _EXPIRES)
        for status, kind, service, uid, expires in combos:
            row = ticket(status=status, kind=kind, service=service, union_id=uid, expires=expires)
            with self.subTest(status=status, kind=kind, service=service, uid=uid, exp=expires):
                self.check([row])

    def test_whole_value_space_in_one_ledger(self):
        """整份台账一起判：多张单子混在一起时两侧也不能背离。"""
        rows = [
            ticket(
                tid=f"REQ-{i}",
                status=status,
                kind=kind,
                service=service,
                union_id=uid,
                expires=expires,
            )
            for i, (status, kind, service, uid, expires) in enumerate(
                itertools.product(_STATUSES, _KINDS, _SERVICES, _UIDS, _EXPIRES)
            )
        ]
        held = self.check(rows, "整份台账")
        # 这份台账里两个人两个服务都该有有效授权，否则这条用例等于没判什么
        self.assertEqual(
            _pairs_from_holdings(held),
            set(itertools.product((UID, OTHER), (SERVICE, OTHER_SERVICE))),
        )

    def test_dirty_rows_alone_and_mixed_with_good_ones(self):
        """脏行：两侧都不许抛，也不许其中一侧把它算成授权。"""
        dirty = _dirty_rows()
        self.check(dirty, "全是脏行")
        self.assertEqual(sa.holdings(dirty, NOW), {})
        good = ticket(expires=NOW + 86400)
        for i, bad in enumerate(dirty):
            with self.subTest(i=i):
                # 脏行在前、在后都试：短路写错时只有一种顺序会暴露
                self.check([bad, good])
                self.check([good, bad])

    def test_all_small_ledgers_drawn_from_a_mixed_pool(self):
        """从一池混合单子里枚举所有 ≤3 张的组合（确定性，不用随机）。"""
        pool = [
            ticket(tid="A", expires=NOW + 86400),
            ticket(tid="B"),  # 不限期
            ticket(tid="C", expires=NOW - 1),  # 已过期
            ticket(tid="D", expires=NOW),  # 到期时刻本身
            ticket(tid="E", status=tickets_mod.REVOKED, expires=NOW + 86400),
            ticket(tid="F", status=tickets_mod.PENDING),
            ticket(tid="G", kind="permission", expires=NOW + 86400),
            ticket(tid="H", union_id=OTHER, expires=NOW + 86400),
            ticket(tid="I", service=OTHER_SERVICE, expires=NOW + 86400),
            ticket(tid="J", expires="2026-10-01"),  # 解析不出来
            ticket(tid="K", expires=0),  # 显式 0 = 不限期
            ticket(tid="L", expires=""),
            ticket(tid="M", union_id=""),
            ticket(tid="N", service=""),
            ticket(tid="O", template=None),
            ticket(tid="P", applicant=None),
            None,
            "脏",
        ]
        for size in range(4):
            for combo in itertools.combinations(pool, size):
                self.check(list(combo))

    def test_expiry_and_ticket_id_agree_too(self):
        """不只是「有没有」：放行侧回的到期时间和单号也必须来自同一批单子。

        `allowed` 在多张有效单里挑**到期最晚**的那张，不限期最优（续期时新旧并存）。
        """
        cases = {
            "续期并存": [ticket(tid="old", expires=NOW + 10), ticket(tid="new", expires=NOW + 999)],
            "新的在前": [ticket(tid="new", expires=NOW + 999), ticket(tid="old", expires=NOW + 10)],
            "不限期在后": [ticket(tid="a", expires=NOW + 10), ticket(tid="b")],
            "不限期在前": [ticket(tid="b"), ticket(tid="a", expires=NOW + 10)],
            "掺过期的": [ticket(tid="x", expires=NOW - 1), ticket(tid="y", expires=NOW + 5)],
        }
        for name, rows in cases.items():
            with self.subTest(name):
                held = sa.holdings(rows, NOW)[UID]
                got = sa.allowed(union_id=UID, service=SERVICE, tickets=rows, now=NOW)
                expiries = [exp for _svc, _tid, exp in held]
                want = 0.0 if any(not e for e in expiries) else max(expiries)
                self.assertTrue(got.allowed)
                self.assertEqual(got.expires_at, want, held)
                self.assertIn(got.ticket_id, [tid for _svc, tid, _exp in held])

    #: 台账里带空白的标识。**怎么处理由 dev 定，但两侧必须一致** —— 见下面两条
    PADDED = (
        (f" {UID} ", SERVICE),
        (UID, f" {SERVICE}"),
        (f"{UID}\n", SERVICE),
        (f"\t{UID}", f"{SERVICE}\t"),
    )

    def test_padded_identifiers_never_diverge(self):
        """**回归（方向无关）**：`allowed()` 原先只 strip 入参、拿去比单子里的原始值，
        于是台账里带空白的那一行 `holdings` 算「他有」、`allowed` 永远算「他没有」——
        正是这次重构要消灭的那种背离，只不过藏在一次 strip 的不对称里。

        这一条只断言**两侧一致**，不管选哪个方向（归一化 / 当脏行拒掉）都成立。
        方向本身在下面那条里，改方向只用改那一条。
        """
        for uid, svc in self.PADDED:
            with self.subTest(uid=repr(uid), svc=repr(svc)):
                rows = [ticket(union_id=uid, service=svc, expires=NOW + 10)]
                self.assertEqual(
                    _pairs_from_holdings(sa.holdings(rows, NOW)),
                    _pairs_from_allowed(rows, [(uid, svc), (UID, SERVICE)]),
                )

    def test_padded_identifiers_are_rejected_not_normalised(self):
        """**方向**：带空白的标识当**脏行**拒掉，两边同样地说「没有」。

        不选「两边都 strip」是因为那会把 `union_id` 从精确标识变成归一化比对 ——
        归一化过的比对早晚会把两个人认成一个（`test_delivery_service_access.
        AllowedTests.test_union_id_case_or_space_mismatch_does_not_count` 锁的就是这条）。
        消除背离的正确方向是让两边同样地**说没有**，不是同样地放宽。

        代价写在明面上：那张单谁也用不了（fail-closed）—— 而台账里本来就不该有这种行。
        """
        for uid, svc in self.PADDED:
            with self.subTest(uid=repr(uid), svc=repr(svc)):
                rows = [ticket(union_id=uid, service=svc, expires=NOW + 10)]
                self.assertEqual(sa.holdings(rows, NOW), {})
                for ask_uid, ask_svc in ((uid, svc), (UID, SERVICE)):
                    self.assertFalse(
                        sa.allowed(union_id=ask_uid, service=ask_svc, tickets=rows, now=NOW).allowed
                    )
        # 干净的那张照常放行（否则上面可能只是「什么都判不出来」）
        clean = [ticket(union_id=UID, service=SERVICE, expires=NOW + 10)]
        self.assertTrue(sa.allowed(union_id=UID, service=SERVICE, tickets=clean, now=NOW).allowed)

    def test_one_person_many_services_is_grouped_not_overwritten(self):
        rows = [
            ticket(tid="a", service=SERVICE, expires=NOW + 10),
            ticket(tid="b", service=OTHER_SERVICE),
            ticket(tid="c", service=SERVICE, expires=NOW + 20),
            ticket(tid="d", service=SERVICE, union_id=OTHER),
        ]
        held = self.check(rows)
        self.assertEqual(
            sorted(held[UID]),
            sorted([(OTHER_SERVICE, "b", 0.0), (SERVICE, "a", NOW + 10), (SERVICE, "c", NOW + 20)]),
        )
        self.assertEqual(held[OTHER], [(SERVICE, "d", 0.0)])

    def test_empty_and_none_ledger(self):
        self.assertEqual(sa.holdings([], NOW), {})
        self.assertEqual(sa.holdings(None, NOW), {})
        self.assertEqual(sa.holdings((), NOW), {})

    def test_holdings_takes_any_iterable_once(self):
        """台账可能是生成器（`active_grants` 本身就是）。消费一次就够，别要求可重入。"""
        rows = iter([ticket(expires=NOW + 10)])
        self.assertEqual(sa.holdings(rows, NOW), {UID: [(SERVICE, "REQ-1", NOW + 10)]})

    def test_missing_id_becomes_empty_string_not_none(self):
        """单号缺失时回空串：`None` 会一路漏到卡片/链接上渲染成「None」。"""
        held = sa.holdings([ticket(id=None)], NOW)
        self.assertEqual(held[UID], [(SERVICE, "", 0.0)])

    def test_expiry_is_a_float(self):
        held = sa.holdings([ticket(expires="1700000999")], NOW)
        self.assertEqual(held[UID], [(SERVICE, "REQ-1", 1700000999.0)])
        self.assertIsInstance(held[UID][0][2], float)


class ActiveGrantsTests(unittest.TestCase):
    """`active_grants` 是那唯一一段判据本身。"""

    def test_yields_ticket_expiry_uid_and_service(self):
        """产出四元组 `(单子, 到期, union_id, 服务名)` —— 后两项**在这里就判完**，
        两个消费者拿的是同一份值（那次背离就是藏在一次 strip 的不对称里）。"""
        rows = [ticket(tid="a", expires=NOW + 5), ticket(tid="b"), ticket(tid="c", expires=NOW - 5)]
        got = list(sa.active_grants(rows, NOW))
        self.assertEqual(
            [(t["id"], e, uid, svc) for t, e, uid, svc in got],
            [("a", NOW + 5, UID, SERVICE), ("b", 0.0, UID, SERVICE)],
        )

    def test_padded_identity_or_service_is_dropped_here(self):
        """**归一化的活儿在这一段做完**（这里是丢掉，不是 strip —— 见
        `test_padded_identifiers_are_rejected_not_normalised`）。两个消费者都不再自己判，
        所以不会出现「一边 strip 了、一边没 strip」那种背离。"""
        for uid, svc in ((f" {UID}", SERVICE), (f"{UID} ", SERVICE), (UID, f" {SERVICE}")):
            with self.subTest(uid=repr(uid), svc=repr(svc)):
                rows = [ticket(union_id=uid, service=svc)]
                self.assertEqual(list(sa.active_grants(rows, NOW)), [])

    def test_blank_identity_or_service_is_dropped_here(self):
        """空 uid / 空服务名在这一步就丢掉，下游不用各自再判一次（判漏就是背离）。"""
        for uid, svc in ((" ", SERVICE), (UID, "  "), ("", ""), ("\t", "\n")):
            with self.subTest(uid=repr(uid), svc=repr(svc)):
                self.assertEqual(
                    list(sa.active_grants([ticket(union_id=uid, service=svc)], NOW)), []
                )

    def test_is_lazy(self):
        """生成器：网关每个请求都要跑一次，不该先把整份台账物化一遍。"""
        gen = sa.active_grants([ticket()], NOW)
        self.assertFalse(isinstance(gen, (list, tuple)))
        self.assertEqual(len(list(gen)), 1)

    def test_bad_expiry_is_no_grant_not_forever(self):
        """解析不出来的到期值当「没有授权」，**不当「不限期」** —— 后者是发永久通行证。"""
        for bad in ("2026-10-01", "abc", [1], {}, "2026-10-01T00:00:00"):
            with self.subTest(bad):
                self.assertEqual(list(sa.active_grants([ticket(expires=bad)], NOW)), [])

    def test_nan_infinity_and_negative_timestamps_are_no_grant(self):
        """**永不过期的通行证**：`float("nan")` 是合法值，而 `nan <= now` 恒为假 ——
        一张 `expires_at_ts: "nan"` 的单会永远有效，台账上还看着像个正常数字。
        `inf` 同理；负数（1970 年之前到期）只可能是写坏的。

        方向和别的脏值一致：当作**没有授权**，不是「不限期」。
        """
        for bad in ("nan", "NaN", float("nan"), "inf", "-inf", float("inf"), -1, "-5", -0.5):
            with self.subTest(repr(bad)):
                self.assertEqual(list(sa.active_grants([ticket(expires=bad)], NOW)), [], repr(bad))
                self.assertEqual(sa.holdings([ticket(expires=bad)], NOW), {})
                self.assertFalse(
                    sa.allowed(
                        union_id=UID, service=SERVICE, tickets=[ticket(expires=bad)], now=NOW
                    ).allowed
                )

    def test_explicit_zero_and_empty_mean_unlimited(self):
        for raw in (0, "", None):
            with self.subTest(raw):
                got = list(sa.active_grants([ticket(expires=raw)], NOW))
                self.assertEqual([e for _t, e, _u, _s in got], [0.0])


# ── 那个洞本身 ────────────────────────────────────────────────────────────


class _OffboardBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve()
        self.people_path = self.dir / "people.json"
        self.tickets_path = self.dir / "tickets.json"
        self.write_people(person())
        # **不限期**：这一组用例会走真实时钟的 Backend / CLI，钉死的时间戳会过期
        self.write_tickets(ticket())
        self.path = offboard.path_beside(str(self.people_path))
        self.calls = []

    # ── 文件 ──

    def write_people(self, *people):
        self.people_path.write_text(
            json.dumps(
                {
                    "schema": "wuji-people@1",
                    "people": [
                        {"union_id": p.union_id, "name": p.name, "email": p.email} for p in people
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def write_tickets(self, *rows):
        self.tickets_path.write_text(
            json.dumps({"schema": tickets_mod.SCHEMA, "tickets": list(rows)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def records(self):
        return offboard.load(self.path)

    def holdings(self, *rows):
        return sa.holdings(list(rows) or [ticket()], NOW)

    # ── 假执行体 ──

    def cloud(self, platform, account):
        """云执行体。**服务那一路走到这里就是错的**，所以它记账 + 抛。"""
        self.calls.append((platform, account))
        raise AssertionError(f"服务回收不该调云执行体：{platform}/{account}")

    def working_cloud(self, platform, account):
        self.calls.append((platform, account))
        return _FakeCloudEx()

    def dispatch(self, platform, account):
        """和线上一样：internal 走 ServiceAccess，其余走云。"""
        if platform == offboard.SERVICE_PLATFORM:
            return offboard.ServiceAccess()
        return self.working_cloud(platform, account)


class _FakeCloudEx:
    def disable_user(self, user):
        return {"login": True, "keys": [f"AK-{user}"]}


class TheHoleTests(_OffboardBase):
    """**回归**：没有云子账号的人离职后，一条记录都不写、MLflow 照常进得去。

    修之前和修之后的两种表现在同一条用例里对照着钉住 —— 只钉修后的话，
    「没有记录」看起来和「本来就没东西要收」一模一样，用例会在洞回来的时候照样绿。
    """

    def setUp(self):
        super().setUp()
        # 目标人群：有 union_id、有邮箱、**一个云子账号都没有**
        self.p = person("赵六", UID, email="zhaoliu@wuji.tech")
        self.write_people(self.p)
        self.cands = [(self.p, "飞书状态：已离职")]

    def backend(self):
        return Backend(
            people_path=str(self.people_path),
            tickets_path=str(self.tickets_path),
            platforms={"aliyun": "阿里云"},
        )

    def ask(self):
        return self.backend().service_access(union_id=UID, service=SERVICE)

    def test_without_holdings_nothing_is_recorded_and_he_still_gets_in(self):
        """**修之前的行为**：`auto_disable` 拿不到 holdings → 人进 skipped、零记录。

        这条不是「期望」，是把洞的形状钉下来：它和「这个人本来就没东西要收」
        长得一模一样，所以上面那条正向用例单独存在时证明力不够。
        """
        self.assertTrue(self.ask().allowed, "前提：他现在确实进得去")
        rep = offboard.auto_disable(self.path, self.cands, self.cloud)
        self.assertEqual(rep["skipped"], ["赵六"])
        self.assertEqual(rep["done"], [])
        self.assertEqual(self.records(), {})
        self.assertEqual(self.calls, [], "没有云账号，云执行体一次都不该被调")
        # 洞的本体：离职流程跑完了，人照样进得去，而且没有任何地方报错
        self.assertTrue(self.ask().allowed)

    def test_with_holdings_records_internal_target_and_blocks_him(self):
        """**修之后**：喂 holdings → 记 `internal/mlflow/<union_id>`，判定侧立刻拒。"""
        self.assertTrue(self.ask().allowed)
        rep = offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        self.assertEqual(rep["skipped"], [])
        self.assertEqual(len(rep["done"]), 1)
        self.assertEqual(self.calls, [], "internal 不该落到云执行体上")

        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, UID)
        rec = self.records()[key]
        self.assertEqual(rec["state"], offboard.DISABLED)
        self.assertEqual(rec["platform"], "internal")
        self.assertEqual(rec["account"], SERVICE)
        # 标识是 **union_id**，不是邮箱：键要稳定（邮箱会变）、且撞不上 `protected()`
        # 那套云前缀正则（见 ProtectedEmailRegressionTests）
        self.assertEqual(rec["user"], UID)
        self.assertEqual(rec["union_id"], UID)
        # 人能认的那部分照样在记录里 —— 卡片靠它们显示是谁
        self.assertEqual(rec["person"], "赵六")
        self.assertEqual(rec["email"], "zhaoliu@wuji.tech")
        self.assertEqual(rec["signal"], "飞书状态：已离职")
        # 停用不动数据、也不动那张单子：撤单是「删号」那一步、要等人确认
        self.assertEqual(rec["keys"], [])
        self.assertFalse(rec["login"])
        self.assertEqual(len(tickets_mod.TicketStore(str(self.tickets_path)).all()), 1)

        # 记上了访问立刻就没了（判定侧每个请求都读这份记录）
        got = self.ask()
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.BLOCKED)
        self.assertEqual(got.ticket_id, "")

    def test_gone_set_is_built_from_that_record(self):
        """把那条记录当 `gone` 用（`Backend.service_access` 就是这么建的）。"""
        offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        gone = {
            str(r.get("union_id") or "")
            for r in self.records().values()
            if r.get("state") in (offboard.DISABLED, offboard.DELETED)
        }
        self.assertEqual(gone, {UID})
        got = sa.allowed(
            union_id=UID,
            service=SERVICE,
            tickets=[ticket()],
            now=NOW,
            offboarded=UID in gone,
        )
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.BLOCKED)

    def test_second_round_does_not_write_a_duplicate(self):
        """幂等：下一轮定时任务再跑，已停用的不再动，也不重复记。"""
        offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        before = self.records()
        rep = offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        self.assertEqual(rep["done"], [])
        self.assertEqual(rep["skipped"], ["赵六"])
        self.assertEqual(self.records(), before)

    def test_restored_service_access_is_not_disabled_again(self):
        """管理员判过「这人没走」之后，下一轮不该再把他的服务访问停掉。"""
        offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, UID)
        with offboard._locked(self.path) as box:  # noqa: SLF001 — 绕开 decide（见「已知缺口」）
            box["records"][key]["state"] = offboard.RESTORED
        rep = offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        self.assertEqual(rep["done"], [])
        self.assertEqual(self.records()[key]["state"], offboard.RESTORED)

    def test_a_failed_round_records_a_suspect_that_does_not_block_and_is_retried(self):
        """服务那条写失败 → 记嫌疑（**嫌疑不挡人**）→ 下一轮成功 → 升级成停用。

        嫌疑不挡人是有意的：飞书对「已离职」和「不在应用可见范围内」返回的是同一个
        结果，拿嫌疑挡人会误伤在职的。所以「没停成」必须表现成「他还进得去」，
        而不是悄悄把人挡在外面。

        （上限那条路不再产生 internal 嫌疑 —— 自建服务不受上限连坐，
        见 `AutoDisableMixedTests`。）
        """

        class Boom:
            def disable_user(self, user):
                raise RuntimeError("写记录时炸了")

        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, UID)
        rep = offboard.auto_disable(
            self.path,
            self.cands,
            lambda _p, _a: Boom(),
            holdings=self.holdings(),
        )
        self.assertEqual([r["platform"] for r in rep["failed"]], ["internal"])
        self.assertEqual(self.records()[key]["state"], offboard.SUSPECT)
        self.assertTrue(self.ask().allowed, "嫌疑不挡人：飞书分不清「离职」和「不可见」")
        self.assertEqual(self.ask().reason, "")

        offboard.auto_disable(self.path, self.cands, self.dispatch, holdings=self.holdings())
        self.assertEqual(self.records()[key]["state"], offboard.DISABLED)
        self.assertFalse(self.ask().allowed)


class ProtectedRulesTests(unittest.TestCase):
    """`protected(user, platform)` 现在分三套：云 / 九章 / 自建服务。

    自建服务那套是**按 union_id 精确匹配的空集合**，不是前缀正则。两个理由：
      · union_id 是 `on_` 开头的随机串，前缀规则对它没有意义 —— 要么挡不住，要么误挡一片；
      · 借用云那套会**真的误挡**：云名单里有 `wuji-`，而公司域名就是 `wuji-tech.com`。
        误挡的后果是「这个人的服务访问永远不会被回收」，且没有任何地方报错。

    下面第一条就是防止有人把 internal 那条分支删掉、退回借用云规则。
    """

    #: 这些串**在云那套下是受保护的**，在 internal 下必须不是
    CLOUD_HITS = (
        "wuji-wang@wuji-tech.com",
        "wuji-wangyuran",
        "staff-x",
        "tempak-x",
        "temp-ak-1",
        "panel-executor",
        "power-application-user",
        "rl-runner",
        "finance",
        "data-tran",
        "PANEL-UPPER",
    )

    def test_internal_protects_nobody_by_default(self):
        """默认空集 = 没有豁免，所有人照常回收。"""
        self.assertEqual(offboard._PROTECTED_SERVICE, frozenset())  # noqa: SLF001
        for user in (*self.CLOUD_HITS, "on_b6aece3f", "", None, "李四", 123):
            with self.subTest(repr(user)):
                self.assertFalse(offboard.protected(user, offboard.SERVICE_PLATFORM), repr(user))

    def test_those_same_names_are_still_protected_on_the_cloud_platforms(self):
        """**加分支时没碰到别人**：云那套一个字没变。"""
        for user in self.CLOUD_HITS:
            with self.subTest(user):
                self.assertTrue(offboard.protected(user, "aliyun"), user)
                self.assertTrue(offboard.protected(user, "volcano"), user)
                self.assertTrue(offboard.protected(user, ""), user)  # 不给平台 = 按云那套

    def test_jiuzhang_still_uses_its_own_narrow_rule(self):
        """九章的登录名全是 `wuji-` 开头，套云那套等于把整个平台挡光。"""
        self.assertFalse(offboard.protected("wuji-wangyuran", "jiuzhang"))
        self.assertTrue(offboard.protected("panel-x", "jiuzhang"))
        self.assertTrue(offboard.protected("power-x", "jiuzhang"))
        self.assertFalse(offboard.protected("staff-x", "jiuzhang"))

    def test_legacy_PROTECTED_match_still_means_the_cloud_rule(self):
        """兼容写法 `PROTECTED.match(name)`（没有平台参数）仍按云那套判。"""
        self.assertTrue(offboard.PROTECTED.match("panel-x"))
        self.assertTrue(offboard.PROTECTED.match("wuji-wang@wuji-tech.com"))
        self.assertFalse(offboard.PROTECTED.match("on_b6aece3f"))
        self.assertFalse(offboard.PROTECTED.match(None))

    def test_a_real_union_id_is_never_protected_anywhere(self):
        for platform in ("internal", "aliyun", "volcano", "jiuzhang", ""):
            with self.subTest(platform):
                self.assertFalse(offboard.protected("on_b6aece3f", platform))


class ProtectedServiceEnvTests(unittest.TestCase):
    """豁免名单从 `DELIVERY_PROTECTED_SERVICE_IDS` 读（逗号或空白分隔的 union_id）。"""

    def setUp(self):
        # 解析结果带 `lru_cache`，缓存键是**那个字符串本身** —— 换了值就是换了键，
        # 所以不需要在用例之间手动清缓存。这条顺带把「键选对了」也验了
        self.addCleanup(offboard._protected_service_ids.cache_clear)  # noqa: SLF001

    def ids(self, raw):
        return offboard._protected_service_ids(raw)  # noqa: SLF001

    def test_unset_means_nobody(self):
        for raw in ("", "   ", ",", " , , ", None):
            with self.subTest(repr(raw)):
                self.assertEqual(self.ids(raw), frozenset())

    def test_comma_and_whitespace_separated(self):
        want = frozenset({"on_a", "on_b", "on_c"})
        for raw in (
            "on_a,on_b,on_c",
            "on_a, on_b, on_c",
            "on_a on_b on_c",
            " on_a,\non_b\ton_c ",
            "on_a,,on_b,,,on_c,",
        ):
            with self.subTest(repr(raw)):
                self.assertEqual(self.ids(raw), want)

    def test_env_takes_effect_only_on_the_internal_platform(self):
        with mock.patch.dict(os.environ, {offboard.ENV_PROTECTED_SERVICE: "on_vip"}):
            self.assertTrue(offboard.protected("on_vip", offboard.SERVICE_PLATFORM))
            self.assertFalse(offboard.protected("on_other", offboard.SERVICE_PLATFORM))
            # 云那两套不受这个变量影响（它判的是登录名，不是 union_id）
            for platform in ("aliyun", "volcano", "jiuzhang", ""):
                self.assertFalse(offboard.protected("on_vip", platform), platform)

    def test_changing_the_env_takes_effect_without_a_cache_clear(self):
        """**缓存键就是那个字符串本身**，所以改了变量下一次调用就生效。

        值得单独锁：`@lru_cache` 摆在那里，很容易让人以为「改了环境变量要先
        `cache_clear()` 才算数」。实测不需要 —— 但如果哪天有人把它改成零参
        （`_protected_service_ids()` 自己去读 `os.environ`），缓存就会把第一次读到的
        名单**永久钉住**：改配置重启前不生效，而且没有任何地方报错。这条就会当场红。
        """
        env = offboard.ENV_PROTECTED_SERVICE
        with mock.patch.dict(os.environ, {env: "on_bot1, on_bot2  on_bot3"}):
            self.assertEqual(
                [offboard.protected(x, "internal") for x in ("on_bot1", "on_bot2", "on_bot3")],
                [True, True, True],
            )
            self.assertFalse(offboard.protected("on_someone", "internal"))
        with mock.patch.dict(os.environ, {env: ""}):
            self.assertFalse(offboard.protected("on_bot1", "internal"), "清空后立刻失效")
        with mock.patch.dict(os.environ, {env: "on_bot1"}):
            self.assertTrue(offboard.protected("on_bot1", "internal"), "再配上又立刻生效")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(offboard.protected("on_bot1", "internal"), "变量整个不存在")

    def test_a_garbled_value_does_not_raise_and_fails_toward_recycling(self):
        """**失效方向**：解析不出来 → 那个人照常被回收（可恢复），不是「谁都不停」。"""
        for raw in ("{不是列表}", "on_a;on_b", "[]", '["on_a"]'):
            with (
                self.subTest(repr(raw)),
                mock.patch.dict(os.environ, {offboard.ENV_PROTECTED_SERVICE: raw}),
            ):
                self.assertFalse(offboard.protected("on_a", offboard.SERVICE_PLATFORM), raw)


class ProtectedEmailRegressionTests(_OffboardBase):
    """**回归锁**：拿邮箱当 internal 的标识 + 借用云那套前缀正则 = 静默漏收。

    公司域名是 `wuji-tech.com`，而云那套名单里有 `wuji-` ——
    `protected("wuji-wang@wuji-tech.com", "aliyun")` 至今仍是 True。
    两处只要退回任意一处（标识改回邮箱，或 internal 分支被删掉），这条就会红。
    """

    EMAIL = "wuji-wang@wuji-tech.com"
    UID = "on_noaccount"

    def test_a_protected_looking_email_still_gets_recycled(self):
        # 前提：这个串在云那套下确实是「受保护」的（不然这条用例就没意义了）
        self.assertTrue(offboard.protected(self.EMAIL, "aliyun"))
        # 但 internal 有自己的规则，它不吃这套
        self.assertFalse(offboard.protected(self.EMAIL, offboard.SERVICE_PLATFORM))

        p = person("王五", self.UID, email=self.EMAIL)
        self.write_people(p)
        rep = offboard.auto_disable(
            self.path,
            [(p, "飞书状态：已离职")],
            self.dispatch,
            holdings={self.UID: [(SERVICE, "REQ-1", 0)]},
        )
        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, self.UID)
        self.assertEqual(
            [offboard.key_of(r["platform"], r["account"], r["user"]) for r in rep["done"]], [key]
        )
        rec = self.records()[key]
        self.assertEqual(rec["state"], offboard.DISABLED)
        self.assertEqual(rec["email"], self.EMAIL, "邮箱还在记录里（卡片要显示人）")
        # 键里不该出现那个会被云规则误判的串
        self.assertFalse(offboard.protected(rec["user"], offboard.SERVICE_PLATFORM))
        self.assertNotIn("@", rec["user"])
        # 判定侧立刻拒
        got = Backend(
            people_path=str(self.people_path),
            tickets_path=str(self.tickets_path),
            platforms={},
        ).service_access(union_id=self.UID, service=SERVICE)
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.BLOCKED)

    def test_every_cloud_prefix_would_have_been_a_silent_hole(self):
        """整份云前缀名单过一遍：真实邮箱撞得上的不止 `wuji-`。"""
        for prefix in ("panel-", "power-", "tempak", "staff-", "temp-ak-", "wuji-", "rl-"):
            with self.subTest(prefix):
                email = f"{prefix}someone@wuji-tech.com"
                self.assertTrue(offboard.protected(email, "aliyun"), email)
                got = offboard.service_targets(
                    person("某人", self.UID, email=email), {self.UID: [(SERVICE, "R", 0)]}
                )
                self.assertEqual(got, [(offboard.SERVICE_PLATFORM, SERVICE, self.UID)])
                self.assertFalse(offboard.protected(got[0][2], offboard.SERVICE_PLATFORM))


class ServiceTargetsTests(unittest.TestCase):
    """`service_targets(person, holdings)` → `[(internal, 服务名, 标识)]`。"""

    def test_identifier_is_union_id_not_email(self):
        """标识是 **union_id**：和判定侧那条「按 union_id 严格相等」对齐。

        邮箱会变（改名、换域名），而 `key_of` 拿它拼键 —— 人改了邮箱之后旧记录的键
        就对不上新目标：重复写一条、旧那条永远清不掉。
        """
        got = offboard.service_targets(person(), {UID: [(SERVICE, "REQ-1", NOW + 5)]})
        self.assertEqual(got, [(offboard.SERVICE_PLATFORM, SERVICE, UID)])
        self.assertNotIn("@", got[0][2])
        # 形状要能直接喂给 key_of（云那套的调用点原封不动）
        self.assertEqual(offboard.key_of(*got[0]), f"internal/mlflow/{UID}")

    def test_key_is_stable_when_the_email_changes(self):
        """改名换邮箱不该让同一个人变出第二条记录。"""
        held = {UID: [(SERVICE, "REQ-1", 0)]}
        before = offboard.service_targets(person(email="li.si@wuji.tech"), held)
        after = offboard.service_targets(person(email="lisi@new-domain.com"), held)
        self.assertEqual(before, after)

    def test_no_union_id_means_no_target(self):
        """holdings 的键是 union_id。没绑 union_id 的人在这份里永远查不到，
        拿别的东西去猜等于按姓名/邮箱认人 —— 那正是会认错人的那条路。"""
        for uid in ("", None, 0):
            with self.subTest(uid):
                p = Person(name="李四", email="li.si@wuji.tech", union_id=uid or "")
                self.assertEqual(offboard.service_targets(p, {UID: [(SERVICE, "R", 0)]}), [])

    def test_no_email_is_fine(self):
        """邮箱根本不参与标识，所以没有邮箱的人照样产出目标（不再有回落分支）。"""
        for email in ("", None):
            with self.subTest(repr(email)):
                p = Person(name="李四", email=email or "", union_id=UID)
                self.assertEqual(
                    offboard.service_targets(p, {UID: [(SERVICE, "R", 0)]}),
                    [(offboard.SERVICE_PLATFORM, SERVICE, UID)],
                )

    def test_person_not_in_holdings(self):
        for held in ({}, {OTHER: [(SERVICE, "R", 0)]}, {UID: []}):
            with self.subTest(repr(held)):
                self.assertEqual(offboard.service_targets(person(), held), [])

    def test_many_services_one_person(self):
        held = {UID: [(SERVICE, "R1", 0), (OTHER_SERVICE, "R2", NOW), ("jupyter", "R3", 0)]}
        self.assertEqual(
            offboard.service_targets(person(), held),
            [
                (offboard.SERVICE_PLATFORM, SERVICE, UID),
                (offboard.SERVICE_PLATFORM, OTHER_SERVICE, UID),
                (offboard.SERVICE_PLATFORM, "jupyter", UID),
            ],
        )

    def test_platform_constants(self):
        """`internal` 进了 `ALL_PLATFORMS`，且**不是** MANUAL —— 它能自动处理。"""
        self.assertEqual(offboard.SERVICE_PLATFORM, "internal")
        self.assertEqual(offboard.ALL_PLATFORMS, ("aliyun", "volcano", "jiuzhang", "internal"))
        self.assertFalse(offboard.manual(offboard.SERVICE_PLATFORM))
        self.assertNotIn(offboard.SERVICE_PLATFORM, offboard.PLATFORMS)
        self.assertNotIn(offboard.SERVICE_PLATFORM, offboard.MANUAL_PLATFORMS)

    def test_targets_of_only_sees_roster_accounts(self):
        """名册里没有 internal 账号这种东西，所以云那一路照旧只出云账号。

        **当前行为**：`internal` 进了 `ALL_PLATFORMS`，所以名册里要真有一条
        `platform=internal` 的 AccountRef，`targets_of` 会把它一起返出来 ——
        那时它会和 `service_targets` 的同名目标重合（键都是
        `internal/<服务>/<标识>`，第二条被 `open_` 滤掉，不会写两条）。
        名册目前不产生这种行，这里只是把行为记下来。
        """
        p = person("李四", UID, ref("aliyun", "lisi"))
        self.assertEqual(offboard.targets_of(p), [("aliyun", ALI_ACC, "lisi")])
        faked = person("李四", UID, AccountRef("internal", SERVICE, UID))
        self.assertEqual(offboard.targets_of(faked), [("internal", SERVICE, UID)])


class ServiceAccessExecutorTests(unittest.TestCase):
    """`ServiceAccess`：停用不调任何接口 —— **记录本身就是生效的那个东西**。"""

    def test_disable_returns_empty_and_does_nothing(self):
        got = offboard.ServiceAccess().disable_user("li.si@wuji.tech")
        self.assertEqual(got, {})

    def test_disable_never_raises_on_any_identifier(self):
        """标识来自邮箱/union_id，形状不由这里保证；抛了会被记成 failed、下一轮重试，
        而重试永远不会好 —— 那条记录就永远写不下去。"""
        ex = offboard.ServiceAccess()
        for user in ("", None, "李四", "a" * 300, "x@y", 123, ["列表"]):
            with self.subTest(repr(user)):
                self.assertEqual(ex.disable_user(user), {})

    def test_delete_without_a_revoke_hook_reports_leftovers_not_success(self):
        """**方向**：没接上撤销能力时 `delete_user` 必须回「还有没撤掉的」。

        回空列表等于告诉 `decide()`「删干净了」→ 记录被置成 DELETED、卡片从待办里
        消失，**而那张单还开着、人照样进得去**。「不知道删没删」必须当「没删」。
        """
        left = offboard.ServiceAccess().delete_user(UID)
        self.assertTrue(left, "空列表 = 谎报成功")
        self.assertTrue(all(isinstance(x, str) and x for x in left), left)
        self.assertIn("人工", "".join(left))

    def test_delete_calls_the_injected_revoke_and_passes_its_leftovers_through(self):
        seen = []

        def revoke(uid, service):
            seen.append((uid, service))
            return ["REQ-1：撤不掉"]

        self.assertEqual(offboard.ServiceAccess(revoke).delete_user(UID), ["REQ-1：撤不掉"])
        self.assertEqual(seen, [(UID, "")])

    def test_delete_is_clean_when_revoke_returns_nothing(self):
        for empty in ([], (), None):
            with self.subTest(repr(empty)):
                ex = offboard.ServiceAccess(lambda _u, _s, out=empty: out)
                self.assertEqual(ex.delete_user(UID), [])

    def test_enable_accepts_the_same_call_shape_as_the_cloud_executors(self):
        """`decide()` 恢复时是 `enable_user(user, login=…, keys=…)` 这么调的。"""
        ex = offboard.ServiceAccess()
        self.assertEqual(ex.enable_user(UID, login=True, keys=["AK-x"]), {})
        self.assertEqual(ex.enable_user(UID), {})

    def test_result_is_not_gone_and_has_no_login_or_keys(self):
        """`{}` 的三个含义都要对：没被删（不是 `gone`）、没关登录、没禁 AK ——
        否则记录会被写成 `deleted` 或带上不存在的 AK。"""
        got = offboard.ServiceAccess().disable_user("x")
        self.assertFalse(got.get("gone"))
        self.assertFalse(got.get("login"))
        self.assertEqual(got.get("keys") or [], [])


class AutoDisableMixedTests(_OffboardBase):
    """云账号 + 自建服务混在一个人身上，以及上限怎么算。"""

    def cand(self, n, *refs, services=()):
        p = person(f"人{n}", f"on_{n}", *refs, email=f"u{n}@wuji.tech")
        return p, {f"on_{n}": [(s, f"R-{n}-{s}", 0) for s in services]}

    def test_cloud_and_service_both_land_in_done(self):
        p, held = self.cand(1, ref("aliyun", "u1"), services=(SERVICE,))
        rep = offboard.auto_disable(self.path, [(p, "离职")], self.dispatch, holdings=held)
        self.assertEqual(
            sorted((r["platform"], r["user"]) for r in rep["done"]),
            [("aliyun", "u1"), ("internal", "on_1")],
        )
        self.assertEqual(self.calls, [("aliyun", ALI_ACC)], "云那一路照常走云执行体")
        recs = self.records()
        self.assertEqual(set(recs), {f"aliyun/{ALI_ACC}/u1", f"internal/{SERVICE}/on_1"})
        # 云那条记下了要恢复用的东西，服务那条没有可恢复的东西
        self.assertEqual(recs[f"aliyun/{ALI_ACC}/u1"]["keys"], ["AK-u1"])
        self.assertEqual(recs[f"internal/{SERVICE}/on_1"]["keys"], [])

    def test_service_target_never_reaches_the_cloud_executor(self):
        """只有服务、没有云账号的人：云执行体一次都不该被构造。

        用会抛的假执行体证明 —— 注意 `auto_disable` 会把异常吞进 `failed`，
        所以除了「没被调」还要断言 `failed` 是空的（被调了就会留痕）。
        """
        p, held = self.cand(2, services=(SERVICE, OTHER_SERVICE))

        def only_service(platform, account):
            if platform != offboard.SERVICE_PLATFORM:
                return self.cloud(platform, account)
            return offboard.ServiceAccess()

        rep = offboard.auto_disable(self.path, [(p, "离职")], only_service, holdings=held)
        self.assertEqual(self.calls, [])
        self.assertEqual(rep["failed"], [])
        self.assertEqual(len(rep["done"]), 2)

    def test_service_failure_does_not_block_the_cloud_account(self):
        """真要有一条服务记录写失败，云那个号还是得停 —— 反过来也一样。"""

        class Boom:
            def disable_user(self, user):
                raise RuntimeError("服务侧炸了")

        p, held = self.cand(3, ref("aliyun", "u3"), services=(SERVICE,))
        rep = offboard.auto_disable(
            self.path,
            [(p, "离职")],
            lambda pf, acc: (
                Boom() if pf == offboard.SERVICE_PLATFORM else self.working_cloud(pf, acc)
            ),
            holdings=held,
        )
        self.assertEqual([r["platform"] for r in rep["done"]], ["aliyun"])
        self.assertEqual([r["platform"] for r in rep["failed"]], ["internal"])
        # 失败的那条记成嫌疑（下一轮会再试），而嫌疑不挡人
        self.assertEqual(self.records()[f"internal/{SERVICE}/on_3"]["state"], offboard.SUSPECT)

    def test_an_exempt_identity_is_not_recycled_at_all(self):
        """配了豁免 → 这个人的 internal 目标根本不进 todo（和云那一路在
        `targets_of` 里做的是同一件事）。

        **半接线比没接更糟**：只有 `decide()` 认豁免的话，人照样被停，
        管理员反而动不了那条记录。
        """
        p = person("王五", UID, email="w@wuji.tech")
        held = {UID: [(SERVICE, "REQ-1", 0)]}
        env = offboard.ENV_PROTECTED_SERVICE
        with mock.patch.dict(os.environ, {env: UID}):
            self.assertTrue(offboard.protected(UID, offboard.SERVICE_PLATFORM), "前提")
            self.assertEqual(offboard.service_targets(p, held), [])
            rep = offboard.auto_disable(self.path, [(p, "离职")], self.dispatch, holdings=held)
        self.assertEqual(rep["done"], [])
        self.assertEqual(self.records(), {})
        # 没配豁免时照收（对照，否则上面可能只是「压根没跑起来」）
        rep = offboard.auto_disable(self.path, [(p, "离职")], self.dispatch, holdings=held)
        self.assertEqual([r["user"] for r in rep["done"]], [UID])

    def test_someone_elses_exemption_does_not_protect_him(self):
        p = person("王五", UID, email="w@wuji.tech")
        held = {UID: [(SERVICE, "REQ-1", 0)]}
        with mock.patch.dict(os.environ, {offboard.ENV_PROTECTED_SERVICE: "on_someone_else"}):
            rep = offboard.auto_disable(self.path, [(p, "离职")], self.dispatch, holdings=held)
        self.assertEqual([r["user"] for r in rep["done"]], [UID])

    def test_revoked_expired_and_dirty_tickets_produce_no_internal_target(self):
        """**不该给一个早就没权限的人写离职记录**：那条记录会一直挂在管理员的待办上，
        而他手上根本没有东西要收。判据统一在 holdings 这一步就已经滤掉了。"""
        rows = [
            ticket(tid="a", status=tickets_mod.REVOKED),
            ticket(tid="b", expires=NOW - 1),
            ticket(tid="c", expires=NOW),
            ticket(tid="d", status=tickets_mod.PENDING),
            ticket(tid="e", kind="permission"),
            ticket(tid="f", expires="2026-10-01"),
            ticket(tid="g", template=None),
            ticket(tid="h", applicant=None),
            None,
        ]
        held = sa.holdings(rows, NOW)
        self.assertEqual(held, {})
        p = person("赵六", UID, email="zhaoliu@wuji.tech")
        rep = offboard.auto_disable(self.path, [(p, "离职")], self.cloud, holdings=held)
        self.assertEqual(rep["done"], [])
        self.assertEqual(rep["skipped"], ["赵六"])
        self.assertEqual(self.records(), {})

    def test_holdings_default_is_none_and_empty(self):
        """不传 / 传 None / 传 {} 三者一致（旧调用点一个字不改也不该崩）。"""
        p = person("赵六", UID, ref("aliyun", "u9"), email="zhaoliu@wuji.tech")
        for held in (..., None, {}):
            with self.subTest(repr(held)):
                self.setUp()
                kw = {} if held is ... else {"holdings": held}
                rep = offboard.auto_disable(self.path, [(p, "离职")], self.working_cloud, **kw)
                self.assertEqual([r["platform"] for r in rep["done"]], ["aliyun"])

    # ── 上限 ──

    def test_a_service_only_person_does_not_use_up_the_quota(self):
        """**上限只数「名下有云账号」的人。**

        这个上限的理由是「IT 接口或飞书抖一下可能批量误判，那时停得越多事故越大」，
        而"越大"指的是云上那些动作：关登录、摘 AK 会当场打断在跑的任务。
        停一个自建服务只是写一条记录，随时能恢复、也不打断任何东西。

        不这么分的话，**加了 MLflow 反而让云账号的回收更容易卡住** ——
        3 个有云账号的人 + 1 个只有 MLflow 的人 = 4 > 3 → 一个都不停。
        """
        cands = [(self.cand(i, ref("aliyun", f"u{i}"))[0], "离职") for i in range(3)]
        p, held = self.cand(9, services=(SERVICE,))
        rep = offboard.auto_disable(self.path, [*cands, (p, "离职")], self.dispatch, holdings=held)
        self.assertEqual(rep["held"], [], "那 3 个云账号不该被第 4 个人顶成 held")
        self.assertEqual(
            sorted((r["platform"], r["user"]) for r in rep["done"]),
            [("aliyun", "u0"), ("aliyun", "u1"), ("aliyun", "u2"), ("internal", "on_9")],
        )
        self.assertTrue(all(r["state"] == offboard.DISABLED for r in self.records().values()))

    def test_over_the_limit_holds_the_cloud_accounts_but_not_the_service(self):
        """超限时**自建服务不被连坐**：它既然不占额度，就不该受这个上限影响 ——
        否则同一个人的结果取决于「那一轮还有谁离职」，而他自己什么都没变。"""
        n = offboard.MAX_AUTO_PEOPLE + 1
        cands = [(self.cand(i, ref("aliyun", f"u{i}"))[0], "离职") for i in range(n)]
        p, held = self.cand(9, services=(SERVICE,))
        rep = offboard.auto_disable(self.path, [*cands, (p, "离职")], self.dispatch, holdings=held)
        self.assertEqual(rep["held"], [f"人{i}" for i in range(n)])
        self.assertEqual([(r["platform"], r["user"]) for r in rep["done"]], [("internal", "on_9")])
        self.assertEqual(self.calls, [], "云上一个接口都没打")
        recs = self.records()
        self.assertTrue(
            all(r["state"] == offboard.SUSPECT for k, r in recs.items() if k.startswith("aliyun/"))
        )
        self.assertEqual(recs[f"internal/{SERVICE}/on_9"]["state"], offboard.DISABLED)

    def test_a_persons_own_cloud_accounts_still_hold_his_service_along_normally(self):
        """同一个人云 + 服务都有、而且这一轮超限：云那几个记嫌疑，服务那条照停。

        这是上面那条的另一半 —— 「不连坐」是按目标分的，不是按人分的。
        """
        n = offboard.MAX_AUTO_PEOPLE + 1
        cands, held = [], {}
        for i in range(n):
            person_i, h = self.cand(i, ref("aliyun", f"u{i}"), services=(SERVICE,))
            cands.append((person_i, "离职"))
            held.update(h)
        rep = offboard.auto_disable(self.path, cands, self.dispatch, holdings=held)
        self.assertEqual(len(rep["held"]), n)
        self.assertEqual(
            sorted(r["user"] for r in rep["done"]), sorted(f"on_{i}" for i in range(n))
        )
        self.assertEqual(self.calls, [])
        recs = self.records()
        self.assertEqual(len([k for k, r in recs.items() if r["state"] == offboard.SUSPECT]), n)
        self.assertEqual(len([k for k, r in recs.items() if r["state"] == offboard.DISABLED]), n)

    def test_many_services_on_one_person_still_counts_as_one(self):
        """一个人五个服务算一个人。按「目标数」算的话，一个人就能把整轮拦下来。"""
        p, held = self.cand(1, services=("a", "b", "c", "d", "e"))
        others = [(self.cand(i, ref("aliyun", f"u{i}"))[0], "离职") for i in (7, 8)]
        rep = offboard.auto_disable(self.path, [(p, "离职"), *others], self.dispatch, holdings=held)
        self.assertEqual(rep["held"], [])
        self.assertEqual(len(rep["done"]), 7)

    def test_person_with_both_counts_once(self):
        cands, held = [], {}
        for i in range(offboard.MAX_AUTO_PEOPLE):
            p, h = self.cand(i, ref("aliyun", f"u{i}"), services=(SERVICE,))
            cands.append((p, "离职"))
            held.update(h)
        rep = offboard.auto_disable(self.path, cands, self.dispatch, holdings=held)
        self.assertEqual(rep["held"], [])
        self.assertEqual(len(rep["done"]), 2 * offboard.MAX_AUTO_PEOPLE)


# ── 面板展示 ──────────────────────────────────────────────────────────────


class BackendServiceHoldingsTests(unittest.TestCase):
    """`Backend.service_holdings(union_id)`。**这是展示路径**：读不了就回空，
    不能让「我的账号」页整个打不开 —— 和网关那条 fail-closed 的判定路刻意相反。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.tickets_path = self.root / "tickets.json"
        #: `Backend.service_holdings` 用的是**真实时钟**，所以到期时间得是真的未来
        self.soon = time.time() + 86400
        self.write(ticket(expires=self.soon))

    def write(self, *rows, schema: object = tickets_mod.SCHEMA):
        payload = {"schema": schema, "tickets": list(rows)}
        if schema is None:
            payload.pop("schema")
        self.tickets_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def backend(self, *, tickets=True):
        return Backend(tickets_path=str(self.tickets_path) if tickets else None, platforms={})

    def test_returns_dicts_with_service_ticket_and_expiry(self):
        got = self.backend().service_holdings(UID)
        self.assertEqual(got, [{"service": SERVICE, "ticket": "REQ-1", "expires_at_ts": self.soon}])
        # 字段名带 `_ts` 是有意的：叫 `expires_at` 的话前端会拿它喂 `fmtTime(iso)`
        # （`new Date(串)`），epoch 秒被当毫秒，2030 年的到期渲染成 1970 年
        self.assertNotIn("expires_at", got[0])

    def test_empty_union_id_is_empty_not_everyone(self):
        """空 union_id 回空 —— 拿空串当通配会把别人的清单显示给他。"""
        for uid in ("", None, 0):
            with self.subTest(repr(uid)):
                self.assertEqual(self.backend().service_holdings(uid), [])

    def test_unconfigured_ticket_store_is_empty_not_an_error(self):
        self.assertEqual(self.backend(tickets=False).service_holdings(UID), [])

    def test_broken_ledger_is_empty_not_an_error(self):
        """展示路径 fail-open：台账坏了整页还得打得开。"""
        for bad in ("{ 坏的", "[]", json.dumps({"tickets": []})):
            with self.subTest(bad[:8]):
                self.tickets_path.write_text(bad, encoding="utf-8")
                self.assertEqual(self.backend().service_holdings(UID), [])

    def test_missing_file_is_empty(self):
        self.tickets_path.unlink()
        self.assertEqual(self.backend().service_holdings(UID), [])

    def test_stranger_and_other_people_are_not_leaked(self):
        self.write(ticket(tid="mine"), ticket(tid="his", union_id=OTHER))
        self.assertEqual([r["ticket"] for r in self.backend().service_holdings(UID)], ["mine"])
        self.assertEqual(self.backend().service_holdings("on_nobody"), [])

    def test_agrees_with_the_gateway_on_the_same_ledger(self):
        """页面说有、网关说没有 = 最难查的那种不一致。同一份台账两边必须一致。"""
        rows = [
            ticket(tid="ok", expires=self.soon),
            ticket(tid="revoked", status=tickets_mod.REVOKED, service=OTHER_SERVICE),
            ticket(tid="expired", expires=time.time() - 1, service="jupyter"),
            ticket(tid="his", union_id=OTHER, service="grafana"),
        ]
        self.write(*rows)
        now = time.time()
        shown = {r["service"] for r in self.backend().service_holdings(UID)}
        gate = {
            svc
            for svc in (SERVICE, OTHER_SERVICE, "jupyter", "grafana")
            if sa.allowed(union_id=UID, service=svc, tickets=rows, now=now).allowed
        }
        self.assertEqual(shown, gate)
        self.assertEqual(shown, {SERVICE})

    def test_many_services_are_all_listed(self):
        self.write(
            ticket(tid="a", service=SERVICE),
            ticket(tid="b", service=OTHER_SERVICE, expires=self.soon),
        )
        got = self.backend().service_holdings(UID)
        self.assertEqual(
            sorted((r["service"], r["ticket"], r["expires_at_ts"]) for r in got),
            [(SERVICE, "a", 0.0), (OTHER_SERVICE, "b", self.soon)],
        )

    def test_an_expiry_that_passes_without_a_write_stops_being_reported(self):
        """**回归**：`now` 不许被冻进缓存。

        缓存只按文件 mtime 失效、没有 TTL，而台账不是每天都写 —— 把 `time.time()`
        求值在缓存的 lambda 里面，一张昨天缓存过的单到期之后就会变成：网关 `allowed()`
        拒（它用新的 now），而这一页仍然报「有」。**管理员的离职确认就是看着这一页点的。**
        """
        from delivery import server as server_mod

        backend = self.backend()
        self.assertTrue(backend.service_holdings(UID), "前提：现在是有的")
        with mock.patch.object(server_mod.time, "time", return_value=self.soon + 1):
            self.assertEqual(backend.service_holdings(UID), [], "到点了就该没有")
        self.assertTrue(backend.service_holdings(UID), "时钟回来之后又有了（证明缓存没被清）")

    def test_revoke_takes_effect_on_the_next_read(self):
        """缓存不能把撤销压住：撤销的唯一场景就是「现在就要看到他没有了」。"""
        backend = self.backend()
        self.assertTrue(backend.service_holdings(UID))
        self.write(ticket(status=tickets_mod.REVOKED))
        self.assertEqual(backend.service_holdings(UID), [])


# ── CLI 接线 ──────────────────────────────────────────────────────────────


class OffboardExecutorDispatchTests(unittest.TestCase):
    """`cli._offboard_executor`：internal 走 `ServiceAccess`，其余走云。"""

    def test_internal_returns_service_access_without_touching_env(self):
        with mock.patch.object(
            provision, "executor_from_env", side_effect=AssertionError("不该调")
        ):
            got = cli_mod._offboard_executor(offboard.SERVICE_PLATFORM, SERVICE)
        self.assertIsInstance(got, offboard.ServiceAccess)

    def test_cloud_platforms_go_to_executor_from_env(self):
        seen = []

        def fake(platform, account):
            seen.append((platform, account))
            return "云执行体"

        with mock.patch.object(provision, "executor_from_env", fake):
            for platform, account in (("aliyun", ALI_ACC), ("volcano", "2000000001")):
                self.assertEqual(cli_mod._offboard_executor(platform, account), "云执行体")
        self.assertEqual(seen, [("aliyun", ALI_ACC), ("volcano", "2000000001")])

    def test_unknown_platform_still_goes_to_the_cloud_branch(self):
        """只有 `internal` 短路。别的一律交给云那边去报「不支持的平台」，
        免得这里多一层各自的判断、两处对平台名的理解慢慢分叉。"""
        with self.assertRaises(provision.ProvisionError):
            cli_mod._offboard_executor("jiuzhang", "x")

    def test_dispatch_is_by_the_constant_not_a_literal(self):
        self.assertIsInstance(
            cli_mod._offboard_executor(offboard.SERVICE_PLATFORM, ""), offboard.ServiceAccess
        )


class ServerOffboardExecutorTests(unittest.TestCase):
    """`server._offboard_executor(backend)` —— **飞书卡片和网页面板共用的那个工厂**。

    分两处各写一次的代价已经发生过一次：只改了网页那条，飞书卡片那条仍是
    `executor_from_env`，对 `internal` 直接抛「不支持的平台」—— 而卡片上那行
    「自建服务 `mlflow`」的两个按钮唯一的落点就在那里。
    """

    def backend(self, left=()):
        calls = []

        class FakeBackend:
            def revoke_service_grants(self, union_id, service):
                calls.append((union_id, service))
                return list(left)

        return FakeBackend(), calls

    def test_internal_gets_a_service_access_wired_to_revoke(self):
        from delivery import server as server_mod

        backend, calls = self.backend()
        make = server_mod._offboard_executor(backend)  # noqa: SLF001
        ex = make(offboard.SERVICE_PLATFORM, SERVICE)
        self.assertIsInstance(ex, offboard.ServiceAccess)
        self.assertEqual(ex.delete_user(UID), [])
        # **服务名来自那条记录的 account**，不是 delete_user 的入参
        self.assertEqual(calls, [(UID, SERVICE)])

    def test_leftovers_are_passed_through(self):
        from delivery import server as server_mod

        backend, _calls = self.backend(left=["REQ-1：撤不掉"])
        ex = server_mod._offboard_executor(backend)(offboard.SERVICE_PLATFORM, SERVICE)  # noqa: SLF001
        self.assertEqual(ex.delete_user(UID), ["REQ-1：撤不掉"])

    def test_cloud_platforms_still_go_to_executor_from_env(self):
        from delivery import server as server_mod

        backend, calls = self.backend()
        seen = []
        with mock.patch.object(
            server_mod, "executor_from_env", lambda p, a: seen.append((p, a)) or "云执行体"
        ):
            make = server_mod._offboard_executor(backend)  # noqa: SLF001
            self.assertEqual(make("aliyun", "1234"), "云执行体")
        self.assertEqual(seen, [("aliyun", "1234")])
        self.assertEqual(calls, [], "云那一路不该碰撤单")

    def test_every_decide_call_site_uses_the_shared_factory(self):
        """**结构锁**：`offboard.decide(...)` 的每一处都得用这个工厂。

        漏掉一处的表现是「那个入口点『确认收回』报不支持的平台」，而另一个入口好好的 ——
        两个入口各测各的时候，谁都不会发现只改了一半。
        """
        import re

        src = Path(server_mod_file()).read_text(encoding="utf-8")
        sites = [m.start() for m in re.finditer(r"offboard_mod\.decide\(", src)]
        self.assertTrue(sites, "没找到 decide 的调用点，正则多半失配了")
        self.assertIn(
            "_executor = _offboard_executor(backend)", src, "那个局部变量得是这个工厂给的"
        )
        for pos in sites:
            window = src[pos : pos + 300]
            self.assertIn("_executor", window, src[pos : pos + 200])
            self.assertNotIn(
                "executor_from_env", window, "别把裸的云工厂直接喂给 decide（internal 会抛）"
            )


def server_mod_file() -> str:
    from delivery import server as server_mod

    return server_mod.__file__


class ServiceHoldingsCliTests(unittest.TestCase):
    """`cli._service_holdings`：**读不了 → 当作没有**（和别处 fail-closed 刻意相反）。

    方向的理由：fail-closed 会去写一条离职记录，而那条记录立刻断人访问；
    晚一轮收没关系，凭一次读盘失败断人不行。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root / "tickets.json"
        self.write(ticket())

    def write(self, *rows, schema: object = tickets_mod.SCHEMA):
        payload = {"schema": schema, "tickets": list(rows)}
        if schema is None:
            payload.pop("schema")
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    _DEFAULT = object()

    def call(self, path=_DEFAULT):
        return cli_mod._service_holdings(str(self.path) if path is self._DEFAULT else path)

    def test_reads_holdings(self):
        got = self.call()
        self.assertEqual(list(got), [UID])
        self.assertEqual([svc for svc, _tid, _exp in got[UID]], [SERVICE])

    def test_unreadable_ledger_yields_no_internal_targets(self):
        """**方向**：读不了 → `{}` → `service_targets` 返空 → 这一轮不写任何 internal 记录。

        这一条和「读不了就拒」的那一侧是两个相反的方向，都是有意的，别顺手统一。
        """
        for bad in ("{ 坏的", "[]", json.dumps({"tickets": []}), json.dumps({"schema": "x"})):
            with self.subTest(bad[:10]):
                self.path.write_text(bad, encoding="utf-8")
                self.assertEqual(self.call(), {})
                # 落到离职那一步：没有 internal 目标
                self.assertEqual(offboard.service_targets(person(), self.call()), [])

    def test_missing_path_and_empty_path(self):
        """路径没配（空串 / None，`getattr(args, "tickets", "")` 两种都出得来）→ 不收。"""
        self.assertEqual(self.call(""), {})
        self.assertEqual(self.call(None), {})
        self.path.unlink()
        self.assertEqual(self.call(), {})

    def test_a_directory_instead_of_a_file_does_not_raise(self):
        """路径配错（指到目录）也只是这一轮不收，不能把整个 iam-remind 带崩 ——
        云账号那一路还得跑完。"""
        self.assertEqual(self.call(str(self.root)), {})

    def test_failure_is_reported_on_stderr(self):
        """静默 fail-open 就等于这条路永远不生效也没人知道，所以要留一行。"""
        self.path.write_text("{ 坏的", encoding="utf-8")
        err = _capture_stderr(self.call)
        self.assertIn("申请单台账", err)

    def test_expired_grants_are_already_filtered(self):
        """用的是真实时钟：早就过期的单子不该产生离职目标。"""
        self.write(ticket(expires=1.0))
        self.assertEqual(self.call(), {})


def _capture_stderr(fn):
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        fn()
    return buf.getvalue()


class RemindParserTests(unittest.TestCase):
    """`iam-remind --tickets`：默认值必须有 —— 定时任务的命令行是运维写死的，
    加了参数却要求显式传的话，线上那条命令会继续用旧写法、这条路等于没接上。"""

    def parse(self, *argv):
        return cli_mod.build_parser().parse_args(["identity", "iam-remind", *argv])

    def test_default_points_at_the_ledger(self):
        self.assertEqual(self.parse().tickets, "identity/tickets.json")

    def test_can_be_overridden(self):
        self.assertEqual(self.parse("--tickets", "/tmp/t.json").tickets, "/tmp/t.json")

    def test_empty_value_is_accepted_and_means_skip(self):
        """显式传空 = 这一轮不收自建服务（应急开关），不该报错。"""
        self.assertEqual(self.parse("--tickets", "").tickets, "")
        self.assertEqual(cli_mod._service_holdings(""), {})


# ── 管理员拿主意：确认收回 / 没离职 ──────────────────────────────────────


class DecideServiceTests(_OffboardBase):
    """管理员在卡片/面板上拿主意：「确认收回」= 撤那张单，「没离职」= 放回去。

    **停用和撤单是两件事**：停用只是离职记录里记一笔（判定侧读到就拒），
    撤单是把单子推成 REVOKED —— 即使有人把离职记录删掉，访问也回不来。
    """

    KEY = f"internal/{SERVICE}/{UID}"

    def setUp(self):
        super().setUp()
        self.p = person("赵六", UID, email="zhaoliu@wuji.tech")
        self.write_people(self.p)
        self.revoked = []
        offboard.auto_disable(
            self.path,
            [(self.p, "飞书状态：已离职")],
            self.dispatch,
            holdings={UID: [(SERVICE, "REQ-1", 0)]},
        )
        self.assertEqual(self.records()[self.KEY]["state"], offboard.DISABLED)

    def revoking(self, *, left=()):
        """注入了撤销能力的执行体（服务端 `_executor` 的 internal 分支就是这么注的）。"""

        def revoke(uid, service):
            self.revoked.append((uid, service))
            return list(left)

        def factory(platform, account):
            if platform == offboard.SERVICE_PLATFORM:
                return offboard.ServiceAccess(revoke)
            return self.working_cloud(platform, account)

        return factory

    def ask(self):
        return Backend(
            people_path=str(self.people_path),
            tickets_path=str(self.tickets_path),
            platforms={},
        ).service_access(union_id=UID, service=SERVICE)

    def test_delete_revokes_the_grant_and_marks_the_record_deleted(self):
        rec = offboard.decide(self.path, self.KEY, "delete", self.revoking(), actor="admin:x")
        self.assertEqual(rec["state"], offboard.DELETED)
        self.assertEqual(rec["decided_by"], "admin:x")
        self.assertEqual(self.revoked, [(UID, "")])
        self.assertEqual(self.records()[self.KEY]["state"], offboard.DELETED)
        self.assertNotIn("left", self.records()[self.KEY])
        self.assertEqual(offboard.pending(self.path), [], "处理完了就不该还挂在待办上")
        self.assertEqual(self.ask().reason, sa.BLOCKED)

    def test_delete_that_did_not_finish_raises_and_keeps_the_record_open(self):
        """撤不掉就**别报成功**：记录留在待办上、带上没撤掉的是什么。"""
        with self.assertRaises(offboard.OffboardError) as box:
            offboard.decide(
                self.path, self.KEY, "delete", self.revoking(left=["REQ-1：撤不掉"]), actor="a"
            )
        self.assertIn("没删干净", str(box.exception))
        self.assertIn("REQ-1", str(box.exception))
        rec = self.records()[self.KEY]
        self.assertEqual(rec["state"], offboard.DISABLED, "状态不许被推成已删除")
        self.assertEqual(rec["left"], ["REQ-1：撤不掉"])
        self.assertIn("tried_at", rec)
        self.assertEqual(len(offboard.pending(self.path)), 1)

    def test_delete_without_a_revoke_hook_does_not_become_deleted(self):
        """**方向锁**：面板没接上撤销能力时，这条不能变成 DELETED。

        变成 DELETED 的话卡片从待办里消失、管理员以为办完了，而那张单还开着。
        """
        with self.assertRaises(offboard.OffboardError) as box:
            offboard.decide(self.path, self.KEY, "delete", self.dispatch, actor="a")
        self.assertIn("没删干净", str(box.exception))
        rec = self.records()[self.KEY]
        self.assertNotEqual(rec["state"], offboard.DELETED)
        self.assertTrue(rec["left"])
        self.assertEqual(len(offboard.pending(self.path)), 1, "还挂在待办上，下次还能点")

    def test_restore_puts_the_access_back(self):
        """判错了（冻结/不可见被当成离职）要能一键放回去 —— 记录一置回，`gone` 里就没他了。"""
        self.assertEqual(self.ask().reason, sa.BLOCKED)
        rec = offboard.decide(self.path, self.KEY, "restore", self.revoking(), actor="admin:x")
        self.assertEqual(rec["state"], offboard.RESTORED)
        self.assertEqual(self.revoked, [], "恢复不该去撤单")
        self.assertTrue(self.ask().allowed, "恢复之后立刻能进")
        self.assertEqual(offboard.pending(self.path), [])

    def test_restore_of_a_suspect_record_is_dismissed_not_restored(self):
        with offboard._locked(self.path) as box:  # noqa: SLF001
            box["records"][self.KEY]["state"] = offboard.SUSPECT
        rec = offboard.decide(self.path, self.KEY, "restore", self.revoking(), actor="a")
        self.assertEqual(rec["state"], offboard.DISMISSED)
        self.assertTrue(self.ask().allowed)

    def test_deciding_twice_is_refused(self):
        offboard.decide(self.path, self.KEY, "delete", self.revoking(), actor="a")
        with self.assertRaises(offboard.OffboardError):
            offboard.decide(self.path, self.KEY, "delete", self.revoking(), actor="a")
        with self.assertRaises(offboard.OffboardError):
            offboard.decide(self.path, self.KEY, "restore", self.revoking(), actor="a")

    def test_unknown_key_is_refused_without_calling_revoke(self):
        """撤单只认记录里有的那条：构造一个请求撤任意人的单不行。"""
        for key in (f"internal/{SERVICE}/on_someone", "internal//x", "", f"aliyun/1/{UID}"):
            with self.subTest(key), self.assertRaises(offboard.OffboardError):
                offboard.decide(self.path, key, "delete", self.revoking(), actor="a")
        self.assertEqual(self.revoked, [])

    def test_bad_action_is_refused(self):
        for action in ("revoke", "", "DELETE", None):
            with self.subTest(repr(action)), self.assertRaises(offboard.OffboardError):
                offboard.decide(self.path, self.KEY, action, self.revoking(), actor="a")
        self.assertEqual(self.revoked, [])

    def test_the_audit_log_records_who_did_it(self):
        logged = []
        offboard.decide(
            self.path,
            self.KEY,
            "delete",
            self.revoking(),
            actor="admin:x",
            log=lambda *a: logged.append(a),
        )
        self.assertEqual(
            [(op, actor) for op, _rows, actor in logged], [("offboard_delete", "admin:x")]
        )


class RevokeServiceGrantsTests(unittest.TestCase):
    """`Backend.revoke_service_grants(union_id, service)` —— 按 holdings 找有效单逐张撤。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.tickets_path = self.root / "tickets.json"
        self.write(
            ticket(tid="R-mlflow", service=SERVICE),
            ticket(tid="R-tb", service=OTHER_SERVICE),
            ticket(tid="R-other", service=SERVICE, union_id=OTHER),
            ticket(tid="R-revoked", service="jupyter", status=tickets_mod.REVOKED),
        )
        self.calls = []

    def write(self, *rows):
        self.tickets_path.write_text(
            json.dumps({"schema": tickets_mod.SCHEMA, "tickets": list(rows)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def backend(self, *, tickets=True):
        return Backend(tickets_path=str(self.tickets_path) if tickets else None, platforms={})

    def flows(self, *, boom=()):
        """假 `Flows`：记下撤了哪几张，并把单子真的改成 REVOKED（好验判定侧）。"""
        outer = self

        class FakeFlows:
            def revoke_now(self, ticket_id, *, actor):
                outer.calls.append((ticket_id, actor))
                if ticket_id in boom:
                    raise RuntimeError(f"{ticket_id} 撤不掉")
                data = json.loads(outer.tickets_path.read_text(encoding="utf-8"))
                for row in data["tickets"]:
                    if row["id"] == ticket_id:
                        row["status"] = tickets_mod.REVOKED
                outer.tickets_path.write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8"
                )
                return {"id": ticket_id}

        return FakeFlows()

    def run_revoke(self, union_id=UID, service=SERVICE, *, boom=()):
        backend = self.backend()
        with mock.patch.object(Backend, "flows", lambda _self: self.flows(boom=boom)):
            return backend, backend.revoke_service_grants(union_id, service)

    def test_revokes_only_that_service_and_only_that_person(self):
        backend, left = self.run_revoke()
        self.assertEqual(left, [])
        self.assertEqual(self.calls, [("R-mlflow", "offboard")])
        # 撤完之后：那个服务没了，别的服务和别人的单一个字没动
        self.assertEqual([r["service"] for r in backend.service_holdings(UID)], [OTHER_SERVICE])
        self.assertEqual([r["ticket"] for r in backend.service_holdings(OTHER)], ["R-other"])

    def test_the_gateway_denies_him_afterwards(self):
        _backend, left = self.run_revoke()
        self.assertEqual(left, [])
        rows = tickets_mod.TicketStore(str(self.tickets_path)).all()
        got = sa.allowed(union_id=UID, service=SERVICE, tickets=rows, now=time.time())
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NO_GRANT)

    def test_empty_service_means_every_service_he_holds(self):
        backend, left = self.run_revoke(service="")
        self.assertEqual(left, [])
        self.assertEqual(sorted(t for t, _a in self.calls), ["R-mlflow", "R-tb"])
        self.assertEqual(backend.service_holdings(UID), [])

    def test_one_failure_does_not_block_the_rest(self):
        backend, left = self.run_revoke(service="", boom={"R-mlflow"})
        self.assertEqual(sorted(t for t, _a in self.calls), ["R-mlflow", "R-tb"])
        self.assertEqual(len(left), 1, left)
        self.assertIn("R-mlflow", left[0])
        # 撤掉的那张确实没了，没撤掉的还在（**没撤掉的不许静悄悄消失**）
        self.assertEqual([r["service"] for r in backend.service_holdings(UID)], [SERVICE])

    def test_nothing_to_revoke_is_clean_not_an_error(self):
        _backend, left = self.run_revoke(union_id="on_nobody")
        self.assertEqual(left, [])
        self.assertEqual(self.calls, [])

    def test_already_revoked_or_expired_grants_are_not_touched(self):
        """判据还是 holdings：撤过的、过期的不再撤一次（`revoke_now` 对终态会抛）。"""
        self.write(
            ticket(tid="R-done", service=SERVICE, status=tickets_mod.REVOKED),
            ticket(tid="R-old", service=SERVICE, expires=time.time() - 1),
        )
        _backend, left = self.run_revoke()
        self.assertEqual((self.calls, left), ([], []))

    def test_without_a_ticket_store_it_says_so_instead_of_reporting_success(self):
        """**方向锁**：撤不了要回一条说明。回空 = 告诉调用方「撤干净了」。"""
        left = self.backend(tickets=False).revoke_service_grants(UID, SERVICE)
        self.assertTrue(left)
        self.assertTrue(all(isinstance(x, str) and x for x in left), left)

    def test_the_actor_is_recorded_as_offboard(self):
        """撤单会落审计；这一笔是离职流程做的，不是某个管理员手点的。"""
        self.run_revoke()
        self.assertEqual([a for _t, a in self.calls], ["offboard"])


class PendingCardServiceTests(unittest.TestCase):
    """离职待办卡：自建服务那一行**单独渲染**，云那两行一个字不变。"""

    def rec(self, **kw):
        base = {
            "platform": offboard.SERVICE_PLATFORM,
            "account": SERVICE,
            "user": UID,
            "person": "王五",
            "email": "wuji-wang@wuji-tech.com",
            "state": offboard.DISABLED,
            "signal": "飞书状态：已离职",
        }
        base.update(kw)
        return base

    def cloud_rec(self, **kw):
        return self.rec(platform="aliyun", account="1234", user="lisi", **kw)

    def card(self, *records):
        from delivery import notify

        return notify.pending_card(list(records))

    def buttons(self, card):
        """`(文案, 回调值, 二次确认语)`。2.0 没有 action 容器，按钮直接是元素。"""
        out = []

        def walk(elements):
            for el in elements or ():
                if not isinstance(el, dict):
                    continue
                if el.get("tag") == "button":
                    ask = ((el.get("confirm") or {}).get("text") or {}).get("content", "")
                    for beh in el.get("behaviors") or []:
                        out.append((el["text"]["content"], beh.get("value"), ask))
                    continue
                for col in el.get("columns") or ():
                    walk(col.get("elements"))
                walk(el.get("elements"))

        walk(card["body"]["elements"])
        return out

    def test_the_two_platform_constants_must_not_drift(self):
        """`notify._SERVICE_PLATFORM` 是为了不 import offboard（会成环）才另写的一份。

        漂移的表现：这张卡又退回显示 union_id（`on_…` 随机串），管理员看不出是什么，
        而且没有任何地方报错。
        """
        from delivery import notify

        self.assertEqual(notify._SERVICE_PLATFORM, offboard.SERVICE_PLATFORM)  # noqa: SLF001

    def test_the_service_row_shows_the_service_name_not_the_union_id(self):
        text = json.dumps(self.card(self.rec()), ensure_ascii=False)
        self.assertIn(SERVICE, text)
        self.assertNotIn(UID, text.replace(f"internal/{SERVICE}/{UID}", ""), "只该出现在回调键里")
        self.assertIn("自建服务", text)
        self.assertIn("王五", text)
        self.assertIn("wuji-wang@wuji-tech.com", text, "人还是靠姓名和邮箱认")

    def test_the_button_says_take_back_not_delete(self):
        got = self.buttons(self.card(self.rec()))
        labels = [b[0] for b in got]
        self.assertIn("确认收回", labels)
        self.assertNotIn("确认删除", labels)
        self.assertIn("没离职 / 恢复", labels)
        key = f"internal/{SERVICE}/{UID}"
        self.assertIn({"a": "del", "k": key}, [b[1] for b in got])
        self.assertIn({"a": "keep", "k": key}, [b[1] for b in got])

    def test_the_confirm_text_does_not_lie_about_what_happens(self):
        """云那句「出组、摘策略、删 AK」对自建服务**逐字都是假的**。
        照搬的话管理员会以为点下去动了云上的东西，于是不敢点。"""
        ask = next(b[2] for b in self.buttons(self.card(self.rec())) if b[0] == "确认收回")
        self.assertIn(SERVICE, ask)
        self.assertIn("授权单", ask)
        self.assertIn("数据一个字都不动", ask)
        for lie in ("出组", "摘策略", "删 AK", "不能恢复", "控制台"):
            self.assertNotIn(lie, ask, lie)

    def test_the_cloud_row_is_untouched(self):
        """**加分支时没碰到别人**：阿里云那一行的文案一个字没变。"""
        got = self.buttons(self.card(self.cloud_rec()))
        ask = next(b[2] for b in got if b[0] == "确认删除")
        self.assertEqual(
            ask,
            "删除阿里云账号 lisi？会删掉账号本身（出组、摘策略、删 AK）。"
            "他在桶里的文件、数据集、实例都不动。删了不能恢复。",
        )
        self.assertIn({"a": "del", "k": "aliyun/1234/lisi"}, [b[1] for b in got])

    def test_jiuzhang_row_is_untouched(self):
        got = self.buttons(self.card(self.rec(platform="jiuzhang", account="wuji", user="wuji-x")))
        self.assertIn("我已在控制台处理", [b[0] for b in got])

    def test_both_platforms_in_one_card_keep_their_own_wording(self):
        """同一个人：云账号 + 自建服务挨着摆，两行各说各的。"""
        card = self.card(self.cloud_rec(), self.rec())
        got = self.buttons(card)
        self.assertEqual(
            sorted({b[0] for b in got}), sorted({"确认删除", "确认收回", "没离职 / 恢复"})
        )
        self.assertEqual(
            sorted(b[1]["k"] for b in got if b[1]["a"] == "del"),
            sorted(["aliyun/1234/lisi", f"internal/{SERVICE}/{UID}"]),
        )
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("阿里云 `lisi`", text)
        self.assertIn(f"自建服务 `{SERVICE}`", text)
        self.assertEqual(text.count("**王五**"), 1, "同一个人只出现一块")

    def test_an_unverified_service_row_still_only_gets_the_keep_button(self):
        got = self.buttons(self.card(self.rec(unverified=True)))
        self.assertEqual([b[0] for b in got], ["没离职 / 恢复"])

    def test_two_services_are_two_rows_with_their_own_keys(self):
        card = self.card(self.rec(), self.rec(account=OTHER_SERVICE))
        keys = sorted(b[1]["k"] for b in self.buttons(card) if b[1]["a"] == "del")
        self.assertEqual(
            keys, sorted([f"internal/{OTHER_SERVICE}/{UID}", f"internal/{SERVICE}/{UID}"])
        )


class RemindEndToEndTests(unittest.TestCase):
    """整条定时任务跑一遍：`identity iam-remind` → 记录 → 卡片。

    单测各段都绿、接线漏一处照样什么都不发生 —— 这条用例走的是真的
    `_cmd_identity_iam_remind`（只把飞书、IAM、云执行体换成假的）。
    """

    UID = "on_noaccount"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "people.json").write_text("{}", encoding="utf-8")
        (self.dir / "admins.json").write_text("{}", encoding="utf-8")
        self.tickets = self.dir / "tickets.json"
        self.write_tickets(ticket(tid="REQ-9", union_id=self.UID))
        self.path = offboard.path_beside(str(self.dir / "people.json"))
        self.cloud_calls = []

    def write_tickets(self, *rows, raw=None):
        if raw is not None:
            self.tickets.write_text(raw, encoding="utf-8")
            return
        self.tickets.write_text(
            json.dumps({"schema": tickets_mod.SCHEMA, "tickets": list(rows)}, ensure_ascii=False),
            encoding="utf-8",
        )

    def run_remind(self, roster, *, statuses, tickets=None):
        from types import SimpleNamespace

        from delivery import cli, server  # noqa: F401 — 先导入，别在 patch 期间导入

        args = SimpleNamespace(
            people=str(self.dir / "people.json"),
            attributes=str(self.dir / "attrs.json"),
            admins=str(self.dir / "admins.json"),
            tickets=str(self.tickets) if tickets is None else tickets,
            every_hours=24.0,
        )
        sent = []
        notifier = mock.Mock()
        notifier.send.side_effect = lambda uid, card, id_type: sent.append(card)
        staff = {pp.email.lower(): {"union_id": pp.union_id} for pp in roster}

        def cloud(platform, account, **kw):
            self.cloud_calls.append((platform, account))
            raise AssertionError("这个人没有云账号，不该构造云执行体")

        with (
            mock.patch.dict(
                os.environ,
                {"DELIVERY_FEISHU_APP_ID": "a", "DELIVERY_FEISHU_APP_SECRET": "b"},
            ),
            mock.patch("delivery.iam_sync.reconcile_report", return_value={"apps": []}),
            mock.patch("delivery.iam_sync.load_snooze", return_value=set()),
            mock.patch("delivery.people.load", return_value=SimpleNamespace(people=roster)),
            mock.patch("delivery.identity.directory.staff_index", return_value=staff),
            mock.patch("delivery.identity.directory.status_of", return_value=statuses),
            mock.patch(
                "delivery.roles.load_admins", return_value=SimpleNamespace(union_ids={"on_admin"})
            ),
            mock.patch("delivery.notify.FeishuNotifier", return_value=notifier),
            mock.patch("delivery.server._tenant_token_cache", return_value=lambda: "t"),
            mock.patch("delivery.provision.executor_from_env", side_effect=cloud),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = cli._cmd_identity_iam_remind(args)
        return code, [json.dumps(c, ensure_ascii=False) for c in sent]

    def gone(self):
        return person("王五", self.UID, email="wuji-wang@wuji-tech.com")

    def test_a_person_with_only_a_service_grant_is_recorded_and_shows_up_on_the_card(self):
        code, cards = self.run_remind([self.gone()], statuses={self.UID: {"is_resigned": True}})
        self.assertEqual(code, 0)
        self.assertEqual(self.cloud_calls, [], "没有云账号，一次云执行体都不该构造")

        key = offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, self.UID)
        rec = offboard.load(self.path)[key]
        self.assertEqual(rec["state"], offboard.DISABLED)
        self.assertEqual(rec["union_id"], self.UID)
        self.assertIn("飞书状态", rec["signal"])

        mine = [t for t in cards if "王五" in t]
        self.assertEqual(len(mine), 1, cards)
        self.assertIn(f"自建服务 `{SERVICE}`", mine[0])
        self.assertIn("确认收回", mine[0])
        self.assertIn(key, mine[0])
        self.assertNotIn("确认删除", mine[0])
        # 留痕
        log = (self.dir / "review.log").read_text(encoding="utf-8").splitlines()
        self.assertIn("offboard_disable", [json.loads(line)["op"] for line in log])

    def test_an_unreadable_ledger_only_costs_this_round(self):
        """**fail-open 方向**：台账读不了 → 不写任何 internal 记录，整轮照常结束。"""
        self.write_tickets(raw="{ 坏的")
        code, _cards = self.run_remind([self.gone()], statuses={self.UID: {"is_resigned": True}})
        self.assertEqual(code, 0)
        self.assertEqual(offboard.load(self.path), {})
        # 下一轮台账好了就收得到（嫌疑/记录都没写，不会挡住重来）
        self.write_tickets(ticket(tid="REQ-9", union_id=self.UID))
        self.run_remind([self.gone()], statuses={self.UID: {"is_resigned": True}})
        self.assertIn(
            offboard.key_of(offboard.SERVICE_PLATFORM, SERVICE, self.UID), offboard.load(self.path)
        )

    def test_no_tickets_argument_still_runs(self):
        """老命令行（没有 `--tickets`）照样能跑，只是不收自建服务。"""
        code, _cards = self.run_remind(
            [self.gone()], statuses={self.UID: {"is_resigned": True}}, tickets=""
        )
        self.assertEqual(code, 0)
        self.assertEqual(offboard.load(self.path), {})

    def test_a_revoked_grant_produces_no_record_and_no_card(self):
        self.write_tickets(ticket(tid="REQ-9", union_id=self.UID, status=tickets_mod.REVOKED))
        code, cards = self.run_remind([self.gone()], statuses={self.UID: {"is_resigned": True}})
        self.assertEqual(code, 0)
        self.assertEqual(offboard.load(self.path), {})
        self.assertEqual([t for t in cards if "王五" in t and "待确认" in t], [])


if __name__ == "__main__":
    unittest.main()
