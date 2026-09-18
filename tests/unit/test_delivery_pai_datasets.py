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

    def __call__(self, url, headers=None, method="GET", body=b""):
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

        def denied(url, headers=None, method="GET", body=b""):
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

        def transport(url, headers=None, method="GET", body=b""):
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
        def denied(url, headers=None, method="GET", body=b""):
            return 403, {"Code": "NoPermission", "Message": "You are not authorized"}

        with self.assertRaises(aliyun.AliyunDenied):
            aliyun.call_roa(HZ, "v", "/p", creds=aliyun.Credentials("a", "b"), transport=denied)


if __name__ == "__main__":
    unittest.main()


class RecycleBinTests(unittest.TestCase):
    """回收站：**趁保留期内把 UserId → 姓名固化下来**。

    过了保留期，一个离职的人就只剩一个数字 UserId，而他留下的数据集、目录、机器还在。
    线上已经有一条是这样了（`/cwr`，删于 2026-08-05，回收站里查不到，没人说得清是谁的）。
    """

    def bin_body(self, users, truncated=False, marker=""):
        return {
            "IsTruncated": truncated,
            "Marker": marker,
            "Users": {"User": users},
        }

    def entry(self, uid, login, name, when="2026-09-05T00:12:22Z"):
        return {
            "UserId": uid,
            "UserPrincipalName": f"{login}@1704065796538912.onaliyun.com",
            "DisplayName": name,
            "RecycleDate": when,
        }

    def test_the_principal_is_trimmed_to_a_bare_login(self):
        """回收站给的是 `名@UID.onaliyun.com`，别处的登录名是裸的。
        不裁的话两边永远对不上，而对不上就等于没认出人。"""

        def transport(url, headers=None, method="GET", body=b""):
            return 200, self.bin_body([self.entry("999", "JunleiZhu", "朱俊磊")])

        got = assets.collect_recycle_bin(aliyun.Credentials("a", "b"), transport=transport)
        self.assertEqual(got[0]["login"], "JunleiZhu")
        self.assertEqual(got[0]["name"], "朱俊磊")
        self.assertEqual(got[0]["user_id"], "999")
        self.assertTrue(got[0]["deleted_at"].startswith("2026-09-05"))

    def test_it_pages(self):
        pages = [
            self.bin_body([self.entry("1", "a", "甲")], truncated=True, marker="m2"),
            self.bin_body([self.entry("2", "b", "乙")]),
        ]

        def transport(url, headers=None, method="GET", body=b""):
            return 200, pages.pop(0)

        got = assets.collect_recycle_bin(aliyun.Credentials("a", "b"), transport=transport)
        self.assertEqual([u["user_id"] for u in got], ["1", "2"])


class AbandonedTests(unittest.TestCase):
    """体检清单里「人的号已经删了，东西还留着」那一类。"""

    def owned(self, kind, **kw):
        base = {
            "region": "cn-hangzhou",
            "workspace": "640957",
            "name": "zhujl",
            "path": "/zhujl",
            "owner_kind": kind,
            "owner_user_id": "999",
            "owner_login": "",
            "owner_name": "",
            "owner_deleted_at": "",
        }
        base.update(kw)
        return base

    def test_only_gone_owners_land_here(self):
        """`root` 是主账号建的公共目录，`user` 是人还在 —— 混进来这一栏就全是噪音。
        线上实测 61 条里 root 有 13 条，真 gone 只有 5 条。"""
        from delivery import hygiene

        rep = hygiene.build(
            None,
            [],
            datasets=[
                self.owned("root", name="share"),
                self.owned("user", name="wzh", owner_login="wangzihan"),
                self.owned("gone", owner_login="JunleiZhu", owner_name="朱俊磊"),
            ],
        )
        self.assertEqual([f.subject for f in rep.abandoned], ["zhujl"])

    def test_a_recycled_owner_is_named_outright(self):
        from delivery import hygiene

        rep = hygiene.build(
            None,
            [],
            datasets=[
                self.owned(
                    "gone",
                    owner_login="JunleiZhu",
                    owner_name="朱俊磊",
                    owner_deleted_at="2026-09-05T00:12:22Z",
                )
            ],
        )
        (f,) = rep.abandoned
        self.assertIn("朱俊磊", f.why)
        self.assertIn("2026-09-05", f.why)

    def test_an_owner_past_the_retention_window_says_so_plainly(self):
        """认不出来要明说，否则看的人会以为只是漏填了名字、以为还能查。"""
        from delivery import hygiene

        rep = hygiene.build(None, [], datasets=[self.owned("gone")])
        (f,) = rep.abandoned
        self.assertIn("保留期已过", f.why)
        self.assertIn("999", f.why)

    def test_this_category_survives_a_missing_permission_snapshot(self):
        """数据集来自**资产**快照，和权限快照是两个文件。
        算在权限快照那道门后面的话，权限快照一缺这一类会跟着消失。"""
        from delivery import hygiene

        rep = hygiene.build(None, [], datasets=[self.owned("gone", owner_name="朱俊磊")])
        self.assertEqual(len(rep.abandoned), 1)
        self.assertTrue(any("权限快照" in s for s in rep.skipped))

    def test_no_datasets_at_all_is_reported_as_skipped(self):
        """传 None = 没采到。**不能当成「一条被遗弃的都没有」**。"""
        from delivery import hygiene

        rep = hygiene.build(None, [], datasets=None)
        self.assertEqual(rep.abandoned, [])
        self.assertTrue(any("数据集" in s for s in rep.skipped))

        clean = hygiene.build(None, [], datasets=[])
        self.assertFalse(any("数据集" in s for s in clean.skipped))


