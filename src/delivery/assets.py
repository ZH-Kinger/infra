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

from .clouds import aliyun, oss, volcano
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
                    # **优先用 TypeName**（`Volcengine::VMP::Workspace`）：`ResourceType` 是
                    # 裸的产品内类型名，火山**任何产品**的工作区都叫 `Workspace` ——
                    # 只看它的话，托管 Prometheus 的工作区会被当成机器学习平台的算力空间
                    # （线上真发生了，报给管理员的「3 个火山工作空间」全是 VMP 的）
                    "type": str(r.get("TypeName") or r.get("ResourceType") or ""),
                    "service": str(r.get("Service") or ""),
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
    """RAM 用户回收站：删掉但还没过保留期的子账号。

    `IMS.ListUsersInRecycleBin`（`ims.aliyuncs.com` 2019-08-15），所需权限
    `ram:ListUsersInRecycleBin`，不支持资源级授权。

    **趁保留期内把 UserId → 姓名记下来。** 过期之后这个人在云上就只剩一个数字 UserId，
    而他留下的数据集、目录、机器还在 —— 实测已经有一条是这样了（`/cwr`，删于
    2026-08-05，回收站里已经查不到，现在没人说得清那是谁的）。所以采到之后要**并进
    上一份快照**（见 `merge_recycle_bin`）：只靠这一次的结果等于没记，保留期一过，
    那几个名字会原样消失，只是把「晚了 24 天」推迟成「晚了一个采集周期」。

    这是**离职回收缺的那半块**：体检清单原本只能回答「这个号的主人还在不在通讯录里」，
    回收站回答的是反向的那个问题 —— 号已经没了，他留下的东西还在没在。

    翻页走 `aliyun.paginate`：它对「结构不对」和「说还有下一页却不给 Marker」都抛错。
    自己写一遍很容易把这两道守卫丢掉，而丢掉的后果在这里格外难看 —— 少掉的那个人名下的
    数据集会被体检**正面断言**「保留期已过，现在没人认得出这是谁的」，那是一句错话，
    不是一句缺数据的话。
    """
    from .clouds import aliyun

    out = []
    for u in aliyun.paginate(
        *aliyun.IMS,
        "ListUsersInRecycleBin",
        key="User",
        container="Users",
        creds=creds,
        transport=transport,
        strict_key=True,
    ):
        uid = str(u.get("UserId") or "")
        if not uid:
            # 没有 UserId 就认不回任何东西。收进来的话，空串会成为一个「匹配任何缺 id 的
            # 数据集」的键 —— 那会把一个真实离职者的姓名安到一条不知属主的数据集上
            continue
        principal = str(u.get("UserPrincipalName") or "")
        out.append(
            {
                "user_id": uid,
                # 登录名是 `<名>@<UID>.onaliyun.com`，只留 @ 前面那段，和别处的登录名对得上
                "login": principal.split("@", 1)[0],
                "name": str(u.get("DisplayName") or ""),
                "deleted_at": str(u.get("RecycleDate") or ""),
                # 哪天彻底清除。**这是倒计时**：过了这天云上就再也查不到这个人了
                "purge_at": str(u.get("DeleteDate") or ""),
            }
        )
    return out


