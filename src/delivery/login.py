"""`delivery login`：飞书 OAuth 授权码 + PKCE + 本地回调。

流程
────
  CLI 起一个只活几十秒的本机回调服务（127.0.0.1:8765）
    → 打开（或打印）飞书授权页
    → 用户点同意，飞书带 code 跳回 127.0.0.1
    → CLI 把 code + code_verifier 交给**我们自己的后端**
    → 后端持 client_secret 去飞书换 user_access_token，回一个我们自己的会话令牌

为什么换 token 必须在后端
────────────────────────
飞书 v2 的 token 接口**即使走 PKCE 也强制要 `client_secret`**（实测文档）。把应用密钥
塞进分发出去的 CLI，等于谁拿到 CLI 谁就有了整个应用的身份。所以 CLI 只负责拿 code，
换 token 由后端做。

那 PKCE 还有什么用
──────────────────
code 会出现在 `127.0.0.1` 的 URL 里，也可能进 shell 历史或代理日志。PKCE 把 code 绑死
在**本次 CLI 进程**生成的随机串上：没有 `code_verifier`，捡到 code 也换不走。

安全细节（每条都有对应测试）
────────────────────────────
· 回调服务只绑 **127.0.0.1**，绝不绑 0.0.0.0——后者会让同网段任何人打到回调口。
· `state` 随机生成并**逐字校验**，不匹配直接拒绝（防 CSRF/注入他人 code）。
· 回调只接受一次，处理完立刻关端口。
· code 与 code_verifier 从不打印、不进日志。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import json
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import DeliveryError
from .session import Session, save_session

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
DEFAULT_PORT = 8765
# 飞书对重定向 URL 做**完全匹配**，而 `localhost` 与 `127.0.0.1` 是两个不同的字符串：
# 注册了一个、发的是另一个，照样报 20029。飞书文档举例用的是 localhost，所以默认跟它一致。
DEFAULT_HOST = "localhost"
DEFAULT_TIMEOUT = 180
_HTTP_TIMEOUT = 15

# RFC 7636：code_verifier 取值 43-128 位的 [A-Za-z0-9-._~]
_VERIFIER_BYTES = 48


class LoginError(DeliveryError):
    """登录未完成。"""


def _pkce_pair() -> tuple:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(_VERIFIER_BYTES)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


#: 授权时显式申请的用户侧权限，空格分隔。
#:
#: 飞书的用户授权是**累积**且**显式**的：`authorize` 不带 `scope`，用户就不会授予任何
#: 可选权限，`user_info` 照样 200、照样返回 open_id 和姓名——只是 `enterprise_email`
#: 恒为空。而企业邮箱是「飞书身份 → 云账号」整条映射的连接键，它一空，登录看起来
#: 完全成功，映射却全断。
#:
#: `open_id` / `union_id` / `name` 本身零权限即得，这里申请的是它们之外的字段。
#: 注意授权端点**只校验 scope 名字是否存在，不校验本应用有没有申请到**
#: （实测：`bitable:app` 这种明显没申请的也照样放行），所以这串里的每一条
#: 都必须和开发者后台实际申请的保持一致，否则错要到登录那一刻才暴露。
#:
#: 见 https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/user_info/get
USER_SCOPES = (
    "contact:user.employee:readonly",  # 企业邮箱 enterprise_email —— 映射的连接键
    "contact:user.email:readonly",  # 个人邮箱 email，企业邮箱缺失时的退路
    "offline_access",  # 没有它就拿不到 refresh_token，用户会反复重新授权
)
DEFAULT_SCOPE = " ".join(USER_SCOPES)


def authorize_url(
    *,
    app_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    scope: str = DEFAULT_SCOPE,
) -> str:
    params = {
        "client_id": app_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    # quote_via=quote：scope 是空格分隔的，默认的 quote_plus 会转成 `+`。
    # 多数服务端两者都认，但 %20 无歧义，不值得赌。
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"{AUTHORIZE_URL}?{query}"


@dataclass
class _Captured:
    code: str = ""
    state: str = ""
    error: str = ""


_PAGE_OK = (
    "<html><body style='font-family:sans-serif;padding:3em'>"
    "<h2>登录成功</h2><p>可以关掉这个页面，回到终端。</p></body></html>"
)
_PAGE_BAD = (
    "<html><body style='font-family:sans-serif;padding:3em'>"
    "<h2>登录失败</h2><p>请回到终端查看原因。</p></body></html>"
)


def _make_handler(captured: _Captured, path: str, done: threading.Event):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  (BaseHTTPRequestHandler 的约定命名)
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != path:
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            captured.code = (query.get("code") or [""])[0]
            captured.state = (query.get("state") or [""])[0]
            captured.error = (query.get("error_description") or query.get("error") or [""])[0]
            body = (_PAGE_BAD if captured.error or not captured.code else _PAGE_OK).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args):
            """默认实现会把**含 code 的完整 URL**打到 stderr。必须闭嘴。"""

    return Handler


class _LoopbackServer(http.server.HTTPServer):
    """按解析结果决定 IPv4/IPv6。

    `localhost` 在一些系统上解析成 `::1`，而只绑 `127.0.0.1` 的服务收不到那次回调——
    表现是浏览器显示「无法连接」、CLI 一直等到超时，很难猜到是协议族的问题。
    """

    address_family = socket.AF_INET


def _loopback_family(host: str) -> int:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return socket.AF_INET
    families = {info[0] for info in infos}
    # 两者都有时优先 IPv4：浏览器对 localhost 的选择不确定，而 IPv4 兼容性更好。
    return socket.AF_INET if socket.AF_INET in families else socket.AF_INET6


def wait_for_callback(*, port: int, path: str, timeout: int, host: str = DEFAULT_HOST) -> _Captured:
    captured = _Captured()
    done = threading.Event()
    family = _loopback_family(host)
    bind = "127.0.0.1" if family == socket.AF_INET else "::1"
    # 只绑回环地址：绑 0.0.0.0 会让同网段的人能打到这个回调口。
    _LoopbackServer.address_family = family
    server = _LoopbackServer((bind, port), _make_handler(captured, path, done))
    server.timeout = 1
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
    )
    thread.start()
    try:
        if not done.wait(timeout):
            raise LoginError(f"等待浏览器授权超时（{timeout} 秒）")
    finally:
        server.shutdown()
        server.server_close()
    return captured


class BackendExchange:
    """把 code 交给我们自己的后端换会话。后端持 client_secret。"""

    _ALLOWED_SCHEMES = ("http", "https")

    def __init__(self, server: str):
        parsed = urllib.parse.urlsplit(server)
        if parsed.scheme not in self._ALLOWED_SCHEMES or not parsed.netloc:
            raise LoginError(f"后端地址必须是 http(s):// 开头的完整地址，当前是 {server!r}")
        self.server = server.rstrip("/")

    def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> dict:
        payload = json.dumps(
            {"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri}
        ).encode()
        # scheme 已在 __init__ 收窄为 http(s)
        req = urllib.request.Request(  # noqa: S310
            f"{self.server}/auth/exchange",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            # 后端可能回 JSON 错误体，也可能是网关的 HTML 页。取不到就只报状态码——
            # 状态码已经够定位，没必要为了错误体再抛一层异常。
            detail = ""
            with contextlib.suppress(ValueError, OSError):
                detail = (json.loads(exc.read().decode() or "{}") or {}).get("error", "")
            raise LoginError(
                f"后端换取会话失败（HTTP {exc.code}）{'：' + detail if detail else ''}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LoginError(f"连不上后端 {self.server}：{exc.reason}") from exc


def login(
    exchanger,
    *,
    app_id: str,
    server: str,
    port: int = DEFAULT_PORT,
    callback_path: str = "/callback",
    redirect_uri: str = "",
    host: str = DEFAULT_HOST,
    timeout: int = DEFAULT_TIMEOUT,
    open_browser: bool = True,
    echo: Callable[[str], None] = print,
    waiter: Optional[Callable] = None,
    opener: Callable[[str], bool] = webbrowser.open,
) -> Session:
    if not app_id:
        raise LoginError("缺少飞书 App ID：用 --app-id 或设置 DELIVERY_FEISHU_APP_ID")
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    # 允许直接粘贴飞书白名单里那一条：完全匹配下，任何一个字符不同都是 20029。
    if redirect_uri:
        parsed = urllib.parse.urlsplit(redirect_uri)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LoginError(f"--redirect-uri 不是合法地址：{redirect_uri!r}")
        host = parsed.hostname
        port = parsed.port or port
        callback_path = parsed.path or "/callback"
    else:
        redirect_uri = f"http://{host}:{port}{callback_path}"
    url = authorize_url(app_id=app_id, redirect_uri=redirect_uri, state=state, challenge=challenge)

    opened = False
    if open_browser:
        try:
            opened = bool(opener(url))
        except Exception:
            opened = False
    echo("")
    if opened:
        echo("  已在浏览器中打开飞书授权页。如果没弹出来，手动访问：")
    else:
        echo("  请在浏览器中打开下面的地址完成授权：")
    echo("")
    echo(f"  {url}")
    echo("")
    echo(f"  回调地址 {redirect_uri} 必须**逐字**出现在飞书后台的「重定向 URL」里。")
    echo("  报 20029 就是这条对不上，或者 app_id 与配置该 URL 的应用不是同一个。")
    echo("  等待授权…")

    wait = waiter or wait_for_callback
    captured = wait(port=port, path=callback_path, timeout=timeout, host=host)

    if captured.error:
        raise LoginError(f"飞书返回授权失败：{captured.error}")
    # 逐字校验 state：不校验的话，别人可以把自己的 code 塞进你的回调，
    # 让你的 CLI 拿到**他的**身份（会话混淆）。
    if not secrets.compare_digest(captured.state, state):
        raise LoginError("回调的 state 与本次请求不符，已拒绝（可能是伪造的回调）")
    if not captured.code:
        raise LoginError("回调里没有授权码")

    data = exchanger.exchange(code=captured.code, verifier=verifier, redirect_uri=redirect_uri)
    token = str(data.get("token") or "")
    union_id = str(data.get("union_id") or "")
    if not token or not union_id:
        raise LoginError("后端返回的会话不完整（缺 token 或 union_id）")
    import time as _time

    session = Session(
        union_id=union_id,
        name=str(data.get("name") or ""),
        token=token,
        expires_ts=_time.time() + float(data.get("expires_in") or 86400),
        server=server,
    )
    save_session(session)
    return session
