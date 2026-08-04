"""检测 PAI 工作负载是否绕过不可变 dataset release。

这是检测性控制，不是预防性控制。RAM 能禁止研发创建 Dataset/提交 DLC，
但 DSW 交互会话里的直接挂载无法从原理上彻底阻止；这里让绕过行为可见。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Iterable, Optional, Sequence
from urllib.parse import urlparse

from .aliyun_cli import CommandResult
from .errors import DatasetSinkError
from .registry import Registry


@dataclass(frozen=True)
class MountFinding:
    workload_type: str
    workload_id: str
    workload_name: str
    code: str
    message: str
    mount_path: str
    dataset_id: Optional[str] = None
    dataset_version: Optional[str] = None
    uri: Optional[str] = None


VersionResolver = Callable[[str, str], dict]


def audit_workloads(
    *,
    dlc_jobs: Iterable[dict] = (),
    dsw_instances: Iterable[dict] = (),
    resolve_version: VersionResolver,
    registry: Optional[Registry] = None,
    workspace_uri_prefixes: Sequence[str] = (),
) -> dict:
    findings = []
    checked_mounts = 0
    compliant_mounts = 0

    workloads = [
        ("DLC", item, item.get("JobId", ""), item.get("DisplayName", ""), "DataSources")
        for item in dlc_jobs
    ] + [
        (
            "DSW",
            item,
            item.get("InstanceId", ""),
            item.get("InstanceName", ""),
            "Datasets",
        )
        for item in dsw_instances
    ]

    for kind, workload, workload_id, name, field in workloads:
        for mount in workload.get(field) or []:
            checked_mounts += 1
            found = _audit_mount(
                kind=kind,
                workload_id=str(workload_id),
                name=str(name),
                mount=mount,
                resolve_version=resolve_version,
                registry=registry,
                workspace_uri_prefixes=workspace_uri_prefixes,
            )
            if found:
                findings.extend(found)
            else:
                compliant_mounts += 1

    return {
        "status": "PASS" if not findings else "VIOLATIONS_FOUND",
        "workloads_checked": len(workloads),
        "mounts_checked": checked_mounts,
        "compliant_mounts": compliant_mounts,
        "violation_count": len(findings),
        "findings": [asdict(item) for item in findings],
    }


def _audit_mount(
    *,
    kind: str,
    workload_id: str,
    name: str,
    mount: dict,
    resolve_version: VersionResolver,
    registry: Optional[Registry],
    workspace_uri_prefixes: Sequence[str],
) -> list[MountFinding]:
    mount_path = str(mount.get("MountPath") or "")
    dataset_id = mount.get("DataSourceId") or mount.get("DatasetId")
    version = mount.get("DataSourceVersion") or mount.get("DatasetVersion")
    uri = mount.get("Uri")
    common = {
        "workload_type": kind,
        "workload_id": workload_id,
        "workload_name": name,
        "mount_path": mount_path,
        "dataset_id": str(dataset_id) if dataset_id else None,
        "dataset_version": str(version) if version else None,
        "uri": str(uri) if uri else None,
    }
    findings = []

    access = mount.get("ActualMountAccess") or mount.get("MountAccess")
    if access != "RO":
        findings.append(
            MountFinding(
                code="MOUNT_NOT_READ_ONLY",
                message=f"挂载权限是 {access or '未声明'}，正式训练数据必须显式 RO",
                **common,
            )
        )

    if uri and not dataset_id:
        code = (
            "WORKSPACE_MOUNT"
            if _is_workspace_uri(str(uri), registry, workspace_uri_prefixes)
            else "DIRECT_URI_MOUNT"
        )
        message = (
            "直接挂载了可写 workspace，不能作为正式训练数据"
            if code == "WORKSPACE_MOUNT"
            else "直接 URI 挂载绕过了 PAI Dataset Version"
        )
        findings.append(MountFinding(code=code, message=message, **common))
        return findings

    if not dataset_id:
        findings.append(
            MountFinding(
                code="UNKNOWN_MOUNT_SOURCE",
                message="挂载既没有 Dataset ID，也没有可识别的 URI",
                **common,
            )
        )
        return findings
    if not version:
        findings.append(
            MountFinding(
                code="DATASET_VERSION_NOT_PINNED",
                message="只指定 Dataset ID、未显式固定 Dataset Version",
                **common,
            )
        )
        return findings

    released = resolve_version(str(dataset_id), str(version))
    labels = {
        str(item.get("Key")): str(item.get("Value"))
        for item in released.get("Labels") or []
        if item.get("Key") is not None and item.get("Value") is not None
    }
    source_id = str(released.get("SourceId") or "")
    commit = labels.get("lakefs_commit", "")
    manifest = labels.get("manifest_sha256", "")
    release_uri = str(released.get("Uri") or "")

    if not source_id or not commit or not manifest:
        findings.append(
            MountFinding(
                code="UNMANAGED_DATASET_VERSION",
                message="Dataset Version 缺少 SourceId/lakefs_commit/manifest_sha256 发布身份",
                **common,
            )
        )
    elif source_id != commit:
        findings.append(
            MountFinding(
                code="RELEASE_IDENTITY_MISMATCH",
                message=f"SourceId={source_id} 与 lakefs_commit={commit} 不一致",
                **common,
            )
        )
    elif _uri_leaf(release_uri) != commit:
        findings.append(
            MountFinding(
                code="RELEASE_PATH_MISMATCH",
                message="Dataset Version URI 的最后一级目录不是 lakeFS Commit",
                **{**common, "uri": release_uri},
            )
        )
    return findings


def _is_workspace_uri(
    uri: str, registry: Optional[Registry], workspace_uri_prefixes: Sequence[str]
) -> bool:
    normalized = uri.rstrip("/") + "/"
    if any(normalized.startswith(prefix.rstrip("/") + "/") for prefix in workspace_uri_prefixes):
        return True
    if registry is None or not uri.startswith("oss://"):
        return False
    parsed = urlparse(uri)
    bucket = parsed.netloc.split(".", 1)[0]
    prefix = parsed.path.strip("/")
    try:
        return registry.resolve(bucket, prefix).mode == "workspace"
    except DatasetSinkError:
        return False


def _uri_leaf(uri: str) -> str:
    return urlparse(uri).path.rstrip("/").rsplit("/", 1)[-1] if uri else ""


class AliyunPaiAuditReader:
    """通过 aliyun CLI 的默认凭证链做只读查询。"""

    def __init__(
        self,
        *,
        region: str,
        workspace_id: str,
        profile: Optional[str] = None,
        cli_path: str = "aliyun",
        runner: Optional[Callable[[Sequence[str]], CommandResult]] = None,
    ) -> None:
        if runner is None and shutil.which(cli_path) is None:
            raise DatasetSinkError(f"Alibaba Cloud CLI was not found: {cli_path}")
        self.region = region
        self.workspace_id = workspace_id
        self.profile = profile
        self.cli_path = cli_path
        self.runner = runner or _run
        self._versions: Dict[tuple[str, str], dict] = {}

    def collect(self, kind: str = "both") -> tuple[list[dict], list[dict]]:
        jobs = self._collect_dlc() if kind in {"both", "dlc"} else []
        instances = self._collect_dsw() if kind in {"both", "dsw"} else []
        return jobs, instances

    def resolve_version(self, dataset_id: str, version: str) -> dict:
        key = (dataset_id, version)
        if key not in self._versions:
            self._versions[key] = self._call(
                "aiworkspace",
                "GetDatasetVersion",
                "--DatasetId",
                dataset_id,
                "--VersionName",
                version,
                endpoint=f"aiworkspace.{self.region}.aliyuncs.com",
            )
        return self._versions[key]

    def _collect_dlc(self) -> list[dict]:
        listed = self._pages("pai-dlc", "ListJobs", "Jobs", ["--WorkspaceId", self.workspace_id])
        return [
            self._call(
                "pai-dlc",
                "GetJob",
                "--JobId",
                str(item["JobId"]),
                "--NeedDetail",
                "true",
            )
            for item in listed
            if item.get("JobId")
        ]

    def _collect_dsw(self) -> list[dict]:
        listed = self._pages(
            "pai-dsw",
            "ListInstances",
            "Instances",
            ["--WorkspaceId", self.workspace_id, "--ResourceId", "ALL"],
        )
        return [
            self._call("pai-dsw", "GetInstance", "--InstanceId", str(item["InstanceId"]))
            for item in listed
            if item.get("InstanceId")
        ]

    def _pages(self, product: str, operation: str, field: str, args: list[str]) -> list[dict]:
        items = []
        page = 1
        while True:
            payload = self._call(
                product,
                operation,
                *args,
                "--PageNumber",
                str(page),
                "--PageSize",
                "100",
            )
            batch = payload.get(field) or []
            items.extend(batch)
            total = int(payload.get("TotalCount") or len(items))
            if len(items) >= total or not batch:
                return items
            page += 1

    def _call(
        self,
        product: str,
        operation: str,
        *args: str,
        endpoint: Optional[str] = None,
    ) -> dict:
        command = [self.cli_path, "--region", self.region]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend([product, operation, *args])
        if endpoint:
            command.extend(["--endpoint", endpoint])
        result = self.runner(command)
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise DatasetSinkError(f"aliyun {product} {operation} failed: {message}")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise DatasetSinkError(f"aliyun {product} {operation} returned invalid JSON") from exc


def _run(command: Sequence[str]) -> CommandResult:
    completed = subprocess.run(  # noqa: S603 - argv list, no shell; CLI path is explicit
        list(command), check=False, capture_output=True, text=True
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)
