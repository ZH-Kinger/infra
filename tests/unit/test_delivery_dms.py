"""火山迁移（DMS）里不碰网络的那部分。

三个重点，都是「搬过来的时候会错、而且错了不报错」的地方：

  1. **目的端带前缀必须拒掉。** 火山只认桶级，对象保持源 key 原样落进桶里。
     忽略掉的话，申请人以为数据在自己填的那个目录，实际散在桶根下。
  2. **没有幂等。** 阿里按任务名幂等，火山建一个返回一个新 task_id ——
     同一张单提两次就是两个任务同时搬同一批数据。
  3. **失败终态是 `Failure` 不是 `Failed`。** 拼错的话失败的任务会一直显示
     「进行中」，直到有人手动去控制台看。
"""

import unittest

from delivery.clouds import dms
from delivery.errors import DeliveryError


class FakeReq:
    """SDK 那堆 `XxxForCreateDataMigrateTaskInput`，只记下收到了什么。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeSdk:
    """够用的 `volcenginesdkdms` 替身。"""

    def __getattr__(self, name):
        return FakeReq


class FakeApi:
    def __init__(self, *, tasks=(), task_id=9001, query=None):
        self.tasks = list(tasks)
        self.task_id = task_id
        self.query = query
        self.created = []
        self.listed = []

    def list_data_migrate_task(self, req):
        self.listed.append((req.limit, req.offset))
        page = self.tasks[req.offset : req.offset + req.limit]
        return FakeReq(task_list=[FakeReq(task_name=n, task_id=i) for i, n in page])

    def create_data_migrate_task(self, req):
        self.created.append(req)
        return FakeReq(task_id=self.task_id)

    def query_data_migrate_task(self, req):
        return self.query


def _patch(test, api):
    """把 `client()` 换成替身。真调用需要 SDK 和网络，这里两样都不要。

    **替身把收到的参数记在 `api.client_kw` 上。** 不记的话，「用哪个地域建 client」
    这件事就零覆盖 —— 而那正是最容易写错、错了又最难发现的一处：
    建在源地域的话任务真的会建出来，但之后 `find_task` 永远找不到它，
    于是每提交一次就重复建一个。
    """
    sdk = FakeSdk()
    test.addCleanup(setattr, dms, "client", dms.client)

    def fake(**kw):
        api.client_kw = kw
        return api, sdk

    dms.client = fake
    return api


OSS_SRC = {"scheme": "oss", "bucket": "src-b", "prefix": "a/", "region": "cn-hangzhou"}
TOS_DST = {"scheme": "tos", "bucket": "dst-b", "prefix": "", "region": "cn-shanghai"}


class DestinationTests(unittest.TestCase):
    def test_a_destination_prefix_is_refused_rather_than_silently_dropped(self):
        """火山只认桶级。忽略掉的话申请人会去自己填的目录里找，而数据不在那儿。"""
        api = _patch(self, FakeApi())
        with self.assertRaises(DeliveryError) as caught:
            dms.submit(
                region="cn-shanghai",
                creds=CREDS,
                job_name="panel-1",
                src=OSS_SRC,
                dest={**TOS_DST, "prefix": "landing/"},
                src_key="k",
                src_secret="s",
            )
        self.assertIn("只能指定到桶", str(caught.exception))
        self.assertEqual(api.created, [])

    def test_a_non_tos_destination_is_refused(self):
        _patch(self, FakeApi())
        with self.assertRaises(DeliveryError):
            dms.submit(
                region="cn-shanghai",
                creds=CREDS,
                job_name="panel-1",
                src=OSS_SRC,
                dest={"scheme": "oss", "bucket": "b", "region": "cn-hangzhou"},
                src_key="k",
                src_secret="s",
            )


class IdempotencyTests(unittest.TestCase):
    def test_an_existing_task_with_the_same_name_is_reused(self):
        """火山不按任务名幂等。不先找一遍，重试同一张单就是两个任务同时搬。"""
        api = _patch(self, FakeApi(tasks=[(555, "panel-req-1")]))
        got = dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-req-1",
            src=OSS_SRC,
            dest=TOS_DST,
            src_key="k",
            src_secret="s",
        )
        self.assertEqual(got, "555")
        self.assertEqual(api.created, [])

    def test_a_new_name_creates_a_task(self):
        api = _patch(self, FakeApi(tasks=[(555, "panel-other")], task_id=777))
        got = dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-req-1",
            src=OSS_SRC,
            dest=TOS_DST,
            src_key="k",
            src_secret="s",
        )
        self.assertEqual(got, "777")
        self.assertEqual(len(api.created), 1)

    def test_the_search_pages_through_instead_of_reading_one_page(self):
        rows = [(i, f"task-{i}") for i in range(dms._PAGE + 5)]
        rows[-1] = (999, "panel-wanted")
        api = _patch(self, FakeApi(tasks=rows))
        self.assertEqual(dms.find_task(api, FakeSdk(), "panel-wanted"), 999)
        self.assertGreater(len(api.listed), 1)

    def test_existing_refuses_to_answer_when_it_cannot_see_everything(self):
        """跨云那条在签钥匙**之前**问「云上有没有」。翻不完就说「没有」的话，
        mover 会去重签，而重签会撤掉上一把 —— 如果那个任务其实在第 2001 条之后，
        它就拿着一把死钥匙跑、全部 403（审计 R1）。这一问答不上来就必须停下。"""
        full = [(i, f"task-{i}") for i in range(dms._PAGE * dms._MAX_PAGES)]
        _patch(self, FakeApi(tasks=full))
        with self.assertRaises(dms.TooManyTasks):
            dms.existing(region="cn-shanghai", creds=CREDS, job_name="panel-not-in-view")

    def test_existing_still_says_no_when_it_really_saw_everything(self):
        _patch(self, FakeApi(tasks=[(1, "task-1")]))
        self.assertIsNone(dms.existing(region="cn-shanghai", creds=CREDS, job_name="panel-x"))

    def test_submit_keeps_the_lenient_search(self):
        """`submit` 自己那次查找还是「翻不完就当没有、建一个」—— 反过来的话
        账号里任务一多，所有同云迁移都提不上去。严格只用在签钥匙之前那一问。"""
        full = [(i, f"task-{i}") for i in range(dms._PAGE * dms._MAX_PAGES)]
        api = _patch(self, FakeApi(tasks=full, task_id=4242))
        got = dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-new",
            src=OSS_SRC,
            dest=TOS_DST,
            src_key="k",
            src_secret="s",
        )
        self.assertEqual(got, "4242")
        self.assertEqual(len(api.created), 1)

    def test_a_missing_task_id_is_an_error_not_a_success(self):
        """拿不到 id 就等于这个任务从此没人管：查不了进度，也不知道它在不在搬。"""
        _patch(self, FakeApi(task_id=None))
        with self.assertRaises(DeliveryError):
            dms.submit(
                region="cn-shanghai",
                creds=CREDS,
                job_name="panel-1",
                src=OSS_SRC,
                dest=TOS_DST,
                src_key="k",
                src_secret="s",
            )


class RegionTests(unittest.TestCase):
    """任务归属哪个地域，由**目的 TOS** 决定。真机实测：在 cn-beijing 列任务返回空，
    同一批任务在 cn-shanghai 全在。建错地域 = 每次提交都重复建一个。"""

    def test_the_client_is_built_for_the_destination_region_not_the_source(self):
        api = _patch(self, FakeApi())
        dms.submit(
            region="cn-beijing",
            creds=CREDS,
            job_name="panel-1",
            src={**OSS_SRC, "region": "cn-hangzhou"},
            dest={**TOS_DST, "region": "cn-shanghai"},
            src_key="k",
            src_secret="s",
        )
        self.assertEqual(api.client_kw["region"], "cn-shanghai")

    def test_the_fallback_region_is_used_when_the_destination_has_none(self):
        api = _patch(self, FakeApi())
        dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-1",
            src=OSS_SRC,
            dest={"scheme": "tos", "bucket": "b", "prefix": ""},
            src_key="k",
            src_secret="s",
        )
        self.assertEqual(api.client_kw["region"], "cn-shanghai")


class ClientTests(unittest.TestCase):
    """`client()` 自己的守卫。上面那些用例把它整个换掉了，所以这里单独测。"""

    def _sdk(self):
        made = {}

        class Cfg:
            def __init__(self):
                self.ak = self.sk = self.region = ""

        class Core:
            Configuration = Cfg
            ApiClient = staticmethod(lambda cfg: made.setdefault("cfg", cfg))

        class Sdk:
            DMSApi = staticmethod(lambda c: "api")

        self.addCleanup(setattr, dms, "_sdk", dms._sdk)
        dms._sdk = lambda: (Core, Sdk)
        return made

    def test_the_region_reaches_the_sdk_configuration(self):
        made = self._sdk()
        dms.client(region="cn-shanghai", creds=CREDS)
        self.assertEqual(made["cfg"].region, "cn-shanghai")
        self.assertEqual((made["cfg"].ak, made["cfg"].sk), ("AK", "SK"))

    def test_both_spellings_of_the_secret_are_accepted(self):
        """面板的火山凭证叫 `secret_access_key`，阿里那套叫 `access_key_secret`。
        只认一个的话，另一朵云的凭证进来会被当成「没配 AK/SK」。"""
        made = self._sdk()
        other = type("C", (), {"access_key_id": "AK", "access_key_secret": "SK2"})()
        dms.client(region="cn-shanghai", creds=other)
        self.assertEqual(made["cfg"].sk, "SK2")

    def test_missing_credentials_are_refused(self):
        self._sdk()
        for bad in (
            type("C", (), {"access_key_id": "", "secret_access_key": "SK"})(),
            type("C", (), {"access_key_id": "AK", "secret_access_key": ""})(),
        ):
            with self.assertRaises(DeliveryError):
                dms.client(region="cn-shanghai", creds=bad)

    def test_a_missing_region_is_refused_rather_than_defaulted(self):
        """回落到某个默认地域的话，任务会建在一个没人预期的地方。"""
        self._sdk()
        with self.assertRaises(DeliveryError):
            dms.client(region="", creds=CREDS)


class SourceTests(unittest.TestCase):
    def _built(self, src, **kw):
        api = _patch(self, FakeApi())
        dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-1",
            src=src,
            dest=TOS_DST,
            **{"src_key": "k", "src_secret": "s", **kw},
        )
        return api.created[0].source.object_source_config.bucket_access_config

    def test_an_oss_source_carries_the_oss_prefixed_region(self):
        """真机配置就是 `oss-cn-hangzhou`。这是两朵云唯一写法不同的字段。"""
        got = self._built(OSS_SRC)
        self.assertEqual(got.vendor, dms.VENDOR_OSS)
        self.assertEqual(got.region, "oss-cn-hangzhou")
        self.assertEqual(got.endpoint, "https://oss-cn-hangzhou.aliyuncs.com")

    def test_an_already_prefixed_region_is_not_prefixed_twice(self):
        """拼成 `oss-oss-cn-hangzhou` 的错在别处踩过。"""
        self.assertEqual(
            self._built({**OSS_SRC, "region": "oss-cn-hangzhou"}).region, "oss-cn-hangzhou"
        )

    def test_a_tos_source_keeps_the_bare_region(self):
        got = self._built({"scheme": "tos", "bucket": "b", "prefix": "p/", "region": "cn-shanghai"})
        self.assertEqual(got.vendor, dms.VENDOR_TOS)
        self.assertEqual(got.region, "cn-shanghai")

    def test_an_unknown_source_is_refused_not_guessed(self):
        """掉进某个分支的后果是拿错 vendor 去读桶，表现是「搬了 0 个对象，成功」。"""
        for bad in ("s3compat", "cpfs", ""):
            with self.assertRaises(DeliveryError, msg=bad):
                self._built({"scheme": bad, "bucket": "b", "prefix": "", "region": "r"})

    def test_a_source_without_credentials_is_refused(self):
        with self.assertRaises(DeliveryError):
            self._built(OSS_SRC, src_key="", src_secret="")

    def test_only_the_named_prefix_is_migrated_not_everything_else(self):
        """`is_excluded` 写反就是把整个桶搬过来，只漏掉申请人真正想要的那个目录。"""
        api = _patch(self, FakeApi())
        dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-1",
            src=OSS_SRC,
            dest=TOS_DST,
            src_key="k",
            src_secret="s",
        )
        cfg = api.created[0].source.object_source_config
        self.assertEqual(cfg.prefix_list, ["a/"])
        self.assertFalse(cfg.is_excluded)


class OverwriteTests(unittest.TestCase):
    def test_skip_sends_the_string_none_not_python_none(self):
        """写成 Python 的 None 会被 SDK 当成「没传」，落到没人验过的服务端默认值上。"""
        api = _patch(self, FakeApi())
        dms.submit(
            region="cn-shanghai",
            creds=CREDS,
            job_name="panel-1",
            src=OSS_SRC,
            dest=TOS_DST,
            src_key="k",
            src_secret="s",
            same_name="skip",
        )
        self.assertEqual(api.created[0].basic_config.overwrite_policy, "None")

    def test_an_unknown_policy_is_refused(self):
        _patch(self, FakeApi())
        with self.assertRaises(DeliveryError):
            dms.submit(
                region="cn-shanghai",
                creds=CREDS,
                job_name="panel-1",
                src=OSS_SRC,
                dest=TOS_DST,
                src_key="k",
                src_secret="s",
                same_name="merge",
            )


class PollTests(unittest.TestCase):
    def _poll(self, resp):
        _patch(self, FakeApi(query=resp))
        return dms.poll(region="cn-shanghai", creds=CREDS, job_name="555")

    def test_success_is_done(self):
        got = self._poll(
            FakeReq(
                task_status="Success",
                task_progress=FakeReq(transferred_bytes=9, transferred_objects=2),
            )
        )
        self.assertTrue(got["done"])
        self.assertFalse(got["failed"])
        self.assertEqual((got["bytes"], got["objects"]), (9, 2))

    def test_the_failed_state_is_spelled_failure(self):
        """拼成 `Failed` 的话失败的任务会一直显示「进行中」。"""
        self.assertTrue(self._poll(FakeReq(task_status="Failure", task_progress=None))["failed"])
        self.assertTrue(self._poll(FakeReq(task_status="Stopped", task_progress=None))["failed"])

    def test_an_in_flight_state_is_neither(self):
        for state in ("Transferring", "ResultGenerating", "Listing"):
            got = self._poll(FakeReq(task_status=state, task_progress=None))
            self.assertFalse(got["done"] or got["failed"], state)

    def test_a_failure_carries_a_reason_that_says_what_to_do(self):
        """DMS 失败时 task_status 只有一个 `Failure`。不拼这条，卡上就只有「失败」两个字。"""
        got = self._poll(
            FakeReq(
                task_status="Failure",
                task_progress=FakeReq(
                    transferred_bytes=0,
                    transferred_objects=0,
                    failed_objects=3,
                    not_exist_object_count=1,
                ),
            )
        )
        self.assertIn("3 个对象迁移失败", got["error"])
        self.assertIn("1 个对象源端已不存在", got["error"])

    def test_a_polling_error_is_not_a_task_failure(self):
        """轮询是网络抖动的高发点。空状态让调用方继续等。"""
        api = _patch(self, FakeApi())
        api.query_data_migrate_task = _boom
        got = dms.poll(region="cn-shanghai", creds=CREDS, job_name="555")
        self.assertFalse(got["done"] or got["failed"])
        self.assertIn("查进度失败", got["error"])

    def test_a_non_numeric_task_id_does_not_look_like_a_failure_either(self):
        _patch(self, FakeApi())
        got = dms.poll(region="cn-shanghai", creds=CREDS, job_name="panel-req-1")
        self.assertFalse(got["done"] or got["failed"])


class ReasonTests(unittest.TestCase):
    """火山 SDK 异常的 str() 是「状态码 + 一两千字符的 HTTP 响应头 + body」。
    直接截前 300 字，截出来全是 `Server: Tengine` —— 真正的原因一个字都看不到。"""

    HEADERS = (
        "(400)\nReason: Bad Request\nHTTP response headers: "
        + "HTTPHeaderDict({'Server': 'Tengine', 'x-tt-trace-id': '"
        + "0" * 400
        + "'})"
    )

    def test_the_business_error_is_pulled_out_of_the_body(self):
        exc = RuntimeError(self.HEADERS)
        exc.body = (
            b'{"ResponseMetadata":{"Error":{"Code":"CheckTaskPermission",'
            b'"Message":"Need HeadBucket permission on bucket wuji-bucket-hangzhou."}}}'
        )
        got = dms._reason(exc)
        self.assertIn("CheckTaskPermission", got)
        self.assertIn("HeadBucket", got)
        self.assertNotIn("Tengine", got)

    def test_a_body_that_is_not_the_expected_shape_falls_back_to_the_raw_text(self):
        """挖不出来也得给点东西 —— 空字符串等于失败卡上只有「失败」两个字。"""
        for body in (b"not json at all", b'{"Result":null}', None):
            exc = RuntimeError("炸了")
            exc.body = body
            self.assertTrue(dms._reason(exc))


class EstimateTests(unittest.TestCase):
    def test_a_tos_source_is_reported_as_unmeasurable_not_as_zero(self):
        """面板没有 TOS 的列举实现。返回 0 会让 100TB 被当成 0 字节直接放行。"""
        self.assertEqual(
            dms.estimate("b", "p/", scheme="tos", region="r", creds=CREDS), (0, 0, False)
        )


class CredsStub:
    access_key_id = "AK"
    secret_access_key = "SK"


CREDS = CredsStub()


def _boom(*_a, **_k):
    raise RuntimeError("连不上")


if __name__ == "__main__":
    unittest.main()
