"""identity iam-export：名册 → WUJI IAM 属性表（长格式，一行一条 set/skip/remove 指令）。

属性值直接决定 SSO 进哪个云账号：有疑问一律 skip；增量模式下删号必须发 remove，
而且 remove 要落到正确的人身上。写盘只允许仓库根的 identity/，且必须被 git 忽略。

离线：临时目录 chdir 当仓库根。数据虚构。
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import cli
from delivery.cli import IAM_CSV_HEADER, _iam_diff, _read_iam_csv, _require_identity_dir, main
from delivery.errors import DeliveryError

ALI = "aliyun/1000000000000001"
VOLC = "volcano/2000000001"
ATTRS = {ALI: "aliyun_username", VOLC: "volcano_username"}
A_APP, V_APP = "aliyun_username", "volcano_username"


def ali(name, status=None):
    ref = {"platform": "aliyun", "account": "1000000000000001", "name": name}
    if status:
        ref["status"] = status
    return ref


def volc(name, status=None):
    ref = {"platform": "volcano", "account": "2000000001", "name": name}
    if status:
        ref["status"] = status
    return ref


def person(name, email, accounts=(), union_id="", pending=(), **extra):
    return {
        "name": name,
        "email": email,
        "union_id": union_id,
        "accounts": list(accounts),
        "pending": list(pending),
        **extra,
    }


def roster(*people):
    return {"schema": "wuji-people@1", "people": list(people)}


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == IAM_CSV_HEADER, reader.fieldnames
        return list(reader)


def by_key(rows):
    out = {}
    for r in rows:
        key = (r["email"], r["app"])
        assert key not in out, f"重复行 {key}"
        out[key] = r
    return out


def row(uid, email, app, value, action="set"):
    return {
        "feishu_union_id": uid,
        "email": email,
        "name": "某人",
        "app": app,
        "value": value,
        "action": action,
        "match_by": "feishu_union_id" if uid else "email（存量回填）",
        "problem": "",
    }


class _Repo(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        os.chdir(self.root)
        self.id_dir = self.root / "identity"
        self.id_dir.mkdir()
        (self.id_dir / "attrs.json").write_text(json.dumps(ATTRS), encoding="utf-8")

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def write_people(self, data):
        (self.id_dir / "people.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def export(self, *extra, out="identity/out.csv"):
        argv = [
            "identity",
            "iam-export",
            "--people",
            "identity/people.json",
            "--attributes",
            "identity/attrs.json",
            "--out",
            out,
            *extra,
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = main(argv)
        self.stdout, self.stderr = stdout.getvalue(), stderr.getvalue()
        return rc


# ── 全量 ──────────────────────────────────────────────────────────────────


class FullExportTests(_Repo):
    def test_set_and_skip_rows(self):
        self.write_people(
            roster(
                person("彼得", "p@wuji.tech", [ali("peter"), volc("Peter")], union_id="on_P"),
                # 已确认阿里 + 火山只待确认 → 阿里 set，火山 skip
                person("安娜", "a@wuji.tech", [ali("anna")], pending=[volc("Anna", "review")]),
                # 阿里已确认，同一应用又有一条 pending → 不再多出 skip 行
                person("贝拉", "b@wuji.tech", [ali("bella")], pending=[ali("bella2", "review")]),
                # 只有 pending
                person("待定", "t@wuji.tech", pending=[ali("tbd", "review")]),
                # 同一云账号下两个号
                person("双号", "d@wuji.tech", [ali("d1"), ali("d2")], union_id="on_D"),
                # union_id 重复
                person("甲", "x@wuji.tech", [ali("xx")], union_id="on_DUP"),
                person("乙", "y@wuji.tech", [volc("Yy")], union_id="on_DUP"),
                # 未配置应用的云账号
                person("丙", "c@wuji.tech", [{"platform": "aliyun", "account": "9", "name": "c"}]),
                # 通讯录邮箱冲突
                person("丁", "s@wuji.tech", [ali("shared")], email_collision=True),
                # 没有云账号：不出现
                person("无号", "n@wuji.tech", union_id="on_N"),
            )
        )
        self.assertEqual(self.export(), 0)
        out = self.id_dir / "out.csv"
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        rows = by_key(read_rows(out))

        def check(email, app, value, action, problem=""):
            r = rows[(email, app)]
            self.assertEqual((r["value"], r["action"]), (value, action), (email, app))
            if problem:
                self.assertIn(problem, r["problem"])
            elif action == "set":
                self.assertEqual(r["problem"], "")
            return r

        p = check("p@wuji.tech", A_APP, "peter", "set")
        self.assertEqual((p["feishu_union_id"], p["match_by"]), ("on_P", "feishu_union_id"))
        check("p@wuji.tech", V_APP, "Peter", "set")
        a = check("a@wuji.tech", A_APP, "anna", "set")
        self.assertIn("存量回填", a["match_by"])
        check("a@wuji.tech", V_APP, "", "skip", "对应关系待确认，未导出")
        check("b@wuji.tech", A_APP, "bella", "set")
        check("t@wuji.tech", A_APP, "", "skip", "待确认")
        check("d@wuji.tech", A_APP, "", "skip", "多个号")
        check("x@wuji.tech", A_APP, "", "skip", "重复")
        check("y@wuji.tech", V_APP, "", "skip", "重复")
        check("c@wuji.tech", "", "", "skip", "未配置")
        check("s@wuji.tech", A_APP, "", "skip", "共用")
        self.assertNotIn("n@wuji.tech", {e for e, _ in rows})
        self.assertEqual(
            sorted(rows),
            sorted(
                [
                    ("p@wuji.tech", A_APP),
                    ("p@wuji.tech", V_APP),
                    ("a@wuji.tech", A_APP),
                    ("a@wuji.tech", V_APP),
                    ("b@wuji.tech", A_APP),
                    ("t@wuji.tech", A_APP),
                    ("d@wuji.tech", A_APP),
                    ("x@wuji.tech", A_APP),
                    ("y@wuji.tech", V_APP),
                    ("c@wuji.tech", ""),
                    ("s@wuji.tech", A_APP),
                ]
            ),
        )
        self.assertIn("set 4 条", self.stdout)
        self.assertNotIn("remove", {r["action"] for r in rows.values()})

    def test_no_record_no_archive(self):
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export(), 0)
        self.assertFalse((self.id_dir / "iam-sent").exists())

    def test_formula_prefix_is_escaped_on_write(self):
        self.write_people(
            roster(
                person("甲", "a@wuji.tech", [ali("=HYPERLINK(1)")], union_id="on_A"),
                person("乙", "b@wuji.tech", [ali("-b"), volc("+v")], union_id="on_B"),
            )
        )
        self.assertEqual(self.export(), 0)
        raw = (self.id_dir / "out.csv").read_text(encoding="utf-8-sig")
        self.assertIn("'=HYPERLINK(1)", raw)
        self.assertIn("'-b", raw)
        self.assertIn("'+v", raw)
        # 读回（作基线）时去掉转义前缀
        values = {r["value"] for r in _read_iam_csv(self.id_dir / "out.csv")}
        self.assertEqual(values, {"=HYPERLINK(1)", "-b", "+v"})


# ── 增量 ──────────────────────────────────────────────────────────────────


class IncrementalExportTests(_Repo):
    BASE = roster(
        person("不变", "a@wuji.tech", [ali("alice")], union_id="on_A"),
        person("改名", "b@wuji.tech", [ali("bob")], union_id="on_B"),
        person("删号", "c@wuji.tech", [ali("carol"), volc("Carol")], union_id="on_C"),
        person("补ID", "d@wuji.tech", [ali("dave")]),
        person("删号补ID", "e@wuji.tech", [ali("erin")]),
        person("一直跳过", "f@wuji.tech", [ali("f1"), ali("f2")], union_id="on_F"),
        person("变跳过", "h@wuji.tech", [ali("hank")], union_id="on_H"),
    )
    NOW = roster(
        person("不变", "a@wuji.tech", [ali("alice")], union_id="on_A"),
        person("改名", "b@wuji.tech", [ali("bob2")], union_id="on_B"),
        person("删号", "c@wuji.tech", [volc("Carol")], union_id="on_C"),
        person("补ID", "d@wuji.tech", [ali("dave")], union_id="on_D"),
        person("删号补ID", "e@wuji.tech", [volc("Erin")], union_id="on_E"),
        person("一直跳过", "f@wuji.tech", [ali("f1"), ali("f2")], union_id="on_F"),
        person("变跳过", "h@wuji.tech", [ali("hank"), ali("hank2")], union_id="on_H"),
        person("新人", "g@wuji.tech", [ali("gus")], union_id="on_G"),
    )

    def test_diff_actions(self):
        self.write_people(self.BASE)
        self.assertEqual(self.export(out="identity/base.csv"), 0)
        self.write_people(self.NOW)
        self.assertEqual(self.export("--baseline", "identity/base.csv"), 0)
        self.assertIn("对比基线", self.stdout)
        rows = by_key(read_rows(self.id_dir / "out.csv"))
        got = {k: (r["action"], r["value"]) for k, r in rows.items()}
        self.assertEqual(
            got,
            {
                ("b@wuji.tech", A_APP): ("set", "bob2"),  # 改名
                ("c@wuji.tech", A_APP): ("remove", "carol"),  # 删号，value 保留旧值
                ("e@wuji.tech", A_APP): ("remove", "erin"),
                ("e@wuji.tech", V_APP): ("set", "Erin"),  # 新增一个号
                ("h@wuji.tech", A_APP): ("remove", "hank"),  # 现在是 skip：原值要撤掉
                ("g@wuji.tech", A_APP): ("set", "gus"),  # 新人
            },
        )
        # 未变化（含只是补上 union_id 的 d）不输出；skip 不进增量
        self.assertNotIn(("a@wuji.tech", A_APP), rows)
        self.assertNotIn(("d@wuji.tech", A_APP), rows)
        self.assertFalse(any(r["action"] == "skip" for r in rows.values()))
        c = rows[("c@wuji.tech", A_APP)]
        self.assertEqual(
            (c["feishu_union_id"], c["match_by"], c["problem"]), ("on_C", "feishu_union_id", "")
        )
        # 基线里没有 union_id、现在有了：remove 行补上 union_id
        e = rows[("e@wuji.tech", A_APP)]
        self.assertEqual((e["feishu_union_id"], e["match_by"]), ("on_E", "feishu_union_id"))
        self.assertIn("set 3 条，remove 3 条，skip 0 条", self.stdout)

    def test_same_state_yields_empty_diff(self):
        self.write_people(self.NOW)
        self.assertEqual(self.export(out="identity/base.csv"), 0)
        self.assertEqual(self.export("--baseline", "identity/base.csv"), 0)
        self.assertEqual(read_rows(self.id_dir / "out.csv"), [])

    def test_formula_value_round_trip_is_not_a_change(self):
        self.write_people(roster(person("甲", "a@wuji.tech", [ali("=cmd")], union_id="on_A")))
        self.assertEqual(self.export(out="identity/base.csv"), 0)
        self.assertEqual(self.export("--baseline", "identity/base.csv"), 0)
        self.assertEqual(read_rows(self.id_dir / "out.csv"), [])

    def test_diff_unit_gains_union_id_matches_by_email(self):
        base = [row("", "D@wuji.tech", A_APP, "dave")]
        now = [row("on_D", "d@wuji.tech", A_APP, "dave")]
        self.assertEqual(_iam_diff(now, base), [])
        self.assertEqual(_iam_diff(now, base + [row("", "x@wuji.tech", A_APP, "x", "skip")]), [])

    def test_missing_baseline_file_rc2(self):
        self.write_people(self.NOW)
        self.assertEqual(self.export("--baseline", "identity/nope.csv"), 2)
        self.assertFalse((self.id_dir / "out.csv").exists())

    def test_baseline_with_wrong_header_rc2(self):
        self.write_people(self.NOW)
        (self.id_dir / "old.csv").write_text(
            "feishu_union_id,email,name,aliyun_username,match_by,problem\n"
            "on_A,a@wuji.tech,不变,alice,feishu_union_id,\n",
            encoding="utf-8",
        )
        self.assertEqual(self.export("--baseline", "identity/old.csv"), 2)
        self.assertFalse((self.id_dir / "out.csv").exists())

    def test_diff_email_reuse_with_same_value_is_a_change(self):
        """已知 bug（cli.py _iam_diff.find）：两边都有 union_id 但不同，
        只因邮箱相同就回落按邮箱对上。
        邮箱 p@ 从老员工 on_P 复用给新人 on_Q，号 peter 也转给了 Q：diff 为空 →
        IT 既不给 on_Q 写 peter，也不撤 on_P 的 peter（老员工 SSO 仍进这个号）。
        期望：on_Q set + on_P remove。"""
        base = [row("on_P", "p@wuji.tech", A_APP, "peter")]
        now = [row("on_Q", "p@wuji.tech", A_APP, "peter")]
        got = {(r["feishu_union_id"], r["action"]) for r in _iam_diff(now, base)}
        self.assertEqual(got, {("on_Q", "set"), ("on_P", "remove")})

    def test_remove_is_not_redirected_to_another_union_id(self):
        """已知 bug（cli.py _iam_diff 删号分支「现在有了 union_id 就补上」）：不看基线行自己
        是否已有 union_id，同邮箱的另一个人（邮箱复用）会把 remove 抢走：
        基线 on_P/p@/aliyun=peter，现在 p@ 属于 on_Q（只有火山号）→ remove 行变成 on_Q，
        on_P 的 peter 永远撤不掉。"""
        base = [row("on_P", "p@wuji.tech", A_APP, "peter")]
        now = [row("on_Q", "p@wuji.tech", V_APP, "q")]
        removed = [r for r in _iam_diff(now, base) if r["action"] == "remove"]
        self.assertEqual([r["feishu_union_id"] for r in removed], ["on_P"])


class RecordAndLatestTests(_Repo):
    def _strftime(self, *names):
        return mock.patch.object(cli.time, "strftime", side_effect=list(names))

    def test_record_archives_0600_copy(self):
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        with self._strftime("20260915-100000.csv"):
            self.assertEqual(self.export("--record"), 0)
        archive = self.id_dir / "iam-sent" / "20260915-100000.csv"
        self.assertTrue(archive.exists())
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(archive.read_bytes(), (self.id_dir / "out.csv").read_bytes())
        self.assertIn("已存档", self.stdout)

    def test_latest_uses_newest_archive(self):
        sent = self.id_dir / "iam-sent"
        sent.mkdir()
        header = ",".join(IAM_CSV_HEADER)
        (sent / "20260101-000000.csv").write_text(
            header + "\non_P,p@wuji.tech,彼得,aliyun_username,old,set,feishu_union_id,\n",
            encoding="utf-8",
        )
        (sent / "20260201-000000.csv").write_text(
            header + "\non_P,p@wuji.tech,彼得,aliyun_username,peter,set,feishu_union_id,\n",
            encoding="utf-8",
        )
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export("--baseline", "latest"), 0)
        self.assertIn("20260201-000000.csv", self.stdout)
        self.assertEqual(read_rows(self.id_dir / "out.csv"), [])

    def test_latest_without_archive_rc2(self):
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export("--baseline", "latest"), 2)
        (self.id_dir / "iam-sent").mkdir()
        self.assertEqual(self.export("--baseline", "latest"), 2)
        self.assertFalse((self.id_dir / "out.csv").exists())

    def test_incremental_record_keeps_full_state_for_next_removal(self):
        """已知 bug（cli.py _cmd_identity_iam_export 里 --record 存的是 diff 后的 rows）：
        增量导出 + --record 只存档了「这次的变化」，下一次 --baseline latest 拿它当全状态比，
        两次之前 set 过、之后没变化的号被删时，基线里没有它 → 不发 remove，旧映射永久残留。
        """
        self.write_people(
            roster(
                person("甲", "a@wuji.tech", [ali("alice")], union_id="on_A"),
                person("乙", "b@wuji.tech", [ali("bob")], union_id="on_B"),
            )
        )
        with self._strftime("20260915-100000.csv"):
            self.assertEqual(self.export("--record"), 0)
        self.write_people(
            roster(
                person("甲", "a@wuji.tech", [ali("alice")], union_id="on_A"),
                person("乙", "b@wuji.tech", [ali("bob")], union_id="on_B"),
                person("新人", "g@wuji.tech", [ali("gus")], union_id="on_G"),
            )
        )
        with self._strftime("20260916-100000.csv"):
            self.assertEqual(self.export("--baseline", "latest", "--record"), 0)
        # 甲删号
        self.write_people(
            roster(
                person("乙", "b@wuji.tech", [ali("bob")], union_id="on_B"),
                person("新人", "g@wuji.tech", [ali("gus")], union_id="on_G"),
            )
        )
        self.assertEqual(self.export("--baseline", "latest"), 0)
        rows = by_key(read_rows(self.id_dir / "out.csv"))
        self.assertEqual(rows.get(("a@wuji.tech", A_APP), {}).get("action"), "remove")


# ── 写盘守卫 ──────────────────────────────────────────────────────────────


@unittest.skipUnless(shutil.which("git"), "需要 git")
class GitGuardTests(_Repo):
    def git_init(self, path, ignore):
        subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603,S607
        (path / ".gitignore").write_text(ignore, encoding="utf-8")

    def test_c1_git_repo_not_ignoring_identity_is_rejected(self):
        self.git_init(self.root, "*.pyc\n")
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export(), 2)
        self.assertFalse((self.id_dir / "out.csv").exists())
        with self.assertRaises(DeliveryError):
            _require_identity_dir(self.root / "identity" / "iam-sent" / "x.csv")

    def test_c1_git_repo_ignoring_identity_is_allowed_including_archive(self):
        self.git_init(self.root, "identity/*\n")
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export("--record"), 0)
        self.assertTrue((self.id_dir / "out.csv").exists())
        self.assertEqual(len(list((self.id_dir / "iam-sent").glob("*.csv"))), 1)

    def test_c3_git_dir_env_pointing_elsewhere_does_not_change_verdict(self):
        other = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, other, True)
        self.git_init(other, "identity/*\n")
        target = self.root / "identity" / "x.csv"
        env = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other)}

        self.git_init(self.root, "")  # 本仓库没忽略 identity/
        with mock.patch.dict(os.environ, env), self.assertRaises(DeliveryError):
            _require_identity_dir(target)

        (self.root / ".gitignore").write_text("identity/*\n", encoding="utf-8")
        with mock.patch.dict(os.environ, env):
            _require_identity_dir(target)  # 本仓库忽略了：允许，不受 GIT_DIR 干扰

    def test_git_unavailable_inside_repo_tree_is_rejected(self):
        (self.root / ".git").mkdir()
        target = self.root / "identity" / "x.csv"
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            with self.assertRaises(DeliveryError):
                _require_identity_dir(target)
            sub = self.root / "sub"
            sub.mkdir()
            os.chdir(sub)  # 子目录里跑也一样拒绝
            with self.assertRaises(DeliveryError):
                _require_identity_dir(sub / "identity" / "x.csv")

    def test_git_unavailable_outside_any_repo_uses_cwd_identity(self):
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            _require_identity_dir(self.root / "identity" / "x.csv")
            with self.assertRaises(DeliveryError):
                _require_identity_dir(self.root / "elsewhere" / "x.csv")


class SymlinkGuardTests(_Repo):
    def test_c2_symlinked_out_file_pointing_outside_is_rejected(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (self.id_dir / "out.csv").symlink_to(outside / "leak.csv")
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export(), 2)
        self.assertFalse((outside / "leak.csv").exists())

    def test_c2_symlinked_identity_subdir_pointing_outside_is_rejected(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        (self.id_dir / "iam-sent").symlink_to(outside, target_is_directory=True)
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))
        self.assertEqual(self.export("--record"), 2)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
