"""个人开发目录的 OSS 读写权限：策略正文、建策略、建号流程里的那一步。

为什么这一块值得一整个文件
──────────────────────────
算法组那个用户组 OSS 侧只有 `AliyunOSSReadOnlyAccess` —— 整桶能读、**一个字节写不进去**。
而它在 PAI 里挂载着用一切正常（挂载走工作空间的角色），所以这条缺失的表现是
「换了三把 AK 都写不进去」，没人会往策略上想。

这里锁住的每一条都是「以后最容易被顺手简化掉」的那种设计：

  1. 一条策略覆盖他**所有**地域的开发目录（少一条 = 在新加坡写不进去）
  2. Resource 里不许出现中间通配（ARN 的 `*` 跨 `/` 匹配，`bucket/*/<名>/*` 比预期宽得多）
  3. 桶信息单独一条、**无 Condition**（余湘港那个 403）；列清单另起一条、**叠前缀条件**
  4. 动作集合：读要带版本读、写要带分片、**删要给**（和外部凭证那套刻意相反）
  5. 不授 `oss:PutObjectAcl` 还不够，要显式 Deny（`PutObject` 收 `x-oss-object-acl` 头）
  6. 策略名带哈希后缀（`huang.zenan` 和 `huang-zenan` 洗完会撞）
  7. 建策略用发放身份、挂策略用开通身份 —— 合成一个身份等于拆掉那道闸
  8. 目录取**全量并集**、完成标记 `dev_policy_targets` 是列表不是布尔
     （策略正文全量重写：按单传 = 第二张单静默抹掉第一张；布尔标记 = 新地域永远补不上）
  9. 给不成不把整张单判失败，但要说出来 + 留台账事件

离线，云接口全部替换，数据虚构。
"""

from __future__ import annotations

import fnmatch
import json
import os
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest import mock

from delivery import catalog as catalog_mod
from delivery import devdir
from delivery import notify as notify_mod
from delivery import tickets as t
from delivery.clouds import aliyun
from delivery.provision import AliyunExecutor, ProvisionError

from .test_delivery_access_requests import ACC, NEW, TEMPLATES, Harness, MemberExecutor

#: 现网的两个开发桶（`identity/workspaces.json`）。**两个地域是这一块的起点**：
#: 只授杭州的表现是「同一个人、同一把 AK，换个地域就写不进去」
HZ = {
    "id": "640957",
    "region": "cn-hangzhou",
    "roles": ["PAI.AlgoDeveloper"],
    "mount": "cpfs-hz.cn-hangzhou.cpfs.aliyuncs.com",
    "bucket": "wuji-algo-dev-hz",
    "bucket_region": "cn-hangzhou",
    "bucket_prefix": "general",
}
SING = {
    "id": "284761",
    "region": "ap-southeast-1",
    "roles": ["PAI.AlgoDeveloper"],
    "mount": "cpfs-sg.ap-southeast-1.cpfs.aliyuncs.com",
    "bucket": "wuji-algo-dev-sing",
    "bucket_region": "ap-southeast-1",
    "bucket_prefix": "general",
}
#: 纯 CPFS 的空间：没有桶，就没有开发目录
CPFS_ONLY = {
    "id": "999",
    "region": "cn-hangzhou",
    "roles": ["PAI.AlgoDeveloper"],
    "mount": "cpfs-hz.cn-hangzhou.cpfs.aliyuncs.com",
}

_PATCHES = []


def setUpModule():
    # 和 test_delivery_access_requests 同一个理由：跑完整申请流程要有取件地址
    patch = mock.patch.dict(os.environ, {notify_mod.ENV_BASE_URL: "https://panel.example.com"})
    patch.start()
    _PATCHES.append(patch)


def tearDownModule():
    _PATCHES.pop().stop()


def actions_of(doc: dict, effect: str = "Allow") -> set:
    """**默认只看 Allow。** 不分 effect 的话，末尾那条 Deny 里的 `oss:PutObjectAcl`
    会被当成"授予了改 ACL"，而它恰恰是来封这条路的。"""
    out = set()
    for stmt in doc["Statement"]:
        if stmt["Effect"] != effect:
            continue
        act = stmt["Action"]
        out.update(act if isinstance(act, list) else [act])
    return out


def statements_with(doc: dict, action: str) -> list:
    return [s for s in doc["Statement"] if action in s["Action"]]


def resources_of(doc: dict) -> list:
    out = []
    for stmt in doc["Statement"]:
        res = stmt["Resource"]
        out.extend(res if isinstance(res, list) else [res])
    return out


def object_part(arn: str) -> str:
    """`acs:oss:*:*:bucket/general/li/*` → `bucket/general/li/*`。

    前半段那两个 `*` 是地域和账号占位，和「路径里有没有通配」是两回事 ——
    不切开的话第 2 条断言永远为真。
    """
    return arn.split(":", 4)[4]


class PolicyNameTests(unittest.TestCase):
    def test_two_logins_that_wash_to_the_same_string_get_different_policies(self):
        """**哈希后缀不是装饰。** 洗名字要把点号换成短划线，`huang.zenan` 和
        `huang-zenan` 于是撞成同一个名字 —— 后建的那条要么 `EntityAlreadyExists`，
        要么两个人共用一条策略、各自的目录都授给了对方。"""
        a, b = devdir.policy_name("huang.zenan"), devdir.policy_name("huang-zenan")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("wuji-dev-dir-huang-zenan-"))
        self.assertTrue(b.startswith("wuji-dev-dir-huang-zenan-"))

    def test_the_same_login_always_gets_the_same_name(self):
        """「这个人的策略叫什么」随时能重算，所以台账里不用记。
        不确定的话（比如混进随机数），重试一张单就会再建一条新策略。"""
        self.assertEqual(devdir.policy_name("lisi"), devdir.policy_name("lisi"))

    def test_the_name_is_something_ram_will_actually_accept(self):
        """阿里云策略名只收字母数字和短划线 —— 真人登录名里的点号直接拿去建会被拒。"""
        for login in ("huang.zenan", "Li.Si", "wang_zi_han", "李四".encode().hex()):
            with self.subTest(login=login):
                name = devdir.policy_name(login)
                self.assertRegex(name, r"\A[a-z0-9-]{1,128}\Z")
                self.assertTrue(name.startswith(devdir.POLICY_PREFIX))

    def test_a_very_long_login_does_not_blow_the_length_limit(self):
        name = devdir.policy_name("a" * 200 if False else "a" * 64)
        self.assertLessEqual(len(name), 128)

    def test_the_prefix_is_not_the_credential_one(self):
        """混用 `staff-oss-auto-` 的话，临时凭证的到期清理按前缀扫，会把开发目录策略
        当成过期凭证删掉 —— 表现是「人用着用着突然写不进去了」，而且没人会说是清理干的。"""
        from delivery import grants

        self.assertFalse(devdir.POLICY_PREFIX.startswith(grants.POLICY_PREFIX))
        self.assertFalse(devdir.POLICY_PREFIX.startswith(grants.EXTERNAL_POLICY_PREFIX))

    def test_the_policy_is_not_mistaken_for_a_high_risk_grant(self):
        """给 50 个人各挂一条直授策略这件事，不该在下一次刷新里变成 50 条「新增高危权限」。
        高危只认「能自我提权或等价于超管」的那三个词 —— 这条一个都不沾。"""
        from delivery import inventory

        self.assertFalse(inventory.is_high_risk(devdir.policy_name("lisi")))

    def test_a_login_that_is_not_a_path_segment_is_refused(self):
        """拼错的结果是一条永远匹配不上的 ARN，而 OSS 只回 403 ——
        看不出是策略拼错了，排查会往权限以外的方向跑。"""
        for bad in ("", "li si", "li/si", "..", ".", "../etc", "li;rm", None):
            with self.subTest(login=bad), self.assertRaises(ValueError):
                devdir.policy_name(bad)


