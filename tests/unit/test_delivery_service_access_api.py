"""`POST /api/service-access` 端到端：真 HTTP、真 handler、真申请单文件。

调用方是**另一台机器上的进程**（MLflow 前面那个自建网关），不是浏览器：没有登录会话、
给不出同源 Origin，面板现有那两道门在这儿都是摆设。所以这条路自己长了一道门 ——
专用令牌 —— 而这道门的每一条规矩都有代价明确的反面：

  · **令牌决定 service**，不是请求体决定：否则 mlflow 那个网关的令牌泄漏出去，拿着它
    就能查所有服务谁有权限（身份枚举）。
  · **令牌不对 / 没配 / 文件坏了一律 403，且不说是哪一种**：说了对调用方没用，
    对猜令牌的人有用。
  · **响应体只回 `{allowed, reason, message}`（+ 放行时的到期时间）**：姓名、邮箱、
    手机号、部门、云账号、单号一个都不回。这里的断言是**把整个响应体序列化后做子串
    检查**，不靠人去核字段名 —— 将来谁往 Decision 上再挂一个字段，这几条会当场红。
  · **判不了就 500**，网关那边据此**不放行**（fail-closed）。而网关里已有的
    `is_disabled()` 查库失败时是**放行**（fail-open，禁用名单查不到不该误伤正常人）。
    两个相邻的函数方向相反，**是有意的**：见下面 FailDirectionTests 的说明，别顺手统一。

判定本身（单子/离职/名册怎么算）在 `test_delivery_service_access.py`。
离线，数据虚构。
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delivery import server as server_mod
from delivery import service_access as sa
from delivery.server import ENV_SERVICE_TOKENS as ENV_TOKENS
from delivery.server import Backend

from .test_delivery_offboard_api import _Live

PATH = "/api/service-access"
SERVICE = "mlflow"
OTHER_SERVICE = "tensorboard"
TOKEN = "tok-new-" + "a" * 24
OLD_TOKEN = "tok-old-" + "b" * 24
OTHER_TOKEN = "tok-other-" + "c" * 24

UID = "on_u1"
STRANGER = "on_nobody"
TICKET_ID = "REQ-20260101-DEADBEEF"
#: 申请单里有、**响应体里一个字都不该有**的东西
NAME = "李四"
EMAIL = "li.si@wuji.tech"
PHONE = "13900000001"
DEPT = "算法组"
CLOUD_ACCOUNT = "1234567890123456"
CLOUD_USER = "lisi"

SECRETS = (NAME, EMAIL, PHONE, DEPT, CLOUD_ACCOUNT, CLOUD_USER, TICKET_ID, "svc-mlflow")


def done_ticket(*, tid=TICKET_ID, union_id=UID, service=SERVICE, expires=None, status="done"):
    row = {
        "id": tid,
        "kind": "service",
        "status": status,
        "template": {"id": "svc-mlflow", "kind": "service", "service": service, "max_days": 90},
        "applicant": {
            "union_id": union_id,
            "name": NAME,
            "email": EMAIL,
            "phone": PHONE,
            "department": DEPT,
            "open_id": "ou_x",
        },
        "payload": {"days": 90, "account": CLOUD_ACCOUNT, "user": CLOUD_USER},
        "reason": "要看实验记录",
    }
    if expires is not None:
        row["expires_at_ts"] = expires
    return row


# ── 令牌门禁（`_service_for_token` 自己那一层） ───────────────────────────


class TokenGateTests(unittest.TestCase):
    """`Authorization: Bearer <令牌>` → 这个令牌能查哪个服务。认不出返回空串（调用方 403）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "service-tokens.json"
        # 令牌文件按 (路径, mtime, 大小) 缓存，而这张表是**模块级**的：
        # 不清的话，上一个用例写过的令牌能在下一个用例里继续用
        server_mod._service_token_cache.clear()
        self.addCleanup(server_mod._service_token_cache.clear)
        self.write({SERVICE: {"tokens": [TOKEN, OLD_TOKEN]}})

    def write(self, data, *, raw: str = ""):
        self.path.write_text(raw if raw else json.dumps(data, ensure_ascii=False), encoding="utf-8")
        # mtime 在本机的粒度是 1ms，用例里连写两版常落在同一毫秒 —— 不把时间戳推开的话，
        # 测的就不是「改了令牌文件认不认」而是「这毫秒过完没」
        was = getattr(self, "_mtime_ns", 0)
        self._mtime_ns = max(was, self.path.stat().st_mtime_ns) + 2_000_000
        os.utime(self.path, ns=(self._mtime_ns, self._mtime_ns))

    def ask(self, header, *, env=None):
        conf = {ENV_TOKENS: str(self.path)} if env is None else env
        with mock.patch.dict(os.environ, conf, clear=False):
            if env is not None and ENV_TOKENS not in env:
                os.environ.pop(ENV_TOKENS, None)
            return server_mod._service_for_token(header)

    def test_good_token_maps_to_its_service(self):
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), SERVICE)

    def test_rotation_both_old_and_new_token_work(self):
        """`tokens` 是**数组**：轮换期新旧并存，换完删掉旧的即可，两边都不用重启。"""
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), SERVICE)
        self.assertEqual(self.ask(f"Bearer {OLD_TOKEN}"), SERVICE)

    def test_each_service_has_its_own_token(self):
        self.write(
            {SERVICE: {"tokens": [TOKEN]}, OTHER_SERVICE: {"tokens": [OTHER_TOKEN]}},
        )
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), SERVICE)
        self.assertEqual(self.ask(f"Bearer {OTHER_TOKEN}"), OTHER_SERVICE)

    def test_scheme_is_case_insensitive_and_token_is_trimmed(self):
        for header in (f"bearer {TOKEN}", f"BEARER {TOKEN}", f"Bearer  {TOKEN} "):
            with self.subTest(header):
                self.assertEqual(self.ask(header), SERVICE)

    def test_everything_unrecognised_maps_to_nothing(self):
        """认不出就是空串 → 端点 403。**任何一种情况都不能回退成「放行」**。"""
        cases = {
            "错令牌": "Bearer tok-wrong",
            "空令牌": "Bearer ",
            "只有一个词": "Bearer",
            "别的认证方式": f"Basic {TOKEN}",
            "裸令牌没有 Bearer": TOKEN,
            "空头": "",
            "只有空格": "   ",
            "令牌是前缀": f"Bearer {TOKEN[:-1]}",
            "令牌多一位": f"Bearer {TOKEN}x",
        }
        for name, header in cases.items():
            with self.subTest(name):
                self.assertEqual(self.ask(header), "")

    def test_bad_token_file_never_maps_to_a_service(self):
        """没配 / 读不了 / 格式乱，**一律认不出**。这道门的全部意义就在于「配不对就进不来」。"""
        self.assertEqual(self.ask(f"Bearer {TOKEN}", env={}), "", "没配 env")
        self.assertEqual(
            self.ask(f"Bearer {TOKEN}", env={ENV_TOKENS: str(self.root / "nope.json")}),
            "",
            "文件不存在",
        )
        self.assertEqual(
            self.ask(f"Bearer {TOKEN}", env={ENV_TOKENS: str(self.root)}), "", "路径是目录"
        )
        for name, payload in {
            "不是 JSON": {"raw": "{ 坏的"},
            "顶层是数组": {"data": [{"tokens": [TOKEN]}]},
            "顶层是字符串": {"data": TOKEN},
            "spec 不是对象": {"data": {SERVICE: TOKEN}},
            "没有 tokens": {"data": {SERVICE: {"token": TOKEN}}},
            "tokens 是 null": {"data": {SERVICE: {"tokens": None}}},
            "tokens 是对象": {"data": {SERVICE: {"tokens": {"a": TOKEN}}}},
            "tokens 里是数字": {"data": {SERVICE: {"tokens": [123]}}},
        }.items():
            with self.subTest(name):
                self.write(payload.get("data"), raw=payload.get("raw", ""))
                self.assertEqual(self.ask(f"Bearer {TOKEN}"), "")

    def test_unreadable_file_is_not_an_open_door(self):
        self.path.chmod(0o000)
        self.addCleanup(self.path.chmod, 0o600)
        if os.access(self.path, os.R_OK):  # root 读得了，跳过
            self.skipTest("以 root 运行，chmod 000 挡不住")
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), "")

    def test_empty_entries_in_tokens_are_skipped(self):
        """令牌文件里留了个空串（删旧令牌删了一半），不能变成「空令牌可用」。"""
        self.write({SERVICE: {"tokens": ["", None, TOKEN]}})
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), SERVICE)
        self.assertEqual(self.ask("Bearer "), "")

    def test_comparison_goes_through_compare_digest(self):
        """运行时也得走常量时间比对（源码层面的锁在 test_delivery_service_access.py）。"""
        with mock.patch.object(
            server_mod.secrets, "compare_digest", wraps=secrets.compare_digest
        ) as spy:
            self.assertEqual(self.ask(f"Bearer {TOKEN}"), SERVICE)
        self.assertTrue(spy.called)

    def test_tokens_written_as_a_string_voids_the_whole_spec(self):
        """**回归**：`tokens` 写成字符串而不是数组（少一对方括号，文档里是数组，
        写错不会有任何报错）时，`for known in tokens` 会逐**字符**迭代 ——
        `Bearer t` 就能过门，一把 32 位的密钥退化成 1 位。

        修法是「格式不对 → 整条 spec 作废」，所以这里两头都要断言：
        单字符不过**且全串也不过**。只锁单字符的话，「顺手把字符串 split 一下」
        这种修法也能过，而那是把畸形配置当成有效配置在用。
        """
        self.write({SERVICE: {"tokens": TOKEN}})
        self.assertEqual(self.ask(f"Bearer {TOKEN[0]}"), "", "单字符不能过门")
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), "", "整条 spec 作废，全串也不认")
        # 同一份文件里另一条写法正确的 spec 不受牵连
        self.write({SERVICE: {"tokens": TOKEN}, OTHER_SERVICE: {"tokens": [OTHER_TOKEN]}})
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), "")
        self.assertEqual(self.ask(f"Bearer {OTHER_TOKEN}"), OTHER_SERVICE)

    def test_non_ascii_token_is_rejected_without_raising(self):
        """**回归**：`secrets.compare_digest` 对含非 ASCII 的 str 抛 `TypeError`，
        而 Authorization 头是按 latin-1 解出来的 —— 随便一个中文字节就能让请求抛出
        handler（`do_POST` 没有兜底）：连接被直接关掉、栈进日志，而不是干净的 403。

        所以非 ASCII 在比对**之前**就要丢掉，两头都要丢：请求里的令牌、令牌文件里的令牌。
        """
        for header in ("Bearer 中文令牌", "Bearer tok-\xff", f"Bearer {TOKEN}\u00e9"):
            with self.subTest(header):
                self.assertEqual(self.ask(header), "")
        # 令牌文件里配了非 ASCII 令牌：也不能在比对时炸，只能是「认不出」
        self.write({SERVICE: {"tokens": ["中文令牌"]}})
        self.assertEqual(self.ask("Bearer 中文令牌"), "")
        self.assertEqual(self.ask(f"Bearer {TOKEN}"), "")


