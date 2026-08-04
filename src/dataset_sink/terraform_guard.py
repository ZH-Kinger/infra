"""Claude Code PreToolUse hook：阻止在本地执行 Terraform 变更命令。"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Iterable, Optional

FORBIDDEN = frozenset(
    {"apply", "destroy", "import", "state", "taint", "untaint", "force-unlock", "login"}
)
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})


def forbidden_terraform_subcommand(command: str) -> Optional[str]:
    """返回命中的危险子命令；识别 env 前缀、-chdir 和 shell -c。"""
    try:
        tokens = list(_tokens(command))
    except ValueError:
        # shell 引号不完整时 Bash 本来也执行不了，不由本 hook 猜测。
        return None

    segment = []
    for token in [*tokens, ";"]:
        if token in _SEPARATORS:
            found = _scan_segment(segment)
            if found:
                return found
            segment = []
        else:
            segment.append(token)
    return None


def _tokens(command: str) -> Iterable[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return lexer


def _scan_segment(tokens: list[str]) -> Optional[str]:
    if not tokens:
        return None

    executable_index = _executable_index(tokens)
    if executable_index is None:
        return None
    executable = Path(tokens[executable_index]).name

    if executable in {"sh", "bash", "zsh"}:
        try:
            command_index = tokens.index("-c", executable_index + 1) + 1
        except (ValueError, IndexError):
            return None
        return forbidden_terraform_subcommand(tokens[command_index])

    if executable != "terraform":
        return None
    for token in tokens[executable_index + 1 :]:
        if token.startswith("-"):
            continue
        return token if token in FORBIDDEN else None
    return None


def _executable_index(tokens: list[str]) -> Optional[int]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if "=" in token and not token.startswith("="):
            index += 1
            continue
        if token in {"env", "command"}:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            continue
        return index
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    forbidden = forbidden_terraform_subcommand(command)
    if not forbidden:
        return 0
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"本仓库禁止本地 terraform {forbidden}；请通过审批后的 CI/CD 流水线执行。"
                ),
            }
        },
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
