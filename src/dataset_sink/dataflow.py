"""CPFS 数据流动：OSS 与 CPFS 之间的预热（Import）与沉淀（Export）。

**这是 OSS 与 CPFS 之间搬字节的正规通道，不是优化。**

在此之前 `materialize` 自己拿 boto3 从 lakeFS S3 Gateway 逐个文件读、写进 CPFS，
`archive` 自己逐个上传——都是在应用层做平台能在服务端并行做的事。代价不只是慢：
它要求执行者必须挂载 CPFS，于是数据集发布流水线被迫跑在能进 VPC 的自托管 runner 上。

用数据流动之后，字节搬运只需要能调 `nas` API：

    预热 Import   OSS 前缀 → CPFS 路径
    沉淀 Export   CPFS 路径 → OSS 前缀
    Evict        释放 CPFS 上的数据块，保留元数据（见 reclaim.py）

**预热和沉淀都是复制，不删源。** Import 之后 OSS 那份还在，Export 之后 CPFS
那份也还在。所以：

    沉淀**不释放 CPFS 空间**——它只是在 OSS 多存一份。
    要腾出 CPFS 容量，得在沉淀之后再 Evict。

这一点值得单独写出来，因为「沉淀」这个词容易让人以为数据被移走了。正确的配对是
Export → Evict：先确保源存储里有一份，再释放本地数据块。单独 Export 只增不减，
单独 Evict 则要求数据本来就在源存储里。

**为什么按路径拉得动我们的数据。** 一般 lakeFS 的对象存在 blockstore 里是哈希
地址（`.../data/<partition>/<random-id>`），按路径根本拉不出 release 布局。但本
项目建 Commit 一律走**零拷贝 import**——对象从没被搬进 lakeFS 自己的命名空间，
物理地址就是原始的可读前缀。所以：

    存量数据      物理地址 = 原 OSS 前缀
    cpfs-ingest   物理地址 = archive 写入的前缀

两者都是可读路径，数据流动都能按路径处理。这个前缀记在 Commit metadata 的
`object_store_uri` 里。

**建 DataFlow 的四条前提**（2026-08-03 在真实 CPFS 2.0 上逐条撞出来的）：

1. **必须挂在 Fileset 上。** 不给 `FsetId` 直接报 `FsetId is mandatory`。
   所以 CPFS 上的数据集根目录要先建成 Fileset，这是 Terraform 该管的东西。
2. **`Throughput` 必填**，且只接受 600 / 1200 / 1500（MB/s），并且要小于文件
   系统本身的 I/O 吞吐。
3. **OSS 桶必须打上 `cpfs-dataflow` 标签**，否则报
   `The operation is denied. The OSS Bucket tag cpfs-dataflow is missing.`
   这是个显式的准入开关——没有它，CPFS 碰不了这个桶。归档桶的这个标签也该
   由 Terraform 管，否则第一次预热会在很后面才失败。
4. 协议服务、Fileset、文件系统都要处于就绪状态；任一还在创建中，
   `CreateDataFlow` 会报 `OperationDenied.InvalidState`——**而这个报错会盖住
   真正的原因**（缺标签、缺 Throughput 都可能先表现为它），排查时要逐个参数
   剥离才能看到真话。

跑任务时还有两条：

5. **Export 要求源 OSS 桶开启版本控制**，否则报
   `InvalidSourceStorage.NeedVersioning`。Import 不要求。所以归档桶必须开版本
   控制，否则沉淀这条路根本用不了——这也和「被 import 引用的前缀是只读区」
   的兜底措施是同一件事，一举两得。
6. **同一个 DataFlow 上的任务是串行的。** 前一个还在跑时提交下一个会报
   `The operation is not permitted when the status is processing`。所以预热和
   沉淀不能并发，编排时要等前一个到终态。

**数据流动不做校验。** 它只保证字节到位，不保证内容和 manifest 一致。所以预热
之后仍然要走 `certify`：全量比对文件集合、大小、SHA-256，通过了才 rename 发布。
换句话说数据流动替掉的是「搬」，替不掉「验」和「封」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .errors import DatasetSinkError

# 终态：任务不会再变了
_TERMINAL = frozenset({"Completed", "Failed", "Canceled", "Cancelled", "Stopped"})
_SUCCESS = frozenset({"Completed"})


@dataclass(frozen=True)
class DataFlowTask:
    task_id: str
    dataflow_id: str
    action: str
    status: str
    detail: Dict[str, object]

    @property
    def succeeded(self) -> bool:
        return self.status in _SUCCESS


class CpfsDataFlow:
    """一个 CPFS 文件系统上的数据流动操作入口。

    `runner` 可注入，所以单元测试不需要真实 CPFS——和 `aliyun_cli.py`
    以及 `reclaim.CpfsEvictStrategy` 是同一个模式。
    """

    def __init__(
        self,
        *,
        filesystem_id: str,
        region: str,
        mount_prefix: str = "",
        runner: Optional[Callable] = None,
        cli_path: str = "aliyun",
        profile: Optional[str] = None,
    ) -> None:
        self.filesystem_id = filesystem_id
        self.region = region
        self.mount_prefix = mount_prefix.rstrip("/")
        self.cli_path = cli_path
        self.profile = profile
        self._runner = runner
        self._dataflows: Optional[List[dict]] = None

    # -- 坐标换算 ---------------------------------------------------------

    def filesystem_path(self, mount_path: str) -> str:
        """挂载视角路径 → 文件系统内部路径。

        这两个是不同坐标系。和 `pai-request` 的 `release_dir` / `--filesystem-path`
        是同一个坑，而那个坑在文档里被列为接入期最常见的错误。填错的后果是
        操作作用到**错误的目录**上，所以这里宁可报错也不猜。
        """
        path = str(mount_path)
        if self.mount_prefix and not path.startswith(self.mount_prefix):
            raise DatasetSinkError(
                f"路径 {path} 不在挂载点 {self.mount_prefix} 下面。"
                "数据流动 API 要的是文件系统内部视角，靠 mount_prefix 换算。"
            )
        inner = path[len(self.mount_prefix) :] if self.mount_prefix else path
        if not inner.startswith("/"):
            inner = "/" + inner
        return inner

    # -- 与阿里云交互 -----------------------------------------------------

    def _run(self, args: Sequence[str]) -> dict:
        from .aliyun_cli import CommandResult, _parse_result, _subprocess_runner

        runner = self._runner or _subprocess_runner
        command = [self.cli_path, "--region", self.region]
        if self.profile:
            command.extend(["--profile", self.profile])
        command.extend(args)
        result = runner(command)
        if not isinstance(result, CommandResult):  # pragma: no cover - 仅防御
            raise DatasetSinkError("runner 必须返回 CommandResult")
        return _parse_result(result, args[1] if len(args) > 1 else args[0])

    def list_dataflows(self, refresh: bool = False) -> List[dict]:
        if self._dataflows is None or refresh:
            payload = self._run(["nas", "DescribeDataFlows", "--FileSystemId", self.filesystem_id])
            info = payload.get("DataFlowInfo", {}) if isinstance(payload, dict) else {}
            self._dataflows = info.get("DataFlow", []) if isinstance(info, dict) else []
        return self._dataflows

    def dataflow_for(self, inner_path: str) -> str:
        """找到覆盖这个文件系统内部路径的 DataFlow，取最长匹配。

        必须按**路径段边界**判断而不是字符串前缀：`/datasets-old` 不覆盖
        `/datasets/xxx`，只比前缀会选中错误的 DataFlow，从而把操作作用到
        另一个 OSS 桶上。
        """
        best_id = None
        best_len = -1
        for flow in self.list_dataflows():
            fs_path = str(flow.get("FileSystemPath") or "/").rstrip("/") or "/"
            boundary = fs_path if fs_path.endswith("/") else fs_path + "/"
            if (inner_path == fs_path or inner_path.startswith(boundary)) and len(
                fs_path
            ) > best_len:
                best_id, best_len = flow.get("DataFlowId"), len(fs_path)
        if not best_id:
            raise DatasetSinkError(
                f"{inner_path} 不在任何数据流动的范围内。\n"
                "预热/沉淀都需要该路径先由一个绑定了 OSS 的 DataFlow 管理。用 "
                f"`aliyun nas DescribeDataFlows --FileSystemId {self.filesystem_id}` 确认。"
            )
        return str(best_id)

    def _flow_for(self, inner_path: str) -> Tuple[str, str]:
        """返回覆盖该路径的 (dataflow_id, FileSystemPath)。

        提交任务只需要这两样。**刻意不要求 SourceStorage**：真实 API 不一定回
        `SourceStoragePath`，而提交任务并不关心源在哪——把这个依赖塞进来会让
        `submit()` 在完全正常的 DataFlow 上报「没有可用的源存储路径」。
        """
        best = None
        best_len = -1
        for flow in self.list_dataflows():
            fs_path = str(flow.get("FileSystemPath") or "/").rstrip("/") or "/"
            boundary = fs_path if fs_path.endswith("/") else fs_path + "/"
            if (inner_path == fs_path or inner_path.startswith(boundary)) and len(
                fs_path
            ) > best_len:
                best, best_len = flow, len(fs_path)
        if best is None:
            raise DatasetSinkError(
                f"{inner_path} 不在任何数据流动的范围内。\n"
                "预热/沉淀都需要该路径先由一个绑定了 OSS 的 DataFlow 管理。用 "
                f"`aliyun nas DescribeDataFlows --FileSystemId {self.filesystem_id}` 确认。"
            )
        fs_path = str(best.get("FileSystemPath") or "/").rstrip("/") or "/"
        return str(best.get("DataFlowId")), fs_path

    def binding_for(self, inner_path: str) -> Tuple[str, str, str]:
        """返回覆盖该路径的 (dataflow_id, FileSystemPath, SourceStoragePath)。"""
        flow_id, fs_path = self._flow_for(inner_path)
        best = next(f for f in self.list_dataflows() if str(f.get("DataFlowId")) == flow_id)
        # SourceStoragePath 可能是完整 URI，也可能只给桶级 SourceStorage。
        source = str(best.get("SourceStoragePath") or best.get("SourceStorage") or "").rstrip("/")
        if not source:
            raise DatasetSinkError(f"数据流动 {best.get('DataFlowId')} 没有可用的源存储路径")
        return flow_id, fs_path, source

    def object_uri_for(self, inner_path: str) -> str:
        """CPFS 内部路径 → 它在 OSS 上对应的 URI。

        DataFlow 把 `FileSystemPath`(F) 和 `SourceStoragePath`(S) 死绑在一起：
        路径 D 对应的对象前缀就是 `S + (D - F)`。**这意味着 OSS 的前缀布局必须
        镜像 CPFS 的路径布局**，不能各按各的来——这是接入数据流动时最容易忽略
        的约束，也是 `archive --via dataflow` 不能再接受任意 `--prefix` 的原因。

        算出这个 URI 之后就能喂给 `commit --object-store-uri`，让 lakeFS 从
        同一个位置零拷贝 import。

        **一个无法在运行时校验的前提：DataFlow 必须绑在桶根上，不能设
        `SourceStoragePath`。**

        2026-08-03 在真实 CPFS 2.0 上实测：`DescribeDataFlows` 的响应里**根本
        没有 `SourceStoragePath` 字段**——创建时传了 `/cpfs-verify`，回读时看不到。
        所以如果有人建 DataFlow 时设了它，这里算出来的 URI 会**静默少掉那一段**，
        而 `commit --object-store-uri` 就会指向一个错误的 OSS 前缀。

        我们自己的 `cpfs-workspaces` 模块只设 `source_storage = oss://<bucket>`，
        不设 path，所以这个计算对我们创建的 DataFlow 是对的。但对手工创建的
        DataFlow 无法保证，而且**我们没有办法检测**（字段读不回来）。
        接手别人建好的 DataFlow 时必须人工确认这一点。
        """
        _, fs_path, source = self.binding_for(inner_path)
        relative = inner_path[len(fs_path) :] if fs_path != "/" else inner_path
        relative = relative.strip("/")
        uri = f"{source}/{relative}" if relative else source
        # lakeFS import 要求 URI 指向前缀，末尾斜杠有意义。
        return uri.rstrip("/") + "/"

    # -- 任务 -------------------------------------------------------------

    def submit(
        self,
        *,
        action: str,
        directory: str,
        data_type: str = "MetaAndData",
        dataflow_id: Optional[str] = None,
    ) -> DataFlowTask:
        """提交一个数据流动任务。`directory` 是文件系统内部路径（绝对）。"""
        if action not in {"Import", "Export", "Evict", "StreamImport", "StreamExport"}:
            raise ValueError(f"不支持的 TaskAction: {action}")
        inner = directory if directory.startswith("/") else "/" + directory
        # Directory 要求首尾都是斜杠。
        if not inner.endswith("/"):
            inner += "/"
        flow_id, fs_path = self._flow_for(inner.rstrip("/") or "/")
        if dataflow_id:
            flow_id = dataflow_id

        # --------------------------------------------------------------------
        # **`Directory` 是相对 DataFlow 的 FileSystemPath 的路径，不是绝对
        # 文件系统内部路径。** 这是第三个坐标系。
        #
        # 2026-08-03 在真实 CPFS 2.0 上对照实测（FileSystemPath = /verify/）：
        #
        #     Directory=/verify/imp-test/   →  Failed，progress 0
        #     Directory=/imp-test/          →  Completed，progress 100
        #
        # 传绝对路径**不会报参数错误**，任务会被正常受理、几秒后变成 Failed，
        # 而且 `ProgressStats` 是空的、没有任何 ErrorMessage 字段——纯静默失败。
        # 这也是为什么这个 bug 一直没被发现：单元测试注入的 runner 只检查我们
        # 发出了什么，不知道服务端会怎么解释它。
        # --------------------------------------------------------------------
        base = fs_path.rstrip("/")
        relative = inner[len(base) :] if base and inner.startswith(base) else inner
        if not relative.startswith("/"):
            relative = "/" + relative
        if not relative.endswith("/"):
            relative += "/"

        payload = self._run(
            [
                "nas",
                "CreateDataFlowTask",
                "--FileSystemId",
                self.filesystem_id,
                "--DataFlowId",
                flow_id,
                "--TaskAction",
                action,
                "--DataType",
                data_type,
                "--Directory",
                relative,
            ]
        )
        task_id = payload.get("TaskId") if isinstance(payload, dict) else None
        if not task_id:
            raise DatasetSinkError(f"{action} 任务已提交但没有返回 TaskId: {payload}")
        return DataFlowTask(
            task_id=str(task_id),
            dataflow_id=flow_id,
            action=action,
            status="Pending",
            detail={},
        )

    def describe(self, task_id: str) -> DataFlowTask:
        payload = self._run(
            [
                "nas",
                "DescribeDataFlowTasks",
                "--FileSystemId",
                self.filesystem_id,
                "--Filters.1.Key",
                "TaskIds",
                "--Filters.1.Value",
                task_id,
            ]
        )
        # 真实响应是 TaskInfo.Task[]（2026-08-03 实测）。
        info = payload.get("TaskInfo", {}) if isinstance(payload, dict) else {}
        tasks = info.get("Task", []) if isinstance(info, dict) else []
        for task in tasks:
            if str(task.get("TaskId")) == task_id:
                return DataFlowTask(
                    task_id=task_id,
                    dataflow_id=str(task.get("DataFlowId") or ""),
                    action=str(task.get("TaskAction") or ""),
                    status=str(task.get("Status") or "Unknown"),
                    detail=task,
                )
        raise DatasetSinkError(f"查不到数据流动任务 {task_id}")

    def wait(
        self,
        task_id: str,
        *,
        timeout_seconds: int = 7200,
        poll_seconds: int = 20,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> DataFlowTask:
        """轮询到终态。

        **超时不当成成功。** 搬运没完成就去 certify，会因为文件缺失或大小对不上
        而失败——那是好的，但报错发生在离原因很远的地方。所以这里超时直接抛错。
        """
        deadline = now() + timeout_seconds
        while True:
            task = self.describe(task_id)
            if task.status in _TERMINAL:
                if not task.succeeded:
                    raise DatasetSinkError(
                        f"数据流动任务 {task_id} 以 {task.status} 结束: {task.detail}"
                    )
                return task
            if now() >= deadline:
                raise DatasetSinkError(
                    f"数据流动任务 {task_id} 在 {timeout_seconds}s 内没有结束"
                    f"（当前 {task.status}）。它可能仍在跑，用 "
                    f"`aliyun nas DescribeDataFlowTasks --FileSystemId {self.filesystem_id}` 查。"
                )
            sleep(poll_seconds)

    # -- 两个入口 ---------------------------------------------------------

    def prefetch(self, mount_dir: str, **kw) -> DataFlowTask:
        """预热：把 OSS 上对应前缀的数据拉进 CPFS 这个目录。"""
        return self.submit(action="Import", directory=self.filesystem_path(mount_dir), **kw)

    def flush(self, mount_dir: str, **kw) -> DataFlowTask:
        """沉淀：把 CPFS 这个目录的数据推回 OSS。"""
        return self.submit(action="Export", directory=self.filesystem_path(mount_dir), **kw)
