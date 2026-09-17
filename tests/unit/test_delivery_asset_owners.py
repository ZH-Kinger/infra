"""资源归属：谁的机器是人工记的，这张表就是唯一的事实来源。

分两层锁：
  · 表本身（`assets.owner_key` / `load_owners` / `set_owner`）—— 键怎么拼、坏表怎么办、
    并发指派会不会互相覆盖、文件里能出现什么、权限是不是 600。
  · 面板接口（`POST /api/admin/assets/owner`）—— 谁能指派、指给谁算数、CSRF。

为什么这么较真：资源中心不告诉我们一台机器是谁的（实测归属类标签一个都没有），
所以这张表**指过的才算，没指过就是「未指定」，绝不猜**。表读错、指错人、被别人改，
后果都是「拿着错的归属去问责和分摊成本」。离线，数据虚构。
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from delivery import assets
from delivery.errors import DeliveryError
from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import (
    COOKIE_NAME,
    Backend,
    Store,
    _owned_by,
    _WebSession,
    make_handler,
)

ACC = "1000000000000001"
ME = "li.si@wuji.tech"
KEY = f"aliyun/{ACC}/i-2"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class OwnerKeyTests(unittest.TestCase):
    def test_key_is_platform_account_resource(self):
        self.assertEqual(assets.owner_key("aliyun", ACC, "i-2"), KEY)
        self.assertEqual(assets.owner_key(" aliyun ", f" {ACC} ", " i-2 "), KEY)

    def test_any_missing_part_raises(self):
        """三段缺一就不是一个能认回来的资源。少了账号那一段，两个账号里同名的
        `i-1` 会串成一条，归属直接指错机器。"""
        for bad in (
            ("", ACC, "i-2"),
            ("aliyun", "", "i-2"),
            ("aliyun", ACC, ""),
            ("aliyun", ACC, "   "),
            ("aliyun", None, "i-2"),
            (None, None, None),
        ):
            with self.assertRaises(assets.AssetError, msg=bad):
                assets.owner_key(*bad)


class OwnerTableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "asset-owners.json"

    def assign(self, key=KEY, *, email=ME, name="李四", note="", actor="on_admin"):
        return assets.set_owner(str(self.path), key, email=email, name=name, note=note, actor=actor)

    # ── 读 ────────────────────────────────────────────────────────────────
    def test_missing_table_is_empty_not_an_error(self):
        """还没指派过任何东西是正常的初始状态，不是故障：面板不能因此 500。"""
        self.assertEqual(assets.load_owners(str(self.path)), {})
        self.assertEqual(assets.load_owners(None), {})
        self.assertEqual(assets.load_owners(""), {})

    def test_broken_table_raises_instead_of_reading_as_empty(self):
        """坏表绝不能读成空表。读成空 = 页面把指派过的资源全显示成「未指定」，
        看的人只会以为没人指派过，然后再指一遍 / 或者干脆不信这张表了。"""
        for bad in ("{", "", "[]", "null", '"owners"', '{"owners": []}', '{"owners": null}', "{}"):
            self.path.write_text(bad, encoding="utf-8")
            with self.assertRaises(assets.AssetError, msg=bad):
                assets.load_owners(str(self.path))

    def test_error_message_does_not_dump_the_table(self):
        """报错信息会进日志和接口响应，别把整张归属表（含全员邮箱）带出去。"""
        self.path.write_text('{"owners": {"a/b/c": {"email": "li.si@wuji.tech"}}, ', "utf-8")
        with self.assertRaises(assets.AssetError) as caught:
            assets.load_owners(str(self.path))
        self.assertNotIn(ME, str(caught.exception))

    # ── 写 ────────────────────────────────────────────────────────────────
    def test_assign_writes_private_file_with_only_owners(self):
        self.assign(note="训练机")
        self.assertEqual(mode(self.path), 0o600)  # 表里是全员邮箱，别让同机其他账号读到
        data = read(self.path)
        self.assertEqual(set(data), {"owners"})  # 归属表只放归属，别顺手塞别的
        entry = data["owners"][KEY]
        self.assertEqual(set(entry), {"email", "name", "note", "at", "by"})
        self.assertEqual(entry["email"], ME)
        self.assertEqual(entry["name"], "李四")
        self.assertEqual(entry["note"], "训练机")
        self.assertEqual(entry["by"], "on_admin")  # 谁指的要留痕
        self.assertTrue(entry["at"])

    def test_email_is_normalized_to_lowercase(self):
        """名册里写 Li.Si@，登录带回 li.si@，是同一个人。存之前就归一，
        免得匹配那头每处都要记得转小写。"""
        self.assign(email="  Li.Si@WUJI.Tech  ")
        self.assertEqual(read(self.path)["owners"][KEY]["email"], ME)

    def test_assigned_resource_shows_up_for_that_person_end_to_end(self):
        """写进去的能被看的人匹配上（大小写各写各的也要匹配上）。"""
        self.assign(email="Li.Si@WUJI.Tech")
        data = {
            "captured_at": "x",
            "accounts": [
                {
                    "platform": "aliyun",
                    "account": ACC,
                    "resources": [{"type": "ACS::ECS::Instance", "id": "i-2", "name": "web"}],
                }
            ],
        }
        view = assets.summary_view(
            data,
            scopes={("aliyun", ACC)},
            labels=lambda p, a: f"{p} {a}",
            owners=assets.load_owners(str(self.path)),
            viewer_email="LI.SI@wuji.tech",
        )
        self.assertEqual([r["id"] for r in view["accounts"][0]["resources"]], ["i-2"])

    def test_long_name_and_note_are_truncated(self):
        """备注是管理员随手填的自由文本，不设上限的话一次粘贴就能把这张表撑大。"""
        self.assign(name="名" * 500, note="备" * 5000)
        entry = read(self.path)["owners"][KEY]
        self.assertLessEqual(len(entry["name"]), 64)
        self.assertLessEqual(len(entry["note"]), 200)

    def test_clearing_removes_the_row_entirely(self):
        """取消指派要真的从表里消失，而不是留一条 email 为空的记录 ——
        留着的话「没指过」和「指过又取消」在页面上长得一样，还会被当成有人认领过。"""
        other = f"aliyun/{ACC}/i-9"
        self.assign()
        self.assign(other, email="wang.wu@wuji.tech", name="王五")
        self.assign(email="")
        owners = read(self.path)["owners"]
        self.assertNotIn(KEY, owners)
        self.assertIn(other, owners)  # 只取消这一条，别人的不受影响
        self.assertEqual(assets.load_owners(str(self.path)), owners)

    def test_clearing_something_never_assigned_is_a_noop(self):
        self.assign(email="")
        self.assertEqual(read(self.path), {"owners": {}})

    def test_reassign_overwrites_in_place(self):
        self.assign()
        self.assign(email="wang.wu@wuji.tech", name="王五")
        owners = read(self.path)["owners"]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[KEY]["email"], "wang.wu@wuji.tech")
        self.assertEqual(owners[KEY]["name"], "王五")

    def test_unrelated_top_level_keys_are_dropped(self):
        """有人手工往表里塞了别的东西（或旧格式残留）：写回去之后只留 owners。"""
        self.path.write_text(
            json.dumps({"owners": {KEY: {"email": ME}}, "captured_at": "x", "note": "手改的"}),
            encoding="utf-8",
        )
        self.assign(f"aliyun/{ACC}/i-9", email="wang.wu@wuji.tech", name="王五")
        data = read(self.path)
        self.assertEqual(set(data), {"owners"})
        self.assertEqual(set(data["owners"]), {KEY, f"aliyun/{ACC}/i-9"})

    def test_broken_table_is_not_clobbered_by_a_write(self):
        """表坏了就停下来让人看，别拿一条新指派把之前所有归属覆盖掉。"""
        self.path.write_text('{"owners": {"a/b/c": ', encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(assets.AssetError):
            self.assign()
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_assignments_do_not_lose_each_other(self):
        """管理员开两个页面同时指派：读-改-写没上锁的话后写的会把先写的抹掉，
        表面上还「指派成功」了，没人会发现少了一条。"""
        keys = [f"aliyun/{ACC}/i-{n}" for n in range(12)]
        errors = []
        start = threading.Barrier(len(keys))

        def worker(key):
            try:
                start.wait(timeout=5)
                self.assign(key, email=f"{key[-3:].strip('-')}@wuji.tech", name="谁")
            except Exception as exc:  # noqa: BLE001 — 线程里的异常要带回主线程
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(read(self.path)["owners"]), sorted(keys))

    def test_no_leftovers_from_the_atomic_write(self):
        """原子替换用的临时文件不能留在 identity/ 里：它和正式表同样含邮箱。"""
        self.assign()
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir() if p.name.startswith(".")),
            [],
        )


PEOPLE = {
    "schema": "wuji-people@1",
    "people": [
        {
            "union_id": "on_admin",
            "name": "管理员",
            "email": "admin@wuji.tech",
            "accounts": [{"platform": "aliyun", "account": ACC, "name": "boss"}],
        },
        {
            "union_id": "on_1",
            "name": "李四",
            "email": ME,
            "accounts": [{"platform": "aliyun", "account": ACC, "name": "lisi"}],
        },
        {
            "union_id": "on_2",
            "name": "老张",
            "email": "shared@wuji.tech",
            "email_collision": True,
            "accounts": [{"platform": "aliyun", "account": ACC, "name": "zhang"}],
        },
        {"union_id": "on_3", "name": "双胞胎甲", "email": "twin@wuji.tech"},
        {"union_id": "on_4", "name": "双胞胎乙", "email": "twin@wuji.tech"},
    ],
}
ASSETS = {
    "captured_at": "2026-09-15T10:00:00+08:00",
    "accounts": [
        {
            "platform": "aliyun",
            "account": ACC,
            "resources": [
                {"type": "ACS::ECS::Instance", "id": "i-1", "name": "secret-db"},
                {"type": "ACS::ECS::Instance", "id": "i-2", "name": "web"},
            ],
        }
    ],
}
OWNER_API = "/api/admin/assets/owner"


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

    def request(self, method, path, *, cookie="", payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        head = dict(headers or {})
        if cookie:
            head["Cookie"] = f"{COOKIE_NAME}={cookie}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            head.setdefault("Content-Type", "application/json")
            if headers is None:  # 显式给了头就按给的发：CSRF 用例要试「缺 X-Panel-Request」
                head.setdefault("X-Panel-Request", "1")
        conn.request(method, path, body=body, headers=head)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw or b"null")
        except ValueError:
            return resp.status, raw.decode(errors="replace")


class _PanelBase(unittest.TestCase):
    """一个临时目录当 identity/：名册、管理员名单、资产快照、归属表。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "people.json").write_text(
            json.dumps(PEOPLE, ensure_ascii=False), encoding="utf-8"
        )
        (self.dir / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), "utf-8")
        (self.dir / "assets.json").write_text(json.dumps(ASSETS), encoding="utf-8")
        self.owners_path = self.dir / "asset-owners.json"
        self.backend = self.make_backend()

    def make_backend(self, **kw):
        opts = dict(
            people_path=str(self.dir / "people.json"),
            bindings_path=str(self.dir / "bindings.json"),
            admins_path=str(self.dir / "admins.json"),
            assets_path=str(self.dir / "assets.json"),
            platforms={"aliyun": "阿里云", "volcano": "火山引擎"},
        )
        opts.update(kw)
        return Backend(**opts)

    def write_owners(self, owners: dict):
        self.owners_path.write_text(
            json.dumps({"owners": owners}, ensure_ascii=False), encoding="utf-8"
        )

    def login(self, live, uid, email=""):
        sid = f"sid-{uid}"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(
                open_id="ou_x", union_id=uid, name="某人", email=email, enterprise_email=email
            )
        )
        return sid


