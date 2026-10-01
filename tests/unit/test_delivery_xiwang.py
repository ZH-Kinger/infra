import unittest

from delivery.xiwang import Config, XiwangError, commands


class XiwangCommandTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "src": {"scheme": "oss", "bucket": "wuji-bucket-hangzhou", "prefix": "third-party-data/worldengine"},
            "dest": {"scheme": "oss", "bucket": "wuji-data-tran-sing", "prefix": "aliyun-hz/third-party-data/worldengine"},
        }
        self.config = Config()

    def test_pipeline_uses_atomic_marker_and_nas_target(self):
        got = commands(self.plan, self.config, "move-123")
        self.assertIn(".panel-done", got["sg"])
        self.assertIn("ossutil ls", got["xw"])
        self.assertIn("/mnt/data04/296834/Wuji-Algorithm@wuji.tech/data/third-party-data/worldengine", got["target"])
        self.assertIn("--checkpoint-dir", got["sg"])
        self.assertIn("--checkpoint-dir", got["xw"])
        self.assertNotIn("accessKeySecret", got["sg"] + got["xw"])

    def test_relay_prefix_cannot_escape_fixed_root(self):
        bad = {**self.plan, "dest": {**self.plan["dest"], "prefix": "other/x"}}
        with self.assertRaises(XiwangError):
            commands(bad, self.config, "move-123")


if __name__ == "__main__":
    unittest.main()
