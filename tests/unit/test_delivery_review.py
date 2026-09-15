"""名册审核：确认 / 驳回 / 分配 / 服务号 / 撤销，union_id 与登录绑定不丢，并发与校验。数据虚构。"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path

from delivery import people as people_mod
from delivery.review import ReviewError, ReviewPaths, apply, records

ALI = "aliyun/100"


def link(name, status="confirmed"):
    return {"scope": ALI, "name": name, "status": status, "display_name": ""}


PROPOSAL = {
    "schema": "wuji-sso-map/proposal@1",
    "domain": "wuji.tech",
    "people": [
        {"email": "li.si@wuji.tech", "links": [link("lisi"), link("lisi2", "review")]},
        {"email": "wang.wu@wuji.tech", "links": [link("wangwu")]},
    ],
    "unlinked": [{"scope": ALI, "name": "ghost", "display_name": "", "reason": "无邮箱"}],
    "services": [{"scope": ALI, "name": "ci-bot"}],
}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.paths = ReviewPaths(
            proposal=str(self.dir / "proposal.json"),
            manual=str(self.dir / "manual-links.json"),
            people=str(self.dir / "people.json"),
            bindings=str(self.dir / "bindings.json"),
        )
        (self.dir / "proposal.json").write_text(json.dumps(PROPOSAL), encoding="utf-8")
        self.write_roster({"wang.wu@wuji.tech": "on_wang"})

    def write_roster(self, uids, extra=()):
        built = people_mod.build(PROPOSAL, [])
        for row in built["people"]:
            row["union_id"] = uids.get(row["email"], "")
        built["people"].extend(extra)
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")

    def write_bindings(self, entries):
        data = {"schema": people_mod.BINDINGS_SCHEMA, "bindings": entries}
        (self.dir / "bindings.json").write_text(json.dumps(data), encoding="utf-8")

    def index(self):
        return people_mod.load(self.paths.people, bindings_path=self.paths.bindings)

    def raw(self):
        return json.loads((self.dir / "people.json").read_text(encoding="utf-8"))

    def do(self, **action):
        return apply(self.paths, action, actor_union_id="on_admin")

    def person(self, email):
        return next(p for p in self.index().people if p.email == email)

    def names(self, person, attr="accounts"):
        return sorted(r.name for r in getattr(person, attr))

    def test_confirm_moves_pending_to_confirmed(self):
        self.do(op="confirm", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        p = self.person("li.si@wuji.tech")
        self.assertEqual(self.names(p), ["lisi", "lisi2"])
        self.assertEqual(self.names(p, "pending"), [])
        manual = json.loads((self.dir / "manual-links.json").read_text(encoding="utf-8"))
        self.assertIn(f"{ALI}/lisi2", manual["links"]["li.si@wuji.tech"]["accounts"])
        for name in ("people.json", "manual-links.json"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o600)

    def test_reject_removes_pending_and_survives_rebuild(self):
        self.do(op="reject", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        p = self.person("li.si@wuji.tech")
        self.assertEqual(self.names(p, "pending"), [])
        self.assertIn("lisi2", [u.name for u in self.index().unlinked])
        manual = json.loads((self.dir / "manual-links.json").read_text(encoding="utf-8"))
        merged = people_mod.apply_manual(PROPOSAL, manual)
        owners = [
            r["email"] for r in merged["people"] for lk in r["links"] if lk["name"] == "lisi2"
        ]
        self.assertEqual(owners, [])

    def test_reject_with_manual_link_refused(self):
        manual = {"links": {"li.si@wuji.tech": {"accounts": [f"{ALI}/ghost"]}}}
        (self.dir / "manual-links.json").write_text(json.dumps(manual), encoding="utf-8")
        built = people_mod.build(people_mod.apply_manual(PROPOSAL, manual), [])
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")
        with self.assertRaises(ReviewError):
            # ghost 已人工确认给李四，不在待确认里
            self.do(op="reject", email="li.si@wuji.tech", account=f"{ALI}/ghost")

    def test_assign_unlinked(self):
        self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")
        self.assertIn("ghost", self.names(self.person("li.si@wuji.tech")))
        self.assertNotIn("ghost", [u.name for u in self.index().unlinked])

    def test_assign_already_owned_rejected(self):
        with self.assertRaises(ReviewError) as ctx:
            self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/wangwu")
        self.assertEqual(ctx.exception.status, 409)

    def test_service_marks_and_undo_restores(self):
        self.do(op="service", account=f"{ALI}/ghost")
        self.assertEqual({u.name: u.kind for u in self.index().unlinked}["ghost"], "service")
        self.do(op="undo", account=f"{ALI}/ghost")
        self.assertEqual({u.name: u.kind for u in self.index().unlinked}["ghost"], "unknown")

    def test_undo_without_record_rejected(self):
        with self.assertRaises(ReviewError):
            self.do(op="undo", account=f"{ALI}/ghost")

    def test_union_id_kept_when_account_added(self):
        self.do(op="assign", email="wang.wu@wuji.tech", account=f"{ALI}/ghost")
        p = self.person("wang.wu@wuji.tech")
        self.assertEqual(p.union_id, "on_wang")
        self.assertEqual(self.names(p), ["ghost", "wangwu"])

    def test_assign_to_directory_only_new_hire_keeps_union_id(self):
        # 审计 A：通讯录里的新人（有 union_id、没有云账号）分配账号后不能丢 union_id
        newbie = {
            "union_id": "on_new",
            "name": "新人",
            "email": "new@wuji.tech",
            "accounts": [],
            "pending": [],
        }
        self.write_roster({"wang.wu@wuji.tech": "on_wang"}, extra=[newbie])
        self.do(op="assign", email="new@wuji.tech", account=f"{ALI}/ghost")
        p = self.person("new@wuji.tech")
        self.assertEqual((p.union_id, self.names(p)), ("on_new", ["ghost"]))
        found = self.index().resolve(union_id="on_other", enterprise_email="new@wuji.tech")
        self.assertNotEqual(found.binding, "bound_now")

    def test_confirm_first_pending_keeps_union_id(self):
        proposal = dict(PROPOSAL)
        proposal["people"] = [
            *PROPOSAL["people"],
            {"email": "p@wuji.tech", "links": [link("pp", "review")]},
        ]
        (self.dir / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
        built = people_mod.build(proposal, [])
        for row in built["people"]:
            row["union_id"] = {"p@wuji.tech": "on_p", "wang.wu@wuji.tech": "on_wang"}.get(
                row["email"], ""
            )
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")
        self.do(op="service", account=f"{ALI}/ghost")  # 无关操作
        self.assertEqual(self.person("p@wuji.tech").union_id, "on_p")
        self.do(op="confirm", email="p@wuji.tech", account=f"{ALI}/pp")
        self.assertEqual(self.person("p@wuji.tech").union_id, "on_p")

    def test_rejecting_only_link_keeps_person(self):
        self.write_roster({"li.si@wuji.tech": "on_li"})
        proposal = dict(PROPOSAL)
        proposal["people"] = [
            {"email": "li.si@wuji.tech", "links": [link("lisi2", "review")]},
            PROPOSAL["people"][1],
        ]
        (self.dir / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
        built = people_mod.build(proposal, [])
        for row in built["people"]:
            row["union_id"] = {"li.si@wuji.tech": "on_li"}.get(row["email"], "")
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")
        self.do(op="reject", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        self.assertEqual(self.person("li.si@wuji.tech").union_id, "on_li")

    def test_login_binding_not_written_into_roster(self):
        # 审计 B：绑定只在 bindings.json；审核之后删掉绑定必须立刻生效
        self.write_roster({})
        self.write_bindings(
            {"on_li": {"accounts": [f"{ALI}/lisi"], "email": "li.si@wuji.tech", "name": "李四"}}
        )
        self.assertEqual(self.person("li.si@wuji.tech").union_id, "on_li")
        self.do(op="service", account=f"{ALI}/ghost")
        self.assertTrue(all(not r["union_id"] for r in self.raw()["people"]))
        self.write_bindings({})
        self.assertEqual(self.person("li.si@wuji.tech").union_id, "")

    def test_login_binding_fingerprint_updated(self):
        self.write_roster({})
        self.write_bindings(
            {"on_li": {"accounts": [f"{ALI}/lisi"], "email": "li.si@wuji.tech", "name": "李四"}}
        )
        result = self.do(op="confirm", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        self.assertEqual(result["rebound"], ["on_li"])
        index = self.index()
        self.assertEqual(index.warnings, [])
        self.assertEqual(index.resolve(union_id="on_li").person.email, "li.si@wuji.tech")
        self.assertTrue(all(not r["union_id"] for r in self.raw()["people"]))

    def test_rebind_refused_when_account_held_by_other_binding(self):
        self.write_roster({})
        self.write_bindings(
            {
                "on_li": {"accounts": [f"{ALI}/lisi"], "email": "li.si@wuji.tech", "name": "李四"},
                "on_x": {"accounts": [f"{ALI}/ghost"], "email": "x@wuji.tech", "name": "X"},
            }
        )
        before = (self.dir / "people.json").read_text(encoding="utf-8")
        with self.assertRaises(ReviewError) as ctx:
            self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual((self.dir / "people.json").read_text(encoding="utf-8"), before)
        self.assertFalse((self.dir / "manual-links.json").exists())

    def test_blocked_binding_account_cannot_be_assigned(self):
        # 审计：on_q 的绑定对不上名册（被暂停），它认领过的 ghost 不能分给任何人
        self.write_bindings(
            {"on_q": {"accounts": [f"{ALI}/ghost"], "email": "q@wuji.tech", "name": "Q"}}
        )
        self.assertIn(f"{ALI}/ghost", self.index()._blocked_accounts)
        for email in ("li.si@wuji.tech", "wang.wu@wuji.tech"):
            with self.assertRaises(ReviewError, msg=email) as ctx:
                self.do(op="assign", email=email, account=f"{ALI}/ghost")
            self.assertEqual(ctx.exception.status, 409)

    def test_undo_wrong_assignment_to_login_bound_person_drops_binding(self):
        self.write_roster({})
        self.write_bindings(
            {"on_li": {"accounts": [f"{ALI}/lisi"], "email": "li.si@wuji.tech", "name": "李四"}}
        )
        self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")
        # 管理员发现分错了，撤销后又把李四唯一确认的号也驳回（经人工记录撤销）
        self.do(op="undo", account=f"{ALI}/ghost")
        self.assertEqual(self.names(self.person("li.si@wuji.tech")), ["lisi"])
        bindings = json.loads((self.dir / "bindings.json").read_text(encoding="utf-8"))
        self.assertEqual(bindings["bindings"]["on_li"]["accounts"], [f"{ALI}/lisi"])

    def test_undo_only_link_of_login_bound_person_removes_binding(self):
        proposal = dict(PROPOSAL)
        proposal["people"] = [PROPOSAL["people"][1]]
        (self.dir / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
        manual = {"links": {"li.si@wuji.tech": {"name": "李四", "accounts": [f"{ALI}/ghost"]}}}
        (self.dir / "manual-links.json").write_text(json.dumps(manual), encoding="utf-8")
        built = people_mod.build(people_mod.apply_manual(proposal, manual), [])
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")
        self.write_bindings(
            {"on_li": {"accounts": [f"{ALI}/ghost"], "email": "li.si@wuji.tech", "name": "李四"}}
        )
        result = self.do(op="undo", account=f"{ALI}/ghost")
        self.assertEqual(result["rebound"], ["on_li（已解除）"])
        bindings = json.loads((self.dir / "bindings.json").read_text(encoding="utf-8"))
        self.assertNotIn("on_li", bindings["bindings"])

    def test_hand_edited_manual_adding_new_email_refused(self):
        manual = {"links": {"evil@wuji.tech": {"accounts": [f"{ALI}/ci-bot"]}}}
        (self.dir / "manual-links.json").write_text(json.dumps(manual), encoding="utf-8")
        with self.assertRaises(ReviewError) as ctx:
            self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")
        self.assertEqual(ctx.exception.status, 409)

    def test_stale_proposal_refused(self):
        # 提案在名册生成之后被重新生成过：拒绝，不猜
        changed = json.loads(json.dumps(PROPOSAL))
        changed["people"][1]["links"].append(link("wangwu-new"))
        (self.dir / "proposal.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(ReviewError) as ctx:
            self.do(op="assign", email="wang.wu@wuji.tech", account=f"{ALI}/ghost")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("refresh", str(ctx.exception))

    def test_second_admin_cannot_override_first(self):
        # 审计 C：A 已确认 lisi2 给李四；B 页面过期，再分配给王五 / 驳回都必须失败
        self.do(op="confirm", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        with self.assertRaises(ReviewError):
            self.do(op="assign", email="wang.wu@wuji.tech", account=f"{ALI}/lisi2")
        with self.assertRaises(ReviewError):
            self.do(op="reject", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        self.assertIn("lisi2", self.names(self.person("li.si@wuji.tech")))

    def test_confirm_keeps_other_peoples_rejections(self):
        manual = {
            "links": {},
            "rejected": {f"{ALI}/ghost": ["wang.wu@wuji.tech", "li.si@wuji.tech"]},
        }
        (self.dir / "manual-links.json").write_text(json.dumps(manual), encoding="utf-8")
        self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")
        saved = json.loads((self.dir / "manual-links.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["rejected"][f"{ALI}/ghost"], ["wang.wu@wuji.tech"])

    def test_email_collision_target_refused(self):
        built = people_mod.build(PROPOSAL, [])
        for row in built["people"]:
            if row["email"] == "li.si@wuji.tech":
                row["email_collision"] = True
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")
        with self.assertRaises(ReviewError):
            self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")

    def test_email_collision_flag_preserved(self):
        built = people_mod.build(PROPOSAL, [])
        for row in built["people"]:
            if row["email"] == "wang.wu@wuji.tech":
                row["email_collision"] = True
        (self.dir / "people.json").write_text(json.dumps(built), encoding="utf-8")
        self.do(op="service", account=f"{ALI}/ghost")
        wang = next(r for r in self.raw()["people"] if r["email"] == "wang.wu@wuji.tech")
        self.assertTrue(wang["email_collision"])

    def test_stale_page_errors(self):
        with self.assertRaises(ReviewError):
            self.do(op="confirm", email="wang.wu@wuji.tech", account=f"{ALI}/lisi2")
        with self.assertRaises(ReviewError):
            self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/not-in-proposal")
        with self.assertRaises(ReviewError):
            self.do(op="assign", email="nobody@wuji.tech", account=f"{ALI}/ghost")
        with self.assertRaises(ReviewError):
            self.do(op="drop", account=f"{ALI}/ghost")
        with self.assertRaises(ReviewError):
            self.do(op="service", account="aliyun/100")

    def test_locked_by_refresh(self):
        fd = os.open(self.paths.lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ReviewError) as ctx:
                self.do(op="service", account=f"{ALI}/ghost")
            self.assertEqual(ctx.exception.status, 409)
        finally:
            os.close(fd)
        self.assertFalse((self.dir / "manual-links.json").exists())

    def test_audit_log_is_json_lines_and_records(self):
        self.do(op="assign", email="li.si@wuji.tech", account=f"{ALI}/ghost")
        self.do(op="reject", email="li.si@wuji.tech", account=f"{ALI}/lisi2")
        lines = [json.loads(x) for x in self.paths.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([x["op"] for x in lines], ["assign", "reject"])
        self.assertEqual(lines[0]["actor"], "on_admin")
        self.assertEqual(self.paths.log.stat().st_mode & 0o777, 0o600)
        kinds = sorted((r["kind"], r["account"]) for r in records(self.paths.manual))
        self.assertEqual(kinds, [("link", f"{ALI}/ghost"), ("rejected", f"{ALI}/lisi2")])

    def test_corrupt_manual_is_server_error(self):
        (self.dir / "manual-links.json").write_text(
            '{"links": {}, "rejected": [1]}', encoding="utf-8"
        )
        with self.assertRaises(ReviewError) as ctx:
            records(self.paths.manual)
        self.assertEqual(ctx.exception.status, 500)


class ApplyManualExtensionTests(unittest.TestCase):
    def test_service_and_link_conflict_rejected(self):
        manual = {
            "links": {"li.si@wuji.tech": {"accounts": [f"{ALI}/ghost"]}},
            "services": [f"{ALI}/ghost"],
        }
        with self.assertRaises(people_mod.PeopleError):
            people_mod.apply_manual(PROPOSAL, manual)

    def test_bad_shapes_rejected(self):
        for manual in (
            {"links": {}, "rejected": ["x"]},
            {"links": {}, "rejected": {f"{ALI}/x": "a@wuji.tech"}},
            {"links": {}, "services": {"a": 1}},
            {"links": {}, "services": ["aliyun/100"]},
        ):
            with self.assertRaises(people_mod.PeopleError, msg=manual):
                people_mod.apply_manual(PROPOSAL, manual)

    def test_rejected_for_other_person_keeps_link(self):
        manual = {"links": {}, "rejected": {f"{ALI}/lisi2": ["someone@wuji.tech"]}}
        merged = people_mod.apply_manual(PROPOSAL, manual)
        li = next(r for r in merged["people"] if r["email"] == "li.si@wuji.tech")
        self.assertIn("lisi2", [lk["name"] for lk in li["links"]])
        self.assertNotIn("lisi2", [u["name"] for u in merged["unlinked"]])

    def test_old_manual_without_new_keys_unchanged(self):
        manual = {"links": {"wang.wu@wuji.tech": {"accounts": [f"{ALI}/ghost"]}}}
        merged = people_mod.apply_manual(PROPOSAL, manual)
        self.assertEqual([u["name"] for u in merged["unlinked"]], [])
        self.assertEqual([s["name"] for s in merged["services"]], ["ci-bot"])


class ReviewApiTests(unittest.TestCase):
    """HTTP 层：管理员才能改、CSRF 防护、错误不泄露服务端细节。"""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from delivery.feishu import FeishuUser
        from delivery.registry import PlatformRegistry
        from delivery.server import Backend, Store, _WebSession, make_handler

        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "proposal.json").write_text(json.dumps(PROPOSAL), encoding="utf-8")
        (self.dir / "people.json").write_text(
            json.dumps(people_mod.build(PROPOSAL, [])), encoding="utf-8"
        )
        (self.dir / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}))
        backend = Backend(
            people_path=str(self.dir / "people.json"),
            bindings_path=str(self.dir / "bindings.json"),
            admins_path=str(self.dir / "admins.json"),
            proposal_path=str(self.dir / "proposal.json"),
            manual_path=str(self.dir / "manual-links.json"),
            platforms={"aliyun": "阿里云"},
        )
        store = Store()
        store.sessions["admin"] = _WebSession(user=FeishuUser("ou", "on_admin", "管理员"))
        store.sessions["user"] = _WebSession(user=FeishuUser("ou", "on_x", "某人"))
        handler = make_handler(
            PlatformRegistry.load(),
            store,
            app_id="cli_demo",
            app_secret="s",
            base_url="http://127.0.0.1",
            backend=backend,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.host = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, *, sid="admin", headers=None):
        import http.client

        from delivery.server import COOKIE_NAME

        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        head = {
            "Cookie": f"{COOKIE_NAME}={sid}",
            "Content-Type": "application/json",
            "X-Panel-Request": "1",
            "Origin": f"http://{self.host}",
        }
        head.update(headers or {})
        head = {k: v for k, v in head.items() if v is not None}
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("POST", "/api/admin/review", body=raw, headers=head)
        resp = conn.getresponse()
        out = (resp.status, json.loads(resp.read() or b"{}"))
        conn.close()
        return out

    def get(self, sid="admin"):
        import http.client

        from delivery.server import COOKIE_NAME

        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        conn.request("GET", "/api/admin/review", headers={"Cookie": f"{COOKIE_NAME}={sid}"})
        resp = conn.getresponse()
        out = (resp.status, json.loads(resp.read() or b"{}"))
        conn.close()
        return out

    ASSIGN = {"op": "assign", "email": "li.si@wuji.tech", "account": f"{ALI}/ghost"}

    def test_admin_assign_updates_roster(self):
        status, data = self.post(self.ASSIGN)
        self.assertEqual(status, 200, data)
        roster = people_mod.load(str(self.dir / "people.json"))
        li = next(p for p in roster.people if p.email == "li.si@wuji.tech")
        self.assertIn("ghost", [r.name for r in li.accounts])
        status, data = self.get()
        self.assertEqual(status, 200)
        self.assertEqual([r["account"] for r in data["records"]], [f"{ALI}/ghost"])

    def test_non_admin_and_anonymous_refused(self):
        self.assertEqual(self.post(self.ASSIGN, sid="user")[0], 403)
        self.assertEqual(self.post(self.ASSIGN, sid="nope")[0], 401)
        self.assertEqual(self.get(sid="user")[0], 403)
        self.assertFalse((self.dir / "manual-links.json").exists())

    def test_csrf_checks(self):
        cases = [
            {"X-Panel-Request": None},
            {"Content-Type": "text/plain"},
            {"Content-Type": "application/x-www-form-urlencoded"},
            {"Origin": "https://evil.example.com"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ]
        for headers in cases:
            status, data = self.post(self.ASSIGN, headers=headers)
            self.assertEqual(status, 403, headers)
        self.assertFalse((self.dir / "manual-links.json").exists())

    def test_bad_bodies(self):
        self.assertEqual(self.post(b"{bad")[0], 400)
        self.assertEqual(self.post(b"[1]")[0], 400)
        self.assertEqual(self.post({"op": "assign", "pad": "x" * 5000})[0], 400)
        status, data = self.post(
            {"op": "assign", "email": "nobody@wuji.tech", "account": f"{ALI}/ghost"}
        )
        self.assertEqual(status, 400)
        self.assertIn("没有这个邮箱", data["error"])

    def test_non_post_methods_do_not_mutate(self):
        import http.client

        for method in ("HEAD", "OPTIONS", "PUT", "DELETE"):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
            conn.request(method, "/api/admin/review", body=json.dumps(self.ASSIGN))
            status = conn.getresponse().status
            conn.close()
            self.assertNotEqual(status, 200, method)
        self.assertFalse((self.dir / "manual-links.json").exists())

    def test_origin_matching_base_url_accepted_behind_proxy(self):
        # 经 nginx 转发后 Host 是内部地址，Origin 是对外地址 base_url
        status, data = self.post(
            self.ASSIGN, headers={"Host": "127.0.0.1:4180", "Origin": "http://127.0.0.1"}
        )
        self.assertEqual(status, 200, data)

    def test_server_error_hides_details(self):
        (self.dir / "proposal.json").write_text("{broken", encoding="utf-8")
        import contextlib
        import io

        with contextlib.redirect_stderr(io.StringIO()):
            status, data = self.post(self.ASSIGN)
        self.assertEqual(status, 500)
        self.assertNotIn(str(self.dir), data["error"])


if __name__ == "__main__":
    unittest.main()
