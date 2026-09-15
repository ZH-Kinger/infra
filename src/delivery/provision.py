"""执行器：审批通过后真正写云的那几个动作。

只有这几个动作，刻意不做通用：
  in_group        子账号当前是否在某个用户组（开通前记下，到期回收时不误删原有权限）
  add_to_group    子账号加入用户组（权限申请、开账号）
  remove_from_group  权限到期后移出用户组
  create_user     建子账号（开账号）；同名已存在一律报错，**不接管已有账号**
  reset_password  开通或重置控制台登录密码，强制首次登录修改（领取初始密码）
  assume_role     扮演模板里的受限角色，拿 STS 临时凭证（领取访问凭证）

执行身份的凭证按云账号分开配置（环境变量，前缀 `DELIVERY_EXEC_<平台>_<账号ID>`），
每次写操作前先确认凭证确实属于目标云账号：配错账号时宁可失败，也不能在另一个主账号下开号。
执行身份应该只有这几个动作的权限（docs/cloud-access-platform.md 规则 R5）。
"""

from __future__ import annotations

import os
import re
import secrets
import string
from dataclasses import dataclass
from typing import Callable, Optional

from .clouds import aliyun, volcano
from .errors import DeliveryError

_SESSION_NAME = re.compile(r"[^A-Za-z0-9.@_-]")
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#%^*-_=+"


class ProvisionError(DeliveryError):
    """执行失败。消息会进申请单事件，不能带凭证。"""


@dataclass(frozen=True)
class TempCredential:
    access_key_id: str
    secret: str
    token: str
    expiration: str

    def __repr__(self) -> str:
        return f"TempCredential(ak=…{self.access_key_id[-4:]}, expiration={self.expiration})"


def session_name(value: str) -> str:
    """STS 会话名：云审计里显示为操作人，只保留允许的字符。"""
    cleaned = _SESSION_NAME.sub("-", value)[:64]
    return cleaned if len(cleaned) >= 2 else f"u-{cleaned}"


def new_password() -> str:
    """满足两家默认密码策略：大小写、数字、符号各至少一个，20 位。"""
    while True:
        pw = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(20))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(not c.isalnum() for c in pw)
        ):
            return pw


def exec_env_prefix(platform: str, account: str) -> str:
    return f"DELIVERY_EXEC_{platform.upper()}_{account}"


