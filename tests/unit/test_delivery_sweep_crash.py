"""sweep 崩在「还没开始干活」那一段时，运维能拿到什么（`cli_requests._sweep`）。

起因是线上告警刷屏：`delivery-sweep.service 异常退出，没来得及出报告
（崩溃 / 超时被杀 / 依赖导入失败）`，一分钟一条。

这个文件盯的是 `_sweep` 的**序幕**——建 store、建 Flows、第一次读申请单——
那几行在任何 `try` 之外。后面每一步、每张单子都包了 `except`（「一张单子出错
不能挡住后面的到期回收」），序幕却没有：序幕一抛异常，整个进程带着 traceback 退出，
一行报告都没有，`OnFailure` 兜底告警接管。

而 timer 是 `OnUnitActiveSec=1min`，所以「序幕里的错」= **每分钟一条告警，永不停止**，
且告警里那句「修好之前，这个任务管的数据不会更新」这时候是**真的**：
审批同步、到期回收、到期提醒，一样都没跑。

不碰网络、不碰真 identity/：每个用例自己开临时目录（跑完删掉），
并且把 `DELIVERY_FEISHU_*` 清空 —— 见 `FEISHU_ENV`。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import cli_requests
from delivery import people as people_mod
from delivery import tickets as t
from delivery.cli import EXIT_REPORTED

from . import test_delivery_access_requests as base
from .test_delivery_access_requests import APPROVAL_JSON, TEMPLATES, _roster

setUpModule = base.setUpModule
tearDownModule = base.tearDownModule

HEALTHY = json.dumps({"schema": t.SCHEMA, "tickets": []})

#: 飞书环境变量。**不清掉就会真的打到 open.feishu.cn。**
#: 本机（或 CI）上只要有人 export 过 `DELIVERY_FEISHU_APP_ID/SECRET`，`_sweep` 就会拿它们
#: 去换 tenant token，对照组那条随之变成 `飞书审批不可用…code=10003` → 退 1 → 红。
#: 实测：`DELIVERY_FEISHU_APP_ID=cli_x DELIVERY_FEISHU_APP_SECRET=s pytest <本文件>`
#: 在修之前会红在 `test_a_healthy_store_finishes_quietly`，stdout 里是飞书回的 `code=10003`。
#: 清法同兄弟文件 `test_delivery_sweep_exit_codes.py`：`patch.dict` 成空串 + `pop`
#: （空串就够了 —— `_sweep` 判的是 `app_id and secret`；`pop` 只是连 `.get` 的默认值一起对齐）。
#: （审计 Low-2）
FEISHU_ENV = ("DELIVERY_FEISHU_APP_ID", "DELIVERY_FEISHU_APP_SECRET", "DELIVERY_NOTIFY")


class SweepPrologueTests(unittest.TestCase):
    def sweep(self, tickets_text):
        """在一个临时 identity/ 里跑一轮 sweep。

        返回 `("exit", 退出码)` 或 `("crash", 异常)` —— 这两者对运维是**完全不同**的
        两件事：前者 journal 里有报告，后者只有 traceback，而告警文案是照后者写的。
        """
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)  # 不删的话每跑一次留一个目录
        ident = work / "identity"
        ident.mkdir()
        rows = [people_mod.person_row(p) for p in _roster().people]
        (ident / "people.json").write_text(
            json.dumps({"schema": people_mod.SCHEMA, "people": rows}), encoding="utf-8"
        )
        (ident / "templates.json").write_text(json.dumps(TEMPLATES), encoding="utf-8")
        (ident / "approval.json").write_text(json.dumps(APPROVAL_JSON), encoding="utf-8")
        (ident / "tickets.json").write_text(tickets_text, encoding="utf-8")
        args = argparse.Namespace(
            tickets="identity/tickets.json",
            templates="identity/templates.json",
            approval="identity/approval.json",
            people="identity/people.json",
            proposal="identity/sso-map.proposal.json",
            manual="identity/manual-links.json",
        )
        cwd = Path.cwd()
        os.chdir(work)
        try:
            with contextlib.ExitStack() as stack:
                # **先 patch.dict 再 pop**：`patch.dict` 退出时是「清空 + 灌回进入那一刻的副本」，
                # 在它之前 pop 掉的键不会被还原（那会把跑测试的人自己的 env 弄丢）
                stack.enter_context(mock.patch.dict(os.environ, {k: "" for k in FEISHU_ENV}))
                for key in FEISHU_ENV:  # 别的模块（和跑测试的人）可能留下值
                    os.environ.pop(key, None)
                stack.enter_context(mock.patch("delivery.cli._git_toplevel", lambda path: None))
                return ("exit", cli_requests._sweep(args))
        except Exception as exc:  # noqa: BLE001 —— 崩了也是一种结果，正是这里要观察的
            return ("crash", exc)
        finally:
            os.chdir(cwd)

    def test_a_healthy_store_finishes_quietly(self):
        """对照组：申请单存储是好的时候，一轮空跑退出 0、不触发兜底告警。

        没有这条对照，下面那条 expectedFailure 说不清是「存储坏了才崩」
        还是「这套临时目录本来就跑不起来」。
        """
        self.assertEqual(self.sweep(HEALTHY), ("exit", 0))

    def test_an_unreadable_store_is_reported_instead_of_crashing(self):
        """回归锁（2026-09-23 修）：存储读不了要打印一行原因再退 1，不许带 traceback 崩掉。

        修之前：

        `src/delivery/cli_requests.py:775`（`TicketStore(args.tickets)`）到 `:797`
        （`for ticket in flows.store.all()`）都在 try 之外，而 `store.all()` 会去
        读文件并解析 JSON —— 文件被写到一半、磁盘满、空文件，都会抛
        `TicketError: 读不了申请单存储：JSONDecodeError` 一路冒到顶。

        本机实测（三种输入各跑一轮）：
            正常空单子                → 退出码 0
            被截断的 tickets.json     → 崩溃 TicketError: 读不了申请单存储：JSONDecodeError
            空文件                    → 崩溃 TicketError: 读不了申请单存储：JSONDecodeError

        后果不是「少办一张单子」，是**这台机器上的审批同步、到期回收、到期提醒
        全部停摆**，而且 timer 一分钟一轮 → 兜底告警一分钟一条，刷到没人再看它。
        告警正文也帮不上忙：它照「崩溃 / 超时被杀 / 依赖导入失败」写死，
        不会告诉你是申请单存储读不了。

        期望：打印一行说明（例如「申请单存储读不了：…，本次什么都没做」）后返回非零，
        让 journal 的最后一行就是原因；`_sweep` 的其余部分本来就是按「出错也要留下
        一句话」设计的（「一张单子出错不能挡住后面的到期回收」）。
        实际：`TicketError` 直接冒到进程外。
        修法：把 `TicketStore(...)` + 第一次 `flows.store.all()` 包进 try，
        和 `:754-764` 那段「飞书不可用」同一个写法。
        """
        how, what = self.sweep('{"schema": "wuji-tickets@1", "tickets": [')
        self.assertEqual(how, "exit", f"不该抛到进程外：{what!r}")
        # **必须正好是 1。** `assertNotEqual(what, 0)` 在返回 3 时照样绿，而 3 被
        # `SuccessExitStatus=3` 声明成正常结束 —— 那就等于「审批同步 / 到期回收 /
        # 到期提醒全停」这件事一声不吭地过去了，正是这条用例要挡的（审计 Low-3）
        self.assertEqual(what, 1, "存储读不了 = 这一轮什么都没干成，要惊动人")
        self.assertNotEqual(
            what,
            EXIT_REPORTED,
            "3 的含义是「干完了、某张单子有问题、待办页上看得到」；这里一张单子都没读到",
        )

    def test_an_empty_store_file_is_reported_the_same_way(self):
        """空文件（写到一半断电、`truncate` 后进程被杀）和截断的 JSON 是同一类。

        分开列是因为它走的是 `json.loads("")` 那条分支，而且它是线上更常见的那种形态：
        磁盘满的时候写出来的就是 0 字节。
        """
        how, what = self.sweep("")
        self.assertEqual(how, "exit", f"不该抛到进程外：{what!r}")
        self.assertEqual(what, 1)

    def test_the_reason_is_the_last_thing_in_the_journal(self):
        """退 1 还不够：`OnFailure` 那条告警只会说「这一轮没跑成」，
        真正能省下排查时间的是 journal 最后一行写着「读不了申请单存储」。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            how, what = self.sweep("")
        self.assertEqual((how, what), ("exit", 1))
        self.assertIn("读不了申请单存储", out.getvalue())


if __name__ == "__main__":
    unittest.main()
