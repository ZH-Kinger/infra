"""采集权限快照：两朵云上「每个子账号有哪些权限、在哪些组」。

产出 `inventory.parse` 认的格式。和 `identity/cloudcollect.py` 分开，是因为那边只关心
账号和邮箱（用来认人），这边只关心权限（用来展示），两份数据的更新频率和敏感度不同。

几条取舍
────────
① 阿里云的授权从**资源管理**的 `ListPolicyAttachments` 取，不从 RAM 的
   `ListPoliciesForUser` 取：后者只返回账号级授权，授在资源组上的查不到。
   主账号 2026-09-15 实测有 6 条资源组级授权，只看 RAM 会把这些人报少。
② 授权范围写进策略名（`策略 @资源组:rg-xxx`）。快照格式的 policies 是字符串数组，
   为了一个范围字段改格式不值得；高危判定是子串匹配，带后缀不影响。
③ **一个云账号内任何一步失败，整个云账号记为 error，不产出半份数据。**
   半份数据在看板上的样子是「这个人没有权限」，和真的没有权限分不出来。
   `inventory.parse` 会把 error 显示成「快照不完整」。
"""

from __future__ import annotations

import datetime
from typing import Callable, Optional

from .clouds import aliyun, volcano
from .errors import DeliveryError

Progress = Callable[[str], None]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def _principal(name: str) -> str:
    """`alice@1704….onaliyun.com` / `ops@group.1704….onaliyun.com` → `alice` / `ops`。"""
    return name.split("@", 1)[0]


# ── 阿里云 ────────────────────────────────────────────────────────────────


def _aliyun_attachments(creds, transport) -> list:
    out, page = [], 1
    while True:
        body = aliyun.call(
            *aliyun.RESOURCE_MANAGER,
            "ListPolicyAttachments",
            {"PageSize": 100, "PageNumber": page},
            creds=creds,
            transport=transport,
        )
        node = body.get("PolicyAttachments")
        if not isinstance(node, dict) or "TotalCount" not in body:
            raise aliyun.AliyunError("ListPolicyAttachments 响应缺 PolicyAttachments/TotalCount")
        batch = node.get("PolicyAttachment") or []
        out += batch
        if len(out) >= int(body["TotalCount"]):
            return out
        if not batch:
            raise aliyun.AliyunError(
                f"ListPolicyAttachments 第 {page} 页为空，"
                f"但只取到 {len(out)}/{body['TotalCount']} 条，数据不完整，已中断"
            )
        page += 1
        if page > 200:
            raise aliyun.AliyunError("ListPolicyAttachments 翻页超过 200 页，已中断")


def _aliyun_login(creds, user: str, transport):
    """这个号还能不能登控制台。`True/False` 是已知，**`None` 是没采到**。

    为什么要采
    ──────────
    面板原先只知道「自己做过什么」，不知道云上现在是什么样。于是在控制台上手工停过的号，
    面板照样显示「还开着」，而管理员会照着那个标签做判断 —— 信息不准这件事，根在这里。

    阿里云没有「禁止登录」开关：有登录配置 = 能登，删掉配置 = 不能登（控制台上那个
    「禁用控制台登录」就是删配置）。所以 `EntityNotExist.*.LoginProfile` 是明确的「否」，
    不是「查不到」—— 这两者混同的话，没配登录的号会被当成不确定，永远没人处理。
    """
    try:
        aliyun.call(
            *aliyun.RAM, "GetLoginProfile", {"UserName": user}, creds=creds, transport=transport
        )
        return True
    except aliyun.AliyunDenied:
        return None  # 采集身份没这个权限：不知道，别当成「不能登」
    except aliyun.AliyunError as exc:
        if "LoginProfile" in str(exc.code or ""):
            return False
        if "NotExist" in str(exc.code or ""):
            return None  # 号都没了，交给别的检查去说
        raise


