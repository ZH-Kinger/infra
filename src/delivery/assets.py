"""云账号资产：用两家的资源中心一次列出账号下的全部资源。

  阿里云   resourcecenter 2022-12-01 SearchResources（需要在控制台开通资源中心，免费）
  火山引擎 resourcecenter 2023-06-01 SearchResources（POST JSON，同样要先开通）

不逐个产品去对接（ECS、OSS、RDS……各一套接口）：资源中心就是为这件事做的，覆盖面和维护成本
都比自己拼好得多。采集只读，凭证复用权限快照的只读身份，需要加 ResourceCenter 只读权限。

快照 identity/assets.json（gitignored，0600）::

    {"captured_at": "...", "accounts": [
        {"platform": "aliyun", "account": "<UID>", "resources": [...]},
        {"platform": "volcano", "account": "<ID>", "error": "..."}]}

采集失败的账号记 error，不写成「没有资源」（和权限快照同一个原则）。

看的人不同，给的粒度不同：员工看得到**指给自己**的那些资源的明细，其余只给数量和地域分布；
管理员看全部明细。

归属从哪来
──────────
资源中心**不告诉你一台机器是谁的** —— 实测 6319 个资源里，归属类标签一个都没有，
资源组也是默认组。所以归属只能我们自己记：`identity/asset-owners.json`（gitignored，0600）::

    {"owners": {"aliyun/<UID>/i-bp1xxx": {"email": "...", "note": "...", "at": "...", "by": "..."}}}

管理员在资产页上指派，或者以后面板自己开通资源时打标签自动带上。
**没指过的就是「未指定」，不猜** —— 按名字、按创建时间猜归属，猜错一次就再没人信这张表。
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from .clouds import aliyun, volcano
from .errors import DeliveryError

ALIYUN_RC = ("resourcecenter.aliyuncs.com", "2022-12-01")
VOLCANO_RC = ("resourcecenter", "2023-06-01")
_MAX_PAGES = 500


class AssetError(DeliveryError):
    """资产快照不可用。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def collect_aliyun(creds: aliyun.Credentials, *, transport=None, progress=None) -> tuple:
    """返回 (账号 ID, 资源列表)。"""
    ident = aliyun.call(*aliyun.STS, "GetCallerIdentity", {}, creds=creds, transport=transport)
    account = str(ident.get("AccountId") or "")
    out, token = [], ""
    for page in range(_MAX_PAGES):
        params = {"MaxResults": "100"}
        if token:
            params["NextToken"] = token
        body = aliyun.call(*ALIYUN_RC, "SearchResources", params, creds=creds, transport=transport)
        items = body.get("Resources")
        if not isinstance(items, list):
            raise AssetError("阿里云资源中心返回缺 Resources，不能当作空结果（资源中心开通了吗？）")
        for r in items:
            out.append(
                {
                    "type": str(r.get("ResourceType") or ""),
                    "id": str(r.get("ResourceId") or ""),
                    "name": str(r.get("ResourceName") or ""),
                    "region": str(r.get("RegionId") or ""),
                    "created": str(r.get("CreateTime") or ""),
                    "group": str(r.get("ResourceGroupId") or ""),
                    "tags": {
                        str(t.get("Key")): str(t.get("Value") or "")
                        for t in r.get("Tags") or []
                        if isinstance(t, dict) and t.get("Key")
                    },
                }
            )
        if progress:
            progress(f"阿里云资源中心 {account}：第 {page + 1} 页，累计 {len(out)} 个")
        nxt = str(body.get("NextToken") or "")
        if not nxt:
            return account, out
        if nxt == token:
            raise AssetError("阿里云资源中心翻页 NextToken 没变，数据不完整，已中断")
        token = nxt
    raise AssetError("阿里云资源中心页数超过上限，数据不完整，已中断")


