import tempfile
import unittest
from pathlib import Path

from delivery.xiwang_sync import FAILED, PULLING, RELAYED, RELAYING, VERIFIED, Manifest, SyncError


class SyncManifestTests(unittest.TestCase):
    def test_relayed_objects_are_ready_while_other_objects_continue(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(str(Path(d) / "manifest.json"))
            m.seed([{"key": "a", "size": 1}, {"key": "b", "size": 2}])
            m.mark("a", RELAYED)
            self.assertEqual([x.key for x in m.ready_for_pull()], ["a"])
            self.assertFalse(m.done())

    def test_pull_is_verified_independently(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(str(Path(d) / "manifest.json"))
            m.seed([{"key": "a", "size": 1}])
            m.mark("a", RELAYED)
            m.mark("a", PULLING)
            m.mark("a", VERIFIED)
            self.assertTrue(m.done())

    def test_changed_object_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(str(Path(d) / "manifest.json"))
            m.seed([{"key": "a", "size": 1, "etag": "x"}])
            with self.assertRaises(SyncError):
                m.seed([{"key": "a", "size": 2, "etag": "x"}])

    def test_invalid_transition_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(str(Path(d) / "manifest.json"))
            m.seed([{"key": "a", "size": 1}])
            with self.assertRaises(SyncError):
                m.mark("a", VERIFIED)


if __name__ == "__main__":
    unittest.main()
