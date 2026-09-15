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
            "policies": sorted(set(user_policies.get(str(u.get("UserName") or ""), []))),
            "groups": sorted(user_groups.get(str(u.get("UserName") or ""), [])),
        }
        for u in users
    ]
    return {"platform": "aliyun", "account": uid, "users": out_users, "groups": out_groups}


# ── 火山 ──────────────────────────────────────────────────────────────────


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
                "policies": policies,
                "groups": sorted(user_groups.get(name, [])),
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