class TargetsTests(unittest.TestCase):
    def test_every_region_is_a_target(self):
        """**一条策略要覆盖他的每一个目录。** 只授杭州的表现是「在新加坡的 DSW 里
        写不进去」，而那是最不好查的一种：同一个人、同一把 AK，换个地域就不行。"""
        self.assertEqual(
            devdir.targets_of([HZ, SING], "lisi"),
            [("wuji-algo-dev-hz", "general"), ("wuji-algo-dev-sing", "general")],
        )

    def test_a_cpfs_only_workspace_is_skipped_not_guessed(self):
        """没有桶就没有开发目录。瞎编一个桶名只会生成一条匹配不上任何东西的 ARN。"""
        self.assertEqual(
            devdir.targets_of([CPFS_ONLY, HZ], "lisi"), [("wuji-algo-dev-hz", "general")]
        )
        self.assertEqual(devdir.targets_of([CPFS_ONLY], "lisi"), [])
        self.assertEqual(devdir.targets_of([], "lisi"), [])
        self.assertEqual(devdir.targets_of(None, "lisi"), [])

    def test_the_same_bucket_twice_is_one_target(self):
        """两个地域填同一个桶是合法配置（登记表没禁）。同一个 ARN 写两遍不报错，
        但策略正文白白变长，离 6144 上限更近。"""
        self.assertEqual(
            devdir.targets_of([HZ, dict(HZ, id="x")], "lisi"), [("wuji-algo-dev-hz", "general")]
        )

    def test_a_bad_login_is_refused_here_too(self):
        """**不能静默跳过。** 跳过的话这个人一条策略都没有，而单子显示一切正常。"""
        for bad in ("", "li si", "li/si", ".."):
            with self.subTest(login=bad), self.assertRaises(ValueError):
                devdir.targets_of([HZ], bad)

    def test_outer_slashes_on_the_group_are_trimmed(self):
        """登记表允许写成 `/general/`，拼进 ARN 前要归一 —— 否则出来的是
        `bucket//general//li/*`，一个永远匹配不上的前缀。"""
        self.assertEqual(
            devdir.targets_of([dict(HZ, bucket_prefix="/general/")], "lisi"),
            [("wuji-algo-dev-hz", "general")],
        )


class PolicyDocumentTests(unittest.TestCase):
    def doc(self, targets=(("wuji-algo-dev-hz", "general"),), who="lisi"):
        return devdir.build_policy(list(targets), who)

    # ── 1. 一条策略，所有地域 ─────────────────────────────────────────────

    def test_one_policy_covers_every_region(self):
        doc = self.doc((("wuji-algo-dev-hz", "general"), ("wuji-algo-dev-sing", "general")))
        text = json.dumps(doc)
        for bucket in ("wuji-algo-dev-hz", "wuji-algo-dev-sing"):
            with self.subTest(bucket=bucket):
                self.assertIn(f"{bucket}/general/lisi/*", text)

    # ── 2. 中间不许有通配 ─────────────────────────────────────────────────

    def test_no_wildcard_in_the_middle_of_a_resource(self):
        """阿里云 ARN 的 `*` **跨 `/` 匹配**：`bucket/*/lisi/*` 不是「任意一级目录下的他」，
        而是「任意深度下任何叫 lisi 的目录」—— 比预期宽得多，而且宽在别人的桶路径里。"""
        for arn in resources_of(self.doc()):
            tail = object_part(arn)
            with self.subTest(resource=arn):
                self.assertLessEqual(tail.count("*"), 1, "路径里最多一个通配")
                if "*" in tail:
                    self.assertTrue(tail.endswith("*"), "通配只能在末尾")

    def test_the_policy_variable_trap_is_not_reintroduced(self):
        """`${ram:UserName}` 写进 Resource **语法合法、保存成功**，然后被当成字面的
        十六个字符去匹配 object key —— 策略挂上了、面板报成功、人还是写不进去。
        阿里云 RAM 根本没有策略变量这个功能，这条路是死的。"""
        text = json.dumps(self.doc())
        self.assertNotIn("${", text)

    def test_one_persons_policy_does_not_reach_another_persons_directory(self):
        mine = json.dumps(self.doc(who="lisi"))
        self.assertNotIn("/wangwu/", mine)
        # 用 ARN 的匹配规则真跑一遍：别人的目录不能被我的任何一条 Resource 命中
        for arn in resources_of(self.doc(who="lisi")):
            with self.subTest(resource=arn):
                self.assertFalse(
                    fnmatch.fnmatch("wuji-algo-dev-hz/general/wangwu/x.bin", object_part(arn))
                )

    # ── 3. 桶级动作单独一条、不带 Condition ───────────────────────────────

    def test_bucket_actions_sit_alone_without_any_condition(self):
        """**线上「拿了凭证访问不了桶」就是这个**：桶级请求不带 prefix 参数，
        混进带 `oss:Prefix` 的语句会被服务端判成不满足条件（余湘港那单）。"""
        doc = self.doc()
        bucket_stmt = statements_with(doc, "oss:GetBucketInfo")
        self.assertEqual(len(bucket_stmt), 1)
        self.assertEqual(bucket_stmt[0]["Resource"], "acs:oss:*:*:wuji-algo-dev-hz")
        self.assertNotIn("Condition", bucket_stmt[0], "桶级请求不带 prefix，叠条件即 403")
        self.assertNotIn("oss:ListObjects", bucket_stmt[0]["Action"], "列清单要单独一条")

    def test_listing_is_a_separate_statement_scoped_to_his_own_prefix(self):
        """和桶信息拆开，**而且叠前缀条件**。

        「组里本来就有 `AliyunOSSReadOnlyAccess`、收窄不多一分安全」这个理由
        **只在今天成立**：这是一条挂在真人身上、没有时间窗、没有回收路径的长期策略，
        哪天算法组那条只读被收窄了，这几十条策略会静默保留整桶列清单权限，
        而不会有任何人想起来它们还在。
        """
        stmt = statements_with(self.doc(), "oss:ListObjects")
        self.assertEqual(len(stmt), 1)
        self.assertEqual(stmt[0]["Resource"], "acs:oss:*:*:wuji-algo-dev-hz", "列清单是桶级 ARN")
        self.assertEqual(
            stmt[0]["Condition"],
            {"StringLike": {"oss:Prefix": ["general/lisi/", "general/lisi/*"]}},
        )
        self.assertNotIn("oss:GetBucketInfo", stmt[0]["Action"])

    def test_the_four_bucket_actions_are_all_there(self):
        """GetBucketLocation 少了的话，S3 兼容客户端（lakeFS / s3fs / rclone）建连时
        探地域探不到，报一个和权限八竿子打不着的错。"""
        got = actions_of(self.doc())
        for act in (
            "oss:GetBucketInfo",
            "oss:GetBucketStat",
            "oss:GetBucketAcl",
            "oss:GetBucketLocation",
        ):
            with self.subTest(action=act):
                self.assertIn(act, got)

    # ── 4. 动作集合不能少 ─────────────────────────────────────────────────

    def test_read_write_and_delete_are_all_granted(self):
        """**删必须给**：连自己写错的文件都删不掉的目录实际上不可用，
        人会绕到别处去写，最后开发数据散在谁也不知道的地方。
        这是和 `grants.py`（外部凭证，Deny 掉删除）刻意的差异，不是漏抄。"""
        got = actions_of(self.doc())
        for act in (
            "oss:GetObject",
            "oss:GetObjectVersion",  # 桶开了版本控制时带 version id 的请求走它
            "oss:PutObject",
            "oss:AbortMultipartUpload",
            "oss:ListParts",
            "oss:DeleteObject",
            "oss:DeleteObjectVersion",
            "oss:ListObjects",
        ):
            with self.subTest(action=act):
                self.assertIn(act, got)

    def test_the_write_actions_only_apply_to_his_own_prefix(self):
        doc = self.doc()
        write = [s for s in doc["Statement"] if "oss:PutObject" in s["Action"]]
        self.assertEqual(len(write), 1)
        self.assertEqual(
            sorted(write[0]["Resource"]),
            sorted(
                [
                    "acs:oss:*:*:wuji-algo-dev-hz/general/lisi/*",
                    "acs:oss:*:*:wuji-algo-dev-hz/general/lisi/",
                ]
            ),
        )

    def test_the_placeholder_object_itself_is_covered(self):
        """`<前缀>/*` 能不能匹配空串是没写明的事。少了 `<前缀>/` 那条的表现是
        「目录里的文件都能删、唯独那个 0 字节占位删不掉」—— 没人能想明白为什么。"""
        self.assertIn("acs:oss:*:*:wuji-algo-dev-hz/general/lisi/", resources_of(self.doc()))

    # ── 5. 不许有的动作 ───────────────────────────────────────────────────

    def test_it_cannot_make_anything_public(self):
        """`oss:PutObjectAcl` 能把对象改成公共读 —— 一条命令就能把内部数据挂到公网上，
        而开发目录用不到它。"""
        got = actions_of(self.doc())
        for act in (
            "oss:PutObjectAcl",
            "oss:PutBucketAcl",
            "oss:DeleteBucket",
            "oss:PutBucketPolicy",
        ):
            with self.subTest(action=act):
                self.assertNotIn(act, got)

    def test_not_granting_the_acl_action_is_not_enough(self):
        """**光不授予封不住这条路**：OSS 的 `PutObject` 本身收 `x-oss-object-acl`
        请求头，服务端按 `oss:PutObject` 判 —— 「上传的同时把对象设成公共读」
        走的就是这个动作，而那个动作他必须有。只有显式 Deny 才封得住
        （Deny 在阿里云里压倒一切 Allow）。"""
        deny = [s for s in self.doc()["Statement"] if s["Effect"] == "Deny"]
        self.assertEqual(len(deny), 1, "Deny 只该有这一条")
        self.assertEqual(sorted(deny[0]["Action"]), ["oss:PutBucketAcl", "oss:PutObjectAcl"])

    def test_the_deny_does_not_reach_past_his_own_directory(self):
        """**Deny 跨策略压倒一切 Allow。** Resource 写宽一点（比如整桶的 `/*`）的话，
        这条只是想封 ACL 的语句会顺手把别人给他的、甚至临时凭证里的同名动作也挡掉 ——
        而排查时没人会想到一条「开发目录」策略。"""
        deny = [s for s in self.doc()["Statement"] if s["Effect"] == "Deny"][0]
        self.assertEqual(
            sorted(deny["Resource"]),
            sorted(
                [
                    "acs:oss:*:*:wuji-algo-dev-hz",
                    "acs:oss:*:*:wuji-algo-dev-hz/general/lisi/*",
                    "acs:oss:*:*:wuji-algo-dev-hz/general/lisi/",
                ]
            ),
        )

    def test_the_deny_does_not_take_back_what_was_just_granted(self):
        """封 ACL 不能连上传本身一起封掉 —— 那就成了「策略挂上了、还是写不进去」，
        和这个功能要解决的问题一模一样。"""
        denied = actions_of(self.doc(), effect="Deny")
        for act in ("oss:PutObject", "oss:GetObject", "oss:DeleteObject", "oss:ListObjects"):
            with self.subTest(action=act):
                self.assertNotIn(act, denied)

    def test_nothing_is_granted_with_a_star_action(self):
        for act in actions_of(self.doc()) | actions_of(self.doc(), effect="Deny"):
            with self.subTest(action=act):
                self.assertNotIn("*", act)

    # ── 边界 ──────────────────────────────────────────────────────────────

    def test_no_target_is_an_error_not_an_empty_policy(self):
        """空策略挂上去 = 面板报成功、人还是写不进去。"""
        with self.assertRaises(ValueError):
            devdir.build_policy([], "lisi")

    def test_a_bad_bucket_name_is_refused(self):
        for bad in ("Wuji-Algo", "a", "bucket/with/slash", "", "x" * 64, "-lead"):
            with self.subTest(bucket=bad), self.assertRaises(ValueError):
                devdir.build_policy([(bad, "general")], "lisi")

    def test_a_group_dir_that_is_not_one_segment_is_refused(self):
        """登记表只做了「去掉首尾斜杠」，`a/b` 这种能过它 —— 到这里必须拦下，
        否则拼出来的是一条谁也匹配不上的 ARN（而 OSS 只回 403）。"""
        for bad in ("a/b", "深度 目录", ".."):
            with self.subTest(group=bad), self.assertRaises(ValueError):
                devdir.build_policy([("wuji-algo-dev-hz", bad)], "lisi")

    def test_a_group_less_bucket_puts_him_at_the_root(self):
        doc = devdir.build_policy([("wuji-algo-dev-hz", "")], "lisi")
        self.assertIn("acs:oss:*:*:wuji-algo-dev-hz/lisi/*", resources_of(doc))

    def test_document_is_json_and_refuses_to_exceed_the_cloud_limit(self):
        """撞上 6144 时要的是合并语句，不是砍动作 —— 砍动作就是又一次
        「余湘港那样的缺失」，而且是静默的。"""
        one = devdir.document([("wuji-algo-dev-hz", "general")], "lisi")
        self.assertEqual(
            json.loads(one), devdir.build_policy([("wuji-algo-dev-hz", "general")], "lisi")
        )
        many = [(f"wuji-algo-dev-{i:02d}", "general") for i in range(40)]
        with self.assertRaises(ValueError) as ctx:
            devdir.document(many, "lisi")
        self.assertIn("6144", str(ctx.exception))

    def test_the_human_readable_dir_matches_what_the_policy_grants(self):
        """给人看的那行路径和策略里的前缀**必须是同一个串**。漂移的表现是
        「面板告诉我目录在这儿，可我往那儿写还是 403」。"""
        shown = devdir.dev_dir("wuji-algo-dev-hz", "general", "lisi")
        self.assertEqual(shown, "wuji-algo-dev-hz/general/lisi/")
        self.assertIn(f"acs:oss:*:*:{shown}", resources_of(self.doc()))
        self.assertEqual(devdir.dev_dir("b1234", "", "lisi"), "b1234/lisi/")


