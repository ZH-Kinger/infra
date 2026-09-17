"""申请状态变化的飞书通知：给申请人发机器人消息卡片，开通失败时给管理员群发告警。

    申请人   im/v1/messages（应用机器人单聊，receive_id_type=open_id）
    管理员   alerts.py 的群自定义机器人（签名校验）

只在 DELIVERY_NOTIFY=1 时开启。飞书应用要开通「以应用身份发消息」
（im:message:send_as_bot），应用可用范围要覆盖员工，否则发不出去。

约定：
  · 通知发不出去**绝不影响申请单**：调用方（flows.Flows._emit）吞掉异常，只打日志。
  · 卡片内容全部用 plain_text：标题、策略名来自申请单，不能被当成 markdown 解析。
  · 开通失败只告诉员工「管理员会处理」，原始错误只进管理员告警，且只取脱敏后的第一行。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Mapping, Optional

from . import alerts, platforms
from .errors import DeliveryError

API = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
#: 公司 IAM 登录（oauth2-proxy）拿不到 open_id，只有企业内通用的 user_id
API_BY_USER_ID = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=user_id"
ENV_NOTIFY = "DELIVERY_NOTIFY"
ENV_BASE_URL = "DELIVERY_BASE_URL"
EVENTS = (
    "done",
    "fulfilling",
    "failed",
    "rejected",
    "withdrawn",
    "expiring",
    "revoked",
)
_TIMEOUT = 10
_MAX_BODY = 256 * 1024
_TITLE_MAX = 60
_LINE_MAX = 200
_LOCAL_HOSTS = ("127.0.0.1", "localhost")

#: (method, url, token, body) -> 解析后的 JSON
Transport = Callable[[str, str, str, Optional[dict]], dict]
Notify = Callable[[str, dict], None]


class NotifyError(DeliveryError):
    """通知没发出去。"""


def _http(method: str, url: str, token: str, body: Optional[dict]) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # url 是本模块的固定常量
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            raw = resp.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_BODY)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise NotifyError(f"连不上飞书消息接口：{type(exc).__name__}") from None
    try:
        parsed = json.loads(raw.decode() or "{}")
    except ValueError:
        raise NotifyError("飞书消息接口返回的不是 JSON") from None
    if not isinstance(parsed, dict):
        raise NotifyError("飞书消息接口返回格式不对")
    return parsed


def safe_base_url(base_url: object) -> str:
    """卡片按钮只指向 https 面板地址（本机调试放行 http://127.0.0.1 / localhost）；否则返回空串。"""
    if not isinstance(base_url, str) or any(c in base_url for c in "\"'<> \\"):
        return ""
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.query or parsed.fragment or not parsed.hostname:
        return ""
    if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS):
        return base_url.strip().rstrip("/")
    return ""


def request_link(base_url: str, ticket_id: str, *, admin: bool = False) -> str:
    base = safe_base_url(base_url)
    if not base or not ticket_id:
        return ""
    page = "admin/request" if admin else "request"
    return f"{base}/#{page}={urllib.parse.quote(str(ticket_id), safe='')}"


