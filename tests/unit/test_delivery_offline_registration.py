"""管理后台「人工登记」：粘贴控制台名单 → 预览差异 → 保存（九章这类没有采集接口的平台）。

分三层锁：
  · `parse_paste` —— 按内容认列；手机号那一列**丢掉，绝不落盘**；登录名要带前缀，
    主账号名 `wuji` 不是前缀、不能被剥掉
  · `diff` / `save_account` —— 整体替换的语义下，粘漏半页 = 半个名单从名册里消失，
    所以有大比例移除闸门；幂等、history 上限、0600、同文件里别的账号不受影响
  · `/api/admin/offline-accounts` —— 管理员专属；preview 不写盘，save 才写

离线，数据虚构。
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from delivery import offline_accounts as off
from delivery.feishu import FeishuUser
from delivery.registry import PlatformRegistry
from delivery.server import COOKIE_NAME, Backend, Store, _WebSession, make_handler

PHONES = ("17800001111", "13900002222", "15600003333")

# 从九章控制台用户列表整页复制下来的样子：表头、每行后面的按钮、列之间是 tab，
# 手机号是一整列。第 3 行姓名是「李 四」（控制台导出姓名中间常带空格）
PASTE = (
    "用户管理\n"
    "新建用户\t批量导入\n"
    "序号\t用户名\t登录用户名\t姓名\t手机号\t邮箱\t状态\t创建时间\t操作\n"
    f"1\twangzihan\twuji-wangzihan\t王梓涵\t{PHONES[0]}\twang.zihan@wuji.tech\t正常"
    "\t2026-09-21 15:01:19\t重置密码\t禁用\n"
    "重置密码\n"
    "禁用\n"
    f"2\thuangzenan\twuji-huangzenan\t黄泽楠\t{PHONES[1]}\tHuang.Zenan@Wuji.Tech\t正常"
    "\t2026-09-20 09:00:00\t重置密码\t禁用\n"
    f"3\tlisi\twuji-lisi\t李 四\t{PHONES[2]}\tli.si@wuji.tech\t禁用"
    "\t2026-09-19 10:11:12\t重置密码\t启用\n"
    "共 3 条\t10条/页\n"
)


def names(users):
    return [u["name"] for u in users]


def user(name, email=None, display="", status="正常"):
    return {
        "name": name,
        "display_name": display,
        "email": email or f"{name.replace('wuji-', '')}@wuji.tech",
        "status": status,
        "created": "",
    }


class ParsePasteTests(unittest.TestCase):
    def setUp(self):
        self.users, self.warnings = off.parse_paste(PASTE, login_prefix="wuji-")

    def test_only_data_rows_come_out(self):
        """表头、按钮、分页这些行没有 @，自然被跳过，也不该产生告警噪声。"""
        self.assertEqual(names(self.users), ["wuji-wangzihan", "wuji-huangzenan", "wuji-lisi"])
        self.assertEqual(self.warnings, [])

    def test_columns_are_recognised_by_content(self):
        first = self.users[0]
        self.assertEqual(
            first,
            {
                "name": "wuji-wangzihan",
                "display_name": "王梓涵",
                "email": "wang.zihan@wuji.tech",
                "status": "正常",
                "created": "2026-09-21 15:01:19",
            },
        )
        self.assertEqual(self.users[1]["email"], "huang.zenan@wuji.tech", "邮箱归一成小写")
        self.assertEqual(self.users[2]["display_name"], "李四", "姓名里的空格去掉")

    def test_the_status_column_wins_over_the_trailing_buttons(self):
        """每行后面跟着「禁用」按钮。状态列写「正常」的人不能被按钮文字判成禁用。"""
        self.assertEqual([u["status"] for u in self.users], ["正常", "正常", "禁用"])

    def test_phone_numbers_are_nowhere_in_the_output(self):
        """名册按邮箱关联到人，用不上手机号；多存一份就多一份泄漏面。"""
        dumped = json.dumps(self.users, ensure_ascii=False)
        for phone in PHONES:
            self.assertNotIn(phone, dumped)
        for u in self.users:
            self.assertEqual(set(u), {"name", "display_name", "email", "status", "created"})

    def test_column_order_does_not_matter(self):
        """控制台挪一下列：按位置解析会把手机号当邮箱、把姓名当登录名，而且不报错。"""
        text = f"wang.zihan@wuji.tech\t正常\t{PHONES[0]}\twuji-wangzihan\t王梓涵\t1\n"
        users, _ = off.parse_paste(text, login_prefix="wuji-")
        self.assertEqual(
            (users[0]["name"], users[0]["display_name"], users[0]["email"]),
            ("wuji-wangzihan", "王梓涵", "wang.zihan@wuji.tech"),
        )

    def test_space_separated_paste_works_too(self):
        text = f"1  wangzihan  wuji-wangzihan  王梓涵  {PHONES[0]}  wang.zihan@wuji.tech  正常\n"
        users, _ = off.parse_paste(text, login_prefix="wuji-")
        self.assertEqual(names(users), ["wuji-wangzihan"])
        self.assertNotIn(PHONES[0], json.dumps(users))

    def test_empty_name_column_does_not_pull_the_phone_in(self):
        """姓名留空时，登录名后面紧挨着的就是手机号 —— 不能被当成姓名。"""
        for phone in (PHONES[0], "+86 17800001111", "+86-178-0000-1111"):
            text = f"1\twangzihan\twuji-wangzihan\t\t{phone}\twang.zihan@wuji.tech\t正常\n"
            users, _ = off.parse_paste(text, login_prefix="wuji-")
            self.assertEqual(users[0]["display_name"], "", phone)

    def test_a_masked_phone_is_not_taken_for_the_name(self):
        """BUG（offline_accounts.py:215/255-258）：控制台常把手机号打码成 178****1111。
        `_PHONE` 认不出它，姓名留空时它就被当成 display_name 存进登记表 —— 违反「不登记手机号」。"""
        text = "1\twangzihan\twuji-wangzihan\t\t178****1111\twang.zihan@wuji.tech\t正常\n"
        users, _ = off.parse_paste(text, login_prefix="wuji-")
        self.assertEqual(users[0]["display_name"], "")

    def test_an_unknown_status_word_does_not_fall_back_to_the_disable_button(self):
        """BUG（offline_accounts.py:259）：状态列不在 `_STATUS_WORDS` 里（比如「启用」）时，
        status 取到的是行尾「禁用」按钮 → 这人被当成已禁用，从快照/名册/离职检查里消失。"""
        text = (
            "1\twangzihan\twuji-wangzihan\t王梓涵\t17800001111\twang.zihan@wuji.tech"
            "\t启用\t2026-09-21 15:01:19\t重置密码\t禁用\n"
        )
        users, _ = off.parse_paste(text, login_prefix="wuji-")
        self.assertTrue(off.active(users[0]), users[0])

    def test_duplicates_keep_the_first_and_warn(self):
        row = PASTE.splitlines()[3]
        users, warnings = off.parse_paste(f"{row}\n{row}\n", login_prefix="wuji-")
        self.assertEqual(names(users), ["wuji-wangzihan"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("重复", warnings[0])

    def test_a_row_without_a_readable_email_is_skipped_with_a_warning(self):
        users, warnings = off.parse_paste("1\twuji-x\tx@\t正常\n", login_prefix="wuji-")
        self.assertEqual(users, [])
        self.assertTrue(warnings and "认不出邮箱" in warnings[0])

    def test_empty_or_none_input(self):
        self.assertEqual(off.parse_paste("", login_prefix="wuji-"), ([], []))
        self.assertEqual(off.parse_paste(None), ([], []))


class LoginPrefixTests(unittest.TestCase):
    """九章：主账号名 `wuji`，登录名 `wuji-<用户名>`。登录名是**完整的** `wuji-wangzihan`。"""

    def test_the_prefixed_column_is_the_login_and_kept_whole(self):
        users, _ = off.parse_paste(PASTE, login_prefix="wuji-")
        self.assertTrue(all(n.startswith("wuji-") for n in names(users)))
        self.assertNotIn("wangzihan", names(users), "拿了「用户名」列，不是登录名")

    def test_a_row_without_a_prefixed_cell_is_skipped_not_guessed(self):
        text = "1\twangzihan\t王梓涵\twang.zihan@wuji.tech\t正常\n"
        users, warnings = off.parse_paste(text, login_prefix="wuji-")
        self.assertEqual(users, [])
        self.assertIn("wuji-", warnings[0])

    def test_the_account_name_itself_is_not_a_prefix_to_strip(self):
        """account 是 `wuji`，登录名以 `wuji-` 开头：保存后原样是 `wuji-wangzihan`，
        不是被剥成 `wangzihan`；account 也原样是 `wuji`。"""
        users, _ = off.parse_paste(PASTE, login_prefix="wuji-")
        with tempfile.TemporaryDirectory() as box:
            path = Path(box) / off.FILENAME
            off.save_account(
                str(path),
                platform="jiuzhang",
                account="wuji",
                login_prefix="wuji-",
                users=users,
                source="t",
                as_of="2026-09-22",
                actor="admin:t",
            )
            rows = off.load(str(path))
        self.assertEqual(rows[0]["account"], "wuji")
        self.assertEqual(rows[0]["login_prefix"], "wuji-")
        self.assertIn("wuji-wangzihan", names(rows[0]["users"]))


class DiffTests(unittest.TestCase):
    def test_added_removed_changed(self):
        old = [user("wuji-a"), user("wuji-b"), user("wuji-c", display="C")]
        new = [user("wuji-a"), user("wuji-c", display="丙"), user("wuji-d")]
        self.assertEqual(
            off.diff(old, new),
            {"added": ["wuji-d"], "removed": ["wuji-b"], "changed": ["wuji-c"]},
        )

    def test_email_and_status_changes_count(self):
        old = [user("wuji-a"), user("wuji-b")]
        new = [user("wuji-a", email="new@wuji.tech"), user("wuji-b", status="禁用")]
        self.assertEqual(off.diff(old, new)["changed"], ["wuji-a", "wuji-b"])

    def test_created_alone_is_not_a_change_and_missing_equals_empty(self):
        a = user("wuji-a")
        b = dict(a, created="2026-01-01 00:00:00")
        b.pop("display_name")
        self.assertEqual(off.diff([a], [b]), {"added": [], "removed": [], "changed": []})

    def test_none_and_empty(self):
        self.assertEqual(off.diff(None, None), {"added": [], "removed": [], "changed": []})
        self.assertEqual(off.diff([], [user("wuji-a")])["added"], ["wuji-a"])


OTHER = {
    "platform": "jiuzhang",
    "account": "other",
    "source": "别的主账号",
    "as_of": "2026-09-01",
    "login_prefix": "other-",
    "users": [user("other-x", email="x@wuji.tech")],
    "history": [{"at": "2026-09-01 00:00", "by": "admin:old"}],
}


class SaveAccountTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / off.FILENAME

    def save(self, users, **kw):
        opts = dict(
            platform="jiuzhang",
            account="wuji",
            login_prefix="wuji-",
            users=users,
            source="控制台导出",
            as_of="2026-09-22",
            actor="admin:王梓涵",
        )
        opts.update(kw)
        return off.save_account(str(self.path), **opts)

    def stored(self, account="wuji"):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return next(a for a in data["accounts"] if a["account"] == account)

    def team(self, n):
        return [user(f"wuji-u{i:02d}") for i in range(n)]

    def test_first_save_creates_a_loadable_private_file(self):
        change = self.save(self.team(3))
        self.assertEqual(change["added"], ["wuji-u00", "wuji-u01", "wuji-u02"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(off.load(str(self.path))[0]["users"]), 3)  # load 会拒非 600

    def test_an_existing_world_readable_file_comes_back_as_600(self):
        self.path.write_text(json.dumps({"accounts": []}), encoding="utf-8")
        self.path.chmod(0o644)
        self.save(self.team(1))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_temp_files_left_behind(self):
        self.save(self.team(2))
        leftovers = [p.name for p in self.dir.iterdir() if p.name.startswith(".offline-")]
        self.assertEqual(leftovers, [])

    def test_phones_never_reach_the_file(self):
        users, _ = off.parse_paste(PASTE, login_prefix="wuji-")
        self.save(users)
        raw = self.path.read_text(encoding="utf-8")
        for phone in PHONES:
            self.assertNotIn(phone, raw)

    def test_empty_list_is_refused_and_nothing_written(self):
        with self.assertRaises(off.OfflineError):
            self.save([])
        self.assertFalse(self.path.exists())

    def test_it_replaces_the_whole_list(self):
        self.save(self.team(5))
        change = self.save(self.team(4) + [user("wuji-new")])
        self.assertEqual(change, {"added": ["wuji-new"], "removed": ["wuji-u04"], "changed": []})
        self.assertEqual(len(self.stored()["users"]), 5)

    def test_mass_removal_is_refused_without_confirmation(self):
        """粘漏半页 = 半个名单从名册里消失，离职检查也就再也看不到他们。"""
        self.save(self.team(10))
        before = self.path.read_bytes()
        with self.assertRaises(off.OfflineError) as caught:
            self.save(self.team(5))
        self.assertIn("移除 5 人", str(caught.exception))
        self.assertEqual(self.path.read_bytes(), before, "被拒的保存不能动文件")

    def test_mass_removal_goes_through_when_confirmed(self):
        self.save(self.team(10))
        change = self.save(self.team(5), allow_mass_remove=True)
        self.assertEqual(len(change["removed"]), 5)
        self.assertEqual(len(self.stored()["users"]), 5)

    def test_the_threshold_boundary(self):
        """10 人移除 3 人（正好 30%）放行；移除 4 人要确认。"""
        self.save(self.team(10))
        self.save(self.team(7))  # 3 > max(2, 3.0) 不成立 → 放行
        self.save(self.team(10))
        with self.assertRaises(off.OfflineError):
            self.save(self.team(6))

    def test_small_lists_may_lose_two_without_confirmation(self):
        self.save(self.team(3))
        self.save(self.team(1))  # 移除 2，不超 max(2, 0.9)
        self.save(self.team(3))
        with self.assertRaises(off.OfflineError):
            self.save([user("wuji-other")])  # 移除 3

    def test_saving_the_same_list_again_changes_nothing_but_history(self):
        users = self.team(4)
        self.save(users)
        first = self.stored()
        change = self.save(users)
        second = self.stored()
        self.assertEqual(change, {"added": [], "removed": [], "changed": []})
        self.assertEqual(second["users"], first["users"])
        self.assertEqual(
            {k: v for k, v in second.items() if k != "history"},
            {k: v for k, v in first.items() if k != "history"},
        )
        self.assertEqual(len(second["history"]), 2)
        self.assertEqual(second["history"][-1]["added"], 0)

    def test_history_records_who_and_counts_and_is_capped(self):
        for i in range(off.HISTORY_KEEP + 5):
            self.save(self.team(3), actor=f"admin:{i}")
        history = self.stored()["history"]
        self.assertEqual(len(history), off.HISTORY_KEEP)
        self.assertEqual(history[-1]["by"], f"admin:{off.HISTORY_KEEP + 4}")
        self.assertEqual(history[0]["by"], "admin:5", "留最近的，丢最早的")
        self.assertEqual(set(history[-1]), {"at", "by", "total", "added", "removed", "changed"})
        self.assertEqual(history[-1]["total"], 3)

    def test_other_accounts_in_the_file_are_untouched(self):
        self.path.write_text(json.dumps({"accounts": [OTHER]}, ensure_ascii=False), "utf-8")
        self.path.chmod(0o600)
        self.save(self.team(2))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([a["account"] for a in data["accounts"]], ["other", "wuji"])
        self.assertEqual(data["accounts"][0], OTHER)

    def test_a_save_that_would_break_the_table_is_refused_and_leaves_the_file(self):
        self.save(self.team(2))
        before = self.path.read_bytes()
        with self.assertRaises(off.OfflineError):
            self.save([user("wangzihan")])  # 缺 wuji- 前缀
        with self.assertRaises(off.OfflineError):
            self.save(self.team(2), platform="aliyun")  # 有采集接口的平台
        with self.assertRaises(off.OfflineError):
            self.save(self.team(2), as_of="")
        self.assertEqual(self.path.read_bytes(), before)


class _Live:
    """真 HTTP、真 handler（同 test_delivery_asset_owners 的夹具）。"""

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
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        people = {
            "schema": "wuji-people@1",
            "people": [
                {"union_id": "on_admin", "name": "管理员", "email": "admin@wuji.tech"},
                {"union_id": "on_1", "name": "李四", "email": "li.si@wuji.tech"},
            ],
        }
        (self.dir / "people.json").write_text(json.dumps(people, ensure_ascii=False), "utf-8")
        (self.dir / "admins.json").write_text(json.dumps({"union_ids": ["on_admin"]}), "utf-8")
        self.backend = self.make_backend()

    def make_backend(self, **kw):
        opts = dict(
            people_path=str(self.dir / "people.json"),
            bindings_path=str(self.dir / "bindings.json"),
            admins_path=str(self.dir / "admins.json"),
            platforms={"aliyun": "阿里云", "volcano": "火山引擎"},
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


OFFLINE_API = "/api/admin/offline-accounts"


class OfflineApiTests(_PanelBase):
    """登记表放在权限快照旁边（`beside(inventory_path)`）。"""

    def make_backend(self, **kw):
        kw.setdefault("inventory_path", str(self.dir / "inventory.json"))
        return super().make_backend(**kw)

    @property
    def table(self):
        return self.dir / off.FILENAME

    def body(self, action, text=PASTE, **extra):
        payload = {
            "action": action,
            "platform": "jiuzhang",
            "account": "wuji",
            "login_prefix": "wuji-",
            "text": text,
        }
        payload.update(extra)
        return payload

    def test_non_admin_is_refused_for_get_and_post(self):
        with _Live(self.backend) as live:
            member = self.login(live, "on_1", "li.si@wuji.tech")
            self.assertEqual(live.request("GET", OFFLINE_API, cookie=member)[0], 403)
            for action in ("preview", "save"):
                status, _ = live.request(
                    "POST", OFFLINE_API, cookie=member, payload=self.body(action)
                )
                self.assertEqual(status, 403, action)
            self.assertEqual(live.request("GET", OFFLINE_API)[0], 401)
            self.assertEqual(live.request("POST", OFFLINE_API, payload=self.body("save"))[0], 401)
        self.assertFalse(self.table.exists())

    def test_csrf_headers_are_required(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, _ = live.request(
                "POST",
                OFFLINE_API,
                cookie=admin,
                payload=self.body("save"),
                headers={"Content-Type": "application/json"},  # 缺 X-Panel-Request
            )
            self.assertEqual(status, 403)
        self.assertFalse(self.table.exists())

    def test_preview_shows_the_diff_and_writes_nothing(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, got = live.request(
                "POST", OFFLINE_API, cookie=admin, payload=self.body("preview")
            )
        self.assertEqual(status, 200, got)
        self.assertEqual(len(got["users"]), 3)
        self.assertEqual(got["before"], 0)
        self.assertEqual(len(got["diff"]["added"]), 3)
        for phone in PHONES:
            self.assertNotIn(phone, json.dumps(got["users"]))
        self.assertFalse(self.table.exists(), "预览不能写盘")
        self.assertEqual([p.name for p in self.dir.iterdir() if "offline" in p.name], [])

    def test_a_missing_action_is_a_preview(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            payload = self.body("preview")
            payload.pop("action")
            self.assertEqual(
                live.request("POST", OFFLINE_API, cookie=admin, payload=payload)[0], 200
            )
        self.assertFalse(self.table.exists())

    def test_save_writes_and_the_view_shows_it(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, got = live.request("POST", OFFLINE_API, cookie=admin, payload=self.body("save"))
            self.assertEqual(status, 200, got)
            self.assertTrue(got["saved"])
            self.assertEqual(got["total"], 3)
            self.assertEqual(self.table.stat().st_mode & 0o777, 0o600)
            raw = self.table.read_text(encoding="utf-8")
            for phone in PHONES:
                self.assertNotIn(phone, raw)
            row = off.load(str(self.table))[0]
            self.assertEqual((row["platform"], row["account"]), ("jiuzhang", "wuji"))
            self.assertEqual(json.loads(raw)["accounts"][0]["history"][0]["by"], "admin:某人")

            status, view = live.request("GET", OFFLINE_API, cookie=admin)
            self.assertEqual(status, 200)
            acc = view["accounts"][0]
            self.assertEqual(acc["account"], "wuji")
            self.assertEqual(acc["login_prefix"], "wuji-")
            self.assertEqual(len(acc["users"]), 3)
            self.assertFalse(acc["stale"])
            self.assertEqual(len(acc["history"]), 1)

            # 再预览同一份名单：没有差异
            _, again = live.request("POST", OFFLINE_API, cookie=admin, payload=self.body("preview"))
            self.assertEqual(again["before"], 3)
            self.assertEqual(again["diff"], {"added": [], "removed": [], "changed": []})

    def test_a_full_page_of_80_people_fits(self):
        """审计 H-1：默认 4KB 上限只够 30 人左右，整页粘贴会 400。"""
        rows = "".join(
            f"{i}\tu{i}\twuji-u{i}\t王某{i}\t{PHONES[0]}\tu{i}@wuji.tech\t正常"
            "\t2026-09-21 15:01:19\t重置密码\t禁用\n"
            for i in range(80)
        )
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, got = live.request(
                "POST", OFFLINE_API, cookie=admin, payload=self.body("preview", rows)
            )
            self.assertEqual(status, 200, got)
            self.assertEqual(len(got["users"]), 80)

    def test_changing_the_stored_prefix_is_refused(self):
        """审计 M-6：前缀清空后用户名会被当成登录名，整份名单被悄悄换一套名字。"""
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            self.assertEqual(
                live.request("POST", OFFLINE_API, cookie=admin, payload=self.body("save"))[0], 200
            )
            status, got = live.request(
                "POST", OFFLINE_API, cookie=admin, payload=self.body("preview", login_prefix="")
            )
            self.assertEqual(status, 400, got)
            self.assertIn("wuji-", got["error"])

    def test_mass_removal_via_the_api_needs_the_checkbox(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            big = "".join(f"{i}\twuji-u{i}\tu{i}@wuji.tech\t正常\n" for i in range(10))
            self.assertEqual(
                live.request("POST", OFFLINE_API, cookie=admin, payload=self.body("save", big))[0],
                200,
            )
            half = "".join(f"{i}\twuji-u{i}\tu{i}@wuji.tech\t正常\n" for i in range(5))
            status, got = live.request(
                "POST", OFFLINE_API, cookie=admin, payload=self.body("save", half)
            )
            self.assertEqual(status, 409, got)
            self.assertEqual(len(off.load(str(self.table))[0]["users"]), 10)
            status, _ = live.request(
                "POST",
                OFFLINE_API,
                cookie=admin,
                payload=self.body("save", half, allow_mass_remove=True),
            )
            self.assertEqual(status, 200)
            self.assertEqual(len(off.load(str(self.table))[0]["users"]), 5)

    def test_collected_platforms_and_bad_accounts_are_400(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            for extra in ({"platform": "aliyun"}, {"account": ""}, {"account": "a/b"}):
                status, _ = live.request(
                    "POST", OFFLINE_API, cookie=admin, payload=self.body("save", **extra)
                )
                self.assertEqual(status, 400, extra)
        self.assertFalse(self.table.exists())

    def test_nothing_parsed_is_not_saved(self):
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, _ = live.request(
                "POST", OFFLINE_API, cookie=admin, payload=self.body("save", "序号\t用户名\n")
            )
            self.assertEqual(status, 409)
        self.assertFalse(self.table.exists())

    def test_without_an_inventory_path_there_is_nowhere_to_save(self):
        backend = Backend(
            people_path=str(self.dir / "people.json"),
            admins_path=str(self.dir / "admins.json"),
        )
        with _Live(backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, view = live.request("GET", OFFLINE_API, cookie=admin)
            self.assertEqual((status, view["accounts"]), (200, []))
            status, _ = live.request("POST", OFFLINE_API, cookie=admin, payload=self.body("save"))
            self.assertEqual(status, 404)

    def test_a_broken_table_shows_an_error_instead_of_an_empty_page(self):
        self.table.write_text("{", encoding="utf-8")
        with _Live(self.backend) as live:
            admin = self.login(live, "on_admin", "admin@wuji.tech")
            status, view = live.request("GET", OFFLINE_API, cookie=admin)
        self.assertEqual(status, 200)
        self.assertEqual(view["accounts"], [])
        self.assertTrue(view.get("error"))

    def test_the_page_script_is_served(self):
        with _Live(self.backend) as live:
            conn_status, _ = live.request("GET", "/offline.js")
        self.assertEqual(conn_status, 200)


if __name__ == "__main__":
    unittest.main()
