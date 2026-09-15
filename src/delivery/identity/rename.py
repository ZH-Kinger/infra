"""把云上的用户名改成「和企业邮箱前缀一致」，为用户 SSO 做准备。

为什么要改名
────────────
阿里云和火山的**用户 SSO 都要求 SAML 断言里的 NameID 精确等于云上的用户名**，
两家都不提供任何匹配规则配置——官方口径一致：要做映射就在 IdP 侧做 claim
transformation。而飞书能放进 NameID 的是固定的几个字段（邮箱、企业邮箱、工号、
别名……），没有按人做字符串变换的能力。

所以 `zhangsan`（云上）和 `zhang.san`（邮箱前缀）之间那个点，只能在云上消掉。

改名安全吗
──────────
`ram:UpdateUser` 只改登录名，**UserId 不变**，因此策略、用户组、PAI 工作空间
成员资格、用户自己的 AccessKey 全部保留。真正会断的是**按用户名索引的外部引用**
（策略命名、映射表、脚本），所以 `plan()` 会把它们一并列出来。

这个模块只算计划、不执行
────────────────────────
计划与执行分离是刻意的：47 个账号改名是一次性、不可逆（改回去要再跑一次）、
且影响真人登录的动作。审阅一份明确的清单，和看着脚本一边跑一边改，
是完全不同的两件事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..errors import DeliveryError
from .audit import AccountUser

#: 阿里云 RAM 用户名字符集：1-64 位，字母数字加 `.` `-` `_`，不能以连字符开头。
#: 火山 IAM 的约束更严一些，所以这里取两家的交集。
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

ACTION_RENAME = "rename"
ACTION_KEEP = "keep"
ACTION_SKIP = "skip"


class RenameError(DeliveryError):
    """改名计划不合法。"""


@dataclass(frozen=True)
class RenamePlan:
    platform: str
    account: str
    current: str
    target: str
    action: str
    display_name: str = ""
    reason: str = ""

    @property
    def changes(self) -> bool:
        return self.action == ACTION_RENAME


def target_name(email: str) -> str:
    """目标用户名 = 企业邮箱的本地部分，**原样保留点号**。

    这正是和现有规则相反的方向：今天的对账规则是「去掉点」，而 SSO 要的是
    「和邮箱前缀逐字一致」。两者不能同时满足，SSO 的要求是硬的。
    """
    return (email or "").split("@", 1)[0].strip()


def plan(
    users: Iterable[AccountUser],
    *,
    domain: str,
    service_prefixes: Sequence[str] = (),
) -> list:
    """算出每个人要不要改名。不连云、不改任何东西。"""
    out = []
    for user in users:
        if service_prefixes and user.name.lower().startswith(tuple(service_prefixes)):
            out.append(
                RenamePlan(
                    platform=user.platform,
                    account=user.account,
                    current=user.name,
                    target="",
                    action=ACTION_SKIP,
                    display_name=user.display_name,
                    reason="服务号，不参与 SSO",
                )
            )
            continue
        if not user.email.endswith(domain):
            out.append(
                RenamePlan(
                    platform=user.platform,
                    account=user.account,
                    current=user.name,
                    target="",
                    action=ACTION_SKIP,
                    display_name=user.display_name,
                    reason=f"没有 {domain} 企业邮箱，SSO 本来就登不进来，先补邮箱",
                )
            )
            continue
        want = target_name(user.email)
        if not _NAME_RE.match(want):
            # 邮箱前缀里有云平台不接受的字符（`+` 分隔符之类）。这种必须人工处理，
            # 硬改会造出一个建不出来的名字，而错误要等 apply 时才暴露。
            out.append(
                RenamePlan(
                    platform=user.platform,
                    account=user.account,
                    current=user.name,
                    target=want,
                    action=ACTION_SKIP,
                    display_name=user.display_name,
                    reason=f"邮箱前缀 `{want}` 不符合云平台用户名字符集，需人工处理",
                )
            )
            continue
        if want == user.name:
            out.append(
                RenamePlan(
                    platform=user.platform,
                    account=user.account,
                    current=user.name,
                    target=want,
                    action=ACTION_KEEP,
                    display_name=user.display_name,
                )
            )
            continue
        out.append(
            RenamePlan(
                platform=user.platform,
                account=user.account,
                current=user.name,
                target=want,
                action=ACTION_RENAME,
                display_name=user.display_name,
            )
        )
    _assert_no_collisions(out)
    out.sort(key=lambda p: (p.action != ACTION_RENAME, p.platform, p.current))
    return out


def _assert_no_collisions(plans: Sequence) -> None:
    """改名后不能出现重名，也不能撞上一个已存在、但不该被改的账号。

    撞名的后果不是「报个错」——`UpdateUser` 到一半失败会留下一半改了一半没改的
    状态，而这批人此刻正处在「旧名字已失效、新名字还没生效」的中间态。
    所以在**一个都没改之前**就要全量检查。
    """
    by_scope: dict = {}
    for p in plans:
        scope = f"{p.platform}/{p.account}"
        by_scope.setdefault(scope, {"final": {}, "existing": set()})
        by_scope[scope]["existing"].add(p.current.lower())
    for p in plans:
        if p.action not in (ACTION_RENAME, ACTION_KEEP):
            continue
        scope = f"{p.platform}/{p.account}"
        bucket = by_scope[scope]["final"]
        key = p.target.lower()
        if key in bucket:
            raise RenameError(
                f"{scope}: `{bucket[key]}` 和 `{p.current}` 改名后都会叫 `{p.target}`。"
                f"两人共用一个企业邮箱前缀？先把这个查清楚再改。"
            )
        bucket[key] = p.current
    for p in plans:
        if p.action != ACTION_RENAME:
            continue
        scope = f"{p.platform}/{p.account}"
        if p.target.lower() in by_scope[scope]["existing"]:
            owner = next(
                (
                    q.current
                    for q in plans
                    if q.platform == p.platform
                    and q.account == p.account
                    and q.current.lower() == p.target.lower()
                ),
                p.target,
            )
            if owner.lower() != p.current.lower():
                raise RenameError(
                    f"{scope}: `{p.current}` 要改成 `{p.target}`，"
                    f"但这个名字已经被 `{owner}` 占着。需要先决定改名顺序或人工处理。"
                )


#: 按用户名索引、改名后会断的外部引用。改名计划必须把它们列出来——
#: 云上改名本身是安全的（UserId 不变），真正的风险全在这些地方。
EXTERNAL_REFERENCES = (
    ("oss_perm/permsync", "生成的策略名 `wuji-oss-auto-<username>`，改名后旧策略成孤儿"),
    ("ram_user_map.json", "「姓名 → RAM 用户名」映射表，要同步更新"),
    ("identity/overrides.json", "登记表里按旧用户名写的条目"),
    ("bot get_ram_user_by_open_id", "飞书 open_id → RAM 用户的解析，按名字匹配的部分"),
    ("ALIYUN_BOT_ROLE_MAPPING", "按**组名**匹配，不受改名影响（已核）"),
    ("用户自己的 AccessKey", "绑 UserId，不受影响（已核）"),
)


def render(plans: Sequence, *, limit: Optional[int] = None) -> str:
    """给人看的计划。先列要改的，再列不改的原因。"""
    todo = [p for p in plans if p.action == ACTION_RENAME]
    keep = [p for p in plans if p.action == ACTION_KEEP]
    skip = [p for p in plans if p.action == ACTION_SKIP]
    lines = [
        f"改名计划：{len(todo)} 个要改，{len(keep)} 个已经对了，{len(skip)} 个跳过",
        "",
    ]
    shown = todo if limit is None else todo[:limit]
    for p in shown:
        lines.append(
            f"  {p.platform}/{p.account}  {p.current:<24} → {p.target:<26} [{p.display_name}]"
        )
    if limit is not None and len(todo) > limit:
        lines.append(f"  …… 另有 {len(todo) - limit} 个")
    if skip:
        lines += ["", "跳过的："]
        for p in skip:
            lines.append(f"  {p.current:<24} {p.reason}")
    lines += ["", "改名本身安全（UserId 不变，策略/组/AK 全保留）。要一起改的外部引用："]
    for name, note in EXTERNAL_REFERENCES:
        lines.append(f"  · {name}：{note}")
    return "\n".join(lines)
