from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dataset_sink.cli import build_parser
from dataset_sink.runtime import RuntimeRequestError, build_runtime_request, load_runtime_config

DIGEST = "a" * 64
COMMIT = "b" * 64


def config():
    return {
        "platform": {
            "workspace_id": "ws-1",
            "vpc_id": "vpc-1",
            "vswitch_id": "vsw-1",
            "security_group_id": "sg-1",
            "extended_cidrs": ["10.0.0.0/8"],
            "default_route": "eth1",
            "allowed_image_registries": ["registry.cn-hangzhou.aliyuncs.com/team"],
            "require_image_digest": True,
            "workspace_uri_template": "cpfs://fs/ptc/exp/users/{actor}/",
            "output_uri_template": "cpfs://fs/ptc/exp/output/{actor}/{run_id}/",
        },
        "users": {"ZH-Kinger": {"ram_user_id": "204400000000000001"}},
        "datasets": {
            "robotics": {"dataset_id": "d-1", "mount_path": "/mnt/dataset"},
        },
        "image_profiles": {
            "pytorch-2.6": {
                "image": f"registry.cn-hangzhou.aliyuncs.com/team/train@sha256:{DIGEST}",
                "runtimes": ["dsw", "dlc"],
            }
        },
        "compute_profiles": {
            "gpu-dev": {
                "runtime": "dsw",
                "ecs_spec": "ecs.gn7i",
                "resource_id": "dsw-r-1",
                "ttl_hours": 8,
            },
            "gpu-training": {
                "runtime": "dlc",
                "ecs_spec": "ecs.gn7i",
                "resource_id": "dlc-r-1",
                "ttl_hours": 24,
                "pod_count": 1,
                "default_command": "python train.py",
            },
        },
    }


class RuntimeRequestTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 6, 1, 2, 3, tzinfo=timezone.utc)

    def _build(self, runtime, compute, **overrides):
        values = {
            "runtime": runtime,
            "dataset": "robotics",
            "commit_id": COMMIT,
            "image_profile": "pytorch-2.6",
            "compute_profile": compute,
            "actor": "ZH-Kinger",
            "run_id": "12345",
            "now": self.now,
        }
        values.update(overrides)
        return build_runtime_request(config(), **values)

    def test_dsw_uses_private_network_readonly_release_and_private_workspace(self):
        result = self._build("dsw", "gpu-dev")
        request = result.request
        self.assertEqual(request["Accessibility"], "PRIVATE")
        self.assertEqual(request["UserId"], "204400000000000001")
        self.assertEqual(request["Datasets"][0]["DataSourceVersion"], COMMIT)
        self.assertEqual(request["Datasets"][0]["MountAccess"], "RO")
        self.assertEqual(request["Datasets"][1]["MountAccess"], "RW")
        self.assertEqual(request["Datasets"][1]["Uri"], "cpfs://fs/ptc/exp/users/zh-kinger/")
        self.assertEqual(request["WorkspaceSource"], "/mnt/workspace")
        labels = {item["Key"]: item["Value"] for item in request["Labels"]}
        self.assertEqual(labels["expires_at"], "2026-08-06T09:02:03Z")
        self.assertEqual(request["UserVpc"]["VSwitchId"], "vsw-1")
        self.assertNotIn("SwitchId", request["UserVpc"])
        self.assertEqual(request["UserVpc"]["DefaultRoute"], "eth1")
        self.assertEqual(result.expires_at, "2026-08-06T09:02:03Z")

    def test_dlc_uses_readonly_release_separate_output_and_runtime_limit(self):
        result = self._build("dlc", "gpu-training", command="python custom.py")
        request = result.request
        self.assertEqual(request["UserCommand"], "/workspace/deploy/pai/training-entrypoint.sh")
        self.assertEqual(request["JobMaxRunningTimeMinutes"], 1440)
        self.assertEqual(request["UserVpc"]["SwitchId"], "vsw-1")
        self.assertNotIn("VSwitchId", request["UserVpc"])
        self.assertEqual(request["DataSources"][0]["MountAccess"], "RO")
        self.assertEqual(request["DataSources"][1]["MountAccess"], "RW")
        self.assertEqual(
            request["DataSources"][1]["Uri"],
            "cpfs://fs/ptc/exp/output/zh-kinger/12345/",
        )
        self.assertTrue(request["JobSpecs"][0]["Image"].endswith(DIGEST))
        envs = {item["Key"]: item["Value"] for item in request["CustomEnvs"]}
        self.assertEqual(envs["TRAINING_COMMAND"], "python custom.py")

    def test_mutable_or_non_commit_version_is_rejected(self):
        for version in ("latest", "main", "robotics-v1", "ABCDEF0123"):
            with self.subTest(version=version), self.assertRaises(RuntimeRequestError):
                self._build("dsw", "gpu-dev", commit_id=version)

    def test_tagged_or_unapproved_image_is_rejected(self):
        for image in (
            "registry.cn-hangzhou.aliyuncs.com/team/train:latest",
            f"docker.io/team/train@sha256:{DIGEST}",
        ):
            document = config()
            document["image_profiles"]["pytorch-2.6"]["image"] = image
            with self.subTest(image=image), self.assertRaises(RuntimeRequestError):
                build_runtime_request(
                    document,
                    runtime="dsw",
                    dataset="robotics",
                    commit_id=COMMIT,
                    image_profile="pytorch-2.6",
                    compute_profile="gpu-dev",
                    actor="user",
                    run_id="1",
                )

    def test_compute_profile_cannot_cross_runtime(self):
        with self.assertRaisesRegex(RuntimeRequestError, "not a dsw profile"):
            self._build("dsw", "gpu-training")

    def test_environment_placeholders_are_required_and_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            path.write_text(json.dumps({"value": "${REQUIRED_VALUE}"}), encoding="utf-8")
            self.assertEqual(
                load_runtime_config(path, environment={"REQUIRED_VALUE": "resolved"}),
                {"value": "resolved"},
            )
            with self.assertRaisesRegex(RuntimeRequestError, "REQUIRED_VALUE"):
                load_runtime_config(path, environment={})

    def test_dataset_id_catalog_expands_into_runtime_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = config()
            document["datasets"] = "${PAI_DATASET_IDS_JSON}"
            path = Path(tmp) / "profiles.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_runtime_config(
                path,
                environment={"PAI_DATASET_IDS_JSON": '{"robotics":"d-existing"}'},
            )
            self.assertEqual(
                loaded["datasets"]["robotics"],
                {"dataset_id": "d-existing", "mount_path": "/mnt/dataset"},
            )

    def test_cli_command_option_does_not_overwrite_subcommand_dispatch(self):
        args = build_parser().parse_args(
            [
                "runtime-request",
                "--config",
                "profiles.json",
                "--runtime",
                "dlc",
                "--dataset",
                "robotics",
                "--commit",
                COMMIT,
                "--image-profile",
                "pytorch-2.6",
                "--compute-profile",
                "gpu-training",
                "--actor",
                "ZH-Kinger",
                "--run-id",
                "123",
                "--command",
                "python train.py",
            ]
        )
        self.assertEqual(args.command, "runtime-request")
        self.assertEqual(args.training_command, "python train.py")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
