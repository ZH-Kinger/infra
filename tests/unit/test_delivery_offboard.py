"""离职停号：检测 → 自动停用（可恢复）→ 管理员确认删号 / 恢复。

这份用例按「停错了、删错了会怎样」组织：

  · 删号只认 offboard.json 里有的号；受保护的号（panel-/power-/tempak）永远不停不删。
  · 一轮判离职的人超过上限 → 一个都不停（接口抖一下别把一批在职的人停掉）。
  · 恢复只开回停用时关掉的那几把 AK，别的一律不碰。
  · 云上只打 RAM / IAM / STS 的账号接口，任何数据接口（OSS/NAS/PAI/TOS…）都不许出现。

离线，数据虚构，云接口用有状态的假 transport。
"""

from __future__ import annotations

import json
import stat
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from delivery import offboard
from delivery.clouds import aliyun, volcano
from delivery.people import AccountRef, Person
from delivery.provision import AliyunExecutor, PartialDisable, ProvisionError, VolcanoExecutor

ALI_ACC = "1000000000000001"
VOLC_ACC = "2000000001"

#: 离职停号允许打的全部云接口。出现别的（尤其是任何数据面接口）就是越界
ALIYUN_ALLOWED = {
    "GetCallerIdentity",
    "GetLoginProfile",
    "DeleteLoginProfile",
    "CreateLoginProfile",
    "ListAccessKeys",
    "UpdateAccessKey",
    "DeleteAccessKey",
    "ListPoliciesForUser",
    "ListGroupsForUser",
    "RemoveUserFromGroup",
    "DetachPolicyFromUser",
    "UnbindMFADevice",
    "DeleteUser",
}
VOLCANO_ALLOWED = {
    "ListUsers",
    "GetLoginProfile",
    "UpdateLoginProfile",
    "DeleteLoginProfile",
    "ListAccessKeys",
    "UpdateAccessKey",
    "DeleteAccessKey",
    "ListAttachedUserPolicies",
    "ListGroupsForUser",
    "RemoveUserFromGroup",
    "DetachUserPolicy",
    "DeleteUser",
}


# ── 有状态的假云 ─────────────────────────────────────────────────────────


class AliyunCloud:
    """一个 RAM 用户的实时状态，按 Action 改。`fail` = {Action: 错误码}。"""

    def __init__(self, *, login=True, keys=None, groups=(), policies=(), mfa=False):
        self.exists = True
        self.login = login
        self.keys = dict(keys if keys is not None else {"LTAIa1": "Active", "LTAIb2": "Inactive"})
        self.groups = list(groups)
        self.policies = list(policies)
        self.mfa = mfa
        self.fail = {}
        self.calls = []
        self.hosts = set()

    def err(self, code, status=404):
        return status, {"Code": code, "Message": code}

    def send(self, url):
        parts = urllib.parse.urlsplit(url)
        self.hosts.add(parts.hostname)
        q = dict(urllib.parse.parse_qsl(parts.query))
        act = q["Action"]
        self.calls.append((act, dict(q)))
        if act == "GetCallerIdentity":
            return 200, {"AccountId": ALI_ACC}
        if act in self.fail:
            return self.err(self.fail[act], 400)
        if not self.exists:
            return self.err("EntityNotExist.User")
        if act == "GetLoginProfile":
            return (
                (200, {"LoginProfile": {}})
                if self.login
                else self.err("EntityNotExist.User.LoginProfile")
            )
        if act == "DeleteLoginProfile":
            if not self.login:
                return self.err("EntityNotExist.User.LoginProfile")
            self.login = False
            return 200, {}
        if act == "CreateLoginProfile":
            self.login = True
            return 200, {}
        if act == "ListAccessKeys":
            return 200, {
                "AccessKeys": {
                    "AccessKey": [{"AccessKeyId": k, "Status": s} for k, s in self.keys.items()]
                }
            }
        if act == "UpdateAccessKey":
            if q["UserAccessKeyId"] not in self.keys:
                return self.err("EntityNotExist.User.AccessKey")
            self.keys[q["UserAccessKeyId"]] = q["Status"]
            return 200, {}
        if act == "DeleteAccessKey":
            self.keys.pop(q["UserAccessKeyId"], None)
            return 200, {}
        if act == "ListPoliciesForUser":
            return 200, {"Policies": {"Policy": [dict(p) for p in self.policies]}}
        if act == "ListGroupsForUser":
            return 200, {"Groups": {"Group": [{"GroupName": g} for g in self.groups]}}
        if act == "RemoveUserFromGroup":
            self.groups.remove(q["GroupName"])
            return 200, {}
        if act == "DetachPolicyFromUser":
            self.policies = [p for p in self.policies if p["PolicyName"] != q["PolicyName"]]
            return 200, {}
        if act == "UnbindMFADevice":
            if not self.mfa:
                return self.err("EntityNotExist.User.MFADevice")
            self.mfa = False
            return 200, {}
        if act == "DeleteUser":
            if self.keys or self.groups or self.policies or self.mfa or self.login:
                return self.err("DeleteConflict.User", 409)
            self.exists = False
            return 200, {}
        return self.err("UnexpectedAction", 400)

    @property
    def actions(self):
        return [a for a, _ in self.calls]

    def executor(self):
        return AliyunExecutor(ALI_ACC, aliyun.Credentials("id", "sk"), transport=self.send)


class VolcanoCloud:
    """火山 IAM 用户的实时状态。登录配置用显式的 LoginAllowed 开关。"""

    def __init__(self, *, login_allowed=True, keys=None, groups=(), policies=()):
        self.exists = True
        self.profile = True
        self.login_allowed = login_allowed
        self.keys = dict(keys if keys is not None else {"AKLTa1": "active", "AKLTb2": "inactive"})
        self.groups = list(groups)
        self.policies = list(policies)
        self.fail = {}
        self.calls = []

    def err(self, code, status=404):
        return status, {"ResponseMetadata": {"Error": {"Code": code, "Message": code}}}

    def ok(self, result=None):
        body = {"ResponseMetadata": {}}
        if result is not None:
            body["Result"] = result
        return 200, body

    def send(self, url, headers, data=None):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        act = q["Action"]
        self.calls.append((act, dict(q)))
        if act == "ListUsers":
            return self.ok({"UserMetadata": [{"AccountId": VOLC_ACC}]})
        if act in self.fail:
            return self.err(self.fail[act], 400)
        if not self.exists:
            return self.err("EntityNotExist.User")
        if act == "GetUser":
            return self.ok({"User": {"UserName": "u"}})
        if act == "GetLoginProfile":
            if not self.profile:
                # 没有登录配置时火山回全零 stub，不是 NotExist
                return self.ok({"LoginProfile": {"UserName": "", "LoginAllowed": False}})
            return self.ok({"LoginProfile": {"LoginAllowed": self.login_allowed}})
        if act == "UpdateLoginProfile":
            self.login_allowed = q["LoginAllowed"] == "true"
            return self.ok()
        if act == "DeleteLoginProfile":
            if not self.profile:
                return self.err("EntityNotExist.LoginProfile")
            self.profile = False
            return self.ok()
        if act == "ListAccessKeys":
            return self.ok(
                {
                    "AccessKeyMetadata": [
                        {"AccessKeyId": k, "Status": s} for k, s in self.keys.items()
                    ]
                }
            )
        if act == "UpdateAccessKey":
            if q["AccessKeyId"] not in self.keys:
                return self.err("AccessKeyNotExist")
            self.keys[q["AccessKeyId"]] = q["Status"]
            return self.ok()
        if act == "DeleteAccessKey":
            # 真机：火山不许删启用中的 AK
            if self.keys.get(q["AccessKeyId"]) == "active":
                return self.err("AccessKeyCanNotDelete", 400)
            self.keys.pop(q["AccessKeyId"], None)
            return self.ok()
        if act == "ListAttachedUserPolicies":
            return self.ok({"AttachedPolicyMetadata": [dict(p) for p in self.policies]})
        if act == "ListGroupsForUser":
            return self.ok({"UserGroupMetadata": [{"UserGroupName": g} for g in self.groups]})
        if act == "RemoveUserFromGroup":
            self.groups.remove(q["UserGroupName"])
            return self.ok()
        if act == "DetachUserPolicy":
            self.policies = [p for p in self.policies if p["PolicyName"] != q["PolicyName"]]
            return self.ok()
        if act == "DeleteUser":
            if self.keys or self.groups or self.policies:
                return self.err("DeleteConflict", 409)
            self.exists = False
            return self.ok()
        return self.err("UnexpectedAction", 400)

    @property
    def actions(self):
        return [a for a, _ in self.calls]

    def executor(self):
        return VolcanoExecutor(VOLC_ACC, volcano.Credentials("AK", "SK"), transport=self.send)


