from __future__ import annotations

import json
import unittest

from dataset_sink.aliyun_cli import CommandResult
from dataset_sink.dataflow import CpfsDataFlow
from dataset_sink.errors import DatasetSinkError


class FakeAliyun:
    def __init__(self, dataflows=None, statuses=None, task_id="t-1"):
        self.dataflows = dataflows or []
        self.statuses = list(statuses or [])
        self.task_id = task_id
        self.calls = []

    def __call__(self, command):
        self.calls.append(list(command))
        if "DescribeDataFlows" in command:
            body = {"DataFlowInfo": {"DataFlow": self.dataflows}}
        elif "DescribeDataFlowTasks" in command:
            status = self.statuses.pop(0) if self.statuses else "Completed"
            # 结构照抄真实响应（2026-08-03 实测）。之前这里是凭空想的
            # DataFlowTaskInfo.DataFlowTask，假数据和代码一起错，测试全绿
            # 但对真实 API 直接失败——假数据必须来自真实抓包。
            body = {
                "TaskInfo": {
                    "Task": [
                        {
                            "TaskId": self.task_id,
                            "Status": status,
                            "TaskAction": "Import",
                            "DataFlowId": "df-0078f0819f8d93ab",
                            "DataType": "MetaAndData",
                            "FsPath": "/datasets/",
                            "Progress": 0,
                            "SourceStorage": "oss://bucket",
                        }
                    ]
                }
            }
        else:
            body = {"TaskId": self.task_id}
        return CommandResult(0, json.dumps(body), "")

    def last(self, flag):
        cmd = self.calls[-1]
        return cmd[cmd.index(flag) + 1] if flag in cmd else None


FLOWS = [{"DataFlowId": "df-1", "FileSystemPath": "/datasets"}]


class DataFlowTests(unittest.TestCase):
    def _df(self, fake, mount="/mnt/cpfs"):
        return CpfsDataFlow(
            filesystem_id="cpfs-0001baad3c95cb4a",
            region="cn-hangzhou",
            mount_prefix=mount,
            runner=fake,
        )

    def test_prefetch_submits_import(self):
        fake = FakeAliyun(FLOWS)
        task = self._df(fake).prefetch("/mnt/cpfs/datasets/robotics/c1")

        self.assertEqual(fake.last("--TaskAction"), "Import")
        # Directory 是**相对 DataFlow 的 FileSystemPath** 的路径，不是绝对的
        # 文件系统内部路径。FLOWS 里 df-1 的 FileSystemPath 是 /datasets/，
        # 所以 /datasets/robotics/c1 要发成 /robotics/c1/。
        #
        # 这条断言原来写的是绝对路径 /datasets/robotics/c1/，也就是把 bug 固化
        # 成了期望值。2026-08-03 在真实 CPFS 2.0 上对照实测才发现：
        #     Directory=/datasets/robotics/c1/  →  Failed，progress 0，无报错
        #     Directory=/robotics/c1/           →  Completed，progress 100
        self.assertEqual(fake.last("--Directory"), "/robotics/c1/")
        self.assertEqual(task.dataflow_id, "df-1")

    def test_directory_is_relative_to_the_dataflow_binding(self):
        """第三个坐标系：挂载视角 / 文件系统内部视角 / DataFlow 相对视角。

        传绝对路径不会报参数错误——任务被正常受理，几秒后变 Failed，
        ProgressStats 是空的、没有 ErrorMessage。纯静默失败，所以必须锁住。
        """
        for mount_path, expected in (
            ("/mnt/cpfs/datasets/robotics/c1", "/robotics/c1/"),
            ("/mnt/cpfs/datasets/robotics", "/robotics/"),
            ("/mnt/cpfs/datasets/a/b/c", "/a/b/c/"),
        ):
            fake = FakeAliyun(FLOWS)
            self._df(fake).prefetch(mount_path)
            self.assertEqual(fake.last("--Directory"), expected, mount_path)
            # 绝对文件系统路径绝不能出现在 Directory 里
            self.assertNotIn("/datasets/", fake.last("--Directory"))

    def test_flush_submits_export(self):
        fake = FakeAliyun(FLOWS)
        self._df(fake).flush("/mnt/cpfs/datasets/robotics/c1")
        self.assertEqual(fake.last("--TaskAction"), "Export")

    def test_mount_prefix_mismatch_is_refused(self):
        # 换算错会作用到别的目录上，宁可报错也不猜
        with self.assertRaises(DatasetSinkError):
            self._df(FakeAliyun(FLOWS)).prefetch("/elsewhere/robotics/c1")

    def test_sibling_prefix_is_not_covered(self):
        fake = FakeAliyun([{"DataFlowId": "df-x", "FileSystemPath": "/datasets-old"}])
        with self.assertRaises(DatasetSinkError):
            self._df(fake).prefetch("/mnt/cpfs/datasets/robotics/c1")

    def test_longest_matching_dataflow_wins(self):
        fake = FakeAliyun(
            [
                {"DataFlowId": "df-root", "FileSystemPath": "/"},
                {"DataFlowId": "df-deep", "FileSystemPath": "/datasets/robotics"},
            ]
        )
        self._df(fake).prefetch("/mnt/cpfs/datasets/robotics/c1")
        self.assertEqual(fake.last("--DataFlowId"), "df-deep")

    def test_wait_returns_on_completion(self):
        fake = FakeAliyun(FLOWS, statuses=["Running", "Running", "Completed"])
        df = self._df(fake)
        task = df.wait("t-1", sleep=lambda _: None, now=lambda: 0.0)
        self.assertTrue(task.succeeded)

    def test_wait_raises_on_failure(self):
        fake = FakeAliyun(FLOWS, statuses=["Failed"])
        with self.assertRaises(DatasetSinkError) as ctx:
            self._df(fake).wait("t-1", sleep=lambda _: None, now=lambda: 0.0)
        self.assertIn("Failed", str(ctx.exception))

    def test_timeout_is_an_error_not_a_silent_success(self):
        # 搬运没完成就去 certify，报错会发生在离原因很远的地方
        fake = FakeAliyun(FLOWS, statuses=["Running"] * 10)
        clock = iter([0.0, 0.0, 9999.0, 9999.0])
        with self.assertRaises(DatasetSinkError) as ctx:
            self._df(fake).wait(
                "t-1", timeout_seconds=1, sleep=lambda _: None, now=lambda: next(clock)
            )
        self.assertIn("没有结束", str(ctx.exception))

    def test_rejects_unknown_action(self):
        with self.assertRaises(ValueError):
            self._df(FakeAliyun(FLOWS)).submit(action="Nuke", directory="/datasets/x")


