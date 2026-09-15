"""审计（2026-09-15）指出的问题的回归测试。数据全部虚构。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from delivery.clouds import aliyun
from delivery.inventory_collect import _aliyun_attachments
from delivery.people import (
    BIND_CONFLICT,
    BIND_NONE,
    BIND_NOW,
    BIND_UNION_ID,
    PeopleError,
    load,
    parse,
)

ACC_P = {"platform": "aliyun", "account": "1000000000000001", "name": "peter"}
ACC_Q = {"platform": "aliyun", "account": "1000000000000001", "name": "qadmin"}


def roster(*rows):
    return {"schema": "wuji-people@1", "people": list(rows)}


def row(name, email, accounts=(), union_id=""):
    return {"name": name, "email": email, "union_id": union_id, "accounts": list(accounts)}


class _Files:
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.people = self.dir / "people.json"
        self.bindings = self.dir / "bindings.json"

    def write_roster(self, data):
        self.people.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def index(self):
        return load(str(self.people), bindings_path=str(self.bindings))


class EmailReuseAfterRegenerationTests(_Files, unittest.TestCase):
    """B1：绑定按邮箱套回名册，邮箱被复用给新人后，老员工登录看到新人的账号。"""

    def test_email_reuse_after_regeneration_never_shows_someone_else(self):
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P])))
        first = self.index().resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        self.assertEqual(first.binding, BIND_NOW)

        # 名册重新生成：P 的邮箱变成 p2，旧邮箱 p 分给了新人 Q（Q 名下是管理员账号）
        self.write_roster(
            roster(row("新人", "p@wuji.tech", [ACC_Q]), row("彼得", "p2@wuji.tech", [ACC_P]))
        )
        idx = self.index()
        # 绑定记下的邮箱对不上 → 不套用，P 被拦下交管理员（fail-closed），绝不给他看 Q
        res = idx.resolve(union_id="on_P", enterprise_email="p2@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)
        self.assertEqual(idx.by_key("email:p@wuji.tech").union_id, "")
        # Q 本人照常可以首次关联自己的号
        q = idx.resolve(union_id="on_Q", enterprise_email="p@wuji.tech")
        self.assertEqual([a.name for a in q.person.accounts], ["qadmin"])

    def test_username_reuse_after_rename_never_shows_someone_else(self):
        """复审 R1：P 的号改名为 p1，旧名 p 分给新人 N。只按指纹会把 N 认成 P。"""
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P])))
        self.index().resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        self.write_roster(
            roster(
                row("新人", "n@wuji.tech", [ACC_P]),
                row("彼得", "p@wuji.tech", [ACC_P | {"name": "peter1"}]),
            )
        )
        res = self.index().resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        self.assertIsNone(res.person)
        self.assertEqual(res.binding, BIND_CONFLICT)

    def test_changed_accounts_block_instead_of_rebinding_by_email(self):
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P])))
        self.index().resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        # P 的账号集合变了（多了一个号）：指纹对不上 → 不套用，也不许再走邮箱
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P, ACC_Q])))
        idx = self.index()
        res = idx.resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertIsNone(res.person)
        self.assertTrue(any("对不上" in w for w in idx.warnings))

    def test_old_email_keyed_bindings_file_is_rejected(self):
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P])))
        self.bindings.write_text(
            json.dumps(
                {"schema": "wuji-bindings@1", "bindings": {"p@wuji.tech": {"union_id": "x"}}}
            )
        )
        with self.assertRaises(PeopleError):
            self.index()

    def test_same_accounts_claimed_by_another_process_is_conflict(self):
        """M3：两份索引（或两个进程）先后按同一组账号绑定，后到的不能覆盖。"""
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P])))
        a, b = self.index(), self.index()
        self.assertEqual(
            a.resolve(union_id="on_1", enterprise_email="p@wuji.tech").binding, BIND_NOW
        )
        self.assertEqual(
            b.resolve(union_id="on_2", enterprise_email="p@wuji.tech").binding, BIND_CONFLICT
        )
        saved = json.loads(self.bindings.read_text(encoding="utf-8"))
        self.assertEqual(list(saved["bindings"]), ["on_1"])

    def test_person_without_cloud_accounts_is_not_bound(self):
        self.write_roster(roster(row("无号", "n@wuji.tech")))
        res = self.index().resolve(union_id="on_n", enterprise_email="n@wuji.tech")
        self.assertEqual(res.binding, BIND_NONE)
        self.assertFalse(self.bindings.exists())

    def test_concurrent_claims_in_one_index_still_single_winner(self):
        self.write_roster(roster(row("彼得", "p@wuji.tech", [ACC_P])))
        idx = self.index()
        out = {}
        gate = threading.Barrier(2)

        def claim(uid):
            gate.wait()
            out[uid] = idx.resolve(union_id=uid, enterprise_email="p@wuji.tech").binding

        threads = [threading.Thread(target=claim, args=(u,)) for u in ("on_a", "on_b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(out.values()), sorted([BIND_NOW, BIND_CONFLICT]))


class DuplicateUnionIdTests(unittest.TestCase):
    def test_disabled_duplicate_union_id_cannot_fall_back_to_email(self):
        """L1：名册里重复而被停用的 union_id，不能再走邮箱分支认领别人。"""
        idx = parse(
            roster(
                row("甲", "a@wuji.tech", [ACC_P], union_id="on_D"),
                row("乙", "b@wuji.tech", [ACC_Q], union_id="on_D"),
                row("丙", "c@wuji.tech", [{**ACC_P, "name": "cc"}]),
            )
        )
        res = idx.resolve(union_id="on_D", enterprise_email="c@wuji.tech")
        self.assertEqual(res.binding, BIND_CONFLICT)
        self.assertEqual(idx.by_key("email:c@wuji.tech").union_id, "")


class AliyunPaginationTests(unittest.TestCase):
    """M2：翻页异常不能静默返回半份数据。"""

    creds = aliyun.Credentials("AKIDEXAMPLE", "secret")

    def test_truncated_without_new_marker_raises(self):
        def transport(url):
            return 200, {"Users": {"User": [{"UserName": "u"}]}, "IsTruncated": True}

        with self.assertRaises(aliyun.AliyunError):
            aliyun.paginate(
                *aliyun.RAM,
                "ListUsers",
                key="User",
                container="Users",
                creds=self.creds,
                transport=transport,
            )

    def test_empty_page_before_total_raises(self):
        pages = [
            {"TotalCount": 250, "PolicyAttachments": {"PolicyAttachment": [{}] * 100}},
            {"TotalCount": 250, "PolicyAttachments": {"PolicyAttachment": []}},
        ]

        def transport(url):
            return 200, pages.pop(0)

        with self.assertRaises(aliyun.AliyunError):
            _aliyun_attachments(self.creds, transport)

    def test_signature_error_does_not_echo_access_key_id(self):
        """M5：SignatureDoesNotMatch 的消息会回显待签名串，里面有 AccessKeyId。"""

        def transport(url):
            return 400, {
                "Code": "IncompleteSignature",
                "Message": "server string to sign is:GET&%2F&AccessKeyId%3DLTAIREALKEY123%26Action",
            }

        with self.assertRaises(aliyun.AliyunError) as ctx:
            aliyun.call(*aliyun.RAM, "ListUsers", creds=self.creds, transport=transport)
        self.assertNotIn("LTAIREALKEY123", str(ctx.exception))


class ServerHardeningTests(unittest.TestCase):
    def _backend(self, **kw):
        from delivery.server import Backend

        return Backend(platforms={"aliyun": "阿里云"}, **kw)

    def test_missing_roster_is_unavailable_not_everyone_has_no_accounts(self):
        """M1：名册文件缺失不能降级成空名册。"""
        from delivery.errors import DeliveryError

        d = Path(tempfile.mkdtemp())
        backend = self._backend(people_path=str(d / "people.json"))
        with self.assertRaises(DeliveryError):
            backend.people()
        # 之后生成了文件，不重启也能读到
        (d / "people.json").write_text(json.dumps(roster(row("彼得", "p@wuji.tech", [ACC_P]))))
        self.assertEqual(len(backend.people().people), 1)

    def test_removing_a_binding_by_hand_takes_effect(self):
        """M3：管理员手工删绑定要立刻生效（绑定文件 mtime 进缓存键）。"""
        import os

        d = Path(tempfile.mkdtemp())
        (d / "people.json").write_text(json.dumps(roster(row("彼得", "p@wuji.tech", [ACC_P]))))
        backend = self._backend(
            people_path=str(d / "people.json"), bindings_path=str(d / "bindings.json")
        )
        backend.people().resolve(union_id="on_P", enterprise_email="p@wuji.tech")
        self.assertEqual(backend.people().resolve(union_id="on_P").binding, BIND_UNION_ID)
        b = d / "bindings.json"
        b.write_text(json.dumps({"schema": "wuji-bindings@2", "bindings": {}}))
        st = b.stat()
        os.utime(b, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
        self.assertEqual(backend.people().resolve(union_id="on_P").binding, BIND_NONE)


if __name__ == "__main__":
    unittest.main()


class IamExportTests(unittest.TestCase):
    """名册 → IAM 属性表：属性值直接决定 SSO 进哪个号，宁可空着也不能填错。"""

    def test_export_rules(self):
        import csv

        from delivery.cli import main

        d = Path(tempfile.mkdtemp()) / "identity"
        d.mkdir()
        acct = "aliyun/1000000000000001"
        (d / "people.json").write_text(
            json.dumps(
                roster(
                    row("彼得", "p@wuji.tech", [ACC_P], union_id="on_P"),
                    row("双号", "d@wuji.tech", [ACC_P | {"name": "d1"}, ACC_P | {"name": "d2"}]),
                    {**row("待定", "t@wuji.tech"), "pending": [ACC_Q | {"status": "review"}]},
                )
            ),
            encoding="utf-8",
        )
        (d / "attrs.json").write_text(json.dumps({acct: "aliyun_username"}))
        out = d / "out.csv"
        rc = main(
            [
                "identity",
                "iam-export",
                "--people",
                str(d / "people.json"),
                "--attributes",
                str(d / "attrs.json"),
                "--out",
                str(out),
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        rows = {r["email"]: r for r in csv.DictReader(out.open(encoding="utf-8-sig"))}
        self.assertEqual(rows["p@wuji.tech"]["aliyun_username"], "peter")
        self.assertEqual(rows["p@wuji.tech"]["feishu_union_id"], "on_P")
        # 同一云账号下两个号：不导出，标问题
        self.assertEqual(rows["d@wuji.tech"]["aliyun_username"], "")
        self.assertIn("多个号", rows["d@wuji.tech"]["problem"])
        # 只有待确认对应的人不导出
        self.assertNotIn("t@wuji.tech", rows)
        self.assertEqual(rows["p@wuji.tech"]["match_by"], "feishu_union_id")
        self.assertIn("存量回填", rows["d@wuji.tech"]["match_by"])

    def test_refuses_to_write_outside_identity_dir(self):
        from delivery.cli import main

        d = Path(tempfile.mkdtemp())
        (d / "people.json").write_text(json.dumps(roster()), encoding="utf-8")
        (d / "attrs.json").write_text(json.dumps({"aliyun/1": "a"}))
        rc = main(
            [
                "identity",
                "iam-export",
                "--people",
                str(d / "people.json"),
                "--attributes",
                str(d / "attrs.json"),
                "--out",
                str(d / "out.csv"),
            ]
        )
        self.assertEqual(rc, 2)
        self.assertFalse((d / "out.csv").exists())

    def test_duplicate_attribute_names_are_rejected(self):
        from delivery.cli import main

        d = Path(tempfile.mkdtemp()) / "identity"
        d.mkdir()
        (d / "people.json").write_text(json.dumps(roster()), encoding="utf-8")
        (d / "attrs.json").write_text(json.dumps({"aliyun/1": "a", "aliyun/2": "a"}))
        rc = main(
            [
                "identity",
                "iam-export",
                "--people",
                str(d / "people.json"),
                "--attributes",
                str(d / "attrs.json"),
                "--out",
                str(d / "out.csv"),
            ]
        )
        self.assertEqual(rc, 2)
