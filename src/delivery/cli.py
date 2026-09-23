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
    srv.add_argument(
        "--services",
        default="identity/services.json",
        help="服务号清单（体检时这些不算「无主」）",
    )
    srv.add_argument(
        "--dataset-buckets",
        default="identity/dataset-buckets.json",
        help="数据集桶白名单（和申请模板一起决定体检里哪些桶算「没登记」）",
    )
    # 和 `delivery hygiene` 的同名参数必须配一致：定时任务按 90 天判、网页按 180 天判的话，
    # 同一把 AK 在两个地方会有两种说法，而 _SECTIONS 那次重构就是为了消灭这种分歧
    srv.add_argument("--stale-days", type=int, default=0, help="AK 多久算该换（默认 180）")
    srv.add_argument("--unused-days", type=int, default=0, help="多久没用算闲置（默认 90）")
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

    iamp = isub.add_parser(
        "iam-push", help="把属性表增量直接下发到 IT 的云账号属性接口（替代人工发 CSV）"
    )
    iamp.add_argument("--people", default="identity/people.json")
    iamp.add_argument("--attributes", default="identity/iam-attributes.json")
    iamp.add_argument("--apply", action="store_true", help="真的下发。不给就是预演，只打印要发什么")
    iamp.add_argument(
        "--resolved",
        action="append",
        default=[],
        metavar="EMAIL",
        help="已核对无误的邮箱：上次因邮箱复用或姓名不符被跳过的行，这次正常下发",
    )
    iamp.add_argument(
        "--allow-mass-remove",
        action="store_true",
        help="确认基线里整体消失的 union_id 确实是离职/删号，允许发 remove",
    )

    iamc = isub.add_parser(
        "iam-reclaim", help="离职回收：删掉已离职的人的 cloud_accounts 属性（不碰云上账号）"
    )
    iamc.add_argument("--people", default="identity/people.json")
    iamc.add_argument("--attributes", default="identity/iam-attributes.json")
    iamc.add_argument("--apply", action="store_true", help="真的删。不给就是预演")
    iamc.add_argument("--admins", default="identity/admins.json", help="通知发给这里面的管理员")
    iamc.add_argument(
        "--force",
        action="store_true",
        help=f"确认这一轮超过 {iam_sync.RECLAIM_MAX} 个人的回收是真的（默认拒绝，防名册读残）",
    )

    iamr = isub.add_parser(
        "iam-reconcile", help="对账：IAM 侧实际的 cloud_accounts vs 名册应该是什么"
    )
    iamr.add_argument("--people", default="identity/people.json")
    iamr.add_argument("--attributes", default="identity/iam-attributes.json")

    iamn = isub.add_parser(
        "iam-remind",
        help="对账后私聊管理员：谁离职了云登录名还挂着。**只提醒不回收**（回收是人点的）",
    )
    iamn.add_argument("--people", default="identity/people.json")
    iamn.add_argument("--attributes", default="identity/iam-attributes.json")
    iamn.add_argument("--admins", default="identity/admins.json")
    iamn.add_argument(
        "--every-hours",
        type=float,
        default=24.0,
        help="同一批人多久提醒一次。天天重复的提醒等于没有提醒",
    )

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

    uf = commands.add_parser(
        "unit-failed", help="systemd OnFailure 兜底：定时任务异常退出时私聊管理员"
    )
    uf.add_argument("--unit", required=True, help="挂掉的单元名（systemd 传 %%i）")
    uf.add_argument("--admins", default="identity/admins.json")
    uf.add_argument(
        "--state",
        default=UNIT_ALERT_STATE,
        help="告警冷却记录（只存单元名和时间戳），默认 identity/alert-state.json",
    )

    rf = commands.add_parser(
        "refresh", help="定时任务：采集权限快照、生成映射提案、重建人员名册，异常时飞书告警"
    )
    rf.add_argument("--inventory", default="identity/inventory.json")
    rf.add_argument("--admins", default="identity/admins.json", help="没配 webhook 时告警私聊谁")
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
    """服务号清单。实现在 `hygiene.load_service_names` —— 面板服务端也要读同一份，
    留在这里的话服务端就得反向 import CLI（顺带把整套 argparse 拉进内存）。"""
    from . import hygiene

    return hygiene.load_service_names(path)


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


