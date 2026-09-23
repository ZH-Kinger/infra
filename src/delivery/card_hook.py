"""飞书卡片按钮回调：管理员在飞书里点一下就处理完，不用再开面板。

用在哪
──────
离职停号的卡片上有「确认删除」「没离职」两个按钮。原先卡片只有一个「去确认」链接，
人得先打开面板、找到那一行、再点一次 —— 一件五秒钟的事被拆成三步，于是就拖着。

为什么这个入口比面板更需要小心
──────────────────────────────
面板那条路有登录会话（飞书 OAuth）兜底；这条路**没有登录**，飞书只带来一个 open_id。
一个能伪造请求的人，把 open_id 填成管理员就能删号。所以这里三道都要过：

  1. **验签**（`X-Lark-Signature` = sha256(时间戳 + 随机串 + Encrypt Key + 原始 body)）。
     **没配 Encrypt Key 就拒绝一切回调** —— 不是「先放行等配好」：这个入口能删云账号。
  2. `header.token` 和后台的 Verification Token 常数时间比对。
  3. 点的人必须在管理员名单里。open_id 不能直接比 —— 名单里存的是 union_id，
     所以要拿 open_id 去飞书换 union_id（`resolve_admin`，在 server 里注入）。

**签名算的是原始字节**，不是重新序列化的 JSON：`json.dumps` 出来的空格、键顺序都可能不一样，
一个字节的差别就是永远 403。

加密模式
────────
后台配了 Encrypt Key 之后，body 是 `{"encrypt": "<base64>"}`，AES-256-CBC、前 16 字节是 IV、
PKCS7 补位、密钥是 `sha256(Encrypt Key)`。**URL 校验（challenge）那一次也是加密的**，
所以解密要排在 challenge 判断之前，否则回调地址根本存不下来。

重放
────
同一次点击飞书会重投（没在 3 秒内收到 200 就重发）。按 `header.event_id` 去重：
重投时回同样的结果，不重复执行。
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
from typing import Callable, Optional

ENV_ENCRYPT_KEY = "DELIVERY_FEISHU_CARD_ENCRYPT_KEY"  # noqa: S105  环境变量名
ENV_VERIFY_TOKEN = "DELIVERY_FEISHU_VERIFY_TOKEN"  # noqa: S105  和审批回调同一把

#: 同一个 event_id 多久之内算重投
DEDUP_WINDOW = 300.0
_DEDUP_MAX = 2000
#: 请求时间戳和现在差这么多就不认（防重放）。飞书重投窗口是分钟级
CLOCK_SKEW = 300.0


class CardError(Exception):
    """回调不合法。`status` 是该回给飞书的状态码。"""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def decrypt(encrypted: str, key: str) -> str:
    """AES-256-CBC 解密飞书的 `encrypt` 字段。密钥是 sha256(Encrypt Key)。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = base64.b64decode(encrypted)
    if len(raw) <= 16:
        raise CardError("密文太短")
    digest = hashlib.sha256(str(key).encode()).digest()
    dec = Cipher(algorithms.AES(digest), modes.CBC(raw[:16])).decryptor()
    body = dec.update(raw[16:]) + dec.finalize()
    pad = body[-1] if body else 0
    if not 1 <= pad <= 16 or len(body) < pad:
        raise CardError("解密结果补位不对（Encrypt Key 配错了？）")
    return body[:-pad].decode("utf-8")


