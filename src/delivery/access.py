"""面向用户的登录引导。

这一层只回答一个问题：**这个人现在要用这个平台，下一步该做什么。**

为什么要单独一层：能力矩阵是给流水线看的，用户不该被迫理解 `auth=static-secret`
意味着什么。他们只需要知道「能直接进」还是「要先绑一次」，以及绑的动作在哪。

诚实原则：接不了飞书 SSO 的平台**如实说接不了**，并给出替代路径。把「绑定一次」
包装成「已支持单点登录」会让人在控制台前反复试密码，比直说更伤。
"""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import LOGIN_BIND, LOGIN_CERT, LOGIN_PASSWORD, LOGIN_SSO
from .registry import Platform

# 用户状态。刻意不引入枚举类：这三个值会直接进 JSON 给前端，字符串最省事。
STATE_READY = "ready"  # 可以直接用
STATE_ACTION = "action"  # 需要用户做一次动作
STATE_BLOCKED = "blocked"  # 用户自己解决不了，要找管理员


@dataclass(frozen=True)
class LoginGuidance:
    """一个平台在某个用户视角下的登录引导，看板和 CLI 共用。"""

    platform_id: str
    display: str
    mode: str
    state: str
    action_label: str
    headline: str
    hint: str
    next_command: str = ""

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY

    def to_dict(self) -> dict:
        return {
            "platform_id": self.platform_id,
            "display": self.display,
            "mode": self.mode,
            "state": self.state,
            "action_label": self.action_label,
            "headline": self.headline,
            "hint": self.hint,
            "next_command": self.next_command,
        }


def guide(
    platform: Platform,
    *,
    has_account: bool = True,
    bound: bool = False,
    sso_enabled: bool = True,
) -> LoginGuidance:
    """算出某个用户在某个平台上的登录引导。

    `has_account`：身份表里这人有没有该平台的账号。
    `bound`：仅对 login=bind 有意义——凭证是否已托管。
    `sso_enabled`：仅对 login=sso 有意义——SAML 是否已经配好上线。
    """
    mode = platform.capabilities.login
    if mode == LOGIN_SSO:
        return _guide_sso(platform, has_account=has_account, sso_enabled=sso_enabled)
    if mode == LOGIN_BIND:
        return _guide_bind(platform, bound=bound)
    if mode == LOGIN_CERT:
        return _guide_cert(platform, has_account=has_account)
    if mode == LOGIN_PASSWORD:
        return _guide_password(platform, has_account=has_account)
    # registry 已经校验过取值，走到这里说明 capabilities 和本模块不同步了。
    raise AssertionError(f"未处理的登录方式 {mode!r}（平台 {platform.id}）")


def _guide_password(platform: Platform, *, has_account: bool) -> LoginGuidance:
    """有控制台、每人一个号，但接不了我们的身份系统。

    **不写「单点登录还没上线」**：那是给 aliyun/volcano 用的，它们确实在推进 SAML。
    对一个接不了的平台那么写，人会一直等一个不会来的东西，而正确的动作
    （去控制台用自己的号密码登录）反而没人告诉他。
    """
    if not has_account:
        return LoginGuidance(
            platform_id=platform.id,
            display=platform.display,
            mode=LOGIN_PASSWORD,
            state=STATE_BLOCKED,
            action_label="申请账号",
            headline=f"你在{platform.short}还没有账号",
            hint=f"{platform.short}的账号由管理员开，开好后用账号密码登录它自己的控制台。",
            next_command="",
        )
    return LoginGuidance(
        platform_id=platform.id,
        display=platform.display,
        mode=LOGIN_PASSWORD,
        state=STATE_READY,
        action_label="打开控制台",
        headline=f"用你在{platform.short}的账号密码登录",
        hint=(
            f"{platform.short}用的是它自己的身份系统，接不进飞书，所以没有一键直达，"
            f"也没有凭证可以托管。密码忘了找管理员重置。"
        ),
        next_command="",
    )