def pol(name, kind="System"):
    return {"PolicyName": name, "PolicyType": kind}


# ── 执行身份：阿里 ───────────────────────────────────────────────────────


class AliyunDisableTests(unittest.TestCase):
    def test_closes_login_and_only_active_keys(self):
        cloud = AliyunCloud()
        got = cloud.executor().disable_user("lisi")
        self.assertEqual(got, {"login": True, "keys": ["LTAIa1"]})
        self.assertFalse(cloud.login)
        self.assertEqual(cloud.keys, {"LTAIa1": "Inactive", "LTAIb2": "Inactive"})
        # 本来就 Inactive 的那把不能动：恢复时才不会把它一起开回去
        updated = [q["UserAccessKeyId"] for a, q in cloud.calls if a == "UpdateAccessKey"]
        self.assertEqual(updated, ["LTAIa1"])
        self.assertLessEqual(set(cloud.actions), ALIYUN_ALLOWED)
        self.assertEqual(cloud.hosts, {"ram.aliyuncs.com", "sts.aliyuncs.com"})

    def test_user_gone_on_cloud_reports_gone_and_writes_nothing(self):
        cloud = AliyunCloud()
        cloud.exists = False
        got = cloud.executor().disable_user("lisi")
        self.assertTrue(got.get("gone"))
        self.assertEqual((got["login"], got["keys"]), (False, []))
        self.assertEqual([a for a in cloud.actions if not a.startswith(("Get", "List"))], [])

    def test_partial_disable_carries_what_was_done(self):
        """登录已关、禁 AK 失败 → PartialDisable，带上已经做成的部分。"""
        cloud = AliyunCloud(keys={"LTAIa1": "Active"})
        cloud.fail["UpdateAccessKey"] = "ServiceUnavailable"
        with self.assertRaises(PartialDisable) as cm:
            cloud.executor().disable_user("lisi")
        self.assertEqual(cm.exception.partial, {"login": True, "keys": []})

    def test_nothing_done_is_a_plain_error_not_partial(self):
        cloud = AliyunCloud(login=False, keys={"LTAIa1": "Active"})
        cloud.fail["UpdateAccessKey"] = "ServiceUnavailable"
        with self.assertRaises(aliyun.AliyunError) as cm:
            cloud.executor().disable_user("lisi")
        self.assertNotIsInstance(cm.exception, PartialDisable)

    def test_no_login_profile_is_not_an_error(self):
        cloud = AliyunCloud(login=False)
        got = cloud.executor().disable_user("lisi")
        self.assertEqual(got["login"], False)
        self.assertNotIn("DeleteLoginProfile", cloud.actions)

    def test_other_errors_surface(self):
        """读登录配置被拒不能当「没有登录配置」：那样会报「已停用」而人其实还能登。"""
        cloud = AliyunCloud()
        cloud.fail["GetLoginProfile"] = "NoPermission"
        with self.assertRaises(aliyun.AliyunError):
            cloud.executor().disable_user("lisi")
        self.assertTrue(cloud.login)

    def test_disable_twice_is_harmless(self):
        cloud = AliyunCloud()
        ex = cloud.executor()
        ex.disable_user("lisi")
        again = ex.disable_user("lisi")
        self.assertEqual(again, {"login": False, "keys": []})

    def test_wrong_account_touches_nothing(self):
        cloud = AliyunCloud()
        ex = AliyunExecutor("9999", aliyun.Credentials("id", "sk"), transport=cloud.send)
        with self.assertRaises(ProvisionError):
            ex.disable_user("lisi")
        self.assertEqual(cloud.actions, ["GetCallerIdentity"])


class AliyunEnableTests(unittest.TestCase):
    def test_reopens_exactly_what_was_closed(self):
        cloud = AliyunCloud(keys={"LTAIa1": "Active", "LTAIb2": "Inactive", "LTAIc3": "Active"})
        ex = cloud.executor()
        got = ex.disable_user("lisi")
        ex.enable_user("lisi", login=got["login"], keys=got["keys"])
        # b2 停用前就是 Inactive —— 恢复后也必须还是 Inactive
        self.assertEqual(cloud.keys, {"LTAIa1": "Active", "LTAIb2": "Inactive", "LTAIc3": "Active"})
        self.assertTrue(cloud.login)
        activated = [
            q["UserAccessKeyId"]
            for a, q in cloud.calls
            if a == "UpdateAccessKey" and q["Status"] == "Active"
        ]
        self.assertEqual(sorted(activated), ["LTAIa1", "LTAIc3"])

    def test_missing_key_is_skipped_others_reopened(self):
        """停用后有人手工删了一把 AK：恢复时跳过它，其余照开。"""
        cloud = AliyunCloud(keys={"LTAIa1": "Inactive"})
        cloud.executor().enable_user("lisi", login=False, keys=["LTAIgone", "LTAIa1"])
        self.assertEqual(cloud.keys, {"LTAIa1": "Active"})

    def test_other_enable_errors_surface(self):
        cloud = AliyunCloud(keys={"LTAIa1": "Inactive"})
        cloud.fail["UpdateAccessKey"] = "Throttling"
        with self.assertRaises(aliyun.AliyunError):
            cloud.executor().enable_user("lisi", login=False, keys=["LTAIa1"])

    def test_login_false_does_not_create_a_login_profile(self):
        cloud = AliyunCloud(login=False)
        cloud.executor().enable_user("lisi", login=False, keys=[])
        self.assertNotIn("CreateLoginProfile", cloud.actions)
        self.assertFalse(cloud.login)


class AliyunDeleteTests(unittest.TestCase):
    def full(self):
        return AliyunCloud(
            groups=["algo", "ops"],
            policies=[pol("AliyunOSSReadOnlyAccess"), pol("wuji-oss-auto-lisi", "Custom")],
            mfa=True,
        )

    def test_order_and_clean_delete(self):
        cloud = self.full()
        left = cloud.executor().delete_user("lisi")
        self.assertEqual(left, [])
        self.assertFalse(cloud.exists)
        writes = [a for a in cloud.actions if not a.startswith(("List", "Get"))]
        self.assertEqual(
            writes,
            [
                "DeleteAccessKey",
                "DeleteAccessKey",
                "RemoveUserFromGroup",
                "RemoveUserFromGroup",
                "DetachPolicyFromUser",
                "DetachPolicyFromUser",
                "UnbindMFADevice",
                "DeleteLoginProfile",
                "DeleteUser",
            ],
        )
        self.assertLessEqual(set(cloud.actions), ALIYUN_ALLOWED)
        self.assertEqual(cloud.hosts, {"ram.aliyuncs.com", "sts.aliyuncs.com"})
        # DeleteUser 只对这个用户
        for a, q in cloud.calls:
            if a != "GetCallerIdentity":
                self.assertEqual(q.get("UserName"), "lisi", a)

    def test_no_mfa_no_login_is_not_a_leftover(self):
        cloud = AliyunCloud(login=False, keys={}, mfa=False)
        self.assertEqual(cloud.executor().delete_user("lisi"), [])
        self.assertFalse(cloud.exists)

    def test_failure_leaves_user_and_reports_it(self):
        """有一步失败就**不能**调 DeleteUser：阿里会拒，而且就算不拒也说明没清干净。"""
        cloud = self.full()
        cloud.fail["DetachPolicyFromUser"] = "ServiceUnavailable"
        left = cloud.executor().delete_user("lisi")
        self.assertEqual(len(left), 2)
        self.assertTrue(all("摘策略" in x for x in left), left)
        self.assertNotIn("DeleteUser", cloud.actions)
        self.assertTrue(cloud.exists)
        # 失败的那步不挡其它步
        self.assertIn("DeleteLoginProfile", cloud.actions)

    def test_delete_user_rejected_is_a_leftover(self):
        cloud = AliyunCloud(keys={}, login=False)
        cloud.fail["DeleteUser"] = "DeleteConflict.User.Group"
        left = cloud.executor().delete_user("lisi")
        self.assertEqual(len(left), 1)
        self.assertIn("删用户", left[0])

    def test_already_gone_user_is_not_an_error(self):
        """回归（曾是 bug）：用户已经不在（控制台手工删过）时
        `ListAccessKeys` 抛 `EntityNotExist.User`，
        不在 `step` 里，直接穿出 `delete_user`。docstring 说「NotExist 不算没删干净」，
        可这一条会让这条离职记录永远删不掉（每次点「确认删除」都 409）。
        provision.py AliyunExecutor.delete_user：`body = self._call(... "ListAccessKeys" ...)`。"""
        cloud = AliyunCloud()
        cloud.exists = False
        self.assertEqual(cloud.executor().delete_user("lisi"), [])


