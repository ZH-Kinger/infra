"""火山引擎「存储迁移服务」（DMS 1.0）。目的端固定是 TOS —— 这条是服务定的，不是我们定的。

搬自 bot 的 `core/transfer/engine_tos.py`，那套真机跑过：
  - OSS→TOS：task 552876，`wuji-bucket-hangzhou` → `data-tran`，2 个对象 4.5MB，Success
  - TOS→TOS：task 553276，`data-tran` → `seed-test1111`，Success
所有字段值都是从这两个任务的真实配置反查出来的，不是照文档猜的。

和阿里那条（`mgw.py`）三处不一样，每一处都会咬人
────────────────────────────────────────────────
**① 目的端真的只能是 TOS。** 阿里那条限制是我们自己加的保守约束（服务其实支持
   往自建机房搬），这条不是：`TargetForCreateDataMigrateTaskInput` 只有
   `ak / sk / bucket_name` 三个字段，协议层就表达不出别的目的地。
   （陷阱：**查询**任务时返回的 Target 反而带 `vendor/endpoint/region` ——
   那是响应模型，SDK 生成器复用了通用结构，别被它骗得以为创建时能传。）

**② 目的端只到桶级，给不了子目录。** 对象**保持完整源 key 原样**落进目的桶，
   不去前缀也不加前缀。所以这里**直接拒绝带前缀的目的地址** —— bot 那边是
   记下来但不生效，那等于让申请人以为数据在 `tos://桶/我填的目录/`，
   实际散落在桶根下的源目录结构里。搬错地方比搬失败难发现得多。

**③ 没有幂等。** 阿里按任务名幂等，重复建撞 `AlreadyExist`；火山这边建一个
   返回一个新 task_id，同一张单提两次就是两个任务同时搬。所以建之前先列一遍
   找同名的，见 `find_task()`。

配置从参数来，不从全局 settings 来 —— 面板这边一张单一套参数。
"""

from __future__ import annotations

from typing import Optional

from ..errors import DeliveryError

#: 源存储厂商。**大小写不统一是官方的锅**（`Ks3`/`Kodo` 是驼峰，其余全大写），别自己拼
VENDOR_OSS = "StorageVendorOSS"
VENDOR_TOS = "StorageVendorTOS"

#: 对象存储类型的任务（相对于 URL 清单、本地文件系统那几种）
SOURCE_TYPE = "StorageTypeObject"
#: 保持源对象的存储类型，不在搬运途中改成别的规格
STORAGE_CLASS = "InheritSource"

#: 同名策略 → `overwrite_policy`。合法枚举 `Force / None / LastModify`。
#:
#: **`"None"` 是个字符串，不是 Python 的 None。** 写成 None 会被 SDK 当成「没传」，
#: 落到服务端默认值上 —— 而默认是什么没人验过。
_MODES = {
    "skip": "None",
    "overwrite": "Force",
}

#: 终态。**失败是 `Failure` 不是 `Failed`**（真机实测的枚举，拼错的话失败的任务
#: 会一直显示「进行中」，直到有人手动去控制台看）
STATUS_SUCCESS = "Success"
STATUS_FAILURE = "Failure"
STATUS_STOPPED = "Stopped"
_FAILED = frozenset({STATUS_FAILURE, STATUS_STOPPED})

#: 列任务时一页拿多少
_PAGE = 100
#: 最多翻几页找同名任务。翻不完就当没找到 —— 见 `find_task()` 里那段
_MAX_PAGES = 20


class DmsError(DeliveryError):
    """火山迁移服务调用失败。"""


def _reason(exc) -> str:
    """从 SDK 异常里挖出**人能看懂的那一句**。

    火山 SDK 的异常 `str()` 出来是「状态码 + 一整坨 HTTP 响应头 + body」，
    响应头能有一两千字符。直接 `str(exc)[:300]` 的话截出来全是 `Server: Tengine`、
    `x-tt-trace-id` 这种，真正的原因（`Need HeadBucket permission on bucket xxx`）
    在后面，一个字都看不到 —— 而那句话恰恰是唯一能指导下一步动作的。
    """
    body = getattr(exc, "body", None)
    if body is not None:
        raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        try:
            import json

            err = (json.loads(raw).get("ResponseMetadata") or {}).get("Error") or {}
        except (ValueError, AttributeError):
            err = {}
        code = str(err.get("Code") or "").strip()
        message = str(err.get("Message") or "").strip()
        if code or message:
            return f"{code}：{message}".strip("：")[:300]
    return str(exc)[:300]


