"""人员名册：一个人（飞书 union_id）→ 他在各云上的账号。

认人规则照 WUJI IAM 接入规范（2026-09-07）
────────────────────────────────────────
**唯一绑定键是 `union_id`。** 邮箱、工号、姓名都会变（公司已有 27 个 username 漂移，
两次登录事故），只能当展示属性。邮箱唯一允许的用途是**首次登录时辅助关联一次**，
关联成功立刻落 `union_id`，之后只认 `union_id`。

名册文件 `identity/people.json`（gitignored，含全员邮箱）由 CLI 生成::

    {
      "schema": "wuji-people@1",
      "generated_at": "…",
      "people": [
        {"union_id": "on_…" 或 "", "name": "…", "email": "…", "employee_no": "…",
         "accounts": [{"platform": "aliyun", "account": "<UID>", "name": "zhangsan"}],
         "pending":  [{"platform": "volcano", "account": "<UID>", "name": "SanZhang",
                       "status": "review"}]}
      ],
      "unlinked": [{"platform": …, "account": …, "name": …, "display_name": …,
                    "kind": "service" | "unknown", "reason": "…"}]
    }

`accounts` 只放**已确认**的对应；待确认、有冲突的放 `pending`。用户视图只展示
`accounts`——映射错一条，用户看到的就是别人的权限清单。

首次登录落下的绑定写在另一个文件 `identity/bindings.json`，不改名册：名册是生成物、
会被重新生成覆盖；绑定是登录事实，丢了就要重新认一遍人。
"""

from __future__ import annotations

import datetime
import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .errors import DeliveryError

SCHEMA = "wuji-people@1"
BINDINGS_SCHEMA = "wuji-bindings@2"

BIND_UNION_ID = "union_id"
BIND_NOW = "bound_now"
BIND_NONE = "unbound"
BIND_CONFLICT = "conflict"


class PeopleError(DeliveryError):
    """名册或绑定文件不合法。"""


@dataclass(frozen=True)
class AccountRef:
    platform: str
    account: str
    name: str
    status: str = "confirmed"

    @property
    def scope(self) -> str:
        return f"{self.platform}/{self.account}"


@dataclass(frozen=True)
class Person:
    name: str
    email: str = ""
    union_id: str = ""
    employee_no: str = ""
    accounts: tuple = ()
    pending: tuple = ()
    #: 通讯录里有多人共用这个企业邮箱。这种人不能靠邮箱认领，只能由管理员补 union_id。
    email_collision: bool = False

    @property
    def key(self) -> str:
        """管理员接口里指代这个人的键。没绑 union_id 的人只能先用邮箱指代。"""
        return self.union_id or f"email:{self.email.lower()}"


def _norm_account(value: str) -> str:
    """`平台/账号/用户名` 只把用户名转小写，与 fingerprint 一致。"""
    platform, _, rest = str(value).partition("/")
    account, _, name = rest.partition("/")
    return f"{platform}/{account}/{name.lower()}"


def fingerprint(person: Person) -> tuple:
    """这个人名下**已确认**云账号的指纹：用来确认「绑定时的那个人」和「现在名册里这一行」是同一个人。

    只算 confirmed：pending 不展示给本人，被驳回或移走是常事，算进来会让人频繁被锁。
    没有已确认账号的人指纹为空——对他们套用绑定没有意义，也无从校验。
    指纹里是云用户名（小写），用户名可能被复用，所以套用绑定时还要求邮箱一致（见 _apply_bindings）。
    """
    # 用户名按小写：云上是否区分大小写没有定论，按不区分处理，宁可多拦（与共用账号判定一致）
    return tuple(sorted(f"{r.platform}/{r.account}/{r.name.lower()}" for r in person.accounts))


@dataclass(frozen=True)
class Unlinked:
    platform: str
    account: str
    name: str
    display_name: str = ""
    kind: str = "unknown"
    reason: str = ""


