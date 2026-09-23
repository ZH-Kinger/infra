"""飞书卡片按钮回调的校验层（`delivery.card_hook`）与那张带按钮的卡片（`notify.pending_card`）。

为什么这一层值得单独测到这个密度
────────────────────────────────
`/feishu/card` 是个**免登录的公网 POST 入口**，点一下就能删云账号。面板那条路有飞书 OAuth
会话兜底，这条路没有 —— 身份全靠请求体里的 `open_id`。所以「谁能让这个入口动起来」
完全取决于本文件测的三样东西：验签、Verification Token、去重。任何一条松掉，
伪造一个 `open_id` 就能删号。

本文件只测纯逻辑（不起 HTTP）。端到端那一层在 `test_delivery_card_endpoint.py`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest

from delivery import notify
from delivery.card_hook import CLOCK_SKEW, DEDUP_WINDOW, CardError, Hook, decrypt, toast

KEY = "enc-key-0123456789"
TOKEN = "verify-token-abc"  # noqa: S105 — 测试常量


# ── 造请求的工具（端点测试也用这几个） ─────────────────────────────────


def seal(plain: bytes, key: str, *, iv: bytes = b"0123456789abcdef") -> str:
    """按飞书的规矩加密：AES-256-CBC，key=sha256(Encrypt Key)，IV 放在密文最前面，PKCS7。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - len(plain) % 16
    enc = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + enc.update(plain + bytes([pad]) * pad) + enc.finalize()).decode()


def sign(ts: str, nonce: str, key: str, raw: bytes) -> str:
    """`X-Lark-Signature` = sha256(时间戳 + 随机串 + Encrypt Key + 原始 body)。"""
    return hashlib.sha256((ts + nonce + key).encode() + raw).hexdigest()


def card_body(
    *,
    open_id: str = "ou_admin",
    value=None,
    event_id: str = "ev-1",
    token: str = TOKEN,
    event_type: str = "card.action.trigger",
) -> dict:
    """一次按钮点击的事件体。**open_id 在 `event.operator.open_id`**。"""
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "token": token,
            "event_type": event_type,
            "create_time": "1700000000000",
            "app_id": "cli_demo",
        },
        "event": {
            "operator": {"open_id": open_id, "union_id": "on_whatever"},
            "action": {"tag": "button", "value": value if value is not None else {}},
        },
    }


