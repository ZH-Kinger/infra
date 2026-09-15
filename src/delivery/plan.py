"""归一化变更计划。

**这是「多云统一」真正发生的地方。** Terraform 产出 `terraform show -json`、
CLI 型平台产出自己的 declared-vs-live 比对结果，两者都转成本模块的 `Plan`——
于是审批的人看到的永远是同一种格式，不必为每个平台学一遍怎么读 diff。

格式一旦上线就很难改（它是审批产物、要进卡片和 Artifact），所以在只有两朵云的
时候就定下来，而不是等第三个平台接进来再重构审批流程。

安全要点：Terraform 会标记敏感属性（`before_sensitive` / `after_sensitive`），
本模块**必须**按标记打码。计划会进飞书卡片和 CI Artifact，泄漏面比 state 还大。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .errors import DeliveryError

ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_REPLACE = "replace"
ACTIONS = (ACTION_CREATE, ACTION_UPDATE, ACTION_DELETE, ACTION_REPLACE)

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

REDACTED = "«已打码»"

# 命中这些子串的资源类型视为「访问控制类」。仓库既有约定就是 access 层变更要更严格
# 审批，这里把它编码进风险分级，而不是靠评审人自己认出来。
_ACCESS_HINTS = ("ram_", "iam_", "policy", "role", "oidc", "saml", "access_key", "grant")


class PlanParseError(DeliveryError):
    """上游计划产物无法解析成归一化 Plan。"""


def _is_access_resource(kind: str) -> bool:
    lowered = kind.lower()
    return any(hint in lowered for hint in _ACCESS_HINTS)


def _classify_risk(action: str, kind: str) -> str:
    # 删除与替换都会让线上资源消失一段时间（替换 = 先删后建），一律高危。
    if action in (ACTION_DELETE, ACTION_REPLACE):
        return RISK_HIGH
    if action == ACTION_UPDATE:
        return RISK_HIGH if _is_access_resource(kind) else RISK_MEDIUM
    return RISK_MEDIUM if _is_access_resource(kind) else RISK_LOW


def _normalize_actions(actions: Sequence[str], *, address: str) -> Optional[str]:
    """Terraform 的 actions 是数组，替换表示成两个动作（顺序取决于 create_before_destroy）。"""
    items = [a for a in actions if a]
    if not items or items == ["no-op"]:
        return None
    if items == ["read"]:
        return None
    if set(items) == {"create", "delete"}:
        return ACTION_REPLACE
    if items == ["create"]:
        return ACTION_CREATE
    if items == ["update"]:
        return ACTION_UPDATE
    if items == ["delete"]:
        return ACTION_DELETE
    raise PlanParseError(f"{address}: 无法识别的 actions {items!r}")


def redact(value: Any, sensitive: Any) -> Any:
    """按 Terraform 的敏感标记递归打码。**不认识的结构一律按敏感处理。**

    `sensitive` 与 `value` 结构同形：某个位置为 true 表示该值（或整棵子树）敏感。

    判断必须用 `is`，不能用 `==` 或真值测试：`{} == False` 为假但 `{}` 本身是假值，
    早先那版写成 `REDACTED if sensitive else value`，于是 `{"pw": {}}`、`{"pw": 0}`、
    `{"pw": ""}` 这些**假值型**的怪结构全部原样输出明文——审计实测复现。只有 `False`
    和 `None` 这两个「明确说了不敏感」的取值才放行原值。

    公开（不带下划线）是刻意的：CLI 型平台的 adapter 也要能调它，否则它们构造的
    `Change` 会绕过打码，这条门禁在接第三个平台时就自然失效了。
    """
    if sensitive is True:
        return REDACTED
    if sensitive is False or sensitive is None:
        return _copy(value)
    if isinstance(value, dict) and isinstance(sensitive, Mapping):
        # 键缺失按 False（不敏感）处理：Terraform 只为敏感字段写标记，
        # 整份标记的存在性在 from_terraform 里已经校验过了。
        return {k: redact(v, sensitive.get(k, False)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(sensitive, list) and len(sensitive) == len(value):
        return [redact(v, s) for v, s in zip(value, sensitive)]
    # 结构对不上、或 sensitive 是其它任何取值 → 说不清哪部分敏感，整体打码。
    return REDACTED


def _copy(value: Any) -> Any:
    """返回容器的副本。

    直接返回原对象会让 Plan 与调用方传进来的 document 共享同一份 dict——后面给
    `after_unknown` 打标记时就写回了输入文档，且 frozen 的 Plan 仍可被外部改写。
    审批产物必须是只读快照。
    """
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


@dataclass(frozen=True)
class Change:
    action: str
    kind: str
    address: str
    name: str
    risk: str
    before: Any = None
    after: Any = None

    def __post_init__(self) -> None:
        # 非法 action 原先要等到 Plan.summary 才以 KeyError 炸开，堆栈指向汇总逻辑、
        # 看不出是哪条变更构造错了。在源头拦。
        if self.action not in ACTIONS:
            raise PlanParseError(f"{self.address}: 非法 action {self.action!r}")

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "kind": self.kind,
            "address": self.address,
            "name": self.name,
            "risk": self.risk,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class Plan:
    platform: str
    env: str
    changes: tuple = ()
    account: str = "default"
    blocked: tuple = ()
    source: str = ""
    warnings: tuple = field(default=())

    @property
    def summary(self) -> dict:
        counts = {action: 0 for action in ACTIONS}
        for change in self.changes:
            counts[change.action] += 1
        return counts

    @property
    def empty(self) -> bool:
        return not self.changes

    @property
    def high_risk(self) -> list:
        return [c for c in self.changes if c.risk == RISK_HIGH]

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "account": self.account,
            "env": self.env,
            "source": self.source,
            "summary": self.summary,
            "changes": [c.to_dict() for c in self.changes],
            "blocked": list(self.blocked),
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=False)


def _redact_side(change: Mapping[str, Any], side: str, *, address: str) -> Any:
    """取 before/after 并按同名的 *_sensitive 标记打码。

    **标记整个缺失时直接报错，不当作「都不敏感」。** 早先默认成 `False`，于是只要
    喂一份字段布局不同的计划（旧版 terraform、或者干脆不是 plan 文档），整份 before/after
    就会以明文进到审批卡片和 Artifact 里——而 `to_json()` 的去处正是这两个地方。
    「不知道哪部分敏感」必须当「都敏感」，这和本仓库 SSH 链校验那条 fail-closed 原则一致。
    """
    value = change.get(side)
    if value is None:
        return None
    key = f"{side}_sensitive"
    if key not in change:
        raise PlanParseError(
            f"{address}: 计划里缺少 `{key}`，无法判断哪些字段敏感。"
            f"请确认这是 `terraform show -json <planfile>` 的输出。"
        )
    return redact(value, change[key])


def from_terraform(
    document: Mapping[str, Any],
    *,
    platform: str,
    env: str,
    account: str = "default",
) -> Plan:
    """把 `terraform show -json <planfile>` 的输出转成归一化 Plan。"""
    if not isinstance(document, Mapping):
        raise PlanParseError("terraform 计划必须是 JSON 对象")
    if "resource_changes" not in document:
        # `terraform show -json` **不带 planfile** 时输出的是 state：有 `values`、
        # 没有 `resource_changes`。早先把这种情况当成「空计划」，于是喂错文件会得到
        # 「无变更 + 退出码 0」——流水线的合理反应是放行，这是条静默的 fail-open。
        if "values" in document:
            raise PlanParseError(
                "这看起来是 terraform **state**（有 `values`、无 `resource_changes`），"
                "不是变更计划。请用 `terraform show -json <planfile>` 的输出。"
            )
        raise PlanParseError(
            '计划里没有 `resource_changes`。空计划应当是 `"resource_changes": []`；'
            "键整个缺失说明这不是一份可识别的 terraform 计划。"
        )
    raw_changes = document["resource_changes"]
    if not isinstance(raw_changes, list):
        raise PlanParseError("`resource_changes` 必须是数组")

    changes, warnings = [], []
    for item in raw_changes:
        if not isinstance(item, Mapping):
            raise PlanParseError("`resource_changes` 的元素必须是对象")
        address = str(item.get("address") or "")
        if not address:
            raise PlanParseError("资源变更缺少 `address`")
        # data source 的读取不是变更，混进来会让审批看到一堆噪音。
        if item.get("mode") == "data":
            continue
        change = item.get("change")
        if not isinstance(change, Mapping):
            raise PlanParseError(f"{address}: 缺少 `change` 对象")
        actions = change.get("actions")
        if not isinstance(actions, list):
            raise PlanParseError(f"{address}: `change.actions` 必须是数组")
        action = _normalize_actions(actions, address=address)
        if action is None:
            continue
        kind = str(item.get("type") or "")
        before = _redact_side(change, "before", address=address)
        after = _redact_side(change, "after", address=address)
        # after_unknown 里为 true 的字段在 apply 前算不出来，如实标注比留空更清楚。
        unknown = change.get("after_unknown")
        if isinstance(unknown, Mapping) and isinstance(after, dict):
            for key, is_unknown in unknown.items():
                if is_unknown is True:
                    after[key] = "«apply 后才确定»"
        changes.append(
            Change(
                action=action,
                kind=kind,
                address=address,
                name=str(item.get("name") or ""),
                risk=_classify_risk(action, kind),
                before=before,
                after=after,
            )
        )

    blocked = []
    errored = document.get("errored")
    if errored:
        blocked.append("terraform 计划自身报告了错误（errored=true），不得据此 apply")
    version = document.get("format_version")
    if version and not str(version).startswith("1."):
        # 字段布局可能已变 → 敏感标记的位置也可能变 → 打码是否生效无法保证。
        # 「不知道对不对」当「不对」，进 blocked 而不是只提醒一句。
        blocked.append(
            f"terraform 计划格式版本 {version} 未经验证，敏感字段标记可能已变，不得据此 apply"
        )

    return Plan(
        platform=platform,
        env=env,
        account=account,
        changes=tuple(changes),
        blocked=tuple(blocked),
        source="terraform",
        warnings=tuple(warnings),
    )
