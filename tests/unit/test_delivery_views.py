"""看板的两个视角。

这里的核心不变量只有一条：**用户视图里不能出现别人的数据**。它不能靠「页面上没渲染」
来保证——只要数据结构里带着，将来任何一个 JSON 接口、任何一次模板改动都会把它漏出去。
所以断言打在接口返回的数据结构上，而不是页面上。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from delivery.inventory import InventoryError, is_high_risk, load, parse
from delivery.people import parse as people_parse
from delivery.roles import ROLE_USER, Admins, RoleError, load_admins
from delivery.views import Labels, admin_overview, admin_people, person_detail

SNAP = {
    "captured_at": "2026-09-14T15:00:00+08:00",
    "accounts": [
        {
            "platform": "aliyun",
            "account": "default",
            "users": [
                {
                    "name": "lisi",
                    "display_name": "李四",
                    "email": "li.si@wuji.tech",
                    "policies": ["AdministratorAccess", "AliyunOSSFullAccess"],
                    "groups": ["team-alpha"],
                },
                {
                    "name": "wangwu",
                    "display_name": "王五",
                    "email": "wang.wu@wuji.tech",
                    "policies": [],
                    "groups": ["team-alpha"],
                },
                {"name": "Data-sync", "display_name": "数据同步", "policies": []},
            ],
            "groups": [
                {
                    "name": "team-alpha",
                    "display_name": "算法组",
                    "policies": ["AliyunOSSFullAccess", "AliyunECSFullAccess"],
                    "members": ["lisi", "wangwu"],
                },
            ],
        },
        {
            "platform": "volcano",
            "account": "default",
            "users": [
                {
                    "name": "SiLi",
                    "display_name": "李四",
                    "email": "li.si@wuji.tech",
                    "policies": ["IAMFullAccess"],
                    "groups": ["ops-admins"],
                },
            ],
            "groups": [
                {
                    "name": "ops-admins",
                    "display_name": "infra",
                    "policies": ["AdministratorAccess"],
                    "members": ["SiLi"],
                },
            ],
        },
    ],
}


class SnapshotTests(unittest.TestCase):
    def test_parse_indexes_users_and_groups(self):
        s = parse(SNAP)
        self.assertEqual(len(s.users), 4)
        self.assertEqual(len(s.groups), 2)
        self.assertTrue(s.complete)

    def test_captured_at_is_mandatory(self):
        """没有采集时间的快照不能用——看的人无从判断它是一小时前还是三个月前的。"""
        bad = {k: v for k, v in SNAP.items() if k != "captured_at"}
        with self.assertRaises(InventoryError) as ctx:
            parse(bad)
        self.assertIn("captured_at", str(ctx.exception))

    def test_account_level_error_marks_snapshot_incomplete(self):
        """采集失败的账号必须留痕，否则「这人没权限」和「这个账号没采到」长得一样。"""
        s = parse(
            {
                "captured_at": "t",
                "accounts": [{"platform": "volcano", "account": "default", "error": "403"}],
            }
        )
        self.assertFalse(s.complete)
        self.assertIn("volcano/default：403", s.incomplete[0])

    def test_effective_policies_merge_user_and_group(self):
        s = parse(SNAP)
        u = next(x for x in s.users if x.name == "lisi")
        eff = s.effective_policies(u)
        self.assertIn("AdministratorAccess", eff)  # 用户级
        self.assertIn("AliyunECSFullAccess", eff)  # 组级
        self.assertEqual(len(eff), len(set(eff)))  # 去重

    def test_group_membership_matches_either_direction(self):
        """用户记了组、或组记了成员，任一边成立都算——两边数据不同步是常态。"""
        s = parse(
            {
                "captured_at": "t",
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": "default",
                        "users": [{"name": "a", "policies": [], "groups": []}],
                        "groups": [{"name": "g", "policies": ["X"], "members": ["a"]}],
                    }
                ],
            }
        )
        u = s.users[0]
        self.assertEqual(s.effective_policies(u), ("X",))

    def test_high_risk_markers(self):
        self.assertTrue(is_high_risk("AdministratorAccess"))
        self.assertTrue(is_high_risk("AliyunRAMFullAccess"))
        self.assertTrue(is_high_risk("IAMFullAccess"))

    def test_business_full_access_is_not_high_risk(self):
        """把业务全权也标红，红色就没意义了，运维会学会忽略它。"""
        self.assertFalse(is_high_risk("AliyunOSSFullAccess"))
        self.assertFalse(is_high_risk("ECSFullAccess"))

    def test_user_lookup_is_exact(self):
        """按平台+账号+用户名精确取；不提供按邮箱取（邮箱不是稳定身份）。"""
        s = parse(SNAP)
        self.assertEqual(s.user("aliyun", "default", "lisi").display_name, "李四")
        self.assertIsNone(s.user("aliyun", "default", "LiSi"))
        self.assertIsNone(s.user("volcano", "default", "lisi"))
        self.assertFalse(hasattr(s, "users_for_email"))

    def test_load_reports_bad_json_clearly(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "s.json"
        p.write_text("{", encoding="utf-8")
        with self.assertRaises(InventoryError):
            load(str(p))


PEOPLE = {
    "schema": "wuji-people@1",
    "people": [
        {
            "union_id": "on_ls",
            "name": "李四",
            "email": "li.si@wuji.tech",
            "accounts": [
                {"platform": "aliyun", "account": "default", "name": "lisi"},
                {"platform": "volcano", "account": "default", "name": "SiLi"},
            ],
            "pending": [
                {"platform": "volcano", "account": "default", "name": "ls-alt", "status": "review"}
            ],
        },
        {
            "union_id": "on_ww",
            "name": "王五",
            "email": "wang.wu@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": "default", "name": "wangwu"}],
        },
        {
            "union_id": "",
            "name": "未绑定",
            "email": "wei.bang@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": "default", "name": "gone"}],
        },
        {"union_id": "on_none", "name": "无账号", "email": "wu.zhanghao@wuji.tech"},
    ],
    "unlinked": [
        {
            "platform": "aliyun",
            "account": "default",
            "name": "Data-sync",
            "kind": "service",
            "reason": "服务号",
        }
    ],
}
LABELS = Labels({"aliyun": "阿里云", "volcano": "火山引擎"}, {"aliyun/default": "阿里云主账号"})


class PersonDetailBoundaryTests(unittest.TestCase):
    """核心安全不变量：用户视图里只能有他自己的数据。"""

    def setUp(self):
        self.snap = parse(SNAP)
        self.index = people_parse(PEOPLE)

    def _me(self, uid):
        found = self.index.resolve(union_id=uid)
        return person_detail(found.person, self.snap, LABELS, binding=found.binding)

    def test_only_my_accounts_are_returned(self):
        v = self._me("on_ls")
        self.assertEqual(
            sorted((a["platform"], a["name"]) for a in v["accounts"]),
            [("aliyun", "lisi"), ("volcano", "SiLi")],
        )

    def test_cloud_registered_email_is_shown_on_the_card(self):
        """云上这个号自己登记的邮箱只作展示：认人不看它（名册才认人）。"""
        card = {a["name"]: a for a in self._me("on_ls")["accounts"]}["lisi"]
        self.assertEqual(card["email"], "li.si@wuji.tech")

    def test_nobody_elses_name_appears_anywhere_in_the_payload(self):
        """断在整个 JSON 串上：任何字段、任何嵌套里都不能带出别人。"""
        blob = json.dumps(self._me("on_ls"), ensure_ascii=False)
        for other in ("wangwu", "王五", "Data-sync", "wei.bang"):
            self.assertNotIn(other, blob)

    def test_pending_mappings_are_never_shown_to_the_person(self):
        """待确认的对应可能是错的——给本人看等于可能把别人的账号给他看。"""
        v = self._me("on_ls")
        self.assertEqual(v["pending"], [])
        self.assertNotIn("ls-alt", json.dumps(v))

    def test_admin_detail_includes_pending(self):
        p = self.index.by_key("on_ls")
        v = person_detail(p, self.snap, LABELS, binding="union_id", include_pending=True)
        self.assertEqual([x["name"] for x in v["pending"]], ["ls-alt"])

    def test_unknown_union_id_gets_nothing(self):
        found = self.index.resolve(union_id="on_stranger")
        v = person_detail(
            found.person,
            self.snap,
            LABELS,
            binding=found.binding,
            note=found.note,
            fallback={"name": "路人"},
        )
        self.assertEqual(v["accounts"], [])
        self.assertEqual(v["person"]["name"], "路人")
        self.assertEqual(v["binding"], "unbound")

    def test_email_alone_never_identifies_anyone(self):
        """规范：邮箱只能辅助首次关联，而且只对未绑定的人。已绑定的人拿邮箱冒领 → 冲突。"""
        found = self.index.resolve(union_id="on_attacker", enterprise_email="li.si@wuji.tech")
        self.assertEqual(found.binding, "conflict")
        self.assertIsNone(found.person)

    def test_person_without_cloud_accounts_is_a_normal_empty_state(self):
        v = self._me("on_none")
        self.assertEqual(v["binding"], "union_id")
        self.assertEqual(v["accounts"], [])
        self.assertEqual(v["summary"]["account_count"], 0)

    def test_direct_and_group_policies_are_shown_separately(self):
        card = next(a for a in self._me("on_ls")["accounts"] if a["platform"] == "aliyun")
        self.assertEqual(card["direct_policies"], ["AdministratorAccess", "AliyunOSSFullAccess"])
        self.assertEqual(card["group_policies"][0]["group"], "team-alpha")
        self.assertIn("AliyunECSFullAccess", card["effective_policies"])

    def test_high_risk_includes_those_inherited_from_a_group(self):
        card = next(a for a in self._me("on_ls")["accounts"] if a["platform"] == "volcano")
        self.assertIn("IAMFullAccess", card["high_risk"])
        self.assertIn("AdministratorAccess（经组 ops-admins）", card["high_risk"])

    def test_mapped_account_missing_from_snapshot_is_marked(self):
        """映射表有、快照没有：账号被删了或没采到。不能显示成「有账号但零权限」。"""
        p = self.index.by_key("email:wei.bang@wuji.tech")
        v = person_detail(p, self.snap, LABELS, binding="union_id")
        self.assertFalse(v["accounts"][0]["in_snapshot"])

    def test_captured_at_and_incomplete_are_carried_through(self):
        v = self._me("on_ls")
        self.assertEqual(v["captured_at"], "2026-09-14T15:00:00+08:00")
        self.assertEqual(v["snapshot_incomplete"], [])

    def test_no_snapshot_still_lists_accounts_without_permissions(self):
        found = self.index.resolve(union_id="on_ww")
        v = person_detail(found.person, None, LABELS, binding=found.binding)
        self.assertEqual(v["captured_at"], "")
        self.assertEqual(v["accounts"][0]["effective_policies"], [])

    def test_account_label_falls_back_to_platform_and_id(self):
        card = next(a for a in self._me("on_ls")["accounts"] if a["platform"] == "volcano")
        self.assertEqual(card["account_label"], "火山引擎 default")


class AdminViewTests(unittest.TestCase):
    def setUp(self):
        self.snap = parse(SNAP)
        self.index = people_parse(PEOPLE)

    def test_totals(self):
        t = admin_overview(self.snap, self.index, LABELS)["totals"]
        self.assertEqual(t["people"], 3)
        self.assertEqual(t["no_account_people"], 1)
        self.assertEqual(t["multi_account_people"], 1)
        self.assertEqual(t["unbound_people"], 1)
        self.assertEqual(t["cloud_users"], 4)
        self.assertEqual(t["unlinked_accounts"], 1)

    def test_counts_per_account(self):
        rows = {
            (r["platform"], r["account"]): r
            for r in admin_overview(self.snap, self.index, LABELS)["accounts"]
        }
        self.assertEqual(rows[("aliyun", "default")]["users"], 3)
        self.assertEqual(rows[("aliyun", "default")]["high_risk_users"], 1)
        self.assertEqual(rows[("aliyun", "default")]["account_label"], "阿里云主账号")

    def test_filters(self):
        def names(f):
            rows = admin_people(self.snap, self.index, LABELS, filter=f)["people"]
            return [r["name"] for r in rows]

        self.assertNotIn("无账号", names("all"))
        self.assertEqual(names("no_account"), ["无账号"])
        self.assertEqual(names("multi"), ["李四"])
        self.assertEqual(names("unbound"), ["未绑定"])
        self.assertEqual(names("high_risk"), ["李四"])

    def test_unknown_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            admin_people(self.snap, self.index, LABELS, filter="everyone")

    def test_high_risk_people_sort_first(self):
        rows = admin_people(self.snap, self.index, LABELS)["people"]
        self.assertEqual(rows[0]["name"], "李四")

    def test_unbound_person_is_keyed_by_email(self):
        rows = admin_people(self.snap, self.index, LABELS, filter="unbound")["people"]
        self.assertEqual(rows[0]["key"], "email:wei.bang@wuji.tech")

    def test_same_account_duplicates_are_flagged(self):
        index = people_parse(
            {
                "schema": "wuji-people@1",
                "people": [
                    {
                        "union_id": "on_z",
                        "name": "赵",
                        "email": "z@wuji.tech",
                        "accounts": [
                            {"platform": "volcano", "account": "default", "name": "zhaoxl"},
                            {"platform": "volcano", "account": "default", "name": "XiaoliuZhao"},
                        ],
                    }
                ],
            }
        )
        row = admin_people(None, index, LABELS)["people"][0]
        self.assertEqual(row["same_account_duplicates"], ["volcano/default"])

    def test_unlinked_accounts_come_from_snapshot_and_carry_kind(self):
        rows = admin_people(self.snap, self.index, LABELS)["unlinked_accounts"]
        self.assertEqual([(r["name"], r["kind"]) for r in rows], [("Data-sync", "service")])

    def test_account_created_after_roster_shows_as_unknown(self):
        snap = parse(
            {
                "captured_at": "t",
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": "default",
                        "users": [{"name": "newbie", "policies": ["AdministratorAccess"]}],
                    }
                ],
            }
        )
        rows = admin_people(snap, self.index, LABELS)["unlinked_accounts"]
        self.assertEqual(rows[0]["kind"], "unknown")
        self.assertEqual(rows[0]["high_risk"], ["AdministratorAccess"])


class RoleTests(unittest.TestCase):
    def _write(self, payload):
        d = tempfile.mkdtemp()
        p = Path(d) / "admins.json"
        p.write_text(payload, encoding="utf-8")
        return str(p)

    def test_empty_list_means_nobody_is_admin(self):
        """fail-closed：配错方向的代价不对称。"""
        a = Admins()
        self.assertEqual(a.role_of(email="anyone@wuji.tech"), ROLE_USER)
        self.assertFalse(a.configured)

    def test_missing_file_is_not_an_error_but_grants_nobody(self):
        a = load_admins("/nonexistent/admins.json")
        self.assertFalse(a.configured)
        self.assertFalse(a.is_admin(email="li.si@wuji.tech"))

    def test_malformed_file_is_an_error(self):
        """想配但配歪了，静默当空名单会让人以为配上了。"""
        with self.assertRaises(RoleError):
            load_admins(self._write("{"))

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(RoleError) as ctx:
            load_admins(self._write(json.dumps({"admins": ["a@wuji.tech"]})))
        self.assertIn("emails", str(ctx.exception))

    def test_email_match_is_case_insensitive(self):
        a = load_admins(self._write(json.dumps({"emails": ["Li.Si@Wuji.Tech"]})))
        self.assertTrue(a.is_admin(email="li.si@WUJI.tech"))

    def test_union_id_match_is_case_sensitive(self):
        a = load_admins(self._write(json.dumps({"union_ids": ["on_ABC"]})))
        self.assertTrue(a.is_admin(union_id="on_ABC"))
        self.assertFalse(a.is_admin(union_id="on_abc"))

    def test_blank_entry_is_rejected(self):
        """空串会匹配上「没有邮箱的登录者」，等于把管理员权限发给任何人。"""
        with self.assertRaises(RoleError):
            load_admins(self._write(json.dumps({"emails": ["  "]})))

    def test_empty_email_never_matches(self):
        a = Admins(emails=frozenset({"a@wuji.tech"}))
        self.assertFalse(a.is_admin(email=""))
        self.assertFalse(a.is_admin(union_id=""))


if __name__ == "__main__":
    unittest.main()