def merge_recycle_bin(old, new) -> Optional[list]:
    """把这次采到的回收站并进上一份快照里的那份。**只增不减。**

    云上的回收站有保留期，过期就查不到了。每次采集整份覆盖的话，保留期一过那几个名字
    会原样消失 —— 这个功能想解决的问题原封不动地复发。所以这里做并集，不做替换。

    `new` 是 None 表示这次没采（比如 `--skip-pai`）：**原样留着旧的**。
    绝不能因为「这次没问」就把攒下来的记录抹掉 —— 一次例行的「只刷资源中心」
    就把台账清空，是这个功能最容易死的方式。

    同一个 id 以**先记下的那条**为准：早一次采到的信息离真实删除时间更近。
    """
    if new is None:
        return list(old) if old is not None else None
    merged: dict = {}
    for entry in list(old or ()) + list(new):
        if not isinstance(entry, dict):
            continue
        uid = str(entry.get("user_id") or "")
        if uid and uid not in merged:
            merged[uid] = dict(entry)
    return sorted(merged.values(), key=lambda e: (e.get("deleted_at") or "", e["user_id"]))


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
    bin_users = {str(u.get("user_id") or ""): u for u in (recycled or ()) if u.get("user_id")}
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
            # 和地区层同一个取舍：权限不足一律中断（`AliyunDenied` 不接），
            # 其余错误记进 skipped 不让一个空间挡住其余。**不能静默跳过** ——
            # 少一个工作空间的数据集，台账看起来照样是完整的
            try:
                found = _pai_datasets(creds, region, wid, transport=transport)
            except aliyun.AliyunDenied:
                raise
            except aliyun.AliyunError as exc:
                skipped.append(f"{region} 工作空间 {wid}：{exc}")
                continue
            for d in found:
                uid = str(d.get("UserId") or "")
                owner = ram_users.get(uid, {})
                kind = OWNER_USER if owner else (OWNER_ROOT if uid == account else OWNER_GONE)
                recycled_at = ""
                if kind == OWNER_GONE and uid and uid in bin_users:
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


def _pai_store(uri: str) -> str:
    """数据集**落在哪个桶 / 哪个文件系统**。

    为什么非要单独有这个
    ────────────────────
    `_pai_path` 只给出 `/wzh`、`/general/wangzihan` 这种路径，而**路径在不同的桶里是会重名的**。
    页面上只显示路径的话，「OSS · /general/wangzihan」这一行没法回答最要紧的那个问题：
    是哪个 OSS？杭州的开发桶，还是新加坡那个？

    现网 6 种 host 形状（实测）：
      wuji-algo-dev-hz.oss-cn-hangzhou.aliyuncs.com              → wuji-algo-dev-hz
      cpfs-00000ub3ici1dnniit2i0-vpc-egtdgw.cn-hangzhou.cpfs…    → cpfs-00000ub3ici1dnniit2i0
      bmcpfs-00000ub3ici1dnniit2i0.cn-hangzhou                   → bmcpfs-00000ub3ici1dnniit2i0

    **不把 `cpfs-<id>-vpc-x` 归一成 `bmcpfs-<id>`**：智算版这两个确实指同一个文件系统，
    但通用版只有 `cpfs-` 这一种写法，归一的规则对两种版本不一样，写错了比不归一更糟。
    去掉 `-vpc-xxx` 就够了 —— 剩下的 id 段相同，人一眼看得出是同一个。
    """
    body = str(uri or "").split("://", 1)[-1]
    host = body.split("/", 1)[0]
    first = host.split(".", 1)[0]
    # CPFS 挂载点带 `-vpc-<随机>`，那是挂载地址的一部分，不是文件系统标识
    cut = first.find("-vpc-")
    return first[:cut] if cut > 0 else first


#: 数据集的可见范围。**个人目录一律 ROLE_PUBLIC**：
#: · PRIVATE 只是在 PAI 界面里藏起来，数据面照样谁都挂得到 —— 换来的是虚假的安全感
#:   加真实的不方便（别人要接手你的活时找不到东西）；
#: · PUBLIC 更糟：组里那条 `pai:*` 策略对 PUBLIC 无条件放行，**包括删除**。
#:   现网 9 条个人数据集是 PUBLIC，任何人都能删掉它们。
#: ROLE_PUBLIC 在两条语句之间，谁都删不了，而工作空间里的人看得见。
DATASET_ACCESS = "ROLE_PUBLIC"
#: `ROLE_PUBLIC` 必须同时给这张角色表（不给的话 `CreateDataset` 直接 400）。
#: 照现网那 30 条抄的 —— 工作空间里的四种角色 + 属主本人
DATASET_ROLES = (
    "PAI.WorkspaceAdmin",
    "PAI.AlgoOperator",
    "PAI.LabelManager",
    "PAI.AlgoDeveloper",
    "owner",
)


#: 挂进 DSW/DLC 时的默认路径。现网 30 条 CPFS 数据集全是这个
DATASET_MOUNT = "/mnt/data/"


