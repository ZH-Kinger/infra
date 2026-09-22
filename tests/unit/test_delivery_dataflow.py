"""预热和沉降：CPFS ↔ OSS（阿里 NAS）、vePFS ↔ TOS（火山）。

**方向搞反不报错，只是把数据往相反方向覆盖一遍** —— 那不可逆。
所以这里断言的大多不是「返回了什么」，而是**发出去的请求里那几个字段**。
"""

import unittest

from delivery.clouds import nas, vepfs
from delivery.errors import DeliveryError


class NasDirectionTests(unittest.TestCase):
    def test_the_direction_comes_from_the_addresses(self):
        self.assertEqual(nas.direction("oss", "cpfs"), nas.ACTION_IMPORT)
        self.assertEqual(nas.direction("cpfs", "oss"), nas.ACTION_EXPORT)

    def test_other_pairs_are_not_this_chain(self):
        for a, b in (("oss", "oss"), ("cpfs", "cpfs"), ("tos", "cpfs"), ("cpfs", "tos")):
            self.assertIsNone(nas.direction(a, b), f"{a}->{b}")


class NormTests(unittest.TestCase):
    def test_a_directory_gets_slashes_on_both_ends(self):
        """少了开头那个，接口会当相对路径（相对什么没定义）；
        少了结尾那个，`/a/bc` 会被 `/a/b` 的绑定误判成覆盖得到。"""
        for raw in ("a/b", "/a/b", "a/b/", "/a/b/"):
            self.assertEqual(nas.norm_dir(raw), "/a/b/", raw)

    def test_empty_is_the_root(self):
        self.assertEqual(nas.norm_dir(""), "/")


class ResolveTests(unittest.TestCase):
    """挑绑定。**两头都要落在绑定里**：CPFS 目录在 FileSystemPath 之下，而且 OSS 那头
    是同一个桶、前缀在 SourceStoragePath 之下。形状照真机：
    `/share/data/ ↔ oss://wuji-bucket-hangzhou/teleop/`。"""

    #: 刻意**不放**绑在根 `/` 上的那种 —— 它覆盖一切，会让「找不到绑定」这条路走不到
    ROWS = [
        {"id": "df-share", "fs_path": "/share/data/", "bucket": "b1", "oss_path": "/teleop/"},
        {"id": "df-deep", "fs_path": "/share/data/x/", "bucket": "b1", "oss_path": "/teleop/x/"},
        {"id": "df-other", "fs_path": "/zzz/", "bucket": "b2", "oss_path": "/"},
    ]

    def test_the_longest_covering_binding_wins(self):
        """有多个能覆盖时最长的那个最贴近目标 —— 取到外层那条，
        任务的相对目录会被换算到另一个根上。"""
        got = nas.resolve(
            self.ROWS, fs_path="/share/data/x/y/", bucket="b1", oss_prefix="teleop/x/y/"
        )
        self.assertEqual(got["id"], "df-deep")

    def test_a_directory_with_no_binding_is_refused_loudly(self):
        """**面板不替人建绑定** —— 对已有数据的目录建流会把它清空。"""
        with self.assertRaises(DeliveryError) as caught:
            nas.resolve(self.ROWS, fs_path="/nowhere/", bucket="b1", oss_prefix="teleop/")
        self.assertIn("没有数据流动绑定", str(caught.exception))

    def test_a_binding_pointing_at_another_bucket_does_not_count(self):
        with self.assertRaises(DeliveryError):
            nas.resolve(self.ROWS, fs_path="/zzz/x/", bucket="b1", oss_prefix="")

    def test_the_oss_side_must_fall_under_the_binding_too(self):
        """**只对 CPFS 那头的话，会挑中一条绑在别的 OSS 前缀上的流** —— 相对目录被拼到
        那个前缀底下，读写的是另一块数据，而云上不会报错（审计 H-B）。"""
        with self.assertRaises(DeliveryError):
            nas.resolve(self.ROWS, fs_path="/share/data/", bucket="b1", oss_prefix="elsewhere/")


