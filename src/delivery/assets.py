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

看的人不同，给的粒度不同：员工只看自己有子账号的云账号里，各类资源的数量和地域分布；
资源名称、ID、IP 这些明细只给管理员。
"""

from __future__ import annotations

import json
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


def _type_label(resource_type: str) -> str:
    """ACS::ECS::Instance → ECS Instance；volcano 的 ecs.instance 原样。"""
    parts = resource_type.split("::")
    return " ".join(parts[1:]) if len(parts) == 3 and parts[0] == "ACS" else resource_type


def summary_view(
    data: Optional[dict], *, scopes: Optional[set], labels: Callable[[str, str], str]
) -> dict:
    """员工视角（scopes=本人有子账号的云账号）只给数量；管理员（scopes=None）给明细。"""
    if data is None:
        return {"captured_at": "", "accounts": []}
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
        resources = acc.get("resources") or []
        types = Counter(_type_label(r.get("type", "")) for r in resources)
        regions = Counter(r.get("region") or "全局" for r in resources)
        item["total"] = len(resources)
        item["by_type"] = [{"type": t, "count": n} for t, n in types.most_common()]
        item["by_region"] = [{"region": r, "count": n} for r, n in regions.most_common()]
        if scopes is None:
            item["resources"] = [
                {**r, "type_label": _type_label(r.get("type", ""))}
                for r in sorted(resources, key=lambda r: (r.get("type", ""), r.get("name", "")))
            ]
        if scopes is not None and item["error"]:
            item["error"] = "本次未采集完整"
        out.append(item)
    return {"captured_at": str(data.get("captured_at") or ""), "accounts": out}
