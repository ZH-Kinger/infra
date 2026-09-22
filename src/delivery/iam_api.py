"""IT 的云账号属性接口（cloud-accounts-api）客户端。

它替掉的是「导出 CSV → 人工发给 IT → IT 导入 → 回传结果」这一段：
`iam_export.diff()` 算出来的那些行，现在可以直接下发。**比对规则一行都不用改** ——
`action=set` 就是 PUT、`action=remove` 就是 DELETE，`skip` 不发。

多出来的那件事更重要：以前面板对 IAM 侧的实际状态是瞎的，只能拿自己存的基线当真相，
基线和实际一旦漂移谁都不知道。现在 `GET /users?app=` 能直接读回来 ——
**基线可以从「我们记得发过什么」升级成「IAM 现在是什么」**。

凭证
────
token 只从环境变量 `DELIVERY_IAM_API_TOKEN` 读。**不接受参数默认值、不读文件、不落日志**：
它能改全员的 SSO NameID，泄露等于别人可以把自己的云登录名写到任何人身上。
`_scrub()` 在所有对外消息里抹掉它 —— 上游真回显了 Authorization 头也不会漏出去。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from .errors import DeliveryError

ENV_BASE = "DELIVERY_IAM_API_BASE"
ENV_TOKEN = "DELIVERY_IAM_API_TOKEN"  # noqa: S105 — 环境变量名，不是口令本身
DEFAULT_BASE = "https://iam.wuji-tech.com/ext/cloud-accounts"

#: 接口认的平台标识。**不在这张表里的一律不发** —— 拼错一个 app 名，
#: 接口回 400 是好的结局；真正危险的是拼成另一个存在的平台，把人的阿里云登录名写进火山
APPS = {
    "aliyun/1704065796538912": "aliyun-main",
    "volcano/2111674479": "volcano-main",
}

#: 终态错误：重试多少次都一样，而且每一个都意味着有人要去处理一件事
TERMINAL = {
    "value_taken",  # 登录名已属于另一个人 —— 一个云身份不能对应两个人
    "inactive_user",  # 人已离职，IAM 拒绝写入 —— 该回收云上账号了
    "not_found",  # IAM 里没这个 union_id —— 名册和 IAM 对不上
    "bad_request",  # 格式不合规 —— 我们这边生成错了
}
#: 可重试：上游暂时不可用
RETRIABLE = {"upstream_error"}

#: HTTP 状态 → 文案。接口约定错误体是 {"error": code, "detail": ...}，
#: 但上游挂掉时可能回非 JSON，那时按状态码兜底
_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    404: "not_found",
    409: "conflict",
    502: "upstream_error",
}

Transport = Callable[[str, str, dict, bytes], tuple]


class IamApiError(DeliveryError):
    """接口返回错误。`code` 用来区分「该重试」和「该找人」。"""

    def __init__(self, message: str, *, code: str = "", status: int = 0, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail

    @property
    def terminal(self) -> bool:
        return self.code in TERMINAL


@dataclass(frozen=True)
class Config:
    base: str
    #: **`repr=False`**：dataclass 自动生成的 __repr__ 会把 token 原样打出来，
    #: 一行 print(cfg) 或者异常里带上它就漏了
    token: str = field(repr=False)

    @classmethod
    def from_env(cls) -> Config:
        token = (os.environ.get(ENV_TOKEN) or "").strip()
        if not token:
            raise IamApiError(
                f"缺 {ENV_TOKEN}。这个 token 由 IT 私发，只放面板服务器的环境变量或 600 文件，"
                "不进代码仓库、镜像、日志。"
            )
        base = (os.environ.get(ENV_BASE) or DEFAULT_BASE).strip().rstrip("/")
        if not base.startswith("https://"):
            # 明文传 token 等于送出去。**不给降级开关**
            raise IamApiError(f"{ENV_BASE} 必须是 https：{base!r}")
        return cls(base=base, token=token)

    def scrub(self, text: str) -> str:
        """把 token 从任何对外文本里抹掉。上游回显了 Authorization 头也不会漏。"""
        out = str(text or "")
        if self.token and self.token in out:
            out = out.replace(self.token, "<token 已隐去>")
        return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """一律不跟随跳转。

    **urllib 跟随 302 时只剥 content-length / content-type，`Authorization` 原样带走** ——
    上游要是被劫持或配错，一个 302 就能把 token 送到别的 host。
    这个接口本来也不该跳转，所以直接拒绝，不做「同 host 才跟」那种判断。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IamApiError(f"IAM 接口返回跳转（HTTP {code}），不跟随", status=code)


