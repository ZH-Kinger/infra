"""数据源注册表：管理员声明哪些对象存储位置可以作为数据源，用户只能从中选。

在此之前「哪些 OSS 前缀算数据源」是隐式的：`scan-oss --bucket X --prefix Y` 能指向
任意位置，能不能读全靠 RAM 兜底。这有两个问题：

  1. **报错太晚太差。** 用户拿到的是 `AccessDenied`，而不是「这个前缀没注册」。
     前者要翻 RAM 策略才能查明白，后者一眼就知道该找谁。
  2. **管理面和用户面混在一起。** 谁有权决定「什么可以当数据源」这件事，
     没有一个明确的地方回答。

注册表把这件事显式化，并且是**两道强制**：

    CLI 本地校验   快速失败、报错清楚，但绕得过去（用户可以不传 --registry）
    RAM 策略       绕不过去，但报错难懂

只有前者是好用不安全，只有后者是安全不好用。两道都要。

注册表本身由 Terraform 的 `data_sources` 变量生成（`deploy/data-sources.json`），
改它要走 access 层的 PR + 安全团队评审——和改 RAM 策略同一条路径，因为它**就是**
在改 RAM 策略。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from .errors import DatasetSinkError

# readonly：只读数据源。典型是存量数据前缀——被 lakeFS 零拷贝 import 引用之后
#           就不能再改，否则 Commit 悬空。
# archive ：dataset-sink 自己写入的归档前缀。这类桶还必须打 cpfs-dataflow 标签
#           并开启版本控制，否则 CPFS 数据流动的沉淀用不了。
MODES = ("readonly", "archive")


@dataclass(frozen=True)
class DataSource:
    name: str
    bucket: str
    prefix: str
    mode: str

    @property
    def location(self) -> str:
        return f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket

    def covers(self, bucket: str, prefix: str) -> bool:
        """这个数据源是否覆盖给定的桶与前缀。

        必须按**路径段边界**判断：`legacy/robotics` 不覆盖 `legacy/robotics-old`。
        只比字符串前缀会让用户读到未注册的相邻目录——而那正是注册表要防的事。
        """
        if bucket != self.bucket:
            return False
        if not self.prefix:
            return True
        target = prefix.strip("/")
        mine = self.prefix.strip("/")
        return target == mine or target.startswith(mine + "/")


@dataclass(frozen=True)
class Registry:
    sources: Tuple[DataSource, ...]
    path: Optional[str] = None

    def resolve(self, bucket: str, prefix: str) -> DataSource:
        """找到覆盖该位置的数据源，取最长匹配；找不到就报错。"""
        best: Optional[DataSource] = None
        for source in self.sources:
            if source.covers(bucket, prefix) and (
                best is None or len(source.prefix) > len(best.prefix)
            ):
                best = source
        if best is None:
            known = ", ".join(sorted(s.location for s in self.sources)) or "(空)"
            raise DatasetSinkError(
                f"{bucket}/{prefix.strip('/')} 不在数据源注册表里。\n"
                f"已注册的位置：{known}\n"
                "数据源由管理员在 infra/envs/<env>/access 的 data_sources 里声明，"
                "改动走 PR + 安全团队评审。需要新增请找管理员，不要绕过——"
                "RAM 策略同样只放行注册过的前缀，绕过去也读不到。"
            )
        return best

    def assert_writable(self, bucket: str, prefix: str) -> DataSource:
        """确认该位置可写。`readonly` 数据源拒绝写入。"""
        source = self.resolve(bucket, prefix)
        if source.mode != "archive":
            raise DatasetSinkError(
                f"数据源 {source.name}（{source.location}）是 {source.mode}，不允许写入。\n"
                "只读数据源通常是已经被 lakeFS 零拷贝 import 引用的存量前缀——"
                "改动其中的对象会让已发布的 Commit 悬空，且当时不会报错。\n"
                '要归档新数据请写到 mode = "archive" 的数据源下。'
            )
        return source


def load_registry(path: Path) -> Registry:
    """从 JSON 读注册表。文件由 Terraform 生成，不要手改。"""
    file = Path(path)
    if not file.is_file():
        raise DatasetSinkError(
            f"数据源注册表不存在: {path}\n"
            "它由 Terraform 的 data_sources 变量生成，跑 `make render-data-sources` 同步。"
        )
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetSinkError(f"数据源注册表不是合法 JSON: {path}: {exc}") from exc
    return build_registry(payload.get("data_sources", payload), path=str(file))


def build_registry(entries: Sequence[dict], *, path: Optional[str] = None) -> Registry:
    if not isinstance(entries, (list, tuple)):
        raise DatasetSinkError("data_sources 必须是数组")

    seen: Dict[str, str] = {}
    sources = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise DatasetSinkError(f"data_sources[{index}] 不是对象")
        name = str(raw.get("name") or "").strip()
        bucket = str(raw.get("bucket") or "").strip()
        prefix = str(raw.get("prefix") or "").strip().strip("/")
        mode = str(raw.get("mode") or "readonly").strip()
        if not name or not bucket:
            raise DatasetSinkError(f"data_sources[{index}] 缺少 name 或 bucket")
        if mode not in MODES:
            raise DatasetSinkError(
                f"data_sources[{index}] 的 mode 必须是 {MODES} 之一，得到 {mode!r}"
            )
        if name in seen:
            raise DatasetSinkError(f"数据源名字重复: {name}")
        seen[name] = bucket
        sources.append(DataSource(name=name, bucket=bucket, prefix=prefix, mode=mode))

    return Registry(sources=tuple(sources), path=path)