def create_dataset(
    creds,
    *,
    region: str,
    workspace: str,
    name: str,
    uri: str,
    source: str,
    accessibility: str = DATASET_ACCESS,
    import_info: Optional[dict] = None,
    labels: Optional[list] = None,
    user_id: str,
    mount_path: str = DATASET_MOUNT,
    transport=None,
) -> str:
    """建一条 PAI 数据集，返回 DatasetId。**这是写操作。**

    `CreateDataset` 只**登记一个指针**，不会去创建底层那个目录（文档要求填「已有的
    存储路径」）。OSS 那边无所谓——前缀是虚的，写第一个对象时自然就有；CPFS 那边
    要目录真的存在，得另外想办法。

    `source` 用 `BMCPFS` / `OSS`，`uri` **照现网已有那 30 条的写法**，不照文档示例：
    文档给的 CPFS 格式是 `nas://<fsid>.<region>/...`，而现网全是
    `bmcpfs://cpfs-…-vpc-x.<region>.cpfs.aliyuncs.com/<路径>/`。跟着存量走，
    不然新建的和老的在控制台里会长成两种东西。

    **不提供删除。** `DeleteDataset` 很可能连底层目录一起删（官方文档对底层存储的影响
    只字未提，只写了「一旦删除，则不可恢复」），而离职交接最常见的情况恰恰是数据要留给
    接手的人。要删只能人到控制台删，且先确认数据已转移。
    """
    from .clouds import aliyun

    body = {
        "Name": name,
        "Uri": uri,
        "DataSourceType": source,
        "Property": "DIRECTORY",
        "Accessibility": accessibility,
        "WorkspaceId": workspace,
        # 挂载路径。不给的话现网那 30 条的形状就对不上了
        "Options": json.dumps({"mountPath": mount_path}, separators=(",", ":")),
    }
    if accessibility == DATASET_ACCESS:
        body["AccessibleRoleIdList"] = list(DATASET_ROLES)
    if import_info:
        # **PAI 真正拿去挂载的东西。** 不带的话很可能建出一条「看得见但挂不上」的数据集
        body["ImportInfo"] = json.dumps(import_info, ensure_ascii=False, separators=(",", ":"))
    if labels:
        body["Labels"] = labels
    # 数据集的属主。**空串必须抛，不能静默不带** —— 不带的话 PAI 会把属主记成调用者
    # （面板自己），而 `UpdateDataset` 事后改 UserId 是**静默无效**的、`DeleteDataset`
    # 面板又刻意不实现，于是只能去控制台手删重建。线上已经因为这个返工过一次
    # （49 条属主全是 panel-executor），根因就是「加了参数但调用方没传」。
    # 要设它，调用者得是工作空间的 Owner 或 Admin
    if not user_id:
        raise AssetError(
            f"建数据集 {name} 没有属主（user_id 为空）。"
            "**不建**：建出来属主会是面板自己，而且事后改不回来，只能去控制台删了重建。"
            "先确认这个人的 RAM 登录名在 ListUsers 里查得到"
        )
    body["UserId"] = user_id
    reply = aliyun.call_roa(
        f"aiworkspace.{region}.aliyuncs.com",
        aliyun.AIWORKSPACE,
        "/api/v1/datasets",
        creds=creds,
        transport=transport,
        method="POST",
        body=body,
    )
    got = str(reply.get("DatasetId") or "")
    if not got:
        raise AssetError(f"建数据集 {name} 没有返回 DatasetId，不确认是否建成：{str(reply)[:200]}")
    return got


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


def collect_buckets(creds, *, region: str = "oss-cn-hangzhou", transport=None) -> list:
    """这个主账号下所有 OSS 桶。`region` 只是拨号用的，ListBuckets 返回的是全地域。

    体检里「桶在云上但没登记在任何白名单里」那条要用它。**采不到就抛**，
    由上层记成 `bucket_error` —— 绝不能返回空清单，那会被下游当成
    「云上一个桶都没有」，于是那条检查永远报「没问题」。
    """
    return oss.list_buckets(region=region, creds=creds, transport=transport)


