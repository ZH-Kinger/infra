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

import contextlib
import gzip
import hashlib
import html
import http.cookies
import http.server
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable, Optional

from . import access as access_mod
from . import alerts, iam_sync, inventory
from . import approval_hook as hook_mod
from . import assets as assets_mod
from . import health as health_mod
from . import notify as notify_mod
from . import nudge as nudge_mod
from . import people as people_mod
from . import platforms as platforms_mod
from . import policies as policies_mod
from . import review as review_mod
from . import revoke as revoke_mod
from . import tickets as tickets_mod
from .approval import ApprovalConfig, FeishuApproval
from .catalog import load as load_catalog
from .errors import DeliveryError
from .feishu import FeishuError, FeishuUser, exchange_code, fetch_user
from .flows import Flows
from .login import _pkce_pair, authorize_url
from .people import BIND_NONE, BIND_UNION_ID
from .provision import describe_error as provision_describe
from .provision import executor_configured as provision_executor_configured
from .provision import executor_from_env
from .provision import issuer_configured as provision_issuer_configured
from .proxy_auth import (
    AUTH_FEISHU,
    AUTH_MODES,
    AUTH_PROXY,
    ENV_AUTH,
    ProxyAuthConfig,
    ProxyIdentity,
)
from .registry import PlatformRegistry
from .requests_api import TICKET_ID as _ID
from .requests_api import Caller, RequestsApi
from .roles import ROLE_ADMIN, Admins, load_admins
from .views import FILTERS, Labels, admin_overview, admin_people, my_keys, person_detail

#: 工具下载目录（`--downloads`）。九章的 aladdin 没有公开下载地址，只能我们自己托管；
#: 阿里和火山的 CLI 有官方地址，页面上直接给链接，不在这里放第二份。
#: **只从这一个目录发文件，文件名走严格白名单** —— 拼接用户给的路径去读文件是
#: 目录穿越的经典入口，这里连拼接都不做，只在目录列表里按名字精确匹配。
_DOWNLOAD_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")
_DOWNLOAD_CHUNK = 256 * 1024
#: 一次批量指派的上限。整个读-改-写在一把文件锁里，批太大会把锁占太久；
#: 而实际场景是「把某个地域/某一类机器整批划给某人」，几十个足够
_OWNER_BATCH_MAX = 200


#: sha256 缓存，键含**大小和 mtime** —— 文件换了就自然算新的。
#: 只按文件名缓存是错的：报一个过期的校验和比不报校验和更糟
_digests: dict = {}


def _sha256(item: Path) -> str:
    stat = item.stat()
    key = (str(item), stat.st_size, stat.st_mtime_ns)
    cached = _digests.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with item.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    if len(_digests) > 256:
        _digests.clear()
    _digests[key] = digest.hexdigest()
    return _digests[key]


def _alert_rules_opened(user, opened: list) -> None:
    """有人放开了内置禁用的策略 —— 发到管理员告警群。

    以前放开这类策略要 SSH 到服务器改文件（两种能力：服务器访问 + 审批），
    现在一个管理员会话就够了。审批那道门还在（员工申请到它仍要人批），
    但「谁在什么时候把护栏拆了一格」这件事必须当场有人看见。
    """
    conf = alerts.from_env(os.environ)
    if conf is None:
        print(f"[rules] 放开了内置禁用项 {opened}，但没配告警地址", file=sys.stderr)
        return
    who = getattr(user, "name", "") or getattr(user, "union_id", "")
    text = (
        f"【云权限面板】{who} 放开了默认不开放的权限策略：{'、'.join(opened)}\n"
        f"员工现在可以在权限列表里申请它们（仍需飞书审批）。"
        f"如果不是预期的改动，去管理后台「权限规则」页收回。"
    )
    try:
        alerts.send_feishu(text, webhook=conf[0], secret=conf[1])
    except Exception as exc:  # noqa: BLE001 — 告警发不出去不能影响已经落盘的规则
        print(f"[rules] 告警发送失败：{type(exc).__name__}: {exc}", file=sys.stderr)


def _owned_by(backend, email: str) -> dict:
    """指给这个人的资源，按 (平台, 云账号) 分好。

    资产快照可能没采、归属表可能还没建 —— 那是正常的初始状态，返回空表就好，
    绝不能让「资产没配」把首页整个弄挂。

    但**「文件坏了」不在此列**：那时返回空表的表现是「指派过的资源全都不见了」，
    和「还没指派过」一模一样，没人会发现。坏文件一律往上抛，让页面明确报错。
    """
    me = str(email or "").strip().lower()
    if not me:
        return {}
    owners = backend.asset_owners()
    snap = backend.assets()
    if not owners or not snap:
        return {}
    out: dict = {}
    for acc in snap.get("accounts") or []:
        key = (str(acc.get("platform") or ""), str(acc.get("account") or ""))
        for r in acc.get("resources") or []:
            own = owners.get(f"{key[0]}/{key[1]}/{r.get('id', '')}") or {}
            if str(own.get("email") or "").lower() != me:
                continue
            out.setdefault(key, []).append(
                {
                    "id": r.get("id", ""),
                    "name": r.get("name", ""),
                    "type_label": assets_mod.type_label(r.get("type", "")),
                    "region": r.get("region", ""),
                    "note": str(own.get("note") or ""),
                }
            )
    return out


def _downloads(directory: Optional[str]) -> list:
    """下载目录里的文件清单（名字、大小、sha256）。目录不存在就是空清单。"""
    if not directory:
        return []
    base = Path(directory)
    if not base.is_dir():
        return []
    out = []
    for item in sorted(base.iterdir()):
        if not item.is_file() or not _DOWNLOAD_NAME.match(item.name):
            continue
        out.append({"name": item.name, "size": item.stat().st_size, "sha256": _sha256(item)})
    return out


#: 取件页。**不在 _STATIC 里**，因为那张表里的页面进了 SPA 的登录流程
_PICKUP_STATIC = {
    "/pickup.js": ("pickup.js", "text/javascript; charset=utf-8"),
}
#: 查看凭证的地址：/c/<申请单号>#<密钥>。**密钥在 # 之后**，浏览器不会发给服务端
_VIEW_PREFIX = "/c/"
#: 取件接口的限流：没有登录这道门，剩下唯一能拦暴力猜令牌的就是它。
#: 只数**失败**，键是 (来源 IP, 申请单号) —— 正常人反复打开自己的链接不该被自己卡死，
#: 而线上所有请求的直连来源都是本机的反向代理，只按 IP 计数等于全员共用一份配额。
_PICKUP_WINDOW = 300.0
_PICKUP_MAX = 20
#: 除了「每个来源对每张单子」的失败配额，再留一个**粗粒度的整体上限**：
#: 按单号分桶之后，换一个格式合法但不存在的单号就能重新开一桶，而每次尝试都会
#: 走一遍 store.all() —— 那把文件锁是面板和定时任务共用的
_PICKUP_TRIES = 120
_pickup_hits: dict = {}
_pickup_tries: dict = {}
_pickup_lock = threading.Lock()
#: 面板前面有几层代理会往 X-Forwarded-For 追加。线上是 nginx → oauth2-proxy → 面板
ENV_PROXY_HOPS = "DELIVERY_PROXY_HOPS"
_DEFAULT_HOPS = 2


def _client_ip(peer: str, forwarded: str, hops: int = 0) -> str:
    """反向代理后面的真实来源 IP。

    **按固定跳数从右边数**，不按「第一个看起来像公网的」找。每一跳代理都往右追加自己收到
    的对端，所以客户端只能往**左边**塞值；数着位置取就永远取到我们自己的代理写进去的那个。
    早先那版是「从右往左找第一个非内网地址」，对公网使用方是对的，但公司内网的人发一个
    `X-Forwarded-For: 8.8.8.8`，真实地址（10.x）会被当成「我们自己的代理跳」跳过去，
    结果取到他伪造的那个 —— 限流键随便换，`credential_viewed` 记的「谁看的」也随便编。

    默认 2 跳，对应线上的 nginx → oauth2-proxy → 面板：nginx 追加真实来源，
    oauth2-proxy 追加 nginx。跳数不对时退回直连地址（台账里会明显看到一片 127.0.0.1），
    不去猜 —— 猜错的方向是「采信客户端塞的值」。
    """
    if not _is_local(peer):
        return peer or "?"
    hops = hops or _forwarded_hops()
    chain = [x.strip() for x in (forwarded or "").split(",") if x.strip()]
    if hops and len(chain) >= hops:
        # 数着位置取到的就是我们自己的代理写进去的值，**不再判它是不是公网地址** ——
        # 内网员工的真实地址本来就是 10.x，按「是不是公网」筛会把他跳过去。
        #
        # 但「解析得出是个地址」这条还是要留。这跟上面那条不冲突：内网地址照样解得出来。
        # 去掉的话，跳数一旦配错（少一层代理就要手动改成 1），客户端塞的任意文本就会
        # 原样成为限流的桶键 —— 每换一串就是一份新配额，取件的限流整个失效 ——
        # 顺带把伪造内容写进申请单里「谁看过凭证」那条记录。
        hop = chain[-hops]
        with contextlib.suppress(ValueError):
            return str(ipaddress.ip_address(hop.strip().strip("[]").split("%")[0]))
    return peer or "?"


