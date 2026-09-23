"""长期数据访问凭证：命名、时间窗策略文档、到期判定。

**纯逻辑，不调任何云接口**，所以能被完整测试——而这正是整个功能里最容易出安全事故的一块：
策略写宽一格，发出去的就是一把越权的长期钥匙。

为什么需要它
────────────
阿里云 STS 的 `AssumeRole` 最长 12 小时（`DurationSeconds` 上限 43200），这是云厂商的硬限制。
超过 12 小时只能建一个真的 RAM 子账号发长期 AK，把有效期写进策略的 `Condition` 里——
服务端每次调用都按当前时间判，**凭证泄漏也随到期自动失效**，不依赖我们准时删号。
删号只是清理残留。

能力模型（三者正交，对齐 bot 的 core/temp_ak_issuance/policy.py）
──────────────────────────────────────────────────────────
  list      ListObjects / GetBucketMultipartUploads —— 只能看清单，**不能下载**
  download  GetObject                              —— 才给下载内容
  write     PutObject / AbortMultipartUpload / ListParts —— **不含任何删除动作**

桶元数据（GetBucketInfo/Stat/Acl）**勾了任何一项就给，且独立成条**：
少了它，只勾"上传"的使用方在探桶那一步就 403，表现是「凭证发了但什么也干不了」
（bot 线上「元客」那单踩过）。这条**绝不能叠 `oss:Prefix`**——桶级请求不带 prefix 参数，
叠上去会被服务端判假拒绝。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from . import platforms
from .errors import DeliveryError

#: 长期凭证的子账号与策略前缀。执行身份的云上策略按这些前缀收窄授权，
#: 所以**改名等于绕过那道闸**——要改必须同步改云上 wuji-panel-issuer 那条策略
#: （现网副本在 deploy/panel/cloud-policies/）。
#:
#: **内部和外部分开两套前缀。** 以前两边都是 `tempak-`：面板发给同事的、机器人发给
#: 供应商的，在 RAM 控制台上长得一模一样，只能靠显示名分辨 —— 而出事那一刻，
#: 「这把钥匙在公司里还是在公司外」是最先要回答的问题。
USER_PREFIX = "staff-"  # 面板发给内部同事的
POLICY_PREFIX = "staff-oss-auto-"
#: 机器人发给外部使用方的（历史凭证也都是这套，撤销时还要认它）
EXTERNAL_USER_PREFIX = "tempak-"
EXTERNAL_POLICY_PREFIX = "temp-ak-auto-"
_PREFIX_PAIRS = ((USER_PREFIX, POLICY_PREFIX), (EXTERNAL_USER_PREFIX, EXTERNAL_POLICY_PREFIX))

#: **程序发出来的子账号前缀，全仓唯一一份。**（面板发的 + 机器人发的 + 更早的历史写法）
#: 体检、审计、工作空间、离职保护各处都引用它 —— 这次前缀改名漏掉了其中四处，
#: 而其中两处是安全边界（受保护名单）。各写各的字面量迟早只改一边
ISSUED_PREFIXES = (USER_PREFIX, EXTERNAL_USER_PREFIX, "temp-ak-", "panel-")

CAP_LIST, CAP_DOWNLOAD, CAP_WRITE = "list", "download", "write"
CAPS = (CAP_LIST, CAP_DOWNLOAD, CAP_WRITE)

#: 每朵云的动作名、ARN 写法、时间键都在 platforms.py 的 StorageDialect 里。
#: **这里不再留第二份** —— 曾经这两处逐字重复过，而重复的表只会被修一边
#: （bot 那边就是：阿里侧为线上事故修好的桶信息语句，火山那份至今还是原样）。


#: RAM 登录名规则：小写字母数字加 .-_，首字符必须是字母
_SLUG_OK = re.compile(r"[^a-z0-9]+")
_BUCKET_OK = re.compile(r"\A[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
#: 目录前缀：不允许 .. 、反斜杠、控制字符、通配符——它们会被带进策略的 Resource
_PREFIX_BAD = re.compile(r"(\.\.|[\\*?\x00-\x1f])")

_BJ = timezone(timedelta(hours=8))


class GrantError(DeliveryError):
    """凭证参数不合法。"""


def iso8601_bj(epoch: float) -> str:
    """epoch → `2026-09-17T10:00:00+08:00`。RAM 的 Date* 条件要求带时区。"""
    return datetime.fromtimestamp(int(epoch), tz=_BJ).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def bj_time(epoch: float) -> str:
    """epoch → `2026-09-17 10:00`（北京时间）。给人看的，不是给 RAM 条件用的。"""
    return datetime.fromtimestamp(int(epoch), tz=_BJ).strftime("%Y-%m-%d %H:%M")


def slug(subject: str) -> str:
    """使用方名称 → RAM 登录名里能用的片段。

    取不出拉丁字母时（中文使用方，例如「元客」「枢途」）**不能一律回落成同一个词**——
    那样云上会出现一堆 `tempak-ext-*`，只靠随机后缀区分，运维看账号名根本认不出是谁的。
    改用名字本身的短哈希：同一个使用方每次都得到同一个片段，不同使用方不会撞。
    """
    out = _SLUG_OK.sub("-", (subject or "").strip().lower()).strip("-")[:20].strip("-")
    if out:
        return out
    name = (subject or "").strip()
    if not name:
        return "ext"
    # 只用来起个可读的名字，不是安全用途；blake2s 省掉 ruff 对 sha1 的告警
    return "x" + hashlib.blake2s(name.encode("utf-8"), digest_size=4).hexdigest()


def readable(subject: str) -> bool:
    """`slug` 这次是真取出了拉丁字母，还是退化成哈希了。

    **显式问，别靠形状猜**：退化值长成 `x` + 8 位十六进制，而 `xuzhiyuan`（徐志远）
    这类拼音邮箱前缀恰好也是 x 开头的 9 个字母 —— 按形状判会把他们判成哈希，
    于是本该可读的名字又被换成一串乱码。
    """
    return bool(_SLUG_OK.sub("-", (subject or "").strip().lower()).strip("-"))


def user_name(subject: str, *, rand: Optional[str] = None, email: str = "") -> str:
    """`staff-<谁>-<6hex>`。随机后缀让同一个人的多次发放互不覆盖。

    **优先用邮箱前缀**：中文名转不出拉丁字母时 `slug` 会退化成哈希，
    于是云上出现 `staff-x090fd8a0-…` 这种名字，运维在 RAM 控制台上根本认不出是谁
    （线上真出现过）。邮箱前缀可读、稳定、人人都有。
    """
    local = str(email or "").split("@", 1)[0]
    who = slug(local) if readable(local) else ""
    if not who:
        who = slug(subject)
    return f"{USER_PREFIX}{who}-{rand or secrets.token_hex(3)}"


def policy_name(user: str) -> str:
    """子账号 → 它那条自定义策略的名字。**内外两套前缀都要认**：
    撤销和到期清理会处理历史上发出去的外部凭证，只认新前缀的话它们永远清不掉。"""
    for user_prefix, policy_prefix in _PREFIX_PAIRS:
        if user.startswith(user_prefix):
            return policy_prefix + user
    allowed = " / ".join(x for x, _ in _PREFIX_PAIRS)
    raise GrantError(f"长期凭证的子账号必须以 {allowed} 开头：{user}")


def check_bucket(bucket: str) -> str:
    b = (bucket or "").strip().lower()
    if not _BUCKET_OK.match(b):
        raise GrantError(f"桶名不合法：{bucket!r}")
    return b


def check_prefix(prefix: str) -> str:
    """目录前缀。空串 = 整桶。末尾补 `/`，避免 `data` 意外匹配到 `database/`。"""
    p = (prefix or "").strip().lstrip("/")
    if not p:
        return ""
    if _PREFIX_BAD.search(p):
        raise GrantError(f"目录前缀不合法（不能含 .. 、反斜杠或通配符）：{prefix!r}")
    return p if p.endswith("/") else p + "/"


def check_caps(caps: Iterable[str]) -> tuple:
    got = tuple(c for c in CAPS if c in set(caps or ()))
    if not got:
        raise GrantError("至少要选一项权限（list / download / write）")
    return got


#: STS 会话策略的硬上限（阿里云 AssumeRole 的 Policy 参数，2048 字符）
SESSION_POLICY_MAX = 2048


def session_policy(doc: dict) -> str:
    """策略文档 → AssumeRole 的 Policy 参数。

    超长直接抛错、**不做任何截断或降级**：截断出来的策略仍是合法 JSON，但少掉的
    往往正是那条 Deny，结果是悄悄发出一把比批准范围更大的凭证。
    """
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    if len(text) > SESSION_POLICY_MAX:
        raise GrantError(
            f"会话策略 {len(text)} 字符，超过 {SESSION_POLICY_MAX} 上限（目录名太长？）"
        )
    return text


def build_policy(
    platform: str,
    bucket: str,
    *,
    prefix: str = "",
    caps: Iterable[str],
    not_before: float,
    expire: float,
    source_ips: Optional[Iterable[str]] = None,
) -> dict:
    """单桶 + 单目录 + 能力集 + 时间窗 → 策略文档。两朵云共用这一份逻辑。

    结构照搬阿里云那套线上验证过的（bot 的 core/temp_ak_issuance/policy.py），因为它的
    每一条都是踩出来的：

      · 桶信息动作**独立成条、桶级、绝不叠前缀条件** —— 桶级请求不带 prefix 参数，
        叠上去会被服务端判否，表现是「凭证发了但什么也干不了」（元客那单）。
      · caps 只要非空就给桶信息 —— 只勾「上传」的使用方同样要探桶。
      · 列清单用**桶级** ARN + 前缀条件；下载/上传用对象级 ARN（前缀已经写进 ARN，不再叠条件）。
      · 每一条都叠时间窗，桶信息那条也不例外 —— bot 早期漏了它，凭证到期后外部方仍能调
        GetBucketInfo，直到清理任务当天删号为止，与「到期自动失效」的宣称矛盾。
      · 末尾兜底 Deny 删除和改 ACL：写权限不等于删权限，也不等于能把桶改成公开。
    """
    spec = platforms.storage_of(platform)
    bucket = check_bucket(bucket)
    prefix = check_prefix(prefix)
    caps = check_caps(caps)
    if not_before >= expire:
        raise GrantError("生效时间必须早于到期时间")

    base = spec.bucket_arn(bucket)
    obj = spec.object_arn(bucket, prefix)

    def cond(with_prefix: bool = False) -> dict:
        c = {
            "DateGreaterThan": {spec.time_key: spec.time_value(not_before)},
            "DateLessThan": {spec.time_key: spec.time_value(expire)},
        }
        if source_ips:
            c["IpAddress"] = {spec.ip_key: sorted(set(source_ips))}
        if with_prefix and prefix:
            c["StringLike"] = {spec.prefix_key: [prefix, prefix + "*"]}
        return c

    stmts = [
        {
            "Effect": "Allow",
            "Action": list(spec.bucket_actions),
            "Resource": [base],
            "Condition": cond(),
        }
    ]
    if CAP_LIST in caps:
        stmts.append(
            {
                "Effect": "Allow",
                "Action": list(spec.list_actions),
                "Resource": [base],
                "Condition": cond(True),
            }
        )
    if CAP_DOWNLOAD in caps:
        stmts.append(
            {
                "Effect": "Allow",
                "Action": list(spec.download_actions),
                "Resource": [obj],
                "Condition": cond(),
            }
        )
    if CAP_WRITE in caps:
        stmts.append(
            {
                "Effect": "Allow",
                "Action": list(spec.write_actions),
                "Resource": [obj],
                "Condition": cond(),
            }
        )
    stmts.append(
        {
            "Effect": "Deny",
            "Action": list(spec.deny_actions),
            "Resource": [spec.bucket_arn("*")],
        }
    )
    doc = {"Statement": stmts}
    # 阿里要 Version，火山**不能有**这个字段
    return {"Version": "1", **doc} if spec.versioned else doc