_opener = urllib.request.build_opener(_NoRedirect)


def _http(method: str, url: str, headers: dict, body: bytes) -> tuple:
    # noqa 的理由：`url` 上面已经强制过 https（明文传 token 等于送出去），
    # 不存在 file:// 之类的 scheme 能走到这里
    req = urllib.request.Request(  # noqa: S310
        url, data=body or None, headers=headers, method=method
    )
    try:
        with _opener.open(req, timeout=20) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx / 5xx 也要读正文拿 error code
        raw, status = exc.read(), exc.code
    except OSError as exc:
        raise IamApiError(f"连不上 IAM 接口：{exc}") from exc
    try:
        return status, json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {"_raw": raw[:200].decode("utf-8", "replace")}


def call(
    method: str,
    path: str,
    body: Optional[dict] = None,
    *,
    cfg: Config,
    transport: Optional[Transport] = None,
) -> dict:
    """打一次接口。2xx 返回正文，其余抛 `IamApiError`（消息里不含 token）。"""
    send = transport or _http
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = {
        "Authorization": f"Bearer {cfg.token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    status, reply = send(method, f"{cfg.base}{path}", headers, payload)
    if not isinstance(reply, dict):
        raise IamApiError(f"`{path}` 返回的不是对象，不能当结果用", status=status)
    if 200 <= status < 300:
        return reply
    # error 也要 scrub：上游把回显放这个字段就绕过了只擦 detail 的那一道
    code = cfg.scrub(str(reply.get("error") or _BY_STATUS.get(status) or "http_error"))
    detail = cfg.scrub(str(reply.get("detail") or reply.get("_raw") or ""))
    raise IamApiError(
        f"`{method} {path}` 失败 HTTP {status}（{code}）：{detail[:200]}",
        code=code,
        status=status,
        detail=detail,
    )


# ── 四个接口 ────────────────────────────────────────────────────────────────


def get_user(union_id: str, *, cfg: Config, transport: Optional[Transport] = None) -> dict:
    return call("GET", f"/users/{union_id}", cfg=cfg, transport=transport)


def put_value(
    union_id: str, app: str, value: str, *, cfg: Config, transport: Optional[Transport] = None
) -> dict:
    """写入/更新。返回体里的 `previous` 是写入前的值 —— **那是我们唯一能拿到的旧值**，
    必须记进操作日志，否则改错了没有东西可以回滚。"""
    return call("PUT", f"/users/{union_id}/{app}", {"value": value}, cfg=cfg, transport=transport)


def delete_value(
    union_id: str, app: str, *, cfg: Config, transport: Optional[Transport] = None
) -> dict:
    return call("DELETE", f"/users/{union_id}/{app}", cfg=cfg, transport=transport)


def list_by_app(app: str, *, cfg: Config, transport: Optional[Transport] = None) -> list:
    """按平台列全量。`users` 缺失时**抛错而不是当空**：
    把「读不到」渲染成「IAM 里一个人都没有」，对账那边会得出「全员都该删」。"""
    got = call("GET", f"/users?app={app}", cfg=cfg, transport=transport)
    users = got.get("users")
    if not isinstance(users, list):
        raise IamApiError(f"列表接口没返回 users（app={app}），不能当作空结果")
    return users


# ── 把 diff 的输出下发出去 ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    """一行的下发结果。`previous` 来自接口，是回滚时唯一的依据。"""

    row: dict
    ok: bool
    action: str
    previous: str = ""
    code: str = ""
    message: str = ""


def app_of(scope_or_app: str) -> str:
    """`aliyun/<UID>` → `aliyun-main`。已经是接口标识就原样返回。"""
    text = str(scope_or_app or "").strip()
    if text in APPS.values():
        return text
    got = APPS.get(text)
    if not got:
        raise IamApiError(
            f"不认识的平台标识 {text!r}。接口只认 {' / '.join(sorted(APPS.values()))} —— "
            "拼错了不会静默跳过，因为那可能把一个平台的登录名写进另一个平台。"
        )
    return got


def apply_rows(
    rows: list,
    *,
    cfg: Config,
    transport: Optional[Transport] = None,
    dry_run: bool = False,
) -> list:
    """把 `iam_export.diff()` 的行下发。返回每行一个 `Outcome`。

    **per-row 包 try，不是整批包一个**：一条脏记录不该让这一轮剩下的全部不发。
    这跟 `temp_ak_issuance.cleanup.sweep_expired` 踩过的是同一个坑。

    `skip` 行不发 —— 那是比对阶段就标出「要人核对」的，下发它等于绕过核对。
    """
    out: list = []
    for row in rows:
        action = str(row.get("action") or "")
        if action not in ("set", "remove"):
            continue
        uid = str(row.get("feishu_union_id") or "").strip()
        if not uid:
            out.append(
                Outcome(
                    row=row,
                    ok=False,
                    action=action,
                    code="bad_request",
                    message="这一行没有 feishu_union_id，接口只认 union_id",
                )
            )
            continue
        try:
            app = app_of(str(row.get("app") or ""))
        except IamApiError as exc:
            out.append(
                Outcome(row=row, ok=False, action=action, code="bad_request", message=str(exc))
            )
            continue
        if dry_run:
            out.append(Outcome(row=row, ok=True, action=action, message="预演，未下发"))
            continue
        try:
            if action == "set":
                got = put_value(uid, app, str(row.get("value") or ""), cfg=cfg, transport=transport)
            else:
                got = delete_value(uid, app, cfg=cfg, transport=transport)
            out.append(
                Outcome(row=row, ok=True, action=action, previous=str(got.get("previous") or ""))
            )
        except IamApiError as exc:  # noqa: PERF203 — 就是要按行隔离
            out.append(
                Outcome(
                    row=row, ok=False, action=action, code=exc.code, message=cfg.scrub(str(exc))
                )
            )
    return out


# ── 对账：IAM 实际 vs 我们的名册 ────────────────────────────────────────────


@dataclass(frozen=True)
class Drift:
    """一条对不上的。`kind` 决定谁去处理。"""

    kind: str
    app: str
    union_id: str
    username: str = ""
    name: str = ""
    theirs: str = ""
    ours: str = ""


#: 对不上的四种，处置完全不同
DRIFT_LEFT = "left"  # IAM 有、我们没有 —— 多半是人离职了但云上没回收
DRIFT_MISSING = "missing"  # 我们有、IAM 没有 —— 该发没发，或者被别处删了
DRIFT_DIFFERENT = "different"  # 两边都有但值不一样 —— **最危险的一种**
DRIFT_INACTIVE = "inactive"  # IAM 说这人已离职，但云登录名还挂着
#: IAM 里挂着一个登录名，但**云上已经没有这个账号了**（多半是有人在控制台直接删了）。
#: 这一类只有把云侧快照喂进来才判得出，而它的后果很隐蔽：属性看着是好的、人也在职，
#: 但 SSO 会匹配到一个不存在的用户，登录失败且毫无线索
DRIFT_GONE = "gone"


@dataclass(frozen=True)
class Reclaim:
    """一个该回收的人。`sure` 决定能不能自动做。"""

    app: str
    union_id: str
    username: str
    name: str
    value: str
    #: 能不能自动删。**以 IT 的 Authentik 为准** —— 它说离职就是离职
    sure: bool
    #: 飞书通讯录那边还有这个人。**不阻止回收**，但要单独报出来：
    #: 那说明飞书没同步离职，人还在部门树里、还收得到内部消息
    stale_roster: bool = False
    why: str = ""


def reclaim_plan(theirs: list, *, app: str, roster_uids) -> list:
    """谁的 IAM 属性该回收。**纯逻辑，不调任何接口。**

    **以 IT 的 Authentik 为准**：它说 `is_active=false` 就回收，不再要求名册也同意。

    名册（飞书通讯录）那边还有这个人时，**照样回收，但标 `stale_roster`** ——
    那不是"要不要删"的分歧，是**飞书那边该同步离职了**：人还在部门树里，
    还在收内部消息、还占着通讯录。这是个要去修的上游问题，得单独报出来，
    不能因为回收做完了就当没这回事。

    **只管属性，不管云上账号。** 禁用 / 删除 RAM 用户永远走人工 ——
    删属性是可逆的（PUT 回去就行），删账号不是。

    唯一还拦着的是**数量闸门**（见 `iam_sync.RECLAIM_MAX`）：Authentik 自己出故障、
    一次标出几十个离职时，那道闸拦得住。
    """
    known = {str(u or "") for u in roster_uids}
    out = []
    for u in theirs:
        uid = str(u.get("union_id") or "")
        value = str(u.get("value") or "")
        # **`is not False` 不是 `.get(..., True)`**：Authentik 属性没设时回的是 JSON null，
        # `.get` 拿到 None、falsy，于是「不知道在不在职」被当成「已离职」直接删。
        # 不知道必须当在职
        if not uid or not value or u.get("is_active") is not False:
            continue
        still_here = uid in known
        out.append(
            Reclaim(
                app=app,
                union_id=uid,
                value=value,
                username=str(u.get("username") or ""),
                name=str(u.get("name") or ""),
                sure=True,  # Authentik 为准
                stale_roster=still_here,
                why="飞书通讯录里还有他，那边该同步离职" if still_here else "飞书通讯录里也没有了",
            )
        )
    out.sort(key=lambda r: (r.stale_roster, r.username))
    return out


def uncomparable(ours: list) -> list:
    """我们这边**没有 union_id、因此根本没法对账**的行。

    接口只认 union_id，所以这些行既发不出去也比不了。它们必须单独报出来 ——
    混在「对不上 0 条」里的话，读的人会以为两边一致，而实际上有几个人压根没进过比对。
    """
    return [
        r
        for r in ours
        if r.get("action") == "set" and not str(r.get("feishu_union_id") or "").strip()
    ]


def _login(value: str) -> str:
    """从属性值里取出云上的登录名。

    阿里那边是 `zhangzichao@1704065796538912.onaliyun.com`，火山那边就是裸的
    `zhangzichao`。**按 `@` 切，不按后缀匹配** —— 后缀写在配置里、随账号别名变，
    在这儿再认一遍两处迟早不一致。
    """
    return str(value or "").split("@", 1)[0].strip()


def reconcile(theirs: list, ours: list, *, app: str, cloud_users=None) -> list:
    """IAM 侧实际（`list_by_app` 的返回）对比我们这边应该有的（diff 的 set 行）。

    **这是以前做不到的事**：没有这个接口时，面板只能拿自己存的基线当真相，
    而基线只记录「我们发过什么」，不记录「IT 那边最后变成了什么」。
    中间任何一次人工导入出错，两边就永久漂移且无人察觉。
    """
    want = {
        str(r.get("feishu_union_id") or ""): str(r.get("value") or "")
        for r in ours
        if r.get("action") == "set" and r.get("feishu_union_id")
    }
    seen = set()
    out: list = []
    for u in theirs:
        uid = str(u.get("union_id") or "")
        if not uid:
            continue
        seen.add(uid)
        theirs_value = str(u.get("value") or "")
        base = {
            "app": app,
            "union_id": uid,
            "username": str(u.get("username") or ""),
            "name": str(u.get("name") or ""),
            "theirs": theirs_value,
            "ours": want.get(uid, ""),
        }
        if u.get("is_active") is False and theirs_value:
            # 接口文档明说：is_active=false 的是「已离职但云上登录名尚未回收」
            out.append(Drift(kind=DRIFT_INACTIVE, **base))
            continue
        if cloud_users is not None and theirs_value and _login(theirs_value) not in cloud_users:
            # **属性指向一个不存在的云账号。** 人在职、属性也在，但 SSO 会匹配到空 ——
            # 登录失败，而排查的人看到「属性明明写着呢」就卡住了。
            # `cloud_users` 为 None 表示这次没拿到云侧快照：那就不判这一类，
            # 而不是把全公司报成「账号已删」
            out.append(Drift(kind=DRIFT_GONE, **base))
            continue
        if uid not in want:
            out.append(Drift(kind=DRIFT_LEFT, **base))
        elif want[uid] != theirs_value:
            out.append(Drift(kind=DRIFT_DIFFERENT, **base))
    for uid, value in sorted(want.items()):
        if uid not in seen:
            out.append(Drift(kind=DRIFT_MISSING, app=app, union_id=uid, ours=value))
    out.sort(key=lambda d: (d.kind, d.username, d.union_id))
    return out
