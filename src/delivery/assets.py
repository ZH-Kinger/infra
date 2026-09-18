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


#: PAI 工作空间分布在哪些地区。**写死一张表而不是去枚举**：阿里云没有「列出我开通了
#: 哪些地区的 PAI」这个接口，只能挨个地区问；问一个没开通的地区会报错而不是返回空，
#: 所以这张表宁可长一点，采集时对报错的地区记一笔跳过、不中断。
PAI_REGIONS = (
    "cn-hangzhou",
    "cn-shanghai",
    "cn-beijing",
    "cn-shenzhen",
    "cn-wulanchabu",
    "cn-heyuan",
    "cn-zhangjiakou",
    "ap-southeast-1",
)


def _pai(region: str) -> str:
    return f"aiworkspace.{region}.aliyuncs.com"


#: 属主的三种情况。**必须分开**：主账号建的公共目录（share、backbones 这些）本来就没有
#: 个人属主，把它算成「属主不在了」的话，那一栏 18 条里有 14 条是噪音，而真正要看的
#: 只有剩下 4 条 —— 一份四分之三是噪音的清单没人会看第二遍。
OWNER_USER = "user"  # 认出来了，是某个在职 RAM 用户
OWNER_ROOT = "root"  # 主账号自己建的，通常是公共目录
OWNER_GONE = "gone"  # RAM 里已经查无此人 —— 人走了，数据集和目录还留着


def collect_recycle_bin(creds, *, transport=None) -> list:
    """RAM 用户回收站：删掉但还没过保留期的子账号。`IMS.ListUsersInRecycleBin`。

    **趁保留期内把 UserId → 姓名固化下来。** 过了期这个人就只剩一个数字 UserId，
    而他留下的数据集、目录、机器还在 —— 实测已经有一条是这样了（`/cwr`，删于
    2026-08-05，回收站里已经查不到，现在没人说得清那是谁的）。

    这是**离职回收缺的那半块**：体检清单原本只能回答「这个号的主人还在不在通讯录里」，
    回收站回答的是反向的那个问题 —— 号已经没了，他留下的东西还在没在。
    """
    from .clouds import aliyun

    out, marker = [], ""
    for _ in range(_MAX_PAGES):
        query = {"MaxItems": "100"}
        if marker:
            query["Marker"] = marker
        body = aliyun.call(
            *aliyun.IMS, "ListUsersInRecycleBin", query, creds=creds, transport=transport
        )
        for u in (body.get("Users") or {}).get("User") or []:
            principal = str(u.get("UserPrincipalName") or "")
            out.append(
                {
                    "user_id": str(u.get("UserId") or ""),
                    # 登录名是 `<名>@<UID>.onaliyun.com`，只留 @ 前面那段，和别处的登录名对得上
                    "login": principal.split("@", 1)[0],
                    "name": str(u.get("DisplayName") or ""),
                    "deleted_at": str(u.get("RecycleDate") or ""),
                }
            )
        if not body.get("IsTruncated"):
            return out
        marker = str(body.get("Marker") or "")
        if not marker:
            return out
    raise AssetError("回收站翻页超过上限，数据不完整，已中断")


