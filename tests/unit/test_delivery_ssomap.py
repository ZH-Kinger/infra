"""登录用映射提案。

这张表最终决定「以谁的身份进云控制台」，所以测试的重点不在覆盖率，而在：
只有一个证据时绝不能直接确认，任何歧义都不能被悄悄解决掉。
"""

from __future__ import annotations

import unittest

from delivery.identity.ssomap import (
    SOURCE_IAM_EMAIL,
    SOURCE_RAM_EMAIL,
    SOURCE_SECURITY_EMAIL,
    STATUS_BLOCKED,
    STATUS_CONFIRMED,
    STATUS_REVIEW,
    CloudAccount,
    EmailClaim,
    propose,
    render,
)

ALI = "aliyun/1000000000000001"
VOLC = "volcano/2000000001"
DOMAIN = "wuji.tech"


def sec(addr, verified=True):
    return EmailClaim(addr, SOURCE_SECURITY_EMAIL, verified)


def ram(addr):
    return EmailClaim(addr, SOURCE_RAM_EMAIL, False)


def iam(addr, verified=True):
    return EmailClaim(addr, SOURCE_IAM_EMAIL, verified)


def acc(scope, name, display="", *emails):
    return CloudAccount(scope, name, display, tuple(emails))


def link_of(p, scope, name):
    return next(x for x in p.links if x.scope == scope and x.name == name)


class TrustTests(unittest.TestCase):
    def test_ram_email_field_is_trusted(self):
        self.assertTrue(ram("a@wuji.tech").trusted)

    def test_pending_security_email_is_not_trusted(self):
        self.assertFalse(sec("a@wuji.tech", verified=False).trusted)

    def test_verified_security_email_is_trusted(self):
        self.assertTrue(sec("a@wuji.tech").trusted)


class EmailPassTests(unittest.TestCase):
    def test_trusted_email_plus_rule_is_confirmed(self):
        p = propose([acc(ALI, "lisi", "李四", sec("li.si@wuji.tech"))], domain=DOMAIN)
        self.assertEqual(p.links[0].status, STATUS_CONFIRMED)
        self.assertEqual(p.links[0].email, "li.si@wuji.tech")

    def test_unverified_email_needs_review_even_if_rule_matches(self):
        """安全邮箱 pending = 本人从没验证过，可能是别人替他填的。"""
        p = propose(
            [acc(ALI, "lisi", "李四", sec("li.si@wuji.tech", verified=False))],
            domain=DOMAIN,
        )
        self.assertEqual(p.links[0].status, STATUS_REVIEW)
        self.assertTrue(any("未经本人验证" in e for e in p.links[0].evidence))

    def test_trusted_email_but_username_not_derivable_needs_review(self):
        """典型情况：用户名只有名（xiaoqi），企业邮箱却是 姓.名（sun.xiaoqi@wuji.tech）。"""
        p = propose([acc(VOLC, "xiaoqi", "孙小七", iam("sun.xiaoqi@wuji.tech"))], domain=DOMAIN)
        self.assertEqual(p.links[0].status, STATUS_REVIEW)
        self.assertTrue(any("无法由邮箱推出" in e for e in p.links[0].evidence))

    def test_reversed_name_order_counts_as_derivable(self):
        p = propose([acc(VOLC, "SiLi", "李四", iam("li.si@wuji.tech"))], domain=DOMAIN)
        self.assertEqual(p.links[0].status, STATUS_CONFIRMED)

    def test_non_corporate_email_is_ignored(self):
        p = propose([acc(VOLC, "zhouba", "", iam("10000001@qq.com"))], domain=DOMAIN)
        self.assertEqual(p.links, ())
        self.assertEqual(len(p.unlinked), 1)

    def test_email_case_is_normalised(self):
        p = propose([acc(ALI, "lisi", "", sec("Li.Si@WUJI.tech"))], domain=DOMAIN)
        self.assertEqual(p.links[0].email, "li.si@wuji.tech")

    def test_two_different_corporate_emails_on_one_account_is_not_guessed(self):
        p = propose(
            [acc(ALI, "lisi", "", ram("li.si@wuji.tech"), sec("other.person@wuji.tech"))],
            domain=DOMAIN,
        )
        self.assertEqual(p.links, ())
        self.assertIn("多个不同的企业邮箱", p.unlinked[0].reason)

    def test_same_address_in_two_fields_uses_the_trusted_one(self):
        p = propose(
            [
                acc(
                    ALI,
                    "lisi",
                    "",
                    sec("li.si@wuji.tech", verified=False),
                    ram("li.si@wuji.tech"),
                )
            ],
            domain=DOMAIN,
        )
        self.assertEqual(p.links[0].status, STATUS_CONFIRMED)


