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
import time
import unicodedata
from pathlib import Path
from typing import Optional, Sequence

from . import iam_sync
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

    srv = commands.add_parser("serve", help="本地开发服务器：看板 + 飞书登录或公司 IAM 登录")
    srv.add_argument(
        "--auth",
        choices=("feishu", "proxy"),
        default=None,
        help="登录方式：feishu 飞书应用（默认）；proxy 挂在 oauth2-proxy 后面走公司 IAM。"
        "默认取 DELIVERY_AUTH",
    )
    srv.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    srv.add_argument(
        "--sessions",
        default=None,
        help="登录会话落盘的位置（如 identity/sessions.json）。"
        "不给则只存内存，进程一重启所有人要重新登录",
    )
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
        "--proposal",
        default="identity/sso-map.proposal.json",
        help="映射提案，名册审核重建名册时用",
    )
    srv.add_argument(
        "--manual", default="identity/manual-links.json", help="人工记录，名册审核写入这里"
    )
    srv.add_argument(
        "--tickets", default="identity/tickets.json", help="申请单存储（开账号、权限、访问凭证）"
    )
    srv.add_argument("--assets", default="identity/assets.json", help="云账号资产快照")
    srv.add_argument(
        "--downloads",
        default="",
        help="工具下载目录（如 downloads/）。放九章的 aladdin 这类没有公开下载地址的二进制；"
        "阿里和火山的 CLI 有官方地址，页面上直接给链接，不在这里放第二份",
    )
    srv.add_argument(
        "--policies",
        default="identity/policies.json",
        help="权限策略目录（delivery policies collect）",
    )
    srv.add_argument(
        "--policy-rules",
        default="identity/policy-rules.json",
        help="权限策略申请规则（可选，不存在时用内置禁用清单）",
    )
    srv.add_argument("--templates", default="identity/request-templates.json", help="申请模板目录")
    srv.add_argument(
        "--approval", default="identity/approval.json", help="飞书审批定义与表单控件配置"
    )
    srv.add_argument(
        "--iam-attributes",
        default=iam_sync.DEFAULT_SPEC,
        help="云账号 → IAM 属性名配置，管理后台的属性表页要用",
    )
    srv.add_argument(
        "--iam-out",
        default=iam_sync.DEFAULT_OUT,
        help="管理后台导出的属性表增量写到哪里",
    )
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
    iamx.add_argument(
        "--adopt-result",
        default="",
        metavar="FILE",
        help="IT 回传的导入结果 CSV（feishu_union_id,email,name,app,value,result）："
        "把它采纳为新的确认基线，之后的增量直接按 union_id 比对",
    )
    iamx.add_argument("--people", default="identity/people.json")
    iamx.add_argument(
        "--attributes",
        default="identity/iam-attributes.json",
        help='云账号 → IAM 属性名，形如 {"aliyun/<UID>": "aliyun_username"}',
    )
    iamx.add_argument("--out", default="identity/iam-attributes.csv")
    iamx.add_argument(
        "--baseline",
        default="",
        help="只导出变化。基线必须是 identity/iam-sent/ 里经 IT 确认的全量存档，latest = 最新一份",
    )
    iamx.add_argument(
        "--record",
        action="store_true",
        help="把本次的全量状态存进 identity/iam-sent/pending/，等 IT 确认导入后再 --confirm-sent",
    )
    iamx.add_argument(
        "--confirm-sent",
        default="",
        metavar="FILE",
        help="IT 确认已导入后执行：把 pending 里的这份存档转为正式基线",
    )
    iamx.add_argument(
        "--resolved",
        action="append",
        default=[],
        metavar="EMAIL",
        help="已核对无误的邮箱：上次因邮箱复用或姓名不符被跳过的行，这次正常导出",
    )
    iamx.add_argument(
        "--allow-mass-remove",
        action="store_true",
        help="增量里 remove 超过阈值、或基线里有人整体消失时，确认无误后才加",
    )

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

    from . import cli_requests

    cli_requests.add_parsers(commands)

    rf = commands.add_parser(
        "refresh", help="定时任务：采集权限快照、生成映射提案、重建人员名册，异常时飞书告警"
    )
    rf.add_argument("--inventory", default="identity/inventory.json")
    rf.add_argument("--proposal", default="identity/sso-map.proposal.json")
    rf.add_argument("--people", default="identity/people.json")
    rf.add_argument("--manual", default="identity/manual-links.json")
    rf.add_argument(
        "--directory",
        default="none",
        help="union_id 来源：none（默认，沿用上一份名册）、feishu、csv:<路径>",
    )
    rf.add_argument("--domain", default="wuji.tech", help="企业邮箱域名")
    rf.add_argument(
        "--baseline",
        default="identity/inventory.baseline.json",
        help="变化比对基线，只有采集成功的平台才更新",
    )
    rf.add_argument(
        "--aliyun-profile",
        action="append",
        default=[],
        metavar="PREFIX",
        help="阿里云凭证环境变量前缀，可重复，快照和映射提案都按这些账号采集",
    )
    rf.add_argument(
        "--service", action="append", default=[], metavar="NAME", help="同 identity sso-map"
    )
    rf.add_argument(
        "--trust-unverified-when-derivable",
        action="store_true",
        help="同 identity sso-map：企业邮箱未验证、但用户名可由它推出时也直接确认。"
        "要和上次手动生成提案时的选择一致，否则名册里的已确认账号会变",
    )
    rf.add_argument("--no-alert", action="store_true", help="只打印，不发飞书告警")

    lg = commands.add_parser(
        "login", help="登录：飞书账号（浏览器授权），或公司 IAM（--iam，设备码）"
    )
    lg.add_argument(
        "--iam",
        action="store_true",
        help="面板挂在公司 IAM（oauth2-proxy）后面时用：终端显示验证码，在浏览器里用公司账号确认",
    )
    lg.add_argument("--issuer", default="", help="公司 IAM 签发方地址，默认取 DELIVERY_IAM_ISSUER")
    lg.add_argument(
        "--client-id",
        default="",
        help="IAM 里给 CLI 建的公开客户端 ID，默认取 DELIVERY_IAM_CLI_CLIENT_ID",
    )
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
    if getattr(args, "iam", False):
        return _cmd_login_iam(args, server)
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


