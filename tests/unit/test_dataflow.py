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
            body = {
                "DataFlowTaskInfo": {
                    "DataFlowTask": [
                        {"TaskId": self.task_id, "Status": status, "TaskAction": "Import"}
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
        self.assertEqual(fake.last("--Directory"), "/datasets/robotics/c1/")
        self.assertEqual(task.dataflow_id, "df-1")

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
