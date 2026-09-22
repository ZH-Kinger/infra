"""阿里云「数据在线迁移」（MGW）。面板只用它往 OSS 搬。

搬自 bot 的 `core/transfer/engine_mgw.py` —— 那套在真机上跑过
（TOS→OSS 的 wuji_il 路径、OSS→OSS 跨 region 179MB/30 对象）。
**调用序列一字不改**，改的只有配置来源：bot 从全局 settings 读，面板从模板和环境变量读。

为什么这里非要用 SDK
────────────────────
面板其余部分零依赖，自己做 HMAC 签名。但迁移服务不认那一套：实测拿
**正确签名**和**故意改坏的签名**打同一个接口，回的是同一个
`AccessDenied: Anonymous access is forbidden` —— 它根本没读那个 Authorization 头。
盲猜签名方式的代价是把几十 TB 搬到错的地方，所以这里用官方 SDK。

调用序列（顺序不能改）
──────────────────────
    建源地址 → 校验 → 建目的地址 → 校验 → 建任务 → 启动 → 轮询

地址和任务都是**幂等**的：地址名带任务名后缀，重复建会撞 `AlreadyExist` 被忽略；
任务撞 `ImportJobRepeatedOnSameAddress` 时复用旧的继续轮询。
所以重试同一张申请单不会搬第二遍。
"""

from __future__ import annotations

from ..errors import DeliveryError

#: 地址校验的返回值。**真机实测是小写的 `available`**，文档上那几个大写串对不上。
#: 白名单化：认得出的才算过，认不出的一律当失败 —— 不然新状态串会被静默当成通过
_OK = frozenset({"available", "verify_success", "success", "ok"})
#: 还在查。空串也是（刚建时校验是异步的，还没结果）
_PENDING = frozenset({"verifying", "pending", "checking", "init"})

#: 启动任务时要写进去的状态。异步，写完立刻返回
STATUS_LAUNCHING = "IMPORT_JOB_LAUNCHING"
#: 终态
STATUS_FINISHED = "IMPORT_JOB_FINISHED"
STATUS_FAILED = "IMPORT_JOB_INTERRUPTED"

#: 同名策略 → (transfer_mode, overwrite_mode)。
#:
#: **文档说 `overwrite_mode=never` 能不覆盖，真机拒收这个值**（bot 那边探测出来的）。
#: 实际做法：跳过同名 = 增量模式（`lastmodified`），同名且没变就跳过。
_MODES = {
    "skip": ("lastmodified", "always"),
    "overwrite": ("all", "always"),
}


class MgwError(DeliveryError):
    """迁移服务调用失败。"""


def _models():
    try:
        from alibabacloud_hcs_mgw20240626 import models as m
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
        raise MgwError(
            "alibabacloud-hcs-mgw20240626 没装，做不了数据迁移。pip install -e . 重新装一遍依赖。"
        ) from exc
    return m


def client(*, endpoint: str, region: str, creds):
    """构造迁移服务 Client。用长期 AK —— 这是后台任务，没有用户上下文。

    **`endpoint` 填 `cn-beijing.mgw.aliyuncs.com`，别自己在前面拼 userid** ——
    SDK 会拼。拼两遍的表现是 SSL 证书 hostname 不匹配，报错里看不出是配置问题。
    """
    try:
        from alibabacloud_hcs_mgw20240626.client import Client
        from alibabacloud_tea_openapi import models as open_api_models
    except ImportError as exc:  # pragma: no cover
        raise MgwError("alibabacloud-hcs-mgw20240626 / alibabacloud-tea-openapi 没装") from exc
    config = open_api_models.Config(
        access_key_id=creds.access_key_id,
        access_key_secret=creds.access_key_secret,
        endpoint=endpoint,
        region_id=region,
    )
    if getattr(creds, "security_token", ""):
        config.security_token = creds.security_token
    return Client(config)


