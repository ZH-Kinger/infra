"""两朵云实时采集。

最容易出错的一处：阿里云查安全邮箱时，「这个用户没绑邮箱」和「没权限查」
都会让调用失败。前者是正常情况，后者必须中断——混在一起的话，
权限一掉，清单上所有人都会变成「没有邮箱」，看起来还挺合理。
"""

from __future__ import annotations

import unittest
import urllib.parse

from delivery.clouds import aliyun, volcano
from delivery.identity.cloudcollect import collect_aliyun, collect_volcano
from delivery.identity.ssomap import SOURCE_RAM_EMAIL, SOURCE_SECURITY_EMAIL

CREDS = aliyun.Credentials("LTAItest", "secret")
VCREDS = volcano.Credentials("AKLTtest", "secret")


class AliyunFake:
    def __init__(self, users, details, verification):
        self.users = users
        self.details = details
        self.verification = verification

    def __call__(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        action = q["Action"]
        if action == "GetCallerIdentity":
            return 200, {"AccountId": "1000000000000001"}
        if action == "ListUsers":
            return 200, {
                "Users": {"User": [{"UserName": u} for u in self.users]},
                "IsTruncated": False,
            }
        if action == "GetUser":
            return 200, {"User": self.details[q["UserName"]]}
        if action == "GetVerificationInfo":
            name = q["UserPrincipalName"].split("@")[0]
            return self.verification[name]
        raise AssertionError(action)


class AliyunCollectTests(unittest.TestCase):
    def test_reads_both_ram_email_and_security_email(self):
        fake = AliyunFake(
            ["a", "b"],
            {
                "a": {"UserName": "a", "DisplayName": "甲", "Email": "a@wuji.tech"},
                "b": {"UserName": "b", "DisplayName": "乙", "Email": ""},
            },
            {
                "a": (200, {}),
                "b": (200, {"SecurityEmailDevice": {"Email": "b@wuji.tech", "Status": "pending"}}),
            },
        )
        got = {x.name: x for x in collect_aliyun(CREDS, transport=fake)}
        self.assertEqual(got["a"].scope, "aliyun/1000000000000001")
        self.assertEqual(got["a"].emails[0].source, SOURCE_RAM_EMAIL)
        self.assertEqual(got["b"].emails[0].source, SOURCE_SECURITY_EMAIL)
        self.assertFalse(got["b"].emails[0].verified)

    def test_active_security_email_is_verified(self):
        fake = AliyunFake(
            ["a"],
            {"a": {"UserName": "a"}},
            {"a": (200, {"SecurityEmailDevice": {"Email": "a@wuji.tech", "Status": "active"}})},
        )
        self.assertTrue(collect_aliyun(CREDS, transport=fake)[0].emails[0].verified)

    def test_no_security_email_bound_is_normal(self):
        fake = AliyunFake(
            ["a"],
            {"a": {"UserName": "a"}},
            {"a": (404, {"Code": "EntityNotExist.SecurityEmailDevice", "Message": "not bound"})},
        )
        self.assertEqual(collect_aliyun(CREDS, transport=fake)[0].emails, ())

    def test_permission_denied_on_security_email_aborts(self):
        """权限一掉，所有人都会变成「没有邮箱」——必须中断，不能产出这种清单。"""
        fake = AliyunFake(
            ["a"],
            {"a": {"UserName": "a"}},
            {"a": (403, {"Code": "NoPermission", "Message": "denied"})},
        )
        with self.assertRaises(aliyun.AliyunDenied):
            collect_aliyun(CREDS, transport=fake)

    def test_other_errors_on_security_email_abort(self):
        fake = AliyunFake(
            ["a"],
            {"a": {"UserName": "a"}},
            {"a": (500, {"Code": "InternalError", "Message": "boom"})},
        )
        with self.assertRaises(aliyun.AliyunError):
            collect_aliyun(CREDS, transport=fake)

    def test_getuser_without_user_object_aborts(self):
        class Broken(AliyunFake):
            def __call__(self, url):
                if "Action=GetUser" in url:
                    return 200, {"NotUser": {}}
                return super().__call__(url)

        fake = Broken(["a"], {"a": {}}, {"a": (200, {})})
        with self.assertRaises(aliyun.AliyunError):
            collect_aliyun(CREDS, transport=fake)


class VolcanoCollectTests(unittest.TestCase):
    def fake(self, users):
        def transport(url, headers):
            return 200, {"Result": {"UserMetadata": users}}

        return transport

    def test_reads_email_and_verification(self):
        got = collect_volcano(
            VCREDS,
            transport=self.fake(
                [
                    {
                        "UserName": "SiLi",
                        "DisplayName": "李四",
                        "Email": "li.si@wuji.tech",
                        "EmailIsVerify": True,
                        "AccountId": 2000000001,
                    },
                    {
                        "UserName": "xiaoqi",
                        "DisplayName": "孙小七",
                        "Email": "",
                        "AccountId": 2000000001,
                    },
                ]
            ),
        )
        by = {x.name: x for x in got}
        self.assertEqual(by["SiLi"].scope, "volcano/2000000001")
        self.assertTrue(by["SiLi"].emails[0].verified)
        self.assertEqual(by["xiaoqi"].emails, ())

    def test_mixed_account_ids_are_rejected(self):
        with self.assertRaises(volcano.VolcanoError):
            collect_volcano(
                VCREDS,
                transport=self.fake(
                    [
                        {"UserName": "a", "AccountId": 1},
                        {"UserName": "b", "AccountId": 2},
                    ]
                ),
            )


if __name__ == "__main__":
    unittest.main()
