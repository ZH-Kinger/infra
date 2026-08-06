from __future__ import annotations

import unittest
from pathlib import Path


class WorkflowTriggerIsolationTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return Path(".github/workflows", name).read_text(encoding="utf-8")

    def test_ci_is_pr_or_manual_and_ignores_documentation(self):
        workflow = self._read("ci.yml")
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)
        self.assertNotIn("  push:\n", workflow)
        self.assertIn('      - ".github/workflows/**"', workflow)
        self.assertNotIn('      - "README.md"', workflow)
        self.assertNotIn('      - "docs/**"', workflow)

    def test_terraform_apply_is_manual_and_explicit(self):
        workflow = self._read("terraform.yml")
        self.assertIn("  workflow_dispatch:\n", workflow)
        self.assertIn("confirm_apply:", workflow)
        self.assertIn("inputs.confirm_apply", workflow)
        self.assertIn("核对 GitHub OIDC 身份声明", workflow)
        self.assertIn("claims.sub !== expectedSub", workflow)
        self.assertNotIn("  push:\n", workflow)

    def test_cloud_data_workflows_are_manual(self):
        for name in ("dataset-release.yml", "pai-mount-audit.yml", "oidc-role-smoke.yml"):
            workflow = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("  workflow_dispatch:\n", workflow)
                self.assertNotIn("  push:\n", workflow)
                self.assertNotIn("  schedule:\n", workflow)

    def test_oidc_smoke_only_checks_temporary_credentials(self):
        workflow = self._read("oidc-role-smoke.yml")
        self.assertIn("environment: development", workflow)
        self.assertIn("ALIBABA_CLOUD_PLATFORM_APPLY_ROLE_ARN", workflow)
        self.assertIn("ALIBABA_CLOUD_ACCESS_APPLY_ROLE_ARN", workflow)
        self.assertNotIn("terraform apply", workflow)
        self.assertNotIn("--execute", workflow)

    def test_plan_role_trusts_manual_main_but_remains_readonly(self):
        bootstrap = Path("infra/bootstrap/oidc.tf").read_text(encoding="utf-8")
        variables = Path("infra/bootstrap/variables.tf").read_text(encoding="utf-8")
        self.assertIn("repo:${var.github_oidc_repo}:ref:refs/heads/main", bootstrap)
        self.assertIn("platform_apply_subjects", bootstrap)
        self.assertIn("access_apply_subjects", bootstrap)
        self.assertIn('default     = ["development", "production"]', variables)
        self.assertIn('default     = ["development", "production-access"]', variables)
        self.assertIn("DenyAllMutations", bootstrap)

    def test_oidc_subject_uses_immutable_github_ids(self):
        bootstrap = Path("infra/bootstrap/variables.tf").read_text(encoding="utf-8")
        roles = Path("infra/modules/dataset-sink-roles/roles.tf").read_text(encoding="utf-8")
        workflow = self._read("terraform.yml")
        self.assertIn('variable "github_oidc_repo"', bootstrap)
        self.assertIn("repo:${var.github_oidc_repo}:environment:", roles)
        self.assertIn("claims.repository_owner_id", workflow)
        self.assertIn("claims.repository_id", workflow)

    def test_oidc_role_uses_ram_trust_policy_action(self):
        module = Path("infra/modules/ci-oidc-role/main.tf").read_text(encoding="utf-8")
        self.assertIn('Action = "sts:AssumeRole"', module)
        self.assertNotIn('Action = "sts:AssumeRoleWithOIDC"', module)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
