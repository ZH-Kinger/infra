"""飞书身份 → 云账号 的映射表。

为什么是**数据**不是规则
────────────────────────
真实数据里至少三种命名约定并存，而且**两朵云的习惯正好相反**：

    阿里云   zhangsan      ←  zhang.san@wuji.tech    小写，姓在前
    火山     SanZhang      ←  同一个人                 驼峰，名在前

任何**单**条规则都会给一整朵云算错，而算错的后果是那个人登录报「账户不存在」。
所以规则生成的是**一组候选**（同一份姓名的两种排列），命中任意一个即算解析成功；
推不出或有歧义时交给显式登记，两者冲突时登记赢。

实测（2026-09-14，两朵云全量）：

    阿里云   有企业邮箱 48 人   候选命中 48
    火山     有企业邮箱 19 人   候选命中 17

    第二种排列多救回的人里就有 `siliu` ← `liu.si`——
    在只有「去点」一条规则时，他是要去找本人改账号名的例外。

匹配发生在云那侧，是精确比对，我们改不了它。能动的只有飞书往断言里放什么值——
所以这张表的实际用途是：告诉管理员「每个人的飞书工号该填成什么」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from ..errors import DeliveryError
from .audit import AccountUser

SOURCE_RULE = "rule"  # 由邮箱按规则推导
SOURCE_DISPLAY_NAME = "display_name"  # 邮箱推不出，靠飞书中文名 == 云上显示名
SOURCE_OVERRIDE = "override"  # 显式登记
SOURCE_NONE = "none"  # 推不出也没登记


class MappingError(DeliveryError):
    """映射表不合法。"""


def derive(email_local: str) -> str:
    """原序拼接：`zhang.san` → `zhangsan`。阿里云的习惯。"""
    return email_local.replace(".", "")


def _tokens(raw: str, sep: str) -> list:
    return [t for t in raw.replace("_", sep).split(sep) if t]


_HAN = re.compile(r"[\u4e00-\u9fff]")
#: 飞书显示名的实际格式：`张三（San Zhang）`，全角括号为主，偶见半角。
#: 括号内外都允许多余空格——人工录入的数据，不要假设它整齐。
_FEISHU_NAME = re.compile(r"\A\s*(?P<cn>[^()（）]*?)\s*[（(]\s*(?P<en>[^()（）]+?)\s*[）)]\s*\Z")


def parse_feishu_name(raw: str) -> tuple:
    """`张三（San Zhang）` → (`张三`, `San Zhang`)；`tom` → (``, `tom`)。

    飞书名把两份信息打包在一个字段里，而它们喂的是两层不同的匹配：
      中文名  → 对云上的 DisplayName（阿里云/火山都存的是纯中文名）
      英文名  → 生成候选用户名（`candidates(feishu_en=...)`）

    没有括号时按是否含汉字判断归属——纯中文就是中文名，纯英文（如 `tom`）
    就是英文名。**不猜**混合写法，比如 `张三 San Zhang` 不带括号：
    那种整串当中文名处理，匹配不上就进待办，比拆错了更安全。
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    m = _FEISHU_NAME.match(text)
    if m:
        return m.group("cn").strip(), m.group("en").strip()
    if _HAN.search(text):
        return text, ""
    return "", text


def _name_key(value: str) -> str:
    """显示名比对键：去空白；纯 ASCII 的转小写（`Tom` 和 `tom` 是同一个人），
    含汉字的原样保留（汉字没有大小写，转换只会引入意外）。"""
    v = (value or "").strip()
    if not v:
        return ""
    return v if _HAN.search(v) else v.lower()


def candidates(email_local: str = "", *, feishu_en: str = "") -> set:
    """一个人 → 一组可接受的云用户名（全小写）。

    两个来源产出的是**同一份信息的两种排列**，所以合成一个候选集：

        邮箱     zhang.san     → 姓在前 zhangsan / 名在前 sanzhang
        飞书英文名 San Zhang    → 名在前 sanzhang / 姓在前 zhangsan

    正因为两种排列都接受，撞车才成为真实风险（见 `build` 里的歧义检测）：
    线上出现过一个 `test` 账号占着某员工邮箱的情况，它的反序候选
    `sanzhang` 恰好是另一个真人的用户名。规则本身分辨不了，必须靠护栏。

    只有恰好两个 token 时才生成反序——三段式邮箱反过来拼没有任何语义，
    只会凭空制造撞车面。
    """
    out = set()
    for raw, sep in ((email_local, "."), (feishu_en, " ")):
        toks = _tokens(raw or "", sep)
        if not toks:
            continue
        out.add("".join(toks).lower())
        if len(toks) == 2:
            out.add((toks[1] + toks[0]).lower())
    return out