class AliyunExecutor:
    platform = "aliyun"

    def __init__(self, account: str, creds: aliyun.Credentials, *, transport=None):
        self.account = account
        self._creds = creds
        self._transport = transport
        self._checked = False

    @classmethod
    def from_env(cls, account: str, environ=None) -> AliyunExecutor:
        env = os.environ if environ is None else environ
        prefix = exec_env_prefix("aliyun", account)
        ak = env.get(f"{prefix}_ACCESS_KEY_ID", "")
        sk = env.get(f"{prefix}_ACCESS_KEY_SECRET", "")
        if not ak or not sk:
            raise ProvisionError(f"没有配置阿里云 {account} 的执行身份（{prefix}_ACCESS_KEY_ID）")
        return cls(account, aliyun.Credentials(ak, sk))

    def _call(self, api, action: str, params: dict) -> dict:
        return aliyun.call(*api, action, params, creds=self._creds, transport=self._transport)

    def _check_account(self) -> None:
        if self._checked:
            return
        body = self._call(aliyun.STS, "GetCallerIdentity", {})
        if str(body.get("AccountId") or "") != self.account:
            raise ProvisionError("执行身份不属于目标云账号，已停止（检查执行凭证配置）")
        self._checked = True

    def user_exists(self, user: str) -> bool:
        self._check_account()
        try:
            self._call(aliyun.RAM, "GetUser", {"UserName": user})
        except aliyun.AliyunError as exc:
            if exc.code == "EntityNotExist.User":
                return False
            raise
        return True

    def add_to_group(self, user: str, group: str) -> None:
        self._check_account()
        if not self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 不存在")
        try:
            self._call(aliyun.RAM, "AddUserToGroup", {"UserName": user, "GroupName": group})
        except aliyun.AliyunError as exc:
            if exc.code != "EntityAlreadyExists.User.Group":
                raise

    def in_group(self, user: str, group: str) -> bool:
        self._check_account()
        body = self._call(aliyun.RAM, "ListGroupsForUser", {"UserName": user})
        groups = (body.get("Groups") or {}).get("Group") or []
        return any(str(g.get("GroupName")) == group for g in groups)

    def remove_from_group(self, user: str, group: str) -> None:
        self._check_account()
        try:
            self._call(aliyun.RAM, "RemoveUserFromGroup", {"UserName": user, "GroupName": group})
        except aliyun.AliyunError as exc:
            if exc.code not in ("EntityNotExist.User.Group", "EntityNotExist.User"):
                raise

    def create_user(self, user: str, display_name: str) -> None:
        self._check_account()
        if self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 已存在，不会接管已有账号，请换一个用户名")
        self._call(aliyun.RAM, "CreateUser", {"UserName": user, "DisplayName": display_name[:24]})

    def reset_password(self, user: str) -> str:
        self._check_account()
        password = new_password()
        params = {"UserName": user, "Password": password, "PasswordResetRequired": "true"}
        try:
            self._call(aliyun.RAM, "CreateLoginProfile", params)
        except aliyun.AliyunError as exc:
            if exc.code != "EntityAlreadyExists.User.LoginProfile":
                raise
            self._call(aliyun.RAM, "UpdateLoginProfile", params)
        return password

    def assume_role(self, role_arn: str, name: str, hours: int) -> TempCredential:
        self._check_account()
        body = self._call(
            aliyun.STS,
            "AssumeRole",
            {
                "RoleArn": role_arn,
                "RoleSessionName": session_name(name),
                "DurationSeconds": str(hours * 3600),
            },
        )
        c = body.get("Credentials") or {}
        try:
            return TempCredential(
                c["AccessKeyId"], c["AccessKeySecret"], c["SecurityToken"], c["Expiration"]
            )
        except KeyError:
            raise ProvisionError("AssumeRole 返回缺少凭证字段") from None