def _cmd_identity_iam_push(args) -> int:
    """算增量 → 直接调接口下发。替掉「导 CSV → 人工发给 IT → 回传结果」那一段。

    **只有全部成功才推进基线。** 有失败还推进的话，基线会声称那几行已经在 IAM 里了，
    下一轮比对就不再发它们 —— 于是永久漂移，而且没有任何地方看得出来。
    重发一个已成功的 PUT 是幂等的（接口文档明说），所以「全失败重来」的代价只是多发几条。
    """
    from . import iam_api

    cfg = iam_api.Config.from_env()
    paths = iam_sync.SyncPaths(
        people=args.people,
        attributes=args.attributes,
        out=iam_sync.DEFAULT_OUT,
        sent_dir=IAM_SENT_DIR,
    )
    warn = lambda line: print(line, file=sys.stderr)  # noqa: E731
    increment = iam_sync.compute(
        paths,
        baseline="latest",
        allow_mass_remove=args.allow_mass_remove,
        resolved_emails=frozenset(args.resolved),
        warn=warn,
    )
    counts = increment.counts
    print(f"增量：set {counts['set']} 条，remove {counts['remove']} 条，skip {counts['skip']} 条")
    for note in increment.notes:
        print(f"  ⚠ {note}")
    if not counts["set"] and not counts["remove"]:
        print("没有要下发的。")
        return 0

    results = iam_api.apply_rows(increment.rows, cfg=cfg, transport=None, dry_run=not args.apply)
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]
    for r in results:
        who = r.row.get("name") or r.row.get("email") or r.row.get("feishu_union_id")
        if r.ok:
            was = f"（原 {r.previous}）" if r.previous else ""
            print(f"  ✓ {r.action:6} {who} {r.row.get('app')} {r.row.get('value', '')}{was}")
        else:
            print(f"  ✗ {r.action:6} {who} {r.row.get('app')} —— {r.code}：{r.message}")
    if not args.apply:
        print("\n预演结束，没有下发任何东西。确认无误后加 --apply。")
        return 0

    print(f"\n成功 {len(ok)} 条，失败 {len(bad)} 条")
    if bad:
        print(
            "  **基线不推进** —— 有失败时推进会让那几行永久漏发。修掉之后重跑即可（重发是幂等的）。"
        )
        _iam_push_hints(bad)
        return 1
    archive = iam_sync.next_archive(
        Path(IAM_SENT_DIR), pending=True, stamp=time.strftime("%Y%m%d-%H%M%S.csv")
    )
    _require_identity_dir(archive.resolve())
    _require_identity_dir(archive.with_name(archive.name + ".meta.json").resolve())
    iam_sync.record(increment, archive)
    final = iam_sync.confirm(archive, Path(IAM_SENT_DIR))
    print(f"  基线已推进：{final}")
    return 0


def _iam_push_hints(bad: list) -> None:
    """失败的每一类都意味着有人要去做一件具体的事，说清楚是哪件。"""
    hints = {
        "value_taken": "这个登录名已属于另一个 IAM 用户。**一个云身份不能对应两个人** —— "
        "先确认云上那个账号到底是谁的",
        "inactive_user": "IAM 说这人已离职，拒绝写入。**该去云上禁用/回收那个 RAM 用户**，"
        "而不是想办法把属性写进去",
        "not_found": "IAM 里没有这个 union_id —— 名册和 IAM 对不上，找 IT 核对",
        "bad_request": "我们这边生成的值不合格式，检查 iam-attributes.json 的 suffix",
        "unauthorized": "token 不对或来源 IP 不在白名单。**不要把 token 打出来**，找 IT 重签",
        "upstream_error": "IAM 暂时不可用，稍后重跑",
    }
    for code in sorted({r.code for r in bad}):
        if code in hints:
            print(f"    · {code}：{hints[code]}")


def _review_paths_or_none(people: str):
    """回收要落 review.log，而那个路径由 ReviewPaths 算（名册同目录）。
    提案/人工记录这一轮用不到，给同目录的默认名即可。"""
    from . import review as review_mod

    base = Path(people).resolve().parent
    return review_mod.ReviewPaths(
        proposal=str(base / "sso-map.proposal.json"),
        manual=str(base / "manual-links.json"),
        people=people,
    )


def _cmd_identity_iam_reclaim(args) -> int:
    """离职回收。**两个信号都指向离职才自动删**，只有一个就只报不动。"""
    paths = iam_sync.SyncPaths(
        people=args.people,
        attributes=args.attributes,
        out=iam_sync.DEFAULT_OUT,
        sent_dir=IAM_SENT_DIR,
    )
    from . import notify as notify_mod
    from . import review as review_mod
    from . import roles as roles_mod

    def _log(report: dict) -> None:
        rp = _review_paths_or_none(args.people)
        if rp is not None:
            review_mod.log_iam_reclaim(
                rp, report["done"], actor="auto:iam-reclaim", held=report["held"]
            )

    def _announce(report: dict) -> None:
        """私聊每个管理员。**不发群** —— 这是要人去做事的通知，发群等于发给没有人。

        走应用机器人（`DELIVERY_FEISHU_APP_ID/SECRET`，面板本来就有），
        收件人用 **union_id** —— 名册里只有它，没有 open_id。
        """
        from .server import _tenant_token_cache

        app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
        secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
        base = os.environ.get("DELIVERY_BASE_URL", "")
        admins = roles_mod.load_admins(args.admins).union_ids
        if not (app_id and secret):
            # **不静默**：没说一声的话，没人知道通知根本没发出去
            print("  （没配飞书应用凭证，这次没发通知）")
            return
        if not admins:
            print(f"  （{args.admins} 里没有 union_ids，没人可通知）")
            return
        notifier = notify_mod.FeishuNotifier(_tenant_token_cache(app_id, secret), base)
        problems = notify_mod.notify_admins(
            notifier, admins, notify_mod.reclaim_card(report, base_url=base)
        )
        print(f"  已私聊 {len(admins) - len(problems)}/{len(admins)} 位管理员")
        for line in problems:
            print(f"    ✗ {line}", file=sys.stderr)

    report = iam_sync.reclaim_iam(
        paths,
        apply=args.apply,
        force=args.force,
        log=_log if args.apply else None,
        announce=_announce if args.apply else None,
    )
    for r in report["done"]:
        mark = "（预演）" if r.get("dry_run") else "✓"
        was = f"，原 {r['previous']}" if r.get("previous") else ""
        print(f"  {mark} 删属性 {r['username']} {r['name']} {r['app']} {r['value']}{was}")
    if report["held"]:
        print("\n  ⚠ 下面这些 IT 的 IAM 说已离职，**但我们名册里还有**，没动：")
        for r in report["held"]:
            print(f"    {r['username']} {r['name']} {r['app']} {r['value']}")
        print("    两边不一致时该去问一句，不是删。确认离职后名册会自己少掉他，下一轮就自动回收。")
    for r in report["failed"]:
        print(f"  ✗ {r.get('username') or r.get('app')}：{r.get('error')}", file=sys.stderr)
    if not report["done"] and not report["held"]:
        print("没有要回收的。")
    if not args.apply and report["done"]:
        print("\n预演结束，什么都没删。确认后加 --apply。")
    if report["done"] and args.apply:
        print("\n**云上那些 RAM/IAM 用户还在。** 禁用或删除账号不自动做 —— 到云控制台处理，")
        print("或者在面板的体检里看「已离职但云上还有号」。")
    return 1 if (report["held"] or report["failed"]) else 0


