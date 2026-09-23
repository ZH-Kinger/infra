"""兜底告警分不分得清「这是演习还是真故障」——`cli._is_drill` / `_how_it_died` + 标题 + 记账。

2026-09-23 线上真事：告警那批改动上线后，有人手工触发了两次兜底告警做验证，**收件人以为
线上又出故障了**，去翻了一遍日志；那两次还顺手把 `delivery-sweep` 的 6 小时冷却占掉了。
所以要把「演习」和「真故障」分开 —— 但**分错方向的代价极不对称**：

    演习被写成故障  → 有人白紧张一次、翻一遍日志
    真故障被写成演习 → 收件人看一眼标题就划过去，而那正是兜底告警唯一要拦的时刻

判据经过一次重写（审计读 systemd v255 源码后推翻了两个假设，2026-09-23）：

  · `$MONITOR_*` 是 systemd **v251** 起才有的，不是 v250 —— **本项目的开发机是 249**，
    爆炸半径内已经有一台机器不成立（Ubuntu 20.04=245 / Debian 11=247 / RHEL8=239 同理）。
  · 注入条件也不是「触发方 `Type=oneshot`」，而是「同一个 handler 实例只能有一个触发方」
    （`service.c:1574 service_get_triggering_service()` 多于一个 `OnFailureOf` 直接返回
    NULL）—— 那正是 `OnFailure=…@%n.service` 里 `%n` 在保证的事。

于是判据不再是「`MONITOR_UNIT` 缺失 = 演习」（那会让老 systemd 上**每一条真告警**都顶着
「告警演习」的标题，一声不吭），改成**演习自报家门**、拿不准就往真故障那边倒：

    MONITOR_UNIT 在                      → 真故障（systemd 亲口说的）
    实例名就是 drill / drill.service      → 演习
    有 INVOCATION_ID（任何 systemd 起的单元都有，v232 起，与 MONITOR_* 条件独立）
                                         → 真故障 + stderr 留一行痕
    三者都没有                            → 演习（连 systemd 都不在，是人在 shell 里跑）

本文件锁四件事：**标题**（卡片上最显眼，只改正文等于没改）、**反方向**（真故障绝不许被写成
演习，含老 systemd 那条路）、**`MONITOR_*` 终究是环境变量**（值再畸形也不许把告警通道自己
带塌 —— 这类是承重墙）、**记账**（演习记在 `drill:` 键下，不许污染真单元的冷却和连号）。

冷却窗口本身在 `test_delivery_alert_cooldown.py`，告警通道的其余部分（退出码分级、
没配 webhook 时私聊、文案不猜原因）在 `test_delivery_alert_fallback.py`。

**演习怎么做**：

    systemctl start delivery-unit-failed@drill.service

用这个名字，而不是真单元名 —— 它在任何环境下都被认成演习（第二层自报家门），
而拿真单元名在一台有 systemd 的机器上手跑，会命中第三层、被当成**真故障**播出去。
记账那一半代码侧已经兜住了（演习落在 `drill:<unit>` 键，不碰真单元的冷却/连号），
不必再靠人记住文档；`drill.service` 还让标题也一起对。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import cli, notify

#: 真实量级的时间戳，理由同 `test_delivery_alert_cooldown.py`：拿小数字当 now，
#: 连第一条告警都会被算进 6 小时冷却期。
NOW = 1_790_000_000.0
UNIT = "delivery-sweep.service"
DRILL_UNIT = "drill.service"  # 文档里那条演习命令的实例名

#: 判据读的全部环境。用例之间**必须**互不串，而且**必须把机器上原有的摘干净** ——
#: 这几个变量决定告警标题，串了就是「本机绿、换台机器红」那种最难查的假绿。
#: `INVOCATION_ID` 尤其坑：桌面终端、`systemd-run`、CI runner 里**可能本来就有**
#: （本机实测就有），不摘的话「人手跑」那一支根本测不到。
SYSTEMD_VARS = (
    "MONITOR_UNIT",
    "MONITOR_EXIT_CODE",
    "MONITOR_EXIT_STATUS",
    "MONITOR_SERVICE_RESULT",
    "INVOCATION_ID",
)


class DrillBase(unittest.TestCase):
    """写法照抄 `test_delivery_alert_cooldown.py::CooldownBase`，多一层环境隔离。"""

    def setUp(self):
        self.addCleanup(setattr, cli, "_admin_alert", cli._admin_alert)
        self.sent = []
        self.reason = ""  # 非空 = 这条告警一个人也没送到

        def fake(title, text, path=""):
            self.sent.append({"title": title, "text": text})
            return self.reason

        cli._admin_alert = fake
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.state = self.work / "alert-state.json"

    @contextlib.contextmanager
    def systemd_env(self, **values):
        """这一轮的 `MONITOR_*` / `INVOCATION_ID` **只**认这里给的，机器上原有的一律先摘掉。

        `patch.dict` 退出时整份还原 `os.environ`（它存的是原字典的副本），
        所以里面先 pop 再 update 都是可回滚的。
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in SYSTEMD_VARS:
                os.environ.pop(key, None)
            os.environ.update(values)
            yield

    def fail_once(self, *, unit=UNIT, now=NOW, state=None, **env):
        """报一次单元失败。不传任何环境 = 人在裸 shell 里手工跑的那种。"""
        path = self.state if state is None else state
        out, err = io.StringIO(), io.StringIO()
        with (
            self.systemd_env(**env),
            mock.patch.object(cli.time, "time", lambda: now),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(["unit-failed", "--unit", unit, "--state", str(path)])
        return code, out.getvalue(), err.getvalue()

    def real_failure(self, *, unit=UNIT, **kw):
        """systemd 因 `OnFailure=` 拉起来的形态：退出码 1 —— 线上 sweep 最常见的那种。"""
        kw.setdefault("MONITOR_UNIT", unit)
        kw.setdefault("MONITOR_EXIT_CODE", "exited")
        kw.setdefault("MONITOR_EXIT_STATUS", "1")
        kw.setdefault("INVOCATION_ID", "0f3c9e1ab4d2")
        return self.fail_once(unit=unit, **kw)

    def old_systemd(self, *, unit=UNIT, **kw):
        """systemd v251 以下（或同一 handler 有多个触发方）：只有 `INVOCATION_ID`，
        一个 `MONITOR_*` 都拿不到。开发机 249 就是这样。"""
        kw.setdefault("INVOCATION_ID", "0f3c9e1ab4d2")
        return self.fail_once(unit=unit, **kw)

    @property
    def last(self):
        return self.sent[-1]

    def units(self, state=None):
        path = self.state if state is None else state
        return json.loads(path.read_text(encoding="utf-8"))["units"]


class IsDrillJudgementTests(DrillBase):
    """判据本身。四层各一条，外加两条「别想当然」。

    标题问的是 `_is_drill()`，不是拿正文串去比（旧写法 `how != _DRILL` 耦合在一个正文
    字符串上：`_how_it_died` 将来只要给演习串加点修饰，标题就会**静默**退回，
    而正文还写着「这是演习」——两处自相矛盾比哪一处单独错更难查）。
    """

    #: (环境, 实例名, 是不是演习, 为什么)
    TABLE = (
        ({"MONITOR_UNIT": UNIT}, UNIT, False, "systemd 亲口说的：真故障"),
        ({"MONITOR_UNIT": ""}, UNIT, False, "判的是变量在不在，不是空不空"),
        ({"MONITOR_UNIT": UNIT}, DRILL_UNIT, False, "第一层先命中：systemd 真拉起来的就不是演习"),
        ({}, "drill", True, "自报家门（短名）"),
        ({}, DRILL_UNIT, True, "自报家门（全名）"),
        ({"INVOCATION_ID": "abc"}, DRILL_UNIT, True, "自报家门排在 INVOCATION_ID 之前"),
        ({"INVOCATION_ID": "abc"}, UNIT, False, "老 systemd：没有 MONITOR_* 也按真故障"),
        ({}, UNIT, True, "什么都没有 = 人在裸 shell 里跑"),
    )

    def test_who_counts_as_a_drill(self):
        for env, unit, want, why in self.TABLE:
            with self.subTest(env=sorted(env), unit=unit), self.systemd_env(**env):
                self.assertIs(cli._is_drill(unit), want, why)

    def test_the_drill_name_is_an_exact_match_not_a_prefix(self):
        """`drill2` / `drill-x` **不**算自报家门。

        写成前缀匹配的话，`drill-run.service`、`drillbit.service` 这种真单元名
        （今天没有、明天难说）会被整条静音成演习。名字白名单就该是白名单。

        它们在裸 shell 里仍然是演习 —— 那是第三层「连 systemd 都不在」的功劳，
        和名字无关；**有** `INVOCATION_ID` 时它们就落回真故障，这条差别正是这里要说清的。
        """
        for name in ("drill2", "drill-x", "drills.service", "mydrill", "drill.timer"):
            with self.subTest(name=name):
                with self.systemd_env(INVOCATION_ID="abc"):
                    self.assertFalse(cli._is_drill(name), f"{name} 不该被当成演习自报家门")
                with self.systemd_env():
                    self.assertTrue(cli._is_drill(name), f"{name} 在裸 shell 里靠第三层算演习")

    def test_the_title_cannot_be_flipped_by_what_lands_in_the_body(self):
        """对抗用例：`MONITOR_EXIT_STATUS` 里原样带着演习标记那串字。

        正文里于是会出现「这是手工触发的演习」（systemd 说什么就写什么），
        但这一轮确实是 systemd 拉起来的 —— 标题必须还是「定时任务没跑成」。
        标题和正文各问各的来源，谁也别拿对方当判据。
        """
        self.real_failure(MONITOR_EXIT_STATUS=cli._DRILL)
        self.assertEqual(self.last["title"], f"定时任务没跑成：{UNIT}")
        self.assertIn(cli._DRILL, self.last["text"])


class OldSystemdTests(DrillBase):
    """**最该锁的一条**：`MONITOR_*` 一个都拿不到时，告警仍然是「定时任务没跑成」。

    v251 以下的 systemd（本项目开发机 249、Ubuntu 20.04 245、Debian 11 247、RHEL8 239）
    根本不注入 `MONITOR_*`；同一个 handler 实例被多个单元触发时也不注入。
    判据要是只认「`MONITOR_UNIT` 缺失 = 演习」，换一台机器部署 → **每一条真告警的标题
    都变成「告警演习」**，而且没有任何征兆：收件人学会了忽略它，兜底等于没有。
    """

    def test_a_failure_without_monitor_vars_is_still_a_real_failure(self):
        code, _, _ = self.old_systemd()
        self.assertEqual(code, 0)
        self.assertEqual(self.last["title"], f"定时任务没跑成：{UNIT}")
        self.assertNotIn(cli._DRILL, self.last["text"])
        self.assertNotIn("演习", self.last["text"])

    def test_it_leaves_a_line_in_the_journal_so_someone_can_find_out(self):
        """按真故障处理是对的，但**不能悄悄**这么干：正文里少了「怎么死的」那半句，
        没有这行痕，谁也查不出「标题一直对、内容一直缺」是因为 systemd 太老。
        """
        _, _, err = self.old_systemd()
        self.assertIn("MONITOR_*", err)
        self.assertIn("按真故障处理", err)

    def test_it_says_nothing_about_the_exit_code_rather_than_guessing(self):
        self.old_systemd()
        text = self.last["text"]
        self.assertNotIn("退出码", text)
        self.assertNotIn("（）", text, "空括号让人以为消息被截断了")
        self.assertTrue(text.startswith(f"{UNIT} 这一轮没跑成。\n看日志"), text.splitlines()[0])

    def test_the_cooldown_is_booked_under_the_real_unit_not_under_drill(self):
        """老 systemd 上它是真故障，记账也必须落在真单元名下 ——
        落到 `drill:` 去的话，那台机器的冷却从此形同虚设（真单元永远是「第 1 次」）。
        """
        self.old_systemd(now=NOW)
        self.assertEqual(sorted(self.units()), [UNIT])


class DrillMarkerTests(DrillBase):
    def test_a_hand_run_in_a_bare_shell_says_so_in_both_the_title_and_the_body(self):
        code, _, err = self.fail_once()
        self.assertEqual(code, 0)
        self.assertIn(cli._DRILL, self.last["text"])
        self.assertEqual(self.last["title"], f"告警演习：{UNIT}")
        self.assertEqual(err, "", "裸 shell 里没什么可抱怨的")

    def test_the_documented_drill_unit_is_a_drill_even_under_systemd(self):
        """`systemctl start delivery-unit-failed@drill.service` —— 文档里那条命令。

        它是**有** `INVOCATION_ID` 的（systemd 起的），全靠自报家门这一层认出来。
        这条要是红了，那条演习命令就会往管理员那儿播一条长得像真故障的告警。
        """
        code, _, _ = self.fail_once(unit=DRILL_UNIT, INVOCATION_ID="abc")
        self.assertEqual(code, 0)
        self.assertIn(cli._DRILL, self.last["text"])
        self.assertEqual(self.last["title"], f"告警演习：{DRILL_UNIT}")

    def test_a_real_systemd_failure_is_never_labelled_a_drill(self):
        """反方向比正方向重要：真故障挂上「演习」两个字，收件人就学会了不看它。"""
        code, _, _ = self.real_failure()
        self.assertEqual(code, 0)
        self.assertNotIn(cli._DRILL, self.last["text"])
        self.assertNotIn("演习", self.last["text"])
        self.assertEqual(self.last["title"], f"定时任务没跑成：{UNIT}")

    def test_the_two_do_not_look_alike_at_a_glance(self):
        """**标题单独锁一条。** 飞书卡片上标题最显眼、正文要展开才看得全，
        所以「只改正文」等于没改 —— 收件人先看到的那一行仍然和真故障逐字一样。

        同时钉住：两种标题里都还留着单元名。分清了演习/故障却不说是谁挂了，
        换来的是另一次翻日志。
        """
        self.fail_once(unit=DRILL_UNIT, now=NOW)
        self.real_failure(unit=UNIT, now=NOW)
        drill, real = self.sent[0]["title"], self.sent[1]["title"]
        self.assertNotEqual(drill, real, "标题没跟着变：收件人第一眼看到的还是同一句话")
        self.assertIn(DRILL_UNIT, drill)
        self.assertIn(UNIT, real)

    def test_an_empty_monitor_unit_is_a_real_failure_not_a_drill(self):
        """`MONITOR_UNIT=`（设了但为空）算**真故障**：第一层判的是变量在不在。

        空值只可能来自「systemd 换了行为 / 中间套了层 wrapper」这类没人预料到的情况 ——
        猜不准的时候往「当成真故障」那边倒，代价方向见模块开头。
        """
        self.fail_once(MONITOR_UNIT="", MONITOR_EXIT_CODE="exited", MONITOR_EXIT_STATUS="1")
        self.assertNotIn(cli._DRILL, self.last["text"])
        self.assertEqual(self.last["title"], f"定时任务没跑成：{UNIT}")
        self.assertIn("退出码 1", self.last["text"], "另两个变量还在，该说的照说")


class HowItDiedTests(DrillBase):
    """退出情况透传。收到兜底告警的人第一个要问的就是「它是崩了还是被杀了」。"""

    def test_an_ordinary_non_zero_exit_says_the_code(self):
        self.real_failure(MONITOR_EXIT_CODE="exited", MONITOR_EXIT_STATUS="1")
        first = self.last["text"].splitlines()[0]
        self.assertIn("退出码 1", first, "要在第一行 —— 卡片上后面几行得展开才看得到")

    def test_a_unit_killed_by_the_timeout_names_the_signal_instead(self):
        """超时被杀的真实形态：`killed` + `TERM`（`TimeoutStartSec=` 到点 systemd 发 SIGTERM）。

        写成「退出码 TERM」就是假话 —— 被信号杀掉的进程根本没有退出码，
        而「有没有退出码」正好是判断「崩了 vs 卡死」的那一刀。
        """
        self.real_failure(MONITOR_EXIT_CODE="killed", MONITOR_EXIT_STATUS="TERM")
        text = self.last["text"]
        self.assertIn("killed", text)
        self.assertIn("TERM", text)
        self.assertNotIn("退出码", text, "它没有退出码，别替 systemd 编一个")

    def test_a_core_dump_keeps_systemds_own_words(self):
        self.real_failure(MONITOR_EXIT_CODE="dumped", MONITOR_EXIT_STATUS="ABRT")
        self.assertIn("dumped", self.last["text"])
        self.assertIn("ABRT", self.last["text"])
        self.assertNotIn("退出码", self.last["text"])

    def test_a_job_whose_main_process_never_ran_still_says_something(self):
        """`MONITOR_EXIT_CODE/STATUS` 还额外要求主进程真的跑起来并退出过；
        `MONITOR_SERVICE_RESULT` 是无条件给的。

        主进程压根没起来的那些真故障 —— **start-limit-hit**（「start request repeated
        too quickly」，对一分钟一轮的 sweep 相当现实）、ExecStartPre 失败、exec 之前
        就超时 —— 只有它说得出话。没这层兜底的话，正文第一行后面一个字都没有，
        而那恰恰是最让人摸不着头脑的一类失败。
        """
        for verdict in ("start-limit-hit", "timeout", "resources", "exec-condition"):
            with self.subTest(verdict):
                self.sent.clear()
                self.fail_once(
                    state=self.work / f"verdict-{verdict}.json",
                    MONITOR_UNIT=UNIT,
                    MONITOR_SERVICE_RESULT=verdict,
                )
                text = self.last["text"]
                self.assertIn(f"systemd 判定：{verdict}", text)
                self.assertNotIn("退出码", text, "它没有退出码，别编一个")
                self.assertEqual(self.last["title"], f"定时任务没跑成：{UNIT}")

    def test_the_exit_code_wins_when_systemd_gives_both(self):
        """三个都有时先说退出码 —— 「退出码 1」比「systemd 判定：exit-code」具体得多，
        而具体正是这行存在的理由。"""
        self.real_failure(MONITOR_SERVICE_RESULT="exit-code")
        text = self.last["text"]
        self.assertIn("退出码 1", text)
        self.assertNotIn("systemd 判定", text)

    def test_a_real_failure_without_any_details_says_nothing_rather_than_guessing(self):
        """三个 `MONITOR_*` 值都拿不到（只有 `MONITOR_UNIT`）：不崩、不瞎写，
        正文里干脆不提退出码，也**不**因此退回演习那一支。

        空括号「（）」同样不行 —— 那会让人以为消息被截断了，又是一次白查。
        """
        partial = (
            {},
            {"MONITOR_EXIT_CODE": "exited"},
            {"MONITOR_EXIT_STATUS": "1"},
            {"MONITOR_SERVICE_RESULT": ""},
        )
        for i, extra in enumerate(partial):
            with self.subTest(extra=sorted(extra)):
                self.sent.clear()
                code, _, err = self.fail_once(
                    state=self.work / f"partial-{i}.json", MONITOR_UNIT=UNIT, **extra
                )
                self.assertEqual(code, 0, err)
                text = self.last["text"]
                self.assertEqual(len(self.sent), 1)
                self.assertNotIn("退出码", text)
                self.assertNotIn("systemd 判定", text)
                self.assertNotIn(cli._DRILL, text, "systemd 拉起来的就不是演习")
                self.assertNotIn("（）", text, "空括号让人以为消息被截断了")
                self.assertTrue(
                    text.startswith(f"{UNIT} 这一轮没跑成。"),
                    f"第一行长这样：{text.splitlines()[0]}",
                )


class MonitorValuesAreStillJustEnvironmentTests(DrillBase):
    """**承重墙。** `MONITOR_*` 是 systemd 写的，但它终究是环境变量。

    这一类锁的不是「文案好不好看」，而是**告警通道不许被它带塌**：
    兜底告警发不出去 = 真出事时一个字都不报，比演习标记严重得多。
    """

    #: 每条都带着 `MONITOR_UNIT`（= 真故障），所以每条都必须发出来、且不许写成演习。
    WEIRD = {
        "三个都是空串": {
            "MONITOR_EXIT_CODE": "",
            "MONITOR_EXIT_STATUS": "",
            "MONITOR_SERVICE_RESULT": "",
        },
        "只有空格": {"MONITOR_EXIT_CODE": " ", "MONITOR_EXIT_STATUS": "   "},
        "判定只有空格": {"MONITOR_SERVICE_RESULT": " \t "},
        "status 不是数字": {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "失败了"},
        "status 是负数": {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "-1"},
        "没见过的 result": {"MONITOR_EXIT_CODE": "core-dumped", "MONITOR_EXIT_STATUS": "SEGV"},
        "带引号和反斜杠": {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": '1"\\{}'},
        "带换行": {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "1\n2"},
        "判定带换行": {"MONITOR_SERVICE_RESULT": "start-limit-hit\n假行"},
        "非 ASCII": {"MONITOR_EXIT_CODE": "退出", "MONITOR_EXIT_STATUS": "壹"},
        "五万个字符": {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "9" * 50_000},
        "判定五万个字符": {"MONITOR_SERVICE_RESULT": "x" * 50_000},
    }

    def test_no_monitor_value_can_stop_the_alert_from_going_out(self):
        for i, (label, env) in enumerate(self.WEIRD.items()):
            with self.subTest(label):
                self.sent.clear()
                code, _, err = self.fail_once(
                    state=self.work / f"weird-{i}.json", MONITOR_UNIT=UNIT, **env
                )
                self.assertEqual(code, 0, f"{label}：{err}")
                self.assertEqual(len(self.sent), 1, f"{label}：这条告警没发出去")
                self.assertNotIn(cli._DRILL, self.last["text"], f"{label}：真故障被写成了演习")
                self.assertEqual(self.last["title"], f"定时任务没跑成：{UNIT}", label)

    def test_the_card_survives_a_monstrous_value(self):
        """卡片是真正出门的那一步：正文再长也得序列化得出来、且不会被飞书按长度拒收。

        这里故意连着 `notify.alert_card` 一起测 —— `_how_it_died` 返回了什么串不重要，
        「那张卡还发不发得出去」才重要（它逐行截到 200 字，这条就是钉那个截断）。
        """
        for label, env in (
            ("退出码", {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "9" * 50_000}),
            ("systemd 判定", {"MONITOR_SERVICE_RESULT": "x" * 50_000}),
        ):
            with self.subTest(label):
                self.sent.clear()
                self.fail_once(state=self.work / f"huge-{label}.json", MONITOR_UNIT=UNIT, **env)
                card = notify.alert_card(self.last["title"], self.last["text"])
                blob = json.dumps(card, ensure_ascii=False)
                self.assertLess(len(blob), 20_000, f"{label}：卡片被撑到 {len(blob)} 字节")
                self.assertIn("…", blob, f"{label}：超长那行要被截断")

    def test_a_value_with_newlines_cannot_push_the_useful_lines_off_the_card(self):
        """回归锁（2026-09-23 修）：值里带换行时，**「去哪看日志」那几行必须还在卡片上**。

        修之前：`_how_it_died` 把原值直接拼进正文第一行，于是值里的换行在卡片上
        变成一行一段，而 `alert_card` 只留前 24 行 —— `journalctl …` 和下面三条
        「退 1 的真实来源」全被挤出去。告警还在，但它不再告诉你下一步做什么，
        而「下一步做什么」正是兜底告警存在的理由。

        修法是 `_how_it_died` 里的 `_flat()`：`" ".join(x.split())` 把三个值都压成单行
        （`notify._clip` 对单行也是这么干的）。压完这一大坨落在第一行里，再由 `_clip`
        截到 200 字 —— 正文的结构不受外部值影响。
        """
        self.real_failure(MONITOR_EXIT_STATUS="1\n" + "假行\n" * 40)
        self.assertEqual(len(self.sent), 1, "底线：这条还是发出去了")
        elements = notify.alert_card(self.last["title"], self.last["text"])["body"]["elements"]
        body = json.dumps(elements, ensure_ascii=False)
        self.assertIn("journalctl", body, "「去哪看日志」被挤出卡片了")
        for source in ("到期回收没收干净", "审批同步整步跳过", "读不了申请单存储"):
            self.assertIn(source, body, f"「{source}」那行被挤出卡片了")
        self.assertLessEqual(len(elements), 24, "卡片行数被外部值撑开了 —— 值里的换行没被压平")
        self.assertIn("退出码 1", body, "压平不等于丢掉：退出码还得说得出来")


class DrillAccountingTests(DrillBase):
    """**演习记在 `drill:<unit>` 键下，碰不着真单元的账。**

    2026-09-23 下午的事故：拿真单元名演了两次，`delivery-sweep` 的 6 小时冷却被占掉，
    真挂了也不会私聊（journal 里只有「仍在失败，第 N 次」）。

    跳过记账不是解法 —— 那会在连号中间挖洞。但记进真单元的账也是假话：
    「连续第 3 次」里有一次是人演的。另起一把键两边都不骗人，而且是**代码侧关掉**，
    不靠人记住「演习要用 drill 这个名字」。
    """

    def drill_key(self, unit=UNIT):
        return f"drill:{unit}"

    def test_a_drill_is_booked_under_its_own_key(self):
        code, _, _ = self.fail_once(now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.units()), [self.drill_key()], "演习不该出现在真单元名下")
        rec = self.units()[self.drill_key()]
        self.assertEqual(rec["streak"], 1)
        self.assertEqual(rec["last_fail"], NOW)
        self.assertEqual(rec["last_alert"], NOW, "演习那条也真送到了，它自己的冷却从此刻起算")

    def test_a_drill_cannot_silence_the_real_unit(self):
        """**9/23 那次事故的回归锁。** 演一次 → 一小时后它真挂了 → 管理员照样收得到。

        修之前：两条共用一把键，真故障那条被演习开启的 6 小时冷却捂住。
        """
        self.fail_once(now=NOW)  # 拿真单元名演（最容易犯的那种）
        self.real_failure(now=NOW + 3600)  # 一小时后它真挂了
        self.assertEqual(len(self.sent), 2, "真故障那条被演习的冷却捂住了")
        self.assertEqual(self.sent[0]["title"], f"告警演习：{UNIT}")
        self.assertEqual(self.sent[1]["title"], f"定时任务没跑成：{UNIT}")

    def test_a_drill_does_not_inflate_the_real_streak(self):
        """真挂 → 有人演习 → 还在挂：真单元的连号是 **2**，不是 3。

        记进同一本账的话，那条真告警会写「连续第 3 次」，而它只真挂过 2 次 ——
        一样是告警说假话，只不过这次是演习替它撒的谎。
        """
        self.real_failure(now=NOW)
        self.fail_once(now=NOW + 60)  # 中间有人手工验了一次告警通道
        self.real_failure(now=NOW + 120)
        units = self.units()
        self.assertEqual(units[UNIT]["streak"], 2, "真故障只有两次")
        self.assertEqual(units[self.drill_key()]["streak"], 1)

    def test_a_drill_still_keeps_its_own_ledger(self):
        """「不污染真单元」不等于「不记账」：演习自己的连号和冷却照常走 ——
        6 小时内连演两次，第二次被自己的冷却挡住（否则验告警通道的人一连点五下，
        管理员就收五条演习）。
        """
        self.fail_once(unit=DRILL_UNIT, now=NOW, INVOCATION_ID="abc")
        code, out, _ = self.fail_once(unit=DRILL_UNIT, now=NOW + 60, INVOCATION_ID="abc")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1, "演习也不该刷屏")
        self.assertIn("第 2 次", out)
        self.assertEqual(sorted(self.units()), [self.drill_key(DRILL_UNIT)])


