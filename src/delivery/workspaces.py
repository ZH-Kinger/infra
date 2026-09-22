"""地域登记表：一个地域一条，模板只引用 key。

为什么要有这张表
────────────────
原先工作空间配置直接抄在每个模板里，一份七个字段（id / region / roles / mount /
bucket / bucket_region / bucket_prefix）。加一个地域就是复制一整块，于是：

  · **`roles` 各抄一份会漂。** 现网实测过一个人只有 `PAI.AlgoDeveloper`
    而同空间其他人都是三件套 —— 角色写在几个地方，改的时候就会漏掉几个地方。
  · **写错要等到有人建号那一刻才炸。** `bucket_region` 写成裸地域时，
    OSS 的主机名拼出来根本不存在，表现是 DNS 解析失败；而它一失败，
    建号流程里数据集会被**一起跳过**，人只看到一句「他还进不去 DSW/DLC」。
  · 加了新地域，**已有账号不会自动补** —— 得有人记得去补。

抽成一张表之后，加地域 = 加一条 + 在模板里填个 key，不碰代码。
格式上能查的一律在**加载时**查完（见 `_one`），查不了的（工作空间 ID 对不对、
桶在不在）留给 `delivery requests workspaces --check` 真去云上探一次。

文件长这样
──────────
```json
{
  "defaults": {"roles": ["PAI.AlgoDeveloper", "PAI.AlgoOperator", "PAI.LabelManager"],
               "bucket_prefix": "general"},
  "workspaces": {
    "hz":   {"label": "杭州",   "id": "640957", "region": "cn-hangzhou",
             "mount": "cpfs-….cn-hangzhou.cpfs.aliyuncs.com",
             "bucket": "wuji-algo-dev-hz", "bucket_region": "cn-hangzhou"},
    "sing": {"label": "新加坡", "id": "284761", "region": "ap-southeast-1", "...": "..."}
  }
}
```
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import DeliveryError

#: 工作空间成员的角色。照现网的写法给 —— 绝大多数人是这三件套（普通算法开发者）
DEFAULT_ROLES = ("PAI.AlgoDeveloper", "PAI.AlgoOperator", "PAI.LabelManager")

#: 登记表默认放在模板文件旁边。**不给它单独的命令行参数** ——
#: 多一个参数就多一处「systemd 里忘了传」的可能，而那种漏的表现是
#: 所有模板的 workspaces 都解析不到，建号安静地少做三件事
FILENAME = "workspaces.json"

_KEYS = {
    "label",
    "id",
    "region",
    "roles",
    "mount",
    "bucket",
    "bucket_region",
    "bucket_prefix",
    "quota_gib",
}
_DEFAULTABLE = ("roles", "bucket_prefix", "quota_gib")

#: 地域是 `cn-hangzhou` / `ap-southeast-1` 这种。**不收带 `oss-` 前缀的写法**：
#: 两套写法并存正是当初拼出不存在的域名的原因，登记表这一层只留一种
_REGION = re.compile(r"\A[a-z]{2}-[a-z]+(?:-\d+)?\Z")
#: CPFS 挂载点是个域名，不是 URI。写成 `bmcpfs://…` 的话数据集的 URI 会拼成
#: `bmcpfs://bmcpfs://…` —— 建得出来，但挂载时才发现是坏的
_MOUNT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.\-]*\.cpfs\.aliyuncs\.com\Z")
_BUCKET = re.compile(r"\A[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]\Z")
_KEY = re.compile(r"\A[a-z][a-z0-9\-]{0,23}\Z")


class WorkspaceError(DeliveryError):
    """地域登记表不合法。"""


@dataclass(frozen=True)
class Registry:
    """key → 解析好的工作空间配置。"""

    items: dict = field(default_factory=dict)

    def get(self, key: str) -> dict:
        got = self.items.get(key)
        if got is None:
            known = "、".join(sorted(self.items)) or "（一个都没有）"
            raise WorkspaceError(f"地域登记表里没有 {key!r}。现有：{known}")
        return dict(got)

    def all_keys(self) -> list:
        """排好序的全部 key。**不叫 `keys()`** —— 那个名字会让人（和 linter）
        以为这是个 Mapping，而它不是。"""
        return sorted(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


def _one(key: str, spec, defaults: dict, where: str) -> dict:
    """查一条。**格式上能查的在这里查完，不留到建号那一刻。**"""
    if not _KEY.match(key):
        raise WorkspaceError(f"{where}：地域 key {key!r} 只能用小写字母、数字和横线，字母开头")
    if not isinstance(spec, dict):
        raise WorkspaceError(f"{where}：{key} 必须是对象")
    unknown = sorted(set(spec) - _KEYS)
    if unknown:
        raise WorkspaceError(f"{where}：{key} 里不认识的字段 {'、'.join(unknown)}（拼错了？）")

    merged = {k: defaults[k] for k in _DEFAULTABLE if k in defaults}
    merged.update({k: v for k, v in spec.items() if v not in (None, "")})

    wid = str(merged.get("id") or "").strip()
    if not wid.isdigit():
        raise WorkspaceError(f"{where}：{key}.id 是数字（控制台上那串工作空间 ID），收到 {wid!r}")
    region = str(merged.get("region") or "").strip()
    if not _REGION.match(region):
        raise WorkspaceError(
            f"{where}：{key}.region 要写成 cn-hangzhou / ap-southeast-1 这样，收到 {region!r}"
            "（**不要带 oss- 前缀**）"
        )
    roles = merged.get("roles") or list(DEFAULT_ROLES)
    if not isinstance(roles, list) or not all(isinstance(r, str) and r for r in roles):
        raise WorkspaceError(f"{where}：{key}.roles 是角色名字符串数组")

    out = {
        "key": key,
        "label": str(merged.get("label") or key),
        "id": wid,
        "region": region,
        "roles": list(roles),
    }
    mount = str(merged.get("mount") or "").strip()
    if mount:
        if not _MOUNT.match(mount):
            raise WorkspaceError(
                f"{where}：{key}.mount 是 CPFS 挂载点**域名**"
                f"（cpfs-….cn-hangzhou.cpfs.aliyuncs.com），不带 bmcpfs:// 前缀。收到 {mount!r}"
            )
        out["mount"] = mount
    bucket = str(merged.get("bucket") or "").strip()
    if bucket:
        if not _BUCKET.match(bucket):
            raise WorkspaceError(f"{where}：{key}.bucket 不是合法的 OSS 桶名：{bucket!r}")
        out["bucket"] = bucket
        breg = str(merged.get("bucket_region") or "").strip()
        if not _REGION.match(breg):
            raise WorkspaceError(
                f"{where}：给了 {key}.bucket 就要给 bucket_region，"
                f"写成 cn-hangzhou 这样（不带 oss- 前缀）。收到 {breg!r}"
            )
        out["bucket_region"] = breg
        prefix = str(merged.get("bucket_prefix") or "").strip().strip("/")
        if prefix:
            out["bucket_prefix"] = prefix
    # 两种落点至少要有一种，否则「给他开个人目录」这句话落不到地上
    if not (out.get("mount") or out.get("bucket")):
        raise WorkspaceError(
            f"{where}：{key} 要给 mount（CPFS 挂载点）或 bucket（OSS 开发桶）其中之一 —— "
            "不然不知道把他的个人目录开在哪"
        )
    quota = merged.get("quota_gib")
    if quota not in (None, ""):
        if not isinstance(quota, int) or isinstance(quota, bool) or quota <= 0:
            raise WorkspaceError(f"{where}：{key}.quota_gib 是正整数")
        out["quota_gib"] = quota
    return out


def parse(data, where: str = "地域登记表") -> Registry:
    if not isinstance(data, dict):
        raise WorkspaceError(f"{where}：顶层要是对象")
    # `_` 开头的是写给人看的说明（为什么这么配、还差什么），不参与校验 —— 和模板同一个约定
    unknown = sorted(
        k for k in set(data) - {"schema", "defaults", "workspaces"} if not k.startswith("_")
    )
    if unknown:
        raise WorkspaceError(f"{where}：不认识的字段 {'、'.join(unknown)}")
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise WorkspaceError(f"{where}：defaults 要是对象")
    rows = data.get("workspaces") or {}
    if not isinstance(rows, dict):
        raise WorkspaceError(f"{where}：workspaces 是 key → 配置 的对象")
    return Registry({key: _one(key, spec, defaults, where) for key, spec in rows.items()})


def load(path: Optional[str]) -> Registry:
    """读登记表。**文件不在 = 一个地域都没登记**（空表），不是错误。

    空表本身不会让任何模板炸；只有模板引用了 key 才会在那时报「登记表里没有它」，
    而那句话指得到具体哪一条要补。
    """
    if not path or not Path(path).exists():
        return Registry()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceError(f"读不了地域登记表 {path}：{exc}") from exc
    return parse(data, where=f"地域登记表 {path}")


def beside(templates_path: Optional[str]) -> Optional[str]:
    """模板文件旁边那份登记表的路径。"""
    if not templates_path:
        return None
    return str(Path(templates_path).with_name(FILENAME))
