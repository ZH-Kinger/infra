from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dataset_sink.aliyun_cli import CommandResult
from dataset_sink.cli import build_parser
from dataset_sink.dataflow import CpfsDataFlow

FLOWS = [
    {
        "DataFlowId": "df-1",
        "FileSystemPath": "/datasets",
        "SourceStoragePath": "oss://bucket/releases",
    }
]


class FakeAliyun:
    def __init__(self, task_id="t-1"):
        self.task_id = task_id
        self.calls = []

    def __call__(self, command):
        self.calls.append(list(command))
        if "DescribeDataFlows" in command:
            body = {"DataFlowInfo": {"DataFlow": FLOWS}}
        elif "DescribeDataFlowTasks" in command:
            body = {
                "TaskInfo": {
                    "Task": [
                        {
                            "TaskId": self.task_id,
                            "Status": "Completed",
                            "TaskAction": "Export",
                            "DataFlowId": "df-1",
                        }
                    ]
                }
            }
        else:
            body = {"TaskId": self.task_id}
        return CommandResult(0, json.dumps(body), "")

    def actions(self):
        return [c[c.index("--TaskAction") + 1] for c in self.calls if "--TaskAction" in c]


class ArgValidationTests(unittest.TestCase):
    """--via dataflow 缺参数必须在提交任何任务之前失败。"""

    def _args(self, argv):
        return build_parser().parse_args(argv)

    def test_archive_requires_cpfs_arguments(self):
        from dataset_sink.cli import _archive

        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.bin").write_text("x", encoding="utf-8")
        manifest = tmp / "m.jsonl"
        manifest.write_text(
            json.dumps({"source_key": "a.bin", "target_path": "a.bin", "size_bytes": 1}) + "\n",
            encoding="utf-8",
        )
        args = self._args(
            ["archive", str(tmp), "--manifest", str(manifest), "--prefix", "p", "--via", "dataflow"]
        )
        with self.assertRaises(ValueError) as ctx:
            _archive(args)
        for flag in ("--cpfs-filesystem-id", "--cpfs-mount-prefix", "--region"):
            self.assertIn(flag, str(ctx.exception))

    def test_client_path_is_still_the_default(self):
        args = self._args(["archive", "/tmp/x", "--manifest", "/tmp/m", "--prefix", "p"])
        self.assertEqual(args.via, "client")
        args = self._args(
            [
                "materialize",
                "--dataset",
                "d",
                "--repository",
                "r",
                "--commit",
                "c",
                "--manifest",
                "/tmp/m",
                "--target-root",
                "/tmp/t",
            ]
        )
        self.assertEqual(args.via, "client")


class FlushDestinationTests(unittest.TestCase):
    """沉淀的目标前缀由 DataFlow 绑定推导，不由调用方指定。"""

    def _df(self, fake):
        return CpfsDataFlow(
            filesystem_id="cpfs-x",
            region="cn-hangzhou",
            mount_prefix="/mnt/cpfs",
            runner=fake,
        )

    def test_export_destination_comes_from_the_binding(self):
        fake = FakeAliyun()
        df = self._df(fake)
        inner = df.filesystem_path("/mnt/cpfs/datasets/robotics/c1")
        # 这个 URI 就是随后要喂给 commit --object-store-uri 的值
        self.assertEqual(df.object_uri_for(inner), "oss://bucket/releases/robotics/c1/")

        df.flush("/mnt/cpfs/datasets/robotics/c1")
        self.assertEqual(fake.actions(), ["Export"])

    def test_prefetch_and_export_use_opposite_actions(self):
        fake = FakeAliyun()
        df = self._df(fake)
        df.prefetch("/mnt/cpfs/datasets/robotics/c1")
        df.flush("/mnt/cpfs/datasets/robotics/c1")
        self.assertEqual(fake.actions(), ["Import", "Export"])


if __name__ == "__main__":
    unittest.main()
