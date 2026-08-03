import unittest

from dataset_sink.aliyun_cli import CommandResult, register_pai_dataset_version
from dataset_sink.errors import ReleaseConflictError


def request():
    return {
        "dataset_id": "d-example",
        "body": {
            "Property": "DIRECTORY",
            "DataSourceType": "CPFS",
            "Uri": "cpfs://cpfs-example.cn-hangzhou/datasets/robotics/abc123/",
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

        result = register_pai_dataset_version(request(), region="cn-hangzhou", runner=runner)
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


class UriSchemeTests(unittest.TestCase):
    """Uri 的 scheme 必须与 DataSourceType 严格对应。

    2026-08-03 在真实 PAI 账号上逐条实测得出，**与官方文档不符**：ROS 文档说
    CPFS 用 `nas://<cpfs-fsid>.region/...`，实际只接受 `cpfs://`。按文档写会在
    流水线最后一步才炸，而那时 CPFS release 已经发布、Commit 和 Tag 都建好了。
    """

    def _request(self, source_type, uri):
        return {
            "dataset_id": "d-example",
            "body": {
                "Property": "DIRECTORY",
                "DataSourceType": source_type,
                "Uri": uri,
                "SourceId": "abc123",
                "ImportInfo": "{}",
            },
        }

    def _reject(self, source_type, uri):
        with self.assertRaises(ValueError) as ctx:
            register_pai_dataset_version(
                self._request(source_type, uri),
                region="cn-hangzhou",
                runner=lambda cmd: CommandResult(0, "{}", ""),
            )
        return str(ctx.exception)

    def test_accepts_matching_schemes(self):
        for source_type, uri in (
            ("CPFS", "cpfs://cpfs-0001baad3c95cb4a.cn-hangzhou/ptc-x/datasets/"),
            ("BMCPFS", "bmcpfs://cpfs-0001baad3c95cb4a.cn-hangzhou/ptc-x/"),
            ("NAS", "nas://0011abcdef.cn-hangzhou/datasets/"),
        ):
            with self.subTest(source_type):
                result = register_pai_dataset_version(
                    self._request(source_type, uri),
                    region="cn-hangzhou",
                    runner=lambda cmd: CommandResult(0, "{}", ""),
                )
                self.assertEqual(result["status"], "DRY_RUN")

    def test_rejects_nas_scheme_for_cpfs(self):
        # 这正是官方文档教的写法，也是我们自己代码里原来的写法
        message = self._reject("CPFS", "nas://cpfs-0001baad3c95cb4a.cn-hangzhou/ptc-x/")
        self.assertIn("cpfs://", message)

    def test_rejects_cpfs_scheme_for_nas(self):
        self.assertIn("nas://", self._reject("NAS", "cpfs://0011abcdef.cn-hangzhou/"))

    def test_rejects_directory_uri_without_trailing_slash(self):
        # PAI 在这种情况下报 "not DIRECTORY"
        message = self._reject("CPFS", "cpfs://cpfs-0001baad3c95cb4a.cn-hangzhou/ptc-x")
        self.assertIn("/ 结尾", message)
