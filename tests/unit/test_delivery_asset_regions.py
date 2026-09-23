"""资产快照 → 「哪儿有东西、哪些没登记」。`assets` 那几个纯函数 + CLI 侧的登记表读数。

两条来自线上真实数据的不变量，是这一组用例存在的理由：

① **按工作空间 ID 对账，绝不按地域。** 杭州真的有两个工作空间（`wuji_general_gpu`
   登记过、`ai_hz_gpu` 没有）。按地域比的话，杭州「登记过」→ 第二个永远报不出来，
   而它里面有人在跑任务、有数据集，却不在任何申请流程里。
② **地域必须带平台前缀。** 两朵云都有 `cn-shanghai`/`cn-beijing`，扁平的地域名集合
   会让火山那条把阿里那条遮掉 —— 遮掉的表现是「一切正常」，没人会发现。

③ **「有没有卡」查不到时只能说不知道。** 算力配额接口按调用者的空间成员身份裁剪返回，
   而要判的恰恰是采集身份多半不在里面的那些空间 —— 把「看不到」当成「没有」，
   就会把线上那个有 144 张卡的 `ai_hz_gpu`（id 590221）当成空壳藏起来。

另外锁住：类型名**全名匹配**，两个方向都踩过 —— 按子串 `"pai"` 匹配会把火山的
`keypair` 算进来；而火山**任何产品**的工作区在 `ResourceType` 里都叫 `Workspace`，
只认这个裸名会把托管 Prometheus（VMP）的工作区当成机器学习算力空间报出去。
以及空快照返回空清单而不是抛。全部离线，数据虚构。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from delivery import assets, catalog, cli_requests, todo, workspaces
from delivery.clouds import aliyun, volcano

#: 线上那条：杭州两个工作空间，登记表里只有第一个
HZ_REGISTERED = "640957"  # wuji_general_gpu
HZ_UNREGISTERED = "712233"  # ai_hz_gpu —— 同一个地域，没登记；线上它有 144 张卡


def res(type_, id_, region, name="", **extra):
    """资源中心归一化之后的一条（见 assets.collect_aliyun / collect_volcano）。

    火山那侧多一个 `service`（`mlplatform` / `vmp` …）—— 光看类型名分不出产品的时候，
    这一列是唯一能说清「这是哪个产品的工作区」的东西。
    """
    out = {"type": type_, "id": id_, "region": region, "name": name, "created": "", "tags": {}}
    out.update(extra)
    return out


def snapshot(*, aliyun_res=(), volcano_res=(), buckets=None, extra_accounts=()):
    out = {
        "captured_at": "2026-09-20T10:00:00+08:00",
        "accounts": [
            {"platform": "aliyun", "account": "1000000000000001", "resources": list(aliyun_res)},
            {"platform": "volcano", "account": "2000000001", "resources": list(volcano_res)},
            *extra_accounts,
        ],
    }
    if buckets is not None:
        out["buckets"] = list(buckets)
    return out


#: 线上形状：杭州两个工作空间 + 一个数据集，新加坡一个工作空间，
#: 北京只有别的资源（VPC / ECS），上海两朵云都有东西
PROD_LIKE = snapshot(
    aliyun_res=[
        res("ACS::PAIWorkspace::Workspace", HZ_REGISTERED, "cn-hangzhou", "wuji_general_gpu"),
        res("ACS::PAIWorkspace::Workspace", HZ_UNREGISTERED, "cn-hangzhou", "ai_hz_gpu"),
        res("ACS::PAIWorkspace::Dataset", "d-1", "cn-hangzhou", "训练集"),
        res("ACS::PAIWorkspace::Workspace", "284761", "ap-southeast-1", "wuji_sing"),
        res("ACS::ECS::Instance", "i-1", "cn-beijing"),
        res("ACS::VPC::VPC", "vpc-1", "cn-beijing"),
        res("ACS::RAM::User", "u-1", "global"),  # 全局资源不属于任何地域
        res("ACS::OSS::Bucket", "b-0", ""),  # 地域字段空的也不算
        res("ACS::ECS::Instance", "i-2", "cn-shanghai"),
    ],
    volcano_res=[
        res(
            "Volcengine::MLPlatform::Workspace",
            "vw-1",
            "cn-shanghai",
            "火山机器学习",
            service="mlplatform",
        ),
        # 线上那三个「火山工作空间」其实都是这个：托管 Prometheus 的监控工作区，
        # 和算力毫无关系。`ResourceType` 里它也叫 `Workspace`
        res("Volcengine::VMP::Workspace", "vmp-1", "cn-shanghai", "h20-VLA", service="vmp"),
        # 名字里有 pai，但它不是 PAI（子串匹配那个真 bug 的方向）
        res("Volcengine::ECS::KeyPair", "kp-1", "cn-shanghai", "登录密钥", service="ecs"),
        res("Volcengine::ECS::Instance", "vi-1", "cn-beijing", service="ecs"),
    ],
    buckets=[
        {"name": "wuji-algo-dev-hz", "region": "oss-cn-hangzhou", "created": ""},
        {"name": "wuji-bangkok", "region": "oss-ap-southeast-7", "created": ""},
    ],
)


class RegionsInUseTests(unittest.TestCase):
    def test_counts_by_platform_and_type(self):
        got = assets.regions_in_use(PROD_LIKE)
        self.assertEqual(
            got["aliyun/cn-hangzhou"],
            {
                "ACS::PAIWorkspace::Workspace": 2,
                "ACS::PAIWorkspace::Dataset": 1,
                "OSS 桶": 1,  # 顶层 buckets 也折进来
            },
        )
        self.assertEqual(got["aliyun/cn-beijing"], {"ACS::ECS::Instance": 1, "ACS::VPC::VPC": 1})

    def test_two_clouds_same_region_name_stay_apart(self):
        """**不变量②**：两朵云都有 `cn-shanghai`。合成一条的话，
        「上海有什么」这个问题的答案会把另一朵云的东西算进来，
        而下面那条「没登记的地域」还会因此少报一整朵云。"""
        got = assets.regions_in_use(PROD_LIKE)
        self.assertEqual(got["aliyun/cn-shanghai"], {"ACS::ECS::Instance": 1})
        self.assertEqual(
            got["volcano/cn-shanghai"],
            {
                "Volcengine::MLPlatform::Workspace": 1,
                "Volcengine::VMP::Workspace": 1,
                "Volcengine::ECS::KeyPair": 1,
            },
        )

    def test_pseudo_regions_and_blanks_skipped(self):
        """`global` / `cn-global` / `all` 不是地域（RAM 用户、账号级配置这类）。
        混进来的话「哪个地域有资源」的答案里会永远挂着一条谁也处理不了的 global。"""
        for pseudo in ("global", "cn-global", "all", "GLOBAL", "Cn-Global", " "):
            with self.subTest(region=pseudo):
                got = assets.regions_in_use(
                    snapshot(aliyun_res=[res("ACS::RAM::User", "u", pseudo)])
                )
                self.assertEqual(got, {})

    def test_bucket_region_prefix_stripped(self):
        """OSS 的 `Location` 是 `oss-cn-hangzhou`，登记表和资源中心都写裸地域。
        不归一的话同一个杭州会裂成两行，其中一行永远显示「没登记」。"""
        got = assets.regions_in_use(snapshot(buckets=[{"name": "b", "region": "oss-cn-hangzhou"}]))
        self.assertEqual(got, {"aliyun/cn-hangzhou": {"OSS 桶": 1}})

    def test_account_that_failed_to_collect_does_not_crash(self):
        """采集失败的账号只有 `error`、没有 `resources`。
        这时要照常算出其余账号的地域，而不是整页崩掉。"""
        got = assets.regions_in_use(
            snapshot(
                aliyun_res=[res("ACS::ECS::Instance", "i", "cn-hangzhou")],
                extra_accounts=[{"platform": "volcano", "account": "v", "error": "被拒"}],
            )
        )
        self.assertEqual(got, {"aliyun/cn-hangzhou": {"ACS::ECS::Instance": 1}})

    def test_empty_inputs_are_empty_not_raise(self):
        for empty in (None, {}, {"accounts": []}, {"accounts": None}, {"buckets": None}):
            with self.subTest(empty=empty):
                self.assertEqual(assets.regions_in_use(empty), {})


class WorkspacesInUseTests(unittest.TestCase):
    def test_only_whole_type_names_count(self):
        """**全名匹配，两个方向**：`keypair` 里有 `pai` 但它不是 PAI；
        VMP 的监控工作区在 `ResourceType` 里也叫 `Workspace` 但它不是算力空间。
        数据集也不算工作空间 —— 它只说明「这个地域有 PAI 的东西」。"""
        got = assets.workspaces_in_use(PROD_LIKE)
        self.assertEqual(
            [(w["platform"], w["id"]) for w in got],
            [
                ("aliyun", "284761"),  # ap-southeast-1
                ("aliyun", HZ_UNREGISTERED),  # 排序按 平台/地域/名字：ai_hz_gpu 在前
                ("aliyun", HZ_REGISTERED),
                ("volcano", "vw-1"),
            ],
        )
        blob = repr(got)
        self.assertNotIn("kp-1", blob)
        self.assertNotIn("d-1", blob)

    def test_monitoring_workspace_is_not_a_workspace(self):
        """**线上报错的就是这个**：给管理员报出去的「3 个火山工作空间」
        （`h20-VLA` / `data-infra` / `WujiGrasp`）全是托管 Prometheus 的监控工作区，
        和机器学习算力毫无关系 —— 照着去「登记进 workspaces.json」是纯噪音。"""
        got = assets.workspaces_in_use(
            snapshot(
                volcano_res=[
                    res("Volcengine::VMP::Workspace", "vmp-a", "cn-beijing", "data-infra"),
                    res("Volcengine::VMP::Workspace", "vmp-b", "cn-beijing", "WujiGrasp"),
                    res("Volcengine::MLPlatform::Workspace", "ml-a", "cn-beijing", "真算力"),
                ]
            )
        )
        self.assertEqual([w["id"] for w in got], ["ml-a"])
        self.assertNotIn("Volcengine::VMP::Workspace", assets.WORKSPACE_TYPES)

    def test_bare_workspace_type_in_an_old_snapshot_is_not_counted(self):
        """改之前采的快照里，火山所有产品的工作区都写着裸 `Workspace` ——
        **分不出是哪个产品，就不报**。报出来的那一版正是噪音的来源；
        重新采一次（采集器现在存 `TypeName`）才有资格重新出现在清单上。"""
        got = assets.workspaces_in_use(snapshot(volcano_res=[res("Workspace", "vw", "cn-beijing")]))
        self.assertEqual(got, [])

    def test_empty_snapshot_is_empty_list_again(self):
        self.assertEqual(assets.workspaces_in_use({"accounts": [{"platform": "volcano"}]}), [])

    def test_empty_snapshot_is_empty_list(self):
        for empty in (None, {}, {"accounts": []}):
            with self.subTest(empty=empty):
                self.assertEqual(assets.workspaces_in_use(empty), [])


class VolcanoCollectTypeTests(unittest.TestCase):
    """采集这一步就要把产品认出来：存 `TypeName` + `service`，不存裸 `ResourceType`。"""

    CREDS = volcano.Credentials("AKLTtest", "secret")

    def collect(self, resources):
        seen = []

        def send(url, headers, data=None):
            seen.append(url)
            if "ListUsers" in url:
                return 200, {"Result": {"UserMetadata": [{"AccountId": 2000000001}]}}
            return 200, {"Result": {"Resources": list(resources)}}

        account, items = assets.collect_volcano(self.CREDS, transport=send)
        return account, items, seen

    def test_type_name_wins_over_resource_type(self):
        """两个字段同时有：`ResourceType` 是裸的产品内类型名（每个产品的工作区都叫
        `Workspace`），`TypeName` 才带产品。只认前者 = 把监控工作区当算力空间。"""
        _, items, _ = self.collect(
            [
                {
                    "ResourceType": "Workspace",
                    "TypeName": "Volcengine::VMP::Workspace",
                    "Service": "vmp",
                    "ResourceID": "vmp-1",
                    "ResourceName": "h20-VLA",
                    "Region": "cn-beijing",
                },
                {
                    "ResourceType": "Workspace",
                    "TypeName": "Volcengine::MLPlatform::Workspace",
                    "Service": "mlplatform",
                    "ResourceID": "ml-1",
                    "ResourceName": "真算力",
                    "Region": "cn-beijing",
                },
            ]
        )
        self.assertEqual(
            [(r["type"], r["service"]) for r in items],
            [
                ("Volcengine::VMP::Workspace", "vmp"),
                ("Volcengine::MLPlatform::Workspace", "mlplatform"),
            ],
        )
        # 采完直接喂给下游：只有机器学习那个算工作空间
        snap = {"accounts": [{"platform": "volcano", "account": "2000000001", "resources": items}]}
        self.assertEqual([w["id"] for w in assets.workspaces_in_use(snap)], ["ml-1"])

    def test_falls_back_to_resource_type_when_type_name_missing(self):
        """没有 `TypeName` 的资源（老接口 / 别的产品）不能变成空类型 ——
        空类型在地域统计里会汇成一条「未知类型」，看的人只会以为是脏数据。"""
        _, items, _ = self.collect(
            [{"ResourceType": "Instance", "ResourceID": "i-1", "Region": "cn-beijing"}]
        )
        self.assertEqual(items[0]["type"], "Instance")
        self.assertEqual(items[0]["service"], "")  # 没有就是空串，不是缺字段

    def test_service_survives_write_and_read(self):
        """`service` 要一路活到快照文件里：产品维度只剩它说得清，
        丢了就又只能靠类型名猜，而那正是这次出事的原因。"""
        import json
        import tempfile
        from pathlib import Path

        _, items, _ = self.collect(
            [
                {
                    "TypeName": "Volcengine::VMP::Workspace",
                    "Service": "vmp",
                    "ResourceID": "vmp-1",
                    "Region": "cn-beijing",
                }
            ]
        )
        data = assets.build_snapshot([("volcano", "v", lambda: ("2000000001", items))])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "assets.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            got = assets.load(str(path))
        self.assertEqual(got["accounts"][0]["resources"][0]["service"], "vmp")


class UnregisteredWorkspaceTests(unittest.TestCase):
    """**不变量①**：按 ID 对账，一个地域里的第二个工作空间不许被第一个盖掉。"""

    def test_second_workspace_in_the_same_region_is_reported(self):
        got = assets.unregistered_workspaces(PROD_LIKE, {HZ_REGISTERED, "284761"})
        self.assertEqual([w["id"] for w in got], [HZ_UNREGISTERED, "vw-1"])
        self.assertEqual(got[0]["name"], "ai_hz_gpu")
        self.assertEqual(got[0]["region"], "cn-hangzhou")

    def test_registering_the_region_does_not_hide_it(self):
        """反过来写一遍，把「按地域比会怎样」钉在用例里：
        杭州这个地域登记过（`aliyun/cn-hangzhou` 在 `workspace_regions` 里），
        但 `ai_hz_gpu` 这个 ID 没登记 —— 它必须照样被报出来。"""
        reg = workspaces.parse(
            {
                "workspaces": {
                    "hz": {
                        "label": "杭州",
                        "id": HZ_REGISTERED,
                        "region": "cn-hangzhou",
                        "bucket": "wuji-algo-dev-hz",
                        "bucket_region": "cn-hangzhou",
                    }
                }
            }
        )
        self.assertIn("aliyun/cn-hangzhou", cli_requests.workspace_regions(reg))  # 地域是登记过的
        got = assets.unregistered_workspaces(PROD_LIKE, cli_requests.workspace_ids(reg))
        self.assertIn(HZ_UNREGISTERED, [w["id"] for w in got])

    def test_blank_known_ids_are_ignored_not_matched(self):
        """登记表里某条没有 ID（空串 / None / 全是空格）时，绝不能因此把
        某个云上工作空间当成已登记 —— 那正是最该被报出来的那种残缺登记。"""
        got = assets.unregistered_workspaces(PROD_LIKE, ["", None, "   ", HZ_REGISTERED])
        self.assertEqual([w["id"] for w in got], ["284761", HZ_UNREGISTERED, "vw-1"])

    def test_empty_snapshot_returns_empty_not_raise(self):
        for empty in (None, {}, {"accounts": []}):
            with self.subTest(empty=empty):
                self.assertEqual(assets.unregistered_workspaces(empty, {HZ_REGISTERED}), [])
        # 反过来：有快照、登记表是空的 → 云上每一个都要报
        self.assertEqual(len(assets.unregistered_workspaces(PROD_LIKE, [])), 4)


class PaiRegionTests(unittest.TestCase):
    def test_only_pai_types_and_whole_names(self):
        got = assets.pai_regions(PROD_LIKE)
        self.assertEqual(
            got,
            {
                "aliyun/cn-hangzhou": {
                    "ACS::PAIWorkspace::Workspace": 2,
                    "ACS::PAIWorkspace::Dataset": 1,
                },
                "aliyun/ap-southeast-1": {"ACS::PAIWorkspace::Workspace": 1},
                # VMP 的监控工作区不是算力空间，不进这张表
                "volcano/cn-shanghai": {"Volcengine::MLPlatform::Workspace": 1},
            },
        )

    def test_keypair_is_not_pai(self):
        """`"pai" in "keypair"` 是真 —— 按子串匹配的那一版把火山每个地域的密钥对
        都算成了「这儿有 PAI 工作空间」，于是「该开工作区吗」这个问题恒为是。"""
        for kind in ("keypair", "Volcengine::ECS::KeyPair"):
            with self.subTest(kind=kind):
                got = assets.pai_regions(snapshot(volcano_res=[res(kind, "kp", "cn-beijing")]))
                self.assertEqual(got, {})
                self.assertNotIn(kind, assets.PAI_TYPES)
                self.assertNotIn(kind, assets.WORKSPACE_TYPES)

    def test_monitoring_workspace_is_not_a_pai_region(self):
        """只有 VMP 监控工作区的地域，不构成「该开算力工作区」的理由。"""
        got = assets.pai_regions(
            snapshot(volcano_res=[res("Volcengine::VMP::Workspace", "v", "cn-beijing")])
        )
        self.assertEqual(got, {})
        self.assertNotIn("Volcengine::VMP::Workspace", assets.PAI_TYPES)

    def test_regions_with_only_other_resources_are_not_pai_regions(self):
        """北京只有 ECS/VPC：那是「这些东西是谁的」的问题，不是「该不该开工作区」。"""
        self.assertNotIn("aliyun/cn-beijing", assets.pai_regions(PROD_LIKE))
        self.assertIn("aliyun/cn-beijing", assets.regions_in_use(PROD_LIKE))


def quota(name, gpu, *workspace_ids):
    return {
        "QuotaName": name,
        "QuotaDetails": {"ActualMinQuota": {"GPU": gpu, "CPU": 96}},
        "Workspaces": [{"WorkspaceId": w} for w in workspace_ids],
    }


class QuotasByWorkspaceTests(unittest.TestCase):
    """算力配额：谁有卡。接口是 PaiStudio 的 `GET /api/v1/quotas/`（ROA 签名）。"""

    CREDS = aliyun.Credentials("LTAItest", "secret")

    def transport(self, per_region):
        """`per_region`：`{地域: [配额, …]}`，值是 Exception 就当这个地域打不通。"""
        calls = []

        def send(url, headers, method="GET", raw=b""):
            region = url.split("//", 1)[1].split(".", 2)[1]
            calls.append((region, url, headers))
            got = per_region.get(region, [])
            if isinstance(got, Exception):
                raise got
            return 200, {"Quotas": list(got)}

        return send, calls

    def test_sums_gpu_per_workspace_across_quotas(self):
        send, _ = self.transport(
            {
                "cn-hangzhou": [
                    quota("quota-a", 8, HZ_UNREGISTERED),
                    quota("quota-b", 136, HZ_UNREGISTERED),
                    quota("shared", 16, HZ_REGISTERED, HZ_UNREGISTERED),  # 一条配额挂两个空间
                ]
            }
        )
        found, skipped = assets.quotas_by_workspace(self.CREDS, ["cn-hangzhou"], transport=send)
        self.assertEqual(skipped, [])
        self.assertEqual(found[HZ_UNREGISTERED]["gpu"], 160)
        self.assertEqual(found[HZ_REGISTERED]["gpu"], 16)
        self.assertEqual(found[HZ_REGISTERED]["quotas"], ["shared×16卡"])

    def test_calls_paistudio_endpoint_per_region(self):
        send, calls = self.transport({"cn-hangzhou": [], "ap-southeast-1": []})
        assets.quotas_by_workspace(self.CREDS, ["cn-hangzhou", "ap-southeast-1"], transport=send)
        self.assertEqual([c[0] for c in calls], ["cn-hangzhou", "ap-southeast-1"])
        for region, url, _headers in calls:
            self.assertIn(f"https://pai.{region}.aliyuncs.com/api/v1/quotas/", url)
        # 版本号写在 `aliyun.PAISTUDIO` 里，走 x-acs-version 头；换了版本这里要一起改
        self.assertEqual(calls[0][2]["x-acs-version"], aliyun.PAISTUDIO)

    def test_no_regions_means_no_calls(self):
        """一个阿里云工作空间都没有时不该去打接口 —— 白打一次还可能白报一个错。"""
        send, calls = self.transport({})
        self.assertEqual(assets.quotas_by_workspace(self.CREDS, [], transport=send), ({}, []))
        self.assertEqual(calls, [])

    def test_a_region_that_fails_is_reported_not_swallowed(self):
        """河源没有 PAI 接入点、张家口实测 503。**那一整个地域的结论是「不知道」**，
        而且必须报出来 —— 悄悄跳过的话，清单看起来完整，实际少了一整个地域。"""
        send, _ = self.transport(
            {
                "cn-hangzhou": [quota("q", 144, HZ_UNREGISTERED)],
                "cn-heyuan": aliyun.AliyunError("连不上阿里云：Name or service not known"),
            }
        )
        found, skipped = assets.quotas_by_workspace(
            self.CREDS, ["cn-hangzhou", "cn-heyuan"], transport=send
        )
        self.assertEqual(found[HZ_UNREGISTERED]["gpu"], 144)  # 其余地域照常出结果
        self.assertEqual(len(skipped), 1)
        self.assertIn("cn-heyuan", skipped[0])  # 报出来的是哪个地域没查成

    def test_garbage_gpu_values_do_not_crash(self):
        """配额里 GPU 这一项可能是 `null` / 字符串 / 根本没有。
        **不能炸**（炸一次整批工作空间都没结论），按 0 算。"""
        send, _ = self.transport(
            {
                "cn-hangzhou": [
                    {"QuotaName": "x", "QuotaDetails": {}, "Workspaces": [{"WorkspaceId": "w"}]},
                    {
                        "QuotaName": "y",
                        "QuotaDetails": {"ActualMinQuota": {"GPU": "很多"}},
                        "Workspaces": [{"WorkspaceId": "w"}],
                    },
                    {
                        "QuotaName": "z",
                        "QuotaDetails": {"ActualMinQuota": {"GPU": None}},
                        "Workspaces": [{"WorkspaceId": ""}, {}],  # 没有 ID 的条目跳过
                    },
                ]
            }
        )
        found, skipped = assets.quotas_by_workspace(self.CREDS, ["cn-hangzhou"], transport=send)
        self.assertEqual(skipped, [])
        self.assertEqual(found["w"]["gpu"], 0)
        self.assertEqual(set(found), {"w"})

    def test_empty_response_is_no_entries_not_an_error(self):
        send, _ = self.transport({"cn-hangzhou": []})
        self.assertEqual(
            assets.quotas_by_workspace(self.CREDS, ["cn-hangzhou"], transport=send), ({}, [])
        )


class CardsStateTests(unittest.TestCase):
    """**不变量③**：`看不到` ≠ `没有`。

    配额接口按调用者的空间成员身份裁剪返回（真机实测三把凭证看到三份不同的清单），
    而要判的恰恰是采集身份多半不在里面的那些空间。把「查不到」判成「没卡」，
    线上那个有 144 张卡的 `ai_hz_gpu` 就会被当成空壳过滤掉 —— 没人会再看见它。
    """

    QUOTAS = {
        HZ_UNREGISTERED: {"gpu": 144, "quotas": ["q×144卡"]},
        "cpu-only": {"gpu": 0, "quotas": ["cpu×0卡"]},
    }

    def test_seen_with_cards_is_yes(self):
        self.assertEqual(assets.cards_state(HZ_UNREGISTERED, self.QUOTAS, []), assets.CARDS_YES)

    def test_seen_without_cards_is_no(self):
        self.assertEqual(assets.cards_state("cpu-only", self.QUOTAS, []), assets.CARDS_NO)

    def test_not_seen_while_a_region_failed_is_unknown_never_no(self):
        """**这条是承重的**：只要有地域没查成，查不到的空间一律「不知道」。
        判成「没卡」的话，一个 144 卡的空间会被当成空壳藏起来，而那正是要找的东西。"""
        for skipped in (["cn-heyuan：连不上"], ["a", "b"]):
            with self.subTest(skipped=skipped):
                self.assertEqual(assets.cards_state("590221", {}, skipped), assets.CARDS_UNKNOWN)
                self.assertNotEqual(assets.cards_state("590221", {}, skipped), assets.CARDS_NO)

    def test_all_regions_answered_and_still_not_seen_is_no(self):
        """全部地域都答上来了、仍然查不到 → 「没有专属配额」。
        （这一句的底气来自「地域都答上来了」；注意它仍受成员身份裁剪影响，
        所以 CLI 的文案写的是「没有专属算力配额」而不是「这个空间是空的」。）"""
        self.assertEqual(assets.cards_state("590221", self.QUOTAS, []), assets.CARDS_NO)

    def test_seen_with_zero_cards_is_not_affected_by_other_regions_failing(self):
        """查得到的空间说明**它所在的地域答上来了**，别的地域没查成与它无关。
        这条和上一条的边界要分清，否则「河源永远打不通」会让每一行都变成不知道，
        这一栏就等于没有。"""
        self.assertEqual(
            assets.cards_state("cpu-only", self.QUOTAS, ["cn-heyuan：连不上"]), assets.CARDS_NO
        )

    def test_blank_and_missing_ids(self):
        for wid in ("", None):
            with self.subTest(wid=wid):
                self.assertEqual(assets.cards_state(wid, self.QUOTAS, ["x"]), assets.CARDS_UNKNOWN)
                self.assertEqual(assets.cards_state(wid, self.QUOTAS, []), assets.CARDS_NO)

    def test_states_are_three_distinct_values(self):
        self.assertEqual(
            len({assets.CARDS_YES, assets.CARDS_NO, assets.CARDS_UNKNOWN}),
            3,
            "三态塌成两态的话，页面上「不知道」和「没有」就长一样了",
        )


class QuotaAnnotationTests(unittest.TestCase):
    """`cli_requests._quotas_for`：把配额结论贴到每一行上。**行一条都不能少。**"""

    def rows(self):
        return [
            {"platform": "aliyun", "region": "cn-hangzhou", "id": HZ_UNREGISTERED, "name": "ai_hz"},
            {"platform": "aliyun", "region": "cn-hangzhou", "id": "空壳", "name": "shell"},
            {"platform": "volcano", "region": "cn-shanghai", "id": "vw-1", "name": "火山"},
        ]

    def run_with(self, found, skipped, rows=None):
        rows = self.rows() if rows is None else rows
        with (
            mock.patch.dict(
                os.environ,
                {"ALIYUN_ACCESS_KEY_ID": "LTAItest", "ALIYUN_ACCESS_KEY_SECRET": "secret"},
            ),
            mock.patch.object(assets, "quotas_by_workspace", return_value=(found, skipped)) as spy,
        ):
            out = cli_requests._quotas_for(rows)
        return rows, out, spy

    def test_annotates_each_row_and_keeps_them_all(self):
        rows, (quotas, skipped), _ = self.run_with(
            {
                HZ_UNREGISTERED: {"gpu": 144, "quotas": ["q×144卡"]},
                "空壳": {"gpu": 0, "quotas": []},
            },
            [],
        )
        self.assertEqual(len(rows), 3, "**不许过滤**：没卡的、不知道的都要留在清单上")
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[HZ_UNREGISTERED]["cards"], assets.CARDS_YES)
        self.assertEqual(by_id[HZ_UNREGISTERED]["gpu"], 144)
        self.assertEqual(by_id[HZ_UNREGISTERED]["quotas"], ["q×144卡"])
        self.assertEqual(by_id["空壳"]["cards"], assets.CARDS_NO)
        self.assertEqual(quotas[HZ_UNREGISTERED]["gpu"], 144)
        self.assertEqual(skipped, [])

    def test_unknown_rows_stay_and_are_marked_unknown(self):
        """有地域没查成时，查不到的行是「不知道」，而且**还在清单里** ——
        被过滤掉的话，144 卡那个空间就此从所有视野里消失。"""
        rows, (_, skipped), _ = self.run_with({}, ["cn-heyuan：连不上"])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["cards"] == assets.CARDS_UNKNOWN for r in rows))
        self.assertEqual(skipped, ["cn-heyuan：连不上"])

    def test_volcano_rows_are_unknown_not_no(self):
        """这个配额接口只有阿里云有。火山的行没法判 —— 只能是「不知道」，
        绝不能因为「没查到」就给它盖一个「没卡」的结论。"""
        rows, _, _ = self.run_with({HZ_UNREGISTERED: {"gpu": 144, "quotas": []}}, [])
        volc = next(r for r in rows if r["platform"] == "volcano")
        self.assertEqual(volc["cards"], assets.CARDS_UNKNOWN)

    def test_no_credentials_marks_everything_unknown(self):
        """采集凭证都没有的时候，一行都不许判成「没卡」。"""
        rows = self.rows()
        with mock.patch.dict(os.environ, {}, clear=True):
            _, skipped = cli_requests._quotas_for(rows)
        self.assertTrue(all(r["cards"] == assets.CARDS_UNKNOWN for r in rows))
        self.assertTrue(skipped and "凭证" in skipped[0])

    def test_no_aliyun_rows_means_no_cloud_call(self):
        rows = [{"platform": "volcano", "region": "cn-shanghai", "id": "vw-1", "name": "火山"}]
        with mock.patch.object(assets, "quotas_by_workspace") as spy:
            self.assertEqual(cli_requests._quotas_for(rows), ({}, []))
        spy.assert_not_called()
        # 这条路径直接返回、不贴 `cards`（CLI 那边 `w.get("cards")` 拿到 None，
        # 那一行的结论栏是空的）。**载重的是它不能被说成「没卡」** ——
        # 要让文案完整的话，这里该补成 CARDS_UNKNOWN
        self.assertNotEqual(rows[0].get("cards"), assets.CARDS_NO)


class UnregisteredRegionTests(unittest.TestCase):
    def test_platform_qualified_known_does_not_mask_the_other_cloud(self):
        """**不变量②**：只登记了火山的上海，阿里的上海必须照样报出来。
        扁平集合的话阿里那条会被悄悄吃掉 —— 而被吃掉的表现和「没问题」一模一样。"""
        rows = assets.unregistered_regions(PROD_LIKE, {"volcano/cn-shanghai"})
        where = [w for w, _ in rows]
        self.assertIn("aliyun/cn-shanghai", where)
        self.assertNotIn("volcano/cn-shanghai", where)

    def test_known_region_written_with_oss_prefix_still_matches(self):
        """登记表不收 `oss-` 前缀，但模板里的桶地域是那种写法。
        不归一的话杭州会一直挂着「没登记」，报久了就没人看了。"""
        rows = assets.unregistered_regions(PROD_LIKE, {"aliyun/oss-cn-hangzhou"})
        self.assertNotIn("aliyun/cn-hangzhou", [w for w, _ in rows])

    def test_sorted_by_how_much_is_there(self):
        rows = assets.unregistered_regions(PROD_LIKE, set())
        counts = [sum(kinds.values()) for _, kinds in rows]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(rows[0][0], "aliyun/cn-hangzhou")  # 4 个

    def test_blank_known_entries_ignored(self):
        rows = assets.unregistered_regions(PROD_LIKE, ["", None, "  "])
        self.assertEqual(len(rows), len(assets.regions_in_use(PROD_LIKE)))

    def test_empty_snapshot_is_empty(self):
        for empty in (None, {}, {"accounts": []}):
            with self.subTest(empty=empty):
                self.assertEqual(assets.unregistered_regions(empty, {"aliyun/cn-hangzhou"}), [])


def tpl(tid, platform="aliyun", buckets=(), workspaces_=()):
    return catalog.Template(
        id=tid,
        kind="storage",
        platform=platform,
        account="1000000000000001",
        title=tid,
        buckets=tuple(buckets),
        workspaces=tuple(workspaces_),
    )


class RegistryReadingTests(unittest.TestCase):
    """CLI 侧从登记表 / 模板里读出来的三个集合。`_discover_regions` 的判据全靠它们。"""

    def registry(self):
        return workspaces.parse(
            {
                "workspaces": {
                    "hz": {
                        "label": "杭州",
                        "id": HZ_REGISTERED,
                        "region": "cn-hangzhou",
                        "bucket": "wuji-algo-dev-hz",
                        "bucket_region": "cn-hangzhou",
                    },
                    "sing": {
                        "label": "新加坡",
                        "id": "284761",
                        "region": "ap-southeast-1",
                        "bucket": "wuji-algo-dev-sing",
                        "bucket_region": "ap-southeast-1",
                    },
                }
            }
        )

    def test_workspace_ids_are_ids_not_regions(self):
        self.assertEqual(cli_requests.workspace_ids(self.registry()), {HZ_REGISTERED, "284761"})

    def test_workspace_ids_of_empty_registry(self):
        self.assertEqual(cli_requests.workspace_ids(workspaces.Registry()), set())

    def test_workspace_regions_carry_the_platform(self):
        self.assertEqual(
            cli_requests.workspace_regions(self.registry()),
            {"aliyun/cn-hangzhou", "aliyun/ap-southeast-1"},
        )

    def test_bucket_regions_carry_the_platform_and_strip_oss_prefix(self):
        cat = catalog.Catalog(
            (
                tpl("a", buckets=[("wuji-hz", "oss-cn-hangzhou"), ("wuji-bkk", "ap-southeast-7")]),
                tpl("b", platform="volcano", buckets=[("tos-sh", "cn-shanghai")]),
                tpl("c", buckets=[("no-region", "")]),  # 没写地域的跳过，不要拼出 `aliyun/`
            )
        )
        self.assertEqual(
            cli_requests.bucket_regions(cat),
            {"aliyun/cn-hangzhou", "aliyun/ap-southeast-7", "volcano/cn-shanghai"},
        )

    def test_a_registered_bucket_region_is_not_a_registered_workspace_region(self):
        """曼谷有个桶，不等于曼谷登记过工作空间 —— 线上曼谷那个 PAI 工作空间
        当初就是这么被遮掉、一直报不出来的。两个集合必须各算各的。"""
        cat = catalog.Catalog((tpl("a", buckets=[("wuji-bkk", "ap-southeast-7")]),))
        self.assertIn("aliyun/ap-southeast-7", cli_requests.bucket_regions(cat))
        self.assertNotIn("aliyun/ap-southeast-7", cli_requests.workspace_regions(self.registry()))


class TodoCollectorTests(unittest.TestCase):
    """`todo.collect_workspaces` / `collect_regions` 拿上面两个函数的输出做成待办条目。

    **只测这两个函数本身**：工作空间那条还没接进 `server._todo_view`（一个没有算力配额的
    工作空间是空壳，配额接口还在查），所以这里不断言它出现在页面上。
    """

    def rows(self):
        return assets.unregistered_workspaces(PROD_LIKE, {HZ_REGISTERED, "284761"})

    def test_workspace_item_counts_workspaces_not_regions(self):
        report = todo.Report()
        todo.collect_workspaces(report, self.rows())
        (item,) = report.items
        self.assertEqual(item.kind, "workspace_unregistered")
        # 两个工作空间在**同一个地域**里的那种情况，条数要按工作空间算
        self.assertEqual(item.count, 2)
        self.assertIn("ai_hz_gpu", item.title)

    def test_workspace_item_falls_back_to_id_when_unnamed(self):
        """没名字的工作空间也要能指认出来，否则待办上是一串顿号、没法照着去找。"""
        report = todo.Report()
        todo.collect_workspaces(
            report, [{"platform": "aliyun", "region": "cn-hangzhou", "id": "9", "name": ""}]
        )
        self.assertIn("9", report.items[0].title)

    def test_no_rows_no_item(self):
        """一条都没有时**不出待办**：常驻一条「0 个没登记」会让这一页变成噪音。"""
        for collect in (todo.collect_workspaces, todo.collect_regions):
            for rows in ([], None):
                with self.subTest(fn=collect.__name__, rows=rows):
                    report = todo.Report()
                    collect(report, rows)
                    self.assertEqual(report.items, [])

    def test_region_item_totals_the_resources(self):
        report = todo.Report()
        todo.collect_regions(report, assets.unregistered_regions(PROD_LIKE, set()))
        (item,) = report.items
        self.assertEqual(item.kind, "region_unregistered")
        self.assertEqual(item.count, len(assets.regions_in_use(PROD_LIKE)))
        # 标题里只写地域名（去掉平台前缀），但条数按「平台/地域」算，两朵云不合并
        self.assertIn("cn-hangzhou", item.title)


if __name__ == "__main__":
    unittest.main()