def _cmd_identity_iam_remind(args) -> int:
    """对账后提醒管理员，并**自动停用**确认离职的人的云账号（可恢复）。**从不删号、不碰数据**
    —— 删号要管理员在面板上确认，见 offboard.py。

    原先这件事没有任何触发器：`iam-reclaim` 只能手工跑，那条飞书私聊还只在 `--apply`
    时才发。于是一个人离职之后云登录名一直挂着，直到某天有人恰好打开面板那一页。

    提醒两类人，各发一张卡：
      · IT 的 IAM 标了离职、云登录名还挂着的（按 union_id 对）；
      · 名下有云账号、按 union_id 和公司邮箱在飞书通讯录里都找不到的。第一类只覆盖
        有 union_id、IAM 里有属性的人，名册里没 union_id 的、只有九章账号的都漏掉了。

    **按批去重**：同一批人默认 24 小时才再提醒一次。天天重复的提醒等于没有提醒，
    而多提醒一次的代价只是多一条消息 —— 所以去重状态读不到时**照常提醒**。
    """
    import hashlib

    from . import hygiene, iam_sync, offboard, provision
    from . import notify as notify_mod
    from . import people as people_mod
    from . import review as review_mod
    from . import roles as roles_mod
    from .identity import directory

    def sig_of(keys) -> str:
        return hashlib.md5("|".join(sorted(keys)).encode()).hexdigest()  # noqa: S324 — 只做去重

    paths = iam_sync.SyncPaths(people=args.people, attributes=args.attributes)
    app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    base = os.environ.get("DELIVERY_BASE_URL", "")
    failed = False
    cards = []

    report = iam_sync.reconcile_report(paths)
    held = iam_sync.load_snooze(paths)
    rows = [
        (e, d)
        for e in report["apps"]
        for d in e["drift"]
        if d["kind"] == "inactive" and f"{e['app']}/{d['union_id']}" not in held
    ]
    if not rows:
        print("没有「已离职但云登录名还挂着」的人")
    elif not iam_sync.claim_remind(
        paths, sig_of(f"{e['app']}/{d['union_id']}" for e, d in rows), hours=args.every_hours
    ):
        print(f"这批 {len(rows)} 人最近提醒过了，本次跳过")
    else:
        cards.append((f"{len(rows)} 人待回收", notify_mod.drift_card(report, base_url=base)))

    try:
        roster = people_mod.load(args.people).people
    except DeliveryError as exc:
        print(f"★ 名册读不了，本次不判断谁离职：{exc}", file=sys.stderr)
        roster, failed = None, True

    # ── 强信号：IT 的 IAM 标了离职，或飞书状态是已离职 → 自动停用（可恢复），等人确认删号
    statuses = None
    if roster is not None and app_id and secret:
        try:
            statuses = directory.status_of(
                [p.union_id for p in roster if p.union_id], app_id, secret
            )
        except (DeliveryError, OSError, ValueError) as exc:
            print(f"★ 飞书在职状态没查成，这一路本次不判断：{exc}", file=sys.stderr)
            failed = True
    fresh: list = []  # 这一轮新出现的待处理账号 —— 卡片上直接给按钮
    if roster is not None:
        rp = _review_paths_or_none(args.people)

        def _olog(op, rows_, actor):
            if rp is not None:
                review_mod.log_offboard(rp, op, rows_, actor=actor)

        # 飞书那一路**只认「已离职」**（offboard.resigned）。冻结 / 退出企业只提醒
        cands = offboard.strong_candidates(roster, drift_rows=rows, statuses=statuses)
        weak = offboard.weak_statuses(roster, statuses)
        if weak:
            try:
                added = offboard.note_suspects(offboard.path_beside(args.people), weak)
            except (DeliveryError, OSError, ValueError) as exc:
                print(f"★ 离职待确认没记上：{exc}", file=sys.stderr)
                failed, added = True, []
            for r in added:
                print(f"  待确认（没停用）{r['platform']}/{r['user']}：{r['signal']}")
            fresh += added  # 只有新记下的才发：记录本身就是去重
        rep = None
        if cands:
            try:
                rep = offboard.auto_disable(
                    offboard.path_beside(args.people),
                    cands,
                    lambda platform, account: provision.executor_from_env(platform, account),
                    log=_olog,
                )
            except (DeliveryError, OSError, ValueError) as exc:
                # 离职记录坏了也别吞掉其余提醒：「登录名还挂着」那张卡照发
                print(f"★ 自动停用没做成：{exc}", file=sys.stderr)
                failed = True
        if rep is not None:
            for r in rep["done"]:
                print(f"  已停用 {r['platform']}/{r['user']}（{r['person']}，{r['signal']}）")
            for r in rep["failed"]:
                print(f"  ✗ 停用失败 {r['platform']}/{r['user']}：{r['error']}", file=sys.stderr)
            if rep["held"]:
                print(f"★ {len(rep['held'])} 人超过自动停用上限，一个都没停", file=sys.stderr)
            failed = failed or bool(rep["failed"])
            # 停了号每次都说；只有「没停成 / 被上限拦下」的，同一批 24 小时说一次
            stuck = sorted(
                [f"f:{r['platform']}/{r['user']}" for r in rep["failed"]]
                + [f"h:{n}" for n in rep["held"]]
            )
            # 九章那类没接口的也要进卡片（rep["manual"]）—— 不然只有打开面板才看得到
            fresh += rep["done"] + (rep.get("manual") or [])
            # 「没停成 / 被上限拦下」单独一张说明卡，同一批 24 小时一次（停成了的走下面那张按钮卡）
            if stuck and iam_sync.claim_remind(
                paths, "off:" + sig_of(stuck), hours=args.every_hours
            ):
                cards.append(
                    (f"{len(stuck)} 个号没停成", notify_mod.offboard_card(rep, base_url=base))
                )

    # ── 弱信号：通讯录里找不到 → 只记下来等人确认，不自动停
    if not (app_id and secret):
        print("★ 没配飞书应用凭证，「通讯录里找不到」这一类本次没查", file=sys.stderr)
    elif roster is not None:
        try:
            gone = hygiene.missing_from_directory(roster, directory.staff_index(app_id, secret))
        except (DeliveryError, hygiene.DirectoryIncomplete, OSError, ValueError) as exc:
            # **不静默**：这一类没查成就是没查成，别让人以为「没有人离职」
            print(f"★ 按公司邮箱对通讯录没做成，这一类本次没有结论：{exc}", file=sys.stderr)
            failed = True
            gone = []
        if gone:
            try:
                fresh += offboard.note_suspects(offboard.path_beside(args.people), gone)
            except (DeliveryError, OSError, ValueError) as exc:
                print(f"★ 离职待确认没记上：{exc}", file=sys.stderr)
                failed = True
        if not gone:
            if not failed:
                print("没有「通讯录里找不到、云账号还在」的人")
        else:
            # 提醒去重靠离职记录本身（同一个号只会新记一次），不用再压一层时间窗
            for p in gone:
                print(f"  通讯录里找不到：{p.name} {p.email}")

    if fresh:
        # 按钮卡：确认删除 / 没离职都能在飞书里直接点（回调走 /feishu/card）
        cards.append((f"{len(fresh)} 个号待处理", notify_mod.pending_card(fresh, base_url=base)))

    if not cards:
        return 1 if failed else 0
    what = "；".join(label for label, _ in cards)
    admins = roles_mod.load_admins(args.admins).union_ids
    if not (app_id and secret):
        print(f"★ {what}，但没配飞书应用凭证，这次没发通知", file=sys.stderr)
        return 1
    if not admins:
        print(f"★ {what}，但 {args.admins} 里没有 union_ids", file=sys.stderr)
        return 1
    from .server import _tenant_token_cache

    notifier = notify_mod.FeishuNotifier(_tenant_token_cache(app_id, secret), base)
    problems = []
    for _label, card in cards:
        problems += notify_mod.notify_admins(notifier, admins, card)
    print(f"{what}，已私聊 {len(admins)} 位管理员（失败 {len(problems)} 次）")
    for line in problems:
        print(f"  ✗ {line}", file=sys.stderr)
    return 1 if (problems or failed) else 0


