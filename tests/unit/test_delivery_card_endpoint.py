"""`POST /feishu/card` 端到端：真 HTTP、真 handler、真记录文件，只有云和飞书是假的。

这个入口和面板那条路的差别，一句话
─────────────────────────────────
面板：飞书 OAuth 登录 → 会话 cookie → 管理员。
卡片：**没有登录**。飞书在请求体里带一个 `open_id` 过来，仅此而已。
所以「这个请求真是飞书发的」完全靠验签，「点的人是管理员」完全靠拿 open_id 去飞书换
union_id 再比名单。这两道任意一道松掉，伪造一个 `{"a":"del","k":"aliyun/…/someone"}`
就能删掉任何一个在待办里的云账号。

因此每个用例都同时断言两件事：**回了什么** 和 **云上被碰了没有**（`Book.calls` 为空）。

夹具沿用 `test_delivery_offboard_api`（同一套 `_Live` / `Book` / `FakeEx`）。
外部依赖三处被换掉：`delivery.server.executor_from_env`（云）、
`delivery.identity.directory.union_id_of`（open_id→union_id）、
`delivery.server._tenant_token_cache`（拿 tenant_access_token，否则 make_handler 就要联网）。
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from delivery import card_hook as card_hook_mod
from delivery import offboard
from delivery.feishu import FeishuError
from delivery.provision import ProvisionError
from delivery.server import Backend

from .test_delivery_card_hook import KEY, TOKEN, card_body, seal, sign
from .test_delivery_offboard_api import ALI_ACC, VOLC_ACC, Book, FakeEx, _Live, person, ref

ADMIN_OPEN = "ou_admin"
OTHER_OPEN = "ou_zhangsan"
PATH = "/feishu/card"


def raw_post(port: int, raw: bytes, headers: dict, path: str = PATH):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
    finally:
        conn.close()
    try:
        return resp.status, json.loads(data or b"null")
    except ValueError:
        return resp.status, data.decode(errors="replace")


class CardEndpointBase(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        d = self.root / "identity"
        d.mkdir()
        (d / "attrs.json").write_text("{}", encoding="utf-8")
        (d / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), encoding="utf-8")
        (d / "people.json").write_text(
            json.dumps(
                {
                    "schema": "wuji-people@1",
                    "people": [
                        {"union_id": "on_admin", "name": "管理员", "email": "admin@wuji.tech"},
                        {
                            "union_id": "on_1",
                            "name": "李四",
                            "email": "li.si@wuji.tech",
                            "accounts": [
                                {"platform": "aliyun", "account": ALI_ACC, "name": "lisi"},
                                {"platform": "volcano", "account": VOLC_ACC, "name": "LiSi"},
                            ],
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (d / "proposal.json").write_text(
            json.dumps({"domain": "wuji.tech", "people": [], "unlinked": [], "services": []}),
            encoding="utf-8",
        )
        (d / "manual.json").write_text(json.dumps({"links": []}), encoding="utf-8")
        self.dir = d
        self.backend = Backend(
            people_path="identity/people.json",
            bindings_path="identity/bindings.json",
            admins_path="identity/admins.json",
            proposal_path="identity/proposal.json",
            manual_path="identity/manual.json",
            iam_spec_path="identity/attrs.json",
            iam_out_path="identity/iam-attributes.csv",
            platforms={"aliyun": "阿里云", "volcano": "火山引擎", "jiuzhang": "九章"},
        )
        self.path = offboard.path_beside("identity/people.json")
        self.book = Book()

        # 云：全部换成假执行身份，任何一次调用都会被记下来
        p = mock.patch("delivery.server.executor_from_env", side_effect=self.book.factory)
        p.start()
        self.addCleanup(p.stop)
        # 飞书：拿 tenant token（make_handler 里就会建缓存）
        p2 = mock.patch("delivery.server._tenant_token_cache", return_value=lambda: "tenant-tok")
        p2.start()
        self.addCleanup(p2.stop)
        # 飞书：open_id → union_id
        self.directory = {ADMIN_OPEN: "on_admin", OTHER_OPEN: "on_1"}
        self.union_error = None
        self.resolved: list = []
        p3 = mock.patch(
            "delivery.identity.directory.union_id_of", side_effect=self._fake_union_id_of
        )
        p3.start()
        self.addCleanup(p3.stop)

    def _fake_union_id_of(self, open_id, app_id, app_secret, *, get=None, token=""):
        self.resolved.append((open_id, token))
        if self.union_error is not None:
            raise self.union_error
        return self.directory.get(str(open_id), "")

    # ── 夹具 ──

    @contextlib.contextmanager
    def live(self, **env):
        """起一台真服务器。`env` 覆盖回调的两把凭证（不给就是配好的正常状态）。"""
        conf = {card_hook_mod.ENV_ENCRYPT_KEY: KEY, card_hook_mod.ENV_VERIFY_TOKEN: TOKEN}
        conf.update(env)
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, {k: str(v) for k, v in conf.items()}),
            contextlib.redirect_stderr(err),
            _Live(self.backend) as server,
        ):
            server.stderr = err
            yield server

    def post(
        self,
        server,
        body=None,
        *,
        raw=None,
        encrypt=False,
        key=KEY,
        sign_key=None,
        ts=None,
        nonce="nonce-1",
        sig=None,
        drop=(),
    ):
        """签好名发出去。默认一切正常，参数用来逐项破坏。"""
        if raw is None:
            payload = {"encrypt": seal(json.dumps(body).encode(), key)} if encrypt else body
            raw = json.dumps(payload, ensure_ascii=False).encode()
        ts = str(int(time.time())) if ts is None else str(ts)
        head = {
            "Content-Type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": sign(ts, nonce, KEY if sign_key is None else sign_key, raw)
            if sig is None
            else sig,
        }
        for name in drop:
            head.pop(name, None)
        return raw_post(server.port, raw, head)

    def click(self, server, action="del", key="", *, who=ADMIN_OPEN, event_id="ev-1", **kw):
        return self.post(
            server, card_body(open_id=who, value={"a": action, "k": key}, event_id=event_id), **kw
        )

    # ── 记录文件 ──

    def seed_disabled(self, user="lisi", platform="aliyun"):
        offboard.auto_disable(
            self.path,
            [(person("李四", "on_1", ref(platform, user)), "IT 的 IAM 标记离职")],
            lambda p, a: FakeEx(Book(), p, a),
        )
        return offboard.key_of(platform, ALI_ACC if platform == "aliyun" else VOLC_ACC, user)

    def records(self):
        return offboard.load(self.path)

    def state_of(self, key):
        return (self.records().get(key) or {}).get("state")

    def assertNoCloud(self):  # noqa: N802 — 跟 unittest 的命名走
        """一次云调用都没有，连执行身份都没建。"""
        self.assertEqual(self.book.calls, [], f"不该碰云，却调了：{self.book.calls}")

    def assertNoCloudWrites(self):  # noqa: N802
        """没有任何改动云上状态的调用（建执行身份不算 —— `offboard.decide` 分支前就建好了）。"""
        acted = [c for c in self.book.calls if c[0] != "factory"]
        self.assertEqual(acted, [], f"不该动云上的号，却调了：{acted}")


# ── 门：验签 / token / Encrypt Key ───────────────────────────────────────


class GateTests(CardEndpointBase):
    def test_a_correctly_signed_click_gets_through(self):
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "success", body)
        self.assertEqual(self.state_of(key), "deleted")

    def test_a_forged_signature_is_403_and_nothing_happens(self):
        """这是整条路上最重要的一条：外人不知道 Encrypt Key 就签不出来。"""
        key = self.seed_disabled()
        with self.live() as s:
            for i, kw in enumerate(
                (
                    {"sig": "0" * 64},
                    {"sig": ""},
                    {"sign_key": "别人猜的 key"},
                    {"nonce": "n-other", "sig": sign("1", "n", KEY, b"{}")},
                )
            ):
                status, body = self.click(s, "del", key, event_id=f"ev-{i}", **kw)
                self.assertEqual(status, 403, (kw, body))
        self.assertEqual(self.state_of(key), "disabled")
        self.assertEqual(self.resolved, [])  # 连「你是谁」都没去查
        self.assertNoCloud()

    def test_a_tampered_body_is_403(self):
        """截获一次合法点击、把 `k` 换成别人的号再发 —— 签名算的是原始字节，必须挂。"""
        victim = self.seed_disabled()
        self.seed_disabled("wangwu")
        good = json.dumps(card_body(value={"a": "del", "k": victim}), ensure_ascii=False).encode()
        ts = str(int(time.time()))
        head = {
            "Content-Type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": sign(ts, "n", KEY, good),
        }
        evil = good.replace(victim.encode(), offboard.key_of("aliyun", ALI_ACC, "wangwu").encode())
        self.assertNotEqual(evil, good)
        with self.live() as s:
            status, _body = raw_post(s.port, evil, head)
        self.assertEqual(status, 403)
        self.assertNoCloud()

    def test_missing_signature_headers_are_403(self):
        key = self.seed_disabled()
        with self.live() as s:
            for i, drop in enumerate(
                (
                    ("X-Lark-Signature",),
                    ("X-Lark-Request-Timestamp",),
                    ("X-Lark-Request-Nonce",),
                    ("X-Lark-Signature", "X-Lark-Request-Timestamp", "X-Lark-Request-Nonce"),
                )
            ):
                status, _body = self.click(s, "del", key, event_id=f"ev-{i}", drop=drop)
                self.assertEqual(status, 403, drop)
        self.assertNoCloud()

    def test_an_old_but_validly_signed_request_is_403(self):
        """重放：录一个合法请求，过 5 分钟再发。"""
        key = self.seed_disabled()
        old = int(time.time()) - int(card_hook_mod.CLOCK_SKEW) - 5
        with self.live() as s:
            status, _body = self.click(s, "del", key, ts=old)
        self.assertEqual(status, 403)
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_a_wrong_verification_token_is_403(self):
        key = self.seed_disabled()
        with self.live() as s:
            for i, token in enumerate(("", "nope", TOKEN + "x", None)):
                body = card_body(value={"a": "del", "k": key}, token=token, event_id=f"ev-{i}")
                status, _b = self.post(s, body)
                self.assertEqual(status, 403, token)
        self.assertNoCloud()

    def test_without_an_encrypt_key_everything_is_refused(self):
        """**没配 Encrypt Key 就拒绝一切**，不是「先放行等配好」 —— 这个入口能删云账号。"""
        key = self.seed_disabled()
        with self.live(**{card_hook_mod.ENV_ENCRYPT_KEY: ""}) as s:
            status, body = self.click(s, "del", key)
            self.assertEqual(status, 403, body)
            # 连签名都不给算：没有 key 就没有「正确的签名」可言
            status, _b = self.click(s, "del", key, sign_key="")
            self.assertEqual(status, 403)
            status, _b = self.post(
                s, card_body(value={"a": "keep", "k": key}), drop=("X-Lark-Signature",)
            )
            self.assertEqual(status, 403)
        self.assertEqual(self.state_of(key), "disabled")
        self.assertEqual(self.resolved, [])
        self.assertNoCloud()

    def test_challenge_works_before_the_encrypt_key_is_configured(self):
        """先有鸡还是先有蛋：后台填回调地址那一下，这边的 Encrypt Key 还没配。"""
        with self.live(**{card_hook_mod.ENV_ENCRYPT_KEY: ""}) as s:
            status, body = self.post(
                s, {"type": "url_verification", "challenge": "cc-1", "token": "whatever"}
            )
        self.assertEqual((status, body), (200, {"challenge": "cc-1"}))

    def test_challenge_works_without_any_signature_headers(self):
        with self.live(
            **{card_hook_mod.ENV_ENCRYPT_KEY: "", card_hook_mod.ENV_VERIFY_TOKEN: ""}
        ) as s:
            status, body = self.post(
                s,
                {"type": "url_verification", "challenge": "cc-2"},
                drop=("X-Lark-Signature", "X-Lark-Request-Timestamp", "X-Lark-Request-Nonce"),
            )
        self.assertEqual((status, body), (200, {"challenge": "cc-2"}))

    def test_challenge_works_in_encrypted_mode(self):
        """配了 Encrypt Key 之后**连 challenge 那一次也是加密的** ——
        解密要排在 challenge 判断之前，否则回调地址根本存不下来。"""
        with self.live() as s:
            status, body = self.post(
                s, {"type": "url_verification", "challenge": "cc-3"}, encrypt=True
            )
        self.assertEqual((status, body), (200, {"challenge": "cc-3"}))

    def test_an_encrypted_click_is_accepted(self):
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.post(s, card_body(value={"a": "del", "k": key}), encrypt=True)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(key), "deleted")

    def test_a_ciphertext_sealed_with_another_key_never_executes(self):
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.post(
                s, card_body(value={"a": "del", "k": key}), encrypt=True, key="别的 Encrypt Key"
            )
        self.assertIn(status, (400, 403), body)
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_a_malformed_ciphertext_is_a_4xx_not_a_5xx(self):
        """`decrypt` 里 base64 解不开 / 长度不是块的整数倍 / 解出来不是 UTF-8，
        抛的都**不是** `CardError`（是 `binascii.Error` / `ValueError` / `UnicodeDecodeError`）。
        接住它们的是 `_feishu_card` 的兜底 `except Exception` —— 这条锁住那个兜底别被拿掉。"""
        with self.live() as s:
            for blob in ("!!!not base64!!!", "", "eHh4", "A" * 64, "/w==" * 8):
                status, body = self.post(s, raw=json.dumps({"encrypt": blob}).encode())
                self.assertIn(status, (400, 403), (blob, status, body))
        self.assertNoCloud()

    def test_garbage_bodies_do_not_500(self):
        with self.live() as s:
            for raw in (b"{", b"not json", b"[]", b"\xff\xfe", b"<xml/>"):
                status, _body = self.post(s, raw=raw)
                self.assertIn(status, (400, 403), raw)
        self.assertNoCloud()

    def test_an_empty_body_is_refused(self):
        with self.live() as s:
            status, _body = self.post(s, raw=b"")
        self.assertEqual(status, 403)
        self.assertNoCloud()

    def test_a_huge_body_is_refused(self):
        with self.live() as s:
            status, _body = self.post(s, raw=b"x" * (64 * 1024 + 10))
        self.assertEqual(status, 400)
        self.assertNoCloud()

    def test_other_event_types_are_ignored(self):
        """消息事件、卡片渲染事件等都会打到同一个地址。认错了就等于给别的事件开了后门。"""
        key = self.seed_disabled()
        with self.live() as s:
            kinds = ("im.message.receive_v1", "card.action.trigger_v1", "", "approval_task")
            for i, kind in enumerate(kinds):
                body = card_body(value={"a": "del", "k": key}, event_type=kind, event_id=f"ev-{i}")
                status, got = self.post(s, body)
                self.assertEqual((status, got), (200, {}), kind)
        self.assertEqual(self.state_of(key), "disabled")
        self.assertEqual(self.resolved, [])
        self.assertNoCloud()

    def test_a_get_is_not_the_card_hook(self):
        with self.live() as s:
            status, _body = s.request("GET", PATH)
        self.assertNotEqual(status, 200)


# ── 谁在点 ───────────────────────────────────────────────────────────────


class WhoTests(CardEndpointBase):
    def test_a_non_admin_gets_a_toast_and_touches_nothing(self):
        """名单外的人点了同一张卡（卡片会被转发、截图、也可能被猜出 open_id）。"""
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.click(s, "del", key, who=OTHER_OPEN)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error", body)
        self.assertIn("管理员", body["toast"]["content"])
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_an_open_id_that_is_not_in_the_directory_is_refused(self):
        """查不到就是查不到，不能当成「查到了一个空 union_id」然后去比名单。"""
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.click(s, "del", key, who="ou_nobody")
        self.assertEqual(status, 200)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_the_open_id_comes_from_event_operator(self):
        """回归锁：`event.operator.open_id`。写成消息事件的 `operator.operator_id.open_id`
        就永远取到空串 → 每个管理员都被拒，而日志里只有一句「只有管理员能…」。"""
        key = self.seed_disabled()
        body = card_body(value={"a": "del", "k": key})
        body["event"]["operator"] = {"operator_id": {"open_id": ADMIN_OPEN, "union_id": "on_admin"}}
        with self.live() as s:
            status, got = self.post(s, body)
        self.assertEqual(status, 200)
        self.assertEqual(got["toast"]["type"], "error", got)
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_a_union_id_in_the_body_cannot_shortcut_the_lookup(self):
        """请求体里的 union_id 是外部数据，不能当身份用 —— 必须拿 open_id 去飞书换。"""
        key = self.seed_disabled()
        body = card_body(open_id=OTHER_OPEN, value={"a": "del", "k": key})
        body["event"]["operator"]["union_id"] = "on_admin"
        with self.live() as s:
            status, got = self.post(s, body)
        self.assertEqual(got["toast"]["type"], "error", (status, got))
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_the_lookup_uses_the_cached_tenant_token(self):
        key = self.seed_disabled()
        with self.live() as s:
            self.click(s, "del", key)
        self.assertEqual(self.resolved, [(ADMIN_OPEN, "tenant-tok")])

    def test_a_feishu_outage_is_a_toast_not_a_500(self):
        """换 union_id 要打飞书接口。它挂了要么说清楚、要么什么都别做 —— 不能默认放行。"""
        key = self.seed_disabled()
        self.union_error = FeishuError("按 open_id 查用户失败：code=99991663")
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()

    def test_an_unexpected_lookup_crash_is_still_a_toast(self):
        key = self.seed_disabled()
        self.union_error = RuntimeError("socket 炸了")
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertEqual(self.state_of(key), "disabled")
        self.assertNoCloud()


# ── 按钮带的值 ───────────────────────────────────────────────────────────


class ValueTests(CardEndpointBase):
    def test_unknown_buttons_are_refused_without_touching_anything(self):
        with self.live() as s:
            for i, value in enumerate(
                (
                    {},
                    {"a": "del"},  # 没有 k
                    {"a": "del", "k": ""},
                    {"k": "aliyun/x/y"},  # 没有 a
                    {"a": "nuke", "k": "aliyun/x/y"},
                    {"a": "DEL", "k": "aliyun/x/y"},  # 大小写不放宽
                    {"a": ["del"], "k": "aliyun/x/y"},
                    "del",
                    None,
                )
            ):
                body = card_body(value=value, event_id=f"ev-{i}")
                status, got = self.post(s, body)
                self.assertEqual(status, 200, value)
                self.assertEqual(got["toast"]["type"], "error", value)
                self.assertIn("不认识", got["toast"]["content"], value)
        self.assertEqual(self.resolved, [])
        self.assertNoCloud()

    def test_an_arbitrary_key_cannot_delete_an_account(self):
        """**删号只认记录文件里有的 key**。否则一个构造出来的请求就能删掉任何人的号。"""
        self.seed_disabled()
        with self.live() as s:
            for i, key in enumerate(
                (
                    f"aliyun/{ALI_ACC}/wangwu",  # 记录里没有这个人
                    f"aliyun/{ALI_ACC}/root",
                    "aliyun/别的账号/lisi",  # 换个主账号
                    "../../etc/passwd",
                    "aliyun/1/2/3/4",
                    "volcano/x/LiSi",
                    f"  aliyun/{ALI_ACC}/lisi  ",  # 前后空格不该被归一成命中
                )
            ):
                status, got = self.click(s, "del", key, event_id=f"ev-{i}")
                self.assertEqual(status, 200, key)
                self.assertEqual(got["toast"]["type"], "error", (key, got))
        self.assertEqual(self.book.of("delete"), [])
        self.assertEqual(self.book.of("disable"), [])
        self.assertEqual(self.book.others(), [])

    def test_a_long_key_does_not_blow_up(self):
        with self.live() as s:
            status, got = self.click(s, "del", "aliyun/" + "x" * 5000)
        self.assertEqual(status, 200)
        self.assertEqual(got["toast"]["type"], "error")
        self.assertNoCloud()

    def test_a_non_string_key_is_stringified_and_then_missed(self):
        """`k` 是外部数据，可能是任意 JSON。变成字符串去查记录、查不到就拒 ——
        不能变成 `TypeError` 穿出去（那就是断连 + 飞书一直重投）。"""
        self.seed_disabled()
        with self.live() as s:
            for i, k in enumerate(({"$ne": None}, ["aliyun", "x"], 3, True)):
                body = card_body(value={"a": "del", "k": k}, event_id=f"ev-{i}")
                status, got = self.post(s, body)
                self.assertEqual(status, 200, k)
                self.assertEqual(got["toast"]["type"], "error", (k, got))
        self.assertEqual(self.book.of("delete"), [])


# ── 真正执行 ─────────────────────────────────────────────────────────────


class DecideTests(CardEndpointBase):
    def test_delete_deletes_the_account_and_records_who_decided(self):
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "success")
        self.assertIn("李四", body["toast"]["content"])
        self.assertIn("数据没动", body["toast"]["content"])
        self.assertEqual(self.book.of("delete"), [("delete", "aliyun", ALI_ACC, "lisi")])
        self.assertEqual(self.book.others(), [])  # 没碰数据（桶、数据集、实例）
        rec = self.records()[key]
        self.assertEqual(rec["state"], "deleted")
        self.assertEqual(rec["decided_by"], "admin:on_admin")
        row = json.loads((self.dir / "review.log").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(
            (row["op"], row["user"], row["actor"]), ("offboard_delete", "lisi", "admin:on_admin")
        )

    def test_keep_restores_a_disabled_account(self):
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.click(s, "keep", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "success")
        self.assertIn("恢复", body["toast"]["content"])
        self.assertEqual(self.state_of(key), "restored")
        self.assertEqual(
            self.book.of("enable"), [("enable", "aliyun", ALI_ACC, "lisi", True, ("AK-lisi",))]
        )
        self.assertEqual(self.book.of("delete"), [])

    def test_keep_on_a_suspect_just_dismisses_it_without_touching_the_cloud(self):
        """弱信号（通讯录里找不到）从来没停过号，「没离职」就只是把它从待办里拿掉。"""
        offboard.note_suspects(self.path, [person("李四", "on_1", ref("volcano", "LiSi"))])
        key = offboard.key_of("volcano", VOLC_ACC, "LiSi")
        with self.live() as s:
            status, body = self.click(s, "keep", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(key), "dismissed")
        self.assertIn("没离职", body["toast"]["content"])
        self.assertNoCloudWrites()
        self.assertEqual(self.book.of("enable"), [])

    def test_a_manual_platform_is_only_book_keeping(self):
        """九章没有接口：按钮是销账，**一次云调用都不该有**。"""
        offboard.ensure_record(
            self.path,
            platform="jiuzhang",
            account="wuji",
            user="wuji-gone",
            person="王昱然",
            signal="飞书通讯录里找不到（没有自动停用）",
        )
        key = offboard.key_of("jiuzhang", "wuji", "wuji-gone")
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "success")
        self.assertIn("九章", body["toast"]["content"])
        self.assertEqual(self.state_of(key), "deleted")
        self.assertNoCloud()

    def test_an_already_decided_record_is_refused(self):
        """同一张卡在群里留着，明天再点一次不能把已经删掉的记录再走一遍。"""
        key = self.seed_disabled()
        with self.live() as s:
            self.assertEqual(self.click(s, "del", key)[0], 200)
            status, body = self.click(s, "keep", key, event_id="ev-2")
        self.assertEqual(status, 200)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertIn("处理过", body["toast"]["content"])
        self.assertEqual(self.book.of("enable"), [])
        self.assertEqual(len(self.book.of("delete")), 1)

    def test_an_unverified_record_cannot_be_deleted_from_the_card(self):
        """名册里这个号不归他 —— 一键删掉的可能是别人在用的号。卡片上也不许删。"""
        offboard.ensure_record(
            self.path,
            platform="aliyun",
            account=ALI_ACC,
            user="someone-else",
            person="李四",
            signal="IT 的 IAM 标记离职",
            verified=False,
        )
        key = offboard.key_of("aliyun", ALI_ACC, "someone-else")
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertEqual(self.book.of("delete"), [])
        self.assertIn(self.state_of(key), ("suspect", "disabled"))

    def test_a_cloud_failure_is_a_toast_not_a_5xx(self):
        """飞书对非 200 会重投。执行失败回 5xx 的话就是无限重试 + 卡片上什么都看不到。"""
        key = self.seed_disabled()
        self.book.left["lisi"] = ["摘策略 X：Throttling"]
        with self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertIn("没删干净", body["toast"]["content"])
        self.assertEqual(self.state_of(key), "disabled")  # 还挂在待办里，下次还能点

    def test_a_provision_error_is_a_toast(self):
        key = self.seed_disabled()

        def boom(platform, account, **kw):
            ex = self.book.factory(platform, account, **kw)
            ex.delete_user = lambda user: (_ for _ in ()).throw(
                ProvisionError("删不动：Throttling")
            )
            return ex

        with mock.patch("delivery.server.executor_from_env", side_effect=boom), self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertEqual(self.state_of(key), "disabled")

    def test_an_unexpected_crash_is_a_generic_toast(self):
        """没预料到的异常不能把细节（堆栈、内部路径）回给外面，也不能变成 5xx。"""
        key = self.seed_disabled()

        def boom(platform, account, **kw):
            raise RuntimeError("超级机密的内部路径 /srv/delivery/secrets")

        with mock.patch("delivery.server.executor_from_env", side_effect=boom), self.live() as s:
            status, body = self.click(s, "del", key)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertNotIn("secrets", body["toast"]["content"])
        self.assertEqual(self.state_of(key), "disabled")


# ── 重投 ─────────────────────────────────────────────────────────────────


class ReplayTests(CardEndpointBase):
    def test_a_replay_of_the_same_click_does_not_delete_twice(self):
        """飞书 3 秒没收到 200 就重投。同一次点击必须只删一次。"""
        key = self.seed_disabled()
        with self.live() as s:
            first = self.click(s, "del", key, event_id="ev-same")
            second = self.click(s, "del", key, event_id="ev-same")
            third = self.click(s, "del", key, event_id="ev-same", nonce="n-2")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200, second)
        self.assertEqual(third[0], 200)
        self.assertEqual(self.book.of("delete"), [("delete", "aliyun", ALI_ACC, "lisi")])
        self.assertEqual(self.state_of(key), "deleted")
        self.assertIn("处理过", second[1]["toast"]["content"])

    def test_a_replay_is_caught_before_the_admin_lookup(self):
        key = self.seed_disabled()
        with self.live() as s:
            self.click(s, "del", key, event_id="ev-x")
            self.click(s, "del", key, event_id="ev-x")
        self.assertEqual(self.resolved, [(ADMIN_OPEN, "tenant-tok")])

    def test_two_different_clicks_both_run(self):
        a = self.seed_disabled("lisi")
        b = self.seed_disabled("LiSi", platform="volcano")
        with self.live() as s:
            self.click(s, "del", a, event_id="ev-a")
            self.click(s, "del", b, event_id="ev-b")
        self.assertEqual(sorted(c[1] for c in self.book.of("delete")), ["aliyun", "volcano"])
        self.assertEqual((self.state_of(a), self.state_of(b)), ("deleted", "deleted"))

    def test_a_replayed_forgery_is_still_refused(self):
        """去重表不能变成绕过验签的捷径：先发一个签名坏的，再发同 id 的好的。"""
        key = self.seed_disabled()
        with self.live() as s:
            bad = self.click(s, "del", key, event_id="ev-r", sig="0" * 64)
            good = self.click(s, "del", key, event_id="ev-r")
        self.assertEqual(bad[0], 403)
        self.assertEqual(good[0], 200, good)
        self.assertEqual(self.state_of(key), "deleted")  # 坏的那次没占掉 id

    def test_an_event_without_an_id_is_not_swallowed(self):
        """没有 event_id 就没法去重 —— 宁可重复也不要把一次真实点击静默吞掉。"""
        key = self.seed_disabled()
        with self.live() as s:
            status, body = self.click(s, "del", key, event_id="")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.state_of(key), "deleted")

    def test_after_a_restart_the_record_file_is_what_stops_a_replay(self):
        """去重表在内存里，进程一重启就空了。真正兜住「别删第二次」的是记录文件的状态。

        这条很重要：飞书的重投窗口比一次发版长得多，而这个服务是会被 systemd 重启的。"""
        key = self.seed_disabled()
        with self.live() as s:
            self.assertEqual(self.click(s, "del", key, event_id="ev-boom")[0], 200)
        with self.live() as s:  # 新 handler = 新 Hook = 空的去重表
            status, body = self.click(s, "del", key, event_id="ev-boom")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["toast"]["type"], "error")
        self.assertIn("处理过", body["toast"]["content"])
        self.assertEqual(self.book.of("delete"), [("delete", "aliyun", ALI_ACC, "lisi")])

    def test_two_admins_clicking_at_the_same_time_delete_once(self):
        """两个管理员同时点（去重表按 event_id 拦不住，两次是不同的点击）。
        记录文件的锁 + 「已处理过就拒」是这里唯一的闸门。"""
        key = self.seed_disabled()
        self.directory["ou_admin2"] = "on_admin"
        out: list = []
        barrier = threading.Barrier(2)

        def click(who, eid):
            barrier.wait(timeout=5)
            out.append(self.click(s, "del", key, who=who, event_id=eid))

        with self.live() as s:
            threads = [
                threading.Thread(target=click, args=("ou_admin", "ev-a")),
                threading.Thread(target=click, args=("ou_admin2", "ev-b")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual([r[0] for r in out], [200, 200], out)
        kinds = sorted(r[1]["toast"]["type"] for r in out)
        self.assertEqual(kinds, ["error", "success"], out)
        self.assertEqual(self.book.of("delete"), [("delete", "aliyun", ALI_ACC, "lisi")])
        self.assertEqual(self.state_of(key), "deleted")


# ── 形状怪的事件 ─────────────────────────────────────────────────────────


class OddShapeTests(CardEndpointBase):
    """形状怪但签名合法的事件。**每一个都必须拿到一个 HTTP 响应。**

    `event_type` / `claim` / `action` 在 `server.py` 里是**在 try/except 之外**调的：
    它们一抛异常，`do_POST` 就把异常带出去、socketserver 打印堆栈并直接断连 ——
    客户端拿不到任何响应，而飞书对「没收到 200」是一直重投。所以这一组的断言是
    「有响应、没 5xx、没碰云」，不是具体回了什么。
    """

    def test_odd_event_shapes_always_get_an_answer(self):
        key = self.seed_disabled()
        shapes = [
            {"event": "x"},
            {"event": []},
            {"event": 3},
            {"event": {"operator": "x", "action": "y"}},
            {"event": {"operator": {"open_id": None}, "action": {"value": None}}},
            {"event": {"action": {"value": {"a": "del", "k": key}}}},  # 没有 operator
            {"event": None},
            {"event": {}},  # 空事件
        ]
        with self.live() as s:
            for i, over in enumerate(shapes):
                body = card_body(value={"a": "del", "k": key}, event_id=f"ev-{i}")
                body.update(over)
                try:
                    status, _got = self.post(s, body)
                except Exception as exc:  # noqa: BLE001 — 断连也是一种「回答」，要报出来
                    self.fail(f"{over} 没拿到响应：{type(exc).__name__}: {exc}")
                self.assertLess(status, 500, (over, status))
        self.assertEqual(self.state_of(key), "disabled")
        self.assertEqual(self.book.of("delete"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2, stream=sys.stderr)
