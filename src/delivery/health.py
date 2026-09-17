"""管理后台「系统状态」：上线前后逐项看平台还缺什么配置、数据是不是太旧、有没有卡住的申请单。

只读本地文件和环境变量是否存在，**不调任何云或飞书接口，也不返回任何密钥的值**。
每项给出 level（ok / warn / crit / off）、一句现状和一句怎么修。
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Optional

from . import catalog as catalog_mod
from . import flows as flows_mod
from . import platforms, sealed
from . import tickets as t
from .approval import ApprovalConfig
from .errors import DeliveryError
from .notify import ENV_BASE_URL, ENV_NOTIFY, safe_base_url
from .provision import exec_env_prefix, issuer_env_prefix

OK, WARN, CRIT, OFF = "ok", "warn", "crit", "off"
#: 快照超过这么久算过期（定时任务每天跑，两天没更新说明任务停了）
STALE_AFTER = 2 * 86400
_STUCK_AFTER = 30 * 60


@dataclass(frozen=True)
class Check:
    group: str
    title: str
    level: str
    detail: str
    fix: str = ""

    def view(self) -> dict:
        return {
            "group": self.group,
            "title": self.title,
            "level": self.level,
            "detail": self.detail,
            "fix": self.fix,
        }


def _ts(iso: object) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _age(iso: object, now: float) -> str:
    ts = _ts(iso)
    if not ts:
        return "时间未知"
    hours = max(0.0, now - ts) / 3600
    return f"{int(hours)} 小时前" if hours < 48 else f"{int(hours // 24)} 天前"


def _snapshot_check(group: str, title: str, captured_at: str, now: float, fix: str) -> Check:
    if not captured_at:
        return Check(group, title, CRIT, "还没有生成", fix)
    stale = now - _ts(captured_at) > STALE_AFTER
    return Check(
        group,
        title,
        WARN if stale else OK,
        f"采集于 {_age(captured_at, now)}",
        fix if stale else "",
    )


def _safe(group: str, title: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except DeliveryError as exc:
        first = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
        # 读文件失败类的消息带服务器路径和原始异常：浏览器里只留冒号前的那句，全文进服务端日志
        print(f"[health] {title}：{first}", file=sys.stderr)
        if "/" in first or "\\" in first:
            first = re.sub(r"\S*[/\\]\S*", "（文件）", first.split("：", 1)[0])
        return Check(
            group, title, CRIT, first[:200] or "读取失败", "检查对应文件格式，详情见服务端日志"
        )
    except Exception as exc:  # noqa: BLE001 — 状态页本身不能 500
        return Check(group, title, CRIT, f"读取失败：{type(exc).__name__}", "查看服务端日志")


def collect(
    backend,
    *,
    auth_mode: str,
    approval_ready: Optional[bool] = None,
    environ: Optional[Mapping] = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    env = os.environ if environ is None else environ
    now = clock()
    checks: list = []
    accounts: set = set()

    # ── 登录与审批 ────────────────────────────────────────────────────────
    checks.append(
        Check(
            "登录与审批",
            "登录方式",
            OK,
            "公司 IAM（oauth2-proxy）" if auth_mode == "proxy" else "飞书登录",
        )
    )

    def approval() -> Check:
        config = ApprovalConfig.load(backend.approval_path)
        if config is None:
            return Check(
                "登录与审批",
                "飞书审批",
                CRIT,
                "没有配置，员工提交申请会被拒绝",
                "在飞书审批后台建审批定义，用 delivery approval widgets 查控件 ID，"
                "写 identity/approval.json",
            )
        if approval_ready is False:
            return Check(
                "登录与审批",
                "飞书审批",
                CRIT,
                "审批配置在，但面板缺飞书应用凭证：员工提交申请会被拒绝",
                "配置 DELIVERY_FEISHU_APP_ID / DELIVERY_FEISHU_APP_SECRET（公司 IAM 登录时也需要）",
            )
        if config.allow_self_approval:
            return Check(
                "登录与审批",
                "飞书审批",
                WARN,
                "已配置，但允许申请人自己审批（allow_self_approval）",
                "除非审批定义本身有他人把关，否则去掉 allow_self_approval",
            )
        return Check("登录与审批", "飞书审批", OK, "已配置，开通前要求申请人以外的审批人同意")

    checks.append(_safe("登录与审批", "飞书审批", approval))

    # ── 申请内容 ──────────────────────────────────────────────────────────
    def templates() -> Check:
        catalog = backend.catalog()
        items = list(catalog.templates)
        for tpl in items:
            accounts.add((tpl.platform, tpl.account))
        if not items:
            return Check(
                "申请内容",
                "申请模板",
                WARN,
                "没有模板：「申请」页只能看到空状态（权限列表不受影响）",
                "按 identity/request-templates.example.json 写 identity/request-templates.json",
            )
        kinds = Counter(tpl.kind for tpl in items)
        # 用 catalog 那份唯一的对照表，别在这儿抄第二份 —— 上一份就漏了 resource，
        # 体检页把资源模板显示成 "resource 3"
        parts = "、".join(f"{catalog_mod.KIND_LABELS.get(k, k)} {n}" for k, n in kinds.items())
        return Check("申请内容", "申请模板", OK, f"{len(items)} 个：{parts}")

    checks.append(_safe("申请内容", "申请模板", templates))

    def policy_directory() -> Check:
        data = backend.policies()
        if data is None:
            return Check(
                "申请内容",
                "权限策略列表",
                WARN,
                "还没采集：员工的「权限列表」页是空的",
                "运行 delivery policies collect --out identity/policies.json，并加进定时任务",
            )
        bad = []
        for acc in data.get("accounts") or []:
            key = (str(acc.get("platform") or ""), str(acc.get("account") or ""))
            if acc.get("error"):
                bad.append(f"{key[0]}/{key[1]} 采集失败")
            else:
                accounts.add(key)
                if acc.get("stale"):
                    bad.append(f"{key[0]}/{key[1]} 沿用旧列表")
        base = _snapshot_check(
            "申请内容",
            "权限策略列表",
            str(data.get("captured_at") or ""),
            now,
            "检查 delivery policies collect 定时任务",
        )
        if bad:
            return Check(
                "申请内容",
                "权限策略列表",
                WARN,
                f"{base.detail}；{'；'.join(bad)}",
                base.fix or "检查采集身份权限",
            )
        return base

    checks.append(_safe("申请内容", "权限策略列表", policy_directory))

    # ── 数据快照 ──────────────────────────────────────────────────────────
    def inventory() -> Check:
        snap = backend.snapshot()
        if snap is None:
            return Check(
                "数据",
                "权限快照",
                CRIT,
                "还没生成：员工看不到自己的权限，「已拥有」也判断不了",
                "运行 delivery refresh（deploy/panel 里的 delivery-refresh.timer）",
            )
        for u in snap.users:
            accounts.add((u.platform, u.account))
        base = _snapshot_check(
            "数据", "权限快照", snap.captured_at, now, "检查 delivery-refresh 定时任务"
        )
        if snap.incomplete:
            return Check(
                "数据",
                "权限快照",
                WARN,
                f"{base.detail}；{len(snap.incomplete)} 个云账号没采全",
                "检查采集身份权限",
            )
        return base

    checks.append(_safe("数据", "权限快照", inventory))

    def assets() -> Check:
        data = backend.assets()
        if data is None:
            return Check(
                "数据",
                "云资产",
                OFF,
                "没有配置资产快照（可选）",
                "需要时运行 delivery assets collect --out identity/assets.json",
            )
        failed = [a for a in data.get("accounts") or [] if a.get("error")]
        base = _snapshot_check(
            "数据", "云资产", str(data.get("captured_at") or ""), now, "检查资产采集定时任务"
        )
        if failed:
            return Check(
                "数据",
                "云资产",
                WARN,
                f"{base.detail}；{len(failed)} 个云账号采集失败",
                "确认资源中心已开通",
            )
        return base

    checks.append(_safe("数据", "云资产", assets))

    # ── 执行身份与发放身份（只看环境变量是否存在，不调云）─────────────────
    #
    # 两把是分开的：开通身份在云上被**故意**禁掉建号/发 AK/造策略，而长期访问凭证
    # 恰恰需要这三样。只体检开通身份的话，一个只发长期凭证的模板可以一路显示正常，
    # 却在开通那一刻才失败。
    # 模板目录读不了时按空集算：体检页本来就有专门一项报模板的问题，
    # 不该因为同一个原因让整页打不开
    # 哪些云账号**必须**有发放身份：模板没配角色（只能走长期凭证）→ 缺了就完全发不出，
    # 算 crit；配了角色但允许超过 12 小时 → 短的能发、长的会失败，算 warn。
    # 不分级的话，一个只发 12 小时以内凭证的正常平台会被报成严重故障。
    needs_issuer: dict = {}
    #: 有没有凭证模板。「查看地址」和「加密」两项对**所有**凭证都必需（STS 也要），
    #: 挂在 needs_issuer 下面是错的：一个配了角色、max_hours=12 的部署那张表是空的
    has_credential = False
    try:
        for tpl in backend.catalog().of_kind("credential"):
            has_credential = True
            key = (tpl.platform, tpl.account)
            if not tpl.role_arn:
                needs_issuer[key] = CRIT
            elif tpl.max_hours > catalog_mod.STS_MAX_HOURS:
                needs_issuer.setdefault(key, WARN)
    except Exception:  # noqa: BLE001 — 体检页任何一项都不能让整页挂掉
        needs_issuer = {}
    for platform, account in sorted(a for a in accounts if a[0] and a[1]):
        title = f"{platforms.name_of(platform)} {account}"
        prefix = exec_env_prefix(platform, account)
        if all(env.get(n) for n in platforms.cred_env_names(platform, prefix)):
            checks.append(Check("执行身份", title, OK, "已配置（开通前会核对凭证属于这个云账号）"))
        else:
            checks.append(
                Check(
                    "执行身份",
                    title,
                    CRIT,
                    "没有配置：审批通过的申请在这个云账号上会开通失败",
                    "按 deploy/panel/executor-policy.*.example.json 建最小权限身份，"
                    f"配置 {prefix}_*",
                )
            )
        level = needs_issuer.get((platform, account))
        if level is None:
            continue
        prefix = issuer_env_prefix(platform, account)
        if all(env.get(n) for n in platforms.cred_env_names(platform, prefix)):
            checks.append(
                Check("发放身份", title, OK, "已配置（只能动 tempak-* 子账号和它的策略）")
            )
        else:
            checks.append(
                Check(
                    "发放身份",
                    title,
                    level,
                    "没有配置：这个云账号的长期访问凭证发不出去"
                    + (
                        "" if level == CRIT else f"（{catalog_mod.STS_MAX_HOURS} 小时以内的仍可发）"
                    ),
                    f"建一把只能动 tempak-* 的身份，配置 {prefix}_*",
                )
            )

    # ── 凭证交付 ──────────────────────────────────────────────────────────
    # 这两项任何一项缺了，访问凭证类申请就会在审批通过那一刻失败。放在这里是因为
    # 它们和云账号无关、装完就不会再变，而缺的时候症状（「申请提交被拒」）很难指回原因
    if has_credential:
        try:
            flows_mod.view_base(env)
        except DeliveryError as exc:
            checks.append(
                Check(
                    "凭证交付",
                    "查看地址",
                    CRIT,
                    f"{exc}：访问凭证一律不受理",
                    f"把 {ENV_BASE_URL} 配成面板的对外 https 地址",
                )
            )
        else:
            checks.append(Check("凭证交付", "查看地址", OK, "已配置面板对外地址"))
        try:
            sealed.selfcheck()
        except Exception as exc:  # noqa: BLE001 — 体检页任何一项都不能让整页挂掉
            checks.append(
                Check(
                    "凭证交付",
                    "加密",
                    CRIT,
                    f"加密不可用：访问凭证一律不受理（{exc}）",
                    "在面板的运行环境里装 cryptography",
                )
            )
        else:
            checks.append(Check("凭证交付", "加密", OK, "AES-256-GCM 自检通过"))

    # ── 通知 ──────────────────────────────────────────────────────────────
    if env.get(ENV_NOTIFY) != "1":
        checks.append(
            Check(
                "通知",
                "申请状态通知",
                OFF,
                "未开启：员工要自己回平台看进度",
                f"设置 {ENV_NOTIFY}=1",
            )
        )
    else:
        base_url = safe_base_url(env.get(ENV_BASE_URL, ""))
        has_app = bool(env.get("DELIVERY_FEISHU_APP_ID")) and bool(
            env.get("DELIVERY_FEISHU_APP_SECRET")
        )
        if base_url and has_app:
            checks.append(Check("通知", "申请人私信", OK, "已开启"))
        else:
            checks.append(
                Check(
                    "通知",
                    "申请人私信",
                    WARN,
                    "已开启但发不出去：缺飞书应用凭证或 https 面板地址",
                    f"配置 DELIVERY_FEISHU_APP_ID / SECRET 和 https 的 {ENV_BASE_URL}",
                )
            )
        if env.get("DELIVERY_ALERT_WEBHOOK"):
            checks.append(Check("通知", "管理员告警", OK, "开通失败会发到告警群"))
        else:
            checks.append(
                Check(
                    "通知",
                    "管理员告警",
                    OFF,
                    "没有配置告警群（可选）",
                    "配置 DELIVERY_ALERT_WEBHOOK",
                )
            )

    # ── 申请单 ────────────────────────────────────────────────────────────
    def queue() -> Check:
        flows = backend.flows()
        if flows is None:
            return Check(
                "申请单",
                "申请单存储",
                OFF,
                "没有配置申请单文件：申请功能关闭",
                "serve 时指定 --tickets",
            )
        items = flows.store.all()
        by = Counter(x.get("status") for x in items)
        stuck = [
            x
            for x in items
            if x.get("status") in (t.EXECUTING, t.SUBMITTING, t.APPROVED)
            and now - _ts(x.get("updated_at")) > _STUCK_AFTER
        ]
        failed = by.get(t.FAILED, 0) + by.get(t.SUBMIT_FAILED, 0)
        detail = f"共 {len(items)} 张，进行中 {sum(by.get(s, 0) for s in t.OPEN)} 张"
        if stuck:
            return Check(
                "申请单",
                "申请单",
                CRIT,
                f"{detail}；{len(stuck)} 张超过 30 分钟没有进展",
                "确认 delivery requests sweep 定时任务在跑；在「申请与开通」里处理",
            )
        if failed:
            return Check(
                "申请单",
                "申请单",
                WARN,
                f"{detail}；{failed} 张失败待处理",
                "在「申请与开通」的「需要处理」里查看",
            )
        return Check("申请单", "申请单", OK, detail)

    checks.append(_safe("申请单", "申请单", queue))

    levels = Counter(c.level for c in checks)
    return {
        "checked_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "summary": {k: levels.get(k, 0) for k in (OK, WARN, CRIT, OFF)},
        "checks": [c.view() for c in checks],
    }
