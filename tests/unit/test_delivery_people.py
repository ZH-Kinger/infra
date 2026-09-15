"""人员名册：union_id 是唯一绑定键，邮箱只允许首次登录辅助关联一次。

测试重点不在覆盖率，而在「认错人」的几条路：邮箱被复用、名册自身重复、
并发抢绑、绑定文件损坏。认错一次，用户看到的就是别人的权限清单。

测试数据全部虚构（example.com / wuji.tech 虚构前缀）。
"""

from __future__ import annotations

import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from delivery.people import (
    BIND_CONFLICT,
    BIND_NONE,
    BIND_NOW,
    BIND_UNION_ID,
    BINDINGS_SCHEMA,
    SCHEMA,
    AccountRef,
    DirectoryEntry,
    PeopleError,
    PeopleIndex,
    Person,
    apply_manual,
    build,
    fingerprint,
    load,
    parse,
)

DOMAIN = "wuji.tech"


def roster(*people, unlinked=None):
    return {
        "schema": SCHEMA,
        "generated_at": "2026-09-15T10:00:00+08:00",
        "people": list(people),
        "unlinked": list(unlinked or []),
    }


def row(name, email="", union_id="", accounts=None, pending=None, employee_no=""):
    return {
        "name": name,
        "email": email,
        "union_id": union_id,
        "employee_no": employee_no,
        "accounts": accounts or [],
        "pending": pending or [],
    }


ACC_A = {"platform": "aliyun", "account": "1000000000000001", "name": "zhangsan"}
ACC_V = {"platform": "volcano", "account": "2000000001", "name": "ZhangSan"}


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.bindings = self.dir / "identity" / "bindings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def index(self, *people):
        return parse(roster(*people), bindings_path=str(self.bindings))


# ── resolve ────────────────────────────────────────────────────────────────


