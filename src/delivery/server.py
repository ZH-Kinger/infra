"""本地开发服务器：看板 + 飞书 OAuth 回调 + 给 CLI 用的换取端点。

**这是开发用的服务器，不是生产件。** 会话存在进程内存里（重启即失效）、没有 CSRF
以外的防护、只绑回环地址。生产部署要换成正经的 WSGI 应用 + 持久化会话。

零第三方依赖：仓库硬规 `dependencies = []`，所以用标准库的 http.server，
不用 Flask/FastAPI。

路由
────
  GET  /  /app.js /app.css   前端（src/delivery/web/，零构建）。
  GET  /api/session          当前登录者与角色。
  GET  /api/me               自己的云账号与权限（按 union_id 认人）。
  GET  /api/admin/overview   管理员：各云账号统计、告警。
  GET  /api/admin/people     管理员：人员列表，?filter=all|multi|high_risk|unbound|no_account
  GET  /api/admin/people/<key>  管理员：某个人的权限详情。
  GET  /api/admin/review     管理员：名册审核的人工记录。
  /api/requests/*            员工：云账号申请（开账号、权限、访问凭证），见 requests_api.py。
  GET  /api/policies         员工：权限列表（全部权限策略对本人的状态），见 policies.py。
  /api/admin/requests/*      管理员：全部申请、重试开通、关闭。
  POST /api/admin/review     管理员：确认 / 驳回 / 分配 / 标记服务号 / 撤销（见 review.py）。
  GET  /auth/login       跳飞书授权页（带 PKCE 与 state）。
                         代理登录模式下 /auth/* 全部 404，登录退出走 oauth2-proxy 的 /oauth2/*。
  GET  /auth/callback    接飞书回调，换 token，建会话。
  POST /auth/exchange    **给 CLI 用**：CLI 自己拿到 code，交到这里换会话令牌。
                         app_secret 只在这一侧，CLI 不持有。
"""

from __future__ import annotations

import html
import http.cookies
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import assets as assets_mod
from . import health as health_mod
from . import inventory
from . import notify as notify_mod
from . import people as people_mod
from . import policies as policies_mod
from . import review as review_mod
from . import tickets as tickets_mod
from .approval import ApprovalConfig, FeishuApproval
from .catalog import load as load_catalog
from .errors import DeliveryError
from .feishu import FeishuError, FeishuUser, exchange_code, fetch_user
from .flows import Flows
from .login import _pkce_pair, authorize_url
from .people import BIND_NONE, BIND_UNION_ID
from .provision import executor_from_env
from .proxy_auth import (
    AUTH_FEISHU,
    AUTH_MODES,
    AUTH_PROXY,
    ENV_AUTH,
    ProxyAuthConfig,
    ProxyIdentity,
)
from .registry import PlatformRegistry
from .requests_api import Caller, RequestsApi
from .roles import ROLE_ADMIN, Admins, load_admins
from .views import FILTERS, Labels, admin_overview, admin_people, person_detail

COOKIE_NAME = "delivery_session"
_STATE_TTL = 600
_SESSION_TTL = 8 * 3600


@dataclass
class _Pending:
    verifier: str
    redirect_uri: str
    created: float = field(default_factory=time.time)


@dataclass
class _WebSession:
    user: FeishuUser
    created: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created > _SESSION_TTL


class Store:
    """进程内状态。开发用；生产要换 Redis 之类。"""

    def __init__(self):
        self.pending: dict = {}
        self.sessions: dict = {}

    def put_pending(self, state: str, item: _Pending) -> None:
        self._sweep()
        self.pending[state] = item

    def take_pending(self, state: str) -> Optional[_Pending]:
        """取出即删除：授权码流程里 state 是一次性的，重放必须失败。"""
        self._sweep()
        return self.pending.pop(state, None)

    def _sweep(self) -> None:
        now = time.time()
        # list() 先拍快照：多线程服务器里遍历期间别的线程可能在插会话
        for key, v in list(self.pending.items()):
            if now - v.created > _STATE_TTL:
                self.pending.pop(key, None)
        for key, v in list(self.sessions.items()):
            if v.expired:
                self.sessions.pop(key, None)


