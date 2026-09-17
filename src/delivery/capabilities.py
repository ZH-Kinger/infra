"""平台能力模型。

设计原则：**按能力声明，不按平台枚举**。流水线分支看的是 `Capabilities`，
不是平台名字——否则每加一个平台都要回头改流水线，而弱平台会被当成强平台的补丁。

能力的取值刻意收窄成有限集合，非法值在加载期就抛错：描述符是人手写的 JSON，
写错一个字母若被静默当成「未知能力」放行，最坏的结局是对一个没有可信预览的
平台执行了 apply。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import PlatformSpecError

# 凭证链路。oidc 是唯一不落长期密钥的一档。
AUTH_OIDC = "oidc"
AUTH_STATIC_SECRET = "static-secret"  # noqa: S105  # 凭证链路的名字，不是口令本身
AUTH_SSH_KEY = "ssh-key"
AUTH_MODES = frozenset({AUTH_OIDC, AUTH_STATIC_SECRET, AUTH_SSH_KEY})

# 基础设施即代码的形态。
IAC_TERRAFORM = "terraform"
IAC_NONE = "none"
IAC_MODES = frozenset({IAC_TERRAFORM, IAC_NONE})

# 预览来源：provider 给的 / 自己用 declared-vs-live 算的 / 没有。
PLAN_NATIVE = "native"
PLAN_DRYRUN = "dryrun"
PLAN_NONE = "none"
PLAN_MODES = frozenset({PLAN_NATIVE, PLAN_DRYRUN, PLAN_NONE})

# 清单读取方式。none 表示读不回来。
INVENTORY_API = "api"
INVENTORY_CLI = "cli"
INVENTORY_SSH = "ssh"
INVENTORY_NONE = "none"
#: 没有任何机器接口，清单由人维护（曦望这种：有控制台，但没 CLI 也没有可调的 API）。
#: 和 none 的区别不是程度，是**谁负责**：none 是「读不回来，也没人管」，
#: manual 是「读不回来，所以由人对账」—— 后者能力矩阵上看得见，前者是个黑洞。
#: 允许注册，但 apply 必须 false：没有可信的线上状态就不做变更。
INVENTORY_MANUAL = "manual"
INVENTORY_MODES = frozenset(
    {INVENTORY_API, INVENTORY_CLI, INVENTORY_SSH, INVENTORY_MANUAL, INVENTORY_NONE}
)

# 用户怎么登进这个平台。这一维只影响体验，不影响投递安全。
LOGIN_SSO = "sso"  # 飞书 SAML → 一键进控制台，不输密码
LOGIN_BIND = "bind"  # 接不了 SSO：绑定一次凭证，之后系统代跑
LOGIN_CERT = "cert"  # 签发短期证书（SSH 类）
#: 有控制台、每人一个号，但接不了我们的身份系统，也没有可托管的凭证。
#: 和 sso + sso_enabled=false 的区别是**没有「以后会好」**：后者的文案写「还没上线」，
#: 对一个永远接不了的平台那么写，人会一直等一个不会来的东西。
LOGIN_PASSWORD = "password"  # noqa: S105  # 登录方式的名字，不是口令本身
LOGIN_MODES = frozenset({LOGIN_SSO, LOGIN_BIND, LOGIN_CERT, LOGIN_PASSWORD})

# 描述符的成熟度。pending-verification 表示这条能力是查文档得来的、尚未真机验证。
STATUS_VERIFIED = "verified"
STATUS_PENDING = "pending-verification"
STATUS_VALUES = frozenset({STATUS_VERIFIED, STATUS_PENDING})


def _name(platform: str, caps: Capabilities) -> str:
    """报错里用的平台名：优先调用方给的，其次实例自带的。"""
    return platform or caps.platform or "?"


def _require(value: Any, field: str, allowed: frozenset, platform: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PlatformSpecError(
            f"平台 `{platform}` 的 `{field}` 取值非法：{value!r}；可选值 {sorted(allowed)}"
        )
    return value


@dataclass(frozen=True)
class Capabilities:
    """一个平台能做到什么。字段少而正交，新增平台不该需要新增字段。"""

    auth: str
    iac: str
    plan: str
    inventory: str
    login: str
    apply: bool
    policy_as_code: bool
    require_approval: bool
    status: str
    platform: str = ""

    def __post_init__(self) -> None:
        # 硬规必须是**类型不变量**，不能只在 from_mapping 这一条加载路径上生效：
        # frozen 挡得住原地改，挡不住 `dataclasses.replace(caps, apply=True)` 重建。
        # 审计实测：replace 能让 pending-verification 的平台拿到 apply=True。
        # 将来任何「按开关覆盖能力」的代码都会天然长成 replace 那个形状。
        self.validate(platform=self.platform)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, platform: str) -> Capabilities:
        # status 必填、不给默认值：它是 apply 的门禁之一（规则⑤），给默认值就等于
        # 「新平台的作者不知道有这个字段时，默认拿到最宽松的那档」——和下面那段
        # 拒绝给 bool 默认值的理由是同一条。审计指出过这处自相矛盾。
        for key in ("auth", "iac", "plan", "inventory", "login", "status"):
            if key not in data:
                raise PlatformSpecError(f"平台 `{platform}` 的 capabilities 缺字段 `{key}`")
        for key in ("apply", "policy_as_code", "require_approval"):
            if not isinstance(data.get(key), bool):
                raise PlatformSpecError(
                    f"平台 `{platform}` 的 `{key}` 必须显式写成 true/false，"
                    f"当前是 {data.get(key)!r}。不给默认值是刻意的：apply 这种开关"
                    f"靠默认值传播，早晚会在某个平台上默认成 true。"
                )
        caps = cls(
            auth=_require(data["auth"], "auth", AUTH_MODES, platform),
            iac=_require(data["iac"], "iac", IAC_MODES, platform),
            plan=_require(data["plan"], "plan", PLAN_MODES, platform),
            inventory=_require(data["inventory"], "inventory", INVENTORY_MODES, platform),
            login=_require(data["login"], "login", LOGIN_MODES, platform),
            apply=bool(data["apply"]),
            policy_as_code=bool(data["policy_as_code"]),
            require_approval=bool(data["require_approval"]),
            status=_require(data.get("status", STATUS_VERIFIED), "status", STATUS_VALUES, platform),
        )
        caps.validate(platform=platform)
        return caps

    def validate(self, *, platform: str = "") -> None:
        """能力之间的硬规。违反即拒绝加载，不做「警告后放行」。"""
        # ① 没有可信预览就不许 apply。蒙眼改生产算力集群是这套系统能造成的最大破坏。
        if self.plan == PLAN_NONE and self.apply:
            raise PlatformSpecError(
                f"平台 `{_name(platform, self)}`：plan=none 时 apply 必须为 false。"
                f"没有预览的 apply 等于蒙眼改生产环境。"
            )
        # ② 人工维护清单的平台，绝不能做变更：没有可信的线上状态，apply 就是蒙眼改
        if self.inventory == INVENTORY_MANUAL and self.apply:
            raise PlatformSpecError(
                f"平台 `{_name(platform, self)}`：inventory=manual 时 apply 必须为 false。"
                f"清单靠人维护就没有漂移检测，这种平台只做登记和引导。"
            )
        # ③ 读不回来的平台纳进来也只是一份写不回去的清单，对账无从谈起。
        if self.inventory == INVENTORY_NONE:
            raise PlatformSpecError(
                f"平台 `{_name(platform, self)}`：inventory=none 无法纳入投递。"
                f"读不回线上状态就没有漂移检测，「代码化」会退化成一份会过期的文档。"
            )
        # ③ Terraform 的预览由 provider 提供，声明成别的说明描述符写错了。
        if self.iac == IAC_TERRAFORM and self.plan != PLAN_NATIVE:
            raise PlatformSpecError(
                f"平台 `{_name(platform, self)}`：iac=terraform 时 "
                f"plan 必须是 native（由 provider 提供）"
            )
        # ④ 长期密钥的泄漏面比临时凭证大一个量级，人工审批是唯一补偿。
        if self.auth != AUTH_OIDC and self.apply and not self.require_approval:
            raise PlatformSpecError(
                f"平台 `{_name(platform, self)}`：非 OIDC 凭证（{self.auth}）开启 apply 时，"
                f"require_approval 必须为 true。"
            )
        # ⑤ 能力声明只要还没真机验证过，就不该拿它当 apply 的依据。查文档得来的结论
        #    （比如「这朵云支持 OIDC」）在真机上翻车过不止一次，翻车时 apply 已经动手了。
        if self.status != STATUS_VERIFIED and self.apply:
            raise PlatformSpecError(
                f"平台 `{_name(platform, self)}`：能力声明为 {self.status} 时 apply 必须为 false，"
                f"真机验证通过后再翻开。"
            )

    @property
    def keyless(self) -> bool:
        """是否完全不落长期密钥。"""
        return self.auth == AUTH_OIDC

    @property
    def read_only(self) -> bool:
        return not self.apply
