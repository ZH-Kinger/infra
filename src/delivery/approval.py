"""飞书审批：发起、查询、校验。

**开通之前的唯一放行条件是 `verify()` 通过**（docs/cloud-access-platform.md 规则 R1）：
实时调飞书接口取审批实例，逐项核对

  · 状态是 APPROVED
  · 审批定义编号是我们配置的那一个（防止拿别的审批流里通过的实例来冒充）
  · 发起人就是申请人（open_id 或 user_id）
  · 表单里的申请单号就是这张申请单（防止一个审批实例被套用到另一张单子上）
  · 实例没有被撤销（reverted：通过后又被撤销的单据，status 可能仍是 APPROVED）
  · 至少有一位申请人以外的审批人点了通过（task_list 里 APPROVED 的任务）：只有本人或自动通过的
    不算，allow_self_approval=true 才放开。只算 APPROVED 不算 DONE（或签里别人批了，其余是 DONE）

飞书回调、本地保存的状态都只当「该去查一次了」的提示，不当结论。

配置 `identity/approval.json`（gitignored）::

    {"approval_code": "<审批定义编号>",
     "widgets": {"ticket_id": "<控件ID>", "kind": "<控件ID>",
                 "summary": "<控件ID>", "reason": "<控件ID>"},
     "instance_url": "https://.../{instance_code}",          # 可选
     "instance_url_mobile": "https://.../{instance_code}",   # 可选
     "allow_self_approval": false}                           # 可选，默认 false

申请单详情页「去飞书查看审批」用飞书 AppLink 打开审批实例（飞书客户端 7.3.0 起支持，
官方文档「打开审批页面」）。默认用飞书审批应用的 PC 与移动端链接；Lark 国际版等情况可以在
approval.json 里用 instance_url / instance_url_mobile 覆盖，`{instance_code}` 会换成审批实例编号。

控件 ID 在飞书审批后台建好表单后，用 `delivery approval widgets` 查（调查询审批定义接口）。
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .errors import DeliveryError

API = "https://open.feishu.cn/open-apis"
STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_CANCELED = "CANCELED"
STATUS_DELETED = "DELETED"
#: 本地用：通过后又被撤销（实例 reverted=true）
STATUS_REVERTED = "REVERTED"
FINAL_NEGATIVE = (STATUS_REJECTED, STATUS_CANCELED, STATUS_DELETED)
WIDGET_KEYS = ("ticket_id", "kind", "summary", "reason")
#: `_approval_fields` 会按申请类型产出的补充字段。定义里没有的键**静默跳过** ——
#: 这是设计（加一个生效一个），代价是「名字拼错」和「定义还没加」长得一模一样。
#: 所以 `delivery approval widgets` 要把缺哪些报出来，否则功能没落地也看不出来
EXTRA_KEYS = (
    "account",
    "subject",
    "scope",
    "caps",
    "valid",
    "cloud_user",
    "project",
    "env",
    "spec",
    "cost",
    "until",
)
#: 飞书 AppLink：打开审批实例详情（PC 端 / 移动端）。path 参数是 URL 编码过的小程序页面路径
DEFAULT_INSTANCE_URL = (
    "https://applink.feishu.cn/client/mini_program/open?appId=cli_9cb844403dbb9108"
    "&mode=appCenter&path=pc%2Fpages%2Fin-process%2Findex%3FinstanceId%3D{instance_code}"
)
DEFAULT_INSTANCE_URL_MOBILE = (
    "https://applink.feishu.cn/client/mini_program/open?appId=cli_9cb844403dbb9108"
    "&path=pages%2Fdetail%2Findex%3FinstanceId%3D{instance_code}"
)
#: 审批实例编号只接受这几类字符：编号在链接里处于已编码的 path 参数中，
#: 限定字符集就不存在要不要二次编码的问题
_INSTANCE_CODE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TIMEOUT = 15
_MAX_BODY = 1024 * 1024

#: (method, url, token, body) -> 解析后的 JSON
Transport = Callable[[str, str, str, Optional[dict]], dict]


class ApprovalError(DeliveryError):
    """审批接口出错，或审批实例校验不通过。"""


#: 审批控件的类型 → 提交实例时 value 的写法。
#:
#: **飞书按控件类型校验 value 的形状，写错整张单子发不出去**（不是某个字段留空，是
#: 整个接口报错）。所以类型不是可有可无的元数据，配置里必须记着，发起时按它分派。
#: 单选/多选提交的是**选项的 value**，不是显示文案 —— 显示文案改了不影响已发出的单子。
WIDGET_TEXT = ("input", "textarea")
#: 单选。提交的是**选项 key**（读回来却是显示文案，别把读到的当成 key 回填）。
#: `radio` 是旧名，后台人工建的控件可能回这个 —— 漏了它就会撞上下面那条 fail-closed
WIDGET_PICK = ("radioV2", "radio")
#: 多选。value 是**字符串数组**，不是逗号串。`checkbox` 同样是旧名
WIDGET_MULTI = ("checkboxV2", "checkbox")
#: 日期。必须是 RFC3339 带时区，`2026-09-18` 这种裸日期飞书不收
WIDGET_DATE = ("date",)


def _wid(widget: object, default: str = "input") -> tuple:
    """控件配置 → `(id, type)`。

    两种写法都认：`"widget123"`（历史配置，类型用 `default`）和 `{"id":…, "type":…}`。
    `ApprovalConfig` 可以被直接构造（测试就是这么用的），所以归一化不能只放在 `load()` 里。

    `default` 存在的原因：四个必填控件的类型是建定义时就定死的（摘要和理由是多行文本），
    历史配置里只记了 id。不给默认值的话，升级后这两个字段会被当成单行文本发出去，
    而飞书按控件类型校验 —— 拒的是整张单子，等于所有人都提不了申请。
    """
    if isinstance(widget, Mapping):
        return str(widget.get("id") or ""), str(widget.get("type") or default)
    return str(widget or ""), default


def _field(widget: object, value: str, *, default: str = "input") -> dict:
    """把一个值装成飞书要的控件数据。

    **形状由控件类型决定**：单行/多行文本是字符串，单选是**选项的 value**（不是显示文案），
    多选是数组。发错形状飞书拒的是整张单子，不是这一个字段 —— 所以宁可在这里显式列全，
    也不要用一个 "input" 蒙混过去。没见过的类型按文本发，并把类型原样带上，
    让飞书的报错指向真正的问题控件。
    """
    wid, wtype = _wid(widget, default)
    if wtype in WIDGET_MULTI:
        # 多选收的是数组，这里按「、」拆回去。
        #
        # **前提：值必须是本模块用「、」拼出来的一串选项 key**。自由文本走这条路会被
        # 静默拆错（「甲、乙项目」→ ["甲", "乙项目"]），而 `_approval_fields` 的 caps
        # 现在是用「_」拼的（list_download），配成多选也匹配不到任何选项 ——
        # 所以**权限那一栏必须建成单选**。改成多选的话，拼接端要一起改。
        return {"id": wid, "type": wtype, "value": [v for v in value.split("、") if v]}
    if wtype in WIDGET_PICK:
        # 单选收的是**选项 key**（`prod` / `algo` / `resource`），不是显示文案。
        # `_approval_fields` 已经按这个约定产出，这里原样发
        return {"id": wid, "type": wtype, "value": value}
    if wtype in WIDGET_DATE:
        return {"id": wid, "type": wtype, "value": _rfc3339(value)}
    if wtype not in WIDGET_TEXT:
        # fail-closed：宁可在配置的时候就炸，也别让每张单子到飞书那边才被拒 ——
        # 那时的报错指向控件，查不到这里。dateInterval / amount / fieldList 的 value
        # 都不是字符串，按文本发一定失败
        raise ApprovalError(f"审批控件类型 {wtype} 还不支持，先在 approval.py 里接上再用")
    return {"id": wid, "type": wtype, "value": value}


def _rfc3339(value: str) -> str:
    """`2026-12-16` → `2026-12-16T00:00:00+08:00`。

    飞书的日期控件**只收带时区的 RFC3339**，裸日期串直接报参数错误、整张单子发不出去。
    已经带时间的原样放行（调用方给什么就是什么，这里不猜它想表达哪个时区）。
    """
    text = value.strip()
    if not text or "T" in text:
        return text
    return f"{text}T00:00:00+08:00"


#: 四个必填控件的类型是建定义时定死的。历史配置只记了 id，补默认值时必须按键区分 ——
#: 摘要和理由是多行文本，当成单行文本发出去会被飞书拒掉**整张单子**。
#: 这个默认值必须在这里生效（load 的归一化处），放到下游 `_field` 就是死代码：
#: 那时 type 已经被填成 "input" 了，`or default` 永远轮不到。
_DEFAULT_TYPES = {"summary": "textarea", "reason": "textarea"}


def _widget(key: str, raw: object) -> dict:
    """一个控件的配置。

    两种写法：`"widget123"`（历史写法，类型按 `_DEFAULT_TYPES`）、`{"id":…, "type":…}`。
    """
    if isinstance(raw, str):
        return {"id": raw, "type": _DEFAULT_TYPES.get(key, "input")} if raw else _bad(key)
    if not isinstance(raw, Mapping):
        return _bad(key)
    wid, wtype = raw.get("id"), raw.get("type") or _DEFAULT_TYPES.get(key, "input")
    if not isinstance(wid, str) or not wid or not isinstance(wtype, str) or not wtype:
        return _bad(key)
    return {"id": wid, "type": wtype}


def _bad(key: str) -> dict:
    raise ApprovalError(f'审批配置的 widgets.{key} 必须是控件 id，或 {{"id": …, "type": …}}')


@dataclass(frozen=True)
class ApprovalConfig:
    approval_code: str
    widgets: Mapping
    instance_url: str = ""
    instance_url_mobile: str = ""
    #: 默认要求至少有一位**不是申请人**的审批人点了通过；审批定义里只有申请人自己或自动通过时不开通
    allow_self_approval: bool = False
    #: 发凭证评论用的身份。飞书审批的评论接口 `user_id` 是必填的、**没有"以应用名义发"的选项**，
    #: 所以凭证评论必然挂在某个自然人名下。这个 open_id 必须是**面板这个飞书应用下的**——
    #: open_id 按应用隔离，拿别的应用的 open_id 过来会回 `open_id cross app`。
    comment_open_id: str = ""

    @classmethod
    def load(cls, path: Optional[str]) -> Optional[ApprovalConfig]:
        if not path or not Path(path).exists():
            return None
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApprovalError(f"读不了审批配置 {path}：{exc}") from exc
        if not isinstance(data, dict):
            raise ApprovalError("审批配置必须是对象")
        code = data.get("approval_code")
        widgets = data.get("widgets")
        if not isinstance(code, str) or not code.strip():
            raise ApprovalError("审批配置缺 approval_code")
        if not isinstance(widgets, dict):
            raise ApprovalError("审批配置的 widgets 必须是对象")
        widgets = {k: _widget(k, v) for k, v in widgets.items()}
        if any(k not in widgets for k in WIDGET_KEYS):
            raise ApprovalError(f"审批配置的 widgets 必须包含 {', '.join(WIDGET_KEYS)}")
        urls = {}
        for key in ("instance_url", "instance_url_mobile"):
            url = data.get(key, "")
            if url and not valid_instance_url(url):
                raise ApprovalError(f"审批配置的 {key} 必须是 https 地址，且包含 {{instance_code}}")
            urls[key] = url or ""
        self_ok = data.get("allow_self_approval", False)
        if not isinstance(self_ok, bool):
            raise ApprovalError("审批配置的 allow_self_approval 必须是 true / false")
        commenter = data.get("comment_open_id", "")
        if not isinstance(commenter, str) or (commenter and not commenter.startswith("ou_")):
            raise ApprovalError("审批配置的 comment_open_id 必须是 ou_ 开头的 open_id")
        return cls(
            approval_code=code.strip(),
            widgets=dict(widgets),
            allow_self_approval=self_ok,
            comment_open_id=commenter.strip(),
            **urls,
        )


def valid_instance_url(template: object) -> bool:
    if not isinstance(template, str) or "{instance_code}" not in template:
        return False
    parsed = urllib.parse.urlsplit(template.replace("{instance_code}", "x"))
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and not any(c in template for c in ('"', "'", "<", ">", " ", "\\"))
    )


def instance_links(config: Optional[ApprovalConfig], instance_code: object) -> dict:
    """跳到飞书审批实例的链接 {"pc": ..., "mobile": ...}；编号为空或不合规时为空串。"""
    code = str(instance_code or "")
    out = {"pc": "", "mobile": ""}
    if not _INSTANCE_CODE.match(code):
        return out
    pairs = (
        ("pc", (config.instance_url if config else "") or DEFAULT_INSTANCE_URL),
        ("mobile", (config.instance_url_mobile if config else "") or DEFAULT_INSTANCE_URL_MOBILE),
    )
    for key, template in pairs:
        if valid_instance_url(template):
            out[key] = template.replace("{instance_code}", code)
    return out


@dataclass(frozen=True)
class Applicant:
    union_id: str
    name: str
    open_id: str = ""
    user_id: str = ""

    def id_fields(self) -> dict:
        if self.open_id:
            return {"open_id": self.open_id}
        if self.user_id:
            return {"user_id": self.user_id}
        raise ApprovalError(
            "登录信息里没有飞书 open_id / user_id，无法以你的身份发起飞书审批。"
            "公司 IAM 登录时需要 wuji scope 返回 feishu_user_id"
        )


def _http(method: str, url: str, token: str, body: Optional[dict]) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # url 由本模块用固定前缀拼接
    req = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            raw = resp.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        raw = exc.read(_MAX_BODY)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise ApprovalError(f"连不上飞书审批接口：{type(exc).__name__}") from None
    try:
        parsed = json.loads(raw.decode() or "{}")
    except ValueError:
        raise ApprovalError("飞书审批接口返回的不是 JSON") from None
    if not isinstance(parsed, dict):
        raise ApprovalError("飞书审批接口返回格式不对")
    return parsed


def _data(resp: dict, what: str) -> dict:
    if resp.get("code") != 0:
        raise ApprovalError(
            f"{what}失败：code={resp.get('code')} {str(resp.get('msg') or '')[:120]}"
        )
    data = resp.get("data")
    if not isinstance(data, dict):
        raise ApprovalError(f"{what}失败：响应缺 data")
    return data


class SelfApprovalError(ApprovalError):
    """审批「通过」了，但没有申请人以外的审批人同意。这是确定的结论，不是查询失败。"""


class FeishuApproval:
    def __init__(
        self,
        config: ApprovalConfig,
        token: Callable[[], str],
        *,
        transport: Transport = _http,
    ):
        self.config = config
        self._token = token
        self._send = transport

    def create(
        self,
        *,
        ticket_id: str,
        kind: str,
        summary: str,
        reason: str,
        applicant: Applicant,
        extra: Optional[Mapping] = None,
    ) -> str:
        """发起审批实例。

        `kind` 送的是申请类型的**内部标识**（`credential` / `resource` …），不是中文。
        审批定义里这一栏是单选控件，单选提交的是**选项 key**，中文由飞书按选项渲染。
        送中文过去会匹配不到任何选项 —— 而条件分支正是按它分流的。

        `extra` 是按类型补充的字段（使用方、访问范围、规格、成本归属……），
        键是审批定义里的 **custom_id**。审批定义里没有的键**静默跳过** ——
        新字段还没加进定义时，不该让整张单子发不出去。

        为什么按 custom_id 而不是 widget id：飞书会自己生成 widget id，
        而且**管理员在后台改一次表单 id 就会漂移**。custom_id 是稳定别名。
        """
        w = self.config.widgets
        form = [
            _field(w["ticket_id"], ticket_id),
            _field(w["kind"], kind),
            _field(w["summary"], summary[:2000], default="textarea"),
            _field(w["reason"], reason[:2000], default="textarea"),
        ]
        for key, value in (extra or {}).items():
            # 撞上四个必填控件就当场拒发，不是跳过：撞名会让 form 里出现两条同 id 的控件，
            # 审批人在同一栏看到两个值、其中一个来自申请人。`ticket_id` 撞名会被
            # `_form_value` 的「只能有一条」挡下（碰巧安全），summary/reason 没这层保护
            if key in WIDGET_KEYS:
                raise ApprovalError(f"审批字段 {key} 和必填控件重名，不能发起")
            widget = w.get(key)
            text = str(value or "").strip()
            if not widget or not text:
                continue
            form.append(_field(widget, text[:500]))
        body = {
            "approval_code": self.config.approval_code,
            "form": json.dumps(form, ensure_ascii=False),
            # 申请单号当幂等键：同一张单子重复提交只会有一个审批实例
            "uuid": ticket_id,
            **applicant.id_fields(),
        }
        data = _data(
            self._send("POST", f"{API}/approval/v4/instances", self._token(), body), "发起飞书审批"
        )
        code = data.get("instance_code")
        if not isinstance(code, str) or not code:
            raise ApprovalError("发起飞书审批失败：没有返回 instance_code")
        return code

    def fetch(self, instance_code: str) -> dict:
        url = f"{API}/approval/v4/instances/{urllib.parse.quote(instance_code, safe='')}"
        return _data(self._send("GET", url, self._token(), None), "查询飞书审批")

    def cancel(self, instance_code: str, applicant: Applicant) -> None:
        ids = applicant.id_fields()
        id_type = "open_id" if "open_id" in ids else "user_id"
        body = {
            "approval_code": self.config.approval_code,
            "instance_code": instance_code,
            "user_id": next(iter(ids.values())),
        }
        url = f"{API}/approval/v4/instances/cancel?user_id_type={id_type}"
        _data(self._send("POST", url, self._token(), body), "撤回飞书审批")

    def status(self, *, instance_code: str, ticket_id: str, applicant: Applicant) -> str:
        """实时查询并核对，返回审批状态。实例和申请单对不上直接报错。"""
        return self._checked(instance_code, ticket_id, applicant)[0]

    def _checked(self, instance_code: str, ticket_id: str, applicant: Applicant) -> tuple:
        """(状态, 实例数据)。实例数据只在本次调用里传递：面板多线程共用这个对象，不能存成属性。"""
        data = self.fetch(instance_code)
        # 只认当前配置的这一条定义。
        #
        # 曾经有过一个 `also_accept`，想让切换定义时在途的单子仍能核对通过。它是假的：
        # 放宽了 code，下一道门却拿**当前**配置的 widget id 去表单里取单号，而新建定义
        # 的 widget id 是飞书重新生成的 —— 在途单子照样全拒，只是报错变成「单号不一致」，
        # 把人引去查工单。切换前把在途单子清空才是真办法（那次切换时线上正好是 0 张）。
        if data.get("approval_code") != self.config.approval_code:
            raise ApprovalError("审批实例不属于配置的审批定义，拒绝")
        if applicant.open_id:
            if data.get("open_id") != applicant.open_id:
                raise ApprovalError("审批发起人不是申请人，拒绝")
        elif data.get("user_id") != applicant.user_id or not applicant.user_id:
            raise ApprovalError("审批发起人不是申请人，拒绝")
        if _form_value(data.get("form"), _wid(self.config.widgets["ticket_id"])[0]) != ticket_id:
            raise ApprovalError("审批表单里的申请单号和这张申请单不一致，拒绝")
        status = data.get("status")
        if status not in (STATUS_PENDING, STATUS_APPROVED, *FINAL_NEGATIVE):
            raise ApprovalError(f"未知的审批状态 {status!r}")
        if data.get("reverted"):
            return STATUS_REVERTED, data
        return status, data

    def verify_approved(self, *, instance_code: str, ticket_id: str, applicant: Applicant) -> None:
        """开通前调用。任何一项不满足都抛错。"""
        status, data = self._checked(instance_code, ticket_id, applicant)
        if status != STATUS_APPROVED:
            raise ApprovalError(f"飞书审批状态是 {status}，不是已通过，拒绝开通")
        if not self.config.allow_self_approval and not _approved_by_other(data, applicant):
            raise SelfApprovalError(
                "审批没有经过申请人以外的审批人同意（只有本人或自动通过），拒绝开通。"
                "请检查飞书审批定义的审批人设置"
            )

    def comment(self, instance_code: str, text: str) -> None:
        """把凭证贴到审批实例的评论里。**这是凭证的唯一出口**。

        为什么不回写面板：secret 一旦进了面板的工单或日志，就多一处要防守的地方，而审批实例
        本身已经是这次发放的权威记录——谁申请、谁批准、发了什么，在同一个地方对齐。面板侧
        只留 AccessKeyId，**绝不存 secret**。

        代价（明知故选）：评论对所有能看到这张审批单的人可见，包括审批人和抄送人。
        """
        if not self.config.comment_open_id:
            raise ApprovalError(
                "没有配置 comment_open_id，凭证发不出去。在 identity/approval.json 里填一个"
                "本应用下的 open_id（飞书审批的评论必须挂在某个人名下，没有以应用名义发的选项）"
            )
        url = (
            f"{API}/approval/v4/instances/{urllib.parse.quote(instance_code, safe='')}"
            "/comments?user_id_type=open_id&user_id="
            f"{urllib.parse.quote(self.config.comment_open_id, safe='')}"
        )
        # content 不是纯文本：飞书要的是 `{"text": "..."}` **序列化后的 JSON 字符串**。
        # 直接传纯文本回 60001 content invalid，传字典回 9499 Invalid parameter type。
        body = {"content": json.dumps({"text": text}, ensure_ascii=False)}
        _data(self._send("POST", url, self._token(), body), "发送凭证评论")

    def widget_map(self, approval_code: str) -> dict:
        """`{custom_id: widget_id}`。

        飞书自己生成 widget id，而且**管理员在后台改一次表单 id 就会漂移**；
        `custom_id` 是建定义时指定的稳定别名，不会变。所以配置里记的是这张对照表，
        由 `delivery approval widgets --code <定义> --write <approval.json>` 生成，不用人手抄。
        **改过表单之后必须重跑**：飞书会重新生成 widget id，不同步配置的话面板会拿旧 id
        去表单里取单号，取到空串 → 所有审批都被判「申请单号不一致」。
        """
        return {
            str(x["custom_id"]): str(x["id"])
            for x in self.widgets(approval_code)
            if x.get("custom_id") and x.get("id")
        }

    def widgets(self, approval_code: str) -> list:
        """管理员配置用：列出审批定义里的表单控件（id、类型、名称、custom_id）。"""
        url = f"{API}/approval/v4/approvals/{urllib.parse.quote(approval_code, safe='')}"
        data = _data(self._send("GET", url, self._token(), None), "查询审批定义")
        try:
            form = json.loads(data.get("form") or "[]")
        except ValueError:
            raise ApprovalError("审批定义的表单不是 JSON") from None
        return [
            {
                "id": str(x.get("id") or ""),
                "type": str(x.get("type") or ""),
                "name": str(x.get("name") or ""),
                "custom_id": str(x.get("custom_id") or ""),
            }
            for x in form
            if isinstance(x, dict)
        ]


def _approved_by_other(data: object, applicant: Applicant) -> bool:
    """task_list 里至少有一条「通过」的任务，审批人有身份且不是申请人。

    自动通过的任务没有审批人 open_id / user_id，不算。
    """
    # 按申请人实际有的那种身份比：飞书登录只有 open_id，公司 IAM 登录只有 user_id。
    # 审批任务缺这种身份就认不出是不是本人，不算数（宁可不开通）。
    key, mine = (
        ("open_id", applicant.open_id) if applicant.open_id else ("user_id", applicant.user_id)
    )
    if not mine:
        return False
    tasks = data.get("task_list") if isinstance(data, dict) else None
    for task in tasks if isinstance(tasks, list) else []:
        if not isinstance(task, dict) or task.get("status") != "APPROVED":
            continue
        theirs = str(task.get(key) or "")
        if not theirs or theirs == mine:
            continue
        # 申请人两种身份都有时，另一种对上了也是本人
        other_key = "user_id" if key == "open_id" else "open_id"
        other_mine = getattr(applicant, other_key)
        if other_mine and str(task.get(other_key) or "") == other_mine:
            continue
        return True
    return False


#: **单选控件读写不对称**：提交时 value 是选项 key，`GET instances/{code}` 读回来的
#: value 是**渲染后的中文文案**，key 在同级 `option.key` 里。所以读到的 value 绝不能
#: 原样当成下次提交的值。今天只读 ticket_id（单行文本，不受影响），但这个函数是通用的 ——
#: 将来做「按审批表单回填」或「按 kind 分支」时会踩。
def _form_value(form: object, widget_id: str) -> str:
    try:
        items = json.loads(form) if isinstance(form, str) else form
    except ValueError:
        return ""
    if not isinstance(items, list):
        return ""
    hits = [x for x in items if isinstance(x, dict) and x.get("id") == widget_id]
    if len(hits) != 1:
        return ""
    value = hits[0].get("value")
    return value if isinstance(value, str) else ""
