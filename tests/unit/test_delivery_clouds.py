"""两朵云的零依赖 API 客户端。

签名错了云厂商只会回 403，而 403 和「权限不足」长得一模一样。所以签名算法用
官方文档里的固定样例锁住；错误分类与翻页守卫用假传输层锁住，不连真云。
"""

from __future__ import annotations

import unittest
import urllib.parse

from delivery.clouds import aliyun, volcano


class AliyunSignTests(unittest.TestCase):
    def test_matches_the_documented_example(self):
        """阿里云 RPC 签名文档里的经典样例（ECS DescribeRegions）。"""
        params = {
            "Format": "XML",
            "AccessKeyId": "testid",
            "Action": "DescribeRegions",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
            "SignatureVersion": "1.0",
            "Version": "2014-05-26",
            "Timestamp": "2016-02-23T12:46:24Z",
        }
        self.assertEqual(aliyun.sign(params, "testsecret"), "OLeaidS1JvxuMvnyHOwuJ+uX5qY=")

    def test_parameter_order_does_not_matter(self):
        a = {"b": "2", "a": "1"}
        self.assertEqual(aliyun.sign(a, "k"), aliyun.sign(dict(reversed(list(a.items()))), "k"))

    def test_space_and_tilde_encoding(self):
        """空格必须是 %20（不是 +），波浪号不能转义——两处写反都会签名不匹配。"""
        self.assertEqual(aliyun._quote("a b~c*"), "a%20b~c%2A")


class _AliyunFake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.responses.pop(0)


CREDS = aliyun.Credentials("LTAItestkey1234", "super-secret-value")


class AliyunCallTests(unittest.TestCase):
    def test_success_returns_body(self):
        fake = _AliyunFake((200, {"User": {"UserName": "a"}}))
        body = aliyun.call(*aliyun.RAM, "GetUser", {"UserName": "a"}, creds=CREDS, transport=fake)
        self.assertEqual(body["User"]["UserName"], "a")

    def test_request_is_signed_and_carries_action(self):
        fake = _AliyunFake((200, {}))
        aliyun.call(*aliyun.RAM, "ListUsers", creds=CREDS, transport=fake)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(fake.urls[0]).query))
        self.assertEqual(q["Action"], "ListUsers")
        self.assertIn("Signature", q)
        self.assertEqual(q["AccessKeyId"], CREDS.access_key_id)

    def test_secret_never_appears_in_the_url(self):
        fake = _AliyunFake((200, {}))
        aliyun.call(*aliyun.RAM, "ListUsers", creds=CREDS, transport=fake)
        self.assertNotIn("super-secret-value", fake.urls[0])

    def test_permission_error_is_classified(self):
        fake = _AliyunFake((403, {"Code": "NoPermission", "Message": "denied"}))
        with self.assertRaises(aliyun.AliyunDenied):
            aliyun.call(*aliyun.RAM, "ListUsers", creds=CREDS, transport=fake)

    def test_transient_error_is_not_mislabelled_as_permission(self):
        fake = _AliyunFake((503, {"Code": "ServiceUnavailable", "Message": "busy"}))
        with self.assertRaises(aliyun.AliyunError) as ctx:
            aliyun.call(*aliyun.RAM, "ListUsers", creds=CREDS, transport=fake)
        self.assertNotIsInstance(ctx.exception, aliyun.AliyunDenied)

    def test_error_message_never_contains_the_secret(self):
        fake = _AliyunFake((403, {"Code": "NoPermission", "Message": "denied"}))
        with self.assertRaises(aliyun.AliyunError) as ctx:
            aliyun.call(*aliyun.RAM, "ListUsers", creds=CREDS, transport=fake)
        self.assertNotIn("super-secret-value", str(ctx.exception))

    def test_credentials_repr_hides_secret(self):
        self.assertNotIn("super-secret-value", repr(CREDS))


