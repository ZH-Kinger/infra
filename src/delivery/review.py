"""名册审核：管理员在面板上处理映射提案里没定下来的账号。

以前要手工改 `identity/manual-links.json` 再重跑 `identity people`，写错一个字符就把
账号挂到别人名下。现在面板上点，这里做校验、写人工记录、立即重建名册、记审计日志。

操作
────
  confirm  待确认的对应 → 确认给这个人
  reject   待确认的对应 → 不是这个人（以后刷新也不再推给他）
  assign   未关联的账号 → 分配给某人
  service  未关联的账号 → 标记为服务号
  undo     撤销对某个账号的人工记录，回到规则推断的结果

人工记录（manual-links.json）::

    {"links":    {"tom@wuji.tech": {"name": "…", "accounts": ["aliyun/<UID>/tom"]}},
     "rejected": {"aliyun/<UID>/tom2": ["tom@wuji.tech"]},
     "services": ["aliyun/<UID>/ci-bot"]}

重建名册不能丢身份、也不能多出身份
──────────────────────────────────
一次审核只动一个账号，所以重建前后的名册**只应在这个账号上有差别**。做法：

  1. 在锁里读原始 people.json（不套登录绑定）、映射提案、人工记录
  2. 用「提案 + 旧人工记录」重建一遍，和原始名册逐人比对账号；对不上（提案被重新生成过
     但名册没重建）就拒绝，请管理员先跑 delivery refresh —— 不猜
  3. 用「提案 + 新人工记录」重建，union_id / 工号 / 共用邮箱标记按邮箱从原始名册**原样**
     带过来；原始名册里有、新名册里没有的人（只在通讯录里、或唯一的账号刚被驳回）保留

登录绑定（bindings.json）**不写进名册正文**：删掉一条绑错的绑定必须立刻生效。靠绑定认出来
的人如果账号变了，只更新 bindings.json 里的指纹，并且检查这些账号没有被别的绑定占用、
不在被暂停的绑定里。

所有检查通过、新名册能加载，才依次写绑定、名册、人工记录。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from . import people as people_mod
from .errors import DeliveryError

OPS = ("confirm", "reject", "assign", "service", "undo")


class ReviewError(DeliveryError):
    """审核操作不合法。status 是给 HTTP 层用的状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ReviewPaths:
    proposal: str
    manual: str
    people: str
    bindings: Optional[str] = None

    @property
    def lock(self) -> Path:
        # 和 delivery refresh 用同一把锁：刷新重建名册时不能同时改
        return Path(self.people).resolve().parent / ".refresh.lock"

    @property
    def log(self) -> Path:
        return Path(self.people).resolve().parent / "review.log"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def account_key(value) -> str:
    parts = [x.strip() for x in str(value or "").split("/")]
    if len(parts) != 3 or not all(parts):
        raise ReviewError("账号应形如 平台/账号ID/用户名")
    return "/".join(parts)


def load_manual(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {"links": {}, "rejected": {}, "services": []}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"读不了人工记录：{exc}", 500) from exc
    if not isinstance(data, dict):
        raise ReviewError("人工记录格式不对", 500)
    data.setdefault("links", {})
    data.setdefault("rejected", {})
    data.setdefault("services", [])
    try:
        people_mod.apply_manual({"people": [], "unlinked": [], "services": []}, data)
    except people_mod.PeopleError as exc:
        raise ReviewError(f"人工记录格式不对：{exc}", 500) from exc
    return data


def _read_json(path: str, what: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"读不了{what}：{exc}", 500) from exc
    if not isinstance(data, dict):
        raise ReviewError(f"{what}格式不对", 500)
    return data


