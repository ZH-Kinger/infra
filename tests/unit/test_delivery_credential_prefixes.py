"""长期凭证子账号的两套前缀：内部 `staff-` / 外部 `tempak-`，以及消费这张表的五处。

为什么值得单独一个文件
──────────────────────
把面板发给同事的凭证和机器人发给供应商的凭证改成两套前缀，是这一轮改动里**唯一
动了安全边界**的一处：`grants.ISSUED_PREFIXES` 同时被「无主账号」「服务号打标」
「个人目录」「离职受保护名单」四处消费，而后两处是**判「不许碰」的名单**。
改名漏掉其中任何一处，表现都不是报错，而是：

  · 漏掉 `hygiene` / `views` → 程序发的号涌进「无主账号」，那一栏变噪音，没人看第二遍；
  · 漏掉 `workspace_tree` → 给一个临时凭证号建了个人目录，建完没人认领；
  · **漏掉 `offboard` → 离职流程会去停一个程序发的凭证号**，而那把钥匙正在跑生产任务。

所以这里的断言刻意**不只比字面量**：每一处都拿「一个该被识别的名字」走一遍它的
真实判据。字面量对齐只能证明两张表长得一样，走一遍才能证明它真的生效了。

纯逻辑，不碰网络、不碰云。
"""

from __future__ import annotations

import re
import unittest

from delivery import grants, hygiene, move_creds, offboard, policies, workspace_tree
from delivery.identity.audit import SERVICE_PREFIXES, classify
from delivery.identity.audit import AccountUser as _AuditUser

#: 一个能覆盖到所有分支的样本名：每个程序前缀各造一个
SAMPLES = tuple(f"{p}alice-9a1b2c" for p in grants.ISSUED_PREFIXES)


class _User:
    """权限快照里的一个子账号（`workspace_tree.plan` 只看 `.name` 和 `.groups`）。"""

    def __init__(self, name: str, groups=("algo",)):
        self.name = name
        self.groups = tuple(groups)


class ConstantTests(unittest.TestCase):
    """前缀表本身：取值、配对、唯一一份。"""

    def test_internal_and_external_are_different_families(self):
        """两套前缀要能**一眼分出内外** —— 这是整件事的目的。

        长得一样（都叫 `tempak-`）的时候，RAM 控制台上只能靠显示名分辨，
        而出事那一刻最先要回答的就是「这把钥匙在公司里还是在公司外」。
        """
        self.assertEqual(grants.USER_PREFIX, "staff-")
        self.assertEqual(grants.POLICY_PREFIX, "staff-oss-auto-")
        self.assertEqual(grants.EXTERNAL_USER_PREFIX, "tempak-")
        self.assertEqual(grants.EXTERNAL_POLICY_PREFIX, "temp-ak-auto-")
        self.assertNotEqual(grants.USER_PREFIX, grants.EXTERNAL_USER_PREFIX)
        self.assertNotEqual(grants.POLICY_PREFIX, grants.EXTERNAL_POLICY_PREFIX)

    def test_neither_user_prefix_is_a_prefix_of_the_other(self):
        """互不包含。否则 `policy_name` 里那个 `startswith` 循环会按表的顺序
        把一族判成另一族，而两族对应的策略名不同 —— 撤销时找不到那条策略。"""
        self.assertFalse(grants.USER_PREFIX.startswith(grants.EXTERNAL_USER_PREFIX))
        self.assertFalse(grants.EXTERNAL_USER_PREFIX.startswith(grants.USER_PREFIX))

    def test_issued_prefixes_covers_both_families_and_the_historical_ones(self):
        """程序发的号那张表要**盖住历史写法**：撤销和到期清理会处理早年发出去的号，
        只认新前缀的话它们永远清不掉，而它们是**长期 AK**。"""
        self.assertIn(grants.USER_PREFIX, grants.ISSUED_PREFIXES)
        self.assertIn(grants.EXTERNAL_USER_PREFIX, grants.ISSUED_PREFIXES)
        self.assertIn("temp-ak-", grants.ISSUED_PREFIXES)
        self.assertIn("panel-", grants.ISSUED_PREFIXES)

    def test_no_issued_prefix_is_empty_or_uppercase(self):
        """空串会让所有名字都被判成「程序发的」（`startswith("")` 恒真）——
        那等于把整份无主清单和整份离职名单一起清空，而且一条日志都不会有。"""
        for p in grants.ISSUED_PREFIXES:
            self.assertTrue(p, "空前缀会命中所有名字")
            self.assertEqual(p, p.lower(), f"{p} 带大写，而消费方都先 lower()")