class ReadBackTests(unittest.TestCase):
    """`targets_in` / `merge_targets`：把云上那篇读回来认出它授了哪些目录。

    这一对是「读回合并」的地基。认错了的后果不是少给权限，是**把别人（别的单）
    给的地域悄悄删掉** —— `CreatePolicyVersion` 是整篇替换。
    """

    TWO = [("wuji-algo-dev-hz", "general"), ("wuji-algo-dev-sing", "general")]

    def test_it_reads_back_exactly_what_build_policy_wrote(self):
        """**正则和拼 ARN 的那几行是一对。** 这条是它们唯一的连接点：
        谁动了其中一边而没动另一边，这里当场红 —— 而线上的表现会是
        「读回来一条都认不出」，然后（幸好）被 fail-closed 拦成一次开通失败。
        """
        for targets in ([("wuji-algo-dev-hz", "general")], self.TWO, [("b1234", "")]):
            with self.subTest(targets=targets):
                doc = devdir.build_policy(targets, "lisi")
                self.assertEqual(devdir.targets_in(doc, "lisi"), targets)

    def test_another_persons_directory_is_not_claimed_as_mine(self):
        """认成自己的话，合并之后那条 ARN 会被写进**我这条**策略 ——
        等于把别人的目录授给我，而且是面板自己动手加的。"""
        doc = devdir.build_policy(self.TWO, "wangwu")
        self.assertEqual(devdir.targets_in(doc, "lisi"), [])

    def test_a_similar_looking_name_is_not_a_match(self):
        """`lisi2` 的目录不是 `lisi` 的。按 `endswith(登录名)` 判就会混。"""
        doc = devdir.build_policy([("wuji-algo-dev-hz", "general")], "lisi2")
        self.assertEqual(devdir.targets_in(doc, "lisi"), [])
        doc = devdir.build_policy([("wuji-algo-dev-hz", "xlisi")], "lisi")
        self.assertEqual(devdir.targets_in(doc, "lisi"), [("wuji-algo-dev-hz", "xlisi")])

    def test_garbage_reads_back_as_nothing_instead_of_raising(self):
        """认不出要返回空、让**调用方**去 fail-closed —— 在这儿抛的话，
        「云上那篇不是我们写的」和「正则写错了」会长成同一个异常。"""
        for junk in (
            None,
            {},
            {"Statement": None},
            {"Statement": [{"Resource": None}]},
            {"Statement": [{"Resource": "acs:ram::1:policy/x"}]},
        ):
            with self.subTest(doc=junk):
                self.assertEqual(devdir.targets_in(junk, "lisi"), [])

    def test_merge_keeps_the_old_ones_first_and_deduplicates(self):
        """老的排前面 = 「已经能写的地方继续能写」。去重是因为同一个 ARN 写两遍
        白白吃掉 6144 的预算。"""
        old = [("wuji-algo-dev-hz", "general")]
        new = [("wuji-algo-dev-sing", "general"), ("wuji-algo-dev-hz", "general")]
        self.assertEqual(devdir.merge_targets(old, new), self.TWO)
        self.assertEqual(devdir.merge_targets(old, old), old)
        self.assertEqual(devdir.merge_targets([], new), list(reversed(self.TWO)))
        self.assertEqual(devdir.merge_targets(old, []), old)

    def test_merge_normalises_the_same_way_targets_of_does(self):
        """合并出来的东西要能和 `targets_of` 的产物比相等 —— 不然
        「没有新增就不写」那条判据永远不成立，每次开通都白写一个版本。"""
        self.assertEqual(
            devdir.merge_targets([("wuji-algo-dev-hz", "/general/")], []),
            [("wuji-algo-dev-hz", "general")],
        )


