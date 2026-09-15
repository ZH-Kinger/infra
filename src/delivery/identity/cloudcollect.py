"""从两朵云实时采集账号，产出 `ssomap.CloudAccount`。

和 `collect.py` 的区别：那边调官方 CLI、只取 RAM 用户邮箱字段；这边用零依赖客户端，
而且**会读阿里云的安全邮箱**。实测阿里云 48 个企业邮箱里有 34 个只存在于安全邮箱，
只读 RAM 用户邮箱字段会漏掉七成的人。

采集原则同其它模块：权限不足、响应形状不对一律中断，不产出不完整的清单。
唯一允许当作「没有」的是明确的「不存在」错误——某个用户没绑安全邮箱是正常情况。
"""

from __future__ import annotations

from typing import Callable, Optional

from ..clouds import aliyun, volcano
from .ssomap import (
    SOURCE_IAM_EMAIL,
    SOURCE_RAM_EMAIL,
    SOURCE_SECURITY_EMAIL,
    CloudAccount,
    EmailClaim,
)

#: 阿里云「不存在」类错误码。只有这些可以解释为「这个用户没有安全邮箱」。
_NOT_EXIST = ("EntityNotExist", "NotExist")

Progress = Callable[[str], None]


def aliyun_account_id(creds: aliyun.Credentials, *, transport=None) -> str:
    body = aliyun.call(*aliyun.STS, "GetCallerIdentity", creds=creds, transport=transport)
    uid = str(body.get("AccountId") or "")
    if not uid:
        raise aliyun.AliyunError("GetCallerIdentity 没有返回 AccountId，无法确定采集的是哪个账号")
    return uid


def collect_aliyun(
    creds: aliyun.Credentials,
    *,
    transport=None,
    progress: Optional[Progress] = None,
) -> list:
    uid = aliyun_account_id(creds, transport=transport)
    scope = f"aliyun/{uid}"
    users = aliyun.paginate(
        *aliyun.RAM, "ListUsers", key="User", container="Users", creds=creds, transport=transport
    )
    out = []
    for i, item in enumerate(users, 1):
        name = str(item.get("UserName") or "")
        if not name:
            raise aliyun.AliyunError("ListUsers 返回了没有 UserName 的条目")
        if progress:
            progress(f"阿里云 {i}/{len(users)} {name}")
        detail = aliyun.call(
            *aliyun.RAM, "GetUser", {"UserName": name}, creds=creds, transport=transport
        )
        user = detail.get("User")
        if not isinstance(user, dict):
            raise aliyun.AliyunError(f"GetUser({name}) 响应缺 User，拒绝当空值处理")
        claims = []
        if user.get("Email"):
            claims.append(EmailClaim(str(user["Email"]), SOURCE_RAM_EMAIL, False))
        try:
            info = aliyun.call(
                *aliyun.IMS,
                "GetVerificationInfo",
                {"UserPrincipalName": f"{name}@{uid}.onaliyun.com"},
                creds=creds,
                transport=transport,
            )
        except aliyun.AliyunDenied:
            raise
        except aliyun.AliyunError as exc:
            if not any(m in exc.code for m in _NOT_EXIST):
                raise
            info = {}
        device = info.get("SecurityEmailDevice") or {}
        if device.get("Email"):
            status = str(device.get("Status") or "").lower()
            claims.append(
                EmailClaim(str(device["Email"]), SOURCE_SECURITY_EMAIL, status == "active")
            )
        out.append(CloudAccount(scope, name, str(user.get("DisplayName") or ""), tuple(claims)))
    return out


def collect_volcano(
    creds: volcano.Credentials,
    *,
    transport=None,
    progress: Optional[Progress] = None,
) -> list:
    users = volcano.paginate(
        *volcano.IAM, "ListUsers", key="UserMetadata", creds=creds, transport=transport
    )
    out = []
    account_ids = {str(u.get("AccountId") or "") for u in users} - {""}
    if len(account_ids) > 1:
        raise volcano.VolcanoError(
            f"ListUsers 返回了多个主账号 {sorted(account_ids)}，拒绝混在一起"
        )
    scope = f"volcano/{next(iter(account_ids), 'default')}"
    for item in users:
        name = str(item.get("UserName") or "")
        if not name:
            raise volcano.VolcanoError("ListUsers 返回了没有 UserName 的条目")
        claims = []
        if item.get("Email"):
            claims.append(
                EmailClaim(str(item["Email"]), SOURCE_IAM_EMAIL, bool(item.get("EmailIsVerify")))
            )
        out.append(CloudAccount(scope, name, str(item.get("DisplayName") or ""), tuple(claims)))
    if progress:
        progress(f"火山 {len(out)} 个账号")
    return out