class MergeTests(unittest.TestCase):
    """回收站要**只增不减**地攒。这是这个功能存在的全部理由。

    云上那份有保留期，过期就查不到了。每次采集整份覆盖的话，保留期一过那几个名字会
    原样消失 —— 想解决的问题原封不动复发，只是从「晚了 24 天」变成「晚了一个采集周期」。
    """

    def e(self, uid, name, when="2026-09-05T00:00:00Z"):
        return {"user_id": uid, "login": f"u{uid}", "name": name, "deleted_at": when}

    def test_someone_who_aged_out_of_the_cloud_bin_is_still_remembered(self):
        """朱俊磊 9 月在回收站里被记下；10 月云上已经清了 —— 台账里必须还在。"""
        kept = assets.merge_recycle_bin(None, [self.e("999", "朱俊磊")])
        later = assets.merge_recycle_bin(kept, [])  # 云上已经没有他了
        self.assertEqual([u["name"] for u in later], ["朱俊磊"])

    def test_skipping_the_pai_pass_never_wipes_what_was_collected(self):
        """`--skip-pai` 是一次例行的「只刷资源中心」。它把攒下来的记录抹掉的话，
        这个功能会死得毫无征兆。"""
        kept = assets.merge_recycle_bin(None, [self.e("999", "朱俊磊")])
        self.assertEqual(assets.merge_recycle_bin(kept, None), kept)

    def test_the_earlier_record_wins(self):
        """早一次采到的信息离真实删除时间更近；后来那次可能已经是残缺的。"""
        kept = assets.merge_recycle_bin(None, [self.e("999", "朱俊磊", "2026-09-05T00:00:00Z")])
        merged = assets.merge_recycle_bin(kept, [self.e("999", "", "2026-09-30T00:00:00Z")])
        self.assertEqual(merged[0]["name"], "朱俊磊")
        self.assertTrue(merged[0]["deleted_at"].startswith("2026-09-05"))

    def test_nothing_ever_collected_stays_none(self):
        """从来没采过 ≠ 采过但是空的。"""
        self.assertIsNone(assets.merge_recycle_bin(None, None))
        self.assertEqual(assets.merge_recycle_bin(None, []), [])

    def test_entries_without_an_id_are_dropped(self):
        """空 id 会变成一个「匹配任何缺 id 的数据集」的键 ——
        那会把一个真实离职者的姓名安到一条不知属主的数据集上。"""
        merged = assets.merge_recycle_bin(None, [self.e("", "谁"), self.e("1", "甲")])
        self.assertEqual([u["user_id"] for u in merged], ["1"])


