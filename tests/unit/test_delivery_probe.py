"""外部探活（`cli._cmd_probe` + `deploy/panel/delivery-probe.*`）。

起因是 2026-09-24：那台跑 MLflow 的机器公网 IP 掉了，整机从外面失联（ping/22/80/443
全不通），而**没有任何地方会告诉你** —— 服务自己没崩，所以自己发不出告警；是用户来问
「怎么打不开」才发现的。这条命令守的就是这类「没人会主动发现」的故障。

正因为它守的是这类故障，**它必须保持可信**：报过一次假警之后就不再有人认真看了，
而它静默失效的话，没人会发现它不干活了 —— 探针的两种坏法都很贵。所以这个文件里
分量最重的是这几组：

    ①「连续 N 次才报」      —— 让它可信的全部理由，也最容易被以后有人「简化」掉
    ②「什么算活着」         —— 2xx/3xx/4xx/5xx 都算活着（探的是机器在不在，
                               不是接口对不对），连不上/超时/DNS 才算挂
    ③ 状态文件             —— 读坏了不许因此报警；**写不下去必须吵**
                               （写不下去 = 计数永远到不了阈值 = 这条告警永久静音）
    ④ 只打 http(s)、不跟随重定向、不走代理 —— 地址来自配置，是这台机上少有的
                               「按外部输入去打开一个 URL」的地方
    ⑤ 恢复提示 / 参数与环境变量 / 去重与陈旧计数
    ⑥ 单元文件             —— 代码和 unit 是一对，拆开任何一半告警就静默失效

**不碰公网。** 多数用例把 `cli._probe_opener` 换成桩（顺带当 spy：证明非 http 地址
根本没被打开过）；`LocalServerTests` 起一个 127.0.0.1 上的真 http server，不打桩地走
真实的 urllib —— 全打桩的话，「403 算活着」「302 不跟随」测的其实是我对 urllib 的想象。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from delivery import cli

#: 真实量级的时间戳（`unit-failed` 的冷却判的是 `now - last_alert >= 6h`，
#: 拿小数字当 now 连第一条都会被算进冷却期）。只有和告警共处的那两组用得上。
NOW = 1_790_000_000.0

#: 桩里表示「opener 里没有能处理它的 handler」。**`open()` 这时返回的是 None，
#: 不是抛错** —— 开发过程中真踩过：None 被当成「打开成功」，于是 `ftp://` / `file://`
#: 反而被判成「活着」。这个哨兵就是为了能在用例里复现那条路。
NO_HANDLER = object()


class _Response:
    """`opener.open(...)` 的返回值：代码只看 `.status` 并 `close()`。"""

    def __init__(self, status: int):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    """服务端真回了一个状态码 —— 机器活着，只是这个接口不让进。"""
    return urllib.error.HTTPError(url, code, f"status {code}", {}, None)


class _FakeOpener:
    """替 `cli._probe_opener()`。既是桩也是 spy。"""

    def __init__(self, owner):
        self.owner = owner

    def open(self, req, timeout=None):
        url = getattr(req, "full_url", req)
        self.owner.calls.append((url, timeout))
        self.owner.requests.append(req)
        if self.owner.on_open is not None:
            self.owner.on_open()  # 模拟「这一趟 I/O 要花十几秒，期间别人在写盘」
        answer = self.owner.answers.get(url, self.owner.default)
        if isinstance(answer, BaseException):
            raise answer
        if answer is NO_HANDLER:
            return None
        return _Response(answer)


class ProbeBase(unittest.TestCase):
    #: `LocalServerTests` 关掉它 —— 那组要走真的 opener。
    PATCH_OPENER = True

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.state = self.work / "probe-state.json"

        #: 每次 open 的 `(url, timeout)`。**当 spy 用**：协议白名单那几条断言的是
        #: 「压根没去打开」，而不是「打开了但结果被判成失败」—— 后者的话白名单等于没有，
        #: `file:///etc/shadow` 照样被读了一遍。
        self.calls = []
        self.requests = []
        #: url -> 状态码 / 异常实例 / NO_HANDLER。没列出来的走 `self.default`。
        self.answers = {}
        self.default = 200
        self.on_open = None
        if self.PATCH_OPENER:
            patcher = mock.patch.object(cli, "_probe_opener", lambda: _FakeOpener(self))
            patcher.start()
            self.addCleanup(patcher.stop)

        # **裸 `urlopen` 是禁区**：它带着默认的 handler 集合（FTP/File/Data）和
        # 环境变量代理。哪天有人图省事从 opener 退回 `urlopen`，这条会当场响，
        # 而不是等某次重定向把面板骗去连内网端口时才发现。
        guard = mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("探活不许走裸 urlopen —— 要用 _probe_opener()"),
        )
        guard.start()
        self.addCleanup(guard.stop)

        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # 开发机上真配了这些变量的话，不清掉的用例会去探真地址、或者走开发机的代理
        for name in (
            "DELIVERY_PROBE_URLS",
            "http_proxy",
            "https_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "all_proxy",
            "no_proxy",
            "NO_PROXY",
        ):
            os.environ.pop(name, None)

    def run_probe(self, *argv, state=True):
        """跑一次 `delivery probe`，返回 `(退出码, stdout, stderr)`。

        走 `cli.main` 而不是直接调 `_cmd_probe`：**dispatch 那一行也是被测对象**。
        漏了它 argparse 照样认这个子命令，main 一路走到底返回 2，
        而「探活每次都退 2」和「地址真打不开」并不容易区分。
        """
        args = list(argv)
        if state and "--state" not in args:
            args += ["--state", str(self.state)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["probe", *args])
        return code, out.getvalue(), err.getvalue()

    def counts(self):
        """状态文件里记的连续失败次数。**外层没有键** —— 文件内容直接就是 `{url: n}`。"""
        return json.loads(self.state.read_text(encoding="utf-8"))


class ConsecutiveFailureTests(ProbeBase):
    """①「连续 N 次才报」。**这条命令能被人当回事的全部理由。**

    一次 curl 失败可能只是网络抖一下；而报过一次假警的告警，下一次真出事也没人看了。
    这组用例是给「以后有人觉得这个计数很啰嗦、顺手简化掉」准备的。
    """

    URL = "https://mlflow.example.invalid/"

    def setUp(self):
        super().setUp()
        self.answers[self.URL] = OSError("connection refused")

    def test_the_first_two_failures_do_not_page(self):
        """失败 1、2 次退 0（journal 里留一行，但不惊动人）。"""
        for i in (1, 2):
            code, _, err = self.run_probe("--url", self.URL)
            self.assertEqual(code, 0, f"第 {i} 次就退非零 = OnFailure 立刻私聊，抖一下就报警")
            self.assertIn(f"连续第 {i} 次", err, "journal 里要留痕，否则和「命令没跑」一样")
            self.assertNotIn("★", err, "★ 是「这次真报了」的标记，第 1/2 次不该有")

    def test_the_third_failure_pages(self):
        """第 3 次退 1 —— 交给 `OnFailure=` 那条管道去私聊。"""
        self.run_probe("--url", self.URL)
        self.run_probe("--url", self.URL)
        code, _, err = self.run_probe("--url", self.URL)
        self.assertEqual(code, 1)
        self.assertIn("★", err)
        self.assertIn(self.URL, err, "报警正文里要有到底哪个地址打不开")
        self.assertIn("连续第 3 次", err)

    def test_it_keeps_paging_while_it_stays_down(self):
        """挂着不动就一直退非零。

        「报过一次就不再报」的话，6 小时冷却之后那条「它还挂着」的提醒永远不会来
        —— 而冷却本来就是设计来发第二条的（见 `_alert_cooldown`）。
        """
        for _ in range(3):
            self.run_probe("--url", self.URL)
        for n in (4, 5):
            code, _, err = self.run_probe("--url", self.URL)
            self.assertEqual(code, 1)
            self.assertIn(f"连续第 {n} 次", err, "连号要接着涨，不然读的人以为刚挂")

    def test_one_success_in_between_clears_the_streak(self):
        """**中间成功一次就清零。**

        不清零的话，一台一天抖三次、每次一分钟的机器，三天内必然攒够 3 次 →
        报一条「连续打不开」，而它其实一直是好的。那一条就是把这个告警变成噪音的开始。
        """
        self.run_probe("--url", self.URL)
        self.run_probe("--url", self.URL)
        self.answers[self.URL] = 200
        code, out, _ = self.run_probe("--url", self.URL)
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        self.assertEqual(self.counts()[self.URL], 0, "成功一次没把计数清零")

        self.answers[self.URL] = OSError("connection refused")
        for i in (1, 2):
            code, _, err = self.run_probe("--url", self.URL)
            self.assertEqual(code, 0, "清零后又挂 2 次不该报 —— 累加的话这里会是 1")
            self.assertIn(f"连续第 {i} 次", err)

    def test_fails_one_pages_immediately(self):
        """`--fails 1` = 不宽限，第一次就报。有人真想要这个语义时得还在。"""
        code, _, err = self.run_probe("--url", self.URL, "--fails", "1")
        self.assertEqual(code, 1)
        self.assertIn("★", err)

    def test_fails_zero_or_negative_is_not_a_silencer(self):
        """`--fails 0` / 负数**不许**变成「永远不报」（代码里是 `max(1, args.fails)`）。

        直接拿 `n >= args.fails` 判的话 `--fails 0` 恒为真（当场报，还算好的），
        而按别的写法很容易写成永不满足 —— 那就是一个看不出来的静音开关：
        单元照跑、日志照打、状态文件照写，只是永远不退非零。配错了一声不吭，
        正是这条告警最不能有的毛病。
        """
        for bad in ("0", "-1", "-999"):
            with self.subTest(fails=bad):
                self.state.unlink(missing_ok=True)
                code, _, err = self.run_probe("--url", self.URL, "--fails", bad)
                self.assertEqual(code, 1, f"--fails {bad} 把告警静音了")
                self.assertIn("★", err)

    def test_the_default_threshold_is_three(self):
        """默认 3 —— 和 5 分钟的 timer 配成「连挂约 15 分钟才报」（README 里写的就是这个数）。

        改默认值不是一个局部改动：它同时改掉「多久发现」和「多容易误报」，
        而 `delivery-probe.service` 里那个显式的 `--fails 3` 会让线上毫无变化，
        于是本地和线上悄悄分叉。
        """
        args = cli.build_parser().parse_args(["probe"])
        self.assertEqual(args.fails, 3)

    def test_every_url_is_probed_even_after_one_is_down(self):
        """一个挂了不能让后面的地址不探了 —— 否则清单越长，后面的越形同虚设。"""
        other = "https://tb.example.invalid/"
        self.answers[other] = 200
        code, out, err = self.run_probe("--url", self.URL, "--url", other, "--fails", "1")
        self.assertEqual(code, 1)
        self.assertIn(other, out, "第一个失败之后就不探了")
        self.assertIn(self.URL, err)
        self.assertEqual([c[0] for c in self.calls], [self.URL, other])


class VerdictTests(ProbeBase):
    """② 判据方向：什么算活着、什么算挂。"""

    URL = "https://mlflow.example.invalid/"

    def probe_once(self, answer, *, fails="1"):
        self.answers[self.URL] = answer
        return self.run_probe("--url", self.URL, "--fails", fails)

    def test_2xx_and_3xx_are_alive(self):
        """3xx 也算活着：**我们不跟随重定向**，收到 302 本身就证明对面还在。"""
        for status in (200, 204, 301, 302, 307):
            with self.subTest(status=status):
                self.state.unlink(missing_ok=True)
                code, out, _ = self.probe_once(status)
                self.assertEqual(code, 0)
                self.assertIn(f"HTTP {status}", out)

    def test_4xx_and_5xx_are_alive(self):
        """**4xx/5xx 算活着。** 探的是「这台机还在不在」，不是「这个接口对不对」。

        把 403 当挂掉的话，任何一次门禁调整（加一条 IP 白名单、换一次 oauth2-proxy
        配置）都会变成一条故障告警 —— 而那恰恰是有人正在动它、最不需要被喊的时候。
        500 同理：应用炸了是另一类问题，有它自己的日志和报警；这条命令只回答
        「这台机器还在网上吗」。
        """
        for status in (401, 403, 404, 500, 502, 503):
            with self.subTest(status=status):
                self.state.unlink(missing_ok=True)
                code, out, err = self.probe_once(_http_error(self.URL, status))
                self.assertEqual(code, 0, f"HTTP {status} 被判成挂了")
                self.assertIn(f"HTTP {status}", out)
                self.assertNotIn("✗", err)

    def test_the_status_code_shows_up_even_for_errors(self):
        """状态码要打出来，不能是「HTTP ?」。

        判据上 403 和 502 都是「活着」，但出事时人要靠这个数分方向：403 去看门禁、
        502 去看后端。装 `HTTPDefaultErrorHandler` 就是为了这个。
        """
        _, out, _ = self.probe_once(_http_error(self.URL, 502))
        self.assertIn("HTTP 502", out)
        self.assertNotIn("HTTP ?", out)

    def test_an_http_error_also_clears_the_streak(self):
        """403 不只是「这次不报」，它得把连号清零 —— 它是一次成功的探活。"""
        self.answers[self.URL] = OSError("connection refused")
        self.run_probe("--url", self.URL)
        self.run_probe("--url", self.URL)
        self.probe_once(_http_error(self.URL, 403), fails="3")
        self.assertEqual(self.counts()[self.URL], 0)

    def test_network_level_failures_are_down(self):
        """连不上 / 超时 / DNS 解不出 —— 这几样才是「机器不在了」。

        错误类型名要进 journal：`URLError` 和 `gaierror` 指向完全不同的排查方向
        （机器没了 vs DNS/域名出了问题），只写「失败」等于让人从头查。
        """
        cases = {
            "URLError": urllib.error.URLError(ConnectionRefusedError(111, "refused")),
            "gaierror": socket.gaierror(-2, "Name or service not known"),
            "timeout": socket.timeout("timed out"),
            "OSError": OSError(113, "No route to host"),
        }
        for name, exc in cases.items():
            with self.subTest(kind=name):
                self.state.unlink(missing_ok=True)
                code, _, err = self.probe_once(exc)
                self.assertEqual(code, 1, f"{name} 没被判成挂")
                self.assertIn("✗", err)
                self.assertIn(type(exc).__name__, err, "journal 里要看得出是哪一类失败")

    def test_an_opener_that_returns_none_is_down_not_alive(self):
        """**开发时真踩过的坑，单独锁一条。**

        `OpenerDirector` 里没有能处理这个协议的 handler 时，`open()` **返回 None，
        不抛错**。拿 None 当「打开成功」的话，`ftp://` / `file://` 会被判成**活着**
        —— 方向正好反了：一条配错协议的地址从此永远报平安，而探活等于没装。

        所以协议这件事有两道：结构上不装 handler（防「真去连了」）+ 调用处判 None
        和判串（防「被判成活着」）。这条守的是后者。
        """
        code, _, err = self.probe_once(NO_HANDLER)
        self.assertEqual(code, 1, "opener 返回 None 被当成「活着」了")
        self.assertIn("✗", err)

    def test_non_http_schemes_are_down_and_never_opened(self):
        """非 http(s) 一律算挂，**而且根本不去打开它**。

        地址来自配置文件（`panel.env` 里的 `DELIVERY_PROBE_URLS`），这是这台机器上
        少有的「按外部输入去打开一个 URL」的地方。

        断言分两半，缺一不可：
          · 退非零（判成挂）—— 只有这半的话，「打开了、读到了、然后判失败」也能过；
          · opener 一次都没被调用 —— 这才是白名单真的挡在了前面。
        """
        bad = [
            "file:///etc/passwd",
            "file:///etc/shadow",
            "ftp://example.invalid/x",
            "gopher://example.invalid/",
            "data:text/plain,hi",
            "/etc/passwd",  # 相对/绝对路径，不是 URL
            "example.invalid",  # 漏写 scheme
            "https:",  # 有 scheme 没主机
            "http:/single-slash",
            "httpx://example.invalid/",  # 前缀像、但不是
            " https://example.invalid/",  # 前导空格：字符串比较不认
        ]
        for url in bad:
            with self.subTest(url=url):
                self.state.unlink(missing_ok=True)
                self.calls.clear()
                code, _, err = self.run_probe("--url", url, "--fails", "1")
                self.assertEqual(code, 1, f"{url!r} 没被判成挂")
                self.assertIn("只支持 http/https", err)
                self.assertEqual(self.calls, [], f"{url!r} 真的被打开了 —— 白名单没挡住")

    def test_uppercase_scheme_is_still_http(self):
        """`HTTPS://…` 是合法写法（代码里 `.lower()` 之后再比）。

        判成挂的话，配置里大写一个字母就变成一条永远在报的假警 —— 而且原因写的是
        「只支持 http/https」，读的人会以为自己配的就是别的协议。
        """
        url = "HTTPS://mlflow.example.invalid/"
        self.answers[url] = 200
        code, out, _ = self.run_probe("--url", url, "--fails", "1")
        self.assertEqual(code, 0)
        self.assertIn("HTTP 200", out)
        self.assertEqual([c[0] for c in self.calls], [url])

    def test_the_timeout_reaches_the_opener(self):
        """超时值要真传下去。没传的话整机失联时这条命令会挂在 socket 默认超时上
        （可以是几分钟），`TimeoutStartSec` 到点把单元杀掉 —— 于是收到的是
        「单元异常退出」而不是「地址打不开」，两者的排查方向差得很远。
        """
        self.answers[self.URL] = 200
        self.run_probe("--url", self.URL, "--timeout", "2.5")
        self.assertEqual(self.calls[0][1], 2.5)

    def test_the_request_is_a_plain_get(self):
        """探活只能是 GET：HEAD 有站点不认（405 还算好的，有的直接不理），
        而带 body 的方法可能真改到对面的东西 —— 探活不该有副作用。"""
        self.answers[self.URL] = 200
        self.run_probe("--url", self.URL, "--fails", "1")
        self.assertEqual(self.requests[0].get_method(), "GET")


class OpenerShapeTests(unittest.TestCase):
    """④ 真 `_probe_opener()` 的结构。**不发任何请求**，只看装了哪些 handler。

    `build_opener()` 会把 FTP/File/Data/Unknown 那几个 handler 装回来 —— 谁哪天
    图省事改成它，这组会当场响。审计实测过后果：被探机回
    `302 Location: ftp://127.0.0.1:<port>/`，面板真的去连了那个内网端口。
    """

    def setUp(self):
        self.handlers = {type(h).__name__ for h in cli._probe_opener().handlers}

    def test_only_http_handlers_are_installed(self):
        for forbidden in ("FTPHandler", "FileHandler", "DataHandler", "UnknownHandler"):
            self.assertNotIn(forbidden, self.handlers, f"{forbidden} 被装回来了")
        self.assertIn("HTTPHandler", self.handlers)
        self.assertIn("HTTPSHandler", self.handlers)

    def test_errors_keep_their_status_code(self):
        """没有 `HTTPDefaultErrorHandler` 的话非 2xx 会让 `open()` 返回 None ——
        判据还对（都算活着），但日志里只剩「HTTP ?」，出事时分不清 403 和 502。"""
        self.assertIn("HTTPDefaultErrorHandler", self.handlers)
        self.assertIn("HTTPErrorProcessor", self.handlers)

    def test_redirects_are_not_followed(self):
        """重定向处理器要在（否则 urllib 自带的那个会跟随），且它一律拒绝跟随。

        跟随对「这台机还在不在」这个判据**没有任何好处**（回了 302 就已经证明活着），
        坏处却很实在：被探机（或能改它响应的人）可以把面板指到任意地址 ——
        指到必挂的地址 = 造假警，指到活地址 = 掩盖自己已经不行了。
        """
        redirectors = [h for h in cli._probe_opener().handlers if hasattr(h, "redirect_request")]
        self.assertTrue(redirectors, "一个重定向处理器都没有")
        for handler in redirectors:
            self.assertIsNone(
                handler.redirect_request(None, None, 302, "Found", {}, "ftp://127.0.0.1:21/"),
                "这个重定向处理器会跟随 —— SSRF 那条路又通了",
            )

    def test_unknown_protocols_have_no_handler_at_all(self):
        """结构那一道：`ftp:` / `file:` 连个能处理它的 handler 都没有。

        注意 `open()` 这时返回的是 **None**，不是抛错 —— 所以调用处那道判串
        **不能**因为「结构上已经拦住了」而删掉（删过一次，后果见
        `VerdictTests::test_an_opener_that_returns_none_is_down_not_alive`）。
        """
        opener = cli._probe_opener()
        for url in ("ftp://127.0.0.1:1/x", "file:///etc/passwd"):
            with self.subTest(url=url):
                # noqa: S310 —— 这里**故意**造一个非 http 的 Request，
                # 要的就是「opener 根本不认它」这个结论；没有任何东西会被打开
                req = urllib.request.Request(url, method="GET")  # noqa: S310
                self.assertIsNone(opener.open(req, timeout=1))


class LocalServerTests(ProbeBase):
    """不打桩，走一遍真的 urllib —— 在 127.0.0.1 上。

    上面那些 4xx/5xx/3xx 用的是我手搓的响应；万一 urllib 在某个版本上换了抛法，
    那些会继续绿而线上开始误报。这组让判据面对真实的 urllib：**端到端才算数。**

    绑 127.0.0.1、端口 0（内核分配）：不出本机，也不会和别的东西抢端口。
    """

    PATCH_OPENER = False  # 这组要真的 opener

    seen = []  # 服务端收到的请求路径（类属性，handler 里拿得到）

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 —— BaseHTTPRequestHandler 的命名约定
            LocalServerTests.seen.append(self.path)
            path = self.path.strip("/")
            if path.startswith("redirect-to-"):
                self.send_response(302)
                self.send_header("Location", "/" + path[len("redirect-to-") :])
                self.end_headers()
                return
            if path.startswith("redirect-ftp-"):
                self.send_response(302)
                self.send_header("Location", f"ftp://127.0.0.1:{path.rsplit('-', 1)[1]}/x")
                self.end_headers()
                return
            try:
                code = int(path or "200")
            except ValueError:
                code = 200
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # 别把请求日志刷进测试输出
            pass

    def setUp(self):
        super().setUp()
        type(self).seen = []
        self.srv = HTTPServer(("127.0.0.1", 0), self._Handler)
        thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def probe_url(self, path, *extra):
        return self.run_probe(
            "--url", f"{self.base}/{path}", "--fails", "1", "--timeout", "5", *extra
        )

    def test_real_2xx_4xx_5xx_all_count_as_alive(self):
        for status in (200, 403, 404, 500):
            with self.subTest(status=status):
                self.state.unlink(missing_ok=True)
                code, out, _ = self.probe_url(str(status))
                self.assertEqual(code, 0, f"真的 HTTP {status} 被判成挂了")
                self.assertIn(f"HTTP {status}", out)

    def test_a_real_redirect_is_alive_and_not_followed(self):
        """真 302：算活着，而且**只发出一个请求**。

        跟随的话服务端会看到两条路径 —— 那就意味着「去哪」由被探方说了算。
        """
        code, out, _ = self.probe_url("redirect-to-200")
        self.assertEqual(code, 0)
        self.assertIn("HTTP 302", out)
        self.assertEqual(self.seen, ["/redirect-to-200"], f"重定向被跟随了：{self.seen}")

    def test_a_redirect_to_another_protocol_is_not_followed(self):
        """审计实测过的 SSRF 复现：`302 Location: ftp://127.0.0.1:<内网端口>/`。

        这里起一个真的监听 socket 当靶子，探完之后确认**没有任何连接进来**。
        断言「没连上」而不是「没报错」：跟随 ftp 的话探活照样退 0，
        从外面看一切正常 —— 唯一的痕迹就是那个端口上多了一条连接。
        """
        target = socket.socket()
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        self.addCleanup(target.close)
        port = target.getsockname()[1]

        code, out, _ = self.probe_url(f"redirect-ftp-{port}")
        self.assertEqual(code, 0, "收到 302 就说明对面活着")
        self.assertIn("HTTP 302", out)
        target.settimeout(0.5)
        with self.assertRaises((TimeoutError, socket.timeout), msg="面板真的去连那个端口了"):
            target.accept()

    def test_a_closed_port_counts_as_down(self):
        """服务停了 = 端口拒连 —— 这是真挂。用一个刚关掉的端口，不出本机。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        code, _, err = self.run_probe(
            "--url", f"http://127.0.0.1:{dead}/", "--fails", "1", "--timeout", "5"
        )
        self.assertEqual(code, 1)
        self.assertIn("✗", err)

    def test_the_proxy_environment_is_ignored(self):
        """`http_proxy` 指到一个死端口，探活照样成功 —— 说明它不走代理。

        走代理的话这条命令就从「目标机还活着吗」变成了「代理还活着吗」：
        目标机挂了也可能照样 200。**方向是漏报**，而漏报正是这条告警唯一要防的事。
        """
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead = probe.getsockname()[1]
        probe.close()
        os.environ["http_proxy"] = f"http://127.0.0.1:{dead}"
        os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{dead}"
        code, out, _ = self.probe_url("200")
        self.assertEqual(code, 0, "探活被环境变量里的代理劫持了")
        self.assertIn("HTTP 200", out)


