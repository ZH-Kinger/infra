"""迁移能选哪些桶：从云上实时拉，不靠人手维护。

这份清单同时是**白名单**（`flows._validate_transfer` 拿它挡提交），所以这里排掉的桶
不是「页面上看不见」，而是「提交不了」。排错了的两个方向都难发现：
多排一个，人来问「为什么没有我的桶」；少排一个，有人能往镜像仓库里搬数据。
"""

import json
import tempfile
import unittest
from pathlib import Path

from delivery import transfer_catalog as tc

OSS = [
    {"name": "wuji-bucket-hangzhou", "region": "oss-cn-hangzhou"},
    {"name": "wuji-bangkok", "region": "oss-ap-southeast-7"},
    {"name": "cri-9j67oat8c3scne73-registry", "region": "oss-cn-hangzhou"},
    {"name": "oss-pai-w49z-ap-southeast-7", "region": "oss-ap-southeast-7"},
    {"name": "h2r-dlc-1704065796538912-cn-shanghai", "region": "oss-cn-shanghai"},
    {"name": "data-infra-emr-log-hgh", "region": "oss-cn-hangzhou"},
]
TOS = [
    {"name": "data-tran", "region": "cn-shanghai"},
    {"name": "umi-tos-raw", "region": "cn-guangzhou"},
    {"name": "las-datastore", "region": "cn-shanghai"},
    {"name": "ml-platform-auto-created-required-2111674479-cn-beijing", "region": "cn-beijing"},
]


class PickTests(unittest.TestCase):
    def test_the_oss_prefix_is_stripped_from_the_region(self):
        """ListBuckets 返回 `oss-cn-hangzhou`，引擎要裸地域自己拼域名。
        不归一会拼出 `oss-oss-cn-hangzhou.aliyuncs.com`。"""
        rows, _ = tc.pick(OSS, [])
        got = {r["name"]: r["region"] for r in rows}
        self.assertEqual(got["wuji-bucket-hangzhou"], "cn-hangzhou")

    def test_both_clouds_end_up_in_one_list(self):
        rows, _ = tc.pick(OSS, TOS)
        names = {r["name"] for r in rows}
        self.assertIn("wuji-bucket-hangzhou", names)
        self.assertIn("data-tran", names)

    def test_buckets_nobody_should_migrate_into_are_excluded_with_a_reason(self):
        """镜像仓库、平台自建的那些 —— 排掉的同时要说清楚为什么，
        不然一个桶悄悄从申请页消失比它一直在更让人困惑。"""
        _, dropped = tc.pick(OSS, TOS)
        out = dict(dropped)
        for name in (
            "cri-9j67oat8c3scne73-registry",
            "oss-pai-w49z-ap-southeast-7",
            "h2r-dlc-1704065796538912-cn-shanghai",
            "las-datastore",
            "ml-platform-auto-created-required-2111674479-cn-beijing",
            "data-infra-emr-log-hgh",
        ):
            self.assertIn(name, out, name)
            self.assertTrue(out[name], f"{name} 排掉了却没写理由")

    def test_a_name_that_exists_in_both_clouds_is_dropped_from_both(self):
        """清单只有 {name, region}，表达不出是哪朵云。收任何一个都会让
        「按名字查地域」静默取错 —— 数据搬到另一朵云的另一个地域，一路不报错。"""
        rows, dropped = tc.pick(
            [{"name": "wuji-ego-processed", "region": "oss-cn-hangzhou"}],
            [{"name": "wuji-ego-processed", "region": "cn-shanghai"}],
        )
        self.assertEqual(rows, [])
        self.assertIn("两朵云", dict(dropped)["wuji-ego-processed"])

    def test_the_same_bucket_listed_twice_is_not_a_collision(self):
        one = [{"name": "b", "region": "cn-hangzhou"}]
        rows, dropped = tc.pick(one, [{"name": "b", "region": "cn-hangzhou"}])
        self.assertEqual(rows, [{"name": "b", "region": "cn-hangzhou"}])
        self.assertEqual(dropped, [])

    def test_a_bucket_without_a_region_is_not_offered(self):
        """地域是空的话，引擎会拼出 `oss-.aliyuncs.com`，或者在错的地域建任务。"""
        rows, dropped = tc.pick([{"name": "b", "region": ""}], [])
        self.assertEqual(rows, [])
        self.assertIn("地域", dict(dropped)["b"])


