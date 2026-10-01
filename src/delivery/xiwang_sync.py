"""曦望同步拉取的对象级清单状态机。

杭州到新加坡和新加坡到曦望是两条独立的并发队列。对象只有在中转端
完整可见后才进入曦望队列，避免把 OSS 的原子对象误当成可边写边读的流。
此模块只负责持久化清单和调度决策，远端传输由 worker 执行。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import DeliveryError

RELAYING, RELAYED, PULLING, VERIFIED, FAILED = (
    "relaying", "relayed", "pulling", "verified", "failed"
)


class SyncError(DeliveryError):
    pass


@dataclass(frozen=True)
class Object:
    key: str
    size: int
    etag: str = ""
    state: str = RELAYING
    attempts: int = 0
    error: str = ""

    def as_dict(self):
        return self.__dict__.copy()


class Manifest:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.objects = {}
        if self.path.exists():
            try:
                rows = json.loads(self.path.read_text(encoding="utf-8"))
                self.objects = {x["key"]: Object(**x) for x in rows}
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise SyncError(f"同步清单损坏：{self.path}") from exc

    def save(self):
        fd, tmp = tempfile.mkstemp(prefix=".sync-", dir=str(self.path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump([x.as_dict() for x in self.objects.values()], out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def seed(self, rows):
        for row in rows:
            key = str(row.get("key") or "")
            if not key or key.startswith("/") or ".." in key:
                raise SyncError("清单对象路径不合法")
            size = int(row.get("size") or 0)
            old = self.objects.get(key)
            if old and (old.size != size or old.etag != str(row.get("etag") or "")):
                raise SyncError(f"对象在同步中发生变化：{key}")
            if not old:
                self.objects[key] = Object(key, size, str(row.get("etag") or ""))
        self.save()

    def ready_for_pull(self, limit=10):
        return [x for x in self.objects.values() if x.state == RELAYED][:limit]

    def mark(self, key, state, *, error=""):
        old = self.objects.get(key)
        if old is None:
            raise SyncError(f"清单中没有对象：{key}")
        allowed = {RELAYING: {RELAYED, FAILED}, RELAYED: {PULLING}, PULLING: {VERIFIED, FAILED}, FAILED: {RELAYING, PULLING}}
        if state not in allowed.get(old.state, set()):
            raise SyncError(f"对象状态不能从 {old.state} 改为 {state}：{key}")
        self.objects[key] = Object(old.key, old.size, old.etag, state, old.attempts + 1, error[:300])
        self.save()

    def done(self):
        return bool(self.objects) and all(x.state == VERIFIED for x in self.objects.values())

