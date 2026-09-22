"""火山 vePFS ↔ TOS 数据流动：预热（Import）和沉降（Export）。

service `vepfs`，version `2022-01-01`，走面板自己的签名器（`volcano.call`），不引 SDK。

比阿里那边少一层
────────────────
火山**没有** `CreateDataFlow` 这种持久绑定对象 —— 任务参数里直接带 TOS 桶和前缀，
所以不需要 `resolve`、不需要临建临删，也就没有「建流会清空 Fileset」那个风险。
方向只由 `TaskAction` 决定，源和目的字段**不反转**。

字段的坑（机器人那边真机反查确认过）
──────────────────────────────────
  · `DataStorage` 是**裸桶名**。带 `tos://` 前缀会被回 `InvalidParameter.BucketName`。
  · `DataStoragePath` / `SubPath` 非空时**首尾都要带斜杠**（`/a/b/`）；允许空串（= 桶根）。
  · 任务状态在 SDK 里是自由字符串，不是受限枚举 —— 所以按**子串**归类。
    `Unsuccessful` 这种既含 `success` 又表失败的串必须先判失败，否则会被当成成功。

前置条件（控制台一次性，面板管不了）
──────────────────────────────────
vePFS 和 TOS 必须同地域；要开「vePFS→TOS 服务访问授权」；数据流动带宽要 > 0。
这几样没配的表现是任务建得出来但一直不动 —— 所以失败文案里要提一句。
"""

from __future__ import annotations

from typing import Optional

from ..errors import DeliveryError
from . import volcano

SERVICE = "vepfs"
VERSION = "2022-01-01"

#: TOS → vePFS，把数据加载进文件系统
ACTION_IMPORT = "Import"
#: vePFS → TOS，把文件系统里的改动刷回对象存储
ACTION_EXPORT = "Export"

#: 状态是自由字符串，按子串归类。**先判失败** —— `Unsuccessful` 同时命中两边
_DONE_HINTS = ("success", "finished", "complete", "done")
_FAIL_HINTS = ("unsuccess", "fail", "error", "cancel", "stopped", "abort")

#: 同名文件怎么办。默认跳过 —— 覆盖不可逆
POLICY = {"skip": "Skip", "latest": "KeepLatest", "overwrite": "OverWrite"}
DEFAULT_POLICY = "Skip"


class VepfsError(DeliveryError):
    """vePFS 数据流动调用失败。"""


def norm_dir(path: str) -> str:
    """首尾都带斜杠，或者空串。空串是合法的（桶根 / 文件系统根）。"""
    got = str(path or "").strip().strip("/")
    return f"/{got}/" if got else ""


def policy(same_name: str) -> str:
    return POLICY.get(str(same_name or "").strip().lower(), DEFAULT_POLICY)


def is_done(status: str) -> bool:
    low = str(status or "").lower()
    # 先判失败：`Unsuccessful` 里有 `success`
    if any(h in low for h in _FAIL_HINTS):
        return False
    return any(h in low for h in _DONE_HINTS)


def is_failed(status: str) -> bool:
    low = str(status or "").lower()
    return any(h in low for h in _FAIL_HINTS)


def _deep(node, *keys):
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


def submit(
    *,
    fs_id: str,
    region: str,
    action: str,
    bucket: str,
    prefix: str = "",
    sub_path: str = "",
    same_name: str = "",
    creds,
    transport=None,
) -> str:
    """建一个数据流动任务，返回 DataFlowTaskId。"""
    if action not in (ACTION_IMPORT, ACTION_EXPORT):
        raise VepfsError(f"方向只能是 {ACTION_IMPORT} 或 {ACTION_EXPORT}，收到 {action!r}")
    if not bucket:
        raise VepfsError("没给 TOS 桶名")
    if "://" in bucket:
        # 带前缀会被回 InvalidParameter.BucketName，而那个报错不会说是前缀的问题
        raise VepfsError(f"TOS 桶名要写裸名，不带 tos:// 前缀：{bucket!r}")
    body = {
        "FileSystemId": fs_id,
        "TaskAction": action,
        "DataType": "MetaAndData",
        "DataStorage": bucket,
        "DataStoragePath": norm_dir(prefix),
        "SameNameFilePolicy": policy(same_name),
    }
    if sub_path:
        body["SubPath"] = norm_dir(sub_path)
    try:
        got = volcano.call(
            SERVICE,
            VERSION,
            "CreateDataFlowTask",
            creds=creds,
            region=region,
            body=body,
            transport=transport,
        )
    except Exception as exc:  # noqa: BLE001
        raise VepfsError(f"提交数据流动任务失败：{str(exc)[:300]}") from exc
    task = str(_deep(got, "DataFlowTaskId", "TaskId") or "")
    if not task:
        # 拿不到任务号就当没提交成 —— 单子转「在途」之后轮询一个空任务号永远查不到，
        # 表现是单子永远停在那儿，连「卡住」都不报
        raise VepfsError(f"提交了但没拿到任务号，云上返回：{str(got)[:200]}")
    return task


def poll(*, fs_id: str, region: str, task_id: str, creds, transport=None) -> dict:
    """查一次进度。返回 `{status, bytes, objects, error, done, failed}`。"""
    try:
        got = volcano.call(
            SERVICE,
            VERSION,
            "DescribeDataFlowTasks",
            creds=creds,
            region=region,
            # **两处都是真机踩出来的，别按直觉改**：
            #   · `DataFlowTaskIds` 是**字符串**，不是数组 —— 传数组回 InvalidParameter
            #   · **必须带分页**。不传的话云上返回 `TotalCount > 0` 但列表是空的，
            #     于是永远拿不到任务、status 恒为空 —— 单子永远卡在「在途」，
            #     不报错、不失败，连「卡住」都不报
            body={
                "FileSystemId": fs_id,
                "DataFlowTaskIds": str(task_id),
                "PageNumber": 1,
                "PageSize": 100,
            },
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
    why = str(_deep(got, "ErrorMsg", "ErrorMessage", "Message") or "")
    if is_failed(status) and not why:
        # 前置没配好时任务会建出来但直接失败，而云上不一定给原因。
        # 说一句比留空强 —— 留空的话人只看到「失败」两个字
        why = "失败原因云上没给。先确认 vePFS 与 TOS 同地域、已开服务访问授权、数据流动带宽 > 0"
    return {
        "status": status,
        "bytes": _int(got, "ExecSize", "BytesDone"),
        "objects": _int(got, "ExecCount", "FilesDone"),
        "error": why,
        "done": is_done(status),
        "failed": is_failed(status),
    }


def direction(src_scheme: str, dest_scheme: str) -> Optional[str]:
    """这对地址走预热还是沉降；不是这条链就返回 None。"""
    if src_scheme == "tos" and dest_scheme == "vepfs":
        return ACTION_IMPORT
    if src_scheme == "vepfs" and dest_scheme == "tos":
        return ACTION_EXPORT
    return None
