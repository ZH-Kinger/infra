"""投递层 CLI：看平台能力、看自己该怎么登。

刻意和 `dataset_sink.cli` 分开：那个是数据集发布的操作台，这个是平台与身份的查询入口。
两者共用错误基类，但没有互相 import 业务逻辑。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import unicodedata
from pathlib import Path
from typing import Optional, Sequence

from .access import STATE_ACTION, STATE_BLOCKED, STATE_READY, guide
from .capabilities import LOGIN_BIND
from .errors import DeliveryError
from .login import BackendExchange, login
from .plan import PlanParseError, from_terraform
from .registry import PlatformRegistry
from .render import render_plan
from .scopes import SCOPE_FOUNDATION, SCOPES
from .scopes import describe as describe_scope
from .session import (
    bound_platforms,
    clear_session,
    describe_credential,
    drop_credential,
    home,
    load_session,
    record_bind_audit,
    save_credential,
)


def _width(text: str) -> int:
    """终端显示宽度。CJK 与全角标点占两列，用 len() 对齐会歪掉。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    """按显示宽度右补空格；超宽则原样返回，不截断（截断平台名比不对齐更糟）。"""
    return text + " " * max(0, width - _width(text))


def _wrap(text: str, width: int = 72) -> list:
    """按**显示宽度**折行。

    不能用 textwrap：它按字符数算，而中文一个字占两列——72 个中文字会排到 144 列，
    在标准终端里照样炸。中文也没有空格可断，只能逐字累加宽度。
    """
    lines, cur, cur_w = [], "", 0
    for ch in text:
        w = _width(ch)
        if cur_w + w > width and cur:
            lines.append(cur)
            cur, cur_w = "", 0
        cur += ch
        cur_w += w
    if cur:
        lines.append(cur)
    return lines or [""]


