"""离职：检测到就停用，管理员确认了就删号。**任何一步都不动数据。**

流程
────
  · **强信号**（飞书状态是已离职，或 IT 的 IAM 标了离职）→ 定时任务自动停用他的云账号
    （关登录 + 禁 AK，都能恢复），记一条 `disabled`，发卡片给管理员。
  · **弱信号**（通讯录里按 union_id、公司邮箱都找不到）→ 不动账号，记一条 `suspect`，
    发卡片。飞书对「已离职」和「不在应用可见范围内」给的是同一个结果，自动停会误伤在职的人。
  · 管理员在面板上点「确认删除」→ 删号（`deleted`）；点「恢复」→ 把这次关掉的开回去
    （`restored`），**之后不再自动停这个号**，除非管理员删掉这条记录。

只删**账号本身**：他桶里的文件、数据集、实例一概不碰。删用户不会连带删数据 ——
那些东西属于主账号，不属于子用户。

谁不能停
────────
只停名册里**确认归属某个人**的号。服务号、面板自己的三个身份、`power-application-user`
这类名字在这里硬拦一道（`PROTECTED`），云上的执行身份策略里也有 Deny 再拦一道 ——
两边各自独立，任何一边写漏了另一边还在。现网策略的副本在 `deploy/panel/cloud-policies/`。

强信号只认两个：飞书状态 `is_resigned`、IT 的 IAM `is_active=false`。飞书的「冻结」
「退出企业」只提醒 —— 冻结多半是长假或临时封禁，停了会让在跑的任务断掉。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .errors import DeliveryError

FILENAME = "offboard.json"

#: 能被停、被删的平台。九章没有接口，只能提醒人去控制台
PLATFORMS = ("aliyun", "volcano")

#: 一轮最多自动停几个人。**超了就一个都不停，只提醒**：IT 的接口或飞书抖一下，
#: 可能一次性把一批在职的人标成离职，那时候停得越多事故越大
MAX_AUTO_PEOPLE = 3

#: 永远不自动停、不删的号。云上的 Deny 策略是第二道
#: 和云上执行身份策略里的 Deny 名单对齐（阿里、火山两份）。**改一边要改另一边**
PROTECTED = re.compile(r"\A(panel-|power-|tempak|wuji-|rl-|finance\Z|data-tran\Z)", re.IGNORECASE)

DISABLED = "disabled"
SUSPECT = "suspect"
DELETED = "deleted"
RESTORED = "restored"
DISMISSED = "dismissed"
#: 还等着管理员拿主意的两种
PENDING = (DISABLED, SUSPECT)


class OffboardError(DeliveryError):
    """离职处理出错。"""


def path_beside(people_path: str) -> Path:
    return Path(people_path).resolve().parent / FILENAME


def key_of(platform: str, account: str, user: str) -> str:
    return f"{platform}/{account}/{user}"


def cloud_user(value: str) -> str:
    """SSO 属性值 → 云上的用户名。阿里是 `name@<uid>.onaliyun.com`，火山是裸名。"""
    return str(value or "").split("@", 1)[0].strip()


def load(path) -> dict:
    """`{key: 记录}`。文件不在 = 一条都没有。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OffboardError(f"离职记录读不了 {p}：{exc}") from exc
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        raise OffboardError(f"{p} 格式不对：缺 records")
    return records


