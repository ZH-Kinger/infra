"""阿里云 CPFS ↔ OSS 数据流动：预热（Import）和沉降（Export）。

产品 `NAS`，版本 `2017-06-26`，RPC 风格 —— 走 `aliyun.call`，不引新 SDK。

三个动作就够了
──────────────
    DescribeDataFlows      查现有绑定 → 拿 DataFlowId
    CreateDataFlowTask     TaskAction=Import 预热 / Export 沉降
    DescribeDataFlowTasks  轮询状态和进度

**面板刻意不做 `CreateDataFlow`。**
CPFS 通用版的 DataFlow 绑在 Fileset 上，而对一个**已有数据的 Fileset 建流会把它清空**、
替换成 OSS 侧的内容。机器人那边为了覆盖「没有现成绑定」的场景做了临建临删，
面板不跟 —— 面板是给全公司自助用的，一个会清空目录的动作不该藏在「提交申请」后面。
找不到能覆盖这个目录的绑定就**明说找不到**，让人去把绑定建好。

方向由源和目的决定，不由人选
────────────────────────────
    oss://…  →  cpfs://…   预热（Import）
    cpfs://… →  oss://…    沉降（Export）
让人选「这是预热还是沉降」等于把一个系统能自己判断的问题推给申请人，
而他选错的表现是数据往反方向覆盖 —— 那是不可逆的。

字段的坑（都是机器人那边踩出来的）
────────────────────────────────
  · 失败原因在 `ErrorMsg`，**不是** `ErrorMessage` / `Message`。读错字段的后果是
    所有失败都退化成一句光秃秃的「任务 Failed」，没人知道为什么。
  · 进度计数藏在 `ProgressStats` 子对象里，顶层取不到 —— 所以要深搜。
  · 智算版（`bmcpfs-` 开头）的任务**必填** `ConflictPolicy`；通用版给了会被拒。
"""

from __future__ import annotations

from typing import Optional

from ..errors import DeliveryError
from . import aliyun

ENDPOINT = "nas.{region}.aliyuncs.com"
VERSION = "2017-06-26"

#: OSS → CPFS，把数据加载进文件系统
ACTION_IMPORT = "Import"
#: CPFS → OSS，把文件系统里的改动刷回对象存储
ACTION_EXPORT = "Export"

_DONE = {"completed"}
_FAILED = {"failed", "canceled"}

#: 智算版的文件系统 id 以这个开头。两个版本的任务参数不一样
_COMPUTING = "bmcpfs-"
#: 同名文件怎么办。默认跳过 —— 覆盖是不可逆的，而这条链常常是别人的数据
DEFAULT_CONFLICT = "SKIP_THE_FILE"


class NasError(DeliveryError):
    """CPFS 数据流动调用失败。"""


def _region(fs: dict, label: str) -> str:
    got = str((fs or {}).get("region") or "").strip()
    if not got:
        raise NasError(f"不知道 {label} 在哪个地域 —— 模板里没登记这个文件系统")
    return got


def computing(fs_id: str) -> bool:
    return str(fs_id or "").startswith(_COMPUTING)


def norm_dir(path: str) -> str:
    """目录归一成 `/a/b/` 这种。

    **两头都要有斜杠。** 少了开头那个，接口会把它当相对路径（相对什么没有定义）；
    少了结尾那个，`/a/bc` 这种目录会被 `/a/b` 的绑定误判成覆盖得到。
    """
    got = str(path or "").strip()
    if not got:
        return "/"
    if not got.startswith("/"):
        got = "/" + got
    if not got.endswith("/"):
        got += "/"
    return got


def _deep(node, *keys):
    """在嵌套结构里找第一个非空的键。进度计数在子对象里，顶层取不到。"""
    stack = [node]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for key in keys:
                if cur.get(key) not in (None, ""):
                    return cur[key]
            stack += list(cur.values())
        elif isinstance(cur, list):
            stack += list(cur)
    return ""


def _int(node, *keys) -> int:
    try:
        return int(_deep(node, *keys) or 0)
    except (TypeError, ValueError):
        return 0