class EnsurePolicyTests(unittest.TestCase):
    """`AliyunExecutor.ensure_dev_policy`：建/更新那条策略。**发的请求本身**是断言对象。"""

    HZ = ("wuji-algo-dev-hz", "general")
    SING = ("wuji-algo-dev-sing", "general")
    TARGETS = [HZ, SING]

    def executor(self, responses=None, *, on_cloud=None, who="lisi"):
        """`on_cloud` 给了就表示这条策略云上已经有了，正文按那几个目录现造。"""
        calls = []
        responses = dict(responses or {})
        if on_cloud is not None:
            responses.setdefault(
                "CreatePolicy", (409, {"Code": "EntityAlreadyExists.Policy", "Message": "x"})
            )
            responses.setdefault("GetPolicy", (200, {"Policy": {"DefaultVersion": "v2"}}))
            responses.setdefault(
                "GetPolicyVersion",
                (
                    200,
                    {
                        "PolicyVersion": {
                            "PolicyDocument": json.dumps(devdir.build_policy(on_cloud, who))
                        }
                    },
                ),
            )

        def send(url):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            calls.append(query)
            return responses.get(query["Action"], (200, {}))

        responses.setdefault("GetCallerIdentity", (200, {"AccountId": ACC}))
        return AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=send), calls

    @staticmethod
    def sent(calls, action):
        return [c for c in calls if c["Action"] == action]

    def test_a_new_policy_is_created_with_the_computed_name_and_document(self):
        ex, calls = self.executor()
        name, covered = ex.ensure_dev_policy("lisi", self.TARGETS)
        self.assertEqual(name, devdir.policy_name("lisi"))
        self.assertEqual(covered, self.TARGETS)
        made = self.sent(calls, "CreatePolicy")[0]
        self.assertEqual(made["PolicyName"], name)
        self.assertEqual(
            json.loads(made["PolicyDocument"]), devdir.build_policy(self.TARGETS, "lisi")
        )
        self.assertEqual(self.sent(calls, "GetPolicy"), [], "新建的没必要读回来")

    def test_it_takes_a_login_not_a_policy_name(self):
        """同 `rewrite_policy` 的理由：云上对发放身份放行的建策略前缀不止一个，
        收策略名的话传错一个就能改掉机器人管着的外部凭证策略，云不会拦。"""
        import inspect

        params = list(inspect.signature(AliyunExecutor.ensure_dev_policy).parameters)
        self.assertEqual(params, ["self", "username", "targets"])

    def test_an_existing_policy_is_read_back_and_merged_not_overwritten(self):
        """**这就是审计阻塞 2 的根**：`CreatePolicyVersion` 是整篇替换，而每张单
        只批了它自己那几个地域。不先读回来合并，第二张单（「加入工作空间·新加坡」）
        就把第一张给的杭州写权限抹掉了 —— 云上不报错、卡片显示成功、人从此
        写不进杭州，而且补不回来。"""
        ex, calls = self.executor(on_cloud=[self.HZ])
        name, covered = ex.ensure_dev_policy("lisi", [self.SING])
        self.assertEqual(covered, [self.HZ, self.SING], "老的在前，新的追加")
        ver = self.sent(calls, "CreatePolicyVersion")[0]
        self.assertEqual(ver["PolicyName"], name)
        self.assertEqual(ver["SetAsDefault"], "true")
        self.assertEqual(
            json.loads(ver["PolicyDocument"]), devdir.build_policy([self.HZ, self.SING], "lisi")
        )
        # 读的是**当前生效的那一版**，不是 v1：拿错版本合出来的正文会少掉最近一次改动
        self.assertEqual(self.sent(calls, "GetPolicyVersion")[0]["VersionId"], "v2")

    def test_nothing_new_means_nothing_written(self):
        """**不是为了省一次调用**：一条策略最多 5 个版本且不可调，每次开通都盲写一版的话，
        几张单之后就开始轮换删旧版本 —— 而旧版本是改坏了唯一的退路。
        （`retry_dev_policies` 一分钟一轮，盲写的话这事会很快。）"""
        ex, calls = self.executor(on_cloud=self.TARGETS)
        name, covered = ex.ensure_dev_policy("lisi", [self.HZ])
        self.assertEqual(name, devdir.policy_name("lisi"))
        self.assertEqual(covered, self.TARGETS)
        self.assertEqual(self.sent(calls, "CreatePolicyVersion"), [])

    def test_the_version_limit_is_handled_by_the_server(self):
        """真要写的时候，5 个版本的轮换交给服务端 —— 不给轮转策略的话，
        第六次改动直接报 `LimitExceeded`，而那时人正等着一个新地域的写权限。"""
        ex, calls = self.executor(on_cloud=[self.HZ])
        ex.ensure_dev_policy("lisi", [self.SING])
        ver = self.sent(calls, "CreatePolicyVersion")[0]
        self.assertEqual(ver["RotateStrategy"], "DeleteOldestNonDefaultVersionWhenLimitExceeded")

    # ── fail-closed：读不明白就别动 ───────────────────────────────────────

    def test_an_unrecognisable_document_is_refused_instead_of_overwritten(self):
        """一条目录都认不出，说明云上那篇**不是我们写的**、或者 ARN 格式变了。
        这时候覆盖它等于把不认识的东西删掉 —— 而那东西可能是某人手工配的权限。
        宁可这张单报「开发目录授权失败」让人来看。"""
        ex, calls = self.executor(on_cloud=[self.HZ], who="wangwu")  # 别人的目录
        with self.assertRaises(ProvisionError) as ctx:
            ex.ensure_dev_policy("lisi", [self.SING])
        self.assertIn("认不出", str(ctx.exception))
        self.assertEqual(self.sent(calls, "CreatePolicyVersion"), [], "认不出就一个写请求都不发")

    def test_a_document_that_is_not_json_is_refused(self):
        ex, calls = self.executor(
            {
                "CreatePolicy": (409, {"Code": "EntityAlreadyExists.Policy", "Message": "x"}),
                "GetPolicy": (200, {"Policy": {"DefaultVersion": "v2"}}),
                "GetPolicyVersion": (200, {"PolicyVersion": {"PolicyDocument": "不是 JSON"}}),
            }
        )
        with self.assertRaises(ProvisionError):
            ex.ensure_dev_policy("lisi", [self.SING])
        self.assertEqual(self.sent(calls, "CreatePolicyVersion"), [])

    def test_a_missing_default_version_is_refused(self):
        """`GetPolicy` 没给默认版本时，**不能退回去读 v1 或者直接覆盖** ——
        两种都会拿一篇不是当前生效的正文去合并。"""
        ex, calls = self.executor(
            {
                "CreatePolicy": (409, {"Code": "EntityAlreadyExists.Policy", "Message": "x"}),
                "GetPolicy": (200, {"Policy": {}}),
            }
        )
        with self.assertRaises(ProvisionError):
            ex.ensure_dev_policy("lisi", [self.SING])
        self.assertEqual(self.sent(calls, "GetPolicyVersion"), [])
        self.assertEqual(self.sent(calls, "CreatePolicyVersion"), [])

    def test_a_failure_to_read_back_does_not_fall_through_to_overwriting(self):
        """读回来这一步本身 403 / 超时的时候也一样：不许「读不到就当没有」。"""
        ex, calls = self.executor(
            {
                "CreatePolicy": (409, {"Code": "EntityAlreadyExists.Policy", "Message": "x"}),
                "GetPolicy": (403, {"Code": "NoPermission", "Message": "x"}),
            }
        )
        with self.assertRaises(aliyun.AliyunError):
            ex.ensure_dev_policy("lisi", [self.SING])
        self.assertEqual(self.sent(calls, "CreatePolicyVersion"), [])

    def test_any_other_cloud_error_surfaces(self):
        """全吞掉的话，一条没建出来的策略会被当成建好了 —— 然后挂载一个不存在的策略名，
        或者更糟：挂成功了（同名别人的），授出去的是别人的目录。"""
        ex, calls = self.executor({"CreatePolicy": (403, {"Code": "NoPermission", "Message": "x"})})
        with self.assertRaises(aliyun.AliyunError):
            ex.ensure_dev_policy("lisi", self.TARGETS)
        self.assertEqual(self.sent(calls, "CreatePolicyVersion"), [])

    def test_a_bad_login_never_reaches_the_cloud(self):
        ex, calls = self.executor()
        with self.assertRaises(ValueError):
            ex.ensure_dev_policy("li/si", self.TARGETS)
        self.assertEqual(self.sent(calls, "CreatePolicy"), [])