def collect_volcano(creds: volcano.Credentials, *, transport=None, progress=None) -> tuple:
    users = volcano.call(
        *volcano.IAM, "ListUsers", {"Limit": "1"}, creds=creds, transport=transport
    )
    accounts = {str(u.get("AccountId") or "") for u in users.get("UserMetadata") or []} - {""}
    account = next(iter(accounts)) if len(accounts) == 1 else ""
    out, token = [], ""
    for page in range(_MAX_PAGES):
        body: dict = {"MaxResults": 100}
        if token:
            body["NextToken"] = token
        result = volcano.call(
            *VOLCANO_RC, "SearchResources", {}, body=body, creds=creds, transport=transport
        )
        items = result.get("Resources")
        if not isinstance(items, list):
            raise AssetError("火山资源中心返回缺 Resources，不能当作空结果（资源中心开通了吗？）")
        for r in items:
            account = account or str(r.get("AccountID") or "")
            out.append(
                {
                    "type": str(r.get("ResourceType") or r.get("TypeName") or ""),
                    "id": str(r.get("ResourceID") or ""),
                    "name": str(r.get("ResourceName") or ""),
                    "region": str(r.get("Region") or ""),
                    "created": str(r.get("CreateTime") or ""),
                    "group": str(r.get("ProjectName") or ""),
                    "tags": {
                        str(t.get("Key")): str(t.get("Value") or "")
                        for t in r.get("Tags") or []
                        if isinstance(t, dict) and t.get("Key")
                    },
                }
            )
        if progress:
            progress(f"火山资源中心 {account}：第 {page + 1} 页，累计 {len(out)} 个")
        nxt = str(result.get("NextToken") or "")
        if not nxt:
            return account, out
        if nxt == token:
            raise AssetError("火山资源中心翻页 NextToken 没变，数据不完整，已中断")
        token = nxt
    raise AssetError("火山资源中心页数超过上限，数据不完整，已中断")


Job = tuple  # (platform, 凭证前缀提示, collect() -> (account, resources))


def build_snapshot(jobs: Iterable[Job]) -> dict:
    accounts = []
    for platform, hint, collect in jobs:
        try:
            account, resources = collect()
            accounts.append({"platform": platform, "account": account, "resources": resources})
        except (DeliveryError, OSError) as exc:
            first = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
            first = aliyun._scrub(volcano._scrub(first)) or type(exc).__name__
            accounts.append({"platform": platform, "account": hint, "error": first[:200]})
    return {"captured_at": _now(), "accounts": accounts}


def load(path: Optional[str]) -> Optional[dict]:
    if not path or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"读不了资产快照：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise AssetError("资产快照格式不对")
    return data


def owner_key(platform: str, account: str, resource_id: str) -> str:
    """归属表的键。和申请单里 `平台/账号/资源ID` 的写法保持一致。"""
    parts = [str(x or "").strip() for x in (platform, account, resource_id)]
    if not all(parts):
        raise AssetError("资源标识不完整，应形如 平台/账号/资源ID")
    return "/".join(parts)


def load_owners(path: Optional[str]) -> dict:
    """读归属表。文件不在就是空表 —— 这是正常的初始状态，不是错误。"""
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"读不了资源归属表：{type(exc).__name__}") from exc
    owners = data.get("owners") if isinstance(data, dict) else None
    if not isinstance(owners, dict):
        raise AssetError('资源归属表格式应为 {"owners": {"平台/账号/资源ID": {...}}}')
    return owners