class StateFileTests(ProbeBase):
    """③ 状态文件。**读**坏了不许因此报警，**写**不下去必须吵。

    两边不对称是有道理的：
      · 读坏了 → 这一轮当「没记过」，最多晚几分钟报 —— 而拿它去报警的话，
        正文写的是「N 个地址连续打不开」，收到的人会去查一台根本没事的机器，
        查不出所以然，然后学会忽略这条告警。
      · 写不下去 → 计数永远从 0 开始 → **永远到不了阈值 → 这条告警永久静音**，
        而它守的恰恰是「没人会主动发现」的故障。静默失效的探针比没有探针更糟：
        它让人以为有人在看着。
    """

    A = "https://a.example.invalid/"
    B = "https://b.example.invalid/"

    def star_names_the_state_file(self, err: str) -> bool:
        """★ 那几行里有没有点名状态文件（= 这一轮是「计数记不住」才报的）。

        判在 ★ 行上，而不是整段 stderr：`_save_alert_state` 自己也会抱怨一行、
        也带文件名，拿整段找的话那行就能把断言喂饱，而 ★ 里的线索删掉也不会红。
        """
        return any(self.state.name in ln for ln in err.splitlines() if ln.startswith("★"))

    def make_readonly_state(self, name="ro"):
        ro = self.work / name
        ro.mkdir()
        self.state = ro / "probe-state.json"
        ro.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(ro.chmod, stat.S_IRWXU)
        return ro

    def test_a_missing_state_file_is_fine(self):
        self.answers[self.A] = 200
        code, out, _ = self.run_probe("--url", self.A)
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        self.assertTrue(self.state.exists(), "第一次跑要把状态文件建出来，否则永远数不到 2")

    def test_the_state_file_holds_the_counts_directly(self):
        """文件内容就是 `{url: 次数}`，没有外层键。

        锁形状是因为它是**跨进程**约定：升级时新代码读的是旧文件。换形状而不兼容，
        升级那一刻所有连号归零 —— 一次静默的「承诺失效」，而且没人看得见。
        """
        self.answers[self.A] = OSError("refused")
        self.run_probe("--url", self.A)
        self.assertEqual(self.counts(), {self.A: 1})

    def test_a_missing_parent_directory_is_created(self):
        """`StateDirectory=delivery` 由 systemd 建；手工跑或首次部署时目录可能还不在。"""
        self.state = self.work / "nested" / "dir" / "probe-state.json"
        self.answers[self.A] = OSError("refused")
        code, _, _ = self.run_probe("--url", self.A)
        self.assertEqual(code, 0)
        self.assertEqual(self.counts()[self.A], 1)

    def test_broken_json_does_not_page(self):
        """坏 JSON：当成「没记过」接着探，**不因此报警**，并把文件修回可用的样子。"""
        self.state.write_text("{这不是 JSON", encoding="utf-8")
        self.answers[self.A] = OSError("refused")
        code, _, err = self.run_probe("--url", self.A)
        self.assertEqual(code, 0, "状态文件坏了把它自己报成了故障")
        self.assertIn("连续第 1 次", err)
        self.assertEqual(self.counts()[self.A], 1, "状态文件没被修回可用的形状")

    def test_broken_json_still_lets_a_real_outage_page(self):
        """对照组：状态坏了也不能把真故障咽掉（`--fails 1` 时这次就该报）。

        没有这条的话，上面那条用例被「干脆永远退 0」实现掉也一样绿。
        """
        self.state.write_text("nonsense", encoding="utf-8")
        self.answers[self.A] = OSError("refused")
        code, _, err = self.run_probe("--url", self.A, "--fails", "1")
        self.assertEqual(code, 1)
        self.assertIn("★", err)

    @unittest.skipIf(os.geteuid() == 0, "root 写得进只读目录，这条测不到")
    def test_a_write_failure_pages_instead_of_going_silent(self):
        """写不下去 + 有地址挂着 → **退 1**，并说清「不等连号直接报」。

        这是 High-1：计数落不了盘等于阈值永远到不了。宁可早报一次
        （最多是一次误报，人去看一眼就知道），也不能让这条告警无声无息地关掉。
        """
        self.make_readonly_state()
        self.answers[self.A] = OSError("refused")
        code, _, err = self.run_probe("--url", self.A, "--fails", "1")
        self.assertEqual(code, 1)
        self.assertIn("★", err)
        # **要落在 ★ 那几行上。** ★ 是告警正文点名让人去看的东西（「journal 最后几行
        # 那个 ★」），线索写在别的行上等于没写。只在整段 stderr 里找文件名的话，
        # `_save_alert_state` 自己那行抱怨就能把断言喂饱 —— 实测过，那样 ★ 里的
        # 文件名被删掉用例照样绿。
        self.assertTrue(
            self.star_names_the_state_file(err),
            f"★ 没说清这次是「计数记不住」才报的（哪个文件），人会去查错方向：{err}",
        )

    @unittest.skipIf(os.geteuid() == 0, "root 写得进只读目录，这条测不到")
    def test_a_write_failure_pages_at_the_default_threshold_too(self):
        """**这条才是「写失败要吵」对线上成立的证据** —— 用默认 `--fails 3` 验。

        判据必须看「本轮有地址挂着」（`down`），不能看「够连号了」（`bad`）：
        计数写不下去时 `n` 每轮恒为 1，而 `bad` 要连挂够 `--fails` 才有东西 ——
        **线上单元里写死的就是 `--fails 3`**，拿 `bad` 判的话这个分支永远进不去，
        「写失败要吵」对生产等于没做，一台真整机失联的机器每 5 分钟安静地退 0。

        这个坑当初就是被「拿 `--fails 1` 验」盖住的：那条路上 `bad` 和 `down` 恰好等价，
        修复看起来是好的。**默认值和验证用的值不一样时，要用默认值再验一遍。**
        上面那条 `--fails 1` 的用例保留，但它一个人证明不了这件事。
        """
        self.make_readonly_state("ro-default")
        self.answers[self.A] = OSError("refused")
        for _ in range(3):
            code, _, err = self.run_probe("--url", self.A)  # 默认 --fails 3
            self.assertEqual(code, 1, f"写不下去却安静退 0：{err}")
            self.assertTrue(self.star_names_the_state_file(err), err)
            self.assertIn("连续第 1 次", err, "写不下去时连号本来就攒不起来 —— 正是要吵的理由")

    @unittest.skipIf(os.geteuid() == 0, "root 写得进只读目录，这条测不到")
    def test_a_write_failure_alone_does_not_page(self):
        """全都活着、只是状态写不下去 → **不报警**。

        这条是上面两条的边界：写失败本身不是故障告警的理由（没有连号要记，
        下一轮从 0 开始也没害处）。拿它报警的话，一个权限问题会变成每 5 分钟一条
        「探活失败」，而所有机器都好好的 —— 又一条会被学会忽略的告警。
        """
        self.make_readonly_state("ro-ok")
        self.answers[self.A] = 200
        code, out, err = self.run_probe("--url", self.A, "--fails", "1")
        self.assertEqual(code, 0)
        self.assertIn("✓", out)
        # **按「线索」断言，不按措辞断言。** 这一行存在的意义是让人知道
        # 「是哪个文件写不下去、为什么」—— 锁文件名 + 异常类型，措辞怎么改都还成立；
        # 锁整句的话，措辞一改用例就红，而它其实什么都没退化（这条就这么红过一次）。
        self.assertIn(self.state.name, err, "没说是哪个文件写不下去，人会去翻错的那个")
        self.assertIn("PermissionError", err, "没说为什么，只知道「写不下去」帮不上忙")

    def test_each_url_counts_on_its_own(self):
        """一个挂了不影响另一个的计数 —— 合并计数的话，两台各抖一次就凑出一条假警。"""
        self.answers[self.A] = OSError("refused")
        self.answers[self.B] = 200
        for _ in range(3):
            code, _, _ = self.run_probe("--url", self.A, "--url", self.B)
        self.assertEqual(code, 1)
        counts = self.counts()
        self.assertEqual(counts[self.A], 3)
        self.assertEqual(counts[self.B], 0)

    def test_only_the_url_that_is_down_shows_up_in_the_alert(self):
        """报警正文里只列真打不开的那个。列多了人会去查没事的机器。"""
        self.answers[self.A] = OSError("refused")
        self.answers[self.B] = _http_error(self.B, 403)
        code, _, err = self.run_probe("--url", self.A, "--url", self.B, "--fails", "1")
        self.assertEqual(code, 1)
        star = next(ln for ln in err.splitlines() if ln.startswith("★"))
        self.assertIn(self.A, star)
        self.assertNotIn(self.B, star)
        self.assertIn("1 个地址", star)

    def test_a_repeated_address_counts_once_per_round(self):
        """同一地址写两遍（`panel.env` 手写，重复一行很正常）只算一次。

        不去重的话「连挂 3 次」变成「连挂 2 轮」—— 阈值被悄悄改小了，
        而配置看起来完全正常。
        """
        self.answers[self.A] = OSError("refused")
        code, _, err = self.run_probe("--url", self.A, "--url", self.A)
        self.assertEqual(code, 0, "一轮里同一个地址被记了两次")
        self.assertIn("连续第 1 次", err)
        self.assertEqual(self.counts(), {self.A: 1})
        self.assertEqual(len(self.calls), 1, "同一个地址一轮探了两遍")

    def test_addresses_removed_from_the_config_do_not_linger(self):
        """地址从配置里删掉后，它的旧计数不许留在文件里。

        留着的话：一个月后把它加回来，第一次失败就直接报警（旧连号还在）——
        「连续 3 次才报」这个承诺静悄悄失效一次，而且是在没人预期的时候。
        """
        self.answers[self.A] = OSError("refused")
        self.answers[self.B] = OSError("refused")
        self.run_probe("--url", self.A, "--url", self.B)
        self.run_probe("--url", self.A, "--url", self.B)
        self.assertEqual(self.counts(), {self.A: 2, self.B: 2})

        code, _, err = self.run_probe("--url", self.A)  # B 从配置里去掉了
        self.assertEqual(self.counts(), {self.A: 3}, "B 的陈旧计数留在文件里了")
        self.assertEqual(code, 1)
        self.assertNotIn(self.B, err)