class RelativeTests(unittest.TestCase):
    """接口要**相对绑定根**的目录。传绝对路径的话，绑在 `/share/data/` 上的流收到
    `/share/data/x/` 会去读写 `/share/data/share/data/x/`，`CreateDirIfNotExist`
    还会把错目录建出来 —— 源为空时报「完成、0 个文件」，一次假成功。"""

    def test_a_path_under_the_root_becomes_relative(self):
        self.assertEqual(nas.relative("/share/data/x/", "/share/data/"), "/x/")

    def test_the_root_itself_is_slash(self):
        self.assertEqual(nas.relative("/share/data/", "/share/data/"), "/")

    def test_a_root_binding_leaves_the_path_alone(self):
        self.assertEqual(nas.relative("/a/b/", "/"), "/a/b/")

    def test_a_sibling_that_only_shares_a_prefix_is_not_under_it(self):
        """`/share/database/` 以 `/share/data` 开头，但不在 `/share/data/` 下面。"""
        with self.assertRaises(DeliveryError):
            nas.relative("/share/database/", "/share/data/")


class TaskShapeTests(unittest.TestCase):
    """真正下发的那组参数。**方向和相对路径两样都不报错**，所以都钉死在这里。
    结构与真机 DryRun 通过的那组一致（2026-09-22）。"""

    ROWS = [{"id": "df-1", "fs_path": "/share/data/", "bucket": "b1", "oss_path": "/teleop/"}]

    def _task(self, src, dst, **kw):
        from delivery import mover, moves

        return mover.dataflow_task(self.ROWS, moves.plan(src, dst), **kw)

    def test_import_reads_oss_and_writes_cpfs_both_relative(self):
        got = self._task("oss://b1/teleop/a/", "cpfs://bmcpfs-1/share/data/b/")
        self.assertEqual(got["action"], "Import")
        self.assertEqual((got["directory"], got["dst_directory"]), ("/a/", "/b/"))

    def test_export_reads_cpfs_and_writes_oss_both_relative(self):
        got = self._task("cpfs://bmcpfs-1/share/data/b/", "oss://b1/teleop/a/")
        self.assertEqual(got["action"], "Export")
        self.assertEqual((got["directory"], got["dst_directory"]), ("/b/", "/a/"))

    def test_the_overwrite_enum_is_the_documented_one(self):
        """枚举是 SKIP_THE_FILE / KEEP_LATEST / OVERWRITE_EXISTING（审计 M-3）。"""
        got = self._task(
            "oss://b1/teleop/a/", "cpfs://bmcpfs-1/share/data/b/", same_name="overwrite"
        )
        self.assertEqual(got["conflict"], "OVERWRITE_EXISTING")
        self.assertEqual(
            self._task("oss://b1/teleop/a/", "cpfs://bmcpfs-1/share/data/b/")["conflict"],
            "SKIP_THE_FILE",
        )


class Sent:
    """把发出去的请求记下来。断言的是这个，不是返回值。"""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply if reply is not None else {"TaskId": "task-9"}

    def __call__(self, *args, **kw):
        self.calls.append((args, kw))
        return self.reply