class TriggerUnitTests(unittest.TestCase):
    """`MONITOR_*` 到底什么时候有 —— 单元文件这半边。

    **承重的是 `@%n` 那条**：systemd 只在「同一个 handler 实例只有一个触发方」时才注入
    `MONITOR_*`（v255 `service.c:1574 service_get_triggering_service()`，多于一个
    `OnFailureOf` 依赖直接返回 NULL）。`OnFailure=delivery-unit-failed@%n.service`
    里的 `%n` 给每个被兜底的单元开一个实例，正是在保证这件事。换成不带实例名的
    `delivery-unit-failed.service`，两个单元同时挂 → 变量全没 → 落到判据第三层
    （有 `INVOCATION_ID` → 当真故障 + 留痕）。**那不是灾难，只是正文里少了「怎么死的」**
    —— 自从判据不再依赖 `MONITOR_UNIT` 缺失之后，这条从「承重」降级成了「保内容质量」。

    `Type=oneshot` 那条**不是**注入条件（2026-09-23 审计读源码推翻的假设之一），
    留着只是顺带记录现状，别拿它当护身符。
    """

    ROOT = Path(__file__).resolve().parents[2] / "deploy" / "panel"

    #: 少于这个数说明 glob 没匹配到东西（改目录结构、改文件名、跑测试的 cwd 不对）。
    #: 守卫塞在取数据的那一步里，**每个消费者自带**：只挂在兄弟用例上的话，
    #: 这条断言自己空转变绿，而空转的覆盖类断言和写对了长得一模一样。
    UNITS_AT_LEAST = 6

    def triggering_units(self):
        found = []
        for path in sorted(self.ROOT.glob("*.service")):
            text = path.read_text(encoding="utf-8")
            # 顶格才是指令；注释里也会提到 OnFailure=
            if any(ln.startswith("OnFailure=delivery-unit-failed@") for ln in text.splitlines()):
                found.append((path.name, text))
        self.assertGreaterEqual(
            len(found),
            self.UNITS_AT_LEAST,
            f"挂兜底告警的单元怎么变这么少了：{[n for n, _ in found]}",
        )
        return found

    @staticmethod
    def directives(text: str, key: str) -> list:
        return [ln.split("=", 1)[1].strip() for ln in text.splitlines() if ln.startswith(f"{key}=")]

    def test_the_fallback_is_hooked_up_as_a_per_unit_template(self):
        """`@%n` 那一截不是写着好看的 —— 它是 `MONITOR_*` 能不能到手的唯一条件。"""
        for name, text in self.triggering_units():
            with self.subTest(unit=name):
                self.assertEqual(
                    self.directives(text, "OnFailure"),
                    ["delivery-unit-failed@%n.service"],
                    f"{name} 的 OnFailure 不是按单元实例化的模板：两个单元同时挂时，"
                    "systemd 不再注入 MONITOR_*，告警正文里就没有「怎么死的」",
                )

    def test_the_timed_units_happen_to_be_oneshot(self):
        """**只是记录现状，不是不变量。**

        这里曾经写着「`Type=oneshot` 是 `MONITOR_*` 的注入条件」—— 2026-09-23 审计读
        v255 源码后推翻了：真正的条件是上面那条「一个 handler 实例只有一个触发方」。
        改成别的 `Type=` 不会让告警失真，所以这条**不该拿来拦人**；留着是因为这几个
        单元都是「跑完就退」的定时任务，形态一致本身有信息量（哪天有人把某个改成
        `Type=simple`，值得停下来问一句为什么，而不是被一条假不变量挡住）。
        """
        for name, text in self.triggering_units():
            with self.subTest(unit=name):
                self.assertEqual(self.directives(text, "Type"), ["oneshot"], name)


