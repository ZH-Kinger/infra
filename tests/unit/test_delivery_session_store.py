"""面板登录会话落盘（`server.Store` 的持久化契约）。

这个服务 `Restart=always`、改个启动参数也要重启，所以「重启后会话还在」是每天都要
生效的性质，不是边角料。同时盘上放的是**会话 ID（等同于 cookie 值）**，所以这里的
用例分两路：

  · 存得住 —— 重启读得回、过期的不读回、退出登录盘上也没了、`_sweep` 清完要写盘
  · 不惹祸 —— 只写 0600、不留临时文件、PKCE verifier 不落盘、文件坏了服务照样起、
              写盘失败不能把异常甩给正在登录的人

其中三个用例对应首轮补测发现、随后修掉的缺陷（坏文件形状、并发 save、临时文件残留），
保留下来防回归。
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.parse
from dataclasses import fields
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from delivery import server as server_mod
from delivery.cli import _sessions_path, build_parser
from delivery.errors import DeliveryError
from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import _SESSION_TTL, COOKIE_NAME, Store, _Pending, _WebSession

POSIX_ONLY = unittest.skipUnless(os.name == "posix", "文件权限语义仅 POSIX")

#: 每个字段都填非空值：断言时逐字段比，空值等于没测到这个字段。
_USER = {
    "open_id": "ou_ZhangSan",
    "union_id": "on_ZhangSan",
    "name": "张三",
    "email": "zhang.san@wuji.tech",
    "enterprise_email": "zhang.san@ent.wuji.tech",
    "contact_email": "zhangsan@qq.com",
    "user_id": "1234567",
}


def a_user(**over) -> FeishuUser:
    data = dict(_USER)
    data.update(over)
    return FeishuUser(**data)


@contextlib.contextmanager
def captured_stderr():
    """`Store` 的降级路径只打 stderr，不想让它污染测试输出，同时要断言确实打了。"""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


def on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["sessions"]


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        self.path = self.dir / "sessions.json"
        self.addCleanup(self._tmp.cleanup)

    def restart(self) -> Store:
        """新建一个指向同一个文件的 Store —— 服务重启就是这个。"""
        with captured_stderr():
            return Store(str(self.path))

    def assert_user_round_tripped(self, got: FeishuUser, want: FeishuUser, sid=""):
        for f in fields(FeishuUser):
            self.assertTrue(
                getattr(want, f.name),
                f"用例的 {f.name} 是空的：这个字段等于没测到，补进 _USER",
            )
            self.assertEqual(
                getattr(got, f.name),
                getattr(want, f.name),
                f"会话 {sid} 读回来后 FeishuUser.{f.name} 不对（丢字段/写盘漏字段）",
            )

    def files(self) -> list:
        return sorted(p.name for p in self.dir.iterdir())


class RestartTests(_StoreCase):
    def test_session_survives_restart(self):
        store = Store(str(self.path))
        user = a_user()
        store.sessions["sid-1"] = _WebSession(user=user)
        store.save()

        back = self.restart()
        self.assertIn("sid-1", back.sessions, f"重启后会话没了；盘上是 {self.path.read_text()}")
        self.assert_user_round_tripped(back.sessions["sid-1"].user, user, "sid-1")

    def test_created_is_preserved_so_ttl_keeps_counting_from_login(self):
        # created 若被重置成「重启时刻」，8 小时的 TTL 会被每次重启续期
        store = Store(str(self.path))
        born = time.time() - 3600
        store.sessions["sid-1"] = _WebSession(user=a_user(), created=born)
        store.save()
        self.assertAlmostEqual(
            self.restart().sessions["sid-1"].created, born, places=3, msg="created 被重置了"
        )

    def test_many_sessions_survive_together(self):
        store = Store(str(self.path))
        for i in range(5):
            store.sessions[f"sid-{i}"] = _WebSession(user=a_user(open_id=f"ou_{i}"))
        store.save()

        back = self.restart()
        self.assertEqual(sorted(back.sessions), [f"sid-{i}" for i in range(5)])
        self.assertEqual(
            sorted(v.user.open_id for v in back.sessions.values()),
            [f"ou_{i}" for i in range(5)],
            "会话读回来了但张冠李戴（sid ↔ user 对错了）",
        )

    def test_expired_session_is_not_loaded_back(self):
        """文件是 9 小时前写的，那时会话还新鲜；重启时已过 8 小时 TTL，必须当没有。"""
        self.write_raw(created=time.time() - _SESSION_TTL - 60)
        back = self.restart()
        self.assertEqual(back.sessions, {}, "过期会话被读回来了：重启等于给所有人续期")

    def test_session_just_under_ttl_still_loads(self):
        # 边界另一侧：别为了「不读过期的」把还有效的也扔了
        self.write_raw(created=time.time() - _SESSION_TTL + 60)
        self.assertIn("sid-1", self.restart().sessions, "还差 60 秒才过期的会话被丢了")

    def test_expired_sessions_are_not_written_out(self):
        # 过期的留在盘上没意义，还多留一份身份信息
        store = Store(str(self.path))
        store.sessions["stale"] = _WebSession(user=a_user(), created=time.time() - _SESSION_TTL - 1)
        store.sessions["fresh"] = _WebSession(user=a_user())
        store.save()
        self.assertEqual(sorted(on_disk(self.path)), ["fresh"], "过期会话被写进文件了")

    def test_logout_is_persisted(self):
        store = Store(str(self.path))
        store.sessions["sid-1"] = _WebSession(user=a_user())
        store.sessions["sid-2"] = _WebSession(user=a_user(open_id="ou_2"))
        store.save()

        store.sessions.pop("sid-1")
        store.save()

        back = self.restart()
        self.assertNotIn("sid-1", back.sessions, "退出登录的会话重启后又活了（登不掉的后门）")
        self.assertIn("sid-2", back.sessions, "退出登录把别人的会话也清了")

    def test_missing_file_is_empty_and_is_not_created_on_load(self):
        store = Store(str(self.path))
        self.assertEqual(store.sessions, {})
        self.assertEqual(self.files(), [], "只是构造 Store 就建了文件")

    def write_raw(self, *, created: float, sid="sid-1", user=None):
        payload = {"sessions": {sid: {"user": user or dict(_USER), "created": created}}}
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class FilePermissionTests(_StoreCase):
    def _mode(self) -> int:
        return stat.S_IMODE(self.path.stat().st_mode)

    def _saved(self) -> Store:
        store = Store(str(self.path))
        store.sessions["sid-1"] = _WebSession(user=a_user())
        store.save()
        return store

    @POSIX_ONLY
    def test_file_is_0600(self):
        # 盘上是会话 ID，等同于 cookie 值：同机别的账号不能读
        self._saved()
        self.assertEqual(oct(self._mode()), oct(0o600), "会话文件不是 0600")

    @POSIX_ONLY
    def test_rewrite_tightens_an_existing_loose_file(self):
        # O_CREAT 的 mode 对已存在文件不生效，这类实现第二次写会退回宽权限
        self.path.write_text('{"sessions": {}}', encoding="utf-8")
        self.path.chmod(0o644)
        self._saved()
        self.assertEqual(oct(self._mode()), oct(0o600), "覆盖写之后权限退回了宽权限")

    def test_no_temp_file_left_behind(self):
        store = self._saved()
        store.save()
        store.save()
        self.assertEqual(
            self.files(), ["sessions.json"], "目录里有残留：原子写的临时文件没被 replace 掉"
        )


class CorruptFileTests(_StoreCase):
    """文件读不了就当没有会话：大家重新登录一次，但**服务必须起得来**。"""

    GOOD_USER = dict(_USER)

    def _cases(self) -> dict:
        fresh = time.time()
        return {
            "整个文件不是 JSON": "not json",
            "空文件": "",
            "顶层是数组": "[]",
            "顶层是字符串": '"x"',
            "没有 sessions 键": '{"other": 1}',
            "sessions 是 null": '{"sessions": null}',
            "user 缺字段": json.dumps({"sessions": {"s": {"user": {"nope": 1}, "created": fresh}}}),
            "user 是数组": json.dumps({"sessions": {"s": {"user": [1], "created": fresh}}}),
            "user 是 null": json.dumps({"sessions": {"s": {"user": None, "created": fresh}}}),
            "created 不是数字": json.dumps(
                {"sessions": {"s": {"user": self.GOOD_USER, "created": "abc"}}}
            ),
            "created 是对象": json.dumps(
                {"sessions": {"s": {"user": self.GOOD_USER, "created": {}}}}
            ),
            "被截断": json.dumps({"sessions": {"s": {"user": self.GOOD_USER}}})[:-5],
        }

    def test_broken_file_is_never_fatal(self):
        for label, content in self._cases().items():
            with self.subTest(label):
                self.path.write_text(content, encoding="utf-8")
                try:
                    with captured_stderr():
                        store = Store(str(self.path))
                except Exception as exc:  # noqa: BLE001 — 就是要证明它不抛
                    self.fail(f"{label}：Store 构造抛了 {type(exc).__name__}: {exc}，服务起不来")
                self.assertEqual(store.sessions, {}, f"{label}：坏文件竟然解析出了会话")

    def test_broken_file_still_lets_new_logins_persist(self):
        # 坏文件不能变成「从此不落盘」：清掉之后新会话照样要存得住
        self.path.write_text("not json", encoding="utf-8")
        with captured_stderr():
            store = Store(str(self.path))
        store.sessions["sid-1"] = _WebSession(user=a_user())
        store.save()
        self.assertIn("sid-1", self.restart().sessions, "坏文件之后新会话没能落盘")

    def test_load_failure_is_reported_on_stderr(self):
        self.path.write_text("not json", encoding="utf-8")
        with captured_stderr() as err:
            Store(str(self.path))
        self.assertIn("[store]", err.getvalue(), "静默吞掉：运维不会知道会话文件坏了")

    def test_non_mapping_containers_are_not_fatal(self):
        """防回归：`sessions` / 单条记录不是对象时，曾让 `_load` 抛 AttributeError 出构造函数。

        server.py:118 `(items or {}).items()` 与 :120 `row.get(...)` 只防了 `None`。
        `except (OSError, ValueError, TypeError)` 不接 AttributeError →
        `Store(path)` 抛 → `serve()` 起不来，正是这段代码声称要避免的后果。
        修好后删掉本装饰器。
        """
        cases = {
            "sessions 是字符串": '{"sessions": "x"}',
            "sessions 是数组": '{"sessions": [1, 2]}',
            "单条记录是字符串": '{"sessions": {"s": "x"}}',
            "单条记录是数组": '{"sessions": {"s": []}}',
        }
        for label, content in cases.items():
            with self.subTest(label):
                self.path.write_text(content, encoding="utf-8")
                try:
                    with captured_stderr():
                        store = Store(str(self.path))
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{label}：Store 构造抛了 {type(exc).__name__}: {exc}，服务起不来")
                self.assertEqual(store.sessions, {})


class InMemoryOnlyTests(_StoreCase):
    """不给路径时必须与改动前逐字一致：只存内存、不碰磁盘。"""

    def test_path_is_none_and_save_is_a_noop(self):
        cwd = Path.cwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)

        store = Store()
        self.assertIsNone(store.path)
        store.sessions["sid-1"] = _WebSession(user=a_user())
        store.put_pending("state-1", _Pending(verifier="v", redirect_uri="u"))
        store.save()

        self.assertIn("sid-1", store.sessions, "内存里的会话被 save() 弄丢了")
        self.assertEqual(self.files(), [], "没给路径却建了文件")

    def test_empty_string_path_is_memory_only(self):
        # CLI 守卫不通过时传的是 None，但空串同样不该被当成「写到当前目录」
        self.assertIsNone(Store("").path)


class PendingTests(_StoreCase):
    """`pending` 是授权码流程的一次性凭证（PKCE verifier），刻意不落盘。"""

    def test_pending_is_never_written(self):
        store = Store(str(self.path))
        store.put_pending(
            "state-1", _Pending(verifier="VERIFIER-MUST-NOT-HIT-DISK", redirect_uri="u")
        )
        store.sessions["sid-1"] = _WebSession(user=a_user())
        store.save()

        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("VERIFIER-MUST-NOT-HIT-DISK", raw, "PKCE verifier 被写进文件了")
        self.assertNotIn("state-1", raw, "pending 的 state 被写进文件了")

        back = self.restart()
        self.assertEqual(back.pending, {}, "pending 被读回来了：一次性凭证跨重启复活")
        self.assertIsNone(back.take_pending("state-1"))


class SweepTests(_StoreCase):
    def _store_with_one_of_each(self) -> Store:
        store = Store(str(self.path))
        store.sessions["stale"] = _WebSession(user=a_user(), created=time.time() - _SESSION_TTL - 1)
        store.sessions["fresh"] = _WebSession(user=a_user(open_id="ou_fresh"))
        store.save()
        return store

    def test_sweep_persists_the_drop(self):
        store = self._store_with_one_of_each()
        self.path.write_text(  # 先让盘上确实有那条过期的，否则测不出「清完写盘」
            json.dumps(
                {
                    "sessions": {
                        sid: {"user": dict(_USER), "created": v.created}
                        for sid, v in store.sessions.items()
                    }
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(sorted(on_disk(self.path)), ["fresh", "stale"])

        store.take_pending("nothing")  # 公开入口，内部会 _sweep()

        self.assertEqual(sorted(store.sessions), ["fresh"], "内存里没清掉过期会话")
        self.assertEqual(
            sorted(on_disk(self.path)), ["fresh"], "sweep 清了内存但没写盘：重启后过期会话又回来"
        )

    def test_sweep_without_drop_does_not_write(self):
        # 没东西可清还写盘 = 每次请求都在 identity/ 上做一次原子写
        store = Store(str(self.path))
        store.sessions["fresh"] = _WebSession(user=a_user())
        store.save()
        with mock.patch.object(store, "save") as saved:
            store.take_pending("nothing")
        saved.assert_not_called()

    def test_sweep_also_drops_stale_pending(self):
        store = Store(str(self.path))
        store.pending["old"] = _Pending(verifier="v", redirect_uri="u", created=0.0)
        store.put_pending("new", _Pending(verifier="v2", redirect_uri="u"))
        self.assertEqual(sorted(store.pending), ["new"])


class SaveFailureTests(_StoreCase):
    """落盘失败不能影响登录本身：内存里的会话仍然有效，只是重启会丢。"""

    def _one_session_store(self) -> Store:
        store = Store(str(self.path))
        store.sessions["sid-1"] = _WebSession(user=a_user())
        return store

    def test_write_error_is_swallowed_and_memory_survives(self):
        store = self._one_session_store()
        with (
            captured_stderr() as err,
            mock.patch.object(server_mod.json, "dump", side_effect=OSError("ENOSPC")),
        ):
            store.save()  # 不能抛：这行跑在登录请求线程上
        self.assertIn("[store]", err.getvalue(), "写盘失败没有任何告警")
        self.assertIn("sid-1", store.sessions, "写盘失败把内存里的会话也弄丢了")

    def test_missing_parent_directory_is_not_fatal(self):
        store = Store(str(self.dir / "nope" / "sessions.json"))
        store.sessions["sid-1"] = _WebSession(user=a_user())
        with captured_stderr() as err:
            store.save()
        self.assertIn("[store]", err.getvalue())

    @POSIX_ONLY
    def test_unreadable_file_is_not_fatal(self):
        self.path.write_text('{"sessions": {}}', encoding="utf-8")
        self.path.chmod(0o000)
        self.addCleanup(self.path.chmod, 0o600)
        if os.geteuid() == 0:
            self.skipTest("root 无视文件权限")
        with captured_stderr() as err:
            store = Store(str(self.path))
        self.assertEqual(store.sessions, {})
        self.assertIn("[store]", err.getvalue())

    def test_failed_save_leaves_no_temp_file(self):
        """已修复，此用例防回归：`save()` 的 except 分支不删临时文件（server.py:153）。

        典型触发是磁盘满（写/fsync 抛 ENOSPC）：每失败一次就在 identity/ 里留一个
        `.sessions.json.*.tmp`，越满越多，且里头是会话 ID。
        对照 cli.py `_atomic_private_write` 的 except 里有 `unlink(missing_ok=True)`。
        修好后删掉本装饰器。
        """
        store = self._one_session_store()
        with (
            captured_stderr(),
            mock.patch.object(server_mod.json, "dump", side_effect=OSError("ENOSPC")),
        ):
            store.save()
        self.assertEqual(self.files(), [], "写盘失败留下了临时文件")

    def test_login_during_save_does_not_blow_up_the_request(self):
        """已修复，此用例防回归：`save()` 直接遍历 `self.sessions`，被并发登录插入就抛。

        server.py:133-140 的字典推导边遍历 `self.sessions` 边调 `asdict`；
        ThreadingHTTPServer 下另一个请求线程这时写 `store.sessions[sid]` →
        `RuntimeError: dictionary changed size during iteration`。`self._lock`
        拦不住它——handler 里的写入根本不拿这把锁；`except OSError` 也接不住 →
        异常穿到登录/登出请求上（会话其实已经建好了，人却看到 500）。
        修法是先在锁内 `dict(self.sessions)` 取快照再拼 payload。
        这里用注入代替真并发（语义等价、不靠时序）；真并发也能复现：
        3 写 3 存线程跑 3 秒，三个 save 线程全挂在这个 RuntimeError 上。
        修好后删掉本装饰器。
        """
        store = self._one_session_store()
        real_asdict = server_mod.asdict

        def inject(obj):
            store.sessions.setdefault("sid-2", _WebSession(user=a_user(open_id="ou_2")))
            return real_asdict(obj)

        with captured_stderr(), mock.patch.object(server_mod, "asdict", side_effect=inject):
            store.save()

    def test_concurrent_saves_never_leave_a_torn_file(self):
        # 原子写 + 锁要保证的最低限度：任何时刻读到的文件都是完整 JSON
        store = Store(str(self.path))
        for i in range(20):
            store.sessions[f"sid-{i}"] = _WebSession(user=a_user(open_id=f"ou_{i}"))
        errors = []

        def hammer():
            try:
                for _ in range(20):
                    store.save()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], "并发 save() 抛异常了")
        self.assertEqual(sorted(on_disk(self.path)), sorted(store.sessions), "落盘内容不完整")
        self.assertEqual(self.files(), ["sessions.json"], f"并发写留了临时文件：{self.files()}")


class CliSessionsPathTests(unittest.TestCase):
    """`--sessions` 守卫：会话 ID 等同登录凭证，只允许落在 identity/ 下。

    不合规**不是致命错误**——打一行提示、退回只存内存，服务照常起。
    """

    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "identity").mkdir()
        os.chdir(self.root)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)

    def call(self, value):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = _sessions_path(argparse.Namespace(sessions=value))
        return got, out.getvalue()

    def test_inside_identity_is_accepted_verbatim(self):
        for value in ("identity/sessions.json", str(self.root / "identity" / "sub" / "s.json")):
            with self.subTest(value):
                got, printed = self.call(value)
                self.assertEqual(got, value, "identity/ 下的路径被守卫拒了，等于落盘功能不可用")
                self.assertEqual(printed, "", "正常路径不该打提示")

    def test_outside_identity_falls_back_to_memory(self):
        cases = {
            "仓库根": "sessions.json",
            "同级目录": "elsewhere/sessions.json",
            "绝对路径": "/tmp/sessions.json",  # noqa: S108
            "穿越回上层": "identity/../sessions.json",
            "家目录": "~/sessions.json",
        }
        for label, value in cases.items():
            with self.subTest(label):
                got, printed = self.call(value)
                self.assertIsNone(got, f"{label}：{value} 过了守卫，会话 ID 会写到 identity/ 之外")
                self.assertIn("会话不落盘", printed, f"{label}：静默退回内存，没人会发现")

    def test_symlink_escaping_identity_is_rejected(self):
        link = self.root / "identity" / "sessions.json"
        link.symlink_to(self.root / "outside.json")
        got, printed = self.call("identity/sessions.json")
        self.assertIsNone(got, "符号链接指到 identity/ 外却过了守卫")
        self.assertIn("会话不落盘", printed)

    def test_no_argument_means_memory_only(self):
        for value in (None, ""):
            with self.subTest(repr(value)):
                got, printed = self.call(value)
                self.assertIsNone(got)
                self.assertEqual(printed, "", "没给 --sessions 不该打任何提示")
        self.assertIsNone(_sessions_path(argparse.Namespace()), "没有 sessions 属性时应当返回 None")

    def test_guard_failure_is_not_fatal(self):
        # 守卫抛 DeliveryError 必须被吃掉：服务不能因为路径写错就起不来
        boom = mock.patch("delivery.cli._require_identity_dir", side_effect=DeliveryError("坏"))
        with boom:
            got, printed = self.call("identity/sessions.json")
        self.assertIsNone(got)
        self.assertIn("坏", printed)

    def test_default_is_none(self):
        self.assertIsNone(build_parser().parse_args(["serve"]).sessions)

    def test_serve_receives_the_guarded_path(self):
        from delivery import cli as cli_mod

        wanted = (("identity/sessions.json", "identity/sessions.json"), ("x.json", None))
        for value, expect in wanted:
            quiet = contextlib.redirect_stdout(io.StringIO())
            with self.subTest(value), mock.patch("delivery.server.serve") as serve, quiet:
                self.assertEqual(cli_mod.main(["serve", "--sessions", value]), 0)
            self.assertEqual(
                serve.call_args.kwargs.get("sessions_path"),
                expect,
                f"--sessions {value} 没有按守卫结果传给 serve()",
            )


class _Panel:
    """起一个真服务器，Store 由调用方给 —— 这样能用同一个文件「重启」一次。"""

    def __init__(self, store, **kw):
        opts = {"app_id": "cli_demo", "app_secret": "s3cret", "base_url": "http://127.0.0.1"}
        opts.update(kw)
        handler = server_mod.make_handler(PlatformRegistry.load(), store, **opts)
        self.store = store
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, *, cookie=""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Cookie": f"{COOKIE_NAME}={cookie}"} if cookie else {})
        resp = conn.getresponse()
        out = (resp.status, dict(resp.getheaders()), resp.read().decode(errors="replace"))
        conn.close()
        return out

    def post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        out = (resp.status, resp.read().decode(errors="replace"))
        conn.close()
        return out


class EndToEndRestartTests(_StoreCase):
    """走真实登录路径写会话，再拿同一个文件新建 Store 当「重启」。

    只桩掉飞书那两个网络调用（`exchange_code` / `fetch_user`），会话的建立、写盘、
    读回、鉴权全是真代码。
    """

    def setUp(self):
        super().setUp()
        self.user = a_user(open_id="ou_e2e", union_id="on_e2e", name="王五")
        patches = [
            mock.patch.object(server_mod, "exchange_code", return_value="tok"),
            mock.patch.object(server_mod, "fetch_user", return_value=self.user),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _browser_login(self, panel) -> str:
        _, headers, _ = panel.get("/auth/login")
        query = urllib.parse.urlsplit(headers["Location"]).query
        state = dict(urllib.parse.parse_qsl(query))["state"]
        status, headers, _ = panel.get(f"/auth/callback?code=the-code&state={state}")
        self.assertEqual(status, 302, "回调没走通，后面的落盘断言就没意义了")
        return headers["Set-Cookie"].split(";")[0].split("=", 1)[1]

    def test_browser_login_survives_restart(self):
        with _Panel(Store(str(self.path))) as panel:
            sid = self._browser_login(panel)

        back = self.restart()
        self.assertIn(sid, back.sessions, f"登录没落盘；盘上是 {self.path.read_text()}")
        self.assert_user_round_tripped(back.sessions[sid].user, self.user, sid)

        with _Panel(back) as panel:  # 重启后的新进程
            status, _, body = panel.get("/api/session", cookie=sid)
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["authenticated"], f"重启后原 cookie 不认了：{data}")
        self.assertEqual(data["union_id"], "on_e2e")
        self.assertEqual(data["name"], "王五")

    def test_cli_exchange_survives_restart(self):
        with _Panel(Store(str(self.path))) as panel:
            status, body = panel.post(
                "/auth/exchange", {"code": "c", "redirect_uri": "http://127.0.0.1/auth/callback"}
            )
            self.assertEqual(status, 200, body)
            token = json.loads(body)["token"]

        back = self.restart()
        self.assertIn(token, back.sessions, "CLI 换取的会话没落盘，重启后 CLI 要重新登录")

    def test_logout_survives_restart(self):
        with _Panel(Store(str(self.path))) as panel:
            sid = self._browser_login(panel)
            self.assertEqual(panel.get("/auth/logout", cookie=sid)[0], 302)

        back = self.restart()
        self.assertNotIn(sid, back.sessions, "退出登录后重启，旧 cookie 又能用了")
        with _Panel(back) as panel:
            _, _, body = panel.get("/api/session", cookie=sid)
        self.assertFalse(json.loads(body)["authenticated"])

    def test_memory_only_panel_loses_sessions_on_restart(self):
        # 不给路径 = 改动前的行为，明确锁住（免得以后有人默认落盘）
        with _Panel(Store()) as panel:
            sid = self._browser_login(panel)
            self.assertTrue(json.loads(panel.get("/api/session", cookie=sid)[2])["authenticated"])
        self.assertEqual(self.files(), [], "没给路径却写了文件")


if __name__ == "__main__":
    unittest.main()
