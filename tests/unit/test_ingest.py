from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from dataset_sink.errors import DatasetSinkError, IntegrityError
from dataset_sink.ingest import (
    CommitResult,
    LocalObjectWriter,
    archive_staging,
    build_commit_metadata,
    import_and_commit,
    object_store_uri_for,
    scan_staging,
    validate_destination,
)
from dataset_sink.manifest import Manifest, dump_manifest


class FakeImporter:
    """记录调用参数的假 importer，让 commit 编排逻辑无需真实 lakeFS 即可测试。"""

    def __init__(self) -> None:
        self.calls: list = []

    def import_prefix(self, **kwargs) -> CommitResult:
        self.calls.append(kwargs)
        return CommitResult(
            commit_id="c0ffee1234",
            ingested_objects=len(kwargs),
            branch=kwargs["branch"],
            tag=kwargs["tag"],
            object_store_uri=kwargs["object_store_uri"],
        )


class ScanTests(unittest.TestCase):
    def test_scans_and_hashes_staging_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shards").mkdir()
            (root / "shards" / "train-000000.bin").write_bytes(b"hello")
            (root / "shards" / "train-000001.bin").write_bytes(b"world!")

            result = scan_staging(root, workers=2)

            self.assertEqual(result.file_count, 2)
            self.assertEqual(result.total_bytes, 11)
            paths = [e.target_path for e in result.entries]
            # 顺序必须确定，否则同样的数据会产出不同的 manifest_sha256
            self.assertEqual(paths, ["shards/train-000000.bin", "shards/train-000001.bin"])
            self.assertEqual(result.entries[0].sha256, hashlib.sha256(b"hello").hexdigest())
            # staging 布局即 release 布局，两者相同
            self.assertEqual(result.entries[0].source_key, result.entries[0].target_path)

    def test_fails_on_files_that_certify_would_later_reject(self):
        # scan 与 certify 必须对「什么算数据集内容」用同一个定义。
        # 如果这里悄悄跳过，使用者会在归档完一整轮之后才撞上 certify 的报错。
        for junk in ["_READY", "release.json", "manifest.jsonl", ".DS_Store"]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "data.bin").write_bytes(b"x")
                (root / junk).write_bytes(b"junk")

                with self.assertRaises(DatasetSinkError) as ctx:
                    scan_staging(root)
                self.assertIn(junk, str(ctx.exception))

    def test_accepts_a_clean_staging_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.bin").write_bytes(b"x")
            result = scan_staging(root)
            self.assertEqual([e.target_path for e in result.entries], ["data.bin"])

    def test_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "real.bin").write_bytes(b"x")
            (root / "link.bin").symlink_to(root / "real.bin")

            with self.assertRaises(DatasetSinkError):
                scan_staging(root)

    def test_rejects_empty_staging(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(DatasetSinkError):
            scan_staging(Path(tmp))


class ArchiveTests(unittest.TestCase):
    def _staging(self, root: Path) -> Manifest:
        (root / "shards").mkdir()
        (root / "shards" / "a.bin").write_bytes(b"aaaa")
        (root / "shards" / "b.bin").write_bytes(b"bb")
        scan = scan_staging(root)
        manifest_path = root.parent / "manifest.jsonl"
        dump_manifest(scan.entries, manifest_path)
        return Manifest.load(manifest_path)

    def test_uploads_all_then_skips_on_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            staging = base / "staging"
            staging.mkdir()
            manifest = self._staging(staging)
            writer = LocalObjectWriter(base / "oss")

            first = archive_staging(staging, manifest, writer, prefix="staging/batch-1")
            self.assertEqual(first.uploaded, 2)
            self.assertEqual(first.skipped_existing, 0)
            self.assertEqual(first.total_bytes, 6)
            self.assertTrue((base / "oss" / "staging/batch-1/shards/a.bin").is_file())

            # 幂等：重跑只补缺口，不把已传完的重传一遍
            second = archive_staging(staging, manifest, writer, prefix="staging/batch-1")
            self.assertEqual(second.uploaded, 0)
            self.assertEqual(second.skipped_existing, 2)

    def test_detects_staging_modified_after_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            staging = base / "staging"
            staging.mkdir()
            manifest = self._staging(staging)

            # 扫描之后有人改了数据——归档必须拦下来，否则 Commit 会指向
            # 与 manifest 不符的字节，整条校验链就断了
            (staging / "shards" / "a.bin").write_bytes(b"zzzz")

            writer = LocalObjectWriter(base / "oss")
            with self.assertRaises(IntegrityError):
                archive_staging(staging, manifest, writer, prefix="p", workers=1)

    def test_rejects_empty_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            staging = base / "staging"
            staging.mkdir()
            manifest = self._staging(staging)
            with self.assertRaises(DatasetSinkError):
                archive_staging(staging, manifest, LocalObjectWriter(base), prefix="  /  ")


class CommitTests(unittest.TestCase):
    def test_builds_uri_with_trailing_slash(self):
        # 末尾斜杠有意义：没有它 lakeFS 可能当成单个对象而不是前缀
        self.assertEqual(
            object_store_uri_for("s3://bucket/", "staging/batch-1"),
            "s3://bucket/staging/batch-1/",
        )

    def test_rejects_unsafe_destination(self):
        for bad in ["a/../../etc", "  ", "  /  ", "."]:
            with self.assertRaises(DatasetSinkError):
                validate_destination(bad)
        # 前导/尾随斜杠是有意允许并规范化掉的，不算不安全
        self.assertEqual(validate_destination("/datasets/robotics/"), "datasets/robotics")
        # 返回值必须是折叠后的形式，否则 lakeFS 里会出现字面量 "./x" 路径
        self.assertEqual(validate_destination("./x"), "x")
        self.assertEqual(validate_destination("a//b"), "a/b")

    def test_metadata_carries_the_cross_system_join_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.bin").write_bytes(b"abc")
            scan = scan_staging(root)
            path = root / "manifest.jsonl"
            dump_manifest(scan.entries, path)
            manifest = Manifest.load(path)

            metadata = build_commit_metadata(
                manifest=manifest, scan=scan, paimon_snapshot_id="1842"
            )
            # manifest_sha256 要贯穿 lakeFS Commit / release.json /
            # PAI Label / 训练环境变量四处，是交叉校验的锚点
            self.assertEqual(metadata["manifest_sha256"], manifest.sha256)
            self.assertEqual(metadata["paimon_snapshot_id"], "1842")
            self.assertEqual(metadata["file_count"], "1")

    def test_import_passes_normalized_arguments(self):
        importer = FakeImporter()
        result = import_and_commit(
            repository="robotics-data",
            branch="main",
            object_store_uri="s3://bucket/staging/batch-1/",
            destination="/datasets/robotics/",
            message="msg",
            metadata={"manifest_sha256": "deadbeef"},
            tag="robotics-v1",
            importer=importer,
        )
        self.assertEqual(result.commit_id, "c0ffee1234")
        call = importer.calls[0]
        self.assertEqual(call["destination"], "datasets/robotics")
        self.assertEqual(call["tag"], "robotics-v1")

    def test_requires_credentials_when_no_importer_given(self):
        with self.assertRaises(ValueError):
            import_and_commit(
                repository="r",
                branch="main",
                object_store_uri="s3://b/p/",
                destination="d",
                message="m",
            )


if __name__ == "__main__":
    unittest.main()
