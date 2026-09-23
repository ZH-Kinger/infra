"""`cli_requests._sweep` 的退出码分级：**1 = 这一轮没干成活，3 = 干完了、有问题、已经报了。**

起因是 2026-09-23 线上刷屏。面板机上 `delivery-sweep.timer` 是一分钟一轮；两张凭证单
（tak-2512b462b3 / tak-3bd9eb6811）的申请人没有任何飞书标识，到期提醒**永远**发不出去，
于是每一轮都打印一行「…：到期提醒发送失败」→ 老代码里这一行让 `problems` 加一 → 退出码 1
→ `OnFailure=delivery-unit-failed@%n.service` 触发 → 管理员**每分钟**收到一条
「异常退出，没来得及出报告（崩溃 / 超时被杀 / 依赖导入失败）」。三种猜测的原因一个都不是：
任务跑得好好的，只是有两张办不了的单子。journalctl 实证已确认。

所以这个文件锁的是「哪一类问题配得上惊动 systemd」，一条一条穷举：

    整步抛异常 / 飞书审批整个跳过         → 1   （OnFailure 兜底告警接管）
    存储读不了（序幕就挂了）              → 1   （锁在 `test_delivery_sweep_crash.py`）
    `revoke_expired` 的问题行             → 1   （**到期没收干净 = 一把还能用的凭证留在云上**）
    单张单子同步失败 / 其余四步的问题行   → 3   （`EXIT_REPORTED`）
    两者同时                              → 1   （broken 压倒 handled）
    干净一轮                              → 0

「问题行」的判据是 `flows.is_trouble`（`flows.TROUBLE_WORDS`），**拼行和判行共用同一张表**；
那张表本身的用例在 `test_delivery_trouble_words.py`。

3 靠 `deploy/panel/delivery-sweep.service` 里的 `SuccessExitStatus=3` 声明成正常结束 ——
代码和单元文件是**一对**，拆开任何一半刷屏就会回来，那一半锁在
`test_delivery_alert_fallback.py::UnitFileTests`（**不是** `test_delivery_sweep_crash.py`：
那个文件里只有 `SweepPrologueTests`，指过去的交叉引用是错的，审计 Low-7）。

**步骤输出一律用 flows 的真实行文**，每处标出处（`flows.py:行号`）。编出来的字符串
（比如「写 IAM 失败」）会让用例在「分级逻辑早就看不懂真实行文了」的时候照样绿 ——
`retry_iam_writes` 真正的两条问题行是「登录名还是没写进公司 IAM（…）」和
「补写登录名出错（…）」，一个「失败」字都没有（审计 Med-2）。最要紧的几条干脆
不打桩、直接用真 `Flows` 跑出来：见 `RealWordingTests`。

不碰网络、不碰真 identity/：每个用例自己开临时目录，跑完删掉，`DELIVERY_FEISHU_*` 清空。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from delivery import cli_requests
from delivery import people as people_mod
from delivery import tickets as t
from delivery.cli import EXIT_REPORTED

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import ACC, APPROVAL_JSON, TEMPLATES, _roster

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

#: 线上那两张单子的原样：状态待审批，同步/提醒都会在它身上出问题
PENDING_TICKET = {"id": "tak-2512b462b3", "status": t.PENDING}

#: 飞书环境变量。不显式清掉的话，别的模块留下的值会让「飞书不可用」这条路悄悄换分支
FEISHU_ENV = ("DELIVERY_FEISHU_APP_ID", "DELIVERY_FEISHU_APP_SECRET", "DELIVERY_NOTIFY")

DAY = 86400


def _boom(message):
    """造一个「整步抛异常」的替身。"""

    def step(self, *a, **kw):
        raise RuntimeError(message)

    return step


def _returns(*lines):
    """造一个「跑完了、返回这些行」的替身。

    **传进来的行必须是从 `src/delivery/flows.py` 摘的原文**（调用处标 `flows.py:行号`）。
    自己编一句「写 IAM 失败」很省事，但那样锁住的是「含『失败』二字的行退 3」这个
    同义反复，而不是「flows 真的会吐的那些行分到了对的桶里」——
    真实行文里恰恰有不含「失败」的（审计 Med-2）。
    """

    def step(self, *a, **kw):
        return list(lines)

    return step


class SweepExitCodeTests(unittest.TestCase):
    """退出码用真的 `_sweep` 跑出来，不是把计数器抠出来单测 —— 分级出错的方式恰恰是
    「某一处 `+= 1` 记到了另一个桶里」，只有整轮跑才看得见。"""

    def sweep(self, *, tickets=(), feishu=False, patches=()):
        """在一个临时 identity/ 里跑一轮，返回 `(退出码, 打印出来的东西)`。

        `feishu=True` 才配飞书凭证 —— 否则 `_sweep` 连审批同步都不会尝试，
        「飞书不可用 → 1」那条路根本走不到。
        """
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        ident = work / "identity"
        ident.mkdir()
        rows = [people_mod.person_row(p) for p in _roster().people]
        (ident / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (ident / "templates.json").write_text(json.dumps(TEMPLATES), encoding="utf-8")
        (ident / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
        (ident / "tickets.json").write_text(
            json.dumps({"schema": t.SCHEMA, "tickets": list(tickets)}), encoding="utf-8"
        )
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/templates.json",
            approval="identity/approval.json",
            people="identity/people.json",
            proposal="identity/sso-map.proposal.json",
            manual="identity/manual-links.json",
        )
        env = {k: "" for k in FEISHU_ENV}
        if feishu:
            env = {"DELIVERY_FEISHU_APP_ID": "cli_x", "DELIVERY_FEISHU_APP_SECRET": "s"}
        out = io.StringIO()
        cwd = Path.cwd()
        os.chdir(work)
        try:
            with contextlib.ExitStack() as stack:
                # **先 patch.dict 再 pop**：`patch.dict` 退出时是「清空 + 灌回进入那一刻的
                # 副本」，在它之前 pop 掉的键不会被还原 —— 那会把跑测试的人自己 export 的
                # `DELIVERY_FEISHU_*` 在整个进程里弄丢（后面别的测试文件就不是它以为的环境了）
                stack.enter_context(mock.patch.dict(os.environ, env))
                for key in FEISHU_ENV:  # 别的模块可能留下值，先清干净
                    if not env.get(key):
                        os.environ.pop(key, None)
                stack.enter_context(mock.patch("delivery.cli._git_toplevel", lambda path: None))
                for target, replacement in patches:
                    stack.enter_context(mock.patch(target, replacement))
                stack.enter_context(contextlib.redirect_stdout(out))
                code = cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        return code, out.getvalue()

    # ── 0：什么问题都没有 ────────────────────────────────────────────────
    def test_a_clean_round_exits_zero(self):
        """对照组。没有它，下面每一条都说不清是「分级对了」还是「这套临时目录本来就非零」。"""
        code, _ = self.sweep()
        self.assertEqual(code, 0)

    def test_steps_that_print_ordinary_lines_are_not_problems(self):
        """步骤有输出 ≠ 有问题。只有 `flows.TROUBLE_WORDS` 里的词才算，别的行照打
        不影响退出码 —— 不然任何一条正常日志都会变成一分钟一条私聊。

        这几行都是 flows 的真实进度行文（不是编的），一条一条过：
        `flows.py:1776`（已提醒）、`:2670`（已补写登录名）、`:1600`（停住的单子接着处理完）。
        """
        code, out = self.sweep(
            patches=[
                ("delivery.flows.Flows.remind_expiring", _returns("tak-1：已提醒，还有 3 天到期")),
                (
                    "delivery.flows.Flows.retry_iam_writes",
                    _returns("req-2：已补写登录名进公司 IAM"),
                ),
                (
                    # 这一句是 `resume_approved` 的行文，但那一步只有飞书可用时才进
                    # `steps`（见下面 test_a_problem_resuming_an_approved_ticket…），
                    # 所以这里借 recover_stuck 的位置把它喂进去 —— 这条用例看的是
                    # 「这样的一行不算问题」，跟谁说的无关
                    "delivery.flows.Flows.recover_stuck",
                    _returns("req-9：审批通过后停住的单子已接着处理（已完成）"),
                ),
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("已提醒", out)
        self.assertIn("已补写登录名进公司 IAM", out)

    # ── 3：干完了，有单子有问题，已经报出来了 ─────────────────────────────
    def test_one_bad_ticket_is_reported_not_escalated(self):
        """单张单子同步失败：打印、跳过、继续跑后面的，退 3。

        退 1 的那些年，一张审批码写坏的单子就能让兜底告警一分钟一条。
        """
        code, out = self.sweep(
            tickets=[PENDING_TICKET],
            patches=[("delivery.flows.Flows.sync", _boom("实例被删了"))],
        )
        self.assertEqual(code, EXIT_REPORTED)
        self.assertIn("tak-2512b462b3：同步失败", out)

    def test_an_undeliverable_expiry_reminder_does_not_page_anyone(self):
        """**线上那一幕的回归**：申请人没有飞书标识 → 每轮都是「到期提醒发送失败」。

        这是一张办不了的单子，不是一次没跑成的任务：它该出现在待办页上（`delivery/todo.py`
        从单子里读，一条就是一条），而不是变成一分钟一条私聊。
        """
        code, out = self.sweep(
            patches=[
                (
                    "delivery.flows.Flows.remind_expiring",
                    # flows.py:1764（第一轮的行文，一个字不改；重试稳态那条见 RealWordingTests）
                    _returns("tak-2512b462b3：到期提醒发送失败"),
                )
            ]
        )
        self.assertEqual(code, EXIT_REPORTED)
        self.assertIn("到期提醒发送失败", out)

    def test_an_interrupted_line_is_also_only_reported(self):
        """「中断」和「失败」同一类：单子上记了、待办页看得到。"""
        code, _ = self.sweep(
            patches=[
                # flows.py:1574 —— 原文是「已标为失败」，不是「已置为失败」
                ("delivery.flows.Flows.recover_stuck", _returns("req-9：开通中断，已标为失败"))
            ]
        )
        self.assertEqual(code, EXIT_REPORTED)

    def test_many_reported_problems_still_only_make_it_three(self):
        """三个都出问题也还是 3 —— 分级看的是**种类**，不是条数。
        （老代码是一个计数器，条数一多就更像「真出事了」，其实一样是办不了的单子。）"""
        code, _ = self.sweep(
            tickets=[PENDING_TICKET],
            patches=[
                ("delivery.flows.Flows.sync", _boom("实例被删了")),
                # flows.py:1764 / :2668 —— 后者原先写成编的「req-2：写 IAM 失败」，
                # 而真实行文是「补写登录名出错（…）」，一个「失败」字都没有（审计 Med-2）
                ("delivery.flows.Flows.remind_expiring", _returns("tak-1：到期提醒发送失败")),
                (
                    "delivery.flows.Flows.retry_iam_writes",
                    _returns("req-2：补写登录名出错（IAM 接口 500）"),
                ),
            ],
        )
        self.assertEqual(code, EXIT_REPORTED)

    # ── 1：这一轮真没干成活 ──────────────────────────────────────────────
    def test_a_whole_step_blowing_up_is_a_real_failure(self):
        """整步抛异常 = 到期回收这一轮压根没跑 → 1 → 兜底告警接管。"""
        code, out = self.sweep(
            patches=[("delivery.flows.Flows.revoke_expired", _boom("云接口 500"))]
        )
        self.assertEqual(code, 1)
        self.assertIn("定时任务出错", out)

    def test_sweep_still_revokes_when_feishu_token_fails(self):
        """飞书审批整个跳过 → 1；但到期回收**照跑** —— 那是不依赖飞书的那一半活。

        `test_delivery_access_requests.py::SweepTests` 的注释指着这条：
        「1 留给这一轮真没干成活」的那个例子就是它。
        """
        revoked = []

        def revoke(self, *a, **kw):
            revoked.append(True)
            return []

        code, out = self.sweep(
            feishu=True,
            patches=[
                ("delivery.identity.directory.tenant_token", _boom("拿不到 tenant token")),
                ("delivery.flows.Flows.revoke_expired", revoke),
            ],
        )
        self.assertEqual(code, 1)
        self.assertIn("飞书审批不可用", out)
        self.assertEqual(revoked, [True], "飞书挂了不该连到期回收一起停")

    def test_broken_beats_handled(self):
        """一边有办不了的单子、一边有步骤崩了 → 1。

        **方向只能是这个**：把真故障降级成 3 就等于关掉兜底告警，而任务可以连着
        挂好几天没人知道；反过来（handled 压倒 broken）只是多收几条私聊。
        """
        code, out = self.sweep(
            tickets=[PENDING_TICKET],
            patches=[
                ("delivery.flows.Flows.sync", _boom("实例被删了")),
                ("delivery.flows.Flows.revoke_expired", _boom("云接口 500")),
            ],
        )
        self.assertEqual(code, 1)
        # 降级不等于吞掉：那张坏单子的行照样要在日志里
        self.assertIn("tak-2512b462b3：同步失败", out)
        self.assertIn("定时任务出错", out)

    def test_a_broken_step_does_not_stop_the_later_ones(self):
        """崩掉的那一步跳过，后面的步骤继续 —— 退出码是 1，但活该干的还是干了。"""
        later = []

        def remind(self, *a, **kw):
            later.append(True)
            return []

        code, _ = self.sweep(
            patches=[
                ("delivery.flows.Flows.revoke_expired", _boom("云接口 500")),
                ("delivery.flows.Flows.remind_expiring", remind),
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(later, [True])

    # ── 1：回收失败是「问题行」里的例外 ──────────────────────────────────
    def test_a_failed_revoke_pages_even_though_the_step_finished(self):
        """`revoke_expired` 吐的问题行 → **1**，不是 3（这一步 `urgent=True`）。

        为什么单它例外：到期没收干净 = **一把还能用的凭证留在云上，而我们以为收了**。
        其余四步的问题最坏是「某个人的某张单子办不了」，有人在等、待办页上看得到；
        这一条没有对应的人在等，不主动说就永远没人知道。

        它也不会变成刷屏：单子收成功后这行自然消失，而且兜底告警有 6 小时冷却兜着
        （`cli._alert_cooldown`）。
        """
        code, out = self.sweep(
            patches=[
                # flows.py:1802（revoke_expired 自己的兜底 except）
                (
                    "delivery.flows.Flows.revoke_expired",
                    _returns("tak-1：回收失败（AccessDenied），下次重试"),
                )
            ]
        )
        self.assertEqual(code, 1, "到期没收干净必须惊动人")
        self.assertIn("回收失败", out)

    def test_the_other_steps_are_still_only_reported(self):
        """反过来的一边：同样一句「失败」，出自别的步骤就只退 3。

        逐步过一遍（`revoke_expired` 之外的四步各来一次），因为 urgent 是**按步**给的，
        错给到别的步骤上就是刷屏原样回来 —— 而那种错只有一条一条试才看得见。
        `resume_approved` 只有在飞书审批可用时才进 `steps`，所以单独一条（见下）。
        """
        cases = {
            # flows.py:1574 / :1802 的措辞里都有「失败」，差别只在谁说的
            "recover_stuck": "req-1：开通中断，已标为失败",
            "retry_iam_writes": "req-1：补写登录名出错（IAM 接口 500）",
            "remind_expiring": "tak-1：到期提醒发送失败",
        }
        for step, line in cases.items():
            with self.subTest(step):
                code, out = self.sweep(patches=[(f"delivery.flows.Flows.{step}", _returns(line))])
                self.assertEqual(code, EXIT_REPORTED, f"{step} 的问题行不该惊动 systemd")
                self.assertIn(line, out)

    def test_a_problem_resuming_an_approved_ticket_is_only_reported(self):
        """第四步 `resume_approved`：它只在飞书审批可用时才进 `steps`。

        所以这条得把飞书配上（`tenant_token` 换成不打网络的替身），否则那一步
        根本不会被调用，用例会「因为没跑到」而绿。
        """
        code, out = self.sweep(
            feishu=True,
            patches=[
                ("delivery.identity.directory.tenant_token", lambda *a, **kw: "t-test"),
                # flows.py:1596
                (
                    "delivery.flows.Flows.resume_approved",
                    _returns("req-1：审批通过后中断，继续处理失败（已失败）"),
                ),
            ],
        )
        self.assertEqual(code, EXIT_REPORTED)
        self.assertIn("审批通过后中断", out, "这一步压根没被调用，那这条用例什么也没证明")

    def test_an_urgent_line_beats_a_reported_one(self):
        """回收失败 + 别的步骤有问题 → 1。urgent 走的是 `broken` 那个桶，压倒 handled。"""
        code, out = self.sweep(
            patches=[
                (
                    "delivery.flows.Flows.revoke_expired",
                    _returns("tak-1：回收失败（AccessDenied），下次重试"),
                ),
                ("delivery.flows.Flows.remind_expiring", _returns("tak-2：到期提醒发送失败")),
            ]
        )
        self.assertEqual(code, 1)
        # 压倒不等于吞掉：两行都要在 journal 里
        self.assertIn("回收失败", out)
        self.assertIn("到期提醒发送失败", out)

    def test_a_clean_revoke_step_does_not_page(self):
        """`revoke_expired` 的**成功**行不是问题行 —— urgent 只提高问题行的档次，
        不是「这一步出声就报警」。不然每收掉一张到期凭证就私聊一次。"""
        code, out = self.sweep(
            patches=[
                # flows.py:2055（_revoke 成功）/ :1951 + :1943（_mark_revoked 的 note）
                (
                    "delivery.flows.Flows.revoke_expired",
                    _returns(
                        "req-1：已移出 grp-oss-read",
                        "tak-1：到期：已删除子账号 tempak-abc 及其密钥和策略",
                    ),
                )
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("已移出", out)

    # ── 分级本身 ────────────────────────────────────────────────────────
    def test_the_three_outcomes_are_three_different_numbers(self):
        """3 必须既不是 0 也不是 1：和 0 撞 = 真问题被当成干净一轮，和 1 撞 = 刷屏原样回来。"""
        self.assertNotIn(EXIT_REPORTED, (0, 1))

    def test_the_judgement_is_the_one_flows_exports(self):
        """判问题行用的必须是 `flows.is_trouble`，不是 `_sweep` 自己手写的一张表。

        手写的那张表就是这次的根因：`("失败", "中断")` 漏掉了 flows 里**两类持续失败**
        的真实行文，于是它们被算成干净一轮退 0 —— 一个永远办不了的单子看起来和
        万事大吉一模一样。这条用例把「拼行的地方和判行的地方共用同一张表」钉住：
        改判据的人必须改 `flows.TROUBLE_WORDS`，而那里就在被判的那些行旁边。
        """
        seen = []

        def spy(line):
            seen.append(line)
            return "到期提醒" in str(line)

        code, _ = self.sweep(
            patches=[
                ("delivery.flows.is_trouble", spy),
                ("delivery.flows.Flows.remind_expiring", _returns("tak-1：到期提醒发送失败")),
            ]
        )
        self.assertEqual(seen, ["tak-1：到期提醒发送失败"], "_sweep 没问 flows.is_trouble")
        self.assertEqual(code, EXIT_REPORTED)


class RealWordingTests(unittest.TestCase):
    """**不打桩**：用真 `Flows` 跑出问题行，再看退出码。

    上面那些用例把步骤整个换成替身，所以它们锁的是「给定这一行，分到哪个桶」；
    这里锁的是另一半 ——「flows 真的会吐出这样一行，而且它确实被算成了问题」。
    少了这一半，`flows.py` 的行文一改（或者 `TROUBLE_WORDS` 漏收一类），
    上面全绿、线上退 0，和 2026-09-23 之前一模一样。

    每一条都注明：**老判据 `("失败", "中断")` 会把它判成干净一轮。**
    """

    NOW = 1_800_000_000.0

    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.ident = self.work / "identity"
        self.ident.mkdir()
        rows = [people_mod.person_row(p) for p in _roster().people]
        self.write("people.json", {"schema": people_mod.SCHEMA, "people": rows})
        self.write("templates.json", TEMPLATES)
        self.write("approval.json", APPROVAL_JSON)

    def write(self, name, data):
        (self.ident / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def sweep(self, *tickets, patches=()):
        self.write("tickets.json", {"schema": t.SCHEMA, "tickets": list(tickets)})
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/templates.json",
            approval="identity/approval.json",
            people="identity/people.json",
            proposal="identity/sso-map.proposal.json",
            manual="identity/manual-links.json",
        )
        out = io.StringIO()
        cwd = Path.cwd()
        os.chdir(self.work)
        try:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, {k: "" for k in FEISHU_ENV}))
                for key in FEISHU_ENV:
                    os.environ.pop(key, None)
                stack.enter_context(mock.patch("delivery.cli._git_toplevel", lambda path: None))
                for target, replacement in patches:
                    stack.enter_context(mock.patch(target, replacement))
                stack.enter_context(contextlib.redirect_stdout(out))
                code = cli_requests._sweep(args)
        finally:
            os.chdir(cwd)
        return code, out.getvalue()

    def test_a_reminder_that_keeps_failing_is_reported_not_swallowed(self):
        """**这次改动的正主**（审计 Med-2）：`到期提醒仍然发不出去（…）`。

        它是重试稳态的行文 —— 第一轮是「到期提醒发送失败」（带「失败」，老判据认得），
        从第二轮起就换成这一句，而 sweep 是一分钟一轮：也就是说线上 99.9% 的轮次
        打的都是这一句，**老判据一个字都不认**，于是退 0 = 干净一轮。
        两张办不了的单子从此在退出码上彻底隐形。

        怎么跑出来的：单子处在「上一轮这一档刚失败过」的状态（最后一条事件是
        `expiry_remind_failed:7`，note 以同样的原因结尾），通知回调照样抛同一个原因
        → `flows.py:1748-1753` 的 retrying 分支。
        """
        reason = "申请人没有飞书标识"

        def notifier(event, ticket):  # 没有 reaches_applicant 属性 = 能发给申请人
            raise RuntimeError(reason)

        code, out = self.sweep(
            self.credential_ticket(reason),
            patches=[("delivery.notify.from_env", lambda env, token=None: notifier)],
        )
        self.assertIn("到期提醒仍然发不出去", out, f"没跑到重试稳态那一支：{out!r}")
        self.assertNotIn("失败", out.splitlines()[0], "这一行里确实没有「失败」二字")
        self.assertEqual(code, EXIT_REPORTED, "老判据在这里退的是 0")

    def test_an_iam_write_that_keeps_failing_is_reported_not_swallowed(self):
        """另一类老判据认不出的：`登录名还是没写进公司 IAM（…）`（`flows.py:2665`）。

        后果是「号建出来了、单子绿着、人登不进控制台」。它没有「失败」也没有「中断」，
        老判据同样退 0。

        怎么跑出来的：一张 DONE 的开账号单，`user_created` 有、`iam_written` 没有 →
        `retry_iam_writes` 调 `push_iam`，而这一轮 `--iam-attributes` 没配
        （`iam_sync.writer("")` → None）→ `_write_iam_attr` 报「没接上写入回调」
        → `push_iam` 抛 `FlowError`。这正是线上那张挂了两天的单子（REQ-20260921）的形态。
        """
        code, out = self.sweep(self.account_ticket())
        self.assertIn("登录名还是没写进公司 IAM", out, f"没跑到那一支：{out!r}")
        first = out.splitlines()[0]
        self.assertNotIn("失败", first)
        self.assertNotIn("中断", first)
        self.assertEqual(code, EXIT_REPORTED)

    def test_a_real_failed_revoke_pages(self):
        """真 `revoke_expired` 收不掉一张到期权限单 → `flows.py:2028` 的
        `回收失败，下次重试` → 退 1（urgent）。

        开通身份拿不到（AK 没配 / 云接口挂了）是线上最常见的形态，这里就打这个桩，
        行文仍然由 flows 自己拼。
        """

        def no_executor(platform, account, **kw):
            from delivery.provision import ProvisionError

            raise ProvisionError("没配阿里云开通身份的 AK")

        code, out = self.sweep(
            self.expired_permission_ticket(),
            patches=[("delivery.provision.executor_from_env", no_executor)],
        )
        self.assertIn("回收失败", out)
        self.assertEqual(code, 1, "到期没收干净 = 一把还能用的凭证留在云上")

    def test_a_reminder_that_goes_through_is_a_clean_round(self):
        """对照组：同一张单子，通知发得出去 → 真 flows 打「已提醒，…」→ 退 0。

        没有它，上面两条说不清是「分类对了」还是「这套单子怎么摆都退 3」。
        """
        sent = []

        def notifier(event, ticket):
            sent.append(event)

        code, out = self.sweep(
            self.credential_ticket("申请人没有飞书标识", events=[]),
            patches=[("delivery.notify.from_env", lambda env, token=None: notifier)],
        )
        self.assertEqual(sent, ["expiring"])
        self.assertIn("已提醒", out)
        self.assertEqual(code, 0)

    # ── 单子 ────────────────────────────────────────────────────────────
    def credential_ticket(self, reason, events=None):
        """7 天档内到期的凭证单。`events` 默认摆成「上一轮这一档刚失败过」。"""
        return {
            "id": "tak-2512b462b3",
            "kind": "credential",
            "status": t.DONE,
            "created_at": "2026-09-01T00:00:00+08:00",
            "expires_at_ts": time.time() + 5 * DAY,
            "template": {"id": "dev-sts", "platform": "aliyun", "account": ACC, "title": "凭证"},
            "payload": {},
            # 线上那两张单子的要害就在这里：申请人没有任何飞书标识
            "applicant": {"union_id": "", "name": "某人"},
            "events": [
                {
                    "event": "expiry_remind_failed:7",
                    "note": f"还有 5 天到期的提醒没有发出去：{reason}",
                }
            ]
            if events is None
            else list(events),
        }

    def account_ticket(self):
        return {
            "id": "req-20260921",
            "kind": "account",
            "status": t.DONE,
            "created_at": "2026-09-21T00:00:00+08:00",
            "user_created": True,
            "iam_written": False,
            "template": {
                "id": "new-user",
                "platform": "aliyun",
                "account": ACC,
                "title": "新员工子账号",
            },
            "payload": {"username": "lisi"},
            "applicant": {"union_id": "on_li", "name": "李四"},
            "events": [],
        }

    def expired_permission_ticket(self):
        return {
            "id": "req-1",
            "kind": "permission",
            "status": t.DONE,
            "created_at": "2026-08-01T00:00:00+08:00",
            "expires_at_ts": time.time() - DAY,
            "template": {
                "id": "oss-read",
                "platform": "aliyun",
                "account": ACC,
                "title": "OSS 只读",
                "groups": ["grp-oss-read"],
            },
            "payload": {"cloud_user": "lisi"},
            "applicant": {"union_id": "on_li", "name": "李四"},
            "events": [],
        }


if __name__ == "__main__":
    unittest.main()