class _Clock:
    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# ── 验签 ─────────────────────────────────────────────────────────────────


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.hook = Hook(encrypt_key=KEY, verify_token=TOKEN, clock=self.clock)
        self.raw = json.dumps(card_body(), ensure_ascii=False).encode()
        self.ts = str(int(self.clock.now))

    def headers(self, **over):
        head = {
            "X-Lark-Request-Timestamp": self.ts,
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": sign(self.ts, "nonce-1", KEY, self.raw),
        }
        head.update(over)
        return head

    def test_a_correctly_signed_request_passes(self):
        self.hook.check_signature(self.headers(), self.raw)

    def test_a_wrong_signature_is_refused(self):
        for bad in ("", "0" * 64, sign(self.ts, "nonce-2", KEY, self.raw)):
            with self.assertRaises(CardError):
                self.hook.check_signature(self.headers(**{"X-Lark-Signature": bad}), self.raw)

    def test_signing_with_another_key_is_refused(self):
        """别人不知道 Encrypt Key 就签不出来 —— 这正是这个入口唯一的身份证明。"""
        head = self.headers(
            **{"X-Lark-Signature": sign(self.ts, "nonce-1", "另一把钥匙", self.raw)}
        )
        with self.assertRaises(CardError):
            self.hook.check_signature(head, self.raw)

    def test_a_tampered_body_is_refused(self):
        """签名算的是**原始字节**：body 改一个字节就该对不上。"""
        tampered = self.raw.replace(b"ou_admin", b"ou_hackr")
        self.assertNotEqual(tampered, self.raw)
        with self.assertRaises(CardError):
            self.hook.check_signature(self.headers(), tampered)

    def test_reserializing_the_body_breaks_the_signature(self):
        """回归锁：验签必须拿 `_raw_body` 的字节，不能拿 `json.dumps(解析结果)` ——
        空格和键顺序都可能不一样，一个字节的差别就是永远 403，而现场只看到「回调不生效」。"""
        same_json = json.dumps(json.loads(self.raw.decode()), separators=(",", ":")).encode()
        self.assertEqual(json.loads(same_json), json.loads(self.raw))  # 语义一样
        self.assertNotEqual(same_json, self.raw)  # 字节不一样
        with self.assertRaises(CardError):
            self.hook.check_signature(self.headers(), same_json)

    def test_missing_headers_are_refused(self):
        for drop in ("X-Lark-Request-Timestamp", "X-Lark-Request-Nonce", "X-Lark-Signature"):
            head = self.headers()
            head.pop(drop)
            with self.assertRaises(CardError) as box:
                self.hook.check_signature(head, self.raw)
            self.assertIn("签名头", str(box.exception))

    def test_headers_object_without_the_keys_is_refused_not_crashed(self):
        with self.assertRaises(CardError):
            self.hook.check_signature({}, self.raw)

    def test_an_old_timestamp_is_refused_even_when_the_signature_is_valid(self):
        """重放：截获一个签名合法的请求，过一会儿再发一次。时间戳兜这一层。"""
        old = str(int(self.clock.now - CLOCK_SKEW - 1))
        head = {
            "X-Lark-Request-Timestamp": old,
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": sign(old, "nonce-1", KEY, self.raw),
        }
        with self.assertRaises(CardError) as box:
            self.hook.check_signature(head, self.raw)
        self.assertIn("时间", str(box.exception))

    def test_a_timestamp_from_the_future_is_refused_too(self):
        ahead = str(int(self.clock.now + CLOCK_SKEW + 1))
        head = {
            "X-Lark-Request-Timestamp": ahead,
            "X-Lark-Request-Nonce": "nonce-1",
            "X-Lark-Signature": sign(ahead, "nonce-1", KEY, self.raw),
        }
        with self.assertRaises(CardError):
            self.hook.check_signature(head, self.raw)

    def test_the_edge_of_the_window_still_passes(self):
        edge = str(int(self.clock.now - CLOCK_SKEW))
        head = {
            "X-Lark-Request-Timestamp": edge,
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": sign(edge, "n", KEY, self.raw),
        }
        self.hook.check_signature(head, self.raw)

    def test_the_go_style_timestamp_feishu_actually_sends_is_understood(self):
        """真机实测：飞书发的不是数字，是 Go 的 time.Time.String()，纳秒 9 位。
        认不出来的话新鲜度检查形同虚设，重放就只剩签名一道。"""
        import datetime

        from delivery.card_hook import _seconds

        when = datetime.datetime.fromtimestamp(self.clock.now).astimezone()
        go = (
            when.strftime("%Y-%m-%d %H:%M:%S.")
            + "993230440 "
            + when.strftime("%z")
            + " CST m=+27.1"
        )
        got = _seconds(go)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, self.clock.now, delta=1.0)
        head = {
            "X-Lark-Request-Timestamp": go,
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": sign(go, "n", KEY, self.raw),
        }
        self.hook.check_signature(head, self.raw)  # 新鲜，不抛
        old_go = (when - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S.123456789 %z CST")
        stale = {
            "X-Lark-Request-Timestamp": old_go,
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": sign(old_go, "n", KEY, self.raw),
        }
        with self.assertRaises(CardError):
            self.hook.check_signature(stale, self.raw)

    def test_an_unknown_timestamp_format_does_not_block_a_signed_request(self):
        """认不出格式**不拒**：新鲜度只是第二道防线，真正的门是验签。
        为一个没见过的格式把所有点击挡掉，等于功能直接不可用（真机上踩过）。"""
        for odd in ("abc", "١٢٣٤x", "12 34", "2026-09-23T10:39:26Z"):
            head = {
                "X-Lark-Request-Timestamp": odd,
                "X-Lark-Request-Nonce": "n",
                "X-Lark-Signature": sign(odd, "n", KEY, self.raw),
            }
            self.hook.check_signature(head, self.raw)  # 不抛
            # 但签名仍然必须对
            with self.assertRaises(CardError):
                self.hook.check_signature({**head, "X-Lark-Signature": "00"}, self.raw)

    def test_millisecond_timestamps_are_understood(self):
        """毫秒当秒算 = 五万年后、永远超窗，整个功能挂掉。13 位按毫秒读。"""
        ms = str(int(self.clock.now * 1000))
        head = {
            "X-Lark-Request-Timestamp": ms,
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": sign(ms, "n", KEY, self.raw),
        }
        self.hook.check_signature(head, self.raw)  # 新鲜，不抛
        old_ms = str(int((self.clock.now - 3600) * 1000))
        stale = {
            "X-Lark-Request-Timestamp": old_ms,
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": sign(old_ms, "n", KEY, self.raw),
        }
        with self.assertRaises(CardError):
            self.hook.check_signature(stale, self.raw)


# ── 加密 ─────────────────────────────────────────────────────────────────


class DecryptTests(unittest.TestCase):
    def test_round_trip(self):
        plain = json.dumps({"hello": "世界", "n": 1}, ensure_ascii=False)
        self.assertEqual(decrypt(seal(plain.encode(), KEY), KEY), plain)

    def test_round_trip_at_the_block_boundary(self):
        """明文正好 16 字节时 PKCS7 会补一整块。少判这一种就会把正常回调判成补位错。"""
        for size in (1, 15, 16, 17, 31, 32, 33):
            plain = "x" * size
            self.assertEqual(decrypt(seal(plain.encode(), KEY), KEY), plain, size)

    def test_payload_decrypts_the_encrypted_envelope(self):
        hook = Hook(encrypt_key=KEY, verify_token=TOKEN)
        body = card_body()
        raw = json.dumps({"encrypt": seal(json.dumps(body).encode(), KEY)}).encode()
        self.assertEqual(hook.payload(raw), body)

    def test_the_wrong_key_does_not_yield_a_body(self):
        """配错 Encrypt Key 必须是「明确拒绝」，不能变成一个乱七八糟的 dict 往下走。"""
        blob = seal(json.dumps(card_body()).encode(), KEY)
        for wrong in ("enc-key-0123456788", "", "别的 key", KEY + "x"):
            hook = Hook(encrypt_key=wrong or "x", verify_token=TOKEN)
            with self.assertRaises(Exception) as box:  # noqa: B017 — 下面断言它不是「成功」
                hook.payload(json.dumps({"encrypt": blob}).encode())
            self.assertNotIsInstance(box.exception, AssertionError)

    def test_a_wrong_key_raises_card_error(self):
        """绝大多数错误 key 会被补位检查抓住 —— 这是设计上的那条路。"""
        blob = seal(json.dumps(card_body()).encode(), KEY)
        with self.assertRaises(CardError) as box:
            decrypt(blob, "enc-key-0123456788")
        self.assertIn("补位", str(box.exception))

    def test_bad_padding_is_a_card_error(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        iv = b"0123456789abcdef"
        # 手搓一段补位非法的密文（最后一字节是 0x00，PKCS7 里没有这个取值）
        plain = b"A" * 15 + b"\x00"
        enc = Cipher(algorithms.AES(hashlib.sha256(KEY.encode()).digest()), modes.CBC(iv))
        ct = enc.encryptor()
        blob = base64.b64encode(iv + ct.update(plain) + ct.finalize()).decode()
        with self.assertRaises(CardError) as box:
            decrypt(blob, KEY)
        self.assertIn("补位", str(box.exception))

    def test_a_too_short_ciphertext_is_a_card_error(self):
        for blob in ("", base64.b64encode(b"short").decode(), base64.b64encode(b"x" * 16).decode()):
            with self.assertRaises(CardError):
                decrypt(blob, KEY)

    def test_an_encrypted_callback_without_a_key_is_refused(self):
        hook = Hook(encrypt_key="", verify_token=TOKEN)
        self.assertFalse(hook.configured)
        with self.assertRaises(CardError) as box:
            hook.payload(json.dumps({"encrypt": "whatever"}).encode())
        self.assertIn("Encrypt Key", str(box.exception))


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.hook = Hook(encrypt_key=KEY, verify_token=TOKEN)

    def test_plain_json_passes_through(self):
        self.assertEqual(self.hook.payload(b'{"a": 1}'), {"a": 1})

    def test_an_empty_body_is_an_empty_dict(self):
        self.assertEqual(self.hook.payload(b""), {})

    def test_garbage_is_a_400_not_a_crash(self):
        for raw in (b"{", b"not json", b"\xff\xfe\x00", b"<html>"):
            with self.assertRaises(CardError) as box:
                self.hook.payload(raw)
            self.assertEqual(box.exception.status, 400, raw)

    def test_a_json_array_is_refused(self):
        with self.assertRaises(CardError) as box:
            self.hook.payload(b"[1, 2]")
        self.assertEqual(box.exception.status, 400)


# ── challenge / token ────────────────────────────────────────────────────


class ChallengeTests(unittest.TestCase):
    def test_challenge_works_before_any_credential_is_configured(self):
        """先有鸡还是先有蛋：后台填回调地址时 Encrypt Key/Token 还没配到这边。"""
        hook = Hook(encrypt_key="", verify_token="")
        body = {"type": "url_verification", "challenge": "abc", "token": "t"}
        self.assertEqual(hook.challenge(body), "abc")

    def test_challenge_survives_the_encrypted_envelope(self):
        """配了 Encrypt Key 之后**连 challenge 那一次也是加密的** ——
        解密要排在 challenge 判断之前，否则回调地址根本存不下来。"""
        hook = Hook(encrypt_key=KEY, verify_token=TOKEN)
        inner = {"type": "url_verification", "challenge": "c-42", "token": TOKEN}
        raw = json.dumps({"encrypt": seal(json.dumps(inner).encode(), KEY)}).encode()
        self.assertEqual(hook.challenge(hook.payload(raw)), "c-42")

    def test_an_ordinary_event_is_not_a_challenge(self):
        self.assertIsNone(Hook(encrypt_key=KEY).challenge(card_body()))

    def test_a_challenge_without_the_string_is_still_a_challenge(self):
        """返回 `""` 而不是 None：调用方靠 `is not None` 区分「这是校验请求」。"""
        self.assertEqual(Hook(encrypt_key=KEY).challenge({"type": "url_verification"}), "")


class TokenTests(unittest.TestCase):
    def test_nothing_passes_when_no_token_is_configured(self):
        hook = Hook(encrypt_key=KEY, verify_token="")
        for body in (card_body(), card_body(token=""), {}):
            with self.assertRaises(CardError):
                hook.check_token(body)

    def test_the_right_token_passes_in_both_shapes(self):
        hook = Hook(encrypt_key=KEY, verify_token=TOKEN)
        hook.check_token(card_body())  # 2.0：header.token
        hook.check_token({"token": TOKEN})  # 旧版：顶层 token

    def test_a_mismatched_token_is_refused(self):
        hook = Hook(encrypt_key=KEY, verify_token=TOKEN)
        for bad in (TOKEN[:-1], TOKEN + " ", "", TOKEN.upper(), None):
            with self.assertRaises(CardError):
                hook.check_token(card_body(token=bad))

    def test_a_missing_header_does_not_crash_the_token_check(self):
        hook = Hook(encrypt_key=KEY, verify_token=TOKEN)
        for body in ({"header": None}, {}, {"header": {}}):
            with self.assertRaises(CardError):
                hook.check_token(body)

    def test_a_non_object_header_is_refused_not_crashed(self):
        """回归锁（`card_hook.Hook._part`）：`header` / `event` 是外部数据，可能是任何 JSON。

        曾经这几处直接 `.get()`，拿到 `"x"` 就 `AttributeError`；而
        `event_type` / `claim` / `action` 在 `server.py` 里是**在 try/except 之外**调的 ——
        抛出去就是连接被掐掉：客户端拿不到任何 HTTP 响应，飞书对「没收到 200」一直重投，
        日志里只有一串堆栈。要的是「明确拒绝」。"""
        hook = Hook(encrypt_key=KEY, verify_token=TOKEN)
        for odd in ("x", 3, [], True):
            with self.assertRaises(CardError):
                hook.check_token({"header": odd})
            self.assertEqual(hook.event_type({"header": odd}), "")
            self.assertTrue(hook.claim({"header": odd}))  # 取不到 id 就不去重
            self.assertEqual(Hook.action({"header": odd, "event": odd}), ("", {}))
            self.assertEqual(Hook.action({"event": {"operator": odd, "action": odd}}), ("", {}))


# ── 事件类型 / 去重 / 取动作 ────────────────────────────────────────────


class EventTypeTests(unittest.TestCase):
    def test_it_reads_the_2_0_header(self):
        hook = Hook(encrypt_key=KEY)
        self.assertEqual(hook.event_type(card_body()), "card.action.trigger")
        self.assertEqual(
            hook.event_type(card_body(event_type="im.message.receive_v1")),
            "im.message.receive_v1",
        )
        self.assertEqual(hook.event_type({}), "")
        self.assertEqual(hook.event_type({"header": None}), "")

    def test_event_type_is_not_confused_by_a_top_level_field(self):
        """伪造者在顶层塞 `event_type` 也不该被当成卡片点击。"""
        hook = Hook(encrypt_key=KEY)
        body = {"event_type": "card.action.trigger", "event": {}}
        self.assertEqual(hook.event_type(body), "")


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.hook = Hook(encrypt_key=KEY, verify_token=TOKEN, clock=self.clock)

    def test_a_replay_of_the_same_event_id_is_not_claimed(self):
        """飞书 3 秒没收到 200 就重投。同一次点击不能删两次号。"""
        body = card_body(event_id="ev-77")
        self.assertTrue(self.hook.claim(body))
        self.assertFalse(self.hook.claim(body))
        self.assertFalse(self.hook.claim(card_body(event_id="ev-77", open_id="ou_other")))

    def test_different_events_are_each_claimed(self):
        self.assertTrue(self.hook.claim(card_body(event_id="a")))
        self.assertTrue(self.hook.claim(card_body(event_id="b")))

    def test_the_window_expires(self):
        self.assertTrue(self.hook.claim(card_body(event_id="ev")))
        self.clock.now += DEDUP_WINDOW + 1
        self.assertTrue(self.hook.claim(card_body(event_id="ev")))

    def test_an_event_without_an_id_is_always_claimed(self):
        """没有 event_id 就没法去重。宁可重复执行也不要静默吞掉一次真实点击。"""
        for body in (card_body(event_id=""), {"header": {}}, {}):
            self.assertTrue(self.hook.claim(body))

    def test_the_table_does_not_grow_without_bound(self):
        for i in range(5000):
            self.hook.claim(card_body(event_id=f"e{i}"))
        self.assertLessEqual(len(self.hook._seen), 2000)


class ActionTests(unittest.TestCase):
    def test_it_reads_the_operator_open_id(self):
        who, value = Hook.action(card_body(open_id="ou_abc", value={"a": "del", "k": "x/y/z"}))
        self.assertEqual(who, "ou_abc")
        self.assertEqual(value, {"a": "del", "k": "x/y/z"})

    def test_the_message_event_shape_is_not_accepted(self):
        """回归锁：`operator.operator_id.open_id` 是**消息事件**的形状。取错了永远是空串，
        然后所有按 open_id 判管理员的地方一律拒绝，日志里什么线索都没有。"""
        body = card_body()
        body["event"]["operator"] = {"operator_id": {"open_id": "ou_abc"}}
        who, _value = Hook.action(body)
        self.assertEqual(who, "")

    def test_a_missing_or_odd_value_becomes_an_empty_dict(self):
        for value in (None, "del", ["del"], 3):
            body = card_body(value=value)
            if value is None:
                body["event"]["action"].pop("value")
            _who, got = Hook.action(body)
            self.assertEqual(got, {}, value)

    def test_a_body_without_an_event_does_not_crash(self):
        self.assertEqual(Hook.action({}), ("", {}))
        self.assertEqual(Hook.action({"event": {"operator": None, "action": None}}), ("", {}))


class ToastTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(toast("error", "不行"), {"toast": {"type": "error", "content": "不行"}})

    def test_long_text_is_clipped(self):
        got = toast("info", "长" * 500)
        self.assertEqual(len(got["toast"]["content"]), 120)


# ── 卡片本身 ─────────────────────────────────────────────────────────────


def rec(**over) -> dict:
    base = {
        "platform": "aliyun",
        "account": "1234",
        "user": "lisi",
        "person": "李四",
        "signal": "IT 的 IAM 标记离职",
        "state": "disabled",
    }
    base.update(over)
    return base


def raw_buttons(card: dict) -> list:
    """按钮元素本身（不拆 behaviors）—— 要看 confirm / type 的用它。"""
    out = []

    def walk(elements):
        for el in elements or ():
            if not isinstance(el, dict):
                continue
            if el.get("tag") == "button":
                out.append(el)
                continue
            for col in el.get("columns") or ():
                walk(col.get("elements"))
            walk(el.get("elements"))

    walk(card["body"]["elements"])
    return out


def buttons(card: dict) -> list:
    """卡片里所有按钮。**2.0 没有 action 容器**：按钮直接是元素，并排的放在 column_set 里。"""
    out = []

    def walk(elements):
        for el in elements or ():
            if not isinstance(el, dict):
                continue
            if el.get("tag") == "button":
                for beh in el.get("behaviors") or []:
                    out.append((el["text"]["content"], beh.get("type"), beh.get("value")))
            for col in el.get("columns") or ():
                walk(col.get("elements"))
            walk(el.get("elements") if el.get("tag") != "button" else None)

    walk(card["body"]["elements"])
    return out


class PendingCardTests(unittest.TestCase):
    def test_it_is_card_json_2_0(self):
        """1.0 的按钮走的是另一条老通道、另一套验签 —— 混着写按钮点了没反应。"""
        card = notify.pending_card([rec()])
        self.assertEqual(card["schema"], "2.0")
        self.assertIn("elements", card["body"])

    def test_each_row_has_a_delete_and_a_keep_button_carrying_the_key(self):
        card = notify.pending_card([rec()])
        got = buttons(card)
        self.assertIn(("确认删除", "callback", {"a": "del", "k": "aliyun/1234/lisi"}), got)
        keep = [b for b in got if b[2] == {"a": "keep", "k": "aliyun/1234/lisi"}]
        self.assertEqual(len(keep), 1, got)

    def test_the_delete_button_asks_once_more(self):
        """删号不可恢复。二次确认是这张卡上唯一的「后悔」机会。"""
        card = notify.pending_card([rec()])
        dels = [b for b in raw_buttons(card) if b["text"]["content"] == "确认删除"]
        self.assertEqual(len(dels), 1)
        self.assertIn("confirm", dels[0])
        self.assertIn("不能恢复", json.dumps(dels[0], ensure_ascii=False))
        self.assertEqual(dels[0]["type"], "danger")

    def test_jiuzhang_rows_say_you_did_it_by_hand(self):
        """九章没有接口：面板停不了也删不了，按钮只是销账。文案不能写成「确认删除」。"""
        card = notify.pending_card([rec(platform="jiuzhang", account="wuji", user="wuji-gone")])
        got = buttons(card)
        labels = [b[0] for b in got]
        self.assertIn("我已在控制台处理", labels)
        self.assertNotIn("确认删除", labels)
        self.assertIn(
            ("我已在控制台处理", "callback", {"a": "del", "k": "jiuzhang/wuji/wuji-gone"}), got
        )
        self.assertIn("控制台", json.dumps(card, ensure_ascii=False))

    def test_unverified_rows_get_no_delete_button(self):
        """名册里这个号不归他 —— 一键删掉的可能是别人在用的号。只留「没离职」。"""
        card = notify.pending_card([rec(unverified=True)])
        got = buttons(card)
        self.assertEqual([b[2] for b in got], [{"a": "keep", "k": "aliyun/1234/lisi"}])
        self.assertIn("不归他", json.dumps(card, ensure_ascii=False))

    def test_more_than_five_records_show_an_overflow_line(self):
        records = [rec(user=f"u{i}", person=f"人{i}") for i in range(8)]
        card = notify.pending_card(records, base_url="https://cloud.example.com")
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("还有 3 个号", text)
        self.assertIn("8", card["header"]["title"]["content"])
        self.assertIn("u4", text)
        self.assertNotIn("u5", text)  # 第 6 个起只进「还有 N 个」
        self.assertEqual(len([b for b in buttons(card) if b[1] == "callback"]), 10)
        self.assertIn("#admin/iam", text)

    def test_exactly_five_records_have_no_overflow_line(self):
        card = notify.pending_card([rec(user=f"u{i}") for i in range(5)])
        self.assertNotIn("还有", json.dumps(card, ensure_ascii=False))
        self.assertEqual(len([b for b in buttons(card) if b[1] == "callback"]), 10)

    def test_no_base_url_means_no_panel_button(self):
        card = notify.pending_card([rec()], base_url="")
        self.assertNotIn("open_url", json.dumps(card, ensure_ascii=False))

    def test_the_state_is_visible(self):
        """「已停用」和「没停过」是两回事：前者点删是收尾，后者点删是第一次动这个号。"""
        # 标签只说面板自己做过什么，不替云上做断言（改版方案 5.4）
        self.assertIn(
            "已停用（面板停的）", json.dumps(notify.pending_card([rec()]), ensure_ascii=False)
        )
        text = json.dumps(notify.pending_card([rec(state="suspect")]), ensure_ascii=False)
        self.assertIn("面板没停过它", text)
        self.assertNotIn("已停用", text)

    def test_a_row_without_a_person_name_falls_back_to_the_username(self):
        card = notify.pending_card([rec(person="")])
        self.assertIn("lisi", json.dumps(card, ensure_ascii=False))

    def test_a_custom_title_is_used(self):
        card = notify.pending_card([rec()], title="离职停号需要你看一下")
        self.assertEqual(card["header"]["title"]["content"], "离职停号需要你看一下")

    def test_an_empty_list_does_not_crash(self):
        card = notify.pending_card([])
        self.assertEqual(buttons(card), [])

    def test_a_generator_of_records_is_not_silently_emptied(self):
        """回归锁：`records` 只许 `list()` 一次。对迭代器连取两次的话，第二次是空 ——
        标题会说「0 个」、溢出行算成负数，而按钮却是对的。"""
        card = notify.pending_card(iter([rec(user=f"u{i}") for i in range(3)]))
        self.assertIn("3", card["header"]["title"]["content"])
        self.assertEqual(len([b for b in buttons(card) if b[1] == "callback"]), 6)