def _aliyun_keys(creds, user: str, transport) -> list:
    """一个 RAM 子账号的 AK 清单。**绝不返回 secret**（那个接口本来也不给）。

    `GetAccessKeyLastUsed` 是关键：没有它，「该轮换了」和「这把根本没人用」分不开，
    而这两种的处置完全相反 —— 前者要提醒本人换，后者该直接停用。
    本次审计就是靠它发现 feishu-bot-master 那把 AK 建出来 163 天一次没用过。
    """
    try:
        items = aliyun.paginate(
            *aliyun.RAM,
            "ListAccessKeys",
            key="AccessKey",
            container="AccessKeys",
            params={"UserName": user},
            creds=creds,
            transport=transport,
        )
    except aliyun.AliyunDenied:
        # 没给这个权限就当没有这项信息。**不要吞成空列表**外加静默——
        # 上层按 `keys is None` 区分「没有 AK」和「没采到」
        return None
    out = []
    for k in items:
        kid = str(k.get("AccessKeyId") or "")
        if not kid:
            continue
        last = ""
        try:
            got = aliyun.call(
                *aliyun.RAM,
                "GetAccessKeyLastUsed",
                {"UserName": user, "UserAccessKeyId": kid},
                creds=creds,
                transport=transport,
            )
            last = str((got.get("AccessKeyLastUsed") or {}).get("LastUsedDate") or "")
        except aliyun.AliyunError:
            last = ""
        out.append(
            {
                # 只留前 8 位：足够在两次采集之间认出是同一把，又不至于把完整 AKId
                # 写进一个会被传阅的快照文件
                "id": kid[:8],
                "status": str(k.get("Status") or ""),
                "created": str(k.get("CreateDate") or ""),
                "last_used": last,
            }
        )
    return out


def collect_aliyun(creds, *, transport=None, progress: Optional[Progress] = None) -> dict:
    uid = str(
        aliyun.call(*aliyun.STS, "GetCallerIdentity", creds=creds, transport=transport).get(
            "AccountId"
        )
        or ""
    )
    if not uid:
        raise aliyun.AliyunError("GetCallerIdentity 没有返回 AccountId")

    users = aliyun.paginate(
        *aliyun.RAM, "ListUsers", key="User", container="Users", creds=creds, transport=transport
    )
    groups = aliyun.paginate(
        *aliyun.RAM, "ListGroups", key="Group", container="Groups", creds=creds, transport=transport
    )
    if progress:
        progress(f"阿里云 {uid}：{len(users)} 个用户，{len(groups)} 个组")

    user_policies: dict = {}
    group_policies: dict = {}
    for att in _aliyun_attachments(creds, transport):
        kind = att.get("PrincipalType")
        if kind not in ("IMSUser", "IMSGroup"):
            continue  # ServiceRole 不是人，不进人员看板
        policy = str(att.get("PolicyName") or "")
        if not policy:
            raise aliyun.AliyunError("ListPolicyAttachments 返回了没有 PolicyName 的授权")
        rg = str(att.get("ResourceGroupId") or "")
        if rg and rg != uid:
            policy = f"{policy} @资源组:{rg}"
        target = user_policies if kind == "IMSUser" else group_policies
        target.setdefault(_principal(str(att.get("PrincipalName") or "")), []).append(policy)

    user_groups: dict = {}
    out_groups = []
    for g in groups:
        gname = str(g.get("GroupName") or "")
        members = aliyun.paginate(
            *aliyun.RAM,
            "ListUsersForGroup",
            key="User",
            container="Users",
            params={"GroupName": gname},
            creds=creds,
            transport=transport,
        )
        names = [str(m.get("UserName") or "") for m in members]
        for n in names:
            user_groups.setdefault(n, []).append(gname)
        out_groups.append(
            {
                "name": gname,
                "display_name": str(g.get("Comments") or ""),
                "policies": sorted(set(group_policies.get(gname, []))),
                "members": sorted(names),
            }
        )

    out_users = [
        {
            "name": str(u.get("UserName") or ""),
            "display_name": str(u.get("DisplayName") or ""),
            # RAM 用户自带的邮箱字段，**只作展示**。注意这里通常是空的：
            # 公司大多数人的邮箱只存在阿里云「安全邮箱」（IMS）里，映射提案那条链路才读得到
            "email": str(u.get("Email") or ""),
            "policies": sorted(set(user_policies.get(str(u.get("UserName") or ""), []))),
            "groups": sorted(user_groups.get(str(u.get("UserName") or ""), [])),
            "keys": _aliyun_keys(creds, str(u.get("UserName") or ""), transport),
            "login_enabled": _aliyun_login(creds, str(u.get("UserName") or ""), transport),
        }
        for u in users
    ]
    return {"platform": "aliyun", "account": uid, "users": out_users, "groups": out_groups}


