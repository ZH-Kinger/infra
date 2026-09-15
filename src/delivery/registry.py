"""平台注册表：从 `platforms/*.json` 加载描述符。

为什么是 JSON 不是 YAML：本仓库硬规「不引入运行时第三方依赖」，而 py39 的标准库
没有 YAML。仓库既有的 `deploy/data-sources.json`、`deploy/pai/runtime-profiles.json`
也都是 JSON，保持一致。

新增一个平台 = 往 `platforms/` 放一个 JSON + 写一个 adapter，流水线不用改。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional

from .capabilities import Capabilities
from .errors import PlatformNotFoundError, PlatformSpecError
from .scopes import SCOPE_FOUNDATION, ScopeCapabilities, default_scopes

# 仓库根下的 platforms/。用相对本文件的路径，避免依赖调用方的 cwd。
_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "platforms"

# id 会被拼进 `delivery bind <id>`、GitHub Actions matrix 和（未来的）Redis 键。
# 必须字母开头：`-rf` 这种以连字符开头的 id 在命令行里会被当成 flag（审计实测
# `"-"`、`"--"`、`"-rf"` 原先全部能加载）。同时限长，避免拼出超长路径。
_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")


@dataclass(frozen=True)
class Platform:
    """一个可投递的平台。"""

    id: str
    display: str
    capabilities: Capabilities
    short: str = ""
    console_url: str = ""
    # SSO 是否**真的配好上线**了。这是运营状态、不是能力：SAML 没配完之前
    # 告诉用户「可直接登录」，他会在控制台前反复试密码。实测两个阿里云账号
    # 的 GetUserSsoSettings 都是 SsoEnabled=false，所以默认 false。
    sso_enabled: bool = False
    accounts: tuple = ()
    adapter: Mapping[str, Any] = MappingProxyType({})
    notes: tuple = ()
    scopes: Mapping[str, ScopeCapabilities] = MappingProxyType({})

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str) -> Platform:
        pid = data.get("id")
        if not isinstance(pid, str) or not pid:
            raise PlatformSpecError(f"{source}: 缺少 `id`")
        if not _ID_RE.match(pid):
            raise PlatformSpecError(
                f"{source}: `id` 必须以小写字母开头，只含小写字母/数字/连字符，"
                f"长度 1-32，当前是 {pid!r}"
            )
        caps = data.get("capabilities")
        if not isinstance(caps, Mapping):
            raise PlatformSpecError(f"{source}: 缺少 `capabilities` 对象")
        accounts = data.get("accounts") or []
        if not isinstance(accounts, list) or any(not isinstance(a, str) for a in accounts):
            raise PlatformSpecError(f"{source}: `accounts` 必须是字符串数组")
        notes = data.get("notes") or []
        if not isinstance(notes, list) or any(not isinstance(n, str) for n in notes):
            raise PlatformSpecError(f"{source}: `notes` 必须是字符串数组")
        caps_obj = Capabilities.from_mapping(caps, platform=pid)
        raw_scopes = data.get("scopes")
        if raw_scopes is None:
            scopes = default_scopes(platform_apply=caps_obj.apply)
        elif isinstance(raw_scopes, Mapping):
            scopes = {
                name: ScopeCapabilities.from_mapping(
                    name,
                    value if isinstance(value, Mapping) else {},
                    platform=pid,
                    platform_apply=caps_obj.apply,
                )
                for name, value in raw_scopes.items()
            }
        else:
            raise PlatformSpecError(f"{source}: `scopes` 必须是对象")
        if SCOPE_FOUNDATION not in scopes:
            # 固定资产那一档必须存在：没有它就意味着「这个平台上没有需要管理员
            # 把关的东西」，而那对任何真实平台都不成立。
            raise PlatformSpecError(f"{source}: `scopes` 必须包含 `{SCOPE_FOUNDATION}`")
        return cls(
            id=pid,
            display=str(data.get("display") or pid),
            short=str(data.get("short") or data.get("display") or pid),
            capabilities=caps_obj,
            console_url=str(data.get("console_url") or ""),
            sso_enabled=bool(data.get("sso_enabled", False)),
            accounts=tuple(accounts),
            # 只读：adapter 里有 `forbidden` 这类禁用名单，可变浅拷贝意味着
            # 任何持有 Platform 的代码都能把它改空。
            adapter=MappingProxyType(dict(data.get("adapter") or {})),
            notes=tuple(notes),
            scopes=MappingProxyType(scopes),
        )


class PlatformRegistry:
    """已加载的平台集合。刻意不缓存到模块级：测试要能指向不同目录而互不污染。"""

    def __init__(self, platforms: Mapping[str, Platform]):
        self._platforms = dict(platforms)

    @classmethod
    def load(cls, directory: Optional[str] = None) -> PlatformRegistry:
        # 装成非 editable wheel 后，_DEFAULT_DIR 的仓库相对定位会失效（platforms/
        # 不是 package data）。留一个环境变量出口，报错也会如实带上路径。
        base = Path(directory or os.environ.get("DELIVERY_PLATFORMS_DIR") or _DEFAULT_DIR)
        if not base.is_dir():
            raise PlatformSpecError(f"平台描述符目录不存在：{base}")
        found: dict = {}
        for path in sorted(base.glob("*.json")):
            try:
                with path.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except json.JSONDecodeError as exc:
                raise PlatformSpecError(f"{path}: JSON 解析失败：{exc}") from exc
            platform = Platform.from_mapping(data, source=str(path))
            if platform.id != path.stem:
                # 重复 id 有检测，「张冠李戴」原先没有：aliyun.json 里写 id=volcano
                # 会被静默注册成 volcano，评审看文件名根本看不出来。
                raise PlatformSpecError(
                    f"{path}: 文件名与 `id` 不一致（id={platform.id!r}），请让文件名等于 id"
                )
            if platform.id in found:
                # 两个文件声明同一个 id，后加载的会静默覆盖前一个——那意味着某个平台的
                # 能力声明（含 apply 开关）被另一个文件悄悄改写了，必须拒绝。
                raise PlatformSpecError(f"{path}: 平台 id `{platform.id}` 重复声明")
            found[platform.id] = platform
        if not found:
            raise PlatformSpecError(f"{base} 下没有任何平台描述符")
        return cls(found)

    def get(self, platform_id: str) -> Platform:
        try:
            return self._platforms[platform_id]
        except KeyError:
            known = ", ".join(sorted(self._platforms)) or "(空)"
            raise PlatformNotFoundError(f"未知平台 `{platform_id}`；已注册：{known}") from None

    def __contains__(self, platform_id: object) -> bool:
        return platform_id in self._platforms

    def __len__(self) -> int:
        return len(self._platforms)

    def __iter__(self) -> Iterator[Platform]:
        for pid in sorted(self._platforms):
            yield self._platforms[pid]

    def appliable(self) -> list:
        """允许 apply 的平台。流水线用它决定哪些平台能进执行阶段。"""
        return [p for p in self if p.capabilities.apply]

    def read_only(self) -> list:
        return [p for p in self if p.capabilities.read_only]
