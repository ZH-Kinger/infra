from __future__ import annotations

import unittest
from pathlib import Path


class DatasetPlatformUserSkillTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("skills/dataset-platform-user")
        self.skill = (self.root / "SKILL.md").read_text(encoding="utf-8")

    def test_skill_metadata_and_interface_are_discoverable(self):
        self.assertTrue(self.skill.startswith("---\nname: dataset-platform-user\n"))
        self.assertIn("description:", self.skill.split("---", 2)[1])
        self.assertNotIn("TODO", self.skill)

        interface = (self.root / "agents/openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "训练数据平台用户助手"', interface)
        self.assertIn("$dataset-platform-user", interface)

    def test_skill_keeps_user_requests_inside_the_governed_boundary(self):
        for contract in (
            "Default every write-capable request to plan-only",
            "Never create RAM users",
            "Never train from raw OSS",
            "Never silently fall back",
            "Never bypass `_READY`",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, self.skill)

    def test_references_cover_requests_and_escalation(self):
        request = (self.root / "references/request-contract.md").read_text(encoding="utf-8")
        errors = (self.root / "references/errors-and-escalation.md").read_text(encoding="utf-8")
        for workflow in (
            "dataset-release.yml",
            "pai-runtime.yml",
            "dataset-lifecycle.yml",
            "pai-mount-audit.yml",
        ):
            self.assertIn(workflow, request)
        self.assertIn("Never include AccessKey", errors)


if __name__ == "__main__":
    unittest.main()