class DevDirExecutor(MemberExecutor):
    """记下「谁被要求建策略、谁被要求挂策略」——**这两件事必须落在不同的身份上**。

    `ensure_dev_policy` 这里**照真实实现的契约做合并**（它就是站在云那一侧的替身）：
    `built` 记的是这一次**被要求**授的目录（该只有这张单批的那几个），
    `covered` 记的是合并之后云上一共覆盖的范围。两者分开记，才验得出
    「flows 只传本单的、不抹掉别的单的」这件事。
    """

    def __init__(self):
        super().__init__()
        self.built = []  # ensure_dev_policy 收到的 (登录名, 目录)
        self.attached = []  # attach_policy 收到的 (登录名, 类型, 策略名)
        self.on_cloud = {}  # 登录名 → 云上这条策略现在覆盖的目录
        self.build_fail = None
        self.attach_fail = None

    def ensure_dev_policy(self, username, targets):
        if self.build_fail:
            raise self.build_fail
        want = [tuple(x) for x in targets]
        self.built.append((username, want))
        merged = devdir.merge_targets(self.on_cloud.get(username) or [], want)
        self.on_cloud[username] = merged
        return devdir.policy_name(username), merged

    def attach_policy(self, user, policy_type, policy):
        if self.attach_fail:
            raise self.attach_fail
        self.attached.append((user, policy_type, policy))


class DevDirHarness:
    """开通路径和重试路径共用的夹具（两组用例各自继承它 + `TestCase`）。"""

    def harness(self, *spaces, template="new-user", **extra):
        """`spaces` 配给 `template`，`extra` 形如 `{"oss-read": (SING,)}` 配给别的模板。

        分开给是因为「哪张单带哪个地域」正是审计那条阻塞项的现场：
        开账号单只带杭州，新加坡在另一张单上。
        """
        h = Harness(TEMPLATES)
        h.executor = DevDirExecutor()
        # 发放身份是**另一个**对象：两件事落在谁身上才看得出来
        h.issuer = DevDirExecutor()
        by_id = {template: tuple(spaces)} if spaces else {}
        by_id.update({k: tuple(v) for k, v in (extra or {}).items()})
        if by_id:
            self.set_spaces(h, by_id)
        return h

    @staticmethod
    def set_spaces(h, by_id: dict):
        rows = [
            replace(tpl, workspaces=tuple(dict(w) for w in by_id[tpl.id]))
            if tpl.id in by_id
            else tpl
            for tpl in h.flows._catalog().templates
        ]
        patched = catalog_mod.Catalog(templates=tuple(rows))
        h.flows._catalog = lambda: patched

    def account(self, h, username="xinren"):
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": username})
        h.approve(ticket)
        return h.flows.sync(ticket["id"], force=True)

    @staticmethod
    def _boom(*_a, **_k):
        raise RuntimeError("名册写不进去")

    def failed_account(self, h, username="xinren"):
        """跑一张「工作空间之后那一步崩了」的单 → 整单 FAILED，可以被重试。

        这是唯一能观察到「重跑一张已经配好工作空间的单」的真实路径：
        成功的单进 DONE，而 `execute` 只收 APPROVED / FAILED。
        """
        h.flows._add_manual_link = self._boom
        done = self.account(h, username)
        assert done["status"] == t.FAILED, done["status"]
        h.flows._add_manual_link = lambda *a, **k: None
        return done


