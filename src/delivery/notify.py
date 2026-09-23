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
#: 允许的收件人标识类型。**白名单，不是拼接** —— 这一段直接进 URL 的查询串，
#: 放任意字符串进去等于让上游数据决定请求打到哪
_ID_TYPES = ("open_id", "user_id", "union_id")
_API_BASE = "https://open.feishu.cn/open-apis/im/v1/messages"
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


def _account_how(tpl: Mapping) -> str:
    """开完号之后，这个人到底该怎么登进去。

    **这句话是这张卡唯一有用的信息。** 原先写死成「初始密码请在 7 天内到平台领取」——
    那是密码时代的说法；开了企业 SSO 之后 RAM 密码登录全局失效，照着做只会拿到
    一串登不进去的东西，而他会以为是账号没建好。
    """
    from . import platforms as platforms_mod

    if not tpl.get("console_login"):
        return "子账号已建好。这个账号不开控制台登录，只能用访问凭证。"
    platform = str(tpl.get("platform") or "")
    account = str(tpl.get("account") or "")
    if platforms_mod.console_login_is_sso(platform, account):
        return "子账号已建好。**用公司账号登录，不需要密码** —— 在登录页选企业/SSO 登录。"
    return "子账号已建好。控制台初始密码请在 7 天内到平台领取。"


def console_link(ticket: Mapping) -> str:
    """这个云账号的控制台登录地址。拼不出来返回空串。

    地址模板只写在 `platforms.py` 一处 —— 在这儿再拼一遍的话，
    哪天某朵云换了登录域名，两处会不一致，而不一致的那一处没人会去看。
    """
    from . import platforms as platforms_mod

    tpl = ticket.get("template") or {}
    try:
        spec = platforms_mod.get(str(tpl.get("platform") or ""))
    except Exception:  # noqa: BLE001 — 不认识的平台就是没有地址，不该让整张卡发不出去
        return ""
    account = str(tpl.get("account") or "")
    return spec.login_url(account) if account else ""


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
            lines.append(_account_how(tpl))
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


#: 面板上「权限对账」那一页的地址。**只写一份。**
#: 前端 `app.js:parseHash` 只认 `admin/iam` 这一个串，写成别的（比如 `iam`）会掉到
#: 兜底分支、落在「我的」页面上 —— 而按钮看起来是好的，点了只是没到该到的地方。
#: 这个错刚发生过一次：两张卡片各写各的，一张对一张错。
IAM_PAGE = "#admin/iam"


def page_link(base_url: str, page: str = IAM_PAGE) -> str:
    """面板内页地址。base_url 不可用时返回空串 —— 调用方据此不加按钮。"""
    link = safe_base_url(base_url)
    return f"{link}/{page}" if link else ""


def reclaim_card(report: Mapping, *, base_url: str = "") -> dict:
    """离职回收的飞书交互卡片，发到管理员群。

    **删掉的和扣住的分两段、两种颜色，不合并计数。** 删掉的是已经发生的事（知会一声），
    扣住的是**要人去做的事**（两边不一致，得去问一句）。合成一句「本轮处理 3 人」的话，
    那件要人做的事就没人做了 —— 卡片的头色也按「有没有待办」定，不按「做了多少」。
    """
    done = list(report.get("done") or [])
    held = list(report.get("held") or [])
    failed = list(report.get("failed") or [])
    who = lambda r: f"{r.get('username', '')} {r.get('name', '')}".strip() or r.get("union_id", "")  # noqa: E731
    div = lambda md: {"tag": "div", "text": {"tag": "lark_md", "content": md}}  # noqa: E731

    elements: list = []
    if done:
        elements.append(div(f"**已回收 {len(done)} 人的云登录名**"))
        for r in done[:10]:
            elements.append(
                div(
                    f"· {who(r)}　`{r.get('app', '')}`　"
                    f"{_clip(r.get('previous') or r.get('value'), 60)}"
                )
            )
        if len(done) > 10:
            elements.append(div(f"　…… 还有 {len(done) - 10} 人"))
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "只删了公司 IAM 的属性。"
                        "云上的 RAM/IAM 账号还在，需要去控制台禁用。",
                    }
                ],
            }
        )
    if held:
        if elements:
            elements.append({"tag": "hr"})
        elements.append(div(f"**{len(held)} 人待确认**（IT 的 IAM 说已离职，我们名册里还有）"))
        for r in held[:10]:
            elements.append(div(f"· {who(r)}　`{r.get('app', '')}`　{_clip(r.get('value'), 60)}"))
        elements.append(
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "在面板上确认离职后回收，或等名册同步后下一轮自动处理。",
                    }
                ],
            }
        )
    if failed:
        elements.append({"tag": "hr"})
        elements.append(div(f"**{len(failed)} 条失败**"))
        for r in failed[:5]:
            elements.append(div(f"· {_clip(r.get('error'), 90)}"))
    if not elements:
        elements.append(div("没有要回收的。"))

    link = page_link(base_url)
    if link:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "去后台看"},
                        "url": link,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            # 有待办就橙色。**不按「回收了多少」定色** —— 全自动做完是常态，不需要显眼
            "template": "orange" if (held or failed) else ("blue" if done else "grey"),
            "title": {"tag": "plain_text", "content": "云账号离职回收"},
        },
        "elements": elements,
    }


