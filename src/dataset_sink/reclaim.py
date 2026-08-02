"""回收 CPFS 上不再需要的 release，把容量还回来。

CPFS release 只增不减：每发一个版本就占一份全量空间，永远不释放。CPFS 容量有限
且按容量计费，写满之后 `materialize` 直接失败——那时既发不了新版本，也不敢乱删，
因为没有任何东西告诉你哪个 release 正在被训练任务挂载。

回收之所以是**安全**的，前提只有一条：

    删掉的 release 必须能重建。

而重建依赖对象存储上的归档：release 目录名是 Commit ID，Commit 指向对象存储里的
字节，所以只要 Commit 还在，随时可以重新 materialize 回来。**这条前提不成立时
一律不删**——本模块宁可漏删，不可错删。

删除本身用「先原子改名进 .trash，再慢慢 rmtree」：

    rename 是原子的元数据操作，release 在一瞬间从数据集命名空间里消失，
    不存在「删了一半的 release」被消费方看到的窗口。rmtree 中途挂掉也只是
    在 .trash 里留下残骸，下次 sweep_trash 接着扫。

顺序上，所有便宜的本地判断（pin / 保护期 / 保留最近 N 个）都排在需要网络的判断
（是否在用 / 是否可重建）前面，避免为一个注定要保留的 release 白跑一次远程调用。
"""

from __future__ import annotations

import fcntl
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from .errors import DatasetSinkError

# 发布协议自己占用的目录，不是数据集
_RESERVED_DIRS = frozenset({".locks", ".materializing", ".trash"})

# 人工置顶标记：放一个这个文件，回收永远不碰这个 release
KEEP_MARKER = ".keep"

TRASH_DIR = ".trash"


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseInfo:
    dataset: str
    commit_id: str
    path: Path
    size_bytes: int
    file_count: int
    created_at: Optional[datetime]
    manifest_sha256: Optional[str]
    repository: Optional[str]
    pinned: bool
    ready: bool

    @property
    def label(self) -> str:
        return f"{self.dataset}/{self.commit_id}"


def scan_releases(target_root: Path) -> Tuple[ReleaseInfo, ...]:
    """盘点 `<target_root>/<dataset>/<commit>/` 下所有 release。

    体量取 `release.json` 里记录的 `size_bytes` 而不是 du：TB 级目录 du 一遍
    本身就要几分钟，而这个值在发布时已经算准并固化了。
    """
    root = Path(target_root).resolve()
    if not root.is_dir():
        raise DatasetSinkError(f"target_root 不存在或不是目录: {target_root}")

    found: List[ReleaseInfo] = []
    for dataset_dir in sorted(root.iterdir()):
        if not dataset_dir.is_dir() or dataset_dir.name in _RESERVED_DIRS:
            continue
        if dataset_dir.is_symlink():
            continue
        for release_dir in sorted(dataset_dir.iterdir()):
            if not release_dir.is_dir() or release_dir.is_symlink():
                continue
            found.append(_inspect(dataset_dir.name, release_dir))
    return tuple(found)


