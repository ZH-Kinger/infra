"""IAM 属性表增量比对（审计 E1–E5）：remove 不发错人、邮箱复用不按邮箱 set、配置丢失与批量删除拒绝、
基线必须是确认过的全量存档、决定导入结果的列不做公式转义。数据虚构。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from delivery.iam_export import IamDiffError, cell, check_baseline, diff, sanitize, uncell

APPS = {"aliyun-main", "volcano-main"}


def row(uid, email, app, value, action="set", name="", problem=""):
    return {
        "feishu_union_id": uid,
        "email": email,
        "name": name or email.split("@")[0],
        "app": app,
        "value": value,
        "action": action,
        "match_by": "feishu_union_id" if uid else "email（存量回填）",
        "problem": problem,
    }


def actions(rows):
    return sorted(
        (r["action"], r["feishu_union_id"], r["email"], r["app"], r["value"]) for r in rows
    )


class DiffTests(unittest.TestCase):
    def test_unchanged_is_empty(self):
        base = [row("U1", "p@x", "aliyun-main", "p1")]
        out, notes = diff(base, base, current_apps=APPS)
        self.assertEqual((out, notes), ([], []))

    def test_value_change_is_set_without_remove(self):
        base = [row("U1", "p@x", "aliyun-main", "p1")]
        cur = [row("U1", "p@x", "aliyun-main", "p2")]
        out, _ = diff(cur, base, current_apps=APPS)
        self.assertEqual(actions(out), [("set", "U1", "p@x", "aliyun-main", "p2")])

    def test_backfilled_union_id_matches_by_email_and_sets_by_uid(self):
        base = [row("", "p@x", "aliyun-main", "p1")]
        cur = [row("U1", "p@x", "aliyun-main", "p1")]
        out, _ = diff(cur, base, current_apps=APPS)
        # 回填：不按邮箱认领基线行，先按值删掉旧值，再按 union_id 写回（remove 在前）
        self.assertEqual(
            [(r["action"], r["feishu_union_id"], r["value"]) for r in out],
            [("remove", "", "p1"), ("set", "U1", "p1")],
        )

    def test_m3_email_reused_to_new_hire_with_same_account(self):
        # 基线：P 按邮箱回填了 v；邮箱 e 复用给新人 N（姓名不同），N 恰好拿到同一个号
        base = [row("", "e@x", "aliyun-main", "v", name="彼得")]
        cur = [row("UN", "e@x", "aliyun-main", "v", name="新人")]
        out, notes = diff(cur, base, current_apps=APPS)
        # 原主身上的旧值按值删掉，新人按 union_id 写入；姓名不同给出提示
        self.assertEqual(
            [(r["action"], r["feishu_union_id"]) for r in out], [("remove", ""), ("set", "UN")]
        )
        self.assertTrue(notes)

    def test_r3_derived_name_email_reuse_removes_old_value(self):
        # 基线姓名是邮箱前缀；邮箱复用给新人 UN（另一个号 w），原来的号 v 给了 R
        base = [row("", "p@x", "aliyun-main", "v", name="p"), row("UR", "r@x", "aliyun-main", "r")]
        cur = [row("UN", "p@x", "aliyun-main", "w"), row("UR", "r@x", "aliyun-main", "v")]
        out, _ = diff(cur, base, current_apps=APPS)
        removes = [r["value"] for r in out if r["action"] == "remove"]
        self.assertIn("v", removes)
        self.assertEqual(out[0]["action"], "remove")

    def test_m5_remove_matches_by_value(self):
        base = [row("", "p@x", "aliyun-main", "p1"), row("U2", "q@x", "aliyun-main", "q")]
        cur = [row("U2", "q@x", "aliyun-main", "q")]
        out, _ = diff(cur, base, current_apps=APPS)
        self.assertEqual([r["match_by"] for r in out if r["action"] == "remove"], ["value"])

    def test_m1_email_owned_by_uid_on_other_app_blocks_email_set(self):
        base = [row("U1", "e@x", "volcano-main", "V1")]
        cur = [row("", "e@x", "aliyun-main", "a"), row("U1", "e2@x", "volcano-main", "V1")]
        out, _ = diff(cur, base, current_apps=APPS)
        self.assertIn(("skip", "", "e@x", "aliyun-main", ""), actions(out))
        self.assertNotIn("set", [r["action"] for r in out])

    def test_m4_threshold_is_per_app(self):
        base = [row(f"A{i}", f"a{i}@x", "aliyun-main", f"a{i}") for i in range(300)]
        base += [row(f"A{i}", f"a{i}@x", "volcano-main", f"V{i}") for i in range(25)]
        cur = base[:300]  # 火山整体没采到
        with self.assertRaises(IamDiffError):
            diff(cur, base, current_apps=APPS)

    def test_e1_remove_never_borrows_someone_elses_union_id(self):
        # P 当初按邮箱回填；P 改了邮箱且这个应用现在待确认（skip）；新人 N 拿到邮箱 e
        base = [row("", "e@x", "aliyun-main", "p_old")]
        cur = [
            row("U_P", "e2@x", "aliyun-main", "", action="skip"),
            row("U_N", "e@x", "volcano-main", "n_user"),
        ]
        out, _ = diff(cur, base, current_apps=APPS, allow_mass_remove=True)
        removes = [r for r in out if r["action"] == "remove"]
        self.assertEqual(len(removes), 1)
        self.assertEqual(removes[0]["feishu_union_id"], "")
        self.assertEqual(removes[0]["value"], "p_old")
        self.assertEqual(removes[0]["email"], "e@x")

    def test_e2_email_reuse_is_not_set_by_email(self):
        # 基线里 e 属于 U1；当前同邮箱的行没有 union_id（新人或名册没带通讯录）
        base = [row("U1", "e@x", "aliyun-main", "v1"), row("U9", "k@x", "aliyun-main", "k")]
        cur = [
            row("", "e@x", "aliyun-main", "v2"),
            row("U9", "k@x", "aliyun-main", "k"),
            row("U1", "e3@x", "volcano-main", "", action="skip"),
        ]
        out, notes = diff(cur, base, current_apps=APPS)
        self.assertIn(("skip", "", "e@x", "aliyun-main", ""), actions(out))
        self.assertIn(("remove", "U1", "e@x", "aliyun-main", "v1"), actions(out))
        self.assertNotIn("set", [r["action"] for r in out])
        self.assertTrue(notes)

    def test_e2_vanished_union_ids_refused(self):
        base = [row("U1", "e@x", "aliyun-main", "v1")]
        cur = [row("", "e@x", "aliyun-main", "v1")]
        with self.assertRaises(IamDiffError):
            diff(cur, base, current_apps=APPS)
        out, _ = diff(cur, base, current_apps=APPS, allow_mass_remove=True)
        self.assertIn(("remove", "U1", "e@x", "aliyun-main", "v1"), actions(out))

    def test_e3_missing_app_refused(self):
        base = [row("U1", "p@x", "volcano-main", "P")]
        with self.assertRaises(IamDiffError):
            diff(base, base, current_apps={"aliyun-main"})

    def test_e3_mass_remove_refused(self):
        base = [row(f"U{i}", f"p{i}@x", "volcano-main", f"v{i}") for i in range(20)]
        cur = [dict(r, action="skip", value="") for r in base[:6]] + base[6:]
        with self.assertRaises(IamDiffError):
            diff(cur, base, current_apps=APPS)
        out, _ = diff(cur, base, current_apps=APPS, allow_mass_remove=True)
        self.assertEqual(sum(r["action"] == "remove" for r in out), 6)

    def test_small_remove_allowed(self):
        base = [
            row("U1", "p@x", "volcano-main", "v"),
            row("U2", "q@x", "volcano-main", "w"),
            row("U3", "s@x", "volcano-main", "x"),
        ]
        cur = [
            row("U1", "p@x", "volcano-main", "v"),
            row("U2", "q@x", "volcano-main", "", action="skip"),
            row("U3", "s@x", "volcano-main", "x"),
        ]
        out, _ = diff(cur, base, current_apps=APPS)
        self.assertEqual(actions(out), [("remove", "U2", "q@x", "volcano-main", "w")])


class OrderAndStateTests(unittest.TestCase):
    def test_n1_removes_come_before_sets_when_value_moves(self):
        base = [row("on_P", "p@x", "aliyun-main", "peter")]
        cur = [row("on_Q", "q@x", "aliyun-main", "peter")]
        out, _ = diff(cur, base, current_apps=APPS, allow_mass_remove=True)
        self.assertEqual([r["action"] for r in out], ["remove", "set"])
        self.assertIn("重新写入", out[0]["problem"])

    def test_n2_recorded_state_keeps_skip_and_reflags_next_time(self):
        from delivery.iam_export import recorded_state

        base = [row("U1", "e@x", "aliyun-main", "v1"), row("U9", "k@x", "aliyun-main", "k")]
        cur = [
            row("", "e@x", "aliyun-main", "v2"),
            row("U9", "k@x", "aliyun-main", "k"),
            row("U1", "e3@x", "volcano-main", "", action="skip"),
        ]
        out, _ = diff(cur, base, current_apps=APPS, allow_mass_remove=True)
        state = recorded_state(cur, out, base)
        self.assertEqual([r["action"] for r in state], ["skip", "set", "skip"])
        # 确认后以 state 为基线再比：仍然提示，不会静默按邮箱写入
        out2, notes2 = diff(cur, state, current_apps=APPS)
        self.assertEqual([r["action"] for r in out2], ["skip"])
        self.assertTrue(notes2)
        # 这个人暂时不是 set：标记也要带到下一份存档
        cur_pending = [dict(cur[0], action="skip", value="", problem="待确认"), *cur[1:]]
        out3, _ = diff(cur_pending, state, current_apps=APPS)
        kept = recorded_state(cur_pending, out3, state)
        self.assertTrue(any(r["problem"] and "复用" in r["problem"] for r in kept))
        # 管理员核对后放行
        out4, _ = diff(cur, state, current_apps=APPS, resolved_emails=frozenset({"E@x"}))
        self.assertEqual([r["action"] for r in out4], ["set"])

    def test_derived_baseline_name_does_not_block_backfill(self):
        # 基线姓名是邮箱前缀（名册当时没接通讯录），现在是真实姓名：正常回填
        base = [row("", "zhang.san@x", "aliyun-main", "v", name="zhang.san")]
        cur = [row("U1", "zhang.san@x", "aliyun-main", "v", name="张三")]
        out, _ = diff(cur, base, current_apps=APPS)
        self.assertEqual([r["action"] for r in out], ["remove", "set"])

    def test_backfill_of_everyone_is_not_mass_remove(self):
        base = [row("", f"p{i}@x", "aliyun-main", f"v{i}") for i in range(20)]
        cur = [row(f"U{i}", f"p{i}@x", "aliyun-main", f"v{i}") for i in range(20)]
        out, _ = diff(cur, base, current_apps=APPS)
        self.assertEqual(sum(r["action"] == "remove" for r in out), 20)

    def test_m6_small_app_vanishing_refused(self):
        base = [row(f"U{i}", f"p{i}@x", "volcano-main", f"v{i}") for i in range(4)]
        base += [row("A", "a@x", "aliyun-main", "a")]
        cur = [base[-1]] + [dict(r, action="skip", value="") for r in base[:4]]
        with self.assertRaises(IamDiffError):
            diff(cur, base, current_apps=APPS)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sent = self.root / "iam-sent"
        self.sent.mkdir()

    def test_e4_baseline_must_be_in_sent_dir(self):
        other = self.root / "iam-attributes.csv"
        with self.assertRaises(IamDiffError):
            check_baseline(other, [], self.sent)
        with self.assertRaises(IamDiffError):
            check_baseline(self.sent / "pending" / "20260915-130852.csv", [], self.sent)
        check_baseline(self.sent / "20260915-130852.csv", [], self.sent)
        check_baseline(self.sent / "20260915-130852~2.csv", [], self.sent)

    def test_m1_non_archive_names_refused(self):
        for name in ("inc.csv", "base.csv", "20260915-130852-inc.csv", "zz.csv"):
            with self.assertRaises(IamDiffError, msg=name):
                check_baseline(self.sent / name, [], self.sent)

    def test_e4_symlink_escaping_sent_dir_refused(self):
        outside = self.root / "outside.csv"
        outside.write_text("", encoding="utf-8")
        link = self.sent / "20260101-000000.csv"
        link.symlink_to(outside)
        with self.assertRaises(IamDiffError):
            check_baseline(link, [], self.sent)

    def test_e4_incremental_file_refused(self):
        with self.assertRaises(IamDiffError):
            check_baseline(
                self.sent / "20260101-000000.csv",
                [row("U1", "p@x", "a", "v", action="remove")],
                self.sent,
            )


class CellTests(unittest.TestCase):
    def test_e5_raw_columns_not_escaped(self):
        for col in ("feishu_union_id", "email", "app", "value", "action"):
            self.assertEqual(cell(col, "-abc"), "-abc")
            self.assertEqual(uncell(col, "'-abc"), "'-abc")

    def test_e5_other_columns_escaped_including_tab_cr(self):
        for bad in ("=1+1", "+x", "-x", "@x", "\tx", "\rx"):
            self.assertEqual(cell("name", bad), "'" + bad)
            self.assertEqual(uncell("name", "'" + bad), bad)
        self.assertEqual(cell("name", "张三"), "张三")

    def test_e5_unsafe_raw_values_become_skip(self):
        rows = sanitize(
            [row("U1", "p@x", "aliyun-main", "=HYPERLINK()"), row("U2", "q@x", "aliyun-main", "ok")]
        )
        self.assertEqual(rows[0]["action"], "skip")
        self.assertEqual(rows[0]["value"], "")
        self.assertIn("特殊字符", rows[0]["problem"])
        self.assertEqual(rows[1]["action"], "set")

    def test_m2_email_with_formula_prefix_skipped(self):
        rows = sanitize([row("", "-j@x", "aliyun-main", "j")])
        self.assertEqual((rows[0]["action"], rows[0]["email"]), ("skip", ""))

    def test_m3_skip_rows_with_unsafe_raw_values_cleared(self):
        rows = sanitize([row("=U", "p@x", "aliyun-main", "", action="skip", problem="待确认")])
        self.assertEqual((rows[0]["feishu_union_id"], rows[0]["action"]), ("", "skip"))


if __name__ == "__main__":
    unittest.main()
