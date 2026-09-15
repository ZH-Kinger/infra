"""身份对账：把各云上的账号和「人」对起来，并指出对不上的地方。

这是开 SSO 的硬前置：用户SSO 靠 IdP 断言里的标识匹配云上子用户，匹配不上就登不进去。
实测两朵云各只有 1 个用户能直接对上，所以必须先跑这个、清干净，再谈 SSO。
"""

from .audit import (
    CLASS_MISMATCH,
    CLASS_MISSING_EMAIL,
    CLASS_OK,
    CLASS_SERVICE,
    AccountUser,
    AuditReport,
    audit,
    classify,
)

__all__ = [
    "CLASS_MISMATCH",
    "CLASS_MISSING_EMAIL",
    "CLASS_OK",
    "CLASS_SERVICE",
    "AccountUser",
    "AuditReport",
    "audit",
    "classify",
]