def collect_pai_datasets(
    creds, *, regions=PAI_REGIONS, recycled=None, transport=None, progress=None
) -> tuple:
    """PAI 数据集清单。返回 `(数据集列表, 跳过的地区说明)`。

    **这是唯一一类自带归属的资产。** 资源中心对 ECS/OSS 一个归属标签都不给，所以那些
    只能靠 `asset-owners.json` 一条条人工指；而数据集的 `UserId` 就是建它的那个 RAM 用户 ——
    归属是白来的，不用任何人工登记。

    UserId 换名字走的是**账号级的 RAM 用户表**，不是工作空间成员表：一个人被移出工作空间
    之后，他建的数据集还在，但成员表里已经查不到他了 —— 只看成员表会把「被移出工作空间」
    误报成「人已经没了」，而这两件事的处置完全不同。
    """
    from .clouds import aliyun

    account = str(
        aliyun.call(*aliyun.STS, "GetCallerIdentity", creds=creds, transport=transport).get(
            "AccountId"
        )
        or ""
    )
    ram_users = {
        str(u.get("UserId") or ""): {
            "login": str(u.get("UserName") or ""),
            "name": str(u.get("DisplayName") or ""),
        }
        for u in aliyun.paginate(
            *aliyun.RAM,
            "ListUsers",
            key="User",
            container="Users",
            creds=creds,
            transport=transport,
        )
    }
    # 回收站只用来**认人**，不改 owner_kind：号确实已经删了，
    # 只是趁保留期还在，把「那是谁」记下来
    bin_users = {str(u.get("user_id") or ""): u for u in (recycled or ())}
    out: list = []
    skipped: list = []
    for region in regions:
        try:
            spaces = aliyun.call_roa(
                _pai(region),
                aliyun.AIWORKSPACE,
                "/api/v1/workspaces",
                {"PageSize": 50},
                creds=creds,
                transport=transport,
            ).get("Workspaces")
        except aliyun.AliyunDenied:
            raise
        except aliyun.AliyunError as exc:
            # 没开通这个地区是常态，不该让整次采集失败；但要记下来，
            # 否则「这个地区没有数据集」和「这个地区没问过」在结果里长得一样
            skipped.append(f"{region}：{exc}")
            continue
        if not isinstance(spaces, list):
            skipped.append(f"{region}：返回缺 Workspaces，跳过")
            continue
        for ws in spaces:
            wid = str(ws.get("WorkspaceId") or "")
            if not wid:
                continue
            for d in _pai_datasets(creds, region, wid, transport=transport):
                uid = str(d.get("UserId") or "")
                owner = ram_users.get(uid, {})
                kind = OWNER_USER if owner else (OWNER_ROOT if uid == account else OWNER_GONE)
                recycled_at = ""
                if kind == OWNER_GONE and uid in bin_users:
                    owner = bin_users[uid]
                    recycled_at = str(owner.get("deleted_at") or "")
                uri = str(d.get("Uri") or "")
                out.append(
                    {
                        "region": region,
                        "workspace": wid,
                        "workspace_name": str(ws.get("WorkspaceName") or ""),
                        "id": str(d.get("DatasetId") or ""),
                        "name": str(d.get("Name") or ""),
                        "source": str(d.get("DataSourceType") or ""),
                        "accessibility": str(d.get("Accessibility") or ""),
                        "uri": uri,
                        "path": _pai_path(uri),
                        "owner_user_id": uid,
                        "owner_kind": kind,
                        "owner_login": str(owner.get("login") or ""),
                        "owner_name": str(owner.get("name") or ""),
                        # 非空 = 属主的号删了但还在回收站里，认得出是谁；
                        # kind 是 gone 而这里为空 = 保留期也过了，**再也认不出来了**
                        "owner_deleted_at": recycled_at,
                    }
                )
        if progress:
            progress(f"PAI {region}：{len(spaces)} 个工作空间，累计 {len(out)} 条数据集")
    return out, skipped


def _pai_page(creds, region, path, key, *, transport=None, extra=None) -> list:
    """ROA 接口的翻页。PageNumber 从 1 开始，拿不满一页就是最后一页。"""
    from .clouds import aliyun

    items: list = []
    for page in range(1, _MAX_PAGES + 1):
        query = {"PageSize": 100, "PageNumber": page}
        query.update(extra or {})
        got = aliyun.call_roa(
            _pai(region), aliyun.AIWORKSPACE, path, query, creds=creds, transport=transport
        ).get(key)
        if not isinstance(got, list):
            raise AssetError(f"PAI {path} 返回缺 {key}，不能当作空结果")
        items += got
        if len(got) < 100:
            return items
    raise AssetError(f"PAI {path} 页数超过上限，数据不完整，已中断")


def _pai_datasets(creds, region, workspace, *, transport=None) -> list:
    return _pai_page(
        creds,
        region,
        "/api/v1/datasets",
        "Datasets",
        transport=transport,
        extra={"WorkspaceId": workspace},
    )


