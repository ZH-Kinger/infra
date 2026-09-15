"""`_delivery.yml` 的契约，以及 workflow 依赖的 `delivery describe` 输出格式。

按本仓库既有做法（test_workflow_triggers.py）对 YAML 文本做断言：这些是**安全门禁**
的形状，改动时应当被迫看到这里变红。
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from delivery.cli import main


class DescribeOutputTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _fields(self, platform):
        code, out, _ = self._run(["describe", platform])
        self.assertEqual(code, 0)
        return dict(line.split("=", 1) for line in out.strip().splitlines())

    def test_booleans_are_lowercase(self):
        # GitHub Actions 的 if: 比较字符串。Python 的 "True" 与 == 'true' 不相等，
        # 会让 apply 门禁**恒为假**——静默跳过执行，而不是报错，极难发现。
        fields = self._fields("aliyun")
        for key in ("apply", "policy_as_code", "require_approval", "keyless"):
            self.assertIn(fields[key], ("true", "false"), key)

    def test_emits_every_field_the_workflow_reads(self):
        # _delivery.yml 的 describe Job 把这些挂在 outputs 上，缺一个就是空串，
        # 而空串在 if: 里恒为假 —— 又一个静默跳过。
        fields = self._fields("volcano")
        for key in (
            "id",
            "display",
            "auth",
            "iac",
            "plan",
            "inventory",
            "login",
            "apply",
            "status",
            "keyless",
        ):
            self.assertTrue(fields.get(key), key)

    def test_volcano_reports_not_appliable(self):
        fields = self._fields("volcano")
        self.assertEqual(fields["apply"], "false")
        self.assertEqual(fields["status"], "pending-verification")

    def test_aliyun_reports_appliable(self):
        fields = self._fields("aliyun")
        self.assertEqual(fields["apply"], "true")

    def test_unknown_platform_exits_two(self):
        code, _, err = self._run(["describe", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("未知平台", err)

    def test_json_form_is_available(self):
        import json

        code, out, _ = self._run(["--json", "describe", "aliyun"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["id"], "aliyun")


class MatrixTests(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, out.getvalue()

    def test_single_line_output(self):
        # workflow 里是 `echo "matrix=$(...)" >> $GITHUB_OUTPUT`，多行会被截断
        code, out = self._run(["matrix"])
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_iac_filter_keeps_only_terraform_platforms(self):
        import json

        _, out = self._run(["matrix", "--iac", "terraform"])
        ids = {row["platform"] for row in json.loads(out)["include"]}
        self.assertEqual(ids, {"aliyun", "volcano"})

    def test_appliable_filter_excludes_unverified_platforms(self):
        import json

        _, out = self._run(["matrix", "--appliable"])
        ids = {row["platform"] for row in json.loads(out)["include"]}
        self.assertIn("aliyun", ids)
        self.assertNotIn("volcano", ids)  # pending-verification ⇒ apply=false

    def test_empty_result_is_the_shape_the_workflow_compares_against(self):
        # deliver.yml 用字符串比较 '{"include":[]}' 判空，格式变了那条判断会失效
        import json

        _, out = self._run(["matrix", "--iac", "nonexistent"])
        self.assertEqual(out.strip(), json.dumps({"include": []}, separators=(",", ":")))


class CallerWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = Path(".github/workflows/deliver.yml").read_text(encoding="utf-8")

    def test_pr_path_uses_dynamic_matrix(self):
        self.assertIn("delivery.cli matrix --iac terraform", self.wf)
        self.assertIn("fromJSON(needs.discover.outputs.matrix)", self.wf)

    def test_one_platform_failing_does_not_block_others(self):
        self.assertIn("fail-fast: false", self.wf)

    def test_pr_path_never_executes(self):
        preview = self.wf.split("  preview:", 1)[1].split("  deliver:", 1)[0]
        self.assertIn("execute: false", preview)

    def test_manual_path_requires_explicit_execute(self):
        self.assertIn("execute: ${{ inputs.execute }}", self.wf)
        self.assertIn("default: false", self.wf)

    def test_access_layer_gets_its_own_approval_environment(self):
        # 权限变更不该和改个桶走同一批审批人
        self.assertIn("production-access", self.wf)

    def test_empty_matrix_check_matches_cli_output_format(self):
        self.assertIn("""'{"include":[]}'""", self.wf)


class DeliveryWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wf = Path(".github/workflows/_delivery.yml").read_text(encoding="utf-8")

    def test_is_a_reusable_workflow(self):
        self.assertIn("  workflow_call:\n", self.wf)
        # 不能自己被触发：它是被调用的，直接触发会绕过调用方的参数约束
        self.assertNotIn("  push:\n", self.wf)
        self.assertNotIn("  pull_request:\n", self.wf)

    def test_capabilities_come_from_the_descriptor(self):
        # 能力门禁必须读描述符，不能在 workflow 里硬编码平台名
        self.assertIn("delivery.cli describe", self.wf)
        self.assertIn("$GITHUB_OUTPUT", self.wf)

    def test_plan_job_skipped_when_no_trustworthy_preview(self):
        self.assertIn("needs.describe.outputs.plan != 'none'", self.wf)

    def test_apply_requires_all_three_gates(self):
        # 描述符允许 + 调用方显式要求 + 计划未被阻断，缺一不可
        self.assertIn("needs.describe.outputs.apply == 'true'", self.wf)
        self.assertIn("inputs.execute", self.wf)
        self.assertIn("needs.plan.outputs.blocked == 'false'", self.wf)

    def test_apply_environment_follows_the_scope(self):
        # foundation 必须挂 Environment（审批）；workspace 配额内自助时不挂——
        # 让本该秒过的日常申请去排审批队列，结果是大家绕过流水线自己点控制台。
        self.assertIn("needs.describe.outputs.require_approval == 'true'", self.wf)
        self.assertIn("inputs.environment", self.wf)

    def test_scope_drives_capability_lookup(self):
        # 门禁取的必须是 scope 级能力，不能退回平台级
        self.assertIn('--scope "${{ inputs.scope }}"', self.wf)

    def test_plan_and_apply_use_the_same_resolved_path(self):
        """两处路径不一致的话，apply 会去 cp 一个不存在的 tfplan。

        断的是「路径只有一个来源」，不是某个具体字符串——原来那版把
        `infra/<scope>/<platform>/<env>/<layer>` 这个约定写死在断言里，
        而仓库真实布局是 `infra/envs/<env>/<layer>`，等于测试在保护一个错的约定。
        """
        source = "${{ needs.describe.outputs.stack_dir }}"
        self.assertGreaterEqual(self.wf.count(source), 3)
        # 不能再有任何一处自己拼路径——否则两边可以各拼各的
        self.assertNotIn("working-directory: infra/${{ inputs.", self.wf)

    def test_stack_dir_is_resolved_once_and_checked_to_exist(self):
        """目录不存在要在 describe 阶段就失败。

        否则错误会推迟到 terraform init 报「没有配置文件」——那时已经换过临时
        身份、过了审批门，排查的人还得回头猜是路径错了还是仓库没同步。
        """
        self.assertIn("stack_dir: ${{ steps.stack.outputs.dir }}", self.wf)
        self.assertIn("Terraform 目录不存在", self.wf)
        self.assertIn("不是 Terraform 根模块", self.wf)

    def test_apply_reuses_the_approved_artifact_and_never_replans(self):
        # apply 若自己重新 plan，审批看过的和最终执行的可以是两份东西
        self.assertIn("actions/download-artifact", self.wf)
        self.assertIn("terraform apply -input=false tfplan", self.wf)
        apply_section = self.wf.split("  apply:\n", 1)[1].split("  reconcile:", 1)[0]
        self.assertNotIn("terraform plan", apply_section)

    def test_volcano_credentials_fail_loudly_until_verified(self):
        # 宁可显式失败，也不要写一个看起来能用、实际没验过的取凭证步骤：
        # 那样会静默回落到某个默认凭证链。
        self.assertIn("inputs.platform == 'volcano'", self.wf)
        self.assertIn("exit 1", self.wf)
        self.assertIn("pending-verification", self.wf)

    def test_oidc_claims_checked_before_exchanging_for_credentials(self):
        # 顺序不能反：拿到凭证之后才发现 sub 不对，权限已经到手了
        claims_at = self.wf.index("核对 GitHub OIDC 身份声明")
        creds_at = self.wf.index("取阿里云临时凭证")
        self.assertLess(claims_at, creds_at)

    def test_plan_stays_read_only(self):
        self.assertIn("-lock=false", self.wf)

    def test_no_credentials_in_the_workflow_file(self):
        for marker in ("LTAI", "access_key_secret", "AccessKeySecret"):
            self.assertNotIn(marker, self.wf)


if __name__ == "__main__":
    unittest.main()
