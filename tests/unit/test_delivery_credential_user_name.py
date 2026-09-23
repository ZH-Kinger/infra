"""发出去的那个子账号**在 RAM 控制台上叫什么**（端到端，不是只测 `grants.user_name`）。

为什么要端到端测
────────────────
`grants.user_name(subject, email=)` 本身是对的（见 `test_delivery_credential_prefixes.py`）。
但它是否拿得到邮箱，取决于 `flows._issue_credential` 从申请单的哪个字段读 ——
**单测那一层永远发现不了读错字段**。而读错字段的后果正是这次改动要消灭的那个：
控制台上一排 `staff-x090fd8a0-…`，运维认不出是谁的号，要撤销时只能一个个点开看。

名字不是装饰：撤销、到期清理、离职保护、审计对账都按它走。

飞书、云、审批全部替换，时钟固定。
"""

from __future__ import annotations

import unittest

from delivery import grants

from . import test_delivery_access_requests as base
from .test_delivery_credentials import BUCKET, Env

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

#: 超过 12 小时 → 走长期（建真的 RAM 子账号），才会有子账号名
LONG = {"bucket": BUCKET, "prefix": "batch/", "hours": 24}


class Base(unittest.TestCase):
    def issued(self, **payload) -> str:
        env = Env()
        done = env.run(payload={**LONG, **payload})
        self.assertEqual(done["status"], "done", done.get("result"))
        self.assertEqual([a[0] for a in env.issuer.actions], ["issue"])
        return done["cred_user"]


class PrefixTests(Base):
    def test_a_panel_issued_account_carries_the_internal_prefix(self):
        """面板发给同事的 = 内部。用外部那套前缀的话，出事时第一个问题
        「这把钥匙在公司里还是公司外」就答错了。"""
        name = self.issued()
        self.assertTrue(name.startswith(grants.USER_PREFIX), name)
        self.assertFalse(name.startswith(grants.EXTERNAL_USER_PREFIX), name)

    def test_the_issued_account_has_a_derivable_policy_name(self):
        """撤销时要按这个名字去删那条策略。`policy_name` 不认 = 策略永远删不掉，
        而策略里写着桶和目录。"""
        name = self.issued()
        self.assertEqual(grants.policy_name(name), grants.POLICY_PREFIX + name)

    def test_the_issued_account_is_protected_from_offboarding(self):
        from delivery import offboard

        self.assertTrue(offboard.protected(self.issued()))

    def test_the_display_name_says_it_is_internal(self):
        """RAM 控制台上只有显示名这一栏能一眼分出内外 —— 前缀给机器看，显示名给人看。"""
        env = Env()
        env.run(payload=dict(LONG))
        _, _user, display = next(a for a in env.issuer.actions if a[0] == "issue")
        self.assertIn("内部", display)
        # **不能带括号**：火山的 IAM 拒绝显示名里的括号（真机试出来的），
        # 而火山每张凭证都走这条路
        self.assertNotIn("（", display)
        self.assertNotIn("(", display)
        self.assertIn("李四", display, "还要认得出是谁申请的")


class ReadableNameTests(Base):
    """名字要能让运维在控制台上认出是谁的号。"""

    def test_the_login_name_uses_the_applicant_email_not_a_hash(self):
        """**缺陷（本轮改动没生效）**：`src/delivery/flows.py:2778` 读的是
        `ticket.get("email")`，而申请单里根本没有这个顶层字段 —— 邮箱在
        `ticket["applicant"]["email"]`（`flows.py:99` 的 `_applicant_dict`）。

        于是 `email=` 永远是空串，`user_name` 每次都回落到主体名的哈希：
        申请人叫「李四」时云上建出来的是 `staff-xb1ffeb52-…`，正是这次要修掉的那个样子。

        期望：`staff-li-si-<6hex>`（申请人邮箱 `li.si@wuji.tech`）。
        实际：`staff-xb1ffeb52-<6hex>`。
        修法：改成 `str((ticket.get("applicant") or {}).get("email") or "")`。
        """
        name = self.issued()
        self.assertTrue(name.startswith("staff-li-si-"), name)

    def test_the_email_wins_over_the_subject(self):
        """邮箱优先：同一个人无论这次申请写的主体名是什么，云上都是同一个可读的名字。
        主体名可中可英、可能每次写法都不一样，邮箱不会。"""
        self.assertTrue(self.issued(subject="Yuanke Ltd").startswith("staff-li-si-"))

    def test_without_an_email_it_falls_back_to_the_subject(self):
        """没有邮箱时才用主体名 —— 拉丁字母就直接用，取不出字母才退到哈希。"""
        from delivery.grants import user_name

        self.assertTrue(user_name("Yuanke Ltd", rand="aabbcc").startswith("staff-yuanke-ltd-"))
        self.assertTrue(user_name("元客", rand="aabbcc").startswith("staff-x"))


if __name__ == "__main__":
    unittest.main()