@dataclass(frozen=True)
class Resolution:
    person: Optional[Person]
    binding: str
    note: str = ""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _str(value, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PeopleError(f"{what} 必须是字符串")
    return value.strip()


def _refs(items, what: str, *, default_status: str) -> tuple:
    if items is None:
        return ()
    if not isinstance(items, list):
        raise PeopleError(f"{what} 必须是数组")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PeopleError(f"{what}[{i}] 必须是对象")
        ref = AccountRef(
            platform=_str(item.get("platform"), f"{what}[{i}].platform"),
            account=_str(item.get("account"), f"{what}[{i}].account"),
            name=_str(item.get("name"), f"{what}[{i}].name"),
            status=_str(item.get("status"), f"{what}[{i}].status") or default_status,
        )
        if not (ref.platform and ref.account and ref.name):
            raise PeopleError(f"{what}[{i}] 缺 platform/account/name")
        out.append(ref)
    return tuple(out)


class PeopleIndex:
    """名册 + 绑定，按 union_id 查人。线程安全（开发服务器是多线程的）。"""

    def __init__(
        self,
        people: Iterable[Person] = (),
        unlinked: Iterable[Unlinked] = (),
        *,
        generated_at: str = "",
        bindings_path: Optional[str] = None,
        warnings: Iterable[str] = (),
        blocked_union_ids: Iterable[str] = (),
        blocked_accounts: Iterable[str] = (),
    ):
        self.generated_at = generated_at
        self.unlinked = tuple(unlinked)
        self.warnings = list(warnings)
        self._bindings_path = bindings_path
        self._lock = threading.Lock()
        self._people = list(people)
        #: 不能再按邮箱关联的 union_id：绑定记录与名册对不上的、名册里重复的。
        #: 这些人登录一律「冲突」，交给管理员，不走邮箱分支。
        self._blocked = set(blocked_union_ids)
        #: 被拦下的绑定当时认领的账号。含这些账号的行不能被别的身份按邮箱认领——
        #: 否则「账号有增减 → 原主被拦 → 邮箱复用给新人」这条路又通了。
        self._blocked_accounts = set(blocked_accounts)
        self._reindex()

    # ── 索引 ──────────────────────────────────────────────────────────────
    def _reindex(self) -> None:
        by_uid: dict = {}
        by_email: dict = {}
        for p in self._people:
            if p.union_id:
                if p.union_id in by_uid:
                    # 同一个 union_id 挂两个人：名册本身坏了。两个都不认，比认错一个安全。
                    msg = f"union_id {p.union_id} 在名册里出现多次，已全部停用"
                    if msg not in self.warnings:
                        self.warnings.append(msg)
                    by_uid[p.union_id] = None
                    self._blocked.add(p.union_id)
                else:
                    by_uid[p.union_id] = p
            if p.email:
                by_email.setdefault(p.email.lower(), []).append(p)
        self._by_uid = {k: v for k, v in by_uid.items() if v is not None}
        self._by_email = by_email

    @property
    def people(self) -> tuple:
        with self._lock:
            return tuple(self._people)

    def claim_blocked(self, person: Person) -> bool:
        """这个人名下是否有账号卡在一条对不上的历史绑定里。"""
        with self._lock:
            return bool(set(fingerprint(person)) & self._blocked_accounts)

    def by_key(self, key: str) -> Optional[Person]:
        with self._lock:
            if key.startswith("email:"):
                hits = self._by_email.get(key[6:].lower(), [])
                unbound = [p for p in hits if not p.union_id]
                return unbound[0] if len(unbound) == 1 else None
            return self._by_uid.get(key)

    # ── 登录认人 ──────────────────────────────────────────────────────────
    def resolve(self, *, union_id: str, enterprise_email: str = "") -> Resolution:
        """登录者是谁。

        只接收**企业邮箱**做辅助：个人联系邮箱不是公司分配的，不能拿来认领公司账号。
        """
        if not union_id:
            return Resolution(None, BIND_NONE, "登录信息里没有 union_id，无法认人。")
        with self._lock:
            hit = self._by_uid.get(union_id)
            if hit is not None:
                return Resolution(hit, BIND_UNION_ID)
            if union_id in self._blocked:
                return Resolution(
                    None,
                    BIND_CONFLICT,
                    "你的身份记录与人员名册对不上（可能名册重新生成后账号有变化），"
                    "为避免认错人已暂停自动关联，请联系管理员核对。",
                )
            email = (enterprise_email or "").strip().lower()
            if not email:
                return Resolution(
                    None,
                    BIND_NONE,
                    "名册里还没有你的 union_id，且飞书没有返回企业邮箱，无法自动关联。"
                    "请把本页显示的 union_id 发给管理员登记。",
                )
            candidates = self._by_email.get(email, [])
            if not candidates:
                return Resolution(
                    None,
                    BIND_NONE,
                    "你目前没有阿里云或火山账号。如果你确认有账号，可能是还没对应到你，请联系管理员。",
                )
            if len(candidates) > 1:
                return Resolution(
                    None, BIND_CONFLICT, "你的企业邮箱在名册里对应了多个人，需要管理员处理。"
                )
            person = candidates[0]
            if person.email_collision:
                return Resolution(
                    None,
                    BIND_CONFLICT,
                    "通讯录里有多人共用你的企业邮箱，不能按邮箱自动关联，请联系管理员登记。",
                )
            if person.union_id:
                # 邮箱对上了，但这个人已经绑了别的 union_id：多半是邮箱被复用给了新同事。
                # 这正是规范禁止拿邮箱认人的原因——拒绝，不覆盖。
                return Resolution(
                    None,
                    BIND_CONFLICT,
                    "你的企业邮箱对应的账号已绑定到另一个飞书身份，登录被拒绝。请联系管理员核对。",
                )
            if set(fingerprint(person)) & self._blocked_accounts:
                return Resolution(
                    None,
                    BIND_CONFLICT,
                    "这组云账号有一条待核对的历史绑定，暂不能自动关联，请联系管理员。",
                )
            if not fingerprint(person):
                # 没有云账号的人：没有可看的数据，也无从做指纹校验，不落绑定
                return Resolution(
                    None,
                    BIND_NONE,
                    "你目前没有阿里云或火山账号。如果你确认有账号，可能是还没对应到你，请联系管理员。",
                )
            bound = replace(person, union_id=union_id)
            if not self._persist_binding(bound):
                return Resolution(
                    None,
                    BIND_CONFLICT,
                    "这组云账号已经被另一个飞书身份认领，登录被拒绝。请联系管理员核对。",
                )
            self._people = [bound if p is person else p for p in self._people]
            self._reindex()
            return Resolution(bound, BIND_NOW)

    def _persist_binding(self, person: Person) -> bool:
        """落绑定。以 union_id 为键，连同当时的账号指纹一起存。

        写之前复核文件里的现状（另一个进程或另一份索引可能刚写过）：同一指纹已被别的
        union_id 认领 → 不覆盖，返回 False。
        """
        if not self._bindings_path:
            return True
        path = Path(self._bindings_path)
        data = _read_bindings(path)
        fp = list(fingerprint(person))
        for uid, entry in data["bindings"].items():
            held = {_norm_account(a) for a in entry.get("accounts") or ()}
            if uid != person.union_id and held & set(fp):
                return False  # 这组账号（的一部分）已被别的身份认领
            if uid == person.union_id and sorted(held) != fp:
                return False  # 本索引已过期：文件里这个人的绑定和我们看到的不一样
        data["bindings"][person.union_id] = {
            "accounts": fp,
            "email": person.email.lower(),
            "name": person.name,
            "bound_at": _now(),
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        tmp.replace(path)
        # 审计：谁在什么时候凭哪个企业邮箱认领了哪个 union_id。追加写，不改旧行。
        log = path.with_suffix(".log")
        line = f"{_now()}\tbind\tunion_id={person.union_id}\temail={person.email.lower()}\n"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True


def _read_bindings(path: Path) -> dict:
    if not path.exists():
        return {"schema": BINDINGS_SCHEMA, "bindings": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeopleError(f"读不了绑定文件 {path}：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict):
        raise PeopleError(f"{path} 格式不对，缺 bindings 对象")
    if data.get("schema") != BINDINGS_SCHEMA:
        raise PeopleError(
            f"{path} 的 schema 不是 {BINDINGS_SCHEMA}。旧版按邮箱记绑定，不能安全迁移，"
            "请删除该文件，让大家重新登录一次"
        )
    for uid, entry in data["bindings"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("accounts"), list):
            raise PeopleError(f"{path} 里 {uid} 的绑定条目格式不对")
    return data


def parse(
    data: Mapping, *, bindings: Optional[Mapping] = None, bindings_path: Optional[str] = None
) -> PeopleIndex:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise PeopleError(f"名册 schema 必须是 {SCHEMA}")
    rows = data.get("people")
    if not isinstance(rows, list):
        raise PeopleError("名册缺 people 数组")
    bound = (bindings or {}).get("bindings", {}) if bindings else {}
    warnings = []
    people = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PeopleError(f"people[{i}] 必须是对象")
        person = Person(
            name=_str(row.get("name"), f"people[{i}].name"),
            email=_str(row.get("email"), f"people[{i}].email"),
            union_id=_str(row.get("union_id"), f"people[{i}].union_id"),
            employee_no=_str(row.get("employee_no"), f"people[{i}].employee_no"),
            accounts=_refs(
                row.get("accounts"), f"people[{i}].accounts", default_status="confirmed"
            ),
            pending=_refs(row.get("pending"), f"people[{i}].pending", default_status="review"),
            email_collision=row.get("email_collision") is True,
        )
        people.append(person)
    people = _demote_shared_accounts(people, warnings)
    for uid, entry in bound.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("accounts"), list):
            raise PeopleError(f"绑定条目 {uid} 格式不对")
    people, blocked, blocked_accounts = _apply_bindings(people, bound, warnings)
    unlinked = []
    for i, row in enumerate(data.get("unlinked") or []):
        if not isinstance(row, dict):
            raise PeopleError(f"unlinked[{i}] 必须是对象")
        unlinked.append(
            Unlinked(
                platform=_str(row.get("platform"), f"unlinked[{i}].platform"),
                account=_str(row.get("account"), f"unlinked[{i}].account"),
                name=_str(row.get("name"), f"unlinked[{i}].name"),
                display_name=_str(row.get("display_name"), f"unlinked[{i}].display_name"),
                kind=_str(row.get("kind"), f"unlinked[{i}].kind") or "unknown",
                reason=_str(row.get("reason"), f"unlinked[{i}].reason"),
            )
        )
    return PeopleIndex(
        people,
        unlinked,
        generated_at=_str(data.get("generated_at"), "generated_at"),
        bindings_path=bindings_path,
        warnings=warnings,
        blocked_union_ids=blocked,
        blocked_accounts=blocked_accounts,
    )


def _demote_shared_accounts(people: list, warnings: list) -> list:
    """同一个云账号被确认给了多个人：名册本身错了（常见于人工对应写错）。

    全部降为 pending 并告警——用户视图只给已确认的账号，这样谁都看不到这个号的权限，
    IAM 导出也不会把它写进任何人的属性。按小写比对：宁可多拦。
    """
    owners: dict = {}
    for i, p in enumerate(people):
        for r in p.accounts:
            owners.setdefault((r.platform, r.account, r.name.lower()), set()).add(i)
    shared = {k for k, v in owners.items() if len(v) > 1}
    if not shared:
        return people
    out = []
    for p in people:
        moved = [r for r in p.accounts if (r.platform, r.account, r.name.lower()) in shared]
        if moved:
            p = replace(
                p,
                accounts=tuple(r for r in p.accounts if r not in moved),
                pending=p.pending + tuple(replace(r, status="shared") for r in moved),
            )
        out.append(p)
    for platform, account, name in sorted(shared):
        warnings.append(f"{platform}/{account}/{name} 被确认给了多个人，已全部降为待确认，请核对")
    return out


def _apply_bindings(people: list, bound: Mapping, warnings: list) -> tuple:
    """把登录绑定套回名册。**按账号指纹对人，不按邮箱。**

    - 名册里已有这个 union_id：指纹一致就什么都不做；不一致只告警（以名册为准）。
    - 名册里没有：找「未绑定、指纹完全一致」的唯一一行套上。
    - 找不到或不唯一：不套，这个 union_id 进 blocked——他再登录直接判冲突，
      不允许再走一次邮箱关联（那正是规范要堵的路）。
    """
    blocked = set()
    blocked_accounts = set()
    by_uid = {p.union_id: p for p in people if p.union_id}
    for uid, entry in bound.items():
        fp = tuple(sorted(_norm_account(a) for a in entry.get("accounts") or ()))
        who = str(entry.get("name") or uid)
        if uid in by_uid:
            if fingerprint(by_uid[uid]) != fp:
                warnings.append(f"{who}：名册里的云账号与登录绑定时不一致，以名册为准，请核对")
            continue
        mail = str(entry.get("email") or "").strip().lower()
        # 指纹 + 绑定时的邮箱都一致才套用。用户名会被复用（改名后旧名分给新人），邮箱也会被
        # 复用；两者同时对上同一个未绑定的人才算。邮箱在这里只缩小范围，不单独认人。
        hits = [
            i
            for i, p in enumerate(people)
            if not p.union_id and fp and mail and fingerprint(p) == fp and p.email.lower() == mail
        ]
        if len(hits) == 1:
            people[hits[0]] = replace(people[hits[0]], union_id=uid)
        else:
            blocked.add(uid)
            blocked_accounts.update(fp)
            warnings.append(
                f"{who}：登录绑定与名册对不上（名册重新生成后账号有变化？），已暂停此人自动关联"
            )
    return people, blocked, blocked_accounts


def load(path: str, *, bindings_path: Optional[str] = None) -> PeopleIndex:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise PeopleError(f"读不了名册 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise PeopleError(f"{path} 不是合法 JSON：{exc}") from exc
    bindings = _read_bindings(Path(bindings_path)) if bindings_path else None
    return parse(data, bindings=bindings, bindings_path=bindings_path)


# ── 生成名册 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DirectoryEntry:
    """通讯录里的一个人。来自飞书通讯录接口或 IT 从 IAM 导出的对照表。"""

    union_id: str
    name: str
    enterprise_email: str = ""
    employee_no: str = ""
    extra: dict = field(default_factory=dict, compare=False)


def apply_manual(proposal: Mapping, manual: Optional[Mapping]) -> dict:
    """人工确认的对应，覆盖映射提案。

    文件 `identity/manual-links.json`（gitignored）::

        {"links": {"tom@wuji.tech": {"name": "…",
                   "accounts": ["volcano/<UID>/TomLee", "aliyun/<UID>/tomlee"]}}}

    列出的账号从提案里所有位置（别人的 links、unlinked、services）摘掉，
    以 confirmed 挂到这个邮箱名下。人工确认优先于任何规则推断。
    """
    if not manual:
        return dict(proposal)
    links = manual.get("links") if isinstance(manual, dict) else None
    if not isinstance(links, dict):
        raise PeopleError('manual-links 格式应为 {"links": {邮箱: {"accounts": [...]}}}')
    wanted: dict = {}
    names: dict = {}
    for email, spec in links.items():
        if not isinstance(email, str) or not email.strip():
            raise PeopleError("manual-links 的键必须是非空邮箱字符串")
        accounts = spec.get("accounts") if isinstance(spec, dict) else None
        if not isinstance(accounts, list) or not accounts:
            raise PeopleError(f"manual-links 里 {email} 缺 accounts 数组")
        mail = email.strip().lower()
        names[mail] = str(spec.get("name") or "")
        for item in accounts:
            parts = [x.strip() for x in str(item).split("/")]
            if len(parts) != 3 or not all(parts):
                raise PeopleError(
                    f"manual-links 里 {email} 的账号 {item!r} 应形如 平台/账号ID/用户名"
                )
            key = (f"{parts[0]}/{parts[1]}", parts[2])
            if key in wanted and wanted[key] != mail:
                raise PeopleError(f"账号 {item} 在 manual-links 里被分给了多个人")
            wanted[key] = mail

    def keep(scope, name):
        return (str(scope or "").strip(), str(name or "").strip()) not in wanted

    out = dict(proposal)
    people = []
    for row in proposal.get("people") or []:
        row = dict(row)
        row["links"] = [
            lk for lk in row.get("links") or [] if keep(lk.get("scope"), lk.get("name"))
        ]
        people.append(row)
    out["unlinked"] = [
        r for r in proposal.get("unlinked") or [] if keep(r.get("scope"), r.get("name"))
    ]
    out["services"] = [
        r for r in proposal.get("services") or [] if keep(r.get("scope"), r.get("name"))
    ]
    by_email = {str(r.get("email") or "").strip().lower(): r for r in people}
    for (scope, name), mail in sorted(wanted.items()):
        target = by_email.get(mail)
        if target is None:
            target = {"email": mail, "links": []}
            people.append(target)
            by_email[mail] = target
        target["links"].append(
            {
                "scope": scope,
                "name": name,
                "display_name": names[mail],
                "status": "confirmed",
                "evidence": ["人工确认"],
            }
        )
    out["people"] = [r for r in people if r.get("links")]
    return out


def build(proposal: Mapping, directory: Iterable[DirectoryEntry] = ()) -> dict:
    """映射提案（按邮箱组织）+ 通讯录（带 union_id）→ 名册。

    按企业邮箱回填 union_id——这是规范里允许的「存量回填」用法。回填不上的人
    `union_id` 留空，等他首次登录时再关联，或由管理员补。
    """
    domain = str(proposal.get("domain") or "").strip().lower().lstrip("@")
    by_email: dict = {}
    dup = set()
    directory = list(directory)
    for entry in directory:
        mail = entry.enterprise_email.strip().lower()
        if not mail or not entry.union_id:
            continue
        if not domain or not mail.endswith(f"@{domain}"):
            # 没有公司域就无从区分企业邮箱和个人联系邮箱，宁可不回填
            continue
        if mail in by_email and by_email[mail].union_id != entry.union_id:
            dup.add(mail)
        by_email[mail] = entry

    people = []
    for row in proposal.get("people") or []:
        email = str(row.get("email") or "").strip().lower()
        accounts, pending = [], []
        for link in row.get("links") or []:
            platform, _, account = str(link.get("scope") or "").partition("/")
            ref = {"platform": platform, "account": account, "name": str(link.get("name") or "")}
            status = str(link.get("status") or "")
            if status == "confirmed":
                accounts.append(ref)
            else:
                pending.append({**ref, "status": status or "review"})
        entry = None if email in dup else by_email.get(email)
        people.append(
            {
                "email_collision": email in dup,
                "union_id": entry.union_id if entry else "",
                "name": (entry.name if entry else "") or _display_of(row),
                "email": email,
                "employee_no": entry.employee_no if entry else "",
                "accounts": accounts,
                "pending": pending,
            }
        )

    # 通讯录里没有任何云账号的人也进名册：不是所有人都有阿里云/火山账号，
    # 他们登录后应该看到「你目前没有云账号」，而不是「名册里找不到你」。
    listed = {p["email"] for p in people}
    listed_uids = {p["union_id"] for p in people if p["union_id"]}
    for entry in directory:
        mail = entry.enterprise_email.strip().lower()
        if not entry.union_id or entry.union_id in listed_uids or (mail and mail in listed):
            continue
        listed_uids.add(entry.union_id)
        people.append(
            {
                "union_id": entry.union_id,
                "name": entry.name,
                "email": mail,
                "employee_no": entry.employee_no,
                "accounts": [],
                "pending": [],
            }
        )

    unlinked = []
    for row in proposal.get("unlinked") or []:
        platform, _, account = str(row.get("scope") or "").partition("/")
        unlinked.append(
            {
                "platform": platform,
                "account": account,
                "name": str(row.get("name") or ""),
                "display_name": str(row.get("display_name") or ""),
                "kind": "unknown",
                "reason": str(row.get("reason") or ""),
            }
        )
    for row in proposal.get("services") or []:
        platform, _, account = str(row.get("scope") or "").partition("/")
        unlinked.append(
            {
                "platform": platform,
                "account": account,
                "name": str(row.get("name") or ""),
                "display_name": "",
                "kind": "service",
                "reason": "服务号",
            }
        )
    people.sort(key=lambda p: (p["name"], p["email"]))
    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "people": people,
        "unlinked": unlinked,
        "stats": {
            "people": len(people),
            "with_union_id": sum(1 for p in people if p["union_id"]),
            "with_cloud_account": sum(1 for p in people if p["accounts"] or p["pending"]),
            "email_collisions_in_directory": sorted(dup),
        },
    }


def _display_of(row: Mapping) -> str:
    for link in row.get("links") or []:
        if link.get("display_name"):
            return str(link["display_name"])
    return str(row.get("email") or "").split("@")[0]


__all__ = [
    "BIND_CONFLICT",
    "BIND_NONE",
    "BIND_NOW",
    "BIND_UNION_ID",
    "AccountRef",
    "DirectoryEntry",
    "PeopleError",
    "PeopleIndex",
    "Person",
    "Resolution",
    "Unlinked",
    "apply_manual",
    "build",
    "load",
    "parse",
]
