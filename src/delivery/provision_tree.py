"""给每个人开好「那块地方」：对象存储上的目录 + PAI 数据集。

两种存储，两种布局，理由写在 `workspace_tree` 里：
  OSS   `wuji-algo-dev-hz/<组>/<登录名>/`   新桶、空的，可以分层
  CPFS  `/<登录名>/`                        存量三十个目录全是扁平的，不能搬

**这个模块只算计划。** 真正下手的两步（往 OSS 放占位对象、建 PAI 数据集）分别是
一次写对象存储和一次写 PAI，都要调用方显式点头 —— 一次给五十个人建东西，
算错一个前缀就是五十条错的记录留在生产上。

顺带说清楚这套东西**不是什么**：它给的是台账、秩序和成本归属的依据，**不是隔离**。
DSW/DLC 里读写走的是 `AliyunPAIDSWDefaultRole`（`Resource: *`，无条件），
跟每个人自己的 RAM 策略没关系；组里那条 `AliyunOSSFullAccess` 也是全通的。
把目录结构当成权限边界，是这套东西最容易被误用的方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import workspace_tree as tree
from .clouds.oss import OssError as _OssError
from .errors import DeliveryError


class ProvisionTreeError(DeliveryError):
    """计划算不出来。"""


@dataclass(frozen=True)
class Target:
    """一个人在某种存储上该有的东西。"""

    login: str
    person: str
    prefix: str
    #: 数据集名。**一个工作空间内必须唯一**，所以同一个人的两种存储不能都叫登录名 ——
    #: 实测撞了 10 个（那些人的 CPFS 数据集正好就叫登录名）。带上存储后缀之后，
    #: 列表里也才分得清哪条是 OSS 哪条是 CPFS，否则两条同名谁也说不出区别。
    #: 形如 `chuzhong-oss`：人名在前，搜登录名时两种存储会挨在一起
    name: str
    uri: str
    source: str
    region: str
    workspace: str
    #: 属主的 RAM 数字 UserId。**不传它，建出来的每一条属主都是面板自己** ——
    #: 「PAI 数据集自带归属」当场失效，体检会说所有数据集都属于 panel-executor
    user_id: str = ""
    #: PAI 真正拿去挂载的东西。不带很可能建出一条「看得见但挂不上」的数据集
    import_info: dict = field(default_factory=dict, compare=False)

    @property
    def scope(self) -> str:
        return f"{self.region}/{self.workspace}/{self.name}"


def oss_import(bucket: str, region: str, prefix: str) -> dict:
    """OSS 数据集的 `ImportInfo`，照现网那条 `lerobot_epic` 的形状。

    **region 在这里是裸的**（`cn-hangzhou`），而 `Uri` 里是带前缀的（`oss-cn-hangzhou`）——
    同一个地域两套写法，这个坑在临时凭证那边也踩过（拼出过 `oss-oss-ap-southeast-1`）。
    """
    return {
        "bucket": bucket,
        "path": prefix.strip("/") + "/",
        "region": region.removeprefix("oss-"),
    }


def cpfs_import(fs_id: str, region: str, mount_target: str, prefix: str) -> dict:
    """CPFS 数据集的 `ImportInfo`，照现网那 30 条的形状。"""
    return {
        "path": "/" + prefix.strip("/") + "/",
        "fileSystemId": fs_id,
        "isVpcMount": True,
        "region": region,
        "mountTarget": mount_target,
    }


def oss_uri(bucket: str, region: str, prefix: str) -> str:
    """`oss://<桶>.<地域>.aliyuncs.com/<前缀>/`，和现网那条 `lerobot_epic` 同写法。"""
    return f"oss://{bucket}.{region}.aliyuncs.com/{prefix.strip('/')}/"


def cpfs_uri(mount_host: str, prefix: str) -> str:
    """`bmcpfs://<挂载点域名>/<前缀>/`。

    **照现网那 30 条的写法**，不照官方文档的 `nas://<fsid>.<region>/…` ——
    跟着文档走会让新建的和老的在控制台里长成两种东西。
    """
    return f"bmcpfs://{mount_host}/{prefix.strip('/')}/"


def plan(
    users: Iterable,
    *,
    people: Optional[dict] = None,
    services: Optional[Iterable] = None,
    layout: str,
    department_of: Optional[dict] = None,
    slugs: Optional[dict] = None,
    unbound: Optional[Iterable] = None,
    uri_of=None,
    source: str,
    region: str,
    workspace: str,
    suffix: str = "",
    user_ids: Optional[dict] = None,
    import_of=None,
) -> tuple:
    """算出要给谁建什么。返回 `(Target 列表, 跳过的原因)`。

    `uri_of(prefix)` 把前缀拼成那种存储的 URI —— 两种存储只有这一处不同，
    所以拼法作为参数传进来，而不是在这里 if/else。
    """
    slots, skipped = tree.plan(
        users,
        people=people,
        services=services,
        layout=layout,
        department_of=department_of,
        slugs=slugs,
        unbound=unbound,
    )
    targets = [
        Target(
            login=s.login,
            person=s.person,
            prefix=s.prefix,
            name=f"{s.login}-{suffix}" if suffix else s.login,
            uri=uri_of(s.prefix),
            source=source,
            region=region,
            workspace=workspace,
            user_id=str((user_ids or {}).get(s.login) or ""),
            import_info=import_of(s.prefix) if import_of else {},
        )
        for s in slots
    ]
    return targets, skipped


def storage_of(uri: str) -> str:
    """从数据集 URI 里取出「这条指向哪个存储」——OSS 是桶名，CPFS 是 `bmcpfs-…` 那段。

    判重要用它：**同一个人在同一个工作空间里的另一条数据集，未必是他在这个桶/这个
    文件系统上的那一份**。`wuji-algo-dev-hz` 是新建的空桶，此前不存在任何个人 OSS
    数据集 —— 所以属主是本人的任何一条 OSS 数据集（`lerobot_epic` 那类）必然不是
    他在新桶里的那份，只按「属主 + 存储类型」判会把他静默跳过。

    CPFS 那边相反：现网 30 条就在同一个 fs 上，只是名字不标准 —— 比 fs-id 而不是比
    挂载点域名，是因为同一个文件系统可能有多个挂载点（`-vpc-x` / `-vpc-y`）。
    """
    body = str(uri or "").split("://", 1)[-1]
    host = body.split("/", 1)[0]
    for part in host.split("."):
        if part.startswith(("bmcpfs-", "cpfs-")):
            # `cpfs-00000ub3…-vpc-egtdgw` → `00000ub3…`，两种前缀和挂载点后缀都剥掉
            return part.removeprefix("bmcpfs-").removeprefix("cpfs-").split("-vpc-")[0]
    return host.split(".")[0]


def to_create(targets: Iterable, existing: Iterable, *, location: str = "") -> tuple:
    """还没有的那些，以及**已经纳管**的那些。返回 `(要建的, 已纳管的)`。

    判重按**属主 + 存储类型**，不按名字。存量是手工建的、名字五花八门
    （`wzh` 对 `wangzihan`、`zhangwt` 对 `zhangwentao`，49 个里有一半对不上），
    按名字判的话会给这些人再建一个空目录 —— 而他们的数据在老路径下面。

    「纳管」就是这个意思：**老的不动、不改名、不搬，但面板认得出它是谁的那一份**，
    于是不会重复建。新增的才按标准来。

    同一个工作空间里重名建不出来，而不同工作空间各有一条是正常的
    （现网 8 条路径就是这么两边各一份），所以工作空间也是判重的一部分。
    """
    want = storage_of(location) if location else ""
    owned = {
        (str(d.get("workspace") or ""), str(d.get("source") or ""), str(d.get("owner_login") or ""))
        for d in existing
        if isinstance(d, dict)
        and d.get("owner_login")
        # **存储位置也得一致才算同一份。** 不比的话，这个人在别的桶里建过的任何
        # 一条数据集都会让他在这个桶里的那份被静默跳过 —— 既没有目录也没有数据集
        # **位置不明时不拿它当否定证据。** `uri` 缺失或解析不出时 `storage_of` 返回
        # 空串，判成「不是同一份」的话，一份旧版本采集器写的快照（没有 uri 字段）
        # 就会让全员重建 —— 「不知道」被当成了「不是」，正是这个仓库反复警惕的那条
        and (
            not want
            or not storage_of(str(d.get("uri") or ""))
            or storage_of(str(d.get("uri") or "")) == want
        )
    }
    named = {
        (str(d.get("workspace") or ""), str(d.get("name") or ""))
        for d in existing
        if isinstance(d, dict)
    }
    todo, managed = [], []
    for t in targets:
        if (t.workspace, t.source, t.login) in owned or (t.workspace, t.name) in named:
            managed.append(t)
        else:
            todo.append(t)
    return todo, managed


def collisions(targets: Iterable, existing: Iterable) -> list:
    """名字一样但**路径不一样**的。

    这类不能自动建也不能自动改：同名意味着建不出来，而路径不同意味着有人手工建过
    一条指向别处的。改它可能让正在跑的任务读不到东西，得人来看。
    """
    by_key = {
        (str(d.get("workspace") or ""), str(d.get("name") or "")): str(d.get("uri") or "")
        for d in existing
        if isinstance(d, dict)
    }
    out = []
    for t in targets:
        was = by_key.get((t.workspace, t.name))
        if was is not None and was.rstrip("/") != t.uri.rstrip("/"):
            out.append((t, was))
    return out


@dataclass(frozen=True)
class MoveResult:
    """一次搬目录的结果。**只复制，不删源。**"""

    login: str
    old_prefix: str
    new_prefix: str
    copied: int = 0
    #: 目的端已经有一份、而且字节一致 —— **续跑时正常**，不是错误
    already: int = 0
    #: 目的端有一份但**字节不一样**。不覆盖、不猜，交给人看
    conflicts: tuple = ()
    too_big: tuple = ()
    #: 对账：源和目的的对象数、字节数对不对得上
    matched: bool = False
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.matched and not self.too_big and not self.conflicts


def move_prefix(bucket: str, move, *, region: str, creds, oss, progress=None) -> MoveResult:
    """把一个人的目录从旧位置**复制**到新位置，然后对账。

    **不删源。** 复制是可逆的（大不了多占一份空间），删是不可逆的 —— 让可逆的自动、
    不可逆的人来，和体检清单、离职回收上定的规矩一致。面板的策略里也没有
    `oss:DeleteObject`，所以就算代码想删也删不掉。

    对账看的是**源的每个对象在目的端都存在且字节一致**，不是「两边数量相等」：
    目的端可能本来就有别的东西（比如上次搬了一半），拿数量相等当判据会让正常情况
    报失败，而运维很快就会学会忽略这个结论。

    逐对象复制不是原子的 —— 中途断了两边各有一半，而那时候「没报错的那些」看起来
    一切正常。所以**必须对完账才能说搬完了**。

    **这个账只对到字节数。** 同名同大小但内容不同的东西它分辨不出来 —— 真要那一层，
    得像 `ssh_transfer/verify.py` 那样抽样重下来逐字节比。目的路径是
    `新组/登录名/`、实践中独占，所以暂时没做；但别把它当成「内容一致」的保证。
    """
    src = list(oss.list_objects(bucket, move.old_prefix, region=region, creds=creds))
    if not src:
        return MoveResult(
            login=move.login,
            old_prefix=move.old_prefix,
            new_prefix=move.new_prefix,
            matched=True,
            note="旧目录是空的，没什么可搬",
        )
    big = {k for k, size in src if size > oss.COPY_MAX}
    # 目的端现有的，用来分辨「上次已经搬过去了」和「那边有个不一样的同名文件」
    at_dst = dict(oss.list_objects(bucket, move.new_prefix, region=region, creds=creds))
    copied = already = 0
    conflicts: list = []
    for key, size in src:
        if key in big:
            continue
        tail = key[len(move.old_prefix) :]
        dst_key = move.new_prefix + tail
        if dst_key in at_dst:
            # **这一支决定了搬目录能不能重跑。** 复制带了 `x-oss-forbid-overwrite`，
            # 目的端已存在时 OSS 回 409；要是把 409 和「网络断了」当成一回事，
            # 断过一次之后第一个对象就撞墙，后面的永远搬不过去 —— 而每次重跑
            # 结果一模一样，人只会以为「又断了」
            if at_dst[dst_key] == size:
                already += 1
            else:
                conflicts.append(key)
            continue
        try:
            oss.copy_object(bucket, key, dst_key, region=region, creds=creds)
        except _OssError as exc:
            # 并发窗口：列完之后、复制之前有人往那儿写了。按码分流，不按异常类型
            if getattr(exc, "code", "") == "FileAlreadyExists":
                conflicts.append(key)
                continue
            return MoveResult(
                login=move.login,
                old_prefix=move.old_prefix,
                new_prefix=move.new_prefix,
                copied=copied,
                already=already,
                conflicts=tuple(conflicts),
                too_big=tuple(sorted(big)),
                matched=False,
                note=f"复制到第 {copied + 1} 个（{key}）时中断：{type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            # **带着进度返回，不要让异常裸奔。** 裸抛的话调用方只看到一个错误，
            # 不知道复制了几个、停在哪个 key —— 人会倾向于从头再来，或者更糟：
            # 以为没搬成而去手工 ossutil 搞一遍
            return MoveResult(
                login=move.login,
                old_prefix=move.old_prefix,
                new_prefix=move.new_prefix,
                copied=copied,
                already=already,
                conflicts=tuple(conflicts),
                too_big=tuple(sorted(big)),
                matched=False,
                note=f"复制到第 {copied + 1} 个（{key}）时中断：{type(exc).__name__}: {exc}",
            )
        copied += 1
        if progress and copied % 50 == 0:
            progress(f"{move.login}：已复制 {copied}/{len(src) - len(big)}")
    # 对账：重新列目的端，逐个比字节数
    dst = dict(oss.list_objects(bucket, move.new_prefix, region=region, creds=creds))
    # conflicts 里那些已经单独报过了，别再进 missing 说第二遍 ——
    # 同一个对象说两遍，看的人会以为是两件事
    conflicted = set(conflicts)
    missing = [
        key
        for key, size in src
        if key not in big
        and key not in conflicted
        and dst.get(move.new_prefix + key[len(move.old_prefix) :]) != size
    ]
    return MoveResult(
        login=move.login,
        old_prefix=move.old_prefix,
        new_prefix=move.new_prefix,
        copied=copied,
        already=already,
        conflicts=tuple(conflicts),
        too_big=tuple(sorted(big)),
        matched=not missing,
        note=" · ".join(
            x
            for x in (
                f"{len(missing)} 个对象没对上" if missing else "",
                f"{len(conflicts)} 个目的端已有且字节不同" if conflicts else "",
                f"{already} 个上次已经搬过去了" if already else "",
            )
            if x
        ),
    )