# ── 执行身份：火山 ───────────────────────────────────────────────────────


class VolcanoDisableTests(unittest.TestCase):
    def test_closes_login_switch_and_active_keys(self):
        cloud = VolcanoCloud()
        got = cloud.executor().disable_user("lisi")
        self.assertEqual(got, {"login": True, "keys": ["AKLTa1"]})
        self.assertFalse(cloud.login_allowed)
        # 火山关登录是关开关，不删登录配置
        self.assertNotIn("DeleteLoginProfile", cloud.actions)
        self.assertTrue(cloud.profile)
        self.assertEqual(cloud.keys, {"AKLTa1": "inactive", "AKLTb2": "inactive"})
        self.assertLessEqual(set(cloud.actions), VOLCANO_ALLOWED)

    def test_already_disallowed_is_not_recorded_as_closed(self):
        """登录本来就关着 → login=False。否则恢复时会把一个**本来就不能登录**的号打开。"""
        cloud = VolcanoCloud(login_allowed=False)
        got = cloud.executor().disable_user("lisi")
        self.assertFalse(got["login"])
        self.assertNotIn("UpdateLoginProfile", cloud.actions)

    def test_user_gone_on_cloud_reports_gone(self):
        cloud = VolcanoCloud()
        cloud.exists = False
        got = cloud.executor().disable_user("lisi")
        self.assertTrue(got.get("gone"))
        self.assertNotIn("UpdateLoginProfile", cloud.actions)
        self.assertNotIn("UpdateAccessKey", cloud.actions)

    def test_login_profile_notexist_but_user_alive_is_not_gone(self):
        """火山对「有用户、没登录配置」的报错码里可能也带 user + notexist。
        只看它就把号记成「云上已不存在」，一个还开着 AK 的离职号会从待办里消失。"""
        cloud = VolcanoCloud(keys={"AKLTa1": "active"})
        cloud.fail["GetLoginProfile"] = "EntityNotExist.User.LoginProfile"
        got = cloud.executor().disable_user("lisi")
        self.assertNotIn("gone", got)
        self.assertEqual(got["keys"], ["AKLTa1"])
        self.assertIn("GetUser", cloud.actions)

    def test_partial_disable_carries_what_was_done(self):
        cloud = VolcanoCloud(keys={"AKLTa1": "active"})
        cloud.fail["ListAccessKeys"] = "InternalError"
        with self.assertRaises(PartialDisable) as cm:
            cloud.executor().disable_user("lisi")
        self.assertEqual(cm.exception.partial, {"login": True, "keys": []})

    def test_enable_skips_missing_key(self):
        cloud = VolcanoCloud(keys={"AKLTa1": "inactive"})
        cloud.executor().enable_user("lisi", login=False, keys=["AKLTgone", "AKLTa1"])
        self.assertEqual(cloud.keys, {"AKLTa1": "active"})

    def test_zero_stub_profile_is_nothing_to_close(self):
        cloud = VolcanoCloud()
        cloud.profile = False
        got = cloud.executor().disable_user("lisi")
        self.assertFalse(got["login"])
        self.assertNotIn("UpdateLoginProfile", cloud.actions)

    def test_restore_reopens_exactly_what_was_closed(self):
        cloud = VolcanoCloud(keys={"AKLTa1": "active", "AKLTb2": "inactive"})
        ex = cloud.executor()
        got = ex.disable_user("lisi")
        ex.enable_user("lisi", login=got["login"], keys=got["keys"])
        self.assertTrue(cloud.login_allowed)
        self.assertEqual(cloud.keys, {"AKLTa1": "active", "AKLTb2": "inactive"})

    def test_restore_without_login_leaves_switch_off(self):
        cloud = VolcanoCloud(login_allowed=False)
        ex = cloud.executor()
        got = ex.disable_user("lisi")
        ex.enable_user("lisi", login=got["login"], keys=got["keys"])
        self.assertFalse(cloud.login_allowed)


class VolcanoDeleteTests(unittest.TestCase):
    def test_order_and_clean_delete(self):
        cloud = VolcanoCloud(groups=["algo"], policies=[pol("TOSReadOnlyAccess")])
        left = cloud.executor().delete_user("lisi")
        self.assertEqual(left, [])
        self.assertFalse(cloud.exists)
        writes = [a for a in cloud.actions if not a.startswith(("List", "Get"))]
        self.assertEqual(
            writes,
            [
                "UpdateAccessKey",
                "DeleteAccessKey",
                "DeleteAccessKey",
                "RemoveUserFromGroup",
                "DetachUserPolicy",
                "DeleteLoginProfile",
                "DeleteUser",
            ],
        )
        self.assertNotIn("UnbindMFADevice", cloud.actions)
        self.assertLessEqual(set(cloud.actions), VOLCANO_ALLOWED)

    def key_writes(self, cloud):
        return [
            (a, q["AccessKeyId"], q.get("Status", ""))
            for a, q in cloud.calls
            if a in ("UpdateAccessKey", "DeleteAccessKey")
        ]

    def test_active_key_is_deactivated_before_delete(self):
        """真机：火山拒删启用中的 AK（AccessKeyCanNotDelete）。没被自动停过的号
        （弱信号 / 管理员直接确认离职）AK 都还开着，不先禁就永远删不掉。"""
        cloud = VolcanoCloud(keys={"AKLTa1": "active"})
        left = cloud.executor().delete_user("lisi")
        self.assertEqual(left, [])
        self.assertEqual(
            self.key_writes(cloud),
            [("UpdateAccessKey", "AKLTa1", "inactive"), ("DeleteAccessKey", "AKLTa1", "")],
        )
        self.assertFalse(cloud.exists)

    def test_inactive_key_is_deleted_without_extra_update(self):
        cloud = VolcanoCloud(keys={"AKLTb2": "inactive"})
        self.assertEqual(cloud.executor().delete_user("lisi"), [])
        self.assertEqual(self.key_writes(cloud), [("DeleteAccessKey", "AKLTb2", "")])
        self.assertFalse(cloud.exists)

    def test_active_status_is_case_insensitive(self):
        cloud = VolcanoCloud(keys={"AKLTa1": "Active"})
        cloud.executor().delete_user("lisi")
        self.assertEqual(self.key_writes(cloud)[0], ("UpdateAccessKey", "AKLTa1", "inactive"))

    def test_deactivate_failure_is_a_leftover_and_user_kept(self):
        cloud = VolcanoCloud(keys={"AKLTa1": "active"})
        cloud.fail["UpdateAccessKey"] = "InternalError"
        left = cloud.executor().delete_user("lisi")
        self.assertTrue(any("禁用 AccessKey" in x for x in left), left)
        self.assertNotIn("DeleteUser", cloud.actions)
        self.assertTrue(cloud.exists)
        self.assertIn("AKLTa1", cloud.keys)

    def test_missing_login_profile_is_not_a_leftover(self):
        cloud = VolcanoCloud(keys={})
        cloud.profile = False
        self.assertEqual(cloud.executor().delete_user("lisi"), [])
        self.assertFalse(cloud.exists)

    def test_failure_keeps_user(self):
        cloud = VolcanoCloud(groups=["algo"])
        cloud.fail["RemoveUserFromGroup"] = "InternalError"
        left = cloud.executor().delete_user("lisi")
        self.assertEqual(len(left), 1)
        self.assertIn("移出用户组 algo", left[0])
        self.assertNotIn("DeleteUser", cloud.actions)
        self.assertTrue(cloud.exists)

    def test_already_gone_user_is_not_an_error(self):
        """回归（曾是 bug）：同阿里。用户已不在时 `ListAccessKeys` 的 NotExist 穿出 `delete_user`。
        provision.py VolcanoExecutor.delete_user：`body = self._call(... "ListAccessKeys" ...)`。"""
        cloud = VolcanoCloud()
        cloud.exists = False
        self.assertEqual(cloud.executor().delete_user("lisi"), [])