def notify_admins(notifier, union_ids, card: dict) -> list:
    """把一张卡片私聊发给每个管理员。返回失败说明（空 = 全发出去了）。

    **按人分别 try。** 一个管理员的 open_id 失效（离职、退出企业）不该让其余人收不到 ——
    而通知的全部价值就在于「有人看到了」。
    """
    problems = []
    for uid in sorted({str(u or "").strip() for u in union_ids if str(u or "").strip()}):
        try:
            notifier.send(uid, card, id_type="union_id")
        except Exception as exc:  # noqa: BLE001 — 一个人发不到不挡其余
            problems.append(f"{uid[:12]}…：{type(exc).__name__}: {str(exc)[:80]}")
    return problems


#: 卡片分三类，**颜色是有语义的**：
#:   todo  待你处理的事（蓝）—— 系统没出问题，只是轮到人了。这类占绝大多数
#:   warn  要留意但不用立刻动手的（橙）
#:   alert 真出事了：任务挂了、权限没了、数据可能不对（红）
#: 什么都用红色的话，红色就不再意味着任何事 —— 收的人一周之后就不看了
_TEMPLATES = {"todo": "blue", "warn": "orange", "alert": "red", "done": "green", "mute": "grey"}


#: 一张卡最多摆几个人的按钮。再多只给链接 —— 卡片太长没人看得完，飞书对卡片体积也有限制
CARD_ROWS = 5


def card2(kind: str, title: str, elements: list) -> dict:
    """Card JSON 2.0 的外壳。按钮要能回调面板（`behaviors`），只有 2.0 支持。"""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _TEMPLATES.get(kind, "blue"),
            "title": {"tag": "plain_text", "content": _clip(title, 60)},
        },
        "body": {"elements": elements},
    }


def md(text: str) -> dict:
    return {"tag": "markdown", "content": str(text)}


def note(text: str) -> dict:
    """小灰字：补充说明、采集时间这类。正文和注脚分开，卡片才不是一堵墙。

    **2.0 里没有 `note` 组件**（真机试出来的：`tag: note` 会被回 200861，
    `plain_text` 当元素用回 200621），灰字只能靠 markdown 的 font 标签。
    """
    return md(f"<font color=grey>{_clip(text, _LINE_MAX)}</font>")


