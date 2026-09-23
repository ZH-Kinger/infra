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
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import platforms
from .errors import DeliveryError

FILENAME = "offboard.json"

#: 面板能自己停、自己删的平台
PLATFORMS = ("aliyun", "volcano")

#: 没有接口、只能人去控制台处理的平台。**照样要列出来**：九章的号以前完全不进离职流程，
#: 一个离职的人在九章上的号没有任何地方会提醒去停（王昱然就是这么漏掉的）
MANUAL_PLATFORMS = ("jiuzhang",)

#: 自建服务（MLflow 这类）。**不是云平台，但一样要进离职流程** —— 它的用户恰恰是
#: 那批没有云子账号的人，只按云子账号记的话，这些人离职后一条记录都不会有，
#: 而「没有记录」和「没有东西要收」长得一模一样（审计 High-3）
SERVICE_PLATFORM = "internal"
ALL_PLATFORMS = PLATFORMS + MANUAL_PLATFORMS + (SERVICE_PLATFORM,)

#: 一轮最多自动停几个人。**超了就一个都不停，只提醒**：IT 的接口或飞书抖一下，
#: 可能一次性把一批在职的人标成离职，那时候停得越多事故越大
MAX_AUTO_PEOPLE = 3

#: 自建服务那边一轮最多停几个人。**独立的、宽得多的上限，但不能没有。**
#: 「不占云的额度」不等于「不需要熔断」—— 上限防的是「IT 接口或飞书抖一下，
#: 一次性把一批在职的人标成离职」，而**那个故障源对自建服务是同一个**
#: （`strong_candidates` 吃的是同一份 statuses / drift_rows）。
#: 没有它的话：云账号被上限拦住了，而几百人的 MLflow 当场全断 ——
#: 正在跑的训练写不进实验记录，恢复还要管理员一条条点。
#: 宽得多是因为这边的动作可逆、不打断任何东西，不该像云那样一碰就全停。
MAX_AUTO_SERVICE_PEOPLE = 20

#: 永远不自动停、不删的号。云上的 Deny 策略是第二道
#: 和云上执行身份策略里的 Deny 名单对齐（阿里、火山两份）。**改一边要改另一边**
_PROTECTED_CLOUD = re.compile(
    r"\A(panel-|power-|tempak|staff-|temp-ak-|wuji-|rl-|finance\Z|data-tran\Z)", re.IGNORECASE
)
#: 九章的登录名**全都是 `wuji-` 开头**（`wuji-wangyuran`），套云上那套前缀等于把整个平台挡光。
#: 九章那边没有服务号的概念，面板也动不了它的号，所以只挡面板自己可能登记的名字
_PROTECTED_MANUAL = re.compile(r"\A(panel-|power-)", re.IGNORECASE)
#: 自建服务这边不能自动停的身份。**按 union_id 精确匹配，不是前缀正则。**
#:
#: 这里不能借用云那套（`_PROTECTED_CLOUD`）有两个理由：
#:   · union_id 是 `on_` 开头的随机串，**前缀规则对它没有任何意义** —— 要么挡不住，
#:     要么误挡一片，两种都不是想要的；
#:   · 借用会真的误挡：曾经用邮箱当标识时实测 `protected("wuji-wang@wuji-tech.com")`
#:     返回 True，因为云那套里有 `wuji-` 而**公司域名就是 wuji-tech.com**。
#:     而这种误挡的后果是「这个人的服务访问永远不会被回收」，且没有任何地方报错。
#:
#: **默认空集 = 没有豁免，所有人照常回收。** 方向是有意的：多停是可恢复的
#: （有 `restored` 状态、有待确认卡片），漏停是永久残留且静默的。
#:
#: 要豁免就配 `DELIVERY_PROTECTED_SERVICE_IDS`（逗号或空白分隔的 union_id）。
#: **在 panel.env 里给每个 id 写一行注释说明是谁、为什么不能停** —— 写不出这两样
#: 的条目不该存在，半年后没人敢删的豁免名单比没有名单更糟。
ENV_PROTECTED_SERVICE = "DELIVERY_PROTECTED_SERVICE_IDS"
_PROTECTED_SERVICE: frozenset = frozenset()


