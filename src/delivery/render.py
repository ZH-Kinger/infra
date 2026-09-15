"""把归一化 Plan 渲染成人能读的样子。

审批的人不该被要求读 `terraform plan` 原文——几百行 diff 里真正需要判断的往往只有
一两条。渲染的优先级是：**先给结论，再给高危项与后果，最后才是明细。**

同一个 Plan 会被渲染到三个地方（PR 评论、飞书卡片、终端），所以渲染与数据分离：
本模块只产出纯文本，卡片那层自己套壳。
"""

from __future__ import annotations

from .plan import ACTION_CREATE, ACTION_DELETE, ACTION_REPLACE, ACTION_UPDATE, RISK_HIGH, Plan

_ACTION_LABEL = {
    ACTION_CREATE: ("+", "创建"),
    ACTION_UPDATE: ("~", "修改"),
    ACTION_DELETE: ("-", "删除"),
    ACTION_REPLACE: ("±", "替换"),
}

# 高危变更为什么值得停下来看一眼。写「后果」而不是只标红：标红只让人知道危险，
# 写清后果才让人判断得了该不该放行。
_CONSEQUENCE = {
    ACTION_DELETE: "资源会消失，依赖它的任务会失败",
    ACTION_REPLACE: "先删后建，期间不可用；有状态资源可能丢数据",
}


def render_plan(plan: Plan, *, detail_limit: int = 20) -> str:
    lines = [f"{plan.platform} / {plan.account} / {plan.env}"]

    if plan.empty:
        lines.append("  无变更（线上与代码一致）")
        _append_notices(plan, lines)
        return "\n".join(lines)

    summary = plan.summary
    for action in (ACTION_CREATE, ACTION_UPDATE, ACTION_DELETE, ACTION_REPLACE):
        count = summary[action]
        if count:
            mark, label = _ACTION_LABEL[action]
            lines.append(f"  {mark} {label} {count} 个")

    high = plan.high_risk
    if high:
        lines.append("")
        lines.append(f"⚠ {len(high)} 项需要二次确认")
        for change in high[:detail_limit]:
            _, label = _ACTION_LABEL[change.action]
            lines.append(f"    {label}  {change.address}  ({change.kind})")
            consequence = _CONSEQUENCE.get(change.action)
            if consequence:
                lines.append(f"      → {consequence}")
        if len(high) > detail_limit:
            lines.append(f"    …另有 {len(high) - detail_limit} 项")

    low = [c for c in plan.changes if c.risk != RISK_HIGH]
    if low:
        lines.append("")
        lines.append("其余变更")
        for change in low[:detail_limit]:
            mark, _ = _ACTION_LABEL[change.action]
            lines.append(f"    {mark} {change.address}  ({change.kind})")
        if len(low) > detail_limit:
            lines.append(f"    …另有 {len(low) - detail_limit} 项")

    _append_notices(plan, lines)
    return "\n".join(lines)


def _append_notices(plan: Plan, lines: list) -> None:
    for warning in plan.warnings:
        lines.append(f"注意：{warning}")
    for reason in plan.blocked:
        lines.append(f"✗ 已阻断：{reason}")
