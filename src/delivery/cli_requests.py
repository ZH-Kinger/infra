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
  delivery requests sweep                    服务端定时任务：同步审批、到期回收权限、过期凭证
  delivery policies collect                  服务端：采集两家云的权限策略目录（只读）
  delivery approval widgets --code <审批定义编号>
                                             管理员配置飞书审批表单时查控件 ID

访问凭证**不在这里领**：审批通过后由服务端直接发放，凭证作为飞书审批的评论下发。
面板和 CLI 都不保存 secret，这是刻意的 —— 页面会被截图转发，审批实例只有申请人和审批人看得到。
"""

from __future__ import annotations

import argparse
import json
import os
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


class _Unauthorized(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib 跟随跳转时会把 Authorization 原样带到新地址（不管主机和协议）：一律不跟。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, "拒绝跟随跳转", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


class PanelClient:
    def __init__(self, server: str, token: str):
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ClientError(f"后端地址不对：{server!r}")
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ClientError("后端地址必须是 https（本机调试除外）：会话令牌不能明文传输")
        self.server = server.rstrip("/")
        self._token = token
        #: 公司 IAM 登录：收到 401 时续期一次再重试（令牌可能刚好在路上过期）
        self._renew = None

    @classmethod
    def from_session(cls, *, refresher=None) -> PanelClient:
        session = load_session()
        if session is None:
            raise ClientError(
                "未登录。运行： delivery login（公司 IAM 登录用 delivery login --iam）"
            )
        if session.kind == "iam":
            session = _fresh_iam_session(session, refresher)
        elif session.expired:
            raise ClientError("会话已过期，请重新 delivery login")
        server = session.server or os.environ.get("DELIVERY_SERVER", "")
        if not server:
            raise ClientError("不知道后端地址：重新 delivery login --server <地址>")
        client = cls(server, session.token)
        if session.kind == "iam":
            client._renew = lambda: _fresh_iam_session(session, refresher, force=True).token
        return client

    def request(self, method: str, path: str, body=None) -> dict:
        try:
            return self._request_once(method, path, body)
        except _Unauthorized:
            if self._renew is None:
                raise ClientError("会话无效或已过期，请重新 delivery login") from None
            self._token = self._renew()
            self._renew = None
            try:
                return self._request_once(method, path, body)
            except _Unauthorized:
                raise ClientError("身份校验没通过，请重新 delivery login --iam") from None

    def _request_once(self, method: str, path: str, body=None) -> dict:
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
            with _OPENER.open(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode() or "{}").get("error")
            except ValueError:
                message = None
            if exc.code == 401:
                raise _Unauthorized() from None
            if 300 <= exc.code < 400:
                raise ClientError(
                    f"后端返回了跳转（HTTP {exc.code}），为保护令牌不跟随。"
                    "检查 DELIVERY_SERVER 是否是面板地址"
                ) from None
            raise ClientError(message or f"请求失败（HTTP {exc.code}）") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ClientError(f"连不上后端 {self.server}：{type(exc).__name__}") from None


def _fresh_iam_session(session, refresher=None, *, force=False):
    """IAM 登录的令牌快过期时用 refresh_token 续期并写回本机会话。

    · 续期在本机文件锁里做：IAM 续期会作废旧 refresh_token，两个 CLI 进程同时续期时后到的会失败。
      拿到锁后先重读会话文件，别的进程已经续过就直接用它的结果。
    · 续期失败但令牌还没真正过期：先用着，真过期时再报错（请求收到 401 还会再续一次）。
    """
    import dataclasses
    import time

    from . import iam_device
    from .session import load_session, save_session, session_lock

    if not force and session.expires_ts - time.time() > iam_device.REFRESH_MARGIN:
        return session
    refresh = refresher or iam_device.refresh
    with session_lock():
        current = load_session()
        if (
            current is not None
            and current.kind == "iam"
            and current.token != session.token
            and current.expires_ts - time.time() > iam_device.REFRESH_MARGIN
        ):
            return current  # 别的进程刚续过
        base = current if current is not None and current.kind == "iam" else session
        try:
            tokens = refresh(base.token_endpoint, base.client_id, base.refresh_token)
        except iam_device.IamLoginError as exc:
            if not force and base.expires_ts > time.time():
                return base
            raise ClientError(str(exc)) from None
        renewed = dataclasses.replace(
            base,
            token=tokens.token,
            refresh_token=tokens.refresh_token,
            expires_ts=tokens.expires_ts,
        )
        save_session(renewed)
        return renewed


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

    reqs = commands.add_parser("requests", help="服务端维护：同步审批、到期回收")
    rsub = reqs.add_subparsers(dest="requests_command", required=True)
    sweep = rsub.add_parser("sweep", help="定时任务：同步待审批、回收到期的权限和凭证")
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

    hyg = commands.add_parser(
        "hygiene", help="体检：人走了号还在、AK 太久没换、AK 没人用（只出清单，不改任何东西）"
    )
    hyg.add_argument("--inventory", default="identity/inventory.json", help="权限快照")
    hyg.add_argument("--people", default="identity/people.json", help="人员名册")
    hyg.add_argument(
        "--check-status",
        action="store_true",
        help="按 union_id 逐个查飞书在职状态，据此列出「人走了号还在」。"
        "不加就不判断离职 —— 查不到却照算，等于把全公司报成离职",
    )
    hyg.add_argument(
        "--services", default="identity/services.json", help="服务号清单（这些不算「无主」）"
    )
    hyg.add_argument(
        "--assets",
        default="identity/assets.json",
        help="资产快照（PAI 数据集在里面，用来看「人的号删了东西还在」）",
    )
    hyg.add_argument("--stale-days", type=int, default=0, help=f"AK 多久算该换（默认 {180}）")
    hyg.add_argument("--unused-days", type=int, default=0, help=f"多久没用算闲置（默认 {90}）")
    # 这两个是「已登记的桶」的两个来源，用来判断哪些桶没登记
    hyg.add_argument("--templates", default="identity/request-templates.json", help="申请模板目录")
    hyg.add_argument("--allowed", default="identity/dataset-buckets.json", help="数据集桶白名单")
    hyg.add_argument(
        "--cpfs-dirs",
        default="identity/cpfs-dirs.json",
        help="CPFS 目录清单。**面板自己列不了 CPFS**（挂在计算节点上，也没有列文件的云 API），"
        "得由挂了盘的机器 ls 出来喂进来",
    )

    assets = commands.add_parser("assets", help="云账号资产：我能看到的云账号里有哪些资源")
    asub_assets = assets.add_subparsers(dest="assets_command")
    collect = asub_assets.add_parser("collect", help="服务端：用资源中心采集资产快照（只读）")
    collect.add_argument("--out", default="identity/assets.json")
    collect.add_argument("--aliyun-profile", action="append", default=[], metavar="PREFIX")
    collect.add_argument("--skip-volcano", action="store_true")
    collect.add_argument("--skip-pai", action="store_true", help="不采 PAI 数据集")
    collect.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="UID",
        help="资源目录成员账号的 UID，可给多个。主账号的采集身份换临时凭证进去（只读角色）",
    )

    tree = commands.add_parser(
        "workspaces", help="每个人那块地方：对象存储目录 + PAI 数据集（默认只出计划）"
    )
    tree.add_argument("--inventory", default="identity/inventory.json")
    tree.add_argument("--people", default="identity/people.json")
    tree.add_argument("--assets", default="identity/assets.json", help="已有的数据集从这里读")
    tree.add_argument("--services", default="identity/services.json")
    tree.add_argument(
        "--slugs", default="identity/departments.json", help="部门名 → 目录英文短名的对照表"
    )
    tree.add_argument(
        "--dept-map",
        default="",
        metavar="文件",
        help="部门映射的缓存文件。存在就读它（不打飞书），不存在就现查并写进去。"
        "拿飞书凭证的机器和拿云写权限的机器不是同一台时，用它把两步分开",
    )
    tree.add_argument(
        "--departments",
        action="store_true",
        help="按**飞书部门**分层（要 DELIVERY_FEISHU_APP_ID/SECRET）。"
        "不加就按云上的用户组分——而那个组现在是个常量，49 个人全在下面",
    )
    tree.add_argument("--storage", choices=("oss", "cpfs"), required=True, help="给哪种存储算")
    tree.add_argument("--bucket", default="wuji-algo-dev-hz", help="oss：桶名")
    tree.add_argument("--region", default="oss-cn-hangzhou", help="oss：桶所在地域（带 oss- 前缀）")
    tree.add_argument("--mount", default="", help="cpfs：挂载点域名，照现网已有数据集的写法")
    tree.add_argument(
        "--fs-id", default="", help="cpfs：文件系统 ID（bmcpfs-…），ImportInfo 里要用"
    )
    tree.add_argument("--pai-region", default="cn-hangzhou", help="PAI 工作空间所在地域")
    tree.add_argument("--workspace", default="", help="建在哪个 PAI 工作空间")
    tree.add_argument(
        "--apply",
        action="store_true",
        help="**真的动手**：建 OSS 占位目录、建 PAI 数据集。不加就只打印计划",
    )
    tree.add_argument(
        "--profile",
        default="ALIYUN",
        metavar="前缀",
        help="用哪套环境变量里的凭证（<前缀>_ACCESS_KEY_ID/SECRET）。"
        "面板上是 DELIVERY_EXEC_ALIYUN_<主账号 UID>",
    )
    tree.add_argument(
        "--only", action="append", default=[], metavar="登录名", help="只处理这几个人"
    )
    tree.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="登录名",
        help="不给这几个人建。**离职但号还没收的人要写在这里** —— "
        "权限快照只知道「号还在」，不知道「人还在不在」",
    )

    cd = commands.add_parser(
        "dataset", help="登记一条自定义数据集（共享数据、项目数据这类，名字和路径自己定）"
    )
    cd.add_argument("--name", required=True, help="数据集名（字母数字开头，不含中文）")
    cd.add_argument("--bucket", required=True, help="桶名，或 CPFS 的 fs-id。**必须在白名单里**")
    cd.add_argument("--prefix", required=True, help="路径，不能是桶根目录")
    cd.add_argument("--storage", choices=("oss", "cpfs"), default="oss")
    cd.add_argument("--region", default="oss-cn-hangzhou", help="oss：桶地域（带 oss- 前缀）")
    cd.add_argument("--mount", default="", help="cpfs：挂载点域名")
    cd.add_argument("--fs-id", default="", help="cpfs：文件系统 ID")
    cd.add_argument("--pai-region", default="cn-hangzhou")
    cd.add_argument("--workspace", required=True, help="建在哪个 PAI 工作空间")
    cd.add_argument(
        "--owner",
        default="",
        help="属主的 RAM 登录名。个人目录**必填** —— PAI 的属主事后改不回来"
        "（UpdateDataset 改 UserId 静默无效），留空会建成面板自己的。公共目录用 --shared",
    )
    cd.add_argument(
        "--shared",
        action="store_true",
        help="公共目录：属主记主账号、不归任何人，可见性 PUBLIC —— 和现网那批"
        "（ANT / share / lakefs-server / _lakefs_cache）一个规格。"
        "**注意 PUBLIC 意味着谁都能删这条登记**（组里那条 pai 策略对 PUBLIC 无条件放行）",
    )
    cd.add_argument("--description", default="", help="这份数据是什么，给后来的人看")
    cd.add_argument("--allowed", default="identity/dataset-buckets.json", help="允许登记的桶白名单")
    cd.add_argument("--profile", default="ALIYUN", metavar="前缀", help="用哪套凭证")
    cd.add_argument("--apply", action="store_true", help="**真的建**。不加就只检查参数")

    ap = commands.add_parser("approval", help="飞书审批配置辅助")
    asub = ap.add_subparsers(dest="approval_command", required=True)
    widgets = asub.add_parser("widgets", help="查审批定义的表单控件 ID")
    widgets.add_argument("--code", required=True, help="审批定义编号 approval_code")
    widgets.add_argument(
        "--write",
        metavar="文件",
        default="",
        help="把控件对照表写进这个 approval.json。改过表单必须重跑，否则面板拿旧 id 取不到单号",
    )


def dispatch(args: argparse.Namespace):
    """返回退出码；不是本模块的命令返回 None。"""
    if args.command == "request":
        return _request(args)
    if args.command == "requests":
        return _sweep(args)
    if args.command == "approval":
        return _widgets(args)
    if args.command == "hygiene":
        return _hygiene(args)
    if args.command == "workspaces":
        return _workspaces(args)
    if args.command == "dataset":
        return _custom_dataset(args)
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
        if r.get("expires_at"):
            print(f"  权限到期：{r['expires_at']}")
        for e in r.get("events", []):
            note = f"  {e['note']}" if e.get("note") and e["note"] != e["label"] else ""
            print(f"  · {e['at']}  {e['label']}（{e['actor']}）{note}")
        if r.get("kind") == "credential" and r.get("status") == "done":
            print("\n  凭证已发到对应飞书审批的评论里，面板和 CLI 都不保存。")
        return 0
    if cmd == "withdraw":
        r = client.request("POST", path + "/withdraw")["request"]
        print(f"{r['id']}：{r['status_label']}")
        return 0
    raise DeliveryError(f"未知子命令 {cmd}")


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
    from .provision import executor_configured, executor_from_env, issuer_configured
    from .tickets import PENDING, TicketStore

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
        # 发放身份必须显式接上。不接的话 _sign_credential 会回落成开通身份 ——
        # 而开通身份在云上没有 CreatePolicy/CreateAccessKey/DeleteUser：
        # 子账号建得出来、策略建不上，清理和到期回收也全失败，留一地孤儿 AK。
        # sweep 是无人值守的那条路，这里回落没人会看见。
        issuer=lambda platform, account: executor_from_env(platform, account, issuer=True),
        add_manual_link=link,
        policy_snapshot=lambda: policies_mod.load(policies_path),
        policy_rules=lambda: policies_mod.load_rules(rules_path),
        notify=notify,
        executor_ready=executor_configured,
        issuer_ready=issuer_configured,
    )
    # 每一步、每张单子都隔离：一张单子出错不能挡住后面的到期回收
    for ticket in flows.store.all():
        if ticket.get("status") != PENDING:
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


def _hygiene(args) -> int:
    """打印体检清单。**只读**：不停用、不删除、不改任何东西。

    退出码：有待办 → 1，干净 → 0。定时任务据此决定要不要推通知。
    数据不完整（快照没采全、通讯录没拿到）同样返回 1 —— 那本身就是要人看一眼的事。
    """
    from . import assets as assets_mod
    from . import hygiene, inventory
    from . import people as people_mod

    snap = None
    try:
        snap = inventory.load(args.inventory)
    except DeliveryError as exc:
        print(f"权限快照读不了：{exc}", file=sys.stderr)
    roster = ()
    try:
        roster = people_mod.load(args.people).people
    except DeliveryError as exc:
        print(f"人员名册读不了：{exc}", file=sys.stderr)

    statuses = None
    if args.check_status:
        from .identity import directory

        app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
        secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
        try:
            # **按 union_id 逐个查，不拉全量部门**：拉全量要通讯录部门权限
            # （应用的可用范围通常不覆盖全员，会 40004），而我们只关心名册里
            # 这几十个有云账号的人
            statuses = directory.status_of(
                [p.union_id for p in roster if p.union_id], app_id, secret
            )
        except DeliveryError as exc:
            # 查不了就不判离职，别硬算 —— 见 hygiene.build 的说明
            print(f"查不了在职状态，本次不判断离职：{exc}", file=sys.stderr)

    from .cli import _load_service_names

    # 资产快照缺 / 坏 都不该让体检整个跑不起来 —— 那一类记一笔跳过就行。
    # **拿不到时传 None 而不是空列表**：空列表等于断言「一条被遗弃的都没有」
    datasets = None
    cloud_buckets = None
    try:
        snapshot_assets = assets_mod.load(args.assets)
        if snapshot_assets is not None:
            datasets = snapshot_assets.get("datasets")
            cloud_buckets = snapshot_assets.get("buckets")
    except DeliveryError as exc:
        print(f"资产快照读不了，本次不看数据集：{exc}", file=sys.stderr)

    _reg = _registered_buckets(args)
    _dirs = hygiene.load_cpfs_dirs(args.cpfs_dirs)
    report = hygiene.build(
        snap,
        roster,
        statuses=statuses,
        datasets=datasets,
        buckets=cloud_buckets,
        cpfs_dirs=_dirs[0],
        dirs_captured_at=_dirs[1],
        registered=_reg[0],
        registered_notes=_reg[1],
        services=_load_service_names(args.services),
        stale_days=args.stale_days or hygiene.STALE_KEY_DAYS,
        unused_days=args.unused_days or hygiene.UNUSED_KEY_DAYS,
    )
    print(report.render())
    return 1 if (report.total or report.skipped) else 0


def _registered_buckets(args):
    """已登记的桶：凭证模板 + 数据集白名单。实现在 `hygiene.load_registered_buckets` ——
    面板服务端读的是同一份，各写一份的那一版漏防了白名单那一路。"""
    from . import hygiene

    return hygiene.load_registered_buckets(args.templates, getattr(args, "allowed", None))


def _refuse_issuer(profile: str) -> None:
    """不许用**凭证发放身份**跑这些命令。

    `--profile` 是个自由前缀，`DELIVERY_ISSUER_ALIYUN_<UID>` 会被照单全收 ——
    而那把 AK 能建 RAM 子账号、能发 AccessKey、能造策略。建目录和建数据集
    只需要 PutObject / CreateDataset / ListUsers，**没有任何理由拿那把**。
    开通身份和发放身份在云上是分开收窄的，共用一把等于把那道闸拆了。
    """
    if "ISSUER" in str(profile or "").upper():
        raise DeliveryError(
            f"--profile {profile} 指向凭证发放身份，拒绝使用。"
            "那把 AK 能建子账号和发 AccessKey，而这个命令只需要建目录和建数据集。"
            "用开通身份（DELIVERY_EXEC_…）"
        )


def _custom_dataset(args) -> int:
    """登记一条自定义数据集。默认只校验，`--apply` 才真建。"""
    from . import assets as assets_mod
    from . import custom_dataset, provision_tree
    from .clouds import aliyun

    _refuse_issuer(args.profile)
    source = "OSS" if args.storage == "oss" else "BMCPFS"
    if source == "OSS" and not args.region.startswith("oss-"):
        # 这条命令一次 OSS 调用都不打，所以填错地域**不会有任何东西报错** ——
        # PAI 收下、返回 DatasetId、打印「建好了」，等有人挂载才发现，
        # 而 UpdateDataset 改 Uri 同样静默无效，只能去控制台删了重建
        print(
            f"--region 要带 oss- 前缀（如 oss-cn-hangzhou），收到 {args.region!r}", file=sys.stderr
        )
        return 2
    try:
        spec = custom_dataset.validate(
            name=args.name,
            bucket=args.bucket,
            prefix=args.prefix,
            source=source,
            workspace=args.workspace,
            region=args.pai_region,
            allowed=custom_dataset.load_allowed(args.allowed),
            fs_id=args.fs_id,
            mount=args.mount,
            oss_region=args.region,
            owner_login=args.owner,
            description=args.description,
        )
    except DeliveryError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if source == "OSS":
        # **只用 spec.***，两条分支同一套规矩
        uri = provision_tree.oss_uri(spec.bucket, spec.oss_region, spec.prefix)
        info = provision_tree.oss_import(spec.bucket, spec.oss_region, spec.prefix)
    else:
        # **只用 spec.***：`args.fs_id`/`args.mount` 是没过白名单的原始输入
        uri = provision_tree.cpfs_uri(spec.mount, spec.prefix)
        info = provision_tree.cpfs_import(spec.fs_id, spec.region, spec.mount, spec.prefix)

    if not args.shared and not spec.owner_login:
        print("个人目录必须给 --owner；公共目录用 --shared", file=sys.stderr)
        return 2
    if args.shared and spec.owner_login:
        print("--shared 和 --owner 只能给一个：公共目录不归任何人", file=sys.stderr)
        return 2

    print(f"数据集 {spec.name}")
    print(f"  路径   {uri}")
    print(f"  属主   {spec.owner_login or '主账号（公共目录，PUBLIC —— 谁都能删这条登记）'}")
    print(f"  空间   {spec.region}/{spec.workspace}")
    if not args.apply:
        print("\n参数没问题。真要建加 --apply。")
        return 0

    creds = aliyun.Credentials.from_env(args.profile)
    user_id = ""
    accessibility = assets_mod.DATASET_ACCESS
    if args.shared:
        # 公共目录归**主账号**，不归任何 RAM 用户 —— 现网那批公共数据集就是这个形状
        # （owner 在台账里显示为空，因为那个 UserId 在 RAM 用户表里查不到）。
        # 可见性跟着用 PUBLIC，否则控制台里新旧两批会长成两种东西
        user_id = str(
            aliyun.call(*aliyun.STS, "GetCallerIdentity", creds=creds).get("AccountId") or ""
        )
        accessibility = "PUBLIC"
        if not user_id:
            print("取不到主账号 ID，不建", file=sys.stderr)
            return 2
    elif spec.owner_login:
        found = {
            str(u.get("UserName") or ""): str(u.get("UserId") or "")
            for u in aliyun.paginate(
                *aliyun.RAM, "ListUsers", key="User", container="Users", creds=creds
            )
        }
        user_id = found.get(spec.owner_login, "")
        if not user_id:
            print(f"RAM 里没有 {spec.owner_login} 这个用户", file=sys.stderr)
            return 2
    try:
        did = assets_mod.create_dataset(
            creds,
            region=spec.region,
            workspace=spec.workspace,
            name=spec.name,
            uri=uri,
            source=spec.source,
            import_info=info,
            accessibility=accessibility,
            user_id=user_id,
            labels=[{"Key": "kind", "Value": "shared" if args.shared else "custom"}],
        )
    except DeliveryError as exc:
        print(f"建不了：{str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    print(f"\n建好了：{did}。别忘了重跑 delivery assets collect 刷新台账。")
    return 0


def _workspaces(args) -> int:
    """算出（可选地建出）每个人那块地方。默认 dry-run。"""
    from . import assets as assets_mod
    from . import inventory, provision_tree, workspace_tree
    from . import people as people_mod
    from .cli import _load_service_names
    from .clouds import aliyun, oss  # noqa: F401

    _refuse_issuer(args.profile)
    snap = inventory.load(args.inventory)
    users = [u for u in snap.users if u.platform == "aliyun"]
    roster = people_mod.load(args.people).people
    known = {r.name: p.name for p in roster for r in p.accounts if r.platform == "aliyun"}

    if args.storage == "oss":
        layout = workspace_tree.LAYOUT_GROUPED
        source = "OSS"
        uri_of = lambda pre: provision_tree.oss_uri(args.bucket, args.region, pre)  # noqa: E731
        import_of = lambda pre: provision_tree.oss_import(args.bucket, args.region, pre)  # noqa: E731
    else:
        if not args.mount:
            print("cpfs 要给 --mount（挂载点域名），照现网已有数据集的写法", file=sys.stderr)
            return 2
        if not args.fs_id:
            print("cpfs 还要给 --fs-id（形如 bmcpfs-…），ImportInfo 里要用", file=sys.stderr)
            return 2
        layout = workspace_tree.LAYOUT_FLAT
        source = "BMCPFS"
        uri_of = lambda pre: provision_tree.cpfs_uri(args.mount, pre)  # noqa: E731
        # ImportInfo 是 PAI 真正拿去挂载的东西。形状抄现网那 30 条，不抄文档示例
        import_of = lambda pre: provision_tree.cpfs_import(  # noqa: E731
            args.fs_id, args.pai_region, args.mount, pre
        )

    # 名册里没有公司邮箱的人 —— 对不上飞书，和「查不到部门」不是一回事
    unbound = {r.name for p in roster for r in p.accounts if r.platform == "aliyun" and not p.email}
    dept = None
    if args.departments and args.dept_map and Path(args.dept_map).exists():
        # 缓存命中：不打飞书。**部门变动不会自动反映**，要刷新就删掉这个文件
        cached = json.loads(Path(args.dept_map).read_text(encoding="utf-8"))
        dept = {k: tuple(v) for k, v in (cached.get("departments") or {}).items()}
        print(f"部门映射读自 {args.dept_map}：{len(dept)} 人\n")
    elif args.departments:
        from .identity import directory

        index = directory.staff_index(
            os.environ.get("DELIVERY_FEISHU_APP_ID", ""),
            os.environ.get("DELIVERY_FEISHU_APP_SECRET", ""),
        )
        # **按公司邮箱对，不按 union_id** —— 新人没登录过面板就没有 union_id，
        # 而建目录这件事不该等他登录
        dept, total = {}, 0
        for person in roster:
            for ref in person.accounts:
                if ref.platform != "aliyun":
                    continue
                total += 1
                hit = index.get((person.email or "").strip().lower())
                if hit:
                    dept[ref.name] = (hit["department_id"], hit["department"])
        print(f"飞书部门：{len(dept)} / {total} 人对上了（按公司邮箱）\n")
        if args.dept_map:
            from .cli import _write_private

            _write_private(args.dept_map, {"departments": {k: list(v) for k, v in dept.items()}})
            print(f"已写入 {args.dept_map}（权限 600）")

    targets, skipped = provision_tree.plan(
        users,
        people=known,
        services=_load_service_names(args.services),
        layout=layout,
        department_of=dept,
        slugs=workspace_tree.load_slugs(args.slugs),
        unbound=unbound,
        uri_of=uri_of,
        source=source,
        region=args.pai_region,
        workspace=args.workspace,
        # 数据集名带存储后缀：一个工作空间里名字必须唯一，而同一个人两种存储都想叫登录名
        suffix=args.storage,
        # 属主的 RAM 数字 UserId。名册里没有这个，只能现查
        user_ids={
            str(u.get("UserName") or ""): str(u.get("UserId") or "")
            for u in aliyun.paginate(
                *aliyun.RAM,
                "ListUsers",
                key="User",
                container="Users",
                creds=aliyun.Credentials.from_env(args.profile),
            )
        },
        import_of=import_of,
    )
    # **换组/无主的判断要用过滤前的完整名单。** 用过滤后的算，`--only wangzihan`
    # 跑一次就会把另外 48 个人的目录全印成「谁都不该有的目录」—— 而这个仓库的规矩是
    # 「报错话比不报更糟，运维会学会忽略结论」
    all_targets = list(targets)
    if args.only:
        want = set(args.only)
        targets = [t for t in targets if t.login in want]
    if args.exclude:
        drop = set(args.exclude)
        skipped += [f"{t.login}：命令行里显式排除了" for t in targets if t.login in drop]
        targets = [t for t in targets if t.login not in drop]

    probe_creds = aliyun.Credentials.from_env(args.profile)
    ram_names = {
        str(u.get("UserId") or ""): str(u.get("UserName") or "")
        for u in aliyun.paginate(
            *aliyun.RAM, "ListUsers", key="User", container="Users", creds=probe_creds
        )
    }
    # **从 PAI 实时查**，不读快照：快照可能是几天前的，而「这个人有没有」判错的代价是
    # 给已经有数据的人再建一个空目录。要 --workspace 才查得了，没给就退回快照
    existing = None
    if args.workspace:
        try:
            existing = [
                {
                    "workspace": args.workspace,
                    "name": str(d.get("Name") or ""),
                    "source": str(d.get("DataSourceType") or ""),
                    "uri": str(d.get("Uri") or ""),
                    "owner_login": ram_names.get(str(d.get("UserId") or ""), ""),
                }
                for d in assets_mod._pai_datasets(probe_creds, args.pai_region, args.workspace)
            ]
            print(f"工作空间 {args.workspace} 现有数据集 {len(existing)} 条（实时查的）")
        except DeliveryError as exc:
            print(f"⚠ 查不到现有数据集，退回资产快照：{exc}")
    if existing is None:
        existing = (assets_mod.load(args.assets) or {}).get("datasets")
    if existing is None:
        # **不知道现状 ≠ 现状是空。** 按「全都没有」建的话，存量那些名字对不上的人
        # （`wzh` vs `wangzihan-oss`）会被再建一条指向标准路径的空数据集，
        # 而他们的数据在老路径下面。dry-run 可以继续（只是打印），写不行
        if args.apply:
            print(
                "查不到现有数据集，**拒绝 --apply** —— 不知道谁已经有了就建，"
                "会给存量的人再建一份空的。先确认 --workspace 和 PAI 权限",
                file=sys.stderr,
            )
            return 2
        print("⚠ 查不到现有数据集，下面按「全都没有」算，仅供参考")
        existing = []
    # **空 user_id 先踢，而且放在 try 之外。** 它只依赖 t.user_id、不需要任何云调用；
    # 埋在下面那个 try 里的话，ListMembers 一失败（掉权限/5xx）整段过滤就不执行，
    # 这些 target 会一路走到 create_dataset —— 虽然那边有必填门接住（纵深有效），
    # 但表现会从「跳过并说明原因」退化成混在失败计数里的 ✗
    nameless = [t for t in targets if not t.user_id]
    if nameless:
        skipped += [
            f"{t.login}：RAM 里查不到这个登录名，拿不到属主 UserId —— 不建，"
            "否则属主会记成面板自己而且改不回来"
            for t in nameless
        ]
        targets = [t for t in targets if t.user_id]

    # **不是工作空间成员的，连试都不试。** PAI 会用 400 拒掉（`UserId is not a member`），
    # 但那在输出里长得像故障，而它其实是个正常状态：人还没被管理员加进工作空间。
    # 加人是管理员在面板或阿里云控制台上做的决定，不该由这个命令代劳 ——
    # 加进去之后下次跑自动就建了
    if args.workspace:
        try:
            member_ids = set(assets_mod._pai_members(probe_creds, args.pai_region, args.workspace))
            outside = [t for t in targets if t.user_id and t.user_id not in member_ids]
            if outside:
                skipped += [
                    f"{t.login}：还不是工作空间 {args.workspace} 的成员 —— "
                    "等管理员把他加进去，下次跑自动建"
                    for t in outside
                ]
                targets = [t for t in targets if not (t.user_id and t.user_id not in member_ids)]
        except DeliveryError as exc:
            print(f"⚠ 查不到工作空间成员，不预先过滤：{exc}")

    # 传上这次的目标存储，判重才分得清「他在别的桶里那条」和「他在这个桶里那份」
    # **传命令级常量，别从 targets[0] 反推** —— target 列表被过滤空了的话，
    # 反推会静默退回「位置盲判重」，而那正是 B-2 那个 bug
    todo, managed = provision_tree.to_create(
        targets, existing, location=(args.bucket if args.storage == "oss" else args.fs_id)
    )
    clash = provision_tree.collisions(targets, existing)

    # **换部门会不会被发现，全看这一段。** 数据集名里没有部门（`chuzhong-oss`），
    # 所以人调组之后「已存在」判定照样命中 —— 不看云上的实际目录的话，
    # 结果是人在新组、数据在旧组，而且一句话都不报
    moved = strayed = []
    if args.storage == "oss" and layout == workspace_tree.LAYOUT_GROUPED:
        try:
            here = []
            for top in oss.list_prefixes(args.bucket, region=args.region, creds=probe_creds):
                here += oss.list_prefixes(args.bucket, top, region=args.region, creds=probe_creds)
            slots = [
                workspace_tree.Slot(login=t.login, group=t.prefix.split("/")[0], person=t.person)
                for t in all_targets
            ]
            moved = workspace_tree.moves(slots, here)
            strayed = workspace_tree.strays(slots, here)
        except DeliveryError as exc:
            print(f"⚠ 列不了桶里现有的目录，本次看不出谁换了组：{exc}")

    print(
        f"{args.storage}：名册里该有 {len(targets)} 个人，"
        f"{len(managed)} 个已纳管、{len(todo)} 个要新建"
    )
    if managed:
        # 存量是手工建的、路径和名字都不统一。**不动、不改名、不搬** —— 只是认出来
        # **把 URI 一并打出来**：同桶里有两条的情况判重看不出来，得靠人扫一眼
        # **按人收成列表，不是字典** —— 字典推导会让一人多条时只剩最后一条，
        # 而这行的目的恰恰是「同桶里有两条时人能扫出来」
        by_login: dict = {}
        for d in existing:
            if isinstance(d, dict) and d.get("owner_login"):
                by_login.setdefault(str(d["owner_login"]), []).append(str(d.get("uri") or "?"))
        print(f"  已纳管 {len(managed)} 条（保持原样，不动）：")
        for t in sorted(managed, key=lambda x: x.login):
            for uri in by_login.get(t.login) or ["(URI 未知)"]:
                print(f"    {t.login:<18}{uri}")
    for t in todo[:80]:
        print(f"  + {t.login:<18}{t.person:<8}{t.uri}")
    if len(todo) > 80:
        print(f"  …… 还有 {len(todo) - 80} 个")
    if clash:
        print(f"\n**名字撞了但路径不一样的 {len(clash)} 条 —— 不自动动，要人看**：")
        for t, was in clash:
            print(f"  ! {t.name:<18}已有 {was}")
            print(f"  {'':20}该是 {t.uri}")
    if moved:
        print(f"\n**换了部门的 {len(moved)} 个 —— 目录还在旧位置，面板不会自动搬**：")
        for m in moved:
            print(f"  ! {m.login:<18}{m.person:<8}{m.old_prefix}  →  {m.new_prefix}")
        print("   搬是 copy + delete（OSS 没有原子 rename），几 TB 要跑几小时、期间在跑的")
        print("   任务读不到，所有写死路径的脚本也要改。**要人决定什么时候搬。**")
    if strayed:
        print(f"\n桶里有、但现在谁都不该有的目录 {len(strayed)} 个（只报不删）：")
        for pre in strayed:
            print(f"  ? {pre}")
    if skipped:
        print(f"\n跳过 {len(skipped)} 个：")
        for note in skipped[:20]:
            print(f"  · {note}")
    # 权限快照回答的是「这个号还在不在」，不是「这个人还在不在」。离职但号还没收的人
    # 照样出现在上面那份清单里 —— 给他建一块地方，就是在制造下一条「无主资产」
    print(
        "\n⚠ 这份清单来自权限快照，它只知道「号还在」。**先跑一次**\n"
        "    delivery hygiene --check-status\n"
        "  确认里面没有已离职的人；有的话用 --exclude 排掉再建。"
    )
    if not args.apply:
        print("\n这是计划，什么都没动。确认无误后加 --apply。")
        return 0
    if not args.workspace:
        print("要 --apply 就得给 --workspace（建在哪个 PAI 工作空间）", file=sys.stderr)
        return 2

    # 用**开通身份**（面板上是 panel-executor），不是采集身份：建目录和建数据集都是写操作。
    # 它的权限只够「在指定桶里建目录 + 建 PAI 数据集」，一条删除动作都没有
    creds = aliyun.Credentials.from_env(args.profile)
    made, failed = 0, 0
    for t in todo:
        try:
            if args.storage == "oss":
                oss.put_folder(args.bucket, t.prefix, region=args.region, creds=creds)
            did = assets_mod.create_dataset(
                creds,
                region=t.region,
                workspace=t.workspace,
                name=t.name,
                uri=t.uri,
                source=t.source,
                import_info=t.import_info or None,
                user_id=t.user_id,
                labels=[
                    {"Key": "kind", "Value": "personal"},
                    {"Key": "owner", "Value": t.login},
                    # 扁平布局（CPFS）下路径里没有组，第一段就是登录名 ——
                    # 直接 split 会把 group 标签写成 owner。宁可不打这个标签
                    *(
                        [{"Key": "group", "Value": t.prefix.split("/")[0]}]
                        if layout == workspace_tree.LAYOUT_GROUPED
                        else []
                    ),
                ],
            )
            made += 1
            print(f"  ✓ {t.login:<18}{did}")
        except DeliveryError as exc:
            failed += 1
            print(f"  ✗ {t.login:<18}{str(exc).splitlines()[0][:120]}")
    print(f"\n建好 {made} 个，失败 {failed} 个。别忘了重跑 delivery assets collect 刷新台账。")
    return 1 if failed else 0


def _assets(args) -> int:
    if getattr(args, "assets_command", None) == "collect":
        from . import assets
        from .cli import _write_private
        from .clouds import aliyun, volcano

        def aliyun_job(prefix: str):
            return lambda: assets.collect_aliyun(aliyun.Credentials.from_env(prefix))

        def member_job(uid: str):
            # 主账号的采集身份（第一个 profile）换临时凭证进成员账号
            base = args.aliyun_profile[0] if args.aliyun_profile else "ALIYUN"
            return lambda: assets.collect_member(aliyun.Credentials.from_env(base), uid)

        jobs = [("aliyun", p, aliyun_job(p)) for p in (args.aliyun_profile or ["ALIYUN"])]
        # 成员账号：资源目录纳管的那几个。采不到会被 build_snapshot 记成这个账号的 error，
        # 不会静默少一个账号 —— 那样资产页会显示成「这个账号什么都没有」
        jobs += [("aliyun", uid, member_job(uid)) for uid in (args.member or [])]
        if not args.skip_volcano:
            jobs.append(
                (
                    "volcano",
                    "default",
                    lambda: assets.collect_volcano(volcano.Credentials.from_env()),
                )
            )
        # PAI 数据集：和资源中心是两条路（那边不给归属，这边 UserId 就是属主）。
        # 采不到只记一笔，不让整次资产采集失败
        sets, skipped, ds_err, fresh_bin, bin_err = None, [], "", None, ""
        buckets, bkt_err = None, ""
        # 上一份快照里攒下来的回收站记录：云上那份有保留期，这份没有。
        # **读不了就当没有**，不能让它挡住这次采集
        kept_bin = None
        try:
            previous = assets.load(args.out)
            kept_bin = (previous or {}).get("recycle_bin")
        except DeliveryError as exc:
            print(f"  ⚠ 上一份快照读不了，回收站记录这次从零开始：{exc}")

        if not args.skip_pai:
            base = args.aliyun_profile[0] if args.aliyun_profile else "ALIYUN"
            creds = aliyun.Credentials.from_env(base)
            # **回收站单独一个 try**：它只是用来认人的补充数据，而 PAI 数据集才是主数据。
            # 合在一起的话，采集身份少一个 ram:ListUsersInRecycleBin，
            # 整份数据集登记就会从快照里消失 —— 而 PAI 那边其实一点问题都没有
            try:
                fresh_bin = assets.collect_recycle_bin(creds)
            except DeliveryError as exc:
                bin_err = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
            recycled = assets.merge_recycle_bin(kept_bin, fresh_bin)
            try:
                sets, skipped = assets.collect_pai_datasets(creds, recycled=recycled)
            except DeliveryError as exc:
                ds_err = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
        else:
            recycled = assets.merge_recycle_bin(kept_bin, None)

        # 列桶和 PAI 没有关系，所以**不在 --skip-pai 里面**：放进去的话，
        # 有人为了绕开 PAI 权限加个 --skip-pai，会连体检里「没登记的桶」一起悄悄关掉。
        # 单独一个 try 的理由同上 —— 少一个 oss:ListBuckets 不该把别的采集带走
        try:
            base = args.aliyun_profile[0] if args.aliyun_profile else "ALIYUN"
            buckets = assets.collect_buckets(aliyun.Credentials.from_env(base))
        except DeliveryError as exc:
            bkt_err = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")

        data = assets.build_snapshot(
            jobs,
            datasets=sets,
            dataset_error=ds_err,
            recycled=recycled,
            buckets=buckets,
            bucket_error=bkt_err,
        )
        out = _write_private(args.out, data)
        failed = [a for a in data["accounts"] if a.get("error")]
        total = sum(len(a.get("resources") or []) for a in data["accounts"])
        print(f"已写入 {out}（权限 600）：{total} 个资源")
        if bin_err:
            print(f"  ⚠ 回收站没采到：{bin_err}（旧记录照旧保留，数据集照常采）")
        if recycled is not None:
            added = len(recycled) - len(kept_bin or ())
            note = f"，本次新增 {added}" if added > 0 else ""
            print(f"  RAM 回收站累计 {len(recycled)} 个已删账号{note}")
        if bkt_err:
            print(f"  ⚠ OSS 桶清单没采到：{bkt_err}（体检里「没登记的桶」这一类会标成跳过）")
        if buckets is not None:
            print(f"  OSS 桶 {len(buckets)} 个")
        if sets is not None:
            gone = [d for d in sets if d["owner_kind"] == assets.OWNER_GONE]
            tail = f"，其中 {len(gone)} 条属主已经不在了" if gone else ""
            print(f"  PAI 数据集 {len(sets)} 条{tail}")
            for note in skipped:
                print(f"    · 跳过 {note}")
        elif ds_err:
            print(f"  ⚠ PAI 数据集没采到：{ds_err}")
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
    from .approval import EXTRA_KEYS, WIDGET_KEYS, ApprovalConfig, FeishuApproval
    from .identity.directory import tenant_token

    app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    token = tenant_token(app_id, secret)
    placeholder = ApprovalConfig(
        args.code, dict.fromkeys(("ticket_id", "kind", "summary", "reason"), "-")
    )
    found = FeishuApproval(placeholder, lambda: token).widgets(args.code)
    for w in found:
        # custom_id 才是稳定别名：飞书自己生成 id，后台改一次表单 id 就会漂移。
        # 只打 id 的话，运维照着这条命令做，配置里那些按 custom_id 认的字段一个也配不上
        print(
            f"{w['custom_id'] or '(没有 custom_id)':<14} {w['type']:<12} {w['id']:<28} {w['name']}"
        )
    mapping = {
        w["custom_id"]: {"id": w["id"], "type": w["type"]}
        for w in found
        if w.get("custom_id") and w.get("id")
    }
    missing = [k for k in WIDGET_KEYS if k not in mapping]
    # 可选字段缺了是**静默**的（approval.create 会跳过），所以必须在这里说出来 ——
    # 否则「审批定义还没加这一栏」和「custom_id 拼错了」长得一模一样，
    # 而两者的表现都是「审批单上那一栏空着」
    absent = [k for k in EXTRA_KEYS if k not in mapping]
    if not args.write:
        print(
            f"\n{len(mapping)} 个控件有 custom_id。加 --write <approval.json> 直接写进配置",
            file=sys.stderr,
        )
        if missing:
            print(f"缺这几个必填控件：{'、'.join(missing)}", file=sys.stderr)
        if absent:
            print(
                f"定义里没有这些可选字段，它们会被静默丢弃：{'、'.join(absent)}",
                file=sys.stderr,
            )
        return 0
    if missing:
        # 写一份缺必填控件的配置出去，面板下次加载就会拒——不如现在就停
        print(f"缺必填控件 {'、'.join(missing)}，不写", file=sys.stderr)
        return 2
    path = Path(args.write)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        print(f"读不了 {path}：{exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print(f"{path} 不是一个 JSON 对象", file=sys.stderr)
        return 2
    data["approval_code"] = args.code
    data["widgets"] = mapping
    # 0600：这份配置本身不含密钥，但它和 identity/ 下别的文件一样按私有处理
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    print(f"\n已写入 {path}：{len(mapping)} 个控件", file=sys.stderr)
    if absent:
        print(
            f"注意：定义里没有这些可选字段，审批单上那几栏会是空的：{'、'.join(absent)}",
            file=sys.stderr,
        )
    return 0