def _sdk():
    try:
        import volcenginesdkcore as core
        import volcenginesdkdms as dms
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的提示
        raise DmsError(
            "volcengine-python-sdk 没装，做不了往 TOS 的迁移。pip install -e . 重新装一遍依赖。"
        ) from exc
    return core, dms


def client(*, region: str, creds):
    """构造 DMS Client。

    **`region` 是目的 TOS 的地域，不是源的。** 任务本身也按这个地域归属：
    真机实测在 `cn-beijing` 列任务返回空，同一批任务在 `cn-shanghai` 全在。
    所以查进度、找同名任务，都必须用当初建任务时那个目的地域。
    """
    core, dms = _sdk()
    ak = getattr(creds, "access_key_id", "")
    sk = getattr(creds, "secret_access_key", "") or getattr(creds, "access_key_secret", "")
    if not (ak and sk):
        raise DmsError("没配火山的 AK/SK，建不了迁移任务")
    if not region:
        raise DmsError("没给目的 TOS 的地域，建不了迁移任务")
    cfg = core.Configuration()
    cfg.ak, cfg.sk, cfg.region = ak, sk, region
    return dms.DMSApi(core.ApiClient(cfg)), dms


class TooManyTasks(DmsError):
    """翻到上限还没翻完 —— 「有没有这个任务」这一问没有答案。"""


def find_task(cli, dms, name: str, *, strict: bool = False) -> Optional[int]:
    """按任务名找已有任务，返回 task_id；没有返回 None。

    火山没有按名字查的接口（`ListDataMigrateTaskRequest` 只有 limit/offset/task_status），
    所以只能翻列表自己比。

    **翻不完就返回 None，也就是「会重复建一个」。** 反过来更糟：翻不完就报错的话，
    账号里任务一多，所有迁移都提不上去。这里的竞态（两个进程同时查完、同时建）
    由上层挡 —— `moves.start()` 只在 stage 是 NEW 时提交，提完立刻写 RUNNING。

    **`strict=True` 时翻不完就抛 `TooManyTasks`。** 跨云那条在签源端钥匙之前问这一句：
    「没有」会导致重签，而重签会撤掉上一把 —— 如果那个任务其实在第 2001 条之后，
    它就拿着一把死钥匙跑，全部 403。那条路上「不知道」必须当成「停下」，不能当成「没有」。
    """
    want = str(name or "").strip()
    if not want:
        raise DmsError("任务名为空")
    for page in range(_MAX_PAGES):
        try:
            resp = cli.list_data_migrate_task(
                dms.ListDataMigrateTaskRequest(limit=_PAGE, offset=page * _PAGE)
            )
        except Exception as exc:  # noqa: BLE001
            raise DmsError(f"列迁移任务失败：{_reason(exc)}") from exc
        rows = getattr(resp, "task_list", None) or []
        for row in rows:
            if str(getattr(row, "task_name", "") or "") == want:
                got = getattr(row, "task_id", None)
                if got is not None:
                    return int(got)
        if len(rows) < _PAGE:
            return None
    if strict:
        raise TooManyTasks(
            f"火山账号里的迁移任务超过 {_PAGE * _MAX_PAGES} 个，翻不完，"
            f"确认不了 {want} 在不在 —— 为了不撤掉它可能正在用的钥匙，这一轮先不提交"
        )
    return None