# ── offboard 模块 ────────────────────────────────────────────────────────


def ref(platform, name, account=None, status="confirmed"):
    acc = account or (ALI_ACC if platform == "aliyun" else VOLC_ACC)
    return AccountRef(platform, acc, name, status)


def person(name, uid, *refs, email=""):
    return Person(name=name, email=email or f"{uid}@wuji.tech", union_id=uid, accounts=tuple(refs))


class FakeEx:
    """按 (platform, account) 发的假执行器，记下每一次调用。"""

    def __init__(self, book, platform, account):
        self.book, self.platform, self.account = book, platform, account

    def _rec(self, *call):
        self.book.calls.append((self.platform, self.account, *call))

    def disable_user(self, user):
        self._rec("disable", user)
        if user in self.book.fail:
            raise ProvisionError(f"{user} 停用失败")
        return {"login": True, "keys": [f"AK-{user}"]}

    def enable_user(self, user, *, login, keys):
        self._rec("enable", user, login, tuple(keys or ()))

    def delete_user(self, user):
        self._rec("delete", user)
        return list(self.book.left.get(user, []))


class Book:
    def __init__(self):
        self.calls = []
        self.fail = set()
        self.left = {}

    def __call__(self, platform, account):
        return FakeEx(self, platform, account)

    def of(self, kind):
        return [c for c in self.calls if c[2] == kind]


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "people.json").write_text("{}", encoding="utf-8")
        self.path = offboard.path_beside(str(self.dir / "people.json"))
        self.book = Book()

    def records(self):
        return offboard.load(self.path)


class PathAndKeyTests(_Base):
    def test_file_sits_beside_people(self):
        self.assertEqual(self.path, (self.dir / "offboard.json").resolve())

    def test_cloud_user(self):
        self.assertEqual(offboard.cloud_user("lisi@1000.onaliyun.com"), "lisi")
        self.assertEqual(offboard.cloud_user(" lisi "), "lisi")
        self.assertEqual(offboard.cloud_user(None), "")
        self.assertEqual(offboard.cloud_user(""), "")

    def test_missing_file_is_empty(self):
        self.assertEqual(self.records(), {})
        self.assertEqual(offboard.pending(self.path), [])

    def test_corrupt_file_raises_not_empty(self):
        """读坏了当成「没有记录」的话，恢复过的人下一轮会被再停一次。"""
        self.path.write_text("{nope", encoding="utf-8")
        with self.assertRaises(offboard.OffboardError):
            offboard.load(self.path)
        self.path.write_text(json.dumps({"x": 1}), encoding="utf-8")
        with self.assertRaises(offboard.OffboardError):
            offboard.load(self.path)

    def test_written_file_is_0600(self):
        offboard.note_suspects(self.path, [person("李四", "on_1", ref("aliyun", "lisi"))])
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(list(self.dir.glob(".offboard-*")), "临时文件没清理")


class TargetsTests(unittest.TestCase):
    def test_only_confirmed_refs_on_supported_clouds(self):
        p = person(
            "李四",
            "on_1",
            ref("aliyun", "lisi"),
            ref("volcano", "lisi"),
            ref("jiuzhang", "lisi", account="wuji"),
            ref("aliyun", "lisi-pending", status="pending"),
        )
        # 九章也要列出来（面板停不了，但要提醒人去控制台）；pending 的不算
        self.assertEqual(
            offboard.targets_of(p),
            [
                ("aliyun", ALI_ACC, "lisi"),
                ("volcano", VOLC_ACC, "lisi"),
                ("jiuzhang", "wuji", "lisi"),
            ],
        )
        self.assertEqual(
            offboard.targets_of(p, offboard.PLATFORMS),
            [("aliyun", ALI_ACC, "lisi"), ("volcano", VOLC_ACC, "lisi")],
        )

    def test_jiuzhang_logins_are_not_protected_by_the_cloud_prefixes(self):
        """九章所有人都叫 wuji-xxx。套云上那套前缀 = 整个平台被挡光，一个都检测不到。"""
        self.assertFalse(offboard.protected("wuji-wangyuran", "jiuzhang"))
        self.assertTrue(offboard.protected("wuji-ci", "aliyun"))
        self.assertTrue(offboard.protected("panel-executor", "jiuzhang"))
        p2 = person("王昱然", "on_w", ref("jiuzhang", "wuji-wangyuran", account="wuji"))
        self.assertEqual(offboard.targets_of(p2), [("jiuzhang", "wuji", "wuji-wangyuran")])

    def test_protected_names_never_targets(self):
        p = person(
            "服务号",
            "on_s",
            ref("aliyun", "panel-executor"),
            ref("aliyun", "Power-Application-User"),
            ref("volcano", "tempak-foo"),
            ref("aliyun", "TEMPAK-x"),
            ref("aliyun", "wuji-oss-sync"),
            ref("volcano", "RL-trainer"),
            ref("aliyun", "finance"),
            ref("aliyun", "FINANCE"),
            ref("volcano", "data-tran"),
            ref("aliyun", "real"),
        )
        self.assertEqual(offboard.targets_of(p), [("aliyun", ALI_ACC, "real")])

    def test_exact_names_are_exact(self):
        """`finance` / `data-tran` 是整名匹配：`finance-li`、`data-transfer` 是人，要能停。"""
        p = person(
            "某人",
            "on_x",
            ref("aliyun", "finance-li"),
            ref("aliyun", "finance2"),
            ref("volcano", "data-transfer"),
            ref("volcano", "data-tran2"),
            ref("aliyun", "xwuji-a"),
            ref("aliyun", "url-x"),
        )
        self.assertEqual(
            [t[2] for t in offboard.targets_of(p)],
            ["finance-li", "finance2", "data-transfer", "data-tran2", "xwuji-a", "url-x"],
        )

    def test_protected_regex_matches_every_listed_form(self):
        for name in (
            "panel-x",
            "power-y",
            "tempak",
            "tempak-1949-a",
            "wuji-a",
            "rl-b",
            "finance",
            "Finance",
            "data-tran",
            "DATA-TRAN",
        ):
            self.assertTrue(offboard.PROTECTED.match(name), name)

    def test_protected_is_prefix_only(self):
        """「xpanel-」不是面板身份：前缀锚定，不是子串。"""
        p = person("某人", "on_x", ref("aliyun", "xpanel-1"))
        self.assertEqual(offboard.targets_of(p), [("aliyun", ALI_ACC, "xpanel-1")])


