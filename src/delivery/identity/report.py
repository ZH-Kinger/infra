"""把对账结果渲染成人能读的报告，以及可分发的 CSV。"""

from __future__ import annotations

import csv
import io

from .audit import CLASS_MISMATCH, CLASS_MISSING_EMAIL, CLASS_OK, CLASS_SERVICE, AuditReport

_TITLE = {
    CLASS_OK: "可直接用于 SSO",
    CLASS_MISMATCH: "标识对不上，需本人确认",
    CLASS_MISSING_EMAIL: "云上没填邮箱，需本人补",
    CLASS_SERVICE: "疑似服务号（请人工确认）",
}


def render(report: AuditReport, *, limit: int = 0) -> str:
    counts = report.summary()
    lines = [
        f"企业域 {report.domain}",
        f"  可用 {counts[CLASS_OK]} · "
        f"对不上 {counts[CLASS_MISMATCH]} · "
        f"缺邮箱 {counts[CLASS_MISSING_EMAIL]} · "
        f"服务号 {counts[CLASS_SERVICE]}",
        "",
    ]
    for verdict in (CLASS_MISSING_EMAIL, CLASS_MISMATCH, CLASS_OK, CLASS_SERVICE):
        items = report.of(verdict)
        if not items:
            continue
        lines.append(f"── {_TITLE[verdict]}（{len(items)}）")
        shown = items if limit <= 0 else items[:limit]
        for f in shown:
            u = f.user
            lines.append(
                f"   {u.platform}/{u.account}  {u.name:<22} {u.label:<12} {f.note}".rstrip()
            )
        if limit > 0 and len(items) > limit:
            lines.append(f"   …另有 {len(items) - limit} 条（去掉 --limit 看全部）")
        lines.append("")

    if report.ready:
        lines.append("✓ 全部人员都能被 SSO 匹配 —— 满足开启用户SSO 的前置条件")
    else:
        blocked = counts[CLASS_MISMATCH] + counts[CLASS_MISSING_EMAIL]
        lines.append(
            f"✗ 还有 {blocked} 个人员对不上。用户SSO 靠这个标识匹配子用户，"
            f"不清零就会有人登录报「账户不存在」。"
        )
    return "\n".join(lines)


def to_csv(report: AuditReport) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["平台", "账号", "用户名", "显示名", "云上邮箱", "结论", "说明"])
    for f in report.findings:
        u = f.user
        writer.writerow(
            [u.platform, u.account, u.name, u.display_name, u.email, _TITLE[f.verdict], f.note]
        )
    return buf.getvalue()