# ── 端到端 ────────────────────────────────────────────────────────────────


class ServiceAccessEndpointBase(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        d = self.root / "identity"
        d.mkdir()
        (d / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), encoding="utf-8")
        self.people_path = d / "people.json"
        self.offboard_path = d / "offboard.json"
        self.tickets_path = self.root / "tickets.json"
        self.tokens_path = self.root / "service-tokens.json"
        self.write_people(UID)
        self.write_tickets(done_ticket())
        self.write_tokens({SERVICE: {"tokens": [TOKEN, OLD_TOKEN]}})
        self.backend = Backend(
            people_path=str(self.people_path),
            admins_path=str(d / "admins.json"),
            tickets_path=str(self.tickets_path),
            platforms={"aliyun": "阿里云"},
        )
        # 用例里手动再调一次 setUp（换一份坏数据重来）时，必须连服务器一起换掉：
        # handler 是建的时候就把 backend 绑死的，留着旧的等于在测上一轮的文件
        self._server = None
        # 限流表和令牌缓存都是**模块级**的，按来源 IP 计数；用例全从 127.0.0.1 打，
        # 不清就会互相串（尤其 `_svc_bad`：攒够 20 次坏令牌之后 403 会变成 429）
        for table in (
            server_mod._pickup_tries,
            server_mod._svc_tries,
            server_mod._svc_bad,
            server_mod._service_token_cache,
        ):
            table.clear()
            self.addCleanup(table.clear)
        patcher = mock.patch.dict(os.environ, {ENV_TOKENS: str(self.tokens_path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 文件 ──

    def write_people(self, *uids):
        self._write(
            self.people_path,
            {
                "schema": "wuji-people@1",
                "people": [
                    {
                        "union_id": u,
                        "name": NAME,
                        "email": EMAIL,
                        "accounts": [
                            {"platform": "aliyun", "account": CLOUD_ACCOUNT, "name": CLOUD_USER}
                        ],
                    }
                    for u in uids
                ],
            },
        )

    def write_tickets(self, *rows, schema="wuji-tickets@1"):
        payload = {"tickets": list(rows)}
        if schema is not None:
            payload["schema"] = schema
        self._write(self.tickets_path, payload)

    def write_tokens(self, data):
        self._write(self.tokens_path, data)

    def write_offboard(self, uid, state):
        self._write(
            self.offboard_path,
            {
                "records": {
                    f"aliyun/{CLOUD_ACCOUNT}/{CLOUD_USER}": {"union_id": uid, "state": state}
                }
            },
        )

    def _write(self, path: Path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 文件 mtime 粒度 1ms、缓存键就是 mtime：把时间戳推到严格递增，
        # 免得用例测的是「这毫秒过完没」
        self._mtime_ns = max(getattr(self, "_mtime_ns", 0), path.stat().st_mtime_ns) + 2_000_000
        os.utime(path, ns=(self._mtime_ns, self._mtime_ns))

    # ── 发请求 ──

    def live(self):
        """这个用例自己那台服务器（第一次用时才起）。

        一个用例一台：`ThreadingHTTPServer.shutdown()` 要等轮询间隔（0.5s），
        每发一次请求起停一次会让这个文件独占掉整个测试套件的几分之一。
        用例里换了 `self.backend` 的（比如故意不配申请单路径），**换完再发第一个请求**，
        服务器就会拿到新的那个。
        """
        if getattr(self, "_server", None) is None:
            stack = contextlib.ExitStack()
            self.addCleanup(stack.close)
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            self._server = stack.enter_context(_Live(self.backend))
        return self._server

    def post(self, server, *, token=TOKEN, body=None, raw=None, headers=None, method="POST"):
        """返回 `(status, 解析后的 body, 原始字节)`。原始字节给子串断言用。"""
        if raw is None:
            payload = {"service": SERVICE, "union_id": UID} if body is None else body
            raw = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        head = {"Content-Type": "application/json"}
        if token is not None:
            head["Authorization"] = f"Bearer {token}"
        head.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        try:
            conn.request(method, PATH, body=raw, headers=head)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
        finally:
            conn.close()
        try:
            return status, json.loads(data or b"null"), data
        except ValueError:
            return status, None, data

    def ask(self, **kw):
        return self.post(self.live(), **kw)


class ServiceAccessDecisionTests(ServiceAccessEndpointBase):
    """七种情形走完整条 HTTP：放行 / 三种拒绝 / 令牌 / 服务对不上 / 判不了。"""

    def test_granted(self):
        status, body, _ = self.ask()
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"allowed": True, "reason": "", "message": ""})

    def test_granted_carries_expiry(self):
        self.write_tickets(done_ticket(expires=4_102_444_800))
        status, body, _ = self.ask()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["allowed"], True)
        self.assertEqual(body["expires_at"], 4_102_444_800)
        self.assertEqual(set(body), {"allowed", "reason", "message", "expires_at"})

    def test_no_grant(self):
        self.write_tickets()
        status, body, _ = self.ask()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["allowed"], False)
        self.assertEqual(body["reason"], "no_grant")
        self.assertTrue(body["message"].strip())
        self.assertNotIn("expires_at", body)

    def test_revoked_ticket_is_refused_right_away(self):
        """撤销 → 下一次问就挡住。这个按钮的唯一场景就是「现在就要挡住」。"""
        server = self.live()
        self.assertTrue(self.post(server)[1]["allowed"])
        self.write_tickets(done_ticket(status="revoked"))
        status, body, _ = self.post(server)
        self.assertEqual((status, body["allowed"], body["reason"]), (200, False, "no_grant"))

    def test_not_in_roster(self):
        status, body, _ = self.ask(body={"service": SERVICE, "union_id": STRANGER})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["reason"], "not_in_roster")
        self.assertFalse(body["allowed"])

    def test_blocked_when_offboarded(self):
        for state in ("disabled", "deleted"):
            with self.subTest(state):
                self.write_offboard(UID, state)
                status, body, _ = self.ask()
                self.assertEqual(status, 200, body)
                self.assertEqual(body["reason"], "blocked")
                self.assertFalse(body["allowed"])

    def test_suspect_is_not_blocked(self):
        """`suspect` 只是嫌疑，还没人确认。拿它挡人会误伤在职的。"""
        self.write_offboard(UID, "suspect")
        status, body, _ = self.ask()
        self.assertEqual((status, body["allowed"]), (200, True))

    def test_three_refusals_say_three_different_things(self):
        """没申请的该去申请，名册没同步的该找管理员，离职的谁也帮不了他 ——
        文案混成一句会让人做错事。这里只锁区分度，不锁字句。"""
        msgs = {}
        self.write_tickets()
        msgs["no_grant"] = self.ask()[1]
        msgs["not_in_roster"] = self.ask(body={"service": SERVICE, "union_id": STRANGER})[1]
        self.write_offboard(UID, "disabled")
        self.write_tickets(done_ticket())
        msgs["blocked"] = self.ask()[1]
        for reason, body in msgs.items():
            self.assertEqual(body["reason"], reason, body)
            self.assertTrue(body["message"].strip(), reason)
        texts = [b["message"] for b in msgs.values()]
        self.assertEqual(len(set(texts)), 3, texts)

    def test_missing_union_id_is_400(self):
        for body in (
            {"service": SERVICE},
            {"service": SERVICE, "union_id": ""},
            {"service": SERVICE, "union_id": "   "},
            {"service": SERVICE, "union_id": None},
        ):
            with self.subTest(body):
                status, out, _ = self.ask(body=body)
                self.assertEqual(status, 400, out)
                self.assertNotIn("allowed", out)

    def test_bad_request_body_is_400(self):
        for name, kw in {
            "不是 JSON": {"raw": "{ 坏的".encode()},
            "是数组": {"raw": b"[1,2]"},
            "是字符串": {"raw": b'"x"'},
            "不是 UTF-8": {"raw": b"\xff\xfe"},
        }.items():
            with self.subTest(name):
                status, out, _ = self.ask(**kw)
                self.assertEqual(status, 400, out)

    def test_oversized_body_is_refused_before_anything_else(self):
        status, out, _ = self.ask(
            raw=json.dumps({"service": SERVICE, "union_id": UID, "pad": "x" * 8192}).encode()
        )
        self.assertEqual(status, 400, out)
        self.assertNotIn("allowed", out)

    def test_only_post(self):
        server = self.live()
        for method in ("GET", "PUT", "DELETE"):
            with self.subTest(method):
                status, _body, _ = self.post(server, method=method, raw=b"")
                self.assertNotEqual(status, 200)


