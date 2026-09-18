"""每个人那块地方：`桶/组/人`（OSS）和 `/人/`（CPFS）。

这个模块算出来的东西会变成**生产桶里的目录名和 PAI 数据集的 Uri**，一旦建出来就
长期留在那儿。所以三件事必须钉死：

1. **认不出属主的号不建。** 给一个认不出是谁的号建目录，等于亲手制造下一条「无主资产」——
   而今天的体检清单上正好有 5 条那样的东西。
2. **组名和登录名要过白名单。** 这两段会拼进 OSS 的 key 和 RAM 策略的 `oss:Prefix`
   条件里，一个 `*` 或 `../` 就能让一条策略覆盖到别人的目录。
3. **两种布局各有各的理由**，不能互相套用：CPFS 存量三十个目录全是扁平的，加一层组
   只会让两种形状并存；OSS 是新桶，可以分层。

离线，数据虚构。
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from delivery import provision_tree
from delivery import workspace_tree as tree


@dataclass
class FakeUser:
    name: str
    groups: tuple = ()
    platform: str = "aliyun"


def users(*pairs):
    return [FakeUser(name=n, groups=tuple(g)) for n, g in pairs]


ROSTER = {"wangzihan": "王梓涵", "chuzhong": "储众", "lichaoqi": "李超奇"}


class GroupedTests(unittest.TestCase):
    def plan(self, us, **kw):
        kw.setdefault("people", ROSTER)
        return tree.plan(us, layout=tree.LAYOUT_GROUPED, **kw)

    def test_the_path_is_bucket_group_person(self):
        slots, _ = self.plan(users(("wangzihan", ["wuji_Algorithm"])))
        self.assertEqual(slots[0].prefix, "wuji_Algorithm/wangzihan/")

    def test_somebody_the_roster_cannot_name_gets_nothing(self):
        """认不出是谁就不建 —— 否则今天体检清单上那 5 条「无主」会变成 6 条。"""
        slots, skipped = self.plan(users(("mystery", ["wuji_Algorithm"])))
        self.assertEqual(slots, [])
        self.assertTrue(any("认不出" in s for s in skipped))

    def test_service_accounts_get_nothing(self):
        slots, skipped = self.plan(
            users(("tempak-abc-123", []), ("wuji-ci", [])), services=["wuji-ci"]
        )
        self.assertEqual(slots, [])
        self.assertEqual(len(skipped), 2)

    def test_somebody_with_no_group_is_flagged_not_guessed(self):
        """不编造组名。猜一个「看起来对」的组，比明摆着说「这个人还没归位」更糟：
        前者没人会去修，后者一眼看得见。现网正好有一个这样的人。"""
        slots, skipped = self.plan(users(("lichaoqi", [])))
        self.assertEqual(slots, [])
        self.assertTrue(any("不在任何用户组" in s for s in skipped))

    def test_somebody_in_two_groups_needs_a_human(self):
        """两个组 = 两条路径都说得通，而路径只能有一条。自动挑一个必然有一半人是错的。"""
        slots, skipped = self.plan(users(("wangzihan", ["a", "b"])))
        self.assertEqual(slots, [])
        self.assertTrue(any("同时属于" in s for s in skipped))

    def test_a_dangerous_segment_is_refused(self):
        """这两段会拼进 oss:Prefix 条件里。`*` 能让一条策略覆盖到别人的目录。"""
        for group in ("*", "../etc", "a b", "-lead", ""):
            with self.assertRaises(tree.TreeError, msg=group):
                self.plan(users(("wangzihan", [group])))


class FlatTests(unittest.TestCase):
    def plan(self, us):
        return tree.plan(us, people=ROSTER, layout=tree.LAYOUT_FLAT)

    def test_the_path_has_no_group_in_it(self):
        slots, _ = self.plan(users(("wangzihan", ["wuji_Algorithm"])))
        self.assertEqual(slots[0].prefix, "wangzihan/")

    def test_no_group_is_fine_here(self):
        """扁平布局下组不进路径，所以没组、多个组都不影响 —— 这正是它的好处。"""
        slots, skipped = self.plan(users(("lichaoqi", []), ("wangzihan", ["a", "b"])))
        self.assertEqual(sorted(s.prefix for s in slots), ["lichaoqi/", "wangzihan/"])
        self.assertEqual(skipped, [])

    def test_a_bad_layout_name_is_refused(self):
        with self.assertRaises(tree.TreeError):
            tree.plan([], layout="whatever")


class MoveTests(unittest.TestCase):
    """人换组了，目录该从哪搬到哪。**只算不搬。**"""

    def slots(self, *pairs):
        got, _ = tree.plan(users(*pairs), people=ROSTER, layout=tree.LAYOUT_GROUPED)
        return got

    def test_a_group_change_shows_up_as_a_move(self):
        slots = self.slots(("wangzihan", ["data"]))
        got = tree.moves(slots, ["algo/wangzihan/"])
        self.assertEqual(len(got), 1)
        self.assertEqual(
            (got[0].old_prefix, got[0].new_prefix), ("algo/wangzihan/", "data/wangzihan/")
        )

    def test_staying_put_is_not_a_move(self):
        slots = self.slots(("wangzihan", ["algo"]))
        self.assertEqual(tree.moves(slots, ["algo/wangzihan/"]), [])

    def test_people_are_matched_by_login_not_by_path(self):
        """登录名是最后一段、组是第一段。靠路径认人的话，换组之后就认不出是同一个人了。"""
        slots = self.slots(("wangzihan", ["data"]), ("chuzhong", ["algo"]))
        got = tree.moves(slots, ["algo/wangzihan/", "algo/chuzhong/"])
        self.assertEqual([m.login for m in got], ["wangzihan"])

    def test_a_directory_nobody_should_have_is_reported_not_deleted(self):
        """人删号了、或者名册认不出了。和「人的号删了东西还留着」是同一类东西，
        处置也一样：先找他原来的组确认，**不自动删**。"""
        slots = self.slots(("wangzihan", ["algo"]))
        self.assertEqual(tree.strays(slots, ["algo/zhujl/"]), ["algo/zhujl/"])


class TargetTests(unittest.TestCase):
    def targets(self, layout, uri_of, source):
        got, _ = provision_tree.plan(
            users(("wangzihan", ["wuji_Algorithm"])),
            people=ROSTER,
            layout=layout,
            uri_of=uri_of,
            source=source,
            region="cn-hangzhou",
            workspace="590221",
        )
        return got

    def test_oss_uri_matches_the_shape_already_in_production(self):
        got = self.targets(
            tree.LAYOUT_GROUPED,
            lambda p: provision_tree.oss_uri("wuji-algo-dev-hz", "oss-cn-hangzhou", p),
            "OSS",
        )
        self.assertEqual(
            got[0].uri,
            "oss://wuji-algo-dev-hz.oss-cn-hangzhou.aliyuncs.com/wuji_Algorithm/wangzihan/",
        )

    def test_cpfs_uri_follows_the_existing_thirty_not_the_docs(self):
        """文档给的是 `nas://<fsid>.<region>/…`，现网 30 条全是 `bmcpfs://<挂载点域名>/…`。
        跟着文档走，新建的和老的在控制台里会长成两种东西。"""
        got = self.targets(
            tree.LAYOUT_FLAT,
            lambda p: provision_tree.cpfs_uri("cpfs-x-vpc-y.cn-hangzhou.cpfs.aliyuncs.com", p),
            "BMCPFS",
        )
        self.assertEqual(
            got[0].uri, "bmcpfs://cpfs-x-vpc-y.cn-hangzhou.cpfs.aliyuncs.com/wangzihan/"
        )

    def test_the_dataset_name_equals_the_last_path_segment(self):
        """看到名字就知道路径。现网 30 条里有 15 条对不上（`wzh` 对 `wangzihan`），
        那是手工建的历史 —— 新建的不再制造这种。"""
        got = self.targets(tree.LAYOUT_FLAT, lambda p: f"oss://b/{p}", "OSS")
        self.assertEqual(got[0].name, got[0].prefix.strip("/"))

    def test_already_built_ones_are_not_built_twice(self):
        got = self.targets(tree.LAYOUT_FLAT, lambda p: f"oss://b/{p}", "OSS")
        existing = [{"workspace": "590221", "name": "wangzihan", "uri": "oss://b/wangzihan/"}]
        todo, managed = provision_tree.to_create(got, existing)
        self.assertEqual(todo, [])
        self.assertEqual(len(managed), 1)
        # 别的工作空间里有一条同名的，不算已建 —— 现网 8 条路径就是两边各一份
        other = [{"workspace": "640957", "name": "wangzihan", "uri": "oss://b/wangzihan/"}]
        self.assertEqual(len(provision_tree.to_create(got, other)[0]), 1)

    def test_an_existing_one_under_a_different_name_is_recognised(self):
        """**纳管**：存量是手工建的，名字五花八门（`wzh` 对 `wangzihan`）。
        按名字判重的话会给这些人再建一个空目录 —— 而他们的数据在老路径下面。
        所以判重按**属主 + 存储类型**，不按名字。"""
        got = self.targets(tree.LAYOUT_FLAT, lambda p: f"oss://b/{p}", "OSS")
        legacy = [
            {
                "workspace": "590221",
                "name": "wzh",
                "source": "OSS",
                "uri": "oss://b/wzh/",
                "owner_login": "wangzihan",
            }
        ]
        todo, managed = provision_tree.to_create(got, legacy)
        self.assertEqual(todo, [])
        self.assertEqual([t.login for t in managed], ["wangzihan"])

    def test_another_bucket_is_not_his_slot_in_this_one(self):
        """**B-2**：`wuji-algo-dev-hz` 是新建的空桶，此前不存在任何个人 OSS 数据集。
        所以属主是本人的任何一条 OSS 数据集（`lerobot_epic` 那类）必然不是他在新桶里
        的那份 —— 不比存储位置的话，他会被静默跳过，既没目录也没数据集。"""
        got = self.targets(
            tree.LAYOUT_FLAT, lambda p: "oss://new-bucket.oss-cn-hangzhou.aliyuncs.com/" + p, "OSS"
        )
        elsewhere = [
            {
                "workspace": "590221",
                "name": "lerobot_epic",
                "source": "OSS",
                "uri": "oss://other-bucket.oss-cn-hangzhou.aliyuncs.com/public/lerobot/",
                "owner_login": "wangzihan",
            }
        ]
        todo, managed = provision_tree.to_create(got, elsewhere, location=got[0].uri)
        self.assertEqual([t.login for t in todo], ["wangzihan"])
        self.assertEqual(managed, [])

    def test_the_same_filesystem_under_a_different_uri_spelling_still_counts(self):
        """现网 CPFS 有两种 URI 写法（`cpfs-…-vpc-x.…` 和 `bmcpfs-….cn-hangzhou`）。
        不归一的话，用第二种写法那条会掉出纳管、被重复建。"""
        got = self.targets(
            tree.LAYOUT_FLAT,
            lambda p: "bmcpfs://cpfs-00000ub3-vpc-egtdgw.cn-hangzhou.cpfs.aliyuncs.com/" + p,
            "BMCPFS",
        )
        legacy = [
            {
                "workspace": "590221",
                "name": "wzh",
                "source": "BMCPFS",
                "uri": "bmcpfs://bmcpfs-00000ub3.cn-hangzhou/wzh/",
                "owner_login": "wangzihan",
            }
        ]
        todo, managed = provision_tree.to_create(got, legacy, location=got[0].uri)
        self.assertEqual(todo, [])
        self.assertEqual([t.login for t in managed], ["wangzihan"])

    def test_a_different_storage_type_is_not_the_same_slot(self):
        """同一个人的 CPFS 数据集不能算成他的 OSS 那一份 —— 两块地方是分开的。"""
        got = self.targets(tree.LAYOUT_FLAT, lambda p: f"oss://b/{p}", "OSS")
        cpfs = [
            {
                "workspace": "590221",
                "name": "wzh",
                "source": "BMCPFS",
                "uri": "bmcpfs://x/wzh/",
                "owner_login": "wangzihan",
            }
        ]
        todo, managed = provision_tree.to_create(got, cpfs)
        self.assertEqual(len(todo), 1)
        self.assertEqual(managed, [])

    def test_same_name_different_path_is_reported_for_a_human(self):
        """同名建不出来，而路径不同意味着有人手工建过一条指向别处的。
        自动改它可能让正在跑的任务读不到东西。"""
        got = self.targets(tree.LAYOUT_FLAT, lambda p: f"oss://b/{p}", "OSS")
        clash = provision_tree.collisions(
            got, [{"workspace": "590221", "name": "wangzihan", "uri": "oss://b/somewhere-else/"}]
        )
        self.assertEqual(len(clash), 1)
        self.assertIn("somewhere-else", clash[0][1])


class MovePrefixTests(unittest.TestCase):
    """搬目录：**复制 + 对账，不删源。**

    逐对象复制不是原子的 —— 中途断了两边各有一半，而那时候「没报错的那些」看起来
    一切正常。所以只看「复制没报错」不行，必须对完账。
    """

    class FakeOss:
        COPY_MAX = 900 * 1024 * 1024

        def __init__(self, objects, *, fail_after=None, drop=(), forbid_overwrite=True):
            self.objects = dict(objects)
            self.fail_after = fail_after
            self.drop = set(drop)
            # **默认开**：真的 copy_object 带 x-oss-forbid-overwrite，
            # 假的不带就测不出「断过一次之后还能不能续跑」
            self.forbid_overwrite = forbid_overwrite
            self.copies = []

        def list_objects(self, bucket, prefix, **kw):
            return [(k, v) for k, v in sorted(self.objects.items()) if k.startswith(prefix)]

        def copy_object(self, bucket, src, dst, **kw):
            if self.fail_after is not None and len(self.copies) >= self.fail_after:
                raise RuntimeError("网络断了")
            if self.forbid_overwrite and dst in self.objects:
                from delivery.clouds.oss import OssError

                raise OssError("409 目的端已存在", code="FileAlreadyExists")
            self.copies.append((src, dst))
            if src not in self.drop:
                self.objects[dst] = self.objects[src]

    def move(self, **kw):
        return tree.Move(
            login="chuzhong", old_prefix="pretrain/chuzhong/", new_prefix="rl/chuzhong/", **kw
        )

    def test_a_clean_move_copies_everything_and_reconciles(self):
        fake = self.FakeOss({"pretrain/chuzhong/a.pt": 10, "pretrain/chuzhong/b/c.pt": 20})
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertTrue(got.ok)
        self.assertEqual(got.copied, 2)
        self.assertIn(("pretrain/chuzhong/b/c.pt", "rl/chuzhong/b/c.pt"), fake.copies)

    def test_the_source_is_never_deleted(self):
        """复制可逆、删不可逆 —— 让可逆的自动、不可逆的人来。"""
        fake = self.FakeOss({"pretrain/chuzhong/a.pt": 10})
        provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertIn("pretrain/chuzhong/a.pt", fake.objects)
        self.assertFalse(hasattr(fake, "deleted"))

    def test_a_silently_lost_object_fails_reconciliation(self):
        """复制调用全都「成功」了，但有一个对象没真的落地。
        只看「没报错」的话这里会报搬完了 —— 而那正是最坏的结局：报成功但少数据。"""
        fake = self.FakeOss(
            {"pretrain/chuzhong/a.pt": 10, "pretrain/chuzhong/b.pt": 20},
            drop={"pretrain/chuzhong/b.pt"},
        )
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertFalse(got.ok)
        self.assertIn("没对上", got.note)

    def test_an_oversized_object_is_reported_not_skipped_silently(self):
        """大于 COPY_MAX 的要走分片复制，这里不做 —— 但**必须报出来**，
        悄悄漏掉一个文件比直接失败糟得多。"""
        fake = self.FakeOss({"pretrain/chuzhong/huge.bin": 2 * 1024**3})
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertEqual(got.too_big, ("pretrain/chuzhong/huge.bin",))
        self.assertFalse(got.ok)

    def test_extra_stuff_at_the_destination_does_not_fail_it(self):
        """判据是「源的每个对象都在且字节一致」，不是两边数量相等 ——
        目的端可能本来就有别的东西（上次搬了一半），拿数量当判据会让正常情况报失败。"""
        fake = self.FakeOss({"pretrain/chuzhong/a.pt": 10, "rl/chuzhong/old-stuff.pt": 99})
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertTrue(got.ok)

    def test_an_empty_source_is_not_an_error(self):
        fake = self.FakeOss({})
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertTrue(got.ok)
        self.assertEqual(got.copied, 0)

    def test_a_resumed_move_skips_what_is_already_there(self):
        """**这条钉住的是「搬目录能不能重跑」。**

        复制带了 `x-oss-forbid-overwrite`，目的端已存在时 OSS 回 409。把 409 和
        「网络断了」当成一回事的话，断过一次之后第一个对象就撞墙、后面的永远搬不过去，
        而每次重跑结果一模一样，人只会以为「又断了」。
        """
        fake = self.FakeOss(
            {
                "pretrain/chuzhong/a.pt": 10,
                "pretrain/chuzhong/b.pt": 20,
                "pretrain/chuzhong/c.pt": 30,
                # 上一趟已经搬过去的两个
                "rl/chuzhong/a.pt": 10,
                "rl/chuzhong/b.pt": 20,
            }
        )
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertTrue(got.ok, got.note)
        self.assertEqual(got.already, 2)
        self.assertEqual(got.copied, 1)  # 只补了 c
        self.assertIn(("pretrain/chuzhong/c.pt", "rl/chuzhong/c.pt"), fake.copies)

    def test_a_different_file_at_the_destination_is_a_conflict_not_an_overwrite(self):
        """同名但字节不同 —— 不覆盖、不猜，交给人看。"""
        fake = self.FakeOss({"pretrain/chuzhong/a.pt": 10, "rl/chuzhong/a.pt": 999})
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertFalse(got.ok)
        self.assertEqual(got.conflicts, ("pretrain/chuzhong/a.pt",))
        self.assertEqual(fake.objects["rl/chuzhong/a.pt"], 999)  # 没被盖掉

    def test_a_mid_flight_failure_reports_how_far_it_got(self):
        """裸抛异常的话，调用方不知道复制了几个、停在哪 —— 人会倾向于从头再来，
        或者更糟：以为没搬成而去手工 ossutil 搞一遍。"""
        fake = self.FakeOss({f"pretrain/chuzhong/{i}.pt": 10 for i in range(5)}, fail_after=2)
        got = provision_tree.move_prefix("b", self.move(), region="r", creds=None, oss=fake)
        self.assertFalse(got.ok)
        self.assertEqual(got.copied, 2)
        self.assertIn("中断", got.note)

    def test_the_destination_is_never_overwritten(self):
        """`copy_object` 带 `x-oss-forbid-overwrite`。「面板没有 DeleteObject 所以
        删不掉东西」这个推理对覆盖不成立 —— 覆盖只要 PutObject，而策略给了。"""
        from delivery.clouds import oss

        seen = {}

        def transport(url, method, headers, body=b""):
            seen.update(headers)
            return 200, b""

        oss.copy_object(
            "bk",
            "a/x",
            "b/x",
            region="oss-cn-hangzhou",
            creds=__import__("delivery.clouds.aliyun", fromlist=["x"]).Credentials("A", "S"),
            transport=transport,
        )
        self.assertEqual(seen.get("x-oss-forbid-overwrite"), "true")


class CustomDatasetTests(unittest.TestCase):
    """自定义数据集：把一条已有路径登记成数据集。

    **URI 白名单是这个功能唯一的控制点。** PAI 在 DSW 里读写存储走的是服务角色
    （`Resource: *`、无 Condition），和申请人自己的 RAM 策略没关系 —— 所以
    「登记一条指向某路径的数据集」等价于「拿到那个路径的读写权限」。
    """

    def ok(self, **kw):
        from delivery import custom_dataset

        args = dict(
            name="lerobot-epic",
            bucket="wuji-algo-dev-hz",
            prefix="shared/lerobot/",
            source="OSS",
            workspace="640957",
            region="cn-hangzhou",
            # 桶地域是另一套写法（带 oss- 前缀），和上面的 region 不是同一个字段。
            # 默认值放在这个 helper 里、不放进 validate()：它必须是必填的，见下面那条用例
            oss_region="oss-cn-hangzhou",
            allowed=["wuji-algo-dev-hz"],
        )
        args.update(kw)
        return custom_dataset.validate(**args)

    def bad(self, **kw):
        from delivery import custom_dataset

        with self.assertRaises(custom_dataset.CustomDatasetError):
            self.ok(**kw)

    def test_a_normal_one_passes_and_is_normalised(self):
        got = self.ok(prefix="/shared/lerobot")
        self.assertEqual(got.prefix, "shared/lerobot/")
        self.assertEqual(got.bucket, "wuji-algo-dev-hz")

    def test_an_oss_dataset_without_a_prefixed_region_is_refused(self):
        """地域填错/漏填，这条路一次 OSS 调用都不打 —— PAI 收下、返回 DatasetId、
        打印「建好了」，等有人挂载才发现。所以它只能在登记这一刻拦。"""
        for bad in ("", None, "cn-hangzhou", " ", "hangzhou"):
            with self.subTest(bad):
                self.bad(oss_region=bad)
        # CPFS 不看这个字段：它的定位参数是 fs_id / mount
        self.ok(
            source="BMCPFS",
            bucket="bmcpfs-mine",
            fs_id="bmcpfs-mine",
            mount="cpfs-mine-vpc-x.cn-hangzhou.cpfs.aliyuncs.com",
            allowed=["bmcpfs-mine"],
            oss_region="",
        )

    def test_a_bucket_outside_the_allowlist_is_refused(self):
        """这一条挡住的是：注册一条指向财务桶的数据集，挂进 DSW 就读到了。"""
        self.bad(bucket="wuji-finance")

    def test_an_empty_allowlist_refuses_everything(self):
        """**没配 ≠ 都放行。** 配置缺失时放行是这类控制点最常见的死法。"""
        self.bad(allowed=[])
        self.bad(allowed=None)

    def test_the_bucket_root_is_refused(self):
        """整个桶登记成一个数据集，等于把桶里所有人的东西都开出去。"""
        self.bad(prefix="")
        self.bad(prefix="/")

    def test_a_traversing_path_is_refused(self):
        for bad in ("a/../b", "a/*/b", "a/ b", "-lead/x", "a//b/.."):
            with self.subTest(bad):
                self.bad(prefix=bad)

    def test_a_bad_name_is_refused(self):
        for bad in ("", "-x", "a b", "中文名", "x" * 64, "a/b"):
            with self.subTest(bad):
                self.bad(name=bad)

    def test_an_unknown_storage_type_is_refused(self):
        self.bad(source="NAS")
        self.bad(source="")

    def test_a_cpfs_filesystem_outside_the_allowlist_is_refused(self):
        """**白名单对 CPFS 也得生效。**

        真正决定 PAI 挂哪个文件系统的是 `fs_id`/`mount`，不是 `bucket`。只校验 bucket
        的话，拿一个白名单里的 bucket 过检、再传别人的 fs_id 就绕过去了 —— 而模块注释
        和配置文件都写着「这是唯一的控制点」。
        """
        self.bad(
            source="BMCPFS",
            bucket="bmcpfs-mine",
            fs_id="bmcpfs-someone-else",
            mount="cpfs-someone-else-vpc-x.cn-hangzhou.cpfs.aliyuncs.com",
            allowed=["bmcpfs-mine"],
        )

    def test_a_mount_pointing_at_another_filesystem_is_refused(self):
        """fs_id 过了检，但挂载点指向别的文件系统 —— 那白名单一样白校验。"""
        self.bad(
            source="BMCPFS",
            bucket="bmcpfs-mine",
            fs_id="bmcpfs-mine",
            mount="cpfs-someone-else-vpc-x.cn-hangzhou.cpfs.aliyuncs.com",
            allowed=["bmcpfs-mine"],
        )

    def test_a_matching_cpfs_passes_and_carries_the_ids_on_the_spec(self):
        """校验过的值要挂在 Spec 上 —— 调用方只许用 spec.*，
        回头去用 args.fs_id 的话白名单就成了纯装饰。"""
        got = self.ok(
            source="BMCPFS",
            bucket="bmcpfs-mine",
            fs_id="bmcpfs-mine",
            mount="cpfs-mine-vpc-x.cn-hangzhou.cpfs.aliyuncs.com",
            allowed=["bmcpfs-mine"],
        )
        self.assertEqual(got.fs_id, "bmcpfs-mine")
        self.assertIn("cpfs-mine", got.mount)

    def test_a_missing_allowlist_file_yields_an_empty_list_which_refuses(self):
        from delivery import custom_dataset

        self.assertEqual(custom_dataset.load_allowed("/nope/nothing.json"), [])
        self.assertEqual(custom_dataset.load_allowed(None), [])


if __name__ == "__main__":
    unittest.main()
