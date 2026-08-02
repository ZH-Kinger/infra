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
    if body["DataSourceType"] not in {"CPFS", "BMCPFS", "NAS"}:
        raise ValueError("only CPFS, BMCPFS and NAS dataset releases are supported")


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