def _put_address(cli, user_id: str, name: str, detail, *, replace: bool = False) -> None:
    """建数据地址。**已存在就当成功** —— 地址名带任务名，内容由名字唯一决定，复用是安全的。

    **`replace=True` 时例外：先删掉旧的再建。** 带钥匙的源地址（跨云那条）里存着 AK，
    而那把 AK 每次重签都会变。吞掉「已存在」的话，地址里留着的是**上一轮签的、
    已经被撤掉的**那把 —— 任务建得出来、也启动得了，然后全部 403（审计 H-C）。
    只在「云上还没有这张单的任务」时才会走到这里（见 mover._submit），
    所以删的地址不会被任何在跑的任务引用。
    """
    m = _models()

    def create():
        cli.create_address(
            user_id,
            m.CreateAddressRequest(
                import_address=m.CreateAddressInfo(name=name, address_detail=detail)
            ),
        )

    try:
        create()
    except Exception as exc:  # noqa: BLE001 — 只认「已存在」，其余照抛
        blob = f"{getattr(exc, 'code', '') or ''} {exc}"
        if replace and any(k in blob for k in ("AlreadyExist", "Duplicate", "已存在")):
            try:
                cli.delete_address(user_id, name)
                create()
            except Exception as again:  # noqa: BLE001
                raise MgwError(f"换钥匙重建数据地址 {name} 失败：{str(again)[:300]}") from again
            return
        # **只认「已经有了」，别认光秃秃的 `Exist`。** 那个子串同时命中
        # `EntityNotExist` / `AddressNotExist` —— 把「不存在」当成「已存在」吞掉，
        # 结果是地址没建成却照样往下走，错误推迟到建任务那步才炸
        if not any(k in blob for k in ("AlreadyExist", "Duplicate", "已存在")):
            # 裹一层：其余出口都包了 MgwError，只有这里让 SDK 原始异常穿透，
            # 用户看到的是「请联系管理员」，而 Tea 的异常对象上还挂着请求体
            raise MgwError(f"建数据地址 {name} 失败：{str(exc)[:300]}") from exc


def _oss_detail(m, *, bucket: str, prefix: str, region: str, role: str, internal: bool):
    detail = m.AddressDetail()
    detail.address_type = "oss"
    detail.region_id = f"oss-{region}"
    detail.bucket = bucket
    detail.prefix = prefix
    detail.role = role
    detail.domain = f"oss-{region}{'-internal' if internal else ''}.aliyuncs.com"
    return detail


def _keyed_detail(
    m, *, kind: str, bucket: str, prefix: str, domain: str, access_id: str, access_secret: str
):
    """要 AK/SK 的那一类源（火山 TOS、第三方 S3 兼容存储）。字段是同一套五件套。

    `kind` 只在 `tos` 和 `s3compat` 里取：前者是我们自己的火山桶，后者是别人的
    MinIO / Ceph RGW —— 官方 SDK 示例里通用 S3 兼容存储就是这个类型名。
    （阿里 API 元数据那份枚举漏了它，照元数据写会以为只能用 `s3`。）
    """
    detail = m.AddressDetail()
    detail.address_type = kind
    detail.access_id = access_id
    detail.access_secret = access_secret
    detail.bucket = bucket
    detail.prefix = prefix
    detail.domain = domain
    return detail


def _region(node: dict, label: str) -> str:
    """取地域。**缺了就明说**。

    路径串 `oss://桶/目录/` 里带不出地域，得由调用方从模板查出来填进去。
    忘了填的话直接下标会抛 KeyError —— 那不是 `DeliveryError`，面板会把它渲染成
    「请联系管理员」的 500，看不出是配置问题。
    """
    got = str(node.get("region") or "")
    if not got:
        raise MgwError(f"不知道{label}桶 {node.get('bucket', '')} 在哪个地域")
    return got