@contextlib.contextmanager
def _locked(path):
    """读改写全程持锁：定时任务和面板是两个进程，会同时改这份文件。"""
    import fcntl

    target = Path(path)
    with Path(f"{target}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = load(target)
        box = {"records": records}
        yield box
        _write(target, box["records"])


def _write(target: Path, records: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".offboard-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"records": records}, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        Path(tmp).chmod(0o600)
        Path(tmp).replace(target)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def targets_of(person, platforms: Iterable[str] = PLATFORMS) -> list:
    """这个人名下可以停的号：`[(platform, account, user)]`。只取已确认归属的。"""
    out = []
    for ref in getattr(person, "accounts", ()) or ():
        if ref.platform not in platforms:
            continue
        if getattr(ref, "status", "confirmed") != "confirmed":
            continue
        if PROTECTED.match(ref.name):
            continue
        out.append((ref.platform, ref.account, ref.name))
    return out


def resigned(status) -> str:
    """飞书状态里**只有「已离职」算强信号**。冻结是暂停（长假、临时封禁），退出企业语义不清，
    这两种停了号会误伤在职的人 —— 只提醒，见 `weak_statuses`。"""
    return "飞书状态：已离职" if (status or {}).get("is_resigned") else ""


def weak_statuses(roster, statuses) -> list:
    """飞书状态是冻结 / 退出企业、但不是离职的人 → `[(person, 说明)]`，只提醒不停。"""
    by_uid = {p.union_id: p for p in roster if getattr(p, "union_id", "")}
    out = []
    for uid, st in (statuses or {}).items():
        if not st or st.get("is_resigned") or uid not in by_uid:
            continue
        if st.get("is_frozen"):
            out.append((by_uid[uid], "飞书状态：账号被冻结（没有自动停用）"))
        elif st.get("is_exited"):
            out.append((by_uid[uid], "飞书状态：已退出企业（没有自动停用）"))
    return out


def strong_candidates(roster, *, drift_rows=(), statuses=None, gone=resigned) -> list:
    """强信号的人 → `[(person, signal)]`。

    `drift_rows`：`(entry, drift)`，IT 的 IAM 标了离职的（按 union_id 找名册里的人）。
    `statuses`：`{union_id: 飞书状态}`，`gone(status)` 返回非空说明已离职。
    """
    by_uid = {p.union_id: p for p in roster if getattr(p, "union_id", "")}
    out, seen = [], set()
    for _entry, d in drift_rows:
        p = by_uid.get(str(d.get("union_id") or ""))
        if p is not None and p.union_id not in seen:
            seen.add(p.union_id)
            out.append((p, "IT 的 IAM 标记离职"))
    for uid, st in (statuses or {}).items():
        if st is None or uid in seen or gone is None:
            continue
        why = gone(st)
        if why and uid in by_uid:
            seen.add(uid)
            out.append((by_uid[uid], why))
    return out


def auto_disable(
    path,
    candidates,
    executor: Callable,
    *,
    max_people: int = MAX_AUTO_PEOPLE,
    log: Optional[Callable] = None,
) -> dict:
    """强信号的人：停用他的号，记 `disabled`。返回 `{done, skipped, failed, held}`。

    `executor(platform, account)` 返回执行身份（带 `disable_user`）。

    **已经有记录的号不再动**：`restored` 表示管理员判过「这人没走」，再停就是跟人对着干；
    `disabled` / `deleted` 已经处理过了。
    """
    report = {"done": [], "skipped": [], "failed": [], "held": []}
    with _locked(path) as box:
        records = box["records"]
        todo = []

        def open_(t) -> bool:
            # 没记录的、只是嫌疑的（强信号来了就升级）、上次停到一半的，这轮都要停。
            # `restored` / `dismissed`（管理员判过没走）和 `deleted` 不再碰
            rec = records.get(key_of(*t))
            if rec is None:
                return True
            if rec.get("state") == SUSPECT:
                return True
            return rec.get("state") == DISABLED and bool(rec.get("incomplete"))

        for person, signal in candidates:
            fresh = [t for t in targets_of(person) if open_(t)]
            if fresh:
                todo.append((person, signal, fresh))
            else:
                report["skipped"].append(person.name)
        if len(todo) > max_people:
            # 一个都不停，全部交给人。见 MAX_AUTO_PEOPLE。**记成嫌疑**，面板上才有东西可点
            report["held"] = [p.name for p, _s, _t in todo]
            for person, signal, fresh in todo:
                for t in fresh:
                    records.setdefault(
                        key_of(*t),
                        _suspect(person, t, f"{signal}（这一轮判离职的人太多，没有自动停用）"),
                    )
            return report
        for person, signal, fresh in todo:
            for platform, account, user in fresh:
                rec = {
                    "platform": platform,
                    "account": account,
                    "user": user,
                    "person": person.name,
                    "email": person.email,
                    "union_id": person.union_id,
                    "signal": signal,
                    "at": _now(),
                }
                k = key_of(platform, account, user)
                old = records.get(k) or {}
                # 上次停到一半的：已经关掉的登录 / AK 要并进来，恢复时才开得回去
                was = old.get("state") == DISABLED
                had_login = bool(old.get("login")) if was else False
                had_keys = list(old.get("keys") or []) if was else []
                try:
                    got = executor(platform, account).disable_user(user)
                except Exception as exc:  # noqa: BLE001 — 一个号失败不挡其余
                    error = f"{type(exc).__name__}: {str(exc)[:160]}"
                    partial = getattr(exc, "partial", None)
                    if partial:
                        records[k] = dict(
                            rec,
                            state=DISABLED,
                            login=had_login or bool(partial.get("login")),
                            keys=list(dict.fromkeys(had_keys + list(partial.get("keys") or []))),
                            incomplete=error,
                        )
                    elif not was:
                        # 一步都没做成：记成嫌疑（下一轮会再试），面板上也能直接处理
                        records[k] = dict(
                            _suspect(person, (platform, account, user), signal), incomplete=error
                        )
                    report["failed"].append(dict(rec, error=error))
                    continue
                if got.get("gone"):
                    # 云上已经没有这个号了（有人在控制台删过）：目标达成，不用再管
                    records[k] = dict(
                        rec, state=DELETED, decided_at=_now(), decided_by="云上已不存在"
                    )
                    report.setdefault("gone", []).append(records[k])
                    continue
                rec.update(
                    state=DISABLED,
                    login=had_login or bool(got.get("login")),
                    keys=list(dict.fromkeys(had_keys + list(got.get("keys") or []))),
                )
                records[k] = rec
                report["done"].append(rec)
    if log is not None and report["done"]:
        log("offboard_disable", report["done"], "auto:iam-remind")
    if log is not None and report.get("gone"):
        log("offboard_gone", report["gone"], "auto:iam-remind")
    return report


def _suspect(person, target, signal) -> dict:
    platform, account, user = target
    return {
        "platform": platform,
        "account": account,
        "user": user,
        "person": person.name,
        "email": person.email,
        "union_id": person.union_id,
        "signal": signal,
        "at": _now(),
        "state": SUSPECT,
        "login": False,
        "keys": [],
    }


def note_suspects(path, people, signal: str = "飞书通讯录里找不到（没有自动停用）") -> list:
    """弱信号的人：不动账号，只记 `suspect` 等人拿主意。返回这次新记的。

    `people` 里每项是 person，或 `(person, 说明)`。
    """
    added = []
    with _locked(path) as box:
        records = box["records"]
        for item in people:
            person, why = item if isinstance(item, tuple) else (item, signal)
            for t in targets_of(person):
                k = key_of(*t)
                if k in records:
                    continue
                rec = _suspect(person, t, why)
                records[k] = rec
                added.append(rec)
    return added


def pending(path) -> list:
    return [r for r in load(path).values() if r.get("state") in PENDING]


def decide(path, key: str, action: str, executor: Callable, *, actor: str, log=None) -> dict:
    """管理员拿主意：`delete`（删号）/ `restore`（恢复或排除嫌疑）。返回更新后的记录。

    **删号只认记录里有的号**，不接受请求里随便给一个用户名 —— 否则一个构造出来的请求
    就能删掉任何人的号。
    """
    if action not in ("delete", "restore"):
        raise OffboardError("action 只能是 delete 或 restore")
    failed = ""
    with _locked(path) as box:
        records = box["records"]
        rec = records.get(key)
        if rec is None:
            raise OffboardError("没有这条离职记录")
        if rec.get("state") not in PENDING:
            raise OffboardError(f"这条已经处理过了（{rec.get('state')}）")
        if PROTECTED.match(str(rec.get("user") or "")):
            raise OffboardError("这个号受保护，面板不停也不删")
        if action == "delete" and rec.get("unverified"):
            # 名册里这个号不归这个人（IT 那边的属性值可能导错了）。一键删掉的可能是别人在用的号
            raise OffboardError(
                "名册里这个号不归他，面板不删。核实归属后到云控制台处理，这里点「没离职」拿掉"
            )
        ex = executor(rec["platform"], rec["account"])
        if action == "delete":
            left = ex.delete_user(rec["user"])
            if left:
                # 记下来再报错：异常穿出 with 的话这次尝试就不落盘了
                rec.update(left=left, tried_at=_now())
                records[key] = rec
                failed = "没删干净：" + "；".join(left)
            else:
                rec.pop("left", None)
                rec.update(state=DELETED, decided_at=_now(), decided_by=actor)
        else:
            if rec.get("state") == DISABLED:
                ex.enable_user(rec["user"], login=bool(rec.get("login")), keys=rec.get("keys"))
                rec.update(state=RESTORED)
            else:
                rec.update(state=DISMISSED)
            rec.update(decided_at=_now(), decided_by=actor)
        records[key] = rec
    if failed:
        raise OffboardError(failed)
    if log is not None:
        log(f"offboard_{action}", [rec], actor)
    return rec


def ensure_record(
    path, *, platform, account, user, person="", email="", union_id="", signal, verified=True
):
    """管理员直接确认离职时，这个号可能还没有记录（比如没被自动停过）。先补一条再删。"""
    with _locked(path) as box:
        records = box["records"]
        k = key_of(platform, account, user)
        # 已经删掉的不翻回待办：删号的审计信息（谁、何时）在这条记录上
        if k not in records or records[k].get("state") in (RESTORED, DISMISSED):
            records[k] = {
                "platform": platform,
                "account": account,
                "user": user,
                "person": person,
                "email": email,
                "union_id": union_id,
                "signal": signal,
                "at": _now(),
                "state": SUSPECT,
                "login": False,
                "keys": [],
            }
            if not verified:
                records[k]["unverified"] = True
        return k
