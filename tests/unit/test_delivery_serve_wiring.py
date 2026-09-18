"""面板启动这条链的接线：`cli.main("serve") → server.serve() → Backend/make_handler`。

**为什么单独有这个文件**：给 `Backend.__init__` 加了一个参数、命令行也传了，唯独漏了
中间那层 `serve()` 的签名 —— 面板起来就 `TypeError: serve() got an unexpected keyword
argument`，systemd 反复重启，线上挂了 4 分钟。而当时全量单测和 lint 全绿：
**没有任何用例调用过 `serve()`**，单测一律直接构造 `Backend`。
被直接构造的那一层测得再细，也测不到「参数是怎么传到它手上的」。

两道防线，都是离线的：

1. **冒烟**：把 `ThreadingHTTPServer` 换成假的，真跑一遍 `cli.main(["serve", ...])`。
   不绑端口、不落盘、不要环境变量，但整条接线是真的走了一遍 —— 任何一层签名对不上
   都会当场 TypeError。这条是主力。
2. **签名比对**（ast 解析调用处，不用正则：多行调用、夹注释都能正确解析）：
   冒烟只走得到「默认参数」那条路径，而 `_request_paths()` 这类助手会因为路径守卫
   少返回几个键；签名比对不受运行时分支影响，把每一个**写在代码里**的关键字参数
   都对一遍。两条互补，缺一条都有盲区。

离线，不联网、不绑端口。
"""

from __future__ import annotations

import ast
import contextlib
import http.server
import inspect
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
PLATFORMS = str(REPO / "platforms")