class MalformedStateShapeTests(ProbeBase):
    """③ 续：状态文件是**合法 JSON 但形状不对**（手工编辑、版本回滚、旧结构）。

    这里全都必须「当 0，接着探」。抛出去的话 `main()` 只 catch DeliveryError，
    一个 `int("abc")` 就带着 traceback 退非零 → systemd 判失败 → `OnFailure` 私聊
    「外部探活失败」—— **告警由「状态文件被写歪了」触发，正文指向一台没事的机器。**
    """

    URL = "https://mlflow.example.invalid/"

    def setUp(self):
        super().setUp()
        self.answers[self.URL] = 200

    def test_malformed_shapes_are_treated_as_zero(self):
        for shape in (
            {self.URL: "abc"},  # 计数不是数字
            {self.URL: None},
            {self.URL: -5},  # 负数（手改/回滚）—— 负连号比 0 更糟，会拖长静默
            {self.URL: {"nested": 1}},
            {self.URL: [1, 2]},
            {},
            ["not", "a", "dict"],  # 顶层就不是对象
            "just a string",
            42,
        ):
            with self.subTest(shape=shape):
                self.state.write_text(json.dumps(shape), encoding="utf-8")
                code, out, _ = self.run_probe("--url", self.URL)
                self.assertEqual(code, 0, f"形状 {shape!r} 把探活炸了")
                self.assertIn("✓", out)

    def test_a_malformed_count_does_not_fake_a_recovery_note(self):
        """形状不对当 0，也就不该冒出「之前连挂 N 次，已恢复」那句 —— 那是假消息。"""
        self.state.write_text(json.dumps({self.URL: "abc"}), encoding="utf-8")
        _, out, _ = self.run_probe("--url", self.URL)
        self.assertNotIn("已恢复", out)

    def test_a_malformed_count_does_not_page_on_its_own(self):
        """形状不对也不能顺势报警：从 0 重新数，默认阈值下这一轮仍然安静。"""
        self.answers[self.URL] = OSError("refused")
        self.state.write_text(json.dumps({self.URL: {"was": 99}}), encoding="utf-8")
        code, _, err = self.run_probe("--url", self.URL)
        self.assertEqual(code, 0)
        self.assertIn("连续第 1 次", err)