@lru_cache(maxsize=1)
def _protected_service_ids(raw: str) -> frozenset:
    """`DELIVERY_PROTECTED_SERVICE_IDS` 解析成集合。逗号或空白分隔。

    **解析不出来的条目直接丢掉，不报错也不整条作废** —— 这里的失效方向是
    「那个人照常被回收」，是安全的那一侧；而把整份名单作废同样安全。
    反过来（解析失败就谁都不停）才是要避免的。
    """
    return frozenset(x for x in re.split(r"[,\s]+", str(raw or "")) if x)


def protected(user: str, platform: str = "") -> bool:
    """这个号是不是不能碰。**按平台分**：见 `_PROTECTED_MANUAL` / `_PROTECTED_SERVICE`。"""
    if platform == SERVICE_PLATFORM:
        # 自建服务按 union_id 精确匹配，**不落进下面那套前缀正则**（理由见常量注释）
        ids = _PROTECTED_SERVICE | _protected_service_ids(os.environ.get(ENV_PROTECTED_SERVICE, ""))
        return str(user or "") in ids
    rule = _PROTECTED_MANUAL if platform in MANUAL_PLATFORMS else _PROTECTED_CLOUD
    return bool(rule.match(str(user or "")))


class _Protected:
    """兼容旧写法 `PROTECTED.match(name)`（默认按云上那套判）。"""

    def match(self, user):
        return _PROTECTED_CLOUD.match(str(user or ""))


PROTECTED = _Protected()

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


def targets_of(person, platforms: Iterable[str] = ALL_PLATFORMS) -> list:
    """这个人名下要处理的号：`[(platform, account, user)]`。只取已确认归属的。

    默认**包含九章**：面板停不了它，但记下来才有人去控制台停。
    """
    out = []
    for ref in getattr(person, "accounts", ()) or ():
        if ref.platform not in platforms:
            continue
        if getattr(ref, "status", "confirmed") != "confirmed":
            continue
        if protected(ref.name, ref.platform):
            continue
        out.append((ref.platform, ref.account, ref.name))
    return out


def manual(platform: str) -> bool:
    """这个平台只能人去控制台处理。"""
    return platform in MANUAL_PLATFORMS


def service_targets(person, holdings) -> list:
    """这个人手上的自建服务 → `[(internal, 服务名, 标识)]`，形状和云子账号那套一致。

    `holdings` 是 `service_access.holdings()` 的结果（`{union_id: [(服务, 单号, 到期)]}`）。
    **判据来自那一份，不在这里另写** —— 「面板说他有、回收说他没有」这种背离，
    最先出问题的就是离职这一侧，而它不会报错。

    标识用 **union_id**，和判定侧（`service_access`）那条「按 union_id 严格相等」
    对齐，**不用邮箱**：
      · 邮箱会变（改名、换域名），而 `key_of` 拿它拼键 —— 人改了邮箱之后旧记录的键
        就对不上新目标，结果是重复写一条、旧那条永远清不掉；
      · `protected()` 对 internal 走的是**云那套前缀正则**（`wuji-`/`staff-`/`rl-`…），
        而那些前缀撞得上真实邮箱 —— 一个 `wuji-` 开头的邮箱会被静默判成「受保护、
        不自动停」，后果是那个人的服务访问**永远不会被回收**，且没有任何地方报错。
        union_id 是 `on_` 开头的随机串，撞不上。

    人能认的那部分（姓名、邮箱）在记录的 `person` / `email` 字段里，卡片照样显示得出来。
    """
    uid = str(getattr(person, "union_id", "") or "")
    if not uid:
        return []
    # **豁免名单要在这里过一道**，和云那一路在 `targets_of` 里做的是同一件事。
    # 漏了这道的表现特别坏：运维在 panel.env 里配了 `DELIVERY_PROTECTED_SERVICE_IDS`
    # （文档写的就是「不自动停这个人」），那个人**照样被停**，而配置看起来是生效的
    # —— 因为 `decide()` 那边确实认这个集合，于是停完之后管理员反而动不了这条记录
    if protected(uid, SERVICE_PLATFORM):
        return []
    return [(SERVICE_PLATFORM, service, uid) for service, _tid, _exp in holdings.get(uid, ())]