def verify_address(cli, user_id: str, name: str, *, tries: int = 10, sleep=None) -> None:
    """等地址校验通过。

    **超时按通过处理**（bot 那边的做法，照搬）：校验是异步的，刚建时 status 为空；
    真正建任务时服务端会复检，所以地址不通的错误会延后到建任务那一步暴露，
    而那一步的报错更具体。在这里卡死反而会让一次正常的迁移提不上去。

    正因为兜底是「放行」，**这里的状态串写错了不会报错，只会白等满 30 秒再放行**。
    搬过来时就是这样：认的是 `VERIFY_SUCCESS`，真机回的是小写 `available`，
    十轮全落空转。测试盯的就是这个 —— 成功必须是第一轮返回、一次都不睡。
    """
    import time as _time

    nap = sleep or (lambda s: _time.sleep(s))
    for _ in range(max(1, tries)):
        try:
            resp = cli.verify_address(user_id, name)
        except Exception:  # noqa: BLE001 — 校验本身抖动不该判失败，下一轮再看
            nap(3)
            continue
        body = getattr(resp, "body", None)
        got = getattr(body, "verify_address_response", None)
        status = str(getattr(got, "status", "") or "").strip().lower()
        # **真机实测返回的是 `available`**，不是文档里那几个大写串。
        # 写错的后果不是功能坏（会落到下面「超时按通过」那条），而是每个地址白等 30 秒，
        # 一次迁移两个地址就是一分钟 —— 而且真出现失败状态时也认不出来。
        if status in _OK:
            return
        if status and status not in _PENDING:
            why = str(getattr(got, "error_message", "") or "")
            raise MgwError(f"数据地址 {name} 校验不过：{status} {why[:200]}")
        nap(3)


def job_exists(*, user_id: str, endpoint: str, region: str, creds, job_name: str) -> bool:
    """云上有没有这个名字的任务。**查不清就当「没有」往下走会重签钥匙**，
    所以查询本身出错时抛，让这一轮停下，下一轮再来。"""
    m = _models()
    cli = client(endpoint=endpoint, region=region, creds=creds)
    try:
        cli.get_job(user_id, job_name, m.GetJobRequest())
        return True
    except Exception as exc:  # noqa: BLE001
        blob = str(exc)
        if any(k in blob for k in ("NoSuchImportJob", "NotExist", "NotFound", "not exist")):
            return False
        raise MgwError(f"查不清任务 {job_name} 在不在：{blob[:200]}") from exc


def relaunch(*, user_id: str, endpoint: str, region: str, creds, job_name: str) -> str:
    """任务已经在云上了：确保它被启动过，返回任务名。**不碰地址、不换钥匙。**"""
    m = _models()
    cli = client(endpoint=endpoint, region=region, creds=creds)
    _launch(cli, m, user_id, job_name)
    return job_name