class PolicyNameTests(unittest.TestCase):
    """子账号名 → 它那条自定义策略名。"""

    def test_both_families_resolve_to_their_own_policy_prefix(self):
        self.assertEqual(
            grants.policy_name("staff-lisi-9a1b2c"), "staff-oss-auto-staff-lisi-9a1b2c"
        )
        self.assertEqual(grants.policy_name("tempak-ext-9a1b2c"), "temp-ak-auto-tempak-ext-9a1b2c")

    def test_a_name_from_neither_family_is_refused(self):
        """**不给兜底前缀。** 猜一个的话，撤销时会去删一条不存在的策略、
        当成「删干净了」，而真正那条还挂在号上。"""
        for bad in ("lisi", "staffx-1", "staff", "", "temp-ak-auto-x", "STAFF-lisi", " staff-a"):
            with self.assertRaises(grants.GrantError, msg=bad):
                grants.policy_name(bad)

    def test_the_refusal_names_both_allowed_prefixes(self):
        # 报错要能让人直接看出该叫什么，否则只能去翻源码
        with self.assertRaises(grants.GrantError) as caught:
            grants.policy_name("lisi")
        said = str(caught.exception)
        self.assertIn(grants.USER_PREFIX, said)
        self.assertIn(grants.EXTERNAL_USER_PREFIX, said)

    def test_the_historical_prefixes_are_not_policy_nameable(self):
        """`temp-ak-` / `panel-` 在 `ISSUED_PREFIXES` 里（要认得出、不许碰），
        但**不是**面板发凭证用的两族之一 —— 别让它们意外拿到一个策略名。"""
        for name in ("temp-ak-old-1", "panel-executor"):
            with self.assertRaises(grants.GrantError, msg=name):
                grants.policy_name(name)


class ReadableTests(unittest.TestCase):
    """`readable()`：这次 slug 是真取出了拉丁字母，还是退化成哈希了。"""

    def test_pinyin_local_parts_are_readable(self):
        """回归锁：原先靠**形状**猜（`x` + 9 个字符 = 哈希），而 `xuzhiyuan`（徐志远）
        恰好就是 x 开头的 9 个字母 —— 按形状判会把他判成哈希，于是本该可读的名字
        又被换成一串乱码。姓「谢」「肖」「熊」「许」的人全中。"""
        for local in ("xuzhiyuan", "xiehaoran", "xionglibo", "xiaoming", "x", "x12345678"):
            self.assertTrue(grants.readable(local), local)

    def test_a_name_with_no_latin_letters_is_not_readable(self):
        for bad in ("元客", "枢途", "", "   ", "—", "。。", None):
            self.assertFalse(grants.readable(bad), repr(bad))

    def test_readable_agrees_with_what_slug_actually_did(self):
        """判据只有一条：`readable` 说可读 ⟺ `slug` 没走哈希那条路。
        两者各写各的话，以后改 slug 的字符表就会悄悄失配。"""
        for text in ("xuzhiyuan", "li.si", "元客", "", "a_b", "123", "李-四", "n1"):
            self.assertEqual(
                grants.readable(text),
                not re.fullmatch(r"x[0-9a-f]{8}", grants.slug(text)) and grants.slug(text) != "ext",
                text,
            )


