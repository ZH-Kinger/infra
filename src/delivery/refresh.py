"""定时刷新：采集权限快照 → 生成映射提案 → 重建人员名册，有异常就告警。

面板的数据全靠这三步，以前是手动逐条跑，隔几天不跑面板就显示过期的权限。

不完整时怎么办
──────────────
权限快照照写：面板本来就会标出「哪个账号没采全」，比显示上一次的旧数据诚实。
**名册不重建**：映射提案是从云上用户列表推出来的，某朵云采集失败时提案里就少了那朵云的
所有账号，拿它重建名册会把这些人的账号全部抹掉。保留上一份名册，告警里说明。
上一份名册存在却读不了时同样不重建：沿用 union_id、共用邮箱标记都依赖它。

变化比对的基线
──────────────
不拿「上一份快照」比：某个平台采集失败的那一次，快照里没有这个平台，下一次恢复后
这个平台的所有用户都会被报成「新增」；反过来失败期间的变化会被跳过、再也报不出来。
所以另存一份基线（inventory.baseline.json），**只有采集成功的平台才推进基线**。

身份信息沿用
────────────
通讯录来源是 none 时，union_id、「通讯录多人共用邮箱」标记、只在通讯录里的人都要从
上一份名册带过来，规则见 people.carry_identities。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional

from . import inventory
from . import people as people_mod

#: 告警里每类最多列几条，其余只给数字
_LIST_LIMIT = 10
_GROUP_SUFFIX = re.compile(r"（经组 .*）$")


@dataclass
class RefreshReport:
    snapshot_written: bool = False
    people_written: bool = False
    #: 采集不完整的账号，或整步失败的原因
    problems: list = field(default_factory=list)
    new_high_risk: list = field(default_factory=list)
    added_users: list = field(default_factory=list)
    #: 新增子账号里**不是面板开的**那些。云上没有任何字段记着「这个号是谁为谁开的」，
    #: 所以手工在控制台开的号，事后查不到来历——今天审计里那些来路不明的账号就是这么来的。
    #: 做不到完全自动登记，能做到的最好程度是「当天发现 + 提醒补登记」，
    #: 越早问越有人记得，隔一个月就没人说得清了
    added_unregistered: list = field(default_factory=list)
    removed_users: list = field(default_factory=list)
    new_unlinked: list = field(default_factory=list)
    lost_union_ids: list = field(default_factory=list)
    people_count: int = 0
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.problems
            or self.new_high_risk
            or self.added_users
            or self.removed_users
            or self.new_unlinked
            or self.lost_union_ids
        )

    def render(self) -> str:
        head = "云权限面板数据刷新" + ("完成" if self.ok else "异常")
        lines = [head]

        def section(title: str, items: list) -> None:
            if not items:
                return
            lines.append(f"{title}（{len(items)}）")
            lines.extend(f"  · {x}" for x in items[:_LIST_LIMIT])
            if len(items) > _LIST_LIMIT:
                lines.append(f"  · 另有 {len(items) - _LIST_LIMIT} 条，见面板")

        section("采集问题", self.problems)
        section("新增高危权限", self.new_high_risk)
        section("新增子账号", self.added_users)
        section("其中不是面板开的（请补登记：是谁、给谁、为什么）", self.added_unregistered)
        section("删除子账号", self.removed_users)
        section("新出现的未关联账号", self.new_unlinked)
        section("没能沿用 union_id 的人（下次登录需重新关联）", self.lost_union_ids)
        lines.extend(self.notes)
        lines.append(
            f"快照{'已更新' if self.snapshot_written else '未更新'}，"
            f"名册{'已更新' if self.people_written else '未更新（保留上一份）'}"
            + (f"，共 {self.people_count} 人" if self.people_written else "")
        )
        return "\n".join(lines)


def _user_key(user) -> str:
    return f"{user.platform}/{user.account}/{user.name}"


def _risk_pairs(snapshot: inventory.Snapshot) -> set:
    # 去掉「（经组 X）」：只是换了组、权限没变，不算新增
    return {
        (_user_key(user), _GROUP_SUFFIX.sub("", policy))
        for user, policies in inventory.high_risk_holders(snapshot)
        for policy in policies
    }


def _failed_platforms(snapshot: inventory.Snapshot) -> set:
    return {line.split("/", 1)[0].strip() for line in snapshot.incomplete}


def diff_snapshots(
    before: Optional[inventory.Snapshot],
    after: inventory.Snapshot,
    report: RefreshReport,
    known: Optional[set] = None,
) -> None:
    """只比两边都采集成功的平台。

    按平台整体排除，不按账号：采集失败时错误条目里的账号字段可能是凭证前缀而不是 UID，
    对不上号就会把失败账号误当成「全员被删」。
    """
    if before is None:
        report.notes.append("没有比对基线，本次不比较变化")
        return
    failed = _failed_platforms(after) | _failed_platforms(before)

    def usable(key: str) -> bool:
        return key.split("/", 1)[0] not in failed

    now_users = {_user_key(u) for u in after.users}
    old_users = {_user_key(u) for u in before.users}
    report.added_users = sorted(k for k in now_users - old_users if usable(k))
    # 面板开的号在台账里有记录，不用问；剩下的才要人去补来历
    report.added_unregistered = sorted(k for k in report.added_users if k not in (known or set()))
    report.removed_users = sorted(k for k in old_users - now_users if usable(k))
    report.new_high_risk = sorted(
        f"{key}：{policy}"
        for key, policy in _risk_pairs(after) - _risk_pairs(before)
        if usable(key)
    )


def next_baseline(baseline: Optional[Mapping], snapshot_data: Mapping) -> dict:
    """采集成功的平台用新数据，失败的平台保留旧基线。"""
    snap = inventory.parse(snapshot_data)
    failed = _failed_platforms(snap)
    kept = [
        acc
        for acc in (baseline or {}).get("accounts") or []
        if isinstance(acc, dict)
        and str(acc.get("platform") or "").strip() in failed
        and not acc.get("error")
    ]
    fresh = [
        acc
        for acc in snapshot_data.get("accounts") or []
        if isinstance(acc, dict) and str(acc.get("platform") or "").strip() not in failed
    ]
    return {"captured_at": snapshot_data["captured_at"], "accounts": fresh + kept}


def _unlinked_keys(data: Optional[Mapping]) -> set:
    if not data:
        return set()
    return {
        f"{u.get('platform')}/{u.get('account')}/{u.get('name')}"
        for u in data.get("unlinked") or []
        if u.get("kind") != "service"
    }


def run(
    *,
    collect_snapshot: Callable[[], dict],
    collect_proposal: Callable[[], dict],
    directory: Callable[[], Iterable],
    manual: Optional[Mapping],
    previous_baseline: Optional[Mapping],
    previous_people: Optional[Mapping],
    write_snapshot: Callable[[dict], object],
    write_baseline: Callable[[dict], object],
    write_proposal: Callable[[dict], object],
    write_people: Callable[[dict], object],
    carry_over: bool,
    previous_errors: Iterable[str] = (),
    known_users: Optional[set] = None,
    report: Optional[RefreshReport] = None,
) -> RefreshReport:
    """`previous_errors`：上一份名册 / 基线存在但读不了的说明。有这类错误时不重建名册。"""
    report = report if report is not None else RefreshReport()
    previous_errors = list(previous_errors)
    report.problems.extend(previous_errors)

    try:
        data = collect_snapshot()
        snap = inventory.parse(data)
    except Exception as exc:  # noqa: BLE001 — 任何失败都要进告警，不能让定时任务静默挂掉
        report.problems.append(f"权限快照采集失败：{brief(exc)}")
        return report
    try:
        write_snapshot(data)
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"快照写入失败：{brief(exc)}")
        return report
    report.snapshot_written = True
    report.problems.extend(snap.incomplete)

    try:
        before = inventory.parse(previous_baseline) if previous_baseline else None
    except Exception as exc:  # noqa: BLE001
        before = None
        report.problems.append(f"比对基线读不了：{brief(exc)}")
    diff_snapshots(before, snap, report, known=known_users)
    try:
        write_baseline(next_baseline(previous_baseline if before else None, data))
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"比对基线写入失败：{brief(exc)}")

    if snap.incomplete:
        report.notes.append("快照不完整，名册不重建，避免抹掉没采到的账号")
        return report
    if previous_errors:
        report.notes.append("上一份数据读不了，名册不重建，避免丢掉 union_id")
        return report

    try:
        proposal = collect_proposal()
        entries = list(directory())
        merged = people_mod.apply_manual(proposal, manual) if manual else proposal
        built = people_mod.build(merged, entries)
        if carry_over and previous_people:
            stats = people_mod.carry_identities(built, previous_people.get("people") or [])
            if stats["carried"]:
                report.notes.append(f"沿用上一份名册的 union_id {stats['carried']} 人")
            report.lost_union_ids = stats["lost"]
            if stats["duplicates"]:
                report.problems.append(
                    f"上一份名册里有 {len(stats['duplicates'])} 个 union_id 重复出现，"
                    "未沿用，请核对"
                )
        people_mod.parse(built)
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"名册重建失败：{brief(exc)}")
        return report
    try:
        # 先写名册再写提案：提案写失败时名册是新的、提案是旧的，名册审核会检测到两者对不上
        # 并拒绝操作；反过来（提案新、名册旧）审核同样会拒绝，但定时刷新下一轮才能纠正
        write_people(built)
        write_proposal(proposal)
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"名册写入失败：{brief(exc)}")
        return report
    report.people_written = True
    report.people_count = built["stats"]["people"]
    if previous_people is not None:
        report.new_unlinked = sorted(_unlinked_keys(built) - _unlinked_keys(previous_people))
    return report


def brief(exc: Exception) -> str:
    """只取第一个非空行：云 API 报错后面可能跟着请求参数。"""
    lines = [line for line in str(exc).splitlines() if line.strip()]
    return f"{type(exc).__name__}: {lines[0].strip()}" if lines else type(exc).__name__
