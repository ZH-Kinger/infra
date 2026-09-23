"""权限策略目录：员工能从两家云的全部权限策略里挑着申请。

采集（只读）::

    阿里云 RAM  ListPolicies           系统策略 + 自定义策略
    火山   IAM  ListPolicies           系统策略 + 自定义策略

快照 identity/policies.json（gitignored，0600）::

    {"captured_at": "...", "accounts": [
        {"platform": "aliyun", "account": "<UID>", "policies": [
            {"type": "System", "name": "AliyunOSSReadOnlyAccess", "description": "...",
             "service": "OSS", "updated": "..."}]},
        {"platform": "volcano", "account": "<ID>", "error": "..."}]}

采集失败的账号记 error，不写成「没有策略」。

**能申请什么由服务端规则决定**（identity/policy-rules.json，可选，示例见
identity/policy-rules.example.json）。内置的禁用清单挡住提权、身份管理、账单、审计这几类：
执行身份有 AttachPolicyToUser 权限，约等于能给任何人任何权限，禁用清单是唯一的闸，
所以规则文件只能在内置清单上**追加**禁用；要放开内置禁用项必须逐条写进 allow。

风险等级：禁用 > 高（管理员、FullAccess、*Manage*Access、全部自定义策略）> 低（只读）> 中。
风险决定最长授权天数（默认 低 180 / 中 90 / 高 30 天）。
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .clouds import aliyun, volcano
from .errors import DeliveryError

RULES_SCHEMA = "wuji-policy-rules@1"
TYPE_SYSTEM = "System"
TYPE_CUSTOM = "Custom"
TYPES = (TYPE_SYSTEM, TYPE_CUSTOM)
RISKS = ("low", "medium", "high")
RISK_LABELS = {"low": "低风险", "medium": "中风险", "high": "高风险"}
TYPE_LABELS = {TYPE_SYSTEM: "系统策略", TYPE_CUSTOM: "自定义策略"}

#: 内置禁用：提权（身份与权限管理、STS）、账号体系（资源目录、资源管理、云 SSO）、
#: 账单、操作审计。大小写不敏感的通配符，按策略名匹配。
DEFAULT_DENY = (
    "AdministratorAccess",
    "*AdministratorAccess",
    # 近似管理员
    "*PowerUser*",
    # 自己管理长期 AccessKey：绕过平台「只发临时凭证」的流程
    "AccessKey*",
    "*AccessKey*Manage*",
    "AliyunRAM*FullAccess",
    "AliyunRAM*ManageAccess",
    "AliyunIMS*FullAccess",
    "IAM*FullAccess",
    "AliyunSTS*",
    "STS*",
    # 密钥管理里可能存着云凭证：只读也不开放
    "AliyunKMS*",
    "KMS*",
    "*ResourceDirectory*FullAccess",
    "*ResourceManager*FullAccess",
    "*Organization*FullAccess",
    "*CloudSSO*FullAccess",
    "AliyunBSS*FullAccess",
    "*Billing*FullAccess",
    "*ActionTrail*FullAccess",
    "*CloudTrail*FullAccess",
)
#: 整个产品线都不开放，只有名字里带 ReadOnly 的只读策略例外：
#: 身份（RAM / IMS / IAM / IDaaS / 云 SSO / CloudIdentity）、账号体系（资源目录、资源管理、组织）、
#: 账单与购买（BSS / Billing）、审计（ActionTrail / Config / CloudTrail）。
#: 这类产品的「非 FullAccess」策略（如 AliyunBSSOrderAccess）照样能造成越权，不能只按后缀挡。
DEFAULT_DENY_FAMILIES = (
    "AliyunRAM*",
    "AliyunIMS*",
    "AliyunIDaaS*",
    "AliyunCloudSSO*",
    "AliyunResourceDirectory*",
    "AliyunResourceManager*",
    "AliyunBSS*",
    "AliyunActionTrail*",
    "AliyunConfig*",
    "IAM*",
    "CloudIdentity*",
    "Organization*",
    "Billing*",
    "BSS*",
    "CloudTrail*",
)
DEFAULT_MAX_DAYS = {"low": 180, "medium": 90, "high": 30}
DEFAULT_MAX_PER_REQUEST = 10
DENY_NOTE = "身份与权限管理、账单、审计类权限不开放申请，请联系管理员"
CUSTOM_NOTE = "自定义策略不开放申请，请联系管理员"

_MAX_PAGES = 200
_RULE_KEYS = {"schema", "deny", "allow", "risk", "max_days", "allow_custom", "max_per_request"}
_SERVICE_IN_DESC = re.compile(r"[（(]\s*([A-Za-z][A-Za-z0-9 ._/-]{0,30}?)\s*[)）]")
_NAME_SUFFIX = re.compile(
    r"(FullAccess|ReadOnlyAccess|ReadAccess|ReadOnly|ManageAccess|Access|Policy)$"
)
#: 自定义策略名可以有中文：只挡空白、路径分隔、引号、尖括号这类可能在别处被误解析的字符
_POLICY_NAME = re.compile(r"^[^\s/\\\"'<>]{1,128}$")


class PolicyError(DeliveryError):
    """策略目录或规则不可用。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── 规则 ─────────────────────────────────────────────────────────────────


