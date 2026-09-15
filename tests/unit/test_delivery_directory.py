"""通讯录：飞书通讯录接口 / IT 导出的 CSV → DirectoryEntry。

重点：接口报错不能当「没有人」；离职的人不进；同一人多部门只出现一次；
CSV 两种表头都认、缺 union_id 列必须报错。

离线：注入假 get(url, token)，不取 tenant token。数据虚构。
"""

from __future__ import annotations

import tempfile
import unittest
import urllib.parse
from pathlib import Path

from delivery.feishu import FeishuError
from delivery.identity.directory import from_csv, from_feishu
from delivery.people import DirectoryEntry

TOKEN = "t-test-token"  # noqa: S105


def user(uid, name, email="", employee_no="", resigned=False, personal=""):
    u = {
        "union_id": uid,
        "name": name,
        "employee_no": employee_no,
        "status": {"is_resigned": resigned},
    }
    if email:
        u["enterprise_email"] = email
    if personal:
        u["email"] = personal
    return u


class FeishuFake:
    """children: 部门列表页；users: {dept_id: [页1, 页2, ...]}，每页是 user 列表。"""

    def __init__(self, departments_pages, users_pages, errors=None):
        self.departments_pages = departments_pages
        self.users_pages = users_pages
        self.errors = errors or {}
        self.calls = []

    def _page(self, pages, page_token):
        idx = int(page_token or 0)
        items = pages[idx] if idx < len(pages) else []
        more = idx + 1 < len(pages)
        data = {"items": items, "has_more": more}
        if more:
            data["page_token"] = str(idx + 1)
        return {"code": 0, "data": data}

    def __call__(self, url, token):
        assert token == TOKEN
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((parts.path, q))
        assert q["page_size"] == "50"
        if parts.path.endswith("/contact/v3/departments/0/children"):
            if "children" in self.errors:
                return self.errors["children"]
            return self._page(self.departments_pages, q.get("page_token"))
        if parts.path.endswith("/contact/v3/users/find_by_department"):
            assert q["user_id_type"] == "union_id"
            dept = q["department_id"]
            if dept in self.errors:
                return self.errors[dept]
            return self._page(self.users_pages.get(dept, [[]]), q.get("page_token"))
        raise AssertionError(url)


def dept(did):
    return {"open_department_id": did, "name": did}