class AssetOwnerApiTests(_PanelBase):
    """POST /api/admin/assets/owner：管理员专属、指给的人必须在名册里认得准。"""

    def assign(self, live, sid, **payload):
        body = {"platform": "aliyun", "account": ACC, "id": "i-2"}
        body.update(payload)
        return live.request("POST", OWNER_API, cookie=sid, payload=body)

    def owners(self):
        return assets.load_owners(str(self.owners_path))

    def test_owner_table_sits_next_to_the_asset_snapshot(self):
        self.assertEqual(self.backend.asset_owners_path, str(self.owners_path))
        self.assertIsNone(Backend().asset_owners_path)  # 没配快照就没有归属表

    def test_admin_assigns_and_the_employee_sees_it(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, body = self.assign(live, admin, email="Li.Si@WUJI.Tech", note="训练机")
            self.assertEqual(status, 200, body)
            self.assertEqual(body["ok"], True)
            self.assertEqual(body["key"], KEY)
            self.assertEqual(body["email"], ME)
            self.assertEqual(body["name"], "李四")  # 名字取名册里的，不听前端的
            self.assertEqual(mode(self.owners_path), 0o600)

            status, view = live.request("GET", "/api/assets", cookie=self.login(live, "on_1", ME))
            self.assertEqual(status, 200, view)
            acc = view["accounts"][0]
            self.assertEqual([r["id"] for r in acc["resources"]], ["i-2"])
            self.assertEqual(acc["resources"][0]["owner_note"], "训练机")
            self.assertEqual((acc["mine"], acc["unassigned"], acc["total"]), (1, 1, 2))
            self.assertNotIn("secret-db", json.dumps(view))  # 没指给他的那台不露出来

    def test_admin_clears_assignment_and_the_employee_loses_the_row(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            self.assertEqual(self.assign(live, admin, email=ME)[0], 200)
            status, body = self.assign(live, admin, email="")
            self.assertEqual(status, 200, body)
            self.assertEqual(self.owners(), {})
            _, view = live.request("GET", "/api/assets", cookie=self.login(live, "on_1", ME))
            acc = view["accounts"][0]
            self.assertEqual(acc["resources"], [])
            self.assertEqual((acc["mine"], acc["unassigned"]), (0, 2))

    def test_applicant_cannot_assign(self):
        """归属是问责和分摊成本的依据，只有管理员能改 —— 否则谁都能把自己的机器
        划给别人（或者把别人的划给自己去看明细）。"""
        with _Live(self.backend) as live:
            status, body = self.assign(live, self.login(live, "on_1", ME), email=ME)
            self.assertEqual(status, 403, body)
            self.assertFalse(self.owners_path.exists())

    def test_anonymous_is_401(self):
        with _Live(self.backend) as live:
            status, _ = self.assign(live, "", email=ME)
            self.assertEqual(status, 401)
            self.assertFalse(self.owners_path.exists())

    def test_unknown_email_is_404_and_writes_nothing(self):
        """随手打错一个邮箱，那台机器就永远认不回来了（没人会用一个不存在的账号来认领）。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, body = self.assign(live, admin, email="typo@wuji.tech")
            self.assertEqual(status, 404, body)
            self.assertIn("typo@wuji.tech", body["error"])
            self.assertFalse(self.owners_path.exists())

    def test_ambiguous_email_is_409(self):
        """名册里两个人同邮箱、或那个人被标了「多人共用此邮箱」：按邮箱指派必然指给错的人。
        宁可让管理员先去把名册理清楚，也不要把一台机器挂到错的人头上。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            for email in ("twin@wuji.tech", "shared@wuji.tech"):
                status, body = self.assign(live, admin, email=email)
                self.assertEqual(status, 409, (email, body))
                self.assertFalse(self.owners_path.exists(), email)

    def test_email_lookup_is_case_insensitive_against_the_roster(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, body = self.assign(live, admin, email="LI.SI@WUJI.TECH")
            self.assertEqual(status, 200, body)
            self.assertEqual(self.owners()[KEY]["email"], ME)

    def test_incomplete_resource_is_400(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            for missing in ({"id": ""}, {"account": ""}, {"platform": ""}):
                status, body = self.assign(live, admin, email=ME, **missing)
                self.assertEqual(status, 400, (missing, body))
            self.assertFalse(self.owners_path.exists())

    def test_csrf_headers_are_required(self):
        """只靠 Cookie 的话，管理员点开任意外部页面就可能被替他指派资源。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            body = {"platform": "aliyun", "account": ACC, "id": "i-2", "email": ME}
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
                status, _ = live.request(
                    "POST", OWNER_API, cookie=admin, payload=body, headers=headers
                )
                self.assertEqual(status, 403, headers)
            self.assertFalse(self.owners_path.exists())

    def test_get_is_not_a_route(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            self.assertEqual(live.request("GET", OWNER_API, cookie=admin)[0], 404)

    def test_broken_owner_table_never_silently_becomes_unassigned(self):
        """归属表坏了：写入必须拒绝（别把之前的指派覆盖掉）。"""
        self.owners_path.write_text('{"owners": {', encoding="utf-8")
        before = self.owners_path.read_bytes()
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, body = self.assign(live, admin, email=ME)
            self.assertEqual(status, 400, body)
            self.assertEqual(self.owners_path.read_bytes(), before)

    def test_viewer_is_matched_by_roster_email_not_session_email(self):
        """看的人按**名册邮箱**认，不看会话里带回来的那个。

        管理员指派时校验的是名册，两头必须用同一个键。会话邮箱不行：公司 IAM 代理登录
        （`proxy_auth.user()`）根本不带邮箱 —— 那样这功能对走 IAM 登录的人整个失效；
        飞书登录在租户没开企业邮箱服务时退化成私人联系邮箱 —— 那更糟，私人邮箱撞上
        别人的名册邮箱就会看到别人的资源。
        """
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            self.assertEqual(self.assign(live, admin, email=ME)[0], 200)
            # 代理登录：会话里没有邮箱，人却在名册里（union_id 对得上）
            sid = self.login(live, "on_1", "")
            _, view = live.request("GET", "/api/assets", cookie=sid)
            self.assertEqual([r["id"] for r in view["accounts"][0]["resources"]], ["i-2"])
            _, me = live.request("GET", "/api/me", cookie=sid)
            self.assertEqual([r["id"] for r in me["accounts"][0]["resources"]], ["i-2"])

    def test_session_email_cannot_claim_someone_elses_resources(self):
        """会话里的邮箱是别人的名册邮箱（私人联系邮箱撞名/被改）：认名册的话谁也拿不到。"""
        self.write_owners({KEY: {"email": ME, "name": "李四"}})
        with _Live(self.make_backend()) as live:
            # on_2 老张登录，会话邮箱冒充李四
            sid = self.login(live, "on_2", ME)
            _, view = live.request("GET", "/api/assets", cookie=sid)
            self.assertEqual(view["accounts"][0]["resources"], [])
            _, me = live.request("GET", "/api/me", cookie=sid)
            self.assertEqual(me["accounts"][0]["resources"], [])

    def test_uppercase_email_in_the_table_still_reaches_its_owner(self):
        """归属表被手工编辑过、或历史数据没归一：一个 `Li.Si@` 不能让那台机器对本人隐身。"""
        self.write_owners({KEY: {"email": "Li.Si@WUJI.Tech", "name": "李四"}})
        with _Live(self.make_backend()) as live:
            sid = self.login(live, "on_1", ME)
            _, view = live.request("GET", "/api/assets", cookie=sid)
            self.assertEqual([r["id"] for r in view["accounts"][0]["resources"]], ["i-2"])
            self.assertEqual(view["accounts"][0]["mine"], 1)
            _, me = live.request("GET", "/api/me", cookie=sid)
            self.assertEqual([r["id"] for r in me["accounts"][0]["resources"]], ["i-2"])

    def test_broken_owner_table_makes_the_asset_pages_say_unavailable(self):
        """坏表不能静默变成「谁都没指派过」：那和「表没建」长得一模一样，
        看的人只会以为归属还没开始做，然后照旧没人认领。宁可整页报不可用。"""
        self.owners_path.write_text('{"owners": [', encoding="utf-8")
        with _Live(self.make_backend()) as live:
            for path, cookie in (
                ("/api/assets", self.login(live, "on_1", ME)),
                ("/api/admin/assets", self.login(live, "on_admin", "admin@wuji.tech")),
            ):
                status, body = live.request("GET", path, cookie=cookie)
                self.assertEqual(status, 500, (path, body))
                # 细节只进服务端日志：异常里可能带服务器路径和邮箱
                self.assertNotIn(ME, json.dumps(body, ensure_ascii=False), path)

    def test_broken_owner_table_also_stops_the_home_page(self):
        """同一张坏表，`/api/me` 上必须是同一个结论。

        现在 `_owned_by` 把 `DeliveryError` 吞掉打日志返回 `{}` —— 卡片上「名下资源」
        空着，和「这个人没有资源」一模一样，而没人会去翻服务端日志。改法：把
        `_owned_by` 里的 `try/except DeliveryError` 去掉即可 —— 「还没采过」由
        `assets.load` 返回 None、「还没指派过」由 `load_owners` 返回 {} 覆盖，
        那两种初始状态本来就走不到异常，这个 except 现在只挡住「文件坏了」这一种。
        """
        self.owners_path.write_text('{"owners": [', encoding="utf-8")
        with _Live(self.make_backend()) as live:
            status, body = live.request("GET", "/api/me", cookie=self.login(live, "on_1", ME))
            self.assertEqual(status, 500, body)
            self.assertNotIn(ME, json.dumps(body, ensure_ascii=False))


VOLC = "2000000001"
TWO_ACCOUNTS = {
    "captured_at": "2026-09-15T10:00:00+08:00",
    "accounts": [
        {
            "platform": "aliyun",
            "account": ACC,
            "resources": [
                {"type": "ACS::ECS::Instance", "id": "i-1", "name": "secret-db", "region": "cn-hz"},
                {"type": "ACS::ECS::Instance", "id": "i-2", "name": "web", "region": "cn-hz"},
                {"type": "ACS::OSS::Bucket", "id": "b-1", "name": "colleague-bucket"},
            ],
        },
        {
            "platform": "volcano",
            "account": VOLC,
            # 同一个资源 ID 在另一朵云里也存在：归属按 平台/账号/ID 认，不能串
            "resources": [
                {"type": "ecs.instance", "id": "i-2", "name": "火山机", "region": "cn-b"}
            ],
        },
        {"platform": "aliyun", "account": "9000000000000009", "error": "AccessDenied 细节"},
    ],
}


class OwnedByTests(_PanelBase):
    """`server._owned_by`：首页账号卡片上的「名下资源」。

    这个函数跑在**每个人登录后的第一屏**上。资产没采、归属表没建、文件坏了都是
    常态（功能是分批上线的），任何一种都不能把首页弄成 500 —— 那等于因为一个
    附加板块，把权限、申请入口全挡了。
    """

    def owned(self, backend=None, email=ME):
        return _owned_by(backend or self.backend, email)

    def test_groups_by_account_with_only_the_display_fields(self):
        (self.dir / "assets.json").write_text(json.dumps(TWO_ACCOUNTS, ensure_ascii=False), "utf-8")
        self.write_owners(
            {
                f"aliyun/{ACC}/i-2": {"email": ME, "name": "李四", "note": "训练机"},
                f"volcano/{VOLC}/i-2": {"email": ME, "name": "李四"},
            }
        )
        out = self.owned(self.make_backend())
        self.assertEqual(sorted(out), [("aliyun", ACC), ("volcano", VOLC)])
        self.assertEqual(
            out[("aliyun", ACC)],
            [
                {
                    "id": "i-2",
                    "name": "web",
                    "type_label": "ECS Instance",
                    "region": "cn-hz",
                    "note": "训练机",
                }
            ],
        )
        # 另一朵云的同 ID 资源归到它自己的账号下，不会混进上面那条
        self.assertEqual([r["name"] for r in out[("volcano", VOLC)]], ["火山机"])

    def test_other_peoples_and_unassigned_resources_never_appear(self):
        (self.dir / "assets.json").write_text(json.dumps(TWO_ACCOUNTS, ensure_ascii=False), "utf-8")
        self.write_owners(
            {
                f"aliyun/{ACC}/b-1": {"email": "wang.wu@wuji.tech", "name": "王五"},
                # 别的账号里指给我的，不能让它冒到这个账号下
                "aliyun/9000000000000009/i-1": {"email": ME, "name": "李四"},
            }
        )
        out = self.owned(self.make_backend())
        self.assertEqual(out, {})
        self.assertNotIn("colleague-bucket", json.dumps(out, ensure_ascii=False))

    def test_email_match_is_case_insensitive_both_ways(self):
        """归属表里可能是历史数据（大小写原样存的），登录带回的邮箱大小写也不保证。
        两边都归一，不然「明明指派了却看不到」，没人查得出为什么。"""
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": "Li.Si@WUJI.Tech"}})
        backend = self.make_backend()
        for who in (ME, "LI.SI@Wuji.Tech", "  li.si@wuji.tech  "):
            self.assertEqual(
                [r["id"] for r in self.owned(backend, who).get(("aliyun", ACC), [])], ["i-2"], who
            )

    def test_blank_email_matches_nobody(self):
        """代理登录拿不到邮箱时是空串：空串绝不能匹配「归属表里 email 也是空」的记录。"""
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": ME}, f"aliyun/{ACC}/i-1": {"email": ""}})
        backend = self.make_backend()
        for blank in ("", "   ", None):
            self.assertEqual(self.owned(backend, blank), {}, blank)

    def test_no_asset_snapshot_yet(self):
        """资产还没采过（或根本没配路径）：空表，不报错。"""
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": ME}})
        (self.dir / "assets.json").unlink()
        self.assertEqual(self.owned(self.make_backend()), {})
        self.assertEqual(self.owned(self.make_backend(assets_path=None)), {})

    def test_no_owner_table_yet(self):
        """一次都还没指派过：空表，不报错（也不按名字猜）。"""
        self.assertFalse(self.owners_path.exists())
        self.assertEqual(self.owned(), {})
        self.write_owners({})
        self.assertEqual(self.owned(self.make_backend()), {})

    def test_broken_files_are_reported_not_turned_into_an_empty_list(self):
        """「还没配好」和「文件坏了」必须分开：前面几条锁的是前者（返回空表），
        这条锁后者 —— 坏表读成空的表现是「指派过的资源全都不见了」，
        而页面上它和「没有资源」长得一样，没人会发现。

        三种初始状态（没采过 / 没指派过 / 没邮箱）都走不到异常，所以让它抛不会
        影响首页的常态；`_owned_by` 里那个 `except DeliveryError: return {}`
        现在只挡住「文件坏了」这一种，正好是唯一不该挡的。
        """
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": ME}})
        (self.dir / "assets.json").write_text('{"accounts": ', encoding="utf-8")
        with self.assertRaises(DeliveryError):
            self.owned(self.make_backend())

        (self.dir / "assets.json").write_text(json.dumps(ASSETS), encoding="utf-8")
        self.owners_path.write_text('{"owners": [', encoding="utf-8")
        with self.assertRaises(DeliveryError):
            self.owned(self.make_backend())

    def test_account_that_failed_collection_is_skipped(self):
        (self.dir / "assets.json").write_text(json.dumps(TWO_ACCOUNTS, ensure_ascii=False), "utf-8")
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": ME}})
        out = self.owned(self.make_backend())
        self.assertEqual(list(out), [("aliyun", ACC)])  # 采集失败那个账号不出现，也不抛

    def test_note_is_carried_but_not_the_owner_email(self):
        """卡片上只需要「这是我的哪台机器、干什么用的」；归属邮箱是自己的，没必要回显，
        字段越少越不容易在别的页面上被顺手渲染出去。"""
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": ME, "name": "李四", "note": "训练机"}})
        row = self.owned(self.make_backend())[("aliyun", ACC)][0]
        self.assertEqual(set(row), {"id", "name", "type_label", "region", "note"})
        self.assertEqual(row["note"], "训练机")


class MyResourcesApiTests(_PanelBase):
    """`/api/me` 的账号卡片带「名下资源」；管理员看别人详情时不能串成自己的。"""

    def test_account_card_carries_my_resources(self):
        self.write_owners({f"aliyun/{ACC}/i-2": {"email": ME, "name": "李四", "note": "训练机"}})
        with _Live(self.make_backend()) as live:
            status, me = live.request("GET", "/api/me", cookie=self.login(live, "on_1", ME))
            self.assertEqual(status, 200, me)
            card = me["accounts"][0]
            self.assertEqual([r["id"] for r in card["resources"]], ["i-2"])
            self.assertEqual(card["resources"][0]["note"], "训练机")
            self.assertNotIn("secret-db", json.dumps(me))  # 没指给他的不露出来

    def test_account_card_has_the_field_even_with_nothing_configured(self):
        """资产功能还没铺开时，前端也要能无脑读 `card.resources`。"""
        with _Live(self.make_backend(assets_path=None)) as live:
            status, me = live.request("GET", "/api/me", cookie=self.login(live, "on_1", ME))
            self.assertEqual(status, 200, me)
            self.assertEqual(me["accounts"][0]["resources"], [])

    def test_admin_looking_at_someone_else_does_not_see_their_own_resources(self):
        """管理员名下也有资源。看别人详情时把自己的塞进去，等于告诉他「这人有我这台机器」——
        归属是拿去问责的，串一次就全错。"""
        self.write_owners(
            {
                f"aliyun/{ACC}/i-1": {"email": "admin@wuji.tech", "name": "管理员"},
                f"aliyun/{ACC}/i-2": {"email": ME, "name": "李四"},
            }
        )
        with _Live(self.make_backend()) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, detail = live.request("GET", "/api/admin/people/on_1", cookie=admin)
            self.assertEqual(status, 200, detail)
            # 现状这条路不传 _owned_by（卡片上没有 resources 字段）。这里不锁「必须没有」——
            # 以后要显示某人名下资源是合理需求；锁的是「显示的话只能是**那个人**的」：
            # 管理员自己的 i-1 / secret-db 一旦冒出来，就是把 session 当成了被看的人。
            for card in detail["accounts"]:
                self.assertEqual(
                    [r["id"] for r in card.get("resources", [])],
                    [r["id"] for r in card.get("resources", []) if r["id"] == "i-2"],
                )
            self.assertNotIn("secret-db", json.dumps(detail))
            self.assertNotIn("admin@wuji.tech", json.dumps(detail))
            # 管理员自己的首页照常显示自己的那台
            _, mine = live.request("GET", "/api/me", cookie=admin)
            self.assertEqual([r["id"] for r in mine["accounts"][0]["resources"]], ["i-1"])


if __name__ == "__main__":
    unittest.main()