class UserNameTests(unittest.TestCase):
    """面板发的子账号名。"""

    def test_the_login_name_carries_the_internal_prefix(self):
        self.assertTrue(grants.user_name("李四", rand="abc123").startswith(grants.USER_PREFIX))

    def test_the_email_local_part_wins_over_a_chinese_name(self):
        """中文名转不出拉丁字母时 slug 会退化成哈希，于是云上出现
        `staff-x090fd8a0-…`，运维在 RAM 控制台上根本认不出是谁（线上真出现过）。
        邮箱前缀可读、稳定、人人都有。"""
        self.assertEqual(
            grants.user_name("李四", rand="abc123", email="li.si@wuji.tech"),
            "staff-li-si-abc123",
        )

    def test_a_pinyin_email_is_not_mistaken_for_a_hash(self):
        # 这正是 readable 那条回归的下游后果：判错就又回到乱码名字
        self.assertEqual(
            grants.user_name("徐志远", rand="abc123", email="xuzhiyuan@wuji.tech"),
            "staff-xuzhiyuan-abc123",
        )

    def test_a_chinese_email_local_part_falls_back_to_the_subject(self):
        """邮箱前缀取不出拉丁字母时才回落。回落到**主体名**而不是直接哈希：
        主体名可能是「元客」这种中文，也可能是一个英文公司名。"""
        self.assertEqual(
            grants.user_name("Yuanke", rand="abc123", email="元客@wuji.tech"),
            "staff-yuanke-abc123",
        )

    def test_no_email_and_a_chinese_subject_still_produces_a_stable_name(self):
        """哈希是最后一道：同一个主体每次得到同一个片段，不同主体不会撞 ——
        否则云上一堆 `staff-ext-*`，只靠随机后缀区分。"""
        first = grants.user_name("元客", rand="abc123")
        again = grants.user_name("元客", rand="def456")
        self.assertNotEqual(first, again, "随机后缀让同一个人的多次发放互不覆盖")
        self.assertEqual(first[: -len("abc123")], again[: -len("def456")])
        self.assertNotEqual(
            first[: -len("abc123")], grants.user_name("枢途", rand="abc123")[: -len("abc123")]
        )

    def test_the_generated_name_can_always_be_turned_into_a_policy_name(self):
        """发得出去就必须撤得回来。`user_name` 造出来的名字如果 `policy_name` 不认，
        那条策略就成了删不掉的残留。"""
        for subject, email in (("李四", "li.si@wuji.tech"), ("元客", ""), ("Xu", "x@wuji.tech")):
            name = grants.user_name(subject, rand="abc123", email=email)
            self.assertTrue(grants.policy_name(name).startswith(grants.POLICY_PREFIX), name)


class ExternalFamilyTests(unittest.TestCase):
    """交给对方云的那把源端钥匙按**外部**算。"""

    def test_move_credentials_use_the_external_prefix(self):
        """这把 AK 会被**明文写进对方云的迁移任务配置里长期留存** —— 它离开了
        我们的边界，和发给同事的内部凭证不是一回事，名字上就该分开。"""
        name = move_creds.user_name("REQ-20260923-ABCD")
        self.assertTrue(name.startswith(grants.EXTERNAL_USER_PREFIX), name)
        self.assertFalse(name.startswith(grants.USER_PREFIX), name)

    def test_a_move_credential_is_still_revocable(self):
        self.assertEqual(
            grants.policy_name(move_creds.user_name("REQ-1")),
            grants.EXTERNAL_POLICY_PREFIX + move_creds.user_name("REQ-1"),
        )

    def test_a_move_credential_is_protected_from_offboarding(self):
        # 搬运跑到一半的号被离职流程停掉 = 21TB 传到一半断了，而没人会想到是离职流程干的
        self.assertTrue(offboard.protected(move_creds.user_name("REQ-1")))


