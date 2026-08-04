from __future__ import annotations

import unittest

from dataset_sink.terraform_guard import forbidden_terraform_subcommand


class TerraformGuardTests(unittest.TestCase):
    def test_blocks_direct_and_prefixed_mutations(self):
        cases = {
            "terraform apply plan.tfplan": "apply",
            "TF_IN_AUTOMATION=1 terraform -chdir=infra/envs/dev/platform destroy": "destroy",
            "env TF_LOG=info /opt/bin/terraform import x.y id": "import",
            "cd infra && command terraform state list": "state",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(forbidden_terraform_subcommand(command), expected)

    def test_blocks_nested_shell_command(self):
        self.assertEqual(
            forbidden_terraform_subcommand("bash -c 'TF_LOG=info terraform apply'"), "apply"
        )

    def test_allows_readonly_and_mentions(self):
        for command in (
            "terraform plan -out=tfplan",
            "terraform -chdir=infra validate",
            "terraform fmt -recursive infra",
            "echo 'terraform apply is forbidden'",
            "python3 -c 'print(\"terraform destroy\")'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(forbidden_terraform_subcommand(command))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
