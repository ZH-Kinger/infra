"""投递层的错误类型。沿用仓库既有约定：全部挂在 DatasetSinkError 下。"""

from __future__ import annotations

from dataset_sink.errors import DatasetSinkError


class DeliveryError(DatasetSinkError):
    """投递层的基类错误。"""


class PlatformSpecError(DeliveryError):
    """平台描述符不合法（字段缺失、取值非法、或违反能力硬规）。"""


class PlatformNotFoundError(DeliveryError):
    """注册表里没有这个平台。"""
