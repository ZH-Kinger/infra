from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .materializer import verify_release


@dataclass(frozen=True)
class CpfsRegistration:
    """一次 CreateDatasetVersion 所需的文件系统坐标。

    `data_source_type` 必须与**父 Dataset** 的类型一致，否则 PAI 报
    `DataSourceType not match`。且 PAI 按类型分别校验 Uri：
        NAS    nas://<fsid>.<region>/<subpath>/        只校验格式，不校验存在
        CPFS   nas://<cpfs-fsid>.<region>/<subpath>/   **会校验文件系统真实存在**
    2026-08-02 在真实账号上逐条验证过。
    """

    dataset_id: str
    region: str
    filesystem_id: str
    uri: str
    filesystem_path: Optional[str] = None
    data_source_type: str = "CPFS"
    protocol_service_id: Optional[str] = None
    export_id: Optional[str] = None
    mount_target: Optional[str] = None
    is_vpc_mount: Optional[bool] = None


def build_create_dataset_version_request(
    release_dir: Path,
    registration: CpfsRegistration,
) -> dict:
    release = verify_release(release_dir)
    import_info = {
        "region": registration.region,
        "fileSystemId": registration.filesystem_id,
        "path": registration.filesystem_path or release.release_path,
    }
    optional = {
        "protocolServiceId": registration.protocol_service_id,
        "exportId": registration.export_id,
        "mountTarget": registration.mount_target,
        "isVpcMount": registration.is_vpc_mount,
    }
    import_info.update({key: value for key, value in optional.items() if value is not None})

    return {
        "dataset_id": registration.dataset_id,
        "api": {
            "product": "AIWorkSpace",
            "version": "2021-02-04",
            "method": "POST",
            "path": f"/api/v1/datasets/{registration.dataset_id}/versions",
        },
        "body": {
            "Property": "DIRECTORY",
            "DataSourceType": registration.data_source_type,
            "Uri": registration.uri,
            "SourceType": "USER",
            "SourceId": release.lakefs_commit,
            "DataSize": release.size_bytes,
            "DataCount": release.file_count,
            "Labels": [
                {"Key": "lakefs_commit", "Value": release.lakefs_commit},
                {"Key": "manifest_sha256", "Value": release.manifest_sha256},
            ],
            "ImportInfo": json.dumps(import_info, separators=(",", ":")),
        },
        "release": asdict(release),
    }
