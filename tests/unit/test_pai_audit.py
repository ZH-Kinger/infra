import unittest
from pathlib import Path

from dataset_sink.aliyun_cli import CommandResult
from dataset_sink.pai_audit import AliyunPaiAuditReader, audit_workloads
from dataset_sink.registry import build_registry


def release(commit="abc123"):
    return {
        "SourceId": commit,
        "Uri": f"cpfs://cpfs.example/ptc/export/datasets/robotics/{commit}/",
        "Labels": [
            {"Key": "lakefs_commit", "Value": commit},
            {"Key": "manifest_sha256", "Value": "digest"},
        ],
    }


class PaiMountAuditTests(unittest.TestCase):
    def test_accepts_pinned_readonly_release(self):
        result = audit_workloads(
            dlc_jobs=[
                {
                    "JobId": "job-1",
                    "DisplayName": "train",
                    "DataSources": [
                        {
                            "DataSourceId": "d-1",
                            "DataSourceVersion": "v2",
                            "MountPath": "/mnt/dataset",
                            "MountAccess": "RO",
                        }
                    ],
                }
            ],
            resolve_version=lambda _dataset, _version: release(),
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compliant_mounts"], 1)

    def test_finds_workspace_direct_mount_and_rw(self):
        registry = build_registry(
            [{"name": "scratch", "bucket": "team", "prefix": "scratch", "mode": "workspace"}]
        )
        result = audit_workloads(
            dsw_instances=[
                {
                    "InstanceId": "dsw-1",
                    "InstanceName": "notebook",
                    "Datasets": [
                        {
                            "Uri": "oss://team.oss-cn-hangzhou.aliyuncs.com/scratch/alice/",
                            "MountPath": "/mnt/workspace-data",
                            "ActualMountAccess": "RW",
                        }
                    ],
                }
            ],
            resolve_version=lambda _dataset, _version: {},
            registry=registry,
        )
        self.assertEqual(result["violation_count"], 2)
        self.assertEqual(
            {item["code"] for item in result["findings"]},
            {"MOUNT_NOT_READ_ONLY", "WORKSPACE_MOUNT"},
        )

    def test_finds_unmanaged_dataset_version(self):
        result = audit_workloads(
            dsw_instances=[
                {
                    "InstanceId": "dsw-2",
                    "Datasets": [{"DatasetId": "d-2", "DatasetVersion": "v1", "MountAccess": "RO"}],
                }
            ],
            resolve_version=lambda _dataset, _version: {"SourceId": "something", "Labels": []},
        )
        self.assertEqual(result["findings"][0]["code"], "UNMANAGED_DATASET_VERSION")

    def test_finds_identity_and_path_mismatch(self):
        bad_identity = release("abc123")
        bad_identity["SourceId"] = "other"
        first = audit_workloads(
            dlc_jobs=[
                {
                    "JobId": "j",
                    "DataSources": [
                        {"DataSourceId": "d", "DataSourceVersion": "v", "MountAccess": "RO"}
                    ],
                }
            ],
            resolve_version=lambda _dataset, _version: bad_identity,
        )
        self.assertEqual(first["findings"][0]["code"], "RELEASE_IDENTITY_MISMATCH")

        bad_path = release("abc123")
        bad_path["Uri"] = "cpfs://cpfs.example/datasets/robotics/wrong/"
        second = audit_workloads(
            dlc_jobs=[
                {
                    "JobId": "j",
                    "DataSources": [
                        {"DataSourceId": "d", "DataSourceVersion": "v", "MountAccess": "RO"}
                    ],
                }
            ],
            resolve_version=lambda _dataset, _version: bad_path,
        )
        self.assertEqual(second["findings"][0]["code"], "RELEASE_PATH_MISMATCH")


class AliyunPaiAuditReaderTests(unittest.TestCase):
    def test_collects_detailed_jobs_and_instances_and_caches_versions(self):
        commands = []

        def runner(command):
            commands.append(list(command))
            if "ListJobs" in command:
                return CommandResult(0, '{"Jobs":[{"JobId":"job-1"}],"TotalCount":1}', "")
            if "GetJob" in command:
                return CommandResult(0, '{"JobId":"job-1","DataSources":[]}', "")
            if "ListInstances" in command:
                return CommandResult(0, '{"Instances":[{"InstanceId":"dsw-1"}],"TotalCount":1}', "")
            if "GetInstance" in command:
                return CommandResult(0, '{"InstanceId":"dsw-1","Datasets":[]}', "")
            if "GetDatasetVersion" in command:
                return CommandResult(0, '{"SourceId":"abc123"}', "")
            raise AssertionError(command)

        reader = AliyunPaiAuditReader(
            region="cn-hangzhou",
            workspace_id="ws-1",
            profile="audit",
            runner=runner,
        )
        jobs, instances = reader.collect()
        self.assertEqual(jobs[0]["JobId"], "job-1")
        self.assertEqual(instances[0]["InstanceId"], "dsw-1")
        reader.resolve_version("d-1", "v2")
        reader.resolve_version("d-1", "v2")

        version_calls = [c for c in commands if "GetDatasetVersion" in c]
        self.assertEqual(len(version_calls), 1)
        self.assertIn("aiworkspace.cn-hangzhou.aliyuncs.com", version_calls[0])
        self.assertTrue(all("--region" in command for command in commands))

    def test_pages_until_total_count(self):
        pages = []

        def runner(command):
            if "ListJobs" in command:
                page = command[command.index("--PageNumber") + 1]
                pages.append(page)
                job_id = f"job-{page}"
                return CommandResult(0, f'{{"Jobs":[{{"JobId":"{job_id}"}}],"TotalCount":2}}', "")
            if "GetJob" in command:
                job_id = command[command.index("--JobId") + 1]
                return CommandResult(0, f'{{"JobId":"{job_id}","DataSources":[]}}', "")
            raise AssertionError(command)

        reader = AliyunPaiAuditReader(region="cn-hangzhou", workspace_id="ws-1", runner=runner)
        jobs, _ = reader.collect("dlc")
        self.assertEqual(pages, ["1", "2"])
        self.assertEqual([item["JobId"] for item in jobs], ["job-1", "job-2"])


class AuditWorkflowContractTests(unittest.TestCase):
    def test_scheduled_audit_uses_dedicated_readonly_role(self):
        workflow = Path(".github/workflows/pai-mount-audit.yml").read_text(encoding="utf-8")
        self.assertIn("schedule:", workflow)
        self.assertIn("PAI_MOUNT_AUDIT_ROLE_ARN", workflow)
        self.assertIn("audit-pai-mounts", workflow)
        self.assertNotIn("DLC_SUBMIT_ROLE_ARN", workflow)


if __name__ == "__main__":
    unittest.main()