def _source(
    dms, *, scheme: str, bucket: str, prefix: str, region: str, access_id: str, access_secret: str
):
    """源配置。阿里 OSS 和火山 TOS 的 region 写法不一样，这里是唯一的差别所在。"""
    if scheme == "oss":
        vendor = VENDOR_OSS
        endpoint = f"https://oss-{region}.aliyuncs.com"
        # OSS 源的 region 要带 `oss-` 前缀（真机配置就是 `oss-cn-hangzhou`）。
        # 已经带了就别再加一层 —— 拼成 `oss-oss-cn-hangzhou` 的错在别处踩过
        reg = region if region.startswith("oss-") else f"oss-{region}"
    elif scheme == "tos":
        vendor = VENDOR_TOS
        endpoint = f"https://tos-{region}.volces.com"
        reg = region
    else:
        # 认不出的源类型不往下走。掉进某个分支的后果是拿错 vendor 去读桶，
        # 而那种错误的表现是「搬了 0 个对象，成功」
        raise DmsError(f"火山迁移不支持的源类型 {scheme!r}")
    if not (access_id and access_secret):
        raise DmsError(f"源是 {scheme}，但没拿到它的 AK/SK")

    access = dms.BucketAccessConfigForCreateDataMigrateTaskInput(
        vendor=vendor,
        endpoint=endpoint,
        region=reg,
        bucket_name=bucket,
        ak=access_id,
        sk=access_secret,
    )
    return dms.SourceForCreateDataMigrateTaskInput(
        object_source_config=dms.ObjectSourceConfigForCreateDataMigrateTaskInput(
            bucket_access_config=access,
            # 前缀是个**列表**，`is_excluded=False` 表示「只搬这些前缀」而不是「排除这些」。
            # 写反了就是把整个桶搬过来，只漏掉申请人真正想要的那个目录
            prefix_list=[prefix],
            is_excluded=False,
            scan_with_delimiter=False,
        )
    )


def existing(*, region: str, creds, job_name: str) -> Optional[str]:
    """云上有没有这个名字的任务；有就返回 task_id。

    **跨云时 mover 在签源端钥匙之前先问这一句。** 先签后查的话：上一轮任务其实
    已经建出来了（只是回包丢了、或者写单子失败），这一轮签新钥匙时会把上一把撤掉，
    然后 `submit` 里的 `find_task` 把那个旧任务捞回来 —— 它拿着一把已经删掉的钥匙在跑，
    全部 403（审计 H-C）。
    """
    cli, dms = client(region=region, creds=creds)
    got = find_task(cli, dms, job_name, strict=True)
    return None if got is None else str(got)


def submit(
    *,
    region: str,
    creds,
    job_name: str,
    src: dict,
    dest: dict,
    same_name: str = "skip",
    src_key: str = "",
    src_secret: str = "",
) -> str:
    """建任务并返回 task_id（字符串，拿去轮询）。

    **建完就开始搬**，DMS 1.0 没有单独的启动动作 —— 和阿里那条要先 `update_job`
    置成 LAUNCHING 不一样。

    `src` / `dest` 形如 `{"scheme": "tos", "bucket": …, "prefix": …, "region": …}`。
    """
    if dest.get("scheme") != "tos":
        raise DmsError("火山迁移只能把数据搬进 TOS，目的是 OSS 请走阿里在线迁移")
    if dest.get("prefix"):
        # 见文件开头 ②。这里拒掉而不是忽略：让人在提交时就知道填了没用，
        # 好过搬完之后去桶里找不到自己填的那个目录
        raise DmsError(
            "火山迁移的目的端只能指定到桶，给不了子目录 —— 对象会保持源路径原样落进桶里。"
            f"把目的地址写成 tos://{dest.get('bucket', '')}/ 再提，"
            "确实需要固定子目录的找管理员搬完再整理。"
        )
    policy = _MODES.get(same_name)
    if policy is None:
        raise DmsError(f"同名策略只能是 {' / '.join(_MODES)}")

    dest_region = str(dest.get("region") or region or "")
    cli, dms = client(region=dest_region, creds=creds)

    # 火山不按任务名幂等，重复提交 = 两个任务同时搬同一批数据。见文件开头 ③
    existing = find_task(cli, dms, job_name)
    if existing is not None:
        return str(existing)

    source = _source(
        dms,
        scheme=str(src.get("scheme") or ""),
        bucket=str(src.get("bucket") or ""),
        prefix=str(src.get("prefix") or ""),
        region=str(src.get("region") or ""),
        access_id=src_key,
        access_secret=src_secret,
    )
    target = dms.TargetForCreateDataMigrateTaskInput(
        ak=getattr(creds, "access_key_id", ""),
        sk=(getattr(creds, "secret_access_key", "") or getattr(creds, "access_key_secret", "")),
        bucket_name=str(dest.get("bucket") or ""),
    )
    basic = dms.BasicConfigForCreateDataMigrateTaskInput(
        task_name=job_name,
        source_type=SOURCE_TYPE,
        overwrite_policy=policy,
        storage_class=STORAGE_CLASS,
        # 失败多少个对象就整单中止。**0 = 不中止**，有失败也把剩下的搬完，
        # 失败明细留给对账去看 —— 中途停下来的任务更难收拾
        failed_num_to_abort=0,
        enable_range_check=False,
    )

    try:
        resp = cli.create_data_migrate_task(
            dms.CreateDataMigrateTaskRequest(basic_config=basic, source=source, target=target)
        )
    except Exception as exc:  # noqa: BLE001
        raise DmsError(f"提交火山迁移任务失败：{_reason(exc)}") from exc
    task_id = getattr(resp, "task_id", None)
    if task_id is None:
        # 没拿到 id 就等于这个任务从此没人管：查不了进度、也不知道它有没有在搬。
        # 不能当成功返回
        raise DmsError("火山迁移服务没返回 task_id，任务状态未知，去控制台确认一下")
    return str(task_id)


