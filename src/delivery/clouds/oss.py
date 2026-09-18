"""OSS 的最小客户端：列对象、放一个占位目录、搬目录。

**和 RAM/PAI 又是另一套签名**（OSS V1：`Authorization: OSS <ak>:<sig>`，HMAC-SHA1）。
面板里现在有三套了 —— RPC（ram/sts）、ROA（PAI）、这一套。混用的报错都是
`SignatureDoesNotMatch`，而错误信息不会告诉你是风格用错了。

签名串里那个 CanonicalizedResource 有个坑，实测踩过：**只有真正的「子资源」
（`?acl`、`?uploads`、`?location` 这些）进签名串，`prefix` / `delimiter` /
`max-keys` / `list-type` 这些列举参数不进。** 带上它们必然 403，而 OSS 的错误响应里
会回显它期望的 StringToSign —— 对不上的时候先看那个，别猜。

只做只读 + 建占位目录 + 搬目录。**没有删桶、没有改 ACL、没有生命周期**：
这个模块会拿着一把能写生产桶的凭证跑，能力越少越好。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from ..errors import DeliveryError
from .aliyun import Credentials

_TIMEOUT = 30
#: 进签名串的子资源。列举参数（prefix/delimiter/max-keys/list-type…）**不在其列**
_SUBRESOURCES = frozenset(
    {
        "acl",
        "append",
        "cors",
        "delete",
        "lifecycle",
        "location",
        "objectMeta",
        "partNumber",
        "position",
        "restore",
        "symlink",
        "tagging",
        "uploadId",
        "uploads",
        # **翻页 token 也是子资源。** 漏了它第一页正常、第二页 403，而错误码是
        # SignatureDoesNotMatch —— 很容易被读成「缺权限」。官方帮助文档那份
        # 清单末尾带「等」字、没列全，以 oss2 SDK 的 `_subresource_key_set` 为准
        "continuation-token",
        "sequential",
    }
)
#: 响应 XML 的长度上限。1000 个 key 的列举响应通常在几百 KB 以内
_MAX_XML = 8 * 1024 * 1024
#: 一次最多列多少页。翻页不完就抛错，不返回半份 —— 半份清单会让调用方以为「就这些」
_MAX_PAGES = 200


class OssError(DeliveryError):
    """OSS 调用失败。`code` 是 OSS 的错误码（`AccessDenied`、`NoSuchBucket`……）。

    **基类不收 `code=`**，得自己定 `__init__` —— 不定的话
    `OssError(msg, code=...)` 会抛 `takes no keyword arguments`，
    把真正的错误信息整个吃掉，排错时只看得到这句 TypeError。
    """

    def __init__(self, message: str, *, code: str = ""):
        super().__init__(message)
        self.code = code


class OssDenied(OssError):
    """凭证没有这个权限。**不是「没有数据」**。"""


def _http(url: str, method: str, headers: dict, body: bytes) -> tuple:
    request = urllib.request.Request(  # noqa: S310
        url, data=body or None, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise OssError(f"连不上 OSS：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        # 读阶段超时抛的是**裸 TimeoutError**，不是 URLError —— 不接住的话它会穿透
        # 调用方的 `except DeliveryError`，让整个批量循环在半路崩掉
        raise OssError(f"读 OSS 响应中断：{type(exc).__name__}: {exc}") from exc


def _error(status: int, raw: bytes, where: str) -> OssError:
    text = raw.decode(errors="replace")
    # **只回显 Code / Message / RequestId 三个字段，不回显原始正文。**
    # OSS 的 SignatureDoesNotMatch 正文字段顺序是
    # Code → Message → RequestId → HostId → **OSSAccessKeyId** → SignatureProvided
    # → StringToSign —— 所以「按 <StringToSign> 截断」挡不住完整的 AccessKeyId，
    # 它排在前面。与其一个个想着排除，不如只把要的三个拿出来。
    code = msg = rid = ""
    with contextlib.suppress(ET.ParseError, OssError):
        node = _parse(text)
        code = node.findtext("Code") or ""
        msg = node.findtext("Message") or ""
        rid = node.findtext("RequestId") or ""
    brief = " ".join(x for x in (msg[:200], f"(RequestId {rid})" if rid else "") if x)
    if not brief:
        brief = f"响应不是可解析的 OSS 错误（{len(text)} 字节，已省略）"
    if status == 403 or code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
        return OssDenied(f"{where} 被拒（{code or status}）：{brief}", code=code)
    return OssError(f"{where} 失败 HTTP {status}（{code}）：{brief}", code=code)


def call(
    method: str,
    bucket: str,
    key: str = "",
    *,
    region: str,
    query: Optional[dict] = None,
    body: bytes = b"",
    content_type: str = "",
    extra: Optional[dict] = None,
    creds: Credentials,
    transport=None,
    service: bool = False,
) -> bytes:
    send = transport or _http
    query = {str(k): str(v) for k, v in (query or {}).items()}
    # **服务级请求要显式说，不从「桶名是空的」推断出来。**
    # 推断过一版，漏得很难看：守卫写成「桶名空且带 key 就抛」，可 list_prefixes /
    # list_objects 的前缀走的是 query、`key` 恒为空 —— 于是 `list_prefixes("", "wzh/")`
    # 照样发出一个合法的 GET Service，拿回 ListAllMyBucketsResult，找不到 CommonPrefixes，
    # **安静返回 []**，被上层读成「这个桶里一个目录都没有」，而那正是判断「谁换了组、
    # 有没有多余目录」的依据。根子在于「调用方少填一个桶名」和「我要列所有桶」
    # 在类型上长得一模一样 —— 那就别让它们长得一样
    if service:
        if bucket or key:
            raise OssError("服务级请求不能带桶名或对象名")
    elif not bucket:
        raise OssError("桶名不能为空")
    # `bucket` 留空 = 服务级请求（ListBuckets）：主机名不带桶名，签名串里的
    # CanonicalizedResource 是光秃秃的 `/`。拼成 `.oss-cn-hangzhou.aliyuncs.com`
    # 或者 `//` 都只会换来 SignatureDoesNotMatch，而那个报错不会告诉你差在哪
    host = f"{region}.aliyuncs.com" if service else f"{bucket}.{region}.aliyuncs.com"
    stamp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode() if body else ""  # noqa: S324

    sub = sorted((k, v) for k, v in query.items() if k in _SUBRESOURCES)
    tail = "&".join(f"{k}={v}" if v else k for k, v in sub)
    resource = ("/" if service else f"/{bucket}/{key}") + (f"?{tail}" if tail else "")
    # `x-oss-` 开头的头**必须按字典序进签名串**（`名:值\n`，名小写）。
    # 服务端复制靠的就是 `x-oss-copy-source` 这个头 —— 漏掉它就是 SignatureDoesNotMatch，
    # 而错误信息只会说签名不对，不会说少签了哪个头
    oss_headers = {str(k).lower(): str(v) for k, v in (extra or {}).items()}
    # 临时凭证的 token 也是 `x-oss-` 头，**必须在算签名之前放进去**。
    # 放到签名之后（原来就是这样）的话，任何 STS 凭证都会签名失败
    if creds.security_token:
        oss_headers["x-oss-security-token"] = creds.security_token
    canon = "".join(
        f"{k}:{oss_headers[k]}\n" for k in sorted(oss_headers) if k.startswith("x-oss-")
    )
    to_sign = f"{method}\n{md5}\n{content_type}\n{stamp}\n{canon}{resource}"
    sig = base64.b64encode(
        hmac.new(creds.access_key_secret.encode(), to_sign.encode(), hashlib.sha1).digest()  # noqa: S324
    ).decode()

    headers = {
        "Date": stamp,
        "Host": host,
        "Authorization": f"OSS {creds.access_key_id}:{sig}",
    }
    if md5:
        headers["Content-MD5"] = md5
    if content_type:
        headers["Content-Type"] = content_type
    headers.update(oss_headers)

    url = f"https://{host}/{urllib.parse.quote(key)}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    status, raw = send(url, method, headers, body)
    if status in (200, 204):
        return raw
    raise _error(status, raw, f"{method} {bucket}/{key or ''}")


def _parse(text: str):
    """解析 OSS 的 XML 响应。

    用标准库的 ElementTree（这个仓库不引第三方依赖）。它对「十亿个笑」这类实体膨胀
    没有防护，所以**先卡长度**：正常响应有 `max-keys` 兜着，超过这个数量级只可能是
    对端出了问题或者响应被人做了手脚，那就别解析它。
    外部实体（XXE）Python 3 的 ElementTree 本身不解析，不用额外处理。
    """
    if len(text) > _MAX_XML:
        raise OssError(f"OSS 响应异常大（{len(text)} 字节），没有解析")
    # **长度上限挡不住实体膨胀** —— 它量的是膨胀前的原文，几百字节能炸到 GB 级。
    # 真正管用的是拒绝带 DTD 的响应：OSS 正常响应里不会有 <!DOCTYPE / <!ENTITY
    if "<!DOCTYPE" in text[:2048] or "<!ENTITY" in text:
        raise OssError("OSS 响应里有 DTD —— 正常响应不会有，没有解析")
    return ET.fromstring(text)  # noqa: S314 — 见上：已拒 DTD，且 ET 不解析外部实体


def _ns(root) -> str:
    return root.tag.split("}")[0] + "}" if "}" in root.tag else ""


def list_prefixes(bucket: str, prefix: str = "", *, region: str, creds, transport=None) -> list:
    """列出 `prefix` 下面的一层「目录」（CommonPrefixes）。用来看现在有哪些组/人。"""
    out, token = [], ""
    for _ in range(_MAX_PAGES):
        query = {"list-type": "2", "delimiter": "/", "max-keys": "1000"}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        raw = call("GET", bucket, region=region, query=query, creds=creds, transport=transport)
        root = _parse(raw.decode(errors="replace"))
        ns = _ns(root)
        out += [p.findtext(f"{ns}Prefix") or "" for p in root.findall(f"{ns}CommonPrefixes")]
        if (root.findtext(f"{ns}IsTruncated") or "false").lower() != "true":
            return out
        token = root.findtext(f"{ns}NextContinuationToken") or ""
        if not token:
            # 说还有下一页却不给 token：只拿到一部分。静默返回会让调用方以为「就这些」，
            # 而这个清单是拿来判断「这个人的目录建没建过」的 —— 少一条就会重复建
            raise OssError("OSS 说还有下一页却没给 continuation-token，清单不完整，已中断")
    raise OssError(f"OSS 列举超过 {_MAX_PAGES} 页，疑似死循环，已中断")


def list_buckets(*, region: str, creds, transport=None) -> list:
    """这个主账号下的**全部**桶，返回 `[{"name", "region", "created"}, …]`。

    ListBuckets 是服务级请求：随便哪个地域的 endpoint 都返回所有地域的桶，
    每条自带 `Location`（形如 `oss-cn-hangzhou`）。所以 `region` 只是拨号用的，
    **不是过滤条件** —— 别拿它当「只看杭州的桶」使。

    用途是体检里那条「桶在云上，但没登记在任何白名单里」。所以**宁可中断也不能少列**：
    少列出来的那个桶正好就是没人管的那个，而它会安安静静地不出现在清单上。
    """
    out, marker = [], ""
    for _ in range(_MAX_PAGES):
        query = {"max-keys": "1000"}
        if marker:
            query["marker"] = marker
        raw = call(
            "GET", "", region=region, query=query, creds=creds, transport=transport, service=True
        )
        root = _parse(raw.decode(errors="replace"))
        ns = _ns(root)
        for b in root.iter(f"{ns}Bucket"):
            name = b.findtext(f"{ns}Name") or ""
            if name:
                out.append(
                    {
                        "name": name,
                        "region": b.findtext(f"{ns}Location") or "",
                        "created": b.findtext(f"{ns}CreationDate") or "",
                    }
                )
        if (root.findtext(f"{ns}IsTruncated") or "false").lower() != "true":
            return out
        # 同 list_prefixes：说还有下一页却不给游标，就是只拿到一部分，不能假装列完了
        marker = root.findtext(f"{ns}NextMarker") or ""
        if not marker:
            raise OssError("OSS 说还有下一页却没给 NextMarker，桶清单不完整，已中断")
    raise OssError(f"OSS 列举桶超过 {_MAX_PAGES} 页，疑似死循环，已中断")


def put_folder(bucket: str, prefix: str, *, region: str, creds, transport=None) -> None:
    """放一个 0 字节的 `<prefix>/` 占位对象。

    **OSS 没有真目录**，前缀是虚的 —— 写第一个对象时「目录」自然就出现了。
    这个占位对象纯粹是为了让人在控制台里看得见结构、知道「这是给我的地方」。
    不放也不影响任何读写。
    """
    key = prefix if prefix.endswith("/") else prefix + "/"
    call("PUT", bucket, key, region=region, body=b"", creds=creds, transport=transport)


def list_objects(bucket: str, prefix: str, *, region: str, creds, transport=None) -> list:
    """`prefix` 底下的全部对象（递归）。返回 `[(key, 字节数), …]`。

    搬目录之前要拿它列源、搬完要拿它对账 —— **两边的对象数和字节数都得一致才算搬完**。
    只看「复制没报错」不行：OSS 的逐对象复制不是原子的，中途断了两边各有一半，
    而那时候「没报错的那些」看起来一切正常。
    """
    out, token = [], ""
    for _ in range(_MAX_PAGES):
        query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            query["continuation-token"] = token
        root = _parse(
            call(
                "GET", bucket, region=region, query=query, creds=creds, transport=transport
            ).decode(errors="replace")
        )
        ns = _ns(root)
        for node in root.findall(f"{ns}Contents"):
            out.append((node.findtext(f"{ns}Key") or "", int(node.findtext(f"{ns}Size") or 0)))
        if (root.findtext(f"{ns}IsTruncated") or "false").lower() != "true":
            return out
        token = root.findtext(f"{ns}NextContinuationToken") or ""
        if not token:
            raise OssError("OSS 说还有下一页却没给 continuation-token，清单不完整，已中断")
    raise OssError(f"OSS 列举超过 {_MAX_PAGES} 页，已中断")


#: 超过这个大小的对象，简单的 CopyObject 会被 OSS 拒（要走分片复制）。
#: 官方上限是 1GB，这里留点余量
COPY_MAX = 900 * 1024 * 1024


def copy_object(bucket: str, src_key: str, dst_key: str, *, region: str, creds, transport=None):
    """**服务端复制**：数据不经过我们这边，走的是 `x-oss-copy-source` 头。

    所以搬目录的成本是「对象**个数**」，不是「字节数」—— 几 TB 一个大文件和几 KB
    一个小文件，单次复制的耗时差不了多少。

    大于 `COPY_MAX` 的对象这个接口会拒，得走分片复制（`UploadPartCopy`）——
    **这里不做**，遇到就抛错让人看见，而不是悄悄漏掉一个文件。
    """
    call(
        "PUT",
        bucket,
        dst_key,
        region=region,
        extra={
            "x-oss-copy-source": f"/{bucket}/{urllib.parse.quote(src_key)}",
            # **禁止覆盖。**「面板没有 oss:DeleteObject 所以删不掉东西」这个推理
            # 对覆盖不成立 —— 覆盖只要 PutObject，而策略给了。上次搬了一半、期间
            # 有人往新路径写了新数据，重跑就会盖掉它，而对账（比字节数）还说「对上了」
            "x-oss-forbid-overwrite": "true",
        },
        creds=creds,
        transport=transport,
    )