class FlowTests(DevDirHarness, unittest.TestCase):
    """建号流程里的那一步。"""

    # ── 给到位了没有 ──────────────────────────────────────────────────────

    def test_a_new_account_can_write_its_own_dev_dir(self):
        h = self.harness(HZ)
        done = self.account(h)
        self.assertEqual(h.issuer.built, [("xinren", [("wuji-algo-dev-hz", "general")])])
        self.assertEqual(h.executor.attached, [("xinren", "Custom", devdir.policy_name("xinren"))])
        self.assertEqual(done.get("dev_policy_targets"), ["wuji-algo-dev-hz/general"])
        self.assertIn("wuji-algo-dev-hz/general/xinren/", done["result"])

    def test_both_regions_are_one_policy_not_two(self):
        """**少一条的表现是「在新加坡写不进去」**，而那看着像是地域的问题。
        一条策略覆盖两个目录，所以云上只该有一次建策略、一次挂载。"""
        h = self.harness(HZ, SING)
        done = self.account(h)
        self.assertEqual(len(h.issuer.built), 1, "一个人一条策略，不是一个目录一条")
        self.assertEqual(
            h.issuer.built[0][1],
            [("wuji-algo-dev-hz", "general"), ("wuji-algo-dev-sing", "general")],
        )
        self.assertEqual(len(h.executor.attached), 1)
        for bucket in ("wuji-algo-dev-hz", "wuji-algo-dev-sing"):
            self.assertIn(f"{bucket}/general/xinren/", done["result"])

    def test_building_and_attaching_land_on_different_identities(self):
        """云上是这么切的，而且是有意的：发放身份够不着真人的子账号，
        开通身份造不了策略正文 —— 单独哪个都发不出「内容任意 + 挂在真人身上」的策略。
        图省事合成一个身份等于把这道闸拆了，而合并之后所有别的用例照样绿。"""
        h = self.harness(HZ)
        self.account(h)
        self.assertTrue(h.issuer.built, "建策略要走发放身份")
        self.assertEqual(h.executor.built, [], "开通身份不该被要求造策略正文")
        self.assertTrue(h.executor.attached, "挂策略要走开通身份")
        self.assertEqual(h.issuer.attached, [], "发放身份够不着真人的子账号")

    def test_the_granted_prefix_is_the_directory_that_was_actually_created(self):
        """两处各算一份路径的话，漂移的表现是「面板说目录在这儿，往那儿写还是 403」。"""
        h = self.harness(HZ)
        done = self.account(h)
        made = [a for a in h.executor.actions if a[0] == "dir"]
        self.assertEqual(made, [("dir", "wuji-algo-dev-hz", "general/xinren", "cn-hangzhou")])
        self.assertIn(f"{made[0][1]}/{made[0][2]}/", done["result"])

    # ── 全量重写带来的坑（审计阻塞 2） ────────────────────────────────────

    def test_a_second_ticket_does_not_wipe_what_the_first_one_granted(self):
        """**这条是这一轮最重要的回归。**

        `ensure_dev_policy` 是全量重写策略正文（新版本 + 设为默认），
        所以传进去少一个目录，云上就少一个 —— 不是「补上这一个」。
        现网开账号单只带杭州，新加坡要靠第二张「加入工作空间·新加坡」的单子拿：
        按单传的话第二张单一落地，第一张给的杭州写权限被**静默覆盖掉**，
        卡片还显示成功，人从此写不进杭州目录，而且哪儿都不报。
        """
        h = self.harness(HZ, **{"oss-read": (SING,)})
        first = self.account(h, username="lisi")
        self.assertEqual(h.issuer.built[-1], ("lisi", [("wuji-algo-dev-hz", "general")]))
        self.assertEqual(first.get("dev_policy_targets"), ["wuji-algo-dev-hz/general"])

        ticket = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        # 第二张单**只要**新加坡（它只批了这个）……
        self.assertEqual(h.issuer.built[-1], ("lisi", [("wuji-algo-dev-sing", "general")]))
        # ……而云上最终两个都在：合并发生在 `ensure_dev_policy` 里（读回默认版本再并）
        self.assertEqual(
            h.issuer.on_cloud["lisi"],
            [("wuji-algo-dev-hz", "general"), ("wuji-algo-dev-sing", "general")],
            "第二张单不能把杭州抹掉",
        )
        # 完成标记记的是**合并后的全量覆盖**，不是本单那几个 —— 拿它判「要不要重跑」才准
        self.assertEqual(
            done.get("dev_policy_targets"),
            ["wuji-algo-dev-hz/general", "wuji-algo-dev-sing/general"],
        )

    def test_only_the_regions_this_ticket_approved_are_requested(self):
        """**不许用「把所有地域一次授完」去绕过合并。**

        那种写法（曾经的实现）同样能让第二张单不抹掉第一张，代价是发出这张单
        **没批的范围** —— 而「加入工作空间·新加坡」是一道真实存在的审批门，
        绕过去等于面板自己给自己批了。所以这里断言的是「只请求本单批的地域」，
        不抹掉旧的那件事由 `ensure_dev_policy` 读回合并保证（上一条用例）。
        """
        h = self.harness(HZ, **{"oss-read": (SING,)})
        done = self.account(h)
        self.assertEqual(h.issuer.built, [("xinren", [("wuji-algo-dev-hz", "general")])])
        self.assertNotIn("wuji-algo-dev-sing", done["result"])

    def test_the_marker_records_which_dirs_were_covered_not_just_yes(self):
        """布尔标记表达不了「少覆盖了一个地域」：以后登记表里加一个地域，
        所有老单子都会因为标记已置位而跳过，新地域永远补不上、同样没人会发现。"""
        h = self.harness(HZ)
        first = self.failed_account(h)
        self.assertEqual(first.get("dev_policy_targets"), ["wuji-algo-dev-hz/general"])

        self.set_spaces(h, {"new-user": (HZ, SING)})  # 登记表加了一个地域
        again = h.flows.execute(first["id"], actor="admin")
        self.assertEqual(
            h.issuer.built[-1][1],
            [("wuji-algo-dev-hz", "general"), ("wuji-algo-dev-sing", "general")],
        )
        self.assertEqual(
            again.get("dev_policy_targets"),
            ["wuji-algo-dev-hz/general", "wuji-algo-dev-sing/general"],
        )

    def test_it_is_not_redone_when_everything_is_already_covered(self):
        """已经覆盖到了就别再写一遍：每次重跑都 `CreatePolicyVersion` 的话，
        5 个版本的额度几次重试就用光了。"""
        h = self.harness(HZ)
        first = self.failed_account(h)
        h.flows.execute(first["id"], actor="admin")
        self.assertEqual(len(h.issuer.built), 1)
        self.assertEqual(len(h.executor.attached), 1)

    def test_an_old_ticket_whose_workspace_is_already_done_still_gets_it(self):
        """**存量老单子**：空间那几件事上一轮就做完了（`workspace_done` 为真），
        开发目录权限是后加的一步、有自己的完成标记。不单独判的话，重跑会在
        工作空间那里直接返回，于是这一步对所有已经开好的人永远补不上。"""
        h = self.harness(HZ)
        h.issuer.build_fail = RuntimeError("这一步当时还没有")  # 相当于「那时还没这一步」
        first = self.failed_account(h)
        self.assertTrue(first.get("workspace_done"))
        self.assertIsNone(first.get("dev_policy_targets"))
        members = len([a for a in h.executor.actions if a[0] == "member"])

        h.issuer.build_fail = None
        again = h.flows.execute(first["id"], actor="admin")
        self.assertEqual(
            len([a for a in h.executor.actions if a[0] == "member"]),
            members,
            "工作空间那几步不该重做 —— 走的就是 workspace_done 那条早返回",
        )
        self.assertEqual(h.issuer.built, [("xinren", [("wuji-algo-dev-hz", "general")])])
        self.assertEqual(again.get("dev_policy_targets"), ["wuji-algo-dev-hz/general"])
        self.assertIn("wuji-algo-dev-hz/general/xinren/", again["result"])

    # ── 给不成的时候 ──────────────────────────────────────────────────────

    def test_a_failure_does_not_lose_the_account(self):
        """账号这时已经建好了。判整单失败只会让管理员以为什么都没发生、
        跑去手工再建一个。"""
        h = self.harness(HZ)
        h.issuer.build_fail = RuntimeError("云上拒了")
        done = self.account(h)
        self.assertEqual(done["status"], t.DONE)
        self.assertTrue(done["user_created"])
        self.assertIsNone(done.get("dev_policy_targets"))

    def test_a_failure_says_so_and_leaves_a_trace(self):
        """只写「已新建子账号 X」的话，人拿到 AK 写不进去，会以为 AK 坏了
        （这正是这个功能要消灭的那种误判）。"""
        h = self.harness(HZ)
        h.issuer.build_fail = RuntimeError("云上拒了")
        done = self.account(h)
        self.assertIn("开发目录授权失败", done["result"])
        self.assertIn("dev_policy_needed", [e.get("event") for e in done.get("events") or []])

    def test_an_attach_failure_is_reported_too(self):
        """策略建出来了、没挂上 = 权限一点没有，而云上多了一条谁也没用的策略。"""
        h = self.harness(HZ)
        h.executor.attach_fail = RuntimeError("挂不上")
        done = self.account(h)
        self.assertIsNone(done.get("dev_policy_targets"))
        self.assertIn("开发目录授权失败", done["result"])

    def test_a_dotted_login_is_not_collateral_damage(self):
        """点号是合法的 RAM 登录名（现网一半人都是 `名.姓`），只是不能直接当策略名。"""
        h = self.harness(HZ)
        done = self.account(h, username="li.si")
        self.assertEqual(done.get("dev_policy_targets"), ["wuji-algo-dev-hz/general"])
        self.assertEqual(h.issuer.built[0][0], "li.si")

    # ── 不该做事的时候 ────────────────────────────────────────────────────

    def test_a_cpfs_only_workspace_asks_for_nothing(self):
        h = self.harness(CPFS_ONLY)
        done = self.account(h)
        self.assertEqual(h.issuer.built, [])
        self.assertEqual(h.executor.attached, [])
        self.assertNotIn("开发目录", done["result"])

    def test_a_template_without_workspaces_behaves_exactly_as_before(self):
        h = self.harness()
        done = self.account(h)
        self.assertEqual(h.issuer.built, [])
        self.assertNotIn("开发目录", done["result"])

    def test_joining_another_workspace_also_grants_the_dev_dir(self):
        """存量的人是走这条路补上的（「加入别的工作空间」的权限单）——
        不走的话，这个功能只对新人生效，而写不进去的正是那 50 个老人。"""
        h = self.harness(**{"oss-read": (SING,)})
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(h.issuer.built, [("lisi", [("wuji-algo-dev-sing", "general")])])

    def test_a_non_aliyun_template_is_untouched(self):
        """火山没有这条路（桶在 TOS、动作名也不一样）。照着阿里那份发一条 `oss:*`
        的策略过去，云上要么拒、要么建出一条谁也看不懂的东西。"""
        h = self.harness(HZ)
        tpl = replace(h.flows._catalog().templates[0], platform="volcano")
        self.assertEqual(h.flows._provision_dev_policy(tpl, {"id": "t-1"}, "xinren"), "")
        self.assertEqual(h.issuer.built, [])