def submit(
    *,
    user_id: str,
    endpoint: str,
    region: str,
    creds,
    job_name: str,
    src: dict,
    dest: dict,
    same_name: str = "skip",
    oss_role: str = "",
    src_key: str = "",
    src_secret: str = "",
    src_domain: str = "",
) -> str:
    """建地址 → 校验 → 建任务 → 启动。返回任务名（拿去轮询）。

    `src` / `dest` 形如 `{"scheme": "oss", "bucket": …, "prefix": …, "region": …}`。

    **目的端只放行 OSS，这是面板的约束、不是服务的。** 服务本身还支持 `local`
    （往自建机房搬，官方场景页列了 OSS→Local / Local→Local），但那要在对方机器上
    装迁移代理 —— 面板做不了这件事，而一个「能往任意 IDC 写」的入口，出事的量级
    和只能往自家桶里写不是一回事。要往 IDC 搬走人工。进 TOS 是火山那条（见 `dms.py`）。
    """
    m = _models()
    if dest.get("scheme") != "oss":
        raise MgwError("面板只放行搬进 OSS；目的是 TOS 请走火山迁移，目的是自建机房请找管理员")
    if not user_id:
        raise MgwError("没配迁移服务的 userid")
    if not oss_role:
        raise MgwError("没配 OSS 的 RAM 角色名，目的地址建不起来")
    modes = _MODES.get(same_name)
    if modes is None:
        raise MgwError(f"同名策略只能是 {' / '.join(_MODES)}")
    transfer_mode, overwrite_mode = modes

    cli = client(endpoint=endpoint, region=region, creds=creds)
    src_addr, dest_addr = f"{job_name}-src", f"{job_name}-dst"

    scheme = src.get("scheme")
    if scheme == "oss":
        # 同账号桶间：源也用 role。
        # **内外网要跟「迁移服务部署在哪」比，不是源和目的互比。**
        # 互比的错法在「深圳→深圳、服务在北京」这种单子上会拼出
        # `oss-cn-shenzhen-internal.aliyuncs.com` —— 从北京的迁移服务根本连不上。
        # 面板模板里的桶横跨杭州/深圳/北京/新加坡/曼谷，这是常态不是边角。
        # bot 那边是对的（`core/bucket_transfer/orchestrator.py` 两侧都跟部署地域比），
        # 搬过来的时候写坏了
        src_region = _region(src, "源")
        detail = _oss_detail(
            m,
            bucket=src["bucket"],
            prefix=src.get("prefix", ""),
            region=src_region,
            role=oss_role,
            internal=src_region == region,
        )
    elif scheme in ("tos", "s3compat"):
        if not (src_key and src_secret):
            raise MgwError(f"源是 {scheme}，但没拿到它的 AK/SK")
        # 火山的域名能从地域推出来；第三方的推不出来，必须由登记表给
        domain = src_domain or (
            f"tos-s3-{src.get('region', '')}.volces.com" if scheme == "tos" else ""
        )
        if not domain:
            raise MgwError("第三方数据源没登记 endpoint，源地址建不起来")
        detail = _keyed_detail(
            m,
            kind=scheme,
            bucket=src["bucket"],
            prefix=src.get("prefix", ""),
            domain=domain,
            access_id=src_key,
            access_secret=src_secret,
        )
    else:
        # 不认识的源类型**不往下走**。掉进某个分支的后果是拿错域名去读别人的桶，
        # 而那种错误的表现是「搬了 0 个对象，成功」
        raise MgwError(f"不支持的源类型 {scheme!r}")
    # 带钥匙的源（火山 TOS / 第三方 S3）要换掉旧地址 —— 见 `_put_address` 的 replace
    _put_address(cli, user_id, src_addr, detail, replace=bool(src_key))
    verify_address(cli, user_id, src_addr)

    dest_region = _region(dest, "目的")
    _put_address(
        cli,
        user_id,
        dest_addr,
        _oss_detail(
            m,
            bucket=dest["bucket"],
            prefix=dest.get("prefix", ""),
            region=dest_region,
            role=oss_role,
            internal=dest_region == region,
        ),
    )
    verify_address(cli, user_id, dest_addr)

    try:
        cli.create_job(
            user_id,
            m.CreateJobRequest(
                import_job=m.CreateJobInfo(
                    name=job_name,
                    transfer_mode=transfer_mode,
                    overwrite_mode=overwrite_mode,
                    src_address=src_addr,
                    dest_address=dest_addr,
                )
            ),
        )
    except Exception as exc:  # noqa: BLE001
        blob = str(exc)
        if not any(
            k in blob
            for k in (
                "ImportJobRepeated",
                "Already has job",
                "already has job",
                "JobExist",
                "AlreadyExist",
            )
        ):
            raise MgwError(f"提交迁移任务失败：{str(exc)[:300]}") from exc
        # 同源同目的已经有任务了 —— 复用它继续轮询，别搬第二遍。
        #
        # **但要先确认它真的叫这个名字。** 错误码字面是
        # `ImportJobRepeatedOnSameAddress`，也就是可能按「地址对」判重而不是按名字：
        # 重试换了名字（`-r2`）却指向同一对桶+前缀时，撞的是那个**旧名字**的任务，
        # 而我们返回的是新名字 —— 云上没有这个任务，之后每次 `get_job` 都 404，
        # 404 又不算失败，于是单子永远停在在途、连「卡住」都不报。
        # 查一下就能分清，不用赌它是哪种判重语义
        try:
            cli.get_job(user_id, job_name, m.GetJobRequest())
        except Exception as probe:  # noqa: BLE001
            raise MgwError(
                f"源和目的之间已经有一个迁移任务了，但它不叫 {job_name} —— "
                f"去控制台看一眼那个任务的状态，别重复提交（{str(probe)[:120]}）"
            ) from exc
    _launch(cli, m, user_id, job_name)
    return job_name


