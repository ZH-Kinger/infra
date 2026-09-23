"""「这一行是出了问题，还是进度播报」——`flows.TROUBLE_WORDS` / `flows.is_trouble`。

为什么这张表值得一个自己的文件
──────────────────────────────
它是退出码分级的判据（`cli_requests._sweep` 拿它把步骤输出分进 `broken` / `handled`），
而 2026-09-23 的线上事故有**两面**，这张表两面都踩过：

  · 判得太宽 → 一张办不了的单子每分钟触发一次兜底告警，人学会忽略告警；
  · 判得太窄 → **一个永远办不了的单子看起来和万事大吉一模一样**（退 0）。

后一面是这次改动的正主：`_sweep` 原先自己手写着 `("失败", "中断")`，于是两类**持续**
失败被算成干净一轮 ——「到期提醒仍然发不出去（…）」（重试稳态的行文，第二轮起就是它）
和「登录名还是没写进公司 IAM（…）」/「补写登录名出错（…）」。三条真实行文里一个
「失败」字都没有。

所以这个文件只做三件事：
  1. 五个词各来一条**从 `flows.py` 摘的真实行文**，证明它认得；
  2. 真实的**进度行**一条都不许被判成问题（误判会把正常日志变成一分钟一条私聊）；
  3. `TableCoverageTests` 直接读 `flows.py` 的语法树，把所有「单号：…」行**穷举**出来，
     一条一条对答案。以后有人加了新的问题行、用了表里没有的词，这里会红 ——
     而不是等线上某张单子安静地卡上两天。

退出码那一半（问题行 → 1 还是 3）在 `test_delivery_sweep_exit_codes.py`。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from delivery import flows

FLOWS_PY = Path(flows.__file__)

#: 真实问题行（`flows.py:行号` → 渲染后的样子）。占位符都填成线上会出现的内容。
#: **一条都不是编的**，可以拿行号去对。
TROUBLE_LINES = {
    1574: "req-1：开通中断，已标为失败",
    1592: "req-1：审批通过后继续处理失败（云接口 500）",
    1596: "req-1：审批通过后中断，继续处理失败（已失败）",
    1753: "tak-1：到期提醒仍然发不出去（申请人没有飞书标识）",
    1764: "tak-1：到期提醒发送失败",
    1802: "tak-1：回收失败（AccessDenied），下次重试",
    1902: "tak-1：回收失败，下次重试",
    2028: "req-1：回收失败，下次重试",
    2665: "req-1：登录名还是没写进公司 IAM（他现在登不进去：没接上公司 IAM 的写入）",
    2668: "req-1：补写登录名出错（IAM 接口 500）",
}

#: 真实进度行。判成问题的代价：定时任务每跑顺一轮就退 3 ——
#: 而 3 的含义是「有单子有问题、去待办页看」，那页上却什么都没有。
PROGRESS_LINES = {
    1600: "req-1：审批通过后停住的单子已接着处理（已完成）",
    1776: "tak-1：已提醒，还有 3 天到期",
    1915: "tak-1：已由其他进程处理",
    1938: "tak-1：云上没有要清理的东西",
    1957: "tak-1：状态已变，本轮不清理",
    1959: "tak-1：单子已不在",
    1988: "tak-1：已删除没送达的凭证子账号 tempak-abc",
    2053: "req-1：已由其他进程处理",
    2670: "req-1：已补写登录名进公司 IAM",
}

#: `_mark_revoked` / `_revoke` 的收尾行是 `{单号}：{note}`，算不算问题取决于 note。
#: 这些 note 全是正常收尾，一条都不该判成问题。
REVOKE_NOTES = (
    "tak-1：临时凭证已到期自动失效",
    "tak-1：管理员作废：临时凭证本就到点自灭，云上没有残留",
    "tak-1：到期：已删除子账号 tempak-abc 及其密钥和策略",
    "req-1：已移出 grp-oss-read；保留 grp-default",
    "req-1：已撤销 AliyunOSSReadOnlyAccess",
    "req-1：无需回收",
)


class WordTests(unittest.TestCase):
    """五个词各一条。用真实行文，不用「含有失败二字的字符串」那种同义反复。"""

    CASES = {
        "失败": TROUBLE_LINES[1764],
        "中断": TROUBLE_LINES[1574],
        "发不出去": TROUBLE_LINES[1753],
        "出错": TROUBLE_LINES[2668],
        "还是没写进": TROUBLE_LINES[2665],
    }

    def test_each_word_is_carried_by_a_real_line(self):
        for word, line in self.CASES.items():
            with self.subTest(word):
                self.assertIn(word, line)
                self.assertTrue(flows.is_trouble(line), line)

    def test_the_table_is_exactly_these_five(self):
        """加词是好事（新的问题行要被认出来），但**删词是危险的**：删掉哪个，
        对应那类持续失败就重新变回「干净一轮」。所以整表钉死，改动必须路过这里。"""
        self.assertEqual(set(flows.TROUBLE_WORDS), set(self.CASES))
        self.assertEqual(len(flows.TROUBLE_WORDS), len(set(flows.TROUBLE_WORDS)), "有重复词")

    def test_no_word_is_empty(self):
        """空串会让 `any(word in line ...)` 恒真 —— 每一行都是问题行，
        于是每一轮都退非零。一个手滑的逗号就能做到（`("失败", "", "中断")`）。"""
        for word in flows.TROUBLE_WORDS:
            with self.subTest(word=repr(word)):
                self.assertTrue(str(word).strip(), "空词会把所有行都判成问题")

    def test_the_words_are_not_substrings_of_the_progress_vocabulary(self):
        """每个词都要在**所有**真实进度行上判假。

        单看一条「某个词不误判」不够：词是一张表，`any()` 里任何一个命中都算数。
        """
        for word in flows.TROUBLE_WORDS:
            for line in PROGRESS_LINES.values():
                with self.subTest(word=word, line=line):
                    self.assertNotIn(word, line)


class RealLineTests(unittest.TestCase):
    def test_every_real_problem_line_is_trouble(self):
        for lineno, line in sorted(TROUBLE_LINES.items()):
            with self.subTest(f"flows.py:{lineno}"):
                self.assertTrue(flows.is_trouble(line), f"flows.py:{lineno} 的问题行没被认出来")

    def test_no_real_progress_line_is_trouble(self):
        for lineno, line in sorted(PROGRESS_LINES.items()):
            with self.subTest(f"flows.py:{lineno}"):
                self.assertFalse(flows.is_trouble(line), f"flows.py:{lineno} 的进度行被误判")

    def test_a_normal_revoke_note_is_not_trouble(self):
        """回收成功的收尾行不是问题行。

        这一条格外要紧：`revoke_expired` 这一步是 `urgent=True`，它的问题行直接退 1、
        直接惊动人。误判在这里的后果不是多一条日志，是**每收掉一张到期凭证就私聊一次**。
        """
        for line in REVOKE_NOTES:
            with self.subTest(line):
                self.assertFalse(flows.is_trouble(line), line)

    def test_the_two_wordings_of_the_same_failure_are_both_caught(self):
        """同一件事的两种行文都要认出来。

        「到期提醒发送失败」是第一轮，「到期提醒仍然发不出去（…）」是第二轮起的稳态；
        sweep 一分钟一轮，所以线上绝大多数轮次打的是后者 —— 只认前者等于什么都没认。
        """
        self.assertTrue(flows.is_trouble(TROUBLE_LINES[1764]))
        self.assertTrue(flows.is_trouble(TROUBLE_LINES[1753]))


class InputTests(unittest.TestCase):
    """判据本身被喂了奇怪的东西时不许炸：它跑在 sweep 的主循环里，
    抛异常就等于这一步之后的行全不看了（而 `except` 在更外层，整步会被记成 broken）。"""

    def test_an_empty_line_is_not_trouble(self):
        self.assertFalse(flows.is_trouble(""))

    def test_a_non_string_is_coerced_instead_of_raising(self):
        for junk in (None, 0, [], {"id": "req-1"}):
            with self.subTest(repr(junk)):
                self.assertFalse(flows.is_trouble(junk))

    def test_a_non_string_carrying_the_word_still_counts(self):
        """`str()` 之后含问题词就算 —— 宁可多报一条，也别因为某一步改成返回别的类型
        就把整类失败漏掉。"""
        self.assertTrue(flows.is_trouble(["req-1：回收失败，下次重试"]))

    def test_the_word_is_matched_anywhere_in_the_line(self):
        """子串匹配，不锚定位置：问题词在 flows 的行文里有时在中间
        （`审批通过后中断，继续处理失败（…）`），有时在结尾（`到期提醒发送失败`）。"""
        self.assertTrue(flows.is_trouble(TROUBLE_LINES[1596]))
        self.assertTrue(flows.is_trouble("失败"))

    def test_the_state_change_line_is_not_trouble(self):
        """`_sweep` 自己打的那行状态流转 `{单号}：{旧状态} → {新状态}`。

        两件事：
        · 它压根不过 `is_trouble`（在 pending 同步那一段直接 `print`），
          所以流转本身永远不影响退出码 —— 这是对的，「待审批 → 已拒绝」不是故障；
        · 而且它打的是 `tickets.py` 里的**状态 id**（`failed` / `submit_failed`），
          不是中文 label。哪天有人图好看换成 `t.LABELS`，「开通失败」「提交失败」
          就会让每一条流转行都含「失败」二字 —— 那时这条注释是唯一的线索。
        """
        from delivery import tickets as tickets_mod

        line = f"req-1：{tickets_mod.PENDING} → {tickets_mod.FAILED}"
        self.assertFalse(flows.is_trouble(line), line)
        self.assertTrue(flows.is_trouble(f"req-1：{tickets_mod.LABELS[tickets_mod.FAILED]}"))

    def test_a_data_field_that_happens_to_contain_the_word_also_counts(self):
        """**已知的、刻意接受的误判方向**：判据是整行子串匹配，所以云上的用户组名、
        错误原文里带「失败」二字时，一条正常的收尾行也会被算成问题。

        接受它是因为两个方向不对称：多判一条 → 退 3（待办页上没东西，最多让人白看一眼）
        或者退 1（多一条私聊，有 6 小时冷却兜着）；少判一条 → 单子安静地卡死。
        这些字段都是管理员配的模板 / 云返回的错误，不是申请人能随手填的自由文本，
        所以拿不到「构造一行假告警」那种价值。

        记在这里是为了以后有人看到一条莫名其妙的退 3 时，能直接找到原因。
        """
        self.assertTrue(flows.is_trouble("req-1：已移出 grp-失败演练"))


class TableCoverageTests(unittest.TestCase):
    """直接读 `flows.py` 的语法树，把所有「单号：…」行穷举出来，一条一条对答案。

    这条是**面向未来**的：以后谁加了一条新的问题行（比如「…没能撤销」），
    而它用的词不在 `TROUBLE_WORDS` 里，这里会红并且点名那一行。
    没有它的话，新的一类持续失败会像这次一样，安安静静地退 0。
    """

    @staticmethod
    def ticket_lines() -> dict:
        """`{源码行号: 渲染后的模板}`。f-string 的插值位统一渲染成 `{}`。"""
        tree = ast.parse(FLOWS_PY.read_text(encoding="utf-8"))

        def render(node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.JoinedStr):
                return "".join(
                    v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
                )
            return None

        out = {}
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Return) and node.value is not None:
                text = render(node.value)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and node.args
            ):
                text = render(node.args[0])
            # 「单号：…」才是会被 sweep 打印、被分级的那种行
            if text and text.startswith("{}："):
                out[node.lineno] = text
        return out

    #: 收尾行 `{单号}：{note}` —— 算不算问题取决于 note，单看模板判不了，
    #: 对应的 note 在 `REVOKE_NOTES` 里逐条测。
    NOTE_PASSTHROUGH = "{}：{}"

    def test_every_ticket_line_in_flows_has_been_classified(self):
        lines = self.ticket_lines()
        self.assertGreater(len(lines), 15, "语法树没扫出东西，八成是 flows.py 的写法变了")
        decided = set(TROUBLE_LINES) | set(PROGRESS_LINES)
        for lineno, template in sorted(lines.items()):
            if template == self.NOTE_PASSTHROUGH:
                continue
            with self.subTest(f"flows.py:{lineno}"):
                self.assertIn(
                    lineno,
                    decided,
                    f"flows.py:{lineno} 是新的一行「{template}」，还没人决定它算不算问题行。"
                    "把它填进 TROUBLE_LINES（同时确认 TROUBLE_WORDS 认得出来）"
                    "或 PROGRESS_LINES。定时任务根本不会打印它 —— 也请填进 PROGRESS_LINES "
                    "并在那里写一句为什么，这张表要的是「每一行都有人看过」",
                )

    def test_the_classified_line_numbers_still_point_at_those_lines(self):
        """行号会漂。漂了之后这张表就是在给不存在的行背书，所以对一次首尾。

        比的是模板里那几段**字面量**（插值位填了什么无所谓），够挡住「整段代码被挪走」
        这种漂移，又不会因为改一个变量名就红。
        """
        lines = self.ticket_lines()
        for lineno, sample in sorted({**TROUBLE_LINES, **PROGRESS_LINES}.items()):
            with self.subTest(f"flows.py:{lineno}"):
                self.assertIn(lineno, lines, f"flows.py:{lineno} 已经不是一行「单号：…」了")
                chunks = [c for c in lines[lineno].split("{}") if c.strip("：")]
                self.assertTrue(chunks, f"flows.py:{lineno} 只剩插值位了：{lines[lineno]}")
                for chunk in chunks:
                    self.assertIn(
                        chunk.strip("："),
                        sample,
                        f"flows.py:{lineno} 的行文变成了「{lines[lineno]}」，"
                        f"而表里记的样本是「{sample}」——行号漂了，或者行文改了",
                    )

    def test_every_word_in_the_table_is_earning_its_keep(self):
        """表里的每个词都要有至少一条真实行文在用。

        没人用的词是纯风险：它只会在别的地方意外命中（比如某个用户组名里有它），
        把正常的一轮判成有问题。
        """
        source = FLOWS_PY.read_text(encoding="utf-8")
        for word in flows.TROUBLE_WORDS:
            with self.subTest(word):
                self.assertIn(word, source, f"`{word}` 在 flows.py 里一条行文都不对应")
                self.assertTrue(
                    any(word in line for line in TROUBLE_LINES.values()),
                    f"`{word}` 没有对应的真实问题行样本",
                )


if __name__ == "__main__":
    unittest.main()