def _cmd_identity_iam_reconcile(args) -> int:
    """IAM 侧实际 vs 名册。**这是以前做不到的事。**

    没有这个接口时，面板只能拿自己存的基线当真相，而基线只记录「我们发过什么」，
    不记录「IT 那边最后变成了什么」。中间任何一次人工导入出错，两边就永久漂移且无人察觉。

    比对逻辑在 `iam_sync.reconcile_report`，和面板共用 —— 规则只写一份。
    """
    from . import iam_api

    paths = iam_sync.SyncPaths(
        people=args.people,
        attributes=args.attributes,
        out=iam_sync.DEFAULT_OUT,
        sent_dir=IAM_SENT_DIR,
    )
    report = iam_sync.reconcile_report(paths)
    labels = {
        iam_api.DRIFT_INACTIVE: "已离职但云登录名还挂着 —— 去云上禁用该账号",
        iam_api.DRIFT_DIFFERENT: "两边值不一样 —— 最危险的一类，SSO 会登进错的账号",
        iam_api.DRIFT_LEFT: "IAM 有、名册没有",
        iam_api.DRIFT_MISSING: "名册有、IAM 没有 —— 该发没发",
        iam_api.DRIFT_GONE: "属性指向的云账号已不存在 —— 多半是有人在控制台直接删了，"
        "人登不进去且属性看着是好的",
    }
    for entry in report["apps"]:
        if entry["error"]:
            print(f"\n{entry['scope']}：✗ {entry['error']}", file=sys.stderr)
            continue
        print(
            f"\n{entry['app']}：IAM {entry['theirs']} 人，"
            f"名册 {entry['compared']} 人可比对，对不上 {len(entry['drift'])} 条"
        )
        if entry["blind"]:
            print(f"  ⚠ 另有 {len(entry['blind'])} 条没有 union_id，**没进比对也发不出去**：")
            for r in entry["blind"][:10]:
                print(f"    {r['name']} {r['email']} → {r['value']}")
            if len(entry["blind"]) > 10:
                print(f"    … 还有 {len(entry['blind']) - 10} 条")
        for kind in (
            iam_api.DRIFT_INACTIVE,
            iam_api.DRIFT_DIFFERENT,
            iam_api.DRIFT_GONE,
            iam_api.DRIFT_LEFT,
            iam_api.DRIFT_MISSING,
        ):
            rows = [d for d in entry["drift"] if d["kind"] == kind]
            if not rows:
                continue
            print(f"  {labels[kind]}（{len(rows)}）")
            for d in rows[:20]:
                who = f"{d['username']} {d['name']}".strip() or d["union_id"]
                if kind == iam_api.DRIFT_DIFFERENT:
                    print(f"    {who}：IAM={d['theirs']}  名册={d['ours']}")
                else:
                    print(f"    {who}：{d['theirs'] or d['ours']}")
            if len(rows) > 20:
                print(f"    … 还有 {len(rows) - 20} 条")
    return 1 if report["total"] else 0


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


