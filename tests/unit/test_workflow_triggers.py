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
        self.assertNotIn("  push:\n", workflow)

    def test_cloud_data_workflows_are_manual(self):
        for name in ("dataset-release.yml", "pai-mount-audit.yml"):
            workflow = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("  workflow_dispatch:\n", workflow)
                self.assertNotIn("  push:\n", workflow)
                self.assertNotIn("  schedule:\n", workflow)

    def test_plan_role_trusts_manual_main_but_remains_readonly(self):
        bootstrap = Path("infra/bootstrap/oidc.tf").read_text(encoding="utf-8")
        variables = Path("infra/bootstrap/variables.tf").read_text(encoding="utf-8")
        self.assertIn("repo:${var.github_repo}:ref:refs/heads/main", bootstrap)
        self.assertIn("platform_apply_subjects", bootstrap)
        self.assertIn("access_apply_subjects", bootstrap)
        self.assertIn('default     = ["development", "production"]', variables)
        self.assertIn('default     = ["development", "production-access"]', variables)
        self.assertIn("DenyAllMutations", bootstrap)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
