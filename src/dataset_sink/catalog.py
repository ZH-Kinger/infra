"""Resolve platform-owned dataset names to existing PAI Dataset containers."""

from __future__ import annotations

import json
from typing import Optional

from .errors import DatasetSinkError


def resolve_pai_dataset_id(
    dataset: str,
    mapping_json: str,
    *,
    fallback: Optional[str] = None,
) -> str:
    """Return the governed PAI Dataset ID for a short dataset name.

    The mapping is controlled as a GitHub Repository Variable. Users submit only
    ``dataset``; accepting a Dataset ID as workflow input would let them target an
    arbitrary PAI container and bypass the platform catalogue.
    """
    name = dataset.strip()
    if not name:
        raise DatasetSinkError("数据集名称不能为空")

    raw = mapping_json.strip()
    if not raw:
        value = (fallback or "").strip()
        if value:
            return value
        raise DatasetSinkError("缺少 PAI_DATASET_IDS_JSON 或兼容变量 PAI_DATASET_ID")

    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DatasetSinkError(f"PAI_DATASET_IDS_JSON 不是合法 JSON: {exc}") from exc
    if not isinstance(mapping, dict):
        raise DatasetSinkError("PAI_DATASET_IDS_JSON 必须是 JSON object")

    value = mapping.get(name)
    if not isinstance(value, str) or not value.strip():
        known = ", ".join(sorted(str(key) for key in mapping)) or "(空)"
        raise DatasetSinkError(f"数据集 {name!r} 没有配置 PAI Dataset ID；已配置：{known}")
    return value.strip()