def _glob(name: str, patterns: Iterable[str]) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in patterns)


@dataclass(frozen=True)
class Rules:
    deny: tuple = DEFAULT_DENY
    allow: tuple = ()
    risk: Mapping = field(default_factory=dict)
    max_days: Mapping = field(default_factory=lambda: dict(DEFAULT_MAX_DAYS))
    #: 自定义策略由管理员自己写，看名字判断不了能做什么（执行身份的策略也是自定义策略）：
    #: 默认不开放，只有在 allow 里逐条写明的才能申请；allow_custom=true 才整体放开
    allow_custom: bool = False
    max_per_request: int = DEFAULT_MAX_PER_REQUEST

    def denied(self, ptype: str, name: str) -> str:
        """不能申请的原因；能申请返回空串。allow 里逐条写明的名字才能放开禁用项。"""
        allowed = name.lower() in {a.lower() for a in self.allow}
        if ptype == TYPE_CUSTOM and not self.allow_custom and not allowed:
            return CUSTOM_NOTE
        if allowed:
            return ""
        if _glob(name, self.deny):
            return DENY_NOTE
        if _glob(name, DEFAULT_DENY_FAMILIES) and "readonly" not in name.lower():
            return DENY_NOTE
        return ""

    def risk_of(self, ptype: str, name: str) -> str:
        explicit = self.risk.get(name.lower())
        if explicit:
            return explicit
        low = name.lower()
        if ptype == TYPE_CUSTOM:
            return "high"  # 内容由管理员自己写，看名字判断不了
        if "admin" in low or low.endswith("fullaccess") or "manage" in low:
            return "high"
        if "readonly" in low or low.endswith("readaccess"):
            return "low"
        return "medium"

    def max_days_of(self, ptype: str, name: str) -> int:
        return int(self.max_days[self.risk_of(ptype, name)])


def parse_rules(data: object) -> Rules:
    if not isinstance(data, dict):
        raise PolicyError("策略规则必须是对象")
    unknown = sorted(set(data) - _RULE_KEYS)
    if unknown:
        raise PolicyError(f"策略规则里有不认识的字段 {', '.join(unknown)}（拼错了？）")
    if data.get("schema", RULES_SCHEMA) != RULES_SCHEMA:
        raise PolicyError(f"策略规则 schema 必须是 {RULES_SCHEMA}")

    def names(key: str) -> tuple:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(v, str) and 0 < len(v) <= 128 for v in value
        ):
            raise PolicyError(f"策略规则 {key} 必须是字符串数组")
        return tuple(value)

    risk = data.get("risk", {})
    if not isinstance(risk, dict) or not all(
        isinstance(k, str) and v in RISKS for k, v in risk.items()
    ):
        raise PolicyError(f"策略规则 risk 必须是 {{策略名: {' / '.join(RISKS)}}}")
    max_days = dict(DEFAULT_MAX_DAYS)
    override = data.get("max_days", {})
    if not isinstance(override, dict):
        raise PolicyError("策略规则 max_days 必须是对象")
    for level, days in override.items():
        if level not in RISKS or not isinstance(days, int) or isinstance(days, bool):
            raise PolicyError("策略规则 max_days 的键只能是 low / medium / high，值是整数")
        if not 1 <= days <= 3650:
            raise PolicyError("策略规则 max_days 必须在 1–3650 天之间")
        max_days[level] = days
    allow_custom = data.get("allow_custom", False)
    if not isinstance(allow_custom, bool):
        raise PolicyError("策略规则 allow_custom 必须是 true / false")
    per = data.get("max_per_request", DEFAULT_MAX_PER_REQUEST)
    if not isinstance(per, int) or isinstance(per, bool) or not 1 <= per <= 20:
        raise PolicyError("策略规则 max_per_request 必须在 1–20 之间")
    return Rules(
        # 只能在内置禁用清单上追加
        deny=DEFAULT_DENY + names("deny"),
        allow=names("allow"),
        risk={k.lower(): v for k, v in risk.items()},
        max_days=max_days,
        allow_custom=allow_custom,
        max_per_request=per,
    )