def link_button(label: str, url: str, kind: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def alert_card(title: str, text: str) -> dict:
    """定时任务出问题时私聊管理员的卡片。正文就是报告原文，一行一段。

    **报告可能很长**（「另有 N 条，见面板」之前能列十几行），卡片上截到 30 行 ——
    全文在 journal 里，卡片要做的只是让人知道出事了、大概是什么事。
    """
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    tail = ""
    if len(lines) > 24:
        tail, lines = f"还有 {len(lines) - 24} 行，见服务器日志", lines[:24]
    elements: list = [md(_clip(ln, _LINE_MAX)) for ln in lines or ["（没有更多信息）"]]
    if tail:
        elements.append(note(tail))
    return card2("alert", title, elements)


def drift_card(report: Mapping, *, base_url: str = "") -> dict:
    """对账发现「人走了但云登录名还挂着」时，私聊管理员。**只提醒，不回收。**

    为什么要有这个
    ──────────────
    回收本身是人点的（有确认按钮和「稍后处理」）—— 可原先**没有任何东西去提醒人来点**：
    `identity iam-reclaim` 只能手工跑，而那条飞书私聊只在 `--apply` 时才发。
    于是一个人离职之后，他的云登录名会一直挂着，直到某天有人恰好打开面板那一页。
    线上就有这么一条躺着，没人被通知过。

    **不在这里做回收**：删属性是不可逆的，而「他到底离没离职」的判据来自 IT 的 Authentik，
    接口抖一下就可能把在职的人判成离职。提醒的代价是多一条消息，误删的代价是人登不进去。
    """
    rows = [
        (e, d)
        for e in (report.get("apps") or ())
        for d in (e.get("drift") or ())
        if d.get("kind") == "inactive"
    ]
    lines = [
        f"{d.get('name') or d.get('username') or d.get('union_id', '')[:12]}"
        f"（{e.get('app', '')}）：{_clip(d.get('theirs'), 60)}"
        for e, d in rows[:8]
    ]
    if len(rows) > 8:
        lines.append(f"…还有 {len(rows) - 8} 人")
    lines.append("到面板「权限对账」确认回收，或先点「稍后处理」。")
    elements: list = [
        {"tag": "div", "text": {"tag": "plain_text", "content": line}} for line in lines
    ]
    link = page_link(base_url)
    if link:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "去确认"},
                        "url": link,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": f"待确认：{len(rows)} 人的云登录名还挂着"},
        },
        "elements": elements,
    }


def offboard_card(report: Mapping, *, base_url: str = "") -> dict:
    """自动停用的结果：停了谁、谁没停成、为什么一个都没停。**删号仍要人确认。**

    只有「没停成 / 被上限拦下」才是橙色 —— 正常停用是按计划做完的事，不该长得像事故。
    """
    from . import platforms

    def who(r):
        cloud = platforms.name_of(r.get("platform", ""))
        return f"{r.get('person', '')} · {cloud} {r.get('user', '')}"

    done = report.get("done") or []
    failed = report.get("failed") or []
    held = report.get("held") or []
    suspects = report.get("suspects") or []
    elements: list = []
    if done:
        elements.append(md("**已停用**（关登录、禁 AK，可以恢复）"))
        for r in done[:10]:
            elements.append(note(f"{who(r)}：{r.get('signal', '')}"))
    if suspects:
        elements.append(md("**待确认**（没有自动停用）"))
        for r in suspects[:10]:
            elements.append(note(f"{who(r)}：{r.get('signal', '')}"))
    if failed:
        elements.append(md("**没停成**，下一轮会再试"))
        for r in failed[:5]:
            elements.append(note(f"{who(r)}：{_clip(r.get('error'), 80)}"))
    if held:
        elements.append(md(f"**这一轮有 {len(held)} 人被判离职，超过上限，一个都没停**"))
        elements.append(note("、".join(held[:10]) + " —— 多半是接口出了问题，先核实。"))
    elements.append(note("删号要你在面板或卡片上确认。数据一律不动。"))
    link = page_link(base_url)
    if link:
        elements.append(_actions([link_button("去面板", link)]))
    kind = "warn" if (failed or held) else "todo"
    if failed or held:
        title = f"离职停号：{len(failed) + len(held)} 个没处理成"
    else:
        title = f"已停用 {len(done)} 个离职账号，待确认删除"
    return card2(kind, title, elements)


