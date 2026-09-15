"""统一 CLI：云账号申请与访问凭证。和面板调同一套后端接口，规则只在服务端。

  delivery request templates                 能申请什么（不能申请的会写原因）
  delivery request new <模板> --reason ...   提交申请，以你的名义发起飞书审批
  delivery request list [--all]              我的申请
  delivery request show <申请单号>
  delivery request withdraw <申请单号>
  delivery request policies [--account 平台/ID] [--search 关键字]
                                             权限列表：全部权限策略，标出已有 / 申请中 / 不开放
  delivery request grant --account 平台/ID --policy 策略名 [--policy ...] --days N --reason ...
                                             按策略申请权限（以你的名义发起飞书审批）
  delivery creds <申请单号> [--format env|json] [--hours N]
                                             领取临时凭证。env 格式可以直接 eval，
                                             官方 aliyun / ve CLI 和 SDK 都认这些环境变量

  delivery requests sweep                    服务端定时任务：同步审批、到期回收权限、过期凭证
  delivery policies collect                  服务端：采集两家云的权限策略目录（只读）
  delivery approval widgets --code <审批定义编号>
                                             管理员配置飞书审批表单时查控件 ID

凭证只打印到标准输出，不写本地文件；提示信息走标准错误，`eval "$(delivery creds ...)"` 不会混进去。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .errors import DeliveryError
from .session import load_session

_TIMEOUT = 30


class ClientError(DeliveryError):
    """后端返回错误。"""


class PanelClient:
    def __init__(self, server: str, token: str):
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ClientError(f"后端地址不对：{server!r}")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ClientError("后端地址必须是 https（本机调试除外）：会话令牌不能明文传输")
        self.server = server.rstrip("/")
        self._token = token

    @classmethod
    def from_session(cls) -> PanelClient:
        session = load_session()
        if session is None:
            raise ClientError("未登录。运行： delivery login")
        if session.expired:
            raise ClientError("会话已过期，请重新 delivery login")
        server = session.server or os.environ.get("DELIVERY_SERVER", "")
        if not server:
            raise ClientError("不知道后端地址：重新 delivery login --server <地址>")
        return cls(server, session.token)

    def request(self, method: str, path: str, body=None) -> dict:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "X-Panel-Request": "1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        # server 在 __init__ 里限定了协议
        req = urllib.request.Request(  # noqa: S310
            self.server + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode() or "{}").get("error")
            except ValueError:
                message = None
            if exc.code == 401:
                raise ClientError("会话无效或已过期，请重新 delivery login") from None
            raise ClientError(message or f"请求失败（HTTP {exc.code}）") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ClientError(f"连不上后端 {self.server}：{type(exc).__name__}") from None


def add_parsers(commands) -> None:
    req = commands.add_parser("request", help="云账号申请：开账号、云账号权限、访问凭证")
    sub = req.add_subparsers(dest="request_command", required=True)
    sub.add_parser("templates", help="列出可以申请的模板")
    new = sub.add_parser("new", help="提交申请（以你的名义发起飞书审批）")
    new.add_argument("template", help="模板 id，见 delivery request templates")
    new.add_argument("--reason", required=True, help="申请理由，审批人会看到")
    new.add_argument("--user", default="", help="权限申请：给哪个子账号（必须是你自己的）")
    new.add_argument("--days", type=int, default=None, help="权限申请：需要多少天")
    new.add_argument("--hours", type=int, default=None, help="凭证申请：每次领取的有效小时数")
    new.add_argument("--username", default="", help="开账号：子账号用户名")
    pol = sub.add_parser("policies", help="权限列表：全部权限策略和你的状态")
    pol.add_argument("--account", default="", help="只看某个云账号，格式 平台/账号ID")
    pol.add_argument("--search", default="", help="按策略名、说明、产品过滤")
    pol.add_argument("--all", action="store_true", help="包括已有和不开放的")
    grant = sub.add_parser("grant", help="按策略申请权限（以你的名义发起飞书审批）")
    grant.add_argument("--account", required=True, help="云账号，格式 平台/账号ID")
    grant.add_argument(
        "--policy", action="append", required=True, metavar="NAME", help="策略名，可重复"
    )
    grant.add_argument(
        "--type", choices=("System", "Custom"), default=None, help="同名时指定策略类型"
    )
    grant.add_argument("--days", type=int, required=True, help="需要多少天")
    grant.add_argument("--reason", required=True, help="申请理由，审批人会看到")
    ls = sub.add_parser("list", help="我的申请")
    ls.add_argument("--all", action="store_true", help="包括已结束的")
    for name, help_text in (("show", "查看申请详情"), ("withdraw", "撤回待审批的申请")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("id", help="申请单号")

    creds = commands.add_parser("creds", help="领取已批准的临时访问凭证")
    creds.add_argument("id", help="访问凭证申请的申请单号")
    creds.add_argument("--format", choices=("env", "json"), default="env")
    creds.add_argument(
        "--hours", type=int, default=None, help="这次要多长时间（不超过申请时的时长）"
    )

    reqs = commands.add_parser("requests", help="服务端维护：同步审批、到期回收")
    rsub = reqs.add_subparsers(dest="requests_command", required=True)
    sweep = rsub.add_parser("sweep", help="定时任务：同步待审批、回收到期权限、标记过期凭证")
    sweep.add_argument("--tickets", default="identity/tickets.json")
    sweep.add_argument("--templates", default="identity/request-templates.json")
    sweep.add_argument("--approval", default="identity/approval.json")
    sweep.add_argument("--people", default="identity/people.json")
    sweep.add_argument("--proposal", default="identity/sso-map.proposal.json")
    sweep.add_argument(
        "--manual", default="identity/manual-links.json", help="开账号成功后把新账号对应给申请人"
    )
    sweep.add_argument(
        "--policies",
        default="identity/policies.json",
        help="权限策略目录（按策略申请的单子开通前核对）",
    )
    sweep.add_argument("--policy-rules", default="identity/policy-rules.json")

    policies = commands.add_parser("policies", help="权限策略目录（服务端采集）")
    psub = policies.add_subparsers(dest="policies_command", required=True)
    pcollect = psub.add_parser("collect", help="采集两家云的全部权限策略（只读）")
    pcollect.add_argument("--out", default="identity/policies.json")
    pcollect.add_argument("--aliyun-profile", action="append", default=[], metavar="PREFIX")
    pcollect.add_argument("--skip-volcano", action="store_true")

    assets = commands.add_parser("assets", help="云账号资产：我能看到的云账号里有哪些资源")
    asub_assets = assets.add_subparsers(dest="assets_command")
    collect = asub_assets.add_parser("collect", help="服务端：用资源中心采集资产快照（只读）")
    collect.add_argument("--out", default="identity/assets.json")
    collect.add_argument("--aliyun-profile", action="append", default=[], metavar="PREFIX")
    collect.add_argument("--skip-volcano", action="store_true")

    ap = commands.add_parser("approval", help="飞书审批配置辅助")
    asub = ap.add_subparsers(dest="approval_command", required=True)
    widgets = asub.add_parser("widgets", help="查审批定义的表单控件 ID")
    widgets.add_argument("--code", required=True, help="审批定义编号 approval_code")


def dispatch(args: argparse.Namespace):
    """返回退出码；不是本模块的命令返回 None。"""
    if args.command == "request":
        return _request(args)
    if args.command == "creds":
        return _creds(args)
    if args.command == "requests":
        return _sweep(args)
    if args.command == "approval":
        return _widgets(args)
    if args.command == "assets":
        return _assets(args)
    if args.command == "policies":
        return _policies_collect(args)
    return None


def _request(args) -> int:
    client = PanelClient.from_session()
    cmd = args.request_command
    if cmd == "templates":
        data = client.request("GET", "/api/requests/options")
        for o in data.get("options", []):
            mark = "✓" if o.get("available") else "·"
            scope = f"{o['platform']}/{o['account']}"
            print(f"{mark} {o['id']:<28} {o['kind_label']:<8} {o['title']}  [{scope}]")
            if not o.get("available"):
                print(f"    {o.get('unavailable_reason', '')}")
        return 0
    if cmd == "new":
        payload: dict = {}
        if args.user:
            payload["cloud_user"] = args.user
        if args.days is not None:
            payload["days"] = args.days
        if args.hours is not None:
            payload["hours"] = args.hours
        if args.username:
            payload["username"] = args.username
        data = client.request(
            "POST",
            "/api/requests",
            {"template_id": args.template, "payload": payload, "reason": args.reason},
        )
        r = data["request"]
        print(f"已提交 {r['id']}：{r['status_label']}")
        print(f"  {r['summary']}")
        if r["status"] == "pending_approval":
            print("  飞书审批已经以你的名义发起。查看进度： delivery request show " + r["id"])
        return 0 if r["status"] != "submit_failed" else 1
    if cmd == "policies":
        return _list_policies(client, args)
    if cmd == "grant":
        return _grant(client, args)
    if cmd == "list":
        data = client.request("GET", "/api/requests")
        rows = [r for r in data.get("requests", []) if args.all or r.get("open")]
        if not rows:
            print("没有进行中的申请" if not args.all else "没有申请")
            return 0
        for r in rows:
            print(
                f"{r['id']}  {r['status_label']:<6}  {r['kind_label']:<6}  {r['template']['title']}"
            )
        return 0
    path = f"/api/requests/{urllib.parse.quote(args.id, safe='')}"
    if cmd == "show":
        r = client.request("GET", path)["request"]
        print(f"{r['id']}  {r['status_label']}  {r['kind_label']} · {r['template']['title']}")
        print(f"  内容：{r['summary']}")
        print(f"  理由：{r['reason']}")
        if r.get("valid_until"):
            print(f"  领取截止：{r['valid_until']}")
        if r.get("expires_at"):
            print(f"  权限到期：{r['expires_at']}")
        for e in r.get("events", []):
            note = f"  {e['note']}" if e.get("note") and e["note"] != e["label"] else ""
            print(f"  · {e['at']}  {e['label']}（{e['actor']}）{note}")
        if r["actions"].get("credential"):
            print(f'\n  领取凭证： eval "$(delivery creds {r["id"]})"')
        return 0
    if cmd == "withdraw":
        r = client.request("POST", path + "/withdraw")["request"]
        print(f"{r['id']}：{r['status_label']}")
        return 0
    raise DeliveryError(f"未知子命令 {cmd}")


def _creds(args) -> int:
    client = PanelClient.from_session()
    body = {"hours": args.hours} if args.hours is not None else {}
    data = client.request(
        "POST", f"/api/requests/{urllib.parse.quote(args.id, safe='')}/credential", body
    )
    c = data["credential"]
    print(f"# 临时凭证 {c['platform']}/{c['account']}，{c['expiration']} 失效", file=sys.stderr)
    if args.format == "json":
        print(json.dumps(c, ensure_ascii=False))
        return 0
    if c["platform"] == "volcano":
        pairs = {
            "VOLCENGINE_ACCESS_KEY": c["access_key_id"],
            "VOLCENGINE_SECRET_KEY": c["access_key_secret"],
            "VOLCENGINE_SESSION_TOKEN": c["security_token"],
        }
    else:
        pairs = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": c["access_key_id"],
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": c["access_key_secret"],
            "ALIBABA_CLOUD_SECURITY_TOKEN": c["security_token"],
        }
    for key, value in pairs.items():
        print(f"export {key}={shlex.quote(value)}")
    return 0


def _sweep(args) -> int:
    from . import notify as notify_mod
    from . import people as people_mod
    from . import policies as policies_mod
    from . import review as review_mod
    from .approval import ApprovalConfig, FeishuApproval
    from .catalog import load as load_catalog
    from .cli import _require_identity_dir
    from .flows import Flows
    from .identity.directory import tenant_token
    from .provision import executor_from_env
    from .tickets import CLAIMABLE, PENDING, TicketStore

    # 申请单、名册、人工记录都含员工信息：路径必须在 gitignored 的 identity/ 下
    policies_path = getattr(args, "policies", "identity/policies.json")
    rules_path = getattr(args, "policy_rules", "identity/policy-rules.json")
    for path in (args.tickets, args.people, args.manual, policies_path, rules_path):
        _require_identity_dir(Path(path).resolve())
    problems = 0
    app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    approval = None
    token_fn = None
    # 飞书不可用（密钥轮换、接口故障、审批配置写坏）只影响审批同步和通知；到期回收照常执行
    try:
        config = ApprovalConfig.load(args.approval)
        wants_notify = os.environ.get(notify_mod.ENV_NOTIFY) == "1"
        if app_id and secret and (config is not None or wants_notify):
            token = tenant_token(app_id, secret)
            token_fn = lambda: token  # noqa: E731
            if config is not None:
                approval = FeishuApproval(config, token_fn)
    except Exception as exc:  # noqa: BLE001
        problems += 1
        print(f"飞书审批不可用，本次跳过审批同步：{_brief(exc)}")
    notify = notify_mod.from_env(os.environ, token=token_fn)
    bindings = str(Path(args.people).with_name("bindings.json"))
    paths = review_mod.ReviewPaths(
        proposal=args.proposal, manual=args.manual, people=args.people, bindings=bindings
    )

    def link(email: str, account: str, ticket_id: str) -> None:
        review_mod.add_link(paths, email, account, actor=f"request:{ticket_id}")

    flows = Flows(
        store=TicketStore(args.tickets),
        catalog=lambda: load_catalog(args.templates),
        approval=lambda: approval,
        roster=lambda: people_mod.load(args.people, bindings_path=bindings),
        executor=executor_from_env,
        add_manual_link=link,
        policy_snapshot=lambda: policies_mod.load(policies_path),
        policy_rules=lambda: policies_mod.load_rules(rules_path),
        notify=notify,
    )
    # 每一步、每张单子都隔离：一张单子出错不能挡住后面的到期回收
    for ticket in flows.store.all():
        if ticket.get("status") not in (PENDING, CLAIMABLE):
            continue
        try:
            after = flows.sync(ticket["id"], force=True)
        except Exception as exc:  # noqa: BLE001
            problems += 1
            print(f"{ticket['id']}：同步失败 {_brief(exc)}")
            continue
        if after.get("status") != ticket.get("status"):
            print(f"{ticket['id']}：{ticket['status']} → {after['status']}")
    steps = (
        lambda: flows.recover_stuck(actor="system"),
        flows.resume_approved if approval is not None else list,
        flows.revoke_expired,
        flows.remind_expiring,
    )
    for step in steps:
        try:
            lines = step()
        except Exception as exc:  # noqa: BLE001
            problems += 1
            print(f"定时任务出错：{_brief(exc)}")
            continue
        for line in lines:
            print(line)
            problems += "失败" in line or "中断" in line
    return 1 if problems else 0


def _find_account(data: dict, spec: str) -> dict:
    platform, _, account = spec.partition("/")
    for acc in data.get("accounts", []):
        if acc["platform"] == platform and acc["account"] == account:
            return acc
    raise ClientError(
        f"你在 {spec} 下没有子账号（格式：平台/账号ID，见 delivery request policies）"
    )


def _list_policies(client: PanelClient, args) -> int:
    data = client.request("GET", "/api/policies")
    accounts = data.get("accounts", [])
    if args.account:
        accounts = [_find_account(data, args.account)]
    if not accounts:
        print("你还没有云账号，先申请开账号。")
        return 0
    marks = {"available": " ", "owned": "✓", "pending": "…", "unavailable": "×"}
    words = [w.lower() for w in args.search.split()]
    for acc in accounts:
        scope = f"{acc['platform']}/{acc['account']}"
        head = f"{acc['account_label']}（{scope}，子账号 {acc['cloud_user']}）"
        print(head + (f"  {acc['error']}" if acc.get("error") else ""))
        for p in acc.get("policies", []):
            hay = f"{p['name']} {p['description']} {p['service']}".lower()
            if words and not all(w in hay for w in words):
                continue
            if not args.all and p["state"] in ("owned", "unavailable") and not words:
                continue
            note = f"  {p['state_note']}" if p.get("state_note") else ""
            print(
                f"  {marks.get(p['state'], ' ')} {p['name']:<44} {p['risk']:<6} "
                f"≤{p['max_days']}天 {p['service']}{note}"
            )
    print(
        f"\n✓ 已有  … 申请中  × 不开放。一次最多 {data.get('max_per_request', 10)} 条。",
        file=sys.stderr,
    )
    return 0


def _grant(client: PanelClient, args) -> int:
    data = client.request("GET", "/api/policies")
    acc = _find_account(data, args.account)
    chosen = []
    for name in args.policy:
        matches = [
            p
            for p in acc.get("policies", [])
            if p["name"].lower() == name.lower() and (args.type is None or p["type"] == args.type)
        ]
        if not matches:
            raise ClientError(f"权限列表里没有策略 {name}（见 delivery request policies --search）")
        if len(matches) > 1:
            raise ClientError(f"{name} 同时有系统策略和自定义策略，请加 --type System 或 Custom")
        chosen.append({"type": matches[0]["type"], "name": matches[0]["name"]})
    body = {
        "template_id": "policy",
        "payload": {
            "platform": acc["platform"],
            "account": acc["account"],
            "cloud_user": acc["cloud_user"],
            "days": args.days,
            "policies": chosen,
        },
        "reason": args.reason,
    }
    r = client.request("POST", "/api/requests", body)["request"]
    print(f"已提交 {r['id']}：{r['status_label']}")
    print(f"  {r['summary']}")
    if r.get("approval_url"):
        print(f"  飞书审批：{r['approval_url']}")
    return 0 if r["status"] != "submit_failed" else 1


def _policies_collect(args) -> int:
    from . import policies
    from .cli import _write_private
    from .clouds import aliyun, volcano

    def aliyun_job(prefix: str):
        return lambda: policies.collect_aliyun(aliyun.Credentials.from_env(prefix))

    jobs = [("aliyun", p, aliyun_job(p)) for p in (args.aliyun_profile or ["ALIYUN"])]
    if not args.skip_volcano:
        jobs.append(
            ("volcano", "default", lambda: policies.collect_volcano(volcano.Credentials.from_env()))
        )
    try:
        previous = policies.load(args.out)
    except policies.PolicyError:
        previous = None
    data = policies.build_snapshot(jobs, previous=previous)
    out = _write_private(args.out, data)
    failed = [a for a in data["accounts"] if a.get("error") or a.get("stale")]
    total = sum(len(a.get("policies") or []) for a in data["accounts"])
    print(f"已写入 {out}（权限 600）：{total} 条策略")
    for a in failed:
        if a.get("stale"):
            print(
                f"  ⚠ {a['platform']}/{a['account']}：采集失败，沿用上次的列表（{a['error_note']}）"
            )
        else:
            print(f"  ⚠ {a['platform']}/{a['account']}：{a['error']}")
    return 1 if failed else 0


def _brief(exc: Exception) -> str:
    from .provision import describe_error

    return (describe_error(exc) or type(exc).__name__)[:120]


def _assets(args) -> int:
    if getattr(args, "assets_command", None) == "collect":
        from . import assets
        from .cli import _write_private
        from .clouds import aliyun, volcano

        def aliyun_job(prefix: str):
            return lambda: assets.collect_aliyun(aliyun.Credentials.from_env(prefix))

        jobs = [("aliyun", p, aliyun_job(p)) for p in (args.aliyun_profile or ["ALIYUN"])]
        if not args.skip_volcano:
            jobs.append(
                (
                    "volcano",
                    "default",
                    lambda: assets.collect_volcano(volcano.Credentials.from_env()),
                )
            )
        data = assets.build_snapshot(jobs)
        out = _write_private(args.out, data)
        failed = [a for a in data["accounts"] if a.get("error")]
        total = sum(len(a.get("resources") or []) for a in data["accounts"])
        print(f"已写入 {out}（权限 600）：{total} 个资源")
        for a in failed:
            print(f"  ⚠ {a['platform']}/{a['account']}：{a['error']}")
        return 1 if failed else 0
    client = PanelClient.from_session()
    data = client.request("GET", "/api/assets")
    if not data.get("accounts"):
        print("你还没有云账号，或者资产还没有采集。")
        return 0
    for acc in data["accounts"]:
        print(
            f"{acc['account_label']}  共 {acc['total']} 个资源"
            + (f"（{acc['error']}）" if acc["error"] else "")
        )
        for t in acc["by_type"][:15]:
            print(f"  {t['count']:>5}  {t['type']}")
    return 0


def _widgets(args) -> int:
    from .approval import ApprovalConfig, FeishuApproval
    from .identity.directory import tenant_token

    app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    token = tenant_token(app_id, secret)
    placeholder = ApprovalConfig(
        args.code, dict.fromkeys(("ticket_id", "kind", "summary", "reason"), "-")
    )
    for w in FeishuApproval(placeholder, lambda: token).widgets(args.code):
        print(f"{w['id']:<40} {w['type']:<12} {w['name']}")
    print(
        "\n把申请单号、申请类型、申请内容、申请理由四个控件的 id 填进 identity/approval.json",
        file=sys.stderr,
    )
    return 0