def _clip(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def ticket_title(ticket: Mapping) -> str:
    """和前端 requestTitle 一致：按策略申请的单子用策略名，其余用模板标题。"""
    tpl = ticket.get("template") or {}
    policies = tpl.get("policies") or []
    if tpl.get("id") == "policy" and policies:
        first = str(policies[0].get("name") or "")
        title = first if len(policies) == 1 else f"{first} 等 {len(policies)} 项"
    else:
        title = str(tpl.get("title") or ticket.get("kind_label") or ticket.get("id") or "")
    return _clip(title, _TITLE_MAX)


def _when(iso: object) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return ""


def _where(ticket: Mapping) -> str:
    tpl = ticket.get("template") or {}
    platform = platforms.name_of(str(tpl.get("platform") or ""))
    user = (ticket.get("payload") or {}).get("cloud_user") or (ticket.get("payload") or {}).get(
        "username"
    )
    parts = [p for p in (platform, f"子账号 {user}" if user else "") if p]
    return " · ".join(parts)


def message(event: str, ticket: Mapping, *, now: Optional[float] = None) -> tuple:
    """(卡片颜色, 标题, [正文行])。只用申请单里的结构化字段，不带任何错误原文。"""
    title = ticket_title(ticket)
    where = _where(ticket)
    kind = ticket.get("kind")
    if event == "done":
        lines = [where] if where else []
        expires = _when(ticket.get("expires_at"))
        if expires:
            # 资源到期只提醒不回收（删机器不能由定时任务替人决定），别在卡上承诺会收
            lines.append(
                f"{expires} 到期，到期前会提醒，不会自动释放。"
                if kind == "resource"
                else f"{expires} 到期，到期自动收回。"
            )
        if kind == "account":
            tpl = ticket.get("template") or {}
            lines.append(
                "子账号已建好。控制台初始密码请在 7 天内到平台领取。"
                if tpl.get("console_login")
                else "子账号已建好。"
            )
        if kind == "credential":
            # 这张卡会进飞书的消息列表，比审批实例好转发得多，所以刻意不带任何
            # 凭证内容，连查看地址都不带 —— 地址就是凭证
            lines.append("查看凭证的地址已发在对应飞书审批的评论里。")
        return "green", f"已开通：{title}", lines or ["已开通。"]
    if event == "fulfilling":
        return (
            "blue",
            f"审批已通过：{title}",
            ["审批通过了。这类资源由管理员按流程开通，开通后会再通知你。"],
        )
    if event == "failed":
        return (
            "red",
            f"开通失败：{title}",
            ["审批已通过，但开通没有成功。管理员会处理，处理好后会再通知你。"],
        )
    if event == "rejected":
        return (
            "orange",
            f"审批未通过：{title}",
            ["可以在飞书审批里查看审批意见，调整后重新申请。"],
        )
    if event == "withdrawn":
        return "grey", f"审批已撤销：{title}", ["这张申请不会开通。需要的话请重新申请。"]
    if event == "expiring":
        now = time.time() if now is None else now
        left = max(1, int((float(ticket.get("expires_at_ts") or now) - now + 86399) // 86400))
        expires = _when(ticket.get("expires_at"))
        return (
            "orange",
            f"即将到期：{title}",
            [
                (
                    f"{left} 天后（{expires}）到期，到期自动收回。"
                    if expires
                    else f"{left} 天后到期。"
                ),
                "需要继续用请在到期前续期。",
            ],
        )
    if event == "revoked":
        return (
            "grey",
            f"已到期收回：{title}",
            ["权限已按申请时的期限收回。需要继续用请重新申请。"],
        )
    raise NotifyError(f"不认识的通知类型 {event!r}")


def build_card(event: str, ticket: Mapping, *, base_url: str, now: Optional[float] = None) -> dict:
    color, title, lines = message(event, ticket, now=now)
    elements: list = [
        {"tag": "div", "text": {"tag": "plain_text", "content": _clip(line, _LINE_MAX)}}
        for line in lines[:4]
    ]
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"申请单 {_clip(ticket.get('id'), 40)}"}],
        }
    )
    link = request_link(base_url, str(ticket.get("id") or ""))
    if link:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "查看申请"},
                        "url": link,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def _log(text: str) -> None:
    print(f"[notify] {text}", file=sys.stderr)


def _failures(ticket: Mapping) -> int:
    return sum(1 for e in ticket.get("events") or [] if e.get("event") == "execute_failed")