def poll(*, region: str, creds, job_name: str) -> dict:
    """查一次进度。返回 `{status, bytes, objects, error, done, failed}` —— 和 `mgw.poll` 同形。

    **查不到状态不算失败**：轮询是网络抖动的高发点，空状态让调用方继续等，
    真失败由终态字符串判定。
    """
    blank = {"status": "", "bytes": 0, "objects": 0, "done": False, "failed": False}
    try:
        cli, dms = client(region=region, creds=creds)
        resp = cli.query_data_migrate_task(dms.QueryDataMigrateTaskRequest(task_id=int(job_name)))
    except (TypeError, ValueError):
        return {**blank, "error": f"任务号不是数字：{str(job_name)[:40]}"}
    except Exception as exc:  # noqa: BLE001
        return {**blank, "error": f"查进度失败：{_reason(exc)[:160]}"}

    status = str(getattr(resp, "task_status", "") or "")
    prog = getattr(resp, "task_progress", None)
    return {
        "status": status,
        "bytes": int(getattr(prog, "transferred_bytes", 0) or 0) if prog else 0,
        "objects": int(getattr(prog, "transferred_objects", 0) or 0) if prog else 0,
        "error": _why(resp, prog),
        "done": status == STATUS_SUCCESS,
        "failed": status in _FAILED,
    }


def _why(resp, prog) -> str:
    """拼一条能看的失败原因。

    DMS 没有单一的错误字段 —— 失败时 `task_status` 只有一个 `Failure`。
    不拼这一条的话，失败卡上就只有「失败」两个字，没人知道该怎么办。
    """
    parts = []
    if prog is not None:
        failed = int(getattr(prog, "failed_objects", 0) or 0)
        missing = int(getattr(prog, "not_exist_object_count", 0) or 0)
        if failed:
            parts.append(f"{failed} 个对象迁移失败")
        if missing:
            parts.append(f"{missing} 个对象源端已不存在")
    if not parts:
        report = getattr(resp, "task_report", None)
        for attr in ("err_msg", "error_message", "message", "fail_reason", "report_url"):
            got = str(getattr(report, attr, "") or "") if report is not None else ""
            if got:
                parts.append(got[:200])
                break
    return "；".join(parts)


def estimate(bucket: str, prefix: str, *, scheme: str, region: str, creds, transport=None) -> tuple:
    """搬之前量一下有多大。返回 `(字节数, 对象数, 准不准)`。

    **只量得了 OSS 源。** 面板没有 TOS 的列举实现（火山那边是另一套 S3 风格的签名，
    不是 `volcano.py` 里那个 OpenAPI 签名），所以源是 TOS 时第三个返回值是 False，
    调用方会当成「大任务」送去人工确认。宁可多问一次，也不能把量不出来当成 0 字节 ——
    那样一个 100TB 的任务会被直接放行。
    """
    if scheme != "oss":
        return 0, 0, False
    from . import oss

    try:
        rows = oss.list_objects(bucket, prefix, region=region, creds=creds, transport=transport)
    except Exception:  # noqa: BLE001
        return 0, 0, False
    return sum(n for _, n in rows), len(rows), True