class SeparateStateFilesTests(ProbeBase):
    """③ 续二：探活和 `delivery-unit-failed` **各写各的文件**。

    共用过一版，问题是**丢更新**：探活读状态 → 花十几秒挨个发 HTTP → 再写回去，
    而这中间 `delivery-unit-failed`（sweep 一分钟一轮，比探活勤得多）很可能刚写过
    冷却时间戳 —— 探活一写就把它顶回旧值，冷却失去锚点、开始刷屏。共用没带来任何好处
    （探活不读冷却、告警不读计数）。

    **这组必须是行为级的。** 只按顺序跑一遍、比对键还在，race 全绿也照样存在 ——
    所以这里用 `on_open` 钩子在「HTTP I/O 期间」真的让告警那边写一次盘。
    """

    URL = "https://mlflow.example.invalid/"
    UNIT = "delivery-sweep.service"

    def setUp(self):
        super().setUp()
        self.alert_state = self.work / "alert-state.json"
        self.addCleanup(setattr, cli, "_admin_alert", cli._admin_alert)
        self.sent = []
        cli._admin_alert = lambda title, text, path="": (
            self.sent.append({"title": title, "text": text}),
            "",
        )[1]
        # 钉成「systemd 因 OnFailure 拉起来的真故障」：不钉的话，跑测试的 shell 里有没有
        # INVOCATION_ID 会决定冷却记在 `units[UNIT]` 还是 `units[drill:UNIT]`
        # （见 test_delivery_alert_drill.py），本机绿、CI 红。
        for key in ("MONITOR_EXIT_CODE", "MONITOR_EXIT_STATUS", "MONITOR_SERVICE_RESULT"):
            os.environ.pop(key, None)
        os.environ["MONITOR_UNIT"] = self.UNIT
        os.environ["INVOCATION_ID"] = "probe-separate-state-tests"

    def fail_unit(self, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli.time, "time", lambda: now),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(["unit-failed", "--unit", self.UNIT, "--state", str(self.alert_state)])
        return code, out.getvalue(), err.getvalue()

    @unittest.skipIf(
        os.environ.get("DELIVERY_PROBE_STATE") or os.environ.get("DELIVERY_ALERT_STATE"),
        "环境里覆盖了状态文件路径，这两个常量不是代码默认值，这条测不到",
    )
    def test_the_two_defaults_are_different_files(self):
        """默认路径必须是两个文件。同一个的话，下面那些隔离全是空话。

        两个常量都是 import 期算出来的，`DELIVERY_PROBE_STATE` / `DELIVERY_ALERT_STATE`
        一旦设了就不是「代码默认值」了 —— 那时这条**跳过**，不要写成断言里的 `or`
        （审计 Low-D：恒真的用例看起来是绿的，其实什么也没管）。
        """
        self.assertNotEqual(cli.PROBE_STATE, cli.UNIT_ALERT_STATE)
        self.assertTrue(cli.PROBE_STATE.endswith("probe-state.json"))

    def test_probe_defaults_to_its_own_file(self):
        """不传 `--state` 时落到 `PROBE_STATE`，不是告警那个文件。

        单元文件里没有 `Environment=DELIVERY_PROBE_STATE=`（靠 `$STATE_DIRECTORY`
        推出来），所以线上跑的就是这条默认路径 —— 它指错地方不会有任何报错。
        """
        mine = self.work / "default-probe.json"
        with mock.patch.object(cli, "PROBE_STATE", str(mine)):
            self.answers[self.URL] = OSError("refused")
            code, _, _ = self.run_probe("--url", self.URL, state=False)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(mine.read_text(encoding="utf-8")), {self.URL: 1})
        self.assertFalse(self.alert_state.exists(), "探活写到告警文件里去了")

    def test_probing_leaves_the_alert_file_byte_identical(self):
        self.fail_unit()
        before = self.alert_state.read_bytes()
        self.answers[self.URL] = 200
        self.run_probe("--url", self.URL)
        self.assertEqual(self.alert_state.read_bytes(), before, "探活动了告警冷却的文件")

    def test_a_write_during_the_probe_is_not_lost(self):
        """**丢更新的复现。** 探活正在发 HTTP 的那十几秒里，告警那边写了一次盘。

        探活写自己的文件，所以那次写必须留下 —— 共用一个文件的话，探活会拿着
        十几秒前读到的快照整个写回去，把 `last_fail`/`streak` 顶回旧值。
        """
        self.fail_unit(now=NOW)  # 第一次：真发了，记下 last_alert=NOW
        self.on_open = lambda: self.fail_unit(now=NOW + 60)  # I/O 期间又挂了一轮
        self.answers[self.URL] = 200
        self.run_probe("--url", self.URL)
        self.on_open = None

        rec = json.loads(self.alert_state.read_text(encoding="utf-8"))["units"][self.UNIT]
        self.assertEqual(rec["last_fail"], NOW + 60, "探活把 I/O 期间的那次写顶回去了")
        self.assertEqual(rec["streak"], 2)
        self.assertEqual(rec["last_alert"], NOW, "冷却锚点被动了 → 下一条告警的时机就错了")

    def test_the_cooldown_still_silences_after_a_probe_run(self):
        """行为层面的同一件事：探活跑过之后，第二条单元告警仍然被冷却掉。

        只比对文件内容的话，「内容没变但冷却逻辑被别的方式带坏了」这类漏得掉。
        """
        self.fail_unit(now=NOW)
        self.assertEqual(len(self.sent), 1)
        self.answers[self.URL] = OSError("refused")
        for _ in range(3):
            self.run_probe("--url", self.URL)
        self.fail_unit(now=NOW + 60)
        self.assertEqual(len(self.sent), 1, "探活跑完，6 小时冷却失效了")

    def test_the_unit_alert_keeps_the_probe_counts(self):
        """反过来也一样：`unit-failed` 不能把探活的连号清掉 ——
        清掉的话探活永远数不到 3（sweep 一分钟一轮，比探活勤）。"""
        self.answers[self.URL] = OSError("refused")
        self.run_probe("--url", self.URL)
        self.run_probe("--url", self.URL)
        self.fail_unit()
        code, _, err = self.run_probe("--url", self.URL)
        self.assertEqual(code, 1, "探活的计数被单元告警写掉了")
        self.assertIn("连续第 3 次", err)


