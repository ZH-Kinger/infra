"""执行器：审批通过后真正写云的那几个动作。

只有这几个动作，刻意不做通用：
  in_group        子账号当前是否在某个用户组（开通前记下，到期回收时不误删原有权限）
  add_to_group    子账号加入用户组（权限申请、开账号）
  remove_from_group  权限到期后移出用户组
  has_policy      子账号当前是否直接被授予某条策略（开通前记下，到期回收时不误删原有授权）
  attach_policy   给子账号授予策略（从策略目录申请的权限）
  detach_policy   权限到期后撤销授予的策略
  create_user     建子账号（开账号）；同名已存在一律报错，**不接管已有账号**
  reset_password  开通或重置控制台登录密码，强制首次登录修改（领取初始密码）
  assume_role     扮演模板里的受限角色，拿 STS 临时凭证（领取访问凭证）

执行身份的凭证按云账号分开配置（环境变量，前缀 `DELIVERY_EXEC_<平台>_<账号ID>`），
每次写操作前先确认凭证确实属于目标云账号：配错账号时宁可失败，也不能在另一个主账号下开号。
执行身份应该只有这几个动作的权限（docs/cloud-access-platform.md 规则 R5）。

**授予策略的权限约等于管理员**：能 AttachPolicyToUser 就能给任何子账号任何策略。
能不能授予由服务端的策略规则（policies.py 禁用清单）把关，执行凭证必须按最高敏感度保管。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from . import grants, platforms
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


def issuer_env_prefix(platform: str, account: str) -> str:
    """凭证发放身份的环境变量前缀。

    **和开通身份是两把不同的 AK，云上的策略也是分开收窄的**，这不是洁癖：
    开通身份（panel-executor）被明确禁止建子账号、发 AccessKey、造策略，
    就是为了面板逻辑出漏洞时炸不到真人账号；而长期凭证恰恰需要这三样。
    共用一把 = 把那道闸拆了，面板任何一处越权都能直接造出一个带 AK 的子账号。
    """
    return f"DELIVERY_ISSUER_{platform.upper()}_{account}"


def executor_configured(platform: str, account: str, environ: Optional[Mapping] = None) -> bool:
    """这个云账号的执行身份配了没有。**只看环境变量在不在，不调云 API**。

    给「申请」页用：没配的模板要提前置灰。否则员工能提交、能等到审批通过，
    最后卡在开通那一步失败 —— 白等一轮，还留下一张要人工处理的失败单。
    """
    env = os.environ if environ is None else environ
    names = platforms.cred_env_names(platform, exec_env_prefix(platform, account))
    return all(env.get(n) for n in names)


def issuer_configured(platform: str, account: str, environ: Optional[Mapping] = None) -> bool:
    """凭证发放身份配了没有。同样只看环境变量，给凭证模板提前置灰用。"""
    env = os.environ if environ is None else environ
    names = platforms.cred_env_names(platform, issuer_env_prefix(platform, account))
    return all(env.get(n) for n in names)


@dataclass(frozen=True)
class LongTermCredential:
    """长期凭证。**secret 只在发放那一刻存在于内存里**，绝不落库、不进日志、不进通知。"""

    user: str
    policy: str
    access_key_id: str
    access_key_secret: str

    def __repr__(self) -> str:  # 防止误打日志把 secret 带出去
        return f"LongTermCredential(user={self.user!r}, ak=…{self.access_key_id[-4:]}, sk=<hidden>)"


class AliyunExecutor:
    platform = "aliyun"

    def __init__(self, account: str, creds: aliyun.Credentials, *, transport=None):
        self.account = account
        self._creds = creds
        self._transport = transport
        self._checked = False

    @classmethod
    def from_env(cls, account: str, environ=None, *, issuer: bool = False) -> AliyunExecutor:
        env = os.environ if environ is None else environ
        prefix = (issuer_env_prefix if issuer else exec_env_prefix)("aliyun", account)
        ak = env.get(f"{prefix}_ACCESS_KEY_ID", "")
        sk = env.get(f"{prefix}_ACCESS_KEY_SECRET", "")
        if not ak or not sk:
            what = "凭证发放身份" if issuer else "执行身份"
            raise ProvisionError(f"没有配置阿里云 {account} 的{what}（{prefix}_ACCESS_KEY_ID）")
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

    def has_policy(self, user: str, policy_type: str, policy: str) -> bool:
        """账号级直接授权（不含经用户组继承的）。"""
        self._check_account()
        body = self._call(aliyun.RAM, "ListPoliciesForUser", {"UserName": user})
        node = body.get("Policies")
        if not isinstance(node, dict):
            raise ProvisionError("ListPoliciesForUser 返回缺 Policies，不能当作没有授权")
        return any(
            str(p.get("PolicyName")) == policy and str(p.get("PolicyType")) == policy_type
            for p in node.get("Policy") or []
        )

    def attach_policy(self, user: str, policy_type: str, policy: str) -> None:
        self._check_account()
        if not self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 不存在")
        try:
            self._call(
                aliyun.RAM, "AttachPolicyToUser", _aliyun_policy_params(user, policy_type, policy)
            )
        except aliyun.AliyunError as exc:
            if exc.code == "LimitExceeded.User.Policy":
                raise ProvisionError(
                    f"子账号 {user} 直接授予的策略已达上限（阿里云每个子账号最多 20 条系统策略、"
                    "10 条自定义策略），请先撤销不用的策略或改用用户组"
                ) from None
            if exc.code != "EntityAlreadyExists.User.Policy":
                raise

    def detach_policy(self, user: str, policy_type: str, policy: str) -> None:
        self._check_account()
        try:
            self._call(
                aliyun.RAM, "DetachPolicyFromUser", _aliyun_policy_params(user, policy_type, policy)
            )
        except aliyun.AliyunError as exc:
            # 只吞「本来就没授予」「子账号已删」和「自定义策略已删」（阿里云不允许删除仍在授权中的
            # 策略，删掉了说明已经不在这个人身上）：系统策略名写错之类照样报错
            gone = exc.code == "EntityNotExist.Policy" and policy_type == "Custom"
            if exc.code not in ("EntityNotExist.User.Policy", "EntityNotExist.User") and not gone:
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

    # ── 长期凭证：建号 + 时间窗策略 + 长期 AK；到期删干净 ──────────────────
    #
    # 这些方法**只能用凭证发放身份（panel-issuer）调**，别用开通身份（panel-executor）：
    # 云上那两把 AK 的策略是分开收窄的，executor 明确禁止建号/发 AK/造策略，
    # 就是为了面板逻辑出漏洞时炸不到真人账号。

    def issue_long_term(self, user: str, display_name: str, policy_doc: dict) -> LongTermCredential:
        """建子账号 → 造带时间窗的自定义策略 → 挂上 → 发一对长期 AK。

        顺序不能反：**AK 必须最后发**。先发 AK 再挂策略的话，中间那一刻存在一把
        什么都能干不了、但已经交付出去的凭证；更糟的是挂策略失败时 AK 已经存在，
        清理不及时就是一把裸奔的长期密钥。
        """
        self._check_account()
        policy = grants.policy_name(user)
        self.create_user(user, display_name)
        self._call(
            aliyun.RAM,
            "CreatePolicy",
            {
                "PolicyName": policy,
                "PolicyDocument": json.dumps(policy_doc, separators=(",", ":")),
                "Description": f"面板长期数据访问凭证 {user}",
            },
        )
        self._call(
            aliyun.RAM,
            "AttachPolicyToUser",
            {"UserName": user, "PolicyName": policy, "PolicyType": "Custom"},
        )
        body = self._call(aliyun.RAM, "CreateAccessKey", {"UserName": user})
        ak = body.get("AccessKey") or {}
        try:
            return LongTermCredential(user, policy, ak["AccessKeyId"], ak["AccessKeySecret"])
        except KeyError:
            raise ProvisionError("CreateAccessKey 返回缺少凭证字段") from None

    def revoke_long_term(self, user: str) -> list:
        """到期清理：删 AK → 摘策略 → 删策略 → 删用户。返回没删掉的东西（供告警）。

        顺序同样不能反：阿里云不允许删除仍挂在用户身上的策略，也不允许删除还有 AK 的用户。
        每一步单独 try：**一处失败不能中断后面的**，否则一个已经手动删掉的策略会让
        用户和 AK 永远留在云上——那正是我们要清理的东西。
        """
        self._check_account()
        policy = grants.policy_name(user)
        left = []

        def step(label, fn):
            try:
                fn()
            except aliyun.AliyunError as exc:
                # 已经不存在 = 目标达成，不算失败
                if "NotExist" not in exc.code:
                    left.append(f"{label}：{exc.code}")

        body = {}
        try:
            body = self._call(aliyun.RAM, "ListAccessKeys", {"UserName": user})
        except aliyun.AliyunError as exc:
            if "NotExist" not in exc.code:
                left.append(f"列 AccessKey：{exc.code}")
        for key in (body.get("AccessKeys") or {}).get("AccessKey") or []:
            kid = key.get("AccessKeyId")
            step(
                f"删 AccessKey {str(kid)[-4:]}",
                lambda kid=kid: self._call(
                    aliyun.RAM, "DeleteAccessKey", {"UserName": user, "UserAccessKeyId": kid}
                ),
            )
        step(
            "摘策略",
            lambda: self._call(
                aliyun.RAM,
                "DetachPolicyFromUser",
                {"UserName": user, "PolicyName": policy, "PolicyType": "Custom"},
            ),
        )
        step(
            "删策略",
            lambda: self._call(
                aliyun.RAM, "DeletePolicy", {"PolicyName": policy, "PolicyType": "Custom"}
            ),
        )
        step("删用户", lambda: self._call(aliyun.RAM, "DeleteUser", {"UserName": user}))
        return left

    def assume_role(
        self, role_arn: str, name: str, hours: int, *, policy: Optional[dict] = None
    ) -> TempCredential:
        """扮演角色换一组临时凭证。

        `policy` 是**会话策略**：最终权限是「角色策略 ∩ 会话策略」，只会更小不会更大。
        凭证申请都带它——角色本身覆盖十来个桶，而一张单子只批了一个桶下的一个目录，
        不收窄就等于把角色的全部范围发出去了。
        """
        self._check_account()
        params = {
            "RoleArn": role_arn,
            "RoleSessionName": session_name(name),
            "DurationSeconds": str(hours * 3600),
        }
        if policy is not None:
            params["Policy"] = grants.session_policy(policy)
        body = self._call(aliyun.STS, "AssumeRole", params)
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
    def from_env(cls, account: str, environ=None, *, issuer: bool = False) -> VolcanoExecutor:
        env = os.environ if environ is None else environ
        prefix = (issuer_env_prefix if issuer else exec_env_prefix)("volcano", account)
        ak = env.get(f"{prefix}_ACCESS_KEY", "")
        sk = env.get(f"{prefix}_SECRET_KEY", "")
        if not ak or not sk:
            what = "凭证发放身份" if issuer else "执行身份"
            raise ProvisionError(f"没有配置火山 {account} 的{what}（{prefix}_ACCESS_KEY）")
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

    def has_policy(self, user: str, policy_type: str, policy: str) -> bool:
        """全局范围的直接授权（不含经用户组继承的、只在某个项目里生效的）。"""
        self._check_account()
        body = self._call(volcano.IAM, "ListAttachedUserPolicies", {"UserName": user})
        items = body.get("AttachedPolicyMetadata")
        if items is None:
            raise ProvisionError(
                "ListAttachedUserPolicies 返回缺 AttachedPolicyMetadata，不能当作没有授权"
            )
        for item in items:
            if str(item.get("PolicyName")) != policy or str(item.get("PolicyType")) != policy_type:
                continue
            scopes = item.get("PolicyScope") or [{"PolicyScopeType": "Global"}]
            if any(sc.get("PolicyScopeType", "Global") == "Global" for sc in scopes):
                return True
        return False

    def attach_policy(self, user: str, policy_type: str, policy: str) -> None:
        if not self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 不存在")
        try:
            self._call(
                volcano.IAM,
                "AttachUserPolicy",
                {"UserName": user, "PolicyName": policy, "PolicyType": policy_type},
            )
        except volcano.VolcanoError as exc:
            code = _volcano_code(exc)
            # PolicyAttachConflict：已经授予过，按成功处理
            if not any(m in code for m in ("attachconflict", "alreadyattach", "alreadyexist")):
                raise

    def detach_policy(self, user: str, policy_type: str, policy: str) -> None:
        self._check_account()
        # 先查是否授予，而不是吞「不存在」类错误：策略名写错时要报错，不能当作已回收
        try:
            if not self.has_policy(user, policy_type, policy):
                return
        except volcano.VolcanoError as exc:
            if _volcano_user_missing(exc):
                return
            raise
        try:
            self._call(
                volcano.IAM,
                "DetachUserPolicy",
                {"UserName": user, "PolicyName": policy, "PolicyType": policy_type},
            )
        except volcano.VolcanoError as exc:
            # PolicyDetachConflict：查询和撤销之间已经被撤销，按成功处理
            if "detachconflict" not in _volcano_code(exc):
                raise

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

    # ── 长期凭证：建号 + 时间窗策略 + 长期 AK；到期删干净 ──────────────────
    #
    # 与阿里那边同一套顺序和同一套理由（见 AliyunExecutor.issue_long_term），
    # 只有三处火山方言：策略文档不带 Version、DeletePolicy 不收 PolicyType、
    # 建 AK 必须显式传 UserName（不传会给**调用者自己**建一把 AK —— 那是主控 AK）。

    def issue_long_term(self, user: str, display_name: str, policy_doc: dict) -> LongTermCredential:
        """建子账号 → 造带时间窗的自定义策略 → 挂上 → 发一对长期 AK。**AK 必须最后发。**"""
        self._check_account()
        policy = grants.policy_name(user)
        self.create_user(user, display_name)
        self._call(
            volcano.IAM,
            "CreatePolicy",
            {
                "PolicyName": policy,
                "PolicyDocument": json.dumps(policy_doc, separators=(",", ":")),
                "Description": f"面板长期数据访问凭证 {user}"[:128],
            },
        )
        self._call(
            volcano.IAM,
            "AttachUserPolicy",
            {"UserName": user, "PolicyName": policy, "PolicyType": "Custom"},
        )
        # UserName 不是可选的：火山文档里它标「否」，但不传就是给调用者自己建 AK。
        # 调用者是发放身份，那把 AK 能建号能发 AK —— 会把一把主控级密钥当成凭证发出去
        body = self._call(volcano.IAM, "CreateAccessKey", {"UserName": user})
        ak = body.get("AccessKey") or {}
        try:
            return LongTermCredential(user, policy, ak["AccessKeyId"], ak["SecretAccessKey"])
        except KeyError:
            raise ProvisionError("CreateAccessKey 返回缺少凭证字段") from None

    def revoke_long_term(self, user: str) -> list:
        """到期清理：删 AK → 摘策略 → 删策略 → 删用户。返回没删掉的东西（供告警）。

        每一步单独 try：一处失败不能中断后面的，否则一个已经手动删掉的策略会让
        用户和 AK 永远留在云上 —— 那正是我们要清理的东西。
        """
        self._check_account()
        policy = grants.policy_name(user)
        left = []

        def step(label, fn):
            try:
                fn()
            except volcano.VolcanoError as exc:
                if "notexist" not in _volcano_code(exc):
                    left.append(f"{label}：{exc}")

        body = {}
        try:
            body = self._call(volcano.IAM, "ListAccessKeys", {"UserName": user})
        except volcano.VolcanoError as exc:
            if "notexist" not in _volcano_code(exc):
                left.append(f"列 AccessKey：{exc}")
        for key in body.get("AccessKeyMetadata") or []:
            kid = key.get("AccessKeyId")
            if not kid:
                continue
            step(
                f"删 AccessKey {str(kid)[-4:]}",
                lambda kid=kid: self._call(
                    volcano.IAM, "DeleteAccessKey", {"UserName": user, "AccessKeyId": kid}
                ),
            )
        step(
            "摘策略",
            lambda: self._call(
                volcano.IAM,
                "DetachUserPolicy",
                {"UserName": user, "PolicyName": policy, "PolicyType": "Custom"},
            ),
        )
        # 火山的 DeletePolicy 只收 PolicyName，没有 PolicyType（阿里要）
        step("删策略", lambda: self._call(volcano.IAM, "DeletePolicy", {"PolicyName": policy}))
        step("删用户", lambda: self._call(volcano.IAM, "DeleteUser", {"UserName": user}))
        return left

    def assume_role(
        self, role_trn: str, name: str, hours: int, *, policy: Optional[dict] = None
    ) -> TempCredential:
        """火山 STS。**不接受会话策略**——火山的 AssumeRole 到底认不认 Policy 参数没有取证过，
        静默忽略它就等于把整个角色的范围发出去，所以宁可在这里报错。
        需要按桶按目录收窄的火山凭证一律走 issue_long_term（策略里写死时间窗和范围）。
        """
        if policy is not None:
            raise ProvisionError("火山 STS 的会话策略还没有验证过，这类申请请走长期凭证")
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


def _volcano_code(exc: Exception) -> str:
    """火山错误码，小写并去掉点和下划线，便于匹配。"""
    match = re.search(r"HTTP \d+：(\S+)", str(exc))
    return (match.group(1) if match else "").lower().replace(".", "").replace("_", "")


def _volcano_user_missing(exc: Exception) -> bool:
    """只认「子账号不存在」：用户组不存在、策略不存在之类的错误照样抛出。"""
    code = _volcano_code(exc)
    return "notexist" in code and "user" in code and "group" not in code and "policy" not in code


def _aliyun_policy_params(user: str, policy_type: str, policy: str) -> dict:
    return {"UserName": user, "PolicyType": policy_type, "PolicyName": policy}


Factory = Callable[[str, str], object]


def executor_from_env(platform: str, account: str, *, issuer: bool = False) -> object:
    """开通身份（默认）或凭证发放身份（issuer=True）。两把 AK 的权限在云上是分开收窄的。"""
    if platform == "aliyun":
        return AliyunExecutor.from_env(account, issuer=issuer)
    if platform == "volcano":
        return VolcanoExecutor.from_env(account, issuer=issuer)
    raise ProvisionError(f"不支持的平台 {platform}")


def describe_error(exc: Exception) -> Optional[str]:
    """执行错误写进申请单事件：只取第一行，并去掉凭证回显。"""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    first = lines[0] if lines else type(exc).__name__
    return aliyun._scrub(volcano._scrub(first))[:300]
