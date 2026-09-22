"""飞书审批回调：审批人一点同意，面板立刻知道。

在这之前面板只会拉：定时任务每分钟一次，加上申请人自己打开页面时顺手同步。
所以「审批通过」到「云上建好号、能 SSO 登录」中间隔着最多一分钟，
而申请人那一分钟里看到的是「审批中」。

这个模块只做一件事：把飞书推过来的审批事件翻译成「哪张单子该同步了」。
真正的开通逻辑一行都不在这里 —— 它调的还是 `flows.sync`，和定时任务同一条路。
**回调只是提前触发，不是另一条执行路径**：两条路径迟早会不一致，而不一致的那天
没人知道线上跑的是哪一条。

鉴权
────
飞书不带用户身份，它带的是**这个应用在飞书后台配的 Verification Token**。
所以这里的规矩是：

  · 没配 token → **拒绝一切事件**。不是"先放行等配好"——
    一个没有鉴权的公网 POST 入口，谁都能拿它触发同步。
  · 比对用 `secrets.compare_digest`，不用 `==`。
  · 事件内容一律当外部输入：只从里面取 `instance_code`，而且先过格式白名单。

去重
────
飞书会重投（没收到 200 就重发）。同一个实例短时间内重复推，只处理一次 ——
`flows.sync` 本身是幂等的，但重复同步会重复打飞书接口。
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Callable, Optional

ENV_VERIFY_TOKEN = "DELIVERY_FEISHU_VERIFY_TOKEN"  # noqa: S105  环境变量名

#: 审批实例号：飞书给的是一串字母数字连字符。**过白名单再用** ——
#: 它会被拿去在台账里找单子，虽然只是查表，但不该让外部数据决定查什么形状的键
_INSTANCE = re.compile(r"\A[A-Za-z0-9._-]{6,128}\Z")

#: 同一个实例多少秒内不重复处理
DEDUP_WINDOW = 20.0
#: 去重表的上限，防止长期运行堆积
_DEDUP_MAX = 2000


class Hook:
    """飞书审批事件 → 该同步哪张单子。纯逻辑，不碰网络。"""

    def __init__(
        self,
        *,
        verify_token: str = "",
        codes=(),
        clock: Callable[[], float] = time.time,
    ):
        self._token = str(verify_token or "")
        #: 只处理这些审批定义的事件。**白名单，不是黑名单。**
        #: 飞书的审批事件是按定义订阅的，所以理论上本来就只会推我们订过的那条；
        #: 但「理论上不会来」和「来了也不处理」是两件事 —— 哪天有人在控制台
        #: 多订一个定义，别人的请假单就会开始打这个端点。
        self._codes = frozenset(str(c or "").strip() for c in codes if str(c or "").strip())
        self._clock = clock
        self._seen: dict = {}

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def check_token(self, body) -> bool:
        """事件里的 token 对不对。**没配 token 一律不通过。**

        位置随事件格式而异：schema 2.0 在 `header.token`，旧版在顶层 `token`。
        两处都看 —— 只看一处的话，另一种格式每次都会被拒，而表现是「回调好像没生效」。
        """
        if not self._token:
            return False
        if not isinstance(body, dict):
            return False
        header = body.get("header")
        header = header if isinstance(header, dict) else {}
        got = header.get("token") or body.get("token") or ""
        if not isinstance(got, str) or not got:
            return False
        return secrets.compare_digest(got, self._token)

    @staticmethod
    def challenge(body) -> Optional[str]:
        """飞书在后台填地址时会先发一个 challenge，原样回它。

        **这一步在验 token 之前**：还没配 token 的时候也要能通过地址校验，
        否则先有鸡还是先有蛋。challenge 本身不触发任何动作，回显它不产生风险。
        """
        if not isinstance(body, dict):
            return None
        if body.get("type") != "url_verification":
            return None
        value = body.get("challenge")
        return value if isinstance(value, str) else ""

    def mine(self, body) -> bool:
        """这条事件是不是面板自己那个审批定义的。

        没配白名单时**一律不处理** —— 和 token 一样 fail-closed：
        「先放行等配好」意味着这期间全公司的审批都会打进来。
        """
        if not self._codes or not isinstance(body, dict):
            return False
        event = body.get("event")
        event = event if isinstance(event, dict) else {}
        for holder in (event, event.get("object") or {}, body):
            if not isinstance(holder, dict):
                continue
            got = holder.get("approval_code")
            if isinstance(got, str) and got:
                return got in self._codes
        # 事件里没带定义号就当不是我们的。审批事件一定带它，不带说明不是这类事件
        return False

    def instance_of(self, body) -> str:
        """这条事件说的是哪个审批实例。取不到就返回空串（调用方据此忽略）。"""
        if not isinstance(body, dict):
            return ""
        event = body.get("event")
        event = event if isinstance(event, dict) else {}
        # 审批事件的实例号在不同版本里位置不同，逐个看
        for holder in (event, event.get("object") or {}, body):
            if not isinstance(holder, dict):
                continue
            for key in ("instance_code", "instance_id"):
                got = holder.get(key)
                if isinstance(got, str) and _INSTANCE.match(got):
                    return got
        return ""

    def claim(self, instance: str) -> bool:
        """这条事件该不该处理。重投的、刚处理过的返回 False。"""
        if not instance:
            return False
        now = self._clock()
        last = self._seen.get(instance, 0.0)
        if now - last < DEDUP_WINDOW:
            return False
        if len(self._seen) > _DEDUP_MAX:
            self._seen = {k: v for k, v in self._seen.items() if now - v < DEDUP_WINDOW}
        self._seen[instance] = now
        return True
