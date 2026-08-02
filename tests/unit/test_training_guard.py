import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dataset_sink.errors import IntegrityError
from dataset_sink.manifest import Manifest
from dataset_sink.materializer import Materializer
from dataset_sink.sources import LocalSourceReader
from dataset_sink.training_guard import validate_training_dataset


class TrainingGuardTests(unittest.TestCase):
    def test_validates_commit_manifest_and_paimon_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            data = b"sample"
            (source / "sample.bin").write_bytes(data)
            manifest_path = root / "manifest.jsonl"
            manifest_path.write_text(
                json.dumps(
                    {
                        "source_key": "sample.bin",
                        "target_path": "sample.bin",
                        "size_bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = Manifest.load(manifest_path)
            release = Materializer(root / "cpfs", LocalSourceReader(source)).materialize(
                dataset="robotics",
                repository="robotics-data",
                source_reference="v1",
                commit_id="abc123",
                manifest=manifest,
                paimon_snapshot_id="42",
            )

            result = validate_training_dataset(
                Path(release.release_path),
                expected_commit="abc123",
                expected_manifest_sha256=manifest.sha256,
                expected_paimon_snapshot_id="42",
                deep=True,
            )
            self.assertEqual(result["guard"], "PASSED")

            with self.assertRaises(IntegrityError):
                validate_training_dataset(Path(release.release_path), expected_commit="wrong")


if __name__ == "__main__":
    unittest.main()
