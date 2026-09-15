"""变更范围（scope）：把「管理员改固定资产」和「用户改日常资源」分开。

为什么必须分开
──────────────
这两类东西的变更节奏、爆炸半径、审批人都不一样：

  foundation  VPC / CPFS / OSS 桶 / RAM 角色 / OIDC        月级，量小，风险高
  workspace   DSW 实例 / 训练任务 / 数据集挂载              天级，量大，风险低

混在一条流水线里有三个具体后果：
  ① 用户天天申请 DSW，Terraform state 锁一直被占，管理员想改 VPC 得排队；
  ② 一次 plan 里同时出现「建一台 DSW」和「删一个 CPFS 文件系统」，审批的人很难不看走眼；
  ③ 用户需要的角色会被迫放宽到能碰 VPC——哪怕只是为了 plan。

组合规则
────────
平台级的 `capabilities.apply` 是**上限**：平台说不能 apply，任何 scope 都不能。
scope 只能在上限之内收紧，不能放宽。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import PlatformSpecError

SCOPE_FOUNDATION = "foundation"
SCOPE_WORKSPACE = "workspace"
SCOPES = (SCOPE_FOUNDATION, SCOPE_WORKSPACE)

_DESC = {
    SCOPE_FOUNDATION: "固定资产（网络/存储/身份），管理员专属",
    SCOPE_WORKSPACE: "日常资源（实例/任务/挂载），用户自助",
}


def describe(scope: str) -> str:
    return _DESC.get(scope, scope)


@dataclass(frozen=True)
class ScopeCapabilities:
    name: str
    apply: bool
    require_approval: bool
    quota_gated: bool = False

    @classmethod
    def from_mapping(
        cls,
        name: str,
        data: Mapping[str, Any],
        *,
        platform: str,
        platform_apply: bool,
    ) -> ScopeCapabilities:
        if name not in SCOPES:
            raise PlatformSpecError(
                f"平台 `{platform}` 声明了未知 scope `{name}`；可选 {list(SCOPES)}"
            )
        for key in ("apply", "require_approval"):
            if not isinstance(data.get(key), bool):
                raise PlatformSpecError(
                    f"平台 `{platform}` 的 scope `{name}` 缺少布尔字段 `{key}`（须显式 true/false）"
                )
        scope = cls(
            name=name,
            apply=bool(data["apply"]),
            require_approval=bool(data["require_approval"]),
            quota_gated=bool(data.get("quota_gated", False)),
        )
        scope.validate(platform=platform, platform_apply=platform_apply)
        return scope

    def __post_init__(self) -> None:
        # foundation 的审批门是类型不变量，不能靠某条加载路径。同 Capabilities 的做法。
        if self.name == SCOPE_FOUNDATION and self.apply and not self.require_approval:
            raise PlatformSpecError(
                f"scope `{self.name}`：改固定资产必须过审批，require_approval 不能为 false"
            )

    def validate(self, *, platform: str, platform_apply: bool) -> None:
        # 平台级 apply 是上限：scope 只能收紧，不能放宽。
        # 否则「火山整体还没真机验证（apply=false）」会被某个 scope 悄悄绕过去。
        if self.apply and not platform_apply:
            raise PlatformSpecError(
                f"平台 `{platform}` 的 scope `{self.name}`：平台级 apply 为 false 时，"
                f"scope 不能声明 apply=true（平台能力是上限，scope 只能收紧）"
            )
        if self.quota_gated and self.name == SCOPE_FOUNDATION:
            raise PlatformSpecError(
                f"平台 `{platform}`：quota_gated 只对 {SCOPE_WORKSPACE} 有意义，"
                f"固定资产不按配额自助"
            )


def default_scopes(*, platform_apply: bool) -> dict:
    """描述符没声明 scopes 时的兜底：**全部当固定资产**，只有管理员能动。

    刻意偏保守：没声明就默认最严，而不是默认放开。新平台接进来时，作者可能
    还没想清楚哪些资源是用户能自助的；这时候默认放开会把一个未经设计的自助
    入口直接开给全员。
    """
    return {
        SCOPE_FOUNDATION: ScopeCapabilities(
            name=SCOPE_FOUNDATION, apply=platform_apply, require_approval=True
        )
    }
