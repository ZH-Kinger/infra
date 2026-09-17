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
        if not isinstance(widgets, dict) or any(
            not isinstance(widgets.get(k), str) or not widgets[k] for k in WIDGET_KEYS
        ):
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
        self, *, ticket_id: str, kind_label: str, summary: str, reason: str, applicant: Applicant
    ) -> str:
        w = self.config.widgets
        form = [
            {"id": w["ticket_id"], "type": "input", "value": ticket_id},
            {"id": w["kind"], "type": "input", "value": kind_label},
            {"id": w["summary"], "type": "textarea", "value": summary[:2000]},
            {"id": w["reason"], "type": "textarea", "value": reason[:2000]},
        ]
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
        if data.get("approval_code") != self.config.approval_code:
            raise ApprovalError("审批实例不属于配置的审批定义，拒绝")
        if applicant.open_id:
            if data.get("open_id") != applicant.open_id:
                raise ApprovalError("审批发起人不是申请人，拒绝")
        elif data.get("user_id") != applicant.user_id or not applicant.user_id:
            raise ApprovalError("审批发起人不是申请人，拒绝")
        if _form_value(data.get("form"), self.config.widgets["ticket_id"]) != ticket_id:
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

    def widgets(self, approval_code: str) -> list:
        """管理员配置用：列出审批定义里的表单控件（id、类型、名称）。"""
        url = f"{API}/approval/v4/approvals/{urllib.parse.quote(approval_code, safe='')}"
        data = _data(self._send("GET", url, self._token(), None), "查询审批定义")
        try:
            form = json.loads(data.get("form") or "[]")
        except ValueError:
            raise ApprovalError("审批定义的表单不是 JSON") from None
        return [
            {"id": str(x.get("id")), "type": str(x.get("type")), "name": str(x.get("name"))}
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