class PagingGuardTests(unittest.TestCase):
    """回收站翻页的两道 fail-closed 守卫。丢掉的话，少掉的那个人名下的数据集会被体检
    **正面断言**「保留期已过，现在没人认得出这是谁的」—— 那是一句错话，不是缺数据。"""

    def test_a_truncated_page_without_a_marker_raises(self):
        def transport(url, headers=None, method="GET", body=b""):
            return 200, {"IsTruncated": True, "Marker": "", "Users": {"User": [{"UserId": "1"}]}}

        with self.assertRaises(aliyun.AliyunError):
            assets.collect_recycle_bin(aliyun.Credentials("a", "b"), transport=transport)

    def test_a_renamed_inner_key_raises_instead_of_reporting_empty(self):
        """接口结构变了要响。静默空表会让体检说出上面那句错话。"""

        def transport(url, headers=None, method="GET", body=b""):
            return 200, {"IsTruncated": False, "Users": {"UserList": [{"UserId": "1"}]}}

        with self.assertRaises(aliyun.AliyunError):
            assets.collect_recycle_bin(aliyun.Credentials("a", "b"), transport=transport)

    def test_a_missing_container_raises_too(self):
        def transport(url, headers=None, method="GET", body=b""):
            return 200, {"IsTruncated": False}

        with self.assertRaises(aliyun.AliyunError):
            assets.collect_recycle_bin(aliyun.Credentials("a", "b"), transport=transport)

    def test_a_genuinely_empty_bin_is_not_an_error(self):
        """回收站是空的是正常状态 —— 对它报错等于教人忽略这类报错。"""
        for body in (
            {"IsTruncated": False, "Users": {}},
            {"IsTruncated": False, "Users": {"User": []}},
        ):

            def transport(url, headers=None, method="GET", body=b"", _b=body):
                return 200, _b

            got = assets.collect_recycle_bin(aliyun.Credentials("a", "b"), transport=transport)
            self.assertEqual(got, [], body)


class SnapshotGuardTests(unittest.TestCase):
    def test_a_corrupt_datasets_key_raises_instead_of_crashing_downstream(self):
        import json as _json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "assets.json"
            for bad in ({"a": 1}, "nope", 3):
                path.write_text(_json.dumps({"accounts": [], "datasets": bad}), encoding="utf-8")
                with self.assertRaises(assets.AssetError, msg=repr(bad)):
                    assets.load(str(path))
            path.write_text(_json.dumps({"accounts": [], "datasets": []}), encoding="utf-8")
            self.assertEqual(assets.load(str(path))["datasets"], [])


class DatasetAssetTests(unittest.TestCase):
    """数据集当资产看。**唯一一类归属自带的资产** —— 所以员工那栏不用等管理员指派。"""

    def ds(self, **kw):
        base = {
            "name": "wangzihan",
            "region": "cn-hangzhou",
            "workspace": "590221",
            "source": "BMCPFS",
            "path": "/wangzihan",
            "owner_login": "wangzihan",
            "owner_name": "王梓涵",
            "owner_kind": "user",
        }
        base.update(kw)
        return base

    def test_an_employee_sees_only_their_own(self):
        view = assets.datasets_view(
            [self.ds(), self.ds(name="chuzhong", owner_login="chuzhong", owner_name="储众")],
            logins=["wangzihan"],
        )
        self.assertEqual([d["name"] for d in view["items"]], ["wangzihan"])
        self.assertNotIn("储众", json.dumps(view, ensure_ascii=False))

    def test_login_matching_ignores_case(self):
        """RAM 登录名在别处会被转小写；两边写法不一致就会看不到自己的东西。"""
        view = assets.datasets_view([self.ds(owner_login="ZihanWang")], logins=["zihanwang"])
        self.assertEqual(len(view["items"]), 1)

    def test_the_admin_sees_everyone_and_the_abandoned_count(self):
        view = assets.datasets_view(
            [self.ds(), self.ds(name="zhujl", owner_login="", owner_kind="gone")], logins=None
        )
        self.assertEqual(len(view["items"]), 2)
        self.assertEqual(view["abandoned"], 1)

    def test_an_employee_never_sees_the_abandoned_count(self):
        """那是别人的事，摆在员工页上只会让人以为自己有问题。"""
        view = assets.datasets_view(
            [self.ds(name="zhujl", owner_login="", owner_kind="gone")], logins=["wangzihan"]
        )
        self.assertEqual(view["abandoned"], 0)

    def test_not_collected_is_not_the_same_as_none_owned(self):
        """没跑过采集 / PAI 权限掉了 vs 确实没有。前者要去修采集，后者是正常状态。"""
        missing = assets.datasets_view(None, logins=["wangzihan"])
        self.assertFalse(missing["collected"])
        empty = assets.datasets_view([], logins=["wangzihan"])
        self.assertTrue(empty["collected"])
        self.assertEqual(empty["items"], [])