#: 跑完了、发现了问题、**这个问题有人会知道**。和「进程崩了 / 这一轮没干成活」（1）分开，
#: 这样 systemd 的 OnFailure 兜底只在后一种情况触发。
#:
#: 两个使用者对「有人会知道」的兑现方式不同，别按其中一个去理解另一个：
#:   · `refresh` —— 它自己先把告警私聊出去了，送到了才返 3（见 `_cmd_refresh` 末尾）。
#:   · `requests sweep` —— 它**不发告警**，问题留在单子上、出现在管理后台待办页。
#:     一分钟一轮的定时器不适合把每个办不了的单子变成一条私聊。
#: 见 deploy/panel/delivery-refresh.service、delivery-sweep.service 的 SuccessExitStatus。
EXIT_REPORTED = 3


def _admin_alert(title: str, text: str, admins_path: str = "") -> str:
    """私聊管理员一张告警卡。返回没发出去的原因（空串 = 发出去了）。

    用的是面板自己的飞书应用（`DELIVERY_FEISHU_APP_ID/SECRET`），不依赖群机器人 webhook ——
    定时任务的环境文件里本来就有它（拉通讯录要用）。
    """
    from . import notify as notify_mod
    from . import roles as roles_mod

    app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    if not (app_id and secret):
        return "没配 DELIVERY_FEISHU_APP_ID / DELIVERY_FEISHU_APP_SECRET"
    try:
        admins = roles_mod.load_admins(admins_path or "identity/admins.json").union_ids
    except Exception as exc:  # noqa: BLE001
        return f"读不了管理员名单：{type(exc).__name__}"
    if not admins:
        return "管理员名单里没有 union_id"
    from .server import _tenant_token_cache

    notifier = notify_mod.FeishuNotifier(_tenant_token_cache(app_id, secret), "")
    failed = notify_mod.notify_admins(notifier, admins, notify_mod.alert_card(title, text))
    if failed and len(failed) >= len(set(admins)):
        return "；".join(failed)[:300]
    return ""


#: 同一个单元连续失败时，最多这么久私聊一次。
#:
#: 定时器最密的是 sweep（1 分钟一轮）。一个不会自己好的故障 = 一天 1440 条私聊，
#: 而被刷屏的人第二天就把这个机器人折叠了 —— 之后真出事也没人看。收敛到 6 小时：
#: 一天最多 4 条，既提醒得住，也不至于让人关掉。
UNIT_ALERT_COOLDOWN = 6 * 3600
#: 记「上次为哪个单元告过警」：只有单元名和时间戳，没有任何员工数据。
#:
#: **刻意不放 identity/。** 放那儿的话，为了写这一个文件就得给
#: `delivery-unit-failed@.service` 开整个 identity/ 的写权限 —— 而那个单元带着飞书
#: 应用凭证做出站请求，是本机最靠外的进程之一，凭空获得覆盖 tickets.json /
#: people.json / admins.json 的能力（审计 Med-3）。用 systemd 的 `StateDirectory=delivery`：
#: 目录由 systemd 建、属主自动是 delivery、重启保留，也就不会有「root 手跑一次
#: 把属主改成 root、冷却从此静默失效」那个老坑。
#: **优先读 systemd 自己注入的 `$STATE_DIRECTORY`**：只推 `src/` 不更新 unit 文件是这台机器的
#: 部署惯例，而那样一来进程里既没有 `DELIVERY_ALERT_STATE`、unit 里也没有 `StateDirectory=`，
#: `ProtectSystem=strict` 下写 `/var/lib/delivery` 直接 PermissionError —— 冷却完全不生效、
#: 刷屏原样回来，而线索只有 journal 里一行「告警冷却记不下来」。读 `$STATE_DIRECTORY` 的话，
#: 单元里漏写 `Environment=` 那行也不会错（审计 Med-2）。
_STATE_DIR = os.environ.get("STATE_DIRECTORY", "").split(":")[0]
UNIT_ALERT_STATE = os.environ.get("DELIVERY_ALERT_STATE") or (
    f"{_STATE_DIR}/alert-state.json" if _STATE_DIR else "/var/lib/delivery/alert-state.json"
)


def _load_alert_state(file: Path) -> Optional[dict]:
    """状态文件 → dict。读不了返回 None（调用方当「没记过」，照发）。"""
    try:
        if not file.exists():
            return {}
        got = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else {}


