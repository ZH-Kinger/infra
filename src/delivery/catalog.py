"""申请模板目录：员工只能从这里选，不能自己填用户组、角色或策略。

模板文件 `identity/request-templates.json`（gitignored：里面有云账号 ID），格式见
`identity/request-templates.example.json`。加载时逐项校验，写错直接拒绝加载——
模板决定审批通过后往云上写什么，静默忽略一个拼错的字段可能就是多给了权限。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import DeliveryError

SCHEMA = "wuji-request-templates@1"

KIND_ACCOUNT = "account"
KIND_PERMISSION = "permission"
KIND_CREDENTIAL = "credential"
KINDS = (KIND_ACCOUNT, KIND_PERMISSION, KIND_CREDENTIAL)
KIND_LABELS = {
    KIND_ACCOUNT: "开账号",
    KIND_PERMISSION: "云账号权限",
    KIND_CREDENTIAL: "访问凭证",
}

PLATFORMS = ("aliyun", "volcano")
RISKS = ("low", "medium", "high")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_ACCOUNT = re.compile(r"^[0-9]{6,20}$")
_GROUP = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_ROLE = {
    "aliyun": re.compile(r"^acs:ram::(?P<account>[0-9]+):role/[A-Za-z0-9._-]{1,64}$"),
    "volcano": re.compile(r"^trn:iam::(?P<account>[0-9]+):role/[A-Za-z0-9._-]{1,64}$"),
}
#: 用户名规则的默认值：小写字母开头，只含小写字母数字点和横线
DEFAULT_USERNAME = r"^[a-z][a-z0-9.-]{1,31}$"
MAX_CREDENTIAL_HOURS = 12
_CATEGORY_MAX = 16


class CatalogError(DeliveryError):
    """模板目录不合法。"""


@dataclass(frozen=True)
class Template:
    id: str
    kind: str
    platform: str
    account: str
    title: str
    description: str = ""
    risk: str = "low"
    #: 展示用分类（如「存储」「AI 训练」），只影响申请页的筛选，不影响开通内容
    category: str = ""
    #: permission：加入的用户组；account：新账号默认加入的用户组
    groups: tuple = ()
    #: credential：扮演的角色与单次最长小时数、批准后可领取的天数
    role_arn: str = ""
    max_hours: int = 1
    valid_days: int = 7
    #: permission：最长授权天数，0 = 不限（到期回收在后续阶段）
    max_days: int = 0
    #: account：用户名规则、是否开通控制台登录（领取一次性初始密码）
    username_pattern: str = DEFAULT_USERNAME
    console_login: bool = False
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def scope(self) -> str:
        return f"{self.platform}/{self.account}"

    def public(self) -> dict:
        """给前端和 CLI 的字段：不含角色 ARN（员工不需要知道，也不能改）。"""
        return {
            "id": self.id,
            "kind": self.kind,
            "kind_label": KIND_LABELS[self.kind],
            "platform": self.platform,
            "account": self.account,
            "title": self.title,
            "description": self.description,
            "risk": self.risk,
            "category": self.category,
            "groups": list(self.groups),
            "max_hours": self.max_hours if self.kind == KIND_CREDENTIAL else 0,
            "valid_days": self.valid_days if self.kind == KIND_CREDENTIAL else 0,
            "max_days": self.max_days if self.kind == KIND_PERMISSION else 0,
            "username_pattern": self.username_pattern if self.kind == KIND_ACCOUNT else "",
            "console_login": self.console_login if self.kind == KIND_ACCOUNT else False,
        }


def _str(spec: dict, key: str, where: str, *, required: bool = True) -> str:
    value = spec.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise CatalogError(f"{where}：{key} 必须是非空字符串")
    return value.strip()


def _int(spec: dict, key: str, where: str, default: int, lo: int, hi: int) -> int:
    value = spec.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise CatalogError(f"{where}：{key} 必须是 {lo}–{hi} 之间的整数")
    return value


_KNOWN = {
    "id",
    "kind",
    "platform",
    "account",
    "title",
    "description",
    "risk",
    "category",
    "groups",
    "role_arn",
    "max_hours",
    "valid_days",
    "max_days",
    "username_pattern",
    "console_login",
}


def parse_template(spec: object, index: int) -> Template:
    where = f"templates[{index}]"
    if not isinstance(spec, dict):
        raise CatalogError(f"{where} 必须是对象")
    unknown = sorted(set(spec) - _KNOWN)
    if unknown:
        raise CatalogError(f"{where}：不认识的字段 {', '.join(unknown)}（拼错了？）")
    tid = _str(spec, "id", where)
    where = f"模板 {tid}"
    if not _ID.match(tid):
        raise CatalogError(f"{where}：id 只能含小写字母、数字和横线")
    kind = _str(spec, "kind", where)
    if kind not in KINDS:
        raise CatalogError(f"{where}：kind 只能是 {' / '.join(KINDS)}")
    platform = _str(spec, "platform", where)
    if platform not in PLATFORMS:
        raise CatalogError(f"{where}：platform 只能是 {' / '.join(PLATFORMS)}")
    account = _str(spec, "account", where)
    if not _ACCOUNT.match(account):
        raise CatalogError(f"{where}：account 必须是云账号 ID（数字）")
    risk = _str(spec, "risk", where, required=False) or "low"
    if risk not in RISKS:
        raise CatalogError(f"{where}：risk 只能是 {' / '.join(RISKS)}")
    category = _str(spec, "category", where, required=False)
    if len(category) > _CATEGORY_MAX:
        raise CatalogError(f"{where}：category 最长 {_CATEGORY_MAX} 个字")
    groups = spec.get("groups", [])
    if not isinstance(groups, list) or not all(
        isinstance(g, str) and _GROUP.match(g) for g in groups
    ):
        raise CatalogError(f"{where}：groups 必须是用户组名数组")

    kw: dict = {}
    if kind == KIND_PERMISSION:
        if not groups:
            raise CatalogError(f"{where}：权限模板至少要有一个用户组")
        kw["max_days"] = _int(spec, "max_days", where, 0, 0, 3650)
    elif kind == KIND_CREDENTIAL:
        role = _str(spec, "role_arn", where)
        match = _ROLE[platform].match(role)
        if not match:
            raise CatalogError(f"{where}：role_arn 格式不对")
        if match.group("account") != account:
            raise CatalogError(f"{where}：role_arn 不属于云账号 {account}")
        if groups:
            raise CatalogError(f"{where}：凭证模板不能带用户组")
        kw["role_arn"] = role
        kw["max_hours"] = _int(spec, "max_hours", where, 1, 1, MAX_CREDENTIAL_HOURS)
        kw["valid_days"] = _int(spec, "valid_days", where, 7, 1, 90)
    else:
        pattern = _str(spec, "username_pattern", where, required=False) or DEFAULT_USERNAME
        if not pattern.startswith("^") or not pattern.endswith("$"):
            raise CatalogError(f"{where}：username_pattern 必须以 ^ 开头、$ 结尾")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise CatalogError(f"{where}：username_pattern 不是合法正则：{exc}") from exc
        console = spec.get("console_login", False)
        if not isinstance(console, bool):
            raise CatalogError(f"{where}：console_login 必须是 true / false")
        kw["username_pattern"] = pattern
        kw["console_login"] = console

    return Template(
        id=tid,
        kind=kind,
        platform=platform,
        account=account,
        title=_str(spec, "title", where),
        description=_str(spec, "description", where, required=False),
        risk=risk,
        category=category,
        groups=tuple(groups),
        **kw,
    )


@dataclass(frozen=True)
class Catalog:
    templates: tuple = ()

    def get(self, template_id: str) -> Optional[Template]:
        return next((t for t in self.templates if t.id == template_id), None)

    def of_kind(self, kind: str) -> tuple:
        return tuple(t for t in self.templates if t.kind == kind)


def parse(data: object) -> Catalog:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CatalogError(f"模板目录 schema 必须是 {SCHEMA}")
    items = data.get("templates")
    if not isinstance(items, list):
        raise CatalogError("模板目录缺 templates 数组")
    templates = [parse_template(spec, i) for i, spec in enumerate(items)]
    ids = [t.id for t in templates]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise CatalogError(f"模板 id 重复：{', '.join(dup)}")
    return Catalog(tuple(templates))


def load(path: Optional[str]) -> Catalog:
    """文件不存在 = 没有可申请的模板（安全的一侧），格式错误则报错。"""
    if not path or not Path(path).exists():
        return Catalog()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"读不了模板目录 {path}：{exc}") from exc
    return parse(data)