def _cmd_login_iam(args: argparse.Namespace, server: str, *, transport=None, sleep=None) -> int:
    from . import iam_device
    from .session import Session, save_session

    issuer = args.issuer or os.environ.get("DELIVERY_IAM_ISSUER") or ""
    client_id = args.client_id or os.environ.get("DELIVERY_IAM_CLI_CLIENT_ID") or ""
    if not issuer or not client_id:
        raise DeliveryError(
            "公司 IAM 登录需要签发方地址和 CLI 客户端 ID：--issuer / --client-id，"
            "或环境变量 DELIVERY_IAM_ISSUER / DELIVERY_IAM_CLI_CLIENT_ID（向 IT 要）"
        )
    from .cli_requests import ClientError, PanelClient

    try:
        PanelClient(server, "")  # 先校验面板地址（必须 https），免得登录完才发现存了个用不了的地址
    except ClientError as exc:
        raise DeliveryError(str(exc)) from None
    scope = os.environ.get("DELIVERY_IAM_SCOPE") or iam_device.DEFAULT_SCOPE
    kw = {"transport": transport} if transport else {}
    endpoints = iam_device.discover(issuer, **kw)
    code = iam_device.start(endpoints, client_id, scope=scope, **kw)
    link = code.verification_uri_complete or code.verification_uri
    print("\n  在浏览器里打开下面的链接，用公司账号登录并确认：")
    print(f"    {link}")
    print(f"  验证码：{code.user_code}    （{code.expires_in // 60} 分钟内有效）\n")
    if not args.no_browser:
        import webbrowser

        webbrowser.open(link)
    tokens = iam_device.poll(
        endpoints, client_id, code, **kw, **({"sleep": sleep} if sleep else {})
    )
    claims = tokens.claims
    session = Session(
        union_id=str(claims.get("feishu_union_id") or claims.get("sub") or ""),
        name=str(claims.get("name") or claims.get("preferred_username") or ""),
        token=tokens.token,
        expires_ts=tokens.expires_ts,
        server=server,
        kind="iam",
        refresh_token=tokens.refresh_token,
        token_endpoint=endpoints.token,
        client_id=client_id,
    )
    save_session(session)
    if not claims.get("feishu_union_id"):
        print("  ⚠ 令牌里没有 feishu_union_id：面板会按未登录处理。")
        print("    请 IT 给这个客户端分配 wuji scope 映射")
    print(f"  ✓ 已登录：{session.name or session.union_id}")
    print(
        "    之后的请求经 oauth2-proxy 校验身份；"
        + ("令牌到期前会自动续期" if tokens.refresh_token else "令牌到期后需要重新登录")
    )
    return 0