def _launch(cli, m, user_id: str, job_name: str) -> None:
    """把建好的任务推到 LAUNCHING。

    **这一步和建任务分开，而且复用已有任务那条路也要走到。**
    原来两个调用挤在同一个 try 里：`update_job` 抖一次，下一轮重提交会撞
    `ImportJobRepeated` → 探针查到任务在 → 直接返回 —— `update_job` 再也不会被调用。
    任务就那么躺在云上永不启动，而轮询只会一直显示「还没开始」，不报错、不失败。

    **启动失败就抛。** 抛出去这张单会停在「没提交成」，下一轮 sweep 重来一遍：
    撞 repeated → 探针过 → 再启动一次。这条路自己能好，不需要在这里吞。
    只有一种要吞：任务其实已经启动过了（上次调用成功但响应没回来）——
    那个错误里会带 `IMPORT_JOB_` 开头的状态串。
    """
    try:
        cli.update_job(
            user_id,
            job_name,
            m.UpdateJobRequest(import_job=m.UpdateJobInfo(status=STATUS_LAUNCHING)),
        )
    except Exception as exc:  # noqa: BLE001
        if "IMPORT_JOB_" in str(exc):
            return
        raise MgwError(f"迁移任务建好了但没能启动：{str(exc)[:300]}") from exc


def poll(*, user_id: str, endpoint: str, region: str, creds, job_name: str) -> dict:
    """查一次进度。返回 `{status, bytes, objects, error, done, failed}`。

    **查不到状态不算失败**：轮询是网络抖动的高发点，空状态让调用方继续等，
    真失败由终态字符串判定。
    """
    m = _models()
    try:
        cli = client(endpoint=endpoint, region=region, creds=creds)
        resp = cli.get_job(user_id, job_name, m.GetJobRequest())
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "",
            "bytes": 0,
            "objects": 0,
            "error": f"查进度失败：{str(exc)[:160]}",
            "done": False,
            "failed": False,
        }
    job = getattr(getattr(resp, "body", None), "import_job", None)
    status = str(getattr(job, "status", "") or "")

    copied_bytes = copied_objs = bad = 0
    try:
        hist = cli.list_job_history(user_id, job_name, m.ListJobHistoryRequest())
        rows = (
            getattr(
                getattr(getattr(hist, "body", None), "job_history_list", None), "job_history", None
            )
            or []
        )
        for row in rows:
            count = getattr(row, "copied_count", -1)
            if count is not None and count >= 0:
                copied_objs = count
                copied_bytes = getattr(row, "copied_size", 0) or 0
                bad = getattr(row, "failed_count", 0) or 0
                break
    except Exception:  # noqa: BLE001,S110 — 刚建的任务没有 history，正常，没什么可记的
        pass
    return {
        "status": status,
        "bytes": int(copied_bytes or 0),
        "objects": int(copied_objs or 0),
        "error": f"{bad} 个对象失败" if bad else "",
        "done": status == STATUS_FINISHED,
        "failed": status == STATUS_FAILED,
    }


def estimate(bucket: str, prefix: str, *, region: str, creds, transport=None) -> tuple:
    """搬之前量一下有多大。返回 `(字节数, 对象数, 准不准)`。

    **量不准时第三个返回值是 False，调用方必须当成「大任务」处理。**
    bot 那边这里返回 (0, 0) 就完事了，于是 `needs_approval(0)` 恒为 False ——
    一个 100TB 的任务会被当成 0 字节直接放行。那个 fail-open 不要搬过来。
    """
    from . import oss

    # **OSS 的 endpoint 要带 `oss-` 前缀，而调用方给的是裸地域。**
    # 两边的约定不一样：模板里存的是裸的（`catalog._buckets` 明确拒绝带前缀的写法），
    # `oss.call` 拼主机名时要的是 `oss-cn-shenzhen`。
    # 不转的话拼出来的域名根本不存在 → 报错被下面吞成 `(0,0,False)` →
    # `needs_review(known=False)` 恒为 True → **每一张单都停在「等确认」**，
    # 而管理员在卡片上看到的是「量不出有多大」，一个数字都没有。
    # 体积门于是退化成橡皮图章 —— 它看起来在工作，其实从没量出过任何东西。
    where = region if region.startswith("oss-") else f"oss-{region}"
    try:
        rows = oss.list_objects(bucket, prefix, region=where, creds=creds, transport=transport)
    except Exception:  # noqa: BLE001
        return 0, 0, False
    return sum(n for _, n in rows), len(rows), True
