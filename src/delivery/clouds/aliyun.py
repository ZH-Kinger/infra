"""阿里云 RPC 接口调用（签名 v1.0），零依赖。

为什么自己签名而不是调 `aliyun` CLI
────────────────────────────────
`identity/collect.py` 走的是 subprocess 调 CLI，那条路依赖运行环境装了 CLI 并配好
profile。采集器要在 CI、容器、别人的机器上跑，多一个外部前置就多一种「在我这好好的」。
签名本身四十行标准库，比维护「CLI 装没装、profile 配没配、版本对不对」便宜。

**凭证只从环境变量读，绝不落盘、绝不进日志、绝不进异常消息。**
下面每个错误分支都只回阿里云给的 Code/Message，不回请求串——请求串里带签名。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..errors import DeliveryError

RAM = ("ram.aliyuncs.com", "2015-05-01")
IMS = ("ims.aliyuncs.com", "2019-08-15")
STS = ("sts.aliyuncs.com", "2015-04-01")
#: 资源管理。策略授权要从这里列：RAM 的 ListPoliciesForUser 只返回账号级授权，
#: 授在资源组上的查不到（2026-09-15 主账号实测 6 条）。
RESOURCE_MANAGER = ("resourcemanager.aliyuncs.com", "2020-03-31")


def ecs(region: str) -> tuple:
    """ECS 的地域化 endpoint。**必须按地域打** —— 打错地域的表现不是报错，
    是「查不到这台机器」，而那和「机器没建成」长得一样。"""
    return (f"ecs.{region}.aliyuncs.com", "2014-05-26")


_TIMEOUT = 25

#: 鉴权类错误的识别片段。见 identity/collect.py 里同样的取舍：
#: 宁可把权限问题误判成普通错误（照样中断），也不要把瞬时错误当成权限问题。
_DENIED = (
    "nopermission",
    "forbidden.ram",
    "accessdenied",
    # **不是 "has no permission"**：PAI 的工作空间 RBAC 回的是
    #   `100700008 No permission: denied by RAM and AIWorkspace Rbac PaiDataset:ListDatasets`
    # 没有 "has"，所以原来那条匹配不上 —— 而且它的 HTTP 状态码是 **404 不是 403**，
    # 于是「没权限看这个工作空间」会被当成普通错误、在地区层记进 skipped，
    # 和「这个地区没开通」长得一模一样。台账因此会少掉一整个工作空间而看起来是完整的。
    "no permission",
    "not authorized",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
)


class AliyunError(DeliveryError):
    """阿里云接口返回错误。`code` 是阿里云的错误码，用于区分「不存在」和「出错了」。"""

    def __init__(self, message: str, *, code: str = ""):
        super().__init__(message)
        self.code = code


class AliyunDenied(AliyunError):
    """权限不足。单独成类——它和「没有数据」必须能分开处置。"""


@dataclass(frozen=True)
class Credentials:
    access_key_id: str
    access_key_secret: str
    #: STS 换来的临时凭证要带它。长期 AK 留空。
    #: 缺了它的话临时凭证签名过得去、但阿里云一律回 InvalidSecurityToken
    security_token: str = ""

    @classmethod
    def from_env(cls, prefix: str = "ALIYUN") -> Credentials:
        ak = os.environ.get(f"{prefix}_ACCESS_KEY_ID", "")
        sk = os.environ.get(f"{prefix}_ACCESS_KEY_SECRET", "")
        if not ak or not sk:
            raise AliyunError(
                f"缺少 {prefix}_ACCESS_KEY_ID / {prefix}_ACCESS_KEY_SECRET。"
                f"采集器只从环境变量取凭证，不读配置文件、不落盘。"
            )
        return cls(ak, sk)

    def __repr__(self) -> str:  # 防止误打日志时把 secret 带出去
        tail = self.access_key_id[-4:] if len(self.access_key_id) >= 4 else "?"
        return f"Credentials(ak=…{tail}, sk=<hidden>)"


Transport = Callable[[str], tuple]


def _http(url: str) -> tuple:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"Message": raw[:200]}
    except urllib.error.URLError as exc:
        raise AliyunError(f"连不上阿里云：{exc.reason}") from exc


def _quote(value) -> str:
    return urllib.parse.quote(str(value), safe="~")


def sign(params: dict, secret: str) -> str:
    """RPC 签名 v1.0。canonical 串按 key 排序，HMAC-SHA1，密钥要加尾随 `&`。"""
    canonical = "&".join(f"{_quote(k)}={_quote(params[k])}" for k in sorted(params))
    to_sign = f"GET&{_quote('/')}&{_quote(canonical)}"
    digest = hmac.new((secret + "&").encode(), to_sign.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _scrub(message: str) -> str:
    """去掉阿里云错误消息里回显的待签名串（SignatureDoesNotMatch 会带上 AccessKeyId）。"""
    low = message.lower()
    cut = low.find("string to sign")
    if cut >= 0:
        message = message[:cut] + "<已省略待签名串>"
    return re.sub(r"(AccessKeyId(?:%3D|=))[^&%\s]+", r"\1<hidden>", message)


def call(
    endpoint: str,
    version: str,
    action: str,
    params: Optional[dict] = None,
    *,
    creds: Credentials,
    transport: Optional[Transport] = None,
) -> dict:
    send = transport or _http
    payload = dict(params or {})
    if creds.security_token:
        payload["SecurityToken"] = creds.security_token
    payload.update(
        {
            "Format": "JSON",
            "Version": version,
            "AccessKeyId": creds.access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Action": action,
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    )
    signature = sign(payload, creds.access_key_secret)
    canonical = "&".join(f"{_quote(k)}={_quote(payload[k])}" for k in sorted(payload))
    url = f"https://{endpoint}/?Signature={_quote(signature)}&{canonical}"
    status, body = send(url)
    if status == 200:
        return body
    code = str(body.get("Code") or "")
    message = _scrub(str(body.get("Message") or ""))
    blob = f"{code} {message}".lower()
    if any(m in blob for m in _DENIED):
        raise AliyunDenied(
            f"`{action}` 被拒（{code}）：{message[:200]}\n"
            f"当前凭证缺 `{action}` 的权限。这不是「没有数据」——采集已中断。",
            code=code,
        )
    raise AliyunError(f"`{action}` 失败 HTTP {status}：{code} {message[:200]}", code=code)


#: ROA 风格的接口（PAI 的 AIWorkSpace 就是）。和上面的 RPC 完全是两套签名：
#: RPC 把参数排序拼进 query 再签；ROA 签的是「方法 + 几个固定头 + x-acs-* 头 + 路径和 query」。
#: 混用的结果是 `SignatureDoesNotMatch`，而错误信息里不会告诉你是风格用错了。
AIWORKSPACE = "2021-02-04"


def _http_roa(url: str, headers: dict, method: str = "GET", body: bytes = b"") -> tuple:
    request = urllib.request.Request(  # noqa: S310
        url, data=body or None, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"Message": raw[:200]}
    except urllib.error.URLError as exc:
        raise AliyunError(f"连不上阿里云：{exc.reason}") from exc


def call_roa(
    endpoint: str,
    version: str,
    path: str,
    query: Optional[dict] = None,
    *,
    creds: Credentials,
    transport=None,
    method: str = "GET",
    body: Optional[dict] = None,
) -> dict:
    """ROA（ACS 1.0）签名的只读调用。

    `transport` 收 `(url, headers)`，比 RPC 那个多一个参数。用例里把假 transport 写成
    `(url, headers=None)` 就能同时喂给两种风格 —— 一次采集里两种都会用到。

    待签名串的空行是 Content-MD5 和 Content-Type —— GET 没有正文，两个都空，
    **但那两个换行不能省**，少一个就签不过。query 进签名串时用原始值、不 URL 编码，
    而真正发出去的 URL 要编码，两边写法不同是对的。
    """
    send = transport or _http_roa
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body else b""
    url, headers, query = _roa_sign(
        endpoint,
        version,
        path,
        query,
        method,
        raw,
        "application/json; charset=utf-8" if raw else "",
        creds,
    )
    status, reply = send(url, headers, method, raw)
    return _roa_result(status, reply, path)


def _roa_sign(
    endpoint, version, path, query, method, raw, ctype, creds, accept="application/json"
) -> tuple:
    """ROA（ACS 1.0）的签名。**JSON 和 XML 两条路共用这一份** ——
    待签名串少一行就是 SignatureDoesNotMatch，而错误信息不会说是哪一行对不上，
    所以这段绝不能有第二份。"""
    query = {str(k): str(v) for k, v in (query or {}).items() if v is not None}
    stamp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    # 有正文时，Content-MD5 和 Content-Type 都要进签名串（GET 那两行是空的）
    md5 = base64.b64encode(hashlib.md5(raw).digest()).decode() if raw else ""  # noqa: S324
    headers = {
        "accept": accept,
        "date": stamp,
        "host": endpoint,
        "x-acs-signature-nonce": uuid.uuid4().hex,
        "x-acs-signature-method": "HMAC-SHA1",
        "x-acs-signature-version": "1.0",
        "x-acs-version": version,
    }
    if creds.security_token:
        headers["x-acs-security-token"] = creds.security_token
    signed = sorted(k for k in headers if k.startswith("x-acs-"))
    acs = "".join(f"{k}:{headers[k]}\n" for k in signed)
    pairs = "&".join(f"{k}={query[k]}" if query[k] != "" else k for k in sorted(query))
    resource = path + (f"?{pairs}" if pairs else "")
    to_sign = f"{method}\n{headers['accept']}\n{md5}\n{ctype}\n{stamp}\n{acs}{resource}"
    digest = hmac.new(creds.access_key_secret.encode(), to_sign.encode(), hashlib.sha1).digest()
    headers["authorization"] = f"acs {creds.access_key_id}:{base64.b64encode(digest).decode()}"

    if md5:
        headers["content-md5"] = md5
        headers["content-type"] = ctype
    url = f"https://{endpoint}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url, headers, query


def _roa_result(status, reply, path):
    """**method 一律显式传下去。** 之前调用处在没有正文时调 `send(url, headers)`，
    而那个默认是 GET —— 于是签名按 DELETE 算、请求按 GET 发，回 SignatureDoesNotMatch，
    而错误信息只会说签名不对，不会说是方法不一致。"""
    if status in (200, 201):
        return reply
    code = str(reply.get("Code") or "")
    message = _scrub(str(reply.get("Message") or ""))
    if any(m in f"{code} {message}".lower() for m in _DENIED):
        raise AliyunDenied(
            f"`{path}` 被拒（{code}）：{message[:200]}\n"
            f"当前凭证缺这个接口的权限。这不是「没有数据」——采集已中断。",
            code=code,
        )
    raise AliyunError(f"`{path}` 失败 HTTP {status}：{code} {message[:200]}", code=code)


def assume_role(
    role_arn: str,
    session: str,
    *,
    creds: Credentials,
    seconds: int = 900,
    transport: Optional[Transport] = None,
) -> Credentials:
    """换一份临时凭证。资产采集要进资源目录的成员账号时用。

    **只用来进那些单独建的只读角色**，不要拿它去 assume 资源目录自带的
    `ResourceDirectoryAccountAccessRole` —— 那个角色挂的是 AdministratorAccess，
    而采集凭证是长期挂在面板服务器上的。谁能 assume 到什么，就等于那台机器
    被拿下之后对方能拿到什么。范围限制写在 collector 身上的 RAM 策略里。

    `session` 只能是字母数字和 `.@-_`，而且**至少 2 个字符** —— 短了阿里云回
    `InvalidParameter.RoleSessionName`，报错里看不出是长度问题。
    """
    body = call(
        STS[0],
        STS[1],
        "AssumeRole",
        {"RoleArn": role_arn, "RoleSessionName": session, "DurationSeconds": str(seconds)},
        creds=creds,
        transport=transport,
    )
    got = body.get("Credentials") or {}
    ak, sk = str(got.get("AccessKeyId") or ""), str(got.get("AccessKeySecret") or "")
    token = str(got.get("SecurityToken") or "")
    if not ak or not sk or not token:
        raise AliyunError(f"AssumeRole 没返回完整凭证：{role_arn}")
    return Credentials(ak, sk, token)


def paginate(
    endpoint: str,
    version: str,
    action: str,
    *,
    key: str,
    container: str,
    params: Optional[dict] = None,
    creds: Credentials,
    transport: Optional[Transport] = None,
    max_pages: int = 200,
    strict_key: bool = False,
) -> list:
    """翻页取列表。

    `max_pages` 是防御性的：`IsTruncated` 为真但 `Marker` 不变时会无限循环，
    真机上见过（见 identity/collect.py 里同样的守卫）。宁可少取也不要挂死。

    `strict_key` 给「拿不到就会说错话」的调用方用：容器**非空却没有那个键**时抛错，
    而不是当成空列表。默认关着 —— 多数调用方拿到空列表只是少显示几行，而这一类
    调用方（比如回收站）拿到空列表会让下游**正面断言**「这个人认不出来了」。
    容器本身是空字典时照旧当成「确实没有」，那是正常状态。
    """
    out, marker, pages = [], "", 0
    while pages < max_pages:
        pages += 1
        args = dict(params or {})
        args["MaxItems"] = 100
        if marker:
            args["Marker"] = marker
        body = call(endpoint, version, action, args, creds=creds, transport=transport)
        node = body.get(container)
        if not isinstance(node, dict):
            raise AliyunError(
                f"`{action}` 的响应缺 `{container}`，不能当作空结果：{str(body)[:200]}"
            )
        if strict_key and node and key not in node:
            raise AliyunError(
                f"`{action}` 的响应里 `{container}` 非空却没有 `{key}`，"
                f"接口结构可能变了，不能当作空结果：{str(body)[:200]}"
            )
        out += node.get(key) or []
        if not body.get("IsTruncated"):
            return out
        nxt = str(body.get("Marker") or "")
        if not nxt or nxt == marker:
            # IsTruncated 为真却给不出新 Marker：只拿到了一部分。静默返回会让组成员、
            # 用户列表少人，看板上表现为「这人没权限」。
            raise AliyunError(f"`{action}` 声明还有下一页但没有新的 Marker，数据不完整，已中断")
        marker = nxt
    raise AliyunError(f"`{action}` 翻页超过 {max_pages} 页，疑似死循环，已中断")