if __name__ == "__main__":
    unittest.main()


class ObjectUriMappingTests(unittest.TestCase):
    """DataFlow 把 FileSystemPath 和 SourceStoragePath 死绑在一起。

    路径 D 对应的对象前缀是 `S + (D - F)`，所以 OSS 布局必须镜像 CPFS 布局。
    算错这个映射的后果是 commit 从错误的前缀 import，建出来的 Commit 指向
    别的数据——而 manifest 校验要到 materialize 时才发现。
    """

    def _df(self, flows):
        return CpfsDataFlow(
            filesystem_id="cpfs-x",
            region="cn-hangzhou",
            mount_prefix="/mnt/cpfs",
            runner=FakeAliyun(flows),
        )

    def test_maps_subpath_onto_source_prefix(self):
        df = self._df(
            [
                {
                    "DataFlowId": "df-1",
                    "FileSystemPath": "/datasets",
                    "SourceStoragePath": "oss://bucket/releases",
                }
            ]
        )
        self.assertEqual(
            df.object_uri_for("/datasets/robotics/c1"),
            "oss://bucket/releases/robotics/c1/",
        )

    def test_binding_root_maps_to_source_root(self):
        df = self._df(
            [
                {
                    "DataFlowId": "df-1",
                    "FileSystemPath": "/datasets",
                    "SourceStoragePath": "oss://bucket/releases",
                }
            ]
        )
        self.assertEqual(df.object_uri_for("/datasets"), "oss://bucket/releases/")

    def test_falls_back_to_bucket_level_source_storage(self):
        # 建 DataFlow 时可以只给 SourceStorage（桶级），不给 SourceStoragePath
        df = self._df(
            [{"DataFlowId": "df-1", "FileSystemPath": "/", "SourceStorage": "oss://bucket"}]
        )
        self.assertEqual(
            df.object_uri_for("/datasets/robotics/c1"), "oss://bucket/datasets/robotics/c1/"
        )

    def test_trailing_slash_is_always_present(self):
        # lakeFS import 要求 URI 指向前缀，少了斜杠可能被当成单个对象
        df = self._df(
            [
                {
                    "DataFlowId": "df-1",
                    "FileSystemPath": "/datasets",
                    "SourceStoragePath": "oss://bucket/releases/",
                }
            ]
        )
        self.assertTrue(df.object_uri_for("/datasets/robotics/c1").endswith("/"))

    def test_uncovered_path_is_rejected(self):
        df = self._df(
            [{"DataFlowId": "df-1", "FileSystemPath": "/other", "SourceStorage": "oss://b"}]
        )
        with self.assertRaises(DatasetSinkError):
            df.object_uri_for("/datasets/robotics/c1")