def _save_alert_state(file: Path, state: dict) -> None:
    try:
        # 走仓库现成的原子写：临时文件名随机（`mkstemp`），六个单元共用这个告警单元、
        # 两个同一秒挂掉时不会互相写坏对方的临时文件。写坏一次的代价是永久的 ——
        # `_load_alert_state` 读不懂就直接返回、再也不重写，冷却从此关闭（审计 Low-1）
        _atomic_private_write(file, json.dumps(state, ensure_ascii=False, indent=2).encode())
    except OSError as exc:
        # 写不下去（目录只读、盘满）：下一次还是会发。刷屏好过静默
        print(f"（告警冷却记不下来：{type(exc).__name__} {exc}）", file=sys.stderr)


def _alert_cooldown(unit: str, path: str, now: float) -> tuple:
    """`(要不要发, 这是连续第几次)`。读写状态文件出任何问题都返回「发」。

    **宁可重复也别漏报**：这个文件只是为了少刷屏，它坏了不该把告警通道一起带走。

    **这里只记「又失败了一次」，不动 `last_alert`。** 冷却的计时起点是「告警真的送到」，
    由 `_alert_sent` 在发成功之后写 —— 在这里顺手写上的话，飞书抖一下（500 / token 没注入）
    那条没人收到的告警照样开启了 6 小时静默：第一条真正送达的告警要等到六小时后。
    """
    file = Path(path or UNIT_ALERT_STATE)
    state = _load_alert_state(file)
    if state is None:
        return True, 0  # 读不了就当没记过
    units = state.get("units")
    units = units if isinstance(units, dict) else {}
    rec = units.get(unit)
    rec = rec if isinstance(rec, dict) else {}
    try:
        last = float(rec.get("last_alert") or 0)
        streak = int(rec.get("streak") or 0)
    except (TypeError, ValueError):
        last, streak = 0.0, 0
    # **落在未来的时刻不认**：机器时钟跳变、或者有人手改过这个文件，都会让 now-last 恒为负，
    # 于是这个单元被静音到真实时间追上为止 —— 而且它自己好不了。这个模块是 fail-open 的，
    # 「时刻不合常理」比「读不懂」更该发出来
    if last > now:
        print(f"（{unit} 的上次告警时刻在未来，按没记过处理）", file=sys.stderr)
        last = 0.0
    # **隔够久的一次失败是「新故障」，连号要归零。** 不归零的话，一个恢复了三个月、
    # 今天重新挂掉的单元，告警会写「这是连续第 400 次」—— 把人往「已经挂很久了」带偏，
    # 正是这次改动要消灭的那类「告警说假话」（审计 Low-6）
    try:
        last_fail = float(rec.get("last_fail") or 0)
    except (TypeError, ValueError):
        last_fail = 0.0
    if last_fail and now - last_fail > 2 * UNIT_ALERT_COOLDOWN:
        streak = 0
    # 冷却期外 = 上次告警之后它一直没好（或者刚坏）：这条要发，连号继续往上加
    send = now - last >= UNIT_ALERT_COOLDOWN
    streak += 1
    units[unit] = {"streak": streak, "last_fail": now, "last_alert": last}
    state["units"] = units
    _save_alert_state(file, state)
    return send, streak


def _alert_sent(unit: str, path: str, now: float) -> None:
    """告警**确实送到**之后才开始计冷却。发不出去不算，下一轮还要再试。"""
    file = Path(path or UNIT_ALERT_STATE)
    state = _load_alert_state(file)
    if state is None:
        return
    units = state.get("units")
    units = units if isinstance(units, dict) else {}
    rec = units.get(unit)
    rec = dict(rec) if isinstance(rec, dict) else {}
    rec["last_alert"] = now
    units[unit] = rec
    state["units"] = units
    _save_alert_state(file, state)


#: 手工跑这条命令时正文里的标记。**人工触发和真故障长得一模一样**，
#: 于是每次有人验证告警通道，收件人都要白紧张一次并去查一遍日志（2026-09-23 真发生过）。
_DRILL = "（**这是手工触发的演习，不是真故障**）"


#: 演习专用的实例名。`systemctl start delivery-unit-failed@drill.service`
_DRILL_UNITS = frozenset({"drill", "drill.service"})


def _is_drill(unit: str) -> bool:
    """这条是人手工跑出来的，不是 systemd 的 `OnFailure` 拉起来的。

    **不能只靠「`$MONITOR_UNIT` 缺失」推断**，那个前提比想象中窄得多：

      · `MONITOR_*` 是 systemd **v251** 才有的（本项目的开发机是 249 就已经不成立）；
      · 真正的条件也不是「触发方 `Type=oneshot`」，而是「同一个 handler 实例只能有
        一个触发方」—— 这正是 `OnFailure=…@%n.service` 那个 `%n` 在保证的事。

    前提一破，**每一条真告警的标题都会变成「告警演习」**，而且一声不吭。那是最坏的
    失效方向：收件人看一眼标题就划过去，而兜底告警唯一要拦的就是那一刻。

    所以判据改成**演习自报家门**，三层依次问：
      1. `MONITOR_UNIT` 在 → 确定是 systemd 拉起来的，真故障；
      2. 实例名就是演习专用的那个 → 演习；
      3. 连 `INVOCATION_ID` 都没有 → 根本不是 systemd 起的，人手跑的。
         （`INVOCATION_ID` 对**任何** systemd 起的单元都注入，v232 起，老得足够安全，
         而且和 `MONITOR_*` 的注入条件相互独立。）
    老 systemd 或 host 侧配置漂移时，落到「真故障」这一侧 —— 安全的那一侧。
    """
    if os.environ.get("MONITOR_UNIT") is not None:
        return False
    if str(unit or "").strip() in _DRILL_UNITS:
        return True
    if os.environ.get("INVOCATION_ID"):
        # systemd 起的、却没给 MONITOR_* —— 老版本或者 OnFailure 写法被改过。
        # 按真故障处理，但留一行痕：不然这种「标题一直不对」没人查得出来
        print(
            "[unit-failed] systemd 没注入 MONITOR_*（v251 以下，或同一 handler 有多个触发方），"
            "本条按真故障处理",
            file=sys.stderr,
        )
        return False
    return True