# ── 火山 ──────────────────────────────────────────────────────────────────


def _volcano_login(creds, user: str, transport):
    """火山：这个号还能不能登控制台。`True/False` 是已知，**`None` 是没采到**。

    火山有显式的 `LoginAllowed` 开关（阿里没有），但**对没有登录配置的号返回一个全零的
    假对象、不报错**（bot 那边记过这个坑）。所以不能只看「有没有抛异常」，
    要看 `LoginAllowed` 到底是不是真的 —— 否则从没开过登录的号会被记成「能登」。
    """
    try:
        got = volcano.call(
            *volcano.IAM, "GetLoginProfile", {"UserName": user}, creds=creds, transport=transport
        )
    except volcano.VolcanoDenied:
        return None
    except volcano.VolcanoError as exc:
        # 用**错误码**判，不用整条消息：消息里带 "does not exist" 这种人类写法时，
        # 按整条匹配会漏判；而漏判的方向是「不知道」，看着安全，实际是这一栏悄悄变空
        from .provision import _volcano_code

        return False if "notexist" in _volcano_code(exc) else None
    profile = got.get("LoginProfile") or got
    if not isinstance(profile, dict):
        return None
    return str(profile.get("LoginAllowed", "")).lower() in ("true", "1")


def _volcano_policies(result: dict, action: str) -> list:
    items = result.get("AttachedPolicyMetadata")
    if items is None:
        raise volcano.VolcanoError(f"`{action}` 的响应缺 AttachedPolicyMetadata，不能当作没有权限")
    out = []
    for item in items:
        name = str(item.get("PolicyName") or "")
        scopes = item.get("PolicyScope") or [{"PolicyScopeType": "Global"}]
        for scope in scopes:
            if scope.get("PolicyScopeType") == "Project" and scope.get("ProjectName"):
                out.append(f"{name} @项目:{scope['ProjectName']}")
            else:
                out.append(name)
    return sorted(set(out))


def _volcano_keys(creds, user: str, transport) -> Optional[list]:
    """一个火山 IAM 子账号的 AK 清单。**绝不返回 secret**（接口本来也不给）。

    和阿里那侧同一套三态语义：拿不到权限返回 **None**（上层按 `keys is None`
    区分「没有 AK」和「没采到」），绝不吞成空列表。

    火山的 `ListAccessKeys` 直接在每条里带 `CreateDate`/`UpdateDate`/`Status`，
    **没有**阿里那种单独的 `GetAccessKeyLastUsed` —— 所以「多久没用过」这一维
    火山这边拿不到，`last_used` 只能留空。下游据此只会报「该换了」，
    不会报「没人用」（那一类需要最近使用时间，没有就不该猜）。
    """
    try:
        items = volcano.paginate(
            *volcano.IAM,
            "ListAccessKeys",
            key="AccessKeyMetadata",
            params={"UserName": user},
            creds=creds,
            transport=transport,
        )
    except volcano.VolcanoDenied:
        return None
    out = []
    for k in items:
        kid = str(k.get("AccessKeyId") or "")
        if not kid:
            continue
        out.append(
            {
                # 同阿里：只留前 8 位，够在两次采集之间认出是同一把，
                # 又不至于把完整 AKId 写进一个会被传阅的快照文件
                "id": kid[:8],
                "status": str(k.get("Status") or ""),
                "created": str(k.get("CreateDate") or ""),
                # **火山给不了最近使用时间**，所以这里标明「不知道」而不是留空。
                # 留空会被下游当成「从来没用过」→ 44 把 AK 同时被报成「没人用，停掉吧」，
                # 而那是 44 条假线索，足以让这一栏从此没人看
                "last_used": "",
                "last_used_known": False,
            }
        )
    return out


