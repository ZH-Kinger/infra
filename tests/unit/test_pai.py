from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Optional

from dataset_sink.pai import _labels_for


@dataclass
class FakeRelease:
    """只带 _labels_for 用到的字段，形状与 MaterializationResult 一致。"""

    lakefs_commit: str = "8fe7fda594afb1ea198f0ce507c62499a6052a4eed4665f1306adc97f24d1e3a"
    manifest_sha256: str = "233c0e0e1a240ef910ce95bc6b576524b7c65f01809c88689e34f0f0fe0eccbb"
    dataset: str = "robotics-oss"
    repository: str = "robotics-data"
    lakefs_tag: Optional[str] = "robotics-v2026.08.03.1"
    paimon_snapshot_id: Optional[str] = "1842"


class DatasetVersionLabelTests(unittest.TestCase):
    """PAI Dataset Version 的标签是 PAI 控制台和 DSW 里唯一的可检索面。

    Version 名字由 PAI 自动分配成 v1/v2/v3，本身没有含义。所以标签少了什么，
    DSW 用户就永远找不到什么——这不是锦上添花，是他们每天都要做的选择。
    """

    def labels(self, release) -> dict:
        return {item["Key"]: item["Value"] for item in _labels_for(release)}

    def test_machine_verifiable_identity_is_present(self):
        """training-guard 和 verify --deep 靠这两个确认挂上来的是哪个版本。"""
        labels = self.labels(FakeRelease())
        self.assertEqual(labels["lakefs_commit"], FakeRelease.lakefs_commit)
        self.assertEqual(labels["manifest_sha256"], FakeRelease.manifest_sha256)

    def test_human_searchable_fields_are_present(self):
        labels = self.labels(FakeRelease())
        self.assertEqual(labels["dataset"], "robotics-oss")
        self.assertEqual(labels["repository"], "robotics-data")
        self.assertEqual(labels["lakefs_tag"], "robotics-v2026.08.03.1")
        self.assertEqual(labels["paimon_snapshot_id"], "1842")

    def test_lakefs_tag_is_the_thing_humans_search_by(self):
        """曾经漏掉过 lakefs_tag，结果 DSW 下拉框里只有 64 位 hex 可看。"""
        self.assertIn("lakefs_tag", self.labels(FakeRelease()))

    def test_absent_optional_fields_do_not_become_the_string_None(self):
        """留空就不打标签。

        `paimon_snapshot_id = "None"` 这种字面量值比没有这个标签更糟：
        它看起来像一个真实取值，按它检索会得到一批毫不相关的版本。
        """
        labels = self.labels(FakeRelease(lakefs_tag=None, paimon_snapshot_id=None))
        self.assertNotIn("lakefs_tag", labels)
        self.assertNotIn("paimon_snapshot_id", labels)
        self.assertNotIn("None", labels.values())
        # 必填的四个仍然在
        self.assertEqual(set(labels), {"lakefs_commit", "manifest_sha256", "dataset", "repository"})

    def test_empty_string_is_treated_as_absent(self):
        labels = self.labels(FakeRelease(lakefs_tag="", paimon_snapshot_id=""))
        self.assertNotIn("lakefs_tag", labels)
        self.assertNotIn("paimon_snapshot_id", labels)

    def test_non_string_snapshot_id_is_stringified(self):
        """Paimon snapshot id 在别处是 int，PAI 的标签值必须是字符串。"""
        labels = self.labels(FakeRelease(paimon_snapshot_id=1842))
        self.assertEqual(labels["paimon_snapshot_id"], "1842")
        self.assertIsInstance(labels["paimon_snapshot_id"], str)

    def test_every_label_value_is_a_string(self):
        for item in _labels_for(FakeRelease(paimon_snapshot_id=99)):
            self.assertIsInstance(item["Value"], str, item)


if __name__ == "__main__":
    unittest.main()