def _how_it_died(unit: str) -> str:
    """systemd 亲口说的退出情况。是演习就直接说是演习（判据见 `_is_drill`）。

    `OnFailure=` 拉起的单元里 systemd 会注入 `$MONITOR_*`（**v251 起**，且要求
    「同一个 handler 实例只有一个触发方」—— `@%n` 保证的就是这个）。拿得到的话
    **比「让人自己去日志里分辨」强得多**：退出码是 1 还是被信号杀掉，这里一句话说清，
    而那正是收到告警的人第一个要问的问题。

    拿不到也不影响判定是不是演习 —— 那件事已经由 `_is_drill` 用别的依据回答了，
    这里只是少说一句「怎么死的」。
    """
    if _is_drill(unit):
        return _DRILL

    # 值来自环境变量，终究是外部输入：带换行的话会把卡片上「去哪看日志」那几行挤掉
    # （alert_card 只留前 24 行）。压成单行，同 notify._clip 的做法
    def _flat(name: str) -> str:
        return " ".join(str(os.environ.get(name, "")).split())

    result = _flat("MONITOR_EXIT_CODE")  # exited / killed / dumped…
    status = _flat("MONITOR_EXIT_STATUS")
    if result == "exited" and status:
        return f"（退出码 {status}）"
    if result and status:
        # 被信号杀掉的进程没有退出码，这里**不能**写「退出码」—— 那是假话
        return f"（{result}：{status}）"
    # `MONITOR_EXIT_CODE/STATUS` 还额外要求主进程真的跑起来并退出过，而
    # `MONITOR_SERVICE_RESULT` 是无条件给的。**主进程压根没起来**的那些真故障
    # （start-limit-hit「start request repeated too quickly」、ExecStartPre 失败、
    # exec 之前就超时）只有它说得出话 —— 对一分钟一轮的 sweep，start-limit-hit
    # 是相当现实的一种，没有兜底的话正文一个字都不说
    verdict = _flat("MONITOR_SERVICE_RESULT")
    return f"（systemd 判定：{verdict}）" if verdict else ""


