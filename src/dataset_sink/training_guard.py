from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .errors import IntegrityError
from .materializer import verify_release


def validate_training_dataset(
    dataset_root: Path,
    *,
    expected_commit: str,
    expected_manifest_sha256: Optional[str] = None,
    expected_paimon_snapshot_id: Optional[str] = None,
    deep: bool = False,
) -> dict:
    release = verify_release(dataset_root, deep=deep)
    if release.lakefs_commit != expected_commit:
        raise IntegrityError(
            f"dataset commit mismatch: expected {expected_commit}, found {release.lakefs_commit}"
        )
    if (
        expected_manifest_sha256 is not None
        and release.manifest_sha256 != expected_manifest_sha256
    ):
        raise IntegrityError("dataset manifest checksum does not match the training request")
    if (
        expected_paimon_snapshot_id is not None
        and release.paimon_snapshot_id != expected_paimon_snapshot_id
    ):
        raise IntegrityError(
            "dataset Paimon snapshot does not match the training request"
        )
    result = asdict(release)
    result["guard"] = "PASSED"
    result["deep_verified"] = deep
    return result

