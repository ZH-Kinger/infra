"""每朵云的差异都写在这里，**只写在这里**。

为什么要有这个文件
──────────────────
「阿里云和火山有什么不一样」这件事本来散在六七个地方：notify 里一张显示名表、flows 里
一张 endpoint 表、catalog 里按平台写死的 ARN 正则、provision 里环境变量命名的分支、
前端再来一份平台名。加第三朵云要把这些全找一遍，漏掉一处就是一个只在某个页面上出现的
bug —— 而且往往要等到有人真的申请了才发现。

更贵的教训在隔壁：bot 的火山凭证策略是**复制**阿里那份改的，结果阿里侧因为线上事故修好的
「桶信息动作不能叠前缀条件」，火山那份至今还留着原样。复制出来的东西，修的时候只会修一边。

所以这里的规矩是：**差异用数据描述，逻辑只写一份。** 加一朵新云 = 在这个文件里加一个
`Platform`，外加 `clouds/<平台>.py` 一个签名器和 `provision.py` 一个执行器。模板、审批、
台账、到期回收、前端都不用动 —— 它们只认 `platform` 这个字符串。

能力开关（`session_policy` / `long_term`）不是配置项，是**事实记录**：某条路有没有被
真机验证过。没验过就让它 False，代码在入口处失败并说明原因，而不是发出去一把权限比
批准范围更大的凭证。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import DeliveryError

_BJ = timezone(timedelta(hours=8))


class PlatformError(DeliveryError):
    """不认识的云平台。"""


@dataclass(frozen=True)
class StorageDialect:
    """对象存储在这朵云的策略方言。

    两朵云的策略**结构完全一样**（桶信息一条、列清单一条、下载一条、上传一条、兜底 Deny
    一条，每条叠时间窗），差的只是动作叫什么、资源怎么写、时间键叫什么。所以策略生成的
    逻辑在 grants.py 里只有一份，这里只描述差异。
    """

    #: 桶元信息。**任何能力集都给，且独立成一条不带前缀条件的语句** —— 桶级请求不带
    #: prefix 参数，叠上去会被服务端判否，表现是「凭证发了但什么也干不了」
    bucket_actions: tuple
    #: 列清单（不含下载）。桶级动作，Resource 必须是桶本身
    list_actions: tuple
    download_actions: tuple
    #: 上传。**不含任何删除动作**
    write_actions: tuple
    #: 兜底拒绝：删除和改 ACL。写权限不等于删权限，也不等于能把桶改成公开
    deny_actions: tuple
    time_key: str
    ip_key: str
    prefix_key: str
    #: 阿里云的策略文档要 `"Version": "1"`，火山的**不能有**这个字段
    versioned: bool
    #: 桶的资源标识模板，例如 `acs:oss:*:*:{bucket}` / `trn:tos:::{bucket}`
    arn: str

    def bucket_arn(self, bucket: str) -> str:
        return self.arn.format(bucket=bucket)

    def object_arn(self, bucket: str, prefix: str) -> str:
        return self.bucket_arn(bucket) + (f"/{prefix}*" if prefix else "/*")

    def time_value(self, epoch: float) -> str:
        """阿里收带时区的 ISO8601，火山官方示例是 UTC 的 `Z`（两种它都认，统一发 Z）。"""
        if self.versioned:
            return datetime.fromtimestamp(int(epoch), tz=_BJ).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Platform:
    id: str
    #: 给人看的名字。前端、飞书卡片、审批摘要都从这里取，不各自维护一张表
    name: str
    #: 对象存储的 URI scheme 与对外域名模板
    storage_scheme: str
    endpoint: str
    #: 控制台登录地址模板，`{account}` 是主账号 ID
    console_login: str
    #: 角色标识的格式。捕获组 `account` 用来核对角色确实属于这个云账号
    role_pattern: re.Pattern
    #: 执行身份 / 发放身份的环境变量后缀。两朵云的叫法不一样，别在别处 if
    env_ak: str
    env_sk: str
    #: STS 的会话策略（发凭证时现场收窄到单桶单目录）**验证过没有**。
    #: False = 这朵云不准走 STS 发凭证：不验就发等于把整个角色的范围交出去
    session_policy: bool
    #: 长期凭证（建子账号 + 时间窗策略 + 长期 AK）实现并验证过没有
    long_term: bool
    #: 自助管理 AccessKey 的控制台地址。**面板只把人领到这里，不代办** ——
    #: 代办意味着面板要经手用户的 secret，而 `sealed` 那一整套存在的理由
    #: 就是「面板不持有明文」。为省几次点击把面板变成所有人密钥的中转站，不划算
    key_console: str = ""
    storage: Optional[StorageDialect] = field(default=None)

    def bucket_url(self, bucket: str, region: str) -> str:
        return f"{bucket}.{self.endpoint.format(region=region)}"

    def login_url(self, account: str) -> str:
        return self.console_login.format(account=account)


ALIYUN = Platform(
    id="aliyun",
    name="阿里云",
    storage_scheme="oss",
    endpoint="oss-{region}.aliyuncs.com",
    console_login="https://signin.aliyun.com/{account}.onaliyun.com/login.htm",
    key_console="https://ram.console.aliyun.com/profile/access-keys",
    role_pattern=re.compile(r"^acs:ram::(?P<account>[0-9]+):role/[A-Za-z0-9._-]{1,64}$"),
    env_ak="ACCESS_KEY_ID",
    env_sk="ACCESS_KEY_SECRET",
    session_policy=True,
    long_term=True,
    storage=StorageDialect(
        bucket_actions=("oss:GetBucketInfo", "oss:GetBucketStat", "oss:GetBucketAcl"),
        list_actions=("oss:ListObjects", "oss:GetBucketMultipartUploads"),
        download_actions=("oss:GetObject",),
        write_actions=("oss:PutObject", "oss:AbortMultipartUpload", "oss:ListParts"),
        deny_actions=(
            "oss:DeleteObject",
            "oss:DeleteObjectVersion",
            "oss:DeleteBucket",
            "oss:PutBucketAcl",
            "oss:PutObjectAcl",
        ),
        time_key="acs:CurrentTime",
        ip_key="acs:SourceIp",
        prefix_key="oss:Prefix",
        versioned=True,
        arn="acs:oss:*:*:{bucket}",
    ),
)

VOLCANO = Platform(
    id="volcano",
    name="火山引擎",
    storage_scheme="tos",
    endpoint="tos-{region}.volces.com",
    console_login="https://console.volcengine.com/auth/login/user/{account}",
    key_console="https://console.volcengine.com/iam/keymanage/",
    role_pattern=re.compile(r"^trn:iam::(?P<account>[0-9]+):role/[A-Za-z0-9._-]{1,64}$"),
    env_ak="ACCESS_KEY",
    env_sk="SECRET_KEY",
    # 火山 AssumeRole 到底认不认 Policy 参数没有取证过。静默忽略它 = 把整个角色的范围
    # 发出去，所以这条路关着，火山的凭证一律走长期路径（策略里写死时间窗和范围）
    session_policy=False,
    long_term=True,
    storage=StorageDialect(
        # 火山没有 GetBucketInfo / GetBucketStat 这种动作，最接近的是 HeadBucket
        bucket_actions=("tos:HeadBucket", "tos:GetBucketLocation"),
        list_actions=("tos:ListBucket", "tos:ListBucketMultipartUploads"),
        download_actions=("tos:GetObject",),
        write_actions=("tos:PutObject", "tos:AbortMultipartUpload", "tos:ListMultipartUploadParts"),
        deny_actions=(
            "tos:DeleteObject",
            "tos:DeleteBucket",
            "tos:PutBucketAcl",
            "tos:PutObjectAcl",
        ),
        time_key="volc:CurrentTime",
        ip_key="volc:SourceIp",
        prefix_key="tos:prefix",
        versioned=False,
        # TRN 的地域段和账号段留空，官方示例即如此
        arn="trn:tos:::{bucket}",
    ),
)

ALL = (ALIYUN, VOLCANO)
BY_ID = {p.id: p for p in ALL}
IDS = tuple(p.id for p in ALL)
NAMES = {p.id: p.name for p in ALL}


def get(platform: str) -> Platform:
    try:
        return BY_ID[platform]
    except KeyError:
        raise PlatformError(f"不认识的云平台：{platform!r}") from None


def name_of(platform: str) -> str:
    """显示名。拿不到就原样返回 —— 显示用途不该因为一个陌生的平台串就抛异常。"""
    return NAMES.get(platform, platform)


def storage_of(platform: str) -> StorageDialect:
    dialect = get(platform).storage
    if dialect is None:
        raise PlatformError(f"{name_of(platform)}还没有接入对象存储凭证")
    return dialect


def cred_env_names(platform: str, prefix: str) -> tuple:
    """(AK 环境变量名, SK 环境变量名)。两朵云的后缀叫法不一样。"""
    p = get(platform)
    return f"{prefix}_{p.env_ak}", f"{prefix}_{p.env_sk}"
