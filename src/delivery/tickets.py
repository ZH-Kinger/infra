"""申请单：存储、状态机、审计事件。

状态怎么流转见 docs/cloud-access-platform.md 第 4 节。这里只管三件事：
  · 状态只能按 TRANSITIONS 走，其他转换一律抛错
  · 读 - 改 - 写在文件锁里完成，多线程、多进程（面板 + 定时同步）不会互相覆盖
  · 每次变化追加一条事件（谁、何时、做了什么），事件只追加不修改

申请单里**不存任何明文凭证或密码**。初始密码是领取时现场生成、直接返回；访问凭证存的是
密文（`sealed` 字段），解密密钥只在发给使用方的链接里，服务端没有主密钥、自己也解不开 ——
见 `sealed.py`。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .errors import DeliveryError

SCHEMA = "wuji-tickets@1"

SUBMITTING = "submitting"
SUBMIT_FAILED = "submit_failed"
PENDING = "pending_approval"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
APPROVED = "approved"
EXECUTING = "executing"
DONE = "done"
FAILED = "failed"
#: 资源开通（ECS/RDS…）审批通过后停在这里：面板不建资源，等管理员按 IaC 建好回来登记
FULFILLING = "fulfilling"
CLOSED = "closed"
REVOKED = "revoked"

LABELS = {
    SUBMITTING: "提交中",
    SUBMIT_FAILED: "提交失败",
    PENDING: "待审批",
    REJECTED: "已拒绝",
    WITHDRAWN: "已撤回",
    APPROVED: "已通过",
    EXECUTING: "开通中",
    DONE: "已完成",
    FAILED: "开通失败",
    FULFILLING: "待开通",
    CLOSED: "已关闭",
    REVOKED: "已到期回收",
}

TRANSITIONS = {
    SUBMITTING: {PENDING, SUBMIT_FAILED},
    PENDING: {APPROVED, REJECTED, WITHDRAWN},
    #: 审批「通过」但核对不过（比如只有申请人自己批）的单子直接关闭，不进开通
    APPROVED: {EXECUTING, CLOSED},
    EXECUTING: {DONE, FAILED, FULFILLING},
    #: FAILED / CLOSED 也能进 REVOKED：凭证签发出来了却没送达的单子停在这两个状态，
    #: 云上那把长期 AK 得有人去删。只让 DONE 能回收的话，它永远等不到定时任务
    FAILED: {EXECUTING, CLOSED, REVOKED},
    FULFILLING: {DONE, CLOSED},
    DONE: {REVOKED},
    REVOKED: set(),
    SUBMIT_FAILED: set(),
    REJECTED: set(),
    WITHDRAWN: set(),
    #: CLOSED 能回 FAILED / FULFILLING：审批通过了、开通那步失败被关掉的单子要能重开。
    #: 这**不是**重新走审批 —— 那张飞书批条还在，`Flows._verify` 每次开通都会重新回拉核对。
    #: 关闭只是「先不处理」，不该等于「这张批条作废」：挡人的原因一旦排掉（配置改了、
    #: 云上恢复了），否则唯一的出路是让人重新申请、重新找人审批一遍
    CLOSED: {REVOKED, FAILED, FULFILLING},
}
OPEN = (SUBMITTING, PENDING, APPROVED, EXECUTING, FAILED, FULFILLING)


class TicketError(DeliveryError):
    """申请单操作不合法。status 给 HTTP 层用。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now_iso(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    # 可读、不可猜：申请单号会出现在飞书审批表单里
    return "REQ-" + time.strftime("%Y%m%d") + "-" + secrets.token_hex(4).upper()


class TicketStore:
    def __init__(self, path: str, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock

    # ── 底层读写 ─────────────────────────────────────────────────────────
    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema": SCHEMA, "tickets": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TicketError(f"读不了申请单存储：{type(exc).__name__}", 500) from exc
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise TicketError("申请单存储格式不对", 500)
        if not isinstance(data.get("tickets"), list):
            raise TicketError("申请单存储缺 tickets 数组", 500)
        return data

    def _write(self, data: dict) -> None:
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            try:
                view = memoryview(payload)
                while view:
                    view = view[os.write(fd, view) :]
                # 落盘后再替换：机器崩溃时不会留下被截断的申请单文件
                os.fsync(fd)
            finally:
                os.close(fd)
            Path(tmp).chmod(0o600)
            Path(tmp).replace(self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ── 对外 ─────────────────────────────────────────────────────────────
    def all(self) -> list:
        with self._locked():
            return self._read()["tickets"]

    def get(self, ticket_id: str) -> dict:
        for t in self.all():
            if t.get("id") == ticket_id:
                return t
        raise TicketError("没有这张申请单", 404)

    def mine(self, union_id: str) -> list:
        """某个人的申请单。**空 union_id 直接返回空**。

        比较是裸 `==`，传空串会把所有 `applicant.union_id` 缺失或为空的单子一起捞出来 ——
        那是别人的。调用方各自挡过一次（接口层会 403），但这道门该在源头，
        新增一个调用方时不该指望他记得。
        """
        if not union_id:
            return []
        return [t for t in self.all() if t.get("applicant", {}).get("union_id") == union_id]

    def create(self, ticket: dict, *, actor: str) -> dict:
        ticket = dict(ticket)
        ticket.setdefault("id", new_id())
        ticket["status"] = SUBMITTING
        ticket["created_at"] = now_iso(self._clock)
        ticket["updated_at"] = ticket["created_at"]
        ticket["events"] = [self._event(actor, "created", "提交申请")]
        with self._locked():
            data = self._read()
            if any(t.get("id") == ticket["id"] for t in data["tickets"]):
                raise TicketError("申请单号冲突，请重试", 409)
            data["tickets"].append(ticket)
            self._write(data)
        return ticket

    def update(
        self,
        ticket_id: str,
        *,
        actor: str,
        expect: Iterable[str],
        to: Optional[str] = None,
        event: str,
        note: str = "",
        fields: Optional[dict] = None,
    ) -> dict:
        """在锁里：确认当前状态在 expect 里 → 转到 to（可选）→ 合并字段 → 记事件。"""
        expect = set(expect)
        with self._locked():
            data = self._read()
            ticket = next((t for t in data["tickets"] if t.get("id") == ticket_id), None)
            if ticket is None:
                raise TicketError("没有这张申请单", 404)
            status = ticket.get("status")
            if status not in expect:
                raise TicketError(
                    f"申请单当前是「{LABELS.get(status, status)}」，不能执行这个操作", 409
                )
            if to is not None and to != status and to not in TRANSITIONS.get(status, set()):
                raise TicketError(f"不允许从 {status} 转到 {to}", 409)
            if to is not None:
                ticket["status"] = to
            for key, value in (fields or {}).items():
                if key in ("id", "status", "events", "applicant", "created_at"):
                    raise TicketError(f"不能修改字段 {key}", 500)
                ticket[key] = value
            ticket["updated_at"] = now_iso(self._clock)
            ticket["events"].append(self._event(actor, event, note))
            self._write(data)
            return ticket

    def _event(self, actor: str, event: str, note: str) -> dict:
        return {"at": now_iso(self._clock), "actor": actor, "event": event, "note": note[:500]}
