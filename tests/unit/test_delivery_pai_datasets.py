"""PAI 数据集采集：这是**唯一一类自带归属的资产**，所以归属判定必须准。

资源中心对 ECS/OSS 一个归属标签都不给（实测 6319 个资源零命中），那些只能人工一条条指；
而数据集的 `UserId` 就是建它的那个 RAM 用户 —— 归属是白来的。正因为白来的，
判错了也没人会去核对，所以这里把三种属主钉死：

  · `user` 认出来了     · `root` 主账号建的公共目录     · `gone` RAM 里查无此人

混成两种的代价实测过：把主账号建的 13 条公共目录算进「属主不在了」，那一栏 18 条里
有 13 条是噪音，而真正该看的只有 5 条。一份四分之三是噪音的清单没人会看第二遍。

离线，数据虚构。
"""

from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from delivery import assets
from delivery.clouds import aliyun

ACC = "1704065796538912"
HZ = "aiworkspace.cn-hangzhou.aliyuncs.com"


class Fake:
    """一个 transport 同时喂 RPC 和 ROA —— `headers` 给默认值即可。"""

    def __init__(self, *, workspaces=None, datasets=None, users=None, fail_regions=()):
        self.workspaces = workspaces or {}
        self.datasets = datasets or {}
        self.users = users or []
        self.fail_regions = set(fail_regions)
        self.seen: list = []

    def __call__(self, url, headers=None):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.seen.append((parts.netloc, parts.path, query.get("Action", "")))
        if parts.netloc.startswith("sts."):
            return 200, {"AccountId": ACC}
        if parts.netloc.startswith("ram."):
            return 200, {"IsTruncated": False, "Users": {"User": self.users}}
        region = parts.netloc.split(".")[1]
        if region in self.fail_regions:
            return 404, {"Code": "InvalidRegion", "Message": "region not opened"}
        if parts.path == "/api/v1/workspaces":
            return 200, {"Workspaces": self.workspaces.get(region, [])}
        if parts.path == "/api/v1/datasets":
            return 200, {"Datasets": self.datasets.get(query.get("WorkspaceId"), [])}
        if parts.path.endswith("/members"):
            return 200, {"Members": []}
        raise AssertionError(f"没想到的请求 {url}")


def dataset(name, uid, *, uri=None, acc="ROLE_PUBLIC"):
    return {
        "DatasetId": f"d-{name}",
        "Name": name,
        "UserId": uid,
        "DataSourceType": "BMCPFS",
        "Accessibility": acc,
        "Uri": uri or f"bmcpfs://cpfs-x-vpc-y.cn-hangzhou.cpfs.aliyuncs.com/{name}/",
    }


def ram_user(uid, login, name):
    return {"UserId": uid, "UserName": login, "DisplayName": name}


class OwnerTests(unittest.TestCase):
    def collect(self, **kw):
        fake = Fake(**kw)
        sets, skipped = assets.collect_pai_datasets(
            aliyun.Credentials("ak", "sk"), regions=("cn-hangzhou",), transport=fake
        )
        return sets, skipped, fake

    def test_the_three_owner_kinds_never_collapse(self):
        sets, _, _ = self.collect(
            workspaces={"cn-hangzhou": [{"WorkspaceId": "1", "WorkspaceName": "hz"}]},
            datasets={
                "1": [
                    dataset("wzh", "200"),  # 在 RAM 里
                    dataset("share", ACC),  # 主账号建的
                    dataset("zhujl", "999"),  # RAM 里查无此人
                ]
            },
            users=[ram_user("200", "wangzihan", "王梓涵")],
        )
        got = {d["name"]: d["owner_kind"] for d in sets}
        self.assertEqual(got, {"wzh": "user", "share": "root", "zhujl": "gone"})

    def test_a_resolved_owner_carries_the_ram_login(self):
        """登录名是面板认人的钥匙 —— 没有它这份清单还是「不知道是谁的」。"""
        sets, _, _ = self.collect(
            workspaces={"cn-hangzhou": [{"WorkspaceId": "1", "WorkspaceName": "hz"}]},
            datasets={"1": [dataset("wzh", "200")]},
            users=[ram_user("200", "wangzihan", "王梓涵")],
        )
        self.assertEqual(sets[0]["owner_login"], "wangzihan")
        self.assertEqual(sets[0]["owner_name"], "王梓涵")

    def test_ownership_comes_from_ram_not_from_workspace_membership(self):
        """被移出工作空间的人，号还在、数据集还在 —— 不能报成「人已经没了」。

        两件事的处置完全不同：前者是权限调整，后者要找人确认数据还要不要。
        假 transport 的成员表恒为空，所以这条能通过就说明归属没走成员表。
        """
        sets, _, _ = self.collect(
            workspaces={"cn-hangzhou": [{"WorkspaceId": "1", "WorkspaceName": "hz"}]},
            datasets={"1": [dataset("laoz", "300")]},
            users=[ram_user("300", "laozhang", "老张")],
        )
        self.assertEqual(sets[0]["owner_kind"], "user")
        self.assertEqual(sets[0]["owner_login"], "laozhang")


