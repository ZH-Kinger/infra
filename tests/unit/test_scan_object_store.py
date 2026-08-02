from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import BinaryIO, Iterable, List, Tuple

from dataset_sink.errors import DatasetSinkError, IntegrityError
from dataset_sink.ingest import (
    LocalObjectReader,
    assert_manifest_matches_destination,
    scan_object_store,
)
from dataset_sink.manifest import Manifest, dump_manifest

DEST = "datasets/robotics"


class DictObjectReader:
    """内存对象存储，用来构造真实文件系统造不出来的对象键。

    OSS 的键空间比文件系统宽松：`a//b`、`a/./b`、`a/../b` 是三个不同的键，
    但落到 CPFS 上会塌缩成同一个路径。这类用例只能靠假 reader 覆盖。
    """

    def __init__(self, objects: dict) -> None:
        self.objects = {key: value.encode() for key, value in objects.items()}

    def list_objects(self, prefix: str) -> Iterable[Tuple[str, int]]:
        for key in sorted(self.objects):
            if key.startswith(prefix):
                yield key, len(self.objects[key])

    def open(self, key: str) -> BinaryIO:
        import io

        return io.BytesIO(self.objects[key])


class LyingSizeReader(DictObjectReader):
    """列举报一个大小、读取给另一个大小，模拟扫描期间被改写的对象。"""

    def list_objects(self, prefix: str) -> Iterable[Tuple[str, int]]:
        for key, size in super().list_objects(prefix):
            yield key, size + 10


class ScanObjectStoreTests(unittest.TestCase):
    def _reader(self, files: dict) -> LocalObjectReader:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return LocalObjectReader(root)

    def test_lists_existing_prefix_and_hashes_content(self):
        reader = self._reader(
            {
                "legacy/robotics/shards/a.bin": "aaa",
                "legacy/robotics/shards/b.bin": "bbbb",
                "legacy/other/skip.bin": "zzzzz",
            }
        )
        result = scan_object_store(reader, "legacy/robotics", DEST)

        self.assertTrue(result.digested)
        self.assertEqual(result.file_count, 2)
        self.assertEqual(result.total_bytes, 7)
        # 前缀被剥掉：target_path 是 release 内的相对路径。
        self.assertEqual(
            [e.target_path for e in result.entries],
            ["shards/a.bin", "shards/b.bin"],
        )
        self.assertEqual(
            result.entries[0].sha256,
            hashlib.sha256(b"aaa").hexdigest(),
        )

    def test_no_digest_records_size_only(self):
        reader = self._reader({"legacy/x/a.bin": "aaa"})
        result = scan_object_store(reader, "legacy/x", DEST, with_digest=False)

        self.assertFalse(result.digested)
        self.assertEqual(result.entries[0].size_bytes, 3)
        self.assertIsNone(result.entries[0].sha256)

    def test_prefix_boundary_is_not_a_string_prefix_match(self):
        # `legacy/x-archive/` 不属于 `legacy/x/`，不能因为字符串前缀相同就混进来。
        reader = self._reader(
            {
                "legacy/x/a.bin": "aaa",
                "legacy/x-archive/b.bin": "bbb",
            }
        )
        result = scan_object_store(reader, "legacy/x", DEST)
        self.assertEqual([e.target_path for e in result.entries], ["a.bin"])

    def test_empty_prefix_is_rejected(self):
        reader = self._reader({"legacy/x/a.bin": "aaa"})
        with self.assertRaises(DatasetSinkError):
            scan_object_store(reader, "  /  ", DEST)

    def test_missing_prefix_reports_clearly(self):
        reader = self._reader({"legacy/x/a.bin": "aaa"})
        with self.assertRaises(DatasetSinkError) as ctx:
            scan_object_store(reader, "legacy/nope", DEST)
        self.assertIn("没有对象", str(ctx.exception))

    def test_directory_marker_objects_are_ignored(self):
        reader = DictObjectReader({"legacy/x/": "", "legacy/x/a.bin": "aaa"})
        result = scan_object_store(reader, "legacy/x", DEST)
        self.assertEqual([e.target_path for e in result.entries], ["a.bin"])

    def test_dot_dot_in_object_key_is_rejected(self):
        # 直接放行的话，materialize 会把文件写到 release 目录外面。
        reader = DictObjectReader({"legacy/x/../escape.bin": "aaa"})
        with self.assertRaises(DatasetSinkError) as ctx:
            scan_object_store(reader, "legacy/x", DEST)
        self.assertIn("无法安全落到文件系统", str(ctx.exception))

    def test_empty_path_segment_is_rejected(self):
        # `a//b` 与 `a/b` 在 OSS 里是两个对象，落到文件系统会塌缩成一个：
        # 后写的覆盖先写的，release 少了文件但 file_count 仍然对得上。
        reader = DictObjectReader({"legacy/x/a//b.bin": "aaa"})
        with self.assertRaises(DatasetSinkError) as ctx:
            scan_object_store(reader, "legacy/x", DEST)
        self.assertIn("规范化", str(ctx.exception))

    def test_size_changing_under_scan_is_rejected(self):
        reader = LyingSizeReader({"legacy/x/a.bin": "aaa"})
        with self.assertRaises(IntegrityError) as ctx:
            scan_object_store(reader, "legacy/x", DEST)
        self.assertIn("冻结写入", str(ctx.exception))

    def test_entries_are_sorted_so_manifest_digest_is_stable(self):
        files = {f"legacy/x/{name}.bin": name for name in ("c", "a", "b")}
        reader = self._reader(files)
        first: List[str] = [
            e.target_path for e in scan_object_store(reader, "legacy/x", DEST).entries
        ]
        second: List[str] = [
            e.target_path for e in scan_object_store(reader, "legacy/x", DEST).entries
        ]
        self.assertEqual(first, ["a.bin", "b.bin", "c.bin"])
        self.assertEqual(first, second)


