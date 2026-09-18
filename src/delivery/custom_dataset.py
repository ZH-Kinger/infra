"""自定义数据集：把一条**已有**的存储路径登记成 PAI 数据集。

自动建的那套（`provision_tree`）只管「每个人一块地」，名字和路径都是算出来的。
这里管的是另一类：共享数据、项目数据、处理好的中间产物 —— 名字和路径由人指定。

为什么必须有 URI 白名单（**这是这个功能唯一的控制点**）
────────────────────────────────────────────────
PAI 在 DSW/DLC 里读写存储用的是**服务角色**（`AliyunPAIDSWDefaultRole`），
那个角色对全账号所有 OSS 桶有 `GetObject`/`PutObject`/`DeleteObject`，
`Resource: *`、无任何 Condition —— 和申请人自己的 RAM 策略没关系。

所以「登记一条指向某路径的数据集」等价于「获得那个路径的读写权限」。
不限制 URI 的话，任何能调这个功能的人都能注册一条指向财务桶、指向别人目录的
数据集，挂进自己的 DSW 就读到了。**白名单挡的就是这条路。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .errors import DeliveryError

#: PAI 对数据集名的规则：字母/数字/中文开头，可含下划线和短横线，1–127 字符。
#: 这里再收一道：**不允许中文**，因为名字会和路径一起出现在各种命令行和日志里
_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
#: 路径里每一段的白名单。和 `workspace_tree` 同一套 —— 这两段会拼进 OSS 的 key
#: 和 RAM 策略的 `oss:Prefix` 条件里，一个 `*` 或 `../` 就能让一条策略覆盖别处
_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class CustomDatasetError(DeliveryError):
    """自定义数据集的参数不合法。"""


@dataclass(frozen=True)
class Spec:
    """要登记的那一条。"""

    name: str
    workspace: str
    region: str
    source: str
    bucket: str
    prefix: str
    #: CPFS 用。**必须从这里取，不能再回头用命令行参数** —— 白名单校验的是这两个，
    #: 调用方绕过去用 `args.fs_id` 的话，白名单就成了纯装饰
    fs_id: str = ""
    mount: str = ""
    #: OSS 的桶地域（带 `oss-` 前缀）。**和上面的 `region` 不是一回事** ——
    #: 那个是 PAI 工作空间的地域。两个都叫 region，看串了就会把 Uri 写错，
    #: 而 Uri 事后改不回来（UpdateDataset 改 Uri 静默无效）
    oss_region: str = ""
    owner_login: str = ""
    description: str = ""

    @property
    def scope(self) -> str:
        return f"{self.region}/{self.workspace}/{self.name}"


def validate(
    *,
    name: str,
    bucket: str,
    prefix: str,
    source: str,
    workspace: str,
    region: str,
    allowed: Iterable,
    fs_id: str = "",
    mount: str = "",
    oss_region: str = "",
    owner_login: str = "",
    description: str = "",
) -> Spec:
    """检查并归一。**任何一条不过就抛**，不做「尽力而为」的修补。

    `allowed` 是允许登记的桶 / 文件系统白名单。**空白名单一律拒绝**，
    而不是「没配就都放行」—— 配置缺失时放行是这类控制点最常见的死法。
    """
    ok = {str(b or "").strip().lower() for b in (allowed or ())} - {""}
    if not ok:
        raise CustomDatasetError(
            "没有配置允许登记的桶/文件系统白名单，拒绝登记。"
            "**不是「没配就都放行」** —— PAI 的服务角色对所有桶有读写删权限，"
            "不限制 URI 等于把整个账号的存储开出去"
        )
    name = str(name or "").strip()
    if not _NAME.match(name):
        raise CustomDatasetError(
            f"数据集名 {name!r} 不合法：字母或数字开头，只含字母数字和 . _ -，最长 63 字符"
        )
    bucket = str(bucket or "").strip()
    if bucket.lower() not in ok:
        raise CustomDatasetError(
            f"{bucket!r} 不在白名单里。允许的是：{'、'.join(sorted(ok))}。\n"
            f"要加新的桶/文件系统，改配置并说明为什么 —— 这条白名单是这个功能"
            f"唯一的控制点，PAI 挂载走的是服务角色，不受申请人自己的权限约束"
        )
    parts = [p for p in str(prefix or "").strip("/").split("/") if p]
    if not parts:
        raise CustomDatasetError("路径不能是桶的根目录 —— 那等于把整个桶登记成一个数据集")
    for seg in parts:
        if not _SEGMENT.match(seg):
            raise CustomDatasetError(f"路径里的 {seg!r} 不合法（禁 .. / 空格 / 通配符等）")
    if source not in ("OSS", "BMCPFS"):
        raise CustomDatasetError(f"存储类型只能是 OSS / BMCPFS，收到 {source!r}")

    fs_id = str(fs_id or "").strip()
    mount = str(mount or "").strip()
    oss_region = str(oss_region or "").strip()
    if source == "OSS" and not oss_region.startswith("oss-"):
        # 这条路径一次 OSS 调用都不打，填错地域不会有任何东西报错 —— PAI 收下、
        # 返回 DatasetId、打印「建好了」，等有人挂载才发现
        raise CustomDatasetError(
            f"OSS 的桶地域要带 oss- 前缀（如 oss-cn-hangzhou），收到 {oss_region!r}"
        )
    if source == "BMCPFS":
        # **CPFS 的定位参数也要过白名单。** 只校验 `bucket` 的话，白名单在这条路径上
        # 是纯装饰 —— 真正决定 PAI 挂哪个文件系统的是 `fs_id` 和 `mount`，
        # 拿一个白名单里的 bucket 过检、再传别人的 fs_id，就绕过去了
        if fs_id != bucket:
            raise CustomDatasetError(
                f"CPFS 的文件系统 ID（{fs_id!r}）必须和白名单里那一条（{bucket!r}）一致 —— "
                "白名单校验的就是它，不一致等于绕过了唯一的控制点"
            )
        if not mount or fs_id.removeprefix("bmcpfs-").removeprefix("cpfs-") not in mount:
            raise CustomDatasetError(
                f"挂载点 {mount!r} 里没有文件系统 ID {fs_id!r} —— "
                "现网写法是 `cpfs-<fsid 尾段>-vpc-x.<地域>.cpfs.aliyuncs.com`。"
                "挂载点指向别的文件系统的话，白名单就白校验了"
            )
    return Spec(
        name=name,
        workspace=str(workspace or "").strip(),
        region=str(region or "").strip(),
        source=source,
        bucket=bucket,
        prefix="/".join(parts) + "/",
        fs_id=fs_id,
        mount=mount,
        oss_region=oss_region,
        owner_login=str(owner_login or "").strip(),
        description=str(description or "").strip()[:200],
    )


def load_allowed(path: Optional[str]) -> list:
    """白名单文件 `identity/dataset-buckets.json`。没配就是空清单 → `validate` 会拒。"""
    import json
    from pathlib import Path

    if not path or not Path(path).exists():
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CustomDatasetError(f"读不了白名单 {path}：{exc}") from exc
    names = data.get("allowed") if isinstance(data, dict) else None
    if not isinstance(names, list):
        raise CustomDatasetError(f'{path} 格式应为 {{"allowed": ["桶名或 fs-id", ...]}}')
    return [str(n).strip() for n in names if str(n).strip()]