def _forwarded_hops() -> int:
    """面板前面有几层会往 X-Forwarded-For 追加的代理。见 deploy/panel/README.md。"""
    try:
        return max(0, int(os.environ.get(ENV_PROXY_HOPS, "") or _DEFAULT_HOPS))
    except ValueError:
        return _DEFAULT_HOPS


def _pickup_too_many_tries(peer: str) -> bool:
    """整体上限：这个来源五分钟里打了多少次取件接口（成功也算）。**在读请求体之前判**。"""
    now = time.time()
    with _pickup_lock:
        tries = [x for x in _pickup_tries.get(peer, ()) if now - x < _PICKUP_WINDOW]
        tries.append(now)
        _pickup_tries[peer] = tries
        _prune(_pickup_tries, now)
        return len(tries) > _PICKUP_TRIES


def _pickup_over_limit(peer: str, ticket_id: str) -> bool:
    """这个来源对这张单子的失败次数是不是已经满了。**不计数**，只看。"""
    now = time.time()
    with _pickup_lock:
        hits = [x for x in _pickup_hits.get((peer, ticket_id), ()) if now - x < _PICKUP_WINDOW]
        return len(hits) >= _PICKUP_MAX


def _prune(table: dict, now: float) -> None:
    """只丢已经没有有效计数的条目。整张 clear() 的话，拿足够多的来源打一轮
    就能把别人的计数一起清零 —— 限流就形同虚设。"""
    if len(table) <= 4096:
        return
    for k in [k for k, v in table.items() if not v or now - v[-1] >= _PICKUP_WINDOW]:
        table.pop(k, None)
    if len(table) > 4096:  # 全都在窗口内：这时候已经是被打了
        table.pop(next(iter(table)), None)


def _pickup_record_failure(peer: str, ticket_id: str) -> None:
    now = time.time()
    key = (peer, ticket_id)
    with _pickup_lock:
        hits = [x for x in _pickup_hits.get(key, ()) if now - x < _PICKUP_WINDOW]
        hits.append(now)
        _pickup_hits[key] = hits
        _prune(_pickup_hits, now)


def _is_local(addr: str) -> bool:
    """回环或内网地址 —— 这些只可能是我们自己的代理链，不是使用方。

    注意 `is_private` 把 RFC 文档网段（198.51.100.x / 203.0.113.x / 2001:db8::）也算在内。
    真实客户端不会用那些地址，生产上没影响，但写用例时要用真正可路由的地址。
    """
    try:
        ip = ipaddress.ip_address(addr.strip().strip("[]").split("%")[0])
    except ValueError:
        return True  # 解析不出来的一律不当作真实来源
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


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
    """会话与授权中间态。

    会话可以落盘（`path`）：不落盘的话进程一重启所有人都被踢下线，而这个服务
    `Restart=always`、加个启动参数也要重启——上线后这事每天都会发生。落盘的是
    会话 ID（等同于登录凭证），所以只写 0600、只放在本来就 700 的目录里。

    `pending`（授权码流程的 PKCE verifier）**刻意不落盘**：它只活几分钟，重启时
    正在登录的人重试一次即可；而把它写进文件等于把一次性凭证留在盘上。
    """

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else None
        self.pending: dict = {}
        self.sessions: dict = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        known = {f.name for f in fields(FeishuUser)}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("sessions") if isinstance(raw, dict) else None
            for sid, row in (items or {}).items():
                # 逐条容错：一条坏记录不该把其他人一起踢下线
                try:
                    # 只取认识的字段：以后给 FeishuUser 加字段、再回滚到旧版本时，
                    # 旧版本读新文件不会因为多了一个键就把**所有人**的会话丢掉
                    raw_user = row.get("user") or {}
                    user = FeishuUser(**{k: v for k, v in raw_user.items() if k in known})
                    created = float(row.get("created") or 0)
                    if not math.isfinite(created) or created <= 0:
                        continue
                    item = _WebSession(user=user, created=created)
                    if not item.expired:
                        self.sessions[sid] = item
                except Exception as exc:  # noqa: BLE001 — 单条坏记录跳过即可
                    print(f"[store] 跳过一条坏会话记录：{type(exc).__name__}", file=sys.stderr)
                    continue
        except Exception as exc:  # noqa: BLE001 — 构造函数里任何意外都不该让服务起不来
            print(f"[store] 读不了会话文件，忽略：{type(exc).__name__}", file=sys.stderr)
            self.sessions.clear()

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            # 先在锁内拍快照：handler 线程随时可能插入新会话，直接遍历活字典会
            # RuntimeError，而这个异常会穿到登录请求上——人看到 500，其实会话已经建好了
            snapshot = dict(self.sessions)
            payload = {
                "sessions": {
                    sid: {"user": asdict(v.user), "created": v.created}
                    for sid, v in snapshot.items()
                    if not v.expired
                }
            }
            tmp = ""
            try:
                target = self.path
                fd, tmp = tempfile.mkstemp(
                    dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
                )
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                Path(tmp).chmod(0o600)
                Path(tmp).replace(target)
            except Exception as exc:  # noqa: BLE001 — 见下：这里绝不能让异常穿出去
                # 落盘失败不能影响登录本身：内存里的会话仍然有效，只是重启会丢。
                # 必须接住**所有**异常而不只是 OSError：save() 跑在登录请求线程上，而
                # /auth/callback 不在 do_GET 的 try 里 —— 一个 UnicodeEncodeError 就会让
                # 连接直接断开、Set-Cookie 发不出去，那个人此后每次登录都死在同一行。
                with contextlib.suppress(OSError):
                    if tmp:
                        Path(tmp).unlink(missing_ok=True)
                print(f"[store] 会话落盘失败：{type(exc).__name__}", file=sys.stderr)

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
        dropped = False
        for key, v in list(self.sessions.items()):
            if v.expired:
                self.sessions.pop(key, None)
                dropped = True
        if dropped:
            self.save()


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
#: 可以原样回给浏览器的报错开头。**白名单而不是黑名单**：异常里可能带服务器路径和邮箱，
#: 默认一律换成通用文案。只有这几句是我们自己写的、确认不含敏感信息，而且照着它就能修好。
#: 一律通用文案的代价是管理员只看到「请联系管理员查看服务端日志」—— 而他自己就是管理员。
_SAFE_ERRORS = (
    "人员名册还没生成",
    "读不了服务号名单",
)

WEB_DIR = Path(__file__).with_name("web")
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/core.js": ("core.js", "text/javascript; charset=utf-8"),
    "/requests.js": ("requests.js", "text/javascript; charset=utf-8"),
    "/access.js": ("access.js", "text/javascript; charset=utf-8"),
    "/assets.js": ("assets.js", "text/javascript; charset=utf-8"),
    "/permissions.js": ("permissions.js", "text/javascript; charset=utf-8"),
    "/health.js": ("health.js", "text/javascript; charset=utf-8"),
    "/hygiene.js": ("hygiene.js", "text/javascript; charset=utf-8"),
    "/iam.js": ("iam.js", "text/javascript; charset=utf-8"),
    "/storage.js": ("storage.js", "text/javascript; charset=utf-8"),
}
#: 页面只加载同源资源。前端不拼 innerHTML，这条 CSP 是第二道闸。
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
_ADMIN_PEOPLE = "/api/admin/people/"
_ADMIN_REVIEW = "/api/admin/review"
_ADMIN_ASSET_OWNER = "/api/admin/assets/owner"
_ADMIN_REVOKE = "/api/admin/access/revoke"
_ADMIN_POLICY_RULES = "/api/admin/policies/rules"
#: 飞书审批回调。**免登录**（飞书不带用户身份），鉴权靠 Verification Token
_FEISHU_HOOK = "/feishu/approval"
_ADMIN_NUDGE = "/api/admin/nudge"
_ADMIN_IAM = "/api/admin/iam-attributes"
_ADMIN_IAM_FILE = "/api/admin/iam-attributes/file"


