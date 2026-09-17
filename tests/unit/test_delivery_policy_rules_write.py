"""「哪些云权限不能被申请」改成面板可写：`policies.write_rules` + `/api/admin/policies/rules`。

这是面板最危险的写接口 —— 这个文件决定员工能申请到什么云权限，写坏了整个权限列表
加载失败（员工看到的只是「打不开」，没人知道是谁什么时候写坏的）。所以按三道闸逐条锁：

  1. **先验后写**：`parse_rules` 过不了就抛，文件一个字节都不许动
  2. **不许放开平台自己的策略**（`SELF_POLICY`）：那等于让员工申请到平台的管理权限
  3. **`allow_custom` 接口改不动**：那个开关等于「自定义策略全放开」，而执行身份、
     发放身份用的都是自定义策略

外加：内置禁用是地板（文件怎么写都挖不掉）、存 N 次不累积、每次实质变更留台账。
离线，数据虚构。
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from delivery import alerts
from delivery import policies as pol
from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _WebSession, make_handler

ACC = "1000000000000001"
ADMIN = "admin@wuji.tech"
RULES_API = "/api/admin/policies/rules"
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abcd-1234-efgh"

PEOPLE = {
    "schema": "wuji-people@1",
    "people": [
        {
            "union_id": "on_admin",
            "name": "管理员",
            "email": ADMIN,
            "accounts": [{"platform": "aliyun", "account": ACC, "name": "boss"}],
        },
        {
            "union_id": "on_1",
            "name": "李四",
            "email": "li.si@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": ACC, "name": "lisi"}],
        },
    ],
}


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class WriteRulesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "policy-rules.json"
        self.log = self.dir / "policy-rules.log"

    def write(self, data, actor="on_admin"):
        """只要生效后的规则。`write_rules` 回的是 (规则, 本次放开的内置禁用项)。"""
        return self.write_full(data, actor)[0]

    def write_full(self, data, actor="on_admin"):
        rules, opened = pol.write_rules(str(self.path), data, actor=actor)
        return rules, opened

    def file_body(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def log_lines(self) -> list:
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines() if ln]

    # ── 闸 1：先验后写 ────────────────────────────────────────────────────
    BAD_BODIES = (
        {"deny": "AdministratorAccess"},  # 不是数组
        {"deny": ["ok", 7]},  # 元素不是字符串
        {"deny": [""]},  # 空名字
        {"deny": ["x" * 129]},  # 超长
        {"allow": {"a": 1}},
        {"max_days": {"low": 0}},
        {"max_days": {"low": 3651}},
        {"max_days": {"low": "7"}},
        {"max_days": {"low": True}},  # bool 是 int 的子类，必须单独挡
        {"max_days": {"critical": 7}},
        {"max_days": []},
        {"risk": {"AliyunOSSFullAccess": "extreme"}},
        {"risk": ["AliyunOSSFullAccess"]},
        {"allow_custom": "yes"},
        {"max_per_request": 0},
        {"max_per_request": 21},
        {"max_per_request": True},
        {"schema": "wuji-policy-rules@2"},
        {"denny": []},  # 拼错的字段：静默忽略的话，管理员以为自己禁用了
        {"deny": [], "extra": 1},
    )

    def test_invalid_rules_never_reach_the_disk(self):
        """验不过就抛，且**连文件都不许创建** —— 规则文件写坏＝整个权限列表加载失败。"""
        for bad in self.BAD_BODIES:
            with self.assertRaises(pol.PolicyError, msg=bad):
                self.write(bad)
            self.assertEqual(list(self.dir.iterdir()), [], bad)  # 连锁文件都还没建

    def test_a_body_that_is_not_an_object_is_rejected(self):
        """非对象的 body 要报「格式不对」，不能先 `dict()` 再验。

        曾经是 `parse_rules(dict(data))`：`dict([])` 静默变成 `{}`（＝把整份规则清空后
        落盘），`dict("x")` / `dict(None)` 抛的又是 `ValueError`/`TypeError` 而不是
        `PolicyError` —— HTTP 层只接 `DeliveryError`，那会变成 500 断连而不是一句报错。
        """
        for bad in ([], "deny", None, 7, [("deny", [])]):
            with self.assertRaises(pol.PolicyError, msg=bad):
                self.write(bad)
            self.assertEqual(list(self.dir.iterdir()), [], bad)

    def test_invalid_rules_do_not_disturb_an_existing_file(self):
        self.write({"deny": ["AliyunECSFullAccess"], "allow": ["team-reader"]})
        before = self.path.read_bytes()
        lines = len(self.log_lines())
        for bad in self.BAD_BODIES:
            with self.assertRaises(pol.PolicyError, msg=bad):
                self.write(bad)
            self.assertEqual(self.path.read_bytes(), before, bad)
        self.assertEqual(len(self.log_lines()), lines)  # 失败的尝试也不该进台账

    def test_a_broken_existing_file_is_not_overwritten(self):
        """文件已经坏了：停下来让人看，别拿一份新规则盖掉（盖掉就再也不知道原来写了什么）。"""
        self.path.write_text('{"deny": [', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(pol.PolicyError):
            self.write({"deny": ["AliyunECSFullAccess"]})
        self.assertEqual(self.path.read_bytes(), before)

    # ── 闸 2：不许放开平台自己的策略 ──────────────────────────────────────
    def test_cannot_open_the_platforms_own_policies(self):
        """放开这几把＝员工能申请到平台自己的管理权限（执行身份能给任何人任何权限）。"""
        for allow in (
            ["wuji-panel-executor"],
            ["WUJI-PANEL-EXECUTOR"],  # 大小写不敏感，否则换个写法就绕过去了
            ["wuji-oss-auto-lisi"],
            ["temp-ak-auto-20260101"],
            ["team-reader", "wuji-panel-issuer"],  # 混在正常条目里也要拦
        ):
            with self.assertRaises(pol.PolicyError, msg=allow) as caught:
                self.write({"allow": allow})
            self.assertIn("平台自己的策略", str(caught.exception))
            self.assertEqual(list(self.dir.iterdir()), [], allow)

    def test_ordinary_custom_policies_can_still_be_opened(self):
        """闸 2 只挡平台自己那几把，别顺手把正常的自定义策略也挡了。"""
        rules = self.write({"allow": ["team-data-reader", "wuji-data-shared"]})
        self.assertEqual(rules.denied("Custom", "team-data-reader"), "")
        self.assertEqual(rules.denied("Custom", "delivery-executor"), pol.CUSTOM_NOTE)

    # ── 闸 3：allow_custom 接口改不动 ─────────────────────────────────────
    def test_allow_custom_cannot_be_switched_on_through_this_path(self):
        """整体放开自定义策略 = 把执行身份、发放身份的策略也放进可申请列表。"""
        rules = self.write({"allow_custom": True, "deny": []})
        self.assertFalse(rules.allow_custom)
        self.assertFalse(self.file_body()["allow_custom"])
        self.assertFalse(pol.load_rules(str(self.path)).allow_custom)
        self.assertEqual(rules.denied("Custom", "delivery-executor"), pol.CUSTOM_NOTE)

    def test_allow_custom_already_on_is_not_silently_switched_off(self):
        """反过来也不行：接口不碰这个开关，谁在服务器上开的谁去关。
        悄悄关掉的话，一批本来能申请的自定义策略会突然全部消失，且查不到原因。"""
        self.path.write_text(
            json.dumps({"schema": pol.RULES_SCHEMA, "allow_custom": True}), encoding="utf-8"
        )
        rules = self.write({"allow_custom": False, "deny": ["AliyunECSFullAccess"]})
        self.assertTrue(rules.allow_custom)
        self.assertTrue(self.file_body()["allow_custom"])

    # ── 地板 ──────────────────────────────────────────────────────────────
    FLOOR = (
        "AdministratorAccess",
        "AliyunSTSAssumeRoleAccess",
        "AliyunRAMFullAccess",
        "IAMFullAccess",
        "AliyunKMSFullAccess",
        "AliyunBSSOrderAccess",
        "PowerUserAccess",
    )

    def test_builtin_deny_cannot_be_dug_out_by_writing_an_empty_deny(self):
        rules = self.write({"deny": [], "allow": [], "risk": {}, "max_days": {}})
        self.assertEqual(self.file_body()["deny"], [])
        for name in self.FLOOR:
            self.assertTrue(rules.denied("System", name), name)
            self.assertTrue(pol.load_rules(str(self.path)).denied("System", name), name)

    def test_a_wildcard_in_allow_does_not_open_everything(self):
        """allow 是**逐条精确名**，不是通配符。写 `*` 进去只会白名单一条名叫 `*` 的策略；
        要是哪天改成按通配符匹配，这条会红 —— 那等于一行配置拆掉所有护栏。"""
        rules = self.write({"allow": ["*", "Aliyun*"]})
        for name in self.FLOOR:
            self.assertTrue(rules.denied("System", name), name)
        self.assertEqual(rules.denied("Custom", "delivery-executor"), pol.CUSTOM_NOTE)

    def test_opening_a_builtin_denied_policy_needs_its_exact_name(self):
        """设计上留的口子：逐条写进 allow 才能放开内置禁用项（前端还要求手打一遍名字）。
        锁住「只开这一条」—— 开一条不能顺带把同族的别的也开了。"""
        rules = self.write({"allow": ["AliyunSTSAssumeRoleAccess"]})
        self.assertEqual(rules.denied("System", "AliyunSTSAssumeRoleAccess"), "")
        self.assertTrue(rules.denied("System", "AliyunSTSAssumeRoleAccessX"))
        self.assertTrue(rules.denied("System", "AdministratorAccess"))

    # ── 拆护栏要出声 ──────────────────────────────────────────────────────
    def test_opening_a_builtin_denied_policy_is_reported_back(self):
        """口子留着，但要出声：放开内置禁用项时，第二个返回值列出放开了哪几条，
        调用方据此发告警。以前这要 SSH 上服务器改文件，现在一个管理员会话就够了。"""
        _, opened = self.write_full({"allow": ["AdministratorAccess"]})
        self.assertEqual(opened, ["AdministratorAccess"])
        # 内置**产品线**（DEFAULT_DENY_FAMILIES）挡的也算：BSSOrderAccess 不是 FullAccess，
        # 只被产品线那条挡着，漏掉它等于放开账单权限没人知道
        _, opened = self.write_full({"allow": ["AdministratorAccess", "AliyunBSSOrderAccess"]})
        self.assertEqual(opened, ["AliyunBSSOrderAccess"])  # 只报**本次新增**的那条

    def test_an_ordinary_save_reports_nothing(self):
        """每次保存都发告警的话，告警就没人看了 —— 只有拆护栏才出声。"""
        for data in (
            {"deny": ["AliyunECSFullAccess"]},
            {"allow": ["team-data-reader"]},  # 普通自定义策略，不在内置禁用里
            {"allow": ["team-data-reader"], "max_days": {"high": 7}},
            {"risk": {"team-data-reader": "low"}, "allow": ["team-data-reader"]},
        ):
            self.assertEqual(self.write_full(data)[1], [], data)

    def test_resaving_an_already_open_policy_does_not_report_again(self):
        """告警只针对**这次新增**的：否则改任何一条不相干的规则都会把旧的重报一遍。"""
        self.assertEqual(
            self.write_full({"allow": ["AdministratorAccess"]})[1], ["AdministratorAccess"]
        )
        rules, opened = self.write_full(
            {"allow": ["AdministratorAccess"], "deny": ["AliyunECSFullAccess"]}
        )
        self.assertEqual(opened, [])
        self.assertEqual(rules.denied("System", "AdministratorAccess"), "")

    # ── 落盘与累积 ────────────────────────────────────────────────────────
    def test_saved_file_is_private_and_holds_only_the_writable_part(self):
        self.write({"deny": ["AliyunECSFullAccess"], "risk": {"Team-Reader": "low"}})
        self.assertEqual(mode(self.path), 0o600)
        body = self.file_body()
        self.assertEqual(
            set(body),
            {"schema", "deny", "allow", "risk", "max_days", "allow_custom", "max_per_request"},
        )
        self.assertEqual(body["schema"], pol.RULES_SCHEMA)
        self.assertEqual(body["deny"], ["AliyunECSFullAccess"])  # 内置那 21 条不写进文件
        self.assertEqual(body["risk"], {"team-reader": "low"})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.suffix == ".tmp"), [])

    def test_saving_the_same_rules_three_times_does_not_grow_the_file(self):
        """`parse_rules` 会把 21 条内置禁用前置进 deny。原样写回去就会 21→42→84，
        几次之后文件里全是重复项，谁也看不出哪条是人写的。"""
        for _ in range(3):
            rules = self.write({"deny": ["AliyunECSFullAccess"]})
            self.assertEqual(self.file_body()["deny"], ["AliyunECSFullAccess"])
            self.assertEqual(len(rules.deny), len(pol.DEFAULT_DENY) + 1)

    def test_posting_back_what_the_ui_showed_does_not_grow_the_file(self):
        """真实回路是「GET 出来一份视图 → 改一条 → 原样 POST 回来」。视图里的 `deny`
        是**合并后**的（含 21 条内置），所以这条才是那个翻倍 bug 的实际触发路径。"""
        self.write({"deny": ["AliyunECSFullAccess"]})
        for _ in range(3):
            view = pol.rules_view(pol.load_rules(str(self.path)))
            self.write(
                {
                    "deny": view["deny"],  # 合并后的那份，故意不用 file_deny
                    "allow": view["allow"],
                    "risk": view["risk"],
                    "max_days": view["max_days"],
                    "max_per_request": view["max_per_request"],
                }
            )
            self.assertEqual(self.file_body()["deny"], ["AliyunECSFullAccess"])

    def test_concurrent_writes_leave_a_valid_file(self):
        """两个管理员同时保存：可以后写的赢，但绝不能写出一个解析不了的文件 ——
        那会让整个权限列表打不开。"""
        names = [f"AliyunSvc{n}FullAccess" for n in range(8)]
        errors = []
        start = threading.Barrier(len(names))

        def worker(name):
            try:
                start.wait(timeout=5)
                self.write({"deny": [name]})
            except Exception as exc:  # noqa: BLE001 — 线程里的异常要带回主线程
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertIn(self.file_body()["deny"], [[n] for n in names])
        self.assertTrue(pol.load_rules(str(self.path)).denied("System", "AdministratorAccess"))

    # ── 台账 ──────────────────────────────────────────────────────────────
    def test_log_records_who_added_and_removed_what(self):
        self.write({"deny": ["AliyunECSFullAccess"]}, actor="on_admin")
        self.write({"deny": ["AliyunOSSFullAccess"], "allow": ["team-reader"]}, actor="on_boss")
        first, second = self.log_lines()
        self.assertEqual(first["actor"], "on_admin")
        self.assertEqual(first["deny_added"], ["AliyunECSFullAccess"])
        self.assertNotIn("deny_removed", first)
        self.assertEqual(second["actor"], "on_boss")
        self.assertEqual(second["deny_added"], ["AliyunOSSFullAccess"])
        self.assertEqual(second["deny_removed"], ["AliyunECSFullAccess"])
        self.assertEqual(second["allow_added"], ["team-reader"])
        self.assertTrue(second["at"])
        self.assertEqual(mode(self.log), 0o600)  # 台账和规则同级别，别让同机其他账号读

    def test_log_sits_next_to_the_rules_file(self):
        self.write({"deny": ["AliyunECSFullAccess"]})
        self.assertTrue(self.log.exists())
        self.assertEqual(self.log.parent, self.path.parent)

    def test_unchanged_save_writes_no_line(self):
        """点两次保存不该在台账里留两条 —— 台账要能一眼看出「谁真的改了什么」。"""
        self.write({"deny": ["AliyunECSFullAccess"]})
        self.write({"deny": ["AliyunECSFullAccess"]})
        self.assertEqual(len(self.log_lines()), 1)

    LOG_KEYS = {
        "at",
        "actor",
        "deny_added",
        "deny_removed",
        "allow_added",
        "allow_removed",
        "max_days",
        "risk_changed",
    }

    def test_log_holds_policy_names_only(self):
        """台账不是请求体的快照：只记谁改的、改了哪些策略、天数从多少变成多少。"""
        self.write(
            {
                "deny": ["AliyunECSFullAccess"],
                "risk": {"AliyunOSSFullAccess": "low"},
                "max_days": {"high": 7},
                "allow_custom": True,
                "max_per_request": 3,
            }
        )
        line = self.log_lines()[0]
        self.assertLessEqual(set(line), self.LOG_KEYS)
        raw = self.log.read_text(encoding="utf-8")
        for leak in ("allow_custom", "max_per_request", "schema"):
            self.assertNotIn(leak, raw, leak)
        # 内置那 21 条既没加也没删，不该出现在台账里（出现＝又在原样写回内置项了）
        self.assertNotIn("AdministratorAccess", raw)

    def test_days_and_risk_changes_say_what_changed(self):
        """只改天数 / 风险等级也要记清楚改成了什么。只记「某人改了点什么」的台账
        和没记一样 —— 事后追一次就知道了，而追的时候往往已经出事了。"""
        self.write({"max_days": {"high": 30}, "risk": {"team-reader": "low"}})
        self.write({"max_days": {"high": 7}, "risk": {"team-reader": "high"}}, actor="on_boss")
        line = self.log_lines()[-1]
        self.assertEqual(line["actor"], "on_boss")
        self.assertEqual(line["max_days"]["before"]["high"], 30)
        self.assertEqual(line["max_days"]["after"]["high"], 7)
        self.assertEqual(line["risk_changed"], {"team-reader": "high"})
        self.assertLessEqual(set(line), self.LOG_KEYS)

    def test_risk_log_only_lists_the_entries_that_moved(self):
        """风险表可能有几十条：整表抄进台账的话，真正变的那条会被淹掉。"""
        base = {"a-policy": "low", "b-policy": "low", "c-policy": "low"}
        self.write({"risk": base})
        self.write({"risk": {**base, "b-policy": "high"}})
        line = self.log_lines()[-1]
        self.assertEqual(line["risk_changed"], {"b-policy": "high"})
        self.assertNotIn("a-policy", json.dumps(line))

    def test_removing_a_risk_entry_is_recorded_too(self):
        self.write({"risk": {"a-policy": "low", "b-policy": "low"}})
        self.write({"risk": {"a-policy": "low"}})
        self.assertEqual(list(self.log_lines()[-1]["risk_changed"]), ["b-policy"])

    def test_still_no_line_when_nothing_moved(self):
        """加了 max_days / risk 两个维度之后，「没变化不写行」不能被顺手破坏。"""
        data = {
            "deny": ["AliyunECSFullAccess"],
            "allow": ["team-reader"],
            "risk": {"team-reader": "low"},
            "max_days": {"high": 7},
            "max_per_request": 3,
        }
        self.write(data)
        for _ in range(3):
            self.write(dict(data))
        self.assertEqual(len(self.log_lines()), 1)


class _Live:
    def __init__(self, backend):
        self.store = Store()
        handler = make_handler(
            PlatformRegistry.load(),
            self.store,
            app_id="cli_demo",
            app_secret="s",
            base_url="http://127.0.0.1",
            backend=backend,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, *, cookie="", payload=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        head = dict(headers or {})
        if cookie:
            head["Cookie"] = f"{COOKIE_NAME}={cookie}"
        body = raw
        if payload is not None:
            body = json.dumps(payload).encode()
        if body is not None:
            head.setdefault("Content-Type", "application/json")
            if headers is None:  # 显式给了头就按给的发：CSRF 用例要试「缺 X-Panel-Request」
                head.setdefault("X-Panel-Request", "1")
        conn.request(method, path, body=body, headers=head)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(data or b"null")
        except ValueError:
            return resp.status, data.decode(errors="replace")


class PolicyRulesApiTests(unittest.TestCase):
    """`GET/POST /api/admin/policies/rules`：管理员专属、过 CSRF、坏 body 不落盘。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "people.json").write_text(
            json.dumps(PEOPLE, ensure_ascii=False), encoding="utf-8"
        )
        (self.dir / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), "utf-8")
        self.path = self.dir / "policy-rules.json"
        self.log = self.dir / "policy-rules.log"
        self.backend = self.make_backend()

    def make_backend(self, **kw):
        opts = dict(
            people_path=str(self.dir / "people.json"),
            bindings_path=str(self.dir / "bindings.json"),
            admins_path=str(self.dir / "admins.json"),
            policy_rules_path=str(self.path),
            platforms={"aliyun": "阿里云"},
        )
        opts.update(kw)
        return Backend(**opts)

    def login(self, live, uid, email=""):
        sid = f"sid-{uid}"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(
                open_id="ou_x", union_id=uid, name="某人", email=email, enterprise_email=email
            )
        )
        return sid

    def post(self, live, sid, payload, **kw):
        return live.request("POST", RULES_API, cookie=sid, payload=payload, **kw)

    def test_admin_reads_the_rules_with_the_builtin_floor_marked(self):
        """GET 必须真的走到它自己的 handler。

        曾经走不到：`_is_requests_path()` 认 `/api/admin/policies` 前缀，排在这条判断
        **前面**，于是整条 GET 被申请单接口接走、回 404「没有这个接口」。POST 当时没事，
        纯属 `do_POST` 里两个判断的顺序正好反过来 —— 所以这条要单独锁，别再被前缀抢走。
        """
        with _Live(self.backend) as live:
            status, body = live.request(
                "GET", RULES_API, cookie=self.login(live, "on_admin", ADMIN)
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["rules"]["builtin_deny"], list(pol.DEFAULT_DENY))
            self.assertEqual(
                body["rules"]["builtin_deny_families"], list(pol.DEFAULT_DENY_FAMILIES)
            )
            self.assertEqual(body["rules"]["file_deny"], [])  # 文件还没写过
            self.assertEqual(body["path"], str(self.path))

    def test_only_admins_get_in(self):
        with _Live(self.backend) as live:
            employee = self.login(live, "on_1", "li.si@wuji.tech")
            self.assertEqual(live.request("GET", RULES_API, cookie=employee)[0], 403)
            self.assertEqual(live.request("GET", RULES_API)[0], 401)
            self.assertEqual(self.post(live, employee, {"deny": ["X"]})[0], 403)
            self.assertEqual(self.post(live, "", {"deny": ["X"]})[0], 401)
            self.assertFalse(self.path.exists())  # 被拒的请求一律不落盘
            self.assertFalse(self.log.exists())

    def test_admin_saves_and_gets_the_effective_rules_back(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"deny": ["AliyunECSFullAccess"]})
            self.assertEqual(status, 200, body)
            rules = body["rules"]
            # 回的是**生效后**的规则，不是「保存成功」
            self.assertEqual(rules["file_deny"], ["AliyunECSFullAccess"])
            self.assertIn("AliyunECSFullAccess", rules["deny"])
            self.assertEqual(rules["builtin_deny"], list(pol.DEFAULT_DENY))
            self.assertEqual(json.loads(self.path.read_text())["deny"], ["AliyunECSFullAccess"])
            # 台账记的是登录者的 union_id
            self.assertEqual(json.loads(self.log.read_text().splitlines()[0])["actor"], "on_admin")

    def test_second_save_is_visible_immediately(self):
        """缓存按文件 mtime 失效。两次保存挨得很近时若失效不了，管理员会看到自己刚
        改掉的那条又回来了，然后再点一次 —— 台账里于是多出一堆来回改的噪音。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            self.post(live, admin, {"deny": ["AliyunECSFullAccess"]})
            _, body = self.post(live, admin, {"deny": ["AliyunOSSFullAccess"]})
            # 回执走的是 backend.policy_rules()（带 mtime 的缓存），不是刚才写的那份内存对象
            self.assertEqual(body["rules"]["file_deny"], ["AliyunOSSFullAccess"])
            self.assertNotIn("AliyunECSFullAccess", body["rules"]["deny"])
            self.assertEqual(json.loads(self.path.read_text())["deny"], ["AliyunOSSFullAccess"])

    def test_opening_a_platform_policy_is_refused_end_to_end(self):
        """闸 2 必须在 HTTP 这一层也拦住 —— 单元层过了不代表这条路通到了那里。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"allow": ["wuji-panel-executor"]})
            self.assertEqual(status, 400, body)
            self.assertIn("平台自己的策略", body["error"])
            self.assertFalse(self.path.exists())
            self.assertFalse(self.log.exists())

    def test_opening_a_builtin_denied_policy_alerts(self):
        """拆护栏要当场有人看见：谁、放开了哪几条，都得在告警文本里。"""
        sent = []
        with (
            _Live(self.backend) as live,
            mock.patch.dict(os.environ, {alerts.ENV_WEBHOOK: HOOK, alerts.ENV_SECRET: "s3cret"}),
            mock.patch.object(alerts, "send_feishu", lambda text, **kw: sent.append((text, kw))),
        ):
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"allow": ["AdministratorAccess"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(len(sent), 1)
            text, kw = sent[0]
            self.assertIn("AdministratorAccess", text)
            self.assertIn("某人", text)  # 会话里的名字，认得出是谁干的
            self.assertEqual(kw["webhook"], HOOK)
            self.assertEqual(kw["secret"], "s3cret")
            # 普通改动不告警，否则这条告警很快就没人看了
            self.assertEqual(self.post(live, admin, {"deny": ["AliyunECSFullAccess"]})[0], 200)
            self.assertEqual(len(sent), 1)

    def test_a_failed_alert_does_not_undo_the_save(self):
        """规则已经落盘了，告警只是通知。这里再抛出去的话，管理员会以为没保存成功、
        然后再点一次 —— 而规则其实已经改了两遍。"""

        def boom(text, **kw):
            raise RuntimeError("webhook 挂了")

        with (
            _Live(self.backend) as live,
            mock.patch.dict(os.environ, {alerts.ENV_WEBHOOK: HOOK, alerts.ENV_SECRET: "s3cret"}),
            mock.patch.object(alerts, "send_feishu", boom),
        ):
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"allow": ["AdministratorAccess"]})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["rules"]["allow"], ["AdministratorAccess"])
        self.assertEqual(json.loads(self.path.read_text())["allow"], ["AdministratorAccess"])
        self.assertIn("AdministratorAccess", self.log.read_text(encoding="utf-8"))

    def test_saving_works_without_an_alert_address_configured(self):
        """没配告警地址是常态（开发机、刚部署）：不能因此保存不了规则。"""
        with (
            _Live(self.backend) as live,
            mock.patch.dict(os.environ, {alerts.ENV_WEBHOOK: "", alerts.ENV_SECRET: ""}),
        ):
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"allow": ["AdministratorAccess"]})
            self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(self.path.read_text())["allow"], ["AdministratorAccess"])

    def test_allow_custom_cannot_be_flipped_through_http(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"allow_custom": True})
            self.assertEqual(status, 200, body)
            self.assertFalse(body["rules"]["allow_custom"])
            self.assertFalse(json.loads(self.path.read_text())["allow_custom"])

    def test_bad_bodies_are_400_and_change_nothing(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            self.post(live, admin, {"deny": ["AliyunECSFullAccess"]})
            before = self.path.read_bytes()
            for bad in (
                {"deny": "AdministratorAccess"},
                {"max_days": {"low": 0}},
                {"risk": {"x": "extreme"}},
                {"denny": []},
                {"schema": "wuji-policy-rules@2"},
                [],
            ):
                status, body = self.post(live, admin, bad)
                self.assertEqual(status, 400, (bad, body))
                self.assertEqual(self.path.read_bytes(), before, bad)
            # 空 body 和超大 body 也得挡在解析之前
            self.assertEqual(live.request("POST", RULES_API, cookie=admin, raw=b"")[0], 400)
            huge = json.dumps({"deny": [f"Aliyun{n}FullAccess" for n in range(400)]}).encode()
            self.assertEqual(live.request("POST", RULES_API, cookie=admin, raw=huge)[0], 400)
            self.assertEqual(self.path.read_bytes(), before)

    def test_csrf_headers_are_required(self):
        """只靠 Cookie 的话，管理员点开一个外部页面就可能被替他改掉权限规则。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            for headers in (
                {"Content-Type": "application/json"},  # 缺 X-Panel-Request
                {"Content-Type": "text/plain", "X-Panel-Request": "1"},
                {
                    "Content-Type": "application/json",
                    "X-Panel-Request": "1",
                    "Sec-Fetch-Site": "cross-site",
                },
                {
                    "Content-Type": "application/json",
                    "X-Panel-Request": "1",
                    "Origin": "https://evil.example.com",
                },
            ):
                status, _ = self.post(live, admin, {"deny": ["X"]}, headers=headers)
                self.assertEqual(status, 403, headers)
            self.assertFalse(self.path.exists())
            self.assertFalse(self.log.exists())

    def test_without_a_configured_path_writes_are_404(self):
        """没配规则文件路径时不能凭空猜一个位置写出去 —— 写到一个没人读的地方，
        管理员以为规则生效了，实际什么都没变。"""
        with _Live(self.make_backend(policy_rules_path=None)) as live:
            admin = self.login(live, "on_admin", ADMIN)
            status, body = self.post(live, admin, {"deny": ["AliyunECSFullAccess"]})
            self.assertEqual(status, 404, body)
            self.assertFalse(self.path.exists())
            self.assertEqual(
                sorted(p.name for p in self.dir.iterdir()), ["admins.json", "people.json"]
            )

    def test_saved_rules_actually_close_the_door(self):
        """整条链的意义在这：存完之后 `denied()` 真的开始拒这条策略。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", ADMIN)
            self.post(live, admin, {"deny": ["AliyunOSS*"]})
            rules = pol.load_rules(str(self.path))
            self.assertTrue(rules.denied("System", "AliyunOSSFullAccess"))
            self.assertTrue(rules.denied("System", "AdministratorAccess"))  # 地板还在
            self.assertEqual(rules.denied("System", "AliyunECSReadOnlyAccess"), "")


if __name__ == "__main__":
    unittest.main()
