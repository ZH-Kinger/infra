"""阿里在线迁移（MGW）里不碰网络的那部分。

重点是 `verify_address`。它的兜底是**放行**，所以状态串认错了不会报错、
不会失败、不会有任何征兆 —— 只是每个地址白等满 30 秒然后照样放行。
搬过来时就是这样：认的是 `VERIFY_SUCCESS`，真机回的是小写 `available`。

所以这里的断言盯的不是「返回了没有」，而是**睡了几次**。
"""

import unittest

from delivery.clouds import mgw
from delivery.errors import DeliveryError


class FakeResp:
    """`resp.body.verify_address_response.status` 那三层。"""

    def __init__(self, status, why=""):
        self.body = type(
            "B",
            (),
            {"verify_address_response": type("V", (), {"status": status, "error_message": why})()},
        )()


class FakeClient:
    def __init__(self, *statuses):
        self.queue = list(statuses)
        self.calls = 0

    def verify_address(self, user_id, name):
        self.calls += 1
        got = self.queue.pop(0) if self.queue else self.queue
        if isinstance(got, Exception):
            raise got
        return FakeResp(got)


class VerifyAddressTests(unittest.TestCase):
    def _run(self, cli, **kw):
        naps = []
        mgw.verify_address(cli, "uid", "addr", sleep=naps.append, **kw)
        return naps

    def test_the_real_status_string_returns_on_the_first_try(self):
        """真机实测回的是小写 `available`。认不出它的代价不是报错，是白等 30 秒。"""
        cli = FakeClient("available")
        self.assertEqual(self._run(cli), [])
        self.assertEqual(cli.calls, 1)

    def test_case_and_padding_do_not_matter(self):
        for raw in ("AVAILABLE", " Available ", "VERIFY_SUCCESS", "ok"):
            self.assertEqual(self._run(FakeClient(raw)), [], raw)

    def test_an_empty_status_means_still_checking(self):
        """刚建好时校验还没跑完，status 是空的 —— 要等，不是要报错。"""
        cli = FakeClient("", "", "available")
        self.assertEqual(len(self._run(cli)), 2)
        self.assertEqual(cli.calls, 3)

    def test_an_unrecognised_status_is_refused_not_waited_out(self):
        """认不出的状态一律当失败。当成「还在查」的话，
        一个真·校验不过的地址会安静地等满超时，然后按通过放行。"""
        for bad in ("unavailable", "failed", "denied"):
            with self.assertRaises(DeliveryError, msg=bad):
                self._run(FakeClient(bad))

    def test_a_flaky_call_is_retried_not_treated_as_a_failure(self):
        """校验接口自己抖一下不该判地址不通。"""
        cli = FakeClient(RuntimeError("连接重置"), "available")
        self.assertEqual(len(self._run(cli)), 1)
        self.assertEqual(cli.calls, 2)

    def test_a_never_answering_service_is_let_through_after_the_tries(self):
        """兜底照搬 bot：建任务那一步服务端会复检，报错还更具体。
        在这里卡死反而会让一次正常的迁移提不上去。"""
        cli = FakeClient(*[""] * 20)
        self.assertEqual(len(self._run(cli, tries=4)), 4)
        self.assertEqual(cli.calls, 4)


class Detail:
    """`m.AddressDetail()`：SDK 那个只有属性的空壳。"""


class FakeModels:
    AddressDetail = Detail

    def __getattr__(self, name):
        # CreateAddressRequest / CreateJobInfo… 一律记下收到的关键字
        return lambda **kw: type("R", (), {**kw, "_kw": kw})()


class FakeCli:
    def __init__(self, *, create_raises=None):
        self.addresses = {}
        self.jobs = []
        self.launched = []
        self.create_raises = create_raises

    def create_address(self, uid, req):
        info = req._kw["import_address"]
        if self.create_raises:
            raise self.create_raises
        self.addresses[info._kw["name"]] = info._kw["address_detail"]

    def verify_address(self, uid, name):
        return FakeResp("available")

    def create_job(self, uid, req):
        self.jobs.append(req._kw["import_job"]._kw)

    def update_job(self, uid, name, req):
        self.launched.append(name)
        if self.jobs:
            self.jobs[-1]["status"] = req._kw["import_job"]._kw["status"]


OSS_SRC = {"scheme": "oss", "bucket": "src-b", "prefix": "a/", "region": "cn-shenzhen"}
OSS_DST = {"scheme": "oss", "bucket": "dst-b", "prefix": "b/", "region": "cn-shenzhen"}


def _patch(test, cli):
    test.addCleanup(setattr, mgw, "client", mgw.client)
    test.addCleanup(setattr, mgw, "_models", mgw._models)
    mgw.client = lambda **kw: cli
    mgw._models = lambda: FakeModels()
    return cli


