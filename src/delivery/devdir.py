"""个人开发目录的长期权限：每人一条策略，框住他自己那个目录。

**纯逻辑，不调云接口。**

为什么需要它
────────────
算法组的人只在 `wuji_Algorithm` 用户组里，那个组 OSS 侧只挂着 `AliyunOSSReadOnlyAccess`，
直接授予的策略是**空的**（2026-09-24 逐个查过 50 个人）。也就是说：整桶能读，
**一个字节也写不进去**。

表现很有迷惑性 —— 在 PAI 里挂载着用一切正常（挂载走的是工作空间的角色），
一旦拿自己的 AK/SK 用 ossutil / s3fs 写就 403。人的第一反应是「AK 坏了」，
于是来要新的 AK，换一把还是不行。

开发目录是 `wuji-algo-dev-hz/<部门>/<登录名>/`，每人一个。

为什么是每人一条，而不是一条挂所有人
────────────────────────────────────
原本想用 `${ram:UserName}` 这种策略变量，一条策略挂给所有人。**这条路是死的**：
阿里云 RAM 没有「自定义占位符变量」这个功能（2026-09-24 查证，见
`docs/collab/research/aliyun-ram-policy-variables.md`；动态匹配只能靠预定义的条件
关键字，而那张表里**没有表示「调用者用户名」的 key**）。

最坑的是它的失败形态：`${ram:UserName}` 写进 Resource **语法合法、保存成功**，
然后被当成字面的十六个字符去匹配 object key —— 策略挂上了、面板报成功、
人还是写不进去，而且哪儿都不报错。OSS 官方讲「目录级别精细化访问控制」那篇给的
解法就是每人一条硬编码策略。

另一个必须知道的匹配规则：**ARN 里的 `*` 跨 `/` 匹配**。`bucket/*/<登录名>/*`
不是「任意一级目录下的他」，而是「任意深度下任何叫这个名字的目录」。所以这里的
Resource **一律写死真实路径**，中间不留通配。

和临时凭证那套（`grants.py`）的区别 —— **别把两者的策略互相抄**
────────────────────────────────────────────────────────────
  `grants.py`   给外部方的：每条都叠时间窗 Condition，末尾一条 Deny 挡掉所有删除动作
  这里          自己人的自留地：不叠时间窗（长期有效），**删除要给**

删除必须给：连自己写错的文件都删不掉的目录，实际上是不可用的 —— 人会绕到别处去写，
最后开发数据散在谁也不知道的地方。这是有意的差异，不是漏抄。
"""

from __future__ import annotations

import hashlib
import json
import re

from . import platforms

#: 策略名前缀。**不能复用 `grants.POLICY_PREFIX`（`staff-oss-auto-`）**：
#: 那个前缀被临时凭证的到期清理按前缀扫，混进来的开发目录策略会被当成过期凭证删掉，
#: 表现是「人用着用着突然写不进去了」，而且没有任何地方会说是清理干掉的。
#:
#: **改这个名字 = 同时要改云上那条策略**，两边不一致就建不出来（而且失败很安静：
#: 每张单只在备注里留一行，不告警）。云上 `wuji-panel-issuer` 的 `ram:CreatePolicy`
#: 语句按前缀放行，副本在 `deploy/panel/cloud-policies/aliyun.wuji-panel-issuer.json`。
#: 2026-09-24 已加入该前缀并回读验证（策略版本 v2→v3），仓库副本按云上现状重导过。
#:
#: 只加在 issuer、**绝不能加到 executor 上**：executor 本来就能把策略挂到真人账号上，
#: 再给它建策略的能力，「内容任意 + 挂到真人身上」就单个身份成立了，这道闸就没了。
POLICY_PREFIX = "wuji-dev-dir-"
POLICY_TYPE = "Custom"

#: 自定义策略正文的长度上限（阿里云硬限制 6144 字符）
_DOC_MAX = 6144
#: 策略名长度上限
_NAME_MAX = 128

#: 阿里云策略名只收英文字母、数字和短划线 —— 真人登录名里的点号（`huang.zenan`）
#: 直接拿去建会被拒
_NAME_BAD = re.compile(r"[^a-z0-9]+")
_BUCKET_OK = re.compile(r"\A[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z")
#: 路径每一段：不许 `..`、空白、斜杠和 shell 元字符
_SEG_OK = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
#: 对象级 ARN 的形状，`targets_in` 用它从云上正文里反解出「桶 + 目录」。
#: **和 `build_policy` 里拼 ARN 的那几行是一对**，改一边必须改另一边 ——
#: 不一致的表现是「读回来一条都认不出」，而那条路是 fail-closed 的（拒绝覆盖），
#: 所以会以报错的形式暴露出来，不会变成静默抹掉
_OBJECT_ARN = re.compile(
    r"\Aacs:oss:\*:\*:(?P<bucket>[a-z0-9][a-z0-9-]{1,61}[a-z0-9])/(?P<rest>.+)/\*\Z"
)