def load_rules(path: Optional[str]) -> Rules:
    """文件不存在用内置规则；存在但写坏了报错（不静默放宽）。"""
    if not path or not Path(path).exists():
        return Rules()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"读不了策略规则：{type(exc).__name__}") from exc
    return parse_rules(data)


def rules_view(rules: Rules) -> dict:
    """规则的可编辑视图。**内置禁用（DEFAULT_DENY / DEFAULT_DENY_FAMILIES）单列出来且不可改** ——
    它们是代码里的地板，规则文件只能在其上追加，不能往下挖。"""
    return {
        # deny 是**合并后**的（内置在前、文件在后）。前端要判断「这条策略改不改得动」，
        # 得知道它是文件写的还是代码里的地板 —— 所以 file_deny 单独给一份
        "deny": list(rules.deny),
        "file_deny": [n for n in rules.deny if n not in DEFAULT_DENY],
        "deny_families": list(DEFAULT_DENY_FAMILIES),
        "allow": list(rules.allow),
        "risk": dict(rules.risk),
        "max_days": dict(rules.max_days),
        "allow_custom": bool(rules.allow_custom),
        "max_per_request": int(rules.max_per_request),
        "builtin_deny": list(DEFAULT_DENY),
        "builtin_deny_families": list(DEFAULT_DENY_FAMILIES),
    }


#: 平台自己那几把身份用的策略名。放开它们等于让员工申请到平台的管理权限，
#: 所以**不接受**通过接口写进 allow —— 真要放开只能上服务器改文件，那是一道人肉门槛。
SELF_POLICY = ("wuji-panel-*", "wuji-oss-auto-*", "temp-ak-auto-*", "staff-oss-auto-*")