class FeishuNotifier:
    """给申请人发应用机器人消息。"""

    def __init__(
        self,
        token: Callable[[], str],
        base_url: str,
        *,
        transport: Optional[Transport] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._token = token
        self.base_url = base_url
        self._transport = transport or _http
        self._clock = clock

    reaches_applicant = True

    def send(self, open_id: str, card: dict, *, by_user_id: bool = False) -> None:
        token = self._token()
        body = {
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        try:
            data = self._transport("POST", API_BY_USER_ID if by_user_id else API, token, body)
        except NotifyError:
            raise
        except Exception as exc:  # noqa: BLE001 — 不带原始异常文本：可能带着请求头
            raise NotifyError(f"飞书消息发送失败：{type(exc).__name__}") from None
        code = data.get("code") if isinstance(data, dict) else None
        if type(code) is not int or code != 0:
            msg = str((data or {}).get("msg") or "") if isinstance(data, dict) else ""
            if token:
                msg = msg.replace(token, "***")
            raise NotifyError(f"飞书拒绝发送消息：code={code} {msg[:100]}")

    def __call__(self, event: str, ticket: dict) -> None:
        applicant = ticket.get("applicant") or {}
        open_id = str(applicant.get("open_id") or "")
        user_id = str(applicant.get("user_id") or "")
        if not open_id and not user_id:
            _log(f"{ticket.get('id')} {event}：申请人没有 open_id / user_id，跳过")
            return
        if event == "failed" and _failures(ticket) > 1:
            # 管理员重试又失败：员工已经知道「管理员会处理」，不再重复打扰（管理员群照发）
            return
        card = build_card(event, ticket, base_url=self.base_url, now=self._clock())
        self.send(open_id or user_id, card, by_user_id=not open_id)


class AdminAlert:
    """开通失败时发到管理员群（alerts.py 的签名机器人）。"""

    reaches_applicant = False

    def __init__(
        self,
        webhook: str,
        secret: str,
        base_url: str = "",
        *,
        send: Callable[..., None] = alerts.send_feishu,
    ):
        self._webhook = webhook
        self._secret = secret
        self.base_url = base_url
        self._send = send

    def __call__(self, event: str, ticket: dict) -> None:
        if event != "failed":
            return
        note = next(
            (
                e.get("note")
                for e in reversed(ticket.get("events") or [])
                if e.get("event") == "execute_failed"
            ),
            "",
        )
        applicant = ticket.get("applicant") or {}
        who = applicant.get("name") or applicant.get("email") or ""
        lines = [
            f"申请 {ticket.get('id')} 开通失败",
            f"{ticket_title(ticket)}（{who}）" if who else ticket_title(ticket),
            f"原因：{_clip(note, _LINE_MAX)}" if note else "",
            request_link(self.base_url, str(ticket.get("id") or ""), admin=True),
        ]
        # 自定义机器人的文本会解析 <at user_id="all"> 标签：外部文本里的尖括号换成全角
        text = "\n".join(x for x in lines if x).replace("<", "＜").replace(">", "＞")
        self._send(text, webhook=self._webhook, secret=self._secret)


class NullNotifier:
    reaches_applicant = False

    def __call__(self, event: str, ticket: dict) -> None:
        return None


class RecordingNotifier:
    """测试用：记录每次通知。"""

    reaches_applicant = True

    def __init__(self, base_url: str = "https://panel.example.com"):
        self.base_url = base_url
        self.sent: list = []

    def __call__(self, event: str, ticket: dict) -> None:
        card = build_card(event, ticket, base_url=self.base_url)
        self.sent.append((event, ticket.get("id"), card))

    def events(self) -> list:
        return [e for e, _, _ in self.sent]


def combine(*targets: Optional[Notify]) -> Optional[Notify]:
    """多个通知目标：一个发失败不影响其他，全部发完后如有失败再抛第一个错误。"""
    active = [x for x in targets if x is not None]
    if not active:
        return None

    def notify(event: str, ticket: dict) -> None:
        first: Optional[BaseException] = None
        for target in active:
            try:
                target(event, ticket)
            except Exception as exc:  # noqa: BLE001
                first = first or exc
        if first is not None:
            raise first

    # 到期提醒只有真能发到申请人时才记「已提醒」
    notify.reaches_applicant = any(getattr(x, "reaches_applicant", True) for x in active)
    return notify


def from_env(
    environ: Optional[Mapping] = None, *, token: Optional[Callable[[], str]]
) -> Optional[Notify]:
    """DELIVERY_NOTIFY=1 才开启。

    申请人消息要飞书应用凭证（token）和 DELIVERY_BASE_URL；管理员告警要 DELIVERY_ALERT_WEBHOOK。
    缺哪样就只开另一样，两样都缺返回 None。
    """
    env = os.environ if environ is None else environ
    if env.get(ENV_NOTIFY, "") != "1":
        return None
    base_url = safe_base_url(env.get(ENV_BASE_URL, ""))
    user = FeishuNotifier(token, base_url) if token is not None and base_url else None
    if user is None:
        _log(f"{ENV_NOTIFY}=1 但缺飞书应用凭证或 {ENV_BASE_URL}（需 https），不给申请人发消息")
    conf = alerts.from_env(env)
    admin = AdminAlert(conf[0], conf[1], base_url) if conf else None
    return combine(user, admin)
