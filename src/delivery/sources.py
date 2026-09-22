"""第三方数据源登记表：别人的对象存储，我们要从那儿往回搬。

为什么凭证不进申请单
────────────────────
搬第三方的数据要对方给的 AK/SK，而那把钥匙**面板必须能解开**（要拿去调迁移服务），
所以用不了 `sealed.py` 那套「密钥在链接里、服务端自己也解不开」的设计。

那就只剩两条路：让申请人在表单里填，或者管理员单独登记。选了后者，两个理由：

1. **第三方凭证本来就该管理员保管，不该让申请人转手。** 申请人把对方的 AK
   贴进一个 Web 表单，这个动作本身就有问题 —— 他可能截图、可能贴错地方、
   可能把它同时发进某个群。
2. 一个「服务端能解的主密钥」意味着**面板被攻破 = 所有第三方凭证泄漏**。
   而现在面板连自己发出去的凭证都解不开，那是个很硬的性质，不该为这个功能破掉。

所以申请单里只有 `src://<源标识>/<前缀>/`，真正的钥匙留在这个 600 文件里。

**这个模块的返回值分两种，别混**
────────────────────────────────
  `options()`  给前端和申请单看的：只有标识和名字，**一个字节的凭证都没有**
  `resolve()`  给执行路径用的：带凭证，只在提交迁移任务那一刻用一次

混了的话凭证会跟着 `/api/requests/options` 发到每个打开申请页的人的浏览器里。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .errors import DeliveryError

DEFAULT_PATH = "identity/transfer-sources.json"
SCHEMA = "wuji-transfer-sources@1"

#: 源标识。会进申请单的 `src://<标识>/…`，所以**过白名单**
_ID = re.compile(r"\A[a-z0-9][a-z0-9-]{1,62}\Z")
#: 对方的桶名。各家规则不一，取一个够宽但不含斜杠和空格的交集
_BUCKET = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{1,62}\Z")
#: endpoint 只接受主机名，**不接受带协议或路径的串** —— 那会被拼进迁移服务的
#: 地址配置，一个 `http://` 或者尾随路径就可能把数据发到别的地方
_HOST = re.compile(r"\A[a-z0-9][a-z0-9.-]{3,127}\Z")

_REQUIRED = ("label", "endpoint", "bucket", "access_key_id", "access_key_secret")
_OPTIONAL = ("region", "note", "prefix")


class SourceError(DeliveryError):
    """登记表读不了或写错了。"""


def load(path: Optional[str] = None) -> dict:
    """读登记表。**文件不存在返回空字典**（还没登记过任何第三方源，是正常状态）。

    读得到但内容不合法一律抛错 —— 把一份写坏的登记表当成「没有源」，
    表现是申请页上那些源突然消失，而没人知道为什么。
    """
    target = Path(path or DEFAULT_PATH)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"读不了 {target}：{exc}") from exc
    if not isinstance(data, dict):
        raise SourceError(f"{target} 必须是对象")
    items = data.get("sources", data)
    if not isinstance(items, dict):
        raise SourceError(f"{target} 里 sources 必须是对象")
    out = {}
    for key, spec in items.items():
        if key.startswith("_"):
            continue
        out[key] = _one(key, spec, str(target))
    return out


def _one(key: str, spec, where: str) -> dict:
    if not _ID.match(str(key)):
        raise SourceError(f"{where}：源标识 {key!r} 只能用小写字母、数字和横线")
    if not isinstance(spec, dict):
        raise SourceError(f"{where}：{key} 必须是对象")
    missing = [f for f in _REQUIRED if not str(spec.get(f) or "").strip()]
    if missing:
        raise SourceError(f"{where}：{key} 缺 {'、'.join(missing)}")
    unknown = sorted(set(spec) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise SourceError(f"{where}：{key} 里不认识的字段 {'、'.join(unknown)}（拼错了？）")
    host = str(spec["endpoint"]).strip().lower()
    if not _HOST.match(host):
        raise SourceError(
            f"{where}：{key} 的 endpoint 只写主机名，不要带 http:// 或路径 —— {host[:60]!r}"
        )
    bucket = str(spec["bucket"]).strip()
    if not _BUCKET.match(bucket):
        raise SourceError(f"{where}：{key} 的桶名不合法 {bucket[:60]!r}")
    prefix = str(spec.get("prefix") or "").lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    if ".." in prefix or "//" in prefix:
        raise SourceError(f"{where}：{key} 的 prefix 里不能有 .. 或连续斜杠")
    return {
        "id": key,
        "label": str(spec["label"]).strip()[:80],
        "endpoint": host,
        "bucket": bucket,
        "region": str(spec.get("region") or "").strip()[:40],
        "prefix": prefix,
        "note": str(spec.get("note") or "").strip()[:200],
        "access_key_id": str(spec["access_key_id"]).strip(),
        "access_key_secret": str(spec["access_key_secret"]).strip(),
    }


def options(registry: dict) -> list:
    """给前端的选项。**只有标识、名字和桶，没有任何凭证。**

    这是这个模块最容易出事的地方：`/api/requests/options` 会把返回值发给
    每一个打开申请页的人。漏一个字段，对方的 AK 就进了所有人的浏览器。
    """
    return [
        {
            "id": row["id"],
            "label": row["label"],
            "bucket": row["bucket"],
            "region": row["region"],
            "note": row["note"],
        }
        for row in sorted(registry.values(), key=lambda r: r["id"])
    ]


def confine(row: dict, asked: str) -> str:
    """把申请人填的前缀拼到这个源的根下面，并且**保证越不出去**。

    登记表里那个 `prefix` 是我们和对方约定的范围 —— 「只从 `to-wuji/` 往下取」。
    不强制的话，`src://vendor-a/` 就是把对方整个桶拉回来，而对方给我们这把 AK
    时同意的并不是这件事。

    这个校验必须在这儿，不能留给调用方：接线的人不会知道那个字段有这层含义
    （登记表的说明里写了，代码里不体现的话它就只是个注释）。
    """
    want = str(asked or "").lstrip("/")
    if ".." in want or "//" in want:
        raise SourceError(f"前缀里不能有 .. 或连续斜杠：{want[:60]!r}")
    if want and not want.endswith("/"):
        # 只搬目录。少了这条，前缀 `team` 会连 `team-secret/` 一起匹配进来
        want += "/"
    got = f"{row.get('prefix') or ''}{want}"
    root = str(row.get("prefix") or "")
    if root and not got.startswith(root):
        raise SourceError(f"{got[:80]!r} 超出了这个数据源允许的范围 {root!r}")
    return got


def resolve(registry: dict, source_id: str) -> dict:
    """拿带凭证的那一份。**只在提交迁移任务那一刻调一次。**

    找不到就抛错，不返回 None —— 一个 None 传下去会变成「源桶是空字符串」，
    而那种任务提上去之后的表现是「搬了 0 个对象，成功」。
    """
    got = registry.get(str(source_id or "").strip())
    if got is None:
        raise SourceError(
            f"没有登记过 {source_id!r} 这个第三方源。"
            "第三方凭证由管理员登记在 identity/transfer-sources.json，申请人只能选已登记的。"
        )
    return got