class ResolveTests(_TmpDir):
    def test_union_id_hit(self):
        idx = self.index(row("张三", "zhangsan@wuji.tech", "on_u1", accounts=[ACC_A]))
        res = idx.resolve(union_id="on_u1", enterprise_email="whatever@wuji.tech")
        self.assertEqual(res.binding, BIND_UNION_ID)
        self.assertEqual(res.person.name, "张三")
        self.assertFalse(self.bindings.exists(), "union_id 命中不应写绑定文件")

    def test_email_binds_unbound_person_and_persists(self):
        idx = self.index(row("张三", "ZhangSan@wuji.tech", accounts=[ACC_A], pending=[ACC_V]))
        res = idx.resolve(union_id="on_new", enterprise_email=" zhangsan@WUJI.tech ")
        self.assertEqual(res.binding, BIND_NOW)
        self.assertEqual(res.person.union_id, "on_new")
        self.assertEqual(res.person.accounts[0].name, "zhangsan")

        # 文件 0600，schema @2，以 union_id 为键，存账号指纹（只含 confirmed，不含 pending）
        self.assertTrue(self.bindings.exists())
        self.assertEqual(stat.S_IMODE(self.bindings.stat().st_mode), 0o600)
        data = json.loads(self.bindings.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], BINDINGS_SCHEMA)
        self.assertEqual(list(data["bindings"]), ["on_new"])
        entry = data["bindings"]["on_new"]
        self.assertEqual(entry["accounts"], ["aliyun/1000000000000001/zhangsan"])
        self.assertEqual(entry["accounts"], list(fingerprint(res.person)))
        self.assertEqual(entry["email"], "zhangsan@wuji.tech")
        self.assertEqual(entry["name"], "张三")
        self.assertTrue(entry["bound_at"])
        self.assertFalse(Path(str(self.bindings) + ".tmp").exists(), "临时文件应被 rename 掉")

        # 审计行：追加写，0600
        log = self.bindings.with_suffix(".log")
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("\tbind\t", lines[0])
        self.assertIn("union_id=on_new", lines[0])
        self.assertIn("email=zhangsan@wuji.tech", lines[0])

        # 之后只认 union_id，哪怕不再给邮箱
        again = idx.resolve(union_id="on_new")
        self.assertEqual(again.binding, BIND_UNION_ID)
        self.assertEqual(again.person.name, "张三")

    def test_binding_survives_restart_via_load(self):
        people_path = self.dir / "people.json"
        people_path.write_text(
            json.dumps(roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A]))),
            encoding="utf-8",
        )
        idx = load(str(people_path), bindings_path=str(self.bindings))
        self.assertEqual(
            idx.resolve(union_id="on_new", enterprise_email="zhangsan@wuji.tech").binding,
            BIND_NOW,
        )
        # 进程重启：名册没变，绑定从 bindings.json 回填
        fresh = load(str(people_path), bindings_path=str(self.bindings))
        res = fresh.resolve(union_id="on_new")
        self.assertEqual(res.binding, BIND_UNION_ID)
        self.assertEqual(res.person.email, "zhangsan@wuji.tech")

    def test_second_binding_appends_audit_and_keeps_first(self):
        idx = self.index(
            row("张三", "zhangsan@wuji.tech", accounts=[ACC_A]),
            row("李四", "lisi@wuji.tech", accounts=[ACC_V]),
        )
        idx.resolve(union_id="on_a", enterprise_email="zhangsan@wuji.tech")
        idx.resolve(union_id="on_b", enterprise_email="lisi@wuji.tech")
        data = json.loads(self.bindings.read_text(encoding="utf-8"))
        self.assertEqual(
            {k: (v["email"], v["accounts"]) for k, v in data["bindings"].items()},
            {
                "on_a": ("zhangsan@wuji.tech", ["aliyun/1000000000000001/zhangsan"]),
                "on_b": ("lisi@wuji.tech", ["volcano/2000000001/zhangsan"]),
            },
        )
        self.assertEqual(len(self.bindings.with_suffix(".log").read_text().splitlines()), 2)

    def test_person_without_cloud_accounts_not_bound(self):
        idx = self.index(row("张三", "zhangsan@wuji.tech"))
        res = idx.resolve(union_id="on_x", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_NONE)
        self.assertIsNone(res.person)
        self.assertFalse(self.bindings.exists())
        self.assertFalse(self.bindings.with_suffix(".log").exists())
        # 仍是未绑定状态，管理员可用邮箱指代
        self.assertEqual(idx.by_key("email:zhangsan@wuji.tech").union_id, "")

    def test_pending_only_person_cannot_bind(self):
        # 指纹只算 confirmed：只有待确认账号的人指纹为空，不能按邮箱关联、不落绑定
        idx = self.index(row("张三", "zhangsan@wuji.tech", pending=[ACC_V]))
        res = idx.resolve(union_id="on_x", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_NONE)
        self.assertIsNone(res.person)
        self.assertFalse(self.bindings.exists())
        self.assertFalse(self.bindings.with_suffix(".log").exists())

    def test_fingerprint_only_confirmed_sorted(self):
        p = Person(
            name="x",
            accounts=(AccountRef("volcano", "2", "B"), AccountRef("aliyun", "1", "a")),
            pending=(AccountRef("aliyun", "1", "p", "review"),),
        )
        self.assertEqual(fingerprint(p), ("aliyun/1/a", "volcano/2/b"))
        self.assertEqual(fingerprint(Person(name="y", pending=p.pending)), ())

    def test_pending_change_does_not_lock_bound_person(self):
        # 绑定后 pending 增减不影响指纹，重启后仍按 union_id 认出
        people_path = self.dir / "people.json"
        people_path.write_text(
            json.dumps(roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A]))),
            encoding="utf-8",
        )
        load(str(people_path), bindings_path=str(self.bindings)).resolve(
            union_id="on_zs", enterprise_email="zhangsan@wuji.tech"
        )
        people_path.write_text(
            json.dumps(
                roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A], pending=[ACC_V]))
            ),
            encoding="utf-8",
        )
        idx = load(str(people_path), bindings_path=str(self.bindings))
        self.assertEqual(idx.resolve(union_id="on_zs").binding, BIND_UNION_ID)
        self.assertEqual(idx.warnings, [])

    def test_email_collision_flag_blocks_email_claim(self):
        r = {**row("张三", "shared@wuji.tech", accounts=[ACC_A]), "email_collision": True}
        idx = self.index(r)
        res = idx.resolve(union_id="on_x", enterprise_email="shared@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertFalse(self.bindings.exists())
        # 只有 True 才算冲突标记，字符串 "true" 不算
        idx2 = self.index({**r, "email_collision": "true"})
        self.assertEqual(
            idx2.resolve(union_id="on_x", enterprise_email="shared@wuji.tech").binding, BIND_NOW
        )

    def test_file_already_claims_same_fingerprint_is_conflict(self):
        # 另一个进程已把同一组账号绑给 on_other（本索引加载时还不知道）
        self.bindings.parent.mkdir(parents=True)
        self.bindings.write_text(
            json.dumps(
                {
                    "schema": BINDINGS_SCHEMA,
                    "bindings": {
                        "on_other": {
                            "accounts": ["aliyun/1000000000000001/zhangsan"],
                            "email": "old@wuji.tech",
                            "name": "某人",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        idx = parse(roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A])),
                    bindings_path=str(self.bindings))
        before = self.bindings.read_text(encoding="utf-8")
        res = idx.resolve(union_id="on_me", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)
        self.assertEqual(self.bindings.read_text(encoding="utf-8"), before)
        self.assertFalse(self.bindings.with_suffix(".log").exists())
        self.assertEqual(idx.by_key("email:zhangsan@wuji.tech").union_id, "")

    def test_rebinding_same_union_id_same_fingerprint_is_idempotent(self):
        # 文件里已有自己（同 union_id 同指纹），本索引未加载 → 允许，覆盖自己的条目
        self.bindings.parent.mkdir(parents=True)
        self.bindings.write_text(
            json.dumps(
                {
                    "schema": BINDINGS_SCHEMA,
                    "bindings": {"on_me": {"accounts": ["aliyun/1000000000000001/zhangsan"]}},
                }
            ),
            encoding="utf-8",
        )
        idx = parse(roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A])),
                    bindings_path=str(self.bindings))
        res = idx.resolve(union_id="on_me", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_NOW)
        data = json.loads(self.bindings.read_text(encoding="utf-8"))
        self.assertEqual(list(data["bindings"]), ["on_me"])

    def test_email_hits_person_bound_to_other_union_id_is_conflict(self):
        idx = self.index(row("张三", "zhangsan@wuji.tech", "on_old", accounts=[ACC_A]))
        res = idx.resolve(union_id="on_intruder", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)
        self.assertFalse(self.bindings.exists())
        self.assertFalse(self.bindings.with_suffix(".log").exists())
        # 不覆盖：原主人仍能登录，入侵者仍查不到
        self.assertEqual(idx.resolve(union_id="on_old").binding, BIND_UNION_ID)
        self.assertEqual(idx.resolve(union_id="on_intruder").binding, BIND_NONE)

    def test_email_maps_to_multiple_people_is_conflict(self):
        idx = self.index(
            row("张三", "shared@wuji.tech", accounts=[ACC_A]),
            row("张三丰", "Shared@wuji.tech", accounts=[ACC_V]),
        )
        res = idx.resolve(union_id="on_x", enterprise_email="shared@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)
        self.assertFalse(self.bindings.exists())

    def test_email_multiple_even_if_one_already_bound_is_conflict(self):
        idx = self.index(
            row("张三", "shared@wuji.tech", "on_z"),
            row("张三丰", "shared@wuji.tech"),
        )
        res = idx.resolve(union_id="on_x", enterprise_email="shared@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertFalse(self.bindings.exists())

    def test_no_enterprise_email_is_unbound(self):
        idx = self.index(row("张三", "zhangsan@wuji.tech"))
        for email in ("", "   ", None):
            res = idx.resolve(union_id="on_x", enterprise_email=email)
            self.assertEqual(res.binding, BIND_NONE)
            self.assertIsNone(res.person)
            self.assertIn("union_id", res.note)
        self.assertFalse(self.bindings.exists())

    def test_email_not_in_roster_is_unbound(self):
        idx = self.index(row("张三", "zhangsan@wuji.tech"))
        res = idx.resolve(union_id="on_x", enterprise_email="nobody@wuji.tech")
        self.assertEqual(res.binding, BIND_NONE)
        self.assertIsNone(res.person)

    def test_empty_union_id_is_unbound_even_if_email_matches(self):
        idx = self.index(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A]))
        res = idx.resolve(union_id="", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_NONE)
        self.assertIsNone(res.person)
        self.assertFalse(self.bindings.exists())
        # 且没有把空串绑到这个人身上
        self.assertIsNotNone(idx.by_key("email:zhangsan@wuji.tech"))

    def test_no_bindings_path_binds_in_memory_only(self):
        idx = parse(roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A])))
        self.assertEqual(
            idx.resolve(union_id="on_x", enterprise_email="zhangsan@wuji.tech").binding, BIND_NOW
        )
        self.assertEqual(idx.resolve(union_id="on_x").binding, BIND_UNION_ID)
        self.assertFalse(self.bindings.exists())

    def test_corrupt_bindings_file_fails_and_does_not_bind_in_memory(self):
        self.bindings.parent.mkdir(parents=True)
        for bad in (
            "{not json",
            json.dumps({"schema": "wuji-bindings@1", "bindings": {}}),
            json.dumps({"schema": BINDINGS_SCHEMA, "bindings": {"on_y": "zhangsan"}}),
        ):
            with self.subTest(bad=bad):
                self.bindings.write_text(bad, encoding="utf-8")
                idx = self.index(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A]))
                with self.assertRaises(PeopleError):
                    idx.resolve(union_id="on_x", enterprise_email="zhangsan@wuji.tech")
                # 落盘失败 → 内存也不能认为已绑定，否则重启后「绑定消失」
                self.assertEqual(idx.resolve(union_id="on_x").binding, BIND_NONE)
                self.assertEqual(self.bindings.read_text(encoding="utf-8"), bad)

    def test_bindings_file_missing_bindings_object_rejected(self):
        self.bindings.parent.mkdir(parents=True)
        self.bindings.write_text(json.dumps({"schema": "x"}), encoding="utf-8")
        with self.assertRaises(PeopleError):
            load_path = self.dir / "people.json"
            load_path.write_text(json.dumps(roster()), encoding="utf-8")
            load(str(load_path), bindings_path=str(self.bindings))

    def test_concurrent_claim_same_email_only_one_wins(self):
        for _ in range(20):
            if self.bindings.exists():
                self.bindings.unlink()
            idx = self.index(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A]))
            barrier = threading.Barrier(2)
            results = {}

            def claim(uid, idx=idx, barrier=barrier, results=results):
                barrier.wait()
                results[uid] = idx.resolve(union_id=uid, enterprise_email="zhangsan@wuji.tech")

            threads = [threading.Thread(target=claim, args=(u,)) for u in ("on_a", "on_b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            kinds = sorted(r.binding for r in results.values())
            self.assertEqual(kinds, sorted([BIND_NOW, BIND_CONFLICT]))
            winner = next(u for u, r in results.items() if r.binding == BIND_NOW)
            loser = next(u for u, r in results.items() if r.binding == BIND_CONFLICT)
            self.assertIsNone(results[loser].person)
            data = json.loads(self.bindings.read_text(encoding="utf-8"))
            self.assertEqual(list(data["bindings"]), [winner])
            self.assertEqual(idx.resolve(union_id=loser).binding, BIND_NONE)
            self.assertEqual(idx.resolve(union_id=winner).binding, BIND_UNION_ID)


# ── parse ─────────────────────────────────────────────────────────────────


class ParseTests(unittest.TestCase):
    def test_schema_required(self):
        for bad in (None, [], {}, {"schema": "wuji-people@0", "people": []}):
            with self.assertRaises(PeopleError):
                parse(bad)

    def test_people_must_be_list_of_objects(self):
        with self.assertRaises(PeopleError):
            parse({"schema": SCHEMA})
        with self.assertRaises(PeopleError):
            parse({"schema": SCHEMA, "people": {}})
        with self.assertRaises(PeopleError):
            parse({"schema": SCHEMA, "people": ["张三"]})

    def test_field_types(self):
        cases = [
            row("张三", email=123),
            row("张三", union_id=["on_x"]),
            {**row("张三"), "name": 1},
            {**row("张三"), "accounts": {"platform": "aliyun"}},
            {**row("张三"), "accounts": ["aliyun/1"]},
            {**row("张三"), "accounts": [{"platform": "aliyun", "account": 1000, "name": "z"}]},
            {**row("张三"), "accounts": [{"platform": "aliyun", "account": "1"}]},  # 缺 name
            {**row("张三"), "pending": [{"platform": "", "account": "1", "name": "z"}]},
        ]
        for i, bad in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(PeopleError):
                parse(roster(bad))

    def test_unlinked_type_and_defaults(self):
        with self.assertRaises(PeopleError):
            parse(roster(unlinked=["x"]))
        idx = parse(roster(unlinked=[{"platform": "aliyun", "account": "1", "name": "svc"}]))
        self.assertEqual(idx.unlinked[0].kind, "unknown")

    def test_status_defaults(self):
        idx = parse(
            roster(row("张三", "zhangsan@wuji.tech", "on_1", accounts=[ACC_A], pending=[ACC_V]))
        )
        p = idx.by_key("on_1")
        self.assertEqual(p.accounts[0].status, "confirmed")
        self.assertEqual(p.pending[0].status, "review")
        self.assertEqual(p.accounts[0].scope, "aliyun/1000000000000001")
        self.assertEqual(idx.generated_at, "2026-09-15T10:00:00+08:00")

    def test_strings_are_stripped(self):
        idx = parse(roster(row(" 张三 ", " zhangsan@wuji.tech ", " on_1 ")))
        p = idx.by_key("on_1")
        self.assertEqual((p.name, p.email), ("张三", "zhangsan@wuji.tech"))

    def test_bindings_backfill_union_id_by_fingerprint(self):
        bindings = {
            "schema": BINDINGS_SCHEMA,
            "bindings": {
                "on_b": {
                    "accounts": [
                        "aliyun/1000000000000001/zhangsan",
                        "volcano/2000000001/ZhangSan",
                    ],
                    "email": " ZhangSan@WUJI.tech ",  # 比对时 strip + 小写
                    "name": "张三",
                }
            },
        }
        idx = parse(
            roster(
                # 指纹不同：不算（同一账号不能再确认给第二个人，否则会被降级）
                row("李四", "lisi@wuji.tech", accounts=[{**ACC_A, "name": "lisi"}]),
                # 顺序与绑定里不同，排序后一致；pending 不进指纹
                row(
                    "张三",
                    "zhangsan@wuji.tech",
                    accounts=[ACC_V, ACC_A],
                    pending=[{**ACC_A, "name": "extra"}],
                ),
            ),
            bindings=bindings,
        )
        self.assertEqual(idx.by_key("on_b").name, "张三")
        self.assertEqual(idx.warnings, [])
        self.assertIsNone(idx.by_key("email:zhangsan@wuji.tech"), "已绑定的人不再能用邮箱指代")
        self.assertEqual(idx.by_key("email:lisi@wuji.tech").union_id, "")

    def test_binding_email_mismatch_or_missing_blocks(self):
        fp = ["aliyun/1000000000000001/zhangsan"]
        for entry in (
            {"accounts": fp, "email": "someone-else@wuji.tech"},
            {"accounts": fp},
            {"accounts": fp, "email": ""},
        ):
            with self.subTest(entry=entry):
                idx = parse(
                    roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A])),
                    bindings={"schema": BINDINGS_SCHEMA, "bindings": {"on_b": entry}},
                )
                self.assertEqual(idx.people[0].union_id, "")
                self.assertEqual(len([w for w in idx.warnings if "对不上" in w]), 1)
                res = idx.resolve(union_id="on_b", enterprise_email="zhangsan@wuji.tech")
                self.assertEqual(res.binding, BIND_CONFLICT)
                # 这组账号有待核对的历史绑定：别的身份也不能按邮箱认领
                other = idx.resolve(union_id="on_c", enterprise_email="zhangsan@wuji.tech")
                self.assertEqual(other.binding, BIND_CONFLICT)
                self.assertIsNone(other.person)

    def test_username_reuse_binding_does_not_land_on_new_owner(self):
        """R1：用户名 p 改名为 p1 后，旧名 p 分给了新人 N。on_P 的旧绑定指纹是 aliyun/1/p，
        与 N 的指纹一致，但邮箱不一致 → 不能套到 N 身上；on_P 登录判冲突。"""
        idx = parse(
            roster(
                row("新人", "n@wuji.tech", accounts=[{"platform": "aliyun", "account": "1",
                                                     "name": "p"}]),
                row("彼得", "p@wuji.tech", accounts=[{"platform": "aliyun", "account": "1",
                                                     "name": "p1"}]),
            ),
            bindings={
                "schema": BINDINGS_SCHEMA,
                "bindings": {"on_P": {"accounts": ["aliyun/1/p"], "email": "p@wuji.tech"}},
            },
        )
        res = idx.resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)
        self.assertEqual(idx.by_key("email:n@wuji.tech").union_id, "")
        self.assertEqual(idx.by_key("email:p@wuji.tech").union_id, "")

    def test_binding_not_applied_by_email_when_fingerprint_differs(self):
        bindings = {
            "schema": BINDINGS_SCHEMA,
            "bindings": {
                "on_b": {"accounts": ["aliyun/9/other"], "email": "zhangsan@wuji.tech"}
            },
        }
        idx = parse(
            roster(row("张三", "zhangsan@wuji.tech", accounts=[ACC_A])), bindings=bindings
        )
        self.assertEqual(idx.by_key("email:zhangsan@wuji.tech").union_id, "")
        self.assertEqual(len(idx.warnings), 1)
        self.assertIn("on_b", idx.warnings[0])  # 无 name 时用 union_id 指代
        res = idx.resolve(union_id="on_b", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)

    def test_binding_with_ambiguous_fingerprint_blocked(self):
        bindings = {
            "schema": BINDINGS_SCHEMA,
            "bindings": {
                "on_b": {
                    "accounts": ["aliyun/1000000000000001/zhangsan"],
                    "email": "a@wuji.tech",
                    "name": "张三",
                }
            },
        }
        idx = parse(
            roster(
                row("张三", "a@wuji.tech", accounts=[ACC_A]),
                row("张三", "b@wuji.tech", accounts=[ACC_A]),
            ),
            bindings=bindings,
        )
        self.assertTrue(all(p.union_id == "" for p in idx.people))
        self.assertEqual(idx.resolve(union_id="on_b").binding, BIND_CONFLICT)
        # 同一账号确认给了两人 → 先降级（指纹变空），绑定随之对不上
        self.assertTrue(all(p.accounts == () for p in idx.people))
        self.assertEqual(len([w for w in idx.warnings if "多个人" in w]), 1)
        self.assertEqual(len([w for w in idx.warnings if "对不上" in w]), 1)

    def test_empty_fingerprint_binding_never_applied(self):
        bindings = {"schema": BINDINGS_SCHEMA, "bindings": {"on_b": {"accounts": []}}}
        idx = parse(roster(row("无号", "n@wuji.tech")), bindings=bindings)
        self.assertEqual(idx.people[0].union_id, "")
        self.assertEqual(idx.resolve(union_id="on_b", enterprise_email="n@wuji.tech").binding,
                         BIND_CONFLICT)

    def test_bound_person_uses_bound_union_id_not_other_bindings(self):
        # 两条绑定、两个人，各按指纹对上自己
        bindings = {
            "schema": BINDINGS_SCHEMA,
            "bindings": {
                "on_a": {"accounts": ["aliyun/1000000000000001/zhangsan"], "email": "b@wuji.tech"},
                "on_v": {"accounts": ["volcano/2000000001/ZhangSan"], "email": "a@wuji.tech"},
            },
        }
        idx = parse(
            roster(
                row("甲", "a@wuji.tech", accounts=[ACC_V]),
                row("乙", "b@wuji.tech", accounts=[ACC_A]),
            ),
            bindings=bindings,
        )
        self.assertEqual(idx.by_key("on_a").name, "乙")
        self.assertEqual(idx.by_key("on_v").name, "甲")
        self.assertEqual(idx.warnings, [])


