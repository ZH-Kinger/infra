"""CLI 用公司 IAM 登录：OAuth 2.0 设备码授权（RFC 8628）。

面板挂在 oauth2-proxy 后面时，CLI 拿不到浏览器的登录 Cookie。这里不让面板自己发令牌，而是：

  1. CLI 向 IAM 要一个设备码，终端里显示验证码和链接；
  2. 员工在浏览器里用公司 IAM 登录并确认；
  3. CLI 轮询拿到 IAM 签发的令牌，之后每个请求带 ``Authorization: Bearer <access_token>``；
  4. oauth2-proxy（--skip-jwt-bearer-tokens）校验签名、签发方和 audience，按和浏览器登录相同的
     emailClaim / additionalClaims 把员工身份头注入给面板。

带 access_token 而不是 id_token：两者在 Authentik 里都是同一签发方、同一 audience 的 JWT，
oauth2-proxy 都认；但面板首次关联邮箱要拿它去 IAM userinfo 查，userinfo 只认 access_token。
令牌里必须有 feishu_union_id（wuji scope）：没有的话代理不注入 X-Panel-Union-Id，
面板按未登录处理。
续期要 offline_access scope，IAM 那边也要给这个客户端分配 offline_access 映射，
否则不发 refresh_token。

所以面板的信任模型不变：只认 oauth2-proxy 注入的身份头。本模块**不校验 JWT 签名**
（校验在 oauth2-proxy），只读 exp 判断什么时候该用 refresh_token 续期、读 name 给人看。

令牌只存在 ``~/.w0/session.json``（0600，session.py 负责），不进日志、不进异常消息。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import DeliveryError

GRANT_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_SCOPE = "openid profile email wuji offline_access"
_TIMEOUT = 15
#: id_token 剩余不到这么久就先续期，免得请求走到一半过期
REFRESH_MARGIN = 120

#: (method, url, form) -> (status, json)
Transport = Callable[[str, str, Optional[dict]], tuple]


class IamLoginError(DeliveryError):
    """IAM 设备码登录失败。"""


def _http(method: str, url: str, form: Optional[dict]) -> tuple:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise IamLoginError(f"连不上公司 IAM：{type(exc).__name__}") from None


def _https(url: object, what: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    local = parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
    if not (parsed.scheme == "https" or local) or not parsed.netloc:
        raise IamLoginError(f"{what} 必须是 https 地址")
    return str(url)


@dataclass(frozen=True)
class Endpoints:
    issuer: str
    device_authorization: str
    token: str


def discover(issuer: str, *, transport: Transport = _http) -> Endpoints:
    issuer = _https(issuer, "IAM 签发方地址").rstrip("/")
    status, doc = transport("GET", f"{issuer}/.well-known/openid-configuration", None)
    if status != 200 or not isinstance(doc, dict):
        raise IamLoginError(f"读不了 IAM 的 OIDC 配置（HTTP {status}）")
    # 签发方必须和配置的一致：防止被指到别处的发现文档
    if str(doc.get("issuer") or "").rstrip("/") != issuer:
        raise IamLoginError("IAM 的 OIDC 配置里 issuer 和配置的不一致，拒绝")
    device = doc.get("device_authorization_endpoint")
    if not device:
        raise IamLoginError(
            "IAM 没有开启设备码登录（发现文档里缺 device_authorization_endpoint），请联系 IT"
        )
    return Endpoints(
        issuer=issuer,
        device_authorization=_https(device, "设备码接口"),
        token=_https(doc.get("token_endpoint"), "令牌接口"),
    )


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


def start(
    endpoints: Endpoints,
    client_id: str,
    *,
    scope: str = DEFAULT_SCOPE,
    transport: Transport = _http,
) -> DeviceCode:
    status, body = transport(
        "POST", endpoints.device_authorization, {"client_id": client_id, "scope": scope}
    )
    body = body if isinstance(body, dict) else {}
    if status != 200 or not body.get("device_code") or not body.get("user_code"):
        raise IamLoginError(f"IAM 拒绝发放设备码：{_oauth_error(body, status)}")
    uri = _https(body.get("verification_uri") or body.get("verification_url"), "验证地址")
    complete = body.get("verification_uri_complete") or ""
    return DeviceCode(
        device_code=str(body["device_code"]),
        user_code=str(body["user_code"]),
        verification_uri=uri,
        verification_uri_complete=_https(complete, "验证地址") if complete else "",
        interval=max(1, _int(body.get("interval"), 5)),
        expires_in=max(1, _int(body.get("expires_in"), 600)),
    )


@dataclass(frozen=True)
class Tokens:
    #: 发给面板的 Bearer 令牌：优先 access_token（JWT），IAM 发的是不透明令牌时退回 id_token
    token: str
    refresh_token: str
    expires_ts: float
    claims: dict


def poll(
    endpoints: Endpoints,
    client_id: str,
    code: DeviceCode,
    *,
    transport: Transport = _http,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> Tokens:
    """按 RFC 8628 轮询：pending 继续等，slow_down 间隔加 5 秒，过期 / 拒绝直接报错。"""
    interval = code.interval
    deadline = clock() + code.expires_in
    while clock() < deadline:
        sleep(interval)
        status, body = transport(
            "POST",
            endpoints.token,
            {"grant_type": GRANT_DEVICE, "device_code": code.device_code, "client_id": client_id},
        )
        body = body if isinstance(body, dict) else {}
        if status == 200:
            return _tokens(body, clock)
        error = str(body.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise IamLoginError("在浏览器里拒绝了这次登录")
        # 部分 Authentik 版本对过期的设备码回 invalid_grant 而不是 expired_token
        if error in ("expired_token", "invalid_grant"):
            break
        raise IamLoginError(f"IAM 登录失败：{_oauth_error(body, status)}")
    raise IamLoginError("验证码已过期，请重新 delivery login --iam")


def refresh(
    endpoints_token: str,
    client_id: str,
    refresh_token: str,
    *,
    transport: Transport = _http,
    clock: Callable[[], float] = time.time,
) -> Tokens:
    if not refresh_token:
        raise IamLoginError("登录已过期，请重新 delivery login --iam")
    status, body = transport(
        "POST",
        _https(endpoints_token, "令牌接口"),
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
    )
    body = body if isinstance(body, dict) else {}
    if status in (400, 401) and body.get("error") in ("invalid_grant", "invalid_token", ""):
        raise IamLoginError("登录已过期，请重新 delivery login --iam")
    if status != 200:
        raise IamLoginError(f"续期失败：{_oauth_error(body, status)}，稍后再试")
    tokens = _tokens(body, clock)
    # 有的 IAM 续期时不回新的 refresh_token：沿用旧的
    return (
        tokens
        if tokens.refresh_token
        else Tokens(tokens.token, refresh_token, tokens.expires_ts, tokens.claims)
    )


def _tokens(body: dict, clock: Callable[[], float]) -> Tokens:
    access = str(body.get("access_token") or "")
    id_token = str(body.get("id_token") or "")
    token = access if access.count(".") == 2 else id_token
    if not token:
        raise IamLoginError("IAM 没有返回可用的令牌（申请的 scope 里要有 openid）")
    claims = jwt_claims(token)
    exp = claims.get("exp")
    expires_ts = (
        float(exp)
        if isinstance(exp, (int, float))
        else clock() + float(body.get("expires_in") or 300)
    )
    return Tokens(token, str(body.get("refresh_token") or ""), expires_ts, claims)


def jwt_claims(token: str) -> dict:
    """只解出 JWT 的 payload 读 exp / name，不校验签名（校验在 oauth2-proxy）。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise IamLoginError("IAM 返回的令牌格式不对")
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise IamLoginError("IAM 返回的令牌解不开") from None
    if not isinstance(claims, dict):
        raise IamLoginError("IAM 返回的令牌格式不对")
    return claims


def _int(value: object, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _oauth_error(body: object, status: int) -> str:
    if isinstance(body, dict):
        text = str(body.get("error_description") or body.get("error") or "")
        if text:
            return text[:120]
    return f"HTTP {status}"