class DispatchOrderTests(ProbeBase):
    """探活不能和被它监控的系统共享失败模式（审计 Low-10）。

    `main()` 里 `probe` 的 dispatch 排在 `PlatformRegistry.load()` **之前**。
    排在后面的话：推坏一次部署（platforms/ 没同步、某个描述符 JSON 写崩）→
    `PlatformSpecError` → 退 2 → 而 Med-4 之后那个退出码会被渲染成探活专属文案
    「有地址连续打不开」→ **把人支去查一台好好的机器**，而真正坏掉的是面板自己。

    探活一个平台描述符都不用。它是这台机上唯一「别的都坏了也得照常出声」的东西。
    """

    URL = "https://mlflow.example.invalid/"

    def missing_dir(self) -> str:
        return str(self.work / "no-such-platforms")

    def test_probe_still_runs_when_the_platform_specs_are_broken(self):
        self.answers[self.URL] = 200
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(
                [
                    "--platforms-dir",
                    self.missing_dir(),
                    "probe",
                    "--url",
                    self.URL,
                    "--fails",
                    "1",
                    "--state",
                    str(self.state),
                ]
            )
        self.assertEqual(code, 0, f"平台描述符坏了把探活一起带走了：{err.getvalue()}")
        self.assertIn("HTTP 200", out.getvalue())

    def test_a_broken_platform_dir_still_breaks_everything_else(self):
        """对照组：别把 `PlatformRegistry.load()` 整个删了当成「修好了」。

        其余子命令在描述符坏掉时仍然要退 2（= 工具本身没跑起来），
        那个退出码是流水线用来区分「计划被阻断」和「工具坏了」的。
        """
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["--platforms-dir", self.missing_dir(), "platforms"])
        self.assertEqual(code, 2)

    def test_probe_down_and_broken_specs_still_reads_as_a_probe_failure(self):
        """两件坏事同时发生时，退出码仍然只表达「地址打不开」这一件。

        探活退 1 → 告警文案说「去看那台机」；若它被描述符问题顶成 2，
        文案里那句「退出码 2 = 命令本身没跑起来」就永远对不上号了。
        """
        self.answers[self.URL] = OSError("refused")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(
                [
                    "--platforms-dir",
                    self.missing_dir(),
                    "probe",
                    "--url",
                    self.URL,
                    "--fails",
                    "1",
                    "--state",
                    str(self.state),
                ]
            )
        self.assertEqual(code, 1, "该是 1（地址打不开），2 会被读成「命令本身没跑起来」")