def collect_volcano(creds, *, transport=None, progress: Optional[Progress] = None) -> dict:
    users = volcano.paginate(
        *volcano.IAM, "ListUsers", key="UserMetadata", creds=creds, transport=transport
    )
    ids = {str(u.get("AccountId") or "") for u in users} - {""}
    if len(ids) > 1:
        raise volcano.VolcanoError(f"ListUsers 返回了多个主账号 {sorted(ids)}，拒绝混在一起")
    account = next(iter(ids), "default")
    groups = volcano.paginate(
        *volcano.IAM, "ListGroups", key="UserGroups", creds=creds, transport=transport
    )
    if progress:
        progress(f"火山 {account}：{len(users)} 个用户，{len(groups)} 个组")

    user_groups: dict = {}
    out_groups = []
    for g in groups:
        gname = str(g.get("UserGroupName") or "")
        members = volcano.paginate(
            *volcano.IAM,
            "ListUsersForGroup",
            key="Users",
            params={"UserGroupName": gname},
            creds=creds,
            transport=transport,
        )
        names = [str(m.get("UserName") or "") for m in members]
        for n in names:
            user_groups.setdefault(n, []).append(gname)
        policies = _volcano_policies(
            volcano.call(
                *volcano.IAM,
                "ListAttachedUserGroupPolicies",
                {"UserGroupName": gname},
                creds=creds,
                transport=transport,
            ),
            "ListAttachedUserGroupPolicies",
        )
        out_groups.append(
            {
                "name": gname,
                "display_name": str(g.get("DisplayName") or ""),
                "policies": policies,
                "members": sorted(names),
            }
        )

    out_users = []
    for i, u in enumerate(users, 1):
        name = str(u.get("UserName") or "")
        if progress:
            progress(f"火山 {i}/{len(users)} {name}")
        policies = _volcano_policies(
            volcano.call(
                *volcano.IAM,
                "ListAttachedUserPolicies",
                {"UserName": name},
                creds=creds,
                transport=transport,
            ),
            "ListAttachedUserPolicies",
        )
        out_users.append(
            {
                "name": name,
                "display_name": str(u.get("DisplayName") or ""),
                # 云上这个号自己登记的邮箱，**只作展示**：可能是个人邮箱、可能没验证。
                # 认人只走名册（people.py），任何判断都要自己过企业域 + 验证态（见 ssomap.py）
                "email": str(u.get("Email") or ""),
                "policies": policies,
                "groups": sorted(user_groups.get(name, [])),
                "keys": _volcano_keys(creds, name, transport),
                "login_enabled": _volcano_login(creds, name, transport),
            }
        )
    return {"platform": "volcano", "account": account, "users": out_users, "groups": out_groups}


# ── 汇总 ──────────────────────────────────────────────────────────────────


def build_snapshot(jobs, *, progress: Optional[Progress] = None, now: Callable = _now) -> dict:
    """`jobs` 是 `[(platform, account_hint, callable)]`。

    单个云账号失败只让它自己变成 error 条目，其它照常采；但**不吞**：
    error 会原样进快照，看板显示「快照不完整」。
    """
    accounts = []
    for platform, hint, fn in jobs:
        try:
            accounts.append(fn(progress))
        except DeliveryError as exc:
            # 只留第一行：错误消息里可能有很长的接口返回，看板上一行够判断原因
            # 取第一行非空文本；消息为空也必须留下非空 error，否则 parse 会把这个账号
            # 当成「采集完整、没有用户」——正是这个模块要防的情况
            first = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
            # 错误会进快照、面板和飞书告警：两家的凭证回显都去掉
            first = aliyun._scrub(volcano._scrub(first))
            accounts.append(
                {
                    "platform": platform,
                    "account": hint,
                    "error": (first or type(exc).__name__)[:200],
                }
            )
    return {"captured_at": now(), "accounts": accounts}


__all__ = ["build_snapshot", "collect_aliyun", "collect_volcano"]