def _cmd_unit_failed(args) -> int:
    """systemd 的 OnFailure 兜底：定时任务**自己没来得及发告警**就结束了。

    崩溃、超时被杀、依赖导入失败 —— 这几种情况进程拿不到报告，也就发不出告警。
    不兜底的话，一个每小时跑一次的任务可以连着挂好几天没人知道。

    **文案不许替 systemd 猜原因。** 原先写死「崩溃 / 超时被杀 / 依赖导入失败」，
    而 2026-09-23 线上真正的原因是第四种：任务跑完了、只是退出码非零。
    三句猜测全错，人照着去查日志什么也查不到 —— 告警说假话比不报还糟。
    """
    unit = str(args.unit or "").strip() or "（未知单元）"
    now = time.time()
    state_path = getattr(args, "state", "") or UNIT_ALERT_STATE
    drill = _is_drill(unit)
    how = _how_it_died(unit)
    # **演习记在另一把键下。** 拿真单元名演一次（2026-09-23 就这么干过），
    # 那条演习是真发出去的、也真记冷却 —— 接下来 6 小时这个单元真挂了管理员收不到，
    # journal 里只有「仍在失败，第 N 次」。记在 `drill:` 下之后，真单元的冷却和连号
    # 都不受污染，靠代码关掉这件事，不靠人记住文档
    key = f"drill:{unit}" if drill else unit
    send, streak = _alert_cooldown(key, state_path, now)
    hours = UNIT_ALERT_COOLDOWN // 3600
    if not send:
        # 打 `key` 不打 `unit`：演习被挡住时打真单元名的话，journal 里看起来像那个
        # 真单元在挂 —— 而它可能好好的，正在挂的只是有人连演了两次
        print(f"（{key} 仍在失败，第 {streak} 次；{hours} 小时内不重复私聊）")
        return 0
    again = f"这是连续第 {streak} 次（{hours} 小时内只私聊一次）。\n" if streak > 1 else ""
    # **别在这里教人加 SuccessExitStatus。** 原先那句是这么写的，而这批改完之后
    # sweep 最常见的失败恰恰是「到期回收没收干净」—— 日志里没 traceback、报告也出全了，
    # 完全符合那句话的判据。照做就是 SuccessExitStatus=1，而 1 同时是「整步崩了」
    # 「存储读不了」的退出码 → 兜底告警从此永久失效。告警不该给出会关掉自己的建议。
    text = (
        f"{unit} 这一轮没跑成。{how}\n"
        f"{again}"
        f"看日志：journalctl -u {unit} -n 80 --no-pager\n"
        "最后几行有 Python 报错 = 真崩了（或者被超时杀掉、依赖导入失败）。\n"
        "没有报错、报告也出全了 = 任务跑完了，但这一轮有活没干成，三种：\n"
        "  · 到期回收没收干净 —— 管理后台待办页\n"
        "  · 审批同步整步跳过（飞书不可用）—— 日志里有「飞书审批不可用」\n"
        "  · 申请单存储读不了 —— 日志里有「读不了申请单存储」"
    )
    # **标题问 `_is_drill()`，不要拿正文串去比。** 耦合在一个字符串上的话，将来
    # `_how_it_died` 只要给演习串加点修饰，标题就会静默退回「定时任务没跑成」
    title = "告警演习" if drill else "定时任务没跑成"
    why = _admin_alert(f"{title}：{unit}", text, args.admins)
    if why:
        # 没送到就**不开始计冷却**：下一轮还要再试，否则一次飞书抖动换来六小时静默
        print(f"兜底告警没发出去：{why}", file=sys.stderr)
        return 1
    _alert_sent(key, state_path, now)
    print(f"已私聊管理员：{unit} 没跑成")
    return 0


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
    sent = False
    alert_conf = alerts.from_env(os.environ)
    if alert_conf is not None:
        try:
            alerts.send_feishu(text, webhook=alert_conf[0], secret=alert_conf[1])
            print("已发送飞书告警")
            sent = True
        except alerts.AlertError as exc:
            print(f"  ⚠ {exc}")
    if not sent:
        # **没配群机器人 webhook 时私聊管理员。** 原先这里只打一行「没设置 webhook」就退出 ——
        # 线上一直没配，所以刷新出的每一个问题都只进了日志，从来没有人被通知过
        why = _admin_alert("云权限面板数据刷新异常", text, getattr(args, "admins", ""))
        if why:
            print(f"  ⚠ 告警没发出去：{why}")
            return 1
        print("已私聊管理员")
    # 跑完了、有问题、告警**已经送到** → 退出码 3，单元里声明成正常结束。
    # 仍然返回 1 的话，OnFailure 兜底会再私聊一遍「服务失败」—— 同一件事收两条
    return EXIT_REPORTED if code else 0


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
        from . import offline_accounts as offline_mod

        # 人工登记表读不了就**整轮不刷新名册**：带着一张缺了九章的快照往下走，
        # 名册会把那些人的九章号全抹掉，离职检查也就再也看不到它们
        try:
            offline_rows = offline_mod.load(offline_mod.beside(args.inventory))
        except offline_mod.OfflineError as exc:
            # 报告由外层 `_cmd_refresh` 统一打印、统一告警 —— 这里再打一遍，日志里就是两份
            report.problems.append(str(exc))
            return 1

        def collect_proposal() -> dict:
            accounts = []
            for prefix in profiles:
                accounts += cloudcollect.collect_aliyun(
                    aliyun.Credentials.from_env(prefix), progress=say
                )
            accounts += cloudcollect.collect_volcano(volcano.Credentials.from_env(), progress=say)
            # 没有采集接口的平台（九章）：人工登记的账号也要进名册匹配，
            # 否则「谁拥有这个九章号」永远对不上人，离职检查也就查不到它
            accounts += offline_mod.cloud_accounts(offline_rows)
            services = list(args.service) + _load_service_names("identity/services.json")
            return propose(
                accounts,
                domain=args.domain,
                service_names=services,
                trust_unverified_when_derivable=args.trust_unverified_when_derivable,
            ).to_dict()

        refresh.run(
            # 人工登记表里的号出处已经写在登记表里了（谁导出、哪天），不算「来路不明」。
            # 不排除的话，每往登记表里加一个人，都会被当成「不是面板开的、请补登记」报一次
            known_users=_panel_issued_users(args, report.problems)
            | {
                f"{a['platform']}/{a['account']}/{u['name']}"
                for a in offline_rows
                for u in a["users"]
            },
            collect_snapshot=lambda: _with_offline(
                build_snapshot(_snapshot_jobs(profiles, ()), progress=say), offline_rows
            ),
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
            if args.identity_command == "iam-push":
                return _cmd_identity_iam_push(args)
            if args.identity_command == "iam-reclaim":
                return _cmd_identity_iam_reclaim(args)
            if args.identity_command == "iam-reconcile":
                return _cmd_identity_iam_reconcile(args)
            if args.identity_command == "iam-remind":
                return _cmd_identity_iam_remind(args)
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
                services_path=args.services,
                dataset_buckets_path=args.dataset_buckets,
                stale_days=args.stale_days,
                unused_days=args.unused_days,
                sessions_path=_sessions_path(args),
                downloads_path=args.downloads,
                auth=args.auth,
            )
            return 0
        if args.command == "inventory":
            return _cmd_inventory_collect(args)
        if args.command == "unit-failed":
            return _cmd_unit_failed(args)
        if args.command == "refresh":
            return _cmd_refresh(args)
        if args.command in (
            "request",
            "requests",
            "approval",
            "assets",
            "policies",
            "hygiene",
            "dataset",
            "workspaces",
        ):
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


def _with_offline(snapshot: dict, rows: list) -> dict:
    """把人工登记的账号拼进云上采集的快照。云上那部分一个字不动。"""
    from . import offline_accounts as offline_mod

    snapshot = dict(snapshot)
    snapshot["accounts"] = list(snapshot.get("accounts") or []) + offline_mod.snapshot_accounts(
        rows
    )
    return snapshot


if __name__ == "__main__":
    raise SystemExit(main())