class ShapeTests(unittest.TestCase):
    def test_both_uri_spellings_yield_the_same_path(self):
        """现网两种写法都有，只认长的会让短的那条路径变成空串。"""
        self.assertEqual(
            assets._pai_path("bmcpfs://cpfs-x-vpc-y.cn-hangzhou.cpfs.aliyuncs.com/wzh/"), "/wzh"
        )
        self.assertEqual(assets._pai_path("bmcpfs://bmcpfs-0001.cn-hangzhou/jichuan/"), "/jichuan")
        self.assertEqual(
            assets._pai_path("oss://b.oss-ap-southeast-1.aliyuncs.com/pre/lerobot/"), "/pre/lerobot"
        )
        self.assertEqual(assets._pai_path("bmcpfs://host-only"), "")
        self.assertEqual(assets._pai_path(""), "")

    def test_a_closed_region_is_skipped_with_a_note_not_silently(self):
        """没开通某个地区是常态，不该让整次采集失败；但**得记一笔** ——
        不记的话，「这个地区没有数据集」和「这个地区压根没问过」在结果里长得一样。"""
        fake = Fake(
            workspaces={"cn-hangzhou": [{"WorkspaceId": "1", "WorkspaceName": "hz"}]},
            datasets={"1": [dataset("wzh", "200")]},
            users=[ram_user("200", "wangzihan", "王梓涵")],
            fail_regions={"cn-shenzhen"},
        )
        sets, skipped = assets.collect_pai_datasets(
            aliyun.Credentials("ak", "sk"),
            regions=("cn-hangzhou", "cn-shenzhen"),
            transport=fake,
        )
        self.assertEqual([d["name"] for d in sets], ["wzh"])
        self.assertEqual(len(skipped), 1)
        self.assertIn("cn-shenzhen", skipped[0])

    def test_permission_denied_stops_everything(self):
        """缺权限和「这个地区没开通」不是一回事：前者会让**所有**地区都采不到，
        当成「跳过」的话，一次空采集会被写成一份干净的空清单。"""

        def denied(url, headers=None):
            if urlsplit(url).netloc.startswith(("sts.", "ram.")):
                return 200, {"AccountId": ACC, "IsTruncated": False, "Users": {"User": []}}
            return 403, {"Code": "NoPermission", "Message": "You are not authorized"}

        with self.assertRaises(aliyun.AliyunDenied):
            assets.collect_pai_datasets(
                aliyun.Credentials("ak", "sk"), regions=("cn-hangzhou",), transport=denied
            )


class SnapshotTests(unittest.TestCase):
    def test_not_collected_is_not_the_same_as_none(self):
        """采不到时快照里**没有 datasets 这个键**，不是空列表。

        写成空列表的话，PAI 权限哪天掉了，所有人的数据集会一起从页面上消失，
        而页面会理直气壮地说「你没有数据集」—— 和 AK 台账那条是同一个坑。
        """
        bare = assets.build_snapshot([])
        self.assertNotIn("datasets", bare)

        empty = assets.build_snapshot([], datasets=[])
        self.assertEqual(empty["datasets"], [])

        failed = assets.build_snapshot([], dataset_error="没权限")
        self.assertNotIn("datasets", failed)
        self.assertIn("没权限", failed["dataset_error"])

    def test_the_snapshot_survives_a_json_round_trip(self):
        data = assets.build_snapshot([], datasets=[dataset("wzh", "200")])
        self.assertEqual(json.loads(json.dumps(data))["datasets"][0]["Name"], "wzh")


class RoaSigningTests(unittest.TestCase):
    """ROA 和 RPC 是两套签名。混用的报错是 SignatureDoesNotMatch，
    而错误信息里不会告诉你是风格用错了 —— 所以把关键几处钉住。"""

    def send(self):
        seen = {}

        def transport(url, headers=None):
            seen["url"], seen["headers"] = url, headers or {}
            return 200, {"ok": True}

        return transport, seen

    def test_it_signs_with_an_acs_authorization_header(self):
        transport, seen = self.send()
        aliyun.call_roa(
            HZ,
            "2021-02-04",
            "/api/v1/workspaces",
            {"PageSize": 50},
            creds=aliyun.Credentials("AKID", "secret"),
            transport=transport,
        )
        self.assertTrue(seen["headers"]["authorization"].startswith("acs AKID:"))
        self.assertEqual(seen["headers"]["x-acs-version"], "2021-02-04")
        self.assertIn("PageSize=50", seen["url"])

    def test_a_security_token_is_signed_in_not_just_attached(self):
        """临时凭证走成员账号采集时会用到。只加头不进签名的话，服务端会拒。"""
        transport, seen = self.send()
        creds = aliyun.Credentials("AKID", "secret", security_token="tok")
        a = aliyun.call_roa(HZ, "v", "/p", creds=creds, transport=transport)
        first = seen["headers"]["authorization"]
        self.assertEqual(seen["headers"]["x-acs-security-token"], "tok")
        self.assertTrue(a["ok"])

        transport2, seen2 = self.send()
        aliyun.call_roa(
            HZ, "v", "/p", creds=aliyun.Credentials("AKID", "secret"), transport=transport2
        )
        self.assertNotEqual(first, seen2["headers"]["authorization"])

    def test_being_denied_is_not_an_empty_result(self):
        def denied(url, headers=None):
            return 403, {"Code": "NoPermission", "Message": "You are not authorized"}

        with self.assertRaises(aliyun.AliyunDenied):
            aliyun.call_roa(HZ, "v", "/p", creds=aliyun.Credentials("a", "b"), transport=denied)


if __name__ == "__main__":
    unittest.main()
