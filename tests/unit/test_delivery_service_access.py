"""「自建服务访问授权」的判定层：`delivery.service_access` + `Backend.service_access`。

这条链路是什么
──────────────
MLflow 跑在另一台机（`tensorboard.wuji-tech.com`），前面那个自建网关用 nginx
`auth_request` 对**每一个请求**问面板一次「这个人能不能进」。答案由申请单算出来。

这里锁住的是「答案怎么算」，HTTP 那一层（令牌门禁、响应体最小化）在
`test_delivery_service_access_api.py`。

为什么这些规矩值得一条条锁
──────────────────────────
  · **判据只有申请单一处**。不另存名单，因为两处真相只会被修一边，而漏的那次表现是
    「单子撤了、人还能进」—— 没有任何地方会报错，也没有任何人会发现。
  · **离职在判定侧拒**，不靠「记得去撤单」。`suspect` 不算离职：那只是嫌疑、还没人
    确认，拿它挡人会误伤在职的。
  · **fail 的方向是「查不到就拒」**，和网关里已有的 `is_disabled()`（查库失败放行）
    正好相反，两者相邻但**有意不同**：禁用名单查不到不该误伤正常人；授权查不到就放行
    的话，面板一挂这道门就等于不存在。见下面「fail 的方向」那一组用例。
  · 唯一的例外是**名册读不了**：那不等于「这个人不存在」，判成不在册会把所有人挡在
    外面，而那不是这道门要防的事。

离线，数据虚构。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from delivery import offboard as offboard_mod
from delivery import service_access as sa
from delivery import tickets as tickets_mod
from delivery.errors import DeliveryError
from delivery.server import Backend

UID = "on_u1"
OTHER = "on_u2"
SERVICE = "mlflow"
NOW = 1_700_000_000.0

#: 申请单里会出现、但**一个字都不该回给网关**的东西（响应体那条在 _api 里断言）
PII = {
    "name": "李四",
    "email": "li.si@wuji.tech",
    "phone": "13900000001",
    "department": "算法组",
}


def ticket(
    *,
    tid: str = "REQ-20260101-DEADBEEF",
    kind: str = "service",
    status: str = tickets_mod.DONE,
    service: str = SERVICE,
    union_id: str = UID,
    expires: object = None,
    **extra,
) -> dict:
    """一张服务访问单。默认是「批下来了、没到期、是这个人的」。"""
    row = {
        "id": tid,
        "kind": kind,
        "status": status,
        "template": {"id": "svc-mlflow", "kind": "service", "service": service},
        "applicant": {"union_id": union_id, "open_id": "ou_x", **PII},
        "payload": {"days": 30},
    }
    if expires is not None:
        row["expires_at_ts"] = expires
    row.update(extra)
    return row


def decide(tickets, *, union_id=UID, service=SERVICE, **kw):
    return sa.allowed(union_id=union_id, service=service, tickets=tickets, now=NOW, **kw)


# ── 纯判定 ────────────────────────────────────────────────────────────────


class AllowedTests(unittest.TestCase):
    """`service_access.allowed()`：零 I/O，判据就是传进来的那堆单子。"""

    def test_valid_ticket_allows_with_id_and_expiry(self):
        got = decide([ticket(expires=NOW + 3600)])
        self.assertTrue(got.allowed)
        self.assertEqual(got.reason, "")
        self.assertEqual(got.ticket_id, "REQ-20260101-DEADBEEF")
        self.assertEqual(got.expires_at, NOW + 3600)
        self.assertEqual(got.message, "")

    def test_ticket_without_expiry_allows(self):
        got = decide([ticket()])
        self.assertTrue(got.allowed)
        self.assertEqual(got.expires_at, 0.0)

    def test_someone_elses_ticket_does_not_count(self):
        """判的是 `applicant.union_id` 严格相等。别人的单子放行 = 越权。"""
        got = decide([ticket(union_id=OTHER, expires=NOW + 3600)])
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NO_GRANT)
        self.assertEqual(got.ticket_id, "")

    def test_union_id_case_or_space_mismatch_does_not_count(self):
        """union_id 是精确标识，不做归一化 —— 归一化过的比对早晚会把两个人认成一个。"""
        for bad in (UID.upper(), f" {UID}", f"{UID} ", UID.replace("_", "-"), UID + "x"):
            with self.subTest(bad):
                got = decide([ticket(union_id=bad)])
                self.assertFalse(got.allowed, bad)
                self.assertEqual(got.reason, sa.NO_GRANT)

    def test_other_service_ticket_does_not_count(self):
        """mlflow 的网关只能凭 mlflow 的单子放人，别的服务的单子不是这道门的授权。"""
        for other in ("tensorboard", "MLFLOW", "mlflow2", ""):
            with self.subTest(other):
                self.assertFalse(decide([ticket(service=other)]).allowed, other)
        # 模板整个缺失也不行（旧单子、脏数据）
        row = ticket()
        row.pop("template")
        self.assertFalse(decide([row]).allowed)
        row = ticket()
        row["template"] = None
        self.assertFalse(decide([row]).allowed)

    def test_only_done_counts_revoked_and_pending_do_not(self):
        """只有 done 算数。**撤销（revoked）是这道门唯一的「立刻挡住」手段**，
        它要是不生效，撤销按钮就是个摆设。"""
        for status in (
            tickets_mod.SUBMITTING,
            tickets_mod.SUBMIT_FAILED,
            tickets_mod.PENDING,
            tickets_mod.REJECTED,
            tickets_mod.WITHDRAWN,
            tickets_mod.APPROVED,
            tickets_mod.EXECUTING,
            tickets_mod.FAILED,
            tickets_mod.FULFILLING,
            tickets_mod.CLOSED,
            tickets_mod.REVOKED,
            "",
            "DONE",
        ):
            with self.subTest(status):
                got = decide([ticket(status=status, expires=NOW + 3600)])
                self.assertFalse(got.allowed, status)
                self.assertEqual(got.reason, sa.NO_GRANT)

    def test_other_kinds_do_not_count(self):
        """同一个人、同一个 service 字段，但 kind 不是 service（权限单、凭证单）——
        那是另一件事批的，不能顺带开这道门。"""
        for kind in ("permission", "credential", "storage", "resource", "datatype", "", None):
            with self.subTest(kind):
                self.assertFalse(decide([ticket(kind=kind, expires=NOW + 3600)]).allowed, kind)

    def test_expired_does_not_count_boundary_is_equal(self):
        self.assertFalse(decide([ticket(expires=NOW - 1)]).allowed)
        # 到期时刻本身算过期：到期时间是「有效到」而不是「含这一秒」
        self.assertFalse(decide([ticket(expires=NOW)]).allowed)
        self.assertTrue(decide([ticket(expires=NOW + 1)]).allowed)

    def test_empty_identity_or_service_denies(self):
        """拿不到身份就拒，**不是「默认放行」** —— 网关传空是它自己坏了，不是这人有权限。"""
        for uid, svc in (("", SERVICE), (" ", SERVICE), (UID, ""), ("", ""), (None, SERVICE)):
            with self.subTest(uid=uid, svc=svc):
                got = sa.allowed(
                    union_id=uid, service=svc, tickets=[ticket()], now=NOW, in_roster=True
                )
                self.assertFalse(got.allowed)
                self.assertEqual(got.reason, sa.NO_GRANT)

    def test_no_tickets_means_no_grant(self):
        got = decide([])
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NO_GRANT)
        self.assertEqual(decide(None).reason, sa.NO_GRANT)

    def test_offboarded_overrides_valid_ticket(self):
        """离职接在判定侧，不依赖有人去把他的单子撤掉。"""
        got = decide([ticket(expires=NOW + 3600)], offboarded=True)
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.BLOCKED)
        self.assertEqual(got.ticket_id, "")

    def test_offboarded_wins_over_not_in_roster(self):
        """两样都命中时说「账号已停用」，不说「名册没同步」—— 后者会把人引去找管理员
        登记，而他要的是别再进来。"""
        got = decide([ticket()], offboarded=True, in_roster=False)
        self.assertEqual(got.reason, sa.BLOCKED)

    def test_not_in_roster_is_its_own_reason(self):
        got = decide([ticket(expires=NOW + 3600)], in_roster=False)
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NOT_IN_ROSTER)

    def test_picks_the_latest_expiring_ticket(self):
        """续期时新旧会并存一段，挑早的那张会让人在续过期之后被挡住。"""
        rows = [
            ticket(tid="REQ-OLD", expires=NOW + 60),
            ticket(tid="REQ-NEW", expires=NOW + 86400),
            ticket(tid="REQ-MID", expires=NOW + 600),
        ]
        got = decide(rows)
        self.assertTrue(got.allowed)
        self.assertEqual(got.ticket_id, "REQ-NEW")
        self.assertEqual(got.expires_at, NOW + 86400)

    def test_unlimited_ticket_beats_expiring_one(self):
        for rows in (
            [ticket(tid="REQ-FOREVER"), ticket(tid="REQ-SOON", expires=NOW + 60)],
            [ticket(tid="REQ-SOON", expires=NOW + 60), ticket(tid="REQ-FOREVER")],
        ):
            with self.subTest([r["id"] for r in rows]):
                got = decide(rows)
                self.assertEqual(got.ticket_id, "REQ-FOREVER")
                self.assertEqual(got.expires_at, 0.0)

    def test_expired_ticket_never_shadows_a_live_one(self):
        rows = [ticket(tid="REQ-DEAD", expires=NOW - 10), ticket(tid="REQ-LIVE", expires=NOW + 10)]
        self.assertEqual(decide(rows).ticket_id, "REQ-LIVE")
        self.assertEqual(decide(list(reversed(rows))).ticket_id, "REQ-LIVE")

    def test_finds_own_ticket_among_noise(self):
        rows = [
            ticket(tid="X1", union_id=OTHER),
            ticket(tid="X2", service="tensorboard"),
            ticket(tid="X3", status=tickets_mod.REVOKED),
            ticket(tid="MINE", expires=NOW + 100),
            ticket(tid="X4", kind="permission"),
        ]
        self.assertEqual(decide(rows).ticket_id, "MINE")

    def test_dirty_rows_do_not_raise(self):
        """单子是 JSON 文件读回来的，字段缺了、类型不对都可能发生。**判定不能抛** ——
        抛在这里就是整台 MLflow 进不去。"""
        rows = [
            {},
            {"kind": "service"},
            {"kind": "service", "status": tickets_mod.DONE},
            {"kind": "service", "status": tickets_mod.DONE, "template": {}, "applicant": {}},
            ticket(expires="不是数字"),
            ticket(expires=None),
        ]
        for row in rows:
            with self.subTest(row):
                self.assertIsInstance(decide([row]), sa.Decision)

    def test_one_malformed_row_does_not_lock_everyone_out(self):
        """**回归**：`template` / `applicant` / 行本身不是字典时，直接 `.get()` 会
        `AttributeError` —— 判定抛出去 → 端点 500 → 网关 fail-closed → **所有人**都进不了
        MLflow，而罪魁只是申请单里的一行脏数据（那行还可能跟这个服务毫无关系）。

        所以取每一层字段之前都要先确认形状：脏行**只淘汰它自己**。
        """
        rows = [
            {"kind": "service", "status": tickets_mod.DONE, "template": "x", "applicant": 1},
            {"kind": "service", "status": tickets_mod.DONE, "template": ["x"], "applicant": [UID]},
            ticket(applicant=[UID]),
            ticket(template="mlflow"),
            "整行都不是字典",
            None,
            42,
            ["kind", "service"],
        ]
        for row in rows:
            self.assertFalse(decide([row]).allowed, row)
            # 同一批里混一张好单子，好单子该照样放行
            self.assertTrue(decide([row, ticket(expires=NOW + 60)]).allowed, row)
        # 整批全是脏数据：是「没权限」，不是抛异常
        got = decide(rows)
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NO_GRANT)
        # 脏行在好单子**前面**和**后面**都不该挡住它
        self.assertTrue(decide([*rows, ticket(expires=NOW + 60)]).allowed)
        self.assertTrue(decide([ticket(expires=NOW + 60), *rows]).allowed)


class ExpiryParseTests(unittest.TestCase):
    """到期时间怎么读。**读不懂就拒**，不能兜底成 0 —— 0 在这里的语义是「不限期」，
    脏数据折成 0 就成了一张永久通行证，而且没有任何一处会报错
    （`revoke.py:156` 用同一套 `float(... or 0)`，定时回收也永远扫不到它）。"""

    def test_zero_expiry_means_no_deadline(self):
        self.assertTrue(decide([ticket(expires=0)]).allowed)
        self.assertTrue(decide([ticket(expires=None)]).allowed)
        self.assertTrue(decide([ticket()]).allowed)  # 字段根本没有

    def test_numeric_string_expiry_is_parsed(self):
        got = decide([ticket(expires=str(NOW + 100))])
        self.assertTrue(got.allowed)
        self.assertEqual(got.expires_at, NOW + 100)

    def test_unparsable_expiry_denies_instead_of_granting_forever(self):
        """**回归**：`expires_at_ts` 是 ISO 串（写错字段、旧版本留下的单子）时
        **不能**当成「不限期」。判不出到期时间 = 不知道过没过期 = 按没权限处理。"""
        for bad in ("2020-01-01T00:00:00+08:00", "明天", "3 days", [], {}, object()):
            with self.subTest(bad=repr(bad)):
                got = decide([ticket(expires=bad)])
                self.assertFalse(got.allowed, repr(bad))
                self.assertEqual(got.reason, sa.NO_GRANT)

    def test_a_dirty_ticket_does_not_hide_a_good_one(self):
        """坏数据只淘汰它自己那一张，别的单子照常判。"""
        rows = [ticket(tid="REQ-DIRTY", expires="明天"), ticket(tid="REQ-OK", expires=NOW + 60)]
        self.assertEqual(decide(rows).ticket_id, "REQ-OK")
        self.assertEqual(decide(list(reversed(rows))).ticket_id, "REQ-OK")


class MessageTests(unittest.TestCase):
    """三种拒绝的文案必须**互不相同**：没申请的该去申请，名册没同步的该找管理员，
    离职的谁也帮不了他。混成一句会让人做错事（也会让管理员被没必要的人找上）。

    这里只锁「区分度」，不锁具体字句 —— 字句该能随时改好。
    """

    def messages(self):
        return {
            reason: sa.Decision(False, reason).message
            for reason in (sa.NO_GRANT, sa.NOT_IN_ROSTER, sa.BLOCKED)
        }

    def test_three_denials_have_three_distinct_messages(self):
        msgs = self.messages()
        self.assertEqual(len(set(msgs.values())), 3, msgs)
        for reason, text in msgs.items():
            self.assertTrue(text.strip(), reason)

    def test_allowed_has_no_message(self):
        self.assertEqual(sa.Decision(True).message, "")
        self.assertEqual(sa.Decision(True, ticket_id="REQ-1", expires_at=NOW).message, "")

    def test_not_in_roster_message_is_not_the_permission_one(self):
        """这一句是唯一「该找管理员」的情形。它要是和「没权限」长得一样，
        管理员就会收到一堆其实只该去提申请的人。"""
        msgs = self.messages()
        self.assertNotEqual(msgs[sa.NOT_IN_ROSTER], msgs[sa.NO_GRANT])
        self.assertNotEqual(msgs[sa.NOT_IN_ROSTER], msgs[sa.BLOCKED])

    def test_unknown_reason_falls_back_to_no_grant_message(self):
        """将来加了第四种原因、忘了加文案时，兜底该是最无害的那句。"""
        self.assertEqual(sa.Decision(False, "some_new_reason").message, sa.Decision(False).message)

    def test_reason_codes_are_distinct(self):
        """网关按 reason 分流到不同页面，码撞了就分不开。"""
        self.assertEqual(len({sa.NO_GRANT, sa.NOT_IN_ROSTER, sa.BLOCKED}), 3)

    def test_decision_is_frozen(self):
        """判完还能被改的话，「谁改的」就没法追了。"""
        got = sa.Decision(True)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            got.allowed = False


class NoSecondSourceOfTruthTests(unittest.TestCase):
    """**不另存一份授权名单**。这条不是风格问题：多一处真相就会有一次只修一边，
    而漏的那次表现是「单子撤了、人还能进」，没有任何地方会报错。"""

    def test_allowed_takes_tickets_only_no_roster_list(self):
        params = set(inspect.signature(sa.allowed).parameters)
        self.assertEqual(
            params, {"union_id", "service", "tickets", "now", "in_roster", "offboarded"}
        )

    def test_decision_layer_reads_no_files(self):
        """纯逻辑：不 import os/pathlib/json，也就不可能偷偷读第二份名单。"""
        src = Path(inspect.getfile(sa)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported & {"os", "pathlib", "json", "urllib", "socket", "time"}, set())

    def test_repo_has_no_service_access_list_file(self):
        """`identity/service-access.json` 这种东西一旦出现，就是第二处真相。"""
        root = Path(inspect.getfile(sa)).resolve().parents[2]
        self.assertEqual(list(root.glob("**/service-access.json")), [])


# ── Backend.service_access：读文件那一层 ──────────────────────────────────


class BackendServiceAccessTests(unittest.TestCase):
    """`Backend.service_access` 把三个文件（申请单 / 离职记录 / 名册）合成一个判定。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.tickets_path = self.root / "tickets.json"
        self.people_path = self.root / "people.json"
        self.offboard_path = offboard_mod.path_beside(str(self.people_path))
        self.write_people(UID, OTHER)
        self.write_tickets(ticket(expires=time.time() + 86400))
        self.backend = Backend(
            people_path=str(self.people_path),
            tickets_path=str(self.tickets_path),
            platforms={"aliyun": "阿里云"},
        )

    # ── 文件 ──

    def write_tickets(self, *rows, schema: object = tickets_mod.SCHEMA):
        payload = {"schema": schema, "tickets": list(rows)}
        if schema is None:
            payload.pop("schema")
        self._write(self.tickets_path, payload)

    def write_people(self, *uids):
        self._write(
            self.people_path,
            {
                "schema": "wuji-people@1",
                "people": [
                    {"union_id": u, "name": PII["name"], "email": f"{u}@wuji.tech"} for u in uids
                ],
            },
        )

    def write_offboard(self, **states):
        """`{union_id: state}` → 真实格式的 offboard.json（`records` 外层在文件里）。"""
        records = {
            f"aliyun/1234567890/{uid}": {
                "union_id": uid,
                "user": uid,
                "platform": "aliyun",
                "state": state,
                "at": "2026-01-01T00:00:00+08:00",
            }
            for uid, state in states.items()
        }
        self._write(self.offboard_path, {"records": records})

    def _write(self, path: Path, payload: dict):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.bump(path)

    def bump(self, path: Path):
        """把 mtime 推到一个比之前任何一次都大的值，保证缓存键一定变。

        为什么要显式推：这台机上文件 mtime 的**粒度是 1ms**（实测连写 5 次拿到同一个
        `st_mtime_ns`），而 `Backend._cached` 的键就是 mtime。用例里两次写盘常落在同一
        毫秒，不推的话测的就不是「缓存会不会失效」而是「这毫秒过完没」。
        同一毫秒内改单子读到旧结果这件事本身另有一条 xfail 记在案。
        """
        self._mtime_ns = max(getattr(self, "_mtime_ns", 0), path.stat().st_mtime_ns) + 2_000_000
        os.utime(path, ns=(self._mtime_ns, self._mtime_ns))

    def ask(self, union_id=UID, service=SERVICE):
        return self.backend.service_access(union_id=union_id, service=service)

    # ── 按单子判 ──

    def test_ticket_allows_and_stranger_is_denied(self):
        got = self.ask()
        self.assertTrue(got.allowed)
        self.assertEqual(got.ticket_id, "REQ-20260101-DEADBEEF")
        self.assertFalse(self.ask(union_id=OTHER).allowed)
        self.assertEqual(self.ask(union_id=OTHER).reason, sa.NO_GRANT)

    def test_revoke_takes_effect_on_next_query(self):
        """撤销按钮的唯一场景就是「现在就要挡住」。要等缓存过期的话它就没用。"""
        self.assertTrue(self.ask().allowed)
        self.write_tickets(ticket(status=tickets_mod.REVOKED, expires=time.time() + 86400))
        got = self.ask()
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NO_GRANT)

    def test_new_ticket_takes_effect_on_next_query(self):
        self.write_tickets()
        self.assertFalse(self.ask().allowed)
        self.write_tickets(ticket(expires=time.time() + 86400))
        self.assertTrue(self.ask().allowed)

    def test_cache_avoids_rereading_unchanged_file(self):
        """网关是**每个请求**问一次的，不能每次都把整份申请单读一遍。"""
        real = tickets_mod.TicketStore.all
        reads = []

        def counted(store):
            reads.append(str(store.path))
            return real(store)

        with mock.patch.object(tickets_mod.TicketStore, "all", counted):
            for _ in range(5):
                self.assertTrue(self.ask().allowed)
            self.assertEqual(len(reads), 1, reads)
            self.write_tickets(ticket(status=tickets_mod.REVOKED))
            self.assertFalse(self.ask().allowed)
            self.assertEqual(len(reads), 2, reads)

    def test_a_change_within_the_same_millisecond_is_still_seen(self):
        """**回归**：本机文件 mtime 的实际粒度是 **1ms**（实测：连续 5 次写入拿到同一个
        `st_mtime_ns`）。缓存键只看 mtime 的话，同一毫秒内完成「撤销 → 网关来问」
        就会读到撤销前那份单子 —— 而这个按钮的唯一场景就是「现在就要挡住」。

        所以缓存键要 **mtime + 文件大小**一起看（`Backend._stamp`）。本用例用
        `os.utime` 把两版的 mtime 钉成同一个值，把「这毫秒过完没」这个随机性去掉：
        撑住这条的只能是大小那一半。
        """
        stamp = self.tickets_path.stat()
        self.assertTrue(self.ask().allowed)
        self.tickets_path.write_text(
            json.dumps({"schema": tickets_mod.SCHEMA, "tickets": []}), encoding="utf-8"
        )
        os.utime(self.tickets_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(self.tickets_path.stat().st_mtime_ns, stamp.st_mtime_ns, "mtime 得一样")
        self.assertNotEqual(self.tickets_path.stat().st_size, stamp.st_size)
        self.assertFalse(self.ask().allowed)

    # ── 离职 ──

    def test_disabled_and_deleted_block_suspect_does_not(self):
        """`suspect` 只是「通讯录里找不到他」的嫌疑，还没人确认过。拿它挡人会误伤在职的
        （飞书对「已离职」和「不在应用可见范围内」返回的是同一个结果）。"""
        for state, expect in (
            (offboard_mod.DISABLED, sa.BLOCKED),
            (offboard_mod.DELETED, sa.BLOCKED),
            (offboard_mod.SUSPECT, ""),
        ):
            with self.subTest(state):
                self.write_offboard(**{UID: state})
                got = self.ask()
                self.assertEqual(got.reason, expect, state)
                self.assertEqual(got.allowed, expect == "", state)

    def test_offboard_state_constants_are_stable(self):
        self.assertEqual(
            (offboard_mod.DISABLED, offboard_mod.DELETED, offboard_mod.SUSPECT),
            ("disabled", "deleted", "suspect"),
        )

    def test_restored_or_dismissed_does_not_block(self):
        """`restored`（管理员点了「没离职」）之类的状态不能挡人。"""
        for state in ("restored", "dismissed", "", "unknown"):
            with self.subTest(state):
                self.write_offboard(**{UID: state})
                self.assertTrue(self.ask().allowed, state)

    def test_someone_elses_offboarding_is_irrelevant(self):
        self.write_offboard(**{OTHER: offboard_mod.DISABLED})
        self.assertTrue(self.ask().allowed)

    def test_offboarding_overrides_a_valid_ticket(self):
        self.write_offboard(**{UID: offboard_mod.DISABLED})
        got = self.ask()
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.BLOCKED)
        self.assertEqual(got.ticket_id, "")

    def test_offboard_change_takes_effect_on_next_query(self):
        """停号和撤单是两件事，停号这边也要立刻挡住。"""
        self.assertTrue(self.ask().allowed)
        self.write_offboard(**{UID: offboard_mod.DISABLED})
        self.assertEqual(self.ask().reason, sa.BLOCKED)
        self.write_offboard(**{UID: "restored"})
        self.assertTrue(self.ask().allowed)

    def test_real_offboard_file_format_is_understood(self):
        """**回归**：`offboard.load()` 回的**就是**记录字典本身，`records` 外层只在文件里。
        写成 `load(...).get("records")` 会恒为空 —— 表现是「离职的人照样放行」，
        而且不报任何错。这是整条链路上最不该静默失败的地方。

        本用例走 offboard 模块自己的写入口，保证文件格式和线上逐字一致。
        """
        offboard_mod._write(  # noqa: SLF001 — 就是要用它的真实格式
            self.offboard_path,
            {
                f"aliyun/1234567890/{UID}": {
                    "union_id": UID,
                    "user": UID,
                    "state": offboard_mod.DISABLED,
                }
            },
        )
        self.bump(self.offboard_path)
        raw = json.loads(self.offboard_path.read_text(encoding="utf-8"))
        self.assertEqual(list(raw), ["records"])  # 文件里确实有外层
        self.assertEqual(self.ask().reason, sa.BLOCKED)

    def test_record_without_union_id_blocks_nobody(self):
        """记录缺 `union_id` 时**不拿记录键兜底**：键是 `aliyun/123/foo` 这种串，
        永远等不上任何 union_id，放进名单只会让这条记录看起来生效了、实际谁也挡不住。

        所以这类记录一律当成「没这条」，靠名册那道门去兜（见下面 roster 那组）。
        """
        self._write(
            self.offboard_path, {"records": {UID: {"state": offboard_mod.DISABLED, "user": "x"}}}
        )
        self.assertTrue(self.ask().allowed)
        # 有 union_id 的那条照样挡得住
        self._write(
            self.offboard_path,
            {"records": {f"aliyun/1/{UID}": {"union_id": UID, "state": offboard_mod.DISABLED}}},
        )
        self.assertEqual(self.ask().reason, sa.BLOCKED)

    def test_missing_offboard_file_means_nobody_left(self):
        self.assertFalse(self.offboard_path.exists())
        self.assertTrue(self.ask().allowed)

    def test_broken_offboard_file_refuses_to_decide(self):
        """**fail-closed**：读不出离职记录时不能假设「没人离职」——
        那正好是把已停用的人放进来的那条路。"""
        self.offboard_path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.bump(self.offboard_path)
        with self.assertRaises(DeliveryError):
            self.ask()

    def test_offboard_file_without_records_wrapper_refuses(self):
        self._write(self.offboard_path, {"rows": []})
        with self.assertRaises(DeliveryError):
            self.ask()

    def test_dirty_offboard_rows_are_skipped(self):
        self._write(
            self.offboard_path,
            {"records": {"x": "不是对象", "y": None, UID: {"union_id": UID, "state": "disabled"}}},
        )
        self.assertEqual(self.ask().reason, sa.BLOCKED)

    # ── 名册 ──

    def test_unknown_person_is_not_in_roster(self):
        got = self.ask(union_id="on_stranger")
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NOT_IN_ROSTER)

    def test_unreadable_roster_refuses_to_decide(self):
        """**名册在这条链上是承重的，所以读不了就拒判（异常穿出去 → HTTP 500）。**

        为什么不像面板别处那样「读不了就当在册」：离职记录是按**云子账号**记的
        （`key_of(platform, account, user)`），写它的 `auto_disable` 只遍历这个人已确认的
        云账号 —— 一个**没有云子账号**的人离职后，记录里一条都不会有，`gone` 永远挡不住他。
        而这个功能的立项理由恰恰是「最需要自建服务的人没有云子账号」：对这类人，
        唯一的离职信号就是他从名册里消失。

        所以「名册读不了就当在册」在这条链上的实际效果是「名册一坏，离职的人全部放行」。
        （代价也写在明面上：名册坏了所有人都进不去 —— fail-closed 这一边是有意选的。）
        """
        self.people_path.unlink()
        with self.assertRaises(DeliveryError):
            self.ask()

    def test_corrupt_roster_refuses_to_decide(self):
        self.people_path.write_text("{ 坏的", encoding="utf-8")
        self.bump(self.people_path)
        with self.assertRaises(DeliveryError):
            self.ask()

    def test_no_roster_path_refuses_to_decide(self):
        """没配名册路径也是「判不了」，不是「谁都在册」。"""
        backend = Backend(tickets_path=str(self.tickets_path), platforms={})
        with self.assertRaises(DeliveryError):
            backend.service_access(union_id=UID, service=SERVICE)

    def test_roster_update_takes_effect_on_next_query(self):
        self.assertEqual(self.ask(union_id="on_new").reason, sa.NOT_IN_ROSTER)
        self.write_people(UID, OTHER, "on_new")
        self.assertEqual(self.ask(union_id="on_new").reason, sa.NO_GRANT)

    # ── fail 的方向 ──

    def test_missing_ticket_store_config_refuses_to_decide(self):
        """**fail-closed**：判不了就抛（HTTP 层转 500，网关据此不放行）。
        返回「没权限」也行，返回「放行」绝对不行 —— 那等于配置缺一项这道门就没了。"""
        backend = Backend(people_path=str(self.people_path), platforms={})
        with self.assertRaises(DeliveryError):
            backend.service_access(union_id=UID, service=SERVICE)

    def test_ticket_store_without_schema_refuses_to_decide(self):
        """**回归**：申请单存储要求 `schema == "wuji-tickets@1"`，缺了 `TicketStore` 抛
        「申请单存储格式不对」。这里必须让它抛出去 —— 吞掉会变成「一张单子都没有」，
        而那和「这个人确实没权限」长得一模一样。"""
        self.write_tickets(ticket(), schema=None)
        with self.assertRaises(DeliveryError):
            self.ask()
        self.write_tickets(ticket(), schema="wuji-tickets@2")
        with self.assertRaises(DeliveryError):
            self.ask()

    def test_corrupt_ticket_file_refuses_to_decide(self):
        self.tickets_path.write_text("[]", encoding="utf-8")
        self.bump(self.tickets_path)
        with self.assertRaises(DeliveryError):
            self.ask()
        self.tickets_path.write_text("{ 坏的", encoding="utf-8")
        self.bump(self.tickets_path)
        with self.assertRaises(DeliveryError):
            self.ask()

    def test_absent_ticket_file_is_no_grant_not_an_error(self):
        """还没有人提过申请是正常状态（`TicketStore` 对不存在的文件回空清单）。"""
        self.tickets_path.unlink()
        got = self.ask()
        self.assertFalse(got.allowed)
        self.assertEqual(got.reason, sa.NO_GRANT)

    def test_no_failure_mode_falls_back_to_allow(self):
        """把所有「判不了」的情形过一遍，断言没有任何一种能得到 allowed=True。"""
        cases = {
            "单子坏了": lambda: self.tickets_path.write_text("{", encoding="utf-8"),
            "离职记录坏了": lambda: self.offboard_path.write_text("{", encoding="utf-8"),
            "单子缺 schema": lambda: self.write_tickets(ticket(), schema=None),
        }
        for name, break_it in cases.items():
            with self.subTest(name):
                self.setUp()
                break_it()
                self.bump(self.tickets_path)
                if self.offboard_path.exists():
                    self.bump(self.offboard_path)
                try:
                    got = self.ask()
                except DeliveryError:
                    continue
                self.assertFalse(got.allowed, name)


class CompareDigestTests(unittest.TestCase):
    """令牌比对必须是常量时间的 `secrets.compare_digest`，不是 `==`。

    放在这个文件里是因为它和判定同属「这道门的承重件」；端点行为在 _api 那份。
    """

    def source(self):
        from delivery import server

        return textwrap.dedent(inspect.getsource(server._service_for_token))

    def test_uses_compare_digest(self):
        tree = ast.parse(self.source())
        names = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        self.assertIn("compare_digest", names, names)

    def test_token_is_never_compared_with_equals(self):
        """`==` 会按字节短路返回，比对耗时随「前几位对了多少」变化 —— 够一位一位试出来。"""
        tree = ast.parse(self.source())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                continue
            used = {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
            self.assertFalse(used & {"token", "known"}, ast.dump(node))