class AlertTextTests(unittest.TestCase):
    """兜底告警的正文要按单元分支（审计 Med-4）。

    那三条猜测（到期回收 / 审批同步 / 申请单存储）是 sweep 专用的。探活失败时照样贴
    上去的话，收到的人会去翻这三样，而真相是某台机打不开了 —— **而那正是探活存在的
    全部理由**。这条告警自己就是因为「文案替 systemd 猜原因」吃过亏的。
    """

    SWEEP = "delivery-sweep.service"
    PROBE = "delivery-probe.service"

    #: sweep 专用的三条猜测，原文取自 `cli._cmd_unit_failed`
    SWEEP_ONLY = ("到期回收", "审批同步", "申请单存储")

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.state = self.work / "alert-state.json"
        self.addCleanup(setattr, cli, "_admin_alert", cli._admin_alert)
        self.sent = []
        cli._admin_alert = lambda title, text, path="": (
            self.sent.append({"title": title, "text": text}),
            "",
        )[1]
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("MONITOR_EXIT_CODE", "MONITOR_EXIT_STATUS", "MONITOR_SERVICE_RESULT"):
            os.environ.pop(key, None)
        os.environ["INVOCATION_ID"] = "probe-alert-text-tests"

    def alert_for(self, unit):
        os.environ["MONITOR_UNIT"] = unit
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli.time, "time", lambda: NOW),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            cli.main(["unit-failed", "--unit", unit, "--state", str(self.state)])
        return self.sent[-1]["text"]

    def test_the_probe_alert_points_at_the_probed_host(self):
        text = self.alert_for(self.PROBE)
        self.assertIn("★", text, "要告诉人去 journal 里看那个 ★（哪个地址挂了）")
        self.assertIn("DELIVERY_PROBE_URLS", text, "也可能是压根没配地址，这条最容易忘")
        for word in self.SWEEP_ONLY:
            self.assertNotIn(word, text, f"探活告警里贴了 sweep 专用的「{word}」")

    def test_the_probe_alert_separates_exit_2_from_a_real_outage(self):
        """正文要把「退出码 2 = 命令自己没跑起来」单列。

        1 和 2 指向两个**完全相反**的方向：1 = 去那台被探的机器看，
        2 = 面板这边的参数/环境坏了，被探的机器多半好好的。不分的话，
        一次配置事故会被当成一次机房故障去查 —— 而这正是 Med-4 要消灭的那类误导。
        """
        # 只锁「退出码 2」这个不会漂的词，不锁整句措辞（Low-B 那两条就是这么红的）
        self.assertIn("退出码 2", self.alert_for(self.PROBE))

    def test_the_sweep_alert_keeps_its_own_guesses(self):
        """对照组：别把 sweep 的正文一起改没了。"""
        text = self.alert_for(self.SWEEP)
        for word in self.SWEEP_ONLY:
            self.assertIn(word, text)
        self.assertNotIn("DELIVERY_PROBE_URLS", text)

    def test_neither_alert_suggests_success_exit_status(self):
        """**回归锁**：别在告警正文里教人加 `SuccessExitStatus=` —— 照做就把兜底告警
        自己关掉了（探活那条更致命：非零是它唯一的出口）。"""
        for unit in (self.PROBE, self.SWEEP):
            with self.subTest(unit=unit):
                self.assertNotIn("SuccessExitStatus", self.alert_for(unit))