#: 页面样式。跟着苹果的设计语言走（macOS 系统设置 / apple.com）：
#: 分组卡片、发丝线分隔、克制的强调色、状态用 pill 而不是只靠文字。
#:
#: 字体不打包、不内联：`-apple-system` 打头让苹果设备用上真正的 SF Pro，
#: 其余平台回落 Inter（从 Google Fonts 拿）。**字体加载失败不影响可用性**——
#: 这东西可能跑在没有外网的堡垒机上，所以 fallback 链一直排到 system-ui。
_CSS = """
:root{
  color-scheme:light dark;
  --ground:#f5f5f7; --surface:#fff; --surface-2:#f5f5f7;
  --ink:#1d1d1f; --ink-2:#6e6e73; --ink-3:#86868b;
  --hair:rgba(0,0,0,.10); --hair-strong:rgba(0,0,0,.16);
  --accent:#0071e3; --accent-soft:rgba(0,113,227,.10);
  --good:#248a3d; --good-soft:rgba(36,138,61,.12);
  --warn:#b25000; --warn-soft:rgba(178,80,0,.12);
  --crit:#c4271c; --crit-soft:rgba(196,39,28,.12);
  --shadow:0 1px 2px rgba(0,0,0,.04),0 6px 20px rgba(0,0,0,.05);
  --ui:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI Variable Text",
       "Segoe UI",system-ui,"Noto Sans SC",sans-serif;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#000; --surface:#1c1c1e; --surface-2:#2c2c2e;
    --ink:#f5f5f7; --ink-2:#98989d; --ink-3:#8e8e93;
    --hair:rgba(255,255,255,.12); --hair-strong:rgba(255,255,255,.18);
    --accent:#2997ff; --accent-soft:rgba(41,151,255,.16);
    --good:#30d158; --good-soft:rgba(48,209,88,.16);
    --warn:#ff9f0a; --warn-soft:rgba(255,159,10,.16);
    --crit:#ff453a; --crit-soft:rgba(255,69,58,.16);
    --shadow:0 1px 2px rgba(0,0,0,.5);
  }
}
:root[data-theme="dark"]{
  --ground:#000; --surface:#1c1c1e; --surface-2:#2c2c2e;
  --ink:#f5f5f7; --ink-2:#98989d; --ink-3:#8e8e93;
  --hair:rgba(255,255,255,.12); --hair-strong:rgba(255,255,255,.18);
  --accent:#2997ff; --accent-soft:rgba(41,151,255,.16);
  --good:#30d158; --good-soft:rgba(48,209,88,.16);
  --warn:#ff9f0a; --warn-soft:rgba(255,159,10,.16);
  --crit:#ff453a; --crit-soft:rgba(255,69,58,.16);
  --shadow:0 1px 2px rgba(0,0,0,.5);
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--ui); font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:58rem; margin-inline:auto; padding:3rem 1.5rem 5rem;
      display:flex; flex-direction:column; gap:2rem}
.masthead{display:flex; flex-direction:column; gap:.35rem}
h1{font-size:1.75rem; line-height:1.2; letter-spacing:-.021em; font-weight:600;
   margin:0; text-wrap:balance}
h2{font-size:1rem; font-weight:600; letter-spacing:-.01em; margin:0}
.eyebrow{font-size:.6875rem; font-weight:600; letter-spacing:.06em;
         text-transform:uppercase; color:var(--ink-3)}
.lede{margin:0; color:var(--ink-2); font-size:.9375rem; max-width:42rem}
.group{display:flex; flex-direction:column; gap:.6rem}
.group-label{font-size:.6875rem; font-weight:600; letter-spacing:.06em;
             text-transform:uppercase; color:var(--ink-3); padding-inline:.15rem}
.card{background:var(--surface); border:1px solid var(--hair);
      border-radius:14px; box-shadow:var(--shadow); overflow:hidden}
.card-pad{padding:1.15rem 1.25rem}
.row{display:flex; gap:1rem; align-items:baseline;
     padding:.7rem 1.25rem; border-top:1px solid var(--hair)}
.row:first-child{border-top:0}
.row .k{flex:0 0 9.5rem; color:var(--ink-2); font-size:.8125rem}
.row .v{flex:1 1 auto; min-width:0; font-family:var(--mono); font-size:.8125rem;
        word-break:break-all}
.row .v.empty{color:var(--ink-3); font-family:var(--ui)}
.pill{display:inline-flex; align-items:center; gap:.3rem; flex:none;
      padding:.13rem .5rem; border-radius:980px; font-size:.6875rem;
      font-weight:600; letter-spacing:.01em; white-space:nowrap;
      background:var(--surface-2); color:var(--ink-2)}
.pill.good{background:var(--good-soft); color:var(--good)}
.pill.warn{background:var(--warn-soft); color:var(--warn)}
.pill.crit{background:var(--crit-soft); color:var(--crit)}
.notice{padding:.85rem 1.25rem; border-top:1px solid var(--hair);
        font-size:.8125rem; color:var(--ink-2); line-height:1.6}
.notice.good{background:var(--good-soft)}
.notice.crit{background:var(--crit-soft)}
.notice b{color:var(--ink); font-weight:600}
.notice ol{margin:.5rem 0 0; padding-inline-start:1.2rem;
           display:flex; flex-direction:column; gap:.3rem}
.scroll{overflow-x:auto; -webkit-overflow-scrolling:touch}
table{border-collapse:collapse; width:100%; font-size:.8125rem;
      font-variant-numeric:tabular-nums}
th,td{text-align:left; padding:.6rem 1.25rem; border-top:1px solid var(--hair);
      white-space:nowrap}
thead th{border-top:0; font-size:.6875rem; font-weight:600; letter-spacing:.04em;
         text-transform:uppercase; color:var(--ink-3)}
tbody tr:first-child td{border-top:1px solid var(--hair-strong)}
.plat{font-weight:590; color:var(--ink); white-space:nowrap}
.btn{display:inline-flex; align-items:center; justify-content:center;
     padding:.66rem 1.4rem; border-radius:980px; background:var(--accent);
     color:#fff; text-decoration:none; font-size:.9375rem; font-weight:500;
     transition:opacity .18s ease; align-self:flex-start}
.btn:hover{opacity:.85}
a{color:var(--accent); text-decoration:none}
a:hover{text-decoration:underline}
a:focus-visible,.btn:focus-visible{outline:2px solid var(--accent);
  outline-offset:3px; border-radius:6px}
code{font-family:var(--mono); font-size:.875em; background:var(--surface-2);
     padding:.08rem .32rem; border-radius:5px; border:1px solid var(--hair)}
.notice code{background:var(--surface); }
.steps{display:flex; flex-direction:column}
.step{display:flex; gap:.9rem; padding:.9rem 1.25rem; border-top:1px solid var(--hair)}
.step:first-child{border-top:0}
.step .n{flex:none; width:1.35rem; height:1.35rem; border-radius:50%;
         background:var(--accent-soft); color:var(--accent); font-size:.75rem;
         font-weight:600; display:flex; align-items:center; justify-content:center}
.step .t{flex:1; font-size:.875rem; min-width:0}
.step .t p{margin:.35rem 0 0; color:var(--ink-2); font-size:.8125rem}
.foot{color:var(--ink-3); font-size:.75rem; text-align:center}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


def _page(title: str, body: str) -> bytes:
    return (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Inter:wght@400;500;600&display=swap">'
        f"<style>{_CSS}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>'
    ).encode()


def _setup_hint(base: str = "") -> str:
    """「还差什么」写成能照着做的步骤，而不是一句变量名。

    每一条都有官方文档依据，不是凭印象：
      · 重定向 URL 在「安全设置」配，**和「网页」能力无关**——只有要在飞书客户端里
        免登打开页面才需要那个能力。
      · `user_info` 接口本身零权限，但企业邮箱要 `contact:user.employee:readonly`；
        公司若用第三方企业邮（非飞书邮箱服务），地址在 `email` 字段，
        那条要 `contact:user.email:readonly`。两条都申请最省事。
      · 用户授权是显式的：authorize 不带 scope 就不会授予，字段会静默为空。
    """
    redirect = f"{base or '<本机地址>'}/auth/callback"
    steps = [
        ("1", "创建企业自建应用", "到 <code>open.feishu.cn</code> 开发者后台新建。"),
        (
            "2",
            "登记重定向 URL",
            "「安全设置」→ 重定向 URL，填下面这条，"
            "协议、端口、结尾都要逐字一致：<br>"
            f"<code>{html.escape(redirect)}</code>",
        ),
        (
            "3",
            "申请权限",
            "「权限管理」里加 <code>contact:user.employee:readonly</code> 与 "
            "<code>contact:user.email:readonly</code>——企业邮箱只能从这两条拿。",
        ),
        ("4", "创建版本并发布", "等管理员审核通过。光在权限页勾选不生效。"),
        (
            "5",
            "把凭证给本服务",
            "在启动它的终端里 <code>export DELIVERY_FEISHU_APP_ID=…</code> 与 "
            "<code>export DELIVERY_FEISHU_APP_SECRET=…</code>，然后重启。",
        ),
    ]
    rows = "".join(
        f'<div class="step"><span class="n">{n}</span>'
        f'<span class="t"><b>{t}</b><p>{d}</p></span></div>'
        for n, t, d in steps
    )
    return (
        '<header class="masthead">'
        '<span class="eyebrow">尚未接入</span>'
        "<h1>还没接上飞书</h1>"
        '<p class="lede">本机服务已经起来了，缺的是一个飞书应用。'
        "五步，大约十分钟。</p></header>"
        '<section class="group"><div class="group-label">配置步骤</div>'
        f'<div class="card"><div class="steps">{rows}</div></div></section>'
        '<section class="group"><div class="group-label">常见误区</div>'
        '<div class="card"><div class="notice">'
        "<b>不需要开「网页」能力。</b>重定向 URL 属于安全设置，与它无关；"
        "只有要在飞书客户端内免登打开页面时才开。<br>"
        "要私聊提醒用户则另需「机器人」能力加 <code>im:message:send_as_bot</code>。<br>"
        "App Secret 只在服务端使用，不会出现在浏览器里。"
        "</div></div></section>"
    )


#: 前端静态文件。**白名单，不做目录映射**：路径不经过文件系统解析，谈不上穿越。
WEB_DIR = Path(__file__).with_name("web")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/core.js": ("core.js", "text/javascript; charset=utf-8"),
    "/requests.js": ("requests.js", "text/javascript; charset=utf-8"),
    "/assets.js": ("assets.js", "text/javascript; charset=utf-8"),
    "/permissions.js": ("permissions.js", "text/javascript; charset=utf-8"),
    "/health.js": ("health.js", "text/javascript; charset=utf-8"),
}
#: 页面只加载同源资源。前端不拼 innerHTML，这条 CSP 是第二道闸。
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_ADMIN_PEOPLE = "/api/admin/people/"
_ADMIN_REVIEW = "/api/admin/review"


def _approval_ready(backend) -> Optional[bool]:
    """审批对象能不能建出来（不发网络请求）；配置本身读坏了交给系统状态页那一项去报。"""
    try:
        return backend.approval() is not None
    except Exception:  # noqa: BLE001
        return None


def _is_requests_path(path: str) -> bool:
    return any(
        path == p or path.startswith(p + "/")
        for p in ("/api/requests", "/api/admin/requests", "/api/policies", "/api/admin/policies")
    )


_REVIEW_MAX_BODY = 4096


class Backend:
    """面板的数据源：权限快照、人员名册、管理员名单、账号标签。

    每次请求按文件 mtime 判断要不要重读——采集器重新生成快照后不用重启服务。
    文件**存在但读坏了**一律报错，不降级成「没有数据」：那样和「这个人确实没有权限」
    长得一样（见 inventory.py 的说明）。
    """

    def __init__(
        self,
        *,
        inventory_path: Optional[str] = None,
        people_path: Optional[str] = None,
        bindings_path: Optional[str] = None,
        admins_path: Optional[str] = None,
        labels_path: Optional[str] = None,
        platforms: Optional[dict] = None,
        proposal_path: Optional[str] = None,
        manual_path: Optional[str] = None,
        tickets_path: Optional[str] = None,
        templates_path: Optional[str] = None,
        approval_path: Optional[str] = None,
        feishu_token: Optional[Callable[[], str]] = None,
        executor: Optional[Callable[[str, str], object]] = None,
        approval_transport=None,
        assets_path: Optional[str] = None,
        policies_path: Optional[str] = None,
        policy_rules_path: Optional[str] = None,
        notify: Optional[Callable[[str, dict], None]] = None,
    ):
        self._notify = notify
        self.assets_path = assets_path
        self.policies_path = policies_path
        self.policy_rules_path = policy_rules_path
        self.tickets_path = tickets_path
        self.templates_path = templates_path
        self.approval_path = approval_path
        self._feishu_token = feishu_token
        self._executor = executor or executor_from_env
        self._approval_transport = approval_transport
        self._flows: Optional[Flows] = None
        self.proposal_path = proposal_path
        self.manual_path = manual_path
        self.inventory_path = inventory_path
        self.people_path = people_path
        self.bindings_path = bindings_path
        self.admins_path = admins_path
        self.labels_path = labels_path
        self.platforms = dict(platforms or {})
        self._cache: dict = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stamp(*paths) -> tuple:
        out = []
        for path in paths:
            try:
                out.append(Path(path).stat().st_mtime_ns if path else None)
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    def _cached(self, name: str, stamp: tuple, build):
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] == stamp:
                return hit[1]
            value = build()
            self._cache[name] = (stamp, value)
            return value

    def snapshot(self) -> Optional[inventory.Snapshot]:
        # 快照文件还没生成：显示「未接入」即可（每张账号卡片会标「快照中不存在」）。
        # 文件在但读坏了照样抛。
        def build():
            if not self.inventory_path or not Path(self.inventory_path).exists():
                return None
            return inventory.load(self.inventory_path)

        return self._cached("inventory", self._stamp(self.inventory_path), build)

    def people(self) -> people_mod.PeopleIndex:
        def build():
            # 名册缺失**不能**降级成空名册：那样人人都会被告知「你目前没有云账号」，
            # 和真的没有账号分不出来。
            if not self.people_path or not Path(self.people_path).exists():
                raise DeliveryError("人员名册还没生成（delivery identity people），面板暂不可用")
            return people_mod.load(self.people_path, bindings_path=self.bindings_path)

        # 绑定文件也进缓存键：管理员手工删掉一条错误绑定要立刻生效
        return self._cached("people", self._stamp(self.people_path, self.bindings_path), build)

    def admins(self) -> Admins:
        return self._cached(
            "admins", self._stamp(self.admins_path), lambda: load_admins(self.admins_path)
        )

    def labels(self) -> Labels:
        def build():
            accounts = {}
            if self.labels_path and Path(self.labels_path).exists():
                try:
                    accounts = json.loads(Path(self.labels_path).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise DeliveryError(f"读不了账号标签 {self.labels_path}：{exc}") from exc
                if not isinstance(accounts, dict):
                    raise DeliveryError(f"{self.labels_path} 顶层必须是对象")
            return Labels(self.platforms, accounts)

        return self._cached("labels", self._stamp(self.labels_path), build)

    def assets(self):
        return self._cached(
            "assets", self._stamp(self.assets_path), lambda: assets_mod.load(self.assets_path)
        )

    def policies(self) -> Optional[dict]:
        return self._cached(
            "policies",
            self._stamp(self.policies_path),
            lambda: policies_mod.load(self.policies_path),
        )

    def policy_rules(self) -> policies_mod.Rules:
        return self._cached(
            "policy_rules",
            self._stamp(self.policy_rules_path),
            lambda: policies_mod.load_rules(self.policy_rules_path),
        )

    def current_policies(self, platform: str, account: str, name: str) -> Optional[dict]:
        """权限快照里这个子账号当前有的策略 {策略名小写: (策略名, 来源)}。

        直接授予和经用户组继承的都算；只在资源组 / 项目范围生效的（带 @）不算「已有」。
        快照没有、这个云账号没采全、子账号不在快照里时返回 None（未知）。
        """
        snap = self.snapshot()
        if snap is None or any(i.startswith(f"{platform}/{account}：") for i in snap.incomplete):
            return None
        user = snap.user(platform, account, name)
        if user is None:
            return None
        out: dict = {}
        for policy in user.policies:
            if "@" not in policy:
                out.setdefault(policy.lower(), (policy, "直接授予"))
        for group in snap.groups_of(user):
            for policy in group.policies:
                if "@" not in policy:
                    out.setdefault(policy.lower(), (policy, f"经用户组 {group.name}"))
        return out

    def catalog(self):
        return self._cached(
            "catalog", self._stamp(self.templates_path), lambda: load_catalog(self.templates_path)
        )

    def approval(self) -> Optional[FeishuApproval]:
        def build():
            config = ApprovalConfig.load(self.approval_path)
            if config is None or self._feishu_token is None:
                return None
            kw = {"transport": self._approval_transport} if self._approval_transport else {}
            return FeishuApproval(config, self._feishu_token, **kw)

        return self._cached("approval", self._stamp(self.approval_path), build)

    def flows(self) -> Optional[Flows]:
        if not self.tickets_path:
            return None
        if self._flows is None:
            paths = self.review_paths()

            def link(email: str, account: str, ticket_id: str) -> None:
                if paths is not None:
                    review_mod.add_link(paths, email, account, actor=f"request:{ticket_id}")

            self._flows = Flows(
                store=tickets_mod.TicketStore(self.tickets_path),
                catalog=self.catalog,
                approval=self.approval,
                roster=self.people,
                executor=self._executor,
                add_manual_link=link,
                current_groups=self.current_groups,
                policy_snapshot=self.policies,
                policy_rules=self.policy_rules,
                current_policies=self.current_policies,
                notify=self._notify,
            )
        return self._flows

    def current_groups(self, platform: str, account: str, name: str) -> Optional[set]:
        """权限快照里这个子账号当前所在的用户组；快照没有或这个云账号没采全时返回 None（未知）。"""
        snap = self.snapshot()
        if snap is None or any(i.startswith(f"{platform}/{account}：") for i in snap.incomplete):
            return None
        user = snap.user(platform, account, name)
        if user is None:
            return None
        return {g.name for g in snap.groups_of(user)} | set(user.groups)

    def review_paths(self) -> Optional[review_mod.ReviewPaths]:
        if not (self.people_path and self.proposal_path and self.manual_path):
            return None
        return review_mod.ReviewPaths(
            proposal=self.proposal_path,
            manual=self.manual_path,
            people=self.people_path,
            bindings=self.bindings_path,
        )

    def role(self, user: FeishuUser) -> str:
        # 只拿企业邮箱做兼容比对；个人联系邮箱不是公司分配的，不能用来认管理员。
        return self.admins().role_of(union_id=user.union_id, email=user.enterprise_email)

    def warnings(self) -> list:
        out = []
        if self.admins().emails:
            out.append(
                "管理员名单里还有按邮箱匹配的条目。邮箱会变、会被复用，"
                "请改成 union_ids（WUJI IAM 接入规范）。"
            )
        return out


def make_handler(
    registry: PlatformRegistry,
    store: Store,
    *,
    app_id: str,
    app_secret: str,
    base_url: str,
    backend: Optional[Backend] = None,
    proxy: Optional[ProxyIdentity] = None,
):
    """proxy 不为空即代理登录模式：只认 oauth2-proxy 注入的请求头，飞书登录路由关闭。"""
    backend = backend or Backend(platforms={p.id: p.display for p in registry})
    requests_api = RequestsApi(
        backend.flows,
        account_label=lambda platform, account: backend.labels().account(platform, account),
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "delivery-dev"

        # ── 工具 ──────────────────────────────────────────────────────────
        def _send(self, code: int, body: bytes, *, ctype="text/html; charset=utf-8", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 看板会显示账号与权限，不该进任何缓存
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict):
            self._send(
                code,
                json.dumps(payload, ensure_ascii=False).encode(),
                ctype="application/json; charset=utf-8",
            )

        def _session(self) -> Optional[_WebSession]:
            if proxy is not None:
                user = proxy.user(self.headers)
                return _WebSession(user=user) if user is not None else None
            bearer = self.headers.get("Authorization") or ""
            if bearer.startswith("Bearer "):
                # CLI：delivery login 换来的会话令牌，不走 Cookie
                session = store.sessions.get(bearer[len("Bearer ") :].strip())
                return session if session is not None and not session.expired else None
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            cookie = http.cookies.SimpleCookie()
            try:
                cookie.load(raw)
            except http.cookies.CookieError:
                return None
            morsel = cookie.get(COOKIE_NAME)
            if morsel is None:
                return None
            session = store.sessions.get(morsel.value)
            if session is None or session.expired:
                return None
            return session

        def log_message(self, *args):
            """默认实现会把**含授权码的完整 URL** 打到 stderr。"""

        # ── 路由 ──────────────────────────────────────────────────────────
        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path in _STATIC:
                return self._static(*_STATIC[path])
            if path.startswith("/api/"):
                try:
                    return self._api(path)
                except Exception as exc:  # noqa: BLE001 — 任何异常都要回 JSON，不能断连接
                    # 数据文件坏了：明说「不可用」，不降级成空数据。细节只进服务端日志——
                    # 异常里可能有服务器路径、邮箱，不给浏览器。
                    print(f"[api] {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    lines = str(exc).splitlines() if isinstance(exc, DeliveryError) else []
                    brief = lines[0] if lines else ""
                    safe = brief if brief.startswith("人员名册还没生成") else ""
                    return self._json(
                        500, {"error": safe or "面板数据暂不可用，请联系管理员查看服务端日志"}
                    )
            if proxy is not None and path.startswith("/auth/"):
                return self._send(404, _page("404", "<h1>没有这个页面</h1>"))
            if path == "/auth/login":
                return self._start_login()
            if path == "/auth/callback":
                return self._finish_login()
            if path == "/auth/logout":
                session_id = ""
                raw = self.headers.get("Cookie") or ""
                cookie = http.cookies.SimpleCookie()
                try:
                    cookie.load(raw)
                    morsel = cookie.get(COOKIE_NAME)
                    session_id = morsel.value if morsel else ""
                except http.cookies.CookieError:
                    session_id = ""
                store.sessions.pop(session_id, None)
                return self._send(
                    302,
                    b"",
                    headers={"Location": "/", "Set-Cookie": f"{COOKIE_NAME}=; Max-Age=0; Path=/"},
                )
            return self._send(404, _page("404", "<h1>没有这个页面</h1>"))

        # ── 前端与 API ────────────────────────────────────────────────────
        def _static(self, name: str, ctype: str):
            try:
                body = (WEB_DIR / name).read_bytes()
            except OSError:
                return self._send(500, _page("前端缺失", "<h1>前端文件缺失</h1>"))
            return self._send(
                200,
                body,
                ctype=ctype,
                headers={
                    "Content-Security-Policy": _CSP,
                    "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer",
                },
            )

        def _require(self, *, admin: bool = False) -> Optional[_WebSession]:
            session = self._session()
            if session is None:
                self._json(401, {"error": "未登录或会话已过期"})
                return None
            if admin and backend.role(session.user) != ROLE_ADMIN:
                self._json(403, {"error": "需要管理员权限"})
                return None
            return session

        def _api(self, path: str):
            if path == "/api/session":
                session = self._session()
                if session is None:
                    login_url = proxy.login_url if proxy is not None else "/auth/login"
                    return self._json(200, {"authenticated": False, "login_url": login_url})
                user = session.user
                return self._json(
                    200,
                    {
                        "authenticated": True,
                        "name": user.name,
                        "union_id": user.union_id,
                        "email": user.enterprise_email or user.email,
                        "role": backend.role(user),
                        "login_url": proxy.login_url if proxy is not None else "/auth/login",
                        "logout_url": proxy.logout_url if proxy is not None else "/auth/logout",
                    },
                )
            if path == "/api/me":
                session = self._require()
                if session is None:
                    return None
                user = session.user
                found = backend.people().resolve(
                    union_id=user.union_id, enterprise_email=user.enterprise_email
                )
                if proxy is not None and found.person is None and found.binding == BIND_NONE:
                    # 代理模式：名册里还没有这个 union_id 时才去 IAM 查邮箱做首次关联
                    email = proxy.email(self.headers, user.union_id)
                    if email:
                        found = backend.people().resolve(
                            union_id=user.union_id, enterprise_email=email
                        )
                    elif found.person is None and found.binding == BIND_NONE:
                        found = people_mod.Resolution(
                            None,
                            BIND_NONE,
                            "名册里还没有你的 union_id，且公司 IAM 没有返回可用的企业邮箱，"
                            "无法自动关联。请把本页显示的 union_id 发给管理员登记。",
                        )
                detail = person_detail(
                    found.person,
                    backend.snapshot(),
                    backend.labels(),
                    binding=found.binding,
                    note=found.note,
                    fallback={
                        "name": user.name,
                        "email": user.enterprise_email or user.email,
                        "union_id": user.union_id,
                    },
                )
                if backend.role(user) != ROLE_ADMIN:
                    # 采集错误原文可能带接口返回片段；普通用户只需要知道「哪个账号没采全」
                    detail["snapshot_incomplete"] = [
                        line.split("：", 1)[0] + "：本次未采集完整"
                        for line in detail["snapshot_incomplete"]
                    ]
                return self._json(200, detail)
            if path == "/api/admin/overview":
                if self._require(admin=True) is None:
                    return None
                return self._json(
                    200,
                    admin_overview(
                        backend.snapshot(),
                        backend.people(),
                        backend.labels(),
                        warnings=backend.warnings(),
                    ),
                )
            if path == "/api/admin/people":
                if self._require(admin=True) is None:
                    return None
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                wanted = (query.get("filter") or ["all"])[0]
                if wanted not in FILTERS:
                    return self._json(400, {"error": f"filter 只能是 {', '.join(FILTERS)}"})
                return self._json(
                    200,
                    admin_people(
                        backend.snapshot(), backend.people(), backend.labels(), filter=wanted
                    ),
                )
            if path.startswith(_ADMIN_PEOPLE):
                if self._require(admin=True) is None:
                    return None
                key = urllib.parse.unquote(path[len(_ADMIN_PEOPLE) :])
                person = backend.people().by_key(key) if key else None
                if person is None:
                    return self._json(404, {"error": "名册里没有这个人"})
                bound = bool(person.union_id)
                return self._json(
                    200,
                    person_detail(
                        person,
                        backend.snapshot(),
                        backend.labels(),
                        binding=BIND_UNION_ID if bound else BIND_NONE,
                        note="" if bound else "此人还没有绑定 union_id，本人首次登录后自动绑定。",
                        include_pending=True,
                    ),
                )
            if _is_requests_path(path):
                return self._requests("GET", path, None)
            if path == "/api/admin/health":
                if self._require(admin=True) is None:
                    return None
                return self._json(
                    200,
                    health_mod.collect(
                        backend,
                        auth_mode="proxy" if proxy is not None else "feishu",
                        approval_ready=_approval_ready(backend),
                    ),
                )
            if path in ("/api/assets", "/api/admin/assets"):
                admin = path == "/api/admin/assets"
                session = self._require(admin=admin)
                if session is None:
                    return None
                labels = backend.labels()
                scopes = None
                if not admin:
                    person = backend.people().resolve(union_id=session.user.union_id).person
                    scopes = {(r.platform, r.account) for r in (person.accounts if person else ())}
                return self._json(
                    200,
                    assets_mod.summary_view(backend.assets(), scopes=scopes, labels=labels.account),
                )
            if path == _ADMIN_REVIEW:
                if self._require(admin=True) is None:
                    return None
                paths = backend.review_paths()
                if paths is None:
                    return self._json(200, {"enabled": False, "records": []})
                return self._json(
                    200, {"enabled": True, "records": review_mod.records(paths.manual)}
                )
            return self._json(404, {"error": "没有这个接口"})

        def _same_origin_json(self) -> Optional[str]:
            """写接口的 CSRF 防护：必须是本站前端发的 JSON 请求。返回拒绝原因。"""
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "Content-Type 必须是 application/json"
            if self.headers.get("X-Panel-Request") != "1":
                return "缺少 X-Panel-Request 请求头"
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return "跨站请求被拒绝"
            origin = self.headers.get("Origin")
            if origin:
                # 经 nginx / oauth2-proxy 转发后 Host 可能是内部地址：也接受配置的对外地址
                allowed = {self.headers.get("Host") or "", urllib.parse.urlsplit(base_url).netloc}
                if urllib.parse.urlsplit(origin).netloc not in allowed - {""}:
                    return "跨站请求被拒绝"
            return None

        def _json_body(self, *, allow_empty: bool = False):
            """读 JSON 请求体。返回 (dict, None) 或 (None, 已发送的错误响应)。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length == 0 and allow_empty:
                return {}, None
            if length <= 0 or length > _REVIEW_MAX_BODY:
                return None, self._json(400, {"error": "请求体为空或过大"})
            try:
                payload = json.loads(self.rfile.read(length).decode())
            except (ValueError, UnicodeDecodeError):
                return None, self._json(400, {"error": "请求体不是合法 JSON"})
            if not isinstance(payload, dict):
                return None, self._json(400, {"error": "请求体必须是对象"})
            return payload, None

        def _requests(self, method: str, path: str, body):
            admin = path.startswith("/api/admin/")
            session = self._require(admin=admin)
            if session is None:
                return None
            if method == "POST":
                refused = self._same_origin_json()
                if refused:
                    return self._json(403, {"error": refused})
                body, sent = self._json_body(allow_empty=True)
                if body is None:
                    return sent
            user = session.user
            if not user.union_id:
                # 申请单按 union_id 归属：没有 union_id 的登录者会匹配到别人的空 union_id 单子
                return self._json(403, {"error": "登录信息里没有 union_id，不能使用申请功能"})
            # 只用企业邮箱（名册按它对应新账号）：个人联系邮箱不能拿来认领公司账号
            try:
                person = backend.people().resolve(union_id=user.union_id).person
            except DeliveryError:
                person = None
            email = person.email if person and person.email else user.enterprise_email
            caller = Caller(
                union_id=user.union_id,
                name=user.name,
                email=email,
                open_id=user.open_id,
                user_id=user.user_id,
                admin=backend.role(user) == ROLE_ADMIN,
            )
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            try:
                status, payload = requests_api.handle(method, path, query, body, caller)
            except Exception as exc:  # noqa: BLE001 — 任何异常都回 JSON，细节只进服务端日志
                print(f"[requests] {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(500, {"error": "申请服务出错，请联系管理员查看服务端日志"})
            return self._json(status, payload)

        def _review(self):
            session = self._require(admin=True)
            if session is None:
                return None
            refused = self._same_origin_json()
            if refused:
                return self._json(403, {"error": refused})
            paths = backend.review_paths()
            if paths is None:
                return self._json(404, {"error": "服务端没有配置映射提案和人工记录路径"})
            payload, sent = self._json_body()
            if payload is None:
                return sent
            user = session.user
            try:
                result = review_mod.apply(
                    paths,
                    payload,
                    actor_union_id=user.union_id,
                    actor_name=user.name,
                )
            except review_mod.ReviewError as exc:
                if exc.status >= 500:
                    print(f"[review] {exc}", file=sys.stderr)
                    return self._json(exc.status, {"error": "名册审核暂不可用，请查看服务端日志"})
                return self._json(exc.status, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 — 任何异常都回 JSON
                print(f"[review] {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(500, {"error": "名册审核失败，请查看服务端日志"})
            return self._json(200, result)

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == _ADMIN_REVIEW:
                return self._review()
            if _is_requests_path(path):
                return self._requests("POST", path, None)
            if proxy is not None or path != "/auth/exchange":
                return self._json(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode() or "{}")
            except ValueError:
                return self._json(400, {"error": "请求体不是合法 JSON"})
            code = str(payload.get("code") or "")
            verifier = str(payload.get("code_verifier") or "")
            redirect_uri = str(payload.get("redirect_uri") or "")
            if not code or not redirect_uri:
                return self._json(400, {"error": "缺少 code 或 redirect_uri"})
            try:
                token = exchange_code(
                    app_id=app_id,
                    app_secret=app_secret,
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=verifier,
                )
                user = fetch_user(token)
            except FeishuError as exc:
                return self._json(502, {"error": str(exc)})
            session_id = secrets.token_urlsafe(32)
            store.sessions[session_id] = _WebSession(user=user)
            return self._json(
                200,
                {
                    "token": session_id,
                    "union_id": user.identity,
                    "name": user.name,
                    "expires_in": _SESSION_TTL,
                },
            )

        # ── 浏览器登录 ────────────────────────────────────────────────────
        def _start_login(self):
            if not app_id:
                # 这页是「第一次跑起来」的人唯一会看到的东西，光说缺哪个变量等于没说。
                # 重定向 URL 直接把本机实际用的那条印出来——飞书那边要求逐字一致，
                # 靠人照着文档拼是 20029 错误最常见的来源。
                return self._send(500, _page("还没接上飞书", _setup_hint(base_url)))
            verifier, challenge = _pkce_pair()
            state = secrets.token_urlsafe(24)
            redirect_uri = f"{base_url}/auth/callback"
            store.put_pending(state, _Pending(verifier=verifier, redirect_uri=redirect_uri))
            url = authorize_url(
                app_id=app_id, redirect_uri=redirect_uri, state=state, challenge=challenge
            )
            return self._send(302, b"", headers={"Location": url})

        def _finish_login(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            err = (query.get("error_description") or query.get("error") or [""])[0]
            if err:
                return self._send(
                    400, _page("授权失败", f"<h1>授权失败</h1><p>{html.escape(err)}</p>")
                )
            code = (query.get("code") or [""])[0]
            state = (query.get("state") or [""])[0]
            # take_pending 取出即删：state 一次性，重放必败
            pending = store.take_pending(state)
            if pending is None:
                return self._send(
                    400,
                    _page("state 无效", "<h1>state 无效或已过期</h1><p>请回到首页重新登录。</p>"),
                )
            if not code:
                return self._send(400, _page("缺少授权码", "<h1>回调里没有授权码</h1>"))
            try:
                token = exchange_code(
                    app_id=app_id,
                    app_secret=app_secret,
                    code=code,
                    redirect_uri=pending.redirect_uri,
                    code_verifier=pending.verifier,
                )
                user = fetch_user(token)
            except FeishuError as exc:
                return self._send(
                    502, _page("登录失败", f"<h1>登录失败</h1><p>{html.escape(str(exc))}</p>")
                )
            session_id = secrets.token_urlsafe(32)
            store.sessions[session_id] = _WebSession(user=user)
            cookie = (
                f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={_SESSION_TTL}"
            )
            return self._send(302, b"", headers={"Location": "/", "Set-Cookie": cookie})

    return Handler


def _tenant_token_cache(app_id: str, app_secret: str) -> Callable[[], str]:
    """飞书 tenant_access_token 有效期 2 小时：缓存 90 分钟，过期再换。"""
    from .identity.directory import tenant_token

    state = {"token": "", "at": 0.0}
    lock = threading.Lock()

    def get() -> str:
        with lock:
            if not state["token"] or time.time() - state["at"] > 90 * 60:
                state["token"] = tenant_token(app_id, app_secret)
                state["at"] = time.time()
            return state["token"]

    return get


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    registry: Optional[PlatformRegistry] = None,
    inventory_path: Optional[str] = None,
    people_path: Optional[str] = None,
    admins_path: Optional[str] = None,
    labels_path: Optional[str] = None,
    proposal_path: Optional[str] = None,
    manual_path: Optional[str] = None,
    tickets_path: Optional[str] = None,
    templates_path: Optional[str] = None,
    approval_path: Optional[str] = None,
    assets_path: Optional[str] = None,
    policies_path: Optional[str] = None,
    policy_rules_path: Optional[str] = None,
    auth: Optional[str] = None,
    echo=print,
) -> None:
    registry = registry or PlatformRegistry.load()
    auth = auth or os.environ.get(ENV_AUTH, "") or AUTH_FEISHU
    if auth not in AUTH_MODES:
        raise DeliveryError(f"登录方式只能是 {' / '.join(AUTH_MODES)}，收到 {auth!r}")
    proxy = ProxyIdentity(ProxyAuthConfig.from_env()) if auth == AUTH_PROXY else None
    app_id = os.environ.get("DELIVERY_FEISHU_APP_ID", "")
    app_secret = os.environ.get("DELIVERY_FEISHU_APP_SECRET", "")
    base_url = os.environ.get("DELIVERY_BASE_URL") or f"http://localhost:{port}"
    bindings_path = str(Path(people_path).with_name("bindings.json")) if people_path else None
    token = _tenant_token_cache(app_id, app_secret) if app_id and app_secret else None
    notify = notify_mod.from_env(os.environ, token=token)
    if notify is not None:
        echo("申请状态通知：已开启（DELIVERY_NOTIFY=1）")
    backend = Backend(
        inventory_path=inventory_path,
        people_path=people_path,
        bindings_path=bindings_path,
        admins_path=admins_path,
        labels_path=labels_path,
        platforms={p.id: p.display for p in registry},
        proposal_path=proposal_path,
        manual_path=manual_path,
        tickets_path=tickets_path,
        templates_path=templates_path,
        approval_path=approval_path,
        assets_path=assets_path,
        policies_path=policies_path,
        policy_rules_path=policy_rules_path,
        feishu_token=token,
        notify=notify,
    )
    handler = make_handler(
        registry,
        Store(),
        app_id=app_id,
        app_secret=app_secret,
        base_url=base_url,
        backend=backend,
        proxy=proxy,
    )
    # 只绑回环：这是开发服务器，绑 0.0.0.0 会把还没做访问控制的看板暴露给整个网段。
    server = http.server.ThreadingHTTPServer((host, port), handler)
    echo(f"  权限快照    {inventory_path or '未配置（--inventory）'}")
    echo(f"  人员名册    {people_path or '未配置（--people）'}")
    if proxy is not None:
        echo(f"  登录方式    公司 IAM（经 oauth2-proxy），面板监听 {host}:{port}")
        echo(f"  userinfo    {proxy.config.userinfo_url or '未配置：首次登录不能按邮箱自动关联'}")
        echo("")
        echo("  浏览器访问 oauth2-proxy 的地址，不要直接访问面板端口。")
    else:
        echo(f"  控制台      {base_url}")
        echo(f"  回调地址    {base_url}/auth/callback")
        echo("")
        if not app_id or not app_secret:
            echo("  ⚠ 未设置 DELIVERY_FEISHU_APP_ID / DELIVERY_FEISHU_APP_SECRET，登录会失败")
            echo("")
        echo(f"  把 {base_url}/auth/callback 逐字加进飞书后台的「重定向 URL」，否则报 20029。")
    echo("  Ctrl-C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        echo("\n已停止")
    finally:
        server.server_close()
