#!/usr/bin/env python3
"""升级面板之前，在**目标服务器上**跑一遍：这版新代码上去之后能不能正常工作。

为什么要有这个
──────────────
面板的几处硬依赖是「缺了就整个功能不可用，但服务照样起得来」：

  · `cryptography`：面板长期零依赖，这版起访问凭证要加密存。旧版服务器上没装过，
    缺了的话凭证类申请在**提交那一刻**就被拒——而面板本身一切正常，看不出所以然
  · `DELIVERY_ISSUER_*`：发凭证和到期删号用的是它，和开通身份是两把不同的 AK。
    定时任务的 EnvironmentFile 里漏了的话，长期凭证到期永远不会被清理
  · `DELIVERY_BASE_URL`：凭证的查看地址靠它拼。指向本机的话，发出去的链接使用方打不开

这些都是**部署时**能查出来、运行时才会发作的东西。所以在动线上代码之前查，查不过就中止。

怎么跑
──────
    python3 deploy/panel/preflight.py --env /etc/delivery/panel.env --tickets identity/tickets.json

`--env` 可以给多个（面板一个、定时任务一个），每个都单独查——两边环境不一样是常见的坑。
只读，不改任何东西，不打印任何密钥的值。退出码非 0 即不要继续部署。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

#: 申请单文件的 schema，和 tickets.py 一致（同样是手抄，理由见模块注释）
SCHEMA = "wuji-tickets@1"
#: 这版代码认得的申请单状态。线上已有的单子只要出现别的值，新代码就读不出来
KNOWN_STATUS = {
    "submitting",
    "submit_failed",
    "pending_approval",
    "approved",
    "executing",
    "fulfilling",
    "done",
    "failed",
    "rejected",
    "withdrawn",
    "closed",
    "revoked",
}
#: 已经删掉的状态。线上还留着这两种单子的话，升上去就读不出来了
GONE_STATUS = {"claimable", "expired"}
_LOCAL_HOST = re.compile(r"\A(localhost|127\.\d+\.\d+\.\d+|::1|\[::1\])\Z", re.I)
_ISSUER = re.compile(r"\ADELIVERY_ISSUER_[A-Z0-9_]+\Z")
_EXEC = re.compile(r"\ADELIVERY_EXEC_[A-Z0-9_]+\Z")


class Result:
    def __init__(self) -> None:
        self.problems: list = []
        self.warnings: list = []

    def bad(self, what: str, fix: str) -> None:
        self.problems.append((what, fix))
        print(f"  ✗ {what}\n    → {fix}")

    def warn(self, what: str, fix: str) -> None:
        self.warnings.append((what, fix))
        print(f"  ! {what}\n    → {fix}")

    def ok(self, what: str) -> None:
        print(f"  ✓ {what}")


def read_env(path: Path) -> dict:
    """systemd EnvironmentFile 的最小解析：KEY=VALUE，# 起头是注释。

    **不打印任何值**，只看键在不在、以及 BASE_URL 的形状。
    """
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def check_crypto(r: Result) -> None:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        r.bad(
            "没有装 cryptography：这版起访问凭证要加密存，缺了的话凭证类申请一律被拒收",
            "pip install 'cryptography>=41'（面板跑在哪个解释器里就装到哪个）",
        )
        return
    try:
        key, nonce = os.urandom(32), os.urandom(12)
        box = AESGCM(key).encrypt(nonce, b"preflight", None)
        assert AESGCM(key).decrypt(nonce, box, None) == b"preflight"
    except Exception as exc:  # noqa: BLE001 — 装了但跑不起来（架构不对、so 缺失）
        r.bad(f"cryptography 装了但用不了：{type(exc).__name__}", "重装匹配这台机器架构的版本")
        return
    r.ok("cryptography 可用（AES-256-GCM 往返通过）")


def check_env(r: Result, path: Path) -> None:
    print(f"\n[环境] {path}")
    if not path.is_file():
        r.bad(f"读不到 {path}", "确认路径，或去掉这个 --env")
        return
    env = read_env(path)

    raw = env.get("DELIVERY_BASE_URL", "")
    if not raw:
        r.bad(
            "没有 DELIVERY_BASE_URL：凭证的查看地址拼不出来，凭证类申请一律被拒收",
            "配成面板的对外 https 地址，例如 https://panel.example.com",
        )
    else:
        parsed = urllib.parse.urlsplit(raw)
        host = parsed.hostname or ""
        if _LOCAL_HOST.match(host):
            r.bad(
                f"DELIVERY_BASE_URL 指向本机（{host}）：这样发出去的链接使用方打不开",
                "配成面板的对外 https 地址",
            )
        elif parsed.scheme != "https":
            r.bad(
                f"DELIVERY_BASE_URL 不是 https（{parsed.scheme or '没有 scheme'}）",
                "凭证的查看地址必须走 https",
            )
        else:
            r.ok(f"DELIVERY_BASE_URL 指向 {host}")

    execs = {k for k in env if _EXEC.match(k)}
    issuers = {k for k in env if _ISSUER.match(k)}
    if not issuers:
        # 不算硬错：只发 12 小时以内 STS 凭证、或压根没有凭证模板的部署用不到它。
        # 面板的 /health 会按模板分级判定，这里没有模板信息，只能提醒
        r.warn(
            "一个 DELIVERY_ISSUER_* 都没有：长期访问凭证发不出去；"
            "如果这是定时任务的环境，到期的子账号和密钥也永远不会被删",
            "只发 12 小时以内的凭证可以不配；否则按 README 第 3 步建发放身份。以 /health 为准",
        )
    else:
        r.ok(f"发放身份 {len(issuers)} 项")
    if execs and not issuers:
        r.warn(
            "有开通身份但没有发放身份 —— 两把 AK 是**故意**分开的",
            "别把开通身份的值复制成发放身份：开通身份在云上被禁掉了建号发 AK，复制过去也用不了",
        )

    hops = env.get("DELIVERY_PROXY_HOPS", "")
    if hops and not hops.isdigit():
        r.bad(f"DELIVERY_PROXY_HOPS 不是数字（{hops!r}）", "填代理层数，线上通常是 2")


def check_tickets(r: Result, path: Path) -> None:
    print(f"\n[申请单] {path}")
    if not path.is_file():
        r.ok("还没有申请单文件（全新部署）")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        r.bad(f"读不出来：{type(exc).__name__}", "先确认文件没坏，别在这个状态下升级")
        return
    if isinstance(data, dict):
        # TicketStore._read 认死这两样：schema 对不上或 tickets 不是数组，面板一张单子都读不出来
        if data.get("schema") != SCHEMA:
            r.bad(
                f"schema 是 {data.get('schema')!r}，这版代码只认 {SCHEMA!r}",
                "核对是不是指错了文件，或降级过",
            )
            return
        items = data.get("tickets")
        if not isinstance(items, list):
            r.bad("tickets 字段不是数组", "文件可能坏了，别在这个状态下升级")
            return
    else:
        items = data
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        r.bad("申请单不是对象数组", "文件可能坏了，别在这个状态下升级")
        return
    counts: dict = {}
    for item in items:
        counts[str(item.get("status") or "")] = counts.get(str(item.get("status") or ""), 0) + 1

    stale = {s: n for s, n in counts.items() if s in GONE_STATUS}
    unknown = {s: n for s, n in counts.items() if s and s not in KNOWN_STATUS}
    if stale:
        r.bad(
            f"有 {sum(stale.values())} 张单子停在已经删掉的状态（{'、'.join(stale)}）",
            "升级前先把这些单子处理掉（关闭或标记回收），否则新代码读不出它们",
        )
    for status, n in unknown.items():
        if status not in GONE_STATUS:
            r.bad(
                f"有 {n} 张单子的状态 {status!r} 这版代码不认得",
                "核对是不是降级过，或文件被改过",
            )
    if not stale and not unknown:
        shape = "、".join(f"{s} {n}" for s, n in sorted(counts.items()) if s)
        r.ok(f"{len(items)} 张单子，状态都认得（{shape or '无'}）")

    # 这版把「凭证发出来了但没送达」的单子纳入了到期回收范围。升级后第一轮定时任务
    # 就会去删这些子账号 —— 先报个数，让人知道会发生什么，而不是事后在日志里看见
    pending = [
        x
        for x in items
        if x.get("kind") == "credential"
        and x.get("cred_user")
        and x.get("status") in ("failed", "closed")
    ]
    if pending:
        r.warn(
            f"有 {len(pending)} 张失败/已关闭的凭证单还留着子账号，"
            f"升级后第一轮定时任务会去删掉它们",
            "这是本版的预期行为（那些凭证谁都用不了）。不想删的先手动处理",
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="面板升级前置检查（只读）")
    ap.add_argument("--env", action="append", default=[], help="EnvironmentFile，可给多个")
    ap.add_argument("--tickets", default="identity/tickets.json", help="申请单文件")
    args = ap.parse_args(argv)

    r = Result()
    print(f"[依赖]  解释器 {sys.executable}")
    print("  （要用**面板服务实际跑的那个**解释器跑本脚本；装在别的 venv 里不算）")
    check_crypto(r)
    for path in args.env:
        check_env(r, Path(path))
    check_tickets(r, Path(args.tickets))

    print()
    if r.problems:
        print(f"✗ {len(r.problems)} 项不通过，**不要继续部署**")
        return 1
    if r.warnings:
        print(f"✓ 检查通过（{len(r.warnings)} 条提醒，看清楚再继续）")
        return 0
    print("✓ 检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