def _append_log(path: Path, fields: Mapping) -> None:
    # JSON 行：字段里的换行、制表符、= 都不会造成歧义或伪造日志行
    line = json.dumps({"at": _now(), **fields}, ensure_ascii=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _proposal_accounts(proposal: Mapping) -> set:
    out = set()
    for row in proposal.get("people") or []:
        for link in row.get("links") or []:
            out.add(f"{link.get('scope')}/{link.get('name')}")
    for key in ("unlinked", "services"):
        for row in proposal.get(key) or []:
            out.add(f"{row.get('scope')}/{row.get('name')}")
    return out


def _ref_key(ref) -> str:
    return f"{ref.platform}/{ref.account}/{ref.name}"


def _manual_owner(manual: Mapping, account: str) -> str:
    for email, spec in (manual.get("links") or {}).items():
        if any(account_key(a) == account for a in spec.get("accounts") or []):
            return email
    return ""


def _edit_manual(manual: dict, op: str, account: str, email: str) -> None:
    def drop_links():
        for mail in list(manual["links"]):
            spec = manual["links"][mail]
            kept = [a for a in spec.get("accounts") or [] if account_key(a) != account]
            if kept:
                spec["accounts"] = kept
            else:
                del manual["links"][mail]

    def drop_service():
        manual["services"] = [a for a in manual["services"] if account_key(a) != account]

    if op in ("confirm", "assign"):
        drop_links()
        drop_service()
        # 只撤销这个人自己的驳回：别人的驳回记录保留，撤销后规则不会再把账号推给他们
        rejected = [e for e in manual["rejected"].get(account) or [] if e != email]
        if rejected:
            manual["rejected"][account] = rejected
        else:
            manual["rejected"].pop(account, None)
        spec = manual["links"].setdefault(email, {"name": "", "accounts": []})
        spec["accounts"] = sorted({*spec.get("accounts", []), account})
    elif op == "reject":
        manual["rejected"][account] = sorted({*(manual["rejected"].get(account) or []), email})
    elif op == "service":
        drop_links()
        manual["services"] = sorted({*manual["services"], account})
    elif op == "undo":
        before = json.dumps(manual, sort_keys=True)
        drop_links()
        drop_service()
        manual["rejected"].pop(account, None)
        if json.dumps(manual, sort_keys=True) == before:
            raise ReviewError("这个账号没有人工记录，无需撤销")


def _row_refs(row: Mapping) -> tuple:
    def keys(items):
        return tuple(sorted(f"{a['platform']}/{a['account']}/{a['name']}" for a in items or []))

    return keys(row.get("accounts")), keys(row.get("pending"))


def _by_email(rows) -> dict:
    out: dict = {}
    for row in rows:
        mail = str(row.get("email") or "").strip().lower()
        if mail:
            out.setdefault(mail, []).append(row)
    return out


def _check_consistent(raw: Mapping, rebuilt: Mapping) -> None:
    """原始名册的账号必须就是「提案 + 当前人工记录」推出来的：否则重建会顺带改掉别的人。"""
    built = _by_email(rebuilt["people"])
    raw_mails = {
        str(r.get("email") or "").strip().lower()
        for r in raw.get("people") or []
        if isinstance(r, dict)
    }
    stale = [
        mail
        for mail, rows in built.items()
        if mail not in raw_mails and any(_row_refs(r) != ((), ()) for r in rows)
    ]
    for row in raw.get("people") or []:
        if not isinstance(row, dict):
            raise ReviewError("名册格式不对", 500)
        mail = str(row.get("email") or "").strip().lower()
        refs = _row_refs(row)
        hits = built.get(mail, []) if mail else []
        if len(hits) > 1 or (not hits and refs != ((), ())):
            stale.append(mail or str(row.get("name") or "?"))
        elif hits and _row_refs(hits[0]) != refs:
            stale.append(mail)
    if stale:
        raise ReviewError(
            f"名册和映射提案对不上（{len(stale)} 人），可能提案重新生成过但名册没重建。"
            "请先运行 delivery refresh 再审核",
            409,
        )


def _merge_identity(raw: Mapping, built: dict) -> None:
    """union_id 等身份字段按邮箱从原始名册原样带到新名册。"""
    raw_rows = [r for r in raw.get("people") or [] if isinstance(r, dict)]
    raw_by_mail = _by_email(raw_rows)
    seen = set()
    for row in built["people"]:
        olds = raw_by_mail.get(str(row.get("email") or "").strip().lower(), [])
        if len(olds) != 1:
            continue
        old = olds[0]
        seen.add(id(old))
        for key in ("union_id", "employee_no"):
            row[key] = str(old.get(key) or "")
        if old.get("name"):
            row["name"] = str(old["name"])
        if old.get("email_collision") is True:
            row["email_collision"] = True
    for old in raw_rows:
        if id(old) in seen or not (old.get("union_id") or old.get("email_collision") is True):
            continue
        built["people"].append(
            {
                "union_id": str(old.get("union_id") or ""),
                "name": str(old.get("name") or ""),
                "email": str(old.get("email") or ""),
                "employee_no": str(old.get("employee_no") or ""),
                "email_collision": old.get("email_collision") is True,
                "accounts": [],
                "pending": [],
            }
        )
    built["people"].sort(key=lambda p: (str(p.get("name") or ""), str(p.get("email") or "")))
    built["stats"]["people"] = len(built["people"])
    built["stats"]["with_union_id"] = sum(1 for p in built["people"] if p.get("union_id"))
    built["stats"]["with_cloud_account"] = sum(
        1 for p in built["people"] if p.get("accounts") or p.get("pending")
    )


def _rebind(
    before: people_mod.PeopleIndex, after: people_mod.PeopleIndex, raw_uids: set, bindings: dict
) -> list:
    """靠登录绑定认出来的人账号变了：更新绑定指纹，先检查不会占用别人的账号。

    新名册里这个人没有已确认账号了（比如刚撤销了误分配）：删掉这条绑定，他下次登录重新关联。
    """
    after_by_mail = _by_email([{"email": p.email, "p": p} for p in after.people])
    changed = []
    dropped = []
    for person in before.people:
        uid = person.union_id
        if not uid or uid in raw_uids or uid not in bindings["bindings"]:
            continue
        hits = after_by_mail.get(person.email.lower(), [])
        if len(hits) > 1:
            raise ReviewError(f"{person.name or person.email} 的登录绑定无法对应到新名册", 409)
        new_fp = people_mod.fingerprint(hits[0]["p"]) if hits else ()
        if new_fp == people_mod.fingerprint(person):
            continue
        if not new_fp:
            dropped.append(uid)
            continue
        for other, entry in bindings["bindings"].items():
            held = {people_mod._norm_account(a) for a in entry.get("accounts") or ()}
            if other != uid and held & set(new_fp):
                raise ReviewError("这个账号已被另一个登录绑定占用，请先核对绑定", 409)
        if set(new_fp) & before._blocked_accounts:
            raise ReviewError("这个账号卡在一条待核对的登录绑定里，请先核对绑定", 409)
        changed.append((uid, new_fp))
    for uid, fp in changed:
        bindings["bindings"][uid]["accounts"] = list(fp)
        bindings["bindings"][uid]["rebound_at"] = _now()
    for uid in dropped:
        del bindings["bindings"][uid]
    return [uid for uid, _ in changed] + [f"{uid}（已解除）" for uid in dropped]


def _check_target(before, manual: Mapping, bindings: Mapping, op: str, account: str, email: str):
    person = None
    if email:
        hits = [p for p in before.people if p.email.lower() == email]
        if not hits:
            raise ReviewError("名册里没有这个邮箱的人。没有邮箱的人暂时不能在面板上分配账号")
        if len(hits) > 1:
            raise ReviewError("名册里有多个人使用这个邮箱，请先核对名册")
        person = hits[0]
        if person.email_collision:
            raise ReviewError("通讯录里多人共用这个邮箱，不能按邮箱分配", 409)
    owner = next(
        (p for p in before.people if any(_ref_key(r) == account for r in p.accounts)), None
    )
    if op in ("confirm", "reject") and not any(_ref_key(r) == account for r in person.pending):
        raise ReviewError("这个人的待确认列表里没有这个账号，页面可能已过期，请刷新")
    if op == "reject" and _manual_owner(manual, account):
        raise ReviewError("这个账号有人工确认记录，请先撤销", 409)
    if op in ("assign", "service") and owner is not None:
        raise ReviewError(f"这个账号已确认给 {owner.name or owner.email}，请先撤销", 409)
    if op in ("confirm", "assign"):
        key = people_mod._norm_account(account)
        # 卡在待核对绑定里的账号不能分给任何人：分过去会让那条绑定悄悄解封或被绕过
        if key in before._blocked_accounts:
            raise ReviewError("这个账号卡在一条待核对的登录绑定里，请先核对绑定", 409)
        for uid, entry in bindings["bindings"].items():
            held = {people_mod._norm_account(a) for a in entry.get("accounts") or ()}
            if key in held and uid != person.union_id:
                raise ReviewError("这个账号已被另一个登录绑定占用，请先核对绑定", 409)
    return person


def apply(
    paths: ReviewPaths,
    action: Mapping,
    *,
    actor_union_id: str,
    actor_name: str = "",
) -> dict:
    op = str(action.get("op") or "")
    if op not in OPS:
        raise ReviewError(f"op 只能是 {' / '.join(OPS)}")
    account = account_key(action.get("account"))
    email = str(action.get("email") or "").strip().lower()
    if op in ("confirm", "reject", "assign") and not email:
        raise ReviewError("缺少目标人员的邮箱")
    if op in ("service", "undo"):
        email = ""

    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(paths.lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReviewError("数据正在刷新或有别人在审核，稍后再试", 409) from None

        bindings_path = Path(paths.bindings) if paths.bindings else None
        guard = (
            people_mod.bindings_lock(bindings_path) if bindings_path else contextlib.nullcontext()
        )
        with guard:
            # 一切都在锁里读：页面上看到的可能已经过期
            proposal = _read_json(paths.proposal, "映射提案")
            raw = _read_json(paths.people, "名册")
            manual = load_manual(paths.manual)
            bindings = (
                people_mod._read_bindings(bindings_path)
                if bindings_path
                else {"schema": people_mod.BINDINGS_SCHEMA, "bindings": {}}
            )
            try:
                before = people_mod.parse(raw, bindings=bindings)
            except people_mod.PeopleError as exc:
                raise ReviewError(f"名册加载失败：{exc}", 500) from exc

            if op != "undo" and account not in _proposal_accounts(proposal):
                raise ReviewError("映射提案里没有这个账号，可能已被删除，请先刷新数据")
            person = _check_target(before, manual, bindings, op, account, email)
            _check_consistent(raw, people_mod.build(people_mod.apply_manual(proposal, manual), []))

            new_manual = json.loads(json.dumps(manual))
            _edit_manual(new_manual, op, account, email)
            if email in new_manual["links"] and not new_manual["links"][email].get("name"):
                new_manual["links"][email]["name"] = person.name if person else ""
            built = people_mod.build(people_mod.apply_manual(proposal, new_manual), [])
            _merge_identity(raw, built)

            new_bindings = json.loads(json.dumps(bindings))
            try:
                after = people_mod.parse(built, bindings=bindings)
                raw_uids = {str(r.get("union_id") or "") for r in raw.get("people") or []}
                rebound = _rebind(before, after, raw_uids - {""}, new_bindings)
                people_mod.parse(built, bindings=new_bindings)  # 自检：写出去的必须能加载
            except people_mod.PeopleError as exc:
                raise ReviewError(f"重建后的名册加载失败：{exc}", 500) from exc

            # 人工记录先写：后面任何一步失败，下一次刷新都会按新人工记录把名册补齐
            people_mod.write_private_json(Path(paths.manual), new_manual)
            if bindings_path and rebound:
                people_mod.write_private_json(bindings_path, new_bindings)
            people_mod.write_private_json(Path(paths.people), built)
        try:
            _append_log(
                paths.log,
                {
                    "actor": actor_union_id,
                    "actor_name": actor_name,
                    "op": op,
                    "account": account,
                    "email": email,
                    "rebound": rebound,
                },
            )
        except OSError as exc:  # 数据已经写好，日志失败不能让管理员以为没保存
            print(f"[review] 审计日志写入失败：{type(exc).__name__}", file=sys.stderr)
        return {"op": op, "account": account, "rebound": rebound}
    finally:
        os.close(lock_fd)


def add_link(paths: ReviewPaths, email: str, account: str, *, actor: str) -> None:
    """开账号申请执行成功后：把新账号人工对应给申请人。名册在下一次刷新时生效。

    账号已经有人工记录（对应给别人、驳回、服务号）时拒绝，不覆盖。
    """
    account = account_key(account)
    email = email.strip().lower()
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(paths.lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        manual = load_manual(paths.manual)
        owner = _manual_owner(manual, account)
        if (
            (owner and owner != email)
            or account in manual["rejected"]
            or account in manual["services"]
        ):
            raise ReviewError(
                f"账号 {account} 已有人工记录，未自动对应，请管理员在名册审核里处理", 409
            )
        spec = manual["links"].setdefault(email, {"name": "", "accounts": []})
        spec["accounts"] = sorted({*spec.get("accounts", []), account})
        people_mod.apply_manual({"people": [], "unlinked": [], "services": []}, manual)
        people_mod.write_private_json(Path(paths.manual), manual)
    finally:
        os.close(lock_fd)
    try:
        _append_log(
            paths.log,
            {"actor": actor, "op": "link_new_account", "account": account, "email": email},
        )
    except OSError as exc:
        print(f"[review] 审计日志写入失败：{type(exc).__name__}", file=sys.stderr)


def records(path: str) -> list:
    """人工记录的扁平列表，给面板「撤销」用。"""
    manual = load_manual(path)
    out = []
    for email, spec in sorted(manual["links"].items()):
        for acc in spec.get("accounts") or []:
            out.append(
                {"account": acc, "kind": "link", "email": email, "name": spec.get("name", "")}
            )
    for acc, emails in sorted(manual["rejected"].items()):
        out.append(
            {"account": acc, "kind": "rejected", "email": ", ".join(map(str, emails)), "name": ""}
        )
    for acc in sorted(manual["services"]):
        out.append({"account": acc, "kind": "service", "email": "", "name": ""})
    return out
