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
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

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


class PartialDisable(ProvisionError):
    """停用做了一半：登录关了（或禁了几把 AK）之后出错。`partial` 是已经做成的部分 ——
    不记下来的话，下一轮看到登录已经关着会记成「没关过」，恢复时登录就回不来了。"""

    def __init__(self, message: str, partial: dict):
        super().__init__(message)
        self.partial = partial


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

    def run_instance(self, *, region: str, params: dict, name: str) -> str:
        """开一台 ECS，返回实例 ID。

        **`Amount` 恒为 1，而且写死在这里、不从 params 取。** 模板里任何一个轴
        都不该能决定开几台 —— 一个配错的数字轴就是一次「批了一台、开出二十台」。

        `ClientToken` 用申请单号：`RunInstances` 认它做幂等，所以
        **重试同一张单不会开出第二台**。这是这段里最重要的一行：
        没有它的话，超时重试 = 多一台机器在那儿跑着计费，而台账上只有一条记录。
        """
        from .clouds import aliyun

        if not region:
            raise ProvisionError("模板没写地域，不知道在哪开")
        # 模板里的 params 是内网拓扑（交换机、安全组、镜像），原样透传；
        # 下面这几个由面板决定，**放在后面覆盖**，不让模板改
        query = dict(params or {})
        query.update(
            RegionId=region,
            Amount="1",
            ClientToken=name[:64],
            InstanceName=name[:128],
            Description=f"cloud-panel {name}"[:256],
        )
        got = aliyun.call(
            *aliyun.ecs(region),
            "RunInstances",
            query,
            creds=self._creds,
            transport=self._transport,
        )
        ids = (got.get("InstanceIdSets") or {}).get("InstanceIdSet") or []
        if not isinstance(ids, list) or not ids or not str(ids[0] or ""):
            # **没拿到实例 ID 就当失败**：机器可能已经在开了，但台账里记不下它是哪台 ——
            # 那比不开更糟，因为没人知道去哪找它。ClientToken 保证重试拿到的是同一台
            raise ProvisionError(f"RunInstances 没有返回实例 ID，不确认开成没有：{str(got)[:200]}")
        return str(ids[0])

    def make_dir(self, bucket: str, prefix: str, region: str) -> str:
        """在 OSS 上建一个目录（放一个 0 字节的占位对象）。返回建好的完整路径。

        **OSS 没有真目录**，前缀是虚的。这个占位对象是为了让人在控制台里看得见结构 ——
        以及让面板能回答「这个目录建好了没有」，否则「申请通过了」和「东西真的在那儿」
        之间没有任何可验证的东西。

        幂等：同一个 key PUT 两次就是覆盖一个空对象，重试安全。
        """
        from .clouds import oss

        key = prefix if prefix.endswith("/") else prefix + "/"
        oss.put_folder(bucket, key, region=region, creds=self._creds, transport=self._transport)
        return f"{bucket}/{key}"

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

    def attached(self, user: str) -> tuple:
        """这个子账号**此刻**挂着什么：`([{PolicyName,PolicyType}, …], [用户组名, …])`。

        收权必须读实时的，不能读快照：快照可能是几小时前的，照着它去撤，
        有可能撤掉一条五分钟前刚通过审批发下去的策略。

        **只返回账号级授权。** 资源组级的（控制台里显示成 `策略名 @资源组:rg-xxx`）
        走的是 ResourceManager 的接口，`ram:DetachPolicyFromUser` 撤不掉它，
        所以这里也不列出来 —— 列了却撤不掉，比不列更误导。
        """
        self._check_account()
        # `{"Policies": null}` 和缺键都**不能当作没有授权**：静默返回空会让页面显示
        # 「这个人什么都没挂」，而他可能挂着超管。与火山那侧、与 has_policy 一致
        raw = self._call(aliyun.RAM, "ListPoliciesForUser", {"UserName": user}).get("Policies")
        if not isinstance(raw, dict) or raw.get("Policy") is None:
            raise ProvisionError("ListPoliciesForUser 返回缺 Policies，不能当作没有授权")
        pols = [
            {
                "PolicyName": str(x.get("PolicyName") or ""),
                "PolicyType": str(x.get("PolicyType") or ""),
            }
            for x in raw["Policy"]
        ]
        # 用户组那半边同理：组读空 → 界面一个组都不显示、remaining 里也没有「用户组 X」→
        # 管理员撤完看到「什么都不剩」，而这人还从组里继承着一堆权限
        raw_g = self._call(aliyun.RAM, "ListGroupsForUser", {"UserName": user}).get("Groups")
        if not isinstance(raw_g, dict) or raw_g.get("Group") is None:
            raise ProvisionError("ListGroupsForUser 返回缺 Groups，不能当作不在任何组")
        groups = [str(g.get("GroupName") or "") for g in raw_g["Group"]]
        return pols, [g for g in groups if g]

    def has_deny(self, policy_type: str, policy: str) -> Optional[bool]:
        """这条策略里有没有 `"Effect": "Deny"`。**读不出来返回 None，不是 False。**

        撤掉一条含 Deny 的策略 = 提权（RAM 里 Deny 优先且跨策略生效）。所以收权
        之前要问这一句；而「读不出来」绝不能当成「没有 Deny」—— 那正好是最危险的
        默认值：一条读不了的策略会被当成普通 Allow 放行掉。
        """
        self._check_account()
        try:
            got = self._call(
                aliyun.RAM, "GetPolicy", {"PolicyName": policy, "PolicyType": policy_type}
            )
            doc = json.loads(
                urllib.parse.unquote(str(got["DefaultPolicyVersion"]["PolicyDocument"]))
            )
        except (aliyun.AliyunError, KeyError, ValueError, TypeError):
            return None
        stmts = doc.get("Statement")
        if not isinstance(stmts, list):
            return None
        return any(str(st.get("Effect") or "").lower() == "deny" for st in stmts)

    def create_user(
        self, user: str, display_name: str, *, email: str = "", phone: str = ""
    ) -> None:
        """建 RAM 子用户。**安全邮箱和安全手机一并写上。**

        不写的话云控制台上这两栏是空的：找回密码、风险操作验证、安全提醒都没有落点，
        而补写要管理员一个个去控制台点 —— 建号那一刻手上就有这些信息，那时写最便宜。

        手机号阿里云要 `国家代码-号码` 的形式（`86-138…`）；给了裸号码就补上 `86-`。
        """
        self._check_account()
        if self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 已存在，不会接管已有账号，请换一个用户名")
        params = {"UserName": user, "DisplayName": display_name[:24]}
        if email:
            params["Email"] = email[:128]
        if phone:
            params["MobilePhone"] = phone if "-" in phone else f"86-{phone}"
        self._call(aliyun.RAM, "CreateUser", params)

    def user_id(self, user: str) -> str:
        """子账号的 UserId。PAI 的成员接口只认它，不认登录名。"""
        self._check_account()
        got = self._call(aliyun.RAM, "GetUser", {"UserName": user}).get("User") or {}
        uid = str(got.get("UserId") or "")
        if not uid:
            raise ProvisionError(f"RAM 没返回 {user} 的 UserId，加不进工作空间")
        return uid

    def add_workspace_member(
        self, *, region: str, workspace: str, user: str, roles: Sequence[str]
    ) -> str:
        """把子账号加进 PAI 工作空间，返回 MemberId。

        **这一步不做等于新人开完号还是进不去 DSW/DLC** —— 工作空间是 PAI 的围墙，
        数据集、DSW 实例、DLC 任务全是它的下级资源，不在里面的人一个都看不到。
        原先面板只会「列成员」，加人得管理员去控制台点，每个新人都要手动做一次。

        角色照现网的写法给（`PAI.AlgoDeveloper` 这种）。**不提供移除** ——
        移出成员是收权限、可以人工做，而误移的表现是那个人突然进不去自己的空间、
        还查不出为什么。
        """
        uid = self.user_id(user)
        body = {"UserId": uid, "Roles": list(roles)}
        got = aliyun.call_roa(
            f"aiworkspace.{region}.aliyuncs.com",
            "2021-02-04",
            f"/api/v1/workspaces/{workspace}/members",
            method="POST",
            body={"Members": [body]},
            creds=self._creds,
            transport=self._transport,
        )
        rows = got.get("Members") or []
        return str((rows[0] if rows else {}).get("MemberId") or "")

    def create_dataset(
        self,
        *,
        region: str,
        workspace: str,
        name: str,
        uri: str,
        source: str,
        user: str,
        labels=None,
    ) -> str:
        """在工作空间里登记一条数据集，返回 DatasetId。

        **只登记指针，不建底层目录**：OSS 的前缀是虚的、CPFS 在挂载时自动建。

        放在执行器上而不是让 flows 直接调 `assets.create_dataset` —— 后者要伸手拿
        `ex._creds`，而那是私有属性：调用方一旦绕过执行器，`_check_account`
        那道「这把 AK 真属于目标云账号吗」的门就被跳过了。
        """
        from . import assets as assets_mod

        self._check_account()
        try:
            return assets_mod.create_dataset(
                self._creds,
                region=region,
                workspace=workspace,
                name=name,
                uri=uri,
                source=source,
                user_id=self.user_id(user),
                labels=labels,
                transport=self._transport,
            )
        except Exception as exc:  # noqa: BLE001 — 只吞「已经有了」，见下
            # **重名不是失败。** PAI 这个接口对同名数据集回的是 HTTP 400
            # `201300003 Dataset name already existed`，而不是一个「已存在」的成功。
            # 不吞的话，重试一张单（或者给已有账号补齐）会把它记成问题，
            # 而 `_provision_workspace` 的 problems 一非空就返回「**他还进不去 DSW/DLC**」——
            # 人看到这句会去查权限，实际上那条数据集一直好好地在那儿。
            if "already existed" not in str(exc) and "已存在" not in str(exc):
                raise
            return ""

    def enable_console(self, user: str) -> bool:
        """开控制台登录。**已经开着就什么都不做**，返回「这次有没有真开」。

        为什么要单独有这个
        ──────────────────
        原先只有 `reset_password` 会建登录配置，而它只在「本人领初始密码」那一刻被调。
        企业 SSO 开了之后领密码是死路（密码登录全局失效），于是面板建的号
        **永远没有登录配置** —— 人拿着企业账号也进不去。

        **幂等很重要**：重试同一张单不能走到 `UpdateLoginProfile`，
        那会把人家自己设过的密码重置掉。
        """
        self._check_account()
        try:
            self._call(aliyun.RAM, "GetLoginProfile", {"UserName": user})
            return False
        except aliyun.AliyunError as exc:
            if "LoginProfile" not in str(exc.code or ""):
                raise
        # 密码是随机的、**不返回给任何人**：SSO 模式下它用不上，
        # 这里要的只是「这个号允许登控制台」这个状态
        self._call(
            aliyun.RAM,
            "CreateLoginProfile",
            {"UserName": user, "Password": new_password(), "PasswordResetRequired": "true"},
        )
        return True

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

    # ── 离职：停用 → 管理员确认 → 删号。**只动账号，不动任何数据** ──────────
    #
    # 停用 = 关控制台登录 + 把 AK 设成 Inactive，两样都能恢复。删号要管理员在面板上确认。
    # 谁能被停由 offboard 模块决定（只停名册里确认归属某个人的号）；云上的策略另有一层
    # Deny 护住服务号和面板自己的三个身份。

    def disable_user(self, user: str) -> dict:
        """关登录、禁 AK。返回 `{"login": 这次关没关, "keys": [这次禁掉的 AK]}`，恢复时照着开回去。

        阿里云没有「禁止登录」开关，关登录就是删登录配置。恢复时重建一份（随机密码，
        SSO 下用不上），见 `enable_console`。
        """
        self._check_account()
        closed = False
        try:
            self._call(aliyun.RAM, "GetLoginProfile", {"UserName": user})
            self._call(aliyun.RAM, "DeleteLoginProfile", {"UserName": user})
            closed = True
        except aliyun.AliyunError as exc:
            if exc.code == "EntityNotExist.User":
                return {"login": False, "keys": [], "gone": True}
            if "LoginProfile" not in str(exc.code or ""):
                raise
        keys = []
        try:
            body = self._call(aliyun.RAM, "ListAccessKeys", {"UserName": user})
            for k in (body.get("AccessKeys") or {}).get("AccessKey") or []:
                if str(k.get("Status") or "") == "Active":
                    kid = str(k.get("AccessKeyId") or "")
                    self._call(
                        aliyun.RAM,
                        "UpdateAccessKey",
                        {"UserName": user, "UserAccessKeyId": kid, "Status": "Inactive"},
                    )
                    keys.append(kid)
        except Exception as exc:
            if closed or keys:
                raise PartialDisable(f"停用没做完：{exc}", {"login": closed, "keys": keys}) from exc
            raise
        return {"login": closed, "keys": keys}

    def enable_user(self, user: str, *, login: bool, keys) -> None:
        """撤销停用：把 `disable_user` 这次关掉的东西开回去，别的不碰。"""
        self._check_account()
        for kid in keys or ():
            try:
                self._call(
                    aliyun.RAM,
                    "UpdateAccessKey",
                    {"UserName": user, "UserAccessKeyId": kid, "Status": "Active"},
                )
            except aliyun.AliyunError as exc:
                # 删号删到一半 AK 已经没了：开不回来，也不该卡住其余的
                if "NotExist" not in str(exc.code or ""):
                    raise
        if login:
            self.enable_console(user)

    def delete_user(self, user: str) -> list:
        """删号：删 AK → 出组 → 摘策略 → 解 MFA → 删登录配置 → 删用户。返回没删掉的东西。

        阿里云不许删还挂着 AK / 组 / 策略 / MFA 的用户，所以要先清干净。
        **只动账号本身**：他在桶里的文件、建的数据集、实例一样不碰。
        """
        self._check_account()
        left = []

        def step(label, fn):
            try:
                fn()
            except aliyun.AliyunError as exc:
                if "NotExist" not in str(exc.code or ""):
                    left.append(f"{label}：{exc.code}")

        try:
            body = self._call(aliyun.RAM, "ListAccessKeys", {"UserName": user})
        except aliyun.AliyunError as exc:
            if exc.code == "EntityNotExist.User":
                return []  # 已经没了（比如有人在控制台删过），目标达成
            raise
        for k in (body.get("AccessKeys") or {}).get("AccessKey") or []:
            kid = str(k.get("AccessKeyId") or "")
            step(
                f"删 AccessKey …{kid[-4:]}",
                lambda kid=kid: self._call(
                    aliyun.RAM, "DeleteAccessKey", {"UserName": user, "UserAccessKeyId": kid}
                ),
            )
        pols, groups = self.attached(user)
        for g in groups:
            step(
                f"移出用户组 {g}",
                lambda g=g: self._call(
                    aliyun.RAM, "RemoveUserFromGroup", {"UserName": user, "GroupName": g}
                ),
            )
        for pol in pols:
            step(
                f"摘策略 {pol['PolicyName']}",
                lambda pol=pol: self._call(
                    aliyun.RAM,
                    "DetachPolicyFromUser",
                    _aliyun_policy_params(user, pol["PolicyType"], pol["PolicyName"]),
                ),
            )
        step("解绑 MFA", lambda: self._call(aliyun.RAM, "UnbindMFADevice", {"UserName": user}))
        step(
            "删登录配置",
            lambda: self._call(aliyun.RAM, "DeleteLoginProfile", {"UserName": user}),
        )
        if not left:
            step("删用户", lambda: self._call(aliyun.RAM, "DeleteUser", {"UserName": user}))
        return left

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

    def attached(self, user: str) -> tuple:
        """这个子账号**此刻**挂着什么：`([{PolicyName,PolicyType}, …], [用户组名, …])`。

        语义与阿里云那一侧一致（见那边的说明）：读实时的，只算全局范围的直接授权，
        项目范围的不在内 —— `DetachUserPolicy` 撤不掉那一类。
        """
        self._check_account()
        body = self._call(volcano.IAM, "ListAttachedUserPolicies", {"UserName": user})
        items = body.get("AttachedPolicyMetadata")
        if items is None:
            raise ProvisionError(
                "ListAttachedUserPolicies 返回缺 AttachedPolicyMetadata，不能当作没有授权"
            )
        # **只留 Global 范围**：项目范围的 DetachUserPolicy 撤不掉，而 detach_policy
        # 撤前先 has_policy（它也只认 Global）→ 查不到就直接 return → 面板记进 done、
        # 界面显示「已撤掉」，云上一动没动。列了却撤不掉，比不列更误导
        pols = []
        for x in items:
            scopes = x.get("PolicyScope") or [{"PolicyScopeType": "Global"}]
            if not any(sc.get("PolicyScopeType", "Global") == "Global" for sc in scopes):
                continue
            pols.append(
                {
                    "PolicyName": str(x.get("PolicyName") or ""),
                    "PolicyType": str(x.get("PolicyType") or ""),
                }
            )
        groups, page = [], 0
        while page < 50:
            got = (
                self._call(
                    volcano.IAM,
                    "ListGroupsForUser",
                    {"UserName": user, "Limit": "100", "Offset": str(page * 100)},
                ).get("UserGroupMetadata")
                or []
            )
            groups += [str(g.get("UserGroupName") or "") for g in got]
            if len(got) < 100:
                return pols, [g for g in groups if g]
            page += 1
        raise ProvisionError("火山 ListGroupsForUser 翻页超过上限，已中断")

    def has_deny(self, policy_type: str, policy: str) -> Optional[bool]:
        """这条策略里有没有 `"Effect": "Deny"`。**读不出来返回 None，不是 False。**

        语义与阿里那侧逐字一致（见那边的说明）。**缺了这个方法比返回错的更糟**：
        收权那条路会对每条自定义策略调它，没有就直接 AttributeError 崩在半路 ——
        而那时候前面几条可能已经撤掉了。
        """
        self._check_account()
        try:
            got = self._call(
                volcano.IAM, "GetPolicy", {"PolicyName": policy, "PolicyType": policy_type}
            )
            doc = json.loads(str((got.get("Policy") or {}).get("PolicyDocument") or ""))
        except (volcano.VolcanoError, json.JSONDecodeError, ValueError, TypeError):
            return None
        stmts = doc.get("Statement")
        if not isinstance(stmts, list):
            return None
        return any(str(st.get("Effect") or "").lower() == "deny" for st in stmts)

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

    def create_user(
        self, user: str, display_name: str, *, email: str = "", phone: str = ""
    ) -> None:
        """建 IAM 子用户。安全邮箱和安全手机一并写上，理由同阿里那边。

        火山的手机号字段是 `MobilePhone`，格式要求和阿里一致（`86-138…`）。
        """
        if self.user_exists(user):
            raise ProvisionError(f"子账号 {user} 已存在，不会接管已有账号，请换一个用户名")
        params = {"UserName": user, "DisplayName": display_name[:64]}
        if email:
            params["Email"] = email[:128]
        if phone:
            params["MobilePhone"] = phone if "-" in phone else f"86-{phone}"
        self._call(volcano.IAM, "CreateUser", params)

    def enable_console(self, user: str) -> bool:
        """开控制台登录。理由同阿里那个，但火山多一个显式的 `LoginAllowed` 开关 ——
        不开的话连 SSO 都进不去（阿里那边没有这个字段）。

        **幂等**：已经有登录配置就不碰，免得把人家自己设的密码重置掉。
        """
        self._check_account()
        try:
            got = self._call(volcano.IAM, "GetLoginProfile", {"UserName": user})
            # 火山对没有登录配置的用户返回全零 stub 而不是 NotExist（bot 那边记过这个坑），
            # 所以不能只看「有没有抛异常」，要看 LoginAllowed 到底是不是真的
            profile = got.get("LoginProfile") or got
            if str(profile.get("LoginAllowed", "")).lower() in ("true", "1"):
                return False
        except volcano.VolcanoError as exc:
            if "notexist" not in str(exc).lower().replace(".", ""):
                raise
        params = {
            "UserName": user,
            "Password": new_password(),
            "LoginAllowed": "true",
            "PasswordResetRequired": "true",
        }
        try:
            self._call(volcano.IAM, "CreateLoginProfile", params)
        except volcano.VolcanoError as exc:
            if "alreadyexist" not in str(exc).lower().replace(".", ""):
                raise
            self._call(volcano.IAM, "UpdateLoginProfile", params)
        return True

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

    # ── 离职：停用 → 管理员确认 → 删号。**只动账号，不动任何数据** ──────────
    # 与阿里那边同一套（见 AliyunExecutor.disable_user）。火山有显式的 LoginAllowed 开关，
    # 关登录不用删登录配置，恢复也不用重设密码。

    def disable_user(self, user: str) -> dict:
        self._check_account()
        closed = False
        try:
            got = self._call(volcano.IAM, "GetLoginProfile", {"UserName": user})
            profile = got.get("LoginProfile") or got
            # 没有登录配置时火山回全零 stub，LoginAllowed 是 false —— 那就没什么可关的
            if str(profile.get("LoginAllowed", "")).lower() in ("true", "1"):
                self._call(
                    volcano.IAM,
                    "UpdateLoginProfile",
                    {"UserName": user, "LoginAllowed": "false"},
                )
                closed = True
        except volcano.VolcanoError as exc:
            if _volcano_user_missing(exc):
                # **再查一次 GetUser 确认**：错误码来自 GetLoginProfile，
                # 判错的话，一个还活着、AK 还开着的离职号会被记成「云上已不存在」而不再处理
                if not self.user_exists(user):
                    return {"login": False, "keys": [], "gone": True}
            elif "notexist" not in _volcano_code(exc):
                raise
        keys = []
        try:
            body = self._call(volcano.IAM, "ListAccessKeys", {"UserName": user})
            for k in body.get("AccessKeyMetadata") or []:
                if str(k.get("Status") or "").lower() == "active":
                    kid = str(k.get("AccessKeyId") or "")
                    self._call(
                        volcano.IAM,
                        "UpdateAccessKey",
                        {"UserName": user, "AccessKeyId": kid, "Status": "inactive"},
                    )
                    keys.append(kid)
        except Exception as exc:
            if closed or keys:
                raise PartialDisable(f"停用没做完：{exc}", {"login": closed, "keys": keys}) from exc
            raise
        return {"login": closed, "keys": keys}

    def enable_user(self, user: str, *, login: bool, keys) -> None:
        self._check_account()
        for kid in keys or ():
            try:
                self._call(
                    volcano.IAM,
                    "UpdateAccessKey",
                    {"UserName": user, "AccessKeyId": kid, "Status": "active"},
                )
            except volcano.VolcanoError as exc:
                if "notexist" not in _volcano_code(exc):
                    raise
        if login:
            self._call(
                volcano.IAM, "UpdateLoginProfile", {"UserName": user, "LoginAllowed": "true"}
            )

    def delete_user(self, user: str) -> list:
        """删 AK → 出组 → 摘策略 → 删登录配置 → 删用户。返回没删掉的东西。**不碰数据。**"""
        self._check_account()
        left = []

        def step(label, fn):
            try:
                fn()
            except volcano.VolcanoError as exc:
                if "notexist" not in _volcano_code(exc):
                    left.append(f"{label}：{exc}")

        try:
            body = self._call(volcano.IAM, "ListAccessKeys", {"UserName": user})
        except volcano.VolcanoError as exc:
            if _volcano_user_missing(exc):
                return []  # 已经没了，目标达成
            raise
        for k in body.get("AccessKeyMetadata") or []:
            kid = str(k.get("AccessKeyId") or "")
            if not kid:
                continue
            if str(k.get("Status") or "").lower() == "active":
                # 火山不许删启用中的 AK（AccessKeyCanNotDelete），先禁再删。
                # 没被自动停过的号（弱信号、管理员直接确认的）AK 都还开着
                step(
                    f"禁用 AccessKey …{kid[-4:]}",
                    lambda kid=kid: self._call(
                        volcano.IAM,
                        "UpdateAccessKey",
                        {"UserName": user, "AccessKeyId": kid, "Status": "inactive"},
                    ),
                )
            step(
                f"删 AccessKey …{kid[-4:]}",
                lambda kid=kid: self._call(
                    volcano.IAM, "DeleteAccessKey", {"UserName": user, "AccessKeyId": kid}
                ),
            )
        pols, groups = self.attached(user)
        for g in groups:
            step(
                f"移出用户组 {g}",
                lambda g=g: self._call(
                    volcano.IAM, "RemoveUserFromGroup", {"UserName": user, "UserGroupName": g}
                ),
            )
        for pol in pols:
            step(
                f"摘策略 {pol['PolicyName']}",
                lambda pol=pol: self._call(
                    volcano.IAM,
                    "DetachUserPolicy",
                    {
                        "UserName": user,
                        "PolicyName": pol["PolicyName"],
                        "PolicyType": pol["PolicyType"],
                    },
                ),
            )
        step(
            "删登录配置",
            lambda: self._call(volcano.IAM, "DeleteLoginProfile", {"UserName": user}),
        )
        if not left:
            step("删用户", lambda: self._call(volcano.IAM, "DeleteUser", {"UserName": user}))
        return left

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