class VolcanoExecutor:
    platform = "volcano"
    STS = ("sts", "2018-01-01")
    #: STS 走独立域名和区域（官方 SDK volcengine/sts/StsService.py）
    STS_HOST = "sts.volcengineapi.com"
    STS_REGION = "cn-north-1"

    def __init__(self, account: str, creds: volcano.Credentials, *, transport=None):
        self.account = account
        self._creds = creds
        self._transport = transport
        self._checked = False

    @classmethod
    def from_env(cls, account: str, environ=None) -> VolcanoExecutor:
        env = os.environ if environ is None else environ
        prefix = exec_env_prefix("volcano", account)
        ak = env.get(f"{prefix}_ACCESS_KEY", "")
        sk = env.get(f"{prefix}_SECRET_KEY", "")
        if not ak or not sk:
            raise ProvisionError(f"没有配置火山 {account} 的执行身份（{prefix}_ACCESS_KEY）")
        return cls(account, volcano.Credentials(ak, sk))

    def _call(self, api, action: str, params: dict, **kw) -> dict:
        return volcano.call(
            *api, action, params, creds=self._creds, transport=self._transport, **kw
        )

    def _check_account(self) -> None:
        if self._checked:
            return
        body = self._call(volcano.IAM, "ListUsers", {"Limit": "1"})
        users = body.get("UserMetadata") or []
        accounts = {_volcano_account(u) for u in users} - {""}
        # 失败即关：确认不了凭证属于哪个账号（没有子账号、返回里缺字段）同样停止
        if accounts != {self.account}:
            raise ProvisionError("无法确认执行身份属于目标云账号，已停止（检查执行凭证配置）")
        self._checked = True

    def user_exists(self, user: str) -> bool:
        self._check_account()
        try:
            self._call(volcano.IAM, "GetUser", {"UserName": user})
        except volcano.VolcanoError as exc:
            if _volcano_user_missing(exc):
                return False
            raise
        return True

    def add_to_group(self, user: str, group: str) -> None:
        if not self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 不存在")
        try:
            self._call(volcano.IAM, "AddUserToGroup", {"UserName": user, "UserGroupName": group})
        except volcano.VolcanoError as exc:
            if "alreadyexist" not in str(exc).lower().replace(".", ""):
                raise

    def in_group(self, user: str, group: str) -> bool:
        self._check_account()
        for page in range(50):
            body = self._call(
                volcano.IAM,
                "ListGroupsForUser",
                {"UserName": user, "Limit": "100", "Offset": str(page * 100)},
            )
            groups = body.get("UserGroupMetadata") or []
            if any(str(g.get("UserGroupName")) == group for g in groups):
                return True
            if len(groups) < 100:
                return False
        raise ProvisionError("火山 ListGroupsForUser 翻页超过上限，已中断")

    def remove_from_group(self, user: str, group: str) -> None:
        self._check_account()
        # 先查是否在组里，而不是吞「不存在」类错误：用户组名写错时要报错，不能当作已回收
        try:
            if not self.in_group(user, group):
                return
        except volcano.VolcanoError as exc:
            if _volcano_user_missing(exc):
                return  # 子账号已经删了，权限自然没了
            raise
        self._call(volcano.IAM, "RemoveUserFromGroup", {"UserName": user, "UserGroupName": group})

    def create_user(self, user: str, display_name: str) -> None:
        if self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 已存在，不会接管已有账号，请换一个用户名")
        self._call(volcano.IAM, "CreateUser", {"UserName": user, "DisplayName": display_name[:64]})

    def reset_password(self, user: str) -> str:
        self._check_account()
        password = new_password()
        params = {
            "UserName": user,
            "Password": password,
            "LoginAllowed": "true",
            "PasswordResetRequired": "true",
        }
        try:
            self._call(volcano.IAM, "CreateLoginProfile", params)
        except volcano.VolcanoError as exc:
            if "alreadyexist" not in str(exc).lower().replace(".", ""):
                raise
            self._call(volcano.IAM, "UpdateLoginProfile", params)
        return password

    def assume_role(self, role_trn: str, name: str, hours: int) -> TempCredential:
        self._check_account()
        body = self._call(
            self.STS,
            "AssumeRole",
            {
                "RoleTrn": role_trn,
                "RoleSessionName": session_name(name),
                "DurationSeconds": str(hours * 3600),
            },
            host=self.STS_HOST,
            region=self.STS_REGION,
        )
        c = body.get("Credentials") or {}
        try:
            return TempCredential(
                c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"], c["ExpiredTime"]
            )
        except KeyError:
            raise ProvisionError("AssumeRole 返回缺少凭证字段") from None


def _volcano_account(user: dict) -> str:
    """子账号所属主账号：优先 AccountId，没有就从 Trn（trn:iam::<账号>:user/...）里取。"""
    account = str(user.get("AccountId") or "")
    if account:
        return account
    match = re.match(r"^trn:iam::([0-9]+):", str(user.get("Trn") or ""))
    return match.group(1) if match else ""


def _volcano_user_missing(exc: Exception) -> bool:
    """只认「子账号不存在」：用户组不存在、策略不存在之类的错误照样抛出。"""
    match = re.search(r"HTTP \d+：(\S+)", str(exc))
    code = (match.group(1) if match else "").lower().replace(".", "").replace("_", "")
    return "notexist" in code and "user" in code and "group" not in code


Factory = Callable[[str, str], object]


def executor_from_env(platform: str, account: str) -> object:
    if platform == "aliyun":
        return AliyunExecutor.from_env(account)
    if platform == "volcano":
        return VolcanoExecutor.from_env(account)
    raise ProvisionError(f"不支持的平台 {platform}")


def describe_error(exc: Exception) -> Optional[str]:
    """执行错误写进申请单事件：只取第一行，并去掉凭证回显。"""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    first = lines[0] if lines else type(exc).__name__
    return aliyun._scrub(volcano._scrub(first))[:300]