class DemoteSharedAccountsTests(unittest.TestCase):
    def test_shared_confirmed_account_demoted_for_everyone(self):
        shared_upper = {**ACC_A, "name": "ZhangSan"}  # 大小写不同也算同一个号
        idx = parse(
            roster(
                row("甲", "a@wuji.tech", "on_a", accounts=[ACC_A, ACC_V]),
                row("乙", "b@wuji.tech", "on_b", accounts=[shared_upper]),
                row("丙", "c@wuji.tech", "on_c", accounts=[{**ACC_A, "name": "other"}]),
            )
        )
        a, b, c = idx.by_key("on_a"), idx.by_key("on_b"), idx.by_key("on_c")
        self.assertEqual([r.name for r in a.accounts], ["ZhangSan"])  # 火山号不受影响
        self.assertEqual(a.accounts[0].platform, "volcano")
        self.assertEqual([(r.name, r.status) for r in a.pending], [("zhangsan", "shared")])
        self.assertEqual(b.accounts, ())
        self.assertEqual([(r.name, r.status) for r in b.pending], [("ZhangSan", "shared")])
        self.assertEqual([r.name for r in c.accounts], ["other"])
        self.assertEqual(c.pending, ())
        shared_w = [w for w in idx.warnings if "多个人" in w]
        self.assertEqual(len(shared_w), 1)
        self.assertIn("aliyun/1000000000000001/zhangsan", shared_w[0])
        self.assertIn("降为待确认", shared_w[0])

    def test_one_warning_per_shared_account(self):
        acc2 = {"platform": "aliyun", "account": "1000000000000002", "name": "ops"}
        idx = parse(
            roster(
                row("甲", "a@wuji.tech", accounts=[ACC_A, acc2]),
                row("乙", "b@wuji.tech", accounts=[ACC_A, acc2]),
                row("丙", "c@wuji.tech", accounts=[ACC_A]),
            )
        )
        self.assertEqual(len([w for w in idx.warnings if "多个人" in w]), 2)
        self.assertTrue(all(p.accounts == () for p in idx.people))
        self.assertEqual(len(idx.people[2].pending), 1)

    def test_same_person_listing_account_twice_is_not_shared(self):
        idx = parse(roster(row("甲", "a@wuji.tech", "on_a", accounts=[ACC_A, ACC_A])))
        self.assertEqual(len(idx.by_key("on_a").accounts), 2)
        self.assertEqual(idx.warnings, [])

    def test_pending_duplicates_are_not_demotion(self):
        idx = parse(
            roster(
                row("甲", "a@wuji.tech", "on_a", accounts=[ACC_A]),
                row("乙", "b@wuji.tech", "on_b", pending=[ACC_A]),
            )
        )
        self.assertEqual(len(idx.by_key("on_a").accounts), 1)
        self.assertEqual(idx.warnings, [])

    def test_demoted_people_cannot_bind_by_email(self):
        idx = parse(
            roster(
                row("甲", "a@wuji.tech", accounts=[ACC_A]),
                row("乙", "b@wuji.tech", accounts=[ACC_A]),
            )
        )
        for mail in ("a@wuji.tech", "b@wuji.tech"):
            res = idx.resolve(union_id=f"on_{mail[0]}", enterprise_email=mail)
            self.assertEqual(res.binding, BIND_NONE)
            self.assertIsNone(res.person)

    def test_roster_wins_over_bindings_with_warning(self):
        bindings = {
            "schema": BINDINGS_SCHEMA,
            "bindings": {"on_roster": {"accounts": ["aliyun/9/stale"], "name": "张三"}},
        }
        idx = parse(
            roster(row("张三", "zhangsan@wuji.tech", "on_roster", accounts=[ACC_A])),
            bindings=bindings,
        )
        p = idx.by_key("on_roster")
        self.assertEqual(p.name, "张三")
        self.assertEqual([a.name for a in p.accounts], ["zhangsan"])
        self.assertEqual(len(idx.warnings), 1)
        self.assertIn("张三", idx.warnings[0])
        self.assertEqual(idx.resolve(union_id="on_roster").binding, BIND_UNION_ID)

    def test_roster_union_id_same_fingerprint_no_warning(self):
        bindings = {
            "schema": BINDINGS_SCHEMA,
            "bindings": {"on_roster": {"accounts": ["aliyun/1000000000000001/zhangsan"]}},
        }
        idx = parse(
            roster(row("张三", "zhangsan@wuji.tech", "on_roster", accounts=[ACC_A])),
            bindings=bindings,
        )
        self.assertEqual(idx.warnings, [])

    def test_empty_bindings_arg_is_fine(self):
        idx = parse(roster(row("张三", "zhangsan@wuji.tech")), bindings={})
        self.assertIsNotNone(idx.by_key("email:zhangsan@wuji.tech"))

    def test_duplicate_union_id_disables_both_with_single_warning(self):
        idx = parse(
            roster(
                row("张三", "zhangsan@wuji.tech", "on_dup", accounts=[ACC_A]),
                row("李四", "lisi@wuji.tech", "on_dup", accounts=[ACC_V]),
                row("王五", "wangwu@wuji.tech", accounts=[{**ACC_A, "name": "wangwu"}]),
            )
        )
        self.assertIsNone(idx.by_key("on_dup"))
        res = idx.resolve(union_id="on_dup")
        self.assertIsNone(res.person)
        self.assertEqual(res.binding, BIND_CONFLICT)
        dup_warnings = [w for w in idx.warnings if "on_dup" in w]
        self.assertEqual(len(dup_warnings), 1)

        # 触发一次重建索引（别人首次登录绑定），警告不重复刷，停用状态保留
        self.assertEqual(
            idx.resolve(union_id="on_w", enterprise_email="wangwu@wuji.tech").binding, BIND_NOW
        )
        self.assertEqual(len([w for w in idx.warnings if "on_dup" in w]), 1)
        self.assertIsNone(idx.by_key("on_dup"))
        self.assertEqual(idx.resolve(union_id="on_dup").binding, BIND_CONFLICT)

    def test_duplicate_union_id_by_email_path_still_conflicts(self):
        # 名册里重复的 union_id 持有人拿自己的企业邮箱登录，不能被邮箱路径「救活」
        idx = parse(
            roster(
                row("张三", "zhangsan@wuji.tech", "on_dup"),
                row("李四", "lisi@wuji.tech", "on_dup"),
            )
        )
        res = idx.resolve(union_id="on_dup", enterprise_email="zhangsan@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)


# ── by_key ────────────────────────────────────────────────────────────────


class ByKeyTests(unittest.TestCase):
    def setUp(self):
        self.idx = parse(
            roster(
                row("张三", "zhangsan@wuji.tech", "on_1"),
                row("李四", "LiSi@wuji.tech"),
                row("甲", "shared@wuji.tech"),
                row("乙", "shared@wuji.tech"),
                row("丙", "half@wuji.tech", "on_3"),
                row("丁", "half@wuji.tech"),
            )
        )

    def test_union_id_key(self):
        self.assertEqual(self.idx.by_key("on_1").name, "张三")
        self.assertIsNone(self.idx.by_key("on_missing"))
        self.assertIsNone(self.idx.by_key(""))

    def test_email_key_returns_unbound_unique(self):
        self.assertEqual(self.idx.by_key("email:lisi@WUJI.tech").name, "李四")

    def test_email_key_skips_bound_person(self):
        self.assertIsNone(self.idx.by_key("email:zhangsan@wuji.tech"))

    def test_email_key_ambiguous_returns_none(self):
        self.assertIsNone(self.idx.by_key("email:shared@wuji.tech"))

    def test_email_key_one_bound_one_unbound_returns_unbound(self):
        self.assertEqual(self.idx.by_key("email:half@wuji.tech").name, "丁")

    def test_person_key_roundtrip(self):
        for p in self.idx.people:
            if p.name in ("甲", "乙"):
                continue
            self.assertIs(self.idx.by_key(p.key), p)
        self.assertEqual(Person(name="x", email="A@Example.com").key, "email:a@example.com")

    def test_uid_that_looks_like_email_prefix_not_confused(self):
        idx = PeopleIndex([Person(name="x", email="", union_id="email:x@wuji.tech")])
        # union_id 形如 email: 前缀时 by_key 走邮箱分支；不应返回这个人
        self.assertIsNone(idx.by_key("email:x@wuji.tech"))


# ── build ─────────────────────────────────────────────────────────────────


def link(scope, name, status="confirmed", display_name=""):
    return {"scope": scope, "name": name, "display_name": display_name, "status": status}


class BuildTests(unittest.TestCase):
    def proposal(self, people=(), unlinked=(), services=(), domain=DOMAIN):
        return {
            "schema": "wuji-sso-map/proposal@1",
            "domain": domain,
            "people": list(people),
            "unlinked": list(unlinked),
            "services": list(services),
        }

    def by_email(self, out):
        return {p["email"]: p for p in out["people"]}

    def test_confirmed_to_accounts_others_to_pending(self):
        out = build(
            self.proposal(
                [
                    {
                        "email": "zhangsan@wuji.tech",
                        "links": [
                            link("aliyun/1000000000000001", "zhangsan"),
                            link("volcano/2000000001", "ZhangSan", "review"),
                            link("volcano/2000000002", "zs", "blocked"),
                            link("aliyun/1000000000000002", "zs2", ""),
                        ],
                    }
                ]
            )
        )
        p = out["people"][0]
        self.assertEqual(
            p["accounts"],
            [{"platform": "aliyun", "account": "1000000000000001", "name": "zhangsan"}],
        )
        self.assertEqual(
            [(x["account"], x["status"]) for x in p["pending"]],
            [("2000000001", "review"), ("2000000002", "blocked"), ("1000000000000002", "review")],
        )
        self.assertEqual(out["schema"], SCHEMA)
        self.assertTrue(out["generated_at"])

    def test_backfill_union_id_by_enterprise_email(self):
        out = build(
            self.proposal(
                [{"email": "ZhangSan@wuji.tech", "links": [link("aliyun/1", "zhangsan")]}]
            ),
            [DirectoryEntry("on_1", "张三", "zhangsan@WUJI.tech", "E001")],
        )
        p = self.by_email(out)["zhangsan@wuji.tech"]
        self.assertEqual((p["union_id"], p["name"], p["employee_no"]), ("on_1", "张三", "E001"))
        self.assertEqual(out["stats"]["with_union_id"], 1)
        self.assertEqual(len(out["people"]), 1, "已回填的人不应再以通讯录条目重复进名册")

    def test_non_company_domain_not_backfilled(self):
        out = build(
            self.proposal([{"email": "zhangsan@example.com", "links": [link("aliyun/1", "zs")]}]),
            [DirectoryEntry("on_1", "张三", "zhangsan@example.com")],
        )
        p = self.by_email(out)["zhangsan@example.com"]
        self.assertEqual(p["union_id"], "")
        self.assertEqual(p["name"], "zhangsan")

    def test_domain_with_at_and_case_normalized(self):
        prop = self.proposal(
            [{"email": "zhangsan@wuji.tech", "links": [link("aliyun/1", "zs")]}],
            domain=" @WUJI.tech ",
        )
        out = build(prop, [DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech")])
        self.assertEqual(out["people"][0]["union_id"], "on_1")

    def test_lookalike_domain_not_backfilled(self):
        out = build(
            self.proposal([{"email": "zhangsan@evilwuji.tech", "links": [link("aliyun/1", "zs")]}]),
            [DirectoryEntry("on_1", "张三", "zhangsan@evilwuji.tech")],
        )
        self.assertEqual(self.by_email(out)["zhangsan@evilwuji.tech"]["union_id"], "")

    def test_directory_email_collision_not_backfilled(self):
        out = build(
            self.proposal([{"email": "shared@wuji.tech", "links": [link("aliyun/1", "shared")]}]),
            [
                DirectoryEntry("on_1", "张三", "shared@wuji.tech"),
                DirectoryEntry("on_2", "李四", "Shared@wuji.tech"),
            ],
        )
        p = self.by_email(out)["shared@wuji.tech"]
        self.assertEqual(p["union_id"], "")
        self.assertEqual(out["stats"]["email_collisions_in_directory"], ["shared@wuji.tech"])

    def test_same_entry_twice_is_not_collision(self):
        e = DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech")
        out = build(
            self.proposal([{"email": "zhangsan@wuji.tech", "links": [link("aliyun/1", "zs")]}]),
            [e, e],
        )
        self.assertEqual(out["people"][0]["union_id"], "on_1")
        self.assertEqual(out["stats"]["email_collisions_in_directory"], [])

    def test_directory_people_without_cloud_accounts_included(self):
        out = build(
            self.proposal([{"email": "zhangsan@wuji.tech", "links": [link("aliyun/1", "zs")]}]),
            [
                DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech"),
                DirectoryEntry("on_2", "李四", "LiSi@wuji.tech", "E002"),
                DirectoryEntry("on_3", "王五", ""),
                DirectoryEntry("", "无ID", "noid@wuji.tech"),
            ],
        )
        got = {p["union_id"]: p for p in out["people"]}
        self.assertEqual(set(got), {"on_1", "on_2", "on_3"})
        self.assertEqual(got["on_2"]["accounts"], [])
        self.assertEqual(got["on_2"]["pending"], [])
        self.assertEqual(got["on_2"]["email"], "lisi@wuji.tech")
        self.assertEqual(out["stats"]["people"], 3)
        self.assertEqual(out["stats"]["with_cloud_account"], 1)
        self.assertEqual(out["stats"]["with_union_id"], 3)

    def test_directory_duplicate_union_id_listed_once(self):
        out = build(
            self.proposal(),
            [
                DirectoryEntry("on_2", "李四", "lisi@wuji.tech"),
                DirectoryEntry("on_2", "李四", "lisi@wuji.tech"),
            ],
        )
        self.assertEqual([p["union_id"] for p in out["people"]], ["on_2"])

    def test_unlinked_and_services(self):
        out = build(
            self.proposal(
                unlinked=[
                    {
                        "scope": "aliyun/1",
                        "name": "ghost",
                        "display_name": "某人",
                        "reason": "无邮箱",
                    }
                ],
                services=[{"scope": "volcano/2", "name": "svc-ci"}],
            )
        )
        kinds = {u["name"]: u for u in out["unlinked"]}
        self.assertEqual(kinds["ghost"]["kind"], "unknown")
        self.assertEqual(kinds["ghost"]["reason"], "无邮箱")
        self.assertEqual(kinds["svc-ci"]["kind"], "service")
        self.assertEqual(
            (kinds["svc-ci"]["platform"], kinds["svc-ci"]["account"]), ("volcano", "2")
        )

    def test_display_name_fallback(self):
        out = build(
            self.proposal(
                [{"email": "zs@wuji.tech", "links": [link("aliyun/1", "zs", display_name="张三")]}]
            )
        )
        self.assertEqual(out["people"][0]["name"], "张三")

    def test_build_output_parses_back(self):
        out = build(
            self.proposal(
                [
                    {"email": "zhangsan@wuji.tech", "links": [link("aliyun/1", "zs")]},
                    {"email": "lisi@wuji.tech", "links": [link("volcano/2", "ls", "review")]},
                ],
                unlinked=[{"scope": "aliyun/1", "name": "ghost", "reason": "x"}],
                services=[{"scope": "aliyun/1", "name": "svc"}],
            ),
            [
                DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech"),
                DirectoryEntry("on_9", "赵六", "zhaoliu@wuji.tech"),
            ],
        )
        idx = parse(json.loads(json.dumps(out, ensure_ascii=False)))
        self.assertEqual(idx.by_key("on_1").accounts[0].scope, "aliyun/1")
        self.assertEqual(idx.by_key("on_9").accounts, ())
        lisi = idx.by_key("email:lisi@wuji.tech")
        self.assertEqual(lisi.pending[0].status, "review")
        self.assertEqual({u.kind for u in idx.unlinked}, {"unknown", "service"})
        # 李四只有待确认账号：指纹为空，不能按邮箱关联
        self.assertEqual(
            idx.resolve(union_id="on_l", enterprise_email="lisi@wuji.tech").binding, BIND_NONE
        )

    def test_directory_collision_is_not_claimable_at_login(self):
        """通讯录两人共用企业邮箱：build 不回填，名册留下冲突标记，登录时谁都不能按邮箱认领。"""
        out = build(
            self.proposal([{"email": "shared@wuji.tech", "links": [link("aliyun/1", "shared")]}]),
            [
                DirectoryEntry("on_1", "张三", "shared@wuji.tech"),
                DirectoryEntry("on_2", "李四", "shared@wuji.tech"),
            ],
        )
        idx = parse(out)
        res = idx.resolve(union_id="on_2", enterprise_email="shared@wuji.tech")
        self.assertNotEqual(res.binding, BIND_NOW)


    def test_empty_domain_never_backfills(self):
        # 没有公司域就分不清企业邮箱和个人联系邮箱，宁可不回填
        out = build(
            self.proposal(
                [{"email": "zhangsan@wuji.tech", "links": [link("aliyun/1", "zs")]}], domain=""
            ),
            [DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech")],
        )
        got = self.by_email(out)["zhangsan@wuji.tech"]
        self.assertEqual(got["union_id"], "")
        self.assertIs(got["email_collision"], False)

    def test_email_collision_flag_written_and_read_back(self):
        out = build(
            self.proposal([{"email": "shared@wuji.tech", "links": [link("aliyun/1", "shared")]}]),
            [
                DirectoryEntry("on_1", "张三", "shared@wuji.tech"),
                DirectoryEntry("on_2", "李四", "shared@wuji.tech"),
            ],
        )
        self.assertIs(self.by_email(out)["shared@wuji.tech"]["email_collision"], True)
        idx = parse(json.loads(json.dumps(out, ensure_ascii=False)))
        for uid in ("on_1", "on_2"):
            self.assertEqual(
                idx.resolve(union_id=uid, enterprise_email="shared@wuji.tech").binding,
                BIND_CONFLICT,
            )


class ApplyManualTests(unittest.TestCase):
    ALI = "aliyun/1000000000000001"
    VOLC = "volcano/2000000001"

    def proposal(self):
        return {
            "schema": "wuji-sso-map/proposal@1",
            "domain": DOMAIN,
            "people": [
                {
                    "email": "wrong@wuji.tech",
                    "links": [
                        {"scope": self.ALI, "name": "zhangsan", "status": "review"},
                        {"scope": self.ALI, "name": "wrong", "status": "confirmed"},
                    ],
                },
                {
                    "email": "only@wuji.tech",
                    "links": [{"scope": self.VOLC, "name": "OnlyOne", "status": "blocked"}],
                },
                {
                    "email": "zhangsan@wuji.tech",
                    "links": [{"scope": self.ALI, "name": "zs-old", "status": "confirmed"}],
                },
            ],
            "unlinked": [
                {"scope": self.VOLC, "name": "SanZhang", "reason": "无邮箱"},
                {"scope": self.VOLC, "name": "ghost", "reason": "无邮箱"},
            ],
            "services": [
                {"scope": self.ALI, "name": "svc-ci"},
                {"scope": self.ALI, "name": "svc-x"},
            ],
        }

    def people_by_email(self, out):
        return {r["email"]: r for r in out["people"]}

    def test_empty_manual_returns_copy(self):
        prop = self.proposal()
        for manual in (None, {}):
            out = apply_manual(prop, manual)
            self.assertEqual(out, prop)
            self.assertIsNot(out, prop)

    def test_moves_accounts_from_everywhere_to_email_as_confirmed(self):
        prop = self.proposal()
        manual = {
            "links": {
                " ZhangSan@WUJI.tech ": {
                    "name": "张三",
                    "accounts": [
                        f"{self.ALI}/zhangsan",  # 从别人的 links 摘
                        f"{self.VOLC}/SanZhang",  # 从 unlinked 摘
                        f"{self.ALI}/svc-ci",  # 从 services 摘
                        f"{self.VOLC}/OnlyOne",  # 摘完后原主人没有 link → 整行移除
                    ],
                }
            }
        }
        out = apply_manual(prop, manual)
        people = self.people_by_email(out)
        self.assertEqual(set(people), {"wrong@wuji.tech", "zhangsan@wuji.tech"})
        self.assertEqual([lk["name"] for lk in people["wrong@wuji.tech"]["links"]], ["wrong"])
        zs = people["zhangsan@wuji.tech"]["links"]
        self.assertEqual(
            sorted((lk["scope"], lk["name"]) for lk in zs),
            sorted(
                [
                    (self.ALI, "zs-old"),
                    (self.ALI, "zhangsan"),
                    (self.VOLC, "SanZhang"),
                    (self.ALI, "svc-ci"),
                    (self.VOLC, "OnlyOne"),
                ]
            ),
        )
        manual_links = [lk for lk in zs if lk["name"] != "zs-old"]
        self.assertTrue(all(lk["status"] == "confirmed" for lk in manual_links))
        self.assertTrue(all(lk["evidence"] == ["人工确认"] for lk in manual_links))
        self.assertTrue(all(lk["display_name"] == "张三" for lk in manual_links))
        self.assertEqual([u["name"] for u in out["unlinked"]], ["ghost"])
        self.assertEqual([u["name"] for u in out["services"]], ["svc-x"])
        # 不改入参
        self.assertEqual(prop, self.proposal())
        self.assertEqual(out["domain"], DOMAIN)

    def test_new_person_created_when_email_absent(self):
        out = apply_manual(
            self.proposal(),
            {"links": {"new.person@wuji.tech": {"accounts": [f"{self.VOLC}/ghost"]}}},
        )
        new = self.people_by_email(out)["new.person@wuji.tech"]
        self.assertEqual(
            new["links"],
            [
                {
                    "scope": self.VOLC,
                    "name": "ghost",
                    "display_name": "",
                    "status": "confirmed",
                    "evidence": ["人工确认"],
                }
            ],
        )
        self.assertEqual(out["unlinked"], [self.proposal()["unlinked"][0]])

    def test_account_not_in_proposal_still_attached(self):
        out = apply_manual(
            self.proposal(), {"links": {"zhangsan@wuji.tech": {"accounts": ["aliyun/9/extra"]}}}
        )
        names = [lk["name"] for lk in self.people_by_email(out)["zhangsan@wuji.tech"]["links"]]
        self.assertIn("extra", names)

    def test_same_account_listed_twice_for_same_person_is_ok(self):
        acc = f"{self.ALI}/zhangsan"
        out = apply_manual(
            self.proposal(),
            {
                "links": {
                    "zhangsan@wuji.tech": {"accounts": [acc, acc]},
                    "ZHANGSAN@wuji.tech": {"accounts": [acc]},
                }
            },
        )
        links = self.people_by_email(out)["zhangsan@wuji.tech"]["links"]
        self.assertEqual(sum(1 for lk in links if lk["name"] == "zhangsan"), 1)

    def test_account_string_and_proposal_fields_are_stripped(self):
        prop = self.proposal()
        prop["people"][0]["links"][0]["scope"] = f" {self.ALI} "
        prop["people"][0]["links"][0]["name"] = " zhangsan "
        prop["unlinked"][0]["name"] = "SanZhang "
        out = apply_manual(
            prop,
            {
                "links": {
                    "zhangsan@wuji.tech": {
                        "accounts": [f" {self.ALI} / zhangsan ", f"{self.VOLC}/ SanZhang"]
                    }
                }
            },
        )
        people = self.people_by_email(out)
        self.assertEqual([lk["name"] for lk in people["wrong@wuji.tech"]["links"]], ["wrong"])
        self.assertEqual([u["name"] for u in out["unlinked"]], ["ghost"])
        added = {(lk["scope"], lk["name"]) for lk in people["zhangsan@wuji.tech"]["links"]}
        self.assertIn((self.ALI, "zhangsan"), added)
        self.assertIn((self.VOLC, "SanZhang"), added)

    def test_space_or_case_variants_never_confirmed_for_two_people(self):
        """R2：manual 把 " aliyun/1/bob"（带空格）给 A，提案/manual 把 "Bob"（大小写不同）给 B，
        最终名册里不能两人同时 confirmed 这个号。"""
        prop = {
            "domain": DOMAIN,
            "people": [
                {
                    "email": "b@wuji.tech",
                    "links": [{"scope": "aliyun/1", "name": "Bob", "status": "confirmed"}],
                }
            ],
        }
        variants = [
            {"links": {"a@wuji.tech": {"accounts": [" aliyun/1/bob"]}}},
            {
                "links": {
                    "a@wuji.tech": {"accounts": [" aliyun/1/bob"]},
                    "b@wuji.tech": {"accounts": ["aliyun/1/Bob"]},
                }
            },
        ]
        for manual in variants:
            with self.subTest(manual=manual):
                idx = parse(build(apply_manual(prop, manual)))
                owners = [
                    p.email
                    for p in idx.people
                    for r in p.accounts
                    if (r.platform, r.account, r.name.lower()) == ("aliyun", "1", "bob")
                ]
                self.assertLessEqual(len(owners), 1, owners)
                # 带空格那条被归一，不会以 " aliyun" 平台名混进名册
                self.assertFalse(
                    any(r.platform != r.platform.strip() for p in idx.people for r in p.pending)
                )

    def test_same_account_to_two_people_raises(self):
        acc = f"{self.ALI}/zhangsan"
        with self.assertRaises(PeopleError):
            apply_manual(
                self.proposal(),
                {
                    "links": {
                        "a@wuji.tech": {"accounts": [acc]},
                        "b@wuji.tech": {"accounts": [acc]},
                    }
                },
            )

    def test_bad_format_raises(self):
        cases = [
            {"links": []},
            {"links": "a@wuji.tech"},
            {"other": {}},
            ["links"],
            {"links": {"a@wuji.tech": ["aliyun/1/x"]}},
            {"links": {"a@wuji.tech": {}}},
            {"links": {"a@wuji.tech": {"accounts": []}}},
            {"links": {"a@wuji.tech": {"accounts": "aliyun/1/x"}}},
            {"links": {"a@wuji.tech": {"accounts": ["aliyun/1"]}}},
            {"links": {"a@wuji.tech": {"accounts": ["aliyun/1/x/y"]}}},
            {"links": {"a@wuji.tech": {"accounts": ["aliyun//x"]}}},
            {"links": {"a@wuji.tech": {"accounts": ["/1/x"]}}},
        ]
        for i, manual in enumerate(cases):
            with self.subTest(i=i), self.assertRaises(PeopleError):
                apply_manual(self.proposal(), manual)

    def test_result_builds_into_roster_with_accounts_confirmed(self):
        out = apply_manual(
            self.proposal(),
            {
                "links": {
                    "zhangsan@wuji.tech": {"name": "张三", "accounts": [f"{self.VOLC}/SanZhang"]}
                }
            },
        )
        roster_data = build(out, [DirectoryEntry("on_zs", "张三", "zhangsan@wuji.tech")])
        idx = parse(roster_data)
        zs = idx.by_key("on_zs")
        self.assertIn(("volcano", "2000000001", "SanZhang"),
                      [(a.platform, a.account, a.name) for a in zs.accounts])
        self.assertNotIn("SanZhang", [u.name for u in idx.unlinked])


class BindingHoleTests(unittest.TestCase):
    def test_changed_accounts_row_not_claimable_by_another_identity(self):
        """回归：P 已绑定 [A]；名册重新生成后 P 那一行变成 [A, X]，P 的绑定被停用。
        这一行不能被拿到同一企业邮箱（邮箱复用）的新人 Q 按邮箱认领。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        people, bindings = d / "people.json", d / "bindings.json"
        acc_x = {"platform": "volcano", "account": "2000000009", "name": "Peter"}
        people.write_text(json.dumps(roster(row("彼得", "p@wuji.tech", accounts=[ACC_A]))))
        first = load(str(people), bindings_path=str(bindings))
        self.assertEqual(first.resolve(union_id="on_P", enterprise_email="p@wuji.tech").binding,
                         BIND_NOW)
        people.write_text(
            json.dumps(roster(row("彼得", "p@wuji.tech", accounts=[ACC_A, acc_x])))
        )
        idx = load(str(people), bindings_path=str(bindings))
        res = idx.resolve(union_id="on_Q", enterprise_email="p@wuji.tech")
        self.assertNotEqual(res.binding, BIND_NOW)


    def test_case_only_username_change_still_blocks_other_identity(self):
        """已知缺口：_demote_shared_accounts（people.py:411）按用户名小写比对，但
        blocked_accounts 交集（people.py:259）和 _persist_binding 的占用复核都是大小写敏感的。
        P 绑定 aliyun/…/Peter；名册重新生成后同一个号写成 peter 且多了一个火山号 →
        P 被停用（正确），但新人 Q 拿同一企业邮箱登录仍 bound_now。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        people, bindings = d / "people.json", d / "bindings.json"
        upper = {**ACC_A, "name": "Peter"}
        lower = {**ACC_A, "name": "peter"}
        acc_x = {"platform": "volcano", "account": "2000000009", "name": "Peter"}
        people.write_text(json.dumps(roster(row("彼得", "p@wuji.tech", accounts=[upper]))))
        first = load(str(people), bindings_path=str(bindings))
        self.assertEqual(
            first.resolve(union_id="on_P", enterprise_email="p@wuji.tech").binding, BIND_NOW
        )
        people.write_text(json.dumps(roster(row("彼得", "p@wuji.tech", accounts=[lower, acc_x]))))
        idx = load(str(people), bindings_path=str(bindings))
        res = idx.resolve(union_id="on_Q", enterprise_email="p@wuji.tech")
        self.assertNotEqual(res.binding, BIND_NOW)


if __name__ == "__main__":
    unittest.main()