class SubmitTests(unittest.TestCase):
    def _run(self, *, region="cn-hangzhou", src=None, dest=None, **kw):
        cli = _patch(self, FakeCli())
        mgw.submit(
            user_id="u",
            endpoint="e",
            region=region,
            creds=None,
            job_name="panel-1",
            src=src or dict(OSS_SRC),
            dest=dest or dict(OSS_DST),
            oss_role="role",
            **kw,
        )
        return cli

    def test_the_internal_endpoint_is_chosen_against_where_the_service_runs(self):
        """**不是源和目的互比。** 互比的话「深圳→深圳、服务在北京」会拼出
        `oss-cn-shenzhen-internal.aliyuncs.com` —— 从北京的迁移服务根本连不上。
        面板的桶横跨杭州/深圳/北京/新加坡/曼谷，这是常态不是边角。"""
        cli = self._run(region="cn-hangzhou")
        for name in ("panel-1-src", "panel-1-dst"):
            self.assertNotIn("-internal", cli.addresses[name].domain, name)

    def test_the_internal_endpoint_is_used_when_the_bucket_sits_where_the_service_does(self):
        cli = self._run(region="cn-shenzhen")
        self.assertIn("-internal", cli.addresses["panel-1-src"].domain)

    def test_a_bucket_without_a_region_is_refused_with_a_readable_error(self):
        """直接下标会抛 KeyError —— 那不是 DeliveryError，面板会渲染成 500。"""
        with self.assertRaises(DeliveryError) as caught:
            self._run(src={**OSS_SRC, "region": ""})
        self.assertIn("哪个地域", str(caught.exception))

    def test_the_two_mode_fields_are_not_swapped(self):
        """写反了照样能提交，云上收到的是 `transfer_mode="always"`。"""
        cli = self._run(same_name="skip")
        self.assertEqual(cli.jobs[0]["transfer_mode"], "lastmodified")
        self.assertEqual(cli.jobs[0]["overwrite_mode"], "always")

    def test_the_job_is_actually_launched(self):
        """建完不启动的话任务就躺在那儿，而轮询会一直显示「还没开始」。"""
        self.assertEqual(self._run().jobs[0]["status"], mgw.STATUS_LAUNCHING)

    def test_a_third_party_source_needs_an_endpoint_from_the_registry(self):
        """第三方的域名推不出来，必须由登记表给。推不出来就不能往下走。"""
        with self.assertRaises(DeliveryError):
            self._run(
                src={"scheme": "s3compat", "bucket": "b", "prefix": "", "region": ""},
                src_key="k",
                src_secret="s",
            )

    def test_a_third_party_source_uses_the_s3_compatible_type(self):
        cli = self._run(
            src={"scheme": "s3compat", "bucket": "b", "prefix": "", "region": ""},
            src_key="k",
            src_secret="s",
            src_domain="minio.example.com",
        )
        got = cli.addresses["panel-1-src"]
        self.assertEqual(got.address_type, "s3compat")
        self.assertEqual(got.domain, "minio.example.com")


class RepeatedJobTests(unittest.TestCase):
    """错误码字面是 `ImportJobRepeatedOnSameAddress` —— 可能按「地址对」判重而不是按名字。
    重试换了名字却指向同一对桶时，撞的是旧名字那个任务；返回新名字的话云上根本没有它，
    之后每次查进度都 404，而 404 不算失败 —— 单子永远在途，连「卡住」都不报。"""

    class Cli(FakeCli):
        def __init__(self, *, job_exists):
            super().__init__()
            self.job_exists = job_exists

        def create_job(self, uid, req):
            raise RuntimeError("ImportJobRepeatedOnSameAddress")

        def get_job(self, uid, name, req):
            if not self.job_exists:
                raise RuntimeError("NoSuchImportJob")
            return object()

    def _submit(self, *, job_exists):
        self.cli = _patch(self, self.Cli(job_exists=job_exists))
        return mgw.submit(
            user_id="u",
            endpoint="e",
            region="cn-shenzhen",
            creds=None,
            job_name="panel-1",
            src=dict(OSS_SRC),
            dest=dict(OSS_DST),
            oss_role="role",
        )

    def test_a_job_that_really_is_ours_is_reused(self):
        self.assertEqual(self._submit(job_exists=True), "panel-1")

    def test_the_reused_job_is_still_launched(self):
        """**复用已有任务时也必须启动一次。**

        原来建任务和启动挤在同一个 try 里：`update_job` 抖一次，下一轮重提交撞
        `ImportJobRepeated` → 探针查到任务在 → 直接返回，启动再也不会被调用。
        任务躺在云上永不开始，而轮询只显示「还没开始」—— 不报错、不失败、没人知道。"""
        self._submit(job_exists=True)
        self.assertEqual(self.cli.launched, ["panel-1"])

    def test_a_collision_with_someone_elses_job_is_reported_not_papered_over(self):
        with self.assertRaises(DeliveryError) as caught:
            self._submit(job_exists=False)
        self.assertIn("控制台", str(caught.exception))