class StrongCandidatesTests(unittest.TestCase):
    A = person("甲", "on_a", ref("aliyun", "jia"))
    B = person("乙", "on_b", ref("aliyun", "yi"))
    NOUID = Person(name="丙", email="c@wuji.tech", accounts=(ref("aliyun", "bing"),))

    @staticmethod
    def gone(st):
        return "飞书状态：已离职" if st.get("is_resigned") else ""

    def test_drift_and_status_are_merged_once_per_person(self):
        got = offboard.strong_candidates(
            [self.A, self.B, self.NOUID],
            drift_rows=[({}, {"union_id": "on_a"}), ({}, {"union_id": "on_a"})],
            statuses={"on_a": {"is_resigned": True}, "on_b": {"is_resigned": True}},
            gone=self.gone,
        )
        self.assertEqual(
            [(p.name, s) for p, s in got],
            [("甲", "IT 的 IAM 标记离职"), ("乙", "飞书状态：已离职")],
        )

    def test_in_service_and_unknown_status_are_not_candidates(self):
        got = offboard.strong_candidates(
            [self.A, self.B],
            statuses={"on_a": {}, "on_b": None},
            gone=self.gone,
        )
        self.assertEqual(got, [])

    def test_uid_not_in_roster_is_ignored(self):
        got = offboard.strong_candidates(
            [self.A],
            drift_rows=[({}, {"union_id": "on_zzz"}), ({}, {"union_id": ""})],
            statuses={"on_q": {"is_resigned": True}},
            gone=self.gone,
        )
        self.assertEqual(got, [])

    def test_default_gone_is_resigned_only(self):
        """H-1：默认判据是 `offboard.resigned`，飞书那一路只认「已离职」。"""
        got = offboard.strong_candidates(
            [self.A, self.B],
            statuses={"on_a": {"is_resigned": True}, "on_b": {"is_frozen": True}},
        )
        self.assertEqual([(p.name, s) for p, s in got], [("甲", "飞书状态：已离职")])

    def test_frozen_or_exited_are_never_strong(self):
        """冻结 = 暂停（长假、临时封禁），退出企业语义不清：自动停会误伤在职的人。"""
        got = offboard.strong_candidates(
            [self.A, self.B],
            statuses={"on_a": {"is_frozen": True}, "on_b": {"is_exited": True}},
        )
        self.assertEqual(got, [])

    def test_explicit_none_gone_ignores_statuses(self):
        got = offboard.strong_candidates(
            [self.A], statuses={"on_a": {"is_resigned": True}}, gone=None
        )
        self.assertEqual(got, [])

    def test_resigned(self):
        self.assertEqual(offboard.resigned({"is_resigned": True}), "飞书状态：已离职")
        for st in ({}, None, {"is_frozen": True}, {"is_exited": True}, {"is_resigned": False}):
            self.assertEqual(offboard.resigned(st), "", st)


class WeakStatusesTests(unittest.TestCase):
    F = person("冻", "on_f", ref("aliyun", "dong"))
    E = person("退", "on_e", ref("aliyun", "tui"))
    R = person("离", "on_r", ref("aliyun", "li"))
    S = person("在", "on_s", ref("aliyun", "zai"))

    def test_frozen_and_exited_only(self):
        got = offboard.weak_statuses(
            [self.F, self.E, self.R, self.S],
            {
                "on_f": {"is_frozen": True},
                "on_e": {"is_exited": True},
                # 离职 + 冻结 → 已经是强信号，不重复进弱信号
                "on_r": {"is_resigned": True, "is_frozen": True},
                "on_s": {},
                "on_zzz": {"is_frozen": True},
                "on_none": None,
            },
        )
        self.assertEqual(
            [(p.name, why) for p, why in got],
            [
                ("冻", "飞书状态：账号被冻结（没有自动停用）"),
                ("退", "飞书状态：已退出企业（没有自动停用）"),
            ],
        )

    def test_no_statuses(self):
        self.assertEqual(offboard.weak_statuses([self.F], None), [])


