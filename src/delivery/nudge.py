"""提醒本人：面板上看得到，飞书上也收得到。

为什么要有这么一层
──────────────────
面板上标一个「该换了」，等于**指望人主动来看**。而人不会主动来看密钥页 ——
那一栏平时没有任何理由打开。所以标记只对已经在页面上的人有用，对别人等于不存在。

反过来只发飞书也不行：消息会被划走、会被折叠、找不回来。**两边都要有**，
而且说的是同一件事：面板是「随时查得到的状态」，飞书是「这一刻推到你眼前」。

三条规矩
────────
1. **节流**。同一个人同一件事，`THROTTLE_DAYS` 天内只提醒一次。
   天天提醒等于没提醒 —— 人学会忽略之后，真要紧的那条也一起被忽略了。
2. **文案是动作，不是状态。**「你的密钥建了 300 天」是状态，人看完不知道该干嘛；
   「去控制台建一把新的，两把并存几天，程序都切过去再停旧的」才是动作。
3. **发了要留痕。** 谁提醒的、提醒了谁、哪一件事 —— 进 `review.log`。
   不然三个月后没人说得清「到底通知过他没有」。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from .errors import DeliveryError

#: 同一个人同一件事，多少天内不重复提醒
THROTTLE_DAYS = 14
STATE_FILE = "nudge-sent.json"

#: 能提醒哪些事。**加一条就在这里加**，不在调用处拼文案 ——
#: 散在各处的提醒文案，改的时候只会改到一处
TOPICS = {
    "rotate": {
        "title": "你的云密钥该换了",
        "color": "orange",
        "what": "这把 AccessKey 建得太久了。",
        "todo": (
            "去云控制台建一把新的，两把并存几天，把所有程序和脚本都切过去之后，"
            "再停用旧的那把。\n\n先停旧的会让还在用它的定时任务静默失败，"
            "而那种失败通常要等到有人报错才被发现。"
        ),
    },
    "unused": {
        "title": "你有一把云密钥很久没用了",
        "color": "orange",
        "what": "这把 AccessKey 长时间没有调用记录。",
        "todo": (
            "还要用吗？不用就去控制台停用它，停用随时能改回来。\n\n"
            "要是它其实在用（比如只在某个季度任务里跑），忽略这条。"
        ),
    },
}


class NudgeError(DeliveryError):
    """提醒发不出去。"""


def _state_path(people_path: str) -> Path:
    return Path(people_path).resolve().parent / STATE_FILE


def _key(union_id: str, topic: str, ref: str) -> str:
    """节流键。

    `ref` 是**被提醒的那个东西**的稳定标识（密钥前八位、目录路径……）。

    这里踩过两次：
      · 一开始用 `detail`。而 detail 是「最近用过 2026-09-20」这种会变的文本，
        这个人每用一次密钥键就变一次，14 天的窗口永远不生效。
      · 改用 `subject` 又反了 —— subject 是子账号名，同一个人的两把密钥 subject 相同，
        提醒了第一把，第二把 14 天内就发不出去了。
    所以要调用方明确给一个「这条提醒说的是哪个东西」。
    """
    return f"{topic}/{union_id}/{ref}"


def load_sent(people_path: str, *, now: Optional[float] = None) -> dict:
    """还在节流期内的提醒。过期的自动不算数。"""
    at = time.time() if now is None else now
    try:
        data = json.loads(_state_path(people_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    window = THROTTLE_DAYS * 86400
    return {
        k: v
        for k, v in data.items()
        if isinstance(v, dict) and at - float(v.get("at") or 0) < window
    }


def card(topic: str, *, subject: str, why: str, detail: str, base_url: str = "") -> dict:
    """提醒卡。**先说该干什么，再说为什么** —— 人先看到的是第一行。"""
    spec = TOPICS.get(topic)
    if spec is None:
        raise NudgeError(f"不认识的提醒类型 {topic!r}")
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": spec["todo"]}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{subject}**　{why}"}},
    ]
    if detail:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": detail}]})
    if base_url.startswith("https://"):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "在面板上看"},
                        "url": f"{base_url}/#assets",
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": spec["color"],
            "title": {"tag": "plain_text", "content": spec["title"]},
        },
        "elements": elements,
    }


def send(
    notifier,
    *,
    people_path: str,
    union_id: str,
    topic: str,
    subject: str,
    why: str,
    detail: str = "",
    ref: str = "",
    base_url: str = "",
    actor: str = "",
    log: Optional[Callable] = None,
    now: Optional[float] = None,
    force: bool = False,
) -> dict:
    """私聊提醒一个人。返回 `{sent, throttled_until}`。

    `force=True` 跳过节流 —— 给「我就是要现在再推一次」留的口子，
    但它照样落日志，所以滥用看得见。
    """
    uid = str(union_id or "").strip()
    if not uid:
        raise NudgeError("这条记录没有属主的 union_id，找不到人")
    if topic not in TOPICS:
        raise NudgeError(f"不认识的提醒类型 {topic!r}")
    at = time.time() if now is None else now
    # 没给 ref 就退回 subject。**退回不是等价** —— 那时同一个人的多个对象会互相挡，
    # 所以调用方该给就给
    key = _key(uid, topic, str(ref or subject))
    sent = load_sent(people_path, now=at)
    if not force and key in sent:
        left = THROTTLE_DAYS * 86400 - (at - float(sent[key].get("at") or 0))
        raise NudgeError(
            f"{max(1, int(left // 86400))} 天前提醒过同一件事，{THROTTLE_DAYS} 天内不重复发。"
        )

    notifier.send(
        uid,
        card(topic, subject=subject, why=why, detail=detail, base_url=base_url),
        id_type="union_id",
    )

    # 发出去之后才记。**顺序不能反** —— 先记后发的话，发失败了节流还是生效了，
    # 于是这个人接下来两周都收不到提醒，而没有任何地方显示「其实没发出去」
    path = _state_path(people_path)
    data = {}
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(got, dict):
            data = got
    except (OSError, json.JSONDecodeError):
        data = {}
    data[key] = {"at": at, "by": actor, "topic": topic, "union_id": uid}
    try:
        from .cli import _atomic_private_write, _require_identity_dir

        _require_identity_dir(path)
        _atomic_private_write(path, (json.dumps(data, ensure_ascii=False) + "\n").encode())
    except Exception as exc:  # noqa: BLE001 — 记不下节流不该让「已经发出去了」变成失败
        if log is not None:
            log({"op": "nudge_state_failed", "error": f"{type(exc).__name__}: {exc}"})

    if log is not None:
        log(
            {
                "op": "nudge",
                "actor": actor,
                "union_id": uid,
                "topic": topic,
                "subject": subject,
                "why": why,
                "detail": detail,
            }
        )
    return {"sent": True, "throttled_days": THROTTLE_DAYS}