class RecoveryTests(ProbeBase):
    """⑤ 恢复提示。只报挂、不报好，人就不知道该不该继续处理 —— 于是每次都得自己去点一下。"""

    URL = "https://mlflow.example.invalid/"

    def test_recovery_says_how_long_it_was_down(self):
        self.answers[self.URL] = OSError("refused")
        self.run_probe("--url", self.URL)
        self.run_probe("--url", self.URL)
        self.answers[self.URL] = 200
        code, out, _ = self.run_probe("--url", self.URL)
        self.assertEqual(code, 0)
        self.assertIn("已恢复", out)
        self.assertIn("2 次", out, "要说清之前连挂了几次，否则看不出是抖了一下还是挂了半天")

    def test_a_url_that_was_never_down_says_nothing_extra(self):
        """从来没挂过就不带那句。每轮都写「已恢复」的话，这句话就不再有信息量。"""
        self.answers[self.URL] = 200
        code, out, _ = self.run_probe("--url", self.URL)
        self.assertEqual(code, 0)
        self.assertNotIn("已恢复", out)

    def test_the_recovery_note_is_not_repeated(self):
        """恢复之后第二次成功不再重复 —— 计数已经清零了。"""
        self.answers[self.URL] = OSError("refused")
        self.run_probe("--url", self.URL)
        self.answers[self.URL] = 200
        self.run_probe("--url", self.URL)
        _, out, _ = self.run_probe("--url", self.URL)
        self.assertNotIn("已恢复", out)

    def test_recovery_after_a_real_alert_also_reports(self):
        """报过警之后恢复，这一句更要在：收到过「打不开」的人在等这句。"""
        self.answers[self.URL] = OSError("refused")
        for _ in range(3):
            self.run_probe("--url", self.URL)
        self.answers[self.URL] = 200
        code, out, _ = self.run_probe("--url", self.URL)
        self.assertEqual(code, 0)
        self.assertIn("已恢复", out)
        self.assertIn("3 次", out)


class ArgumentTests(ProbeBase):
    """⑤ 地址从哪来。"""

    A = "https://a.example.invalid/"
    B = "https://b.example.invalid/"

    def test_no_urls_at_all_is_an_error_not_a_clean_pass(self):
        """一个地址都没有 → 退 1 并说清原因。

        **不能静默退 0**：那等于「探活没跑」却每 5 分钟报一次平安 —— 比没有这条命令
        更糟，因为现在有人以为它在看着。（`panel.env` 里漏配 `DELIVERY_PROBE_URLS`
        是最可能发生的那种漏配：加一个 unit 文件容易，改 env 文件要另一个人动手。）
        """
        code, out, err = self.run_probe()
        self.assertEqual(code, 1)
        self.assertEqual(out, "", "没跑就别往 stdout 写任何像结论的东西")
        self.assertIn("--url", err)
        self.assertIn("DELIVERY_PROBE_URLS", err, "要说清去哪配，否则收到告警的人无从下手")
        self.assertEqual(self.calls, [])

    def test_an_empty_url_argument_is_not_an_address(self):
        """`--url ""`（env 里写了个空值那类）也算「没有地址」，不是「探了个空地址」。"""
        code, _, err = self.run_probe("--url", "")
        self.assertEqual(code, 1)
        self.assertIn("DELIVERY_PROBE_URLS", err)
        self.assertEqual(self.calls, [])

    def test_the_env_var_is_used_when_no_flag_is_given(self):
        os.environ["DELIVERY_PROBE_URLS"] = f"{self.A},{self.B}"
        self.answers = {self.A: 200, self.B: 200}
        code, out, _ = self.run_probe()
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.calls], [self.A, self.B])
        self.assertEqual(out.count("✓"), 2)

    def test_the_flag_wins_over_the_env_var(self):
        """`--url` 优先：线上正报着警的时候，手工跑一条只探某个地址得能绕开 env。"""
        os.environ["DELIVERY_PROBE_URLS"] = self.B
        self.answers[self.A] = 200
        code, _, _ = self.run_probe("--url", self.A)
        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in self.calls], [self.A])

    def test_env_separators(self):
        """逗号 / 空格 / 换行 / 混合 / 首尾多余分隔符 —— 都得解析成同样两个地址。

        `panel.env` 是人手写的：多一个逗号、换行对齐、行尾一个空格都很常见。
        多切出一个空串再去探它，就是一条永远在报的假警。
        """
        for raw in (
            f"{self.A},{self.B}",
            f"{self.A}, {self.B}",
            f"{self.A} {self.B}",
            f"{self.A}\n{self.B}",
            f"{self.A}\t{self.B}",
            f" ,{self.A},,{self.B}, ",
            f"{self.A},\n  {self.B}\n",
        ):
            with self.subTest(raw=raw):
                self.calls.clear()
                self.state.unlink(missing_ok=True)
                os.environ["DELIVERY_PROBE_URLS"] = raw
                self.answers = {self.A: 200, self.B: 200}
                code, _, _ = self.run_probe()
                self.assertEqual(code, 0)
                self.assertEqual([c[0] for c in self.calls], [self.A, self.B])

    def test_an_env_var_of_only_separators_is_no_address(self):
        """`DELIVERY_PROBE_URLS=" , "`（配了但等于没配）要报「没有地址」，不是安静退 0。"""
        os.environ["DELIVERY_PROBE_URLS"] = " , ,\n "
        code, _, err = self.run_probe()
        self.assertEqual(code, 1)
        self.assertIn("DELIVERY_PROBE_URLS", err)
        self.assertEqual(self.calls, [])

    def test_a_repeated_flag_probes_every_address(self):
        self.answers = {self.A: 200, self.B: 200}
        self.run_probe("--url", self.A, "--url", self.B)
        self.assertEqual([c[0] for c in self.calls], [self.A, self.B])