def pending_card(records, *, base_url: str = "", title: str = "") -> dict:
    """待确认的离职账号，**按钮直接能点**：确认删除 / 没离职。

    为什么按钮要带在卡片上
    ──────────────────────
    原先卡片只有一个「去确认」链接：人得打开面板、找到那一行、再点一次。一件五秒钟的事
    拆成三步，于是就拖着 —— 而拖着的那几天，离职的人的号一直开着。

    **这不是告警，是待办**（蓝色）。真出事的才是红色，见 `_TEMPLATES`。

    **1.0 和 2.0 不能混**：2.0 的按钮才能把 `behaviors.callback` 回调到面板（`/feishu/card`）。
    """
    from . import platforms

    # 同一个号可能两条都沾（先被弱信号记下、同一轮又被强信号停用）——
    # 不去重的话卡片上会出现两行、两对按钮，还占掉两个名额（审计 Low-2）。后写的赢
    seen: dict = {}
    for r in records:
        seen[f"{r.get('platform')}/{r.get('account')}/{r.get('user')}"] = r
    all_rows = list(seen.values())
    rows = all_rows[:CARD_ROWS]
    elements: list = [
        md("这些人的云账号还留着。确认删除只删账号本身，**他的数据一个字节都不动**。")
    ]
    for r in rows:
        cloud = platforms.name_of(r.get("platform", ""))
        key = f"{r.get('platform')}/{r.get('account')}/{r.get('user')}"
        by_hand = manual_platform(r.get("platform", ""))
        state = "已停用（面板停的）" if r.get("state") == "disabled" else "面板没停过它"
        elements.append({"tag": "hr"})
        elements.append(md(f"**{r.get('person') or r.get('user')}** · {cloud} `{r.get('user')}`"))
        elements.append(note(f"{state} · {_clip(r.get('signal'), 60)}"))
        if r.get("incomplete"):
            elements.append(note(f"上一轮停用没做完，下一轮会再试：{_clip(r['incomplete'], 60)}"))
        if r.get("unverified"):
            elements.append(note("名册里这个号不归他，面板不删。核实后去控制台处理。"))
            elements.append(_actions([_keep_button(key)]))
            continue
        elements.append(_actions([_delete_button(key, cloud, r, by_hand), _keep_button(key)]))
    if len(all_rows) > len(rows):
        elements.append({"tag": "hr"})
        elements.append(md(f"还有 {len(all_rows) - len(rows)} 个号，去面板处理。"))
    link = page_link(base_url)
    if link:
        elements.append(_actions([link_button("去面板看全部", link)]))
    return card2("todo", title or f"待确认：{len(all_rows)} 个离职账号", elements)


def manual_platform(platform: str) -> bool:
    """面板动不了的平台（九章）。按钮文案和确认语都不一样。"""
    from . import offboard

    return offboard.manual(str(platform or ""))


def _actions(buttons: list) -> dict:
    """一行按钮。

    **2.0 里没有 `action` 容器**（真机试出来的：`{"tag":"action","actions":[...]}` 回 200861）——
    按钮本身就是元素，要并排就用 `column_set` 摆。1.0 的写法照搬过来会让整张卡发不出去，
    而发不出去 = 管理员什么通知都收不到。
    """
    if len(buttons) == 1:
        return buttons[0]
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in buttons],
    }


def _delete_button(key: str, cloud: str, rec, by_hand: bool) -> dict:
    who = rec.get("user", "")
    ask = (
        f"{cloud}没有接口，面板停不了也删不了。确认你已经在{cloud}控制台处理了 {who}？"
        if by_hand
        else f"删除{cloud}账号 {who}？会删掉账号本身（出组、摘策略、删 AK）。"
        "他在桶里的文件、数据集、实例都不动。删了不能恢复。"
    )
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "我已在控制台处理" if by_hand else "确认删除"},
        "type": "danger",
        "behaviors": [{"type": "callback", "value": {"a": "del", "k": key}}],
        "confirm": {
            "title": {"tag": "plain_text", "content": "再确认一次"},
            "text": {"tag": "plain_text", "content": ask},
        },
    }


def _keep_button(key: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "没离职 / 恢复"},
        "type": "default",
        "behaviors": [{"type": "callback", "value": {"a": "keep", "k": key}}],
    }


