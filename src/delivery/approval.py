"""飞书审批：发起、查询、校验。

**开通之前的唯一放行条件是 `verify()` 通过**（docs/cloud-access-platform.md 规则 R1）：
实时调飞书接口取审批实例，逐项核对

  · 状态是 APPROVED
  · 审批定义编号是我们配置的那一个（防止拿别的审批流里通过的实例来冒充）
  · 发起人就是申请人（open_id 或 user_id）
  · 表单里的申请单号就是这张申请单（防止一个审批实例被套用到另一张单子上）
  · 实例没有被撤销（reverted：通过后又被撤销的单据，status 可能仍是 APPROVED）

飞书回调、本地保存的状态都只当「该去查一次了」的提示，不当结论。

配置 `identity/approval.json`（gitignored）::

    {"approval_code": "<审批定义编号>",
     "widgets": {"ticket_id": "<控件ID>", "kind": "<控件ID>",
                 "summary": "<控件ID>", "reason": "<控件ID>"}}

控件 ID 在飞书审批后台建好表单后，用 `delivery approval widgets` 查（调查询审批定义接口）。
"""

from __future__ import annotations

import http.client
import json
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
        return cls(approval_code=code.strip(), widgets=dict(widgets))


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
            return STATUS_REVERTED
        return status

    def verify_approved(self, *, instance_code: str, ticket_id: str, applicant: Applicant) -> None:
        """开通前调用。任何一项不满足都抛错。"""
        status = self.status(instance_code=instance_code, ticket_id=ticket_id, applicant=applicant)
        if status != STATUS_APPROVED:
            raise ApprovalError(f"飞书审批状态是 {status}，不是已通过，拒绝开通")

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