class AutoDisableTests(_Base):
    def cand(self, n, *refs):
        return (person(f"人{n}", f"on_{n}", *refs), "IT 的 IAM 标记离职")

    def test_disables_and_records(self):
        logged = []
        rep = offboard.auto_disable(
            self.path,
            [self.cand(1, ref("aliyun", "u1"), ref("volcano", "u1"))],
            self.book,
            log=lambda op, rows, actor: logged.append((op, len(rows), actor)),
        )
        self.assertEqual(len(rep["done"]), 2)
        recs = self.records()
        self.assertEqual(set(recs), {f"aliyun/{ALI_ACC}/u1", f"volcano/{VOLC_ACC}/u1"})
        r = recs[f"aliyun/{ALI_ACC}/u1"]
        self.assertEqual(r["state"], offboard.DISABLED)
        self.assertEqual(r["keys"], ["AK-u1"])
        self.assertTrue(r["login"])
        self.assertEqual(r["union_id"], "on_1")
        self.assertEqual(logged, [("offboard_disable", 2, "auto:iam-remind")])
        # 停用绝不删
        self.assertEqual(self.book.of("delete"), [])

    def test_exactly_max_people_is_allowed(self):
        cands = [self.cand(i, ref("aliyun", f"u{i}")) for i in range(offboard.MAX_AUTO_PEOPLE)]
        rep = offboard.auto_disable(self.path, cands, self.book)
        self.assertEqual(len(rep["done"]), offboard.MAX_AUTO_PEOPLE)
        self.assertEqual(rep["held"], [])

    def test_over_max_people_disables_nobody(self):
        cands = [
            self.cand(i, ref("aliyun", f"u{i}"), ref("volcano", f"u{i}"))
            for i in range(offboard.MAX_AUTO_PEOPLE + 1)
        ]
        logged = []
        rep = offboard.auto_disable(self.path, cands, self.book, log=lambda *a: logged.append(a))
        self.assertEqual(self.book.calls, [])
        self.assertEqual(rep["done"], [])
        self.assertEqual(len(rep["held"]), offboard.MAX_AUTO_PEOPLE + 1)
        self.assertEqual(logged, [])
        # M-1：一个都不停，但每个号记成嫌疑，面板上有东西可点
        recs = self.records()
        self.assertEqual(len(recs), 2 * (offboard.MAX_AUTO_PEOPLE + 1))
        for r in recs.values():
            self.assertEqual(r["state"], offboard.SUSPECT)
            self.assertEqual(r["keys"], [])
            self.assertFalse(r["login"])
            self.assertTrue(
                r["signal"].endswith("（这一轮判离职的人太多，没有自动停用）"), r["signal"]
            )

    def test_held_round_does_not_overwrite_existing_records(self):
        """被上限拦下时只 setdefault：已停用的记录（带 keys）不能被改回嫌疑。"""
        offboard.auto_disable(self.path, [self.cand(0, ref("aliyun", "u0"))], self.book)
        # u0 已停用；再来 4 个新人 + u0 的另一个新号
        cands = [self.cand(0, ref("aliyun", "u0"), ref("volcano", "u0"))] + [
            self.cand(i, ref("aliyun", f"u{i}")) for i in range(1, offboard.MAX_AUTO_PEOPLE + 1)
        ]
        self.book.calls.clear()
        rep = offboard.auto_disable(self.path, cands, self.book)
        self.assertTrue(rep["held"])
        self.assertEqual(self.book.calls, [])
        old = self.records()[f"aliyun/{ALI_ACC}/u0"]
        self.assertEqual((old["state"], old["keys"]), (offboard.DISABLED, ["AK-u0"]))

    def test_held_again_next_round_still_disables_nobody(self):
        """嫌疑是「开着的」，下一轮还会进待办；人数仍超上限 → 还是一个都不停。"""
        cands = [self.cand(i, ref("aliyun", f"u{i}")) for i in range(offboard.MAX_AUTO_PEOPLE + 1)]
        offboard.auto_disable(self.path, cands, self.book)
        rep = offboard.auto_disable(self.path, cands, self.book)
        self.assertEqual(len(rep["held"]), offboard.MAX_AUTO_PEOPLE + 1)
        self.assertEqual(self.book.calls, [])

    def test_gone_on_cloud_is_recorded_deleted_not_done(self):
        class GoneEx:
            def disable_user(self, user):
                return {"login": False, "keys": [], "gone": True}

        logged = []
        rep = offboard.auto_disable(
            self.path,
            [self.cand(1, ref("aliyun", "u1"))],
            lambda p, a: GoneEx(),
            log=lambda *a: logged.append(a),
        )
        self.assertEqual((rep["done"], rep["failed"]), ([], []))
        rec = self.records()[f"aliyun/{ALI_ACC}/u1"]
        self.assertEqual(rec["state"], offboard.DELETED)
        self.assertEqual(rec["decided_by"], "云上已不存在")
        self.assertEqual(offboard.pending(self.path), [])
        self.assertEqual([a[0] for a in logged], ["offboard_gone"])  # 进审计日志，不算 done

    def test_limit_counts_people_not_accounts(self):
        """一个人两个号算一个人：按号数算的话，两个人四个号就会被拦住。"""
        cands = [self.cand(i, ref("aliyun", f"u{i}"), ref("volcano", f"u{i}")) for i in range(3)]
        rep = offboard.auto_disable(self.path, cands, self.book)
        self.assertEqual(len(rep["done"]), 6)

    def test_already_handled_people_do_not_count_toward_limit(self):
        """已有记录的人不算进上限：否则停过的三个人会永远把第四个人挡在门外。"""
        first = [self.cand(i, ref("aliyun", f"u{i}")) for i in range(3)]
        offboard.auto_disable(self.path, first, self.book)
        self.book.calls.clear()
        rep = offboard.auto_disable(
            self.path, first + [self.cand(9, ref("aliyun", "u9"))], self.book
        )
        self.assertEqual([c[3] for c in self.book.of("disable")], ["u9"])
        self.assertEqual(sorted(rep["skipped"]), ["人0", "人1", "人2"])

    def test_restored_is_never_disabled_again(self):
        c = self.cand(1, ref("aliyun", "u1"))
        offboard.auto_disable(self.path, [c], self.book)
        offboard.decide(self.path, f"aliyun/{ALI_ACC}/u1", "restore", self.book, actor="admin:x")
        self.book.calls.clear()
        rep = offboard.auto_disable(self.path, [c], self.book)
        self.assertEqual(self.book.calls, [])
        self.assertEqual(rep["skipped"], ["人1"])
        self.assertEqual(self.records()[f"aliyun/{ALI_ACC}/u1"]["state"], offboard.RESTORED)

    def test_deleted_and_dismissed_are_not_touched(self):
        c = self.cand(1, ref("aliyun", "u1"))
        offboard.note_suspects(self.path, [c[0]])
        offboard.decide(self.path, f"aliyun/{ALI_ACC}/u1", "restore", self.book, actor="a")
        self.book.calls.clear()
        offboard.auto_disable(self.path, [c], self.book)
        self.assertEqual(self.book.calls, [])

    def test_suspect_is_upgraded_to_disabled_by_a_strong_signal(self):
        """弱信号先记了嫌疑，后来飞书 / IAM 明确标了离职 → 这时候该停。"""
        c = self.cand(1, ref("aliyun", "u1"))
        offboard.note_suspects(self.path, [c[0]])
        offboard.auto_disable(self.path, [c], self.book)
        self.assertEqual(len(self.book.of("disable")), 1)
        self.assertEqual(self.records()[f"aliyun/{ALI_ACC}/u1"]["state"], offboard.DISABLED)

    def test_one_failure_does_not_block_others_and_is_retried(self):
        self.book.fail.add("bad")
        rep = offboard.auto_disable(
            self.path,
            [
                self.cand(1, ref("aliyun", "bad"), ref("volcano", "good")),
                self.cand(2, ref("aliyun", "u2")),
            ],
            self.book,
        )
        self.assertEqual(sorted(r["user"] for r in rep["done"]), ["good", "u2"])
        self.assertEqual([r["user"] for r in rep["failed"]], ["bad"])
        self.assertIn("停用失败", rep["failed"][0]["error"])
        # M-1：一步都没做成 → 记成嫌疑，带 incomplete，面板上能直接处理
        rec = self.records()[f"aliyun/{ALI_ACC}/bad"]
        self.assertEqual(rec["state"], offboard.SUSPECT)
        self.assertIn("停用失败", rec["incomplete"])
        self.assertEqual((rec["keys"], rec["login"]), ([], False))
        # 下一轮再试这一个（嫌疑是开着的）
        self.book.fail.clear()
        self.book.calls.clear()
        offboard.auto_disable(self.path, [self.cand(1, ref("aliyun", "bad"))], self.book)
        self.assertEqual([c[3] for c in self.book.of("disable")], ["bad"])
        rec = self.records()[f"aliyun/{ALI_ACC}/bad"]
        self.assertEqual(rec["state"], offboard.DISABLED)
        self.assertNotIn("incomplete", rec)

    def test_failure_on_restored_record_is_not_retried(self):
        c = self.cand(1, ref("aliyun", "u1"))
        offboard.auto_disable(self.path, [c], self.book)
        offboard.decide(self.path, f"aliyun/{ALI_ACC}/u1", "restore", self.book, actor="a")
        self.book.fail.add("u1")
        self.book.calls.clear()
        offboard.auto_disable(self.path, [c], self.book)
        self.assertEqual(self.book.of("disable"), [])

    def test_protected_accounts_never_disabled(self):
        rep = offboard.auto_disable(
            self.path,
            [
                self.cand(
                    1,
                    ref("aliyun", "panel-executor"),
                    ref("aliyun", "power-application-user"),
                    ref("volcano", "tempak-x"),
                )
            ],
            self.book,
        )
        self.assertEqual(self.book.calls, [])
        self.assertEqual(rep["done"], [])

    def test_concurrent_runs_disable_each_account_once(self):
        """定时任务和面板是两个进程：同时跑时每个号只停一次（文件锁）。"""
        c = [self.cand(1, ref("aliyun", "u1"))]
        errs = []

        def run():
            try:
                offboard.auto_disable(self.path, c, self.book)
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=run) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(self.book.of("disable")), 1)


class NoteSuspectsTests(_Base):
    def test_records_without_touching_cloud(self):
        p = person("王五", "on_5", ref("aliyun", "wangwu"), ref("volcano", "wangwu"))
        added = offboard.note_suspects(self.path, [p])
        self.assertEqual(len(added), 2)
        self.assertTrue(all(r["state"] == offboard.SUSPECT for r in added))
        self.assertTrue(all(r["keys"] == [] and r["login"] is False for r in added))
        self.assertEqual(len(offboard.pending(self.path)), 2)
        self.assertEqual(self.book.calls, [])

    def test_does_not_overwrite_existing(self):
        c = (person("王五", "on_5", ref("aliyun", "wangwu")), "sig")
        offboard.auto_disable(self.path, [c], self.book)
        self.assertEqual(offboard.note_suspects(self.path, [c[0]]), [])
        self.assertEqual(self.records()[f"aliyun/{ALI_ACC}/wangwu"]["state"], offboard.DISABLED)


