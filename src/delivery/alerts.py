"""飞书群自定义机器人告警（签名校验版）。

只允许发到飞书 / Lark 官方的机器人 webhook 地址：webhook 地址来自配置，
配错成任意地址就等于把全员账号和权限变化发到外面。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.request
from typing import Callable, Optional

from .errors import DeliveryError

ENV_WEBHOOK = "DELIVERY_ALERT_WEBHOOK"
ENV_SECRET = "DELIVERY_ALERT_SECRET"  # noqa: S105  环境变量名

_ALLOWED_PREFIXES = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/",
    "https://open.larksuite.com/open-apis/bot/v2/hook/",
)
_TIMEOUT = 10
_HOOK_ID = re.compile(r"^[A-Za-z0-9-]{8,128}$")
#: 飞书单条文本消息上限约 150KB，告警不需要那么长
_MAX_TEXT = 4000


class AlertError(DeliveryError):
    """告警没发出去。"""


def sign(timestamp: int, secret: str) -> str:
    """飞书自定义机器人签名：以「timestamp\\nsecret」为密钥对空串做 HMAC-SHA256。"""
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode()


def _post(url: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    # url 已在 send_feishu 里限定为飞书官方 webhook 前缀
    req = urllib.request.Request(  # noqa: S310
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
        return json.loads(resp.read().decode() or "{}")


def send_feishu(
    text: str,
    *,
    webhook: str,
    secret: str,
    post: Callable[[str, dict], dict] = _post,
    clock: Callable[[], float] = time.time,
) -> None:
    _check(webhook, secret)
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT] + "\n…（已截断，详情见面板）"
    _deliver(
        {"msg_type": "text", "content": {"text": text}},
        webhook=webhook,
        secret=secret,
        post=post,
        clock=clock,
    )


def send_feishu_card(
    card: dict,
    *,
    webhook: str,
    secret: str,
    post: Callable[[str, dict], dict] = _post,
    clock: Callable[[], float] = time.time,
) -> None:
    """发一张交互卡片。自定义机器人的卡片放**顶层 `card` 字段**，不是 `content`。

    和 `send_feishu` 共用签名、地址校验、成功码判定 —— 那三样都踩过坑，只该有一份。
    """
    _check(webhook, secret)
    if not isinstance(card, dict) or not card:
        raise AlertError("卡片必须是非空对象")
    _deliver(
        {"msg_type": "interactive", "card": card},
        webhook=webhook,
        secret=secret,
        post=post,
        clock=clock,
    )


def _check(webhook: str, secret: str) -> None:
    prefix = next((x for x in _ALLOWED_PREFIXES if webhook.startswith(x)), "")
    if not prefix or not _HOOK_ID.fullmatch(webhook[len(prefix) :]):
        raise AlertError(f"{ENV_WEBHOOK} 必须是飞书自定义机器人地址（{_ALLOWED_PREFIXES[0]}…）")
    if not secret:
        raise AlertError(f"要设置 {ENV_SECRET}：机器人必须开启签名校验")


def _deliver(body: dict, *, webhook: str, secret: str, post, clock) -> None:
    ts = int(clock())
    payload = {"timestamp": str(ts), "sign": sign(ts, secret), **body}
    try:
        data = post(webhook, payload)
    except Exception as exc:  # noqa: BLE001
        # 不带原始异常文本：里面可能有 webhook 地址（地址本身就是凭证）
        raise AlertError(f"告警发送失败：{type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise AlertError("告警发送失败：飞书返回的不是 JSON 对象")

    # 必须明确返回成功码：空对象、缺字段都不能当成发出去了
    def success(value) -> bool:
        return type(value) is int and value == 0

    if not (success(data.get("code")) or success(data.get("StatusCode"))):
        code = data.get("code", data.get("StatusCode"))
        raise AlertError(f"告警被飞书拒绝：code={code} {str(data.get('msg') or '')[:100]}")


def from_env(environ) -> Optional[tuple]:
    webhook = environ.get(ENV_WEBHOOK, "")
    if not webhook:
        return None
    return webhook, environ.get(ENV_SECRET, "")
