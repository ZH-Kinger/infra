"""名下有云账号、在飞书通讯录里却找不到的人。

以前离职检查只按 union_id 查，名册里没 union_id 的人从来没被查过：体检不报、提醒不发。
王昱然离职几个月，阿里/九章/火山三个号一直挂着，没有任何通知。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from delivery import hygiene, inventory, notify
from delivery import offline_accounts as off
from delivery.people import AccountRef, Person

ROW = {
    "platform": "jiuzhang",
    "account": "wuji",
    "source": "测试",
    "as_of": "2026-09-22",
    "users": [
        {"name": "wuji-stay", "email": "stay@wuji.tech", "status": "正常"},
        {"name": "wuji-gone", "email": "gone@wuji.tech", "status": "正常"},
    ],
}


def _ref(name):
    return AccountRef("jiuzhang", "wuji", name)


STAY = Person(name="在职", email="Stay@wuji.tech", accounts=(_ref("wuji-stay"),))
GONE = Person(name="王昱然", email="gone@wuji.tech", accounts=(_ref("wuji-gone"),))
BY_UID = Person(name="有uid", email="", union_id="on_1", accounts=(_ref("x"),))
NO_ACC = Person(name="没号", email="noacc@wuji.tech")
#: 通讯录里在的人要够多（≥80%），否则按「通讯录没拉全」拒绝判断
PEERS = [
    Person(name=f"同事{i}", email=f"peer{i}@wuji.tech", accounts=(_ref(f"peer{i}"),))
    for i in range(8)
]
STAFF = {
    "stay@wuji.tech": {"union_id": "on_9"},
    "other@wuji.tech": {"union_id": "on_1"},
    **{f"peer{i}@wuji.tech": {} for i in range(8)},
}


class MissingTests(unittest.TestCase):
    def test_person_not_found_by_email_or_uid_is_reported(self):
        got = hygiene.missing_from_directory([STAY, GONE, BY_UID, NO_ACC, *PEERS], STAFF)
        self.assertEqual([p.name for p in got], ["王昱然"])

    def test_people_with_union_id_are_left_to_the_status_check(self):
        """审计 M-2：有 union_id 的人按在职状态查。拿邮箱判他们，挂在根部门下的在职的人
        会被天天报成离职。"""
        uid_gone = Person(
            name="有uid", email="uid@wuji.tech", union_id="on_x", accounts=(_ref("y"),)
        )
        staff = {f"p{i}@wuji.tech": {} for i in range(10)}
        people = [
            Person(name=f"p{i}", email=f"p{i}@wuji.tech", accounts=(_ref(f"u{i}"),))
            for i in range(10)
        ]
        self.assertEqual(hygiene.missing_from_directory(people + [uid_gone], staff), [])

    def test_email_match_ignores_case(self):
        self.assertEqual(hygiene.missing_from_directory([STAY], STAFF), [])

    def test_people_without_accounts_are_not_reported(self):
        self.assertEqual(hygiene.missing_from_directory([NO_ACC], STAFF), [])

    def test_empty_directory_refuses(self):
        with self.assertRaises(hygiene.DirectoryIncomplete):
            hygiene.missing_from_directory([STAY, GONE], {})

    def test_half_read_directory_refuses(self):
        """通讯录拉一半就判，等于把半个公司报成离职。"""
        people = [
            Person(name=f"p{i}", email=f"p{i}@wuji.tech", accounts=(_ref(f"u{i}"),))
            for i in range(10)
        ]
        staff = {"p0@wuji.tech": {}, "p1@wuji.tech": {}}
        with self.assertRaises(hygiene.DirectoryIncomplete):
            hygiene.missing_from_directory(people, staff)


class BuildTests(unittest.TestCase):
    def _snap(self):
        return inventory.parse(
            {
                "captured_at": "2026-09-22T00:00:00Z",
                "accounts": off.snapshot_accounts(off.parse({"accounts": [ROW]})),
            }
        )

    def test_missing_person_shows_up_as_unknown(self):
        report = hygiene.build(self._snap(), [STAY, GONE, *PEERS], staff=STAFF)
        self.assertEqual([f.subject for f in report.unknown], ["wuji-gone"])
        self.assertIn("公司邮箱", report.unknown[0].why)
        self.assertFalse(any("不判断谁离职" in x for x in report.skipped), report.skipped)

    def test_incomplete_directory_is_skipped_not_reported(self):
        report = hygiene.build(self._snap(), [STAY, GONE], staff={"x@wuji.tech": {}})
        self.assertEqual(report.unknown, [])
        self.assertTrue(any("对通讯录没做成" in x for x in report.skipped), report.skipped)

    def test_without_staff_nothing_changes(self):
        report = hygiene.build(self._snap(), [STAY, GONE])
        self.assertEqual(report.unknown, [])


class CardTests(unittest.TestCase):
    def test_card_lists_person_and_every_account(self):
        card = notify.not_found_card([GONE], base_url="https://cloud.example.com")
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("王昱然", text)
        self.assertIn("九章 wuji-gone", text)
        # 按钮改成去 IAM 页确认删除（离职停号那一节在那里）
        self.assertIn("#admin/iam", text)
        self.assertIn("确认删除", text)
        self.assertIn("1 人", card["header"]["title"]["content"])


class RemindTests(unittest.TestCase):
    """`identity iam-remind` 顺带提醒通讯录里找不到的人，同一批只提醒一次。"""

    def _run(self, tmp, staff):
        from delivery import cli, server  # noqa: F401 — 先导入，别在 patch 期间导入

        people = Path(tmp, "people.json")
        people.write_text("{}", encoding="utf-8")
        admins = Path(tmp, "admins.json")
        admins.write_text("{}", encoding="utf-8")
        args = SimpleNamespace(
            people=str(people),
            attributes=str(Path(tmp, "attrs.json")),
            admins=str(admins),
            every_hours=24.0,
        )
        sent = []
        notifier = mock.Mock()
        notifier.send.side_effect = lambda uid, card, id_type: sent.append(card)
        env = {"DELIVERY_FEISHU_APP_ID": "a", "DELIVERY_FEISHU_APP_SECRET": "b"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch("delivery.iam_sync.reconcile_report", return_value={"apps": []}),
            mock.patch("delivery.iam_sync.load_snooze", return_value=set()),
            mock.patch(
                "delivery.people.load", return_value=SimpleNamespace(people=[STAY, GONE, *PEERS])
            ),
            mock.patch("delivery.identity.directory.staff_index", return_value=staff),
            # 强信号那一路会按 union_id 查在职状态：不 mock 就真去打飞书
            mock.patch("delivery.identity.directory.status_of", return_value={}),
            mock.patch(
                "delivery.roles.load_admins", return_value=SimpleNamespace(union_ids={"on_admin"})
            ),
            mock.patch("delivery.notify.FeishuNotifier", return_value=notifier),
            mock.patch("delivery.server._tenant_token_cache", return_value=lambda: "t"),
        ):
            code = cli._cmd_identity_iam_remind(args)
        return code, sent

    def test_sends_once_per_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, sent = self._run(tmp, STAFF)
            self.assertEqual(code, 0)
            self.assertEqual(len(sent), 1)
            self.assertIn("王昱然", json.dumps(sent[0], ensure_ascii=False))
            # 他名下只有九章号：九章没有接口，不进离职停号记录（只能提醒人去控制台）
            from delivery import offboard

            self.assertEqual(offboard.load(offboard.path_beside(str(Path(tmp, "people.json")))), {})
            _code, again = self._run(tmp, STAFF)
            self.assertEqual(again, [])

    def test_directory_failure_is_loud_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, sent = self._run(tmp, {})
            self.assertEqual(sent, [])
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