class PutAddressTests(unittest.TestCase):
    def test_only_a_real_already_exists_is_swallowed(self):
        """`Exist` 这个子串同时命中 `EntityNotExist` / `AddressNotExist` ——
        把「不存在」当成「已存在」吞掉，地址没建成却照样往下走。"""
        cli = _patch(self, FakeCli(create_raises=RuntimeError("AddressNotExist")))
        with self.assertRaises(DeliveryError):
            mgw._put_address(cli, "u", "n", Detail())

    def test_an_address_that_is_already_there_is_fine(self):
        cli = _patch(self, FakeCli(create_raises=RuntimeError("AlreadyExist")))
        mgw._put_address(cli, "u", "n", Detail())

    def test_the_raw_sdk_exception_does_not_escape(self):
        """其余出口都包了 MgwError；这里漏出去的话用户只看到「请联系管理员」。"""
        cli = _patch(self, FakeCli(create_raises=RuntimeError("连接重置")))
        with self.assertRaises(DeliveryError):
            mgw._put_address(cli, "u", "n", Detail())


class EstimateTests(unittest.TestCase):
    """量体积。**断言的是发出去的 URL**，不是返回值 —— 域名拼错时这个函数
    照样返回一个合法的 `(0,0,False)`，看起来只是「量不出来」，
    而那会让每一张单都停在「等确认」，体积门变成橡皮图章。"""

    def _urls(self, region):
        seen = []

        def transport(url, method, headers, body):
            seen.append(url)
            return 200, (
                b'<?xml version="1.0"?><ListBucketResult '
                b'xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com">'
                b"<IsTruncated>false</IsTruncated></ListBucketResult>"
            )

        mgw.estimate("src-b", "a/", region=region, creds=_CREDS, transport=transport)
        return seen

    def test_a_bare_region_gets_the_oss_prefix(self):
        """模板里存的是裸地域（catalog 明确拒绝带前缀的写法），
        而 `oss.call` 拼主机名要的是 `oss-cn-shenzhen`。"""
        self.assertIn("src-b.oss-cn-shenzhen.aliyuncs.com", self._urls("cn-shenzhen")[0])

    def test_a_region_that_already_has_the_prefix_is_not_doubled(self):
        """拼成 `oss-oss-cn-shenzhen` 的错在别处踩过。"""
        self.assertIn("src-b.oss-cn-shenzhen.aliyuncs.com", self._urls("oss-cn-shenzhen")[0])

    def test_a_failure_is_reported_as_unmeasurable_not_as_zero(self):
        """返回 (0,0) 的话审批门恒为 False —— 100TB 被当 0 字节直接放行。"""

        def boom(*_a, **_k):
            raise RuntimeError("连不上")

        self.assertEqual(
            mgw.estimate("b", "p/", region="cn-hangzhou", creds=_CREDS, transport=boom),
            (0, 0, False),
        )


class _Creds:
    access_key_id = "AK"
    access_key_secret = "SK"
    security_token = ""


_CREDS = _Creds()


class ModeTests(unittest.TestCase):
    def test_skip_is_incremental_because_the_documented_value_is_rejected(self):
        """文档说 `overwrite_mode=never` 能不覆盖，真机拒收这个值。"""
        self.assertEqual(mgw._MODES["skip"], ("lastmodified", "always"))
        self.assertNotIn("never", [v for pair in mgw._MODES.values() for v in pair])


if __name__ == "__main__":
    unittest.main()


class LaunchTests(unittest.TestCase):
    """建任务和启动是两步。**分开的理由是一个能永久卡住单子的漏。**"""

    class Cli(FakeCli):
        def __init__(self, *, update_raises):
            super().__init__()
            self.update_raises = update_raises

        def update_job(self, uid, name, req):
            self.launched.append(name)
            raise self.update_raises

    def _submit(self, exc):
        _patch(self, self.Cli(update_raises=exc))
        return mgw.submit(
            user_id="u",
            endpoint="e",
            region="cn-shenzhen",
            creds=None,
            job_name="panel-1",
            src=dict(OSS_SRC),
            dest=dict(OSS_DST),
            oss_role="role",
        )

    def test_a_failed_launch_is_reported_not_swallowed(self):
        """吞掉的话调用方会以为提交成功，单子转到「在途」，
        而云上那个任务从来没开始跑 —— 轮询永远显示「还没开始」。"""
        with self.assertRaises(DeliveryError) as caught:
            self._submit(RuntimeError("连接重置"))
        self.assertIn("没能启动", str(caught.exception))

    def test_an_already_launched_job_is_not_an_error(self):
        """上一次调用其实成功了、只是响应没回来。再启动一次云上会拿状态串顶回来，
        那不是问题 —— 报错的话这张单会永远停在「没提交成」。"""
        self.assertEqual(self._submit(RuntimeError("status IMPORT_JOB_RUNNING")), "panel-1")