@dataclass(frozen=True)
class Mapping_:
    email: str
    cloud_name: str
    source: str
    platform: str = ""
    account: str = ""
    note: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.cloud_name) and self.source != SOURCE_NONE


def load_overrides(path: str) -> dict:
    """读显式登记表：{"<平台>/<账号>": {"<邮箱>": "<云用户名>"}}。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise MappingError(f"读不了 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise MappingError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise MappingError(f"{path} 的顶层必须是对象")
    out: dict = {}
    for scope, table in data.items():
        if not isinstance(table, dict):
            raise MappingError(f"{path}: `{scope}` 的值必须是对象")
        for email, name in table.items():
            if not isinstance(name, str) or not name:
                raise MappingError(f"{path}: `{scope}` 里 {email} 的映射值为空")
        out[scope] = dict(table)
    return out


def build(
    users: Iterable[AccountUser],
    *,
    domain: str,
    overrides: Optional[Mapping[str, Mapping[str, str]]] = None,
    feishu_names: Optional[Mapping[str, str]] = None,
) -> list:
    """为每个云账号算出它属于哪个飞书身份，并标明来源。

    `feishu_names` 是 `{企业邮箱: 飞书原始显示名}`，例如
    `{"zhang.san@wuji.tech": "张三（San Zhang）"}`。可选。

    四层，优先级从高到低：
      ① 显式登记（overrides）   人确认过的
      ② 邮箱候选集              企业邮箱是公司分配、全局唯一的
      ③ 显示名                  飞书中文名 == 云上显示名
      ④ 都不中 → 待办

    ③ 排在 ② 后面，因为显示名是**弱标识**：会重名，而且谁有 RAM 写权限谁就能改。
    所以它只在邮箱推不出时才用，且**任何一侧重名都拒绝**——映射配错的后果是
    A 在看板上看到 B 的权限，宁可进待办让人确认。
    """
    overrides = overrides or {}
    users = list(users)

    # 飞书侧：名字 → 邮箱。中文名和英文名分开建索引，但用同一个键空间——
    # 纯英文名的人（如 tom）云上显示名也是 tom。
    by_name: dict = {}
    feishu_en: dict = {}
    for email, raw in (feishu_names or {}).items():
        cn, en = parse_feishu_name(raw)
        feishu_en[email] = en
        for key in {_name_key(cn), _name_key(en)} - {""}:
            by_name.setdefault(key, set()).add(email)

    # 云侧：每个作用域里真实存在的用户名（候选集撞车检测用），
    # 以及显示名 → 用户名（显示名撞车检测用）。
    actual: dict = {}
    display_owners: dict = {}
    for user in users:
        scope = f"{user.platform}/{user.account}"
        actual.setdefault(scope, {})[user.name.lower()] = user.name
        key = _name_key(user.display_name)
        if key:
            display_owners.setdefault(scope, {}).setdefault(key, []).append(user.name)

    def by_display(user: AccountUser, scope: str) -> tuple:
        """按显示名找飞书身份。返回 (email, 失败原因)，二者恰有一个非空。"""
        key = _name_key(user.display_name)
        if not key:
            return "", "云上显示名为空"
        owners = display_owners.get(scope, {}).get(key, [])
        if len(owners) > 1:
            return "", (
                f"云上 {scope} 有 {len(owners)} 个账号显示名都叫「{user.display_name}」"
                f"（{', '.join(sorted(owners))}）——同一个人多个号，或者真重名"
            )
        emails = by_name.get(key, set())
        if len(emails) > 1:
            return "", f"飞书里有 {len(emails)} 个人叫「{user.display_name}」，无法确定是谁"
        if not emails:
            return "", f"飞书里没有叫「{user.display_name}」的人"
        return next(iter(emails)), ""

    def resolve_override(email: str, user: AccountUser, scope: str):
        explicit = (overrides.get(scope) or {}).get(email, "")
        if not explicit:
            return None
        # 登记值必须是这朵云上**真实存在**的账号。指向不存在的账号比不登记更糟：
        # 登记赢过规则，一条过期的登记会把规则本来算对的人覆盖成登不进去的名字，
        # 而且 resolved=True，在待办里看不见。实测踩过——改名后 12 条全部失效。
        if explicit.lower() not in actual[scope]:
            return Mapping_(
                email=email,
                cloud_name="",
                source=SOURCE_NONE,
                platform=user.platform,
                account=user.account,
                note=(f"登记值 {explicit} 在 {scope} 上不存在（账号改过名？）；云上是 {user.name}"),
            )
        note = "" if explicit == user.name else f"登记值 {explicit} 与云上 {user.name} 不一致"
        return Mapping_(
            email=email,
            cloud_name=explicit,
            source=SOURCE_OVERRIDE,
            platform=user.platform,
            account=user.account,
            note=note,
        )

    out = []
    for user in users:
        scope = f"{user.platform}/{user.account}"
        has_corp = user.email.endswith(domain)

        if not has_corp:
            # 云上没填企业邮箱。以前直接跳过——火山那边 53 人里 34 个是这种。
            # 没有飞书名册就没法按名字找，保持跳过（不制造一堆无意义的待办）。
            if not feishu_names:
                continue
            email, why = by_display(user, scope)
            if not email:
                # 只有「重名」值得进待办——那是要人去查的；
                # 「飞书里没这个人」多半是服务号或已离职，不是映射问题。
                if "个账号显示名都叫" in why or "个人叫" in why:
                    out.append(
                        Mapping_(
                            email="",
                            cloud_name="",
                            source=SOURCE_NONE,
                            platform=user.platform,
                            account=user.account,
                            note=f"{user.name}：{why}",
                        )
                    )
                continue
            hit = resolve_override(email, user, scope)
            if hit is not None:
                out.append(hit)
                continue
            out.append(
                Mapping_(
                    email=email,
                    cloud_name=user.name,
                    source=SOURCE_DISPLAY_NAME,
                    platform=user.platform,
                    account=user.account,
                    note=f"云上没有企业邮箱，按显示名「{user.display_name}」对上",
                )
            )
            continue

        hit = resolve_override(user.email, user, scope)
        if hit is not None:
            out.append(hit)
            continue

        cands = candidates(user.email.split("@", 1)[0], feishu_en=feishu_en.get(user.email, ""))

        # 歧义：候选里有串指向**别人**的真实账号。不能只看「自己中没中」——
        # `test` 持有某员工邮箱时自己不中，但它的候选 sanzhang
        # 是张三的账号；反过来若两人都中，SSO 那天一个邮箱会解析出两个账号。
        stolen = sorted(
            actual[scope][c] for c in cands if c in actual[scope] and c != user.name.lower()
        )
        if stolen:
            out.append(
                Mapping_(
                    email=user.email,
                    cloud_name="",
                    source=SOURCE_NONE,
                    platform=user.platform,
                    account=user.account,
                    note=(
                        f"歧义：候选同时指向 {', '.join(stolen)}；"
                        f"云上是 {user.name}；必须登记到 overrides"
                    ),
                )
            )
            continue

        if user.name.lower() in cands:
            out.append(
                Mapping_(
                    email=user.email,
                    cloud_name=user.name,
                    source=SOURCE_RULE,
                    platform=user.platform,
                    account=user.account,
                )
            )
            continue

        # 邮箱推不出（拼音不一致：圳 zhen/shen、秉 bin/bing、黄 huang/hua……）。
        # 这时显示名是**确认**而不是**猜测**：邮箱已经把人钉住了，只是核对名字。
        if feishu_names and user.email in feishu_names:
            cn, en = parse_feishu_name(feishu_names[user.email])
            owners = display_owners.get(scope, {}).get(_name_key(user.display_name), [])
            same = _name_key(user.display_name) in {_name_key(cn), _name_key(en)} - {""}
            if same and len(owners) == 1:
                out.append(
                    Mapping_(
                        email=user.email,
                        cloud_name=user.name,
                        source=SOURCE_DISPLAY_NAME,
                        platform=user.platform,
                        account=user.account,
                        note=f"邮箱规则推不出，飞书名「{feishu_names[user.email]}」与云上显示名一致",
                    )
                )
                continue

        shown = " / ".join(sorted(cands)) or "(无)"
        out.append(
            Mapping_(
                email=user.email,
                cloud_name="",
                source=SOURCE_NONE,
                platform=user.platform,
                account=user.account,
                note=f"规则推出 {shown}，云上是 {user.name}；请登记到 overrides",
            )
        )
    out.sort(key=lambda x: (x.source != SOURCE_NONE, x.platform, x.email))
    return out


def unresolved(mappings: Iterable[Mapping_]) -> list:
    return [m for m in mappings if not m.resolved]


def to_override_stub(mappings: Iterable[Mapping_]) -> str:
    """把推不出来的那些生成成可直接粘进 overrides 文件的骨架。"""
    grouped: dict = {}
    for m in mappings:
        if m.resolved:
            continue
        scope = f"{m.platform}/{m.account}"
        # 值预填成"云上实际用户名"——这正是绝大多数情况下的正确答案，
        # 管理员只需要核对而不是从头查。
        guessed = m.note.split("云上是", 1)[-1].split("；")[0].strip() if "云上是" in m.note else ""
        grouped.setdefault(scope, {})[m.email] = guessed
    return json.dumps(grouped, ensure_ascii=False, indent=2)
