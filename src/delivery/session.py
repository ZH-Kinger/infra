"""CLI 的本地会话与平台凭证存储。

落盘位置 `~/.w0/`（可用 `W0_HOME` 覆盖），两个文件：
  session.json      当前身份（飞书 union_id / 姓名 / 会话令牌 / 到期）
  credentials.json  各平台凭证（接不了 SSO 的平台需要托管，如九章的 AK/SK）

安全约定
────────
1. **写入必然是 0600，读取时校验**。发现文件对同组或其他用户可读就拒绝使用并提示修复
   ——泰国那台机的 `~/.ossutilconfig` 曾经是 664，同机其他账号能直接读到 AK/SK。
   静默容忍宽权限等于把这个坑复制到每个人的开发机上。
2. **令牌与密钥从不进日志、不进异常消息**。本模块的错误只说“哪个文件、哪个平台”，
   不带值。
3. 目录用 0700 创建；已存在但权限过宽时同样拒绝。
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .errors import DeliveryError

SESSION_FILE = "session.json"
CREDENTIALS_FILE = "credentials.json"

_FILE_MODE = 0o600
_DIR_MODE = 0o700
# 除属主外任何一位权限都不允许
_TOO_OPEN = stat.S_IRWXG | stat.S_IRWXO


class SessionError(DeliveryError):
    """会话或凭证存储不可用。"""


def home() -> Path:
    return Path(os.environ.get("W0_HOME") or (Path.home() / ".w0"))


def _ensure_home() -> Path:
    base = home()
    if not base.exists():
        base.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
        return base
    if not base.is_dir():
        raise SessionError(f"{base} 存在但不是目录")
    _reject_if_too_open(base, what="目录")
    return base


def _reject_if_too_open(path: Path, *, what: str = "文件") -> None:
    """Windows 上 st_mode 不反映 POSIX 权限，跳过；POSIX 上一律严格。"""
    if os.name != "posix":
        return
    mode = path.stat().st_mode
    if mode & _TOO_OPEN:
        raise SessionError(
            f"{what} {path} 权限过宽（{oct(stat.S_IMODE(mode))}），同机其他账号可读。"
            f"请先执行： chmod {oct(_DIR_MODE if what == '目录' else _FILE_MODE)[2:]} {path}"
        )


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    _reject_if_too_open(path)
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SessionError(f"{path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise SessionError(f"{path} 的内容必须是 JSON 对象")
    return data


def _write_json(path: Path, payload: dict) -> None:
    """原子写：同目录临时文件（mkstemp 建出来就是 0600）写完 fsync 再替换。

    不能截断后原地写：写到一半崩了，另一个进程会读到半截 JSON；IAM 续期还会作废旧 refresh_token，
    文件丢了就只能重新登录。
    """
    base = _ensure_home()
    fd, tmp = tempfile.mkstemp(dir=str(base), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name == "posix":
            Path(tmp).chmod(_FILE_MODE)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


@contextlib.contextmanager
def session_lock():
    """本机会话文件的进程间锁（续期令牌时用）。Windows 上没有 fcntl，退化为不加锁。"""
    base = _ensure_home()
    try:
        import fcntl
    except ImportError:  # pragma: no cover - 非 POSIX
        yield
        return
    fd = os.open(str(base / ".session.lock"), os.O_WRONLY | os.O_CREAT, _FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Session:
    union_id: str
    name: str
    token: str
    expires_ts: float
    server: str = ""
    #: panel = 飞书登录换来的面板会话令牌；iam = 公司 IAM 设备码登录的 id_token（oauth2-proxy 校验）
    kind: str = "panel"
    refresh_token: str = ""
    token_endpoint: str = ""
    client_id: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_ts

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.expires_ts - time.time()))

    def redacted(self) -> dict:
        """给人看的形态：**不含令牌**。"""
        return {
            "union_id": self.union_id,
            "name": self.name,
            "server": self.server,
            "expires_ts": self.expires_ts,
            "expired": self.expired,
        }


def load_session() -> Optional[Session]:
    data = _read_json(home() / SESSION_FILE)
    if not data:
        return None
    try:
        return Session(
            union_id=str(data["union_id"]),
            name=str(data.get("name") or ""),
            token=str(data["token"]),
            expires_ts=float(data.get("expires_ts") or 0),
            server=str(data.get("server") or ""),
            kind=str(data.get("kind") or "panel"),
            refresh_token=str(data.get("refresh_token") or ""),
            token_endpoint=str(data.get("token_endpoint") or ""),
            client_id=str(data.get("client_id") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError(f"会话文件字段不完整：{exc}") from exc


def save_session(session: Session) -> None:
    _write_json(
        home() / SESSION_FILE,
        {
            "union_id": session.union_id,
            "name": session.name,
            "token": session.token,
            "expires_ts": session.expires_ts,
            "server": session.server,
            "kind": session.kind,
            "refresh_token": session.refresh_token,
            "token_endpoint": session.token_endpoint,
            "client_id": session.client_id,
        },
    )


def clear_session() -> bool:
    path = home() / SESSION_FILE
    if path.exists():
        path.unlink()
        return True
    return False


def load_credentials() -> dict:
    return _read_json(home() / CREDENTIALS_FILE)


def bound_platforms() -> set:
    """已托管凭证的平台 id 集合，供 login-guide 判断 bound。"""
    return {pid for pid, value in load_credentials().items() if value}


def save_credential(platform_id: str, payload: dict) -> None:
    """写入某平台的凭证。调用方负责只传必要字段。"""
    if not platform_id:
        raise SessionError("platform_id 不能为空")
    if not isinstance(payload, dict) or not payload:
        raise SessionError(f"平台 {platform_id} 的凭证内容为空")
    creds = load_credentials()
    creds[platform_id] = payload
    _write_json(home() / CREDENTIALS_FILE, creds)


def drop_credential(platform_id: str) -> bool:
    creds = load_credentials()
    if platform_id not in creds:
        return False
    del creds[platform_id]
    _write_json(home() / CREDENTIALS_FILE, creds)
    return True


def describe_credential(platform_id: str) -> dict:
    """凭证的**可展示**摘要：只回字段名与掐头去尾的标识，绝不回密钥值。"""
    payload = load_credentials().get(platform_id)
    if not payload:
        return {}
    out: dict[str, Any] = {"fields": sorted(payload)}
    for key in ("access_key", "access_key_id", "ak", "username", "account"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            out["identity"] = value[:6] + "…" if len(value) > 8 else value
            break
    return out


AUDIT_FILE = "bind-audit.log"


def record_bind_audit(platform: str, *, bypassed: bool = False) -> None:
    """记一条本机审计：托管了哪个平台的长期凭证、什么时候、是否绕过了护栏。

    **只记元数据，绝不记字段名以外的任何值**——审计文件的价值在于「将来收敛长期
    密钥时知道去找谁清理」，为此不需要知道密钥长什么样，而多记一分就多一分泄漏面。

    写失败不抛错：审计是辅助手段，不该让「记不下日志」挡住一次正当的凭证托管。
    但会把失败原因打出来，免得以为记上了。
    """
    line = "{ts}\tplatform={platform}\tbypassed={bypassed}\n".format(
        ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        platform=platform,
        bypassed="yes" if bypassed else "no",
    )
    path = home() / AUDIT_FILE
    try:
        _ensure_home()
        # 与凭证文件同样 0600：它暴露的是「这台机器上托管了哪些平台」，
        # 对想横向移动的人是一份现成的地图。
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _FILE_MODE)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        path.chmod(_FILE_MODE)
    except OSError as exc:
        print(f"warning: 审计记录写入失败（{path}）：{exc}", file=sys.stderr)
