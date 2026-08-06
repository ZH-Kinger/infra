from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import DatasetSinkError


class RuntimeRequestError(DatasetSinkError):
    """A DSW/DLC runtime request violates a platform policy."""


_ENV = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_COMMIT = re.compile(r"^[0-9a-f]{10,64}$")
_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
_SAFE_PART = re.compile(r"[^a-z0-9_-]+")
_MUTABLE_REFS = {"latest", "main", "master", "dev", "develop", "head"}


@dataclass(frozen=True)
class RuntimeEnvelope:
    runtime: str
    expires_at: str
    request: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "runtime": self.runtime,
            "expires_at": self.expires_at,
            "request": self.request,
        }


def _expand_environment(value: Any, environment: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _expand_environment(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_environment(item, environment) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = environment.get(name)
        if not replacement:
            raise RuntimeRequestError(
                f"runtime profile requires non-empty environment variable {name}"
            )
        return replacement

    return _ENV.sub(replace, value)


def load_runtime_config(
    path: Path,
    *,
    environment: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeRequestError(f"cannot load runtime profile {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeRequestError("runtime profile root must be a JSON object")
    expanded = _expand_environment(document, environment or os.environ)
    datasets = expanded.get("datasets")
    if isinstance(datasets, str):
        try:
            dataset_ids = json.loads(datasets)
        except json.JSONDecodeError as exc:
            raise RuntimeRequestError(f"PAI dataset catalogue is not valid JSON: {exc}") from exc
        if not isinstance(dataset_ids, dict):
            raise RuntimeRequestError("PAI dataset catalogue must be a JSON object")
        expanded["datasets"] = {
            str(name): {"dataset_id": dataset_id, "mount_path": "/mnt/dataset"}
            for name, dataset_id in dataset_ids.items()
            if isinstance(dataset_id, str) and dataset_id.strip()
        }
    return expanded


def _mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise RuntimeRequestError(f"runtime profile requires object {key}")
    return value


def _text(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeRequestError(f"runtime profile requires non-empty string {key}")
    return value.strip()


def _profile(profiles: Mapping[str, Any], name: str, kind: str) -> Mapping[str, Any]:
    value = profiles.get(name)
    if not isinstance(value, dict):
        choices = ", ".join(sorted(profiles)) or "<none>"
        raise RuntimeRequestError(f"unknown {kind} profile {name!r}; allowed: {choices}")
    return value


def _slug(value: str, *, fallback: str) -> str:
    slug = _SAFE_PART.sub("-", value.strip().lower()).strip("-_")
    return slug or fallback


def _render_uri(template: str, *, actor: str, run_id: str) -> str:
    try:
        uri = template.format(actor=actor, run_id=run_id)
    except (KeyError, ValueError) as exc:
        raise RuntimeRequestError(f"invalid mount URI template: {exc}") from exc
    if not uri.startswith(("cpfs://", "bmcpfs://", "nas://", "oss://")):
        raise RuntimeRequestError("workspace/output URI must use cpfs, bmcpfs, nas, or oss")
    return uri.rstrip("/") + "/"


def _cidrs(platform: Mapping[str, Any]) -> list:
    value = platform.get("extended_cidrs")
    if isinstance(value, str):
        result = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        result = [item.strip() for item in value if item.strip()]
    else:
        result = []
    if not result:
        raise RuntimeRequestError(
            "platform.extended_cidrs is required when a vSwitch is fixed by policy"
        )
    return result


def _validate_commit(commit_id: str) -> None:
    normalized = commit_id.lower()
    if normalized in _MUTABLE_REFS or not _COMMIT.fullmatch(commit_id):
        raise RuntimeRequestError(
            "dataset version must be an immutable lakeFS commit hash (10-64 lowercase hex chars)"
        )


def _validate_image(image: str, platform: Mapping[str, Any]) -> None:
    allowed = platform.get("allowed_image_registries")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(x, str) for x in allowed):
        raise RuntimeRequestError(
            "platform.allowed_image_registries must be a non-empty string list"
        )
    if not any(image.startswith(prefix.rstrip("/") + "/") for prefix in allowed):
        raise RuntimeRequestError("image is outside the approved ACR registries")
    if platform.get("require_image_digest", True) and not _IMAGE_DIGEST.search(image):
        raise RuntimeRequestError(
            "image must be pinned by @sha256 digest; tags and latest are rejected"
        )


def build_runtime_request(
    config: Mapping[str, Any],
    *,
    runtime: str,
    dataset: str,
    commit_id: str,
    image_profile: str,
    compute_profile: str,
    actor: str,
    run_id: str,
    command: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RuntimeEnvelope:
    if runtime not in {"dsw", "dlc"}:
        raise RuntimeRequestError("runtime must be dsw or dlc")
    _validate_commit(commit_id)

    platform = _mapping(config, "platform")
    user_config = _profile(_mapping(config, "users"), actor, "user")
    dataset_config = _profile(_mapping(config, "datasets"), dataset, "dataset")
    image_config = _profile(_mapping(config, "image_profiles"), image_profile, "image")
    compute = _profile(_mapping(config, "compute_profiles"), compute_profile, "compute")

    supported = image_config.get("runtimes")
    if not isinstance(supported, list) or runtime not in supported:
        raise RuntimeRequestError(f"image profile {image_profile!r} is not approved for {runtime}")
    if compute.get("runtime") != runtime:
        raise RuntimeRequestError(f"compute profile {compute_profile!r} is not a {runtime} profile")

    image = _text(image_config, "image")
    _validate_image(image, platform)
    actor_slug = _slug(actor, fallback="unknown")
    run_slug = _slug(run_id, fallback="manual")
    workspace_id = _text(platform, "workspace_id")
    vpc_id = _text(platform, "vpc_id")
    vswitch_id = _text(platform, "vswitch_id")
    security_group_id = _text(platform, "security_group_id")
    extended_cidrs = _cidrs(platform)
    default_route = platform.get("default_route", "eth1")
    if default_route != "eth1":
        raise RuntimeRequestError(
            "public default route is disabled; platform.default_route must be eth1"
        )
    dlc_user_vpc = {
        "VpcId": vpc_id,
        "SwitchId": vswitch_id,
        "SecurityGroupId": security_group_id,
        "ExtendedCIDRs": extended_cidrs,
        "DefaultRoute": default_route,
    }
    dataset_mount = {
        "DataSourceId": _text(dataset_config, "dataset_id"),
        "DataSourceVersion": commit_id,
        "MountPath": dataset_config.get("mount_path", "/mnt/dataset"),
        "MountAccess": "RO",
    }

    ttl_hours = compute.get("ttl_hours")
    if not isinstance(ttl_hours, int) or not 1 <= ttl_hours <= 168:
        raise RuntimeRequestError("compute profile ttl_hours must be an integer from 1 to 168")
    started = now or datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    expires_at = (started + timedelta(hours=ttl_hours)).astimezone(timezone.utc)
    expires_text = expires_at.isoformat().replace("+00:00", "Z")

    common_env = {
        "DATASET_ROOT": dataset_mount["MountPath"],
        "DATASET_COMMIT": commit_id,
        "DATASET_NAME": dataset,
        "REQUESTED_BY": actor_slug,
        "RUNTIME_EXPIRES_AT": expires_text,
    }

    if runtime == "dsw":
        workspace_uri = _render_uri(
            _text(platform, "workspace_uri_template"), actor=actor_slug, run_id=run_slug
        )
        name = f"dsw_{actor_slug}_{run_slug}".replace("-", "_")[:27].rstrip("_")
        request = {
            "WorkspaceId": workspace_id,
            "InstanceName": name,
            "EcsSpec": _text(compute, "ecs_spec"),
            "ImageUrl": image,
            "ResourceId": _text(compute, "resource_id"),
            "Accessibility": "PRIVATE",
            # CI 代用户创建时必须显式转交所有权；否则 PRIVATE 实例归 CI 角色，
            # 真实用户既看不到也进不去。映射来自受评审配置，不是用户输入。
            "UserId": _text(user_config, "ram_user_id"),
            "Datasets": [
                dataset_mount,
                {
                    "Uri": workspace_uri,
                    "MountPath": "/mnt/workspace",
                    "MountAccess": "RW",
                },
            ],
            "WorkspaceSource": "/mnt/workspace",
            "EnvironmentVariables": common_env,
            "Labels": [
                {"Key": "requested_by", "Value": actor_slug},
                {"Key": "dataset_commit", "Value": commit_id},
                {"Key": "expires_at", "Value": expires_text},
            ],
            "UserVpc": {
                "VpcId": vpc_id,
                "VSwitchId": vswitch_id,
                "SecurityGroupId": security_group_id,
                "ExtendedCIDRs": extended_cidrs,
                "DefaultRoute": default_route,
            },
        }
    else:
        selected_command = (command or compute.get("default_command") or "").strip()
        if not selected_command:
            raise RuntimeRequestError("dlc requires --command or compute profile default_command")
        if "\n" in selected_command or "\r" in selected_command or len(selected_command) > 2048:
            raise RuntimeRequestError("DLC command must be a single line no longer than 2048 chars")
        output_uri = _render_uri(
            _text(platform, "output_uri_template"), actor=actor_slug, run_id=run_slug
        )
        pod_count = compute.get("pod_count", 1)
        if not isinstance(pod_count, int) or not 1 <= pod_count <= 64:
            raise RuntimeRequestError("compute profile pod_count must be an integer from 1 to 64")
        name = f"dlc-{_slug(dataset, fallback='dataset')}-{commit_id[:10]}-{run_slug}"[:256]
        custom_envs = [
            {"Key": key, "Value": value, "Visible": "PUBLIC"}
            for key, value in sorted(common_env.items())
        ]
        custom_envs.extend(
            [
                {"Key": "OUTPUT_ROOT", "Value": "/mnt/output", "Visible": "PUBLIC"},
                {"Key": "TRAINING_COMMAND", "Value": selected_command, "Visible": "PUBLIC"},
            ]
        )
        request = {
            "DisplayName": name,
            "JobType": compute.get("job_type", "PyTorchJob"),
            "WorkspaceId": workspace_id,
            "ResourceId": _text(compute, "resource_id"),
            "Accessibility": "PRIVATE",
            # 用户命令不能绕过门禁：PAI 始终先启动受控入口，校验通过后入口才
            # 执行 TRAINING_COMMAND。用户本来就拥有容器内代码执行能力，命令
            # 本身不是安全边界；不可绕过的只读挂载和 guard 才是。
            "UserCommand": platform.get(
                "training_entrypoint", "/workspace/deploy/pai/training-entrypoint.sh"
            ),
            "JobMaxRunningTimeMinutes": ttl_hours * 60,
            "JobSpecs": [
                {
                    "Type": "Worker",
                    "Image": image,
                    "PodCount": pod_count,
                    "EcsSpec": _text(compute, "ecs_spec"),
                    "RestartPolicy": "Never",
                }
            ],
            "DataSources": [
                dataset_mount,
                {
                    "Uri": output_uri,
                    "MountPath": "/mnt/output",
                    "MountAccess": "RW",
                },
            ],
            "CustomEnvs": custom_envs,
            "UserVpc": dlc_user_vpc,
        }

    return RuntimeEnvelope(runtime=runtime, expires_at=expires_text, request=request)