def list_dataflows(*, fs_id: str, region: str, creds, transport=None) -> list:
    """这个文件系统上已有的绑定。返回 `[{id, fs_path, bucket, prefix}]`。"""
    try:
        got = aliyun.call(
            ENDPOINT.format(region=region),
            VERSION,
            "DescribeDataFlows",
            {"RegionId": region, "FileSystemId": fs_id},
            creds=creds,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001
        raise NasError(f"查不到 {fs_id} 的数据流动绑定：{str(exc)[:200]}") from exc
    out = []
    stack = [got]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            if cur.get("DataFlowId"):
                # **OSS 那头的前缀在 `SourceStoragePath`，不在 `SourceStorage` 里。**
                # 真机实测：`SourceStorage=oss://wuji-bucket-hangzhou`、
                # `SourceStoragePath=/teleop/`。只看前者的话前缀永远是空的，
                # 任务目录就没法换算成「相对绑定根」—— 而接口要的正是相对路径
                source = str(cur.get("SourceStorage") or "")
                bucket = source.replace("oss://", "").strip("/").partition("/")[0]
                out.append(
                    {
                        "id": str(cur["DataFlowId"]),
                        "fs_path": norm_dir(str(cur.get("FileSystemPath") or "/")),
                        "bucket": bucket,
                        "oss_path": norm_dir(str(cur.get("SourceStoragePath") or "/")),
                        "status": str(cur.get("Status") or ""),
                    }
                )
            stack += list(cur.values())
        elif isinstance(cur, list):
            stack += list(cur)
    return out


def resolve(rows: list, *, fs_path: str, bucket: str, oss_prefix: str) -> dict:
    """挑出能覆盖这次任务的绑定。纯函数。

    **两头都要落在绑定里面**：CPFS 目录在 `FileSystemPath` 之下，**而且** OSS 那头
    是同一个桶、前缀在 `SourceStoragePath` 之下。只对 CPFS 那头的话，会挑中一条
    绑在别的 OSS 前缀上的流 —— 任务下发的相对目录会被拼到那个前缀底下，
    读写的是另一块数据，而云上不会报错。

    **取最长的那个祖先。** 已有绑定通常比任务目标宽，有多个能覆盖时最长的那个
    最贴近目标。

    找不到就抛，**不返回 None** —— 一个 None 传下去会变成「DataFlowId 是空串」，
    而那个请求的后果说不准。
    """
    want = norm_dir(fs_path)
    want_oss = norm_dir(oss_prefix)
    hits = [
        r
        for r in rows
        if want.startswith(r["fs_path"])
        and r["bucket"] == bucket
        and want_oss.startswith(r.get("oss_path") or "/")
    ]
    if not hits:
        known = (
            "；".join(
                sorted(
                    {
                        f"{r['fs_path']} ↔ oss://{r['bucket']}{r.get('oss_path') or '/'}"
                        for r in rows
                    }
                )
            )
            or "（一条都没有）"
        )
        raise NasError(
            f"{want} ↔ oss://{bucket}{want_oss} 没有数据流动绑定能覆盖，搬不了。"
            f"已有的绑定：{known}。"
            "**面板不会替你建绑定** —— 对已有数据的目录建流会把它清空，"
            "这一步要人去控制台确认后再做"
        )
    return max(hits, key=lambda r: (len(r["fs_path"]), len(r.get("oss_path") or "")))


def relative(full: str, base: str) -> str:
    """把 `full` 写成相对绑定根 `base` 的目录（两头带斜杠）。

    **接口要的是相对路径。** `Directory` / `DstDirectory` 相对的是绑定的
    `FileSystemPath`（CPFS 那头）或 `SourceStoragePath`（OSS 那头）。传绝对路径的话，
    绑在 `/share/data/` 上的流收到 `/share/data/x/` 会去读写 `/share/data/share/data/x/`，
    而 `CreateDirIfNotExist` 还会把这个错目录建出来 —— 源为空时报「完成、0 个文件」，
    一次假成功。

    `full` 不在 `base` 下面就抛：`resolve` 已经保证过这一点，走到这里说明调用方
    拿错了绑定，原样返回的话就是上面那种错。
    """
    full, base = norm_dir(full), norm_dir(base)
    if base == "/":
        return full
    if not full.startswith(base):
        raise NasError(f"{full} 不在绑定根 {base} 下面，换算不出相对路径")
    return norm_dir(full[len(base) :])


def submit(
    *,
    fs_id: str,
    region: str,
    dataflow: str,
    action: str,
    directory: str,
    dst_directory: str = "",
    conflict: str = "",
    dry_run: bool = False,
    creds,
    transport=None,
) -> str:
    """建一个数据流动任务，返回 TaskId。

    `directory` / `dst_directory` 必须已经是**相对绑定根**的路径（见 `relative`）。
    `dry_run=True` 只让服务端预检、不建任务 —— 用来在上线前核参数。
    """
    if action not in (ACTION_IMPORT, ACTION_EXPORT):
        raise NasError(f"方向只能是 {ACTION_IMPORT} 或 {ACTION_EXPORT}，收到 {action!r}")
    params = {
        "RegionId": region,
        "FileSystemId": fs_id,
        "DataFlowId": dataflow,
        "TaskAction": action,
        "DataType": "MetaAndData",
        "Directory": norm_dir(directory),
    }
    if dst_directory:
        params["DstDirectory"] = norm_dir(dst_directory)
    if dry_run:
        params["DryRun"] = "true"
    if computing(fs_id):
        # 智算版必填；通用版给了会被拒
        params["ConflictPolicy"] = conflict or DEFAULT_CONFLICT
        if action == ACTION_IMPORT:
            params["CreateDirIfNotExist"] = "true"
    try:
        got = aliyun.call(
            ENDPOINT.format(region=region),
            VERSION,
            "CreateDataFlowTask",
            params,
            creds=creds,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001
        raise NasError(f"提交数据流动任务失败：{str(exc)[:300]}") from exc
    task = str(_deep(got, "TaskId") or "")
    if dry_run:
        return task or "dry-run-ok"
    if not task:
        # 拿不到 TaskId 就当没提交成。返回空串的话单子会转「在途」，
        # 而轮询一个空任务号永远查不到状态 —— 单子永远停在那儿
        raise NasError(f"提交了但没拿到 TaskId，云上返回：{str(got)[:200]}")
    return task


def poll(*, fs_id: str, region: str, task_id: str, creds, transport=None) -> dict:
    """查一次进度。返回 `{status, bytes, objects, error, done, failed}`。

    **查不到不算失败**：轮询是网络抖动的高发点，空状态让调用方继续等，
    真失败由终态字符串判定。
    """
    try:
        got = aliyun.call(
            ENDPOINT.format(region=region),
            VERSION,
            "DescribeDataFlowTasks",
            {
                "RegionId": region,
                "FileSystemId": fs_id,
                "Filters.1.Key": "TaskIds",
                "Filters.1.Value": task_id,
            },
            creds=creds,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "",
            "bytes": 0,
            "objects": 0,
            "error": f"查进度失败：{str(exc)[:160]}",
            "done": False,
            "failed": False,
        }
    status = str(_deep(got, "Status") or "")
    low = status.strip().lower()
    return {
        "status": status,
        "bytes": _int(got, "BytesDone", "ActualBytes"),
        "objects": _int(got, "FilesDone", "ActualFiles"),
        # 失败原因是 ErrorMsg —— 读错字段的话所有失败都退化成一句「任务 Failed」
        "error": str(_deep(got, "ErrorMsg", "ErrorMessage", "Message") or ""),
        "done": low in _DONE,
        "failed": low in _FAILED,
    }


def direction(src_scheme: str, dest_scheme: str) -> Optional[str]:
    """这对地址走预热还是沉降；不是这条链就返回 None。"""
    if src_scheme == "oss" and dest_scheme == "cpfs":
        return ACTION_IMPORT
    if src_scheme == "cpfs" and dest_scheme == "oss":
        return ACTION_EXPORT
    return None