class AliyunPaginateTests(unittest.TestCase):
    def test_follows_marker_until_not_truncated(self):
        fake = _AliyunFake(
            (200, {"Users": {"User": [{"n": 1}]}, "IsTruncated": True, "Marker": "m1"}),
            (200, {"Users": {"User": [{"n": 2}]}, "IsTruncated": False}),
        )
        out = aliyun.paginate(
            *aliyun.RAM, "ListUsers", key="User", container="Users", creds=CREDS, transport=fake
        )
        self.assertEqual([x["n"] for x in out], [1, 2])

    def test_missing_container_is_an_error_not_an_empty_list(self):
        fake = _AliyunFake((200, {"Something": {}}))
        with self.assertRaises(aliyun.AliyunError):
            aliyun.paginate(
                *aliyun.RAM, "ListUsers", key="User", container="Users", creds=CREDS, transport=fake
            )

    def test_repeated_marker_raises_instead_of_returning_partial(self):
        # IsTruncated 为真但 Marker 不前进：只拿到了一部分，必须报错而不是静默返回半份
        fake = _AliyunFake(
            (200, {"Users": {"User": [1]}, "IsTruncated": True, "Marker": "m"}),
            (200, {"Users": {"User": [2]}, "IsTruncated": True, "Marker": "m"}),
        )
        with self.assertRaises(aliyun.AliyunError):
            aliyun.paginate(
                *aliyun.RAM, "ListUsers", key="User", container="Users", creds=CREDS, transport=fake
            )

    def test_truncated_with_empty_marker_raises(self):
        fake = _AliyunFake(
            (200, {"Users": {"User": [1]}, "IsTruncated": True, "Marker": "m1"}),
            (200, {"Users": {"User": [2]}, "IsTruncated": True, "Marker": ""}),
        )
        with self.assertRaises(aliyun.AliyunError):
            aliyun.paginate(
                *aliyun.RAM, "ListUsers", key="User", container="Users", creds=CREDS, transport=fake
            )


class VolcanoSignTests(unittest.TestCase):
    def test_signature_is_deterministic(self):
        kw = dict(
            params={"Action": "ListUsers", "Version": "2018-01-01"},
            secret="s",
            region="cn-beijing",
            service="iam",
            xdate="20260914T000000Z",
        )
        self.assertEqual(volcano.sign(**kw), volcano.sign(**kw))
        self.assertEqual(len(volcano.sign(**kw)), 64)

    def test_any_input_change_changes_the_signature(self):
        base = dict(
            params={"Action": "ListUsers"},
            secret="s",
            region="cn-beijing",
            service="iam",
            xdate="20260914T000000Z",
        )
        sig = volcano.sign(**base)
        for field, value in (
            ("secret", "t"),
            ("region", "cn-shanghai"),
            ("service", "tos"),
            ("xdate", "20260915T000000Z"),
        ):
            changed = dict(base, **{field: value})
            self.assertNotEqual(volcano.sign(**changed), sig, field)

    def test_canonical_query_sorts_and_encodes(self):
        self.assertEqual(volcano.canonical_query({"b": "x y", "a": "1"}), "a=1&b=x%20y")


class _VolcanoFake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        return self.responses.pop(0)


VCREDS = volcano.Credentials("AKLTtestkey", "volc-secret-value")


class VolcanoCallTests(unittest.TestCase):
    def test_returns_result(self):
        fake = _VolcanoFake((200, {"Result": {"UserMetadata": []}}))
        self.assertEqual(
            volcano.call(*volcano.IAM, "ListUsers", creds=VCREDS, transport=fake),
            {"UserMetadata": []},
        )

    def test_authorization_header_uses_request_scope(self):
        """火山的签名域结尾是 request，不是 AWS 的 aws4_request。"""
        fake = _VolcanoFake((200, {"Result": {}}))
        volcano.call(*volcano.IAM, "ListUsers", creds=VCREDS, transport=fake)
        auth = fake.calls[0][1]["Authorization"]
        self.assertIn("/cn-beijing/iam/request", auth)
        self.assertNotIn("volc-secret-value", auth)

    def test_denied(self):
        fake = _VolcanoFake((403, {"ResponseMetadata": {"Error": {"Code": "AccessDenied"}}}))
        with self.assertRaises(volcano.VolcanoDenied):
            volcano.call(*volcano.IAM, "ListUsers", creds=VCREDS, transport=fake)

    def test_missing_result_is_an_error(self):
        fake = _VolcanoFake((200, {"ResponseMetadata": {}}))
        with self.assertRaises(volcano.VolcanoError):
            volcano.call(*volcano.IAM, "ListUsers", creds=VCREDS, transport=fake)


class VolcanoPaginateTests(unittest.TestCase):
    def test_wrong_key_raises_instead_of_returning_empty(self):
        """实测踩过：ListUsersForGroup 回的是 Users 不是 UserMetadata，
        猜错键名把 6 个用户组全报成 0 人。"""
        fake = _VolcanoFake((200, {"Result": {"Users": [{"UserName": "a"}]}}))
        with self.assertRaises(volcano.VolcanoError) as ctx:
            volcano.paginate(
                *volcano.IAM, "ListUsersForGroup", key="UserMetadata", creds=VCREDS, transport=fake
            )
        self.assertIn("Users", str(ctx.exception))

    def test_stops_on_short_page(self):
        fake = _VolcanoFake(
            (200, {"Result": {"UserMetadata": [1, 2]}}),
            (200, {"Result": {"UserMetadata": [3]}}),
        )
        out = volcano.paginate(
            *volcano.IAM, "ListUsers", key="UserMetadata", creds=VCREDS, transport=fake, limit=2
        )
        self.assertEqual(out, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
