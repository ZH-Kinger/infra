from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from .errors import DatasetSinkError, ReleaseConflictError


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]


def register_pai_dataset_version(
    request: dict,
    *,
    region: str,
    profile: Optional[str] = None,
    execute: bool = False,
    cli_path: str = "aliyun",
    runner: Optional[Runner] = None,
) -> dict:
    """Register a PAI dataset version through Alibaba Cloud CLI.

    Dry-run is the default. On execute, existing versions are checked by
    lakeFS commit before a new version is created.
    """
    _validate_request(request)
    if runner is None:
        if shutil.which(cli_path) is None:
            raise DatasetSinkError(f"Alibaba Cloud CLI was not found: {cli_path}")
        runner = _subprocess_runner

    dataset_id = request["dataset_id"]
    body = request["body"]
    base = [cli_path, "--region", region]
    if profile:
        base.extend(["--profile", profile])

    if execute:
        existing = _find_existing_version(
            base=base,
            dataset_id=dataset_id,
            commit_id=body["SourceId"],
            manifest_sha256=_label_value(body.get("Labels", []), "manifest_sha256"),
            runner=runner,
        )
        if existing is not None:
            return {
                "status": "EXISTS",
                "dataset_id": dataset_id,
                "version_name": existing.get("VersionName"),
                "lakefs_commit": body["SourceId"],
                "response": existing,
            }

    command = base + [
        "aiworkspace",
        "CreateDatasetVersion",
        "--DatasetId",
        dataset_id,
        "--body",
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    ]
    if not execute:
        command.append("--dryrun")
    result = runner(command)
    payload = _parse_result(result, "CreateDatasetVersion")
    return {
        "status": "CREATED" if execute else "DRY_RUN",
        "dataset_id": dataset_id,
        "lakefs_commit": body["SourceId"],
        "response": payload,
    }


def _find_existing_version(
    *,
    base: List[str],
    dataset_id: str,
    commit_id: str,
    manifest_sha256: Optional[str],
    runner: Runner,
) -> Optional[dict]:
    command = base + [
        "aiworkspace",
        "ListDatasetVersions",
        "--DatasetId",
        dataset_id,
        "--PageNumber",
        "1",
        "--PageSize",
        "100",
        "--SourceId",
        commit_id,
    ]
    payload = _parse_result(runner(command), "ListDatasetVersions")
    versions = payload.get("DatasetVersions", []) if isinstance(payload, dict) else []
    for version in versions:
        if version.get("SourceId") != commit_id:
            continue
        existing_digest = _label_value(version.get("Labels", []), "manifest_sha256")
        if manifest_sha256 and existing_digest and existing_digest != manifest_sha256:
            raise ReleaseConflictError(
                f"PAI already has commit {commit_id} with a different manifest checksum"
            )
        return version
    return None


# Uri 的 scheme 必须与 DataSourceType 严格对应。
#
# 2026-08-03 在真实账号上逐条实测得出，**与官方文档不符**：ROS 的
# ALIYUN::PAI::DatasetVersion 文档说 CPFS 用 `nas://<cpfs-fsid>.region/...`，
# 实际 PAI 只接受 `cpfs://`，用 nas:// 一律报 `Uri format error`。
# 别按文档改回去，除非在真实账号上重新验过。
_URI_SCHEMES = {
    "OSS": "oss://",
    "NAS": "nas://",
    "CPFS": "cpfs://",
    "BMCPFS": "bmcpfs://",
}


def _validate_request(request: dict) -> None:
    if not isinstance(request, dict) or not request.get("dataset_id"):
        raise ValueError("request must contain dataset_id")
    body = request.get("body")
    if not isinstance(body, dict):
        raise ValueError("request must contain a body object")
    required = {"Property", "DataSourceType", "Uri", "SourceId", "ImportInfo"}
    missing = sorted(required - set(body))
    if missing:
        raise ValueError(f"PAI request body is missing fields: {missing}")
    if body["Property"] != "DIRECTORY":
        raise ValueError("only DIRECTORY dataset releases are supported")

    source_type = body["DataSourceType"]
    if source_type not in {"CPFS", "BMCPFS", "NAS"}:
        raise ValueError("only CPFS, BMCPFS and NAS dataset releases are supported")

    uri = body["Uri"]
    if not isinstance(uri, str):
        raise ValueError("Uri must be a string")
    expected = _URI_SCHEMES[source_type]
    if not uri.startswith(expected):
        raise ValueError(
            f"DataSourceType={source_type} 要求 Uri 以 {expected} 开头，实际是 {uri!r}。"
            "scheme 与类型必须严格对应，PAI 否则报 Uri format error。"
            "注意官方文档说 CPFS 用 nas:// 是错的，实测只接受 cpfs://。"
        )
    # Property=DIRECTORY 时结尾必须是斜杠，否则 PAI 报 "not DIRECTORY"。
    if not uri.endswith("/"):
        raise ValueError(f"Property=DIRECTORY 要求 Uri 以 / 结尾，实际是 {uri!r}")


def _subprocess_runner(command: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _parse_result(result: CommandResult, operation: str):
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise DatasetSinkError(f"aliyun {operation} failed: {message}")
    output = result.stdout.strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"raw": output}


def _label_value(labels: Sequence[dict], key: str) -> Optional[str]:
    for label in labels:
        if label.get("Key") == key:
            value = label.get("Value")
            return str(value) if value is not None else None
    return None
