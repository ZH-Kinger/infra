"""第三方数据源登记表。

这个文件里装着**别人的**凭证。所以测试的重点只有一个：
它们会不会跟着某个返回值跑到不该去的地方。
"""

import json
import tempfile
import unittest
from pathlib import Path

from delivery import sources
from delivery.errors import DeliveryError

GOOD = {
    "schema": "wuji-transfer-sources@1",
    "sources": {
        "vendor-a": {
            "label": "供应商甲",
            "endpoint": "oss-cn-shanghai.aliyuncs.com",
            "bucket": "their-bucket",
            "region": "cn-shanghai",
            "prefix": "to-wuji/",
            "access_key_id": "AK-SECRET-1",
            "access_key_secret": "SK-SECRET-1",
            "note": "交付完回收",
        }
    },
}


class LoadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "s.json"

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(self.path)

    def test_a_missing_file_means_nothing_registered_yet(self):
        """还没登记过第三方源是正常状态，不该报错。"""
        self.assertEqual(sources.load(str(self.path)), {})

    def test_a_broken_file_is_an_error_not_an_empty_registry(self):
        """把写坏的登记表当成「没有源」，表现是申请页上那些源突然消失，
        而没人知道为什么。"""
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(DeliveryError):
            sources.load(str(self.path))

    def test_a_missing_credential_is_refused_at_load(self):
        bad = json.loads(json.dumps(GOOD))
        del bad["sources"]["vendor-a"]["access_key_secret"]
        with self.assertRaises(DeliveryError):
            sources.load(self.write(bad))

    def test_an_endpoint_with_a_scheme_or_path_is_refused(self):
        """它会被拼进迁移服务的地址配置 —— 一个 http:// 或尾随路径
        就可能把数据发到别的地方。"""
        for bad in (
            "https://oss-cn-shanghai.aliyuncs.com",
            "oss-cn-shanghai.aliyuncs.com/evil",
            "oss-cn-shanghai.aliyuncs.com:8080/x",
        ):
            spec = json.loads(json.dumps(GOOD))
            spec["sources"]["vendor-a"]["endpoint"] = bad
            with self.assertRaises(DeliveryError, msg=bad):
                sources.load(self.write(spec))

    def test_a_prefix_that_could_escape_is_refused(self):
        for bad in ("../other/", "a//b/"):
            spec = json.loads(json.dumps(GOOD))
            spec["sources"]["vendor-a"]["prefix"] = bad
            with self.assertRaises(DeliveryError, msg=bad):
                sources.load(self.write(spec))

    def test_a_typo_in_a_field_name_is_refused_not_ignored(self):
        """静默忽略一个拼错的字段，表现是「配了但没生效」。"""
        spec = json.loads(json.dumps(GOOD))
        spec["sources"]["vendor-a"]["acess_key_id"] = "x"
        with self.assertRaises(DeliveryError):
            sources.load(self.write(spec))

    def test_a_trailing_slash_is_added_to_the_prefix(self):
        spec = json.loads(json.dumps(GOOD))
        spec["sources"]["vendor-a"]["prefix"] = "to-wuji"
        got = sources.load(self.write(spec))
        self.assertEqual(got["vendor-a"]["prefix"], "to-wuji/")


class ExposureTests(unittest.TestCase):
    """`/api/requests/options` 会把选项发给每一个打开申请页的人。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "s.json"
        path.write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")
        self.registry = sources.load(str(path))

    def test_the_options_carry_no_credential_at_all(self):
        blob = json.dumps(sources.options(self.registry), ensure_ascii=False)
        self.assertNotIn("AK-SECRET-1", blob)
        self.assertNotIn("SK-SECRET-1", blob)
        self.assertNotIn("access_key", blob)

    def test_the_options_still_say_enough_to_choose(self):
        row = sources.options(self.registry)[0]
        self.assertEqual(row["id"], "vendor-a")
        self.assertEqual(row["label"], "供应商甲")
        self.assertEqual(row["bucket"], "their-bucket")

    def test_resolve_is_the_only_way_to_the_credential(self):
        got = sources.resolve(self.registry, "vendor-a")
        self.assertEqual(got["access_key_id"], "AK-SECRET-1")

    def test_an_unregistered_source_raises_instead_of_returning_none(self):
        """一个 None 传下去会变成「源桶是空字符串」，
        而那种任务提上去之后的表现是「搬了 0 个对象，成功」。"""
        with self.assertRaises(DeliveryError):
            sources.resolve(self.registry, "never-registered")


class ConfineTests(unittest.TestCase):
    """登记表里的 prefix 是我们和对方约定的范围。不强制的话，
    `src://vendor-a/` 就是把对方整个桶拉回来 —— 而对方给这把 AK 时同意的不是这件事。"""

    ROW = {"prefix": "to-wuji/"}

    def test_the_asked_prefix_lands_under_the_registered_root(self):
        self.assertEqual(sources.confine(self.ROW, "batch-3/"), "to-wuji/batch-3/")

    def test_an_empty_prefix_means_the_whole_registered_root(self):
        self.assertEqual(sources.confine(self.ROW, ""), "to-wuji/")

    def test_a_directory_slash_is_added_so_a_name_does_not_match_its_neighbours(self):
        """少了这条，前缀 `team` 会连 `team-secret/` 一起匹配进来。"""
        self.assertEqual(sources.confine(self.ROW, "team"), "to-wuji/team/")

    def test_climbing_out_is_refused(self):
        for bad in ("../", "a/../../", "a//b/"):
            with self.assertRaises(DeliveryError, msg=bad):
                sources.confine(self.ROW, bad)

    def test_a_leading_slash_does_not_escape_the_root(self):
        self.assertTrue(sources.confine(self.ROW, "/batch-3/").startswith("to-wuji/"))

    def test_a_source_without_a_root_is_not_confined(self):
        self.assertEqual(sources.confine({"prefix": ""}, "x/"), "x/")


if __name__ == "__main__":
    unittest.main()
