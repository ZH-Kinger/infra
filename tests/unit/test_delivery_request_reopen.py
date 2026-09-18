"""重新打开已关闭的申请单。

这个功能存在的理由只有一条：**那张飞书批条一直有效**。`Flows.execute` 接受 APPROVED 和
FAILED 两个状态，且每次开通都先 `_verify` → `_verify_approval` 回拉核对实例。所以
「审批通过了、开通那步失败、管理员关掉了」的单子，排掉问题后重试**不需要重新审批** ——
缺的只是 CLOSED 回不去。

于是这里要钉住的是一对相反的性质：

  · 重开确实把出路接回来了（同一张审批实例，不重走一轮）
  · 重开**一点门禁都没放宽** —— 当初因为审批不合规被关的单子，重开后照样在
    `_verify_approval` 那里被拦下来。这条要是松了，「关闭 → 重开」就是一条绕过审批的后门

数据全部虚构，云和飞书接口全部替换；脚手架复用 test_delivery_access_requests。
"""

from __future__ import annotations

import json
import unittest

from delivery import tickets as t
from delivery.approval import ApprovalError, SelfApprovalError
from delivery.flows import FlowError
from delivery.requests_api import Caller, RequestsApi, ticket_view

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import ACC, BUCKET, CRED, TEMPLATES, Harness, MemberExecutor

#: 凭证的唯一出口是 DELIVERY_BASE_URL 拼出来的取件地址，没配 flows 在提交那一刻就拒
setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

#: 资源开通模板：面板一行云都不写，审批通过后停在「待开通」等管理员登记。
#: 要它是因为 FULFILLING 是 `closed_from` 的第二个合法回溯目标，只有资源单走得到
RESOURCE = json.loads(json.dumps(TEMPLATES))
RESOURCE["templates"].append(
    {
        "id": "ecs-box",
        "kind": "resource",
        "platform": "aliyun",
        "account": ACC,
        "title": "ECS 开发机",
        "max_days": 90,
        "spec_hint": "写清楚要几核几 G",
    }
)
#: 长期凭证模板：>12 小时走「建子账号 + 长期 AK」，这条路才会写 `cred_user`
LONG_CRED = json.loads(json.dumps(TEMPLATES))
LONG_CRED["templates"][1]["max_hours"] = 24
LONG = {"bucket": BUCKET, "hours": 24}

ADMIN = Caller("on_admin", "管理员", "admin@wuji.tech", "ou_admin", "u_admin", True)
LI = Caller("on_li", "李四", "li.si@wuji.tech", "ou_li", "", False)

#: 资源单的到期日。基准时钟是 1_800_000_000（2027-01-15），这个日子在模板上限内
UNTIL = "2027-02-14"
SPEC = {"spec": "4 核 8G", "until": UNTIL}