class SourceKeyCoordinateTests(unittest.TestCase):
    """source_key 与 target_path 是两个坐标系，混淆会让 materialize 全量 404。"""

    def _reader(self, files: dict) -> LocalObjectReader:
        root = Path(tempfile.mkdtemp())
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return LocalObjectReader(root)

    def test_source_key_is_the_path_inside_the_commit(self):
        reader = self._reader({"legacy/robotics/shards/a.bin": "aaa"})
        entry = scan_object_store(reader, "legacy/robotics", "datasets/robotics").entries[0]

        # release 内的路径不带 destination 前缀……
        self.assertEqual(entry.target_path, "shards/a.bin")
        # ……但 Commit 内的路径带，因为 import 把对象放到了 destination 下面，
        # 而 materialize 用 `<commit>/<source_key>` 去 lakeFS 取对象。
        self.assertEqual(entry.source_key, "datasets/robotics/shards/a.bin")

    def test_destination_is_normalized_the_same_way_commit_normalizes_it(self):
        reader = self._reader({"legacy/x/a.bin": "aaa"})
        entry = scan_object_store(reader, "legacy/x", "/datasets/robotics/").entries[0]
        self.assertEqual(entry.source_key, "datasets/robotics/a.bin")

    def test_empty_destination_is_rejected(self):
        reader = self._reader({"legacy/x/a.bin": "aaa"})
        with self.assertRaises(DatasetSinkError):
            scan_object_store(reader, "legacy/x", "  ")

    def test_mismatched_destination_is_caught_before_the_commit_is_created(self):
        reader = self._reader({"legacy/x/a.bin": "aaa"})
        result = scan_object_store(reader, "legacy/x", "datasets/robotics")
        out = Path(tempfile.mkdtemp()) / "manifest.jsonl"
        dump_manifest(result.entries, out)
        manifest = Manifest.load(out)

        # 同一份 manifest 配对的 destination 一致时通过……
        assert_manifest_matches_destination(manifest, "datasets/robotics")
        # ……填错就必须在建 Commit 之前失败，而不是等 materialize 全量 404。
        with self.assertRaises(DatasetSinkError) as ctx:
            assert_manifest_matches_destination(manifest, "datasets/wrong")
        self.assertIn("必须填同一个值", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
