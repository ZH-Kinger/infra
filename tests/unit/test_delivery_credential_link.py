"""凭证的交付方式：**密文在面板，密钥在链接里**。

审批评论里不再有 AK/SK，只有一条 `/c/<申请单号>#<密钥>`。这带来三条必须钉住的性质：

  · 拿到链接 = 拿到凭证 —— 所以链接只能有一条，密钥不能落盘，服务端自己也解不开；
  · 可以反复打开（换机器、重装环境、同事接手），但**每次打开都要记一笔**，
    否则「谁看过」这件事比原来（凭证躺在评论里）还糊涂，换这套就白换了；
  · 打不开的原因对外只有一句话 —— 逐一区分等于告诉试密钥的人「这个 id 是对的，接着试」。

外加几条「出事时怎么收场」：进程在签发途中挂掉、重试、到期回收、审批不作数。
数据全部虚构，云和飞书接口全部替换。
"""

from __future__ import annotations

import base64
import json
import string
import threading
import unittest

from delivery import sealed
from delivery import tickets as t
from delivery.errors import DeliveryError
from delivery.flows import FlowError

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import view_lines
from .test_delivery_credentials import BUCKET, SHENZHEN, TOS_BUCKET, Env

#: 取件地址（DELIVERY_BASE_URL）是凭证的唯一出口：没配 flows 在提交那一刻就拒。
#: 模块级设、跑完还原 —— 不用全局 autouse，否则「没配就该拒」那条路再也测不到。
setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

#: 打不开时对外的唯一说法。密钥错、密文被改、拿别人的单子号来试，都是这一句
OPAQUE = "这个链接打不开这份凭证"


class SealTests(unittest.TestCase):
    """密封本身。AEAD 自己搓必错，所以这里只锁「用错了会不会静默出垃圾」。"""

    def test_round_trip_and_key_is_never_part_of_what_gets_stored(self):
        box = sealed.seal('{"access_key_secret": "lt-secret"}')
        self.assertEqual(
            json.loads(sealed.unseal(box.key, box.nonce, box.ciphertext)),
            {"access_key_secret": "lt-secret"},
        )
        stored = box.stored()
        self.assertEqual(set(stored), {"nonce", "ciphertext"})
        self.assertNotIn(box.key, json.dumps(stored))

    def test_every_seal_uses_a_fresh_key_and_nonce(self):
        """两份凭证共用一把密钥的话，泄漏一条链接就等于泄漏另一条。"""
        first, second = sealed.seal("x"), sealed.seal("x")
        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        with self.assertRaises(sealed.SealError):
            sealed.unseal(second.key, first.nonce, first.ciphertext)

    def test_tampered_ciphertext_fails_instead_of_decrypting_to_garbage(self):
        box = sealed.seal("原文")
        flipped = ("B" if box.ciphertext[0] != "B" else "C") + box.ciphertext[1:]
        with self.assertRaises(sealed.SealError):
            sealed.unseal(box.key, box.nonce, flipped)

    def test_malformed_key_is_refused(self):
        box = sealed.seal("原文")
        for key in ("", "短", "!" * 43, box.key[:-1]):
            with self.assertRaises(sealed.SealError, msg=repr(key)):
                sealed.unseal(key, box.nonce, box.ciphertext)

    def test_only_one_spelling_of_a_key_works(self):
        """同一份凭证只能有一条链接。

        三种「写法不同、字节相同（或字母表外字符被静默丢掉）」的串都必须被拒：

          · 末位换成等价字符 —— 32 字节编成 43 个字符，末位有 2 个比特是多余的，
            不校验规范性的话同一把密钥有 4 种写法，「谁打开过」就再也对不上号；
          · 中间塞字母表外的字符 —— `b64decode` 默认**静默丢掉**它们，明显被改过的串照样能开；
          · 带 `=` 填充的写法 —— 同样是第二种写法。

        全都失败，才谈得上「链接 = 凭证」这个等式。
        """
        box = sealed.seal("原文")
        alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
        equivalent = box.key[:-1] + alphabet[alphabet.index(box.key[-1]) + 1]
        # 先证明它真的是「等价写法」而不只是一把错密钥：解出来的字节一模一样
        self.assertNotEqual(equivalent, box.key)
        self.assertEqual(
            base64.b64decode(equivalent + "=", altchars=b"-_"),
            base64.b64decode(box.key + "=", altchars=b"-_"),
        )
        spellings = {
            "末位等价字符": equivalent,
            "中间塞字母表外字符": box.key[:5] + "*!" + box.key[5:],
            "带 = 填充": box.key + "=",
        }
        for label, key in spellings.items():
            with self.assertRaises(sealed.SealError, msg=label):
                sealed.unseal(key, box.nonce, box.ciphertext)
        # 规范写法本身当然还要能开 —— 上面三条不能是「把所有密钥都拒了」
        self.assertEqual(sealed.unseal(box.key, box.nonce, box.ciphertext), "原文")

    def test_repr_does_not_print_the_key(self):
        """key 是这套设计里唯一不该落盘的东西，而 repr 会进异常回溯、日志、调试打印。"""
        box = sealed.seal("原文")
        for label, text in (
            ("repr", repr(box)),
            ("str", str(box)),
            ("format", f"{box}"),
            ("在容器里", repr({"box": box})),
            ("异常回溯", repr(RuntimeError(box))),
        ):
            self.assertNotIn(box.key, text, label)
            self.assertNotIn(box.key[:16], text, label)  # 前缀也不行：足够用来穷举剩下的
            self.assertIn("Sealed(", text, label)  # 还得看得出这是什么东西


class ViewLinkTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def issued(self, **payload):
        return self.env.run(payload={"bucket": BUCKET, "prefix": "batch/", "hours": 2, **payload})

    def test_comment_has_exactly_one_address_and_no_credential_in_it(self):
        done = self.issued()
        links = view_lines(self.env.feishu, done)
        self.assertEqual(len(links), 1, links)
        link = links[0]
        # 绝对地址、不是相对路径 `/c/...`：相对路径使用方点不开，而密钥只在这条评论里、
        # 服务端自己也解不开密文 —— 发出去就只能整单重来
        self.assertTrue(link.startswith(f"{base.VIEW_BASE}/c/{done['id']}#"), link)
        # 密钥在 # 之后：浏览器不会把 fragment 发给服务端，它不进访问日志、不进 Referer
        self.assertEqual(link.split("#", 1)[1], self.env.key(done))
        body = self.env.feishu.texts()[-1]
        for leak in ("sts-secret", "sts-token", "STS.AK1234", "AccessKeySecret"):
            self.assertNotIn(leak, body, f"审批评论里出现了 {leak}")

    def test_reopening_works_and_every_open_is_recorded(self):
        """可反复打开是这套设计的卖点；「每次都记一笔」是它的对价。"""
        done = self.issued()
        for who in ("10.0.0.1", "10.0.0.2"):
            self.assertEqual(self.env.opened(done, who=who)["access_key_secret"], "sts-secret")
        events = [
            e for e in self.env.store.get(done["id"])["events"] if e["event"] == "credential_viewed"
        ]
        self.assertEqual(len(events), 2, events)
        self.assertEqual([e["actor"] for e in events], ["view", "view"])
        self.assertIn("10.0.0.1", events[0]["note"])
        self.assertIn("10.0.0.2", events[1]["note"])
        # 状态不因为查看而变：查看不是一次性领取
        self.assertEqual(self.env.store.get(done["id"])["status"], t.DONE)

    def test_wrong_key_tampered_ciphertext_and_someone_elses_ticket_all_say_the_same(self):
        mine = self.issued()
        other = self.issued(prefix="team/")
        key = self.env.key(mine)
        cases = {
            "错密钥": (mine["id"], "A" * len(key)),
            "空密钥": (mine["id"], ""),
            "改一个字符": (mine["id"], ("A" if key[0] != "A" else "B") + key[1:]),
            "拿别人的密钥": (mine["id"], self.env.key(other)),
            "拿别人的单子": (other["id"], key),
        }
        for label, (ticket_id, bad) in cases.items():
            with self.assertRaises(FlowError, msg=label) as ctx:
                self.env.flows.view_credential(ticket_id, bad)
            self.assertEqual(str(ctx.exception), OPAQUE, label)
            self.assertEqual(ctx.exception.status, 403, label)
        # 试错也要留痕：没有任何一次失败被记成「查看凭证」
        for ticket in (mine, other):
            events = [e["event"] for e in self.env.store.get(ticket["id"])["events"]]
            self.assertNotIn("credential_viewed", events)

    def test_tampered_stored_ciphertext_does_not_decrypt(self):
        """能改 tickets.json 的人也不该能把凭证换成自己的 —— GCM 会校验。"""
        done = self.issued()
        key = self.env.key(done)
        box = json.loads(self.env.stored())["tickets"][0]["sealed"]
        flipped = ("B" if box["ciphertext"][0] != "B" else "C") + box["ciphertext"][1:]
        self.env.store.update(
            done["id"],
            actor="test",
            expect=[t.DONE],
            event="closed",
            fields={"sealed": {"nonce": box["nonce"], "ciphertext": flipped}},
        )
        with self.assertRaises(FlowError) as ctx:
            self.env.flows.view_credential(done["id"], key)
        self.assertEqual(str(ctx.exception), OPAQUE)

    def test_non_credential_tickets_have_nothing_to_open(self):
        resource = self.env.run("ecs-box", payload={"spec": "4 核 8G", "until": "2027-02-14"})
        # 不存在的单子号和「存在但不是凭证单」都要是 404：状态码分开就等于告诉
        # 试密钥的人「这个号是对的」（两者的文案目前仍不一样，已单独报给 dev）
        for ticket_id in (resource["id"], "REQ-20260101-DEADBEEF"):
            with self.assertRaises(DeliveryError, msg=ticket_id) as ctx:
                self.env.flows.view_credential(ticket_id, "A" * 43)
            self.assertEqual(getattr(ctx.exception, "status", 0), 404, ticket_id)

    def test_concurrent_opens_both_succeed_and_neither_record_is_lost(self):
        """两个人同时点开同一条链接：都该看到凭证，两条查看记录一条都不能丢。"""
        done = self.issued()
        key = self.env.key(done)
        start = threading.Barrier(4)
        results, errors = [], []

        def open_it():
            start.wait()
            try:
                results.append(self.env.flows.view_credential(done["id"], key, who="10.0.0.9")[1])
            except Exception as exc:  # noqa: BLE001 — 失败也要看得见
                errors.append(exc)

        threads = [threading.Thread(target=open_it) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertTrue(all(c["access_key_secret"] == "sts-secret" for c in results))
        events = [e["event"] for e in self.env.store.get(done["id"])["events"]]
        self.assertEqual(events.count("credential_viewed"), 4, events)


class LifecycleTests(unittest.TestCase):
    """签发之后：到期、关闭、进程挂掉、重试。链接什么时候该失效。"""

    LONG = {"bucket": BUCKET, "prefix": "batch/", "hours": 24}

    def test_expiry_is_counted_from_issuance_and_the_link_dies_with_the_credential(self):
        env = Env()
        done = env.run(payload=dict(self.LONG))
        self.assertEqual(float(done["expires_at_ts"]), env.now[0] + 24 * 3600)
        key = env.key(done)
        cred = env.opened(done, key)
        self.assertEqual((cred["not_before"], cred["expire"]), (env.now[0], env.now[0] + 24 * 3600))
        env.now[0] += 25 * 3600
        self.assertEqual(len(env.flows.revoke_expired()), 1)
        self.assertIn(("revoke", done["cred_user"]), env.issuer.actions)
        # 号已经删了，链接也该打不开 —— 留着只会让人以为凭证还能用
        with self.assertRaises(FlowError) as ctx:
            env.flows.view_credential(done["id"], key)
        self.assertEqual(ctx.exception.status, 409)

    def test_closing_a_ticket_kills_the_link(self):
        env = Env()
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        env.feishu.comment_fail = "comment refused"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        env.flows.close(failed["id"], actor="on_admin", note="送不出去，重新申请")
        with self.assertRaises(FlowError) as ctx:
            env.flows.view_credential(failed["id"], "A" * 43)
        self.assertEqual(ctx.exception.status, 409)

    def test_crash_mid_issue_is_recovered_and_the_retry_invalidates_the_old_link(self):
        """进程在签发途中被杀：单子卡在「开通中」，恢复成失败后由管理员重试。

        重试会重新签发、重新密封，**旧链接必须当场失效** —— 它对应的是另一把已经
        没人管的密钥，还能打开的话就是两份凭证在外面跑。
        """
        env = Env()

        def crash(*a, **kw):
            raise KeyboardInterrupt("进程在这里被杀")

        env.issuer.issue_long_term = crash
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        with self.assertRaises(KeyboardInterrupt):
            env.flows.sync(ticket["id"], force=True)
        self.assertEqual(env.store.get(ticket["id"])["status"], t.EXECUTING)
        # 半小时内不动它：可能只是慢，不是死了
        self.assertEqual(env.flows.recover_stuck(actor="system"), [])
        env.now[0] += 31 * 60
        self.assertEqual(len(env.flows.recover_stuck(actor="system")), 1)
        self.assertEqual(env.store.get(ticket["id"])["status"], t.FAILED)

        env.issuer = type(env.issuer)()
        done = env.flows.execute(ticket["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        links = view_lines(env.feishu, ticket)
        self.assertEqual(len(links), 1, links)  # 只有重试那次发出去的那条
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")

    def test_reissue_makes_the_previous_key_useless(self):
        env = Env()
        env.feishu.comment_fail = "comment refused"
        ticket = env.submit(payload=dict(self.LONG))
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        first = json.loads(env.stored())["tickets"][0]["sealed"]["ciphertext"]
        env.feishu.comment_fail = None
        done = env.flows.execute(ticket["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        second = json.loads(env.stored())["tickets"][0]["sealed"]
        self.assertNotEqual(second["ciphertext"], first)
        # 新链接开得了新凭证；旧密文已经被顶掉，再没有任何密钥能开出旧的那份
        self.assertEqual(env.opened(done)["access_key_secret"], "lt-secret")

    def test_retry_rechecks_the_approval_and_signs_nothing_once_it_is_revoked(self):
        """重试要重新核对飞书审批：审批在这中间被撤销了，就不能再签发一次。"""
        env = Env()
        env.feishu.comment_fail = "comment refused"
        ticket = env.submit(payload=dict(self.LONG))
        code = ticket["approval"]["instance_code"]
        env.feishu.instances[code]["status"] = "APPROVED"
        self.assertEqual(env.flows.sync(ticket["id"], force=True)["status"], t.FAILED)
        issued = [a for a in env.issuer.actions if a[0] == "issue"]
        env.feishu.comment_fail = None
        env.feishu.instances[code]["status"] = "CANCELED"
        with self.assertRaises(DeliveryError):
            env.flows.execute(ticket["id"], actor="on_admin")
        self.assertEqual([a for a in env.issuer.actions if a[0] == "issue"], issued)
        self.assertEqual(env.feishu.texts(), [])


class ApprovalGateTests(unittest.TestCase):
    """审批不作数的时候，云上必须一个字节都不写 —— 这条不因交付方式改变而放宽。"""

    def test_self_approved_ticket_never_reaches_the_cloud(self):
        env = Env()
        ticket = env.submit(payload={"bucket": BUCKET, "prefix": "batch/", "hours": 2})
        inst = env.feishu.instances[ticket["approval"]["instance_code"]]
        inst["status"] = "APPROVED"
        inst["task_list"] = [{"open_id": "ou_li", "user_id": "", "status": "APPROVED"}]
        closed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(closed["status"], t.CLOSED)
        self.assertEqual(env.executor.actions, [])
        self.assertEqual(env.issuer.actions, [])
        self.assertEqual(env.feishu.texts(), [], "没签发就不该发任何地址出去")
        self.assertNotIn("sealed", closed)

    def test_template_changed_during_approval_blocks_issuance(self):
        """审批期间桶被从模板里删掉：不签发、不发地址，单子落到失败等人处理。"""
        env = Env()
        ticket = env.submit(payload={"bucket": BUCKET, "prefix": "batch/", "hours": 2})
        env.templates["templates"][0]["buckets"] = [{"name": SHENZHEN, "region": "cn-shenzhen"}]
        env.feishu.instances[ticket["approval"]["instance_code"]]["status"] = "APPROVED"
        failed = env.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        self.assertEqual(env.executor.actions, [])
        self.assertEqual(env.issuer.actions, [])
        self.assertEqual(env.feishu.texts(), [])
        self.assertNotIn("sealed", failed)

    def test_volcano_credential_is_sealed_the_same_way(self):
        """两朵云共用一条交付路径：火山也是密文进单子、密钥进链接。"""
        env = Env()
        done = env.run("volc-data", payload={"bucket": TOS_BUCKET, "hours": 1, "subject": "外部方"})
        self.assertEqual(done["status"], t.DONE)
        cred = env.opened(done)
        self.assertEqual(cred["access_key_secret"], "lt-secret")
        self.assertTrue(cred["long_term"])
        self.assertNotIn("lt-secret", env.stored())
        self.assertNotIn(env.key(done), env.stored())


if __name__ == "__main__":
    unittest.main()
