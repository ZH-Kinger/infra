"""把凭证密封起来存：**密文在面板，密钥在链接里**。

为什么要存
──────────
使用方要能反复看到自己的 AK/SK（换台机器、重装环境、同事接手）。而长期 AK 的 secret
云上只在创建那一刻返回一次，之后任何接口都取不回来 —— 想让它能再看一次，只能我们存。

为什么这么存
────────────
面板此前刻意「一个秘密都不存」。现在必须存了，那就让存下来的东西**单独没有用**：

  · 每份凭证用一把新的随机密钥加密，密钥**从不落盘**，只出现在发给使用方的链接里
  · 申请单里只有密文和随机数
  · 服务端自己也解不开 —— 没有主密钥，没有密钥库，没有「管理员一键查看」

所以 `tickets.json` 整个泄漏（备份、误发的日志、能读那个文件的任何进程）也拿不到凭证。
代价是链接丢了就真的取不回来，只能重新申请 —— 这是刻意的取舍。

为什么不自己实现
────────────────
AEAD 自己搓必错。用 `cryptography` 的 AES-GCM：认证加密，密文被改过会直接解不开，
而不是解出一段垃圾让上层去猜。这是面板唯一的运行时依赖，加它是因为没有替代品 ——
标准库里没有 AES。
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from .errors import DeliveryError

#: AES-256-GCM。12 字节随机数是 GCM 的标准长度，每次加密都换新的
_KEY_BYTES = 32
_NONCE_BYTES = 12


class SealError(DeliveryError):
    """密封或解封失败。"""


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - 部署缺依赖时的兜底
        raise SealError(
            "缺少 cryptography（面板的唯一运行时依赖）。装上再试：pip install cryptography"
        ) from exc
    return AESGCM


def _b64(raw: bytes) -> str:
    """URL 安全、无填充 —— 密钥要放进 URL 的 fragment，`+/=` 会被转义得很难看。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """`validate=True` 不能省：默认的 `b64decode` 会**静默丢掉**字母表以外的字符，
    于是 `key[:5] + '*!' + key[5:]` 这种明显被改过的串照样能解开。"""
    pad = "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text + pad, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise SealError("密钥或密文格式不对") from exc


@dataclass(frozen=True)
class Sealed:
    """密封结果。`key` **不要存**，它只该出现在发给使用方的链接里。"""

    key: str
    nonce: str
    ciphertext: str

    def stored(self) -> dict:
        """要写进申请单的部分。**刻意不含 key** —— 这个方法就是那条边界。"""
        return {"nonce": self.nonce, "ciphertext": self.ciphertext}

    def __repr__(self) -> str:
        """默认的 dataclass repr 会把 key 原样打出来。异常回溯、日志、调试打印都会
        经过它 —— 而 key 是这套设计里唯一不该落盘的东西。"""
        return f"Sealed(key=<隐藏>, nonce={self.nonce!r}, ciphertext=<{len(self.ciphertext)} 字符>)"


def selfcheck() -> None:
    """加密库在不在、能不能跑。**在动云之前调**。

    缺依赖时 `seal` 是在凭证已经签发出来之后才抛的 —— 那时云上子账号和长期 AK 都建好了，
    只能靠失败清理去收，申请人还白等了一轮审批。体检页也调这个。
    """
    box = seal("selfcheck")
    if unseal(box.key, box.nonce, box.ciphertext) != "selfcheck":
        raise SealError("加密自检没通过")


def seal(plaintext: str) -> Sealed:
    key = os.urandom(_KEY_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    data = _aesgcm()(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return Sealed(key=_b64(key), nonce=_b64(nonce), ciphertext=_b64(data))


def unseal(key: str, nonce: str, ciphertext: str) -> str:
    """密钥不对、密文被改过，都在这里失败 —— GCM 会校验，不会解出垃圾让上层去猜。"""
    text = str(key or "")
    raw_key = _unb64(text)
    if len(raw_key) != _KEY_BYTES:
        raise SealError("密钥长度不对")
    # 32 字节编成 43 个字符，最后一个字符有 2 个比特是多余的 —— 不校验规范性的话，
    # 末位换成另外 3 个字符照样解得开，同一份凭证有 4 个不同的链接。
    if _b64(raw_key) != text:
        raise SealError("密钥不是规范编码")
    try:
        data = _aesgcm()(raw_key).decrypt(
            _unb64(str(nonce or "")), _unb64(str(ciphertext or "")), None
        )
    except Exception:  # noqa: BLE001 — 密钥错、密文被改、长度不对，对外都是同一句
        raise SealError("这个链接打不开这份凭证") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SealError("凭证内容损坏") from exc