def reclaim_text(report: Mapping, *, base_url: str = "") -> str:
    """离职回收的结果，发到管理员群（`alerts` 那个签名机器人，文本通道）。

    **删掉的和扣住的分两段写，不合并计数。** 删掉的是已经发生的事（知会一声），
    扣住的是**要人去做的事**（两边不一致，得问一句）。合成一句「本轮处理 3 人」的话，
    那件要人做的事就没人做了。
    """
    done = list(report.get("done") or [])
    held = list(report.get("held") or [])
    failed = list(report.get("failed") or [])
    who = lambda r: f"{r.get('username', '')} {r.get('name', '')}".strip() or r.get("union_id", "")  # noqa: E731
    lines = ["【云账号离职回收】"]
    if done:
        lines.append(f"已回收 {len(done)} 人的云登录名（属性已删，云上账号还在）：")
        lines += [
            f"  · {who(r)} {r.get('app', '')} {_clip(r.get('previous') or r.get('value'), 60)}"
            for r in done[:10]
        ]
        if len(done) > 10:
            lines.append(f"  …… 还有 {len(done) - 10} 人")
    if held:
        lines.append(f"{len(held)} 人待确认 —— IT 的 IAM 说已离职，但我们名册里还有，没动：")
        lines += [f"  · {who(r)} {r.get('app', '')} {_clip(r.get('value'), 60)}" for r in held[:10]]
        lines.append("  两边不一致时该去问一句，不是删。确认离职后名册会少掉他，下一轮自动回收。")
    if failed:
        lines.append(f"{len(failed)} 条失败：")
        lines += [f"  · {_clip(r.get('error'), 90)}" for r in failed[:5]]
    if done:
        lines.append("云上的 RAM/IAM 用户还在 —— 禁用或删除账号不自动做。")
    if base_url:
        lines.append(f"详情：{page_link(base_url)}")
    return "\n".join(lines)


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
    actions = []
    # 开完号的那张卡把控制台地址放上去。**这是他最需要的一个东西**，
    # 而原先整张卡里一个链接都没有 —— 人得自己去问「在哪登」
    if event == "done" and ticket.get("kind") == "account":
        console = console_link(ticket)
        if console:
            actions.append(
                {
                    "tag": "button",
                    "type": "primary",
                    "text": {"tag": "plain_text", "content": "去登录控制台"},
                    "url": console,
                }
            )
    link = request_link(base_url, str(ticket.get("id") or ""))
    if link:
        actions.append(
            {
                "tag": "button",
                "type": "default" if actions else "primary",
                "text": {"tag": "plain_text", "content": "查看申请"},
                "url": link,
            }
        )
    if actions:
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": color, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


#: 搬运要管理员出手的两种情况 → (卡片颜色, 标题前缀, 该做什么)
_MOVE_REASON = {
    "review": ("orange", "搬运等确认", "确认体积没问题后放行；量不出来的多半是源目录读不到。"),
    "failed": ("red", "搬运失败", "看一眼原因，决定重试还是关掉这张单。"),
    # 「这一轮没跑成」——多半是配置或网络，搬运本身还没有结论。
    # 和 failed 分开是因为它俩的处置不同：这个等下一轮自己好，那个要人决定
    "error": ("orange", "搬运这轮没跑成", "多半是配置或网络。下一轮会再试；一直不好就看一眼原因。"),
}


def move_card(ticket: Mapping, stage: str, *, base_url: str = "") -> dict:
    """搬运卡在人这一步时，私聊管理员。

    **只发给管理员，不发申请人。** 申请人确认不了体积、也重试不了，
    给他一条「等确认」只是让他来问一句「还要多久」。

    **也不发群。** 这是要人去做的事 —— 发群等于发给没有人。
    """
    color, what, todo = _MOVE_REASON.get(stage, _MOVE_REASON["failed"])
    payload = ticket.get("payload") or {}
    lines = [
        f"{_clip(payload.get('source'), 120)}",
        f"→ {_clip(payload.get('dest'), 120)}",
    ]
    why = str(ticket.get("move_error") or "").strip()
    if why:
        lines.append(why[:_LINE_MAX])
    lines.append(todo)
    elements: list = [
        {"tag": "div", "text": {"tag": "plain_text", "content": line}} for line in lines
    ]
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"申请单 {_clip(ticket.get('id'), 40)}"}],
        }
    )
    link = request_link(base_url, str(ticket.get("id") or ""), admin=True)
    if link:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {"tag": "plain_text", "content": "去处理"},
                        "url": link,
                    }
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"{what}：{ticket_title(ticket)}"},
        },
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

    def send(
        self, open_id: str, card: dict, *, by_user_id: bool = False, id_type: str = ""
    ) -> None:
        """给一个人发卡片。`id_type` 支持 open_id / user_id / union_id。

        名册里**只有 union_id**（没有 open_id），所以给管理员发通知走 union_id 那条。
        """
        kind = id_type or ("user_id" if by_user_id else "open_id")
        if kind not in _ID_TYPES:
            raise NotifyError(f"不支持的收件人标识类型 {kind!r}")
        token = self._token()
        body = {
            "receive_id": open_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        try:
            data = self._transport("POST", f"{_API_BASE}?receive_id_type={kind}", token, body)
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
