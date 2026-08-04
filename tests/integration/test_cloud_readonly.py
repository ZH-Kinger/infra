"""真实阿里云环境的只读契约测试。

默认全部 skip，避免 `make test` 意外连接云端。只有显式提供对应环境变量时才运行；
凭证始终来自 aliyun CLI 默认凭证链或 profile，不通过测试变量传入。
"""

from __future__ import annotations

import os
import unittest

from dataset_sink.dataflow import CpfsDataFlow
from dataset_sink.pai_audit import AliyunPaiAuditReader, audit_workloads


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise unittest.SkipTest(f"需要显式设置 {name} 才运行真实云只读测试")
    return value


class PaiReadonlyIntegrationTests(unittest.TestCase):
    def test_real_workspace_mount_metadata_matches_reader_contract(self):
        region = _required("INTEGRATION_ALIYUN_REGION")
        workspace_id = _required("INTEGRATION_PAI_WORKSPACE_ID")
        reader = AliyunPaiAuditReader(
            region=region,
            workspace_id=workspace_id,
            profile=os.getenv("INTEGRATION_ALIYUN_PROFILE"),
        )

        jobs, instances = reader.collect()
        report = audit_workloads(
            dlc_jobs=jobs,
            dsw_instances=instances,
            resolve_version=reader.resolve_version,
        )

        self.assertIn(report["status"], {"PASS", "VIOLATIONS_FOUND"})
        self.assertEqual(report["workloads_checked"], len(jobs) + len(instances))


class CpfsReadonlyIntegrationTests(unittest.TestCase):
    def test_real_dataflow_metadata_matches_reader_contract(self):
        region = _required("INTEGRATION_ALIYUN_REGION")
        filesystem_id = _required("INTEGRATION_CPFS_FILESYSTEM_ID")
        client = CpfsDataFlow(
            filesystem_id=filesystem_id,
            region=region,
            profile=os.getenv("INTEGRATION_ALIYUN_PROFILE"),
        )

        flows = client.list_dataflows(refresh=True)

        self.assertIsInstance(flows, list)
        for flow in flows:
            self.assertTrue(flow.get("DataFlowId"))
            self.assertTrue(str(flow.get("FileSystemPath") or "").startswith("/"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
