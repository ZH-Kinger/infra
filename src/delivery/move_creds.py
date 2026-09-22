"""跨云搬运时交给**对方云**的那把源端钥匙。

问题是什么
──────────
跨云迁移的机制是：把源端的 AK/SK **写进对方云的迁移任务配置里**，由对方云拿着它
来读我们的桶。也就是说那串东西离开了我们，长期留在另一家云的控制面上，
**撤不回来，只能轮换**。

原先这里直接用面板的**开通身份**——那把能 `ram:CreateUser` + `ram:AttachPolicyToUser`，
也就是能在我们账号里凭空造一个有权限的号。把它交给另一家云的服务去保管，
是这条链上最贵的一个错：泄漏面不是「这批数据」，是整个账号。

怎么解
──────
现场签一把**新的**：只读、只限这次要搬的那个前缀、带时间窗，搬完就删。
用的是面板本来就有的签发机器（`issuer.issue_long_term`，和发数据访问凭证同一套），
**不新增常驻身份**。交出去的东西从「能建号」变成「只能读这一个前缀，还会过期」。

为什么不用 STS
──────────────
火山 DMS 的源配置只有 ak/sk 两个字段，**没有 SecurityToken**；阿里在线迁移那边
第三方源同理。带 token 的临时凭证塞不进去。所以只能是长期 AK + 策略里的时间窗——
服务端逐次调用判时间，到期即失效，和临时凭证的效果一样，只是多一步删账号。

时间窗给多长
────────────
按**搬运本身**的时长给，不是按审批。19.5 TiB 那单实测跑了约 38 小时，
所以默认 14 天：窗口短于任务时长的后果是搬到一半突然 403，而那时进度是不可恢复的
（迁移服务会把整个任务判失败）。搬完立刻删，不等到期。
"""

from __future__ import annotations

from typing import Optional

from . import grants as grants_mod
from .errors import DeliveryError

#: 交出去的钥匙能干的事：列清单 + 读对象。**没有写、没有删。**
#: 目的端不用它 —— 阿里那边写目的桶走 RAM 角色，火山那边用它自己的账号凭证
CAPS = (grants_mod.CAP_LIST, grants_mod.CAP_DOWNLOAD)

#: 默认时间窗。见模块开头「时间窗给多长」
DEFAULT_DAYS = 14


class MoveCredError(DeliveryError):
    """跨云源端凭证签不出来。"""


def needed(plan: dict) -> str:
    """这次搬运要不要跨云凭证；要的话返回**源端**是哪朵云。

    同云不走这里：阿里那条源用 RAM 角色（根本没有 AK 要交），
    火山那条的钥匙从头到尾没离开火山。
    """
    src = str((plan.get("src") or {}).get("scheme") or "")
    dest = str((plan.get("dest") or {}).get("scheme") or "")
    if src == "oss" and dest == "tos":
        return "aliyun"
    if src == "tos" and dest == "oss":
        return "volcano"
    return ""


def user_name(ticket_id: str) -> str:
    """这张单子的源端子账号名。

    **按单号定名，不随机。** 随机的话，进程在「建了号还没写回单子」之间挂掉，
    云上就留下一个谁也对不上的账号；按单号定名则重试必然命中同一个，
    要么复用、要么能照名字找回来删。
    """
    tail = "".join(c for c in str(ticket_id or "") if c.isalnum())[-20:].lower()
    if not tail:
        raise MoveCredError("没有单号，签不出源端凭证")
    return f"{grants_mod.USER_PREFIX}move-{tail}"


def mint(
    issuer,
    *,
    ticket_id: str,
    bucket: str,
    prefix: str,
    platform: str,
    now: float,
    days: int = DEFAULT_DAYS,
) -> tuple:
    """签一把只读的源端钥匙。返回 `(ak, sk, 子账号名)`。

    **幂等靠名字**：同一张单重试时撞「子账号已存在」，我们先把旧的删掉再签 ——
    不这样的话第二次提交永远失败，而失败的表现是「跨云迁移签不出凭证」，
    指不到根因是上一次留下的残留。
    """
    expire = now + days * 86400
    doc = grants_mod.build_policy(
        platform, bucket, prefix=prefix, caps=CAPS, not_before=now, expire=expire
    )
    user = user_name(ticket_id)
    try:
        cred = issuer.issue_long_term(user, f"面板跨云搬运 {ticket_id}", doc)
    except Exception as exc:  # noqa: BLE001
        if not _exists(exc):
            raise MoveCredError(f"签源端只读凭证失败：{str(exc)[:200]}") from exc
        # 上一次留下的残留。删干净再签一把 —— 旧的那把 AK 我们没存过，留着也用不了
        issuer.revoke_long_term(user)
        try:
            cred = issuer.issue_long_term(user, f"面板跨云搬运 {ticket_id}", doc)
        except Exception as again:  # noqa: BLE001
            raise MoveCredError(f"清掉残留后重签还是失败：{str(again)[:200]}") from again
    return cred.access_key_id, cred.access_key_secret, user


def drop(issuer, user: str) -> list:
    """搬完（或失败）就把这把钥匙删掉。返回没删掉的东西。

    **不等时间窗到期。** 窗是兜底，不是回收手段 —— 一把还能用两周的钥匙躺在
    对方云的任务配置里，和我们已经搬完了这件事没有任何关系。
    """
    if not user:
        return []
    return list(issuer.revoke_long_term(user) or [])


def _exists(exc: Exception) -> bool:
    """这个错误是不是「这个子账号已经有了」。

    **只认明确的已存在。** `Exist` 这个子串同时命中 `EntityNotExist`，
    把「不存在」当成「已存在」会走进删除分支，而那是在删一个不该删的东西。
    """
    blob = str(exc)
    return any(
        k in blob
        for k in ("EntityAlreadyExists", "AlreadyExist", "already exist", "UserAlreadyExist")
    )


def days_from_env(environ: Optional[dict] = None) -> int:
    """时间窗天数。非法值退回默认，**不让一个配错的数字把搬运拦下来**。"""
    import os

    env = os.environ if environ is None else environ
    raw = str(env.get("DELIVERY_TRANSFER_CRED_DAYS", "") or "").strip()
    if not raw.isdigit():
        return DEFAULT_DAYS
    got = int(raw)
    return got if 1 <= got <= 90 else DEFAULT_DAYS
