import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dataset_sink.errors import IntegrityError, ReleaseConflictError
from dataset_sink.manifest import Manifest
from dataset_sink.materializer import Materializer, certify_prepared_release, verify_release
from dataset_sink.pai import CpfsRegistration, build_create_dataset_version_request
from dataset_sink.sources import LocalSourceReader


class MaterializerTests(unittest.TestCase):
    def _fixture(self, root: Path, wrong_checksum: bool = False):
        source = root / "source"
        source.mkdir()
        data = b"robotics-sample-data"
        (source / "sample.bin").write_bytes(data)
        digest = "0" * 64 if wrong_checksum else hashlib.sha256(data).hexdigest()
        manifest_path = root / "manifest.jsonl"
        manifest_path.write_text(
            json.dumps(
                {
                    "source_key": "sample.bin",
                    "target_path": "shards/train-000000.bin",
                    "size_bytes": len(data),
                    "sha256": digest,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return source, Manifest.load(manifest_path), data

    def test_materializes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, data = self._fixture(root)
            target = root / "cpfs"
            materializer = Materializer(target, LocalSourceReader(source), workers=2)

            result = materializer.materialize(
                dataset="robotics",
                repository="robotics-data",
                source_reference="v1",
                commit_id="abc123",
                manifest=manifest,
                lakefs_tag="v1",
                paimon_snapshot_id="42",
            )
            second = materializer.materialize(
                dataset="robotics",
                repository="robotics-data",
                source_reference="v1",
                commit_id="abc123",
                manifest=manifest,
                lakefs_tag="v1",
                paimon_snapshot_id="42",
            )

            release = target / "robotics" / "abc123"
            self.assertEqual((release / "shards/train-000000.bin").read_bytes(), data)
            self.assertTrue((release / "_READY").exists())
            self.assertEqual(result, second)
            self.assertEqual(verify_release(release, deep=True), result)

            (release / "shards/train-000000.bin").write_bytes(b"corrupt")
            with self.assertRaises(IntegrityError):
                verify_release(release, deep=True)

    def test_rejects_bad_object_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, _ = self._fixture(root, wrong_checksum=True)
            with self.assertRaises(IntegrityError):
                Materializer(root / "cpfs", LocalSourceReader(source)).materialize(
                    dataset="robotics",
                    repository="robotics-data",
                    source_reference="v1",
                    commit_id="abc123",
                    manifest=manifest,
                )

    def test_rejects_different_manifest_for_existing_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, _ = self._fixture(root)
            materializer = Materializer(root / "cpfs", LocalSourceReader(source))
            materializer.materialize(
                dataset="robotics",
                repository="robotics-data",
                source_reference="v1",
                commit_id="abc123",
                manifest=manifest,
            )
            changed = root / "changed.jsonl"
            changed.write_text(
                '{"source_key":"sample.bin","target_path":"other.bin"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ReleaseConflictError):
                materializer.materialize(
                    dataset="robotics",
                    repository="robotics-data",
                    source_reference="v1",
                    commit_id="abc123",
                    manifest=Manifest.load(changed),
                )

    def test_builds_pai_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, manifest, _ = self._fixture(root)
            result = Materializer(root / "cpfs", LocalSourceReader(source)).materialize(
                dataset="robotics",
                repository="robotics-data",
                source_reference="v1",
                commit_id="abc123",
                manifest=manifest,
            )
            request = build_create_dataset_version_request(
                Path(result.release_path),
                CpfsRegistration(
                    dataset_id="d-example",
                    region="cn-hangzhou",
                    filesystem_id="cpfs-example",
                    uri="cpfs://cpfs-example.cn-hangzhou/datasets/robotics/abc123/",
                    filesystem_path="/datasets/robotics/abc123",
                ),
            )

            self.assertEqual(request["body"]["SourceId"], "abc123")
            self.assertEqual(request["body"]["DataSourceType"], "CPFS")
            self.assertIn("lakefs_commit", str(request["body"]["Labels"]))
            self.assertIn("/datasets/robotics/abc123", request["body"]["ImportInfo"])

    def test_certifies_prepared_cpfs_directory_without_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = root / "cpfs" / "staging" / "batch-1"
            (prepared / "shards").mkdir(parents=True)
            data = b"already-on-cpfs"
            (prepared / "shards/train.bin").write_bytes(data)
            manifest_path = root / "manifest.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source_key": "raw/train.bin",
                        "target_path": "shards/train.bin",
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            target_root = root / "cpfs" / "datasets"
            target_root.mkdir(parents=True)

            result = certify_prepared_release(
                prepared_dir=prepared,
                target_root=target_root,
                dataset="robotics",
                repository="robotics-data",
                source_reference="robotics-v1",
                commit_id="def456",
                manifest=Manifest.load(manifest_path),
                lakefs_tag="robotics-v1",
                paimon_snapshot_id="43",
            )

            release = target_root / "robotics" / "def456"
            self.assertFalse(prepared.exists())
            self.assertEqual((release / "shards/train.bin").read_bytes(), data)
            self.assertEqual(verify_release(release, deep=True), result)


if __name__ == "__main__":
    unittest.main()
