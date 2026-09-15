"""公司 IAM 登录：面板挂在 oauth2-proxy 后面，从代理注入的请求头认人。

为什么不自己写 OIDC
────────────────────
授权码 + PKCE + state + nonce + ID Token 验签，自己写容易漏一条。仓库硬规零第三方依赖，
标准库又没有 RSA，验签只能省掉。oauth2-proxy 是成熟的开源件，这些都做了，
面板只剩「信任代理传来的身份」这一件事。

信任边界
────────
代理和面板之间约定一个共享密钥，代理每个请求都带上（X-Panel-Proxy-Secret）。
请求头里没有这个密钥、或者对不上，就当没登录：说明请求绕过了代理直接打到面板。
代理会**覆盖**客户端自带的同名请求头（injectRequestHeaders 默认不保留原值），
所以浏览器伪造 X-Panel-Union-Id 没有用。

认人
────
只认 `feishu_union_id`（WUJI IAM 接入规范）。IAM 没返回它，代理那一步就拒绝登录
（oauth2-proxy 的 emailClaim 指向 feishu_union_id，缺了建不了会话）。

邮箱
────
oauth2-proxy 的 email 字段已经拿来放 union_id（公司有员工没有邮箱，按邮箱建会话会把
他们挡在外面），真实邮箱传不过来。名册首次关联要用企业邮箱，所以**只在名册里还没有这个
union_id 时**，拿代理转来的 access token 去 IAM 的 userinfo 查一次。

IAM 里的邮箱不一定是公司分配、用户改不了的（飞书模式下是），所以收紧：
  · userinfo 返回的 feishu_union_id 必须和请求头一致（证明 token 是本人的）
  · email_verified 必须是 true
  · 域名必须在 DELIVERY_IAM_EMAIL_DOMAINS 里
  · 邮箱只用于名册首次关联，**不用于判断管理员**（代理模式下管理员只认 union_id）
查不到或不合格只是不能自动关联，不影响登录。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .errors import DeliveryError
from .feishu import FeishuUser

AUTH_FEISHU = "feishu"
AUTH_PROXY = "proxy"
AUTH_MODES = (AUTH_FEISHU, AUTH_PROXY)

ENV_AUTH = "DELIVERY_AUTH"
ENV_SECRET = "DELIVERY_PROXY_SECRET"  # noqa: S105  环境变量名，不是口令
ENV_SECRET_FILE = "DELIVERY_PROXY_SECRET_FILE"  # noqa: S105
ENV_USERINFO = "DELIVERY_IAM_USERINFO_URL"
ENV_LOGOUT = "DELIVERY_LOGOUT_URL"
ENV_EMAIL_DOMAINS = "DELIVERY_IAM_EMAIL_DOMAINS"

H_SECRET = "X-Panel-Proxy-Secret"  # noqa: S105  请求头名
H_UNION_ID = "X-Panel-Union-Id"
H_NAME = "X-Panel-Name"
H_TOKEN = "X-Panel-Access-Token"  # noqa: S105
H_FEISHU_USER_ID = "X-Panel-Feishu-User-Id"

LOGIN_URL = "/oauth2/start?rd=%2F"
DEFAULT_LOGOUT_URL = "/oauth2/sign_out"

_MIN_SECRET = 32
_UNION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_EMAIL_TTL = 300
_MISS_TTL = 60
_CACHE_MAX = 1000
_TIMEOUT = 5
_MAX_BODY = 64 * 1024


class ProxyAuthError(DeliveryError):
    """代理登录配置不对。"""


def _is_loopback_url(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _safe_path(value: str) -> bool:
    """和前端 safePath 同一套规则：站内路径，不能是 //host 或带反斜杠。"""
    return value.startswith("/") and not value.startswith("//") and "\\" not in value


