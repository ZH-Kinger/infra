"""兜底告警的 6 小时冷却（`cli._alert_cooldown` + `unit-failed`），用**可控时钟**测。

`test_delivery_alert_fallback.py::UnitFailedTests` 锁的是拿真实时间也能看出来的基本行为
（同一单元只发一条、不同单元互不影响、状态文件读不了照发）。跨 6 小时边界、连号、
写不下去、状态文件各种畸形，只有把时间钉住才测得准，所以单独放这里。

冷却是 2026-09-23 那次刷屏的第二道闸。第一道是退出码分级（sweep 每轮退 1 →
`OnFailure` 每分钟触发一次），第二道是这个：**就算真有单元一直挂着，也只私聊到
一天 4 条为止。** 但它的性质必须是 fail-open ——

    冷却记录读不了 / 写不了 / 内容畸形  → **照发**
    只有「上次确实发过、还不到 6 小时」 → 才静默

因为这个文件唯一的作用是少刷几条消息；它坏掉时把告警一起咽掉，等于拿「烦」换「以为没事」。

`--state` 全部显式传到临时目录。默认值现在是 `/var/lib/delivery/alert-state.json`
（环境变量 `DELIVERY_ALERT_STATE` 可覆盖，单元文件里就是这么指的）：不传的话用例会往那个
真路径写 —— 本机跑要么 `PermissionError`、要么**接下来 6 小时每次跑都红**（被自己上一次
的记录冷却掉）。它从前是仓库相对路径 `identity/alert-state.json`，改掉是为了不给兜底告警
单元开整个 identity/ 的写权限（审计 Med-3）；代码默认值和单元文件的配对关系锁在
`UnitFileTests`。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from delivery import cli

#: 真实量级的时间戳。冷却判的是 `now - last_alert >= 6h`，而「没记过」= `last_alert` 0；
#: 拿 100.0 之类的小数字当 now，连第一条告警都会被算进冷却期，测出来的全是假象。
NOW = 1_790_000_000.0
UNIT = "delivery-sweep.service"


class CooldownBase(unittest.TestCase):
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

    def fail_once(self, *, unit=UNIT, now=NOW, state=None):
        """报一次单元失败，返回 `(退出码, stdout, stderr)`。"""
        path = self.state if state is None else state
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(cli.time, "time", lambda: now),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(["unit-failed", "--unit", unit, "--state", str(path)])
        return code, out.getvalue(), err.getvalue()

    def units(self, state=None):
        path = self.state if state is None else state
        return json.loads(path.read_text(encoding="utf-8"))["units"]


class CooldownWindowTests(CooldownBase):
    def test_the_first_failure_pages_and_claims_no_streak(self):
        """第一条必发，而且**不**写「连续第 1 次」—— 那只会让人以为自己漏收了前面几条。"""
        code, _, _ = self.fail_once()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("连续第", self.sent[0]["text"])

    def test_inside_the_window_it_is_quiet_but_keeps_counting(self):
        """一分钟一轮连挂 5 轮 = 1 条私聊，但连号要照加。

        不加的话跨过 6 小时那条会写「连续第 2 次」，读的人以为只挂了两轮、
        而实际上已经挂了 360 轮。
        """
        self.fail_once(now=NOW)
        for i in range(1, 5):
            code, out, _ = self.fail_once(now=NOW + 60 * i)
            self.assertEqual(code, 0, "静默不是失败：非零会把 OnFailure 再触发一遍，白冷却了")
            self.assertIn(f"第 {i + 1} 次", out, "journal 里要留一行，否则和「代码没跑」长得一样")
        self.assertEqual(len(self.sent), 1)
        rec = self.units()[UNIT]
        self.assertEqual(rec["streak"], 5)
        self.assertEqual(rec["last_fail"], NOW + 240)
        self.assertEqual(rec["last_alert"], NOW, "冷却期内刷新告警时刻 = 永远发不出下一条")

    def test_crossing_six_hours_pages_again_with_the_streak(self):
        self.fail_once(now=NOW)
        self.fail_once(now=NOW + 3600)
        code, _, _ = self.fail_once(now=NOW + cli.UNIT_ALERT_COOLDOWN)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("连续第 3 次", self.sent[1]["text"])
        self.assertEqual(self.units()[UNIT]["last_alert"], NOW + cli.UNIT_ALERT_COOLDOWN)

    def test_one_second_short_of_the_window_is_still_quiet(self):
        """边界两边各一发。差一秒就放行 = 冷却被 timer 的抖动磨掉，一天还是上千条。"""
        self.fail_once(now=NOW)
        self.fail_once(now=NOW + cli.UNIT_ALERT_COOLDOWN - 1)
        self.assertEqual(len(self.sent), 1)
        self.fail_once(now=NOW + cli.UNIT_ALERT_COOLDOWN)
        self.assertEqual(len(self.sent), 2)

    def test_the_second_window_starts_from_the_alert_not_from_the_failure(self):
        """计时从**上次告警**起算，不是从上次失败起算。

        按失败起算的话，一分钟一轮的单元每轮都在刷新起点 → 只要它一直挂着，
        第二条告警永远不会来（第一条之后就彻底静音了）。
        """
        self.fail_once(now=NOW)
        for i in range(1, 400):  # 连挂 6 个多小时，每分钟一轮
            self.fail_once(now=NOW + 60 * i)
        self.assertEqual(len(self.sent), 2, "第 361 轮跨过 6 小时，该来第二条")
        self.assertIn("连续第 361 次", self.sent[1]["text"])

    def test_each_unit_keeps_its_own_clock(self):
        """sweep 在冷却里，不能把刚挂的 refresh 一起捂住 —— 那是另一件事。"""
        self.fail_once(unit=UNIT, now=NOW)
        self.fail_once(unit=UNIT, now=NOW + 60)
        self.fail_once(unit="delivery-refresh.service", now=NOW + 60)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("delivery-refresh.service", self.sent[1]["title"])
        units = self.units()
        self.assertEqual(units[UNIT]["streak"], 2)
        self.assertEqual(units["delivery-refresh.service"]["streak"], 1)

    def test_a_recovered_unit_that_breaks_again_much_later_pages_immediately(self):
        """挂 → 好了几天 → 又挂：立刻发，而且从「第 1 次」重新数。

        连号归零是 2026-09-23 补的（审计 Low-6）：隔了三天的新故障要是写着
        「连续第 2 次」，读的人会以为它一直没好过。连号的细账在 `StreakResetTests`，
        这里只钉「隔多久都必须当场响」。
        """
        self.fail_once(now=NOW)
        self.fail_once(now=NOW + 3 * 86400)
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("连续第", self.sent[1]["text"], "隔了三天是新故障，不是第 2 次")


class StreakResetTests(CooldownBase):
    """连号归零：`last_fail` 距今超过 2 倍冷却窗 = 新故障，从第 1 次重新数（审计 Low-6）。

    不归零的话，一个恢复了三个月、今天重新挂掉的单元，第一条告警就写「这是连续第 400 次」。
    那句话会把人直接带偏（「都四百次了怎么现在才说」→ 去翻三个月前的日志），
    而这次改动的整个主题就是**别让告警说假话**。

    阈值取 2 倍窗（12h）不是 1 倍：冷却期内的静默轮次本来就不告警，恰好 1 倍时
    一次正常的「挂着没好」会被误判成新故障。
    """

    def seed(self, *, streak, last_fail, last_alert=None):
        """直接摆一份状态记录 —— 连挂三个月不可能用 `fail_once` 跑出来。"""
        rec = {
            "streak": streak,
            "last_fail": last_fail,
            "last_alert": last_fail if last_alert is None else last_alert,
        }
        self.state.write_text(json.dumps({"units": {UNIT: rec}}), encoding="utf-8")

    def test_a_failure_long_after_the_last_one_starts_counting_over(self):
        gap = 3 * 30 * 86400  # 好了三个月
        self.seed(streak=400, last_fail=NOW - gap)
        self.fail_once(now=NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("连续第", self.sent[0]["text"], "新故障不该继承三个月前的连号")
        self.assertEqual(self.units()[UNIT]["streak"], 1)

    def test_a_failure_inside_the_reset_window_keeps_the_streak(self):
        """还在 2 倍窗内 = 它一直没好：连号照加。

        （`last_alert` 特意放在 6 小时前，好让这一条发得出来、文案看得见连号。）
        """
        self.seed(streak=400, last_fail=NOW - 60, last_alert=NOW - cli.UNIT_ALERT_COOLDOWN)
        self.fail_once(now=NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("连续第 401 次", self.sent[0]["text"])

    def test_exactly_two_windows_is_still_the_same_streak(self):
        """边界两边各一发：判据是 `> 2 倍窗`，正好 2 倍算「还在挂着」。

        差一秒就归零的话，一个卡在 12 小时整的重试节奏上的单元会永远写「第 1 次」。
        """
        exact = NOW - 2 * cli.UNIT_ALERT_COOLDOWN
        self.seed(streak=7, last_fail=exact, last_alert=exact)
        self.fail_once(now=NOW)
        self.assertEqual(self.units()[UNIT]["streak"], 8)

        self.sent.clear()
        past = NOW - 2 * cli.UNIT_ALERT_COOLDOWN - 1
        self.seed(streak=7, last_fail=past, last_alert=past)
        self.fail_once(now=NOW)
        self.assertEqual(self.units()[UNIT]["streak"], 1)

    def test_a_record_without_last_fail_is_not_treated_as_a_new_failure(self):
        """老记录（改动之前写的）没有 `last_fail`。

        缺它就按「不知道上次什么时候挂的」处理：**不归零**。归零的话，升级那一刻
        所有正在挂的单元连号一起清空，而它们并没有好过。
        """
        self.state.write_text(
            json.dumps({"units": {UNIT: {"streak": 9, "last_alert": NOW - 7 * 3600}}}),
            encoding="utf-8",
        )
        self.fail_once(now=NOW)
        self.assertEqual(self.units()[UNIT]["streak"], 10)
        self.assertIn("连续第 10 次", self.sent[0]["text"])

    def test_a_daily_timer_can_never_build_a_streak(self):
        """**现状固定条 + 留给 dev 的一个问题**：归零阈值是绝对的 12 小时，
        而不是「相对这个单元自己的跑动周期」。

        跑得比 12 小时还稀的定时器，连号永远停在 1：
            delivery-assets.timer    OnCalendar=*-*-* 03:20:00      （24 小时一轮）
            delivery-iam-remind      OnCalendar=10:00 / 16:00       （最长间隔 18 小时）
        它们连着挂一个月，每天那条告警都长成「第一次挂」的样子 —— 而「连着挂了 30 天」
        恰恰是最该说出来的那句。（sweep / buckets 是 1 分钟一轮，refresh 20 分钟，
        moves 5 分钟，都在窗内，不受影响。）

        没把它当 bug 锁（`expectedFailure`）是因为方向得 dev 定：可以把阈值改成
        「2 × 该单元的周期」（要传周期进来），也可以干脆接受 —— 每天那条告警照发，
        只是少一句「已经连着挂了几次」。这条用例只保证行为是**被看见过**的，
        哪天改了它会红，而不是悄悄换掉语义。
        """
        daily = "delivery-assets.service"
        day_ago = NOW - 86400  # 昨天 03:20 那一轮也挂了，连着第 30 天
        self.state.write_text(
            json.dumps(
                {"units": {daily: {"streak": 30, "last_fail": day_ago, "last_alert": day_ago}}}
            ),
            encoding="utf-8",
        )
        code, _, _ = self.fail_once(unit=daily, now=NOW)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1, "照发 —— 丢的只是「连着挂了多久」这句")
        self.assertEqual(self.units()[daily]["streak"], 1, "24 小时一轮的单元连号永远是 1")
        self.assertNotIn("连续第", self.sent[-1]["text"])

    def test_a_junk_last_fail_does_not_crash_the_alert(self):
        """`last_fail` 坏成字符串/列表：当作「没有」，告警照发。fail-open 同本文件其余部分。"""
        for junk in ('"刚刚"', "[1, 2]", "{}", "null"):
            with self.subTest(junk):
                self.sent.clear()
                self.state.write_text(
                    json.dumps({"units": {UNIT: {"streak": 2, "last_fail": json.loads(junk)}}}),
                    encoding="utf-8",
                )
                code, _, _ = self.fail_once(now=NOW)
                self.assertEqual(code, 0)
                self.assertEqual(len(self.sent), 1)


class StateWriteTests(CooldownBase):
    """怎么写这个文件。写坏一次的代价是**永久**的 —— `_load_alert_state` 读不懂就直接
    返回、再也不重写，冷却从此静默关闭（审计 Low-1）。"""

    def test_the_temp_file_name_is_random_not_a_fixed_sidecar(self):
        """六个单元共用这一个告警单元。两个同一秒挂掉、都去写同一份状态文件时，
        固定的 `alert-state.tmp` 会被两个进程同时打开、互相写进对方的字节 ——
        `replace` 过去就是一份谁也读不懂的 JSON。

        所以走 `mkstemp`（`O_EXCL` + 随机名）：这里断言它确实被用上，
        并且目录里不会留下任何固定名字的临时文件。
        """
        import tempfile as tempfile_mod

        seen = []
        real = tempfile_mod.mkstemp

        def spy(*a, **kw):
            seen.append(kw.get("prefix", ""))
            return real(*a, **kw)

        with mock.patch.object(tempfile_mod, "mkstemp", spy):
            self.fail_once(now=NOW)
        self.assertTrue(seen, "没走 mkstemp：临时文件名是拼出来的，两个单元会撞在一起")
        left = sorted(p.name for p in self.work.iterdir())
        self.assertEqual(left, ["alert-state.json"], f"临时文件没清干净：{left}")
        self.assertNotIn("alert-state.tmp", left)

    def test_the_record_lands_with_owner_only_permissions(self):
        """状态文件跟着 umask 走的话，同机其他账号能读出「哪个单元什么时候挂的」——
        不是机密，但它和 identity/ 下那些文件用的是同一套写法，别在这里开例外。"""
        self.fail_once(now=NOW)
        mode = stat.S_IMODE(self.state.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))

    def test_a_second_unit_does_not_clobber_the_first_units_record(self):
        """两个单元写同一份文件：后写的要**合并**，不是整份覆盖。

        覆盖的话，两个单元轮流挂 = 两边的 `last_alert` 互相抹掉 = 冷却对谁都不生效。
        """
        self.fail_once(unit=UNIT, now=NOW)
        self.fail_once(unit="delivery-refresh.service", now=NOW + 60)
        self.assertEqual(set(self.units()), {UNIT, "delivery-refresh.service"})

    def test_the_state_file_holds_nothing_but_unit_names_and_timestamps(self):
        """这个文件落在 identity/ 下，而 identity/ 是给员工数据准备的目录。
        冷却记录**不许**往里掺人名、union_id、错误正文 —— 多存一样就多一份泄漏面。"""
        self.fail_once()
        units = self.units()
        self.assertEqual(set(units), {UNIT})
        self.assertEqual(set(units[UNIT]), {"streak", "last_fail", "last_alert"})

    def test_the_knobs_are_what_the_unit_file_was_written_against(self):
        """两个旋钮，改哪个都会让冷却静默失效，而代码和测试全是绿的。

        · 6 小时 —— 一天 4 条。调小回去刷屏就回来了。
        · 默认落点 —— **必须是跨重启还在的绝对路径，而且不许在 identity/ 下**。
          落进 `/tmp`（或 `PrivateTmp=true` 的私有 tmp）= 每次跑冷却都清零；
          落回 `identity/` = 为了写一个时间戳，得给带着飞书凭证做出站请求的
          `delivery-unit-failed@.service` 开 tickets.json / people.json / admins.json
          的写权限（审计 Med-3）。配对的那一半（`StateDirectory=` ↔ `Environment=`）
          在 `UnitFileTests` 里锁。
        """
        self.assertEqual(cli.UNIT_ALERT_COOLDOWN, 6 * 3600)
        # 这个模块常量在 import 时就把环境变量读进来了：跑测试的机器上万一设了它，
        # 比的就该是它，否则这条会红在一个和源码无关的地方
        want = os.environ.get("DELIVERY_ALERT_STATE") or "/var/lib/delivery/alert-state.json"
        self.assertEqual(cli.UNIT_ALERT_STATE, want)
        state = cli.UNIT_ALERT_STATE
        self.assertTrue(state.startswith("/"), f"要绝对路径，不然落点取决于谁来跑：{state}")
        self.assertNotIn("/identity/", f"{state}/", f"冷却记录不许落进 identity/：{state}")
        self.assertFalse(state.startswith("/tmp/"), f"/tmp 每次跑都清零：{state}")


class CooldownFailOpenTests(CooldownBase):
    """**冷却坏了一律照发。** 宁可重复也别漏报：重复只是烦，漏报是「以为没事」。"""

    #: 每一份都写着「刚刚发过」的时刻：能读懂的话这一轮就该被捂住。
    #: 所以下面每条只要发出来了，就证明它确实走的是 fail-open 那条路。
    BROKEN = {
        "不是 JSON": "{这不是 json",
        "空文件": "",
        "顶层是数组": '[{"units": {}}]',
        "顶层是字符串": '"nope"',
        "顶层是 null": "null",
        "units 不是对象": '{"units": []}',
        "单元记录不是对象": '{"units": {"delivery-sweep.service": "yesterday"}}',
        "时间戳是字符串": '{"units": {"delivery-sweep.service": {"last_alert": "刚刚"}}}',
        "时间戳是列表": '{"units": {"delivery-sweep.service": {"last_alert": [1, 2]}}}',
        "记录里没有 last_alert": '{"units": {"delivery-sweep.service": {"streak": 3}}}',
        "streak 坏成了别的类型": '{"units": {"delivery-sweep.service": '
        '{"last_alert": 1790000000.0, "streak": "好多次"}}}',
    }

    def test_a_state_file_it_cannot_understand_never_suppresses_an_alert(self):
        for label, text in self.BROKEN.items():
            with self.subTest(label):
                self.sent.clear()
                state = self.work / f"{len(label)}-{abs(hash(label))}.json"
                state.write_text(text, encoding="utf-8")
                code, _, _ = self.fail_once(now=NOW + 60, state=state)
                self.assertEqual(code, 0)
                self.assertEqual(len(self.sent), 1, f"{label}：读不懂就该当没记过，照发")

    def test_a_readable_timestamp_still_counts_even_if_the_streak_is_junk(self):
        """反过来的一边：**时刻读得出来就按它判**，别因为 streak 不好看就重发。

        `streak` 坏成假值（`{}`、`[]`、`null`）时静默转成 0，不影响那个 6 小时窗口；
        坏成 `"好多次"` 那种会连 `last_alert` 一起丢掉、这一轮照发（见上面那张表）。
        两边都不漏报，差别只是多不多一条 —— 记在这里省得下次有人以为它坏了。
        """
        state = self.work / "junk-streak.json"
        state.write_text(
            json.dumps({"units": {UNIT: {"last_alert": NOW, "streak": {}}}}), encoding="utf-8"
        )
        code, out, _ = self.fail_once(now=NOW + 60, state=state)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [], "刚发过就是刚发过")
        self.assertIn("仍在失败", out)

    def test_a_missing_directory_is_created_instead_of_losing_the_alert(self):
        """第一次部署时 identity/ 可能还不在。建出来，别把告警赔进去。"""
        state = self.work / "还没建过" / "alert-state.json"
        code, _, err = self.fail_once(state=state)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(err, "")
        self.assertTrue(state.exists())

    @unittest.skipIf(os.geteuid() == 0, "root 写得进只读目录，这条测不到")
    def test_an_unwritable_state_file_pages_every_single_time(self):
        """写不下去（目录只读、盘满、单元忘了给 ReadWritePaths）→ 照发，只往 stderr 抱怨一句。

        **不许降级成静默**：记不住「上次什么时候发的」= 每次都是第一次，
        而每次都是第一次的正确行为就是每次都发。这条真发生过的形态是单元文件
        `ProtectSystem=strict` 没配 `ReadWritePaths=/opt/infra/identity`。
        """
        locked = self.work / "readonly"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(locked.chmod, stat.S_IRWXU)
        state = locked / "alert-state.json"
        code, _, err = self.fail_once(now=NOW, state=state)
        self.assertEqual(code, 0, "写不下冷却记录不是故障，告警本身送到了")
        self.assertIn("告警冷却", err, "至少要在 journal 里留下「冷却没生效」的线索")
        self.assertFalse(state.exists())
        self.fail_once(now=NOW + 60, state=state)  # 没记下来 → 还是第一次 → 还是发
        self.assertEqual(len(self.sent), 2)

    def test_an_unreadable_state_file_is_left_alone_and_keeps_paging(self):
        """读不懂的文件不被改写（怕盖掉别的单元的记录），代价是冷却一直不生效 ——
        这是刻意选的那一边：一直响 > 一直哑。"""
        state = self.work / "broken.json"
        state.write_text("{坏了", encoding="utf-8")
        self.fail_once(now=NOW, state=state)
        self.fail_once(now=NOW + 60, state=state)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(state.read_text(encoding="utf-8"), "{坏了")

    def test_an_alert_that_never_left_the_building_should_not_start_the_cooldown(self):
        """回归锁（2026-09-23 修）：告警没送到就不许开始冷却。

        修之前：发失败也照样把 `last_alert` 写成 now，接下来 6 小时一条都不发。

        `src/delivery/cli.py::_cmd_unit_failed` 先调 `_alert_cooldown(...)`（那里就把
        `last_alert` 写成了 now），再调 `_admin_alert(...)`；而 `_admin_alert` 返回非空
        意味着**一个人也没收到**（没配 `DELIVERY_FEISHU_APP_ID`、管理员名单里没
        union_id、飞书接口 5xx）。

        复现（就是本用例；本机跑 `-k never_left_the_building` 可见）：
          1. 飞书侧暂时不可用 → `unit-failed` 退 1，管理员什么也没收到；
          2. 一分钟后飞书恢复、单元仍在失败 → 这一条被冷却掉，静默返回 0；
          3. 直到 6 小时后才会有第一条真正送达的告警，期间 journal 里只有
             「仍在失败，第 N 次」，看起来一切正常。
        触发条件很普通：容器刚重启、env 还没注入，或飞书网关抖一下，
        刚好撞上单元第一次失败。

        期望：`last_alert` 只在**确实发出去**之后才刷新（发失败 = 没发过）。
        实际：发没发出去都刷新。
        修法：`_alert_cooldown` 拆成「问要不要发」和「记已经发了」两步，
        或者 `_cmd_unit_failed` 在 `_admin_alert` 返回非空时把 `last_alert` 写回原值。
        """
        self.reason = "飞书接口 500"
        code, _, _ = self.fail_once(now=NOW)
        self.assertEqual(code, 1)
        self.reason = ""  # 飞书恢复了
        self.fail_once(now=NOW + 60)
        self.assertEqual(len(self.sent), 2, "前一条根本没送出去，不该拿它当「刚发过」")

    def test_a_timestamp_from_the_future_does_not_silence_a_unit_forever(self):
        """回归锁（2026-09-23 修）：`last_alert` 落在未来时按「没记过」处理。

        修之前：这个单元被静音到真实时间追上为止，而且自愈不了。

        `_alert_cooldown` 只判 `now - last >= 6h`，不判 `last > now`，而且把未来的
        `last_alert` 原样写回文件，所以它自愈不了。

        复现（直接调函数，本机实测）：
            state = {"units": {"u": {"streak": 1, "last_alert": now + 86400}}}
            cli._alert_cooldown("u", state_path, now)        → (False, 2)，文件里仍是 now+86400
            cli._alert_cooldown("u", state_path, now + 7*3600) → (False, 3)
          整整一天没有一条告警，而单元可能一直在挂。
        来源不稀奇：云主机时钟跳变（宿主迁移、chrony 首次校时把系统时间往前推过一次），
        或者有人手动编辑过这个文件。这个模块的性质是 fail-open —— 读不懂都要发，
        「时刻不合常理」更该发。

        期望：`last_alert` 比 now 还晚 → 当作没记过，照发并把时刻纠正成 now。
        实际：静默，且未来时刻被保留，下一轮继续静默。
        """
        self.state.write_text(
            json.dumps({"units": {UNIT: {"streak": 1, "last_alert": NOW + 86400}}}),
            encoding="utf-8",
        )
        self.fail_once(now=NOW)
        self.assertEqual(len(self.sent), 1, "时刻不合常理时也该照发")


class UnitFileTests(unittest.TestCase):
    """单元文件里那两行写不写，决定冷却是真生效还是静默失效。

    配对关系（2026-09-23 改）现在是：

        StateDirectory=delivery                                （systemd 建目录并给写权限）
        Environment=DELIVERY_ALERT_STATE=/var/lib/delivery/…   （代码往哪写）

    从前是 `ReadWritePaths=/opt/infra/identity` + 相对路径 `identity/alert-state.json`。
    **那一行不许回来**：这个单元带着飞书应用凭证做出站请求，是本机最靠外的进程之一，
    为了写一个时间戳文件不该获得覆盖 tickets.json / people.json / admins.json 的能力
    （审计 Med-3）。回退是静默的 —— 冷却照样工作，只是攻击面悄悄大了一圈。

    （`SuccessExitStatus` / `OnFailure` 那两行在
    `test_delivery_alert_fallback.py::UnitFileTests` 里锁 —— 不在
    `test_delivery_sweep_crash.py` 里，那个文件只有 `SweepPrologueTests`。）
    """

    ROOT = Path(__file__).resolve().parents[2] / "deploy" / "panel"
    UNIT_FILE = "delivery-unit-failed@.service"

    def unit_text(self) -> str:
        return (self.ROOT / self.UNIT_FILE).read_text(encoding="utf-8")

    @staticmethod
    def directives(text: str, key: str) -> list:
        """单元文件里某个指令的所有取值（注释行不算 —— 注释里也会提到这些指令名）。"""
        return [
            ln.split("=", 1)[1].strip()
            for ln in text.splitlines()
            if ln.startswith(f"{key}=")  # 顶格才是指令，`# ReadWritePaths=…` 不是
        ]

    def test_the_fallback_unit_may_write_the_cooldown_record(self):
        """`delivery-unit-failed@.service` 开了 `ProtectSystem=strict`，整个文件系统只读。

        不显式给一块可写的地方，冷却记录**每次都写不下去** → 每次都是第一次 →
        一分钟一轮的 timer 一天一千多条私聊。而且它不会报错、不会让单元变红，
        只是那个 6 小时窗口从来没生效过 —— 这正是「配错了也一声不吭」的那类。

        给法只认 `StateDirectory=`：systemd 自己建目录、属主是 `User=`、重启保留，
        也就没有「root 手跑一次把属主改成 root、冷却从此静默失效」那个老坑。
        """
        text = self.unit_text()
        self.assertIn("ProtectSystem=strict", text)  # 前提没了的话下面这条也失去意义
        state_dirs = self.directives(text, "StateDirectory")
        self.assertTrue(state_dirs, "少了 StateDirectory，冷却记录落不了盘")
        self.assertIn("delivery", " ".join(state_dirs).split(), f"StateDirectory：{state_dirs}")

    def test_the_fallback_unit_cannot_write_the_identity_directory(self):
        """**回归锁**：`ReadWritePaths=…/identity` 不许回来（审计 Med-3）。

        回来了不会有任何人发现：冷却照常工作、测试照样绿，只是这个带着飞书应用凭证
        出网的单元又能覆盖 tickets.json（申请单台账）/ people.json（全员名册）/
        admins.json（管理员名单，能写就等于能给自己发管理员）。

        断言写成「一条 ReadWritePaths 都不许有」而不是「不许含 identity」：将来真要放行
        别的目录，应该有人在这里停一下、把理由写进来，而不是顺手加一行。
        """
        text = self.unit_text()
        allowed = self.directives(text, "ReadWritePaths")
        self.assertEqual(allowed, [], f"这个单元不该有任何 ReadWritePaths：{allowed}")
        self.assertNotIn(
            "ReadWritePaths=/opt/infra/identity",
            text,
            "identity/ 的写权限回来了：为了一个时间戳文件，赔上整个员工数据目录",
        )

    def test_the_cooldown_state_lands_inside_the_path_that_is_writable(self):
        """代码往哪写、单元给哪块可写，是**一对**：改了一边就等于关掉冷却。

        `StateDirectory=delivery` 的落点由 systemd 固定为 `/var/lib/delivery`
        （`$STATE_DIRECTORY`），所以这里按这个前缀核对单元里给代码的那个环境变量。
        """
        text = self.unit_text()
        state_dir = next(iter(self.directives(text, "StateDirectory")), "").split()[0]
        env = [
            v for v in self.directives(text, "Environment") if v.startswith("DELIVERY_ALERT_STATE=")
        ]
        self.assertTrue(
            env, "单元没告诉代码往哪写：DELIVERY_ALERT_STATE 没设，代码会用自己的默认值"
        )
        path = env[0].split("=", 1)[1].strip().strip('"')
        want_prefix = f"/var/lib/{state_dir}/"
        self.assertTrue(
            path.startswith(want_prefix),
            f"冷却记录会落到 {path}，而单元给的可写目录是 {want_prefix}",
        )
        # 代码默认值也要落在同一块地方：单元里哪天漏了 Environment= 那一行，
        # 冷却记录得照样落进 StateDirectory，而不是掉进一个只读路径
        self.assertTrue(
            cli.UNIT_ALERT_STATE.startswith(want_prefix) or os.environ.get("DELIVERY_ALERT_STATE"),
            f"代码默认值 {cli.UNIT_ALERT_STATE} 不在 {want_prefix} 下",
        )


if __name__ == "__main__":
    unittest.main()
