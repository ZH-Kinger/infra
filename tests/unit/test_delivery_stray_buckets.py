"""体检新类别「桶在云上，但没登记在任何地方」，以及它依赖的 OSS 服务级请求。

三件事改坏了都不会有任何报错，所以都得钉住：

1. **服务级签名**。ListBuckets 的 CanonicalizedResource 是光秃秃的 `/`、host 不带桶名。
   拼成 `.oss-cn-hangzhou.aliyuncs.com` 或者 `//` 换来的只是一句 SignatureDoesNotMatch，
   它不会告诉你差在哪。更要紧的是为了它动了 `call()` 里拼 host 和拼 resource 那两行——
   **桶级请求的签名一个字节都不能变**，否则列对象、搬目录、放占位目录会一起挂，
   而那几条路径平时跑得好好的，没人会想到是「加了个列桶功能」弄坏的。
2. **「没采到」不能变成「没问题」**。桶清单采不到、白名单读不到，这一类必须标成跳过。
   写成空清单的话，`oss:ListBuckets` 哪天掉了，体检会报「没有没登记的桶」——
   而那恰恰是它最该报警的时候。这是本仓库最核心的一条不变式（见 hygiene.py 开头）。
3. **空集合 ≠ None**。白名单里确实一个桶都没配（空集合）→ 云上每个桶都算没登记，对；
   白名单读不到（None）→ 跳过。两者行为必须不同，混成一个就是上面第 2 条翻车。

离线，数据虚构。真机形状参考：云上 27 个桶、白名单登记 15 个、
滤掉云产品自建（`cri-` / `oss-pai-`）之后剩 4 个 —— 那四个才是要人去认领的。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from delivery import assets, hygiene
from delivery.clouds import aliyun, oss

REGION = "oss-cn-hangzhou"
CREDS = aliyun.Credentials("AK", "SK")
NOW = 1_800_000_000.0
NS = 'xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com"'


def sign(to_sign: str, secret: str = "SK") -> str:  # noqa: S107
    """按 OSS V1 自己算一遍签名。**不是照抄实现**：逐字写出期望的 string-to-sign，
    实现改了拼法这里就红 —— 只断言「两次调用签名不同」是抓不到拼错的。"""
    mac = hmac.new(secret.encode(), to_sign.encode(), hashlib.sha1)  # noqa: S324
    return base64.b64encode(mac.digest()).decode()


def page(names, *, truncated=False, next_marker="", ns: str = NS) -> bytes:
    """一页 ListBuckets 响应。**带 xmlns**：真机返回的就是带命名空间的，
    不带的话 `_ns()` 那条分支在测试里永远走不到，而线上永远走它。"""
    items = "".join(
        f"<Bucket><Name>{n}</Name><Location>{loc}</Location>"
        f"<CreationDate>{created}</CreationDate></Bucket>"
        for n, loc, created in names
    )
    tail = f"<NextMarker>{next_marker}</NextMarker>" if next_marker else ""
    return (
        f"<?xml version='1.0'?><ListAllMyBucketsResult {ns}>"
        f"<Buckets>{items}</Buckets>"
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{tail}"
        "</ListAllMyBucketsResult>"
    ).encode()


def bucket(name, region="oss-cn-hangzhou", created="2023-04-05T06:07:08.000Z") -> dict:
    return {"name": name, "region": region, "created": created}


class OssServiceRequestSigningTests(unittest.TestCase):
    """服务级（ListBuckets）这条新路径的签名与 URL，外加桶级那条的回归锁。"""

    def capture(self, bucket_name="", key="", *, method="GET", creds=CREDS, **kw):
        """`service=True` 走服务级那条（`bucket_name` 必须留空）。"""
        seen: dict = {}

        def transport(url, m, headers, body):
            seen.update(url=url, method=m, headers=dict(headers), body=body)
            return 200, page([])

        oss.call(method, bucket_name, key, region=REGION, creds=creds, transport=transport, **kw)
        return seen

    def test_a_service_level_request_signs_a_bare_slash(self):
        """CanonicalizedResource 必须是 `/`。写成 `//`、`/ /`、`/?max-keys=1000` 都只会
        换来 SignatureDoesNotMatch —— 而那个报错不会说是资源串拼错了。"""
        seen = self.capture(service=True, query={"max-keys": "1000"})
        want = f"GET\n\n\n{seen['headers']['Date']}\n/"
        self.assertEqual(seen["headers"]["Authorization"], f"OSS AK:{sign(want)}")

    def test_the_service_level_host_carries_no_bucket_and_no_stray_dot(self):
        """host 是 `oss-cn-hangzhou.aliyuncs.com`，**不是** `.oss-cn-hangzhou...`。
        前导点解析得出去（DNS 会当成根域的写法），于是这条会以一个莫名其妙的
        连接错误收场，而不是一眼能看懂的签名错误。"""
        seen = self.capture(service=True, query={"max-keys": "1000"})
        self.assertEqual(seen["headers"]["Host"], "oss-cn-hangzhou.aliyuncs.com")
        parts = urlsplit(seen["url"])
        self.assertEqual(parts.netloc, "oss-cn-hangzhou.aliyuncs.com")
        self.assertFalse(parts.netloc.startswith("."))
        self.assertEqual(parts.path, "/")
        self.assertNotIn("//", seen["url"][len("https://") :])

    def test_a_bucket_request_signs_exactly_what_it_signed_before(self):
        """**这次改动前后必须逐字一致**。`call()` 里拼 resource 那行被动过，
        改坏了的表现是「列桶好了、列对象全挂」，而列对象是这个模块的主业。"""
        seen = self.capture("bk", "a/b.txt")
        want = f"GET\n\n\n{seen['headers']['Date']}\n/bk/a/b.txt"
        self.assertEqual(seen["headers"]["Authorization"], f"OSS AK:{sign(want)}")
        self.assertEqual(seen["headers"]["Host"], "bk.oss-cn-hangzhou.aliyuncs.com")
        self.assertEqual(urlsplit(seen["url"]).netloc, "bk.oss-cn-hangzhou.aliyuncs.com")

    def test_a_bucket_request_with_no_key_still_ends_in_a_slash(self):
        """列对象走的就是这条（key 为空）：资源串是 `/bk/`，**不是 `/bk`、也不是 `/`**。
        塌成 `/` 的话签的是「整个服务」，桶级请求会被拒。"""
        seen = self.capture("bk", query={"list-type": "2", "prefix": "x/"})
        want = f"GET\n\n\n{seen['headers']['Date']}\n/bk/"
        self.assertEqual(seen["headers"]["Authorization"], f"OSS AK:{sign(want)}")

    def test_a_subresource_still_lands_after_the_bucket_path(self):
        """子资源接在资源串后面，列举参数不进签名。两者混一起是最常见的踩法。"""
        seen = self.capture("bk", query={"continuation-token": "t2", "prefix": "x/"})
        want = f"GET\n\n\n{seen['headers']['Date']}\n/bk/?continuation-token=t2"
        self.assertEqual(seen["headers"]["Authorization"], f"OSS AK:{sign(want)}")

    def test_the_list_buckets_paging_params_are_not_subresources(self):
        """`marker` / `max-keys` 是列举参数，**不能**进签名串。
        把 marker 当子资源签的话：第一页正常、第二页 403 —— 和当初 continuation-token
        漏签是同一个坑，只是方向反过来。"""
        for query in ({}, {"max-keys": "1000"}, {"marker": "bkt-9", "max-keys": "1000"}):
            seen = self.capture(service=True, query=query)
            want = f"GET\n\n\n{seen['headers']['Date']}\n/"
            self.assertEqual(
                seen["headers"]["Authorization"], f"OSS AK:{sign(want)}", msg=str(query)
            )

    def test_a_security_token_is_signed_on_the_service_path_too(self):
        """服务级这条也可能拿临时凭证跑。`x-oss-` 头进签名串、排在资源串之前，
        顺序反了或者漏了，所有 STS 凭证在这条路径上都会失败。"""
        seen = self.capture(
            service=True, creds=aliyun.Credentials("AK", "SK", security_token="tok")
        )
        want = f"GET\n\n\n{seen['headers']['Date']}\nx-oss-security-token:tok\n/"
        self.assertEqual(seen["headers"]["Authorization"], f"OSS AK:{sign(want)}")
        self.assertEqual(seen["headers"]["x-oss-security-token"], "tok")


class OssServiceFlagTests(unittest.TestCase):
    """「这是不是服务级请求」必须是**显式说的**，不能从「桶名是不是空的」推断。

    推断过一版，漏得很难看：守卫写成「桶名空**且带 key** 才抛」，可
    `list_prefixes` / `list_objects` 的前缀走的是 query、`key` 恒为空 ——
    于是 `list_prefixes("", "wzh/")` 照样发出一个合法的 GET Service，
    拿回 `ListAllMyBucketsResult`，找不到 `CommonPrefixes`，**安静返回 `[]`**，
    被上层读成「这个桶里一个目录都没有」。那是判断「谁换了组、有没有多余目录」的依据，
    读错就会重复建目录。

    **每条都断言「一个请求都没发出去」**：这比抛不抛更要紧 —— 真发出去的话，
    服务端会认认真真回一份**别的东西**（全部桶的清单），而那份东西解析得动、
    不报错、只是答非所问。
    """

    def counting_transport(self):
        sent: list = []

        def transport(url, method, headers, body):
            sent.append(url)
            return 200, page([("a-bkt", "oss-cn-hangzhou", "")])

        return transport, sent

    def refuses(self, fn, *, want="桶名不能为空"):
        transport, sent = self.counting_transport()
        with self.assertRaises(oss.OssError) as caught:
            fn(transport)
        self.assertIn(want, str(caught.exception))
        self.assertEqual(sent, [], "拦住了就不该发出去")

    def test_listing_prefixes_without_a_bucket_is_refused(self):
        """LOW-6 说的正是这一条：前缀走 query、`key` 恒为空，上一版守卫盖不住。"""
        self.refuses(
            lambda t: oss.list_prefixes("", "wzh/", region=REGION, creds=CREDS, transport=t)
        )

    def test_listing_objects_without_a_bucket_is_refused(self):
        """对称的一条。对账靠它列源和目的 —— 空清单会被读成「两边都是空的，对上了」。"""
        self.refuses(
            lambda t: oss.list_objects("", "wzh/", region=REGION, creds=CREDS, transport=t)
        )

    def test_a_keyed_call_without_a_bucket_is_refused(self):
        self.refuses(
            lambda t: oss.call("GET", "", "a/b.txt", region=REGION, creds=CREDS, transport=t)
        )

    def test_putting_a_folder_without_a_bucket_is_refused(self):
        """写路径更要拦在发出去之前：空桶名拼出来的请求打到哪都不该赌。"""
        self.refuses(lambda t: oss.put_folder("", "wzh/", region=REGION, creds=CREDS, transport=t))

    def test_copying_without_a_bucket_is_refused(self):
        self.refuses(
            lambda t: oss.copy_object("", "src", "dst", region=REGION, creds=CREDS, transport=t)
        )

    def test_a_service_request_may_not_carry_a_bucket(self):
        """**反向**：`service=True` 不是「顺便也能列桶里的东西」。
        带了桶名却按服务级签名，签出来的资源串是 `/`，服务端回 SignatureDoesNotMatch ——
        排查的人会盯着凭证看半天，而问题在调用处。"""
        self.refuses(
            lambda t: oss.call("GET", "bk", region=REGION, creds=CREDS, transport=t, service=True),
            want="服务级请求不能带桶名或对象名",
        )

    def test_a_service_request_may_not_carry_a_key(self):
        self.refuses(
            lambda t: oss.call(
                "GET", "", "a/b.txt", region=REGION, creds=CREDS, transport=t, service=True
            ),
            want="服务级请求不能带桶名或对象名",
        )

    def test_list_buckets_is_the_one_call_that_goes_through(self):
        """守卫不能挡过头：`list_buckets` 是唯一的服务级调用，它必须照常走通，
        而且仍然打在 `/` 上（挡过头的话这次新功能整个不能用）。"""
        transport, sent = self.counting_transport()
        got = oss.list_buckets(region=REGION, creds=CREDS, transport=transport)
        self.assertEqual([b["name"] for b in got], ["a-bkt"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(urlsplit(sent[0]).path, "/")
        self.assertEqual(urlsplit(sent[0]).netloc, "oss-cn-hangzhou.aliyuncs.com")

    def test_a_normal_bucket_call_is_untouched(self):
        """守卫加在最前面，正常调用一个字节都不该变。"""
        transport, sent = self.counting_transport()
        oss.call("GET", "bk", "a/b.txt", region=REGION, creds=CREDS, transport=transport)
        self.assertEqual(urlsplit(sent[0]).netloc, "bk.oss-cn-hangzhou.aliyuncs.com")


class ListBucketsTests(unittest.TestCase):
    """翻页与失败。这里的判据只有一条：**宁可中断，也不能少列**。
    少列出来的那个桶正好就是没人管的那个 —— 它会安安静静地不出现在清单上。"""

    def calls(self, pages):
        """pages 按请求顺序取用，同时记下每次请求的 query。"""
        seen: list = []

        def transport(url, method, headers, body):
            seen.append(dict(parse_qsl(urlsplit(url).query)))
            return 200, pages[min(len(seen) - 1, len(pages) - 1)]

        return transport, seen

    def test_parses_name_region_and_created(self):
        """`Location` 才是桶所在地域；请求发到哪个 endpoint 与它无关 ——
        ListBuckets 是服务级的，随便哪个地域都返回全部桶。"""
        transport, _ = self.calls(
            [page([("wuji-sing", "oss-ap-southeast-1", "2024-01-02T03:04:05Z")])]
        )
        got = oss.list_buckets(region=REGION, creds=CREDS, transport=transport)
        self.assertEqual(
            got,
            [
                {
                    "name": "wuji-sing",
                    "region": "oss-ap-southeast-1",
                    "created": "2024-01-02T03:04:05Z",
                }
            ],
        )

    def test_merges_two_pages_and_carries_the_marker(self):
        """第二页要带上 `marker=<NextMarker>`。不带就会永远拿第一页 ——
        表现是「清单只有前 100 个桶」，而云上有 27 个的时候根本看不出来。"""
        transport, seen = self.calls(
            [
                page([("a-bkt", "oss-cn-hangzhou", "")], truncated=True, next_marker="a-bkt"),
                page([("b-bkt", "oss-cn-beijing", "")]),
            ]
        )
        got = oss.list_buckets(region=REGION, creds=CREDS, transport=transport)
        self.assertEqual([b["name"] for b in got], ["a-bkt", "b-bkt"])
        self.assertNotIn("marker", seen[0])
        self.assertEqual(seen[1]["marker"], "a-bkt")

    def test_truncated_without_a_marker_refuses_to_return_half_a_list(self):
        """说还有下一页却不给游标 = 只拿到一部分。**不能静默返回半份**：
        调用方会当成「云上就这些桶」，而漏掉的那些恰好永远不会被体检看到。"""
        transport, _ = self.calls([page([("a-bkt", "oss-cn-hangzhou", "")], truncated=True)])
        with self.assertRaises(oss.OssError) as caught:
            oss.list_buckets(region=REGION, creds=CREDS, transport=transport)
        self.assertIn("不完整", str(caught.exception))

    def test_an_endless_truncation_stops_at_the_page_cap(self):
        """对端一直说「还有下一页」时不能转到天荒地老 —— 桶清单是给体检用的，
        一次采集卡死会让整份资产快照都写不出来。"""
        count = {"n": 0}

        def transport(url, method, headers, body):
            count["n"] += 1
            return 200, page(
                [(f"b{count['n']}", "oss-cn-hangzhou", "")],
                truncated=True,
                next_marker=f"b{count['n']}",
            )

        with self.assertRaises(oss.OssError):
            oss.list_buckets(region=REGION, creds=CREDS, transport=transport)
        self.assertEqual(count["n"], oss._MAX_PAGES)

    def test_access_denied_is_an_error_not_an_empty_list(self):
        """少一个 `oss:ListBuckets` 必须炸出来。悄悄返回空清单的话，
        体检会说「一个没登记的桶都没有」，而实际情况是「一个桶都没看到」。"""

        def transport(url, method, headers, body):
            return 403, b"<Error><Code>AccessDenied</Code><Message>no</Message></Error>"

        with self.assertRaises(oss.OssDenied):
            oss.list_buckets(region=REGION, creds=CREDS, transport=transport)

    def test_a_response_with_a_dtd_is_refused(self):
        """同 list_prefixes：长度上限挡不住实体膨胀，真正管用的是拒 DTD。"""

        def transport(url, method, headers, body):
            return 200, b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "boom">]><r/>'

        with self.assertRaises(oss.OssError):
            oss.list_buckets(region=REGION, creds=CREDS, transport=transport)

    def test_a_nameless_bucket_is_dropped_not_recorded_as_blank(self):
        """没有 Name 的条目进了清单就会变成一个空名字的「没登记的桶」——
        一条没法处置、又不敢忽略的线索是最耗人的那种。"""
        raw = (
            f"<?xml version='1.0'?><ListAllMyBucketsResult {NS}>"
            "<Buckets><Bucket><Location>oss-cn-hangzhou</Location></Bucket>"
            "<Bucket><Name>real</Name></Bucket></Buckets>"
            "<IsTruncated>false</IsTruncated></ListAllMyBucketsResult>"
        ).encode()
        transport, _ = self.calls([raw])
        got = oss.list_buckets(region=REGION, creds=CREDS, transport=transport)
        self.assertEqual([b["name"] for b in got], ["real"])


class CollectBucketsTests(unittest.TestCase):
    """采集侧：抛得出去、并且「没采到」在快照里看得出来。"""

    def test_collect_buckets_raises_instead_of_returning_an_empty_list(self):
        """`collect_buckets` 绝不能把失败吞成空清单 —— 上层靠异常才知道要记 bucket_error。"""

        def transport(url, method, headers, body):
            return 403, b"<Error><Code>AccessDenied</Code><Message>no</Message></Error>"

        with self.assertRaises(oss.OssError):
            assets.collect_buckets(CREDS, transport=transport)

    def test_it_is_a_service_level_call(self):
        """采集走的是服务级 endpoint，`region` 只是拨号用的、不是过滤条件。"""
        seen: list = []

        def transport(url, method, headers, body):
            seen.append(url)
            return 200, page([("x", "oss-cn-beijing", "")])

        got = assets.collect_buckets(CREDS, region="oss-cn-shanghai", transport=transport)
        self.assertEqual(urlsplit(seen[0]).netloc, "oss-cn-shanghai.aliyuncs.com")
        self.assertEqual(got[0]["region"], "oss-cn-beijing")

    def test_not_collected_is_not_the_same_as_none_collected(self):
        """同 datasets 那条：**采不到就不写 `buckets` 这个键**，不是写空列表。
        写空列表的话，采集权限掉了会让体检那一栏变成一片干净 —— 最该报警的时候最安静。"""
        bare = assets.build_snapshot([])
        self.assertNotIn("buckets", bare)
        self.assertNotIn("bucket_error", bare)

        empty = assets.build_snapshot([], buckets=[])
        self.assertEqual(empty["buckets"], [])

        failed = assets.build_snapshot([], bucket_error="没权限 oss:ListBuckets")
        self.assertNotIn("buckets", failed)
        self.assertIn("没权限", failed["bucket_error"])

    def test_a_huge_error_is_truncated_and_survives_json(self):
        """错误正文会被原样写进快照文件，长度要收着 —— 快照是给人读的。"""
        data = assets.build_snapshot([], buckets=[bucket("wuji-sing")], bucket_error="x" * 900)
        self.assertEqual(len(data["bucket_error"]), 300)
        self.assertEqual(json.loads(json.dumps(data))["buckets"][0]["name"], "wuji-sing")


class RegisteredBucketsTests(unittest.TestCase):
    """已登记的桶名集合。三种桶形状 + 「读不到 = None」这条。"""

    def test_nothing_at_all_is_none_not_an_empty_set(self):
        """两个来源都没有 → None。返回空集合的话云上每个桶都会被报成「没登记」，
        一份全是噪音的清单第二天就没人看了。"""
        self.assertIsNone(hygiene.registered_buckets(None, None))
        self.assertIsNone(hygiene.registered_buckets())

    def test_sources_that_yield_no_names_are_none_too(self):
        """**抽不出名字也算「没读到」**（auditor 复现的那条）：合法但不带桶的模板
        文件，`catalog.load()` 返回的空目录和「文件缺失」在返回值上分不开。
        分不开时只能选不报 —— 这一栏的建议里写着「确认没用了再删」，多报的代价是
        让人拿着合法桶去考虑删不删。所以这里只剩一种「有结论」的形态：非空集合。"""
        self.assertIsNone(hygiene.registered_buckets(None, []))
        self.assertIsNone(hygiene.registered_buckets([], None))
        self.assertIsNone(hygiene.registered_buckets([{"buckets": []}], []))

    def test_a_catalog_template_carries_pairs(self):
        """`catalog.Template.buckets` 是 `((桶名, 地域), ...)` 二元组 —— 最容易写错成
        「直接当字符串用」，那样会把 `('wuji-sing', 'cn-hangzhou')` 整个塞进集合，
        于是没有一个桶匹配得上，27 个桶全变成「没登记」。"""
        from delivery import catalog

        tpl = catalog.Template(
            id="t",
            kind="credential",
            platform="aliyun",
            account="1",
            title="凭证",
            buckets=(("wuji-sing", "cn-hangzhou"), ("wuji-data-tran", "cn-hangzhou")),
        )
        self.assertEqual(hygiene.registered_buckets([tpl], None), {"wuji-sing", "wuji-data-tran"})

    def test_a_raw_json_template_carries_dicts(self):
        """原始 JSON（和 `Template.public()`）里是 `{"name":…, "region":…}`。
        面板那条路传的就是这种形状。"""
        raw = {"id": "t", "buckets": [{"name": "wuji-sing", "region": "cn-hangzhou"}]}
        self.assertEqual(hygiene.registered_buckets([raw], None), {"wuji-sing"})

    def test_the_whitelist_is_bare_strings(self):
        """`identity/dataset-buckets.json` 里是裸字符串。"""
        self.assertEqual(
            hygiene.registered_buckets(None, ["wuji-sing", "h2r-dlc-1"]),
            {"wuji-sing", "h2r-dlc-1"},
        )

    def test_names_are_lowercased_and_stripped(self):
        """桶名在控制台里可以写成 `WuJi-X`（OSS 自己不区分大小写地对待桶名）。
        不归一的话，白名单里明明有的桶会被报成「没登记」—— 而报的人和登记的人
        看到的是同一个名字，谁也看不出哪里不对。"""
        got = hygiene.registered_buckets(None, ["  WuJi-Sing  "])
        self.assertEqual(got, {"wuji-sing"})

    def test_two_sources_merge(self):
        raw = {"buckets": [{"name": "A-one"}]}
        self.assertEqual(hygiene.registered_buckets([raw], ["b-two"]), {"a-one", "b-two"})

    def test_junk_shapes_are_ignored_not_crashed_on(self):
        """模板文件是人写的。一条畸形记录不该让整次体检崩掉 —— 崩掉的代价是
        「体检这一页打不开」，而它本来只该少认一个桶。"""
        raw = {"buckets": [None, "", 5, (), ("",), {"name": None}, {"nope": "x"}, "ok-bkt"]}
        self.assertEqual(hygiene.registered_buckets([raw], None), {"ok-bkt"})


class StrayBucketTests(unittest.TestCase):
    """体检那一类本身。"""

    def report(self, **kw):
        return hygiene.build(None, [], now=NOW, **kw)

    def test_no_bucket_list_is_skipped_not_clean(self):
        """桶清单没采到 → 说一声，而不是报「没问题」。"""
        rep = self.report(buckets=None, registered={"wuji-sing"})
        self.assertEqual(rep.stray_bucket, [])
        self.assertTrue(any("没有 OSS 桶清单" in s for s in rep.skipped))

    def test_no_whitelist_is_skipped_not_clean(self):
        """白名单读不到 → 同样是「没法判断」。照算的话会把云上每个桶都报一遍，
        而那种清单没人看第二遍 —— 于是真有一个野桶时也没人看得见。"""
        rep = self.report(buckets=[bucket("wuji-sing")], registered=None)
        self.assertEqual(rep.stray_bucket, [])
        self.assertTrue(any("没法判断哪些桶没登记" in s for s in rep.skipped))

    def test_an_empty_whitelist_reports_everything_none_reports_nothing(self):
        """`build()` 这一层的契约：`registered` 是空集合就当「确实什么都没登记」、
        是 None 就跳过。**注意生产路径现在给不出空集合了**（`registered_buckets()`
        末尾 `or None`，见 RegisteredBucketsTests）—— 这条守的是 `build()` 自己的
        契约：哪天有人从别处构造一个空集合传进来，它得按「全都没登记」算，而不是
        悄悄退化成跳过。两个分支行为一样的话，`None` 那道门就是白设的。"""
        cloud = [bucket("wuji-sing"), bucket("wuji-data-tran")]
        empty = self.report(buckets=cloud, registered=set())
        none = self.report(buckets=cloud, registered=None)
        self.assertEqual([f.subject for f in empty.stray_bucket], ["wuji-data-tran", "wuji-sing"])
        self.assertEqual(none.stray_bucket, [])
        self.assertFalse(any("没法判断" in s for s in empty.skipped))

    def test_registered_buckets_are_not_reported(self):
        rep = self.report(buckets=[bucket("wuji-sing"), bucket("nobody")], registered={"wuji-sing"})
        self.assertEqual([f.subject for f in rep.stray_bucket], ["nobody"])

    def test_matching_ignores_case(self):
        """云上叫 `WuJi-Sing`、白名单里写的 `wuji-sing`，是同一个桶。"""
        rep = self.report(buckets=[bucket("WuJi-Sing")], registered={"wuji-sing"})
        self.assertEqual(rep.stray_bucket, [])

    def test_cloud_managed_buckets_are_filtered(self):
        """`cri-*`（容器镜像服务）和 `oss-pai-*`（PAI）是云产品自己建的，
        没人「申请」得到它们。真机上这类占了这一栏的三分之二。"""
        cloud = [bucket("cri-abc123-registry"), bucket("oss-pai-workspace-x"), bucket("real-one")]
        rep = self.report(buckets=cloud, registered=set())
        self.assertEqual([f.subject for f in rep.stray_bucket], ["real-one"])

    def test_the_prefix_is_cri_dash_not_cri(self):
        """**边界**：过滤的是 `cri-`（带横线）和 `oss-pai-`，不是 `cri` / `oss-pai`。
        少一个横线就会把 `criteo-data` 这种正常业务桶一起吃掉 —— 而被吃掉的桶
        不会出现在任何清单上，没人会发现它被漏检了。"""
        cloud = [bucket("criteo-data"), bucket("crius"), bucket("oss-paintings")]
        rep = self.report(buckets=cloud, registered=set())
        self.assertEqual(
            [f.subject for f in rep.stray_bucket], ["criteo-data", "crius", "oss-paintings"]
        )

    def test_findings_are_sorted_by_name(self):
        """清单每次跑的顺序要稳定，否则两次输出没法对比。"""
        cloud = [bucket(n) for n in ("z-bkt", "a-bkt", "m-bkt")]
        rep = self.report(buckets=cloud, registered=set())
        self.assertEqual([f.subject for f in rep.stray_bucket], ["a-bkt", "m-bkt", "z-bkt"])

    def test_a_finding_says_where_and_when(self):
        """地域和建桶日期是认领这个桶的唯一线索 —— 一个光秃秃的桶名没人认得出来。
        建桶时间只取到日，秒级时间戳在这一栏里没有任何用处，只会挤掉地域。"""
        rep = self.report(
            buckets=[bucket("wuji-sing", "oss-ap-southeast-1", "2024-07-31T10:00:00.000Z")],
            registered=set(),
        )
        (found,) = rep.stray_bucket
        self.assertEqual(found.kind, "stray_bucket")
        self.assertEqual(found.platform, "aliyun")
        self.assertEqual(found.subject, "wuji-sing")
        self.assertIn("oss-ap-southeast-1", found.why)
        self.assertIn("2024-07-31", found.why)
        self.assertNotIn("T10:00", found.why)

    def test_a_finding_with_nothing_to_say_says_that(self):
        """地域/建桶时间采不到时，`why` **不能是空串**：空串在命令行和网页上都渲染成
        一行只有桶名的条目，看的人会以为是显示 bug，而不是「这两项没采到」。"""
        rep = self.report(buckets=["nobody"], registered=set())
        self.assertTrue(rep.stray_bucket[0].why.strip())

    def test_a_bare_string_bucket_list_also_works(self):
        """快照是 JSON，来源不止一处。给一串桶名也得能算，别只认 dict。"""
        rep = self.report(buckets=["wuji-sing", "nobody"], registered={"wuji-sing"})
        self.assertEqual([f.subject for f in rep.stray_bucket], ["nobody"])

    def test_it_runs_even_without_a_permission_snapshot(self):
        """桶清单来自**资产**快照，和权限快照没关系。放在那道门后面的话，
        权限快照一缺，这一类会跟着一起消失 —— 而它本来算得出来。"""
        rep = hygiene.build(None, [], buckets=[bucket("nobody")], registered=set(), now=NOW)
        self.assertEqual([f.subject for f in rep.stray_bucket], ["nobody"])
        self.assertTrue(any("没有权限快照" in s for s in rep.skipped))

    def test_it_counts_towards_the_total(self):
        """`total` 是「有没有事要办」的唯一判据（命令行退出码、卡片要不要推都看它）。
        不计进去的话，唯一的发现是一个野桶时，体检会打出「没有发现需要处理的」。"""
        rep = self.report(buckets=[bucket("nobody")], registered=set())
        self.assertEqual(rep.total, 1)
        self.assertIn("nobody", rep.render())

    def test_the_section_is_between_abandoned_and_rotate(self):
        """顺序就是展示顺序：人/归属的问题排在密钥问题前面。
        这一类是「没人管的数据」，归在前半段。"""
        kinds = [k for k, _, _, _ in hygiene.Report().sections()]
        self.assertEqual(
            kinds,
            [
                "left",
                "unknown",
                "orphan",
                "abandoned",
                "stray_bucket",
                "stray_dir",
                "rotate",
                "unused",
            ],
        )

    def test_the_view_carries_it_to_the_panel(self):
        """面板读的是 `view()`。少了这一节，网页上看不到、而命令行看得到 ——
        两边对同一份数据给出不同结论，那之后谁都不会信这一页。"""
        rep = self.report(buckets=[bucket("nobody", "oss-cn-hangzhou")], registered=set())
        section = next(s for s in hygiene.view(rep)["sections"] if s["kind"] == "stray_bucket")
        self.assertEqual(section["count"], 1)
        self.assertEqual(section["items"][0]["subject"], "nobody")
        self.assertTrue(section["title"])
        self.assertTrue(section["note"])

    def test_the_real_world_shape(self):
        """真机那次的形状压缩版：27 个桶、登记 15 个、云产品自建一堆，
        最后要人认领的是 4 个。这条是「整条链拼起来还对不对」的样子货。"""
        registered = {f"reg-{i}" for i in range(15)}
        cloud = (
            [bucket(f"reg-{i}") for i in range(15)]
            + [bucket(f"cri-{i}-registry") for i in range(6)]
            + [bucket(f"oss-pai-{i}") for i in range(2)]
            + [
                bucket("wuji-sing"),
                bucket("wuji-data-tran"),
                bucket("h2r-dlc-cn-hangzhou"),
                bucket("data-infra-emr-log-hgh"),
            ]
        )
        self.assertEqual(len(cloud), 27)
        rep = self.report(buckets=cloud, registered=registered)
        self.assertEqual(
            [f.subject for f in rep.stray_bucket],
            ["data-infra-emr-log-hgh", "h2r-dlc-cn-hangzhou", "wuji-data-tran", "wuji-sing"],
        )


class LoadRegisteredBucketsTests(unittest.TestCase):
    """从两个文件读「已登记的桶」（`hygiene.load_registered_buckets`）。

    这里是**面板和命令行唯一共用的那份实现**。它必须自己判断文件在不在：
    `catalog.load()` 缺文件返空目录、`load_allowed()` 缺文件返空列表，对它们各自的
    本职都是安全的一侧，对这里正好相反 —— 空清单会让云上每个桶都被报成「没登记」。
    `identity/` 整个是 gitignored 的，新部署第一次开面板就是这个状态。

    「只缺一个」是真机上的常态（面板服务器有模板、没有 dataset-buckets.json），
    那时**必须照常用另一个来源**，不能整类跳过；但要把「少了哪一半」说出来（`notes`），
    否则登记在缺失那一半里的桶会被报成野桶，而看的人以为清单是全的。

    返回的是 `(names, notes)`：`names is None` = 一个来源都没真读到，那一类整个跳过，
    此时 `notes` 恒为空（skipped 里已经有话说，再来一条只是噪音）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def load(self, templates=None, allowed=None):
        return hygiene.load_registered_buckets(
            str(templates) if templates else None, str(allowed) if allowed else None
        )

    def names(self, templates=None, allowed=None):
        return self.load(templates, allowed)[0]

    def write_bucketless_templates(self):
        """合法模板，但**一个带桶的凭证模板都没有**（只有权限包这类）。"""
        path = self.root / "bucketless-templates.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "wuji-request-templates@1",
                    "templates": [
                        {
                            "id": "perm",
                            "kind": "permission",
                            "platform": "aliyun",
                            "account": "100000000000001",
                            "title": "权限包",
                            "groups": ["some-group"],
                            "max_days": 90,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_templates(self, buckets):
        path = self.root / "request-templates.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "wuji-request-templates@1",
                    "templates": [
                        {
                            "id": "cred",
                            "kind": "credential",
                            "platform": "aliyun",
                            "account": "100000000000001",
                            "title": "凭证",
                            "caps": ["list"],
                            "buckets": [{"name": n, "region": "cn-hangzhou"} for n in buckets],
                            "role_arn": "acs:ram::100000000000001:role/fake-sts-role",
                            "max_hours": 720,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_allowed(self, names):
        path = self.root / "dataset-buckets.json"
        path.write_text(json.dumps({"allowed": names}), encoding="utf-8")
        return path

    def test_both_files_missing_is_none(self):
        """两个都读不到 → None → 体检说「没法判断」。返回 set() 的那一版会在
        新部署的面板上刷出十几条假线索，而假线索一多，这一栏就再也没人看了。"""
        self.assertIsNone(self.names(self.root / "nope.json", self.root / "nope2.json"))
        self.assertIsNone(self.names(None, None))
        self.assertIsNone(self.names("", ""))

    def test_nothing_read_means_no_notes_either(self):
        """`names is None` 时 `notes` 必须是空的：那一类整个跳过、skipped 里已经
        有「没法判断」那句话了，再补两条「少了一个来源」只是把真正的结论挤下去。"""
        self.assertEqual(self.load(self.root / "nope.json", self.root / "nope2.json")[1], ())

    def test_only_templates_still_counts(self):
        """**真机常态**：面板服务器有模板、没有 dataset-buckets.json。
        这时不能整类跳过 —— 那等于因为少一个来源就把这一类关掉。"""
        names, notes = self.load(self.write_templates(["wuji-sing"]), self.root / "nope.json")
        self.assertEqual(names, {"wuji-sing"})
        self.assertTrue(any("数据集白名单" in n and "不在" in n for n in notes), notes)

    def test_only_the_whitelist_still_counts(self):
        names, notes = self.load(self.root / "nope.json", self.write_allowed(["h2r-dlc"]))
        self.assertEqual(names, {"h2r-dlc"})
        self.assertTrue(any("申请模板" in n for n in notes), notes)

    def test_both_sources_are_merged_and_say_nothing(self):
        """两个来源都读到了 → 没什么可说的。这时还出声的话，「可能多报」那句话
        就会天天出现在报告里，出现得多了就等于不存在。"""
        names, notes = self.load(
            self.write_templates(["wuji-sing"]), self.write_allowed(["h2r-dlc"])
        )
        self.assertEqual(names, {"wuji-sing", "h2r-dlc"})
        self.assertEqual(notes, ())

    def test_an_unconfigured_path_is_not_worth_a_note(self):
        """**没配**（路径是空的）和**配了但文件不在**是两回事：前者是「这个来源本来
        就不用」，报出来是噪音；后者才是「该有的东西不见了」。"""
        _, notes = self.load(self.write_templates(["wuji-sing"]), None)
        self.assertEqual(notes, ())

    def test_a_file_that_reads_fine_but_yields_no_names_is_none(self):
        """**抽不出任何桶名 = 按「没读到」处理。**

        以前这里断言的是 `set()`（「文件在、读得动、里面确实没有桶」是一个有效结论）。
        auditor 复现的场景说明那个区分在现实里不成立：模板文件完全合法、只是一个
        带桶的凭证模板都没有（全是开账号/权限包），`catalog.load()` 返回的正是空目录 ——
        和「文件缺失」在返回值上分不开。而分不开时只能选不报：这一栏的建议里写着
        「确认没用了再删」，多报的代价是让人拿着合法桶去考虑删不删。
        """
        self.assertIsNone(self.names(self.write_bucketless_templates(), None))
        empty_tpl = self.root / "empty-templates.json"
        empty_tpl.write_text(
            json.dumps({"schema": "wuji-request-templates@1", "templates": []}), encoding="utf-8"
        )
        self.assertIsNone(self.names(empty_tpl, None))
        self.assertIsNone(self.names(None, self.write_allowed([])))

    def test_a_bucketless_template_file_still_lets_the_whitelist_win(self):
        """反过来：模板里没有桶，但白名单里有 —— 那就按白名单算，别整类跳过。"""
        names, _ = self.load(self.write_bucketless_templates(), self.write_allowed(["h2r-dlc"]))
        self.assertEqual(names, {"h2r-dlc"})

    def test_a_broken_template_file_falls_back_to_the_other_source(self):
        """模板文件坏了是运维问题，不该把白名单那一路一起带走，更不该让体检崩掉 ——
        崩掉的代价是整页打不开，而它本来只该少认一批桶。"""
        bad = self.root / "bad-templates.json"
        bad.write_text("{ not json", encoding="utf-8")
        names, notes = self.load(bad, self.write_allowed(["h2r-dlc"]))
        self.assertEqual(names, {"h2r-dlc"})
        self.assertIn("申请模板 读不了（CatalogError）", notes)

    def test_a_template_file_that_is_not_utf8_is_caught_too(self):
        """非 UTF-8 的模板文件抛的是 `UnicodeDecodeError`（`ValueError` 的子类，
        **不是 DeliveryError**）。只接 DeliveryError 的话它会穿到接口那一层，
        把整个体检页变成「面板数据暂不可用」—— 而真实原因只是一个文件存错了编码。"""
        bad = self.root / "gbk-templates.json"
        bad.write_bytes(b'\xff\xfe{"schema": "wuji-request-templates@1"}')
        names, notes = self.load(bad, self.write_allowed(["keep-me"]))
        self.assertEqual(names, {"keep-me"})
        self.assertIn("申请模板 读不了（UnicodeDecodeError）", notes)

    def test_a_broken_whitelist_falls_back_to_the_other_source(self):
        """反向同理。白名单的「格式不对」和「语法错」都走 CustomDatasetError。"""
        bad = self.root / "bad-allowed.json"
        bad.write_text(json.dumps({"nope": ["x"]}), encoding="utf-8")
        names, notes = self.load(self.write_templates(["wuji-sing"]), bad)
        self.assertEqual(names, {"wuji-sing"})
        self.assertIn("数据集白名单 读不了（CustomDatasetError）", notes)

    def test_both_files_broken_is_none_not_an_empty_set(self):
        """两个都坏 = 一个来源都没真读到 = 不知道。**不能当成「什么都没登记」** ——
        那会在两个文件同时被改坏的那一刻，把云上每个桶都报成野桶。"""
        bad_tpl = self.root / "bad-templates.json"
        bad_tpl.write_text("{ not json", encoding="utf-8")
        bad_allowed = self.root / "bad-allowed.json"
        bad_allowed.write_text("[1, 2", encoding="utf-8")
        self.assertEqual(self.load(bad_tpl, bad_allowed), (None, ()))

    def test_the_whole_class_is_skipped_when_nothing_was_read(self):
        """串起来看一眼：读不到 → `build()` 标跳过 → 那一栏是空的、也不多一句废话。"""
        names, notes = self.load(self.root / "nope.json", self.root / "nope2.json")
        rep = hygiene.build(
            None,
            [],
            buckets=[bucket("nobody")],
            registered=names,
            registered_notes=notes,
            now=NOW,
        )
        self.assertEqual(rep.stray_bucket, [])
        self.assertTrue(any("没法判断哪些桶没登记" in s for s in rep.skipped))
        self.assertFalse(any("可能多报" in s for s in rep.skipped))

    def test_half_a_whitelist_reports_but_says_it_may_over_report(self):
        """auditor 那条的完整走法：模板坏了、白名单好着 —— 只该报白名单里没有的那个，
        **登记在白名单里的不能被误报**；同时报告里要留下「这次是按半份数据判的」。
        不说的话，看的人会拿着一份多报的清单去删桶。"""
        bad = self.root / "bad-templates.json"
        bad.write_text("{ not json", encoding="utf-8")
        names, notes = self.load(bad, self.write_allowed(["keep-me"]))
        rep = hygiene.build(
            None,
            [],
            buckets=[bucket("keep-me"), bucket("zzz")],
            registered=names,
            registered_notes=notes,
            now=NOW,
        )
        self.assertEqual([f.subject for f in rep.stray_bucket], ["zzz"])
        self.assertTrue(any("可能多报" in s and "申请模板" in s for s in rep.skipped))

    def test_the_cli_delegates_to_the_same_function(self):
        """命令行不许再自己写一份。各写一份的那一版：命令行防住了模板、漏了白名单，
        面板两边都没防 —— 同一份逻辑写两遍，第二遍必然缺一块。"""
        from delivery import cli_requests

        tpl = self.write_templates(["wuji-sing"])
        allowed = self.write_allowed(["h2r-dlc"])
        args = argparse.Namespace(templates=str(tpl), allowed=str(allowed))
        self.assertEqual(cli_requests._registered_buckets(args), self.load(tpl, allowed))

        missing = argparse.Namespace(templates=str(self.root / "nope.json"), allowed="")
        self.assertEqual(cli_requests._registered_buckets(missing), (None, ()))

    def test_the_cli_survives_an_args_without_allowed(self):
        """`--allowed` 是后加的。老的 Namespace（或别处复用这个函数）没有这个属性时
        要退化成「只有模板」，不能 AttributeError 把整次体检打掉。"""
        from delivery import cli_requests

        args = argparse.Namespace(templates=str(self.write_templates(["wuji-sing"])))
        self.assertEqual(cli_requests._registered_buckets(args)[0], {"wuji-sing"})


class BackendRegisteredBucketsTests(unittest.TestCase):
    """面板那一路：同一个函数 + 一层缓存。缓存键必须含**两个**路径的 stamp。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.tpl = self.root / "request-templates.json"
        self.allowed = self.root / "dataset-buckets.json"

    def write_allowed(self, names, *, mtime=None):
        self.allowed.write_text(json.dumps({"allowed": names}), encoding="utf-8")
        if mtime:
            os.utime(self.allowed, (mtime, mtime))

    def write_templates(self, buckets, *, mtime=None):
        self.tpl.write_text(
            json.dumps(
                {
                    "schema": "wuji-request-templates@1",
                    "templates": [
                        {
                            "id": "cred",
                            "kind": "credential",
                            "platform": "aliyun",
                            "account": "100000000000001",
                            "title": "凭证",
                            "caps": ["list"],
                            "buckets": [{"name": n, "region": "cn-hangzhou"} for n in buckets],
                            "role_arn": "acs:ram::100000000000001:role/fake-sts-role",
                            "max_hours": 720,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if mtime:
            os.utime(self.tpl, (mtime, mtime))

    def backend(self):
        from delivery import server

        return server.Backend(templates_path=str(self.tpl), dataset_buckets_path=str(self.allowed))

    def test_missing_files_are_none_on_the_panel_too(self):
        """面板上这条是重灾区：两个文件都是 gitignored 的，新部署第一次开面板
        正好是这个状态。以前这里会把云上每个桶都报成没登记。"""
        self.assertEqual(self.backend().registered_buckets(), (None, ()))

    def test_it_merges_both_files(self):
        self.write_templates(["wuji-sing"])
        self.write_allowed(["h2r-dlc"])
        self.assertEqual(self.backend().registered_buckets()[0], {"wuji-sing", "h2r-dlc"})

    def test_changing_the_whitelist_invalidates_the_cache(self):
        """只把模板路径放进缓存键的话，改了白名单要等到模板文件也动过才生效 ——
        表现是「我明明把这个桶登记了，体检还在报它」，而下一次重启又自己好了。"""
        self.write_templates(["wuji-sing"], mtime=1_700_000_000)
        self.write_allowed(["h2r-dlc"], mtime=1_700_000_000)
        backend = self.backend()
        self.assertEqual(backend.registered_buckets()[0], {"wuji-sing", "h2r-dlc"})
        self.write_allowed(["h2r-dlc", "late-comer"], mtime=1_700_000_900)
        self.assertEqual(backend.registered_buckets()[0], {"wuji-sing", "h2r-dlc", "late-comer"})

    def test_changing_the_templates_invalidates_the_cache(self):
        self.write_templates(["wuji-sing"], mtime=1_700_000_000)
        self.write_allowed(["h2r-dlc"], mtime=1_700_000_000)
        backend = self.backend()
        self.assertEqual(backend.registered_buckets()[0], {"wuji-sing", "h2r-dlc"})
        self.write_templates(["wuji-sing", "new-bkt"], mtime=1_700_000_900)
        self.assertEqual(backend.registered_buckets()[0], {"wuji-sing", "h2r-dlc", "new-bkt"})

    def test_an_appearing_file_invalidates_the_cache(self):
        """从「文件不存在」到「文件建出来了」也得重算：登记表是人手建的，
        第一次建好之后不该还要重启面板才认。"""
        backend = self.backend()
        self.assertEqual(backend.registered_buckets(), (None, ()))
        self.write_allowed(["h2r-dlc"])
        self.assertEqual(backend.registered_buckets()[0], {"h2r-dlc"})

    def test_an_unchanged_read_is_served_from_cache(self):
        """缓存还得真在缓存：体检页每次打开都读，两个文件都没动时不该重读。"""
        self.write_allowed(["h2r-dlc"])
        backend = self.backend()
        first = backend.registered_buckets()
        with mock.patch.object(hygiene, "load_registered_buckets", side_effect=AssertionError):
            self.assertIs(backend.registered_buckets(), first)


class SummaryAndScopeTests(unittest.TestCase):
    """计数与 `scope`。两条都是「类别清单只该有一份」的余波。"""

    def test_summary_covers_every_section(self):
        """**和前端那两张表同款守卫**：`summary()` 必须按 `_SECTIONS` 生成。
        手抄类别的那一版漏过 `unknown`（`total` 算了它、`summary` 里没有），
        加 `stray_bucket` 时差点漏第二次 —— 少一个键不会报错，只会让卡片少一行。"""
        keys = set(hygiene.summary(hygiene.Report()))
        kinds = {kind for kind, _, _ in hygiene._SECTIONS}
        self.assertEqual(sorted(kinds - keys), [], "summary() 少了这些类别")
        self.assertEqual(keys - kinds, {"incomplete"}, "summary() 只该额外带一个 incomplete")

    def test_summary_never_drifts_from_total(self):
        """`total` 和 `summary()` 是同一份结论的两种形态。两边各数各的就会出现
        「卡片上加起来是 3、标题说有 4 件事」—— 那时候人只会两边都不信。"""
        rep = hygiene.build(
            None,
            [],
            buckets=[bucket("nobody"), bucket("nobody-2")],
            registered=set(),
            now=NOW,
        )
        counts = {k: v for k, v in hygiene.summary(rep).items() if k != "incomplete"}
        self.assertEqual(sum(counts.values()), rep.total)
        self.assertEqual(counts["stray_bucket"], 2)

    def test_incomplete_survives(self):
        """`incomplete` 是定时任务判断「这份清单能不能当真」的唯一开关，不能弄丢。"""
        rep = hygiene.build(None, [], buckets=None, registered=None, now=NOW)
        self.assertTrue(hygiene.summary(rep)["incomplete"])

    def test_a_bucket_scope_has_no_empty_segment(self):
        """桶不属于任何子账号，`account` 是空的。照拼会渲染成 `aliyun//桶名`，
        看的人第一反应是「这里少了点什么」，而实际上没少。"""
        rep = hygiene.build(None, [], buckets=[bucket("nobody")], registered=set(), now=NOW)
        self.assertEqual(rep.stray_bucket[0].scope, "aliyun/nobody")
        self.assertNotIn("//", rep.stray_bucket[0].scope)

    def test_a_user_scope_still_has_all_three_segments(self):
        """别为了去掉空段把正常那条也改了：人的线索必须还是 `平台/账号/用户名`，
        那是去控制台里找到这个号的唯一线索。"""
        found = hygiene.Finding(
            kind="orphan", platform="aliyun", account="100", subject="lisi", why="x"
        )
        self.assertEqual(found.scope, "aliyun/100/lisi")


class AssetsCollectBucketWiringTests(unittest.TestCase):
    """`delivery assets collect` 里三个独立的 try。

    合成一个的代价实测过（回收站那次）：少一个 `ram:ListUsersInRecycleBin`，
    整份数据集登记就会从快照里消失，而 PAI 那边一点问题都没有。桶清单是第三个，
    同理：少一个 `oss:ListBuckets` 不该把数据集和回收站一起带走。
    """

    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        os.chdir(self.root)
        (self.root / "identity").mkdir()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)

    def collect(self, *, buckets=None, bucket_exc=None, datasets_exc=None, skip_pai=False):
        from delivery import cli_requests

        out = self.root / "identity" / "assets.json"
        args = argparse.Namespace(
            assets_command="collect",
            aliyun_profile=["ALIYUN"],
            member=[],
            skip_volcano=True,
            skip_pai=skip_pai,
            out=str(out),
        )

        def fake_buckets(creds, **kw):
            if bucket_exc:
                raise bucket_exc
            return buckets if buckets is not None else [bucket("wuji-sing")]

        def fake_datasets(creds, **kw):
            if datasets_exc:
                raise datasets_exc
            return [{"Name": "ds", "owner_kind": assets.OWNER_GONE, "UserId": "9"}], []

        with (
            mock.patch.object(aliyun.Credentials, "from_env", staticmethod(lambda *a, **k: CREDS)),
            mock.patch.object(assets, "collect_aliyun", lambda *a, **k: ("100", [])),
            mock.patch.object(assets, "collect_recycle_bin", lambda *a, **k: [{"user_id": "9"}]),
            mock.patch.object(assets, "collect_pai_datasets", fake_datasets),
            mock.patch.object(assets, "collect_buckets", fake_buckets),
        ):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli_requests._assets(args)
        self.assertEqual(rc, 0)
        return json.loads(out.read_text(encoding="utf-8")), buf.getvalue()

    def test_buckets_land_in_the_snapshot(self):
        data, text = self.collect(buckets=[bucket("wuji-sing"), bucket("nobody")])
        self.assertEqual([b["name"] for b in data["buckets"]], ["wuji-sing", "nobody"])
        self.assertNotIn("bucket_error", data)
        self.assertIn("OSS 桶 2 个", text)

    def test_a_bucket_failure_leaves_datasets_and_the_recycle_bin_alone(self):
        """桶采不到时：`buckets` 这个键不写（= 体检标跳过）、`bucket_error` 落进快照，
        而数据集和回收站照常。三个 try 并成一个的话这条会红。"""
        from delivery.clouds.oss import OssDenied

        data, text = self.collect(bucket_exc=OssDenied("列桶被拒（AccessDenied）"))
        self.assertNotIn("buckets", data)
        self.assertIn("AccessDenied", data["bucket_error"])
        self.assertEqual(len(data["datasets"]), 1)
        self.assertEqual(len(data["recycle_bin"]), 1)
        self.assertIn("OSS 桶清单没采到", text)

    def test_skip_pai_does_not_quietly_skip_the_bucket_list(self):
        """`--skip-pai` 是「跳过 PAI 数据集」，不是「跳过所有阿里云的东西」。
        列桶挂在它下面的话：有人为了绕开 PAI 权限加个 `--skip-pai`，
        会把体检里「没登记的桶」一起悄悄关掉 —— 而快照里既没有 `buckets`、
        也没有 `bucket_error`，看起来和「这次没采」一模一样，没人会发现。"""
        data, text = self.collect(buckets=[bucket("wuji-sing")], skip_pai=True)
        self.assertEqual([b["name"] for b in data["buckets"]], ["wuji-sing"])
        self.assertNotIn("datasets", data)
        self.assertIn("OSS 桶 1 个", text)

    def test_a_dataset_failure_leaves_the_bucket_list_alone(self):
        """反过来也一样：PAI 权限掉了不该把桶清单带走。"""
        from delivery.errors import DeliveryError

        data, _ = self.collect(datasets_exc=DeliveryError("PAI 没权限"))
        self.assertNotIn("datasets", data)
        self.assertEqual([b["name"] for b in data["buckets"]], ["wuji-sing"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