def set_owner(
    path: str, key: str, *, email: str, name: str = "", note: str = "", actor: str
) -> dict:
    """把一个资源指给某人；email 为空表示取消指派。

    整个文件读-改-写在一把文件锁里完成：管理员多开几个页面同时指派，不会互相覆盖。
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    lock = file.with_suffix(".lock")
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        owners = load_owners(path)
        if email:
            owners[key] = {
                "email": email.strip().lower(),
                "name": str(name or "")[:64],
                "note": str(note or "")[:200],
                "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "by": str(actor or "")[:64],
            }
        else:
            owners.pop(key, None)
        _write_private(file, {"owners": owners})
        return owners
    finally:
        os.close(fd)


def _write_private(path: Path, data: dict) -> None:
    """0600、原子替换。和 people.write_private_json 同一个做法，这里不引它是为了
    让 assets 这一支不依赖名册模块。"""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.write(fd, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    Path(tmp).chmod(0o600)
    Path(tmp).replace(path)


def _with_owner(r: dict, owners: dict, key: tuple) -> dict:
    """给一条资源附上归属。没指过就是空串 —— 前端据此显示「未指定」。"""
    own = owners.get(f"{key[0]}/{key[1]}/{r.get('id', '')}") or {}
    return {
        **r,
        "type_label": type_label(r.get("type", "")),
        # 前端按它做筛选和分组（计算 / 存储 / 其他）
        "category": category_of(r.get("type", "")),
        "owner_email": str(own.get("email") or ""),
        "owner_name": str(own.get("name") or ""),
        "owner_note": str(own.get("note") or ""),
        "owner_at": str(own.get("at") or ""),
    }


#: 资源中心会返回一些**不是资产**的东西，混在里面会让总数失去意义 ——
#: 线上实测 6137 条里 5024 条是火山的 `invocation`（调用记录），另有 200 多条是身份对象。
#: 一个「6137 个资源」的数字既吓人又没用，而真正的计算实例两朵云加起来只有 43 台。
#:
#: **只排除两类，不多排**：
#:   · 调用/执行记录 —— 它们是事件，不是资产，而且数量会一直涨
#:   · 身份对象（用户、组、角色、策略）—— 它们在「权限」那一页已经有了，
#:     而且把「人」算进「资产」会让归属这件事彻底讲不清
#: 网关、主题、子网这类配置对象**保留**：它们确实是账号里存在的东西，也要有人负责。
#:
#: 过滤发生在**展示层**，快照文件原样保留 —— 那是取证材料，而且过滤规则以后还会改。
#: 每个账号会带上被过滤掉的条数，不让它们悄悄消失。
_NOT_ASSET_EXACT = frozenset(
    {
        # 火山：调用记录
        "invocation",
        # 火山：身份对象
        "user",
        "group",
        "role",
        "policy",
        "permissionnamespace",
    }
)
#: 阿里的类型是 `ACS::<服务>::<对象>`，身份对象都在 RAM 这个服务下
_NOT_ASSET_PREFIX = ("ACS::RAM::",)


#: 资产分类。判据是「**使用它的人能不能对它做决定**」：
#:   计算 / 存储 —— 员工要的就是这些：我有几台机器、几个桶、几个文件系统
#:   网络 / 其他 —— 交换机、安全组、网卡、路由表、镜像……是跟着上面那些走的附属品，
#:     单独列给员工只会让他以为自己名下有十几样东西，其实就三台机器
#: 云盘刻意归到「网络/其他」那一侧不给员工看，理由同上：它是实例的一部分，
#: 不是一件可以独立处置的东西。
_COMPUTE = ("instance", "devinstance", "dsw", "vci", "container", "ecs")
_STORAGE = ("bucket", "vepfs", "cpfs", "nas", "filesystem", "oss", "tos")
#: 员工看得到的分类
VISIBLE_TO_STAFF = ("compute", "storage")


def category_of(resource_type: str) -> str:
    """计算 / 存储 / 其他。用于决定员工那边看不看得到。"""
    t = str(resource_type or "").lower()
    tail = t.split("::")[-1] if "::" in t else t
    if tail in ("disk", "image", "snapshot"):
        return "other"
    if any(k in tail for k in _COMPUTE):
        return "compute"
    if any(k in tail for k in _STORAGE):
        return "storage"
    return "other"


def is_asset(resource_type: str) -> bool:
    """这条记录算不算「资产」。见 `_NOT_ASSET_EXACT` 的说明。"""
    t = str(resource_type or "")
    return t not in _NOT_ASSET_EXACT and not t.startswith(_NOT_ASSET_PREFIX)


def type_label(resource_type: str) -> str:
    """ACS::ECS::Instance → ECS Instance；volcano 的 ecs.instance 原样。"""
    parts = resource_type.split("::")
    return " ".join(parts[1:]) if len(parts) == 3 and parts[0] == "ACS" else resource_type


def holdings_view(tickets: Iterable[dict], *, labels: Callable[[str, str], str]) -> list:
    """从申请单算出「这个人手里有什么」。

    **这是归属最确定的那份数据，而且一直就在手边。** 云上采来的资产没有归属信息（资源
    不带主人这个属性），要靠 `asset-owners.json` 一条条指；而面板自己发出去的东西，
    申请人是谁写在单子里，不需要任何映射。资产页却只读云上快照、完全没碰申请单 ——
    于是每个人的资产都是 0，哪怕他手里正握着三把凭证。

    只算**还有效**的：已作废、已到期、被拒、撤回的都不算持有。到期时间为空表示长期。
    """
    out = []
    for ticket in tickets:
        status = str(ticket.get("status") or "")
        if status not in ("done", "fulfilling"):
            continue
        tpl = ticket.get("template") or {}
        kind = str(ticket.get("kind") or "")
        platform, account = str(tpl.get("platform") or ""), str(tpl.get("account") or "")
        item = {
            "kind": kind,
            "request_id": str(ticket.get("id") or ""),
            "title": str(tpl.get("title") or ""),
            "platform": platform,
            "account": account,
            "account_label": labels(platform, account),
            "expires_at": str(ticket.get("expires_at") or ""),
            "detail": "",
            "gone": False,
        }
        if kind == "credential":
            # 凭证被作废之后密文就没了。没有密文 = 这把凭证已经用不了，别再算成持有
            sealed = ticket.get("sealed") or {}
            item["gone"] = not sealed.get("ciphertext")
            payload = ticket.get("payload") or {}
            subject = str(payload.get("subject") or "")
            item["detail"] = f"给 {subject}" if subject else "给你自己"
        elif kind == "account":
            name = ticket.get("cred_user") or (ticket.get("payload") or {}).get("username") or ""
            item["detail"] = f"子账号 {name}" if name else "子账号"
        elif kind == "permission":
            item["detail"] = "、".join(tpl.get("groups") or []) or "权限"
        elif kind == "resource":
            # 资源是人工开通的，实例 ID 埋在登记的那段文字里（见 flows.fulfil）
            item["detail"] = str(ticket.get("result") or "")[:120] or "待管理员登记"
        if item["gone"]:
            continue
        out.append(item)
    out.sort(key=lambda x: (x["kind"], x["expires_at"] or "9999", x["request_id"]))
    return out


def summary_view(
    data: Optional[dict],
    *,
    scopes: Optional[set],
    labels: Callable[[str, str], str],
    owners: Optional[dict] = None,
    viewer_email: str = "",
) -> dict:
    """管理员（scopes=None）拿全部明细；员工（scopes=本人有子账号的云账号）拿两样：

      · **指给自己的那些资源的明细** —— 他得知道自己有哪台机器、在哪个地域、什么时候建的
      · 其余资源只有数量和地域分布

    没指过归属的资源对员工一律不显示明细。宁可让人看到「未指定」去问管理员，
    也不要按名字或创建时间去猜 —— 猜错一次，这张表就再没人信了。
    """
    if data is None:
        return {"captured_at": "", "accounts": []}
    owners = owners or {}
    me = str(viewer_email or "").strip().lower()
    out = []
    for acc in data["accounts"]:
        key = (str(acc.get("platform") or ""), str(acc.get("account") or ""))
        if scopes is not None and key not in scopes:
            continue
        item = {
            "platform": key[0],
            "account": key[1],
            "account_label": labels(*key),
            "error": str(acc.get("error") or ""),
        }
        raw = acc.get("resources") or []
        resources = [r for r in raw if is_asset(r.get("type", ""))]
        item["filtered"] = len(raw) - len(resources)
        types = Counter(type_label(r.get("type", "")) for r in resources)
        regions = Counter(r.get("region") or "全局" for r in resources)
        item["total"] = len(resources)
        item["by_type"] = [{"type": t, "count": n} for t, n in types.most_common()]
        item["by_region"] = [{"region": r, "count": n} for r, n in regions.most_common()]

        ordered = sorted(resources, key=lambda r: (r.get("type", ""), r.get("name", "")))
        viewed = [_with_owner(r, owners, key) for r in ordered]
        if scopes is None:
            item["resources"] = viewed
        else:
            mine = [
                v
                for v in viewed
                if me
                and v["owner_email"].strip().lower() == me
                # 只给计算和存储。网络对象和云盘是附属品，列出来只会稀释「我有什么」
                and category_of(v.get("type", "")) in VISIBLE_TO_STAFF
            ]
            item["resources"] = mine
            item["mine"] = len(mine)
            item["unassigned"] = sum(1 for v in viewed if not v["owner_email"])
        if scopes is not None and item["error"]:
            item["error"] = "本次未采集完整"
        out.append(item)
    return {"captured_at": str(data.get("captured_at") or ""), "accounts": out}