def write_rules(path: str, data: Mapping, *, actor: str) -> tuple:
    """把规则写回文件。

    返回 `(生效后的规则, 本次放开的内置禁用项)`。第二项非空时调用方应当发告警。

    三道闸，缺一不可：
      · **先 parse_rules 验一遍**，验不过直接抛、不落盘 —— 规则文件写坏会让整个权限列表
        加载失败，而那时员工看到的是「权限列表打不开」，没人知道是谁在什么时候写坏的
      · `allow` 里不许出现平台自己的策略（见 SELF_POLICY）
      · `allow_custom` 只认文件里原有的值，接口改不了它 —— 那个开关等于「自定义策略全放开」，
        而执行身份、发放身份的策略都是自定义策略
    """
    # 先确认它是个对象再 dict() —— 传 `[]` / `"x"` 进来时 dict() 抛的是 ValueError/TypeError，
    # 而 HTTP 层只接 DeliveryError，那会变成 500 断连而不是一句「格式不对」
    if not isinstance(data, Mapping):
        raise PolicyError("策略规则必须是对象")
    parsed = parse_rules(dict(data))
    bad = [n for n in parsed.allow if _glob(n, SELF_POLICY)]
    if bad:
        raise PolicyError(f"不能放开平台自己的策略：{'、'.join(bad)}")
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    lock = file.with_suffix(".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = load_rules(path) if file.exists() else Rules()
        before_allow = set(current.allow)
        # **只写文件自己那部分的 deny**。parse_rules 会把 DEFAULT_DENY 前置进来，
        # 原样写回去的话每存一次内置项就翻一倍（自验时 21 条变成 42 条）
        extra_deny = [n for n in parsed.deny if n not in DEFAULT_DENY]
        body = {
            "schema": RULES_SCHEMA,
            "deny": extra_deny,
            "allow": list(parsed.allow),
            "risk": dict(parsed.risk),
            "max_days": dict(parsed.max_days),
            # 刻意用**文件里原有的值**，不接受接口传进来的
            "allow_custom": bool(current.allow_custom),
            "max_per_request": int(parsed.max_per_request),
        }
        # 再验一次真正要落盘的那份：上面拼的时候可能引入了新问题
        out = parse_rules(body)
        # 放开一条**内置禁用**的策略：以前这要 SSH 到服务器改文件，现在一个管理员会话就够了。
        # 台账记了，但那是事后追溯 —— 单独标出来，让调用方能当场发告警
        opened = [
            n
            for n in set(out.allow) - set(before_allow)
            if _glob(n, DEFAULT_DENY) or _glob(n, DEFAULT_DENY_FAMILIES)
        ]
        fd2, tmp = tempfile.mkstemp(prefix=f".{file.name}.", suffix=".tmp", dir=file.parent)
        try:
            os.write(fd2, (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        finally:
            os.close(fd2)
        Path(tmp).chmod(0o600)
        Path(tmp).replace(file)
        _log_rules(file, actor, current, out)
        return out, sorted(opened)
    finally:
        os.close(fd)


def _log_rules(file: Path, actor: str, before: Rules, after: Rules) -> None:
    """改了什么、谁改的，追加一行。台账在规则旁边，不进 git（identity/ 已整个忽略）。"""
    diff = {
        "deny_added": sorted(set(after.deny) - set(before.deny)),
        "deny_removed": sorted(set(before.deny) - set(after.deny)),
        "allow_added": sorted(set(after.allow) - set(before.allow)),
        "allow_removed": sorted(set(before.allow) - set(after.allow)),
    }
    if after.max_days != before.max_days:
        diff["max_days"] = {"before": dict(before.max_days), "after": dict(after.max_days)}
    if after.risk != before.risk:
        # 只记改了哪些策略的风险等级，不记全表
        both = set(before.risk) & set(after.risk)
        keys = sorted(
            (set(before.risk) ^ set(after.risk))
            | {k for k in both if before.risk[k] != after.risk[k]}
        )
        diff["risk_changed"] = {k: after.risk.get(k, "（删除）") for k in keys}
    if not any(diff.values()):
        return
    line = {
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "actor": actor,
        **{k: v for k, v in diff.items() if v},
    }
    with contextlib.suppress(OSError):
        log = file.with_name("policy-rules.log")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        log.chmod(0o600)


# ── 采集 ─────────────────────────────────────────────────────────────────


def service_of(name: str, description: str = "") -> str:
    """策略属于哪个云产品：优先取描述里括号中的英文缩写（如「(ECS)」），否则从名字推。"""
    found = _SERVICE_IN_DESC.findall(description or "")
    if found:
        return found[-1].strip()
    base = name[len("Aliyun") :] if name.startswith("Aliyun") else name
    return _NAME_SUFFIX.sub("", base) or base


def _entry(ptype: str, name: str, description: str, updated: str, category: str = "") -> dict:
    return {
        "type": ptype,
        "name": name,
        "description": description,
        # 火山的系统策略自带产品代码（Category），比从描述里猜准
        "service": category.upper() if category else service_of(name, description),
        "updated": updated,
    }


def collect_aliyun(creds: aliyun.Credentials, *, transport=None) -> tuple:
    """返回 (账号 ID, 策略列表)。"""
    ident = aliyun.call(*aliyun.STS, "GetCallerIdentity", {}, creds=creds, transport=transport)
    account = str(ident.get("AccountId") or "")
    if not account:
        raise PolicyError("GetCallerIdentity 没有返回 AccountId")
    items = aliyun.paginate(
        *aliyun.RAM,
        "ListPolicies",
        key="Policy",
        container="Policies",
        creds=creds,
        transport=transport,
        max_pages=_MAX_PAGES,
    )
    return account, _normalize(
        (
            str(p.get("PolicyType") or ""),
            str(p.get("PolicyName") or ""),
            str(p.get("Description") or ""),
            str(p.get("UpdateDate") or ""),
        )
        for p in items
    )


def collect_volcano(creds: volcano.Credentials, *, transport=None) -> tuple:
    users = volcano.call(
        *volcano.IAM, "ListUsers", {"Limit": "1"}, creds=creds, transport=transport
    )
    accounts = {_volcano_account(u) for u in users.get("UserMetadata") or []} - {""}
    account = next(iter(accounts)) if len(accounts) == 1 else ""
    items = volcano.paginate(
        *volcano.IAM,
        "ListPolicies",
        key="PolicyMetadata",
        params={"Scope": "All", "WithServiceRolePolicy": "0"},
        creds=creds,
        transport=transport,
        max_pages=_MAX_PAGES,
    )
    if not account:
        account = next(
            (_trn_account(str(p.get("PolicyTrn") or "")) for p in items if p.get("PolicyTrn")),
            "",
        )
    return account, _normalize(
        (
            str(p.get("PolicyType") or ""),
            str(p.get("PolicyName") or ""),
            str(p.get("Description") or ""),
            str(p.get("UpdateDate") or ""),
            str(p.get("Category") or ""),
        )
        for p in items
        # 服务关联角色专用策略不是给人用的
        if not p.get("IsServiceRolePolicy")
    )


def _volcano_account(user: dict) -> str:
    account = str(user.get("AccountId") or "")
    return account or _trn_account(str(user.get("Trn") or ""))


def _trn_account(trn: str) -> str:
    match = re.match(r"^trn:iam::([0-9]+):", trn)
    return match.group(1) if match else ""


def _normalize(rows: Iterable[tuple]) -> list:
    out = {}
    for row in rows:
        ptype, name, description, updated = row[:4]
        category = row[4] if len(row) > 4 else ""
        ptype = ptype.capitalize()
        if ptype not in TYPES or not _POLICY_NAME.match(name):
            raise PolicyError(f"策略列表里有无法识别的条目：类型 {ptype!r}，名称 {name[:40]!r}")
        out[(ptype, name)] = _entry(ptype, name, description[:300], updated, category)
    return sorted(out.values(), key=lambda p: (p["type"], p["name"].lower()))


Job = tuple  # (platform, 账号提示, collect() -> (account, policies))


def build_snapshot(jobs: Iterable[Job], previous: Optional[dict] = None) -> dict:
    """采集失败的账号：上一份快照里同一个凭证来源（source）有采集成功的列表时，沿用它并标 stale，
    免得一次采集失败让已批准、待开通的单子全部因为「列表不可用」而失败。
    本次已经采到的账号不会被旧列表覆盖。"""
    fresh, failed = [], []
    for platform, hint, collect in jobs:
        try:
            account, items = collect()
            fresh.append(
                {"platform": platform, "account": account, "source": hint, "policies": items}
            )
        except (DeliveryError, OSError) as exc:
            first = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
            failed.append(
                (platform, hint, aliyun._scrub(volcano._scrub(first)) or type(exc).__name__)
            )
    collected = {(a["platform"], a["account"]) for a in fresh}
    accounts = list(fresh)
    for platform, hint, note in failed:
        good = next(
            (
                a
                for a in (previous or {}).get("accounts") or []
                if a.get("platform") == platform
                and a.get("source") == hint
                and not a.get("error")
                and a.get("policies")
                and (platform, a.get("account")) not in collected
            ),
            None,
        )
        if good is not None:
            kept = {k: v for k, v in good.items() if k not in ("stale", "error_note")}
            accounts.append({**kept, "stale": True, "error_note": note[:200]})
        else:
            accounts.append({"platform": platform, "account": hint, "error": note[:200]})
    return {"captured_at": _now(), "accounts": accounts}


def load(path: Optional[str]) -> Optional[dict]:
    if not path or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"读不了策略目录：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise PolicyError("策略目录格式不对")
    return data


def account_entry(data: Optional[dict], platform: str, account: str) -> Optional[dict]:
    """快照里某个云账号的条目；没采集返回 None。"""
    if data is None:
        return None
    for acc in data["accounts"]:
        if acc.get("platform") == platform and str(acc.get("account")) == account:
            return acc
    return None


def directory(data: Optional[dict], platform: str, account: str) -> Optional[list]:
    """某个云账号的完整策略列表；没采集或采集失败返回 None（未知，不是「没有策略」）。"""
    acc = account_entry(data, platform, account)
    if acc is None or acc.get("error") or not isinstance(acc.get("policies"), list):
        return None
    return acc["policies"]


def find(items: list, ptype: str, name: str) -> Optional[dict]:
    for p in items:
        if p.get("type") == ptype and p.get("name") == name:
            return p
    return None


#: (platform, account, 子账号) -> {策略名小写: (策略名, 来源说明)}，未知返回 None
CurrentPolicies = Callable[[str, str, str], Optional[dict]]