def policy_name(username: str) -> str:
    """登录名 → 他那条策略的名字。`wuji-dev-dir-<洗过的名字>-<6位哈希>`。

    **哈希后缀不是装饰**：洗名字要把点号换成短划线，于是 `huang.zenan` 和
    （假想的）`huang-zenan` 会撞成同一个策略名 —— 后建的那条会 `EntityAlreadyExists`，
    或者更糟：两个人共用一条策略、各自的目录都授给了对方。哈希取自**原始**登录名，
    两人不同则名字必不同。

    同一个登录名每次算出来都一样，所以「这个人的策略叫什么」随时能重算，
    不需要在台账里记。
    """
    who = _require_segment(username, "登录名")
    washed = _NAME_BAD.sub("-", who.lower()).strip("-")[:48].strip("-")
    tag = hashlib.blake2s(who.encode("utf-8"), digest_size=3).hexdigest()
    name = f"{POLICY_PREFIX}{washed}-{tag}" if washed else f"{POLICY_PREFIX}{tag}"
    if len(name) > _NAME_MAX:  # pragma: no cover — 上面已截断，留着防以后改宽前缀
        raise ValueError(f"策略名 {len(name)} 字符，超过 {_NAME_MAX} 上限：{name}")
    return name


def targets_of(spaces, username: str) -> list:
    """模板的工作空间列表 → 这个人**所有**开发目录 `[(桶, 部门目录), …]`，去重保序。

    **一条策略要覆盖他的每一个目录，不是每个目录一条。** 现网是两个地域
    （杭州 `wuji-algo-dev-hz` + 新加坡 `wuji-algo-dev-sing`），只授杭州的表现是
    「在新加坡的 DSW 里写不进去」—— 而那恰恰是最不好查的一种：同一个人、
    同一把 AK、换个地域就不行，看着像是地域的问题，其实是策略少了一条。

    两个地域用同一个桶的配置也是合法的（登记表没禁），所以要去重：
    同一个 ARN 写两遍不报错，但策略正文白白变长，离 6144 上限更近。
    """
    _require_segment(username, "登录名")
    out: list = []
    for ws in spaces or ():
        bucket = str((ws or {}).get("bucket") or "")
        if not bucket:
            continue  # CPFS-only 的空间没有桶，正常
        pair = (bucket, str((ws or {}).get("bucket_prefix") or "").strip("/"))
        if pair not in out:
            out.append(pair)
    return out


def targets_in(doc, username: str) -> list:
    """从**云上现有的**策略正文里认出它已经授了哪些目录，`[(桶, 部门目录), …]`。

    给「读回来合并」用：每张单只批了它自己那几个地域，而 `CreatePolicyVersion`
    是整篇覆盖 —— 不先读回已有的，第二张单就会把第一张授的地域抹掉（且无声）。

    **只认本人的 ARN**（结尾是 `/<登录名>/*`）。认不出的一律跳过，由调用方判断
    「一条都认不出」算不算异常 —— 那意味着云上那篇不是我们写的，或者格式变了，
    这时候覆盖它等于把不认识的东西删掉。
    """
    who = _require_segment(username, "登录名")
    out: list = []
    for stmt in (doc or {}).get("Statement") or ():
        res = (stmt or {}).get("Resource")
        for arn in [res] if isinstance(res, str) else list(res or ()):
            hit = _OBJECT_ARN.match(str(arn))
            if not hit:
                continue
            rest = hit.group("rest")
            if rest == who:
                grp = ""
            elif rest.endswith("/" + who):
                grp = rest[: -len(who) - 1]
            else:
                continue  # 别人的目录，不该出现在这条策略里，更不该被我们当成自己的
            pair = (hit.group("bucket"), grp)
            if pair not in out:
                out.append(pair)
    return out


def merge_targets(old, new) -> list:
    """老的 + 新的，去重保序。老的排前面 —— 保证「已经能写的地方继续能写」。"""
    out: list = []
    for pair in list(old or ()) + list(new or ()):
        pair = (str(pair[0] or ""), str(pair[1] or "").strip("/"))
        if pair not in out:
            out.append(pair)
    return out


