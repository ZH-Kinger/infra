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


def _labels_for(release) -> list:
    """PAI Dataset Version 的标签。

    这是 PAI 控制台和 DSW 里**唯一的可检索面**：Version 名字由 PAI 自动分配成
    v1/v2/v3，本身没有含义。所以标签要同时承担两件事：

      1. **给机器校验**：lakefs_commit / manifest_sha256。training-guard 和
         verify --deep 靠它们确认「挂上来的确实是这个版本」。
      2. **给人检索**：dataset / repository / lakefs_tag / paimon_snapshot_id。
         少了这些，DSW 用户在下拉框里看到的就只有一串 v2/v3 和 64 位 hex，
         分不出哪个是哪个——而这是他们每天都要做的选择。

    **`lakefs_tag` 只是「找得到」的把手，不是挂载标识。** 挂载用的 Uri 始终是
    Commit 命名的路径。Tag 在 lakeFS 里是可以被人手工移动的，如果让它参与寻址，
    已发布版本指向的数据就会跟着变——那就破了「不挂载可变引用」这条硬规则。
    """
    labels = [
        # 机器校验用
        {"Key": "lakefs_commit", "Value": release.lakefs_commit},
        {"Key": "manifest_sha256", "Value": release.manifest_sha256},
        # 人检索用
        {"Key": "dataset", "Value": release.dataset},
        {"Key": "repository", "Value": release.repository},
    ]
    # 可选字段留空就不打标签，避免出现 "None" 这种字面量值。
    for key, value in (
        ("lakefs_tag", release.lakefs_tag),
        ("paimon_snapshot_id", release.paimon_snapshot_id),
    ):
        if value:
            labels.append({"Key": key, "Value": str(value)})
    return labels


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
            "Labels": _labels_for(release),
            "ImportInfo": json.dumps(import_info, separators=(",", ":")),
        },
        "release": asdict(release),
    }