class NasSubmitTests(unittest.TestCase):
    def _submit(self, action, **kw):
        sent = Sent()
        self.addCleanup(setattr, nas.aliyun, "call", nas.aliyun.call)
        seen = {}

        def fake(endpoint, version, act, params, *, creds, transport=None):
            seen.update({"endpoint": endpoint, "action": act, "params": params})
            return {"TaskId": "task-9"}

        nas.aliyun.call = fake
        got = nas.submit(
            fs_id=kw.pop("fs_id", "bmcpfs-1"),
            region="cn-hangzhou",
            dataflow="df-1",
            action=action,
            creds=None,
            **kw,
        )
        del sent
        return got, seen

    def test_import_reads_from_oss_and_writes_into_the_filesystem(self):
        """Directory 永远是**源**那一侧，DstDirectory 是目的那一侧。
        写反的话预热会去读文件系统、往 OSS 写 —— 也就是把沉降干成了预热。"""
        _got, seen = self._submit(nas.ACTION_IMPORT, directory="batch/", dst_directory="/data/")
        self.assertEqual(seen["params"]["TaskAction"], "Import")
        self.assertEqual(seen["params"]["Directory"], "/batch/")
        self.assertEqual(seen["params"]["DstDirectory"], "/data/")

    def test_a_computing_edition_task_carries_the_conflict_policy(self):
        """智算版**必填** ConflictPolicy；通用版给了会被拒。"""
        _got, seen = self._submit(nas.ACTION_IMPORT, directory="a/", fs_id="bmcpfs-1")
        self.assertIn("ConflictPolicy", seen["params"])
        _got, plain = self._submit(nas.ACTION_EXPORT, directory="a/", fs_id="cpfs-1")
        self.assertNotIn("ConflictPolicy", plain["params"])

    def test_the_default_is_to_skip_not_overwrite(self):
        """覆盖不可逆，而这条链常常动的是别人的数据。"""
        _got, seen = self._submit(nas.ACTION_IMPORT, directory="a/")
        self.assertEqual(seen["params"]["ConflictPolicy"], nas.DEFAULT_CONFLICT)
        self.assertIn("SKIP", nas.DEFAULT_CONFLICT.upper())

    def test_a_bad_direction_is_refused(self):
        with self.assertRaises(DeliveryError):
            nas.submit(
                fs_id="bmcpfs-1",
                region="cn-hangzhou",
                dataflow="df-1",
                action="Whatever",
                directory="a/",
                creds=None,
            )

    def test_no_task_id_means_it_was_not_submitted(self):
        """返回空串的话单子会转「在途」，而轮询一个空任务号永远查不到 ——
        单子永远停在那儿，连「卡住」都不报。"""
        self.addCleanup(setattr, nas.aliyun, "call", nas.aliyun.call)
        nas.aliyun.call = lambda *a, **k: {"RequestId": "x"}
        with self.assertRaises(DeliveryError):
            nas.submit(
                fs_id="bmcpfs-1",
                region="cn-hangzhou",
                dataflow="df-1",
                action=nas.ACTION_IMPORT,
                directory="a/",
                creds=None,
            )


class NasPollTests(unittest.TestCase):
    def _poll(self, reply):
        self.addCleanup(setattr, nas.aliyun, "call", nas.aliyun.call)
        nas.aliyun.call = lambda *a, **k: reply
        return nas.poll(fs_id="bmcpfs-1", region="cn-hangzhou", task_id="t", creds=None)

    def test_the_failure_reason_is_read_from_the_right_field(self):
        """失败原因是 `ErrorMsg`，不是 `ErrorMessage` —— 读错字段的后果是
        所有失败都退化成一句光秃秃的「任务 Failed」。"""
        got = self._poll({"Status": "Failed", "ErrorMsg": "源目录不存在"})
        self.assertTrue(got["failed"])
        self.assertIn("源目录", got["error"])

    def test_progress_is_dug_out_of_the_nested_object(self):
        """计数在 ProgressStats 子对象里，顶层取不到。"""
        got = self._poll(
            {"Status": "Executing", "ProgressStats": {"BytesDone": 42, "FilesDone": 7}}
        )
        self.assertEqual((got["bytes"], got["objects"]), (42, 7))
        self.assertFalse(got["done"])

    def test_a_flaky_query_is_not_a_failure(self):
        """轮询是网络抖动的高发点。判失败的话一次抖动就把在跑的任务判死了。"""
        self.addCleanup(setattr, nas.aliyun, "call", nas.aliyun.call)

        def boom(*_a, **_k):
            raise RuntimeError("连接重置")

        nas.aliyun.call = boom
        got = nas.poll(fs_id="f", region="cn-hangzhou", task_id="t", creds=None)
        self.assertFalse(got["failed"])
        self.assertFalse(got["done"])


