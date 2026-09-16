"""管理后台「IAM 属性表」：预览增量 → 导出存档 → IT 导入后确认 → 下载 CSV。

和 CLI 走同一套规则（iam_sync）：写盘只允许仓库根的 identity/，删号必须发 remove，
超阈值的批量 remove 要显式确认。属性表含全员邮箱：只有管理员能看、能下载。

离线：临时目录 chdir 当仓库根，数据虚构。
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import fcntl
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from delivery import cli, iam_sync
from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _WebSession, make_handler

ALI = "aliyun/1000000000000001"
ATTRS = {ALI: "aliyun_username"}
A_APP = "aliyun_username"
IAM = "/api/admin/iam-attributes"


def person(name, email, accounts=(), union_id="", pending=()):
    return {
        "name": name,
        "email": email,
        "union_id": union_id,
        "accounts": list(accounts),
        "pending": list(pending),
    }


def ali(name):
    return {"platform": "aliyun", "account": "1000000000000001", "name": name}


def roster(*people):
    return {"schema": "wuji-people@1", "people": list(people)}


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
        self.sessions = None

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
        out = (resp.status, dict(resp.getheaders()), raw)
        conn.close()
        return out


class PanelTests(unittest.TestCase):
    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        self.id_dir = self.root / "identity"
        self.id_dir.mkdir()
        (self.id_dir / "attrs.json").write_text(json.dumps(ATTRS), encoding="utf-8")
        (self.id_dir / "admins.json").write_text(
            json.dumps({"union_ids": ["on_admin"]}), encoding="utf-8"
        )
        self.write_people(
            roster(
                person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P"),
                person("安娜", "a@wuji.tech", [ali("anna")], union_id="on_A"),
            )
        )
        self.backend = Backend(
            people_path="identity/people.json",
            bindings_path="identity/bindings.json",
            admins_path="identity/admins.json",
            iam_spec_path="identity/attrs.json",
            iam_out_path="identity/iam-attributes.csv",
            platforms={"aliyun": "阿里云"},
        )

    def write_people(self, data):
        (self.id_dir / "people.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def live(self):
        return _Live(self.backend)

    def login(self, live, uid="on_admin"):
        sid = f"sid-{uid}"
        live.store.sessions[sid] = _WebSession(
            user=FeishuUser(open_id="ou_x", union_id=uid, name="某人")
        )
        return sid

    def get(self, live, path, sid):
        status, headers, raw = live.request("GET", path, cookie=sid)
        return status, headers, raw

    def api(self, live, sid, payload=None, method="GET", path=IAM):
        status, _, raw = live.request(method, path, cookie=sid, payload=payload)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, {}

    # ── 下面几个只是把「导一次 / 下一份 / 解析一份」写短，语义没有额外约定 ──────

    def export(self, live, sid, **body):
        status, data = self.api(live, sid, {"op": "export", **body}, method="POST")
        self.assertEqual(status, 200, f"导出失败：{data}")
        return data

    def make_baseline(self, live, sid):
        """导出并确认一次：基线里有彼得和安娜。返回基线存档名。"""
        name = self.export(live, sid)["recorded"]
        status, data = self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
        self.assertEqual(status, 200, f"确认基线失败：{data}")
        return name

    def anna_leaves(self):
        """安娜离职：名册只剩彼得，下一次增量里必须带她的 remove。

        整个 union_id 从名册消失会撞上批量移除闸（见 test_mass_remove_refused_until_opted_in），
        所以这类导出都带 allow_mass_remove —— 这正是面板上管理员确认过离职后勾的那个框。
        """
        self.write_people(roster(person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")))

    def download(self, live, sid, name):
        status, headers, raw = self.get(live, f"{IAM}/file?name={name}", sid)
        self.assertEqual(status, 200, f"下载 {name} 失败：{raw!r}")
        return headers, raw

    def csv_rows(self, raw):
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))

    def out_bytes(self):
        return (self.id_dir / "iam-attributes.csv").read_bytes()

    def inc_name(self, archive):
        """存档名 → 它旁边那份增量副本应有的名字。"""
        return archive[: -len(".csv")] + iam_sync.INCREMENT_SUFFIX

    def pending_entry(self, data, name):
        for item in data["pending"]:
            if item["name"] == name:
                return item
        self.fail(f"pending 列表里没有 {name}：{data['pending']}")

    @property
    def sent_dir(self):
        return self.id_dir / "iam-sent"

    @property
    def pending_dir(self):
        return self.sent_dir / "pending"

    # ── 预览 ──────────────────────────────────────────────────────────────

    def test_preview_without_baseline_lists_full_state(self):
        with self.live() as live:
            sid = self.login(live)
            status, data = self.api(live, sid)
            self.assertEqual(status, 200)
            self.assertTrue(data["attributes_configured"])
            self.assertIsNone(data["baseline"])
            self.assertEqual(data["increment"]["counts"], {"set": 2, "remove": 0, "skip": 0})
            self.assertEqual(
                sorted(r["email"] for r in data["increment"]["rows"]),
                ["a@wuji.tech", "p@wuji.tech"],
            )
            self.assertEqual(data["pending"], [])
            self.assertTrue(data["can_export"])
            # 只是预览：什么都不该写出来
            self.assertFalse((self.id_dir / "iam-attributes.csv").exists())
            self.assertFalse((self.id_dir / "iam-sent").exists())

    def test_preview_with_baseline_only_shows_changes(self):
        with self.live() as live:
            sid = self.login(live)
            self.api(live, sid, {"op": "export"}, method="POST")
            name = self.api(live, sid)[1]["pending"][0]["name"]
            self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
            status, data = self.api(live, sid)
            self.assertEqual(status, 200)
            self.assertEqual(data["baseline"]["name"], name)
            self.assertTrue(data["baseline"]["captured_at"])
            self.assertEqual(data["increment"]["counts"]["set"], 0)
            # 名册多一个人 → 增量就多一条
            self.write_people(
                roster(
                    person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P"),
                    person("安娜", "a@wuji.tech", [ali("anna")], union_id="on_A"),
                    person("新人", "n@wuji.tech", [ali("newbie")], union_id="on_N"),
                )
            )
            data = self.api(live, sid)[1]
            self.assertEqual(
                [(r["email"], r["action"]) for r in data["increment"]["rows"]],
                [("n@wuji.tech", "set")],
            )

    def test_missing_attributes_spec_is_explained_not_500(self):
        (self.id_dir / "attrs.json").unlink()
        with self.live() as live:
            sid = self.login(live)
            status, data = self.api(live, sid)
            self.assertEqual(status, 200)
            self.assertFalse(data["attributes_configured"])
            self.assertFalse(data["can_export"])
            self.assertIn("attrs.json", data["blocked"])
            # 导出直接拒绝，并且不留下半份文件
            status, data = self.api(live, sid, {"op": "export"}, method="POST")
            self.assertEqual(status, 409)
            self.assertIn("attrs.json", data["error"])
            self.assertFalse((self.id_dir / "iam-attributes.csv").exists())

    def test_paths_not_configured_is_404(self):
        backend = Backend(people_path="identity/people.json", admins_path="identity/admins.json")
        with _Live(backend) as live:
            sid = self.login(live)
            self.assertEqual(self.api(live, sid)[0], 404)

    # ── 导出与确认 ────────────────────────────────────────────────────────

    def test_export_writes_csv_and_pending_archive(self):
        with self.live() as live:
            sid = self.login(live)
            status, data = self.api(live, sid, {"op": "export"}, method="POST")
            self.assertEqual(status, 200)
            out = self.id_dir / "iam-attributes.csv"
            self.assertTrue(out.exists())
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            archive = self.id_dir / "iam-sent" / "pending" / data["recorded"]
            self.assertTrue(archive.exists())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertTrue(archive.with_name(archive.name + ".meta.json").exists())
            self.assertEqual([p["name"] for p in data["pending"]], [data["recorded"]])
            self.assertEqual(data["pending"][0]["rows"], 2)

    def test_confirm_promotes_archive_to_baseline(self):
        with self.live() as live:
            sid = self.login(live)
            name = self.api(live, sid, {"op": "export"}, method="POST")[1]["recorded"]
            status, data = self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
            self.assertEqual(status, 200)
            self.assertEqual(data["confirmed"], name)
            self.assertEqual(data["baseline"]["name"], name)
            self.assertEqual(data["pending"], [])
            self.assertTrue((self.id_dir / "iam-sent" / name).exists())
            self.assertFalse((self.id_dir / "iam-sent" / "pending" / name).exists())
            # 同一份不能确认两次
            status, data = self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
            self.assertEqual(status, 409)

    def test_confirm_rejects_bad_names(self):
        with self.live() as live:
            sid = self.login(live)
            self.api(live, sid, {"op": "export"}, method="POST")
            for bad in ("", "../people.json", "iam-attributes.csv", "20260101-000000.csv"):
                status, data = self.api(live, sid, {"op": "confirm", "name": bad}, method="POST")
                self.assertEqual(status, 409, bad)
                self.assertTrue(data["error"])

    def test_unknown_op_is_400(self):
        with self.live() as live:
            sid = self.login(live)
            self.assertEqual(self.api(live, sid, {"op": "nope"}, method="POST")[0], 400)

    def test_mass_remove_refused_until_opted_in(self):
        many = [person(f"人{i}", f"u{i}@wuji.tech", [ali(f"u{i}")], f"on_{i}") for i in range(12)]
        self.write_people(roster(*many))
        with self.live() as live:
            sid = self.login(live)
            name = self.api(live, sid, {"op": "export"}, method="POST")[1]["recorded"]
            self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
            # 所有人都没了（名册没带通讯录重新生成的典型症状）
            self.write_people(roster(person("留守", "stay@wuji.tech", [ali("stay")], "on_S")))
            status, data = self.api(live, sid)
            self.assertEqual(status, 200)
            self.assertFalse(data["can_export"])
            self.assertTrue(data["blocked"])
            before = (self.id_dir / "iam-attributes.csv").read_bytes()
            status, data = self.api(live, sid, {"op": "export"}, method="POST")
            self.assertEqual(status, 409)
            # 被拒的这次不能改写上一次的输出，也不能多存一份待确认
            self.assertEqual((self.id_dir / "iam-attributes.csv").read_bytes(), before)
            self.assertEqual(len(list((self.id_dir / "iam-sent" / "pending").glob("*.csv"))), 0)
            status, data = self.api(
                live, sid, {"op": "export", "allow_mass_remove": True}, method="POST"
            )
            self.assertEqual(status, 200)
            self.assertEqual(data["increment"]["counts"]["remove"], 12)

    def test_preview_can_show_the_removes_it_refuses_to_export(self):
        """页面要先让管理员看见「要移除谁」才敢给勾选框：预览带参数只算不写。"""
        many = [person(f"人{i}", f"u{i}@wuji.tech", [ali(f"u{i}")], f"on_{i}") for i in range(12)]
        self.write_people(roster(*many))
        with self.live() as live:
            sid = self.login(live)
            name = self.api(live, sid, {"op": "export"}, method="POST")[1]["recorded"]
            self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
            self.write_people(roster(person("留守", "stay@wuji.tech", [ali("stay")], "on_S")))
            before = (self.id_dir / "iam-attributes.csv").read_bytes()
            status, data = self.api(live, sid, path=IAM + "?allow_mass_remove=1")
            self.assertEqual(status, 200)
            removes = [r for r in data["increment"]["rows"] if r["action"] == "remove"]
            self.assertEqual(len(removes), 12)
            self.assertTrue(all(r["name"] for r in removes), removes)
            # 预览不写盘：输出文件没动，也没多出待确认存档
            self.assertEqual((self.id_dir / "iam-attributes.csv").read_bytes(), before)
            self.assertEqual(len(list((self.id_dir / "iam-sent" / "pending").glob("*.csv"))), 0)

    # ── 下载 ──────────────────────────────────────────────────────────────

    def test_download_export_and_archive(self):
        with self.live() as live:
            sid = self.login(live)
            name = self.api(live, sid, {"op": "export"}, method="POST")[1]["recorded"]
            status, headers, raw = self.get(live, f"{IAM}/file?name=iam-attributes.csv", sid)
            self.assertEqual(status, 200)
            self.assertIn("text/csv", headers["Content-Type"])
            self.assertIn("attachment", headers["Content-Disposition"])
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertIn("p@wuji.tech", raw.decode("utf-8-sig"))
            self.assertEqual(self.get(live, f"{IAM}/file?name={name}", sid)[0], 200)

    def test_download_rejects_traversal_and_unknown_names(self):
        with self.live() as live:
            sid = self.login(live)
            self.api(live, sid, {"op": "export"}, method="POST")
            (self.id_dir / "secret.csv").write_text("x", encoding="utf-8")
            link = self.id_dir / "iam-sent" / "20990101-000000.csv"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.id_dir / "secret.csv")
            for bad in (
                "",
                "..%2Fpeople.json",
                "../people.json",
                "people.json",
                "%2Fetc%2Fpasswd",
                "20990101-000000.csv",  # 符号链接
                "20260101-000000.csv",  # 不存在
            ):
                status, _, _ = self.get(live, f"{IAM}/file?name={bad}", sid)
                self.assertEqual(status, 404, bad)

    # ── 权限 ──────────────────────────────────────────────────────────────

    def test_anonymous_and_non_admin_are_refused(self):
        with self.live() as live:
            employee = self.login(live, "on_P")
            for path in (IAM, f"{IAM}/file?name=iam-attributes.csv"):
                self.assertEqual(live.request("GET", path)[0], 401, path)
                self.assertEqual(live.request("GET", path, cookie=employee)[0], 403, path)
            status, _, _ = live.request("POST", IAM, cookie=employee, payload={"op": "export"})
            self.assertEqual(status, 403)
            self.assertEqual(live.request("POST", IAM, payload={"op": "export"})[0], 401)
            self.assertFalse((self.id_dir / "iam-attributes.csv").exists())

    def test_write_needs_same_origin_json(self):
        with self.live() as live:
            sid = self.login(live)
            status, _, raw = live.request(
                "POST",
                IAM,
                cookie=sid,
                payload={"op": "export"},
                headers={"Content-Type": "application/json"},  # 缺 X-Panel-Request
            )
            self.assertEqual(status, 403)
            self.assertIn("X-Panel-Request", json.loads(raw)["error"])
            self.assertFalse((self.id_dir / "iam-attributes.csv").exists())

    def test_file_route_rejects_post(self):
        with self.live() as live:
            sid = self.login(live)
            status, _, _ = live.request(
                "POST", f"{IAM}/file", cookie=sid, payload={"name": "x.csv"}
            )
            self.assertEqual(status, 405)

    def test_download_marks_responses_nosniff(self):
        """CSV 里有员工邮箱：浏览器不许猜类型把它当别的东西渲染。"""
        with self.live() as live:
            sid = self.login(live)
            data = self.export(live, sid)
            for name in (data["out_name"], data["recorded"]):
                headers, _ = self.download(live, sid, name)
                self.assertEqual(
                    headers.get("X-Content-Type-Options"),
                    "nosniff",
                    f"{name} 的响应缺 nosniff：{headers}",
                )

    # ── 增量 vs 全量快照 ──────────────────────────────────────────────────
    #
    # 一轮导出产生两份文件，语义完全不同，发错一份的后果是「离职的人属性永远
    # 留在公司 IAM 里」——这是刚修掉的 Blocker，下面几条就是钉死它。

    def test_increment_carries_the_removes_that_the_snapshot_must_not(self):
        with self.live() as live:
            sid = self.login(live)
            self.make_baseline(live, sid)
            self.anna_leaves()
            data = self.export(live, sid, allow_mass_remove=True)

            self.assertEqual(data["out_name"], "iam-attributes.csv")
            self.assertNotIn(
                "out",
                data,
                f"响应把服务端路径回给浏览器了：{data.get('out')!r}（只该回文件名）",
            )
            self.assertNotEqual(
                data["out_name"],
                data["recorded"],
                "增量和全量快照用了同一个名字，前端没法区分该下哪一份",
            )

            _, increment = self.download(live, sid, data["out_name"])
            _, snapshot = self.download(live, sid, data["recorded"])

            self.assertEqual(
                [(r["email"], r["action"]) for r in self.csv_rows(increment)],
                [("a@wuji.tech", "remove")],
                f"增量（发给 IT 的那份）里没有离职者的 remove 行：{increment!r}",
            )
            snapshot_rows = self.csv_rows(snapshot)
            self.assertEqual(
                [r for r in snapshot_rows if r["action"] == "remove"],
                [],
                f"全量快照含 remove，check_baseline 会拒绝它当基线：{snapshot!r}",
            )
            self.assertEqual(
                [(r["email"], r["action"]) for r in snapshot_rows],
                [("p@wuji.tech", "set")],
                f"全量快照应当是「发完之后 IAM 里应有的状态」：{snapshot!r}",
            )
            self.assertNotEqual(
                increment,
                snapshot,
                "两份文件内容一样：下载入口把增量指到了全量快照（照它发给 IT 会漏掉删号）",
            )

    def test_preview_download_name_matches_the_increment_file(self):
        """页面下载增量只认 out_name；它必须就是磁盘上那份增量输出。"""
        with self.live() as live:
            sid = self.login(live)
            self.make_baseline(live, sid)
            self.anna_leaves()
            data = self.export(live, sid, allow_mass_remove=True)
            _, body = self.download(live, sid, data["out_name"])
            self.assertEqual(
                body,
                (self.id_dir / "iam-attributes.csv").read_bytes(),
                "按 out_name 下到的不是 --iam-out 写出的那份增量",
            )

    # ── 每一轮的增量副本 ──────────────────────────────────────────────────
    #
    # out（identity/iam-attributes.csv）是共享单文件，下一次导出就把它覆盖掉；
    # 而「下载增量」的入口以前只活在刚导出那一次响应里。刷新一下页面，管理员
    # 能下到的就只剩全量快照 —— 照它发给 IT 会漏掉所有离职者的 remove。
    # 所以每一轮都在存档旁边留一份同内容的副本，下载入口和存档一一对应。

    def test_export_keeps_an_increment_copy_next_to_the_archive(self):
        with self.live() as live:
            sid = self.login(live)
            self.make_baseline(live, sid)
            self.anna_leaves()
            data = self.export(live, sid, allow_mass_remove=True)
            archive = data["recorded"]
            entry = self.pending_entry(data, archive)
            self.assertEqual(
                entry["increment"],
                self.inc_name(archive),
                f"pending 项里没有指向这一轮增量副本的文件名：{entry}",
            )
            copy = self.pending_dir / entry["increment"]
            self.assertTrue(copy.is_file(), f"{copy} 不在磁盘上：只在响应里报了名字，没落盘")
            self.assertEqual(
                copy.stat().st_mode & 0o777,
                0o600,
                f"{copy.name} 权限不是 0600：这份含全员邮箱与云用户名",
            )

            _, body = self.download(live, sid, entry["increment"])
            self.assertEqual(
                [(r["email"], r["action"]) for r in self.csv_rows(body)],
                [("a@wuji.tech", "remove")],
                f"增量副本里没有离职者的 remove 行，存下的其实是全量快照：{body!r}",
            )
            _, snapshot = self.download(live, sid, archive)
            self.assertEqual(
                [r for r in self.csv_rows(snapshot) if r["action"] == "remove"],
                [],
                f"全量存档含 remove，它不该是增量：{snapshot!r}",
            )
            self.assertEqual(
                body,
                self.out_bytes(),
                "增量副本和这一轮 --iam-out 写出的增量字节不一致：两个入口下到的不是同一份",
            )

    def test_increment_copy_survives_a_page_refresh(self):
        """这次修复的核心：下载增量的入口不能只活在「刚导出」那一次响应里。"""
        with self.live() as live:
            sid = self.login(live)
            self.make_baseline(live, sid)
            self.anna_leaves()
            exported = self.export(live, sid, allow_mass_remove=True)["recorded"]
            sent_to_it = self.out_bytes()

            status, view = self.api(live, sid)  # 刷新页面 = 重新拉一次只读视图
            self.assertEqual(status, 200, view)
            self.assertNotIn(
                "recorded", view, "只读视图也带上了一次性的导出结果，这条就没在测「刷新之后」"
            )
            entry = self.pending_entry(view, exported)
            self.assertTrue(
                entry["increment"],
                f"刷新后增量的下载入口没了，页面上只剩全量快照可下：{entry}",
            )
            _, body = self.download(live, sid, entry["increment"])
            self.assertEqual(body, sent_to_it, "刷新后下到的副本不是刚才导出的那份增量")
            self.assertEqual(
                [(r["email"], r["action"]) for r in self.csv_rows(body)],
                [("a@wuji.tech", "remove")],
                f"刷新后下到的这份没有 remove 行，发给 IT 会漏掉离职者：{body!r}",
            )

    def test_each_round_keeps_its_own_increment_copy(self):
        """out 被下一次导出覆盖，但两轮的副本各自对应各自那一轮，互不影响。"""
        peter = person("彼得", "p@wuji.tech", [ali("peter")], union_id="on_P")
        anna = person("安娜", "a@wuji.tech", [ali("anna")], union_id="on_A")
        newbie = person("新人", "n@wuji.tech", [ali("newbie")], union_id="on_N")
        with self.live() as live:
            sid = self.login(live)
            self.make_baseline(live, sid)

            self.write_people(roster(peter, anna, newbie))  # 第一轮：来了个新人
            first = self.export(live, sid)
            first_inc = self.pending_entry(first, first["recorded"])["increment"]
            _, first_body = self.download(live, sid, first_inc)

            self.write_people(roster(peter, newbie))  # 第二轮：安娜离职
            second = self.export(live, sid, allow_mass_remove=True)
            second_inc = self.pending_entry(second, second["recorded"])["increment"]
            _, second_body = self.download(live, sid, second_inc)

            self.assertNotEqual(
                first_inc, second_inc, f"两轮共用了一个副本名，前一轮的被盖掉：{first_inc}"
            )
            self.assertEqual(
                [(r["email"], r["action"]) for r in self.csv_rows(first_body)],
                [("n@wuji.tech", "set")],
                f"第一轮的副本不是第一轮的内容：{first_body!r}",
            )
            self.assertEqual(
                sorted((r["email"], r["action"]) for r in self.csv_rows(second_body)),
                [("a@wuji.tech", "remove"), ("n@wuji.tech", "set")],
                f"第二轮的副本不是第二轮的内容：{second_body!r}",
            )
            self.assertEqual(second_body, self.out_bytes(), "最新一轮的副本和 out 对不上")
            _, again = self.download(live, sid, first_inc)
            self.assertEqual(again, first_body, "第二轮导出改写了第一轮的增量副本")

    def test_increment_copy_name_allows_the_same_second_suffix(self):
        """同一秒重复导出的存档叫 ...~1.csv，它的增量副本名字也必须认得这个形状。"""
        with self.live() as live:
            sid = self.login(live)
            self.export(live, sid)
            twin = self.pending_dir / "20260101-000000~1.increment.csv"
            twin.write_bytes(("﻿" + ",".join(iam_sync.CSV_HEADER) + "\r\n").encode())
            status, _, raw = self.get(live, f"{IAM}/file?name={twin.name}", sid)
            self.assertEqual(status, 200, f"~n 形状的增量副本下不到：{raw!r}")

    def test_two_exports_in_a_row_each_get_a_downloadable_copy(self):
        with self.live() as live:
            sid = self.login(live)
            rounds = [self.export(live, sid), self.export(live, sid)]
            names = [self.pending_entry(d, d["recorded"])["increment"] for d in rounds]
            self.assertEqual(len(set(names)), 2, f"两份存档共用了一个增量副本名：{names}")
            for name in names:
                self.assertTrue((self.pending_dir / name).is_file(), f"{name} 没落盘")
                status, _, raw = self.get(live, f"{IAM}/file?name={name}", sid)
                self.assertEqual(status, 200, f"{name} 下不到：名字校验不认这个形状？{raw!r}")

    def test_pending_without_a_copy_reports_an_empty_increment_name(self):
        """这次改动之前存下的那几份没有副本文件：老实回空串（前端据此不画按钮），不能报个下不到的名字。"""
        with self.live() as live:
            sid = self.login(live)
            name = self.export(live, sid)["recorded"]
            (self.pending_dir / self.inc_name(name)).unlink()
            entry = self.pending_entry(self.api(live, sid)[1], name)
            self.assertEqual(entry["increment"], "", f"副本文件不在了，列表还报着它的名字：{entry}")
            self.assertEqual(entry["rows"], 2, f"少了副本就连行数都读不出来了：{entry}")
            status, _, _ = self.get(live, f"{IAM}/file?name={self.inc_name(name)}", sid)
            self.assertEqual(status, 404, "副本已经不在了，下载入口却还能下到东西")

    def test_download_rejects_bogus_increment_names(self):
        with self.live() as live:
            sid = self.login(live)
            data = self.export(live, sid)
            good = self.pending_entry(data, data["recorded"])["increment"]
            # 同名的一份放进存档目录根下：副本只住在 pending/，不能顺着名字读到别处
            (self.sent_dir / "20990101-000000.increment.csv").write_text("stray", encoding="utf-8")
            link = self.pending_dir / "20990102-000000.increment.csv"
            link.symlink_to(self.id_dir / "people.json")
            for bad in (
                ".increment.csv",
                "increment.csv",
                f"../{good}",
                f"../pending/{good}",
                f"pending/{good}",
                f"..\\{good}",
                f"/identity/iam-sent/pending/{good}",
                "20990101-000000.increment.csv",  # 只存在于 iam-sent/ 根下
                "20260101-000000.increment.csv",  # 名字合法但不存在
                f"{good}.meta.json",
                "20990102-000000.increment.csv",  # 符号链接，指向名册
            ):
                with self.subTest(name=bad):
                    status, _, raw = self.get(live, f"{IAM}/file?name={bad}", sid)
                    self.assertEqual(status, 404, f"{bad!r} 被接受了：{raw!r}")
                    self.assertNotIn(b"on_P", raw, f"{bad!r} 读到了 identity/ 里的名册：{raw!r}")
            self.assertEqual(
                self.get(live, f"{IAM}/file?name={good}", sid)[0], 200, "合法的增量副本也被挡住了"
            )
            self.assertTrue((self.id_dir / "people.json").is_file(), "名册被动了")

    def test_confirm_clears_the_increment_copy(self):
        """确认之后这一轮就结束了：留着副本只会让人下到上一轮的增量再发一次。"""
        with self.live() as live:
            sid = self.login(live)
            data = self.export(live, sid)
            name = data["recorded"]
            inc = self.pending_entry(data, name)["increment"]
            self.assertTrue(inc, "导出就没写副本，这条没在测该测的")

            status, done = self.api(live, sid, {"op": "confirm", "name": name}, method="POST")
            self.assertEqual(status, 200, f"确认失败：{done}")
            self.assertEqual(
                sorted(p.name for p in self.pending_dir.iterdir()),
                [],
                "确认之后 pending 目录没清干净",
            )
            self.assertTrue((self.sent_dir / name).is_file(), f"确认过的基线 {name} 不见了")
            self.assertEqual(
                self.get(live, f"{IAM}/file?name={inc}", sid)[0],
                404,
                f"{inc} 已经确认过了还能下到",
            )

    # ── 作废待确认存档 ────────────────────────────────────────────────────

    def test_discard_deletes_the_pending_csv_and_its_meta(self):
        with self.live() as live:
            sid = self.login(live)
            name = self.export(live, sid)["recorded"]
            status, data = self.api(live, sid, {"op": "discard", "name": name}, method="POST")
            self.assertEqual(status, 200, f"作废失败：{data}")
            self.assertEqual(data.get("discarded"), name, f"响应没说作废了哪一份：{data}")
            self.assertEqual(data["pending"], [], f"作废后 pending 列表里还留着：{data['pending']}")
            self.assertFalse((self.pending_dir / name).exists(), f"{name} 还在磁盘上")
            self.assertFalse(
                (self.pending_dir / f"{name}.meta.json").exists(),
                f"{name} 的 .meta.json 没删：下次列 pending 会看到一份没有 CSV 的幽灵",
            )
            self.assertEqual(self.api(live, sid)[1]["pending"], [], "重新查一次 pending 又冒出来了")
            self.assertEqual(
                sorted(p.name for p in self.sent_dir.glob("*.csv")),
                [],
                "作废动到了已确认存档目录",
            )

    def test_discard_deletes_csv_meta_and_increment_together(self):
        with self.live() as live:
            sid = self.login(live)
            data = self.export(live, sid)
            name = data["recorded"]
            inc = self.pending_entry(data, name)["increment"]
            status, done = self.api(live, sid, {"op": "discard", "name": name}, method="POST")
            self.assertEqual(status, 200, f"作废失败：{done}")
            self.assertEqual(
                sorted(p.name for p in self.pending_dir.iterdir()),
                [],
                f"作废后 pending 目录还有残留（{name} 的增量副本没删？）",
            )
            self.assertFalse((self.pending_dir / inc).exists(), f"{inc} 还在磁盘上")
            self.assertEqual(
                self.get(live, f"{IAM}/file?name={inc}", sid)[0],
                404,
                f"{inc} 这一轮已经作废了还能下到，发出去就是发了一份不存在的增量",
            )

    def test_discard_cleans_up_a_half_deleted_round(self):
        """csv / 说明 / 增量副本被手工删剩哪几个，作废都要把剩下的清掉。

        清不掉的那个会一直挡着后面的确认（说明），或一直列在页面上（csv）。
        """
        with self.live() as live:
            sid = self.login(live)
            for keep in (
                ("csv",),
                ("meta",),
                ("increment",),
                ("csv", "meta"),
                ("csv", "increment"),
                ("meta", "increment"),
            ):
                with self.subTest(keep=keep):
                    name = self.export(live, sid)["recorded"]
                    files = {
                        "csv": self.pending_dir / name,
                        "meta": self.pending_dir / f"{name}.meta.json",
                        "increment": self.pending_dir / self.inc_name(name),
                    }
                    for kind, path in files.items():
                        self.assertTrue(path.is_file(), f"导出没写出 {kind}：{path}")
                        if kind not in keep:
                            path.unlink()
                    status, data = self.api(
                        live, sid, {"op": "discard", "name": name}, method="POST"
                    )
                    self.assertEqual(status, 200, f"只剩 {keep} 时作废失败：{data}")
                    self.assertEqual(data.get("discarded"), name, f"响应没说作废了哪一份：{data}")
                    self.assertEqual(
                        sorted(p.name for p in self.pending_dir.iterdir()),
                        [],
                        f"只剩 {keep} 时没清干净：残留会一直挡着后面的确认",
                    )
                    self.assertTrue((self.id_dir / "people.json").is_file(), "作废删到了名册")
                    self.assertEqual(
                        sorted(p.name for p in self.sent_dir.glob("*.csv")),
                        [],
                        "作废动到了已确认存档目录",
                    )

    def test_discard_refuses_a_confirmed_baseline(self):
        """基线一旦被删，下一轮就拿不到「上次发了什么」，remove 会整批漏掉。"""
        with self.live() as live:
            sid = self.login(live)
            name = self.make_baseline(live, sid)
            archive = self.sent_dir / name
            status, data = self.api(live, sid, {"op": "discard", "name": name}, method="POST")
            self.assertEqual(status, 409, f"已确认的基线被允许作废了：{status} {data}")
            self.assertTrue(data.get("error"), f"拒绝了但没给理由：{data}")
            self.assertTrue(archive.is_file(), f"基线 {archive} 被删了")
            self.assertEqual(self.api(live, sid)[1]["baseline"]["name"], name, "基线在面板上也没了")

    def test_discard_rejects_paths_and_unknown_names(self):
        with self.live() as live:
            sid = self.login(live)
            name = self.export(live, sid)["recorded"]
            outside = self.root / "outside.csv"
            outside.write_text("keep me", encoding="utf-8")
            for bad in (
                "",
                ".",
                "..",
                # 这几个都指向 pending/ 之外真实存在的文件：路径拼接一旦漏防就会真删掉
                "../people.json",
                "../../people.json",
                "../../iam-attributes.csv",
                "../../../outside.csv",
                "..\\people.json",
                f"pending/{name}",
                f"/identity/iam-sent/pending/{name}",
                "iam-attributes.csv",  # 增量输出不是待确认存档
                "20260101-000000.csv",  # 名字合法但不存在
            ):
                with self.subTest(name=bad):
                    status, data = self.api(
                        live, sid, {"op": "discard", "name": bad}, method="POST"
                    )
                    self.assertIn(status, (404, 409), f"{bad!r} 被接受了：{status} {data}")
                    self.assertTrue(data.get("error"), f"{bad!r} 拒绝了但没给理由：{data}")
            self.assertTrue((self.pending_dir / name).is_file(), "合法的待确认存档被误删")
            self.assertTrue((self.id_dir / "people.json").is_file(), "名册被删了")
            self.assertTrue((self.id_dir / "iam-attributes.csv").is_file(), "增量输出被删了")
            self.assertTrue(outside.is_file(), "删到了 identity/ 之外的文件")

    def test_export_works_again_after_discarding(self):
        """作废的意义就在这里：上一份没发出去，这一轮要能重来一次并确认。"""
        with self.live() as live:
            sid = self.login(live)
            stale = self.export(live, sid)["recorded"]
            status, data = self.api(live, sid, {"op": "discard", "name": stale}, method="POST")
            self.assertEqual(status, 200, f"作废失败：{data}")

            data = self.export(live, sid)
            fresh = data["recorded"]
            self.assertEqual(
                [p["name"] for p in data["pending"]],
                [fresh],
                f"作废后重导，pending 应当只剩新的这一份：{data['pending']}",
            )
            self.assertTrue((self.pending_dir / fresh).is_file(), f"{fresh} 没落盘")
            self.assertTrue((self.pending_dir / f"{fresh}.meta.json").is_file(), "缺存档说明")
            status, data = self.api(live, sid, {"op": "confirm", "name": fresh}, method="POST")
            self.assertEqual(status, 200, f"重导的这份确认不了：{data}")
            self.assertEqual(data["baseline"]["name"], fresh)

    def test_discarding_the_stale_export_unblocks_the_newer_one(self):
        """连点两次导出：更早那份没发出去，作废它之后更新那份必须能确认成基线。

        不然管理员会被永久卡住——旧那份不能确认（IT 没导入过），新那份又被旧那份挡着。
        """
        with self.live() as live:
            sid = self.login(live)
            stale = self.export(live, sid)["recorded"]
            fresh = self.export(live, sid)["recorded"]
            self.assertNotEqual(stale, fresh, "连点两次导出互相覆盖了，只剩一份存档")

            status, data = self.api(live, sid, {"op": "confirm", "name": fresh}, method="POST")
            self.assertEqual(status, 409, f"更早那份还在 pending 里，不该能确认更新的：{data}")
            self.assertIn(stale, data.get("error", ""), f"没告诉管理员是谁挡着：{data}")

            status, data = self.api(live, sid, {"op": "discard", "name": stale}, method="POST")
            self.assertEqual(status, 200, f"作废更早那份失败：{data}")

            status, data = self.api(live, sid, {"op": "confirm", "name": fresh}, method="POST")
            self.assertEqual(status, 200, f"作废之后仍然确认不了，管理员被卡死：{data}")
            self.assertEqual(data["baseline"]["name"], fresh)
            self.assertEqual(data["pending"], [], f"确认后还留着待确认存档：{data['pending']}")
            self.assertTrue((self.sent_dir / fresh).is_file(), f"{fresh} 没转成正式基线")

    def test_orphan_meta_does_not_block_confirming_a_newer_archive(self):
        """pending 里只剩一份「说明」（csv 已经不在）时，它没有存档可确认，不该挡着后面的。

        挡住的话管理员会被永久卡死：那一轮没东西可确认，新的一轮又被它拦着。
        """
        with self.live() as live:
            sid = self.login(live)
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            orphan = self.pending_dir / "20200101-000000.csv.meta.json"
            # 同一个基线（都还没有基线，都是 ""）：不看「csv 还在不在」就一定会拦
            orphan.write_text(json.dumps({"baseline": ""}), encoding="utf-8")

            fresh = self.export(live, sid)["recorded"]
            status, data = self.api(live, sid, {"op": "confirm", "name": fresh}, method="POST")
            self.assertEqual(status, 200, f"一份只剩说明的旧记录把确认挡死了：{data}")
            self.assertEqual(data["baseline"]["name"], fresh)
            self.assertTrue((self.sent_dir / fresh).is_file(), f"{fresh} 没转成正式基线")
            self.assertEqual(
                [p["name"] for p in data["pending"]],
                [],
                f"孤儿说明不该被列成待确认存档：{data['pending']}",
            )

            # 清掉它同样要走得通，否则它会一直留在目录里
            status, data = self.api(
                live, sid, {"op": "discard", "name": "20200101-000000.csv"}, method="POST"
            )
            self.assertEqual(status, 200, f"只剩说明的那份作废不了，它会永远留着：{data}")
            self.assertFalse(orphan.exists(), f"{orphan.name} 没删掉")

    # ── 并发 ──────────────────────────────────────────────────────────────

    def test_concurrent_exports_each_keep_their_own_archive(self):
        """两个管理员同时点导出：串行闸要让两份存档各占各的名字，不能一份盖掉另一份。"""
        with self.live() as live:
            sid = self.login(live)
            ready = threading.Barrier(2)
            results = []
            guard = threading.Lock()

            def go():
                ready.wait(timeout=10)
                out = self.api(live, sid, {"op": "export"}, method="POST")
                with guard:
                    results.append(out)

            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            self.assertEqual(
                [t.name for t in threads if t.is_alive()], [], "导出线程没结束：串行闸没放锁？"
            )
            self.assertEqual(
                sorted(status for status, _ in results), [200, 200], f"并发导出失败：{results}"
            )
            names = sorted(data["recorded"] for _, data in results)
            self.assertEqual(len(set(names)), 2, f"两次并发导出算出了同一个存档名：{names}")
            for name in names:
                self.assertTrue(
                    (self.pending_dir / name).is_file(),
                    f"{name} 不在磁盘上：被另一次导出覆盖了（{names}）",
                )
                self.assertTrue(
                    (self.pending_dir / f"{name}.meta.json").is_file(), f"{name} 缺存档说明"
                )
            self.assertEqual(
                sorted(p["name"] for p in self.api(live, sid)[1]["pending"]),
                names,
                "面板列出的待确认存档和磁盘对不上",
            )

    def test_exclusive_really_takes_an_exclusive_lock(self):
        """上一条证明结果对；这条证明它是靠真锁保证的，而不是恰好没撞上。"""
        paths = iam_sync.SyncPaths(
            people="identity/people.json",
            attributes="identity/attrs.json",
            out="identity/iam-attributes.csv",
        )
        with iam_sync.exclusive(paths):
            lock = self.sent_dir / ".lock"
            self.assertTrue(lock.is_file(), f"锁文件没建出来：{lock}")
            fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with self.assertRaises(BlockingIOError, msg="锁内还能再拿到排他锁：闸没生效"):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        # 出了 with 必须放锁，否则第二个管理员永远点不动导出
        fd = os.open(self.sent_dir / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:  # pragma: no cover - 只在回归时触发
            self.fail("exclusive() 退出后没放锁")
        finally:
            os.close(fd)


class ServeIamOutTests(unittest.TestCase):
    """`delivery serve --iam-out` 指进存档目录 → 关掉属性表页，而不是把增量写进存档目录。

    增量含 remove 行，落进 identity/iam-sent/ 会被当成基线，check_baseline 随后永久拒绝。
    """

    def setUp(self):
        self._cwd = Path.cwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        (self.root / "identity").mkdir()

    def request_paths(self, iam_out, *extra):
        args = cli.build_parser().parse_args(["serve", *extra, "--iam-out", iam_out])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli._request_paths(args), buf.getvalue()

    def request_paths_from(self, args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return cli._request_paths(args), buf.getvalue()

    def test_iam_out_inside_sent_dir_turns_the_page_off(self):
        for bad in (
            "identity/iam-sent/20260101-000000.csv",
            "identity/iam-sent/pending/20260101-000000.csv",
            "identity/iam-sent/../iam-sent/x.csv",
        ):
            with self.subTest(iam_out=bad):
                out, printed = self.request_paths(bad)
                self.assertNotIn(
                    "iam_out_path", out, f"{bad} 仍然开着属性表页：增量会被写进存档目录"
                )
                self.assertNotIn("iam_spec_path", out, f"{bad} 只关了一半")
                self.assertIn("IAM 属性表已关闭", printed, f"没提示为什么没有这个页面：{printed!r}")
                self.assertIn("iam-sent", printed, f"提示没说清是路径的问题：{printed!r}")

    def test_iam_out_outside_sent_dir_keeps_the_page_on(self):
        out, printed = self.request_paths("identity/iam-attributes.csv")
        self.assertEqual(out.get("iam_out_path"), "identity/iam-attributes.csv")
        self.assertEqual(out.get("iam_spec_path"), iam_sync.DEFAULT_SPEC)
        self.assertNotIn("IAM 属性表已关闭", printed, f"正常路径也被关掉了：{printed!r}")

    def test_iam_out_outside_identity_turns_the_page_off(self):
        out, printed = self.request_paths(str(self.root / "elsewhere.csv"))
        self.assertNotIn("iam_out_path", out, "属性表含全员邮箱，不该写到 identity/ 之外")
        self.assertIn("IAM 属性表已关闭", printed)

    def test_iam_out_pointing_at_another_managed_file_turns_the_page_off(self):
        """导出是原子替换：--iam-out 指到名册 / 快照 / 申请单上，一次导出就把它整个冲掉。"""
        for bad in (
            "identity/people.json",
            "identity/inventory.json",
            "identity/tickets.json",
            "identity/request-templates.json",
            "identity/approval.json",
            "identity/sso-map.proposal.json",
            "identity/manual-links.json",
            "identity/assets.json",
            "identity/policies.json",
            "identity/policy-rules.json",
            "identity/accounts.json",
            "identity/iam-attributes.json",
            "identity/../identity/people.json",  # 绕一下也是同一个文件
            "identity/./people.json",
        ):
            with self.subTest(iam_out=bad):
                out, printed = self.request_paths(bad)
                self.assertNotIn("iam_out_path", out, f"{bad} 仍开着属性表页：一次导出就把它覆盖了")
                self.assertNotIn("iam_spec_path", out, f"{bad} 只关了一半")
                self.assertIn(
                    "面板管理的其它文件", printed, f"没说清为什么没有这个页面：{printed!r}"
                )

    def test_iam_out_matching_an_explicit_admins_path_turns_the_page_off(self):
        """--admins 是显式给的时候，它同样是面板托管的文件（覆盖了就没人是管理员了）。"""
        out, printed = self.request_paths(
            "identity/admins.json", "--admins", "identity/admins.json"
        )
        self.assertNotIn("iam_out_path", out, "属性表能覆盖管理员名单")
        self.assertIn("面板管理的其它文件", printed, f"没说清原因：{printed!r}")

    def test_managed_check_skips_args_the_namespace_does_not_carry(self):
        """各子命令传进来的 Namespace 不一定带齐所有参数：缺的按名字跳过，不能 AttributeError。"""
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/request-templates.json",
            approval="identity/approval.json",
            assets="identity/assets.json",
            policies="identity/policies.json",
            policy_rules="identity/policy-rules.json",
            iam_attributes="identity/iam-attributes.json",
            iam_out="identity/iam-attributes.csv",
        )  # 故意不带 people / bindings / admins / labels / inventory / proposal / manual
        out, printed = self.request_paths_from(args)
        self.assertEqual(
            out.get("iam_out_path"),
            "identity/iam-attributes.csv",
            f"精简 Namespace 把属性表页关掉了：{printed!r}",
        )
        self.assertNotIn("IAM 属性表已关闭", printed, f"正常路径被关掉了：{printed!r}")

    def test_managed_check_still_fires_on_a_slim_namespace(self):
        """跳过缺的参数，不等于连带齐的那几个也不查。"""
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/request-templates.json",
            approval="identity/approval.json",
            assets="identity/assets.json",
            policies="identity/policies.json",
            policy_rules="identity/policy-rules.json",
            iam_attributes="identity/iam-attributes.json",
            iam_out="identity/tickets.json",
        )
        out, printed = self.request_paths_from(args)
        self.assertNotIn("iam_out_path", out, "属性表能覆盖申请单")
        self.assertIn("面板管理的其它文件", printed, f"没说清原因：{printed!r}")


if __name__ == "__main__":
    unittest.main()
