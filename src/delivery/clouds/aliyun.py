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

_TIMEOUT = 25

#: 鉴权类错误的识别片段。见 identity/collect.py 里同样的取舍：
#: 宁可把权限问题误判成普通错误（照样中断），也不要把瞬时错误当成权限问题。
_DENIED = (
    "nopermission",
    "forbidden.ram",
    "accessdenied",
    "has no permission",
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
) -> list:
    """翻页取列表。

    `max_pages` 是防御性的：`IsTruncated` 为真但 `Marker` 不变时会无限循环，
    真机上见过（见 identity/collect.py 里同样的守卫）。宁可少取也不要挂死。
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
