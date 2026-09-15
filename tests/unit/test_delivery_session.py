"""本地会话/凭证存储与配对码登录。

这两块直接持有令牌与平台密钥，所以测试重点是**权限**与**不泄漏**，
而不只是「存进去能读出来」。
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from delivery.session import (
    Session,
    SessionError,
    bound_platforms,
    clear_session,
    describe_credential,
    drop_credential,
    load_credentials,
    load_session,
    save_credential,
    save_session,
)


class _Home:
    """把 W0_HOME 指到临时目录，避免碰真实 ~/.w0。"""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = os.environ.get("W0_HOME")
        os.environ["W0_HOME"] = str(Path(self.tmp.name) / "w0")
        return Path(os.environ["W0_HOME"])

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("W0_HOME", None)
        else:
            os.environ["W0_HOME"] = self.prev
        self.tmp.cleanup()


def a_session(**over):
    data = {
        "union_id": "on_abc",
        "name": "李四",
        "token": "SESSION-TOKEN-SHOULD-NOT-LEAK",
        "expires_ts": 4102444800.0,
        "server": "http://bot:8088",
    }
    data.update(over)
    return Session(**data)


class SessionStorageTests(unittest.TestCase):
    def test_round_trip(self):
        with _Home():
            save_session(a_session())
            loaded = load_session()
            self.assertEqual(loaded.union_id, "on_abc")
            self.assertFalse(loaded.expired)

    def test_no_session_returns_none(self):
        with _Home():
            self.assertIsNone(load_session())

    @unittest.skipUnless(os.name == "posix", "权限语义仅 POSIX")
    def test_files_are_written_0600(self):
        with _Home() as home:
            save_session(a_session())
            mode = stat.S_IMODE((home / "session.json").stat().st_mode)
            self.assertEqual(mode, 0o600, oct(mode))

    @unittest.skipUnless(os.name == "posix", "权限语义仅 POSIX")
    def test_overwriting_keeps_0600(self):
        # O_CREAT 的 mode 对**已存在**文件不生效，容易在第二次写时退回默认权限
        with _Home() as home:
            save_session(a_session())
            (home / "session.json").chmod(0o644)
            save_session(a_session(name="改名"))
            self.assertEqual(stat.S_IMODE((home / "session.json").stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "权限语义仅 POSIX")
    def test_too_open_file_is_refused_not_silently_used(self):
        # 泰国那台机的 ~/.ossutilconfig 曾经是 664，同机其他账号能读到 AK/SK。
        # 静默容忍等于把这个坑复制到每个人的开发机上。
        with _Home() as home:
            save_session(a_session())
            (home / "session.json").chmod(0o644)
            with self.assertRaises(SessionError) as ctx:
                load_session()
            self.assertIn("chmod", str(ctx.exception))

    def test_redacted_never_contains_the_token(self):
        payload = a_session().redacted()
        self.assertNotIn("token", payload)
        self.assertNotIn("SESSION-TOKEN-SHOULD-NOT-LEAK", str(payload))

    def test_clear_session(self):
        with _Home():
            save_session(a_session())
            self.assertTrue(clear_session())
            self.assertFalse(clear_session())
            self.assertIsNone(load_session())

    def test_corrupt_session_file_is_an_error(self):
        with _Home() as home:
            home.mkdir(parents=True, exist_ok=True)
            home.chmod(0o700)
            path = home / "session.json"
            path.write_text("{not json", encoding="utf-8")
            path.chmod(0o600)
            with self.assertRaises(SessionError):
                load_session()


class CredentialStorageTests(unittest.TestCase):
    def test_save_and_list_bound(self):
        with _Home():
            save_credential("jiuzhang", {"access_key": "ak_abcdef123", "secret_key": "sk_x"})
            self.assertEqual(bound_platforms(), {"jiuzhang"})
            self.assertIn("jiuzhang", load_credentials())

    def test_describe_never_returns_secret_values(self):
        with _Home():
            save_credential(
                "jiuzhang", {"access_key": "ak_abcdef123", "secret_key": "sk_TOPSECRET"}
            )
            info = describe_credential("jiuzhang")
            self.assertNotIn("sk_TOPSECRET", str(info))
            self.assertNotIn("ak_abcdef123", str(info))  # 只回掐头去尾的标识
            self.assertIn("secret_key", info["fields"])  # 字段名可以回

    def test_empty_payload_is_rejected(self):
        with _Home():
            for bad in ({}, None, ""):
                with self.assertRaises(SessionError):
                    save_credential("jiuzhang", bad)

    def test_drop(self):
        with _Home():
            save_credential("jiuzhang", {"access_key": "a"})
            self.assertTrue(drop_credential("jiuzhang"))
            self.assertFalse(drop_credential("jiuzhang"))
            self.assertEqual(bound_platforms(), set())


if __name__ == "__main__":
    unittest.main()