def _cmd_logout() -> int:
    print("已清除本机会话" if clear_session() else "本机没有已保存的会话")
    return 0


def _cmd_status(registry: PlatformRegistry) -> int:
    session = load_session()
    if session is None:
        print("未登录。运行： delivery login")
    elif session.kind == "iam" and session.expired and not session.refresh_token:
        print(f"登录已过期（{session.name or session.union_id}），请重新 delivery login --iam")
    elif session.kind == "iam":
        renew = "到期前自动续期" if session.refresh_token else "到期后需要重新 delivery login --iam"
        print(f"已登录（公司 IAM）：{session.name or session.union_id}    {renew}")
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
        _write_private_text(args.csv, buf.getvalue())
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
        _write_private_text(args.csv, to_csv(report))
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

    payload = json.dumps(proposal.to_dict(), ensure_ascii=False, indent=2) + "\n"
    out = _write_private(args.out, proposal.to_dict())

    if args.json:
        print(payload, end="")
    else:
        print(render(proposal, show_confirmed=args.show_confirmed), end="")
        print(f"\n提案已写入 {out}（含员工邮箱，权限 600，不要提交到公开仓库）")
    return 0


def _require_identity_dir(out: Path) -> None:
    """含员工身份的文件只允许写进**仓库根目录**的 identity/（整个目录 gitignore）。

    `.gitignore` 的 `identity/*` 只匹配仓库根目录那一层——`src/identity/`、
    `src/delivery/identity/` 都不被忽略。所以按「目标所在的 git 工作树」判根，
    不能拿当前工作目录当根（在子目录里跑命令就绕过了）：
      · 目标在某个 git 工作树里 → 必须在该工作树顶层的 identity/ 下
      · 目标落在本包所在仓库里（git 不可用时的兜底）→ 必须在该仓库的 identity/ 下
      · 都不是（例如测试用的临时目录）→ 必须在当前工作目录的 identity/ 下
    """
    target = out.resolve()
    repo = Path(__file__).resolve().parents[2]
    top = _git_toplevel(target.parent)
    if top is not None:
        root = top
        # 别的仓库不一定忽略 identity/；以后有人误删 .gitignore 规则也能拦住
        if _is_under(target, root / "identity") and not _git_ignored(root, target):
            raise DeliveryError(f"{out} 没有被 {root} 的 .gitignore 忽略，拒绝写入员工数据")
    elif _is_under(target, repo):
        root = repo
    else:
        root = Path.cwd().resolve()
        if any((d / ".git").exists() for d in (root, *root.parents)):
            # git 不可用但当前目录在某个仓库里：无法确认忽略规则，拒绝
            raise DeliveryError(f"无法确认 {out} 是否被 git 忽略（git 不可用），拒绝写入员工数据")
    if not _is_under(target, root / "identity"):
        raise DeliveryError(
            f"{out} 不在仓库根目录的 identity/ 下：这类文件含员工身份，只允许写到那里"
        )


