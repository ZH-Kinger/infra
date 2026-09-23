"""体检的「认不出属主」那一栏：名册里**待确认**的归属算不算数（`hygiene.build`）。

名册对一个云账号有三种说法，处置完全不同：

  · `accounts`（已确认）—— 管理员点过头，就是他的；
  · `pending` 里 `status == "review"`（推断待确认）—— 名册自己按邮箱/姓名推出来的，
    **等管理员确认**。名册刚跑完、还没人点的那段时间里，每个新号都是这个状态；
  · `pending` 里 `status == "shared"` —— 名册发现同一个号被确认给了**多个人**，
    自己判定自己错了才降级的。它**明确拒绝**认定属主。

三种混成一种的后果都不显眼：
  · review 不算数 → 名册跑完的第二天，体检页把一批刚推断出主人的号报成「无主」，
    而人员页上它们明明有主人，两页对不上，没有任何地方解释；
  · shared 也算数 → 面板替名册做了它拒绝做的断言，还是**按排序随机挑一个人**署名
    —— 出事时照着这个名字去找人，找的是错的人；
  · 填表顺序反了 → A 的「待确认」盖过 B 的「已确认」，属主看名册里谁排在前面。

数据虚构，不碰网络。
"""

from __future__ import annotations

import unittest

from delivery import hygiene, inventory
from delivery.people import AccountRef, Person

ACC = "1000000000000001"
NOW = 1_800_000_000.0


def snapshot(*names):
    return inventory.parse(
        {
            "captured_at": "2026-09-23T00:00:00+08:00",
            "accounts": [
                {
                    "platform": "aliyun",
                    "account": ACC,
                    "groups": [],
                    "users": [{"name": n, "display_name": n, "policies": []} for n in names],
                }
            ],
        }
    )


def ref(name, status="confirmed"):
    return AccountRef("aliyun", ACC, name, status)


def person(name, uid, *, accounts=(), pending=()):
    return Person(
        name=name,
        email=f"{uid}@wuji.tech",
        union_id=uid,
        accounts=tuple(accounts),
        pending=tuple(pending),
    )


class Base(unittest.TestCase):
    def report(self, snap, people):
        return hygiene.build(snap, people, now=NOW)

    def orphans(self, snap, people):
        return sorted(f.subject for f in self.report(snap, people).orphan)


