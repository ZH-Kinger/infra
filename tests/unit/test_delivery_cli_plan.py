"""`plan-show` 的退出码契约。

这是这批代码里**唯一一条会被自动化依赖的安全断言**：计划被阻断时返回 1，
流水线据此拦住 apply。原先零测试覆盖，「state 当空计划 + rc 0」那个洞就是这么漏的。
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from delivery.cli import main


def plan_doc(changes, **extra):
    doc = {"format_version": "1.2", "resource_changes": changes}
    doc.update(extra)
    return doc


def a_change(actions, *, sensitive=None, after=None):
    return {
        "address": "a.x",
        "mode": "managed",
        "type": "alicloud_ram_policy",
        "name": "x",
        "change": {
            "actions": actions,
            "before": None,
            "after": after if after is not None else {"n": 1},
            "before_sensitive": {},
            "after_sensitive": sensitive if sensitive is not None else {},
        },
    }


class PlanShowExitCodeTests(unittest.TestCase):
    def _run(self, document, extra_args=()):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plan.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["plan-show", str(path), "--platform", "aliyun", *extra_args])
            return code, out.getvalue(), err.getvalue()

    def test_normal_plan_returns_zero(self):
        code, out, _ = self._run(plan_doc([a_change(["create"])]))
        self.assertEqual(code, 0)
        self.assertIn("创建 1 个", out)

    def test_empty_plan_returns_zero(self):
        code, out, _ = self._run(plan_doc([]))
        self.assertEqual(code, 0)
        self.assertIn("无变更", out)

    def test_errored_plan_returns_one(self):
        code, _, _ = self._run(plan_doc([a_change(["create"])], errored=True))
        self.assertEqual(code, 1)

    def test_unknown_format_version_returns_one(self):
        doc = plan_doc([a_change(["create"])])
        doc["format_version"] = "9.9"
        code, _, _ = self._run(doc)
        self.assertEqual(code, 1)

    def test_state_document_returns_two_not_zero(self):
        # 手滑用了 `terraform show -json`（不带 planfile）→ 拿到的是 state。
        # 绝不能报「无变更 + rc 0」，那会让流水线放行。
        code, out, err = self._run({"format_version": "1.0", "values": {"root_module": {}}})
        self.assertEqual(code, 2)
        self.assertNotIn("无变更", out)
        self.assertIn("state", err)

    def test_malformed_json_returns_two_not_one(self):
        # rc 1 已经表示「计划被阻断」，工具自身的问题必须用别的码，否则流水线分不开。
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = main(["plan-show", str(path), "--platform", "aliyun"])
        self.assertEqual(code, 2)

    def test_missing_file_returns_two(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["plan-show", "/nonexistent/plan.json", "--platform", "aliyun"])
        self.assertEqual(code, 2)

    def test_unknown_platform_returns_two(self):
        code, _, _ = self._run(plan_doc([a_change(["create"])]))
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(["plan-show", "/tmp/x.json", "--platform", "nope"])
        self.assertEqual(code, 2)

    def test_json_flag_works_after_subcommand(self):
        # `plan-show --json f.json` 原先报 unrecognized arguments。
        code, out, _ = self._run(plan_doc([a_change(["create"])]), extra_args=("--json",))
        self.assertEqual(code, 0)
        json.loads(out)

    def test_json_output_carries_no_plaintext_secret(self):
        change = a_change(["create"], after={"sk": "TOPSECRET"}, sensitive={"sk": True})
        code, out, _ = self._run(plan_doc([change]), extra_args=("--json",))
        self.assertEqual(code, 0)
        self.assertNotIn("TOPSECRET", out)


if __name__ == "__main__":
    unittest.main()
