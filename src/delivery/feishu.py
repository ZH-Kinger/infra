"""飞书 OAuth 的服务端部分：拿 code 换身份。

**只有这里碰 app_secret。** CLI 不持有它（见 login.py 的说明），所以换 token 必须
在服务端做。零第三方依赖，只用标准库。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .errors import DeliveryError

TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"  # noqa: S105  端点，不是口令
USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
_TIMEOUT = 15


class FeishuError(DeliveryError):
    """飞书接口返回了错误。"""


@dataclass(frozen=True)
class FeishuUser:
    open_id: str
    union_id: str
    name: str
    email: str = ""
    #: 两个来源分开存，因为它们的缺失原因完全不同：
    #:   enterprise_email  飞书邮箱服务分配的 —— 租户没开这个服务就恒为空
    #:   contact_email     管理员导入的联系方式 —— 用腾讯企业邮/Google Workspace 的公司在这里
    #: 混成一个字段的话，看到空值无从判断是「权限没申请」还是「租户没开邮箱服务」，
    #: 而这两件事一个要找管理员改权限、一个要去管理后台开开关。
    enterprise_email: str = ""
    contact_email: str = ""
    #: 租户内的 user_id。公司 IAM 登录时从 wuji scope 的 feishu_user_id 来；飞书应用登录时
    #: 用 open_id 就够，这里留空。发起飞书审批要用其中之一。
    user_id: str = ""

    @property
    def identity(self) -> str:
        """身份表主键。

        用 union_id 不用 open_id：open_id 是 **per-app** 的，同一个人在不同飞书应用
        里不一样。而 user_id 要通讯录权限（那个至今没开），不能被它卡住。
        """
        return self.union_id or self.open_id


def _post_json(url: str, payload: dict, *, headers: Optional[dict] = None) -> dict:
    body = json.dumps(payload).encode()
    head = {"Content-Type": "application/json; charset=utf-8"}
    head.update(headers or {})
    # url 是本模块写死的飞书端点常量，不接受外部输入
    req = urllib.request.Request(url, data=body, headers=head, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        # 只回飞书给的 code/msg，**绝不回请求体**——里面有 client_secret 和授权码。
        raise FeishuError(
            f"飞书接口 {url} 返回 HTTP {exc.code}"
            f"（code={data.get('code')} msg={data.get('msg') or data.get('error')}）"
        ) from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"连不上飞书：{exc.reason}") from exc


def exchange_code(
    *, app_id: str, app_secret: str, code: str, redirect_uri: str, code_verifier: str = ""
) -> str:
    """授权码换 user_access_token。

    飞书 v2 的 token 接口**即使走 PKCE 也强制要 client_secret**（实测文档），
    这正是它不能放在 CLI 里的原因。
    """
    if not app_id or not app_secret:
        raise FeishuError(
            "缺少飞书 App ID / App Secret："
            "请设置 DELIVERY_FEISHU_APP_ID 与 DELIVERY_FEISHU_APP_SECRET"
        )
    payload = {
        "grant_type": "authorization_code",
        "client_id": app_id,
        "client_secret": app_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier
    data = _post_json(TOKEN_URL, payload)
    if data.get("code") not in (0, None):
        raise FeishuError(f"换取 token 失败：code={data.get('code')} msg={data.get('msg')}")
    token = data.get("access_token")
    if not token:
        raise FeishuError("飞书没有返回 access_token")
    return str(token)


def fetch_user(access_token: str) -> FeishuUser:
    req = urllib.request.Request(
        USER_INFO_URL, headers={"Authorization": f"Bearer {access_token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise FeishuError(f"取用户信息失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"连不上飞书：{exc.reason}") from exc
    if data.get("code") not in (0, None):
        raise FeishuError(
            f"取用户信息失败：code={data.get('code')} msg={data.get('msg')}。"
            f"常见原因是应用缺少通讯录/用户信息权限。"
        )
    body = data.get("data") or {}
    enterprise = str(body.get("enterprise_email") or "").strip()
    contact = str(body.get("email") or "").strip()
    user = FeishuUser(
        open_id=str(body.get("open_id") or ""),
        union_id=str(body.get("union_id") or ""),
        name=str(body.get("name") or body.get("en_name") or ""),
        # enterprise_email 优先：它是公司统一分配的，contact_email 可能是私人地址。
        # 但两个都留着，诊断时要分得开。
        email=enterprise or contact,
        enterprise_email=enterprise,
        contact_email=contact,
    )
    if not user.identity:
        raise FeishuError("飞书返回的用户信息里没有 open_id / union_id")
    return user
