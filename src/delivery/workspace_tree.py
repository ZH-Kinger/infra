"""**做训练的人**在对象存储上的那块地方：`桶/组/人`。

不是全员。IT、财务这类不跑训练的人不需要数据集，也就不需要这块地方 ——
所以「不在任何用户组里」是**跳过**而不是报错：现网唯一那个人正好是 IT 部的。

算出「现在应该是什么样」，和「云上实际是什么样」比一比，得出要建哪些、哪些该搬。
**这个模块只算，不调任何云接口**，所以它能被完整地测。

为什么组在路径里（以及这意味着什么）
────────────────────────────────
组写进路径，等于把一个**会变的属性**编进了一个**不能变的标识**。人换组的时候，
路径就只有三条出路：搬数据、留着不动（路径在说谎，而且按组前缀授权时旧组还能读到
他的新数据）、或者在新组下再建一个（数据劈成两半）。

选了这条路，就得把「搬」做成一个**正经操作**而不是让人手敲 ossutil：
OSS 没有原子 rename，「搬目录」是逐对象 copy + delete，中途断了就是两边各一半。
所以 `moves()` 只负责**算出该搬什么**，真搬要先复制、校验、改数据集指向，最后才删源。

一句实话记在这里：**桶空着的时候搬是免费的，数据长起来之后不是。**
某人目录里有 2TB 的时候换组，那是几小时的事，期间在跑的训练读不到。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from . import grants
from .errors import DeliveryError

#: 组名和登录名都只允许这些字符。**不是洁癖**：这两段会直接拼进 OSS 的 key 和 RAM
#: 策略的 `oss:Prefix` 条件里，一个 `*` 或者 `../` 就能让一条策略覆盖到别人的目录
_SEGMENT = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
#: 一个人没有任何组的时候放哪。**不编造组名** —— 猜一个「看起来对」的组，
#: 比明摆着说「这个人还没归位」更糟：前者没人会去修，后者一眼就看得见
NO_GROUP = ""


class TreeError(DeliveryError):
    """目录规划算不出来。"""


#: 两种布局。**不是口味问题，是存量决定的**：
#: CPFS 上那三十个目录全是扁平的（`/wzh/`、`/zhangwt/`…），而按「存量不动」的原则
#: 它们不能搬 —— 给 CPFS 加一层组，结果只会是两种形状并存，比统一成任何一种都糟。
#: OSS 那个桶是新建的、空的，所以可以分层。
LAYOUT_FLAT = "flat"  # <登录名>/
LAYOUT_GROUPED = "grouped"  # <组>/<登录名>/
LAYOUTS = (LAYOUT_FLAT, LAYOUT_GROUPED)


@dataclass(frozen=True)
class Slot:
    """一个人应该拥有的那块地方。"""

    login: str
    group: str
    person: str = ""
    layout: str = LAYOUT_GROUPED

    @property
    def prefix(self) -> str:
        if self.layout == LAYOUT_FLAT or not self.group:
            return f"{self.login}/"
        return f"{self.group}/{self.login}/"


@dataclass(frozen=True)
class Move:
    """人换组了，他的目录该从哪搬到哪。"""

    login: str
    old_prefix: str
    new_prefix: str
    person: str = ""


def _segment(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not _SEGMENT.match(text):
        raise TreeError(f"{what} {text!r} 不能作为目录名（只允许字母数字和 . _ -，且不以符号开头）")
    return text


def load_slugs(path: Optional[str]) -> dict:
    """`identity/departments.json`：飞书部门名 → 目录用的英文短名。

    **填过就别改。** 这些短名会变成 OSS 上的真实目录名和 PAI 数据集的 Uri，
    改一个就是搬一次数据。
    """
    import json
    from pathlib import Path

    if not path or not Path(path).exists():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TreeError(f"读不了部门对照表 {path}：{exc}") from exc
    table = data.get("departments") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        raise TreeError(f'{path} 格式应为 {{"departments": {{"od-…": {{"slug": "…"}}}}}}')
    out = {}
    for key, value in table.items():
        # 值可以是裸字符串（老写法）或 {"slug": …, "name": …}。name 只是给人看的，
        # 每次采集会刷新；**键是部门 id**，因为改名不改 id
        slug = value.get("slug") if isinstance(value, dict) else value
        out[str(key)] = _segment(slug, f"{key} 的短名")
    return out


def plan(
    users: Iterable,
    *,
    people: Optional[dict] = None,
    services: Optional[Iterable] = None,
    layout: str = LAYOUT_GROUPED,
    department_of: Optional[dict] = None,
    slugs: Optional[dict] = None,
    unbound: Optional[Iterable] = None,
) -> tuple:
    """算出该给谁建目录。返回 `(要建的 Slot 列表, 跳过的原因)`。

    `users` 是权限快照里的子账号（要有 `.name` 和 `.groups`）。`people` 是
    `{登录名: 姓名}`，**认不出的人一律跳过**：目录名会长期留在桶里，给一个认不出
    属主的号建目录，等于制造下一条「无主资产」。

    服务号也跳过 —— 它们是程序在用的，没有「个人目录」这个概念。
    """
    if layout not in LAYOUTS:
        raise TreeError(f"布局只能是 {' / '.join(LAYOUTS)}，收到 {layout!r}")
    known = dict(people or {})
    svc = {str(s or "").lower() for s in (services or ())} - {""}
    slots, skipped = [], []
    for user in users:
        login = str(getattr(user, "name", "") or "")
        low = login.lower()
        if not login:
            continue
        if low in svc or low.startswith(grants.ISSUED_PREFIXES):
            skipped.append(f"{login}：服务号或程序发的临时凭证，不建个人目录")
            continue
        if login not in known:
            skipped.append(f"{login}：名册里认不出这个号是谁的，先补登记再建目录")
            continue
        # 部门优先于云上的用户组：云上的组是**权限组**（`wuji_Algorithm` 一个装了 50 人，
        # 那一层是常量、不带信息），而部门是真的组织结构（算法组下面 7 个子组）。
        # 部门名是中文，路径里不能用，所以必须过对照表；**没登记的不建**，
        # 不自动拼音 —— 自动生成的短名等有人补了对照表就会变，那是一次搬目录
        if department_of is not None:
            found = department_of.get(login)
            if not found:
                # **三种情况分开说**，因为处置完全不同：新人等他登录一次就好了，
                # 而「有 union_id 却查不到部门」才值得去看是不是离职了。
                # 混成一句「查不到部门」的话，每次都得人去翻一遍才知道该不该管
                if login in (unbound or ()):
                    skipped.append(f"{login}：名册里没有公司邮箱，对不上飞书，先补名册")
                else:
                    skipped.append(
                        f"{login}：公司邮箱在飞书的部门成员里找不到 —— "
                        "**先查是不是离职了**（delivery hygiene --check-status）"
                    )
                continue
            did, dept = found
            slug = (slugs or {}).get(did)
            if not slug:
                skipped.append(
                    f"{login}：部门「{dept}」（{did}）还没登记英文短名，"
                    "在 identity/departments.json 里补一行再建"
                )
                continue
            groups = [slug]
        else:
            groups = [g for g in (getattr(user, "groups", ()) or ())]
        if layout == LAYOUT_FLAT:
            # 扁平布局下组不进路径，所以没组、多个组都不影响 —— 路径照样算得出来
            slots.append(
                Slot(
                    login=_segment(login, "登录名"),
                    group=groups[0] if len(groups) == 1 else NO_GROUP,
                    person=known[login],
                    layout=layout,
                )
            )
            continue
        if len(groups) > 1:
            # 一个人两个组 = 两条路径都说得通，而路径只能有一条。
            # 自动挑一个必然有一半人是错的，这种要人来定
            skipped.append(
                f"{login}：同时属于 {len(groups)} 个组（{'、'.join(groups)}），要人指定放哪"
            )
            continue
        group = _segment(groups[0], "组名") if groups else NO_GROUP
        if not group:
            skipped.append(
                f"{login}：不在任何用户组里，不建。"
                "**不一定是漏了** —— 不做训练的人（IT、财务这些）本来就不需要这块地方"
            )
            continue
        slots.append(
            Slot(login=_segment(login, "登录名"), group=group, person=known[login], layout=layout)
        )
    slots.sort(key=lambda s: (s.group, s.login))
    return slots, skipped


def missing(slots: Iterable, existing: Iterable) -> list:
    """还没建的那些。`existing` 是云上已有的前缀（`组/人/`）。"""
    have = {str(p or "").strip("/") + "/" for p in existing if str(p or "").strip("/")}
    return [s for s in slots if s.prefix not in have]


def moves(slots: Iterable, existing: Iterable) -> list:
    """人换组了 —— 云上那份在旧组下面，名册说他现在在别的组。

    **靠登录名认人，不靠路径**：登录名是最后一段，组是第一段，所以同一个人换组之后，
    云上那条和应该那条的最后一段相同、第一段不同。

    只报，不搬。真搬是 copy → 校验 → 改数据集指向 → 最后才删源，
    而且得有人点头：OSS 没有原子 rename，中途断了就是两边各一半。
    """
    want = {s.login: s for s in slots}
    out = []
    for raw in existing:
        parts = [p for p in str(raw or "").strip("/").split("/") if p]
        if len(parts) != 2:
            # 扁平布局（一段）本来就不会因为换组而失效 —— 这正是它的好处
            continue
        group, login = parts
        slot = want.get(login)
        if slot is not None and slot.group != group:
            out.append(
                Move(
                    login=login,
                    old_prefix=f"{group}/{login}/",
                    new_prefix=slot.prefix,
                    person=slot.person,
                )
            )
    out.sort(key=lambda m: m.login)
    return out


def strays(slots: Iterable, existing: Iterable) -> list:
    """云上有、但现在谁都不该有的目录（人删号了、或者名册认不出了）。

    **只报不删。** 这跟 PAI 数据集那批「人的号删了东西还留着」是同一类东西，
    处置也一样：先找他原来的组确认还要不要、要不要交接。
    """
    want = {s.login for s in slots}
    out = []
    for raw in existing:
        parts = [p for p in str(raw or "").strip("/").split("/") if p]
        if len(parts) == 2 and parts[1] not in want:
            out.append(f"{parts[0]}/{parts[1]}/")
    return sorted(set(out))