def _inspect(dataset: str, path: Path) -> ReleaseInfo:
    ready = (path / "_READY").is_file()
    meta: Dict[str, object] = {}
    release_file = path / "release.json"
    if release_file.is_file():
        try:
            loaded = json.loads(release_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (json.JSONDecodeError, OSError):
            # 读不出来就当作没有元数据：它会因为「不可重建」而被保留，
            # 这正是我们想要的保守行为。
            meta = {}

    return ReleaseInfo(
        dataset=dataset,
        commit_id=path.name,
        path=path,
        size_bytes=int(meta.get("size_bytes") or 0),
        file_count=int(meta.get("file_count") or 0),
        created_at=_parse_time(meta.get("created_at")) or _mtime(path),
        manifest_sha256=_str_or_none(meta.get("manifest_sha256")),
        repository=_str_or_none(meta.get("repository")),
        pinned=(path / KEEP_MARKER).exists(),
        ready=ready,
    )


def _str_or_none(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 没带时区的一律当 UTC，否则后面和 aware 的 now 相减会抛 TypeError。
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _mtime(path: Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 两个可注入的判断：还在不在用、删了能不能重建
# ---------------------------------------------------------------------------


class UsageProbe(Protocol):
    """判断 release 是否正在被使用。返回原因字符串表示「在用」，None 表示「没在用」。"""

    def in_use(self, release: ReleaseInfo) -> Optional[str]: ...


class NoUsageProbe:
    """不做占用检查。

    此时**保护期是唯一挡在回收和运行中训练之间的东西**，所以 min_age_days
    不能设小。接了真实的占用探测之后才谈得上缩短保护期。
    """

    def in_use(self, release: ReleaseInfo) -> Optional[str]:
        del release
        return None


class RecoverabilityProbe(Protocol):
    """判断 release 删掉之后能否重建。返回 (能否重建, 原因)。"""

    def recoverable(self, release: ReleaseInfo) -> Tuple[bool, str]: ...


class LakeFSCommitProbe:
    """Commit 还在 lakeFS 里 ⟹ 字节还在对象存储里 ⟹ 可以重新 materialize。

    这是本项目发布协议自带的推论：任何 release 的 Commit 都是先把字节放到
    对象存储、再 import 出来的，所以 Commit 存在就意味着字节有个持久落点。
    """

    def __init__(self, client_factory) -> None:
        self._factory = client_factory

    def recoverable(self, release: ReleaseInfo) -> Tuple[bool, str]:
        if not release.repository:
            return False, "release.json 里没有 repository，无法确认 Commit 是否还在"
        try:
            exists = self._factory(release.repository, release.commit_id)
        except Exception as exc:  # noqa: BLE001 - 查不动就当作不可重建
            return False, f"查询 lakeFS 失败: {exc}"
        if exists:
            return True, "lakeFS 上仍有该 Commit"
        return False, "lakeFS 上找不到该 Commit，删了就再也拿不回来"


class AssumeRecoverable:
    """跳过可重建性检查。

    只有在你**另有依据**确认归档还在时才用（例如刚刚人工核对过对象存储）。
    这是整个回收流程里唯一能造成不可逆数据丢失的开关。
    """

    def recoverable(self, release: ReleaseInfo) -> Tuple[bool, str]:
        del release
        return True, "已由调用方声明可重建（--assume-recoverable）"


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    release: ReleaseInfo
    reason: str


@dataclass(frozen=True)
class ReclaimPlan:
    reclaim: Tuple[Decision, ...]
    retain: Tuple[Decision, ...]
    reclaimable_bytes: int


def plan_reclaim(
    releases: Sequence[ReleaseInfo],
    *,
    now: datetime,
    min_age_days: int = 14,
    keep_last: int = 2,
    reclaim_bytes: Optional[int] = None,
    include_incomplete: bool = False,
    usage_probe: Optional[UsageProbe] = None,
    recoverability_probe: Optional[RecoverabilityProbe] = None,
) -> ReclaimPlan:
    """决定哪些 release 可以回收。

    判断顺序是刻意的：先跑完全部本地判断，再对幸存者跑需要网络的判断。
    一个注定要保留的 release 不该消耗一次 lakeFS 查询。
    """
    if min_age_days < 0:
        raise ValueError("min_age_days 不能为负")
    if keep_last < 0:
        raise ValueError("keep_last 不能为负")

    usage_probe = usage_probe or NoUsageProbe()
    if recoverability_probe is None:
        raise ValueError("必须提供 recoverability_probe：不确认能否重建就删除是不可逆的数据丢失")

    cutoff = now - timedelta(days=min_age_days)

    # 每个数据集里最新的 keep_last 个一律保留，保证不会把一个数据集清空。
    newest: Dict[str, set] = {}
    by_dataset: Dict[str, List[ReleaseInfo]] = {}
    for release in releases:
        by_dataset.setdefault(release.dataset, []).append(release)
    for dataset, items in by_dataset.items():
        ordered = sorted(
            items,
            key=lambda r: r.created_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        newest[dataset] = {r.path for r in ordered[:keep_last]}

    retain: List[Decision] = []
    survivors: List[ReleaseInfo] = []

    for release in releases:
        if release.pinned:
            retain.append(Decision(release, f"人工置顶（存在 {KEEP_MARKER}）"))
        elif not release.ready and not include_incomplete:
            retain.append(
                Decision(release, "缺少 _READY，可能是发布中断的残骸；需 --include-incomplete")
            )
        elif release.path in newest.get(release.dataset, set()):
            retain.append(Decision(release, f"是该数据集最近 {keep_last} 个版本之一"))
        elif release.created_at is None:
            retain.append(Decision(release, "无法确定发布时间，不敢按保护期判断"))
        elif release.created_at > cutoff:
            age = (now - release.created_at).days
            retain.append(Decision(release, f"发布仅 {age} 天，未过 {min_age_days} 天保护期"))
        else:
            survivors.append(release)

    candidates: List[Decision] = []
    for release in survivors:
        used = usage_probe.in_use(release)
        if used:
            retain.append(Decision(release, f"正在使用中: {used}"))
            continue
        ok, why = recoverability_probe.recoverable(release)
        if not ok:
            retain.append(Decision(release, f"删了无法重建: {why}"))
            continue
        candidates.append(Decision(release, why))

    # 先回收最旧的：同样腾出空间，淘汰最不可能再被用到的那些。
    candidates.sort(key=lambda d: d.release.created_at or datetime.min.replace(tzinfo=timezone.utc))

    if reclaim_bytes is not None:
        selected: List[Decision] = []
        freed = 0
        for decision in candidates:
            if freed >= reclaim_bytes:
                retain.append(
                    Decision(decision.release, f"已达到目标回收量 {reclaim_bytes} 字节，本次不动")
                )
                continue
            selected.append(decision)
            freed += decision.release.size_bytes
        candidates = selected

    return ReclaimPlan(
        reclaim=tuple(candidates),
        retain=tuple(retain),
        reclaimable_bytes=sum(d.release.size_bytes for d in candidates),
    )


# ---------------------------------------------------------------------------
# 两种回收策略
#
# 它们不是同一件事的两种实现，语义差别很大：
#
#                     硬删                    CPFS Evict
#   release 目录      消失                    **还在**，元数据保留
#   PAI 版本          悬空，挂载会失败        **仍然有效**
#   再次训练          要重跑 materialize      **访问时按需从 OSS 加载**
#   最坏情况          数据不可用              一次冷读的性能损失
#
# 所以 Evict 的安全要求本来就该低得多——它释放的是缓存，不是数据。
# 但在真实环境验证过之前，默认仍按硬删的严格程度设闸门。
# ---------------------------------------------------------------------------


class ReclaimStrategy(Protocol):
    """怎么把一个 release 占的空间还回去。"""

    name: str

    def reclaim(self, release: ReleaseInfo) -> str:
        """执行回收，返回一句人可读的结果描述。"""
        ...


class HardDeleteStrategy:
    """先原子改名进 .trash，再慢慢 rmtree。

    rename 是原子的元数据操作，release 一瞬间从数据集命名空间里消失，
    **不存在「删了一半的 release」被消费方看到的窗口**——这是 `_READY`
    最后写入那条规则的反向应用。

    任何 POSIX 文件系统都能用，不依赖 CPFS 特性。代价是删掉之后
    PAI Dataset Version 会指向一个不存在的路径。
    """

    name = "hard-delete"

    def __init__(self, target_root: Path) -> None:
        self.root = Path(target_root).resolve()

    def reclaim(self, release: ReleaseInfo) -> str:
        trash_dir = self.root / TRASH_DIR / release.dataset
        trash_dir.mkdir(parents=True, exist_ok=True)
        grave = _unique_grave(trash_dir, release.commit_id)
        release.path.rename(grave)
        # rmtree 放在锁外由调用方做：它可能很慢，而此时目录已经不在
        # 数据集命名空间里，继续占着锁只会挡住同一个 Commit 的重新发布。
        self._pending = grave
        return f"已移入 {TRASH_DIR}，待清理"

    def finalize(self) -> None:
        grave = getattr(self, "_pending", None)
        if grave is not None:
            shutil.rmtree(grave, ignore_errors=True)
            self._pending = None


class CpfsEvictStrategy:
    """用 CPFS 数据流动的 Evict 释放数据块，保留元数据。

    Evict 之后文件仍可列出、`release.json` 与 `_READY` 仍可读，访问数据时
    CPFS 按需从绑定的 OSS 源存储加载。所以 **PAI Dataset Version 不会失效**，
    也不需要重跑 materialize——这是它比硬删好的地方。

    四个前提，任一不满足都会失败而不是降级：

    1. **数据必须已经在源存储里。** Evict 只释放本地数据块，它不负责把数据送去
       OSS——预热和沉淀都是「复制」，Evict 是唯一真正释放空间的动作，三者不能
       互相替代。所以对 `certify` 直接在 CPFS 上发布出来的 release，Evict 之前
       必须确认 OSS 上确实有对应的一份（本项目里由 `archive` 保证）。落在
       DataFlow 范围内是**必要条件而非充分条件**——路径被管理不等于这批文件
       已经同步过去了。
    2. 该 release 的路径必须落在某个已建好的**数据流动（DataFlow）**下面。
       Evict 的含义是「把缓存还给源存储」，没有源存储就无从谈起。
    3. 文件系统必须支持 Evict。官方文档明写灵骏 BMCPFS 只支持
       Import/Export/StreamImport/StreamExport，**不支持 Evict**。
    4. 传给 API 的是**文件系统内部路径**，不是挂载路径。这两者是不同坐标系，
       和 `pai-request` 的 `release_dir` / `--filesystem-path` 是同一个坑。
    """

    name = "cpfs-evict"

    def __init__(
        self,
        *,
        filesystem_id: str,
        region: str,
        mount_prefix: str,
        runner=None,
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

    # -- 路径换算 ---------------------------------------------------------

    def filesystem_path(self, release: ReleaseInfo) -> str:
        """挂载视角路径 → 文件系统内部路径。"""
        mount = str(release.path)
        if self.mount_prefix and not mount.startswith(self.mount_prefix):
            raise DatasetSinkError(
                f"release 路径 {mount} 不在挂载点 {self.mount_prefix} 下面。"
                "reclaim 用的是挂载视角，而 CPFS API 要的是文件系统内部视角，"
                "两者靠 --cpfs-mount-prefix 换算，填错会作用到错误的目录上。"
            )
        inner = mount[len(self.mount_prefix) :] if self.mount_prefix else mount
        return inner if inner.startswith("/") else "/" + inner

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

    def _load_dataflows(self) -> List[dict]:
        if self._dataflows is None:
            payload = self._run(["nas", "DescribeDataFlows", "--FileSystemId", self.filesystem_id])
            flows = payload.get("DataFlowInfo", {}) if isinstance(payload, dict) else {}
            self._dataflows = flows.get("DataFlow", []) if isinstance(flows, dict) else []
        return self._dataflows

    def _dataflow_for(self, inner_path: str) -> str:
        """找到覆盖这个路径的 DataFlow。

        找不到就报错而不是退化成硬删：两者语义不同，静默切换会让操作者
        以为「只是释放了缓存」，实际上目录已经被删掉了。
        """
        best_id = None
        best_len = -1
        for flow in self._load_dataflows():
            fs_path = str(flow.get("FileSystemPath") or "/").rstrip("/") or "/"
            covered = inner_path == fs_path or inner_path.startswith(
                fs_path if fs_path.endswith("/") else fs_path + "/"
            )
            if covered and len(fs_path) > best_len:
                best_id, best_len = flow.get("DataFlowId"), len(fs_path)
        if not best_id:
            raise DatasetSinkError(
                f"{inner_path} 不在任何数据流动的范围内，无法 Evict。\n"
                "Evict 的含义是把缓存还给源存储，所以该路径必须先由一个绑定了 OSS "
                "的 DataFlow 管理。用 `aliyun nas DescribeDataFlows --FileSystemId "
                f"{self.filesystem_id}` 确认，或改用 --strategy hard-delete。"
            )
        return str(best_id)

    def reclaim(self, release: ReleaseInfo) -> str:
        inner = self.filesystem_path(release)
        flow_id = self._dataflow_for(inner)
        payload = self._run(
            [
                "nas",
                "CreateDataFlowTask",
                "--FileSystemId",
                self.filesystem_id,
                "--DataFlowId",
                flow_id,
                "--TaskAction",
                "Evict",
                # 只释放数据块，保留元数据——这正是 Evict 相对硬删的价值。
                "--DataType",
                "Data",
                # Directory 要求首尾都是斜杠。
                "--Directory",
                inner if inner.endswith("/") else inner + "/",
            ]
        )
        task_id = payload.get("TaskId") if isinstance(payload, dict) else None
        return f"已提交 Evict 任务 {task_id or '(无 TaskId)'}（DataFlow {flow_id}）"


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReclaimResult:
    reclaimed: Tuple[Tuple[str, str], ...]
    skipped: Tuple[Tuple[str, str], ...]
    freed_bytes: int
    executed: bool
    strategy: str


def execute_plan(
    plan: ReclaimPlan,
    target_root: Path,
    *,
    execute: bool = False,
    strategy: Optional[ReclaimStrategy] = None,
) -> ReclaimResult:
    """执行回收计划。默认 dry-run，`execute=True` 才真动手。

    每个 release 在处理前会拿**和 materialize / certify 同一把锁**，并在锁内
    重新核对前置条件。否则可能出现「计划生成时该目录可回收，执行时另一个进程
    正好在往同一个 Commit 发布」。
    """
    root = Path(target_root).resolve()
    strategy = strategy or HardDeleteStrategy(root)

    reclaimed: List[Tuple[str, str]] = []
    skipped: List[Tuple[str, str]] = []
    freed = 0

    for decision in plan.reclaim:
        release = decision.release
        if not execute:
            reclaimed.append((release.label, f"[dry-run] 将用 {strategy.name} 回收"))
            freed += release.size_bytes
            continue

        lock_dir = root / ".locks" / release.dataset
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"{release.commit_id}.lock"

        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            # 锁内重新确认：计划是在锁外生成的，期间什么都可能发生。
            if not release.path.is_dir():
                skipped.append((release.label, "已经不存在"))
                continue
            if (release.path / KEEP_MARKER).exists():
                skipped.append((release.label, f"执行前被打上了 {KEEP_MARKER}"))
                continue

            try:
                note = strategy.reclaim(release)
            except DatasetSinkError as exc:
                skipped.append((release.label, str(exc)))
                continue

        # 慢活儿放锁外（硬删的 rmtree）：此时目录已经不在命名空间里了。
        finalize = getattr(strategy, "finalize", None)
        if finalize is not None:
            finalize()

        reclaimed.append((release.label, note))
        freed += release.size_bytes

    return ReclaimResult(
        reclaimed=tuple(reclaimed),
        skipped=tuple(skipped),
        freed_bytes=freed,
        executed=execute,
        strategy=strategy.name,
    )


def _unique_grave(trash_dir: Path, commit_id: str) -> Path:
    """在 .trash 下取一个没被占用的名字。

    同名可能来自上一次中断的回收残骸——那时不能覆盖，否则会把上次没删完的
    目录和这次的混在一起，rmtree 报错后两边都留下半残状态。
    """
    candidate = trash_dir / commit_id
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = trash_dir / f"{commit_id}.{suffix}"
    return candidate


def sweep_trash(target_root: Path, *, execute: bool = False) -> Tuple[int, int]:
    """清掉 .trash 里的残骸，返回 (目录数, 是否真删)。

    rmtree 中途被杀会在 .trash 里留下东西。它们已经不在数据集命名空间里，
    不影响正确性，但仍占着容量，所以每次回收都顺手扫一遍。
    """
    trash_root = Path(target_root).resolve() / TRASH_DIR
    if not trash_root.is_dir():
        return 0, 0

    graves = [p for d in sorted(trash_root.iterdir()) if d.is_dir() for p in sorted(d.iterdir())]
    if execute:
        for grave in graves:
            shutil.rmtree(grave, ignore_errors=True)
    return len(graves), int(execute)