class CreateBodyTests(unittest.TestCase):
    """`create_dataset` 组装出来的请求体。

    **这一组的存在理由**：线上返工过一次 —— `user_id`/`import_info`/`labels` 三个参数
    加在了函数签名上，一次性脚本里传了、CLI 里忘了传，于是 49 条数据集属主全是面板自己，
    而 `UpdateDataset` 改 UserId 是静默无效的、`DeleteDataset` 面板又不实现，
    只能去控制台手删重建。没有用例钉住请求体，这类错就只能等它上生产。
    """

    def sent(self, **kw):
        seen = {}

        def transport(url, headers=None, method="GET", body=b""):
            seen.update(url=url, method=method, body=json.loads(body or b"{}"))
            return 200, {"DatasetId": "d-x"}

        args = dict(
            region="cn-hangzhou",
            workspace="640957",
            name="wzh-oss",
            uri="oss://b.oss-cn-hangzhou.aliyuncs.com/general/wzh/",
            source="OSS",
            user_id="206",
        )
        args.update(kw)
        assets.create_dataset(aliyun.Credentials("a", "b"), transport=transport, **args)
        return seen["body"]

    def test_an_empty_owner_is_refused_outright(self):
        """**不建比建错好。** 建出来属主是面板自己，而且改不回来。"""
        with self.assertRaises(assets.AssetError) as caught:
            self.sent(user_id="")
        self.assertIn("属主", str(caught.exception))

    def test_the_owner_actually_reaches_the_request_body(self):
        self.assertEqual(self.sent()["UserId"], "206")

    def test_role_public_always_carries_the_role_list(self):
        """不给 `AccessibleRoleIdList` 的话 PAI 直接 400 —— 实测踩过。"""
        body = self.sent()
        self.assertEqual(body["Accessibility"], "ROLE_PUBLIC")
        self.assertIn("owner", body["AccessibleRoleIdList"])
        self.assertIn("PAI.AlgoDeveloper", body["AccessibleRoleIdList"])

    def test_import_info_and_options_are_json_strings_not_objects(self):
        """PAI 要的是 JsonString，传对象会被拒。"""
        body = self.sent(
            import_info={"bucket": "b", "path": "general/wzh/", "region": "cn-hangzhou"}
        )
        self.assertIsInstance(body["ImportInfo"], str)
        self.assertEqual(json.loads(body["ImportInfo"])["bucket"], "b")
        self.assertIsInstance(body["Options"], str)
        self.assertEqual(json.loads(body["Options"])["mountPath"], assets.DATASET_MOUNT)

    def test_labels_pass_through_verbatim(self):
        """键名是大写 `Key`/`Value` —— 小写会被拒成 `Label Key should not be blank`，
        而现网那 30 条 Labels 全是空数组，没有样本可抄。"""
        body = self.sent(labels=[{"Key": "kind", "Value": "personal"}])
        self.assertEqual(body["Labels"], [{"Key": "kind", "Value": "personal"}])

    def test_it_is_a_post_with_a_body(self):
        seen = {}

        def transport(url, headers=None, method="GET", body=b""):
            seen.update(method=method, headers=headers or {})
            return 200, {"DatasetId": "d-x"}

        assets.create_dataset(
            aliyun.Credentials("a", "b"),
            transport=transport,
            region="cn-hangzhou",
            workspace="640957",
            name="x",
            uri="oss://b/x/",
            source="OSS",
            user_id="206",
        )
        self.assertEqual(seen["method"], "POST")
        # 有正文时 Content-MD5 必须既进签名串也进请求头，少一个就是 SignatureDoesNotMatch
        self.assertIn("content-md5", seen["headers"])
        self.assertTrue(seen["headers"]["authorization"].startswith("acs a:"))