class ReopenBase(unittest.TestCase):
    def harness(self, templates=TEMPLATES):
        h = Harness(templates)
        h.executor = MemberExecutor()
        return h

    def failed(self, h, group="grp-oss-read", days=7):
        """造一张「审批通过、开通那步失败」的权限单 —— 重开功能的正主。"""
        h.executor.fail_on = ("add", group)
        ticket = h.submit(payload={"cloud_user": "lisi", "days": days})
        h.approve(ticket)
        out = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(out["status"], t.FAILED)
        return out

    def closed(self, h, **kw):
        failed = self.failed(h, **kw)
        out = h.flows.close(failed["id"], actor="on_admin", note="配置有问题，先放着")
        self.assertEqual(out["status"], t.CLOSED)
        return out

    def fulfilling(self, h):
        ticket = h.submit(template="ecs-box", payload=dict(SPEC))
        h.approve(ticket)
        out = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(out["status"], t.FULFILLING)
        return out

    def issued_credential(self, h, payload):
        """一张「凭证签出来了、评论没送出去」的已关闭凭证单：签发过的东西都还留在单子上。"""
        ticket = h.submit(template="dev-sts", payload=payload)
        h.approve(ticket)
        h.feishu.comment_fail = "comment refused"
        failed = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(failed["status"], t.FAILED)
        return h.flows.close(ticket["id"], actor="on_admin", note="不发了")

    def rewrite(self, h, ticket_id, **fields):
        """直接改存储里的那张单子。

        只用来造「这个字段加上之前就存在的老单子」和「字段被人工改坏」两种形状 ——
        它们没有任何代码路径造得出来，而重开的回退分支正是为它们写的。
        """
        path = h.dir / "tickets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["tickets"]:
            if row.get("id") == ticket_id:
                for key, value in fields.items():
                    if value is None:
                        row.pop(key, None)
                    else:
                        row[key] = value
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class ReopenHappyPathTests(ReopenBase):
    def test_reopened_ticket_is_provisioned_without_a_second_approval(self):
        """主路径：失败 → 关闭 → 重开 → 重试开通成功，**全程只有一张审批实例**。

        「不用重新审批」是这个功能的全部价值。要是重开会重新发起审批，那还不如让人
        重新提交一张单子 —— 重开就没有存在意义了。
        """
        h = self.harness(base.TWO_GROUPS)
        closed = self.closed(h, group="grp-b")
        code = closed["approval"]["instance_code"]
        self.assertEqual(closed["closed_from"], t.FAILED)

        h.executor.fail_on = None
        reopened = h.flows.reopen(closed["id"], actor="on_admin")
        self.assertEqual(reopened["status"], t.FAILED)
        self.assertEqual(reopened["events"][-1]["event"], "reopened")

        done = h.flows.execute(closed["id"], actor="on_admin")
        self.assertEqual(done["status"], t.DONE)
        self.assertEqual(h.executor.members, {("lisi", "grp-a"), ("lisi", "grp-b")})

        self.assertEqual(list(h.feishu.instances), [code], "重开又发起了一张审批")
        created = [
            c for c in h.feishu.calls if c[0] == "POST" and c[1].endswith("/approval/v4/instances")
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(done["approval"]["instance_code"], code)

    def test_reopen_reattaches_the_retry_edge_and_the_close_edge(self):
        """重开之后「重试开通」按钮要回来，「关闭申请」也还在 —— 不然重开是条死路。"""
        h = self.harness()
        closed = self.closed(h)
        self.assertFalse(ticket_view(closed, viewer=ADMIN)["actions"]["retry"])
        reopened = h.flows.reopen(closed["id"], actor="on_admin")
        actions = ticket_view(reopened, viewer=ADMIN)["actions"]
        self.assertTrue(actions["retry"])
        self.assertTrue(actions["close"])
        self.assertFalse(actions["reopen"], "已经打开了，按钮不该还亮着")
        again = h.flows.close(closed["id"], actor="on_admin", note="还是不要了")
        self.assertEqual(again["status"], t.CLOSED)
        self.assertEqual(again["closed_from"], t.FAILED)


class ReopenDoesNotRelaxTheGateTests(ReopenBase):
    """**这一组是这个功能的安全边界**：重开只改状态，不碰审批核对。"""

    def test_a_self_approved_ticket_is_still_refused_after_being_reopened(self):
        """只有申请人自己点的同意 → 单子被自动关闭；重开之后再开通，必须还是被拦。

        这条路最危险：`_close_self_approved` 是**系统**关的，管理员在面板上只看到
        一张「已关闭」的单子，重开按钮和别的失败单长得一模一样。要是重开顺带把这单
        放行了，那「审批必须有申请人以外的人同意」这条规则就被一次点击绕过去了。
        """
        h = self.harness()
        ticket = h.submit()
        inst = h.feishu.instances[ticket["approval"]["instance_code"]]
        inst.update(status="APPROVED", task_list=[{"open_id": "ou_li", "status": "APPROVED"}])
        closed = h.flows.sync(ticket["id"], force=True)
        self.assertEqual(closed["status"], t.CLOSED)
        self.assertEqual(closed["events"][-1]["event"], "approval_invalid")

        reopened = h.flows.reopen(ticket["id"], actor="on_admin")
        self.assertEqual(reopened["status"], t.FAILED)
        with self.assertRaises(SelfApprovalError):
            h.flows.execute(ticket["id"], actor="on_admin")
        self.assertEqual(h.executor.actions, [], "一个云调用都不该发生")
        self.assertEqual(h.executor.members, set())
        after = h.store.get(ticket["id"])
        self.assertEqual(after["status"], t.FAILED, "拦下来了，但状态别被搞坏")
        self.assertNotIn("execute_done", [e["event"] for e in after["events"]])

    def test_an_approval_revoked_after_the_fact_still_blocks_a_reopened_ticket(self):
        """审批通过后在飞书里被撤销：重开不看历史，只看**现在**那张实例是什么状态。"""
        h = self.harness()
        closed = self.closed(h)
        h.feishu.instances[closed["approval"]["instance_code"]]["status"] = "CANCELED"
        h.executor.fail_on = None
        h.flows.reopen(closed["id"], actor="on_admin")
        with self.assertRaises(ApprovalError):
            h.flows.execute(closed["id"], actor="on_admin")
        self.assertEqual(h.executor.members, set())
        self.assertEqual(h.store.get(closed["id"])["status"], t.FAILED)

    def test_a_template_changed_during_the_close_is_still_caught(self):
        """关着的这段时间里模板被改了 —— 重开不该把「按旧模板批的、按新模板开」放过去。"""
        h = self.harness()
        closed = self.closed(h)
        h.templates["templates"][0]["groups"] = ["grp-admin-everything"]
        h.executor.fail_on = None
        h.flows.reopen(closed["id"], actor="on_admin")
        with self.assertRaises(FlowError) as ctx:
            h.flows.execute(closed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(h.executor.members, set(), "绝不能按改过的模板去加组")
        self.assertEqual(h.store.get(closed["id"])["status"], t.FAILED)


class ClosedFromTests(ReopenBase):
    def test_a_ticket_closed_while_awaiting_fulfilment_goes_back_there(self):
        """从「待开通」关的资源单，重开要回「待开通」，不是回「开通失败」。

        回错了不是小事：FAILED 上亮的是「重试开通」，而资源单重试一遍**什么也不会发生**
        （面板本来就不建资源），管理员会以为点了没反应；真正该亮的是「登记开通结果」。
        """
        h = self.harness(RESOURCE)
        ticket = self.fulfilling(h)
        closed = h.flows.close(ticket["id"], actor="on_admin", note="项目延期")
        self.assertEqual(closed["closed_from"], t.FULFILLING)

        reopened = h.flows.reopen(ticket["id"], actor="on_admin")
        self.assertEqual(reopened["status"], t.FULFILLING)
        self.assertTrue(ticket_view(reopened, viewer=ADMIN)["actions"]["fulfil"])
        self.assertEqual(h.executor.actions, [], "资源单一行云都不写")

        done = h.flows.fulfil(ticket["id"], actor="on_admin", note="i-abc 4C8G 杭州")
        self.assertEqual(done["status"], t.DONE)

    def test_a_missing_or_tampered_closed_from_falls_back_to_failed(self):
        """`closed_from` 缺失或是个不该出现的值，一律回 FAILED，绝不当成状态机的旁路。

        缺失的是这个字段加上之前关的**老单子**（线上已经有）。被改坏的那几种造不出来，
        但回退分支要是写成「照着字段里的值转」，一个手改过的 tickets.json 就能把单子
        直接推到 DONE / REVOKED —— 那才是真正要挡的东西。FAILED 也是两个合法值里更
        保守的一个：它只是让管理员能点重试，不会把单子塞回别人的待办队列。
        """
        for bad in (None, "", "done", "revoked", "approved", "closed", ["failed"], 42):
            with self.subTest(closed_from=bad):
                h = self.harness(RESOURCE)
                ticket = self.fulfilling(h)
                h.flows.close(ticket["id"], actor="on_admin", note="先关掉")
                self.rewrite(h, ticket["id"], closed_from=bad)
                reopened = h.flows.reopen(ticket["id"], actor="on_admin")
                self.assertEqual(reopened["status"], t.FAILED)


class ReopenRefusalTests(ReopenBase):
    def test_only_closed_tickets_can_be_reopened(self):
        """不是「已关闭」就 409。重开是状态机上的一条边，不是「强行改状态」的工具。"""
        h = self.harness(RESOURCE)
        pending = h.submit()
        done = h.submit(template="ecs-box", payload=dict(SPEC))
        h.approve(done)
        h.flows.sync(done["id"], force=True)
        h.flows.fulfil(done["id"], actor="on_admin", note="i-abc")
        failed = self.failed(h, days=9)

        for ticket, status in (
            (pending, t.PENDING),
            (done, t.DONE),
            (failed, t.FAILED),
        ):
            with self.subTest(status=status):
                self.assertEqual(h.store.get(ticket["id"])["status"], status)
                with self.assertRaises(FlowError) as ctx:
                    h.flows.reopen(ticket["id"], actor="on_admin")
                self.assertEqual(ctx.exception.status, 409)
                self.assertEqual(h.store.get(ticket["id"])["status"], status)

    def test_a_short_term_credential_with_ciphertext_left_cannot_be_reopened(self):
        """STS 凭证：云上没有子账号，但**密文还在** —— 那团密文就是这单唯一还能被打开的东西。

        重开会让它回到 FAILED、看起来还能「重试开通」，而重试只会再签一份新凭证，
        旧那份的取件地址仍然有效。该走的是「作废凭证」：掐掉密文、删掉云上的东西。
        """
        h = self.harness()
        closed = self.issued_credential(h, dict(CRED))
        self.assertFalse(closed.get("cred_user"), "STS 不建子账号")
        self.assertTrue(closed["sealed"].get("ciphertext"))
        with self.assertRaises(FlowError) as ctx:
            h.flows.reopen(closed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("作废", str(ctx.exception))
        self.assertEqual(h.store.get(closed["id"])["status"], t.CLOSED)

    def test_a_long_term_credential_with_a_sub_account_cannot_be_reopened(self):
        """长期凭证：云上真建了子账号。`cred_user` 单独就足以拒绝 —— 密文被掐掉之后
        （管理员点过「作废凭证」、或定时任务清过一轮）那个号可能还在，重试会往**同一个**
        子账号上再发一把 AK。所以两个判据是「或」，各自都要单独成立。
        """
        h = self.harness(LONG_CRED)
        closed = self.issued_credential(h, dict(LONG))
        self.assertTrue(closed.get("cred_user"))
        self.rewrite(h, closed["id"], sealed={})  # 只留 cred_user，验另一半判据
        cleaned = h.store.get(closed["id"])
        self.assertFalse((cleaned.get("sealed") or {}).get("ciphertext"))
        with self.assertRaises(FlowError) as ctx:
            h.flows.reopen(closed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(h.store.get(closed["id"])["status"], t.CLOSED)

    def test_reopening_twice_is_refused_instead_of_corrupting_the_state(self):
        """连点两次「重新打开」：第二次是 409，不是把单子再推一格。

        面板上按钮点两下、两个管理员同时点，都是这条路。第二次要是也「成功」了，
        台账里会多一条没发生过的重开事件，而单子早就不在 CLOSED 了。
        """
        h = self.harness()
        closed = self.closed(h)
        first = h.flows.reopen(closed["id"], actor="on_admin")
        self.assertEqual(first["status"], t.FAILED)
        with self.assertRaises(FlowError) as ctx:
            h.flows.reopen(closed["id"], actor="on_admin")
        self.assertEqual(ctx.exception.status, 409)
        after = h.store.get(closed["id"])
        self.assertEqual(after["status"], t.FAILED)
        events = [e["event"] for e in after["events"]]
        self.assertEqual(events.count("reopened"), 1, "空转还往台账里记了一笔")
        self.assertEqual(events.count("closed"), 1)


class ClosedExitsStayNarrowTests(ReopenBase):
    """CLOSED 多了两条出边，别顺手把出口开太大。"""

    def test_closed_only_leads_to_revoked_failed_or_fulfilling(self):
        self.assertEqual(t.TRANSITIONS[t.CLOSED], {t.REVOKED, t.FAILED, t.FULFILLING})

    def test_closed_cannot_jump_to_a_state_it_never_passed_through(self):
        """DONE / EXECUTING / APPROVED 这些是「开通」这条链上的状态，只能靠 execute 走到。

        CLOSED 直连 DONE 的话，一次 `store.update` 就能把一张没开通过的单子写成「已完成」，
        台账、到期回收、归属表全部跟着说谎。
        """
        h = self.harness()
        closed = self.closed(h)
        for to in (t.DONE, t.EXECUTING, t.APPROVED, t.PENDING, t.SUBMITTING, t.REJECTED):
            with self.subTest(to=to):
                with self.assertRaises(t.TicketError) as ctx:
                    h.store.update(
                        closed["id"], actor="on_admin", expect=[t.CLOSED], to=to, event="x"
                    )
                self.assertEqual(ctx.exception.status, 409)
                self.assertEqual(h.store.get(closed["id"])["status"], t.CLOSED)

    def test_closed_can_still_be_revoked(self):
        """作废那条老路没被这次改动碰坏：凭证签出来没送达的单子还要靠它去云上删号。"""
        h = self.harness()
        closed = self.closed(h)
        out = h.store.update(
            closed["id"], actor="system", expect=[t.CLOSED], to=t.REVOKED, event="revoked"
        )
        self.assertEqual(out["status"], t.REVOKED)


class ReopenApiTests(ReopenBase):
    def api(self, h):
        return RequestsApi(lambda: h.flows)

    def test_the_button_is_admin_only_and_hidden_once_credentials_were_issued(self):
        h = self.harness()
        closed = self.closed(h)
        self.assertTrue(ticket_view(closed, viewer=ADMIN)["actions"]["reopen"])
        self.assertFalse(
            ticket_view(closed, viewer=LI)["actions"]["reopen"],
            "申请人不该看到管理操作 —— 看得到就会去点，点了是 404",
        )
        cred = self.issued_credential(h, dict(CRED))
        self.assertFalse(
            ticket_view(cred, viewer=ADMIN)["actions"]["reopen"],
            "按钮的判据要和 flows.reopen 里的一致，否则点了必然 409",
        )

    def test_the_employee_route_has_no_reopen_at_all(self):
        """`/api/requests/<id>/reopen` 不能成功 —— 连「自己的单子」也不行。

        重开是管理操作：它把一张单子放回开通队列。申请人能自助重开的话，等于他能
        反复触发开通重试，而那条链上唯一的门就是审批核对。
        """
        h = self.harness()
        closed = self.closed(h)
        api = self.api(h)
        path = f"/api/requests/{closed['id']}/reopen"
        for caller, why in ((LI, "申请人本人"), (ADMIN, "管理员走员工路径")):
            with self.subTest(why):
                status, _ = api.handle("POST", path, {}, {}, caller)
                self.assertEqual(status, 404)
                self.assertEqual(h.store.get(closed["id"])["status"], t.CLOSED)

    def test_the_admin_route_needs_admin_and_then_works(self):
        h = self.harness()
        closed = self.closed(h)
        api = self.api(h)
        path = f"/api/admin/requests/{closed['id']}/reopen"

        status, body = api.handle("POST", path, {}, {}, LI)
        self.assertEqual(status, 403)
        self.assertEqual(h.store.get(closed["id"])["status"], t.CLOSED)

        status, body = api.handle("POST", path, {}, {"note": "配置修好了"}, ADMIN)
        self.assertEqual(status, 200)
        self.assertEqual(body["request"]["status"], t.FAILED)
        self.assertTrue(body["request"]["actions"]["retry"])
        self.assertEqual(body["request"]["events"][-1]["note"], "配置修好了")

        # 再点一次：接口层如实回 409，不静默成功
        status, body = api.handle("POST", path, {}, {}, ADMIN)
        self.assertEqual(status, 409)
        self.assertIn("error", body)

    def test_the_admin_route_refuses_a_ticket_whose_credentials_went_out(self):
        """按钮藏起来了不等于接口关了 —— 直接 POST 过来的一样要 409。"""
        h = self.harness()
        cred = self.issued_credential(h, dict(CRED))
        api = self.api(h)
        status, body = api.handle("POST", f"/api/admin/requests/{cred['id']}/reopen", {}, {}, ADMIN)
        self.assertEqual(status, 409)
        self.assertIn("作废", body["error"])
        self.assertEqual(h.store.get(cred["id"])["status"], t.CLOSED)


if __name__ == "__main__":
    unittest.main()
