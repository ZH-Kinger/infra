"""把审批通过的迁移单真正跑起来：提交、轮询、收尾。

这一层是**定时任务的入口**，不是引擎。引擎在 `clouds/mgw.py`（搬进 OSS）和
`clouds/dms.py`（搬进 TOS），纯逻辑的状态机在 `moves.py`。这里只干三件事：
挑出该动的单子、给引擎凑齐参数、把结果写回单子。

为什么单独一个定时器，不挂在 `delivery-sweep` 上
────────────────────────────────────────────────
`delivery-sweep` 一分钟一轮，它的活是同步审批、回收到期权限 —— 都是轻的。
搬运不一样：一趟迁移动辄几小时，每轮要给每张在途单子打一次云 API。
一分钟一次没有任何意义（进度不会那么快变），只是白白把调用量乘以 5。
所以另开一个 5 分钟的定时器。

单子的状态怎么走
────────────────
飞书审批通过 → `flows` 把单子置成 **FULFILLING**（模板是 `transfer`，
`awaits_human` 返回 True）→ 本模块接手：

    FULFILLING + move_stage 空/new   → 估算、过门、提交 → move_stage=running
    FULFILLING + move_stage=running  → 轮询 → 完成则单子进 DONE
    FULFILLING + move_stage=review   → 等管理员点「确认搬运」，本模块不碰
    FULFILLING + move_stage=failed   → 停在这儿，管理员看得见，能重试

**失败不把单子推进 FAILED。** 状态机里 `FULFILLING` 只通向 `DONE` 和 `CLOSED`，
而且这也更诚实：搬失败了这张单确实还没办完，需要人来决定是重试还是放弃。
错误记在 `move_error` 上，管理员在「待开通」里直接看得到。

为什么大任务要再拦一道
──────────────────────
飞书审批人看到的是**两个路径**，不是**多少数据**。一个以为是 100GB、
实际 100TB 的迁移，批的时候看不出区别。所以超过阈值（或者根本量不出来）的，
停在 `review` 等管理员点一下 —— 他在面板上能看到真实体积。
小任务不受影响，直接搬，这才是「审批通过就自动做」。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import catalog as catalog_mod
from . import move_creds, moves
from . import sources as sources_mod
from . import tickets as t
from .errors import DeliveryError

#: 迁移服务的接入点。**只写地域主机名，别在前面拼 userid** —— SDK 会拼，
#: 拼两遍的表现是 SSL 证书 hostname 不匹配，报错里看不出是配置问题
DEFAULT_MGW_ENDPOINT = "cn-beijing.mgw.aliyuncs.com"
DEFAULT_MGW_REGION = "cn-beijing"

STAGE_REVIEW = "review"


class MoverError(DeliveryError):
    """迁移任务跑不起来。"""


@dataclass(frozen=True)
class Config:
    """跑迁移要的那几样配置。凭证不在这里 —— 那些走 `provision.executor_from_env`。"""

    mgw_endpoint: str = DEFAULT_MGW_ENDPOINT
    mgw_region: str = DEFAULT_MGW_REGION
    #: 目的 OSS 地址用的 RAM 角色名。迁移服务拿这个角色去写目的桶，所以**不需要 AK**
    oss_role: str = ""
    #: 火山账号 ID。搬进 TOS 时要拿这个账号的执行身份
    volcano_account: str = ""
    #: 第三方源登记表。**里面是别人的 AK/SK**，600 权限
    sources_path: str = sources_mod.DEFAULT_PATH
    #: 跨云源端只读凭证的时间窗（天）。见 `move_creds` 模块开头
    cred_days: int = move_creds.DEFAULT_DAYS

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> Config:
        env = os.environ if environ is None else environ
        return cls(
            mgw_endpoint=env.get("DELIVERY_MGW_ENDPOINT", "") or DEFAULT_MGW_ENDPOINT,
            mgw_region=env.get("DELIVERY_MGW_REGION", "") or DEFAULT_MGW_REGION,
            oss_role=env.get("DELIVERY_TRANSFER_OSS_ROLE", ""),
            volcano_account=env.get("DELIVERY_TRANSFER_VOLCANO_ACCOUNT", ""),
            sources_path=(env.get("DELIVERY_TRANSFER_SOURCES", "") or sources_mod.DEFAULT_PATH),
            cred_days=move_creds.days_from_env(env),
        )


def pending(store) -> list:
    """该本模块管的单子：审批已过、停在「待开通」、而且是迁移类。

    **按 kind 挑，不按模板 id 挑。** 以后多一个迁移模板（比如专门的跨云入口），
    不该因为这里写死了 `oss-move` 就永远不被搬。
    """
    out = []
    for ticket in store.all():
        if ticket.get("status") != t.FULFILLING:
            continue
        if ticket.get("kind") != catalog_mod.KIND_TRANSFER:
            continue
        out.append(ticket)
    return out


def _creds(platform: str, account: str, *, executor=None):
    """拿执行身份的凭证。执行器对象自己藏着 `_creds`，这里只借它的加载逻辑。"""
    from .provision import executor_from_env

    make = executor_from_env if executor is None else executor
    got = make(platform, account)
    creds = getattr(got, "_creds", None)
    if creds is None:
        raise MoverError(f"{platform} {account} 的执行身份没有可用凭证")
    return creds


def _third_party(ticket_src: dict, config: Config) -> tuple:
    """第三方源：从登记表里取凭证。**申请单里只有标识，钥匙在 600 文件里。**

    返回 `(src 字典, ak, sk, endpoint)`。登记表里的 `prefix` 是这个源的根，
    申请人填的前缀拼在它下面 —— 越不出去。
    """
    registry = sources_mod.load(config.sources_path)
    row = sources_mod.resolve(registry, ticket_src["bucket"])
    return (
        {
            "scheme": "s3compat",
            "bucket": row["bucket"],
            "region": row["region"],
            "prefix": sources_mod.confine(row, ticket_src.get("prefix")),
        },
        row["access_key_id"],
        row["access_key_secret"],
        row["endpoint"],
    )


def _regions(ticket: dict) -> dict:
    """桶名 → 地域，从申请单里那份模板快照来。

    路径串里带不出地域（`oss://桶/目录/` 就这么多信息），而两个引擎都要它：
    OSS 地址要拼 `oss-<地域>.aliyuncs.com`，火山查进度必须用建任务时那个地址域。
    提交时 `_validate_transfer` 已经强制「桶必须在模板清单里」，所以这里查得到。

    **同名不同地域的一律不给答案。** 模板的桶清单只有名字和地域、没有云的标识，
    而同一个名字两朵云都可能有（`wuji-ego-processed` 就是：阿里杭州一个、火山上海一个）。
    按名字取的话会静默取到其中一个 —— 搬到错的云、错的地域，而且一路都不报错。
    """
    out: dict = {}
    for row in (ticket.get("template") or {}).get("buckets") or []:
        name = str(row.get("name") or "")
        region = str(row.get("region") or "")
        if name in out and out[name] != region:
            out[name] = ""  # 有歧义 → 当成查不到，由 `_locate` 报出来
        elif name not in out:
            out[name] = region
    return out


def _locate(plan: dict, ticket: dict) -> dict:
    """把地域填进 plan 的 src / dest。

    **查不到地域就停下**。拿空地域往下走的话，OSS 域名会拼成 `oss-.aliyuncs.com`，
    火山则会在一个错误的地域里建任务 —— 后者更糟：任务真的建出来了，
    但之后每一次查进度都返回「查不到」，而查不到不算失败，单子会一直显示在途。
    """
    where = _regions(ticket)
    for side in ("src", "dest"):
        node = plan[side]
        if node["scheme"] == "src":
            continue  # 第三方源的地域在登记表里，不在模板清单里
        if node["scheme"] in ("cpfs", "vepfs"):
            # 并行文件系统的地域在模板的 `filesystems` 登记表里，不在桶表里。
            # **不跳过的话每一张预热 / 沉降单都在这里被挡死**：文件系统 id 永远查不到桶，
            # 每 5 分钟报一次「查不到桶的地域」（审计 H-A）
            node["region"] = _filesystem(ticket, node["bucket"])["region"]
            continue
        region = where.get(node["bucket"], "")
        if not region:
            raise MoverError(
                f"模板里查不到桶 {node['bucket']} 的地域（没登记，或者同名的有好几个地域），"
                "不知道该往哪个地域发请求"
            )
        node["region"] = region
    return plan


def _measure(plan: dict, *, aliyun_creds, config: Config) -> tuple:
    """量一下要搬多少。返回 `(字节, 对象数, 准不准)`。

    **量不出来第三个返回值是 False，调用方必须当成大任务。** 返回 (0,0) 的话
    审批门恒为 False，一个 100TB 的任务会被当成 0 字节直接放行。
    """
    src = plan["src"]
    if src["scheme"] != "oss":
        # 火山 TOS 和第三方 S3 都没有列举实现，量不出来
        return 0, 0, False
    from .clouds import mgw

    return mgw.estimate(
        src["bucket"], src.get("prefix", ""), region=src.get("region", ""), creds=aliyun_creds
    )


def _source_creds(plan: dict, ticket: dict, *, config: Config, issuer=None) -> tuple:
    """跨云时交给对方云的源端凭证。**现场签一把只读的，不交常驻身份。**

    为什么不能用开通身份：那串 AK 会被写进对方云的迁移任务配置里长期留存、撤不回来，
    而开通身份能 `ram:CreateUser` —— 泄漏面不是「这批数据」，是整个账号。
    细节见 `move_creds` 模块开头。

    签出来的账号名记回单子（`move_cred_user`），搬完由 `_drop_cred` 删掉。
    """
    from . import move_creds

    cloud = move_creds.needed(plan)
    if not cloud:
        return "", "", ""
    account = _account_for(cloud, ticket, config)
    make = issuer if issuer is not None else _issuer_from_env
    src = plan["src"]
    key, secret, user = move_creds.mint(
        make(cloud, account),
        ticket_id=str(ticket.get("id") or ""),
        bucket=str(src.get("bucket") or ""),
        prefix=str(src.get("prefix") or ""),
        platform=cloud,
        now=time.time(),
        days=config.cred_days,
    )
    return key, secret, user


def _submit_dataflow(plan: dict, ticket: dict, *, config: Config, executor=None) -> str:
    """并行文件系统的预热 / 沉降。返回任务号。

    **地域从模板里的文件系统登记表拿**，不从地址里猜 —— `cpfs://<fs-id>/<目录>/`
    这个串里没有地域，猜的话只能默认一个，而请求发错地域的回复是「文件系统不存在」，
    那句话会把人引去查文件系统有没有被删。
    """
    from .clouds import nas, vepfs

    fs, store = plan["fs"], plan["store"]
    row = _filesystem(ticket, fs["bucket"])
    same_name = str((ticket.get("payload") or {}).get("overwrite") or "skip")

    if plan["engine"] == "nas":
        creds = _creds(
            "aliyun", str((ticket.get("template") or {}).get("account") or ""), executor=executor
        )
        rows = nas.list_dataflows(fs_id=fs["bucket"], region=row["region"], creds=creds)
        return nas.submit(
            fs_id=fs["bucket"],
            region=row["region"],
            creds=creds,
            **dataflow_task(rows, plan, same_name=same_name),
        )

    if not config.volcano_account:
        raise MoverError("没配 DELIVERY_TRANSFER_VOLCANO_ACCOUNT，动不了火山的文件系统")
    creds = _creds("volcano", config.volcano_account, executor=executor)
    return vepfs.submit(
        fs_id=fs["bucket"],
        region=row["region"],
        action=plan["action"],
        bucket=store["bucket"],
        prefix=store["prefix"],
        sub_path=fs["prefix"],
        same_name=same_name,
        creds=creds,
    )


def dataflow_task(rows: list, plan: dict, *, same_name: str = "skip") -> dict:
    """CPFS 任务的参数：选哪条绑定、两个目录各是什么。纯函数，方便单测和真机预检共用。

    **两个目录都要写成相对绑定根的路径，而且按方向对调**（接口文档：
    `DstDirectory` 在 Import 时相对 `FileSystemPath`，Export 时相对 `SourceStoragePath`）：

        预热 Import   Directory = OSS 那头（相对 SourceStoragePath）
                      DstDirectory = CPFS 那头（相对 FileSystemPath）
        沉降 Export   Directory = CPFS 那头   DstDirectory = OSS 那头

    传绝对路径的后果是读写错目录、源为空时还报「完成、0 个文件」（审计 H-B）。
    方向搞反的后果是把数据往相反方向覆盖一遍 —— 两样都不报错，所以都要单测钉死。
    """
    from .clouds import nas

    fs, store = plan["fs"], plan["store"]
    flow = nas.resolve(
        rows, fs_path=fs["prefix"], bucket=store["bucket"], oss_prefix=store["prefix"]
    )
    cpfs_dir = nas.relative(fs["prefix"], flow["fs_path"])
    oss_dir = nas.relative(store["prefix"], flow.get("oss_path") or "/")
    importing = plan["action"] == nas.ACTION_IMPORT
    return {
        "dataflow": flow["id"],
        "action": plan["action"],
        "directory": oss_dir if importing else cpfs_dir,
        "dst_directory": cpfs_dir if importing else oss_dir,
        # 枚举是 SKIP_THE_FILE / KEEP_LATEST / OVERWRITE_EXISTING（审计 M-3：
        # 原先写的 OVERWRITE_EXISTING_FILES 不在枚举里，选「覆盖」会被回参数错误）
        "conflict": "OVERWRITE_EXISTING" if same_name == "overwrite" else nas.DEFAULT_CONFLICT,
    }


def _filesystem(ticket: dict, fs_id: str) -> dict:
    """模板里登记的这个文件系统。**没登记就抛** —— 猜地域的代价见 `_submit_dataflow`。"""
    rows = (ticket.get("template") or {}).get("filesystems") or []
    hit = next((r for r in rows if str(r.get("id") or "") == fs_id), None)
    if hit is None:
        known = "、".join(sorted(str(r.get("id") or "") for r in rows)) or "（一个都没有）"
        raise MoverError(f"模板里没登记文件系统 {fs_id}（已登记：{known}），不知道它在哪个地域")
    return hit


def _remember(minted: Optional[dict], user: str, cloud: str) -> None:
    """把签出来的子账号名交给调用方去记。

    **必须记，而且要在提交之前记。** 提交那一步失败时账号已经建出来了 ——
    不记的话云上留下一个谁也对不上的子账号（名字按单号定，但没人知道去哪儿找）。
    """
    if minted is not None and user:
        minted["move_cred_user"] = user
        minted["move_cred_cloud"] = cloud


def _account_for(cloud: str, ticket: dict, config: Config) -> str:
    """这朵云上用哪个账号签。"""
    if cloud == "volcano":
        if not config.volcano_account:
            raise MoverError("没配 DELIVERY_TRANSFER_VOLCANO_ACCOUNT，签不出火山那边的源端凭证")
        return config.volcano_account
    return str((ticket.get("template") or {}).get("account") or "")


def _issuer_from_env(platform: str, account: str):
    from .provision import executor_from_env

    return executor_from_env(platform, account, issuer=True)


def _drop_cred(ticket: dict, *, config: Config, issuer=None) -> list:
    """搬完（或失败）把源端那把钥匙删掉。**不等时间窗到期。**

    窗是兜底，不是回收手段 —— 一把还能用两周的钥匙躺在对方云的任务配置里，
    和「我们已经搬完了」这件事没有任何关系。
    """
    from . import move_creds

    user = str(ticket.get("move_cred_user") or "")
    if not user:
        return []
    cloud = str(ticket.get("move_cred_cloud") or "")
    if not cloud:
        return []
    make = issuer if issuer is not None else _issuer_from_env
    return move_creds.drop(make(cloud, _account_for(cloud, ticket, config)), user)


def _submit(
    plan: dict,
    name: str,
    ticket: dict,
    *,
    config: Config,
    executor=None,
    issuer=None,
    minted: Optional[dict] = None,
) -> str:
    """按方向挑引擎、凑参数、提交。返回云上的任务标识。

    跨云时会**现场签一把只读的源端凭证**，签出来的子账号名写进 `minted`
    （调用方要把它记回单子，否则那个账号就成了查无此人的残留）。
    """
    from .clouds import dms, mgw

    payload = ticket.get("payload") or {}
    same_name = str(payload.get("overwrite") or "skip")
    account = str((ticket.get("template") or {}).get("account") or "")
    src, dest = plan["src"], plan["dest"]

    if plan["engine"] in ("nas", "vepfs"):
        # 预热 / 沉降。**不跨云，也就没有钥匙要交出去** —— 两头都在同一朵云里
        return _submit_dataflow(plan, ticket, config=config, executor=executor)

    if plan["engine"] == "mgw":
        creds = _creds("aliyun", account, executor=executor)
        if not config.oss_role:
            raise MoverError("没配 DELIVERY_TRANSFER_OSS_ROLE —— 迁移服务要靠这个 RAM 角色写目的桶")
        key = secret = ""
        if src["scheme"] == "src":
            src, key, secret, _endpoint = _third_party(src, config)
        elif src["scheme"] == "tos":
            # **先问云上有没有这张单的任务，有就复用、不签新钥匙。** 先签后查的话，
            # 签新钥匙那一步会把上一把撤掉，而云上那个任务的源地址里存的正是上一把 ——
            # 任务照样启动，然后全部 403（审计 H-C）
            where = dict(
                user_id=account,
                endpoint=config.mgw_endpoint,
                region=config.mgw_region,
                creds=creds,
                job_name=name,
            )
            if mgw.job_exists(**where):
                # 复用也要把钥匙名记下：上一轮可能在「签出来」和「写回单子」之间被杀掉，
                # 单子上没有名字的话 `reclaim` 永远找不到它（审计 R2）。名字按单号定，
                # 算得出来；云上万一没有这个号，撤的时候「不存在」按成功算
                _remember(minted, move_creds.user_name(str(ticket.get("id") or "")), "volcano")
                return mgw.relaunch(**where)
            # 跨云：这把钥匙要交给**阿里**的迁移服务去读火山的桶
            key, secret, cred_user = _source_creds(plan, ticket, config=config, issuer=issuer)
            _remember(minted, cred_user, "volcano")
        return mgw.submit(
            user_id=account,
            endpoint=config.mgw_endpoint,
            region=config.mgw_region,
            creds=creds,
            job_name=name,
            src=src,
            dest=dest,
            same_name=same_name,
            oss_role=config.oss_role,
            src_key=key,
            src_secret=secret,
        )

    if not config.volcano_account:
        raise MoverError("没配 DELIVERY_TRANSFER_VOLCANO_ACCOUNT，搬不进 TOS")
    volcano = _creds("volcano", config.volcano_account, executor=executor)
    if src["scheme"] == "oss":
        # 同上：云上已有这张单的任务就直接返回它，**不重签** —— 重签会撤掉它正在用的那把
        found = dms.existing(region=str(dest.get("region") or ""), creds=volcano, job_name=name)
        if found is not None:
            _remember(minted, move_creds.user_name(str(ticket.get("id") or "")), "aliyun")
            return found
        # 跨云：这把钥匙要交给**火山**的迁移服务去读阿里的桶
        key, secret, cred_user = _source_creds(plan, ticket, config=config, issuer=issuer)
        _remember(minted, cred_user, "aliyun")
    else:
        key = getattr(volcano, "access_key_id", "")
        secret = getattr(volcano, "secret_access_key", "") or getattr(
            volcano, "access_key_secret", ""
        )
    return dms.submit(
        region=str(dest.get("region") or ""),
        creds=volcano,
        job_name=name,
        src=src,
        dest=dest,
        same_name=same_name,
        src_key=key,
        src_secret=secret,
    )


def _poll(ticket: dict, *, config: Config, executor=None) -> dict:
    from .clouds import dms, mgw

    account = str((ticket.get("template") or {}).get("account") or "")
    # 轮询用 `move_ref`（火山那边是数字 task_id），不是任务名。
    # 老单子没有这个字段，回落任务名 —— 阿里那条两者本来就相等
    job = str(ticket.get("move_ref") or ticket.get("move_job") or "")
    engine = str(ticket.get("move_engine") or "")
    if engine in ("nas", "vepfs"):
        return _poll_dataflow(ticket, job, engine, config=config, executor=executor)
    if engine == "mgw":
        return mgw.poll(
            user_id=account,
            endpoint=config.mgw_endpoint,
            region=config.mgw_region,
            creds=_creds("aliyun", account, executor=executor),
            job_name=job,
        )
    return dms.poll(
        region=_dest_region(ticket),
        creds=_creds("volcano", config.volcano_account, executor=executor),
        job_name=job,
    )


def _poll_dataflow(ticket: dict, task: str, engine: str, *, config: Config, executor) -> dict:
    """查一次预热 / 沉降的进度。

    **文件系统是哪个要从单子里的地址重新解出来**，不从 `move_job` 猜 ——
    任务号里不带文件系统，而这两个接口都必须同时给文件系统和任务号。
    """
    from . import moves as moves_mod
    from .clouds import nas, vepfs

    payload = ticket.get("payload") or {}
    try:
        plan = moves_mod.plan(str(payload.get("source") or ""), str(payload.get("dest") or ""))
    except Exception as exc:  # noqa: BLE001
        # 地址在单子里被改坏了（或者模板变了）—— 报出来，别当成「还没查到」
        return {
            "status": "",
            "bytes": 0,
            "objects": 0,
            "done": False,
            "failed": False,
            "error": f"重算不出这张单的路径：{_brief(exc)}",
        }
    fs = plan.get("fs") or {}
    row = _filesystem(ticket, str(fs.get("bucket") or ""))
    if engine == "nas":
        account = str((ticket.get("template") or {}).get("account") or "")
        return nas.poll(
            fs_id=fs["bucket"],
            region=row["region"],
            task_id=task,
            creds=_creds("aliyun", account, executor=executor),
        )
    return vepfs.poll(
        fs_id=fs["bucket"],
        region=row["region"],
        task_id=task,
        creds=_creds("volcano", config.volcano_account, executor=executor),
    )


def _dest_region(ticket: dict) -> str:
    """目的地域。查进度必须用建任务时那个地域 —— 用错了火山会返回「查不到」，
    而查不到不算失败，单子会一直停在在途直到有人发现。"""
    payload = ticket.get("payload") or {}
    got = _locate(
        moves.plan(str(payload.get("source") or ""), str(payload.get("dest") or "")), ticket
    )
    return str(got["dest"].get("region") or "")


def start_one(
    ticket: dict, *, config: Config, executor=None, issuer=None, now: Optional[float] = None
) -> dict:
    """提交一张单。返回要写回单子的字段。

    过不了体积门的**不提交**，只标成 `review` 等人 —— 见模块开头那段。
    """
    payload = ticket.get("payload") or {}
    plan = _locate(
        moves.plan(str(payload.get("source") or ""), str(payload.get("dest") or "")), ticket
    )
    account = str((ticket.get("template") or {}).get("account") or "")

    if not ticket.get("move_reviewed"):
        creds = None
        if plan["src"]["scheme"] == "oss":
            creds = _creds("aliyun", account, executor=executor)
        size, objects, known = _measure(plan, aliyun_creds=creds, config=config)
        if moves.needs_review(size, known=known):
            return {
                "move_stage": STAGE_REVIEW,
                "move_bytes": int(size),
                "move_objects": int(objects),
                "move_size_known": bool(known),
                "move_error": (
                    f"{_size(size)}，超过 {moves.REVIEW_TB} TB，等管理员确认再搬"
                    if known
                    else "量不出有多大（源目录读不到或太大），等管理员确认再搬"
                ),
            }

    minted: dict = {}
    try:
        fields = moves.start(
            ticket,
            # `moves.start` 自己会再 plan 一遍（它不认识模板），这里把带地域的那份换回去
            submit=lambda _got, name: _submit(
                plan,
                name,
                ticket,
                config=config,
                executor=executor,
                issuer=issuer,
                minted=minted,
            ),
            now=now,
        )
    except Exception as exc:
        # 签出来了但提交没成：**钥匙名要跟着异常一起落进单子**（`_one` 读 `fields`），
        # 只写进文案的话单子上查不到它，搬运结束时也就没人去撤（审计 M-1）。
        # 原始错误要留着 —— `from None` 会把真正的根因吞掉，人只看到一句「提交失败」
        if minted:
            raise CarriedError(f"{_brief(exc)}（{_carry(minted)}）", fields=dict(minted)) from exc
        raise
    fields.update(minted)
    fields["move_error"] = ""
    return fields


def _carry(minted: dict) -> str:
    # 下一轮会先查云上有没有任务：有就复用它（不动钥匙）；没有就撤掉这把、重签一把
    return (
        "源端只读凭证已签出："
        f"{minted.get('move_cred_cloud', '')}/{minted.get('move_cred_user', '')}，已记进单子"
    )


class CarriedError(MoverError):
    """带着要写回单子的字段的错误。提交失败时签出来的钥匙名靠它落盘。"""

    def __init__(self, message: str, *, fields: dict):
        super().__init__(message)
        self.fields = fields


def advance_one(
    ticket: dict, *, config: Config, executor=None, now: Optional[float] = None
) -> dict:
    """推进一张在途的单。返回要写回单子的字段（没变化就空）。"""
    status = _poll(ticket, config=config, executor=executor)
    return moves.advance(ticket, status, now=now)


def _size(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.1f} {unit}" if unit != "B" else f"{int(step)} B"
        step /= 1024
    return f"{step:.1f} TB"


def sweep(
    store,
    *,
    config: Optional[Config] = None,
    executor=None,
    issuer=None,
    now: Optional[float] = None,
    log: Callable = print,
    announce: Optional[Callable] = None,
) -> int:
    """跑一轮。返回出问题的单子数。

    **每张单子各自 try。** 一张单子炸了不能挡住后面的 —— 这条定时任务是无人值守的，
    一个卡住的任务会让所有在途迁移集体停摆，而且没人会发现。

    `announce(stage, ticket)` 在搬运卡到人这一步时调一次（等确认 / 失败）。
    **同一张单同一个状态只叫一次** —— 五分钟一轮，不去重就是一天 288 条，
    而一个每天响 288 次的通知等于没有通知。
    """
    cfg = config or Config.from_env()
    problems = 0
    for ticket in pending(store):
        stage = str(ticket.get("move_stage") or "")
        if stage in (moves.STAGE_DONE, moves.STAGE_FAILED, STAGE_REVIEW):
            continue
        # **整张单子的处理都在 try 里，写盘也在。**
        # 写盘会抛：`store.update` 带 `expect=[FULFILLING]`，而管理员随时可能在
        # 面板上把某张单子关掉 —— 那一下会把**整轮** sweep 掀掉，后面的单子这一轮
        # 一个都推不动。而这条定时任务是无人值守的，没人会发现少推了几张。
        try:
            problems += _one(
                ticket,
                store,
                cfg,
                executor=executor,
                issuer=issuer,
                now=now,
                log=log,
                announce=announce,
            )
        except Exception as exc:  # noqa: BLE001 — 见上
            problems += 1
            log(f"{str(ticket.get('id') or '')}：这轮没处理成 {_brief(exc)}")
    problems += reclaim(store, config=cfg, issuer=issuer, log=log)
    return problems


def reclaim(store, *, config: Config, issuer=None, log: Callable = print) -> int:
    """把「单子已经结束、跨云源端钥匙还挂着」的撤掉。返回撤不掉的张数。

    **为什么要单独扫一遍。** 正常路径是搬完 / 失败那一刻顺手撤（`_release`），
    但有三种情况会漏（审计 M-2）：
      · 那一刻撤不干净（记了 `move_cred_left`），之后单子转成 done、不再进 `pending`，
        再也没人试第二次
      · 单子在途时被人关掉 —— 钥匙和对方云上的任务都还在
      · 进程在「签出来」和「写回单子」之间挂掉（这个靠 CarriedError 已经落盘）
    钥匙策略 14 天后会失效，但**子账号和 AK 永久留在云上**，而体检的孤儿报告
    按 `tempak-` 前缀排除了它们 —— 没有任何报表会报出来。

    **在途的不碰**：状态是「开通中」且搬运还没到终态的单子，钥匙正在被用。
    """
    left = 0
    for ticket in store.all():
        if ticket.get("kind") != catalog_mod.KIND_TRANSFER:
            continue
        if not ticket.get("move_cred_user"):
            continue
        stage = str(ticket.get("move_stage") or "")
        status = str(ticket.get("status") or "")
        finished = stage in (moves.STAGE_DONE, moves.STAGE_FAILED) or status != t.FULFILLING
        if not finished:
            continue
        tid = str(ticket.get("id") or "")
        fields = _release(ticket, config, issuer=issuer, log=log, tid=tid)
        if fields.get("move_cred_left"):
            left += 1
        # **没变化就别写。** 撤不掉的钥匙每一轮都会走到这里，而每写一次就在单子上
        # 多一条事件 —— 一天 288 条，台账里真正有用的记录会被淹掉（审计 R3）
        if all(ticket.get(k) == v for k, v in fields.items()):
            continue
        try:
            store.update(
                tid, actor="system", expect=[status], event="move_cred_reclaimed", fields=fields
            )
        except Exception as exc:  # noqa: BLE001 — 下一轮再写
            log(f"{tid}：撤完钥匙写不回单子 {_brief(exc)}")
    return left


#: 「这一轮没跑成」——区别于 `moves.STAGE_FAILED`（搬运本身失败了）。
#: 只用作通知去重标记，不写进单子的 `move_stage`
STAGE_ERROR = "error"


def _one(ticket: dict, store, cfg: Config, *, executor, issuer, now, log, announce) -> int:
    """推进一张单。返回它算不算「出了问题」（0 / 1）。"""
    stage = str(ticket.get("move_stage") or "")
    tid = str(ticket.get("id") or "")
    try:
        if stage == moves.STAGE_RUNNING:
            fields = advance_one(ticket, config=cfg, executor=executor, now=now)
        else:
            fields = start_one(ticket, config=cfg, executor=executor, issuer=issuer, now=now)
    except Exception as exc:  # noqa: BLE001
        why = _brief(exc)
        log(f"{tid}：搬运出错 {why}")
        # 提交本身出错（比如漏配 DELIVERY_TRANSFER_OSS_ROLE）也是「卡到人这一步」——
        # 只记一行错误不通知的话，它会每分钟静静失败下去，而管理员什么都收不到
        # **标记用 STAGE_ERROR，不用 FAILED。** 共用的话：这轮抖了一下记上
        # `move_notified="failed"`，下一轮恢复（没有 stage 变化，标记清不掉），
        # 再往后**真失败时** `was != stage` 不成立 —— 那条通知永远发不出去。
        # 而这条定时任务无人值守，飞书私聊是它唯一的出口。
        _hand_off(
            ticket,
            store,
            tid,
            {"move_error": why, **dict(getattr(exc, "fields", None) or {})},
            log=log,
            announce=announce,
            stage=STAGE_ERROR,
            notify=True,
        )
        return 1
    if not fields:
        # 这轮查成了 —— 上一轮那个瞬时错误的标记要清掉，否则它会一直占着位置
        if str(ticket.get("move_notified") or "") == STAGE_ERROR:
            _write(store, tid, {"move_notified": ""}, log=log)
        return 0
    after = str(fields.get("move_stage") or "")
    bad = 1 if after == moves.STAGE_FAILED else 0
    if after in (moves.STAGE_DONE, moves.STAGE_FAILED):
        # **搬完就撤，不等时间窗到期。** 窗是兜底 —— 一把还能用两周的钥匙躺在
        # 对方云的任务配置里，和「我们已经搬完了」这件事没有任何关系
        fields.update(_release(ticket, cfg, issuer=issuer, log=log, tid=tid))
    _hand_off(
        ticket,
        store,
        tid,
        fields,
        log=log,
        announce=announce,
        stage=after,
        notify=after in (STAGE_REVIEW, moves.STAGE_FAILED),
    )
    if after == moves.STAGE_RUNNING and stage != moves.STAGE_RUNNING:
        log(f"{tid}：已提交迁移任务 {fields.get('move_job')}")
    elif after == STAGE_REVIEW:
        log(f"{tid}：{fields.get('move_error')}")
    elif after == moves.STAGE_DONE:
        why = fields.get("move_error")
        log(f"{tid}：搬完了" + (f"（但 {why}）" if why else ""))
    elif after == moves.STAGE_FAILED:
        log(f"{tid}：搬运失败 {fields.get('move_error')}")
    return bad


def _stable(text: str) -> str:
    """把错误文本里每次都变的部分抹掉（RequestId、各种 UUID / 十六进制请求号）。

    `reclaim` 靠「字段没变就不写」防刷屏，而原始错误里带着 RequestId ——
    每一轮都不一样，那道判断就永远不成立，照样一天写 288 次（审计三审）。
    """
    import re

    got = re.sub(r"(?i)request\s*id[\"'=: ]+[\w-]+", "RequestId=…", str(text or ""))
    got = re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27,}\b", "…", got)
    got = re.sub(r"\b[0-9A-Fa-f]{20,}\b", "…", got)
    return got[:200]


def _release(ticket: dict, cfg: Config, *, issuer, log, tid: str) -> dict:
    """撤掉跨云那把源端钥匙。**撤不干净要留痕**，不能安静跳过。"""
    if not ticket.get("move_cred_user"):
        return {}
    try:
        left = _drop_cred(ticket, config=cfg, issuer=issuer)
    except Exception as exc:  # noqa: BLE001 — 撤不掉不该让一次成功的搬运变成失败
        log(f"{tid}：源端凭证没撤掉 {_brief(exc)}（云上还留着，要人工清）")
        return {"move_cred_left": _stable(_brief(exc))}
    if left:
        log(f"{tid}：源端凭证没撤干净 {'、'.join(left)}")
        return {"move_cred_left": "、".join(left)}
    log(f"{tid}：已撤掉源端只读凭证")
    return {"move_cred_user": "", "move_cred_cloud": "", "move_cred_left": ""}


def _hand_off(
    ticket: dict, store, tid: str, fields: dict, *, log, announce, stage: str, notify: bool
) -> None:
    """落盘 + 该叫人时叫人。

    **先发通知，再写去重标记。** 反过来的话，一次发送失败（飞书挂了、token 过期）
    就让这张单从此再也不提醒 —— 而那条通知正是它唯一的出口。
    先发后写最坏是重复一条，那个方向安全得多。

    发通知用的是**写盘前**的 `ticket`：`_write` 改的就是这个字典本身，
    写完再读去重标记的话读到的已经是新值，判断永远成立、一条都发不出去。
    """
    was = str(ticket.get("move_notified") or "")
    sent = False
    if notify and announce is not None and was != stage:
        try:
            announce(stage, {**ticket, **fields})
            sent = True
        except Exception as exc:  # noqa: BLE001 — 通知发不出去不该让搬运本身算失败
            log(f"{tid}：通知没发出去 {_brief(exc)}（下一轮会再试）")
    if notify:
        # 发出去了才记标记。没发出去就不记 —— 下一轮还会再试
        if sent or was == stage:
            fields["move_notified"] = stage
    elif stage or was:
        # **离开「要人处理」就把标记清掉。** 不清的话，一张重试后又失败的单子
        # 因为标记还停在 failed 而不再提醒 —— 而第二次失败恰恰更需要人看一眼。
        # `or was` 是为了 stage 为空（这轮没有状态变化）时也能清掉陈旧标记
        fields["move_notified"] = ""
    _write(store, tid, fields, log=log)


def _write(store, ticket_id: str, fields: dict, *, log: Callable) -> None:
    """把进度写回单子。**搬完了才动单子状态**，其余一律只写字段。

    进度和状态分两次写：先把进度落下去，再谈状态转换。合在一次里的话，
    状态机拒绝（比如别人刚把单子关了）会把已经拿到的进度一起丢掉 ——
    而那份进度正是下一轮判断「这任务还在不在动」的依据。
    """
    done = str(fields.get("move_stage") or "") == moves.STAGE_DONE
    store.update(
        ticket_id, actor="system", expect=[t.FULFILLING], event="move_progress", fields=fields
    )
    if not done:
        return
    store.update(
        ticket_id,
        actor="system",
        expect=[t.FULFILLING],
        to=t.DONE,
        event="move_done",
        note="数据迁移完成",
        fields={"done_at_ts": fields.get("move_done_ts")},
    )


def _brief(exc: Exception) -> str:
    from .provision import describe_error

    return describe_error(exc) or type(exc).__name__
