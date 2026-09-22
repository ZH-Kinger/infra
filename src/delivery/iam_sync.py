"""属性表同步流程：名册 → 增量 CSV → 存档 → IT 确认 → 新基线。

CLI（`delivery identity iam-export`）和面板（管理后台「IAM 属性表」）共用这里，规则只写一份：
比对规则在 `iam_export.py`，本模块只管「读名册、算这一次要发什么、写盘、存档、确认」。

写盘一律走 `cli._require_identity_dir` + 原子 0600 写：属性表含全员邮箱与云用户名，
只允许落在仓库根的 identity/（整体 gitignore）。守卫留在 cli.py 里没有搬走——
那套 git 判定有测试直接打它的桩，搬走会让打桩失效；这里按调用时惰性导入，
方向是 iam_sync → cli，服务端不会因此在导入期依赖 CLI。
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import iam_export
from .errors import DeliveryError

#: 发给 IT 的属性表存档目录（identity/ 整体 gitignore）
SENT_DIR = "identity/iam-sent"
CSV_HEADER = [
    "feishu_union_id",
    "email",
    "name",
    "app",
    "value",
    "action",
    "match_by",
    "problem",
]
#: 增量输出文件的默认位置
DEFAULT_OUT = "identity/iam-attributes.csv"
#: 存档旁边那份「这一轮发给 IT 的增量」。存档是全量快照、按设计不含 remove，
#: 只能当基线；真正要发出去的是这一份。两份一起存，下载入口才能和存档一一对应，
#: 不必依赖「刚导出」那一瞬间的页面状态。
INCREMENT_SUFFIX = ".increment.csv"
INCREMENT_NAME = re.compile(r"^\d{8}-\d{6}(~\d+)?\.increment\.csv$")
DEFAULT_SPEC = "identity/iam-attributes.json"


@dataclass(frozen=True)
class SyncPaths:
    """一次属性表操作要用到的路径。sent_dir 之下还有 pending/（待 IT 确认的存档）。"""

    people: str
    attributes: str = DEFAULT_SPEC
    out: str = DEFAULT_OUT
    sent_dir: str = SENT_DIR

    @property
    def sent(self) -> Path:
        return Path(self.sent_dir)

    @property
    def pending_dir(self) -> Path:
        return self.sent / "pending"


@dataclass
class Increment:
    """这一次要发给 IT 的内容，以及算它用到的上下文（存档要写全量状态，得留着）。"""

    rows: list
    notes: list
    full: list
    base_rows: list = field(default_factory=list)
    baseline: Optional[Path] = None

    @property
    def counts(self) -> dict:
        return {a: sum(1 for r in self.rows if r["action"] == a) for a in ("set", "remove", "skip")}


# ── 写盘守卫（实现在 cli.py，见模块开头说明）────────────────────────────────


def _guard(target: Path) -> None:
    from .cli import _require_identity_dir

    _require_identity_dir(target)


def _write_private_json(path: Path, data: dict) -> None:
    from .cli import _atomic_private_write

    _guard(path.resolve())
    _atomic_private_write(path.resolve(), (json.dumps(data, ensure_ascii=False) + "\n").encode())


# ── 名册 → 属性表的行 ──────────────────────────────────────────────────────


def writer(spec_path):
    """建号后把登录名写进公司 IAM 的 `cloud_accounts`，返回写入前的旧值。

    **这一步不成，人就登不进去** —— SSO 靠这个属性把云账号和企业身份对上。

    抽出来共用，是因为它原先只写在 `server.py` 里：面板进程带着 `--iam-attributes`
    所以有它，而**定时任务构造 `Flows` 时压根没传**，于是审批一通过、sweep 顺手执行，
    这一步被静默跳过。线上第一个账号就是这么建出来一个登不进去的号的。
    两处各写一份的结局就是这样 —— 只有一处被想起来。

    没配 spec 路径返回 None，由 `Flows._write_iam_attr` 记成「他登不进去」。
    """
    if not spec_path:
        return None

    def write(union_id: str, platform: str, account: str, username: str) -> str:
        from . import iam_api

        scope = f"{platform}/{account}"
        spec = load_specs(spec_path).get(scope)
        if spec is None:
            raise DeliveryError(f"{spec_path} 里没有 {scope}，不知道这个云账号对应哪个 IAM 应用")
        _, suffix = spec
        # 后缀从配置取，**不在这里拼域名**：阿里云的完整 NameID 是
        # `<名>@<UID>.onaliyun.com`，而哪个账号用哪个域名（有的是别名、有的是辅助域名）
        # 只有那份配置知道。在这里再写一遍，两处迟早不一致，
        # 而不一致的表现是「SSO 登录报找不到用户」，查起来一点线索都没有
        got = iam_api.put_value(
            union_id, iam_api.app_of(scope), f"{username}{suffix}", cfg=iam_api.Config.from_env()
        )
        return str(got.get("previous") or "")

    return write


def load_specs(attributes: str, *, warn: Optional[Callable[[str], None]] = None) -> dict:
    """读云账号 → IAM 属性名的配置，返回 {scope: (应用标识, NameID 后缀)}。"""
    attr_file = Path(attributes)
    if not attr_file.exists():
        raise DeliveryError(
            f"缺 {attr_file}：写明每个云账号对应的 IAM 属性名，"
            '形如 {"aliyun/<UID>": "aliyun_username", "volcano/<UID>": "volcano_username"}'
        )
    try:
        attrs = json.loads(attr_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"读不了 {attr_file}：{exc}") from exc
    if not isinstance(attrs, dict) or not attrs:
        raise DeliveryError(f"{attr_file} 必须是非空对象")

    # 值可以是列名字符串，或 {"key": 应用标识, "suffix": NameID 后缀}。
    # 后缀写进导出值里（完整 NameID），IAM 侧表达式就不必按账号拼接域名。
    specs: dict = {}
    for scope, spec in attrs.items():
        if scope.startswith("_"):
            continue
        if isinstance(spec, str):
            specs[scope] = (spec, "")
        elif isinstance(spec, dict) and isinstance(spec.get("key"), str) and spec["key"]:
            suffix = spec.get("suffix") or ""
            if not isinstance(suffix, str):
                raise DeliveryError(f"{attr_file} 里 {scope} 的 suffix 必须是字符串")
            if suffix and (not suffix.startswith("@") or "@" in suffix[1:] or len(suffix) < 2):
                raise DeliveryError(f"{attr_file} 里 {scope} 的 suffix 必须形如 @域名：{suffix!r}")
            platform, _, account = scope.partition("/")
            expected = f"@{account}.onaliyun.com"
            if platform == "aliyun" and suffix and suffix != expected and warn is not None:
                warn(
                    f"  注意：{scope} 的 suffix 是 {suffix}，不是该账号默认域名 {expected}；"
                    "确认这是它的域名别名，否则 SSO 会找不到用户"
                )
            specs[scope] = (spec["key"], suffix)
        else:
            raise DeliveryError(f'{attr_file} 里 {scope} 的配置应为字符串或 {{"key": ...}}')
    columns = [key for key, _ in specs.values()]
    if len(set(columns)) != len(columns):
        raise DeliveryError(f"{attr_file} 里有两个云账号用了同一个应用标识，值会互相覆盖")
    return specs


def build_rows(index, specs: dict) -> list:
    """名册 → 属性表的行。一行 = 一个人在一个云账号应用上的一条属性。

    action：set 写入 cloud_accounts[app]=value；skip 不导入（problem 写原因）。
    属性值直接决定 SSO 进哪个号，有任何疑问一律 skip，宁可登录被拒也不能填错。
    """
    rows = []
    for person in index.people:
        if not person.accounts and not person.pending:
            continue
        match_by = "feishu_union_id" if person.union_id else "email（存量回填）"
        blocker = ""
        if person.union_id and index.is_blocked_uid(person.union_id):
            blocker = "名册里 union_id 重复，需管理员核对"
        elif not person.union_id and not person.email:
            blocker = "既没有 union_id 也没有邮箱，IAM 无法匹配"
        elif not person.union_id and person.email_collision:
            blocker = "通讯录里多人共用此邮箱，需管理员核对后补 union_id"
        elif not person.union_id and index.claim_blocked(person):
            blocker = "登录绑定与名册对不上，需管理员核对后补 union_id"

        def add(app, value, action, problem, person=person, match_by=match_by):
            rows.append(
                {
                    "feishu_union_id": person.union_id,
                    "email": person.email,
                    "name": person.name,
                    "app": app,
                    "value": value,
                    "action": action,
                    "match_by": match_by,
                    "problem": problem,
                }
            )

        by_scope: dict = {}
        for ref in person.accounts:
            by_scope.setdefault(ref.scope, []).append(ref.name)
        for scope, names in sorted(by_scope.items()):
            spec = specs.get(scope)
            if spec is None:
                add("", "", "skip", f"{scope} 未配置应用标识")
                continue
            app, suffix = spec
            if blocker:
                add(app, "", "skip", blocker)
            elif len(names) > 1:
                add(
                    app,
                    "",
                    "skip",
                    f"同一云账号下有多个号 {'/'.join(sorted(names))}，需先定保留哪个",
                )
            elif suffix and "@" in names[0]:
                add(app, "", "skip", f"用户名 {names[0]} 已含 @，不能再拼后缀")
            else:
                add(app, names[0] + suffix, "set", "")
        confirmed = {specs[sc][0] for sc in by_scope if sc in specs}
        for ref in person.pending:
            spec = specs.get(ref.scope)
            app = spec[0] if spec else ""
            if app in confirmed:
                continue
            add(app, "", "skip", "对应关系待确认，未导出")
    return rows


def full_state(paths: SyncPaths, *, warn: Optional[Callable[[str], None]] = None) -> tuple:
    """(全量行, specs)。全量行 = 名册此刻应有的状态，增量和存档都从它算。"""
    from . import people as people_mod

    bindings = Path(paths.people).with_name("bindings.json")
    index = people_mod.load(
        paths.people, bindings_path=str(bindings) if bindings.exists() else None
    )
    specs = load_specs(paths.attributes, warn=warn)
    return iam_export.sanitize(build_rows(index, specs)), specs


# ── CSV 读写 ───────────────────────────────────────────────────────────────


def read_csv(path: Path) -> list:
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != CSV_HEADER:
                raise DeliveryError(f"{path} 不是当前格式的属性表（表头不一致），不能当基线")
            return [{k: iam_export.uncell(k, v or "") for k, v in row.items()} for row in reader]
    except OSError as exc:
        raise DeliveryError(f"读不了基线 {path}：{exc}") from exc


def write_csv(out: Path, rows: list) -> None:
    from .cli import _atomic_private_write

    out = out.resolve()
    _guard(out)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow([iam_export.cell(k, r[k]) for k in CSV_HEADER])
    _atomic_private_write(out, buf.getvalue().encode("utf-8-sig"))


# ── 基线与存档 ─────────────────────────────────────────────────────────────


def resolve_baseline(value: str, sent_dir: Path) -> Path:
    if value == "latest":
        archived = iam_export.confirmed_archives(sent_dir)
        if not archived:
            raise DeliveryError(
                f"{sent_dir} 里还没有确认过的存档：第一次请导出全量加 --record，"
                "IT 导入后再 --confirm-sent"
            )
        return archived[-1]
    path = Path(value)
    if not path.exists():
        raise DeliveryError(f"基线文件 {path} 不存在")
    return path


def latest_baseline(sent_dir: Path) -> Optional[Path]:
    archived = iam_export.confirmed_archives(sent_dir)
    return archived[-1] if archived else None


def pending_archives(sent_dir: Path) -> list:
    """待 IT 确认的存档（新到旧）：名字、行数、当初对着哪个基线导的。"""
    out = []
    pending = sent_dir / "pending"
    if not pending.is_dir():
        return out
    for path in sorted(
        (p for p in pending.glob("*.csv") if iam_export.ARCHIVE_NAME.match(p.name)),
        key=iam_export.archive_order,
        reverse=True,
    ):
        meta_path = path.with_name(path.name + ".meta.json")
        try:
            baseline = str(json.loads(meta_path.read_text(encoding="utf-8")).get("baseline") or "")
        except (OSError, ValueError, AttributeError):
            baseline = ""
        try:
            rows = len(read_csv(path))
        except DeliveryError:
            rows = -1
        inc = path.with_name(path.name[: -len(".csv")] + INCREMENT_SUFFIX)
        out.append(
            {
                "name": path.name,
                "rows": rows,
                "baseline": baseline,
                # 早于这次改动存下的那几份没有增量文件，前端据此不显示下载按钮
                "increment": inc.name if inc.is_file() else "",
            }
        )
    return out


def next_archive(sent_dir: Path, *, pending: bool, stamp: Optional[str] = None) -> Path:
    """下一个存档路径。同一秒内重复导出加 ~n；pending 与已确认目录都不能撞名。"""
    base = sent_dir / "pending" if pending else sent_dir
    archive = base / (stamp or time.strftime("%Y%m%d-%H%M%S.csv"))
    other = sent_dir if pending else sent_dir / "pending"
    n = 1
    while archive.exists() or (other / archive.name).exists():
        archive = archive.with_name(f"{archive.stem.split('~')[0]}~{n}.csv")
        n += 1
    return archive


def record(increment: Increment, archive: Path) -> Path:
    """把这一次的**全量状态**存档（不是增量本身），并记下它对着哪个基线。"""
    meta = archive.with_name(archive.name + ".meta.json")
    _guard(archive.resolve())
    _guard(meta.resolve())
    write_csv(
        archive, iam_export.recorded_state(increment.full, increment.rows, increment.base_rows)
    )
    _write_private_json(meta, {"baseline": increment.baseline.name if increment.baseline else ""})
    return meta


# ── 算增量 ─────────────────────────────────────────────────────────────────


def compute(
    paths: SyncPaths,
    *,
    baseline: str = "",
    allow_mass_remove: bool = False,
    resolved_emails=frozenset(),
    warn: Optional[Callable[[str], None]] = None,
) -> Increment:
    full, specs = full_state(paths, warn=warn)
    if not baseline:
        return Increment(rows=full, notes=[], full=full)
    base_path = resolve_baseline(baseline, paths.sent)
    base_rows = read_csv(base_path)
    iam_export.check_baseline(base_path, base_rows, paths.sent)
    rows, notes = iam_export.diff(
        full,
        base_rows,
        current_apps={key for key, _ in specs.values()},
        allow_mass_remove=allow_mass_remove,
        resolved_emails=frozenset(resolved_emails),
    )
    return Increment(rows=rows, notes=notes, full=full, base_rows=base_rows, baseline=base_path)


# ── 确认与采纳 ─────────────────────────────────────────────────────────────


def confirm(src: Path, sent_dir: Path) -> Path:
    """IT 确认导入后，把 pending 里的存档转为正式基线。返回新基线路径。"""
    pending = sent_dir / "pending"
    if src.is_symlink() or not src.is_file():
        raise DeliveryError(f"{src} 不存在或是符号链接")
    if src.resolve().parent != pending.resolve() or not iam_export.ARCHIVE_NAME.match(src.name):
        raise DeliveryError(f"只能确认 {pending} 里 --record 生成的存档")
    meta_path = src.with_name(src.name + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        recorded = str(meta["baseline"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DeliveryError(f"{src} 缺少存档说明 {meta_path.name}，不能确认") from exc
    archives = iam_export.confirmed_archives(sent_dir)
    latest = archives[-1].name if archives else ""
    for other in sorted(pending.glob("*.csv.meta.json")):
        name = other.name[: -len(".meta.json")]
        # 只剩说明、csv 已经不在了：那一轮没有存档可确认，不该再挡着后面的
        if not (pending / name).is_file():
            continue
        if iam_export.archive_order(Path(name)) >= iam_export.archive_order(src):
            continue
        try:
            same = json.loads(other.read_text(encoding="utf-8")).get("baseline") == recorded
        except (OSError, ValueError, AttributeError):
            same = True
        if same:
            raise DeliveryError(
                f"pending 里还有更早的 {name} 也是对着同一个基线导出的。"
                "如果两份都发给了 IT：先确认更早的那份，再重新 --baseline latest --record；"
                "如果更早那份没发，删掉它再确认这份"
            )
    if recorded != latest:
        raise DeliveryError(
            f"{src.name} 是对着基线「{recorded or '无'}」导出的，"
            f"但现在的最新基线是「{latest or '无'}」。"
            "中间有别的存档被确认过，这份的 remove 不完整：请重新 --baseline latest --record"
        )
    rows = read_csv(src)
    target = sent_dir / src.name
    iam_export.check_baseline(target, rows, sent_dir)
    _guard(target.resolve())
    try:
        os.link(src.resolve(), target.resolve())  # 目标已存在时失败，不会覆盖
    except FileExistsError:
        raise DeliveryError(f"{target} 已存在，不覆盖") from None
    except OSError as exc:
        raise DeliveryError(f"确认失败：{type(exc).__name__}（文件系统不支持硬链接？）") from None
    src.unlink()
    meta_path.unlink()
    inc = src.with_name(src.name[: -len(".csv")] + INCREMENT_SUFFIX)
    if inc.is_file() and not inc.is_symlink():
        inc.unlink()
    return target


def read_result(src: Path) -> list:
    """读 IT 回传的导入结果 CSV。"""
    if src.is_symlink() or not src.is_file():
        raise DeliveryError(f"{src} 不存在或是符号链接")
    try:
        with src.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = sorted(set(iam_export.RESULT_COLUMNS) - set(reader.fieldnames or []))
            if missing:
                raise DeliveryError(f"{src} 缺列：{'、'.join(missing)}")
            return [{k: (r.get(k) or "") for k in iam_export.RESULT_COLUMNS} for r in reader]
    except OSError as exc:
        raise DeliveryError(f"读不了 {src}：{exc}") from exc
    except csv.Error as exc:
        raise DeliveryError(f"{src} 不是合法 CSV：{exc}") from exc


def adopt(src: Path, full: list, sent_dir: Path, *, stamp: Optional[str] = None) -> tuple:
    """IT 回传的导入结果 → 新的确认基线。返回 (存档路径, 提示, set 条数, 沿用条数)。"""
    result_rows = read_result(src)
    previous = iam_export.confirmed_archives(sent_dir)
    # 旧基线里回传没覆盖的值还在 IAM 里：不带上就再也不会生成它们的 remove
    old_rows = read_csv(previous[-1]) if previous else []
    baseline, notes, carried = iam_export.adopt_result(result_rows, full, old_rows)
    if len(baseline) <= carried:
        raise DeliveryError(f"{src} 里没有一条导入成功的记录，不能当基线")
    archive = next_archive(sent_dir, pending=False, stamp=stamp)
    if previous and iam_export.archive_order(archive) <= iam_export.archive_order(previous[-1]):
        raise DeliveryError(
            f"新存档 {archive.name} 排不到最新（当前最新 {previous[-1].name}）：检查系统时间"
        )
    _guard(archive.resolve())
    write_csv(archive, baseline)
    sets = sum(1 for r in baseline if r["action"] == "set")
    return archive, notes, sets, carried


# ── 串行闸 ─────────────────────────────────────────────────────────────────


#: 一轮最多自动回收几个人。超过就整轮拒绝，要人确认。
#: **不是怕删得慢，是怕名册出问题**：`reclaim_plan` 判「可以删」的依据是「名册里没有这个人」，
#: 所以名册一旦读残（刷新挂了、文件被截断），所有在 IAM 里标记离职的人会一起变成「可以删」。
#: 平时一轮就一两个，十个以上必然是上游出了事。
RECLAIM_MAX = 10


def reclaim_iam(
    paths: SyncPaths,
    *,
    apply: bool = False,
    force: bool = False,
    transport=None,
    log: Optional[Callable] = None,
    announce: Optional[Callable] = None,
) -> dict:
    """离职回收：把已离职的人的 cloud_accounts 属性删掉。面板和 CLI 共用。

    **只删属性，不碰云上账号。** 删属性可逆（PUT 回去），禁用/删除 RAM 用户不可逆，
    那一步永远报给人做。

    **先算完整个计划再动手。** 边算边删的话，超量闸门要等删过之后才触发 —— 那时候
    该拦的已经删掉了。
    """
    from . import iam_api

    cfg = iam_api.Config.from_env()
    full, _ = full_state(paths)
    roster_uids = {str(r.get("feishu_union_id") or "") for r in full if r.get("feishu_union_id")}
    if not roster_uids:
        # 名册一个 union_id 都没有 = 读残了。这时候「名册里没有他」对**每个人**都成立
        raise DeliveryError(
            "名册里一个 union_id 都没有，不能据此判断谁离职了。先确认 people.json 和刷新任务正常。"
        )
    scopes = sorted({str(r.get("app") or "") for r in full if r.get("app")})

    # ① 先把整个计划算出来
    plan, held, failed = [], [], []
    for scope in scopes:
        try:
            app = iam_api.app_of(scope)
            theirs = iam_api.list_by_app(app, cfg=cfg, transport=transport)
        except iam_api.IamApiError as exc:
            # 读不到就什么都别做。**基于残缺的清单去删，比不删危险得多**
            failed.append({"app": scope, "error": cfg.scrub(str(exc).splitlines()[0])})
            continue
        for r in iam_api.reclaim_plan(theirs, app=app, roster_uids=roster_uids):
            row = {
                "app": r.app,
                "union_id": r.union_id,
                "username": r.username,
                "name": r.name,
                "value": r.value,
                "why": r.why,
                "stale_roster": r.stale_roster,
            }
            # 以 Authentik 为准 → 都回收。`held` 现在只在拿不到判断依据时才用
            (plan if r.sure else held).append(row)

    # ② 有任何一个平台读不到，就**整轮不动手**：少读一个平台，
    #    那个平台上真该回收的人这轮漏掉是小事，怕的是据此做出的判断本身不完整
    if failed and apply:
        raise DeliveryError(
            "有平台的清单没读到（" + "；".join(f.get("error", "") for f in failed)[:160] + "），"
            "这一轮不回收。读不全的时候不动手。"
        )
    if apply and not force and len(plan) > RECLAIM_MAX:
        raise DeliveryError(
            f"这一轮要回收 {len(plan)} 个人的属性，超过 {RECLAIM_MAX} 个。"
            "平时一轮就一两个 —— 这么多通常意味着名册出了问题，不是真有这么多人离职。"
            "核对无误后加 --force。"
        )

    # ③ 闸门都过了才动手
    done = []
    for row in plan:
        if not apply:
            done.append({**row, "previous": "", "dry_run": True})
            continue
        try:
            got = iam_api.delete_value(row["union_id"], row["app"], cfg=cfg, transport=transport)
            done.append({**row, "previous": str(got.get("previous") or "")})
        except iam_api.IamApiError as exc:
            failed.append({**row, "error": cfg.scrub(str(exc).splitlines()[0])})
    report = {
        "done": done,
        "held": held,
        "failed": failed,
        "applied": bool(apply),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if apply and (done or held):
        # 落审计、发通知。**任一处失败都不该让已经做完的回收变成「没发生过」** ——
        # 所以两个都吞掉异常，只把问题记进 failed
        for fn, what in ((log, "审计日志"), (announce, "飞书通知")):
            if fn is None:
                continue
            try:
                fn(report)
            except Exception as exc:  # noqa: BLE001
                failed.append({"app": "", "error": f"{what}失败：{type(exc).__name__}: {exc}"})
    return report


def confirm_reclaim(
    paths: SyncPaths, union_id: str, app: str, *, actor: str, transport=None, log=None
) -> dict:
    """管理员确认某人确实离职 → 删掉他那条 cloud_accounts 属性。

    **管理员这一下就是第二个信号。** 自动回收要求「IT 的 IAM 说离职」+「名册里也没有」
    两条都成立；这里用人的确认顶替第二条 —— 名册是从飞书通讯录自动生成的，
    手工在里面标离职下次刷新就被冲掉，所以没法靠改名册来表达这个判断。

    **但第一条不能省。** 服务端**重新读一次 IAM**，确认这个人现在确实是 `is_active=false`
    才删 —— 不能只凭请求里带的 union_id 就动手。否则一个构造出来的请求
    就能删掉任何一个**在职**的人的登录名，而他要到下次登录才发现。
    """
    from . import iam_api

    cfg = iam_api.Config.from_env()
    uid = str(union_id or "").strip()
    if not uid:
        raise DeliveryError("要回收谁？缺 union_id")
    try:
        name = iam_api.app_of(str(app or ""))
    except iam_api.IamApiError as exc:
        raise DeliveryError(str(exc)) from exc

    # 现读现判，不信请求里说的
    hit = next(
        (
            u
            for u in iam_api.list_by_app(name, cfg=cfg, transport=transport)
            if str(u.get("union_id") or "") == uid
        ),
        None,
    )
    if hit is None:
        raise DeliveryError(f"IAM 里没有这个人（{name}），没有可回收的属性")
    # 同 reclaim_plan：值是 null 时「不知道」必须当「在职」，不能当离职放行
    if hit.get("is_active") is not False:
        raise DeliveryError(
            f"{hit.get('username') or uid} 在 IAM 里还是**在职**状态，不能按离职回收。"
            "IT 那边先把人标成离职，这里才能收。"
        )
    if not str(hit.get("value") or ""):
        raise DeliveryError("这个人在这个平台本来就没有登录名，没什么可收的")

    got = iam_api.delete_value(uid, name, cfg=cfg, transport=transport)
    row = {
        "app": name,
        "union_id": uid,
        "username": str(hit.get("username") or ""),
        "name": str(hit.get("name") or ""),
        "value": str(hit.get("value") or ""),
        "previous": str(got.get("previous") or ""),
        "why": "管理员确认离职",
    }
    if log is not None:
        log([row])
    return row


#: 对账结果缓存。**存在的理由是「别让人去点」** —— 对账要出去打 IT 的接口、几秒钟，
#: 做成按钮就意味着没人点，待办永远躺在那儿没人看见。缓存下来，页面一打开就显示。
RECONCILE_CACHE = "iam-reconcile.json"
#: 「稍后处理」的记录。键是 `<app>/<union_id>`
SNOOZE_FILE = "iam-snooze.json"


def _side_file(paths: SyncPaths, name: str) -> Path:
    return Path(paths.people).resolve().parent / name


def load_snooze(paths: SyncPaths, *, now: Optional[float] = None) -> dict:
    """还在「稍后处理」期内的条目。过期的自动不算数，不用清理。"""
    at = time.time() if now is None else now
    try:
        data = json.loads(_side_file(paths, SNOOZE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items() if isinstance(v, dict) and float(v.get("until") or 0) > at
    }


REMIND_FILE = "iam-remind.json"


def claim_remind(
    paths: SyncPaths, signature: str, *, hours: float, now: Optional[float] = None
) -> bool:
    """这批人该不该现在提醒。该提醒就记下时间并返回 True。

    **读不到状态时照常提醒**（返回 True）。多提醒一次的代价是一条消息，
    漏提醒的代价是一个离职的人的云账号一直挂着没人知道 —— 这两边不对称。
    """
    at = time.time() if now is None else now
    path = _side_file(paths, REMIND_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last = float((data.get(signature) or {}).get("at") or 0)
    except Exception:  # noqa: BLE001 — 见 docstring
        data, last = {}, 0.0
    if last and at - last < max(0.0, hours) * 3600:
        return False
    data[signature] = {"at": at}
    # 只留最近 50 条：签名随人员变动而变，不清理的话这个文件会一直长
    if len(data) > 50:
        data = dict(sorted(data.items(), key=lambda kv: -float(kv[1].get("at") or 0))[:50])
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
    return True


def snooze(paths: SyncPaths, union_id: str, app: str, *, hours: float, actor: str) -> dict:
    """稍后处理：`hours` 小时内不再提醒这一条。

    **不是「忽略」。** 到点它会自己回到待办里 —— 忽略需要一个理由和一次复核，
    而「现在没空」不需要。两者混成一个按钮的话，人会拿忽略当稍后用。
    """
    key = f"{str(app or '').strip()}/{str(union_id or '').strip()}"
    if key == "/" or not union_id or not app:
        raise DeliveryError("缺 union_id 或 app")
    if not 0 < hours <= 24 * 7:
        raise DeliveryError("稍后处理最长一周")
    now = time.time()
    path = _side_file(paths, SNOOZE_FILE)
    data = {}
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(got, dict):
            data = got
    except (OSError, json.JSONDecodeError):
        data = {}
    data[key] = {"until": now + hours * 3600, "by": actor, "at": now, "hours": hours}
    _write_private_json(path, data)
    return {"key": key, "until": data[key]["until"], "hours": hours}


def cached_reconcile(paths: SyncPaths) -> Optional[dict]:
    """上次对账的结果。**读不到就返回 None，不返回空报告** ——
    「还没对过账」和「对过了，没问题」必须分得开。"""
    try:
        data = json.loads(_side_file(paths, RECONCILE_CACHE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _cloud_users(paths: SyncPaths, scope: str):
    """某个云账号上**现在还存在**的子账号登录名。拿不到返回 None（那一类就整类不判）。

    读的是权限快照（`identity/inventory.json`），每 20 分钟由 refresh 重采。
    所以「有人刚在控制台删了号」最多 20 分钟后才报出来 —— 这个延迟可以接受，
    而把「快照读不到」当成「账号都没了」不可以。

    **采集失败的账号一律返回 None。** 快照里那个账号会是 0 个用户，
    照着判的话它下面每一个人都会被报成「账号已删」 —— 一次凭证过期就能刷出
    几十条假线索，而假线索会让这一栏从此没人看。`incomplete` 记的就是这件事。
    """
    from . import inventory

    want = str(scope or "")
    try:
        snap = inventory.load(getattr(paths, "inventory", None) or "identity/inventory.json")
    except Exception:  # noqa: BLE001 — 快照缺失/写坏都只是「这次不判这一类」
        return None
    if any(str(x or "").startswith(f"{want}：") for x in (snap.incomplete or ())):
        return None
    platform, _, account = want.partition("/")
    names = {
        u.name for u in snap.users if u.platform == platform and u.account == account and u.name
    }
    return names or None


def reconcile_report(paths: SyncPaths, *, transport=None) -> dict:
    """IAM 侧实际 vs 名册。面板和 CLI 共用 —— 规则只写一份，和这个模块其余部分一样。

    **只读**：一次调用不写任何文件、不改基线、不下发任何东西。

    返回里 `blind` 和 `drift` 是两件事，不能合并：`drift` 是两边都看得到但对不上，
    `blind` 是我们这边**根本没法比**（没有 union_id，接口只认它）。
    把后者混进「对不上 0 条」，读的人会以为两边一致，而实际上有人压根没进过比对。
    """
    from . import iam_api

    cfg = iam_api.Config.from_env()
    full, _ = full_state(paths)
    scopes = sorted({str(r.get("app") or "") for r in full if r.get("app")})
    apps, total = [], 0
    for scope in scopes:
        entry = {
            "scope": scope,
            "app": "",
            "theirs": 0,
            "compared": 0,
            "drift": [],
            "blind": [],
            "error": "",
        }
        try:
            app = iam_api.app_of(scope)
            entry["app"] = app
            theirs = iam_api.list_by_app(app, cfg=cfg, transport=transport)
        except iam_api.IamApiError as exc:
            # 一个平台读不到不该让整页失败，但**必须显示出来** ——
            # 静默跳过等于把「没读到」渲染成「这个平台没问题」
            entry["error"] = cfg.scrub(str(exc).splitlines()[0])
            apps.append(entry)
            continue
        ours = [r for r in full if r.get("app") == scope]
        # 云侧快照喂进去才判得出「属性指向的账号已经没了」。
        # **拿不到就传 None**，那一类整类跳过 —— 拿不到快照却照判，
        # 等于把全公司报成「账号已删」
        drift = iam_api.reconcile(theirs, ours, app=app, cloud_users=_cloud_users(paths, scope))
        blind = iam_api.uncomparable(ours)
        entry["theirs"] = len(theirs)
        entry["compared"] = len([r for r in ours if r.get("action") == "set"]) - len(blind)
        entry["drift"] = [
            # **`app` 必须带上**：前端的「确认离职」「稍后处理」两个按钮都要它，
            # 少了它请求体里就是 undefined（被 JSON.stringify 丢掉），两个按钮 100% 失败，
            # 而弹窗上还会显示「删掉他在 undefined 的登录名」
            {
                "kind": d.kind,
                "app": d.app,
                "union_id": d.union_id,
                "username": d.username,
                "name": d.name,
                "theirs": d.theirs,
                "ours": d.ours,
            }
            for d in drift
        ]
        entry["blind"] = [
            {
                "name": str(r.get("name") or ""),
                "email": str(r.get("email") or ""),
                "value": str(r.get("value") or ""),
            }
            for r in blind
        ]
        total += len(drift) + len(blind)
        apps.append(entry)
    held = load_snooze(paths)
    pending = sum(
        1
        for e in apps
        for d in e["drift"]
        if d["kind"] == "inactive" and f"{e['app']}/{d['union_id']}" not in held
    )
    report = {
        "apps": apps,
        "total": total,
        "pending": pending,
        "snoozed": len(held),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with contextlib.suppress(DeliveryError, OSError):
        # 缓存失败不该让对账本身失败 —— 结果已经算出来了，页面照样显示
        _write_private_json(_side_file(paths, RECONCILE_CACHE), report)
    return report


@contextlib.contextmanager
def exclusive(paths: SyncPaths):
    """导出 / 确认 / 作废 串行执行。

    这三步都是「读现状 → 判断 → 写盘」：两个管理员同时点，可能算出同一个存档名而
    互相覆盖，或者两份待确认存档都通过「基线没变过」这道一致性检查。锁文件在
    identity/iam-sent/.lock，CLI 和面板共用同一把，跨进程有效。
    """
    sent = paths.sent
    sent.mkdir(parents=True, exist_ok=True)
    lock = (sent / ".lock").resolve()
    _guard(lock)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


# ── 面板用：预览 / 导出 / 确认 ─────────────────────────────────────────────


def _baseline_view(sent_dir: Path) -> Optional[dict]:
    path = latest_baseline(sent_dir)
    if path is None:
        return None
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(path.stat().st_mtime))
    except OSError:
        stamp = ""
    return {"name": path.name, "captured_at": stamp}


def preview(paths: SyncPaths, *, allow_mass_remove: bool = False) -> dict:
    """管理后台的只读视图：这一次要发什么、有哪些待确认存档。不写任何文件。"""
    sent = paths.sent
    view = {
        "attributes_configured": Path(paths.attributes).exists(),
        "baseline": _baseline_view(sent),
        "increment": {"rows": [], "counts": {"set": 0, "remove": 0, "skip": 0}},
        "pending": pending_archives(sent),
        "notes": [],
        "can_export": False,
        "blocked": "",
    }
    if not view["attributes_configured"]:
        view["blocked"] = (
            f"缺 {paths.attributes}：写明每个云账号对应的 IAM 属性名，配好之后这里才能导出"
        )
        return view
    mode = "latest" if view["baseline"] else ""
    try:
        increment = compute(paths, baseline=mode, allow_mass_remove=allow_mass_remove)
    except DeliveryError as exc:
        view["blocked"] = str(exc).splitlines()[0]
        return view
    view["increment"] = {"rows": increment.rows, "counts": increment.counts}
    view["notes"] = list(increment.notes)
    view["can_export"] = True
    return view


def export(paths: SyncPaths, *, allow_mass_remove: bool = False) -> dict:
    """算增量 → 写输出文件 → 存一份待确认存档。返回预览同款视图 + 两个文件名。

    两份文件不是一回事，**发给 IT 的是增量那份**：

    * ``out_name``  这次的增量（含 remove 行），IT 导入的就是它；
    * ``recorded``  这一刻的全量状态存档，只用来当下一轮的比对基线。
      全量存档按设计不含 remove（``check_baseline`` 会拒），发它等于漏掉所有删号。
    """
    sent = paths.sent
    with exclusive(paths):
        baseline = latest_baseline(sent)
        increment = compute(
            paths, baseline="latest" if baseline else "", allow_mass_remove=allow_mass_remove
        )
        # 存档路径先过守卫再写主输出：守卫失败时不能留下一份没有存档的输出
        archive = next_archive(sent, pending=True)
        _guard(archive.resolve())
        _guard(archive.with_name(archive.name + ".meta.json").resolve())
        out = Path(paths.out)
        write_csv(out, increment.rows)
        record(increment, archive)
        # 与存档同名的增量副本：out 是共享单文件，会被下一次导出覆盖
        inc = archive.with_name(archive.name[: -len(".csv")] + INCREMENT_SUFFIX)
        write_csv(inc, increment.rows)
    view = {
        "attributes_configured": True,
        "baseline": _baseline_view(sent),
        "increment": {"rows": increment.rows, "counts": increment.counts},
        "pending": pending_archives(sent),
        "notes": list(increment.notes),
        "can_export": True,
        "blocked": "",
        "recorded": archive.name,
        # 只回文件名：服务端路径不进浏览器，下载也只按名字取
        "out_name": out.name,
    }
    return view


def confirm_by_name(paths: SyncPaths, name: str) -> dict:
    """按存档名确认（面板用）。名字必须是 pending 里的存档名，不接受路径。"""
    if not iam_export.ARCHIVE_NAME.match(name or ""):
        raise DeliveryError("存档名不对：只能确认 pending 里 --record 生成的存档")
    src = paths.pending_dir / name
    with exclusive(paths):
        target = confirm(src, paths.sent)
    view = preview(paths)
    view["confirmed"] = target.name
    return view


def discard(paths: SyncPaths, name: str) -> dict:
    """作废一份待确认存档（面板用）：这次导的没发出去，或被后面重导的那份取代了。

    只删 pending 里的，已确认的基线动不了。删掉之后那一轮就当没发生过，
    下次导出仍以当前基线为准 —— 所以**只有确认 IT 没有导入过这份，才能作废**。
    """
    if not iam_export.ARCHIVE_NAME.match(name or ""):
        raise DeliveryError("存档名不对：只能作废 pending 里的存档")
    with exclusive(paths):
        pending = paths.pending_dir.resolve()
        src = paths.pending_dir / name
        meta = src.with_name(src.name + ".meta.json")
        # csv 和说明各自可能单独残留（写到一半失败、或上一次只删掉了一个）：
        # 两个都不在才算「没有这份」，否则清不掉的那个会永远挡住 confirm
        inc = src.with_name(name[: -len(".csv")] + INCREMENT_SUFFIX)
        alive = [
            f
            for f in (src, meta, inc)
            if f.is_file() and not f.is_symlink() and f.resolve().parent == pending
        ]
        if not alive:
            raise DeliveryError("没有这份待确认存档")
        for f in alive:
            _guard(f.resolve())
            f.unlink()
    view = preview(paths)
    view["discarded"] = name
    return view


def archive_file(paths: SyncPaths, name: str) -> Path:
    """按名字定位一份可下载的 CSV：已确认存档、待确认存档，或当前增量输出文件。

    只认这三处的文件名，不接受路径分隔符；再用写盘守卫确认落在 identity/ 下。
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise DeliveryError("文件名不对")
    out = Path(paths.out)
    if name == out.name:
        candidate = out
    elif iam_export.ARCHIVE_NAME.match(name):
        confirmed = paths.sent / name
        candidate = confirmed if confirmed.exists() else paths.pending_dir / name
    elif INCREMENT_NAME.match(name):
        candidate = paths.pending_dir / name
    else:
        raise DeliveryError("文件名不对")
    if candidate.is_symlink() or not candidate.is_file():
        raise DeliveryError("没有这份文件")
    real = candidate.resolve()
    if real.parent not in (
        paths.sent.resolve(),
        paths.pending_dir.resolve(),
        out.resolve().parent,
    ):
        raise DeliveryError("没有这份文件")
    _guard(real)  # 含员工邮箱：只能从 identity/ 下取
    return real