class ResponseMinimisationTests(ServiceAccessEndpointBase):
    """响应体只回 `{allowed, reason, message}` + 放行时的 `expires_at`。

    调用方是另一台机器上的进程，给多了就是白给：它要的只是「放不放行」和「不放行时给
    人看什么」。断言方式是**把整个响应体当成一个字符串做子串检查** —— 靠肉眼核字段名的话，
    将来谁往 `Decision` 上挂一个字段、`out` 里顺手带出去，没人会发现。
    """

    def assert_clean(self, raw: bytes, body):
        text = raw.decode("utf-8", "replace")
        for needle in SECRETS:
            self.assertNotIn(needle, text, f"响应体里出现了 {needle}：{text}")
        # 转义过的也算（`ensure_ascii` 万一被改回 True）
        for needle in SECRETS:
            self.assertNotIn(
                json.dumps(needle, ensure_ascii=True).strip('"'), text, f"转义后的 {needle}"
            )
        if isinstance(body, dict):
            # 正常答复只有这四个键；错误答复只有 error 一个键（文案是固定串）
            allowed_keys = {"allowed", "reason", "message", "expires_at"}
            self.assertTrue(set(body) <= allowed_keys or set(body) == {"error"}, body)

    def test_granted_response_carries_nothing_extra(self):
        self.write_tickets(done_ticket(expires=4_102_444_800))
        status, body, raw = self.ask()
        self.assertEqual(status, 200)
        self.assertTrue(body["allowed"])
        self.assert_clean(raw, body)

    def test_ticket_id_is_never_returned(self):
        """单号是内部台账用的（`Decision.ticket_id` 有，但不回给网关）：
        它能让调用方顺着查申请详情，而这个接口只该回答「放不放行」。"""
        status, _body, raw = self.ask()
        self.assertEqual(status, 200)
        self.assertNotIn(TICKET_ID, raw.decode())
        self.assertNotIn("ticket", raw.decode().lower())

    def test_every_refusal_response_is_clean_too(self):
        self.write_tickets()
        _status, body, raw = self.ask()  # no_grant
        self.assertEqual(body["reason"], "no_grant")
        self.assert_clean(raw, body)
        status, body, raw = self.ask(body={"service": SERVICE, "union_id": STRANGER})
        self.assertEqual(body["reason"], "not_in_roster")
        self.assert_clean(raw, body)
        self.write_offboard(UID, "disabled")
        self.write_tickets(done_ticket())
        status, body, raw = self.ask()
        self.assertEqual(body["reason"], "blocked")
        self.assert_clean(raw, body)

    def test_error_responses_leak_nothing_either(self):
        """403/400/500 也不能带出内部细节（路径、异常文本、申请人信息）。"""
        cases = [
            ("错令牌", dict(token="tok-wrong")),
            ("缺 union_id", dict(body={"service": SERVICE})),
            ("服务对不上", dict(body={"service": OTHER_SERVICE, "union_id": UID})),
        ]
        for name, kw in cases:
            with self.subTest(name):
                status, body, raw = self.ask(**kw)
                self.assertIn(status, (400, 403), (status, body))
                self.assert_clean(raw, body)
                self.assertNotIn(str(self.root), raw.decode())

    def test_server_error_leaks_neither_paths_nor_reasons(self):
        self.write_tickets(done_ticket(), schema=None)  # 存储格式不对 → 判不了
        status, body, raw = self.ask()
        self.assertEqual(status, 500, body)
        self.assertNotIn("allowed", body)
        self.assertNotIn(str(self.root), raw.decode())
        self.assertNotIn("schema", raw.decode())


