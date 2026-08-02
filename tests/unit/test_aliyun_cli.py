import unittest

from dataset_sink.aliyun_cli import CommandResult, register_pai_dataset_version
from dataset_sink.errors import ReleaseConflictError


def request():
    return {
        "dataset_id": "d-example",
        "body": {
            "Property": "DIRECTORY",
            "DataSourceType": "CPFS",
            "Uri": "nas://cpfs-example.cn-hangzhou/datasets/robotics/abc123/",
            "SourceType": "USER",
            "SourceId": "abc123",
            "ImportInfo": "{}",
            "Labels": [{"Key": "manifest_sha256", "Value": "digest-1"}],
        },
    }


class AliyunCliTests(unittest.TestCase):
    def test_dry_run_never_lists_or_executes(self):
        commands = []

        def runner(command):
            commands.append(list(command))
            return CommandResult(0, '{"RequestId":"dry-run"}', "")

        result = register_pai_dataset_version(
            request(), region="cn-hangzhou", runner=runner
        )
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(len(commands), 1)
        self.assertIn("--dryrun", commands[0])

    def test_execute_is_idempotent_by_commit(self):
        commands = []

        def runner(command):
            commands.append(list(command))
            if "ListDatasetVersions" in command:
                return CommandResult(
                    0,
                    '{"DatasetVersions":[{"VersionName":"v3","SourceId":"abc123",'
                    '"Labels":[{"Key":"manifest_sha256","Value":"digest-1"}]}]}',
                    "",
                )
            raise AssertionError("create must not run for an existing commit")

        result = register_pai_dataset_version(
            request(), region="cn-hangzhou", execute=True, runner=runner
        )
        self.assertEqual(result["status"], "EXISTS")
        self.assertEqual(result["version_name"], "v3")
        self.assertEqual(len(commands), 1)

    def test_rejects_same_commit_with_different_manifest(self):
        def runner(command):
            return CommandResult(
                0,
                '{"DatasetVersions":[{"VersionName":"v3","SourceId":"abc123",'
                '"Labels":[{"Key":"manifest_sha256","Value":"other"}]}]}',
                "",
            )

        with self.assertRaises(ReleaseConflictError):
            register_pai_dataset_version(
                request(), region="cn-hangzhou", execute=True, runner=runner
            )


if __name__ == "__main__":
    unittest.main()