class DisplayNamePassTests(unittest.TestCase):
    def test_display_name_plus_rule_plus_confirmed_sibling_is_confirmed(self):
        """火山 SiLi 没填企业邮箱，但显示名、用户名规则、阿里云已确认账号三者一致。"""
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech")),
                acc(VOLC, "SiLi", "李四"),
            ],
            domain=DOMAIN,
        )
        self.assertEqual(link_of(p, VOLC, "SiLi").status, STATUS_CONFIRMED)

    def test_display_name_alone_is_never_confirmed(self):
        """乐读 yue 还是 le——用户名推不出时，显示名是唯一证据，必须人工确认。"""
        p = propose(
            [
                acc(ALI, "zhoule", "周乐", sec("zhou.le@wuji.tech")),
                acc(VOLC, "YueZhou", "周乐"),
            ],
            domain=DOMAIN,
        )
        self.assertEqual(link_of(p, VOLC, "YueZhou").status, STATUS_REVIEW)

    def test_sibling_that_is_itself_unconfirmed_does_not_corroborate(self):
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech", verified=False)),
                acc(VOLC, "SiLi", "李四"),
            ],
            domain=DOMAIN,
        )
        self.assertEqual(link_of(p, VOLC, "SiLi").status, STATUS_REVIEW)

    def test_display_name_shared_by_two_people_is_not_linked(self):
        p = propose(
            [
                acc(ALI, "zhangwei", "张伟", sec("zhang.wei@wuji.tech")),
                acc(ALI, "zhangwei2", "张伟", sec("zhang.wei2@wuji.tech")),
                acc(VOLC, "WeiZhang", "张伟"),
            ],
            domain=DOMAIN,
        )
        self.assertFalse(any(x.scope == VOLC for x in p.links))
        self.assertTrue(any("对应多个人" in u.reason for u in p.unlinked))

    def test_username_only_fallback_needs_review(self):
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech")),
                acc(VOLC, "SiLi", "不一样的名字"),
            ],
            domain=DOMAIN,
        )
        m = link_of(p, VOLC, "SiLi")
        self.assertEqual(m.status, STATUS_REVIEW)
        self.assertTrue(any("只有一个证据" in e for e in m.evidence))

    def test_nothing_matches_is_unlinked_with_reason(self):
        p = propose([acc(VOLC, "someone", "某某")], domain=DOMAIN)
        self.assertEqual(p.links, ())
        self.assertIn("找不到对应的人", p.unlinked[0].reason)