class TokenGateOverHttpTests(ServiceAccessEndpointBase):
    """令牌这道门在真 HTTP 上的样子：403、且**所有失败长得一模一样**。"""

    def test_good_token_works_and_so_does_the_old_one(self):
        for token in (TOKEN, OLD_TOKEN):
            with self.subTest(token):
                status, body, _ = self.ask(token=token)
                self.assertEqual((status, body["allowed"]), (200, True))

    def test_token_decides_the_service_not_the_body(self):
        """mlflow 的令牌不能拿来查别的服务 —— 否则一个服务的令牌泄漏就能查所有服务，
        连带把「谁有权限」这张表也枚举干净。"""
        self.write_tokens({SERVICE: {"tokens": [TOKEN]}, OTHER_SERVICE: {"tokens": [OTHER_TOKEN]}})
        # 拿 tensorboard 的令牌问 mlflow → 拒
        status, body, _ = self.ask(token=OTHER_TOKEN)
        self.assertEqual(status, 403, body)
        # 反过来也一样
        status, body, _ = self.ask(token=TOKEN, body={"service": OTHER_SERVICE, "union_id": UID})
        self.assertEqual(status, 403, body)
        # 各问各的就正常
        self.assertEqual(self.ask(token=TOKEN)[0], 200)

    def test_service_field_must_match_exactly(self):
        for svc in (None, "", "MLFLOW", " mlflow", "mlflow2", 0, ["mlflow"]):
            with self.subTest(svc):
                status, body, _ = self.ask(body={"service": svc, "union_id": UID})
                self.assertEqual(status, 403, (svc, body))

    def test_all_token_failures_look_identical(self):
        """令牌错 / 没令牌 / 没配文件 / 文件读不了 / 格式乱 —— **一律 403，且不说是哪一种**。
        说了对调用方没用，对猜令牌的人有用（能区分「令牌错」和「面板没配」就等于
        告诉他还要不要继续试）。"""
        seen = set()
        server = self.live()
        baseline = self.post(server, token="tok-wrong")
        seen.add((baseline[0], baseline[2]))
        variants = {
            "没有 Authorization 头": lambda: self.post(server, token=None),
            "空令牌": lambda: self.post(server, token=""),
            "别的认证方式": lambda: self.post(
                server, token=None, headers={"Authorization": f"Basic {TOKEN}"}
            ),
            "少一位": lambda: self.post(server, token=TOKEN[:-1]),
        }
        for name, call in variants.items():
            status, _body, raw = call()
            self.assertEqual(status, 403, name)
            seen.add((status, raw))
        # 没配令牌文件
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(server_mod.ENV_SERVICE_TOKENS, None)
            status, _body, raw = self.post(server)
            self.assertEqual(status, 403, "没配令牌文件")
            seen.add((status, raw))
        # 文件不见了 / 格式乱
        self.tokens_path.write_text("{ 坏的", encoding="utf-8")
        status, _body, raw = self.post(server)
        self.assertEqual(status, 403, "格式乱")
        seen.add((status, raw))
        self.tokens_path.unlink()
        status, _body, raw = self.post(server)
        self.assertEqual(status, 403, "文件没了")
        seen.add((status, raw))
        self.assertEqual(len(seen), 1, seen)

    def test_token_is_checked_before_anything_is_revealed(self):
        """没有令牌就问不出任何一个人的授权状态（身份枚举）。"""
        for union_id in (UID, STRANGER):
            with self.subTest(union_id):
                status, body, raw = self.ask(
                    token="tok-wrong", body={"service": SERVICE, "union_id": union_id}
                )
                self.assertEqual(status, 403)
                self.assertNotIn("allowed", raw.decode())
                self.assertNotIn("reason", raw.decode())
                self.assertIsInstance(body, dict)

    def test_token_rotation_takes_effect_without_restart(self):
        """令牌文件改了不用重启面板 —— 轮换时两边都能继续跑。"""
        server = self.live()
        self.assertEqual(self.post(server, token=OLD_TOKEN)[0], 200)
        self.write_tokens({SERVICE: {"tokens": [TOKEN]}})  # 删掉旧的
        self.assertEqual(self.post(server, token=OLD_TOKEN)[0], 403)
        self.assertEqual(self.post(server, token=TOKEN)[0], 200)


