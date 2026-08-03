from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dataset_sink.errors import DatasetSinkError
from dataset_sink.registry import MODES, build_registry, load_registry

ENTRIES = [
    {
        "name": "robotics-legacy",
        "bucket": "legacy-data",
        "prefix": "legacy/robotics",
        "mode": "readonly",
    },
    {"name": "robotics-archive", "bucket": "ds-archive", "prefix": "releases", "mode": "archive"},
    {"name": "whole-bucket", "bucket": "open-bucket", "prefix": "", "mode": "readonly"},
]


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry(ENTRIES)

    def test_resolves_exact_and_nested_prefixes(self):
        self.assertEqual(self.reg.resolve("legacy-data", "legacy/robotics").name, "robotics-legacy")
        self.assertEqual(
            self.reg.resolve("legacy-data", "legacy/robotics/2026/01").name, "robotics-legacy"
        )

    def test_sibling_prefix_is_not_covered(self):
        # legacy/robotics 不覆盖 legacy/robotics-old——只比字符串前缀会放行相邻目录
        with self.assertRaises(DatasetSinkError):
            self.reg.resolve("legacy-data", "legacy/robotics-old")

    def test_different_bucket_is_not_covered(self):
        with self.assertRaises(DatasetSinkError):
            self.reg.resolve("other-bucket", "legacy/robotics")

    def test_empty_prefix_covers_whole_bucket(self):
        self.assertEqual(self.reg.resolve("open-bucket", "anything/at/all").name, "whole-bucket")

    def test_unregistered_error_lists_known_locations_and_who_to_ask(self):
        with self.assertRaises(DatasetSinkError) as ctx:
            self.reg.resolve("nope", "x")
        message = str(ctx.exception)
        self.assertIn("不在数据源注册表里", message)
        self.assertIn("legacy-data/legacy/robotics", message)  # 告诉用户有哪些可选
        self.assertIn("管理员", message)  # 告诉用户找谁

    def test_longest_match_wins(self):
        reg = build_registry(
            [
                {"name": "broad", "bucket": "b", "prefix": "data", "mode": "readonly"},
                {"name": "narrow", "bucket": "b", "prefix": "data/robotics", "mode": "archive"},
            ]
        )
        self.assertEqual(reg.resolve("b", "data/robotics/x").name, "narrow")


class WritabilityTests(unittest.TestCase):
    def setUp(self):
        self.reg = build_registry(ENTRIES)

    def test_archive_mode_is_writable(self):
        self.assertEqual(
            self.reg.assert_writable("ds-archive", "releases/robotics").mode, "archive"
        )

    def test_readonly_mode_refuses_writes(self):
        # 写进只读数据源会让已发布的 Commit 悬空，而那种损坏当时不报错
        with self.assertRaises(DatasetSinkError) as ctx:
            self.reg.assert_writable("legacy-data", "legacy/robotics")
        self.assertIn("不允许写入", str(ctx.exception))
        self.assertIn("悬空", str(ctx.exception))


class ValidationTests(unittest.TestCase):
    def test_rejects_unknown_mode(self):
        with self.assertRaises(DatasetSinkError):
            build_registry([{"name": "a", "bucket": "b", "mode": "whatever"}])

    def test_rejects_duplicate_names(self):
        with self.assertRaises(DatasetSinkError):
            build_registry([{"name": "a", "bucket": "b1"}, {"name": "a", "bucket": "b2"}])

    def test_rejects_missing_bucket(self):
        with self.assertRaises(DatasetSinkError):
            build_registry([{"name": "a"}])

    def test_defaults_to_readonly(self):
        self.assertEqual(build_registry([{"name": "a", "bucket": "b"}]).sources[0].mode, "readonly")


