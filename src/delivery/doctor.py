"""飞书应用体检：一条命令探完这个应用会调的所有接口。

为什么需要它
────────────
飞书的权限缺失**大多是静默的**。消息、卡片照发（那些只要 `im:*`），但某个后台路径
会安静地失败，要等下一次有人问「我的号怎么没建出来」才暴露。这个项目的另一半
（AIOps bot）在 2026-08-13 因此挂了近一天。

而权限本身没有自查手段——只能靠功能报错反推。所以这里把「这个应用会调的每个接口」
逐个探一遍，用应用自己的凭证，**全部只读或不产生副作用**。

判定依据（飞书的权限校验发生在参数校验**之前**）::

    code == 0          → 通
    code == 99991672   → 缺权限，飞书会在 msg 里列出需要的 scope，原样打出来
    其它 code          → 参数问题等，说明**权限是通的**（探针故意传最小参数）

这条「其它 code 也算通过」是核心：探针不追求调用成功，只追求越过权限闸门。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import DeliveryError

BASE = "https://open.feishu.cn/open-apis"
_DENIED = 99991672
_TIMEOUT = 15

OK = "ok"
DENIED = "denied"
SKIPPED = "skipped"
FAILED = "failed"
#: 越过了权限闸门，但调用本身没成功（参数不对、对象不存在……）。
#: 必须和 DENIED 分开：把它当成缺权限，会让人去申请一条自己已经有的权限——
#: 实测发生过（230001 invalid receive_id 被报成「要申请 im:message:send_as_bot」）。
NOTE = "note"


class DoctorError(DeliveryError):
    """体检本身跑不起来（拿不到 token 等）。"""


@dataclass(frozen=True)
class Probe:
    name: str
    feature: str
    scope: str
    status: str
    detail: str = ""

    @property
    def bad(self) -> bool:
        return self.status in (DENIED, FAILED)

    @property
    def needs_scope(self) -> bool:
        """只有明确被拒才是「要去申请权限」。网络失败、参数错都不是。"""
        return self.status == DENIED


Caller = Callable[[str, str, dict, Optional[dict]], tuple]


def _call(method: str, url: str, headers: dict, payload: Optional[dict]) -> tuple:
    data = json.dumps(payload).encode() if payload is not None else None
    head = dict(headers)
    if data is not None:
        head.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, headers=head, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return resp.getcode(), json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"msg": raw[:200]}
    except urllib.error.URLError as exc:
        raise DoctorError(f"连不上飞书：{exc.reason}") from exc


def tenant_token(*, app_id: str, app_secret: str, caller: Optional[Caller] = None) -> str:
    """拿应用身份 token。这一步失败说明 app_id/secret 本身就不对，后面全不用探。"""
    if not app_id or not app_secret:
        raise DoctorError("缺少 DELIVERY_FEISHU_APP_ID / DELIVERY_FEISHU_APP_SECRET，无法体检")
    call = caller or _call
    _, body = call(
        "POST",
        f"{BASE}/auth/v3/tenant_access_token/internal",
        {},
        {"app_id": app_id, "app_secret": app_secret},
    )
    if body.get("code") != 0 or not body.get("tenant_access_token"):
        raise DoctorError(
            f"拿不到 tenant_access_token：code={body.get('code')} msg={body.get('msg')}。"
            f"通常是 app_id / app_secret 写错，或应用还没发布版本。"
        )
    return str(body["tenant_access_token"])


def _probe(
    call: Caller,
    token: str,
    name: str,
    feature: str,
    scope: str,
    method: str,
    path: str,
    payload: Optional[dict] = None,
) -> Probe:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        _, body = call(method, f"{BASE}{path}", headers, payload)
    except DoctorError as exc:
        return Probe(name, feature, scope, FAILED, str(exc))
    code = body.get("code")
    if code == _DENIED:
        # 飞书会在 msg 里写清「需要以下任一权限」，原样透出比我们复述准确。
        return Probe(name, feature, scope, DENIED, str(body.get("msg") or "")[:300])
    if code == 0:
        return Probe(name, feature, scope, OK)
    # 参数错、对象不存在等 —— 说明已经越过权限闸门，权限是通的。
    return Probe(name, feature, scope, OK, f"（越过权限闸门，code={code}）")


def run(
    *,
    app_id: str,
    app_secret: str,
    caller: Optional[Caller] = None,
    send_to: str = "",
) -> list:
    """探完这个应用会用到的每个接口，返回结果列表。

    `send_to` 给了就**真发一条消息**到那个邮箱。这是唯一决定性的验证——
    探针靠「非 99991672 即通过」判定，而飞书会回一些文档里查不到的错误码
    （实测 99992351，通用错误码表和接口错误码表都没有它）。推理靠不住时，
    发一条真的出去最省事。顺带还能回答「企业邮箱能不能直接当 receive_id」，
    官方文档对此只写了「真实邮箱」，没说企业邮箱算不算。
    """
    call = caller or _call
    token = tenant_token(app_id=app_id, app_secret=app_secret, caller=call)
    out = [
        _probe(
            call,
            token,
            "im/v1/messages",
            "发消息与卡片（私聊 + 群）",
            "im:message:send_as_bot",
            "POST",
            "/im/v1/messages?receive_id_type=open_id",
            # 故意给一个不存在的 receive_id：权限通了会回「用户不存在」而不是 99991672，
            # 而且**不会真的发出任何消息**。
            {
                "receive_id": "ou_probe_nonexistent",
                "msg_type": "text",
                "content": json.dumps({"text": "probe"}),
            },
        ),
        _probe(
            call,
            token,
            "im/v1/chats",
            "读机器人所在的群（取 chat_id）",
            "im:chat:readonly",
            "GET",
            "/im/v1/chats?page_size=1",
        ),
    ]
    if send_to:
        out.append(_send_test(call, token, send_to))
    return out


def _send_test(call: Caller, token: str, email: str) -> Probe:
    """真发一条到指定邮箱。成功即证明发消息这条链路整体可用。"""
    name = "真发一条（email）"
    feature = f"以企业邮箱为 receive_id 发给 {email}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        _, body = call(
            "POST",
            f"{BASE}/im/v1/messages?receive_id_type=email",
            headers,
            {
                "receive_id": email,
                "msg_type": "text",
                "content": json.dumps(
                    {"text": "[delivery doctor] 权限自检，收到这条说明发消息链路已通。"}
                ),
            },
        )
    except DoctorError as exc:
        return Probe(name, feature, "im:message:send_as_bot", FAILED, str(exc))
    code = body.get("code")
    if code == 0:
        return Probe(
            name,
            feature,
            "im:message:send_as_bot",
            OK,
            "已送达 —— 企业邮箱可直接当 receive_id，不需要先拿 open_id",
        )
    if code == _DENIED:
        return Probe(
            name, feature, "im:message:send_as_bot", DENIED, str(body.get("msg") or "")[:300]
        )
    # 走到这里说明**权限是通的**（否则会是 99991672）。发不出去的原因另有其他：
    # 可用范围没覆盖、这个 receive_id 类型不被接受、机器人能力没开……
    # 每种处置不同，所以原样透出飞书的 code+msg，不替它解释、更不能报成缺权限。
    msg = str(body.get("msg") or "")
    ext = (
        str(body.get("error", {}).get("message") or "")
        if isinstance(body.get("error"), dict)
        else ""
    )
    hint = ""
    if code == 230001 and "receive_id" in f"{msg}{ext}".lower():
        hint = "（权限没问题：企业邮箱不能当 receive_id，要用 open_id。实测确认）"
    return Probe(
        name, feature, "im:message:send_as_bot", NOTE, f"code={code} msg={msg[:160]} {hint}".strip()
    )


def render(probes: list) -> str:
    """人能读的报告。

    「要申请的 scope」**只列明确被拒的**。把参数错、网络错也算进去，
    会让人去申请一条自己已经有的权限——那正是这个工具要消灭的浪费。
    """
    icon = {
        OK: "  [通过]",
        DENIED: "  [缺权限]",
        SKIPPED: "  [跳过]",
        FAILED: "  [失败]",
        NOTE: "  [注意]",
    }
    rows = sorted(probes, key=lambda p: (not p.bad, p.status != NOTE, p.name))
    lines = []
    for p in rows:
        lines.append(f"{icon[p.status]} {p.name:<20} {p.feature}")
        if p.detail:
            lines.append(f"           {p.detail}")
    denied = [p for p in probes if p.needs_scope]
    failed = [p for p in probes if p.status == FAILED]
    lines.append("")
    if denied:
        lines.append(f"缺 {len(denied)} 项权限。要申请的 scope：")
        for p in denied:
            lines.append(f"  · {p.scope}    （{p.feature}）")
        lines.append("")
        lines.append("改完权限**必须创建版本并发布**才生效，光在权限页勾选没用。")
    if failed:
        lines.append(f"{len(failed)} 项调用失败，但**不是权限问题**，别去申请权限：")
        for p in failed:
            lines.append(f"  · {p.name}：{p.detail}")
    if not denied and not failed:
        lines.append("应用身份这一侧全通。")
    lines.append("")
    lines.append(
        "注意：`contact:user.employee:readonly` 是**用户身份**权限，探不到——"
        "它要等第一次有人走完飞书登录才能验证。登录后如果拿不到企业邮箱，"
        "就是这条没申请、或管理后台没启用飞书邮箱服务。"
    )
    return "\n".join(lines)