def build_snapshot(
    jobs: Iterable[Job],
    *,
    datasets=None,
    dataset_error: str = "",
    recycled=None,
    buckets=None,
    bucket_error: str = "",
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
    # 同 datasets：**采不到就不写这个键**。写成空列表的话，哪天 oss:ListBuckets 掉了，
    # 体检里「没登记的桶」会变成一片干净 —— 而那正是它该报警的时候
    if buckets is not None:
        out["buckets"] = list(buckets)
    if bucket_error:
        out["bucket_error"] = bucket_error[:300]
    return out


def regions_in_use(snapshot: Optional[dict]) -> dict:
    """资产快照里**实际有东西**的地域：`{地域: {资源类型: 个数}}`。

    为什么这件事值得单独算
    ──────────────────────
    「哪些地域有我们的资源」以前没有任何地方回答得了。面板只认登记过的地域
    （`workspaces.json`），而人在控制台上随手开一台机器、建一个桶，面板完全不知道 ——
    那台机器没人管、出了事也没人知道它归谁。

    **数据是现成的**：阿里云资源中心一次返回所有资源并带 `RegionId`，火山同理。
    这里只是按地域归个类，不额外打任何接口。

    全局资源（地域字段为空，比如 OSS 的账号级配置）不算进来 —— 它们不属于任何地域。
    """
    out: dict = {}
    #: 不属于任何地域的东西。资源中心把 RAM 用户、策略这些标成 `global`，
    #: 它们本来就不该出现在「哪个地域有资源」这个问题的答案里
    skip = {"global", "cn-global", "all"}
    for account in (snapshot or {}).get("accounts") or []:
        platform = str(account.get("platform") or "")
        for r in account.get("resources") or []:
            region = str(r.get("region") or "").strip()
            if not region or region.lower() in skip:
                continue
            slot = out.setdefault(f"{platform}/{region}", {})
            kind = str(r.get("type") or "未知类型")
            slot[kind] = slot.get(kind, 0) + 1
    for bucket in (snapshot or {}).get("buckets") or []:
        region = str(bucket.get("region") or "").strip().replace("oss-", "")
        if region:
            slot = out.setdefault(f"aliyun/{region}", {})
            slot["OSS 桶"] = slot.get("OSS 桶", 0) + 1
    return out


#: 工作空间那几类资源的**完整类型名**（真机核过的，见下）。只有它们能回答
#: 「该不该在这个地域开工作区」—— 一个地域里有 VPC、有网卡不代表那儿要有工作空间。
#:
#: **必须全名匹配**：按子串 `"pai"` 匹配会把火山的 `keypair` 算进来（真踩过）。
PAI_TYPES = frozenset(
    {
        "ACS::PAIWorkspace::Workspace",
        "ACS::PAIWorkspace::Dataset",
        "Volcengine::MLPlatform::Workspace",
    }
)
#: 工作空间本身（不含数据集）—— 按 ID 对账用。
#: **火山这边曾经写成裸的 `Workspace`**，结果把托管 Prometheus（VMP）的工作区
#: 当成了算力空间报给管理员。火山任何产品的工作区在 `ResourceType` 里都叫 `Workspace`，
#: 能区分产品的是 `TypeName`：机器学习平台是 `Volcengine::MLPlatform::*`。
WORKSPACE_TYPES = frozenset({"ACS::PAIWorkspace::Workspace", "Volcengine::MLPlatform::Workspace"})


#: 配额查不到时的三态。**「看不到」不是「没有」** —— 见 `quotas_by_workspace`
CARDS_YES, CARDS_NO, CARDS_UNKNOWN = "yes", "no", "unknown"


def quotas_by_workspace(creds, regions, *, transport=None) -> tuple:
    """每个工作空间有多少张卡：`({工作空间ID: {"gpu": 卡数, "quotas": [名字]}}, 没查成的地域)`。

    **接口按调用者的工作空间成员身份裁剪返回**（真机实测：同一个接口，三把凭证看到的
    条数各不相同）。所以「这个空间查不到配额」有两种可能 —— 它真没有专属算力，
    或者采集身份不在这个空间里。而我们要判的恰恰是**没登记的空间**，采集身份
    大概率就不在里面。两者绝不能混：混了就会把一个有 144 张卡的空间当成空壳过滤掉。

    调用方据此分三态（`CARDS_*`）：查得到且有卡 / 查得到且没卡 / 没查成。

    地域打不通（PAI 没有河源接入点、张家口实测 503）记进第二个返回值 ——
    那一整个地域的结论都是「不知道」，不是「没卡」。
    """
    from .clouds import aliyun

    found: dict = {}
    skipped = []
    for region in regions:
        try:
            got = aliyun.call_roa(
                f"pai.{region}.aliyuncs.com",
                aliyun.PAISTUDIO,
                "/api/v1/quotas/",
                {"PageSize": 100, "PageNumber": 1},
                creds=creds,
                transport=transport,
            )
        except Exception as exc:  # noqa: BLE001 — 一个地域不通不该让其余地域没结论
            skipped.append(f"{region}：{str(exc)[:80]}")
            continue
        for q in got.get("Quotas") or []:
            detail = (q.get("QuotaDetails") or {}).get("ActualMinQuota") or {}
            try:
                gpu = int(detail.get("GPU") or 0)
            except (TypeError, ValueError):
                gpu = 0
            for ws in q.get("Workspaces") or []:
                wid = str(ws.get("WorkspaceId") or "")
                if not wid:
                    continue
                slot = found.setdefault(wid, {"gpu": 0, "quotas": []})
                slot["gpu"] += gpu
                slot["quotas"].append(f"{q.get('QuotaName') or q.get('Name') or ''}×{gpu}卡")
    return found, skipped


def cards_state(workspace_id: str, quotas: dict, skipped) -> str:
    """这个工作空间有没有卡：`yes` / `no` / `unknown`。

    有任何地域没查成时，**查不到的空间一律算 `unknown`** —— 宁可多列一个让人看一眼，
    也不要把一个真有卡的空间判成空壳藏起来。
    """
    got = quotas.get(str(workspace_id or ""))
    if got and got.get("gpu"):
        return CARDS_YES
    if got:
        return CARDS_NO
    return CARDS_UNKNOWN if skipped else CARDS_NO


def workspaces_in_use(snapshot: Optional[dict]) -> list:
    """云上实际存在的工作空间：`[{platform, region, id, name}]`。

    **按工作空间 ID 而不是按地域**：一个地域可以有好几个工作空间（杭州就有两个），
    按地域比的话，只要那个地域登记过任意一个，其余的全被判成「已登记」而漏掉 ——
    漏掉的那个里面有人在跑任务、有数据集，却不在任何申请流程里。
    """
    out = []
    for account in (snapshot or {}).get("accounts") or []:
        platform = str(account.get("platform") or "")
        for r in account.get("resources") or []:
            if str(r.get("type") or "") not in WORKSPACE_TYPES:
                continue
            out.append(
                {
                    "platform": platform,
                    "region": str(r.get("region") or ""),
                    "id": str(r.get("id") or ""),
                    "name": str(r.get("name") or ""),
                }
            )
    return sorted(out, key=lambda w: (w["platform"], w["region"], w["name"]))


def unregistered_workspaces(snapshot: Optional[dict], known_ids: Iterable[str]) -> list:
    """云上有、登记表里没有的工作空间。**只报不动**（理由同 `unregistered_regions`）。"""
    seen = {str(x or "").strip() for x in known_ids} - {""}
    return [w for w in workspaces_in_use(snapshot) if w["id"] not in seen]


def pai_regions(snapshot: Optional[dict]) -> dict:
    """有 PAI 工作空间 / 数据集的地域：`{平台/地域: {类型: 个数}}`。

    这是「新增地区」真正要看的信号。别的资源（VPC、ECS、网卡）属于另一个问题
    ——「这些东西是谁的、还要不要」，那是资产页要回答的，不该混在一起。
    """
    out: dict = {}
    for where, kinds in regions_in_use(snapshot).items():
        mine = {k: n for k, n in kinds.items() if k in PAI_TYPES}
        if mine:
            out[where] = mine
    return out


def unregistered_regions(snapshot: Optional[dict], known: Iterable[str]) -> list:
    """有资源、但面板没登记的地域。返回 `[(平台/地域, {类型: 个数})]`，按资源多的排前。

    **`known` 里的每一项都要带平台**（`aliyun/cn-shanghai`）：不带的话火山的 `cn-shanghai`
    会把阿里的挡掉 —— 两朵云的地域名大量重合，混成一个集合就是互相遮蔽。

    **只报不动**：面板不会自己去纳管一个没人确认过的地域 —— 那意味着往一个
    没人看过的地方开目录、建数据集、放人进去。这里要的是「你知道那儿有东西吗」。
    """
    seen = {str(k or "").strip().replace("oss-", "") for k in known} - {""}
    rows = [
        (where, kinds) for where, kinds in regions_in_use(snapshot).items() if where not in seen
    ]
    return sorted(rows, key=lambda row: -sum(row[1].values()))


def load(path: Optional[str]) -> Optional[dict]:
    if not path or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError(f"读不了资产快照：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise AssetError("资产快照格式不对")
    for key in ("datasets", "recycle_bin"):
        # 缺这个键是正常的（没采过）；**在但不是列表**就是文件坏了，别放它进下游 ——
        # 下游只会拿到一个 AttributeError，然后整页 500，而没人知道是文件坏了
        if key in data and not isinstance(data[key], list):
            raise AssetError(f"资产快照里的 {key} 不是列表，文件可能坏了")
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


def datasets_view(datasets, *, logins=None, labels=None) -> dict:
    """数据集当资产看。`logins` 给员工那边（只看自己的），`None` 是管理员视角。

    **这是唯一一类归属自带的资产。** ECS、OSS 桶那些，资源中心一个归属标签都不给
    （实测 6319 个资源零命中），所以只能靠 `asset-owners.json` 一条条人工指；而数据集的
    `UserId` 就是建它的那个 RAM 用户，采集时已经换成登录名了。所以这一栏对员工来说
    **一上来就是满的**，不用等管理员指派 —— 那也是它值得单独成一栏、而不是混进
    `resources` 里的原因。

    `datasets` 是 `None` 表示**没采到**（比如没跑过 `assets collect`，或者 PAI 那几个
    权限缺了）。那和「你没有数据集」是两回事：前者要去修采集，后者是正常状态。
    """
    if datasets is None:
        return {"collected": False, "items": [], "abandoned": 0}
    mine = {str(x or "").lower() for x in (logins or ())} - {""}
    label = labels or (lambda platform, account: account)
    items, abandoned = [], 0
    for d in datasets:
        if not isinstance(d, dict):
            continue
        owner = str(d.get("owner_login") or "")
        kind = str(d.get("owner_kind") or "")
        if kind == OWNER_GONE:
            abandoned += 1
        if logins is not None and owner.lower() not in mine:
            continue
        items.append(
            {
                "name": str(d.get("name") or ""),
                "region": str(d.get("region") or ""),
                "workspace": str(d.get("workspace") or ""),
                "workspace_name": str(d.get("workspace_name") or ""),
                "source": str(d.get("source") or ""),
                "path": str(d.get("path") or ""),
                "uri": str(d.get("uri") or ""),
                # 从 uri 现算，不依赖采集时有没有存 —— 旧快照照样显示得出来
                "store": _pai_store(str(d.get("uri") or "")),
                "accessibility": str(d.get("accessibility") or ""),
                "owner_login": owner,
                "owner_name": str(d.get("owner_name") or ""),
                "owner_kind": kind,
                "owner_deleted_at": str(d.get("owner_deleted_at") or ""),
                "account_label": label("aliyun", str(d.get("workspace") or "")),
            }
        )
    items.sort(key=lambda x: (x["region"], x["workspace"], x["name"]))
    return {
        "collected": True,
        "items": items,
        # 只有管理员那边有意义：员工看自己的，看不到别人遗弃的
        "abandoned": abandoned if logins is None else 0,
    }


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
