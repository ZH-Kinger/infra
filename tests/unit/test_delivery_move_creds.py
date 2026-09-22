"""跨云搬运交给对方云的那把源端钥匙。

**这是替换一个 Blocker 的。** 原先交出去的是面板的开通身份 ——
那把能 `ram:CreateUser` + `ram:AttachPolicyToUser`，而跨云迁移会把它
**明文写进对方云的迁移任务配置里长期留存**，撤不回来、只能轮换。
泄漏面不是「这批数据」，是整个账号。

所以这里的断言盯的是三件事：交出去的**权限有多大**、名字**能不能找回来**、
搬完**撤没撤**。
"""

import unittest

from delivery import move_creds


class Cred:
    def __init__(self, ak="AK-new", sk="SK-new"):
        self.access_key_id = ak
        self.access_key_secret = sk


class FakeIssuer:
    def __init__(self, *, first=None):
        self.issued = []
        self.revoked = []
        self.first = first

    def issue_long_term(self, user, display, doc):
        self.issued.append((user, display, doc))
        if self.first is not None and len(self.issued) == 1:
            exc, self.first = self.first, None
            raise exc
        return Cred()

    def revoke_long_term(self, user):
        self.revoked.append(user)
        return []


PLAN_CROSS = {
    "src": {"scheme": "oss", "bucket": "src-b", "prefix": "batch/"},
    "dest": {"scheme": "tos", "bucket": "dst-b", "prefix": ""},
}


class NeededTests(unittest.TestCase):
    """同云不走这条：阿里那条源用 RAM 角色（根本没有 AK 要交），
    火山那条的钥匙从头到尾没离开火山。"""

    def test_cross_cloud_needs_one_and_says_which_side(self):
        self.assertEqual(move_creds.needed(PLAN_CROSS), "aliyun")
        self.assertEqual(
            move_creds.needed({"src": {"scheme": "tos"}, "dest": {"scheme": "oss"}}), "volcano"
        )

    def test_same_cloud_needs_nothing(self):
        for a, b in (("oss", "oss"), ("tos", "tos")):
            self.assertEqual(
                move_creds.needed({"src": {"scheme": a}, "dest": {"scheme": b}}), "", f"{a}->{b}"
            )


class MintTests(unittest.TestCase):
    def _mint(self, issuer, **kw):
        return move_creds.mint(
            issuer,
            ticket_id="REQ-20260922-ABCD",
            bucket="src-b",
            prefix="batch/",
            platform="aliyun",
            now=1_700_000_000.0,
            **kw,
        )

    def test_what_goes_out_can_only_read_and_only_this_prefix(self):
        """**交出去的东西能干什么，是这个模块存在的全部理由。**
        出现任何写/删动作，或者范围越出这次要搬的前缀，都是把那个 Blocker 又放回去。"""
        iss = FakeIssuer()
        self._mint(iss)
        doc = iss.issued[0][2]
        allows = [st for st in doc["Statement"] if st["Effect"] == "Allow"]
        actions = {a for st in allows for a in st["Action"]}
        for bad in ("oss:PutObject", "oss:DeleteObject", "oss:DeleteBucket", "ram:CreateUser"):
            self.assertNotIn(bad, actions, f"交出去的钥匙不该能 {bad}")
        self.assertTrue(actions, "一个 Allow 都没有的话搬运会失败，而那会被当成网络问题查半天")
        blob = str(doc)
        self.assertIn("batch/", blob, "范围要收到这次搬的前缀上")
        self.assertNotIn("dst-b", blob, "目的桶不该出现在源端这把钥匙里")

    def test_every_statement_carries_a_time_window(self):
        """窗是兜底：就算撤的那一步没跑成，这串东西也会自己失效。
        漏掉任何一条语句 = 那条永远有效。"""
        iss = FakeIssuer()
        self._mint(iss, days=3)
        for st in iss.issued[0][2]["Statement"]:
            if st["Effect"] != "Allow":
                continue
            cond = st.get("Condition") or {}
            self.assertIn("DateGreaterThan", cond, st["Action"])
            self.assertIn("DateLessThan", cond, st["Action"])

    def test_the_name_comes_from_the_ticket_so_leftovers_can_be_found(self):
        """随机名的话，进程在「建了号还没写回单子」之间挂掉，
        云上就留下一个谁也对不上的账号。"""
        one = move_creds.user_name("REQ-20260922-ABCD")
        self.assertEqual(one, move_creds.user_name("REQ-20260922-ABCD"))
        self.assertNotEqual(one, move_creds.user_name("REQ-20260922-EFGH"))
        self.assertTrue(one.startswith("tempak-"), "要能被既有的清理逻辑认出来")

    def test_no_ticket_id_is_refused_instead_of_making_up_a_name(self):
        with self.assertRaises(move_creds.MoveCredError):
            move_creds.user_name("")

    def test_a_leftover_from_last_time_is_cleaned_and_resigned(self):
        """不这样的话第二次提交永远失败，而失败的表现是「签不出凭证」，
        指不到根因是上一次留下的残留。"""
        iss = FakeIssuer(first=RuntimeError("EntityAlreadyExists.User"))
        ak, _sk, user = self._mint(iss)
        self.assertEqual(iss.revoked, [user])
        self.assertEqual(len(iss.issued), 2)
        self.assertEqual(ak, "AK-new")

    def test_a_not_exists_error_is_not_mistaken_for_already_exists(self):
        """`Exist` 这个子串同时命中 `EntityNotExist` —— 认错了会走进删除分支，
        而那是在删一个不该删的东西。"""
        iss = FakeIssuer(first=RuntimeError("EntityNotExist.User"))
        with self.assertRaises(move_creds.MoveCredError):
            self._mint(iss)
        self.assertEqual(iss.revoked, [], "不该去删")


class DropTests(unittest.TestCase):
    def test_it_really_revokes(self):
        iss = FakeIssuer()
        self.assertEqual(move_creds.drop(iss, "tempak-move-x"), [])
        self.assertEqual(iss.revoked, ["tempak-move-x"])

    def test_nothing_to_drop_is_not_a_call(self):
        """空名字还去调一次的话，会拿一个空串去云上删 —— 那个请求的后果说不准。"""
        iss = FakeIssuer()
        move_creds.drop(iss, "")
        self.assertEqual(iss.revoked, [])


class WindowTests(unittest.TestCase):
    def test_a_bad_number_falls_back_instead_of_blocking_the_move(self):
        for env in (
            {},
            {"DELIVERY_TRANSFER_CRED_DAYS": "abc"},
            {"DELIVERY_TRANSFER_CRED_DAYS": "0"},
            {"DELIVERY_TRANSFER_CRED_DAYS": "999"},
        ):
            self.assertEqual(move_creds.days_from_env(env), move_creds.DEFAULT_DAYS, str(env))

    def test_a_sane_number_is_honoured(self):
        self.assertEqual(move_creds.days_from_env({"DELIVERY_TRANSFER_CRED_DAYS": "3"}), 3)

    def test_the_default_outlives_a_real_migration(self):
        """19.5 TiB 那单实测跑了约 38 小时。窗短于任务时长的后果是搬到一半突然 403，
        而那时迁移服务会把整个任务判失败 —— 进度不可恢复。"""
        self.assertGreaterEqual(move_creds.DEFAULT_DAYS * 24, 38 * 3)


if __name__ == "__main__":
    unittest.main()