class BlockingTests(unittest.TestCase):
    def test_two_accounts_for_one_person_in_one_cloud_are_both_blocked(self):
        """典型情况：同一个人在火山上有缩写号和全拼号两个号，其中一个是超管。
        登录时无法确定用哪个，挑错就是给错人超管权限。"""
        p = propose(
            [
                acc(ALI, "zhaoxiaoliu", "赵小六", sec("zhao.xiaoliu@wuji.tech")),
                acc(VOLC, "XiaoliuZhao", "赵小六"),
                acc(VOLC, "zhaoxl", "赵小六", iam("zhaoxl.home@gmail.com", verified=False)),
            ],
            domain=DOMAIN,
        )
        volc = [x for x in p.links if x.scope == VOLC]
        self.assertEqual(len(volc), 2)
        self.assertTrue(all(x.status == STATUS_BLOCKED for x in volc))
        self.assertEqual(link_of(p, ALI, "zhaoxiaoliu").status, STATUS_CONFIRMED)

    def test_same_person_in_two_clouds_is_not_a_conflict(self):
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech")),
                acc(VOLC, "SiLi", "李四", iam("li.si@wuji.tech")),
            ],
            domain=DOMAIN,
        )
        self.assertEqual({x.status for x in p.links}, {STATUS_CONFIRMED})

    def test_squatter_account_holding_someones_email_blocks_both(self):
        """一个叫 test 的账号挂着别人的企业邮箱。"""
        p = propose(
            [
                acc(VOLC, "test", "test", iam("li.si@wuji.tech", verified=False)),
                acc(VOLC, "SiLi", "李四"),
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech")),
            ],
            domain=DOMAIN,
        )
        volc = [x for x in p.links if x.scope == VOLC]
        self.assertTrue(volc)
        self.assertTrue(all(x.status == STATUS_BLOCKED for x in volc))


class ServiceAndOutputTests(unittest.TestCase):
    def test_service_accounts_are_set_aside(self):
        p = propose(
            [acc(ALI, "tempak-vendor-000001"), acc(ALI, "shared-app-user")],
            domain=DOMAIN,
            service_names=["shared-app-user"],
        )
        self.assertEqual(p.links, ())
        self.assertEqual(len(p.services), 2)

    def test_to_dict_counts(self):
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech")),
                acc(VOLC, "xiaoqi", "孙小七", iam("sun.xiaoqi@wuji.tech")),
            ],
            domain=DOMAIN,
        )
        d = p.to_dict()
        self.assertEqual(d["counts"]["confirmed"], 1)
        self.assertEqual(d["counts"]["review"], 1)
        self.assertEqual(d["schema"], "wuji-sso-map/proposal@1")

    def test_render_puts_blocked_and_review_first_and_hides_confirmed(self):
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech")),
                acc(VOLC, "xiaoqi", "孙小七", iam("sun.xiaoqi@wuji.tech")),
            ],
            domain=DOMAIN,
        )
        text = render(p)
        self.assertIn("需确认", text)
        self.assertIn("xiaoqi", text)
        self.assertNotIn("lisi", text)
        self.assertIn("lisi", render(p, show_confirmed=True))


if __name__ == "__main__":
    unittest.main()


class RelaxedPolicyTests(unittest.TestCase):
    """放宽策略：未验证的企业邮箱 + 用户名可由它推出，也直接确认。默认关闭。"""

    def test_default_is_strict(self):
        p = propose([acc(ALI, "lisi", "", sec("li.si@wuji.tech", verified=False))], domain=DOMAIN)
        self.assertEqual(p.links[0].status, STATUS_REVIEW)

    def test_relaxed_confirms_unverified_when_derivable(self):
        p = propose(
            [acc(ALI, "lisi", "", sec("li.si@wuji.tech", verified=False))],
            domain=DOMAIN,
            trust_unverified_when_derivable=True,
        )
        self.assertEqual(p.links[0].status, STATUS_CONFIRMED)
        self.assertTrue(any("策略确认" in e for e in p.links[0].evidence))

    def test_relaxed_does_not_confirm_when_username_not_derivable(self):
        """放宽的只是「邮箱未验证」这一条，用户名推不出仍然要人看。"""
        p = propose(
            [acc(VOLC, "xiaoqi", "", iam("sun.xiaoqi@wuji.tech", verified=False))],
            domain=DOMAIN,
            trust_unverified_when_derivable=True,
        )
        self.assertEqual(p.links[0].status, STATUS_REVIEW)

    def test_relaxed_still_blocks_duplicates(self):
        p = propose(
            [
                acc(ALI, "lisi", "李四", sec("li.si@wuji.tech", verified=False)),
                acc(ALI, "sili", "李四", sec("li.si@wuji.tech", verified=False)),
            ],
            domain=DOMAIN,
            trust_unverified_when_derivable=True,
        )
        self.assertTrue(all(x.status == STATUS_BLOCKED for x in p.links))