class FailDirectionTests(ServiceAccessEndpointBase):
    """**fail 的方向**：面板判不了 → 500 → 网关当成「不放行」。

    这和网关里已有的 `is_disabled()`（查库失败时**放行**）方向相反，而且**是有意的**：

      · 禁用名单是「黑名单」：查不到不该误伤正常人 → fail-open。
      · 授权是「白名单」：查不到就放行的话，面板一挂这道门就等于不存在 → fail-closed。

    两个函数在网关代码里紧挨着（见 `docs/collab/research/mlflow-gateway-patch.md`），
    后来者很容易觉得「两个查询一个放一个拒，不一致」而顺手统一掉 —— 统一成 fail-open
    就把整道门废了，统一成 fail-closed 会让禁用名单一抖就全员被踢。**别统一。**

    面板这半边能锁的是：任何判不了的情形都不得回 `allowed: true`，且必须是 5xx
    （网关那半边靠它自己的默认值 `{"allowed": False}` 兜住，那段代码不在本仓库）。
    """

    def broken_cases(self):
        return {
            "申请单存储格式不对": lambda: self.write_tickets(done_ticket(), schema=None),
            "申请单文件坏了": lambda: self.tickets_path.write_text("{", encoding="utf-8"),
            "离职记录坏了": lambda: self.offboard_path.write_text("{", encoding="utf-8"),
            "离职记录缺 records": lambda: self._write(self.offboard_path, {"rows": []}),
        }

    def test_every_failure_is_5xx_and_never_allows(self):
        for name, break_it in self.broken_cases().items():
            with self.subTest(name):
                # setUp 重来一次 → 新的 Backend、空缓存，所以坏文件一定会被读到
                self.setUp()
                break_it()
                status, body, raw = self.ask()
                self.assertGreaterEqual(status, 500, (name, status, body))
                self.assertNotIn("true", raw.decode().lower())

    def test_backend_blowing_up_is_5xx_not_a_grant(self):
        """判定里抛任何异常都不能变成放行（`_service_access` 兜的是 `Exception`）。"""
        for exc in (RuntimeError("炸了"), MemoryError(), ValueError("x")):
            with self.subTest(exc):
                with mock.patch.object(Backend, "service_access", side_effect=exc):
                    status, body, _ = self.ask()
                self.assertEqual(status, 500, body)
                self.assertNotIn("allowed", body)

    def test_missing_ticket_store_config_is_5xx(self):
        """没配申请单存储路径 = 判不了，不是「谁都没权限」，更不是放行。"""
        self.backend = Backend(people_path=str(self.people_path), platforms={})
        status, body, _ = self.ask()
        self.assertEqual(status, 500, body)
        self.assertNotIn("allowed", body)

    def test_unreadable_roster_is_5xx_too(self):
        """名册读不了**也**走 fail-closed（和面板别处的「读不了就当在册」相反）。

        理由在 `Backend.service_access` 里：离职记录是按**云子账号**记的，一个没有云
        子账号的人离职后那份记录里一条都没有 —— 而这个功能的立项理由恰恰是「最需要
        自建服务的人没有云子账号」。对这类人，唯一的离职信号就是他从名册里消失。
        所以「名册读不了就当在册」在这条链上等于「名册一坏，离职的人全部放行」。

        代价也是实打实的：名册坏了谁都进不去。这是有意选的那一边。
        """
        self.people_path.unlink()
        status, body, raw = self.ask()
        self.assertEqual(status, 500, body)
        self.assertNotIn("allowed", raw.decode())

    # ── 变异：把 fail-closed 改成 fail-open，必须有用例转红 ──

    def assert_fail_closed(self, backend):
        """「判不了的时候不放行」这条断言本身。真 Backend 传进来该绿，变异体该红。

        单独抽出来是为了能**拿变异体喂给同一段断言** —— 否则「这条守卫有没有牙」
        全靠读代码相信，而这正是审计点名要防的那种退化。
        """
        self.backend = backend
        self._server = None
        self.write_tickets(done_ticket(), schema=None)  # 判不了：存储格式不对
        status, body, raw = self.ask()
        self.assertGreaterEqual(status, 500, body)
        self.assertNotIn("allowed", raw.decode())
        self.assertNotIn("true", raw.decode().lower())

    def real_backend(self):
        return Backend(
            people_path=str(self.people_path),
            tickets_path=str(self.tickets_path),
            platforms={"aliyun": "阿里云"},
        )

    def test_the_guard_passes_on_the_real_backend(self):
        self.assert_fail_closed(self.real_backend())

    def test_the_guard_catches_a_fail_open_backend(self):
        """变异体：`Backend.service_access` 把异常吞掉、回「放行」。

        这正是「以后有人把 fail-closed 顺手统一成 fail-open」的样子。
        上面那条守卫必须当场红 —— 这里断言它**确实**红（抛 AssertionError）。
        """

        class FailOpenBackend(Backend):
            def service_access(self, *, union_id, service):  # noqa: D102
                try:
                    return super().service_access(union_id=union_id, service=service)
                except Exception:  # noqa: BLE001 — 变异体就是要吞掉
                    return sa.Decision(True)

        with self.assertRaises(AssertionError):
            self.assert_fail_closed(FailOpenBackend(**self._backend_kwargs()))

    def test_the_guard_catches_a_backend_that_downgrades_to_no_grant(self):
        """另一个变异体：判不了时不抛、回「没权限」。

        它比 fail-open 温和，但一样要红：`no_grant` 会让网关把人引去申请页，
        而他其实已经有单子了 —— 面板坏了却装成「你没申请过」，没人会去看日志。
        """

        class QuietBackend(Backend):
            def service_access(self, *, union_id, service):  # noqa: D102
                try:
                    return super().service_access(union_id=union_id, service=service)
                except Exception:  # noqa: BLE001
                    return sa.Decision(False, sa.NO_GRANT)

        with self.assertRaises(AssertionError):
            self.assert_fail_closed(QuietBackend(**self._backend_kwargs()))

    def _backend_kwargs(self):
        return {
            "people_path": str(self.people_path),
            "tickets_path": str(self.tickets_path),
            "platforms": {"aliyun": "阿里云"},
        }

    def test_the_500_branch_turns_a_crash_into_an_answer(self):
        """`_service_access` 里那段 `except Exception → 500` 不是装饰：`do_POST` 没有
        兜底，去掉它异常就穿出去、连接直接断 —— 网关侧看到的是「偶发断连」而不是一行
        500 日志（两者都不放行，但后者能排查）。

        所以这里不只断言状态码，还断言**连接是好的**（同一条连接之后还能继续问）。
        """
        server = self.live()
        with mock.patch.object(Backend, "service_access", side_effect=RuntimeError("炸了")):
            status, body, _ = self.post(server)
        self.assertEqual(status, 500, body)
        self.assertEqual(self.post(server)[0], 200, "500 之后服务器还得能继续答")

    # ── 限流：自己一张表 ──

    def test_gateway_traffic_is_not_throttled_by_the_pickup_quota(self):
        """**回归**：这条路曾经和取件接口共用一张计数表（120 次 / 5 分钟），而它是网关
        **每个请求**都要问一次的、且全部来自网关那一个 IP —— 共用的后果是几十个在线
        用户就能把配额填满，然后 429 → 网关 fail-closed → 所有人都进不去。

        现在两张表分开：取件配额打满，授权查询照常。
        """
        server = self.live()
        server_mod._pickup_tries["127.0.0.1"] = [time.time()] * (server_mod._PICKUP_TRIES + 50)
        for i in range(30):
            status, body, _ = self.post(server)
            self.assertEqual(status, 200, f"第 {i + 1} 次被取件的配额挡住了：{body}")

    def test_service_traffic_does_not_eat_the_pickup_quota(self):
        """反过来也一样：网关问一整天，不该把取件接口的配额吃掉。"""
        server = self.live()
        for _ in range(30):
            self.assertEqual(self.post(server)[0], 200)
        self.assertEqual(server_mod._pickup_tries.get("127.0.0.1", []), [])

    def test_a_burst_of_normal_questions_is_allowed(self):
        """网关的正常流量密度：`_SVC_TRIES` 是每分钟一千多次，连打一百次不该被拦。"""
        server = self.live()
        for i in range(100):
            status, _body, _ = self.post(server)
            self.assertEqual(status, 200, f"第 {i + 1} 次就被限流了")

    def test_token_guessing_gets_throttled(self):
        """猜令牌要被拦住：同一来源攒够坏令牌之后从 403 变 429。

        **只数失败**，所以正常流量不受影响 —— 上面那两条就是这个意思。
        """
        server = self.live()
        for _ in range(server_mod._SVC_BAD_MAX):
            self.assertEqual(self.post(server, token="tok-wrong")[0], 403)
        self.assertEqual(self.post(server, token="tok-wrong")[0], 429)
        # **但正确的令牌照样能问**：计数只在失败那条路上加，429 也只在失败那条路上回。
        # 否则同一个出口 IP 上有人乱试，就能把整台 MLflow 的授权查询打停
        # （网关和乱试的人共用一个公网地址是常态）
        self.assertEqual(self.post(server)[0], 200)
