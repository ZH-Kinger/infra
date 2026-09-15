"""火山引擎 OpenAPI 调用（签名 V4），零依赖。

火山没有阿里那种好用的官方 CLI，SDK 又是个装了一堆东西的巨型包（`volcengine-python-sdk`
一个 wheel 里塞了全部产品线）。仓库硬规 `dependencies = []`，所以自己签。

V4 与阿里 v1 的差异只在形式，坑在细节：
  · 规范请求里的 query 必须按 key 排序、且用 `-_.~` 之外全转义（和 v1 的 safe 集不同）
  · 签名域是 `<date>/<region>/<service>/request`，**结尾是 `request` 不是 `aws4_request`**
  · 派生密钥四层 HMAC，起点直接是 secret 本身（没有 `AWS4` 前缀）
这三条只要错一条就是 403，而 403 又和「权限不足」长得一样——所以有测试逐条锁住。

**凭证只从环境变量读，绝不落盘、绝不进日志、绝不进异常消息。**
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from ..errors import DeliveryError

HOST = "open.volcengineapi.com"
IAM = ("iam", "2018-01-01")
DEFAULT_REGION = "cn-beijing"
_TIMEOUT = 25
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_DENIED = ("accessdenied", "nopermission", "unauthorized", "forbidden", "invalidaccesskey")


class VolcanoError(DeliveryError):
    """火山接口返回错误。"""


class VolcanoDenied(VolcanoError):
    """权限不足。"""


@dataclass(frozen=True)
class Credentials:
    access_key_id: str
    secret_access_key: str

    @classmethod
    def from_env(cls, prefix: str = "VOLCANO") -> Credentials:
        ak = os.environ.get(f"{prefix}_ACCESS_KEY", "") or os.environ.get("TOS_ACCESS_KEY", "")
        sk = os.environ.get(f"{prefix}_SECRET_KEY", "") or os.environ.get("TOS_SECRET_KEY", "")
        if not ak or not sk:
            raise VolcanoError(
                f"缺少 {prefix}_ACCESS_KEY / {prefix}_SECRET_KEY"
                f"（或 TOS_ACCESS_KEY / TOS_SECRET_KEY）"
            )
        return cls(ak, sk)

    def __repr__(self) -> str:
        tail = self.access_key_id[-4:] if len(self.access_key_id) >= 4 else "?"
        return f"Credentials(ak=…{tail}, sk=<hidden>)"


Transport = Callable[[str, dict], tuple]


def _http(url: str, headers: dict) -> tuple:
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw[:200]}
    except urllib.error.URLError as exc:
        raise VolcanoError(f"连不上火山：{exc.reason}") from exc


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def canonical_query(params: dict) -> str:
    return "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(params[k]), safe='-_.~')}"
        for k in sorted(params)
    )


def sign(*, params: dict, secret: str, region: str, service: str, xdate: str) -> str:
    datestamp = xdate[:8]
    headers = {"host": HOST, "x-content-sha256": _EMPTY_SHA256, "x-date": xdate}
    signed = "host;x-content-sha256;x-date"
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in signed.split(";"))
    canon_req = "\n".join(
        ["GET", "/", canonical_query(params), canon_headers, signed, _EMPTY_SHA256]
    )
    scope = f"{datestamp}/{region}/{service}/request"
    to_sign = "\n".join(
        ["HMAC-SHA256", xdate, scope, hashlib.sha256(canon_req.encode()).hexdigest()]
    )
    key = _hmac(_hmac(_hmac(_hmac(secret.encode(), datestamp), region), service), "request")
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def call(
    service: str,
    version: str,
    action: str,
    params: Optional[dict] = None,
    *,
    creds: Credentials,
    region: str = DEFAULT_REGION,
    transport: Optional[Transport] = None,
) -> dict:
    send = transport or _http
    query = dict(params or {})
    query.update({"Action": action, "Version": version})
    xdate = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    signature = sign(
        params=query,
        secret=creds.secret_access_key,
        region=region,
        service=service,
        xdate=xdate,
    )
    scope = f"{xdate[:8]}/{region}/{service}/request"
    headers = {
        "host": HOST,
        "x-date": xdate,
        "x-content-sha256": _EMPTY_SHA256,
        "Authorization": (
            f"HMAC-SHA256 Credential={creds.access_key_id}/{scope}, "
            f"SignedHeaders=host;x-content-sha256;x-date, Signature={signature}"
        ),
    }
    status, body = send(f"https://{HOST}/?{canonical_query(query)}", headers)
    if status == 200 and "Result" in body:
        return body["Result"]
    err = (body.get("ResponseMetadata") or {}).get("Error") or {}
    code, message = str(err.get("Code") or ""), str(err.get("Message") or "")
    blob = f"{code} {message}".lower()
    if any(m in blob for m in _DENIED):
        raise VolcanoDenied(
            f"`{action}` 被拒（{code}）：{message[:200]}\n"
            f"当前凭证缺 `{action}` 的权限。这不是「没有数据」——采集已中断。"
        )
    raise VolcanoError(f"`{action}` 失败 HTTP {status}：{code} {message[:200]}")


def paginate(
    service: str,
    version: str,
    action: str,
    *,
    key: str,
    params: Optional[dict] = None,
    creds: Credentials,
    region: str = DEFAULT_REGION,
    transport: Optional[Transport] = None,
    limit: int = 100,
    max_pages: int = 200,
) -> list:
    """按 Offset/Limit 翻页。

    `key` 缺失时**抛错而不是当空**——火山不同接口的列表键名不一致
    （`ListUsers` 回 `UserMetadata`、`ListUsersForGroup` 回 `Users`），
    猜错键名会静默得到「0 条」。实测踩过：把 6 个用户组全报成 0 人。
    """
    out, offset, pages = [], 0, 0
    while pages < max_pages:
        pages += 1
        args = dict(params or {})
        args.update({"Limit": limit, "Offset": offset})
        result = call(
            service,
            version,
            action,
            args,
            creds=creds,
            region=region,
            transport=transport,
        )
        batch = result.get(key)
        if batch is None:
            raise VolcanoError(
                f"`{action}` 的响应缺 `{key}`，不能当作空列表；实际键：{sorted(result)}"
            )
        out += batch
        if len(batch) < limit:
            return out
        offset += limit
    raise VolcanoError(f"`{action}` 翻页超过 {max_pages} 页，疑似死循环，已中断")