_STATE_MARK = {STATE_READY: "✓", STATE_ACTION: "!", STATE_BLOCKED: "✗"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delivery",
        description="多平台投递：平台能力与登录引导",
    )
    parser.add_argument(
        "--platforms-dir", default=None, help="平台描述符目录（默认仓库 platforms/）"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON，供脚本与看板消费")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("platforms", help="列出已注册平台及其能力")

    login = commands.add_parser("login-guide", help="显示每个平台该怎么登录")
    login.add_argument(
        "--bound",
        default="",
        help="已托管凭证的平台 id，逗号分隔（真实场景由身份表提供）",
    )
    login.add_argument(
        "--no-account",
        default="",
        help="该用户尚无账号的平台 id，逗号分隔",
    )
    login.add_argument(
        "--sso-disabled",
        default="",
        help="SAML 尚未上线的平台 id，逗号分隔",
    )
    ident = commands.add_parser("identity", help="身份对账：云上账号与「人」的对应关系")
    isub = ident.add_subparsers(dest="identity_command", required=True)
    iaudit = isub.add_parser("audit", help="指出谁缺邮箱、谁的标识对不上")
    iaudit.add_argument("--platform", default="", help="从阿里云实时采集（需本机 aliyun CLI）")
    iaudit.add_argument("--account", default="default", help="账号标识，仅作标注")
    iaudit.add_argument("--profile", default="", help="aliyun CLI 的 profile 名")
    iaudit.add_argument(
        "--from-json",
        action="append",
        default=[],
        metavar="FILE",
        help="读离线导出的账号清单，可重复；火山这类没有 CLI 的平台走它",
    )
    iaudit.add_argument("--domain", default="@wuji.tech", help="企业邮箱域名")
    iaudit.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="NAME",
        help="显式标记为服务号的用户名，可重复（启发式判错时用它兜底）",
    )
    iaudit.add_argument("--limit", type=int, default=0, help="每类最多显示几条，0=全部")
    iaudit.add_argument("--csv", default="", metavar="FILE", help="同时导出 CSV")

    imap = isub.add_parser("map", help="生成「飞书邮箱 → 云用户名」映射表")
    imap.add_argument(
        "--from-json", action="append", default=[], metavar="FILE", help="账号清单，可重复"
    )
    imap.add_argument("--overrides", default="", metavar="FILE", help="显式登记表")
    imap.add_argument("--domain", default="@wuji.tech")
    imap.add_argument(
        "--stub", action="store_true", help="只输出推不出来的那些，格式可直接粘进 overrides 文件"
    )
    imap.add_argument("--csv", default="", metavar="FILE", help="导出 CSV（飞书工号批量导入用）")

    isso = isub.add_parser(
        "sso-map", help="生成登录用映射提案：实时采集两朵云，列出已确认 / 需确认 / 拦截"
    )
    isso.add_argument("--domain", default="wuji.tech", help="企业邮箱域名")
    isso.add_argument(
        "--out",
        default="identity/sso-map.proposal.json",
        help="提案输出路径。含员工邮箱，默认位置已 gitignore",
    )
    isso.add_argument("--skip", action="append", default=[], choices=["aliyun", "volcano"])
    isso.add_argument(
        "--service", action="append", default=[], metavar="NAME", help="额外标记为服务号的用户名"
    )
    isso.add_argument("--show-confirmed", action="store_true", help="清单里也列出已确认的")
    isso.add_argument(
        "--trust-unverified-when-derivable",
        action="store_true",
        help="企业邮箱未验证、但用户名可由它推出时也直接确认（默认需人工确认）",
    )

    srv = commands.add_parser("serve", help="本地开发服务器：看板 + 飞书登录")
    srv.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    srv.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址。默认只绑回环——看板会显示账号与权限，不该暴露给网段",
    )
    srv.add_argument(
        "--inventory",
        default="identity/inventory.json",
        help="权限快照（delivery inventory collect 生成）",
    )
    srv.add_argument(
        "--people", default="identity/people.json", help="人员名册（delivery identity people 生成）"
    )
    srv.add_argument("--admins", default=None, help="管理员名单，默认 identity/admins.json")
    srv.add_argument(
        "--labels",
        default="identity/accounts.json",
        help='云账号显示名，形如 {"aliyun/<UID>": "主账号"}',
    )

    inv = commands.add_parser("inventory", help="权限快照：采集两朵云上每个子账号的权限")
    invsub = inv.add_subparsers(dest="inventory_command", required=True)
    invc = invsub.add_parser("collect", help="实时采集，写入快照文件（只读调用云 API）")
    invc.add_argument("--out", default="identity/inventory.json")
    invc.add_argument(
        "--aliyun-profile",
        action="append",
        default=[],
        metavar="PREFIX",
        help="阿里云凭证环境变量前缀，可重复。默认 ALIYUN；第二个账号如 ALIYUN_SECOND",
    )
    invc.add_argument("--skip", action="append", default=[], choices=["aliyun", "volcano"])

    iamx = isub.add_parser(
        "iam-export", help="从人员名册导出给 WUJI IAM 导入的用户属性表（每人各云用户名）"
    )
    iamx.add_argument("--people", default="identity/people.json")
    iamx.add_argument(
        "--attributes",
        default="identity/iam-attributes.json",
        help='云账号 → IAM 属性名，形如 {"aliyun/<UID>": "aliyun_username"}',
    )
    iamx.add_argument("--out", default="identity/iam-attributes.csv")

    ppl = isub.add_parser("people", help="生成人员名册：映射提案 + 通讯录 union_id")
    ppl.add_argument("--proposal", default="identity/sso-map.proposal.json")
    ppl.add_argument(
        "--directory",
        default="feishu",
        help="union_id 来源：feishu（调通讯录接口）、csv:<IT 导出的对照表>、none（首次登录再关联）",
    )
    ppl.add_argument("--out", default="identity/people.json")
    ppl.add_argument(
        "--manual",
        default="identity/manual-links.json",
        help="人工确认的对应（优先于规则推断），格式见 people.apply_manual",
    )

    lg = commands.add_parser("login", help="用飞书账号登录（浏览器授权）")
    lg.add_argument("--server", default="", help="后端地址，默认取 DELIVERY_SERVER")
    lg.add_argument("--app-id", default="", help="飞书 App ID，默认取 DELIVERY_FEISHU_APP_ID")
    lg.add_argument("--port", type=int, default=8765, help="本机回调端口（须与飞书白名单一致）")
    lg.add_argument(
        "--redirect-uri",
        default="",
        help="直接粘贴飞书白名单里那一条，逐字一致；给了它就忽略 --port",
    )
    lg.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器，只打印授权链接（SSH 到远端时用）",
    )

    doc = commands.add_parser("doctor", help="飞书应用体检：一条命令探完这个应用会调的所有接口")
    doc.add_argument("--app-id", default="", help="默认取 DELIVERY_FEISHU_APP_ID")
    doc.add_argument(
        "--send-to",
        default="",
        help="真发一条测试消息到这个邮箱。唯一决定性的验证——探针只能靠错误码推断",
    )

    commands.add_parser("logout", help="清除本机会话")
    commands.add_parser("status", help="显示当前身份与各平台登录状态")

    bind = commands.add_parser("bind", help="托管某平台的凭证（接不了 SSO 的平台用）")
    bind.add_argument("platform", help="平台 id")
    bind.add_argument(
        "--i-know-this-is-worse",
        action="store_true",
        help="对支持 SSO 的平台强行托管长期凭证。会在本机留一条审计记录",
    )
    bind.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY",
        help="要录入的字段名，可重复；值从终端安全读入，不进 shell 历史",
    )

    unbind = commands.add_parser("unbind", help="删除某平台已托管的凭证")
    unbind.add_argument("platform", help="平台 id")

    matrix = commands.add_parser(
        "matrix", help="按能力筛出平台列表，供 GitHub Actions 的 strategy.matrix 消费"
    )
    matrix.add_argument("--iac", default="", help="只保留该 iac 形态的平台，如 terraform")
    matrix.add_argument("--appliable", action="store_true", help="只保留允许 apply 的平台")
    matrix.add_argument(
        "--scope",
        choices=SCOPES,
        default="",
        help="按变更范围筛：foundation=固定资产(管理员) / workspace=日常资源(用户)",
    )

    describe = commands.add_parser(
        "describe", help="输出某平台的能力，供 GitHub Actions 按能力分支"
    )
    describe.add_argument("platform", help="平台 id")
    describe.add_argument(
        "--scope",
        choices=SCOPES,
        default=SCOPE_FOUNDATION,
        help="变更范围，默认 foundation（最严的一档）",
    )
    describe.add_argument(
        "--github-output",
        action="store_true",
        help="按 key=value 逐行输出，可直接重定向进 $GITHUB_OUTPUT",
    )

    show = commands.add_parser(
        "plan-show", help="把 terraform show -json 的输出渲染成人能读的变更摘要"
    )
    show.add_argument("file", help="terraform show -json 产出的 JSON 文件")
    show.add_argument("--platform", required=True, help="平台 id")
    show.add_argument("--env", default="prod", help="环境名，默认 prod")
    show.add_argument("--account", default="default", help="账号标识，默认 default")
    # --json 同时挂在子命令上：只挂顶层的话 `plan-show --json f.json` 会报
    # unrecognized arguments，而 CI 里写反了就是硬失败。
    # default=SUPPRESS 是必须的：argparse 的**子解析器默认值会覆盖父解析器已解析的值**，
    # 用普通的 store_true（默认 False）会让 `--json describe x` 里的顶层 --json 被冲掉。
    for sub in (describe, show, iaudit, imap):
        sub.add_argument(
            "--json",
            action="store_true",
            dest="json",
            default=argparse.SUPPRESS,
            help="输出 JSON",
        )
    return parser