def _guide_sso(platform: Platform, *, has_account: bool, sso_enabled: bool) -> LoginGuidance:
    if not has_account:
        # 这条是用户自己解决不了的：没有子账号，SSO 断言落不到任何身份上。
        return LoginGuidance(
            platform_id=platform.id,
            display=platform.display,
            mode=LOGIN_SSO,
            state=STATE_BLOCKED,
            action_label="申请账号",
            headline=f"你在{platform.short}还没有账号",
            hint=(
                f"{platform.short}走飞书单点登录，但前提是你在上面有一个子账号。"
                f"在飞书发起「RAM 子账号申请」，审批通过后这里会自动变成可直接进入。"
            ),
            next_command="",
        )
    if not sso_enabled:
        return LoginGuidance(
            platform_id=platform.id,
            display=platform.display,
            mode=LOGIN_SSO,
            state=STATE_ACTION,
            action_label="用账号密码登录",
            headline=f"{platform.short}的单点登录还没上线",
            hint=(
                f"SAML 配置尚未完成，暂时还要用{platform.short}的账号密码登录。"
                f"上线后这里会变成一键直达，不用再记密码。"
            ),
            next_command="",
        )
    return LoginGuidance(
        platform_id=platform.id,
        display=platform.display,
        mode=LOGIN_SSO,
        state=STATE_READY,
        action_label="进控制台 →",
        headline="飞书账号可直接登录",
        hint="点击后用飞书身份直接进入控制台，不需要输入密码。",
        next_command=f"delivery console {platform.id}",
    )


def _guide_bind(platform: Platform, *, bound: bool) -> LoginGuidance:
    if bound:
        return LoginGuidance(
            platform_id=platform.id,
            display=platform.display,
            mode=LOGIN_BIND,
            state=STATE_READY,
            action_label="已托管 ✓",
            headline="凭证已托管，日常无需登录",
            hint=(
                f"{platform.short}不支持飞书登录，但你的凭证已经托管，"
                f"下单和查询都在飞书/看板完成，不用再打开它的控制台。"
            ),
            next_command=f"delivery bind {platform.id} --rotate",
        )
    # 这段文字是这一层存在的理由：说清「为什么不能像别的平台那样一键进」，
    # 并且把用户要做的一次性动作讲成三步，而不是丢一句「请配置凭证」。
    return LoginGuidance(
        platform_id=platform.id,
        display=platform.display,
        mode=LOGIN_BIND,
        state=STATE_ACTION,
        action_label="绑定凭证（一次）",
        headline=f"{platform.short}不支持飞书登录，绑定一次即可",
        hint=(
            f"{platform.short}用的是它自己的身份系统，我们无权把飞书接进去，"
            f"所以做不到一键直达。替代方案是绑定一次：\n"
            f"  1. 打开 {platform.console_url or '平台控制台'} 用你的平台账号登录\n"
            f"  2. 自助签发一对 AK/SK（只需一次，平台限制这一步无法自动化）\n"
            f"  3. 回到飞书发「绑定{platform.short}凭证」，把它粘进绑定卡\n"
            f"绑完之后你就不用再登录它了 —— 日常操作都在飞书和看板里完成。"
        ),
        next_command=f"delivery bind {platform.id}",
    )


def _guide_cert(platform: Platform, *, has_account: bool) -> LoginGuidance:
    if not has_account:
        return LoginGuidance(
            platform_id=platform.id,
            display=platform.display,
            mode=LOGIN_CERT,
            state=STATE_BLOCKED,
            action_label="申请访问",
            headline=f"你还没有{platform.short}的访问权限",
            hint=f"在飞书申请{platform.short}访问权限，批准后即可签发登录证书。",
            next_command="",
        )
    return LoginGuidance(
        platform_id=platform.id,
        display=platform.display,
        mode=LOGIN_CERT,
        state=STATE_READY,
        action_label="取 SSH 证书 →",
        headline="签发短期证书后免密登录",
        hint=(
            "点击后用飞书身份签发一张短期 SSH 证书，到期自动失效，不需要把公钥长期留在服务器上。"
        ),
        next_command=f"delivery ssh-cert {platform.id}",
    )