class ApplyTests(unittest.TestCase):
    def _templates(self):
        return {
            "templates": [
                {
                    "id": "oss-move",
                    "kind": "transfer",
                    "buckets": [
                        {"name": "old-one", "region": "cn-hangzhou"},
                        {"name": "wuji-bangkok", "region": "ap-southeast-7"},
                    ],
                },
                {"id": "aliyun-ecs", "kind": "resource"},
            ]
        }

    def test_it_reports_what_changed(self):
        data = self._templates()
        added, removed = tc.apply_to(
            data,
            [
                {"name": "wuji-bangkok", "region": "ap-southeast-7"},
                {"name": "brand-new", "region": "cn-shanghai"},
            ],
        )
        self.assertEqual(added, ["brand-new"])
        self.assertEqual(removed, ["old-one"])

    def test_an_empty_result_does_not_wipe_the_list(self):
        """一个桶都没拉到多半是凭证过期或接口抖了。清空的后果是所有迁移申请
        都提交不了，而且页面上看不出为什么。"""
        data = self._templates()
        with self.assertRaises(ValueError):
            tc.apply_to(data, [])
        self.assertEqual(len(data["templates"][0]["buckets"]), 2)

    def test_other_templates_are_not_touched(self):
        data = self._templates()
        tc.apply_to(data, [{"name": "b", "region": "cn-hangzhou"}])
        self.assertEqual(data["templates"][1], {"id": "aliyun-ecs", "kind": "resource"})

    def test_a_missing_template_is_an_error_not_a_silent_skip(self):
        with self.assertRaises(KeyError):
            tc.apply_to({"templates": []}, [{"name": "b", "region": "r"}])


class LiveTests(unittest.TestCase):
    def test_one_cloud_being_down_does_not_void_the_other(self):
        """火山抖一下不该把阿里那边刚建的桶挡在申请页外面。"""
        self.addCleanup(setattr, tc, "list_tos", tc.list_tos)
        tc.list_tos = _boom
        rows, _dropped, problems, _counts = tc.live(
            aliyun_creds=None,
            volcano_creds=None,
            transport=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        self.assertEqual(len(problems), 2)  # 两朵都失败时两条都要报
        self.assertEqual(rows, [])


class PartialFailureTests(unittest.TestCase):
    """一朵云没拉到时**整个不写**。

    写的话那朵云的桶会从白名单里被悄悄删光 —— 申请页上突然少一半选项，
    提交已有路径被拒「不在可申请的范围里」，而日志里只有一行列桶失败。
    清单宁可旧一天，不能少一半。
    """

    def _run(self, tmpl_path) -> int:
        from delivery.cli import main

        return main(["requests", "buckets", "--templates", tmpl_path, "--apply"])

    def test_one_cloud_down_leaves_the_template_alone(self):
        import os

        with tempfile.TemporaryDirectory() as box:
            ident = Path(box) / "identity"
            ident.mkdir()
            path = ident / "request-templates.json"
            before = {
                "schema": "x",
                "templates": [
                    {
                        "id": "oss-move",
                        "kind": "transfer",
                        "buckets": [{"name": "keep-me", "region": "cn-shanghai"}],
                    }
                ],
            }
            path.write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")

            # 凭证在参数位置就被求值了，喂假的 —— 反正 live() 被换掉了，不会真调
            for key in (
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
                "VOLCANO_ACCESS_KEY",
                "VOLCANO_SECRET_KEY",
            ):
                self.addCleanup(os.environ.pop, key, None)
                os.environ[key] = "fake"
            self.addCleanup(setattr, tc, "live", tc.live)
            tc.live = lambda **kw: (
                [{"name": "only-aliyun", "region": "cn-hangzhou"}],
                [],
                ["火山 TOS 列桶失败：Access Denied"],
                (13, 0),
            )
            here = Path.cwd()
            os.chdir(box)
            try:
                rc = self._run("identity/request-templates.json")
            finally:
                os.chdir(here)
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), before)