def _git_toplevel(start: Path) -> Optional[Path]:
    """start 所在 git 工作树的顶层目录；不在工作树里或没有 git 时返回 None。"""
    import subprocess

    probe = start
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent  # 目标目录可能还没建
    try:
        done = subprocess.run(  # noqa: S603 — 固定参数，不拼接外部输入
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return Path(done.stdout.strip()).resolve()


def _git_env() -> dict:
    # GIT_DIR / GIT_WORK_TREE 会覆盖 -C，判定就不是针对目标路径了
    return {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}


def _git_ignored(root: Path, target: Path) -> bool:
    import subprocess

    try:
        done = subprocess.run(  # noqa: S603 — 固定参数，不拼接外部输入
            ["git", "-C", str(root), "check-ignore", "-q", str(target)],  # noqa: S607
            capture_output=True,
            timeout=5,
            check=False,
            env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _write_private(path: str, data: dict) -> Path:
    """含全员身份/权限数据的文件：0600，原子替换。"""
    return _write_private_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _write_private_text(path: str, text: str) -> Path:
    out = Path(path).resolve()  # 先解析符号链接，临时文件和替换都基于真实路径
    _require_identity_dir(out)
    _atomic_private_write(out, text.encode("utf-8"))
    return out


def _atomic_private_write(out: Path, payload: bytes) -> None:
    """临时文件用 mkstemp（O_EXCL、随机名），不会顺着事先放好的符号链接写到别处。"""
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        Path(tmp).chmod(0o600)
        Path(tmp).replace(out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _snapshot_jobs(aliyun_profiles, skip) -> list:
    from .clouds import aliyun, volcano
    from .inventory_collect import collect_aliyun, collect_volcano

    jobs = []
    if "aliyun" not in skip:
        for prefix in aliyun_profiles or ["ALIYUN"]:
            creds = aliyun.Credentials.from_env(prefix)
            jobs.append(("aliyun", prefix, lambda p, c=creds: collect_aliyun(c, progress=p)))
    if "volcano" not in skip:
        vcreds = volcano.Credentials.from_env()
        jobs.append(("volcano", "default", lambda p: collect_volcano(vcreds, progress=p)))
    return jobs


def _cmd_inventory_collect(args) -> int:
    from . import inventory
    from .inventory_collect import build_snapshot

    def say(msg: str) -> None:
        print(f"\r\033[K{msg}", end="", file=sys.stderr, flush=True)

    data = build_snapshot(_snapshot_jobs(args.aliyun_profile, args.skip), progress=say)
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


#: 发给 IT 的属性表存档目录与表头：实现搬到 iam_sync，面板和 CLI 共用同一套规则。
IAM_SENT_DIR = iam_sync.SENT_DIR
#: 管理员名单的兜底路径，与 roles.load_admins 的回落保持一致
ADMINS_DEFAULT = "identity/admins.json"
IAM_CSV_HEADER = iam_sync.CSV_HEADER


def _iam_rows(index, specs: dict) -> list:
    return iam_sync.build_rows(index, specs)


def _resolve_baseline(value: str) -> Path:
    return iam_sync.resolve_baseline(value, Path(IAM_SENT_DIR))


def _read_iam_csv(path: Path) -> list:
    return iam_sync.read_csv(path)


def _write_iam_csv(out: Path, rows: list) -> None:
    iam_sync.write_csv(out, rows)


def _cmd_identity_iam_export(args) -> int:
    """名册 → IAM 属性表 CSV（cloud_accounts 的写入指令）。

    默认导出全量；`--baseline` 只导出与上次发给 IT 的存档相比有变化的行，
    删号、改名以 remove / set 表达。`--record` 把本次结果存档。
    """
    from . import iam_export

    if args.confirm_sent:
        if (
            args.baseline
            or args.record
            or args.allow_mass_remove
            or args.resolved
            or args.adopt_result
        ):
            raise DeliveryError("--confirm-sent 只做确认，不能和 --baseline / --record 等一起用")
        return _confirm_sent(args.confirm_sent)
    if args.adopt_result and (
        args.baseline or args.record or args.resolved or args.allow_mass_remove
    ):
        raise DeliveryError(
            "--adopt-result 只做基线采纳，不能和 --baseline / --record / "
            "--allow-mass-remove 等一起用（--out 在这个模式下也用不上）"
        )
    sent_dir = Path(IAM_SENT_DIR).resolve()
    if Path(args.out).resolve().is_relative_to(sent_dir):
        raise DeliveryError(f"--out 不能写到 {IAM_SENT_DIR} 里：那里只放确认过的全量存档")
    if (
        args.record
        and args.baseline != "latest"
        and iam_export.confirmed_archives(Path(IAM_SENT_DIR))
    ):
        raise DeliveryError(
            "已经有确认过的基线：--record 必须和 --baseline latest 一起用。"
            "否则这份存档没有对应的 remove，确认后旧值会永远留在 IAM 里"
        )
    paths = iam_sync.SyncPaths(
        people=args.people, attributes=args.attributes, out=args.out, sent_dir=IAM_SENT_DIR
    )
    warn = lambda line: print(line, file=sys.stderr)  # noqa: E731
    if args.adopt_result:
        full, _ = iam_sync.full_state(paths, warn=warn)
        return _adopt_result(args.adopt_result, full)
    if args.resolved and not args.baseline:
        raise DeliveryError("--resolved 只在增量导出（--baseline latest）时有意义")
    increment = iam_sync.compute(
        paths,
        baseline=args.baseline,
        allow_mass_remove=args.allow_mass_remove,
        resolved_emails=frozenset(args.resolved),
        warn=warn,
    )
    if increment.baseline is not None:
        print(f"对比基线 {increment.baseline}")

    archive = None
    if args.record:
        # 存档路径先过守卫再写主输出：守卫失败时不能留下一份没有存档的输出
        archive = iam_sync.next_archive(
            Path(IAM_SENT_DIR), pending=True, stamp=time.strftime("%Y%m%d-%H%M%S.csv")
        )
        _require_identity_dir(archive.resolve())
        meta = archive.with_name(archive.name + ".meta.json")
        _require_identity_dir(meta.resolve())

    out = Path(args.out)
    _write_iam_csv(out, increment.rows)
    counts = increment.counts
    print(
        f"已写入 {out}（权限 600）：set {counts['set']} 条，remove {counts['remove']} 条，"
        f"skip {counts['skip']} 条"
    )
    for note in increment.notes:
        print(f"  ⚠ {note}")
    if archive is not None:
        # 存档的是全量状态，不是这次的增量：下次比对要靠它发现删号
        iam_sync.record(increment, archive)
        print(
            f"  已存入待确认 {archive}。IT 确认导入后执行："
            f"delivery identity iam-export --confirm-sent {archive}"
        )
    print(
        "  导入须知：match_by=email 的行只按邮箱回填一次，且只用于 IAM 里还没有 union_id 的用户，"
        "回填时同时写下 union_id；remove（match_by=value）删除当前值恰好等于 value 的那个用户的该键"
    )
    return 0


def _adopt_result(path: str, full: list) -> int:
    """IT 回传的导入结果 → 新的确认基线。

    第一次导出按邮箱回填、基线里没有 union_id；回传结果每行都带 union_id。不采纳的话，
    下次增量会把所有人「按值删掉再按 union_id 写回」，IT 白导一遍。
    """
    archive, notes, sets, carried = iam_sync.adopt(
        Path(path), full, Path(IAM_SENT_DIR), stamp=time.strftime("%Y%m%d-%H%M%S.csv")
    )
    print(
        f"已采纳为基线 {archive}（权限 600）：{sets} 条 set"
        + (f"（其中 {carried} 条沿用旧基线）" if carried else "，全部按 union_id 匹配")
    )
    for note in notes:
        print(f"  ⚠ {note}")
    print("  之后导增量： delivery identity iam-export --baseline latest --record")
    return 0


def _confirm_sent(value: str) -> int:
    """IT 确认导入后，把 pending 里的存档转为正式基线。"""
    target = iam_sync.confirm(Path(value), Path(IAM_SENT_DIR))
    print(f"已确认 {target}：之后 --baseline latest 以它为基线")
    return 0


def _directory_entries(source: str, *, progress: bool) -> list:
    from .identity import directory

    if source == "feishu":
        say = (
            (lambda m: print(f"\r\033[K{m}", end="", file=sys.stderr, flush=True))
            if progress
            else None
        )
        entries = directory.from_feishu(
            os.environ.get("DELIVERY_FEISHU_APP_ID", ""),
            os.environ.get("DELIVERY_FEISHU_APP_SECRET", ""),
            progress=say,
        )
        if progress:
            print("\r\033[K", end="", file=sys.stderr)
        return entries
    if source.startswith("csv:"):
        return directory.from_csv(source[4:])
    if source == "none":
        # 暂时拿不到通讯录：名册里人人 union_id 为空，本人首次登录时按企业邮箱关联
        return []
    raise DeliveryError("--directory 只能是 feishu、csv:<路径> 或 none")


def _load_manual(path: str) -> Optional[dict]:
    manual_path = Path(path)
    if not manual_path.exists():
        return None
    try:
        return json.loads(manual_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"读不了人工对应 {manual_path}：{exc}") from exc


def _cmd_identity_people(args) -> int:
    from .people import apply_manual, build, parse

    try:
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryError(f"读不了映射提案 {args.proposal}：{exc}") from exc
    entries = _directory_entries(args.directory, progress=True)
    manual = _load_manual(args.manual)
    if manual:
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


def _review_paths(args) -> dict:
    """名册审核会写名册和人工记录：路径过不了写盘守卫就不开审核功能。"""
    try:
        for path in (args.people, args.manual, args.proposal):
            _require_identity_dir(Path(path).resolve())
    except DeliveryError as exc:
        print(f"  ⚠ 名册审核已关闭：{str(exc).splitlines()[0]}")
        return {}
    return {"proposal_path": args.proposal, "manual_path": args.manual}


def _sessions_path(args) -> Optional[str]:
    """会话文件里是登录凭证（等同于 cookie 值），和名册同级看待：只允许落在 identity/ 下。

    路径不合规不是致命错误——退回只存内存，重启会把人踢下线，但服务照常起。
    """
    value = getattr(args, "sessions", None)
    if not value:
        return None
    try:
        _require_identity_dir(Path(value).resolve())
    except DeliveryError as exc:
        print(f"  ⚠ 会话不落盘（重启后需重新登录）：{str(exc).splitlines()[0]}")
        return None
    return value


def _request_paths(args) -> dict:
    """申请单含员工信息，模板和审批配置含云账号 ID，资产快照含资源明细：
    路径过不了写盘守卫（必须在 gitignored 的 identity/ 下）就不开对应功能。"""
    out = {}
    try:
        for path in (args.tickets, args.templates, args.approval):
            _require_identity_dir(Path(path).resolve())
        out.update(
            tickets_path=args.tickets, templates_path=args.templates, approval_path=args.approval
        )
    except DeliveryError as exc:
        print(f"  ⚠ 云账号申请已关闭：{str(exc).splitlines()[0]}")
    try:
        _require_identity_dir(Path(args.assets).resolve())
        out["assets_path"] = args.assets
    except DeliveryError as exc:
        print(f"  ⚠ 云账号资产已关闭：{str(exc).splitlines()[0]}")
    try:
        # 规则文件决定能授予什么：不在 identity/ 下同样关掉按策略申请，而不是回落到内置规则
        for path in (args.policies, args.policy_rules):
            _require_identity_dir(Path(path).resolve())
        out.update(policies_path=args.policies, policy_rules_path=args.policy_rules)
    except DeliveryError as exc:
        print(f"  ⚠ 按策略申请权限已关闭：{str(exc).splitlines()[0]}")
    try:
        # 属性表含全员邮箱与云用户名：写不进 identity/ 就不开这个页面
        for path in (args.iam_attributes, args.iam_out):
            _require_identity_dir(Path(path).resolve())
        # 增量含 remove 行，落进存档目录会被当成基线，check_baseline 随后永久拒绝
        if Path(args.iam_out).resolve().is_relative_to(Path(IAM_SENT_DIR).resolve()):
            raise DeliveryError(f"--iam-out 不能写到 {IAM_SENT_DIR} 里：那里只放确认过的全量存档")
        # 也不能指向面板托管的其它文件：导出是原子替换，指错了直接把名册冲掉
        # 按名字取：各子命令传进来的 Namespace 不一定带齐所有参数
        managed = set()
        # admins 的命令行默认是 None（真正的回落在 roles.load_admins 里），
        # bindings 根本没有这个参数（按 people 同目录推）—— 两个都要自己补上，
        # 否则 --iam-out 指过去时守卫放行，第一次导出就把文件冲成 CSV
        from .roles import ENV_ADMINS

        managed.add(
            Path(
                getattr(args, "admins", "") or os.environ.get(ENV_ADMINS, "") or ADMINS_DEFAULT
            ).resolve()
        )
        if getattr(args, "people", ""):
            managed.add(Path(args.people).resolve().with_name("bindings.json"))
        for name in (
            "people",
            "bindings",
            "admins",
            "labels",
            "inventory",
            "tickets",
            "templates",
            "approval",
            "proposal",
            "manual",
            "assets",
            "policies",
            "policy_rules",
            "iam_attributes",
        ):
            value = getattr(args, name, "")
            if value:
                managed.add(Path(value).resolve())
        if Path(args.iam_out).resolve() in managed:
            raise DeliveryError("--iam-out 不能指向面板管理的其它文件：导出会把它整个覆盖")
        out.update(iam_spec_path=args.iam_attributes, iam_out_path=args.iam_out)
    except DeliveryError as exc:
        print(f"  ⚠ IAM 属性表已关闭：{str(exc).splitlines()[0]}")
    return out


def _cmd_refresh(args) -> int:
    """定时任务入口：快照 → 提案 → 名册，有异常或变化就发飞书告警。"""
    from . import alerts, refresh

    report = refresh.RefreshReport()
    try:
        code = _refresh_locked(args, report)
    except Exception as exc:  # noqa: BLE001 — 定时任务里任何失败都要进告警
        report.problems.append(f"刷新中断：{refresh.brief(exc)}")
        code = None
    if code == 75:
        return 75
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr)

    text = report.render()
    print(text)
    code = 0 if report.ok else 1
    if not report.needs_attention or args.no_alert:
        return code
    alert_conf = alerts.from_env(os.environ)
    if alert_conf is None:
        print(f"  ⚠ 需要告警但没设置 {alerts.ENV_WEBHOOK}（不需要告警加 --no-alert）")
        return 1
    try:
        alerts.send_feishu(text, webhook=alert_conf[0], secret=alert_conf[1])
        print("已发送飞书告警")
    except alerts.AlertError as exc:
        print(f"  ⚠ {exc}")
        return 1
    return code


def _read_previous(path: str, what: str, errors: list, parse) -> Optional[dict]:
    """上一份数据：不存在返回 None；存在但读不了或格式不对记进 errors（不能当成第一次运行）。"""
    file = Path(path)
    if not file.exists():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
        parse(data)
    except (OSError, ValueError, DeliveryError) as exc:
        errors.append(f"上一份{what}读不了：{type(exc).__name__}")
        return None
    return data


def _refresh_locked(args, report) -> Optional[int]:
    import fcntl

    from . import refresh
    from .clouds import aliyun, volcano
    from .identity import cloudcollect
    from .identity.ssomap import propose
    from .inventory_collect import build_snapshot

    interactive = sys.stderr.isatty()

    def say(msg: str) -> None:
        if interactive:
            print(f"\r\033[K{msg}", end="", file=sys.stderr, flush=True)

    lock_path = Path(args.people).resolve().parent / ".refresh.lock"
    _require_identity_dir(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("上一次刷新还没结束，本次跳过")
            return 75

        from . import inventory
        from . import people as people_mod

        errors: list = []
        previous_people = _read_previous(args.people, "名册", errors, people_mod.parse)
        baseline_errors: list = []
        previous_baseline = _read_previous(
            args.baseline, "比对基线", baseline_errors, inventory.parse
        )
        if previous_baseline is None and not baseline_errors:
            # 第一次跑还没有基线：拿现有快照当起点，避免首轮把所有人报成新增
            previous_baseline = _read_previous(
                args.inventory, "权限快照", baseline_errors, inventory.parse
            )
        report.problems.extend(baseline_errors)
        profiles = args.aliyun_profile or ["ALIYUN"]

        def collect_proposal() -> dict:
            accounts = []
            for prefix in profiles:
                accounts += cloudcollect.collect_aliyun(
                    aliyun.Credentials.from_env(prefix), progress=say
                )
            accounts += cloudcollect.collect_volcano(volcano.Credentials.from_env(), progress=say)
            services = list(args.service) + _load_service_names("identity/services.json")
            return propose(
                accounts,
                domain=args.domain,
                service_names=services,
                trust_unverified_when_derivable=args.trust_unverified_when_derivable,
            ).to_dict()

        refresh.run(
            known_users=_panel_issued_users(args, report.problems),
            collect_snapshot=lambda: build_snapshot(_snapshot_jobs(profiles, ()), progress=say),
            collect_proposal=collect_proposal,
            directory=lambda: _directory_entries(args.directory, progress=False),
            manual=_load_manual(args.manual),
            previous_baseline=previous_baseline,
            previous_people=previous_people,
            write_snapshot=lambda d: _write_private(args.inventory, d),
            write_baseline=lambda d: _write_private(args.baseline, d),
            write_proposal=lambda d: _write_private(args.proposal, d),
            write_people=lambda d: _write_private(args.people, d),
            carry_over=args.directory == "none",
            previous_errors=errors,
            report=report,
        )
        return None
    finally:
        os.close(lock_fd)


def _panel_issued_users(args, problems: list) -> set:
    """面板自己开过哪些子账号（`平台/账号/用户名`）。

    用来把「新增子账号」拆成两类：面板开的不用问，手工在控制台开的才要人去补来历。
    云上没有任何字段记着「这个号是谁为谁开的」，所以这份记录只能来自我们的台账。
    读不到申请单就返回空集 —— 那样全部算「来路不明」，宁可多问几句，不要漏掉；
    但「台账在却读不了」是真故障，要进告警，否则这一栏天天全量误报、很快没人看。
    """
    path = getattr(args, "tickets", "") or "identity/tickets.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        # 还没有台账（全新部署）：安静返回空集，那时本来也没有「面板开的号」
        return set()
    except (OSError, ValueError) as exc:
        # 台账在但读不了是**真故障**。不报的话，每个新增子账号都会被报成「来路不明」，
        # 那一栏天天全量误报，两周后就没人看了 —— 功能等于白做
        from .refresh import brief

        problems.append(f"申请单读不了，无法区分哪些号是面板开的：{brief(exc)}")
        return set()
    items = data.get("tickets") if isinstance(data, dict) else data
    out = set()
    for ticket in items if isinstance(items, list) else []:
        if not isinstance(ticket, dict):
            continue
        tpl = ticket.get("template") or {}
        # 被拒 / 撤回 / 提交失败的单子不算「面板开过」—— 那些号根本没建出来，
        # 事后有人手工用同名建了号，反而该被提示补登记
        if ticket.get("status") in ("rejected", "withdrawn", "submit_failed"):
            continue
        name = ticket.get("cred_user") or (ticket.get("payload") or {}).get("username") or ""
        if name and tpl.get("platform") and tpl.get("account"):
            out.add(f"{tpl['platform']}/{tpl['account']}/{name}")
    return out


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
                **_review_paths(args),
                **_request_paths(args),
                sessions_path=_sessions_path(args),
                downloads_path=args.downloads,
                auth=args.auth,
            )
            return 0
        if args.command == "inventory":
            return _cmd_inventory_collect(args)
        if args.command == "refresh":
            return _cmd_refresh(args)
        if args.command in ("request", "requests", "approval", "assets", "policies"):
            from . import cli_requests

            return cli_requests.dispatch(args)
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