class LoadTests(unittest.TestCase):
    def test_loads_from_terraform_output_shape(self):
        p = Path(tempfile.mkdtemp()) / "data-sources.json"
        p.write_text(json.dumps({"data_sources": ENTRIES}), encoding="utf-8")
        self.assertEqual(len(load_registry(p).sources), 3)

    def test_missing_file_says_how_to_generate_it(self):
        with self.assertRaises(DatasetSinkError) as ctx:
            load_registry(Path("/nonexistent/data-sources.json"))
        self.assertIn("Terraform", str(ctx.exception))

    def test_malformed_json_is_reported_clearly(self):
        p = Path(tempfile.mkdtemp()) / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(DatasetSinkError) as ctx:
            load_registry(p)
        self.assertIn("不是合法 JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class WorkspaceModeTests(unittest.TestCase):
    """工作区可写、可扫描，但**绝不能**作为 Commit 的来源。

    零拷贝 import 只记录物理地址不复制字节。Commit 指向可写位置，等于让已发布
    的版本可以被静默篡改——verify --deep 要到下一次校验才发现，而那时
    training-guard 已经放行过训练了。
    """

    def setUp(self):
        self.reg = build_registry(
            [
                {
                    "name": "alice",
                    "bucket": "workspaces",
                    "prefix": "users/alice",
                    "mode": "workspace",
                },
                {"name": "shared", "bucket": "workspaces", "prefix": "shared", "mode": "workspace"},
                {
                    "name": "archive",
                    "bucket": "ds-archive",
                    "prefix": "releases",
                    "mode": "archive",
                },
                {"name": "legacy", "bucket": "legacy", "prefix": "raw", "mode": "readonly"},
            ]
        )

    def test_workspace_is_writable(self):
        self.assertEqual(
            self.reg.assert_writable("workspaces", "users/alice/exp").mode, "workspace"
        )

    def test_workspace_is_rejected_as_commit_source(self):
        with self.assertRaises(DatasetSinkError) as ctx:
            self.reg.assert_commit_source("workspaces", "users/alice/exp")
        message = str(ctx.exception)
        self.assertIn("不能作为 lakeFS Commit 的来源", message)
        self.assertIn("静默篡改", message)
        self.assertIn("archive", message)  # 告诉用户正确做法

    def test_shared_workspace_is_rejected_too(self):
        # 公共区「全体可读写」，比个人区更不稳定
        with self.assertRaises(DatasetSinkError):
            self.reg.assert_commit_source("workspaces", "shared/scratch")

    def test_archive_and_readonly_are_valid_commit_sources(self):
        self.assertEqual(self.reg.assert_commit_source("ds-archive", "releases/x").mode, "archive")
        self.assertEqual(self.reg.assert_commit_source("legacy", "raw/2026").mode, "readonly")

    def test_readonly_is_not_writable(self):
        with self.assertRaises(DatasetSinkError):
            self.reg.assert_writable("legacy", "raw/2026")


class RenderedRegistryContractTest(unittest.TestCase):
    """守住 Terraform 与 Python 之间的 mode 契约。

    注册表由 Terraform 的 `data_sources` 变量渲染成 deploy/data-sources.json，
    Python 只是消费方。所以 **Terraform 表达不了的 mode 等于不存在**——
    曾经出现过 registry.py 支持 workspace 而 Terraform 的 validation 只允许
    readonly/archive 的情况，结果是这个 mode 永远无法通过管理面声明。

    这个测试直接读渲染产物而不是 fixture：fixture 会跟着代码一起改，
    渲染产物不会。
    """

    RENDERED = Path(__file__).resolve().parents[2] / "deploy" / "data-sources.json"

    def test_rendered_document_parses(self):
        registry = load_registry(self.RENDERED)
        self.assertTrue(registry.sources, "渲染出的注册表不该是空的")

    def test_every_mode_is_exercised_by_the_render_fixture(self):
        """render.tfvars 必须为每个 mode 都放一条占位数据源。

        deploy/ram/*.json 是评审时实际会看的东西。少放一个 mode，那个 mode
        生成的语句形状就从未被任何人看到过。
        """
        registry = load_registry(self.RENDERED)
        rendered_modes = {s.mode for s in registry.sources}
        self.assertEqual(
            rendered_modes,
            set(MODES),
            "render.tfvars 的 data_sources 没有覆盖全部 mode；"
            "缺的那个在 deploy/ram/*.json 里看不到语句形状",
        )
