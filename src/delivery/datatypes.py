"""数据类型词表：所有桶的一级目录只能从这里取。

为什么要有这张表
────────────────
规范（docs/collab/planning/oss-storage-spec.md）的三条规则都压在它身上：

  · **一级目录只有一张词表，所有桶共用。** 原始桶、交付桶、回传桶里同一个词
    指同一类数据，搬运才能「只换桶名」。
  · **每种类型的层级登记在这里，同一种类型在所有桶里一模一样。**
    `label` 是 `<来源>/<版本>/<批次ID>/`、`public-datasets` 是 `<数据集名>/`，
    各桶自己定的话，同一个类型在两个桶里深度不同，「只换桶名」就不成立了。
  · **新增类型走审批。** 面板的「新增数据类型」申请通过后由 `append` 写进来，
    不是谁改一下文件就算数。

原先这套分类**写死在代码里、前后端各一份**（catalog.STAGES + storage.js 的 STAGE_META），
加一类数据要改两处代码再部署 —— 于是没人加，大家往桶根下随手建目录。

文件长这样
──────────
```json
{
  "types": {
    "third-party-data": {"label": "供应商数据", "layers": ["供应商", "批次ID"]},
    "public-datasets":  {"label": "开源数据集", "layers": ["数据集名"]}
  },
  "buckets": {
    "wuji-bucket-hangzhou": ["third-party-data", "public-datasets"]
  }
}
```
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import DeliveryError

FILENAME = "data-types.json"

#: 批次那一层的名字。面板对它有专门的输入（日期 + 场景 + 序号），其余层是自由的一段
BATCH = "批次ID"

#: 规范里有固定含义的目录名，**不能当数据类型**：
#: 拿 `tmp` 当类型的话，`tmp/` 下的数据会被 7 天清理规则一起删掉
RESERVED = frozenset({"general", "tmp", "staging", "qc", "_misc", "_staging"})

_KEY = re.compile(r"\A[a-z][a-z0-9-]{1,31}\Z")
_LAYER = re.compile(r"\A[\w一-鿿]{1,12}\Z")
_BUCKET = re.compile(r"\A[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
MAX_LAYERS = 4


class DataTypeError(DeliveryError):
    """数据类型词表不合法。"""


@dataclass(frozen=True)
class Registry:
    types: dict = field(default_factory=dict)
    buckets: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.types)

    def get(self, key: str) -> dict:
        got = self.types.get(key)
        if got is None:
            known = "、".join(sorted(self.types)) or "（一个都没有）"
            raise DataTypeError(f"数据类型词表里没有 {key!r}。现有：{known}")
        return dict(got)

    def for_bucket(self, bucket: str) -> tuple:
        return tuple(self.buckets.get(bucket, ()))


def check_key(key: str, where: str = "数据类型") -> str:
    got = str(key or "").strip()
    if not _KEY.match(got):
        raise DataTypeError(
            f"{where}：英文名只能用小写字母、数字和横线，字母开头，2～32 个字符，收到 {got!r}"
        )
    if got in RESERVED:
        raise DataTypeError(
            f"{where}：{got!r} 是规范里有固定含义的目录名（{'、'.join(sorted(RESERVED))}），"
            "不能当数据类型"
        )
    return got


def check_layers(layers, where: str = "数据类型") -> list:
    if not isinstance(layers, list) or not layers:
        raise DataTypeError(f'{where}：layers 是非空数组，例如 ["来源", "批次ID"]')
    if len(layers) > MAX_LAYERS:
        raise DataTypeError(f"{where}：层级最多 {MAX_LAYERS} 层 —— 再深就没人记得住该放哪了")
    out = []
    for name in layers:
        got = str(name or "").strip()
        if not _LAYER.match(got):
            raise DataTypeError(f"{where}：层名只能是 1～12 个汉字/字母/数字，收到 {got!r}")
        out.append(got)
    if len(set(out)) != len(out):
        raise DataTypeError(f"{where}：层名重复了：{out}")
    if BATCH in out and out[-1] != BATCH:
        raise DataTypeError(f"{where}：{BATCH} 只能是最后一层")
    return out


def parse(data, where: str = "数据类型词表") -> Registry:
    if not isinstance(data, dict):
        raise DataTypeError(f"{where}：顶层要是对象")
    unknown = sorted(k for k in set(data) - {"types", "buckets"} if not k.startswith("_"))
    if unknown:
        raise DataTypeError(f"{where}：不认识的字段 {'、'.join(unknown)}")
    types = {}
    for key, spec in (data.get("types") or {}).items():
        at = f"{where}：{key}"
        check_key(key, at)
        if not isinstance(spec, dict):
            raise DataTypeError(f"{at} 必须是对象")
        label = str(spec.get("label") or "").strip()
        if not label or len(label) > 20:
            raise DataTypeError(f"{at}.label 是 1～20 个字的中文名")
        types[key] = {
            "key": key,
            "label": label,
            "layers": check_layers(spec.get("layers"), at),
            "note": str(spec.get("note") or "")[:120],
        }
    buckets = {}
    for name, keys in (data.get("buckets") or {}).items():
        if not _BUCKET.match(str(name)):
            raise DataTypeError(f"{where}：{name!r} 不是合法的桶名")
        if not isinstance(keys, list):
            raise DataTypeError(f"{where}：buckets[{name!r}] 是类型名数组")
        bad = [k for k in keys if k not in types]
        if bad:
            raise DataTypeError(f"{where}：buckets[{name!r}] 用了词表里没有的类型 {bad[0]!r}")
        buckets[name] = tuple(dict.fromkeys(keys))
    return Registry(types, buckets)


def load(path: Optional[str]) -> Registry:
    """文件不在 = 词表为空（老模板照旧用写死的那套分类），不是错误。"""
    if not path or not Path(path).exists():
        return Registry()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataTypeError(f"读不了数据类型词表 {path}：{exc}") from exc
    return parse(data, where=f"数据类型词表 {path}")


def beside(templates_path: Optional[str]) -> Optional[str]:
    if not templates_path:
        return None
    return str(Path(templates_path).with_name(FILENAME))


def append(path: str, *, key: str, label: str, layers: list, buckets: list, note: str = "") -> dict:
    """审批通过后把一个新类型写进词表。返回写进去的那一条。

    **原子写 + 写前校验整张表**：写了一半的 JSON 会让所有模板加载失败，
    整个申请页打不开；校验放在写之前，写进去的一定是能加载的。

    **读改写全程持有文件锁**（`data-types.json.lock`，和 tickets.json.lock 同一个做法）：
    审批回调和 sweep 定时器是两个进程，两张不同的申请可能同时执行。不锁的话
    两边各自读到旧表、各加一个、后写的覆盖先写的 —— 先写的那个类型就没了，
    两张单子却都记成「已加入」（审计 M-2）。

    **已经有同名类型**：内容和这次申请一模一样就当成功（上一次写进去了、只是单子没来得及
    落盘，重试不该判失败，审计 L-1）；不一样就报错、不覆盖 —— 覆盖会悄悄改掉一个在用类型的
    层级，而那些已经按旧层级建好的目录不会跟着变。
    """
    import fcntl

    target = Path(path)
    with Path(f"{target}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        types = data.setdefault("types", {})
        table = data.setdefault("buckets", {})
        if key in types:
            same = (
                types[key].get("label") == label
                and list(types[key].get("layers") or []) == list(layers)
                and all(key in (table.get(b) or []) for b in buckets)
            )
            if same:
                return types[key]
            raise DataTypeError(f"词表里已经有 {key!r} 了，内容和这次申请不一样，不会覆盖")
        types[key] = {"label": label, "layers": list(layers)}
        if note:
            types[key]["note"] = str(note)[:120]
        for name in buckets:
            row = table.setdefault(name, [])
            if key not in row:
                row.append(key)
        parse(data)  # 写之前整张表过一遍
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".dtypes-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(target)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
        return types[key]