class TosCredsTests(unittest.TestCase):
    """列桶用采集身份（panel-collector，只读）。**没配就抛错，不回落。**
    回落到 `TOS_ACCESS_KEY` 的话，这个功能会拿 bot 那把宽得多的钥匙照常工作 ——
    谁都不会发现最小权限那层已经没了。"""

    def test_the_collector_key_is_used(self):
        got = tc.tos_creds({"VOLCANO_ACCESS_KEY": "AK", "VOLCANO_SECRET_KEY": "SK"})
        self.assertEqual((got.access_key_id, got.secret_access_key), ("AK", "SK"))

    def test_the_broad_bot_key_is_not_silently_borrowed(self):
        """`clouds.volcano.Credentials.from_env` 会回落 `TOS_ACCESS_KEY`，
        这里刻意不用它 —— 那把是 bot 的运维钥匙，能读写对象、能改 IAM。"""
        for env in (
            {},
            {"TOS_ACCESS_KEY": "AK", "TOS_SECRET_KEY": "SK"},
            {"VOLCANO_ACCESS_KEY": "AK"},
        ):
            with self.assertRaises(RuntimeError, msg=str(env)):
                tc.tos_creds(env)


class NoChangeTests(unittest.TestCase):
    def test_an_unchanged_list_is_not_rewritten(self):
        """跑得勤的时候每轮重写，会让文件 mtime 永远是「刚刚」——
        而那是排查「清单什么时候变的」唯一能用的线索。"""
        import os

        with tempfile.TemporaryDirectory() as box:
            (Path(box) / "identity").mkdir()
            path = Path(box) / "identity" / "request-templates.json"
            same = [{"name": "b", "region": "cn-hangzhou"}]
            path.write_text(
                json.dumps(
                    {
                        "schema": "x",
                        "templates": [{"id": "oss-move", "kind": "transfer", "buckets": same}],
                    }
                ),
                encoding="utf-8",
            )
            before = path.stat().st_mtime_ns

            for key in (
                "ALIYUN_ACCESS_KEY_ID",
                "ALIYUN_ACCESS_KEY_SECRET",
                "VOLCANO_ACCESS_KEY",
                "VOLCANO_SECRET_KEY",
            ):
                self.addCleanup(os.environ.pop, key, None)
                os.environ[key] = "fake"
            self.addCleanup(setattr, tc, "live", tc.live)
            tc.live = lambda **kw: (list(same), [], [], (1, 0))

            from delivery.cli import main

            here = Path.cwd()
            os.chdir(box)
            try:
                rc = main(
                    [
                        "requests",
                        "buckets",
                        "--templates",
                        "identity/request-templates.json",
                        "--apply",
                    ]
                )
            finally:
                os.chdir(here)
            self.assertEqual(rc, 0)
            self.assertEqual(path.stat().st_mtime_ns, before, "没变化却重写了")


class SaveTests(unittest.TestCase):
    def test_the_write_is_atomic_and_the_file_stays_private(self):
        """写了一半的 JSON 会让整个申请页打不开。"""
        with tempfile.TemporaryDirectory() as box:
            path = Path(box) / "t.json"
            tc.save(str(path), {"templates": []})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"templates": []})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual([p.name for p in Path(box).iterdir()], ["t.json"])


class SummaryTests(unittest.TestCase):
    def test_no_change_still_says_something(self):
        """「什么都没打印」和「任务没跑」分不出来。"""
        self.assertIn("没变化", tc.summary([], [], [], 20))

    def test_the_per_cloud_counts_are_reported(self):
        """阿里的 ListBuckets 结果会按调用者权限过滤（实测两把 AK 看到的数目不同，
        差的那几个既不在清单也不在排除列表里）。把数目摆出来，人对着控制台才看得出少了。"""
        got = tc.summary([], [], [], 25, (13, 13))
        self.assertIn("阿里 13", got)
        self.assertIn("火山 13", got)


def _boom(*_a, **_k):
    raise RuntimeError("炸了")


if __name__ == "__main__":
    unittest.main()