class DecideTests(_Base):
    KEY = f"aliyun/{ALI_ACC}/u1"

    def disabled(self):
        offboard.auto_disable(
            self.path, [(person("甲", "on_1", ref("aliyun", "u1")), "s")], self.book
        )
        self.book.calls.clear()

    def test_unknown_key_refused_without_cloud_call(self):
        """删号只认记录里有的号：构造一个请求删任意用户名不行。"""
        self.disabled()
        for key in (f"aliyun/{ALI_ACC}/someone", "", "aliyun//u1", f"volcano/{ALI_ACC}/u1"):
            with self.assertRaises(offboard.OffboardError, msg=key):
                offboard.decide(self.path, key, "delete", self.book, actor="a")
        self.assertEqual(self.book.calls, [])

    def test_bad_action_refused(self):
        self.disabled()
        with self.assertRaises(offboard.OffboardError):
            offboard.decide(self.path, self.KEY, "disable", self.book, actor="a")
        self.assertEqual(self.book.calls, [])

    def test_delete_clean(self):
        self.disabled()
        logged = []
        rec = offboard.decide(
            self.path,
            self.KEY,
            "delete",
            self.book,
            actor="admin:x",
            log=lambda op, rows, actor: logged.append((op, actor)),
        )
        self.assertEqual(rec["state"], offboard.DELETED)
        self.assertEqual(rec["decided_by"], "admin:x")
        self.assertEqual(self.book.of("delete"), [("aliyun", ALI_ACC, "delete", "u1")])
        self.assertEqual(self.records()[self.KEY]["state"], offboard.DELETED)
        self.assertEqual(logged, [("offboard_delete", "admin:x")])
        self.assertEqual(offboard.pending(self.path), [])

    def test_delete_with_leftovers_persists_and_raises_then_retry_clears(self):
        self.disabled()
        self.book.left["u1"] = ["摘策略 X：Throttling"]
        logged = []
        with self.assertRaises(offboard.OffboardError) as cm:
            offboard.decide(
                self.path, self.KEY, "delete", self.book, actor="a", log=lambda *x: logged.append(x)
            )
        self.assertIn("摘策略 X", str(cm.exception))
        rec = self.records()[self.KEY]
        self.assertEqual(rec["state"], offboard.DISABLED)
        self.assertEqual(rec["left"], ["摘策略 X：Throttling"])
        self.assertIn("tried_at", rec)
        self.assertEqual(logged, [], "没删成不该记成已删")
        # 仍在待办里，可以再点一次
        self.assertEqual(len(offboard.pending(self.path)), 1)
        self.book.left.clear()
        rec = offboard.decide(self.path, self.KEY, "delete", self.book, actor="a")
        self.assertEqual(rec["state"], offboard.DELETED)
        self.assertNotIn("left", self.records()[self.KEY])

    def test_executor_exception_does_not_change_record(self):
        self.disabled()

        def boom(platform, account):
            raise ProvisionError("执行身份没配")

        with self.assertRaises(ProvisionError):
            offboard.decide(self.path, self.KEY, "delete", boom, actor="a")
        self.assertEqual(self.records()[self.KEY]["state"], offboard.DISABLED)

    def test_restore_disabled_reenables_exact_keys(self):
        self.disabled()
        rec = offboard.decide(self.path, self.KEY, "restore", self.book, actor="a")
        self.assertEqual(rec["state"], offboard.RESTORED)
        self.assertEqual(self.book.calls, [("aliyun", ALI_ACC, "enable", "u1", True, ("AK-u1",))])
        self.assertEqual(self.book.of("delete"), [])

    def test_restore_suspect_is_dismissed_without_cloud_call(self):
        offboard.note_suspects(self.path, [person("甲", "on_1", ref("aliyun", "u1"))])
        rec = offboard.decide(self.path, self.KEY, "restore", self.book, actor="a")
        self.assertEqual(rec["state"], offboard.DISMISSED)
        self.assertEqual(self.book.calls, [])

    def test_decided_is_refused(self):
        self.disabled()
        offboard.decide(self.path, self.KEY, "delete", self.book, actor="a")
        self.book.calls.clear()
        for action in ("delete", "restore"):
            with self.assertRaises(offboard.OffboardError, msg=action):
                offboard.decide(self.path, self.KEY, action, self.book, actor="a")
        self.assertEqual(self.book.calls, [])

    def test_protected_record_refused_even_if_in_file(self):
        """文件被手工塞进一条 panel- 的记录，面板也不删。"""
        key = f"aliyun/{ALI_ACC}/panel-executor"
        self.path.write_text(
            json.dumps(
                {
                    "records": {
                        key: {
                            "platform": "aliyun",
                            "account": ALI_ACC,
                            "user": "panel-executor",
                            "state": "disabled",
                            "keys": ["AK"],
                            "login": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        for action in ("delete", "restore"):
            with self.assertRaises(offboard.OffboardError):
                offboard.decide(self.path, key, action, self.book, actor="a")
        self.assertEqual(self.book.calls, [])

    def test_concurrent_deletes_hit_cloud_once(self):
        self.disabled()
        results = []

        def run():
            try:
                offboard.decide(self.path, self.KEY, "delete", self.book, actor="a")
                results.append("ok")
            except offboard.OffboardError:
                results.append("refused")

        ts = [threading.Thread(target=run) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), ["ok"] + ["refused"] * 4)
        self.assertEqual(len(self.book.of("delete")), 1)


class EnsureRecordTests(_Base):
    KEY = f"aliyun/{ALI_ACC}/u1"

    def ensure(self):
        return offboard.ensure_record(
            self.path,
            platform="aliyun",
            account=ALI_ACC,
            user="u1",
            person="甲",
            signal="管理员确认离职",
        )

    def test_creates_suspect(self):
        self.assertEqual(self.ensure(), self.KEY)
        self.assertEqual(self.records()[self.KEY]["state"], offboard.SUSPECT)

    def test_keeps_pending_disabled_record_with_its_keys(self):
        """已停用的号保留原记录：恢复要靠里面的 keys。"""
        offboard.auto_disable(
            self.path, [(person("甲", "on_1", ref("aliyun", "u1")), "s")], self.book
        )
        self.ensure()
        rec = self.records()[self.KEY]
        self.assertEqual(rec["state"], offboard.DISABLED)
        self.assertEqual(rec["keys"], ["AK-u1"])

    def test_does_not_resurrect_a_deleted_record(self):
        """回归（曾是 bug）：`ensure_record` 曾对非 PENDING 的记录一律覆盖成 SUSPECT
        —— 包括 DELETED。
        已删的号再走一次「确认离职」会被改回待办，删号审计信息（decided_by/decided_at）丢失，
        而且再删会撞上「用户不存在」
        （见 AliyunDeleteTests.test_already_gone_user_is_not_an_error），
        这条记录就一直挂在待办里。offboard.py:295。"""
        offboard.note_suspects(self.path, [person("甲", "on_1", ref("aliyun", "u1"))])
        offboard.decide(self.path, self.KEY, "delete", self.book, actor="admin:x")
        self.ensure()
        rec = self.records()[self.KEY]
        self.assertEqual(rec["state"], offboard.DELETED)


class EndToEndAliyunTests(_Base):
    """offboard + 真 AliyunExecutor + 有状态假云：停用 → 恢复 → 云上状态回到原样。"""

    def test_disable_then_restore_roundtrip(self):
        cloud = AliyunCloud(keys={"LTAIa1": "Active", "LTAIb2": "Inactive"})
        before = dict(cloud.keys)
        c = (person("甲", "on_1", ref("aliyun", "u1")), "s")
        offboard.auto_disable(self.path, [c], lambda p, a: cloud.executor())
        self.assertFalse(cloud.login)
        offboard.decide(
            self.path, f"aliyun/{ALI_ACC}/u1", "restore", lambda p, a: cloud.executor(), actor="a"
        )
        self.assertTrue(cloud.login)
        self.assertEqual(cloud.keys, before)
        self.assertLessEqual(set(cloud.actions), ALIYUN_ALLOWED)

    def test_partial_disable_does_not_lose_the_login_closure(self):
        """回归（曾是 bug）：阿里 `disable_user` 先删登录配置、再禁 AK。禁 AK 失败时异常穿出，
        `auto_disable` 不落任何记录 → 「登录已被关」这件事丢了。下一轮重试时登录配置已经
        不存在，记成 `login=False`；管理员点「恢复」只开 AK，**控制台登录回不来**。
        provision.py AliyunExecutor.disable_user（DeleteLoginProfile 在 UpdateAccessKey 之前、
        且没有部分结果）+ offboard.py auto_disable 的 except 分支（不记录部分进度）。
        火山同理（UpdateLoginProfile LoginAllowed=false 之后禁 AK 失败）。"""
        cloud = AliyunCloud(keys={"LTAIa1": "Active"})
        cloud.fail["UpdateAccessKey"] = "ServiceUnavailable"
        c = (person("甲", "on_1", ref("aliyun", "u1")), "s")
        rep = offboard.auto_disable(self.path, [c], lambda p, a: cloud.executor())
        self.assertEqual(len(rep["failed"]), 1)
        del cloud.fail["UpdateAccessKey"]
        offboard.auto_disable(self.path, [c], lambda p, a: cloud.executor())
        offboard.decide(
            self.path, f"aliyun/{ALI_ACC}/u1", "restore", lambda p, a: cloud.executor(), actor="a"
        )
        self.assertTrue(cloud.login, "恢复后控制台登录应当回来")

    def test_volcano_partial_disable_is_resumed_and_fully_restored(self):
        """火山：关了登录开关后列 AK 失败 → 记 disabled + incomplete（login=True）；
        下一轮补完 AK；恢复时登录开关和 AK 都回来。"""
        cloud = VolcanoCloud(keys={"AKLTa1": "active"})
        cloud.fail["ListAccessKeys"] = "InternalError"
        c = (person("甲", "on_1", ref("volcano", "u1")), "s")
        key = f"volcano/{VOLC_ACC}/u1"
        rep = offboard.auto_disable(self.path, [c], lambda p, a: cloud.executor())
        self.assertEqual(len(rep["failed"]), 1)
        rec = self.records()[key]
        self.assertEqual((rec["state"], rec["login"]), (offboard.DISABLED, True))
        self.assertIn("incomplete", rec)
        del cloud.fail["ListAccessKeys"]
        offboard.auto_disable(self.path, [c], lambda p, a: cloud.executor())
        rec = self.records()[key]
        self.assertEqual((rec["login"], rec["keys"]), (True, ["AKLTa1"]))
        self.assertNotIn("incomplete", rec)
        self.assertEqual(cloud.keys, {"AKLTa1": "inactive"})
        offboard.decide(self.path, key, "restore", lambda p, a: cloud.executor(), actor="a")
        self.assertTrue(cloud.login_allowed)
        self.assertEqual(cloud.keys, {"AKLTa1": "active"})

    def test_user_gone_on_cloud_becomes_deleted_via_real_executor(self):
        cloud = AliyunCloud()
        cloud.exists = False
        c = (person("甲", "on_1", ref("aliyun", "u1")), "s")
        offboard.auto_disable(self.path, [c], lambda p, a: cloud.executor())
        self.assertEqual(self.records()[f"aliyun/{ALI_ACC}/u1"]["state"], offboard.DELETED)

    def test_delete_via_decide_removes_account_and_no_data_api(self):
        cloud = AliyunCloud(groups=["g"], policies=[pol("P")], mfa=True)
        offboard.note_suspects(self.path, [person("甲", "on_1", ref("aliyun", "u1"))])
        offboard.decide(
            self.path, f"aliyun/{ALI_ACC}/u1", "delete", lambda p, a: cloud.executor(), actor="a"
        )
        self.assertFalse(cloud.exists)
        self.assertLessEqual(set(cloud.actions), ALIYUN_ALLOWED)
        self.assertEqual(cloud.hosts, {"ram.aliyuncs.com", "sts.aliyuncs.com"})


class UnverifiedAndGoneTests(_Base):
    """名册核对不上的号面板不删；云上已不存在的号记进审计日志；弱信号新记录发卡。"""

    def test_unverified_record_cannot_be_deleted_but_can_be_dismissed(self):
        k = offboard.ensure_record(
            self.path,
            platform="aliyun",
            account=ALI_ACC,
            user="zhangsan",
            person="李四",
            union_id="on_li",
            signal="管理员确认离职，但名册里这个号不归他（没删）",
            verified=False,
        )
        with self.assertRaises(offboard.OffboardError) as cm:
            offboard.decide(self.path, k, "delete", self.book, actor="admin:x")
        self.assertIn("不归他", str(cm.exception))
        self.assertEqual(self.book.of("delete"), [])
        rec = offboard.decide(self.path, k, "restore", self.book, actor="admin:x")
        self.assertEqual(rec["state"], offboard.DISMISSED)

    def test_verified_record_has_no_unverified_flag(self):
        k = offboard.ensure_record(
            self.path, platform="aliyun", account=ALI_ACC, user="u1", signal="管理员确认离职"
        )
        self.assertNotIn("unverified", self.records()[k])

    def test_gone_user_is_logged(self):
        logged = []

        class GoneBook(Book):
            def __call__(self, platform, account):
                ex = FakeEx(self, platform, account)
                ex.disable_user = lambda user: {"login": False, "keys": [], "gone": True}
                return ex

        offboard.auto_disable(
            self.path,
            [(person("甲", "on_1", ref("aliyun", "u1")), "IT 的 IAM 标记离职")],
            GoneBook(),
            log=lambda op, rows, actor: logged.append((op, [r["user"] for r in rows])),
        )
        self.assertEqual(logged, [("offboard_gone", ["u1"])])

    def test_suspect_card_lists_new_weak_records(self):
        from delivery import notify

        card = notify.offboard_card(
            {
                "done": [],
                "failed": [],
                "held": [],
                "suspects": [
                    {
                        "person": "甲",
                        "platform": "aliyun",
                        "user": "u1",
                        "signal": "飞书状态：账号被冻结（没有自动停用）",
                    }
                ],
            }
        )
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("待确认", text)
        self.assertIn("甲", text)
        self.assertIn("账号被冻结", text)
        # 没有失败、没有被拦下 → 这不是告警，是待办
        self.assertEqual(card["header"]["template"], "blue")


class ManualPlatformTests(_Base):
    """九章：面板停不了也删不了，只记待办；管理员在控制台处理完点一下销账。"""

    KEY = "jiuzhang/wuji/wuji-wangyuran"

    def cand(self):
        return (
            person("王昱然", "on_w", ref("jiuzhang", "wuji-wangyuran", account="wuji")),
            "IT 的 IAM 标记离职",
        )

    def test_auto_disable_records_it_without_touching_any_cloud(self):
        rep = offboard.auto_disable(self.path, [self.cand()], self.book)
        self.assertEqual(self.book.calls, [])
        self.assertEqual(rep["done"], [])
        # **要进 report**：卡片只发 report 里的东西，不放等于九章的人在飞书上
        # 一个字都不会出现（审计 Med-1，正是王昱然那一类）
        self.assertEqual([r["user"] for r in rep["manual"]], ["wuji-wangyuran"])
        rec = self.records()[self.KEY]
        self.assertEqual(rec["state"], offboard.SUSPECT)
        self.assertIn("控制台", rec["signal"])

    def test_an_already_recorded_manual_account_is_not_re_announced(self):
        """记录本身就是去重：第二轮不该再把同一个号塞进卡片。"""
        offboard.auto_disable(self.path, [self.cand()], self.book)
        again = offboard.auto_disable(self.path, [self.cand()], self.book)
        self.assertEqual(again.get("manual") or [], [])

    def test_manual_does_not_eat_the_auto_disable_limit(self):
        many = [
            (person(f"人{i}", f"on_{i}", ref("jiuzhang", f"wuji-u{i}", account="wuji")), "s")
            for i in range(6)
        ]
        rep = offboard.auto_disable(self.path, many, self.book)
        self.assertEqual(rep["held"], [])
        self.assertEqual(len(self.records()), 6)

    def test_confirm_marks_handled_without_cloud_calls(self):
        offboard.auto_disable(self.path, [self.cand()], self.book)
        logged = []
        rec = offboard.decide(
            self.path,
            self.KEY,
            "delete",
            self.book,
            actor="admin:x",
            log=lambda op, rows, actor: logged.append(op),
        )
        self.assertEqual(rec["state"], offboard.DELETED)
        self.assertEqual(rec["by_hand"], "九章")
        self.assertEqual(self.book.calls, [])
        self.assertEqual(logged, ["offboard_delete_by_hand"])
        self.assertEqual(offboard.pending(self.path), [])

    def test_not_departed_dismisses_it(self):
        offboard.auto_disable(self.path, [self.cand()], self.book)
        rec = offboard.decide(self.path, self.KEY, "restore", self.book, actor="admin:x")
        self.assertEqual(rec["state"], offboard.DISMISSED)
        self.assertEqual(self.book.calls, [])


if __name__ == "__main__":
    unittest.main()