class PendingOwnerTests(Base):
    def test_a_review_pending_account_counts_as_owned(self):
        """名册刚推断完、管理员还没点确认的那段时间里，每个新号都是这个状态。
        不算的话，**名册跑得越勤，体检页上的「无主」越多** —— 正好反了。"""
        people = [person("李四", "on_l", pending=[ref("lisi", "review")])]
        self.assertEqual(self.orphans(snapshot("lisi"), people), [])

    def test_a_shared_pending_account_stays_an_orphan(self):
        """`shared` 是名册**自己判定自己错了**才降级的：同一个号被确认给了多个人。
        它明确拒绝认定属主，面板不该替它断言 —— 断言出来的那个名字是按排序挑的，
        出事时照着找人会找错人。留在「无主」里，正是让人去把它分清楚。"""
        people = [
            person("李四", "on_l", pending=[ref("shareduser", "shared")]),
            person("王五", "on_w", pending=[ref("shareduser", "shared")]),
        ]
        self.assertEqual(self.orphans(snapshot("shareduser"), people), ["shareduser"])

    def test_an_unknown_pending_status_is_not_trusted(self):
        """将来名册多出一种状态（比如 `rejected`）时，默认**不认**。
        默认认下来的话，新状态一上线就悄悄把一批号从无主清单里抹掉了。"""
        people = [person("李四", "on_l", pending=[ref("mystery", "rejected")])]
        self.assertEqual(self.orphans(snapshot("mystery"), people), ["mystery"])

    def test_a_confirmed_owner_beats_another_persons_pending(self):
        """**填表顺序**：已确认的先填、待确认的只 setdefault。

        反过来的话，属主取决于名册里谁排在前面 —— 那是按文件里的行序，等于随机。
        这里把「待确认的那个人」排在名册前面，正是要盯住顺序。
        """
        guess = person("王五", "on_w", pending=[ref("lisi", "review")])
        owner = person("李四", "on_l", accounts=[ref("lisi")])
        report = self.report(snapshot("lisi"), [guess, owner])
        self.assertEqual(report.orphan, [])
        # 属主要落在已确认的那个人头上。体检页上这个名字会被拿去「提醒本人」
        rotate_and_unused = [*report.rotate, *report.unused]
        owners = {f.owner for f in rotate_and_unused}
        self.assertNotIn("王五 on_w@wuji.tech", owners)

    def test_the_owner_shown_is_the_confirmed_one_even_with_keys(self):
        """带 AK 的号才看得到 owner 那一栏（无主那栏没有属主）。
        用一把老 AK 把属主逼出来，直接断言它是谁。"""
        snap = inventory.parse(
            {
                "captured_at": "2026-09-23T00:00:00+08:00",
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": ACC,
                        "groups": [],
                        "users": [
                            {
                                "name": "lisi",
                                "policies": [],
                                "keys": [
                                    {
                                        "id": "AK1",
                                        "status": "Active",
                                        "created": "2020-01-01T00:00:00Z",
                                        "last_used": "2020-01-02T00:00:00Z",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        guess = person("王五", "on_w", pending=[ref("lisi", "review")])
        owner = person("李四", "on_l", accounts=[ref("lisi")])
        report = hygiene.build(snap, [guess, owner], now=NOW)
        found = [*report.rotate, *report.unused]
        self.assertTrue(found, "180 天以上的 AK 该被列出来")
        for finding in found:
            self.assertEqual(finding.owner, "李四 on_l@wuji.tech")
            self.assertEqual(finding.owner_uid, "on_l")

    def test_a_review_pending_owner_is_named_when_nobody_confirmed(self):
        """没有已确认的人时，待确认的那个名字要出现 —— 否则「有主」和「说得出是谁」
        两件事对不上，人员页显示了主人、体检页却空着。"""
        snap = inventory.parse(
            {
                "captured_at": "2026-09-23T00:00:00+08:00",
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": ACC,
                        "groups": [],
                        "users": [
                            {
                                "name": "lisi",
                                "policies": [],
                                "keys": [
                                    {
                                        "id": "AK1",
                                        "status": "Active",
                                        "created": "2020-01-01T00:00:00Z",
                                        "last_used": "2020-01-02T00:00:00Z",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        people = [person("王五", "on_w", pending=[ref("lisi", "review")])]
        found = [*hygiene.build(snap, people, now=NOW).rotate]
        self.assertTrue(found)
        self.assertEqual(found[0].owner, "王五 on_w@wuji.tech")

    def test_a_person_with_no_pending_attribute_does_not_crash(self):
        """名册的 Person 是从文件读出来的，字段可能缺。整份体检不该因此挂掉 ——
        体检是管理员发现问题的唯一入口，它挂了等于什么问题都看不见。"""

        class Bare:
            name, email, union_id, key = "赵六", "z@wuji.tech", "on_z", "on_z"
            accounts = ()

        self.assertEqual(self.orphans(snapshot("zhaoliu"), [Bare()]), ["zhaoliu"])


class ProgramAccountTests(Base):
    def test_a_program_issued_account_is_never_an_orphan(self):
        """和 `views.unlinked_rows` 同一个判据（`hygiene.is_program_account`）。
        两页各写一份的话，同一个号在体检页和人员页给出两个结论。"""
        from delivery import grants

        names = [f"{p}alice-9a1b2c" for p in grants.ISSUED_PREFIXES]
        self.assertEqual(self.orphans(snapshot(*names), []), [])

    def test_a_registered_service_name_is_never_an_orphan(self):
        report = hygiene.build(snapshot("mes-sync"), [], services=["mes-sync"], now=NOW)
        self.assertEqual(report.orphan, [])


if __name__ == "__main__":
    unittest.main()