class Hook:
    """卡片回调的校验与分发。纯逻辑，不碰网络 —— 换 union_id 和执行动作都由调用方注入。"""

    def __init__(
        self,
        *,
        encrypt_key: str = "",
        verify_token: str = "",
        clock: Callable[[], float] = time.time,
    ):
        self._key = str(encrypt_key or "")
        self._token = str(verify_token or "")
        self._clock = clock
        self._seen: dict = {}

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def check_signature(self, headers, raw: bytes) -> None:
        """验签。`headers` 是个能 `.get(名字)` 的东西（大小写不敏感由调用方保证）。"""
        ts = str(headers.get("X-Lark-Request-Timestamp") or "")
        nonce = str(headers.get("X-Lark-Request-Nonce") or "")
        sig = str(headers.get("X-Lark-Signature") or "")
        if not (ts and nonce and sig):
            raise CardError("请求缺签名头")
        try:
            skew = abs(self._clock() - float(ts))
        except ValueError:
            raise CardError("时间戳不是数字") from None
        if not math.isfinite(skew):
            # `float("nan")` 和任何数比都是 False，下面那行放它过去（审计 Low-5）
            raise CardError("时间戳不是有限数")
        if skew > CLOCK_SKEW:
            # 重放：拿一个旧的、签名合法的请求反复发
            raise CardError("请求时间对不上（超过 5 分钟）")
        want = hashlib.sha256((ts + nonce + self._key).encode() + raw).hexdigest()
        if not secrets.compare_digest(want, sig):
            raise CardError("签名不对")

    def payload(self, raw: bytes) -> dict:
        """原始 body → 明文 dict。加密模式下先解密。"""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise CardError("请求体不是合法 JSON", status=400) from None
        if not isinstance(body, dict):
            raise CardError("请求体必须是对象", status=400)
        if "encrypt" in body:
            if not self._key:
                raise CardError("收到加密回调，但没配 Encrypt Key")
            body = json.loads(decrypt(str(body["encrypt"]), self._key))
            if not isinstance(body, dict):
                raise CardError("解密后不是对象", status=400)
        return body

    @staticmethod
    def challenge(body: dict) -> Optional[str]:
        """URL 校验。**在验 token 之前**：还没配好时也要能存下回调地址。"""
        if str(body.get("type") or "") == "url_verification":
            got = str(body.get("challenge") or "")
            return got or ""
        return None

    @staticmethod
    def _part(body, name: str) -> dict:
        """取 body 里的一段。**不是对象就当空** —— 形状怪的事件不该把连接掐掉：
        飞书对非 200 会一直重投，而客户端看到的是网络错误，日志里什么都没有。"""
        got = body.get(name) if isinstance(body, dict) else None
        return got if isinstance(got, dict) else {}

    def check_token(self, body: dict) -> None:
        token = str(self._part(body, "header").get("token") or body.get("token") or "")
        if not self._token:
            raise CardError(f"没配 {ENV_VERIFY_TOKEN}")
        if not secrets.compare_digest(self._token, token):
            raise CardError("校验失败")

    def event_type(self, body: dict) -> str:
        return str(self._part(body, "header").get("event_type") or "")

    def claim(self, body: dict) -> bool:
        """这次事件该不该处理。重投（同一个 event_id）返回 False。"""
        eid = str(self._part(body, "header").get("event_id") or "")
        if not eid:
            return True
        now = self._clock()
        self._seen = {k: v for k, v in self._seen.items() if now - v < DEDUP_WINDOW}
        if eid in self._seen:
            return False
        if len(self._seen) >= _DEDUP_MAX:
            self._seen.clear()
        self._seen[eid] = now
        return True

    @staticmethod
    def action(body: dict) -> tuple:
        """`(点的人 open_id, 按钮 value dict)`。

        **open_id 在 `event.operator.open_id`**，不是 `operator.operator_id.open_id` ——
        后者是消息事件的形状，取错了永远是空串，然后所有按 open_id 判管理员的地方
        一律拒绝，而日志里什么线索都没有。
        """
        event = Hook._part(body, "event")
        who = str(Hook._part(event, "operator").get("open_id") or "")
        value = Hook._part(event, "action").get("value")
        return who, value if isinstance(value, dict) else {}


def toast(kind: str, text: str) -> dict:
    """同步返回体：飞书会把这句话浮在点按钮的人屏幕上。`kind`：info/success/error/warning。"""
    return {"toast": {"type": kind, "content": str(text)[:120]}}