class FromFeishuTests(unittest.TestCase):
    def run_fake(self, fake, **kw):
        return from_feishu("", "", get=fake, token=TOKEN, **kw)

    def test_paginates_departments_and_users(self):
        fake = FeishuFake(
            departments_pages=[[dept("od-a")], [dept("od-b")]],
            users_pages={
                "0": [[user("on_1", "张三", "zhangsan@wuji.tech", "E001")]],
                "od-a": [
                    [user("on_2", "李四", "lisi@wuji.tech")],
                    [user("on_3", "王五", "wangwu@wuji.tech")],
                ],
                "od-b": [[user("on_4", "赵六", "zhaoliu@wuji.tech")]],
            },
        )
        got = self.run_fake(fake)
        self.assertEqual([e.union_id for e in got], ["on_1", "on_2", "on_3", "on_4"])
        self.assertEqual(got[0], DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech", "E001"))
        child_tokens = [q.get("page_token") for p, q in fake.calls if p.endswith("children")]
        self.assertEqual(child_tokens, [None, "1"])
        a_tokens = [q.get("page_token") for p, q in fake.calls if q.get("department_id") == "od-a"]
        self.assertEqual(a_tokens, [None, "1"])

    def test_root_department_always_scanned(self):
        fake = FeishuFake([[]], {"0": [[user("on_1", "张三", "zs@wuji.tech")]]})
        self.assertEqual([e.union_id for e in self.run_fake(fake)], ["on_1"])

    def test_resigned_skipped(self):
        fake = FeishuFake(
            [[]],
            {
                "0": [
                    [
                        user("on_1", "在职", "a@wuji.tech"),
                        user("on_2", "离职", "b@wuji.tech", resigned=True),
                    ]
                ]
            },
        )
        self.assertEqual([e.name for e in self.run_fake(fake)], ["在职"])

    def test_same_union_id_in_multiple_departments_once(self):
        zs = user("on_1", "张三", "zhangsan@wuji.tech")
        fake = FeishuFake(
            [[dept("od-a"), dept("od-b")]],
            {"0": [[zs]], "od-a": [[zs, user("on_2", "李四")]], "od-b": [[zs]]},
        )
        got = self.run_fake(fake)
        self.assertEqual(sorted(e.union_id for e in got), ["on_1", "on_2"])

    def test_resigned_in_one_dept_does_not_hide_active_record_elsewhere(self):
        fake = FeishuFake(
            [[dept("od-a")]],
            {"0": [[user("on_1", "张三", resigned=True)]], "od-a": [[user("on_1", "张三")]]},
        )
        self.assertEqual([e.union_id for e in self.run_fake(fake)], ["on_1"])

    def test_user_without_union_id_skipped(self):
        fake = FeishuFake([[]], {"0": [[user("", "无ID", "x@wuji.tech"), {"name": "缺字段"}]]})
        self.assertEqual(self.run_fake(fake), [])

    def test_departments_without_id_skipped(self):
        fake = FeishuFake([[{"name": "no-id"}, dept("od-a")]], {"od-a": [[user("on_2", "李四")]]})
        got = self.run_fake(fake)
        self.assertEqual([e.union_id for e in got], ["on_2"])
        depts = [q["department_id"] for p, q in fake.calls if "department_id" in q]
        self.assertEqual(depts, ["0", "od-a"])

    def test_personal_email_fallback_when_no_enterprise_email(self):
        # 现状：enterprise_email 为空时回落个人 email。build 靠 domain 过滤兜底，
        # 这里锁住现状，变更时要同步考虑 people.build 的域校验。
        fake = FeishuFake([[]], {"0": [[user("on_1", "张三", personal="zs@example.com")]]})
        self.assertEqual(self.run_fake(fake)[0].enterprise_email, "zs@example.com")

    def test_code_nonzero_on_departments_raises_with_scope_hint(self):
        fake = FeishuFake(
            [[]], {}, errors={"children": {"code": 40004, "msg": "no dept authority"}}
        )
        with self.assertRaises(FeishuError) as ctx:
            self.run_fake(fake)
        msg = str(ctx.exception)
        self.assertIn("40004", msg)
        self.assertIn("通讯录权限范围", msg)

    def test_code_nonzero_on_users_raises_not_partial(self):
        fake = FeishuFake(
            [[dept("od-a")]],
            {"0": [[user("on_1", "张三")]]},
            errors={"od-a": {"code": 99991672, "msg": "Access denied"}},
        )
        with self.assertRaises(FeishuError) as ctx:
            self.run_fake(fake)
        self.assertIn("通讯录权限范围", str(ctx.exception))

    def test_missing_code_is_error(self):
        fake = FeishuFake([[]], {}, errors={"children": {"data": {"items": []}}})
        with self.assertRaises(FeishuError):
            self.run_fake(fake)

    def test_has_more_without_page_token_is_incomplete_not_silent(self):
        """has_more 却不给翻页令牌：静默返回已取到的部分，名册就会少人。"""

        def get(url, token):
            return {"code": 0, "data": {"items": [], "has_more": True}}

        with self.assertRaises(FeishuError):
            from_feishu("", "", get=get, token=TOKEN)

    def test_endless_pagination_aborts(self):
        def get(url, token):
            return {"code": 0, "data": {"items": [], "has_more": True, "page_token": "same"}}

        with self.assertRaises(FeishuError):
            from_feishu("", "", get=get, token=TOKEN)

    def test_progress_reported(self):
        fake = FeishuFake([[dept("od-a")]], {})
        msgs = []
        self.run_fake(fake, progress=msgs.append)
        self.assertEqual(len(msgs), 2)

    def test_no_token_and_no_app_credentials_raises(self):
        with self.assertRaises(FeishuError):
            from_feishu("", "", get=lambda u, t: {"code": 0})


class FromCsvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text, encoding="utf-8"):
        path = self.dir / "directory.csv"
        path.write_bytes(text.encode(encoding))
        return str(path)

    def test_chinese_headers(self):
        path = self.write("union_id,姓名,邮箱,工号\non_1,张三,zhangsan@wuji.tech,E001\n")
        self.assertEqual(
            from_csv(path), [DirectoryEntry("on_1", "张三", "zhangsan@wuji.tech", "E001")]
        )

    def test_english_headers(self):
        path = self.write(
            "feishu_union_id,name,email,employee_no\n"
            "on_1,张三,zhangsan@wuji.tech,E001\n"
            "on_2,李四,lisi@wuji.tech,E002\n"
        )
        got = from_csv(path)
        self.assertEqual([e.union_id for e in got], ["on_1", "on_2"])
        self.assertEqual(got[1].employee_no, "E002")

    def test_enterprise_email_alias_and_header_case_whitespace(self):
        path = self.write(" Feishu_Union_ID ,企业邮箱\non_1, zs@wuji.tech \n")
        got = from_csv(path)
        self.assertEqual(got, [DirectoryEntry("on_1", "", "zs@wuji.tech", "")])

    def test_bom(self):
        path = self.write("\ufeffunion_id,姓名,邮箱,工号\non_1,张三,zs@wuji.tech,E001\n")
        self.assertEqual(from_csv(path)[0].union_id, "on_1")

    def test_crlf_and_quoted_fields(self):
        path = self.write('union_id,姓名,邮箱\r\non_1,"张,三",zs@wuji.tech\r\n')
        self.assertEqual(from_csv(path)[0].name, "张,三")

    def test_missing_union_id_column_raises(self):
        path = self.write("姓名,邮箱,工号\n张三,zs@wuji.tech,E001\n")
        with self.assertRaises(FeishuError) as ctx:
            from_csv(path)
        self.assertIn("union_id", str(ctx.exception))

    def test_blank_union_id_rows_skipped(self):
        path = self.write("union_id,姓名\n,张三\n   ,李四\non_3,王五\n")
        self.assertEqual([e.name for e in from_csv(path)], ["王五"])

    def test_optional_columns_absent(self):
        path = self.write("union_id\non_1\n")
        self.assertEqual(from_csv(path), [DirectoryEntry("on_1", "")])

    def test_short_row_missing_trailing_fields(self):
        path = self.write("union_id,姓名,邮箱\non_1\n")
        self.assertEqual(from_csv(path), [DirectoryEntry("on_1", "")])

    def test_empty_file_is_rejected_as_missing_header(self):
        """空文件没有 union_id 列，和表头写错的模板一样要报出来。"""
        with self.assertRaises(FeishuError):
            from_csv(self.write(""))

    def test_missing_file_raises_feishu_error(self):
        with self.assertRaises(FeishuError):
            from_csv(str(self.dir / "nope.csv"))

    def test_header_only_without_union_id_column_raises(self):
        """只有表头、没有数据行也要检查 union_id 列——表头写错的空模板不能被当成「通讯录没人」。"""
        with self.assertRaises(FeishuError):
            from_csv(self.write("姓名,邮箱\n"))

    def test_first_row_with_extra_columns_does_not_crash(self):
        """已知 bug（directory.py:395）：首行字段比表头多时 DictReader 把多余值放在
        key=None 下，`h.strip()` 抛 AttributeError（不是 FeishuError），CLI 拿到的是 traceback。"""
        path = self.write("union_id,姓名\non_1,张三,多余列\n")
        self.assertEqual(from_csv(path)[0].union_id, "on_1")


if __name__ == "__main__":
    unittest.main()