class UnitFileTests(unittest.TestCase):
    """⑥ 单元文件。代码和 unit 是**一对**，拆开任何一半这条告警就静默失效。

    形状照着 `test_delivery_alert_fallback.py::UnitFileTests` /
    `test_delivery_alert_cooldown.py::UnitFileTests` 来（同一批 systemd 坑）。
    """

    ROOT = Path(__file__).resolve().parents[2] / "deploy" / "panel"
    SERVICE = "delivery-probe.service"

    def text(self, name: str) -> str:
        return (self.ROOT / name).read_text(encoding="utf-8")

    @staticmethod
    def directives(text: str, key: str) -> list:
        """某个指令的所有取值。**顶格才算指令** —— 注释里也会提到这些名字。"""
        return [ln.split("=", 1)[1].strip() for ln in text.splitlines() if ln.startswith(f"{key}=")]

    @staticmethod
    def seconds(value: str) -> float:
        """systemd 的时长串 → 秒（只认这个文件里用到的那几种写法）。"""
        value = value.strip()
        for suffix, mult in (("min", 60), ("sec", 1), ("s", 1)):
            if value.endswith(suffix):
                return float(value[: -len(suffix)]) * mult
        return float(value)

    def exec_parts(self) -> list:
        line = self.directives(self.text(self.SERVICE), "ExecStart")
        self.assertTrue(line, "没有 ExecStart")
        return line[0].split()

    def test_failure_is_routed_to_the_fallback_alert(self):
        """`OnFailure=` 必须在 `[Unit]` 段：写进 `[Service]` 段 systemd 直接忽略
        （这个仓库以前就这么栽过，见 fallback 那个文件）。而这条命令**自己不发通知**，
        全靠这一行 —— 漏了它，探活挂了只有 journal 知道，和现在没区别。
        """
        text = self.text(self.SERVICE)
        unit = text.split("\n[Service]\n")[0]  # 按整行切，注释里也会出现段名
        self.assertIn("OnFailure=delivery-unit-failed@%n.service", unit)

    def test_a_nonzero_exit_must_stay_a_failure(self):
        """**回归锁**：这个单元不许有 `SuccessExitStatus=`。

        别的定时任务用它把「跑完了、有问题但已经报了」声明成正常结束；探活相反 ——
        非零就是它唯一的告警出口。谁顺手抄一行 `SuccessExitStatus=1` 过来，
        探活从此永远不报警，而单元一直是绿的、日志一直在打「★」。
        """
        self.assertEqual(self.directives(self.text(self.SERVICE), "SuccessExitStatus"), [])

    def test_it_runs_as_the_panel_user_with_a_state_directory(self):
        """`User=delivery` + `StateDirectory=delivery`。

        `StateDirectory` 少了的话（`ProtectSystem=strict` 之下）连续失败次数**每次都
        写不下去** → 计数永远是 1 → 默认阈值 3 再也到不了。
        用 `StateDirectory=` 而不是 `ReadWritePaths=`：目录由 systemd 建、属主自动是
        `User=`，也就没有「root 手跑一次把属主改掉、之后静默失效」那个老坑（审计 Med-3）。
        """
        text = self.text(self.SERVICE)
        self.assertEqual(self.directives(text, "User"), ["delivery"])
        self.assertIn("delivery", " ".join(self.directives(text, "StateDirectory")).split())
        self.assertEqual(self.directives(text, "Type"), ["oneshot"])
        self.assertIn("ProtectSystem=strict", text)

    @unittest.skipIf(
        os.environ.get("DELIVERY_PROBE_STATE"),
        "环境里设了 DELIVERY_PROBE_STATE，`cli.PROBE_STATE` 不是代码默认值，这条测不到",
    )
    def test_the_state_file_lands_in_the_writable_directory(self):
        """代码默认往哪写、单元给哪块可写，是**一对**。

        `StateDirectory=delivery` 的落点由 systemd 固定为 `/var/lib/delivery`
        （`$STATE_DIRECTORY`）；单元里没有 `Environment=DELIVERY_PROBE_STATE=`，
        所以线上跑的就是代码那个默认值 —— 它指到别处不会有任何报错，
        只是计数永远写不下去，而那等于把这条告警关掉。

        **环境变量那一支走 `skipIf`，不能写成断言里的 `or`**（审计 Low-D）：
        `assertTrue(… or os.environ.get("DELIVERY_PROBE_STATE"))` 在任何设了这个变量的
        环境里**恒真** —— 用例还在、还是绿的，但它对这件事不再有任何约束。
        跳过是看得见的（报告里有一行 skip 和理由），恒真不是。
        """
        state_dir = next(iter(self.directives(self.text(self.SERVICE), "StateDirectory")))
        want = f"/var/lib/{state_dir.split()[0]}/"
        self.assertTrue(
            cli.PROBE_STATE.startswith(want),
            f"探活状态默认落到 {cli.PROBE_STATE}，而单元给的可写目录是 {want}",
        )

    def test_it_does_not_get_write_access_to_identity(self):
        """**回归锁**（审计 Med-3 的同一条）：为了记一个计数，不该拿到 identity/ 的写权限。

        这个单元向公网发请求，是本机最靠外的进程之一；能写 identity/ 就等于能改
        tickets.json / people.json / admins.json。
        """
        self.assertEqual(self.directives(self.text(self.SERVICE), "ReadWritePaths"), [])

    def test_the_unit_threshold_matches_the_code_default(self):
        """unit 里的 `--fails` 和代码默认值是一对：分叉的话，本地验出来的行为不是线上的。"""
        parts = self.exec_parts()
        self.assertIn("probe", parts, "ExecStart 跑的不是 probe 子命令")
        self.assertIn("--fails", parts)
        fails = int(parts[parts.index("--fails") + 1])
        self.assertEqual(fails, cli.build_parser().parse_args(["probe"]).fails)
        self.assertGreaterEqual(fails, 2, "unit 里降到 1 = 一次抖动就私聊，这条告警会变噪音")

    def test_the_start_timeout_fits_a_real_address_list(self):
        """`TimeoutStartSec` 要容得下「地址数 × --timeout」。

        容不下的后果不是「慢」：整机失联时每个地址都要等满 `--timeout`，systemd
        到点把单元杀掉 → **本轮计数一个都不落盘** → 连号永远攒不够 → 告警静音；
        而管理员收到的是「单元异常退出」，指向的方向完全不对。

        按 12 个地址算 —— 面板要盯的自建服务就这个量级。
        （`2min / 10s` 恰好等于 12，是**贴边**：第 12 个地址还没探完就到点了。
        真要稳，`TimeoutStartSec` 得比这个乘积明显大一截。）
        """
        parts = self.exec_parts()
        self.assertIn("--timeout", parts)
        timeout = float(parts[parts.index("--timeout") + 1])
        self.assertLessEqual(timeout, 30, f"单个地址等 {timeout}s 太久")
        start = self.directives(self.text(self.SERVICE), "TimeoutStartSec")
        self.assertTrue(start, "少了 TimeoutStartSec")
        fits = self.seconds(start[0]) / timeout
        self.assertGreaterEqual(fits, 12, f"最多探得完 {fits:.0f} 个地址就会被 systemd 杀掉")

    def test_the_timer_fires_every_five_minutes(self):
        """5 分钟 × `--fails 3` = 连挂约 15 分钟才报（README 里承诺的就是这个）。

        间隔调短不是「更及时」：探活守的是不会自愈的整机故障，早几分钟发现区别不大，
        而探得越勤，「抖一下」凑够连号的概率越高 —— 那才是把它训练成噪音的方式。
        """
        text = self.text("delivery-probe.timer")
        self.assertEqual(self.directives(text, "OnUnitActiveSec"), ["5min"])
        self.assertIn("WantedBy=timers.target", text)
        self.assertTrue(self.directives(text, "OnBootSec"), "少了 OnBootSec：重启后不会自己起")

    def test_the_readme_documents_where_the_addresses_come_from(self):
        """`DELIVERY_PROBE_URLS` 不写进 README = 装了单元但没人知道要配 →
        每 5 分钟退 1 报一次「没有要探的地址」。"""
        self.assertIn("DELIVERY_PROBE_URLS", self.text("README.md"))


if __name__ == "__main__":
    unittest.main()