def _split(raw: str) -> set:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def _cmd_platforms(registry: PlatformRegistry, as_json: bool) -> int:
    if as_json:
        payload = [
            {
                "id": p.id,
                "display": p.display,
                "accounts": list(p.accounts),
                "capabilities": vars(p.capabilities),
                "notes": list(p.notes),
            }
            for p in registry
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    cols = (
        ("平台", 26),
        ("凭证", 15),
        ("IaC", 10),
        ("预览", 8),
        ("清单", 6),
        ("apply", 7),
        ("状态", 0),
    )
    print("".join(_pad(name, w) for name, w in cols).rstrip())
    print("-" * 78)
    for p in registry:
        c = p.capabilities
        cells = (p.display, c.auth, c.iac, c.plan, c.inventory, str(c.apply), c.status)
        print("".join(_pad(v, w) for v, (_, w) in zip(cells, cols)).rstrip())
    print()
    print("变更范围（同一朵云上，用户和管理员走两套门禁）")
    for p in registry:
        parts = []
        for name in SCOPES:
            sc = p.scopes.get(name)
            if sc is None:
                continue
            if not sc.apply:
                state = "只读"
            elif sc.require_approval:
                state = "需审批"
            elif sc.quota_gated:
                state = "配额内自助"
            else:
                state = "自助"
            parts.append(f"{name}={state}")
        print("  " + _pad(p.display, 26) + "  ".join(parts))

    pending = [p for p in registry if p.capabilities.status != "verified"]
    if pending:
        print()
        for p in pending:
            print(f"!  {p.display} 的能力声明尚未真机验证，apply 保持关闭直到验证通过")
    return 0


def _cmd_login_guide(registry: PlatformRegistry, args: argparse.Namespace) -> int:
    bound = _split(args.bound)
    no_account = _split(args.no_account)
    sso_disabled = _split(args.sso_disabled)
    for name in bound | no_account | sso_disabled:
        registry.get(name)  # 拼错平台名要立刻报错，不要静默当成「没绑」
    guides = [
        guide(
            p,
            has_account=p.id not in no_account,
            bound=p.id in bound,
            sso_enabled=p.sso_enabled and p.id not in sso_disabled,
        )
        for p in registry
    ]
    if args.json:
        print(json.dumps([g.to_dict() for g in guides], ensure_ascii=False, indent=2))
        return 0
    for g in guides:
        print(f"{_STATE_MARK[g.state]}  {g.display}    [{g.action_label}]")
        print(f"   {g.headline}")
        for line in g.hint.splitlines():
            # 提示文本本身不换行（看板要自己排版），只在终端输出时按宽度折行。
            indent = " " * (len(line) - len(line.lstrip()))
            for chunk in _wrap(line.strip(), 72):
                print(f"   {indent}{chunk}")
        if g.next_command:
            print(f"   → {g.next_command}")
        print()
    todo = [g for g in guides if g.state == STATE_ACTION]
    if todo:
        print("需要你做一次的：" + "、".join(g.display for g in todo))
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    server = args.server or os.environ.get("DELIVERY_SERVER") or ""
    if not server:
        raise DeliveryError(
            "未指定后端地址。用 --server http://<host>:<port>，或设置环境变量 DELIVERY_SERVER。"
        )
    app_id = args.app_id or os.environ.get("DELIVERY_FEISHU_APP_ID") or ""
    session = login(
        BackendExchange(server),
        app_id=app_id,
        server=server,
        port=args.port,
        redirect_uri=args.redirect_uri,
        open_browser=not args.no_browser,
    )
    print(f"\n  ✓ 已登录：{session.name or session.union_id}")
    print(f"    会话有效期至 {_fmt_ts(session.expires_ts)}")
    return 0


def _cmd_logout() -> int:
    print("已清除本机会话" if clear_session() else "本机没有已保存的会话")
    return 0


def _cmd_status(registry: PlatformRegistry) -> int:
    session = load_session()
    if session is None:
        print("未登录。运行： delivery login")
    elif session.expired:
        print(f"会话已过期（{session.name or session.union_id}），请重新 delivery login")
    else:
        hours = session.remaining_seconds // 3600
        print(f"已登录：{session.name or session.union_id}    剩余 {hours} 小时")
    print()
    bound = bound_platforms()
    for platform in registry:
        g = guide(platform, bound=platform.id in bound, sso_enabled=platform.sso_enabled)
        mark = _STATE_MARK[g.state]
        extra = ""
        if platform.id in bound:
            info = describe_credential(platform.id)
            if info.get("identity"):
                extra = f"  ({info['identity']})"
        print(f"{mark}  {_pad(platform.display, 26)}{g.headline}{extra}")
        if g.state != STATE_READY and g.next_command:
            print(f"   → {g.next_command}")
    return 0


def _cmd_bind(registry: PlatformRegistry, args: argparse.Namespace) -> int:
    platform = registry.get(args.platform)

    # 托管长期密钥是这套东西里**唯一**会把可长期使用的凭证放到个人电脑上的动作。
    # 它同时也是最省事的一条路——所以如果不挡，SSO 建好之后大家还是会继续 bind，
    # 整套身份收敛就白做了。判据用描述符里现成的 login 字段，不另立一套。
    if platform.capabilities.login != LOGIN_BIND and not args.i_know_this_is_worse:
        raise DeliveryError(
            f"{platform.display} 不该托管长期凭证：它的登录方式是 "
            f"`{platform.capabilities.login}`，不是 `bind`。\n"
            f"长期密钥放在个人电脑上，丢一台机器等于丢一份可长期使用的云权限。\n"
            f"请改用 `delivery login` 登录后换取临时凭证。\n"
            f"确实要绕过（平台 SSO 还没建好、临时救火），加 --i-know-this-is-worse，"
            f"它会在本机留一条审计记录。"
        )

    fields = args.field or _default_fields(platform)
    if not fields:
        raise DeliveryError(
            f"不知道 {platform.display} 需要哪些字段，请用 --field 指定，例如 "
            f"--field access_key --field secret_key"
        )
    payload = {}
    print(f"录入 {platform.display} 的凭证（输入不回显，也不会进 shell 历史）：")
    for key in fields:
        value = getpass.getpass(f"  {key}: ").strip()
        if not value:
            raise DeliveryError(f"字段 {key} 不能为空")
        payload[key] = value
    save_credential(platform.id, payload)
    # 审计只记「谁、什么时候、哪个平台、是不是绕过了护栏」，**绝不记字段值**。
    # 目的是将来收敛长期密钥时知道去找谁清理，不是为了追责。
    record_bind_audit(platform.id, bypassed=args.i_know_this_is_worse)
    print(f"✓ 已托管 {platform.display} 的凭证（{home()}/credentials.json，权限 600）")
    if args.i_know_this_is_worse:
        print(
            f"⚠ 你绕过了长期凭证护栏。本机已记一条审计（{home()}/bind-audit.log）。\n"
            f"  {platform.display} 的 SSO 一旦可用，请 `delivery unbind {platform.id}`。"
        )
    return 0


def _default_fields(platform) -> list:
    """按 adapter 声明推断要录入的字段；推断不出就要求显式给 --field。"""
    adapter = platform.adapter or {}
    if adapter.get("kind") == "cli" and adapter.get("binary") == "aladdin":
        return ["access_key", "secret_key"]
    return []


def _cmd_unbind(registry: PlatformRegistry, args: argparse.Namespace) -> int:
    platform = registry.get(args.platform)
    if drop_credential(platform.id):
        print(f"已删除 {platform.display} 的托管凭证")
        return 0
    print(f"{platform.display} 本来就没有托管凭证")
    return 0


def _fmt_ts(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _cmd_identity_map(args: argparse.Namespace) -> int:
    import csv as _csv
    import io as _io

    from .identity.collect import load_json
    from .identity.mapping import (
        SOURCE_OVERRIDE,
        SOURCE_RULE,
        build,
        load_overrides,
        to_override_stub,
        unresolved,
    )

    users = []
    for path in args.from_json:
        users.extend(load_json(path))
    if not users:
        raise DeliveryError("没有账号可映射：用 --from-json 指定来源")
    overrides = load_overrides(args.overrides) if args.overrides else {}
    mappings = build(users, domain=args.domain, overrides=overrides)
    pending = unresolved(mappings)

    if args.stub:
        print(to_override_stub(mappings))
        return 0 if not pending else 1

    by_rule = [m for m in mappings if m.source == SOURCE_RULE]
    by_override = [m for m in mappings if m.source == SOURCE_OVERRIDE]
    print(
        f"映射 {len(mappings)} 条：规则推导 {len(by_rule)} · 显式登记 {len(by_override)} · "
        f"待登记 {len(pending)}"
    )
    for m in by_override:
        if m.note:
            print(f"  ! {m.email} → {m.cloud_name}    {m.note}")
    if pending:
        print("\n待登记（云账号不用改，把这些登记进 overrides 即可）：")
        for m in pending:
            print(f"   {m.email:34} {m.note}")
        print("\n生成骨架： delivery identity map --from-json ... --stub > overrides.json")

    if args.csv:
        buf = _io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["平台", "账号", "飞书邮箱", "云用户名（填进飞书工号）", "来源"])
        for m in mappings:
            w.writerow([m.platform, m.account, m.email, m.cloud_name, m.source])
        Path(args.csv).write_text(buf.getvalue(), encoding="utf-8")
        print(f"\nCSV 已写入 {args.csv}")
    return 0 if not pending else 1


def _cmd_identity(args: argparse.Namespace) -> int:
    from .identity import audit as run_audit
    from .identity.collect import collect_aliyun, load_json
    from .identity.report import render, to_csv

    users = []
    for path in args.from_json:
        users.extend(load_json(path))
    if args.platform:
        if args.platform != "aliyun":
            raise DeliveryError(
                f"实时采集目前只支持 aliyun（用的是官方 CLI）；"
                f"{args.platform} 请用 --from-json 喂离线导出"
            )
        users.extend(collect_aliyun(account=args.account, profile=args.profile))
    if not users:
        raise DeliveryError("没有任何账号可对账：用 --platform aliyun 或 --from-json 指定来源")

    report = run_audit(users, domain=args.domain, service_names=args.service)
    if args.json:
        print(
            json.dumps(
                {
                    "domain": report.domain,
                    "summary": report.summary(),
                    "ready": report.ready,
                    "findings": [
                        {
                            "platform": f.user.platform,
                            "account": f.user.account,
                            "name": f.user.name,
                            "display_name": f.user.display_name,
                            "email": f.user.email,
                            "verdict": f.verdict,
                            "note": f.note,
                        }
                        for f in report.findings
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(report, limit=args.limit))
    if args.csv:
        Path(args.csv).write_text(to_csv(report), encoding="utf-8")
        print(f"\nCSV 已写入 {args.csv}")
    # 未清零时返回 1：让它能直接当 CI 门禁用
    return 0 if report.ready else 1


def _cmd_matrix(registry: PlatformRegistry, args: argparse.Namespace) -> int:
    """产出 {"include": [{"platform": id}, ...]}。

    放在 CLI 里而不是 workflow 的 shell 里，是因为内嵌多行 Python 会破坏 YAML 缩进，
    而且筛选规则（哪些平台该进矩阵）是业务逻辑，应当可测。
    """
    rows = []
    for p in registry:
        if args.iac and p.capabilities.iac != args.iac:
            continue
        if args.scope:
            scope = p.scopes.get(args.scope)
            if scope is None:
                continue  # 这个平台没声明该 scope，就不该出现在它的矩阵里
            if args.appliable and not scope.apply:
                continue
        elif args.appliable and not p.capabilities.apply:
            continue
        row = {"platform": p.id}
        if args.scope:
            row["scope"] = args.scope
        rows.append(row)
    payload = {"include": rows}
    # 单行输出：workflow 里要 `echo "matrix=$(...)" >> $GITHUB_OUTPUT`，多行会截断。
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


def _cmd_describe(registry: PlatformRegistry, args: argparse.Namespace) -> int:
    platform = registry.get(args.platform)
    caps = platform.capabilities
    scope_name = getattr(args, "scope", SCOPE_FOUNDATION) or SCOPE_FOUNDATION
    scope = platform.scopes.get(scope_name)
    if scope is None:
        raise DeliveryError(
            f"平台 `{platform.id}` 没有声明 scope `{scope_name}`；已声明：{sorted(platform.scopes)}"
        )
    # 布尔值输出成小写 true/false：GitHub Actions 的 if: 比较的是字符串，
    # Python 的 "True" 与 `== 'true'` 不相等，会让 apply 门禁**恒为假**而静默跳过。
    fields = {
        "id": platform.id,
        "display": platform.display,
        "auth": caps.auth,
        "iac": caps.iac,
        "plan": caps.plan,
        "inventory": caps.inventory,
        "login": caps.login,
        # apply / require_approval 走 **scope 级**：流水线要按范围分支，
        # 平台级只是上限。platform_apply 单独给出来，便于排查「为什么 scope 不能 apply」。
        "scope": scope.name,
        "scope_desc": describe_scope(scope.name),
        "apply": str(scope.apply).lower(),
        "require_approval": str(scope.require_approval).lower(),
        "quota_gated": str(scope.quota_gated).lower(),
        "platform_apply": str(caps.apply).lower(),
        "policy_as_code": str(caps.policy_as_code).lower(),
        "status": caps.status,
        "keyless": str(caps.keyless).lower(),
    }
    if args.json:
        print(json.dumps(fields, ensure_ascii=False, indent=2))
        return 0
    for key, value in fields.items():
        print(f"{key}={value}")
    return 0


def _cmd_plan_show(registry: PlatformRegistry, args: argparse.Namespace) -> int:
    platform = registry.get(args.platform)  # 平台名拼错要立刻报错
    try:
        with Path(args.file).open(encoding="utf-8") as fh:
            document = json.load(fh)
    except OSError as exc:
        raise PlanParseError(f"读不了计划文件 {args.file}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"{args.file} 不是合法 JSON：{exc}") from exc
    plan = from_terraform(document, platform=platform.id, env=args.env, account=args.account)
    if args.json:
        print(plan.to_json())
    else:
        print(render_plan(plan))
    # 被阻断的计划用非零退出码，流水线据此拦住 apply。
    return 1 if plan.blocked else 0


def _cmd_doctor(args) -> int:
    """探飞书权限。退出码 1 = 有权限缺失（区别于 2 = 工具本身跑不起来）。"""
    from .doctor import render, run

    app_id = args.app_id or os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    probes = run(app_id=app_id, app_secret=secret, send_to=args.send_to)
    print(render(probes))
    return 1 if any(p.bad for p in probes) else 0


def _load_service_names(path: str) -> list:
    file = Path(path)
    if not file.exists():
        return []
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"读不了服务号名单 {path}：{exc}") from exc
    names = data.get("names") if isinstance(data, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise DeliveryError(f'{path} 格式应为 {{"names": ["服务号", ...]}}')
    return [n.strip() for n in names]


def _cmd_identity_ssomap(args) -> int:
    """实时采集两朵云 → 生成登录用映射提案。

    提案文件含全员企业邮箱和云账号，按 0600 写入，默认路径已 gitignore。
    """
    from .clouds import aliyun, volcano
    from .identity import cloudcollect
    from .identity.ssomap import propose, render

    accounts = []

    def say(msg: str) -> None:
        print(f"\r\033[K{msg}", end="", file=sys.stderr, flush=True)

    if "aliyun" not in args.skip:
        accounts += cloudcollect.collect_aliyun(aliyun.Credentials.from_env(), progress=say)
    if "volcano" not in args.skip:
        accounts += cloudcollect.collect_volcano(volcano.Credentials.from_env(), progress=say)
    print("\r\033[K", end="", file=sys.stderr)

    # 已知的服务号：前缀规则覆盖不到的几个。名单是线上账号名，放在 gitignored 的
    # identity/services.json（格式见 identity/services.example.json），不写进代码。
    services = list(args.service) + _load_service_names("identity/services.json")
    proposal = propose(
        accounts,
        domain=args.domain,
        service_names=services,
        trust_unverified_when_derivable=args.trust_unverified_when_derivable,
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2) + "\n"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    out.chmod(0o600)

    if args.json:
        print(payload, end="")
    else:
        print(render(proposal, show_confirmed=args.show_confirmed), end="")
        print(f"\n提案已写入 {out}（含员工邮箱，权限 600，不要提交到公开仓库）")
    return 0


def _require_identity_dir(out: Path) -> None:
    """含员工身份的文件只允许写进 identity/（整个目录 gitignore）。仓库是公开镜像的源头。"""
    if "identity" not in out.resolve().parent.parts:
        raise DeliveryError(f"{out} 不在 identity/ 目录下：这类文件含员工身份，只允许写到那里")


def _write_private(path: str, data: dict) -> Path:
    """含全员身份/权限数据的文件：0600，原子替换。"""
    out = Path(path)
    _require_identity_dir(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    tmp.replace(out)
    return out


def _cmd_inventory_collect(args) -> int:
    from . import inventory
    from .clouds import aliyun, volcano
    from .inventory_collect import build_snapshot, collect_aliyun, collect_volcano

    def say(msg: str) -> None:
        print(f"\r\033[K{msg}", end="", file=sys.stderr, flush=True)

    jobs = []
    if "aliyun" not in args.skip:
        for prefix in args.aliyun_profile or ["ALIYUN"]:
            creds = aliyun.Credentials.from_env(prefix)
            jobs.append(("aliyun", prefix, lambda p, c=creds: collect_aliyun(c, progress=p)))
    if "volcano" not in args.skip:
        vcreds = volcano.Credentials.from_env()
        jobs.append(("volcano", "default", lambda p: collect_volcano(vcreds, progress=p)))
    data = build_snapshot(jobs, progress=say)
    for skipped in args.skip:
        # 跳过也要留痕：否则快照看起来是完整的，名册里那朵云的账号全显示「快照中不存在」
        data["accounts"].append({"platform": skipped, "account": "*", "error": "本次采集跳过"})
    print("\r\033[K", end="", file=sys.stderr)
    snap = inventory.parse(data)  # 自检：写出去的必须是看板能读的
    out = _write_private(args.out, data)
    print(f"已写入 {out}（权限 600）：{len(snap.users)} 个子账号，{len(snap.groups)} 个组")
    for line in snap.incomplete:
        print(f"  ⚠ 采集失败，快照不完整：{line}")
    return 1 if snap.incomplete else 0


def _cmd_identity_iam_export(args) -> int:
    """名册 → IAM 用户属性 CSV。

    一行一个人，每个云账号一列（列名即 IAM 属性名），值是**云上现有用户名原样**。
    只导出已确认的对应；同一云账号下有多个号的人不导出该列并标出问题——
    属性值会直接决定 SSO 进哪个号，宁可空着（登录被拒）也不能填错。
    """
    import csv
    import io

    from . import people as people_mod

    bindings = Path(args.people).with_name("bindings.json")
    index = people_mod.load(args.people, bindings_path=str(bindings) if bindings.exists() else None)
    attr_file = Path(args.attributes)
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

    columns = list(attrs.values())
    if len(set(columns)) != len(columns):
        raise DeliveryError(f"{attr_file} 里有两个云账号用了同一个属性名，值会互相覆盖")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["feishu_union_id", "email", "name", *columns, "match_by", "problem"])

    def cell(value: str) -> str:
        # Excel 打开 CSV 时会把 = + - @ 开头的格子当公式执行
        return "'" + value if value[:1] in ("=", "+", "-", "@") else value

    exported = skipped = 0
    for person in index.people:
        if not person.accounts:
            continue
        values, problems = {}, []
        by_scope: dict = {}
        for ref in person.accounts:
            by_scope.setdefault(ref.scope, []).append(ref.name)
        for scope, names in by_scope.items():
            column = attrs.get(scope)
            if column is None:
                problems.append(f"{scope} 未配置属性名")
            elif len(names) > 1:
                problems.append(f"{scope} 下有多个号 {'/'.join(sorted(names))}，需先定保留哪个")
            else:
                values[column] = names[0]
        if person.pending:
            problems.append(f"另有 {len(person.pending)} 个待确认对应未导出")
        if not values:
            skipped += 1
        else:
            exported += 1
        # 没有 union_id 的行，IAM 侧只能按邮箱做一次存量回填（规范允许），在表里写明
        match_by = "feishu_union_id" if person.union_id else "email（存量回填）"
        writer.writerow(
            [
                cell(person.union_id),
                cell(person.email),
                cell(person.name),
                *[cell(values.get(c, "")) for c in columns],
                match_by,
                cell("；".join(problems)),
            ]
        )
    out = Path(args.out)
    _require_identity_dir(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, buf.getvalue().encode("utf-8-sig"))
    finally:
        os.close(fd)
    tmp.replace(out)
    print(f"已写入 {out}（权限 600）：{exported} 人有可导入的属性，{skipped} 人没有")
    missing_uid = sum(1 for p in index.people if p.accounts and not p.union_id)
    if missing_uid:
        print(
            f"  {missing_uid} 人还没有 union_id：IAM 侧导入时需按 IAM 规范做一次存量回填"
            "（按邮箱找到 IAM 用户），或先用 IT 的对照表重新生成名册"
        )
    return 0


def _cmd_identity_people(args) -> int:
    from .identity import directory
    from .people import apply_manual, build, parse

    try:
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"读不了映射提案 {args.proposal}：{exc}") from exc
    if args.directory == "feishu":
        entries = directory.from_feishu(
            os.environ.get("DELIVERY_FEISHU_APP_ID", ""),
            os.environ.get("DELIVERY_FEISHU_APP_SECRET", ""),
            progress=lambda m: print(f"\r\033[K{m}", end="", file=sys.stderr, flush=True),
        )
        print("\r\033[K", end="", file=sys.stderr)
    elif args.directory.startswith("csv:"):
        entries = directory.from_csv(args.directory[4:])
    elif args.directory == "none":
        # 暂时拿不到通讯录：名册里人人 union_id 为空，本人首次登录时按企业邮箱关联
        entries = []
    else:
        raise DeliveryError("--directory 只能是 feishu、csv:<路径> 或 none")
    manual_path = Path(args.manual)
    if manual_path.exists():
        try:
            manual = json.loads(manual_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryError(f"读不了人工对应 {manual_path}：{exc}") from exc
        proposal = apply_manual(proposal, manual)
    data = build(proposal, entries)
    parse(data)  # 自检
    out = _write_private(args.out, data)
    stats = data["stats"]
    print(
        f"已写入 {out}（权限 600）：{stats['people']} 人，"
        f"{stats['with_union_id']} 人有 union_id，{stats['with_cloud_account']} 人有云账号"
    )
    missing = [p for p in data["people"] if not p["union_id"] and (p["accounts"] or p["pending"])]
    if missing:
        print(
            f"  {len(missing)} 个有云账号的人没回填上 union_id"
            "（本人首次登录时按企业邮箱自动关联）："
        )
        for p in missing[:20]:
            print(f"    {p['name']} <{p['email']}>")
    for mail in stats["email_collisions_in_directory"]:
        print(f"  ⚠ 通讯录里多人共用企业邮箱 {mail}，这些人不回填")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = PlatformRegistry.load(args.platforms_dir)
        if args.command == "platforms":
            return _cmd_platforms(registry, args.json)
        if args.command == "login-guide":
            return _cmd_login_guide(registry, args)
        if args.command == "identity":
            if args.identity_command == "sso-map":
                return _cmd_identity_ssomap(args)
            if args.identity_command == "iam-export":
                return _cmd_identity_iam_export(args)
            if args.identity_command == "people":
                return _cmd_identity_people(args)
            if args.identity_command == "map":
                return _cmd_identity_map(args)
            return _cmd_identity(args)
        if args.command == "serve":
            from .server import serve

            serve(
                host=args.host,
                port=args.port,
                registry=registry,
                inventory_path=args.inventory,
                people_path=args.people,
                admins_path=args.admins,
                labels_path=args.labels,
            )
            return 0
        if args.command == "inventory":
            return _cmd_inventory_collect(args)
        if args.command == "login":
            return _cmd_login(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "logout":
            return _cmd_logout()
        if args.command == "status":
            return _cmd_status(registry)
        if args.command == "bind":
            return _cmd_bind(registry, args)
        if args.command == "unbind":
            return _cmd_unbind(registry, args)
        if args.command == "matrix":
            return _cmd_matrix(registry, args)
        if args.command == "describe":
            return _cmd_describe(registry, args)
        if args.command == "plan-show":
            return _cmd_plan_show(registry, args)
    except DeliveryError as exc:
        # 退出码语义：2 = 工具/输入有问题；1 = 计划本身被阻断。流水线要分得开，
        # 否则「计划有高危变更」和「工具崩了」会走同一条处理分支。
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"未处理的子命令 {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