def build_policy(targets, username: str) -> dict:
    """产出这个人的策略文档。`targets` 是 `[(桶, 部门目录), …]`，部门目录可为空。"""
    who = _require_segment(username, "登录名")
    pairs = [(str(b or ""), str(g or "").strip("/")) for b, g in (targets or ())]
    if not pairs:
        raise ValueError("没有任何开发目录，不该建策略")
    oss = platforms.get("aliyun").storage
    statements: list = []
    for bucket, grp in pairs:
        if not _BUCKET_OK.match(bucket):
            raise ValueError(f"桶名不合 OSS 规范：{bucket!r}")
        if grp:
            _require_segment(grp, "部门目录")
        prefix = f"{grp}/{who}" if grp else who
        statements.append(
            {
                # 桶信息：**绝不能叠 `oss:Prefix`**。桶级请求本来就不带 prefix 参数，
                # 叠上去会被服务端判成不满足条件 —— 线上「拿了凭证访问不了桶」就是这个
                # （余湘港那单，见 grants.py 开头）
                "Effect": "Allow",
                "Action": list(oss.bucket_actions),
                "Resource": f"acs:oss:*:*:{bucket}",
            }
        )
        statements.append(
            {
                # 列清单：桶级 ARN，但**叠前缀条件**，和上面那条分开。
                #
                # 曾经把这两条合成一条无条件的，理由是「组里的 AliyunOSSReadOnlyAccess
                # 本来就能列整桶，收窄不多一分安全」。那个理由**只在今天成立** ——
                # 这是一条挂在真人身上、没有时间窗、没有回收路径的长期策略，
                # 哪天把算法组那条只读收窄了，这几十条策略会静默保留整桶列清单权限，
                # 而不会有任何人想起来它们还在。
                #
                # 控制台逐层点进去不受影响：控制台列目录是带 prefix 的
                "Effect": "Allow",
                "Action": list(oss.list_actions),
                "Resource": f"acs:oss:*:*:{bucket}",
                "Condition": {"StringLike": {oss.prefix_key: [f"{prefix}/", f"{prefix}/*"]}},
            }
        )
        statements.append(
            {
                # 自己的目录：读、写、删全给。
                #
                # 两条 Resource 不是重复：`<前缀>/*` 覆盖目录里的东西，
                # `<前缀>/` 是建号时放的那个 0 字节占位对象本身 —— `*` 能不能匹配空串
                # 是没写明的事，少了第二条的表现是「目录里的文件都能删、
                # 唯独那个占位删不掉」，没人能想明白为什么。
                #
                # 刻意**不含** `oss:PutObjectAcl` —— 那个能把对象改成公共读，
                # 一条命令就能把内部数据挂到公网上，而开发目录用不到它
                "Effect": "Allow",
                "Action": list(oss.download_actions)
                + list(oss.write_actions)
                + ["oss:DeleteObject", "oss:DeleteObjectVersion"],
                "Resource": [
                    f"acs:oss:*:*:{bucket}/{prefix}/*",
                    f"acs:oss:*:*:{bucket}/{prefix}/",
                ],
            }
        )
        statements.append(
            {
                # **不给 `PutObjectAcl` 还不够。** OSS 的 `PutObject` 本身收
                # `x-oss-object-acl` 请求头，服务端按 `oss:PutObject` 判 ——
                # 也就是「上传的同时把对象设成公共读」这条路，光靠不授予 ACL 动作
                # 封不住。显式 Deny 才封得住（Deny 在阿里云里压倒一切 Allow）。
                #
                # 只 Deny 改 ACL，不 Deny 上传本身 —— 他照样能传，只是传不成公开的
                "Effect": "Deny",
                "Action": ["oss:PutObjectAcl", "oss:PutBucketAcl"],
                "Resource": [
                    f"acs:oss:*:*:{bucket}",
                    f"acs:oss:*:*:{bucket}/{prefix}/*",
                    f"acs:oss:*:*:{bucket}/{prefix}/",
                ],
            }
        )
    return {"Version": "1", "Statement": statements}


def document(targets, username: str) -> str:
    """策略正文的 JSON 串（就是发给 CreatePolicy 的那一份）。"""
    doc = json.dumps(build_policy(targets, username), ensure_ascii=False)
    if len(doc) > _DOC_MAX:
        # 地域加到一定数量就会撞上。撞了要的是合并语句（多个 Resource 一条 Statement），
        # 不是砍动作 —— 砍动作就是又一次「余湘港那样的缺失」
        raise ValueError(f"策略正文 {len(doc)} 字符，超过阿里云 {_DOC_MAX} 上限")
    return doc


def dev_dir(bucket: str, group: str, username: str) -> str:
    """这个人的开发目录（给人看的路径，不是 ARN）。"""
    who = _require_segment(username, "登录名")
    grp = str(group or "").strip().strip("/")
    return f"{bucket}/{grp}/{who}/" if grp else f"{bucket}/{who}/"


def _require_segment(value: object, what: str) -> str:
    """路径里的一段：拒空、拒空白、拒斜杠、拒 `..`。

    拼错的结果是一条永远匹配不上的 ARN，而 OSS 只会回 403 ——
    看不出是策略拼错了，排查会往权限以外的方向跑。
    """
    got = str(value or "")
    if not _SEG_OK.match(got) or got in (".", ".."):
        raise ValueError(f"{what}不合法（不能为空、带空白、斜杠或 `..`）：{value!r}")
    return got