_GZIP_MIN = 16 * 1024


def _approval_ready(backend) -> Optional[bool]:
    """审批对象能不能建出来（不发网络请求）；配置本身读坏了交给系统状态页那一项去报。"""
    try:
        return backend.approval() is not None
    except Exception:  # noqa: BLE001
        return None


def _is_requests_path(path: str) -> bool:
    # 规则的读写不是申请单接口，尽管它以 /api/admin/policies/ 开头。
    # 不先排掉的话它会被 _requests 接走，然后因为「没配申请单存储路径」回 503
    if path == _ADMIN_POLICY_RULES:
        return False
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
        iam_spec_path: Optional[str] = None,
        iam_out_path: Optional[str] = None,
        services_path: Optional[str] = None,
        dataset_buckets_path: Optional[str] = None,
        stale_days: int = 0,
        unused_days: int = 0,
        notify: Optional[Callable[[str, dict], None]] = None,
    ):
        self._notify = notify
        #: 给人发提醒用的飞书应用机器人。和申请状态通知是同一个凭证，**不另配**
        self._user_notifier = None
        self._approval_hook = None
        self.base_url = notify_mod.safe_base_url(os.environ.get("DELIVERY_BASE_URL", ""))
        self.services_path = services_path
        self.dataset_buckets_path = dataset_buckets_path
        # 0 = 用 hygiene 的默认值。面板和命令行必须能配成同一套阈值
        # 钳到非负：`--stale-days -5` 会让每把 AK 都算「该换」，标题还印成「超过 -5 天」
        self.stale_days = max(0, stale_days)
        self.unused_days = max(0, unused_days)
        self.iam_spec_path = iam_spec_path
        self.iam_out_path = iam_out_path
        self.assets_path = assets_path
        self.policies_path = policies_path
        self.policy_rules_path = policy_rules_path
        self.tickets_path = tickets_path
        self.templates_path = templates_path
        self.approval_path = approval_path
        self._feishu_token = feishu_token
        self._executor = executor or executor_from_env
        # 凭证发放身份：和开通身份是两把不同的 AK（云上策略分开收窄）。
        # 测试注入自定义 executor 时沿用同一个，免得每个用例都要再造一份
        self._issuer = (
            (lambda platform, account: executor(platform, account))
            if executor
            else (lambda platform, account: executor_from_env(platform, account, issuer=True))
        )
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

    def service_names(self) -> list:
        """服务号清单（体检时这些不算「无主」）。没配就是空清单 —— 只会多报几条，不会漏。"""

        def build():
            from . import hygiene

            return hygiene.load_service_names(self.services_path)

        return self._cached("services", self._stamp(self.services_path), build)

    def registered_buckets(self):
        """已登记的桶（凭证模板 + 数据集白名单）。**读不到返回 None**，体检据此说
        「没法判断」而不是把云上每个桶都报成没登记 —— 两个文件都是 gitignored 的，
        新部署第一次开面板正好是这个状态。"""

        def build():
            from . import hygiene

            return hygiene.load_registered_buckets(self.templates_path, self.dataset_buckets_path)

        return self._cached(
            "registered_buckets",
            (self._stamp(self.templates_path), self._stamp(self.dataset_buckets_path)),
            build,
        )

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

    @property
    def asset_owners_path(self) -> Optional[str]:
        """资源归属表。和资产快照同目录 —— 它们是同一件事的两半：快照说有什么，这张表说是谁的。"""
        if not self.assets_path:
            return None
        return str(Path(self.assets_path).with_name("asset-owners.json"))

    def asset_owners(self) -> dict:
        """归属表。**读坏了就抛**，不降级成空表 —— 空表的表现是「指派过的全都显示未指定」，
        而那正是「表坏了」和「还没指派过」分不出来的那种失败。和本类其余数据同一个原则。"""
        return assets_mod.load_owners(self.asset_owners_path)

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
        # **三个文件一起看修改时间**：模板、旁边的数据类型词表、地域登记表。
        # 只看模板的话，「新增数据类型」审批通过、词表写好了，服务里的缓存却不失效 ——
        # 申请页选不到新类型，查重也还用旧表，而单子上写着「现在能选它了」（审计 M-1）
        from . import datatypes as datatypes_mod
        from . import workspaces as workspaces_mod

        stamp = self._stamp(
            self.templates_path,
            datatypes_mod.beside(self.templates_path),
            workspaces_mod.beside(self.templates_path),
        )
        return self._cached("catalog", stamp, lambda: load_catalog(self.templates_path))

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
                issuer=self._issuer,
                add_manual_link=link,
                # 没配 token 时它内部 Config.from_env() 会抛错，
                # 被 _write_iam_attr 接住记成「他登不进去」—— 不影响建号本身。
                # **实现共用 iam_sync.writer**：原先这段闭包只写在这儿，
                # 定时任务那边构造 Flows 时没传，于是审批通过后 sweep 执行建号，
                # 写属性这一步被静默跳过 —— 线上第一个账号就是这么登不进去的
                write_iam=iam_sync.writer(self.iam_spec_path),
                current_groups=self.current_groups,
                policy_snapshot=self.policies,
                policy_rules=self.policy_rules,
                current_policies=self.current_policies,
                notify=self._notify,
                executor_ready=provision_executor_configured,
                issuer_ready=provision_issuer_configured,
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

    def approval_hook(self):
        """飞书审批回调的校验器。token 从环境变量读，**没配就一律拒绝**。"""
        if self._approval_hook is None:
            codes = []
            with contextlib.suppress(Exception):
                config = ApprovalConfig.load(self.approval_path)
                codes = [config.approval_code] if config is not None else []
            self._approval_hook = hook_mod.Hook(
                verify_token=os.environ.get(hook_mod.ENV_VERIFY_TOKEN, ""),
                codes=codes,
            )
        return self._approval_hook

    def user_notifier(self):
        """私聊某个人用的飞书机器人。没配凭证返回 None —— 调用方据此回 503，
        **不静默跳过**：以为提醒发出去了、其实没发，比报错糟得多。"""
        if self._feishu_token is None or not self.base_url:
            return None
        if self._user_notifier is None:
            self._user_notifier = notify_mod.FeishuNotifier(self._feishu_token, self.base_url)
        return self._user_notifier

    def admin_todo(self, user) -> dict:
        """管理员的待办数。**读缓存，不打外部接口** —— 这个方法在每次拿会话时都会跑。

        `iam_pending` 是「已离职但云登录名还挂着、且没被稍后处理」的人数。
        读不到缓存返回 0 而不是报错：待办数拿不到不该让整个面板登不进去。
        """
        if self.role(user) != ROLE_ADMIN:
            return {}
        paths = self.iam_paths()
        if paths is None:
            return {}
        try:
            cached = iam_sync.cached_reconcile(paths)
        except Exception:  # noqa: BLE001 — 待办数不该让会话失败
            return {}
        if not cached:
            return {}
        held = set()
        try:
            held = set(iam_sync.load_snooze(paths))
        except Exception:  # noqa: BLE001
            held = set()
        pending = sum(
            1
            for e in (cached.get("apps") or [])
            for d in (e.get("drift") or [])
            if d.get("kind") == "inactive" and f"{e.get('app')}/{d.get('union_id')}" not in held
        )
        return {"iam_pending": pending, "checked_at": str(cached.get("checked_at") or "")}

    def iam_paths(self) -> Optional[iam_sync.SyncPaths]:
        """属性表同步要写名册所在目录：路径没配（或过不了写盘守卫）就不开这个功能。"""
        if not (self.people_path and self.iam_spec_path and self.iam_out_path):
            return None
        return iam_sync.SyncPaths(
            people=self.people_path,
            attributes=self.iam_spec_path,
            out=self.iam_out_path,
        )

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
    downloads_dir: str = "",
):
    """proxy 不为空即代理登录模式：只认 oauth2-proxy 注入的请求头，飞书登录路由关闭。"""
    backend = backend or Backend(
        platforms={**platforms_mod.NAMES, **{p.id: p.display for p in registry}}
    )

    def _claim_resources(platform: str, account: str, ids: list, email: str) -> None:
        """资源登记完把实例指给申请人。**开通那一刻是唯一确定主人的时机**，错过只能靠猜。

        名册里查不到这个邮箱就不指：宁可留「未指定」让管理员去补，也不要指给一个
        对不上人的邮箱 —— 归属是拿去问责和算成本的。
        """
        path = backend.asset_owners_path
        if not path or not email:
            return
        hits = [p for p in backend.people().people if p.email.lower() == email.lower()]
        if len(hits) != 1 or hits[0].email_collision:
            return
        for rid in ids:
            assets_mod.set_owner(
                path,
                assets_mod.owner_key(platform, account, rid),
                email=email.lower(),
                name=hits[0].name,
                note="按申请单自动指派",
                actor="system",
            )

    requests_api = RequestsApi(
        backend.flows,
        account_label=lambda platform, account: backend.labels().account(platform, account),
        claim_resources=_claim_resources,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "delivery-dev"

        # ── 工具 ──────────────────────────────────────────────────────────
        def _send(self, code: int, body: bytes, *, ctype="text/html; charset=utf-8", headers=None):
            headers = dict(headers or {})
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 看板会显示账号与权限，不该进任何缓存。JS/CSS 这类不含数据的会传自己的
            # Cache-Control 覆盖它 —— 必须是**覆盖**不是追加：发两个 Cache-Control 的话
            # 浏览器按更严的那个算，协商缓存就白做了
            self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode()
            headers = {}
            # 权限列表这类大响应压缩后小一个数量级。只压 GET：POST 的响应里有现签的凭证、初始密码，
            # 不和压缩放在一起（避免 BREACH 类侧信道）
            if (
                self.command == "GET"
                and len(body) >= _GZIP_MIN
                and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            ):
                body = gzip.compress(body, compresslevel=6)
                headers = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
            self._send(code, body, ctype="application/json; charset=utf-8", headers=headers)

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
        def _pickup(self):
            """用链接里的密钥解开凭证。**刻意不要求登录** —— 凭证常发给外部合作方，
            他们没有面板账号。链接本身就是凭据：密钥是 256 位随机数，服务端不存。

            限流只数**失败**，按 (来源 IP, 申请单号) 分桶：没有登录这道门，剩下唯一能拦
            暴力试密钥的就是它。只数失败是刻意的 —— 「同一个链接能反复打开」是这套设计的
            全部意义，正常使用不该把自己的配额刷光。
            """
            peer = _client_ip(
                self.client_address[0] if self.client_address else "",
                self.headers.get("X-Forwarded-For", ""),
            )
            # 整体上限先判：再往下每一次尝试都会读一遍 tickets.json（还带着文件锁）。
            # **这里提前 return 时请求体还没读**，安全的前提是 protocol_version 保持
            # 默认的 HTTP/1.0（每个响应后关连接）。哪天为了性能改成 HTTP/1.1，
            # 没读完的 body 会被当成下一个请求行去解析 —— 到时候这里要先把 body 读掉再返回
            if _pickup_too_many_tries(peer):
                return self._json(429, {"error": "试得太频繁了，过一会儿再来"})
            flows = backend.flows()
            if flows is None:
                return self._json(503, {"error": "还没有配置申请流程"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode() or "{}")
            except ValueError:
                return self._json(400, {"error": "请求体不是合法 JSON"})
            ticket_id = str(payload.get("id") or "")
            if not _ID.match(ticket_id):
                return self._json(404, {"error": "这个链接打不开任何凭证"})
            if _pickup_over_limit(peer, ticket_id):
                return self._json(429, {"error": "试得太频繁了，过一会儿再来"})
            try:
                # 记一笔谁看的。没有登录态，能记的只有来源 —— 有总比没有强
                _, cred = flows.view_credential(ticket_id, str(payload.get("key") or ""), who=peer)
            except DeliveryError as exc:
                _pickup_record_failure(peer, ticket_id)
                status = getattr(exc, "status", 400)
                first = next((ln for ln in str(exc).splitlines() if ln.strip()), "取件失败")
                return self._json(status, {"error": first})
            except Exception as exc:  # noqa: BLE001 — 细节只进服务端日志
                print(f"[pickup] {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(500, {"error": "签发凭证失败，请联系管理员"})
            return self._json(200, {"credential": cred})

        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if path in _STATIC:
                return self._static(*_STATIC[path])
            # 查看凭证的页面在登录之外：凭证常发给外部合作方，他们没有面板账号
            if path in _PICKUP_STATIC:
                return self._static(*_PICKUP_STATIC[path])
            if path.startswith(_VIEW_PREFIX):
                return self._static("pickup.html", "text/html; charset=utf-8")
            # /download/ 也走 _api：它要登录、要统一的异常兜底，和接口是一类东西
            if path.startswith(("/api/", "/download/")):
                try:
                    return self._api(path)
                except Exception as exc:  # noqa: BLE001 — 任何异常都要回 JSON，不能断连接
                    # 数据文件坏了：明说「不可用」，不降级成空数据。细节只进服务端日志——
                    # 异常里可能有服务器路径、邮箱，不给浏览器。
                    print(f"[api] {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
                    lines = str(exc).splitlines() if isinstance(exc, DeliveryError) else []
                    brief = lines[0] if lines else ""
                    safe = brief if brief.startswith(_SAFE_ERRORS) else ""
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
                if store.sessions.pop(session_id, None) is not None:
                    store.save()
                return self._send(
                    302,
                    b"",
                    headers={"Location": "/", "Set-Cookie": f"{COOKIE_NAME}=; Max-Age=0; Path=/"},
                )
            return self._send(404, _page("404", "<h1>没有这个页面</h1>"))

        # ── 前端与 API ────────────────────────────────────────────────────
        def _static(self, name: str, ctype: str):
            """前端文件。**每次都带按内容算的 ETag**。

            不带缓存头的话浏览器会按自己的启发式缓存 —— 部署完新代码，用户那边还是旧的，
            要人去硬刷新才看得到。那等于没部署，而且没人会记得这一步。

            用内容哈希而不是 mtime：rsync 会保留时间戳，同一份内容重新部署不该让所有人重下；
            而内容真变了时，哈希一定变。`no-cache` 不是「不缓存」，是「每次都回来问一句」，
            没变就是 304，几十字节。
            """
            try:
                body = (WEB_DIR / name).read_bytes()
            except OSError:
                return self._send(500, _page("前端缺失", "<h1>前端文件缺失</h1>"))
            extra = {
                "Content-Security-Policy": _CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
            # **页面本身保持 no-store**：看板上有账号和权限，共用电脑上进了缓存
            # 会被下一个人翻出来。只有 JS/CSS 这类不含数据的走 ETag 协商缓存
            if not ctype.startswith("text/html"):
                etag = '"' + hashlib.blake2s(body, digest_size=16).hexdigest() + '"'
                if self.headers.get("If-None-Match") == etag:
                    return self._send(304, b"", ctype=ctype, headers={"ETag": etag})
                extra["ETag"] = etag
                extra["Cache-Control"] = "no-cache"
            return self._send(200, body, ctype=ctype, headers=extra)

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
                        # 待办数跟着会话走：**每一页都看得见**。
                        # 埋在某个二级页里的待办，等于没有待办
                        "todo": backend.admin_todo(user),
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
                # 名下资源：管理员在资产页逐个指派的那些。放进账号卡片是因为「我在这个云账号里
                # 有哪台机器」和「我在这个云账号里有什么权限」是同一个问题的两半，
                # 分在两页看，人就得自己在脑子里拼
                # 同上：按名册邮箱匹配。found.person 是本 handler 已经解析好的那个人
                # 同上：只认名册邮箱，不回落会话邮箱
                mine_res = _owned_by(backend, found.person.email if found.person else "")
                for card in detail.get("accounts") or []:
                    card["resources"] = mine_res.get((card["platform"], card["account"]), [])
                if backend.role(user) != ROLE_ADMIN:
                    # 采集错误原文可能带接口返回片段；普通用户只需要知道「哪个账号没采全」
                    detail["snapshot_incomplete"] = [
                        line.split("：", 1)[0] + "：本次未采集完整"
                        for line in detail["snapshot_incomplete"]
                    ]
                return self._json(200, detail)
            if path == "/api/access":
                # 「怎么用起来」：每个平台对这个人的下一步动作。
                # 引导文案从 access.guide() 来，和 `delivery login-guide` 同一份逻辑 ——
                # 网页上手写第二份的话，两边迟早对不上，而这正是用户照着做的东西
                session = self._require()
                if session is None:
                    return None
                user = session.user
                found = backend.people().resolve(
                    union_id=user.union_id, enterprise_email=user.enterprise_email
                )
                mine: dict = {}
                for ref in found.person.accounts if found.person else ():
                    mine.setdefault(ref.platform, []).append(ref.name)
                out = []
                for platform in registry:
                    names = mine.get(platform.id, [])
                    got = access_mod.guide(
                        platform,
                        has_account=bool(names),
                        # 凭证托管是 CLI 本机的事，服务端不知道也不该知道 ——
                        # 一律按「还没绑」给引导，多说一次比说错强
                        bound=False,
                        sso_enabled=platform.sso_enabled,
                    ).to_dict()
                    got.update(
                        short=platform.short,
                        console_url=platform.console_url,
                        accounts=sorted(names),
                        notes=list(platform.notes),
                    )
                    out.append(got)
                return self._json(200, {"platforms": out})
            if path == "/api/downloads":
                if self._require() is None:
                    return None
                return self._json(200, {"files": _downloads(downloads_dir)})
            if path.startswith("/download/"):
                # **要登录**：这些是内部工具，而且不登录就能下等于把面板变成公开文件站
                if self._require() is None:
                    return None
                name = path[len("/download/") :]
                item = next(
                    (f for f in _downloads(downloads_dir) if f["name"] == name),
                    None,
                )
                if item is None:
                    return self._send(404, _page("没有这个文件", "<h1>没有这个文件</h1>"))
                # 文件名来自上面那份目录清单，不是请求里的字符串拼出来的
                target = Path(downloads_dir) / item["name"]
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(item["size"]))
                self.send_header("Content-Disposition", f'attachment; filename="{item["name"]}"')
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with target.open("rb") as fh:
                    # 分块写：这些文件十几 MB，整个读进内存没必要
                    for chunk in iter(lambda fh=fh: fh.read(_DOWNLOAD_CHUNK), b""):
                        self.wfile.write(chunk)
                return None
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
                mail = session.user.email
                if not admin:
                    person = backend.people().resolve(union_id=session.user.union_id).person
                    scopes = {(r.platform, r.account) for r in (person.accounts if person else ())}
                    # **只认名册邮箱，取不到就当没有**。会话邮箱在代理（公司 IAM）登录下压根没有，
                    # 飞书登录下可能退化成私人联系邮箱 —— 而企业邮箱是会被回收给新同事的
                    # （people.py 里那条「邮箱对应的账号已绑到另一个飞书身份」就是为它写的）。
                    # 回落到会话邮箱换不来任何可用性：名册里没邮箱的人本来也没法被指派资源
                    mail = person.email if person else ""
                snap_assets = backend.assets()
                view = assets_mod.summary_view(
                    snap_assets,
                    scopes=scopes,
                    labels=labels.account,
                    owners=backend.asset_owners(),
                    viewer_email=mail,
                )
                # 数据集单独一栏，不混进 resources：**它是唯一一类归属自带的资产**
                # （UserId 就是属主），所以员工那边一上来就是满的，不用等管理员指派。
                # `.get` 拿不到就是 None —— 没采到，和「没有数据集」要分得开
                view["datasets"] = assets_mod.datasets_view(
                    (snap_assets or {}).get("datasets"),
                    logins=(
                        None
                        if admin
                        else [
                            r.name
                            for r in (person.accounts if person else ())
                            if r.platform == "aliyun"
                        ]
                    ),
                    labels=labels.account,
                )
                if not admin:
                    # 面板自己发出去的东西归属最确定 —— 申请人就写在单子里，不用查归属表。
                    # 云上采来的资产一条归属都没有时，这是员工资产页上唯一不为空的部分。
                    # 没配申请单文件时 flows() 是 None，那就只是没有这一段，不该 500
                    flows = backend.flows()
                    view["holdings"] = (
                        assets_mod.holdings_view(
                            flows.store.mine(session.user.union_id), labels=labels.account
                        )
                        if flows is not None
                        else []
                    )
                    # 自己的 AK 建了多久、上次什么时候用过。以前这份数据采到了却只有
                    # 命令行看得见，于是持有人根本不知道手上那把密钥有多老 ——
                    # 「该轮换了」的提醒推过去也没有地方可点、可核对
                    view["keys"] = my_keys(
                        person,
                        backend.snapshot(),
                        labels,
                        stale_days=backend.stale_days,
                        unused_days=backend.unused_days,
                    )
                return self._json(200, view)
            if path == "/api/admin/hygiene":
                if self._require(admin=True) is None:
                    return None
                return self._json(200, self._hygiene(backend))
            if path == _ADMIN_POLICY_RULES:
                if self._require(admin=True) is None:
                    return None
                return self._json(
                    200,
                    {
                        "rules": policies_mod.rules_view(backend.policy_rules()),
                        "path": backend.policy_rules_path or "",
                    },
                )
            if path.rstrip("/") == _ADMIN_IAM:
                return self._iam_attributes("GET")
            if path == _ADMIN_IAM_FILE:
                return self._iam_file()
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

        def _hygiene(self, backend) -> dict:
            """体检清单的只读接口。**只算不改**，和命令行 `delivery hygiene` 同一份逻辑。

            默认**不查**飞书在职状态：那是几十个串行 HTTP 请求，挂在页面加载上会让
            管理后台无缘无故卡十几秒。带 `?status=1` 才查（页面上是一个按钮），
            结果按 `_STATUS_TTL` 缓存，免得刷几下页面就把飞书接口打一遍。

            查不到状态时**不判断离职**，由 `hygiene.build` 记进 `skipped` —— 拿不到
            通讯录却照算，等于把全公司报成离职。
            """
            from . import hygiene

            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            want_status = (query.get("status") or ["0"])[0] == "1"

            snapshot = backend.snapshot()
            roster = backend.people().people
            statuses, status_error = None, ""
            asked = missing_uid = 0
            if want_status and app_id and app_secret:
                try:
                    statuses, asked, missing_uid = _employment_statuses(roster, app_id, app_secret)
                except Exception as exc:  # noqa: BLE001
                    # 查询故障 ≠ 这些人离职，**更不该等于整页打不开**。飞书读超时抛的是
                    # TimeoutError、响应不是 JSON 抛的是 ValueError，都不是 DeliveryError，
                    # 只 catch DeliveryError 的话它们会穿到 do_GET 的兜底、渲染成
                    # 「面板数据暂不可用」——而真实原因只是飞书慢了一下。
                    # 照样打日志：这里也可能吃到我们自己的 bug（AttributeError 之类），
                    # 那时只剩页面上一句没人会转述的提示，服务端一点痕迹都不留
                    print(f"[hygiene] 查在职状态失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                    status_error = str(exc) or exc.__class__.__name__

            # 数据集在**资产**快照里（另一个文件）。读不了就传 None，
            # hygiene 会记一笔跳过 —— 传空列表等于断言「一条被遗弃的都没有」
            asset_error = ""
            try:
                snap_assets = backend.assets()
            except DeliveryError as exc:
                # **说清楚是哪一种**：「文件坏了」和「还没采过」在页面上会长得一模一样，
                # 而管理员对这两件事的反应完全不同（去修文件 vs 去跑一次采集）
                snap_assets = None
                asset_error = str(exc).splitlines()[0]
                print(f"[hygiene] 资产快照读不了：{type(exc).__name__}: {exc}", file=sys.stderr)
            _reg = backend.registered_buckets()
            report = hygiene.build(
                snapshot,
                roster,
                statuses=statuses,
                datasets=(snap_assets or {}).get("datasets"),
                buckets=(snap_assets or {}).get("buckets"),
                registered=_reg[0],
                registered_notes=_reg[1],
                services=backend.service_names(),
                stale_days=backend.stale_days or hygiene.STALE_KEY_DAYS,
                unused_days=backend.unused_days or hygiene.UNUSED_KEY_DAYS,
            )
            out = hygiene.view(report)
            out["status_checked"] = statuses is not None
            out["status_asked"] = asked
            out["status_missing_uid"] = missing_uid
            out["status_error"] = status_error
            out["status_available"] = bool(app_id and app_secret)
            out["captured_at"] = snapshot.captured_at if snapshot else ""
            if asset_error:
                out["skipped"] = [f"资产快照读不了：{asset_error}"] + list(out.get("skipped") or [])
            return out

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

        def _iam_attributes(self, method: str):
            """管理后台「IAM 属性表」：预览这次要发什么、导出并存档、IT 导入后确认。

            写的是员工属性表（全员邮箱与云用户名），路径守卫、格式规则和 CLI 完全同一套。
            """
            who = self._require(admin=True)
            if who is None:
                return None
            paths = backend.iam_paths()
            if paths is None:
                return self._json(404, {"error": "服务端没有配置名册或属性表路径"})
            if method == "GET":
                # 超阈值的大批移除要勾选才导出，但得先让管理员看见「要移除谁」。
                # 预览不写任何文件，这个参数只是把被拦下的 remove 行也算出来给人看。
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                allow = (query.get("allow_mass_remove") or [""])[0] == "1"

                def _preview():
                    out = iam_sync.preview(paths, allow_mass_remove=allow)
                    # 上次对账的结果直接给出去 —— 对账要打外部接口、几秒钟，
                    # 做成「点一下才有」就意味着没人点
                    out["reconcile"] = iam_sync.cached_reconcile(paths)
                    out["snoozed"] = sorted(iam_sync.load_snooze(paths))
                    return out

                return self._iam_result(_preview)
            refused = self._same_origin_json()
            if refused:
                return self._json(403, {"error": refused})
            body, sent = self._json_body(allow_empty=True)
            if body is None:
                return sent
            op = str(body.get("op") or "")
            if op == "export":
                allow = body.get("allow_mass_remove") is True
                return self._iam_result(lambda: iam_sync.export(paths, allow_mass_remove=allow))
            if op == "confirm":
                name = str(body.get("name") or "")
                return self._iam_result(lambda: iam_sync.confirm_by_name(paths, name))
            if op == "discard":
                name = str(body.get("name") or "")
                return self._iam_result(lambda: iam_sync.discard(paths, name))
            if op == "reconcile":
                # 只读：不写文件、不改基线、不下发。出去打 IT 的接口，所以可能慢
                return self._iam_result(lambda: iam_sync.reconcile_report(paths))
            if op == "snooze":
                # 稍后处理。**不是忽略** —— 到点它自己回到待办里
                return self._iam_result(
                    lambda: iam_sync.snooze(
                        paths,
                        str(body.get("union_id") or ""),
                        str(body.get("app") or ""),
                        hours=float(body.get("hours") or 3),
                        actor=who.user.union_id,
                    )
                )
            if op == "reclaim":
                # 管理员确认某人离职 → 删他的 cloud_accounts 属性。
                # **服务端重新读一次 IAM 核对**，不信请求里说的「他离职了」
                rp = backend.review_paths()

                def _log(rows):
                    if rp is not None:
                        review_mod.log_iam_reclaim(rp, rows, actor=f"admin:{who.user.union_id}")

                return self._iam_result(
                    lambda: iam_sync.confirm_reclaim(
                        paths,
                        str(body.get("union_id") or ""),
                        str(body.get("app") or ""),
                        actor=who.user.union_id,
                        log=_log,
                    )
                )
            return self._json(
                400,
                {"error": "op 只能是 export、confirm、discard、reconcile、reclaim 或 snooze"},
            )

        def _feishu_approval(self):
            """飞书审批回调。审批人一点同意，这里立刻把对应的单子同步一次。

            **免登录**：飞书不带用户身份，鉴权靠 Verification Token（飞书后台配的那个）。
            没配 token 就拒绝一切事件 —— 一个没有鉴权的公网 POST 入口，
            谁都能拿它触发同步。

            **不做任何开通动作。** 它调的是 `flows.sync`，和定时任务同一条路，
            只是提前触发。两条执行路径迟早会不一致，而不一致那天没人知道跑的是哪条。
            """
            body, sent = self._json_body(allow_empty=True)
            if body is None:
                return sent
            hook = backend.approval_hook()
            # challenge 在验 token 之前：还没配 token 时也要能过地址校验
            got = hook.challenge(body)
            if got is not None:
                return self._json(200, {"challenge": got})
            if not hook.configured:
                print(
                    f"[feishu-hook] 未配置 {hook_mod.ENV_VERIFY_TOKEN}，拒绝事件", file=sys.stderr
                )
                return self._json(403, {"error": "未配置回调校验"})
            if not hook.check_token(body):
                return self._json(403, {"error": "校验失败"})
            if not hook.mine(body):
                # 别人的审批（请假、报销……）。回 200 丢掉 —— 回非 200 飞书会一直重投
                return self._json(200, {"ok": True, "synced": 0})
            instance = hook.instance_of(body)
            if not instance or not hook.claim(instance):
                # 认不出实例号、或者刚处理过（飞书会重投）—— 都回 200，
                # 回非 200 飞书会一直重发
                return self._json(200, {"ok": True, "synced": 0})
            flows = backend.flows()
            if flows is None:
                return self._json(200, {"ok": True, "synced": 0})
            try:
                done = flows.sync_by_instance(instance)
            except Exception as exc:  # noqa: BLE001 — 回调出错不能让飞书一直重投
                print(f"[feishu-hook] 同步失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(200, {"ok": True, "synced": 0})
            return self._json(200, {"ok": True, "synced": len(done)})

        def _nudge(self):
            """管理员提醒某个人处理一件事（该换密钥、密钥没人用……）。

            **面板 + 飞书双发。** 面板上那个标记只对「已经打开了那一页的人」有用，
            而密钥页平时没有任何理由打开 —— 所以光有标记等于没通知。
            """
            who = self._require(admin=True)
            if who is None:
                return None
            refused = self._same_origin_json()
            if refused:
                return self._json(403, {"error": refused})
            body, sent = self._json_body(allow_empty=True)
            if body is None:
                return sent
            if not backend.people_path:
                return self._json(404, {"error": "服务端没有配置名册路径"})
            notifier = backend.user_notifier()
            if notifier is None:
                return self._json(
                    503,
                    {"error": "没有配置飞书应用凭证，发不了提醒"},
                )
            rp = backend.review_paths()

            def _log(row):
                if rp is not None:
                    review_mod.log_event(rp, row)

            try:
                out = nudge_mod.send(
                    notifier,
                    people_path=backend.people_path,
                    union_id=str(body.get("union_id") or ""),
                    topic=str(body.get("topic") or ""),
                    subject=str(body.get("subject") or ""),
                    why=str(body.get("why") or ""),
                    detail=str(body.get("detail") or ""),
                    ref=str(body.get("ref") or ""),
                    base_url=backend.base_url,
                    actor=f"admin:{who.user.union_id}",
                    log=_log,
                    force=body.get("force") is True,
                )
            except DeliveryError as exc:
                return self._json(409, {"error": str(exc).splitlines()[0]})
            except Exception as exc:  # noqa: BLE001 — 不把堆栈回给浏览器
                print(f"[nudge] {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(502, {"error": "提醒没发出去，请查看服务端日志"})
            return self._json(200, out)

        def _iam_result(self, run):
            try:
                return self._json(200, run())
            except DeliveryError as exc:
                # 冲突（没基线、超阈值、存档对不上）都是让人去处理的状态，不是服务器故障
                return self._json(409, {"error": str(exc).splitlines()[0]})
            except Exception as exc:  # noqa: BLE001 — 不把路径和堆栈回给浏览器
                print(f"[iam-attributes] {type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(500, {"error": "属性表操作失败，请查看服务端日志"})

        def _iam_file(self):
            """下载一份属性表 CSV。含员工邮箱：管理员限定，不进日志、不进缓存。"""
            if self._require(admin=True) is None:
                return None
            paths = backend.iam_paths()
            if paths is None:
                return self._json(404, {"error": "服务端没有配置名册或属性表路径"})
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            name = (query.get("name") or [""])[0]
            try:
                real = iam_sync.archive_file(paths, name)
                body = real.read_bytes()
            except DeliveryError as exc:
                return self._json(404, {"error": str(exc).splitlines()[0]})
            except OSError:
                return self._json(404, {"error": "没有这份文件"})
            return self._send(
                200,
                body,
                ctype="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{real.name}"',
                    "X-Content-Type-Options": "nosniff",
                },
            )

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

        def _revoke_access(self):
            """管理员收权：撤掉子账号身上**不是面板发的**那些策略和用户组。

            默认只**预演**（算出会撤什么、拒什么、剩什么），带 `apply` 才真动。
            预演不是可有可无的礼貌 —— 收权最常见的事故不是撤错一条，
            是撤完才发现这个人连活都干不了了，而那时候已经撤完了。
            """
            session = self._require(admin=True)
            if session is None:
                return None
            refused = self._same_origin_json()
            if refused:
                return self._json(403, {"error": refused})
            payload, sent = self._json_body()
            if payload is None:
                return sent

            platform = str(payload.get("platform") or "")
            account = str(payload.get("account") or "")
            user = str(payload.get("user") or "")
            reason = str(payload.get("reason") or "").strip()
            if not (platform and account and user):
                return self._json(400, {"error": "要指明 platform / account / user"})
            wanted = [
                revoke_mod.Item("policy", str(n))
                for n in (payload.get("policies") or [])
                if str(n or "").strip()
            ] + [
                revoke_mod.Item("group", str(n))
                for n in (payload.get("groups") or [])
                if str(n or "").strip()
            ]
            if not wanted:
                return self._json(400, {"error": "没有选中要撤的东西"})
            apply = bool(payload.get("apply"))
            # 真撤必须写清楚为什么：这是唯一一条「减权限」的直接通道，
            # 没有申请单兜着，理由就是事后唯一查得到的东西
            if apply and len(reason) < 5:
                return self._json(400, {"error": "请写明收权理由（至少 5 个字）"})

            try:
                ex = backend._executor(platform, account)
                attached, groups = ex.attached(user)
            except DeliveryError as exc:
                # 过一次脱敏再回前端：ProvisionError 的文案里可能带 AccessKeyId
                return self._json(
                    502, {"error": (provision_describe(exc) or str(exc).splitlines()[0])[:300]}
                )

            # **读不到申请单就整个拒掉。** 「面板发的不能从这里撤」这条护栏全靠它，
            # 读不到却照撤，等于护栏静默消失 —— 而收权是不可逆的
            tickets = self._all_tickets()
            if tickets is None:
                return self._json(
                    503,
                    {"error": "读不了申请单台账，暂时不能收权（否则分不清哪些是面板发的）"},
                )
            if tickets == "missing":
                # 给一条 30 秒的出路，并且让「相信这里真的什么都没发过」成为一个
                # **显式的人工动作** —— 而不是代码替人默认
                return self._json(
                    503,
                    {
                        "error": f"申请单台账 {backend.tickets_path} 不存在。"
                        "如果这是全新部署、确实还没有任何申请单，"
                        '先建一个空台账（{"schema":"wuji-tickets@1","tickets":[]}）再收权'
                    },
                )
            # 每条自定义策略问一次「里面有没有 Deny」。系统策略不问：阿里云的系统策略
            # 都是纯 Allow，而多问 60 次会让预演变慢到没人愿意点
            deny_of = {}
            for p_ in attached:
                name, ptype = str(p_.get("PolicyName") or ""), str(p_.get("PolicyType") or "")
                if name and ptype == "Custom":
                    deny_of[name] = ex.has_deny(ptype, name)
            plan = revoke_mod.plan(
                user=user,
                attached=attached,
                groups=groups,
                wanted=wanted,
                admin_holders=self._admin_holders(platform, account),
                admin_groups=self._admin_groups(platform, account),
                protected_groups=self._protected_groups(platform, account),
                deny_of=deny_of,
                panel_granted=revoke_mod.granted_by_panel(tickets, platform, account, user),
                confirm_last_admin=bool(payload.get("confirm_last_admin")),
            )
            view = {
                "user": plan.user,
                "remove": [
                    {"kind": i.kind, "name": i.name, "type": i.policy_type} for i in plan.remove
                ],
                "refused": [
                    {"kind": i.kind, "name": i.name, "why": plan.why(code), "code": code}
                    for i, code in plan.refused
                ],
                "remaining": plan.remaining,
                "applied": False,
            }
            if not apply:
                return self._json(200, view)

            # **拿不到留痕目的地就不执行**。这条通道没有申请单兜底，review.log 是
            # 事后唯一凭据；而 review_paths() 在「名册审核」那几个路径缺任一时就返回
            # None —— 一个没开名册审核的部署，收权会一条日志都不留，响应里也看不出来
            log_to = backend.review_paths()
            if log_to is None:
                return self._json(
                    503, {"error": "服务端没有配置留痕路径，不能收权（这条通道没有别的凭据）"}
                )
            done, failed = [], []
            for item in plan.remove:
                # **PolicyType 为空不许猜**。猜 "System" 的那一版：阿里会吞
                # EntityNotExist.User.Policy、火山 has_policy 要求类型精确相等直接 return，
                # 两边都记进 done —— 界面说撤了、云上一动没动
                if item.kind == "policy" and not item.policy_type:
                    failed.append({"name": item.name, "error": "云上没返回策略类型，不敢猜，没撤"})
                    continue
                try:
                    if item.kind == "group":
                        ex.remove_from_group(user, item.name)
                    else:
                        ex.detach_policy(user, item.policy_type or "System", item.name)
                    done.append(item.name)
                except Exception as exc:  # noqa: BLE001 — 一条失败不挡住其余，逐条记
                    failed.append(
                        {
                            "name": item.name,
                            "error": provision_describe(exc) or type(exc).__name__,
                        }
                    )
            view["applied"] = True
            view["done"] = done
            view["failed"] = failed
            # **留痕**：这是唯一一条不经申请单的减权通道，没有台账兜着，
            # 事后能查到的只有这一行。写不进去也不让整个操作失败 —— 权限已经撤了，
            # 这时候报错只会让人以为没撤成、再点一次
            review_mod.log_revoke(
                log_to,
                actor=session.user.union_id,
                platform=platform,
                account=account,
                user=user,
                done=done,
                failed=failed,
                reason=reason,
            )
            return self._json(200, view)

        def _admin_holders(self, platform: str, account: str) -> set:
            """这个云账号里还持有管理员策略的登录名。撤最后一个管理员要显式确认，靠它判断。"""
            try:
                snap = backend.snapshot()
            except DeliveryError:
                return set()
            out = set()
            for u in getattr(snap, "users", ()) if snap else ():
                if (u.platform, u.account) != (platform, account):
                    continue
                # **要算经用户组继承的**：火山 `wuji-opration` 组本身就挂着 AdministratorAccess，
                # 只看 u.policies 的话，那个组里的人不算 holders —— 于是明明还有别的管理员
                # 却照样弹「最后一个管理员」。弹多了没人看，真到最后一个时也会被一路点过去
                try:
                    effective = snap.effective_policies(u)
                except Exception:  # noqa: BLE001 — 算不出就退回只看直挂的，宁可多问一次
                    effective = getattr(u, "policies", ())
                if any(str(p) in revoke_mod.ADMIN_POLICIES for p in effective):
                    out.add(u.name)
            return out

        def _admin_groups(self, platform: str, account: str) -> set:
            """本身就发管理员权限的用户组。移出这种组等于撤管理员，同样要确认。

            火山 `wuji-opration` 就是这种：组上直接挂着 AdministratorAccess。
            只看 user.policies 的话，把它的成员移出去是零确认的。
            """
            try:
                snap = backend.snapshot()
            except DeliveryError:
                return set()
            out = set()
            for g in getattr(snap, "groups", ()) if snap else ():
                if (g.platform, g.account) != (platform, account):
                    continue
                if any(str(p) in revoke_mod.ADMIN_POLICIES for p in getattr(g, "policies", ())):
                    out.add(g.name)
            return out

        def _protected_groups(self, platform: str, account: str) -> set:
            """挂着护栏策略（名字命中 `revoke.PROTECTED`）的用户组。

            和 `_admin_groups` 同一个道理：护栏挂在组上时，「把人移出这个组」
            绕过按策略名的判断。
            """
            try:
                snap = backend.snapshot()
            except DeliveryError:
                return set()
            out = set()
            for g in getattr(snap, "groups", ()) if snap else ():
                if (g.platform, g.account) != (platform, account):
                    continue
                if any(
                    str(p).strip().lower().startswith(revoke_mod.PROTECTED)
                    for p in getattr(g, "policies", ())
                ):
                    out.add(g.name)
            return out

        def _all_tickets(self):
            """所有申请单。**读不到返回 None，不是空列表。**

            空列表 = 「面板一条权限都没发过」= 什么都不用拒，方向正好错了：
            一个坏掉的 tickets.json 就能让面板自己发出去的权限被从这里撤掉，
            而界面上没有任何异常。所以读不到时返回 None，由调用方拒绝整个操作。
            """
            # **「不见了」和「坏掉了」同样危险**：TicketStore._read 对不存在的文件
            # 返回空台账（对它自己是对的），而这里空 = 「面板一条都没发过」=
            # 什么都不拒。部署把路径挂错、新建卷、文件被删，护栏都会静默消失
            # 分三态：None=读不了 / "missing"=文件不在 / list=读到了。
            # **「不在」和「读不了」不是一回事**：全新部署在第一张单子之前本来就没有
            # 这个文件，而「清理存量权限」恰恰是全新部署最先要做的事
            path = backend.tickets_path
            if not path:
                return None
            if not Path(path).exists():
                return "missing"
            try:
                flows = backend.flows()
                return list(flows.store.all()) if flows is not None else None
            except Exception:  # noqa: BLE001
                return None

        def _asset_owner(self):
            """管理员把一个资源指给某人（email 留空＝取消指派）。

            资源中心不告诉我们一台机器是谁的，所以归属只能人工记。这里**不做任何推断** ——
            指过的就是指过的，没指过就显示「未指定」。
            """
            session = self._require(admin=True)
            if session is None:
                return None
            refused = self._same_origin_json()
            if refused:
                return self._json(403, {"error": refused})
            path = backend.asset_owners_path
            if not path:
                return self._json(404, {"error": "服务端没有配置资产快照路径"})
            payload, sent = self._json_body()
            if payload is None:
                return sent
            try:
                # 一次可以指一批：ids 是资源 ID 数组，id 是单个（老写法，留着）。
                # 批量存在的理由很实在：43 台计算实例逐条开抽屉要点 43 次，
                # 没人会这么干，于是归属表永远是空的
                raw_ids = payload.get("ids")
                ids = (
                    [str(x) for x in raw_ids if str(x or "").strip()]
                    if isinstance(raw_ids, list)
                    else [str(payload.get("id") or "")]
                )
                ids = [i for i in ids if i]
                if not ids:
                    return self._json(400, {"error": "没有要指派的资源"})
                if len(ids) > _OWNER_BATCH_MAX:
                    return self._json(400, {"error": f"一次最多指派 {_OWNER_BATCH_MAX} 个"})
                keys = [
                    assets_mod.owner_key(payload.get("platform"), payload.get("account"), i)
                    for i in ids
                ]
                key = keys[0]
                email = str(payload.get("email") or "").strip().lower()
                # 指给的人必须在名册里：随手打错一个邮箱，那台机器就永远认不回来了
                name = ""
                if email:
                    hits = [p for p in backend.people().people if p.email.lower() == email]
                    if not hits:
                        return self._json(404, {"error": f"名册里没有 {email}"})
                    if len(hits) > 1 or hits[0].email_collision:
                        # 多人共用一个企业邮箱（名册里有这个标记）。按邮箱指派会指给错的人，
                        # 而资源归属是要拿去问责和算成本的，宁可让管理员先去把名册理清楚
                        return self._json(409, {"error": f"{email} 在名册里对应多个人，先理清名册"})
                    name = hits[0].name
                for one in keys:
                    assets_mod.set_owner(
                        path,
                        one,
                        email=email,
                        name=name,
                        note=str(payload.get("note") or ""),
                        actor=session.user.union_id,
                    )
            except DeliveryError as exc:
                return self._json(getattr(exc, "status", 400), {"error": str(exc)})
            return self._json(
                200, {"ok": True, "key": key, "count": len(keys), "email": email, "name": name}
            )

        def _policy_rules(self):
            """管理员改「哪些权限不能被申请」。

            这是面板最危险的一个写接口：规则决定员工能申请到什么。所以除了
            policies.write_rules 里那三道闸（先校验再落盘、不许放开平台自己的策略、
            allow_custom 接口改不了），这里再加一条 —— **改完立刻重新加载并回读**，
            让管理员当场看到生效后的样子（含内置禁用），而不是「保存成功」四个字。
            """
            session = self._require(admin=True)
            if session is None:
                return None
            refused = self._same_origin_json()
            if refused:
                return self._json(403, {"error": refused})
            if not backend.policy_rules_path:
                return self._json(404, {"error": "服务端没有配置策略规则文件路径"})
            payload, sent = self._json_body()
            if payload is None:
                return sent
            try:
                _, opened = policies_mod.write_rules(
                    backend.policy_rules_path, payload, actor=session.user.union_id
                )
                # 回读也收进 try：规则文件是刚写的，这里再抛说明写出来的东西读不回来
                fresh = policies_mod.rules_view(backend.policy_rules())
            except DeliveryError as exc:
                return self._json(getattr(exc, "status", 400), {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 — do_POST 没有兜底，漏出去就是断连
                # 写盘会抛 PermissionError / OSError（目录归属不对、磁盘满）。不兜的话
                # 异常穿过 do_POST，连接被直接关掉，管理员看到的是「网络错误」
                print(f"[rules] 保存失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                return self._json(500, {"error": "保存失败，请查看服务端日志"})
            if opened:
                # 放开了内置禁用项。台账已经记了，但那是事后翻的 —— 这种事要当场有人知道。
                # 发不出去不影响保存：规则已经落盘了，这里失败只是少一条通知
                _alert_rules_opened(session.user, opened)
            # 不用手动清缓存：_cached 的键带文件 mtime，写完自然失效
            return self._json(200, {"rules": fresh})

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == _FEISHU_HOOK:
                return self._feishu_approval()
            if path == "/api/pickup":
                return self._pickup()
            if path == _ADMIN_POLICY_RULES:
                return self._policy_rules()
            if path == _ADMIN_ASSET_OWNER:
                return self._asset_owner()
            if path == _ADMIN_REVOKE:
                return self._revoke_access()
            if path == _ADMIN_REVIEW:
                return self._review()
            if path.rstrip("/") == _ADMIN_NUDGE:
                return self._nudge()
            if path.rstrip("/") == _ADMIN_IAM:
                return self._iam_attributes("POST")
            if path == _ADMIN_IAM_FILE:
                return self._json(405, {"error": "不支持的方法"})
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
            store.save()
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
            store.save()
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


#: 飞书在职状态的缓存时长。查一次是几十个串行请求，管理员连点几下不该每次都打一遍；
#: 而离职这种事以天计，十分钟的滞后没有任何影响
_STATUS_TTL = 600.0
_status_cache: dict = {"key": None, "at": 0.0, "value": None}
_status_lock = threading.Lock()


def _employment_statuses(roster, app_id: str, app_secret: str) -> tuple:
    """名册里每个人的飞书在职状态。返回 `({union_id: 状态 或 None}, 查了几个, 没 union_id 的几个)`。

    **后两个数不是装饰**：只有绑过 union_id 的人查得到（没登录过面板的人名册里就没有），
    而「查了 12 个」和「查了 60 个」得出的「没发现离职」完全不是一回事。不把这两个数
    交出去，页面就会拿一句「已查过在职状态」盖住「其实一多半人根本没查」。

    缓存 10 分钟：离职这种事以天计，而查一次是几十个串行请求。
    """
    from .identity import directory

    uids = tuple(sorted({p.union_id for p in roster if p.union_id}))
    missing = sum(1 for p in roster if not p.union_id)
    # 键带上 app_id：换了飞书应用就是另一套可见范围，拿旧结果等于拿别人的答案
    ckey = (app_id, uids)
    with _status_lock:
        if _status_cache["key"] == ckey and time.time() - _status_cache["at"] < _STATUS_TTL:
            return dict(_status_cache["value"]), len(uids), missing
    # **锁外发请求**：几十个串行 HTTP，占着锁会把并发的管理员请求一起卡住
    value = directory.status_of(uids, app_id, app_secret)
    with _status_lock:
        _status_cache.update(key=ckey, at=time.time(), value=value)
    return dict(value), len(uids), missing


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
    iam_spec_path: Optional[str] = None,
    iam_out_path: Optional[str] = None,
    services_path: Optional[str] = None,
    dataset_buckets_path: Optional[str] = None,
    stale_days: int = 0,
    unused_days: int = 0,
    sessions_path: Optional[str] = None,
    downloads_path: Optional[str] = None,
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
        platforms={**platforms_mod.NAMES, **{p.id: p.display for p in registry}},
        proposal_path=proposal_path,
        manual_path=manual_path,
        tickets_path=tickets_path,
        templates_path=templates_path,
        approval_path=approval_path,
        assets_path=assets_path,
        policies_path=policies_path,
        policy_rules_path=policy_rules_path,
        iam_spec_path=iam_spec_path,
        iam_out_path=iam_out_path,
        services_path=services_path,
        dataset_buckets_path=dataset_buckets_path,
        stale_days=stale_days,
        unused_days=unused_days,
        feishu_token=token,
        notify=notify,
    )
    handler = make_handler(
        registry,
        Store(sessions_path),
        app_id=app_id,
        app_secret=app_secret,
        base_url=base_url,
        backend=backend,
        proxy=proxy,
        downloads_dir=downloads_path or os.environ.get("DELIVERY_DOWNLOADS", ""),
    )
    # 只绑回环：这是开发服务器，绑 0.0.0.0 会把还没做访问控制的看板暴露给整个网段。
    server = http.server.ThreadingHTTPServer((host, port), handler)
    echo(f"  工具下载    {downloads_path or '未配置（--downloads）'}")
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