class RetryTests(DevDirHarness, unittest.TestCase):
    """`retry_dev_policies()`：定时任务把「号开好了、权限没给成」的单子补上。

    为什么必须有它：这一步失败**不判整单失败**（账号已经建好了），于是单子进 DONE ——
    而 `execute()` 只认 APPROVED / FAILED，**这张单再也重试不了**。没有这个循环的话，
    `dev_policy_needed` 那条事件就只是一行文案，没有任何东西会去做它，
    那个人只能一直写不进自己的目录、并以为是 AK 坏了。

    这一组的另一半是「**别碰不该碰的**」：它一分钟一轮，扫的是全量申请单。
    捞宽一格，就是每轮给几百个历史老单子凭空发策略。
    """

    def stuck(self, h, username="xinren"):
        """跑出一张「号建好了、开发目录权限没给成」的 DONE 单。"""
        h.issuer.build_fail = RuntimeError("RAM 接口 500")
        done = self.account(h, username)
        assert done["status"] == t.DONE, done["status"]
        assert not done.get("dev_policy_targets")
        h.issuer.build_fail = None
        return done

    def test_a_stuck_ticket_is_picked_up_and_finished(self):
        h = self.harness(HZ)
        stuck = self.stuck(h)
        lines = h.flows.retry_dev_policies()
        self.assertEqual(h.issuer.built, [("xinren", [("wuji-algo-dev-hz", "general")])])
        self.assertEqual(h.executor.attached, [("xinren", "Custom", devdir.policy_name("xinren"))])
        self.assertEqual(
            h.store.get(stuck["id"]).get("dev_policy_targets"), ["wuji-algo-dev-hz/general"]
        )
        self.assertEqual(len(lines), 1)
        self.assertIn("wuji-algo-dev-hz/general/xinren/", lines[0])

    def test_it_does_not_do_it_again_next_round(self):
        """一分钟一轮。补完不收手的话，每轮都去写一次策略版本 ——
        5 个版本的额度几分钟就轮换光，而旧版本是改坏了唯一的退路。"""
        h = self.harness(HZ)
        self.stuck(h)
        h.flows.retry_dev_policies()
        self.assertEqual(h.flows.retry_dev_policies(), [])
        self.assertEqual(len(h.issuer.built), 1)

    def test_a_ticket_that_never_tried_is_left_alone(self):
        """**这是这一组里最要紧的一条。** 判据必须是「试过且失败了」（有
        `dev_policy_needed` 事件），不是「没有完成标记」——
        后者会把这一步出现之前的**所有历史单子**都捞进来，等于每一轮给几百个人
        凭空发策略，没有任何审批、也没有任何人按下过什么。
        """
        h = self.harness(CPFS_ONLY)  # 纯 CPFS：没有桶 → 既没事件也没标记，像极了老单子
        done = self.account(h)
        self.assertIsNone(done.get("dev_policy_targets"))
        self.set_spaces(h, {"new-user": (HZ,)})  # 后来登记表加了 OSS 开发桶
        self.assertEqual(h.flows.retry_dev_policies(), [])
        self.assertEqual(h.issuer.built, [])

    def test_a_finished_ticket_is_left_alone(self):
        h = self.harness(HZ)
        self.account(h)
        self.assertEqual(h.flows.retry_dev_policies(), [])
        self.assertEqual(len(h.issuer.built), 1, "开通时那一次，不该有第二次")

    def test_a_ticket_that_is_not_done_is_left_alone(self):
        """还没开通完的单子由 `execute` 自己走这一步 —— 两条路同时动一张单，
        会在同一条策略上并发写版本。"""
        h = self.harness(HZ)
        ticket = h.submit(applicant=NEW, template="new-user", payload={"username": "xinren"})
        self.assertEqual(h.flows.retry_dev_policies(), [])
        self.assertEqual(h.store.get(ticket["id"])["status"], t.PENDING)
        self.assertEqual(h.issuer.built, [])

    def test_a_non_aliyun_ticket_is_left_alone(self):
        h = self.harness(HZ)
        self.stuck(h)
        rows = [
            replace(tpl, platform="volcano") if tpl.id == "new-user" else tpl
            for tpl in h.flows._catalog().templates
        ]
        patched = catalog_mod.Catalog(templates=tuple(rows))
        h.flows._catalog = lambda: patched
        self.assertEqual(h.flows.retry_dev_policies(), [])
        self.assertEqual(h.issuer.built, [])

    def test_a_template_that_no_longer_exists_does_not_crash_the_round(self):
        """模板被删 / 改名之后，那张老单子的 `template.id` 就指空了。
        这一步抛出去的话，**整轮定时任务**从这里断掉 —— 后面的步骤一个都不跑。"""
        h = self.harness(HZ)
        self.stuck(h)
        rows = [tpl for tpl in h.flows._catalog().templates if tpl.id != "new-user"]
        patched = catalog_mod.Catalog(templates=tuple(rows))
        h.flows._catalog = lambda: patched
        self.assertEqual(h.flows.retry_dev_policies(), [])

    def stuck_permission(self, h):
        """同上，但走权限单（「加入工作空间」）—— 存量的人是这一类。"""
        h.issuer.build_fail = RuntimeError("RAM 接口 500")
        ticket = h.submit(payload={"cloud_user": "lisi", "days": 7})
        h.approve(ticket)
        done = h.flows.sync(ticket["id"], force=True)
        assert done["status"] == t.DONE, done["status"]
        h.issuer.build_fail = None
        return done

    def test_a_permission_ticket_is_found_by_its_cloud_user(self):
        """权限单（「加入工作空间」）的登录名在 `cloud_user`，不在 `username`。
        只认一个的话，另一类单子会被静默跳过 —— 而存量的人走的正是这一类。"""
        h = self.harness(**{"oss-read": (SING,)})
        self.stuck_permission(h)
        h.flows.retry_dev_policies()
        self.assertEqual(h.issuer.built, [("lisi", [("wuji-algo-dev-sing", "general")])])

    def test_one_broken_ticket_does_not_block_the_rest(self):
        """一轮扫全量。一张单抛出去就把后面的人全耽误了，而且没人知道少做了什么。"""
        h = self.harness(HZ, **{"oss-read": (SING,)})
        first = self.stuck(h, "xinren")
        second = self.stuck_permission(h)
        real = h.flows.store.update

        def flaky(ticket_id, **kw):
            if ticket_id == first["id"] and kw.get("event") == "dev_policy_done":
                raise t.TicketError("单子正被别人改")
            return real(ticket_id, **kw)

        h.flows.store.update = flaky
        lines = h.flows.retry_dev_policies()
        self.assertEqual(len(lines), 2, lines)
        bad = [ln for ln in lines if ln.startswith(first["id"])]
        self.assertTrue(bad and "出错" in bad[0], bad)
        self.assertEqual(
            h.store.get(second["id"]).get("dev_policy_targets"), ["wuji-algo-dev-sing/general"]
        )

    def test_a_still_failing_retry_says_so_instead_of_going_quiet(self):
        """**定时任务的产出行是唯一的出口。** 一直补不上却打一行看着像正常的字，
        那就退回到「没有重试入口」那个状态了 —— 只是这次连事件都不新增。
        （这一行会不会被判成问题行，在 `test_delivery_trouble_words.py` 里钉。）"""
        h = self.harness(HZ)
        self.stuck(h)
        h.issuer.build_fail = RuntimeError("RAM 接口还是 500")
        lines = h.flows.retry_dev_policies()
        self.assertEqual(len(lines), 1)
        self.assertIn("开发目录授权失败", lines[0])
        self.assertIn("RAM 接口还是 500", lines[0])


# `token` 是 OSS 列举的**翻页游标**（NextContinuationToken），不是密钥。
# S107 只看参数名，所以在这儿关掉 —— 改名会让它和 OSS 的字段对不上
def _oss_page(prefixes, *, truncated=False, token="t2"):  # noqa: S107
    body = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
    more = f"<NextContinuationToken>{token}</NextContinuationToken>" if truncated else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">'
        f"{body}<IsTruncated>{str(truncated).lower()}</IsTruncated>{more}"
        "</ListBucketResult>"
    ).encode()


