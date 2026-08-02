import hashlib
import tempfile
import unittest
from pathlib import Path

from dataset_sink.errors import ManifestError
from dataset_sink.manifest import Manifest


class ManifestTests(unittest.TestCase):
    def test_loads_valid_jsonl(self):
        raw = b'{"source_key":"raw/a.bin","target_path":"shards/a.bin","size_bytes":3}\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.jsonl"
            path.write_bytes(raw)
            manifest = Manifest.load(path)

        self.assertEqual(len(manifest.entries), 1)
        self.assertEqual(manifest.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest.declared_size_bytes, 3)

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.jsonl"
            path.write_text(
                '{"source_key":"raw/a.bin","target_path":"../escape.bin"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                Manifest.load(path)

    def test_rejects_duplicate_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.jsonl"
            path.write_text(
                '{"source_key":"a","target_path":"same"}\n'
                '{"source_key":"b","target_path":"same"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                Manifest.load(path)


if __name__ == "__main__":
    unittest.main()