class OssSigningTests(unittest.TestCase):
    """OSS 的签名。这套是手写的、没有 SDK 兜底，改回去测试还是绿的话就没人发现了。"""

    def sign_of(self, query):
        from delivery.clouds import oss

        seen = {}

        def transport(url, method, headers, body):
            seen["auth"] = headers["Authorization"]
            return 200, b"<ListBucketResult></ListBucketResult>"

        oss.call(
            "GET",
            "bk",
            region="oss-cn-hangzhou",
            query=query,
            creds=aliyun.Credentials("AK", "SK"),
            transport=transport,
        )
        return seen["auth"]

    def test_listing_params_do_not_change_the_signature(self):
        """`prefix`/`delimiter`/`max-keys`/`list-type` **不是子资源**，不进签名串。
        带上它们必然 403 —— 实测踩过。"""
        base = self.sign_of({})
        self.assertEqual(base, self.sign_of({"prefix": "a/", "delimiter": "/", "max-keys": "5"}))
        self.assertEqual(base, self.sign_of({"list-type": "2"}))

    def test_the_continuation_token_does_change_it(self):
        """**翻页 token 是子资源**，必须进签名。漏了的话第一页正常、第二页 403，
        而错误码是 SignatureDoesNotMatch，很容易被读成「缺权限」。"""
        self.assertNotEqual(self.sign_of({}), self.sign_of({"continuation-token": "abc"}))

    def test_an_x_oss_header_changes_it(self):
        """服务端复制靠的就是 `x-oss-copy-source` 这个头。"""
        from delivery.clouds import oss

        seen = {}

        def transport(url, method, headers, body):
            seen["auth"] = headers["Authorization"]
            return 200, b""

        oss.call(
            "PUT",
            "bk",
            "dst",
            region="oss-cn-hangzhou",
            creds=aliyun.Credentials("AK", "SK"),
            transport=transport,
        )
        plain = seen["auth"]
        oss.call(
            "PUT",
            "bk",
            "dst",
            region="oss-cn-hangzhou",
            extra={"x-oss-copy-source": "/bk/src"},
            creds=aliyun.Credentials("AK", "SK"),
            transport=transport,
        )
        self.assertNotEqual(plain, seen["auth"])

    def test_a_security_token_is_signed_not_just_attached(self):
        """临时凭证的 token 也是 `x-oss-` 头。放到算签名之后（原来就是这样）的话，
        任何 STS 凭证都会签名失败。"""
        from delivery.clouds import oss

        seen = {}

        def transport(url, method, headers, body):
            seen.update(auth=headers["Authorization"], headers=headers)
            return 200, b""

        oss.call(
            "GET",
            "bk",
            region="oss-cn-hangzhou",
            creds=aliyun.Credentials("AK", "SK"),
            transport=transport,
        )
        plain = seen["auth"]
        oss.call(
            "GET",
            "bk",
            region="oss-cn-hangzhou",
            creds=aliyun.Credentials("AK", "SK", security_token="tok"),
            transport=transport,
        )
        self.assertNotEqual(plain, seen["auth"])
        self.assertEqual(seen["headers"]["x-oss-security-token"], "tok")

    def test_an_error_never_echoes_the_access_key_or_the_string_to_sign(self):
        """OSS 的 SignatureDoesNotMatch 正文里 **OSSAccessKeyId 排在 StringToSign 前面**，
        所以「按 StringToSign 截断」挡不住完整 AK。只回显 Code/Message/RequestId。"""
        from delivery.clouds import oss

        body = (
            b"<?xml version='1.0'?><Error><Code>SignatureDoesNotMatch</Code>"
            b"<Message>signature mismatch</Message><RequestId>REQ1</RequestId>"
            b"<OSSAccessKeyId>LTAI5tLEAKEDKEYID</OSSAccessKeyId>"
            b"<SignatureProvided>SECRETSIG</SignatureProvided>"
            b"<StringToSign>GET\\n\\n\\n</StringToSign></Error>"
        )

        def transport(url, method, headers, body_=b""):
            return 403, body

        with self.assertRaises(oss.OssDenied) as caught:
            oss.call(
                "GET",
                "bk",
                region="oss-cn-hangzhou",
                creds=aliyun.Credentials("AK", "SK"),
                transport=transport,
            )
        text = str(caught.exception)
        self.assertNotIn("LTAI5tLEAKEDKEYID", text)
        self.assertNotIn("SECRETSIG", text)
        self.assertNotIn("StringToSign", text)
        self.assertIn("REQ1", text)

    def test_a_response_with_a_dtd_is_refused(self):
        """长度上限挡不住实体膨胀（它量的是膨胀前的原文）。真正管用的是拒 DTD。"""
        from delivery.clouds import oss

        def transport(url, method, headers, body=b""):
            return 200, b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]><r/>'

        with self.assertRaises(oss.OssError):
            oss.list_prefixes(
                "bk",
                region="oss-cn-hangzhou",
                creds=aliyun.Credentials("AK", "SK"),
                transport=transport,
            )