@dataclass(frozen=True)
class ProxyAuthConfig:
    secret: str
    userinfo_url: str = ""
    logout_url: str = DEFAULT_LOGOUT_URL
    email_domains: tuple = ()

    def __post_init__(self):
        if len(self.secret) < _MIN_SECRET:
            raise ProxyAuthError(
                f"{ENV_SECRET} 至少 {_MIN_SECRET} 个字符。生成：python -c "
                '"import secrets; print(secrets.token_urlsafe(32))"'
            )
        # 请求头按 latin-1 解码，非 ASCII 或带空白的密钥永远对不上，只会表现成「一直未登录」
        if not self.secret.isascii() or self.secret != self.secret.strip() or " " in self.secret:
            raise ProxyAuthError(f"{ENV_SECRET} 只能是不含空白的 ASCII 字符")
        if self.userinfo_url and not self.email_domains:
            raise ProxyAuthError(
                f"设置了 {ENV_USERINFO} 就要设置 {ENV_EMAIL_DOMAINS}（如 wuji.tech），"
                "只接受公司域名的邮箱做名册关联"
            )
        if self.userinfo_url:
            scheme = urllib.parse.urlsplit(self.userinfo_url).scheme
            # access token 要发过去，明文 HTTP 只允许本机（测试用的模拟 IAM）
            if scheme != "https" and not (scheme == "http" and _is_loopback_url(self.userinfo_url)):
                raise ProxyAuthError(f"{ENV_USERINFO} 必须是 https 地址")
        if not _safe_path(self.logout_url):
            raise ProxyAuthError(
                f"{ENV_LOGOUT} 必须是站内路径，例如 /oauth2/sign_out?rd=<URL 编码的 IAM 退出地址>"
            )

    @classmethod
    def from_env(cls, environ: Optional[Mapping] = None) -> ProxyAuthConfig:
        env = os.environ if environ is None else environ
        secret = env.get(ENV_SECRET, "")
        secret_file = env.get(ENV_SECRET_FILE, "")
        if secret and secret_file:
            raise ProxyAuthError(f"{ENV_SECRET} 和 {ENV_SECRET_FILE} 只能设一个")
        if secret_file:
            try:
                secret = Path(secret_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ProxyAuthError(f"读不了 {ENV_SECRET_FILE}：{exc}") from exc
        if not secret:
            raise ProxyAuthError(f"代理登录模式要设置 {ENV_SECRET} 或 {ENV_SECRET_FILE}")
        return cls(
            secret=secret,
            userinfo_url=env.get(ENV_USERINFO, ""),
            logout_url=env.get(ENV_LOGOUT, "") or DEFAULT_LOGOUT_URL,
            email_domains=tuple(
                d.strip().lower().lstrip("@")
                for d in env.get(ENV_EMAIL_DOMAINS, "").split(",")
                if d.strip()
            ),
        )


def _header_text(value: str) -> str:
    """http.server 按 latin-1 解请求头，代理发的是 UTF-8 原始字节，中文名要还原。"""
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib 跟随重定向时会把 Authorization 原样带到新地址（不管主机和协议），一律不跟。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(
            req.full_url, code, "userinfo 返回重定向，拒绝跟随", headers, fp
        )


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fetch_userinfo(url: str, token: str) -> dict:
    # url 来自部署配置并在 ProxyAuthConfig 里校验过协议
    req = urllib.request.Request(  # noqa: S310
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    with _OPENER.open(req, timeout=_TIMEOUT) as resp:
        raw = resp.read(_MAX_BODY + 1)
    if len(raw) > _MAX_BODY:
        raise ValueError("userinfo 响应过大")
    data = json.loads(raw.decode() or "{}")
    if not isinstance(data, dict):
        raise ValueError("userinfo 不是 JSON 对象")
    return data


class ProxyIdentity:
    """从一次请求的请求头里取出登录者。"""

    def __init__(
        self,
        config: ProxyAuthConfig,
        *,
        fetch: Callable[[str, str], dict] = _fetch_userinfo,
        clock: Callable[[], float] = time.time,
    ):
        self.config = config
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._emails: dict = {}

    @property
    def login_url(self) -> str:
        return LOGIN_URL

    @property
    def logout_url(self) -> str:
        return self.config.logout_url

    def user(self, headers: Mapping) -> Optional[FeishuUser]:
        given = headers.get(H_SECRET) or ""
        if not hmac.compare_digest(given.encode(), self.config.secret.encode()):
            return None
        union_id = (headers.get(H_UNION_ID) or "").strip()
        if not _UNION_ID.match(union_id):
            return None
        name = _header_text((headers.get(H_NAME) or "").strip())
        user_id = (headers.get(H_FEISHU_USER_ID) or "").strip()
        if not _UNION_ID.match(user_id):
            user_id = ""  # 只用于以本人身份发起飞书审批，格式不对就当没有
        # 不带邮箱：管理员只认 union_id；名册关联需要邮箱时由调用方显式调 email()
        return FeishuUser(open_id="", union_id=union_id, name=name, user_id=user_id)

    def email(self, headers: Mapping, union_id: str) -> str:
        """名册首次关联用的企业邮箱。只在名册里查不到这个 union_id 时调用。"""
        token = (headers.get(H_TOKEN) or "").strip()
        if not token or not self.config.userinfo_url:
            return ""
        return self._email(token, union_id)

    def _accept(self, data: Mapping, union_id: str) -> str:
        if data.get("feishu_union_id") != union_id:
            # 不打值：只说明哪一项不满足
            print("[proxy-auth] userinfo 的 feishu_union_id 缺失或与登录者不一致", file=sys.stderr)
            return ""
        if data.get("email_verified") is not True:
            print("[proxy-auth] userinfo 的 email_verified 不是 true，不用于关联", file=sys.stderr)
            return ""
        email = data.get("email")
        if not isinstance(email, str) or email.count("@") != 1:
            return ""
        email = email.strip().lower()
        if email.rsplit("@", 1)[1] not in self.config.email_domains:
            print("[proxy-auth] userinfo 邮箱不在允许的公司域名内", file=sys.stderr)
            return ""
        return email

    def _email(self, token: str, union_id: str) -> str:
        # 缓存键里带上 union_id：同一个 token 配别的 union_id 不能命中缓存
        key = hashlib.sha256(f"{union_id}\0{token}".encode()).hexdigest()
        now = self._clock()
        with self._lock:
            hit = self._emails.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        try:
            data = self._fetch(self.config.userinfo_url, token)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            # 只打异常类型：错误里可能带 token 或返回片段
            print(f"[proxy-auth] userinfo 查询失败：{type(exc).__name__}", file=sys.stderr)
            email, ttl = "", _MISS_TTL
        else:
            email, ttl = self._accept(data, union_id), _EMAIL_TTL
        with self._lock:
            if len(self._emails) >= _CACHE_MAX:
                for stale in [k for k, v in self._emails.items() if v[0] <= now]:
                    self._emails.pop(stale, None)
                if len(self._emails) >= _CACHE_MAX:
                    self._emails.clear()
            self._emails[key] = (now + ttl, email)
        return email