class FanoutTests(unittest.TestCase):
    """**这张表只有一份**：四处消费方各走一遍自己的真实判据。

    比字面量只能证明两张表长得一样；走一遍才能证明它真的生效了。
    """

    def test_hygiene_reads_the_shared_table_itself(self):
        self.assertIs(hygiene._ISSUED_PREFIXES, grants.ISSUED_PREFIXES)

    def test_audit_service_prefixes_start_with_the_shared_table(self):
        self.assertEqual(SERVICE_PREFIXES[: len(grants.ISSUED_PREFIXES)], grants.ISSUED_PREFIXES)

    def test_every_issued_prefix_is_classified_as_a_service_account(self):
        """SSO 对账那一页：程序发的号没有企业邮箱，不打成服务号就会挂在
        「云上没填邮箱」那一栏里，把真正该催的人淹掉。"""
        for name in SAMPLES:
            found = classify(_AuditUser("aliyun", "1704065796538912", name), domain="@wuji.tech")
            self.assertEqual(found.verdict, "service", name)

    def test_every_issued_prefix_is_not_an_orphan(self):
        """程序发的号有到期时间、由清理任务负责，不是「无主」。
        混进无主清单里会让那一栏的一半都是噪音。"""
        for name in SAMPLES:
            self.assertTrue(hygiene.is_program_account(name, ()), name)
        self.assertFalse(hygiene.is_program_account("lisi", ()), "真人不该被当成程序发的号")

    def test_the_orphan_judgement_ignores_case(self):
        # 云上的登录名大小写不由我们定；判错的后果是这个号进了无主清单或离职名单
        self.assertTrue(hygiene.is_program_account("STAFF-Alice-9A1B2C", ()))
        self.assertTrue(offboard.protected("STAFF-Alice-9A1B2C"))

    def test_every_issued_prefix_gets_no_personal_directory(self):
        """给一个临时凭证号建个人目录，建完没人认领，而目录名会一直留在桶里。

        名册里**故意给它们都登记了主人**：判据必须是前缀，不能靠「认不出属主」
        顺带挡掉 —— 哪天有人把发凭证的号也登记进名册，那层顺带就没了。
        """
        names = [*SAMPLES, "lisi"]
        users = [_User(n) for n in names]
        slots, skipped = workspace_tree.plan(users, people=dict.fromkeys(names, "某人"))
        made = " ".join(str(getattr(s, "login", "")) for s in slots)
        said = " ".join(skipped)
        for name in SAMPLES:
            self.assertIn(name, said, name)
            self.assertNotIn(name, made, name)
        self.assertIn("lisi", made, "真人照建")

    def test_every_issued_prefix_is_protected_from_offboarding(self):
        """**这条是安全边界。** 离职流程去停一个程序发的凭证号，
        而那把钥匙正在跑生产任务 —— 停完没有任何地方会说是谁停的。"""
        for name in SAMPLES:
            self.assertTrue(offboard.protected(name), name)
            self.assertTrue(offboard.PROTECTED.match(name), f"{name}（旧写法入口）")

    def test_a_real_person_is_still_offboardable(self):
        """反过来也要成立：保护名单写宽了，离职的人就永远停不掉，
        而那比多停一个号危险得多。"""
        for name in ("lisi", "xuzhiyuan", "wangwu"):
            self.assertFalse(offboard.protected(name), name)


class SelfPolicyTests(unittest.TestCase):
    """两族的策略名都不许被员工通过接口申请到。"""

    def test_both_policy_prefixes_are_in_the_self_policy_list(self):
        """漏掉 `staff-oss-auto-*` 的后果很具体：一个人的凭证策略能通过规则接口
        被挂到另一个人头上 —— 那条策略里写着别人的桶和目录。"""
        self.assertIn(grants.POLICY_PREFIX + "*", policies.SELF_POLICY)
        self.assertIn(grants.EXTERNAL_POLICY_PREFIX + "*", policies.SELF_POLICY)

    def test_a_concrete_credential_policy_is_refused_by_the_rules_api(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as box:
            path = str(Path(box) / "policy-rules.json")
            for user in ("staff-lisi-9a1b2c", "tempak-ext-9a1b2c"):
                with self.assertRaises(policies.PolicyError, msg=user):
                    policies.write_rules(
                        path, {"allow": [grants.policy_name(user)]}, actor="on_admin"
                    )


if __name__ == "__main__":
    unittest.main()