class VepfsTests(unittest.TestCase):
    def test_the_direction_comes_from_the_addresses(self):
        self.assertEqual(vepfs.direction("tos", "vepfs"), vepfs.ACTION_IMPORT)
        self.assertEqual(vepfs.direction("vepfs", "tos"), vepfs.ACTION_EXPORT)
        self.assertIsNone(vepfs.direction("oss", "vepfs"))

    def test_a_bucket_name_with_a_scheme_is_refused_before_the_call(self):
        """带前缀会被云上回 `InvalidParameter.BucketName`，而那个报错
        不会说问题出在前缀上。"""
        with self.assertRaises(DeliveryError) as caught:
            vepfs.submit(
                fs_id="vepfs-1",
                region="cn-shanghai",
                action=vepfs.ACTION_IMPORT,
                bucket="tos://b",
                creds=None,
            )
        self.assertIn("裸名", str(caught.exception))

    def test_paths_carry_slashes_on_both_ends_but_empty_stays_empty(self):
        """非空要首尾带斜杠；空串是合法的（桶根）。"""
        self.assertEqual(vepfs.norm_dir("a/b"), "/a/b/")
        self.assertEqual(vepfs.norm_dir(""), "")

    def test_an_ambiguous_status_is_read_as_failure_not_success(self):
        """`Unsuccessful` 同时含 success 和 unsuccess。判成成功的话，
        一个失败的沉降会被当成搬完了 —— 而人会照着那个结论去删源数据。"""
        self.assertTrue(vepfs.is_failed("Unsuccessful"))
        self.assertFalse(vepfs.is_done("Unsuccessful"))

    def test_a_failure_without_a_reason_still_says_what_to_check(self):
        """前置没配好时任务建得出来但直接失败，云上不一定给原因。
        留空的话人只看到「失败」两个字。"""
        self.addCleanup(setattr, vepfs.volcano, "call", vepfs.volcano.call)
        vepfs.volcano.call = lambda *a, **k: {"Status": "Failed"}
        got = vepfs.poll(fs_id="vepfs-1", region="cn-shanghai", task_id="t", creds=None)
        self.assertTrue(got["failed"])
        self.assertIn("同地域", got["error"])

    def test_the_default_policy_never_overwrites(self):
        self.assertEqual(vepfs.policy(""), "Skip")
        self.assertEqual(vepfs.policy("skip"), "Skip")
        self.assertEqual(vepfs.policy("overwrite"), "OverWrite")


if __name__ == "__main__":
    unittest.main()


class VepfsPollShapeTests(unittest.TestCase):
    """查进度那个请求的两个字段。**都是真机踩出来的，而且都不报错。**

    第一次按直觉写成「数组 + 不分页」，真机回 `InvalidParameter`；
    改成分页之后才发现 bot 那边早有注释：不带分页时云上返回 `TotalCount > 0`
    但列表是空的 —— 于是永远拿不到任务、status 恒为空，**单子永远卡在「在途」**，
    不报错、不失败，连「卡住」都不报。
    """

    def _body(self):
        seen = {}
        self.addCleanup(setattr, vepfs.volcano, "call", vepfs.volcano.call)

        def fake(service, version, action, *, creds, region, body=None, transport=None):
            seen.update(body or {})
            return {"Status": "Success"}

        vepfs.volcano.call = fake
        vepfs.poll(fs_id="vepfs-1", region="cn-shanghai", task_id="12345", creds=None)
        return seen

    def test_the_task_id_goes_as_a_string_not_a_list(self):
        got = self._body()
        self.assertEqual(got["DataFlowTaskIds"], "12345")
        self.assertNotIsInstance(got["DataFlowTaskIds"], list, "传数组会被回 InvalidParameter")

    def test_paging_is_always_sent(self):
        got = self._body()
        self.assertEqual(got["PageNumber"], 1)
        self.assertGreaterEqual(got["PageSize"], 1)


class VepfsSubmitShapeTests(unittest.TestCase):
    def _body(self, **kw):
        seen = {}
        self.addCleanup(setattr, vepfs.volcano, "call", vepfs.volcano.call)

        def fake(service, version, action, *, creds, region, body=None, transport=None):
            seen.update(body or {})
            return {"DataFlowTaskId": "t-1"}

        vepfs.volcano.call = fake
        vepfs.submit(fs_id="vepfs-1", region="cn-shanghai", bucket="b", creds=None, **kw)
        return seen

    def test_the_direction_is_what_actually_goes_out(self):
        """搞反不报错，只是把数据往相反方向覆盖一遍。"""
        self.assertEqual(self._body(action=vepfs.ACTION_IMPORT)["TaskAction"], "Import")
        self.assertEqual(self._body(action=vepfs.ACTION_EXPORT)["TaskAction"], "Export")

    def test_the_bucket_goes_bare(self):
        self.assertEqual(self._body(action=vepfs.ACTION_IMPORT)["DataStorage"], "b")

    def test_an_empty_prefix_stays_empty_not_a_lone_slash(self):
        """空串是合法的（桶根）。写成 `/` 的话范围就变成了「根目录这一个」。"""
        self.assertEqual(self._body(action=vepfs.ACTION_IMPORT, prefix="")["DataStoragePath"], "")