class ServiceAccess:
    """自建服务的停用 / 删除 / 恢复执行体。接口形状和云那套一致。

    **停用 = 把这条记进离职记录，不需要调任何接口。** 判定侧
    （`server.Backend.service_access`）每个请求都读那份记录里的 `union_id`，
    记上了访问立刻就没了 —— 所以 `disable_user` 什么都不做、直接返回成功，
    记录由 `auto_disable` 自己写。不是"假装成功"：**记录就是生效的那个东西**。

    **`delete_user` 才真的撤那张授权单**，对应「确认才删」那一步：管理员在卡片上
    点了确认，才把单子推成 REVOKED。撤单之后即使有人把离职记录删掉，访问也回不来
    —— 这正是"删"和"停用"的区别。实验数据一个字不动。

    `revoke(union_id, service) -> list[str]` 由调用方注入（它要拿到申请单存储）。
    返回没撤掉的东西，空列表 = 干净。**没注入时 `delete_user` 直接报错，不静默成功**：
    静默成功会让管理员看到「已删除」，而那张单还开着、人照样进得去。
    """

    def __init__(self, revoke: Optional[Callable[[str, str], list]] = None):
        self._revoke = revoke

    def disable_user(self, user: str) -> dict:  # noqa: ARG002 — 接口形状要和云那套一致
        return {}

    def delete_user(self, user: str) -> list:
        """撤销这个人在这个服务上的授权。返回没撤掉的，空 = 干净。"""
        if self._revoke is None:
            # 调用方没接上撤销能力。**报错而不是返回空** —— 返回空等于告诉
            # `decide()` "删干净了"，记录会被置成 DELETED，而那张单还开着
            return ["面板没接上撤销能力，这条要人工处理"]
        return list(self._revoke(user, "") or [])

    def enable_user(self, user: str, **_kw) -> dict:  # noqa: ARG002
        """恢复。同样不需要调接口 —— 记录被置回 RESTORED，`gone` 里就没有他了。

        **前提是还没走到「删」那一步**：`decide()` 只在 `state == DISABLED` 时调它，
        已经 DELETED 的单子撤都撤了，恢复要重新申请。
        """
        return {}


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
    holdings: Optional[dict] = None,
    max_people: int = MAX_AUTO_PEOPLE,
    max_service_people: int = MAX_AUTO_SERVICE_PEOPLE,
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
            # 云子账号 + 自建服务一起算。**只算云账号的话，没有云子账号的人
            # `fresh` 为空 → 进 report["skipped"] → 一条记录都不写**，而他手上
            # 可能正拿着 MLflow 的访问权限（审计 High-3）
            own = targets_of(person) + service_targets(person, holdings or {})
            todo_all = [t for t in own if open_(t)]
            # 九章这类没接口的：只记一条待办，不算进「这一轮停几个人」的上限。
            # **新记的要放进 report**：卡片只发 report 里的东西，不放等于这个人在飞书上
            # 一个字都不会出现 —— 而那正是这次要解决的漏网（审计 Med-1）
            for t in [x for x in todo_all if manual(x[0])]:
                k = key_of(*t)
                if k in records:
                    continue
                where = platforms.name_of(t[0])
                records[k] = _suspect(person, t, f"{signal}（{where}没有接口，要去控制台停用）")
                report.setdefault("manual", []).append(records[k])
            fresh = [t for t in todo_all if not manual(t[0])]
            if fresh:
                todo.append((person, signal, fresh))
            else:
                report["skipped"].append(person.name)

        # **上限只数「名下有云账号」的人。** 这个上限的理由是「IT 接口或飞书抖一下
        # 可能批量误判，那时停得越多事故越大」—— 而"越大"指的是云上那些动作：
        # 关登录、摘 AK，会当场打断在跑的任务，恢复也要一步步开回去。
        # 停一个自建服务只是写一条记录，随时能恢复、也不会打断任何东西，
        # 它不该占这份风险额度。
        #
        # 不这么分的话，**加了 MLflow 反而让云账号的回收更容易卡住**：3 个有云账号的人
        # 加 1 个只有 MLflow 的人就是 4 > 3 → 一个都不停，连那 3 个云账号也不停。
        def _risky(fresh) -> list:
            return [t for t in fresh if t[0] != SERVICE_PLATFORM]

        risky = [x for x in todo if _risky(x[2])]
        if len(risky) > max_people:
            # **云上那些号一个都不停，全部交给人**。见 MAX_AUTO_PEOPLE。
            # 记成嫌疑，面板上才有东西可点。
            #
            # **自建服务不在这里被连坐**：它既然不占额度，就不该受这个上限影响 ——
            # 否则同一个人的结果取决于"那一轮还有谁离职"，而他自己什么都没变。
            # 它照常走下面的正常流程（只是写一条记录，随时能恢复）
            report["held"] = [p.name for p, _s, _t in risky]
            for person, signal, fresh in risky:
                for t in _risky(fresh):
                    records.setdefault(
                        key_of(*t),
                        _suspect(person, t, f"{signal}（这一轮判离职的人太多，没有自动停用）"),
                    )
            todo = [
                (p, sig, [t for t in fresh if t[0] == SERVICE_PLATFORM]) for p, sig, fresh in todo
            ]
            todo = [x for x in todo if x[2]]

        # 自建服务自己那道熔断（见 MAX_AUTO_SERVICE_PEOPLE）。宽得多，但不能没有
        svc = [x for x in todo if any(pl == SERVICE_PLATFORM for pl, _a, _u in x[2])]
        if len(svc) > max_service_people:
            report["held"] = report["held"] + [p.name for p, _s, _t in svc]
            for person, signal, fresh in svc:
                for t in [x for x in fresh if x[0] == SERVICE_PLATFORM]:
                    records.setdefault(
                        key_of(*t),
                        _suspect(person, t, f"{signal}（这一轮判离职的人太多，没有自动停用）"),
                    )
            todo = [
                (p, sig, [t for t in fresh if t[0] != SERVICE_PLATFORM]) for p, sig, fresh in todo
            ]
            todo = [x for x in todo if x[2]]

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
        if protected(str(rec.get("user") or ""), str(rec.get("platform") or "")):
            raise OffboardError("这个号受保护，面板不停也不删")
        if action == "delete" and rec.get("unverified"):
            # 名册里这个号不归这个人（IT 那边的属性值可能导错了）。一键删掉的可能是别人在用的号。
            # **排在 manual 分支之前**：否则九章那种「我已在控制台处理」会绕过这条警告，
            # 而那正是管理员去控制台动手之前最该看到的一句（审计 Low-1）
            raise OffboardError(
                "名册里这个号不归他，面板不删。核实归属后到云控制台处理，这里点「没离职」拿掉"
            )
        if manual(str(rec.get("platform") or "")):
            # 面板调不了这个平台（九章）。管理员点的是「我在控制台处理完了」，这里只记账。
            # **不碰云**：这个平台没有接口，记成已处理是为了它别一直挂在待办里
            rec.update(
                state=DELETED if action == "delete" else DISMISSED,
                decided_at=_now(),
                decided_by=actor,
                by_hand=platforms.name_of(rec.get("platform", "")),
            )
            records[key] = rec
            if log is not None:
                log(f"offboard_{action}_by_hand", [rec], actor)
            return rec
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
