"""把云上的账号拉下来。

调用方式沿用本仓库既有做法（见 `dataset_sink/aliyun_cli.py`）：**subprocess 调 `aliyun` CLI**，
不引 SDK——仓库硬规 `dependencies = []`。runner 可注入，测试不碰真云、不碰凭证。

火山没有同等好用的 CLI，走 `--from-json`：在别处（比如已经装了 SDK 的 bot）导出成
统一格式再喂进来。宁可多一步导出，也不要为一朵云破掉零依赖。
"""

from __future__ import annotations

import json
import subprocess  # noqa: S404  仓库既有做法：调 aliyun CLI
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..errors import DeliveryError
from .audit import AccountUser


class CollectError(DeliveryError):
    """拉取账号失败。"""


class PermissionDeniedError(CollectError):
    """凭证权限不足。

    单独成类，是因为它和「这人没填邮箱」长得一模一样——都是拿不到值——
    但处置完全相反：一个要去补权限，一个要去问人。混在一起会产出假结论。
    """


Runner = Callable[[Sequence[str]], str]

#: 阿里云返回的鉴权类错误码/文案片段（小写比对）。
#: 宁可漏判成普通错误（照样中断），也不要把瞬时错误误判成权限问题。
_DENIED_MARKERS = (
    "nopermission",
    "forbidden.ram",
    "accessdenied",
    "has no permission",
    "not authorized",
    "unauthorized",
    "invalidaccesskeyid",
    "signaturedoesnotmatch",
    "securitytokenexpired",
)


def _is_denied(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _DENIED_MARKERS)


def _denied(action: str, detail: str) -> PermissionDeniedError:
    return PermissionDeniedError(
        f"权限不足，`{action}` 被拒：{detail.strip()[:300]}\n"
        f"当前凭证缺 `{action}`。这不是「没有数据」——采集已中断，"
        f"不会产出不完整的结论。补齐权限后重跑。"
    )


def _subprocess_runner(command: Sequence[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=120)  # noqa: S603
    except FileNotFoundError as exc:
        raise CollectError(
            f"找不到命令 `{command[0]}`。阿里云账号采集依赖官方 CLI，请先安装并配置 profile。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectError(f"`{' '.join(command[:3])}…` 超时") from exc
    if done.returncode != 0:
        # stderr 可能含 RequestId 等排查信息，但**不会**含密钥（CLI 从 profile 读）
        detail = (done.stderr or done.stdout or "").strip()
        if _is_denied(detail):
            raise _denied(_action_of(command), detail)
        raise CollectError(f"`{' '.join(command[:3])}…` 退出码 {done.returncode}：{detail[:300]}")
    return done.stdout


def _action_of(command: Sequence[str]) -> str:
    """`["aliyun","ram","GetUser",...]` → `ram:GetUser`，用于报「缺哪个 action」。"""
    parts = [p for p in command[1:3] if p and not p.startswith("-")]
    return ":".join(parts) if len(parts) == 2 else (parts[0] if parts else command[0])


def _aliyun(args: Sequence[str], *, profile: str, runner: Runner, expect: str) -> dict:
    """调一次 CLI 并**校验响应形状**。

    `expect` 是这次调用成功时必然存在的顶层键。缺了就抛——
    CLI 有时会 0 退出码打印错误信封（`{"Code":..., "Message":...}`），
    那种 body 走 `.get("User") or {}` 会被当成「这人没填邮箱」，
    正是它造出过一次假「全通过」。
    """
    command = ["aliyun", *args]
    if profile:
        command += ["--profile", profile]
    action = _action_of(command)
    raw = runner(command)
    try:
        body = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        if _is_denied(raw):
            raise _denied(action, raw) from exc
        raise CollectError(f"aliyun CLI 的输出不是合法 JSON：{raw[:200]}") from exc
    if not isinstance(body, dict):
        raise CollectError(f"`{action}` 的响应不是对象：{str(body)[:200]}")
    if expect in body:
        return body
    code = str(body.get("Code") or "")
    message = str(body.get("Message") or "")
    envelope = f"{code} {message}".strip() or str(body)[:200]
    if _is_denied(code) or _is_denied(message):
        raise _denied(action, envelope)
    raise CollectError(f"`{action}` 的响应缺 `{expect}`，不能当作空结果：{envelope[:300]}")


def collect_aliyun(
    *, account: str = "default", profile: str = "", runner: Optional[Runner] = None
) -> list:
    """列出某个阿里云账号下的 RAM 用户（含邮箱）。

    邮箱只在 `GetUser` 里有，`ListUsers` 不返回——所以必须逐个取，
    这也是这个命令会跑几十秒的原因。
    """
    run = runner or _subprocess_runner
    users, marker, more = [], "", True
    while more:
        args = ["ram", "ListUsers", "--MaxItems", "1000"]
        if marker:
            args += ["--Marker", marker]
        body = _aliyun(args, profile=profile, runner=run, expect="Users")
        for item in (body.get("Users") or {}).get("User") or []:
            users.append(str(item.get("UserName") or ""))
        more = bool(body.get("IsTruncated"))
        marker = str(body.get("Marker") or "")
        if more and not marker:
            break

    out = []
    for name in users:
        if not name:
            continue
        detail = _aliyun(
            ["ram", "GetUser", "--UserName", name], profile=profile, runner=run, expect="User"
        )
        user = detail.get("User")
        if not isinstance(user, dict):
            raise CollectError(f"`ram:GetUser` 对 {name} 返回的 `User` 不是对象，拒绝当空值处理")
        out.append(
            AccountUser(
                platform="aliyun",
                account=account,
                name=name,
                display_name=str(user.get("DisplayName") or ""),
                email=str(user.get("Email") or ""),
            )
        )
    return out


def load_json(path: str) -> list:
    """读离线导出的账号清单。

    格式：{"platform": "volcano", "account": "default",
           "users": [{"name": ..., "display_name": ..., "email": ...}, ...]}
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CollectError(f"读不了 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise CollectError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        raise CollectError(f"{path} 缺少 `users` 数组")
    platform = str(data.get("platform") or "unknown")
    account = str(data.get("account") or "default")
    out = []
    for item in data["users"]:
        if not isinstance(item, dict) or not item.get("name"):
            raise CollectError(f"{path}: users 里有缺 `name` 的条目")
        out.append(
            AccountUser(
                platform=platform,
                account=account,
                name=str(item["name"]),
                display_name=str(item.get("display_name") or ""),
                email=str(item.get("email") or ""),
            )
        )
    return out
