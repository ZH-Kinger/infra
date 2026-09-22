"""数据迁移的编排：从「审批通过」到「搬完了」。

为什么不共用 bot 的那套
────────────────────────
bot 的编排把任务记在 Redis，30 天 TTL。那对它够用 —— 它要的是「任务跑着的时候
状态查得到」。面板要的是另一件事：**三个月后有人问「去年谁把那批数据搬到泰国的」，
得答得出来。** Redis 30 天之后什么都不剩。

所以状态存在**申请单里**：单子本来就要长期留、本来就每小时备份、本来就带着
「谁申请的、谁批的、批的是什么」。搬运进度多写几个字段进去，审计就是白来的。

代价是没有 bot 那套「容器重启后对账线程接管孤儿任务」的自愈 —— 面板这边靠
定时任务扫「在途的单子」补上，见 `pending()`。

状态机
──────
    NEW → RUNNING → DONE / FAILED

**只推进，不回退。** 重试是把 stage 显式改回 NEW（`retry()`），
而不是让轮询自己把 FAILED 变回 RUNNING —— 那样一次网络抖动就能让失败的任务
看起来又活了。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .errors import DeliveryError

STAGE_NEW = "new"
STAGE_RUNNING = "running"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

#: 在途的任务多久没动静就当它断了。比轮询间隔大得多 —— 这个数只用来发现
#: 「进程死了、没人再推进它」，不是用来判断任务本身慢
STALE_AFTER = 30 * 60

#: 超过这个量要管理员确认。**量不出来时一律按超了处理**（见 `needs_review`）
REVIEW_TB = 1.0


class MoveError(DeliveryError):
    """迁移编排出错。"""


def plan(source: str, dest: str) -> dict:
    """两个地址 → 用哪条链。纯函数，不碰网络。

    四条链共用这一个判断，**不让人选「这是哪种搬运」** —— 那是系统能自己看出来的事，
    而人选错的表现是数据往反方向覆盖（预热和沉降尤其），那不可逆。

      oss/tos 之间      对象存储搬运（阿里在线迁移 / 火山 DMS）
      oss ↔ cpfs        阿里 CPFS 数据流动：预热 / 沉降
      tos ↔ vepfs       火山 vePFS 数据流动：预热 / 沉降

    其余组合明确拒绝，**不留「以后再说」的分支** —— 一个返回 None 的分支
    迟早会被当成「没问题」传下去。
    """
    src, dst = _parse(source, "源"), _parse(dest, "目标")
    pair = f"{src['scheme']}->{dst['scheme']}"
    # 并行文件系统那两条先判：它们和对象存储之间的组合会被下面的 `dst == oss`
    # 误收进在线迁移分支（`cpfs -> oss` 的目的确实是 oss），而那条链根本搬不了文件系统
    if {src["scheme"], dst["scheme"]} & {"cpfs", "vepfs"}:
        return _dataflow(src, dst, pair)
    # `src://<源标识>/<前缀>/` = 已登记的第三方存储。**凭证不在这个串里** ——
    # 它只带标识，真正的 AK/SK 在 identity/transfer-sources.json（600），
    # 提交任务那一刻才解出来。见 sources.py 开头那段。
    if dst["scheme"] == "oss" and src["scheme"] in ("oss", "tos", "src"):
        engine = "mgw"
    elif dst["scheme"] == "tos" and src["scheme"] in ("oss", "tos"):
        engine = "dms"
    elif src["scheme"] == "src":
        # 第三方只能往我们的 OSS 搬：进 TOS 要把对方的 AK 交给火山的迁移服务，
        # 那是把别人的钥匙再转一手给第三方，不做
        raise MoveError("第三方数据源只能搬进我们的 OSS")
    else:
        raise MoveError(f"{pair} 这个方向面板还接不了，找管理员用命令行搬")
    return {"engine": engine, "src": src, "dest": dst, "direction": pair}


def _dataflow(src: dict, dst: dict, pair: str) -> dict:
    """并行文件系统那两条：预热和沉降。

    **方向由地址推出来，任务里带的那个 `action` 才是真正生效的东西。**
    搞反的后果不是报错，是把数据往相反方向覆盖一遍。
    """
    from .clouds import nas, vepfs

    action = nas.direction(src["scheme"], dst["scheme"])
    if action:
        fs = src if src["scheme"] == "cpfs" else dst
        other = dst if src["scheme"] == "cpfs" else src
        return {
            "engine": "nas",
            "action": action,
            "src": src,
            "dest": dst,
            "fs": fs,
            "store": other,
            "direction": pair,
        }
    action = vepfs.direction(src["scheme"], dst["scheme"])
    if action:
        fs = src if src["scheme"] == "vepfs" else dst
        other = dst if src["scheme"] == "vepfs" else src
        return {
            "engine": "vepfs",
            "action": action,
            "src": src,
            "dest": dst,
            "fs": fs,
            "store": other,
            "direction": pair,
        }
    if src["scheme"] == dst["scheme"]:
        raise MoveError(
            f"{src['scheme']} 到 {src['scheme']} 搬不了 —— 并行文件系统之间没有直连，"
            "要先沉降到对象存储、跨过去、再预热回来（三段，面板还没接）"
        )
    raise MoveError(
        f"{pair} 这个方向搬不了。CPFS 只能和阿里 OSS 之间流动，"
        "vePFS 只能和火山 TOS 之间流动 —— 跨云那一段要分开提"
    )


def _parse(uri: str, label: str) -> dict:
    text = str(uri or "").strip()
    if "://" not in text:
        raise MoveError(f"{label}路径要形如 oss://桶名/目录/")
    scheme, rest = text.split("://", 1)
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise MoveError(f"{label}路径里没有桶名")
    return {"scheme": scheme, "bucket": bucket, "prefix": prefix}


def needs_review(size_bytes: int, *, known: bool) -> bool:
    """这次搬运要不要管理员额外点头。

    **量不出来一律返回 True。** bot 那边量不出来时返回 (0,0)，于是判定恒为 False ——
    一个 100TB 的任务会被当成 0 字节直接放行。不知道多大必须当大的。
    """
    if not known:
        return True
    return size_bytes / (1024**4) > REVIEW_TB


def job_name(ticket_id: str, attempt: int = 1) -> str:
    """迁移任务名。**用申请单号** —— 两朵云都按名字认任务，
    所以同一张单重复提交不会搬第二遍，而且云上那个任务名能直接对回台账。

    **重试要换名字。** 两边「按名字认」的实现都是「找到同名的就复用」：
    阿里撞 `ImportJobRepeated` 直接返回旧任务名，火山 `find_task()` 把旧的 id 捞回来。
    失败重试时如果还用原名，捞回来的是那个**已经失败的**任务 —— 单子在
    NEW → FAILED 之间空转，看起来一直在重试，实际一次都没重新跑过。

    第一次不带后缀，台账和云控制台上仍然是干净的 `panel-<单号>`。
    """
    clean = "".join(c for c in str(ticket_id or "") if c.isalnum() or c in "-_")
    if not clean:
        raise MoveError("申请单号为空，没法给迁移任务命名")
    try:
        nth = int(attempt)
    except (TypeError, ValueError):
        nth = 1
    tail = "" if nth <= 1 else f"-r{nth}"
    # 截的是单号，不是整个串 —— 截整串会把 `-r2` 削掉，于是重试又用回原名
    return f"panel-{clean[: 60 - len('panel-') - len(tail)]}{tail}"


def start(ticket: dict, *, submit: Callable, now: Optional[float] = None) -> dict:
    """把任务提上去。返回要写回单子的字段。

    `submit(plan, job)` 由调用方注入（真跑时是 `mgw.submit` / `dms.submit`，
    测试里是替身）。**这里不 import 任何云 SDK** —— 编排是纯逻辑，能完整地测。
    """
    at = time.time() if now is None else now
    stage = str(ticket.get("move_stage") or STAGE_NEW)
    if stage == STAGE_RUNNING:
        raise MoveError("这张单的搬运已经在跑了")
    if stage == STAGE_DONE:
        raise MoveError("这张单已经搬完了")
    if stage == STAGE_FAILED:
        # 直接再提一次很自然，但那样 attempt 不变、任务名不变，两朵云都会把
        # 那个**已经失败的**任务原样捞回来。重试必须走 `retry()`，它会把次数加上去
        raise MoveError("失败的搬运要走重试，不能直接再提一次")
    payload = ticket.get("payload") or {}
    got = plan(str(payload.get("source") or ""), str(payload.get("dest") or ""))
    nth = int(ticket.get("move_attempt") or 1)
    name = job_name(str(ticket.get("id") or ""), nth)
    ref = submit(got, name)
    return {
        "move_stage": STAGE_RUNNING,
        # **任务名和轮询句柄不是一个东西，别合成一个字段。**
        # 阿里按任务名轮询，两者恰好相等；火山返回的是个数字 task_id，
        # 拿任务名去查它会得到「任务号不是数字」—— 而那句话走的是「查进度失败」分支，
        # 不算失败，于是单子永远停在在途、没人会发现。
        "move_job": name,
        "move_ref": str(ref or name),
        "move_attempt": nth,
        "move_engine": got["engine"],
        "move_started_ts": at,
        "move_updated_ts": at,
        "move_polled_ts": at,
        "move_error": "",
    }


def advance(ticket: dict, status: dict, *, now: Optional[float] = None) -> dict:
    """拿一次轮询结果推进状态。返回要写回单子的字段（没变化就返回空）。

    **只推进不回退**：已经 DONE / FAILED 的单子，再来什么轮询结果都不改它。
    一次网络抖动不该让失败的任务看起来又活了。
    """
    at = time.time() if now is None else now
    stage = str(ticket.get("move_stage") or "")
    if stage not in (STAGE_NEW, STAGE_RUNNING):
        return {}
    why = str(status.get("error") or "")
    got_status = bool(status.get("status"))
    # **「查过了」和「有进展」是两回事。** `move_polled_ts` 每轮都刷，它只说明定时器还活着；
    # `move_updated_ts` 是 `stuck()` 的判据，只有真从云上读到状态才刷 —— 否则任务被人删掉、
    # 或者返回一个我们不认识的终态，单子会永远在途而且永远不上「卡住」清单
    out = {"move_polled_ts": at}
    if got_status:
        out["move_updated_ts"] = at
        out["move_bytes"] = int(status.get("bytes") or 0)
        out["move_objects"] = int(status.get("objects") or 0)
    if status.get("done"):
        out["move_stage"] = STAGE_DONE
        out["move_done_ts"] = at
        # **搬完了也可能有对象没搬过去。** 丢掉这句话，台账上就是一个干净的「已完成」，
        # 而少掉的那几个文件要等几个月后训练读到才发现
        out["move_error"] = why[:300]
    elif status.get("failed"):
        out["move_stage"] = STAGE_FAILED
        out["move_error"] = (why or "迁移服务报告任务中断")[:300]
        out["move_done_ts"] = at
    elif why:
        # 查进度失败 ≠ 任务失败。记下来但不改 stage，下一轮再看
        out["move_error"] = why[:300]
    elif got_status:
        # 这一轮读到了状态而且没毛病 —— 把上一轮抖动留下的红字清掉，
        # 不清的话一次网络波动会在单子上留一条永久的错误
        out["move_error"] = ""
    return out


def retry(ticket: dict) -> dict:
    """把失败的单子放回 NEW，让它能重新提交。

    **只有 FAILED 能重试。** 在途的单子重试等于并行跑两份 ——
    迁移服务那边按任务名幂等挡得住，但台账会有两条互相覆盖的进度。
    """
    if str(ticket.get("move_stage") or "") != STAGE_FAILED:
        raise MoveError("只有失败的搬运能重试")
    # 次数要加 —— 不加的话下一次提交会把那个已经失败的同名任务原样捞回来，
    # 单子在 NEW → FAILED 之间空转，看起来一直在重试，实际一次都没重新跑过
    return {
        "move_stage": STAGE_NEW,
        "move_error": "",
        "move_job": "",
        "move_ref": "",
        "move_attempt": int(ticket.get("move_attempt") or 1) + 1,
    }


def pending(tickets) -> list:
    """在途、而且该去问一次进度的单子。

    面板没有 bot 那种常驻轮询线程 —— 推进靠定时任务。所以这里返回的是
    「该轮询的」，不是「卡住的」：一张刚提上去的单子也在里面。
    """
    out = []
    for ticket in tickets or ():
        if str(ticket.get("move_stage") or "") != STAGE_RUNNING:
            continue
        if not str(ticket.get("move_ref") or ticket.get("move_job") or ""):
            continue
        out.append(ticket)
    return out


def stuck(ticket: dict, *, now: Optional[float] = None) -> bool:
    """在途但很久没动静了。**只是提示，不改状态** ——
    真判失败要有迁移服务给的终态，不能靠「我们这边没收到消息」。"""
    at = time.time() if now is None else now
    if str(ticket.get("move_stage") or "") != STAGE_RUNNING:
        return False
    return at - float(ticket.get("move_updated_ts") or 0) > STALE_AFTER