def _pai_members(creds, region, workspace, *, transport=None) -> dict:
    """`{UserId: {login, name}}`。工作空间成员 —— **这是权限，不是归属**。

    归属看的是账号级 RAM 用户表（见 `collect_pai_datasets`）：被移出工作空间的人，
    他建的数据集还在、人也还在，只是不该再进这个空间了。

    只收 `AccountType == "5"`（RAM 用户）：其余是服务角色，成员表里能占到三分之二，
    混进来会让「属主是谁」这件事多出一堆永远对不上的条目。
    """
    out: dict = {}
    for m in _pai_page(
        creds, region, f"/api/v1/workspaces/{workspace}/members", "Members", transport=transport
    ):
        if str(m.get("AccountType") or "") != "5" or not m.get("AccountName"):
            continue
        out[str(m.get("UserId") or "")] = {
            "login": str(m.get("AccountName")),
            "name": str(m.get("MemberName") or m.get("DisplayName") or ""),
        }
    return out


def _pai_path(uri: str) -> str:
    """从 `bmcpfs://<挂载点>/a/b/` 里取出 `/a/b`。两种 URI 写法现网都有，别只认长的。"""
    body = str(uri or "").split("://", 1)[-1]
    slash = body.find("/")
    return body[slash:].rstrip("/") if slash >= 0 else ""


#: 成员账号里那个只读角色的名字。三个账号里都叫这个（见 identity/member-collector-policy.json）
MEMBER_ROLE = "wuji-panel-collector"


def collect_member(
    creds: aliyun.Credentials,
    account: str,
    *,
    role: str = MEMBER_ROLE,
    transport=None,
    progress=None,
) -> tuple:
    """采一个资源目录成员账号。主账号的采集身份换一份临时凭证进去。

    **不用资源目录自带的 `ResourceDirectoryAccountAccessRole`** —— 那个挂的是
    AdministratorAccess（实测确认），让长期挂在面板服务器上的采集凭证能 assume 它，
    等于把成员账号的超管钥匙放在那台机器上。每个成员账号单独建了同名只读角色，
    只信任主账号的 panel-collector。

    采不到要**抛错**，让 `build_snapshot` 把它记成这个账号的 error —— 悄悄少一个账号
    比报错糟得多：资产页会显示成「这个账号什么都没有」，而不是「没采到」。
    """
    if progress:
        progress(f"成员账号 {account}：换临时凭证")
    inner = assume_role_for(creds, account, role=role, transport=transport)
    return collect_aliyun(inner, transport=transport, progress=progress)


def assume_role_for(
    creds: aliyun.Credentials, account: str, *, role: str = MEMBER_ROLE, transport=None
) -> aliyun.Credentials:
    """进某个成员账号的只读角色。会话名要 ≥2 个字符，短了阿里云的报错看不出原因。"""
    return aliyun.assume_role(
        f"acs:ram::{account}:role/{role}", "panel-assets", creds=creds, transport=transport
    )


Job = tuple  # (platform, 凭证前缀提示, collect() -> (account, resources))


def build_snapshot(
    jobs: Iterable[Job], *, datasets=None, dataset_error: str = "", recycled=None
) -> dict:
    """`datasets` 放在快照顶层而不是塞进某个账号的 `resources` 里。

    两个原因：它跨地区跨工作空间，本来就不属于「某个账号下的某个地区」这个结构；
    而且它**自带属主**，和 `resources` 那种「归属得人工指」的东西在下游走的是两条路。
    """
    accounts = []
    for platform, hint, collect in jobs:
        try:
            account, resources = collect()
            accounts.append({"platform": platform, "account": account, "resources": resources})
        except (DeliveryError, OSError) as exc:
            first = next((ln.strip() for ln in str(exc).splitlines() if ln.strip()), "")
            first = aliyun._scrub(volcano._scrub(first)) or type(exc).__name__
            accounts.append({"platform": platform, "account": hint, "error": first[:200]})
    out = {"captured_at": _now(), "accounts": accounts}
    # 采集失败时**不写 datasets 这个键**，让下游能分出「没有数据集」和「没采到」——
    # 写成空列表的话，PAI 权限哪天掉了，所有人的数据集会一起从页面上消失而没人知道
    if datasets is not None:
        out["datasets"] = list(datasets)
    if dataset_error:
        out["dataset_error"] = dataset_error[:300]
    # 回收站有保留期。**每次采集都把它抄一份存下来**：过期之后云上就查不到了，
    # 而快照里这份会一直留着 —— 这是「过期之后还认得出那是谁的东西」的唯一办法
    if recycled is not None:
        out["recycle_bin"] = list(recycled)
    return out


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
