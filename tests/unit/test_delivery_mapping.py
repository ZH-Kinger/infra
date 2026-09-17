"""飞书身份 → 云账号 的映射。

判错的代价不对称：推不出来只是让管理员多登记一行；**推错**会让人在 SSO 上线当天
登进别人的账号，或者登录报「账户不存在」而没人知道为什么。所以这里的测试
大半在盯「该拒绝的有没有拒绝」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery.identity import AccountUser
from delivery.identity.mapping import (
    SOURCE_DISPLAY_NAME,
    SOURCE_NONE,
    SOURCE_OVERRIDE,
    SOURCE_RULE,
    MappingError,
    build,
    candidates,
    derive,
    load_overrides,
    parse_feishu_name,
    to_override_stub,
    unresolved,
)

DOMAIN = "@wuji.tech"


def u(name, email, platform="aliyun", account="default"):
    return AccountUser(platform=platform, account=account, name=name, display_name="", email=email)


class CandidateTests(unittest.TestCase):
    def test_email_yields_both_orderings(self):
        self.assertEqual(candidates("li.si"), {"lisi", "sili"})

    def test_feishu_english_name_yields_the_same_set(self):
        """飞书英文名和企业邮箱是同一份信息的两种排列，候选集必须一致。"""
        self.assertEqual(candidates(feishu_en="Si Li"), candidates("li.si"))

    def test_both_sources_merge(self):
        got = candidates("li.si", feishu_en="Si Li")
        self.assertEqual(got, {"lisi", "sili"})

    def test_single_token_has_no_reverse(self):
        self.assertEqual(candidates("tom"), {"tom"})

    def test_three_tokens_do_not_get_reversed(self):
        """三段式反过来拼没有语义，只会凭空制造撞车面。"""
        self.assertEqual(candidates("a.b.c"), {"abc"})

    def test_underscore_is_a_separator_too(self):
        self.assertEqual(candidates("li_si"), {"lisi", "sili"})

    def test_empty_inputs(self):
        self.assertEqual(candidates(""), set())
        self.assertEqual(candidates("", feishu_en=""), set())

    def test_case_is_normalised(self):
        self.assertEqual(candidates("LI.Si"), {"lisi", "sili"})

    def test_derive_still_means_forward_order(self):
        self.assertEqual(derive("li.si"), "lisi")


class BuildRuleTests(unittest.TestCase):
    def test_forward_order_matches_aliyun_convention(self):
        got = build([u("lisi", "li.si@wuji.tech")], domain=DOMAIN)
        self.assertEqual(got[0].source, SOURCE_RULE)
        self.assertEqual(got[0].cloud_name, "lisi")

    def test_reverse_order_matches_volcano_convention(self):
        got = build([u("SiLi", "li.si@wuji.tech", platform="volcano")], domain=DOMAIN)
        self.assertEqual(got[0].source, SOURCE_RULE)
        self.assertEqual(got[0].cloud_name, "SiLi")

    def test_given_name_first_no_longer_needs_a_human(self):
        """典型情况：只有「去点」一条规则时，这类人是要去找本人改账号名的例外。"""
        got = build([u("xiaobaqian", "qian.xiaoba@wuji.tech")], domain=DOMAIN)
        self.assertEqual(got[0].source, SOURCE_RULE)

    def test_non_corporate_email_is_skipped_entirely(self):
        self.assertEqual(build([u("zhouba", "10000001@qq.com")], domain=DOMAIN), [])

    def test_unmatchable_reports_every_candidate_it_tried(self):
        got = build([u("xiaoqi", "sun.xiaoqi@wuji.tech")], domain=DOMAIN)
        self.assertEqual(got[0].source, SOURCE_NONE)
        self.assertEqual(got[0].cloud_name, "")
        self.assertIn("xiaoqisun", got[0].note)
        self.assertIn("sunxiaoqi", got[0].note)
        self.assertIn("xiaoqi", got[0].note)

    def test_feishu_name_rescues_someone_without_a_corporate_email(self):
        """这朵云上没填企业邮箱，但飞书有英文名——照样能解析。"""
        got = build(
            [u("JiuWu", "wu.jiu@wuji.tech", platform="volcano")],
            domain=DOMAIN,
            feishu_names={"wu.jiu@wuji.tech": "Jiu Wu"},
        )
        self.assertEqual(got[0].source, SOURCE_RULE)


class AmbiguityTests(unittest.TestCase):
    """两种排列都接受，撞车就成了真实风险。这一组全是「必须拒绝」。"""

    def test_candidate_pointing_at_someone_else_is_refused(self):
        """`test` 账号占着某人的企业邮箱，而反序候选正是那人自己的账号名。"""
        got = build(
            [
                u("test", "li.si@wuji.tech", platform="volcano"),
                u("SiLi", "someone@163.com", platform="volcano"),
            ],
            domain=DOMAIN,
        )
        self.assertEqual(len(got), 1)  # 163 邮箱不是企业域，本就不参与
        self.assertEqual(got[0].source, SOURCE_NONE)
        self.assertIn("歧义", got[0].note)
        self.assertIn("SiLi", got[0].note)

    def test_ambiguity_wins_over_a_self_match(self):
        """自己中了也不算数——同一个邮箱解析出两个账号，SSO 当天登谁全看实现顺序。"""
        got = build(
            [
                u("lisi", "li.si@wuji.tech"),
                u("sili", "other.person@wuji.tech"),
            ],
            domain=DOMAIN,
        )
        first = next(m for m in got if m.email == "li.si@wuji.tech")
        self.assertEqual(first.source, SOURCE_NONE)
        self.assertIn("歧义", first.note)

    def test_ambiguity_is_scoped_per_cloud(self):
        """阿里的 lisi 和火山的 SiLi 是同一个人，不是冲突。"""
        got = build(
            [
                u("lisi", "li.si@wuji.tech", platform="aliyun"),
                u("SiLi", "li.si@wuji.tech", platform="volcano"),
            ],
            domain=DOMAIN,
        )
        self.assertEqual({m.source for m in got}, {SOURCE_RULE})

    def test_ambiguity_is_scoped_per_account_too(self):
        got = build(
            [
                u("lisi", "li.si@wuji.tech", account="default"),
                u("sili", "li.si@wuji.tech", account="1949"),
            ],
            domain=DOMAIN,
        )
        self.assertEqual({m.source for m in got}, {SOURCE_RULE})

    def test_override_beats_ambiguity(self):
        """人工登记过的就按登记来——护栏是给自动推导用的，不是拦人的。"""
        got = build(
            [
                u("test", "li.si@wuji.tech", platform="volcano"),
                u("SiLi", "x.y@wuji.tech", platform="volcano"),
            ],
            domain=DOMAIN,
            overrides={"volcano/default": {"li.si@wuji.tech": "test"}},
        )
        m = next(x for x in got if x.email == "li.si@wuji.tech")
        self.assertEqual(m.source, SOURCE_OVERRIDE)
        self.assertEqual(m.cloud_name, "test")


class OverrideTests(unittest.TestCase):
    def test_override_wins_over_rule(self):
        """登记值必须是同云上真实存在的账号；不存在的那种见 StaleOverrideTests。"""
        got = build(
            [u("lisi", "li.si@wuji.tech"), u("someone-else", "a.b@wuji.tech")],
            domain=DOMAIN,
            overrides={"aliyun/default": {"li.si@wuji.tech": "someone-else"}},
        )
        m = next(x for x in got if x.email == "li.si@wuji.tech")
        self.assertEqual(m.source, SOURCE_OVERRIDE)
        self.assertEqual(m.cloud_name, "someone-else")
        self.assertIn("不一致", m.note)

    def test_override_matching_reality_carries_no_warning(self):
        got = build(
            [u("lisi", "li.si@wuji.tech")],
            domain=DOMAIN,
            overrides={"aliyun/default": {"li.si@wuji.tech": "lisi"}},
        )
        self.assertEqual(got[0].note, "")

    def test_unresolved_only_returns_the_actionable_ones(self):
        got = build(
            [u("lisi", "li.si@wuji.tech"), u("xiaoqi", "sun.xiaoqi@wuji.tech")],
            domain=DOMAIN,
        )
        self.assertEqual([m.email for m in unresolved(got)], ["sun.xiaoqi@wuji.tech"])

    def test_stub_prefills_the_actual_cloud_name(self):
        got = build([u("xiaoqi", "sun.xiaoqi@wuji.tech")], domain=DOMAIN)
        stub = json.loads(to_override_stub(got))
        self.assertEqual(stub["aliyun/default"]["sun.xiaoqi@wuji.tech"], "xiaoqi")

    def test_stub_prefills_for_ambiguous_rows_too(self):
        got = build(
            [
                u("test", "li.si@wuji.tech", platform="volcano"),
                u("SiLi", "x.y@wuji.tech", platform="volcano"),
            ],
            domain=DOMAIN,
        )
        stub = json.loads(to_override_stub(got))
        self.assertEqual(stub["volcano/default"]["li.si@wuji.tech"], "test")


class LoadOverridesTests(unittest.TestCase):
    def _write(self, payload):
        d = tempfile.mkdtemp()
        p = Path(d) / "overrides.json"
        p.write_text(payload, encoding="utf-8")
        return str(p)

    def test_roundtrip(self):
        path = self._write(json.dumps({"aliyun/default": {"a@wuji.tech": "aa"}}))
        self.assertEqual(load_overrides(path), {"aliyun/default": {"a@wuji.tech": "aa"}})

    def test_missing_file(self):
        with self.assertRaises(MappingError):
            load_overrides("/nonexistent/overrides.json")

    def test_bad_json(self):
        with self.assertRaises(MappingError):
            load_overrides(self._write("{"))

    def test_top_level_must_be_object(self):
        with self.assertRaises(MappingError):
            load_overrides(self._write("[]"))

    def test_scope_value_must_be_object(self):
        with self.assertRaises(MappingError):
            load_overrides(self._write(json.dumps({"aliyun/default": "nope"})))

    def test_empty_mapping_value_is_rejected(self):
        """空值会被当成「已登记」而静默解析成空账号名。"""
        with self.assertRaises(MappingError):
            load_overrides(self._write(json.dumps({"aliyun/default": {"a@wuji.tech": ""}})))


if __name__ == "__main__":
    unittest.main()


class StaleOverrideTests(unittest.TestCase):
    """登记值指向不存在的账号 —— 比不登记更糟，因为它赢过规则且不进待办列表。

    典型场景：账号批量改名后，`identity/overrides.json` 里的登记可能整批失效，
    每一条都指向云上已不存在的名字，而不拦的话一条都不会被发现。
    """

    def test_override_pointing_at_a_nonexistent_account_is_rejected(self):
        got = build(
            [u("xiaoqi", "sun.xiaoqi@wuji.tech", platform="volcano")],
            domain=DOMAIN,
            overrides={"volcano/default": {"sun.xiaoqi@wuji.tech": "xiaoqisun"}},
        )
        self.assertEqual(got[0].source, SOURCE_NONE)
        self.assertEqual(got[0].cloud_name, "")
        self.assertIn("不存在", got[0].note)
        self.assertIn("xiaoqisun", got[0].note)

    def test_stale_override_shows_up_in_unresolved(self):
        """关键：它必须出现在待办里，否则没人会去修。"""
        got = build(
            [u("zhengshi", "zheng.shi@wuji.tech")],
            domain=DOMAIN,
            overrides={"aliyun/default": {"zheng.shi@wuji.tech": "ShiZheng"}},
        )
        self.assertEqual([m.email for m in unresolved(got)], ["zheng.shi@wuji.tech"])

    def test_stale_override_does_not_mask_a_rule_that_now_works(self):
        """改名后规则本来算得对，过期登记不该把它覆盖掉。"""
        got = build(
            [u("zhengshi", "zheng.shi@wuji.tech")],
            domain=DOMAIN,
            overrides={"aliyun/default": {"zheng.shi@wuji.tech": "ShiZheng"}},
        )
        self.assertNotEqual(got[0].cloud_name, "ShiZheng")

    def test_deliberate_remap_to_another_real_account_is_still_allowed(self):
        """指向同云上另一个真实账号 = 管理员有意为之，不拦，只标注。"""
        got = build(
            [
                u("zhangsan", "zhang.san@wuji.tech"),
                u("legacy-zhangsan", "x.y@wuji.tech"),
            ],
            domain=DOMAIN,
            overrides={"aliyun/default": {"zhang.san@wuji.tech": "legacy-zhangsan"}},
        )
        m = next(x for x in got if x.email == "zhang.san@wuji.tech")
        self.assertEqual(m.source, SOURCE_OVERRIDE)
        self.assertEqual(m.cloud_name, "legacy-zhangsan")
        self.assertIn("不一致", m.note)

    def test_override_is_matched_case_insensitively(self):
        got = build(
            [u("SiLi", "li.si@wuji.tech", platform="volcano")],
            domain=DOMAIN,
            overrides={"volcano/default": {"li.si@wuji.tech": "sili"}},
        )
        self.assertEqual(got[0].source, SOURCE_OVERRIDE)


class FeishuNameParseTests(unittest.TestCase):
    """飞书显示名的实际格式是 `李四（Si Li）`，少量纯英文如 `tom`。"""

    def test_full_width_parentheses(self):
        self.assertEqual(parse_feishu_name("李四（Si Li）"), ("李四", "Si Li"))

    def test_half_width_parentheses(self):
        self.assertEqual(parse_feishu_name("李四(Si Li)"), ("李四", "Si Li"))

    def test_messy_whitespace(self):
        self.assertEqual(parse_feishu_name("  李四 （ Si Li ） "), ("李四", "Si Li"))

    def test_english_only(self):
        self.assertEqual(parse_feishu_name("tom"), ("", "tom"))

    def test_chinese_only(self):
        self.assertEqual(parse_feishu_name("李四"), ("李四", ""))

    def test_four_character_name(self):
        self.assertEqual(
            parse_feishu_name("欧阳小明（Xiaoming Ouyang）"), ("欧阳小明", "Xiaoming Ouyang")
        )

    def test_empty(self):
        self.assertEqual(parse_feishu_name(""), ("", ""))
        self.assertEqual(parse_feishu_name(None), ("", ""))

    def test_unbracketed_mix_is_not_guessed(self):
        """不带括号的混写不拆——拆错比匹配不上更危险。"""
        cn, en = parse_feishu_name("李四 Si Li")
        self.assertEqual(en, "")


def v(name, display, email=""):
    return AccountUser(
        platform="volcano", account="default", name=name, display_name=display, email=email
    )


FEISHU = {
    "li.si@wuji.tech": "李四（Si Li）",
    "zhou.le@wuji.tech": "周乐（Le Zhou）",
    "tom@wuji.tech": "tom",
}


class DisplayNameLayerTests(unittest.TestCase):
    def test_rescues_a_user_with_no_corporate_email(self):
        """火山上多数人没填企业邮箱——以前直接跳过，现在按中文名对上。"""
        got = build([v("SiLi", "李四")], domain=DOMAIN, feishu_names=FEISHU)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].source, SOURCE_DISPLAY_NAME)
        self.assertEqual(got[0].email, "li.si@wuji.tech")
        self.assertEqual(got[0].cloud_name, "SiLi")

    def test_rescues_a_pinyin_mismatch(self):
        """乐读 le 还是 yue——任何字符串规则都推不出 YueZhou，但中文名一样。"""
        got = build([v("YueZhou", "周乐")], domain=DOMAIN, feishu_names=FEISHU)
        self.assertEqual(got[0].source, SOURCE_DISPLAY_NAME)
        self.assertEqual(got[0].email, "zhou.le@wuji.tech")

    def test_confirms_when_email_rule_fails(self):
        """有企业邮箱但规则推不出时，显示名是**确认**不是猜——邮箱已经钉住了人。"""
        got = build(
            [v("YueZhou", "周乐", "zhou.le@wuji.tech")],
            domain=DOMAIN,
            feishu_names=FEISHU,
        )
        self.assertEqual(got[0].source, SOURCE_DISPLAY_NAME)

    def test_english_only_name_matches_case_insensitively(self):
        got = build([v("tom", "Tom")], domain=DOMAIN, feishu_names=FEISHU)
        self.assertEqual(got[0].source, SOURCE_DISPLAY_NAME)
        self.assertEqual(got[0].email, "tom@wuji.tech")

    def test_email_rule_still_wins_over_display_name(self):
        """显示名是弱标识，只在邮箱推不出时才用。"""
        got = build(
            [v("lisi", "李四", "li.si@wuji.tech")],
            domain=DOMAIN,
            feishu_names=FEISHU,
        )
        self.assertEqual(got[0].source, SOURCE_RULE)

    def test_override_wins_over_display_name(self):
        got = build(
            [v("SiLi", "李四"), v("legacy", "旧号")],
            domain=DOMAIN,
            feishu_names=FEISHU,
            overrides={"volcano/default": {"li.si@wuji.tech": "legacy"}},
        )
        m = next(x for x in got if x.email == "li.si@wuji.tech")
        self.assertEqual(m.source, SOURCE_OVERRIDE)

    def test_without_a_feishu_directory_no_email_users_are_still_skipped(self):
        """没给名册就保持旧行为，不凭空制造一堆待办。"""
        self.assertEqual(build([v("SiLi", "李四")], domain=DOMAIN), [])

    def test_unknown_person_is_skipped_not_flagged(self):
        """飞书里没这个人，多半是服务号或已离职，不是映射问题。"""
        self.assertEqual(build([v("robot", "机器人")], domain=DOMAIN, feishu_names=FEISHU), [])


class DisplayNameCollisionTests(unittest.TestCase):
    """显示名会重名，谁有 RAM 写权限谁就能改。任何一侧重名都必须拒绝——
    配错的后果是 A 在看板上看到 B 的权限。"""

    def test_two_cloud_accounts_with_the_same_display_name_are_both_refused(self):
        """同一个人在火山上有缩写号和全拼号两个号，其中一个是超管。
        挑一个就可能把超管号映射给错的人。"""
        feishu = {"zhao.xiaoliu@wuji.tech": "赵小六（Xiaoliu Zhao）"}
        got = build(
            [v("zhaoxl", "赵小六"), v("XiaoliuZhao", "赵小六")],
            domain=DOMAIN,
            feishu_names=feishu,
        )
        self.assertEqual(len(got), 2)
        self.assertTrue(all(m.source == SOURCE_NONE for m in got))
        self.assertTrue(all(m.cloud_name == "" for m in got))
        self.assertTrue(all("赵小六" in m.note for m in got))

    def test_two_feishu_people_with_the_same_name_are_refused(self):
        feishu = {
            "zhang.wei@wuji.tech": "张伟（Wei Zhang）",
            "zhang.wei2@wuji.tech": "张伟（Wei Zhang）",
        }
        got = build([v("ZhangWei", "张伟")], domain=DOMAIN, feishu_names=feishu)
        self.assertEqual(got[0].source, SOURCE_NONE)
        self.assertIn("2 个人叫", got[0].note)

    def test_collision_never_leaks_someone_elses_email(self):
        """被拒的那条里不能带出任何一个候选邮箱——否则等于猜了一个。"""
        feishu = {
            "zhang.wei@wuji.tech": "张伟（Wei Zhang）",
            "zhang.wei2@wuji.tech": "张伟（Wei Zhang）",
        }
        got = build([v("ZhangWei", "张伟")], domain=DOMAIN, feishu_names=feishu)
        self.assertEqual(got[0].email, "")

    def test_confirmation_path_also_refuses_cloud_duplicates(self):
        """有邮箱的确认路径同样要查云侧重名，不能因为邮箱对上了就放过。"""
        feishu = {"zhao.xiaoliu@wuji.tech": "赵小六（Xiaoliu Zhao）"}
        got = build(
            [v("XiaoliuZhao", "赵小六", "zhao.xiaoliu@wuji.tech"), v("zhaoxl", "赵小六")],
            domain=DOMAIN,
            feishu_names=feishu,
        )
        m = next(x for x in got if "zhao.xiaoliu" in x.email or x.cloud_name == "XiaoliuZhao")
        self.assertNotEqual(m.source, SOURCE_DISPLAY_NAME)

    def test_collision_is_scoped_per_cloud(self):
        """阿里云和火山各有一个李四，是同一个人，不是冲突。"""
        got = build(
            [
                AccountUser("aliyun", "default", "lisi", "李四", ""),
                v("SiLi", "李四"),
            ],
            domain=DOMAIN,
            feishu_names=FEISHU,
        )
        self.assertEqual({m.source for m in got}, {SOURCE_DISPLAY_NAME})