class FallbackCoverageTests(unittest.TestCase):
    """**每个定时任务都得挂上兜底告警。** 漏挂是静默的。

    兜底存在的全部理由是「任务自己没来得及发告警」（崩溃、超时被杀、导入失败 ——
    这几种情况进程拿不到报告）。新加一个 timer 时忘了在对应的 service 里写
    `OnFailure=`，不会报错、不会红、日志里也看不出来：那个任务从此崩了没人知道，
    而「没人知道」和「一直没崩」长得一模一样，可以这么过几个月。

    所以按 **timer** 逐个核，而不是按 service ——「哪些任务是定时跑的」才是这条的判据，
    `delivery-unit-failed@.service` 自己没有 timer，也就不在名单里（它不该自举）。
    """

    ROOT = Path(__file__).resolve().parents[2] / "deploy" / "panel"

    #: 少于这个数就说明 glob 没匹配到东西，否则下面的循环一条不跑、**空转变绿**。
    #: 守卫在取数据这一步里，每个消费者自带 —— 覆盖类断言最容易烂在这儿。
    TIMERS_AT_LEAST = 7

    def timer_targets(self):
        """`delivery-X.timer` 到底触发谁：显式 `Unit=`，
        没写就是同名的 `.service`（systemd 默认）。"""
        targets = []
        for timer in sorted(self.ROOT.glob("*.timer")):
            lines = timer.read_text(encoding="utf-8").splitlines()
            explicit = [ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("Unit=")]
            targets.append((timer.name, explicit[0] if explicit else f"{timer.stem}.service"))
        self.assertGreaterEqual(len(targets), self.TIMERS_AT_LEAST, f"只找到 {targets}")
        return targets

    def test_every_timer_points_at_a_service_that_exists(self):
        """先钉住名单本身 —— 打错名字的 timer 会静静地什么也不跑。"""
        for timer, service in self.timer_targets():
            with self.subTest(timer=timer):
                self.assertTrue((self.ROOT / service).exists(), f"{timer} 指向的 {service} 不在")

    def test_every_timed_job_has_the_fallback_alert_hooked_up(self):
        for timer, service in self.timer_targets():
            with self.subTest(timer=timer):
                lines = (self.ROOT / service).read_text(encoding="utf-8").splitlines()
                self.assertIn(
                    "OnFailure=delivery-unit-failed@%n.service",
                    [ln for ln in lines if ln.startswith("OnFailure=")],
                    f"{service}（由 {timer} 定时拉起）没挂兜底告警：它崩掉时没有任何人会知道",
                )


if __name__ == "__main__":
    unittest.main()
