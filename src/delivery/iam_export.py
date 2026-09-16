"""IAM 属性表的增量比对与 CSV 格式规则。

属性值直接决定员工 SSO 登进哪个云账号：发错人 = 他能登进别人的号。所以增量比对里
**任何拿不准的情况都不猜**：要么当成两个人处理，要么整份拒绝，交给人核对。

比对规则
────────
人按 union_id 对。基线里还没有 union_id 的行（首次按邮箱回填的）**不再按邮箱认领**：
当前同邮箱的人有了 union_id 时，输出「按值 remove 旧值」+「按 union_id set」。
真回填时值删了又写回同一个人，结果不变；邮箱复用给了新人时，旧值从原主身上删掉，
不会因为姓名碰巧一样（或者都是邮箱前缀）就把原主的访问留下来。
其余情况一律当成不同的人：
  · 基线有 union_id、当前同邮箱的行没有 → 邮箱可能复用给了新人。基线那条 remove，
    当前这条改成 skip 并写明原因，不按邮箱 set（否则会写到原主的 IAM 用户上）
  · remove 行原样沿用基线里的 union_id / 邮箱，**不按邮箱去补别人的 union_id**。
    remove 行 match_by=value：IT 删除「当前 cloud_accounts[app] 恰好等于 value」的那个用户的
    这个键，不按 union_id / 邮箱找人。这个值只在真正持有它的人身上，天然定位到对的人

整份拒绝的情况
──────────────
  · 基线里出现过的应用标识，当前配置里没有了（配丢了会给全员发 remove）
  · 基线里带 union_id 的人在当前名册里整体消失（名册没带通讯录重新生成的典型症状）
  · 任一应用的 remove 条数超过该应用基线的阈值，除非显式允许（某个平台整体没采到时只影响
    这个应用，全局阈值会被其他应用的条数稀释）
  · 基线不是 identity/iam-sent/ 下经确认的全量存档，或含 remove 行（那是一份增量）
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .errors import DeliveryError

#: remove 超过 max(这个数, 基线 set 条数的 10%) 就要显式允许
MASS_REMOVE_MIN = 5
MASS_REMOVE_RATIO = 0.10

#: 以这些字符开头的格子，Excel 会当公式执行
_FORMULA = ("=", "+", "-", "@", "\t", "\r")
#: 这几列决定导入结果，原样写出、不做转义；值本身有问题就整行 skip
RAW_COLUMNS = ("feishu_union_id", "email", "app", "value", "action", "match_by")
#: 确认过的全量存档名：时间戳（同一秒内再存加 ~n）。别的名字一律不当基线
ARCHIVE_NAME = re.compile(r"^\d{8}-\d{6}(~\d+)?\.csv$")
MATCH_BY_VALUE = "value"


_REUSED = "基线里这个邮箱属于另一个 union_id，邮箱可能已复用，需核对后补 union_id"
_NAME_CHANGED = "基线里同邮箱的行姓名不同，邮箱可能已复用，需核对"


class IamDiffError(DeliveryError):
    """增量比对拒绝输出。"""


def unsafe_raw(row: dict) -> str:
    """原样列里有会被表格软件当公式的值：返回原因，否则空串。"""
    for col in ("feishu_union_id", "email", "app", "value"):
        if str(row.get(col) or "")[:1] in _FORMULA:
            return f"{col} 以特殊字符开头，表格软件会当公式执行，需人工处理"
    return ""


def cell(column: str, value: str) -> str:
    if column in RAW_COLUMNS:
        return value
    return "'" + value if value[:1] in _FORMULA else value


def uncell(column: str, value: str) -> str:
    if column in RAW_COLUMNS:
        return value
    return value[1:] if value[:1] == "'" and value[1:2] in _FORMULA else value


def sanitize(rows: list) -> list:
    """原样列不安全：set 行改成 skip；其他行把不安全的值清空（它们只供核对）。"""
    out = []
    for r in rows:
        reason = unsafe_raw(r)
        if not reason:
            out.append(r)
            continue
        cleared = {
            c: ("" if str(r.get(c) or "")[:1] in _FORMULA else r[c])
            for c in ("feishu_union_id", "email", "app")
        }
        out.append(dict(r, **cleared, action="skip", value="", problem=reason))
    return out


def archive_order(path: Path) -> tuple:
    stem, _, n = path.stem.partition("~")
    return (stem, int(n) if n.isdigit() else 0)


def confirmed_archives(sent_dir: Path) -> list:
    return sorted(
        (p for p in sent_dir.glob("*.csv") if ARCHIVE_NAME.match(p.name) and not p.is_symlink()),
        key=archive_order,
    )


def check_baseline(path: Path, rows: list, sent_dir: Path) -> None:
    real = path.resolve()
    root = sent_dir.resolve()
    if real.parent != root or not ARCHIVE_NAME.match(path.name) or path.is_symlink():
        raise IamDiffError(
            f"基线必须是 {sent_dir} 里经确认的全量存档（--baseline latest），"
            f"不能用 {path}：上一次的输出可能是增量，拿它比对会漏发 remove"
        )
    if any(r["action"] == "remove" for r in rows):
        raise IamDiffError(f"{path} 含 remove 行，是一份增量，不能当基线")


def diff(
    current: list,
    baseline: list,
    *,
    current_apps: set,
    allow_mass_remove: bool = False,
    resolved_emails: frozenset = frozenset(),
) -> tuple:
    """返回 (输出行, 提示)。输出行含 set / remove，以及因邮箱复用改成 skip 的行。

    `resolved_emails`：管理员已经核对过、确认没有问题的邮箱（--resolved）。上一次因邮箱复用或
    姓名不符被 skip 的行，存档里记为 skip；没核对之前每次比对都继续 skip 并提示。
    """
    resolved = {e.strip().lower() for e in resolved_emails}
    flagged = {
        (r["feishu_union_id"], r["email"].lower(), r["app"]): r["problem"]
        for r in baseline
        if r["action"] == "skip" and r["problem"] in (_REUSED, _NAME_CHANGED)
    }
    old_sets = [r for r in baseline if r["action"] == "set"]
    new_sets = [r for r in current if r["action"] == "set"]

    missing_apps = sorted({r["app"] for r in old_sets} - set(current_apps))
    if missing_apps:
        raise IamDiffError(
            f"基线里的应用标识 {', '.join(missing_apps)} 在当前配置里不存在。"
            "配置丢了会给所有人发 remove，请先核对 iam-attributes.json"
        )

    current_uids = {r["feishu_union_id"] for r in current if r["feishu_union_id"]}
    vanished = sorted(
        {r["feishu_union_id"] for r in old_sets if r["feishu_union_id"]} - current_uids
    )
    if vanished and not allow_mass_remove:
        raise IamDiffError(
            f"基线里有 {len(vanished)} 个 union_id 在当前名册里整体消失。"
            "常见原因是名册没带通讯录重新生成；确认这些人确实离职或删号后加 --allow-mass-remove"
        )

    old_by_uid = {(r["feishu_union_id"], r["app"]): r for r in old_sets if r["feishu_union_id"]}
    # 只有「基线没有 union_id」的行可以被按邮箱对上
    old_by_mail_no_uid = {
        (r["email"].lower(), r["app"]): r
        for r in old_sets
        if not r["feishu_union_id"] and r["email"]
    }
    # 邮箱在基线里属于某个 union_id（任何应用）：当前没 union_id 的同邮箱行不能按邮箱写
    old_by_mail_with_uid = {
        r["email"].lower(): r for r in baseline if r["feishu_union_id"] and r["email"]
    }

    out, notes = [], []
    matched_old = set()
    #: 补 union_id 产生的「删了又写回同一个值」：不算批量删除
    backfill_pairs: set = set()
    for r in new_sets:
        uid, mail, app = r["feishu_union_id"], r["email"].lower(), r["app"]
        problem = flagged.get((uid, mail, app))
        if problem and mail not in resolved:
            out.append(dict(r, action="skip", value="", problem=problem))
            notes.append(
                f"{r['name'] or mail}：上次标记为「{problem}」，核对后用 --resolved {mail}"
            )
            continue
        prev = None
        if uid:
            prev = old_by_uid.get((uid, app))
            if prev is None and mail and (mail, app) in old_by_mail_no_uid:
                backfill = old_by_mail_no_uid[(mail, app)]
                if not _same_person_name(backfill, r):
                    notes.append(
                        f"{r['name'] or mail}：邮箱与基线里的「{backfill['name']}」相同但姓名不同，"
                        "已按值删除旧值后重新写入，请核对"
                    )
                # 不认领基线行：旧值走 remove（先执行），再按 union_id 写入
                backfill_pairs.add((mail, app, backfill["value"], r["value"]))
                out.append(dict(r))
                continue
        else:
            if mail and mail in old_by_mail_with_uid:
                holder = old_by_mail_with_uid[mail]
                out.append(
                    dict(
                        r,
                        action="skip",
                        value="",
                        problem=_REUSED,
                    )
                )
                old_uid = holder["feishu_union_id"]
                notes.append(f"{r['name'] or mail}：邮箱可能已复用给新人（原 union_id {old_uid}）")
                continue
            prev = old_by_mail_no_uid.get((mail, app)) if mail else None
        if prev is not None:
            matched_old.add(id(prev))
        if prev is None or prev["value"] != r["value"]:
            out.append(dict(r))

    removes = [
        dict(prev, action="remove", match_by=MATCH_BY_VALUE, problem="")
        for prev in old_sets
        if id(prev) not in matched_old
    ]
    set_values = {(r["app"], r["value"]) for r in out if r["action"] == "set"}
    for rm in removes:
        if (rm["app"], rm["value"]) in set_values:
            # 同一个值这次也写给了别人：提醒 IT 删的是原持有者（remove 先于 set 执行）
            rm["problem"] = "同一文件中会重新写入该值（转给他人或补 union_id），请先删除再写入"

    if not allow_mass_remove:
        for app in sorted({r["app"] for r in old_sets}):
            base_n = sum(1 for r in old_sets if r["app"] == app)
            remove_n = sum(
                1
                for r in removes
                if r["app"] == app
                and (r["email"].lower(), app, r["value"], r["value"]) not in backfill_pairs
            )
            threshold = max(MASS_REMOVE_MIN, int(base_n * MASS_REMOVE_RATIO))
            # 应用整体消失（全部 remove）同样拦下：阈值下限 5 挡不住只有几个人的应用
            if remove_n > threshold or (base_n and remove_n == base_n):
                raise IamDiffError(
                    f"应用 {app} 这次要发 {remove_n} 条 remove，超过阈值 {threshold}。"
                    "一次删这么多通常是配置、名册或采集出了问题；确认无误后加 --allow-mass-remove"
                )
    # remove 必须排在 set 前面：值从 P 转给 Q 时先 set 会让两人同时持有，按值删除就找到两个人
    return removes + out, notes


#: IT 回传的导入结果列（第一列是 union_id：IAM 里写进去的是谁）
RESULT_COLUMNS = ("feishu_union_id", "email", "name", "app", "value", "result")
#: 导入成功的结果串：ok / OK / ok: created。别的一律不当成功
_RESULT_OK = re.compile(r"^ok\b", re.IGNORECASE)
#: 提示最多列多少条，多了只给条数（一份对不上的回传会把整份名册的邮箱刷屏）
_NOTE_LIMIT = 20


def adopt_result(result_rows: list, current: list, baseline: list = ()) -> tuple:
    """把 IT 回传的导入结果当作新基线。

    第一次导出是按邮箱回填的，基线里没有 union_id；IT 导入后回传的结果每行都带 union_id。
    直接拿旧基线做增量会输出一大批「按值删掉再按 union_id 写回」的同值行，让 IT 白导一遍，
    所以采纳回传结果作为基线：IAM 里此刻实际是什么，就记什么。

    **旧基线里回传没覆盖到的 set 行照抄进新基线**：那些值还在 IAM 里。丢掉它们，
    离职的人就再也不会生成 remove，属性永远留在 IAM（和 --record 的守卫是同一个道理）。

    返回 (基线行, 提示, 沿用的旧基线行数)。IT 那边没导进去的行只做提示，不新增进基线。
    """
    notes, baseline_rows, carried = [], [], 0
    refused: set = set()  # IT 明确说没导入的：IAM 里没有，旧基线里的同一条也不能再算数
    by_key = {(r["feishu_union_id"], r["app"]): r for r in current if r["action"] == "set"}
    by_mail = {(r["email"].lower(), r["app"]): r for r in current if r["action"] == "set"}
    seen: dict = {}
    for row in result_rows:
        uid = str(row.get("feishu_union_id") or "").strip()
        email = str(row.get("email") or "").strip()
        app = str(row.get("app") or "").strip()
        value = str(row.get("value") or "").strip()
        result = str(row.get("result") or "").strip()
        # 只认 ok 开头为导入成功；其余一律只提示、不进基线（下次增量会重新发）。
        # 不按「看不懂就拒绝整份」处理：IT 写 failed / not found 都很正常，逼人手改回传文件更糟
        if not _RESULT_OK.match(result):
            notes.append(f"IT 未导入：{email} / {app}（{result or '原因未写'}）")
            if uid and app:
                refused.add((uid, app))
            if email and app:
                refused.add((email.lower(), app))
            continue
        if not uid or not app or not value:
            notes.append(f"回传结果缺字段，跳过：{email or '(无邮箱)'} / {app or '(无应用)'}")
            continue
        old = seen.get((uid, app))
        if old is not None:
            if old["value"] != value:
                raise IamDiffError(
                    f"回传里同一个人同一应用有两个值：{email} / {app}："
                    f"{old['value']} 和 {value}，整份拒绝"
                )
            continue  # 完全重复的行：留一条就够（留两条会让下次增量把其中一条 remove 掉）
        mine = by_key.get((uid, app)) or by_mail.get((email.lower(), app))
        if mine is None:
            notes.append(
                f"回传里有、名册里没有：{email} / {app} = {value}（有人离职或对应关系变了？）"
            )
        elif mine["value"] != value:
            notes.append(
                f"IAM 里的值和名册不一致，按 IAM 记：{email} / {app}：{value} ≠ {mine['value']}"
            )
        entry = {
            "feishu_union_id": uid,
            "email": email,
            "name": str(row.get("name") or "").strip(),
            "app": app,
            "value": value,
            "action": "set",
            "match_by": "feishu_union_id",
            "problem": "",
        }
        seen[(uid, app)] = entry
        baseline_rows.append(entry)

    covered_uid = set(seen)
    covered_mail = {(r["email"].lower(), r["app"]) for r in baseline_rows if r["email"]}
    # 同一条 IAM 记录可能对不上 uid / 邮箱（IT 按 union_id 匹配、邮箱列留空），但值是同一个：
    # 再按 (应用, 值) 认一次，否则旧基线那条会被重复带一份，下次增量把在职的人 remove 掉
    covered_value = {(r["app"], r["value"]) for r in baseline_rows}
    # 旧基线里没被回传覆盖的 set 行：IAM 里还留着，照抄，下次增量按名册决定保留还是 remove
    for r in baseline:
        if r["action"] != "set":
            continue
        uid_key = (r["feishu_union_id"], r["app"]) if r["feishu_union_id"] else None
        mail_key = (r["email"].lower(), r["app"]) if r["email"] else None
        if (uid_key and uid_key in covered_uid) or (mail_key and mail_key in covered_mail):
            continue
        if (uid_key and uid_key in refused) or (mail_key and mail_key in refused):
            # IT 这次明确没导入：IAM 里没有这个值，基线不能记着它，否则下次增量不会补发
            notes.append(f"IT 明确没导入、旧基线里有：{r['email']} / {r['app']}（下次增量会重发）")
            continue
        if (r["app"], r["value"]) in covered_value:
            # 同一个值这次归了别人：号被回收转给了新同事。旧的那条不能带（带上就两个人同值，
            # 说不清该删谁），但也不能当没发生：旧属性可能还留在 IAM 里，得有人去看一眼
            owner = next(
                (
                    x["email"]
                    for x in baseline_rows
                    if (x["app"], x["value"]) == (r["app"], r["value"])
                ),
                "",
            )
            notes.append(
                f"{r['app']} 的 {r['value']} 这次记在 {owner or '别人'} 名下，"
                f"旧基线里是 {r['email']}："
                "换人或只是换了邮箱都可能，换人的话请确认 IAM 里旧那条属性已经清掉"
            )
            continue
        baseline_rows.append(dict(r))
        carried += 1
        covered_value.add((r["app"], r["value"]))
        if mail_key:
            covered_mail.add(mail_key)
        if uid_key:
            covered_uid.add(uid_key)
        notes.append(
            f"回传里没有、旧基线里有：{r['email']} / {r['app']}（沿用旧值，下次增量再定去留）"
        )

    missing = []
    for r in current:
        if r["action"] != "set":
            continue
        uid_key = (r["feishu_union_id"], r["app"]) if r["feishu_union_id"] else None
        mail_key = (r["email"].lower(), r["app"]) if r["email"] else None
        if (
            (uid_key and uid_key in covered_uid)
            or (mail_key and mail_key in covered_mail)
            or (r["app"], r["value"]) in covered_value
        ):
            continue
        missing.append(r)
    for r in missing[:_NOTE_LIMIT]:
        notes.append(f"名册里有、这次没进 IAM：{r['email']} / {r['app']}（下次增量会带上）")
    if len(missing) > _NOTE_LIMIT:
        notes.append(f"另有 {len(missing) - _NOTE_LIMIT} 条名册里有、这次没进 IAM 的记录")
    # 同一个应用里同一个值出现两次：remove 是按值删的，这种基线本身就说不清该删谁
    dupes = Counter((r["app"], r["value"]) for r in baseline_rows if r["action"] == "set")
    clash = [k for k, n in dupes.items() if n > 1]
    if clash:
        app, value = clash[0]
        raise IamDiffError(f"基线里 {app} 的值 {value} 属于两个人，说不清该删谁，整份拒绝")
    return sanitize(baseline_rows), notes, carried


def _normalize_name(name: str) -> str:
    return "".join(str(name or "").split()).lower()


def _same_person_name(baseline_row: dict, row: dict) -> bool:
    """回填 union_id 时核对姓名。基线姓名是推出来的（邮箱前缀）或为空时不做判断：
    名册接入通讯录后姓名会从邮箱前缀变成真实姓名，这不是换了人。"""
    old = _normalize_name(baseline_row.get("name", ""))
    new = _normalize_name(row.get("name", ""))
    prefix = str(baseline_row.get("email") or "").split("@")[0].lower()
    if not old or not new or old == prefix:
        return True
    return old == new


def recorded_state(full: list, out: list, baseline: list = ()) -> list:
    """存档写「实际发出去之后 IAM 里应有的状态」，不是全量导出本身。

    增量里被改成 skip 的行（邮箱复用、姓名不符）没有发给 IT，存档里也必须是 skip；
    否则确认之后基线声称 IAM 里有这个值，下一次比对就再也不会补发、也不会再提示。
    """
    skipped = {
        (r["feishu_union_id"], r["email"].lower(), r["app"]) for r in out if r["action"] == "skip"
    }
    state = []
    current_keys = {(r["email"].lower(), r["app"]) for r in full if r["action"] == "set"}
    for r in full:
        key = (r["feishu_union_id"], r["email"].lower(), r["app"])
        if r["action"] == "set" and key in skipped:
            hit = next(
                x
                for x in out
                if x["action"] == "skip"
                and (x["feishu_union_id"], x["email"].lower(), x["app"]) == key
            )
            state.append(dict(hit))
        else:
            state.append(r)
    # 上次标记过、这次这个人暂时不是 set（比如对应关系临时回到待确认）：标记不能丢
    have = {
        (r["email"].lower(), r["app"])
        for r in state
        if r["action"] == "skip" and r["problem"] == _REUSED
    }
    for r in baseline:
        key = (r["email"].lower(), r["app"])
        if (
            r["action"] == "skip"
            and r["problem"] == _REUSED
            and key not in have
            and key not in current_keys
        ):
            state.append(dict(r))
    return state