def call_in(source: Path, callee: str, *, inside: str = ""):
    """源码里对 `callee(...)` 那处调用。`inside` 限定在某个函数定义内。

    返回 `(显式关键字参数名, 形如 **helper(args) 的展开项)`。**找不到就抛** ——
    调用改了写法时这个守卫必须当场坏掉、而不是悄悄退化成一条空断言。
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    scope = tree
    if inside:
        scope = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == inside),
            None,
        )
        assert scope is not None, f"{source.name} 里找不到函数 {inside}()"
    calls = [
        node
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == callee)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == callee)
        )
    ]
    assert len(calls) == 1, f"{source.name} 里 {callee}(...) 找到 {len(calls)} 处，期望 1 处"
    node = calls[0]
    named = [kw.arg for kw in node.keywords if kw.arg]
    spread = [kw.value for kw in node.keywords if kw.arg is None]
    assert named, f"{callee}(...) 一个关键字参数都没抠到，这条守卫失效了"
    return named, spread, node


def accepts(func) -> set:
    params = inspect.signature(func).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        raise AssertionError(f"{func.__name__} 收了 **kwargs，这条守卫挡不住它，得换写法")
    return set(params)


class ServeSignatureTests(unittest.TestCase):
    """一层一层比对：调用处写了什么，被调方收不收。"""

    def test_the_cli_only_passes_what_serve_accepts(self):
        """就是挂过的那一处。少一个参数 = 面板启动即 TypeError，而单测全绿。"""
        from delivery import server

        named, _, _ = call_in(REPO / "src/delivery/cli.py", "serve")
        missing = sorted(set(named) - accepts(server.serve))
        self.assertEqual(
            missing,
            [],
            f"cli.py 给 serve() 传了 {missing}，但 server.serve() 的签名里没有 —— "
            "面板会在启动时 TypeError，systemd 反复重启，而全量单测照样是绿的。",
        )

    def test_the_dict_unpacks_also_only_carry_known_keys(self):
        """`**_review_paths(args)` / `**_request_paths(args)` 里的键一样会撞签名，
        而它们在源码里看不见 —— 只能把助手真调一次，看它返回哪些键。"""
        from delivery import cli, server

        _, spread, _ = call_in(REPO / "src/delivery/cli.py", "serve")
        self.assertTrue(spread, "serve() 调用里没有 ** 展开了，这条守卫可以删")
        args = cli.build_parser().parse_args(["serve"])
        known = accepts(server.serve)
        for node in spread:
            self.assertIsInstance(node, ast.Call, "** 展开的不是一处函数调用，这条守卫要跟着改")
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            helper = getattr(cli, name, None)
            self.assertTrue(callable(helper), f"cli.py 里找不到助手 {name}()")
            with contextlib.redirect_stdout(io.StringIO()):
                keys = helper(args)
            missing = sorted(set(keys) - known)
            self.assertEqual(
                missing, [], f"cli.{name}() 返回的 {missing} 不在 server.serve() 的签名里"
            )

    def test_every_args_attribute_exists_on_the_serve_parser(self):
        """`serve(..., dataset_buckets_path=args.dataset_buckets)` 的另一半：
        参数没在 serve 子命令上定义的话，是 AttributeError，同样是启动即死。"""
        from delivery import cli

        _, _, node = call_in(REPO / "src/delivery/cli.py", "serve")
        used = {
            kw.value.attr
            for kw in node.keywords
            if isinstance(kw.value, ast.Attribute)
            and isinstance(kw.value.value, ast.Name)
            and kw.value.value.id == "args"
        }
        self.assertTrue(used, "没抠到任何 args.X，这条守卫失效了")
        have = set(vars(cli.build_parser().parse_args(["serve"])))
        self.assertEqual(sorted(used - have), [])

    def test_serve_only_passes_what_backend_accepts(self):
        """下一层。这次没断，但成因完全一样，迟早轮到它。"""
        from delivery import server

        named, _, _ = call_in(REPO / "src/delivery/server.py", "Backend", inside="serve")
        missing = sorted(set(named) - accepts(server.Backend.__init__))
        self.assertEqual(
            missing,
            [],
            f"serve() 给 Backend(...) 传了 {missing}，但 Backend.__init__ 不收 —— 同样是启动即死。",
        )

    def test_serve_only_passes_what_make_handler_accepts(self):
        """再下一层，同理。"""
        from delivery import server

        named, _, _ = call_in(REPO / "src/delivery/server.py", "make_handler", inside="serve")
        missing = sorted(set(named) - accepts(server.make_handler))
        self.assertEqual(
            missing,
            [],
            f"serve() 给 make_handler(...) 传了 {missing}，但它的签名里没有。",
        )


class _FakeHttpServer:
    """够 `serve()` 用的假 HTTP 服务器：**不绑端口**，`serve_forever` 立刻返回。

    真绑端口的冒烟测试在 CI 上会因为端口占用/防火墙间歇性红，而这条要天天跑 ——
    间歇性红的用例第二周就会被人加上 skip。
    """

    instances: list = []

    def __init__(self, address, handler):
        self.address, self.handler = address, handler
        _FakeHttpServer.instances.append(self)
        self.closed = False

    def serve_forever(self):
        return None

    def server_close(self):
        self.closed = True


class ServeSmokeTests(unittest.TestCase):
    """真跑一遍 `delivery serve` 的启动路径（不绑端口、不落盘、不要环境变量）。"""

    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "identity").mkdir()
        os.chdir(self.root)
        _FakeHttpServer.instances.clear()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)

    def run_serve(self, *extra):
        from delivery import cli

        buf = io.StringIO()
        with (
            mock.patch.object(http.server, "ThreadingHTTPServer", _FakeHttpServer),
            contextlib.redirect_stdout(buf),
        ):
            rc = cli.main(["--platforms-dir", PLATFORMS, "serve", "--port", "0", *extra])
        return rc, buf.getvalue()

    def test_it_starts_with_nothing_configured(self):
        """空目录 + 全默认参数：这是新部署第一次起面板的样子。
        整条 `cli → serve → Backend → make_handler` 都真走了一遍。"""
        rc, out = self.run_serve()
        self.assertEqual(rc, 0)
        self.assertEqual(len(_FakeHttpServer.instances), 1)
        self.assertTrue(_FakeHttpServer.instances[0].closed, "退出时要 server_close()")
        self.assertIn("Ctrl-C 停止", out)

    def test_it_starts_with_the_hygiene_paths_wired(self):
        """体检这几个路径参数是新加的那批 —— 显式传一遍，别只走默认值。"""
        (self.root / "identity" / "dataset-buckets.json").write_text(
            '{"allowed": ["wuji-sing"]}', encoding="utf-8"
        )
        rc, _ = self.run_serve(
            "--dataset-buckets",
            "identity/dataset-buckets.json",
            "--services",
            "identity/services.json",
            "--stale-days",
            "30",
            "--unused-days",
            "15",
        )
        self.assertEqual(rc, 0)

    def test_it_binds_only_the_loopback_by_default(self):
        """顺带钉住：默认只绑回环。这个看板还没有访问控制，绑 0.0.0.0 等于
        把全员的云账号、AK 台账摊给整个网段。"""
        self.run_serve()
        self.assertEqual(_FakeHttpServer.instances[0].address[0], "127.0.0.1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
