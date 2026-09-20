"""权限快照采集。

重点：授在资源组 / 项目上的权限不能丢范围、响应缺字段不能当「没有权限」、
单个云账号失败只能让它自己变成 error，且 error 必须在看板上显示成「快照不完整」。

全部离线：假 transport 按 Action 分派。数据虚构。
"""

from __future__ import annotations

import unittest
import urllib.parse

from delivery import inventory
from delivery.clouds import aliyun, volcano
from delivery.errors import DeliveryError
from delivery.inventory_collect import build_snapshot, collect_aliyun, collect_volcano

UID = "1000000000000001"
CREDS = aliyun.Credentials("LTAItest", "secret")
VCREDS = volcano.Credentials("AKLTtest", "secret")


def _query(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


# ── 阿里云 ────────────────────────────────────────────────────────────────


def att(ptype, pname, policy, rg=UID):
    return {
        "PrincipalType": ptype,
        "PrincipalName": pname,
        "PolicyName": policy,
        "PolicyType": "System",
        "ResourceGroupId": rg,
    }


class AliyunFake:
    def __init__(
        self, *, users, groups, members, attachments, page_size=100, overrides=None, keys=None
    ):
        self.users = users
        self.groups = groups
        self.members = members
        self.attachments = attachments
        #: {用户名: [AK, …]}。没给的人当作一把都没有 —— 注意那和「没采到」是两件事，
        #: 后者由 overrides 里让 ListAccessKeys 回 403 来模拟
        self.keys = keys or {}
        self.page_size = page_size
        self.overrides = overrides or {}
        self.calls = []

    def __call__(self, url):
        q = _query(url)
        action = q["Action"]
        self.calls.append((action, q))
        if action in self.overrides:
            return self.overrides[action](q)
        if action == "GetCallerIdentity":
            return 200, {"AccountId": UID}
        if action == "ListUsers":
            return 200, {"Users": {"User": self.users}, "IsTruncated": False}
        if action == "ListGroups":
            return 200, {"Groups": {"Group": self.groups}, "IsTruncated": False}
        if action == "ListUsersForGroup":
            names = self.members.get(q["GroupName"], [])
            return 200, {"Users": {"User": [{"UserName": n} for n in names]}, "IsTruncated": False}
        if action == "ListAccessKeys":
            got = self.keys.get(q.get("UserName"), [])
            return 200, {"AccessKeys": {"AccessKey": got}, "IsTruncated": False}
        if action == "GetAccessKeyLastUsed":
            for k in self.keys.get(q.get("UserName"), []):
                if k.get("AccessKeyId") == q.get("UserAccessKeyId"):
                    return 200, {"AccessKeyLastUsed": {"LastUsedDate": k.get("LastUsedDate", "")}}
            return 200, {"AccessKeyLastUsed": {}}
        if action == "ListPolicyAttachments":
            page = int(q["PageNumber"])
            size = self.page_size
            chunk = self.attachments[(page - 1) * size : page * size]
            return 200, {
                "PolicyAttachments": {"PolicyAttachment": chunk},
                "TotalCount": len(self.attachments),
                "PageNumber": page,
            }
        raise AssertionError(action)


def aliyun_fixture(**kw):
    base = dict(
        users=[
            {"UserName": "alice", "DisplayName": "爱丽丝", "Email": "alice@wuji.tech"},
            {"UserName": "bob", "DisplayName": "鲍勃"},
        ],
        groups=[{"GroupName": "ops", "Comments": "运维组"}, {"GroupName": "empty"}],
        members={"ops": ["bob", "alice"]},
        attachments=[
            att("IMSUser", f"alice@{UID}.onaliyun.com", "AliyunOSSReadOnlyAccess"),
            att("IMSUser", f"alice@{UID}.onaliyun.com", "AliyunECSFullAccess", rg="rg-aek2abc"),
            att("IMSUser", f"alice@{UID}.onaliyun.com", "AliyunOSSReadOnlyAccess"),  # 重复
            att("IMSGroup", f"ops@group.{UID}.onaliyun.com", "AdministratorAccess"),
            att("IMSGroup", f"ops@group.{UID}.onaliyun.com", "AliyunPAIFullAccess", rg="rg-xyz"),
            att("ServiceRole", "AliyunServiceRoleForPAI", "AliyunPAIRolePolicy"),
            att("IMSUser", "bob", "AliyunRAMReadOnlyAccess", rg=""),
        ],
    )
    base.update(kw)
    return AliyunFake(**base)


class AliyunCollectTests(unittest.TestCase):
    def collect(self, fake):
        return collect_aliyun(CREDS, transport=fake)

    def test_shape_and_account(self):
        out = self.collect(aliyun_fixture())
        self.assertEqual(out["platform"], "aliyun")
        self.assertEqual(out["account"], UID)
        self.assertEqual([u["name"] for u in out["users"]], ["alice", "bob"])
        self.assertEqual(out["users"][0]["display_name"], "爱丽丝")
        # 云上登记的邮箱进快照（只作展示）；没登记的是空串，不是缺字段
        self.assertEqual([u["email"] for u in out["users"]], ["alice@wuji.tech", ""])

    def test_resource_group_scope_suffix_only_when_not_account_level(self):
        users = {u["name"]: u for u in self.collect(aliyun_fixture())["users"]}
        self.assertEqual(
            users["alice"]["policies"],
            ["AliyunECSFullAccess @资源组:rg-aek2abc", "AliyunOSSReadOnlyAccess"],
        )
        # ResourceGroupId 为空也按账号级处理
        self.assertEqual(users["bob"]["policies"], ["AliyunRAMReadOnlyAccess"])

    def test_group_policies_and_principal_suffix_stripped(self):
        groups = {g["name"]: g for g in self.collect(aliyun_fixture())["groups"]}
        self.assertEqual(
            groups["ops"]["policies"],
            ["AdministratorAccess", "AliyunPAIFullAccess @资源组:rg-xyz"],
        )
        self.assertEqual(groups["ops"]["display_name"], "运维组")
        self.assertEqual(groups["ops"]["members"], ["alice", "bob"])
        self.assertEqual(
            groups["empty"], {"name": "empty", "display_name": "", "policies": [], "members": []}
        )

    def test_service_role_ignored(self):
        out = self.collect(aliyun_fixture())
        blob = repr(out)
        self.assertNotIn("AliyunPAIRolePolicy", blob)
        self.assertNotIn("AliyunServiceRoleForPAI", blob)

    def test_group_membership_written_back_to_users(self):
        fake = aliyun_fixture(
            groups=[{"GroupName": "ops"}, {"GroupName": "dev"}],
            members={"ops": ["alice"], "dev": ["alice", "bob"]},
        )
        users = {u["name"]: u for u in self.collect(fake)["users"]}
        self.assertEqual(users["alice"]["groups"], ["dev", "ops"])
        self.assertEqual(users["bob"]["groups"], ["dev"])
        calls = [q["GroupName"] for a, q in fake.calls if a == "ListUsersForGroup"]
        self.assertEqual(sorted(calls), ["dev", "ops"])

    def test_user_and_group_with_same_name_not_mixed(self):
        fake = aliyun_fixture(
            users=[{"UserName": "ops"}],
            groups=[{"GroupName": "ops"}],
            members={},
            attachments=[
                att("IMSUser", f"ops@{UID}.onaliyun.com", "UserPolicy"),
                att("IMSGroup", f"ops@group.{UID}.onaliyun.com", "GroupPolicy"),
            ],
        )
        out = self.collect(fake)
        self.assertEqual(out["users"][0]["policies"], ["UserPolicy"])
        self.assertEqual(out["groups"][0]["policies"], ["GroupPolicy"])

    def test_attachments_paginate_by_total_count(self):
        many = [att("IMSUser", f"alice@{UID}.onaliyun.com", f"P{i:03d}") for i in range(250)]
        fake = aliyun_fixture(attachments=many)
        users = {u["name"]: u for u in self.collect(fake)["users"]}
        self.assertEqual(len(users["alice"]["policies"]), 250)
        pages = [int(q["PageNumber"]) for a, q in fake.calls if a == "ListPolicyAttachments"]
        self.assertEqual(pages, [1, 2, 3])
        self.assertTrue(
            all(q["PageSize"] == "100" for a, q in fake.calls if a == "ListPolicyAttachments")
        )

    def test_attachments_empty_page_before_total_raises(self):
        # TotalCount 说还有，但某页空了：数据不完整，不能当作取完
        def lying(q):
            page = int(q["PageNumber"])
            batch = [att("IMSUser", "alice", "P1")] if page == 1 else []
            return 200, {"PolicyAttachments": {"PolicyAttachment": batch}, "TotalCount": 999}

        fake = aliyun_fixture(overrides={"ListPolicyAttachments": lying})
        with self.assertRaises(aliyun.AliyunError):
            self.collect(fake)

    def test_attachments_empty_total_zero_is_legit_empty(self):
        def empty(q):
            return 200, {"PolicyAttachments": {"PolicyAttachment": []}, "TotalCount": 0}

        fake = aliyun_fixture(overrides={"ListPolicyAttachments": empty})
        users = {u["name"]: u for u in self.collect(fake)["users"]}
        self.assertEqual(users["alice"]["policies"], [])
        self.assertEqual(
            [q["PageNumber"] for a, q in fake.calls if a == "ListPolicyAttachments"], ["1"]
        )

    def test_attachments_missing_container_raises(self):
        for body in (
            {"TotalCount": 0},
            {"PolicyAttachments": [], "TotalCount": 0},
            {"PolicyAttachments": {"PolicyAttachment": []}},  # 缺 TotalCount
        ):
            with self.subTest(body=body):
                fake = aliyun_fixture(
                    overrides={"ListPolicyAttachments": lambda q, b=body: (200, b)}
                )
                with self.assertRaises(aliyun.AliyunError):
                    self.collect(fake)

    def test_attachment_without_policy_name_raises(self):
        fake = aliyun_fixture(attachments=[att("IMSUser", "alice", "")])
        with self.assertRaises(aliyun.AliyunError):
            self.collect(fake)

    def test_missing_account_id_raises(self):
        fake = aliyun_fixture(overrides={"GetCallerIdentity": lambda q: (200, {})})
        with self.assertRaises(aliyun.AliyunError):
            self.collect(fake)

    def test_denied_propagates_not_empty(self):
        fake = aliyun_fixture(
            overrides={
                "ListPolicyAttachments": lambda q: (
                    403,
                    {"Code": "NoPermission", "Message": "not authorized"},
                )
            }
        )
        with self.assertRaises(aliyun.AliyunDenied):
            self.collect(fake)

    def test_group_members_missing_container_raises(self):
        fake = aliyun_fixture(
            overrides={"ListUsersForGroup": lambda q: (200, {"IsTruncated": False})}
        )
        with self.assertRaises(aliyun.AliyunError):
            self.collect(fake)

    def test_progress_called(self):
        msgs = []
        collect_aliyun(CREDS, transport=aliyun_fixture(), progress=msgs.append)
        self.assertTrue(any(UID in m for m in msgs))

    def test_secret_not_in_error(self):
        fake = aliyun_fixture(overrides={"ListUsers": lambda q: (500, {"Code": "InternalError"})})
        with self.assertRaises(aliyun.AliyunError) as ctx:
            self.collect(fake)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertNotIn("Signature", str(ctx.exception))


# ── 火山 ──────────────────────────────────────────────────────────────────


def pol(name, *scopes):
    item = {"PolicyName": name, "PolicyType": "System"}
    if scopes:
        item["PolicyScope"] = list(scopes)
    return item


GLOBAL = {"PolicyScopeType": "Global"}


def project(name):
    return {"PolicyScopeType": "Project", "ProjectName": name}


class VolcanoFake:
    def __init__(
        self, *, users, groups, members, user_policies, group_policies, overrides=None, keys=None
    ):
        self.users = users
        self.groups = groups
        self.members = members
        self.user_policies = user_policies
        self.group_policies = group_policies
        #: {用户名: [AK, …]}。没给的人当作一把都没有 —— 和「没采到」是两件事，
        #: 后者由 overrides 让 ListAccessKeys 回 403 来模拟。
        #: **火山的 ListAccessKeys 不带最近使用时间**，也没有阿里那种
        #: GetAccessKeyLastUsed 可以补问，所以这里的条目里根本没有 LastUsedDate
        self.keys = keys or {}
        self.overrides = overrides or {}
        self.calls = []

    def __call__(self, url, headers):
        assert "Authorization" in headers
        q = _query(url)
        action = q["Action"]
        self.calls.append((action, q))
        if action in self.overrides:
            return self.overrides[action](q)
        offset, limit = int(q.get("Offset", 0)), int(q.get("Limit", 100))

        def page(items):
            return items[offset : offset + limit]

        if action == "ListUsers":
            return 200, {"Result": {"UserMetadata": page(self.users)}}
        if action == "ListGroups":
            return 200, {"Result": {"UserGroups": page(self.groups)}}
        if action == "ListUsersForGroup":
            names = self.members.get(q["UserGroupName"], [])
            return 200, {"Result": {"Users": page([{"UserName": n} for n in names])}}
        if action == "ListAccessKeys":
            return 200, {"Result": {"AccessKeyMetadata": page(self.keys.get(q["UserName"], []))}}
        if action == "ListAttachedUserPolicies":
            return 200, {
                "Result": {"AttachedPolicyMetadata": self.user_policies.get(q["UserName"], [])}
            }
        if action == "ListAttachedUserGroupPolicies":
            return 200, {
                "Result": {
                    "AttachedPolicyMetadata": self.group_policies.get(q["UserGroupName"], [])
                }
            }
        raise AssertionError(action)


def volcano_fixture(**kw):
    base = dict(
        users=[
            {
                "UserName": "ShenYi",
                "DisplayName": "沈一",
                "AccountId": 2000000001,
                "Email": "shen.yi@wuji.tech",
                "EmailIsVerify": False,
            },
            {"UserName": "WangEr", "DisplayName": "王二", "AccountId": 2000000001},
        ],
        groups=[{"UserGroupName": "algo", "DisplayName": "算法组"}],
        members={"algo": ["WangEr", "ShenYi"]},
        user_policies={
            "ShenYi": [
                pol("TOSReadOnlyAccess", GLOBAL),
                pol("VKEFullAccess", project("proj-a"), project("proj-b")),
                pol("ECSReadOnlyAccess"),  # 无 PolicyScope → Global
            ],
            "WangEr": [],
        },
        group_policies={
            "algo": [pol("IAMFullAccess", project("default")), pol("TOSFullAccess", GLOBAL)]
        },
    )
    base.update(kw)
    return VolcanoFake(**base)


class OldSnapshotTests(unittest.TestCase):
    def test_snapshot_without_email_field_still_parses(self):
        """旧快照没有 email 字段（两个采集器以前都不填）：照常加载，值是空串。"""
        from delivery.inventory import parse

        snap = parse(
            {
                "captured_at": "2026-09-14T15:00:00+08:00",
                "accounts": [
                    {
                        "platform": "aliyun",
                        "account": "1000000000000001",
                        "users": [{"name": "alice", "display_name": "爱丽丝", "policies": []}],
                        "groups": [],
                    }
                ],
            }
        )
        self.assertEqual(snap.users[0].email, "")


class VolcanoCollectTests(unittest.TestCase):
    def collect(self, fake):
        return collect_volcano(VCREDS, transport=fake)

    def test_shape_and_account(self):
        out = self.collect(volcano_fixture())
        self.assertEqual(out["platform"], "volcano")
        self.assertEqual(out["account"], "2000000001")
        self.assertEqual([u["name"] for u in out["users"]], ["ShenYi", "WangEr"])
        self.assertEqual([u["email"] for u in out["users"]], ["shen.yi@wuji.tech", ""])

    def test_project_scope_suffix(self):
        users = {u["name"]: u for u in self.collect(volcano_fixture())["users"]}
        self.assertEqual(
            users["ShenYi"]["policies"],
            [
                "ECSReadOnlyAccess",
                "TOSReadOnlyAccess",
                "VKEFullAccess @项目:proj-a",
                "VKEFullAccess @项目:proj-b",
            ],
        )
        self.assertEqual(users["WangEr"]["policies"], [])

    def test_group_policies_members_and_backref(self):
        out = self.collect(volcano_fixture())
        g = out["groups"][0]
        self.assertEqual(g["policies"], ["IAMFullAccess @项目:default", "TOSFullAccess"])
        self.assertEqual(g["members"], ["ShenYi", "WangEr"])
        self.assertEqual(g["display_name"], "算法组")
        self.assertTrue(all(u["groups"] == ["algo"] for u in out["users"]))
        # 高危判定是子串匹配，带项目后缀不影响
        self.assertTrue(inventory.is_high_risk(g["policies"][0]))

    def test_project_scope_without_name_falls_back_to_plain(self):
        fake = volcano_fixture(
            user_policies={"ShenYi": [pol("X", {"PolicyScopeType": "Project"})], "WangEr": []}
        )
        users = {u["name"]: u for u in self.collect(fake)["users"]}
        self.assertEqual(users["ShenYi"]["policies"], ["X"])

    def test_missing_attached_policy_metadata_raises_for_user(self):
        fake = volcano_fixture(
            overrides={"ListAttachedUserPolicies": lambda q: (200, {"Result": {}})}
        )
        with self.assertRaises(volcano.VolcanoError) as ctx:
            self.collect(fake)
        self.assertIn("AttachedPolicyMetadata", str(ctx.exception))

    def test_missing_attached_policy_metadata_raises_for_group(self):
        fake = volcano_fixture(
            overrides={"ListAttachedUserGroupPolicies": lambda q: (200, {"Result": {"Other": []}})}
        )
        with self.assertRaises(volcano.VolcanoError):
            self.collect(fake)

    def test_empty_attached_list_is_legit_empty(self):
        fake = volcano_fixture(
            user_policies={"ShenYi": [], "WangEr": []}, group_policies={"algo": []}
        )
        out = self.collect(fake)
        self.assertEqual([u["policies"] for u in out["users"]], [[], []])

    def test_multiple_account_ids_rejected(self):
        fake = volcano_fixture(
            users=[
                {"UserName": "a", "AccountId": 2000000001},
                {"UserName": "b", "AccountId": 2000000002},
            ]
        )
        with self.assertRaises(volcano.VolcanoError):
            self.collect(fake)

    def test_no_account_id_defaults(self):
        fake = volcano_fixture(users=[{"UserName": "ShenYi"}], members={})
        self.assertEqual(self.collect(fake)["account"], "default")

    def test_missing_list_key_raises(self):
        fake = volcano_fixture(
            overrides={"ListGroups": lambda q: (200, {"Result": {"Groups": []}})}
        )
        with self.assertRaises(volcano.VolcanoError):
            self.collect(fake)

    def test_users_paginate(self):
        users = [{"UserName": f"u{i:03d}", "AccountId": 2000000001} for i in range(150)]
        fake = volcano_fixture(users=users, members={}, user_policies={})
        out = self.collect(fake)
        self.assertEqual(len(out["users"]), 150)
        offsets = [int(q["Offset"]) for a, q in fake.calls if a == "ListUsers"]
        self.assertEqual(offsets, [0, 100])

    def test_denied_raises(self):
        fake = volcano_fixture(
            overrides={
                "ListAttachedUserPolicies": lambda q: (
                    403,
                    {"ResponseMetadata": {"Error": {"Code": "AccessDenied", "Message": "no"}}},
                )
            }
        )
        with self.assertRaises(volcano.VolcanoDenied):
            self.collect(fake)


class VolcanoKeyCollectTests(unittest.TestCase):
    """火山的 AK 采集。**这朵云给不了「最近使用时间」**，所以采上来的每一把都要标明
    「不知道」—— 留空会被下游当成「从来没用过」，44 把 AK 同时变成假线索。

    （真机第一版就是这么错的：体检的「超过 90 天没用过」从 23 条跳到 67 条，
    多出来的 44 条全是火山的，而那 44 条里没有一条是真的。）
    """

    def vkey(self, kid, *, status="Active", created="20260204T104530Z", **extra):
        item = {"AccessKeyId": kid, "Status": status, "CreateDate": created}
        item.update(extra)
        return item

    def collect(self, **kw):
        fake = volcano_fixture(**kw)
        return collect_volcano(VCREDS, transport=fake), fake

    def users_of(self, out):
        return {u["name"]: u for u in out["users"]}

    def test_keys_come_through_marked_as_last_used_unknown(self):
        out, _ = self.collect(keys={"ShenYi": [self.vkey("AKLT0123456789ABCDEF")]})
        (k,) = self.users_of(out)["ShenYi"]["keys"]
        self.assertEqual(k["status"], "Active")
        self.assertEqual(k["created"], "20260204T104530Z")
        # 两件事要同时成立：值是空的，**并且**明确标了「这朵云查不到」。
        # 只留空的话下游没法区分「没用过」和「不知道」
        self.assertEqual(k["last_used"], "")
        self.assertIs(k["last_used_known"], False)
        # 同阿里：只进前 8 位，快照会被传阅
        self.assertEqual(k["id"], "AKLT0123")
        self.assertNotIn("ABCDEF", repr(out))

    def test_secret_is_never_collected(self):
        """接口本来也不给，但万一以后给了，也不能顺手带进快照。"""
        out, _ = self.collect(
            keys={"ShenYi": [self.vkey("AKLT0001", SecretAccessKey="super-secret")]}
        )
        self.assertNotIn("super-secret", repr(out))
        self.assertEqual(
            set(self.users_of(out)["ShenYi"]["keys"][0]),
            {"id", "status", "created", "last_used", "last_used_known"},
        )

    def test_update_date_is_not_mistaken_for_last_used(self):
        """火山的条目里有 `UpdateDate`，那是**上次改状态**的时间，不是上次使用。
        拿它冒充「最近使用」会让这个 bug 换个马甲回来：值看起来很新，
        于是这把 AK 永远不会被问「还要不要」，而它可能从建出来就没人用过。"""
        out, _ = self.collect(
            keys={"ShenYi": [self.vkey("AKLT0002", UpdateDate="20260901T000000Z")]}
        )
        k = self.users_of(out)["ShenYi"]["keys"][0]
        self.assertEqual(k["last_used"], "")
        self.assertIs(k["last_used_known"], False)

    def test_every_user_is_asked(self):
        out, fake = self.collect(keys={"ShenYi": [self.vkey("AKLT0003")]})
        asked = [q["UserName"] for a, q in fake.calls if a == "ListAccessKeys"]
        self.assertEqual(sorted(asked), ["ShenYi", "WangEr"])
        # 一把都没有的人是空列表，不是 None（None 的含义是「没采到」）
        self.assertEqual(self.users_of(out)["WangEr"]["keys"], [])

    def test_permission_denied_reports_not_collected_not_empty(self):
        """缺 `iam:ListAccessKeys` 时是「没采到」。装成空列表的话，
        全公司会看到「你没有密钥」，而那正是最该被发现的状态。"""
        out, _ = self.collect(
            overrides={
                "ListAccessKeys": lambda q: (
                    403,
                    {"ResponseMetadata": {"Error": {"Code": "AccessDenied", "Message": "no"}}},
                )
            }
        )
        self.assertTrue(all(u["keys"] is None for u in out["users"]))
        # 但这一次的其余部分照常采到 —— AK 拿不到不该把整份快照作废
        self.assertEqual(self.users_of(out)["ShenYi"]["policies"][0], "ECSReadOnlyAccess")

    def test_other_errors_are_not_swallowed(self):
        """只吞「被拒」这一种。接口 500、响应缺键这些要炸出来 ——
        吞了就会把「这次没查成」记成「这个人没有 AK」。"""
        out = {
            "ListAccessKeys": lambda q: (200, {"Result": {"Other": []}}),  # 缺列表键
        }
        with self.assertRaises(volcano.VolcanoError):
            self.collect(overrides=out)


# ── build_snapshot ────────────────────────────────────────────────────────


def _raise(exc):
    def fn(progress):
        raise exc

    return fn


class BuildSnapshotTests(unittest.TestCase):
    NOW = staticmethod(lambda: "2026-09-15T12:00:00+08:00")

    def test_one_failed_job_becomes_error_others_collected(self):
        ok_ali = lambda p: collect_aliyun(CREDS, transport=aliyun_fixture(), progress=p)  # noqa: E731
        ok_volc = lambda p: collect_volcano(VCREDS, transport=volcano_fixture(), progress=p)  # noqa: E731
        bad = _raise(
            aliyun.AliyunDenied("`ListPolicyAttachments` 被拒（NoPermission）：x\n第二行很长")
        )
        snap = build_snapshot(
            [
                ("aliyun", UID, ok_ali),
                ("aliyun", "1000000000000002", bad),
                ("volcano", "v", ok_volc),
            ],
            now=self.NOW,
        )
        self.assertEqual(snap["captured_at"], "2026-09-15T12:00:00+08:00")
        self.assertEqual(len(snap["accounts"]), 3)
        err = snap["accounts"][1]
        self.assertEqual(set(err), {"platform", "account", "error"})
        self.assertEqual((err["platform"], err["account"]), ("aliyun", "1000000000000002"))
        self.assertNotIn("\n", err["error"])
        self.assertNotIn("第二行", err["error"])

        parsed = inventory.parse(snap)
        self.assertFalse(parsed.complete)
        self.assertEqual(len(parsed.incomplete), 1)
        self.assertIn("aliyun/1000000000000002", parsed.incomplete[0])
        self.assertIsNotNone(parsed.user("aliyun", UID, "alice"))
        self.assertIsNotNone(parsed.user("volcano", "2000000001", "ShenYi"))
        alice = parsed.user("aliyun", UID, "alice")
        self.assertIn("AdministratorAccess", parsed.effective_policies(alice))

    def test_error_message_truncated(self):
        snap = build_snapshot(
            [("volcano", "v", _raise(volcano.VolcanoError("x" * 1000)))], now=self.NOW
        )
        self.assertEqual(len(snap["accounts"][0]["error"]), 200)

    def test_all_ok_is_complete(self):
        ok = lambda p: collect_aliyun(CREDS, transport=aliyun_fixture(), progress=p)  # noqa: E731
        parsed = inventory.parse(build_snapshot([("aliyun", UID, ok)], now=self.NOW))
        self.assertTrue(parsed.complete)

    def test_non_delivery_error_not_swallowed(self):
        called = []

        def later(p):
            called.append(1)
            return {"platform": "volcano", "account": "v", "users": [], "groups": []}

        for exc in (KeyError("x"), TypeError("x"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__), self.assertRaises(type(exc)):
                build_snapshot(
                    [("aliyun", UID, _raise(exc)), ("volcano", "v", later)], now=self.NOW
                )
        self.assertEqual(called, [], "非 DeliveryError 应立即中断，不继续采后面的账号")

    def test_progress_passed_through(self):
        seen = []
        build_snapshot(
            [("x", "y", lambda p: (p("hello"), {"platform": "x", "account": "y"})[1])],
            progress=seen.append,
            now=self.NOW,
        )
        self.assertEqual(seen, ["hello"])

    def test_default_now_is_iso(self):
        snap = build_snapshot([])
        self.assertRegex(
            snap["captured_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$"
        )
        self.assertEqual(snap["accounts"], [])

    def test_error_with_empty_message_still_marks_incomplete(self):
        """已知 bug（inventory_collect.py:243）：`str(exc).splitlines()[0]` 对空消息
        抛 IndexError，一个云账号失败把整份快照带崩。"""
        snap = build_snapshot([("aliyun", UID, _raise(DeliveryError()))], now=self.NOW)
        self.assertFalse(inventory.parse(snap).complete)

    def test_error_with_leading_newline_still_marks_incomplete(self):
        """已知 bug（inventory_collect.py:243 + inventory.py `if acc.get("error")`）：
        消息以换行开头时首行为空串 → error="" → parse 视为「完整且没有用户」，
        恰好是模块头注释要防的「和真的没权限分不出来」。"""
        snap = build_snapshot([("aliyun", UID, _raise(DeliveryError("\n详细原因")))], now=self.NOW)
        self.assertFalse(inventory.parse(snap).complete)


if __name__ == "__main__":
    unittest.main()
