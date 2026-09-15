"""把云上用户分成四类，指出谁需要补什么。

为什么不自动推导邮箱
────────────────────
真实数据里用户名的拼音顺序**不统一**：

    zhangsan        ←  张三     姓在前  →  zhang.san           规律成立
    sili            ←  李四     名在前  →  li.si             推不出来
    wuwang          ←  王五     名在前  →  wang.wu           推不出来

同一个账号里两种约定并存，任何自动规则都会给一部分人算错。**必须本人确认。**
所以本模块只负责**指出问题**，不负责猜答案。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

CLASS_OK = "ok"  # 有企业邮箱，且用户名 == 邮箱前缀
CLASS_MISMATCH = "mismatch"  # 有企业邮箱，但对不上
CLASS_MISSING_EMAIL = "missing_email"  # 云上没填邮箱
CLASS_SERVICE = "service"  # 服务号/机器人，不是人

# 服务号的启发式前缀。**只用来打标，不用来删东西**——判错了最坏是漏催一个人，
# 而反过来（把人当服务号忽略）会让他在 SSO 上线那天登不进去还没人知道。
SERVICE_PREFIXES = ("tempak-", "mes-", "codex-", "rl-", "wuji-", "svc-", "ci-")
SERVICE_NAMES = frozenset({"finance", "ci", "admin", "root", "bot"})

_EMAIL_RE = re.compile(r"\A[^@\s]+@[^@\s]+\Z")


@dataclass(frozen=True)
class AccountUser:
    """一朵云上的一个账号。"""

    platform: str
    account: str
    name: str
    display_name: str = ""
    email: str = ""

    @property
    def email_local(self) -> str:
        return self.email.split("@", 1)[0] if "@" in self.email else ""

    @property
    def label(self) -> str:
        """给人看的标识：优先中文显示名，否则用户名。"""
        return self.display_name or self.name


@dataclass(frozen=True)
class Finding:
    user: AccountUser
    verdict: str
    note: str = ""


@dataclass
class AuditReport:
    domain: str
    findings: list = field(default_factory=list)

    def of(self, verdict: str) -> list:
        return [f for f in self.findings if f.verdict == verdict]

    @property
    def people(self) -> list:
        return [f for f in self.findings if f.verdict != CLASS_SERVICE]

    @property
    def ready(self) -> bool:
        """全部人员都能被 SSO 匹配到 —— 这是开 SSO 的验收条件。"""
        return bool(self.people) and all(f.verdict == CLASS_OK for f in self.people)

    def summary(self) -> dict:
        return {
            CLASS_OK: len(self.of(CLASS_OK)),
            CLASS_MISMATCH: len(self.of(CLASS_MISMATCH)),
            CLASS_MISSING_EMAIL: len(self.of(CLASS_MISSING_EMAIL)),
            CLASS_SERVICE: len(self.of(CLASS_SERVICE)),
        }


def looks_like_service(user: AccountUser) -> bool:
    lowered = user.name.lower()
    return lowered.startswith(SERVICE_PREFIXES) or lowered in SERVICE_NAMES


def classify(user: AccountUser, *, domain: str, service_names: Sequence[str] = ()) -> Finding:
    if user.name in set(service_names) or looks_like_service(user):
        return Finding(user, CLASS_SERVICE, "疑似服务号（前缀或名称匹配），请人工确认")
    if not user.email:
        return Finding(user, CLASS_MISSING_EMAIL, "云上没填邮箱，SSO 无从匹配")
    if not _EMAIL_RE.match(user.email):
        return Finding(user, CLASS_MISMATCH, f"邮箱格式不合法：{user.email}")
    if not user.email.endswith(domain):
        # 个人邮箱（Gmail 之类）不在本轮范围内，但要如实列出来，
        # 否则这些人会在 SSO 上线时静默掉队。
        return Finding(user, CLASS_MISMATCH, f"非企业邮箱：{user.email}")
    if user.email_local == user.name:
        return Finding(user, CLASS_OK)
    return Finding(user, CLASS_MISMATCH, f"用户名 {user.name} ≠ 邮箱前缀 {user.email_local}")


def audit(
    users: Iterable[AccountUser], *, domain: str, service_names: Sequence[str] = ()
) -> AuditReport:
    report = AuditReport(domain=domain)
    for user in users:
        report.findings.append(classify(user, domain=domain, service_names=service_names))
    report.findings.sort(key=lambda f: (f.verdict, f.user.platform, f.user.name))
    return report