class WhichGroupTests(unittest.TestCase):
    """「这个人的开发目录在哪个组下」——**只能现扫，没有任何地方登记过**。

    组的结构只存在于对象键里，是人写数据时长出来的。模板里那个 `bucket_prefix`
    是个单值默认（`general`），而 2026-09-24 查实：桶里 51 个开发目录散在 10 个组下，
    **只有 9 个在 general**。照默认来的话，另外 42 个人会被额外建一个空的
    `general/<登录名>/`，策略也指向那个没人用的路径 —— 挂上了、还是写不进去，
    而控制台里看着「有权限」。这是最难查的一种。
    """

    def executor(self, pages):
        """`pages` 是 `{prefix: [一级公共前缀, …]}`。"""
        sent = []

        def transport(url, method="GET", headers=None, body=b""):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            sent.append(query)
            return 200, _oss_page(pages.get(query.get("prefix", ""), []))

        ex = AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=transport)
        ex._check_account = lambda: None
        return ex, sent

    def test_it_returns_bare_names_not_full_prefixes(self):
        """返回 `general` 而不是 `general/`、更不是 `general/lisi/` —— 这些名字要去拼
        ARN 和目录键，带上多余的一段就是一条永远匹配不上的前缀（表现还是 403）。"""
        ex, _ = self.executor({"": ["general/", "cv/"], "general/": ["general/lisi/"]})
        self.assertEqual(ex.list_dirs("wuji-algo-dev-hz", "", "cn-hangzhou"), ["general", "cv"])
        self.assertEqual(ex.list_dirs("wuji-algo-dev-hz", "general/", "cn-hangzhou"), ["lisi"])

    def test_the_scan_maps_every_login_to_its_real_group(self):
        from delivery import cli_requests

        ex, _ = self.executor(
            {
                "": ["general/", "cv/", "llm/"],
                "general/": ["general/lisi/"],
                "cv/": ["cv/wangwu/", "cv/zhaoliu/"],
                "llm/": ["llm/xinren/"],
            }
        )
        got = cli_requests._dev_dirs(ex, dict(HZ))
        self.assertEqual(got, {"lisi": "general", "wangwu": "cv", "zhaoliu": "cv", "xinren": "llm"})

    def test_an_empty_bucket_is_normal_and_maps_to_nothing(self):
        """新地域的桶里一个目录都没有，这不是故障 —— 这时候每个人按模板默认建就对了。"""
        from delivery import cli_requests

        ex, _ = self.executor({})
        self.assertEqual(cli_requests._dev_dirs(ex, dict(HZ)), {})

    def test_a_workspace_without_a_bucket_is_not_scanned(self):
        from delivery import cli_requests

        ex, sent = self.executor({"": ["general/"]})
        self.assertEqual(cli_requests._dev_dirs(ex, dict(CPFS_ONLY)), {})
        self.assertEqual(sent, [], "没有桶就别去问 OSS")

    def test_a_failed_scan_raises_instead_of_falling_back(self):
        """**扫不动必须抛。** 退回模板默认值就是这个 bug 本身：42 个人被建到
        `general/` 下、策略指向那个空目录，而面板每一步都报成功。
        宁可这次补齐整个失败、让人看见。"""
        from delivery import cli_requests
        from delivery.clouds import oss as oss_mod

        def transport(url, method="GET", headers=None, body=b""):
            return 403, b"<Error><Code>AccessDenied</Code></Error>"

        ex = AliyunExecutor(ACC, aliyun.Credentials("id", "sk"), transport=transport)
        ex._check_account = lambda: None
        with self.assertRaises(oss_mod.OssError):
            cli_requests._dev_dirs(ex, dict(HZ))

    def test_a_login_in_two_groups_keeps_only_one(self):
        """**已知限制，记在这儿**：同一个登录名在两个组下各有一个目录时，
        这张表只留后扫到的那个，于是策略只覆盖其中一个 —— 他在另一个组下的目录
        还是写不进去。真出现的话要把值改成列表、`targets_of` 那边也跟着一人多目录。
        （现网 51 个目录暂时没有重名，所以先记不改。）"""
        from delivery import cli_requests

        ex, _ = self.executor({"": ["a/", "b/"], "a/": ["a/lisi/"], "b/": ["b/lisi/"]})
        self.assertEqual(cli_requests._dev_dirs(ex, dict(HZ)), {"lisi": "b"})


class CloudPolicySnapshotTests(unittest.TestCase):
    """云上那两把 AK 到底动不动得了这条策略（`deploy/panel/cloud-policies/`）。

    前缀是分两边生效的：代码这边决定「面板打算建一个叫什么的策略」，云那边的策略
    决定「这把 AK 到底造不造得出它」。**改错的一边不会报错**，表现是每张单都走进
    「开发目录授权失败」那条失败分支 —— 而账号是建好的，看上去只是「偶尔有点问题」。

    副本以云上为准：这里红了的意思是「云上的策略还没放行」，修法是去云上改策略
    再重新导出，不是改期望值。
    """

    DIR = Path(__file__).resolve().parents[2] / "deploy" / "panel" / "cloud-policies"
    NAME = devdir.policy_name("alice")

    def load(self, which):
        return json.loads((self.DIR / f"aliyun.wuji-panel-{which}.json").read_text("utf-8"))

    def covers(self, doc, *, effect, action, resource):
        for stmt in doc.get("Statement") or ():
            if stmt.get("Effect") != effect:
                continue
            acts = stmt.get("Action") or []
            acts = acts if isinstance(acts, list) else [acts]
            if not any(fnmatch.fnmatch(action.lower(), str(a).lower()) for a in acts):
                continue
            res = stmt.get("Resource") or []
            res = res if isinstance(res, list) else [res]
            if any(fnmatch.fnmatch(resource, str(a).rsplit(":", 1)[-1]) for a in res):
                return True
        return False

    def test_the_issuer_can_create_and_update_this_policy(self):
        """2026-09-24 之前这条是红的（审计阻塞 1）：发放身份只放行了
        `policy/temp-ak-auto-*` 和 `policy/staff-oss-auto-*` 两族，没有
        `policy/wuji-dev-dir-*` —— **每一张单走到这一步都会 403**，结果是账号建好了、
        卡片上跟着一句「开发目录授权失败」，而人以为是 AK 的问题。

        当天在云上把这个前缀加进了 CreatePolicy 那条语句（v2→v3，回读验证过），
        副本也重新导出了。这条用例从此是那次变更的守门人：谁把前缀改了、
        或者导出的副本退回旧版本，它当场红。
        """
        doc = self.load("issuer")
        # 只列 `ensure_dev_policy` 真会调的那两个 —— 多列一个就是拿测试替云上做主张
        for act in ("ram:CreatePolicy", "ram:CreatePolicyVersion"):
            with self.subTest(action=act):
                self.assertTrue(
                    self.covers(doc, effect="Allow", action=act, resource=f"policy/{self.NAME}"),
                    f"发放身份在云上不能 {act} 一条 {devdir.POLICY_PREFIX}* 策略，"
                    "建号时这一步必 403",
                )

    def test_the_executor_can_attach_it_to_a_real_person(self):
        doc = self.load("executor")
        self.assertTrue(
            self.covers(
                doc, effect="Allow", action="ram:AttachPolicyToUser", resource=f"policy/{self.NAME}"
            )
        )
        self.assertTrue(
            self.covers(doc, effect="Allow", action="ram:AttachPolicyToUser", resource="user/lisi")
        )

    def test_the_new_prefix_did_not_widen_into_any_policy_name(self):
        """加前缀的正确做法是**再列一条 ARN**，不是把 Resource 放成 `policy/*`。

        放宽的那一版不会有任何症状（功能照样好用）：发放身份从此能新建/改写
        这个账号下**任意一条自定义策略**，包括挂在管理员身上的那些 —— 而它本来
        连真人的子账号都够不着，那道隔离就白设了。
        """
        doc = self.load("issuer")
        for name in ("team-data-reader", "wuji-oss-auto-lisi", "AdministratorAccess"):
            with self.subTest(policy=name):
                self.assertFalse(
                    self.covers(
                        doc, effect="Allow", action="ram:CreatePolicy", resource=f"policy/{name}"
                    )
                )

    def test_the_grant_stays_pinned_to_the_panel_server(self):
        """那条语句上绑着 `IpAddress: 120.79.167.166`（面板服务器）—— 这把 AK 泄漏出去
        也只能在那台机器上用。加前缀时把条件一起漏掉的话同样没有任何症状。"""
        doc = self.load("issuer")
        stmt = [
            s
            for s in doc["Statement"]
            if "ram:CreatePolicy" in (s.get("Action") or []) and s.get("Effect") == "Allow"
        ]
        self.assertEqual(len(stmt), 1)
        self.assertEqual(stmt[0]["Condition"], {"IpAddress": {"acs:SourceIp": ["120.79.167.166"]}})

    def test_the_issuer_still_cannot_touch_a_real_person(self):
        """这条策略的加入不该顺手把发放身份的边界撑开。"""
        doc = self.load("issuer")
        for act in ("ram:DeleteUser", "ram:AttachPolicyToUser"):
            with self.subTest(action=act):
                self.assertFalse(self.covers(doc, effect="Allow", action=act, resource="user/lisi"))
